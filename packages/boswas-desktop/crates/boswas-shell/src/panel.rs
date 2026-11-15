//! The panel: launcher button, pinned and running applications, status
//! area and clock, along the bottom edge of each output.

use boswas_ui::text::TextStyle;

use crate::{
    apps::App,
    control::WindowInfo,
    status::Status,
    ui::{Frame, Rect},
};

/// Panel height in logical pixels.
pub const HEIGHT: u32 = 48;
const BUTTON: f32 = 40.0;
const GAP: f32 = 4.0;
const ICON: f32 = 28.0;

/// What a click on the panel does.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PanelAction {
    /// Open or close the launcher.
    Launcher,
    /// Activate this window (or minimize it when it is the active one).
    Window(u64),
    /// Start the application with this index.
    Launch(usize),
}

/// Everything the panel shows.
pub struct PanelModel<'a> {
    pub apps: &'a [App],
    /// Indices into `apps` of the pinned applications.
    pub pinned: &'a [usize],
    pub windows: &'a [WindowInfo],
    pub workspace: usize,
    pub status: &'a Status,
    pub launcher_open: bool,
}

/// One entry of the task area.
#[derive(Debug, PartialEq)]
pub struct Task<'a> {
    /// The application, when the window (or pin) maps to an installed one.
    pub app: Option<usize>,
    /// The window a click activates; `None` for a pinned app that is not
    /// running.
    pub window: Option<&'a WindowInfo>,
}

/// The task area's entries: pinned applications first (with their window
/// when one is open on this workspace), then every other window.
pub fn tasks<'a>(model: &PanelModel<'a>) -> Vec<Task<'a>> {
    let here: Vec<&WindowInfo> = model.windows.iter().filter(|w| w.workspace == model.workspace).collect();
    let mut used = vec![false; here.len()];
    let mut out = Vec::new();
    for &app in model.pinned {
        let Some(entry) = model.apps.get(app) else {
            continue;
        };
        // The active window of the app if there is one, else its first.
        let owned: Vec<usize> = (0..here.len()).filter(|&i| entry.owns_window(&here[i].app_id)).collect();
        let pick = owned.iter().copied().find(|&i| here[i].active).or(owned.first().copied());
        for i in owned {
            used[i] = true;
        }
        out.push(Task {
            app: Some(app),
            window: pick.map(|i| here[i]),
        });
    }
    for (i, window) in here.iter().enumerate() {
        if !used[i] {
            out.push(Task {
                app: model.apps.iter().position(|a| a.owns_window(&window.app_id)),
                window: Some(window),
            });
        }
    }
    out
}

pub fn draw(frame: &mut Frame<PanelAction>, model: &PanelModel) {
    let theme = frame.theme();
    let (w, h) = (frame.canvas.width(), frame.canvas.height());
    frame.canvas.clear(theme.surface.alpha(0.94));
    frame.canvas.fill_rect(0.0, 0.0, w, 1.0, theme.border.alpha(0.8));
    let top = (h - BUTTON) / 2.0;
    let radius = boswas_ui::tokens().radius_card;

    // Launcher button.
    let start = Rect::new(8.0, top, BUTTON, BUTTON);
    let hovered = frame.hit(start, PanelAction::Launcher);
    if model.launcher_open {
        frame.highlight(start, radius, theme.accent.alpha(0.30));
    } else if hovered {
        frame.highlight(start, radius, theme.surface_hover);
    }
    frame.icon_or_placeholder(&["boswas-logo", "start-here"], start.x + 5.0, start.y + 5.0, 30.0);

    // Status area and clock, right aligned.
    let time_style = TextStyle::new(13.0, theme.text).weight(600);
    let date_style = TextStyle::new(11.0, theme.text_muted);
    let (time_w, time_h) = frame.res.text.measure(&model.status.time, &time_style);
    let (date_w, date_h) = frame.res.text.measure(&model.status.date, &date_style);
    let clock_w = time_w.max(date_w);
    let clock_x = w - 14.0 - clock_w;
    let block_h = time_h + date_h - 2.0;
    let clock_y = (h - block_h) / 2.0;
    frame.text(&model.status.time, clock_x + clock_w - time_w, clock_y, None, &time_style);
    frame.text(&model.status.date, clock_x + clock_w - date_w, clock_y + time_h - 2.0, None, &date_style);

    let mut status_x = clock_x - 14.0;
    let mut status_icon = |frame: &mut Frame<PanelAction>, name: &str| {
        status_x -= 18.0;
        frame.icon(name, status_x, (h - 18.0) / 2.0, 18.0);
        status_x -= 12.0;
    };
    if let Some(battery) = &model.status.battery {
        status_icon(frame, battery.icon());
    }
    status_icon(frame, model.status.network.icon());
    let tasks_end = status_x - 8.0;

    // Task area.
    let mut x = start.x + BUTTON + 12.0;
    for task in tasks(model) {
        if x + BUTTON > tasks_end {
            break;
        }
        let rect = Rect::new(x, top, BUTTON, BUTTON);
        let action = match (task.window, task.app) {
            (Some(window), _) => PanelAction::Window(window.id),
            (None, Some(app)) => PanelAction::Launch(app),
            (None, None) => continue,
        };
        let hovered = frame.hit(rect, action);
        let active = task.window.is_some_and(|w| w.active);
        if active {
            frame.highlight(rect, radius, theme.surface_hover);
        } else if hovered {
            frame.highlight(rect, radius, theme.surface_raised);
        }
        let app = task.app.and_then(|i| model.apps.get(i));
        let window_app_id = task.window.map(|w| w.app_id.as_str()).unwrap_or_default();
        frame.icon_or_placeholder(
            &[app.map(|a| a.icon.as_str()).unwrap_or_default(), window_app_id, "application-x-executable"],
            x + (BUTTON - ICON) / 2.0,
            top + (BUTTON - ICON) / 2.0 - 1.0,
            ICON,
        );
        if let Some(window) = task.window {
            // Running indicator: a long accent bar for the active window, a
            // short muted one for the others.
            let (bar, color) = match (window.active, window.minimized) {
                (true, _) => (16.0, theme.accent_hover),
                (false, false) => (6.0, theme.text_muted),
                (false, true) => (6.0, theme.border),
            };
            frame.canvas.fill_rounded_rect(x + (BUTTON - bar) / 2.0, top + BUTTON - 3.0, bar, 3.0, 1.5, color);
        }
        x += BUTTON + GAP;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::status::Network;

    fn app(id: &str) -> App {
        App {
            id: id.into(),
            name: id.into(),
            comment: String::new(),
            icon: id.into(),
            exec: vec![id.into()],
            terminal: false,
            keywords: Vec::new(),
            wm_class: String::new(),
        }
    }

    fn window(id: u64, app_id: &str, active: bool, workspace: usize) -> WindowInfo {
        WindowInfo {
            id,
            title: app_id.into(),
            app_id: app_id.into(),
            active,
            minimized: false,
            workspace,
        }
    }

    fn status() -> Status {
        Status {
            time: "14:05".into(),
            date: "Sun 15 Nov".into(),
            battery: None,
            network: Network::Wired,
        }
    }

    #[test]
    fn pinned_apps_come_first_and_adopt_their_windows() {
        let apps = vec![app("files"), app("terminal"), app("browser")];
        let windows = vec![
            window(1, "editor", false, 0),
            window(2, "terminal", false, 0),
            window(3, "terminal", true, 0),
            window(4, "browser", false, 1),
        ];
        let status = status();
        let model = PanelModel {
            apps: &apps,
            pinned: &[0, 1],
            windows: &windows,
            workspace: 0,
            status: &status,
            launcher_open: false,
        };
        let list = tasks(&model);
        let summary: Vec<(Option<usize>, Option<u64>)> = list.iter().map(|t| (t.app, t.window.map(|w| w.id))).collect();
        assert_eq!(
            summary,
            [(Some(0), None), (Some(1), Some(3)), (None, Some(1))],
            "files is pinned and closed; terminal shows its active window; the editor follows; \
             the browser is on another workspace"
        );
    }

    #[test]
    fn unknown_pins_are_skipped() {
        let apps = vec![app("files")];
        let status = status();
        let model = PanelModel {
            apps: &apps,
            pinned: &[7, 0],
            windows: &[],
            workspace: 0,
            status: &status,
            launcher_open: false,
        };
        assert_eq!(tasks(&model).len(), 1);
    }
}
