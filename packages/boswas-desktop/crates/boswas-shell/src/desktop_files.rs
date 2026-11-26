//! The desktop folder's files: what the desktop icons show and what they
//! do to them. Everything here touches the file system and runs on worker
//! threads, never on the thread that draws.
//!
//! The rules are Files' own (`boswas_apps/files`): names are validated as
//! `text.validate_name` does, free names are found as `text.unique_name`
//! finds them ("New folder (2)", "report - Copy.pdf"), the Trash follows
//! the freedesktop.org Trash specification as `trash.py` does (the info
//! file is created first, with `O_EXCL`, which makes a name ours), and the
//! clipboard and drag formats are `clipformats.py`'s (`text/uri-list` and
//! `x-special/gnome-copied-files`).
//!
//! Safety: nothing is ever done through a symbolic link: a link on the
//! desktop is trashed, moved, copied and renamed as the link itself;
//! copying a folder copies links as links; nothing is overwritten
//! (renames use `RENAME_NOREPLACE`, new files `O_EXCL`); no shell is
//! involved anywhere.

use std::{
    cmp::Ordering,
    collections::HashMap,
    ffi::OsString,
    io::{Read, Write},
    os::unix::{
        ffi::{OsStrExt, OsStringExt},
        fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    },
    path::{Path, PathBuf},
};

use serde_json::{json, Value};

/// The longest name a file can have, in bytes.
pub const NAME_MAX: usize = 255;
/// Most entries of the desktop folder shown.
pub const MAX_ENTRIES: usize = 2000;
/// Extensions that belong together.
const COMPOUND_EXTENSIONS: [&str; 7] = [".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz", ".tar.lzma", ".tar.z"];

// ---------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------

/// Why `name` cannot be the name of a file, in plain words (Files' rule),
/// or `Ok` when it can.
pub fn validate_name(name: &str) -> Result<(), &'static str> {
    if name.is_empty() {
        return Err("A name cannot be empty.");
    }
    if name == "." || name == ".." {
        return Err("That is not a name a file can have.");
    }
    if name.contains('/') {
        return Err("A name cannot contain a slash (/).");
    }
    if name.contains('\0') {
        return Err("A name cannot contain a null character.");
    }
    if name.len() > NAME_MAX {
        return Err("This name is too long.");
    }
    if name.chars().any(char::is_control) {
        return Err("A name cannot contain control characters.");
    }
    if name.trim().is_empty() {
        return Err("A name cannot be only spaces.");
    }
    Ok(())
}

/// `("report", ".pdf")`, `("backup", ".tar.gz")`, `(".bashrc", "")`.
pub fn split_extension(name: &str) -> (&str, &str) {
    let lower = name.to_lowercase();
    for extension in COMPOUND_EXTENSIONS {
        if lower.ends_with(extension) && name.len() > extension.len() && name.is_char_boundary(name.len() - extension.len()) {
            return name.split_at(name.len() - extension.len());
        }
    }
    match name.rfind('.') {
        Some(0) | None => (name, ""),
        Some(at) if at + 1 == name.len() => (name, ""),
        Some(at) => name.split_at(at),
    }
}

/// `name` cut to at most `limit` bytes without splitting a character.
fn truncate(name: &str, limit: usize) -> &str {
    if name.len() <= limit {
        return name;
    }
    let mut end = limit;
    while !name.is_char_boundary(end) {
        end -= 1;
    }
    &name[..end]
}

/// `name` if it is free, else `stem (2).ext`, `stem (3).ext`, ...; with
/// `copy` the first try is `stem - Copy.ext` (a paste into the folder the
/// file came from). Names stay within the byte limit.
pub fn unique_name(name: &str, exists: impl Fn(&str) -> bool, copy: bool) -> String {
    if !copy && !exists(name) {
        return name.to_owned();
    }
    let (stem, extension) = split_extension(name);
    let mut number = if copy { 1 } else { 2 };
    loop {
        let suffix = if copy {
            if number == 1 {
                " - Copy".to_owned()
            } else {
                format!(" - Copy ({number})")
            }
        } else {
            format!(" ({number})")
        };
        let room = NAME_MAX.saturating_sub(suffix.len() + extension.len());
        let candidate = format!("{}{suffix}{extension}", truncate(stem, room));
        if !exists(&candidate) || number > 100_000 {
            return candidate;
        }
        number += 1;
    }
}

/// Orders names as people read them: `file2` before `file10`, without
/// regard to case.
pub fn natural_cmp(a: &str, b: &str) -> Ordering {
    let (mut x, mut y) = (a.chars().peekable(), b.chars().peekable());
    loop {
        match (x.peek().copied(), y.peek().copied()) {
            (None, None) => return a.cmp(b),
            (None, Some(_)) => return Ordering::Less,
            (Some(_), None) => return Ordering::Greater,
            (Some(c), Some(d)) if c.is_ascii_digit() && d.is_ascii_digit() => {
                let mut left = String::new();
                while let Some(c) = x.peek().copied().filter(char::is_ascii_digit) {
                    left.push(c);
                    x.next();
                }
                let mut right = String::new();
                while let Some(d) = y.peek().copied().filter(char::is_ascii_digit) {
                    right.push(d);
                    y.next();
                }
                let (l, r) = (left.trim_start_matches('0'), right.trim_start_matches('0'));
                let order = l.len().cmp(&r.len()).then_with(|| l.cmp(r));
                if order != Ordering::Equal {
                    return order;
                }
            }
            (Some(c), Some(d)) => {
                let order = c.to_lowercase().cmp(d.to_lowercase());
                if order != Ordering::Equal {
                    return order;
                }
                x.next();
                y.next();
            }
        }
    }
}

/// `0 bytes`, `1.46 KB`, `12.5 MB` (1 KB = 1024 bytes, as Files writes it).
pub fn format_size(size: u64) -> String {
    if size < 1024 {
        return if size == 1 { "1 byte".into() } else { format!("{size} bytes") };
    }
    let mut value = size as f64;
    let mut unit = "";
    for name in ["KB", "MB", "GB", "TB", "PB", "EB"] {
        value /= 1024.0;
        unit = name;
        if value < 1024.0 {
            break;
        }
    }
    if value >= 100.0 {
        format!("{value:.0} {unit}")
    } else if value >= 10.0 {
        format!("{value:.1} {unit}")
    } else {
        format!("{value:.2} {unit}")
    }
}

// ---------------------------------------------------------------------------
// The desktop folder
// ---------------------------------------------------------------------------

/// Where the desktop folder is and how it was found.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DesktopDir {
    pub path: PathBuf,
    /// `user-dirs.dirs` names it: xdg-user-dirs creates it, and so does the
    /// shell when it is missing.
    pub configured: bool,
    /// `user-dirs.dirs` sets it to the home folder, which is how
    /// xdg-user-dirs turns a folder off: no files are shown.
    pub disabled: bool,
}

/// The desktop folder from `user-dirs.dirs` (`XDG_DESKTOP_DIR`), else
/// `~/Desktop`.
pub fn desktop_dir(home: &Path, user_dirs: Option<&str>) -> DesktopDir {
    for line in user_dirs.unwrap_or_default().lines() {
        let Some(value) = line.trim().strip_prefix("XDG_DESKTOP_DIR=") else {
            continue;
        };
        let value = value.trim().trim_matches('"');
        let path = match value.strip_prefix("$HOME") {
            Some(rest) if rest.is_empty() || rest.starts_with('/') => home.join(rest.trim_start_matches('/')),
            Some(_) => continue,
            None => PathBuf::from(value),
        };
        if !path.is_absolute() || value.contains("..") {
            continue;
        }
        let disabled = path == home || path.as_os_str().as_bytes().strip_suffix(b"/") == Some(home.as_os_str().as_bytes());
        return DesktopDir { path, configured: true, disabled };
    }
    DesktopDir { path: home.join("Desktop"), configured: false, disabled: false }
}

/// The session's desktop folder.
pub fn session_desktop_dir() -> Option<DesktopDir> {
    let home = std::env::var_os("HOME").map(PathBuf::from).filter(|p| p.is_absolute())?;
    let config = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from).filter(|p| p.is_absolute()).unwrap_or_else(|| home.join(".config"));
    let text = std::fs::read(config.join("user-dirs.dirs")).ok().filter(|t| t.len() < 64 * 1024).map(|t| String::from_utf8_lossy(&t).into_owned());
    Some(desktop_dir(&home, text.as_deref()))
}

// ---------------------------------------------------------------------------
// File types
// ---------------------------------------------------------------------------

/// What kind of file a name is, for its icon, its label and Sort by type.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileType {
    pub mime: &'static str,
    /// "PNG image", "Folder", "PDF document".
    pub label: String,
    /// Icon names, the best first.
    pub icons: &'static [&'static str],
    /// The shell can make a thumbnail of it.
    pub picture: bool,
}

/// The type of an entry from its name (the shell does not sniff contents).
pub fn file_type(name: &str, is_dir: bool, executable: bool) -> FileType {
    if is_dir {
        return FileType { mime: "inode/directory", label: "Folder".into(), icons: &["folder", "inode-directory"], picture: false };
    }
    let (_, extension) = split_extension(name);
    let ext = extension.trim_start_matches('.').to_lowercase();
    let (mime, label, icons, picture): (&'static str, &str, &'static [&'static str], bool) = match ext.as_str() {
        "png" => ("image/png", "PNG image", &["image-png", "image-x-generic"], true),
        "jpg" | "jpeg" | "jpe" => ("image/jpeg", "JPEG image", &["image-jpeg", "image-x-generic"], true),
        "gif" => ("image/gif", "GIF image", &["image-gif", "image-x-generic"], false),
        "webp" => ("image/webp", "WebP image", &["image-webp", "image-x-generic"], false),
        "bmp" => ("image/bmp", "BMP image", &["image-bmp", "image-x-generic"], false),
        "svg" => ("image/svg+xml", "SVG image", &["image-svg+xml", "image-x-generic"], false),
        "tif" | "tiff" => ("image/tiff", "TIFF image", &["image-tiff", "image-x-generic"], false),
        "pdf" => ("application/pdf", "PDF document", &["application-pdf"], false),
        "txt" | "text" => ("text/plain", "Text document", &["text-plain", "text-x-generic"], false),
        "log" => ("text/plain", "Log file", &["text-x-generic"], false),
        "md" => ("text/markdown", "Markdown document", &["text-markdown", "text-x-generic"], false),
        "html" | "htm" => ("text/html", "Web page", &["text-html", "text-x-generic"], false),
        "json" => ("application/json", "JSON file", &["application-json", "text-x-generic"], false),
        "xml" => ("application/xml", "XML file", &["text-xml", "text-x-generic"], false),
        "csv" => ("text/csv", "CSV file", &["text-csv", "x-office-spreadsheet"], false),
        "sh" | "bash" | "py" | "pl" | "rb" => ("text/x-script", "Script", &["text-x-script", "text-x-generic"], false),
        "desktop" => ("application/x-desktop", "Launcher", &["application-x-executable", "text-x-generic"], false),
        "mp3" | "ogg" | "oga" | "flac" | "wav" | "m4a" | "opus" => ("audio/x-generic", "Audio file", &["audio-x-generic"], false),
        "mp4" | "mkv" | "webm" | "avi" | "mov" | "ogv" => ("video/x-generic", "Video file", &["video-x-generic"], false),
        "zip" => ("application/zip", "ZIP archive", &["application-zip", "package-x-generic"], false),
        "7z" => ("application/x-7z-compressed", "7z archive", &["application-x-7z-compressed", "package-x-generic"], false),
        "tar" => ("application/x-tar", "TAR archive", &["application-x-tar", "package-x-generic"], false),
        "tar.gz" | "tar.bz2" | "tar.xz" | "tar.zst" | "tar.lz" | "tar.lzma" | "tar.z" | "tgz" => ("application/x-compressed-tar", "Compressed archive", &["application-x-compressed-tar", "package-x-generic"], false),
        "gz" | "xz" | "bz2" | "zst" => ("application/x-compressed", "Compressed file", &["package-x-generic"], false),
        "deb" => ("application/vnd.debian.binary-package", "Debian package", &["application-x-deb", "package-x-generic"], false),
        "odt" | "doc" | "docx" | "rtf" => ("application/vnd.oasis.opendocument.text", "Document", &["x-office-document"], false),
        "ods" | "xls" | "xlsx" => ("application/vnd.oasis.opendocument.spreadsheet", "Spreadsheet", &["x-office-spreadsheet"], false),
        "odp" | "ppt" | "pptx" => ("application/vnd.oasis.opendocument.presentation", "Presentation", &["x-office-presentation"], false),
        "ttf" | "otf" => ("font/ttf", "Font", &["font-x-generic"], false),
        "exe" => ("application/x-ms-dos-executable", "Windows program", &["application-x-ms-dos-executable", "application-x-executable"], false),
        "msi" => ("application/x-msi", "Windows installer", &["application-x-msi", "application-x-executable"], false),
        "apk" => ("application/vnd.android.package-archive", "Android package", &["application-vnd.android.package-archive", "package-x-generic"], false),
        "appimage" => ("application/vnd.appimage", "AppImage", &["application-x-executable"], false),
        _ if executable => ("application/x-executable", "Program", &["application-x-executable"], false),
        "" => ("application/octet-stream", "File", &["unknown", "application-octet-stream", "text-x-generic"], false),
        _ => ("application/octet-stream", "", &["unknown", "application-octet-stream", "text-x-generic"], false),
    };
    let label = if label.is_empty() { format!("{} file", ext.to_uppercase()) } else { label.to_owned() };
    FileType { mime, label, icons, picture }
}

// ---------------------------------------------------------------------------
// Scanning
// ---------------------------------------------------------------------------

/// One entry of the desktop folder.
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    /// The name as it is on disk.
    pub os_name: OsString,
    /// The name to show (undecodable bytes as U+FFFD).
    pub name: String,
    pub is_dir: bool,
    /// The entry is a symbolic link (shown as what it points to).
    pub is_link: bool,
    pub size: u64,
    /// Seconds since the epoch.
    pub modified: i64,
    pub kind: FileType,
    /// A launcher (`.desktop`) the desktop may start: its name and icon.
    pub launcher: Option<(String, String)>,
}

impl Entry {
    /// The key the desktop knows the entry by (selection, positions).
    pub fn key(&self) -> String {
        format!("file:{}", self.name)
    }
}

/// Whether the desktop may start a launcher (`.desktop` file): only a
/// regular file (not a link) of the user with the execute bit set, the
/// way people mark a launcher as trusted in Files (Properties > "Allow
/// running this file as a program"). Anything else opens as text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Trust {
    Launch,
    /// A symbolic link: whoever controls its target controls what runs.
    Link,
    NotOwned,
    NotExecutable,
    NotRegular,
}

pub fn launcher_trust(meta: &std::fs::Metadata, uid: u32) -> Trust {
    if meta.file_type().is_symlink() {
        Trust::Link
    } else if !meta.file_type().is_file() {
        Trust::NotRegular
    } else if meta.uid() != uid {
        Trust::NotOwned
    } else if meta.mode() & 0o100 == 0 {
        Trust::NotExecutable
    } else {
        Trust::Launch
    }
}

impl Trust {
    /// Why a launcher was opened as text instead.
    pub fn reason(self) -> &'static str {
        match self {
            Trust::Launch => "",
            Trust::Link => "It is a link to another file; only launchers kept on the desktop itself are started.",
            Trust::NotOwned => "It belongs to another user.",
            Trust::NotExecutable => "To start it from the desktop, allow it to run: in Files, Properties > “Allow running this file as a program”.",
            Trust::NotRegular => "It is not a regular file.",
        }
    }
}

/// Reads the desktop folder: hidden entries (a name that starts with a
/// dot) are left out, as Files leaves them out by default.
pub fn scan(dir: &Path, uid: u32, locale: &str) -> std::io::Result<Vec<Entry>> {
    let mut out = Vec::new();
    for entry in std::fs::read_dir(dir)?.flatten() {
        if out.len() >= MAX_ENTRIES {
            break;
        }
        let os_name = entry.file_name();
        if os_name.as_bytes().first() == Some(&b'.') {
            continue;
        }
        let path = entry.path();
        let Ok(own) = std::fs::symlink_metadata(&path) else {
            continue;
        };
        let is_link = own.file_type().is_symlink();
        // A link shows what it points to (a folder icon for a link to a
        // folder); what is done to it is done to the link.
        let shown = if is_link { std::fs::metadata(&path).unwrap_or_else(|_| own.clone()) } else { own.clone() };
        let name = os_name.to_string_lossy().into_owned();
        let is_dir = shown.is_dir();
        let executable = shown.is_file() && shown.mode() & 0o111 != 0;
        let kind = file_type(&name, is_dir, executable);
        let launcher = (kind.mime == "application/x-desktop" && launcher_trust(&own, uid) == Trust::Launch)
            .then(|| read_launcher(&path, &name, locale))
            .flatten();
        out.push(Entry {
            os_name,
            name,
            is_dir,
            is_link,
            size: if is_dir { 0 } else { shown.len() },
            modified: shown.mtime(),
            kind,
            launcher,
        });
    }
    Ok(out)
}

/// A trusted launcher's name and icon.
fn read_launcher(path: &Path, name: &str, locale: &str) -> Option<(String, String)> {
    let mut text = String::new();
    std::fs::File::open(path).ok()?.take(64 * 1024).read_to_string(&mut text).ok()?;
    let app = crate::apps::parse_entry(name, &text, locale)?;
    Some((app.name, app.icon))
}

// ---------------------------------------------------------------------------
// The Trash
// ---------------------------------------------------------------------------

/// The home trash: `$XDG_DATA_HOME/Trash`.
pub fn home_trash() -> Option<PathBuf> {
    crate::agenda::data_home().map(|dir| dir.join("Trash"))
}

/// `quote(path, safe="/")`: every byte but letters, digits, `_.-~` and `/`
/// percent-encoded (the `Path` of a `.trashinfo`).
pub fn trash_quote(bytes: &[u8]) -> String {
    let mut out = String::new();
    for byte in bytes {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' | b'/' => out.push(*byte as char),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// The text of a `.trashinfo` file.
pub fn trashinfo(original: &Path, deleted: &str) -> String {
    format!("[Trash Info]\nPath={}\nDeletionDate={deleted}\n", trash_quote(original.as_os_str().as_bytes()))
}

/// Moves `path` (a file, a folder, or a link itself) to the home trash.
/// Returns the item's new place.
pub fn trash(path: &Path, home_trash: &Path, deleted: &str) -> Result<PathBuf, String> {
    let name = path.file_name().ok_or("This item cannot be moved to the Trash.")?;
    let parent = path.parent().ok_or("This item cannot be moved to the Trash.")?;
    let parent = std::fs::canonicalize(parent).map_err(|e| e.to_string())?;
    let path = parent.join(name);
    std::fs::symlink_metadata(&path).map_err(|_| "The item is no longer there.".to_owned())?;
    let trash = std::fs::canonicalize(home_trash).unwrap_or_else(|_| home_trash.to_path_buf());
    if trash.starts_with(&path) || path.starts_with(&trash) {
        return Err("This item is already in the Trash.".into());
    }
    for sub in ["files", "info"] {
        std::fs::DirBuilder::new().recursive(true).mode(0o700).create(home_trash.join(sub)).map_err(|e| format!("The Trash cannot be written: {e}"))?;
    }
    let content = trashinfo(&path, deleted);
    let base = name.to_string_lossy().into_owned();
    let room = NAME_MAX - ".trashinfo".len();
    for number in 1..10_000 {
        let suffix = if number == 1 { String::new() } else { format!(".{number}") };
        let candidate = format!("{}{suffix}", truncate(&base, room - suffix.len()));
        let info = home_trash.join("info").join(format!("{candidate}.trashinfo"));
        let created = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(&info);
        let mut file = match created {
            Ok(file) => file,
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(format!("The Trash cannot be written: {err}")),
        };
        if file.write_all(content.as_bytes()).is_err() {
            let _ = std::fs::remove_file(&info);
            return Err("The Trash cannot be written.".into());
        }
        drop(file);
        let target = home_trash.join("files").join(&candidate);
        if std::fs::symlink_metadata(&target).is_ok() {
            // A file without a record sits there: leave it alone.
            let _ = std::fs::remove_file(&info);
            continue;
        }
        return match std::fs::rename(&path, &target) {
            Ok(()) => Ok(target),
            Err(err) => {
                let _ = std::fs::remove_file(&info);
                if err.raw_os_error() == Some(libc::EXDEV) {
                    Err("This item is on another drive than the Trash. Delete it in Files.".into())
                } else {
                    Err(err.to_string())
                }
            }
        };
    }
    Err("The Trash is full of items with this name.".into())
}

// ---------------------------------------------------------------------------
// Creating, renaming, copying and moving
// ---------------------------------------------------------------------------

fn lexists(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok()
}

/// Creates a folder with a free name based on "New folder"; returns its name.
pub fn create_folder(dir: &Path) -> std::io::Result<String> {
    for _ in 0..100 {
        let name = unique_name("New folder", |n| lexists(&dir.join(n)), false);
        match std::fs::create_dir(dir.join(&name)) {
            Ok(()) => return Ok(name),
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(err),
        }
    }
    Err(std::io::Error::new(std::io::ErrorKind::AlreadyExists, "no free name"))
}

/// Creates an empty file with a free name based on "New text
/// document.txt"; returns its name.
pub fn create_text_document(dir: &Path) -> std::io::Result<String> {
    for _ in 0..100 {
        let name = unique_name("New text document.txt", |n| lexists(&dir.join(n)), false);
        match std::fs::OpenOptions::new().write(true).create_new(true).custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW).open(dir.join(&name)) {
            Ok(_) => return Ok(name),
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(err),
        }
    }
    Err(std::io::Error::new(std::io::ErrorKind::AlreadyExists, "no free name"))
}

/// Renames an entry of `dir`, never over something that is there.
pub fn rename(dir: &Path, old: &OsString, new: &str) -> Result<(), String> {
    validate_name(new).map_err(str::to_owned)?;
    if old.as_bytes() == new.as_bytes() {
        return Ok(());
    }
    let from = dir.join(old);
    std::fs::symlink_metadata(&from).map_err(|_| "The item is no longer there.".to_owned())?;
    let to = dir.join(new);
    let same_but_case = old.to_string_lossy().to_lowercase() == new.to_lowercase();
    let flags = if same_but_case { rustix::fs::RenameFlags::empty() } else { rustix::fs::RenameFlags::NOREPLACE };
    if same_but_case && lexists(&to) && std::fs::symlink_metadata(&to).map(|m| m.ino()).ok() != std::fs::symlink_metadata(&from).map(|m| m.ino()).ok() {
        return Err("Something with this name is already there.".into());
    }
    match rustix::fs::renameat_with(rustix::fs::CWD, &from, rustix::fs::CWD, &to, flags) {
        Ok(()) => Ok(()),
        Err(rustix::io::Errno::EXIST) => Err("Something with this name is already there.".into()),
        Err(rustix::io::Errno::INVAL) if !same_but_case => {
            // A file system without RENAME_NOREPLACE: check, then rename.
            if lexists(&to) {
                return Err("Something with this name is already there.".into());
            }
            std::fs::rename(&from, &to).map_err(|e| e.to_string())
        }
        Err(err) => Err(std::io::Error::from(err).to_string()),
    }
}

/// What a copy or a move did.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Report {
    /// The names the items have now in the destination.
    pub done: Vec<String>,
    /// Items that could not be copied or moved, with the reason.
    pub failed: Vec<(String, String)>,
}

/// Most entries copied in one go (a folder with more is left incomplete).
const COPY_BUDGET: usize = 100_000;

fn copy_file(from: &Path, to: &Path, mode: u32) -> std::io::Result<()> {
    let mut source = std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(from)?;
    let mut target = std::fs::OpenOptions::new().write(true).create_new(true).mode(mode & 0o777).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(to)?;
    std::io::copy(&mut source, &mut target)?;
    target.set_permissions(std::fs::Permissions::from_mode(mode & 0o7777 & !0o6000))?;
    Ok(())
}

/// Copies `from` to the new path `to`: links as links, folders with what
/// is in them.
fn copy_tree(from: &Path, to: &Path, depth: usize, budget: &mut usize) -> std::io::Result<()> {
    if *budget == 0 || depth > 64 {
        return Err(std::io::Error::other("the folder is too large or too deep"));
    }
    *budget -= 1;
    let meta = std::fs::symlink_metadata(from)?;
    let kind = meta.file_type();
    if kind.is_symlink() {
        std::os::unix::fs::symlink(std::fs::read_link(from)?, to)
    } else if kind.is_dir() {
        std::fs::DirBuilder::new().mode(meta.mode() & 0o777 | 0o700).create(to)?;
        for entry in std::fs::read_dir(from)? {
            let entry = entry?;
            copy_tree(&entry.path(), &to.join(entry.file_name()), depth + 1, budget)?;
        }
        Ok(())
    } else if kind.is_file() {
        copy_file(from, to, meta.mode())
    } else {
        Err(std::io::Error::other("only files, folders and links can be copied"))
    }
}

/// Copies or moves `sources` (absolute paths) into `dest`, keeping both
/// where a name is taken. A move within one file system is a rename;
/// across file systems the copy is finished before the source goes.
pub fn transfer(sources: &[PathBuf], dest: &Path, moving: bool) -> Report {
    let mut report = Report::default();
    let dest_real = std::fs::canonicalize(dest).unwrap_or_else(|_| dest.to_path_buf());
    for source in sources.iter().take(10_000) {
        let label = source.file_name().map_or_else(|| source.display().to_string(), |n| n.to_string_lossy().into_owned());
        let Some(name) = source.file_name().map(|n| n.to_string_lossy().into_owned()) else {
            report.failed.push((label, "This item cannot be copied.".into()));
            continue;
        };
        if !source.is_absolute() || validate_name(&name).is_err() {
            report.failed.push((label, "This item cannot be copied.".into()));
            continue;
        }
        let Ok(meta) = std::fs::symlink_metadata(source) else {
            report.failed.push((label, "The item is no longer there.".into()));
            continue;
        };
        let parent_real = source.parent().and_then(|p| std::fs::canonicalize(p).ok());
        let source_real = parent_real.as_ref().map(|p| p.join(source.file_name().unwrap_or_default()));
        if meta.is_dir() && source_real.as_ref().is_some_and(|s| dest_real.starts_with(s)) {
            report.failed.push((label, "A folder cannot go into itself.".into()));
            continue;
        }
        let same_folder = parent_real.as_deref() == Some(dest_real.as_path());
        if moving && same_folder {
            continue;
        }
        let target_name = unique_name(&name, |n| lexists(&dest.join(n)), !moving && same_folder);
        let target = dest.join(&target_name);
        let result = if moving {
            match rustix::fs::renameat_with(rustix::fs::CWD, source, rustix::fs::CWD, &target, rustix::fs::RenameFlags::NOREPLACE) {
                Ok(()) => Ok(()),
                Err(rustix::io::Errno::XDEV) => {
                    let mut budget = COPY_BUDGET;
                    match copy_tree(source, &target, 0, &mut budget) {
                        Ok(()) => if meta.is_dir() { std::fs::remove_dir_all(source) } else { std::fs::remove_file(source) },
                        Err(err) => {
                            let _ = if std::fs::symlink_metadata(&target).is_ok_and(|m| m.is_dir()) { std::fs::remove_dir_all(&target) } else { std::fs::remove_file(&target) };
                            Err(err)
                        }
                    }
                }
                Err(err) => Err(std::io::Error::from(err)),
            }
        } else {
            let mut budget = COPY_BUDGET;
            copy_tree(source, &target, 0, &mut budget)
        };
        match result {
            Ok(()) => report.done.push(target_name),
            Err(err) => report.failed.push((label, err.to_string())),
        }
    }
    report
}

/// Moves desktop entries into one of the desktop's folders (a drop on a
/// folder icon), without following the folder if it is a link.
pub fn move_into(dir: &Path, names: &[OsString], folder: &OsString) -> Report {
    let target = dir.join(folder);
    if std::fs::symlink_metadata(&target).map(|m| !m.is_dir()).unwrap_or(true) {
        return Report { done: Vec::new(), failed: vec![(folder.to_string_lossy().into_owned(), "That is not a folder on the desktop.".into())] };
    }
    let sources: Vec<PathBuf> = names.iter().filter(|n| *n != folder).map(|n| dir.join(n)).collect();
    transfer(&sources, &target, true)
}

// ---------------------------------------------------------------------------
// Clipboard and drag formats
// ---------------------------------------------------------------------------

/// `file:///home/me/a%20b`, escaped as GLib does (`!$&'()*+,-./:=@_~`
/// stay as they are).
pub fn path_to_uri(path: &Path) -> String {
    let mut out = String::from("file://");
    for byte in path.as_os_str().as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'!' | b'$' | b'&' | b'\'' | b'(' | b')' | b'*' | b'+' | b',' | b'-' | b'.' | b'/' | b':' | b'=' | b'@' | b'_' | b'~' => out.push(*byte as char),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// The local path of a `file:` address; `None` for anything else.
pub fn uri_to_path(uri: &str) -> Option<PathBuf> {
    let uri = uri.trim();
    let rest = uri.get(..7).filter(|s| s.eq_ignore_ascii_case("file://")).map(|_| &uri[7..])?;
    let rest = if rest.len() >= 9 && rest[..9].eq_ignore_ascii_case("localhost") { &rest[9..] } else { rest };
    if !rest.starts_with('/') {
        return None;
    }
    let bytes = rest.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = std::str::from_utf8(bytes.get(i + 1..i + 3)?).ok()?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    if out.contains(&0) {
        return None;
    }
    let path = PathBuf::from(OsString::from_vec(out));
    // Normalised: no `.` or `..` parts.
    path.components().all(|c| matches!(c, std::path::Component::RootDir | std::path::Component::Normal(_))).then_some(path)
}

/// Most bytes of clipboard or drag data read.
pub const MAX_TRANSFER_BYTES: usize = 32 * 1024 * 1024;

/// The local paths of a `text/uri-list`; comments and other schemes are
/// left out.
pub fn decode_uri_list(data: &[u8]) -> Vec<PathBuf> {
    let text = String::from_utf8_lossy(&data[..data.len().min(MAX_TRANSFER_BYTES)]).replace('\0', "");
    text.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')).filter_map(uri_to_path).take(100_000).collect()
}

pub fn encode_uri_list(paths: &[PathBuf]) -> Vec<u8> {
    paths.iter().map(|p| path_to_uri(p) + "\r\n").collect::<String>().into_bytes()
}

/// `x-special/gnome-copied-files`: `cut` or `copy`, then the addresses.
pub fn encode_gnome(paths: &[PathBuf], cut: bool) -> Vec<u8> {
    let mut lines = vec![if cut { "cut".to_owned() } else { "copy".to_owned() }];
    lines.extend(paths.iter().map(|p| path_to_uri(p)));
    lines.join("\n").into_bytes()
}

/// (cut, paths) of `x-special/gnome-copied-files` content.
pub fn decode_gnome(data: &[u8]) -> Option<(bool, Vec<PathBuf>)> {
    let text = String::from_utf8_lossy(&data[..data.len().min(MAX_TRANSFER_BYTES)]).replace('\0', "");
    let mut lines: Vec<&str> = text.lines().map(str::trim).filter(|l| !l.is_empty()).collect();
    if lines.first() == Some(&"x-special/nautilus-clipboard") {
        lines.remove(0);
    }
    let cut = match lines.first() {
        Some(&"cut") => true,
        Some(&"copy") => false,
        _ => return None,
    };
    Some((cut, decode_uri_list(lines[1..].join("\n").as_bytes())))
}

/// The formats the desktop offers for copied or cut files, as Files offers
/// them.
pub const CLIPBOARD_MIMES: [&str; 4] = ["x-special/gnome-copied-files", "text/uri-list", "application/x-kde-cutselection", "text/plain"];

/// The content of one of [`CLIPBOARD_MIMES`].
pub fn clipboard_data(mime: &str, paths: &[PathBuf], cut: bool) -> Option<Vec<u8>> {
    Some(match mime {
        "x-special/gnome-copied-files" => encode_gnome(paths, cut),
        "text/uri-list" => encode_uri_list(paths),
        "application/x-kde-cutselection" => if cut { b"1".to_vec() } else { b"0".to_vec() },
        "text/plain" | "text/plain;charset=utf-8" | "UTF8_STRING" => paths.iter().map(|p| p.to_string_lossy().into_owned()).collect::<Vec<_>>().join("\n").into_bytes(),
        _ => return None,
    })
}

/// Reads from a pipe until it closes, at most [`MAX_TRANSFER_BYTES`] and
/// at most `seconds`.
pub fn read_pipe(mut pipe: impl Read + std::os::fd::AsFd, seconds: u64) -> Vec<u8> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(seconds);
    let _ = rustix::io::ioctl_fionbio(&pipe, true);
    let mut out = Vec::new();
    let mut buffer = [0u8; 16 * 1024];
    while std::time::Instant::now() < deadline && out.len() < MAX_TRANSFER_BYTES {
        match pipe.read(&mut buffer) {
            Ok(0) => break,
            Ok(n) => out.extend_from_slice(&buffer[..n]),
            Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => std::thread::sleep(std::time::Duration::from_millis(10)),
            Err(err) if err.kind() == std::io::ErrorKind::Interrupted => {}
            Err(_) => break,
        }
    }
    out
}

// ---------------------------------------------------------------------------
// The icons' places
// ---------------------------------------------------------------------------

/// How icons are ordered (Sort by).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum SortKey {
    #[default]
    Name,
    Size,
    Type,
    Modified,
}

impl SortKey {
    pub const ALL: [SortKey; 4] = [SortKey::Name, SortKey::Size, SortKey::Type, SortKey::Modified];

    pub fn key(self) -> &'static str {
        match self {
            SortKey::Name => "name",
            SortKey::Size => "size",
            SortKey::Type => "type",
            SortKey::Modified => "modified",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            SortKey::Name => "Name",
            SortKey::Size => "Size",
            SortKey::Type => "Item type",
            SortKey::Modified => "Date modified",
        }
    }

    fn from_key(key: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|k| k.key() == key)
    }
}

/// What `desktop-layout.json` keeps: the order and where icons were put by
/// hand, in grid cells (column, row; fractions where they were not
/// aligned to the grid).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Saved {
    pub sort: SortKey,
    pub positions: HashMap<String, (f32, f32)>,
}

/// Largest layout file read.
const MAX_LAYOUT_BYTES: u64 = 1024 * 1024;
/// Most places kept.
const MAX_POSITIONS: usize = 5000;

/// `$XDG_STATE_HOME/boswas/desktop-layout.json`.
pub fn layout_path() -> Option<PathBuf> {
    crate::config::state_dir().map(|dir| dir.join("desktop-layout.json"))
}

impl Saved {
    /// Reads the file's text. Whatever it holds, the result is usable:
    /// entries that are not understood are left out.
    pub fn parse(text: &str) -> Self {
        let mut saved = Saved::default();
        let Ok(value) = serde_json::from_str::<Value>(text) else {
            return saved;
        };
        saved.sort = value["sort"].as_str().and_then(SortKey::from_key).unwrap_or_default();
        if let Some(icons) = value["icons"].as_object() {
            for (key, place) in icons.iter().take(MAX_POSITIONS) {
                let valid_key = key.len() <= 300 && (key.starts_with("file:") || key.starts_with("system:")) && !key[key.find(':').unwrap_or(0) + 1..].contains('/') && !key.chars().any(char::is_control);
                let coordinates = place.as_array().filter(|a| a.len() == 2).and_then(|a| Some((a[0].as_f64()?, a[1].as_f64()?)));
                if let (true, Some((column, row))) = (valid_key, coordinates) {
                    if column.is_finite() && row.is_finite() && (0.0..=1000.0).contains(&column) && (0.0..=1000.0).contains(&row) {
                        saved.positions.insert(key.clone(), (column as f32, row as f32));
                    }
                }
            }
        }
        saved
    }

    pub fn to_json(&self) -> String {
        let mut icons = serde_json::Map::new();
        let mut keys: Vec<&String> = self.positions.keys().collect();
        keys.sort();
        for key in keys {
            let (c, r) = self.positions[key];
            icons.insert(key.clone(), json!([(c * 100.0).round() / 100.0, (r * 100.0).round() / 100.0]));
        }
        json!({"version": 1, "sort": self.sort.key(), "icons": icons}).to_string()
    }

    pub fn read(path: &Path) -> Self {
        match crate::agenda::read_own_file(path, MAX_LAYOUT_BYTES) {
            Ok(Some(text)) => Self::parse(&text),
            Ok(None) => Self::default(),
            Err(problem) => {
                tracing::warn!("{}: the desktop's icon places are not used: {problem}", path.display());
                Self::default()
            }
        }
    }
}

/// Writes a file atomically with mode 0600 (directories 0700), never
/// through a link.
pub fn write_private(path: &Path, text: &str) -> std::io::Result<()> {
    let dir = path.parent().ok_or_else(|| std::io::Error::other("no folder"))?;
    std::fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
    if std::fs::symlink_metadata(path).is_ok_and(|m| !m.file_type().is_file()) {
        return Err(std::io::Error::other("not a regular file"));
    }
    let name = path.file_name().ok_or_else(|| std::io::Error::other("no name"))?.to_string_lossy();
    let temporary = dir.join(format!(".{name}.{}.tmp", std::process::id()));
    let _ = std::fs::remove_file(&temporary);
    {
        let mut file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(&temporary)?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
    }
    std::fs::rename(&temporary, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&temporary);
    })
}

// ---------------------------------------------------------------------------
// The user's compositor.toml
// ---------------------------------------------------------------------------

/// A value the desktop's View menu writes.
#[derive(Clone, Debug, PartialEq)]
pub enum Setting {
    Bool(bool),
    Text(&'static str),
}

impl Setting {
    fn toml(&self) -> String {
        match self {
            Setting::Bool(on) => on.to_string(),
            Setting::Text(text) => format!("\"{text}\""),
        }
    }

    fn value(&self) -> toml::Value {
        match self {
            Setting::Bool(on) => toml::Value::Boolean(*on),
            Setting::Text(text) => toml::Value::String((*text).to_owned()),
        }
    }
}

fn is_header(line: &str) -> bool {
    line.trim_start().starts_with('[')
}

fn is_desktop_header(line: &str) -> bool {
    let line = line.trim();
    let line = line.split('#').next().unwrap_or("").trim();
    line.strip_prefix('[').and_then(|l| l.strip_suffix(']')).is_some_and(|name| name.trim() == "desktop")
}

/// Sets `[desktop] key = value` in the text of a user's `compositor.toml`,
/// changing nothing else: the result must mean exactly what the text meant
/// but for that one key, or nothing is changed (a file Settings would have
/// to rewrite as a whole is left to Settings).
pub fn set_desktop_key(text: &str, key: &str, value: &Setting) -> Result<String, &'static str> {
    let before: toml::Table = text.parse().map_err(|_| "the settings file is not valid TOML")?;
    let line = format!("{key} = {}", value.toml());
    let lines: Vec<&str> = text.lines().collect();
    let mut out: Vec<String> = Vec::with_capacity(lines.len() + 3);
    let mut done = false;
    let mut index = 0;
    while index < lines.len() {
        let current = lines[index];
        out.push(current.to_owned());
        index += 1;
        if done || !is_desktop_header(current) {
            continue;
        }
        // Inside [desktop]: replace the key's line, or add it at the top.
        let start = out.len();
        let mut replaced = false;
        while index < lines.len() && !is_header(lines[index]) {
            let body = lines[index];
            let name = body.split('=').next().unwrap_or("").trim();
            if !replaced && body.contains('=') && name == key {
                out.push(line.clone());
                replaced = true;
            } else {
                out.push(body.to_owned());
            }
            index += 1;
        }
        if !replaced {
            out.insert(start, line.clone());
        }
        done = true;
    }
    if !done {
        if out.last().is_some_and(|l| !l.trim().is_empty()) {
            out.push(String::new());
        }
        out.push("[desktop]".into());
        out.push(line);
    }
    let mut result = out.join("\n");
    result.push('\n');
    // The check: the same settings, with the one key changed.
    let after: toml::Table = result.parse().map_err(|_| "the settings file cannot be changed safely")?;
    let mut expected = before;
    let desktop = expected.entry("desktop").or_insert_with(|| toml::Value::Table(toml::Table::new()));
    let Some(table) = desktop.as_table_mut() else {
        return Err("the settings file cannot be changed safely");
    };
    table.insert(key.to_owned(), value.value());
    if after != expected {
        return Err("the settings file cannot be changed safely");
    }
    Ok(result)
}

/// Largest settings file the desktop changes (the compositor's limit).
const MAX_SETTINGS_BYTES: u64 = 256 * 1024;

/// Changes `[desktop] key` in the user's `compositor.toml` at `path`.
pub fn write_desktop_key(path: &Path, key: &str, value: &Setting) -> Result<(), String> {
    let text = match std::fs::symlink_metadata(path) {
        Ok(meta) if !meta.file_type().is_file() => return Err("the settings file is not a regular file".into()),
        Ok(meta) if meta.len() > MAX_SETTINGS_BYTES => return Err("the settings file is too large".into()),
        Ok(_) => std::fs::read_to_string(path).map_err(|e| e.to_string())?,
        Err(_) => String::new(),
    };
    let changed = set_desktop_key(&text, key, value).map_err(str::to_owned)?;
    write_private(path, &changed).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Dir(PathBuf);

    impl Dir {
        fn new(name: &str) -> Self {
            let path = std::env::temp_dir().join(format!("boswas-desk-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).unwrap();
            Dir(path)
        }
    }

    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn names_are_checked_as_files_checks_them() {
        for good in ["report.pdf", "New folder", "नमस्ते.txt", ".hidden", "a b"] {
            assert_eq!(validate_name(good), Ok(()), "{good}");
        }
        assert_eq!(validate_name(""), Err("A name cannot be empty."));
        for bad in [".", "..", "a/b", "a\0b", "tab\there", "   "] {
            assert!(validate_name(bad).is_err(), "{bad:?}");
        }
        assert!(validate_name(&"x".repeat(256)).is_err());
        assert!(validate_name(&"x".repeat(255)).is_ok());
    }

    #[test]
    fn free_names_follow_files() {
        let taken = ["New folder", "New folder (2)", "report.pdf", "report - Copy.pdf", "backup.tar.gz"];
        let exists = |n: &str| taken.contains(&n);
        assert_eq!(unique_name("New folder", exists, false), "New folder (3)");
        assert_eq!(unique_name("Fresh", exists, false), "Fresh");
        assert_eq!(unique_name("report.pdf", exists, true), "report - Copy (2).pdf");
        assert_eq!(unique_name("backup.tar.gz", exists, false), "backup (2).tar.gz");
        assert_eq!(split_extension(".bashrc"), (".bashrc", ""));
        assert_eq!(split_extension("README"), ("README", ""));
        assert_eq!(split_extension("archive.TAR.GZ"), ("archive", ".TAR.GZ"));
        let long = format!("{}.txt", "é".repeat(200));
        let free = unique_name(&long, |n| n == long, false);
        assert!(free.len() <= NAME_MAX && free.ends_with(" (2).txt"), "{free}");
        let mut names = vec!["file10", "File2", "file1", "apple"];
        names.sort_by(|a, b| natural_cmp(a, b));
        assert_eq!(names, ["apple", "file1", "File2", "file10"]);
        assert_eq!(format_size(0), "0 bytes");
        assert_eq!(format_size(1500), "1.46 KB");
        assert_eq!(format_size(13_107_200), "12.5 MB");
    }

    #[test]
    fn the_desktop_folder_comes_from_user_dirs() {
        let home = Path::new("/home/asha");
        assert_eq!(desktop_dir(home, None), DesktopDir { path: home.join("Desktop"), configured: false, disabled: false });
        let text = "# comment\nXDG_DOWNLOAD_DIR=\"$HOME/Downloads\"\nXDG_DESKTOP_DIR=\"$HOME/Schreibtisch\"\n";
        assert_eq!(desktop_dir(home, Some(text)), DesktopDir { path: home.join("Schreibtisch"), configured: true, disabled: false });
        assert!(desktop_dir(home, Some("XDG_DESKTOP_DIR=\"$HOME/\"\n")).disabled, "set to the home folder: turned off");
        assert!(desktop_dir(home, Some("XDG_DESKTOP_DIR=\"$HOME\"\n")).disabled);
        assert_eq!(desktop_dir(home, Some("XDG_DESKTOP_DIR=\"/srv/desk\"\n")).path, Path::new("/srv/desk"));
        assert_eq!(desktop_dir(home, Some("XDG_DESKTOP_DIR=\"$HOME/../../etc\"\n")).path, home.join("Desktop"), "not followed out of reason");
        assert_eq!(desktop_dir(home, Some("XDG_DESKTOP_DIR=\"relative\"\n")).path, home.join("Desktop"));
    }

    #[test]
    fn types_icons_and_labels() {
        assert_eq!(file_type("x", true, false).label, "Folder");
        let png = file_type("Holiday.PNG", false, false);
        assert!(png.picture && png.mime == "image/png" && png.icons[0] == "image-png");
        assert_eq!(file_type("a.tar.gz", false, false).mime, "application/x-compressed-tar");
        assert_eq!(file_type("tool", false, true).label, "Program");
        assert_eq!(file_type("data.xyz", false, false).label, "XYZ file");
        assert_eq!(file_type("launch.desktop", false, true).mime, "application/x-desktop");
    }

    #[test]
    fn the_launcher_trust_rule() {
        let dir = Dir::new("trust");
        let uid = rustix::process::geteuid().as_raw();
        let path = dir.0.join("app.desktop");
        std::fs::write(&path, "[Desktop Entry]\nType=Application\nName=Tool\nExec=true\nIcon=utilities-terminal\n").unwrap();
        let meta = |p: &Path| std::fs::symlink_metadata(p).unwrap();
        assert_eq!(launcher_trust(&meta(&path), uid), Trust::NotExecutable, "a launcher someone dropped is not started");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(launcher_trust(&meta(&path), uid), Trust::Launch);
        assert_eq!(launcher_trust(&meta(&path), uid + 1), Trust::NotOwned);
        let link = dir.0.join("link.desktop");
        std::os::unix::fs::symlink(&path, &link).unwrap();
        assert_eq!(launcher_trust(&meta(&link), uid), Trust::Link, "a link is never trusted");
        assert_eq!(launcher_trust(&meta(&dir.0), uid), Trust::NotRegular);
        assert!(!Trust::NotExecutable.reason().is_empty());
        // The scan shows a trusted launcher by its name and icon, the others by their file name.
        let entries = scan(&dir.0, uid, "C").unwrap();
        let trusted = entries.iter().find(|e| e.name == "app.desktop").unwrap();
        assert_eq!(trusted.launcher, Some(("Tool".into(), "utilities-terminal".into())));
        assert_eq!(entries.iter().find(|e| e.name == "link.desktop").unwrap().launcher, None);
    }

    #[test]
    fn scanning_leaves_hidden_files_out_and_shows_links_as_their_targets() {
        let dir = Dir::new("scan");
        std::fs::write(dir.0.join("note.txt"), "hello").unwrap();
        std::fs::write(dir.0.join(".hidden"), "x").unwrap();
        std::fs::create_dir(dir.0.join("Projects")).unwrap();
        std::os::unix::fs::symlink(dir.0.join("Projects"), dir.0.join("Projects link")).unwrap();
        std::os::unix::fs::symlink("/nonexistent", dir.0.join("broken")).unwrap();
        let mut entries = scan(&dir.0, 0, "C").unwrap();
        entries.sort_by(|a, b| a.name.cmp(&b.name));
        let names: Vec<&str> = entries.iter().map(|e| e.name.as_str()).collect();
        assert_eq!(names, ["Projects", "Projects link", "broken", "note.txt"]);
        assert!(entries[1].is_dir && entries[1].is_link);
        assert!(entries[2].is_link && !entries[2].is_dir);
        assert_eq!(entries[3].size, 5);
        assert_eq!(entries[3].key(), "file:note.txt");
    }

    #[test]
    fn the_trash_follows_the_specification() {
        let dir = Dir::new("trash");
        let desk = dir.0.join("Desktop");
        let bin = dir.0.join("share/Trash");
        std::fs::create_dir_all(&desk).unwrap();
        std::fs::write(desk.join("a b%.txt"), "x").unwrap();
        let moved = trash(&desk.join("a b%.txt"), &bin, "2026-11-26T10:00:00").unwrap();
        assert_eq!(moved, bin.join("files/a b%.txt"));
        assert!(!desk.join("a b%.txt").exists());
        let info = std::fs::read_to_string(bin.join("info/a b%.txt.trashinfo")).unwrap();
        let real = std::fs::canonicalize(&desk).unwrap();
        assert_eq!(info, format!("[Trash Info]\nPath={}/a%20b%25.txt\nDeletionDate=2026-11-26T10:00:00\n", trash_quote(real.as_os_str().as_bytes())));
        assert_eq!(std::fs::metadata(bin.join("info")).unwrap().mode() & 0o777, 0o700);
        // The same name again gets .2.
        std::fs::write(desk.join("a b%.txt"), "y").unwrap();
        assert_eq!(trash(&desk.join("a b%.txt"), &bin, "2026-11-26T10:00:01").unwrap(), bin.join("files/a b%.txt.2"));
        // A link is trashed as the link; its target stays.
        let outside = dir.0.join("outside.txt");
        std::fs::write(&outside, "keep me").unwrap();
        std::os::unix::fs::symlink(&outside, desk.join("link")).unwrap();
        trash(&desk.join("link"), &bin, "2026-11-26T10:00:02").unwrap();
        assert!(std::fs::symlink_metadata(bin.join("files/link")).unwrap().file_type().is_symlink());
        assert_eq!(std::fs::read_to_string(&outside).unwrap(), "keep me");
        // A file without a record in files/ is left alone.
        std::fs::write(bin.join("files/stray"), "s").unwrap();
        std::fs::write(desk.join("stray"), "t").unwrap();
        assert_eq!(trash(&desk.join("stray"), &bin, "x").unwrap(), bin.join("files/stray.2"));
        assert_eq!(std::fs::read_to_string(bin.join("files/stray")).unwrap(), "s");
        assert!(trash(&desk.join("missing"), &bin, "x").is_err());
        assert!(trash(&bin.join("files/stray"), &bin, "x").is_err(), "already in the Trash");
    }

    #[test]
    fn new_items_renames_copies_and_moves() {
        let dir = Dir::new("ops");
        let desk = dir.0.join("Desktop");
        let other = dir.0.join("Other");
        std::fs::create_dir_all(&desk).unwrap();
        std::fs::create_dir_all(&other).unwrap();
        assert_eq!(create_folder(&desk).unwrap(), "New folder");
        assert_eq!(create_folder(&desk).unwrap(), "New folder (2)");
        assert_eq!(create_text_document(&desk).unwrap(), "New text document.txt");
        assert_eq!(create_text_document(&desk).unwrap(), "New text document (2).txt");
        // Renaming never replaces.
        assert_eq!(rename(&desk, &"New folder (2)".into(), "Notes"), Ok(()));
        assert!(rename(&desk, &"Notes".into(), "New folder").is_err(), "taken");
        assert!(rename(&desk, &"Notes".into(), "a/b").is_err());
        assert_eq!(rename(&desk, &"Notes".into(), "notes"), Ok(()), "only the case changes");
        assert!(desk.join("notes").is_dir());
        // A copy into the same folder is "- Copy"; a move into it does nothing.
        std::fs::write(desk.join("plan.txt"), "p").unwrap();
        let report = transfer(&[desk.join("plan.txt")], &desk, false);
        assert_eq!(report.done, ["plan - Copy.txt"]);
        assert_eq!(transfer(&[desk.join("plan.txt")], &desk, true), Report::default());
        // From elsewhere, with links copied as links and nothing overwritten.
        std::fs::create_dir_all(other.join("tree/inner")).unwrap();
        std::fs::write(other.join("tree/inner/file"), "f").unwrap();
        std::os::unix::fs::symlink("/etc/passwd", other.join("tree/link")).unwrap();
        std::fs::write(other.join("plan.txt"), "other").unwrap();
        let report = transfer(&[other.join("tree"), other.join("plan.txt")], &desk, false);
        assert_eq!(report.done, ["tree", "plan (2).txt"]);
        assert_eq!(std::fs::read_to_string(desk.join("tree/inner/file")).unwrap(), "f");
        assert!(std::fs::symlink_metadata(desk.join("tree/link")).unwrap().file_type().is_symlink());
        assert_eq!(std::fs::read_to_string(desk.join("plan.txt")).unwrap(), "p", "not overwritten");
        let report = transfer(&[other.join("plan.txt")], &desk, true);
        assert_eq!(report.done, ["plan (3).txt"]);
        assert!(!other.join("plan.txt").exists());
        // A folder cannot go into itself.
        let report = transfer(&[desk.join("tree")], &desk.join("tree/inner"), true);
        assert_eq!(report.failed.len(), 1);
        // A drop on a folder icon.
        let report = move_into(&desk, &["plan.txt".into(), "notes".into()], &"notes".into());
        assert_eq!(report.done, ["plan.txt"]);
        assert!(desk.join("notes/plan.txt").exists());
        assert!(!move_into(&desk, &["tree".into()], &"plan - Copy.txt".into()).failed.is_empty(), "not a folder");
    }

    #[test]
    fn uri_lists_and_the_gnome_format_round_trip() {
        let paths = vec![PathBuf::from("/home/asha/Desktop/a b#c.txt"), PathBuf::from("/tmp/नमस्ते")];
        let list = encode_uri_list(&paths);
        assert!(String::from_utf8(list.clone()).unwrap().starts_with("file:///home/asha/Desktop/a%20b%23c.txt\r\n"));
        assert_eq!(decode_uri_list(&list), paths);
        assert_eq!(decode_gnome(&encode_gnome(&paths, true)), Some((true, paths.clone())));
        assert_eq!(decode_gnome(b"x-special/nautilus-clipboard\ncopy\nfile:///a\n"), Some((false, vec![PathBuf::from("/a")])));
        assert_eq!(decode_gnome(b"move\nfile:///a"), None);
        let odd = b"# a comment\nhttp://example.com/x\nfile://localhost/srv/x\nfile:///a/../etc/passwd\nfile:///bad%00\nfile:///ok%2\nfile:relative\n";
        assert_eq!(decode_uri_list(odd), [PathBuf::from("/srv/x")], "only local, normal paths");
        assert_eq!(clipboard_data("application/x-kde-cutselection", &paths, true).unwrap(), b"1");
        assert_eq!(clipboard_data("text/plain", &paths[..1], false).unwrap(), b"/home/asha/Desktop/a b#c.txt");
        assert!(clipboard_data("image/png", &paths, false).is_none());
    }

    #[test]
    fn the_positions_file_is_read_whatever_it_holds() {
        let saved = Saved { sort: SortKey::Modified, positions: [("file:a.txt".to_owned(), (0.0, 2.0)), ("system:trash".to_owned(), (1.5, 0.25))].into() };
        assert_eq!(Saved::parse(&saved.to_json()), saved);
        for bad in ["", "null", "[]", "{\"icons\": 5}", "not json"] {
            assert_eq!(Saved::parse(bad), Saved::default(), "{bad}");
        }
        let odd = Saved::parse(r#"{"sort": "colour", "icons": {"file:ok": [1, 2], "file:../x": [0, 0], "elsewhere": [0, 0], "file:far": [5000, 0], "file:neg": [-1, 0], "file:three": [1, 2, 3], "file:text": ["a", 0]}}"#);
        assert_eq!(odd.sort, SortKey::Name);
        assert_eq!(odd.positions.keys().collect::<Vec<_>>(), ["file:ok"]);
        let dir = Dir::new("layout");
        let path = dir.0.join("state/boswas/desktop-layout.json");
        write_private(&path, &saved.to_json()).unwrap();
        assert_eq!(std::fs::metadata(&path).unwrap().mode() & 0o777, 0o600);
        assert_eq!(Saved::read(&path), saved);
        std::fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink("/etc/passwd", &path).unwrap();
        assert_eq!(Saved::read(&path), Saved::default(), "a link is not read");
        assert!(write_private(&path, "{}").is_err(), "nor written through");
    }

    #[test]
    fn the_view_menu_changes_one_key_and_nothing_else() {
        let original = "# my settings\n[keyboard]\nlayout = \"in\" # India\n\n[desktop]\nicons = true\nicon_size = \"small\"   # tiny\n\n[taskbar]\nalignment = \"left\"\n";
        let changed = set_desktop_key(original, "icon_size", &Setting::Text("large")).unwrap();
        assert_eq!(changed, original.replace("icon_size = \"small\"   # tiny", "icon_size = \"large\""));
        let added = set_desktop_key(original, "auto_arrange", &Setting::Bool(true)).unwrap();
        assert!(added.contains("[desktop]\nauto_arrange = true\nicons = true\n"), "{added}");
        let fresh = set_desktop_key("", "icons", &Setting::Bool(false)).unwrap();
        assert_eq!(fresh, "[desktop]\nicons = false\n");
        let appended = set_desktop_key("[keyboard]\nlayout = \"us\"\n", "align_to_grid", &Setting::Bool(false)).unwrap();
        assert_eq!(appended, "[keyboard]\nlayout = \"us\"\n\n[desktop]\nalign_to_grid = false\n");
        // Shapes it does not understand are left to Settings.
        assert!(set_desktop_key("desktop.icons = true\n", "icons", &Setting::Bool(false)).is_err());
        assert!(set_desktop_key("desktop = { icons = true }\n", "icons", &Setting::Bool(false)).is_err());
        assert!(set_desktop_key("[desktop\nbroken", "icons", &Setting::Bool(false)).is_err());
        assert!(set_desktop_key("[desktop]\nsystem_icons = [\n  \"home\",\n  \"icons = 1\"\n]\n", "icons", &Setting::Bool(false)).is_ok_and(|t| t.contains("\"icons = 1\"")));
        // The file itself.
        let dir = Dir::new("toml");
        let path = dir.0.join("boswas/compositor.toml");
        write_desktop_key(&path, "icon_size", &Setting::Text("large")).unwrap();
        write_desktop_key(&path, "icons", &Setting::Bool(false)).unwrap();
        let table: toml::Table = std::fs::read_to_string(&path).unwrap().parse().unwrap();
        assert_eq!(table["desktop"]["icon_size"].as_str(), Some("large"));
        assert_eq!(table["desktop"]["icons"].as_bool(), Some(false));
        assert_eq!(std::fs::metadata(&path).unwrap().mode() & 0o777, 0o600);
    }
}
