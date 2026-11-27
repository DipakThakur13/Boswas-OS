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
//!   instead, one per turn of the event loop while nothing else happens,
//!   so that input is handled between any two: a corner that plans the
//!   panel and the flyout's own icons (made on a worker thread: tens of
//!   milliseconds at 200 %), the same corner again once they are kept,
//!   then bands of a row of tiles.

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
    ui::{Frame, MadePanel, Plan, Rect},
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
    /// Numbers the works (a worker thread's answer goes to its own).
    next_work: u64,
    /// When a flyout, menu or dialog was last open, and whether what was
    /// kept for them has been let go since (see `release_when_idle`).
    used_at: Option<Instant>,
    released: bool,
}

/// How long the shell keeps the canvases and buffers of closed flyouts
/// for the next one after the last was open.
pub const KEEP_SPARES: Duration = Duration::from_secs(180);
/// Shadow pictures larger than this are let go once the panels they were
/// made for are kept (a flyout's shadow at 200 % is over ten megabytes).
const LARGE_SHADOW: usize = 1024 * 1024;

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
        self.release_when_idle(Instant::now());
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
            self.prewarm.next_work += 1;
            let id = self.prewarm.next_work;
            self.prewarm.work = Some(Work { id, key, scale, steps, canvas: None, waiting: 0, busy: Duration::ZERO, longest: Duration::ZERO, count: 0 });
            return;
        }
        if self.prewarm.work.as_ref().is_some_and(|work| work.waiting > 0) {
            // A panel is being made on a worker thread.
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
            if step.plan {
                // The panel (its shadow, its fill from the wallpaper and its
                // edges: tens of milliseconds at 200 %) and the icons not
                // looked up yet (one to two milliseconds each) are made on
                // a worker thread; the next step finds them kept.
                self.res.planned = Some(Plan::default());
                draw_slice(&mut self.res, &self.st, &step, &mut canvas);
                let plan = self.res.planned.take().unwrap_or_default();
                if !(plan.panels.is_empty() && plan.icons.is_empty()) && self.make_planned(work.id, plan) {
                    work.waiting += 1;
                }
            } else {
                draw_slice(&mut self.res, &self.st, &step, &mut canvas);
            }
            work.canvas = Some(canvas);
        }
        let took = started.elapsed();
        tracing::trace!(target: "boswas_shell::stats", "prepared {:?} {:?}{} in {} µs", step.kind, step.part, if step.plan { " (planned)" } else { "" }, took.as_micros());
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
            // The panels are made: their large shadows are not needed.
            boswas_ui::canvas::trim_shadows(LARGE_SHADOW);
        } else {
            self.prewarm.work = Some(work);
        }
    }

    /// A few minutes after the last flyout, menu or dialog closed, lets go
    /// of the canvases and buffers kept for the next one (up to 64 MB at
    /// 200 %) and of large shadow pictures; the icons, shaped text and
    /// panels stay.
    fn release_when_idle(&mut self, now: Instant) {
        if self.st.flyout.is_some() || !self.st.menus.is_empty() || self.st.dialog.is_some() || self.st.capture.is_some() {
            self.prewarm.used_at = Some(now);
            self.prewarm.released = false;
            return;
        }
        let idle = self.prewarm.used_at.is_some_and(|at| now.duration_since(at) >= KEEP_SPARES);
        if idle && !self.prewarm.released {
            self.prewarm.released = true;
            let freed = crate::present::release_spares() + boswas_ui::canvas::trim_shadows(LARGE_SHADOW);
            tracing::debug!(target: "boswas_shell::stats", "let go of {} MB kept for flyouts", freed / (1024 * 1024));
        }
    }

    /// Makes what a drawing planned on a worker thread and keeps it as it
    /// comes; the work `id` waits until all is. False when no thread could
    /// be started (the drawing then makes it on the event loop).
    fn make_planned(&mut self, id: u64, plan: Plan) -> bool {
        let theme = crate::ui::icon_theme(self.res.theme.scheme);
        let (sender, receiver) = channel::channel::<Made>();
        let inserted = self.loop_handle.insert_source(receiver, move |event, _, shell| match event {
            // Icons for a work that has been replaced (the theme changed)
            // are not kept.
            channel::Event::Msg(Made::Icon(name, size, pixmap)) => {
                if shell.prewarm.work.as_ref().is_some_and(|w| w.id == id) {
                    shell.res.icons.keep(&name, size, pixmap);
                }
            }
            channel::Event::Msg(Made::Panel(made)) => shell.res.keep_panel(made),
            channel::Event::Closed => {
                if let Some(work) = shell.prewarm.work.as_mut().filter(|w| w.id == id) {
                    work.waiting = work.waiting.saturating_sub(1);
                }
            }
        });
        if inserted.is_err() {
            return false;
        }
        std::thread::Builder::new()
            .name("panels".into())
            .spawn(move || {
                let loader = IconLoader::new(theme);
                make(plan, &loader, |made| sender.send(made).is_ok());
            })
            .is_ok()
    }

    /// Nothing is being drawn or moving and no flyout is open.
    fn prewarm_quiet(&self) -> bool {
        self.st.flyout.is_none() && self.surfaces.iter().all(|s| !s.dirty && s.anim.is_none())
    }

    /// When the event loop is to come back for the next slice of the
    /// preparation (`None`: no slice is waiting).
    pub fn prewarm_deadline(&self, now: Instant) -> Option<Instant> {
        self.prewarm.work.as_ref().filter(|w| !w.steps.is_empty() && w.waiting == 0 && self.prewarm_quiet()).map(|_| now)
    }

    /// Rasterises, on a worker thread, the icons of every application that
    /// are not kept yet, at the sizes they are drawn at.
    fn rasterise_icons(&mut self, scale: f32) {
        let wanted: Vec<(String, u32)> = icons_wanted(&self.st.apps, scale).into_iter().filter(|(name, size)| !self.res.icons.has(name, *size)).collect();
        if wanted.is_empty() {
            self.prewarm.icons_done = true;
            return;
        }
        let theme = crate::ui::icon_theme(self.res.theme.scheme);
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

/// What the worker thread of a planned drawing hands over.
pub enum Made {
    Icon(String, u32, Option<Pixmap>),
    Panel(MadePanel),
}

/// Makes what a drawing planned: the icons with `loader` (the theme's),
/// then the panels, each handed to `send` as it is made (false: stop).
fn make(plan: Plan, loader: &IconLoader, mut send: impl FnMut(Made) -> bool) {
    for (name, size) in plan.icons {
        let pixmap = loader.render(&name, size);
        if !send(Made::Icon(name, size, pixmap)) {
            return;
        }
    }
    for job in plan.panels {
        if let Some(made) = job.make() {
            if !send(Made::Panel(made)) {
                return;
            }
        }
    }
}

/// Height (logical pixels) of the band of a flyout one slice draws: about
/// a row of Start's tiles.
const BAND: f32 = 96.0;

/// The preparation still to do.
pub struct Work {
    id: u64,
    key: Key,
    scale: f32,
    steps: VecDeque<Step>,
    /// The canvas nobody sees, of the flyout being drawn.
    canvas: Option<Canvas>,
    /// Worker threads making panels that the next step needs.
    waiting: usize,
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
    /// Its panel is planned and made off the event loop, not drawn.
    plan: bool,
}

/// The slices of a flyout of `size`: a corner of its panel, first to plan
/// the panel (made on a worker thread), then to find it kept; then its
/// bands from the top.
fn slices(kind: Flyout, size: (f32, f32), origin: (f32, f32)) -> Vec<Step> {
    let step = |part: Rect| Step { kind, size, origin, part, plan: false };
    let corner = Rect::new(PAD, PAD, 1.0, 1.0);
    let mut steps = vec![Step { plan: true, ..step(corner) }, step(corner)];
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
    /// in slices, over the blurred wallpaper (glass): its panel is planned
    /// and made on another thread, then the slices draw the bands. Once they
    /// have run, drawing Start whole makes no panel, makes no fill and
    /// shapes no more text (it is as quick as a later opening), and no
    /// slice on the event loop takes long, so a click at that moment is not
    /// held up. The limits hold for an optimised build on a loaded machine:
    /// `cargo test --release slices -- --nocapture` prints the figures.
    #[test]
    fn the_preparation_runs_in_short_slices_and_prepares_everything() {
        let st = crate::state::State::for_tests();
        let scale = 2.0;
        let mut res = crate::ui::Resources::new(boswas_ui::Theme::of(Scheme::Dark));
        let icons = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../../desktop/icons");
        res.icons = IconLoader::with_base_dirs("Boswas-Prism", &[icons.clone()]);
        // The icons, as the worker thread hands them over.
        let loader = IconLoader::with_base_dirs("Boswas-Prism", &[icons.clone()]);
        for (name, size) in icons_wanted(&st.apps, scale) {
            res.icons.keep(&name, size, loader.render(&name, size));
        }
        // The wallpaper of a 1920 x 1080 output, blurred for glass.
        let wallpaper = crate::wallpaper::Background::default().reduced(res.theme, 1920.0, 1080.0).unwrap();
        res.backdrop = boswas_ui::material::Backdrop::new(&wallpaper, 1920.0, 1080.0);
        let size = (832.0 + 2.0 * PAD, 918.0 + 2.0 * PAD);
        let origin = (544.0, 102.0);
        let steps = slices(Flyout::Start, size, origin);
        assert!(steps.len() > 5 && steps[0].plan && !steps[1].plan);
        let mut canvas = Canvas::new(size.0, size.1, scale).unwrap();
        let mut each = Vec::new();
        let mut aside = Duration::ZERO;
        let looked_up = res.icons.looked_up();
        for step in &steps {
            let started = Instant::now();
            if step.plan {
                res.planned = Some(Plan::default());
                draw_slice(&mut res, &st, step, &mut canvas);
                let plan = res.planned.take().unwrap();
                each.push(started.elapsed());
                assert_eq!(plan.panels.len(), 1, "Start's panel is planned");
                assert_eq!(res.panel_count(), 0, "and not made yet");
                assert!(plan.icons.iter().any(|(name, _)| name == "system-shutdown-symbolic"), "so are Start's own icons: {:?}", plan.icons);
                let made = Instant::now();
                let dirs = vec![icons.clone()];
                let handle = std::thread::spawn(move || {
                    let mut all = Vec::new();
                    make(plan, &IconLoader::with_base_dirs("Boswas-Prism", &dirs), |made| {
                        all.push(made);
                        true
                    });
                    all
                });
                for made in handle.join().unwrap() {
                    match made {
                        Made::Icon(name, size, pixmap) => res.icons.keep(&name, size, pixmap),
                        Made::Panel(panel) => res.keep_panel(panel),
                    }
                }
                aside += made.elapsed();
                continue;
            }
            draw_slice(&mut res, &st, step, &mut canvas);
            each.push(started.elapsed());
        }
        assert_eq!(res.icons.looked_up(), looked_up, "no icon was looked up on the event loop");
        let all: Duration = each.iter().sum();
        let longest = each.iter().copied().max().unwrap_or_default();
        let panels = res.panel_count();
        let shaped = res.text.shaped_count();
        let fills = res.backdrop.as_ref().unwrap().renders();
        let started = Instant::now();
        let mut whole = Canvas::new(size.0, size.1, scale).unwrap();
        let panel = Rect::new(PAD, PAD, 832.0, 918.0);
        start::draw(&mut Frame { canvas: &mut whole, res: &mut res, pointer: None, pressed: false, hits: &mut Vec::new(), focus: None, origin }, &st, panel);
        let opening = started.elapsed();
        assert_eq!(res.panel_count(), panels, "the slices made Start's panel");
        assert_eq!(res.icons.looked_up(), looked_up, "and looked up its icons");
        assert_eq!(res.backdrop.as_ref().unwrap().renders(), fills, "and its fill");
        assert_eq!(res.text.shaped_count(), shaped, "the slices shaped every label of Start");
        // The panel made on another thread is the panel made directly.
        let mut direct = crate::ui::Resources::new(boswas_ui::Theme::of(Scheme::Dark));
        direct.icons = std::mem::replace(&mut res.icons, IconLoader::new("Boswas-Prism"));
        direct.backdrop = boswas_ui::material::Backdrop::new(&wallpaper, 1920.0, 1080.0);
        let mut reference = Canvas::new(size.0, size.1, scale).unwrap();
        start::draw(&mut Frame { canvas: &mut reference, res: &mut direct, pointer: None, pressed: false, hits: &mut Vec::new(), focus: None, origin }, &st, panel);
        assert!(whole.data() == reference.data(), "the prepared Start is the Start drawn directly");
        println!(
            "Start at 200 % prepared in {} slices: {all:?} on the event loop, the longest {longest:?} (each: {each:?}), and {aside:?} aside; then opening it draws in {opening:?}",
            each.len()
        );
        #[cfg(not(debug_assertions))]
        assert!(longest < Duration::from_millis(8), "a slice of the preparation took {longest:?}");
    }
}
