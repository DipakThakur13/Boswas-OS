//! Text from the embedded glyph outlines.
//!
//! A glyph is rasterised from its outline (see [`crate::raster`]) at the
//! exact size and sub-pixel position it is drawn at, the first time a size
//! needs it, and kept. Pairs are kerned as the typeface says. Characters
//! outside printable ASCII are drawn as `?`: boot messages are ASCII in
//! practice, and nothing here may fail because of an unusual string.

use std::collections::HashMap;

use crate::assets::{Face, Weight};
use crate::gfx::{Canvas, Mask, Rect, Rgb};
use crate::raster::Rasteriser;

/// Horizontal sub-pixel positions a glyph is prepared for. Placing glyphs
/// to a quarter of a pixel keeps letter spacing even at small sizes.
const SUBPIXELS: usize = 4;

/// The text shortened to fit, as drawn: a prefix of it, then `...` if it
/// was cut.
const ELLIPSIS: &str = "...";

struct Scaled {
    mask: Mask,
    /// Left and top edges of the bitmap relative to the pen position on the
    /// baseline (negative is left, above).
    left: i32,
    top: i32,
}

/// Rasterised glyphs for the weights and sizes one display uses.
#[derive(Default)]
pub struct Font {
    sizes: HashMap<(Weight, u32), Vec<[Option<Scaled>; SUBPIXELS]>>,
    raster: Rasteriser,
}

fn index(face: &Face, ch: char) -> usize {
    let code = ch as u32;
    let byte = if (face.first as u32..=face.last as u32).contains(&code) { code as u8 } else { b'?' };
    (byte - face.first) as usize
}

/// Sizes are kept to a sixty-fourth of a pixel.
fn size_key(px: f32) -> u32 {
    (px.clamp(1.0, 1024.0) * 64.0).round() as u32
}

/// Rasterise one glyph at `px`, shifted right by `sub / SUBPIXELS` of a pixel.
fn rasterise(raster: &mut Rasteriser, face: &Face, index: usize, px: f32, sub: usize) -> Option<Scaled> {
    let outline = &face.glyphs[index].outline;
    if outline.points.is_empty() {
        return None;
    }
    let shift = sub as f32 / SUBPIXELS as f32;
    let [x0, y0, x1, y1] = outline.bounds;
    // The glyph's box in pixels, y down from the baseline, with a pixel to spare.
    let left = (x0 * px + shift).floor() as i32 - 1;
    let right = (x1 * px + shift).ceil() as i32 + 1;
    let top = (-y1 * px).floor() as i32 - 1;
    let bottom = (-y0 * px).ceil() as i32 + 1;
    let area = Rect::new(0, 0, right - left, bottom - top);
    raster.begin(area);
    for contour in outline.contours() {
        raster.contour(contour.iter().map(|p| (p[0] * px + shift - left as f32, -p[1] * px - top as f32)));
    }
    let data = raster.finish().iter().map(|&c| crate::gfx::alpha(c) as u8).collect();
    Some(Scaled { mask: Mask { width: area.w as u32, height: area.h as u32, data }, left, top })
}

/// The pen advance after `ch` when `next` follows, in pixels at `px`.
fn advance(face: &Face, ch: char, next: Option<char>, px: f32) -> f32 {
    let i = index(face, ch);
    let kern = next.map_or(0.0, |next| face.kern(face.first + i as u8, face.first + index(face, next) as u8));
    (face.glyphs[i].advance + kern) * px
}

impl Font {
    pub fn new() -> Self {
        Self::default()
    }

    fn glyph(&mut self, weight: Weight, px: f32, index: usize, sub: usize) -> Option<&Scaled> {
        let face = weight.face();
        let size = self
            .sizes
            .entry((weight, size_key(px)))
            .or_insert_with(|| (0..face.glyphs.len()).map(|_| [None, None, None, None]).collect());
        let slot = &mut size[index][sub];
        if slot.is_none() {
            *slot = rasterise(&mut self.raster, face, index, px, sub);
        }
        slot.as_ref()
    }

    /// Distance from the top of the typeface's own line to its baseline.
    pub fn ascent(weight: Weight, px: f32) -> f32 {
        weight.face().ascent * px
    }

    /// Height of the typeface's own line (ascent plus descent).
    pub fn line_height(weight: Weight, px: f32) -> f32 {
        let face = weight.face();
        (face.ascent - face.descent) * px
    }

    /// Width of `text` in pixels.
    pub fn measure(text: &str, weight: Weight, px: f32) -> f32 {
        let face = weight.face();
        let mut chars = text.chars().peekable();
        let mut width = 0.0;
        while let Some(ch) = chars.next() {
            width += advance(face, ch, chars.peek().copied(), px);
        }
        width
    }

    /// How much of `text` fits `max_width`: the length in bytes of the part
    /// that is drawn, and whether `...` follows it. Nothing is allocated.
    pub fn fit(text: &str, weight: Weight, px: f32, max_width: f32) -> (usize, bool) {
        if Self::measure(text, weight, px) <= max_width {
            return (text.len(), false);
        }
        let face = weight.face();
        let room = max_width - Self::measure(ELLIPSIS, weight, px);
        let mut width = 0.0;
        let mut end = 0;
        let mut chars = text.char_indices().peekable();
        while let Some((at, ch)) = chars.next() {
            let step = advance(face, ch, chars.peek().map(|&(_, next)| next), px);
            if width + step > room {
                break;
            }
            width += step;
            end = at + ch.len_utf8();
        }
        (text[..end].trim_end().len(), true)
    }

    /// Draw `text` with its left edge at `x` and its baseline at `baseline`,
    /// in `colour` at `opacity`.
    #[allow(clippy::too_many_arguments)]
    pub fn draw(
        &mut self,
        canvas: &mut Canvas,
        clip: &Rect,
        x: f32,
        baseline: f32,
        text: &str,
        weight: Weight,
        px: f32,
        colour: Rgb,
        opacity: f32,
    ) {
        let face = weight.face();
        let base = baseline.round() as i32;
        let mut pen = x;
        let mut chars = text.chars().peekable();
        while let Some(ch) = chars.next() {
            let i = index(face, ch);
            let mut whole = pen.floor();
            let mut sub = ((pen - whole) * SUBPIXELS as f32).round() as usize;
            if sub >= SUBPIXELS {
                sub = 0;
                whole += 1.0;
            }
            if let Some(glyph) = self.glyph(weight, px, i, sub) {
                canvas.blit_mask(&glyph.mask, whole as i32 + glyph.left, base + glyph.top, colour, opacity, clip);
            }
            pen += advance(face, ch, chars.peek().copied(), px);
        }
    }

    /// The width of `text` as [`Font::draw_fitted`] draws it in `max_width`.
    pub fn fitted_width(text: &str, weight: Weight, px: f32, max_width: f32) -> f32 {
        let (end, cut) = Self::fit(text, weight, px, max_width);
        let mut width = Self::measure(&text[..end], weight, px);
        if cut {
            width += Self::measure(ELLIPSIS, weight, px);
        }
        width
    }

    /// Draw `text` shortened to `max_width` with `...` if it does not fit.
    #[allow(clippy::too_many_arguments)]
    pub fn draw_fitted(
        &mut self,
        canvas: &mut Canvas,
        clip: &Rect,
        x: f32,
        baseline: f32,
        text: &str,
        max_width: f32,
        weight: Weight,
        px: f32,
        colour: Rgb,
        opacity: f32,
    ) {
        let (end, cut) = Self::fit(text, weight, px, max_width);
        self.draw(canvas, clip, x, baseline, &text[..end], weight, px, colour, opacity);
        if cut {
            let after = x + Self::measure(&text[..end], weight, px);
            self.draw(canvas, clip, after, baseline, ELLIPSIS, weight, px, colour, opacity);
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
            let a = Font::measure("Boswas", weight, 20.0);
            let b = Font::measure("Boswas", weight, 40.0);
            assert!(a > 20.0, "{a}");
            assert!((b / a - 2.0).abs() < 0.01);
            assert!(Font::measure("BoswasBoswas", weight, 20.0) > a * 1.9);
            assert_eq!(Font::measure("", weight, 20.0), 0.0);
        }
    }

    #[test]
    fn pairs_are_kerned() {
        let face = Weight::Regular.face();
        let plain = (face.glyphs[index(face, 'T')].advance + face.glyphs[index(face, 'o')].advance) * 20.0;
        let kerned = Font::measure("To", Weight::Regular, 20.0);
        assert!((kerned - plain - face.kern(b'T', b'o') * 20.0).abs() < 1e-4);
        if crate::assets::TYPEFACE == "Inter" {
            assert!(kerned < plain - 0.3, "{kerned} vs {plain}");
        }
    }

    #[test]
    fn line_metrics_scale_with_the_size() {
        for weight in WEIGHTS {
            let (ascent, line) = (Font::ascent(weight, 14.0), Font::line_height(weight, 14.0));
            assert!(ascent > 9.0 && ascent < 16.0, "{ascent}");
            assert!(line > ascent && line < 22.0, "{line}");
            assert!((Font::line_height(weight, 28.0) / line - 2.0).abs() < 0.01);
        }
    }

    #[test]
    fn unknown_characters_draw_as_question_marks() {
        assert_eq!(
            Font::measure("\u{20ac}\u{1f600}\n", Weight::Regular, 20.0),
            Font::measure("???", Weight::Regular, 20.0)
        );
    }

    #[test]
    fn fit_shortens_with_an_ellipsis() {
        let long = "Please unlock disk nvme0n1p3_crypt with a very long name indeed";
        let (end, cut) = Font::fit(long, Weight::Regular, 20.0, 200.0);
        assert!(cut && end < long.len() && !long[..end].ends_with(' '));
        assert!(Font::fitted_width(long, Weight::Regular, 20.0, 200.0) <= 200.0);
        assert_eq!(Font::fit("short", Weight::Regular, 20.0, 2000.0), (5, false));
        // Never cuts a character in half.
        let wide = "\u{e9}".repeat(100);
        let (end, _) = Font::fit(&wide, Weight::Regular, 20.0, 100.0);
        assert!(wide.is_char_boundary(end));
    }

    #[test]
    fn a_glyph_covers_the_area_of_its_outline() {
        // Exact-area rasterisation: the coverage of a drawn glyph adds up to
        // the area its outline encloses, at any size and sub-pixel position.
        let mut raster = Rasteriser::new();
        for weight in WEIGHTS {
            let face = weight.face();
            for ch in ['B', 'o', 'k', '%'] {
                let i = index(face, ch);
                for px in [12.0f32, 14.0, 18.67, 40.0, 64.0] {
                    let want = face.glyphs[i].outline.area() * px * px;
                    for sub in 0..SUBPIXELS {
                        let scaled = rasterise(&mut raster, face, i, px, sub).unwrap();
                        let got: f32 = scaled.mask.data.iter().map(|&v| v as f32 / 255.0).sum();
                        assert!((got - want).abs() < want * 0.01 + 0.5, "{weight:?} {ch} {px} {sub}: {got} vs {want}");
                    }
                }
            }
            assert!(rasterise(&mut raster, face, index(face, ' '), 20.0, 0).is_none());
        }
    }

    #[test]
    fn draw_stays_inside_the_clip() {
        let mut font = Font::new();
        let mut canvas = Canvas::new(200, 60);
        let clip = Rect::new(0, 0, 60, 60);
        let white = Rgb::new(255, 255, 255);
        font.draw(&mut canvas, &clip, 4.0, 40.0, "Boswas OS", Weight::SemiBold, 28.0, white, 1.0);
        let lit = |c: &Canvas, x0: u32, x1: u32| (x0..x1).any(|x| (0..60).any(|y| c.pixel(x, y) != Rgb::new(0, 0, 0)));
        assert!(lit(&canvas, 0, 60));
        assert!(!lit(&canvas, 60, 200));
        // Shortened text ends in an ellipsis inside its width.
        let mut canvas = Canvas::new(200, 60);
        let long = "Boswas OS boot screen with far too much text";
        let all = canvas.bounds();
        font.draw_fitted(&mut canvas, &all, 0.0, 40.0, long, 150.0, Weight::Regular, 20.0, white, 1.0);
        assert!(lit(&canvas, 120, 150) && !lit(&canvas, 151, 200));
    }

    #[test]
    fn opacity_fades_the_text() {
        let mut font = Font::new();
        let white = Rgb::new(255, 255, 255);
        let ink = |opacity: f32| {
            let mut canvas = Canvas::new(80, 40);
            let all = canvas.bounds();
            Font::new().draw(&mut canvas, &all, 2.0, 30.0, "Bo", Weight::Regular, 24.0, white, opacity);
            canvas.pixels().iter().map(|&p| p & 0xff).sum::<u32>()
        };
        let (full, half) = (ink(1.0), ink(0.5));
        assert!((half as f32 / full as f32 - 0.5).abs() < 0.02, "{half} of {full}");
        assert_eq!(ink(0.0), 0);
        let mut canvas = Canvas::new(10, 10);
        let all = canvas.bounds();
        font.draw(&mut canvas, &all, 0.0, 8.0, "", Weight::Regular, 10.0, white, 1.0);
    }
}
