//! The background: `neutral.950` with a soft bloom of the brand hues behind
//! the mark, as on the boot menu (`desktop/branding/boot-splash.svg`): azure
//! up and to the left of the mark, where the light comes from, and iris
//! below it to the right.
//!
//! Each bloom fades smoothly from its centre to nothing at its edge. The
//! change from one shade to the next is far less than the step between two
//! 8-bit values over most of the screen, which would show as bands; so a
//! fixed pattern of noise of less than half a step is added before the
//! colour is rounded (dithering). The noise depends on the pixel only, so
//! any part of the background is painted the same every time, and a flat
//! area stays exactly `neutral.950`.
//!
//! The background is painted once with the first frame; after that only
//! the parts under things that change are painted again.

use crate::assets;
use crate::gfx::{Canvas, Rect, Rgb};

/// Peak opacity of the azure and the iris bloom, their distance from the
/// mark's centre and their radius, in sides of the mark's square box (the
/// boot menu's proportions, with the azure raised a little towards the
/// light).
const AZURE: Bloom = Bloom { peak: 0.2, at: (-0.5, -0.2), radius: 3.75 };
const IRIS: Bloom = Bloom { peak: 0.13, at: (0.8, 0.68), radius: 3.4 };

#[derive(Clone, Copy)]
struct Bloom {
    peak: f32,
    at: (f32, f32),
    radius: f32,
}

/// One bloom placed on a display.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Light {
    cx: f32,
    cy: f32,
    radius: f32,
    inv_r2: f32,
    peak: f32,
    colour: [f32; 3],
}

impl Light {
    fn new(bloom: Bloom, centre: (f32, f32), side: f32, colour: Rgb) -> Self {
        let radius = (bloom.radius * side).max(1.0);
        Self {
            cx: centre.0 + bloom.at.0 * side,
            cy: centre.1 + bloom.at.1 * side,
            radius,
            inv_r2: 1.0 / (radius * radius),
            peak: bloom.peak,
            colour: [colour.r as f32, colour.g as f32, colour.b as f32],
        }
    }

    /// The columns of row `y` (pixel centres) that this bloom reaches,
    /// with a pixel to spare.
    fn reach(&self, y: f32) -> Option<(f32, f32)> {
        let dy = y - self.cy;
        let half = self.radius * self.radius - dy * dy;
        (half > 0.0).then(|| {
            let half = half.sqrt() + 1.0;
            (self.cx - half, self.cx + half)
        })
    }
}

/// The background of one display.
#[derive(Clone, Debug, PartialEq)]
pub struct Backdrop {
    base: Rgb,
    lights: [Light; 2],
}

/// White noise in 0..1 from the pixel's position alone (an integer hash):
/// a fixed pattern without structure, so that it shows no moiré even when a
/// picture of the screen is scaled down.
fn noise(x: i32, y: i32) -> f32 {
    let mut h = (x as u32).wrapping_mul(0x8da6_b343) ^ (y as u32).wrapping_mul(0xd816_3841);
    h ^= h >> 15;
    h = h.wrapping_mul(0x2c1b_3c6d);
    h ^= h >> 12;
    h = h.wrapping_mul(0x297a_2d39);
    h ^= h >> 15;
    (h >> 8) as f32 / (1u32 << 24) as f32
}

impl Backdrop {
    /// The background behind the mark's box, `side` pixels square and
    /// centred at `centre`.
    pub fn new(centre: (f32, f32), side: f32) -> Self {
        Self {
            base: assets::BACKGROUND,
            lights: [
                Light::new(AZURE, centre, side, assets::BLOOM_AZURE),
                Light::new(IRIS, centre, side, assets::BLOOM_IRIS),
            ],
        }
    }

    /// The flat colour where no bloom reaches.
    pub fn base(&self) -> Rgb {
        self.base
    }

    /// The colour of one pixel.
    #[inline]
    pub fn pixel(&self, x: i32, y: i32) -> Rgb {
        let c = self.exact(x, y);
        // Noise of just under half a step either way: a whole value stays
        // what it is, anything between two values is shared out between them.
        let n = (noise(x, y) - 0.5) * 0.998;
        // The values are positive: the cast rounds down, and saturates.
        let q = |v: f32| (v + 0.5 + n) as u8;
        Rgb::new(q(c[0]), q(c[1]), q(c[2]))
    }

    /// The exact colour at the centre of a pixel, before it is rounded.
    #[inline]
    fn exact(&self, x: i32, y: i32) -> [f32; 3] {
        let (fx, fy) = (x as f32 + 0.5, y as f32 + 0.5);
        let mut c = [self.base.r as f32, self.base.g as f32, self.base.b as f32];
        for light in &self.lights {
            let (dx, dy) = (fx - light.cx, fy - light.cy);
            let q = 1.0 - (dx * dx + dy * dy) * light.inv_r2;
            if q > 0.0 {
                // A smooth fall-off: flat at the centre, no edge at the rim.
                let a = light.peak * q * q;
                for (channel, target) in c.iter_mut().zip(light.colour) {
                    *channel += (target - *channel) * a;
                }
            }
        }
        c
    }

    /// Paint the background into `rect` of the canvas.
    pub fn fill(&self, canvas: &mut Canvas, rect: &Rect) {
        let r = rect.intersect(&canvas.bounds());
        let flat = self.base.xrgb();
        for y in r.y..r.bottom() {
            // Where the blooms reach on this row: pixels outside are flat.
            let centre = y as f32 + 0.5;
            let reach = self.lights.iter().filter_map(|l| l.reach(centre)).reduce(|a, b| (a.0.min(b.0), a.1.max(b.1)));
            let (from, to) = match reach {
                Some((a, b)) => ((a.floor() as i32).clamp(r.x, r.right()), (b.ceil() as i32).clamp(r.x, r.right())),
                None => (r.x, r.x),
            };
            let row = canvas.row_mut(y as u32, r.x as u32, r.w as u32);
            for (x, px) in (r.x..).zip(row.iter_mut()) {
                *px = if x >= from && x < to { self.pixel(x, y).xrgb() } else { flat };
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn backdrop() -> Backdrop {
        Backdrop::new((960.0, 475.0), 190.0)
    }

    #[test]
    fn far_from_the_mark_it_is_the_flat_background() {
        let b = backdrop();
        for (x, y) in [(0, 0), (1919, 0), (0, 1079), (1919, 1079), (100, 540)] {
            assert_eq!(b.pixel(x, y), assets::BACKGROUND, "{x},{y}");
        }
        // Behind the mark: brighter, and blue.
        let p = b.pixel(900, 450);
        let bg = assets::BACKGROUND;
        assert!(p.b > bg.b + 25 && p.g > bg.g + 8 && p.r >= bg.r, "{p:?}");
    }

    #[test]
    fn filling_paints_exactly_what_pixel_says() {
        let b = backdrop();
        let mut canvas = Canvas::new(1920, 1080);
        let all = canvas.bounds();
        b.fill(&mut canvas, &all);
        for y in (0..1080).step_by(7) {
            for x in (0..1920).step_by(3) {
                assert_eq!(canvas.pixel(x, y), b.pixel(x as i32, y as i32), "{x},{y}");
            }
        }
        // A part painted again is the same.
        let before = canvas.pixels().to_vec();
        b.fill(&mut canvas, &Rect::new(850, 400, 300, 300));
        assert!(canvas.pixels() == before);
        // Outside the canvas: nothing happens.
        b.fill(&mut canvas, &Rect::new(-50, -50, 20, 20));
    }

    #[test]
    fn the_bloom_is_dithered_not_banded() {
        // Across the bloom the exact shade changes by far less than a step
        // per pixel. Rounded alone, it would be bands: runs of one value,
        // off from the shade by up to half a step. Dithered, the average of
        // a few pixels follows the shade closely, and no pixel is more than
        // a step from it.
        let b = backdrop();
        let (mut worst_dithered, mut worst_rounded) = (0f32, 0f32);
        for y in [200, 450, 700] {
            let exact: Vec<[f32; 3]> = (200..1100).map(|x| b.exact(x, y)).collect();
            let dithered: Vec<Rgb> = (200..1100).map(|x| b.pixel(x, y)).collect();
            for k in 0..3 {
                let channel = |c: &Rgb| [c.r, c.g, c.b][k] as f32;
                for (e, d) in exact.iter().zip(&dithered) {
                    assert!((channel(d) - e[k]).abs() < 1.0, "{d:?} for {e:?}");
                }
                for (e, d) in exact.chunks(64).zip(dithered.chunks(64)) {
                    let mean = |v: &mut dyn Iterator<Item = f32>| v.sum::<f32>() / 64.0;
                    let shade = mean(&mut e.iter().map(|e| e[k]));
                    worst_dithered = worst_dithered.max((mean(&mut d.iter().map(channel)) - shade).abs());
                    worst_rounded = worst_rounded.max((mean(&mut e.iter().map(|e| e[k].round())) - shade).abs());
                }
            }
        }
        assert!(worst_dithered < 0.25, "dithered: off by {worst_dithered} on average");
        assert!(worst_rounded > worst_dithered * 1.5, "rounded: {worst_rounded}, dithered: {worst_dithered}");
    }

    #[test]
    fn the_noise_is_fixed_and_spread() {
        assert_eq!(noise(17, 33), noise(17, 33));
        let samples: Vec<f32> = (0..64).flat_map(|y| (0..64).map(move |x| noise(x, y))).collect();
        assert!(samples.iter().all(|n| (0.0..1.0).contains(n)));
        let mean = samples.iter().sum::<f32>() / samples.len() as f32;
        assert!((mean - 0.5).abs() < 0.02, "{mean}");
    }
}
