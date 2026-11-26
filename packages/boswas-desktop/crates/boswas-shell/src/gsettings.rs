//! Dark mode and the accent colour for GTK applications and Firefox.
//!
//! They take the desktop's colour scheme (and, where they support it, its
//! accent colour) from the settings portal, which the GTK portal answers
//! from GSettings, `org.gnome.desktop.interface`: `color-scheme`
//! (`prefer-dark` or `default`) and, since gsettings-desktop-schemas 47,
//! `accent-color`. So the shell writes those two keys, at start and
//! whenever Settings changes the mode or the accent (the `config` event),
//! and only when the value there differs: `gsettings get` first, `set`
//! only on a difference, and nothing at all while the wanted values are
//! the ones last written. `gsettings` runs with fixed arguments, without a
//! shell, on a thread of its own. A key the installed schemas lack (an
//! older `accent-color`) is left alone.

/// The schema of the desktop's interface settings.
pub const SCHEMA: &str = "org.gnome.desktop.interface";
pub const COLOR_SCHEME: &str = "color-scheme";
pub const ACCENT_COLOR: &str = "accent-color";

/// GNOME's `color-scheme` for the desktop's mode.
pub fn color_scheme(light: bool) -> &'static str {
    if light {
        "default"
    } else {
        "prefer-dark"
    }
}

/// GNOME's accent name nearest to a Boswas accent (a hue of the palette).
/// GNOME has blue, teal, green, yellow, orange, red, pink, purple and slate;
/// the hues of the palette's 500 shades are closest to these (iris, a
/// blue violet at 252°, to purple at 285° rather than blue at 213°; sky, at
/// 198°, to teal; orchid, a magenta at 305°, to pink, which keeps it apart
/// from iris).
pub fn accent_color(accent: &str) -> &'static str {
    match accent {
        "azure" => "blue",
        "sky" => "teal",
        "mint" => "green",
        "sun" => "yellow",
        "coral" => "red",
        "orchid" => "pink",
        "slate" => "slate",
        // iris, the default.
        _ => "purple",
    }
}

/// Reads and writes GSettings keys (the `gsettings` program; a stand-in in
/// tests).
pub trait Store {
    /// The key's value, `None` when the key does not exist (or cannot be
    /// read).
    fn get(&mut self, key: &str) -> Option<String>;
    fn set(&mut self, key: &str, value: &str) -> bool;
}

/// Writes the wanted values where they differ from what the store holds.
/// Returns the keys written.
pub fn sync(store: &mut dyn Store, wanted: &[(&str, &str)]) -> Vec<String> {
    let mut written = Vec::new();
    for (key, value) in wanted {
        match store.get(key) {
            Some(current) if current == *value => {}
            Some(_) if store.set(key, value) => written.push((*key).to_owned()),
            // Not written, or the installed schemas do not have the key.
            Some(_) | None => {}
        }
    }
    written
}

/// A GVariant string as `gsettings get` prints it (`'prefer-dark'`).
pub fn unquote(printed: &str) -> String {
    let text = printed.trim();
    text.strip_prefix('\'').and_then(|t| t.strip_suffix('\'')).unwrap_or(text).to_owned()
}

/// The `gsettings` program, run with fixed arguments.
pub struct Program;

impl Program {
    fn run(args: &[&str]) -> Option<String> {
        let output = std::process::Command::new("gsettings")
            .args(args)
            .env_remove("WAYLAND_SOCKET")
            .env_remove("BOSWAS_COMPOSITOR_FD")
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .output()
            .ok()?;
        output.status.success().then(|| String::from_utf8_lossy(&output.stdout).into_owned())
    }
}

impl Store for Program {
    fn get(&mut self, key: &str) -> Option<String> {
        Self::run(&["get", SCHEMA, key]).map(|text| unquote(&text))
    }

    fn set(&mut self, key: &str, value: &str) -> bool {
        Self::run(&["set", SCHEMA, key, value]).is_some()
    }
}

/// What the shell last asked for, so a `config` event that changes
/// neither the mode nor the accent starts no program.
#[derive(Default)]
pub struct Mirror {
    pub applied: Option<(&'static str, &'static str)>,
}

impl Mirror {
    /// The values to write for a mode and an accent, when they differ from
    /// the ones asked for last.
    pub fn wanted(&mut self, light: bool, accent: &str) -> Option<(&'static str, &'static str)> {
        let wanted = (color_scheme(light), accent_color(accent));
        if self.applied == Some(wanted) {
            return None;
        }
        self.applied = Some(wanted);
        Some(wanted)
    }
}

impl crate::Shell {
    /// Mirrors the desktop's mode and accent into GSettings (see the module
    /// documentation). The desktop only: the lock and login screens leave
    /// the user's settings alone.
    pub fn mirror_scheme(&mut self) {
        if self.st.mode != crate::state::Mode::Desktop || !self.st.desktop_known {
            return;
        }
        let Some((scheme, accent)) = self.st.gsettings.wanted(self.st.desktop.light, &self.st.desktop.accent) else {
            return;
        };
        if !crate::apps::program_exists("gsettings") {
            tracing::debug!("gsettings is not installed; GTK applications keep their own colour scheme");
            return;
        }
        let spawned = std::thread::Builder::new().name("gsettings".into()).spawn(move || {
            let written = sync(&mut Program, &[(COLOR_SCHEME, scheme), (ACCENT_COLOR, accent)]);
            if !written.is_empty() {
                tracing::info!("GSettings: {} set ({scheme}, {accent})", written.join(" and "));
            }
        });
        if spawned.is_err() {
            tracing::warn!("could not start a thread for gsettings");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    #[derive(Default)]
    struct Fake {
        keys: HashMap<String, String>,
        calls: Vec<String>,
    }

    impl Store for Fake {
        fn get(&mut self, key: &str) -> Option<String> {
            self.calls.push(format!("get {key}"));
            self.keys.get(key).cloned()
        }

        fn set(&mut self, key: &str, value: &str) -> bool {
            self.calls.push(format!("set {key} {value}"));
            self.keys.insert(key.to_owned(), value.to_owned());
            true
        }
    }

    #[test]
    fn the_mode_and_the_accent_map_to_gnome_values() {
        assert_eq!(color_scheme(false), "prefer-dark");
        assert_eq!(color_scheme(true), "default");
        let gnome = ["blue", "teal", "green", "yellow", "orange", "red", "pink", "purple", "slate"];
        let mut seen = Vec::new();
        for accent in crate::settings::ACCENTS {
            let name = accent_color(accent);
            assert!(gnome.contains(&name), "{accent} -> {name}");
            assert!(!seen.contains(&name), "{accent}: two accents look the same to GNOME");
            seen.push(name);
        }
        assert_eq!(accent_color("iris"), "purple");
        assert_eq!(accent_color("azure"), "blue");
        assert_eq!(accent_color("unknown"), "purple", "the default accent's");
    }

    #[test]
    fn only_values_that_differ_are_written() {
        let mut store = Fake::default();
        store.keys.insert(COLOR_SCHEME.into(), "default".into());
        store.keys.insert(ACCENT_COLOR.into(), "purple".into());
        let written = sync(&mut store, &[(COLOR_SCHEME, "prefer-dark"), (ACCENT_COLOR, "purple")]);
        assert_eq!(written, [COLOR_SCHEME]);
        assert_eq!(store.calls, ["get color-scheme", "set color-scheme prefer-dark", "get accent-color"]);
        store.calls.clear();
        assert!(sync(&mut store, &[(COLOR_SCHEME, "prefer-dark"), (ACCENT_COLOR, "purple")]).is_empty());
        assert_eq!(store.calls, ["get color-scheme", "get accent-color"], "equal values are only read");
    }

    #[test]
    fn a_key_the_schemas_lack_is_left_alone() {
        let mut store = Fake::default();
        store.keys.insert(COLOR_SCHEME.into(), "default".into());
        let written = sync(&mut store, &[(COLOR_SCHEME, "prefer-dark"), (ACCENT_COLOR, "blue")]);
        assert_eq!(written, [COLOR_SCHEME], "no accent-color before gsettings-desktop-schemas 47");
        assert!(!store.keys.contains_key(ACCENT_COLOR));
    }

    #[test]
    fn nothing_runs_while_the_mode_and_the_accent_stay() {
        let mut mirror = Mirror::default();
        assert_eq!(mirror.wanted(false, "iris"), Some(("prefer-dark", "purple")));
        assert_eq!(mirror.wanted(false, "iris"), None, "a change of another setting starts no program");
        assert_eq!(mirror.wanted(true, "iris"), Some(("default", "purple")));
        assert_eq!(mirror.wanted(true, "mint"), Some(("default", "green")));
    }

    #[test]
    fn printed_values_are_unquoted() {
        assert_eq!(unquote("'prefer-dark'\n"), "prefer-dark");
        assert_eq!(unquote("default"), "default");
        assert_eq!(unquote("''"), "");
    }
}
