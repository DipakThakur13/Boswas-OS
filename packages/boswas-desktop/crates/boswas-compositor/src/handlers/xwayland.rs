//! XWayland: X11 applications on the Wayland desktop.
//!
//! The compositor starts one XWayland server for the session and acts as
//! its window manager. An X11 window becomes an ordinary window of the
//! desktop: it gets the Boswas frame (unless the application draws its
//! own), snaps, minimizes and appears in the taskbar like any other. Menus
//! and tooltips ("override redirect" windows) place themselves and are
//! shown where they ask to be.
//!
//! X11 has no isolation between its clients: applications connected to the
//! same X server can watch each other's windows and keys. That is a
//! property of X11 and stays inside XWayland; Wayland applications are not
//! visible to it, and the X server itself is an ordinary (unprivileged)
//! Wayland client.

use std::{os::fd::OwnedFd, process::Stdio};

use smithay::{
    delegate_xwayland_shell,
    input::pointer::GrabStartData,
    utils::{Logical, Rectangle, SERIAL_COUNTER},
    wayland::{
        selection::{
            data_device::{
                clear_data_device_selection, current_data_device_selection_userdata,
                request_data_device_client_selection, set_data_device_selection,
            },
            primary_selection::{
                clear_primary_selection, current_primary_selection_userdata, request_primary_client_selection,
                set_primary_selection,
            },
            SelectionTarget,
        },
        xwayland_shell::{XWaylandShellHandler, XWaylandShellState},
    },
    xwayland::{
        xwm::{Reorder, ResizeEdge as X11ResizeEdge, WmWindowProperty, XwmId},
        X11Surface, X11Wm, XWayland, XWaylandEvent, XwmHandler,
    },
};
use tracing::{info, warn};

use crate::{
    shell::{
        element::{Tile, WindowElement},
        grabs::ResizeEdge,
    },
    state::Boswas,
};

const BTN_LEFT: u32 = 0x110;

impl Boswas {
    /// Starts the XWayland server, when the configuration asks for it and
    /// the program is installed. Applications find it through `DISPLAY`.
    pub fn start_xwayland(&mut self) {
        if !self.config.runs_xwayland() {
            return;
        }
        let spawned = XWayland::spawn(
            &self.display_handle,
            None,
            std::iter::empty::<(String, String)>(),
            // No abstract socket: it would be reachable from every network
            // namespace, sandboxes included.
            false,
            Stdio::null(),
            Stdio::null(),
            |_| (),
        );
        let (xwayland, client) = match spawned {
            Ok(spawned) => spawned,
            Err(err) => {
                info!("XWayland is not available ({err}); X11 applications cannot run in this session");
                return;
            }
        };
        self.x_display = Some(xwayland.display_number());
        let inserted = self.loop_handle.insert_source(xwayland, move |event, _, state| match event {
            XWaylandEvent::Ready { x11_socket, .. } => {
                match X11Wm::start_wm(state.loop_handle.clone(), x11_socket, client.clone()) {
                    Ok(wm) => {
                        info!("XWayland is ready on display :{}", state.x_display.unwrap_or_default());
                        state.xwm = Some(wm);
                    }
                    Err(err) => warn!("could not attach the X11 window manager: {err}"),
                }
            }
            XWaylandEvent::Error => {
                warn!("XWayland exited while starting");
                state.x_display = None;
                state.xwm = None;
            }
        });
        if let Err(err) = inserted {
            warn!("could not watch XWayland: {err}");
            self.x_display = None;
        }
    }

    /// The window of an X11 surface.
    fn x11_window(&self, surface: &X11Surface) -> Option<WindowElement> {
        self.windows.iter().find(|w| w.x11() == Some(surface)).cloned()
    }

    /// The pointer's position as the start of a move or resize an X11
    /// application asked for.
    fn x11_grab_start(&self) -> GrabStartData<Boswas> {
        GrabStartData {
            focus: None,
            button: BTN_LEFT,
            location: self.pointer.current_location(),
        }
    }
}

impl XWaylandShellHandler for Boswas {
    fn xwayland_shell_state(&mut self) -> &mut XWaylandShellState {
        &mut self.xwayland_shell_state
    }

    fn surface_associated(
        &mut self,
        _xwm: XwmId,
        _wl_surface: smithay::reexports::wayland_server::protocol::wl_surface::WlSurface,
        surface: X11Surface,
    ) {
        // The window now has a surface to give the keyboard to.
        if let Some(element) = self.x11_window(&surface) {
            if element.data().wants_focus.replace(false) && self.space.element_location(&element).is_some() {
                self.focus_window(&element);
                self.windows_changed();
            }
        }
        self.schedule_redraw();
    }
}
delegate_xwayland_shell!(Boswas);

impl XwmHandler for Boswas {
    fn xwm_state(&mut self, _xwm: XwmId) -> &mut X11Wm {
        self.xwm.as_mut().expect("X11 events only arrive while the window manager runs")
    }

    fn new_window(&mut self, _xwm: XwmId, _window: X11Surface) {}

    fn new_override_redirect_window(&mut self, _xwm: XwmId, _window: X11Surface) {}

    fn map_window_request(&mut self, _xwm: XwmId, window: X11Surface) {
        if let Err(err) = window.set_mapped(true) {
            warn!("could not map an X11 window: {err}");
            return;
        }
        if self.x11_window(&window).is_some() {
            return;
        }
        let id = self.next_window_id();
        let element = WindowElement::new_x11(window, id, self.workspace);
        self.windows.push(element.clone());
        self.place_new_window(&element);
    }

    fn mapped_override_redirect_window(&mut self, _xwm: XwmId, window: X11Surface) {
        if self.x11_window(&window).is_some() {
            return;
        }
        // A menu or tooltip: shown where it put itself, above everything.
        let location = window.geometry().loc;
        let id = self.next_window_id();
        let element = WindowElement::new_x11(window, id, self.workspace);
        element.data().mapped.set(true);
        element.data().location.set(location);
        self.windows.push(element.clone());
        self.space.map_element(element, location, false);
        self.schedule_redraw();
    }

    fn unmapped_window(&mut self, _xwm: XwmId, window: X11Surface) {
        if let Some(element) = self.x11_window(&window) {
            self.forget_window(&element);
        }
        if !window.is_override_redirect() {
            let _ = window.set_mapped(false);
        }
    }

    fn destroyed_window(&mut self, _xwm: XwmId, window: X11Surface) {
        if let Some(element) = self.x11_window(&window) {
            self.forget_window(&element);
        }
    }

    fn configure_request(
        &mut self,
        _xwm: XwmId,
        window: X11Surface,
        _x: Option<i32>,
        _y: Option<i32>,
        w: Option<u32>,
        h: Option<u32>,
        _reorder: Option<Reorder>,
    ) {
        // An application chooses its size while its window floats. Where
        // the window is, and its size while snapped or maximized, is the
        // window manager's decision: those requests are answered with the
        // geometry that stays.
        let floating = self.x11_window(&window).is_none_or(|element| element.data().tile.get() == Tile::Floating);
        let mut geometry = window.geometry();
        if floating {
            if let Some(w) = w {
                geometry.size.w = (w as i32).clamp(1, 16384);
            }
            if let Some(h) = h {
                geometry.size.h = (h as i32).clamp(1, 16384);
            }
        }
        let _ = window.configure(geometry);
        self.schedule_redraw();
    }

    fn configure_notify(
        &mut self,
        _xwm: XwmId,
        window: X11Surface,
        geometry: Rectangle<i32, Logical>,
        _above: Option<u32>,
    ) {
        // Menus and tooltips move themselves.
        if window.is_override_redirect() {
            if let Some(element) = self.x11_window(&window) {
                element.data().location.set(geometry.loc);
                self.space.map_element(element, geometry.loc, false);
                self.schedule_redraw();
            }
        }
    }

    fn property_notify(&mut self, _xwm: XwmId, _window: X11Surface, property: WmWindowProperty) {
        if matches!(property, WmWindowProperty::Title | WmWindowProperty::Class) {
            self.windows_changed();
        }
    }

    fn maximize_request(&mut self, _xwm: XwmId, window: X11Surface) {
        if let Some(element) = self.x11_window(&window) {
            self.set_tile(&element, Tile::Maximized);
        }
    }

    fn unmaximize_request(&mut self, _xwm: XwmId, window: X11Surface) {
        if let Some(element) = self.x11_window(&window).filter(|e| e.data().tile.get() == Tile::Maximized) {
            self.set_tile(&element, Tile::Floating);
        }
    }

    fn fullscreen_request(&mut self, _xwm: XwmId, window: X11Surface) {
        if let Some(element) = self.x11_window(&window) {
            self.set_tile(&element, Tile::Fullscreen);
        }
    }

    fn unfullscreen_request(&mut self, _xwm: XwmId, window: X11Surface) {
        if let Some(element) = self.x11_window(&window).filter(|e| e.data().tile.get() == Tile::Fullscreen) {
            self.set_tile(&element, Tile::Floating);
        }
    }

    fn minimize_request(&mut self, _xwm: XwmId, window: X11Surface) {
        if let Some(element) = self.x11_window(&window) {
            self.minimize_window(&element);
        }
    }

    fn unminimize_request(&mut self, _xwm: XwmId, window: X11Surface) {
        if let Some(element) = self.x11_window(&window) {
            self.activate_window(&element);
        }
    }

    fn resize_request(&mut self, _xwm: XwmId, window: X11Surface, _button: u32, edges: X11ResizeEdge) {
        let Some(element) = self.x11_window(&window) else {
            return;
        };
        if element.data().tile.get() != Tile::Floating {
            return;
        }
        let edges = match edges {
            X11ResizeEdge::Top => ResizeEdge::TOP,
            X11ResizeEdge::Bottom => ResizeEdge::BOTTOM,
            X11ResizeEdge::Left => ResizeEdge::LEFT,
            X11ResizeEdge::Right => ResizeEdge::RIGHT,
            X11ResizeEdge::TopLeft => ResizeEdge::TOP | ResizeEdge::LEFT,
            X11ResizeEdge::TopRight => ResizeEdge::TOP | ResizeEdge::RIGHT,
            X11ResizeEdge::BottomLeft => ResizeEdge::BOTTOM | ResizeEdge::LEFT,
            X11ResizeEdge::BottomRight => ResizeEdge::BOTTOM | ResizeEdge::RIGHT,
        };
        let start = self.x11_grab_start();
        self.start_resize(&element, start, edges, SERIAL_COUNTER.next_serial());
    }

    fn move_request(&mut self, _xwm: XwmId, window: X11Surface, _button: u32) {
        if let Some(element) = self.x11_window(&window) {
            let start = self.x11_grab_start();
            self.start_move(&element, start, SERIAL_COUNTER.next_serial());
        }
    }

    // The clipboard is shared between X11 and Wayland applications, but an
    // X11 application may only read it while one of its windows has the
    // keyboard, as a Wayland application may.

    fn allow_selection_access(&mut self, xwm: XwmId, _selection: SelectionTarget) -> bool {
        self.focused_window()
            .and_then(|window| window.x11().and_then(X11Surface::xwm_id))
            .is_some_and(|id| id == xwm)
    }

    fn send_selection(&mut self, _xwm: XwmId, selection: SelectionTarget, mime_type: String, fd: OwnedFd) {
        let result = match selection {
            SelectionTarget::Clipboard => {
                request_data_device_client_selection(&self.seat, mime_type, fd).map_err(|e| e.to_string())
            }
            SelectionTarget::Primary => {
                request_primary_client_selection(&self.seat, mime_type, fd).map_err(|e| e.to_string())
            }
        };
        if let Err(err) = result {
            warn!("could not pass the selection to an X11 application: {err}");
        }
    }

    fn new_selection(&mut self, _xwm: XwmId, selection: SelectionTarget, mime_types: Vec<String>) {
        match selection {
            SelectionTarget::Clipboard => set_data_device_selection(&self.display_handle, &self.seat, mime_types, ()),
            SelectionTarget::Primary => set_primary_selection(&self.display_handle, &self.seat, mime_types, ()),
        }
    }

    fn cleared_selection(&mut self, _xwm: XwmId, selection: SelectionTarget) {
        match selection {
            SelectionTarget::Clipboard => {
                if current_data_device_selection_userdata(&self.seat).is_some() {
                    clear_data_device_selection(&self.display_handle, &self.seat);
                }
            }
            SelectionTarget::Primary => {
                if current_primary_selection_userdata(&self.seat).is_some() {
                    clear_primary_selection(&self.display_handle, &self.seat);
                }
            }
        }
    }

    fn disconnected(&mut self, _xwm: XwmId) {
        warn!("the X11 window manager lost its connection; X11 applications are gone");
        self.xwm = None;
        self.x_display = None;
        let gone: Vec<WindowElement> = self.windows.iter().filter(|w| w.is_x11()).cloned().collect();
        for window in gone {
            self.forget_window(&window);
        }
    }
}
