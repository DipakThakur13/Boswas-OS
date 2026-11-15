//! ext-session-lock: the lock screen.
//!
//! The compositor owns the lock. `Boswas::lock_session` blanks every output
//! and takes input away from applications at once, then starts the lock
//! program; that program's surfaces replace the blank screen when they
//! arrive. Only a lock client's `unlock_and_destroy` (sent after it has
//! authenticated the user) ends the lock. If the lock program crashes the
//! outputs stay blank and it is started again.

use std::time::{Duration, Instant};

use smithay::{
    delegate_session_lock,
    output::Output,
    reexports::wayland_server::protocol::{wl_output::WlOutput, wl_surface::WlSurface},
    utils::{IsAlive, Size},
    wayland::session_lock::{LockSurface, SessionLockHandler, SessionLockManagerState, SessionLocker},
};
use tracing::{info, warn};

use crate::state::Boswas;

impl SessionLockHandler for Boswas {
    fn lock_state(&mut self) -> &mut SessionLockManagerState {
        &mut self.session_lock_state
    }

    fn lock(&mut self, confirmation: SessionLocker) {
        self.lock.surfaces.retain(|_, surface| surface.alive());
        if self.lock.locked && !self.lock.surfaces.is_empty() {
            // Another lock client is showing its surfaces; dropping the
            // confirmation tells this one that it did not get the lock.
            return;
        }
        // From here on applications are neither drawn nor given input; only
        // then is the client told that the session is locked.
        self.enter_locked_state();
        confirmation.lock();
    }

    fn unlock(&mut self) {
        info!("session unlocked");
        self.lock.locked = false;
        self.lock.surfaces.clear();
        self.lock.client_started = None;
        self.last_input = Instant::now();
        self.update_keyboard_focus();
        self.schedule_redraw();
        self.ipc_event("lock", serde_json::json!({ "locked": false }));
    }

    fn new_surface(&mut self, surface: LockSurface, output: WlOutput) {
        let Some(output) = Output::from_resource(&output) else {
            return;
        };
        let size = self
            .space
            .output_geometry(&output)
            .map(|geo| Size::from((geo.size.w.max(1) as u32, geo.size.h.max(1) as u32)));
        surface.with_pending_state(|state| state.size = size);
        surface.send_configure();
        self.lock.surfaces.insert(output, surface);
        self.update_keyboard_focus();
        self.schedule_redraw();
    }
}
delegate_session_lock!(Boswas);

impl Boswas {
    fn enter_locked_state(&mut self) {
        if !self.lock.locked {
            info!("session locked");
            self.lock.locked = true;
            self.ipc_event("lock", serde_json::json!({ "locked": true }));
        }
        self.switcher = None;
        self.overview = None;
        self.ghosts.clear();
        self.snap_preview = None;
        // Drop pointer and keyboard grabs (moves, popups) and all focus.
        let serial = smithay::utils::SERIAL_COUNTER.next_serial();
        let pointer = self.pointer.clone();
        pointer.unset_grab(self, serial, 0);
        let keyboard = self.keyboard.clone();
        keyboard.unset_grab(self);
        self.update_keyboard_focus();
        self.schedule_redraw();
    }

    /// Locks the session now and starts the lock program.
    pub fn lock_session(&mut self) {
        if self.lock.locked {
            return;
        }
        self.enter_locked_state();
        self.start_lock_client();
    }

    fn start_lock_client(&mut self) {
        self.lock.client_started = Some(Instant::now());
        let command = self.config.lock.command.clone();
        if let Err(err) = self.spawn_trusted(&command).map(|_| ()) {
            warn!("could not start the lock screen ({err}); the session stays locked and blank");
        }
    }

    /// Restarts the lock program when the session is locked but no lock
    /// surface is alive (the program crashed or never came up).
    pub fn supervise_lock(&mut self) {
        if !self.lock.locked {
            return;
        }
        self.lock.surfaces.retain(|_, surface| surface.alive());
        let waited = self
            .lock
            .client_started
            .is_none_or(|started| started.elapsed() > Duration::from_secs(3));
        if self.lock.surfaces.is_empty() && waited {
            self.start_lock_client();
            self.schedule_redraw();
        }
    }

    /// Keeps the keyboard on the lock surface once it has content.
    pub fn lock_surface_committed(&mut self, surface: &WlSurface) {
        if self.lock.locked && self.lock.surfaces.values().any(|s| s.wl_surface() == surface) {
            self.update_keyboard_focus();
        }
    }

    /// Locks after the configured time without input, unless a client (a
    /// video player, a presentation) inhibits idling.
    pub fn check_idle(&mut self) {
        let limit = self.config.lock.idle_seconds;
        if limit == 0 || self.lock.locked {
            return;
        }
        self.idle_inhibitors.retain(|surface| surface.alive());
        let inhibited = !self.idle_inhibitors.is_empty();
        self.idle_notifier_state.set_is_inhibited(inhibited);
        if !self.idle_inhibitors.is_empty() {
            self.last_input = Instant::now();
            return;
        }
        if self.last_input.elapsed() >= Duration::from_secs(u64::from(limit)) {
            info!("idle for {limit} s; locking");
            self.lock_session();
        }
    }
}
