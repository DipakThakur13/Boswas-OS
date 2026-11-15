//! Text from the embedded glyph atlas.
//!
//! The atlas holds printable ASCII at one large size; a glyph is scaled down
//! (area-averaged, at its exact size and sub-pixel position) the first time a
//! size needs it and cached. Characters
//! outside the atlas are drawn as `?`: boot messages are ASCII in practice,
//! and nothing here may fail because of an unusual string.

use std::collections::HashMap;

use crate::assets::{GLYPHS, GLYPH_A8, GLYPH_ASCENT, GLYPH_DESCENT, GLYPH_FIRST, GLYPH_LAST, GLYPH_PX};
use crate::gfx::{Canvas, Mask, Rect, Rgb};

/// Horizontal sub-pixel positions a glyph is prepared for. Placing glyphs
/// to a quarter of a pixel keeps letter spacing even at small sizes.
const SUBPIXELS: usize = 4;

struct Scaled {
    mask: Mask,
    /// Top edge of the bitmap relative to the baseline (negative is above).
    top: i32,
}

/// Scaled-glyph cache for the sizes one display uses.
#[derive(Default)]
pub struct Font {
    sizes: HashMap<u32, Vec<[Option<Scaled>; SUBPIXELS]>>,
}

fn atlas_index(ch: char) -> usize {
    let code = ch as u32;
    let byte = if (GLYPH_FIRST as u32..=GLYPH_LAST as u32).contains(&code) { code as u8 } else { b'?' };
    (byte - GLYPH_FIRST) as usize
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
fn scale_glyph(index: usize, px: u32, sub: usize) -> Option<Scaled> {
    let g = &GLYPHS[index];
    if g.width == 0 || g.height == 0 {
        return None;
    }
    let s = px as f32 / GLYPH_PX;
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
    let src = &GLYPH_A8[g.offset..g.offset + g.width * g.height];
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

fn advance(ch: char, px: u32) -> f32 {
    GLYPHS[atlas_index(ch)].advance * px as f32 / GLYPH_PX
}

impl Font {
    pub fn new() -> Self {
        Self::default()
    }

    fn glyph(&mut self, px: u32, index: usize, sub: usize) -> Option<&Scaled> {
        let size =
            self.sizes.entry(px).or_insert_with(|| (0..GLYPHS.len()).map(|_| [None, None, None, None]).collect());
        let slot = &mut size[index][sub];
        if slot.is_none() {
            *slot = scale_glyph(index, px, sub);
        }
        slot.as_ref()
    }

    /// Distance from the top of a line to its baseline.
    pub fn ascent(px: u32) -> f32 {
        GLYPH_ASCENT * px as f32 / GLYPH_PX
    }

    /// Height of one line of text.
    pub fn line_height(px: u32) -> i32 {
        ((GLYPH_ASCENT - GLYPH_DESCENT) * px as f32 / GLYPH_PX).ceil() as i32
    }

    /// Width of `text` in pixels.
    pub fn measure(&mut self, text: &str, px: u32) -> f32 {
        text.chars().map(|ch| advance(ch, px)).sum()
    }

    /// `text` shortened with `...` so that it fits `max_width`.
    pub fn fit(&mut self, text: &str, px: u32, max_width: f32) -> String {
        if self.measure(text, px) <= max_width {
            return text.to_owned();
        }
        let ellipsis = "...";
        let room = max_width - self.measure(ellipsis, px);
        let mut width = 0.0;
        let mut out = String::new();
        for ch in text.chars() {
            let step = advance(ch, px);
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
    pub fn draw(&mut self, canvas: &mut Canvas, clip: &Rect, x: f32, baseline: f32, text: &str, px: u32, colour: Rgb) {
        let s = px as f32 / GLYPH_PX;
        let base = baseline.round() as i32;
        let mut pen = x;
        for ch in text.chars() {
            let index = atlas_index(ch);
            let exact = pen + GLYPHS[index].left as f32 * s;
            let mut whole = exact.floor();
            let mut sub = ((exact - whole) * SUBPIXELS as f32).round() as usize;
            if sub >= SUBPIXELS {
                sub = 0;
                whole += 1.0;
            }
            if let Some(glyph) = self.glyph(px, index, sub) {
                canvas.blit_mask(&glyph.mask, whole as i32, base + glyph.top, colour, clip);
            }
            pen += GLYPHS[index].advance * s;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn measure_scales_with_size_and_length() {
        let mut font = Font::new();
        let a = font.measure("Boswas", 20);
        let b = font.measure("Boswas", 40);
        assert!(a > 20.0, "{a}");
        assert!((b / a - 2.0).abs() < 0.01);
        assert!(font.measure("BoswasBoswas", 20) > a * 1.9);
        assert_eq!(font.measure("", 20), 0.0);
    }

    #[test]
    fn unknown_characters_draw_as_question_marks() {
        let mut font = Font::new();
        assert_eq!(font.measure("\u{20ac}\u{1f600}\n", 20), font.measure("???", 20));
    }

    #[test]
    fn fit_shortens_with_an_ellipsis() {
        let mut font = Font::new();
        let long = "Please unlock disk nvme0n1p3_crypt with a very long name indeed";
        let fitted = font.fit(long, 20, 200.0);
        assert!(fitted.ends_with("..."));
        assert!(font.measure(&fitted, 20) <= 200.0);
        assert_eq!(font.fit("short", 20, 2000.0), "short");
    }

    #[test]
    fn scaled_glyphs_keep_their_ink() {
        // The coverage of a scaled glyph is the atlas coverage times scale^2,
        // whatever the sub-pixel shift: nothing is lost at the edges.
        let index = atlas_index('B');
        let g = &GLYPHS[index];
        let ink: f32 = GLYPH_A8[g.offset..g.offset + g.width * g.height].iter().map(|&v| v as f32).sum();
        for px in [13u32, 20, 37] {
            let s = px as f32 / GLYPH_PX;
            for sub in 0..SUBPIXELS {
                let scaled = scale_glyph(index, px, sub).unwrap();
                let got: f32 = scaled.mask.data.iter().map(|&v| v as f32).sum();
                let want = ink * s * s;
                assert!((got - want).abs() < want * 0.02, "px {px} sub {sub}: {got} vs {want}");
            }
        }
        assert!(scale_glyph(atlas_index(' '), 20, 0).is_none());
    }

    #[test]
    fn draw_stays_inside_the_clip() {
        let mut font = Font::new();
        let mut canvas = Canvas::new(200, 60);
        let clip = Rect::new(0, 0, 60, 60);
        font.draw(&mut canvas, &clip, 4.0, 40.0, "Boswas OS", 28, Rgb::new(255, 255, 255));
        let lit = |x0: u32, x1: u32| (x0..x1).any(|x| (0..60).any(|y| canvas.pixel(x, y) != Rgb::new(0, 0, 0)));
        assert!(lit(0, 60));
        assert!(!lit(60, 200));
    }
}
