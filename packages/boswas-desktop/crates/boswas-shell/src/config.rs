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

/// An application that sent notifications, as Settings lists it to turn
/// its banners off (`~/.local/state/boswas/notification-senders.json`).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Sender {
    /// The name it sends (`app_name`): what banners are turned off by.
    pub name: String,
    #[serde(default)]
    pub icon: String,
    #[serde(default)]
    pub desktop_entry: String,
    /// When it last sent one, in seconds since the epoch.
    #[serde(default)]
    pub last: u64,
}

/// The applications that sent notifications, most recent first.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Senders {
    path: Option<PathBuf>,
    pub entries: Vec<Sender>,
}

impl Senders {
    /// Applications kept.
    pub const MAX: usize = 100;
    /// A sender already listed is written again after this long, so the
    /// list stays in the order of use without a write per notification.
    const REFRESH: u64 = 3600;

    pub fn load() -> Self {
        let path = state_dir().map(|dir| dir.join("notification-senders.json"));
        let mut senders = Self { path, entries: Vec::new() };
        if let Some(text) = senders.path.as_deref().and_then(read_capped) {
            senders.entries = Self::parse(&text);
        }
        senders
    }

    /// Reads the file's text; what is not understood is left out.
    pub fn parse(text: &str) -> Vec<Sender> {
        let mut out: Vec<Sender> = Vec::new();
        for sender in serde_json::from_str::<Vec<Sender>>(text).unwrap_or_default() {
            if Self::plain(&sender.name, 128) && Self::plain(&sender.icon, 256) && Self::plain(&sender.desktop_entry, 256) && !out.iter().any(|o| o.name == sender.name) && out.len() < Self::MAX {
                out.push(sender);
            }
        }
        out
    }

    fn plain(text: &str, limit: usize) -> bool {
        text.chars().count() <= limit && !text.chars().any(char::is_control)
    }

    /// Notes that `note` arrived. Returns whether the list changed (and was
    /// written).
    pub fn record(&mut self, note: &boswas_services::Notification, now: std::time::SystemTime) -> bool {
        let name = note.app_name.trim();
        if name.is_empty() || !Self::plain(name, 128) {
            return false;
        }
        let seconds = now.duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
        let entry = Sender {
            name: name.to_owned(),
            icon: Some(note.app_icon.as_str()).filter(|i| Self::plain(i, 256)).unwrap_or_default().to_owned(),
            desktop_entry: note.desktop_entry.as_deref().filter(|e| Self::plain(e, 256)).unwrap_or_default().to_owned(),
            last: seconds,
        };
        if let Some(position) = self.entries.iter().position(|s| s.name == entry.name) {
            let old = &self.entries[position];
            if position == 0 && old.icon == entry.icon && old.desktop_entry == entry.desktop_entry && seconds.saturating_sub(old.last) < Self::REFRESH {
                return false;
            }
            self.entries.remove(position);
        }
        self.entries.insert(0, entry);
        self.entries.truncate(Self::MAX);
        if let (Some(path), Ok(text)) = (&self.path, serde_json::to_string(&self.entries)) {
            write_atomically(path, &text);
        }
        true
    }
}

/// When each installed application was first seen, in seconds since the
/// epoch (`~/.local/state/boswas/apps-seen.json`), for Start's "Recently
/// added". The applications of the first run count as old (0).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SeenApps {
    path: Option<PathBuf>,
    /// The file existed: applications not in it are new.
    known: bool,
    pub first_seen: std::collections::BTreeMap<String, u64>,
}

impl SeenApps {
    /// Applications remembered.
    const MAX: usize = 4000;
    /// How long an application counts as recently added.
    pub const RECENT: u64 = 14 * 24 * 3600;

    pub fn load() -> Self {
        let path = state_dir().map(|dir| dir.join("apps-seen.json"));
        let read = path
            .as_deref()
            .and_then(read_capped)
            .and_then(|text| serde_json::from_str::<std::collections::BTreeMap<String, u64>>(&text).ok());
        Self {
            path,
            known: read.is_some(),
            first_seen: read.unwrap_or_default().into_iter().filter(|(id, _)| valid_id(id)).collect(),
        }
    }

    /// Records the applications installed now; forgets those that are gone.
    /// Writes the file when anything changed.
    pub fn update(&mut self, ids: &[&str], now: u64) {
        let before = self.first_seen.clone();
        self.first_seen.retain(|id, _| ids.contains(&id.as_str()));
        for id in ids.iter().filter(|id| valid_id(id)).take(Self::MAX) {
            let stamp = if self.known { now } else { 0 };
            self.first_seen.entry((*id).to_owned()).or_insert(stamp);
        }
        self.known = true;
        if self.first_seen != before {
            if let (Some(path), Ok(text)) = (&self.path, serde_json::to_string(&self.first_seen)) {
                write_atomically(path, &text);
            }
        }
    }

    /// The applications added in the last [`Self::RECENT`] seconds, newest
    /// first.
    pub fn recent(&self, now: u64) -> Vec<&str> {
        let mut found: Vec<(&u64, &String)> = self
            .first_seen
            .iter()
            .filter(|(_, at)| **at > 0 && now.saturating_sub(**at) < Self::RECENT)
            .map(|(id, at)| (at, id))
            .collect();
        found.sort_by(|a, b| b.0.cmp(a.0).then(a.1.cmp(b.1)));
        found.into_iter().map(|(_, id)| id.as_str()).collect()
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
    fn senders_are_listed_once_most_recent_first() {
        let note = |app: &str| boswas_services::Notification {
            id: 1,
            app_name: app.into(),
            app_icon: "mail".into(),
            desktop_entry: Some("org.example.Mail".into()),
            summary: String::new(),
            body: String::new(),
            actions: Vec::new(),
            urgency: boswas_services::Urgency::Normal,
            timeout_ms: None,
            transient: false,
            progress: None,
            time: 0,
            replaces_existing: false,
        };
        let at = |s: u64| std::time::UNIX_EPOCH + std::time::Duration::from_secs(s);
        let mut senders = Senders::default();
        assert!(senders.record(&note("Mail"), at(1000)));
        assert!(senders.record(&note("Chat"), at(1001)));
        assert!(!senders.record(&note("Chat"), at(1002)), "nothing new to write");
        assert!(senders.record(&note("Mail"), at(1003)), "Mail moves to the front");
        assert_eq!(senders.entries.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["Mail", "Chat"]);
        assert!(!senders.record(&note("  "), at(1004)));
        assert!(!senders.record(&note("a\u{7}b"), at(1004)));
        let text = serde_json::to_string(&senders.entries).unwrap();
        assert_eq!(Senders::parse(&text), senders.entries);
        assert!(Senders::parse("not json").is_empty());
        assert_eq!(Senders::parse(r#"[{"name": "A"}, {"name": "A"}, {"name": 3}, {"icon": "x"}]"#).len(), 0, "one bad entry rejects the list");
        assert_eq!(Senders::parse(r#"[{"name": "A"}, {"name": "A"}, {"name": "B", "last": 5}]"#).len(), 2);
        for i in 0..150 {
            senders.record(&note(&format!("App {i}")), at(2000 + i));
        }
        assert_eq!(senders.entries.len(), Senders::MAX);
        assert_eq!(senders.entries[0].name, "App 149");
    }

    #[test]
    fn applications_of_the_first_run_are_not_new() {
        let mut seen = SeenApps::default();
        seen.update(&["a.b", "c"], 1000);
        assert!(seen.recent(1000).is_empty(), "the first run sets the baseline");
        seen.update(&["a.b", "c", "new.app"], 2000);
        seen.update(&["a.b", "c", "new.app", "newer"], 3000);
        assert_eq!(seen.recent(3000), ["newer", "new.app"]);
        assert_eq!(seen.recent(2000 + SeenApps::RECENT), ["newer"], "two weeks later it is no longer new");
        seen.update(&["a.b", "newer"], 4000);
        assert_eq!(seen.recent(4000), ["newer"], "removed applications are forgotten");
        seen.update(&["bad id; x"], 5000);
        assert!(seen.first_seen.is_empty());
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
