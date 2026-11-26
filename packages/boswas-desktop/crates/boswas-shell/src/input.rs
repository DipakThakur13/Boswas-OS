//! How the pointer and the keyboard reach the shell's surfaces.

use std::time::Instant;

use boswas_services::Command;
use smithay_client_toolkit::seat::{
    keyboard::{KeyEvent, Keysym},
    pointer::{PointerEvent, PointerEventKind},
};

use crate::{
    action::Action,
    capture, dialogs,
    dialogs::Dialog,
    lock::{self, AuthState, Stage},
    notify, quick,
    quick::Page,
    start::{self, KeyOutcome},
    state::{Flyout, Mode},
    svc,
    taskbar::{self, PreviewAct},
    ui::{self, Hit, Nav},
    Role, Shell,
};

const BTN_LEFT: u32 = 0x110;
const BTN_RIGHT: u32 = 0x111;
const BTN_MIDDLE: u32 = 0x112;

impl Shell {
    // ---------------------------------------------------------------------
    // Pointer
    // ---------------------------------------------------------------------

    pub fn pointer_event(&mut self, event: &PointerEvent) {
        let Some(index) = self.surface_index(&event.surface) else {
            return;
        };
        let position = (event.position.0 as f32, event.position.1 as f32);
        let role = self.surfaces[index].role;
        // The desktop icons take what happens on the wallpaper.
        if role == Role::Wallpaper && self.desk_pointer(index, event) {
            return;
        }
        match event.kind {
            PointerEventKind::Enter { .. } | PointerEventKind::Motion { .. } => {
                let surface = &mut self.surfaces[index];
                let before = surface.hover;
                let after = ui::hit_at(&surface.hits, position);
                surface.pointer = Some(position);
                surface.hover = after;
                if before != after {
                    surface.touch();
                    self.schedule_tooltip(index);
                    self.track_preview(index);
                }
                if role == Role::Toasts {
                    self.st.notes.hover(true, Instant::now());
                }
                if self.surfaces[index].pressed {
                    self.drag(index, position);
                }
            }
            PointerEventKind::Leave { .. } => {
                let surface = &mut self.surfaces[index];
                surface.pointer = None;
                surface.hover = None;
                surface.pressed = false;
                surface.touch();
                self.hide_tooltip();
                match role {
                    Role::Toasts => self.st.notes.hover(false, Instant::now()),
                    Role::Taskbar | Role::Preview => {
                        if let Some(preview) = self.st.preview.as_mut() {
                            preview.left_at.get_or_insert_with(Instant::now);
                        }
                    }
                    _ => {}
                }
            }
            PointerEventKind::Press { button, serial, .. } => {
                self.serial = serial;
                self.hide_tooltip();
                // A press anywhere outside an open menu closes the menu and
                // does nothing else.
                if !self.st.menus.is_empty() && !matches!(role, Role::Menu(_)) {
                    return self.close_menus();
                }
                let surface = &mut self.surfaces[index];
                surface.pointer = Some(position);
                let hit = ui::hit_at(&surface.hits, position).map(|h| surface.hits[h].clone());
                let Some(hit) = hit else {
                    // Outside every control of an overlay: the shadow's margin.
                    if matches!(role, Role::Flyout | Role::Menu(_)) && button == BTN_LEFT {
                        self.activate(Action::Dismiss, index);
                    }
                    return;
                };
                match button {
                    BTN_LEFT => {
                        if role == Role::Dismiss {
                            return self.activate(Action::Dismiss, index);
                        }
                        surface.pressed = true;
                        surface.touch();
                        self.press = Some((index, hit.action.clone()));
                        if hit.drag {
                            if let Some(capture) = self.st.capture.as_mut().filter(|c| !c.whole) {
                                capture.anchor = Some(position);
                                capture.pointer = Some(position);
                                capture.dragging = true;
                            }
                            self.drag(index, position);
                        }
                    }
                    BTN_RIGHT => self.context(hit.action, index, position),
                    BTN_MIDDLE => self.secondary(hit.action, index, position),
                    _ => {}
                }
            }
            PointerEventKind::Release { button, serial, .. } => {
                self.serial = serial;
                if button != BTN_LEFT {
                    return;
                }
                let surface = &mut self.surfaces[index];
                surface.pressed = false;
                surface.touch();
                let under = ui::hit_at(&surface.hits, position).map(|h| surface.hits[h].action.clone());
                let pressed = self.press.take();
                if role == Role::Capture {
                    return self.capture_released();
                }
                // A click counts when it ends on the control it began on.
                if let (Some((pressed_surface, pressed_action)), Some(under)) = (pressed, under) {
                    if pressed_surface == index && pressed_action == under {
                        self.activate(under, index);
                    }
                }
            }
            PointerEventKind::Axis { vertical, .. } => {
                // Start's page follows a touchpad's fingers pixel by pixel,
                // and moves smoothly by a row of tiles per step of a wheel.
                if role == Role::Flyout && self.open_kind() == Some(Flyout::Start) {
                    return self.scroll_start(vertical.discrete, vertical.absolute);
                }
                let steps = if vertical.discrete != 0 { vertical.discrete } else { (vertical.absolute / 30.0).round() as i32 };
                if steps != 0 {
                    self.scroll(index, position, steps);
                }
            }
        }
    }

    /// Keeps the window previews open while the pointer is on their task
    /// button or on them.
    fn track_preview(&mut self, index: usize) {
        let surface = &self.surfaces[index];
        let on_task = match surface.hover.and_then(|h| surface.hits.get(h)).map(|h| &h.action) {
            Some(Action::Taskbar(taskbar::Act::Task(task))) if surface.role == Role::Taskbar => Some(*task),
            _ => None,
        };
        let role = surface.role;
        if let Some(preview) = self.st.preview.as_mut() {
            if role == Role::Preview || on_task == Some(preview.task) {
                preview.left_at = None;
            } else if role == Role::Taskbar {
                preview.left_at.get_or_insert_with(Instant::now);
            }
        }
    }

    /// The pointer moved with the button held: sliders and the capture
    /// selection follow it.
    fn drag(&mut self, index: usize, position: (f32, f32)) {
        let Some((pressed_surface, action)) = self.press.clone() else {
            return;
        };
        if pressed_surface != index {
            return;
        }
        let rect = self.surfaces[index].hits.iter().find(|h| h.action == action && h.drag).map(|h| h.rect);
        let Some(rect) = rect else {
            return;
        };
        match action {
            Action::Quick(quick::Act::Volume) => {
                let value = ui::slider_value(rect, position.0);
                self.slide(Command::SetVolume(value));
            }
            Action::Quick(quick::Act::Brightness) => {
                let value = ui::slider_value(rect, position.0).max(0.02);
                self.slide(Command::SetBrightness(value));
            }
            Action::Capture(capture::Act::Surface) => {
                if let Some(capture) = self.st.capture.as_mut().filter(|c| c.dragging) {
                    capture.pointer = Some(position);
                    self.mark(Role::Capture);
                }
            }
            _ => {}
        }
    }

    fn capture_released(&mut self) {
        let Some(capture) = self.st.capture.as_mut() else {
            return;
        };
        if capture.whole {
            return self.finish_capture();
        }
        capture.dragging = false;
        self.mark(Role::Capture);
    }

    fn scroll(&mut self, index: usize, position: (f32, f32), steps: i32) {
        let surface = &self.surfaces[index];
        let role = surface.role;
        let under = ui::hit_at(&surface.hits, position).map(|h| surface.hits[h].action.clone());
        match (role, self.open_kind()) {
            (Role::Flyout, Some(Flyout::Quick)) => {
                let count = match self.st.quick.page {
                    Page::Wifi => self.st.sys.network.access_points.len(),
                    Page::Bluetooth => self.st.sys.bluetooth.devices.len(),
                    Page::Outputs => self.st.sys.audio.outputs.len(),
                    Page::Main => 0,
                };
                let scroll = (self.st.quick.scroll as i64 + i64::from(steps)).clamp(0, count.saturating_sub(1) as i64) as usize;
                if scroll != self.st.quick.scroll {
                    self.st.quick.scroll = scroll;
                    self.mark(Role::Flyout);
                }
            }
            (Role::Flyout, Some(Flyout::Clipboard)) => self.clip_scroll(steps),
            (Role::Flyout, Some(Flyout::Emoji)) => self.emoji_scroll(steps),
            (Role::Flyout, Some(Flyout::Widgets)) => self.widgets_scroll(steps),
            (Role::Flyout, Some(Flyout::Notifications)) => {
                let max = self.st.notes.history.len().saturating_sub(1) as i64;
                let scroll = (self.st.notes.scroll as i64 + i64::from(steps)).clamp(0, max) as usize;
                if scroll != self.st.notes.scroll {
                    self.st.notes.scroll = scroll;
                    self.mark(Role::Flyout);
                }
            }
            (Role::Taskbar, _) => match under {
                // The wheel over the volume icon changes the volume.
                Some(Action::Taskbar(taskbar::Act::Status)) => self.media_key(if steps < 0 { "volume-up" } else { "volume-down" }),
                Some(Action::Taskbar(taskbar::Act::Tray(key))) => self.service(Command::TrayScroll { key, delta: steps, horizontal: false }),
                _ => {}
            },
            _ => {}
        }
    }

    /// Start's geometry and page as the open flyout shows them.
    pub(crate) fn start_page(&mut self) -> Option<(start::Geometry, start::Page)> {
        let surface = self.surfaces.iter().find(|s| s.role == Role::Flyout)?;
        let (w, h) = (surface.width as f32, surface.height as f32);
        let panel = ui::Rect::new(crate::PAD, crate::PAD, w - 2.0 * crate::PAD, h - 2.0 * crate::PAD);
        let geo = start::geometry(panel, self.st.desktop.start.layout);
        let page = start::page(&self.st, &geo, &mut self.res.text);
        Some((geo, page))
    }

    /// The wheel or the touchpad over Start.
    fn scroll_start(&mut self, discrete: i32, absolute: f64) {
        if self.st.start.searching() || self.st.start.index {
            return;
        }
        let Some((geo, page)) = self.start_page() else {
            return;
        };
        let max = page.max_scroll(geo.view.h);
        let now = Instant::now();
        let moved = if discrete != 0 {
            self.st.start.scroll_by(discrete as f32 * geo.m.cell_h, max, now, self.res.reduced_motion)
        } else {
            self.st.start.scroll_by(absolute as f32, max, now, true)
        };
        if moved {
            self.mark(Role::Flyout);
        }
    }

    /// A point of a surface in the coordinates of its output.
    fn on_output(&self, index: usize, position: (f32, f32)) -> (f32, f32) {
        let origin = self.surfaces[index].origin;
        (origin.0 + position.0, origin.1 + position.1)
    }

    /// The rectangle of a control of a surface, in output coordinates.
    fn control_rect(&self, index: usize, action: &Action) -> Option<ui::Rect> {
        let surface = self.surfaces.get(index)?;
        let hit = surface.hits.iter().rev().find(|h| &h.action == action)?;
        Some(ui::Rect::new(surface.origin.0 + hit.rect.x, surface.origin.1 + hit.rect.y, hit.rect.w, hit.rect.h))
    }

    // ---------------------------------------------------------------------
    // What controls do
    // ---------------------------------------------------------------------

    /// The secondary button: context menus.
    fn context(&mut self, action: Action, index: usize, position: (f32, f32)) {
        let output = self.surfaces[index].output.clone();
        let at = self.on_output(index, position);
        match action {
            Action::Taskbar(taskbar::Act::Task(task)) => {
                let list = self.st.tasks();
                if let (Some(entry), Some(rect)) = (list.get(task), self.control_rect(index, &Action::Taskbar(taskbar::Act::Task(task)))) {
                    let items = self.task_menu(entry);
                    self.close_flyout();
                    self.open_menu(items, rect.x, rect.y - 8.0, true, &output);
                }
            }
            Action::Taskbar(taskbar::Act::Tray(key)) => {
                // The menu opens when the item's program has sent it.
                self.close_menus();
                self.tray_menu = Some((key.clone(), at.0, self.surfaces[index].origin.1 - 4.0, output));
                self.service(Command::TrayMenu { key });
            }
            Action::Start(start::Act::App(app) | start::Act::Recommended(app) | start::Act::All(app)) => {
                let items = self.app_menu(app);
                self.open_menu(items, at.0, at.1, false, &output);
            }
            Action::Desktop => {
                self.close_transients();
                let items = self.desktop_menu();
                self.open_menu(items, at.0, at.1, false, &output);
            }
            // The taskbar's own background: its settings.
            Action::None if self.surfaces[index].role == Role::Taskbar => {
                self.close_transients();
                let items = self.taskbar_menu();
                let top = self.surfaces[index].origin.1 - 4.0;
                self.open_menu(items, at.0, top, true, &output);
            }
            _ => {}
        }
    }

    /// The middle button: a new instance, or a tray item's secondary action.
    fn secondary(&mut self, action: Action, index: usize, position: (f32, f32)) {
        match action {
            Action::Taskbar(taskbar::Act::Task(task)) => {
                let list = self.st.tasks();
                if let Some(app) = list.get(task).and_then(|t| t.app) {
                    self.launch_app(app);
                }
            }
            Action::Taskbar(taskbar::Act::Tray(key)) => {
                let (x, y) = self.on_output(index, position);
                self.service(Command::TraySecondaryActivate { key, x: x as i32, y: y as i32 });
            }
            _ => {}
        }
    }

    /// A control was clicked, or chosen with the keyboard.
    pub fn activate(&mut self, action: Action, index: usize) {
        let output = self.surfaces.get(index).map(|s| s.output.clone());
        match action {
            Action::None => {
                // A click on a flyout's own background closes its menus.
                if matches!(self.surfaces.get(index).map(|s| s.role), Some(Role::Flyout)) {
                    self.close_menus();
                }
            }
            Action::Dismiss if self.st.desk.renaming.is_some() => self.desk_commit_rename(),
            Action::Dismiss => {
                if self.st.menus.is_empty() {
                    self.close_flyout();
                } else {
                    self.close_menus();
                }
            }
            Action::Desktop => self.close_transients(),
            Action::Taskbar(act) => self.taskbar_action(act, index, output),
            Action::Preview(PreviewAct::Activate(id)) => {
                self.control_with("activate", serde_json::json!({ "window": id }));
                self.close_preview();
            }
            Action::Preview(PreviewAct::Close(id)) => {
                self.control_with("close", serde_json::json!({ "window": id }));
            }
            Action::Start(act) => self.start_action(act, index),
            Action::Quick(act) => self.quick_action(act),
            Action::Notify(act) => self.notify_action(act),
            Action::Menu { level, index: entry } => {
                let Some(item) = self.st.menus.get(level).and_then(|m| m.items.get(entry)).cloned() else {
                    return;
                };
                if item.children.is_empty() {
                    self.run_menu(item.cmd);
                } else {
                    self.open_submenu(level, entry);
                }
            }
            Action::Dialog(act) => match act {
                dialogs::Act::Primary => self.dialog_primary(),
                dialogs::Act::Cancel => self.dialog_cancel(),
                dialogs::Act::Field => {
                    self.st.focus = None;
                    self.mark(Role::Dialog);
                }
                dialogs::Act::Reveal => {
                    if let Some(Dialog::Auth(auth)) = self.st.dialog.as_mut() {
                        auth.reveal = !auth.reveal;
                    }
                    self.mark(Role::Dialog);
                }
                dialogs::Act::User(user) => self.dialog_choose_user(user),
            },
            Action::Capture(_) => {}
            Action::Lock(act) => self.lock_action(act, index),
            Action::Clip(act) => self.clip_action(act),
            Action::Emoji(act) => self.emoji_action(act),
            Action::Widgets(act) => self.widgets_action(act, Some(index)),
            Action::Desk(_) => {}
        }
    }

    fn taskbar_action(&mut self, act: taskbar::Act, index: usize, output: Option<wayland_client::protocol::wl_output::WlOutput>) {
        use taskbar::Act;
        match act {
            Act::Start => self.toggle_flyout(Flyout::Start, output),
            Act::Status => self.toggle_flyout(Flyout::Quick, output),
            Act::Clock => self.toggle_flyout(Flyout::Notifications, output),
            Act::TrayOverflow => self.toggle_flyout(Flyout::TrayOverflow, output),
            Act::Widgets => self.toggle_flyout(Flyout::Widgets, output),
            Act::TaskView => {
                self.close_transients();
                self.send_control("overview");
            }
            Act::ShowDesktop => {
                self.close_transients();
                self.send_control("show-desktop");
            }
            Act::Layout => {
                self.send_control("next-layout");
            }
            Act::StopCapture => self.stop_capturing(),
            Act::Task(task) => {
                self.close_transients();
                let list = self.st.tasks();
                let Some(entry) = list.get(task) else {
                    return;
                };
                match (taskbar::click_target(entry, &self.st.windows), entry.app) {
                    // One window: bring it up, or put it away when it is in front.
                    (Some(id), _) if entry.windows.len() == 1 => {
                        self.control_with("toggle-minimize", serde_json::json!({ "window": id }));
                    }
                    (Some(id), _) => {
                        self.control_with("activate", serde_json::json!({ "window": id }));
                    }
                    (None, Some(app)) => self.launch_app(app),
                    (None, None) => {}
                }
            }
            Act::Tray(key) => {
                let position = self.surfaces.get(index).and_then(|s| s.pointer).unwrap_or_default();
                let (x, y) = self.on_output(index, position);
                self.service(Command::TrayActivate { key, x: x as i32, y: y as i32 });
            }
        }
    }

    fn start_action(&mut self, act: start::Act, index: usize) {
        use start::Act;
        let output = self.surfaces.get(index).map(|s| s.output.clone());
        match act {
            Act::SearchBox => {
                self.st.focus = None;
                self.st.start.index = false;
            }
            Act::App(app) | Act::Recommended(app) | Act::All(app) => return self.launch_app(app),
            Act::ShowAll => {
                self.st.start.expanded = !self.st.start.expanded;
                self.clamp_start();
            }
            Act::View(view) => {
                self.st.config.set_view(view);
                self.st.start.index = false;
                self.clamp_start();
            }
            Act::Letter(_) => {
                self.st.start.index = true;
                self.st.focus = None;
            }
            Act::Jump(letter) => {
                self.st.start.index = false;
                let heading = Act::Letter(letter);
                if let Some((geo, page)) = self.start_page() {
                    if let Some(element) = page.find(&heading) {
                        let max = page.max_scroll(geo.view.h);
                        self.st.start.scroll_to(element.rect.y - 4.0, max, Instant::now(), self.res.reduced_motion);
                    }
                }
                self.st.focus = Some(Action::Start(heading));
            }
            Act::CloseIndex => self.st.start.index = false,
            Act::Result(position) => {
                if let Some(result) = self.st.start.results.get(position).cloned() {
                    return self.activate_result(result);
                }
            }
            Act::Folder(folder) => return self.open_folder(folder),
            Act::User | Act::Power => {
                let action = Action::Start(act.clone());
                if let (Some(rect), Some(output)) = (self.control_rect(index, &action), output) {
                    let items = if act == Act::User { self.user_menu() } else { self.power_menu() };
                    let x = if act == Act::User { rect.x } else { rect.right() - 180.0 };
                    self.open_menu(items, x, rect.y - 8.0, true, &output);
                }
            }
        }
        self.mark(Role::Flyout);
    }

    fn quick_action(&mut self, act: quick::Act) {
        use quick::Act;
        let sys = &self.st.sys;
        match act {
            Act::WifiToggle => {
                let on = !sys.network.wifi_enabled;
                self.service(Command::WifiSetEnabled(on));
            }
            Act::BluetoothToggle => {
                let on = !sys.bluetooth.enabled;
                self.service(Command::BluetoothSetEnabled(on));
            }
            Act::Airplane => {
                let on = !sys.network.airplane;
                self.service(Command::AirplaneMode(on));
            }
            Act::NightLight => {
                let enabled = !self.st.night_light;
                self.control_with("night-light", serde_json::json!({ "enabled": enabled }));
            }
            Act::Profile => {
                if let Some(next) = sys.power.profile.and_then(|p| svc::next_profile(p, &sys.power.profiles)) {
                    self.service(Command::SetPowerProfile(next));
                }
            }
            Act::Dnd => self.set_dnd(!self.st.notes.dnd),
            Act::Mute => {
                if let Some(device) = self.st.sys.audio.output.as_mut() {
                    device.muted = !device.muted;
                    let muted = device.muted;
                    self.service(Command::SetMuted(muted));
                    self.mark(Role::Taskbar);
                }
            }
            // The sliders act while they are dragged; a plain click sets
            // the value under the pointer, which the drag already did.
            Act::Volume | Act::Brightness => {}
            Act::WifiPage => {
                self.st.quick.open_page(Page::Wifi);
                self.service(Command::WifiScan);
            }
            Act::BluetoothPage => {
                self.st.quick.open_page(Page::Bluetooth);
                self.service(Command::BluetoothDiscover(true));
            }
            Act::OutputsPage => self.st.quick.open_page(Page::Outputs),
            Act::Back => {
                if self.st.quick.page == Page::Bluetooth {
                    self.service(Command::BluetoothDiscover(false));
                }
                self.st.quick.open_page(Page::Main);
            }
            Act::Settings(page) => {
                let argv = if page.is_empty() { vec!["boswas-control-center".to_owned()] } else { start::settings_command(page) };
                self.spawn(&argv);
                return self.close_transients();
            }
            Act::MediaPrevious(i) | Act::MediaPlayPause(i) | Act::MediaNext(i) => {
                if let Some(key) = sys.media.get(i).map(|p| p.key.clone()) {
                    self.service(match act {
                        Act::MediaPrevious(_) => Command::MediaPrevious { key },
                        Act::MediaNext(_) => Command::MediaNext { key },
                        _ => Command::MediaPlayPause { key },
                    });
                }
            }
            Act::Network(i) => {
                if let Some(ap) = sys.network.access_points.get(i).cloned() {
                    self.st.quick.select_network(&ap);
                    self.st.sys.network.error = None;
                }
            }
            Act::PassphraseField => self.st.quick.editing = true,
            Act::Reveal => self.st.quick.reveal = !self.st.quick.reveal,
            Act::Cancel => self.st.quick.collapse(),
            Act::Connect => self.connect_selected_network(),
            Act::Disconnect => {
                self.service(Command::WifiDisconnect);
                self.st.quick.collapse();
            }
            Act::Forget => {
                if let Some(ssid) = self.st.quick.network.clone() {
                    self.service(Command::WifiForget { ssid });
                }
                self.st.quick.collapse();
            }
            Act::Scan => self.service(Command::WifiScan),
            Act::Device(i) => {
                if let Some(address) = sys.bluetooth.devices.get(i).map(|d| d.address.clone()) {
                    let same = self.st.quick.device.as_deref() == Some(address.as_str());
                    self.st.quick.collapse();
                    if !same {
                        self.st.quick.device = Some(address);
                    }
                }
            }
            Act::DeviceConnect | Act::DeviceDisconnect | Act::DeviceForget => {
                if let Some(address) = self.st.quick.device.clone() {
                    self.service(match act {
                        Act::DeviceConnect => Command::BluetoothConnect { address },
                        Act::DeviceDisconnect => Command::BluetoothDisconnect { address },
                        _ => Command::BluetoothForget { address },
                    });
                    if act == Act::DeviceForget {
                        self.st.quick.collapse();
                    }
                }
            }
            Act::Output(id) => self.service(Command::SetDefaultOutput { id }),
        }
        self.st.focus = None;
        self.sync_flyout();
    }

    fn connect_selected_network(&mut self) {
        let Some(ssid) = self.st.quick.network.clone() else {
            return;
        };
        let Some(ap) = self.st.sys.network.access_points.iter().find(|ap| ap.ssid == ssid) else {
            return;
        };
        let passphrase = if quick::needs_passphrase(ap) {
            if self.st.quick.passphrase.len() < 8 {
                return;
            }
            let text = std::mem::take(&mut *self.st.quick.passphrase.take());
            Some(boswas_services::Secret::new(text))
        } else {
            None
        };
        self.st.quick.editing = false;
        self.st.quick.reveal = false;
        self.service(Command::WifiConnect { ssid, passphrase });
    }

    pub(crate) fn set_dnd(&mut self, on: bool) {
        self.st.notes.dnd = on;
        self.service(Command::SetDoNotDisturb(on));
        self.mark(Role::Taskbar);
        self.mark(Role::Flyout);
    }

    fn notify_action(&mut self, act: notify::Act) {
        use notify::Act;
        let now = Instant::now();
        let reduced = self.res.reduced_motion;
        match act {
            Act::ToastBody(id) | Act::Body(id) => {
                if !self.open_note(id, "default") {
                    self.service(Command::NotificationAction { id, action: "default".into() });
                }
                self.st.notes.dismiss(id, now, reduced);
                if matches!(act, Act::Body(_)) {
                    self.close_transients();
                }
            }
            Act::ToastAction(id, key) | Act::Invoke(id, key) => {
                if !self.open_note(id, &key) {
                    self.service(Command::NotificationAction { id, action: key });
                }
                self.st.notes.dismiss(id, now, reduced);
            }
            // Closing a toast keeps the notification in the centre.
            Act::ToastClose(id) => self.st.notes.leave(id, now, reduced),
            Act::Dismiss(id) => {
                self.service(Command::NotificationDismiss { id });
                self.st.notes.dismiss(id, now, reduced);
                self.shell_notes.remove(&id);
            }
            Act::DismissApp(app) => {
                for id in self.st.notes.ids_of(&app) {
                    self.service(Command::NotificationDismiss { id });
                    self.st.notes.dismiss(id, now, reduced);
                }
            }
            Act::ClearAll => {
                let ids: Vec<u32> = self.st.notes.history.iter().map(|n| n.id).collect();
                for id in ids {
                    self.service(Command::NotificationDismiss { id });
                    self.st.notes.dismiss(id, now, reduced);
                }
                self.st.notes.scroll = 0;
            }
            Act::ToggleGroup(app) => {
                if let Some(position) = self.st.notes.expanded.iter().position(|a| *a == app) {
                    self.st.notes.expanded.remove(position);
                } else {
                    self.st.notes.expanded.push(app);
                }
            }
            Act::Dnd => return self.set_dnd(!self.st.notes.dnd),
            Act::PreviousMonth => self.st.notes.month = (self.st.notes.month - 1).max(-1200),
            Act::NextMonth => self.st.notes.month = (self.st.notes.month + 1).min(1200),
            Act::Today => self.st.notes.month = 0,
            Act::Settings => {
                self.spawn(&start::settings_command("notifications"));
                return self.close_transients();
            }
        }
        self.st.notes.scroll = self.st.notes.scroll.min(self.st.notes.history.len().saturating_sub(1));
        self.sync_toasts();
        self.mark(Role::Toasts);
        self.mark(Role::Flyout);
        self.mark(Role::Taskbar);
    }

    fn lock_action(&mut self, act: lock::Act, index: usize) {
        use lock::Act;
        match act {
            Act::Wake => self.st.auth.stage = Stage::SignIn,
            Act::Submit => return self.submit(),
            Act::FocusUser => self.st.auth.editing_user = self.st.auth.user_field.is_some(),
            Act::FocusPassword => self.st.auth.editing_user = false,
            Act::Reveal => self.st.auth.reveal = !self.st.auth.reveal,
            Act::User(user) => self.st.auth.select(user),
            Act::OtherUser => self.st.auth.other_user(),
            Act::Power => {
                let action = Action::Lock(Act::Power);
                if let (Some(rect), Some(output)) = (self.control_rect(index, &action), self.surfaces.get(index).map(|s| s.output.clone())) {
                    let items = self.power_menu();
                    self.open_menu(items, rect.right() - 180.0, rect.y - 8.0, true, &output);
                }
            }
        }
        self.st.focus = None;
        self.mark(Role::Auth);
    }

    // ---------------------------------------------------------------------
    // Keyboard
    // ---------------------------------------------------------------------

    /// Moves the keyboard focus on the surface of `role`.
    fn navigate(&mut self, role: Role, nav: Nav) -> bool {
        let Some(surface) = self.surfaces.iter().find(|s| s.role == role) else {
            return false;
        };
        match ui::navigate(&surface.hits, self.st.focus.as_ref(), nav) {
            Some(action) => {
                self.st.focus = Some(action);
                self.mark(role);
                true
            }
            None => false,
        }
    }

    /// Activates the focused control of the surface of `role`.
    fn activate_focus(&mut self, role: Role) -> bool {
        let (Some(action), Some(index)) = (self.st.focus.clone(), self.surfaces.iter().position(|s| s.role == role)) else {
            return false;
        };
        if !self.surfaces[index].hits.iter().any(|h| h.action == action) {
            return false;
        }
        self.activate(action, index);
        true
    }

    fn nav_key(keysym: Keysym, shift: bool) -> Option<Nav> {
        match keysym {
            Keysym::Tab if shift => Some(Nav::Previous),
            Keysym::ISO_Left_Tab => Some(Nav::Previous),
            Keysym::Tab => Some(Nav::Next),
            Keysym::Left => Some(Nav::Left),
            Keysym::Right => Some(Nav::Right),
            Keysym::Up => Some(Nav::Up),
            Keysym::Down => Some(Nav::Down),
            _ => None,
        }
    }

    pub fn key_pressed(&mut self, event: KeyEvent) {
        let ctrl = self.modifiers.ctrl;
        let shift = self.modifiers.shift;
        let typed = event.utf8.as_deref().filter(|_| !ctrl && !self.modifiers.alt && !self.modifiers.logo);
        if self.st.mode != Mode::Desktop {
            return self.auth_key(&event, typed);
        }
        if self.st.capture.is_some() {
            match event.keysym {
                Keysym::Escape => self.cancel_capture(),
                Keysym::Return | Keysym::KP_Enter | Keysym::space => self.finish_capture(),
                _ => {}
            }
            return;
        }
        if self.st.dialog.is_some() {
            return self.dialog_key(&event, typed);
        }
        if self.st.desk.renaming.is_some() {
            return self.desk_rename_key(&event, typed);
        }
        if !self.st.menus.is_empty() {
            return self.menu_key(event.keysym);
        }
        match self.open_kind() {
            Some(Flyout::Start) => self.start_key(&event, typed),
            Some(Flyout::Quick) => self.quick_key(&event, typed),
            Some(Flyout::Clipboard) => self.clip_key(&event, typed),
            Some(Flyout::Emoji) => self.emoji_key(&event, typed),
            Some(Flyout::Widgets) => self.widgets_key(&event),
            Some(Flyout::Notifications | Flyout::TrayOverflow) => match event.keysym {
                Keysym::Escape => self.close_flyout(),
                Keysym::Return | Keysym::KP_Enter | Keysym::space => {
                    self.activate_focus(Role::Flyout);
                }
                Keysym::Delete => {
                    // Delete clears the focused notification.
                    if let Some(Action::Notify(notify::Act::Body(id))) = self.st.focus.clone() {
                        self.notify_action(notify::Act::Dismiss(id));
                    }
                }
                keysym => {
                    if let Some(nav) = Self::nav_key(keysym, shift) {
                        self.navigate(Role::Flyout, nav);
                    }
                }
            },
            // Nothing of the shell's is open: the desktop has the keyboard.
            None => self.desk_key(&event, typed),
        }
    }

    /// Keeps Start's page within its end after it became shorter.
    fn clamp_start(&mut self) {
        if let Some((geo, page)) = self.start_page() {
            self.st.start.clamp(page.max_scroll(geo.view.h));
        }
    }

    /// Moves the keyboard focus through Start: the whole page (scrolling
    /// to what it reaches), the buttons at the bottom, or the index of
    /// letters while it is open.
    fn start_navigate(&mut self, nav: Nav) {
        let Some(index) = self.surfaces.iter().position(|s| s.role == Role::Flyout) else {
            return;
        };
        let pick = |hits: &[Hit], wanted: fn(&start::Act) -> bool| -> Vec<Hit> {
            hits.iter().filter(|h| matches!(&h.action, Action::Start(act) if wanted(act))).cloned().collect()
        };
        if self.st.start.index {
            let letters = pick(&self.surfaces[index].hits, |act| matches!(act, start::Act::Jump(_)));
            if let Some(action) = ui::navigate(&letters, self.st.focus.as_ref(), nav) {
                self.st.focus = Some(action);
                self.mark(Role::Flyout);
            }
            return;
        }
        let footer = pick(&self.surfaces[index].hits, start::Act::in_footer);
        let Some((geo, page)) = self.start_page() else {
            return;
        };
        let next = start::navigate(&page, &footer, self.st.focus.as_ref(), nav);
        if let Some(element) = next.as_ref().and_then(|action| match action {
            Action::Start(act) => page.find(act),
            _ => None,
        }) {
            let max = page.max_scroll(geo.view.h);
            self.st.start.reveal(element.rect, geo.view.h, max, Instant::now(), self.res.reduced_motion);
        }
        self.st.focus = next;
        self.mark(Role::Flyout);
    }

    /// Page Up and Page Down scroll Start's page by a window's height,
    /// Home and End go to its ends.
    fn start_page_key(&mut self, keysym: Keysym) {
        let Some((geo, page)) = self.start_page() else {
            return;
        };
        let max = page.max_scroll(geo.view.h);
        let step = (geo.view.h - geo.m.header_h).max(geo.m.cell_h);
        let target = match keysym {
            Keysym::Page_Up | Keysym::KP_Page_Up => self.st.start.scroll - step,
            Keysym::Page_Down | Keysym::KP_Page_Down => self.st.start.scroll + step,
            Keysym::Home | Keysym::KP_Home => 0.0,
            _ => max,
        };
        if self.st.start.scroll_to(target, max, Instant::now(), self.res.reduced_motion) {
            self.mark(Role::Flyout);
        }
    }

    fn start_key(&mut self, event: &KeyEvent, typed: Option<&str>) {
        let shift = self.modifiers.shift;
        let outcome = self.st.start.key(event.keysym, self.modifiers.ctrl, &self.st.apps, &self.st.usage.counts);
        match outcome {
            KeyOutcome::Close => return self.close_flyout(),
            KeyOutcome::Activate(result) => return self.activate_result(result),
            KeyOutcome::Redraw => {
                self.st.focus = None;
                return self.mark(Role::Flyout);
            }
            KeyOutcome::Ignored => {}
        }
        // Not a search key: the page and the buttons take it.
        match event.keysym {
            Keysym::Return | Keysym::KP_Enter => {
                // The focused control may be scrolled to and not drawn yet.
                if let Some(Action::Start(act)) = self.st.focus.clone() {
                    if let Some(index) = self.surfaces.iter().position(|s| s.role == Role::Flyout) {
                        self.start_action(act, index);
                    }
                }
            }
            Keysym::Page_Up | Keysym::Page_Down | Keysym::KP_Page_Up | Keysym::KP_Page_Down | Keysym::Home | Keysym::End | Keysym::KP_Home | Keysym::KP_End => {
                self.start_page_key(event.keysym);
            }
            keysym => {
                if let Some(nav) = Self::nav_key(keysym, shift) {
                    self.start_navigate(nav);
                } else if let Some(text) = typed {
                    if self.st.start.typed(text, &self.st.apps, &self.st.usage.counts) == KeyOutcome::Redraw {
                        self.st.focus = None;
                        self.mark(Role::Flyout);
                    }
                }
            }
        }
    }

    fn quick_key(&mut self, event: &KeyEvent, typed: Option<&str>) {
        let shift = self.modifiers.shift;
        if self.st.quick.editing {
            match event.keysym {
                Keysym::Escape => self.st.quick.collapse(),
                Keysym::Return | Keysym::KP_Enter => self.connect_selected_network(),
                Keysym::Tab | Keysym::ISO_Left_Tab => {
                    self.st.quick.editing = false;
                    self.navigate(Role::Flyout, Nav::Next);
                }
                keysym => {
                    if !self.st.quick.passphrase.key(keysym, self.modifiers.ctrl) {
                        if let Some(text) = typed {
                            self.st.quick.passphrase.insert(text);
                        }
                    }
                }
            }
            return self.sync_flyout();
        }
        match event.keysym {
            Keysym::Escape => {
                if self.st.quick.page == Page::Main {
                    self.close_flyout();
                } else {
                    self.quick_action(quick::Act::Back);
                }
            }
            Keysym::Return | Keysym::KP_Enter | Keysym::space => {
                self.activate_focus(Role::Flyout);
            }
            // The arrows move a focused slider.
            Keysym::Left | Keysym::Right if matches!(self.st.focus, Some(Action::Quick(quick::Act::Volume | quick::Act::Brightness))) => {
                let up = event.keysym == Keysym::Right;
                if self.st.focus == Some(Action::Quick(quick::Act::Volume)) {
                    if let Some(volume) = self.st.sys.audio.output.as_ref().map(|o| o.volume) {
                        self.slide(Command::SetVolume(crate::osd::step_volume(volume, up)));
                    }
                } else if let Some(brightness) = self.st.sys.brightness {
                    self.slide(Command::SetBrightness(crate::osd::step_brightness(brightness.level, up)));
                }
            }
            keysym => {
                if let Some(nav) = Self::nav_key(keysym, shift) {
                    self.navigate(Role::Flyout, nav);
                }
            }
        }
    }

    fn menu_key(&mut self, keysym: Keysym) {
        let level = self.st.menus.len() - 1;
        match keysym {
            Keysym::Escape => return self.close_menus(),
            Keysym::Down | Keysym::Tab => self.st.menus[level].step(true),
            Keysym::Up | Keysym::ISO_Left_Tab => self.st.menus[level].step(false),
            Keysym::Left => {
                if level > 0 {
                    self.st.menus.pop();
                    self.remove(Role::Menu(level));
                }
            }
            Keysym::Right | Keysym::Return | Keysym::KP_Enter | Keysym::space => {
                let Some(selected) = self.st.menus[level].selected else {
                    return;
                };
                let has_children = !self.st.menus[level].items[selected].children.is_empty();
                if has_children {
                    self.open_submenu(level, selected);
                } else if keysym != Keysym::Right {
                    let index = self.surfaces.iter().position(|s| s.role == Role::Menu(level)).unwrap_or(0);
                    self.activate(Action::Menu { level, index: selected }, index);
                }
                return;
            }
            _ => return,
        }
        for surface in self.surfaces.iter_mut().filter(|s| matches!(s.role, Role::Menu(_))) {
            surface.touch();
        }
    }

    fn dialog_key(&mut self, event: &KeyEvent, typed: Option<&str>) {
        let shift = self.modifiers.shift;
        let ctrl = self.modifiers.ctrl;
        match event.keysym {
            Keysym::Escape => return self.dialog_cancel(),
            Keysym::Return | Keysym::KP_Enter => {
                // Enter on a focused button presses that button. Without
                // one it is the dialog's default, except where there is
                // none: nobody allows a screen capture by a stray Enter.
                if !self.activate_focus(Role::Dialog) && !matches!(self.st.dialog, Some(Dialog::Screen { .. })) {
                    self.dialog_primary();
                }
                return;
            }
            Keysym::Tab | Keysym::ISO_Left_Tab => {
                let nav = if shift || event.keysym == Keysym::ISO_Left_Tab { Nav::Previous } else { Nav::Next };
                self.navigate(Role::Dialog, nav);
                return;
            }
            Keysym::Up | Keysym::Down => {
                if let Some(Dialog::Run(run)) = self.st.dialog.as_mut() {
                    run.browse(&self.st.run_history.entries, event.keysym == Keysym::Up);
                    run.error = None;
                }
                return self.mark(Role::Dialog);
            }
            _ => {}
        }
        let Some(dialog) = self.st.dialog.as_mut() else {
            return;
        };
        if let Dialog::Run(run) = dialog {
            run.error = None;
        }
        if let Some(field) = dialog.field() {
            if !field.key(event.keysym, ctrl) {
                if let Some(text) = typed {
                    field.insert(text);
                }
            }
            self.st.focus = None;
            self.mark(Role::Dialog);
        }
    }

    /// Keys on the lock screen and the greeter.
    fn auth_key(&mut self, event: &KeyEvent, typed: Option<&str>) {
        if !self.st.menus.is_empty() {
            return self.menu_key(event.keysym);
        }
        if self.st.auth.state == AuthState::Checking {
            return;
        }
        if self.st.auth.stage == Stage::Clock {
            // Any key shows the sign-in view; the key itself is not typed.
            self.st.auth.stage = Stage::SignIn;
            return self.mark(Role::Auth);
        }
        match event.keysym {
            Keysym::Return | Keysym::KP_Enter => {
                if !self.activate_focus(Role::Auth) {
                    self.submit();
                }
                return;
            }
            Keysym::Escape => {
                if self.st.auth.field().is_empty() {
                    self.st.auth.sleep();
                } else {
                    self.st.auth.field().clear();
                }
            }
            Keysym::Tab | Keysym::ISO_Left_Tab => {
                if self.st.auth.user_field.is_some() && self.st.focus.is_none() {
                    self.st.auth.editing_user = !self.st.auth.editing_user;
                } else {
                    let nav = if self.modifiers.shift || event.keysym == Keysym::ISO_Left_Tab { Nav::Previous } else { Nav::Next };
                    self.navigate(Role::Auth, nav);
                    return;
                }
            }
            keysym => {
                let ctrl = self.modifiers.ctrl;
                if !self.st.auth.field().key(keysym, ctrl) {
                    match typed {
                        Some(text) => {
                            self.st.auth.field().insert(text);
                        }
                        None => return,
                    }
                }
                self.st.focus = None;
            }
        }
        if matches!(self.st.auth.state, AuthState::Failed(_)) {
            self.st.auth.state = AuthState::Idle;
        }
        self.mark(Role::Auth);
    }
}
