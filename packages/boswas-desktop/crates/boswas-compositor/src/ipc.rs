//! The control socket.
//!
//! One JSON object per line in each direction. Requests carry `cmd` and an
//! optional `id`, which the reply echoes:
//!
//! ```text
//! -> {"id": 1, "cmd": "windows"}
//! <- {"id": 1, "ok": true, "data": [...]}
//! <- {"event": "windows", "data": [...]}        (after "subscribe")
//! ```
//!
//! Connections are either trusted (the descriptor the compositor handed to a
//! program it started itself: the shell, the lock screen) or public (the
//! socket file in `XDG_RUNTIME_DIR`). Public connections can ask for the
//! version, lock the session, have the configuration re-read, list the
//! outputs and start the interactive capture (which the user completes or
//! cancels on screen), nothing else: the window list, window control and
//! silent screenshots are for the shell only.

use std::{
    io::{ErrorKind, Read, Write},
    os::unix::net::{UnixListener, UnixStream},
    path::PathBuf,
};

use serde_json::{json, Value};
use smithay::{
    backend::input::{ButtonState, KeyState},
    reexports::calloop::{generic::Generic, Interest, Mode, PostAction},
    utils::Point,
};
use tracing::{debug, warn};

use crate::{shell::element::Tile, state::Boswas};

/// Longest request line accepted; a longer one closes the connection.
const MAX_LINE: usize = 64 * 1024;
/// Most simultaneous control connections.
const MAX_CONNECTIONS: usize = 32;

struct Connection {
    id: u64,
    /// Write half (a clone of the stream registered with the event loop).
    stream: UnixStream,
    buffer: Vec<u8>,
    trusted: bool,
    subscribed: bool,
}

/// What a capture request wants a picture of.
pub enum CaptureTarget {
    /// An output by name, or the first one.
    Output(Option<String>),
    /// What an output shows the person in front of it: with the magnifier
    /// and the colour filter (for tests; `"display": true`).
    Display(Option<String>),
    /// A window's current content, scaled down to `width` pixels.
    Window { id: u64, width: u32 },
}

/// A screenshot or thumbnail waiting for the next frame.
pub struct ScreenshotRequest {
    pub connection: u64,
    pub request_id: Value,
    pub path: PathBuf,
    pub target: CaptureTarget,
}

#[derive(Default)]
pub struct Ipc {
    connections: Vec<Connection>,
    next_id: u64,
    /// The shell's trusted connection; when it closes the shell is gone.
    pub shell: Option<u64>,
    /// Whether the input-injection commands are enabled (`--test-input`).
    pub test_input: bool,
    /// Set when the window list changed; one event is sent per loop turn.
    windows_dirty: bool,
    pub screenshots: Vec<ScreenshotRequest>,
    pub socket_path: Option<PathBuf>,
}

impl Ipc {
    pub fn has_shell(&self) -> bool {
        self.shell.is_some_and(|id| self.is_open(id))
    }

    /// Notes that the window list changed (see [`Boswas::windows_changed`]).
    pub fn mark_windows_dirty(&mut self) {
        self.windows_dirty = true;
    }

    /// Whether a control connection is still open. The program a trusted
    /// connection was handed to holds it for as long as it lives.
    pub fn is_open(&self, id: u64) -> bool {
        self.connections.iter().any(|c| c.id == id)
    }
}

impl Boswas {
    /// Opens the public control socket next to the Wayland socket.
    pub fn ipc_listen(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let runtime = std::env::var_os("XDG_RUNTIME_DIR").ok_or("XDG_RUNTIME_DIR is not set")?;
        let path = PathBuf::from(runtime).join(format!("boswas-{}.sock", self.socket_name.to_string_lossy()));
        // A stale socket of a previous compositor on the same display name.
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path)?;
        listener.set_nonblocking(true)?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        self.loop_handle
            .insert_source(Generic::new(listener, Interest::READ, Mode::Level), |_, listener, state| {
                loop {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            let trusted = state.config.trust_all_clients();
                            state.ipc_accept(stream, trusted);
                        }
                        Err(err) if err.kind() == ErrorKind::WouldBlock => break,
                        Err(err) => {
                            warn!("control socket accept failed: {err}");
                            break;
                        }
                    }
                }
                Ok(PostAction::Continue)
            })?;
        self.ipc.socket_path = Some(path);
        Ok(())
    }

    /// Adds a control connection. Returns its id, or `None` if it could not
    /// be set up.
    pub fn ipc_accept(&mut self, stream: UnixStream, trusted: bool) -> Option<u64> {
        if self.ipc.connections.len() >= MAX_CONNECTIONS {
            warn!("too many control connections; refusing another");
            return None;
        }
        stream.set_nonblocking(true).ok()?;
        let writer = stream.try_clone().ok()?;
        let id = self.ipc.next_id;
        self.ipc.next_id += 1;
        let source = Generic::new(stream, Interest::READ, Mode::Level);
        let inserted = self.loop_handle.insert_source(source, move |_, stream, state| {
            // SAFETY: the stream is only read here and is not dropped or
            // replaced while the source is registered.
            let stream = unsafe { stream.get_mut() };
            Ok(if state.ipc_readable(id, stream) {
                PostAction::Continue
            } else {
                state.ipc.connections.retain(|c| c.id != id);
                PostAction::Remove
            })
        });
        if let Err(err) = inserted {
            warn!("could not watch a control connection: {err}");
            return None;
        }
        self.ipc.connections.push(Connection {
            id,
            stream: writer,
            buffer: Vec::new(),
            trusted,
            subscribed: false,
        });
        Some(id)
    }

    /// Reads what is available on a connection. Returns `false` when the
    /// connection is finished.
    fn ipc_readable(&mut self, id: u64, stream: &mut UnixStream) -> bool {
        let mut chunk = [0u8; 4096];
        // The peer may send a request and close at once (the login screen
        // says "quit" and exits): what it sent before closing still counts.
        let mut open = true;
        loop {
            match stream.read(&mut chunk) {
                Ok(0) => {
                    open = false;
                    break;
                }
                Ok(n) => {
                    let Some(connection) = self.ipc.connections.iter_mut().find(|c| c.id == id) else {
                        return false;
                    };
                    connection.buffer.extend_from_slice(&chunk[..n]);
                    if connection.buffer.len() > MAX_LINE && !connection.buffer.contains(&b'\n') {
                        warn!("control connection sent an oversized request; closing it");
                        return false;
                    }
                }
                Err(err) if err.kind() == ErrorKind::WouldBlock => break,
                Err(err) if err.kind() == ErrorKind::Interrupted => continue,
                Err(_) => return false,
            }
        }
        loop {
            let Some(connection) = self.ipc.connections.iter_mut().find(|c| c.id == id) else {
                return false;
            };
            let Some(end) = connection.buffer.iter().position(|b| *b == b'\n') else {
                return open;
            };
            let line: Vec<u8> = connection.buffer.drain(..=end).collect();
            let trusted = connection.trusted;
            let reply = match std::str::from_utf8(&line[..end]) {
                Ok(text) if !text.trim().is_empty() => self.ipc_request(id, trusted, text),
                Ok(_) => continue,
                Err(_) => Some(json!({ "ok": false, "error": "request is not UTF-8" })),
            };
            if let Some(reply) = reply {
                if !self.ipc_send(id, &reply) {
                    return false;
                }
            }
        }
    }

    /// Writes one message to a connection. A peer that does not read is
    /// disconnected rather than buffered for.
    pub fn ipc_send(&mut self, id: u64, message: &Value) -> bool {
        let Some(connection) = self.ipc.connections.iter_mut().find(|c| c.id == id) else {
            return false;
        };
        let mut line = message.to_string();
        line.push('\n');
        match connection.stream.write_all(line.as_bytes()) {
            Ok(()) => true,
            Err(err) => {
                debug!("control connection {id} dropped: {err}");
                let _ = connection.stream.shutdown(std::net::Shutdown::Both);
                false
            }
        }
    }

    /// Sends an event to every subscribed, trusted connection.
    pub fn ipc_event(&mut self, name: &str, data: Value) {
        let message = json!({ "event": name, "data": data });
        let targets: Vec<u64> = self
            .ipc
            .connections
            .iter()
            .filter(|c| c.subscribed && c.trusted)
            .map(|c| c.id)
            .collect();
        for id in targets {
            self.ipc_send(id, &message);
        }
    }

    /// Whether a shell is listening for events (and so can ask the user
    /// something).
    pub fn ipc_has_subscriber(&self) -> bool {
        self.ipc.connections.iter().any(|c| c.subscribed && c.trusted)
    }

    /// Notes that the window list changed; the event goes out once per
    /// event-loop turn.
    pub fn windows_changed(&mut self) {
        self.ipc.windows_dirty = true;
        self.schedule_redraw();
    }

    /// Sends the coalesced window-list event.
    pub fn ipc_flush(&mut self) {
        if std::mem::take(&mut self.ipc.windows_dirty) {
            let windows = self.windows_json();
            self.ipc_event("windows", windows);
        }
    }

    pub fn windows_json(&self) -> Value {
        let focused = self.focused_window().map(|w| w.id());
        Value::Array(
            self.windows
                .iter()
                .filter(|w| w.data().mapped.get() && !w.is_override_redirect())
                .map(|window| {
                    let data = window.data();
                    let size = window.frame().size;
                    let location = data.location.get();
                    let tile = data.tile.get();
                    let output = self
                        .output_for_rect(smithay::utils::Rectangle::new(location, size))
                        .map(|o| o.name())
                        .unwrap_or_default();
                    json!({
                        "id": data.id,
                        "title": window.title(),
                        "app_id": window.app_id(),
                        "active": focused == Some(data.id),
                        "minimized": data.minimized.get(),
                        "tile": tile.name(),
                        "maximized": tile == Tile::Maximized,
                        "fullscreen": tile == Tile::Fullscreen,
                        "urgent": data.urgent.get(),
                        "x11": window.is_x11(),
                        "workspace": data.workspace.get(),
                        "output": output,
                        "x": location.x,
                        "y": location.y,
                        "width": size.w,
                        "height": size.h,
                    })
                })
                .collect(),
        )
    }

    /// What the shell needs to match the session's look, and the settings
    /// of its taskbar, Start, notifications and desktop icons (they are kept in the same
    /// layered files as everything else, and reach the shell this way).
    /// The lock screen is started as a trusted client too and takes from
    /// it whether to show the weather.
    pub fn appearance_json(&self) -> Value {
        let c = &self.config;
        json!({
            "scheme": c.appearance.scheme,
            "wallpaper": c.appearance.wallpaper,
            "reduced_motion": c.appearance.reduced_motion,
            "accent": c.appearance.accent,
            "transparency": c.appearance.transparency,
            "high_contrast": self.glass.high_contrast,
            "text_scale": c.accessibility.text_scale,
            // For the programs the shell starts (XCURSOR_SIZE).
            "cursor_size": c.pointer.cursor_size,
            "taskbar": {
                "alignment": c.taskbar.alignment,
                "auto_hide": c.taskbar.auto_hide,
                "task_view": c.taskbar.task_view,
                "clock_seconds": c.taskbar.clock_seconds,
                "badges": c.taskbar.badges,
                "windows": c.multitasking.taskbar_windows,
                "widgets": c.taskbar.widgets,
            },
            "desktop": {
                "icons": c.desktop.icons,
                "system_icons": c.desktop.system_icons,
                "icon_size": c.desktop.icon_size,
                "auto_arrange": c.desktop.auto_arrange,
                "align_to_grid": c.desktop.align_to_grid,
            },
            "start": {
                "layout": c.start.layout,
                "recently_added": c.start.recently_added,
                "most_used": c.start.most_used,
                "folders": c.start.folders,
            },
            "notifications": {
                "do_not_disturb": c.notifications.do_not_disturb,
                "scheduled": c.notifications.do_not_disturb_scheduled,
                "from": c.notifications.do_not_disturb_from,
                "to": c.notifications.do_not_disturb_to,
                "banners_off": c.notifications.banners_off,
                "toast_seconds": c.notifications.toast_seconds,
            },
            "lock": {
                "show_weather": c.lock.show_weather,
            },
            "clipboard": {
                "history": c.clipboard.history,
                "cleared_at": c.clipboard.cleared_at,
            },
        })
    }

    pub fn workspaces_json(&self) -> Value {
        json!({ "active": self.workspace, "count": self.workspace_count })
    }

    /// The keyboard layouts of the session and which one is active.
    pub fn layout_json(&self) -> Value {
        let codes: Vec<&str> = self.config.keyboard.layout.split(',').map(str::trim).collect();
        json!({ "layouts": self.layouts.0, "codes": codes, "active": self.layouts.1 })
    }

    pub fn night_light_json(&self) -> Value {
        let (enabled, temperature) = self.night_light;
        json!({
            "enabled": enabled,
            "temperature": temperature,
            "active": self.night_light_active(),
            // Turned on or off now, against the schedule.
            "override": self.winkeys.night_override.is_some(),
            // Some monitor can be tinted (it has a gamma ramp).
            "available": self.backend.night_light_available(),
        })
    }

    /// The outputs of the desktop. Outputs that are connected but turned
    /// off are listed too (`enabled: false`), so Settings can turn them on.
    /// `preferred` marks the mode used without a `mode` setting: the
    /// monitor's native size at its highest refresh rate.
    pub fn outputs_json(&self) -> Value {
        let mut outputs: Vec<Value> = self
            .space
            .outputs()
            .map(|output| {
                let geo = self.space.output_geometry(output).unwrap_or_default();
                let usable = self.usable_area(output);
                let current = output.current_mode();
                let preferred = output.preferred_mode();
                let physical = output.physical_properties();
                let mut modes: Vec<_> = output.modes();
                modes.sort_by_key(|m| (-(m.size.w * m.size.h), -m.refresh));
                modes.dedup();
                let rotation = match output.current_transform() {
                    smithay::utils::Transform::_90 | smithay::utils::Transform::Flipped90 => "90",
                    smithay::utils::Transform::_180 | smithay::utils::Transform::Flipped180 => "180",
                    smithay::utils::Transform::_270 | smithay::utils::Transform::Flipped270 => "270",
                    _ => "normal",
                };
                json!({
                    "name": output.name(),
                    "description": output.description(),
                    "make": physical.make,
                    "model": physical.model,
                    "physical_width_mm": physical.size.w,
                    "physical_height_mm": physical.size.h,
                    "enabled": true,
                    "x": geo.loc.x,
                    "y": geo.loc.y,
                    "width": geo.size.w,
                    "height": geo.size.h,
                    "scale": output.current_scale().fractional_scale(),
                    // What the output uses when no scale is set (Settings
                    // marks it "Recommended").
                    "recommended_scale": crate::backend::RecommendedScale::of(output),
                    "rotation": rotation,
                    "mode": current.map(|m| json!({ "width": m.size.w, "height": m.size.h, "refresh": m.refresh })),
                    "adaptive_sync": adaptive_sync_json(crate::backend::AdaptiveSync::of(output)),
                    "modes": modes.iter().map(|m| json!({
                        "width": m.size.w,
                        "height": m.size.h,
                        "refresh": m.refresh,
                        "preferred": Some(*m) == preferred,
                    })).collect::<Vec<_>>(),
                    "usable": { "x": usable.loc.x, "y": usable.loc.y, "width": usable.size.w, "height": usable.size.h },
                })
            })
            .collect();
        outputs.extend(self.disabled_outputs.iter().map(|name| json!({ "name": name, "enabled": false })));
        Value::Array(outputs)
    }

    /// Handles one request line. `None` means the reply comes later
    /// (screenshots are answered after the next frame).
    fn ipc_request(&mut self, connection: u64, trusted: bool, line: &str) -> Option<Value> {
        let request: Value = match serde_json::from_str(line) {
            Ok(Value::Object(map)) => Value::Object(map),
            _ => return Some(json!({ "ok": false, "error": "a request is one JSON object per line" })),
        };
        let id = request.get("id").cloned().unwrap_or(Value::Null);
        let reply = |result: Result<Value, String>| {
            Some(match result {
                Ok(data) => json!({ "id": id, "ok": true, "data": data }),
                Err(error) => json!({ "id": id, "ok": false, "error": error }),
            })
        };
        let Some(cmd) = request.get("cmd").and_then(Value::as_str) else {
            return reply(Err("missing \"cmd\"".into()));
        };

        // Available to every connection.
        match cmd {
            "version" => {
                return reply(Ok(json!({
                    "name": "boswas-compositor",
                    "version": env!("CARGO_PKG_VERSION"),
                    "trusted": trusted,
                    // Whether the session is locked is on screen for anyone
                    // to see; it is not a secret.
                    "locked": self.lock.locked,
                    // Settings says where colour filters cannot be drawn.
                    "color_filters": self.backend.filters_color(),
                })))
            }
            "lock" => {
                self.lock_session();
                return reply(Ok(Value::Null));
            }
            // Re-reading the files a user controls anyway grants nothing.
            "reload-config" => {
                let warnings = self.reload_config();
                return reply(Ok(json!({ "warnings": warnings })));
            }
            // What the monitors are is no secret to a local program (every
            // client sees them as wl_output); Settings needs the modes too.
            "outputs" => return reply(Ok(self.outputs_json())),
            // Starts the capture screen, where the user chooses what to keep
            // and confirms or cancels. Nothing is captured without that.
            "capture" => {
                let region = request.get("mode").and_then(Value::as_str) != Some("screen");
                self.request_capture(region);
                return reply(Ok(Value::Null));
            }
            _ => {}
        }
        if !trusted {
            return reply(Err(format!("\"{cmd}\" is not available on this connection")));
        }

        let window = |state: &Boswas| {
            request
                .get("window")
                .and_then(Value::as_u64)
                .and_then(|id| state.window_by_id(id))
                .ok_or_else(|| "no such window".to_string())
        };
        let number = |key: &str| request.get(key).and_then(Value::as_f64).ok_or_else(|| format!("missing \"{key}\""));

        let result: Result<Value, String> = match cmd {
            "subscribe" => {
                if let Some(c) = self.ipc.connections.iter_mut().find(|c| c.id == connection) {
                    c.subscribed = true;
                }
                Ok(json!({
                    "windows": self.windows_json(),
                    "workspaces": self.workspaces_json(),
                    "outputs": self.outputs_json(),
                    "locked": self.lock.locked,
                    "appearance": self.appearance_json(),
                    "night_light": self.night_light_json(),
                    "layout": self.layout_json(),
                    "overview": self.overview_active(),
                    "show_desktop": self.show_desktop.is_some(),
                    "capturing": self.screencopy_json(),
                }))
            }
            // The user's answer to a "capture-request" event ("request" is
            // that event's id; "id" is this command's own number).
            "capture-answer" => match (request.get("request").and_then(Value::as_u64), request.get("allow").and_then(Value::as_bool)) {
                (Some(id), Some(allow)) => {
                    if self.screencopy_answer(id, allow) {
                        Ok(Value::Null)
                    } else {
                        Err("no such capture request".into())
                    }
                }
                _ => Err("missing \"request\" or \"allow\"".into()),
            },
            // "Stop" on the capture indicator.
            "capture-stop" => {
                self.screencopy_stop();
                Ok(Value::Null)
            }
            "windows" => Ok(self.windows_json()),
            "workspaces" => Ok(self.workspaces_json()),
            "overview" => {
                self.set_overview(request.get("active").and_then(Value::as_bool));
                Ok(json!({ "active": self.overview_active() }))
            }
            "show-desktop" => {
                self.set_show_desktop(request.get("active").and_then(Value::as_bool));
                Ok(json!({ "active": self.show_desktop.is_some() }))
            }
            // "active": on or off now (with a schedule, until its next
            // change); "enabled": the session's switch.
            "night-light" if request.get("active").and_then(Value::as_bool).is_some() => {
                self.set_night_light_now(request["active"].as_bool().unwrap_or(false));
                Ok(self.night_light_json())
            }
            "night-light" => {
                let enabled = request.get("enabled").and_then(Value::as_bool).unwrap_or(!self.night_light.0);
                let temperature = request.get("temperature").and_then(Value::as_u64).map(|t| t as u32);
                self.set_night_light(enabled, temperature);
                Ok(self.night_light_json())
            }
            // How the monitors are used (the projection flyout, Super+P).
            "project" => self.project(request.get("mode").and_then(Value::as_str).unwrap_or_default()),
            "projection" => Ok(self.projection_json()),
            "next-layout" => {
                self.next_keyboard_layout();
                Ok(self.layout_json())
            }
            "new-workspace" => {
                self.add_workspace();
                Ok(self.workspaces_json())
            }
            "close-workspace" => number("index").map(|index| {
                self.remove_workspace(index as usize);
                self.workspaces_json()
            }),
            "move-to-workspace" => window(self).and_then(|w| {
                let index = number("index")? as usize;
                if index >= self.workspace_count {
                    return Err("no such workspace".into());
                }
                self.move_to_workspace(&w, index);
                Ok(Value::Null)
            }),
            "thumbnail" => match (
                window(self),
                request.get("path").and_then(Value::as_str).map(PathBuf::from),
            ) {
                (Ok(w), Some(path)) if path.is_absolute() => {
                    let width = request.get("width").and_then(Value::as_u64).unwrap_or(240).clamp(16, 1024) as u32;
                    self.ipc.screenshots.push(ScreenshotRequest {
                        connection,
                        request_id: id.clone(),
                        path,
                        target: CaptureTarget::Window { id: w.id(), width },
                    });
                    self.schedule_redraw();
                    return None;
                }
                (Err(error), _) => Err(error),
                _ => Err("\"path\" must be an absolute file name".into()),
            },
            "activate" => window(self).map(|w| {
                self.activate_window(&w);
                Value::Null
            }),
            "toggle-minimize" => window(self).map(|w| {
                self.toggle_minimize(&w);
                Value::Null
            }),
            "minimize" => window(self).map(|w| {
                self.minimize_window(&w);
                Value::Null
            }),
            "close" => window(self).map(|w| {
                self.close_window(&w);
                Value::Null
            }),
            "tile" => window(self).and_then(|w| {
                let name = request.get("tile").and_then(Value::as_str).unwrap_or_default();
                let tile = [
                    Tile::Floating,
                    Tile::Maximized,
                    Tile::Fullscreen,
                    Tile::Left,
                    Tile::Right,
                    Tile::TopLeft,
                    Tile::TopRight,
                    Tile::BottomLeft,
                    Tile::BottomRight,
                ]
                .into_iter()
                .find(|t| t.name() == name)
                .ok_or_else(|| format!("unknown tile \"{name}\""))?;
                self.set_tile(&w, tile);
                Ok(Value::Null)
            }),
            "workspace" => number("index").map(|index| {
                self.switch_workspace(index as usize);
                self.workspaces_json()
            }),
            "screenshot" => match request.get("path").and_then(Value::as_str).map(PathBuf::from) {
                Some(path) if path.is_absolute() => {
                    let output = request.get("output").and_then(Value::as_str).map(str::to_owned);
                    self.ipc.screenshots.push(ScreenshotRequest {
                        connection,
                        request_id: id.clone(),
                        path,
                        target: if request.get("display").and_then(Value::as_bool) == Some(true) {
                            CaptureTarget::Display(output)
                        } else {
                            CaptureTarget::Output(output)
                        },
                    });
                    self.schedule_redraw();
                    return None;
                }
                _ => Err("\"path\" must be an absolute file name".into()),
            },
            "quit" => {
                self.stop();
                Ok(Value::Null)
            }
            // The clipboard history and the emoji panel: paste what the
            // shell put on the clipboard into the focused window. No keys
            // are named; see paste.rs.
            "paste" => self.request_paste(),
            "pointer" | "button" | "key" | "scroll" if !self.ipc.test_input => {
                Err("input injection is only available with --test-input".into())
            }
            "pointer" => number("x").and_then(|x| number("y").map(|y| (x, y))).map(|(x, y)| {
                let time = self.start_time.elapsed().as_millis() as u32;
                self.on_pointer_position(Point::from((x, y)), time);
                Value::Null
            }),
            "button" => number("button").map(|button| {
                let pressed = request.get("pressed").and_then(Value::as_bool).unwrap_or(true);
                let state = if pressed { ButtonState::Pressed } else { ButtonState::Released };
                let time = self.start_time.elapsed().as_millis() as u32;
                self.on_pointer_button(button as u32, state, time);
                Value::Null
            }),
            "key" => number("keycode").map(|keycode| {
                let pressed = request.get("pressed").and_then(Value::as_bool).unwrap_or(true);
                let state = if pressed { KeyState::Pressed } else { KeyState::Released };
                let time = self.start_time.elapsed().as_millis() as u32;
                // Evdev key code on the wire; XKB key codes are offset by 8.
                self.on_key(keycode as u32 + 8, state, time);
                Value::Null
            }),
            "scroll" => {
                let dx = request.get("dx").and_then(Value::as_f64).unwrap_or(0.0);
                let dy = request.get("dy").and_then(Value::as_f64).unwrap_or(0.0);
                let time = self.start_time.elapsed().as_millis() as u32;
                self.on_scroll(dx, dy, time);
                Ok(Value::Null)
            }
            other => Err(format!("unknown command \"{other}\"")),
        };
        reply(result)
    }

    /// Answers a screenshot or thumbnail request once the file is written.
    pub fn ipc_screenshot_done(&mut self, request: ScreenshotRequest, result: Result<Value, String>) {
        let reply = match result {
            Ok(data) => json!({ "id": request.request_id, "ok": true, "data": data }),
            Err(error) => json!({ "id": request.request_id, "ok": false, "error": error }),
        };
        self.ipc_send(request.connection, &reply);
    }
}

/// How the `outputs` command reports an output's variable refresh rate.
fn adaptive_sync_json(sync: crate::backend::AdaptiveSync) -> Value {
    json!({ "supported": sync.supported, "enabled": sync.enabled })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::AdaptiveSync;

    #[test]
    fn adaptive_sync_is_reported_as_whether_it_is_offered_and_whether_it_is_on() {
        assert_eq!(adaptive_sync_json(AdaptiveSync::default()), json!({ "supported": false, "enabled": false }));
        assert_eq!(
            adaptive_sync_json(AdaptiveSync { supported: true, enabled: false }),
            json!({ "supported": true, "enabled": false })
        );
        assert_eq!(
            adaptive_sync_json(AdaptiveSync { supported: true, enabled: true }),
            json!({ "supported": true, "enabled": true })
        );
    }
}
