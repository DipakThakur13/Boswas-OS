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
    /// Show the state of the machine and change nothing but the volume
    /// and the brightness: every other command is dropped, and the
    /// instance registers no Bluetooth pairing agent. The lock screen and
    /// the greeter run like this ([`Config::status_only`]).
    pub restricted: bool,
    /// A restricted instance may still suspend, restart and shut the
    /// machine down (the login screen's power menu; [`Config::greeter`]).
    /// The lock screen may not: a locked session is not ended from its
    /// lock screen.
    pub power_actions: bool,
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
            restricted: false,
            power_actions: true,
        }
    }

    /// Status only (network, audio, power): the lock screen and the greeter,
    /// which must not show notifications or take over authentication, and
    /// which may change nothing but the volume and the brightness.
    pub fn status_only() -> Self {
        Self {
            notifications: false,
            tray: false,
            auth_agent: false,
            media: false,
            status: true,
            session: false,
            restricted: true,
            power_actions: false,
        }
    }

    /// The login screen: like [`Config::status_only`], plus the power menu
    /// (suspend, restart, shut down). Nobody is signed in, so there is no
    /// session to protect, and logind still decides what is allowed.
    pub fn greeter() -> Self {
        Self {
            power_actions: true,
            ..Self::status_only()
        }
    }

    /// The status services (network, Bluetooth, audio, power, brightness)
    /// with every one of their commands obeyed, and nothing else: a
    /// settings application, which connects to networks and pairs devices
    /// but is not the session's notification server, tray or
    /// authentication agent.
    pub fn settings() -> Self {
        Self {
            restricted: false,
            power_actions: true,
            ..Self::status_only()
        }
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
            AuthRespond { .. } | AuthCancel { .. } | AuthChooseUser { .. } => self.auth.as_ref(),
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

/// Suspend, hibernate, restart and shut down: what a login screen's power
/// menu does. Ending a session is not among them.
fn is_machine_power_action(command: &Command) -> bool {
    matches!(
        command,
        Command::Power(PowerAction::Suspend | PowerAction::Hibernate | PowerAction::Reboot | PowerAction::PowerOff)
    )
}

/// The running services. Dropping it stops them.
pub struct Services {
    routes: Routes,
    restricted: bool,
    power_actions: bool,
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
            restricted: config.restricted,
            power_actions: config.power_actions,
        };
        let (stop, stopped) = async_channel::bounded::<()>(1);
        let mut threads = Vec::new();
        // Starts one service on its thread; its command channel, or `None`
        // when the thread could not be started.
        macro_rules! service {
            ($name:literal, $type:ty) => {{
                let (sender, receiver) = async_channel::unbounded::<Command>();
                match runtime::spawn::<$type>($name, ctx.clone(), receiver, stopped.clone()) {
                    Ok(thread) => {
                        threads.push(thread);
                        Some(sender)
                    }
                    Err(error) => {
                        tracing::error!(service = $name, %error, "could not start the service thread");
                        None
                    }
                }
            }};
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
            restricted: config.restricted,
            power_actions: config.power_actions,
            stop: Some(stop),
            threads,
        }
    }

    /// Queues a command. Never blocks; a command for a service that is not
    /// running is dropped.
    pub fn send(&self, command: Command) {
        if self.restricted && !allowed_when_restricted(&command) && !(self.power_actions && is_machine_power_action(&command)) {
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
    use std::collections::HashMap;

    use super::*;

    #[test]
    fn only_the_lock_screen_configuration_is_restricted() {
        assert!(Config::status_only().restricted);
        assert!(!Config::desktop().restricted);
        assert!(!Config::settings().restricted);
        // The settings configuration runs what the lock screen runs.
        let (settings, lock) = (Config::settings(), Config::status_only());
        assert!(settings.status && lock.status);
        for config in [settings, lock] {
            assert!(!(config.notifications || config.tray || config.auth_agent || config.media || config.session));
        }
        // A lock screen that also listens to logind stays restricted.
        let mut listening = Config::status_only();
        listening.session = true;
        assert!(listening.restricted);
    }

    /// The services as `start_with` builds them for `config`, with the
    /// receiving ends of the command channels instead of service threads.
    fn routed(config: &Config) -> (Services, HashMap<&'static str, async_channel::Receiver<Command>>) {
        let mut receivers = HashMap::new();
        let mut route = |name: &'static str, runs: bool| {
            runs.then(|| {
                let (sender, receiver) = async_channel::unbounded();
                receivers.insert(name, receiver);
                sender
            })
        };
        let routes = Routes {
            network: route("network", config.status),
            bluetooth: route("bluetooth", config.status),
            audio: route("audio", config.status),
            power: route("power", config.status),
            brightness: route("brightness", config.status),
            notifications: route("notifications", config.notifications),
            tray: route("tray", config.tray),
            media: route("media", config.media),
            auth: route("auth", config.auth_agent),
            session: route("session", config.session),
        };
        (
            Services {
                routes,
                restricted: config.restricted,
                power_actions: config.power_actions,
                stop: None,
                threads: Vec::new(),
            },
            receivers,
        )
    }

    #[test]
    fn a_settings_instance_obeys_what_the_lock_screen_drops() {
        let commands = || {
            [
                ("network", Command::WifiConnect { ssid: "Home".into(), passphrase: Some(Secret::new("a passphrase".into())) }),
                ("network", Command::WifiForget { ssid: "Home".into() }),
                ("network", Command::AirplaneMode(true)),
                ("bluetooth", Command::BluetoothConnect { address: "AA:BB:CC:DD:EE:FF".into() }),
                ("bluetooth", Command::BluetoothForget { address: "AA:BB:CC:DD:EE:FF".into() }),
                ("audio", Command::SetDefaultOutput { id: 7 }),
                ("power", Command::SetPowerProfile(PowerProfile::PowerSaver)),
            ]
        };

        let (settings, receivers) = routed(&Config::settings());
        for (service, command) in commands() {
            settings.send(command.clone());
            assert_eq!(receivers[service].try_recv().ok(), Some(command), "the {service} service did not get its command");
        }

        let (lock_screen, receivers) = routed(&Config::status_only());
        for (_, command) in commands() {
            lock_screen.send(command);
        }
        assert!(receivers.values().all(|receiver| receiver.is_empty()), "the lock screen passed a command on");
        // What the lock screen may do still arrives.
        lock_screen.send(Command::SetVolume(0.4));
        lock_screen.send(Command::SetBrightness(0.6));
        lock_screen.send(Command::Refresh(Service::Network));
        assert_eq!(receivers["audio"].try_recv().ok(), Some(Command::SetVolume(0.4)));
        assert_eq!(receivers["brightness"].try_recv().ok(), Some(Command::SetBrightness(0.6)));
        assert_eq!(receivers["network"].try_recv().ok(), Some(Command::Refresh(Service::Network)));

        // Neither runs the session-bus services: their commands go nowhere.
        for config in [Config::settings(), Config::status_only()] {
            let (services, receivers) = routed(&config);
            services.send(Command::NotificationDismiss { id: 1 });
            services.send(Command::TrayMenu { key: "k".into() });
            services.send(Command::MediaNext { key: "k".into() });
            services.send(Command::AuthCancel { cookie: "c".into() });
            assert!(receivers.values().all(|receiver| receiver.is_empty()));
        }
    }

    #[test]
    fn the_desktop_routes_every_command_to_its_service() {
        let (desktop, receivers) = routed(&Config::desktop());
        let cases = [
            ("network", Command::WifiScan),
            ("bluetooth", Command::BluetoothDiscover(true)),
            ("audio", Command::SetInputMuted(true)),
            ("brightness", Command::SetBrightness(0.5)),
            ("power", Command::Power(PowerAction::Suspend)),
            ("notifications", Command::SetDoNotDisturb(true)),
            ("tray", Command::TrayScroll { key: "k".into(), delta: 1, horizontal: false }),
            ("media", Command::MediaPrevious { key: "k".into() }),
            ("auth", Command::AuthChooseUser { cookie: "c".into(), user: "u".into() }),
            ("auth", Command::AuthRespond { cookie: "c".into(), response: Secret::new("pw".into()) }),
        ];
        for (service, command) in cases {
            desktop.send(command.clone());
            assert_eq!(receivers[service].try_recv().ok(), Some(command));
            assert!(receivers.values().all(|receiver| receiver.is_empty()), "a command reached two services");
        }
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
            Command::AuthChooseUser { cookie: "c".into(), user: "root".into() },
        ];
        assert!(!refused.iter().any(allowed_when_restricted));
    }

    #[test]
    fn the_login_screen_has_a_power_menu_and_the_lock_screen_has_none() {
        let (greeter, lock) = (Config::greeter(), Config::status_only());
        assert!(greeter.restricted && greeter.power_actions);
        assert!(lock.restricted && !lock.power_actions);
        // Otherwise the two run the same services.
        assert_eq!(
            (greeter.status, greeter.notifications, greeter.tray, greeter.auth_agent, greeter.media, greeter.session),
            (lock.status, lock.notifications, lock.tray, lock.auth_agent, lock.media, lock.session)
        );
        let machine = [PowerAction::Suspend, PowerAction::Hibernate, PowerAction::Reboot, PowerAction::PowerOff];
        for action in machine {
            assert!(is_machine_power_action(&Command::Power(action)), "{action:?}");
        }
        // Ending a session, and everything that is not a power action,
        // stays out of the greeter's reach.
        for command in [
            Command::Power(PowerAction::LogOut),
            Command::SetPowerProfile(PowerProfile::Performance),
            Command::WifiSetEnabled(false),
            Command::BluetoothForget { address: "a".into() },
        ] {
            assert!(!is_machine_power_action(&command) && !allowed_when_restricted(&command), "{command:?}");
        }
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
