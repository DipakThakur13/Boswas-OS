//! The widgets board: a large flyout from the left edge above the taskbar
//! with cards that show what the user's own applications keep (the
//! weather of Weather's summary, the next events of Calendar, the world
//! clocks and the next alarm of Clock, the newest picture in Pictures),
//! the system (processor, memory, disk, battery) and what is playing.
//! Every card opens its application. Nothing is fetched from the network
//! and nothing is shown that the user did not put there
//! (`docs/security/privacy.md`).
//!
//! It opens with Super+W and with the widgets button at the left end of
//! the taskbar (at the right, next to the tray, when the taskbar's group
//! sits at the left), which shows the current temperature and the weather
//! icon while there is a summary, else a plain widgets mark. The cards and
//! their sizes are the user's: `[widgets]` of `~/.config/boswas/shell.toml`.
//!
//! Files are read on a worker thread when the board opens and every half
//! minute while it is open; the processor and memory are sampled from
//! `/proc` about once a second only while it is open.

use std::{
    path::PathBuf,
    rc::Rc,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

use boswas_ui::{tokens::tokens, Rgba};
use chrono::Timelike;
use smithay_client_toolkit::{
    reexports::calloop::channel::{self, Sender},
    seat::keyboard::{KeyEvent, Keysym},
};
use tiny_skia::Pixmap;

use crate::{
    action::Action,
    agenda::{self, Occurrence},
    glyph::Glyph,
    menu::{Cmd, Item},
    state::{Flyout, State},
    tzif::{self, Zone},
    ui::{self, fit_line, ramp, text_scale, Frame, Hit, Nav, Rect},
    weather,
    widget_sources::{self as sources, ClockData, CpuTimes},
    Role, Shell, PAD,
};

/// A kind of card.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Kind {
    Weather,
    Calendar,
    Clock,
    System,
    Media,
    Photos,
}

impl Kind {
    pub const ALL: [Kind; 6] = [Kind::Weather, Kind::Calendar, Kind::Clock, Kind::System, Kind::Media, Kind::Photos];

    pub fn key(self) -> &'static str {
        match self {
            Kind::Weather => "weather",
            Kind::Calendar => "calendar",
            Kind::Clock => "clock",
            Kind::System => "system",
            Kind::Media => "media",
            Kind::Photos => "photos",
        }
    }

    pub fn from_key(key: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|k| k.key() == key)
    }

    pub fn title(self) -> &'static str {
        match self {
            Kind::Weather => "Weather",
            Kind::Calendar => "Calendar",
            Kind::Clock => "Clock",
            Kind::System => "System",
            Kind::Media => "Media",
            Kind::Photos => "Photos",
        }
    }

    /// The application a click opens (a desktop file id).
    pub fn app_id(self) -> &'static str {
        match self {
            Kind::Weather => "com.boswas.Weather",
            Kind::Calendar => "com.boswas.Calendar",
            Kind::Clock => "com.boswas.Clock",
            Kind::System => "com.boswas.TaskManager",
            Kind::Media => "com.boswas.MediaPlayer",
            Kind::Photos => "com.boswas.Photos",
        }
    }

    fn icon(self) -> &'static str {
        match self {
            Kind::Weather => "boswas-weather",
            Kind::Calendar => "boswas-calendar",
            Kind::Clock => "boswas-clock",
            Kind::System => "boswas-task-manager",
            Kind::Media => "boswas-media-player",
            Kind::Photos => "boswas-photos",
        }
    }

    /// The icon names to try for the card's mark (the application's own
    /// icon name first).
    fn icons(self, st: &State) -> Vec<String> {
        let mut names = Vec::new();
        if let Some(app) = st.app_index(self.app_id()).and_then(|i| st.apps.get(i)) {
            names.push(app.icon.clone());
        }
        names.push(self.icon().to_owned());
        names
    }
}

/// How tall a card is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Size {
    Small,
    Medium,
}

/// The cards on the board and their sizes, in order: what `[widgets]` of
/// `shell.toml` keeps.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Arrangement {
    pub cards: Vec<(Kind, Size)>,
}

impl Default for Arrangement {
    fn default() -> Self {
        Self {
            cards: vec![
                (Kind::Weather, Size::Medium),
                (Kind::Calendar, Size::Medium),
                (Kind::Clock, Size::Small),
                (Kind::System, Size::Medium),
                (Kind::Media, Size::Small),
                (Kind::Photos, Size::Medium),
            ],
        }
    }
}

impl Arrangement {
    /// Reads `[widgets]`: `cards` (the order, each kind once) and `small`
    /// (the kinds shown small). Unknown names are left out; without
    /// `cards` the default board applies.
    pub fn from_table(table: &toml::Table) -> Option<Self> {
        let cards = table.get("cards")?.as_array()?;
        let small: Vec<&str> = table.get("small").and_then(|v| v.as_array()).map(|list| list.iter().filter_map(|v| v.as_str()).collect()).unwrap_or_default();
        let mut out: Vec<(Kind, Size)> = Vec::new();
        for kind in cards.iter().filter_map(|v| v.as_str()).filter_map(Kind::from_key) {
            if !out.iter().any(|(k, _)| *k == kind) {
                out.push((kind, if small.contains(&kind.key()) { Size::Small } else { Size::Medium }));
            }
        }
        Some(Self { cards: out })
    }

    /// The `[widgets]` table as TOML text.
    pub fn to_toml(&self) -> String {
        let quoted = |kinds: Vec<Kind>| kinds.iter().map(|k| format!("\"{}\"", k.key())).collect::<Vec<_>>().join(", ");
        let cards = quoted(self.cards.iter().map(|(k, _)| *k).collect());
        let small = quoted(self.cards.iter().filter(|(_, s)| *s == Size::Small).map(|(k, _)| *k).collect());
        format!("[widgets]\ncards = [{cards}]\nsmall = [{small}]\n")
    }

    pub fn has(&self, kind: Kind) -> bool {
        self.cards.iter().any(|(k, _)| *k == kind)
    }

    /// Changes the board; returns whether anything changed.
    pub fn apply(&mut self, cmd: WidgetCmd) -> bool {
        let position = |cards: &[(Kind, Size)], kind: Kind| cards.iter().position(|(k, _)| *k == kind);
        match cmd {
            WidgetCmd::Add(kind) => {
                if self.has(kind) {
                    return false;
                }
                self.cards.push((kind, Size::Medium));
            }
            WidgetCmd::Remove(kind) => {
                let Some(at) = position(&self.cards, kind) else {
                    return false;
                };
                self.cards.remove(at);
            }
            WidgetCmd::Up(kind) => match position(&self.cards, kind) {
                Some(at) if at > 0 => self.cards.swap(at, at - 1),
                _ => return false,
            },
            WidgetCmd::Down(kind) => match position(&self.cards, kind) {
                Some(at) if at + 1 < self.cards.len() => self.cards.swap(at, at + 1),
                _ => return false,
            },
            WidgetCmd::Resize(kind, size) => match position(&self.cards, kind) {
                Some(at) if self.cards[at].1 != size => self.cards[at].1 = size,
                _ => return false,
            },
            WidgetCmd::Open(_) => return false,
        }
        true
    }
}

/// What a menu entry of the board does.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WidgetCmd {
    Add(Kind),
    Remove(Kind),
    Up(Kind),
    Down(Kind),
    Resize(Kind, Size),
    Open(Kind),
}

/// A control of the board or of the taskbar's widgets button.
#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    /// A card: opens its application.
    Open(Kind),
    /// The card's menu button.
    Menu(Kind),
    /// "Add widgets".
    Add,
    MediaPrevious,
    MediaPlayPause,
    MediaNext,
}

/// The world clock of the Clock card at a moment.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ClockView {
    pub data: ClockData,
    /// The zones of the world clock, read by the worker.
    pub zones: Vec<(String, Zone)>,
    pub local: Option<Zone>,
}

/// What the worker found in the applications' files.
pub struct Sources {
    pub agenda: Vec<Occurrence>,
    /// Calendar colours by calendar id.
    pub colors: Vec<(String, String)>,
    pub clock: ClockView,
    /// The newest picture, its modification time and (when it is new) its
    /// thumbnail.
    pub picture: Option<(PathBuf, std::time::SystemTime, Option<Pixmap>)>,
}

/// A message from the board's worker threads.
pub enum Msg {
    Sources(Box<Sources>),
    Sample(sources::Sample),
}

/// The board's state.
#[derive(Default)]
pub struct Board {
    /// Weather's summary, followed for the taskbar button and the card.
    pub weather: weather::Watch,
    pub agenda: Option<Vec<Occurrence>>,
    pub colors: Vec<(String, String)>,
    pub clock: ClockView,
    pub picture: Option<(PathBuf, std::time::SystemTime, Rc<Pixmap>)>,
    pub cpu: Option<f32>,
    last_cpu: Option<CpuTimes>,
    pub memory: Option<(u64, u64)>,
    pub disk: Option<(u64, u64)>,
    /// How far the cards are scrolled.
    pub scroll: f32,
    /// When the files were last read.
    read_at: Option<Instant>,
    sender: Option<Sender<Msg>>,
    /// Keeps the sampler running while set.
    sampling: Option<Arc<AtomicBool>>,
}

impl Board {
    /// Follows Weather's summary (the desktop only).
    pub fn new() -> Self {
        let mut board = Self::default();
        board.weather = weather::Watch::new(weather::summary_path());
        board
    }

    /// The summary as the card and the button show it now.
    pub fn weather_now(&self) -> Option<weather::Shown<'_>> {
        self.weather.summary.as_ref().and_then(|s| s.shown(chrono::Utc::now().timestamp()))
    }

    fn stop_sampling(&mut self) {
        if let Some(flag) = self.sampling.take() {
            flag.store(false, Ordering::Relaxed);
        }
    }
}

impl Drop for Board {
    fn drop(&mut self) {
        self.stop_sampling();
    }
}

// ---------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------

/// The gap between cards and around them.
const GAP: f32 = 12.0;
/// Height of the board's header row.
const HEADER_H: f32 = 56.0;
/// Width of a column at 100 % text.
const COLUMN_W: f32 = 312.0;

fn grow(base: f32) -> f32 {
    base * (1.0 + (text_scale() - 1.0) * 0.8)
}

/// The height of a card.
pub fn card_height(size: Size) -> f32 {
    match size {
        Size::Small => grow(176.0).round(),
        Size::Medium => grow(352.0).round(),
    }
}

/// The board's panel size on an output (without the room for its shadow).
pub fn panel_size(output: (f32, f32)) -> (f32, f32) {
    let bar = tokens().shell("taskbar-height");
    let column = (COLUMN_W * (1.0 + (text_scale() - 1.0) * 0.5)).round();
    let w = (2.0 * column + 3.0 * GAP).min(output.0 - 2.0 * PAD).max(200.0);
    let h = (output.1 - bar - 2.0 * PAD).max(200.0);
    (w, h)
}

/// Where everything goes, in the coordinates of the panel's content
/// (before scrolling).
#[derive(Clone, Debug, PartialEq)]
pub struct Layout {
    /// The header: "Add widgets".
    pub add: Rect,
    /// Where the cards scroll.
    pub view: Rect,
    pub cards: Vec<(Kind, Size, Rect)>,
    /// Height of everything that scrolls.
    pub height: f32,
}

/// Lays the cards out in two columns, each card under the shorter column
/// (one column where the panel is narrow).
pub fn layout(st: &State, panel: Rect) -> Layout {
    let add_w = grow(132.0).min(panel.w * 0.5);
    let header_h = grow(HEADER_H).min(96.0);
    let add = Rect::new(panel.right() - GAP - add_w, panel.y + (header_h - 32.0) / 2.0, add_w, 32.0);
    let view = Rect::new(panel.x, panel.y + header_h, panel.w, (panel.h - header_h).max(0.0));
    let columns = if panel.w >= 2.0 * 200.0 + 3.0 * GAP { 2 } else { 1 };
    let width = ((view.w - (columns as f32 + 1.0) * GAP) / columns as f32).max(40.0);
    let mut heights = vec![0.0_f32; columns];
    let mut cards = Vec::new();
    for (kind, size) in &st.config.widgets.cards {
        let column = (0..columns).min_by(|a, b| heights[*a].total_cmp(&heights[*b])).unwrap_or(0);
        let h = card_height(*size);
        let rect = Rect::new(view.x + GAP + column as f32 * (width + GAP), view.y + heights[column], width, h);
        heights[column] += h + GAP;
        cards.push((*kind, *size, rect));
    }
    let height = heights.iter().copied().fold(0.0, f32::max) + GAP;
    Layout { add, view, cards, height }
}

impl Layout {
    pub fn max_scroll(&self) -> f32 {
        (self.height - self.view.h).max(0.0)
    }

    /// The focusable controls in order, at their places before scrolling.
    pub fn controls(&self, st: &State) -> Vec<Hit> {
        let hit = |rect: Rect, act: Act| Hit { rect, action: Action::Widgets(act), focusable: true, tooltip: None, drag: false };
        let mut out = vec![hit(self.add, Act::Add)];
        for (kind, _, rect) in &self.cards {
            out.push(hit(*rect, Act::Open(*kind)));
            out.push(hit(menu_button(*rect), Act::Menu(*kind)));
            if *kind == Kind::Media && st.sys.media.first().is_some() {
                for (act, button) in media_buttons(*rect, st) {
                    out.push(hit(button, act));
                }
            }
        }
        out
    }
}

fn menu_button(card: Rect) -> Rect {
    Rect::new(card.right() - 40.0, card.y + 8.0, 32.0, 32.0)
}

fn media_buttons(card: Rect, st: &State) -> Vec<(Act, Rect)> {
    let Some(player) = st.sys.media.first() else {
        return Vec::new();
    };
    let y = card.bottom() - 48.0;
    let x = card.x + (card.w - 3.0 * 40.0) / 2.0;
    let mut out = Vec::new();
    if player.can_previous {
        out.push((Act::MediaPrevious, Rect::new(x, y, 36.0, 36.0)));
    }
    if player.can_play_pause {
        out.push((Act::MediaPlayPause, Rect::new(x + 40.0, y, 36.0, 36.0)));
    }
    if player.can_next {
        out.push((Act::MediaNext, Rect::new(x + 80.0, y, 36.0, 36.0)));
    }
    out
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

/// The board.
pub fn draw(f: &mut Frame, st: &State, panel: Rect) {
    let theme = f.theme();
    let kept = f.panel(panel, "acrylic");
    f.area(panel, Action::None);
    let lay = layout(st, panel);
    let scroll = st.board.scroll.clamp(0.0, lay.max_scroll());

    // The cards, scrolled, then cut at the view's edges.
    let first_hit = f.hits.len();
    for (kind, size, rect) in &lay.cards {
        let rect = Rect::new(rect.x, rect.y - scroll, rect.w, rect.h);
        if rect.bottom() < lay.view.y || rect.y > lay.view.bottom() {
            continue;
        }
        draw_card(f, st, *kind, *size, rect);
    }
    if let Some(kept) = &kept {
        f.restore_rows(kept, 0.0, lay.view.y);
        f.restore_rows(kept, lay.view.bottom(), f.canvas.height());
    }
    let view = lay.view;
    let mut index = first_hit;
    while index < f.hits.len() {
        let rect = f.hits[index].rect;
        let top = rect.y.max(view.y);
        let bottom = rect.bottom().min(view.bottom());
        if bottom - top < 4.0 {
            f.hits.remove(index);
            continue;
        }
        f.hits[index].rect = Rect::new(rect.x, top, rect.w, bottom - top);
        index += 1;
    }
    f.scrollbar(Rect::new(view.x, view.y, view.w - 2.0, view.h), lay.height, scroll);

    // The header, over the cards: the date and "Add widgets".
    let date = chrono::Local::now().format("%A, %-d %B").to_string();
    let header = Rect::new(panel.x + GAP + 4.0, panel.y, lay.add.x - panel.x - 2.0 * GAP, lay.view.y - panel.y);
    f.text_left(&date, header, &ramp("subtitle", theme.text));
    let touch = f.hit(lay.add, Action::Widgets(Act::Add));
    f.tip("Add widgets");
    let radius = tokens().radius_control;
    let fill = f.res.fill(if touch.pressed {
        "control-pressed"
    } else if touch.hovered {
        "control-hover"
    } else {
        "control"
    });
    let stroke = f.res.stroke("control");
    f.canvas.fill_rounded_rect(lay.add.x, lay.add.y, lay.add.w, lay.add.h, radius, fill);
    f.canvas.stroke_rounded_rect(lay.add.x, lay.add.y, lay.add.w, lay.add.h, radius, 1.0, stroke);
    let plus = Rect::new(lay.add.x + 10.0, lay.add.y + 9.0, 14.0, 14.0);
    f.canvas.line(plus.x, plus.y + 7.0, plus.right(), plus.y + 7.0, 1.5, theme.text);
    f.canvas.line(plus.x + 7.0, plus.y, plus.x + 7.0, plus.bottom(), 1.5, theme.text);
    f.text_left("Add widgets", Rect::new(lay.add.x + 32.0, lay.add.y, lay.add.w - 38.0, lay.add.h), &ramp("body", theme.text));
    if touch.focused {
        f.focus_ring(lay.add, radius);
    }
}

/// The card's frame, title and menu button; returns the content area.
fn card_frame(f: &mut Frame, st: &State, kind: Kind, rect: Rect) -> Rect {
    let theme = f.theme();
    let radius = tokens().radius_card;
    let touch = f.hit(rect, Action::Widgets(Act::Open(kind)));
    f.tip(&format!("Open {}", kind.title()));
    let fill = f.res.fill(if touch.pressed {
        "control-pressed"
    } else if touch.hovered {
        "control-hover"
    } else {
        "control"
    });
    let stroke = f.res.stroke("control");
    f.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, fill);
    f.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, 1.0, stroke);
    if touch.focused {
        f.focus_ring(rect, radius);
    }
    let icons = kind.icons(st);
    let names: Vec<&str> = icons.iter().map(String::as_str).collect();
    f.icon_or_placeholder(&names, rect.x + 14.0, rect.y + 14.0, 20.0);
    let title = Rect::new(rect.x + 42.0, rect.y + 8.0, rect.w - 42.0 - 44.0, 32.0);
    f.text_left(kind.title(), title, &ramp("caption", theme.text_secondary));
    let button = menu_button(rect);
    let touch = f.hit(button, Action::Widgets(Act::Menu(kind)));
    f.tip("Widget options");
    f.state_layer(button, tokens().radius_control, touch, false);
    let (cx, cy) = button.center();
    for dx in [-5.0, 0.0, 5.0] {
        f.canvas.fill_circle(cx + dx, cy, 1.4, theme.text);
    }
    Rect::new(rect.x + 16.0, rect.y + 44.0, rect.w - 32.0, rect.h - 56.0)
}

fn draw_card(f: &mut Frame, st: &State, kind: Kind, size: Size, rect: Rect) {
    let content = card_frame(f, st, kind, rect);
    match kind {
        Kind::Weather => draw_weather(f, st, size, content),
        Kind::Calendar => draw_calendar(f, st, size, content),
        Kind::Clock => draw_clock(f, st, size, content),
        Kind::System => draw_system(f, st, size, content),
        Kind::Media => draw_media(f, st, rect, content),
        Kind::Photos => draw_photos(f, st, content),
    }
}

/// A line of text at `y` inside `area`, if it fits; returns the next `y`.
fn line(f: &mut Frame, text: &str, area: Rect, y: f32, style: &boswas_ui::text::TextStyle) -> f32 {
    let h = style.line_height();
    if y + h > area.bottom() + 0.5 {
        return y;
    }
    f.text_left(text, Rect::new(area.x, y, area.w, h), style);
    y + h
}

/// A note in the middle of an empty card.
fn note(f: &mut Frame, text: &str, area: Rect) {
    let theme = f.theme();
    let style = ramp("body", theme.text_secondary);
    let lines = ((area.h / style.line_height()).floor() as usize).clamp(1, 4);
    f.text_wrapped(text, area.x, area.y + 4.0, area.w, lines, &style);
}

fn draw_weather(f: &mut Frame, st: &State, size: Size, area: Rect) {
    let theme = f.theme();
    let Some(shown) = st.board.weather_now() else {
        return note(f, "Add a place in Weather to see its weather here. Nothing is fetched until you do.", area);
    };
    let summary = shown.summary;
    let big = (grow(48.0)).min(area.h * 0.45).min(72.0);
    f.icon_or_placeholder(&[&summary.icon, "weather-few-clouds-symbolic"], area.x, area.y + 4.0, big);
    let temp_style = fit_line(&ramp("title", theme.text), big);
    let tx = area.x + big + 12.0;
    f.text_left(&summary.temperature, Rect::new(tx, area.y + 4.0, area.right() - tx, big), &temp_style);
    let mut y = area.y + big + 12.0;
    y = line(f, &summary.condition, area, y, &ramp("body-strong", theme.text));
    y = line(f, &summary.place, area, y, &ramp("caption", theme.text_secondary));
    if size == Size::Small {
        return;
    }
    if let Some(details) = &shown.details {
        y = line(f, details, area, y + 4.0, &ramp("caption", theme.text_secondary));
    }
    if let Some(updated) = shown.updated {
        y = line(f, updated, area, y, &ramp("caption", theme.text_muted));
    }
    // The next hours, side by side.
    let hours = &shown.hours;
    let caption = ramp("caption", theme.text_secondary);
    let icon = grow(24.0).min(32.0);
    let row_h = caption.line_height() * 2.0 + icon + 8.0;
    let top = (area.bottom() - row_h).max(y + 8.0);
    if hours.is_empty() || top + row_h > area.bottom() + 0.5 {
        return;
    }
    let slot = area.w / hours.len() as f32;
    for (i, hour) in hours.iter().enumerate() {
        let x = area.x + i as f32 * slot;
        f.text_centered(&hour.time, Rect::new(x, top, slot, caption.line_height()), &caption);
        f.icon_or_placeholder(&[&hour.icon], x + (slot - icon) / 2.0, top + caption.line_height() + 4.0, icon);
        f.text_centered(&hour.temperature, Rect::new(x, top + caption.line_height() + icon + 8.0, slot, caption.line_height()), &ramp("caption", theme.text));
    }
}

/// A calendar's colour from the palette.
fn calendar_color(st: &State, calendar: &str) -> Rgba {
    let family = st.board.colors.iter().find(|(id, _)| id == calendar).map_or("iris", |(_, c)| c.as_str());
    tokens().color(&format!("{family}.500")).unwrap_or(st_accent())
}

fn st_accent() -> Rgba {
    tokens().color("iris.500").unwrap_or(Rgba::WHITE)
}

/// "Today", "Tomorrow" or the weekday of an occurrence.
fn day_word(start: chrono::NaiveDateTime, today: chrono::NaiveDate) -> String {
    let days = (start.date() - today).num_days();
    match days {
        0 => "Today".into(),
        1 => "Tomorrow".into(),
        _ => start.format("%a %-d %b").to_string(),
    }
}

fn draw_calendar(f: &mut Frame, st: &State, size: Size, area: Rect) {
    let theme = f.theme();
    let now = chrono::Local::now();
    let today = now.date_naive();
    let mut y = area.y;
    let day = ramp("title", theme.text);
    let day = fit_line(&day, grow(36.0).min(area.h * 0.3));
    y = line(f, &now.format("%-d").to_string(), area, y, &day);
    y = line(f, &now.format("%A, %B").to_string(), area, y, &ramp("caption", theme.text_secondary));
    y += 8.0;
    let Some(agenda) = &st.board.agenda else {
        return;
    };
    let upcoming: Vec<&Occurrence> = agenda.iter().filter(|o| o.end_epoch > now.timestamp() || (o.all_day && o.end.date() > today)).collect();
    if upcoming.is_empty() {
        let style = ramp("body", theme.text_secondary);
        line(f, "Nothing planned for the next 7 days.", area, y, &style);
        return;
    }
    let most = if size == Size::Small { 1 } else { 5 };
    let title = ramp("body-strong", theme.text);
    let when = ramp("caption", theme.text_secondary);
    let row = title.line_height() + when.line_height() + 6.0;
    for occurrence in upcoming.into_iter().take(most) {
        if y + row > area.bottom() + 0.5 {
            break;
        }
        let color = calendar_color(st, &occurrence.calendar);
        f.canvas.fill_rounded_rect(area.x, y + 2.0, 4.0, row - 8.0, 2.0, color);
        let text_x = area.x + 12.0;
        f.text_left(&occurrence.summary, Rect::new(text_x, y, area.right() - text_x, title.line_height()), &title);
        let time = if occurrence.all_day {
            format!("{} · All day", day_word(occurrence.start, today))
        } else {
            format!("{} · {}–{}", day_word(occurrence.start, today), occurrence.start.format("%H:%M"), occurrence.end.format("%H:%M"))
        };
        f.text_left(&time, Rect::new(text_x, y + title.line_height(), area.right() - text_x, when.line_height()), &when);
        y += row;
    }
}

/// `+5:30`, `−1`: how far a zone is from the local one.
fn offset_text(seconds: i32) -> String {
    if seconds == 0 {
        return "Same time".into();
    }
    let sign = if seconds > 0 { '+' } else { '−' };
    let minutes = seconds.unsigned_abs() / 60;
    if minutes % 60 == 0 {
        format!("{sign}{} h", minutes / 60)
    } else {
        format!("{sign}{}:{:02} h", minutes / 60, minutes % 60)
    }
}

fn draw_clock(f: &mut Frame, st: &State, size: Size, area: Rect) {
    let theme = f.theme();
    let now = chrono::Utc::now().timestamp();
    let local_now = chrono::Local::now();
    let mut y = area.y;
    let big = fit_line(&ramp("title", theme.text), grow(36.0).min(area.h * 0.35));
    y = line(f, &local_now.format("%H:%M").to_string(), area, y, &big);
    y = line(f, &local_now.format("%A, %-d %B").to_string(), area, y, &ramp("caption", theme.text_secondary));
    y += 6.0;
    let view = &st.board.clock;
    let local = view.local.clone().unwrap_or_else(|| Zone::fixed(local_now.offset().local_minus_utc()));
    let body = ramp("body", theme.text);
    let caption = ramp("caption", theme.text_secondary);
    if let Some((alarm, at)) = sources::next_alarm(&view.data.alarms, now, &local) {
        let when = agenda::from_epoch(at, &local);
        let day = day_word(when, local_now.date_naive());
        let label = if alarm.name.is_empty() { String::new() } else { format!(" · {}", alarm.name) };
        let text = format!("Alarm {} · {day}{label}", when.format("%H:%M"));
        let h = body.line_height();
        if y + h <= area.bottom() + 0.5 {
            f.icon_or_placeholder(&["alarm-symbolic", "preferences-system-time-symbolic"], area.x, y + (h - 16.0) / 2.0, 16.0);
            f.text_left(&text, Rect::new(area.x + 22.0, y, area.w - 22.0, h), &body);
            y += h + 4.0;
        }
    }
    if size == Size::Small {
        return;
    }
    for place in &view.data.world {
        let Some(zone) = view.zones.iter().find(|(name, _)| *name == place.zone).map(|(_, z)| z) else {
            continue;
        };
        let there = agenda::from_epoch(now, zone);
        let difference = zone.offset_at(now) - local.offset_at(now);
        let shift = (there.date() - agenda::from_epoch(now, &local).date()).num_days();
        let day = match shift {
            0 => "",
            1.. => " · Tomorrow",
            _ => " · Yesterday",
        };
        let h = body.line_height();
        if y + h + caption.line_height() > area.bottom() + 0.5 {
            break;
        }
        let time_w = f.res.text.measure("00:00", &ramp("body-strong", theme.text)).0 + 4.0;
        f.text_left(&place.name, Rect::new(area.x, y, area.w - time_w - 8.0, h), &body);
        f.text_right(&there.format("%H:%M").to_string(), Rect::new(area.right() - time_w, y, time_w, h), &ramp("body-strong", theme.text));
        f.text_left(&format!("{}{day}", offset_text(difference)), Rect::new(area.x, y + h, area.w, caption.line_height()), &caption);
        y += h + caption.line_height() + 6.0;
    }
    if view.data.world.is_empty() && view.data.alarms.iter().all(|a| !a.enabled) {
        line(f, "Add places and alarms in Clock.", area, y, &caption);
    }
}

/// A labelled meter: label and value on one line, a bar under them.
fn meter(f: &mut Frame, label: &str, value: &str, fraction: Option<f32>, area: Rect, y: f32) -> f32 {
    let theme = f.theme();
    let body = ramp("body", theme.text);
    let h = body.line_height();
    if y + h + 10.0 > area.bottom() + 0.5 {
        return y;
    }
    let value_w = f.res.text.measure(value, &ramp("caption", theme.text_secondary)).0.min(area.w * 0.6) + 2.0;
    f.text_left(label, Rect::new(area.x, y, area.w - value_w - 8.0, h), &body);
    f.text_right(value, Rect::new(area.right() - value_w, y, value_w, h), &ramp("caption", theme.text_secondary));
    let bar = Rect::new(area.x, y + h + 3.0, area.w, 4.0);
    let rail = f.res.stroke("window").alpha(2.2);
    f.canvas.fill_rounded_rect(bar.x, bar.y, bar.w, bar.h, 2.0, rail);
    if let Some(fraction) = fraction {
        let color = if fraction > 0.9 { theme.warning } else { theme.accent };
        f.canvas.fill_rounded_rect(bar.x, bar.y, (bar.w * fraction.clamp(0.0, 1.0)).max(bar.h), bar.h, 2.0, color);
    }
    y + h + 14.0
}

fn draw_system(f: &mut Frame, st: &State, size: Size, area: Rect) {
    let board = &st.board;
    let mut y = area.y + 2.0;
    let cpu = board.cpu.map_or_else(|| "…".to_owned(), |c| format!("{:.0} %", c * 100.0));
    y = meter(f, "Processor", &cpu, board.cpu, area, y);
    let (memory, fraction) = match board.memory {
        Some((used, total)) => (format!("{} of {}", sources::size_text(used), sources::size_text(total)), Some(used as f32 / total as f32)),
        None => ("…".into(), None),
    };
    y = meter(f, "Memory", &memory, fraction, area, y);
    if size == Size::Small {
        return;
    }
    let (disk, fraction) = match board.disk {
        Some((used, total)) => (format!("{} free", sources::size_text(total.saturating_sub(used))), Some(used as f32 / total as f32)),
        None => ("…".into(), None),
    };
    y = meter(f, "Disk (home)", &disk, fraction, area, y);
    let battery = st.sys.power.battery.as_ref().map(|b| (b.percent, b.charging)).or_else(|| st.status.battery.as_ref().map(|b| (b.percent, b.charging)));
    if let Some((percent, charging)) = battery {
        let text = if charging { format!("{percent} % · charging") } else { format!("{percent} %") };
        meter(f, "Battery", &text, Some(f32::from(percent) / 100.0), area, y);
    }
}

fn draw_media(f: &mut Frame, st: &State, card: Rect, area: Rect) {
    let theme = f.theme();
    let Some(player) = st.sys.media.first() else {
        return note(f, "Nothing is playing.", area);
    };
    let mut y = area.y;
    let title = if player.title.is_empty() { &player.identity } else { &player.title };
    y = line(f, title, area, y, &ramp("body-strong", theme.text));
    let artist = if player.artist.is_empty() { &player.identity } else { &player.artist };
    line(f, artist, area, y, &ramp("caption", theme.text_secondary));
    for (act, rect) in media_buttons(card, st) {
        let (glyph, tip) = match act {
            Act::MediaPrevious => (Glyph::Previous, "Previous"),
            Act::MediaNext => (Glyph::Next, "Next"),
            _ if player.playing => (Glyph::Pause, "Pause"),
            _ => (Glyph::Play, "Play"),
        };
        f.glyph_button(rect, glyph, tip, Action::Widgets(act));
    }
}

fn draw_photos(f: &mut Frame, st: &State, area: Rect) {
    let Some((_, _, picture)) = &st.board.picture else {
        return note(f, "Your newest picture in Pictures appears here.", area);
    };
    let (pw, ph) = (picture.width() as f32, picture.height() as f32);
    let scale = (area.w / pw).max(area.h / ph);
    let placement = tiny_skia::Transform::from_scale(scale, scale).post_translate(area.x + (area.w - pw * scale) / 2.0, area.y + (area.h - ph * scale) / 2.0);
    let radius = tokens().radius_control;
    f.canvas.fill_rounded_rect_pixmap(area.x, area.y, area.w, area.h, [radius; 4], picture, placement);
}

/// The widgets button on the taskbar: the weather icon and the
/// temperature (with the condition where there is room), else a plain
/// widgets mark.
pub fn draw_button(f: &mut Frame, st: &State, rect: Rect) {
    let theme = f.theme();
    let shown = st.board.weather_now();
    let touch = f.hit(rect, Action::Taskbar(crate::taskbar::Act::Widgets));
    match &shown {
        Some(s) => f.tip(&format!("{} · {}\n{}", s.summary.temperature, s.summary.condition, s.summary.place)),
        None => f.tip("Widgets"),
    }
    f.state_layer(rect, tokens().radius_control, touch, st.flyout == Some(Flyout::Widgets));
    match shown {
        Some(s) => {
            let icon = 24.0;
            let (cx, cy) = (rect.x + 8.0, rect.center().1);
            f.icon_or_placeholder(&[&s.summary.icon, "weather-few-clouds-symbolic"], cx, cy - icon / 2.0, icon);
            let x = cx + icon + 8.0;
            let w = rect.right() - x - 6.0;
            if rect.w >= 110.0 {
                let room = (rect.h - 4.0) / 2.0;
                let strong = ui::ramp_within("caption", theme.text, room).weight(600);
                let caption = ui::ramp_within("caption", theme.text_secondary, room);
                let top = rect.y + (rect.h - 2.0 * room) / 2.0;
                f.text_left(&s.summary.temperature, Rect::new(x, top, w, room), &strong);
                f.text_left(&s.summary.condition, Rect::new(x, top + room, w, room), &caption);
            } else {
                f.text_left(&s.summary.temperature, Rect::new(x, rect.y, w, rect.h), &ui::ramp_within("body-strong", theme.text, rect.h - 4.0));
            }
        }
        None => {
            // Four tiles, two of them wide: a board of cards.
            let (cx, cy) = rect.center();
            let s = 18.0;
            let (x, y) = (cx - s / 2.0, cy - s / 2.0);
            let color = theme.text;
            let r = 2.0;
            f.canvas.stroke_rounded_rect(x, y, 8.0, 10.0, r, 1.5, color);
            f.canvas.stroke_rounded_rect(x + 10.0, y, 8.0, 6.0, r, 1.5, color);
            f.canvas.stroke_rounded_rect(x, y + 12.0, 8.0, 6.0, r, 1.5, color);
            f.canvas.stroke_rounded_rect(x + 10.0, y + 8.0, 8.0, 10.0, r, 1.5, color);
        }
    }
}

/// What the button looks like: nothing, the plain mark or the weather.
pub fn button_kind(st: &State) -> crate::taskbar::WidgetsButton {
    use crate::taskbar::WidgetsButton;
    if !st.desktop.taskbar.widgets || st.mode != crate::state::Mode::Desktop {
        WidgetsButton::None
    } else if st.board.weather_now().is_some() {
        WidgetsButton::Weather
    } else {
        WidgetsButton::Plain
    }
}

// ---------------------------------------------------------------------------
// Behaviour
// ---------------------------------------------------------------------------

impl Shell {
    fn widgets_sender(&mut self) -> Option<Sender<Msg>> {
        if self.st.board.sender.is_none() {
            let (sender, receiver) = channel::channel::<Msg>();
            let inserted = self.loop_handle.insert_source(receiver, |event, _, shell| {
                if let channel::Event::Msg(message) = event {
                    shell.widgets_message(message);
                }
            });
            if inserted.is_err() {
                return None;
            }
            self.st.board.sender = Some(sender);
        }
        self.st.board.sender.clone()
    }

    /// The board opened: read the files and start sampling.
    pub fn widgets_opened(&mut self) {
        self.st.board.scroll = 0.0;
        if self.st.board.weather.check() {
            self.mark(Role::Taskbar);
        }
        self.read_widget_sources();
        self.start_sampling();
    }

    /// The board closed: stop sampling.
    pub fn widgets_closed(&mut self) {
        self.st.board.stop_sampling();
    }

    fn start_sampling(&mut self) {
        let Some(sender) = self.widgets_sender() else {
            return;
        };
        self.st.board.stop_sampling();
        let flag = Arc::new(AtomicBool::new(true));
        self.st.board.sampling = Some(flag.clone());
        let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"));
        let spawned = std::thread::Builder::new().name("widgets-sampler".into()).spawn(move || {
            while flag.load(Ordering::Relaxed) {
                if sender.send(Msg::Sample(sources::sample(&home))).is_err() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(1000));
            }
        });
        if spawned.is_err() {
            self.st.board.sampling = None;
        }
    }

    /// Reads Calendar, Clock and Pictures on a worker thread.
    fn read_widget_sources(&mut self) {
        let Some(sender) = self.widgets_sender() else {
            return;
        };
        self.st.board.read_at = Some(Instant::now());
        let known = self.st.board.picture.as_ref().map(|(path, when, _)| (path.clone(), *when));
        let pictures = pictures_dir();
        let _ = std::thread::Builder::new().name("widgets-sources".into()).spawn(move || {
            let mut zones = tzif::Cache::new(std::path::Path::new(tzif::ZONEINFO));
            let local = agenda::local_zone(&mut zones);
            let now = chrono::Utc::now().timestamp();
            let (agenda, colors) = match agenda::calendar_path().map(|path| (agenda::read_own_file(&path, agenda::MAX_BYTES), path)) {
                Some((Ok(Some(text)), _)) => match agenda::Document::parse(&text, &mut zones) {
                    Some(doc) => (agenda::occurrences(&doc, now, now + 7 * 86400, &local, &mut zones, 32), doc.calendars.iter().map(|c| (c.id.clone(), c.color.clone())).collect()),
                    None => (Vec::new(), Vec::new()),
                },
                Some((Err(problem), path)) => {
                    tracing::warn!("{}: not shown on the widgets board: {problem}", path.display());
                    (Vec::new(), Vec::new())
                }
                _ => (Vec::new(), Vec::new()),
            };
            let data = sources::clock_path().map(|path| sources::read_clock(&path, &mut zones)).unwrap_or_default();
            let world_zones = data.world.iter().filter_map(|p| zones.get(&p.zone).cloned().map(|z| (p.zone.clone(), z))).collect();
            let picture = pictures.and_then(|dir| sources::newest_picture(&dir)).and_then(|path| {
                let modified = std::fs::symlink_metadata(&path).and_then(|m| m.modified()).ok()?;
                let fresh = known.as_ref() != Some(&(path.clone(), modified));
                let thumbnail = if fresh { sources::thumbnail(&path, 720) } else { None };
                if fresh && thumbnail.is_none() {
                    return None;
                }
                Some((path, modified, thumbnail))
            });
            let _ = sender.send(Msg::Sources(Box::new(Sources {
                agenda,
                colors,
                clock: ClockView { data, zones: world_zones, local: Some(local) },
                picture,
            })));
        });
    }

    pub fn widgets_message(&mut self, message: Msg) {
        match message {
            Msg::Sources(found) => {
                let found = *found;
                self.st.board.agenda = Some(found.agenda);
                self.st.board.colors = found.colors;
                self.st.board.clock = found.clock;
                self.st.board.picture = match found.picture {
                    Some((path, when, Some(thumbnail))) => Some((path, when, Rc::new(thumbnail))),
                    Some((path, when, None)) => self.st.board.picture.take().filter(|(p, w, _)| *p == path && *w == when),
                    None => None,
                };
            }
            Msg::Sample(sample) => {
                if self.st.board.sampling.is_none() {
                    return;
                }
                if let Some(cpu) = sample.cpu {
                    if let Some(before) = self.st.board.last_cpu.replace(cpu) {
                        self.st.board.cpu = sources::cpu_load(before, cpu).or(self.st.board.cpu);
                    }
                }
                self.st.board.memory = sample.memory.or(self.st.board.memory);
                self.st.board.disk = sample.disk.or(self.st.board.disk);
            }
        }
        if self.open_kind() == Some(Flyout::Widgets) {
            self.mark(Role::Flyout);
        }
    }

    /// Once a second: the summary once a minute (for the button), the
    /// files again every half minute while the board is open.
    pub fn widgets_tick(&mut self) {
        if self.st.mode != crate::state::Mode::Desktop {
            return;
        }
        if self.st.board.weather.poll(chrono::Utc::now().timestamp()) {
            self.mark(Role::Taskbar);
            if self.st.flyout == Some(Flyout::Widgets) {
                self.mark(Role::Flyout);
            }
        }
        if self.open_kind() == Some(Flyout::Widgets) {
            if self.st.board.read_at.is_none_or(|at| at.elapsed() >= Duration::from_secs(30)) {
                self.read_widget_sources();
            }
            // The clock card's minutes.
            if chrono::Local::now().second() == 0 {
                self.mark(Role::Flyout);
            }
        }
    }

    /// The board's geometry as the open flyout shows it.
    fn widgets_layout(&self) -> Option<Layout> {
        let surface = self.surfaces.iter().find(|s| s.role == Role::Flyout)?;
        let (w, h) = (surface.width as f32, surface.height as f32);
        Some(layout(&self.st, Rect::new(PAD, PAD, w - 2.0 * PAD, h - 2.0 * PAD)))
    }

    pub fn widgets_scroll(&mut self, steps: i32) {
        let Some(lay) = self.widgets_layout() else {
            return;
        };
        let scroll = (self.st.board.scroll + steps as f32 * 64.0).clamp(0.0, lay.max_scroll());
        if scroll != self.st.board.scroll {
            self.st.board.scroll = scroll;
            self.mark(Role::Flyout);
        }
    }

    /// Scrolls so that a control of the board is in sight.
    fn widgets_reveal(&mut self, action: &Action) {
        let Some(lay) = self.widgets_layout() else {
            return;
        };
        let Some(rect) = lay.controls(&self.st).into_iter().find(|h| &h.action == action).map(|h| h.rect) else {
            return;
        };
        if rect.y < lay.view.y {
            return;
        }
        let top = rect.y - lay.view.y;
        let mut scroll = self.st.board.scroll;
        if top < scroll {
            scroll = top - GAP;
        } else if top + rect.h > scroll + lay.view.h {
            scroll = top + rect.h - lay.view.h + GAP;
        }
        self.st.board.scroll = scroll.clamp(0.0, lay.max_scroll());
    }

    pub fn widgets_key(&mut self, event: &KeyEvent) {
        let shift = self.modifiers.shift;
        match event.keysym {
            Keysym::Escape => self.close_flyout(),
            Keysym::Return | Keysym::KP_Enter | Keysym::space => {
                if let Some(Action::Widgets(act)) = self.st.focus.clone() {
                    self.widgets_action(act, None);
                }
            }
            Keysym::Menu | Keysym::F10 if event.keysym == Keysym::Menu || shift => {
                if let Some(Action::Widgets(Act::Open(kind) | Act::Menu(kind))) = self.st.focus.clone() {
                    self.widgets_action(Act::Menu(kind), None);
                }
            }
            Keysym::Page_Up | Keysym::Page_Down => {
                let step = self.widgets_layout().map_or(300.0, |l| l.view.h * 0.9);
                let Some(lay) = self.widgets_layout() else {
                    return;
                };
                let delta = if event.keysym == Keysym::Page_Up { -step } else { step };
                self.st.board.scroll = (self.st.board.scroll + delta).clamp(0.0, lay.max_scroll());
                self.mark(Role::Flyout);
            }
            keysym => {
                let nav = match keysym {
                    Keysym::Tab if shift => Some(Nav::Previous),
                    Keysym::ISO_Left_Tab => Some(Nav::Previous),
                    Keysym::Tab => Some(Nav::Next),
                    Keysym::Left => Some(Nav::Left),
                    Keysym::Right => Some(Nav::Right),
                    Keysym::Up => Some(Nav::Up),
                    Keysym::Down => Some(Nav::Down),
                    _ => None,
                };
                let (Some(nav), Some(lay)) = (nav, self.widgets_layout()) else {
                    return;
                };
                if let Some(next) = ui::navigate(&lay.controls(&self.st), self.st.focus.as_ref(), nav) {
                    self.widgets_reveal(&next);
                    self.st.focus = Some(next);
                    self.mark(Role::Flyout);
                }
            }
        }
    }

    /// A control of the board was clicked or chosen with the keyboard.
    pub fn widgets_action(&mut self, act: Act, surface: Option<usize>) {
        match act {
            Act::Open(kind) => self.widgets_open(kind),
            Act::Menu(kind) => {
                let items = self.widget_menu(kind);
                self.widgets_menu_at(items, &Action::Widgets(Act::Menu(kind)), surface);
            }
            Act::Add => {
                let items = self.add_widgets_menu();
                self.widgets_menu_at(items, &Action::Widgets(Act::Add), surface);
            }
            Act::MediaPrevious | Act::MediaPlayPause | Act::MediaNext => {
                if let Some(key) = self.st.sys.media.first().map(|p| p.key.clone()) {
                    self.service(match act {
                        Act::MediaPrevious => boswas_services::Command::MediaPrevious { key },
                        Act::MediaNext => boswas_services::Command::MediaNext { key },
                        _ => boswas_services::Command::MediaPlayPause { key },
                    });
                }
            }
        }
    }

    /// Opens a menu under a control of the board.
    fn widgets_menu_at(&mut self, items: Vec<Item>, action: &Action, surface: Option<usize>) {
        let Some(index) = surface.or_else(|| self.surfaces.iter().position(|s| s.role == Role::Flyout)) else {
            return;
        };
        let surface = &self.surfaces[index];
        let (origin, output) = (surface.origin, surface.output.clone());
        let rect = surface.hits.iter().rev().find(|h| &h.action == action).map(|h| h.rect).unwrap_or(Rect::new(PAD, PAD, 32.0, 32.0));
        let x = origin.0 + rect.right() - 200.0_f32.min(rect.w.max(32.0));
        self.open_menu(items, x, origin.1 + rect.bottom() + 4.0, false, &output);
    }

    fn widget_menu(&self, kind: Kind) -> Vec<Item> {
        let cards = &self.st.config.widgets.cards;
        let position = cards.iter().position(|(k, _)| *k == kind);
        let size = position.map(|p| cards[p].1);
        let mut small = Item::new("Small", "", Cmd::Widgets(WidgetCmd::Resize(kind, Size::Small)));
        small.toggle = boswas_services::MenuToggle::Radio(size == Some(Size::Small));
        let mut medium = Item::new("Medium", "", Cmd::Widgets(WidgetCmd::Resize(kind, Size::Medium)));
        medium.toggle = boswas_services::MenuToggle::Radio(size == Some(Size::Medium));
        let mut up = Item::new("Move up", "go-up-symbolic", Cmd::Widgets(WidgetCmd::Up(kind)));
        up.enabled = position.is_some_and(|p| p > 0);
        let mut down = Item::new("Move down", "go-down-symbolic", Cmd::Widgets(WidgetCmd::Down(kind)));
        down.enabled = position.is_some_and(|p| p + 1 < cards.len());
        vec![
            Item::new(&format!("Open {}", kind.title()), kind.icon(), Cmd::Widgets(WidgetCmd::Open(kind))),
            Item::separator(),
            small,
            medium,
            up,
            down,
            Item::separator(),
            Item::new("Remove widget", "list-remove-symbolic", Cmd::Widgets(WidgetCmd::Remove(kind))),
        ]
    }

    fn add_widgets_menu(&self) -> Vec<Item> {
        let missing: Vec<Item> = Kind::ALL.iter().filter(|k| !self.st.config.widgets.has(**k)).map(|k| Item::new(k.title(), k.icon(), Cmd::Widgets(WidgetCmd::Add(*k)))).collect();
        if missing.is_empty() {
            let mut all = Item::new("Every widget is on the board", "", Cmd::Nothing);
            all.enabled = false;
            return vec![all];
        }
        missing
    }

    /// Carries out a menu entry of the board.
    pub fn widgets_command(&mut self, cmd: WidgetCmd) {
        if let WidgetCmd::Open(kind) = cmd {
            return self.widgets_open(kind);
        }
        if self.st.config.change_widgets(cmd) {
            if let Some(lay) = self.widgets_layout() {
                self.st.board.scroll = self.st.board.scroll.clamp(0.0, lay.max_scroll());
            }
            if let WidgetCmd::Add(kind) = cmd {
                self.st.focus = Some(Action::Widgets(Act::Open(kind)));
                self.widgets_reveal(&Action::Widgets(Act::Open(kind)));
            }
            self.mark(Role::Flyout);
        }
    }

    /// Opens a card's application (the picture itself for Photos, the
    /// player for Media).
    fn widgets_open(&mut self, kind: Kind) {
        match kind {
            Kind::Photos => {
                if let Some(path) = self.st.board.picture.as_ref().map(|(p, _, _)| p.clone()) {
                    self.close_transients();
                    self.open_path(&path);
                    return;
                }
            }
            Kind::Media => {
                let entry = self.st.sys.media.first().and_then(|p| p.desktop_entry.clone());
                if let Some(index) = entry.and_then(|e| self.st.app_index(e.trim_end_matches(".desktop"))) {
                    return self.launch_app(index);
                }
            }
            _ => {}
        }
        match self.st.app_index(kind.app_id()) {
            Some(index) => self.launch_app(index),
            None => {
                self.close_transients();
                self.notice(&format!("{} is not installed", kind.title()), "Install it with the Software Center.", None);
            }
        }
    }
}

/// The user's Pictures folder.
fn pictures_dir() -> Option<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from)?;
    let user_dirs = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from).filter(|p| p.is_absolute()).unwrap_or_else(|| home.join(".config")).join("user-dirs.dirs");
    let text = std::fs::read_to_string(user_dirs).ok();
    crate::capture::user_dir(&home, text.as_deref(), crate::settings::Folder::Pictures)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::testing;
    use boswas_ui::Scheme;

    #[test]
    fn the_arrangement_is_kept_and_read_back() {
        let default = Arrangement::default();
        assert_eq!(default.cards.len(), Kind::ALL.len(), "every widget is on a new board");
        let text = default.to_toml();
        let table: toml::Table = text.parse().unwrap();
        assert_eq!(Arrangement::from_table(table["widgets"].as_table().unwrap()), Some(default.clone()));
        let mut changed = default.clone();
        assert!(changed.apply(WidgetCmd::Remove(Kind::System)));
        assert!(!changed.apply(WidgetCmd::Remove(Kind::System)), "already gone");
        assert!(changed.apply(WidgetCmd::Up(Kind::Clock)));
        assert!(!changed.apply(WidgetCmd::Up(Kind::Weather)), "the first cannot go up");
        assert!(changed.apply(WidgetCmd::Resize(Kind::Photos, Size::Small)));
        assert!(!changed.apply(WidgetCmd::Resize(Kind::Photos, Size::Small)));
        assert!(!changed.apply(WidgetCmd::Down(Kind::Photos)), "the last cannot go down");
        assert!(changed.apply(WidgetCmd::Add(Kind::System)));
        assert!(!changed.apply(WidgetCmd::Add(Kind::System)));
        assert_eq!(changed.cards.iter().map(|(k, _)| k.key()).collect::<Vec<_>>(), ["weather", "clock", "calendar", "media", "photos", "system"]);
        let table: toml::Table = changed.to_toml().parse().unwrap();
        let back = Arrangement::from_table(table["widgets"].as_table().unwrap()).unwrap();
        assert_eq!(back, changed);
        assert_eq!(back.cards[4], (Kind::Photos, Size::Small));
        // Unknown names, repeats and values of the wrong kind.
        let odd: toml::Table = "cards = [\"clock\", \"news\", \"clock\", 4, \"weather\"]\nsmall = \"clock\"\n".parse().unwrap();
        assert_eq!(Arrangement::from_table(&odd).unwrap().cards, [(Kind::Clock, Size::Medium), (Kind::Weather, Size::Medium)]);
        let empty: toml::Table = "cards = []\n".parse().unwrap();
        assert!(Arrangement::from_table(&empty).unwrap().cards.is_empty(), "an empty board stays empty");
        assert_eq!(Arrangement::from_table(&toml::Table::new()), None, "no list: the default board");
    }

    #[test]
    fn cards_fill_two_columns_under_the_shorter_one() {
        let st = State::for_tests();
        let panel = Rect::new(PAD, PAD, 648.0, 900.0);
        let lay = layout(&st, panel);
        assert_eq!(lay.cards.len(), 6);
        let left = lay.cards[0].2;
        let right = lay.cards[1].2;
        assert!(left.x < right.x && (left.y - right.y).abs() < 0.5, "weather and calendar side by side");
        for (i, (_, _, a)) in lay.cards.iter().enumerate() {
            assert!(a.x >= panel.x && a.right() <= panel.right() + 0.5, "{a:?}");
            for (_, _, b) in &lay.cards[i + 1..] {
                let apart = a.right() <= b.x || b.right() <= a.x || a.bottom() <= b.y || b.bottom() <= a.y;
                assert!(apart, "{a:?} and {b:?} overlap");
            }
        }
        assert!(lay.height > lay.view.h, "six cards are more than the view: the board scrolls");
        // A narrow board has one column.
        let narrow = layout(&st, Rect::new(PAD, PAD, 300.0, 900.0));
        assert!(narrow.cards.iter().all(|(_, _, r)| (r.x - narrow.cards[0].2.x).abs() < 0.5));
    }

    #[test]
    fn the_board_draws_in_both_schemes_at_every_text_size_with_and_without_fonts() {
        let mut st = State::for_tests();
        let fetched = chrono::Utc::now().timestamp() - 600;
        st.board.weather.summary = Some(weather::parse(&weather::sample(fetched).to_string()).unwrap());
        st.board.cpu = Some(0.42);
        st.board.memory = Some((6_000_000_000, 16_000_000_000));
        st.board.disk = Some((200_000_000_000, 500_000_000_000));
        let today = chrono::Local::now().naive_local();
        st.board.agenda = Some(vec![Occurrence {
            summary: "Team meeting with a rather long title that does not fit".into(),
            calendar: "work".into(),
            all_day: false,
            start: today,
            end: today + chrono::Duration::hours(1),
            start_epoch: chrono::Utc::now().timestamp(),
            end_epoch: chrono::Utc::now().timestamp() + 3600,
            uid: "u".into(),
        }]);
        st.board.colors = vec![("work".into(), "coral".into())];
        st.board.clock.data.world.push(sources::Place { zone: "UTC".into(), name: "London".into() });
        st.board.clock.zones.push(("UTC".into(), Zone::fixed(0)));
        st.board.picture = Some((PathBuf::from("/p.png"), std::time::SystemTime::now(), Rc::new(Pixmap::new(64, 48).unwrap())));
        for text in [100, 175, 225] {
            ui::set_text_scale(text);
            for output in [(1920.0, 1080.0), (1366.0, 768.0)] {
                let (w, h) = panel_size(output);
                let panel = Rect::new(PAD, PAD, w, h);
                for scheme in [Scheme::Dark, Scheme::Light] {
                    for scroll in [0.0, 300.0, 1e6] {
                        st.board.scroll = scroll;
                        let (canvas, hits) = testing::render(w + 2.0 * PAD, h + 2.0 * PAD, 1.0, scheme, Some((100.0, 200.0)), None, |f| draw(f, &st, panel));
                        assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0));
                        assert!(testing::hits_are_inside(&hits, w + 2.0 * PAD, h + 2.0 * PAD), "{text} {output:?}");
                        assert!(hits.iter().any(|h| h.action == Action::Widgets(Act::Add)));
                    }
                }
            }
        }
        ui::set_text_scale(100);
        // Without data every card says what it is waiting for, and without fonts nothing panics.
        let empty = State::for_tests();
        let fontless = crate::ui::Resources::with_text(boswas_ui::Theme::of(Scheme::Dark), boswas_ui::text::TextEngine::from_font_dirs(&[]));
        let (canvas, hits) = testing::render_with(fontless, 672.0, 744.0, 1.0, None, None, |f| draw(f, &empty, Rect::new(PAD, PAD, 648.0, 720.0)));
        assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0));
        assert!(hits.iter().any(|h| h.action == Action::Widgets(Act::Open(Kind::Weather))));
    }

    #[test]
    fn the_keyboard_reaches_every_card_and_its_menu() {
        let st = State::for_tests();
        let lay = layout(&st, Rect::new(PAD, PAD, 648.0, 600.0));
        let controls = lay.controls(&st);
        let mut focus: Option<Action> = None;
        let mut seen = Vec::new();
        for _ in 0..controls.len() {
            focus = ui::navigate(&controls, focus.as_ref(), Nav::Next);
            seen.push(focus.clone().unwrap());
        }
        for kind in Kind::ALL {
            assert!(seen.contains(&Action::Widgets(Act::Open(kind))), "{kind:?}");
            assert!(seen.contains(&Action::Widgets(Act::Menu(kind))), "{kind:?}");
        }
        assert_eq!(ui::navigate(&controls, focus.as_ref(), Nav::Next), Some(Action::Widgets(Act::Add)), "Tab wraps to the header");
    }

    #[test]
    fn the_button_follows_the_setting_and_the_weather() {
        use crate::taskbar::WidgetsButton;
        let mut st = State::for_tests();
        assert_eq!(button_kind(&st), WidgetsButton::Plain, "no summary: the plain mark");
        st.board.weather.summary = Some(weather::parse(&weather::sample(chrono::Utc::now().timestamp() - 60).to_string()).unwrap());
        assert_eq!(button_kind(&st), WidgetsButton::Weather);
        st.board.weather.summary = Some(weather::parse(&weather::sample(chrono::Utc::now().timestamp() - 4 * 3600).to_string()).unwrap());
        assert_eq!(button_kind(&st), WidgetsButton::Plain, "a summary older than three hours is not shown");
        st.desktop.taskbar.widgets = false;
        assert_eq!(button_kind(&st), WidgetsButton::None);
        assert_eq!(offset_text(19800), "+5:30 h");
        assert_eq!(offset_text(-3600), "−1 h");
        assert_eq!(offset_text(0), "Same time");
    }
}
