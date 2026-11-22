//! Software drawing on an XRGB8888 canvas.
//!
//! Small and self-contained on purpose: solid fills, premultiplied image and
//! coverage-mask blits at an opacity, and shapes drawn from signed distance
//! functions (anti-aliased rounded rectangles and discs). Filled outlines
//! (the mark, glyphs) are rasterised by [`crate::raster`]. Every operation
//! takes a clip rectangle, which is how the scene redraws only the part of
//! the screen that changed.

/// An opaque sRGB colour.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rgb {
    pub r: u8,
    pub g: u8,
    pub b: u8,
}

impl Rgb {
    pub const fn new(r: u8, g: u8, b: u8) -> Self {
        Self { r, g, b }
    }

    /// The pixel value in an XRGB8888 buffer.
    pub const fn xrgb(self) -> u32 {
        ((self.r as u32) << 16) | ((self.g as u32) << 8) | self.b as u32
    }

    pub const fn from_xrgb(px: u32) -> Self {
        Self { r: (px >> 16) as u8, g: (px >> 8) as u8, b: px as u8 }
    }

    /// Linear mix towards `other`; `t` is clamped to 0..=1.
    pub fn mix(self, other: Rgb, t: f32) -> Rgb {
        let t = t.clamp(0.0, 1.0);
        let ch = |a: u8, b: u8| (a as f32 + (b as f32 - a as f32) * t).round() as u8;
        Rgb::new(ch(self.r, other.r), ch(self.g, other.g), ch(self.b, other.b))
    }
}

/// An axis-aligned pixel rectangle. Empty when `w` or `h` is not positive.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

impl Rect {
    pub const fn new(x: i32, y: i32, w: i32, h: i32) -> Self {
        Self { x, y, w, h }
    }

    pub const fn is_empty(&self) -> bool {
        self.w <= 0 || self.h <= 0
    }

    pub const fn right(&self) -> i32 {
        self.x + self.w
    }

    pub const fn bottom(&self) -> i32 {
        self.y + self.h
    }

    pub fn area(&self) -> i64 {
        if self.is_empty() {
            0
        } else {
            self.w as i64 * self.h as i64
        }
    }

    pub fn intersect(&self, other: &Rect) -> Rect {
        let x = self.x.max(other.x);
        let y = self.y.max(other.y);
        let r = self.right().min(other.right());
        let b = self.bottom().min(other.bottom());
        if r <= x || b <= y {
            Rect::default()
        } else {
            Rect::new(x, y, r - x, b - y)
        }
    }

    pub fn intersects(&self, other: &Rect) -> bool {
        !self.intersect(other).is_empty()
    }

    /// The smallest rectangle covering both; an empty operand is ignored.
    pub fn union(&self, other: &Rect) -> Rect {
        if self.is_empty() {
            return *other;
        }
        if other.is_empty() {
            return *self;
        }
        let x = self.x.min(other.x);
        let y = self.y.min(other.y);
        let r = self.right().max(other.right());
        let b = self.bottom().max(other.bottom());
        Rect::new(x, y, r - x, b - y)
    }

    pub fn contains(&self, other: &Rect) -> bool {
        other.is_empty()
            || (other.x >= self.x
                && other.y >= self.y
                && other.right() <= self.right()
                && other.bottom() <= self.bottom())
    }
}

/// Merge rectangles that overlap or touch, so each pixel is flushed once.
pub fn merge_rects(rects: &mut Vec<Rect>) {
    rects.retain(|r| !r.is_empty());
    let mut merged = true;
    while merged {
        merged = false;
        'outer: for i in 0..rects.len() {
            for j in (i + 1)..rects.len() {
                let (a, b) = (rects[i], rects[j]);
                let touching = a.x <= b.right() && b.x <= a.right() && a.y <= b.bottom() && b.y <= a.bottom();
                if touching {
                    rects[i] = a.union(&b);
                    rects.swap_remove(j);
                    merged = true;
                    break 'outer;
                }
            }
        }
    }
}

/// A premultiplied RGBA8 image.
#[derive(Clone, Debug, Default)]
pub struct Image {
    pub width: u32,
    pub height: u32,
    pub data: Vec<u8>,
}

/// An 8-bit coverage mask, tinted when drawn.
#[derive(Clone, Debug)]
pub struct Mask {
    pub width: u32,
    pub height: u32,
    pub data: Vec<u8>,
}

/// The picture being composed: `width * height` XRGB8888 pixels.
pub struct Canvas {
    width: u32,
    height: u32,
    pixels: Vec<u32>,
}

#[inline]
fn blend_channel(dst: u32, src: u32, a: u32) -> u32 {
    (src * a + dst * (255 - a) + 127) / 255
}

/// An opacity (0..=1) as an 8-bit alpha.
#[inline]
pub fn alpha(opacity: f32) -> u32 {
    if opacity.is_nan() {
        0
    } else {
        (opacity.clamp(0.0, 1.0) * 255.0 + 0.5) as u32
    }
}

#[inline]
fn blend(dst: u32, src: Rgb, a: u32) -> u32 {
    if a >= 255 {
        return src.xrgb();
    }
    let r = blend_channel((dst >> 16) & 0xff, src.r as u32, a);
    let g = blend_channel((dst >> 8) & 0xff, src.g as u32, a);
    let b = blend_channel(dst & 0xff, src.b as u32, a);
    (r << 16) | (g << 8) | b
}

impl Canvas {
    pub fn new(width: u32, height: u32) -> Self {
        Self { width, height, pixels: vec![0; width as usize * height as usize] }
    }

    pub fn width(&self) -> u32 {
        self.width
    }

    pub fn height(&self) -> u32 {
        self.height
    }

    pub fn bounds(&self) -> Rect {
        Rect::new(0, 0, self.width as i32, self.height as i32)
    }

    pub fn pixels(&self) -> &[u32] {
        &self.pixels
    }

    /// One row of pixels, `x..x + len`. Out-of-range requests give an empty slice.
    pub fn row(&self, y: u32, x: u32, len: u32) -> &[u32] {
        if y >= self.height || x >= self.width {
            return &[];
        }
        let len = len.min(self.width - x) as usize;
        let start = y as usize * self.width as usize + x as usize;
        &self.pixels[start..start + len]
    }

    /// One row of pixels to write, `x..x + len`. Out-of-range requests give
    /// an empty slice.
    pub fn row_mut(&mut self, y: u32, x: u32, len: u32) -> &mut [u32] {
        if y >= self.height || x >= self.width {
            return &mut [];
        }
        let len = len.min(self.width - x) as usize;
        let start = y as usize * self.width as usize + x as usize;
        &mut self.pixels[start..start + len]
    }

    pub fn pixel(&self, x: u32, y: u32) -> Rgb {
        Rgb::from_xrgb(self.pixels[y as usize * self.width as usize + x as usize])
    }

    fn clip(&self, rect: &Rect) -> Rect {
        rect.intersect(&self.bounds())
    }

    pub fn fill_rect(&mut self, rect: &Rect, colour: Rgb) {
        let r = self.clip(rect);
        let px = colour.xrgb();
        for y in r.y..r.bottom() {
            let start = y as usize * self.width as usize + r.x as usize;
            self.pixels[start..start + r.w as usize].fill(px);
        }
    }

    /// Cover `rect` with `colour` at `opacity` (0..=1).
    pub fn fill_rect_faded(&mut self, rect: &Rect, colour: Rgb, opacity: f32) {
        let a = alpha(opacity);
        if a == 0 {
            return;
        }
        let r = self.clip(rect);
        for y in r.y..r.bottom() {
            let start = y as usize * self.width as usize + r.x as usize;
            for px in &mut self.pixels[start..start + r.w as usize] {
                *px = blend(*px, colour, a);
            }
        }
    }

    /// Draw a premultiplied image with its top-left corner at (`x`, `y`), at
    /// `opacity` (0..=1).
    pub fn blit_image(&mut self, image: &Image, x: i32, y: i32, opacity: f32, clip: &Rect) {
        let k = alpha(opacity);
        if k == 0 {
            return;
        }
        let area = self.clip(&Rect::new(x, y, image.width as i32, image.height as i32).intersect(clip));
        for py in area.y..area.bottom() {
            let sy = (py - y) as usize;
            for px in area.x..area.right() {
                let sx = (px - x) as usize;
                let s = &image.data[(sy * image.width as usize + sx) * 4..][..4];
                if s[3] == 0 {
                    continue;
                }
                // The image's own premultiplied channels, scaled by the opacity.
                let scale = |v: u8| if k == 255 { v as u32 } else { (v as u32 * k + 127) / 255 };
                let a = scale(s[3]);
                let idx = py as usize * self.width as usize + px as usize;
                let d = self.pixels[idx];
                let inv = 255 - a;
                let ch = |src: u8, dst: u32| (scale(src) + (dst * inv + 127) / 255).min(255);
                self.pixels[idx] =
                    (ch(s[0], (d >> 16) & 0xff) << 16) | (ch(s[1], (d >> 8) & 0xff) << 8) | ch(s[2], d & 0xff);
            }
        }
    }

    /// Draw a coverage mask in one colour at `opacity` (0..=1) with its
    /// top-left corner at (`x`, `y`).
    pub fn blit_mask(&mut self, mask: &Mask, x: i32, y: i32, colour: Rgb, opacity: f32, clip: &Rect) {
        let k = alpha(opacity);
        if k == 0 {
            return;
        }
        let area = self.clip(&Rect::new(x, y, mask.width as i32, mask.height as i32).intersect(clip));
        for py in area.y..area.bottom() {
            let sy = (py - y) as usize;
            for px in area.x..area.right() {
                let a = mask.data[sy * mask.width as usize + (px - x) as usize] as u32;
                if a == 0 {
                    continue;
                }
                let a = if k == 255 { a } else { (a * k + 127) / 255 };
                let idx = py as usize * self.width as usize + px as usize;
                self.pixels[idx] = blend(self.pixels[idx], colour, a);
            }
        }
    }

    /// Paint every pixel of `area` (clipped) with what `shade` returns for the
    /// pixel centre: a colour and a coverage in 0..=1.
    pub fn shade<F>(&mut self, area: &Rect, clip: &Rect, mut shade: F)
    where
        F: FnMut(f32, f32) -> Option<(Rgb, f32)>,
    {
        let area = self.clip(&area.intersect(clip));
        for py in area.y..area.bottom() {
            for px in area.x..area.right() {
                if let Some((colour, coverage)) = shade(px as f32 + 0.5, py as f32 + 0.5) {
                    let a = alpha(coverage);
                    if a > 0 {
                        let idx = py as usize * self.width as usize + px as usize;
                        self.pixels[idx] = blend(self.pixels[idx], colour, a);
                    }
                }
            }
        }
    }

    /// Anti-aliased filled rounded rectangle (float geometry).
    #[allow(clippy::too_many_arguments)]
    pub fn fill_round_rect(&mut self, x: f32, y: f32, w: f32, h: f32, radius: f32, colour: Rgb, clip: &Rect) {
        let area = bounding_rect(x, y, w, h, 1.0);
        self.shade(&area, clip, |px, py| {
            let d = round_rect_distance(px, py, x, y, w, h, radius);
            Some((colour, 0.5 - d))
        });
    }

    /// Anti-aliased filled disc.
    pub fn fill_circle(&mut self, cx: f32, cy: f32, radius: f32, colour: Rgb, clip: &Rect) {
        self.fill_circle_faded(cx, cy, radius, colour, 1.0, clip);
    }

    /// Anti-aliased filled disc at `opacity` (0..=1) over what is there.
    pub fn fill_circle_faded(&mut self, cx: f32, cy: f32, radius: f32, colour: Rgb, opacity: f32, clip: &Rect) {
        let opacity = opacity.clamp(0.0, 1.0);
        let area = bounding_rect(cx - radius, cy - radius, radius * 2.0, radius * 2.0, 1.0);
        self.shade(&area, clip, |px, py| {
            let d = ((px - cx).powi(2) + (py - cy).powi(2)).sqrt() - radius;
            Some((colour, (0.5 - d).clamp(0.0, 1.0) * opacity))
        });
    }
}

/// Integer rectangle covering a float rectangle plus a margin.
pub fn bounding_rect(x: f32, y: f32, w: f32, h: f32, margin: f32) -> Rect {
    let left = (x - margin).floor() as i32;
    let top = (y - margin).floor() as i32;
    let right = (x + w + margin).ceil() as i32;
    let bottom = (y + h + margin).ceil() as i32;
    Rect::new(left, top, right - left, bottom - top)
}

/// Signed distance from a point to the edge of a rounded rectangle
/// (negative inside).
pub fn round_rect_distance(px: f32, py: f32, x: f32, y: f32, w: f32, h: f32, radius: f32) -> f32 {
    let radius = radius.min(w / 2.0).min(h / 2.0).max(0.0);
    let cx = x + w / 2.0;
    let cy = y + h / 2.0;
    let qx = (px - cx).abs() - (w / 2.0 - radius);
    let qy = (py - cy).abs() - (h / 2.0 - radius);
    let outside = (qx.max(0.0).powi(2) + qy.max(0.0).powi(2)).sqrt();
    outside + qx.max(qy).min(0.0) - radius
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rect_algebra() {
        let a = Rect::new(0, 0, 10, 10);
        let b = Rect::new(5, 5, 10, 10);
        assert_eq!(a.intersect(&b), Rect::new(5, 5, 5, 5));
        assert_eq!(a.union(&b), Rect::new(0, 0, 15, 15));
        assert!(a.intersect(&Rect::new(20, 20, 5, 5)).is_empty());
        assert_eq!(a.union(&Rect::default()), a);
        assert!(a.contains(&Rect::new(2, 2, 3, 3)));
        assert!(!a.contains(&b));
    }

    #[test]
    fn merge_overlapping_and_touching() {
        let mut rects =
            vec![Rect::new(0, 0, 10, 10), Rect::new(10, 0, 5, 10), Rect::new(100, 100, 5, 5), Rect::default()];
        merge_rects(&mut rects);
        rects.sort_by_key(|r| r.x);
        assert_eq!(rects, vec![Rect::new(0, 0, 15, 10), Rect::new(100, 100, 5, 5)]);
    }

    #[test]
    fn fill_is_clipped_to_the_canvas() {
        let mut c = Canvas::new(8, 8);
        c.fill_rect(&Rect::new(-4, -4, 8, 8), Rgb::new(255, 0, 0));
        assert_eq!(c.pixel(0, 0), Rgb::new(255, 0, 0));
        assert_eq!(c.pixel(3, 3), Rgb::new(255, 0, 0));
        assert_eq!(c.pixel(4, 4), Rgb::new(0, 0, 0));
        c.fill_rect(&Rect::new(100, 100, 5, 5), Rgb::new(0, 255, 0));
    }

    #[test]
    fn mask_blends_towards_the_colour() {
        let mut c = Canvas::new(2, 1);
        let mask = Mask { width: 2, height: 1, data: vec![255, 128] };
        c.blit_mask(&mask, 0, 0, Rgb::new(200, 100, 50), 1.0, &c.bounds());
        assert_eq!(c.pixel(0, 0), Rgb::new(200, 100, 50));
        let half = c.pixel(1, 0);
        assert!((99..=101).contains(&half.r) && (49..=51).contains(&half.g));
        // At half opacity a full coverage is a half mix.
        let mut c = Canvas::new(1, 1);
        c.blit_mask(&Mask { width: 1, height: 1, data: vec![255] }, 0, 0, Rgb::new(200, 100, 50), 0.5, &c.bounds());
        assert_eq!(c.pixel(0, 0), Rgb::new(100, 50, 25));
        c.fill_rect_faded(&c.bounds(), Rgb::new(0, 0, 0), 0.5);
        assert_eq!(c.pixel(0, 0), Rgb::new(50, 25, 12));
        *c.row_mut(0, 0, 9).first_mut().unwrap() = 7;
        assert_eq!(c.pixels(), [7]);
        assert!(c.row_mut(1, 0, 1).is_empty() && c.row_mut(0, 1, 1).is_empty());
    }

    #[test]
    fn premultiplied_image_over_background() {
        let mut c = Canvas::new(1, 1);
        c.fill_rect(&c.bounds(), Rgb::new(100, 100, 100));
        // 50 % white, premultiplied.
        let img = Image { width: 1, height: 1, data: vec![128, 128, 128, 128] };
        c.blit_image(&img, 0, 0, 1.0, &c.bounds());
        let p = c.pixel(0, 0);
        assert!((177..=179).contains(&p.r), "{p:?}");
        // At half opacity: a quarter white over the grey.
        let mut c = Canvas::new(1, 1);
        c.fill_rect(&c.bounds(), Rgb::new(100, 100, 100));
        c.blit_image(&img, 0, 0, 0.5, &c.bounds());
        let p = c.pixel(0, 0);
        assert!((138..=140).contains(&p.r), "{p:?}");
        c.blit_image(&img, 0, 0, 0.0, &c.bounds());
        assert_eq!(c.pixel(0, 0), p, "a transparent image changes nothing");
    }

    #[test]
    fn round_rect_is_solid_inside_and_clear_outside() {
        let mut c = Canvas::new(40, 20);
        let white = Rgb::new(255, 255, 255);
        c.fill_round_rect(4.0, 4.0, 32.0, 12.0, 6.0, white, &c.bounds());
        assert_eq!(c.pixel(20, 10), white);
        assert_eq!(c.pixel(0, 0), Rgb::new(0, 0, 0));
        // The corner pixel is outside the rounded corner.
        assert_ne!(c.pixel(4, 4), white);
        // Clipping keeps pixels outside the clip untouched.
        let mut d = Canvas::new(40, 20);
        d.fill_round_rect(4.0, 4.0, 32.0, 12.0, 6.0, white, &Rect::new(0, 0, 10, 20));
        assert_eq!(d.pixel(20, 10), Rgb::new(0, 0, 0));
        assert_eq!(d.pixel(8, 10), white);
    }

    #[test]
    fn a_faded_disc_is_a_mix_with_what_is_beneath() {
        let mut c = Canvas::new(20, 20);
        c.fill_rect(&c.bounds(), Rgb::new(20, 20, 20));
        c.fill_circle_faded(10.0, 10.0, 4.0, Rgb::new(220, 220, 220), 0.5, &c.bounds());
        assert_eq!(c.pixel(10, 10), Rgb::new(120, 120, 120));
        assert_eq!(c.pixel(2, 2), Rgb::new(20, 20, 20));
        c.fill_circle_faded(10.0, 10.0, 4.0, Rgb::new(220, 220, 220), 0.0, &c.bounds());
        assert_eq!(c.pixel(10, 10), Rgb::new(120, 120, 120), "a transparent disc changes nothing");
        c.fill_circle(10.0, 10.0, 4.0, Rgb::new(220, 220, 220), &c.bounds());
        assert_eq!(c.pixel(10, 10), Rgb::new(220, 220, 220));
    }

    #[test]
    fn colour_mix() {
        let a = Rgb::new(0, 0, 0);
        let b = Rgb::new(200, 100, 50);
        assert_eq!(a.mix(b, 0.0), a);
        assert_eq!(a.mix(b, 1.0), b);
        assert_eq!(a.mix(b, 0.5), Rgb::new(100, 50, 25));
        assert_eq!(a.mix(b, 7.0), b);
        assert_eq!(Rgb::from_xrgb(b.xrgb()), b);
    }
}
