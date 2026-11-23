//! The taskbar: Start, Task View and the applications centred as one group;
//! the tray, the status cluster and the clock at the right edge.

use boswas_services::{TrayItem, TrayStatus};
use boswas_ui::tokens::tokens;

use crate::{
    action::Action,
    apps::App,
    control::WindowInfo,
    glyph::Glyph,
    state::{Flyout, State},
    ui::{ramp, Frame, Rect},
};

/// Tray icons shown on the taskbar itself; the rest are behind the chevron.
pub const TRAY_INLINE: usize = 3;
const TRAY_SLOT: f32 = 28.0;
const GAP: f32 = 4.0;

#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    Start,
    TaskView,
    /// The nth entry of [`tasks`].
    Task(usize),
    TrayOverflow,
    /// A tray item by key.
    Tray(String),
    Layout,
    /// The mark shown while a program may capture the screen: stops it.
    StopCapture,
    /// Network, volume and battery: opens quick settings.
    Status,
    Clock,
    ShowDesktop,
}

#[derive(Clone, Debug, PartialEq)]
pub enum PreviewAct {
    Activate(u64),
    Close(u64),
}

/// One button of the application group: a pinned application, a running
/// one, or both.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Task {
    /// Index into the application list, when the application is known.
    pub app: Option<usize>,
    /// Desktop file id, or the window's app id when no entry matches.
    pub id: String,
    pub name: String,
    pub icon: String,
    /// Windows on the current desktop, in the compositor's order.
    pub windows: Vec<u64>,
    pub active: bool,
    pub attention: bool,
    pub pinned: bool,
}

/// The buttons of the application group: the pinned applications in their
/// order, then other applications with windows on this desktop (`None`: on
/// any desktop).
pub fn tasks(apps: &[App], pinned: &[String], windows: &[WindowInfo], workspace: Option<usize>) -> Vec<Task> {
    let mut out: Vec<Task> = pinned
        .iter()
        .filter_map(|id| apps.iter().position(|a| &a.id == id))
        .map(|index| Task {
            app: Some(index),
            id: apps[index].id.clone(),
            name: apps[index].name.clone(),
            icon: apps[index].icon.clone(),
            windows: Vec::new(),
            active: false,
            attention: false,
            pinned: true,
        })
        .collect();
    for window in windows.iter().filter(|w| workspace.is_none_or(|current| w.workspace == current) || w.urgent) {
        let owner = apps.iter().position(|a| a.owns_window(&window.app_id));
        let slot = out.iter().position(|t| match owner {
            Some(index) => t.app == Some(index),
            None => t.app.is_none() && t.id == window.app_id,
        });
        let slot = slot.unwrap_or_else(|| {
            out.push(match owner {
                Some(index) => Task {
                    app: Some(index),
                    id: apps[index].id.clone(),
                    name: apps[index].name.clone(),
                    icon: apps[index].icon.clone(),
                    windows: Vec::new(),
                    active: false,
                    attention: false,
                    pinned: false,
                },
                None => Task {
                    app: None,
                    id: window.app_id.clone(),
                    name: if window.title.is_empty() { window.app_id.clone() } else { window.title.clone() },
                    icon: window.app_id.clone(),
                    windows: Vec::new(),
                    active: false,
                    attention: false,
                    pinned: false,
                },
            });
            out.len() - 1
        });
        out[slot].windows.push(window.id);
        out[slot].active |= window.active;
        out[slot].attention |= window.urgent && !window.active;
    }
    out
}

/// The window a click on a task goes to: the only window, or the one after
/// the active one. `None` when the task has no window (it is launched).
pub fn click_target(task: &Task, windows: &[WindowInfo]) -> Option<u64> {
    let active = task.windows.iter().position(|id| windows.iter().any(|w| w.id == *id && w.active));
    match (task.windows.len(), active) {
        (0, _) => None,
        (n, Some(position)) if n > 1 => Some(task.windows[(position + 1) % n]),
        _ => task.windows.first().copied(),
    }
}

/// Where everything on a taskbar of a given width goes.
#[derive(Clone, Debug, PartialEq)]
pub struct Layout {
    pub start: Rect,
    /// `None` when the Task View button is turned off.
    pub task_view: Option<Rect>,
    /// One rectangle per task that fits.
    pub tasks: Vec<Rect>,
    pub overflow: Option<Rect>,
    pub tray: Vec<Rect>,
    pub layout: Option<Rect>,
    pub status: Rect,
    pub clock: Rect,
    pub show_desktop: Rect,
}

/// How the application group is arranged (Personalization > Taskbar).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub struct Arrangement {
    /// At the left edge instead of centred.
    pub left: bool,
    pub task_view: bool,
}

impl Arrangement {
    pub fn of(st: &State) -> Self {
        Self {
            left: st.desktop.taskbar.left,
            task_view: st.desktop.taskbar.task_view,
        }
    }
}

/// Lays the taskbar out. `status_icons` is 2 or 3 (with a battery).
#[allow(clippy::too_many_arguments)]
pub fn layout(
    width: f32,
    task_count: usize,
    tray_inline: usize,
    tray_overflow: bool,
    has_layout: bool,
    status_icons: usize,
    clock_width: f32,
    arrangement: Arrangement,
) -> Layout {
    let t = tokens();
    let height = t.shell("taskbar-height");
    let button = t.shell("taskbar-button");
    let y = (height - button) / 2.0;
    // The right edge, from the outside in.
    let show_desktop = Rect::new(width - 6.0, 0.0, 6.0, height);
    let mut x = width - 6.0 - 6.0;
    let clock = Rect::new(x - clock_width, y, clock_width, button);
    x = clock.x - GAP;
    let status_width = status_icons as f32 * 16.0 + (status_icons as f32 - 1.0) * 8.0 + 20.0;
    let status = Rect::new(x - status_width, y, status_width, button);
    x = status.x - GAP;
    let layout = has_layout.then(|| {
        let rect = Rect::new(x - 40.0, y, 40.0, button);
        x = rect.x - GAP;
        rect
    });
    let mut tray = Vec::new();
    for _ in 0..tray_inline {
        tray.push(Rect::new(x - TRAY_SLOT, y, TRAY_SLOT, button));
        x -= TRAY_SLOT;
    }
    tray.reverse();
    let overflow = tray_overflow.then(|| {
        let rect = Rect::new(x - TRAY_SLOT, y, TRAY_SLOT, button);
        x = rect.x;
        rect
    });
    let right_edge = x - 8.0;
    // The group: Start, Task View, then the applications that fit; centred,
    // or at the left edge.
    let slot = button + GAP;
    let fixed = if arrangement.task_view { 2 } else { 1 };
    let available = (right_edge - 8.0).max(0.0);
    let fit = ((available / slot).floor() as usize).saturating_sub(fixed).min(task_count);
    let group = (fit + fixed) as f32 * slot - GAP;
    let mut gx = if arrangement.left { 12.0 } else { ((width - group) / 2.0).min(right_edge - group).max(8.0).round() };
    let start = Rect::new(gx, y, button, button);
    gx += slot;
    let task_view = arrangement.task_view.then(|| {
        let rect = Rect::new(gx, y, button, button);
        gx += slot;
        rect
    });
    let tasks = (0..fit).map(|i| Rect::new(gx + i as f32 * slot, y, button, button)).collect();
    Layout {
        start,
        task_view,
        tasks,
        overflow,
        tray,
        layout,
        status,
        clock,
        show_desktop,
    }
}

/// Tray places taken by the capture mark: one while any program may capture
/// the screen.
pub fn capture_marks(st: &State) -> usize {
    usize::from(!st.capturing.is_empty())
}

/// What the capture mark says when the pointer rests on it.
pub fn capture_tip(programs: &[String]) -> String {
    let who = match programs {
        [] => return String::new(),
        [one] => one.clone(),
        [first, second] => format!("{first} and {second}"),
        [first, rest @ ..] => format!("{first} and {} more", rest.len()),
    };
    format!("{who} can see your screen\nClick to stop")
}

/// The tray items shown on the taskbar and those behind the chevron.
pub fn split_tray(items: &[TrayItem]) -> (Vec<&TrayItem>, Vec<&TrayItem>) {
    let (mut inline, mut hidden) = (Vec::new(), Vec::new());
    for item in items {
        if item.status != TrayStatus::Passive && inline.len() < TRAY_INLINE {
            inline.push(item);
        } else {
            hidden.push(item);
        }
    }
    (inline, hidden)
}

/// Draws a tray item's icon in a 16 px square.
pub fn draw_tray_icon(f: &mut Frame, item: &TrayItem, x: f32, y: f32) {
    if !item.icon_name.is_empty() && f.icon(&item.icon_name, x, y, 16.0) {
        return;
    }
    if let Some(pixmap) = &item.icon_pixmap {
        if let Some(converted) = pixmap_from_rgba(pixmap.width, pixmap.height, &pixmap.rgba) {
            f.canvas.draw_pixmap(&converted, x, y, 16.0, 16.0, 1.0);
            return;
        }
    }
    let theme = f.theme();
    f.canvas.fill_rounded_rect(x + 1.0, y + 1.0, 14.0, 14.0, 4.0, theme.surface_hover);
    let initial: String = item.title.chars().next().map(|c| c.to_uppercase().collect()).unwrap_or_default();
    f.text_centered(&initial, Rect::new(x, y, 16.0, 16.0), &ramp("caption", theme.text));
}

/// A premultiplied pixmap from straight RGBA bytes another program sent.
pub fn pixmap_from_rgba(width: u32, height: u32, rgba: &[u8]) -> Option<tiny_skia::Pixmap> {
    if width == 0 || height == 0 || width > 512 || height > 512 || rgba.len() != (width * height * 4) as usize {
        return None;
    }
    let mut pixmap = tiny_skia::Pixmap::new(width, height)?;
    for (dst, src) in pixmap.data_mut().chunks_exact_mut(4).zip(rgba.chunks_exact(4)) {
        let a = u32::from(src[3]);
        dst[0] = ((u32::from(src[0]) * a + 127) / 255) as u8;
        dst[1] = ((u32::from(src[1]) * a + 127) / 255) as u8;
        dst[2] = ((u32::from(src[2]) * a + 127) / 255) as u8;
        dst[3] = src[3];
    }
    Some(pixmap)
}

/// The clock's two lines, at the text size as far as the taskbar allows.
fn clock_style(color: boswas_ui::Rgba) -> boswas_ui::text::TextStyle {
    let room = (tokens().shell("taskbar-button") - 4.0) / 2.0;
    crate::ui::ramp_within("caption", color, room)
}

/// Width of the clock button for the given time and date strings.
pub fn clock_width(f: &mut Frame, st: &State) -> f32 {
    let style = clock_style(f.theme().text);
    let widest = f.res.text.measure(&st.status.time, &style).0.max(f.res.text.measure(&st.status.date, &style).0);
    let bell = if st.notes.dnd || st.notes.unread > 0 { 22.0 } else { 0.0 };
    (widest + 20.0 + bell).round()
}

pub fn draw(f: &mut Frame, st: &State) {
    let theme = f.theme();
    let t = tokens();
    let (w, h) = (f.canvas.width(), f.canvas.height());
    let radius = t.radius_control;
    f.material(Rect::new(0.0, 0.0, w, h), [0.0; 4], "chrome");
    f.divider(0.0, 0.0, w);
    f.area(Rect::new(0.0, 0.0, w, h), Action::None);

    let list = st.tasks();
    let (inline, hidden) = split_tray(&st.sys.tray);
    let status_icons = if st.sys.power.battery.is_some() { 3 } else { 2 };
    let clock_w = clock_width(f, st);
    // While a program may capture the screen, its mark takes the tray's
    // first place.
    let marks = capture_marks(st);
    let lay = layout(w, list.len(), inline.len() + marks, !hidden.is_empty(), st.keyboard_layout().is_some(), status_icons, clock_w, Arrangement::of(st));

    // Start.
    let touch = f.hit(lay.start, Action::Taskbar(Act::Start));
    f.tip("Start");
    f.state_layer(lay.start, radius, touch, st.flyout == Some(Flyout::Start));
    let size = if touch.pressed { 22.0 } else { 26.0 };
    let (cx, cy) = lay.start.center();
    if !f.icon("boswas-logo", cx - size / 2.0, cy - size / 2.0, size) {
        f.canvas.fill_rounded_rect(cx - 10.0, cy - 10.0, 20.0, 20.0, 4.0, theme.accent);
    }

    // Task View.
    if let Some(rect) = lay.task_view {
        let touch = f.hit(rect, Action::Taskbar(Act::TaskView));
        f.tip("Task View");
        f.state_layer(rect, radius, touch, false);
        let (cx, cy) = rect.center();
        f.glyph(Glyph::TaskView, Rect::new(cx - 10.0, cy - 10.0, 20.0, 20.0), theme.text);
    }

    // Applications.
    let icon_size = t.shell("taskbar-icon");
    let (idle_w, idle_h) = t.shell_size("taskbar-indicator");
    let (active_w, _) = t.shell_size("taskbar-indicator-active");
    for (index, (task, rect)) in list.iter().zip(&lay.tasks).enumerate() {
        let touch = f.hit(*rect, Action::Taskbar(Act::Task(index)));
        f.tip(&task.name);
        if task.attention {
            f.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, theme.warning.alpha(0.28));
        } else if task.active {
            let fill = f.res.fill("control");
            let stroke = f.res.stroke("control");
            f.canvas.fill_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, fill);
            f.canvas.stroke_rounded_rect(rect.x, rect.y, rect.w, rect.h, radius, 1.0, stroke);
        }
        f.state_layer(*rect, radius, touch, false);
        let size = if touch.pressed { icon_size - 4.0 } else { icon_size };
        let (cx, cy) = rect.center();
        let generic = if task.app.is_some() { "application-x-executable" } else { "" };
        f.icon_or_placeholder(&[&task.icon, generic], cx - size / 2.0, cy - size / 2.0 - 1.0, size);
        if !task.windows.is_empty() {
            let (pw, color) = if task.active { (active_w, theme.accent) } else { (idle_w, theme.text_muted) };
            f.canvas.fill_rounded_rect(cx - pw / 2.0, rect.bottom() - idle_h - 1.0, pw, idle_h, idle_h / 2.0, color);
        }
        let count = if st.desktop.taskbar.badges { badge_count(st, task) } else { 0 };
        if count > 0 {
            // The number of the application's notifications in the centre.
            let label = if count > 9 { "9+".to_owned() } else { count.to_string() };
            let style = ramp("caption", theme.on_accent).weight(600);
            let bw = (f.res.text.measure(&label, &style).0 + 8.0).max(16.0);
            let badge = Rect::new((rect.right() - bw - 1.0).min(cx + icon_size / 2.0 + 4.0 - bw / 2.0), rect.y + 1.0, bw, 16.0);
            f.canvas.fill_rounded_rect(badge.x - 1.0, badge.y - 1.0, badge.w + 2.0, badge.h + 2.0, 9.0, theme.background);
            f.canvas.fill_rounded_rect(badge.x, badge.y, badge.w, badge.h, 8.0, theme.accent);
            f.text_centered(&label, badge, &style);
        }
    }

    // Tray.
    if let Some(rect) = lay.overflow {
        let touch = f.hit(rect, Action::Taskbar(Act::TrayOverflow));
        f.tip("Show hidden icons");
        f.state_layer(rect, radius, touch, st.flyout == Some(Flyout::TrayOverflow));
        let (cx, cy) = rect.center();
        f.glyph(Glyph::ChevronUp, Rect::new(cx - 6.0, cy - 6.0, 12.0, 12.0), theme.text);
    }
    if let Some(rect) = lay.tray.first().filter(|_| marks > 0) {
        let touch = f.hit(*rect, Action::Taskbar(Act::StopCapture));
        f.tip(&capture_tip(&st.capturing));
        f.state_layer(*rect, radius, touch, false);
        let (cx, cy) = rect.center();
        f.canvas.fill_circle(cx, cy, 7.0, theme.danger.alpha(0.28));
        f.canvas.fill_circle(cx, cy, 4.0, theme.danger);
    }
    for (item, rect) in inline.iter().zip(lay.tray.iter().skip(marks)) {
        let touch = f.hit(*rect, Action::Taskbar(Act::Tray(item.key.clone())));
        f.tip(if item.tooltip.is_empty() { &item.title } else { &item.tooltip });
        f.state_layer(*rect, radius, touch, false);
        let (cx, cy) = rect.center();
        draw_tray_icon(f, item, cx - 8.0, cy - 8.0);
        if item.status == TrayStatus::NeedsAttention {
            f.canvas.fill_circle(rect.right() - 7.0, rect.y + 11.0, 3.0, theme.warning);
        }
    }

    // Keyboard layout.
    if let (Some(rect), Some(name)) = (lay.layout, st.keyboard_layout()) {
        let touch = f.hit(rect, Action::Taskbar(Act::Layout));
        f.tip("Keyboard layout: click to switch");
        f.state_layer(rect, radius, touch, false);
        let label: String = name.chars().take(3).collect::<String>().to_uppercase();
        f.text_centered(&label, rect, &ramp("caption", theme.text));
    }

    // Network, volume, battery.
    let touch = f.hit(lay.status, Action::Taskbar(Act::Status));
    let mut tip = format!("{}\n{}", st.sys.network_text(), st.sys.volume_text());
    if let Some(battery) = st.sys.battery_text() {
        tip.push('\n');
        tip.push_str(&battery);
    }
    f.tip(&tip);
    f.state_layer(lay.status, radius, touch, st.flyout == Some(Flyout::Quick));
    let cy = lay.status.y + (lay.status.h - 16.0) / 2.0;
    let mut x = lay.status.x + 10.0;
    for icon in [Some(st.sys.network_icon()), Some(st.sys.volume_icon()), st.sys.battery_icon()].into_iter().flatten() {
        f.icon(icon, x, cy, 16.0);
        x += 24.0;
    }

    // Clock, with the notification mark.
    let touch = f.hit(lay.clock, Action::Taskbar(Act::Clock));
    f.tip(&chrono::Local::now().format("%A, %-d %B %Y").to_string());
    f.state_layer(lay.clock, radius, touch, st.flyout == Some(Flyout::Notifications));
    let caption = clock_style(theme.text);
    let line = caption.line_height();
    let mark = if st.notes.dnd || st.notes.unread > 0 { 22.0 } else { 0.0 };
    let text = Rect::new(lay.clock.x + 8.0, lay.clock.y, lay.clock.w - 16.0 - mark, lay.clock.h);
    let top = lay.clock.y + (lay.clock.h - 2.0 * line) / 2.0;
    f.text_right(&st.status.time, Rect::new(text.x, top, text.w, line), &caption);
    f.text_right(&st.status.date, Rect::new(text.x, top + line, text.w, line), &caption);
    if st.notes.dnd {
        f.icon("notification-disabled-symbolic", lay.clock.right() - 24.0, lay.clock.y + (lay.clock.h - 16.0) / 2.0, 16.0);
    } else if st.notes.unread > 0 {
        let (cx, cy) = (lay.clock.right() - 16.0, lay.clock.y + lay.clock.h / 2.0);
        f.canvas.fill_circle(cx, cy, 8.0, theme.accent);
        let count = if st.notes.unread > 9 { "9+".to_owned() } else { st.notes.unread.to_string() };
        f.text_centered(&count, Rect::new(cx - 8.0, cy - 8.0, 16.0, 16.0), &ramp("caption", theme.on_accent).weight(600));
    }

    // Show desktop.
    let touch = f.area(lay.show_desktop, Action::Taskbar(Act::ShowDesktop));
    f.tip("Show desktop");
    let line_color = f.res.stroke("divider");
    f.canvas.fill_rect(lay.show_desktop.x, 12.0, 1.0, h - 24.0, line_color);
    if touch.hovered {
        let fill = f.res.fill("subtle-hover");
        f.canvas.fill_rect(lay.show_desktop.x, 0.0, lay.show_desktop.w, h, fill);
    }
}

/// How many notifications of a task's application the notification centre
/// holds: those whose desktop entry is the application's, or whose sender
/// calls itself by the application's name.
pub fn badge_count(st: &State, task: &Task) -> usize {
    st.notes
        .history
        .iter()
        .filter(|note| note.desktop_entry.as_deref().is_some_and(|entry| entry == task.id || entry.strip_suffix(".desktop") == Some(task.id.as_str())) || note.app_name == task.name)
        .count()
}

/// The flyout of tray items that are not on the taskbar: a grid of icons.
pub fn overflow_size(st: &State) -> (f32, f32) {
    let count = split_tray(&st.sys.tray).1.len().max(1);
    let columns = count.min(5);
    let rows = count.div_ceil(5);
    (columns as f32 * 40.0 + 8.0, rows as f32 * 40.0 + 8.0)
}

pub fn draw_overflow(f: &mut Frame, st: &State, panel: Rect) {
    f.panel(panel, "acrylic");
    f.area(panel, Action::None);
    let radius = tokens().radius_control;
    let (_, hidden) = split_tray(&st.sys.tray);
    for (index, item) in hidden.iter().enumerate() {
        let rect = Rect::new(panel.x + 4.0 + (index % 5) as f32 * 40.0, panel.y + 4.0 + (index / 5) as f32 * 40.0, 40.0, 40.0);
        let touch = f.hit(rect, Action::Taskbar(Act::Tray(item.key.clone())));
        f.tip(if item.tooltip.is_empty() { &item.title } else { &item.tooltip });
        f.state_layer(rect.inset(2.0), radius, touch, false);
        draw_tray_icon(f, item, rect.x + 12.0, rect.y + 12.0);
    }
}

/// The window previews of one task, shown above its button.
#[derive(Clone, Debug, PartialEq)]
pub struct Preview {
    /// Index of the task in [`tasks`].
    pub task: usize,
    pub windows: Vec<u64>,
    /// Centre of the task's button on the output.
    pub x: f32,
    /// The pointer left both the button and the preview at this time.
    pub left_at: Option<std::time::Instant>,
}

const CARD_W: f32 = 208.0;
const CARD_H: f32 = 156.0;

impl Preview {
    /// Size of the preview's panel.
    pub fn size(&self) -> (f32, f32) {
        let count = self.windows.len().clamp(1, 6) as f32;
        (count * CARD_W + 8.0, CARD_H + 8.0)
    }
}

pub fn draw_preview(f: &mut Frame, st: &State, preview: &Preview, panel: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    f.panel(panel, "acrylic");
    f.area(panel, Action::None);
    for (index, id) in preview.windows.iter().take(6).enumerate() {
        let Some(window) = st.windows.iter().find(|w| w.id == *id) else {
            continue;
        };
        let card = Rect::new(panel.x + 4.0 + index as f32 * CARD_W, panel.y + 4.0, CARD_W, CARD_H);
        let touch_card = f.hit(card.inset(2.0), Action::Preview(PreviewAct::Activate(*id)));
        f.state_layer(card.inset(2.0), radius, touch_card, window.active);
        let app = st.apps.iter().find(|a| a.owns_window(&window.app_id));
        let icon = app.map_or(window.app_id.as_str(), |a| a.icon.as_str());
        f.icon_or_placeholder(&[icon], card.x + 12.0, card.y + 10.0, 16.0);
        let title = if window.title.is_empty() { app.map_or("Window", |a| a.name.as_str()) } else { window.title.as_str() };
        f.text_left(title, Rect::new(card.x + 36.0, card.y + 6.0, CARD_W - 36.0 - 36.0, 24.0), &ramp("caption", theme.text));
        let close = Rect::new(card.right() - 32.0, card.y + 6.0, 24.0, 24.0);
        if touch_card.hovered {
            let touch = f.hit(close, Action::Preview(PreviewAct::Close(*id)));
            f.tip("Close");
            if touch.hovered {
                let danger = tokens().shell_color("caption-close-hover");
                f.canvas.fill_rounded_rect(close.x, close.y, close.w, close.h, radius, danger);
            }
            f.glyph(Glyph::Close, close.inset(6.0), theme.text);
        }
        let area = Rect::new(card.x + 10.0, card.y + 36.0, CARD_W - 20.0, CARD_H - 46.0);
        match st.thumbnails.get(id) {
            Some(thumbnail) => {
                // Fit inside the area, aspect kept.
                let (tw, th) = (thumbnail.width() as f32, thumbnail.height() as f32);
                let scale = (area.w / tw).min(area.h / th);
                let (dw, dh) = (tw * scale, th * scale);
                f.pixmap(thumbnail, Rect::new(area.x + (area.w - dw) / 2.0, area.y + (area.h - dh) / 2.0, dw, dh));
            }
            None => {
                let fill = f.res.fill("control");
                f.canvas.fill_rounded_rect(area.x, area.y, area.w, area.h, radius, fill);
                let (cx, cy) = area.center();
                f.icon_or_placeholder(&[icon], cx - 24.0, cy - 24.0, 48.0);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn app(id: &str, name: &str) -> App {
        App {
            id: id.into(),
            name: name.into(),
            comment: String::new(),
            icon: id.into(),
            exec: vec![id.into()],
            terminal: false,
            keywords: Vec::new(),
            wm_class: String::new(),
            actions: Vec::new(),
            categories: Vec::new(),
        }
    }

    fn window(id: u64, app_id: &str, active: bool, workspace: usize) -> WindowInfo {
        WindowInfo {
            id,
            title: format!("Window {id}"),
            app_id: app_id.into(),
            active,
            minimized: false,
            workspace,
            urgent: false,
        }
    }

    #[test]
    fn the_capture_mark_names_who_can_see_the_screen() {
        assert_eq!(capture_tip(&[]), "");
        assert_eq!(capture_tip(&["obs".into()]), "obs can see your screen\nClick to stop");
        assert_eq!(capture_tip(&["obs".into(), "grim".into()]), "obs and grim can see your screen\nClick to stop");
        assert_eq!(capture_tip(&["a".into(), "b".into(), "c".into()]), "a and 2 more can see your screen\nClick to stop");
    }

    #[test]
    fn tasks_group_windows_under_pinned_and_running_applications() {
        let apps = vec![app("org.kde.dolphin", "Files"), app("firefox-esr", "Firefox"), app("org.kde.konsole", "Terminal")];
        let pinned = vec!["org.kde.dolphin".to_owned(), "missing.app".to_owned(), "firefox-esr".to_owned()];
        let mut urgent = window(6, "org.kde.konsole", false, 3);
        urgent.urgent = true;
        let windows = vec![
            window(1, "org.kde.konsole", false, 0),
            window(2, "firefox-esr", true, 0),
            window(3, "foot", false, 0),
            window(4, "org.kde.dolphin", false, 1),
            window(5, "firefox-esr", false, 0),
            urgent,
        ];
        let list = tasks(&apps, &pinned, &windows, Some(0));
        assert_eq!(list.iter().map(|t| t.name.as_str()).collect::<Vec<_>>(), ["Files", "Firefox", "Terminal", "Window 3"]);
        assert!(list[0].pinned && list[0].windows.is_empty(), "the window on another desktop is not shown");
        assert_eq!(list[1].windows, [2, 5]);
        assert!(list[1].active && !list[1].attention);
        assert_eq!(list[2].windows, [1, 6], "an urgent window shows from any desktop");
        assert!(list[2].attention && !list[2].pinned);
        assert_eq!((list[3].app, list[3].id.as_str()), (None, "foot"));
        // Multitasking > "show windows of all desktops on the taskbar".
        let all = tasks(&apps, &pinned, &windows, None);
        assert_eq!(all[0].windows, [4], "the window on desktop 2 is there too");
        assert_eq!(all.len(), list.len());
    }

    #[test]
    fn a_click_goes_to_the_only_window_or_cycles() {
        let apps = vec![app("firefox-esr", "Firefox")];
        let windows = vec![window(2, "firefox-esr", false, 0), window(5, "firefox-esr", true, 0), window(7, "firefox-esr", false, 0)];
        let list = tasks(&apps, &[], &windows, Some(0));
        assert_eq!(click_target(&list[0], &windows), Some(7), "the window after the active one");
        let inactive: Vec<WindowInfo> = windows.iter().cloned().map(|mut w| { w.active = false; w }).collect();
        assert_eq!(click_target(&list[0], &inactive), Some(2));
        let single = vec![window(9, "firefox-esr", true, 0)];
        let list = tasks(&apps, &[], &single, Some(0));
        assert_eq!(click_target(&list[0], &single), Some(9), "the caller minimises an active single window");
        let pinned = tasks(&apps, &["firefox-esr".to_owned()], &[], Some(0));
        assert_eq!(click_target(&pinned[0], &[]), None);
    }

    #[test]
    fn the_layout_centres_the_group_and_never_overlaps_the_tray() {
        let centred = Arrangement { left: false, task_view: true };
        let lay = layout(1920.0, 6, 2, true, false, 3, 80.0, centred);
        let left = lay.start.x;
        let right = lay.tasks.last().unwrap().right();
        assert!(((left + right) / 2.0 - 960.0).abs() <= 1.0, "centred: {left}..{right}");
        assert_eq!(lay.tasks.len(), 6);
        assert_eq!(lay.start.w, 40.0);
        assert_eq!(lay.show_desktop.right(), 1920.0);
        assert!(lay.clock.right() < lay.show_desktop.x);
        assert!(lay.status.right() <= lay.clock.x);
        assert!(lay.tray[1].right() <= lay.status.x && lay.tray[0].right() <= lay.tray[1].x + 0.1);
        assert!(lay.overflow.unwrap().right() <= lay.tray[0].x + 0.1);
        assert!(lay.layout.is_none());

        // A narrow output: the group moves left and drops what does not fit.
        let narrow = layout(640.0, 30, 3, true, true, 3, 80.0, centred);
        let last = narrow.tasks.last().unwrap();
        assert!(last.right() <= narrow.overflow.unwrap().x - 7.9, "{last:?} vs {:?}", narrow.overflow);
        assert!(narrow.tasks.len() < 30 && narrow.start.x >= 8.0);
        assert!(narrow.layout.unwrap().right() <= narrow.status.x);
        let tiny = layout(200.0, 5, 0, false, false, 2, 60.0, centred);
        assert!(tiny.tasks.len() <= 1);
    }

    #[test]
    fn the_group_can_sit_at_the_left_without_task_view() {
        let left = layout(1920.0, 6, 2, true, false, 3, 80.0, Arrangement { left: true, task_view: true });
        assert_eq!(left.start.x, 12.0, "Start at the left edge");
        assert_eq!(left.task_view.unwrap().x, left.start.right() + GAP);
        assert_eq!(left.tasks[0].x, left.task_view.unwrap().right() + GAP);
        let without = layout(1920.0, 6, 2, true, false, 3, 80.0, Arrangement { left: false, task_view: false });
        assert!(without.task_view.is_none());
        assert_eq!(without.tasks[0].x, without.start.right() + GAP, "the applications follow Start");
        let right = without.tasks.last().unwrap().right();
        assert!(((without.start.x + right) / 2.0 - 960.0).abs() <= 1.0, "still centred");
        let narrow = layout(640.0, 30, 3, true, true, 3, 80.0, Arrangement { left: true, task_view: false });
        assert!(narrow.tasks.last().unwrap().right() <= narrow.overflow.unwrap().x - 7.9);
    }

    #[test]
    fn passive_and_surplus_tray_items_go_behind_the_chevron() {
        let item = |key: &str, status| TrayItem {
            key: key.into(),
            id: key.into(),
            title: key.into(),
            icon_name: String::new(),
            icon_theme_path: String::new(),
            icon_pixmap: None,
            tooltip: String::new(),
            status,
            has_menu: false,
        };
        let items = vec![
            item("a", TrayStatus::Active),
            item("b", TrayStatus::Passive),
            item("c", TrayStatus::NeedsAttention),
            item("d", TrayStatus::Active),
            item("e", TrayStatus::Active),
        ];
        let (inline, hidden) = split_tray(&items);
        assert_eq!(inline.iter().map(|i| i.key.as_str()).collect::<Vec<_>>(), ["a", "c", "d"]);
        assert_eq!(hidden.iter().map(|i| i.key.as_str()).collect::<Vec<_>>(), ["b", "e"]);
    }

    #[test]
    fn pixmaps_from_other_programs_are_checked() {
        assert!(pixmap_from_rgba(2, 2, &[255; 16]).is_some());
        assert!(pixmap_from_rgba(2, 2, &[255; 15]).is_none());
        assert!(pixmap_from_rgba(0, 2, &[]).is_none());
        assert!(pixmap_from_rgba(4096, 4096, &[]).is_none());
        let half = pixmap_from_rgba(1, 1, &[200, 100, 50, 128]).unwrap();
        assert_eq!(half.data(), [100, 50, 25, 128], "stored premultiplied");
    }
}
