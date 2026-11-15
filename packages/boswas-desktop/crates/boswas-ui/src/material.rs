//! Mica, acrylic and chrome: translucent surfaces drawn from the wallpaper.
//!
//! A [`Backdrop`] holds a reduced copy of an output's wallpaper and blurs
//! it once per blur radius. A surface that wants a material tells the
//! backdrop where on the output it sits; the matching part of the blurred
//! wallpaper is drawn there and covered with the material's tint. Windows
//! between the wallpaper and the surface are not sampled, as with Mica.

use tiny_skia::{FillRule, FilterQuality, Paint, Pattern, Pixmap, SpreadMode, Transform};

use crate::{
    canvas::{rounded_rect_path, Canvas, Corners},
    tokens::Material,
};

/// Width of the reduced wallpaper copy in pixels.
const REDUCED_WIDTH: u32 = 240;
/// Blurred copies kept (one per distinct blur radius).
const MAX_BLURS: usize = 4;

/// The blurred wallpaper of one output.
pub struct Backdrop {
    reduced: Pixmap,
    /// Output logical pixels per pixel of `reduced`.
    step: f32,
    blurred: Vec<(u32, Pixmap)>,
}

impl Backdrop {
    /// A backdrop for an output of `width` x `height` logical pixels whose
    /// wallpaper is `wallpaper` (any resolution, covering the output).
    pub fn new(wallpaper: &Pixmap, width: f32, height: f32) -> Option<Self> {
        if !(width >= 1.0 && height >= 1.0) || wallpaper.width() == 0 || wallpaper.height() == 0 {
            return None;
        }
        let rw = REDUCED_WIDTH.min(wallpaper.width()).max(1);
        let rh = ((rw as f32 * height / width).round() as u32).clamp(1, 4096).min(wallpaper.height());
        Some(Self {
            reduced: reduce(wallpaper, rw, rh)?,
            step: width / rw as f32,
            blurred: Vec::new(),
        })
    }

    fn blurred(&mut self, blur: f32) -> &Pixmap {
        // A Gaussian of sigma blur/2, approximated by three box blurs.
        let sigma = (blur / 2.0 / self.step).max(0.0);
        let radius = (sigma * 0.92).round() as u32;
        if let Some(index) = self.blurred.iter().position(|(r, _)| *r == radius) {
            return &self.blurred[index].1;
        }
        if self.blurred.len() >= MAX_BLURS {
            self.blurred.remove(0);
        }
        let mut copy = self.reduced.clone();
        box_blur(&mut copy, radius);
        self.blurred.push((radius, copy));
        &self.blurred[self.blurred.len() - 1].1
    }

    /// Fills a rounded rectangle of `canvas` with `material`. `origin` is
    /// the position of the canvas's top-left corner on the output, in
    /// logical pixels; the rectangle is in canvas coordinates.
    #[allow(clippy::too_many_arguments)]
    pub fn fill(&mut self, canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radii: Corners, origin: (f32, f32), material: Material) {
        let Some(path) = rounded_rect_path(x, y, w, h, radii) else {
            return;
        };
        let step = self.step;
        let scale = canvas.scale();
        let source = self.blurred(material.blur);
        // Reduced pixels -> output logical pixels -> canvas logical pixels.
        let local = Transform::from_scale(step, step).post_translate(-origin.0, -origin.1);
        let paint = Paint {
            shader: Pattern::new(source.as_ref(), SpreadMode::Pad, FilterQuality::Bilinear, 1.0, local),
            anti_alias: true,
            ..Paint::default()
        };
        canvas
            .pixmap_mut()
            .fill_path(&path, &paint, FillRule::Winding, Transform::from_scale(scale, scale), None);
        canvas.fill_path(&path, material.tint.alpha(material.tint_opacity));
    }
}

/// Fills a rounded rectangle with the material's plain tint: what a surface
/// shows where no wallpaper is known.
pub fn fill_fallback(canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radii: Corners, material: Material) {
    canvas.fill_rounded_rect4(x, y, w, h, radii, material.fallback());
}

/// Averages `source` down to `width` x `height` (premultiplied RGBA).
fn reduce(source: &Pixmap, width: u32, height: u32) -> Option<Pixmap> {
    let mut out = Pixmap::new(width, height)?;
    let (sw, sh) = (source.width() as usize, source.height() as usize);
    let data = source.data();
    let (w, h) = (width as usize, height as usize);
    for (oy, row) in out.data_mut().chunks_exact_mut(w * 4).enumerate() {
        let (y0, y1) = (oy * sh / h, (((oy + 1) * sh).div_ceil(h)).min(sh).max(oy * sh / h + 1));
        for (ox, pixel) in row.chunks_exact_mut(4).enumerate() {
            let (x0, x1) = (ox * sw / w, (((ox + 1) * sw).div_ceil(w)).min(sw).max(ox * sw / w + 1));
            // Sample at most 8x8 points of the block: enough for a copy
            // that is blurred afterwards.
            let (sx, sy) = (((x1 - x0) / 8).max(1), ((y1 - y0) / 8).max(1));
            let mut sum = [0u32; 4];
            let mut count = 0u32;
            let mut yy = y0;
            while yy < y1 {
                let mut xx = x0;
                while xx < x1 {
                    let i = (yy * sw + xx) * 4;
                    for c in 0..4 {
                        sum[c] += u32::from(data[i + c]);
                    }
                    count += 1;
                    xx += sx;
                }
                yy += sy;
            }
            for c in 0..4 {
                pixel[c] = (sum[c] / count.max(1)) as u8;
            }
        }
    }
    Some(out)
}

/// Three passes of a box blur of the given radius, edges clamped.
fn box_blur(pixmap: &mut Pixmap, radius: u32) {
    if radius == 0 {
        return;
    }
    let (w, h) = (pixmap.width() as usize, pixmap.height() as usize);
    let r = radius as usize;
    let mut scratch = vec![0u8; w * h * 4];
    for _ in 0..3 {
        blur_pass(pixmap.data(), &mut scratch, w, h, r, true);
        blur_pass(&scratch, pixmap.data_mut(), w, h, r, false);
    }
}

fn blur_pass(src: &[u8], dst: &mut [u8], w: usize, h: usize, r: usize, horizontal: bool) {
    let (lines, len) = if horizontal { (h, w) } else { (w, h) };
    let window = (2 * r + 1) as u32;
    for line in 0..lines {
        let index = |i: usize| if horizontal { (line * w + i) * 4 } else { (i * w + line) * 4 };
        let clamp = |i: isize| i.clamp(0, len as isize - 1) as usize;
        let mut sum = [0u32; 4];
        for i in -(r as isize)..=(r as isize) {
            let p = index(clamp(i));
            for c in 0..4 {
                sum[c] += u32::from(src[p + c]);
            }
        }
        for i in 0..len {
            let p = index(i);
            for c in 0..4 {
                dst[p + c] = (sum[c] / window) as u8;
            }
            let add = index(clamp(i as isize + r as isize + 1));
            let remove = index(clamp(i as isize - r as isize));
            for c in 0..4 {
                sum[c] = sum[c] + u32::from(src[add + c]) - u32::from(src[remove + c]);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tokens::{tokens, Rgba, Scheme};

    fn split_wallpaper() -> Pixmap {
        // Left half red, right half blue.
        let mut p = Pixmap::new(400, 200).unwrap();
        for (i, px) in p.data_mut().chunks_exact_mut(4).enumerate() {
            let left = i % 400 < 200;
            px.copy_from_slice(if left { &[255, 0, 0, 255] } else { &[0, 0, 255, 255] });
        }
        p
    }

    #[test]
    fn a_material_shows_the_wallpaper_behind_it_under_its_tint() {
        let mut backdrop = Backdrop::new(&split_wallpaper(), 400.0, 200.0).unwrap();
        let material = Material { tint: Rgba::rgb(0, 0, 0), tint_opacity: 0.5, blur: 8.0 };
        // A 40x40 surface sitting over the left (red) part of the output...
        let mut left = Canvas::new(40.0, 40.0, 1.0).unwrap();
        backdrop.fill(&mut left, 0.0, 0.0, 40.0, 40.0, [8.0; 4], (20.0, 80.0), material);
        // ...and one over the right (blue) part.
        let mut right = Canvas::new(40.0, 40.0, 1.0).unwrap();
        backdrop.fill(&mut right, 0.0, 0.0, 40.0, 40.0, [8.0; 4], (340.0, 80.0), material);
        let l = left.pixmap().pixel(20, 20).unwrap();
        let r = right.pixmap().pixel(20, 20).unwrap();
        assert!(l.red() > 100 && l.blue() < 20, "left sample is tinted red: {l:?}");
        assert!(r.blue() > 100 && r.red() < 20, "right sample is tinted blue: {r:?}");
        assert!(l.red() < 150, "the tint darkens it");
        assert_eq!(left.pixmap().pixel(0, 0).unwrap().alpha(), 0, "the corner stays outside the rounded shape");
        assert_eq!(l.alpha(), 255);
    }

    #[test]
    fn blur_mixes_across_the_edge_and_keeps_flat_areas() {
        let mut backdrop = Backdrop::new(&split_wallpaper(), 400.0, 200.0).unwrap();
        let material = Material { tint: Rgba::BLACK, tint_opacity: 0.0, blur: 60.0 };
        let mut canvas = Canvas::new(400.0, 200.0, 1.0).unwrap();
        backdrop.fill(&mut canvas, 0.0, 0.0, 400.0, 200.0, [0.0; 4], (0.0, 0.0), material);
        let edge = canvas.pixmap().pixel(200, 100).unwrap();
        assert!(edge.red() > 60 && edge.blue() > 60, "the edge is a mix: {edge:?}");
        let flat = canvas.pixmap().pixel(10, 100).unwrap();
        assert!(flat.red() > 240 && flat.blue() < 15, "far from the edge nothing changes: {flat:?}");
        // The same radius is blurred once.
        backdrop.fill(&mut canvas, 0.0, 0.0, 10.0, 10.0, [0.0; 4], (0.0, 0.0), material);
        assert_eq!(backdrop.blurred.len(), 1);
    }

    #[test]
    fn token_materials_differ_by_tint_and_scale_does_not_move_the_sample() {
        let t = tokens();
        let mut backdrop = Backdrop::new(&split_wallpaper(), 400.0, 200.0).unwrap();
        let mut one = Canvas::new(40.0, 40.0, 1.0).unwrap();
        let mut two = Canvas::new(40.0, 40.0, 2.0).unwrap();
        let acrylic = t.material(Scheme::Dark, "acrylic");
        backdrop.fill(&mut one, 0.0, 0.0, 40.0, 40.0, [0.0; 4], (20.0, 80.0), acrylic);
        backdrop.fill(&mut two, 0.0, 0.0, 40.0, 40.0, [0.0; 4], (20.0, 80.0), acrylic);
        let a = one.pixmap().pixel(20, 20).unwrap();
        let b = two.pixmap().pixel(40, 40).unwrap();
        assert!(a.red().abs_diff(b.red()) <= 2 && a.blue().abs_diff(b.blue()) <= 2, "{a:?} {b:?}");
        assert!(a.red() > a.blue(), "still over the red half");
        assert!(Backdrop::new(&split_wallpaper(), 0.0, 10.0).is_none());
    }
}
