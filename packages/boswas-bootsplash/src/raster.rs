//! An exact-area coverage rasteriser for filled polygons.
//!
//! The method font rasterisers use: every edge adds, to the cells of an
//! accumulation buffer, the signed area it sweeps to the right of itself on
//! each pixel row; a running sum along each row then gives every pixel the
//! exact share of it that the polygon covers. Edges going down count
//! positive and edges going up negative, so the sum is the winding number,
//! and its magnitude, clamped to one, is the coverage under the non-zero
//! rule. No supersampling, no tables: anti-aliasing is exact for edges that
//! do not cross inside one pixel.
//!
//! The mark and the glyphs are drawn with it, at the exact pixel size and
//! position the display needs. The buffers are kept between uses, so once
//! they have grown to the largest area a scene draws, rasterising allocates
//! nothing.

use crate::gfx::Rect;

/// Coverage of one area of the canvas.
#[derive(Default)]
pub struct Rasteriser {
    /// Signed areas: `area.h` rows of `area.w + 2` cells (an edge on the
    /// right border writes one cell past it).
    acc: Vec<f32>,
    /// The result: `area.w * area.h` coverages in 0..=1.
    cov: Vec<f32>,
    area: Rect,
}

impl Rasteriser {
    pub fn new() -> Self {
        Self::default()
    }

    /// The area being rasterised, in canvas pixels.
    pub fn area(&self) -> Rect {
        self.area
    }

    /// Start a new polygon over `area` (in canvas pixels).
    pub fn begin(&mut self, area: Rect) {
        let area = if area.is_empty() { Rect::default() } else { area };
        let cells = (area.w.max(0) as usize + 2) * area.h.max(0) as usize;
        self.acc.clear();
        self.acc.resize(cells, 0.0);
        self.area = area;
    }

    /// Add a closed contour (the last point joins the first). `points` are in
    /// canvas pixel coordinates.
    pub fn contour(&mut self, points: impl IntoIterator<Item = (f32, f32)>) {
        let mut points = points.into_iter();
        let Some(first) = points.next() else { return };
        let mut last = first;
        for p in points {
            self.line(last, p);
            last = p;
        }
        self.line(last, first);
    }

    /// Add one edge of a polygon, from `a` to `b` in canvas pixels.
    pub fn line(&mut self, a: (f32, f32), b: (f32, f32)) {
        let (w, h) = (self.area.w as f32, self.area.h as f32);
        let (ox, oy) = (self.area.x as f32, self.area.y as f32);
        let (x0, y0, x1, y1) = (a.0 - ox, a.1 - oy, b.0 - ox, b.1 - oy);
        if !(x0.is_finite() && y0.is_finite() && x1.is_finite() && y1.is_finite()) || y0 == y1 {
            return;
        }
        // Downwards is positive.
        let (dir, x0, y0, x1, y1) = if y0 < y1 { (1.0, x0, y0, x1, y1) } else { (-1.0, x1, y1, x0, y0) };
        if y1 <= 0.0 || y0 >= h {
            return;
        }
        let dxdy = (x1 - x0) / (y1 - y0);
        let top = y0.max(0.0);
        let bottom = y1.min(h);
        let mut x = x0 + (top - y0) * dxdy;
        let first = top as usize;
        let last = (bottom.ceil() as usize).min(self.area.h as usize);
        let stride = self.area.w as usize + 2;
        for row in first..last {
            let dy = ((row + 1) as f32).min(bottom) - (row as f32).max(top);
            if dy <= 0.0 {
                continue;
            }
            let next = x + dxdy * dy;
            // Parts of the edge outside the area on the left still count
            // for every pixel to their right: they move to its left border.
            // Parts on the right count for none.
            span(&mut self.acc[row * stride..(row + 1) * stride], x.clamp(0.0, w), next.clamp(0.0, w), dy * dir);
            x = next;
        }
    }

    /// Turn what was added since [`Rasteriser::begin`] into coverage, row by
    /// row from the top, and return it.
    pub fn finish(&mut self) -> &mut [f32] {
        let (w, h) = (self.area.w.max(0) as usize, self.area.h.max(0) as usize);
        self.cov.clear();
        self.cov.resize(w * h, 0.0);
        let stride = w + 2;
        for row in 0..h {
            let mut sum = 0.0f32;
            let acc = &self.acc[row * stride..row * stride + w];
            for (cell, out) in acc.iter().zip(&mut self.cov[row * w..(row + 1) * w]) {
                sum += cell;
                *out = sum.abs().min(1.0);
            }
        }
        &mut self.cov
    }

    /// The coverage computed by the last [`Rasteriser::finish`].
    pub fn coverage(&self) -> &[f32] {
        &self.cov
    }
}

/// Add the area that the piece of an edge from `xa` to `xb` on one row
/// (crossing `d` of the row's height, signed) sweeps to its right, cell by
/// cell. Both ends are inside the row's cells.
fn span(row: &mut [f32], xa: f32, xb: f32, d: f32) {
    let (x0, x1) = if xa < xb { (xa, xb) } else { (xb, xa) };
    // Both are positive: casts round down (and need no library call).
    let x0i = x0 as usize;
    let x0floor = x0i as f32;
    let x1i = if (x1 as usize) as f32 == x1 { x1 as usize } else { x1 as usize + 1 };
    let x1ceil = x1i as f32;
    if x1i <= x0i + 1 {
        // Within one cell: the part right of the edge's middle.
        let mid = 0.5 * (x0 + x1) - x0floor;
        row[x0i] += d - d * mid;
        row[x0i + 1] += d * mid;
    } else {
        // Across cells: a triangle in the first, a trapezoid in each one it
        // crosses, the rest of the triangle in the last.
        let s = (x1 - x0).recip();
        let x0f = x0 - x0floor;
        let a0 = 0.5 * s * (1.0 - x0f) * (1.0 - x0f);
        let x1f = x1 - x1ceil + 1.0;
        let am = 0.5 * s * x1f * x1f;
        row[x0i] += d * a0;
        if x1i == x0i + 2 {
            row[x0i + 1] += d * (1.0 - a0 - am);
        } else {
            let a1 = s * (1.5 - x0f);
            row[x0i + 1] += d * (a1 - a0);
            for cell in &mut row[x0i + 2..x1i - 1] {
                *cell += d * s;
            }
            let a2 = a1 + (x1i - x0i - 3) as f32 * s;
            row[x1i - 1] += d * (1.0 - a2 - am);
        }
        row[x1i] += d * am;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cover(area: Rect, contours: &[&[(f32, f32)]]) -> Vec<f32> {
        let mut r = Rasteriser::new();
        r.begin(area);
        for c in contours {
            r.contour(c.iter().copied());
        }
        r.finish().to_vec()
    }

    #[test]
    fn a_square_on_whole_pixels_is_solid_inside_and_clear_outside() {
        let cov = cover(Rect::new(0, 0, 6, 6), &[&[(1.0, 1.0), (4.0, 1.0), (4.0, 4.0), (1.0, 4.0)]]);
        for y in 0..6 {
            for x in 0..6 {
                let inside = (1..4).contains(&x) && (1..4).contains(&y);
                assert_eq!(cov[y * 6 + x], if inside { 1.0 } else { 0.0 }, "{x},{y}");
            }
        }
    }

    #[test]
    fn partial_pixels_get_their_exact_share() {
        // A square from 0.25 to 2.75: edges a quarter into their pixels.
        let cov = cover(Rect::new(0, 0, 3, 3), &[&[(0.25, 0.25), (2.75, 0.25), (2.75, 2.75), (0.25, 2.75)]]);
        assert!((cov[0] - 0.75 * 0.75).abs() < 1e-5, "{}", cov[0]);
        assert!((cov[1] - 0.75).abs() < 1e-5);
        assert!((cov[4] - 1.0).abs() < 1e-5);
        assert!((cov[8] - 0.75 * 0.75).abs() < 1e-5);
        // A triangle cutting a pixel along its diagonal covers half of it.
        let cov = cover(Rect::new(0, 0, 1, 1), &[&[(0.0, 0.0), (1.0, 1.0), (0.0, 1.0)]]);
        assert!((cov[0] - 0.5).abs() < 1e-5);
    }

    #[test]
    fn the_total_coverage_is_the_area_of_the_polygon() {
        // A star-free polygon with slanted edges in every direction.
        let poly = [(3.3, 1.2), (17.8, 4.1), (14.6, 15.9), (6.25, 12.7), (1.4, 8.05)];
        let shoelace: f32 =
            (0..poly.len()).map(|i| poly[i].0 * poly[(i + 1) % 5].1 - poly[(i + 1) % 5].0 * poly[i].1).sum::<f32>()
                / 2.0;
        let cov = cover(Rect::new(0, 0, 20, 20), &[&poly]);
        let total: f32 = cov.iter().sum();
        assert!((total - shoelace.abs()).abs() < 1e-3, "{total} vs {shoelace}");
        assert!(cov.iter().all(|c| (0.0..=1.0).contains(c)));
        // The same in the other direction, and placed in a larger area that
        // does not start at the origin.
        let reversed: Vec<(f32, f32)> = poly.iter().rev().map(|&(x, y)| (x + 100.0, y + 50.0)).collect();
        let cov = cover(Rect::new(97, 48, 30, 30), &[&reversed]);
        assert!((cov.iter().sum::<f32>() - shoelace.abs()).abs() < 1e-3);
    }

    #[test]
    fn non_zero_winding_fills_overlaps_and_leaves_holes() {
        let outer: &[(f32, f32)] = &[(0.0, 0.0), (8.0, 0.0), (8.0, 8.0), (0.0, 8.0)];
        let same_way: &[(f32, f32)] = &[(2.0, 2.0), (6.0, 2.0), (6.0, 6.0), (2.0, 6.0)];
        let other_way: &[(f32, f32)] = &[(2.0, 2.0), (2.0, 6.0), (6.0, 6.0), (6.0, 2.0)];
        let area = Rect::new(0, 0, 8, 8);
        // Winding two inside the overlap: still covered once.
        assert!(cover(area, &[outer, same_way]).iter().all(|&c| c == 1.0));
        // A contour the other way round cuts a hole.
        let ring = cover(area, &[outer, other_way]);
        assert_eq!(ring[4 * 8 + 4], 0.0);
        assert_eq!(ring[8 + 1], 1.0);
        assert_eq!(ring.iter().sum::<f32>(), 64.0 - 16.0);
    }

    #[test]
    fn polygons_reaching_outside_the_area_are_clipped_correctly() {
        // A large square of which only a corner is inside the area.
        let big: &[(f32, f32)] = &[(-50.0, -50.0), (2.5, -50.0), (2.5, 2.5), (-50.0, 2.5)];
        let cov = cover(Rect::new(0, 0, 4, 4), &[big]);
        assert_eq!(&cov[0..4], &[1.0, 1.0, 0.5, 0.0]);
        assert_eq!(&cov[8..12], &[0.5, 0.5, 0.25, 0.0]);
        assert!(cov[12..].iter().all(|&c| c == 0.0));
        // Wholly outside, and degenerate input: nothing, and no panic.
        let away: &[(f32, f32)] = &[(10.0, 10.0), (20.0, 10.0), (20.0, 20.0)];
        assert!(cover(Rect::new(0, 0, 4, 4), &[away]).iter().all(|&c| c == 0.0));
        let nan: &[(f32, f32)] = &[(f32::NAN, 0.0), (1.0, 1.0), (0.0, f32::INFINITY)];
        assert!(cover(Rect::new(0, 0, 4, 4), &[nan]).iter().all(|&c| c == 0.0));
        assert!(cover(Rect::new(0, 0, 0, 0), &[big]).is_empty());
    }

    #[test]
    fn the_buffers_are_reused() {
        let mut r = Rasteriser::new();
        r.begin(Rect::new(0, 0, 64, 64));
        r.contour([(1.0, 1.0), (60.0, 1.0), (30.0, 60.0)]);
        r.finish();
        let (acc, cov) = (r.acc.as_ptr(), r.cov.as_ptr());
        r.begin(Rect::new(5, 5, 32, 32));
        r.contour([(6.0, 6.0), (30.0, 6.0), (20.0, 30.0)]);
        let total: f32 = r.finish().iter().sum();
        assert!((total - 24.0 * 24.0 / 2.0).abs() < 1e-2, "{total}");
        assert_eq!((acc, cov), (r.acc.as_ptr(), r.cov.as_ptr()), "no new memory for a smaller area");
    }
}
