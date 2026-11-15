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
    Appearance { light: bool, wallpaper: String },
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

    /// Sends a request; replies are not awaited (state arrives as events).
    pub fn send(&mut self, cmd: &str, mut arguments: Value) {
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
    }

    pub fn subscribe(&mut self) {
        self.send("subscribe", json!({}));
    }

    pub fn window(&mut self, cmd: &str, id: u64) {
        self.send(cmd, json!({ "window": id }));
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
    })
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
        Some(_) => Vec::new(),
        None => {
            let mut events = Vec::new();
            if data.get("windows").is_some() {
                events.push(ControlEvent::Windows(parse_windows(&data["windows"])));
                events.extend(parse_workspace(&data["workspaces"]));
                events.extend(parse_appearance(&data["appearance"]));
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
        };
        assert_eq!(
            parse_message(&json!({"event": "windows", "data": [window.clone()]})),
            [ControlEvent::Windows(vec![expected.clone()])]
        );
        assert_eq!(
            parse_message(&json!({"id": 1, "ok": true, "data": {"windows": [window], "workspaces": {"active": 1, "count": 4},
                "outputs": [], "locked": false}})),
            [ControlEvent::Windows(vec![expected]), ControlEvent::Workspace { active: 1, count: 4 }]
        );
        assert_eq!(
            parse_message(&json!({"event": "config", "data": {"scheme": "light", "wallpaper": "/usr/share/x.png"}})),
            [ControlEvent::Appearance { light: true, wallpaper: "/usr/share/x.png".into() }]
        );
        assert_eq!(parse_message(&json!({"event": "config", "data": {"wallpaper": 3}})), []);
        assert_eq!(parse_message(&json!({"event": "launcher", "data": {}})), [ControlEvent::Launcher]);
        assert_eq!(parse_message(&json!({"event": "lock", "data": {"locked": true}})), [ControlEvent::Lock(true)]);
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
                events.is_empty() || events == [ControlEvent::Windows(Vec::new())],
                "{message} gave {events:?}"
            );
        }
    }
}
