//! What the boot screen shows, and how little of it is redrawn.
//!
//! The picture: a dark background with a soft bloom of the brand's blues
//! behind the mark ([`crate::backdrop`]), the mark a little above the middle
//! ([`crate::mark`]) and, close under it, a progress ring drawn in the
//! Fluent manner: an arc with round ends that lengthens and shortens while
//! it turns, or shows a reported percentage. A message from the boot scripts
//! is one line under the ring. To unlock a disk the mark glides up and the
//! prompt slides in: a title, the name of the disk and a text box. While the
//! system shuts down or restarts: the mark, the ring and one line of text.
//! How everything moves is [`crate::motion`]'s; a [`View`] is one moment.
//!
//! The picture is kept as a short list of items in fixed slots.
//! [`Scene::render`] compares the new list with the one on screen and
//! repaints only the rectangles of items that changed: a frame of the ring
//! touches the ring's few thousand pixels and nothing else, and allocates
//! nothing.
//!
//! Sizes are in logical pixels, as in the Boswas UI token file; a logical
//! pixel is [`Layout::unit`] pixels on the display.

use std::fmt::Write as _;
use std::time::Duration;

use crate::assets::{self, TextStyle};
use crate::backdrop::Backdrop;
use crate::gfx::{bounding_rect, merge_rects, round_rect_distance, Canvas, Image, Rect, Rgb};
use crate::mark::{MarkPainter, Pose};
use crate::motion::{Assembly, Motion};
use crate::text::Font;

/// Most dots the text box shows, so that a long passphrase does not reveal
/// its exact length to someone watching the screen.
pub const MAX_DOTS: usize = 24;

/// Largest size of a logical pixel (`build.rs` flattens curves for it).
pub const MAX_UNIT: f32 = 3.2;

/// Height of the mark's ink as a share of the display's shorter side, and
/// at most, in logical pixels.
pub const MARK_SHARE: f32 = 0.16;
pub const MARK_MAX: f32 = 200.0;
/// The middle of the mark, as a share of the height from the top.
pub const MARK_AT: f32 = 0.44;
/// The gap between the mark and the ring, in heights of the mark.
pub const RING_GAP: f32 = 0.3;
/// Size of the mark on the unlock screen, relative to the boot screen.
pub const PROMPT_MARK: f32 = 0.75;
/// The ring: its size across and the width of its stroke, in logical pixels.
pub const RING_SIZE: f32 = 34.0;
pub const RING_STROKE: f32 = 3.5;
/// The shortest arc of the ring and how much it grows, in degrees.
const ARC_MIN: f32 = 14.0;
const ARC_GROWTH: f32 = 256.0;
/// Opacity of the track under the arc when the ring shows a percentage.
const TRACK: f32 = 0.16;
/// How far the prompt slides up while it fades in, in logical pixels.
pub const SLIDE: f32 = 12.0;

/// The phase shown when the ring does not move (`--static`): a three-quarter arc.
pub const REST_PHASE: f32 = 0.4;

/// How long the caret of the text box is shown, and then hidden.
pub const CARET_BLINK: Duration = Duration::from_millis(530);
/// How long the text box shakes after a passphrase was rejected.
pub const SHAKE: Duration = Duration::from_millis(400);
/// How far it moves to either side, in logical pixels.
pub const SHAKE_REACH: f32 = 8.0;

/// The arc of the ring at `phase` (0..1 of a cycle): where it starts and
/// how long it is, in degrees clockwise from 12 o'clock.
///
/// In each cycle the arc's head runs ahead while its tail holds back, from
/// the shortest arc to three quarters of the ring, then the tail catches up;
/// all the while the whole ring turns steadily. Both runs are eased, and in
/// a cycle the arc goes round exactly once, so cycles join without a seam.
pub fn ring_arc(phase: f32) -> (f32, f32) {
    let p = phase.rem_euclid(1.0);
    let ease = |s: f32| {
        let s = s.clamp(0.0, 1.0);
        s * s * (3.0 - 2.0 * s)
    };
    let (head, tail) =
        if p < 0.5 { (ARC_GROWTH * ease(p * 2.0), 0.0) } else { (ARC_GROWTH, ARC_GROWTH * ease(p * 2.0 - 1.0)) };
    let turn = (360.0 - ARC_GROWTH) * p;
    ((turn + tail).rem_euclid(360.0), ARC_MIN + head - tail)
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
    /// The prompt, while one is open or fading out.
    pub prompt: Option<PromptView<'a>>,
    pub message: Option<(&'a str, Tone)>,
    /// Progress in percent, when a client reported one.
    pub progress: Option<u8>,
    /// Where the ring is in its cycle (0..1).
    pub phase: f32,
    /// Where everything that moves is.
    pub motion: Motion,
}

impl<'a> View<'a> {
    /// The screen of `mode` at rest.
    pub fn new(mode: Mode) -> Self {
        Self { mode, prompt: None, message: None, progress: None, phase: REST_PHASE, motion: Motion::REST }
    }

    /// The same with the unlock screen shown, at rest.
    pub fn with_prompt(mut self, prompt: PromptView<'a>) -> Self {
        self.prompt = Some(prompt);
        self.motion.reveal = 1.0;
        self
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

/// An opacity as it is compared between frames: in 256 steps, so that a
/// change too small to show repaints nothing.
fn level(opacity: f32) -> u8 {
    crate::gfx::alpha(opacity) as u8
}

/// One thing on screen. `S` is how a label holds its text: borrowed from the
/// view while a frame is composed, owned for what is on screen. Opacities
/// are in 256 steps.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Kind<S> {
    /// The warm light round the dot of the mark in `pose`, under the mark.
    Glow {
        pose: Pose,
        level: u8,
    },
    Mark {
        pose: Pose,
        assembly: Assembly,
        alpha: u8,
    },
    /// The ring: its arc (start and length in degrees), the opacity of its
    /// track, and its own.
    Ring {
        start: f32,
        sweep: f32,
        track: u8,
        alpha: u8,
    },
    /// One centred line of text; `progress` adds a percentage to it.
    Label {
        text: S,
        progress: Option<u8>,
        style: TextStyle,
        colour: Rgb,
        alpha: u8,
    },
    /// The text box with its dots, moved sideways by `shift` and down by
    /// `slide` pixels.
    Field {
        dots: usize,
        shift: i32,
        slide: i32,
        alpha: u8,
    },
    /// The caret of the text box: an item of its own, so that its blink
    /// repaints its own few pixels and not the box.
    Caret {
        alpha: u8,
    },
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
            (
                Kind::Label { text: a, progress: pa, style: sa, colour: ca, alpha: aa },
                Kind::Label { text: b, progress: pb, style: sb, colour: cb, alpha: ab },
            ) => a == b && pa == pb && sa == sb && ca == cb && aa == ab,
            (Kind::Glow { pose: a, level: la }, Kind::Glow { pose: b, level: lb }) => a == b && la == lb,
            (Kind::Mark { pose: a, assembly: sa, alpha: aa }, Kind::Mark { pose: b, assembly: sb, alpha: ab }) => {
                a == b && sa == sb && aa == ab
            }
            (
                Kind::Ring { start: a, sweep: wa, track: ta, alpha: aa },
                Kind::Ring { start: b, sweep: wb, track: tb, alpha: ab },
            ) => a == b && wa == wb && ta == tb && aa == ab,
            (
                Kind::Field { dots: a, shift: sa, slide: la, alpha: aa },
                Kind::Field { dots: b, shift: sb, slide: lb, alpha: ab },
            ) => a == b && sa == sb && la == lb && aa == ab,
            (Kind::Caret { alpha: a }, Kind::Caret { alpha: b }) => a == b,
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
        (&Kind::Label { text, progress, style, colour, alpha }, Some(Kind::Label { text: mut buffer, .. })) => {
            buffer.clear();
            buffer.push_str(text);
            Kind::Label { text: buffer, progress, style, colour, alpha }
        }
        (&Kind::Label { text, progress, style, colour, alpha }, _) => {
            Kind::Label { text: text.to_owned(), progress, style, colour, alpha }
        }
        (&Kind::Glow { pose, level }, _) => Kind::Glow { pose, level },
        (&Kind::Mark { pose, assembly, alpha }, _) => Kind::Mark { pose, assembly, alpha },
        (&Kind::Ring { start, sweep, track, alpha }, _) => Kind::Ring { start, sweep, track, alpha },
        (&Kind::Field { dots, shift, slide, alpha }, _) => Kind::Field { dots, shift, slide, alpha },
        (&Kind::Caret { alpha }, _) => Kind::Caret { alpha },
    };
    *shown = Some(Item { rect: fresh.rect, kind });
}

const SLOT_GLOW: usize = 0;
const SLOT_MARK: usize = 1;
const SLOT_RING: usize = 2;
const SLOT_STATUS: usize = 3;
const SLOT_MESSAGE: usize = 4;
const SLOT_TITLE: usize = 5;
const SLOT_DETAIL: usize = 6;
const SLOT_FIELD: usize = 7;
/// After the field: the caret is drawn on top of the box.
const SLOT_CARET: usize = 8;
const SLOT_HINT: usize = 9;
const SLOTS: usize = 10;

/// Where everything goes on a display of a given size.
#[derive(Clone, Debug, PartialEq)]
pub struct Layout {
    pub width: i32,
    pub height: i32,
    /// Size of one logical pixel in display pixels.
    pub unit: f32,
    /// The mark on the boot, shutdown and restart screens, and on the
    /// unlock screen, and the pixels it covers in each.
    pub mark_pose: Pose,
    pub prompt_pose: Pose,
    pub mark: Rect,
    pub prompt_mark: Rect,
    /// The ring under the mark; the radius of the middle of its stroke and
    /// the stroke's width.
    pub ring: Rect,
    pub ring_radius: f32,
    pub ring_stroke: f32,
    /// The message line under the ring on the boot screen.
    pub boot_message: Rect,
    /// The status line under the ring on the shutdown and restart screens,
    /// and the message line under that.
    pub status: Rect,
    pub busy_message: Rect,
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
    /// How far the prompt slides while it fades in, in pixels.
    pub slide: i32,
}

/// Size of a logical pixel for a display. It is one pixel up to 1080 lines
/// on the shorter side and grows with it above that (2 at 3840x2160), so
/// text and controls have the sizes of the token file on ordinary displays
/// and stay in proportion on dense ones, turned or not. Displays too small
/// for the layout get a smaller unit.
fn unit_for(w: f32, h: f32) -> f32 {
    (w.min(h) / 1080.0).clamp(1.0, MAX_UNIT).min(h / 600.0).min(w / 480.0).max(0.5)
}

impl Layout {
    pub fn new(width: u32, height: u32) -> Self {
        let w = width.max(1) as f32;
        let h = height.max(1) as f32;
        let unit = unit_for(w, h);
        // Whole display pixels for a length in logical pixels.
        let u = |v: f32| (v * unit).round();
        let cx = (w / 2.0).floor();

        // The mark: a share of the shorter side, its middle a little above
        // the middle of the screen.
        let mark_h = (MARK_SHARE * w.min(h)).min(MARK_MAX * unit).max(4.0);
        let mark_cy = (MARK_AT * h).round();
        let mark_pose = Pose::centred(cx, mark_cy, mark_h);

        let line_w = (w - u(48.0)).min(u(640.0)).max(1.0);
        let line = |top: f32, style: TextStyle| {
            Rect::new(
                (cx - line_w / 2.0).round() as i32,
                top.round() as i32,
                line_w as i32,
                u(style.line).max(1.0) as i32,
            )
        };

        // The ring, close under the mark.
        let ring_stroke = (RING_STROKE * unit).max(1.0);
        let outer = (u(RING_SIZE) / 2.0).max(2.0);
        let ring_radius = outer - ring_stroke / 2.0;
        let ring_cy = (mark_cy + mark_h / 2.0 + RING_GAP * mark_h + outer).round();
        let ring = bounding_rect(cx - outer, ring_cy - outer, outer * 2.0, outer * 2.0, 1.0);
        let boot_message = line(ring.bottom() as f32 + u(14.0), assets::BODY);
        let status = line(ring.bottom() as f32 + u(14.0), assets::BODY_LARGE);
        let busy_message = line(status.bottom() as f32 + u(6.0), assets::BODY);

        // Unlock: the mark, smaller, at the head of a column that is centred
        // a little above the middle of the screen.
        let prompt_h = mark_h * PROMPT_MARK;
        let column = prompt_h
            + u(28.0)
            + u(assets::SUBTITLE.line)
            + u(2.0)
            + u(assets::BODY.line)
            + u(20.0)
            + u(36.0)
            + u(8.0)
            + u(assets::CAPTION.line) * 2.0
            + u(4.0);
        let top = (0.47 * h - column / 2.0).round().max(0.0);
        let prompt_pose = Pose::centred(cx, top + prompt_h / 2.0, prompt_h);
        let title = line(top + prompt_h + u(28.0), assets::SUBTITLE);
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
            mark_pose,
            prompt_pose,
            mark: mark_pose.rect(),
            prompt_mark: prompt_pose.rect(),
            ring,
            ring_radius,
            ring_stroke,
            boot_message,
            status,
            busy_message,
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
            slide: u(SLIDE) as i32,
        }
    }

    /// Where the caret is when the text box shows `dots` dots and is moved
    /// sideways by `shift` pixels: right after the last dot.
    pub fn caret_rect(&self, dots: usize, shift: i32) -> Rect {
        let b = self.text_box;
        let left = b.x + shift + self.box_padding + self.dot_pitch * dots.min(MAX_DOTS) as i32;
        Rect::new(left, b.y + (b.h - self.caret.1) / 2, self.caret.0, self.caret.1)
    }

    /// The background of this display: the bloom behind the mark.
    pub fn backdrop(&self) -> Backdrop {
        Backdrop::new(self.mark_pose.centre(), self.mark_pose.side())
    }

    /// The size in display pixels of text in `style`.
    pub fn text_px(&self, style: TextStyle) -> f32 {
        (style.size * self.unit).max(6.0)
    }
}

fn moved(rect: Rect, dy: i32) -> Rect {
    Rect::new(rect.x, rect.y + dy, rect.w, rect.h)
}

/// The items of one frame, in their slots.
fn compose<'a>(l: &Layout, view: &View<'a>) -> [Fresh<'a>; SLOTS] {
    let mut items: [Fresh<'a>; SLOTS] = [None; SLOTS];
    let m = &view.motion;
    let reveal = if m.reveal.is_nan() { 0.0 } else { m.reveal.clamp(0.0, 1.0) };
    // The mark moves first and fast, settling into place; the boot screen
    // gives way in the first part of the glide, and the prompt comes in
    // once the mark is nearly there, rising from a little lower. Going
    // back, the same in reverse: the prompt leaves, then the mark falls.
    let glide = assets::DECELERATE.at(reveal);
    let boot = 1.0 - assets::DECELERATE.at(reveal / 0.4);
    let prompt = assets::DECELERATE.at((reveal - 0.45) / 0.55);
    let slide = ((1.0 - prompt) * l.slide as f32).round() as i32;

    // The mark and the light of its dot.
    let pose = l.mark_pose.lerp(l.prompt_pose, glide).grown(m.mark_scale.clamp(0.5, 1.5));
    let alpha = level(m.mark_alpha);
    if alpha > 0 {
        items[SLOT_MARK] = Some(Item { rect: pose.rect(), kind: Kind::Mark { pose, assembly: m.assembly, alpha } });
        let glow = level(m.glow * m.mark_alpha);
        if let (true, Some((_, _, rect))) = (glow > 0, pose.glow()) {
            items[SLOT_GLOW] = Some(Item { rect, kind: Kind::Glow { pose, level: glow } });
        }
    }

    // The ring: a turning arc, or the reported progress.
    let alpha = level(m.ring_alpha * boot);
    if alpha > 0 {
        let progress = m.ring_progress.or(view.progress.map(|p| f32::from(p.min(100)) / 100.0));
        let (start, sweep, track) = match progress {
            Some(share) => (0.0, 360.0 * share.clamp(0.0, 1.0), level(TRACK)),
            None => {
                let (start, sweep) = ring_arc(view.phase);
                (start, sweep, 0)
            }
        };
        items[SLOT_RING] = Some(Item { rect: l.ring, kind: Kind::Ring { start, sweep, track, alpha } });
    }

    let label = |rect: Rect, text: &'a str, style: TextStyle, colour: Rgb, opacity: f32| {
        let alpha = level(opacity);
        (alpha > 0).then_some(Item { rect, kind: Kind::Label { text, progress: None, style, colour, alpha } })
    };
    let message = view.message.filter(|(text, _)| !text.trim().is_empty()).map(|(text, tone)| match tone {
        Tone::Info => (text, assets::TEXT_SECONDARY),
        Tone::Error => (error_line(text), assets::DANGER),
    });
    let text = m.text_alpha;

    // The status line of the shutdown and restart screens.
    let status = view.mode.status();
    if let Some(status) = status {
        items[SLOT_STATUS] = label(l.status, status, assets::BODY_LARGE, assets::TEXT, text * boot);
    }

    let unlock = view.prompt.filter(|_| prompt > 0.0);
    if let Some(p) = unlock {
        let lines = prompt_lines(p.text);
        let opacity = text * prompt;
        items[SLOT_TITLE] = label(moved(l.title, slide), lines.title, assets::SUBTITLE, assets::TEXT, opacity);
        if !lines.detail.is_empty() {
            items[SLOT_DETAIL] =
                label(moved(l.detail, slide), lines.detail, assets::BODY, assets::TEXT_SECONDARY, opacity);
        }
        let shift = (p.shake.clamp(-SHAKE_REACH, SHAKE_REACH) * l.unit).round() as i32;
        let dots = p.dots.min(MAX_DOTS);
        let alpha = level(opacity);
        if alpha > 0 {
            items[SLOT_FIELD] =
                Some(Item { rect: moved(l.field, slide), kind: Kind::Field { dots, shift, slide, alpha } });
            if p.caret {
                let rect = moved(l.caret_rect(dots, shift), slide);
                items[SLOT_CARET] = Some(Item { rect, kind: Kind::Caret { alpha } });
            }
        }
        let (hint, hint_colour) =
            if p.caps_lock { ("Caps Lock is on", assets::WARNING) } else { (lines.hint, assets::TEXT_MUTED) };
        // A message belongs to the box, so it goes right under it and the
        // hint moves down a line.
        let hint_line = match message.filter(|_| reveal >= 0.5) {
            Some((words, colour)) => {
                items[SLOT_MESSAGE] = label(moved(l.prompt_line, slide), words, assets::CAPTION, colour, opacity);
                l.prompt_second_line
            }
            None => l.prompt_line,
        };
        items[SLOT_HINT] = label(moved(hint_line, slide), hint, assets::CAPTION, hint_colour, opacity);
    }

    // The message line under the ring (and the status line), with the
    // percentage of a reported progress.
    if items[SLOT_MESSAGE].is_none()
        && (unlock.is_none() || reveal < 0.5)
        && (message.is_some() || view.progress.is_some())
    {
        let (words, colour) = message.unwrap_or(("", assets::TEXT_SECONDARY));
        let rect = if status.is_some() { l.busy_message } else { l.boot_message };
        let alpha = level(text * boot);
        if alpha > 0 {
            let kind = Kind::Label { text: words, progress: view.progress, style: assets::BODY, colour, alpha };
            items[SLOT_MESSAGE] = Some(Item { rect, kind });
        }
    }
    items
}

/// The picture for one display, with the state needed to redraw it minimally.
pub struct Scene {
    layout: Layout,
    canvas: Canvas,
    backdrop: Backdrop,
    painter: MarkPainter,
    /// The mark as last drawn, and for which pose and assembly.
    mark: Image,
    mark_drawn: Option<(Pose, Assembly)>,
    font: Font,
    /// A line of text as it is drawn (kept, so that drawing allocates nothing).
    line: String,
    /// The background where things move (the mark in both its places, the
    /// ring), kept from the first frame: repainting it there is a copy.
    under: Vec<u32>,
    under_rect: Rect,
    shown: [Shown; SLOTS],
    damage: Vec<Rect>,
    painted: bool,
}

impl Scene {
    pub fn new(width: u32, height: u32) -> Self {
        let layout = Layout::new(width, height);
        let backdrop = layout.backdrop();
        let under_rect = layout.mark.union(&layout.prompt_mark).union(&layout.ring).intersect(&Rect::new(
            0,
            0,
            width as i32,
            height as i32,
        ));
        Self {
            layout,
            canvas: Canvas::new(width, height),
            backdrop,
            painter: MarkPainter::new(),
            mark: Image::default(),
            mark_drawn: None,
            font: Font::new(),
            line: String::with_capacity(256),
            under: Vec::new(),
            under_rect,
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

    /// The background of this display.
    pub fn backdrop(&self) -> &Backdrop {
        &self.backdrop
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
        if let Some(Item { kind: Kind::Mark { pose, assembly, .. }, .. }) = items[SLOT_MARK] {
            if self.mark_drawn != Some((pose, assembly)) && self.damage.iter().any(|d| d.intersects(&pose.rect())) {
                self.painter.paint(pose, assembly, &mut self.mark);
                self.mark_drawn = Some((pose, assembly));
            }
        }
        for index in 0..self.damage.len() {
            let area = self.damage[index];
            self.paint_backdrop(&area);
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

    /// Paint the background into `area`: copied where it was kept, worked
    /// out elsewhere. The first frame keeps what it paints where things move.
    fn paint_backdrop(&mut self, area: &Rect) {
        let kept = self.under_rect;
        if !self.under.is_empty() && kept.contains(area) {
            for y in area.y..area.bottom() {
                let from = ((y - kept.y) * kept.w + (area.x - kept.x)) as usize;
                let row = self.canvas.row_mut(y as u32, area.x as u32, area.w as u32);
                row.copy_from_slice(&self.under[from..from + row.len()]);
            }
            return;
        }
        self.backdrop.fill(&mut self.canvas, area);
        if self.under.is_empty() && area.contains(&kept) && !kept.is_empty() {
            self.under.reserve_exact(kept.area() as usize);
            for y in kept.y..kept.bottom() {
                self.under.extend_from_slice(self.canvas.row(y as u32, kept.x as u32, kept.w as u32));
            }
        }
    }

    fn draw(&mut self, item: &Item<&str>, clip: &Rect) {
        let opacity = |alpha: u8| alpha as f32 / 255.0;
        match item.kind {
            Kind::Glow { pose, level } => self.draw_glow(&item.rect, pose, opacity(level), clip),
            Kind::Mark { alpha, .. } => {
                self.canvas.blit_image(&self.mark, item.rect.x, item.rect.y, opacity(alpha), clip);
            }
            Kind::Ring { start, sweep, track, alpha } => {
                self.draw_ring(&item.rect, start, sweep, opacity(track), opacity(alpha), clip)
            }
            Kind::Label { text, progress, style, colour, alpha } => {
                self.draw_label(&item.rect, text, progress, style, colour, opacity(alpha), clip)
            }
            Kind::Field { dots, shift, slide, alpha } => self.draw_field(dots, shift, slide, opacity(alpha), clip),
            Kind::Caret { alpha } => self.canvas.fill_rect_faded(clip, assets::TEXT, opacity(alpha)),
        }
    }

    /// The light of the dot: warm, strongest at its centre, gone at the band
    /// of the bowl.
    fn draw_glow(&mut self, rect: &Rect, pose: Pose, opacity: f32, clip: &Rect) {
        let Some(((cx, cy), reach, _)) = pose.glow() else { return };
        let inv = 1.0 / (reach * reach);
        self.canvas.shade(rect, clip, |px, py| {
            let q = 1.0 - ((px - cx).powi(2) + (py - cy).powi(2)) * inv;
            (q > 0.0).then_some((assets::GLOW, opacity * q * q))
        });
    }

    /// The ring: an arc with round ends from `start` over `sweep` degrees,
    /// over a faint full ring (the track) at `track`.
    fn draw_ring(&mut self, rect: &Rect, start: f32, sweep: f32, track: f32, opacity: f32, clip: &Rect) {
        let cx = rect.x as f32 + rect.w as f32 / 2.0;
        let cy = rect.y as f32 + rect.h as f32 / 2.0;
        let (radius, half) = (self.layout.ring_radius, self.layout.ring_stroke / 2.0);
        let end = start + sweep;
        let cap = |angle: f32| {
            let (sin, cos) = angle.to_radians().sin_cos();
            (cx + radius * sin, cy - radius * cos)
        };
        let (c0, c1) = (cap(start), cap(end));
        let full = sweep >= 360.0;
        self.canvas.shade(rect, clip, |px, py| {
            let (dx, dy) = (px - cx, py - cy);
            let band = ((dx * dx + dy * dy).sqrt() - radius).abs() - half;
            if band > 1.0 {
                return None;
            }
            let on_track = (0.5 - band).clamp(0.0, 1.0);
            let along = (dx.atan2(-dy).to_degrees() - start).rem_euclid(360.0);
            let distance = if full || along <= sweep {
                band
            } else {
                let to = |c: (f32, f32)| ((px - c.0).powi(2) + (py - c.1).powi(2)).sqrt();
                to(c0).min(to(c1)) - half
            };
            let on_arc = (0.5 - distance).clamp(0.0, 1.0);
            // The arc over the track: one layer of white over another.
            let cover = 1.0 - (1.0 - on_track * track) * (1.0 - on_arc);
            (cover > 0.0).then_some((assets::TEXT, cover * opacity))
        });
    }

    /// One line of text, centred in its rectangle and shortened to fit it.
    #[allow(clippy::too_many_arguments)]
    fn draw_label(
        &mut self,
        rect: &Rect,
        text: &str,
        progress: Option<u8>,
        style: TextStyle,
        colour: Rgb,
        opacity: f32,
        clip: &Rect,
    ) {
        let px = self.layout.text_px(style);
        self.line.clear();
        clean_into(&mut self.line, text);
        if let Some(percent) = progress {
            let percent = percent.min(100);
            let _ =
                if self.line.is_empty() { write!(self.line, "{percent}%") } else { write!(self.line, " ({percent}%)") };
        }
        let width = Font::fitted_width(&self.line, style.weight, px, rect.w as f32);
        let x = (rect.x as f32 + (rect.w as f32 - width) / 2.0).round();
        // The typeface's own line, centred in the taller line of the style.
        let baseline = rect.y as f32
            + (rect.h as f32 - Font::line_height(style.weight, px)) / 2.0
            + Font::ascent(style.weight, px);
        let line = std::mem::take(&mut self.line);
        self.font.draw_fitted(
            &mut self.canvas,
            clip,
            x,
            baseline,
            &line,
            rect.w as f32,
            style.weight,
            px,
            colour,
            opacity,
        );
        self.line = line;
    }

    /// The text box: the subtle translucent fill and one-pixel outline of a
    /// control, rounded corners, and the accent line of a focused control
    /// along its bottom edge. One dot per typed character (at most
    /// [`MAX_DOTS`]) from the left. `shift` moves all of it sideways, `slide`
    /// down.
    fn draw_field(&mut self, dots: usize, shift: i32, slide: i32, opacity: f32, clip: &Rect) {
        let unit = self.layout.unit;
        let u = |v: f32| (v * unit).round();
        let b = moved(self.layout.text_box, slide);
        let (x, y, w, h) = ((b.x + shift) as f32, b.y as f32, b.w as f32, b.h as f32);
        let radius = assets::RADIUS_CONTROL * unit;
        let stroke = u(1.0).max(1.0);
        let accent = u(2.0).max(2.0).min(h / 2.0);
        // Whole-pixel widths on whole-pixel edges keep every line crisp. The
        // box is one rounded shape: the accent below a pixel row near its
        // bottom, and above it the fill with the outline round it, as
        // layers of white over what lies beneath.
        let split = (b.bottom() as f32 - accent) as i32;
        let (fill, outline) = (assets::FILL_CONTROL, assets::STROKE_CONTROL);
        let inner = (x + stroke, y + stroke, w - stroke * 2.0, h - stroke - accent, (radius - stroke).max(0.0));
        let area = Rect::new(b.x + shift, b.y, b.w, b.h);
        // Well inside, away from the edges and the corners, it is the fill alone.
        let (safe_x, safe_y) = (x + radius + stroke + 1.0, y + radius + stroke + 1.0);
        let (safe_r, safe_b) = (x + w - radius - stroke - 1.0, (split as f32 - 1.0).min(y + h - radius - accent - 1.0));
        self.canvas.shade(&area, clip, |px, py| {
            if px > safe_x && px < safe_r && py > safe_y && py < safe_b {
                return Some((assets::CONTROL_LAYER, fill * opacity));
            }
            let outer = (0.5 - round_rect_distance(px, py, x, y, w, h, radius)).clamp(0.0, 1.0);
            if outer <= 0.0 {
                return None;
            }
            if py as i32 >= split {
                return Some((assets::ACCENT, outer * opacity));
            }
            let within =
                (0.5 - round_rect_distance(px, py, inner.0, inner.1, inner.2, inner.3, inner.4)).clamp(0.0, 1.0);
            let cover = 1.0 - (1.0 - fill * outer) * (1.0 - outline * (outer - within).max(0.0));
            Some((assets::CONTROL_LAYER, cover * opacity))
        });
        let (pad, pitch, dot) = (self.layout.box_padding as f32, self.layout.dot_pitch as f32, self.layout.dot_radius);
        for i in 0..dots.min(MAX_DOTS) {
            self.canvas.fill_circle_faded(
                x + pad + pitch * i as f32 + dot,
                y + h / 2.0,
                dot,
                assets::TEXT,
                opacity,
                clip,
            );
        }
    }
}

/// Make a client-supplied string safe to lay out: one line, no control
/// characters, bounded length; appended to `out`.
fn clean_into(out: &mut String, text: &str) {
    let start = out.len();
    out.extend(text.chars().map(|c| if c.is_control() { ' ' } else { c }).take(240));
    let trimmed = out[start..].trim();
    let (lead, len) = (out[start..].len() - out[start..].trim_start().len(), trimmed.len());
    out.truncate(start + lead + len);
    out.drain(start..start + lead);
}

#[cfg(test)]
fn clean(text: &str) -> String {
    let mut out = String::new();
    clean_into(&mut out, text);
    out
}

#[cfg(test)]
mod tests;
