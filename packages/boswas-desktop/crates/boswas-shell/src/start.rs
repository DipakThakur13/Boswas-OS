//! Start: search, the pinned applications, all applications, recommended
//! applications, and the user and power buttons.

use std::collections::HashMap;

use boswas_ui::tokens::tokens;
use smithay_client_toolkit::seat::keyboard::Keysym;

use crate::{
    action::Action,
    apps::{self, App},
    config::{SeenApps, ShellConfig, DEFAULT_START},
    glyph::Glyph,
    settings::{self, Folder, StartLayout},
    state::State,
    ui::{ramp, ButtonKind, Frame, Rect, TextField},
};

#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    /// The search box (a click puts the caret there; typing always does).
    SearchBox,
    /// An application by index into the application list.
    App(usize),
    /// The same, from the "Recommended" list (a control of its own, so the
    /// keyboard focus can tell the two apart).
    Recommended(usize),
    AllApps,
    Back,
    /// A page of the pinned grid.
    Page(usize),
    /// A search result by position.
    Result(usize),
    User,
    Power,
    /// A folder or place next to the power button.
    Folder(Folder),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum View {
    #[default]
    Home,
    AllApps,
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
    ("privacy", "Privacy", "preferences-system-privacy", &["telemetry", "history", "data"]),
    ("hardware", "Hardware & drivers", "hwinfo", &["driver", "firmware", "device", "graphics"]),
    ("system", "Language, region & time", "preferences-system", &["language", "region", "date", "time", "notifications", "locale"]),
    ("about", "About this device", "boswas-logo", &["version", "specification", "system information"]),
];

/// The command that opens a settings page.
pub fn settings_command(key: &str) -> Vec<String> {
    vec!["boswas-control-center".into(), "--page".into(), key.to_owned()]
}

const COLUMNS: usize = 6;
const CELL_H: f32 = 84.0;
const ROW_H: f32 = 40.0;
const FOOTER_H: f32 = 64.0;
/// Results of each kind shown.
const MAX_APP_RESULTS: usize = 7;
const MAX_SETTING_RESULTS: usize = 4;

/// The applications pinned to Start, as ids: the user's list, or for a new
/// user the defaults followed by other installed applications.
pub fn pinned_ids(config: &ShellConfig, apps: &[App]) -> Vec<String> {
    if let Some(list) = &config.start {
        return list.iter().filter(|id| apps.iter().any(|a| &a.id == *id)).cloned().collect();
    }
    let mut ids: Vec<String> = DEFAULT_START.iter().filter(|id| apps.iter().any(|a| a.id == **id)).map(|id| (*id).to_owned()).collect();
    for app in apps {
        if ids.len() >= 3 * COLUMNS {
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

/// A row of the "All apps" list.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ListRow {
    Letter(char),
    App(usize),
}

/// All applications under letter headings ("#" for names that do not start
/// with a letter). `apps` is sorted by name.
pub fn all_rows(apps: &[App]) -> Vec<ListRow> {
    let mut rows = Vec::new();
    let mut current = None;
    for (index, app) in apps.iter().enumerate() {
        let letter = app.name.chars().next().filter(|c| c.is_alphabetic()).and_then(|c| c.to_uppercase().next()).unwrap_or('#');
        if current != Some(letter) {
            rows.push(ListRow::Letter(letter));
            current = Some(letter);
        }
        rows.push(ListRow::App(index));
    }
    rows
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
    pub view: View,
    /// Page of the pinned grid.
    pub page: usize,
    /// First row shown of the "All apps" list.
    pub scroll: usize,
    pub results: Vec<ResultItem>,
    /// The highlighted search result.
    pub selected: usize,
}

impl Start {
    /// Returns Start to its opening state.
    pub fn reset(&mut self) {
        self.query.clear();
        self.view = View::Home;
        self.page = 0;
        self.scroll = 0;
        self.results.clear();
        self.selected = 0;
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
            self.refresh(apps, usage);
            KeyOutcome::Redraw
        } else {
            KeyOutcome::Ignored
        }
    }

    /// Handles the keys that belong to the search box and its results.
    pub fn key(&mut self, keysym: Keysym, ctrl: bool, apps: &[App], usage: &HashMap<String, u32>) -> KeyOutcome {
        match keysym {
            Keysym::Escape => {
                if self.query.is_empty() && self.view == View::Home {
                    return KeyOutcome::Close;
                }
                if self.query.is_empty() {
                    self.view = View::Home;
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

    /// Scrolls the current view by wheel steps. Returns whether it moved.
    pub fn scroll_by(&mut self, steps: i32, st_pages: usize, list_rows: usize, visible_rows: usize) -> bool {
        match self.view {
            View::Home => {
                let page = (self.page as i64 + i64::from(steps.signum())).clamp(0, st_pages.saturating_sub(1) as i64) as usize;
                let moved = page != self.page;
                self.page = page;
                moved
            }
            View::AllApps => {
                let max = list_rows.saturating_sub(visible_rows) as i64;
                let scroll = (self.scroll as i64 + i64::from(steps) * 3).clamp(0, max) as usize;
                let moved = scroll != self.scroll;
                self.scroll = scroll;
                moved
            }
        }
    }
}

/// The size of Start's panel on an output of the given size.
pub fn panel_size(output: (f32, f32)) -> (f32, f32) {
    let t = tokens();
    let margin = t.shell("flyout-margin");
    let w = t.shell("start-width").min(output.0 - 2.0 * margin).max(240.0);
    let h = t.shell("start-height").min(output.1 - t.shell("taskbar-height") - 2.0 * margin).max(240.0);
    (w, h)
}

/// The fixed parts of Start inside its panel.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Geometry {
    pub search: Rect,
    /// Left edge and width of the content column.
    pub x: f32,
    pub w: f32,
    /// Top of the content under the search box.
    pub top: f32,
    pub footer: Rect,
    /// Rows of the pinned grid.
    pub rows: usize,
    /// Rows of "Recommended" under the grid (two applications each); 0
    /// when it does not fit.
    pub recommended: usize,
    /// Rows the "All apps" list shows.
    pub list_rows: usize,
}

/// Rows of pins and of recommendations of each layout (Personalization >
/// Start > Layout).
pub fn layout_rows(layout: StartLayout) -> (usize, usize) {
    match layout {
        StartLayout::Default => (3, 3),
        StartLayout::MorePins => (4, 1),
        StartLayout::MoreRecommendations => (2, 4),
    }
}

pub fn geometry(panel: Rect, layout: StartLayout) -> Geometry {
    let pad = if panel.w >= 560.0 { 32.0 } else { 16.0 };
    let search = Rect::new(panel.x + pad, panel.y + 24.0, panel.w - 2.0 * pad, 36.0);
    let footer = Rect::new(panel.x, panel.bottom() - FOOTER_H, panel.w, FOOTER_H);
    let top = search.bottom() + 16.0;
    let room = footer.y - top - 44.0 - 8.0;
    let (pin_rows, recommended_rows) = layout_rows(layout);
    let recommended_block = 44.0 + recommended_rows as f32 * 52.0 + 8.0;
    let (rows, recommended) = if room >= pin_rows as f32 * CELL_H + recommended_block {
        (pin_rows, recommended_rows)
    } else {
        (((room / CELL_H).floor() as usize).clamp(1, pin_rows), 0)
    };
    Geometry {
        search,
        x: panel.x + pad,
        w: panel.w - 2.0 * pad,
        top,
        footer,
        rows,
        recommended,
        list_rows: (((footer.y - top - 44.0 - 8.0) / ROW_H).floor() as usize).max(1),
    }
}

/// Pages of the pinned grid.
pub fn page_count(pins: usize, rows: usize) -> usize {
    pins.div_ceil((COLUMNS * rows).max(1)).max(1)
}

pub fn draw(f: &mut Frame, st: &State, panel: Rect) {
    let theme = f.theme();
    let t = tokens();
    let radius = t.radius_control;
    let geo = geometry(panel, st.desktop.start.layout);
    f.panel(panel, "acrylic");
    f.area(panel, Action::None);

    // Search box: always has the caret, whatever else is focused.
    let touch = f.area(geo.search, Action::Start(Act::SearchBox));
    let fill = if touch.hovered { f.res.fill("control-hover") } else { f.res.fill("control") };
    let stroke = f.res.stroke("control");
    f.canvas.fill_rounded_rect(geo.search.x, geo.search.y, geo.search.w, geo.search.h, 18.0, fill);
    f.canvas.stroke_rounded_rect(geo.search.x, geo.search.y, geo.search.w, geo.search.h, 18.0, 1.0, stroke);
    f.icon("system-search-symbolic", geo.search.x + 14.0, geo.search.y + 10.0, 16.0);
    let style = ramp("body", theme.text);
    let text_x = geo.search.x + 40.0;
    let text_y = geo.search.y + (geo.search.h - style.line_height()) / 2.0;
    if st.start.query.is_empty() {
        f.text("Search for apps, settings, and commands", text_x, text_y, Some(geo.search.w - 56.0), &ramp("body", theme.text_muted));
        f.canvas.fill_rect(text_x, geo.search.y + 9.0, 1.0, 18.0, theme.text);
    } else {
        let before: String = st.start.query.text().chars().take(st.start.query.cursor()).collect();
        let caret = f.res.text.measure(&before, &style).0;
        f.text(st.start.query.text(), text_x, text_y, Some(geo.search.w - 56.0), &style);
        f.canvas.fill_rect((text_x + caret).min(geo.search.right() - 16.0), geo.search.y + 9.0, 1.0, 18.0, theme.text);
    }

    if st.start.searching() {
        draw_results(f, st, &geo);
    } else if st.start.view == View::AllApps {
        draw_all_apps(f, st, &geo);
    } else {
        draw_home(f, st, &geo, panel);
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

fn section_header(f: &mut Frame, geo: &Geometry, y: f32, title: &str, button: Option<(&str, Action)>) {
    let theme = f.theme();
    f.text_left(title, Rect::new(geo.x + 8.0, y, geo.w - 140.0, 28.0), &ramp("body-strong", theme.text));
    if let Some((label, action)) = button {
        let width = f.res.text.measure(label, &ramp("caption", theme.text)).0 + 36.0;
        let rect = Rect::new(geo.x + geo.w - width - 4.0, y, width, 28.0);
        let touch = f.hit(rect, action);
        let fill = f.res.fill(if touch.pressed {
            "control-pressed"
        } else if touch.hovered {
            "control-hover"
        } else {
            "control"
        });
        let stroke = f.res.stroke("control");
        let radius = tokens().radius_control;
        f.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, fill);
        f.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, 1.0, stroke);
        if touch.focused {
            f.focus_ring(rect, radius);
        }
        let back = label == "Back";
        let text = Rect::new(rect.x + if back { 24.0 } else { 10.0 }, rect.y, rect.w - 34.0, rect.h);
        f.text_left(label, text, &ramp("caption", theme.text));
        let glyph = if back { Glyph::ChevronLeft } else { Glyph::ChevronRight };
        let gx = if back { rect.x + 8.0 } else { rect.right() - 20.0 };
        f.glyph(glyph, Rect::new(gx, rect.y + 8.0, 12.0, 12.0), theme.text_secondary);
    }
}

fn draw_home(f: &mut Frame, st: &State, geo: &Geometry, panel: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let icon = tokens().shell("start-icon");
    section_header(f, geo, geo.top, "Pinned", Some(("All apps", Action::Start(Act::AllApps))));
    let pins = st.start_pins();
    let per_page = COLUMNS * geo.rows;
    let pages = page_count(pins.len(), geo.rows);
    let page = st.start.page.min(pages - 1);
    let grid_top = geo.top + 44.0;
    let cell_w = geo.w / COLUMNS as f32;
    let label_style = ramp("caption", theme.text);
    for (slot, id) in pins.iter().skip(page * per_page).take(per_page).enumerate() {
        let Some(index) = st.app_index(id) else {
            continue;
        };
        let app = &st.apps[index];
        let cell = Rect::new(geo.x + (slot % COLUMNS) as f32 * cell_w, grid_top + (slot / COLUMNS) as f32 * CELL_H, cell_w, CELL_H);
        let button = cell.inset(2.0);
        let touch = f.hit(button, Action::Start(Act::App(index)));
        f.state_layer(button, radius, touch, false);
        let size = if touch.pressed { icon - 4.0 } else { icon };
        f.icon_or_placeholder(&[&app.icon, "application-x-executable"], cell.x + (cell_w - size) / 2.0, cell.y + 12.0 + (icon - size) / 2.0, size);
        f.text_centered(&app.name, Rect::new(cell.x + 4.0, cell.y + 52.0, cell_w - 8.0, 20.0), &label_style);
    }
    if pins.is_empty() {
        f.text_left("Nothing is pinned. Right-click an application under All apps to pin it.", Rect::new(geo.x + 8.0, grid_top, geo.w - 16.0, 40.0), &ramp("body", theme.text_muted));
    }
    if pages > 1 {
        let x = panel.right() - 18.0;
        let top = grid_top + (geo.rows as f32 * CELL_H - pages as f32 * 16.0) / 2.0;
        for dot in 0..pages {
            let rect = Rect::new(x - 8.0, top + dot as f32 * 16.0, 16.0, 16.0);
            f.area(rect, Action::Start(Act::Page(dot)));
            let (r, color) = if dot == page { (3.0, theme.text) } else { (2.0, theme.text_muted) };
            f.canvas.fill_circle(x, rect.y + 8.0, r, color);
        }
    }
    if geo.recommended == 0 {
        return;
    }
    let top = grid_top + geo.rows as f32 * CELL_H + 8.0;
    section_header(f, geo, top, "Recommended", None);
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
    let list = recommendations(&st.apps, &st.usage.counts, &st.seen, &st.desktop.start, now, 2 * geo.recommended);
    if list.is_empty() {
        let hint = if st.desktop.start.recently_added || st.desktop.start.most_used {
            "New applications and the applications you use most will appear here."
        } else {
            "Turn on recommendations in Settings, Personalization, Start."
        };
        f.text_wrapped(hint, geo.x + 8.0, top + 40.0, geo.w - 16.0, 2, &ramp("body", theme.text_muted));
        return;
    }
    let column = geo.w / 2.0;
    for (slot, (index, why)) in list.iter().enumerate() {
        let app = &st.apps[*index];
        let row = Rect::new(geo.x + (slot % 2) as f32 * column, top + 40.0 + (slot / 2) as f32 * 52.0, column - 8.0, 48.0);
        let touch = f.hit(row, Action::Start(Act::Recommended(*index)));
        f.state_layer(row, radius, touch, false);
        f.icon_or_placeholder(&[&app.icon, "application-x-executable"], row.x + 10.0, row.y + 8.0, 32.0);
        f.text_left(&app.name, Rect::new(row.x + 54.0, row.y + 5.0, row.w - 62.0, 20.0), &ramp("body", theme.text));
        f.text_left(why.label(), Rect::new(row.x + 54.0, row.y + 24.0, row.w - 62.0, 18.0), &ramp("caption", theme.text_muted));
    }
}

fn draw_all_apps(f: &mut Frame, st: &State, geo: &Geometry) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    section_header(f, geo, geo.top, "All apps", Some(("Back", Action::Start(Act::Back))));
    let rows = all_rows(&st.apps);
    let top = geo.top + 44.0;
    let view = Rect::new(geo.x, top, geo.w, geo.list_rows as f32 * ROW_H);
    let scroll = st.start.scroll.min(rows.len().saturating_sub(geo.list_rows));
    for (slot, row) in rows.iter().skip(scroll).take(geo.list_rows).enumerate() {
        let rect = Rect::new(geo.x, top + slot as f32 * ROW_H, geo.w - 12.0, ROW_H);
        match row {
            ListRow::Letter(letter) => {
                f.text_left(&letter.to_string(), Rect::new(rect.x + 16.0, rect.y, 40.0, ROW_H), &ramp("body-strong", theme.text));
            }
            ListRow::App(index) => {
                let app = &st.apps[*index];
                let button = Rect::new(rect.x, rect.y + 2.0, rect.w, ROW_H - 4.0);
                let touch = f.hit(button, Action::Start(Act::App(*index)));
                f.state_layer(button, radius, touch, false);
                f.icon_or_placeholder(&[&app.icon, "application-x-executable"], rect.x + 12.0, rect.y + 8.0, 24.0);
                f.text_left(&app.name, Rect::new(rect.x + 48.0, rect.y, rect.w - 60.0, ROW_H), &ramp("body", theme.text));
            }
        }
    }
    if rows.is_empty() {
        f.text_left("No applications are installed.", Rect::new(geo.x + 8.0, top, geo.w - 16.0, ROW_H), &ramp("body", theme.text_muted));
    }
    f.scrollbar(view, rows.len() as f32 * ROW_H, scroll as f32 * ROW_H);
}

fn draw_results(f: &mut Frame, st: &State, geo: &Geometry) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let mut y = geo.top;
    let bottom = geo.footer.y - 8.0;
    let mut last_kind = 0u8;
    for (position, result) in st.start.results.iter().enumerate() {
        let (kind, heading) = match result {
            _ if position == 0 => (1, "Best match"),
            ResultItem::App(_) => (2, "Apps"),
            ResultItem::Setting(_) => (3, "Settings"),
            ResultItem::Run(_) => (4, "Command"),
        };
        let height = if position == 0 { 64.0 } else { ROW_H };
        let heading_height = if kind != last_kind { 32.0 } else { 0.0 };
        if y + heading_height + height > bottom {
            break;
        }
        if kind != last_kind {
            f.text_left(heading, Rect::new(geo.x + 8.0, y, geo.w - 16.0, 32.0), &ramp("body-strong", theme.text));
            y += 32.0;
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
            f.text_left(&title, Rect::new(text_x, row.y + 8.0, text_w, 22.0), &ramp("body-strong", theme.text));
            f.text_left(subtitle, Rect::new(text_x, row.y + 30.0, text_w, 18.0), &ramp("caption", theme.text_muted));
        } else {
            f.text_left(&title, Rect::new(text_x, row.y, text_w, row.h), &ramp("body", theme.text));
        }
        y += height;
    }
    if st.start.results.is_empty() {
        f.text_left("No results", Rect::new(geo.x + 8.0, y, geo.w - 16.0, 32.0), &ramp("body", theme.text_muted));
    }
    let _ = ButtonKind::Subtle;
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
        assert_eq!(&ids[..5], ["com.boswas.Files", "firefox-esr", "com.boswas.Terminal", "com.boswas.Calculator", "com.boswas.ArchiveManager"]);
        assert_eq!(ids.len(), apps.len(), "the page is filled with the other applications");
        let mut config = ShellConfig::default();
        config.start = Some(vec!["firefox-esr".into(), "not.installed".into()]);
        assert_eq!(pinned_ids(&config, &apps), ["firefox-esr"], "the user's list is used as it is");
        config.start = Some(Vec::new());
        assert!(pinned_ids(&config, &apps).is_empty());
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
        assert!(keys.iter().all(|k| k.chars().all(|c| c.is_ascii_lowercase())));
        keys.sort_unstable();
        keys.dedup();
        assert_eq!(keys.len(), SETTINGS.len());
    }

    #[test]
    fn keys_edit_the_query_and_walk_the_results() {
        let apps = apps();
        let usage = HashMap::new();
        let mut start = Start::default();
        assert_eq!(start.key(Keysym::Down, false, &apps, &usage), KeyOutcome::Ignored, "with no query the grid has the arrows");
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
        start.view = View::AllApps;
        assert_eq!(start.key(Keysym::Escape, false, &apps, &usage), KeyOutcome::Redraw, "then leaves All apps");
        assert_eq!(start.view, View::Home);
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
    fn the_panel_fits_small_outputs_and_pages_scroll() {
        assert_eq!(panel_size((1920.0, 1080.0)), (642.0, 726.0));
        let (w, h) = panel_size((800.0, 600.0));
        assert_eq!(w, 642.0);
        assert_eq!(h, 600.0 - 48.0 - 24.0);
        let geo = geometry(Rect::new(12.0, 12.0, 642.0, 726.0), StartLayout::Default);
        assert_eq!((geo.rows, geo.recommended), (3, 3));
        assert!(geo.footer.bottom() == 738.0 && geo.search.x == 44.0);
        let small = geometry(Rect::new(12.0, 12.0, 642.0, 400.0), StartLayout::Default);
        assert!(small.rows >= 1 && small.recommended == 0 && small.list_rows >= 3);
        // The layouts trade rows of pins for rows of recommendations.
        let pins = geometry(Rect::new(12.0, 12.0, 642.0, 726.0), StartLayout::MorePins);
        assert_eq!((pins.rows, pins.recommended), (4, 1));
        let more = geometry(Rect::new(12.0, 12.0, 642.0, 726.0), StartLayout::MoreRecommendations);
        assert_eq!((more.rows, more.recommended), (2, 4));
        assert_eq!(page_count(20, 4), 1);
        assert_eq!(page_count(0, 3), 1);
        assert_eq!(page_count(18, 3), 1);
        assert_eq!(page_count(19, 3), 2);
        let mut start = Start::default();
        assert!(start.scroll_by(1, 2, 0, 0) && start.page == 1);
        assert!(!start.scroll_by(1, 2, 0, 0), "the last page stays");
        assert!(start.scroll_by(-1, 2, 0, 0) && start.page == 0);
        start.view = View::AllApps;
        assert!(start.scroll_by(1, 1, 40, 10) && start.scroll == 3);
        assert!(start.scroll_by(100, 1, 40, 10) && start.scroll == 30);
        assert!(!start.scroll_by(1, 1, 40, 10));
    }
}
