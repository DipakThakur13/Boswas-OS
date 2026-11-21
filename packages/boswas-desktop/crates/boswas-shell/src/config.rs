//! What the user arranged in the shell: the applications pinned to the
//! taskbar and to Start (`~/.config/boswas/shell.toml`), and the commands
//! typed into the Run dialog (kept in the state directory).

use std::path::{Path, PathBuf};

/// Applications pinned to the taskbar of a new user, by desktop file id.
pub const DEFAULT_TASKBAR: &[&str] = &[
    "com.boswas.Files",
    "firefox-esr",
    "com.boswas.Terminal",
    "com.boswas.ControlCenter",
    "com.boswas.CompatibilityManager",
];

/// Applications pinned to Start for a new user, in order; Start fills the
/// rest of its first page with other installed applications.
pub const DEFAULT_START: &[&str] = &[
    "com.boswas.Files",
    "firefox-esr",
    "com.boswas.ControlCenter",
    "com.boswas.Terminal",
    "com.boswas.TextEditor",
    "com.boswas.Calculator",
    "com.boswas.Photos",
    "com.boswas.MediaPlayer",
    "com.boswas.Camera",
    "com.boswas.SnippingTool",
    "com.boswas.DocumentViewer",
    "com.boswas.Calendar",
    "com.boswas.Clock",
    "com.boswas.Weather",
    "com.boswas.ArchiveManager",
    "com.boswas.TaskManager",
    "com.boswas.CompatibilityManager",
];

/// Most pins of either kind.
const MAX_PINS: usize = 60;
/// Largest configuration file that is read.
const MAX_BYTES: u64 = 64 * 1024;

/// A desktop file id is safe to store and to print.
fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 200 && id.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShellConfig {
    path: Option<PathBuf>,
    pub taskbar: Vec<String>,
    /// `None`: the user has not arranged Start; the defaults apply.
    pub start: Option<Vec<String>>,
}

impl Default for ShellConfig {
    fn default() -> Self {
        Self {
            path: None,
            taskbar: DEFAULT_TASKBAR.iter().map(|s| (*s).to_owned()).collect(),
            start: None,
        }
    }
}

fn config_dir() -> Option<PathBuf> {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
        .map(|dir| dir.join("boswas"))
}

pub fn state_dir() -> Option<PathBuf> {
    std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/state")))
        .map(|dir| dir.join("boswas"))
}

fn read_capped(path: &Path) -> Option<String> {
    use std::io::Read;
    let mut text = String::new();
    std::fs::File::open(path).ok()?.take(MAX_BYTES).read_to_string(&mut text).ok()?;
    Some(text)
}

fn write_atomically(path: &Path, text: &str) {
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let temporary = path.with_extension("tmp");
    if std::fs::write(&temporary, text).is_ok() {
        let _ = std::fs::rename(&temporary, path);
    }
}

impl ShellConfig {
    pub fn load() -> Self {
        let path = config_dir().map(|dir| dir.join("shell.toml"));
        let mut config = path.as_deref().and_then(read_capped).map(|text| Self::parse(&text)).unwrap_or_default();
        config.path = path;
        config
    }

    /// Reads the file's text. Anything that is not understood keeps its
    /// default, so a damaged file never leaves the taskbar empty-handed.
    pub fn parse(text: &str) -> Self {
        let mut config = Self::default();
        let Ok(table) = text.parse::<toml::Table>() else {
            return config;
        };
        let pins = |section: &str| -> Option<Vec<String>> {
            let list = table.get(section)?.as_table()?.get("pinned")?.as_array()?;
            let mut out: Vec<String> = Vec::new();
            for id in list.iter().filter_map(|v| v.as_str()) {
                if valid_id(id) && !out.iter().any(|o| o == id) && out.len() < MAX_PINS {
                    out.push(id.to_owned());
                }
            }
            Some(out)
        };
        if let Some(taskbar) = pins("taskbar") {
            config.taskbar = taskbar;
        }
        config.start = pins("start");
        config
    }

    pub fn to_toml(&self) -> String {
        let list = |ids: &[String]| ids.iter().map(|id| format!("\"{id}\"")).collect::<Vec<_>>().join(", ");
        let mut out = String::from("# Written by boswas-shell: the applications pinned to the taskbar and to Start.\n");
        out.push_str(&format!("[taskbar]\npinned = [{}]\n", list(&self.taskbar)));
        if let Some(start) = &self.start {
            out.push_str(&format!("\n[start]\npinned = [{}]\n", list(start)));
        }
        out
    }

    fn save(&self) {
        if let Some(path) = &self.path {
            write_atomically(path, &self.to_toml());
        }
    }

    pub fn is_on_taskbar(&self, id: &str) -> bool {
        self.taskbar.iter().any(|p| p == id)
    }

    /// Pins or unpins an application on the taskbar. Returns whether
    /// anything changed.
    pub fn pin_taskbar(&mut self, id: &str, pinned: bool) -> bool {
        if !valid_id(id) || self.is_on_taskbar(id) == pinned || (pinned && self.taskbar.len() >= MAX_PINS) {
            return false;
        }
        if pinned {
            self.taskbar.push(id.to_owned());
        } else {
            self.taskbar.retain(|p| p != id);
        }
        self.save();
        true
    }

    /// Pins or unpins an application in Start; `current` is the list Start
    /// shows now (the defaults, until the user changes them).
    pub fn pin_start(&mut self, id: &str, pinned: bool, current: &[String]) -> bool {
        if !valid_id(id) {
            return false;
        }
        let mut list = self.start.clone().unwrap_or_else(|| current.to_vec());
        let present = list.iter().any(|p| p == id);
        if present == pinned || (pinned && list.len() >= MAX_PINS) {
            return false;
        }
        if pinned {
            list.push(id.to_owned());
        } else {
            list.retain(|p| p != id);
        }
        self.start = Some(list);
        self.save();
        true
    }
}

/// Commands typed into the Run dialog, the most recent last.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RunHistory {
    path: Option<PathBuf>,
    pub entries: Vec<String>,
}

impl RunHistory {
    /// Entries kept.
    pub const MAX: usize = 50;

    pub fn load() -> Self {
        let path = state_dir().map(|dir| dir.join("run-history.json"));
        let entries = path
            .as_deref()
            .and_then(read_capped)
            .and_then(|text| serde_json::from_str::<Vec<String>>(&text).ok())
            .unwrap_or_default()
            .into_iter()
            .filter(|e| !e.is_empty() && e.chars().count() <= 256 && !e.chars().any(char::is_control))
            .collect::<Vec<_>>();
        let mut history = Self { path, entries };
        history.trim();
        history
    }

    fn trim(&mut self) {
        let extra = self.entries.len().saturating_sub(Self::MAX);
        self.entries.drain(..extra);
    }

    /// Records a command that was run; an earlier copy moves to the end.
    pub fn push(&mut self, command: &str) {
        let command = command.trim();
        if command.is_empty() {
            return;
        }
        self.entries.retain(|e| e != command);
        self.entries.push(command.to_owned());
        self.trim();
        if let (Some(path), Ok(text)) = (&self.path, serde_json::to_string(&self.entries)) {
            write_atomically(path, &text);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pins_round_trip_through_the_file_format() {
        let mut config = ShellConfig::default();
        assert_eq!(config.taskbar.len(), DEFAULT_TASKBAR.len());
        assert!(config.start.is_none());
        assert!(config.pin_taskbar("org.kde.okular", true));
        assert!(!config.pin_taskbar("org.kde.okular", true), "already pinned");
        assert!(config.pin_taskbar("firefox-esr", false));
        assert!(!config.pin_taskbar("bad id; rm -rf", true));
        let current: Vec<String> = vec!["a.b".into(), "c".into()];
        assert!(config.pin_start("d-e", true, &current));
        assert!(config.pin_start("a.b", false, &current));
        assert_eq!(config.start.as_deref().unwrap(), ["c", "d-e"]);
        let text = config.to_toml();
        let back = ShellConfig::parse(&text);
        assert_eq!(back.taskbar, config.taskbar);
        assert_eq!(back.start, config.start);
        assert!(back.is_on_taskbar("org.kde.okular") && !back.is_on_taskbar("firefox-esr"));
    }

    #[test]
    fn damaged_files_fall_back_to_the_defaults() {
        for text in ["", "not toml [", "[taskbar]\npinned = 3\n", "taskbar = \"x\"\n"] {
            let config = ShellConfig::parse(text);
            assert_eq!(config.taskbar.len(), DEFAULT_TASKBAR.len(), "{text:?}");
            assert!(config.start.is_none());
        }
        let config = ShellConfig::parse("[taskbar]\npinned = [\"ok.id\", \"ok.id\", \"../etc\", 7, \"second\"]\n[start]\npinned = []\n");
        assert_eq!(config.taskbar, ["ok.id", "second"], "duplicates, unsafe ids and non-strings are dropped");
        assert_eq!(config.start, Some(Vec::new()), "an empty Start is the user's choice");
        let many: Vec<String> = (0..200).map(|i| format!("\"app{i}\"")).collect();
        let config = ShellConfig::parse(&format!("[taskbar]\npinned = [{}]\n", many.join(",")));
        assert_eq!(config.taskbar.len(), MAX_PINS);
    }

    #[test]
    fn run_history_keeps_the_latest_once() {
        let mut history = RunHistory::default();
        for i in 0..60 {
            history.push(&format!("cmd{i}"));
        }
        assert_eq!(history.entries.len(), RunHistory::MAX);
        assert_eq!(history.entries.last().unwrap(), "cmd59");
        history.push("cmd20");
        assert_eq!(history.entries.last().unwrap(), "cmd20");
        assert_eq!(history.entries.iter().filter(|e| *e == "cmd20").count(), 1);
        history.push("   ");
        assert_eq!(history.entries.len(), RunHistory::MAX);
    }
}
