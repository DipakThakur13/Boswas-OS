//! Screen capture: a frozen picture of the screen on which the user picks
//! an area (or confirms the whole screen). The result is saved to the
//! Pictures folder and copied to the clipboard.

use std::path::{Path, PathBuf};

use std::cell::RefCell;

use boswas_ui::{canvas::PixelRect, text::TextEngine, Canvas, Rgba};
use tiny_skia::{Pixmap, Transform};

use crate::{
    action::Action,
    ui::{ramp, Frame, Rect},
};

#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    /// The picture itself: dragging on it selects an area.
    Surface,
}

/// Smallest selection (logical pixels) that counts as one.
const MIN_SELECTION: f32 = 4.0;

pub struct Capture {
    /// The screen as it was when the capture started (device pixels).
    pub shot: Pixmap,
    /// The same picture, darkened: what is outside the selection.
    dimmed: Pixmap,
    /// Capture the whole screen instead of an area.
    pub whole: bool,
    /// Where the drag started and where the pointer is, in logical pixels.
    pub anchor: Option<(f32, f32)>,
    pub pointer: Option<(f32, f32)>,
    pub dragging: bool,
    /// What the last drawing showed, so that the next one is drawn again
    /// only where it differs (see [`Capture::changes`]).
    drawn: RefCell<Option<Look>>,
}

/// What a drawing of the capture view shows, as far as where it changes.
#[derive(Clone, Debug, PartialEq)]
struct Look {
    /// The canvas's size in device pixels: when it is the picture's own,
    /// the picture is copied, not scaled.
    pixels: (u32, u32),
    /// The selection in device pixels, and its frame (`frame` pixels wide,
    /// `gap` pixels outside it).
    selection: Option<PixelRect>,
    gap: u32,
    frame: u32,
    /// The size tag next to the selection, and what it says.
    tag: Option<(Rect, String)>,
    /// The hint at the top, and what it says.
    hint: (Rect, &'static str),
}

impl Capture {
    pub fn new(shot: Pixmap, whole: bool) -> Self {
        let mut dimmed = shot.clone();
        for pixel in dimmed.data_mut().chunks_exact_mut(4) {
            for c in &mut pixel[..3] {
                *c = (u16::from(*c) * 115 / 255) as u8;
            }
        }
        Self {
            shot,
            dimmed,
            whole,
            anchor: None,
            pointer: None,
            dragging: false,
            drawn: RefCell::new(None),
        }
    }

    /// The selected area in logical pixels of a surface of `size`.
    pub fn selection(&self, size: (f32, f32)) -> Option<Rect> {
        if self.whole {
            return Some(Rect::new(0.0, 0.0, size.0, size.1));
        }
        let (a, b) = (self.anchor?, self.pointer?);
        let clamp = |p: (f32, f32)| (p.0.clamp(0.0, size.0), p.1.clamp(0.0, size.1));
        let (a, b) = (clamp(a), clamp(b));
        let rect = Rect::new(a.0.min(b.0), a.1.min(b.1), (a.0 - b.0).abs(), (a.1 - b.1).abs());
        (rect.w >= MIN_SELECTION && rect.h >= MIN_SELECTION).then_some(rect)
    }

    /// The selected area in pixels of the picture (x, y, width, height),
    /// for a surface of `size` logical pixels: what is captured, and where
    /// the selection is drawn.
    fn pixel_selection(&self, size: (f32, f32)) -> Option<PixelRect> {
        let selection = self.selection(size)?;
        let (sx, sy) = (self.shot.width() as f32 / size.0, self.shot.height() as f32 / size.1);
        let x0 = ((selection.x * sx).round() as u32).min(self.shot.width() - 1);
        let y0 = ((selection.y * sy).round() as u32).min(self.shot.height() - 1);
        let w = ((selection.w * sx).round() as u32).clamp(1, self.shot.width() - x0);
        let h = ((selection.h * sy).round() as u32).clamp(1, self.shot.height() - y0);
        Some((x0, y0, w, h))
    }

    /// The selected part of the picture, at the picture's own resolution.
    pub fn crop(&self, size: (f32, f32)) -> Option<Pixmap> {
        let (x0, y0, w, h) = self.pixel_selection(size)?;
        let mut out = Pixmap::new(w, h)?;
        let stride = self.shot.width() as usize * 4;
        for (row, target) in out.data_mut().chunks_exact_mut(w as usize * 4).enumerate() {
            let start = (y0 as usize + row) * stride + x0 as usize * 4;
            target.copy_from_slice(&self.shot.data()[start..start + w as usize * 4]);
        }
        Some(out)
    }

    /// What a drawing on a canvas of `size` logical pixels at `scale`
    /// (`pixels` device pixels) shows.
    fn look(&self, text: &mut TextEngine, size: (f32, f32), scale: f32, pixels: (u32, u32)) -> Look {
        let w = size.0;
        let selection = self.selection(size);
        let tag = selection.filter(|_| !self.whole).map(|s| {
            let picture_scale = self.shot.width() as f32 / w;
            let label = format!("{} × {}", (s.w * picture_scale).round() as u32, (s.h * picture_scale).round() as u32);
            let width = text.measure(&label, &ramp("caption", Rgba::WHITE)).0 + 16.0;
            let y = if s.y >= 30.0 { s.y - 28.0 } else { s.y + 6.0 };
            (Rect::new(s.x.min(w - width - 4.0).max(4.0), y, width, 22.0), label)
        });
        let hint = match (self.whole, selection.is_some(), self.dragging) {
            (true, _, _) => "Click or press Enter to capture this screen · Esc to cancel",
            (false, true, false) => "Press Enter to capture the selection · drag to change it · Esc to cancel",
            _ => "Drag to select an area · Esc to cancel",
        };
        let width = (text.measure(hint, &ramp("body", Rgba::WHITE)).0 + 32.0).min(w - 16.0);
        Look {
            pixels,
            selection: self.pixel_selection(size),
            gap: (0.5 * scale).floor() as u32,
            frame: scale.round().max(1.0) as u32,
            tag,
            hint: (Rect::new((w - width) / 2.0, 24.0, width, 36.0), hint),
        }
    }

    /// Where a drawing on a canvas of `size` logical pixels at `scale`
    /// (`pixels` device pixels) would differ from the last one, in logical
    /// pixels (empty: nowhere); `None` when it is to be drawn whole. While
    /// a selection is dragged only the strips its edges moved across, its
    /// size tag and the hint change: the picture itself is drawn once.
    pub fn changes(&self, text: &mut TextEngine, size: (f32, f32), scale: f32, pixels: (u32, u32)) -> Option<Vec<Rect>> {
        if pixels != (self.shot.width(), self.shot.height()) {
            return None;
        }
        let new = self.look(text, size, scale, pixels);
        let drawn = self.drawn.borrow();
        let old = drawn.as_ref().filter(|old| old.pixels == new.pixels)?;
        let mut parts: Vec<PixelRect> = selection_changes(old, &new);
        if old.tag != new.tag {
            for (rect, _) in old.tag.iter().chain(new.tag.iter()) {
                parts.push(crate::ui::device_rect(&rect.inset(-1.0), scale));
            }
        }
        if old.hint != new.hint {
            parts.push(crate::ui::device_rect(&old.hint.0.inset(-1.0), scale));
            parts.push(crate::ui::device_rect(&new.hint.0.inset(-1.0), scale));
        }
        Some(
            parts
                .into_iter()
                .filter(|p| p.2 > 0 && p.3 > 0)
                .map(|(x, y, w, h)| Rect::new(x as f32 / scale, y as f32 / scale, w as f32 / scale, h as f32 / scale))
                .collect(),
        )
    }
}

/// The device-pixel strips in which the selection and its frame look
/// different between two drawings: for each edge that moved, the strip it
/// moved across, with the frame on both sides of the edge.
fn selection_changes(old: &Look, new: &Look) -> Vec<PixelRect> {
    // How far from an edge of the selection its drawing reaches: the gap,
    // the frame and a pixel to spare.
    let reach = i64::from(new.gap.max(old.gap) + new.frame.max(old.frame) + 1);
    let grown = |r: PixelRect| -> [i64; 4] {
        [i64::from(r.0) - reach, i64::from(r.1) - reach, i64::from(r.0 + r.2) + reach, i64::from(r.1 + r.3) + reach]
    };
    let to_rect = |b: [i64; 4]| -> PixelRect {
        let (x0, y0) = (b[0].max(0), b[1].max(0));
        let (x1, y1) = (b[2].max(x0), b[3].max(y0));
        (x0 as u32, y0 as u32, (x1 - x0) as u32, (y1 - y0) as u32)
    };
    let same_frame = old.gap == new.gap && old.frame == new.frame;
    match (old.selection, new.selection) {
        (None, None) => Vec::new(),
        (Some(r), None) | (None, Some(r)) => vec![to_rect(grown(r))],
        (Some(a), Some(b)) if a == b && same_frame => Vec::new(),
        (Some(a), Some(b)) => {
            let (a, b) = (grown(a), grown(b));
            // The zone of an edge is 2 * reach wide, inwards from the grown
            // rectangle's side.
            let zone = 2 * reach;
            let span = [a[0].min(b[0]), a[1].min(b[1]), a[2].max(b[2]), a[3].max(b[3])];
            let mut strips = Vec::new();
            if a[0] != b[0] || !same_frame {
                strips.push(to_rect([a[0].min(b[0]), span[1], a[0].max(b[0]) + zone, span[3]]));
            }
            if a[2] != b[2] || !same_frame {
                strips.push(to_rect([a[2].min(b[2]) - zone, span[1], a[2].max(b[2]), span[3]]));
            }
            if a[1] != b[1] || !same_frame {
                strips.push(to_rect([span[0], a[1].min(b[1]), span[2], a[1].max(b[1]) + zone]));
            }
            if a[3] != b[3] || !same_frame {
                strips.push(to_rect([span[0], a[3].min(b[3]) - zone, span[2], a[3].max(b[3])]));
            }
            strips
        }
    }
}

/// An XDG user folder (`PICTURES`, `DOCUMENTS`, ...) from `user-dirs.dirs`,
/// else `default` in the home folder.
fn xdg_user_dir(home: &Path, user_dirs: Option<&str>, key: &str, default: &str) -> PathBuf {
    let prefix = format!("XDG_{key}_DIR=");
    user_dirs
        .and_then(|text| {
            text.lines().find_map(|line| {
                let value = line.trim().strip_prefix(prefix.as_str())?.trim().trim_matches('"');
                let path = match value.strip_prefix("$HOME/") {
                    Some(rest) => home.join(rest),
                    None => PathBuf::from(value),
                };
                (path.is_absolute() && path != home && !value.contains("..")).then_some(path)
            })
        })
        .unwrap_or_else(|| home.join(default))
}

/// The folder screenshots are saved in: `Screenshots` under the user's
/// Pictures folder (from `user-dirs.dirs`, else `~/Pictures`).
pub fn screenshots_dir(home: &Path, user_dirs: Option<&str>) -> PathBuf {
    xdg_user_dir(home, user_dirs, "PICTURES", "Pictures").join("Screenshots")
}

/// The user's folder behind one of Start's folder buttons (`None` for the
/// two that are programs).
pub fn user_dir(home: &Path, user_dirs: Option<&str>, folder: crate::settings::Folder) -> Option<PathBuf> {
    let key = folder.xdg_name()?;
    Some(xdg_user_dir(home, user_dirs, key, folder.label()))
}

/// A file name for a screenshot taken at `time` that does not exist yet
/// in `dir` according to `exists`.
pub fn file_name(time: &str, exists: impl Fn(&str) -> bool) -> String {
    let base = format!("Screenshot {time}");
    let mut name = format!("{base}.png");
    let mut n = 2;
    while exists(&name) && n < 1000 {
        name = format!("{base} ({n}).png");
        n += 1;
    }
    name
}

pub fn draw(f: &mut Frame, capture: &Capture) {
    let (w, h) = (f.canvas.width(), f.canvas.height());
    let whole = Rect::new(0.0, 0.0, w, h);
    f.area(whole, Action::Capture(Act::Surface));
    f.drag();
    let (scale, pixels) = (f.canvas.scale(), f.canvas.pixel_size());
    let look = capture.look(&mut f.res.text, (w, h), scale, pixels);
    if pixels == (capture.shot.width(), capture.shot.height()) {
        // The picture is the canvas's size (as a rule): it is copied, not
        // scaled, and only where the canvas is drawn again; the selection
        // shows the very pixels it captures, framed on whole pixels.
        f.canvas.copy_from_full(&capture.dimmed);
        if let Some((x, y, sw, sh)) = look.selection {
            copy_rect(f.canvas, &capture.shot, (x, y, sw, sh));
            let (g, t) = (look.gap, look.frame);
            let (ox0, oy0) = (x.saturating_sub(g + t), y.saturating_sub(g + t));
            let (ox1, oy1) = (x + sw + g + t, y + sh + g + t);
            let (ix0, iy0) = (x.saturating_sub(g), y.saturating_sub(g));
            let (ix1, iy1) = (x + sw + g, y + sh + g);
            for bar in [
                (ox0, oy0, ox1 - ox0, iy0 - oy0),
                (ox0, iy1, ox1 - ox0, oy1 - iy1),
                (ox0, iy0, ix0 - ox0, iy1 - iy0),
                (ix1, iy0, ox1 - ix1, iy1 - iy0),
            ] {
                fill_rect(f.canvas, bar, [255, 255, 255, 255]);
            }
        }
    } else {
        // Another size (the scale changed while the view was open): the
        // picture is scaled to the canvas.
        let placement = Transform::from_scale(w / capture.shot.width() as f32, h / capture.shot.height() as f32);
        f.canvas.draw_pixmap(&capture.dimmed, 0.0, 0.0, w, h, 1.0);
        if let Some(s) = capture.selection((w, h)) {
            f.canvas.fill_rounded_rect_pixmap(s.x, s.y, s.w, s.h, [0.0; 4], &capture.shot, placement);
            f.canvas.stroke_rounded_rect(s.x - 1.0, s.y - 1.0, s.w + 2.0, s.h + 2.0, 0.0, 1.0, Rgba::WHITE);
        }
    }
    if let Some((tag, label)) = &look.tag {
        f.canvas.fill_rounded_rect(tag.x, tag.y, tag.w, tag.h, 4.0, Rgba::BLACK.alpha(0.72));
        f.text_centered(label, *tag, &ramp("caption", Rgba::WHITE));
    }
    let (pill, hint) = look.hint;
    f.canvas.fill_rounded_rect(pill.x, pill.y, pill.w, pill.h, 18.0, Rgba::BLACK.alpha(0.72));
    f.text_centered(hint, pill.inset(8.0), &ramp("body", Rgba::WHITE));
    *capture.drawn.borrow_mut() = Some(look);
}

/// The parts of the canvas being drawn: all of it, or its clip.
fn drawn_parts(canvas: &Canvas) -> Vec<PixelRect> {
    let (w, h) = canvas.pixel_size();
    if canvas.clip().is_empty() {
        vec![(0, 0, w, h)]
    } else {
        canvas.clip().to_vec()
    }
}

/// The overlap of two device-pixel rectangles.
fn intersect(a: PixelRect, b: PixelRect) -> Option<PixelRect> {
    let (x0, y0) = (a.0.max(b.0), a.1.max(b.1));
    let (x1, y1) = ((a.0 + a.2).min(b.0 + b.2), (a.1 + a.3).min(b.1 + b.3));
    (x1 > x0 && y1 > y0).then(|| (x0, y0, x1 - x0, y1 - y0))
}

/// Copies `rect` (device pixels) of `picture`, a picture of the canvas's
/// size, onto the canvas, where it is being drawn.
fn copy_rect(canvas: &mut Canvas, picture: &Pixmap, rect: PixelRect) {
    let (w, h) = canvas.pixel_size();
    if (picture.width(), picture.height()) != (w, h) {
        return;
    }
    let parts = drawn_parts(canvas);
    let w = w as usize;
    let data = canvas.pixmap_mut().data_mut();
    for part in parts.into_iter().filter_map(|p| intersect(p, rect)) {
        for y in part.1 as usize..(part.1 + part.3) as usize {
            let range = (y * w + part.0 as usize) * 4..(y * w + (part.0 + part.2) as usize) * 4;
            data[range.clone()].copy_from_slice(&picture.data()[range]);
        }
    }
}

/// Fills `rect` (device pixels) with an opaque colour, where the canvas is
/// being drawn.
fn fill_rect(canvas: &mut Canvas, rect: PixelRect, rgba: [u8; 4]) {
    let (w, h) = canvas.pixel_size();
    let Some(rect) = intersect(rect, (0, 0, w, h)) else {
        return;
    };
    let parts = drawn_parts(canvas);
    let data = canvas.pixmap_mut().data_mut();
    for part in parts.into_iter().filter_map(|p| intersect(p, rect)) {
        for y in part.1 as usize..(part.1 + part.3) as usize {
            let row = y * w as usize;
            for pixel in data[(row + part.0 as usize) * 4..(row + (part.0 + part.2) as usize) * 4].chunks_exact_mut(4) {
                pixel.copy_from_slice(&rgba);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shot() -> Pixmap {
        // 200 x 100 device pixels; each pixel's red is its column, green its row.
        let mut p = Pixmap::new(200, 100).unwrap();
        for (i, px) in p.data_mut().chunks_exact_mut(4).enumerate() {
            px.copy_from_slice(&[(i % 200) as u8, (i / 200) as u8, 0, 255]);
        }
        p
    }

    /// A picture of `w` x `h` device pixels with a pattern in every pixel.
    fn busy_shot(w: u32, h: u32) -> Pixmap {
        let mut p = Pixmap::new(w, h).unwrap();
        for (i, px) in p.data_mut().chunks_exact_mut(4).enumerate() {
            let (x, y) = (i as u32 % w, i as u32 / w);
            px.copy_from_slice(&[(x * 7 % 256) as u8, (y * 5 % 256) as u8, ((x ^ y) % 256) as u8, 255]);
        }
        p
    }

    fn draw_whole(res: &mut crate::ui::Resources, capture: &Capture, size: (f32, f32), scale: f32) -> Canvas {
        let mut canvas = Canvas::new(size.0, size.1, scale).unwrap();
        let mut hits = Vec::new();
        draw(&mut Frame { canvas: &mut canvas, res, pointer: None, pressed: false, hits: &mut hits, focus: None, origin: (0.0, 0.0) }, capture);
        canvas
    }

    /// Drags a selection about as the shell does: after each move only
    /// what [`Capture::changes`] names is drawn again, on a spare canvas
    /// holding an older drawing, and copied over the drawing. The result
    /// must be exactly a whole drawing, and the parts drawn again must stay
    /// near the edges that moved (the picture is not drawn again).
    #[test]
    fn dragging_a_selection_draws_only_the_edges_that_moved_and_the_same_pixels() {
        let mut res = crate::ui::Resources::new(boswas_ui::Theme::of(boswas_ui::Scheme::Dark));
        for scale in [1.0, 1.25, 2.0] {
            let size = (640.0, 400.0);
            let pixels = ((size.0 * scale) as u32, (size.1 * scale) as u32);
            let mut capture = Capture::new(busy_shot(pixels.0, pixels.1), false);
            let mut front = draw_whole(&mut res, &capture, size, scale);
            let mut spare = draw_whole(&mut res, &capture, size, scale);
            assert_eq!(capture.changes(&mut res.text, size, scale, pixels), Some(Vec::new()), "nothing changed");
            capture.anchor = Some((100.3, 80.6));
            capture.dragging = true;
            let mut pointer = (100.3, 80.6);
            let steps = [(3.0, 2.0), (7.5, 0.0), (0.0, 9.25), (40.0, 30.0), (-2.0, 1.5), (-150.0, -120.0), (0.6, -0.3), (300.0, 200.0), (0.0, 0.0)];
            let mut drawn_again = 0u64;
            for (i, (dx, dy)) in steps.iter().enumerate() {
                let before = capture.pixel_selection(size);
                pointer = (pointer.0 + dx, pointer.1 + dy);
                capture.pointer = Some(pointer);
                if i == steps.len() - 1 {
                    capture.dragging = false;
                }
                let after = capture.pixel_selection(size);
                let rects = capture.changes(&mut res.text, size, scale, pixels).expect("drawn in parts");
                let device: Vec<PixelRect> = rects.iter().map(|r| crate::ui::device_rect(r, scale)).collect();
                let clip = spare.begin_clip(&device);
                if !clip.is_empty() {
                    let mut hits = Vec::new();
                    draw(&mut Frame { canvas: &mut spare, res: &mut res, pointer: None, pressed: false, hits: &mut hits, focus: None, origin: (0.0, 0.0) }, &capture);
                    spare.end_clip();
                    for rect in clip {
                        crate::damage::copy_rect(spare.data(), front.pixmap_mut().data_mut(), pixels.0, rect);
                        drawn_again += u64::from(rect.2) * u64::from(rect.3);
                    }
                }
                let expected = draw_whole(&mut res, &capture, size, scale);
                if let Some(differ) = crate::damage::changed_rect(front.data(), expected.data(), pixels.0, pixels.1) {
                    panic!("at {scale}, step {i} ({before:?} to {after:?}): parts {device:?} drawn again, yet {differ:?} differs from a whole drawing");
                }
                // Small moves draw strips along the edges, not the selection
                // (the last step lets go: the hint changes too).
                if let (Some(a), Some(b), true) = (before, after, dx.abs() < 10.0 && dy.abs() < 10.0 && capture.dragging) {
                    let area: u64 = device.iter().map(|r| u64::from(r.2) * u64::from(r.3)).sum();
                    let selection = u64::from(a.2.max(b.2)) * u64::from(a.3.max(b.3));
                    assert!(area < selection / 2 + 40_000, "at {scale}, step {i}: {area} pixels drawn again for a selection of {selection}");
                }
            }
            let whole = u64::from(pixels.0) * u64::from(pixels.1);
            assert!(drawn_again < whole * steps.len() as u64 / 2, "at {scale}: {drawn_again} of {whole} pixels per step");
        }
    }

    #[test]
    fn the_selection_shows_the_pixels_that_are_captured() {
        let mut res = crate::ui::Resources::new(boswas_ui::Theme::of(boswas_ui::Scheme::Dark));
        let size = (320.0, 200.0);
        let mut capture = Capture::new(busy_shot(640, 400), false);
        capture.anchor = Some((40.2, 80.7));
        capture.pointer = Some((200.6, 180.1));
        let canvas = draw_whole(&mut res, &capture, size, 2.0);
        let crop = capture.crop(size).unwrap();
        let (x0, y0, w, h) = capture.pixel_selection(size).unwrap();
        assert_eq!((crop.width(), crop.height()), (w, h));
        for (cx, cy) in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w / 2, h / 2)] {
            let on_screen = canvas.pixmap().pixel(x0 + cx, y0 + cy).unwrap();
            assert_eq!(on_screen, crop.pixel(cx, cy).unwrap(), "({cx}, {cy})");
        }
        // Just outside: the frame, then (beyond it) the darkened picture.
        assert_eq!(canvas.pixmap().pixel(x0 - 2, y0 + h / 2).unwrap().red(), 255);
        let beyond = canvas.pixmap().pixel(x0 - 5, y0 + h / 2).unwrap();
        assert_eq!(beyond, capture.dimmed.pixel(x0 - 5, y0 + h / 2).unwrap());
    }

    #[test]
    fn a_drag_selects_a_rectangle_in_any_direction() {
        let mut capture = Capture::new(shot(), false);
        assert_eq!(capture.selection((200.0, 100.0)), None);
        capture.anchor = Some((150.0, 80.0));
        capture.pointer = Some((50.0, 20.0));
        assert_eq!(capture.selection((200.0, 100.0)), Some(Rect::new(50.0, 20.0, 100.0, 60.0)));
        capture.pointer = Some((500.0, -40.0));
        assert_eq!(capture.selection((200.0, 100.0)), Some(Rect::new(150.0, 0.0, 50.0, 80.0)), "clamped to the screen");
        capture.pointer = Some((151.0, 81.0));
        assert_eq!(capture.selection((200.0, 100.0)), None, "a click is not a selection");
        let whole = Capture::new(shot(), true);
        assert_eq!(whole.selection((200.0, 100.0)), Some(Rect::new(0.0, 0.0, 200.0, 100.0)));
    }

    #[test]
    fn the_crop_is_taken_from_the_picture_at_its_own_resolution() {
        let mut capture = Capture::new(shot(), false);
        capture.anchor = Some((10.0, 5.0));
        capture.pointer = Some((30.0, 25.0));
        // The surface is 100 x 50 logical pixels: the picture is at scale 2.
        let crop = capture.crop((100.0, 50.0)).unwrap();
        assert_eq!((crop.width(), crop.height()), (40, 40));
        let first = crop.pixel(0, 0).unwrap();
        assert_eq!((first.red(), first.green()), (20, 10));
        let last = crop.pixel(39, 39).unwrap();
        assert_eq!((last.red(), last.green()), (59, 49));
        let all = Capture::new(shot(), true).crop((100.0, 50.0)).unwrap();
        assert_eq!((all.width(), all.height()), (200, 100));
        // The darkened copy is darker and keeps the original untouched.
        assert!(capture.dimmed.pixel(100, 50).unwrap().red() < capture.shot.pixel(100, 50).unwrap().red());
    }

    #[test]
    fn screenshots_go_to_the_pictures_folder() {
        let home = Path::new("/home/asha");
        assert_eq!(screenshots_dir(home, None), Path::new("/home/asha/Pictures/Screenshots"));
        let dirs = "# comment\nXDG_DESKTOP_DIR=\"$HOME/Desktop\"\nXDG_PICTURES_DIR=\"$HOME/Bilder\"\n";
        assert_eq!(screenshots_dir(home, Some(dirs)), Path::new("/home/asha/Bilder/Screenshots"));
        assert_eq!(screenshots_dir(home, Some("XDG_PICTURES_DIR=\"/data/pics\"\n")), Path::new("/data/pics/Screenshots"));
        for bad in ["XDG_PICTURES_DIR=\"relative\"\n", "XDG_PICTURES_DIR=\"$HOME/\"\n", "XDG_PICTURES_DIR=\"$HOME/../other\"\n"] {
            assert_eq!(screenshots_dir(home, Some(bad)), Path::new("/home/asha/Pictures/Screenshots"), "{bad}");
        }
    }

    #[test]
    fn start_folders_are_the_users_folders() {
        use crate::settings::Folder;
        let home = Path::new("/home/asha");
        let dirs = "XDG_DOWNLOAD_DIR=\"$HOME/Herunterladen\"\nXDG_MUSIC_DIR=\"/data/music\"\n";
        assert_eq!(user_dir(home, Some(dirs), Folder::Downloads).unwrap(), Path::new("/home/asha/Herunterladen"));
        assert_eq!(user_dir(home, Some(dirs), Folder::Music).unwrap(), Path::new("/data/music"));
        assert_eq!(user_dir(home, Some(dirs), Folder::Documents).unwrap(), Path::new("/home/asha/Documents"));
        assert_eq!(user_dir(home, None, Folder::Videos).unwrap(), Path::new("/home/asha/Videos"));
        assert_eq!(user_dir(home, None, Folder::Settings), None);
        assert_eq!(user_dir(home, None, Folder::Files), None);
    }

    #[test]
    fn file_names_do_not_collide() {
        assert_eq!(file_name("2026-11-15 163812", |_| false), "Screenshot 2026-11-15 163812.png");
        let taken = ["Screenshot t.png", "Screenshot t (2).png"];
        assert_eq!(file_name("t", |name| taken.contains(&name)), "Screenshot t (3).png");
    }
}
