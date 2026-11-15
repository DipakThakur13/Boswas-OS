//! The system services behind the Boswas OS shell.
//!
//! One background thread per service talks to the system (D-Bus, the sound
//! server, sysfs) and reports through a single callback; the shell stays a
//! plain event loop and never blocks on the system. See [`api`] for the
//! contract and the crate's README for what each service talks to.

#![forbid(unsafe_code)]

pub mod api;

mod audio;
mod auth;
mod bluetooth;
mod brightness;
mod logind;
mod media;
mod network;
mod notifications;
mod power;
mod runtime;
mod session;
mod text;
mod tray;
mod var;

use std::{path::PathBuf, sync::Arc, thread::JoinHandle};

use async_channel::Sender;

pub use api::*;

/// Which services to run. The lock screen and the greeter run with less
/// than the desktop does.
#[derive(Clone, Debug)]
pub struct Config {
    /// Own `org.freedesktop.Notifications` on the session bus.
    pub notifications: bool,
    /// Own `org.kde.StatusNotifierWatcher` and host the tray.
    pub tray: bool,
    /// Register as the session's polkit authentication agent.
    pub auth_agent: bool,
    /// Watch MPRIS players.
    pub media: bool,
    /// Network, Bluetooth, audio, power and brightness.
    pub status: bool,
    /// Listen to logind (lock, unlock, sleep).
    pub session: bool,
}

impl Config {
    /// Everything: the desktop shell.
    pub fn desktop() -> Self {
        Self {
            notifications: true,
            tray: true,
            auth_agent: true,
            media: true,
            status: true,
            session: true,
        }
    }

    /// Status only (network, audio, power): the lock screen and the greeter,
    /// which must not show notifications or take over authentication.
    pub fn status_only() -> Self {
        Self {
            notifications: false,
            tray: false,
            auth_agent: false,
            media: false,
            status: true,
            session: false,
        }
    }

    /// Nothing but status services runs: the instance shows the state of
    /// the machine and may change the volume and the brightness, nothing
    /// else.
    fn restricted(&self) -> bool {
        !(self.notifications || self.tray || self.auth_agent || self.media || self.session)
    }
}

/// Where the services find the system: bus addresses, helper programs and
/// kernel files. [`Environment::from_env`] is what [`Services::start`]
/// uses; tests point the fields at private buses and stand-in programs.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct Environment {
    /// Address of the session bus; `None` is the usual one
    /// (`DBUS_SESSION_BUS_ADDRESS`).
    pub session_bus: Option<String>,
    /// Address of the system bus; `None` is the usual one.
    /// `BOSWAS_SERVICES_SYSTEM_BUS` sets it.
    pub system_bus: Option<String>,
    /// PipeWire's `pw-dump`.
    pub pw_dump: PathBuf,
    /// WirePlumber's `wpctl`.
    pub wpctl: PathBuf,
    /// polkit's helper that talks to PAM.
    pub polkit_helper: PathBuf,
    /// The kernel's backlight devices.
    pub backlight_root: PathBuf,
    /// The user database (`/etc/passwd` format).
    pub users_file: PathBuf,
    /// The logind session this process belongs to (`XDG_SESSION_ID`);
    /// `None` asks logind.
    pub session_id: Option<String>,
}

impl Environment {
    /// The real system. Only the bus addresses and the session id are read
    /// from the process environment; program paths are fixed.
    pub fn from_env() -> Self {
        let text = |name: &str| std::env::var(name).ok().filter(|value| !value.is_empty());
        Self {
            session_bus: None,
            system_bus: text("BOSWAS_SERVICES_SYSTEM_BUS"),
            pw_dump: PathBuf::from("/usr/bin/pw-dump"),
            wpctl: PathBuf::from("/usr/bin/wpctl"),
            polkit_helper: PathBuf::from("/usr/lib/polkit-1/polkit-agent-helper-1"),
            backlight_root: PathBuf::from("/sys/class/backlight"),
            users_file: PathBuf::from("/etc/passwd"),
            session_id: text("XDG_SESSION_ID"),
        }
    }
}

impl Default for Environment {
    fn default() -> Self {
        Self::from_env()
    }
}

/// The command channel of each running service.
#[derive(Default)]
struct Routes {
    network: Option<Sender<Command>>,
    bluetooth: Option<Sender<Command>>,
    audio: Option<Sender<Command>>,
    power: Option<Sender<Command>>,
    brightness: Option<Sender<Command>>,
    notifications: Option<Sender<Command>>,
    tray: Option<Sender<Command>>,
    media: Option<Sender<Command>>,
    auth: Option<Sender<Command>>,
    /// The session service takes no commands; it ends when this closes.
    session: Option<Sender<Command>>,
}

impl Routes {
    fn of(&self, command: &Command) -> Option<&Sender<Command>> {
        use Command::*;
        match command {
            Refresh(service) => match service {
                Service::Network => self.network.as_ref(),
                Service::Bluetooth => self.bluetooth.as_ref(),
                Service::Audio => self.audio.as_ref(),
                Service::Power => self.power.as_ref(),
                Service::Brightness => self.brightness.as_ref(),
                Service::Tray => self.tray.as_ref(),
                Service::Media => self.media.as_ref(),
            },
            WifiSetEnabled(_) | WifiScan | WifiConnect { .. } | WifiDisconnect | WifiForget { .. } | AirplaneMode(_) => {
                self.network.as_ref()
            }
            BluetoothSetEnabled(_)
            | BluetoothDiscover(_)
            | BluetoothConnect { .. }
            | BluetoothDisconnect { .. }
            | BluetoothForget { .. } => self.bluetooth.as_ref(),
            SetVolume(_) | SetMuted(_) | SetInputVolume(_) | SetInputMuted(_) | SetDefaultOutput { .. } | SetDefaultInput { .. } => {
                self.audio.as_ref()
            }
            SetBrightness(_) => self.brightness.as_ref(),
            SetPowerProfile(_) | Power(_) => self.power.as_ref(),
            NotificationAction { .. } | NotificationDismiss { .. } | SetDoNotDisturb(_) => self.notifications.as_ref(),
            TrayActivate { .. } | TraySecondaryActivate { .. } | TrayScroll { .. } | TrayMenu { .. } | TrayMenuActivate { .. } => {
                self.tray.as_ref()
            }
            MediaPlayPause { .. } | MediaNext { .. } | MediaPrevious { .. } => self.media.as_ref(),
            AuthRespond { .. } | AuthCancel { .. } => self.auth.as_ref(),
        }
    }
}

/// What a restricted instance (the lock screen, the greeter) still obeys:
/// re-reading, the volume and the brightness.
fn allowed_when_restricted(command: &Command) -> bool {
    matches!(
        command,
        Command::Refresh(_)
            | Command::SetVolume(_)
            | Command::SetMuted(_)
            | Command::SetInputVolume(_)
            | Command::SetInputMuted(_)
            | Command::SetBrightness(_)
    )
}

/// The running services. Dropping it stops them.
pub struct Services {
    routes: Routes,
    restricted: bool,
    /// Dropping this sender ends every service wherever it waits.
    stop: Option<Sender<()>>,
    threads: Vec<JoinHandle<()>>,
}

impl Services {
    /// Starts the services. `sink` is called from service threads with every
    /// event, starting with the current state of each service; it must be
    /// quick and must not block (forward to the shell's event loop).
    pub fn start(config: Config, sink: impl Fn(Event) + Send + Sync + 'static) -> Self {
        Self::start_with(config, Environment::from_env(), sink)
    }

    /// [`Services::start`] in another [`Environment`].
    pub fn start_with(config: Config, environment: Environment, sink: impl Fn(Event) + Send + Sync + 'static) -> Self {
        let ctx = runtime::Ctx {
            sink: Arc::new(sink),
            env: Arc::new(environment),
            restricted: config.restricted(),
        };
        let (stop, stopped) = async_channel::bounded::<()>(1);
        let mut threads = Vec::new();
        let mut start = |name: &str, run: &dyn Fn(&str, async_channel::Receiver<Command>) -> std::io::Result<JoinHandle<()>>| {
            let (sender, receiver) = async_channel::unbounded::<Command>();
            match run(name, receiver) {
                Ok(thread) => {
                    threads.push(thread);
                    Some(sender)
                }
                Err(error) => {
                    tracing::error!(service = name, %error, "could not start the service thread");
                    None
                }
            }
        };
        macro_rules! service {
            ($name:literal, $type:ty) => {
                start($name, &|name, receiver| runtime::spawn::<$type>(name, ctx.clone(), receiver, stopped.clone()))
            };
        }

        let mut routes = Routes::default();
        if config.status {
            routes.network = service!("network", network::Network);
            routes.bluetooth = service!("bluetooth", bluetooth::Bluetooth);
            routes.audio = service!("audio", audio::Audio);
            routes.power = service!("power", power::Power);
            routes.brightness = service!("brightness", brightness::Backlight);
        }
        if config.notifications {
            routes.notifications = service!("notifications", notifications::Notifications);
        }
        if config.tray {
            routes.tray = service!("tray", tray::Tray);
        }
        if config.media {
            routes.media = service!("media", media::Media);
        }
        if config.session {
            routes.session = service!("session", session::Session);
        }
        if config.auth_agent {
            routes.auth = service!("auth", auth::Auth);
        }

        Self {
            routes,
            restricted: config.restricted(),
            stop: Some(stop),
            threads,
        }
    }

    /// Queues a command. Never blocks; a command for a service that is not
    /// running is dropped.
    pub fn send(&self, command: Command) {
        if self.restricted && !allowed_when_restricted(&command) {
            return;
        }
        if let Some(route) = self.routes.of(&command) {
            // The channels are unbounded: this cannot block, and it fails
            // only when the service has ended.
            let _ = route.try_send(command);
        }
    }
}

impl Drop for Services {
    fn drop(&mut self) {
        self.stop.take();
        self.routes = Routes::default();
        let current = std::thread::current().id();
        for thread in self.threads.drain(..) {
            // Dropped from inside the sink: that thread ends by itself.
            if thread.thread().id() != current {
                let _ = thread.join();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_only_is_restricted_and_desktop_is_not() {
        assert!(Config::status_only().restricted());
        assert!(!Config::desktop().restricted());
        let mut lock_screen_with_session = Config::status_only();
        lock_screen_with_session.session = true;
        assert!(!lock_screen_with_session.restricted());
    }

    #[test]
    fn restricted_instances_obey_volume_and_brightness_only() {
        let allowed = [
            Command::Refresh(Service::Network),
            Command::SetVolume(0.5),
            Command::SetMuted(true),
            Command::SetInputVolume(0.5),
            Command::SetInputMuted(false),
            Command::SetBrightness(0.5),
        ];
        assert!(allowed.iter().all(allowed_when_restricted));
        let refused = [
            Command::WifiSetEnabled(false),
            Command::WifiScan,
            Command::WifiConnect { ssid: "a".into(), passphrase: None },
            Command::WifiDisconnect,
            Command::WifiForget { ssid: "a".into() },
            Command::AirplaneMode(true),
            Command::BluetoothSetEnabled(true),
            Command::BluetoothDiscover(true),
            Command::BluetoothConnect { address: "a".into() },
            Command::BluetoothDisconnect { address: "a".into() },
            Command::BluetoothForget { address: "a".into() },
            Command::SetDefaultOutput { id: 1 },
            Command::SetDefaultInput { id: 1 },
            Command::SetPowerProfile(PowerProfile::Balanced),
            Command::Power(PowerAction::PowerOff),
        ];
        assert!(!refused.iter().any(allowed_when_restricted));
    }

    #[test]
    fn secrets_do_not_show_in_debug_output() {
        let connect = Command::WifiConnect {
            ssid: "home".into(),
            passphrase: Some(Secret::new("correct horse battery".into())),
        };
        let respond = Command::AuthRespond {
            cookie: "c1".into(),
            response: Secret::new("hunter2-very-private".into()),
        };
        let shown = format!("{connect:?} {respond:?}");
        assert!(!shown.contains("correct horse"), "{shown}");
        assert!(!shown.contains("hunter2"), "{shown}");
        assert!(shown.contains("Secret(..)"));
    }
}
