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
    glass,
    tokens::{Material, Rgba},
};

/// Width of the reduced wallpaper copy in pixels.
const REDUCED_WIDTH: u32 = 240;
/// Blurred copies kept (one per distinct blur radius).
const MAX_BLURS: usize = 4;
/// Bytes of filled surfaces kept at most per backdrop; the oldest go first.
const MAX_FILLED_BYTES: usize = 48 * 1024 * 1024;

/// What a filled surface is drawn from: its size, corners, place on the
/// output and the fraction of a pixel it is off the grid (device pixels,
/// rounded), and the material.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
struct FillKey([i32; 19]);

/// The blurred wallpaper of one output.
pub struct Backdrop {
    reduced: Pixmap,
    /// Output logical pixels per pixel of `reduced`.
    step: f32,
    /// Blurred copies by (blur radius in reduced pixels, saturation in
    /// hundredths).
    blurred: Vec<((u32, u32), Pixmap)>,
    /// The darkest and brightest colours of each blurred copy (glass makes
    /// its tint legible over them).
    extremes: Vec<((u32, u32), Vec<Rgba>)>,
    /// The grain tile of each noise opacity (in ten-thousandths).
    grains: Vec<(u32, Pixmap)>,
    /// Surfaces filled before, most recently used last: a panel is drawn
    /// again on every hover change, and magnifying the blurred wallpaper
    /// costs more than everything else on it.
    filled: Vec<(FillKey, Pixmap)>,
    /// Fills drawn (not taken from `filled`), for tests.
    renders: usize,
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
            extremes: Vec::new(),
            grains: Vec::new(),
            filled: Vec::new(),
            renders: 0,
        })
    }

    /// A copy of this backdrop without the surfaces it filled: what a
    /// surface is drawn from on another thread.
    pub fn copy_for_work(&self) -> Backdrop {
        Backdrop {
            reduced: self.reduced.clone(),
            step: self.step,
            blurred: self.blurred.clone(),
            extremes: self.extremes.clone(),
            grains: self.grains.clone(),
            filled: Vec::new(),
            renders: 0,
        }
    }

    fn blurred(&mut self, blur: f32, saturation: f32) -> &Pixmap {
        let key = self.blur_key(blur, saturation);
        if let Some(index) = self.blurred.iter().position(|(k, _)| *k == key) {
            return &self.blurred[index].1;
        }
        if self.blurred.len() >= MAX_BLURS {
            self.blurred.remove(0);
        }
        let mut copy = self.reduced.clone();
        saturate(&mut copy, key.1 as f32 / 100.0);
        box_blur(&mut copy, key.0);
        self.blurred.push((key, copy));
        &self.blurred[self.blurred.len() - 1].1
    }

    /// A Gaussian of sigma blur/2, approximated by three box blurs of this
    /// radius in reduced pixels; and the saturation in hundredths.
    fn blur_key(&self, blur: f32, saturation: f32) -> (u32, u32) {
        let sigma = (blur / 2.0 / self.step).max(0.0);
        ((sigma * 0.92).round() as u32, (saturation.clamp(0.0, 4.0) * 100.0).round() as u32)
    }

    /// The darkest and the brightest colours of the wallpaper blurred by
    /// `blur` and saturated by `saturation` (six of each, searched on a
    /// 64 x 36 grid as the toolkit does): what glass makes its tint legible
    /// over.
    /// The whole wallpaper, reduced, saturated and blurred (premultiplied
    /// RGBA), as the fills sample it: a full-screen backdrop is this picture
    /// enlarged.
    pub fn picture(&mut self, blur: f32, saturation: f32) -> &Pixmap {
        self.blurred(blur, saturation)
    }

    /// How many fills were drawn rather than taken from those kept.
    pub fn renders(&self) -> usize {
        self.renders
    }

    pub fn extremes(&mut self, blur: f32, saturation: f32) -> Vec<Rgba> {
        let key = self.blur_key(blur, saturation);
        if let Some((_, found)) = self.extremes.iter().find(|(k, _)| *k == key) {
            return found.clone();
        }
        let source = self.blurred(blur, saturation);
        let (w, h) = (source.width() as usize, source.height() as usize);
        let data = source.data();
        let mut colours: Vec<Rgba> = Vec::with_capacity(64 * 36);
        for gy in 0..36usize {
            for gx in 0..64usize {
                let (x, y) = ((gx * w + w / 2) / 64, (gy * h + h / 2) / 36);
                let i = (y.min(h - 1) * w + x.min(w - 1)) * 4;
                colours.push(Rgba::rgb(data[i], data[i + 1], data[i + 2]));
            }
        }
        colours.sort_by(|a, b| a.luminance().total_cmp(&b.luminance()));
        colours.dedup();
        let mut found: Vec<Rgba> = colours.iter().take(6).copied().collect();
        found.extend(colours.iter().rev().take(6).copied());
        found.dedup();
        if self.extremes.len() >= MAX_BLURS {
            self.extremes.remove(0);
        }
        self.extremes.push((key, found.clone()));
        found
    }

    fn grain(&mut self, noise: f32) -> Option<&Pixmap> {
        let key = (noise.clamp(0.0, 1.0) * 10000.0).round() as u32;
        if key == 0 {
            return None;
        }
        if !self.grains.iter().any(|(k, _)| *k == key) {
            self.grains.push((key, glass::grain_tile(noise)?));
        }
        self.grains.iter().find(|(k, _)| *k == key).map(|(_, tile)| tile)
    }

    /// Fills a rounded rectangle of `canvas` with `material`. `origin` is
    /// the position of the canvas's top-left corner on the output, in
    /// logical pixels; the rectangle is in canvas coordinates.
    #[allow(clippy::too_many_arguments)]
    pub fn fill(&mut self, canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radii: Corners, origin: (f32, f32), material: Material) {
        let recipe = Recipe {
            blur: material.blur,
            saturation: 1.0,
            layers: [(material.tint, material.tint_opacity), (Rgba::TRANSPARENT, 0.0)],
            noise: 0.0,
        };
        self.fill_recipe(canvas, x, y, w, h, radii, origin, &recipe);
    }

    /// Fills a rounded rectangle of `canvas` with the wallpaper blurred,
    /// saturated and covered as `recipe` says (glass: see
    /// [`crate::glass`]); as [`Backdrop::fill`] otherwise.
    #[allow(clippy::too_many_arguments)]
    pub fn fill_recipe(&mut self, canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radii: Corners, origin: (f32, f32), recipe: &Recipe) {
        if !(w > 0.0 && h > 0.0) || !canvas.visible(x, y, w, h) {
            return;
        }
        // The filled shape is drawn once into a picture of its own and kept;
        // the picture is placed on whole pixels, and the fraction of a pixel
        // by which the shape is off the grid is part of the picture.
        let scale = canvas.scale();
        let (left, top) = (x * scale, y * scale);
        let (px, py) = (left.floor(), top.floor());
        let (fx, fy) = (left - px, top - py);
        let colour = |c: Rgba| f32::from(c.r) * 65536.0 + f32::from(c.g) * 256.0 + f32::from(c.b) + f32::from(c.a) / 256.0;
        let [(tint, opacity), (second, second_opacity)] = recipe.layers;
        let key = FillKey(
            [
                w * scale,
                h * scale,
                radii[0] * scale,
                radii[1] * scale,
                radii[2] * scale,
                radii[3] * scale,
                (origin.0 + x) * scale,
                (origin.1 + y) * scale,
                fx,
                fy,
                recipe.blur,
                opacity,
                colour(tint),
                recipe.saturation,
                second_opacity,
                colour(second),
                recipe.noise * 100.0,
                0.0,
                0.0,
            ]
            .map(|v| (v * 64.0).round() as i32),
        );
        let index = match self.filled.iter().position(|(k, _)| *k == key) {
            Some(index) => {
                let entry = self.filled.remove(index);
                self.filled.push(entry);
                self.filled.len() - 1
            }
            None => {
                // Picture logical pixels = canvas logical pixels - the
                // picture's place on the canvas.
                let shift = (px / scale, py / scale);
                self.renders += 1;
                let Some(picture) = self.render_fill(
                    w + (fx + 1.0) / scale,
                    h + (fy + 1.0) / scale,
                    scale,
                    (x - shift.0, y - shift.1, w, h, radii),
                    (origin.0 + shift.0, origin.1 + shift.1),
                    recipe,
                ) else {
                    return;
                };
                self.filled.push((key, picture));
                let mut total: usize = self.filled.iter().map(|(_, p)| p.data().len()).sum();
                while total > MAX_FILLED_BYTES && self.filled.len() > 1 {
                    total -= self.filled.remove(0).1.data().len();
                }
                self.filled.len() - 1
            }
        };
        let paint = tiny_skia::PixmapPaint {
            quality: FilterQuality::Nearest,
            ..tiny_skia::PixmapPaint::default()
        };
        let picture = self.filled[index].1.as_ref();
        canvas.draw_device_pixmap(px as i32, py as i32, picture, &paint);
    }

    /// Draws a material-filled rounded rectangle (`shape`: x, y, w, h,
    /// corners) on a transparent picture of `width` x `height` logical
    /// pixels whose top-left corner is at `origin` on the output.
    fn render_fill(
        &mut self,
        width: f32,
        height: f32,
        scale: f32,
        shape: (f32, f32, f32, f32, Corners),
        origin: (f32, f32),
        recipe: &Recipe,
    ) -> Option<Pixmap> {
        let (x, y, w, h, radii) = shape;
        let path = rounded_rect_path(x, y, w, h, radii)?;
        let mut canvas = Canvas::new(width, height, scale)?;
        let step = self.step;
        let source = self.blurred(recipe.blur, recipe.saturation);
        // Reduced pixels -> output logical pixels -> picture logical pixels.
        let local = Transform::from_scale(step, step).post_translate(-origin.0, -origin.1);
        let paint = Paint {
            shader: Pattern::new(source.as_ref(), SpreadMode::Pad, FilterQuality::Bilinear, 1.0, local),
            anti_alias: true,
            ..Paint::default()
        };
        canvas
            .pixmap_mut()
            .fill_path(&path, &paint, FillRule::Winding, Transform::from_scale(scale, scale), None);
        for (tint, opacity) in recipe.layers {
            if opacity > 0.0 {
                canvas.fill_path(&path, tint.alpha(opacity));
            }
        }
        if let Some(tile) = self.grain(recipe.noise) {
            glass::draw_grain(&mut canvas, x, y, w, h, radii, tile);
        }
        Some(canvas.into_pixmap())
    }
}

/// What a surface filled from the wallpaper is made of: the wallpaper
/// blurred by `blur` logical pixels with its colours saturated by
/// `saturation`, covered with up to two tints at their opacities (an opacity
/// of 0 is no layer) and a grain of `noise` opacity.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Recipe {
    pub blur: f32,
    pub saturation: f32,
    pub layers: [(Rgba, f32); 2],
    pub noise: f32,
}

/// Moves the colours of an opaque picture away from grey by `amount` (1.0:
/// unchanged), as the toolkit saturates its backdrop.
fn saturate(pixmap: &mut Pixmap, amount: f32) {
    if (amount - 1.0).abs() < 1e-3 {
        return;
    }
    for pixel in pixmap.data_mut().chunks_exact_mut(4) {
        let (r, g, b) = (f32::from(pixel[0]), f32::from(pixel[1]), f32::from(pixel[2]));
        let grey = 0.2126 * r + 0.7152 * g + 0.0722 * b;
        let alpha = f32::from(pixel[3]);
        for (channel, value) in pixel[..3].iter_mut().zip([r, g, b]) {
            *channel = (grey + (value - grey) * amount).round().clamp(0.0, alpha) as u8;
        }
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

    /// The material as it was drawn before filled surfaces were kept:
    /// straight onto the canvas.
    #[allow(clippy::too_many_arguments)]
    fn fill_directly(backdrop: &mut Backdrop, canvas: &mut Canvas, x: f32, y: f32, w: f32, h: f32, radii: Corners, origin: (f32, f32), material: Material) {
        let path = rounded_rect_path(x, y, w, h, radii).unwrap();
        let scale = canvas.scale();
        let step = backdrop.step;
        let source = backdrop.blurred(material.blur, 1.0).clone();
        let local = Transform::from_scale(step, step).post_translate(-origin.0, -origin.1);
        let paint = Paint {
            shader: Pattern::new(source.as_ref(), SpreadMode::Pad, FilterQuality::Bilinear, 1.0, local),
            anti_alias: true,
            ..Paint::default()
        };
        canvas.pixmap_mut().fill_path(&path, &paint, FillRule::Winding, Transform::from_scale(scale, scale), None);
        canvas.fill_path(&path, material.tint.alpha(material.tint_opacity));
    }

    #[test]
    fn a_kept_fill_looks_like_one_drawn_directly() {
        let mut backdrop = Backdrop::new(&split_wallpaper(), 400.0, 200.0).unwrap();
        let material = Material { tint: Rgba::rgb(20, 30, 40), tint_opacity: 0.6, blur: 12.0 };
        for (x, y, scale, origin) in [(10.0, 8.0, 1.0, (150.0, 60.0)), (10.5, 8.25, 1.0, (150.0, 60.0)), (10.0, 8.0, 1.5, (163.0, 41.0)), (7.3, 3.9, 1.25, (90.5, 20.0))] {
            for under in [None, Some(Rgba::WHITE.alpha(0.5))] {
                let mut kept = Canvas::new(120.0, 90.0, scale).unwrap();
                let mut direct = Canvas::new(120.0, 90.0, scale).unwrap();
                if let Some(colour) = under {
                    kept.clear(colour);
                    direct.clear(colour);
                }
                // Twice: drawn the first time, taken from the cache the second.
                for _ in 0..2 {
                    let mut canvas = Canvas::new(120.0, 90.0, scale).unwrap();
                    if let Some(colour) = under {
                        canvas.clear(colour);
                    }
                    backdrop.fill(&mut canvas, x, y, 90.0, 60.0, [8.0, 8.0, 0.0, 8.0], origin, material);
                    kept = canvas;
                }
                fill_directly(&mut backdrop, &mut direct, x, y, 90.0, 60.0, [8.0, 8.0, 0.0, 8.0], origin, material);
                let worst = kept.data().iter().zip(direct.data()).map(|(a, b)| a.abs_diff(*b)).max().unwrap();
                assert!(worst <= 2, "at ({x}, {y}) scale {scale}: differs by {worst}");
            }
        }
        assert!(backdrop.filled.len() <= 8, "one picture per distinct surface");
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
    fn a_glass_recipe_saturates_layers_and_grains_the_backdrop() {
        // A muted wallpaper: dull red left, dull blue right.
        let mut wallpaper = Pixmap::new(400, 200).unwrap();
        for (i, px) in wallpaper.data_mut().chunks_exact_mut(4).enumerate() {
            px.copy_from_slice(if i % 400 < 200 { &[160, 110, 110, 255] } else { &[110, 110, 160, 255] });
        }
        let mut backdrop = Backdrop::new(&wallpaper, 400.0, 200.0).unwrap();
        let plain = Recipe { blur: 8.0, saturation: 1.0, layers: [(Rgba::BLACK, 0.0), (Rgba::BLACK, 0.0)], noise: 0.0 };
        let vivid = Recipe { saturation: 1.6, ..plain };
        let sample = |backdrop: &mut Backdrop, recipe: &Recipe| {
            let mut canvas = Canvas::new(40.0, 40.0, 1.0).unwrap();
            backdrop.fill_recipe(&mut canvas, 0.0, 0.0, 40.0, 40.0, [0.0; 4], (20.0, 80.0), recipe);
            canvas
        };
        let a = sample(&mut backdrop, &plain).pixmap().pixel(20, 20).unwrap();
        let b = sample(&mut backdrop, &vivid).pixmap().pixel(20, 20).unwrap();
        assert!(i32::from(b.red()) - i32::from(b.green()) > i32::from(a.red()) - i32::from(a.green()), "saturated: {a:?} {b:?}");
        // Two layers: the second covers the first.
        let layered = Recipe { layers: [(Rgba::BLACK, 0.5), (Rgba::WHITE, 0.5)], ..plain };
        let c = sample(&mut backdrop, &layered).pixmap().pixel(20, 20).unwrap();
        assert!(c.red() > 150 && c.red() < 180 && c.green() > 145, "black then white: {c:?}");
        // The grain varies the flat area a little, the same way every time.
        let grained = Recipe { noise: 0.02, ..plain };
        let one = sample(&mut backdrop, &grained);
        let two = sample(&mut backdrop, &grained);
        assert_eq!(one.data(), two.data());
        let flat = sample(&mut backdrop, &plain);
        let differ = one.data().chunks_exact(4).zip(flat.data().chunks_exact(4)).filter(|(x, y)| x != y).count();
        assert!(differ > 200, "the grain shows ({differ} pixels)");
        let worst = one.data().iter().zip(flat.data()).map(|(x, y)| x.abs_diff(*y)).max().unwrap();
        assert!(worst <= 12, "and stays fine ({worst})");
        // The extremes are the darkest and brightest parts.
        let extremes = backdrop.extremes(8.0, 1.0);
        assert!(extremes.len() >= 2);
        assert!(extremes.first().unwrap().luminance() <= extremes.last().unwrap().luminance());
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
