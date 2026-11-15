//! Icon theme lookup and rasterisation.
//!
//! Follows the freedesktop icon theme layout: an icon name is searched in
//! the selected theme, the themes it inherits, and `hicolor`, preferring
//! scalable artwork and then the nearest fixed size. SVG is rendered with
//! resvg (no text, no external references); PNG is decoded when the
//! `images` feature is on.

use std::{
    collections::HashMap,
    io::Read,
    path::{Path, PathBuf},
    rc::Rc,
};

use tiny_skia::{Pixmap, Transform};

/// Largest icon file that is read.
const MAX_ICON_BYTES: u64 = 4 * 1024 * 1024;
/// Largest `index.theme` that is read.
const MAX_INDEX_BYTES: u64 = 512 * 1024;
/// Depth limit of the `Inherits` chain.
const MAX_THEMES: usize = 12;

#[derive(Debug)]
struct IconDir {
    path: PathBuf,
    size: u32,
    scalable: bool,
}

/// Finds and renders themed icons, caching the results.
pub struct IconLoader {
    /// Directories to search, in theme-chain order.
    dirs: Vec<IconDir>,
    pixmap_dirs: Vec<PathBuf>,
    cache: HashMap<(String, u32), Option<Rc<Pixmap>>>,
}

impl IconLoader {
    /// A loader for `theme` using the XDG data directories.
    pub fn new(theme: &str) -> Self {
        Self::with_base_dirs(theme, &icon_base_dirs())
    }

    /// A loader for `theme` searching the given `icons` directories.
    pub fn with_base_dirs(theme: &str, bases: &[PathBuf]) -> Self {
        let mut dirs = Vec::new();
        let mut seen: Vec<String> = Vec::new();
        let mut queue = vec![theme.to_owned()];
        while let Some(name) = queue.pop() {
            if seen.len() >= MAX_THEMES || seen.contains(&name) || !is_safe_name(&name) {
                continue;
            }
            seen.push(name.clone());
            let mut inherits = Vec::new();
            for base in bases {
                let root = base.join(&name);
                if let Some(index) = read_capped(&root.join("index.theme"), MAX_INDEX_BYTES) {
                    let index = String::from_utf8_lossy(&index);
                    let (theme_dirs, parents) = parse_index(&index, &root);
                    dirs.extend(theme_dirs);
                    if inherits.is_empty() {
                        inherits = parents;
                    }
                }
            }
            // Depth first, in the order the theme lists its parents.
            for parent in inherits.into_iter().rev() {
                queue.push(parent);
            }
        }
        if !seen.iter().any(|name| name == "hicolor") {
            for base in bases {
                let root = base.join("hicolor");
                if let Some(index) = read_capped(&root.join("index.theme"), MAX_INDEX_BYTES) {
                    dirs.extend(parse_index(&String::from_utf8_lossy(&index), &root).0);
                }
            }
        }
        let pixmap_dirs = bases
            .iter()
            .filter_map(|base| base.parent().map(|share| share.join("pixmaps")))
            .collect();
        Self {
            dirs,
            pixmap_dirs,
            cache: HashMap::new(),
        }
    }

    /// The icon `name` rendered to fit a square of `size` device pixels.
    /// `name` is a theme icon name or an absolute file name (desktop
    /// entries may use either).
    pub fn load(&mut self, name: &str, size: u32) -> Option<Rc<Pixmap>> {
        if name.is_empty() || size == 0 || size > 1024 {
            return None;
        }
        let key = (name.to_owned(), size);
        if let Some(cached) = self.cache.get(&key) {
            return cached.clone();
        }
        let pixmap = self.find(name, size).and_then(|path| render_file(&path, size)).map(Rc::new);
        self.cache.insert(key, pixmap.clone());
        pixmap
    }

    /// The file that provides an icon, if any.
    pub fn find(&self, name: &str, size: u32) -> Option<PathBuf> {
        let path = Path::new(name);
        if path.is_absolute() {
            return path.is_file().then(|| path.to_owned());
        }
        // Some desktop entries name the file with its extension.
        let name = name
            .strip_suffix(".svg")
            .or_else(|| name.strip_suffix(".png"))
            .unwrap_or(name);
        if !is_safe_name(name) {
            return None;
        }
        // Scalable artwork first, then the fixed size nearest to (and
        // preferably not below) the requested one.
        let mut best: Option<(u32, PathBuf)> = None;
        for dir in &self.dirs {
            for extension in ["svg", "png"] {
                if !cfg!(feature = "images") && extension == "png" {
                    continue;
                }
                let candidate = dir.path.join(format!("{name}.{extension}"));
                if !candidate.is_file() {
                    continue;
                }
                if dir.scalable && extension == "svg" {
                    return Some(candidate);
                }
                let penalty = if dir.size >= size { dir.size - size } else { (size - dir.size) * 4 };
                if best.as_ref().is_none_or(|(p, _)| penalty < *p) {
                    best = Some((penalty, candidate));
                }
            }
        }
        if let Some((_, path)) = best {
            return Some(path);
        }
        self.pixmap_dirs.iter().find_map(|dir| {
            ["svg", "png"]
                .into_iter()
                .filter(|extension| cfg!(feature = "images") || *extension == "svg")
                .map(|extension| dir.join(format!("{name}.{extension}")))
                .find(|path| path.is_file())
        })
    }
}

/// `$XDG_DATA_HOME/icons` and each `$XDG_DATA_DIRS/icons`.
pub fn icon_base_dirs() -> Vec<PathBuf> {
    data_dirs().into_iter().map(|dir| dir.join("icons")).collect()
}

/// The XDG data directories, the user's first.
pub fn data_dirs() -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    let home = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/share")));
    dirs.extend(home);
    let system = std::env::var("XDG_DATA_DIRS")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "/usr/local/share:/usr/share".into());
    for dir in system.split(':').map(PathBuf::from).filter(|p| p.is_absolute()) {
        if !dirs.contains(&dir) {
            dirs.push(dir);
        }
    }
    dirs
}

/// Icon and theme names are single path components.
fn is_safe_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 128 && !name.contains(['/', '\\', '\0']) && name != "." && name != ".."
}

fn read_capped(path: &Path, limit: u64) -> Option<Vec<u8>> {
    let file = std::fs::File::open(path).ok()?;
    if file.metadata().ok()?.len() > limit {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(limit).read_to_end(&mut bytes).ok()?;
    Some(bytes)
}

/// The icon directories of a theme and the themes it inherits.
fn parse_index(index: &str, root: &Path) -> (Vec<IconDir>, Vec<String>) {
    let mut directories: Vec<String> = Vec::new();
    let mut inherits = Vec::new();
    let mut sections: HashMap<String, (u32, bool)> = HashMap::new();
    let mut section = String::new();
    for line in index.lines() {
        let line = line.trim();
        if let Some(name) = line.strip_prefix('[').and_then(|l| l.strip_suffix(']')) {
            section = name.to_owned();
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let (key, value) = (key.trim(), value.trim());
        if section == "Icon Theme" {
            match key {
                "Directories" | "ScaledDirectories" => {
                    directories.extend(value.split(',').map(|d| d.trim().to_owned()).filter(|d| !d.is_empty()));
                }
                "Inherits" => inherits = value.split(',').map(|d| d.trim().to_owned()).collect(),
                _ => {}
            }
        } else {
            let entry = sections.entry(section.clone()).or_insert((48, false));
            match key {
                "Size" => entry.0 = value.parse().unwrap_or(48),
                "Type" => entry.1 = value.eq_ignore_ascii_case("Scalable"),
                _ => {}
            }
        }
    }
    let dirs = directories
        .into_iter()
        .filter(|dir| !dir.split('/').any(|part| part == ".." || part.is_empty()))
        .map(|dir| {
            let (size, scalable) = sections.get(&dir).copied().unwrap_or((48, false));
            IconDir {
                path: root.join(&dir),
                size,
                scalable,
            }
        })
        .collect();
    (dirs, inherits)
}

/// Renders an icon file to a `size` x `size` pixmap, centred, aspect kept.
pub fn render_file(path: &Path, size: u32) -> Option<Pixmap> {
    let bytes = read_capped(path, MAX_ICON_BYTES)?;
    match path.extension().and_then(|e| e.to_str()) {
        Some("svg") => render_svg(&bytes, size),
        #[cfg(feature = "images")]
        Some("png") => render_png(&bytes, size),
        _ => None,
    }
}

/// Renders SVG data to fit a `size` x `size` pixmap. External references
/// (files, URLs) in the document are never followed.
pub fn render_svg(data: &[u8], size: u32) -> Option<Pixmap> {
    use resvg::usvg;
    let mut options = usvg::Options::default();
    options.image_href_resolver = usvg::ImageHrefResolver {
        resolve_data: Box::new(|_, _, _| None),
        resolve_string: Box::new(|_, _| None),
    };
    let tree = usvg::Tree::from_data(data, &options).ok()?;
    let (w, h) = (tree.size().width(), tree.size().height());
    if !(w > 0.0 && h > 0.0) {
        return None;
    }
    let scale = size as f32 / w.max(h);
    let mut pixmap = Pixmap::new(size, size)?;
    let transform = Transform::from_scale(scale, scale)
        .post_translate((size as f32 - w * scale) / 2.0, (size as f32 - h * scale) / 2.0);
    resvg::render(&tree, transform, &mut pixmap.as_mut());
    Some(pixmap)
}

#[cfg(feature = "images")]
fn render_png(data: &[u8], size: u32) -> Option<Pixmap> {
    let source = decode_image(data)?;
    let (w, h) = (source.width() as f32, source.height() as f32);
    let scale = size as f32 / w.max(h);
    let mut pixmap = Pixmap::new(size, size)?;
    let transform = Transform::from_scale(scale, scale)
        .post_translate((size as f32 - w * scale) / 2.0, (size as f32 - h * scale) / 2.0);
    let paint = tiny_skia::PixmapPaint {
        quality: tiny_skia::FilterQuality::Bilinear,
        ..Default::default()
    };
    pixmap.draw_pixmap(0, 0, source.as_ref(), &paint, transform, None);
    Some(pixmap)
}

/// Decodes a PNG or JPEG into a premultiplied pixmap. Images larger than
/// 8192 pixels on a side are refused.
#[cfg(feature = "images")]
pub fn decode_image(data: &[u8]) -> Option<Pixmap> {
    let mut reader = image::ImageReader::new(std::io::Cursor::new(data)).with_guessed_format().ok()?;
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(8192);
    limits.max_image_height = Some(8192);
    limits.max_alloc = Some(512 * 1024 * 1024);
    reader.limits(limits);
    let rgba = reader.decode().ok()?.into_rgba8();
    let (w, h) = rgba.dimensions();
    let mut pixmap = Pixmap::new(w, h)?;
    for (dst, src) in pixmap.data_mut().chunks_exact_mut(4).zip(rgba.as_raw().chunks_exact(4)) {
        let a = u32::from(src[3]);
        dst[0] = ((u32::from(src[0]) * a + 127) / 255) as u8;
        dst[1] = ((u32::from(src[1]) * a + 127) / 255) as u8;
        dst[2] = ((u32::from(src[2]) * a + 127) / 255) as u8;
        dst[3] = src[3];
    }
    Some(pixmap)
}

/// Encodes a pixmap as PNG (straight alpha).
#[cfg(feature = "images")]
pub fn encode_png(pixmap: &Pixmap) -> Option<Vec<u8>> {
    use image::ImageEncoder;
    let mut straight = Vec::with_capacity(pixmap.data().len());
    for p in pixmap.data().chunks_exact(4) {
        let a = u32::from(p[3]);
        let un = |c: u8| (u32::from(c) * 255 + a / 2).checked_div(a).unwrap_or(0).min(255) as u8;
        straight.extend_from_slice(&[un(p[0]), un(p[1]), un(p[2]), p[3]]);
    }
    let mut out = Vec::new();
    image::codecs::png::PngEncoder::new(&mut out)
        .write_image(&straight, pixmap.width(), pixmap.height(), image::ExtendedColorType::Rgba8)
        .ok()?;
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[cfg(feature = "images")]
    #[test]
    fn png_round_trips() {
        let mut pixmap = Pixmap::new(3, 2).unwrap();
        pixmap.fill(tiny_skia::Color::from_rgba8(10, 200, 30, 255));
        let png = encode_png(&pixmap).unwrap();
        let back = decode_image(&png).unwrap();
        assert_eq!((back.width(), back.height()), (3, 2));
        assert_eq!(back.data(), pixmap.data());
    }

    const SVG: &str = r##"<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><rect x="8" y="8" width="240" height="240" fill="#7C5CFF"/></svg>"##;

    fn theme(root: &Path, name: &str, inherits: &str, dirs: &[(&str, u32, bool)]) {
        let dir = root.join(name);
        fs::create_dir_all(&dir).unwrap();
        let mut index = format!(
            "[Icon Theme]\nName={name}\nInherits={inherits}\nDirectories={}\n",
            dirs.iter().map(|d| d.0).collect::<Vec<_>>().join(",")
        );
        for (path, size, scalable) in dirs {
            index += &format!("\n[{path}]\nSize={size}\nType={}\n", if *scalable { "Scalable" } else { "Fixed" });
            fs::create_dir_all(dir.join(path)).unwrap();
        }
        fs::write(dir.join("index.theme"), index).unwrap();
    }

    fn fixture() -> (tempdir::Dir, PathBuf) {
        let dir = tempdir::Dir::new();
        let icons = dir.path().join("icons");
        theme(&icons, "Prism", "Base", &[("scalable/apps", 256, true), ("scalable/status", 16, true)]);
        theme(&icons, "Base", "hicolor", &[("48x48/apps", 48, false), ("scalable/apps", 256, true)]);
        theme(&icons, "hicolor", "", &[("scalable/apps", 256, true)]);
        fs::write(icons.join("Prism/scalable/apps/terminal.svg"), SVG).unwrap();
        fs::write(icons.join("Base/scalable/apps/terminal.svg"), SVG).unwrap();
        fs::write(icons.join("Base/scalable/apps/editor.svg"), SVG).unwrap();
        fs::write(icons.join("hicolor/scalable/apps/fallback.svg"), SVG).unwrap();
        (dir, icons)
    }

    /// A minimal self-cleaning temporary directory (std only).
    mod tempdir {
        use std::path::{Path, PathBuf};

        pub struct Dir(PathBuf);

        impl Dir {
            pub fn new() -> Self {
                static NEXT: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
                let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                let path = std::env::temp_dir().join(format!("boswas-ui-test-{}-{n}", std::process::id()));
                std::fs::create_dir_all(&path).unwrap();
                Self(path)
            }

            pub fn path(&self) -> &Path {
                &self.0
            }
        }

        impl Drop for Dir {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
    }

    #[test]
    fn the_theme_wins_over_what_it_inherits() {
        let (_dir, icons) = fixture();
        let loader = IconLoader::with_base_dirs("Prism", &[icons.clone()]);
        assert_eq!(loader.find("terminal", 32), Some(icons.join("Prism/scalable/apps/terminal.svg")));
        assert_eq!(loader.find("editor", 32), Some(icons.join("Base/scalable/apps/editor.svg")));
        assert_eq!(loader.find("fallback", 32), Some(icons.join("hicolor/scalable/apps/fallback.svg")));
        assert_eq!(loader.find("missing", 32), None);
        assert_eq!(loader.find("terminal.svg", 32), Some(icons.join("Prism/scalable/apps/terminal.svg")));
    }

    #[test]
    fn names_cannot_leave_the_theme_directories() {
        let (_dir, icons) = fixture();
        let loader = IconLoader::with_base_dirs("Prism", &[icons]);
        for name in ["../Base/scalable/apps/editor", "a/b", "..", "", "x\0y"] {
            assert_eq!(loader.find(name, 32), None, "{name:?}");
        }
        let escaping = IconLoader::with_base_dirs("../../etc", &[PathBuf::from("/usr/share/icons")]);
        assert!(escaping.dirs.iter().all(|d| !d.path.starts_with("/etc")));
    }

    #[test]
    fn inheritance_loops_end() {
        let dir = tempdir::Dir::new();
        let icons = dir.path().join("icons");
        theme(&icons, "A", "B", &[("scalable/apps", 256, true)]);
        theme(&icons, "B", "A", &[("scalable/apps", 256, true)]);
        let loader = IconLoader::with_base_dirs("A", &[icons]);
        assert_eq!(loader.dirs.len(), 2);
    }

    #[test]
    fn svg_renders_at_the_requested_size_and_is_cached() {
        let (_dir, icons) = fixture();
        let mut loader = IconLoader::with_base_dirs("Prism", &[icons]);
        let icon = loader.load("terminal", 48).unwrap();
        assert_eq!((icon.width(), icon.height()), (48, 48));
        let centre = icon.pixel(24, 24).unwrap();
        assert_eq!((centre.red(), centre.green(), centre.blue(), centre.alpha()), (0x7C, 0x5C, 0xFF, 255));
        assert_eq!(icon.pixel(0, 0).unwrap().alpha(), 0);
        let again = loader.load("terminal", 48).unwrap();
        assert!(Rc::ptr_eq(&icon, &again));
        assert!(loader.load("missing", 48).is_none());
        assert!(loader.load("terminal", 0).is_none());
        assert!(loader.load("terminal", 100_000).is_none());
    }

    #[test]
    fn svg_external_references_are_not_followed() {
        let svg = r#"<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="16" height="16"><image width="16" height="16" xlink:href="/etc/passwd"/><image width="16" height="16" href="file:///usr/share/icons/hicolor/48x48/apps/x.png"/></svg>"#;
        let pixmap = render_svg(svg.as_bytes(), 16).unwrap();
        assert!(pixmap.data().iter().all(|b| *b == 0), "nothing external was drawn");
        assert!(render_svg(b"not an svg", 16).is_none());
    }
}
