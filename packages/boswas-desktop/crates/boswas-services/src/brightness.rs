//! The backlight of the built-in display: read from sysfs, set through
//! logind (`Session.SetBrightness`), which needs no privilege for the
//! active session.

use std::{
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use async_channel::Receiver;
use async_executor::Task;

use crate::{
    logind::{own_session, LOGIN1, SESSION},
    runtime::{call_raw, earliest, wake, Bus, BusKind, CallResult, Ctx, Exec, Latest, Service, Wake, CALL_TIMEOUT},
    Brightness, Command, Event, Service as Which,
};

/// After a command the level is read again once a second for this long,
/// to follow what the hardware made of it.
const FOLLOW: Duration = Duration::from_secs(5);
const POLL: Duration = Duration::from_secs(1);

pub(crate) struct Backlight {
    ctx: Ctx,
    ex: Exec,
    state: Latest<Option<Brightness>>,
    bus: Option<Bus>,
    /// The session path to set the brightness on, once known.
    session: Option<String>,
    /// The call to logind that is on its way.
    setting: Option<Task<()>>,
    /// The level asked for while a call was on its way.
    waiting: Option<f32>,
    follow_until: Option<Instant>,
    next_poll: Option<Instant>,
}

/// A backlight device in sysfs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Device {
    pub name: String,
    pub current: u32,
    pub max: u32,
}

enum Msg {
    /// The call to logind returned; false when it failed.
    Set(bool),
}

impl Service for Backlight {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self {
            ctx,
            ex,
            state: Latest::default(),
            bus: None,
            session: None,
            setting: None,
            waiting: None,
            follow_until: None,
            next_poll: None,
        }
    }

    async fn run(&mut self, commands: Receiver<Command>) {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        self.read(true);
        loop {
            match wake(&commands, &messages, self.next_poll).await {
                Wake::Closed => return,
                Wake::Command(Command::Refresh(Which::Brightness)) => self.read(true),
                Wake::Command(Command::SetBrightness(level)) => {
                    if level.is_finite() {
                        self.follow();
                        if self.setting.is_some() {
                            // Only the latest level of a slider drag matters.
                            self.waiting = Some(level);
                        } else {
                            self.set(level, &inbox).await;
                        }
                    }
                }
                Wake::Command(_) => {}
                Wake::Message(Msg::Set(done)) => {
                    self.setting = None;
                    if !done {
                        // The session path or the connection may be stale
                        // (logind or the bus restarted): look again next
                        // time.
                        self.session = None;
                        self.bus = None;
                    }
                    self.read(false);
                    if let Some(level) = self.waiting.take() {
                        self.set(level, &inbox).await;
                    }
                }
                Wake::Timer => {
                    self.read(false);
                    let now = Instant::now();
                    self.next_poll = match self.follow_until {
                        Some(until) if now < until => Some(now + POLL),
                        _ => None,
                    };
                }
            }
        }
    }
}

impl Backlight {
    fn read(&mut self, force: bool) {
        let level = best_device(&self.ctx.env.backlight_root).map(|device| Brightness {
            level: (device.current as f32 / device.max as f32).clamp(0.0, 1.0),
        });
        if let Some(level) = self.state.update(level, force) {
            self.ctx.emit(Event::Brightness(level));
        }
    }

    fn follow(&mut self) {
        let now = Instant::now();
        self.follow_until = Some(now + FOLLOW);
        self.next_poll = earliest(self.next_poll, Some(now + POLL));
    }

    async fn set(&mut self, level: f32, inbox: &async_channel::Sender<Msg>) {
        let Some(device) = best_device(&self.ctx.env.backlight_root) else {
            return;
        };
        let value = raw_value(level, device.max);
        if value == device.current {
            return;
        }
        let Some((conn, session)) = self.session().await else {
            tracing::debug!("brightness: no logind session to set the backlight through");
            return;
        };
        let inbox = inbox.clone();
        self.setting = Some(self.ex.spawn(async move {
            let result: CallResult<_> = call_raw(
                &conn,
                CALL_TIMEOUT,
                LOGIN1,
                &session,
                SESSION,
                "SetBrightness",
                &("backlight", device.name.as_str(), value),
            )
            .await;
            if let Err(error) = &result {
                tracing::warn!(%error, "brightness: logind did not set the backlight");
            }
            let _ = inbox.send(Msg::Set(result.is_ok())).await;
        }));
    }

    /// The system bus and our session on it, connecting on first use.
    async fn session(&mut self) -> Option<(zbus::Connection, String)> {
        if self.bus.is_none() {
            match Bus::connect(BusKind::System, &self.ctx.env, &self.ex).await {
                Ok(bus) => self.bus = Some(bus),
                Err(error) => {
                    tracing::debug!(%error, "brightness: no system bus");
                    return None;
                }
            }
        }
        let conn = self.bus.as_ref()?.conn.clone();
        if self.session.is_none() {
            match own_session(&conn, &self.ctx.env).await {
                Some(session) => self.session = Some(session.path),
                None => {
                    // The connection may be dead (the bus restarted): the
                    // next command connects again.
                    self.bus = None;
                    return None;
                }
            }
        }
        Some((conn, self.session.clone()?))
    }
}

/// The value to write for a level of 0.0 to 1.0. Never 0: on many panels
/// that turns the backlight off, and a black screen cannot be undone by
/// looking at it.
pub(crate) fn raw_value(level: f32, max: u32) -> u32 {
    let floor = (max / 100).max(1);
    let value = (f64::from(level.clamp(0.0, 1.0)) * f64::from(max)).round() as u32;
    value.clamp(floor, max)
}

/// The device to control: the kernel's own order of preference is
/// firmware, then platform, then raw; among equals the first by name.
pub(crate) fn best_device(root: &Path) -> Option<Device> {
    let mut best: Option<(u8, Device)> = None;
    let mut names: Vec<String> = std::fs::read_dir(root)
        .ok()?
        .filter_map(|entry| entry.ok()?.file_name().into_string().ok())
        .filter(|name| plausible_name(name))
        .collect();
    names.sort();
    // A machine has one or two of these; a directory with hundreds is not
    // a backlight class.
    names.truncate(16);
    for name in names {
        let dir = root.join(&name);
        let Some(max) = read_number(&dir.join("max_brightness")).filter(|max| *max > 0) else {
            continue;
        };
        let Some(current) = read_number(&dir.join("brightness")) else {
            continue;
        };
        let rank = match read_text(&dir.join("type")).as_deref() {
            Some("firmware") => 0,
            Some("platform") => 1,
            Some("raw") => 2,
            _ => 3,
        };
        if best.as_ref().map(|(best_rank, _)| rank < *best_rank).unwrap_or(true) {
            best = Some((rank, Device { name, current: current.min(max), max }));
        }
    }
    best.map(|(_, device)| device)
}

/// Device names are file names the kernel chose (`intel_backlight`,
/// `acpi_video0`); logind gets nothing else.
fn plausible_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && !name.starts_with('.')
        && name.chars().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.' | ':'))
}

fn read_text(path: &PathBuf) -> Option<String> {
    use std::io::Read;
    // sysfs attributes are a few bytes; never read more than that.
    let mut text = String::new();
    std::fs::File::open(path).ok()?.take(64).read_to_string(&mut text).ok()?;
    Some(text.trim().to_owned())
}

fn read_number(path: &PathBuf) -> Option<u32> {
    read_text(path)?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn device(root: &Path, name: &str, kind: &str, current: &str, max: &str) {
        let dir = root.join(name);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("type"), format!("{kind}\n")).unwrap();
        std::fs::write(dir.join("brightness"), format!("{current}\n")).unwrap();
        std::fs::write(dir.join("max_brightness"), format!("{max}\n")).unwrap();
    }

    #[test]
    fn firmware_beats_platform_beats_raw() {
        let root = tempfile::tempdir().unwrap();
        device(root.path(), "intel_backlight", "raw", "100", "1000");
        assert_eq!(best_device(root.path()).unwrap().name, "intel_backlight");
        device(root.path(), "thinkpad_screen", "platform", "3", "15");
        assert_eq!(best_device(root.path()).unwrap().name, "thinkpad_screen");
        device(root.path(), "acpi_video0", "firmware", "5", "10");
        let best = best_device(root.path()).unwrap();
        assert_eq!(best, Device { name: "acpi_video0".into(), current: 5, max: 10 });
        // Among equals the first by name.
        device(root.path(), "acpi_video1", "firmware", "1", "10");
        assert_eq!(best_device(root.path()).unwrap().name, "acpi_video0");
    }

    #[test]
    fn broken_devices_are_skipped() {
        let root = tempfile::tempdir().unwrap();
        assert_eq!(best_device(root.path()), None);
        assert_eq!(best_device(&root.path().join("missing")), None);
        device(root.path(), "zero", "raw", "0", "0");
        device(root.path(), "words", "raw", "bright", "very");
        device(root.path(), "bad name", "firmware", "1", "2");
        std::fs::create_dir_all(root.path().join("empty")).unwrap();
        assert_eq!(best_device(root.path()), None);
        // A level above the maximum is not reported as more than full.
        device(root.path(), "over", "raw", "300", "255");
        assert_eq!(best_device(root.path()).unwrap().current, 255);
    }

    #[test]
    fn levels_never_turn_the_panel_off() {
        assert_eq!(raw_value(0.0, 255), 2);
        assert_eq!(raw_value(0.0, 7), 1);
        assert_eq!(raw_value(0.5, 1000), 500);
        assert_eq!(raw_value(1.0, 255), 255);
        assert_eq!(raw_value(7.0, 255), 255);
        assert_eq!(raw_value(-1.0, 255), 2);
        assert_eq!(raw_value(f32::NAN, 255), 2);
    }
}
