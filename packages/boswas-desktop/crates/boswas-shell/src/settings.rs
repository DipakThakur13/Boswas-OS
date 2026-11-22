//! The desktop's settings as the compositor passes them on: the look, and
//! the taskbar, Start and notification settings of `compositor.toml`
//! (boswas-compositor(1), CONFIGURATION). They arrive with the `config`
//! event at start and after every `reload-config`. What arrives comes from
//! files of the user and is read leniently: anything missing or of the
//! wrong kind keeps its default.

use serde_json::Value;

/// The palette hues the accent can take (`[appearance] accent`).
pub const ACCENTS: [&str; 8] = ["iris", "azure", "sky", "mint", "sun", "coral", "orchid", "slate"];

#[derive(Clone, Debug, PartialEq)]
pub struct Desktop {
    pub light: bool,
    pub wallpaper: String,
    pub reduced_motion: bool,
    pub accent: String,
    /// Materials show the blurred wallpaper through their tint.
    pub transparency: bool,
    /// Text size in percent, 100 to 225.
    pub text_scale: u32,
    pub taskbar: Taskbar,
    pub start: Start,
    pub notifications: Notifications,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Taskbar {
    /// Start and the applications at the left edge instead of centred.
    pub left: bool,
    pub auto_hide: bool,
    pub task_view: bool,
    pub clock_seconds: bool,
    pub badges: bool,
    /// The windows of every desktop, not only of the current one.
    pub all_desktops: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StartLayout {
    Default,
    MorePins,
    MoreRecommendations,
}

/// The folders and places Start can show next to the power button.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Folder {
    Settings,
    Files,
    Documents,
    Downloads,
    Music,
    Pictures,
    Videos,
}

impl Folder {
    pub const ALL: [Folder; 7] = [Folder::Settings, Folder::Files, Folder::Documents, Folder::Downloads, Folder::Music, Folder::Pictures, Folder::Videos];

    pub fn key(self) -> &'static str {
        match self {
            Folder::Settings => "settings",
            Folder::Files => "files",
            Folder::Documents => "documents",
            Folder::Downloads => "downloads",
            Folder::Music => "music",
            Folder::Pictures => "pictures",
            Folder::Videos => "videos",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Folder::Settings => "Settings",
            Folder::Files => "Files",
            Folder::Documents => "Documents",
            Folder::Downloads => "Downloads",
            Folder::Music => "Music",
            Folder::Pictures => "Pictures",
            Folder::Videos => "Videos",
        }
    }

    pub fn icon(self) -> &'static str {
        match self {
            Folder::Settings => "preferences-system-symbolic",
            Folder::Files => "system-file-manager-symbolic",
            Folder::Documents => "folder-documents-symbolic",
            Folder::Downloads => "folder-download-symbolic",
            Folder::Music => "folder-music-symbolic",
            Folder::Pictures => "folder-pictures-symbolic",
            Folder::Videos => "folder-videos-symbolic",
        }
    }

    /// The XDG user directory behind a folder (`None` for Settings and
    /// Files, which are programs).
    pub fn xdg_name(self) -> Option<&'static str> {
        match self {
            Folder::Documents => Some("DOCUMENTS"),
            Folder::Downloads => Some("DOWNLOAD"),
            Folder::Music => Some("MUSIC"),
            Folder::Pictures => Some("PICTURES"),
            Folder::Videos => Some("VIDEOS"),
            Folder::Settings | Folder::Files => None,
        }
    }

    fn from_key(key: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|f| f.key() == key)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Start {
    pub layout: StartLayout,
    pub recently_added: bool,
    pub most_used: bool,
    pub folders: Vec<Folder>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Notifications {
    pub do_not_disturb: bool,
    pub scheduled: bool,
    pub from: String,
    pub to: String,
    pub banners_off: Vec<String>,
    pub toast_seconds: u32,
}

impl Default for Desktop {
    fn default() -> Self {
        Self {
            light: false,
            wallpaper: String::new(),
            reduced_motion: false,
            accent: ACCENTS[0].into(),
            transparency: true,
            text_scale: 100,
            taskbar: Taskbar::default(),
            start: Start::default(),
            notifications: Notifications::default(),
        }
    }
}

impl Default for Taskbar {
    fn default() -> Self {
        Self {
            left: false,
            auto_hide: false,
            task_view: true,
            clock_seconds: false,
            badges: true,
            all_desktops: false,
        }
    }
}

impl Default for Start {
    fn default() -> Self {
        Self {
            layout: StartLayout::Default,
            recently_added: true,
            most_used: true,
            folders: vec![Folder::Settings, Folder::Files],
        }
    }
}

impl Default for Notifications {
    fn default() -> Self {
        Self {
            do_not_disturb: false,
            scheduled: false,
            from: "22:00".into(),
            to: "07:00".into(),
            banners_off: Vec::new(),
            toast_seconds: 5,
        }
    }
}

/// Plain text of at most `limit` characters, without control characters.
fn text(value: &Value, limit: usize) -> Option<String> {
    let text = value.as_str()?;
    (text.chars().count() <= limit && !text.chars().any(char::is_control)).then(|| text.to_owned())
}

fn flag(value: &Value, default: bool) -> bool {
    value.as_bool().unwrap_or(default)
}

/// Parses `HH:MM` into minutes after midnight.
pub fn parse_clock(text: &str) -> Option<u32> {
    let (h, m) = text.split_once(':')?;
    if h.len() != 2 || m.len() != 2 {
        return None;
    }
    let (h, m): (u32, u32) = (h.parse().ok()?, m.parse().ok()?);
    (h < 24 && m < 60).then_some(h * 60 + m)
}

impl Desktop {
    /// Reads the `config` event (or the `appearance` part of the
    /// `subscribe` reply). `None` when it carries no scheme at all.
    pub fn parse(value: &Value) -> Option<Self> {
        let d = Self::default();
        let light = value.get("scheme")?.as_str()? == "light";
        let taskbar = &value["taskbar"];
        let start = &value["start"];
        let notes = &value["notifications"];
        let accent = text(&value["accent"], 16).filter(|a| ACCENTS.contains(&a.as_str())).unwrap_or(d.accent);
        let layout = match start["layout"].as_str() {
            Some("more_pins") => StartLayout::MorePins,
            Some("more_recommendations") => StartLayout::MoreRecommendations,
            _ => StartLayout::Default,
        };
        let folders = match start["folders"].as_array() {
            Some(list) => {
                let mut out: Vec<Folder> = Vec::new();
                for folder in list.iter().filter_map(Value::as_str).filter_map(Folder::from_key) {
                    if !out.contains(&folder) {
                        out.push(folder);
                    }
                }
                out
            }
            None => d.start.folders.clone(),
        };
        let clock = |value: &Value, default: &str| text(value, 5).filter(|t| parse_clock(t).is_some()).unwrap_or_else(|| default.to_owned());
        let banners_off = notes["banners_off"]
            .as_array()
            .map(|list| list.iter().take(256).filter_map(|v| text(v, 128)).collect())
            .unwrap_or_default();
        Some(Self {
            light,
            wallpaper: text(&value["wallpaper"], 4096).unwrap_or_default(),
            reduced_motion: flag(&value["reduced_motion"], false),
            accent,
            transparency: flag(&value["transparency"], true),
            text_scale: value["text_scale"].as_u64().map_or(100, |s| s.clamp(100, 225) as u32),
            taskbar: Taskbar {
                left: taskbar["alignment"].as_str() == Some("left"),
                auto_hide: flag(&taskbar["auto_hide"], d.taskbar.auto_hide),
                task_view: flag(&taskbar["task_view"], d.taskbar.task_view),
                clock_seconds: flag(&taskbar["clock_seconds"], d.taskbar.clock_seconds),
                badges: flag(&taskbar["badges"], d.taskbar.badges),
                all_desktops: taskbar["windows"].as_str() == Some("all"),
            },
            start: Start {
                layout,
                recently_added: flag(&start["recently_added"], d.start.recently_added),
                most_used: flag(&start["most_used"], d.start.most_used),
                folders,
            },
            notifications: Notifications {
                do_not_disturb: flag(&notes["do_not_disturb"], false),
                scheduled: flag(&notes["scheduled"], false),
                from: clock(&notes["from"], &d.notifications.from),
                to: clock(&notes["to"], &d.notifications.to),
                banners_off,
                toast_seconds: notes["toast_seconds"].as_u64().map_or(5, |s| s.clamp(1, 300) as u32),
            },
        })
    }
}

impl Notifications {
    /// Whether the schedule asks for do not disturb at `minutes` after local
    /// midnight (false without a schedule).
    pub fn scheduled_at(&self, minutes: u32) -> bool {
        if !self.scheduled {
            return false;
        }
        let (Some(from), Some(to)) = (parse_clock(&self.from), parse_clock(&self.to)) else {
            return false;
        };
        if from <= to {
            (from..to).contains(&minutes)
        } else {
            minutes >= from || minutes < to
        }
    }

    /// Whether an application's notifications may pop up.
    pub fn banners_for(&self, app: &str) -> bool {
        !self.banners_off.iter().any(|name| name == app)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn everything_the_compositor_sends_is_read() {
        let value = json!({
            "scheme": "light", "wallpaper": "/usr/share/w.png", "reduced_motion": true, "accent": "coral",
            "transparency": false, "text_scale": 150,
            "taskbar": {"alignment": "left", "auto_hide": true, "task_view": false, "clock_seconds": true, "badges": false, "windows": "all"},
            "start": {"layout": "more_pins", "recently_added": false, "most_used": false, "folders": ["music", "settings", "music", "nope"]},
            "notifications": {"do_not_disturb": true, "scheduled": true, "from": "21:30", "to": "06:00", "banners_off": ["Mail"], "toast_seconds": 30}
        });
        let d = Desktop::parse(&value).unwrap();
        assert!(d.light && d.reduced_motion && !d.transparency);
        assert_eq!((d.wallpaper.as_str(), d.accent.as_str(), d.text_scale), ("/usr/share/w.png", "coral", 150));
        assert_eq!(d.taskbar, Taskbar { left: true, auto_hide: true, task_view: false, clock_seconds: true, badges: false, all_desktops: true });
        assert_eq!(d.start.layout, StartLayout::MorePins);
        assert!(!d.start.recently_added && !d.start.most_used);
        assert_eq!(d.start.folders, [Folder::Music, Folder::Settings]);
        assert!(d.notifications.do_not_disturb && d.notifications.scheduled);
        assert_eq!((d.notifications.from.as_str(), d.notifications.to.as_str(), d.notifications.toast_seconds), ("21:30", "06:00", 30));
        assert!(!d.notifications.banners_for("Mail") && d.notifications.banners_for("Chat"));
    }

    #[test]
    fn an_older_or_damaged_message_keeps_the_defaults() {
        let d = Desktop::parse(&json!({"scheme": "dark", "wallpaper": ""})).unwrap();
        assert_eq!(d, Desktop::default());
        let d = Desktop::parse(&json!({"scheme": "dark", "accent": "neon", "text_scale": 9000, "taskbar": 4,
            "start": {"folders": "all"}, "notifications": {"from": "late", "banners_off": ["ok", 7, "a\u{7}b"]}}))
        .unwrap();
        assert_eq!(d.accent, "iris");
        assert_eq!(d.text_scale, 225);
        assert_eq!(d.taskbar, Taskbar::default());
        assert_eq!(d.start.folders, Start::default().folders);
        assert_eq!(d.notifications.from, "22:00");
        assert_eq!(d.notifications.banners_off, ["ok"]);
        assert!(Desktop::parse(&json!({"wallpaper": "x"})).is_none());
    }

    #[test]
    fn the_schedule_runs_over_midnight() {
        let mut n = Notifications::default();
        assert!(!n.scheduled_at(23 * 60), "no schedule");
        n.scheduled = true;
        assert!(n.scheduled_at(23 * 60) && n.scheduled_at(3 * 60));
        assert!(!n.scheduled_at(7 * 60) && !n.scheduled_at(12 * 60));
        n.from = "09:00".into();
        n.to = "17:00".into();
        assert!(n.scheduled_at(9 * 60) && !n.scheduled_at(17 * 60));
    }
}
