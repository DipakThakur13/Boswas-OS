//! The desktop background.
//!
//! The default background is drawn in code from the Prism palette: no image
//! file is decoded, it is exact at every resolution, and it is rendered
//! once per output size. A picture replaces it when `[appearance] wallpaper`
//! is set in the compositor configuration (a desktop preset writes it to
//! `~/.config/boswas/appearance.toml`); the compositor passes the path on.

use std::path::{Path, PathBuf};

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
    fract(52.982_918 * fract(v))
}

/// `v.fract()` for `v` from 0 to 2^31, without a call into the C library
/// (on processors without a rounding instruction `fract` is one); the
/// result is the same.
fn fract(v: f32) -> f32 {
    v - v as i32 as f32
}

/// `v.round()` (halves away from zero) for `v` from -2^31 to 2^31, without
/// a call into the C library; the result is the same.
fn round(v: f32) -> f32 {
    let t = v as i32 as f32;
    match v - t {
        f if f >= 0.5 => t + 1.0,
        f if f <= -0.5 => t - 1.0,
        _ => t,
    }
}

/// Pixels below which the background is drawn on one thread.
const THREADED_PIXELS: u32 = 512 * 512;

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
    // A whole screen is a few million pixels (at 2560 x 1440 a tenth of a
    // second on one core, which the lock screen's first frame waited for):
    // bands of rows are drawn side by side.
    let threads = if pw * ph < THREADED_PIXELS { 1 } else { std::thread::available_parallelism().map_or(1, |n| n.get()).min(4) };
    let band_rows = ph.div_ceil(threads as u32).max(1);
    let band_bytes = (band_rows * pw * 4) as usize;
    if threads == 1 {
        return draw_rows(data, 0, pw, &glows, base);
    }
    std::thread::scope(|scope| {
        for (band, rows) in data.chunks_mut(band_bytes).enumerate() {
            let glows = &glows;
            scope.spawn(move || draw_rows(rows, band as u32 * band_rows, pw, glows, base));
        }
    });
}

/// The rows of the default background from row `first` on, each `width`
/// pixels.
fn draw_rows(rows: &mut [u8], first: u32, width: u32, glows: &[Glow; 4], base: [f32; 3]) {
    for (index, row) in rows.chunks_exact_mut(width as usize * 4).enumerate() {
        let y = first + index as u32;
        // The columns each glow can reach on this row, a pixel or two wider
        // than exact: the test below still decides, as before.
        let spans = glows.each_ref().map(|glow| {
            let dy = y as f32 - glow.y;
            let rest = glow.radius * glow.radius - dy * dy;
            if rest <= 0.0 {
                return (0, 0);
            }
            let half = rest.sqrt();
            let from = ((glow.x - half).floor() - 1.0).max(0.0) as usize;
            let to = ((glow.x + half).ceil() + 2.0).clamp(0.0, width as f32) as usize;
            (from, to)
        });
        for (x, pixel) in row.chunks_exact_mut(4).enumerate() {
            let mut c = base;
            for (glow, &(from, to)) in glows.iter().zip(&spans) {
                if x < from || x >= to {
                    continue;
                }
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
                pixel[i] = round(c[i] + noise).clamp(0.0, 255.0) as u8;
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
    /// The wallpaper asked for (empty: the drawn background).
    path: String,
    /// The picture at `path` was not decoded: the background the desktop
    /// shared is that picture (see `adopt_shared`); it is decoded only if
    /// it is needed at another size.
    undecoded: bool,
    /// What the shared background `cached` holds came from: the wallpaper
    /// and the scheme.
    adopted: Option<(String, bool)>,
    /// Where the background is shared once drawn (the desktop only).
    share: Option<PathBuf>,
    /// The shared background being read, on a thread of its own while the
    /// lock screen connects (see `settle`).
    reading: Option<std::thread::JoinHandle<Option<(Pixmap, String, bool)>>>,
}

impl Background {
    /// Uses a picture instead of the drawn background.
    pub fn set_picture(&mut self, picture: Option<Pixmap>) {
        self.picture = picture;
        self.cached = None;
        self.undecoded = false;
        self.adopted = None;
    }

    /// Loads the picture at `path`, or returns to the drawn background when
    /// `path` is empty or cannot be used. The file is decoded here, in the
    /// unprivileged shell, never in the compositor.
    pub fn load_picture(&mut self, path: &str) {
        /// Largest wallpaper file that is read.
        const MAX_BYTES: u64 = 64 * 1024 * 1024;
        self.settle();
        if self.adopted.as_ref().is_some_and(|(shared, _)| shared == path) {
            // The background the desktop shared is this picture, drawn.
            self.path = path.to_owned();
            self.undecoded = !path.is_empty();
            return;
        }
        let picture = (!path.is_empty() && std::path::Path::new(path).is_absolute())
            .then(|| std::fs::metadata(path).ok().filter(|m| m.is_file() && m.len() <= MAX_BYTES))
            .flatten()
            .and_then(|_| std::fs::read(path).ok())
            .and_then(|bytes| boswas_ui::icons::decode_image(&bytes));
        if picture.is_none() && !path.is_empty() {
            tracing::warn!("wallpaper {path} could not be used; showing the default background");
        }
        self.set_picture(picture);
        self.path = path.to_owned();
    }

    /// A reduced rendering of the background for an output of `width` x
    /// `height` logical pixels: what the materials are blurred from.
    pub fn reduced(&self, theme: &Theme, width: f32, height: f32) -> Option<Pixmap> {
        let scale = (320.0 / width.max(1.0)).min(1.0);
        let mut canvas = Canvas::new(width, height, scale)?;
        let shared = self.cached.as_ref().filter(|_| self.undecoded).map(|(_, _, pixmap)| pixmap);
        match self.picture.as_ref().or(shared) {
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
        self.settle();
        let (pw, ph) = canvas.pixel_size();
        let light = theme.scheme == boswas_ui::Scheme::Light;
        let sized = matches!(&self.cached, Some((w, h, _)) if (*w, *h) == (pw, ph));
        // A shared drawn background is of one scheme (a picture is the same
        // in both).
        let adopted_fits = self.adopted.as_ref().is_none_or(|(path, shared_light)| !path.is_empty() || *shared_light == light);
        if !sized || !adopted_fits {
            if self.undecoded {
                let path = std::mem::take(&mut self.path);
                self.adopted = None;
                self.load_picture(&path);
            }
            self.adopted = None;
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
            let pixmap = whole.into_pixmap();
            if let Some(file) = &self.share {
                shared::write(file.clone(), &pixmap, &self.path, light);
            }
            self.cached = Some((pw, ph, pixmap));
        }
        if let Some((_, _, pixmap)) = &self.cached {
            canvas.copy_from_full(pixmap);
        }
    }

    /// The desktop: every background drawn is shared in `file`, for the
    /// lock screen.
    pub fn share_in(&mut self, file: PathBuf) {
        self.share = Some(file);
    }

    /// The lock screen: takes the background the desktop shared in `file`,
    /// so that its first frame shows at once, without drawing the
    /// background or decoding the picture again. Used while its size and
    /// its wallpaper are those of the lock screen.
    pub fn adopt_shared(&mut self, file: &Path) {
        let file = file.to_owned();
        self.reading = std::thread::Builder::new().name("background".into()).spawn(move || shared::read(&file)).ok();
    }

    /// Takes the shared background once it is read (waiting for the read
    /// if it is still going on); drawing and loading a picture do this
    /// first.
    pub fn settle(&mut self) {
        let Some(reading) = self.reading.take() else {
            return;
        };
        if let Some((pixmap, path, light)) = reading.join().ok().flatten() {
            self.path = path.clone();
            self.undecoded = !path.is_empty();
            self.cached = Some((pixmap.width(), pixmap.height(), pixmap));
            self.adopted = Some((path, light));
        }
    }
}

/// The background the desktop drew last, in a file of the user's runtime
/// directory: the lock screen is a process of its own, started when the
/// session locks, and showed its first frame only after drawing the
/// background again (a tenth of a second at 2560 x 1440) or decoding the
/// picture. The file holds the drawing as it is (premultiplied RGBA), with
/// what it was drawn from; it is replaced whole (written aside, then
/// renamed), and read with limits: a file that is not one of these, or too
/// large, is ignored. Anything the user's programs can write, they could
/// as well set as the wallpaper, so the file is not a way past the lock.
mod shared {
    use std::{
        io::{Read, Write},
        os::unix::fs::OpenOptionsExt,
        path::{Path, PathBuf},
    };

    use tiny_skia::{IntSize, Pixmap};

    const MAGIC: &[u8; 8] = b"BWSBG001";
    /// Largest drawing shared (a 5K screen; larger ones are drawn again).
    pub const MAX_BYTES: usize = 64 * 1024 * 1024;
    const MAX_PATH: usize = 4096;

    /// Header: magic, width, height, scheme, length of the wallpaper's path.
    const HEADER: usize = 8 + 4 + 4 + 1 + 4;

    pub fn encode(pixmap: &Pixmap, path: &str, light: bool) -> Option<Vec<u8>> {
        if pixmap.data().len() > MAX_BYTES || path.len() > MAX_PATH {
            return None;
        }
        let mut bytes = Vec::with_capacity(HEADER + path.len() + pixmap.data().len());
        bytes.extend_from_slice(MAGIC);
        bytes.extend_from_slice(&pixmap.width().to_le_bytes());
        bytes.extend_from_slice(&pixmap.height().to_le_bytes());
        bytes.push(u8::from(light));
        bytes.extend_from_slice(&(path.len() as u32).to_le_bytes());
        bytes.extend_from_slice(path.as_bytes());
        bytes.extend_from_slice(pixmap.data());
        Some(bytes)
    }

    /// The drawing, its wallpaper and scheme, if `bytes` are a whole and
    /// valid file. Every pixel is made opaque (a background is), so no
    /// value in the file can be out of range for drawing.
    pub fn decode(mut bytes: Vec<u8>) -> Option<(Pixmap, String, bool)> {
        let word = |at: usize| -> Option<u32> { Some(u32::from_le_bytes(bytes.get(at..at + 4)?.try_into().ok()?)) };
        if bytes.get(..8)? != MAGIC {
            return None;
        }
        let (w, h, path_len) = (word(8)?, word(12)?, word(17)? as usize);
        let light = match bytes.get(16)? {
            0 => false,
            1 => true,
            _ => return None,
        };
        let size = IntSize::from_wh(w, h)?;
        let pixels = (w as usize).checked_mul(h as usize)?.checked_mul(4)?;
        if w > 16384 || h > 16384 || pixels > MAX_BYTES || path_len > MAX_PATH || bytes.len() != HEADER + path_len + pixels {
            return None;
        }
        let path = String::from_utf8(bytes.get(HEADER..HEADER + path_len)?.to_vec()).ok()?;
        let mut data = bytes.split_off(HEADER + path_len);
        for pixel in data.chunks_exact_mut(4) {
            pixel[3] = 255;
        }
        Some((Pixmap::from_vec(data, size)?, path, light))
    }

    /// Writes the shared background on a thread of its own (the desktop
    /// goes on drawing meanwhile).
    pub fn write(file: PathBuf, pixmap: &Pixmap, path: &str, light: bool) {
        let Some(bytes) = encode(pixmap, path, light) else {
            let _ = std::fs::remove_file(&file);
            return;
        };
        let _ = std::thread::Builder::new().name("background".into()).spawn(move || {
            let aside = file.with_extension(format!("{}.part", std::process::id()));
            let written = std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&aside)
                .and_then(|mut out| out.write_all(&bytes));
            match written {
                Ok(()) => {
                    if std::fs::rename(&aside, &file).is_err() {
                        let _ = std::fs::remove_file(&aside);
                    }
                }
                Err(err) => {
                    let _ = std::fs::remove_file(&aside);
                    tracing::debug!("the background could not be shared: {err}");
                }
            }
        });
    }

    /// Reads the shared background, if there is a valid one.
    pub fn read(file: &Path) -> Option<(Pixmap, String, bool)> {
        let mut input = std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(file).ok()?;
        let meta = input.metadata().ok()?;
        if !meta.is_file() || meta.len() > (HEADER + MAX_PATH + MAX_BYTES) as u64 {
            return None;
        }
        let mut bytes = Vec::with_capacity(meta.len() as usize);
        Read::by_ref(&mut input).take((HEADER + MAX_PATH + MAX_BYTES) as u64 + 1).read_to_end(&mut bytes).ok()?;
        decode(bytes)
    }
}

/// The file the desktop shares its background in, in the user's runtime
/// directory.
pub fn shared_file() -> Option<PathBuf> {
    let dir = PathBuf::from(std::env::var_os("XDG_RUNTIME_DIR")?);
    dir.is_absolute().then(|| dir.join("boswas-shell-background"))
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

    /// The straightforward drawing (one thread, every glow tested at every
    /// pixel, the standard library's rounding): what the background was
    /// before it was made fast.
    fn draw_reference(canvas: &mut Canvas, theme: &Theme) {
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
                    let f = 1.0 - d2.sqrt() / glow.radius;
                    let a = glow.strength * f * f * (3.0 - 2.0 * f);
                    for i in 0..3 {
                        c[i] += (glow.color[i] - c[i]) * a;
                    }
                }
                let v = 0.067_110_56 * x as f32 + 0.005_837_15 * y as f32;
                let noise = (52.982_918 * v.fract()).fract() - 0.5;
                for i in 0..3 {
                    pixel[i] = (c[i] + noise).round().clamp(0.0, 255.0) as u8;
                }
                pixel[3] = 255;
            }
        }
    }

    #[test]
    fn the_fast_background_is_the_straightforward_one_pixel_for_pixel() {
        for (w, h, scale) in [(1280.0, 720.0, 2.0), (1536.0, 864.0, 1.25), (300.0, 200.0, 1.0), (1366.0, 768.0, 1.0)] {
            for theme in [Theme::dark(), Theme::light()] {
                let mut fast = Canvas::new(w, h, scale).unwrap();
                draw_default(&mut fast, theme);
                let mut reference = Canvas::new(w, h, scale).unwrap();
                draw_reference(&mut reference, theme);
                assert!(fast.data() == reference.data(), "{w} x {h} at {scale}");
            }
        }
        for v in [-0.5f32, -0.49999997, -0.3, 0.0, 0.49999997, 0.5, 1.5, 2.5, 254.5, 255.49998, 7.999999] {
            assert_eq!(round(v), v.round(), "{v}");
        }
        for v in [0.0f32, 0.25, 1.75, 600.000_06, 52.982_918] {
            assert_eq!(fract(v), v.fract(), "{v}");
        }
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
    fn the_shared_background_is_read_back_only_whole_and_valid() {
        let mut canvas = Canvas::new(40.0, 30.0, 2.0).unwrap();
        draw_default(&mut canvas, Theme::dark());
        let pixmap = canvas.into_pixmap();
        let bytes = shared::encode(&pixmap, "/usr/share/w.png", true).unwrap();
        let (back, path, light) = shared::decode(bytes.clone()).unwrap();
        assert_eq!((back.data(), path.as_str(), light), (pixmap.data(), "/usr/share/w.png", true));
        // Truncated, padded, another magic, an impossible scheme, a size
        // that does not match the pixels: ignored.
        assert!(shared::decode(bytes[..bytes.len() - 1].to_vec()).is_none());
        assert!(shared::decode([bytes.as_slice(), &[0]].concat()).is_none());
        let mut other = bytes.clone();
        other[0] = b'X';
        assert!(shared::decode(other).is_none());
        let mut other = bytes.clone();
        other[16] = 7;
        assert!(shared::decode(other).is_none());
        let mut other = bytes.clone();
        other[8..12].copy_from_slice(&81u32.to_le_bytes());
        assert!(shared::decode(other).is_none());
        let mut huge = bytes[..21].to_vec();
        huge[8..12].copy_from_slice(&16384u32.to_le_bytes());
        huge[12..16].copy_from_slice(&16384u32.to_le_bytes());
        assert!(shared::decode(huge).is_none(), "larger than the limit");
        // Values that are not premultiplied cannot come in: a background is
        // opaque.
        let mut odd = bytes;
        let first = odd.len() - pixmap.data().len();
        odd[first + 3] = 0;
        let (back, _, _) = shared::decode(odd).unwrap();
        assert!(back.data().chunks_exact(4).all(|p| p[3] == 255));
    }

    #[test]
    fn the_lock_screen_shows_the_background_the_desktop_shared() {
        let dir = tempfile_dir();
        let file = dir.join("boswas-shell-background");
        let mut desktop = Background::default();
        desktop.share_in(file.clone());
        let mut drawn = Canvas::new(64.0, 48.0, 2.0).unwrap();
        desktop.paint(&mut drawn, Theme::dark());
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while !file.exists() && std::time::Instant::now() < deadline {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        let mut lock = Background::default();
        lock.adopt_shared(&file);
        lock.settle();
        assert!(lock.cached.is_some(), "the shared background was taken");
        // The configuration names the same wallpaper (here the drawn one):
        // nothing is drawn or decoded again, and the pixels are the desktop's.
        lock.load_picture("");
        let mut shown = Canvas::new(64.0, 48.0, 2.0).unwrap();
        lock.paint(&mut shown, Theme::dark());
        assert_eq!(shown.data(), drawn.data());
        // The other scheme, or another size: drawn anew.
        let mut light = Canvas::new(64.0, 48.0, 2.0).unwrap();
        lock.paint(&mut light, Theme::light());
        let mut expected = Canvas::new(64.0, 48.0, 2.0).unwrap();
        draw_default(&mut expected, Theme::light());
        assert_eq!(light.data(), expected.data());
        // A shared picture is used for that picture only, and decoded when
        // it is needed at another size (here it cannot be: the default).
        let mut lock = Background::default();
        lock.cached = Some((128, 96, drawn.pixmap().clone()));
        lock.adopted = Some(("/nonexistent/w.png".into(), false));
        lock.load_picture("/nonexistent/w.png");
        assert!(lock.undecoded && lock.cached.is_some(), "not decoded: the shared drawing is that picture");
        assert!(lock.reduced(Theme::dark(), 64.0, 48.0).is_some());
        let mut other = Canvas::new(32.0, 24.0, 2.0).unwrap();
        lock.paint(&mut other, Theme::dark());
        assert!(!lock.undecoded && lock.adopted.is_none());
        let mut lock = Background::default();
        lock.adopted = Some(("/nonexistent/w.png".into(), false));
        lock.load_picture("/other.png");
        assert!(lock.adopted.is_none(), "another wallpaper: the shared one is dropped");
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn tempfile_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("bwbg-{}-{:?}", std::process::id(), std::thread::current().id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
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
