//! Application icons for window frames, the window switcher and Task View.
//!
//! A window names its application by `app_id`. The icon is the `Icon` of
//! the desktop entry with that id, looked up in the Boswas Prism icon theme
//! (with its fallbacks), rendered once per size and kept.

use std::{collections::HashMap, path::Path, rc::Rc};

use boswas_ui::icons::{data_dirs, IconLoader};
use tiny_skia::Pixmap;

/// Size limit of a desktop entry read for its icon name.
const MAX_ENTRY_BYTES: u64 = 256 * 1024;

pub struct AppIcons {
    loader: IconLoader,
    light: bool,
    /// Icon names by application id (`None`: no desktop entry names one).
    names: HashMap<String, Option<String>>,
}

impl AppIcons {
    pub fn new(light: bool) -> Self {
        Self {
            loader: IconLoader::new(if light { "Boswas-Prism-Light" } else { "Boswas-Prism" }),
            light,
            names: HashMap::new(),
        }
    }

    /// Whether this loader is for the light scheme.
    pub fn is_light(&self) -> bool {
        self.light
    }

    /// The icon of an application at `size` pixels, if it has one.
    pub fn get(&mut self, app_id: &str, size: u32) -> Option<Rc<Pixmap>> {
        if app_id.is_empty() || app_id.len() > 255 {
            return None;
        }
        let name = self
            .names
            .entry(app_id.to_owned())
            .or_insert_with(|| icon_name(app_id))
            .clone()?;
        self.loader.load(&name, size)
    }
}

/// The `Icon` of the desktop entry of an application id. Ids are matched as
/// they are, in lower case, and by their last dotted component, because
/// applications are not consistent about them. Without an entry the id
/// itself is tried as an icon name.
fn icon_name(app_id: &str) -> Option<String> {
    if !app_id.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_')) {
        return None;
    }
    let lower = app_id.to_ascii_lowercase();
    let last = app_id.rsplit('.').next().unwrap_or(app_id).to_ascii_lowercase();
    let mut candidates = vec![app_id.to_owned()];
    for other in [lower, last] {
        if !candidates.contains(&other) {
            candidates.push(other);
        }
    }
    for dir in data_dirs() {
        for candidate in &candidates {
            let path = dir.join("applications").join(format!("{candidate}.desktop"));
            if let Some(icon) = entry_icon(&path) {
                return Some(icon);
            }
        }
    }
    Some(app_id.to_owned())
}

/// The `Icon` key of the `[Desktop Entry]` group of a desktop entry file.
fn entry_icon(path: &Path) -> Option<String> {
    let meta = std::fs::metadata(path).ok()?;
    if !meta.is_file() || meta.len() > MAX_ENTRY_BYTES {
        return None;
    }
    parse_icon(&std::fs::read_to_string(path).ok()?)
}

fn parse_icon(text: &str) -> Option<String> {
    let mut in_entry = false;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            in_entry = line == "[Desktop Entry]";
            continue;
        }
        if in_entry {
            if let Some((key, value)) = line.split_once('=') {
                if key.trim() == "Icon" {
                    let value = value.trim();
                    return (!value.is_empty()).then(|| value.to_owned());
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_icon_is_read_from_the_desktop_entry_group_only() {
        let entry = "[Desktop Entry]\nName=Files\nIcon=system-file-manager\n\n[Desktop Action new]\nIcon=window-new\n";
        assert_eq!(parse_icon(entry).as_deref(), Some("system-file-manager"));
        assert_eq!(parse_icon("[Desktop Action a]\nIcon=x\n"), None);
        assert_eq!(parse_icon("[Desktop Entry]\nIcon=\n"), None);
        assert_eq!(parse_icon("[Desktop Entry]\n Icon = /usr/share/pixmaps/a.png \n").as_deref(), Some("/usr/share/pixmaps/a.png"));
    }

    #[test]
    fn odd_application_ids_are_refused() {
        assert_eq!(icon_name("../../etc/passwd"), None);
        assert_eq!(icon_name("a b"), None);
        // Without a desktop entry the id itself is the icon name to try.
        assert_eq!(icon_name("boswas-test-no-such-application").as_deref(), Some("boswas-test-no-such-application"));
    }
}
