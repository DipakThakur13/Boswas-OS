//! The desktop background.
//!
//! The default background is drawn in code from the Prism palette: no image
//! file is decoded, it is exact at every resolution, and it is rendered
//! once per output size. A picture replaces it when `[appearance] wallpaper`
//! is set in the compositor configuration (a desktop preset writes it to
//! `~/.config/boswas/appearance.toml`); the compositor passes the path on.

use boswas_ui::{tokens, Canvas, Rgba, Theme};
use tiny_skia::Pixmap;

/// A soft light: `color` at full `strength` in the centre, fading to
/// nothing at `radius` (all in device pixels).
struct Glow {
    x: f32,
    y: f32,
    radius: f32,
    color: [f32; 3],
    strength: f32,
}

/// Interleaved gradient noise in [0, 1): a cheap, even dither pattern.
fn dither(x: u32, y: u32) -> f32 {
    let v = 0.067_110_56 * x as f32 + 0.005_837_15 * y as f32;
    (52.982_918 * v.fract()).fract()
}

/// Draws the default Boswas background onto a canvas: the theme's
/// background lit by three overlapping glows from the Prism palette. The
/// colours are computed per pixel in floating point and dithered when they
/// are quantised, so the wide gradients show no banding.
pub fn draw_default(canvas: &mut Canvas, theme: &Theme) {
    let t = tokens();
    let (pw, ph) = canvas.pixel_size();
    let (w, h) = (pw as f32, ph as f32);
    let unit = w.max(h);
    let rgb = |c: Rgba| [f32::from(c.r), f32::from(c.g), f32::from(c.b)];
    let color = |reference: &str| rgb(t.color(reference).unwrap_or(theme.accent));
    let strength = if theme.scheme == boswas_ui::Scheme::Dark { 1.0 } else { 0.5 };
    let glows = [
        Glow { x: w * 0.16, y: h * 1.04, radius: unit * 0.66, color: color("iris.600"), strength: 0.62 * strength },
        Glow { x: w * 0.94, y: h * 0.64, radius: unit * 0.54, color: color("azure.600"), strength: 0.46 * strength },
        Glow { x: w * 0.62, y: h * -0.12, radius: unit * 0.46, color: color("orchid.600"), strength: 0.30 * strength },
        Glow { x: w * 0.46, y: h * 0.58, radius: unit * 0.32, color: color("iris.400"), strength: 0.14 * strength },
    ];
    let base = rgb(theme.background);
    let data = canvas.pixmap_mut().data_mut();
    for y in 0..ph {
        let row = &mut data[(y * pw * 4) as usize..((y + 1) * pw * 4) as usize];
        for (x, pixel) in row.chunks_exact_mut(4).enumerate() {
            let mut c = base;
            for glow in &glows {
                let (dx, dy) = (x as f32 - glow.x, y as f32 - glow.y);
                let d2 = dx * dx + dy * dy;
                if d2 >= glow.radius * glow.radius {
                    continue;
                }
                // Smooth falloff with zero slope at the centre and the rim.
                let f = 1.0 - d2.sqrt() / glow.radius;
                let a = glow.strength * f * f * (3.0 - 2.0 * f);
                for i in 0..3 {
                    c[i] += (glow.color[i] - c[i]) * a;
                }
            }
            let noise = dither(x as u32, y) - 0.5;
            for i in 0..3 {
                pixel[i] = (c[i] + noise).round().clamp(0.0, 255.0) as u8;
            }
            pixel[3] = 255;
        }
    }
}

/// Scales a picture to cover the canvas, centred.
pub fn draw_picture(canvas: &mut Canvas, picture: &Pixmap) {
    let (w, h) = (canvas.width(), canvas.height());
    let (pw, ph) = (picture.width() as f32, picture.height() as f32);
    let scale = (w / pw).max(h / ph);
    let (dw, dh) = (pw * scale, ph * scale);
    canvas.draw_pixmap(picture, (w - dw) / 2.0, (h - dh) / 2.0, dw, dh, 1.0);
}

/// The background for a surface of this size, rendered once and reused by
/// the wallpaper, the lock screen and the greeter.
#[derive(Default)]
pub struct Background {
    cached: Option<(u32, u32, Pixmap)>,
    picture: Option<Pixmap>,
}

impl Background {
    /// Uses a picture instead of the drawn background.
    pub fn set_picture(&mut self, picture: Option<Pixmap>) {
        self.picture = picture;
        self.cached = None;
    }

    /// Loads the picture at `path`, or returns to the drawn background when
    /// `path` is empty or cannot be used. The file is decoded here, in the
    /// unprivileged shell, never in the compositor.
    pub fn load_picture(&mut self, path: &str) {
        /// Largest wallpaper file that is read.
        const MAX_BYTES: u64 = 64 * 1024 * 1024;
        let picture = (!path.is_empty() && std::path::Path::new(path).is_absolute())
            .then(|| std::fs::metadata(path).ok().filter(|m| m.is_file() && m.len() <= MAX_BYTES))
            .flatten()
            .and_then(|_| std::fs::read(path).ok())
            .and_then(|bytes| boswas_ui::icons::decode_image(&bytes));
        if picture.is_none() && !path.is_empty() {
            tracing::warn!("wallpaper {path} could not be used; showing the default background");
        }
        self.set_picture(picture);
    }

    /// A reduced rendering of the background for an output of `width` x
    /// `height` logical pixels: what the materials are blurred from.
    pub fn reduced(&self, theme: &Theme, width: f32, height: f32) -> Option<Pixmap> {
        let scale = (320.0 / width.max(1.0)).min(1.0);
        let mut canvas = Canvas::new(width, height, scale)?;
        match &self.picture {
            Some(picture) => {
                canvas.clear(theme.background);
                draw_picture(&mut canvas, picture);
            }
            None => draw_default(&mut canvas, theme),
        }
        Some(canvas.into_pixmap())
    }

    /// Fills the canvas with the background.
    pub fn paint(&mut self, canvas: &mut Canvas, theme: &Theme) {
        let (pw, ph) = canvas.pixel_size();
        let fresh = !matches!(&self.cached, Some((w, h, _)) if (*w, *h) == (pw, ph));
        if fresh {
            // Drawn whole on a canvas of its own, then copied (also when
            // only a part of `canvas` is being drawn again).
            let Some(mut whole) = Canvas::new(canvas.width(), canvas.height(), canvas.scale()) else {
                return;
            };
            match &self.picture {
                Some(picture) => {
                    whole.clear(theme.background);
                    draw_picture(&mut whole, picture);
                }
                None => draw_default(&mut whole, theme),
            }
            self.cached = Some((pw, ph, whole.into_pixmap()));
        }
        if let Some((_, _, pixmap)) = &self.cached {
            canvas.copy_from_full(pixmap);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_default_background_is_opaque_and_not_flat() {
        let mut canvas = Canvas::new(320.0, 200.0, 1.0).unwrap();
        draw_default(&mut canvas, Theme::dark());
        assert!(canvas.data().chunks_exact(4).all(|p| p[3] == 255), "no transparent pixel");
        let corner = canvas.pixmap().pixel(319, 0).unwrap();
        let glow = canvas.pixmap().pixel(40, 190).unwrap();
        assert!(glow.blue() > corner.blue() + 20, "the lower left carries the iris glow");
    }

    #[test]
    fn wide_gradients_are_dithered() {
        let mut canvas = Canvas::new(400.0, 40.0, 1.0).unwrap();
        draw_default(&mut canvas, Theme::dark());
        // Along a row, neighbouring pixels differ by at most a couple of
        // levels, and the pattern is not constant over long runs.
        let row: Vec<u8> = (0..400).map(|x| canvas.pixmap().pixel(x, 20).unwrap().blue()).collect();
        assert!(row.windows(2).all(|p| p[0].abs_diff(p[1]) <= 3));
        let longest_run = row.chunk_by(|a, b| a == b).map(<[u8]>::len).max().unwrap();
        assert!(longest_run < 40, "a flat band of {longest_run} pixels");
        for v in [dither(0, 0), dither(17, 3), dither(1919, 1079)] {
            assert!((0.0..1.0).contains(&v));
        }
    }

    #[test]
    fn the_background_is_cached_per_size() {
        let mut background = Background::default();
        let mut a = Canvas::new(64.0, 48.0, 1.0).unwrap();
        background.paint(&mut a, Theme::dark());
        let first = a.data().to_vec();
        let mut b = Canvas::new(64.0, 48.0, 1.0).unwrap();
        background.paint(&mut b, Theme::dark());
        assert_eq!(first, b.data(), "the cached copy equals the first rendering");
        let mut c = Canvas::new(32.0, 48.0, 1.0).unwrap();
        background.paint(&mut c, Theme::dark());
        assert_eq!(c.pixel_size(), (32, 48));
    }

    #[test]
    fn unusable_pictures_fall_back_to_the_default() {
        let mut background = Background::default();
        for path in ["", "relative.png", "/nonexistent/picture.png", "/etc/passwd"] {
            background.load_picture(path);
            assert!(background.picture.is_none(), "{path}");
        }
    }

    #[test]
    fn a_picture_covers_the_canvas() {
        let mut picture = Pixmap::new(10, 40).unwrap();
        picture.fill(tiny_skia::Color::from_rgba8(200, 10, 10, 255));
        let mut canvas = Canvas::new(100.0, 50.0, 1.0).unwrap();
        draw_picture(&mut canvas, &picture);
        for (x, y) in [(0, 0), (99, 0), (0, 49), (99, 49), (50, 25)] {
            let p = canvas.pixmap().pixel(x, y).unwrap();
            assert_eq!((p.red(), p.alpha()), (200, 255), "({x},{y})");
        }
    }
}
