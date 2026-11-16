//! What can have the keyboard.
//!
//! A Wayland surface gets the keyboard by the Wayland protocol alone. A
//! window of an X11 application (through XWayland) additionally has to be
//! given the X11 input focus, which is what makes the X server deliver the
//! keys to that window and not to another one of the same server.

use std::borrow::Cow;

use smithay::{
    backend::input::KeyState,
    desktop::PopupKind,
    input::{
        keyboard::{KeyboardTarget, KeysymHandle, ModifiersState},
        Seat,
    },
    reexports::wayland_server::{backend::ObjectId, protocol::wl_surface::WlSurface, Resource},
    utils::{IsAlive, Serial},
    wayland::seat::WaylandFocus,
    xwayland::X11Surface,
};

use crate::state::Boswas;

#[derive(Debug, Clone, PartialEq)]
pub enum KeyboardFocus {
    /// A Wayland surface: a window, a popup, a layer surface, the lock
    /// screen.
    Surface(WlSurface),
    /// A window of an X11 application, and the Wayland surface XWayland
    /// attached to it.
    X11 { window: X11Surface, surface: WlSurface },
}

impl KeyboardFocus {
    /// The Wayland surface that receives the keys.
    pub fn surface(&self) -> &WlSurface {
        match self {
            KeyboardFocus::Surface(surface) | KeyboardFocus::X11 { surface, .. } => surface,
        }
    }
}

// The pointer's focus is always a Wayland surface (popup grabs compare the
// two kinds of focus).
impl From<KeyboardFocus> for WlSurface {
    fn from(focus: KeyboardFocus) -> Self {
        match focus {
            KeyboardFocus::Surface(surface) | KeyboardFocus::X11 { surface, .. } => surface,
        }
    }
}

impl From<WlSurface> for KeyboardFocus {
    fn from(surface: WlSurface) -> Self {
        KeyboardFocus::Surface(surface)
    }
}

impl From<PopupKind> for KeyboardFocus {
    fn from(popup: PopupKind) -> Self {
        KeyboardFocus::Surface(popup.wl_surface().clone())
    }
}

impl IsAlive for KeyboardFocus {
    fn alive(&self) -> bool {
        match self {
            KeyboardFocus::Surface(surface) => surface.alive(),
            KeyboardFocus::X11 { window, .. } => window.alive(),
        }
    }
}

impl WaylandFocus for KeyboardFocus {
    fn wl_surface(&self) -> Option<Cow<'_, WlSurface>> {
        Some(Cow::Borrowed(self.surface()))
    }

    fn same_client_as(&self, object_id: &ObjectId) -> bool {
        self.surface().id().same_client_as(object_id)
    }
}

impl KeyboardTarget<Boswas> for KeyboardFocus {
    fn enter(&self, seat: &Seat<Boswas>, data: &mut Boswas, keys: Vec<KeysymHandle<'_>>, serial: Serial) {
        match self {
            KeyboardFocus::Surface(surface) => KeyboardTarget::enter(surface, seat, data, keys, serial),
            KeyboardFocus::X11 { window, .. } => KeyboardTarget::enter(window, seat, data, keys, serial),
        }
    }

    fn leave(&self, seat: &Seat<Boswas>, data: &mut Boswas, serial: Serial) {
        match self {
            KeyboardFocus::Surface(surface) => KeyboardTarget::leave(surface, seat, data, serial),
            KeyboardFocus::X11 { window, .. } => KeyboardTarget::leave(window, seat, data, serial),
        }
    }

    fn key(
        &self,
        seat: &Seat<Boswas>,
        data: &mut Boswas,
        key: KeysymHandle<'_>,
        state: KeyState,
        serial: Serial,
        time: u32,
    ) {
        match self {
            KeyboardFocus::Surface(surface) => KeyboardTarget::key(surface, seat, data, key, state, serial, time),
            KeyboardFocus::X11 { window, .. } => KeyboardTarget::key(window, seat, data, key, state, serial, time),
        }
    }

    fn modifiers(&self, seat: &Seat<Boswas>, data: &mut Boswas, modifiers: ModifiersState, serial: Serial) {
        match self {
            KeyboardFocus::Surface(surface) => KeyboardTarget::modifiers(surface, seat, data, modifiers, serial),
            KeyboardFocus::X11 { window, .. } => KeyboardTarget::modifiers(window, seat, data, modifiers, serial),
        }
    }
}
