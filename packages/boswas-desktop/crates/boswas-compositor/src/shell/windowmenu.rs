//! The window menu: Restore, Move, Size, Minimize, Maximize and Close, plus
//! sending the window to another monitor or desktop. A right-click on a
//! title bar opens it, Alt+Space opens it for the focused window, and an
//! application that draws its own title bar asks for it with
//! `xdg_toplevel.show_window_menu`.
//!
//! Move and Size work as on Windows: after choosing one, the arrow keys move
//! or resize the window (Ctrl for single pixels) until Enter or Escape; a
//! moved window also follows the pointer until the next click.

use boswas_ui::{text::TextStyle, tokens, Canvas};
use smithay::{
    backend::{
        allocator::Fourcc,
        renderer::{
            element::{
                memory::{MemoryRenderBuffer, MemoryRenderBufferRenderElement},
                Kind,
            },
            Texture,
        },
    },
    input::pointer::GrabStartData,
    output::Output,
    utils::{Logical, Point, Rectangle, Scale, Size, Transform, SERIAL_COUNTER},
};

use super::element::{Tile, WindowElement};
use crate::{effects::BoswasRenderer, input::Direction, render::OutputRenderElement, state::Boswas};

const WIDTH: i32 = 256;
const ITEM_HEIGHT: i32 = 32;
const SEPARATOR_HEIGHT: i32 = 9;
const PADDING: i32 = 4;
/// How far an arrow key moves or resizes a window.
const KEY_STEP: i32 = 24;
/// The smallest frame the keyboard may shrink a window to.
const MIN_FRAME: (i32, i32) = (160, 96);

/// What a menu item does.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Entry {
    Restore,
    Move,
    Size,
    Minimize,
    Maximize,
    NextMonitor,
    /// Send the window to this desktop (an index).
    Desktop(usize),
    Close,
}

/// One row of the menu; a row without an entry is a separator.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Item {
    pub entry: Option<Entry>,
    pub label: String,
    /// The keyboard shortcut shown at the right edge.
    pub hint: &'static str,
    pub enabled: bool,
    /// Where the row is, relative to the menu's corner.
    pub rect: Rectangle<i32, Logical>,
}

/// What the menu needs to know about its window.
#[derive(Clone, Copy, Debug)]
pub struct Facts {
    pub tile: Tile,
    /// The application lets the window change its size.
    pub resizable: bool,
    pub monitors: usize,
    pub desktop: usize,
    pub desktops: usize,
}

/// The rows for a window, top to bottom, and the menu's size.
pub fn items(facts: &Facts) -> (Vec<Item>, Size<i32, Logical>) {
    let floating = facts.tile == Tile::Floating;
    let fullscreen = facts.tile == Tile::Fullscreen;
    let maximized = facts.tile == Tile::Maximized;
    let mut rows: Vec<(Option<Entry>, String, &'static str, bool)> = vec![
        (Some(Entry::Restore), "Restore".into(), "", !floating),
        (Some(Entry::Move), "Move".into(), "", !maximized && !fullscreen),
        (Some(Entry::Size), "Size".into(), "", floating && facts.resizable),
        (Some(Entry::Minimize), "Minimize".into(), "", true),
        (Some(Entry::Maximize), "Maximize".into(), "", !maximized && !fullscreen && facts.resizable),
    ];
    if facts.monitors > 1 || facts.desktops > 1 {
        rows.push((None, String::new(), "", false));
    }
    if facts.monitors > 1 {
        rows.push((Some(Entry::NextMonitor), "Move to next monitor".into(), "Super+Shift+Right", !fullscreen));
    }
    for desktop in (0..facts.desktops).filter(|d| *d != facts.desktop) {
        rows.push((Some(Entry::Desktop(desktop)), format!("Move to Desktop {}", desktop + 1), "", true));
    }
    rows.push((None, String::new(), "", false));
    rows.push((Some(Entry::Close), "Close".into(), "Alt+F4", true));

    let mut y = PADDING;
    let mut out = Vec::with_capacity(rows.len());
    for (entry, label, hint, enabled) in rows {
        let height = if entry.is_some() { ITEM_HEIGHT } else { SEPARATOR_HEIGHT };
        out.push(Item {
            entry,
            label,
            hint,
            enabled,
            rect: Rectangle::new(Point::from((PADDING, y)), Size::from((WIDTH - 2 * PADDING, height))),
        });
        y += height;
    }
    (out, Size::from((WIDTH, y + PADDING)))
}

/// Where a menu of `size` opened at `at` goes: its corner at the point,
/// flipped or shifted so that it stays inside `area`.
pub fn place(at: Point<i32, Logical>, size: Size<i32, Logical>, area: Rectangle<i32, Logical>) -> Rectangle<i32, Logical> {
    let right = area.loc.x + area.size.w;
    let bottom = area.loc.y + area.size.h;
    let x = if at.x + size.w > right { at.x - size.w } else { at.x };
    let y = if at.y + size.h > bottom { at.y - size.h } else { at.y };
    Rectangle::new(
        Point::from((
            x.clamp(area.loc.x, (right - size.w).max(area.loc.x)),
            y.clamp(area.loc.y, (bottom - size.h).max(area.loc.y)),
        )),
        size,
    )
}

/// The next row an arrow key selects: skips separators and disabled rows
/// and wraps around. `None` when nothing can be selected.
pub fn step(items: &[Item], from: Option<usize>, by: i32) -> Option<usize> {
    let count = items.len() as i32;
    if count == 0 {
        return None;
    }
    let mut index = match from {
        Some(index) => index as i32,
        None if by > 0 => -1,
        None => count,
    };
    for _ in 0..count {
        index = (index + by).rem_euclid(count);
        let item = &items[index as usize];
        if item.entry.is_some() && item.enabled {
            return Some(index as usize);
        }
    }
    None
}

/// A key for the menu while it is open.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MenuKey {
    Step(i32),
    Accept,
    Cancel,
}

/// A key while a window is being moved or resized with the keyboard.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdjustKey {
    Nudge { direction: Direction, fine: bool },
    Finish,
}

/// A window being moved or resized with the arrow keys.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct KeyAdjust {
    pub window: u64,
    pub resize: bool,
}

/// The frame after one arrow key: moved, or with its right or bottom edge
/// pushed, never smaller than `min` and never larger than `max` (0: no
/// limit).
pub fn nudge(
    frame: Rectangle<i32, Logical>,
    resize: bool,
    direction: Direction,
    by: i32,
    min: Size<i32, Logical>,
    max: Size<i32, Logical>,
) -> Rectangle<i32, Logical> {
    let (dx, dy) = match direction {
        Direction::Left => (-by, 0),
        Direction::Right => (by, 0),
        Direction::Up => (0, -by),
        Direction::Down => (0, by),
    };
    if !resize {
        return Rectangle::new(frame.loc + Point::from((dx, dy)), frame.size);
    }
    let limit = |value: i32, low: i32, high: i32| {
        let value = value.max(low);
        if high > 0 {
            value.min(high.max(low))
        } else {
            value
        }
    };
    Rectangle::new(
        frame.loc,
        Size::from((limit(frame.size.w + dx, min.w, max.w), limit(frame.size.h + dy, min.h, max.h))),
    )
}

/// The open menu.
pub struct WindowMenu {
    pub window: u64,
    /// Where the menu is on the desktop.
    pub rect: Rectangle<i32, Logical>,
    items: Vec<Item>,
    hover: Option<usize>,
    picture: Option<((Option<usize>, i32, bool), MemoryRenderBuffer)>,
}

impl Boswas {
    /// Opens the menu of `window` with its corner at `at` (desktop
    /// coordinates).
    pub fn open_window_menu(&mut self, window: &WindowElement, at: Point<i32, Logical>) {
        let Some(frame) = self.window_rect(window) else {
            return;
        };
        let Some(output) = self.output_for_rect(Rectangle::new(at, Size::from((1, 1)))).or_else(|| self.output_for_rect(frame))
        else {
            return;
        };
        let Some(area) = self.space.output_geometry(&output) else {
            return;
        };
        let (min, max) = window.size_limits();
        let fixed = min.w > 0 && min.h > 0 && min == max;
        let (items, size) = items(&Facts {
            tile: window.data().tile.get(),
            resizable: !fixed,
            monitors: self.space.outputs().count(),
            desktop: window.data().workspace.get(),
            desktops: self.workspace_count,
        });
        self.snap_flyout = None;
        self.maximize_hover = None;
        self.key_adjust = None;
        self.window_menu = Some(WindowMenu {
            window: window.id(),
            rect: place(at, size, area),
            items,
            hover: None,
            picture: None,
        });
        self.schedule_redraw();
    }

    /// Opens the menu of the focused window under the left end of its
    /// title bar (Alt+Space).
    pub fn open_focused_window_menu(&mut self) {
        let Some(window) = self.focused_window() else {
            return;
        };
        let Some(frame) = self.window_rect(&window) else {
            return;
        };
        let below = window.titlebar_height().max(8);
        self.open_window_menu(&window, frame.loc + Point::from((8, below)));
        // Opened from the keyboard: the first row is selected.
        if let Some(menu) = &mut self.window_menu {
            menu.hover = step(&menu.items, None, 1);
        }
    }

    pub fn close_window_menu(&mut self) {
        if self.window_menu.take().is_some() {
            self.schedule_redraw();
        }
    }

    /// Whether `point` is on the open menu.
    pub fn on_window_menu(&self, point: Point<f64, Logical>) -> bool {
        self.window_menu.as_ref().is_some_and(|menu| menu.rect.to_f64().contains(point))
    }

    /// Tells the menu where the pointer is. Returns whether the pointer is
    /// on the menu (which then takes it).
    pub fn window_menu_motion(&mut self, point: Point<f64, Logical>) -> bool {
        let Some(menu) = &mut self.window_menu else {
            return false;
        };
        let inside = menu.rect.to_f64().contains(point);
        if inside {
            let local = point - menu.rect.loc.to_f64();
            let hover = menu
                .items
                .iter()
                .position(|item| item.entry.is_some() && item.enabled && item.rect.to_f64().contains(local));
            if hover != menu.hover {
                menu.hover = hover;
                self.redraw = true;
            }
        }
        inside
    }

    /// A click on the menu: does what the row under the pointer says.
    pub fn window_menu_click(&mut self, point: Point<f64, Logical>) {
        if !self.window_menu_motion(point) {
            return;
        }
        self.window_menu_accept();
    }

    pub fn window_menu_key(&mut self, key: MenuKey) {
        match key {
            MenuKey::Cancel => self.close_window_menu(),
            MenuKey::Accept => self.window_menu_accept(),
            MenuKey::Step(by) => {
                if let Some(menu) = &mut self.window_menu {
                    menu.hover = step(&menu.items, menu.hover, by);
                    self.schedule_redraw();
                }
            }
        }
    }

    /// Runs the selected row, if there is one, and closes the menu.
    fn window_menu_accept(&mut self) {
        let Some(menu) = &self.window_menu else {
            return;
        };
        let Some(entry) = menu.hover.and_then(|index| menu.items.get(index)).and_then(|item| item.entry) else {
            return;
        };
        let window = self.window_by_id(menu.window);
        self.close_window_menu();
        let Some(window) = window else {
            return;
        };
        match entry {
            Entry::Restore => self.set_tile(&window, Tile::Floating),
            Entry::Maximize => self.set_tile(&window, Tile::Maximized),
            Entry::Minimize => self.minimize_window(&window),
            Entry::Close => self.close_window(&window),
            Entry::NextMonitor => self.move_to_next_output_wrapping(&window),
            Entry::Desktop(index) => self.move_to_workspace(&window, index),
            Entry::Move => self.start_key_adjust(&window, false),
            Entry::Size => self.start_key_adjust(&window, true),
        }
    }

    /// Sends a window to the monitor on its right, or to the leftmost one
    /// from the last.
    fn move_to_next_output_wrapping(&mut self, window: &WindowElement) {
        let before = self.window_rect(window).and_then(|rect| self.output_for_rect(rect));
        self.move_to_next_output(window, Direction::Right);
        let after = self.window_rect(window).and_then(|rect| self.output_for_rect(rect));
        if before == after {
            // Already on the rightmost monitor: go all the way left.
            for _ in 0..self.space.outputs().count() {
                let here = self.window_rect(window).and_then(|rect| self.output_for_rect(rect));
                self.move_to_next_output(window, Direction::Left);
                if here == self.window_rect(window).and_then(|rect| self.output_for_rect(rect)) {
                    break;
                }
            }
        }
    }

    /// Starts moving or resizing a window with the arrow keys. A moved
    /// window also follows the pointer until the next click.
    fn start_key_adjust(&mut self, window: &WindowElement, resize: bool) {
        self.activate_window(window);
        if !resize && window.data().tile.get() != Tile::Floating {
            // A snapped window comes off its place first.
            self.set_tile(window, Tile::Floating);
        }
        self.key_adjust = Some(KeyAdjust {
            window: window.id(),
            resize,
        });
        if !resize {
            let start_data = GrabStartData {
                focus: None,
                button: 0,
                location: self.pointer.current_location(),
            };
            self.start_move(window, start_data, SERIAL_COUNTER.next_serial());
        }
    }

    /// A key while a window is moved or resized from the keyboard.
    pub fn key_adjust_key(&mut self, key: AdjustKey) {
        let Some(adjust) = self.key_adjust else {
            return;
        };
        let (direction, fine) = match key {
            AdjustKey::Finish => {
                self.finish_key_adjust();
                return;
            }
            AdjustKey::Nudge { direction, fine } => (direction, fine),
        };
        let Some(window) = self.window_by_id(adjust.window) else {
            self.key_adjust = None;
            return;
        };
        // The keyboard takes over from the pointer.
        self.release_pointer_grab();
        let Some(frame) = self.window_rect(&window) else {
            self.key_adjust = None;
            return;
        };
        let (min, max) = window.size_limits();
        let titlebar = window.titlebar_height();
        let lowest = Size::from((min.w.max(MIN_FRAME.0), (min.h + titlebar).max(MIN_FRAME.1)));
        let highest = Size::from((max.w, if max.h > 0 { max.h + titlebar } else { 0 }));
        let target = nudge(frame, adjust.resize, direction, if fine { 1 } else { KEY_STEP }, lowest, highest);
        if adjust.resize {
            if target.size != frame.size {
                window.resize_client(window.client_size_for_frame(target.size), false);
            }
        } else {
            // Keep a part of the title bar reachable on some monitor.
            let reachable = self.space.outputs().filter_map(|o| self.space.output_geometry(o)).any(|geo| {
                let grip = Rectangle::new(target.loc, Size::from((target.size.w, titlebar.max(32))));
                geo.overlaps(grip)
            });
            if reachable {
                self.move_window(&window, target.loc);
            }
        }
        self.schedule_redraw();
    }

    pub fn finish_key_adjust(&mut self) {
        if self.key_adjust.take().is_some() {
            self.release_pointer_grab();
            self.windows_changed();
        }
    }

    fn release_pointer_grab(&mut self) {
        let pointer = self.pointer.clone();
        if pointer.is_grabbed() {
            pointer.unset_grab(self, SERIAL_COUNTER.next_serial(), 0);
        }
    }

    /// The menu's picture on an output.
    pub fn window_menu_elements<R>(
        &mut self,
        renderer: &mut R,
        output: &Output,
        scale: Scale<f64>,
        elements: &mut Vec<OutputRenderElement<R>>,
    ) where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let Some(output_geo) = self.space.output_geometry(output) else {
            return;
        };
        let theme = self.theme;
        let Some(menu) = &self.window_menu else {
            return;
        };
        if !menu.rect.overlaps(output_geo) {
            return;
        }
        if self.window_by_id(menu.window).is_none() {
            self.window_menu = None;
            return;
        }
        let pixel_scale = scale.x.ceil().max(1.0) as i32;
        let key = (menu.hover, pixel_scale, theme.scheme == boswas_ui::Scheme::Light);
        if menu.picture.as_ref().map(|(cached, _)| cached) != Some(&key) {
            let t = tokens();
            let scheme = theme.scheme;
            let body = t.type_style("body");
            let (w, h) = (menu.rect.size.w as f32, menu.rect.size.h as f32);
            let Some(mut canvas) = Canvas::new(w, h, pixel_scale as f32) else {
                return;
            };
            canvas.fill_rounded_rect(0.0, 0.0, w, h, t.radius_flyout, t.material(scheme, "acrylic").tint);
            canvas.stroke_rounded_rect(0.5, 0.5, w - 1.0, h - 1.0, t.radius_flyout - 0.5, 1.0, t.stroke(scheme, "surface"));
            let rows: Vec<Item> = menu.items.clone();
            let hover = menu.hover;
            for (index, item) in rows.iter().enumerate() {
                let (x, y) = (item.rect.loc.x as f32, item.rect.loc.y as f32);
                let (iw, ih) = (item.rect.size.w as f32, item.rect.size.h as f32);
                if item.entry.is_none() {
                    let line = (y + ih / 2.0).floor();
                    canvas.fill_rect(0.0, line, w, 1.0, t.stroke(scheme, "divider"));
                    continue;
                }
                if hover == Some(index) {
                    canvas.fill_rounded_rect(x, y + 1.0, iw, ih - 2.0, t.radius_control, t.fill(scheme, "subtle-hover"));
                }
                let colour = if item.enabled { theme.text } else { theme.text_muted.alpha(0.55) };
                let style = TextStyle::new(body.size, colour).weight(body.weight);
                let top = y + ((ih - style.line_height()) / 2.0).floor();
                let hint_style = TextStyle::new(body.size, if item.enabled { theme.text_secondary } else { colour });
                let hint_width = if item.hint.is_empty() { 0.0 } else { self.text().measure(item.hint, &hint_style).0 };
                let room = iw - 24.0 - hint_width - if hint_width > 0.0 { 16.0 } else { 0.0 };
                self.text().draw(&mut canvas, &item.label, x + 12.0, top, Some(room.max(16.0)), &style);
                if hint_width > 0.0 {
                    self.text().draw(&mut canvas, item.hint, x + iw - 12.0 - hint_width, top, None, &hint_style);
                }
            }
            let (pw, ph) = canvas.pixel_size();
            let mut bytes = vec![0u8; (pw * ph * 4) as usize];
            canvas.write_argb8888(&mut bytes);
            let buffer =
                MemoryRenderBuffer::from_slice(&bytes, Fourcc::Argb8888, (pw as i32, ph as i32), pixel_scale, Transform::Normal, None);
            if let Some(menu) = &mut self.window_menu {
                menu.picture = Some((key, buffer));
            }
        }
        let Some(menu) = &self.window_menu else {
            return;
        };
        if let Some((_, buffer)) = &menu.picture {
            let location = (menu.rect.loc - output_geo.loc).to_physical_precise_round::<_, i32>(scale);
            if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                location.to_f64(),
                buffer,
                None,
                None,
                Some(menu.rect.size),
                Kind::Unspecified,
            ) {
                elements.push(OutputRenderElement::Memory(element));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect(x: i32, y: i32, w: i32, h: i32) -> Rectangle<i32, Logical> {
        Rectangle::new(Point::from((x, y)), Size::from((w, h)))
    }

    fn facts(tile: Tile) -> Facts {
        Facts {
            tile,
            resizable: true,
            monitors: 1,
            desktop: 0,
            desktops: 1,
        }
    }

    fn enabled(items: &[Item], entry: Entry) -> bool {
        items.iter().find(|item| item.entry == Some(entry)).expect("the row exists").enabled
    }

    #[test]
    fn the_rows_follow_the_window_state() {
        let (floating, size) = items(&facts(Tile::Floating));
        assert!(!enabled(&floating, Entry::Restore));
        assert!(enabled(&floating, Entry::Move));
        assert!(enabled(&floating, Entry::Size));
        assert!(enabled(&floating, Entry::Maximize));
        assert!(enabled(&floating, Entry::Minimize));
        assert!(enabled(&floating, Entry::Close));
        assert_eq!(size.w, WIDTH);
        // The rows fill the menu from top to bottom without gaps.
        let mut y = PADDING;
        for item in &floating {
            assert_eq!(item.rect.loc.y, y);
            y += item.rect.size.h;
        }
        assert_eq!(size.h, y + PADDING);

        let (maximized, _) = items(&facts(Tile::Maximized));
        assert!(enabled(&maximized, Entry::Restore));
        assert!(!enabled(&maximized, Entry::Move));
        assert!(!enabled(&maximized, Entry::Size));
        assert!(!enabled(&maximized, Entry::Maximize));

        let (snapped, _) = items(&facts(Tile::Left));
        assert!(enabled(&snapped, Entry::Restore));
        assert!(enabled(&snapped, Entry::Move));
        assert!(!enabled(&snapped, Entry::Size));
        assert!(enabled(&snapped, Entry::Maximize));

        let fixed = Facts {
            resizable: false,
            ..facts(Tile::Floating)
        };
        let (dialog, _) = items(&fixed);
        assert!(!enabled(&dialog, Entry::Size));
        assert!(!enabled(&dialog, Entry::Maximize));
        assert!(enabled(&dialog, Entry::Move));
    }

    #[test]
    fn monitors_and_desktops_appear_only_when_there_is_a_choice() {
        let (one, _) = items(&facts(Tile::Floating));
        assert!(one.iter().all(|item| item.entry != Some(Entry::NextMonitor)));
        assert!(one.iter().all(|item| !matches!(item.entry, Some(Entry::Desktop(_)))));
        // One separator, before Close.
        assert_eq!(one.iter().filter(|item| item.entry.is_none()).count(), 1);

        let many = Facts {
            monitors: 2,
            desktop: 1,
            desktops: 3,
            ..facts(Tile::Floating)
        };
        let (rows, _) = items(&many);
        assert!(enabled(&rows, Entry::NextMonitor));
        let desktops: Vec<usize> =
            rows.iter().filter_map(|item| if let Some(Entry::Desktop(d)) = item.entry { Some(d) } else { None }).collect();
        assert_eq!(desktops, [0, 2], "every desktop but the window's own");
        assert!(rows.iter().any(|item| item.label == "Move to Desktop 3"));
        assert_eq!(rows.iter().filter(|item| item.entry.is_none()).count(), 2);
        assert_eq!(rows.last().and_then(|item| item.entry), Some(Entry::Close));
    }

    #[test]
    fn the_menu_stays_on_the_screen() {
        let screen = rect(0, 0, 1280, 800);
        let size = Size::from((256, 200));
        assert_eq!(place(Point::from((100, 100)), size, screen), rect(100, 100, 256, 200));
        // Near the right or bottom edge it opens to the other side.
        assert_eq!(place(Point::from((1200, 100)), size, screen).loc.x, 1200 - 256);
        assert_eq!(place(Point::from((100, 700)), size, screen).loc.y, 500);
        // A second monitor to the right has its own edges.
        let second = rect(1280, 0, 1920, 1080);
        assert_eq!(place(Point::from((1290, 10)), size, second).loc, Point::from((1290, 10)));
        let tiny = rect(0, 0, 200, 100);
        assert_eq!(place(Point::from((150, 90)), size, tiny).loc, Point::from((0, 0)));
    }

    #[test]
    fn arrow_keys_skip_what_cannot_be_chosen() {
        let (rows, _) = items(&facts(Tile::Floating));
        // Restore is disabled for a floating window: the first choice is Move.
        let first = step(&rows, None, 1).unwrap();
        assert_eq!(rows[first].entry, Some(Entry::Move));
        let last = step(&rows, None, -1).unwrap();
        assert_eq!(rows[last].entry, Some(Entry::Close));
        // Down from Maximize skips the separator; down from Close wraps.
        let maximize = rows.iter().position(|item| item.entry == Some(Entry::Maximize)).unwrap();
        assert_eq!(step(&rows, Some(maximize), 1), Some(last));
        assert_eq!(step(&rows, Some(last), 1), Some(first));
        assert_eq!(step(&rows, Some(first), -1), Some(last));
        assert_eq!(step(&[], None, 1), None);
    }

    #[test]
    fn arrow_keys_move_and_resize_within_limits() {
        let frame = rect(100, 100, 640, 480);
        let none = Size::from((0, 0));
        let min = Size::from((160, 96));
        assert_eq!(nudge(frame, false, Direction::Left, 24, min, none), rect(76, 100, 640, 480));
        assert_eq!(nudge(frame, false, Direction::Down, 1, min, none), rect(100, 101, 640, 480));
        assert_eq!(nudge(frame, true, Direction::Right, 24, min, none), rect(100, 100, 664, 480));
        assert_eq!(nudge(frame, true, Direction::Up, 24, min, none), rect(100, 100, 640, 456));
        // Never below the minimum, never above the maximum.
        let small = rect(0, 0, 170, 100);
        assert_eq!(nudge(small, true, Direction::Left, 24, min, none).size, Size::from((160, 100)));
        assert_eq!(nudge(small, true, Direction::Up, 24, min, none).size, Size::from((170, 96)));
        let max = Size::from((650, 490));
        assert_eq!(nudge(frame, true, Direction::Right, 24, min, max).size, Size::from((650, 480)));
        assert_eq!(nudge(frame, true, Direction::Down, 24, min, max).size, Size::from((640, 490)));
    }
}
