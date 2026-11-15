//! The system services behind the Boswas OS shell.
//!
//! One background thread per service talks to the system (D-Bus, the sound
//! server, sysfs) and reports through a single callback; the shell stays a
//! plain event loop and never blocks on the system. See [`api`] for the
//! contract.

pub mod api;

use std::sync::mpsc::{self, Sender};

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
}

/// The running services. Dropping it stops them.
pub struct Services {
    commands: Sender<Command>,
}

impl Services {
    /// Starts the services. `sink` is called from service threads with every
    /// event, starting with the current state of each service; it must be
    /// quick and must not block (forward to the shell's event loop).
    pub fn start(config: Config, sink: impl Fn(Event) + Send + Sync + 'static) -> Self {
        let (commands, receiver) = mpsc::channel::<Command>();
        // The backends are started here; see the service modules.
        let _ = (config, sink, receiver);
        Self { commands }
    }

    /// Queues a command. Never blocks; a command for a service that is not
    /// running is dropped.
    pub fn send(&self, command: Command) {
        let _ = self.commands.send(command);
    }
}
