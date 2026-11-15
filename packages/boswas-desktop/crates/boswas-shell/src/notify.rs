//! Notifications: the toasts above the taskbar, and the notification
//! centre with its calendar.

use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};

use boswas_services::{Notification, Urgency};
use boswas_ui::{text::TextEngine, tokens::tokens, Rgba};
use chrono::{Datelike, Local};

use crate::{
    action::Action,
    calendar,
    glyph::Glyph,
    state::State,
    ui::{ramp, Anim, ButtonKind, Frame, Rect},
};

#[derive(Clone, Debug, PartialEq)]
pub enum Act {
    /// The body of a toast: its default action.
    ToastBody(u32),
    ToastAction(u32, String),
    ToastClose(u32),
    /// The body of a card in the notification centre.
    Body(u32),
    Invoke(u32, String),
    Dismiss(u32),
    DismissApp(String),
    ClearAll,
    /// Shows all notifications of an application, or only the latest.
    ToggleGroup(String),
    Dnd,
    PreviousMonth,
    NextMonth,
    /// Back to the current month.
    Today,
    Settings,
}

/// Notifications kept in the notification centre.
pub const MAX_HISTORY: usize = 100;
/// Toasts on screen at once.
pub const MAX_TOASTS: usize = 3;
/// Notifications waiting for a free toast slot.
const MAX_WAITING: usize = 20;
/// How long a toast stays when the application does not say.
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(6);
const MIN_TIMEOUT: Duration = Duration::from_secs(3);
const MAX_TIMEOUT: Duration = Duration::from_secs(30);

pub struct Toast {
    pub note: Notification,
    /// When it leaves; `None` stays until dismissed.
    pub deadline: Option<Instant>,
    /// Time left when the pointer came to rest on the toasts.
    paused: Option<Duration>,
    pub anim: Anim,
}

#[derive(Default)]
pub struct Notes {
    /// The notification centre's content, oldest first.
    pub history: Vec<Notification>,
    /// The toasts on screen, oldest first.
    pub toasts: Vec<Toast>,
    waiting: VecDeque<Notification>,
    pub dnd: bool,
    /// Notifications that arrived since the centre was last opened.
    pub unread: usize,
    /// The calendar's month relative to today's.
    pub month: i32,
    /// Applications whose group shows all its notifications.
    pub expanded: Vec<String>,
    /// Cards scrolled past in the centre.
    pub scroll: usize,
    hovered: bool,
}

fn timeout(note: &Notification) -> Option<Duration> {
    if note.urgency == Urgency::Critical {
        return None;
    }
    Some(match note.timeout_ms {
        Some(ms) if ms > 0 => Duration::from_millis(u64::from(ms)).clamp(MIN_TIMEOUT, MAX_TIMEOUT),
        _ => DEFAULT_TIMEOUT,
    })
}

impl Notes {
    /// Takes a notification in. `quiet` is true while nothing may pop up
    /// (the session is locked): the notification is kept but not toasted.
    pub fn receive(&mut self, note: Notification, now: Instant, quiet: bool, reduced_motion: bool) {
        let replaced = self.history.iter().any(|n| n.id == note.id);
        self.history.retain(|n| n.id != note.id);
        if !note.transient {
            self.history.push(note.clone());
            let extra = self.history.len().saturating_sub(MAX_HISTORY);
            self.history.drain(..extra);
        }
        if !replaced {
            self.unread = (self.unread + 1).min(MAX_HISTORY);
        }
        // A replacement updates its toast in place and restarts its time.
        if let Some(toast) = self.toasts.iter_mut().find(|t| t.note.id == note.id) {
            toast.deadline = timeout(&note).map(|d| now + d);
            toast.paused = None;
            toast.note = note;
            return;
        }
        if let Some(waiting) = self.waiting.iter_mut().find(|n| n.id == note.id) {
            *waiting = note;
            return;
        }
        if quiet || (self.dnd && note.urgency != Urgency::Critical) {
            return;
        }
        if self.toasts.len() >= MAX_TOASTS {
            if self.waiting.len() < MAX_WAITING {
                self.waiting.push_back(note);
            }
            return;
        }
        self.show(note, now, reduced_motion);
    }

    fn show(&mut self, note: Notification, now: Instant, reduced_motion: bool) {
        self.toasts.push(Toast {
            deadline: timeout(&note).map(|d| now + d),
            paused: None,
            anim: Anim::opening(now, reduced_motion),
            note,
        });
    }

    /// The application closed a notification, or the service expired it.
    pub fn closed(&mut self, id: u32, withdrawn: bool, now: Instant, reduced_motion: bool) {
        self.leave(id, now, reduced_motion);
        self.waiting.retain(|n| n.id != id);
        if withdrawn {
            self.history.retain(|n| n.id != id);
        }
    }

    /// Starts a toast's exit.
    pub fn leave(&mut self, id: u32, now: Instant, reduced_motion: bool) {
        for toast in self.toasts.iter_mut().filter(|t| t.note.id == id && !t.anim.closing) {
            toast.anim = Anim::closing(now, reduced_motion);
            toast.deadline = None;
        }
    }

    /// The user dismissed a notification: it leaves the centre and the
    /// screen.
    pub fn dismiss(&mut self, id: u32, now: Instant, reduced_motion: bool) {
        self.closed(id, true, now, reduced_motion);
    }

    /// Ids of an application's notifications.
    pub fn ids_of(&self, app: &str) -> Vec<u32> {
        self.history.iter().filter(|n| n.app_name == app).map(|n| n.id).collect()
    }

    /// Hides every toast at once (the session locks, Task View opens).
    pub fn hide_toasts(&mut self) {
        self.toasts.clear();
        self.waiting.clear();
    }

    /// The pointer came to rest on the toasts, or left them: their time
    /// stops while it is there.
    pub fn hover(&mut self, hovered: bool, now: Instant) {
        if self.hovered == hovered {
            return;
        }
        self.hovered = hovered;
        for toast in &mut self.toasts {
            if hovered {
                toast.paused = toast.deadline.map(|d| d.saturating_duration_since(now));
                if toast.paused.is_some() {
                    toast.deadline = None;
                }
            } else if let Some(left) = toast.paused.take() {
                toast.deadline = Some(now + left.max(Duration::from_secs(1)));
            }
        }
    }

    /// Lets time pass: expired toasts leave, toasts that finished leaving
    /// are removed, and waiting notifications take the free slots. Returns
    /// whether anything changed.
    pub fn tick(&mut self, now: Instant, reduced_motion: bool) -> bool {
        let mut changed = false;
        for toast in &mut self.toasts {
            if toast.deadline.is_some_and(|d| d <= now) {
                toast.anim = Anim::closing(now, reduced_motion);
                toast.deadline = None;
                changed = true;
            }
        }
        let before = self.toasts.len();
        self.toasts.retain(|t| !(t.anim.closing && t.anim.done(now)));
        changed |= self.toasts.len() != before;
        while self.toasts.len() < MAX_TOASTS {
            let Some(note) = self.waiting.pop_front() else {
                break;
            };
            self.show(note, now, reduced_motion);
            changed = true;
        }
        changed
    }

    /// When [`Notes::tick`] next has something to do.
    pub fn next_deadline(&self, now: Instant) -> Option<Instant> {
        let animating = self.toasts.iter().any(|t| !t.anim.done(now));
        let leaving = self.toasts.iter().any(|t| t.anim.closing);
        let soonest = self.toasts.iter().filter_map(|t| t.deadline).min();
        if animating || leaving {
            Some(now + Duration::from_millis(16))
        } else {
            soonest
        }
    }

    /// Whether any toast is sliding in or out.
    pub fn animating(&self, now: Instant) -> bool {
        self.toasts.iter().any(|t| !t.anim.done(now))
    }

    /// The centre was opened: nothing is unread any more.
    pub fn seen(&mut self) {
        self.unread = 0;
        self.scroll = 0;
        self.month = 0;
    }

    /// The centre's notifications grouped by application: the group with
    /// the newest notification first, and each group newest first.
    pub fn groups(&self) -> Vec<Group<'_>> {
        let mut groups: Vec<Group<'_>> = Vec::new();
        for note in self.history.iter().rev() {
            match groups.iter_mut().find(|g| g.app == note.app_name) {
                Some(group) => group.notes.push(note),
                None => groups.push(Group {
                    app: &note.app_name,
                    notes: vec![note],
                }),
            }
        }
        groups
    }
}

pub struct Group<'a> {
    pub app: &'a str,
    pub notes: Vec<&'a Notification>,
}

/// "Now", "5 min", "2 h", "Yesterday" or a date, for a time in seconds
/// since the epoch.
pub fn relative_time(now: u64, then: u64) -> String {
    let age = now.saturating_sub(then);
    match age {
        0..=59 => "Now".into(),
        60..=3599 => format!("{} min", age / 60),
        3600..=86_399 => format!("{} h", age / 3600),
        86_400..=172_799 => "Yesterday".into(),
        _ => chrono::DateTime::from_timestamp(then as i64, 0)
            .map(|t| t.with_timezone(&Local).format("%-d %b").to_string())
            .unwrap_or_default(),
    }
}

fn icon_names(note: &Notification) -> [&str; 3] {
    [note.app_icon.as_str(), note.desktop_entry.as_deref().unwrap_or(""), "dialog-information-symbolic"]
}

/// The action buttons of a notification (everything but `default`).
fn buttons(note: &Notification) -> Vec<&(String, String)> {
    note.actions.iter().filter(|(key, label)| key != "default" && !label.is_empty()).take(3).collect()
}

pub const TOAST_GAP: f32 = 8.0;
const CARD_PAD: f32 = 14.0;

/// Height of a toast for a notification.
pub fn toast_height(text: &mut TextEngine, note: &Notification, width: f32) -> f32 {
    let body = ramp("body", Rgba::WHITE);
    let lines = if note.body.trim().is_empty() { 0 } else { text.wrap(&note.body, &body, width - 2.0 * CARD_PAD, 4).len() };
    let mut height = 36.0 + 22.0 + lines as f32 * body.line_height() + CARD_PAD;
    if note.progress.is_some() {
        height += 14.0;
    }
    if !buttons(note).is_empty() {
        height += 44.0;
    }
    height
}

/// Size of the surface that holds the toasts: their width and the height
/// of the stack, with room for their shadows.
pub fn toasts_size(text: &mut TextEngine, notes: &Notes) -> (f32, f32) {
    let t = tokens();
    let width = t.shell("toast-width");
    let margin = t.shell("flyout-margin");
    let stack: f32 = notes.toasts.iter().map(|toast| toast_height(text, &toast.note, width) + TOAST_GAP).sum();
    (width + 2.0 * margin, stack + 2.0 * margin - TOAST_GAP.min(stack))
}

#[allow(clippy::too_many_arguments)]
fn draw_card(f: &mut Frame, note: &Notification, card: Rect, toast: bool, now_secs: u64, body_lines: usize) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let id = note.id;
    let body_act = if toast { Act::ToastBody(id) } else { Act::Body(id) };
    let touch = f.hit(card, Action::Notify(body_act));
    if touch.hovered && !toast {
        let hover = f.res.fill("subtle-hover");
        f.canvas.fill_rounded_rect(card.x, card.y, card.w, card.h, radius, hover);
    }
    if touch.focused {
        f.focus_ring(card, radius);
    }
    let x = card.x + CARD_PAD;
    let w = card.w - 2.0 * CARD_PAD;
    let mut y = card.y;
    if toast {
        // The application's line: icon, name, time, close.
        f.icon_or_placeholder(&icon_names(note), x, y + 12.0, 16.0);
        f.text_left(&note.app_name, Rect::new(x + 24.0, y + 8.0, w - 24.0 - 64.0, 24.0), &ramp("caption", theme.text_secondary));
        y += 36.0;
    } else {
        y += 10.0;
    }
    let time = relative_time(now_secs, note.time);
    let close = Rect::new(card.right() - 34.0, card.y + 8.0, 24.0, 24.0);
    if touch.hovered {
        let close_act = if toast { Act::ToastClose(id) } else { Act::Dismiss(id) };
        let t = f.hit(close, Action::Notify(close_act));
        f.tip(if toast { "Close" } else { "Clear" });
        f.state_layer(close, radius, t, false);
        f.glyph(Glyph::Close, close.inset(6.0), theme.text_secondary);
    } else {
        f.text_right(&time, Rect::new(card.right() - 90.0, card.y + 8.0, 76.0, 24.0), &ramp("caption", theme.text_muted));
    }
    let title_w = if toast { w } else { w - 70.0 };
    f.text_left(&note.summary, Rect::new(x, y, title_w, 22.0), &ramp("body-strong", theme.text));
    y += 22.0;
    if !note.body.trim().is_empty() {
        y += f.text_wrapped(&note.body, x, y, w, body_lines, &ramp("body", theme.text_secondary));
    }
    if let Some(progress) = note.progress {
        let rail = f.res.fill("control-hover");
        f.canvas.fill_rounded_rect(x, y + 6.0, w, 4.0, 2.0, rail);
        f.canvas.fill_rounded_rect(x, y + 6.0, w * f32::from(progress.min(100)) / 100.0, 4.0, 2.0, theme.accent);
        y += 14.0;
    }
    let actions = buttons(note);
    if !actions.is_empty() {
        let gap = 8.0;
        let width = (w - gap * (actions.len() as f32 - 1.0)) / actions.len() as f32;
        for (index, (key, label)) in actions.iter().enumerate() {
            let rect = Rect::new(x + index as f32 * (width + gap), y + 8.0, width, 30.0);
            let act = if toast { Act::ToastAction(id, key.clone()) } else { Act::Invoke(id, key.clone()) };
            f.button(rect, label, ButtonKind::Standard, true, Action::Notify(act));
        }
    }
}

/// Draws the toasts: the newest at the bottom, older ones above it.
pub fn draw_toasts(f: &mut Frame, st: &State, now: Instant) {
    let t = tokens();
    let width = t.shell("toast-width");
    let margin = t.shell("flyout-margin");
    let now_secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
    let mut bottom = f.canvas.height() - margin;
    for toast in st.notes.toasts.iter().rev() {
        let height = toast_height(&mut f.res.text, &toast.note, width);
        // Toasts slide in from the right edge and leave the same way.
        let slide = (1.0 - toast.anim.presence(now)) * (width + margin);
        let card = Rect::new(margin + slide, bottom - height, width, height);
        let e = t.elevation("flyout");
        f.canvas.shadow(card.x, card.y, card.w, card.h, t.radius_flyout, e.blur, e.y, e.alpha);
        f.material(card, [t.radius_flyout; 4], "acrylic");
        let stroke = f.res.stroke("surface");
        f.canvas.stroke_rounded_rect(card.x, card.y, card.w, card.h, t.radius_flyout, 1.0, stroke);
        if toast.note.urgency == Urgency::Critical {
            let danger = f.theme().danger;
            f.canvas.fill_rounded_rect(card.x + 1.0, card.y + 10.0, 3.0, card.h - 20.0, 1.5, danger);
        }
        if slide < 1.0 {
            draw_card(f, &toast.note, card, true, now_secs, 4);
        } else {
            // Still moving: drawn, but not yet clickable.
            let mut hits = Vec::new();
            let mut moving = Frame {
                canvas: &mut *f.canvas,
                res: &mut *f.res,
                pointer: None,
                pressed: false,
                hits: &mut hits,
                focus: None,
                origin: f.origin,
            };
            draw_card(&mut moving, &toast.note, card, true, now_secs, 4);
        }
        bottom -= height + TOAST_GAP;
    }
}

/// Height of a notification's card in the centre.
fn card_height(f: &mut Frame, note: &Notification, width: f32, lines: usize) -> f32 {
    let body = ramp("body", Rgba::WHITE);
    let wrapped = if note.body.trim().is_empty() { 0 } else { f.res.text.wrap(&note.body, &body, width - 2.0 * CARD_PAD, lines).len() };
    let mut height = 10.0 + 22.0 + wrapped as f32 * body.line_height() + 12.0;
    if note.progress.is_some() {
        height += 14.0;
    }
    if !buttons(note).is_empty() {
        height += 40.0;
    }
    height
}

pub const CALENDAR_H: f32 = 368.0;

/// The notification centre: the notifications above, the calendar below.
pub fn draw_centre(f: &mut Frame, st: &State, area: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    let calendar_h = CALENDAR_H.min(area.h - 132.0).max(0.0);
    let show_calendar = calendar_h >= 300.0;
    let calendar_panel = Rect::new(area.x, area.bottom() - calendar_h, area.w, calendar_h);
    let list_room = if show_calendar { area.h - calendar_h - 12.0 } else { area.h };
    let groups = st.notes.groups();
    // With nothing to show, the notifications panel is a short note that
    // sits on the calendar.
    let list_h = if groups.is_empty() { 120.0_f32.min(list_room) } else { list_room };
    let top = if show_calendar { calendar_panel.y - 12.0 - list_h } else { area.y };
    let panel = Rect::new(area.x, top, area.w, list_h);
    f.panel(panel, "acrylic");
    f.area(panel, Action::None);

    // Header.
    f.text_left("Notifications", Rect::new(panel.x + 16.0, panel.y + 8.0, 150.0, 32.0), &ramp("body-strong", theme.text));
    let dnd = Rect::new(panel.right() - 44.0, panel.y + 8.0, 32.0, 32.0);
    let touch = f.hit(dnd, Action::Notify(Act::Dnd));
    f.tip(if st.notes.dnd { "Do not disturb is on" } else { "Do not disturb is off" });
    if st.notes.dnd {
        f.canvas.fill_rounded_rect(dnd.x, dnd.y, dnd.w, dnd.h, radius, theme.accent);
        f.icon_tinted("notification-disabled-symbolic", dnd.x + 8.0, dnd.y + 8.0, 16.0, theme.on_accent);
        if touch.focused {
            f.focus_ring(dnd, radius);
        }
    } else {
        f.state_layer(dnd, radius, touch, false);
        f.icon("notification-disabled-symbolic", dnd.x + 8.0, dnd.y + 8.0, 16.0);
    }
    let settings = Rect::new(dnd.x - 36.0, panel.y + 8.0, 32.0, 32.0);
    f.icon_button(settings, "preferences-system-symbolic", "Notification settings", Action::Notify(Act::Settings));
    if !groups.is_empty() {
        let width = f.res.text.measure("Clear all", &ramp("caption", theme.text)).0 + 24.0;
        let rect = Rect::new(settings.x - width - 8.0, panel.y + 10.0, width, 28.0);
        let touch = f.hit(rect, Action::Notify(Act::ClearAll));
        f.state_layer(rect, radius, touch, false);
        f.text_centered("Clear all", rect, &ramp("caption", theme.text));
    }

    if groups.is_empty() {
        let text = if st.notes.dnd { "Do not disturb is on. New notifications arrive silently." } else { "No new notifications" };
        f.text_wrapped(text, panel.x + 16.0, panel.y + 52.0, panel.w - 32.0, 2, &ramp("body", theme.text_muted));
    } else {
        let now_secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
        let bottom = panel.bottom() - 8.0;
        let mut y = panel.y + 48.0;
        let mut skip = st.notes.scroll;
        let mut hidden_below = false;
        'groups: for group in &groups {
            let expanded = st.notes.expanded.iter().any(|a| a == group.app);
            let shown = if expanded { group.notes.len() } else { 1 };
            // The group's line: icon, name, count, show more or fewer, clear.
            if skip == 0 {
                let first = card_height(f, group.notes[0], panel.w - 16.0, if expanded { 4 } else { 2 });
                if y + 32.0 + first > bottom {
                    hidden_below = true;
                    break;
                }
                let head = Rect::new(panel.x + 8.0, y, panel.w - 16.0, 32.0);
                f.icon_or_placeholder(&icon_names(group.notes[0]), head.x + 8.0, head.y + 8.0, 16.0);
                f.text_left(group.app, Rect::new(head.x + 32.0, head.y, head.w - 32.0 - 110.0, 32.0), &ramp("caption", theme.text_secondary));
                let clear = Rect::new(head.right() - 28.0, head.y + 4.0, 24.0, 24.0);
                f.glyph_button(clear, Glyph::Close, &format!("Clear all from {}", group.app), Action::Notify(Act::DismissApp(group.app.to_owned())));
                if group.notes.len() > 1 {
                    let label = if expanded { "Show fewer".to_owned() } else { format!("+{} more", group.notes.len() - 1) };
                    let width = f.res.text.measure(&label, &ramp("caption", theme.text)).0 + 16.0;
                    let rect = Rect::new(clear.x - width - 4.0, head.y + 4.0, width, 24.0);
                    let touch = f.hit(rect, Action::Notify(Act::ToggleGroup(group.app.to_owned())));
                    f.state_layer(rect, radius, touch, false);
                    f.text_centered(&label, rect, &ramp("caption", theme.accent));
                }
                y += 32.0;
            }
            for note in group.notes.iter().take(shown) {
                if skip > 0 {
                    skip -= 1;
                    continue;
                }
                let lines = if expanded { 4 } else { 2 };
                let height = card_height(f, note, panel.w - 16.0, lines);
                if y + height > bottom {
                    hidden_below = true;
                    break 'groups;
                }
                let card = Rect::new(panel.x + 8.0, y, panel.w - 16.0, height);
                f.card(card, radius);
                draw_card(f, note, card, false, now_secs, lines);
                y += height + 6.0;
            }
            y += 4.0;
        }
        if st.notes.scroll > 0 || hidden_below {
            let total = st.notes.history.len() as f32;
            let view = Rect::new(panel.x, panel.y + 48.0, panel.w, panel.h - 56.0);
            f.scrollbar(view, view.h * (total + 1.0) / (total - st.notes.scroll as f32).max(1.0), st.notes.scroll as f32 * view.h / total.max(1.0));
        }
    }

    if show_calendar {
        draw_calendar(f, st, calendar_panel);
    }
}

fn draw_calendar(f: &mut Frame, st: &State, panel: Rect) {
    let theme = f.theme();
    let radius = tokens().radius_control;
    f.panel(panel, "acrylic");
    f.area(panel, Action::None);
    let today = Local::now().date_naive();
    let touch = f.hit(Rect::new(panel.x + 8.0, panel.y + 8.0, panel.w - 16.0, 36.0), Action::Notify(Act::Today));
    f.state_layer(Rect::new(panel.x + 8.0, panel.y + 8.0, panel.w - 16.0, 36.0), radius, touch, false);
    f.text_left(&today.format("%A, %-d %B").to_string(), Rect::new(panel.x + 16.0, panel.y + 8.0, panel.w - 32.0, 36.0), &ramp("body-strong", theme.text));
    f.divider(panel.x + 1.0, panel.y + 52.0, panel.w - 2.0);

    let month = calendar::month(today, st.notes.month, calendar::week_start(&st.locale));
    let title = format!("{} {}", calendar::month_name(month.month), month.year);
    f.text_left(&title, Rect::new(panel.x + 16.0, panel.y + 60.0, panel.w - 110.0, 32.0), &ramp("body-strong", theme.text));
    f.glyph_button(Rect::new(panel.right() - 80.0, panel.y + 60.0, 32.0, 32.0), Glyph::ChevronUp, "Previous month", Action::Notify(Act::PreviousMonth));
    f.glyph_button(Rect::new(panel.right() - 44.0, panel.y + 60.0, 32.0, 32.0), Glyph::ChevronDown, "Next month", Action::Notify(Act::NextMonth));

    let cell_w = (panel.w - 24.0) / 7.0;
    let grid_top = panel.y + 100.0;
    for (column, name) in month.header.iter().enumerate() {
        f.text_centered(name, Rect::new(panel.x + 12.0 + column as f32 * cell_w, grid_top, cell_w, 24.0), &ramp("caption", theme.text_secondary));
    }
    let cell_h = ((panel.bottom() - 12.0 - grid_top - 28.0) / 6.0).min(40.0);
    for (index, day) in month.days.iter().enumerate() {
        let cell = Rect::new(panel.x + 12.0 + (index % 7) as f32 * cell_w, grid_top + 28.0 + (index / 7) as f32 * cell_h, cell_w, cell_h);
        let (cx, cy) = cell.center();
        let in_month = day.month() == month.month;
        let mut style = ramp("body", if in_month { theme.text } else { theme.text_muted });
        if *day == today {
            f.canvas.fill_circle(cx, cy, (cell_h / 2.0 - 2.0).min(17.0), theme.accent);
            style = ramp("body-strong", theme.on_accent);
        }
        f.text_centered(&day.day().to_string(), cell, &style);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    pub fn note(id: u32, app: &str, urgency: Urgency) -> Notification {
        Notification {
            id,
            app_name: app.into(),
            app_icon: String::new(),
            desktop_entry: None,
            summary: format!("Summary {id}"),
            body: "Body text".into(),
            actions: vec![("default".into(), "Open".into()), ("reply".into(), "Reply".into())],
            urgency,
            timeout_ms: None,
            transient: false,
            progress: None,
            time: 1_000 + u64::from(id),
            replaces_existing: false,
        }
    }

    #[test]
    fn toasts_are_limited_and_the_rest_wait() {
        let now = Instant::now();
        let mut notes = Notes::default();
        for id in 1..=5 {
            notes.receive(note(id, "App", Urgency::Normal), now, false, true);
        }
        assert_eq!(notes.toasts.len(), MAX_TOASTS);
        assert_eq!(notes.history.len(), 5);
        assert_eq!(notes.unread, 5);
        // Six seconds on, the first three leave and the waiting two show.
        let later = now + DEFAULT_TIMEOUT + Duration::from_millis(1);
        assert!(notes.tick(later, true));
        assert!(notes.tick(later, true) || notes.toasts.len() == 2);
        assert_eq!(notes.toasts.iter().map(|t| t.note.id).collect::<Vec<_>>(), [4, 5]);
        assert_eq!(notes.history.len(), 5, "expired toasts stay in the centre");
    }

    #[test]
    fn critical_notifications_stay_and_pass_do_not_disturb() {
        let now = Instant::now();
        let mut notes = Notes::default();
        notes.dnd = true;
        notes.receive(note(1, "Mail", Urgency::Normal), now, false, true);
        notes.receive(note(2, "Battery", Urgency::Critical), now, false, true);
        assert_eq!(notes.toasts.iter().map(|t| t.note.id).collect::<Vec<_>>(), [2], "only the critical one pops up");
        assert_eq!(notes.history.len(), 2, "both are in the centre");
        assert!(notes.toasts[0].deadline.is_none());
        assert!(!notes.tick(now + Duration::from_secs(3600), true), "a critical toast does not expire");
        notes.dismiss(2, now, true);
        notes.tick(now, true);
        assert!(notes.toasts.is_empty());
        assert_eq!(notes.history.iter().map(|n| n.id).collect::<Vec<_>>(), [1]);
    }

    #[test]
    fn nothing_pops_up_while_locked_and_replacements_update_in_place() {
        let now = Instant::now();
        let mut notes = Notes::default();
        notes.receive(note(1, "Mail", Urgency::Critical), now, true, true);
        assert!(notes.toasts.is_empty() && notes.history.len() == 1);
        notes.receive(note(2, "Files", Urgency::Normal), now, false, true);
        let mut update = note(2, "Files", Urgency::Normal);
        update.summary = "Copied 10 of 24".into();
        update.progress = Some(40);
        notes.receive(update, now + Duration::from_secs(5), false, true);
        assert_eq!(notes.toasts.len(), 1);
        assert_eq!(notes.toasts[0].note.progress, Some(40));
        assert_eq!(notes.history.len(), 2);
        assert_eq!(notes.unread, 2, "a replacement is not a new notification");
        assert!(!notes.tick(now + Duration::from_secs(7), true), "the update restarted the toast's time");
        let mut transient = note(3, "Volume", Urgency::Normal);
        transient.transient = true;
        notes.receive(transient, now, false, true);
        assert_eq!(notes.history.len(), 2, "transient notifications are not kept");
        assert_eq!(notes.toasts.len(), 2);
    }

    #[test]
    fn history_is_capped_and_withdrawn_notifications_disappear() {
        let now = Instant::now();
        let mut notes = Notes::default();
        for id in 0..(MAX_HISTORY as u32 + 30) {
            notes.receive(note(id, "App", Urgency::Low), now, true, true);
        }
        assert_eq!(notes.history.len(), MAX_HISTORY);
        assert_eq!(notes.history[0].id, 30, "the oldest were dropped");
        notes.closed(31, false, now, true);
        assert!(notes.history.iter().any(|n| n.id == 31), "an expired notification stays");
        notes.closed(31, true, now, true);
        assert!(!notes.history.iter().any(|n| n.id == 31));
        notes.seen();
        assert_eq!(notes.unread, 0);
    }

    #[test]
    fn hovering_pauses_the_timeouts() {
        let now = Instant::now();
        let mut notes = Notes::default();
        notes.receive(note(1, "App", Urgency::Normal), now, false, true);
        notes.hover(true, now + Duration::from_secs(4));
        assert!(!notes.tick(now + Duration::from_secs(60), true), "paused");
        assert_eq!(notes.next_deadline(now + Duration::from_secs(60)), None);
        notes.hover(false, now + Duration::from_secs(60));
        assert!(!notes.tick(now + Duration::from_secs(61), true));
        assert!(notes.tick(now + Duration::from_secs(63), true), "the two seconds that were left ran out");
    }

    #[test]
    fn groups_are_by_application_newest_first() {
        let now = Instant::now();
        let mut notes = Notes::default();
        for (id, app) in [(1, "Mail"), (2, "Files"), (3, "Mail"), (4, "Chat")] {
            notes.receive(note(id, app, Urgency::Normal), now, true, true);
        }
        let groups = notes.groups();
        assert_eq!(groups.iter().map(|g| g.app).collect::<Vec<_>>(), ["Chat", "Mail", "Files"]);
        assert_eq!(groups[1].notes.iter().map(|n| n.id).collect::<Vec<_>>(), [3, 1]);
        assert_eq!(notes.ids_of("Mail"), [1, 3]);
    }

    #[test]
    fn times_and_timeouts_are_bounded() {
        assert_eq!(relative_time(1000, 990), "Now");
        assert_eq!(relative_time(1000, 2000), "Now", "a clock that ran backwards");
        assert_eq!(relative_time(10_000, 10_000 - 600), "10 min");
        assert_eq!(relative_time(100_000, 100_000 - 7300), "2 h");
        assert_eq!(relative_time(1_000_000, 1_000_000 - 90_000), "Yesterday");
        let mut n = note(1, "A", Urgency::Normal);
        assert_eq!(timeout(&n), Some(DEFAULT_TIMEOUT));
        n.timeout_ms = Some(1);
        assert_eq!(timeout(&n), Some(MIN_TIMEOUT));
        n.timeout_ms = Some(u32::MAX);
        assert_eq!(timeout(&n), Some(MAX_TIMEOUT));
        n.urgency = Urgency::Critical;
        assert_eq!(timeout(&n), None);
    }

    #[test]
    fn toast_heights_follow_their_content() {
        let mut text = TextEngine::new();
        let mut n = note(1, "A", Urgency::Normal);
        let with_actions = toast_height(&mut text, &n, 364.0);
        n.actions.clear();
        let plain = toast_height(&mut text, &n, 364.0);
        assert!(with_actions > plain);
        n.progress = Some(10);
        assert!(toast_height(&mut text, &n, 364.0) > plain);
        n.body = String::new();
        assert!(toast_height(&mut text, &n, 364.0) <= plain + 14.0);
        let empty = Notes::default();
        let (w, h) = toasts_size(&mut text, &empty);
        assert_eq!(w, 364.0 + 24.0);
        assert!(h <= 24.0);
    }
}
