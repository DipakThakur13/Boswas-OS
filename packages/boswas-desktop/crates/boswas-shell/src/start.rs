//! Start, as Windows 11 has it since 2025: the search box over one
//! scrolling page with the pinned applications (the first rows; "Show all"
//! shows every pin), the recommended applications, and every application
//! under "All" (by category, in a grid, or in a list under letter headings
//! whose letters open an index). While something is typed the search
//! results take the page's place. The user and the power button stay at
//! the bottom.
//!
//! The page is laid out once per drawing and per key ([`page`]) in its own
//! coordinates; it is drawn through a window that scrolls smoothly, and
//! the keyboard moves through all of it, also through what is scrolled out
//! of sight.

use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use boswas_ui::{
    text::TextEngine,
    tokens::{cubic_bezier, tokens},
    Rgba,
};
use smithay_client_toolkit::seat::keyboard::Keysym;

use crate::{
    action::Action,
    apps::{self, App},
    config::{AllView, SeenApps, ShellConfig, DEFAULT_START},
    glyph::Glyph,
    settings::{self, Folder, StartLayout},
    state::State,
    ui::{ramp, Frame, Hit, Nav, Rect, TextField},
};

#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    /// The search box (a click puts the caret there; typing always does).
    SearchBox,
    /// A pinned application, by index into the application list.
    App(usize),
    /// The same, from "Recommended" (a control of its own, so the keyboard
    /// focus can tell the sections apart).
    Recommended(usize),
    /// The same, under "All".
    All(usize),
    /// "Show all" or "Show less" of the pinned applications.
    ShowAll,
    /// How "All" lists the applications.
    View(AllView),
    /// A letter heading of the list: opens the index of letters.
    Letter(char),
    /// A letter of the index: goes to its heading.
    Jump(char),
    /// Closes the index of letters (a click beside its letters).
    CloseIndex,
    /// A search result by position.
    Result(usize),
    User,
    Power,
    /// A folder or place next to the power button.
    Folder(Folder),
}

impl Act {
    /// The controls that scroll with the page (everything but the search
    /// box, the search results, the index and the buttons at the bottom).
    pub fn on_page(&self) -> bool {
        matches!(self, Act::App(_) | Act::Recommended(_) | Act::All(_) | Act::ShowAll | Act::View(_) | Act::Letter(_))
    }

    /// The buttons at the bottom of Start.
    pub fn in_footer(&self) -> bool {
        matches!(self, Act::User | Act::Power | Act::Folder(_))
    }
}

/// One search result.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ResultItem {
    App(usize),
    /// Index into [`SETTINGS`].
    Setting(usize),
    /// Run what was typed as a command.
    Run(String),
}

/// The pages of Boswas Control Center that search offers: key, label, icon
/// and extra words to match.
pub const SETTINGS: &[(&str, &str, &str, &[&str])] = &[
    ("personalization", "Personalization", "preferences-desktop-theme-global", &["theme", "wallpaper", "colours", "colors", "dark", "light", "background"]),
    ("network", "Network & internet", "preferences-system-network", &["wifi", "wi-fi", "ethernet", "vpn", "proxy", "internet"]),
    ("bluetooth", "Bluetooth", "preferences-system-bluetooth", &["pair", "wireless", "headphones"]),
    ("display", "Display", "preferences-desktop-display", &["monitor", "screen", "resolution", "scale", "night light", "brightness"]),
    ("sound", "Sound", "preferences-desktop-sound", &["audio", "volume", "speakers", "microphone"]),
    ("power", "Power & battery", "preferences-system-power-management", &["battery", "sleep", "suspend", "energy"]),
    ("storage", "Storage", "boswas-storage", &["disk", "space", "cleanup", "drive"]),
    ("applications", "Apps", "boswas-software-center", &["software", "install", "remove", "programs", "default apps", "startup"]),
    ("windows", "Windows compatibility", "boswas-compat-manager", &["wine", "exe", "msi"]),
    ("android", "Android apps", "boswas-control-center", &["apk", "waydroid"]),
    ("security", "Security", "boswas-security-center", &["firewall", "protection", "encryption", "lock"]),
    ("users", "Accounts", "preferences-system-users", &["user", "password", "sign-in", "login"]),
    ("updates", "Updates", "boswas-update-center", &["upgrade", "patch", "update"]),
    ("devices", "Mouse, keyboard & touchpad", "preferences-desktop-peripherals", &["mouse", "keyboard", "touchpad", "tablet", "pointer", "layout"]),
    ("privacy-security", "Privacy & security", "preferences-system-privacy", &["telemetry", "history", "data", "privacy"]),
    ("hardware", "Hardware & drivers", "boswas-hardware", &["driver", "firmware", "device", "graphics"]),
    ("time-language", "Language, region & time", "preferences-system", &["language", "region", "date", "time", "locale"]),
    ("notifications", "Notifications", "preferences-desktop-notification-bell", &["do not disturb", "dnd", "banners", "quiet hours"]),
    ("multitasking", "Multitasking", "preferences-system-windows", &["snap", "split screen", "alt+tab", "desktops"]),
    ("taskbar", "Taskbar settings", "boswas-shell", &["taskbar", "auto hide", "alignment", "task view"]),
    ("start", "Start settings", "boswas-logo", &["start menu", "pinned", "recommended", "folders"]),
    ("text-size", "Text size", "preferences-desktop-font", &["font size", "bigger text", "larger text"]),
    ("magnifier", "Magnifier", "zoom-in-symbolic", &["zoom", "magnify", "enlarge"]),
    ("color-filters", "Colour filters", "eye-open-symbolic", &["color filters", "grayscale", "greyscale", "invert", "colour blindness"]),
    ("accessibility-keyboard", "Sticky keys and filter keys", "input-keyboard", &["sticky keys", "filter keys", "accessibility"]),
    ("about", "About this device", "boswas-logo", &["version", "specification", "system information"]),
];

/// The command that opens a settings page.
pub fn settings_command(key: &str) -> Vec<String> {
    vec!["boswas-control-center".into(), "--page".into(), key.to_owned()]
}

const FOOTER_H: f32 = 64.0;
/// Results of each kind shown.
const MAX_APP_RESULTS: usize = 7;
const MAX_SETTING_RESULTS: usize = 4;
/// Pins a new user's Start is filled up to with other installed
/// applications when defaults are missing: two rows of eight.
const DEFAULT_FILL: usize = 16;
/// Room kept between a control the keyboard moved to and the edges of the
/// page's window.
const REVEAL_MARGIN: f32 = 12.0;
/// Width of the narrowest column of tiles.
const MIN_CELL_W: f32 = 84.0;

/// The applications pinned to Start, as ids: the user's list, or for a new
/// user the defaults followed by other installed applications.
pub fn pinned_ids(config: &ShellConfig, apps: &[App]) -> Vec<String> {
    if let Some(list) = &config.start {
        return list.iter().filter(|id| apps.iter().any(|a| &a.id == *id)).cloned().collect();
    }
    let mut ids: Vec<String> = DEFAULT_START.iter().filter(|id| apps.iter().any(|a| a.id == **id)).map(|id| (*id).to_owned()).collect();
    for app in apps {
        if ids.len() >= DEFAULT_FILL {
            break;
        }
        if !ids.contains(&app.id) {
            ids.push(app.id.clone());
        }
    }
    ids
}

/// The applications started most often, most used first.
pub fn recommended(apps: &[App], usage: &HashMap<String, u32>, count: usize) -> Vec<usize> {
    let mut used: Vec<(u32, usize)> = apps.iter().enumerate().filter_map(|(i, a)| usage.get(&a.id).filter(|n| **n > 0).map(|n| (*n, i))).collect();
    used.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    used.into_iter().take(count).map(|(_, i)| i).collect()
}

/// Why an application is recommended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Why {
    RecentlyAdded,
    FrequentlyUsed,
}

impl Why {
    pub fn label(self) -> &'static str {
        match self {
            Why::RecentlyAdded => "Recently added",
            Why::FrequentlyUsed => "Frequently used",
        }
    }
}

/// What "Recommended" shows, as far as Personalization > Start allows:
/// applications added lately (newest first), then the most used, each once.
pub fn recommendations(apps: &[App], usage: &HashMap<String, u32>, seen: &SeenApps, start: &settings::Start, now: u64, count: usize) -> Vec<(usize, Why)> {
    let mut out: Vec<(usize, Why)> = Vec::new();
    if start.recently_added {
        for id in seen.recent(now) {
            if let Some(index) = apps.iter().position(|a| a.id == id) {
                out.push((index, Why::RecentlyAdded));
            }
        }
        out.truncate(count.div_ceil(2).max(1).min(count));
    }
    if start.most_used {
        for index in recommended(apps, usage, count) {
            if out.len() < count && !out.iter().any(|(i, _)| *i == index) {
                out.push((index, Why::FrequentlyUsed));
            }
        }
    }
    out.truncate(count);
    out
}

/// A row of the list of all applications.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ListRow {
    Letter(char),
    App(usize),
}

/// The heading an application is listed under: its first letter, or "#"
/// for names that do not start with a letter.
pub fn letter_of(app: &App) -> char {
    app.name.chars().next().filter(|c| c.is_alphabetic()).and_then(|c| c.to_uppercase().next()).unwrap_or('#')
}

/// All applications under letter headings. `apps` is sorted by name.
pub fn all_rows(apps: &[App]) -> Vec<ListRow> {
    let mut rows = Vec::new();
    let mut current = None;
    for (index, app) in apps.iter().enumerate() {
        let letter = letter_of(app);
        if current != Some(letter) {
            rows.push(ListRow::Letter(letter));
            current = Some(letter);
        }
        rows.push(ListRow::App(index));
    }
    rows
}

/// The letters of the index: A to Z, then "#", and the letters of other
/// scripts that names start with.
pub fn index_letters(apps: &[App]) -> Vec<char> {
    let mut letters: Vec<char> = ('A'..='Z').collect();
    for app in apps {
        let letter = letter_of(app);
        if letter != '#' && !letters.contains(&letter) {
            letters.push(letter);
        }
    }
    letters.push('#');
    letters
}

/// The groups of the category view, in the order they are shown.
pub const CATEGORIES: [&str; 7] = ["Productivity", "Creativity", "Utilities & tools", "Entertainment", "Internet", "System", "Other"];

/// Which group of [`CATEGORIES`] an application belongs to, from the
/// `Categories` of its desktop entry. The first rule that matches wins:
/// what an application makes (pictures, recordings) before what it plays,
/// before what it is for, before the general "Utility" and "System".
pub fn category_of(app: &App) -> usize {
    const RULES: [(usize, &[&str]); 6] = [
        (1, &["Graphics", "2DGraphics", "3DGraphics", "VectorGraphics", "RasterGraphics", "Photography", "Publishing", "Art", "AudioVideoEditing", "Recorder", "Midi", "Mixer", "Sequencer", "Scanning", "ImageProcessing"]),
        (3, &["Game", "AudioVideo", "Audio", "Video", "Player", "Music", "TV", "Amusement", "Emulator"]),
        (4, &["Network", "WebBrowser", "Email", "Chat", "InstantMessaging", "IRCClient", "Feed", "FileTransfer", "News", "P2P", "RemoteAccess", "Telephony", "VideoConference"]),
        (0, &["Office", "Calendar", "ContactManagement", "Database", "Dictionary", "Chart", "Finance", "FlowChart", "PDA", "ProjectManagement", "Presentation", "Spreadsheet", "WordProcessor", "TextEditor", "Development", "IDE", "Education", "Science", "Math"]),
        (2, &["Utility", "Accessibility", "Archiving", "Compression", "Calculator", "Clock", "FileManager", "FileTools", "Filesystem", "TextTools", "Maps"]),
        (5, &["System", "Settings", "Monitor", "PackageManager", "Security", "TerminalEmulator", "HardwareSettings", "DesktopSettings", "Core"]),
    ];
    RULES
        .iter()
        .find(|(_, names)| app.categories.iter().any(|c| names.contains(&c.as_str())))
        .map_or(CATEGORIES.len() - 1, |(group, _)| *group)
}

/// The non-empty groups of the category view with their applications (in
/// the order of `apps`, which is by name).
pub fn category_groups(apps: &[App]) -> Vec<(usize, Vec<usize>)> {
    let mut groups: Vec<(usize, Vec<usize>)> = (0..CATEGORIES.len()).map(|g| (g, Vec::new())).collect();
    for (index, app) in apps.iter().enumerate() {
        groups[category_of(app)].1.push(index);
    }
    groups.retain(|(_, members)| !members.is_empty());
    groups
}

fn setting_matches(index: usize, query: &str) -> Option<u8> {
    let (_, label, _, keywords) = SETTINGS[index];
    let label = label.to_lowercase();
    if label.starts_with(query) {
        Some(0)
    } else if label.split_whitespace().any(|w| w.starts_with(query)) || keywords.iter().any(|k| k.starts_with(query)) {
        Some(1)
    } else if query.len() >= 3 && (label.contains(query) || keywords.iter().any(|k| k.contains(query))) {
        Some(2)
    } else {
        None
    }
}

/// What a search for `query` offers: applications, then settings pages,
/// then running the text as a command. Everything is local.
pub fn search(apps: &[App], usage: &HashMap<String, u32>, query: &str) -> Vec<ResultItem> {
    let trimmed = query.trim();
    if trimmed.is_empty() {
        return Vec::new();
    }
    let lower = trimmed.to_lowercase();
    let found: Vec<usize> = apps::search(apps, trimmed, usage).into_iter().take(MAX_APP_RESULTS).collect();
    let mut settings: Vec<(u8, usize)> = (0..SETTINGS.len()).filter_map(|i| setting_matches(i, &lower).map(|rank| (rank, i))).collect();
    settings.sort();
    settings.truncate(MAX_SETTING_RESULTS);
    let mut results: Vec<ResultItem> = Vec::new();
    // The best match is a settings page when it matches by name and no
    // application does (an application that merely mentions the word in
    // its description must not come first).
    let app_rank = found.first().and_then(|i| apps::match_rank(&apps[*i], &lower)).unwrap_or(u8::MAX);
    if settings.first().is_some_and(|(rank, _)| *rank == 0 && app_rank >= 2) {
        results.push(ResultItem::Setting(settings.remove(0).1));
    }
    results.extend(found.into_iter().map(ResultItem::App));
    results.extend(settings.into_iter().map(|(_, i)| ResultItem::Setting(i)));
    results.push(ResultItem::Run(trimmed.to_owned()));
    results
}

/// What a key press in Start led to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum KeyOutcome {
    /// Not a key Start handles itself.
    Ignored,
    Redraw,
    Activate(ResultItem),
    Close,
}

#[derive(Default)]
pub struct Start {
    pub query: TextField,
    pub results: Vec<ResultItem>,
    /// The highlighted search result.
    pub selected: usize,
    /// Every pin is shown ("Show all"), not only the first rows.
    pub expanded: bool,
    /// The index of letters is open over the list.
    pub index: bool,
    /// Where the page is scrolled to, in logical pixels from its top: the
    /// end of a scroll that is still moving.
    pub scroll: f32,
    /// Where a moving scroll started, and when.
    scroll_from: f32,
    scroll_since: Option<Instant>,
}

/// How long a scroll by the wheel or the keyboard moves.
fn scroll_duration() -> Duration {
    Duration::from_millis(u64::from(tokens().motion_normal_ms))
}

impl Start {
    /// Returns Start to its opening state.
    pub fn reset(&mut self) {
        self.query.clear();
        self.results.clear();
        self.selected = 0;
        self.expanded = false;
        self.index = false;
        self.scroll = 0.0;
        self.scroll_from = 0.0;
        self.scroll_since = None;
    }

    pub fn searching(&self) -> bool {
        !self.query.text().trim().is_empty()
    }

    fn refresh(&mut self, apps: &[App], usage: &HashMap<String, u32>) {
        self.results = search(apps, usage, self.query.text());
        self.selected = 0;
    }

    /// Typed text goes to the search box wherever the focus is.
    pub fn typed(&mut self, text: &str, apps: &[App], usage: &HashMap<String, u32>) -> KeyOutcome {
        if self.query.insert(text) {
            self.index = false;
            self.refresh(apps, usage);
            KeyOutcome::Redraw
        } else {
            KeyOutcome::Ignored
        }
    }

    /// Handles Esc and the keys that belong to the search box and its
    /// results.
    pub fn key(&mut self, keysym: Keysym, ctrl: bool, apps: &[App], usage: &HashMap<String, u32>) -> KeyOutcome {
        match keysym {
            Keysym::Escape => {
                if self.index {
                    self.index = false;
                } else if self.query.is_empty() {
                    return KeyOutcome::Close;
                } else {
                    self.query.clear();
                    self.results.clear();
                }
                KeyOutcome::Redraw
            }
            _ if !self.searching() && !matches!(keysym, Keysym::BackSpace) => KeyOutcome::Ignored,
            Keysym::Return | Keysym::KP_Enter => match self.results.get(self.selected) {
                Some(result) => KeyOutcome::Activate(result.clone()),
                None => KeyOutcome::Ignored,
            },
            Keysym::Down | Keysym::Tab => {
                if !self.results.is_empty() {
                    self.selected = (self.selected + 1) % self.results.len();
                }
                KeyOutcome::Redraw
            }
            Keysym::Up | Keysym::ISO_Left_Tab => {
                if !self.results.is_empty() {
                    self.selected = (self.selected + self.results.len() - 1) % self.results.len();
                }
                KeyOutcome::Redraw
            }
            _ => {
                let before = self.query.text().to_owned();
                if !self.query.key(keysym, ctrl) {
                    return KeyOutcome::Ignored;
                }
                if before != self.query.text() {
                    self.refresh(apps, usage);
                }
                KeyOutcome::Redraw
            }
        }
    }

    /// Where the page is at `now`: on its way to [`Start::scroll`] while a
    /// scroll moves (it slows down towards its end).
    pub fn offset(&self, now: Instant) -> f32 {
        match self.scroll_since {
            Some(since) => {
                let t = now.saturating_duration_since(since).as_secs_f32() / scroll_duration().as_secs_f32();
                if t >= 1.0 {
                    self.scroll
                } else {
                    self.scroll_from + (self.scroll - self.scroll_from) * cubic_bezier(tokens().curve("decelerate"), t.max(0.0))
                }
            }
            None => self.scroll,
        }
    }

    /// Whether a scroll is moving: the page is drawn frame by frame.
    pub fn animating(&self, now: Instant) -> bool {
        self.scroll_since.is_some_and(|since| now.saturating_duration_since(since) < scroll_duration())
    }

    /// Scrolls to `target` (kept within 0 to `max`), smoothly unless
    /// `jump`. Returns whether the page moves.
    pub fn scroll_to(&mut self, target: f32, max: f32, now: Instant, jump: bool) -> bool {
        let target = target.clamp(0.0, max.max(0.0));
        if (target - self.scroll).abs() < 0.01 {
            return false;
        }
        if jump {
            self.scroll_since = None;
        } else {
            self.scroll_from = self.offset(now);
            self.scroll_since = Some(now);
        }
        self.scroll = target;
        true
    }

    /// Scrolls by `delta` logical pixels (see [`Start::scroll_to`]).
    pub fn scroll_by(&mut self, delta: f32, max: f32, now: Instant, jump: bool) -> bool {
        self.scroll_to(self.scroll + delta, max, now, jump)
    }

    /// Scrolls as little as needed to show `rect` (page coordinates) in a
    /// window `height` high.
    pub fn reveal(&mut self, rect: Rect, height: f32, max: f32, now: Instant, jump: bool) -> bool {
        let top = rect.y - REVEAL_MARGIN;
        let bottom = rect.bottom() + REVEAL_MARGIN;
        if top < self.scroll {
            self.scroll_to(top, max, now, jump)
        } else if bottom > self.scroll + height {
            self.scroll_to(bottom - height, max, now, jump)
        } else {
            false
        }
    }

    /// Keeps the scroll within a page that changed.
    pub fn clamp(&mut self, max: f32) {
        if self.scroll > max.max(0.0) {
            self.scroll = max.max(0.0);
            self.scroll_since = None;
        }
    }
}

/// The number of tile columns of Start on an output of the given size
/// (logical pixels): eight where the output is at least
/// `start-wide-output` large, else six.
pub fn columns_for(output: (f32, f32)) -> usize {
    let t = tokens();
    let (w, h) = t.shell_size("start-wide-output");
    let key = if output.0 >= w && output.1 >= h { "start-columns" } else { "start-columns-narrow" };
    t.shell(key) as usize
}

/// The size of Start's panel on an output of the given size: as wide as
/// its columns of tiles, as high as `start-height-fraction` of the output
/// (at most `start-max-height`), and never over the taskbar.
pub fn panel_size(output: (f32, f32)) -> (f32, f32) {
    let t = tokens();
    let margin = t.shell("flyout-margin");
    let cell = t.shell_size("start-cell").0;
    let wanted_w = columns_for(output) as f32 * cell + 2.0 * t.shell("start-padding");
    let w = wanted_w.min(output.0 - 2.0 * margin).max(240.0);
    let room = output.1 - t.shell("taskbar-height") - 2.0 * margin;
    let wanted_h = (output.1 * t.shell("start-height-fraction")).min(t.shell("start-max-height"));
    let h = wanted_h.min(room).max(240.0).floor();
    (w.floor(), h)
}

/// Sizes of the page at the current text size: rows grow with the text so
/// that names stay whole up to 225 %.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Metrics {
    /// Icons of the tiles (pins, the grid and the categories).
    pub icon: f32,
    /// Icons of the list.
    pub list_icon: f32,
    pub cell_h: f32,
    /// The line of a tile's name.
    pub label_h: f32,
    /// A section's title line.
    pub header_h: f32,
    /// A row of the list.
    pub row_h: f32,
    /// A row of "Recommended".
    pub rec_h: f32,
}

pub fn metrics() -> Metrics {
    let t = tokens();
    let icon = t.shell("start-icon");
    let label_h = ramp("caption", Rgba::WHITE).line_height().max(16.0);
    let body = ramp("body", Rgba::WHITE).line_height();
    let strong = ramp("body-strong", Rgba::WHITE).line_height();
    Metrics {
        icon,
        list_icon: t.shell("start-list-icon"),
        cell_h: t.shell_size("start-cell").1.max(12.0 + icon + 8.0 + label_h + 10.0),
        label_h,
        header_h: (strong + 8.0).max(28.0),
        row_h: (body + 16.0).max(40.0),
        rec_h: (body + label_h + 16.0).max(52.0),
    }
}

/// The fixed parts of Start inside its panel.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Geometry {
    pub search: Rect,
    /// The window the page scrolls in (and the search results' place).
    pub view: Rect,
    /// Left edge and width of the content column.
    pub x: f32,
    pub w: f32,
    pub footer: Rect,
    pub columns: usize,
    pub cell_w: f32,
    /// Rows of pins before "Show all", and rows of "Recommended" (two
    /// applications each).
    pub pin_rows: usize,
    pub recommended_rows: usize,
    pub m: Metrics,
}

/// Rows of pins and of recommendations of each layout (Personalization >
/// Start > Layout): `start-rows` and `start-recommended-rows`, or one row
/// more of one and one less of the other.
pub fn layout_rows(layout: StartLayout) -> (usize, usize) {
    let t = tokens();
    let (pins, recommended) = (t.shell("start-rows") as usize, t.shell("start-recommended-rows") as usize);
    match layout {
        StartLayout::Default => (pins, recommended),
        StartLayout::MorePins => (pins + 1, recommended.saturating_sub(1).max(1)),
        StartLayout::MoreRecommendations => (pins.saturating_sub(1).max(1), recommended + 1),
    }
}

pub fn geometry(panel: Rect, layout: StartLayout) -> Geometry {
    let m = metrics();
    let pad = if panel.w >= 560.0 { tokens().shell("start-padding") } else { 16.0 };
    let search_h = (ramp("body", Rgba::WHITE).line_height() + 14.0).max(36.0);
    let search = Rect::new(panel.x + pad, panel.y + 24.0, panel.w - 2.0 * pad, search_h);
    let footer = Rect::new(panel.x, panel.bottom() - FOOTER_H, panel.w, FOOTER_H);
    let top = search.bottom() + 12.0;
    let view = Rect::new(panel.x + 1.0, top, panel.w - 2.0, (footer.y - top).max(1.0));
    let w = panel.w - 2.0 * pad;
    let columns = ((w / MIN_CELL_W).floor() as usize).clamp(1, tokens().shell("start-columns") as usize);
    let (pin_rows, recommended_rows) = layout_rows(layout);
    Geometry {
        search,
        view,
        x: panel.x + pad,
        w,
        footer,
        columns,
        cell_w: w / columns as f32,
        pin_rows,
        recommended_rows,
        m,
    }
}

/// The window the page scrolls in, for Start in `panel`: all that changes
/// while the page scrolls.
pub fn page_window(panel: Rect, st: &State) -> Rect {
    geometry(panel, st.desktop.start.layout).view
}

/// What a part of the page shows.
#[derive(Clone, Debug, PartialEq)]
pub enum Piece {
    /// A section's title.
    Title(&'static str),
    /// A group's name in the category view.
    Group(&'static str),
    /// Words in place of what is missing.
    Hint(&'static str),
    /// A button at a section's right edge: "Show all", "Show less".
    Button(&'static str, Glyph),
    /// One of the views of "All"; `true` for the one shown.
    Choice(AllView, bool),
    /// An application as its icon with its name under it.
    Tile(usize),
    /// An application in a row: icon and name, and for a recommendation
    /// why it is recommended.
    Row(usize, Option<Why>),
    /// A letter heading of the list.
    Letter(char),
}

/// One part of the page, in page coordinates: x as on the surface, y from
/// the top of the page.
#[derive(Clone, Debug, PartialEq)]
pub struct Element {
    pub rect: Rect,
    pub piece: Piece,
    pub act: Option<Act>,
}

/// The scrolling page.
#[derive(Clone, Debug, PartialEq)]
pub struct Page {
    pub elements: Vec<Element>,
    pub height: f32,
}

impl Page {
    /// How far the page scrolls in a window `height` high.
    pub fn max_scroll(&self, height: f32) -> f32 {
        (self.height - height).max(0.0)
    }

    /// The part a control is drawn at.
    pub fn find(&self, act: &Act) -> Option<&Element> {
        self.elements.iter().find(|e| e.act.as_ref() == Some(act))
    }

    /// The page's controls as hits, for keyboard navigation (page
    /// coordinates).
    pub fn hits(&self) -> Vec<Hit> {
        self.elements
            .iter()
            .filter_map(|e| {
                e.act.clone().map(|act| Hit {
                    rect: e.rect,
                    action: Action::Start(act),
                    focusable: true,
                    tooltip: None,
                    drag: false,
                })
            })
            .collect()
    }
}

/// Whether "Recommended" is shown: one of its two settings is on.
pub fn shows_recommended(st: &State) -> bool {
    st.desktop.start.recently_added || st.desktop.start.most_used
}

/// Lays the page out.
pub fn page(st: &State, geo: &Geometry, text: &mut TextEngine) -> Page {
    let m = geo.m;
    let mut out: Vec<Element> = Vec::new();
    let mut push = |rect: Rect, piece: Piece, act: Option<Act>| out.push(Element { rect, piece, act });
    let (x, w) = (geo.x, geo.w);
    let title_w = |text: &mut TextEngine, label: &str| text.measure(label, &ramp("body-strong", Rgba::WHITE)).0;
    let button_w = |text: &mut TextEngine, label: &str| text.measure(label, &ramp("caption", Rgba::WHITE)).0 + 36.0;
    let tiles = |push: &mut dyn FnMut(Rect, Piece, Option<Act>), top: f32, apps: &[usize], act: fn(usize) -> Act| -> f32 {
        for (slot, index) in apps.iter().enumerate() {
            let rect = Rect::new(x + (slot % geo.columns) as f32 * geo.cell_w, top + (slot / geo.columns) as f32 * m.cell_h, geo.cell_w, m.cell_h);
            push(rect, Piece::Tile(*index), Some(act(*index)));
        }
        apps.len().div_ceil(geo.columns) as f32 * m.cell_h
    };
    let mut y = 4.0;

    // Pinned.
    let pins: Vec<usize> = st.start_pins().iter().filter_map(|id| st.app_index(id)).collect();
    let capacity = geo.pin_rows * geo.columns;
    let header = y;
    push(Rect::new(x + 8.0, y, w - 16.0, m.header_h), Piece::Title("Pinned"), None);
    y += m.header_h + 8.0;
    if pins.is_empty() {
        push(Rect::new(x + 8.0, y, w - 16.0, m.row_h), Piece::Hint("Nothing is pinned. Right-click an application under All to pin it."), None);
        y += m.row_h;
    } else {
        let shown = if st.start.expanded { pins.len() } else { pins.len().min(capacity) };
        y += tiles(&mut push, y, &pins[..shown], Act::App);
    }
    // After the pins in keyboard order: Down and Tab from the search box
    // go to the first pin.
    if pins.len() > capacity {
        let (label, glyph) = if st.start.expanded { ("Show less", Glyph::ChevronUp) } else { ("Show all", Glyph::ChevronDown) };
        let bw = button_w(text, label).min(w / 2.0);
        push(Rect::new(x + w - bw - 4.0, header, bw, m.header_h), Piece::Button(label, glyph), Some(Act::ShowAll));
    }
    y += 16.0;

    // Recommended.
    if shows_recommended(st) && geo.recommended_rows > 0 {
        push(Rect::new(x + 8.0, y, w - 16.0, m.header_h), Piece::Title("Recommended"), None);
        y += m.header_h + 8.0;
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
        let list = recommendations(&st.apps, &st.usage.counts, &st.seen, &st.desktop.start, now, 2 * geo.recommended_rows);
        if list.is_empty() {
            let hint = "New applications and the applications you use most will appear here.";
            let lines = text.wrap(hint, &ramp("body", Rgba::WHITE), w - 16.0, 3).len().max(1);
            let h = lines as f32 * ramp("body", Rgba::WHITE).line_height();
            push(Rect::new(x + 8.0, y, w - 16.0, h), Piece::Hint(hint), None);
            y += h;
        } else {
            let column = w / 2.0;
            for (slot, (index, why)) in list.iter().enumerate() {
                let rect = Rect::new(x + (slot % 2) as f32 * column, y + (slot / 2) as f32 * m.rec_h, column - 8.0, m.rec_h - 4.0);
                push(rect, Piece::Row(*index, Some(*why)), Some(Act::Recommended(*index)));
            }
            y += list.len().div_ceil(2) as f32 * m.rec_h;
        }
        y += 16.0;
    }

    // All: the title, the three views at the right.
    push(Rect::new(x + 8.0, y, w - 16.0, m.header_h), Piece::Title("All"), None);
    let room = (w - title_w(text, "All") - 24.0).max(0.0);
    // Measured as the chosen view is drawn (strong text).
    let widths: Vec<f32> = AllView::ALL.iter().map(|v| text.measure(v.label(), &ramp("body-strong", Rgba::WHITE)).0 + 20.0).collect();
    let total: f32 = widths.iter().sum();
    let shrink = if total > room { room / total } else { 1.0 };
    let mut right = x + w - 4.0;
    for (view, width) in AllView::ALL.iter().zip(&widths).rev() {
        let width = width * shrink;
        right -= width;
        push(Rect::new(right, y, width, m.header_h), Piece::Choice(*view, *view == st.config.view), Some(Act::View(*view)));
    }
    y += m.header_h + 8.0;
    let everything: Vec<usize> = (0..st.apps.len()).collect();
    if st.apps.is_empty() {
        push(Rect::new(x + 8.0, y, w - 16.0, m.row_h), Piece::Hint("No applications are installed."), None);
        y += m.row_h;
    } else {
        match st.config.view {
            AllView::Grid => y += tiles(&mut push, y, &everything, Act::All),
            AllView::Category => {
                for (group, members) in category_groups(&st.apps) {
                    push(Rect::new(x + 8.0, y, w - 16.0, m.header_h), Piece::Group(CATEGORIES[group]), None);
                    y += m.header_h;
                    y += tiles(&mut push, y, &members, Act::All) + 8.0;
                }
            }
            AllView::List => {
                for row in all_rows(&st.apps) {
                    match row {
                        ListRow::Letter(letter) => {
                            push(Rect::new(x, y + 2.0, m.row_h.max(48.0), m.row_h - 4.0), Piece::Letter(letter), Some(Act::Letter(letter)));
                        }
                        ListRow::App(index) => {
                            push(Rect::new(x, y + 2.0, w, m.row_h - 4.0), Piece::Row(index, None), Some(Act::All(index)));
                        }
                    }
                    y += m.row_h;
                }
            }
        }
    }
    y += 16.0;
    Page { elements: out, height: y }
}

/// The grid of the index of letters, centred in the page's window.
pub fn index_cells(geo: &Geometry, letters: &[char]) -> Vec<(char, Rect)> {
    let size = (geo.m.row_h + 8.0).max(48.0);
    let columns = ((geo.w / size).floor() as usize).clamp(1, 7);
    let rows = letters.len().div_ceil(columns);
    let left = geo.x + (geo.w - columns as f32 * size) / 2.0;
    let top = geo.view.y + ((geo.view.h - rows as f32 * size) / 2.0).max(8.0);
    letters
        .iter()
        .enumerate()
        .map(|(i, c)| (*c, Rect::new(left + (i % columns) as f32 * size, top + (i / columns) as f32 * size, size - 4.0, size - 4.0)))
        .collect()
}

pub fn draw(f: &mut Frame, st: &State, panel: Rect) {
    let theme = f.theme();
    let t = tokens();
    let radius = t.radius_control;
    let geo = geometry(panel, st.desktop.start.layout);
    let chrome = f.panel(panel, "acrylic");
    f.area(panel, Action::None);

    if st.start.searching() {
        draw_results(f, st, &geo);
    } else {
        let page = page(st, &geo, &mut f.res.text);
        let offset = st.start.offset(Instant::now()).clamp(0.0, page.max_scroll(geo.view.h));
        draw_page(f, st, &geo, &page, offset, chrome.as_deref());
    }

    // Search box: always has the caret, whatever else is focused.
    let touch = f.area(geo.search, Action::Start(Act::SearchBox));
    let fill = if touch.hovered { f.res.fill("control-hover") } else { f.res.fill("control") };
    let stroke = f.res.stroke("control");
    let round = geo.search.h / 2.0;
    f.canvas.fill_rounded_rect(geo.search.x, geo.search.y, geo.search.w, geo.search.h, round, fill);
    f.canvas.stroke_rounded_rect(geo.search.x, geo.search.y, geo.search.w, geo.search.h, round, 1.0, stroke);
    f.icon("system-search-symbolic", geo.search.x + 14.0, geo.search.y + (geo.search.h - 16.0) / 2.0, 16.0);
    let style = ramp("body", theme.text);
    let text_x = geo.search.x + 40.0;
    let text_y = geo.search.y + (geo.search.h - style.line_height()) / 2.0;
    let caret_y = geo.search.y + (geo.search.h - 18.0) / 2.0;
    if st.start.query.is_empty() {
        f.text("Search for apps, settings, and commands", text_x, text_y, Some(geo.search.w - 56.0), &ramp("body", theme.text_muted));
        f.canvas.fill_rect(text_x, caret_y, 1.0, 18.0, theme.text);
    } else {
        let before: String = st.start.query.text().chars().take(st.start.query.cursor()).collect();
        let caret = f.res.text.measure(&before, &style).0;
        f.text(st.start.query.text(), text_x, text_y, Some(geo.search.w - 56.0), &style);
        f.canvas.fill_rect((text_x + caret).min(geo.search.right() - 16.0), caret_y, 1.0, 18.0, theme.text);
    }
    if !st.start.searching() && st.start.index && st.config.view == AllView::List {
        draw_index(f, st, &geo);
    }

    // Footer: the user at the left, power at the right.
    let footer_tint = if theme.scheme == boswas_ui::Scheme::Dark { boswas_ui::Rgba::BLACK.alpha(0.18) } else { boswas_ui::Rgba::BLACK.alpha(0.04) };
    f.canvas.fill_rounded_rect4(geo.footer.x + 1.0, geo.footer.y, geo.footer.w - 2.0, geo.footer.h - 1.0, [0.0, 0.0, t.radius_flyout, t.radius_flyout], footer_tint);
    f.divider(geo.footer.x + 1.0, geo.footer.y, geo.footer.w - 2.0);
    let name_style = ramp("body", theme.text);
    let name_w = f.res.text.measure(&st.user_name, &name_style).0.min(geo.w - 120.0).max(0.0);
    let user = Rect::new(geo.x - 8.0, geo.footer.y + 12.0, 32.0 + 12.0 + name_w + 20.0, 40.0);
    let touch = f.hit(user, Action::Start(Act::User));
    f.state_layer(user, radius, touch, false);
    f.canvas.fill_circle(user.x + 24.0, user.y + 20.0, 16.0, theme.surface_hover);
    f.icon("avatar-default-symbolic", user.x + 16.0, user.y + 12.0, 16.0);
    f.text_left(&st.user_name, Rect::new(user.x + 52.0, user.y, name_w + 4.0, 40.0), &name_style);
    let power = Rect::new(geo.x + geo.w - 32.0, geo.footer.y + 12.0, 40.0, 40.0);
    let touch = f.hit(power, Action::Start(Act::Power));
    f.tip("Power");
    f.state_layer(power, radius, touch, false);
    f.icon("system-shutdown-symbolic", power.x + 12.0, power.y + 12.0, 16.0);
    // The folders chosen in Personalization > Start, next to the power
    // button, as long as they leave the user's name room.
    for (slot, folder) in folder_slots(&st.desktop.start.folders, power.x - (user.right() + 8.0)).into_iter().enumerate() {
        let rect = Rect::new(power.x - (slot as f32 + 1.0) * 44.0, power.y, 40.0, 40.0);
        let touch = f.hit(rect, Action::Start(Act::Folder(folder)));
        f.tip(folder.label());
        f.state_layer(rect, radius, touch, false);
        f.icon_or_placeholder(&[folder.icon(), "folder-symbolic"], rect.x + 12.0, rect.y + 12.0, 16.0);
    }
}

/// The folders that fit into `room` logical pixels of the footer, nearest
/// to the power button first (the first of the list is next to it).
pub fn folder_slots(folders: &[Folder], room: f32) -> Vec<Folder> {
    let fit = (room.max(0.0) / 44.0).floor() as usize;
    folders.iter().copied().take(fit).collect()
}

/// Draws the part of the page that shows in its window, scrolled by
/// `offset`: straight onto the panel, and then what fell above or below
/// the window is wiped by putting those rows back as the panel (`chrome`)
/// has them, so that a row half scrolled out is cut at the window's edge
/// (the canvas is Start's own surface: the panel and its shadow).
/// Its controls react only where they show. Nothing else is drawn again
/// per frame: the panel itself is reused.
fn draw_page(f: &mut Frame, st: &State, geo: &Geometry, page: &Page, offset: f32, chrome: Option<&tiny_skia::Pixmap>) {
    let scale = f.canvas.scale();
    // Whole device pixels: the page is drawn as sharp as when it stands.
    let offset = (offset * scale).round() / scale;
    let window = geo.view;
    let first_hit = f.hits.len();
    let pointer = f.pointer;
    f.pointer = pointer.filter(|p| window.contains(*p));
    for element in &page.elements {
        if element.rect.bottom() + 4.0 < offset || element.rect.y - 4.0 > offset + window.h {
            continue;
        }
        let rect = Rect::new(element.rect.x, window.y + element.rect.y - offset, element.rect.w, element.rect.h);
        draw_element(f, st, geo, element, rect);
    }
    f.pointer = pointer;
    if let Some(chrome) = chrome {
        let height = f.canvas.height();
        f.restore_rows(chrome, 0.0, window.y);
        f.restore_rows(chrome, window.bottom(), height);
    }
    // The controls, cut to the window.
    let mut kept = Vec::new();
    for mut hit in f.hits.drain(first_hit..) {
        let r = hit.rect;
        let (y0, y1) = (r.y.max(window.y), r.bottom().min(window.bottom()));
        if y1 - y0 >= 1.0 {
            hit.rect = Rect::new(r.x, y0, r.w, y1 - y0);
            kept.push(hit);
        }
    }
    f.hits.extend(kept);
    f.scrollbar(Rect::new(window.x, window.y, window.w - 4.0, window.h), page.height, offset);
}

fn draw_element(f: &mut Frame, st: &State, geo: &Geometry, element: &Element, rect: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let m = geo.m;
    let action = element.act.clone().map(Action::Start);
    match &element.piece {
        Piece::Title(title) => {
            f.text_left(title, rect, &ramp("body-strong", theme.text));
        }
        Piece::Group(name) => {
            f.text_left(name, rect, &ramp("caption", theme.text_secondary));
        }
        Piece::Hint(hint) => {
            f.text_wrapped(hint, rect.x, rect.y, rect.w, 3, &ramp("body", theme.text_muted));
        }
        Piece::Button(label, glyph) => {
            let touch = f.hit(rect, action.unwrap_or(Action::None));
            let fill = f.res.fill(if touch.pressed {
                "control-pressed"
            } else if touch.hovered {
                "control-hover"
            } else {
                "control"
            });
            let stroke = f.res.stroke("control");
            let r = rect;
            f.canvas.fill_rounded_rect(r.x, r.y, r.w, r.h, radius, fill);
            f.canvas.stroke_rounded_rect(r.x, r.y, r.w, r.h, radius, 1.0, stroke);
            if touch.focused {
                f.focus_ring(r, radius);
            }
            f.text_left(label, Rect::new(r.x + 10.0, r.y, r.w - 34.0, r.h), &ramp("caption", theme.text));
            f.glyph(*glyph, Rect::new(r.right() - 20.0, r.y + (r.h - 12.0) / 2.0, 12.0, 12.0), theme.text_secondary);
        }
        Piece::Choice(view, selected) => {
            // A selector bar: the chosen view has strong text and an accent
            // mark under it.
            let touch = f.hit(rect, action.unwrap_or(Action::None));
            f.tip(&format!("{} view", view.label()));
            f.state_layer(rect, radius, touch, false);
            let color = if *selected { theme.text } else { theme.text_secondary };
            let style = if *selected { ramp("body-strong", color) } else { ramp("body", color) };
            f.text_centered(view.label(), Rect::new(rect.x + 2.0, rect.y, rect.w - 4.0, rect.h - 4.0), &style);
            if *selected {
                let mark = (rect.w * 0.4).clamp(12.0, 24.0);
                f.canvas.fill_rounded_rect(rect.x + (rect.w - mark) / 2.0, rect.bottom() - 3.0, mark, 3.0, 1.5, theme.accent);
            }
        }
        Piece::Tile(index) => {
            let app = &st.apps[*index];
            let button = rect.inset(2.0);
            let touch = f.hit(button, action.unwrap_or(Action::None));
            f.state_layer(button, radius, touch, false);
            let size = if touch.pressed { m.icon - 4.0 } else { m.icon };
            f.icon_or_placeholder(&[&app.icon, "application-x-executable"], rect.x + (rect.w - size) / 2.0, rect.y + 12.0 + (m.icon - size) / 2.0, size);
            let label = Rect::new(rect.x + 4.0, rect.y + 12.0 + m.icon + 6.0, rect.w - 8.0, m.label_h + 2.0);
            f.text_centered(&app.name, label, &ramp("caption", theme.text));
        }
        Piece::Row(index, why) => {
            let app = &st.apps[*index];
            let touch = f.hit(rect, action.unwrap_or(Action::None));
            f.state_layer(rect, radius, touch, false);
            match why {
                Some(why) => {
                    let icon = 32.0;
                    f.icon_or_placeholder(&[&app.icon, "application-x-executable"], rect.x + 10.0, rect.y + (rect.h - icon) / 2.0, icon);
                    let text_x = rect.x + 54.0;
                    let mid = rect.y + rect.h / 2.0;
                    let body = ramp("body", theme.text).line_height();
                    f.text_left(&app.name, Rect::new(text_x, mid - body, rect.w - 62.0, body), &ramp("body", theme.text));
                    f.text_left(why.label(), Rect::new(text_x, mid, rect.w - 62.0, m.label_h + 2.0), &ramp("caption", theme.text_muted));
                }
                None => {
                    let icon = m.list_icon;
                    f.icon_or_placeholder(&[&app.icon, "application-x-executable"], rect.x + 12.0, rect.y + (rect.h - icon) / 2.0, icon);
                    f.text_left(&app.name, Rect::new(rect.x + 24.0 + icon, rect.y, rect.w - 36.0 - icon, rect.h), &ramp("body", theme.text));
                }
            }
        }
        Piece::Letter(letter) => {
            let touch = f.hit(rect, action.unwrap_or(Action::None));
            f.tip("Go to a letter");
            f.state_layer(rect, radius, touch, false);
            f.text_left(&letter.to_string(), Rect::new(rect.x + 16.0, rect.y, rect.w - 16.0, rect.h), &ramp("body-strong", theme.text));
        }
    }
}

/// The index of letters over the list: a letter with applications goes to
/// its heading; the others are shown dimmed.
fn draw_index(f: &mut Frame, st: &State, geo: &Geometry) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    f.material(geo.view, [0.0; 4], "acrylic");
    f.area(geo.view, Action::Start(Act::CloseIndex));
    let present: Vec<char> = st.apps.iter().map(letter_of).collect();
    for (letter, rect) in index_cells(geo, &index_letters(&st.apps)) {
        let label = letter.to_string();
        if present.contains(&letter) {
            let touch = f.hit(rect, Action::Start(Act::Jump(letter)));
            f.state_layer(rect, radius, touch, false);
            f.text_centered(&label, rect, &ramp("subtitle", theme.text));
        } else {
            f.text_centered(&label, rect, &ramp("body-large", theme.text_muted.alpha(0.45)));
        }
    }
}

fn draw_results(f: &mut Frame, st: &State, geo: &Geometry) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let row_h = geo.m.row_h;
    let mut y = geo.view.y + 4.0;
    let bottom = geo.footer.y - 8.0;
    let mut last_kind = 0u8;
    let heading_h = geo.m.header_h + 4.0;
    for (position, result) in st.start.results.iter().enumerate() {
        let (kind, heading) = match result {
            _ if position == 0 => (1, "Best match"),
            ResultItem::App(_) => (2, "Apps"),
            ResultItem::Setting(_) => (3, "Settings"),
            ResultItem::Run(_) => (4, "Command"),
        };
        let height = if position == 0 { (row_h + 24.0).max(64.0) } else { row_h };
        let heading_height = if kind != last_kind { heading_h } else { 0.0 };
        if y + heading_height + height > bottom {
            break;
        }
        if kind != last_kind {
            f.text_left(heading, Rect::new(geo.x + 8.0, y, geo.w - 16.0, heading_h), &ramp("body-strong", theme.text));
            y += heading_h;
            last_kind = kind;
        }
        let row = Rect::new(geo.x, y + 2.0, geo.w, height - 4.0);
        let mut touch = f.area(row, Action::Start(Act::Result(position)));
        touch.focused = false;
        f.state_layer(row, radius, touch, st.start.selected == position);
        if st.start.selected == position {
            f.canvas.fill_rounded_rect(row.x, row.y + (row.h - 16.0) / 2.0, 3.0, 16.0, 1.5, theme.accent);
        }
        let icon_size = if position == 0 { 40.0 } else { 24.0 };
        let (icon_x, icon_y) = (row.x + 14.0, row.y + (row.h - icon_size) / 2.0);
        let text_x = icon_x + icon_size + 12.0;
        let text_w = row.right() - text_x - 12.0;
        let (title, subtitle): (String, &str) = match result {
            ResultItem::App(index) => {
                let app = &st.apps[*index];
                f.icon_or_placeholder(&[&app.icon, "application-x-executable"], icon_x, icon_y, icon_size);
                (app.name.clone(), "App")
            }
            ResultItem::Setting(index) => {
                let (_, label, icon, _) = SETTINGS[*index];
                f.icon_or_placeholder(&[icon, "preferences-system"], icon_x, icon_y, icon_size);
                (label.to_owned(), "Settings")
            }
            ResultItem::Run(command) => {
                f.icon_or_placeholder(&["utilities-terminal"], icon_x, icon_y, icon_size);
                (format!("Run “{command}”"), "Command")
            }
        };
        if position == 0 {
            let body = ramp("body-strong", theme.text).line_height();
            let mid = row.y + row.h / 2.0;
            f.text_left(&title, Rect::new(text_x, mid - body, text_w, body), &ramp("body-strong", theme.text));
            f.text_left(subtitle, Rect::new(text_x, mid, text_w, geo.m.label_h + 2.0), &ramp("caption", theme.text_muted));
        } else {
            f.text_left(&title, Rect::new(text_x, row.y, text_w, row.h), &ramp("body", theme.text));
        }
        y += height;
    }
    if st.start.results.is_empty() {
        f.text_left("No results", Rect::new(geo.x + 8.0, y, geo.w - 16.0, 32.0), &ramp("body", theme.text_muted));
    }
}

/// Where the keyboard focus goes from `current` in direction `nav`: through
/// the whole page (also what is scrolled out of sight), then to the
/// buttons at the bottom (`footer`, surface coordinates) and back.
/// `None` leaves the focus in the search box.
pub fn navigate(page: &Page, footer: &[Hit], current: Option<&Action>, nav: Nav) -> Option<Action> {
    let content = page.hits();
    let on_page = matches!(current, Some(Action::Start(act)) if act.on_page());
    let in_footer = matches!(current, Some(Action::Start(act)) if act.in_footer());
    let first_footer = || footer.first().map(|h| h.action.clone());
    let last_footer = || footer.last().map(|h| h.action.clone());
    let first_content = || content.first().map(|h| h.action.clone());
    let last_content = || content.last().map(|h| h.action.clone());
    if on_page {
        if let Some(next) = crate::ui::navigate(&content, current, nav) {
            // Tab and Shift+Tab leave the page at its ends.
            let position = content.iter().position(|h| Some(&h.action) == current);
            let wrapped = match (nav, position) {
                (Nav::Next, Some(p)) => p + 1 == content.len(),
                (Nav::Previous, Some(p)) => p == 0,
                _ => false,
            };
            if !wrapped {
                return Some(next);
            }
        }
        return match nav {
            Nav::Down | Nav::Next => first_footer().or_else(first_content),
            Nav::Previous => last_footer().or_else(last_content),
            Nav::Up => None,
            _ => current.cloned(),
        };
    }
    if in_footer {
        return match nav {
            Nav::Left | Nav::Right => crate::ui::navigate(footer, current, nav).or_else(|| current.cloned()),
            Nav::Next => {
                let position = footer.iter().position(|h| Some(&h.action) == current);
                match position {
                    Some(p) if p + 1 < footer.len() => Some(footer[p + 1].action.clone()),
                    _ => first_content().or_else(first_footer),
                }
            }
            Nav::Previous => {
                let position = footer.iter().position(|h| Some(&h.action) == current);
                match position {
                    Some(p) if p > 0 => Some(footer[p - 1].action.clone()),
                    _ => last_content().or_else(last_footer),
                }
            }
            Nav::Up => last_content().or_else(|| current.cloned()),
            Nav::Down => current.cloned(),
        };
    }
    // From the search box.
    match nav {
        Nav::Down | Nav::Next | Nav::Right => first_content().or_else(first_footer),
        Nav::Previous => last_footer().or_else(last_content),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn app(id: &str, name: &str, keywords: &[&str]) -> App {
        App {
            id: id.into(),
            name: name.into(),
            comment: String::new(),
            icon: String::new(),
            exec: vec![id.into()],
            terminal: false,
            keywords: keywords.iter().map(|k| (*k).to_owned()).collect(),
            wm_class: String::new(),
            actions: Vec::new(),
            categories: Vec::new(),
        }
    }

    fn apps() -> Vec<App> {
        vec![
            app("com.boswas.ArchiveManager", "Archive Manager", &[]),
            app("com.boswas.Calculator", "Calculator", &[]),
            app("com.boswas.Files", "Files", &["folder"]),
            app("firefox-esr", "Firefox", &["browser", "web"]),
            app("com.boswas.Terminal", "Terminal", &["shell"]),
            app("zz.app", "9 Lives", &[]),
        ]
    }

    #[test]
    fn a_new_user_gets_the_defaults_then_everything_else() {
        let apps = apps();
        let ids = pinned_ids(&ShellConfig::default(), &apps);
        assert_eq!(&ids[..5], ["firefox-esr", "com.boswas.Files", "com.boswas.Calculator", "com.boswas.Terminal", "com.boswas.ArchiveManager"]);
        assert_eq!(ids.len(), apps.len(), "the page is filled with the other applications");
        let mut config = ShellConfig::default();
        config.start = Some(vec!["firefox-esr".into(), "not.installed".into()]);
        assert_eq!(pinned_ids(&config, &apps), ["firefox-esr"], "the user's list is used as it is");
        config.start = Some(Vec::new());
        assert!(pinned_ids(&config, &apps).is_empty());
        // Many other applications: the first rows are filled, no more.
        let mut many = apps.clone();
        many.extend((0..40).map(|i| app(&format!("other{i:02}"), &format!("Other {i:02}"), &[])));
        let ids = pinned_ids(&ShellConfig::default(), &many);
        assert_eq!(ids.len(), DEFAULT_FILL);
        assert_eq!(&ids[..2], ["firefox-esr", "com.boswas.Files"], "the defaults that are installed come first");
    }

    #[test]
    fn search_offers_apps_settings_and_a_command() {
        let apps = apps();
        let usage = HashMap::new();
        assert!(search(&apps, &usage, "  ").is_empty());
        let results = search(&apps, &usage, "fi");
        assert_eq!(results[0], ResultItem::App(2), "Files starts with the query");
        assert_eq!(results[1], ResultItem::App(3));
        assert_eq!(results.last(), Some(&ResultItem::Run("fi".into())));
        let wifi = search(&apps, &usage, "wifi");
        assert!(wifi.iter().any(|r| matches!(r, ResultItem::Setting(i) if SETTINGS[*i].0 == "network")), "{wifi:?}");
        let display = search(&apps, &usage, "Disp");
        assert!(matches!(display[0], ResultItem::Setting(i) if SETTINGS[i].0 == "display"), "{display:?}");
        // An application that only mentions the word does not outrank the page.
        let mut more = apps.clone();
        let mut viewer = app("im.display", "ImageMagick", &[]);
        viewer.comment = "Display and edit images".into();
        more.push(viewer);
        let display = search(&more, &usage, "disp");
        assert!(matches!(display[0], ResultItem::Setting(i) if SETTINGS[i].0 == "display"), "{display:?}");
        assert!(display.contains(&ResultItem::App(more.len() - 1)));
        assert_eq!(display.iter().filter(|r| matches!(r, ResultItem::Setting(_))).count(), 1);
        assert_eq!(search(&apps, &usage, "xyzzy"), [ResultItem::Run("xyzzy".into())]);
        assert_eq!(settings_command("display"), ["boswas-control-center", "--page", "display"]);
    }

    #[test]
    fn settings_keys_are_unique_and_plain() {
        let mut keys: Vec<&str> = SETTINGS.iter().map(|s| s.0).collect();
        // Words of lower-case letters joined by hyphens: safe as an argument, and Control Center's own keys.
        assert!(keys.iter().all(|k| !k.is_empty() && k.split('-').all(|w| !w.is_empty() && w.chars().all(|c| c.is_ascii_lowercase()))));
        keys.sort_unstable();
        keys.dedup();
        assert_eq!(keys.len(), SETTINGS.len());
    }

    #[test]
    fn keys_edit_the_query_and_walk_the_results() {
        let apps = apps();
        let usage = HashMap::new();
        let mut start = Start::default();
        assert_eq!(start.key(Keysym::Down, false, &apps, &usage), KeyOutcome::Ignored, "with no query the page has the arrows");
        assert_eq!(start.key(Keysym::Page_Down, false, &apps, &usage), KeyOutcome::Ignored, "and the page keys");
        assert_eq!(start.key(Keysym::Escape, false, &apps, &usage), KeyOutcome::Close);
        assert_eq!(start.typed("f", &apps, &usage), KeyOutcome::Redraw);
        assert!(start.searching() && start.results.len() >= 3);
        assert_eq!(start.key(Keysym::Down, false, &apps, &usage), KeyOutcome::Redraw);
        assert_eq!(start.selected, 1);
        assert_eq!(start.key(Keysym::Up, false, &apps, &usage), KeyOutcome::Redraw);
        assert_eq!(start.key(Keysym::Up, false, &apps, &usage), KeyOutcome::Redraw);
        assert_eq!(start.selected, start.results.len() - 1, "wraps to the command row");
        assert!(matches!(start.key(Keysym::Return, false, &apps, &usage), KeyOutcome::Activate(ResultItem::Run(_))));
        assert_eq!(start.key(Keysym::BackSpace, false, &apps, &usage), KeyOutcome::Redraw);
        assert!(!start.searching() && start.results.is_empty());
        start.typed("te", &apps, &usage);
        assert_eq!(start.key(Keysym::Escape, false, &apps, &usage), KeyOutcome::Redraw, "Esc first clears the query");
        assert!(start.query.is_empty());
        start.index = true;
        assert_eq!(start.key(Keysym::Escape, false, &apps, &usage), KeyOutcome::Redraw, "then closes the index of letters");
        assert!(!start.index);
        assert_eq!(start.key(Keysym::Escape, false, &apps, &usage), KeyOutcome::Close, "then closes Start");
        start.index = true;
        start.typed("x", &apps, &usage);
        assert!(!start.index, "typing closes the index");
    }

    #[test]
    fn all_apps_are_listed_under_letters() {
        let rows = all_rows(&apps());
        assert_eq!(rows[0], ListRow::Letter('A'));
        assert_eq!(rows[1], ListRow::App(0));
        assert_eq!(rows[2], ListRow::Letter('C'));
        assert_eq!(rows.iter().filter(|r| matches!(r, ListRow::Letter('F'))).count(), 1, "Files and Firefox share one heading");
        assert_eq!(rows[rows.len() - 2], ListRow::Letter('#'));
        assert!(all_rows(&[]).is_empty());
        let mut more = apps();
        more.push(app("de.example", "Ärzte", &[]));
        more.push(app("in.example", "हिंदी", &[]));
        let letters = index_letters(&more);
        assert_eq!(&letters[..26], ('A'..='Z').collect::<Vec<_>>().as_slice());
        assert_eq!(letters.last(), Some(&'#'));
        assert!(letters.contains(&'Ä') && letters.contains(&'ह'), "{letters:?}");
    }

    #[test]
    fn applications_fall_into_categories_like_windows() {
        let with = |name: &str, categories: &str| App { categories: categories.split(';').filter(|c| !c.is_empty()).map(str::to_owned).collect(), ..app(name, name, &[]) };
        // The Categories of the system applications' desktop entries.
        let cases = [
            ("Firefox", "Network;WebBrowser;", "Internet"),
            ("Files", "System;FileTools;FileManager;Utility;Core;Qt;", "Utilities & tools"),
            ("Photos", "Graphics;Viewer;2DGraphics;RasterGraphics;Photography;Qt;", "Creativity"),
            ("Camera", "AudioVideo;Video;Recorder;Graphics;Photography;Qt;", "Creativity"),
            ("Media Player", "AudioVideo;Audio;Video;Player;Qt;", "Entertainment"),
            ("Calendar", "Office;Calendar;Qt;", "Productivity"),
            ("Document Viewer", "Office;Viewer;Qt;", "Productivity"),
            ("Text Editor", "Utility;TextEditor;Qt;", "Productivity"),
            ("Calculator", "Utility;Calculator;Qt;", "Utilities & tools"),
            ("Archive Manager", "Utility;Archiving;Compression;Qt;", "Utilities & tools"),
            ("Terminal", "System;TerminalEmulator;", "System"),
            ("Control Center", "Settings;System;Qt;", "System"),
            ("Task Manager", "System;Monitor;", "System"),
            ("A game", "Game;ArcadeGame;", "Entertainment"),
            ("Unsorted", "", "Other"),
            ("Qt only", "Qt;KDE;", "Other"),
        ];
        for (name, categories, group) in cases {
            assert_eq!(CATEGORIES[category_of(&with(name, categories))], group, "{name}");
        }
        let list: Vec<App> = cases.iter().map(|(n, c, _)| with(n, c)).collect();
        let groups = category_groups(&list);
        let names: Vec<&str> = groups.iter().map(|(g, _)| CATEGORIES[*g]).collect();
        assert_eq!(names, ["Productivity", "Creativity", "Utilities & tools", "Entertainment", "Internet", "System", "Other"]);
        assert_eq!(groups.iter().map(|(_, m)| m.len()).sum::<usize>(), list.len(), "every application once");
        assert!(category_groups(&[]).is_empty());
    }

    #[test]
    fn recommended_lists_the_most_used_first() {
        let apps = apps();
        let usage: HashMap<String, u32> = [("firefox-esr".to_owned(), 9), ("com.boswas.Calculator".to_owned(), 2), ("gone".to_owned(), 50), ("com.boswas.ArchiveManager".to_owned(), 0)].into();
        assert_eq!(recommended(&apps, &usage, 6), [3, 1]);
        assert_eq!(recommended(&apps, &usage, 1), [3]);
        assert!(recommended(&apps, &HashMap::new(), 6).is_empty());
    }

    #[test]
    fn recommendations_follow_the_settings() {
        let apps = apps();
        let usage: HashMap<String, u32> = [("firefox-esr".to_owned(), 9), ("com.boswas.Calculator".to_owned(), 2)].into();
        let mut seen = SeenApps::default();
        let ids: Vec<&str> = apps.iter().map(|a| a.id.as_str()).filter(|id| *id != "zz.app" && *id != "firefox-esr").collect();
        seen.update(&ids, 10);
        let all: Vec<&str> = apps.iter().map(|a| a.id.as_str()).collect();
        seen.update(&all, 100);
        let mut start = settings::Start::default();
        let list = recommendations(&apps, &usage, &seen, &start, 200, 6);
        assert_eq!(list[0].1, Why::RecentlyAdded);
        assert_eq!(list.iter().filter(|(_, why)| *why == Why::RecentlyAdded).count(), 2, "Firefox and 9 Lives are new");
        assert_eq!(list.iter().filter(|(i, _)| *i == 3).count(), 1, "Firefox is listed once");
        assert!(list.contains(&(1, Why::FrequentlyUsed)));
        start.recently_added = false;
        assert_eq!(recommendations(&apps, &usage, &seen, &start, 200, 6), [(3, Why::FrequentlyUsed), (1, Why::FrequentlyUsed)]);
        start.most_used = false;
        assert!(recommendations(&apps, &usage, &seen, &start, 200, 6).is_empty());
        start.recently_added = true;
        assert_eq!(recommendations(&apps, &usage, &seen, &start, 200, 2).len(), 1, "new ones take half the room at most");
        assert_eq!(why_labels(), ["Recently added", "Frequently used"]);
        assert_eq!(folder_slots(&[Folder::Settings, Folder::Music, Folder::Videos], 100.0), [Folder::Settings, Folder::Music]);
        assert!(folder_slots(&[Folder::Settings], -5.0).is_empty());
    }

    fn why_labels() -> [&'static str; 2] {
        [Why::RecentlyAdded.label(), Why::FrequentlyUsed.label()]
    }

    #[test]
    fn start_grows_with_the_output_and_keeps_off_the_taskbar() {
        // Eight columns of pins on wide outputs, six where eight do not fit.
        assert_eq!(columns_for((1920.0, 1080.0)), 8);
        assert_eq!(columns_for((2560.0, 1440.0)), 8);
        assert_eq!(columns_for((1600.0, 900.0)), 8);
        assert_eq!(columns_for((1366.0, 768.0)), 6);
        assert_eq!(columns_for((1536.0, 864.0)), 6, "1920x1080 at 125 %");
        assert_eq!(columns_for((1280.0, 1024.0)), 6);
        // 832 x 918 at 1920x1080: 8 x 96 + 2 x 32, and 85 % of the height.
        assert_eq!(panel_size((1920.0, 1080.0)), (832.0, 918.0));
        assert_eq!(panel_size((1366.0, 768.0)), (640.0, 652.0));
        assert_eq!(panel_size((2560.0, 1440.0)), (832.0, 1200.0), "at most start-max-height");
        assert_eq!(panel_size((1536.0, 864.0)), (640.0, 734.0));
        // Never over the taskbar and its margins, never wider than the output.
        let (w, h) = panel_size((800.0, 600.0));
        assert_eq!((w, h), (640.0, 510.0));
        let (w, h) = panel_size((480.0, 400.0));
        assert!(w <= 480.0 - 24.0 && h <= 400.0 - 48.0 - 24.0, "{w} x {h}");
        let geo = geometry(Rect::new(12.0, 12.0, 832.0, 918.0), StartLayout::Default);
        assert_eq!((geo.columns, geo.cell_w), (8, 96.0));
        assert_eq!((geo.pin_rows, geo.recommended_rows), (2, 2));
        assert!(geo.footer.bottom() == 930.0 && geo.search.x == 44.0 && geo.view.bottom() == geo.footer.y);
        let narrow = geometry(Rect::new(12.0, 12.0, 640.0, 652.0), StartLayout::Default);
        assert_eq!((narrow.columns, narrow.cell_w), (6, 96.0));
        assert_eq!(geometry(Rect::new(12.0, 12.0, 400.0, 360.0), StartLayout::Default).columns, 4);
        // The layouts trade rows of pins for rows of recommendations.
        assert_eq!(layout_rows(StartLayout::MorePins), (3, 1));
        assert_eq!(layout_rows(StartLayout::MoreRecommendations), (1, 3));
        // Pins and the grid at 36 px (Windows: 32), the list at 24.
        let m = metrics();
        assert_eq!((m.icon, m.list_icon, m.cell_h), (36.0, 24.0, 88.0));
    }

    #[test]
    fn the_page_scrolls_smoothly_and_reveals_the_focus() {
        let now = Instant::now();
        let mut start = Start::default();
        assert!(!start.animating(now));
        assert!(start.scroll_by(120.0, 1000.0, now, false));
        assert!(start.animating(now));
        assert!(start.offset(now) < 0.01, "it starts where it was");
        let mid = start.offset(now + scroll_duration() / 2);
        assert!(mid > 60.0 && mid < 120.0, "and slows down towards its end: {mid}");
        assert_eq!(start.offset(now + scroll_duration()), 120.0);
        assert!(!start.animating(now + scroll_duration()));
        // A second turn of the wheel while it moves goes on from there.
        let later = now + scroll_duration() / 2;
        assert!(start.scroll_by(120.0, 1000.0, later, false));
        assert_eq!(start.scroll, 240.0);
        assert!((start.offset(later) - mid).abs() < 0.01);
        // Kept within the page.
        assert!(start.scroll_to(5000.0, 1000.0, now, true));
        assert_eq!((start.scroll, start.offset(now)), (1000.0, 1000.0), "reduced motion jumps");
        assert!(!start.scroll_by(10.0, 1000.0, now, true), "the end stays");
        assert!(start.scroll_to(-50.0, 1000.0, now, true) && start.scroll == 0.0);
        // Revealing: as little as needed, with a margin.
        assert!(!start.reveal(Rect::new(0.0, 100.0, 50.0, 50.0), 400.0, 1000.0, now, true));
        assert!(start.reveal(Rect::new(0.0, 500.0, 50.0, 50.0), 400.0, 1000.0, now, true));
        assert_eq!(start.scroll, 550.0 + REVEAL_MARGIN - 400.0);
        assert!(start.reveal(Rect::new(0.0, 20.0, 50.0, 50.0), 400.0, 1000.0, now, true));
        assert_eq!(start.scroll, 20.0 - REVEAL_MARGIN);
        start.scroll = 900.0;
        start.clamp(300.0);
        assert_eq!(start.scroll, 300.0);
        start.reset();
        assert_eq!((start.scroll, start.expanded, start.index), (0.0, false, false));
    }
}
