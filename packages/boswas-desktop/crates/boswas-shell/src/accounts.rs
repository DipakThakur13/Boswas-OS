//! Account pictures: the picture chosen in Settings > Accounts > "Change
//! picture…", in Start's user button, on the lock screen and on the login
//! screen.
//!
//! Settings saves the picture as `~/.face` and hands it to AccountsService
//! (`SetIconFile`), which keeps a copy in `/var/lib/AccountsService/icons/
//! LOGIN`, readable by everyone (the login screen runs as another user).
//! The newer of the two that exists is shown; the login screen can read
//! only the service's copy. A file is decoded within the shell's limits
//! (a few megabytes, 8192 pixels on a side), cropped to a centred square
//! and kept as a round picture of [`PICTURE_PX`] pixels. Whether it changed
//! is a `stat`, done when Start opens and when a lock or login screen
//! starts; without a picture the initials or the person glyph stay.

use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    rc::Rc,
    time::SystemTime,
};

use tiny_skia::{FillRule, FilterQuality, Mask, Pixmap, PixmapPaint, Transform};

use crate::ui::Frame;

/// Where AccountsService keeps the pictures of the accounts.
pub const SERVICE_ICONS: &str = "/var/lib/AccountsService/icons";
/// Largest picture file read.
pub const MAX_PICTURE_BYTES: u64 = 8 * 1024 * 1024;
/// The side of the kept round picture (enough for the lock screen's
/// 120 px circle at 200 %).
pub const PICTURE_PX: u32 = 256;

/// A file and what `stat` said about it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stamp {
    pub path: PathBuf,
    pub modified: SystemTime,
    pub len: u64,
}

/// The files a picture of `login` may be in, in order of preference when
/// equally new: the service's copy, then `~/.face` (only for the account
/// the shell runs as: `home` is that account's home).
pub fn candidates(login: &str, home: Option<&Path>, service: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if is_login_name(login) {
        out.push(service.join(login));
    }
    if let Some(home) = home {
        out.push(home.join(".face"));
        out.push(home.join(".face.icon"));
    }
    out
}

/// A login name that is safe as a file name.
fn is_login_name(login: &str) -> bool {
    !login.is_empty() && login.len() <= 64 && login != "." && login != ".." && login.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.' | '$'))
}

/// The newest of the files that exist as regular files of a sensible size.
pub fn newest(candidates: &[PathBuf]) -> Option<Stamp> {
    let mut best: Option<Stamp> = None;
    for path in candidates {
        let Ok(meta) = std::fs::metadata(path) else {
            continue;
        };
        if !meta.is_file() || meta.len() == 0 || meta.len() > MAX_PICTURE_BYTES {
            continue;
        }
        let modified = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
        if best.as_ref().is_none_or(|b| modified > b.modified) {
            best = Some(Stamp { path: path.clone(), modified, len: meta.len() });
        }
    }
    best
}

/// Decodes a picture and makes it round: the centred square of the image,
/// scaled to `size` pixels, outside the circle transparent.
pub fn round_picture(data: &[u8], size: u32) -> Option<Pixmap> {
    let source = boswas_ui::icons::decode_image(data)?;
    round(&source, size)
}

/// The round picture of a decoded image.
pub fn round(source: &Pixmap, size: u32) -> Option<Pixmap> {
    let side = source.width().min(source.height());
    if side == 0 || size == 0 {
        return None;
    }
    let (left, top) = ((source.width() - side) / 2, (source.height() - side) / 2);
    let scale = size as f32 / side as f32;
    let mut out = Pixmap::new(size, size)?;
    let paint = PixmapPaint {
        quality: FilterQuality::Bicubic,
        ..PixmapPaint::default()
    };
    let transform = Transform::from_translate(-(left as f32), -(top as f32)).post_scale(scale, scale);
    out.draw_pixmap(0, 0, source.as_ref(), &paint, transform, None);
    let mut mask = Mask::new(size, size)?;
    let radius = size as f32 / 2.0;
    let circle = tiny_skia::PathBuilder::from_circle(radius, radius, radius)?;
    mask.fill_path(&circle, FillRule::Winding, true, Transform::identity());
    out.apply_mask(&mask);
    Some(out)
}

/// Reads and decodes the picture a stamp names.
fn load(stamp: &Stamp) -> Option<Pixmap> {
    use std::io::Read;
    let file = std::fs::File::open(&stamp.path).ok()?;
    let mut data = Vec::new();
    file.take(MAX_PICTURE_BYTES + 1).read_to_end(&mut data).ok()?;
    if data.len() as u64 > MAX_PICTURE_BYTES {
        return None;
    }
    round_picture(&data, PICTURE_PX)
}

struct Entry {
    stamp: Option<Stamp>,
    picture: Option<Rc<Pixmap>>,
}

/// The account pictures known, by login name.
#[derive(Default)]
pub struct Pictures {
    entries: HashMap<String, Entry>,
    /// Where the service's copies are (a test directory in tests).
    service: Option<PathBuf>,
}

impl Pictures {
    /// Pictures from a directory of the service's copies other than the
    /// system's (tests).
    #[cfg(test)]
    pub fn with_service_dir(dir: PathBuf) -> Self {
        Self { entries: HashMap::new(), service: Some(dir) }
    }

    /// Looks whether the picture of `login` changed (a `stat` of its
    /// files) and reads it again when it did. `home` is given for the
    /// account the shell runs as. Returns whether what is shown changed.
    pub fn refresh(&mut self, login: &str, home: Option<&Path>) -> bool {
        let service = self.service.clone().unwrap_or_else(|| PathBuf::from(SERVICE_ICONS));
        let stamp = newest(&candidates(login, home, &service));
        if self.entries.get(login).is_some_and(|e| e.stamp == stamp) {
            return false;
        }
        let picture = stamp.as_ref().and_then(load).map(Rc::new);
        let shown_before = self.entries.get(login).is_some_and(|e| e.picture.is_some());
        let changed = shown_before || picture.is_some();
        if self.entries.len() >= 16 && !self.entries.contains_key(login) {
            self.entries.clear();
        }
        self.entries.insert(login.to_owned(), Entry { stamp, picture });
        changed
    }

    /// The round picture of `login`, when it has one.
    pub fn get(&self, login: &str) -> Option<&Rc<Pixmap>> {
        self.entries.get(login).and_then(|e| e.picture.as_ref())
    }
}

/// The home directory of the account the shell runs as.
pub fn own_home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from).filter(|p| p.is_absolute())
}

/// Draws an account's round picture in the circle at (`cx`, `cy`) with
/// radius `r`. Returns false (and draws nothing) without a picture, so the
/// caller draws its initials or the person glyph.
pub fn draw(f: &mut Frame, picture: Option<&Rc<Pixmap>>, cx: f32, cy: f32, r: f32) -> bool {
    let Some(picture) = picture else {
        return false;
    };
    f.canvas.draw_pixmap(picture, cx - r, cy - r, 2.0 * r, 2.0 * r, 1.0);
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn png(width: u32, height: u32, rgba: [u8; 4]) -> Vec<u8> {
        let mut pixmap = Pixmap::new(width, height).unwrap();
        pixmap.fill(tiny_skia::Color::from_rgba8(rgba[0], rgba[1], rgba[2], rgba[3]));
        boswas_ui::icons::encode_png(&pixmap).unwrap()
    }

    #[test]
    fn a_picture_is_cropped_to_its_centre_and_made_round() {
        // A wide picture: red at the sides, blue in the centred square.
        let mut wide = Pixmap::new(300, 100).unwrap();
        wide.fill(tiny_skia::Color::from_rgba8(255, 0, 0, 255));
        let blue = tiny_skia::Paint { shader: tiny_skia::Shader::SolidColor(tiny_skia::Color::from_rgba8(0, 0, 255, 255)), ..Default::default() };
        wide.fill_rect(tiny_skia::Rect::from_xywh(100.0, 0.0, 100.0, 100.0).unwrap(), &blue, Transform::identity(), None);
        let round = round(&wide, 64).unwrap();
        assert_eq!((round.width(), round.height()), (64, 64));
        let at = |x: u32, y: u32| round.pixel(x, y).unwrap();
        assert_eq!(at(32, 32).blue(), 255, "the centre square is kept");
        assert_eq!(at(32, 32).red(), 0, "the sides are cropped away");
        assert_eq!(at(0, 0).alpha(), 0, "outside the circle is transparent");
        assert_eq!(at(63, 63).alpha(), 0);
        assert_eq!(at(32, 2).alpha(), 255, "the circle reaches the edge");
        assert!(round_picture(b"not a picture", 64).is_none());
    }

    /// A fresh directory of this test's own (removed when dropped).
    struct Scratch(PathBuf);

    impl Scratch {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("boswas-shell-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn the_newest_picture_of_an_account_is_shown_and_followed() {
        let root = Scratch::new("pictures");
        let (service, home) = (root.path().join("icons"), root.path().join("home"));
        std::fs::create_dir_all(&service).unwrap();
        std::fs::create_dir_all(&home).unwrap();
        let mut pictures = Pictures::with_service_dir(service.clone());
        assert!(!pictures.refresh("asha", Some(&home)), "no picture: nothing to show");
        assert!(pictures.get("asha").is_none());
        std::fs::write(home.join(".face"), png(40, 40, [0, 200, 0, 255])).unwrap();
        assert!(pictures.refresh("asha", Some(&home)));
        assert_eq!(pictures.get("asha").unwrap().width(), PICTURE_PX);
        assert!(!pictures.refresh("asha", Some(&home)), "unchanged files are not read again");
        // The service's copy, written later, wins.
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::write(service.join("asha"), png(40, 40, [200, 0, 0, 255])).unwrap();
        assert!(pictures.refresh("asha", Some(&home)));
        assert_eq!(pictures.get("asha").unwrap().pixel(128, 128).unwrap().red(), 200);
        // The login screen knows only the service's copies.
        assert!(pictures.refresh("ravi", None) == pictures.get("ravi").is_some());
        std::fs::remove_file(service.join("asha")).unwrap();
        std::fs::remove_file(home.join(".face")).unwrap();
        assert!(pictures.refresh("asha", Some(&home)), "a removed picture goes");
        assert!(pictures.get("asha").is_none());
    }

    #[test]
    fn oversized_strange_and_unsafe_files_are_not_read() {
        let root = Scratch::new("odd-pictures");
        let big = root.path().join("big");
        std::fs::write(&big, vec![0u8; (MAX_PICTURE_BYTES + 1) as usize]).unwrap();
        assert_eq!(newest(std::slice::from_ref(&big)), None, "a file over the limit is not even opened");
        let dir = root.path().join("a-directory");
        std::fs::create_dir(&dir).unwrap();
        assert_eq!(newest(&[dir]), None);
        let empty = root.path().join("empty");
        std::fs::write(&empty, b"").unwrap();
        assert_eq!(newest(&[empty]), None);
        for login in ["", "..", "a/b", "../etc/passwd", "x\n"] {
            assert!(candidates(login, None, Path::new("/icons")).is_empty(), "{login:?}");
        }
        assert_eq!(candidates("asha", Some(Path::new("/home/asha")), Path::new("/icons"))[0], Path::new("/icons/asha"));
        // A picture larger than the decoder's limit is refused.
        let mut huge = Vec::new();
        image_header_9000(&mut huge);
        assert!(round_picture(&huge, 64).is_none());
    }

    /// The start of a PNG that says it is 9000 by 9000 pixels.
    fn image_header_9000(out: &mut Vec<u8>) {
        let pixmap = Pixmap::new(1, 1).unwrap();
        let mut png = boswas_ui::icons::encode_png(&pixmap).unwrap();
        // IHDR width and height are the 4-byte fields at 16 and 20.
        png[16..20].copy_from_slice(&9000u32.to_be_bytes());
        png[20..24].copy_from_slice(&9000u32.to_be_bytes());
        out.extend(png);
    }
}
