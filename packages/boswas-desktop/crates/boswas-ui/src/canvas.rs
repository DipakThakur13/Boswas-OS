//! Software drawing for Boswas UI surfaces.
//!
//! A [`Canvas`] is a premultiplied RGBA pixmap addressed in logical pixels;
//! the output scale is applied when drawing, so callers lay out in logical
//! units and still get crisp edges on HiDPI outputs.
//!
//! A part of a drawing can be drawn again on its own (see
//! [`Canvas::begin_clip`]): callers draw the whole as usual, and what lies
//! entirely outside that part is skipped. Inside it the pixels are exactly
//! those a whole drawing gives (the same operations reach them in the same
//! order, with the same transform); outside it they are of no use, so the
//! part is drawn on a spare canvas and copied from there. A surface whose
//! hover state changed in one place is drawn again this way in that place
//! only.

use tiny_skia::{
    FillRule, FilterQuality, GradientStop, LineCap, LineJoin, LinearGradient, Paint, Path, PathBuilder, Pixmap,
    PixmapPaint, PixmapRef, Point, Rect, SpreadMode, Stroke, Transform,
};

use crate::tokens::Rgba;

/// Corner radii in the order top-left, top-right, bottom-right, bottom-left.
pub type Corners = [f32; 4];

/// A rectangle in device pixels: x, y, width, height.
pub type PixelRect = (u32, u32, u32, u32);

/// A drawing surface in logical pixels.
pub struct Canvas {
    pixmap: Pixmap,
    width: f32,
    height: f32,
    scale: f32,
    /// Nothing has been drawn since the canvas was made or reset (or since
    /// the clip began).
    blank: bool,
    /// The pixmap still holds an earlier drawing that the first drawing
    /// operation clears (a reset canvas that is then covered completely by
    /// [`Canvas::copy_from_full`] is never cleared).
    stale: bool,
    /// The parts being drawn again: drawing that cannot reach them is
    /// skipped. Empty: the whole canvas is drawn.
    clip: Vec<PixelRect>,
}

impl Canvas {
    /// A transparent canvas of `width` x `height` logical pixels. `None` when
    /// the size is empty or too large to allocate.
    pub fn new(width: f32, height: f32, scale: f32) -> Option<Self> {
        let scale = if scale.is_finite() && scale > 0.0 { scale } else { 1.0 };
        let w = (width * scale).ceil();
        let h = (height * scale).ceil();
        if !(w >= 1.0 && h >= 1.0 && w <= 16384.0 && h <= 16384.0) {
            return None;
        }
        Some(Self {
            pixmap: Pixmap::new(w as u32, h as u32)?,
            width,
            height,
            scale,
            blank: true,
            stale: false,
            clip: Vec::new(),
        })
    }

    /// Logical width.
    pub fn width(&self) -> f32 {
        self.width
    }

    /// Logical height.
    pub fn height(&self) -> f32 {
        self.height
    }

    pub fn scale(&self) -> f32 {
        self.scale
    }

    /// Size of the backing store in device pixels.
    pub fn pixel_size(&self) -> (u32, u32) {
        (self.pixmap.width(), self.pixmap.height())
    }

    /// Whether nothing was drawn since the canvas was made or reset, or
    /// since its clip began.
    pub fn is_blank(&self) -> bool {
        self.blank
    }

    /// Makes the canvas transparent again, for drawing anew. The pixels
    /// are cleared when drawing starts, unless the first thing drawn covers
    /// them all ([`Canvas::copy_from_full`]).
    pub fn reset(&mut self) {
        self.end_clip();
        self.blank = true;
        self.stale = true;
    }

    /// Starts drawing the parts `rects` (device pixels) again: they are made
    /// transparent, and until [`Canvas::end_clip`] drawing that cannot
    /// reach them is skipped. Pixels outside them may change and are not to
    /// be used. Returns the parts as clipped to the canvas (empty: nothing
    /// is clipped).
    pub fn begin_clip(&mut self, rects: &[PixelRect]) -> Vec<PixelRect> {
        self.touch();
        let (w, h) = self.pixel_size();
        self.clip.clear();
        for rect in rects {
            let (x0, y0) = (rect.0.min(w), rect.1.min(h));
            let (x1, y1) = (rect.0.saturating_add(rect.2).min(w), rect.1.saturating_add(rect.3).min(h));
            if x1 <= x0 || y1 <= y0 {
                continue;
            }
            let pixels = self.pixmap.data_mut();
            for y in y0..y1 {
                let row = (y * w) as usize;
                pixels[(row + x0 as usize) * 4..(row + x1 as usize) * 4].fill(0);
            }
            self.clip.push((x0, y0, x1 - x0, y1 - y0));
        }
        if !self.clip.is_empty() {
            self.blank = true;
        }
        self.clip.clone()
    }

    /// Ends drawing parts again (see [`Canvas::begin_clip`]).
    pub fn end_clip(&mut self) {
        self.clip.clear();
    }

    /// The parts being drawn again (empty: the whole canvas is drawn).
    pub fn clip(&self) -> &[PixelRect] {
        &self.clip
    }

    /// About to draw: clears an earlier drawing first.
    fn touch(&mut self) {
        if self.stale {
            self.pixmap.fill(tiny_skia::Color::TRANSPARENT);
            self.stale = false;
        }
        self.blank = false;
    }

    pub fn pixmap(&self) -> &Pixmap {
        &self.pixmap
    }

    /// The pixels themselves, to change without the canvas's drawing
    /// operations. A clip does not apply to them.
    pub fn pixmap_mut(&mut self) -> &mut Pixmap {
        self.touch();
        &mut self.pixmap
    }

    pub fn into_pixmap(mut self) -> Pixmap {
        if self.stale {
            self.touch();
        }
        self.pixmap
    }

    /// Premultiplied RGBA bytes, row by row.
    pub fn data(&self) -> &[u8] {
        self.pixmap.data()
    }

    /// Copies the canvas as `wl_shm` ARGB8888 (premultiplied, little endian:
    /// B, G, R, A). `out` must hold `width * height * 4` bytes.
    pub fn write_argb8888(&self, out: &mut [u8]) {
        for (dst, src) in out.chunks_exact_mut(4).zip(self.pixmap.data().chunks_exact(4)) {
            dst[0] = src[2];
            dst[1] = src[1];
            dst[2] = src[0];
            dst[3] = src[3];
        }
    }

    /// Copies the part `rect` (device pixels) of the canvas into `out`, a
    /// `wl_shm` ARGB8888 buffer of the canvas's size, at the same place.
    pub fn write_argb8888_rect(&self, out: &mut [u8], rect: PixelRect) {
        let (w, h) = (self.pixmap.width() as usize, self.pixmap.height() as usize);
        if out.len() < w * h * 4 {
            return;
        }
        let (x0, y0) = ((rect.0 as usize).min(w), (rect.1 as usize).min(h));
        let (x1, y1) = ((rect.0 as usize + rect.2 as usize).min(w), (rect.1 as usize + rect.3 as usize).min(h));
        if x1 <= x0 || y1 <= y0 {
            return;
        }
        let data = self.pixmap.data();
        // R, G, B, A to B, G, R, A: red and blue trade places; two pixels at
        // a time (the first drawing of a screen is millions of them).
        let swap = |p: u64| (p & 0xff00_ff00_ff00_ff00) | ((p >> 16) & 0x0000_00ff_0000_00ff) | ((p & 0x0000_00ff_0000_00ff) << 16);
        for y in y0..y1 {
            let range = (y * w + x0) * 4..(y * w + x1) * 4;
            let (dst, src) = (&mut out[range.clone()], &data[range]);
            let mut pairs = dst.chunks_exact_mut(8);
            for (d, s) in (&mut pairs).zip(src.chunks_exact(8)) {
                let p = u64::from_le_bytes([s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7]]);
                d.copy_from_slice(&swap(p).to_le_bytes());
            }
            if let (Some(d), Some(s)) = (pairs.into_remainder().get_mut(..4), src.len().checked_sub(4).map(|at| &src[at..])) {
                let p = u64::from(u32::from_le_bytes([s[0], s[1], s[2], s[3]]));
                d.copy_from_slice(&(swap(p) as u32).to_le_bytes());
            }
        }
    }

    /// Takes the pixels of `whole`, a picture of the canvas's size,
    /// replacing what was there (within the clip, while there is one).
    pub fn copy_from_full(&mut self, whole: &Pixmap) {
        if (whole.width(), whole.height()) != self.pixel_size() {
            return;
        }
        if self.clip.is_empty() {
            self.pixmap.data_mut().copy_from_slice(whole.data());
            self.stale = false;
            self.blank = false;
            return;
        }
        self.copy_rows_from_full(whole, 0, whole.height());
    }

    /// Puts the device rows `first..last` back as `whole` (a picture of the
    /// canvas's size) has them (within the clip, while there is one).
    pub fn copy_rows_from_full(&mut self, whole: &Pixmap, first: u32, last: u32) {
        if (whole.width(), whole.height()) != self.pixel_size() {
            return;
        }
        self.touch();
        let (w, h) = self.pixel_size();
        let whole_canvas = [(0, 0, w, h)];
        let parts: &[PixelRect] = if self.clip.is_empty() { &whole_canvas } else { &self.clip };
        let w = w as usize;
        let data = self.pixmap.data_mut();
        for &(x0, y0, cw, ch) in parts {
            let (x0, x1) = (x0 as usize, (x0 + cw) as usize);
            for y in first.max(y0) as usize..last.min(y0 + ch) as usize {
                let range = (y * w + x0) * 4..(y * w + x1) * 4;
                data[range.clone()].copy_from_slice(&whole.data()[range]);
            }
        }
    }

    /// Whether anything drawn in the logical rectangle can change a pixel
    /// that may change (a pixel of margin for anti-aliasing).
    pub fn visible(&self, x: f32, y: f32, w: f32, h: f32) -> bool {
        if self.clip.is_empty() {
            return true;
        }
        let s = self.scale;
        let (left, top) = (x * s - 1.0, y * s - 1.0);
        let (right, bottom) = ((x + w) * s + 1.0, (y + h) * s + 1.0);
        // NaN sizes fail every comparison and are drawn (as before).
        self.clip.iter().any(|&(cx, cy, cw, ch)| {
            let (cx, cy, cw, ch) = (cx as f32, cy as f32, cw as f32, ch as f32);
            !(right < cx || bottom < cy || left > cx + cw || top > cy + ch)
        })
    }

    /// Whether a device-pixel rectangle overlaps the pixels that may change.
    fn visible_device(&self, x: i32, y: i32, w: i32, h: i32) -> bool {
        self.clip.is_empty()
            || self.clip.iter().any(|&(cx, cy, cw, ch)| {
                let (cx, cy, cw, ch) = (cx as i32, cy as i32, cw as i32, ch as i32);
                x < cx + cw && y < cy + ch && x + w > cx && y + h > cy
            })
    }

    /// Draws a picture at a place given in device pixels, unscaled.
    pub fn draw_device_pixmap(&mut self, x: i32, y: i32, picture: PixmapRef, paint: &PixmapPaint) {
        if !self.visible_device(x, y, picture.width() as i32, picture.height() as i32) {
            return;
        }
        // Onto a canvas with nothing drawn on it yet, a picture laid on
        // unchanged is copied (the same pixels as blending it over
        // transparency; a panel's shadow at 200 % is millions of them).
        let plain = paint.opacity >= 1.0 && paint.blend_mode == tiny_skia::BlendMode::SourceOver && paint.quality == FilterQuality::Nearest;
        let onto_nothing = self.blank && self.clip.is_empty();
        self.touch();
        if plain && onto_nothing && picture.width() > 0 && picture.height() > 0 {
            let (w, h) = (self.pixmap.width() as i64, self.pixmap.height() as i64);
            let (pw, ph) = (picture.width() as i64, picture.height() as i64);
            let (x, y) = (i64::from(x), i64::from(y));
            // tiny-skia rounds a negative place up and keeps the size: a
            // picture placed left of (above) the canvas covers one more
            // column (row) on the far side, which repeats its last one.
            let (x1, y1) = ((x + pw + i64::from(x < 0)).min(w), (y + ph + i64::from(y < 0)).min(h));
            let (x0, y0) = (x.max(0), y.max(0));
            let source = picture.data();
            let target = self.pixmap.data_mut();
            for row in y0..y1 {
                let from_row = (row - y).min(ph - 1);
                let to = ((row * w + x0) * 4) as usize;
                let inside = (x1.min(x + pw) - x0).max(0);
                if inside > 0 {
                    let from = ((from_row * pw + (x0 - x)) * 4) as usize;
                    let len = (inside * 4) as usize;
                    target[to..to + len].copy_from_slice(&source[from..from + len]);
                }
                if x1 > x + pw && x + pw >= 0 {
                    let last = ((from_row * pw + pw - 1) * 4) as usize;
                    let at = ((row * w + x + pw) * 4) as usize;
                    let pixel: [u8; 4] = source[last..last + 4].try_into().unwrap_or([0; 4]);
                    target[at..at + 4].copy_from_slice(&pixel);
                }
            }
            return;
        }
        self.pixmap.draw_pixmap(x, y, picture, paint, Transform::identity(), None);
    }

    fn transform(&self) -> Transform {
        Transform::from_scale(self.scale, self.scale)
    }

    fn paint(color: Rgba) -> Paint<'static> {
        let mut paint = Paint::default();
        paint.set_color(color.into());
        paint.anti_alias = true;
        paint
    }

    /// Fills the whole canvas (the clip, while there is one), replacing
    /// what was there.
    pub fn clear(&mut self, color: Rgba) {
        self.stale = false;
        self.blank = false;
        if self.clip.is_empty() {
            return self.pixmap.fill(color.into());
        }
        let w = self.pixmap.width() as usize;
        let c = tiny_skia::Color::from(color).premultiply().to_color_u8();
        let pixel = [c.red(), c.green(), c.blue(), c.alpha()];
        let data = self.pixmap.data_mut();
        for &(x0, y0, cw, ch) in &self.clip {
            for y in y0 as usize..(y0 + ch) as usize {
                for p in data[(y * w + x0 as usize) * 4..(y * w + (x0 + cw) as usize) * 4].chunks_exact_mut(4) {
                    p.copy_from_slice(&pixel);
                }
            }
        }
    }

    pub fn fill_rect(&mut self, x: f32, y: f32, w: f32, h: f32, color: Rgba) {
        if !self.visible(x, y, w, h) {
            return;
        }
        self.touch();
        if let Some(rect) = Rect::from_xywh(x, y, w, h) {
            let mut paint = Self::paint(color);
            // Axis-aligned rectangles on the pixel grid need no anti-aliasing.
            paint.anti_alias = self.scale.fract() != 0.0 || [x, y, w, h].iter().any(|v| v.fract() != 0.0);
            // (tiny-skia rounds a negative edge its own way: left to it.)
            if !paint.anti_alias && x >= 0.0 && y >= 0.0 {
                return self.fill_pixel_rect(x, y, w, h, color);
            }
            let transform = self.transform();
            self.pixmap.fill_rect(rect, &paint, transform, None);
        }
    }

    /// [`Canvas::fill_rect`] of a rectangle on the pixel grid, done here and
    /// only where the canvas is being drawn (within the clip): a dimmed
    /// screen behind the lock screen is drawn again in the part that changed,
    /// not over the whole screen. The pixels are exactly tiny-skia's (its
    /// source-over of a solid colour, or its copy of an opaque one).
    fn fill_pixel_rect(&mut self, x: f32, y: f32, w: f32, h: f32, color: Rgba) {
        let c = tiny_skia::Color::from(color).premultiply();
        let [r, g, b, a] = [c.red(), c.green(), c.blue(), c.alpha()].map(|v| (v * 255.0 + 0.5) as u16);
        let (pw, ph) = (self.pixmap.width() as i64, self.pixmap.height() as i64);
        let s = self.scale;
        let edge = |v: f32, limit: i64| ((v * s) as i64).clamp(0, limit);
        let (x0, y0, x1, y1) = (edge(x, pw), edge(y, ph), edge(x + w, pw), edge(y + h, ph));
        if x1 <= x0 || y1 <= y0 {
            return;
        }
        let whole = [(x0 as u32, y0 as u32, (x1 - x0) as u32, (y1 - y0) as u32)];
        let parts: Vec<PixelRect> = if self.clip.is_empty() {
            whole.to_vec()
        } else {
            self.clip
                .iter()
                .filter_map(|&(cx, cy, cw, ch)| {
                    let (lx, ly) = (i64::from(cx).max(x0), i64::from(cy).max(y0));
                    let (rx, ry) = ((i64::from(cx) + i64::from(cw)).min(x1), (i64::from(cy) + i64::from(ch)).min(y1));
                    (rx > lx && ry > ly).then(|| (lx as u32, ly as u32, (rx - lx) as u32, (ry - ly) as u32))
                })
                .collect()
        };
        let stride = pw as usize * 4;
        let inv = 255 - a;
        let data = self.pixmap.data_mut();
        // Parts of the clip may overlap: each row's spans are joined first,
        // so that no pixel is blended twice.
        let (top, bottom) = parts.iter().fold((u32::MAX, 0), |(t, b), p| (t.min(p.1), b.max(p.1 + p.3)));
        let mut spans: Vec<(u32, u32)> = Vec::with_capacity(parts.len());
        for row in top..bottom {
            spans.clear();
            spans.extend(parts.iter().filter(|p| row >= p.1 && row < p.1 + p.3).map(|p| (p.0, p.0 + p.2)));
            spans.sort_unstable();
            let mut joined: Vec<(u32, u32)> = Vec::with_capacity(spans.len());
            for &(from, to) in &spans {
                match joined.last_mut() {
                    Some(last) if from <= last.1 => last.1 = last.1.max(to),
                    _ => joined.push((from, to)),
                }
            }
            let row = row as usize;
            for (from, to) in joined {
                let line = &mut data[row * stride + from as usize * 4..row * stride + to as usize * 4];
                if a == 255 {
                    for pixel in line.chunks_exact_mut(4) {
                        pixel.copy_from_slice(&[r as u8, g as u8, b as u8, 255]);
                    }
                    continue;
                }
                for pixel in line.chunks_exact_mut(4) {
                    // tiny-skia's low-precision source-over: d * (255 - a),
                    // divided by 255 as (v + 255) >> 8.
                    pixel[0] = (r + ((u16::from(pixel[0]) * inv + 255) >> 8)) as u8;
                    pixel[1] = (g + ((u16::from(pixel[1]) * inv + 255) >> 8)) as u8;
                    pixel[2] = (b + ((u16::from(pixel[2]) * inv + 255) >> 8)) as u8;
                    pixel[3] = (a + ((u16::from(pixel[3]) * inv + 255) >> 8)) as u8;
                }
            }
        }
    }

    /// Whether a path, grown by `grow` logical pixels on every side, can
    /// reach the pixels this canvas holds.
    fn path_visible(&self, path: &Path, grow: f32) -> bool {
        let b = path.bounds();
        self.visible(b.x() - grow, b.y() - grow, b.width() + 2.0 * grow, b.height() + 2.0 * grow)
    }

    pub fn fill_path(&mut self, path: &Path, color: Rgba) {
        if !self.path_visible(path, 0.0) {
            return;
        }
        self.touch();
        let transform = self.transform();
        self.pixmap.fill_path(path, &Self::paint(color), FillRule::Winding, transform, None);
    }

    pub fn stroke_path(&mut self, path: &Path, width: f32, color: Rgba) {
        if !self.path_visible(path, width) {
            return;
        }
        self.touch();
        let stroke = Stroke {
            width,
            line_cap: LineCap::Round,
            line_join: LineJoin::Round,
            ..Stroke::default()
        };
        let transform = self.transform();
        self.pixmap.stroke_path(path, &Self::paint(color), &stroke, transform, None);
    }

    pub fn fill_rounded_rect(&mut self, x: f32, y: f32, w: f32, h: f32, radius: f32, color: Rgba) {
        self.fill_rounded_rect4(x, y, w, h, [radius; 4], color);
    }

    pub fn fill_rounded_rect4(&mut self, x: f32, y: f32, w: f32, h: f32, radii: Corners, color: Rgba) {
        if let Some(path) = rounded_rect_path(x, y, w, h, radii) {
            self.fill_path(&path, color);
        }
    }

    /// A stroke centred `width / 2` inside the rectangle's edge.
    pub fn stroke_rounded_rect(&mut self, x: f32, y: f32, w: f32, h: f32, radius: f32, width: f32, color: Rgba) {
        let inset = width / 2.0;
        if let Some(path) = rounded_rect_path(
            x + inset,
            y + inset,
            w - width,
            h - width,
            [(radius - inset).max(0.0); 4],
        ) {
            self.stroke_path(&path, width, color);
        }
    }

    /// A rounded rectangle filled with a linear gradient from `from` (at
    /// `start`) to `to` (at `end`), both in logical pixels.
    #[allow(clippy::too_many_arguments)]
    pub fn fill_gradient_rect(
        &mut self,
        x: f32,
        y: f32,
        w: f32,
        h: f32,
        radii: Corners,
        start: (f32, f32),
        end: (f32, f32),
        from: Rgba,
        to: Rgba,
    ) {
        let Some(path) = rounded_rect_path(x, y, w, h, radii) else {
            return;
        };
        if !self.path_visible(&path, 0.0) {
            return;
        }
        self.touch();
        let shader = LinearGradient::new(
            Point::from_xy(start.0, start.1),
            Point::from_xy(end.0, end.1),
            vec![GradientStop::new(0.0, from.into()), GradientStop::new(1.0, to.into())],
            SpreadMode::Pad,
            Transform::identity(),
        );
        let Some(shader) = shader else {
            return self.fill_path(&path, from);
        };
        let paint = Paint {
            shader,
            anti_alias: true,
            ..Paint::default()
        };
        let transform = self.transform();
        self.pixmap.fill_path(&path, &paint, FillRule::Winding, transform, None);
    }

    pub fn fill_circle(&mut self, cx: f32, cy: f32, radius: f32, color: Rgba) {
        if let Some(path) = PathBuilder::from_circle(cx, cy, radius) {
            self.fill_path(&path, color);
        }
    }

    pub fn stroke_circle(&mut self, cx: f32, cy: f32, radius: f32, width: f32, color: Rgba) {
        if let Some(path) = PathBuilder::from_circle(cx, cy, radius) {
            self.stroke_path(&path, width, color);
        }
    }

    /// A polyline with round caps and joins.
    pub fn polyline(&mut self, points: &[(f32, f32)], width: f32, color: Rgba) {
        let Some((first, rest)) = points.split_first() else {
            return;
        };
        let mut pb = PathBuilder::new();
        pb.move_to(first.0, first.1);
        for p in rest {
            pb.line_to(p.0, p.1);
        }
        if let Some(path) = pb.finish() {
            self.stroke_path(&path, width, color);
        }
    }

    pub fn line(&mut self, x0: f32, y0: f32, x1: f32, y1: f32, width: f32, color: Rgba) {
        self.polyline(&[(x0, y0), (x1, y1)], width, color);
    }

    /// A soft drop shadow under a rounded rectangle: `blur` is the spread in
    /// logical pixels, `offset_y` moves it down, `alpha` is its peak opacity.
    #[allow(clippy::too_many_arguments)]
    pub fn shadow(&mut self, x: f32, y: f32, w: f32, h: f32, radius: f32, blur: f32, offset_y: f32, alpha: f32) {
        if blur <= 0.0 || alpha <= 0.0 || w <= 0.0 || h <= 0.0 {
            return;
        }
        // The shadow is drawn once into a picture of its own, which is kept
        // and composed here: a panel is drawn again on every hover change,
        // and the shadow's layers cost more than everything else on it.
        // The picture is placed on whole pixels; the fraction of a pixel by
        // which the shadow is off the grid is part of the picture, so the
        // result is the same as drawing the layers here.
        let s = self.scale;
        let left = (x - blur) * s;
        let top = (y - blur + offset_y) * s;
        let (px, py) = (left.floor(), top.floor());
        let key = ShadowKey::new([w * s, h * s, radius * s, blur * s, alpha, left - px, top - py]);
        let Some(picture) = shadow_picture(key, || render_shadow(w, h, radius, blur, alpha, s, left - px, top - py))
        else {
            return;
        };
        let paint = PixmapPaint {
            quality: FilterQuality::Nearest,
            ..PixmapPaint::default()
        };
        self.draw_device_pixmap(px as i32, py as i32, (*picture).as_ref(), &paint);
    }

    /// Draws `source` scaled into the logical rectangle (bilinear filtering).
    pub fn draw_pixmap(&mut self, source: &Pixmap, x: f32, y: f32, w: f32, h: f32, opacity: f32) {
        if source.width() == 0 || source.height() == 0 || w <= 0.0 || h <= 0.0 {
            return;
        }
        if !self.visible(x, y, w, h) {
            return;
        }
        self.touch();
        let sx = w * self.scale / source.width() as f32;
        let sy = h * self.scale / source.height() as f32;
        let exact = (sx - 1.0).abs() < 1e-3 && (sy - 1.0).abs() < 1e-3;
        let paint = PixmapPaint {
            opacity: opacity.clamp(0.0, 1.0),
            quality: if exact { FilterQuality::Nearest } else { FilterQuality::Bilinear },
            ..PixmapPaint::default()
        };
        let transform = Transform::from_scale(sx, sy).post_translate(x * self.scale, y * self.scale);
        self.pixmap.draw_pixmap(0, 0, source.as_ref(), &paint, transform, None);
    }

    /// Fills a rounded rectangle with a picture: `placement` maps the
    /// picture's pixels to logical pixels of this canvas. What lies outside
    /// the picture repeats its edge.
    #[allow(clippy::too_many_arguments)]
    pub fn fill_rounded_rect_pixmap(&mut self, x: f32, y: f32, w: f32, h: f32, radii: Corners, source: &Pixmap, placement: Transform) {
        let Some(path) = rounded_rect_path(x, y, w, h, radii) else {
            return;
        };
        if !self.path_visible(&path, 0.0) {
            return;
        }
        self.touch();
        let paint = Paint {
            shader: tiny_skia::Pattern::new(source.as_ref(), SpreadMode::Pad, FilterQuality::Bilinear, 1.0, placement),
            anti_alias: true,
            ..Paint::default()
        };
        let transform = self.transform();
        self.pixmap.fill_path(&path, &paint, FillRule::Winding, transform, None);
    }

    /// Blends one premultiplied source pixel over the canvas (device
    /// pixels). Coordinates outside the canvas, or outside the clip while
    /// there is one, are ignored.
    #[inline]
    pub fn blend_pixel(&mut self, px: i32, py: i32, color: Rgba, coverage: u8) {
        let (w, h) = (self.pixmap.width() as i32, self.pixmap.height() as i32);
        if px < 0 || py < 0 || px >= w || py >= h || coverage == 0 {
            return;
        }
        if !self.clip.is_empty() {
            let (px, py) = (px as u32, py as u32);
            if !self.clip.iter().any(|&(cx, cy, cw, ch)| px >= cx && py >= cy && px < cx + cw && py < cy + ch) {
                return;
            }
        }
        let a = u32::from(color.a) * u32::from(coverage) / 255;
        if a == 0 {
            return;
        }
        if self.stale || self.blank {
            self.touch();
        }
        let i = (py as usize * w as usize + px as usize) * 4;
        let dst = &mut self.pixmap.data_mut()[i..i + 4];
        let inv = 255 - a;
        let blend = |src: u8, dst: u8| ((u32::from(src) * a + u32::from(dst) * inv + 127) / 255) as u8;
        dst[0] = blend(color.r, dst[0]);
        dst[1] = blend(color.g, dst[1]);
        dst[2] = blend(color.b, dst[2]);
        dst[3] = (a + u32::from(dst[3]) * inv / 255).min(255) as u8;
    }
}

/// A rounded rectangle path; radii are clamped to half the shorter side.
pub fn rounded_rect_path(x: f32, y: f32, w: f32, h: f32, radii: Corners) -> Option<Path> {
    if !(w > 0.0 && h > 0.0) {
        return None;
    }
    let limit = w.min(h) / 2.0;
    let [tl, tr, br, bl] = radii.map(|r| r.clamp(0.0, limit));
    // Control-point distance for a quarter circle drawn as a cubic.
    const K: f32 = 0.552_284_8;
    let mut pb = PathBuilder::new();
    pb.move_to(x + tl, y);
    pb.line_to(x + w - tr, y);
    if tr > 0.0 {
        pb.cubic_to(x + w - tr + tr * K, y, x + w, y + tr - tr * K, x + w, y + tr);
    }
    pb.line_to(x + w, y + h - br);
    if br > 0.0 {
        pb.cubic_to(x + w, y + h - br + br * K, x + w - br + br * K, y + h, x + w - br, y + h);
    }
    pb.line_to(x + bl, y + h);
    if bl > 0.0 {
        pb.cubic_to(x + bl - bl * K, y + h, x, y + h - bl + bl * K, x, y + h - bl);
    }
    pb.line_to(x, y + tl);
    if tl > 0.0 {
        pb.cubic_to(x, y + tl - tl * K, x + tl - tl * K, y, x + tl, y);
    }
    pb.close();
    pb.finish()
}

/// What a shadow picture is drawn from: sizes in device pixels, the
/// opacity, and the fraction of a pixel the shadow is off the pixel grid.
/// The values are rounded so that equal shadows share a picture.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
struct ShadowKey([i32; 7]);

impl ShadowKey {
    fn new(values: [f32; 7]) -> Self {
        Self(values.map(|v| (v * 64.0).round() as i32))
    }
}

/// Bytes of shadow pictures kept at most; the oldest go first.
const SHADOW_CACHE_BYTES: usize = 48 * 1024 * 1024;

thread_local! {
    static SHADOWS: std::cell::RefCell<Vec<(ShadowKey, std::rc::Rc<Pixmap>)>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// The shadow picture for `key`, drawn with `render` when there is none yet.
fn shadow_picture(key: ShadowKey, render: impl FnOnce() -> Option<Pixmap>) -> Option<std::rc::Rc<Pixmap>> {
    SHADOWS.with(|cell| {
        let mut cache = cell.borrow_mut();
        if let Some(index) = cache.iter().position(|(k, _)| *k == key) {
            // Most recently used last.
            let entry = cache.remove(index);
            let picture = entry.1.clone();
            cache.push(entry);
            return Some(picture);
        }
        let picture = std::rc::Rc::new(render()?);
        cache.push((key, picture.clone()));
        let mut total: usize = cache.iter().map(|(_, p)| p.data().len()).sum();
        while total > SHADOW_CACHE_BYTES && cache.len() > 1 {
            total -= cache.remove(0).1.data().len();
        }
        Some(picture)
    })
}

/// Draws a shadow into a picture of its own (see [`render_shadow_layers`]).
///
/// Away from its corners a shadow is the same in every column (and every
/// row): there each of its layers covers the whole width (height) and only
/// the distance to the top and bottom (left and right) edges matters. So
/// the shadow of a smaller rectangle with the same corners is drawn, and
/// its middle column and row are repeated to the full size: the result is
/// the same picture, for a fraction of the cost of drawing every layer over
/// the whole area (Start at 200 % is millions of pixels, and a window's
/// shadow is drawn again at every size while it is resized).
#[allow(clippy::too_many_arguments)]
fn render_shadow(w: f32, h: f32, radius: f32, blur: f32, alpha: f32, scale: f32, fx: f32, fy: f32) -> Option<Pixmap> {
    // The corners and some straight edge on each side of the middle.
    let keep = 2.0 * radius + 16.0 / scale;
    let cut = |side: f32| if side > keep + 32.0 / scale { ((side - keep) * scale).floor() } else { 0.0 };
    let (cut_x, cut_y) = (cut(w), cut(h));
    if cut_x == 0.0 && cut_y == 0.0 {
        return render_shadow_layers(w, h, radius, blur, alpha, scale, fx, fy);
    }
    let (sw, sh) = (w - cut_x / scale, h - cut_y / scale);
    let small = render_shadow_layers(sw, sh, radius, blur, alpha, scale, fx, fy)?;
    // The middle column and row of the smaller rectangle.
    let mid_x = ((fx + (blur + sw / 2.0) * scale).floor() as usize).min(small.width() as usize - 1);
    let mid_y = ((fy + (blur + sh / 2.0) * scale).floor() as usize).min(small.height() as usize - 1);
    stretch_middle(&small, mid_x, cut_x as usize, mid_y, cut_y as usize)
}

/// `picture` with its column `mid_x` repeated `extra_x` more times and its
/// row `mid_y` repeated `extra_y` more times.
fn stretch_middle(picture: &Pixmap, mid_x: usize, extra_x: usize, mid_y: usize, extra_y: usize) -> Option<Pixmap> {
    let (sw, sh) = (picture.width() as usize, picture.height() as usize);
    let (w, h) = (sw + extra_x, sh + extra_y);
    let mut out = Pixmap::new(u32::try_from(w).ok()?, u32::try_from(h).ok()?)?;
    let source = picture.data();
    let target = out.data_mut();
    for y in 0..h {
        let sy = if y < mid_y {
            y
        } else if y <= mid_y + extra_y {
            mid_y
        } else {
            y - extra_y
        };
        let from = &source[sy * sw * 4..(sy + 1) * sw * 4];
        let row = &mut target[y * w * 4..(y + 1) * w * 4];
        row[..mid_x * 4].copy_from_slice(&from[..mid_x * 4]);
        let middle = &from[mid_x * 4..mid_x * 4 + 4];
        for pixel in row[mid_x * 4..(mid_x + extra_x) * 4].chunks_exact_mut(4) {
            pixel.copy_from_slice(middle);
        }
        row[(mid_x + extra_x) * 4..].copy_from_slice(&from[mid_x * 4..]);
    }
    Some(out)
}

/// Draws a shadow into a picture of its own: stacked, growing rounded
/// rectangles approximate a Gaussian falloff without a blur pass. The
/// shadow of a `w` x `h` (logical) rectangle with corner `radius` starts
/// `fx`, `fy` device pixels into the picture.
#[allow(clippy::too_many_arguments)]
fn render_shadow_layers(w: f32, h: f32, radius: f32, blur: f32, alpha: f32, scale: f32, fx: f32, fy: f32) -> Option<Pixmap> {
    let width = (w + 2.0 * blur) + (fx + 1.0) / scale;
    let height = (h + 2.0 * blur) + (fy + 1.0) / scale;
    let mut canvas = Canvas::new(width, height, scale)?;
    let (ox, oy) = (blur + fx / scale, blur + fy / scale);
    let steps = (blur / 1.5).clamp(6.0, 24.0) as u32;
    let layer = alpha / steps as f32 * 1.6;
    for i in 0..steps {
        let t = (i as f32 + 0.5) / steps as f32;
        let grow = blur * t;
        let opacity = layer * (1.0 - t) * (1.0 - t) * 1.5;
        canvas.fill_rounded_rect(
            ox - grow,
            oy - grow,
            w + 2.0 * grow,
            h + 2.0 * grow,
            radius + grow,
            Rgba::BLACK.alpha(opacity),
        );
    }
    Some(canvas.into_pixmap())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pixel(c: &Canvas, x: u32, y: u32) -> [u8; 4] {
        let p = c.pixmap().pixel(x, y).unwrap();
        [p.red(), p.green(), p.blue(), p.alpha()]
    }

    /// A canvas whose pixels are all different (premultiplied).
    fn busy(w: f32, h: f32, scale: f32) -> Canvas {
        let mut c = Canvas::new(w, h, scale).unwrap();
        for (i, p) in c.pixmap_mut().data_mut().chunks_exact_mut(4).enumerate() {
            let a = (i * 37 % 256) as u8;
            let v = |k: usize| ((i * k % 256) as u16 * u16::from(a) / 255) as u8;
            p.copy_from_slice(&[v(7), v(13), v(29), a]);
        }
        c
    }

    #[test]
    fn rectangles_on_the_pixel_grid_are_filled_as_tiny_skia_fills_them() {
        for scale in [1.0, 2.0, 3.0] {
            for color in [Rgba::BLACK.alpha(0.22), Rgba::WHITE.alpha(0.72), Rgba::rgba(200, 30, 90, 255), Rgba::rgba(12, 240, 77, 1), Rgba::rgba(0, 0, 0, 0)] {
                for (x, y, w, h) in [(0.0, 0.0, 64.0, 48.0), (3.0, 5.0, 20.0, 11.0), (-4.0, 40.0, 30.0, 30.0), (60.0, -2.0, 9.0, 9.0)] {
                    let mut ours = busy(64.0, 48.0, scale);
                    ours.fill_rect(x, y, w, h, color);
                    let mut theirs = busy(64.0, 48.0, scale);
                    let mut paint = Canvas::paint(color);
                    paint.anti_alias = false;
                    let transform = theirs.transform();
                    theirs.pixmap.fill_rect(Rect::from_xywh(x, y, w, h).unwrap(), &paint, transform, None);
                    if let Some(i) = ours.data().chunks_exact(4).zip(theirs.data().chunks_exact(4)).position(|(a, b)| a != b) {
                        let wpx = ours.pixel_size().0 as usize;
                        panic!("{color:?} at {scale}: ({x}, {y}, {w}, {h}): pixel ({}, {}) {:?} vs {:?}", i % wpx, i / wpx, &ours.data()[i * 4..i * 4 + 4], &theirs.data()[i * 4..i * 4 + 4]);
                    }
                }
            }
        }
    }

    #[test]
    fn a_picture_onto_an_empty_canvas_is_the_picture_blended_over_nothing() {
        let picture = busy(9.0, 7.0, 1.0).into_pixmap();
        let paint = PixmapPaint { quality: FilterQuality::Nearest, ..PixmapPaint::default() };
        for (x, y) in [(0, 0), (3, 2), (-4, -3), (10, 8), (-20, 0), (-9, 2), (-8, -6), (5, -7), (4, 4), (11, 9), (0, -1), (-1, 0)] {
            let mut ours = Canvas::new(12.0, 10.0, 1.0).unwrap();
            ours.clear(Rgba::WHITE);
            ours.reset();
            ours.draw_device_pixmap(x, y, picture.as_ref(), &paint);
            let mut theirs = Pixmap::new(12, 10).unwrap();
            theirs.draw_pixmap(x, y, picture.as_ref(), &paint, Transform::identity(), None);
            assert_eq!(ours.data(), theirs.data(), "at ({x}, {y})");
        }
    }

    #[test]
    fn every_pixel_converts_to_argb_in_any_rectangle() {
        let c = busy(13.0, 7.0, 1.0);
        for rect in [(0, 0, 13, 7), (1, 2, 5, 3), (4, 0, 2, 7), (12, 6, 1, 1), (3, 3, 0, 2)] {
            let mut out = vec![9u8; 13 * 7 * 4];
            c.write_argb8888_rect(&mut out, rect);
            for (i, (dst, src)) in out.chunks_exact(4).zip(c.data().chunks_exact(4)).enumerate() {
                let (x, y) = (i as u32 % 13, i as u32 / 13);
                let inside = x >= rect.0 && y >= rect.1 && x < rect.0 + rect.2 && y < rect.1 + rect.3;
                let want = if inside { [src[2], src[1], src[0], src[3]] } else { [9; 4] };
                assert_eq!(dst, want, "{rect:?} at ({x}, {y})");
            }
        }
    }

    #[test]
    fn a_rectangle_drawn_in_parts_is_the_whole_rectangle_there() {
        let color = Rgba::BLACK.alpha(0.22);
        let mut part = busy(100.0, 80.0, 2.0);
        let parts = [(10, 12, 30, 7), (150, 100, 50, 60), (30, 15, 20, 10)];
        let before = part.data().to_vec();
        part.begin_clip(&parts);
        part.fill_rect(0.0, 0.0, 100.0, 80.0, color);
        part.end_clip();
        let width = 200usize;
        for (i, (got, was)) in part.data().chunks_exact(4).zip(before.chunks_exact(4)).enumerate() {
            let (x, y) = ((i % width) as u32, (i / width) as u32);
            let inside = parts.iter().any(|&(px, py, pw, ph)| x >= px && y >= py && x < px + pw && y < py + ph);
            if inside {
                // Within a part: the fill over a cleared pixel, as begin_clip
                // clears the parts.
                let mut cleared = Canvas::new(1.0, 1.0, 1.0).unwrap();
                cleared.fill_rect(0.0, 0.0, 1.0, 1.0, color);
                assert_eq!(got, cleared.data(), "({x}, {y})");
            } else {
                assert_eq!(got, was, "({x}, {y}) outside the parts is left alone");
            }
        }
    }

    #[test]
    fn canvas_rejects_empty_and_huge_sizes() {
        assert!(Canvas::new(0.0, 10.0, 1.0).is_none());
        assert!(Canvas::new(10.0, -1.0, 1.0).is_none());
        assert!(Canvas::new(1e9, 10.0, 1.0).is_none());
        assert!(Canvas::new(f32::NAN, 10.0, 1.0).is_none());
        let c = Canvas::new(10.0, 10.0, f32::NAN).unwrap();
        assert_eq!(c.pixel_size(), (10, 10));
    }

    #[test]
    fn scale_multiplies_the_backing_store() {
        let c = Canvas::new(100.0, 50.0, 1.5).unwrap();
        assert_eq!(c.pixel_size(), (150, 75));
        assert_eq!((c.width(), c.height()), (100.0, 50.0));
    }

    #[test]
    fn rounded_rect_leaves_the_corner_transparent() {
        let mut c = Canvas::new(40.0, 40.0, 1.0).unwrap();
        c.fill_rounded_rect(0.0, 0.0, 40.0, 40.0, 12.0, Rgba::rgb(255, 0, 0));
        assert_eq!(pixel(&c, 0, 0)[3], 0, "corner pixel is outside the rounded shape");
        assert_eq!(pixel(&c, 20, 20), [255, 0, 0, 255]);
        assert_eq!(pixel(&c, 20, 0), [255, 0, 0, 255]);
    }

    #[test]
    fn argb8888_swaps_red_and_blue() {
        let mut c = Canvas::new(1.0, 1.0, 1.0).unwrap();
        c.clear(Rgba::rgb(10, 20, 30));
        let mut out = [0u8; 4];
        c.write_argb8888(&mut out);
        assert_eq!(out, [30, 20, 10, 255]);
    }

    #[test]
    fn blend_pixel_is_bounds_checked_and_premultiplied() {
        let mut c = Canvas::new(2.0, 2.0, 1.0).unwrap();
        c.blend_pixel(-1, 0, Rgba::WHITE, 255);
        c.blend_pixel(2, 2, Rgba::WHITE, 255);
        c.blend_pixel(1, 1, Rgba::WHITE, 128);
        let [r, g, b, a] = pixel(&c, 1, 1);
        assert_eq!((r, g, b, a), (128, 128, 128, 128), "half-covered white, stored premultiplied");
        assert_eq!(pixel(&c, 0, 0)[3], 0);
    }

    /// The shadow as it was drawn before it was cached: the layers straight
    /// onto the canvas.
    fn shadow_drawn_directly(c: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radius: f32, blur: f32, offset_y: f32, alpha: f32) {
        let steps = (blur / 1.5).clamp(6.0, 24.0) as u32;
        let layer = alpha / steps as f32 * 1.6;
        for i in 0..steps {
            let t = (i as f32 + 0.5) / steps as f32;
            let grow = blur * t;
            let opacity = layer * (1.0 - t) * (1.0 - t) * 1.5;
            c.fill_rounded_rect(x - grow, y - grow + offset_y, w + 2.0 * grow, h + 2.0 * grow, radius + grow, Rgba::BLACK.alpha(opacity));
        }
    }

    #[test]
    fn a_cached_shadow_looks_like_one_drawn_directly() {
        // On whole and on fractional positions, at scale 1 and 1.5, on an
        // empty canvas and over something already drawn.
        for (x, y, scale) in [(40.0, 30.0, 1.0), (40.25, 30.5, 1.0), (40.0, 30.0, 1.5), (13.3, 21.7, 1.25)] {
            for background in [None, Some(Rgba::WHITE)] {
                let mut cached = Canvas::new(160.0, 140.0, scale).unwrap();
                let mut direct = Canvas::new(160.0, 140.0, scale).unwrap();
                if let Some(colour) = background {
                    cached.clear(colour);
                    direct.clear(colour);
                }
                cached.shadow(x, y, 70.0, 50.0, 8.0, 28.0, 12.0, 0.4);
                shadow_drawn_directly(&mut direct, x, y, 70.0, 50.0, 8.0, 28.0, 12.0, 0.4);
                let worst = cached
                    .data()
                    .iter()
                    .zip(direct.data())
                    .map(|(a, b)| a.abs_diff(*b))
                    .max()
                    .unwrap();
                assert!(worst <= 2, "at ({x}, {y}) scale {scale}: differs by {worst}");
            }
        }
    }

    #[test]
    fn the_same_shadow_is_drawn_once() {
        let key = ShadowKey::new([10.0, 10.0, 2.0, 4.0, 0.5, 0.0, 0.0]);
        let mut renders = 0;
        for _ in 0..3 {
            let picture = shadow_picture(key, || {
                renders += 1;
                Pixmap::new(4, 4)
            });
            assert!(picture.is_some());
        }
        assert_eq!(renders, 1);
    }

    #[test]
    fn a_large_shadow_is_its_small_shadow_stretched() {
        // The shadow of a large panel, drawn layer by layer over its whole
        // area and drawn small and stretched: the same picture, at whole
        // and fractional positions and scales.
        for (w, h, radius, blur, scale, fx, fy) in [
            (832.0, 918.0, 8.0, 28.0, 1.0, 0.0, 0.0),
            (856.0, 600.0, 8.0, 28.0, 2.0, 0.0, 0.5),
            (420.25, 300.5, 8.0, 44.0, 1.25, 0.75, 0.25),
            (300.0, 90.0, 12.0, 16.0, 1.5, 0.5, 0.0),
        ] {
            let stretched = render_shadow(w, h, radius, blur, 0.4, scale, fx, fy).unwrap();
            let layers = render_shadow_layers(w, h, radius, blur, 0.4, scale, fx, fy).unwrap();
            assert!(stretched.width().abs_diff(layers.width()) <= 1 && stretched.height().abs_diff(layers.height()) <= 1);
            let (cw, ch) = (stretched.width().min(layers.width()), stretched.height().min(layers.height()));
            let mut worst = 0;
            for y in 0..ch {
                for x in 0..cw {
                    let (a, b) = (stretched.pixel(x, y).unwrap(), layers.pixel(x, y).unwrap());
                    worst = worst.max(a.alpha().abs_diff(b.alpha()));
                }
            }
            // As close as a kept shadow is to one drawn in place.
            assert!(worst <= 2, "{w}x{h} at {scale}: differs by {worst}");
        }
    }

    #[test]
    fn a_part_drawn_again_is_exactly_the_whole_drawing_there() {
        // A drawing of shapes, a stroke, a gradient, a picture, text-like
        // pixels and a shadow. Over a different drawing, a part of it is
        // drawn again: inside the part the pixels are exactly those of the
        // whole drawing.
        let mut picture = Pixmap::new(7, 5).unwrap();
        picture.fill(tiny_skia::Color::from_rgba8(30, 60, 90, 200));
        let draw = |c: &mut Canvas, shift: f32| {
            c.clear(Rgba::rgb(20, 30, 40));
            c.shadow(20.0 + shift, 18.0, 90.0, 60.0, 8.0, 16.0, 6.0, 0.4);
            c.fill_rounded_rect(20.5, 18.25 + shift, 90.0, 60.0, 8.0, Rgba::rgb(200, 100, 50).alpha(0.8));
            c.stroke_rounded_rect(30.0, 30.0, 40.0, 20.0, 4.0, 1.0, Rgba::WHITE);
            c.fill_gradient_rect(60.0, 50.0, 50.0, 30.0, [3.0; 4], (60.0, 50.0), (110.0, 80.0), Rgba::BLACK, Rgba::WHITE);
            c.draw_pixmap(&picture, 12.3, 70.6, 21.0, 15.0, 0.9);
            c.fill_rect(100.0, 5.0, 13.0, 9.0, Rgba::rgb(0, 200, 0));
            c.line(5.0, 95.0, 115.0 - shift, 60.0, 2.0, Rgba::rgb(250, 250, 0));
            for i in 0..40 {
                c.blend_pixel(50 + i, 12, Rgba::WHITE, (i * 6) as u8);
            }
        };
        for scale in [1.0, 1.25, 2.0] {
            let mut whole = Canvas::new(120.0, 100.0, scale).unwrap();
            draw(&mut whole, 0.0);
            let (w, h) = whole.pixel_size();
            let parts: [&[PixelRect]; 4] = [&[(0, 0, 40, 30)], &[(33, 41, 57, 23), (2, 90, 10, 10)], &[(70, 10, 400, 400)], &[(101, 77, 3, 2)]];
            for rects in parts {
                let mut part = Canvas::new(120.0, 100.0, scale).unwrap();
                draw(&mut part, 7.0);
                let clip = part.begin_clip(rects);
                assert_eq!(clip.len(), rects.len());
                assert!(part.is_blank());
                draw(&mut part, 0.0);
                part.end_clip();
                for rect in clip {
                    for y in rect.1..rect.1 + rect.3 {
                        for x in rect.0..rect.0 + rect.2 {
                            let i = ((y * w + x) * 4) as usize;
                            assert_eq!(&whole.data()[i..i + 4], &part.data()[i..i + 4], "scale {scale}, {rect:?} at {x},{y}");
                        }
                    }
                }
            }
            assert!(h > 0);
        }
        let mut c = Canvas::new(10.0, 10.0, 1.0).unwrap();
        assert!(c.begin_clip(&[(20, 20, 5, 5)]).is_empty(), "outside the canvas");
        assert!(c.clip().is_empty());
    }

    #[test]
    fn a_reset_canvas_draws_anew_and_a_full_copy_needs_no_clearing() {
        let mut c = Canvas::new(8.0, 4.0, 1.0).unwrap();
        c.clear(Rgba::WHITE);
        c.reset();
        assert!(c.is_blank());
        c.fill_rect(0.0, 0.0, 1.0, 1.0, Rgba::rgb(255, 0, 0));
        assert_eq!(pixel(&c, 0, 0), [255, 0, 0, 255]);
        assert_eq!(pixel(&c, 5, 2)[3], 0, "the earlier drawing is gone");
        let mut whole = Pixmap::new(8, 4).unwrap();
        whole.fill(tiny_skia::Color::from_rgba8(1, 2, 3, 255));
        c.reset();
        c.copy_from_full(&whole);
        assert!(!c.is_blank());
        assert_eq!(c.data(), whole.data());
        let mut out = vec![0u8; 8 * 4 * 4];
        c.write_argb8888_rect(&mut out, (2, 1, 3, 2));
        assert_eq!(&out[(8 + 2) * 4..(8 + 2) * 4 + 4], [3, 2, 1, 255]);
        assert_eq!(&out[..4], [0, 0, 0, 0], "outside the rectangle nothing is written");
    }

    #[test]
    fn shadow_fades_outwards() {
        let mut c = Canvas::new(120.0, 120.0, 1.0).unwrap();
        c.shadow(40.0, 40.0, 40.0, 40.0, 8.0, 24.0, 0.0, 0.5);
        let centre = pixel(&c, 60, 60)[3];
        let near = pixel(&c, 34, 60)[3];
        let far = pixel(&c, 18, 60)[3];
        assert!(centre > near && near > far, "{centre} {near} {far}");
        assert_eq!(pixel(&c, 2, 2)[3], 0);
    }
}
