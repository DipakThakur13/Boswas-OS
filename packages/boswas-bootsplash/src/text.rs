//! Text from the embedded glyph atlases.
//!
//! An atlas holds printable ASCII in one weight at one large size; a glyph
//! is scaled down (area-averaged, at its exact size and sub-pixel position)
//! the first time a size needs it and cached. Characters outside the atlas
//! are drawn as `?`: boot messages are ASCII in practice, and nothing here
//! may fail because of an unusual string.

use std::collections::HashMap;

use crate::assets::{Face, Weight};
use crate::gfx::{Canvas, Mask, Rect, Rgb};

/// Horizontal sub-pixel positions a glyph is prepared for. Placing glyphs
/// to a quarter of a pixel keeps letter spacing even at small sizes.
const SUBPIXELS: usize = 4;

struct Scaled {
    mask: Mask,
    /// Top edge of the bitmap relative to the baseline (negative is above).
    top: i32,
}

/// Scaled-glyph cache for the weights and sizes one display uses.
#[derive(Default)]
pub struct Font {
    sizes: HashMap<(Weight, u32), Vec<[Option<Scaled>; SUBPIXELS]>>,
}

fn atlas_index(face: &Face, ch: char) -> usize {
    let code = ch as u32;
    let byte = if (face.first as u32..=face.last as u32).contains(&code) { code as u8 } else { b'?' };
    (byte - face.first) as usize
}

/// For each destination pixel of one axis: first source index and weights.
/// `scale` is destination pixels per source pixel, `shift` moves the image
/// by a fraction of a destination pixel. Source outside the bitmap counts as
/// empty, so edges keep their true coverage.
fn coverage_weights(src: usize, dst: usize, scale: f32, shift: f32) -> Vec<(usize, Vec<f32>)> {
    (0..dst)
        .map(|d| {
            let start = (d as f32 - shift) / scale;
            let end = (d as f32 + 1.0 - shift) / scale;
            let first = (start.floor().max(0.0) as usize).min(src);
            let last = (end.ceil().max(0.0) as usize).min(src).max(first);
            let weights =
                (first..last).map(|s| (((s + 1) as f32).min(end) - (s as f32).max(start)).max(0.0) * scale).collect();
            (first, weights)
        })
        .collect()
}

/// Scale one atlas glyph to `px`, shifted right by `sub / SUBPIXELS` of a pixel.
fn scale_glyph(face: &Face, index: usize, px: u32, sub: usize) -> Option<Scaled> {
    let g = &face.glyphs[index];
    if g.width == 0 || g.height == 0 {
        return None;
    }
    let s = px as f32 / face.px;
    let shift_x = sub as f32 / SUBPIXELS as f32;
    // Keep the baseline exact: the bitmap's top is rounded down and the
    // remainder becomes a vertical shift inside the bitmap.
    let top_exact = -((g.bottom + g.height as i32) as f32) * s;
    let top = top_exact.floor();
    let shift_y = top_exact - top;
    let w = (g.width as f32 * s + shift_x).ceil().max(1.0) as usize;
    let h = (g.height as f32 * s + shift_y).ceil().max(1.0) as usize;
    let xs = coverage_weights(g.width, w, s, shift_x);
    let ys = coverage_weights(g.height, h, s, shift_y);
    let src = &face.coverage[g.offset..g.offset + g.width * g.height];
    let mut tmp = vec![0f32; w * g.height];
    for y in 0..g.height {
        let row = &src[y * g.width..(y + 1) * g.width];
        for (dx, (start, weights)) in xs.iter().enumerate() {
            tmp[y * w + dx] = weights.iter().enumerate().map(|(i, k)| row[start + i] as f32 * k).sum();
        }
    }
    let mut data = vec![0u8; w * h];
    for (dy, (start, weights)) in ys.iter().enumerate() {
        for dx in 0..w {
            let v: f32 = weights.iter().enumerate().map(|(i, k)| tmp[(start + i) * w + dx] * k).sum();
            data[dy * w + dx] = v.round().clamp(0.0, 255.0) as u8;
        }
    }
    Some(Scaled { mask: Mask { width: w as u32, height: h as u32, data }, top: top as i32 })
}

fn advance(face: &Face, ch: char, px: u32) -> f32 {
    face.glyphs[atlas_index(face, ch)].advance * px as f32 / face.px
}

impl Font {
    pub fn new() -> Self {
        Self::default()
    }

    fn glyph(&mut self, weight: Weight, px: u32, index: usize, sub: usize) -> Option<&Scaled> {
        let face = weight.face();
        let size = self
            .sizes
            .entry((weight, px))
            .or_insert_with(|| (0..face.glyphs.len()).map(|_| [None, None, None, None]).collect());
        let slot = &mut size[index][sub];
        if slot.is_none() {
            *slot = scale_glyph(face, index, px, sub);
        }
        slot.as_ref()
    }

    /// Distance from the top of the typeface's own line to its baseline.
    pub fn ascent(weight: Weight, px: u32) -> f32 {
        let face = weight.face();
        face.ascent * px as f32 / face.px
    }

    /// Height of the typeface's own line (ascent plus descent).
    pub fn line_height(weight: Weight, px: u32) -> f32 {
        let face = weight.face();
        (face.ascent - face.descent) * px as f32 / face.px
    }

    /// Width of `text` in pixels.
    pub fn measure(text: &str, weight: Weight, px: u32) -> f32 {
        let face = weight.face();
        text.chars().map(|ch| advance(face, ch, px)).sum()
    }

    /// `text` shortened with `...` so that it fits `max_width`.
    pub fn fit(text: &str, weight: Weight, px: u32, max_width: f32) -> String {
        if Self::measure(text, weight, px) <= max_width {
            return text.to_owned();
        }
        let face = weight.face();
        let ellipsis = "...";
        let room = max_width - Self::measure(ellipsis, weight, px);
        let mut width = 0.0;
        let mut out = String::new();
        for ch in text.chars() {
            let step = advance(face, ch, px);
            if width + step > room {
                break;
            }
            width += step;
            out.push(ch);
        }
        let mut out = out.trim_end().to_owned();
        out.push_str(ellipsis);
        out
    }

    /// Draw `text` with its left edge at `x` and its baseline at `baseline`.
    #[allow(clippy::too_many_arguments)]
    pub fn draw(
        &mut self,
        canvas: &mut Canvas,
        clip: &Rect,
        x: f32,
        baseline: f32,
        text: &str,
        weight: Weight,
        px: u32,
        colour: Rgb,
    ) {
        let face = weight.face();
        let s = px as f32 / face.px;
        let base = baseline.round() as i32;
        let mut pen = x;
        for ch in text.chars() {
            let index = atlas_index(face, ch);
            let exact = pen + face.glyphs[index].left as f32 * s;
            let mut whole = exact.floor();
            let mut sub = ((exact - whole) * SUBPIXELS as f32).round() as usize;
            if sub >= SUBPIXELS {
                sub = 0;
                whole += 1.0;
            }
            if let Some(glyph) = self.glyph(weight, px, index, sub) {
                canvas.blit_mask(&glyph.mask, whole as i32, base + glyph.top, colour, clip);
            }
            pen += face.glyphs[index].advance * s;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const WEIGHTS: [Weight; 2] = [Weight::Regular, Weight::SemiBold];

    #[test]
    fn measure_scales_with_size_and_length() {
        for weight in WEIGHTS {
            let a = Font::measure("Boswas", weight, 20);
            let b = Font::measure("Boswas", weight, 40);
            assert!(a > 20.0, "{a}");
            assert!((b / a - 2.0).abs() < 0.01);
            assert!(Font::measure("BoswasBoswas", weight, 20) > a * 1.9);
            assert_eq!(Font::measure("", weight, 20), 0.0);
        }
    }

    #[test]
    fn line_metrics_scale_with_the_size() {
        for weight in WEIGHTS {
            let (ascent, line) = (Font::ascent(weight, 14), Font::line_height(weight, 14));
            assert!(ascent > 9.0 && ascent < 16.0, "{ascent}");
            assert!(line > ascent && line < 22.0, "{line}");
            assert!((Font::line_height(weight, 28) / line - 2.0).abs() < 0.01);
        }
    }

    #[test]
    fn unknown_characters_draw_as_question_marks() {
        assert_eq!(
            Font::measure("\u{20ac}\u{1f600}\n", Weight::Regular, 20),
            Font::measure("???", Weight::Regular, 20)
        );
    }

    #[test]
    fn fit_shortens_with_an_ellipsis() {
        let long = "Please unlock disk nvme0n1p3_crypt with a very long name indeed";
        let fitted = Font::fit(long, Weight::Regular, 20, 200.0);
        assert!(fitted.ends_with("..."));
        assert!(Font::measure(&fitted, Weight::Regular, 20) <= 200.0);
        assert_eq!(Font::fit("short", Weight::Regular, 20, 2000.0), "short");
    }

    #[test]
    fn scaled_glyphs_keep_their_ink() {
        // The coverage of a scaled glyph is the atlas coverage times scale^2,
        // whatever the sub-pixel shift: nothing is lost at the edges.
        for weight in WEIGHTS {
            let face = weight.face();
            let index = atlas_index(face, 'B');
            let g = &face.glyphs[index];
            let ink: f32 = face.coverage[g.offset..g.offset + g.width * g.height].iter().map(|&v| v as f32).sum();
            for px in [12u32, 14, 20, 37] {
                let s = px as f32 / face.px;
                for sub in 0..SUBPIXELS {
                    let scaled = scale_glyph(face, index, px, sub).unwrap();
                    let got: f32 = scaled.mask.data.iter().map(|&v| v as f32).sum();
                    let want = ink * s * s;
                    assert!((got - want).abs() < want * 0.02, "{weight:?} px {px} sub {sub}: {got} vs {want}");
                }
            }
            assert!(scale_glyph(face, atlas_index(face, ' '), 20, 0).is_none());
        }
    }

    #[test]
    fn draw_stays_inside_the_clip() {
        let mut font = Font::new();
        let mut canvas = Canvas::new(200, 60);
        let clip = Rect::new(0, 0, 60, 60);
        font.draw(&mut canvas, &clip, 4.0, 40.0, "Boswas OS", Weight::SemiBold, 28, Rgb::new(255, 255, 255));
        let lit = |x0: u32, x1: u32| (x0..x1).any(|x| (0..60).any(|y| canvas.pixel(x, y) != Rgb::new(0, 0, 0)));
        assert!(lit(0, 60));
        assert!(!lit(60, 200));
    }
}
