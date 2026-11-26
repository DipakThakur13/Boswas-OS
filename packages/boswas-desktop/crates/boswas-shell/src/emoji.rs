//! The emoji and symbols panel (Super+. and Super+;), like Windows': emoji
//! by category with their skin tones, kaomoji and symbols, a search by
//! name and keyword, and the recently used ones.
//!
//! The data is `data/emoji.tsv`, made by `tools/build_emoji.py` from
//! Unicode's data (Unicode License v3, `data/README`). Emoji are drawn in
//! colour by the system's emoji font (Noto Color Emoji); without one, the
//! text fonts draw what they can.
//!
//! Picking an item puts it into the focused window: `datacontrol.rs` puts
//! it on the clipboard, has the compositor paste it, and gives the
//! clipboard back what it held before.
//!
//! The recently used items and the chosen skin tone are kept in
//! `$XDG_STATE_HOME/boswas/emoji.json`.

use std::{path::PathBuf, sync::OnceLock};

use boswas_ui::{
    text::{Face, TextStyle},
    tokens::tokens,
};
use serde_json::{json, Value};
use smithay_client_toolkit::seat::keyboard::{KeyEvent, Keysym};

use crate::{
    action::Action,
    state::State,
    ui::{fit_line, ramp, text_scale, Frame, Rect, TextField, Touch},
    Role, Shell,
};

static DATA: &str = include_str!("../data/emoji.tsv");

/// The panel's tabs.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Tab {
    #[default]
    Emoji,
    Kaomoji,
    Symbols,
}

impl Tab {
    pub const ALL: [Tab; 3] = [Tab::Emoji, Tab::Kaomoji, Tab::Symbols];

    pub fn label(self) -> &'static str {
        match self {
            Tab::Emoji => "Emoji",
            Tab::Kaomoji => "Kaomoji",
            Tab::Symbols => "Symbols",
        }
    }
}

/// One emoji, kaomoji or symbol.
#[derive(Debug, PartialEq, Eq)]
pub struct Item {
    pub text: &'static str,
    pub name: &'static str,
    /// Search words, separated by `|`.
    pub keywords: &'static str,
    /// The five skin tones, light to dark, when the emoji has them.
    pub tones: Option<[&'static str; 5]>,
}

#[derive(Debug)]
pub struct Group {
    pub key: &'static str,
    pub label: &'static str,
    pub tab: Tab,
    pub items: Vec<Item>,
}

/// The groups of the data, in their order.
pub fn groups() -> &'static [Group] {
    static GROUPS: OnceLock<Vec<Group>> = OnceLock::new();
    GROUPS.get_or_init(|| parse(DATA))
}

/// Reads the data file. Lines it does not understand are skipped.
pub fn parse(text: &'static str) -> Vec<Group> {
    let mut groups: Vec<Group> = Vec::new();
    for line in text.lines() {
        if line.starts_with('#') || line.is_empty() {
            continue;
        }
        let fields: Vec<&'static str> = line.split('\t').collect();
        if fields[0] == "G" && fields.len() == 4 {
            let tab = match fields[3] {
                "emoji" => Tab::Emoji,
                "kaomoji" => Tab::Kaomoji,
                "symbols" => Tab::Symbols,
                _ => continue,
            };
            groups.push(Group { key: fields[1], label: fields[2], tab, items: Vec::new() });
            continue;
        }
        let (Some(group), 4) = (groups.last_mut(), fields.len()) else {
            continue;
        };
        let tones: Vec<&'static str> = fields[3].split(',').filter(|t| !t.is_empty()).collect();
        group.items.push(Item {
            text: fields[0],
            name: fields[1],
            keywords: fields[2],
            tones: <[&'static str; 5]>::try_from(tones).ok(),
        });
    }
    groups
}

/// The groups of a tab, with their index in [`groups`].
pub fn tab_groups(tab: Tab) -> impl Iterator<Item = (usize, &'static Group)> {
    groups().iter().enumerate().filter(move |(_, g)| g.tab == tab)
}

/// An item by its text (the base text, without a skin tone).
pub fn find(text: &str) -> Option<(Tab, &'static Item)> {
    groups().iter().find_map(|g| g.items.iter().find(|i| i.text == text).map(|i| (g.tab, i)))
}

/// What is inserted for an item at a skin tone (0: none, the yellow one;
/// 1 to 5: light to dark).
pub fn toned(item: &Item, tone: usize) -> &'static str {
    match (tone, item.tones) {
        (1..=5, Some(tones)) => tones[tone - 1],
        _ => item.text,
    }
}

/// The items of a tab whose name or keywords match every word of `query`,
/// best matches first: the name itself, a name that begins with the query, then names with
/// a word that begins with each word, then keywords, then names that only
/// contain it. Typing the character itself finds it too.
pub fn search(tab: Tab, query: &str) -> Vec<&'static Item> {
    let query = query.trim().to_lowercase();
    if query.is_empty() {
        return Vec::new();
    }
    let words: Vec<&str> = query.split_whitespace().collect();
    let mut found: Vec<(u32, usize, &'static Item)> = Vec::new();
    let mut order = 0;
    for (_, group) in tab_groups(tab) {
        for item in &group.items {
            order += 1;
            if item.text == query || item.text.trim_end_matches('\u{fe0f}') == query {
                found.push((0, order, item));
                continue;
            }
            let name = item.name.to_lowercase();
            let mut total = if name == query {
                0
            } else if name.starts_with(&query) {
                1
            } else {
                2
            };
            let mut every = true;
            for word in &words {
                let in_name = name.split(|c: char| !c.is_alphanumeric()).any(|w| w.starts_with(word));
                let score = if in_name {
                    1
                } else if item.keywords.split('|').any(|k| k.split(' ').any(|w| w.starts_with(word))) {
                    2
                } else if name.contains(word) {
                    3
                } else {
                    every = false;
                    break;
                };
                total += score;
            }
            if every {
                found.push((total, order, item));
            }
        }
    }
    found.sort_by_key(|(score, order, _)| (*score, *order));
    found.into_iter().take(MAX_RESULTS).map(|(_, _, item)| item).collect()
}

/// Most search results shown.
pub const MAX_RESULTS: usize = 240;
/// Most recently used items kept.
pub const MAX_RECENT: usize = 40;
/// The group index that means "Most recently used".
pub const RECENT: usize = usize::MAX;

/// The recently used items (their base text, newest first) and the skin
/// tone, as kept in the state file.
#[derive(Debug, Default)]
pub struct Recent {
    pub items: Vec<String>,
    pub tone: usize,
    path: Option<PathBuf>,
}

impl Recent {
    pub fn load() -> Self {
        let path = crate::config::state_dir().map(|dir| dir.join("emoji.json"));
        let mut recent = path.as_deref().map(Self::read).unwrap_or_default();
        recent.path = path;
        recent
    }

    /// The file's contents, with anything that is not an item of the data
    /// left out.
    pub fn read(path: &std::path::Path) -> Self {
        use std::io::Read;
        let mut text = String::new();
        let read = std::fs::File::open(path).and_then(|file| file.take(16 * 1024).read_to_string(&mut text));
        if read.is_err() {
            return Self::default();
        }
        Self::parse(&text)
    }

    pub fn parse(text: &str) -> Self {
        let value: Value = serde_json::from_str(text).unwrap_or(Value::Null);
        let mut items: Vec<String> = Vec::new();
        for entry in value.get("recent").and_then(Value::as_array).into_iter().flatten() {
            if let Some(text) = entry.as_str().filter(|t| find(t).is_some() && !items.iter().any(|i| i == t)) {
                items.push(text.to_owned());
            }
            if items.len() >= MAX_RECENT {
                break;
            }
        }
        let tone = value.get("tone").and_then(Value::as_u64).filter(|t| *t <= 5).unwrap_or(0) as usize;
        Self { items, tone, path: None }
    }

    /// An item was used: it goes to the front.
    pub fn used(&mut self, text: &str) {
        self.items.retain(|i| i != text);
        self.items.insert(0, text.to_owned());
        self.items.truncate(MAX_RECENT);
        self.save();
    }

    pub fn set_tone(&mut self, tone: usize) {
        self.tone = tone.min(5);
        self.save();
    }

    fn save(&self) {
        let Some(path) = &self.path else {
            return;
        };
        let text = json!({ "version": 1, "recent": self.items, "tone": self.tone }).to_string();
        if let Err(err) = crate::clipboard::write_private(path, text.as_bytes()) {
            tracing::warn!("could not keep the recently used emoji in {}: {err}", path.display());
        }
    }

    /// The recently used items of a tab.
    pub fn of(&self, tab: Tab) -> Vec<&'static Item> {
        self.items.iter().filter_map(|t| find(t)).filter(|(t, _)| *t == tab).map(|(_, item)| item).collect()
    }
}

/// What the panel shows.
#[derive(Default)]
pub struct Panel {
    pub tab: Tab,
    /// The group shown: an index into [`groups`], or [`RECENT`].
    pub group: usize,
    pub search: TextField,
    /// The first row shown.
    pub scroll: usize,
    /// The skin tones are offered.
    pub tones_open: bool,
    pub recent: Recent,
}

impl Panel {
    pub fn new() -> Self {
        Self { recent: Recent::load(), ..Self::default() }
    }

    /// As the panel opens: the tab used last, at its recently used items
    /// (or its first group).
    pub fn reset(&mut self) {
        self.search.clear();
        self.scroll = 0;
        self.tones_open = false;
        self.show_tab(self.tab);
    }

    pub fn show_tab(&mut self, tab: Tab) {
        self.tab = tab;
        self.scroll = 0;
        self.group = if self.recent.of(tab).is_empty() { tab_groups(tab).next().map_or(0, |(i, _)| i) } else { RECENT };
    }

    pub fn searching(&self) -> bool {
        !self.search.text().trim().is_empty()
    }

    /// The items shown now.
    pub fn items(&self) -> Vec<&'static Item> {
        if self.searching() {
            return search(self.tab, self.search.text());
        }
        if self.group == RECENT {
            return self.recent.of(self.tab);
        }
        groups().get(self.group).filter(|g| g.tab == self.tab).map(|g| g.items.iter().collect()).unwrap_or_default()
    }

    /// The heading over the items.
    pub fn title(&self) -> String {
        if self.searching() {
            return format!("Results for \u{201c}{}\u{201d}", self.search.text().trim());
        }
        if self.group == RECENT {
            return "Most recently used".into();
        }
        groups().get(self.group).map_or(String::new(), |g| g.label.to_owned())
    }

    /// The categories at the bottom: recently used, then the tab's groups.
    pub fn categories(&self) -> Vec<usize> {
        std::iter::once(RECENT).chain(tab_groups(self.tab).map(|(i, _)| i)).collect()
    }
}

/// The small mark of a category on the bar at the bottom.
fn category_mark(key: &str) -> &'static str {
    match key {
        "smileys" => "\u{1F600}",
        "people" => "\u{1F9D1}",
        "animals" => "\u{1F43B}",
        "food" => "\u{1F354}",
        "travel" => "\u{1F697}",
        "activities" => "\u{26BD}",
        "objects" => "\u{1F4A1}",
        "symbols" => "\u{1F523}",
        "flags" => "\u{1F3C1}",
        "kaomoji-classic" => ":-)",
        "kaomoji-happy" => "^\u{203f}^",
        "kaomoji-greeting" => "\u{30ce}",
        "kaomoji-love" => "\u{2661}",
        "kaomoji-sad" => "T_T",
        "kaomoji-angry" => "\u{ca0}_\u{ca0}",
        "kaomoji-surprised" => "\u{b0}o\u{b0}",
        "kaomoji-other" => "\u{30c4}",
        "symbols-punctuation" => "\u{a7}",
        "symbols-currency" => "\u{20ac}",
        "symbols-latin" => "\u{c7}",
        "symbols-greek" => "\u{3a9}",
        "symbols-math" => "\u{2211}",
        "symbols-arrows" => "\u{2192}",
        "symbols-geometric" => "\u{25a0}",
        _ => "\u{2605}",
    }
}

/// What a control of the panel does.
#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    Tab(Tab),
    /// A category of the bar ([`RECENT`] or a group index).
    Group(usize),
    /// The nth item shown.
    Item(usize),
    /// Opens or closes the skin tones.
    Tones,
    Tone(usize),
    Search,
}

/// A colour for measuring line heights (the colour plays no part).
const WHITE: boswas_ui::tokens::Rgba = boswas_ui::tokens::Rgba::WHITE;

/// The panel's width and its height without larger text.
const WIDTH: f32 = 384.0;
const HEIGHT: f32 = 468.0;
const PAD: f32 = 12.0;
/// An emoji's or symbol's cell, and the size it is drawn at.
const CELL: f32 = 44.0;
const GLYPH: f32 = 26.0;
const BAR: f32 = 44.0;

/// The panel's size on an output (logical pixels, without its shadow).
pub fn panel_size(output: (f32, f32)) -> (f32, f32) {
    let t = tokens();
    let room = output.1 - t.shell("taskbar-height") - 2.0 * t.shell("flyout-margin");
    let grow = (text_scale() - 1.0) * 64.0;
    (WIDTH.min(output.0 - 24.0), (HEIGHT + grow).min(room).max(260.0))
}

/// Where everything is.
#[derive(Clone, Debug, PartialEq)]
pub struct Layout {
    pub tabs: Vec<Rect>,
    pub search: Rect,
    pub tone: Option<Rect>,
    pub title: Rect,
    pub grid: Rect,
    pub bar: Rect,
    pub cols: usize,
    pub cell: (f32, f32),
    /// Whole rows that fit.
    pub rows: usize,
}

pub fn layout(st: &State, panel: Rect) -> Layout {
    let inner = panel.inset(PAD);
    let tab_h = ramp("body", WHITE).line_height().max(20.0) + 16.0;
    let tab_w = inner.w / 3.0;
    let tabs = (0..3).map(|i| Rect::new(inner.x + i as f32 * tab_w, inner.y, tab_w, tab_h)).collect();
    let field_h = (ramp("body", WHITE).line_height() + 12.0).max(32.0);
    let tone = (st.emoji.tab == Tab::Emoji).then(|| Rect::new(inner.right() - field_h, inner.y + tab_h + 8.0, field_h, field_h));
    let search_w = inner.w - tone.map_or(0.0, |t| t.w + 8.0);
    let search = Rect::new(inner.x, inner.y + tab_h + 8.0, search_w, field_h);
    let title_h = ramp("body-strong", WHITE).line_height().max(CELL * 0.6);
    let title = Rect::new(inner.x, search.bottom() + 8.0, inner.w, title_h);
    let bar = Rect::new(inner.x, inner.bottom() - BAR, inner.w, BAR);
    let grid = Rect::new(inner.x, title.bottom() + 4.0, inner.w, (bar.y - 6.0 - (title.bottom() + 4.0)).max(CELL));
    let (cols, cell) = match st.emoji.tab {
        Tab::Kaomoji => {
            let cols = if text_scale() > 1.5 { 1 } else { 2 };
            let h = ramp("body", WHITE).line_height() + 18.0;
            (cols, (grid.w / cols as f32, h))
        }
        _ => {
            let cols = ((grid.w / CELL).floor() as usize).max(1);
            (cols, (grid.w / cols as f32, CELL))
        }
    };
    let rows = ((grid.h / cell.1).floor() as usize).max(1);
    Layout { tabs, search, tone, title, grid, bar, cols, cell, rows }
}

/// The tone swatch: a raised hand in a skin tone.
fn swatch(tone: usize) -> String {
    const HAND: &str = "\u{270B}";
    const TONES: [&str; 5] = ["\u{1F3FB}", "\u{1F3FC}", "\u{1F3FD}", "\u{1F3FE}", "\u{1F3FF}"];
    match tone {
        1..=5 => format!("{HAND}{}", TONES[tone - 1]),
        _ => HAND.to_owned(),
    }
}

fn emoji_style(size: f32) -> TextStyle {
    TextStyle::new(size, boswas_ui::tokens::Rgba::WHITE).face(Face::Emoji)
}

pub fn draw(f: &mut Frame, st: &State, panel: Rect) {
    f.panel(panel, "acrylic");
    f.area(panel, Action::None);
    let theme = f.theme();
    let lay = layout(st, panel);
    let panel_state = &st.emoji;

    // The tabs.
    for (tab, rect) in Tab::ALL.iter().zip(&lay.tabs) {
        let selected = panel_state.tab == *tab;
        let touch = f.hit(*rect, Action::Emoji(Act::Tab(*tab)));
        f.state_layer(rect.inset(2.0), tokens().radius_control, touch, false);
        let style = if selected { ramp("body-strong", theme.text) } else { ramp("body", theme.text_secondary) };
        f.text_centered(tab.label(), Rect::new(rect.x, rect.y, rect.w, rect.h - 4.0), &style);
        if selected {
            f.canvas.fill_rounded_rect(rect.center().0 - 8.0, rect.bottom() - 4.0, 16.0, 3.0, 1.5, theme.accent);
        }
    }

    // The search box (it always has the typing).
    f.area(lay.search, Action::Emoji(Act::Search));
    let placeholder = match panel_state.tab {
        Tab::Emoji => "Search emoji",
        Tab::Kaomoji => "Search kaomoji",
        Tab::Symbols => "Search symbols",
    };
    f.text_box(lay.search, &panel_state.search, placeholder, true, false, 34.0, 8.0);
    let icon = 16.0;
    f.icon_tinted("edit-find-symbolic", lay.search.x + 10.0, lay.search.y + (lay.search.h - icon) / 2.0, icon, theme.text_secondary);

    // The skin tone button.
    if let Some(rect) = lay.tone {
        let touch = f.hit(rect, Action::Emoji(Act::Tones));
        f.tip("Skin tone");
        let fill = f.res.fill(if touch.hovered || panel_state.tones_open { "control-hover" } else { "control" });
        let stroke = f.res.stroke("control");
        f.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, tokens().radius_control, fill);
        f.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, tokens().radius_control, 1.0, stroke);
        if touch.focused {
            f.focus_ring(rect, tokens().radius_control);
        }
        draw_glyph(f, &swatch(panel_state.recent.tone), rect, 18.0);
    }

    // The heading, or the skin tones to choose from.
    if panel_state.tones_open && panel_state.tab == Tab::Emoji {
        let size = lay.title.h.min(36.0);
        for tone in 0..6 {
            let rect = Rect::new(lay.title.x + tone as f32 * (size + 6.0), lay.title.y, size, size);
            let touch = f.hit(rect, Action::Emoji(Act::Tone(tone)));
            f.state_layer(rect, tokens().radius_control, touch, panel_state.recent.tone == tone);
            if panel_state.recent.tone == tone {
                f.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, tokens().radius_control, 1.5, theme.accent);
            }
            draw_glyph(f, &swatch(tone), rect, (size * 0.6).min(22.0));
        }
    } else {
        let title = panel_state.title();
        f.text_left(&title, lay.title, &ramp("body-strong", theme.text));
    }

    // The items.
    let items = panel_state.items();
    let tone = panel_state.recent.tone;
    if items.is_empty() {
        let message = if panel_state.searching() {
            "Nothing found. Try another word."
        } else {
            "The ones you use will show up here."
        };
        f.text_wrapped(message, lay.grid.x + 4.0, lay.grid.y + 8.0, lay.grid.w - 8.0, 3, &ramp("body", theme.text_secondary));
    }
    let first = panel_state.scroll * lay.cols;
    let last = (first + lay.rows * lay.cols).min(items.len());
    for (index, item) in items.iter().enumerate().take(last).skip(first) {
        let slot = index - first;
        let rect = Rect::new(
            lay.grid.x + (slot % lay.cols) as f32 * lay.cell.0,
            lay.grid.y + (slot / lay.cols) as f32 * lay.cell.1,
            lay.cell.0,
            lay.cell.1,
        );
        let touch = f.hit(rect.inset(1.0), Action::Emoji(Act::Item(index)));
        f.tip(item.name);
        cell(f, rect.inset(1.0), touch);
        match panel_state.tab {
            Tab::Emoji => draw_glyph(f, toned(item, tone), rect, GLYPH),
            Tab::Symbols => {
                let style = fit_line(&TextStyle::new(22.0, theme.text), rect.h);
                f.text_centered(item.text, rect, &style);
            }
            Tab::Kaomoji => {
                let style = ramp("body", theme.text);
                f.text_centered(item.text, rect.inset(6.0), &style);
            }
        }
    }
    let total_rows = items.len().div_ceil(lay.cols);
    if total_rows > lay.rows {
        let content = total_rows as f32 * lay.cell.1;
        f.scrollbar(Rect::new(lay.grid.x, lay.grid.y, lay.grid.w + PAD * 0.75, lay.rows as f32 * lay.cell.1), content, panel_state.scroll as f32 * lay.cell.1);
    }

    // The categories.
    f.divider(lay.bar.x, lay.bar.y - 2.0, lay.bar.w);
    let categories = panel_state.categories();
    let width = lay.bar.w / categories.len() as f32;
    for (slot, group) in categories.iter().enumerate() {
        let rect = Rect::new(lay.bar.x + slot as f32 * width, lay.bar.y + 4.0, width, lay.bar.h - 4.0);
        let touch = f.hit(rect.inset(2.0), Action::Emoji(Act::Group(*group)));
        let (label, mark) = if *group == RECENT {
            ("Most recently used", None)
        } else {
            groups().get(*group).map_or(("", None), |g| (g.label, Some(category_mark(g.key))))
        };
        f.tip(label);
        let selected = !panel_state.searching() && panel_state.group == *group;
        f.state_layer(rect.inset(2.0), tokens().radius_control, touch, false);
        match mark {
            None => {
                let size = 18.0;
                let (cx, cy) = rect.center();
                f.icon_tinted("document-open-recent-symbolic", cx - size / 2.0, cy - size / 2.0 - 2.0, size, theme.text);
            }
            Some(mark) if panel_state.tab == Tab::Emoji => draw_glyph(f, mark, Rect::new(rect.x, rect.y, rect.w, rect.h - 4.0), 20.0),
            Some(mark) => {
                let style = fit_line(&TextStyle::new(14.0, theme.text), rect.h - 8.0);
                f.text_centered(mark, Rect::new(rect.x, rect.y, rect.w, rect.h - 4.0), &style);
            }
        }
        if selected {
            f.canvas.fill_rounded_rect(rect.center().0 - 8.0, rect.bottom() - 4.0, 16.0, 3.0, 1.5, theme.accent);
        }
    }
}

/// An item's cell: the hover, pressed and focus look.
fn cell(f: &mut Frame, rect: Rect, touch: Touch) {
    f.state_layer(rect, tokens().radius_control, touch, false);
}

/// An emoji, centred in `rect` at `size`.
fn draw_glyph(f: &mut Frame, text: &str, rect: Rect, size: f32) {
    let style = emoji_style(size);
    let (w, h) = f.res.text.measure(text, &style);
    f.text(text, rect.x + (rect.w - w) / 2.0, rect.y + (rect.h - h) / 2.0, None, &style);
}

impl Shell {
    /// The panel's layout as the open flyout has it.
    fn emoji_layout(&self) -> Option<Layout> {
        let surface = self.surfaces.iter().find(|s| s.role == Role::Flyout)?;
        let (w, h) = (surface.width as f32, surface.height as f32);
        let panel = Rect::new(crate::PAD, crate::PAD, w - 2.0 * crate::PAD, h - 2.0 * crate::PAD);
        Some(layout(&self.st, panel))
    }

    pub fn emoji_action(&mut self, act: Act) {
        match act {
            Act::Tab(tab) => {
                self.st.emoji.search.clear();
                self.st.emoji.tones_open = false;
                self.st.emoji.show_tab(tab);
                self.st.focus = None;
            }
            Act::Group(group) => {
                self.st.emoji.search.clear();
                self.st.emoji.group = group;
                self.st.emoji.scroll = 0;
                self.st.focus = None;
            }
            Act::Item(index) => return self.emoji_insert(index),
            Act::Tones => self.st.emoji.tones_open = !self.st.emoji.tones_open,
            Act::Tone(tone) => {
                self.st.emoji.recent.set_tone(tone);
                self.st.emoji.tones_open = false;
            }
            Act::Search => self.st.focus = None,
        }
        self.mark(Role::Flyout);
    }

    /// Puts an item into the focused window and closes the panel.
    fn emoji_insert(&mut self, index: usize) {
        let items = self.st.emoji.items();
        let Some(item) = items.get(index) else {
            return;
        };
        let text = toned(item, self.st.emoji.recent.tone).to_owned();
        self.st.emoji.recent.used(item.text);
        // The panel goes at once, so the window has the keyboard again.
        self.drop_overlays();
        self.insert_text(&text);
    }

    pub fn emoji_scroll(&mut self, steps: i32) {
        let Some(lay) = self.emoji_layout() else {
            return;
        };
        let rows = self.st.emoji.items().len().div_ceil(lay.cols);
        let max = rows.saturating_sub(lay.rows);
        let scroll = (self.st.emoji.scroll as i64 + i64::from(steps) * 2).clamp(0, max as i64) as usize;
        if scroll != self.st.emoji.scroll {
            self.st.emoji.scroll = scroll;
            self.mark(Role::Flyout);
        }
    }

    pub fn emoji_key(&mut self, event: &KeyEvent, typed: Option<&str>) {
        let Some(lay) = self.emoji_layout() else {
            return;
        };
        let count = self.st.emoji.items().len();
        let focused = match &self.st.focus {
            Some(Action::Emoji(Act::Item(index))) if *index < count => Some(*index),
            _ => None,
        };
        let shift = self.modifiers.shift;
        let move_to = |delta: i64| -> Option<usize> {
            if count == 0 {
                return None;
            }
            Some(match focused {
                None => 0,
                Some(index) => (index as i64 + delta).clamp(0, count as i64 - 1) as usize,
            })
        };
        let cols = lay.cols as i64;
        let target = match event.keysym {
            Keysym::Escape => {
                if self.st.emoji.searching() {
                    self.st.emoji.search.clear();
                    self.st.emoji.scroll = 0;
                    self.st.focus = None;
                    return self.mark(Role::Flyout);
                }
                return self.close_flyout();
            }
            Keysym::Return | Keysym::KP_Enter => {
                if let Some(index) = focused.or((count > 0 && self.st.emoji.searching()).then_some(0)) {
                    self.emoji_insert(index);
                }
                return;
            }
            Keysym::Left | Keysym::KP_Left => move_to(-1),
            Keysym::Right | Keysym::KP_Right => move_to(1),
            Keysym::Up | Keysym::KP_Up => move_to(-cols),
            Keysym::Down | Keysym::KP_Down => move_to(cols),
            Keysym::Page_Up | Keysym::KP_Page_Up => move_to(-(cols * lay.rows as i64)),
            Keysym::Page_Down | Keysym::KP_Page_Down => move_to(cols * lay.rows as i64),
            Keysym::Tab | Keysym::ISO_Left_Tab => {
                // The next or previous category.
                let categories = self.st.emoji.categories();
                let at = categories.iter().position(|g| *g == self.st.emoji.group).unwrap_or(0);
                let step = if shift || event.keysym == Keysym::ISO_Left_Tab { categories.len() - 1 } else { 1 };
                let group = categories[(at + step) % categories.len()];
                return self.emoji_action(Act::Group(group));
            }
            Keysym::BackSpace => {
                if self.st.emoji.search.backspace() {
                    self.st.emoji.scroll = 0;
                    self.st.focus = None;
                    self.mark(Role::Flyout);
                }
                return;
            }
            _ => {
                if let Some(text) = typed.filter(|t| !t.chars().all(char::is_control)) {
                    if self.st.emoji.search.insert(text) {
                        self.st.emoji.scroll = 0;
                        self.st.emoji.tones_open = false;
                        self.st.focus = None;
                        self.mark(Role::Flyout);
                    }
                } else if self.st.emoji.search.key(event.keysym, self.modifiers.ctrl) {
                    self.mark(Role::Flyout);
                }
                return;
            }
        };
        if let Some(index) = target {
            // Scrolled so the focused item is in view.
            let row = index / lay.cols;
            if row < self.st.emoji.scroll {
                self.st.emoji.scroll = row;
            } else if row >= self.st.emoji.scroll + lay.rows {
                self.st.emoji.scroll = row + 1 - lay.rows;
            }
            self.st.focus = Some(Action::Emoji(Act::Item(index)));
            self.mark(Role::Flyout);
        }
    }

    /// Prepares the panel as it opens.
    pub fn open_emoji(&mut self) {
        self.st.emoji.reset();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::testing;
    use boswas_ui::Scheme;

    #[test]
    fn the_data_has_every_group_with_names_keywords_and_skin_tones() {
        let all = groups();
        let keys: Vec<&str> = all.iter().filter(|g| g.tab == Tab::Emoji).map(|g| g.key).collect();
        assert_eq!(keys, ["smileys", "people", "animals", "food", "travel", "activities", "objects", "symbols", "flags"]);
        assert!(all.iter().all(|g| !g.items.is_empty()), "no empty group");
        assert!(all.iter().filter(|g| g.tab == Tab::Kaomoji).count() >= 6);
        assert!(all.iter().filter(|g| g.tab == Tab::Symbols).count() >= 6);
        let emoji: usize = all.iter().filter(|g| g.tab == Tab::Emoji).map(|g| g.items.len()).sum();
        assert!(emoji > 1800, "{emoji} emoji");
        let wave = find("\u{1F44B}").unwrap().1;
        assert_eq!(wave.name, "waving hand");
        assert!(wave.keywords.split('|').any(|k| k == "wave"));
        assert_eq!(toned(wave, 0), "\u{1F44B}");
        assert_eq!(toned(wave, 1), "\u{1F44B}\u{1F3FB}");
        assert_eq!(toned(wave, 5), "\u{1F44B}\u{1F3FF}");
        let grin = find("\u{1F600}").unwrap().1;
        assert_eq!((grin.tones, toned(grin, 3)), (None, "\u{1F600}"), "no tones: always the same");
        // No skin-tone variant is an item of its own, and every item has a name.
        for group in all {
            for item in &group.items {
                assert!(!item.name.contains("skin tone"), "{}", item.name);
                assert!(!item.text.is_empty() && !item.name.is_empty());
            }
        }
        assert!(find("\u{1F3FB}").is_none(), "the skin tone swatches are not items");
    }

    #[test]
    fn the_file_s_mistakes_are_skipped() {
        let groups = parse("# comment\nnot a record\nG\tg\tGroup\temoji\nx\tname\t\t\nshort\nG\tbad\tBad\tnone\ny\tother\tk\ta,b\n");
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].items.len(), 2, "the item after an unknown tab joins the last group");
        assert_eq!(groups[0].items[1].tones, None, "two tones are not five");
    }

    #[test]
    fn search_finds_by_name_and_keyword_best_first() {
        let found = search(Tab::Emoji, "grinning");
        assert_eq!(found[0].text, "\u{1F600}", "{:?}", found.iter().take(3).collect::<Vec<_>>());
        let wave = search(Tab::Emoji, "wave");
        assert!(wave.iter().take(5).any(|i| i.text == "\u{1F44B}"));
        assert!(search(Tab::Emoji, "red heart").iter().take(3).any(|i| i.name == "red heart"));
        assert!(search(Tab::Emoji, "india").iter().any(|i| i.name == "flag: India"));
        assert!(search(Tab::Emoji, "SMILE").len() > 3, "without regard to case");
        assert!(search(Tab::Emoji, "zzzqqq").is_empty());
        assert!(search(Tab::Emoji, "  ").is_empty());
        assert_eq!(search(Tab::Symbols, "euro")[0].text, "\u{20ac}");
        assert_eq!(search(Tab::Symbols, "\u{20ac}")[0].text, "\u{20ac}", "the character itself");
        assert!(search(Tab::Kaomoji, "shrug").iter().any(|i| i.text == "\u{af}\\_(\u{30c4})_/\u{af}"));
        assert!(search(Tab::Symbols, "a").len() <= MAX_RESULTS);
    }

    #[test]
    fn recently_used_items_are_kept_newest_first_with_the_skin_tone() {
        let mut recent = Recent::parse(r#"{"recent": ["😀", "not an emoji", "😀", "€", 5], "tone": 3}"#);
        assert_eq!(recent.items, ["\u{1F600}", "\u{20ac}"]);
        assert_eq!(recent.tone, 3);
        recent.used("\u{1F44B}");
        recent.used("\u{1F600}");
        assert_eq!(recent.items[..2], ["\u{1F600}", "\u{1F44B}"]);
        assert_eq!(recent.of(Tab::Emoji).len(), 2);
        assert_eq!(recent.of(Tab::Symbols)[0].text, "\u{20ac}");
        for _ in 0..60 {
            for group in groups().iter().take(1) {
                for item in group.items.iter().take(50) {
                    recent.used(item.text);
                }
            }
        }
        assert_eq!(recent.items.len(), MAX_RECENT);
        assert_eq!(Recent::parse("garbage").items.len(), 0);
        assert_eq!(Recent::parse(r#"{"tone": 9}"#).tone, 0);
    }

    #[test]
    fn the_panel_opens_at_the_recent_items_or_the_first_group() {
        let mut panel = Panel::default();
        panel.reset();
        assert_eq!((panel.tab, panel.group), (Tab::Emoji, 0));
        assert_eq!(panel.title(), "Smileys & emotion");
        panel.recent.items = vec!["\u{1F600}".into()];
        panel.reset();
        assert_eq!(panel.group, RECENT);
        assert_eq!(panel.items().len(), 1);
        panel.show_tab(Tab::Symbols);
        assert_ne!(panel.group, RECENT, "nothing recent among the symbols");
        assert_eq!(panel.items()[0].tones, None);
        panel.search.insert("arrow");
        assert!(panel.title().starts_with("Results for"));
        assert!(panel.items().iter().all(|i| i.name.contains("arrow") || i.keywords.contains("arrow")));
        assert_eq!(panel.categories()[0], RECENT);
    }

    fn panel_state(tab: Tab) -> State {
        let mut st = State::for_tests();
        st.emoji = Panel::default();
        st.emoji.tab = tab;
        st.emoji.reset();
        st
    }

    #[test]
    fn the_panel_draws_its_controls_inside_at_every_text_size() {
        for text in [100, 175, 225] {
            crate::ui::set_text_scale(text);
            for tab in Tab::ALL {
                let mut st = panel_state(tab);
                st.emoji.tones_open = tab == Tab::Emoji && text == 175;
                let (w, h) = panel_size((1366.0, 768.0));
                let (canvas, hits) = testing::render(w + 24.0, h + 24.0, 1.0, Scheme::Dark, None, None, |f| draw(f, &st, Rect::new(12.0, 12.0, w, h)));
                assert!(testing::hits_are_inside(&hits, w + 24.0, h + 24.0), "{tab:?} at {text} %");
                assert!(hits.iter().any(|h| matches!(h.action, Action::Emoji(Act::Item(_)))), "{tab:?}: items");
                assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0));
                let lay = layout(&st, Rect::new(12.0, 12.0, w, h));
                assert!(lay.grid.bottom() <= lay.bar.y && lay.title.bottom() <= lay.grid.y, "{lay:?}");
            }
        }
        crate::ui::set_text_scale(100);
    }
}
