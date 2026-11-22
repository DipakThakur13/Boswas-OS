//! What the boot screen shows, and how little of it is redrawn.
//!
//! The picture is dark and almost empty. While the system starts: the mark,
//! a little above the middle, and a ring of five dots chasing each other
//! lower down. To unlock a disk: the mark, a title, the name of the disk and
//! a text box. While the system shuts down or restarts: the ring and one
//! line of text.
//!
//! It is kept as a short list of items in fixed slots. [`Scene::render`]
//! compares the new list with the one on screen and repaints only the
//! rectangles of items that changed, so an animation frame touches the
//! ring's two thousand pixels and nothing else, and allocates nothing.
//!
//! Sizes are in logical pixels, as in the Boswas UI token file; a logical
//! pixel is [`Layout::unit`] pixels on the display.

use std::fmt::Write as _;
use std::time::Duration;

use crate::assets::{self, TextStyle};
use crate::gfx::{bounding_rect, merge_rects, resample, Canvas, Image, Rect, Rgb};
use crate::text::Font;

/// Most dots the text box shows, so that a long passphrase does not reveal
/// its exact length to someone watching the screen.
pub const MAX_DOTS: usize = 24;

/// Largest size of a logical pixel (`build.rs` sizes the glyph atlases for it).
pub const MAX_UNIT: f32 = 3.2;

/// Dots in the ring.
pub const SPINNER_DOTS: usize = 5;
/// Seconds for one cycle of the ring: every dot goes round twice, then all
/// are gone for a moment.
pub const SPINNER_SECS: f32 = 4.2;
/// The phase shown when the ring does not move (`--static`): all five dots,
/// spread round the ring.
pub const REST_PHASE: f32 = 1.9 / SPINNER_SECS;

/// How long the caret of the text box is shown, and then hidden.
pub const CARET_BLINK: Duration = Duration::from_millis(530);
/// How long the text box shakes after a passphrase was rejected.
pub const SHAKE: Duration = Duration::from_millis(400);
/// How far it moves to either side, in logical pixels.
pub const SHAKE_REACH: f32 = 8.0;

/// Seconds a dot is visible, and seconds between one dot and the next.
const DOT_VISIBLE: f32 = 3.0;
const DOT_DELAY: f32 = 0.2;

/// Where one dot of the ring is `t` seconds after it appeared: its angle in
/// degrees, clockwise from 12 o'clock, and its opacity.
///
/// A dot enters at the bottom, runs up the left side and slows down, drifts
/// over the top, sweeps once round the ring, drifts over the top again and
/// leaves, speeding up, at the bottom. All dots follow the same path one
/// after the other, so they bunch up where it is slow and spread out where
/// it is fast. The slow drift never stops and every change of speed is
/// eased, so the speed has no jumps.
fn dot_at(t: f32) -> Option<(f32, f32)> {
    if !(0.0..DOT_VISIBLE).contains(&t) {
        return None;
    }
    let ease_out = |s: f32| 1.0 - (1.0 - s.clamp(0.0, 1.0)).powi(3);
    let ease_in = |s: f32| s.clamp(0.0, 1.0).powi(3);
    let ease_in_out = |s: f32| {
        let s = s.clamp(0.0, 1.0);
        s * s * s * (s * (6.0 * s - 15.0) + 10.0)
    };
    // 180 + 105 * 3 + 100 + 205 + 100 = 900: two turns, bottom to bottom.
    let angle = 180.0
        + 105.0 * t
        + 100.0 * ease_out(t / 0.5)
        + 205.0 * ease_in_out((t - 1.2) / 0.6)
        + 100.0 * ease_in((t - 2.5) / 0.5);
    let opacity = (t / 0.1).min((DOT_VISIBLE - t) / 0.15).min(1.0);
    Some((angle, opacity))
}

/// The dots of the ring at `phase` (0..1 of a cycle).
pub fn spinner_dots(phase: f32) -> [Option<(f32, f32)>; SPINNER_DOTS] {
    let now = phase.rem_euclid(1.0) * SPINNER_SECS;
    let mut dots = [None; SPINNER_DOTS];
    for (i, dot) in dots.iter_mut().enumerate() {
        *dot = dot_at((now - i as f32 * DOT_DELAY).rem_euclid(SPINNER_SECS));
    }
    dots
}

/// Whether the caret is in the visible half of its blink, `since` the last
/// key was pressed (or the prompt appeared).
pub fn caret_visible(since: Duration) -> bool {
    (since.as_millis() / CARET_BLINK.as_millis()) % 2 == 0
}

/// Horizontal offset of the text box in logical pixels, `since` a passphrase
/// was rejected: three quick swings to either side that die away.
pub fn shake_offset(since: Duration) -> f32 {
    let t = since.as_secs_f32() / SHAKE.as_secs_f32();
    if !(0.0..1.0).contains(&t) {
        return 0.0;
    }
    SHAKE_REACH * (1.0 - t).powi(2) * (t * 3.0 * std::f32::consts::TAU).sin()
}

/// Which screen is shown.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Boot,
    Shutdown,
    Reboot,
}

impl Mode {
    pub fn parse(name: &str) -> Option<Mode> {
        match name {
            "boot" | "boot-up" => Some(Mode::Boot),
            "shutdown" | "poweroff" | "halt" => Some(Mode::Shutdown),
            "reboot" => Some(Mode::Reboot),
            _ => None,
        }
    }

    fn status(self) -> Option<&'static str> {
        match self {
            Mode::Boot => None,
            Mode::Shutdown => Some("Shutting down"),
            Mode::Reboot => Some("Restarting"),
        }
    }
}

/// How a message line is coloured.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tone {
    Info,
    Error,
}

/// The visible part of a passphrase prompt. Never holds the passphrase.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PromptView<'a> {
    /// What the client asked, for example `Please unlock disk sda3_crypt`.
    pub text: &'a str,
    /// Number of characters typed so far.
    pub dots: usize,
    pub caps_lock: bool,
    /// Whether the caret is in the visible half of its blink.
    pub caret: bool,
    /// Horizontal offset of the text box in logical pixels (see [`shake_offset`]).
    pub shake: f32,
}

impl<'a> PromptView<'a> {
    /// An empty text box at rest.
    pub fn new(text: &'a str) -> Self {
        Self { text, dots: 0, caps_lock: false, caret: true, shake: 0.0 }
    }
}

/// Everything the renderer needs to know for one frame.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct View<'a> {
    pub mode: Mode,
    pub prompt: Option<PromptView<'a>>,
    pub message: Option<(&'a str, Tone)>,
    /// Progress in percent, when a client reported one.
    pub progress: Option<u8>,
    /// Where the ring is in its cycle (0..1).
    pub phase: f32,
}

impl View<'_> {
    pub fn new(mode: Mode) -> Self {
        Self { mode, prompt: None, message: None, progress: None, phase: REST_PHASE }
    }
}

/// The two lines above the text box and the hint below it.
struct PromptLines<'a> {
    title: &'static str,
    detail: &'a str,
    hint: &'static str,
}

/// Turn what a client asked into the lines of the unlock screen. The prompts
/// of the disk-unlock helpers name the disk after a fixed phrase; the title
/// says what to do and the name goes on a line of its own. Any other prompt
/// is shown as it came, under a neutral title.
fn prompt_lines(text: &str) -> PromptLines<'_> {
    let text = text.trim().trim_end_matches(':').trim_end();
    // cryptsetup-initramfs, systemd-cryptsetup, cryptsetup itself.
    for phrase in ["Please unlock disk ", "Please enter passphrase for disk ", "Enter passphrase for "] {
        let name = text.strip_prefix(phrase).map(str::trim).filter(|name| !name.is_empty());
        if let Some(detail) = name {
            return PromptLines { title: "Unlock your disk", detail, hint: "Press Enter to unlock" };
        }
    }
    PromptLines { title: "Enter your passphrase", detail: text, hint: "Press Enter to continue" }
}

fn contains_ignore_case(text: &str, phrase: &str) -> bool {
    text.as_bytes().windows(phrase.len()).any(|window| window.eq_ignore_ascii_case(phrase.as_bytes()))
}

/// The line shown for an error. What the disk-unlock scripts report after a
/// passphrase did not open the disk becomes one short sentence; any other
/// error is shown as it came.
fn error_line(text: &str) -> &str {
    let rejected = ["bad password", "bad passphrase", "incorrect pass", "wrong pass", "no key available"];
    if rejected.iter().any(|phrase| contains_ignore_case(text, phrase)) {
        "That passphrase did not work. Try again."
    } else {
        text
    }
}

/// One thing on screen. `S` is how a label holds its text: borrowed from the
/// view while a frame is composed, owned for what is on screen.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Kind<S> {
    Mark,
    Spinner {
        phase: f32,
    },
    /// One centred line of text; `progress` adds a percentage to it.
    Label {
        text: S,
        progress: Option<u8>,
        style: TextStyle,
        colour: Rgb,
    },
    /// The text box with its dots, moved sideways by `shift` pixels.
    Field {
        dots: usize,
        shift: i32,
    },
    /// The caret of the text box: an item of its own, so that its blink
    /// repaints its own few pixels and not the box.
    Caret,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct Item<S> {
    rect: Rect,
    kind: Kind<S>,
}

type Fresh<'a> = Option<Item<&'a str>>;
type Shown = Option<Item<String>>;

/// Is what is on screen what the new frame wants there?
fn same(shown: &Shown, fresh: &Fresh<'_>) -> bool {
    let (shown, fresh) = match (shown, fresh) {
        (None, None) => return true,
        (Some(shown), Some(fresh)) => (shown, fresh),
        _ => return false,
    };
    shown.rect == fresh.rect
        && match (&shown.kind, &fresh.kind) {
            (Kind::Mark, Kind::Mark) => true,
            (Kind::Spinner { phase: a }, Kind::Spinner { phase: b }) => a == b,
            (
                Kind::Label { text: a, progress: pa, style: sa, colour: ca },
                Kind::Label { text: b, progress: pb, style: sb, colour: cb },
            ) => a == b && pa == pb && sa == sb && ca == cb,
            (Kind::Field { dots: a, shift: sa }, Kind::Field { dots: b, shift: sb }) => a == b && sa == sb,
            (Kind::Caret, Kind::Caret) => true,
            _ => false,
        }
}

/// Remember `fresh` as what is on screen. A label that replaces a label
/// reuses its text buffer.
fn keep(shown: &mut Shown, fresh: &Fresh<'_>) {
    let Some(fresh) = fresh else {
        *shown = None;
        return;
    };
    let kind = match (&fresh.kind, shown.take().map(|item| item.kind)) {
        (&Kind::Label { text, progress, style, colour }, Some(Kind::Label { text: mut buffer, .. })) => {
            buffer.clear();
            buffer.push_str(text);
            Kind::Label { text: buffer, progress, style, colour }
        }
        (&Kind::Label { text, progress, style, colour }, _) => {
            Kind::Label { text: text.to_owned(), progress, style, colour }
        }
        (Kind::Mark, _) => Kind::Mark,
        (&Kind::Spinner { phase }, _) => Kind::Spinner { phase },
        (&Kind::Field { dots, shift }, _) => Kind::Field { dots, shift },
        (Kind::Caret, _) => Kind::Caret,
    };
    *shown = Some(Item { rect: fresh.rect, kind });
}

const SLOT_MARK: usize = 0;
const SLOT_SPINNER: usize = 1;
const SLOT_STATUS: usize = 2;
const SLOT_MESSAGE: usize = 3;
const SLOT_TITLE: usize = 4;
const SLOT_DETAIL: usize = 5;
const SLOT_FIELD: usize = 6;
/// After the field: the caret is drawn on top of the box.
const SLOT_CARET: usize = 7;
const SLOT_HINT: usize = 8;
const SLOTS: usize = 9;

/// Where everything goes on a display of a given size.
#[derive(Clone, Debug, PartialEq)]
pub struct Layout {
    pub width: i32,
    pub height: i32,
    /// Size of one logical pixel in display pixels.
    pub unit: f32,
    pub mark: Rect,
    /// The ring on the boot screen, and its message line.
    pub spinner: Rect,
    pub boot_message: Rect,
    /// The ring in the middle of the shutdown and restart screens, the
    /// status line under it and the message line under that.
    pub busy_spinner: Rect,
    pub status: Rect,
    pub busy_message: Rect,
    /// Radius of the circle the dots run on, and of a dot.
    pub spinner_radius: f32,
    pub spinner_dot: f32,
    /// The unlock screen: title, name of the disk, text box, and two lines
    /// under the box (hint and message; a message takes the upper one).
    pub title: Rect,
    pub detail: Rect,
    /// Everything the text box can cover, including its shake to either side.
    pub field: Rect,
    /// The text box at rest.
    pub text_box: Rect,
    pub prompt_line: Rect,
    pub prompt_second_line: Rect,
    /// Inside the text box: the padding left of the first dot, the distance
    /// from one dot to the next, the radius of a dot, the size of the caret.
    pub box_padding: i32,
    pub dot_pitch: i32,
    pub dot_radius: f32,
    pub caret: (i32, i32),
}

/// Size of a logical pixel for a display. It is one pixel up to 1080 lines
/// and grows with the height above that (2 at 2160 lines), so text and
/// controls have the sizes of the token file on ordinary displays and stay
/// in proportion on dense ones. Displays too small for the layout get a
/// smaller unit.
fn unit_for(w: f32, h: f32) -> f32 {
    (h / 1080.0).clamp(1.0, MAX_UNIT).min(h / 600.0).min(w / 480.0).max(0.5)
}

impl Layout {
    pub fn new(width: u32, height: u32) -> Self {
        let w = width.max(1) as f32;
        let h = height.max(1) as f32;
        let unit = unit_for(w, h);
        // Whole display pixels for a length in logical pixels.
        let u = |v: f32| (v * unit).round();
        let cx = (w / 2.0).floor();

        // The mark: 12 % of the shorter side, 160 logical pixels at most,
        // its centre a little above the middle of the screen.
        let mark_h = (0.12 * w.min(h)).min(160.0 * unit).min(assets::MARK_HEIGHT as f32).round().max(1.0);
        let mark_w = (mark_h * assets::MARK_WIDTH as f32 / assets::MARK_HEIGHT as f32).round().max(1.0);
        let mark_top = (0.45 * h - mark_h / 2.0).round().max(0.0);
        let mark = Rect::new((cx - mark_w / 2.0).round() as i32, mark_top as i32, mark_w as i32, mark_h as i32);

        let line_w = (w - u(48.0)).min(u(640.0)).max(1.0);
        let line = |top: f32, style: TextStyle| {
            Rect::new(
                (cx - line_w / 2.0).round() as i32,
                top.round() as i32,
                line_w as i32,
                u(style.line).max(1.0) as i32,
            )
        };

        // The ring: 40 logical pixels across, dots included.
        let spinner_dot = (2.25 * unit).max(1.5);
        let spinner_radius = 20.0 * unit - spinner_dot;
        let reach = spinner_radius + spinner_dot;
        let ring = |cy: f32| bounding_rect(cx - reach, cy - reach, reach * 2.0, reach * 2.0, 1.0);

        // Boot: the ring 28 % of the height below the middle, a message under it.
        let spinner = ring((0.78 * h).round());
        let boot_message = line(spinner.bottom() as f32 + u(12.0), assets::BODY);

        // Shutdown and restart: ring and status line, together in the middle.
        let gap = u(20.0);
        let busy_top = ((h - (reach * 2.0 + gap + u(assets::BODY_LARGE.line))) / 2.0).round().max(0.0);
        let busy_spinner = ring((busy_top + reach).round());
        let status = line(busy_top + reach * 2.0 + gap, assets::BODY_LARGE);
        let busy_message = line(status.bottom() as f32 + u(8.0), assets::BODY);

        // Unlock: a column under the mark, which stays where it is.
        let title = line(mark.bottom() as f32 + u(28.0), assets::SUBTITLE);
        let detail = line(title.bottom() as f32 + u(2.0), assets::BODY);
        let box_w = u(320.0).min(w - u(32.0)).max(1.0);
        let box_h = u(36.0).max(1.0);
        let text_box = Rect::new(
            (cx - box_w / 2.0).round() as i32,
            (detail.bottom() as f32 + u(20.0)) as i32,
            box_w as i32,
            box_h as i32,
        );
        let swing = (SHAKE_REACH * unit).ceil() as i32 + 1;
        let field = Rect::new(text_box.x - swing, text_box.y - 1, text_box.w + swing * 2, text_box.h + 2);
        let prompt_line = line(text_box.bottom() as f32 + u(8.0), assets::CAPTION);
        let prompt_second_line = line(prompt_line.bottom() as f32 + u(4.0), assets::CAPTION);
        // Dots from the left, 12 logical pixels in and 10 apart: all
        // MAX_DOTS of them fit, with the caret after the last.
        let box_padding = u(12.0).min(box_w / 4.0);
        let dot_pitch = u(10.0).min(((box_w - box_padding * 2.0) / MAX_DOTS as f32).floor()).max(1.0);
        let dot_radius = (3.0 * unit).min(dot_pitch * 0.4).max(0.5);
        let caret = (u(1.0).max(1.0) as i32, u(18.0).min(box_h - u(8.0)).max(1.0) as i32);

        Self {
            width: width as i32,
            height: height as i32,
            unit,
            mark,
            spinner,
            boot_message,
            busy_spinner,
            status,
            busy_message,
            spinner_radius,
            spinner_dot,
            title,
            detail,
            field,
            text_box,
            prompt_line,
            prompt_second_line,
            box_padding: box_padding as i32,
            dot_pitch: dot_pitch as i32,
            dot_radius,
            caret,
        }
    }

    /// Where the caret is when the text box shows `dots` dots and is moved
    /// sideways by `shift` pixels: right after the last dot.
    pub fn caret_rect(&self, dots: usize, shift: i32) -> Rect {
        let b = self.text_box;
        let left = b.x + shift + self.box_padding + self.dot_pitch * dots.min(MAX_DOTS) as i32;
        Rect::new(left, b.y + (b.h - self.caret.1) / 2, self.caret.0, self.caret.1)
    }

    /// The size in display pixels of text in `style`.
    pub fn text_px(&self, style: TextStyle) -> u32 {
        ((style.size * self.unit).round() as u32).max(6)
    }
}

/// The items of one frame, in their slots.
fn compose<'a>(l: &Layout, view: &View<'a>) -> [Fresh<'a>; SLOTS] {
    let mut items: [Fresh<'a>; SLOTS] = [None; SLOTS];
    let label = |rect: Rect, text: &'a str, style: TextStyle, colour: Rgb| {
        Some(Item { rect, kind: Kind::Label { text, progress: None, style, colour } })
    };
    let message = view.message.filter(|(text, _)| !text.trim().is_empty()).map(|(text, tone)| match tone {
        Tone::Info => (text, assets::TEXT_SECONDARY),
        Tone::Error => (error_line(text), assets::DANGER),
    });

    if let Some(prompt) = &view.prompt {
        let lines = prompt_lines(prompt.text);
        items[SLOT_MARK] = Some(Item { rect: l.mark, kind: Kind::Mark });
        items[SLOT_TITLE] = label(l.title, lines.title, assets::SUBTITLE, assets::TEXT);
        if !lines.detail.is_empty() {
            items[SLOT_DETAIL] = label(l.detail, lines.detail, assets::BODY, assets::TEXT_SECONDARY);
        }
        let shift = (prompt.shake.clamp(-SHAKE_REACH, SHAKE_REACH) * l.unit).round() as i32;
        let dots = prompt.dots.min(MAX_DOTS);
        items[SLOT_FIELD] = Some(Item { rect: l.field, kind: Kind::Field { dots, shift } });
        if prompt.caret {
            items[SLOT_CARET] = Some(Item { rect: l.caret_rect(dots, shift), kind: Kind::Caret });
        }
        let (hint, hint_colour) =
            if prompt.caps_lock { ("Caps Lock is on", assets::WARNING) } else { (lines.hint, assets::TEXT_MUTED) };
        // A message belongs to the box, so it goes right under it and the
        // hint moves down a line.
        let hint_line = match message {
            Some((text, colour)) => {
                items[SLOT_MESSAGE] = label(l.prompt_line, text, assets::CAPTION, colour);
                l.prompt_second_line
            }
            None => l.prompt_line,
        };
        items[SLOT_HINT] = label(hint_line, hint, assets::CAPTION, hint_colour);
        return items;
    }

    let (spinner, message_line) = match view.mode.status() {
        None => {
            items[SLOT_MARK] = Some(Item { rect: l.mark, kind: Kind::Mark });
            (l.spinner, l.boot_message)
        }
        Some(status) => {
            items[SLOT_STATUS] = label(l.status, status, assets::BODY_LARGE, assets::TEXT);
            (l.busy_spinner, l.busy_message)
        }
    };
    items[SLOT_SPINNER] = Some(Item { rect: spinner, kind: Kind::Spinner { phase: view.phase } });
    if message.is_some() || view.progress.is_some() {
        let (text, colour) = message.unwrap_or(("", assets::TEXT_SECONDARY));
        let kind = Kind::Label { text, progress: view.progress, style: assets::BODY, colour };
        items[SLOT_MESSAGE] = Some(Item { rect: message_line, kind });
    }
    items
}

/// The picture for one display, with the state needed to redraw it minimally.
pub struct Scene {
    layout: Layout,
    canvas: Canvas,
    mark: Image,
    font: Font,
    shown: [Shown; SLOTS],
    damage: Vec<Rect>,
    painted: bool,
}

impl Scene {
    pub fn new(width: u32, height: u32) -> Self {
        let layout = Layout::new(width, height);
        let (mw, mh) = (layout.mark.w.max(1) as usize, layout.mark.h.max(1) as usize);
        let mark = Image {
            width: mw as u32,
            height: mh as u32,
            data: resample(assets::MARK_RGBA, assets::MARK_WIDTH as usize, assets::MARK_HEIGHT as usize, 4, mw, mh),
        };
        Self {
            layout,
            canvas: Canvas::new(width, height),
            mark,
            font: Font::new(),
            shown: [const { None }; SLOTS],
            // Every slot can add two rectangles; with room for all of them
            // a frame never has to grow the list.
            damage: Vec::with_capacity(SLOTS * 2),
            painted: false,
        }
    }

    pub fn layout(&self) -> &Layout {
        &self.layout
    }

    pub fn canvas(&self) -> &Canvas {
        &self.canvas
    }

    /// The rectangles the last [`Scene::render`] changed.
    pub fn damage(&self) -> &[Rect] {
        &self.damage
    }

    /// Forget what is on screen: the next [`Scene::render`] repaints everything.
    pub fn invalidate(&mut self) {
        self.painted = false;
    }

    /// Bring the canvas up to date with `view` and return the rectangles that
    /// changed (empty when nothing did). The rectangles do not overlap.
    pub fn render(&mut self, view: &View) -> &[Rect] {
        let items = compose(&self.layout, view);
        let bounds = self.canvas.bounds();
        self.damage.clear();
        if !self.painted {
            self.damage.push(bounds);
        } else {
            for (shown, fresh) in self.shown.iter().zip(items.iter()) {
                if !same(shown, fresh) {
                    self.damage.extend(shown.as_ref().map(|item| item.rect.intersect(&bounds)));
                    self.damage.extend(fresh.as_ref().map(|item| item.rect.intersect(&bounds)));
                }
            }
        }
        merge_rects(&mut self.damage);
        for index in 0..self.damage.len() {
            let area = self.damage[index];
            self.canvas.fill_rect(&area, assets::BACKGROUND);
            for item in items.iter().flatten() {
                let clip = item.rect.intersect(&area);
                if !clip.is_empty() {
                    self.draw(item, &clip);
                }
            }
        }
        for (shown, fresh) in self.shown.iter_mut().zip(items.iter()) {
            if !same(shown, fresh) {
                keep(shown, fresh);
            }
        }
        self.painted = true;
        &self.damage
    }

    fn draw(&mut self, item: &Item<&str>, clip: &Rect) {
        match item.kind {
            Kind::Mark => self.canvas.blit_image(&self.mark, item.rect.x, item.rect.y, clip),
            Kind::Spinner { phase } => self.draw_spinner(&item.rect, phase, clip),
            Kind::Label { text, progress, style, colour } => {
                self.draw_label(&item.rect, text, progress, style, colour, clip)
            }
            Kind::Field { dots, shift } => self.draw_field(dots, shift, clip),
            Kind::Caret => self.canvas.fill_rect(clip, assets::TEXT),
        }
    }

    /// The ring: each visible dot as a small disc on the circle.
    fn draw_spinner(&mut self, rect: &Rect, phase: f32, clip: &Rect) {
        let cx = rect.x as f32 + rect.w as f32 / 2.0;
        let cy = rect.y as f32 + rect.h as f32 / 2.0;
        let (radius, dot) = (self.layout.spinner_radius, self.layout.spinner_dot);
        for (angle, opacity) in spinner_dots(phase).into_iter().flatten() {
            let (sin, cos) = angle.to_radians().sin_cos();
            self.canvas.fill_circle_faded(cx + radius * sin, cy - radius * cos, dot, assets::TEXT, opacity, clip);
        }
    }

    /// One line of text, centred in its rectangle and shortened to fit it.
    fn draw_label(
        &mut self,
        rect: &Rect,
        text: &str,
        progress: Option<u8>,
        style: TextStyle,
        colour: Rgb,
        clip: &Rect,
    ) {
        let px = self.layout.text_px(style);
        let mut line = clean(text);
        if let Some(percent) = progress {
            let percent = percent.min(100);
            let _ = if line.is_empty() { write!(line, "{percent}%") } else { write!(line, " ({percent}%)") };
        }
        let line = Font::fit(&line, style.weight, px, rect.w as f32);
        let x = rect.x as f32 + (rect.w as f32 - Font::measure(&line, style.weight, px)) / 2.0;
        // The typeface's own line, centred in the taller line of the style.
        let baseline = rect.y as f32
            + (rect.h as f32 - Font::line_height(style.weight, px)) / 2.0
            + Font::ascent(style.weight, px);
        self.font.draw(&mut self.canvas, clip, x, baseline, &line, style.weight, px, colour);
    }

    /// The text box: a subtle fill with a one-pixel outline, rounded corners
    /// and the accent line of a focused control along its bottom edge. One
    /// dot per typed character (at most [`MAX_DOTS`]) from the left.
    /// `shift` moves all of it sideways.
    fn draw_field(&mut self, dots: usize, shift: i32, clip: &Rect) {
        let unit = self.layout.unit;
        let u = |v: f32| (v * unit).round();
        let b = self.layout.text_box;
        let (x, y, w, h) = ((b.x + shift) as f32, b.y as f32, b.w as f32, b.h as f32);
        let radius = assets::RADIUS_CONTROL * unit;
        let stroke = u(1.0).max(1.0);
        let accent = u(2.0).max(2.0).min(h / 2.0);

        // Whole-pixel widths on whole-pixel edges keep every line crisp. The
        // outline and the accent line are one rounded shape in two colours,
        // split at a pixel row; the fill is a second shape inside it.
        let split = b.bottom() - accent as i32;
        let above = Rect::new(clip.x, clip.y, clip.w, split - clip.y).intersect(clip);
        let below = Rect::new(clip.x, split, clip.w, clip.bottom() - split).intersect(clip);
        self.canvas.fill_round_rect(x, y, w, h, radius, assets::FIELD_STROKE, &above);
        self.canvas.fill_round_rect(x, y, w, h, radius, assets::ACCENT, &below);
        self.canvas.fill_round_rect(
            x + stroke,
            y + stroke,
            w - stroke * 2.0,
            h - stroke - accent,
            (radius - stroke).max(0.0),
            assets::FIELD_FILL,
            clip,
        );

        let (pad, pitch, dot) = (self.layout.box_padding as f32, self.layout.dot_pitch as f32, self.layout.dot_radius);
        for i in 0..dots.min(MAX_DOTS) {
            self.canvas.fill_circle(x + pad + pitch * i as f32 + dot, y + h / 2.0, dot, assets::TEXT, clip);
        }
    }
}

/// Make a client-supplied string safe to lay out: one line, no control
/// characters, bounded length.
fn clean(text: &str) -> String {
    text.chars().map(|c| if c.is_control() { ' ' } else { c }).take(240).collect::<String>().trim().to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    const DISPLAYS: [(u32, u32); 11] = [
        (640, 480),
        (800, 600),
        (1024, 768),
        (1280, 720),
        (1366, 768),
        (1920, 1080),
        (2560, 1440),
        (2560, 1600),
        (3840, 2160),
        (5120, 2880),
        (1080, 1920),
    ];

    const UNLOCK: &str = "Please unlock disk nvme0n1p3_crypt";
    const REJECTED: &str = "cryptsetup: ERROR: nvme0n1p3_crypt: cryptsetup failed, bad password or options?";
    const SHORT_ERROR: &str = "That passphrase did not work. Try again.";

    fn area_of(rects: &[Rect]) -> i64 {
        rects.iter().map(Rect::area).sum()
    }

    /// Pixels of exactly `colour`: for solid shapes.
    fn count(canvas: &Canvas, rect: &Rect, colour: Rgb) -> usize {
        let mut n = 0;
        for y in rect.y..rect.bottom() {
            for x in rect.x..rect.right() {
                n += usize::from(canvas.pixel(x as u32, y as u32) == colour);
            }
        }
        n
    }

    /// Pixels that are not background: for text, whose thin strokes rarely
    /// cover a whole pixel.
    fn ink(canvas: &Canvas, rect: &Rect) -> usize {
        rect.area() as usize - count(canvas, rect, assets::BACKGROUND)
    }

    fn blank(canvas: &Canvas, rect: &Rect) -> bool {
        ink(canvas, rect) == 0
    }

    /// Pixels drawn in `colour` over the background at a third of full
    /// coverage or more: every channel is the same mix of the two. Tells a
    /// hue (danger, warning, accent) from white or grey text.
    fn tinted(canvas: &Canvas, rect: &Rect, colour: Rgb) -> usize {
        let bg = assets::BACKGROUND;
        let mut n = 0;
        for y in rect.y..rect.bottom() {
            for x in rect.x..rect.right() {
                let p = canvas.pixel(x as u32, y as u32);
                let share = |p: u8, bg: u8, c: u8| (p as f32 - bg as f32) / (c as f32 - bg as f32);
                let shares = [share(p.r, bg.r, colour.r), share(p.g, bg.g, colour.g), share(p.b, bg.b, colour.b)];
                let (low, high) =
                    (shares.iter().copied().fold(1.0, f32::min), shares.iter().copied().fold(0.0, f32::max));
                n += usize::from(low >= 0.33 && high <= 1.02 && high - low <= 0.06);
            }
        }
        n
    }

    /// The pixels that differ between two frames, as a bounding rectangle.
    fn changed(before: &[u32], after: &[u32], width: u32) -> Rect {
        let mut area = Rect::default();
        for (i, (a, b)) in before.iter().zip(after).enumerate() {
            if a != b {
                area = area.union(&Rect::new((i as u32 % width) as i32, (i as u32 / width) as i32, 1, 1));
            }
        }
        area
    }

    fn prompt(text: &str) -> View<'_> {
        View { prompt: Some(PromptView::new(text)), ..View::new(Mode::Boot) }
    }

    /// The lines of text of a frame: slot, rectangle, text, style, colour.
    fn labels<'a>(l: &Layout, view: &View<'a>) -> Vec<(usize, Rect, &'a str, TextStyle, Rgb)> {
        compose(l, view)
            .iter()
            .enumerate()
            .filter_map(|(slot, item)| match item.as_ref()?.kind {
                Kind::Label { text, style, colour, .. } => Some((slot, item.as_ref()?.rect, text, style, colour)),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn the_unit_is_one_pixel_on_ordinary_displays_and_grows_on_dense_ones() {
        for ((w, h), unit) in [
            ((1024, 768), 1.0),
            ((1366, 768), 1.0),
            ((1920, 1080), 1.0),
            ((2560, 1600), 1600.0 / 1080.0),
            ((3840, 2160), 2.0),
            ((7680, 4320), MAX_UNIT),
            ((640, 480), 0.8),
        ] {
            assert!((Layout::new(w, h).unit - unit).abs() < 1e-4, "{w}x{h}: {}", Layout::new(w, h).unit);
        }
    }

    #[test]
    fn layout_fits_common_displays() {
        for (w, h) in DISPLAYS {
            let l = Layout::new(w, h);
            let screen = Rect::new(0, 0, w as i32, h as i32);
            let boot = [("mark", l.mark), ("ring", l.spinner), ("boot message", l.boot_message)];
            let busy = [("ring", l.busy_spinner), ("status", l.status), ("message", l.busy_message)];
            let unlock = [
                ("mark", l.mark),
                ("title", l.title),
                ("detail", l.detail),
                ("field", l.field),
                ("line", l.prompt_line),
                ("second line", l.prompt_second_line),
            ];
            // Everything is on the display, in order from the top, and items
            // that are visible together do not overlap.
            for screenful in [&boot[..], &busy[..], &unlock[..]] {
                for (name, r) in screenful {
                    assert!(screen.contains(r), "{name} {r:?} leaves a {w}x{h} display");
                }
                for pair in screenful.windows(2) {
                    assert!(pair[1].1.y >= pair[0].1.bottom(), "{} overlaps {} at {w}x{h}", pair[1].0, pair[0].0);
                }
            }
            assert!(l.field.contains(&l.text_box));
            // Centred on the same axis, to the pixel.
            let axis = w as i32 / 2;
            for r in [l.mark, l.spinner, l.busy_spinner, l.text_box, l.title, l.status] {
                assert!((r.x * 2 + r.w - axis * 2).abs() <= 1, "{r:?} is off the axis of a {w}x{h} display");
            }
            // The mark: 12 % of the shorter side, its centre above the middle.
            let side = w.min(h) as f32;
            assert!((l.mark.h as f32 - 0.12 * side).abs() <= 1.0, "{:?} at {w}x{h}", l.mark);
            assert!(l.mark.h as f32 <= 160.0 * l.unit + 0.5 && l.mark.h <= assets::MARK_HEIGHT as i32);
            let mark_centre = l.mark.y as f32 + l.mark.h as f32 / 2.0;
            assert!(mark_centre < h as f32 / 2.0 && mark_centre > h as f32 * 0.4);
            // The ring: 40 logical pixels, 28 % of the height below the middle.
            assert!((l.spinner.w as f32 - 40.0 * l.unit).abs() <= 4.0, "{:?}", l.spinner);
            let ring_centre = l.spinner.y as f32 + l.spinner.h as f32 / 2.0;
            assert!((ring_centre - 0.78 * h as f32).abs() <= 1.0);
            // The busy ring and its status line share the middle of the screen.
            let busy_centre = (l.busy_spinner.y + l.status.bottom()) as f32 / 2.0;
            assert!((busy_centre - h as f32 / 2.0).abs() <= 2.0);
        }
        // Whatever the display, the mark is never larger than its artwork.
        assert!(Layout::new(7680, 4320).mark.h <= assets::MARK_HEIGHT as i32);
    }

    #[test]
    fn the_text_box_has_the_metrics_of_a_fluent_control() {
        let l = Layout::new(1920, 1080);
        assert_eq!((l.text_box.w, l.text_box.h), (320, 36));
        assert_eq!(l.caret, (1, 18));
        let l = Layout::new(3840, 2160);
        assert_eq!((l.text_box.w, l.text_box.h), (640, 72));
        assert_eq!(l.caret, (2, 36));
        assert_eq!(Layout::new(1366, 768).text_px(assets::BODY), 14);
        assert_eq!(Layout::new(3840, 2160).text_px(assets::SUBTITLE), 40);
    }

    #[test]
    fn the_lines_follow_the_type_ramp_and_the_dark_theme() {
        let l = Layout::new(1920, 1080);
        assert!(labels(&l, &View::new(Mode::Boot)).is_empty(), "the boot screen has no text");
        let mut view = View::new(Mode::Boot);
        view.message = Some(("Checking disks", Tone::Info));
        assert_eq!(
            labels(&l, &view),
            [(SLOT_MESSAGE, l.boot_message, "Checking disks", assets::BODY, assets::TEXT_SECONDARY)]
        );
        for (mode, status) in [(Mode::Shutdown, "Shutting down"), (Mode::Reboot, "Restarting")] {
            assert_eq!(
                labels(&l, &View::new(mode)),
                [(SLOT_STATUS, l.status, status, assets::BODY_LARGE, assets::TEXT)]
            );
        }
        let mut view = prompt(UNLOCK);
        assert_eq!(
            labels(&l, &view),
            [
                (SLOT_TITLE, l.title, "Unlock your disk", assets::SUBTITLE, assets::TEXT),
                (SLOT_DETAIL, l.detail, "nvme0n1p3_crypt", assets::BODY, assets::TEXT_SECONDARY),
                (SLOT_HINT, l.prompt_line, "Press Enter to unlock", assets::CAPTION, assets::TEXT_MUTED),
            ]
        );
        view.prompt = Some(PromptView { caps_lock: true, ..PromptView::new(UNLOCK) });
        assert_eq!(
            labels(&l, &view)[2],
            (SLOT_HINT, l.prompt_line, "Caps Lock is on", assets::CAPTION, assets::WARNING)
        );
        // An error takes the line under the box; the hint moves down one.
        view.message = Some((REJECTED, Tone::Error));
        assert_eq!(
            labels(&l, &view)[..1],
            [(SLOT_MESSAGE, l.prompt_line, SHORT_ERROR, assets::CAPTION, assets::DANGER)]
        );
        assert_eq!(
            labels(&l, &view)[3],
            (SLOT_HINT, l.prompt_second_line, "Caps Lock is on", assets::CAPTION, assets::WARNING)
        );
        view.message = Some(("Second volume", Tone::Info));
        assert_eq!(
            labels(&l, &view)[0],
            (SLOT_MESSAGE, l.prompt_line, "Second volume", assets::CAPTION, assets::TEXT_SECONDARY)
        );
    }

    #[test]
    fn tiny_and_odd_sizes_do_not_panic() {
        for (w, h) in [(1, 1), (7, 3), (320, 200), (200, 3000), (5000, 100)] {
            let mut scene = Scene::new(w, h);
            scene.render(&View::new(Mode::Boot));
            scene.render(&View::new(Mode::Reboot));
            let mut view = prompt("x");
            view.prompt = Some(PromptView { dots: 3, caps_lock: true, shake: -8.0, ..PromptView::new("x") });
            view.message = Some(("m", Tone::Error));
            scene.render(&view);
        }
    }

    #[test]
    fn first_frame_paints_everything_then_nothing() {
        let mut scene = Scene::new(1280, 720);
        let view = View::new(Mode::Boot);
        assert_eq!(scene.render(&view), [Rect::new(0, 0, 1280, 720)]);
        assert_eq!(scene.damage(), [Rect::new(0, 0, 1280, 720)]);
        assert_eq!(scene.canvas().pixel(2, 2), assets::BACKGROUND);
        assert!(scene.render(&view).is_empty(), "an unchanged view repaints nothing");
        assert!(scene.damage().is_empty());
        scene.invalidate();
        assert_eq!(scene.render(&view).len(), 1);
    }

    #[test]
    fn the_boot_screen_is_the_mark_and_the_ring_and_nothing_else() {
        let mut scene = Scene::new(1920, 1080);
        scene.render(&View::new(Mode::Boot));
        let l = scene.layout().clone();
        let canvas = scene.canvas();
        assert!(ink(canvas, &l.mark) > (l.mark.area() / 6) as usize, "the mark covers a good part of its box");
        assert!(l.mark.h >= 120 && l.mark.h <= 160, "a modest mark: {:?}", l.mark);
        assert!(count(canvas, &l.spinner, assets::TEXT) > SPINNER_DOTS * 6, "the dots of the ring are drawn");
        // The mark keeps its own colours: the blue of its stem and bowl, and
        // the warm dot in the counter (measured: 5,230 and 251 pixels).
        let (mut blue, mut warm) = (0, 0);
        for y in l.mark.y..l.mark.bottom() {
            for x in l.mark.x..l.mark.right() {
                let p = canvas.pixel(x as u32, y as u32);
                if p.b > 180 && p.b > p.r + 60 {
                    blue += 1;
                }
                if p.r > 180 && p.g > 140 && p.b < 170 && p.r > p.b + 40 {
                    warm += 1;
                }
            }
        }
        assert!(blue > 3000, "blue pixels in the mark: {blue}");
        assert!(warm > 150, "warm pixels in the dot of the mark: {warm}");
        // Everything else is background: no wordmark, no message, no status.
        assert_eq!(ink(canvas, &canvas.bounds()), ink(canvas, &l.mark) + ink(canvas, &l.spinner));
    }

    #[test]
    fn every_dot_goes_round_twice_and_the_dots_follow_each_other() {
        // A dot starts and ends at the bottom, two turns apart, always
        // moving clockwise and never in a jump.
        let (start, _) = dot_at(0.0).unwrap();
        let (end, _) = dot_at(DOT_VISIBLE - 1e-4).unwrap();
        assert!((start - 180.0).abs() < 0.01 && (end - 900.0).abs() < 0.5, "{start} to {end}");
        let (mut last, mut fastest, mut slowest) = (start, 0f32, f32::MAX);
        for step in 1..3000 {
            let (angle, opacity) = dot_at(step as f32 * 0.001).unwrap();
            assert!((0.0..=1.0).contains(&opacity));
            let speed = (angle - last) / 0.001;
            assert!(speed > 0.0, "a dot never goes backwards");
            fastest = fastest.max(speed);
            slowest = slowest.min(speed);
            last = angle;
        }
        // Ease in and out: several times faster in the sweep than over the top...
        assert!(fastest > slowest * 5.0, "{slowest} to {fastest} degrees a second");
        // ...and even on a display paced to 30 frames a second no dot moves
        // more than a ninth of a turn in a frame (at 60 or more, far less).
        assert!(fastest / 30.0 < 40.0, "{fastest}");
        assert!(dot_at(-0.1).is_none() && dot_at(DOT_VISIBLE).is_none());
        // It fades in and out.
        assert!(dot_at(0.01).unwrap().1 < 0.2 && dot_at(1.0).unwrap().1 == 1.0);
        assert!(dot_at(DOT_VISIBLE - 0.01).unwrap().1 < 0.2);

        // The cycle: one dot at the start, all five in the middle, none at the end.
        let visible = |phase: f32| spinner_dots(phase).iter().flatten().count();
        assert_eq!(visible(0.0), 1);
        assert_eq!(visible(REST_PHASE), SPINNER_DOTS);
        assert_eq!(visible(0.99), 0);
        assert_eq!(spinner_dots(1.25), spinner_dots(0.25));
        // Over the top the dots bunch up, without touching: the slow drift
        // keeps them more than a dot apart...
        let l = Layout::new(1920, 1080);
        let apart = |a: f32, b: f32| (a - b).abs().to_radians() * l.spinner_radius;
        let bunched: Vec<f32> = spinner_dots(1.15 / SPINNER_SECS).iter().flatten().map(|dot| dot.0).collect();
        assert_eq!(bunched.len(), SPINNER_DOTS);
        for pair in bunched.windows(2) {
            let gap = apart(pair[0], pair[1]);
            assert!(gap > l.spinner_dot * 2.0 + 1.0 && gap < l.spinner_dot * 4.0, "{gap} pixels: {bunched:?}");
        }
        // ...and in the sweep they spread out round the ring.
        let spread: Vec<f32> = spinner_dots(1.6 / SPINNER_SECS).iter().flatten().map(|dot| dot.0).collect();
        assert!(apart(spread[0], spread[1]) > l.spinner_dot * 8.0, "{spread:?}");
        // At rest all five are well apart.
        let rest: Vec<f32> = spinner_dots(REST_PHASE).iter().flatten().map(|dot| dot.0).collect();
        assert!(rest.windows(2).all(|pair| apart(pair[0], pair[1]) > l.spinner_dot * 2.0 + 1.0), "{rest:?}");
    }

    #[test]
    fn an_animation_frame_only_damages_the_ring() {
        for mode in [Mode::Boot, Mode::Shutdown] {
            let mut scene = Scene::new(1920, 1080);
            let mut view = View::new(mode);
            view.message = Some(("Starting the system", Tone::Info));
            scene.render(&view);
            let ring = if mode == Mode::Boot { scene.layout().spinner } else { scene.layout().busy_spinner };
            assert!(ring.area() <= 44 * 44, "the ring is small: {ring:?}");
            let mut moved = 0;
            for step in 1..=130 {
                let before: Vec<u32> = scene.canvas().pixels().to_vec();
                view.phase = step as f32 / 126.0; // a slow pace (30 a second), past the end of a cycle
                let damage = scene.render(&view).to_vec();
                assert_eq!(damage, [ring], "frame {step}");
                // No pixel outside the damage changed.
                let area = changed(&before, scene.canvas().pixels(), 1920);
                assert!(ring.contains(&area), "frame {step} changed {area:?}");
                moved += usize::from(!area.is_empty());
            }
            assert!(moved > 100, "the dots move in nearly every frame: {moved}");
        }
    }

    #[test]
    fn frames_leave_nothing_behind() {
        // A frame reached through any number of others is, pixel for pixel,
        // the frame painted from nothing: no trace of earlier dots, carets,
        // lines of text or shaken boxes.
        let mut scene = Scene::new(1366, 768);
        let mut view = View::new(Mode::Boot);
        for step in 0..90 {
            view.phase = step as f32 / 90.0;
            scene.render(&view);
        }
        let mut walk = prompt(UNLOCK);
        for step in 0..40u64 {
            let shake = shake_offset(Duration::from_millis(step * 10));
            let typing =
                PromptView { dots: step as usize % 30, caret: step % 3 != 0, shake, ..PromptView::new(UNLOCK) };
            walk.prompt = Some(typing);
            walk.message = (step % 7 < 3).then_some((REJECTED, Tone::Error));
            scene.render(&walk);
        }
        let stopping = View { message: Some(("Stopping services", Tone::Info)), ..View::new(Mode::Shutdown) };
        for view in [
            View { phase: 0.4, ..View::new(Mode::Boot) },
            prompt(UNLOCK),
            stopping,
            View { progress: Some(7), ..View::new(Mode::Boot) },
            View::new(Mode::Reboot),
            View::new(Mode::Boot),
        ] {
            scene.render(&view);
            let mut fresh = Scene::new(1366, 768);
            fresh.render(&view);
            assert!(scene.canvas().pixels() == fresh.canvas().pixels(), "{view:?} differs from a fresh frame");
        }
    }

    #[test]
    fn the_unlock_screen_has_a_title_the_disk_name_a_text_box_and_a_hint() {
        let mut scene = Scene::new(1366, 768);
        scene.render(&View::new(Mode::Boot));
        let l = scene.layout().clone();
        let damage = scene.render(&prompt(UNLOCK)).to_vec();
        for d in &damage {
            assert!(!d.intersects(&l.mark), "the mark stays and is not repainted: {d:?}");
        }
        assert!(area_of(&damage) < (1366 * 768) / 8);
        let canvas = scene.canvas();
        assert!(ink(canvas, &l.title) > 300, "the title");
        assert!(ink(canvas, &l.detail) > 150, "the name of the disk");
        assert!(ink(canvas, &l.prompt_line) > 150, "the hint");
        assert!(blank(canvas, &l.prompt_second_line));
        assert!(blank(canvas, &l.spinner), "the ring is gone while prompting");
        // The box: 320 by 36, a subtle fill, an outline, and the accent line
        // of a focused control along the bottom two rows only.
        let b = l.text_box;
        assert_eq!((b.w, b.h), (320, 36));
        assert!(count(canvas, &b, assets::FIELD_FILL) > 320 * 30, "the fill");
        assert!(count(canvas, &b, assets::FIELD_STROKE) > 300 + 2 * 20, "the outline");
        let accent_rows = Rect::new(b.x, b.bottom() - 2, b.w, 2);
        assert!(count(canvas, &accent_rows, assets::ACCENT) > 2 * 300, "the accent line");
        assert_eq!(count(canvas, &b, assets::ACCENT), count(canvas, &accent_rows, assets::ACCENT));
        assert_eq!(ink(canvas, &l.field), ink(canvas, &b), "nothing is drawn round the box");
        // The corners are rounded by 4 pixels: a rectangle, not a pill.
        assert_eq!(canvas.pixel(b.x as u32, b.y as u32), assets::BACKGROUND);
        assert_eq!(canvas.pixel(b.x as u32 + 4, b.y as u32), assets::FIELD_STROKE);
        assert_eq!(canvas.pixel(b.x as u32, b.y as u32 + 5), assets::FIELD_STROKE);
        assert_eq!(canvas.pixel(b.x as u32 + 1, b.y as u32 + 5), assets::FIELD_FILL);
        assert_eq!(canvas.pixel(b.right() as u32 - 1, b.y as u32 + 18), assets::FIELD_STROKE);
        // An empty box shows the caret at the left padding.
        let caret = Rect::new(b.x + 12, b.y + 9, 1, 18);
        assert_eq!(count(canvas, &caret, assets::TEXT), 18);
        assert_eq!(count(canvas, &b, assets::TEXT), 18, "and nothing else in the text colour");

        // Back to the ring.
        scene.render(&View::new(Mode::Boot));
        assert!(count(scene.canvas(), &l.spinner, assets::TEXT) > SPINNER_DOTS * 6);
        assert!(blank(scene.canvas(), &l.field) && blank(scene.canvas(), &l.title));
    }

    #[test]
    fn prompts_are_turned_into_a_title_and_a_name() {
        let other = "Password for the backup volume";
        for (text, title, detail, hint) in [
            ("Please unlock disk sda3_crypt", "Unlock your disk", "sda3_crypt", "Press Enter to unlock"),
            ("Please unlock disk sda3_crypt: ", "Unlock your disk", "sda3_crypt", "Press Enter to unlock"),
            (
                "Please enter passphrase for disk Samsung SSD 990 (luks-1a2b):",
                "Unlock your disk",
                "Samsung SSD 990 (luks-1a2b)",
                "Press Enter to unlock",
            ),
            ("Enter passphrase for /dev/sda3: ", "Unlock your disk", "/dev/sda3", "Press Enter to unlock"),
            (other, "Enter your passphrase", other, "Press Enter to continue"),
            ("Please unlock disk ", "Enter your passphrase", "Please unlock disk", "Press Enter to continue"),
            ("", "Enter your passphrase", "", "Press Enter to continue"),
        ] {
            let lines = prompt_lines(text);
            assert_eq!((lines.title, lines.detail, lines.hint), (title, detail, hint), "{text:?}");
        }
        // A prompt without text has no second line.
        let mut scene = Scene::new(1024, 768);
        scene.render(&prompt(""));
        let l = scene.layout().clone();
        assert!(blank(scene.canvas(), &l.detail));
        assert!(ink(scene.canvas(), &l.title) > 300);
    }

    #[test]
    fn typing_only_damages_the_field_and_shows_dots_not_characters() {
        let mut scene = Scene::new(1920, 1080);
        let mut view = prompt(UNLOCK);
        scene.render(&view);
        let (field, b) = (scene.layout().field, scene.layout().text_box);
        let empty = count(scene.canvas(), &field, assets::TEXT);
        let mut typed = PromptView { dots: 5, ..PromptView::new(UNLOCK) };
        view.prompt = Some(typed);
        assert_eq!(scene.render(&view), [field]);
        assert!(count(scene.canvas(), &field, assets::TEXT) > empty + 5 * 12, "five dots appear");
        // From the left, 12 pixels in, on the middle line of the box: five
        // identical dots 10 pixels apart, then the caret.
        let canvas = scene.canvas();
        let (left, row) = (b.x as u32 + 12, b.y as u32 + 18);
        for i in 0..5 {
            assert_eq!(canvas.pixel(left + 10 * i + 3, row), assets::TEXT, "dot {i}");
            assert_eq!(canvas.pixel(left + 10 * i + 8, row), assets::FIELD_FILL, "gap after dot {i}");
            let dot = Rect::new((left + 10 * i) as i32, b.y + 1, 10, b.h - 4);
            assert_eq!(
                count(canvas, &dot, assets::TEXT),
                count(canvas, &Rect::new(left as i32, b.y + 1, 10, b.h - 4), assets::TEXT)
            );
        }
        assert_eq!(canvas.pixel(left + 50, row), assets::TEXT, "the caret follows the dots");
        assert_eq!(canvas.pixel(left + 53, row), assets::FIELD_FILL);
        // The dot count is capped, and the dots stay inside the box.
        typed.dots = MAX_DOTS;
        view.prompt = Some(typed);
        scene.render(&view);
        let capped = scene.canvas().pixels().to_vec();
        let right = Rect::new(b.right() - 12, b.y, 12, b.h);
        assert_eq!(count(scene.canvas(), &right, assets::TEXT), 0, "the right padding stays clear");
        typed.dots = MAX_DOTS + 40;
        view.prompt = Some(typed);
        assert!(scene.render(&view).is_empty(), "more characters than the cap change nothing on screen");
        assert_eq!(capped, scene.canvas().pixels());
    }

    #[test]
    fn the_caret_blinks_inside_the_field() {
        assert!(caret_visible(Duration::ZERO) && caret_visible(Duration::from_millis(529)));
        assert!(!caret_visible(Duration::from_millis(530)) && !caret_visible(Duration::from_millis(1059)));
        assert!(caret_visible(Duration::from_millis(1060)));

        let mut scene = Scene::new(1920, 1080);
        let mut view = prompt(UNLOCK);
        let mut typed = PromptView { dots: 3, ..PromptView::new(UNLOCK) };
        view.prompt = Some(typed);
        scene.render(&view);
        let l = scene.layout().clone();
        let caret = l.caret_rect(3, 0);
        assert_eq!((caret.x, caret.y, caret.w, caret.h), (l.text_box.x + 12 + 30, l.text_box.y + 9, 1, 18));
        let on = scene.canvas().pixels().to_vec();
        // A blink repaints the caret's own 18 pixels and nothing else.
        typed.caret = false;
        view.prompt = Some(typed);
        assert_eq!(scene.render(&view), [caret]);
        assert_eq!(changed(&on, scene.canvas().pixels(), 1920), caret);
        assert_eq!(count(scene.canvas(), &caret, assets::FIELD_FILL), 18, "the box shows through where it was");
        typed.caret = true;
        view.prompt = Some(typed);
        assert_eq!(scene.render(&view), [caret]);
        assert!(scene.canvas().pixels() == on);
        // The caret follows the dots and stops with them at the cap.
        assert_eq!(l.caret_rect(MAX_DOTS + 5, 0), l.caret_rect(MAX_DOTS, 0));
        assert!(l.caret_rect(MAX_DOTS, 0).right() <= l.text_box.right() - 12);
        assert_eq!(l.caret_rect(2, -4).x, l.caret_rect(2, 0).x - 4);
    }

    #[test]
    fn a_rejected_passphrase_shows_a_short_error_and_shakes_the_box() {
        assert_eq!(error_line(REJECTED), SHORT_ERROR);
        assert_eq!(error_line("Incorrect passphrase"), SHORT_ERROR);
        assert_eq!(error_line("No key available with this passphrase."), SHORT_ERROR);
        let other = "cryptsetup: ERROR: sda3_crypt: maximum number of tries exceeded";
        assert_eq!(error_line(other), other);
        assert_eq!(error_line(""), "");

        // The swing starts and ends at rest, goes both ways and dies away.
        assert_eq!(shake_offset(Duration::ZERO), 0.0);
        assert_eq!(shake_offset(SHAKE), 0.0);
        assert_eq!(shake_offset(SHAKE * 3), 0.0);
        let swing: Vec<f32> = (0..40).map(|ms| shake_offset(Duration::from_millis(ms * 10))).collect();
        let far_right = swing.iter().copied().fold(0.0, f32::max);
        let far_left = swing.iter().copied().fold(0.0, f32::min);
        assert!((5.0..=SHAKE_REACH).contains(&far_right), "{far_right}");
        assert!((-SHAKE_REACH..-4.0).contains(&far_left), "{far_left}");
        assert!(swing[30..].iter().all(|x| x.abs() < 1.0), "{swing:?}");

        let mut scene = Scene::new(1366, 768);
        let mut view = prompt(UNLOCK);
        scene.render(&view);
        let l = scene.layout().clone();

        // The error takes the line under the box; the hint moves down.
        view.message = Some((REJECTED, Tone::Error));
        scene.render(&view);
        assert!(tinted(scene.canvas(), &l.prompt_line, assets::DANGER) > 150, "the error line");
        assert!(ink(scene.canvas(), &l.prompt_second_line) > 150, "the hint, one line down");
        assert_eq!(tinted(scene.canvas(), &l.prompt_second_line, assets::DANGER), 0);
        if assets::TYPEFACE == "Inter" {
            // Short: no wider than the box above it.
            let wide = Rect::new(l.prompt_line.x, l.prompt_line.y, l.text_box.x - l.prompt_line.x, l.prompt_line.h);
            assert!(blank(scene.canvas(), &wide));
        }

        // The box moves sideways, as a whole, inside the field's rectangle.
        let with_error = scene.canvas().pixels().to_vec();
        let fill = count(scene.canvas(), &l.field, assets::FIELD_FILL);
        let mut shaken = PromptView { shake: 6.0, ..PromptView::new(UNLOCK) };
        view.prompt = Some(shaken);
        assert_eq!(scene.render(&view), [l.field]);
        let (b, row) = (l.text_box, l.text_box.y as u32 + 18);
        let canvas = scene.canvas();
        assert_eq!(canvas.pixel(b.x as u32 + 3, row), assets::BACKGROUND, "the left edge moved right");
        assert_eq!(canvas.pixel(b.x as u32 + 6, row), assets::FIELD_STROKE);
        assert_eq!(canvas.pixel(b.right() as u32 + 5, row), assets::FIELD_STROKE);
        assert_eq!(canvas.pixel(b.x as u32 + 12 + 6, row), assets::TEXT, "the caret moved with it");
        assert_eq!(count(canvas, &l.field, assets::FIELD_FILL), fill);
        // To the left as far as it goes, and never outside its rectangle.
        shaken.shake = -100.0;
        view.prompt = Some(shaken);
        scene.render(&view);
        assert_eq!(scene.canvas().pixel(b.x as u32 - 8, row), assets::FIELD_STROKE);
        assert_eq!(scene.canvas().pixel(b.x as u32 - 9, row), assets::BACKGROUND);
        // Back at rest the picture is what it was.
        shaken.shake = 0.0;
        view.prompt = Some(shaken);
        scene.render(&view);
        assert!(scene.canvas().pixels() == with_error);
    }

    #[test]
    fn caps_lock_replaces_the_hint_with_a_warning() {
        let mut scene = Scene::new(1366, 768);
        let mut view = prompt(UNLOCK);
        scene.render(&view);
        let l = scene.layout().clone();
        assert_eq!(tinted(scene.canvas(), &l.prompt_line, assets::WARNING), 0);
        view.prompt = Some(PromptView { caps_lock: true, ..PromptView::new(UNLOCK) });
        let damage = scene.render(&view).to_vec();
        assert_eq!(damage, [l.prompt_line], "only the hint line is repainted");
        assert!(tinted(scene.canvas(), &l.prompt_line, assets::WARNING) > 100, "the Caps Lock warning");
        // With an error as well, both are shown: the error first.
        view.message = Some((REJECTED, Tone::Error));
        scene.render(&view);
        assert!(tinted(scene.canvas(), &l.prompt_line, assets::DANGER) > 150);
        assert_eq!(tinted(scene.canvas(), &l.prompt_line, assets::WARNING), 0);
        assert!(tinted(scene.canvas(), &l.prompt_second_line, assets::WARNING) > 100);
    }

    #[test]
    fn shutdown_and_restart_show_the_ring_and_a_line_but_no_mark() {
        let mut inks = Vec::new();
        for mode in [Mode::Shutdown, Mode::Reboot] {
            let mut scene = Scene::new(1920, 1080);
            scene.render(&View::new(mode));
            let l = scene.layout().clone();
            let canvas = scene.canvas();
            assert!(ink(canvas, &l.status) > 300, "{mode:?}");
            assert!(count(canvas, &l.busy_spinner, assets::TEXT) > SPINNER_DOTS * 6, "{mode:?}");
            // Nothing else is drawn: no mark (the ring is where it would be,
            // and holds five small dots and no more), no second ring.
            let drawn = ink(canvas, &l.status) + ink(canvas, &l.busy_spinner);
            assert_eq!(ink(canvas, &canvas.bounds()), drawn, "{mode:?}");
            assert!(ink(canvas, &l.busy_spinner) < SPINNER_DOTS * 40, "{mode:?}");
            assert!(blank(canvas, &l.spinner), "{mode:?}");
            inks.push(ink(canvas, &l.status));
        }
        assert_ne!(inks[0], inks[1], "the two screens say different things");
        // Which items a screen is made of: the boot screen has the mark and
        // no status line, and a prompt brings the mark back in any mode.
        let l = Layout::new(1920, 1080);
        let slots = |view: &View| {
            let items = compose(&l, view);
            (0..SLOTS).filter(|&slot| items[slot].is_some()).collect::<Vec<usize>>()
        };
        assert_eq!(slots(&View::new(Mode::Boot)), [SLOT_MARK, SLOT_SPINNER]);
        assert_eq!(slots(&View::new(Mode::Shutdown)), [SLOT_SPINNER, SLOT_STATUS]);
        assert_eq!(slots(&View::new(Mode::Reboot)), [SLOT_SPINNER, SLOT_STATUS]);
        let asking = View { prompt: Some(PromptView::new(UNLOCK)), ..View::new(Mode::Shutdown) };
        assert_eq!(slots(&asking), [SLOT_MARK, SLOT_TITLE, SLOT_DETAIL, SLOT_FIELD, SLOT_CARET, SLOT_HINT]);
    }

    #[test]
    fn messages_and_progress_are_one_line_under_the_ring() {
        let mut scene = Scene::new(1920, 1080);
        let mut view = View::new(Mode::Boot);
        scene.render(&view);
        let l = scene.layout().clone();
        assert!(blank(scene.canvas(), &l.boot_message), "no line until the system sends one");
        view.message = Some(("cryptsetup: nvme0n1p3_crypt: set up successfully", Tone::Info));
        assert_eq!(scene.render(&view), [l.boot_message]);
        let plain = ink(scene.canvas(), &l.boot_message);
        assert!(plain > 400, "the message is drawn: {plain}");
        assert_eq!(tinted(scene.canvas(), &l.boot_message, assets::DANGER), 0);
        view.message = Some(("cryptsetup: ERROR: sda3_crypt: maximum number of tries exceeded", Tone::Error));
        scene.render(&view);
        assert!(tinted(scene.canvas(), &l.boot_message, assets::DANGER) > 250, "an error is in the danger colour");
        // Progress is a percentage: on its own, or after the message.
        view.message = None;
        view.progress = Some(40);
        assert_eq!(scene.render(&view), [l.boot_message]);
        let alone = ink(scene.canvas(), &l.boot_message);
        assert!(alone > 30 && alone < plain / 4, "{alone}");
        view.message = Some(("cryptsetup: nvme0n1p3_crypt: set up successfully", Tone::Info));
        scene.render(&view);
        assert!(ink(scene.canvas(), &l.boot_message) > plain + alone, "the message and the percentage");
        // A message of spaces is no message.
        view.progress = None;
        view.message = Some(("   ", Tone::Info));
        scene.render(&view);
        assert!(blank(scene.canvas(), &l.boot_message));
        // On the shutdown screen the line is under the status line.
        let mut view = View::new(Mode::Shutdown);
        view.message = Some(("Stopping services", Tone::Info));
        scene.render(&view);
        assert!(ink(scene.canvas(), &l.busy_message) > 150);
    }

    #[test]
    fn text_is_centred_on_the_axis_of_the_screen() {
        for (w, h) in [(1366u32, 768u32), (1920, 1080), (3840, 2160)] {
            let mut scene = Scene::new(w, h);
            let mut view = View::new(Mode::Shutdown);
            view.message = Some(("Stopping services", Tone::Info));
            scene.render(&view);
            let l = scene.layout().clone();
            for line in [l.status, l.busy_message] {
                let mut drawn = Rect::default();
                for y in line.y..line.bottom() {
                    for x in line.x..line.right() {
                        if scene.canvas().pixel(x as u32, y as u32) != assets::BACKGROUND {
                            drawn = drawn.union(&Rect::new(x, y, 1, 1));
                        }
                    }
                }
                let centre = drawn.x as f32 + drawn.w as f32 / 2.0;
                assert!((centre - w as f32 / 2.0).abs() <= 2.0 * l.unit, "{w}x{h}: text centred at {centre}");
                assert!(drawn.y > line.y && drawn.bottom() < line.bottom(), "{w}x{h}: {drawn:?} inside {line:?}");
            }
        }
    }

    #[test]
    fn hostile_text_is_contained() {
        let mut scene = Scene::new(1024, 768);
        let nasty = "\u{1b}[2J\n\r\t\u{0}".repeat(500) + &"W".repeat(5000);
        let mut view = View::new(Mode::Boot);
        view.message = Some((&nasty, Tone::Info));
        assert!(!scene.render(&view).is_empty());
        view.prompt = Some(PromptView { dots: usize::MAX, shake: f32::NAN, ..PromptView::new(&nasty) });
        view.progress = Some(255);
        assert!(!scene.render(&view).is_empty());
        view.message = Some((&nasty, Tone::Error));
        scene.render(&view);
        // Long text is cut to its line, never drawn beyond it.
        let l = scene.layout().clone();
        assert!(ink(scene.canvas(), &l.detail) > 1000);
        for line in [l.detail, l.prompt_line] {
            let left = Rect::new(0, line.y, line.x, line.h);
            let right = Rect::new(line.right(), line.y, 1024 - line.right(), line.h);
            assert!(blank(scene.canvas(), &left) && blank(scene.canvas(), &right));
        }
        assert_eq!(clean("a\nb\tc"), "a b c");
        assert!(clean(&"x".repeat(1000)).len() <= 240);
    }

    #[test]
    fn mode_names() {
        assert_eq!(Mode::parse("boot"), Some(Mode::Boot));
        assert_eq!(Mode::parse("shutdown"), Some(Mode::Shutdown));
        assert_eq!(Mode::parse("reboot"), Some(Mode::Reboot));
        assert_eq!(Mode::parse("updates"), None);
    }
}
