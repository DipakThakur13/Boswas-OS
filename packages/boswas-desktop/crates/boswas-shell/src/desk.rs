//! What the desktop does: flyouts, menus, dialogs, toasts and on-screen
//! displays coming and going, and the events of the compositor and of the
//! system services.

use std::{
    path::PathBuf,
    time::{Duration, Instant},
};

use boswas_services::{Command, Event, Notification, PowerAction, Secret, SessionEvent, Urgency};
use boswas_ui::{tokens::tokens, Scheme, Theme};
use serde_json::{json, Value};
use smithay_client_toolkit::shell::{
    wlr_layer::{Anchor, KeyboardInteractivity, Layer},
    WaylandSurface,
};
use wayland_client::protocol::wl_output::WlOutput;

use crate::{
    action::Action,
    apps,
    capture::{self, Capture},
    control::ControlEvent,
    dialogs::{self, AuthDialog, Dialog, Leave, RunDialog},
    menu::{self, Cmd, Item, Menu},
    notify, osd,
    quick::{self, Page},
    start::{self, ResultItem},
    state::{Flyout, Mode, Tooltip},
    taskbar::{self, Preview},
    ui::{ramp, Frame, Rect, Resources},
    Pending, Role, Shell, Spec, PAD,
};

/// How long a preview stays after the pointer left it and its button.
pub const PREVIEW_GRACE: Duration = Duration::from_millis(350);
/// Shortest time between two commands of a dragged slider.
pub const SLIDER_INTERVAL: Duration = Duration::from_millis(40);
/// How long an auto-hiding taskbar stays after the pointer left it.
pub const TASKBAR_HIDE_DELAY: Duration = Duration::from_millis(400);

/// How far an auto-hiding taskbar moves down: all but a two-pixel sliver
/// stays below the screen's edge, where the pointer brings it back.
pub fn taskbar_hidden_offset() -> i32 {
    tokens().shell("taskbar-height") as i32 - 2
}

/// Largest screenshot or thumbnail file read back from the compositor.
const MAX_PICTURE_BYTES: u64 = 256 * 1024 * 1024;

/// Where a layer surface goes: size, margins (top, right, bottom, left)
/// and the position of its top-left corner on the output.
type Placement = ((u32, u32), (i32, i32, i32, i32), (f32, f32));

/// Size of a tooltip's box for its text.
fn tooltip_size(res: &mut Resources, text: &str) -> (f32, f32) {
    let style = ramp("caption", res.theme.text);
    let lines: Vec<&str> = text.lines().take(4).collect();
    let width = lines.iter().map(|line| res.text.measure(line, &style).0).fold(0.0, f32::max).min(320.0);
    (width + 18.0, lines.len().max(1) as f32 * style.line_height() + 12.0)
}

pub fn draw_tooltip(f: &mut Frame, text: &str, panel: Rect) {
    let theme = f.theme();
    let t = tokens();
    let e = t.elevation("tooltip");
    f.canvas.shadow(panel.x, panel.y, panel.w, panel.h, t.radius_tooltip, e.blur, e.y, e.alpha);
    f.canvas.fill_rounded_rect(panel.x, panel.y, panel.w, panel.h, t.radius_tooltip, theme.surface_raised);
    let stroke = f.res.stroke("surface");
    f.canvas.stroke_rounded_rect(panel.x, panel.y, panel.w, panel.h, t.radius_tooltip, 1.0, stroke);
    let style = ramp("caption", theme.text);
    for (index, line) in text.lines().take(4).enumerate() {
        f.text(line, panel.x + 9.0, panel.y + 6.0 + index as f32 * style.line_height(), Some(panel.w - 18.0), &style);
    }
}

fn runtime_dir() -> Option<PathBuf> {
    std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from).filter(|p| p.is_absolute())
}

fn read_picture(path: &std::path::Path) -> Option<tiny_skia::Pixmap> {
    let meta = std::fs::metadata(path).ok()?;
    if !meta.is_file() || meta.len() > MAX_PICTURE_BYTES {
        return None;
    }
    boswas_ui::icons::decode_image(&std::fs::read(path).ok()?)
}

impl Shell {
    // ---------------------------------------------------------------------
    // Talking to the compositor and the services
    // ---------------------------------------------------------------------

    pub fn send_control(&mut self, cmd: &str) -> Option<u64> {
        self.control_with(cmd, json!({}))
    }

    pub fn control_with(&mut self, cmd: &str, arguments: Value) -> Option<u64> {
        self.control.as_mut().map(|control| control.send(cmd, arguments))
    }

    fn window_command(&mut self, cmd: &str, id: u64) {
        self.control_with(cmd, json!({ "window": id }));
    }

    /// Sends a command to the system services (or, in a debug build that
    /// simulates them, to the stand-in).
    pub fn service(&mut self, command: Command) {
        #[cfg(debug_assertions)]
        if let Some(fake) = self.fake.as_mut() {
            for event in fake.apply(&command) {
                self.service_event(event);
            }
            return;
        }
        if let Some(services) = &self.services {
            services.send(command);
        }
    }

    pub fn spawn(&mut self, argv: &[String]) -> bool {
        match apps::spawn(argv) {
            Ok(()) => true,
            Err(err) => {
                tracing::warn!("could not start {}: {err}", argv.first().map_or("", String::as_str));
                false
            }
        }
    }

    /// One of the folders next to Start's power button: Settings and Files
    /// start their programs, the others open the user's folder in the file
    /// manager.
    pub fn open_folder(&mut self, folder: crate::settings::Folder) {
        use crate::settings::Folder;
        self.close_transients();
        let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"));
        match folder {
            Folder::Settings => {
                self.spawn(&["boswas-control-center".to_owned()]);
            }
            Folder::Files => match self.st.app_index("com.boswas.Files") {
                Some(index) => self.launch_app(index),
                None => {
                    self.open_path(&home);
                }
            },
            _ => {
                let user_dirs = std::env::var_os("XDG_CONFIG_HOME")
                    .map(PathBuf::from)
                    .filter(|p| p.is_absolute())
                    .unwrap_or_else(|| home.join(".config"))
                    .join("user-dirs.dirs");
                let text = std::fs::read_to_string(user_dirs).ok();
                if let Some(path) = capture::user_dir(&home, text.as_deref(), folder) {
                    let _ = std::fs::create_dir_all(&path);
                    self.open_path(&path);
                }
            }
        }
    }

    /// Opens a file or a folder with its default application (the shell
    /// resolves it itself; `xdg-open` only when no application is known).
    pub fn open_path(&mut self, path: &std::path::Path) -> bool {
        match crate::open::open(path) {
            Ok(plan) => {
                tracing::info!("opening {} with {plan:?}", path.display());
                true
            }
            Err(err) => {
                tracing::warn!("could not open {}: {err}", path.display());
                let name = path.file_name().map_or_else(|| path.display().to_string(), |n| n.to_string_lossy().into_owned());
                self.notice("The file could not be opened", &format!("{name}: {err}"), None);
                false
            }
        }
    }

    pub fn launch_app(&mut self, index: usize) {
        let Some(app) = self.st.apps.get(index).cloned() else {
            return;
        };
        match apps::launch(&app) {
            Ok(()) => self.st.usage.record(&app.id),
            Err(err) => {
                tracing::warn!("could not start {}: {err}", app.name);
                self.notice("Could not start the application", &format!("{}: {err}", app.name), None);
            }
        }
        self.close_transients();
    }

    /// Shows a notification of the shell's own. With a file, a click on
    /// it opens the file (its default action) and its button opens the
    /// folder the file is in.
    pub fn notice(&mut self, summary: &str, body: &str, open: Option<PathBuf>) {
        let id = self.next_note_id;
        self.next_note_id = self.next_note_id.wrapping_sub(1).max(u32::MAX / 2);
        let mut actions = Vec::new();
        if let Some(path) = open {
            actions.push(("default".to_owned(), "Open".to_owned()));
            actions.push(("folder".to_owned(), "Open folder".to_owned()));
            self.shell_notes.insert(id, path);
            if self.shell_notes.len() > 32 {
                let oldest = self.shell_notes.keys().copied().max();
                if let Some(oldest) = oldest {
                    self.shell_notes.remove(&oldest);
                }
            }
        }
        let note = Notification {
            id,
            app_name: "Boswas OS".into(),
            app_icon: "boswas-logo".into(),
            desktop_entry: None,
            summary: summary.to_owned(),
            body: body.to_owned(),
            actions,
            urgency: Urgency::Normal,
            timeout_ms: None,
            transient: false,
            progress: None,
            time: std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs()),
            replaces_existing: false,
        };
        self.receive_note(note);
    }

    fn receive_note(&mut self, note: Notification) {
        // Settings lists the applications that sent notifications, to turn
        // their banners off; one whose banners are off still reaches the
        // notification centre.
        if self.st.mode == Mode::Desktop {
            self.st.senders.record(&note, std::time::SystemTime::now());
        }
        let muted = !self.st.desktop.notifications.banners_for(&note.app_name);
        let quiet = muted || self.st.locked || self.st.mode != Mode::Desktop || self.st.capture.is_some();
        self.st.notes.toast_time = Duration::from_secs(u64::from(self.st.desktop.notifications.toast_seconds));
        self.st.notes.receive(note, Instant::now(), quiet, self.res.reduced_motion);
        if self.st.flyout == Some(Flyout::Notifications) && !self.flyout_closing {
            self.st.notes.unread = 0;
        }
        self.sync_toasts();
        self.mark(Role::Taskbar);
        if self.st.flyout == Some(Flyout::Notifications) {
            self.mark(Role::Flyout);
        }
    }

    // ---------------------------------------------------------------------
    // Events
    // ---------------------------------------------------------------------

    pub fn control_event(&mut self, event: ControlEvent) {
        match event {
            ControlEvent::Windows(windows) => {
                if self.st.windows == windows {
                    return;
                }
                self.st.windows = windows;
                let live: Vec<u64> = self.st.windows.iter().map(|w| w.id).collect();
                self.st.thumbnails.retain(|id, _| live.contains(id));
                if let Some(preview) = self.st.preview.as_mut() {
                    preview.windows.retain(|id| live.contains(id));
                    if preview.windows.is_empty() {
                        self.close_preview();
                    } else {
                        self.sync_preview();
                    }
                }
                self.mark(Role::Taskbar);
            }
            ControlEvent::Workspace { active, .. } => {
                self.st.workspace = active;
                self.close_preview();
                self.mark(Role::Taskbar);
            }
            ControlEvent::Launcher => self.toggle_flyout(Flyout::Start, None),
            ControlEvent::QuickSettings => self.toggle_flyout(Flyout::Quick, None),
            ControlEvent::Notifications => self.toggle_flyout(Flyout::Notifications, None),
            ControlEvent::Widgets => self.toggle_flyout(Flyout::Widgets, None),
            ControlEvent::Lock(locked) => {
                self.st.locked = locked;
                if locked {
                    // Nothing of the desktop may stay over the lock screen,
                    // and no dialog may wait behind it.
                    if let Some(Dialog::Auth(auth)) = &self.st.dialog {
                        let cookie = auth.request.cookie.clone();
                        self.service(Command::AuthCancel { cookie });
                    }
                    self.drop_overlays();
                    self.st.notes.hide_toasts();
                    self.sync_toasts();
                }
            }
            ControlEvent::Config(desktop) => self.apply_desktop(*desktop),
            ControlEvent::Run => {
                if self.st.mode == Mode::Desktop && !self.st.locked {
                    let last = self.st.run_history.entries.last().cloned();
                    self.open_dialog(Dialog::Run(RunDialog::new(last.as_deref())));
                }
            }
            ControlEvent::Capture { whole_screen } => self.start_capture(whole_screen),
            ControlEvent::MediaKey(key) => self.media_key(&key),
            ControlEvent::TaskbarApp { index, new_instance } => {
                let list = self.st.tasks();
                if let Some(task) = list.get(index - 1) {
                    match (taskbar::click_target(task, &self.st.windows), task.app) {
                        (Some(id), _) if !new_instance => self.window_command(if task.windows.len() == 1 { "toggle-minimize" } else { "activate" }, id),
                        (_, Some(app)) => self.launch_app(app),
                        _ => {}
                    }
                }
            }
            ControlEvent::NightLight { enabled } => {
                self.st.night_light = enabled;
                self.mark(Role::Flyout);
            }
            ControlEvent::CaptureRequest { id, program } => {
                // Only the desktop can ask; anything else in the way means no.
                if self.st.mode != Mode::Desktop || self.st.locked || self.st.dialog.is_some() || self.st.capture.is_some() {
                    self.capture_answer(id, false);
                } else {
                    self.open_dialog(Dialog::Screen {
                        id,
                        program,
                        opened: Instant::now(),
                    });
                }
            }
            ControlEvent::CaptureRequestClosed { id } => {
                if matches!(&self.st.dialog, Some(Dialog::Screen { id: open, .. }) if *open == id) {
                    self.close_dialog();
                }
            }
            ControlEvent::Capturing(programs) => {
                self.st.capturing = programs;
                self.mark(Role::Taskbar);
            }
            ControlEvent::Layout { layouts, active } => {
                self.st.layouts = layouts;
                self.st.layout = active;
                self.mark(Role::Taskbar);
                self.mark(Role::Auth);
            }
            ControlEvent::Overview(active) => {
                if active {
                    self.close_transients();
                }
            }
            ControlEvent::Reply { id, ok, data } => self.reply(id, ok, &data),
        }
    }

    fn reply(&mut self, id: u64, ok: bool, _data: &Value) {
        match self.pending.remove(&id) {
            Some(Pending::Thumbnail { window, path }) => {
                if ok {
                    if let Some(picture) = read_picture(&path) {
                        self.st.thumbnails.insert(window, std::rc::Rc::new(picture));
                        self.mark(Role::Preview);
                    }
                } else {
                    self.thumbnails_unsupported = true;
                }
                let _ = std::fs::remove_file(&path);
            }
            Some(Pending::Screenshot { path, whole }) => {
                let picture = if ok { read_picture(&path) } else { None };
                let _ = std::fs::remove_file(&path);
                match picture {
                    Some(picture) => self.open_capture(picture, whole),
                    None => self.notice("Screen capture is not available", "The screen could not be read.", None),
                }
            }
            None => {}
        }
    }

    pub fn service_event(&mut self, event: Event) {
        let changed = self.st.sys.apply(&event);
        if changed.status {
            self.mark(Role::Taskbar);
            self.mark(Role::Auth);
            if self.st.flyout == Some(Flyout::Quick) {
                self.sync_flyout();
            }
        }
        if changed.tray {
            self.mark(Role::Taskbar);
            if self.st.flyout == Some(Flyout::TrayOverflow) {
                self.sync_flyout();
            }
        }
        if changed.media && self.st.flyout == Some(Flyout::Quick) {
            self.sync_flyout();
        }
        match event {
            Event::Notify(note) => {
                if self.st.mode == Mode::Desktop {
                    self.receive_note(note);
                }
            }
            Event::NotificationClosed { id, withdrawn } => {
                self.st.notes.closed(id, withdrawn, Instant::now(), self.res.reduced_motion);
                self.sync_toasts();
                self.mark(Role::Flyout);
                self.mark(Role::Taskbar);
            }
            Event::TrayMenu { key, entries } => {
                if let Some((wanted, x, y, output)) = self.tray_menu.take() {
                    if wanted == key && !entries.is_empty() {
                        self.open_menu(menu::from_tray(&key, &entries, 0), x, y, true, &output);
                    }
                }
            }
            Event::Session(SessionEvent::Lock | SessionEvent::PrepareForSleep(true)) => {
                self.send_control("lock");
            }
            Event::Session(_) => {}
            Event::AuthRequest(request) => {
                if self.st.mode == Mode::Desktop && !self.st.locked && self.st.dialog.is_none() {
                    self.open_dialog(Dialog::Auth(AuthDialog::new(request)));
                } else {
                    // Nobody can answer it now; do not leave it hanging.
                    self.service(Command::AuthCancel { cookie: request.cookie });
                }
            }
            Event::AuthProgress { cookie, progress } => {
                let finished = match self.st.dialog.as_mut() {
                    Some(Dialog::Auth(auth)) if auth.request.cookie == cookie => auth.progress(progress),
                    _ => return,
                };
                if finished {
                    self.close_dialog();
                } else {
                    self.mark(Role::Dialog);
                }
            }
            _ => {}
        }
    }

    /// Development builds only: acts on the lines appended to the feed
    /// file since the last look (see `svc::fake::feed_line`).
    #[cfg(debug_assertions)]
    fn read_feed(&mut self) {
        if self.fake.is_none() {
            return;
        }
        let Some(text) = std::env::var_os("BOSWAS_SHELL_DEBUG_FEED").and_then(|path| std::fs::read_to_string(path).ok()) else {
            return;
        };
        let lines: Vec<&str> = text.lines().collect();
        for line in lines.iter().skip(self.feed_lines) {
            if let Ok(message) = serde_json::from_str::<Value>(line) {
                for event in crate::control::parse_message(&message) {
                    self.control_event(event);
                }
            }
            for event in crate::svc::fake::feed_line(line, 5000 + self.feed_lines as u32) {
                self.service_event(event);
            }
        }
        self.feed_lines = lines.len();
    }

    pub fn tick(&mut self) {
        #[cfg(debug_assertions)]
        self.read_feed();
        // The lock screen looks at the weather summary once a minute.
        if self.st.weather.poll(chrono::Utc::now().timestamp()) {
            self.mark(Role::Auth);
        }
        self.widgets_tick();
        let clock = chrono::Local::now();
        self.follow_dnd_schedule(chrono::Timelike::hour(&clock) * 60 + chrono::Timelike::minute(&clock));
        let seconds = self.st.mode == Mode::Desktop && self.st.desktop.taskbar.clock_seconds;
        let status = crate::status::Status::read(seconds);
        if status != self.st.status {
            self.st.status = status;
            self.mark(Role::Taskbar);
            self.mark(Role::Auth);
            if self.st.flyout == Some(Flyout::Notifications) {
                self.mark(Role::Flyout);
            }
        }
    }

    /// Takes the desktop's settings as the compositor sent them: at start,
    /// and after every change in Settings.
    pub fn apply_desktop(&mut self, desktop: crate::settings::Desktop) {
        let first = !std::mem::replace(&mut self.st.desktop_known, true);
        let old = std::mem::replace(&mut self.st.desktop, desktop);
        let new = self.st.desktop.clone();
        let scheme = if new.light { Scheme::Light } else { Scheme::Dark };
        let theme = Theme::accented(scheme, &new.accent);
        if self.res.theme.scheme != scheme {
            self.res = Resources::new(theme);
        } else {
            self.res.theme = theme;
        }
        self.res.reduced_motion = new.reduced_motion;
        self.res.transparency = new.transparency;
        crate::ui::set_text_scale(new.text_scale);
        self.res.backdrop = None;
        self.res.backdrop_generation += 1;
        self.background.load_picture(&new.wallpaper);
        if self.st.mode == Mode::Desktop {
            // Do not disturb follows the setting when it changes (and at
            // start); quick settings and the notification centre switch it
            // for the session in between, like night light.
            if first || old.notifications.do_not_disturb != new.notifications.do_not_disturb {
                if new.notifications.do_not_disturb || !first {
                    self.set_dnd(new.notifications.do_not_disturb);
                }
            }
            if first || old.notifications.scheduled != new.notifications.scheduled || old.notifications.from != new.notifications.from || old.notifications.to != new.notifications.to {
                self.st.dnd_schedule = None;
            }
            if new.taskbar.auto_hide != self.taskbar_auto_hide {
                self.recreate_taskbars();
            }
            // The flyouts' places and sizes may have changed with the text
            // size or the taskbar's arrangement.
            if old.taskbar != new.taskbar || old.text_scale != new.text_scale || old.start != new.start {
                self.close_transients();
            }
            // The toasts' heights follow the text size.
            self.sync_toasts();
            self.desk_settings_changed();
        }
        self.mark_all();
    }

    /// Do not disturb by the clock: on when the scheduled time begins, back
    /// to the setting when it ends. A change in between (quick settings)
    /// holds until the next of the two.
    pub fn follow_dnd_schedule(&mut self, minutes: u32) {
        if self.st.mode != Mode::Desktop || !self.st.desktop_known {
            return;
        }
        let on = self.st.desktop.notifications.scheduled_at(minutes);
        let before = self.st.dnd_schedule.replace(on);
        match before {
            Some(was) if was == on => {}
            // At start or after a change of the schedule: only a schedule
            // that is on now acts.
            None if !on => {}
            _ => self.set_dnd(on || self.st.desktop.notifications.do_not_disturb),
        }
    }

    /// Makes the taskbars again after "Automatically hide the taskbar"
    /// changed: whether they reserve room for themselves is fixed when they
    /// are made.
    fn recreate_taskbars(&mut self) {
        let outputs: Vec<WlOutput> = self.surfaces.iter().filter(|s| s.role == Role::Wallpaper).map(|s| s.output.clone()).collect();
        self.remove(Role::Taskbar);
        self.taskbar_hide_at = None;
        for output in outputs {
            self.create_taskbar(&output);
        }
    }

    /// Whether something keeps an auto-hiding taskbar in sight: the pointer
    /// on it, or a flyout, a menu, a preview or a dialog of the desktop.
    fn taskbar_wanted(&self) -> bool {
        self.surfaces.iter().any(|s| s.role == Role::Taskbar && (s.pointer.is_some() || s.pressed))
            || self.st.flyout.is_some()
            || !self.st.menus.is_empty()
            || self.st.preview.is_some()
    }

    /// Shows or hides the auto-hiding taskbars.
    fn set_taskbar_hidden(&mut self, hidden: bool) {
        if self.taskbar_hidden == hidden {
            return;
        }
        self.taskbar_hidden = hidden;
        let offset = if hidden { -taskbar_hidden_offset() } else { 0 };
        for surface in self.surfaces.iter_mut().filter(|s| s.role == Role::Taskbar) {
            if let crate::Handle::Layer(layer) = &surface.handle {
                layer.set_margin(0, 0, offset, 0);
                layer.commit();
            }
            surface.touch();
        }
    }

    /// Lets an auto-hiding taskbar come when the pointer reaches the bottom
    /// edge, and go a moment after nothing needs it any more.
    fn auto_hide_taskbar(&mut self, now: Instant) {
        if !self.taskbar_auto_hide {
            self.taskbar_hide_at = None;
            return self.set_taskbar_hidden(false);
        }
        if self.taskbar_wanted() {
            self.taskbar_hide_at = None;
            self.set_taskbar_hidden(false);
        } else if !self.taskbar_hidden {
            match self.taskbar_hide_at {
                None => self.taskbar_hide_at = Some(now + TASKBAR_HIDE_DELAY),
                Some(at) if at <= now => {
                    self.taskbar_hide_at = None;
                    self.set_taskbar_hidden(true);
                }
                Some(_) => {}
            }
        }
    }

    /// Everything that happens because time passed.
    pub fn housekeeping(&mut self, now: Instant) {
        self.auto_hide_taskbar(now);
        // Transitions that ended.
        let mut removed_flyout = false;
        let mut index = 0;
        while index < self.surfaces.len() {
            let surface = &mut self.surfaces[index];
            match surface.anim {
                Some(anim) if anim.done(now) && anim.closing => {
                    removed_flyout |= surface.role == Role::Flyout;
                    self.surfaces.remove(index);
                    continue;
                }
                Some(anim) if anim.done(now) => {
                    surface.anim = None;
                    surface.dirty = true;
                }
                _ => {}
            }
            index += 1;
        }
        if removed_flyout {
            self.forget_flyout();
        }
        // Tooltips and previews that are due.
        if self.st.tooltip.as_ref().is_some_and(|t| !t.shown && t.due <= now) {
            self.show_tooltip();
        }
        if self.st.preview.as_ref().and_then(|p| p.left_at).is_some_and(|at| at + PREVIEW_GRACE <= now) {
            self.close_preview();
        }
        if self.st.osd.as_ref().is_some_and(|o| o.until <= now) {
            self.st.osd = None;
            self.remove(Role::Osd);
        }
        if self.st.notes.tick(now, self.res.reduced_motion) {
            self.sync_toasts();
        } else if self.st.notes.animating(now) {
            for surface in self.surfaces.iter_mut().filter(|s| s.role == Role::Toasts) {
                surface.dirty = true;
            }
        }
        if self.slider_pending.is_some() && now.saturating_duration_since(self.slider_sent) >= SLIDER_INTERVAL {
            if let Some(command) = self.slider_pending.take() {
                self.slider_sent = now;
                self.service(command);
            }
        }
    }

    // ---------------------------------------------------------------------
    // Flyouts
    // ---------------------------------------------------------------------

    /// Size (with shadow room), anchor, margins and origin of a flyout.
    fn flyout_geometry(&mut self, kind: Flyout, output: (f32, f32)) -> (Anchor, Placement) {
        let t = tokens();
        let bar = t.shell("taskbar-height");
        let (w, h) = output;
        let (pw, ph) = match kind {
            Flyout::Start => start::panel_size(output),
            Flyout::Quick => quick::panel_size(&self.st, output),
            Flyout::Notifications => (t.shell("notification-width").min(w - 2.0 * PAD), (h - bar - 2.0 * PAD).max(200.0)),
            Flyout::TrayOverflow => taskbar::overflow_size(&self.st),
            Flyout::Widgets => crate::widgets::panel_size(output),
        };
        let (sw, sh) = (pw + 2.0 * PAD, ph + 2.0 * PAD);
        let top = h - bar - sh;
        match kind {
            // Start opens above its button: in the middle, or at the left
            // edge when the taskbar's group is there.
            Flyout::Start if self.st.desktop.taskbar.left => {
                (Anchor::BOTTOM | Anchor::LEFT, ((sw as u32, sh as u32), (0, 0, bar as i32, 0), (0.0, top)))
            }
            Flyout::Start => (Anchor::BOTTOM, ((sw as u32, sh as u32), (0, 0, bar as i32, 0), (((w - sw) / 2.0).floor(), top))),
            // The widgets board comes from the left edge.
            Flyout::Widgets => (Anchor::BOTTOM | Anchor::LEFT, ((sw as u32, sh as u32), (0, 0, bar as i32, 0), (0.0, top))),
            Flyout::TrayOverflow => {
                // Above the chevron.
                let (inline, hidden) = taskbar::split_tray(&self.st.sys.tray);
                let status_icons = if self.st.sys.power.battery.is_some() { 3 } else { 2 };
                let lay = taskbar::layout(
                    w,
                    0,
                    inline.len() + taskbar::capture_marks(&self.st),
                    !hidden.is_empty(),
                    self.st.keyboard_layout().is_some(),
                    status_icons,
                    80.0,
                    taskbar::Arrangement::of(&self.st),
                );
                let centre = lay.overflow.map_or(w - 200.0, |r| r.center().0);
                let right = (w - (centre + sw / 2.0)).max(0.0).round();
                (Anchor::BOTTOM | Anchor::RIGHT, ((sw as u32, sh as u32), (0, right as i32, bar as i32, 0), (w - right - sw, top)))
            }
            _ => (Anchor::BOTTOM | Anchor::RIGHT, ((sw as u32, sh as u32), (0, 0, bar as i32, 0), (w - sw, top))),
        }
    }

    pub fn toggle_flyout(&mut self, kind: Flyout, output: Option<WlOutput>) {
        if self.st.mode != Mode::Desktop || self.st.locked || self.st.dialog.is_some() || self.st.capture.is_some() {
            return;
        }
        if self.st.flyout == Some(kind) && !self.flyout_closing {
            return self.close_flyout();
        }
        self.open_flyout(kind, output);
    }

    pub fn open_flyout(&mut self, kind: Flyout, output: Option<WlOutput>) {
        // Whatever was open goes at once: one flyout at a time.
        self.remove(Role::Flyout);
        self.close_menus();
        self.hide_tooltip();
        self.close_preview();
        self.flyout_closing = false;
        let Some(output) = output.or_else(|| self.primary_output()) else {
            return;
        };
        match kind {
            Flyout::Start => {
                // Applications may have been installed since the last time.
                self.st.apps = apps::load_apps(&apps::application_dirs(), &self.st.locale);
                let ids: Vec<&str> = self.st.apps.iter().map(|a| a.id.as_str()).collect();
                let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
                self.st.seen.update(&ids, now);
                self.st.start.reset();
            }
            Flyout::Quick => {
                self.st.quick.reset();
                self.service(Command::Refresh(boswas_services::Service::Audio));
            }
            Flyout::Notifications => {
                // The centre shows everything; toasts over it would repeat it.
                self.st.notes.seen();
                self.st.notes.hide_toasts();
                self.sync_toasts();
            }
            Flyout::TrayOverflow => {}
            Flyout::Widgets => self.widgets_opened(),
        }
        self.st.flyout = Some(kind);
        self.st.focus = None;
        let size = self.output_size(&output);
        let (anchor, (surface_size, margin, origin)) = self.flyout_geometry(kind, size);
        self.ensure_dismiss(&output);
        self.create_layer(
            Role::Flyout,
            &output,
            Spec {
                layer: Layer::Overlay,
                namespace: "boswas-flyout",
                anchor,
                size: surface_size,
                margin,
                exclusive: -1,
                keyboard: KeyboardInteractivity::Exclusive,
                input: true,
                origin,
                animate: true,
            },
        );
        self.mark(Role::Taskbar);
    }

    /// Resizes the open flyout to what it now shows.
    pub fn sync_flyout(&mut self) {
        let (Some(kind), Some(output)) = (self.st.flyout, self.surfaces.iter().find(|s| s.role == Role::Flyout).map(|s| s.output.clone())) else {
            return;
        };
        let size = self.output_size(&output);
        let (_, (surface_size, margin, origin)) = self.flyout_geometry(kind, size);
        self.reshape(Role::Flyout, surface_size, margin, origin);
    }

    /// The invisible surface behind flyouts and menus.
    pub(crate) fn ensure_dismiss(&mut self, output: &WlOutput) {
        if self.has(Role::Dismiss) {
            return;
        }
        let mut spec = Self::full_screen(Layer::Top, "boswas-dismiss", KeyboardInteractivity::None, 0);
        spec.input = true;
        self.create_layer(Role::Dismiss, output, spec);
    }

    pub(crate) fn full_screen(layer: Layer, namespace: &'static str, keyboard: KeyboardInteractivity, exclusive: i32) -> Spec {
        Spec {
            layer,
            namespace,
            anchor: Anchor::TOP | Anchor::BOTTOM | Anchor::LEFT | Anchor::RIGHT,
            size: (0, 0),
            margin: (0, 0, 0, 0),
            exclusive,
            keyboard,
            input: true,
            origin: (0.0, 0.0),
            animate: false,
        }
    }

    /// Closes the flyout with its transition.
    pub fn close_flyout(&mut self) {
        if self.st.flyout.is_none() || self.flyout_closing {
            return;
        }
        if self.st.flyout == Some(Flyout::Quick) {
            self.st.quick.collapse();
            if self.st.quick.page == Page::Bluetooth {
                self.service(Command::BluetoothDiscover(false));
            }
        }
        self.close_menus();
        self.flyout_closing = true;
        self.fade_out(Role::Flyout);
        if self.st.menus.is_empty() {
            self.remove(Role::Dismiss);
        }
        self.hide_tooltip();
        self.mark(Role::Taskbar);
    }

    fn forget_flyout(&mut self) {
        if self.st.flyout == Some(Flyout::Widgets) {
            self.widgets_closed();
        }
        self.st.flyout = None;
        self.flyout_closing = false;
        self.st.focus = None;
        self.st.start.reset();
        self.st.quick.reset();
        self.mark(Role::Taskbar);
    }

    /// The flyout that is open and not on its way out.
    pub fn open_kind(&self) -> Option<Flyout> {
        self.st.flyout.filter(|_| !self.flyout_closing)
    }

    /// Closes flyout, menus, tooltip and preview (not dialogs).
    pub fn close_transients(&mut self) {
        self.close_menus();
        self.close_flyout();
        self.hide_tooltip();
        self.close_preview();
    }

    /// Removes every transient surface at once, without transitions.
    pub fn drop_overlays(&mut self) {
        self.surfaces.retain(|s| matches!(s.role, Role::Wallpaper | Role::Taskbar | Role::Toasts | Role::Auth | Role::Osd));
        self.st.menus.clear();
        self.st.dialog = None;
        self.st.tooltip = None;
        self.st.preview = None;
        self.st.capture = None;
        self.st.desk.renaming = None;
        self.press = None;
        self.forget_flyout();
    }

    // ---------------------------------------------------------------------
    // Menus
    // ---------------------------------------------------------------------

    fn menu_spec(rect: Rect) -> Spec {
        Spec {
            layer: Layer::Overlay,
            namespace: "boswas-menu",
            anchor: Anchor::TOP | Anchor::LEFT,
            size: ((rect.w + 2.0 * PAD) as u32, (rect.h + 2.0 * PAD) as u32),
            margin: ((rect.y - PAD) as i32, 0, 0, (rect.x - PAD) as i32),
            exclusive: -1,
            keyboard: KeyboardInteractivity::Exclusive,
            input: true,
            origin: (rect.x - PAD, rect.y - PAD),
            animate: true,
        }
    }

    /// Opens a context menu at a point of an output.
    pub fn open_menu(&mut self, items: Vec<Item>, x: f32, y: f32, upward: bool, output: &WlOutput) {
        self.close_menus();
        self.hide_tooltip();
        self.close_preview();
        if items.is_empty() || self.st.dialog.is_some() || self.st.locked {
            return;
        }
        let mut menu = Menu::new(items, x, y, upward);
        let size = self.output_size(output);
        let rect = menu.place(&mut self.res.text, size);
        self.st.menus.push(menu);
        self.ensure_dismiss(output);
        self.create_layer(Role::Menu(0), output, Self::menu_spec(rect));
    }

    /// Opens the submenu of an entry, beside its row.
    pub fn open_submenu(&mut self, level: usize, index: usize) {
        let Some(output) = self.surfaces.iter().find(|s| s.role == Role::Menu(level)).map(|s| (s.output.clone(), s.origin)) else {
            return;
        };
        let (output, origin) = output;
        self.st.menus.truncate(level + 1);
        self.surfaces.retain(|s| !matches!(s.role, Role::Menu(l) if l > level));
        let size = self.output_size(&output);
        let Some(parent) = self.st.menus.get_mut(level) else {
            return;
        };
        let Some(item) = parent.items.get(index).filter(|i| !i.children.is_empty()) else {
            return;
        };
        parent.selected = Some(index);
        let row = parent.row((origin.0 + PAD, origin.1 + PAD), index);
        let mut menu = Menu::new(item.children.clone(), row.right() + 2.0, row.y - 4.0, false);
        let mut rect = menu.place(&mut self.res.text, size);
        if rect.x < row.right() {
            // No room at the right: open at the left of the parent.
            menu.x = (row.x - menu.width - 2.0).max(PAD);
            rect = menu.place(&mut self.res.text, size);
        }
        menu.selected = menu.items.iter().position(|i| i.enabled && !i.separator);
        self.st.menus.push(menu);
        self.create_layer(Role::Menu(level + 1), &output, Self::menu_spec(rect));
        self.mark(Role::Menu(level));
    }

    pub fn close_menus(&mut self) {
        if self.st.menus.is_empty() {
            return;
        }
        self.st.menus.clear();
        self.surfaces.retain(|s| !matches!(s.role, Role::Menu(_)));
        self.tray_menu = None;
        if self.open_kind().is_none() {
            self.remove(Role::Dismiss);
        }
    }

    /// Carries out a menu entry.
    pub fn run_menu(&mut self, cmd: Cmd) {
        self.close_menus();
        match cmd {
            Cmd::Launch(id) => {
                if let Some(index) = self.st.app_index(&id) {
                    self.launch_app(index);
                }
            }
            Cmd::Spawn(argv) => {
                self.spawn(&argv);
                self.close_transients();
            }
            Cmd::Close(ids) => {
                for id in ids {
                    self.window_command("close", id);
                }
            }
            Cmd::PinTaskbar(id, pinned) => {
                if self.st.config.pin_taskbar(&id, pinned) {
                    self.mark(Role::Taskbar);
                }
            }
            Cmd::PinStart(id, pinned) => {
                let current = self.st.start_pins();
                if self.st.config.pin_start(&id, pinned, &current) {
                    self.mark(Role::Flyout);
                }
            }
            Cmd::Lock => {
                self.close_transients();
                self.send_control("lock");
            }
            Cmd::SignOut => self.leave(Leave::SignOut),
            Cmd::Power(PowerAction::Reboot) => self.leave(Leave::Restart),
            Cmd::Power(PowerAction::PowerOff) => self.leave(Leave::ShutDown),
            Cmd::Power(action) => {
                self.close_transients();
                self.power(action);
            }
            Cmd::Tray { key, id } => self.service(Command::TrayMenuActivate { key, id }),
            Cmd::Widgets(cmd) => self.widgets_command(cmd),
            Cmd::Desk(cmd) => self.desk_command(cmd),
            Cmd::Submenu | Cmd::Nothing => {}
        }
    }

    /// The menu of a taskbar button.
    pub fn task_menu(&self, task: &taskbar::Task) -> Vec<Item> {
        let mut items = Vec::new();
        if let Some(app) = task.app.and_then(|index| self.st.apps.get(index)) {
            for action in &app.actions {
                items.push(Item::new(&action.name, "", Cmd::Spawn(action.exec.clone())));
            }
            if !items.is_empty() {
                items.push(Item::separator());
            }
            let label = if task.windows.is_empty() { "Open".to_owned() } else { format!("New window of {}", app.name) };
            items.push(Item::new(&label, &app.icon, Cmd::Launch(app.id.clone())));
            let pinned = self.st.config.is_on_taskbar(&app.id);
            items.push(Item::new(if pinned { "Unpin from taskbar" } else { "Pin to taskbar" }, "", Cmd::PinTaskbar(app.id.clone(), !pinned)));
        }
        match task.windows.len() {
            0 => {}
            1 => items.push(Item::new("Close window", "window-close-symbolic", Cmd::Close(task.windows.clone()))),
            _ => items.push(Item::new("Close all windows", "window-close-symbolic", Cmd::Close(task.windows.clone()))),
        }
        items
    }

    /// The menu of an application in Start.
    pub fn app_menu(&self, index: usize) -> Vec<Item> {
        let Some(app) = self.st.apps.get(index) else {
            return Vec::new();
        };
        let mut items = vec![Item::new("Open", &app.icon, Cmd::Launch(app.id.clone()))];
        let in_start = self.st.start_pins().contains(&app.id);
        items.push(Item::new(if in_start { "Unpin from Start" } else { "Pin to Start" }, "", Cmd::PinStart(app.id.clone(), !in_start)));
        let on_taskbar = self.st.config.is_on_taskbar(&app.id);
        items.push(Item::new(if on_taskbar { "Unpin from taskbar" } else { "Pin to taskbar" }, "", Cmd::PinTaskbar(app.id.clone(), !on_taskbar)));
        if !app.actions.is_empty() {
            items.push(Item::separator());
            for action in &app.actions {
                items.push(Item::new(&action.name, "", Cmd::Spawn(action.exec.clone())));
            }
        }
        items
    }

    pub fn user_menu(&self) -> Vec<Item> {
        vec![
            Item::new("Lock", "system-lock-screen-symbolic", Cmd::Lock),
            Item::new("Sign out", "system-log-out-symbolic", Cmd::SignOut),
            Item::separator(),
            Item::new("Account settings", "preferences-system-symbolic", Cmd::Spawn(start::settings_command("users"))),
        ]
    }

    pub fn power_menu(&self) -> Vec<Item> {
        let power = &self.st.sys.power;
        let known = self.st.sys.power_known;
        let mut sleep = Item::new("Sleep", "system-suspend-symbolic", Cmd::Power(PowerAction::Suspend));
        sleep.enabled = !known || power.can_suspend;
        let mut restart = Item::new("Restart", "system-reboot-symbolic", Cmd::Power(PowerAction::Reboot));
        restart.enabled = !known || power.can_reboot;
        let mut off = Item::new("Shut down", "system-shutdown-symbolic", Cmd::Power(PowerAction::PowerOff));
        off.enabled = !known || power.can_power_off;
        let mut items = vec![sleep];
        if known && power.can_hibernate {
            items.push(Item::new("Hibernate", "system-suspend-symbolic", Cmd::Power(PowerAction::Hibernate)));
        }
        items.extend([restart, off]);
        items
    }

    /// The menu of the taskbar's background.
    pub fn taskbar_menu(&self) -> Vec<Item> {
        vec![Item::new("Taskbar settings", "preferences-system-symbolic", Cmd::Spawn(start::settings_command("taskbar")))]
    }

    /// The menu of the desktop's background (desktop_icons.rs): View,
    /// Sort by, Refresh, Paste, New, Open in Terminal and the settings.
    pub fn desktop_menu(&self) -> Vec<Item> {
        self.desk_background_menu()
    }

    // ---------------------------------------------------------------------
    // Power
    // ---------------------------------------------------------------------

    /// Signs out, restarts or shuts down; asks first when windows are open.
    pub fn leave(&mut self, leave: Leave) {
        self.close_transients();
        let open_windows = self.st.windows.len();
        if open_windows == 0 {
            return self.do_leave(leave);
        }
        self.open_dialog(Dialog::Confirm { leave, open_windows });
    }

    fn do_leave(&mut self, leave: Leave) {
        match leave {
            Leave::SignOut => {
                self.send_control("quit");
            }
            Leave::Restart => self.power(PowerAction::Reboot),
            Leave::ShutDown => self.power(PowerAction::PowerOff),
        }
    }

    pub fn power(&mut self, action: PowerAction) {
        if self.st.sys.power_known {
            return self.service(Command::Power(action));
        }
        // No answer from logind through the services: ask systemd directly.
        let verb = match action {
            PowerAction::Suspend => "suspend",
            PowerAction::Hibernate => "hibernate",
            PowerAction::Reboot => "reboot",
            PowerAction::PowerOff => "poweroff",
            PowerAction::LogOut => {
                self.send_control("quit");
                return;
            }
        };
        self.spawn(&["systemctl".into(), verb.into()]);
    }

    // ---------------------------------------------------------------------
    // Dialogs
    // ---------------------------------------------------------------------

    pub fn open_dialog(&mut self, dialog: Dialog) {
        self.close_transients();
        self.remove(Role::Flyout);
        self.remove(Role::Dismiss);
        if self.st.flyout.is_some() {
            self.forget_flyout();
        }
        self.remove(Role::Dialog);
        self.remove(Role::Smoke);
        let Some(output) = self.primary_output() else {
            return;
        };
        let (w, h) = self.output_size(&output);
        let (dw, dh) = dialog.size();
        let (dw, dh) = (dw.min(w - 2.0 * PAD), dh.min(h - 2.0 * PAD));
        let corner = dialog.at_corner();
        let bar = tokens().shell("taskbar-height");
        self.st.dialog = Some(dialog);
        self.st.focus = None;
        self.create_layer(Role::Smoke, &output, Self::full_screen(Layer::Overlay, "boswas-smoke", KeyboardInteractivity::None, -1));
        let size = ((dw + 2.0 * PAD) as u32, (dh + 2.0 * PAD) as u32);
        let (anchor, margin, origin) = if corner {
            (Anchor::BOTTOM | Anchor::LEFT, (0, 0, bar as i32, 0), (0.0, h - bar - dh - 2.0 * PAD))
        } else {
            (Anchor::empty(), (0, 0, 0, 0), (((w - dw) / 2.0 - PAD).floor(), ((h - dh) / 2.0 - PAD).floor()))
        };
        self.create_layer(
            Role::Dialog,
            &output,
            Spec {
                layer: Layer::Overlay,
                namespace: "boswas-dialog",
                anchor,
                size,
                margin,
                exclusive: -1,
                keyboard: KeyboardInteractivity::Exclusive,
                input: true,
                origin,
                animate: true,
            },
        );
    }

    pub fn close_dialog(&mut self) {
        self.st.dialog = None;
        self.st.focus = None;
        self.remove(Role::Dialog);
        self.remove(Role::Smoke);
    }

    pub fn dialog_cancel(&mut self) {
        match &self.st.dialog {
            Some(Dialog::Auth(auth)) => {
                let cookie = auth.request.cookie.clone();
                self.service(Command::AuthCancel { cookie });
            }
            Some(Dialog::Screen { id, .. }) => {
                let id = *id;
                self.capture_answer(id, false);
            }
            _ => {}
        }
        self.close_dialog();
    }

    /// Tells the compositor what the user answered to a program that asked
    /// to see the screen.
    fn capture_answer(&mut self, id: u64, allow: bool) {
        self.control_with("capture-answer", json!({ "request": id, "allow": allow }));
    }

    /// The capture mark on the taskbar was clicked: nobody may capture any
    /// more.
    pub fn stop_capturing(&mut self) {
        self.send_control("capture-stop");
        self.st.capturing.clear();
        self.mark(Role::Taskbar);
    }

    /// Another of the administrators a request names authenticates.
    pub fn dialog_choose_user(&mut self, index: usize) {
        let Some(Dialog::Auth(auth)) = self.st.dialog.as_mut() else {
            return;
        };
        if let Some(user) = auth.choose(index) {
            let cookie = auth.request.cookie.clone();
            self.service(Command::AuthChooseUser { cookie, user });
            self.st.focus = None;
            self.mark(Role::Dialog);
        }
    }

    pub fn dialog_primary(&mut self) {
        match self.st.dialog.as_mut() {
            Some(Dialog::Auth(auth)) => {
                if !auth.ready() {
                    return;
                }
                let response = Secret::new(std::mem::take(&mut *auth.field.take()));
                auth.busy = true;
                auth.message = None;
                auth.reveal = false;
                let cookie = auth.request.cookie.clone();
                self.service(Command::AuthRespond { cookie, response });
                self.mark(Role::Dialog);
            }
            Some(Dialog::Confirm { leave, .. }) => {
                let leave = *leave;
                self.close_dialog();
                self.do_leave(leave);
            }
            Some(Dialog::Screen { id, opened, .. }) => {
                if opened.elapsed() < dialogs::SCREEN_GUARD {
                    return;
                }
                let id = *id;
                self.capture_answer(id, true);
                self.close_dialog();
            }
            Some(Dialog::Run(run)) => {
                let text = run.field.text().trim().to_owned();
                let Some(argv) = dialogs::split_command(&text) else {
                    run.error = Some("That is not a command.".into());
                    return self.mark(Role::Dialog);
                };
                // A program on the PATH, or else an application by name.
                let started = if apps::program_exists(&argv[0]) {
                    apps::spawn(&argv).map_err(|e| e.to_string())
                } else {
                    let lower = text.to_lowercase();
                    match self.st.apps.iter().find(|a| a.name.to_lowercase() == lower || a.id.to_lowercase() == lower) {
                        Some(app) => apps::launch(app).map_err(|e| e.to_string()),
                        None => Err(format!("“{}” was not found.", argv[0])),
                    }
                };
                match started {
                    Ok(()) => {
                        self.st.run_history.push(&text);
                        self.close_dialog();
                    }
                    Err(message) => {
                        if let Some(Dialog::Run(run)) = self.st.dialog.as_mut() {
                            run.error = Some(message);
                        }
                        self.mark(Role::Dialog);
                    }
                }
            }
            None => {}
        }
    }

    // ---------------------------------------------------------------------
    // Tooltips, previews, toasts and the on-screen display
    // ---------------------------------------------------------------------

    pub fn hide_tooltip(&mut self) {
        if self.st.tooltip.take().is_some() {
            self.remove(Role::Tooltip);
        }
    }

    /// Schedules the tooltip (or, for a running application, the window
    /// previews) of the control the pointer came to rest on.
    pub fn schedule_tooltip(&mut self, surface: usize) {
        self.hide_tooltip();
        if !self.st.menus.is_empty() || self.st.dialog.is_some() || self.st.capture.is_some() {
            return;
        }
        let Some(s) = self.surfaces.get(surface) else {
            return;
        };
        let Some(hit) = s.hover.and_then(|h| s.hits.get(h)) else {
            return;
        };
        let Some(text) = hit.tooltip.clone().filter(|t| !t.is_empty()) else {
            return;
        };
        let task = match &hit.action {
            Action::Taskbar(taskbar::Act::Task(index)) if s.role == Role::Taskbar => Some(*index),
            _ => None,
        };
        self.st.tooltip = Some(Tooltip {
            text,
            x: s.origin.0 + hit.rect.center().0,
            y: s.origin.1 + hit.rect.y,
            due: Instant::now() + Duration::from_millis(tokens().shell("tooltip-delay-ms") as u64),
            shown: false,
            task,
        });
    }

    fn show_tooltip(&mut self) {
        let Some(tooltip) = self.st.tooltip.as_mut() else {
            return;
        };
        tooltip.shown = true;
        let (text, x, y, task) = (tooltip.text.clone(), tooltip.x, tooltip.y, tooltip.task);
        let Some(output) = self.primary_output() else {
            return;
        };
        // A running application shows its windows instead of its name.
        if let Some(task) = task {
            let list = self.st.tasks();
            if let Some(entry) = list.get(task).filter(|t| !t.windows.is_empty()) {
                if self.open_kind().is_none() {
                    let windows = entry.windows.clone();
                    self.st.tooltip = None;
                    return self.open_preview(task, windows, x, &output);
                }
            }
        }
        let (w, h) = self.output_size(&output);
        let (tw, th) = tooltip_size(&mut self.res, &text);
        let margin = PAD;
        let (sw, sh) = (tw + 2.0 * margin, th + 2.0 * margin);
        let left = (x - sw / 2.0).clamp(0.0, (w - sw).max(0.0)).round();
        // Above the control; below it when there is no room above.
        let above = y - 4.0 - sh + margin;
        let top = if above >= 0.0 { above } else { (y + 40.0).min(h - sh) }.round();
        self.create_layer(
            Role::Tooltip,
            &output,
            Spec {
                layer: Layer::Overlay,
                namespace: "boswas-tooltip",
                anchor: Anchor::TOP | Anchor::LEFT,
                size: (sw as u32, sh as u32),
                margin: (top as i32, 0, 0, left as i32),
                exclusive: -1,
                keyboard: KeyboardInteractivity::None,
                input: false,
                origin: (left, top),
                animate: true,
            },
        );
    }

    fn preview_spec(&self, preview: &Preview, output: (f32, f32)) -> Placement {
        let bar = tokens().shell("taskbar-height");
        let (pw, ph) = preview.size();
        let (sw, sh) = (pw.min(output.0 - 2.0 * PAD) + 2.0 * PAD, ph + 2.0 * PAD);
        let left = (preview.x - sw / 2.0).clamp(0.0, (output.0 - sw).max(0.0)).round();
        let bottom = bar - PAD + 8.0;
        ((sw as u32, sh as u32), (0, 0, bottom as i32, left as i32), (left, output.1 - bottom - sh))
    }

    fn open_preview(&mut self, task: usize, windows: Vec<u64>, x: f32, output: &WlOutput) {
        self.remove(Role::Preview);
        let preview = Preview {
            task,
            windows,
            x,
            left_at: None,
        };
        let (size, margin, origin) = self.preview_spec(&preview, self.output_size(output));
        // Ask the compositor for pictures of the windows.
        if !self.thumbnails_unsupported {
            if let Some(dir) = runtime_dir() {
                for id in preview.windows.iter().take(6).copied() {
                    let path = dir.join(format!("boswas-shell-thumbnail-{}-{id}.png", std::process::id()));
                    if let Some(request) = self.control_with("thumbnail", json!({ "window": id, "path": path, "width": 384 })) {
                        self.pending.insert(request, Pending::Thumbnail { window: id, path });
                    }
                }
            }
        }
        self.st.preview = Some(preview);
        self.create_layer(
            Role::Preview,
            output,
            Spec {
                layer: Layer::Overlay,
                namespace: "boswas-preview",
                anchor: Anchor::BOTTOM | Anchor::LEFT,
                size,
                margin,
                exclusive: -1,
                keyboard: KeyboardInteractivity::None,
                input: true,
                origin,
                animate: true,
            },
        );
    }

    fn sync_preview(&mut self) {
        let (Some(preview), Some(output)) = (self.st.preview.as_ref(), self.surfaces.iter().find(|s| s.role == Role::Preview).map(|s| s.output.clone())) else {
            return;
        };
        let (size, margin, origin) = self.preview_spec(preview, self.output_size(&output));
        self.reshape(Role::Preview, size, margin, origin);
    }

    pub fn close_preview(&mut self) {
        if self.st.preview.take().is_some() {
            self.remove(Role::Preview);
        }
    }

    /// Creates, resizes or removes the surface that holds the toasts.
    pub fn sync_toasts(&mut self) {
        if self.st.notes.toasts.is_empty() {
            return self.remove(Role::Toasts);
        }
        let Some(output) = self.primary_output() else {
            return;
        };
        let (w, h) = self.output_size(&output);
        let bar = tokens().shell("taskbar-height");
        let (sw, sh) = notify::toasts_size(&mut self.res.text, &self.st.notes);
        let sh = sh.min(h - bar);
        let size = (sw as u32, sh as u32);
        let margin = (0, 0, bar as i32, 0);
        let origin = (w - sw, h - bar - sh);
        if self.has(Role::Toasts) {
            return self.reshape(Role::Toasts, size, margin, origin);
        }
        self.create_layer(
            Role::Toasts,
            &output,
            Spec {
                layer: Layer::Overlay,
                namespace: "boswas-toasts",
                anchor: Anchor::BOTTOM | Anchor::RIGHT,
                size,
                margin,
                exclusive: -1,
                keyboard: KeyboardInteractivity::None,
                input: true,
                origin,
                animate: false,
            },
        );
    }

    fn show_osd(&mut self, kind: osd::Kind, value: f32) {
        self.st.osd = Some(osd::Osd::new(kind, value, Instant::now()));
        if self.has(Role::Osd) {
            return self.mark(Role::Osd);
        }
        let Some(output) = self.primary_output() else {
            return;
        };
        let (w, h) = self.output_size(&output);
        let bar = tokens().shell("taskbar-height");
        let (sw, sh) = (osd::SIZE.0 + 2.0 * PAD, osd::SIZE.1 + 2.0 * PAD);
        self.create_layer(
            Role::Osd,
            &output,
            Spec {
                layer: Layer::Overlay,
                namespace: "boswas-osd",
                anchor: Anchor::BOTTOM,
                size: (sw as u32, sh as u32),
                margin: (0, 0, bar as i32, 0),
                exclusive: -1,
                keyboard: KeyboardInteractivity::None,
                input: false,
                origin: (((w - sw) / 2.0).floor(), h - bar - sh),
                animate: true,
            },
        );
    }

    /// A volume, brightness or media key.
    pub fn media_key(&mut self, key: &str) {
        if self.st.mode != Mode::Desktop {
            return;
        }
        let output = self.st.sys.audio.output.clone();
        match key {
            "volume-up" | "volume-down" => match output {
                Some(mut device) => {
                    let up = key == "volume-up";
                    device.volume = osd::step_volume(device.volume, up);
                    if up && device.muted {
                        device.muted = false;
                        self.service(Command::SetMuted(false));
                    }
                    self.service(Command::SetVolume(device.volume));
                    self.show_osd(osd::Kind::Volume { muted: device.muted }, device.volume);
                    self.st.sys.audio.output = Some(device);
                    self.mark(Role::Taskbar);
                }
                None => self.show_osd(osd::Kind::Unavailable, 0.0),
            },
            "volume-mute" => match output {
                Some(mut device) => {
                    device.muted = !device.muted;
                    self.service(Command::SetMuted(device.muted));
                    self.show_osd(osd::Kind::Volume { muted: device.muted }, device.volume);
                    self.st.sys.audio.output = Some(device);
                    self.mark(Role::Taskbar);
                }
                None => self.show_osd(osd::Kind::Unavailable, 0.0),
            },
            "mic-mute" => {
                if let Some(input) = self.st.sys.audio.input.as_mut() {
                    input.muted = !input.muted;
                    let muted = input.muted;
                    self.service(Command::SetInputMuted(muted));
                    self.notice(if muted { "Microphone muted" } else { "Microphone on" }, "", None);
                }
            }
            "brightness-up" | "brightness-down" => {
                if let Some(mut brightness) = self.st.sys.brightness {
                    brightness.level = osd::step_brightness(brightness.level, key == "brightness-up");
                    self.st.sys.brightness = Some(brightness);
                    self.service(Command::SetBrightness(brightness.level));
                    self.show_osd(osd::Kind::Brightness, brightness.level);
                }
            }
            "media-play" | "media-next" | "media-previous" | "media-stop" => {
                if let Some(player) = self.st.sys.media.first() {
                    let key_name = player.key.clone();
                    let playing = player.playing;
                    match key {
                        "media-next" => self.service(Command::MediaNext { key: key_name }),
                        "media-previous" => self.service(Command::MediaPrevious { key: key_name }),
                        "media-stop" if !playing => {}
                        _ => self.service(Command::MediaPlayPause { key: key_name }),
                    }
                }
            }
            _ => {}
        }
    }

    /// Sets a slider's value: shown at once, sent at a limited rate.
    pub fn slide(&mut self, command: Command) {
        match &command {
            Command::SetVolume(value) => {
                if let Some(device) = self.st.sys.audio.output.as_mut() {
                    device.volume = *value;
                    device.muted = false;
                }
                self.mark(Role::Taskbar);
            }
            Command::SetBrightness(value) => {
                if let Some(brightness) = self.st.sys.brightness.as_mut() {
                    brightness.level = *value;
                }
            }
            _ => {}
        }
        let now = Instant::now();
        if now.saturating_duration_since(self.slider_sent) >= SLIDER_INTERVAL {
            self.slider_sent = now;
            self.slider_pending = None;
            self.service(command);
        } else {
            self.slider_pending = Some(command);
        }
        self.mark(Role::Flyout);
    }

    // ---------------------------------------------------------------------
    // Screen capture
    // ---------------------------------------------------------------------

    /// Asks the compositor for a picture of the screen; the capture view
    /// opens when it arrives.
    pub fn start_capture(&mut self, whole: bool) {
        if self.st.mode != Mode::Desktop || self.st.locked || self.st.capture.is_some() || self.st.dialog.is_some() {
            return;
        }
        if self.pending.values().any(|p| matches!(p, Pending::Screenshot { .. })) {
            return;
        }
        // Flyouts and menus must not be in the picture.
        self.drop_overlays();
        let Some(dir) = runtime_dir() else {
            return self.notice("Screen capture is not available", "There is no runtime directory.", None);
        };
        let path = dir.join(format!("boswas-shell-capture-{}.png", std::process::id()));
        match self.control_with("screenshot", json!({ "path": path })) {
            Some(request) => {
                self.pending.insert(request, Pending::Screenshot { path, whole });
            }
            None => self.notice("Screen capture is not available", "The compositor cannot be reached.", None),
        }
    }

    fn open_capture(&mut self, picture: tiny_skia::Pixmap, whole: bool) {
        let Some(output) = self.primary_output() else {
            return;
        };
        self.drop_overlays();
        self.st.capture = Some(Capture::new(picture, whole));
        self.create_layer(Role::Capture, &output, Self::full_screen(Layer::Overlay, "boswas-capture", KeyboardInteractivity::Exclusive, -1));
    }

    pub fn cancel_capture(&mut self) {
        self.st.capture = None;
        self.remove(Role::Capture);
    }

    /// Saves the selection, puts it on the clipboard and says so.
    pub fn finish_capture(&mut self) {
        let Some(size) = self.surfaces.iter().find(|s| s.role == Role::Capture).map(|s| (s.width as f32, s.height as f32)) else {
            return self.cancel_capture();
        };
        // Nothing selected yet: Enter does nothing.
        let Some(picture) = self.st.capture.as_ref().and_then(|c| c.crop(size)) else {
            return;
        };
        self.cancel_capture();
        let Some(png) = boswas_ui::icons::encode_png(&picture) else {
            return;
        };
        let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
        let user_dirs = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
            .unwrap_or_else(|| home.join(".config"))
            .join("user-dirs.dirs");
        let dir = capture::screenshots_dir(&home, std::fs::read_to_string(user_dirs).ok().as_deref());
        let name = capture::file_name(&chrono::Local::now().format("%Y-%m-%d %H%M%S").to_string(), |name| dir.join(name).exists());
        let path = dir.join(&name);
        let saved = std::fs::create_dir_all(&dir).and_then(|()| std::fs::write(&path, &png));
        self.copy_png(png);
        match saved {
            Ok(()) => self.notice("Screenshot saved", &format!("{name}\nCopied to the clipboard."), Some(path)),
            Err(err) => self.notice("Screenshot copied to the clipboard", &format!("It could not be saved: {err}"), None),
        }
    }

    /// An action of one of the shell's own notifications: `default` opens
    /// its file, `folder` the folder the file is in. Returns whether the
    /// notification was one of the shell's own.
    pub fn open_note(&mut self, id: u32, action: &str) -> bool {
        let Some(path) = self.shell_notes.get(&id).cloned() else {
            return false;
        };
        match action {
            "folder" => {
                if let Some(folder) = path.parent().map(std::path::Path::to_path_buf) {
                    self.open_path(&folder);
                }
            }
            _ => {
                self.open_path(&path);
            }
        }
        true
    }

    /// Runs a search result of Start.
    pub fn activate_result(&mut self, result: ResultItem) {
        match result {
            ResultItem::App(index) => self.launch_app(index),
            ResultItem::Setting(index) => {
                self.spawn(&start::settings_command(start::SETTINGS[index].0));
                self.close_transients();
            }
            ResultItem::Run(command) => {
                let started = dialogs::split_command(&command).filter(|argv| apps::program_exists(&argv[0])).is_some_and(|argv| self.spawn(&argv));
                if started {
                    self.st.run_history.push(&command);
                } else {
                    self.notice("The command could not be started", &command, None);
                }
                self.close_transients();
            }
        }
    }
}
