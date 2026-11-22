//! The mark, drawn from its vector geometry at the exact size and position
//! of each frame, and at any stage of its assembly during the intro.
//!
//! Each path is rasterised with exact-area coverage ([`crate::raster`]) and
//! filled with its gradient, evaluated in the mark's own units as the SVG
//! defines it; the paths are composed back to front, as in the SVG. The
//! shading overlays of the bowl and of the stem share their shape's outline,
//! so that coverage is computed once.
//!
//! The assembly moves the parts as the SVG's geometry allows (see
//! [`crate::assets::Mark`]): the upper half of the stem moves down, so the
//! stem rises from a disc at its foot, its gradient stretched over its
//! length; the bowl is revealed by a soft-edged sweep round its centre,
//! from one cut to the other; the dot is scaled about its centre.

use crate::assets::{Paint, Part, Stop, MARK};
use crate::gfx::{bounding_rect, Image, Rect};
use crate::motion::Assembly;
use crate::raster::Rasteriser;

/// Entries of a gradient's colour table.
const RAMP: usize = 256;
/// Width of the bowl's leading edge while it sweeps round, in degrees.
const FEATHER: f32 = 14.0;
/// How far the glow round the dot reaches, in radii of the dot: it fills the
/// counter and stops at the band of the bowl.
pub const GLOW_REACH: f32 = 2.1;

/// Where the mark is drawn: a point (u, v) of its view box is at
/// (x + u * scale, y + v * scale) on the display.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Pose {
    pub x: f32,
    pub y: f32,
    pub scale: f32,
}

/// The middle of the mark's ink, in its own units.
fn ink_centre() -> (f32, f32) {
    ((MARK.ink[0] + MARK.ink[2]) / 2.0, (MARK.ink[1] + MARK.ink[3]) / 2.0)
}

impl Pose {
    /// The pose that makes the mark's ink `height` pixels high, its middle at
    /// (`cx`, `cy`).
    pub fn centred(cx: f32, cy: f32, height: f32) -> Pose {
        let scale = height / (MARK.ink[3] - MARK.ink[1]);
        let (u, v) = ink_centre();
        Pose { x: cx - u * scale, y: cy - v * scale, scale }
    }

    /// Where a point of the view box is on the display.
    pub fn map(&self, u: f32, v: f32) -> (f32, f32) {
        (self.x + u * self.scale, self.y + v * self.scale)
    }

    /// Where the middle of the ink is on the display.
    pub fn centre(&self) -> (f32, f32) {
        let (u, v) = ink_centre();
        self.map(u, v)
    }

    /// Height of the ink in pixels.
    pub fn height(&self) -> f32 {
        (MARK.ink[3] - MARK.ink[1]) * self.scale
    }

    /// Side of the mark's square view box in pixels.
    pub fn side(&self) -> f32 {
        MARK.view * self.scale
    }

    /// From this pose towards `to`, `t` of the way.
    pub fn lerp(self, to: Pose, t: f32) -> Pose {
        if t <= 0.0 {
            return self;
        }
        if t >= 1.0 {
            return to;
        }
        let mix = |a: f32, b: f32| a + (b - a) * t;
        Pose { x: mix(self.x, to.x), y: mix(self.y, to.y), scale: mix(self.scale, to.scale) }
    }

    /// The same, `k` times the size about the middle of the ink.
    pub fn grown(self, k: f32) -> Pose {
        if k == 1.0 {
            return self;
        }
        let (cx, cy) = self.centre();
        Pose::centred(cx, cy, self.height() * k)
    }

    /// The pixels a region `[x0, y0, x1, y1]` of the view box covers.
    pub fn rect_of(&self, b: [f32; 4]) -> Rect {
        let (x0, y0) = self.map(b[0], b[1]);
        let (x1, y1) = self.map(b[2], b[3]);
        bounding_rect(x0, y0, x1 - x0, y1 - y0, 1.0)
    }

    /// The pixels the mark can cover in this pose, whatever its assembly.
    pub fn rect(&self) -> Rect {
        self.rect_of(MARK.ink)
    }

    /// The glow round the dot: its centre and reach in pixels, and the
    /// pixels it covers. `None` for a mark without a dot.
    pub fn glow(&self) -> Option<((f32, f32), f32, Rect)> {
        let dot = MARK.dot?;
        let centre = self.map(dot.centre[0], dot.centre[1]);
        let reach = GLOW_REACH * dot.radius * self.scale;
        Some((centre, reach, bounding_rect(centre.0 - reach, centre.1 - reach, reach * 2.0, reach * 2.0, 1.0)))
    }
}

/// A gradient as a table of premultiplied colours: red, green and blue
/// times alpha, and alpha, all in 0..=255.
type Ramp = [[f32; 4]; RAMP];

fn ramp(stops: &[Stop], opacity: f32) -> Box<Ramp> {
    let mut table = Box::new([[0.0f32; 4]; RAMP]);
    for (i, entry) in table.iter_mut().enumerate() {
        let t = i as f32 / (RAMP - 1) as f32;
        let after = stops.iter().position(|s| s.offset >= t).unwrap_or(stops.len().saturating_sub(1));
        let before = after.saturating_sub(1);
        let (a, b) = (stops[before], stops[after]);
        let f = if b.offset > a.offset { ((t - a.offset) / (b.offset - a.offset)).clamp(0.0, 1.0) } else { 1.0 };
        let mix = |x: f32, y: f32| x + (y - x) * f;
        let alpha = mix(a.opacity, b.opacity) * opacity;
        *entry = [
            mix(a.colour.r as f32, b.colour.r as f32) * alpha,
            mix(a.colour.g as f32, b.colour.g as f32) * alpha,
            mix(a.colour.b as f32, b.colour.b as f32) * alpha,
            alpha * 255.0,
        ];
    }
    table
}

/// How a pixel's position in the view box becomes a position in a
/// gradient's table: t = (u - u0) * du + (v - v0) * dv for a linear one,
/// t = |(u, v) - (u0, v0)| * du for a radial one.
#[derive(Clone, Copy)]
enum Ramping {
    Flat,
    Linear { u0: f32, v0: f32, du: f32, dv: f32 },
    Radial { u0: f32, v0: f32, inv: f32 },
}

impl Ramping {
    #[inline]
    fn at(&self, u: f32, v: f32) -> usize {
        let t = match *self {
            Ramping::Flat => 0.0,
            Ramping::Linear { u0, v0, du, dv } => (u - u0) * du + (v - v0) * dv,
            Ramping::Radial { u0, v0, inv } => (u - u0).hypot(v - v0) * inv,
        };
        (t.clamp(0.0, 1.0) * (RAMP - 1) as f32 + 0.5) as usize
    }
}

/// How a part of the mark moves while it assembles.
#[derive(Clone, Copy)]
enum Deform {
    None,
    /// The stem rising: the points above `split` move down by `shift`, and
    /// the paint is stretched over the shorter length, up from `bottom`.
    Rise {
        split: f32,
        shift: f32,
        bottom: f32,
        stretch: f32,
    },
    /// The dot growing about its centre.
    Grow {
        centre: (f32, f32),
        size: f32,
    },
}

impl Deform {
    /// Where a point of the outline is drawn.
    #[inline]
    fn point(&self, u: f32, v: f32) -> (f32, f32) {
        match *self {
            Deform::None => (u, v),
            Deform::Rise { split, shift, .. } => (u, if v < split { v + shift } else { v }),
            Deform::Grow { centre: (cu, cv), size } => (cu + (u - cu) * size, cv + (v - cv) * size),
        }
    }

    /// Where the paint of a drawn point comes from.
    #[inline]
    fn paint(&self, u: f32, v: f32) -> (f32, f32) {
        match *self {
            Deform::None => (u, v),
            Deform::Rise { bottom, stretch, .. } => (u, bottom - (bottom - v) * stretch),
            Deform::Grow { centre: (cu, cv), size } => (cu + (u - cu) / size, cv + (v - cv) / size),
        }
    }
}

/// Draws the mark.
pub struct MarkPainter {
    raster: Rasteriser,
    ramps: Vec<(Box<Ramp>, Ramping)>,
    /// The picture being composed: premultiplied, in 0..=255.
    layer: Vec<[f32; 4]>,
}

impl Default for MarkPainter {
    fn default() -> Self {
        Self::new()
    }
}

impl MarkPainter {
    pub fn new() -> Self {
        let ramps = MARK
            .shapes
            .iter()
            .map(|shape| match shape.paint {
                Paint::Solid(colour) => {
                    (ramp(&[Stop { offset: 0.0, colour, opacity: 1.0 }], shape.opacity), Ramping::Flat)
                }
                Paint::Linear { from, to, stops } => {
                    let (du, dv) = (to[0] - from[0], to[1] - from[1]);
                    let length2 = (du * du + dv * dv).max(f32::EPSILON);
                    let ramping = Ramping::Linear { u0: from[0], v0: from[1], du: du / length2, dv: dv / length2 };
                    (ramp(stops, shape.opacity), ramping)
                }
                Paint::Radial { centre, radius, stops } => {
                    let ramping = Ramping::Radial { u0: centre[0], v0: centre[1], inv: 1.0 / radius.max(f32::EPSILON) };
                    (ramp(stops, shape.opacity), ramping)
                }
            })
            .collect();
        Self { raster: Rasteriser::new(), ramps, layer: Vec::new() }
    }

    /// Draw the mark at `pose`, assembled as far as `assembly` says, into
    /// `image` (premultiplied RGBA8), which then covers `pose.rect()`.
    pub fn paint(&mut self, pose: Pose, assembly: Assembly, image: &mut Image) {
        let area = pose.rect();
        let (w, h) = (area.w.max(0) as usize, area.h.max(0) as usize);
        self.layer.clear();
        self.layer.resize(w * h, [0.0; 4]);
        let inv = 1.0 / pose.scale.max(f32::EPSILON);

        let stem_rise = assembly.stem.clamp(0.0, 1.0);
        let bowl_sweep = assembly.bowl.clamp(0.0, 1.0);
        let dot_size = assembly.dot.max(0.0);
        let mut rasterised: Option<usize> = None;
        for (i, shape) in MARK.shapes.iter().enumerate() {
            let deform = match (shape.part, MARK.stem, MARK.dot) {
                (Part::Stem, Some(stem), _) if stem_rise < 1.0 => {
                    let shift = (1.0 - stem_rise) * stem.travel;
                    let stretch = (stem.bottom - stem.top) / (stem.bottom - stem.top - shift).max(f32::EPSILON);
                    Deform::Rise { split: stem.split, shift, bottom: stem.bottom, stretch }
                }
                (Part::Dot, _, Some(_)) if dot_size < 1e-3 => continue,
                (Part::Dot, _, Some(dot)) if dot_size != 1.0 => {
                    Deform::Grow { centre: (dot.centre[0], dot.centre[1]), size: dot_size }
                }
                (Part::Bowl, _, _) if bowl_sweep <= 0.0 => continue,
                _ => Deform::None,
            };

            if !(shape.same_outline && rasterised == Some(i.wrapping_sub(1))) {
                let b = shape.outline.bounds;
                let (x0, y0) = deform.point(b[0], b[1]);
                let (x1, y1) = deform.point(b[2], b[3]);
                let bounds = pose.rect_of([x0.min(x1), y0.min(y1), x0.max(x1), y0.max(y1)]).intersect(&area);
                self.raster.begin(bounds);
                for contour in shape.outline.contours() {
                    self.raster.contour(contour.iter().map(|p| {
                        let (u, v) = deform.point(p[0], p[1]);
                        pose.map(u, v)
                    }));
                }
                let coverage = self.raster.finish();
                if let (Part::Bowl, Some(bowl)) = (shape.part, MARK.bowl) {
                    if bowl_sweep < 1.0 {
                        sweep(coverage, bounds, pose, inv, bowl_sweep * (bowl.span + FEATHER), bowl);
                    }
                }
            }
            rasterised = Some(i);

            // Fill: the gradient at each covered pixel, over what is beneath.
            let bounds = self.raster.area();
            let (table, ramping) = (&self.ramps[i].0, self.ramps[i].1);
            let coverage = self.raster.coverage();
            for row in 0..bounds.h.max(0) as usize {
                let y = bounds.y + row as i32;
                let v = (y as f32 + 0.5 - pose.y) * inv;
                let line = &coverage[row * bounds.w as usize..(row + 1) * bounds.w as usize];
                let out = (y - area.y) as usize * w + (bounds.x - area.x) as usize;
                for (col, &c) in line.iter().enumerate() {
                    if c <= 0.0 {
                        continue;
                    }
                    let u = ((bounds.x + col as i32) as f32 + 0.5 - pose.x) * inv;
                    let (pu, pv) = deform.paint(u, v);
                    let src = table[ramping.at(pu, pv)];
                    let dst = &mut self.layer[out + col];
                    let keep = 1.0 - src[3] * c / 255.0;
                    for k in 0..4 {
                        dst[k] = src[k] * c + dst[k] * keep;
                    }
                }
            }
        }

        image.width = w as u32;
        image.height = h as u32;
        image.data.clear();
        image.data.reserve(w * h * 4);
        for px in &self.layer {
            // Rounded (the values are positive, and the cast saturates);
            // premultiplied colour never exceeds its alpha.
            let a = (px[3] + 0.5) as u8;
            for &channel in &px[..3] {
                image.data.push(((channel + 0.5) as u8).min(a));
            }
            image.data.push(a);
        }
    }
}

/// Reveal the bowl up to `revealed` degrees clockwise from its first cut,
/// with a soft leading edge.
fn sweep(coverage: &mut [f32], bounds: Rect, pose: Pose, inv: f32, revealed: f32, bowl: crate::assets::Bowl) {
    let w = bounds.w.max(0) as usize;
    if w == 0 {
        return;
    }
    for (row, line) in coverage.chunks_mut(w).enumerate() {
        let v = ((bounds.y + row as i32) as f32 + 0.5 - pose.y) * inv;
        for (col, c) in line.iter_mut().enumerate() {
            if *c <= 0.0 {
                continue;
            }
            let u = ((bounds.x + col as i32) as f32 + 0.5 - pose.x) * inv;
            let angle = (u - bowl.centre[0]).atan2(bowl.centre[1] - v).to_degrees();
            let along = (angle - bowl.start).rem_euclid(360.0);
            *c *= ((revealed - along) / FEATHER).clamp(0.0, 1.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::assets::{MARK_REFERENCE, MARK_REFERENCE_PX};

    /// The mark drawn so that its whole view box fills `side` pixels from
    /// the origin, as the reference rendering does.
    fn draw(side: f32, assembly: Assembly) -> (Image, Rect) {
        let pose = Pose { x: 0.0, y: 0.0, scale: side / MARK.view };
        let mut image = Image::default();
        MarkPainter::new().paint(pose, assembly, &mut image);
        (image, pose.rect())
    }

    /// Alpha of a pixel of an image that covers `rect`.
    fn alpha_at(image: &Image, rect: Rect, x: i32, y: i32) -> u8 {
        if !rect.contains(&Rect::new(x, y, 1, 1)) {
            return 0;
        }
        image.data[(((y - rect.y) * rect.w + (x - rect.x)) * 4 + 3) as usize]
    }

    #[test]
    fn the_mark_looks_as_the_system_renders_it() {
        // Compared with resvg's rendering of the same SVG at the same size:
        // the same colours everywhere, and the same edges to within the
        // difference between two ways of anti-aliasing.
        let px = MARK_REFERENCE_PX as i32;
        let (image, rect) = draw(px as f32, Assembly::DONE);
        let (mut total, mut worst, mut opaque) = (0u64, 0u8, 0);
        for y in 0..px {
            for x in 0..px {
                let reference = &MARK_REFERENCE[((y * px + x) * 4) as usize..][..4];
                let ours: [u8; 4] = if rect.contains(&Rect::new(x, y, 1, 1)) {
                    let i = (((y - rect.y) * rect.w + (x - rect.x)) * 4) as usize;
                    image.data[i..i + 4].try_into().unwrap()
                } else {
                    [0; 4]
                };
                for k in 0..4 {
                    let d = ours[k].abs_diff(reference[k]);
                    total += d as u64;
                    worst = worst.max(d);
                }
                if reference[3] == 255 && ours[3] == 255 {
                    opaque += 1;
                    // Inside the shapes, the gradients agree to a few levels.
                    for k in 0..3 {
                        assert!(ours[k].abs_diff(reference[k]) <= 3, "({x}, {y}): {ours:?} vs {reference:?}");
                    }
                }
            }
        }
        let mean = total as f64 / (px * px * 4) as f64;
        assert!(mean < 0.3, "mean difference {mean:.3} levels");
        assert!(worst < 60, "largest difference {worst} (anti-aliased edges)");
        assert!(opaque > px * px / 4, "{opaque} opaque pixels");
        // The rest of the pixels are premultiplied.
        assert!(image.data.chunks_exact(4).all(|p| p[0] <= p[3] && p[1] <= p[3] && p[2] <= p[3]));
    }

    #[test]
    fn it_is_drawn_at_any_size_and_position() {
        for height in [24.0f32, 77.5, 173.0, 640.0] {
            let pose = Pose::centred(500.3, 400.7, height);
            assert!((pose.height() - height).abs() < 1e-3);
            let (cx, cy) = pose.centre();
            assert!((cx - 500.3).abs() < 1e-3 && (cy - 400.7).abs() < 1e-3);
            let mut image = Image::default();
            MarkPainter::new().paint(pose, Assembly::DONE, &mut image);
            let rect = pose.rect();
            assert_eq!((image.width, image.height), (rect.w as u32, rect.h as u32));
            // Ink touches every side of the box but the margin stays clear.
            let a = |x: i32, y: i32| alpha_at(&image, rect, x, y);
            assert!((rect.x..rect.right()).all(|x| a(x, rect.y) == 0), "top margin at {height}");
            assert!((rect.y..rect.bottom()).any(|y| a(rect.x + 1, y) > 0 || a(rect.x + 2, y) > 0), "stem at {height}");
            let covered: f64 = image.data.chunks_exact(4).map(|p| p[3] as f64 / 255.0).sum();
            let box_area = (pose.height() * pose.height() * 172.0 / 236.0) as f64;
            assert!(covered > box_area * 0.35 && covered < box_area * 0.6, "{covered} of {box_area} at {height}");
        }
    }

    #[test]
    fn the_assembly_moves_the_parts() {
        let side = 256.0; // one pixel per unit
        let opaque = |a: Assembly, x: i32, y: i32| {
            let (image, rect) = draw(side, a);
            alpha_at(&image, rect, x, y) > 200
        };
        let nothing = Assembly { stem: 0.0, bowl: 0.0, dot: 0.0 };
        // At first the stem is a disc at its foot: its lower end, not its top.
        assert!(opaque(nothing, 63, 235) && !opaque(nothing, 63, 30) && !opaque(nothing, 63, 150));
        let (image, _) = draw(side, nothing);
        let stem_only: f64 = image.data.chunks_exact(4).map(|p| p[3] as f64 / 255.0).sum();
        let disc = std::f64::consts::PI * 21.0 * 21.0;
        assert!((stem_only - disc).abs() < disc * 0.02, "{stem_only} vs {disc}");
        // Half risen.
        let half = Assembly { stem: 0.5, ..nothing };
        assert!(opaque(half, 63, 150) && !opaque(half, 63, 40));
        // The bowl sweeps from its upper cut over the top: the top is shown
        // before the right side, the right side before the bottom.
        let sweeping = |bowl: f32| Assembly { stem: 1.0, bowl, dot: 0.0 };
        let (top, right, bottom) = ((150, 84), (205, 160), (150, 236));
        assert!(!opaque(sweeping(0.0), top.0, top.1));
        assert!(opaque(sweeping(0.35), top.0, top.1) && !opaque(sweeping(0.35), right.0, right.1));
        assert!(opaque(sweeping(0.6), right.0, right.1) && !opaque(sweeping(0.6), bottom.0, bottom.1));
        assert!(opaque(sweeping(1.0), bottom.0, bottom.1));
        // The dot grows from its centre.
        let dot = |size: f32| Assembly { dot: size, ..Assembly::DONE };
        assert!(!opaque(dot(0.0), 134, 160));
        assert!(opaque(dot(0.3), 134, 160) && !opaque(dot(0.3), 148, 160));
        assert!(opaque(dot(1.0), 148, 160) && !opaque(dot(1.0), 154, 160));
        assert!(opaque(dot(1.15), 152, 160), "in the overshoot it is larger");
        // Assembled, it is the mark.
        let (whole, _) = draw(side, Assembly::DONE);
        let (again, _) = draw(side, Assembly { stem: 1.0, bowl: 1.0, dot: 1.0 });
        assert_eq!(whole.data, again.data);
    }

    #[test]
    fn poses_move_and_grow() {
        let a = Pose::centred(100.0, 100.0, 100.0);
        let b = Pose::centred(300.0, 50.0, 50.0);
        assert_eq!(a.lerp(b, 0.0), a);
        let mid = a.lerp(b, 0.5);
        assert!((mid.height() - 75.0).abs() < 1e-3);
        let grown = a.grown(1.5);
        assert!((grown.height() - 150.0).abs() < 1e-3);
        let (cx, cy) = grown.centre();
        assert!((cx - 100.0).abs() < 1e-3 && (cy - 100.0).abs() < 1e-3, "grows about its middle");
        let (centre, reach, rect) = a.glow().unwrap();
        assert!(a.rect().contains(&rect), "the glow stays inside the mark's box");
        assert!(reach > 0.0 && rect.contains(&Rect::new(centre.0 as i32, centre.1 as i32, 1, 1)));
    }
}
