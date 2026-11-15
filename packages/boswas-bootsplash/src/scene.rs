//! What the boot screen shows, and how little of it is redrawn.
//!
//! The picture is a short list of items in fixed slots: the mark, the
//! wordmark, the progress arc, and a few text lines and the passphrase field.
//! [`Scene::render`] compares the new list with the one on screen and
//! repaints only the rectangles of items that changed, so an animation frame
//! touches the arc's few thousand pixels and nothing else. The lockup never
//! moves: everything that changes lives in a band below it.

use crate::assets;
use crate::gfx::{bounding_rect, merge_rects, resample, Canvas, Image, Mask, Rect, Rgb};
use crate::text::Font;

/// Most dots the passphrase field shows, so that a long passphrase does not
/// reveal its exact length to someone watching the screen.
pub const MAX_DOTS: usize = 24;

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
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PromptView {
    pub text: String,
    /// Number of characters typed so far.
    pub dots: usize,
    pub caps_lock: bool,
}

/// Everything the renderer needs to know for one frame.
#[derive(Clone, Debug, PartialEq)]
pub struct View {
    pub mode: Mode,
    pub prompt: Option<PromptView>,
    pub message: Option<(String, Tone)>,
    /// Boot progress in percent, when a client reported one.
    pub progress: Option<u8>,
    /// Position of the comet's head, in turns (0..1, clockwise from the top).
    pub phase: f32,
}

impl View {
    pub fn new(mode: Mode) -> Self {
        Self { mode, prompt: None, message: None, progress: None, phase: 0.0 }
    }
}

#[derive(Clone, Debug, PartialEq)]
enum Kind {
    Mark,
    Wordmark,
    Arc { phase: f32, progress: Option<u8> },
    Label { text: String, px: u32, colour: Rgb },
    Field { dots: usize },
}

#[derive(Clone, Debug, PartialEq)]
struct Item {
    rect: Rect,
    kind: Kind,
}

const SLOT_MARK: usize = 0;
const SLOT_WORDMARK: usize = 1;
const SLOT_ARC: usize = 2;
const SLOT_STATUS: usize = 3;
const SLOT_MESSAGE: usize = 4;
const SLOT_PROMPT: usize = 5;
const SLOT_FIELD: usize = 6;
const SLOT_HINT: usize = 7;
const SLOTS: usize = 8;

/// Where everything goes on a display of a given size.
#[derive(Clone, Debug, PartialEq)]
pub struct Layout {
    pub width: i32,
    pub height: i32,
    /// Size of one design unit in pixels (1.0 on a 1080-line display).
    pub unit: f32,
    pub mark: Rect,
    pub wordmark: Rect,
    pub arc: Rect,
    pub arc_centre: (f32, f32),
    pub arc_radius: f32,
    pub arc_thickness: f32,
    pub status: Rect,
    pub boot_message: Rect,
    pub prompt: Rect,
    pub field: Rect,
    /// The pill itself, inside `field` (which also covers the focus ring).
    pub pill: (f32, f32, f32, f32),
    pub hint: Rect,
    pub prompt_message: Rect,
    pub text_px: u32,
    pub status_px: u32,
    pub small_px: u32,
}

impl Layout {
    pub fn new(width: u32, height: u32) -> Self {
        let w = width.max(1) as f32;
        let h = height.max(1) as f32;
        let unit = (h / 1080.0).clamp(0.55, 3.2);
        let u = |v: f32| v * unit;

        // The lockup: mark above wordmark, centred a little above the middle.
        let mark_h = (0.18 * h).round().clamp(96.0, assets::MARK_HEIGHT as f32).min(h * 0.5).max(1.0);
        let mark_w = (mark_h * assets::MARK_WIDTH as f32 / assets::MARK_HEIGHT as f32).round();
        let word_w = (2.1 * mark_h).round().min(w * 0.9).max(1.0);
        let word_h = (word_w * assets::WORDMARK_HEIGHT as f32 / assets::WORDMARK_WIDTH as f32).round().max(1.0);
        let gap = (0.23 * mark_h).round();
        let lockup_h = mark_h + gap + word_h;
        let top = (0.42 * h - lockup_h / 2.0).round().max(0.0);
        let cx = (w / 2.0).round();
        let mark = Rect::new((cx - mark_w / 2.0).round() as i32, top as i32, mark_w as i32, mark_h as i32);
        let wordmark =
            Rect::new((cx - word_w / 2.0).round() as i32, (top + mark_h + gap) as i32, word_w as i32, word_h as i32);

        // The band below the lockup: arc and status, or the prompt.
        let band_top = wordmark.bottom() as f32 + u(56.0);
        let text_px = (u(20.0).round() as u32).max(13);
        let status_px = (u(18.0).round() as u32).max(13);
        let small_px = (u(15.0).round() as u32).max(12);
        let line_w = (w - u(48.0)).min(u(900.0)).max(1.0);
        let line = |y: f32, px: u32| {
            Rect::new(
                (cx - line_w / 2.0).round() as i32,
                y.round() as i32,
                line_w.round() as i32,
                Font::line_height(px) + 2,
            )
        };

        let arc_radius = u(24.0);
        let arc_thickness = u(4.0).max(2.5);
        let arc_centre = (cx, (band_top + u(32.0)).round());
        let reach = arc_radius + arc_thickness;
        let arc = bounding_rect(arc_centre.0 - reach, arc_centre.1 - reach, reach * 2.0, reach * 2.0, 1.0);
        let status = line(band_top + u(76.0), status_px);
        let boot_message = line(status.bottom() as f32 + u(10.0), small_px);

        let prompt = line(band_top, text_px);
        let pill_w = u(360.0).min(w - u(40.0)).max(1.0).round();
        let pill_h = u(46.0).round();
        let pill_x = (cx - pill_w / 2.0).round();
        let pill_y = (prompt.bottom() as f32 + u(14.0)).round();
        let ring = u(3.0).round().max(2.0) + u(2.0).round().max(2.0);
        let field = bounding_rect(pill_x - ring, pill_y - ring, pill_w + ring * 2.0, pill_h + ring * 2.0, 1.0);
        let hint = line(field.bottom() as f32 + u(8.0), small_px);
        let prompt_message = line(hint.bottom() as f32 + u(4.0), small_px);

        Self {
            width: width as i32,
            height: height as i32,
            unit,
            mark,
            wordmark,
            arc,
            arc_centre,
            arc_radius,
            arc_thickness,
            status,
            boot_message,
            prompt,
            field,
            pill: (pill_x, pill_y, pill_w, pill_h),
            hint,
            prompt_message,
            text_px,
            status_px,
            small_px,
        }
    }
}

/// The picture for one display, with the state needed to redraw it minimally.
pub struct Scene {
    layout: Layout,
    canvas: Canvas,
    mark: Image,
    wordmark: Mask,
    font: Font,
    shown: Vec<Option<Item>>,
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
        let (ww, wh) = (layout.wordmark.w.max(1) as usize, layout.wordmark.h.max(1) as usize);
        let wordmark = Mask {
            width: ww as u32,
            height: wh as u32,
            data: resample(
                assets::WORDMARK_A8,
                assets::WORDMARK_WIDTH as usize,
                assets::WORDMARK_HEIGHT as usize,
                1,
                ww,
                wh,
            ),
        };
        Self {
            layout,
            canvas: Canvas::new(width, height),
            mark,
            wordmark,
            font: Font::new(),
            shown: vec![None; SLOTS],
            painted: false,
        }
    }

    pub fn layout(&self) -> &Layout {
        &self.layout
    }

    pub fn canvas(&self) -> &Canvas {
        &self.canvas
    }

    /// Forget what is on screen: the next [`Scene::render`] repaints everything.
    pub fn invalidate(&mut self) {
        self.painted = false;
    }

    fn compose(&mut self, view: &View) -> Vec<Option<Item>> {
        let l = self.layout.clone();
        let mut items: Vec<Option<Item>> = vec![None; SLOTS];
        items[SLOT_MARK] = Some(Item { rect: l.mark, kind: Kind::Mark });
        items[SLOT_WORDMARK] = Some(Item { rect: l.wordmark, kind: Kind::Wordmark });

        let mut label = |rect: Rect, text: &str, px: u32, colour: Rgb| {
            let text = self.font.fit(&clean(text), px, rect.w as f32);
            Item { rect, kind: Kind::Label { text, px, colour } }
        };
        let message = view.message.as_ref().filter(|(text, _)| !text.trim().is_empty()).map(|(text, tone)| {
            let colour = match tone {
                Tone::Info => assets::TEXT_MUTED,
                Tone::Error => assets::DANGER,
            };
            (text.as_str(), colour)
        });

        match &view.prompt {
            Some(prompt) => {
                items[SLOT_PROMPT] = Some(label(l.prompt, &prompt.text, l.text_px, assets::TEXT));
                items[SLOT_FIELD] = Some(Item { rect: l.field, kind: Kind::Field { dots: prompt.dots.min(MAX_DOTS) } });
                if prompt.caps_lock {
                    items[SLOT_HINT] = Some(label(l.hint, "Caps Lock is on", l.small_px, assets::WARNING));
                }
                if let Some((text, colour)) = message {
                    items[SLOT_MESSAGE] = Some(label(l.prompt_message, text, l.small_px, colour));
                }
            }
            None => {
                items[SLOT_ARC] =
                    Some(Item { rect: l.arc, kind: Kind::Arc { phase: view.phase, progress: view.progress } });
                if let Some(status) = view.mode.status() {
                    items[SLOT_STATUS] = Some(label(l.status, status, l.status_px, assets::TEXT_SECONDARY));
                }
                if let Some((text, colour)) = message {
                    items[SLOT_MESSAGE] = Some(label(l.boot_message, text, l.small_px, colour));
                }
            }
        }
        items
    }

    /// Bring the canvas up to date with `view` and return the rectangles that
    /// changed (empty when nothing did). The rectangles do not overlap.
    pub fn render(&mut self, view: &View) -> Vec<Rect> {
        let items = self.compose(view);
        let mut damage = Vec::new();
        if !self.painted {
            damage.push(self.canvas.bounds());
        } else {
            for (old, new) in self.shown.iter().zip(items.iter()) {
                if old != new {
                    damage.extend(old.as_ref().map(|i| i.rect));
                    damage.extend(new.as_ref().map(|i| i.rect));
                }
            }
        }
        let bounds = self.canvas.bounds();
        damage.iter_mut().for_each(|r| *r = r.intersect(&bounds));
        merge_rects(&mut damage);
        for area in &damage {
            self.canvas.fill_rect(area, assets::BACKGROUND);
            for item in items.iter().flatten() {
                let clip = item.rect.intersect(area);
                if !clip.is_empty() {
                    self.draw(item, &clip);
                }
            }
        }
        self.shown = items;
        self.painted = true;
        damage
    }

    fn draw(&mut self, item: &Item, clip: &Rect) {
        match &item.kind {
            Kind::Mark => self.canvas.blit_image(&self.mark, item.rect.x, item.rect.y, clip),
            Kind::Wordmark => self.canvas.blit_mask(&self.wordmark, item.rect.x, item.rect.y, assets::TEXT, clip),
            Kind::Arc { phase, progress } => self.draw_arc(*phase, *progress, clip),
            Kind::Label { text, px, colour } => {
                let width = self.font.measure(text, *px);
                let x = item.rect.x as f32 + (item.rect.w as f32 - width) / 2.0;
                let baseline = item.rect.y as f32 + 1.0 + Font::ascent(*px);
                self.font.draw(&mut self.canvas, clip, x, baseline, text, *px, *colour);
            }
            Kind::Field { dots } => self.draw_field(*dots, clip),
        }
    }

    /// The progress arc: a faint track, the reported progress on it, and the
    /// comet (bright head, tail fading to nothing) that shows the system is
    /// alive.
    fn draw_arc(&mut self, phase: f32, progress: Option<u8>, clip: &Rect) {
        let (cx, cy) = self.layout.arc_centre;
        let r = self.layout.arc_radius;
        let t = self.layout.arc_thickness;
        let area = self.layout.arc;
        let turn = std::f32::consts::TAU;
        let done = progress.map(|p| p.min(100) as f32 / 100.0);
        // Clockwise angle from 12 o'clock, in turns (0..1).
        let angle = |dx: f32, dy: f32| {
            let a = dx.atan2(-dy) / turn;
            if a < 0.0 {
                a + 1.0
            } else {
                a
            }
        };

        self.canvas.shade(&area, clip, |px, py| {
            let (dx, dy) = (px - cx, py - cy);
            let ring = 0.5 - ((dx * dx + dy * dy).sqrt() - r).abs() + t / 2.0;
            if ring <= 0.0 {
                return None;
            }
            let colour = match done {
                Some(d) if angle(dx, dy) <= d => assets::ACCENT_DEEP,
                _ => assets::TRACK,
            };
            Some((colour, ring))
        });

        let phase = phase.rem_euclid(1.0);
        let tail = 0.72; // length of the comet, in turns
        let head_angle = phase * turn;
        let (hx, hy) = (cx + r * head_angle.sin(), cy - r * head_angle.cos());
        self.canvas.shade(&area, clip, |px, py| {
            let (dx, dy) = (px - cx, py - cy);
            let ring = (0.5 - ((dx * dx + dy * dy).sqrt() - r).abs() + t / 2.0).clamp(0.0, 1.0);
            let cap = (0.5 - (((px - hx).powi(2) + (py - hy).powi(2)).sqrt() - t / 2.0)).clamp(0.0, 1.0);
            if ring <= 0.0 && cap <= 0.0 {
                return None;
            }
            let behind = (phase - angle(dx, dy)).rem_euclid(1.0);
            let k = if behind < tail { 1.0 - behind / tail } else { 0.0 };
            let body = ring * k.powf(1.3);
            if cap >= body {
                Some((assets::ACCENT, cap))
            } else {
                Some((assets::ACCENT_TAIL.mix(assets::ACCENT, k), body))
            }
        });
    }

    /// The passphrase field: a pill with a focus ring and one dot per typed
    /// character (at most [`MAX_DOTS`]); a caret marks the empty field.
    fn draw_field(&mut self, dots: usize, clip: &Rect) {
        let u = self.layout.unit;
        let (x, y, w, h) = self.layout.pill;
        let radius = h / 2.0;
        // Whole-pixel widths on whole-pixel edges keep every line crisp: the
        // ring and the border are drawn as nested filled shapes.
        let gap = (3.0 * u).round().max(2.0);
        let ring = (2.0 * u).round().max(2.0);
        let border = (1.5 * u).round().max(1.0);
        let mut nested = |inset: f32, colour: Rgb| {
            self.canvas.fill_round_rect(
                x - inset,
                y - inset,
                w + inset * 2.0,
                h + inset * 2.0,
                radius + inset,
                colour,
                clip,
            );
        };
        nested(gap + ring, assets::ACCENT);
        nested(gap, assets::BACKGROUND);
        nested(0.0, assets::FIELD_BORDER);
        nested(-border, assets::FIELD_FILL);
        let (cx, cy) = (x + w / 2.0, y + h / 2.0);
        if dots == 0 {
            let cw = (2.0 * u).round().max(2.0);
            let ch = (20.0 * u).round();
            self.canvas.fill_round_rect(
                (cx - cw / 2.0).round(),
                (cy - ch / 2.0).round(),
                cw,
                ch,
                cw / 2.0,
                assets::ACCENT,
                clip,
            );
            return;
        }
        let dots = dots.min(MAX_DOTS);
        let spacing = (13.0 * u).min((w - h) / MAX_DOTS as f32);
        let dot = (3.6 * u).max(2.0).min(spacing * 0.4);
        let start = cx - spacing * (dots - 1) as f32 / 2.0;
        for i in 0..dots {
            self.canvas.fill_circle(start + spacing * i as f32, cy, dot, assets::TEXT, clip);
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

    fn area_of(rects: &[Rect]) -> i64 {
        rects.iter().map(Rect::area).sum()
    }

    fn count(canvas: &Canvas, rect: &Rect, colour: Rgb) -> usize {
        let mut n = 0;
        for y in rect.y..rect.bottom() {
            for x in rect.x..rect.right() {
                if canvas.pixel(x as u32, y as u32) == colour {
                    n += 1;
                }
            }
        }
        n
    }

    #[test]
    fn layout_fits_common_displays() {
        for (w, h) in
            [(800, 600), (1024, 768), (1366, 768), (1920, 1080), (2560, 1440), (3840, 2160), (640, 480), (1080, 1920)]
        {
            let l = Layout::new(w, h);
            let screen = Rect::new(0, 0, w as i32, h as i32);
            for (name, r) in [
                ("mark", l.mark),
                ("wordmark", l.wordmark),
                ("arc", l.arc),
                ("status", l.status),
                ("boot message", l.boot_message),
                ("prompt", l.prompt),
                ("field", l.field),
                ("hint", l.hint),
                ("prompt message", l.prompt_message),
            ] {
                assert!(screen.contains(&r), "{name} {r:?} leaves a {w}x{h} display");
            }
            // The lockup is centred and the band is below it.
            assert!((l.mark.x + l.mark.w / 2 - w as i32 / 2).abs() <= 1);
            assert!(l.wordmark.y >= l.mark.bottom());
            assert!(l.arc.y > l.wordmark.bottom() && l.prompt.y > l.wordmark.bottom());
            // Items that are visible together do not overlap.
            assert!(!l.arc.intersects(&l.status) && !l.status.intersects(&l.boot_message));
            assert!(
                !l.prompt.intersects(&l.field) && !l.field.intersects(&l.hint) && !l.hint.intersects(&l.prompt_message)
            );
        }
        let hd = Layout::new(1920, 1080);
        assert!((hd.mark.h - 194).abs() <= 1, "the mark is about 18 % of the height: {}", hd.mark.h);
        assert_eq!(hd.unit, 1.0);
    }

    #[test]
    fn tiny_and_odd_sizes_do_not_panic() {
        for (w, h) in [(1, 1), (7, 3), (320, 200), (200, 3000), (5000, 100)] {
            let mut scene = Scene::new(w, h);
            let mut view = View::new(Mode::Boot);
            scene.render(&view);
            view.prompt = Some(PromptView { text: "x".into(), dots: 3, caps_lock: true });
            view.message = Some(("m".into(), Tone::Error));
            scene.render(&view);
        }
    }

    #[test]
    fn first_frame_paints_everything_then_nothing() {
        let mut scene = Scene::new(1280, 720);
        let view = View::new(Mode::Boot);
        let damage = scene.render(&view);
        assert_eq!(damage, vec![Rect::new(0, 0, 1280, 720)]);
        assert_eq!(scene.canvas().pixel(2, 2), assets::BACKGROUND);
        assert!(scene.render(&view).is_empty(), "an unchanged view repaints nothing");
        scene.invalidate();
        assert_eq!(scene.render(&view).len(), 1);
    }

    #[test]
    fn the_mark_and_wordmark_are_drawn_in_place() {
        let mut scene = Scene::new(1920, 1080);
        scene.render(&View::new(Mode::Boot));
        let l = scene.layout().clone();
        let canvas = scene.canvas();
        let ink = |r: &Rect| (r.area() as usize) - count(canvas, r, assets::BACKGROUND);
        assert!(ink(&l.mark) > (l.mark.area() / 6) as usize, "the mark covers a good part of its box");
        assert!(count(canvas, &l.wordmark, assets::TEXT) > 500, "the wordmark is drawn in the text colour");
        // Left of the lockup there is only background.
        let left = Rect::new(0, 0, l.wordmark.x.min(l.mark.x) - 2, 1080);
        assert_eq!(count(canvas, &left, assets::BACKGROUND), left.area() as usize);
        // The mark keeps its gold: some pixel is clearly warmer than the background.
        let mut gold = 0;
        for y in l.mark.y..l.mark.bottom() {
            for x in l.mark.x..l.mark.right() {
                let p = canvas.pixel(x as u32, y as u32);
                if p.r > 180 && p.g > 140 && p.b < 170 && p.r > p.b + 40 {
                    gold += 1;
                }
            }
        }
        assert!(gold > 200, "gold pixels in the mark: {gold}");
    }

    #[test]
    fn an_animation_frame_only_damages_the_arc() {
        let mut scene = Scene::new(1920, 1080);
        let mut view = View::new(Mode::Boot);
        scene.render(&view);
        let before: Vec<u32> = scene.canvas().pixels().to_vec();
        view.phase = 0.37;
        let damage = scene.render(&view);
        let arc = scene.layout().arc;
        assert_eq!(damage, vec![arc]);
        assert!(arc.area() < 72 * 72, "the arc is small: {arc:?}");
        // No pixel outside the damage changed.
        let after = scene.canvas().pixels();
        for y in 0..1080 {
            for x in 0..1920 {
                let inside = x >= arc.x && x < arc.right() && y >= arc.y && y < arc.bottom();
                if !inside {
                    let i = (y * 1920 + x) as usize;
                    assert_eq!(before[i], after[i], "pixel {x},{y} changed outside the damage");
                }
            }
        }
        // And the arc did change.
        assert!((0..before.len()).any(|i| before[i] != after[i]));
    }

    #[test]
    fn the_comet_head_follows_the_phase() {
        let mut scene = Scene::new(1920, 1080);
        let mut view = View::new(Mode::Boot);
        for (phase, dx, dy) in [(0.0f32, 0.0f32, -1.0f32), (0.25, 1.0, 0.0), (0.5, 0.0, 1.0), (0.75, -1.0, 0.0)] {
            view.phase = phase;
            scene.render(&view);
            let l = scene.layout();
            let (cx, cy) = l.arc_centre;
            let head = scene.canvas().pixel((cx + dx * l.arc_radius) as u32, (cy + dy * l.arc_radius) as u32);
            assert_eq!(head, assets::ACCENT, "head at phase {phase}");
            // Just ahead of the head the track is bare.
            let ahead = (phase + 0.12) * std::f32::consts::TAU;
            let p = scene
                .canvas()
                .pixel((cx + ahead.sin() * l.arc_radius) as u32, (cy - ahead.cos() * l.arc_radius) as u32);
            assert_eq!(p, assets::TRACK, "track ahead of the head at phase {phase}");
        }
    }

    #[test]
    fn typing_only_damages_the_field() {
        let mut scene = Scene::new(1920, 1080);
        let mut view = View::new(Mode::Boot);
        view.prompt = Some(PromptView { text: "Please unlock disk sda3_crypt".into(), dots: 0, caps_lock: false });
        scene.render(&view);
        let field = scene.layout().field;
        let dots_before = count(scene.canvas(), &field, assets::TEXT);
        view.prompt.as_mut().unwrap().dots = 5;
        assert_eq!(scene.render(&view), vec![field]);
        assert!(count(scene.canvas(), &field, assets::TEXT) > dots_before + 5 * 20, "five dots appear");
        // The dot count is capped.
        view.prompt.as_mut().unwrap().dots = MAX_DOTS;
        scene.render(&view);
        let capped = scene.canvas().pixels().to_vec();
        view.prompt.as_mut().unwrap().dots = MAX_DOTS + 40;
        assert!(scene.render(&view).is_empty(), "more characters than the cap change nothing on screen");
        assert_eq!(capped, scene.canvas().pixels());
    }

    #[test]
    fn prompt_replaces_the_arc_and_the_lockup_stays() {
        let mut scene = Scene::new(1366, 768);
        let mut view = View::new(Mode::Boot);
        scene.render(&view);
        let l = scene.layout().clone();
        view.prompt = Some(PromptView { text: "Please unlock disk sda3_crypt".into(), dots: 2, caps_lock: true });
        view.message =
            Some(("cryptsetup: ERROR: sda3_crypt: cryptsetup failed, bad password or options?".into(), Tone::Error));
        let damage = scene.render(&view);
        for d in &damage {
            assert!(!d.intersects(&l.mark) && !d.intersects(&l.wordmark), "the lockup is not repainted: {d:?}");
        }
        assert!(area_of(&damage) < (1366 * 768) / 4);
        let canvas = scene.canvas();
        assert!(count(canvas, &l.prompt, assets::TEXT) > 100, "prompt text");
        assert!(count(canvas, &l.field, assets::FIELD_FILL) > 1000, "field fill");
        assert!(count(canvas, &l.field, assets::ACCENT) > 100, "focus ring");
        assert!(count(canvas, &l.hint, assets::WARNING) > 20, "Caps Lock hint");
        assert!(count(canvas, &l.prompt_message, assets::DANGER) > 50, "error message");
        assert_eq!(count(canvas, &l.arc, assets::TRACK), 0, "the arc is gone while prompting");
        // Back to the arc.
        view.prompt = None;
        view.message = None;
        scene.render(&view);
        assert!(count(scene.canvas(), &l.arc, assets::TRACK) > 50);
        assert_eq!(count(scene.canvas(), &l.field, assets::FIELD_FILL), 0);
    }

    #[test]
    fn shutdown_and_reboot_show_their_status() {
        for mode in [Mode::Shutdown, Mode::Reboot] {
            let mut scene = Scene::new(1920, 1080);
            scene.render(&View::new(mode));
            let l = scene.layout().clone();
            assert!(count(scene.canvas(), &l.status, assets::TEXT_SECONDARY) > 100, "{mode:?}");
        }
        let mut scene = Scene::new(1920, 1080);
        scene.render(&View::new(Mode::Boot));
        let l = scene.layout().clone();
        assert_eq!(count(scene.canvas(), &l.status, assets::BACKGROUND), l.status.area() as usize);
    }

    #[test]
    fn progress_fills_the_track_without_moving_anything() {
        let mut scene = Scene::new(1920, 1080);
        let mut view = View::new(Mode::Boot);
        view.phase = 0.9;
        scene.render(&view);
        let arc = scene.layout().arc;
        assert_eq!(count(scene.canvas(), &arc, assets::ACCENT_DEEP), 0);
        view.progress = Some(50);
        assert_eq!(scene.render(&view), vec![arc]);
        assert!(count(scene.canvas(), &arc, assets::ACCENT_DEEP) > 30);
    }

    #[test]
    fn hostile_text_is_contained() {
        let mut scene = Scene::new(1024, 768);
        let mut view = View::new(Mode::Boot);
        let nasty = "\u{1b}[2J\n\r\t\u{0}".repeat(500) + &"W".repeat(5000);
        view.message = Some((nasty.clone(), Tone::Info));
        view.prompt = Some(PromptView { text: nasty, dots: usize::MAX, caps_lock: false });
        let damage = scene.render(&view);
        assert!(!damage.is_empty());
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
