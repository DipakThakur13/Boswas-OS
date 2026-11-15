//! The launcher: search, the application grid and the power buttons.

use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use boswas_ui::text::TextStyle;

use crate::{
    apps::{self, App},
    ui::{Frame, Rect, TextField},
};

pub const WIDTH: f32 = 608.0;
pub const HEIGHT: f32 = 620.0;
const MARGIN: f32 = 8.0;
const PADDING: f32 = 20.0;
const COLUMNS: usize = 6;
const ROWS: usize = 4;
const CELL_W: f32 = (WIDTH - 2.0 * PADDING) / COLUMNS as f32;
const CELL_H: f32 = 100.0;
/// How long a power button stays armed after the first click.
const CONFIRM: Duration = Duration::from_secs(4);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Power {
    Lock,
    LogOut,
    Restart,
    ShutDown,
}

impl Power {
    fn icon(self) -> &'static str {
        match self {
            Power::Lock => "system-lock-screen-symbolic",
            Power::LogOut => "system-log-out-symbolic",
            Power::Restart => "system-reboot-symbolic",
            Power::ShutDown => "system-shutdown-symbolic",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Power::Lock => "Lock",
            Power::LogOut => "Log out",
            Power::Restart => "Restart",
            Power::ShutDown => "Shut down",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LauncherAction {
    /// Start the application with this index and close the launcher.
    Launch(usize),
    Power(Power),
    /// A click outside the flyout.
    Dismiss,
    /// A click on the flyout's own background.
    Nothing,
}

/// What a key press did to the launcher.
#[derive(Debug, PartialEq, Eq)]
pub enum KeyOutcome {
    Redraw,
    Launch(usize),
    Dismiss,
    Ignored,
}

/// Keys the launcher understands.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Key {
    Escape,
    Enter,
    Backspace,
    Left,
    Right,
    Up,
    Down,
    ClearLine,
}

#[derive(Default)]
pub struct Launcher {
    pub open: bool,
    pub query: TextField,
    /// Indices into the application list, best match first.
    pub results: Vec<usize>,
    pub selected: usize,
    /// First visible row of the grid.
    pub scroll: usize,
    /// A power action waiting for its confirming second click.
    armed: Option<(Power, Instant)>,
}

impl Launcher {
    pub fn show(&mut self, apps: &[App], usage: &HashMap<String, u32>) {
        self.open = true;
        self.query.clear();
        self.armed = None;
        self.refresh(apps, usage);
    }

    pub fn hide(&mut self) {
        self.open = false;
        self.query.clear();
        self.armed = None;
    }

    /// Recomputes the results for the current query.
    pub fn refresh(&mut self, apps: &[App], usage: &HashMap<String, u32>) {
        self.results = apps::search(apps, self.query.text(), usage);
        self.selected = 0;
        self.scroll = 0;
    }

    fn rows(&self) -> usize {
        self.results.len().div_ceil(COLUMNS)
    }

    fn keep_selection_visible(&mut self) {
        let row = self.selected / COLUMNS;
        if row < self.scroll {
            self.scroll = row;
        } else if row >= self.scroll + ROWS {
            self.scroll = row + 1 - ROWS;
        }
    }

    /// Scrolls the grid by whole rows.
    pub fn scroll_by(&mut self, rows: i32) -> bool {
        let max = self.rows().saturating_sub(ROWS);
        let target = (self.scroll as i64 + i64::from(rows)).clamp(0, max as i64) as usize;
        std::mem::replace(&mut self.scroll, target) != target
    }

    pub fn typed(&mut self, text: &str, apps: &[App], usage: &HashMap<String, u32>) -> KeyOutcome {
        if self.query.insert(text) {
            self.refresh(apps, usage);
            KeyOutcome::Redraw
        } else {
            KeyOutcome::Ignored
        }
    }

    pub fn key(&mut self, key: Key, apps: &[App], usage: &HashMap<String, u32>) -> KeyOutcome {
        let count = self.results.len();
        match key {
            Key::Escape if self.query.is_empty() => KeyOutcome::Dismiss,
            Key::Escape | Key::ClearLine => {
                self.query.clear();
                self.refresh(apps, usage);
                KeyOutcome::Redraw
            }
            Key::Backspace => {
                if self.query.backspace() {
                    self.refresh(apps, usage);
                    KeyOutcome::Redraw
                } else {
                    KeyOutcome::Ignored
                }
            }
            Key::Enter => match self.results.get(self.selected) {
                Some(&app) => KeyOutcome::Launch(app),
                None => KeyOutcome::Ignored,
            },
            Key::Left | Key::Right | Key::Up | Key::Down if count == 0 => KeyOutcome::Ignored,
            Key::Left => self.select(self.selected.saturating_sub(1)),
            Key::Right => self.select((self.selected + 1).min(count - 1)),
            Key::Up => self.select(self.selected.checked_sub(COLUMNS).unwrap_or(self.selected)),
            Key::Down => self.select(if self.selected + COLUMNS < count { self.selected + COLUMNS } else { self.selected }),
        }
    }

    fn select(&mut self, index: usize) -> KeyOutcome {
        self.selected = index;
        self.keep_selection_visible();
        KeyOutcome::Redraw
    }

    /// A click on a power button: the first click arms it, a second click
    /// within a few seconds confirms. Locking needs no confirmation.
    pub fn power_clicked(&mut self, power: Power, now: Instant) -> bool {
        if power == Power::Lock {
            return true;
        }
        match self.armed {
            Some((armed, at)) if armed == power && now.duration_since(at) <= CONFIRM => {
                self.armed = None;
                true
            }
            _ => {
                self.armed = Some((power, now));
                false
            }
        }
    }

    /// Disarms a power button whose confirmation time ran out. Returns
    /// whether something changed.
    pub fn expire(&mut self, now: Instant) -> bool {
        match self.armed {
            Some((_, at)) if now.duration_since(at) > CONFIRM => {
                self.armed = None;
                true
            }
            _ => false,
        }
    }

    #[cfg(test)]
    pub fn is_armed(&self) -> bool {
        self.armed.is_some()
    }
}

pub fn draw(frame: &mut Frame<LauncherAction>, launcher: &Launcher, apps: &[App], user: &str) {
    let theme = frame.theme();
    let (w, h) = (frame.canvas.width(), frame.canvas.height());
    let tokens = boswas_ui::tokens();
    // The surface covers the output so a click anywhere outside the flyout
    // closes it; only the flyout itself is painted.
    frame.hit(Rect::new(0.0, 0.0, w, h), LauncherAction::Dismiss);
    let fw = WIDTH.min(w - 2.0 * MARGIN);
    let fh = HEIGHT.min(h - 2.0 * MARGIN);
    let flyout = Rect::new(MARGIN, h - fh - MARGIN, fw, fh);
    let elevation = tokens.elevation("flyout");
    frame.canvas.shadow(flyout.x, flyout.y, flyout.w, flyout.h, tokens.radius_flyout, 20.0, elevation.y.min(8.0), elevation.alpha);
    frame.canvas.fill_rounded_rect(flyout.x, flyout.y, flyout.w, flyout.h, tokens.radius_flyout, theme.surface);
    frame.canvas.stroke_rounded_rect(flyout.x, flyout.y, flyout.w, flyout.h, tokens.radius_flyout, 1.0, theme.border);
    frame.hit(flyout, LauncherAction::Nothing);

    // Search field.
    let field = Rect::new(flyout.x + PADDING, flyout.y + PADDING, flyout.w - 2.0 * PADDING, 40.0);
    frame.canvas.fill_rounded_rect(field.x, field.y, field.w, field.h, 20.0, theme.surface_raised);
    frame.canvas.stroke_rounded_rect(field.x, field.y, field.w, field.h, 20.0, 1.5, theme.focus);
    frame.icon("system-search-symbolic", field.x + 14.0, field.y + 11.0, 18.0);
    let text_x = field.x + 42.0;
    let input = TextStyle::new(14.0, theme.text);
    let line = input.line_height();
    if launcher.query.is_empty() {
        let placeholder = TextStyle::new(14.0, theme.text_muted);
        frame.text("Search applications", text_x, field.y + (field.h - line) / 2.0, Some(field.w - 60.0), &placeholder);
    } else {
        let drawn = frame.text(launcher.query.text(), text_x, field.y + (field.h - line) / 2.0, Some(field.w - 64.0), &input);
        frame.canvas.fill_rect(text_x + drawn + 2.0, field.y + 11.0, 1.5, 18.0, theme.focus);
    }

    // Section label.
    let label = TextStyle::new(12.0, theme.text_muted).weight(600);
    let section = if launcher.query.is_empty() { "All applications" } else { "Results" };
    let grid_top = field.y + field.h + 44.0;
    frame.text(section, flyout.x + PADDING + 4.0, field.y + field.h + 16.0, None, &label);

    // Application grid.
    let name_style = TextStyle::new(12.0, theme.text_secondary);
    let first = launcher.scroll * COLUMNS;
    if launcher.results.is_empty() {
        let none = TextStyle::new(14.0, theme.text_muted);
        frame.text_centered("No applications found", Rect::new(flyout.x, grid_top + 40.0, flyout.w, 24.0), &none);
    }
    for (slot, &app_index) in launcher.results.iter().enumerate().skip(first).take(COLUMNS * ROWS) {
        let Some(app) = apps.get(app_index) else {
            continue;
        };
        let (column, row) = ((slot - first) % COLUMNS, (slot - first) / COLUMNS);
        let cell = Rect::new(
            flyout.x + PADDING + column as f32 * CELL_W,
            grid_top + row as f32 * CELL_H,
            CELL_W,
            CELL_H - 4.0,
        );
        let hovered = frame.hit(cell, LauncherAction::Launch(app_index));
        if slot == launcher.selected && !launcher.query.is_empty() {
            frame.highlight(cell.inset(2.0), tokens.radius_card, theme.accent.alpha(0.28));
        } else if hovered {
            frame.highlight(cell.inset(2.0), tokens.radius_card, theme.surface_hover);
        }
        frame.icon_or_placeholder(&[app.icon.as_str(), "application-x-executable"], cell.x + (cell.w - 48.0) / 2.0, cell.y + 10.0, 48.0);
        frame.text_centered(&app.name, Rect::new(cell.x + 4.0, cell.y + 64.0, cell.w - 8.0, 22.0), &name_style);
    }

    // Scroll position, when there is more than one page.
    let rows = launcher.results.len().div_ceil(COLUMNS);
    if rows > ROWS {
        let track = Rect::new(flyout.x + flyout.w - 10.0, grid_top, 4.0, ROWS as f32 * CELL_H - 8.0);
        let thumb_h = (track.h * ROWS as f32 / rows as f32).max(24.0);
        let thumb_y = track.y + (track.h - thumb_h) * launcher.scroll as f32 / (rows - ROWS) as f32;
        frame.canvas.fill_rounded_rect(track.x, thumb_y, track.w, thumb_h, 2.0, theme.border);
    }

    // Footer: the user and the power buttons.
    let footer_y = flyout.y + flyout.h - 60.0;
    frame.canvas.fill_rect(flyout.x + 1.0, footer_y, flyout.w - 2.0, 1.0, theme.border.alpha(0.7));
    frame.canvas.fill_circle(flyout.x + PADDING + 16.0, footer_y + 30.0, 16.0, theme.surface_hover);
    frame.icon("avatar-default-symbolic", flyout.x + PADDING + 7.0, footer_y + 21.0, 18.0);
    let user_style = TextStyle::new(14.0, theme.text).weight(500);
    frame.text(user, flyout.x + PADDING + 42.0, footer_y + 30.0 - user_style.line_height() / 2.0, Some(200.0), &user_style);

    let mut x = flyout.x + flyout.w - PADDING - 36.0;
    for power in [Power::ShutDown, Power::Restart, Power::LogOut, Power::Lock] {
        let button = Rect::new(x, footer_y + 12.0, 36.0, 36.0);
        let hovered = frame.hit(button, LauncherAction::Power(power));
        let armed = launcher.armed.is_some_and(|(p, _)| p == power);
        if armed {
            frame.highlight(button, tokens.radius_card, theme.danger.alpha(0.85));
        } else if hovered {
            frame.highlight(button, tokens.radius_card, theme.surface_hover);
        }
        frame.icon(power.icon(), button.x + 9.0, button.y + 9.0, 18.0);
        if armed || hovered {
            let text = if armed { format!("Click again to {}", power.label().to_lowercase()) } else { power.label().to_owned() };
            let hint = TextStyle::new(12.0, if armed { theme.danger } else { theme.text_muted });
            let (hint_w, hint_h) = frame.res.text.measure(&text, &hint);
            let hint_x = flyout.x + flyout.w - PADDING - 4.0 * 44.0 - hint_w - 8.0;
            frame.text(&text, hint_x, footer_y + 30.0 - hint_h / 2.0, None, &hint);
        }
        x -= 44.0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn apps(count: usize) -> Vec<App> {
        (0..count)
            .map(|i| App {
                id: format!("app{i:02}"),
                name: format!("App {i:02}"),
                comment: String::new(),
                icon: String::new(),
                exec: vec!["true".into()],
                terminal: false,
                keywords: Vec::new(),
                wm_class: String::new(),
            })
            .collect()
    }

    #[test]
    fn typing_filters_and_enter_launches_the_first_result() {
        let apps = apps(30);
        let usage = HashMap::new();
        let mut launcher = Launcher::default();
        launcher.show(&apps, &usage);
        assert_eq!(launcher.results.len(), 30);
        assert_eq!(launcher.typed("app 1", &apps, &usage), KeyOutcome::Redraw);
        assert_eq!(launcher.results.len(), 10);
        assert_eq!(launcher.key(Key::Enter, &apps, &usage), KeyOutcome::Launch(10));
        assert_eq!(launcher.key(Key::Right, &apps, &usage), KeyOutcome::Redraw);
        assert_eq!(launcher.key(Key::Enter, &apps, &usage), KeyOutcome::Launch(11));
        assert_eq!(launcher.typed("zzz", &apps, &usage), KeyOutcome::Redraw);
        assert_eq!(launcher.key(Key::Enter, &apps, &usage), KeyOutcome::Ignored);
        assert_eq!(launcher.key(Key::Down, &apps, &usage), KeyOutcome::Ignored);
    }

    #[test]
    fn escape_clears_the_query_before_it_closes() {
        let apps = apps(3);
        let usage = HashMap::new();
        let mut launcher = Launcher::default();
        launcher.show(&apps, &usage);
        launcher.typed("a", &apps, &usage);
        assert_eq!(launcher.key(Key::Escape, &apps, &usage), KeyOutcome::Redraw);
        assert!(launcher.query.is_empty());
        assert_eq!(launcher.key(Key::Escape, &apps, &usage), KeyOutcome::Dismiss);
        assert_eq!(launcher.key(Key::Backspace, &apps, &usage), KeyOutcome::Ignored);
    }

    #[test]
    fn arrow_keys_stay_inside_the_grid_and_scroll_it() {
        let apps = apps(40);
        let usage = HashMap::new();
        let mut launcher = Launcher::default();
        launcher.show(&apps, &usage);
        launcher.key(Key::Left, &apps, &usage);
        launcher.key(Key::Up, &apps, &usage);
        assert_eq!(launcher.selected, 0);
        for _ in 0..5 {
            launcher.key(Key::Down, &apps, &usage);
        }
        assert_eq!(launcher.selected, 30);
        assert_eq!(launcher.scroll, 2, "row 5 is visible as the last of four rows");
        launcher.key(Key::Down, &apps, &usage);
        assert_eq!(launcher.selected, 36, "the last row is shorter but reachable");
        launcher.key(Key::Down, &apps, &usage);
        assert_eq!(launcher.selected, 36);
        for _ in 0..10 {
            launcher.key(Key::Right, &apps, &usage);
        }
        assert_eq!(launcher.selected, 39);
        assert!(launcher.scroll_by(-100));
        assert_eq!(launcher.scroll, 0);
        assert!(!launcher.scroll_by(-1));
        assert!(launcher.scroll_by(100));
        assert_eq!(launcher.scroll, 3);
    }

    #[test]
    fn power_actions_need_a_second_click_in_time() {
        let mut launcher = Launcher::default();
        let t0 = Instant::now();
        assert!(launcher.power_clicked(Power::Lock, t0), "locking is always safe");
        assert!(!launcher.power_clicked(Power::ShutDown, t0));
        assert!(launcher.is_armed());
        assert!(!launcher.power_clicked(Power::Restart, t0 + Duration::from_secs(1)), "a different button re-arms");
        assert!(!launcher.power_clicked(Power::ShutDown, t0 + Duration::from_secs(2)));
        assert!(launcher.power_clicked(Power::ShutDown, t0 + Duration::from_secs(3)));
        assert!(!launcher.is_armed());
        assert!(!launcher.power_clicked(Power::LogOut, t0));
        assert!(!launcher.power_clicked(Power::LogOut, t0 + Duration::from_secs(10)), "too late: armed again instead");
        assert!(!launcher.expire(t0 + Duration::from_secs(11)));
        assert!(launcher.expire(t0 + Duration::from_secs(20)));
        assert!(!launcher.is_armed());
    }
}
