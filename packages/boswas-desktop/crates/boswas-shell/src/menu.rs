//! Context menus: the taskbar's and Start's right-click menus, the user
//! and power menus, the desktop menu and the menus of tray items.

use boswas_services::{MenuEntry, MenuToggle, PowerAction};
use boswas_ui::{text::TextEngine, tokens::tokens, Rgba};

use crate::{
    action::Action,
    glyph::Glyph,
    ui::{ramp, Frame, Rect},
};

/// What a menu entry does.
#[derive(Clone, Debug, PartialEq)]
pub enum Cmd {
    /// Start an application by desktop file id.
    Launch(String),
    /// Start a fixed program (a desktop action, a settings page).
    Spawn(Vec<String>),
    Close(Vec<u64>),
    PinTaskbar(String, bool),
    PinStart(String, bool),
    Lock,
    SignOut,
    Power(PowerAction),
    /// An entry of a tray item's own menu.
    Tray { key: String, id: i32 },
    /// An entry of the widgets board's menus.
    Widgets(crate::widgets::WidgetCmd),
    /// An entry of the desktop's menus.
    Desk(crate::desktop_icons::DeskCmd),
    /// Another window of an application, by desktop file id (its "new
    /// window" action, or else its plain command).
    NewWindow(String),
    /// An entry of the quick-link menu and the other menus of the taskbar
    /// that act inside the shell (quicklinks.rs).
    Shell(crate::quicklinks::ShellCmd),
    /// Opens the entry's submenu.
    Submenu,
    Nothing,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Item {
    pub label: String,
    /// A themed icon name, or empty.
    pub icon: String,
    pub enabled: bool,
    pub separator: bool,
    pub toggle: MenuToggle,
    pub children: Vec<Item>,
    pub cmd: Cmd,
}

impl Item {
    pub fn new(label: &str, icon: &str, cmd: Cmd) -> Self {
        Self {
            label: label.to_owned(),
            icon: icon.to_owned(),
            enabled: true,
            separator: false,
            toggle: MenuToggle::None,
            children: Vec::new(),
            cmd,
        }
    }

    pub fn separator() -> Self {
        Self {
            separator: true,
            enabled: false,
            ..Self::new("", "", Cmd::Nothing)
        }
    }

    fn selectable(&self) -> bool {
        self.enabled && !self.separator
    }
}

/// Converts a tray item's menu. Depth and size are bounded: the data comes
/// from another program.
pub fn from_tray(key: &str, entries: &[MenuEntry], depth: usize) -> Vec<Item> {
    entries
        .iter()
        .take(64)
        .map(|entry| {
            let children = if depth < 3 { from_tray(key, &entry.children, depth + 1) } else { Vec::new() };
            Item {
                label: entry.label.chars().filter(|c| !c.is_control()).take(80).collect(),
                icon: entry.icon_name.clone(),
                enabled: entry.enabled,
                separator: entry.separator,
                toggle: entry.toggle,
                cmd: if children.is_empty() { Cmd::Tray { key: key.to_owned(), id: entry.id } } else { Cmd::Submenu },
                children,
            }
        })
        .collect()
}

const ROW: f32 = 32.0;
const SEPARATOR: f32 = 9.0;
const PAD: f32 = 4.0;
const MIN_WIDTH: f32 = 180.0;
const MAX_WIDTH: f32 = 340.0;
/// Room around a menu's panel for its shadow.
pub const MARGIN: f32 = 12.0;

/// An open menu.
#[derive(Clone, Debug, PartialEq)]
pub struct Menu {
    pub items: Vec<Item>,
    /// The entry the keyboard or the pointer is on.
    pub selected: Option<usize>,
    /// Where the panel's corner goes on the output, in logical pixels.
    pub x: f32,
    pub y: f32,
    /// The panel grows upwards from `y` (menus opened from the taskbar).
    pub upward: bool,
    /// The panel's size, set by [`Menu::place`].
    pub width: f32,
    pub height: f32,
}

impl Menu {
    pub fn new(items: Vec<Item>, x: f32, y: f32, upward: bool) -> Self {
        Self {
            items,
            selected: None,
            x,
            y,
            upward,
            width: MIN_WIDTH,
            height: 2.0 * PAD,
        }
    }

    fn has_marks(&self) -> bool {
        self.items.iter().any(|i| !i.icon.is_empty() || i.toggle != MenuToggle::None)
    }

    /// Measures the menu and keeps its panel inside an output of the given
    /// size. Returns the panel's rectangle on the output.
    pub fn place(&mut self, text: &mut TextEngine, output: (f32, f32)) -> Rect {
        let style = ramp("body", Rgba::WHITE);
        let marks = if self.has_marks() { 28.0 } else { 0.0 };
        let widest = self
            .items
            .iter()
            .map(|i| text.measure(&i.label, &style).0 + if i.children.is_empty() { 0.0 } else { 24.0 })
            .fold(0.0, f32::max);
        self.width = (widest + marks + 28.0).clamp(MIN_WIDTH, MAX_WIDTH).min(output.0 - 2.0 * MARGIN).max(40.0);
        self.height = 2.0 * PAD + self.items.iter().map(|i| if i.separator { SEPARATOR } else { ROW }).sum::<f32>();
        let x = self.x.min(output.0 - self.width - MARGIN).max(MARGIN);
        let top = if self.upward { self.y - self.height } else { self.y };
        let y = top.min(output.1 - self.height - MARGIN).max(MARGIN);
        Rect::new(x, y, self.width, self.height)
    }

    /// The rectangle of entry `index` inside the panel at `origin`.
    pub fn row(&self, origin: (f32, f32), index: usize) -> Rect {
        let y = origin.1 + PAD + self.items[..index].iter().map(|i| if i.separator { SEPARATOR } else { ROW }).sum::<f32>();
        let h = if self.items[index].separator { SEPARATOR } else { ROW };
        Rect::new(origin.0 + PAD, y, self.width - 2.0 * PAD, h)
    }

    /// Moves the selection to the next (or previous) entry that can be
    /// chosen, wrapping around.
    pub fn step(&mut self, forward: bool) {
        let count = self.items.len();
        if !self.items.iter().any(Item::selectable) {
            return;
        }
        let mut index = self.selected.unwrap_or(if forward { count - 1 } else { 0 });
        for _ in 0..count {
            index = if forward { (index + 1) % count } else { (index + count - 1) % count };
            if self.items[index].selectable() {
                self.selected = Some(index);
                return;
            }
        }
    }

    /// Draws the menu's panel at `origin` on the frame's canvas.
    pub fn draw(&self, f: &mut Frame, origin: (f32, f32), level: usize) {
        let theme = f.theme();
        let panel = Rect::new(origin.0, origin.1, self.width, self.height);
        f.panel(panel, "flyout");
        f.area(panel, Action::None);
        let marks = if self.has_marks() { 28.0 } else { 0.0 };
        let radius = tokens().radius_control;
        for (index, item) in self.items.iter().enumerate() {
            let row = self.row(origin, index);
            if item.separator {
                f.divider(panel.x, row.y + 4.0, panel.w);
                continue;
            }
            let color = if item.enabled { theme.text } else { theme.text_muted };
            if item.enabled {
                let mut touch = f.hit(row, Action::Menu { level, index });
                touch.focused = false;
                f.state_layer(row, radius, touch, self.selected == Some(index));
            }
            let mark = Rect::new(row.x + 8.0, row.y + (ROW - 16.0) / 2.0, 16.0, 16.0);
            match item.toggle {
                MenuToggle::Checkmark(true) => f.glyph(Glyph::Check, mark, color),
                MenuToggle::Radio(true) => f.glyph(Glyph::Dot, mark, color),
                MenuToggle::None if !item.icon.is_empty() => {
                    f.icon(&item.icon, mark.x, mark.y, 16.0);
                }
                _ => {}
            }
            let chevron = if item.children.is_empty() { 0.0 } else { 20.0 };
            let label = Rect::new(row.x + 10.0 + marks, row.y, row.w - 20.0 - marks - chevron, ROW);
            f.text_left(&item.label, label, &ramp("body", color));
            if !item.children.is_empty() {
                f.glyph(Glyph::ChevronRight, Rect::new(row.right() - 20.0, row.y + 10.0, 12.0, 12.0), theme.text_secondary);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::testing;
    use boswas_ui::Scheme;

    fn sample() -> Menu {
        let mut pin = Item::new("Pin to taskbar", "", Cmd::PinTaskbar("a".into(), true));
        pin.toggle = MenuToggle::Checkmark(true);
        let mut more = Item::new("More", "", Cmd::Submenu);
        more.children = vec![Item::new("Inner", "", Cmd::Nothing)];
        let mut disabled = Item::new("Unavailable", "", Cmd::Nothing);
        disabled.enabled = false;
        Menu::new(vec![Item::new("Open", "document-open-symbolic", Cmd::Launch("a".into())), pin, Item::separator(), disabled, more], 300.0, 400.0, true)
    }

    #[test]
    fn a_menu_is_measured_and_kept_on_the_output() {
        let mut text = TextEngine::new();
        let mut menu = sample();
        let rect = menu.place(&mut text, (800.0, 600.0));
        assert_eq!(rect.h, 2.0 * PAD + 4.0 * ROW + SEPARATOR);
        assert_eq!(rect.bottom(), 400.0, "an upward menu ends at its anchor");
        assert!(rect.w >= MIN_WIDTH && rect.w <= MAX_WIDTH);
        menu.x = 790.0;
        menu.y = 20.0;
        let rect = menu.place(&mut text, (800.0, 600.0));
        assert!(rect.right() <= 800.0 - MARGIN + 0.5 && rect.y >= MARGIN, "{rect:?}");
        let mut down = Menu::new(sample().items, 10.0, 590.0, false);
        let rect = down.place(&mut text, (800.0, 600.0));
        assert!(rect.bottom() <= 600.0 - MARGIN + 0.5);
    }

    #[test]
    fn stepping_skips_separators_and_disabled_entries() {
        let mut menu = sample();
        menu.step(true);
        assert_eq!(menu.selected, Some(0));
        menu.step(true);
        assert_eq!(menu.selected, Some(1));
        menu.step(true);
        assert_eq!(menu.selected, Some(4), "the separator and the disabled entry are skipped");
        menu.step(true);
        assert_eq!(menu.selected, Some(0), "wraps");
        menu.step(false);
        assert_eq!(menu.selected, Some(4));
        let mut empty = Menu::new(vec![Item::separator()], 0.0, 0.0, false);
        empty.step(true);
        assert_eq!(empty.selected, None);
    }

    #[test]
    fn drawing_registers_only_enabled_entries() {
        let mut menu = sample();
        let (_, hits) = testing::render(400.0, 300.0, 1.0, Scheme::Dark, None, None, |f| {
            menu.place(&mut f.res.text, (400.0, 300.0));
            menu.draw(f, (12.0, 12.0), 0);
        });
        let entries: Vec<usize> = hits.iter().filter_map(|h| if let Action::Menu { index, .. } = h.action { Some(index) } else { None }).collect();
        assert_eq!(entries, [0, 1, 4]);
        assert!(testing::hits_are_inside(&hits, 400.0, 300.0));
    }

    #[test]
    fn tray_menus_are_converted_with_bounds() {
        let leaf = |id: i32, label: &str| MenuEntry { id, label: label.into(), enabled: true, separator: false, toggle: MenuToggle::None, icon_name: String::new(), children: Vec::new() };
        let mut nested = leaf(1, "A");
        for _ in 0..6 {
            let mut outer = leaf(2, "Outer");
            outer.children = vec![nested];
            nested = outer;
        }
        let items = from_tray("key", &[nested, leaf(9, "Quit\u{7}")], 0);
        assert_eq!(items[1].label, "Quit");
        assert_eq!(items[1].cmd, Cmd::Tray { key: "key".into(), id: 9 });
        assert_eq!(items[0].cmd, Cmd::Submenu);
        let mut depth = 0;
        let mut at = &items[0];
        while let Some(child) = at.children.first() {
            at = child;
            depth += 1;
        }
        assert!(depth <= 4, "nesting is cut off: {depth}");
        let many: Vec<MenuEntry> = (0..500).map(|i| leaf(i, "x")).collect();
        assert_eq!(from_tray("k", &many, 0).len(), 64);
    }
}
