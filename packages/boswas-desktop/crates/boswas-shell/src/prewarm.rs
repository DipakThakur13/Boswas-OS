//! Getting ready ahead of time for what the user opens first.
//!
//! The first time Start opens, every application's icon is rasterised at
//! its size, every label is shaped, and the panel's shadow and material are
//! drawn; at 200 % that took a third of a second, during which the desktop
//! stood still. Instead, once the desktop is up (and again when the scale,
//! the theme, the wallpaper or the applications change):
//!
//! * the icons Start and the taskbar show are rasterised on a worker
//!   thread, with an icon loader of its own, and handed to the shell's;
//! * then, at a quiet moment (nothing being drawn or moving, no flyout
//!   open), Start, quick settings and the notification centre are drawn
//!   once on a canvas nobody sees, which shapes their text and keeps their
//!   panels.

use boswas_ui::{icons::IconLoader, tokens::tokens, Canvas, Scheme};
use smithay_client_toolkit::reexports::calloop::channel;
use tiny_skia::Pixmap;

use crate::{
    notify, quick, start,
    state::{Flyout, Mode},
    ui::{Frame, Rect},
    Role, Shell, PAD,
};

/// What the work was done for; done again when this changes.
#[derive(Clone, Debug, PartialEq)]
pub struct Key {
    scale: u32,
    scheme: bool,
    output: (u32, u32),
    backdrop: u64,
    text: u32,
    /// The applications' ids and icons, hashed.
    apps: u64,
}

#[derive(Default)]
pub struct Prewarm {
    /// The icons are being rasterised for this.
    icons_for: Option<Key>,
    /// The worker has finished.
    icons_done: bool,
    /// Everything was done for this.
    done: Option<Key>,
}

/// The sizes (logical pixels) an application's icon is drawn at: Start's
/// tiles (and pressed), recommendations, the list, search results and the
/// taskbar (and pressed).
fn icon_sizes() -> Vec<f32> {
    let t = tokens();
    let (tile, list, bar) = (t.shell("start-icon"), t.shell("start-list-icon"), t.shell("taskbar-icon"));
    // A tile magnified under the pointer grows into the 10 px above it
    // (magnify.rs).
    vec![tile, tile - 4.0, tile + 10.0, 32.0, list, 40.0, 24.0, bar, bar - 4.0]
}

/// The icons (names and sizes in pixels) applications are drawn with at
/// `scale`, with the stand-in for those without one.
pub fn icons_wanted(apps: &[crate::apps::App], scale: f32) -> Vec<(String, u32)> {
    let mut wanted: Vec<(String, u32)> = Vec::new();
    let names = apps.iter().map(|a| a.icon.as_str()).chain(["application-x-executable"]);
    for name in names.filter(|n| !n.is_empty()) {
        for size in icon_sizes() {
            let item = (name.to_owned(), (size * scale).round().max(1.0) as u32);
            if !wanted.contains(&item) {
                wanted.push(item);
            }
        }
    }
    wanted
}

impl Shell {
    /// Called once per turn of the event loop: starts or continues the
    /// work, when there is something to do.
    pub fn prewarm_tick(&mut self) {
        if self.st.mode != Mode::Desktop {
            return;
        }
        let Some(taskbar) = self.surfaces.iter().find(|s| s.role == Role::Taskbar && s.configured) else {
            return;
        };
        let (output, scale) = (taskbar.output.clone(), taskbar.scale);
        let size = self.output_size(&output);
        let key = Key {
            scale: scale.to_bits(),
            scheme: self.res.theme.scheme == Scheme::Light,
            output: (size.0 as u32, size.1 as u32),
            backdrop: self.res.backdrop_generation,
            text: (crate::ui::text_scale() * 100.0) as u32,
            apps: {
                use std::hash::{Hash, Hasher};
                let mut hasher = std::collections::hash_map::DefaultHasher::new();
                for app in &self.st.apps {
                    (&app.id, &app.icon).hash(&mut hasher);
                }
                hasher.finish()
            },
        };
        if self.prewarm.done.as_ref() == Some(&key) {
            return;
        }
        // The icons, on a worker thread.
        let icons_current = self.prewarm.icons_for.as_ref().is_some_and(|k| k.scale == key.scale && k.scheme == key.scheme && k.apps == key.apps);
        if !icons_current {
            self.prewarm.icons_for = Some(key.clone());
            self.prewarm.icons_done = false;
            self.rasterise_icons(scale);
            return;
        }
        if !self.prewarm.icons_done {
            return;
        }
        // The rest at a quiet moment.
        let quiet = self.st.flyout.is_none() && self.surfaces.iter().all(|s| !s.dirty && s.anim.is_none());
        if !quiet {
            return;
        }
        let started = std::time::Instant::now();
        self.predraw(&output, scale);
        tracing::debug!(target: "boswas_shell::stats", "flyouts drawn ahead of time in {} ms", started.elapsed().as_millis());
        self.prewarm.done = Some(key);
    }

    /// Rasterises, on a worker thread, the icons of every application that
    /// are not kept yet, at the sizes they are drawn at.
    fn rasterise_icons(&mut self, scale: f32) {
        let wanted: Vec<(String, u32)> = icons_wanted(&self.st.apps, scale).into_iter().filter(|(name, size)| !self.res.icons.has(name, *size)).collect();
        if wanted.is_empty() {
            self.prewarm.icons_done = true;
            return;
        }
        let theme = match self.res.theme.scheme {
            Scheme::Dark => "Boswas-Prism",
            Scheme::Light => "Boswas-Prism-Light",
        };
        let (sender, receiver) = channel::channel::<(String, u32, Option<Pixmap>)>();
        let inserted = self.loop_handle.insert_source(receiver, |event, _, shell| match event {
            channel::Event::Msg((name, size, pixmap)) => shell.res.icons.keep(&name, size, pixmap),
            channel::Event::Closed => shell.prewarm.icons_done = true,
        });
        if inserted.is_err() {
            self.prewarm.icons_done = true;
            return;
        }
        let spawned = std::thread::Builder::new().name("icons".into()).spawn(move || {
            let loader = IconLoader::new(theme);
            for (name, size) in wanted {
                let pixmap = loader.render(&name, size);
                if sender.send((name, size, pixmap)).is_err() {
                    return;
                }
            }
        });
        if spawned.is_err() {
            self.prewarm.icons_done = true;
        }
    }

    /// Draws Start, quick settings and the notification centre once where
    /// nobody sees them, as they will be drawn when they open.
    fn predraw(&mut self, output: &smithay_client_toolkit::reexports::client::protocol::wl_output::WlOutput, scale: f32) {
        let size = self.output_size(output);
        self.prepare_backdrop(size);
        for kind in [Flyout::Start, Flyout::Quick, Flyout::Notifications] {
            let (_, ((w, h), _, origin)) = self.flyout_geometry(kind, size);
            let (w, h) = (w as f32, h as f32);
            let Some(mut canvas) = Canvas::new(w, h, scale) else {
                continue;
            };
            let panel = Rect::new(PAD, PAD, w - 2.0 * PAD, h - 2.0 * PAD);
            let mut hits = Vec::new();
            let mut frame = Frame {
                canvas: &mut canvas,
                res: &mut self.res,
                pointer: None,
                pressed: false,
                hits: &mut hits,
                focus: None,
                origin,
            };
            match kind {
                Flyout::Start => start::draw(&mut frame, &self.st, panel),
                Flyout::Quick => quick::draw(&mut frame, &self.st, panel),
                _ => notify::draw_centre(&mut frame, &self.st, panel),
            }
        }
    }
}
