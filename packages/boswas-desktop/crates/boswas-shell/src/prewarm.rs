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
//! * then, at quiet moments (nothing being drawn or moving, no flyout
//!   open), Start, quick settings and the notification centre are drawn
//!   on a canvas nobody sees, which shapes their text and keeps their
//!   panels. This took about 200 ms at 200 %, in one piece, on the event
//!   loop: a click then waited for all of it. It is drawn in slices
//!   instead (a corner that makes a panel, then bands of a row of tiles),
//!   one per turn of the event loop while nothing else happens, so that
//!   input is handled between any two.

use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};

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
    /// The slices still to draw.
    work: Option<Work>,
}

/// The sizes (logical pixels) an application's icon is drawn at: Start's
/// tiles (and pressed), recommendations, the list, search results and the
/// taskbar (and pressed).
fn icon_sizes() -> Vec<f32> {
    let t = tokens();
    let (tile, list, bar) = (t.shell("start-icon"), t.shell("start-list-icon"), t.shell("taskbar-icon"));
    vec![tile, tile - 4.0, 32.0, list, 40.0, 24.0, bar, bar - 4.0]
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
            self.prewarm.work = None;
            return;
        }
        // The icons, on a worker thread.
        let icons_current = self.prewarm.icons_for.as_ref().is_some_and(|k| k.scale == key.scale && k.scheme == key.scheme && k.apps == key.apps);
        if !icons_current {
            self.prewarm.icons_for = Some(key.clone());
            self.prewarm.icons_done = false;
            self.prewarm.work = None;
            self.rasterise_icons(scale);
            return;
        }
        if !self.prewarm.icons_done {
            return;
        }
        // The rest in slices, while the shell has nothing else to do: one
        // slice per turn of the event loop, so that a click waits for one
        // slice at most, not for the whole preparation.
        if !self.prewarm_quiet() {
            return;
        }
        if self.prewarm.work.as_ref().is_none_or(|work| work.key != key) {
            let backdrop = self.res.backdrop_generation;
            self.prepare_backdrop(size);
            if self.res.backdrop_generation != backdrop {
                // The backdrop is new: so is the key; a turn later.
                return;
            }
            let mut steps = VecDeque::new();
            for kind in [Flyout::Start, Flyout::Quick, Flyout::Notifications] {
                let (_, ((w, h), _, origin)) = self.flyout_geometry(kind, size);
                steps.extend(slices(kind, (w as f32, h as f32), origin));
            }
            self.prewarm.work = Some(Work { key, scale, steps, canvas: None, busy: Duration::ZERO, longest: Duration::ZERO, count: 0 });
            return;
        }
        let Some(mut work) = self.prewarm.work.take() else {
            return;
        };
        let Some(step) = work.steps.pop_front() else {
            return;
        };
        let started = Instant::now();
        let canvas = match work.canvas.take().filter(|c| c.width() == step.size.0 && c.height() == step.size.1) {
            Some(canvas) => Some(canvas),
            None => Canvas::new(step.size.0, step.size.1, work.scale),
        };
        if let Some(mut canvas) = canvas {
            draw_slice(&mut self.res, &self.st, &step, &mut canvas);
            work.canvas = Some(canvas);
        }
        let took = started.elapsed();
        work.busy += took;
        work.longest = work.longest.max(took);
        work.count += 1;
        if work.steps.is_empty() {
            tracing::debug!(
                target: "boswas_shell::stats",
                "flyouts drawn ahead of time in {} ms, in {} slices of at most {} µs",
                work.busy.as_millis(),
                work.count,
                work.longest.as_micros()
            );
            self.prewarm.done = Some(work.key);
        } else {
            self.prewarm.work = Some(work);
        }
    }

    /// Nothing is being drawn or moving and no flyout is open.
    fn prewarm_quiet(&self) -> bool {
        self.st.flyout.is_none() && self.surfaces.iter().all(|s| !s.dirty && s.anim.is_none())
    }

    /// When the event loop is to come back for the next slice of the
    /// preparation (`None`: no slice is waiting).
    pub fn prewarm_deadline(&self, now: Instant) -> Option<Instant> {
        self.prewarm.work.as_ref().filter(|w| !w.steps.is_empty() && self.prewarm_quiet()).map(|_| now)
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
}

/// Height (logical pixels) of the band of a flyout one slice draws: about
/// a row of Start's tiles.
const BAND: f32 = 96.0;

/// The preparation still to do.
pub struct Work {
    key: Key,
    scale: f32,
    steps: VecDeque<Step>,
    /// The canvas nobody sees, of the flyout being drawn.
    canvas: Option<Canvas>,
    busy: Duration,
    longest: Duration,
    count: u32,
}

/// One slice: a part of a flyout drawn where nobody sees it, as it will
/// be drawn when it opens. Drawing only that part (the rest is skipped, as
/// for a hover change) still shapes its labels and makes its panel.
#[derive(Clone, Debug, PartialEq)]
struct Step {
    kind: Flyout,
    /// The surface's size and place on the output (logical pixels).
    size: (f32, f32),
    origin: (f32, f32),
    /// The part drawn (logical pixels).
    part: Rect,
}

/// The slices of a flyout of `size`: first a corner of its panel (which
/// makes the panel, its shadow and material, and nothing else), then its
/// bands from the top.
fn slices(kind: Flyout, size: (f32, f32), origin: (f32, f32)) -> Vec<Step> {
    let step = |part: Rect| Step { kind, size, origin, part };
    let mut steps = vec![step(Rect::new(PAD, PAD, 1.0, 1.0))];
    let mut top = 0.0;
    while top < size.1 {
        steps.push(step(Rect::new(0.0, top, size.0, BAND.min(size.1 - top))));
        top += BAND;
    }
    steps
}

/// Draws one slice on `canvas` (of the flyout's size).
fn draw_slice(res: &mut crate::ui::Resources, st: &crate::state::State, step: &Step, canvas: &mut Canvas) {
    let (w, h) = step.size;
    let scale = canvas.scale();
    canvas.begin_clip(&[crate::ui::device_rect(&step.part, scale)]);
    let panel = Rect::new(PAD, PAD, w - 2.0 * PAD, h - 2.0 * PAD);
    let mut hits = Vec::new();
    let mut frame = Frame {
        canvas: &mut *canvas,
        res,
        pointer: None,
        pressed: false,
        hits: &mut hits,
        focus: None,
        origin: step.origin,
    };
    match step.kind {
        Flyout::Start => start::draw(&mut frame, st, panel),
        Flyout::Quick => quick::draw(&mut frame, st, panel),
        _ => notify::draw_centre(&mut frame, st, panel),
    }
    canvas.end_clip();
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The preparation of Start at 200 % (its largest: 3.2 million pixels)
    /// in slices: once the slices have run, drawing Start whole shapes no
    /// more text and makes no panel (it is as quick as a later opening),
    /// and no slice takes long, so a click at that moment is not held up.
    /// The limit holds for an optimised build on a loaded machine:
    /// `cargo test --release slices -- --nocapture` prints the figures.
    #[test]
    fn the_preparation_runs_in_short_slices_and_prepares_everything() {
        let st = crate::state::State::for_tests();
        let scale = 2.0;
        let mut res = crate::ui::Resources::new(boswas_ui::Theme::of(Scheme::Dark));
        let icons = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../../desktop/icons");
        res.icons = IconLoader::with_base_dirs("Boswas-Prism", &[icons.clone()]);
        // The icons, as the worker thread hands them over.
        let loader = IconLoader::with_base_dirs("Boswas-Prism", &[icons]);
        for (name, size) in icons_wanted(&st.apps, scale) {
            res.icons.keep(&name, size, loader.render(&name, size));
        }
        let size = (832.0 + 2.0 * PAD, 918.0 + 2.0 * PAD);
        let steps = slices(Flyout::Start, size, (0.0, 0.0));
        assert!(steps.len() > 5);
        let mut canvas = Canvas::new(size.0, size.1, scale).unwrap();
        // The first slice makes the panel (its shadow and material, once);
        // the others draw bands.
        let mut each = Vec::new();
        for step in &steps {
            let started = Instant::now();
            draw_slice(&mut res, &st, step, &mut canvas);
            each.push(started.elapsed());
        }
        let all: Duration = each.iter().sum();
        let panel_slice = each[0];
        let longest = each[1..].iter().copied().max().unwrap_or_default();
        let panels = res.panel_count();
        let shaped = res.text.shaped_count();
        let started = Instant::now();
        let mut whole = Canvas::new(size.0, size.1, scale).unwrap();
        let panel = Rect::new(PAD, PAD, 832.0, 918.0);
        start::draw(&mut Frame { canvas: &mut whole, res: &mut res, pointer: None, pressed: false, hits: &mut Vec::new(), focus: None, origin: (0.0, 0.0) }, &st, panel);
        let opening = started.elapsed();
        assert_eq!(res.panel_count(), panels, "the slices made Start's panel");
        assert_eq!(res.text.shaped_count(), shaped, "the slices shaped every label of Start");
        println!("Start at 200 % prepared in {} slices: {all:?} in all, {panel_slice:?} for its panel, the longest band {longest:?}; then opening it draws in {opening:?}", steps.len());
        #[cfg(not(debug_assertions))]
        {
            assert!(longest < Duration::from_millis(8), "a band of the preparation took {longest:?}");
            assert!(panel_slice < Duration::from_millis(60), "making Start's panel took {panel_slice:?}");
        }
    }
}
