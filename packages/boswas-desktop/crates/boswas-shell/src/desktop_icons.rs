//! Desktop icons: the user's desktop folder (`XDG_DESKTOP_DIR`, else
//! `~/Desktop`) and the system icons chosen in Settings (Home, Trash, This
//! device) as icons with labels on the wallpaper of the primary output,
//! like the Windows desktop.
//!
//! Icons sit on a grid from the top-left, column by column. Click selects,
//! Ctrl and Shift add, a drag on the background draws a selection band,
//! a double-click or Enter opens (with the shell's opener; folders in
//! Files), a drag moves icons (their places are kept in
//! `$XDG_STATE_HOME/boswas/desktop-layout.json` while auto arrange is
//! off; a drop on a folder moves into it, on the Trash trashes), F2
//! renames in a field over the label, Delete moves to the Trash, Ctrl+C,
//! Ctrl+X and Ctrl+V use the clipboard format Files uses, Ctrl+A selects
//! all and the arrow keys move. Files dropped from other programs (a
//! Wayland drag of `text/uri-list`) are copied, or moved when the source
//! asks for a move, into the folder. Right-click menus offer View, Sort by,
//! Refresh, New, Paste, Open in Terminal and the settings on the
//! background, and Open, Open with, Cut, Copy, Rename, Delete and
//! Properties on icons.
//!
//! The folder is watched with inotify; reading it, making thumbnails and
//! every change to files happen on worker threads (`desktop_files.rs`).
//! A launcher (`.desktop`) is started only when it is a regular file of
//! the user with the execute bit set; any other launcher opens as text,
//! with a notice that says why.

use std::{
    collections::{HashMap, HashSet},
    ffi::OsString,
    path::{Path, PathBuf},
    rc::Rc,
    time::{Duration, Instant},
};

use boswas_ui::{text::TextStyle, tokens::tokens, Rgba};
use smithay_client_toolkit::{
    data_device_manager::{data_offer::DragOffer, data_source::CopyPasteSource, WritePipe},
    reexports::calloop::channel::{self, Sender},
    seat::{
        keyboard::{KeyEvent, Keysym},
        pointer::{PointerEvent, PointerEventKind},
    },
    shell::{
        wlr_layer::{Anchor, KeyboardInteractivity, Layer},
        WaylandSurface,
    },
};
use tiny_skia::Pixmap;
use wayland_client::protocol::{wl_data_device_manager::DndAction, wl_data_source::WlDataSource, wl_surface::WlSurface};

use crate::{
    action::Action,
    desktop_files::{self as files, DesktopDir, Entry, Report, Saved, Setting, SortKey},
    menu::{Cmd, Item},
    settings::{DesktopIcons, IconSize, SystemIcon},
    state::{Mode, State},
    ui::{self, ramp, text_scale, Frame, Hit, Nav, Rect, TextField},
    Role, Shell, Spec, PAD,
};

const BTN_LEFT: u32 = 0x110;
const BTN_RIGHT: u32 = 0x111;
/// Two clicks within this time on one icon open it.
const DOUBLE_CLICK: Duration = Duration::from_millis(450);
/// How far the pointer moves with the button held before a drag begins.
const DRAG_START: f32 = 5.0;
/// The margin between the screen's edge and the first icons.
const MARGIN: f32 = 6.0;

/// A control on the wallpaper (for hovering and tooltips).
#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    Icon(String),
}

/// What a menu entry of the desktop does.
#[derive(Clone, Debug, PartialEq)]
pub enum DeskCmd {
    Size(IconSize),
    AutoArrange,
    AlignToGrid,
    ShowIcons,
    Sort(SortKey),
    Refresh,
    Paste,
    NewFolder,
    NewTextDocument,
    OpenTerminal,
    Open,
    /// Open the selection with this application (a desktop file id).
    OpenWith(String),
    Cut,
    Copy,
    Rename,
    Delete,
    Properties,
}

/// One icon.
#[derive(Clone, Debug, PartialEq)]
pub struct Icon {
    pub key: String,
    pub label: String,
    pub system: Option<SystemIcon>,
    /// The file behind it (index into the scan).
    pub entry: Option<usize>,
}

/// An icon being renamed.
pub struct Renaming {
    pub key: String,
    pub old: OsString,
    pub field: TextField,
    /// The name (without its extension) is still selected, as Files and
    /// Windows select it: what is typed first replaces it.
    pub fresh: bool,
    /// Bytes of the extension that a replacement keeps.
    pub extension: String,
}

impl Renaming {
    /// Handles a key; returns whether it was an editing key.
    pub fn key(&mut self, keysym: Keysym, ctrl: bool, typed: Option<&str>) -> bool {
        if self.fresh {
            match keysym {
                Keysym::BackSpace | Keysym::Delete | Keysym::KP_Delete => {
                    self.replace("");
                    return true;
                }
                Keysym::Left | Keysym::Right | Keysym::Home | Keysym::End | Keysym::KP_Left | Keysym::KP_Right => self.fresh = false,
                _ => {
                    if let Some(text) = typed.filter(|t| !t.chars().all(char::is_control)) {
                        self.replace(text);
                        return true;
                    }
                }
            }
        }
        if self.field.key(keysym, ctrl) {
            return true;
        }
        typed.is_some_and(|text| self.field.insert(text))
    }

    /// Replaces the selected name with `text`, keeping the extension.
    fn replace(&mut self, text: &str) {
        self.fresh = false;
        let extension = self.extension.clone();
        self.field.set(&format!("{text}{extension}"));
        for _ in 0..extension.chars().count() {
            self.field.key(Keysym::Left, false);
        }
    }
}

/// The pointer went down on the desktop.
#[derive(Clone, Debug)]
struct Press {
    at: (f32, f32),
    key: Option<String>,
    ctrl: bool,
    shift: bool,
}

/// Icons being dragged to a new place.
#[derive(Clone, Debug, PartialEq)]
pub struct Drag {
    pub keys: Vec<String>,
    pub from: (f32, f32),
    pub to: (f32, f32),
}

/// A drag from another program over the desktop.
struct Incoming {
    offer: Option<DragOffer>,
    at: (f32, f32),
}

/// Where dropped or pasted files go.
#[derive(Clone, Debug, PartialEq)]
pub enum Target {
    Desktop,
    Folder(OsString),
    Trash,
}

/// A message from the desktop's worker threads.
pub enum Msg {
    Scanned { entries: Result<Vec<Entry>, String>, thumbnails: Vec<(String, i64, u64, Pixmap)>, saved: Option<Saved>, trash_full: bool },
    Changed,
    Created { name: Result<String, String>, rename: bool },
    Renamed { key: String, name: String, result: Result<(), String> },
    Trashed { failed: Vec<(String, String)> },
    Transferred { report: Report, cut: bool },
    /// The data of a drop arrived.
    Dropped { paths: Vec<PathBuf>, target: Target, moving: bool },
    ViewSet(Result<(), String>),
}

/// The desktop icons' state.
#[derive(Default)]
pub struct Desk {
    pub dir: Option<DesktopDir>,
    pub entries: Vec<Entry>,
    pub scanned: bool,
    pub saved: Saved,
    saved_loaded: bool,
    pub thumbnails: HashMap<String, (i64, u64, Rc<Pixmap>)>,
    pub selected: Vec<String>,
    anchor: Option<String>,
    /// The icon the keyboard is on.
    pub cursor: Option<String>,
    /// The keyboard was used since the last click: the cursor is drawn.
    pub keyboard: bool,
    /// The selection band: where it began and where the pointer is.
    pub band: Option<((f32, f32), (f32, f32))>,
    band_base: Vec<String>,
    press: Option<Press>,
    pub drag: Option<Drag>,
    last_click: Option<(String, Instant)>,
    /// The icon a drag hovers (a folder or the Trash).
    pub drop_target: Option<String>,
    incoming: Option<Incoming>,
    pub renaming: Option<Renaming>,
    /// What the desktop put on the clipboard.
    clip: Option<(CopyPasteSource, Vec<PathBuf>, bool)>,
    /// Cut icons are drawn faded.
    pub cut: Vec<String>,
    /// Where items that are about to appear go (grid cells).
    arrivals: Vec<(String, (f32, f32))>,
    /// Where the next pasted or dropped items go (a grid cell).
    drop_place: Option<(f32, f32)>,
    /// A drop whose list is being read: finished when it is in.
    finishing: Option<DragOffer>,
    /// The Trash holds something (its icon shows it).
    pub trash_full: bool,
    /// A new folder to rename once it is seen.
    rename_next: Option<String>,
    /// Where the menu was opened (output coordinates).
    menu_point: Option<(f32, f32)>,
    /// The folder was shown before: icons keep their places when new ones
    /// appear.
    shown_once: bool,
    scanning: bool,
    rescan: bool,
    watching: bool,
    sender: Option<Sender<Msg>>,
}

// ---------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------

/// The grid of a desktop.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Grid {
    pub cell: (f32, f32),
    pub columns: usize,
    pub rows: usize,
    pub icon: f32,
}

/// The label's style.
fn label_style() -> TextStyle {
    ramp("caption", Rgba::WHITE)
}

/// The grid of an output of `size` logical pixels with a taskbar of
/// `bar` pixels at the bottom.
pub fn grid(size: (f32, f32), bar: f32, icon_size: IconSize) -> Grid {
    let icon = icon_size.pixels();
    let grow = 1.0 + (text_scale() - 1.0) * 0.6;
    let width = match icon_size {
        IconSize::Small => 76.0,
        IconSize::Medium => 86.0,
        IconSize::Large => 128.0,
    } * grow;
    let height = icon + 10.0 + 2.0 * label_style().line_height() + 8.0;
    let rows = (((size.1 - bar - 2.0 * MARGIN) / height).floor() as usize).max(1);
    let columns = (((size.0 - 2.0 * MARGIN) / width).floor() as usize).max(1);
    Grid { cell: (width.round(), height.round()), columns, rows, icon }
}

impl Grid {
    /// The rectangle of the cell at (column, row); fractions are allowed.
    pub fn rect(&self, place: (f32, f32)) -> Rect {
        Rect::new(MARGIN + place.0 * self.cell.0, MARGIN + place.1 * self.cell.1, self.cell.0, self.cell.1)
    }

    /// The grid place under a point (fractions).
    pub fn place_at(&self, point: (f32, f32)) -> (f32, f32) {
        ((point.0 - MARGIN) / self.cell.0 - 0.5, (point.1 - MARGIN) / self.cell.1 - 0.5)
    }

    fn clamp(&self, place: (f32, f32)) -> (f32, f32) {
        (place.0.clamp(0.0, (self.columns - 1) as f32), place.1.clamp(0.0, (self.rows - 1) as f32))
    }
}

/// The order of the icons: the system icons in their order, then folders,
/// then files, each by the chosen key.
pub fn order(items: &[Icon], entries: &[Entry], sort: SortKey) -> Vec<usize> {
    let mut indices: Vec<usize> = (0..items.len()).collect();
    indices.sort_by(|a, b| {
        let (x, y) = (&items[*a], &items[*b]);
        match (x.system, y.system) {
            (Some(_), None) => return std::cmp::Ordering::Less,
            (None, Some(_)) => return std::cmp::Ordering::Greater,
            (Some(_), Some(_)) => return a.cmp(b),
            (None, None) => {}
        }
        let (Some(e), Some(f)) = (x.entry.map(|i| &entries[i]), y.entry.map(|i| &entries[i])) else {
            return a.cmp(b);
        };
        let folders = f.is_dir.cmp(&e.is_dir);
        let by_name = files::natural_cmp(&x.label, &y.label);
        folders.then_with(|| match sort {
            SortKey::Name => by_name,
            SortKey::Size => e.size.cmp(&f.size).then(by_name),
            SortKey::Type => e.kind.label.to_lowercase().cmp(&f.kind.label.to_lowercase()).then(by_name),
            SortKey::Modified => f.modified.cmp(&e.modified).then(by_name),
        })
    });
    indices
}

/// Where each icon goes (grid places), in the order of `items`. With auto
/// arrange the icons fill the grid column by column in `order`; otherwise
/// icons with a kept place go there (aligned to the grid when asked, and
/// never two in one cell), and the others fill the first free cells.
pub fn arrange(items: &[Icon], order: &[usize], saved: &HashMap<String, (f32, f32)>, settings: &DesktopIcons, grid: &Grid) -> Vec<(f32, f32)> {
    let mut places = vec![(0.0, 0.0); items.len()];
    let rows = grid.rows.max(1);
    let cell = |n: usize| ((n / rows) as f32, (n % rows) as f32);
    if settings.auto_arrange {
        for (n, index) in order.iter().enumerate() {
            places[*index] = cell(n);
        }
        return places;
    }
    let mut taken: HashSet<(i64, i64)> = HashSet::new();
    let mut waiting = Vec::new();
    for index in order {
        match saved.get(&items[*index].key) {
            Some(place) => {
                let mut place = grid.clamp(*place);
                if settings.align_to_grid {
                    place = (place.0.round(), place.1.round());
                }
                let slot = (place.0.round() as i64, place.1.round() as i64);
                if taken.insert(slot) {
                    places[*index] = place;
                } else {
                    waiting.push(*index);
                }
            }
            None => waiting.push(*index),
        }
    }
    let mut next = 0;
    for index in waiting {
        while taken.contains(&(((next / rows) as i64), ((next % rows) as i64))) {
            next += 1;
        }
        taken.insert(((next / rows) as i64, (next % rows) as i64));
        places[index] = cell(next);
        next += 1;
    }
    places
}

/// The icons a band from `a` to `b` touches.
pub fn in_band(rects: &[(String, Rect)], a: (f32, f32), b: (f32, f32)) -> Vec<String> {
    let band = Rect::new(a.0.min(b.0), a.1.min(b.1), (a.0 - b.0).abs(), (a.1 - b.1).abs());
    rects
        .iter()
        .filter(|(_, r)| r.x < band.right() && band.x < r.right() && r.y < band.bottom() && band.y < r.bottom())
        .map(|(k, _)| k.clone())
        .collect()
}

// ---------------------------------------------------------------------------
// The model
// ---------------------------------------------------------------------------

fn system_label(icon: SystemIcon) -> &'static str {
    match icon {
        SystemIcon::Home => "Home",
        SystemIcon::Trash => "Trash",
        SystemIcon::Computer => "This device",
    }
}

fn system_icons(icon: SystemIcon, trash_full: bool) -> &'static [&'static str] {
    match icon {
        SystemIcon::Home => &["user-home", "folder"],
        SystemIcon::Trash if trash_full => &["user-trash-full", "user-trash"],
        SystemIcon::Trash => &["user-trash", "user-trash-full"],
        SystemIcon::Computer => &["computer", "boswas-device", "drive-harddisk"],
    }
}

/// The desktop's icons, positioned.
pub struct View {
    pub grid: Grid,
    pub items: Vec<Icon>,
    /// Each item's rectangle (on the output), in the order of `items`.
    pub rects: Vec<Rect>,
    pub order: Vec<usize>,
}

impl State {
    /// Whether the desktop shows icons at all.
    pub fn desk_shown(&self) -> bool {
        self.mode == Mode::Desktop && self.desktop_known && self.desktop.icons.icons
    }

    /// The icons, with their file's index.
    pub fn desk_items(&self) -> Vec<Icon> {
        let mut items: Vec<Icon> = self
            .desktop
            .icons
            .system_icons
            .iter()
            .map(|icon| Icon { key: format!("system:{}", icon.key()), label: system_label(*icon).into(), system: Some(*icon), entry: None })
            .collect();
        if self.desk.dir.as_ref().is_some_and(|d| !d.disabled) {
            for (index, entry) in self.desk.entries.iter().enumerate() {
                let label = entry.launcher.as_ref().map_or_else(|| entry.name.clone(), |(name, _)| name.clone());
                items.push(Icon { key: entry.key(), label, system: None, entry: Some(index) });
            }
        }
        items
    }

    /// The icons laid out on an output.
    pub fn desk_view(&self, output: (f32, f32)) -> View {
        let bar = if self.desktop.taskbar.auto_hide { 0.0 } else { tokens().shell("taskbar-height") };
        let grid = grid(output, bar, self.desktop.icons.size);
        let items = self.desk_items();
        let order = order(&items, &self.desk.entries, self.desk.saved.sort);
        let places = arrange(&items, &order, &self.desk.saved.positions, &self.desktop.icons, &grid);
        let rects = places.iter().map(|p| grid.rect(*p)).collect();
        View { grid, items, rects, order }
    }
}

impl View {
    pub fn find(&self, key: &str) -> Option<usize> {
        self.items.iter().position(|i| i.key == key)
    }

    /// The icon under a point.
    pub fn at(&self, point: (f32, f32)) -> Option<usize> {
        self.rects.iter().position(|r| r.inset(4.0).contains(point))
    }

    /// The icons that are on the screen, as keys and rectangles.
    fn visible(&self, size: (f32, f32)) -> Vec<(String, Rect)> {
        self.items.iter().zip(&self.rects).filter(|(_, r)| r.right() <= size.0 + 0.5 && r.bottom() <= size.1 + 0.5).map(|(i, r)| (i.key.clone(), *r)).collect()
    }
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

/// Text with a shadow, readable on any wallpaper.
fn shadowed(f: &mut Frame, text: &str, rect: Rect, style: &TextStyle) {
    let shadow = TextStyle { color: Rgba::BLACK.alpha(0.75), ..*style };
    f.res.text.draw_centered(f.canvas, text, rect.x + 1.0, rect.y + 1.0, rect.w, rect.h, &shadow);
    let soft = TextStyle { color: Rgba::BLACK.alpha(0.35), ..*style };
    f.res.text.draw_centered(f.canvas, text, rect.x, rect.y + 1.5, rect.w, rect.h, &soft);
    f.res.text.draw_centered(f.canvas, text, rect.x, rect.y, rect.w, rect.h, style);
}

/// The desktop's icons on the wallpaper of an output of `size`.
pub fn draw(f: &mut Frame, st: &State, size: (f32, f32)) {
    if !st.desk_shown() {
        return;
    }
    let theme = f.theme();
    let view = st.desk_view(size);
    let desk = &st.desk;
    let trash_full = desk.trash_full;
    let drag_offset = desk.drag.as_ref().map(|d| (d.to.0 - d.from.0, d.to.1 - d.from.1));
    let style = label_style();
    for (index, item) in view.items.iter().enumerate() {
        let rect = view.rects[index];
        if rect.right() > size.0 + 0.5 || rect.bottom() > size.1 + 0.5 {
            continue;
        }
        let touch = f.area(rect.inset(2.0), Action::Desk(Act::Icon(item.key.clone())));
        let entry = item.entry.map(|i| &desk.entries[i]);
        f.tip(&tooltip(item, entry));
        let selected = desk.selected.contains(&item.key);
        let dragged = drag_offset.is_some() && desk.drag.as_ref().is_some_and(|d| d.keys.contains(&item.key));
        let target = desk.drop_target.as_deref() == Some(item.key.as_str());
        let body = rect.inset(2.0);
        let radius = tokens().radius_control;
        if selected || target {
            f.canvas.fill_rounded_rect(body.x, body.y, body.w, body.h, radius, theme.accent.alpha(if target { 0.5 } else { 0.32 }));
            f.canvas.stroke_rounded_rect(body.x, body.y, body.w, body.h, radius, 1.0, theme.accent.alpha(0.7));
        } else if touch.hovered && desk.drag.is_none() {
            f.canvas.fill_rounded_rect(body.x, body.y, body.w, body.h, radius, Rgba::WHITE.alpha(0.14));
        }
        if desk.keyboard && desk.cursor.as_deref() == Some(item.key.as_str()) {
            f.canvas.stroke_rounded_rect(body.x + 1.0, body.y + 1.0, body.w - 2.0, body.h - 2.0, radius, 1.5, Rgba::WHITE.alpha(0.9));
        }
        let faded = desk.cut.contains(&item.key) || dragged;
        draw_icon(f, st, item, entry, rect, view.grid.icon, trash_full, faded);
        if desk.renaming.as_ref().is_some_and(|r| r.key == item.key) {
            continue;
        }
        let lines = if selected { 4 } else { 2 };
        let label_top = rect.y + 6.0 + view.grid.icon + 4.0;
        let width = rect.w - 6.0;
        let wrapped = f.res.text.wrap(&item.label, &style, width, lines);
        for (n, line) in wrapped.iter().enumerate() {
            let y = label_top + n as f32 * style.line_height();
            if y + style.line_height() > size.1 {
                break;
            }
            shadowed(f, line, Rect::new(rect.x + 3.0, y, width, style.line_height()), &style);
        }
    }
    // The dragged icons follow the pointer.
    if let (Some(drag), Some((dx, dy))) = (&desk.drag, drag_offset) {
        for key in &drag.keys {
            let Some(index) = view.find(key) else {
                continue;
            };
            let rect = view.rects[index];
            let moved = Rect::new(rect.x + dx, rect.y + dy, rect.w, rect.h);
            let item = &view.items[index];
            draw_icon(f, st, item, item.entry.map(|i| &desk.entries[i]), moved, view.grid.icon, trash_full, false);
        }
    }
    // The selection band.
    if let Some((a, b)) = desk.band {
        let band = Rect::new(a.0.min(b.0), a.1.min(b.1), (a.0 - b.0).abs(), (a.1 - b.1).abs());
        if band.w >= 1.0 && band.h >= 1.0 {
            f.canvas.fill_rect(band.x, band.y, band.w, band.h, theme.accent.alpha(0.22));
            f.canvas.stroke_rounded_rect(band.x, band.y, band.w, band.h, 0.0, 1.0, theme.accent);
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn draw_icon(f: &mut Frame, st: &State, item: &Icon, entry: Option<&Entry>, rect: Rect, size: f32, trash_full: bool, faded: bool) {
    let x = rect.x + (rect.w - size) / 2.0;
    let y = rect.y + 6.0;
    let mut drawn = false;
    if let Some(entry) = entry {
        if let Some((_, _, thumbnail)) = st.desk.thumbnails.get(&item.key).filter(|(m, s, _)| *m == entry.modified && *s == entry.size) {
            let (tw, th) = (thumbnail.width() as f32, thumbnail.height() as f32);
            let scale = (size / tw).min(size / th);
            let (w, h) = (tw * scale, th * scale);
            let (px, py) = (x + (size - w) / 2.0, y + (size - h) / 2.0);
            f.canvas.fill_rect(px - 1.0, py - 1.0, w + 2.0, h + 2.0, Rgba::WHITE.alpha(0.8));
            f.canvas.draw_pixmap(thumbnail, px, py, w, h, if faded { 0.5 } else { 1.0 });
            drawn = true;
        }
    }
    if !drawn {
        let mut names: Vec<&str> = Vec::new();
        if let Some((_, icon)) = entry.and_then(|e| e.launcher.as_ref()) {
            names.push(icon);
        }
        match (item.system, entry) {
            (Some(icon), _) => names.extend_from_slice(system_icons(icon, trash_full)),
            (None, Some(entry)) => names.extend_from_slice(entry.kind.icons),
            _ => {}
        }
        if faded {
            // Drawn into a picture of its own, then composed at half strength.
            let px = (size * f.canvas.scale()).round().max(1.0) as u32;
            if let Some(pixmap) = names.iter().find_map(|n| f.res.icons.load(n, px)) {
                f.canvas.draw_pixmap(&pixmap, x, y, size, size, 0.5);
            }
        } else {
            f.icon_or_placeholder(&names, x, y, size);
        }
    }
    if entry.is_some_and(|e| e.is_link) {
        let badge = (size * 0.4).clamp(12.0, 24.0);
        f.canvas.fill_rounded_rect(x, y + size - badge, badge, badge, 3.0, Rgba::WHITE.alpha(0.92));
        let c = Rgba::BLACK.alpha(0.85);
        let (bx, by) = (x + badge * 0.25, y + size - badge * 0.25);
        f.canvas.line(bx, by, bx + badge * 0.5, by - badge * 0.5, 1.5, c);
        f.canvas.line(bx + badge * 0.2, by - badge * 0.5, bx + badge * 0.5, by - badge * 0.5, 1.5, c);
        f.canvas.line(bx + badge * 0.5, by - badge * 0.5, bx + badge * 0.5, by - badge * 0.2, 1.5, c);
    }
}

fn tooltip(item: &Icon, entry: Option<&Entry>) -> String {
    match (item.system, entry) {
        (Some(SystemIcon::Home), _) => "Your personal folder".into(),
        (Some(SystemIcon::Trash), _) => "Files you deleted, until you empty the Trash".into(),
        (Some(SystemIcon::Computer), _) => "The drives and folders of this device".into(),
        (None, Some(entry)) => {
            let when = chrono::DateTime::from_timestamp(entry.modified, 0).map(|t| t.with_timezone(&chrono::Local).format("%-d %B %Y %H:%M").to_string()).unwrap_or_default();
            let kind = if entry.is_link { format!("{} (link)", entry.kind.label) } else { entry.kind.label.clone() };
            if entry.is_dir {
                format!("{}\nType: {kind}\nDate modified: {when}", entry.name)
            } else {
                format!("{}\nType: {kind}\nSize: {}\nDate modified: {when}", entry.name, files::format_size(entry.size))
            }
        }
        _ => item.label.clone(),
    }
}

/// The rename field over an icon's label.
pub fn draw_rename(f: &mut Frame, st: &State) {
    let Some(renaming) = &st.desk.renaming else {
        return;
    };
    let (w, h) = (f.canvas.width(), f.canvas.height());
    let field = Rect::new(PAD, PAD, w - 2.0 * PAD, h - 2.0 * PAD);
    f.area(Rect::new(0.0, 0.0, w, h), Action::None);
    f.text_box(field, &renaming.field, "", true, false, 6.0, 6.0);
    if renaming.fresh {
        // The selected name, as a highlight over its letters.
        let style = ramp("body", f.theme().text);
        let name = renaming.field.text();
        let stem = &name[..name.len() - renaming.extension.len().min(name.len())];
        let width = f.res.text.measure(stem, &style).0.min(field.w - 12.0);
        let accent = f.theme().accent.alpha(0.35);
        f.canvas.fill_rect(field.x + 6.0, field.y + 4.0, width.max(2.0).round(), field.h - 8.0, accent);
    }
}

// ---------------------------------------------------------------------------
// Behaviour
// ---------------------------------------------------------------------------

/// The user's compositor settings file (where the View menu writes).
fn user_settings_path() -> Option<PathBuf> {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
        .map(|dir| dir.join("boswas").join("compositor.toml"))
}

impl Shell {
    fn desk_sender(&mut self) -> Option<Sender<Msg>> {
        if self.st.desk.sender.is_none() {
            let (sender, receiver) = channel::channel::<Msg>();
            let inserted = self.loop_handle.insert_source(receiver, |event, _, shell| {
                if let channel::Event::Msg(message) = event {
                    shell.desk_message(message);
                }
            });
            if inserted.is_err() {
                return None;
            }
            self.st.desk.sender = Some(sender);
        }
        self.st.desk.sender.clone()
    }

    /// Runs file work on a worker thread; its result comes back as a message.
    fn desk_job(&mut self, name: &str, job: impl FnOnce() -> Msg + Send + 'static) {
        let Some(sender) = self.desk_sender() else {
            return;
        };
        let spawned = std::thread::Builder::new().name(name.into()).spawn(move || {
            let _ = sender.send(job());
        });
        if spawned.is_err() {
            tracing::warn!("could not start a worker for the desktop icons");
        }
    }

    /// The wallpaper surface that shows the icons: the primary output's.
    fn desk_surface(&self) -> Option<usize> {
        let output = self.primary_output()?;
        self.surfaces.iter().position(|s| s.role == Role::Wallpaper && s.output == output)
    }

    fn desk_view_now(&self) -> Option<(usize, View)> {
        let index = self.desk_surface()?;
        let surface = &self.surfaces[index];
        Some((index, self.st.desk_view((surface.width as f32, surface.height as f32))))
    }

    /// Whether the wallpaper takes the keyboard (to move between icons).
    pub fn desk_keyboard(&self) -> KeyboardInteractivity {
        if self.st.desk_shown() {
            KeyboardInteractivity::OnDemand
        } else {
            KeyboardInteractivity::None
        }
    }

    /// The settings arrived or changed: follow them.
    pub fn desk_settings_changed(&mut self) {
        let keyboard = self.desk_keyboard();
        for surface in self.surfaces.iter_mut().filter(|s| s.role == Role::Wallpaper) {
            if let crate::Handle::Layer(layer) = &surface.handle {
                layer.set_keyboard_interactivity(keyboard);
                layer.commit();
            }
        }
        if self.st.desk_shown() {
            if self.st.desk.dir.is_none() {
                self.st.desk.dir = files::session_desktop_dir();
                if let Some(dir) = self.st.desk.dir.as_ref().filter(|d| d.configured && !d.disabled) {
                    // xdg-user-dirs makes the folders it names; so does the shell.
                    let _ = std::fs::create_dir_all(&dir.path);
                }
            }
            self.desk_watch();
            if !self.st.desk.scanned {
                self.desk_rescan();
            }
        }
        self.mark(Role::Wallpaper);
    }

    /// Reads the folder again on a worker thread.
    pub fn desk_rescan(&mut self) {
        if self.st.desk.scanning {
            self.st.desk.rescan = true;
            return;
        }
        let Some(dir) = self.st.desk.dir.clone().filter(|d| !d.disabled) else {
            self.st.desk.scanned = true;
            return;
        };
        self.st.desk.scanning = true;
        let known: HashMap<String, (i64, u64)> = self.st.desk.thumbnails.iter().map(|(k, (m, s, _))| (k.clone(), (*m, *s))).collect();
        let load_saved = !self.st.desk.saved_loaded;
        let uid = rustix::process::geteuid().as_raw();
        let locale = self.st.locale.clone();
        let px = (self.st.desktop.icons.size.pixels() * 2.0) as u32;
        self.desk_job("desktop-scan", move || {
            let entries = match files::scan(&dir.path, uid, &locale) {
                Ok(entries) => Ok(entries),
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
                Err(err) => Err(err.to_string()),
            };
            let mut thumbnails = Vec::new();
            if let Ok(entries) = &entries {
                for entry in entries.iter().filter(|e| e.kind.picture && !e.is_link && e.size <= 20 * 1024 * 1024) {
                    if known.get(&entry.key()) == Some(&(entry.modified, entry.size)) || thumbnails.len() >= 200 {
                        continue;
                    }
                    if let Some(picture) = crate::widget_sources::thumbnail(&dir.path.join(&entry.os_name), px.max(64)) {
                        thumbnails.push((entry.key(), entry.modified, entry.size, picture));
                    }
                }
            }
            let saved = load_saved.then(|| files::layout_path().map(|p| Saved::read(&p)).unwrap_or_default());
            let trash_full = files::home_trash().is_some_and(|t| std::fs::read_dir(t.join("files")).map(|mut d| d.next().is_some()).unwrap_or(false));
            Msg::Scanned { entries, thumbnails, saved, trash_full }
        });
    }

    /// Starts watching the folder (once).
    fn desk_watch(&mut self) {
        if self.st.desk.watching {
            return;
        }
        let (Some(dir), Some(sender)) = (self.st.desk.dir.clone().filter(|d| !d.disabled), self.desk_sender()) else {
            return;
        };
        self.st.desk.watching = true;
        let _ = std::thread::Builder::new().name("desktop-watch".into()).spawn(move || watch(&dir.path, &sender));
    }

    pub fn desk_message(&mut self, message: Msg) {
        match message {
            Msg::Scanned { entries, thumbnails, saved, trash_full } => {
                self.st.desk.scanning = false;
                self.st.desk.scanned = true;
                self.st.desk.trash_full = trash_full;
                if let Some(saved) = saved {
                    self.st.desk.saved = saved;
                    self.st.desk.saved_loaded = true;
                }
                match entries {
                    Ok(entries) => self.desk_take_entries(entries),
                    Err(problem) => tracing::warn!("the desktop folder cannot be read: {problem}"),
                }
                for (key, modified, size, picture) in thumbnails {
                    self.st.desk.thumbnails.insert(key, (modified, size, Rc::new(picture)));
                }
                if std::mem::take(&mut self.st.desk.rescan) {
                    self.desk_rescan();
                }
            }
            Msg::Changed => self.desk_rescan(),
            Msg::Created { name, rename } => match name {
                Ok(name) => {
                    let key = format!("file:{name}");
                    let place = self.desk_arrival_place();
                    if let Some(place) = place {
                        self.st.desk.arrivals.push((key.clone(), place));
                    }
                    self.st.desk.selected = vec![key.clone()];
                    self.st.desk.cursor = Some(key);
                    if rename {
                        self.st.desk.rename_next = Some(name);
                    }
                    self.desk_rescan();
                }
                Err(problem) => self.notice("The item could not be created", &problem, None),
            },
            Msg::Renamed { key, name, result } => match result {
                Ok(()) => {
                    let new_key = format!("file:{name}");
                    if let Some(place) = self.st.desk.saved.positions.remove(&key) {
                        self.st.desk.saved.positions.insert(new_key.clone(), place);
                        self.desk_save_layout();
                    }
                    self.st.desk.selected = vec![new_key.clone()];
                    self.st.desk.cursor = Some(new_key);
                    self.desk_rescan();
                }
                Err(problem) => self.notice("The item could not be renamed", &problem, None),
            },
            Msg::Trashed { failed } => {
                if let Some((name, problem)) = failed.first() {
                    let more = if failed.len() > 1 { format!(" ({} items could not be moved)", failed.len()) } else { String::new() };
                    self.notice("Not moved to the Trash", &format!("{name}: {problem}{more}"), None);
                }
                self.desk_rescan();
            }
            Msg::Transferred { report, cut } => {
                if let Some((name, problem)) = report.failed.first() {
                    self.notice("Some items could not be copied", &format!("{name}: {problem}"), None);
                }
                if cut && report.failed.is_empty() {
                    self.desk_drop_clip();
                }
                if let Some(place) = self.st.desk.drop_place.take() {
                    for (n, name) in report.done.iter().enumerate() {
                        self.st.desk.arrivals.push((format!("file:{name}"), (place.0, place.1 + n as f32)));
                    }
                }
                self.st.desk.selected = report.done.iter().map(|n| format!("file:{n}")).collect();
                self.desk_rescan();
            }
            Msg::Dropped { paths, target, moving } => {
                if let Some(offer) = self.st.desk.finishing.take() {
                    offer.finish();
                    offer.destroy();
                }
                self.desk_transfer(paths, target, moving, false);
            }
            Msg::ViewSet(result) => match result {
                Ok(()) => {
                    self.send_control("reload-config");
                }
                Err(problem) => self.notice("The desktop's view could not be saved", &format!("{problem}. Use Settings > Personalization instead."), None),
            },
        }
        self.mark(Role::Wallpaper);
    }

    /// A new scan: keep the selection and places of what is still there,
    /// and put new items where they were dropped.
    fn desk_take_entries(&mut self, entries: Vec<Entry>) {
        // Where the icons are now stays where they are: new ones go to free
        // cells instead of pushing the others along (as on Windows).
        if self.st.desk.shown_once && !self.st.desktop.icons.auto_arrange && self.desk_fix_places() {
            self.desk_save_layout();
        }
        self.st.desk.shown_once = true;
        self.st.desk.entries = entries;
        let keys: HashSet<String> = self.st.desk_items().into_iter().map(|i| i.key).collect();
        self.st.desk.selected.retain(|k| keys.contains(k));
        self.st.desk.cut.retain(|k| keys.contains(k));
        self.st.desk.thumbnails.retain(|k, _| keys.contains(k));
        if self.st.desk.cursor.as_ref().is_some_and(|k| !keys.contains(k)) {
            self.st.desk.cursor = None;
        }
        let arrivals = std::mem::take(&mut self.st.desk.arrivals);
        let mut placed = false;
        for (key, place) in arrivals {
            if keys.contains(&key) {
                if !self.st.desktop.icons.auto_arrange {
                    let place = self.desk_view_now().map_or(place, |(_, view)| view.grid.clamp(place));
                    self.st.desk.saved.positions.insert(key, place);
                    placed = true;
                }
            } else {
                self.st.desk.arrivals.push((key, place));
            }
        }
        if placed {
            self.desk_save_layout();
        }
        if let Some(name) = self.st.desk.rename_next.take() {
            let key = format!("file:{name}");
            if keys.contains(&key) {
                self.desk_start_rename(&key);
            } else {
                self.st.desk.rename_next = Some(name);
            }
        }
    }

    /// Writes down where every icon is now, so that new ones do not push
    /// them around. Returns whether a place was added.
    fn desk_fix_places(&mut self) -> bool {
        let Some((_, view)) = self.desk_view_now() else {
            return false;
        };
        let mut added = false;
        for (item, rect) in view.items.iter().zip(&view.rects) {
            if !self.st.desk.saved.positions.contains_key(&item.key) {
                let place = view.grid.place_at(rect.center());
                self.st.desk.saved.positions.insert(item.key.clone(), (place.0.max(0.0), place.1.max(0.0)));
                added = true;
            }
        }
        added
    }

    fn desk_save_layout(&mut self) {
        // Each save has a number; a worker that comes late with an older
        // one does not write over a newer one.
        static LATEST: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let number = LATEST.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
        let text = self.st.desk.saved.to_json();
        let Some(path) = files::layout_path() else {
            return;
        };
        let _ = std::thread::Builder::new().name("desktop-layout".into()).spawn(move || {
            if LATEST.load(std::sync::atomic::Ordering::SeqCst) != number {
                return;
            }
            if let Err(err) = files::write_private(&path, &text) {
                tracing::warn!("{}: {err}", path.display());
            }
        });
    }

    /// The grid place for something about to appear: where the menu was
    /// opened, else none (the first free cell).
    fn desk_arrival_place(&mut self) -> Option<(f32, f32)> {
        let point = self.st.desk.menu_point.take()?;
        let (_, view) = self.desk_view_now()?;
        let mut place = view.grid.clamp(view.grid.place_at(point));
        if self.st.desktop.icons.align_to_grid {
            place = (place.0.round(), place.1.round());
        }
        Some(place)
    }

    // -- the pointer ---------------------------------------------------------

    /// A pointer event on a wallpaper. Returns whether the desktop took it.
    pub fn desk_pointer(&mut self, index: usize, event: &PointerEvent) -> bool {
        if !self.st.desk_shown() || self.desk_surface() != Some(index) || !self.st.menus.is_empty() {
            return false;
        }
        let position = (event.position.0 as f32, event.position.1 as f32);
        match event.kind {
            PointerEventKind::Press { button, serial, .. } => {
                self.serial = serial;
                self.hide_tooltip();
                if self.st.desk.renaming.is_some() {
                    self.desk_commit_rename();
                }
                let Some((_, view)) = self.desk_view_now() else {
                    return false;
                };
                let key = view.at(position).map(|i| view.items[i].key.clone());
                let (ctrl, shift) = (self.modifiers.ctrl, self.modifiers.shift);
                self.st.desk.keyboard = false;
                match button {
                    BTN_LEFT => self.desk_press(&view, position, key, ctrl, shift),
                    BTN_RIGHT => {
                        self.close_transients();
                        let output = self.surfaces[index].output.clone();
                        let at = (self.surfaces[index].origin.0 + position.0, self.surfaces[index].origin.1 + position.1);
                        self.st.desk.menu_point = Some(position);
                        let items = match key {
                            Some(key) => {
                                if !self.st.desk.selected.contains(&key) {
                                    self.st.desk.selected = vec![key.clone()];
                                }
                                self.st.desk.cursor = Some(key);
                                self.desk_item_menu()
                            }
                            None => {
                                self.st.desk.selected.clear();
                                self.desk_background_menu()
                            }
                        };
                        self.mark(Role::Wallpaper);
                        self.open_menu(items, at.0, at.1, false, &output);
                    }
                    _ => {}
                }
                true
            }
            PointerEventKind::Motion { .. } => {
                if self.st.desk.press.is_some() {
                    self.desk_motion(position);
                }
                false
            }
            PointerEventKind::Release { button, serial, .. } => {
                self.serial = serial;
                if button == BTN_LEFT {
                    self.desk_release(position);
                }
                true
            }
            _ => false,
        }
    }

    fn desk_press(&mut self, view: &View, position: (f32, f32), key: Option<String>, ctrl: bool, shift: bool) {
        if let Some(key) = &key {
            // A second click on the same icon opens it.
            let double = !ctrl && !shift && self.st.desk.last_click.as_ref().is_some_and(|(k, at)| k == key && at.elapsed() <= DOUBLE_CLICK);
            if double {
                self.st.desk.last_click = None;
                self.st.desk.press = None;
                if !self.st.desk.selected.contains(key) {
                    self.st.desk.selected = vec![key.clone()];
                }
                self.mark(Role::Wallpaper);
                return self.desk_open();
            }
        }
        let desk = &mut self.st.desk;
        match &key {
            Some(key) => {
                desk.last_click = Some((key.clone(), Instant::now()));
                if ctrl {
                    match desk.selected.iter().position(|k| k == key) {
                        Some(at) => {
                            desk.selected.remove(at);
                        }
                        None => desk.selected.push(key.clone()),
                    }
                    desk.anchor = Some(key.clone());
                } else if shift {
                    let anchor = desk.anchor.clone().unwrap_or_else(|| key.clone());
                    desk.selected = range(view, &anchor, key);
                } else if !desk.selected.contains(key) {
                    desk.selected = vec![key.clone()];
                    desk.anchor = Some(key.clone());
                }
                desk.cursor = Some(key.clone());
            }
            None => {
                desk.last_click = None;
                if !ctrl && !shift {
                    desk.selected.clear();
                }
                desk.band_base = desk.selected.clone();
                desk.band = Some((position, position));
            }
        }
        desk.press = Some(Press { at: position, key, ctrl, shift });
        self.mark(Role::Wallpaper);
    }

    fn desk_motion(&mut self, position: (f32, f32)) {
        let Some(press) = self.st.desk.press.clone() else {
            return;
        };
        if self.st.desk.band.is_some() {
            let size = self.desk_surface().map(|i| (self.surfaces[i].width as f32, self.surfaces[i].height as f32)).unwrap_or((0.0, 0.0));
            let Some((_, view)) = self.desk_view_now() else {
                return;
            };
            let touched = in_band(&view.visible(size), press.at, position);
            let desk = &mut self.st.desk;
            desk.band = Some((press.at, position));
            let mut selected = if press.ctrl || press.shift { desk.band_base.clone() } else { Vec::new() };
            for key in touched {
                if !selected.contains(&key) {
                    selected.push(key);
                }
            }
            desk.selected = selected;
            return self.mark(Role::Wallpaper);
        }
        let Some(key) = press.key.clone() else {
            return;
        };
        let moved = ((position.0 - press.at.0).powi(2) + (position.1 - press.at.1).powi(2)).sqrt();
        if self.st.desk.drag.is_none() && moved < DRAG_START {
            return;
        }
        let keys = if self.st.desk.selected.contains(&key) { self.st.desk.selected.clone() } else { vec![key] };
        self.hide_tooltip();
        let target = self.desk_view_now().and_then(|(_, view)| view.at(position).map(|i| view.items[i].clone())).filter(|item| !keys.contains(&item.key) && self.desk_accepts_drop(item)).map(|item| item.key);
        self.st.desk.drop_target = target;
        self.st.desk.drag = Some(Drag { keys, from: press.at, to: position });
        self.mark(Role::Wallpaper);
    }

    /// Whether files can be dropped on an icon (a folder or the Trash).
    fn desk_accepts_drop(&self, item: &Icon) -> bool {
        match (item.system, item.entry) {
            (Some(SystemIcon::Trash), _) => true,
            (None, Some(index)) => self.st.desk.entries.get(index).is_some_and(|e| e.is_dir && !e.is_link),
            _ => false,
        }
    }

    fn desk_release(&mut self, position: (f32, f32)) {
        let press = self.st.desk.press.take();
        self.st.desk.band = None;
        if let Some(drag) = self.st.desk.drag.take() {
            let target = self.st.desk.drop_target.take();
            self.st.desk.last_click = None;
            self.desk_finish_drag(drag, target, position);
        } else if let Some(Press { key: Some(key), ctrl: false, shift: false, .. }) = press {
            // A click on one of several selected icons selects it alone.
            if self.st.desk.selected.len() > 1 {
                self.st.desk.selected = vec![key.clone()];
                self.st.desk.anchor = Some(key);
            }
        }
        self.mark(Role::Wallpaper);
    }

    fn desk_finish_drag(&mut self, drag: Drag, target: Option<String>, position: (f32, f32)) {
        let Some((_, view)) = self.desk_view_now() else {
            return;
        };
        let dir = self.st.desk.dir.clone();
        if let (Some(target), Some(dir)) = (target, dir) {
            let Some(item) = view.find(&target).map(|i| view.items[i].clone()) else {
                return;
            };
            let names: Vec<OsString> = drag.keys.iter().filter_map(|k| view.find(k)).filter_map(|i| view.items[i].entry).filter_map(|e| self.st.desk.entries.get(e)).map(|e| e.os_name.clone()).collect();
            if item.system == Some(SystemIcon::Trash) {
                let paths = names.iter().map(|n| dir.path.join(n)).collect();
                return self.desk_trash(paths);
            }
            if let Some(folder) = item.entry.and_then(|e| self.st.desk.entries.get(e)).map(|e| e.os_name.clone()) {
                self.desk_job("desktop-move", move || Msg::Transferred { report: files::move_into(&dir.path, &names, &folder), cut: false });
            }
            return;
        }
        if self.st.desktop.icons.auto_arrange {
            return;
        }
        // A new place: the same offset for every dragged icon.
        let delta = ((position.0 - drag.from.0) / view.grid.cell.0, (position.1 - drag.from.1) / view.grid.cell.1);
        let _ = self.desk_fix_places();
        let align = self.st.desktop.icons.align_to_grid;
        let mut taken: HashSet<(i64, i64)> = view
            .items
            .iter()
            .zip(&view.rects)
            .filter(|(i, _)| !drag.keys.contains(&i.key))
            .map(|(_, r)| {
                let p = view.grid.place_at(r.center());
                (p.0.round() as i64, p.1.round() as i64)
            })
            .collect();
        for key in &drag.keys {
            let Some(index) = view.find(key) else {
                continue;
            };
            let old = view.grid.place_at(view.rects[index].center());
            let mut place = view.grid.clamp((old.0 + delta.0, old.1 + delta.1));
            if align {
                place = (place.0.round(), place.1.round());
                // The nearest free cell.
                let mut best = place;
                'search: for radius in 0..(view.grid.columns.max(view.grid.rows) as i64) {
                    for dx in -radius..=radius {
                        for dy in -radius..=radius {
                            let cell = (place.0 as i64 + dx, place.1 as i64 + dy);
                            if cell.0 < 0 || cell.1 < 0 || cell.0 >= view.grid.columns as i64 || cell.1 >= view.grid.rows as i64 {
                                continue;
                            }
                            if !taken.contains(&cell) {
                                best = (cell.0 as f32, cell.1 as f32);
                                break 'search;
                            }
                        }
                    }
                }
                place = best;
            }
            taken.insert((place.0.round() as i64, place.1.round() as i64));
            self.st.desk.saved.positions.insert(key.clone(), place);
        }
        self.desk_save_layout();
    }

    // -- the keyboard --------------------------------------------------------

    /// A key while the desktop has the keyboard.
    pub fn desk_key(&mut self, event: &KeyEvent, typed: Option<&str>) {
        if !self.st.desk_shown() {
            return;
        }
        let (ctrl, shift) = (self.modifiers.ctrl, self.modifiers.shift);
        match event.keysym {
            Keysym::Return | Keysym::KP_Enter => self.desk_open(),
            Keysym::F2 => self.desk_command(DeskCmd::Rename),
            Keysym::F5 => self.desk_command(DeskCmd::Refresh),
            Keysym::Delete | Keysym::KP_Delete => self.desk_command(DeskCmd::Delete),
            Keysym::Escape => {
                self.st.desk.selected.clear();
                self.mark(Role::Wallpaper);
            }
            Keysym::a | Keysym::A if ctrl => {
                self.st.desk.selected = self.st.desk_items().into_iter().map(|i| i.key).collect();
                self.mark(Role::Wallpaper);
            }
            Keysym::c | Keysym::C if ctrl => self.desk_command(DeskCmd::Copy),
            Keysym::x | Keysym::X if ctrl => self.desk_command(DeskCmd::Cut),
            Keysym::v | Keysym::V if ctrl => self.desk_command(DeskCmd::Paste),
            Keysym::n | Keysym::N if ctrl && shift => self.desk_command(DeskCmd::NewFolder),
            Keysym::Menu | Keysym::F10 if event.keysym == Keysym::Menu || shift => self.desk_keyboard_menu(),
            Keysym::space if ctrl => {
                if let Some(key) = self.st.desk.cursor.clone() {
                    match self.st.desk.selected.iter().position(|k| *k == key) {
                        Some(at) => {
                            self.st.desk.selected.remove(at);
                        }
                        None => self.st.desk.selected.push(key),
                    }
                    self.mark(Role::Wallpaper);
                }
            }
            Keysym::Home | Keysym::End => {
                let Some((_, view)) = self.desk_view_now() else {
                    return;
                };
                let key = if event.keysym == Keysym::Home { view.order.first() } else { view.order.last() }.map(|i| view.items[*i].key.clone());
                if let Some(key) = key {
                    self.desk_move_cursor(&view, key, ctrl, shift);
                }
            }
            keysym => {
                let nav = match keysym {
                    Keysym::Left => Some(Nav::Left),
                    Keysym::Right => Some(Nav::Right),
                    Keysym::Up => Some(Nav::Up),
                    Keysym::Down => Some(Nav::Down),
                    Keysym::Tab if shift => Some(Nav::Previous),
                    Keysym::ISO_Left_Tab => Some(Nav::Previous),
                    Keysym::Tab => Some(Nav::Next),
                    _ => None,
                };
                let Some((_, view)) = self.desk_view_now() else {
                    return;
                };
                if let Some(nav) = nav {
                    let size = self.desk_surface().map(|i| (self.surfaces[i].width as f32, self.surfaces[i].height as f32)).unwrap_or((0.0, 0.0));
                    let hits: Vec<Hit> = view.visible(size).into_iter().map(|(key, rect)| Hit { rect, action: Action::Desk(Act::Icon(key)), focusable: true, tooltip: None, drag: false }).collect();
                    let current = self.st.desk.cursor.clone().map(|k| Action::Desk(Act::Icon(k)));
                    if let Some(Action::Desk(Act::Icon(key))) = ui::navigate(&hits, current.as_ref(), nav) {
                        self.desk_move_cursor(&view, key, ctrl, shift);
                    }
                } else if let Some(text) = typed.filter(|t| !t.trim().is_empty() && !ctrl) {
                    // Typing goes to the next icon whose name starts so.
                    let lower = text.to_lowercase();
                    let start = self.st.desk.cursor.as_ref().and_then(|k| view.order.iter().position(|i| view.items[*i].key == *k)).map_or(0, |p| p + 1);
                    let found = (0..view.order.len()).map(|n| view.order[(start + n) % view.order.len()]).find(|i| view.items[*i].label.to_lowercase().starts_with(&lower));
                    if let Some(index) = found {
                        let key = view.items[index].key.clone();
                        self.desk_move_cursor(&view, key, false, false);
                    }
                }
            }
        }
    }

    fn desk_move_cursor(&mut self, view: &View, key: String, ctrl: bool, shift: bool) {
        let desk = &mut self.st.desk;
        desk.keyboard = true;
        if shift {
            let anchor = desk.anchor.clone().or_else(|| desk.cursor.clone()).unwrap_or_else(|| key.clone());
            desk.anchor = Some(anchor.clone());
            desk.selected = range(view, &anchor, &key);
        } else if !ctrl {
            desk.selected = vec![key.clone()];
            desk.anchor = Some(key.clone());
        }
        desk.cursor = Some(key);
        self.mark(Role::Wallpaper);
    }

    /// The menu key: the menu of the selection, or of the background.
    fn desk_keyboard_menu(&mut self) {
        let Some((index, view)) = self.desk_view_now() else {
            return;
        };
        let output = self.surfaces[index].output.clone();
        let origin = self.surfaces[index].origin;
        let (items, at) = match self.st.desk.cursor.clone().filter(|k| self.st.desk.selected.contains(k)).and_then(|k| view.find(&k)) {
            Some(i) => (self.desk_item_menu(), view.rects[i].center()),
            None => (self.desk_background_menu(), (MARGIN + 40.0, MARGIN + 40.0)),
        };
        self.st.desk.menu_point = Some(at);
        self.open_menu(items, origin.0 + at.0, origin.1 + at.1, false, &output);
    }

    // -- menus ---------------------------------------------------------------

    /// The menu of the desktop's background.
    pub fn desk_background_menu(&self) -> Vec<Item> {
        background_menu(&self.st, self.desk_clipboard_has_files())
    }

    /// The selected files (not the system icons).
    fn desk_selected_entries(&self) -> Vec<&Entry> {
        selected_entries(&self.st)
    }

    fn desk_selected_paths(&self) -> Vec<PathBuf> {
        let Some(dir) = &self.st.desk.dir else {
            return Vec::new();
        };
        self.desk_selected_entries().iter().map(|e| dir.path.join(&e.os_name)).collect()
    }

    /// The menu of the selected icons, with the applications that open the
    /// first one's type under Open with.
    pub fn desk_item_menu(&self) -> Vec<Item> {
        let apps = match self.desk_selected_entries().first().filter(|e| e.kind.mime != "application/x-desktop" && !e.is_dir) {
            Some(first) => {
                let index = crate::open::Index::load(&crate::open::Dirs::from_env());
                index.associated(first.kind.mime).into_iter().take(12).map(|e| (e.id.clone(), e.name.clone(), e.icon.clone())).collect()
            }
            None => Vec::new(),
        };
        item_menu(&self.st, &apps)
    }

    /// Carries out a menu entry or a key of the desktop.
    pub fn desk_command(&mut self, cmd: DeskCmd) {
        let dir = self.st.desk.dir.clone().filter(|d| !d.disabled);
        match cmd {
            DeskCmd::Size(size) => {
                // Another size has other rows: the icons flow again in their
                // order, as on Windows.
                if self.st.desktop.icons.size != size {
                    self.st.desk.saved.positions.clear();
                    self.desk_save_layout();
                }
                self.desk_set_view("icon_size", Setting::Text(size.key()), |i| i.size = size);
            }
            DeskCmd::AutoArrange => {
                let on = !self.st.desktop.icons.auto_arrange;
                self.desk_set_view("auto_arrange", Setting::Bool(on), |i| i.auto_arrange = on);
            }
            DeskCmd::AlignToGrid => {
                let on = !self.st.desktop.icons.align_to_grid;
                self.desk_set_view("align_to_grid", Setting::Bool(on), |i| i.align_to_grid = on);
            }
            DeskCmd::ShowIcons => {
                let on = !self.st.desktop.icons.icons;
                self.desk_set_view("icons", Setting::Bool(on), |i| i.icons = on);
            }
            DeskCmd::Sort(key) => {
                // Sorting puts every icon in the new order again.
                self.st.desk.saved.sort = key;
                self.st.desk.saved.positions.clear();
                self.desk_save_layout();
            }
            DeskCmd::Refresh => self.desk_rescan(),
            DeskCmd::Paste => self.desk_paste(),
            DeskCmd::NewFolder | DeskCmd::NewTextDocument => {
                let Some(dir) = self.desk_dir_for_writing() else {
                    return;
                };
                let folder = cmd == DeskCmd::NewFolder;
                self.desk_job("desktop-new", move || {
                    let name = if folder { files::create_folder(&dir) } else { files::create_text_document(&dir) };
                    Msg::Created { name: name.map_err(|e| e.to_string()), rename: true }
                });
            }
            DeskCmd::OpenTerminal => {
                let Some(dir) = self.desk_dir_for_writing() else {
                    return;
                };
                let path = dir.to_string_lossy().into_owned();
                let command = [("boswas-terminal", "--working-directory"), ("konsole", "--workdir")].into_iter().find(|(p, _)| crate::apps::program_exists(p));
                match command {
                    Some((program, flag)) => {
                        self.spawn(&[program.to_owned(), flag.to_owned(), path]);
                    }
                    None => self.notice("No terminal is installed", "Install Terminal with the Software Center.", None),
                }
            }
            DeskCmd::Open => self.desk_open(),
            DeskCmd::OpenWith(id) => {
                let paths = self.desk_selected_paths();
                let index = crate::open::Index::load(&crate::open::Dirs::from_env());
                if let Some(entry) = index.entry(&id) {
                    for path in paths.iter().take(16) {
                        if let Some(argv) = crate::open::command(entry, path) {
                            if let Err(err) = crate::apps::run(&argv, entry.terminal) {
                                self.notice("The file could not be opened", &format!("{}: {err}", entry.name), None);
                                break;
                            }
                        }
                    }
                }
            }
            DeskCmd::Cut | DeskCmd::Copy => self.desk_copy(cmd == DeskCmd::Cut),
            DeskCmd::Rename => {
                let key = self.st.desk.cursor.clone().filter(|k| self.st.desk.selected.contains(k)).or_else(|| self.st.desk.selected.first().cloned());
                if let Some(key) = key.filter(|k| k.starts_with("file:")) {
                    self.desk_start_rename(&key);
                }
            }
            DeskCmd::Delete => {
                let paths = self.desk_selected_paths();
                if !paths.is_empty() && dir.is_some() {
                    self.desk_trash(paths);
                }
            }
            DeskCmd::Properties => {
                let mut paths = self.desk_selected_paths();
                let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
                if paths.is_empty() && self.st.desk.selected.iter().any(|k| k == "system:home") {
                    paths.push(home);
                }
                let mut argv = vec!["boswas-files".to_owned()];
                for path in paths.iter().take(32) {
                    argv.push("--select".into());
                    argv.push(path.to_string_lossy().into_owned());
                }
                if argv.len() == 1 {
                    argv.push("computer:///".into());
                }
                self.spawn(&argv);
            }
        }
        self.mark(Role::Wallpaper);
    }

    /// The desktop folder, made when it is not there yet (the first new
    /// folder, paste or drop makes it, as Files would).
    fn desk_dir_for_writing(&mut self) -> Option<PathBuf> {
        let dir = self.st.desk.dir.clone().filter(|d| !d.disabled)?;
        if std::fs::create_dir_all(&dir.path).is_err() {
            self.notice("The desktop folder cannot be created", &dir.path.display().to_string(), None);
            return None;
        }
        if !self.st.desk.watching {
            self.desk_watch();
        }
        Some(dir.path)
    }

    /// Changes a View setting: at once here, and in the user's settings
    /// file (the compositor then reads it again and tells every program).
    fn desk_set_view(&mut self, key: &'static str, value: Setting, apply: impl FnOnce(&mut DesktopIcons)) {
        apply(&mut self.st.desktop.icons);
        self.desk_settings_changed();
        let Some(path) = user_settings_path() else {
            return;
        };
        self.desk_job("desktop-view", move || Msg::ViewSet(files::write_desktop_key(&path, key, &value)));
    }

    /// Opens the selection: folders in Files, files with their default
    /// application, trusted launchers by starting them.
    pub fn desk_open(&mut self) {
        let items = self.st.desk_items();
        let chosen: Vec<Icon> = items.into_iter().filter(|i| self.st.desk.selected.contains(&i.key)).take(16).collect();
        let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"));
        for item in chosen {
            match item.system {
                Some(SystemIcon::Home) => {
                    self.open_path(&home);
                }
                Some(SystemIcon::Trash) => {
                    self.spawn(&["boswas-files".to_owned(), "trash:///".to_owned()]);
                }
                Some(SystemIcon::Computer) => {
                    self.spawn(&["boswas-files".to_owned(), "computer:///".to_owned()]);
                }
                None => {
                    let (Some(entry), Some(dir)) = (item.entry.and_then(|e| self.st.desk.entries.get(e)).cloned(), self.st.desk.dir.clone()) else {
                        continue;
                    };
                    let path = dir.path.join(&entry.os_name);
                    if entry.kind.mime == "application/x-desktop" {
                        self.desk_launch(&path, &entry.name);
                    } else {
                        self.open_path(&path);
                    }
                }
            }
        }
    }

    /// Starts a launcher of the desktop, or opens it as text (with the
    /// reason) when the trust rule does not allow starting it.
    fn desk_launch(&mut self, path: &Path, name: &str) {
        let uid = rustix::process::geteuid().as_raw();
        let trust = std::fs::symlink_metadata(path).map(|m| files::launcher_trust(&m, uid)).unwrap_or(files::Trust::NotRegular);
        if trust == files::Trust::Launch {
            use std::io::Read;
            let mut text = String::new();
            let read = std::fs::File::open(path).and_then(|f| f.take(64 * 1024).read_to_string(&mut text));
            match read.ok().and_then(|_| crate::apps::parse_entry(name, &text, &self.st.locale)) {
                Some(app) => {
                    if let Err(err) = crate::apps::launch(&app) {
                        self.notice("Could not start the application", &format!("{}: {err}", app.name), None);
                    }
                }
                None => self.notice("This launcher does not start a program", name, None),
            }
            return;
        }
        // Opened as text instead, with the default text editor.
        let dirs = crate::open::Dirs::from_env();
        let index = crate::open::Index::load(&dirs);
        let opened = index.default_for(&["text/plain"]).and_then(|entry| crate::open::command(entry, path).map(|argv| (argv, entry.terminal))).is_some_and(|(argv, terminal)| crate::apps::run(&argv, terminal).is_ok());
        let what = if opened { "It was opened as text." } else { "It was not started." };
        self.notice(&format!("“{name}” was not started"), &format!("{what} {}", trust.reason()), None);
    }

    fn desk_trash(&mut self, paths: Vec<PathBuf>) {
        let Some(trash) = files::home_trash() else {
            return;
        };
        for path in &paths {
            let key = format!("file:{}", path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default());
            self.st.desk.saved.positions.remove(&key);
        }
        self.desk_save_layout();
        self.desk_job("desktop-trash", move || {
            let deleted = chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string();
            let failed = paths
                .iter()
                .filter_map(|path| files::trash(path, &trash, &deleted).err().map(|e| (path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(), e)))
                .collect();
            Msg::Trashed { failed }
        });
    }

    fn desk_transfer(&mut self, paths: Vec<PathBuf>, target: Target, moving: bool, cut: bool) {
        if paths.is_empty() {
            return;
        }
        if target == Target::Trash {
            return self.desk_trash(paths);
        }
        let Some(dir) = self.desk_dir_for_writing() else {
            return;
        };
        let dest = match target {
            Target::Folder(name) => dir.join(name),
            _ => dir,
        };
        self.desk_job("desktop-copy", move || Msg::Transferred { report: files::transfer(&paths, &dest, moving), cut });
    }

    // -- renaming ------------------------------------------------------------

    fn desk_start_rename(&mut self, key: &str) {
        let Some((index, view)) = self.desk_view_now() else {
            return;
        };
        let Some(item_index) = view.find(key) else {
            return;
        };
        let Some(entry) = view.items[item_index].entry.and_then(|e| self.st.desk.entries.get(e)).cloned() else {
            return;
        };
        let rect = view.rects[item_index];
        let origin = self.surfaces[index].origin;
        let output = self.surfaces[index].output.clone();
        let height = (ramp("body", Rgba::WHITE).line_height() + 12.0).max(30.0);
        let width = (rect.w + 60.0).max(180.0);
        let x = (origin.0 + rect.center().0 - width / 2.0).max(origin.0);
        let y = origin.1 + rect.y + 6.0 + view.grid.icon + 2.0;
        let mut field = TextField::default();
        field.set(&entry.name);
        // The caret before the extension, as Files does it.
        let extension = if entry.is_dir { "" } else { files::split_extension(&entry.name).1 }.to_owned();
        for _ in 0..extension.chars().count() {
            field.key(Keysym::Left, false);
        }
        self.close_transients();
        self.st.desk.renaming = Some(Renaming { key: key.to_owned(), old: entry.os_name.clone(), field, fresh: true, extension });
        self.st.desk.selected = vec![key.to_owned()];
        self.ensure_dismiss(&output);
        self.create_layer(
            Role::Rename,
            &output,
            Spec {
                layer: Layer::Overlay,
                namespace: "boswas-rename",
                anchor: Anchor::TOP | Anchor::LEFT,
                size: ((width + 2.0 * PAD) as u32, (height + 2.0 * PAD) as u32),
                margin: ((y - PAD) as i32, 0, 0, (x - PAD) as i32),
                exclusive: -1,
                keyboard: KeyboardInteractivity::Exclusive,
                input: true,
                origin: (x - PAD, y - PAD),
                animate: false,
            },
        );
        self.mark(Role::Wallpaper);
    }

    /// Ends renaming without a change.
    pub fn desk_cancel_rename(&mut self) {
        self.st.desk.renaming = None;
        self.remove(Role::Rename);
        if self.open_kind().is_none() && self.st.menus.is_empty() {
            self.remove(Role::Dismiss);
        }
        self.mark(Role::Wallpaper);
    }

    /// Renames with what was typed.
    pub fn desk_commit_rename(&mut self) {
        let Some(renaming) = self.st.desk.renaming.take() else {
            return;
        };
        self.desk_cancel_rename();
        let name = renaming.field.text().trim_end().to_owned();
        if name.as_bytes() == renaming.old.as_encoded_bytes() {
            return;
        }
        if let Err(problem) = files::validate_name(&name) {
            return self.notice("The item could not be renamed", problem, None);
        }
        let Some(dir) = self.st.desk.dir.clone() else {
            return;
        };
        let key = renaming.key.clone();
        self.desk_job("desktop-rename", move || {
            let result = files::rename(&dir.path, &renaming.old, &name);
            Msg::Renamed { key, name, result }
        });
    }

    /// A key while renaming.
    pub fn desk_rename_key(&mut self, event: &KeyEvent, typed: Option<&str>) {
        match event.keysym {
            Keysym::Escape => return self.desk_cancel_rename(),
            Keysym::Return | Keysym::KP_Enter | Keysym::Tab => return self.desk_commit_rename(),
            keysym => {
                let ctrl = self.modifiers.ctrl;
                if let Some(renaming) = self.st.desk.renaming.as_mut() {
                    renaming.key(keysym, ctrl, typed);
                }
            }
        }
        self.mark(Role::Rename);
    }

    // -- the clipboard -------------------------------------------------------

    fn desk_copy(&mut self, cut: bool) {
        let paths = self.desk_selected_paths();
        if paths.is_empty() {
            return;
        }
        let (Some(manager), Some(device)) = (&self.data_devices, &self.data_device) else {
            return;
        };
        let source = manager.create_copy_paste_source(&self.qh, files::CLIPBOARD_MIMES);
        source.set_selection(device, self.serial);
        self.st.desk.cut = if cut { self.st.desk.selected.iter().filter(|k| k.starts_with("file:")).cloned().collect() } else { Vec::new() };
        self.st.desk.clip = Some((source, paths, cut));
    }

    fn desk_drop_clip(&mut self) {
        if let Some((source, _, _)) = self.st.desk.clip.take() {
            if let Some(device) = &self.data_device {
                device.unset_selection(self.serial);
            }
            drop(source);
        }
        self.st.desk.cut.clear();
    }

    /// Another program asks for what the desktop copied. Returns the pipe
    /// when the request is not the desktop's.
    pub fn desk_clipboard_send(&mut self, source: &WlDataSource, mime: &str, pipe: WritePipe) -> Option<WritePipe> {
        let Some((own, paths, cut)) = &self.st.desk.clip else {
            return Some(pipe);
        };
        if own.inner() != source {
            return Some(pipe);
        }
        if let Some(data) = files::clipboard_data(mime, paths, *cut) {
            let mut pipe = pipe;
            let _ = std::thread::Builder::new().name("clipboard".into()).spawn(move || {
                use std::io::Write;
                let _ = pipe.write_all(&data);
            });
        }
        None
    }

    /// Another program took the clipboard.
    pub fn desk_clipboard_cancelled(&mut self, source: &WlDataSource) {
        if self.st.desk.clip.as_ref().is_some_and(|(own, _, _)| own.inner() == source) {
            self.st.desk.clip = None;
            self.st.desk.cut.clear();
            self.mark(Role::Wallpaper);
        }
    }

    /// Whether Paste can do something: what is on the clipboard holds
    /// files, or the clipboard is not known yet (the desktop learns it
    /// once it has the keyboard).
    fn desk_clipboard_has_files(&self) -> bool {
        match self.data_device.as_ref().and_then(|d| d.data().selection_offer()) {
            Some(offer) => offer.with_mime_types(|types| types.iter().any(|t| t == "text/uri-list" || t == "x-special/gnome-copied-files")),
            None => self.st.desk.clip.is_some() || self.data_device.is_some(),
        }
    }

    fn desk_paste(&mut self) {
        let Some(offer) = self.data_device.as_ref().and_then(|d| d.data().selection_offer()) else {
            return;
        };
        let types: Vec<String> = offer.with_mime_types(|t| t.to_vec());
        let gnome = types.iter().any(|t| t == "x-special/gnome-copied-files");
        let uri = types.iter().any(|t| t == "text/uri-list");
        if !gnome && !uri {
            return;
        }
        let main = offer.receive(if gnome { "x-special/gnome-copied-files".into() } else { "text/uri-list".into() });
        let kde = (!gnome && types.iter().any(|t| t == "application/x-kde-cutselection")).then(|| offer.receive("application/x-kde-cutselection".into()).ok()).flatten();
        let _ = self.conn.flush();
        let Ok(main) = main else {
            return;
        };
        self.st.desk.drop_place = self.desk_arrival_place();
        let Some(dir) = self.desk_dir_for_writing() else {
            return;
        };
        self.desk_job("desktop-paste", move || {
            let data = files::read_pipe(main, 5);
            let (cut, paths) = if gnome {
                files::decode_gnome(&data).unwrap_or((false, Vec::new()))
            } else {
                let cut = kde.map(|pipe| files::read_pipe(pipe, 2)).is_some_and(|d| d.starts_with(b"1"));
                (cut, files::decode_uri_list(&data))
            };
            Msg::Transferred { report: files::transfer(&paths, &dir, cut), cut }
        });
    }

    // -- drops from other programs ----------------------------------------

    /// A drag entered a surface of the shell.
    pub fn desk_drag_enter(&mut self, surface: &WlSurface, x: f64, y: f64) {
        let Some(index) = self.desk_surface().filter(|i| self.surfaces[*i].wl_surface() == surface) else {
            return;
        };
        let _ = index;
        let Some(offer) = self.data_device.as_ref().and_then(|d| d.data().drag_offer()) else {
            return;
        };
        let accepts = self.st.desk_shown() && self.st.desk.dir.as_ref().is_some_and(|d| !d.disabled) && offer.with_mime_types(|t| t.iter().any(|m| m == "text/uri-list"));
        if accepts {
            offer.accept_mime_type(offer.serial, Some("text/uri-list".into()));
            offer.set_actions(DndAction::Copy | DndAction::Move, DndAction::Copy);
        } else {
            offer.accept_mime_type(offer.serial, None);
        }
        self.st.desk.incoming = Some(Incoming { offer: accepts.then_some(offer), at: (x as f32, y as f32) });
        self.desk_drag_motion(x, y);
    }

    pub fn desk_drag_motion(&mut self, x: f64, y: f64) {
        let at = (x as f32, y as f32);
        let Some(incoming) = self.st.desk.incoming.as_mut() else {
            return;
        };
        incoming.at = at;
        let target = self.desk_view_now().and_then(|(_, view)| view.at(at).map(|i| view.items[i].clone())).filter(|item| self.desk_accepts_drop(item)).map(|item| item.key);
        if target != self.st.desk.drop_target {
            self.st.desk.drop_target = target;
            self.mark(Role::Wallpaper);
        }
    }

    pub fn desk_drag_leave(&mut self) {
        if self.st.desk.incoming.take().is_some() {
            self.st.desk.drop_target = None;
            self.mark(Role::Wallpaper);
        }
    }

    /// The files were dropped: read the list, then copy or move them.
    pub fn desk_drop(&mut self) {
        let Some(incoming) = self.st.desk.incoming.take() else {
            return;
        };
        let target_key = self.st.desk.drop_target.take();
        let Some(offer) = incoming.offer else {
            return;
        };
        let moving = offer.selected_action == DndAction::Move;
        let Ok(pipe) = offer.receive("text/uri-list".into()) else {
            offer.finish();
            offer.destroy();
            return;
        };
        let _ = self.conn.flush();
        let target = match target_key {
            Some(key) if key == "system:trash" => Target::Trash,
            Some(key) => self.st.desk.entries.iter().find(|e| e.key() == key).map_or(Target::Desktop, |e| Target::Folder(e.os_name.clone())),
            None => Target::Desktop,
        };
        if target == Target::Desktop && !self.st.desktop.icons.auto_arrange {
            if let Some((_, view)) = self.desk_view_now() {
                let mut place = view.grid.clamp(view.grid.place_at(incoming.at));
                if self.st.desktop.icons.align_to_grid {
                    place = (place.0.round(), place.1.round());
                }
                self.st.desk.drop_place = Some(place);
            }
        }
        // The list is read on a worker; the offer is finished once it is in.
        if let Some(old) = self.st.desk.finishing.replace(offer) {
            old.destroy();
        }
        self.desk_job("desktop-drop", move || {
            let paths = files::decode_uri_list(&files::read_pipe(pipe, 5));
            Msg::Dropped { paths, target, moving }
        });
    }
}

/// The selected files (not the system icons).
fn selected_entries(st: &State) -> Vec<&Entry> {
    st.desk_items().into_iter().filter(|i| st.desk.selected.contains(&i.key)).filter_map(|i| i.entry).filter_map(|e| st.desk.entries.get(e)).collect()
}

/// The menu of the desktop's background: View, Sort by, Refresh, Paste,
/// New, Open in Terminal and the settings.
pub fn background_menu(st: &State, paste_possible: bool) -> Vec<Item> {
    let icons = &st.desktop.icons;
    let radio = |label: &str, on: bool, cmd: DeskCmd| {
        let mut item = Item::new(label, "", Cmd::Desk(cmd));
        item.toggle = boswas_services::MenuToggle::Radio(on);
        item
    };
    let check = |label: &str, on: bool, cmd: DeskCmd| {
        let mut item = Item::new(label, "", Cmd::Desk(cmd));
        item.toggle = boswas_services::MenuToggle::Checkmark(on);
        item
    };
    let mut view = Item::new("View", "view-grid-symbolic", Cmd::Submenu);
    view.children = IconSize::ALL.iter().map(|s| radio(s.label(), icons.size == *s, DeskCmd::Size(*s))).collect();
    view.children.push(Item::separator());
    view.children.push(check("Auto arrange icons", icons.auto_arrange, DeskCmd::AutoArrange));
    let mut align = check("Align icons to grid", icons.align_to_grid, DeskCmd::AlignToGrid);
    align.enabled = !icons.auto_arrange;
    view.children.push(align);
    view.children.push(Item::separator());
    view.children.push(check("Show desktop icons", icons.icons, DeskCmd::ShowIcons));
    let mut sort = Item::new("Sort by", "view-sort-ascending-symbolic", Cmd::Submenu);
    sort.children = SortKey::ALL.iter().map(|k| radio(k.label(), st.desk.saved.sort == *k, DeskCmd::Sort(*k))).collect();
    sort.enabled = icons.icons;
    let usable = st.desk.dir.as_ref().is_some_and(|d| !d.disabled) && icons.icons;
    let mut paste = Item::new("Paste", "edit-paste-symbolic", Cmd::Desk(DeskCmd::Paste));
    paste.enabled = usable && paste_possible;
    let mut new = Item::new("New", "list-add-symbolic", Cmd::Submenu);
    new.children = vec![Item::new("Folder", "folder-new-symbolic", Cmd::Desk(DeskCmd::NewFolder)), Item::new("Text document", "document-new-symbolic", Cmd::Desk(DeskCmd::NewTextDocument))];
    new.enabled = usable;
    let mut terminal = Item::new("Open in Terminal", "utilities-terminal-symbolic", Cmd::Desk(DeskCmd::OpenTerminal));
    terminal.enabled = usable;
    let mut refresh = Item::new("Refresh", "view-refresh-symbolic", Cmd::Desk(DeskCmd::Refresh));
    refresh.enabled = icons.icons;
    vec![
        view,
        sort,
        refresh,
        Item::separator(),
        paste,
        Item::separator(),
        new,
        Item::separator(),
        terminal,
        Item::separator(),
        Item::new("Display settings", "preferences-desktop-display", Cmd::Spawn(crate::start::settings_command("display"))),
        Item::new("Personalise", "preferences-desktop-theme-global", Cmd::Spawn(crate::start::settings_command("personalization"))),
        Item::new("All settings", "preferences-system-symbolic", Cmd::Spawn(vec!["boswas-control-center".into()])),
    ]
}

/// The menu of the selected icons. `apps`: (id, name, icon) of the
/// applications for Open with.
pub fn item_menu(st: &State, apps: &[(String, String, String)]) -> Vec<Item> {
    let entries = selected_entries(st);
    let systems = st.desk_items().iter().filter(|i| st.desk.selected.contains(&i.key)).filter(|i| i.system.is_some()).count();
    let mut items = vec![Item::new("Open", "document-open-symbolic", Cmd::Desk(DeskCmd::Open))];
    if entries.is_empty() {
        // System icons: open them; Properties shows them in Files.
        if systems > 0 {
            items.push(Item::separator());
            items.push(Item::new("Properties", "document-properties-symbolic", Cmd::Desk(DeskCmd::Properties)));
        }
        return items;
    }
    if !apps.is_empty() {
        let mut with = Item::new("Open with", "", Cmd::Submenu);
        with.children = apps.iter().map(|(id, name, icon)| Item::new(name, icon, Cmd::Desk(DeskCmd::OpenWith(id.clone())))).collect();
        items.push(with);
    }
    items.push(Item::separator());
    items.push(Item::new("Cut", "edit-cut-symbolic", Cmd::Desk(DeskCmd::Cut)));
    items.push(Item::new("Copy", "edit-copy-symbolic", Cmd::Desk(DeskCmd::Copy)));
    items.push(Item::separator());
    let mut rename = Item::new("Rename", "edit-rename-symbolic", Cmd::Desk(DeskCmd::Rename));
    rename.enabled = entries.len() == 1 && systems == 0;
    items.push(rename);
    items.push(Item::new("Delete", "user-trash-symbolic", Cmd::Desk(DeskCmd::Delete)));
    items.push(Item::separator());
    items.push(Item::new("Properties", "document-properties-symbolic", Cmd::Desk(DeskCmd::Properties)));
    items
}

/// The icons from `a` to `b` in the icons' order.
fn range(view: &View, a: &str, b: &str) -> Vec<String> {
    let position = |key: &str| view.order.iter().position(|i| view.items[*i].key == key);
    let (Some(x), Some(y)) = (position(a), position(b)) else {
        return vec![b.to_owned()];
    };
    let (from, to) = (x.min(y), x.max(y));
    view.order[from..=to].iter().map(|i| view.items[*i].key.clone()).collect()
}

/// Watches the desktop folder with inotify and says when it changed (at
/// most every 150 ms); waits for the folder when it is not there yet.
fn watch(dir: &Path, sender: &Sender<Msg>) {
    use rustix::fs::inotify::{self, CreateFlags, ReadFlags, WatchFlags};
    let flags = WatchFlags::CREATE | WatchFlags::DELETE | WatchFlags::MOVED_FROM | WatchFlags::MOVED_TO | WatchFlags::CLOSE_WRITE | WatchFlags::ATTRIB | WatchFlags::DELETE_SELF | WatchFlags::MOVE_SELF | WatchFlags::ONLYDIR;
    loop {
        let Ok(fd) = inotify::init(CreateFlags::CLOEXEC) else {
            return;
        };
        if inotify::add_watch(&fd, dir, flags).is_err() {
            // Not there yet: look again in a while.
            std::thread::sleep(Duration::from_secs(5));
            continue;
        }
        if sender.send(Msg::Changed).is_err() {
            return;
        }
        let mut buffer = [std::mem::MaybeUninit::<u8>::uninit(); 4096];
        let mut reader = inotify::Reader::new(&fd, &mut buffer);
        let mut gone = false;
        while !gone {
            match reader.next() {
                Ok(event) => {
                    if event.events().intersects(ReadFlags::DELETE_SELF | ReadFlags::MOVE_SELF | ReadFlags::IGNORED) {
                        gone = true;
                    }
                }
                Err(rustix::io::Errno::INTR) => continue,
                Err(_) => {
                    gone = true;
                }
            }
            // Let a burst of changes settle, then say it once.
            std::thread::sleep(Duration::from_millis(150));
            let _ = rustix::io::ioctl_fionbio(&fd, true);
            while reader.next().is_ok() {}
            let _ = rustix::io::ioctl_fionbio(&fd, false);
            if sender.send(Msg::Changed).is_err() {
                return;
            }
        }
        std::thread::sleep(Duration::from_secs(1));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::testing;
    use boswas_ui::Scheme;

    fn entry(name: &str, dir: bool, size: u64, modified: i64) -> Entry {
        Entry { os_name: name.into(), name: name.into(), is_dir: dir, is_link: false, size, modified, kind: files::file_type(name, dir, false), launcher: None }
    }

    fn state_with(entries: Vec<Entry>) -> State {
        let mut st = State::for_tests();
        st.desk.dir = Some(DesktopDir { path: "/home/asha/Desktop".into(), configured: true, disabled: false });
        st.desk.entries = entries;
        st.desk.scanned = true;
        st
    }

    fn sample() -> Vec<Entry> {
        vec![entry("zebra.txt", false, 10, 5), entry("Photos", true, 0, 1), entry("apple.pdf", false, 900, 9), entry("Music 10", true, 0, 3), entry("Music 9", true, 0, 2), entry("b.png", false, 50, 7)]
    }

    #[test]
    fn the_grid_follows_the_icon_size_and_the_text_size() {
        let medium = grid((1920.0, 1080.0), 48.0, IconSize::Medium);
        let large = grid((1920.0, 1080.0), 48.0, IconSize::Large);
        let small = grid((1366.0, 768.0), 48.0, IconSize::Small);
        assert!(large.cell.0 > medium.cell.0 && large.cell.1 > medium.cell.1 && medium.cell.0 > small.cell.0);
        assert_eq!((medium.icon, large.icon, small.icon), (48.0, 96.0, 32.0));
        assert!(medium.rows as f32 * medium.cell.1 <= 1080.0 - 48.0, "above the taskbar");
        crate::ui::set_text_scale(225);
        let big = grid((1920.0, 1080.0), 48.0, IconSize::Medium);
        crate::ui::set_text_scale(100);
        assert!(big.cell.0 > medium.cell.0 && big.cell.1 > medium.cell.1, "labels grow with the text");
        assert!(big.rows >= 1 && big.columns >= 1);
        let tiny = grid((100.0, 60.0), 48.0, IconSize::Large);
        assert_eq!((tiny.rows, tiny.columns), (1, 1), "never no cell at all");
    }

    #[test]
    fn icons_fill_columns_from_the_top_left_in_the_chosen_order() {
        let st = state_with(sample());
        let items = st.desk_items();
        assert_eq!(items[0].key, "system:trash", "the system icons come first");
        let labels = |sort| order(&items, &st.desk.entries, sort).into_iter().map(|i| items[i].label.clone()).collect::<Vec<_>>();
        assert_eq!(labels(SortKey::Name), ["Trash", "Music 9", "Music 10", "Photos", "apple.pdf", "b.png", "zebra.txt"], "folders first, numbers by value");
        assert_eq!(labels(SortKey::Size), ["Trash", "Music 9", "Music 10", "Photos", "zebra.txt", "b.png", "apple.pdf"]);
        assert_eq!(labels(SortKey::Modified), ["Trash", "Music 10", "Music 9", "Photos", "apple.pdf", "b.png", "zebra.txt"], "newest first");
        assert_eq!(labels(SortKey::Type)[4..], ["apple.pdf", "b.png", "zebra.txt"]);
        let g = Grid { cell: (80.0, 100.0), columns: 10, rows: 3, icon: 48.0 };
        let mut settings = DesktopIcons { auto_arrange: true, ..DesktopIcons::default() };
        let ord = order(&items, &st.desk.entries, SortKey::Name);
        let places = arrange(&items, &ord, &HashMap::new(), &settings, &g);
        let sorted: Vec<(f32, f32)> = ord.iter().map(|i| places[*i]).collect();
        assert_eq!(sorted, [(0.0, 0.0), (0.0, 1.0), (0.0, 2.0), (1.0, 0.0), (1.0, 1.0), (1.0, 2.0), (2.0, 0.0)], "column by column");
        // Kept places win when auto arrange is off; two never share a cell; the others fill free cells.
        settings.auto_arrange = false;
        let saved: HashMap<String, (f32, f32)> = [("file:zebra.txt".to_owned(), (4.4, 1.2)), ("file:apple.pdf".to_owned(), (4.0, 1.0)), ("system:trash".to_owned(), (99.0, -5.0))].into();
        let places = arrange(&items, &ord, &saved, &settings, &g);
        let at = |label: &str| places[items.iter().position(|i| i.label == label).unwrap()];
        assert_eq!(at("Trash"), (9.0, 0.0), "clamped into the grid");
        assert_eq!(at("apple.pdf"), (4.0, 1.0));
        assert_ne!(at("zebra.txt"), (4.0, 1.0), "the cell was taken");
        assert_eq!(at("Music 9"), (0.0, 0.0), "the first free cell");
        let mut all: Vec<(i64, i64)> = places.iter().map(|p| (p.0.round() as i64, p.1.round() as i64)).collect();
        all.sort_unstable();
        all.dedup();
        assert_eq!(all.len(), items.len(), "one icon per cell");
        // Without the grid, a place may be between cells.
        settings.align_to_grid = false;
        let free = arrange(&items, &ord, &[("file:b.png".to_owned(), (2.5, 0.5))].into(), &settings, &g);
        assert_eq!(free[items.iter().position(|i| i.label == "b.png").unwrap()], (2.5, 0.5));
    }

    #[test]
    fn selection_by_band_and_by_range() {
        let st = state_with(sample());
        let view = st.desk_view((1366.0, 768.0));
        let rects: Vec<(String, Rect)> = view.items.iter().zip(&view.rects).map(|(i, r)| (i.key.clone(), *r)).collect();
        let first = view.rects[view.order[0]];
        let second = view.rects[view.order[1]];
        let band = in_band(&rects, (first.x + 10.0, first.y + 10.0), (second.x + 20.0, second.bottom() - 10.0));
        assert_eq!(band.len(), 2, "{band:?}");
        assert!(in_band(&rects, (1300.0, 700.0), (1350.0, 740.0)).is_empty(), "the empty corner");
        let a = view.items[view.order[1]].key.clone();
        let b = view.items[view.order[4]].key.clone();
        assert_eq!(range(&view, &b, &a).len(), 4, "Shift selects everything between, either way");
        assert_eq!(view.at(view.rects[0].center()), Some(0));
        assert_eq!(view.at((1360.0, 760.0)), None);
    }

    #[test]
    fn the_desktop_draws_in_both_schemes_with_every_size_and_without_fonts() {
        let mut st = state_with(sample());
        st.desk.selected = vec!["file:Photos".into()];
        st.desk.cut = vec!["file:zebra.txt".into()];
        st.desk.keyboard = true;
        st.desk.cursor = Some("file:apple.pdf".into());
        st.desktop.icons.system_icons = SystemIcon::ALL.to_vec();
        st.desk.thumbnails.insert("file:b.png".into(), (7, 50, Rc::new(Pixmap::new(40, 30).unwrap())));
        st.desk.band = Some(((300.0, 300.0), (500.0, 420.0)));
        for size in IconSize::ALL {
            st.desktop.icons.size = size;
            for text in [100, 175, 225] {
                crate::ui::set_text_scale(text);
                for scheme in [Scheme::Dark, Scheme::Light] {
                    for output in [(1920.0, 1080.0), (1366.0, 768.0)] {
                        let (canvas, hits) = testing::render(output.0, output.1, 1.0, scheme, Some((40.0, 40.0)), None, |f| draw(f, &st, output));
                        assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0));
                        assert!(testing::hits_are_inside(&hits, output.0, output.1));
                        assert_eq!(hits.len(), st.desk_items().len(), "every icon reacts");
                        assert!(hits.iter().all(|h| h.tooltip.is_some()));
                    }
                }
            }
        }
        crate::ui::set_text_scale(100);
        let fontless = crate::ui::Resources::with_text(boswas_ui::Theme::of(Scheme::Light), boswas_ui::text::TextEngine::from_font_dirs(&[]));
        let (canvas, _) = testing::render_with(fontless, 800.0, 600.0, 1.0, None, None, |f| draw(f, &st, (800.0, 600.0)));
        assert!(canvas.data().chunks_exact(4).any(|p| p[3] > 0));
        // Turned off, nothing is drawn and nothing reacts.
        st.desktop.icons.icons = false;
        let (_, hits) = testing::render(800.0, 600.0, 1.0, Scheme::Dark, None, None, |f| draw(f, &st, (800.0, 600.0)));
        assert!(hits.is_empty());
        // A folder turned off by xdg-user-dirs shows the system icons only.
        st.desktop.icons.icons = true;
        st.desk.dir = Some(DesktopDir { path: "/home/asha".into(), configured: true, disabled: true });
        assert_eq!(st.desk_items().len(), 3);
    }

    #[test]
    fn renaming_replaces_the_selected_name_and_keeps_the_extension() {
        let mut field = TextField::default();
        field.set("notes.txt");
        let mut renaming = Renaming { key: "file:notes.txt".into(), old: "notes.txt".into(), field, fresh: true, extension: ".txt".into() };
        assert!(renaming.key(Keysym::t, false, Some("t")));
        assert!(renaming.key(Keysym::o, false, Some("o")));
        assert_eq!(renaming.field.text(), "to.txt", "the first letter replaced the name, the extension stayed");
        assert!(renaming.key(Keysym::End, false, None));
        assert!(renaming.key(Keysym::BackSpace, false, None));
        assert_eq!(renaming.field.text(), "to.tx");
        let mut folder = TextField::default();
        folder.set("New folder");
        let mut renaming = Renaming { key: "file:New folder".into(), old: "New folder".into(), field: folder, fresh: true, extension: String::new() };
        assert!(renaming.key(Keysym::BackSpace, false, None));
        assert_eq!(renaming.field.text(), "", "Backspace removes the selected name");
        let mut moved = TextField::default();
        moved.set("a.txt");
        let mut renaming = Renaming { key: "k".into(), old: "a.txt".into(), field: moved, fresh: true, extension: ".txt".into() };
        renaming.key(Keysym::Home, false, None);
        renaming.key(Keysym::x, false, Some("x"));
        assert_eq!(renaming.field.text(), "xa.txt", "after moving the caret, typing inserts");
    }

    #[test]
    fn the_menus_offer_what_windows_offers() {
        let mut st = state_with(sample());
        let labels = |items: &[Item]| items.iter().filter(|i| !i.separator).map(|i| i.label.clone()).collect::<Vec<_>>();
        let menu = background_menu(&st, true);
        assert_eq!(labels(&menu), ["View", "Sort by", "Refresh", "Paste", "New", "Open in Terminal", "Display settings", "Personalise", "All settings"]);
        assert_eq!(labels(&menu[0].children), ["Large icons", "Medium icons", "Small icons", "Auto arrange icons", "Align icons to grid", "Show desktop icons"]);
        assert_eq!(menu[0].children[1].toggle, boswas_services::MenuToggle::Radio(true), "medium is the default size");
        assert_eq!(labels(&menu[1].children), ["Name", "Size", "Item type", "Date modified"]);
        assert_eq!(labels(&menu[6].children), ["Folder", "Text document"]);
        assert!(!background_menu(&st, false)[4].enabled, "nothing to paste");
        st.desktop.icons.icons = false;
        let hidden = background_menu(&st, true);
        assert!(hidden[0].enabled && !hidden[4].enabled && !hidden[6].enabled, "View stays to bring the icons back");
        st.desktop.icons.icons = true;
        // An icon's menu.
        st.desk.selected = vec!["file:apple.pdf".into()];
        let apps = vec![("com.boswas.DocumentViewer.desktop".to_owned(), "Document Viewer".to_owned(), "boswas-document-viewer".to_owned())];
        let menu = item_menu(&st, &apps);
        assert_eq!(labels(&menu), ["Open", "Open with", "Cut", "Copy", "Rename", "Delete", "Properties"]);
        assert_eq!(menu[1].children[0].cmd, Cmd::Desk(DeskCmd::OpenWith("com.boswas.DocumentViewer.desktop".into())));
        st.desk.selected = vec!["file:apple.pdf".into(), "file:b.png".into()];
        assert!(!item_menu(&st, &[]).iter().find(|i| i.label == "Rename").unwrap().enabled, "one at a time");
        st.desk.selected = vec!["system:trash".into()];
        assert_eq!(labels(&item_menu(&st, &[])), ["Open", "Properties"]);
    }
}
