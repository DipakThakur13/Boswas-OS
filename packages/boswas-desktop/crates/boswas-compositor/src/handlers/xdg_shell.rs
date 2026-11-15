//! xdg-shell (application windows and popups) and xdg-decoration.

use smithay::{
    delegate_xdg_decoration, delegate_xdg_shell,
    desktop::{
        find_popup_root_surface, PopupKeyboardGrab, PopupKind, PopupPointerGrab, PopupUngrabStrategy,
    },
    input::{
        pointer::{Focus, GrabStartData},
        Seat,
    },
    reexports::{
        wayland_protocols::xdg::{
            decoration::zv1::server::zxdg_toplevel_decoration_v1::Mode as DecorationMode,
            shell::server::xdg_toplevel,
        },
        wayland_server::{
            protocol::{wl_output::WlOutput, wl_seat::WlSeat, wl_surface::WlSurface},
            Resource,
        },
    },
    utils::Serial,
    wayland::shell::xdg::{
        decoration::XdgDecorationHandler, PopupSurface, PositionerState, ToplevelSurface, XdgShellHandler,
        XdgShellState,
    },
};
use tracing::warn;

use crate::{
    shell::{
        element::{Tile, WindowElement},
        grabs::{MoveGrab, ResizeEdge, ResizeGrab},
    },
    state::Boswas,
};

impl XdgShellHandler for Boswas {
    fn xdg_shell_state(&mut self) -> &mut XdgShellState {
        &mut self.xdg_shell_state
    }

    fn new_toplevel(&mut self, surface: ToplevelSurface) {
        // The window is shown once the client has committed a buffer
        // (`window_committed`), when its size is known.
        let id = self.next_window_id();
        self.windows.push(WindowElement::new(surface, id, self.workspace));
    }

    fn toplevel_destroyed(&mut self, surface: ToplevelSurface) {
        self.remove_window(surface.wl_surface());
    }

    fn new_popup(&mut self, surface: PopupSurface, _positioner: PositionerState) {
        self.unconstrain_popup(&surface);
        if let Err(err) = self.popups.track_popup(PopupKind::Xdg(surface)) {
            warn!("could not track a popup: {err}");
        }
    }

    fn reposition_request(&mut self, surface: PopupSurface, positioner: PositionerState, token: u32) {
        surface.with_pending_state(|state| {
            state.geometry = positioner.get_geometry();
            state.positioner = positioner;
        });
        self.unconstrain_popup(&surface);
        surface.send_repositioned(token);
    }

    fn move_request(&mut self, surface: ToplevelSurface, seat: WlSeat, serial: Serial) {
        let Some(window) = self.window_for_surface(surface.wl_surface()) else {
            return;
        };
        if let Some(start_data) = self.check_grab(&seat, surface.wl_surface(), serial) {
            self.start_move(&window, start_data, serial);
        }
    }

    fn resize_request(&mut self, surface: ToplevelSurface, seat: WlSeat, serial: Serial, edges: xdg_toplevel::ResizeEdge) {
        let Some(window) = self.window_for_surface(surface.wl_surface()) else {
            return;
        };
        if let Some(start_data) = self.check_grab(&seat, surface.wl_surface(), serial) {
            self.start_resize(&window, start_data, edges.into(), serial);
        }
    }

    fn grab(&mut self, surface: PopupSurface, seat: WlSeat, serial: Serial) {
        let Some(seat) = Seat::<Boswas>::from_resource(&seat) else {
            return;
        };
        let kind = PopupKind::Xdg(surface);
        let Ok(root) = find_popup_root_surface(&kind) else {
            return;
        };
        let Ok(mut grab) = self.popups.grab_popup(root, kind, &seat, serial) else {
            return;
        };
        if let Some(keyboard) = seat.get_keyboard() {
            if keyboard.is_grabbed()
                && !(keyboard.has_grab(serial) || keyboard.has_grab(grab.previous_serial().unwrap_or(serial)))
            {
                grab.ungrab(PopupUngrabStrategy::All);
                return;
            }
            keyboard.set_focus(self, grab.current_grab(), serial);
            keyboard.set_grab(self, PopupKeyboardGrab::new(&grab), serial);
        }
        if let Some(pointer) = seat.get_pointer() {
            if pointer.is_grabbed()
                && !(pointer.has_grab(serial) || pointer.has_grab(grab.previous_serial().unwrap_or_else(|| grab.serial())))
            {
                grab.ungrab(PopupUngrabStrategy::All);
                return;
            }
            pointer.set_grab(self, PopupPointerGrab::new(&grab), serial, Focus::Keep);
        }
    }

    fn maximize_request(&mut self, surface: ToplevelSurface) {
        match self.window_for_surface(surface.wl_surface()) {
            Some(window) => self.set_tile(&window, Tile::Maximized),
            // The protocol wants a configure in reply either way.
            None => {
                surface.send_configure();
            }
        }
    }

    fn unmaximize_request(&mut self, surface: ToplevelSurface) {
        if let Some(window) = self.window_for_surface(surface.wl_surface()) {
            if window.data().tile.get() == Tile::Maximized {
                self.set_tile(&window, Tile::Floating);
            }
        }
    }

    fn fullscreen_request(&mut self, surface: ToplevelSurface, _output: Option<WlOutput>) {
        match self.window_for_surface(surface.wl_surface()) {
            Some(window) => self.set_tile(&window, Tile::Fullscreen),
            None => {
                surface.send_configure();
            }
        }
    }

    fn unfullscreen_request(&mut self, surface: ToplevelSurface) {
        if let Some(window) = self.window_for_surface(surface.wl_surface()) {
            if window.data().tile.get() == Tile::Fullscreen {
                self.set_tile(&window, Tile::Floating);
            }
        }
    }

    fn minimize_request(&mut self, surface: ToplevelSurface) {
        if let Some(window) = self.window_for_surface(surface.wl_surface()) {
            self.minimize_window(&window);
        }
    }

    fn title_changed(&mut self, _surface: ToplevelSurface) {
        self.schedule_redraw();
        self.windows_changed();
    }

    fn app_id_changed(&mut self, _surface: ToplevelSurface) {
        self.windows_changed();
    }
}
delegate_xdg_shell!(Boswas);

impl Boswas {
    /// A client may only start a move or resize from the click it just
    /// received: the serial must belong to the pointer's current implicit
    /// grab on one of that client's surfaces.
    fn check_grab(&self, seat: &WlSeat, surface: &WlSurface, serial: Serial) -> Option<GrabStartData<Boswas>> {
        let seat = Seat::<Boswas>::from_resource(seat)?;
        let pointer = seat.get_pointer()?;
        if !pointer.has_grab(serial) {
            return None;
        }
        let start_data = pointer.grab_start_data()?;
        let (focus, _) = start_data.focus.as_ref()?;
        if !focus.id().same_client_as(&surface.id()) {
            return None;
        }
        Some(start_data)
    }

    pub fn start_move(&mut self, window: &WindowElement, start_data: GrabStartData<Boswas>, serial: Serial) {
        if window.data().tile.get() == Tile::Fullscreen {
            return;
        }
        let Some(location) = self.space.element_location(window) else {
            return;
        };
        let grab = MoveGrab {
            anchor: start_data.location,
            start_data,
            window: window.clone(),
            initial_location: location,
        };
        let pointer = self.pointer.clone();
        pointer.set_grab(self, grab, serial, Focus::Clear);
    }

    pub fn start_resize(
        &mut self,
        window: &WindowElement,
        start_data: GrabStartData<Boswas>,
        edges: ResizeEdge,
        serial: Serial,
    ) {
        if window.data().tile.get() != Tile::Floating || edges.is_empty() {
            return;
        }
        let Some(rect) = self.window_rect(window) else {
            return;
        };
        let toplevel = window.toplevel();
        toplevel.with_pending_state(|state| {
            state.states.set(xdg_toplevel::State::Resizing);
        });
        toplevel.send_pending_configure();
        let grab = ResizeGrab::start(start_data, window.clone(), edges, rect);
        let pointer = self.pointer.clone();
        pointer.set_grab(self, grab, serial, Focus::Clear);
    }
}

impl XdgDecorationHandler for Boswas {
    fn new_decoration(&mut self, toplevel: ToplevelSurface) {
        // A client that supports the protocol gets the Boswas frame.
        self.apply_decoration(&toplevel, DecorationMode::ServerSide);
    }

    fn request_mode(&mut self, toplevel: ToplevelSurface, mode: DecorationMode) {
        let mode = match mode {
            DecorationMode::ClientSide => DecorationMode::ClientSide,
            _ => DecorationMode::ServerSide,
        };
        self.apply_decoration(&toplevel, mode);
    }

    fn unset_mode(&mut self, toplevel: ToplevelSurface) {
        self.apply_decoration(&toplevel, DecorationMode::ServerSide);
    }
}
delegate_xdg_decoration!(Boswas);

impl Boswas {
    fn apply_decoration(&mut self, toplevel: &ToplevelSurface, mode: DecorationMode) {
        toplevel.with_pending_state(|state| state.decoration_mode = Some(mode));
        self.set_decoration_mode(toplevel.wl_surface(), mode);
        if toplevel.is_initial_configure_sent() {
            toplevel.send_pending_configure();
        }
    }
}
