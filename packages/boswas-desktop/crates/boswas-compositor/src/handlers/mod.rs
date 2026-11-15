//! Wayland protocol handlers.

mod compositor;
mod layer_shell;
mod session_lock;
mod xdg_shell;

use std::{os::fd::OwnedFd, sync::Arc};

use smithay::{
    backend::input::TabletToolDescriptor,
    delegate_cursor_shape, delegate_data_control, delegate_data_device, delegate_foreign_toplevel_list,
    delegate_fractional_scale, delegate_idle_inhibit, delegate_keyboard_shortcuts_inhibit, delegate_output,
    delegate_pointer_constraints, delegate_pointer_gestures, delegate_presentation, delegate_primary_selection, delegate_relative_pointer,
    delegate_seat, delegate_security_context, delegate_single_pixel_buffer, delegate_viewporter,
    delegate_xdg_activation,
    desktop::utils::surface_primary_scanout_output,
    input::{
        pointer::{CursorImageStatus, PointerHandle},
        Seat, SeatHandler, SeatState,
    },
    reexports::wayland_server::{
        protocol::{wl_data_source::WlDataSource, wl_surface::WlSurface},
        Resource,
    },
    utils::{Logical, Point},
    wayland::{
        compositor::with_states,
        foreign_toplevel_list::{ForeignToplevelListHandler, ForeignToplevelListState},
        fractional_scale::{with_fractional_scale, FractionalScaleHandler},
        idle_inhibit::IdleInhibitHandler,
        keyboard_shortcuts_inhibit::{
            KeyboardShortcutsInhibitHandler, KeyboardShortcutsInhibitState, KeyboardShortcutsInhibitor,
        },
        output::OutputHandler,
        pointer_constraints::{with_pointer_constraint, PointerConstraintsHandler},
        security_context::{SecurityContext, SecurityContextHandler, SecurityContextListenerSource},
        selection::{
            data_device::{
                set_data_device_focus, ClientDndGrabHandler, DataDeviceHandler, DataDeviceState,
                ServerDndGrabHandler,
            },
            primary_selection::{set_primary_focus, PrimarySelectionHandler, PrimarySelectionState},
            wlr_data_control::{DataControlHandler, DataControlState},
            SelectionHandler,
        },
        tablet_manager::TabletSeatHandler,
        xdg_activation::{XdgActivationHandler, XdgActivationState, XdgActivationToken, XdgActivationTokenData},
    },
};
use tracing::warn;

use crate::state::{root_surface, Boswas, ClientState, Trust};

// ---------------------------------------------------------------------------
// Seat, selections
// ---------------------------------------------------------------------------

impl SeatHandler for Boswas {
    type KeyboardFocus = WlSurface;
    type PointerFocus = WlSurface;
    type TouchFocus = WlSurface;

    fn seat_state(&mut self) -> &mut SeatState<Boswas> {
        &mut self.seat_state
    }

    fn cursor_image(&mut self, _seat: &Seat<Self>, image: CursorImageStatus) {
        self.cursor_status = image;
        self.schedule_redraw();
    }

    fn focus_changed(&mut self, seat: &Seat<Self>, focused: Option<&WlSurface>) {
        let dh = &self.display_handle;
        let client = focused.and_then(|s| dh.get_client(s.id()).ok());
        set_data_device_focus(dh, seat, client.clone());
        set_primary_focus(dh, seat, client);
    }

    fn led_state_changed(&mut self, _seat: &Seat<Self>, led_state: smithay::input::keyboard::LedState) {
        self.backend.update_leds(led_state);
    }
}
delegate_seat!(Boswas);

impl TabletSeatHandler for Boswas {
    fn tablet_tool_image(&mut self, _tool: &TabletToolDescriptor, image: CursorImageStatus) {
        self.cursor_status = image;
    }
}
delegate_cursor_shape!(Boswas);

impl SelectionHandler for Boswas {
    type SelectionUserData = ();
}

impl DataDeviceHandler for Boswas {
    fn data_device_state(&self) -> &DataDeviceState {
        &self.data_device_state
    }
}

impl ClientDndGrabHandler for Boswas {
    fn started(&mut self, _source: Option<WlDataSource>, icon: Option<WlSurface>, _seat: Seat<Self>) {
        self.dnd_icon = icon;
        self.schedule_redraw();
    }

    fn dropped(&mut self, _target: Option<WlSurface>, _validated: bool, _seat: Seat<Self>) {
        self.dnd_icon = None;
        self.schedule_redraw();
    }
}

impl ServerDndGrabHandler for Boswas {
    fn send(&mut self, _mime_type: String, _fd: OwnedFd, _seat: Seat<Self>) {}
}
delegate_data_device!(Boswas);

impl PrimarySelectionHandler for Boswas {
    fn primary_selection_state(&self) -> &PrimarySelectionState {
        &self.primary_selection_state
    }
}
delegate_primary_selection!(Boswas);

impl DataControlHandler for Boswas {
    fn data_control_state(&self) -> &DataControlState {
        &self.data_control_state
    }
}
delegate_data_control!(Boswas);

// ---------------------------------------------------------------------------
// Outputs, scaling, presentation
// ---------------------------------------------------------------------------

impl OutputHandler for Boswas {}
delegate_output!(Boswas);
delegate_viewporter!(Boswas);
delegate_presentation!(Boswas);
delegate_single_pixel_buffer!(Boswas);

impl FractionalScaleHandler for Boswas {
    fn new_fractional_scale(&mut self, surface: WlSurface) {
        let root = root_surface(&surface);
        let output = with_states(&surface, |states| surface_primary_scanout_output(&surface, states))
            .or_else(|| {
                self.window_for_surface(&root)
                    .and_then(|window| self.space.outputs_for_element(&window).first().cloned())
            })
            .or_else(|| self.space.outputs().next().cloned());
        if let Some(output) = output {
            with_states(&surface, |states| {
                with_fractional_scale(states, |fractional| {
                    fractional.set_preferred_scale(output.current_scale().fractional_scale());
                });
            });
        }
    }
}
delegate_fractional_scale!(Boswas);

// ---------------------------------------------------------------------------
// Input extras
// ---------------------------------------------------------------------------

delegate_relative_pointer!(Boswas);
delegate_pointer_gestures!(Boswas);

impl PointerConstraintsHandler for Boswas {
    fn new_constraint(&mut self, surface: &WlSurface, pointer: &PointerHandle<Self>) {
        if pointer.current_focus().as_ref() == Some(surface) {
            with_pointer_constraint(surface, pointer, |constraint| {
                if let Some(constraint) = constraint {
                    constraint.activate();
                }
            });
        }
    }

    fn cursor_position_hint(&mut self, surface: &WlSurface, pointer: &PointerHandle<Self>, location: Point<f64, Logical>) {
        let active = with_pointer_constraint(surface, pointer, |c| c.is_some_and(|c| c.is_active()));
        if !active {
            return;
        }
        let root = root_surface(surface);
        if let Some(origin) = self.window_for_surface(&root).and_then(|w| self.window_origin(&w)) {
            let bar = self.window_for_surface(&root).map_or(0, |w| w.titlebar_height());
            pointer.set_location(origin.to_f64() + Point::from((0.0, f64::from(bar))) + location);
        }
    }
}
delegate_pointer_constraints!(Boswas);

impl KeyboardShortcutsInhibitHandler for Boswas {
    fn keyboard_shortcuts_inhibit_state(&mut self) -> &mut KeyboardShortcutsInhibitState {
        &mut self.shortcuts_inhibit_state
    }

    fn new_inhibitor(&mut self, inhibitor: KeyboardShortcutsInhibitor) {
        // Remote desktops and virtual machines may take the window
        // management shortcuts; lock, VT switching and Super stay with the
        // compositor (see input.rs).
        inhibitor.activate();
    }
}
delegate_keyboard_shortcuts_inhibit!(Boswas);

impl IdleInhibitHandler for Boswas {
    fn inhibit(&mut self, surface: WlSurface) {
        if !self.idle_inhibitors.contains(&surface) {
            self.idle_inhibitors.push(surface);
        }
    }

    fn uninhibit(&mut self, surface: WlSurface) {
        self.idle_inhibitors.retain(|s| s != &surface);
    }
}
delegate_idle_inhibit!(Boswas);

// ---------------------------------------------------------------------------
// Activation, window list
// ---------------------------------------------------------------------------

impl XdgActivationHandler for Boswas {
    fn activation_state(&mut self) -> &mut XdgActivationState {
        &mut self.xdg_activation_state
    }

    fn token_created(&mut self, _token: XdgActivationToken, data: XdgActivationTokenData) -> bool {
        // A token is only good when it comes from the client the user is
        // interacting with: its serial must be at least as new as the last
        // keyboard enter. This is what keeps windows from stealing focus.
        let Some((serial, seat)) = data.serial else {
            return false;
        };
        Seat::from_resource(&seat).as_ref() == Some(&self.seat)
            && self
                .keyboard
                .last_enter()
                .is_some_and(|last_enter| serial.is_no_older_than(&last_enter))
    }

    fn request_activation(&mut self, token: XdgActivationToken, data: XdgActivationTokenData, surface: WlSurface) {
        if data.timestamp.elapsed().as_secs() < 10 {
            if let Some(window) = self.window_for_surface(&root_surface(&surface)) {
                self.activate_window(&window);
            }
        }
        self.xdg_activation_state.remove_token(&token);
    }
}
delegate_xdg_activation!(Boswas);

impl ForeignToplevelListHandler for Boswas {
    fn foreign_toplevel_list_state(&mut self) -> &mut ForeignToplevelListState {
        &mut self.foreign_toplevel_state
    }
}
delegate_foreign_toplevel_list!(Boswas);

// ---------------------------------------------------------------------------
// Sandboxed clients
// ---------------------------------------------------------------------------

impl SecurityContextHandler for Boswas {
    fn context_created(&mut self, source: SecurityContextListenerSource, context: SecurityContext) {
        let result = self.loop_handle.insert_source(source, move |stream, _, state| {
            let mut client = ClientState::new(Trust::Sandboxed);
            client.security_context = Some(context.clone());
            if let Err(err) = state.display_handle.insert_client(stream, Arc::new(client)) {
                warn!("could not accept a sandboxed Wayland client: {err}");
            }
        });
        if let Err(err) = result {
            warn!("could not listen on a security context socket: {err}");
        }
    }
}
delegate_security_context!(Boswas);
