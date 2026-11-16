//! Client side of the compositor's control socket (see the compositor's
//! `ipc.rs`). The shell does not open the public socket: the compositor
//! hands it an already connected, trusted descriptor in
//! `BOSWAS_COMPOSITOR_FD` when it starts the shell.

use std::{
    io::{ErrorKind, Read, Write},
    os::{
        fd::{FromRawFd, OwnedFd},
        unix::net::UnixStream,
    },
};

use serde_json::{json, Value};

/// Longest message accepted from the compositor.
const MAX_LINE: usize = 4 * 1024 * 1024;

/// A window as the compositor reports it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WindowInfo {
    pub id: u64,
    pub title: String,
    pub app_id: String,
    pub active: bool,
    pub minimized: bool,
    pub workspace: usize,
    /// The window asks for attention.
    pub urgent: bool,
}

/// Something the compositor told the shell.
#[derive(Clone, Debug, PartialEq)]
pub enum ControlEvent {
    Windows(Vec<WindowInfo>),
    Workspace { active: usize, count: usize },
    /// The Super key was tapped.
    Launcher,
    Lock(bool),
    /// The session's look: sent at start and when the configuration is
    /// reloaded.
    Appearance { light: bool, wallpaper: String, reduced_motion: bool },
    /// Super+A.
    QuickSettings,
    /// Super+N.
    Notifications,
    /// Super+R.
    Run,
    /// Print, Super+Shift+S or the public `capture` command.
    Capture { whole_screen: bool },
    /// A volume, brightness or media key (`volume-up`, `media-play`, ...).
    MediaKey(String),
    /// Super+1..9: the nth application on the taskbar.
    TaskbarApp { index: usize, new_instance: bool },
    NightLight { enabled: bool },
    Layout { layouts: Vec<String>, active: usize },
    /// Task View opened or closed.
    Overview(bool),
    /// A program asks to see the screen; answered with `capture-answer`.
    CaptureRequest { id: u64, program: String },
    /// The question went away (answered elsewhere, timed out, locked).
    CaptureRequestClosed { id: u64 },
    /// The programs that may capture the screen right now.
    Capturing(Vec<String>),
    /// The answer to a request sent with [`Control::send`].
    Reply { id: u64, ok: bool, data: Value },
}

pub struct Control {
    stream: UnixStream,
    buffer: Vec<u8>,
    next_id: u64,
}

/// Takes the descriptor named by an environment variable, marks it
/// close-on-exec and removes the variable, so no child inherits it.
pub fn take_fd_from_env(variable: &str) -> Option<OwnedFd> {
    let fd: i32 = std::env::var(variable).ok()?.parse().ok()?;
    std::env::remove_var(variable);
    if fd < 3 {
        return None;
    }
    // SAFETY: the compositor passed this descriptor to this process for its
    // exclusive use and named it in `variable`; it is taken exactly once
    // (the variable is removed above) and owned from here on.
    let owned = unsafe { OwnedFd::from_raw_fd(fd) };
    match rustix::io::fcntl_setfd(&owned, rustix::io::FdFlags::CLOEXEC) {
        Ok(()) => Some(owned),
        Err(_) => {
            // Not an open descriptor after all; do not close a number that
            // was never ours.
            std::mem::forget(owned);
            None
        }
    }
}

impl Control {
    /// Connects through the descriptor the compositor provided.
    pub fn from_env() -> Option<Self> {
        let stream = UnixStream::from(take_fd_from_env("BOSWAS_COMPOSITOR_FD")?);
        stream.set_nonblocking(true).ok()?;
        Some(Self {
            stream,
            buffer: Vec::new(),
            next_id: 1,
        })
    }

    /// A clone of the socket for the event loop to watch.
    pub fn watch(&self) -> std::io::Result<UnixStream> {
        self.stream.try_clone()
    }

    /// Sends a request and returns its id. Nothing waits for the reply: it
    /// arrives later as [`ControlEvent::Reply`] with the same id.
    pub fn send(&mut self, cmd: &str, mut arguments: Value) -> u64 {
        let id = self.next_id;
        self.next_id += 1;
        if let Some(map) = arguments.as_object_mut() {
            map.insert("cmd".into(), json!(cmd));
            map.insert("id".into(), json!(id));
        }
        let mut line = arguments.to_string();
        line.push('\n');
        // The socket is non-blocking; requests are tiny, so a full buffer
        // means the compositor is gone and the read side will notice.
        let _ = self.stream.write_all(line.as_bytes());
        id
    }

    pub fn subscribe(&mut self) {
        self.send("subscribe", json!({}));
    }

    /// Reads what is available. `Err` means the compositor closed the
    /// connection.
    pub fn read_events(&mut self) -> Result<Vec<ControlEvent>, ()> {
        let mut chunk = [0u8; 8192];
        loop {
            match self.stream.read(&mut chunk) {
                Ok(0) => return Err(()),
                Ok(n) => {
                    self.buffer.extend_from_slice(&chunk[..n]);
                    if self.buffer.len() > MAX_LINE {
                        return Err(());
                    }
                }
                Err(err) if err.kind() == ErrorKind::WouldBlock => break,
                Err(err) if err.kind() == ErrorKind::Interrupted => continue,
                Err(_) => return Err(()),
            }
        }
        let mut events = Vec::new();
        while let Some(end) = self.buffer.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.buffer.drain(..=end).collect();
            if let Ok(message) = serde_json::from_slice::<Value>(&line[..end]) {
                events.extend(parse_message(&message));
            }
        }
        Ok(events)
    }
}

fn parse_windows(value: &Value) -> Vec<WindowInfo> {
    value
        .as_array()
        .map(|list| {
            list.iter()
                .filter_map(|w| {
                    Some(WindowInfo {
                        id: w.get("id")?.as_u64()?,
                        title: w.get("title")?.as_str()?.to_owned(),
                        app_id: w.get("app_id")?.as_str()?.to_owned(),
                        active: w.get("active")?.as_bool()?,
                        minimized: w.get("minimized")?.as_bool()?,
                        workspace: w.get("workspace")?.as_u64()? as usize,
                        urgent: w.get("urgent").and_then(Value::as_bool).unwrap_or(false),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn parse_workspace(value: &Value) -> Option<ControlEvent> {
    Some(ControlEvent::Workspace {
        active: value.get("active")?.as_u64()? as usize,
        count: value.get("count")?.as_u64()? as usize,
    })
}

fn parse_appearance(value: &Value) -> Option<ControlEvent> {
    Some(ControlEvent::Appearance {
        light: value.get("scheme")?.as_str()? == "light",
        wallpaper: value.get("wallpaper").and_then(Value::as_str).unwrap_or_default().to_owned(),
        reduced_motion: value.get("reduced_motion").and_then(Value::as_bool).unwrap_or(false),
    })
}

fn parse_night_light(value: &Value) -> Option<ControlEvent> {
    Some(ControlEvent::NightLight {
        enabled: value.get("enabled")?.as_bool()?,
    })
}

fn parse_layout(value: &Value) -> Option<ControlEvent> {
    // The compositor sends the short codes ("us", "in") next to the full
    // names; the indicator shows the codes.
    let layouts: Vec<String> = value
        .get("codes")
        .filter(|codes| codes.as_array().is_some_and(|c| !c.is_empty()))
        .or_else(|| value.get("layouts"))?
        .as_array()?
        .iter()
        .filter_map(|l| l.as_str())
        .take(16)
        .map(|l| l.chars().take(12).collect())
        .collect();
    let active = value.get("active").and_then(Value::as_u64).unwrap_or(0) as usize;
    Some(ControlEvent::Layout {
        active: active.min(layouts.len().saturating_sub(1)),
        layouts,
    })
}

/// A program's name as the compositor reports it, made safe to show.
fn program_name(value: &Value) -> String {
    let name: String = value.as_str().unwrap_or_default().chars().filter(|c| !c.is_control()).take(48).collect();
    if name.trim().is_empty() {
        "A program".to_owned()
    } else {
        name
    }
}

fn parse_capturing(data: &Value) -> Option<ControlEvent> {
    let programs = data.get("programs")?.as_array()?;
    Some(ControlEvent::Capturing(programs.iter().take(8).map(program_name).collect()))
}

/// Turns one message into events. The reply to `subscribe` carries the
/// initial state and is treated like the events that follow it.
pub fn parse_message(message: &Value) -> Vec<ControlEvent> {
    let data = &message["data"];
    match message.get("event").and_then(Value::as_str) {
        Some("windows") => vec![ControlEvent::Windows(parse_windows(data))],
        Some("workspace") => parse_workspace(data).into_iter().collect(),
        Some("launcher") => vec![ControlEvent::Launcher],
        Some("lock") => data.get("locked").and_then(Value::as_bool).map(ControlEvent::Lock).into_iter().collect(),
        Some("config") => parse_appearance(data).into_iter().collect(),
        Some("quick-settings") => vec![ControlEvent::QuickSettings],
        Some("notifications") => vec![ControlEvent::Notifications],
        Some("run") => vec![ControlEvent::Run],
        Some("capture") => vec![ControlEvent::Capture {
            whole_screen: data.get("mode").and_then(Value::as_str) == Some("screen"),
        }],
        Some("media-key") => data
            .get("key")
            .and_then(Value::as_str)
            .map(|key| ControlEvent::MediaKey(key.chars().take(32).collect()))
            .into_iter()
            .collect(),
        Some("taskbar-app") => data
            .get("index")
            .and_then(Value::as_u64)
            .filter(|index| (1..=9).contains(index))
            .map(|index| ControlEvent::TaskbarApp {
                index: index as usize,
                new_instance: data.get("shift").and_then(Value::as_bool).unwrap_or(false),
            })
            .into_iter()
            .collect(),
        Some("night-light") => parse_night_light(data).into_iter().collect(),
        Some("layout") => parse_layout(data).into_iter().collect(),
        Some("overview") => data.get("active").and_then(Value::as_bool).map(ControlEvent::Overview).into_iter().collect(),
        Some("capture-request") => data
            .get("id")
            .and_then(Value::as_u64)
            .map(|id| ControlEvent::CaptureRequest {
                id,
                program: program_name(&data["program"]),
            })
            .into_iter()
            .collect(),
        Some("capture-request-closed") => {
            data.get("id").and_then(Value::as_u64).map(|id| ControlEvent::CaptureRequestClosed { id }).into_iter().collect()
        }
        Some("capturing") => parse_capturing(data).into_iter().collect(),
        Some(_) => Vec::new(),
        None => {
            let mut events = Vec::new();
            if data.get("windows").is_some() {
                // The reply to `subscribe`: the whole initial state.
                events.push(ControlEvent::Windows(parse_windows(&data["windows"])));
                events.extend(parse_workspace(&data["workspaces"]));
                events.extend(parse_appearance(&data["appearance"]));
                events.extend(parse_night_light(&data["night_light"]));
                events.extend(parse_layout(&data["layout"]));
                events.extend(parse_capturing(&data["capturing"]));
                if let Some(locked) = data.get("locked").and_then(Value::as_bool) {
                    events.push(ControlEvent::Lock(locked));
                }
            } else if let (Some(id), Some(ok)) = (message.get("id").and_then(Value::as_u64), message.get("ok").and_then(Value::as_bool)) {
                events.push(ControlEvent::Reply {
                    id,
                    ok,
                    data: data.clone(),
                });
            }
            events
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn events_and_the_subscribe_reply_are_understood() {
        let window = json!({"id": 3, "title": "Files", "app_id": "org.kde.dolphin", "active": true,
            "minimized": false, "workspace": 1, "tile": "floating", "x": 0, "y": 0, "width": 10, "height": 10});
        let expected = WindowInfo {
            id: 3,
            title: "Files".into(),
            app_id: "org.kde.dolphin".into(),
            active: true,
            minimized: false,
            workspace: 1,
            urgent: false,
        };
        assert_eq!(
            parse_message(&json!({"event": "windows", "data": [window.clone()]})),
            [ControlEvent::Windows(vec![expected.clone()])]
        );
        assert_eq!(
            parse_message(&json!({"id": 1, "ok": true, "data": {"windows": [window], "workspaces": {"active": 1, "count": 4},
                "outputs": [], "locked": false}})),
            [ControlEvent::Windows(vec![expected]), ControlEvent::Workspace { active: 1, count: 4 }, ControlEvent::Lock(false)]
        );
        assert_eq!(
            parse_message(&json!({"event": "config", "data": {"scheme": "light", "wallpaper": "/usr/share/x.png"}})),
            [ControlEvent::Appearance { light: true, wallpaper: "/usr/share/x.png".into(), reduced_motion: false }]
        );
        assert_eq!(parse_message(&json!({"event": "config", "data": {"wallpaper": 3}})), []);
        assert_eq!(parse_message(&json!({"event": "launcher", "data": {}})), [ControlEvent::Launcher]);
        assert_eq!(parse_message(&json!({"event": "lock", "data": {"locked": true}})), [ControlEvent::Lock(true)]);
    }

    #[test]
    fn round_two_events_and_replies_are_understood() {
        let ev = |name: &str, data: Value| parse_message(&json!({"event": name, "data": data}));
        assert_eq!(ev("quick-settings", json!({})), [ControlEvent::QuickSettings]);
        assert_eq!(
            ev("capture-request", json!({"id": 7, "program": "obs\u{1b}[0m"})),
            [ControlEvent::CaptureRequest { id: 7, program: "obs[0m".into() }]
        );
        assert_eq!(ev("capture-request", json!({"program": "obs"})), [], "a request without an id cannot be answered");
        assert_eq!(
            ev("capture-request", json!({"id": 8, "program": ""})),
            [ControlEvent::CaptureRequest { id: 8, program: "A program".into() }]
        );
        assert_eq!(ev("capture-request-closed", json!({"id": 7})), [ControlEvent::CaptureRequestClosed { id: 7 }]);
        assert_eq!(
            ev("capturing", json!({"programs": ["obs", 3]})),
            [ControlEvent::Capturing(vec!["obs".into(), "A program".into()])]
        );
        assert_eq!(ev("capturing", json!({"programs": []})), [ControlEvent::Capturing(Vec::new())]);
        assert_eq!(ev("notifications", json!({})), [ControlEvent::Notifications]);
        assert_eq!(ev("run", json!({})), [ControlEvent::Run]);
        assert_eq!(ev("capture", json!({"mode": "screen"})), [ControlEvent::Capture { whole_screen: true }]);
        assert_eq!(ev("capture", json!({})), [ControlEvent::Capture { whole_screen: false }]);
        assert_eq!(ev("media-key", json!({"key": "volume-up"})), [ControlEvent::MediaKey("volume-up".into())]);
        assert_eq!(ev("media-key", json!({"key": 3})), []);
        assert_eq!(
            ev("taskbar-app", json!({"index": 3, "shift": true})),
            [ControlEvent::TaskbarApp { index: 3, new_instance: true }]
        );
        assert_eq!(ev("taskbar-app", json!({"index": 0})), []);
        assert_eq!(ev("taskbar-app", json!({"index": 10})), []);
        assert_eq!(ev("night-light", json!({"enabled": true, "temperature": 4000})), [ControlEvent::NightLight { enabled: true }]);
        assert_eq!(
            ev("layout", json!({"layouts": ["us", "in"], "active": 7})),
            [ControlEvent::Layout { layouts: vec!["us".into(), "in".into()], active: 1 }]
        );
        assert_eq!(
            ev("layout", json!({"layouts": ["English (US)", "Hindi"], "codes": ["us", "in"], "active": 1})),
            [ControlEvent::Layout { layouts: vec!["us".into(), "in".into()], active: 1 }]
        );
        assert_eq!(ev("overview", json!({"active": true})), [ControlEvent::Overview(true)]);
        assert_eq!(
            parse_message(&json!({"id": 9, "ok": false, "error": "unknown command"})),
            [ControlEvent::Reply { id: 9, ok: false, data: Value::Null }]
        );
        assert_eq!(
            parse_message(&json!({"id": 4, "ok": true, "data": {"width": 240, "height": 150}})),
            [ControlEvent::Reply { id: 4, ok: true, data: json!({"width": 240, "height": 150}) }]
        );
        let urgent = json!({"id": 1, "title": "t", "app_id": "a", "active": false, "minimized": false, "workspace": 0, "urgent": true});
        match &ev("windows", json!([urgent]))[..] {
            [ControlEvent::Windows(list)] => assert!(list[0].urgent),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn malformed_messages_are_ignored() {
        for message in [
            json!({"event": "windows", "data": "nope"}),
            json!({"event": "windows", "data": [{"id": "x"}]}),
            json!({"event": "workspace", "data": {}}),
            json!({"event": "unknown"}),
            json!({"id": 4, "ok": true, "data": null}),
            json!(42),
        ] {
            let events = parse_message(&message);
            assert!(
                events.is_empty()
                    || events == [ControlEvent::Windows(Vec::new())]
                    || matches!(&events[..], [ControlEvent::Reply { .. }]),
                "{message} gave {events:?}"
            );
        }
    }
}
