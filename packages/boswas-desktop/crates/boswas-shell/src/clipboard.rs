//! The clipboard history (Super+V), like Windows': what the user copies,
//! newest first.
//!
//! * The last 25 items: text (rich text as its plain text), pictures up to
//!   [`MAX_IMAGE_BYTES`], and lists of files as their URIs. The same item
//!   copied again moves to the top instead of being listed twice.
//! * Kept in memory only, and so gone when the session ends, except the
//!   items the user pinned: those are kept in
//!   `$XDG_STATE_HOME/boswas/clipboard-pinned.json` (0600, replaced as a
//!   whole, size-capped and read defensively; pictures only when small).
//! * Never what a program marks as secret (password managers do), never
//!   while the session is locked, and never in the lock screen or the
//!   login screen, which do not watch the clipboard at all.
//! * `[clipboard] history = false` keeps nothing but the pinned items;
//!   `cleared_at` (Settings > System > Clipboard > Clear) forgets what was
//!   copied before then, pinned items excepted.
//!
//! This file holds the history and its file; `datacontrol.rs` watches the
//! clipboard and `clipview.rs` draws the flyout.

use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
    rc::Rc,
    sync::Arc,
};

use serde_json::{json, Value};
use tiny_skia::Pixmap;

/// Most items kept that are not pinned.
pub const MAX_ITEMS: usize = 25;
/// Most pinned items.
pub const MAX_PINNED: usize = 25;
/// Longest text kept, in bytes (longer text is not kept at all).
pub const MAX_TEXT_BYTES: usize = 1024 * 1024;
/// Largest picture kept (PNG bytes).
pub const MAX_IMAGE_BYTES: usize = 8 * 1024 * 1024;
/// Largest picture kept in the pinned file.
pub const MAX_PINNED_IMAGE_BYTES: usize = 256 * 1024;
/// Most files of one copied list.
pub const MAX_FILES: usize = 1000;
/// Largest pinned file read or written.
pub const MAX_PINNED_FILE_BYTES: u64 = 4 * 1024 * 1024;

/// What an item holds.
#[derive(Clone, Debug)]
pub enum Content {
    Text(String),
    /// A PNG, its size, and a small picture for the card (made on the
    /// thread that read it).
    Image { png: Arc<Vec<u8>>, width: u32, height: u32, thumbnail: Option<Rc<Pixmap>> },
    /// Files and folders, as URIs (`file:///home/...`).
    Files(Vec<String>),
}

impl Content {
    /// Whether two contents are the same item (pictures by their bytes).
    pub fn same(&self, other: &Content) -> bool {
        match (self, other) {
            (Content::Text(a), Content::Text(b)) => a == b,
            (Content::Image { png: a, .. }, Content::Image { png: b, .. }) => a == b,
            (Content::Files(a), Content::Files(b)) => a == b,
            _ => false,
        }
    }

    /// The text a search looks in.
    pub fn searchable(&self) -> String {
        match self {
            Content::Text(text) => text.to_lowercase(),
            Content::Image { .. } => "picture image".into(),
            Content::Files(uris) => uris.iter().map(|u| file_name(u)).collect::<Vec<_>>().join(" ").to_lowercase(),
        }
    }

    /// The MIME types and bytes this item is offered as when it goes back
    /// on the clipboard.
    pub fn offers(&self) -> Vec<(String, Arc<Vec<u8>>)> {
        match self {
            Content::Text(text) => {
                let bytes = Arc::new(text.clone().into_bytes());
                TEXT_TYPES.iter().map(|m| ((*m).to_owned(), bytes.clone())).collect()
            }
            Content::Image { png, .. } => vec![("image/png".into(), png.clone())],
            Content::Files(uris) => {
                let list = Arc::new(uris.iter().map(|u| format!("{u}\r\n")).collect::<String>().into_bytes());
                let gnome = Arc::new(format!("copy\n{}", uris.join("\n")).into_bytes());
                let plain = Arc::new(uris.iter().map(|u| path_of(u)).collect::<Vec<_>>().join("\n").into_bytes());
                vec![
                    ("text/uri-list".into(), list),
                    ("x-special/gnome-copied-files".into(), gnome),
                    ("text/plain;charset=utf-8".into(), plain.clone()),
                    ("text/plain".into(), plain),
                ]
            }
        }
    }
}

/// The text types offered and read, best first.
pub const TEXT_TYPES: [&str; 5] = ["text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "STRING", "TEXT"];

/// Types that mark what is on the clipboard as not for any history:
/// password managers (KDE's hint, read for its value), and the markers
/// other systems' programs use (present means secret or transient).
pub const PASSWORD_HINT: &str = "x-kde-passwordManagerHint";
pub const CONCEALED_TYPES: [&str; 5] = [
    "application/x-nspasteboard-concealed-type",
    "org.nspasteboard.ConcealedType",
    "org.nspasteboard.TransientType",
    "ExcludeClipboardContentFromMonitorProcessing",
    "x-kde-onetime-copy",
];
/// Windows' marker, through Wine: a DWORD 0 means "not for the history".
pub const HISTORY_HINT: &str = "CanIncludeInClipboardHistory";
/// What the shell itself puts on the clipboard carries this type: the
/// history does not take it in again.
pub const OWN_TYPE: &str = "application/x-boswas-shell";

/// How what is on the clipboard should be read, decided from its types.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Plan {
    /// Not for the history (marked secret or transient, or the shell's own).
    Skip,
    /// Read these hint types first; they may say it is secret.
    Hints(Vec<String>),
    /// Read this type as the item.
    Read { mime: String, kind: Kind },
    /// Nothing the history can hold (only types it does not know).
    Nothing,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Text,
    /// Rich text without a plain-text version: kept as its text.
    Html,
    Image,
    Files,
}

/// What to read of a clipboard offer with these MIME types. `hints_read`
/// is set once the hint types have been read and said nothing against it.
pub fn plan(types: &[String], hints_read: bool) -> Plan {
    let has = |wanted: &str| types.iter().any(|t| t.eq_ignore_ascii_case(wanted));
    if has(OWN_TYPE) || CONCEALED_TYPES.iter().any(|t| has(t)) {
        return Plan::Skip;
    }
    if !hints_read {
        let hints: Vec<String> = [PASSWORD_HINT, HISTORY_HINT].iter().filter(|t| has(t)).map(|t| (*t).to_owned()).collect();
        if !hints.is_empty() {
            return Plan::Hints(hints);
        }
    }
    if has("text/uri-list") {
        return Plan::Read { mime: "text/uri-list".into(), kind: Kind::Files };
    }
    if let Some(text) = TEXT_TYPES.iter().find(|t| has(t)) {
        return Plan::Read { mime: (*text).to_owned(), kind: Kind::Text };
    }
    if has("image/png") {
        return Plan::Read { mime: "image/png".into(), kind: Kind::Image };
    }
    if has("text/html") {
        return Plan::Read { mime: "text/html".into(), kind: Kind::Html };
    }
    Plan::Nothing
}

/// Whether the hint values read say "keep out of the history".
pub fn hints_say_secret(hints: &[(String, Vec<u8>)]) -> bool {
    hints.iter().any(|(mime, value)| {
        if mime.eq_ignore_ascii_case(PASSWORD_HINT) {
            // KDE's convention: the value "secret". Anything unreadable is
            // treated the same: better to keep a password out.
            let value = value.trim_ascii();
            !value.is_empty() && value != b"public"
        } else if mime.eq_ignore_ascii_case(HISTORY_HINT) {
            value.first().is_none_or(|b| *b == 0 || *b == b'0')
        } else {
            false
        }
    })
}

/// Makes an item's content from what was read, or nothing when it cannot
/// be one (empty, too large, not text, a damaged list).
pub fn content_from(kind: Kind, bytes: &[u8]) -> Option<Content> {
    match kind {
        Kind::Text => {
            if bytes.len() > MAX_TEXT_BYTES {
                return None;
            }
            let text = String::from_utf8_lossy(bytes).replace('\0', "");
            (!text.trim().is_empty()).then_some(Content::Text(text))
        }
        Kind::Html => {
            if bytes.len() > MAX_TEXT_BYTES {
                return None;
            }
            let text = html_text(&String::from_utf8_lossy(bytes));
            (!text.trim().is_empty()).then_some(Content::Text(text))
        }
        Kind::Files => {
            let text = std::str::from_utf8(bytes).ok()?;
            let uris: Vec<String> = text
                .lines()
                .map(str::trim)
                .filter(|l| !l.is_empty() && !l.starts_with('#'))
                .take(MAX_FILES + 1)
                .map(str::to_owned)
                .collect();
            if uris.is_empty() || uris.len() > MAX_FILES || uris.iter().any(|u| u.len() > 4096 || !u.contains(':') || u.chars().any(char::is_control)) {
                return None;
            }
            Some(Content::Files(uris))
        }
        // Pictures are decoded on the reading thread (see datacontrol.rs).
        Kind::Image => None,
    }
}

/// The text of an HTML fragment: tags removed, the common entities
/// replaced, blocks on lines of their own.
pub fn html_text(html: &str) -> String {
    let mut out = String::new();
    let mut rest = html;
    let mut skip_until: Option<&str> = None;
    while let Some(open) = rest.find('<') {
        if skip_until.is_none() {
            out.push_str(&rest[..open]);
        }
        let after = &rest[open..];
        let Some(close) = after.find('>') else {
            rest = "";
            break;
        };
        let tag = after[1..close].trim().to_ascii_lowercase();
        let name: String = tag.trim_start_matches('/').chars().take_while(|c| c.is_ascii_alphanumeric()).collect();
        if let Some(end) = skip_until {
            if tag.starts_with('/') && name == end {
                skip_until = None;
            }
        } else if !tag.starts_with('/') && matches!(name.as_str(), "script" | "style" | "head" | "title") {
            skip_until = Some(match name.as_str() {
                "script" => "script",
                "style" => "style",
                "head" => "head",
                _ => "title",
            });
        } else if matches!(name.as_str(), "br" | "p" | "div" | "li" | "tr" | "h1" | "h2" | "h3" | "h4" | "h5" | "h6") && !out.ends_with('\n') && !out.is_empty() {
            out.push('\n');
        }
        rest = &after[close + 1..];
    }
    if skip_until.is_none() {
        out.push_str(rest);
    }
    let text = out
        .replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&amp;", "&");
    text.lines().map(str::trim_end).collect::<Vec<_>>().join("\n").trim().to_owned()
}

/// The last part of a URI's path, decoded (for cards and searching).
pub fn file_name(uri: &str) -> String {
    let path = path_of(uri);
    let trimmed = path.trim_end_matches('/');
    trimmed.rsplit('/').next().filter(|n| !n.is_empty()).unwrap_or(trimmed).to_owned()
}

/// A `file://` URI as a path (percent escapes decoded); other URIs as
/// they are.
pub fn path_of(uri: &str) -> String {
    let Some(rest) = uri.strip_prefix("file://") else {
        return uri.to_owned();
    };
    // "file://host/path": the host is dropped.
    let path = match rest.find('/') {
        Some(slash) => &rest[slash..],
        None => rest,
    };
    let bytes = path.as_bytes();
    let hex = |b: u8| (b as char).to_digit(16).map(|d| d as u8);
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(high), Some(low)) = (hex(bytes[i + 1]), hex(bytes[i + 2])) {
                out.push(high * 16 + low);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// One item of the history.
#[derive(Clone, Debug)]
pub struct Clip {
    /// Stable for the item's life (for the flyout's controls).
    pub id: u64,
    pub content: Content,
    pub pinned: bool,
    /// When it was copied (Unix seconds; for "Clear clipboard data").
    pub copied_at: i64,
}

/// The history.
#[derive(Debug)]
pub struct History {
    /// Newest first; pinned items keep their place among the others.
    pub items: Vec<Clip>,
    /// `[clipboard] history`.
    pub enabled: bool,
    /// The `cleared_at` already acted on.
    pub cleared_at: i64,
    next_id: u64,
    /// Where pinned items are kept (`None`: nowhere, as in the tests).
    pub path: Option<PathBuf>,
}

impl Default for History {
    fn default() -> Self {
        Self { items: Vec::new(), enabled: true, cleared_at: 0, next_id: 1, path: None }
    }
}

impl History {
    /// The history of a desktop session: the pinned items of the file.
    pub fn load() -> Self {
        let path = crate::config::state_dir().map(|dir| dir.join("clipboard-pinned.json"));
        let mut history = Self { path: path.clone(), ..Self::default() };
        if let Some(path) = path {
            for content in read_pinned(&path) {
                history.push_pinned(content);
            }
        }
        history
    }

    fn take_id(&mut self) -> u64 {
        let id = self.next_id;
        self.next_id += 1;
        id
    }

    fn push_pinned(&mut self, content: Content) {
        if self.items.iter().filter(|c| c.pinned).count() >= MAX_PINNED || self.items.iter().any(|c| c.content.same(&content)) {
            return;
        }
        let id = self.take_id();
        self.items.push(Clip { id, content, pinned: true, copied_at: 0 });
    }

    /// Something was copied: it goes to the top (moved there if it is in
    /// the list already). Returns whether the list changed.
    pub fn add(&mut self, content: Content, now: i64) -> bool {
        if let Some(index) = self.items.iter().position(|c| c.content.same(&content)) {
            let mut clip = self.items.remove(index);
            clip.copied_at = now;
            self.items.insert(0, clip);
            return index != 0;
        }
        if !self.enabled {
            return false;
        }
        let id = self.take_id();
        self.items.insert(0, Clip { id, content, pinned: false, copied_at: now });
        // The oldest that is not pinned goes.
        while self.items.iter().filter(|c| !c.pinned).count() > MAX_ITEMS {
            if let Some(last) = self.items.iter().rposition(|c| !c.pinned) {
                self.items.remove(last);
            }
        }
        true
    }

    pub fn get(&self, id: u64) -> Option<&Clip> {
        self.items.iter().find(|c| c.id == id)
    }

    /// Pins or unpins an item; the pinned items are written out.
    pub fn set_pinned(&mut self, id: u64, pinned: bool) -> bool {
        if pinned && self.items.iter().filter(|c| c.pinned).count() >= MAX_PINNED {
            return false;
        }
        let Some(clip) = self.items.iter_mut().find(|c| c.id == id) else {
            return false;
        };
        if clip.pinned == pinned {
            return false;
        }
        clip.pinned = pinned;
        self.save();
        true
    }

    pub fn remove(&mut self, id: u64) -> bool {
        let before = self.items.len();
        let was_pinned = self.items.iter().any(|c| c.id == id && c.pinned);
        self.items.retain(|c| c.id != id);
        if was_pinned {
            self.save();
        }
        self.items.len() != before
    }

    /// "Clear all": everything but the pinned items.
    pub fn clear(&mut self) -> bool {
        let before = self.items.len();
        self.items.retain(|c| c.pinned);
        self.items.len() != before
    }

    /// Applies the settings: history on or off (off forgets what is not
    /// pinned), and a newer "Clear clipboard data" (forgets what was
    /// copied before it). Returns whether anything was forgotten by a
    /// clear (the clipboard itself should then be emptied when it holds
    /// something older).
    pub fn configure(&mut self, enabled: bool, cleared_at: i64) -> bool {
        let mut cleared = false;
        if !enabled && self.enabled {
            self.clear();
        }
        self.enabled = enabled;
        if cleared_at > self.cleared_at {
            self.items.retain(|c| c.pinned || c.copied_at >= cleared_at);
            self.cleared_at = cleared_at;
            cleared = true;
        }
        cleared
    }

    /// The items a search shows (all of them without a search).
    pub fn matching(&self, query: &str) -> Vec<&Clip> {
        let query = query.trim().to_lowercase();
        self.items.iter().filter(|c| query.is_empty() || c.content.searchable().contains(&query)).collect()
    }

    /// Writes the pinned items, as a whole, readable by the user only.
    pub fn save(&self) {
        let Some(path) = &self.path else {
            return;
        };
        let text = pinned_json(self.items.iter().filter(|c| c.pinned).map(|c| &c.content));
        if let Err(err) = write_private(path, text.as_bytes()) {
            tracing::warn!("could not keep the pinned clipboard items in {}: {err}", path.display());
        }
    }
}

/// The pinned file's text: pictures larger than the limit are left out.
pub fn pinned_json<'a>(items: impl Iterator<Item = &'a Content>) -> String {
    let mut list = Vec::new();
    let mut size = 0usize;
    for content in items {
        let entry = match content {
            Content::Text(text) => json!({ "kind": "text", "text": text }),
            Content::Files(uris) => json!({ "kind": "files", "uris": uris }),
            Content::Image { png, .. } if png.len() <= MAX_PINNED_IMAGE_BYTES => json!({ "kind": "image", "png": base64_encode(png) }),
            Content::Image { .. } => continue,
        };
        let length = entry.to_string().len();
        if size + length > MAX_PINNED_FILE_BYTES as usize - 1024 {
            break;
        }
        size += length;
        list.push(entry);
    }
    json!({ "version": 1, "items": list }).to_string()
}

/// The pinned items of a file, skipping anything that is not as written:
/// a file that is too large, not a regular file (a link is not followed),
/// not JSON, or entries of an unknown kind or beyond the limits.
pub fn read_pinned(path: &Path) -> Vec<Content> {
    use std::os::unix::fs::OpenOptionsExt;
    let Ok(file) = std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path) else {
        return Vec::new();
    };
    if !file.metadata().is_ok_and(|m| m.is_file()) {
        return Vec::new();
    }
    let mut text = String::new();
    if file.take(MAX_PINNED_FILE_BYTES + 1).read_to_string(&mut text).is_err() || text.len() as u64 > MAX_PINNED_FILE_BYTES {
        return Vec::new();
    }
    parse_pinned(&text)
}

pub fn parse_pinned(text: &str) -> Vec<Content> {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return Vec::new();
    };
    let Some(items) = value.get("items").and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut out: Vec<Content> = Vec::new();
    for entry in items.iter().take(MAX_PINNED) {
        let content = match entry.get("kind").and_then(Value::as_str) {
            Some("text") => entry.get("text").and_then(Value::as_str).and_then(|t| content_from(Kind::Text, t.as_bytes())),
            Some("files") => entry.get("uris").and_then(Value::as_array).and_then(|uris| {
                let list: Option<Vec<&str>> = uris.iter().map(Value::as_str).collect();
                content_from(Kind::Files, list?.join("\n").as_bytes())
            }),
            Some("image") => entry
                .get("png")
                .and_then(Value::as_str)
                .and_then(base64_decode)
                .filter(|png| png.len() <= MAX_PINNED_IMAGE_BYTES && png.starts_with(b"\x89PNG\r\n\x1a\n"))
                .and_then(|png| image_content(png, 0)),
            _ => None,
        };
        if let Some(content) = content {
            if !out.iter().any(|c| c.same(&content)) {
                out.push(content);
            }
        }
    }
    out
}

/// A picture item from PNG bytes: decoded (to learn that it is one, and
/// its size) with a small picture for its card. `max` bounds the bytes
/// (0: [`MAX_IMAGE_BYTES`]).
pub fn image_content(png: Vec<u8>, max: usize) -> Option<Content> {
    let max = if max == 0 { MAX_IMAGE_BYTES } else { max };
    if png.len() > max {
        return None;
    }
    let picture = boswas_ui::icons::decode_image(&png)?;
    let (width, height) = (picture.width(), picture.height());
    let thumbnail = thumbnail(&picture, THUMBNAIL.0, THUMBNAIL.1).map(Rc::new);
    Some(Content::Image { png: Arc::new(png), width, height, thumbnail })
}

/// The largest picture on a card, in device pixels (two for each logical
/// pixel, so it stays sharp at 200 %).
pub const THUMBNAIL: (u32, u32) = (640, 240);

/// A picture made smaller to fit `max_w` x `max_h`, keeping its shape.
pub fn thumbnail(picture: &Pixmap, max_w: u32, max_h: u32) -> Option<Pixmap> {
    let (w, h) = (picture.width() as f32, picture.height() as f32);
    let scale = (max_w as f32 / w).min(max_h as f32 / h).min(1.0);
    let (tw, th) = ((w * scale).round().max(1.0) as u32, (h * scale).round().max(1.0) as u32);
    let mut out = Pixmap::new(tw, th)?;
    let paint = tiny_skia::PixmapPaint { quality: tiny_skia::FilterQuality::Bilinear, ..Default::default() };
    out.draw_pixmap(0, 0, picture.as_ref(), &paint, tiny_skia::Transform::from_scale(scale, scale), None);
    Some(out)
}

/// Writes a file only the user can read, replacing the old one at once
/// (a new file renamed over it), in a directory only the user can enter
/// when it has to be made.
pub fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    let dir = path.parent().ok_or_else(|| std::io::Error::other("no directory"))?;
    std::fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
    let temporary = path.with_extension(format!("tmp{}", std::process::id()));
    let _ = std::fs::remove_file(&temporary);
    let result = (|| {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        std::fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

const BASE64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn base64_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16) | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8) | u32::from(*chunk.get(2).unwrap_or(&0));
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(BASE64[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

pub fn base64_decode(text: &str) -> Option<Vec<u8>> {
    let text = text.trim_end_matches('=');
    let mut out = Vec::with_capacity(text.len() * 3 / 4);
    let mut buffer = 0u32;
    let mut bits = 0;
    for c in text.bytes() {
        let value = BASE64.iter().position(|b| *b == c)? as u32;
        buffer = (buffer << 6) | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buffer >> bits) as u8);
            buffer &= (1 << bits) - 1;
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(t: &str) -> Content {
        Content::Text(t.into())
    }

    fn texts(history: &History) -> Vec<String> {
        history
            .items
            .iter()
            .map(|c| match &c.content {
                Content::Text(t) => t.clone(),
                Content::Files(f) => f.join(","),
                Content::Image { width, height, .. } => format!("{width}x{height}"),
            })
            .collect()
    }

    fn types(list: &[&str]) -> Vec<String> {
        list.iter().map(|t| (*t).to_owned()).collect()
    }

    #[test]
    fn the_newest_comes_first_once_and_the_oldest_unpinned_goes() {
        let mut history = History::default();
        assert!(history.add(text("one"), 1));
        assert!(history.add(text("two"), 2));
        assert!(!history.add(text("two"), 3), "the same again at the top changes nothing");
        assert!(history.add(text("one"), 4), "copied again: moved to the top");
        assert_eq!(texts(&history), ["one", "two"]);
        assert_eq!(history.items[0].copied_at, 4);
        let id = history.items[1].id;
        assert!(history.set_pinned(id, true));
        for n in 0..40 {
            history.add(text(&format!("item {n}")), 10 + n);
        }
        assert_eq!(history.items.iter().filter(|c| !c.pinned).count(), MAX_ITEMS);
        assert_eq!(history.items[0].content.searchable(), "item 39");
        assert!(history.items.iter().any(|c| c.pinned && c.content.same(&text("two"))), "pinned items stay");
        assert!(!history.items.iter().any(|c| c.content.same(&text("one"))), "the oldest went");
        // Pictures are the same item when their bytes are.
        let png = Arc::new(vec![1, 2, 3]);
        let a = Content::Image { png: png.clone(), width: 1, height: 1, thumbnail: None };
        let b = Content::Image { png: Arc::new(vec![1, 2, 3]), width: 1, height: 1, thumbnail: None };
        assert!(a.same(&b) && !a.same(&text("x")));
    }

    #[test]
    fn clear_all_delete_and_pinning() {
        let mut history = History::default();
        for t in ["a", "b", "c"] {
            history.add(text(t), 1);
        }
        let b = history.items[1].id;
        history.set_pinned(b, true);
        assert!(!history.set_pinned(b, true), "already pinned");
        assert!(history.clear());
        assert_eq!(texts(&history), ["b"]);
        assert!(history.remove(b));
        assert!(history.items.is_empty() && !history.remove(b));
    }

    #[test]
    fn history_off_keeps_only_pinned_items_and_a_clear_forgets_what_came_before() {
        let mut history = History::default();
        history.add(text("old"), 100);
        history.add(text("pinned"), 100);
        let pinned = history.items[0].id;
        history.set_pinned(pinned, true);
        history.add(text("new"), 300);
        // Settings > Clear clipboard data at 200.
        assert!(history.configure(true, 200));
        assert_eq!(texts(&history), ["new", "pinned"]);
        assert!(!history.configure(true, 200), "the same clear once only");
        assert_eq!(texts(&history), ["new", "pinned"]);
        // Off: nothing new is kept, what was is forgotten, pinned stays.
        history.configure(false, 200);
        assert_eq!(texts(&history), ["pinned"]);
        assert!(!history.add(text("while off"), 400));
        history.add(text("pinned"), 400);
        assert_eq!(texts(&history), ["pinned"]);
        history.configure(true, 200);
        assert!(history.add(text("on again"), 500));
    }

    #[test]
    fn secrets_and_the_shell_s_own_items_are_never_read() {
        assert_eq!(plan(&types(&["text/plain", "x-kde-passwordManagerHint"]), false), Plan::Hints(types(&["x-kde-passwordManagerHint"])));
        assert_eq!(plan(&types(&["text/plain", "x-kde-passwordManagerHint"]), true), Plan::Read { mime: "text/plain".into(), kind: Kind::Text });
        assert!(hints_say_secret(&[("x-kde-passwordManagerHint".into(), b"secret".to_vec())]));
        assert!(hints_say_secret(&[("x-kde-passwordManagerHint".into(), b"secret\n".to_vec())]));
        assert!(hints_say_secret(&[("x-kde-passwordManagerHint".into(), b"whatever".to_vec())]), "unknown values count as secret");
        assert!(!hints_say_secret(&[("x-kde-passwordManagerHint".into(), Vec::new())]));
        assert!(hints_say_secret(&[("CanIncludeInClipboardHistory".into(), vec![0, 0, 0, 0])]));
        assert!(!hints_say_secret(&[("CanIncludeInClipboardHistory".into(), vec![1, 0, 0, 0])]));
        for marker in CONCEALED_TYPES.iter().chain([&OWN_TYPE]) {
            assert_eq!(plan(&types(&["text/plain", marker]), false), Plan::Skip, "{marker}");
        }
        assert_eq!(plan(&types(&["TEXT/PLAIN", "ORG.NSPASTEBOARD.CONCEALEDTYPE"]), true), Plan::Skip, "types compare without case");
    }

    #[test]
    fn files_beat_text_beat_pictures_beat_rich_text() {
        let read = |list: &[&str]| match plan(&types(list), true) {
            Plan::Read { mime, kind } => (mime, kind),
            other => panic!("{other:?}"),
        };
        assert_eq!(read(&["text/plain", "text/uri-list", "x-special/gnome-copied-files"]), ("text/uri-list".into(), Kind::Files));
        assert_eq!(read(&["text/html", "UTF8_STRING", "text/plain;charset=utf-8"]), ("text/plain;charset=utf-8".into(), Kind::Text));
        assert_eq!(read(&["image/png", "text/html"]), ("image/png".into(), Kind::Image));
        assert_eq!(read(&["text/html"]), ("text/html".into(), Kind::Html));
        assert_eq!(plan(&types(&["application/x-something"]), true), Plan::Nothing);
        assert_eq!(plan(&[], true), Plan::Nothing);
    }

    #[test]
    fn what_was_read_becomes_an_item_or_nothing() {
        assert!(matches!(content_from(Kind::Text, b"hello"), Some(Content::Text(t)) if t == "hello"));
        assert!(content_from(Kind::Text, b"  \n ").is_none(), "nothing but space");
        assert!(content_from(Kind::Text, &vec![b'a'; MAX_TEXT_BYTES + 1]).is_none(), "too long");
        assert!(matches!(content_from(Kind::Text, b"a\0b\xff"), Some(Content::Text(t)) if t == "ab\u{fffd}"));
        let html = b"<html><head><style>p{}</style></head><body><p>Hello&nbsp;<b>world</b></p><p>A &amp; B</p><script>x()</script></body></html>";
        assert!(matches!(content_from(Kind::Html, html), Some(Content::Text(t)) if t == "Hello world\nA & B"));
        let files = content_from(Kind::Files, b"# comment\r\nfile:///home/asha/Report%202026.pdf\r\nfile:///tmp/a/\r\n").unwrap();
        assert!(matches!(&files, Content::Files(f) if f.len() == 2));
        assert_eq!(files.searchable(), "report 2026.pdf a");
        assert!(content_from(Kind::Files, b"not a uri").is_none());
        assert!(content_from(Kind::Files, b"").is_none());
        assert_eq!(path_of("file://localhost/home/x%41y"), "/home/xAy");
        assert_eq!(path_of("https://example.org/a"), "https://example.org/a");
        assert_eq!(file_name("file:///home/asha/Documents/"), "Documents");
        assert_eq!(path_of("file:///bad%zz%4"), "/bad%zz%4", "broken escapes stay as they are");
    }

    #[test]
    fn an_item_goes_back_on_the_clipboard_in_the_forms_programs_read() {
        let offers = text("hi").offers();
        assert_eq!(offers.iter().map(|(m, _)| m.as_str()).collect::<Vec<_>>(), TEXT_TYPES);
        assert!(offers.iter().all(|(_, b)| b.as_slice() == b"hi"));
        let files = Content::Files(vec!["file:///a%20b".into(), "file:///c".into()]).offers();
        let get = |m: &str| String::from_utf8(files.iter().find(|(t, _)| t == m).unwrap().1.to_vec()).unwrap();
        assert_eq!(get("text/uri-list"), "file:///a%20b\r\nfile:///c\r\n");
        assert_eq!(get("x-special/gnome-copied-files"), "copy\nfile:///a%20b\nfile:///c");
        assert_eq!(get("text/plain"), "/a b\n/c");
    }

    fn png(width: u32, height: u32) -> Vec<u8> {
        let picture = Pixmap::new(width, height).unwrap();
        boswas_ui::icons::encode_png(&picture).unwrap()
    }

    /// A picture that does not compress: larger than a pinned picture may be.
    fn noise(width: u32, height: u32) -> Vec<u8> {
        let mut picture = Pixmap::new(width, height).unwrap();
        let mut seed = 12345u32;
        for byte in picture.data_mut() {
            seed = seed.wrapping_mul(1_103_515_245).wrapping_add(12345);
            *byte = (seed >> 16) as u8 | 0x0f;
        }
        for pixel in picture.data_mut().chunks_exact_mut(4) {
            pixel[3] = 255;
        }
        boswas_ui::icons::encode_png(&picture).unwrap()
    }

    #[test]
    fn pinned_items_survive_in_their_file_and_a_damaged_file_harms_nothing() {
        let dir = tempfile_dir();
        let path = dir.join("state/boswas/clipboard-pinned.json");
        let mut history = History { path: Some(path.clone()), ..History::default() };
        history.add(text("kept"), 1);
        history.add(Content::Files(vec!["file:///home/asha/a.txt".into()]), 2);
        history.add(image_content(png(32, 16), 0).unwrap(), 3);
        let big = noise(400, 400);
        assert!(big.len() > MAX_PINNED_IMAGE_BYTES, "{}", big.len());
        history.add(image_content(big, 0).unwrap(), 4);
        history.add(text("not pinned"), 5);
        for clip in history.items.clone().iter().filter(|c| !c.content.same(&text("not pinned"))) {
            history.set_pinned(clip.id, true);
        }
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert_eq!(std::fs::metadata(path.parent().unwrap()).unwrap().permissions().mode() & 0o777, 0o700);
        let loaded = read_pinned(&path);
        // Newest first; the large picture is not kept in the file.
        assert_eq!(loaded.len(), 3);
        assert!(matches!(&loaded[0], Content::Image { width: 32, height: 16, thumbnail: Some(_), .. }));
        assert!(matches!(&loaded[1], Content::Files(f) if f[0] == "file:///home/asha/a.txt"));
        assert!(matches!(&loaded[loaded.len() - 1], Content::Text(t) if t == "kept"));
        // Damaged, hostile or strange files: nothing, or what is valid.
        for bad in ["", "not json", "[]", "{\"items\": 5}", "{\"items\": [{\"kind\": \"text\"}]}", "{\"items\": [{\"kind\": \"image\", \"png\": \"!!\"}]}"] {
            std::fs::write(&path, bad).unwrap();
            assert!(read_pinned(&path).is_empty(), "{bad}");
        }
        std::fs::write(&path, r#"{"items": [{"kind": "text", "text": "ok"}, {"kind": "files", "uris": [1]}, {"kind": "image", "png": "aGVsbG8="}, {"kind": "x"}, {"kind": "text", "text": "ok"}]}"#).unwrap();
        let some = read_pinned(&path);
        assert_eq!(some.len(), 1, "only the one valid, once");
        std::fs::write(&path, vec![b' '; MAX_PINNED_FILE_BYTES as usize + 10]).unwrap();
        assert!(read_pinned(&path).is_empty(), "too large");
        // A link is not followed.
        std::fs::remove_file(&path).unwrap();
        let target = dir.join("elsewhere.json");
        std::fs::write(&target, r#"{"items": [{"kind": "text", "text": "linked"}]}"#).unwrap();
        std::os::unix::fs::symlink(&target, &path).unwrap();
        assert!(read_pinned(&path).is_empty());
        assert!(read_pinned(&dir.join("missing.json")).is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn tempfile_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("boswas-clip-test-{}-{:?}", std::process::id(), std::thread::current().id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn base64_round_trips() {
        for bytes in [&b""[..], b"h", b"he", b"hel", b"hello world", &[0, 255, 128, 7]] {
            assert_eq!(base64_decode(&base64_encode(bytes)).unwrap(), bytes);
        }
        assert_eq!(base64_encode(b"hello"), "aGVsbG8=");
        assert!(base64_decode("a$b").is_none());
    }

    #[test]
    fn the_settings_arrive_with_the_desktop_s_and_default_to_on() {
        use crate::settings::Desktop;
        use serde_json::json;
        let d = Desktop::parse(&json!({"scheme": "dark", "clipboard": {"history": false, "cleared_at": 1764180000}})).unwrap();
        assert_eq!((d.clipboard_history, d.clipboard_cleared_at), (false, 1_764_180_000));
        let d = Desktop::parse(&json!({"scheme": "dark"})).unwrap();
        assert_eq!((d.clipboard_history, d.clipboard_cleared_at), (true, 0), "an older compositor: on, never cleared");
        let d = Desktop::parse(&json!({"scheme": "dark", "clipboard": {"history": "no", "cleared_at": -3}})).unwrap();
        assert_eq!((d.clipboard_history, d.clipboard_cleared_at), (true, 0));
    }

    #[test]
    fn searching_looks_in_text_and_file_names() {
        let mut history = History::default();
        history.add(text("Quarterly Report"), 1);
        history.add(Content::Files(vec!["file:///home/asha/report.pdf".into()]), 2);
        history.add(text("lunch"), 3);
        assert_eq!(history.matching("REPORT").len(), 2);
        assert_eq!(history.matching("").len(), 3);
        assert!(history.matching("nothing").is_empty());
    }
}
