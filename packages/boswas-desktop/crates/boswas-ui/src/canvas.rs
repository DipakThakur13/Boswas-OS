//! Software drawing for Boswas UI surfaces.
//!
//! A [`Canvas`] is a premultiplied RGBA pixmap addressed in logical pixels;
//! the output scale is applied when drawing, so callers lay out in logical
//! units and still get crisp edges on HiDPI outputs.

use tiny_skia::{
    FillRule, FilterQuality, GradientStop, LineCap, LineJoin, LinearGradient, Paint, Path, PathBuilder, Pixmap,
    PixmapPaint, Point, Rect, SpreadMode, Stroke, Transform,
};

use crate::tokens::Rgba;

/// Corner radii in the order top-left, top-right, bottom-right, bottom-left.
pub type Corners = [f32; 4];

/// A drawing surface in logical pixels.
pub struct Canvas {
    pixmap: Pixmap,
    width: f32,
    height: f32,
    scale: f32,
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

    pub fn pixmap(&self) -> &Pixmap {
        &self.pixmap
    }

    pub fn pixmap_mut(&mut self) -> &mut Pixmap {
        &mut self.pixmap
    }

    pub fn into_pixmap(self) -> Pixmap {
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

    fn transform(&self) -> Transform {
        Transform::from_scale(self.scale, self.scale)
    }

    fn paint(color: Rgba) -> Paint<'static> {
        let mut paint = Paint::default();
        paint.set_color(color.into());
        paint.anti_alias = true;
        paint
    }

    /// Fills the whole canvas, replacing what was there.
    pub fn clear(&mut self, color: Rgba) {
        self.pixmap.fill(color.into());
    }

    pub fn fill_rect(&mut self, x: f32, y: f32, w: f32, h: f32, color: Rgba) {
        if let Some(rect) = Rect::from_xywh(x, y, w, h) {
            let mut paint = Self::paint(color);
            // Axis-aligned rectangles on the pixel grid need no anti-aliasing.
            paint.anti_alias = self.scale.fract() != 0.0 || [x, y, w, h].iter().any(|v| v.fract() != 0.0);
            self.pixmap.fill_rect(rect, &paint, self.transform(), None);
        }
    }

    pub fn fill_path(&mut self, path: &Path, color: Rgba) {
        self.pixmap
            .fill_path(path, &Self::paint(color), FillRule::Winding, self.transform(), None);
    }

    pub fn stroke_path(&mut self, path: &Path, width: f32, color: Rgba) {
        let stroke = Stroke {
            width,
            line_cap: LineCap::Round,
            line_join: LineJoin::Round,
            ..Stroke::default()
        };
        self.pixmap
            .stroke_path(path, &Self::paint(color), &stroke, self.transform(), None);
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
        self.pixmap
            .fill_path(&path, &paint, FillRule::Winding, self.transform(), None);
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
        if blur <= 0.0 || alpha <= 0.0 {
            return;
        }
        // Stacked, growing rounded rectangles approximate a Gaussian falloff
        // without a blur pass; shadows are rendered once and cached.
        let steps = (blur / 1.5).clamp(6.0, 24.0) as u32;
        let layer = alpha / steps as f32 * 1.6;
        for i in 0..steps {
            let t = (i as f32 + 0.5) / steps as f32;
            let grow = blur * t;
            let opacity = layer * (1.0 - t) * (1.0 - t) * 1.5;
            self.fill_rounded_rect(
                x - grow,
                y - grow + offset_y,
                w + 2.0 * grow,
                h + 2.0 * grow,
                radius + grow,
                Rgba::BLACK.alpha(opacity),
            );
        }
    }

    /// Draws `source` scaled into the logical rectangle (bilinear filtering).
    pub fn draw_pixmap(&mut self, source: &Pixmap, x: f32, y: f32, w: f32, h: f32, opacity: f32) {
        if source.width() == 0 || source.height() == 0 || w <= 0.0 || h <= 0.0 {
            return;
        }
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

    /// Blends one premultiplied source pixel over the canvas (device pixels).
    /// Out-of-bounds coordinates are ignored.
    #[inline]
    pub fn blend_pixel(&mut self, px: i32, py: i32, color: Rgba, coverage: u8) {
        let (w, h) = (self.pixmap.width() as i32, self.pixmap.height() as i32);
        if px < 0 || py < 0 || px >= w || py >= h || coverage == 0 {
            return;
        }
        let a = u32::from(color.a) * u32::from(coverage) / 255;
        if a == 0 {
            return;
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

#[cfg(test)]
mod tests {
    use super::*;

    fn pixel(c: &Canvas, x: u32, y: u32) -> [u8; 4] {
        let p = c.pixmap().pixel(x, y).unwrap();
        [p.red(), p.green(), p.blue(), p.alpha()]
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
