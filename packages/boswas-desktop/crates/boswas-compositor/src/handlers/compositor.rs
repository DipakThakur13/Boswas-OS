//! wl_compositor, wl_shm and linux-dmabuf.

use smithay::{
    backend::{allocator::dmabuf::Dmabuf, renderer::utils::on_commit_buffer_handler},
    delegate_compositor, delegate_dmabuf, delegate_shm,
    desktop::{layer_map_for_output, PopupKind, WindowSurfaceType},
    reexports::wayland_server::{
        protocol::{wl_buffer, wl_surface::WlSurface},
        Client,
    },
    wayland::{
        buffer::BufferHandler,
        compositor::{is_sync_subsurface, CompositorClientState, CompositorHandler, CompositorState},
        dmabuf::{DmabufGlobal, DmabufHandler, DmabufState, ImportNotifier},
        shell::wlr_layer::LayerSurfaceData,
        shm::{ShmHandler, ShmState},
    },
    xwayland::XWaylandClientData,
};

use crate::{
    shell::grabs,
    state::{root_surface, Boswas, ClientState},
};

impl CompositorHandler for Boswas {
    fn compositor_state(&mut self) -> &mut CompositorState {
        &mut self.compositor_state
    }

    fn client_compositor_state<'a>(&self, client: &'a Client) -> &'a CompositorClientState {
        // The XWayland server is a client the X11 support created.
        if let Some(xwayland) = client.get_data::<XWaylandClientData>() {
            return &xwayland.compositor_state;
        }
        &client
            .get_data::<ClientState>()
            .expect("every other client is inserted with ClientState")
            .compositor_state
    }

    fn commit(&mut self, surface: &WlSurface) {
        on_commit_buffer_handler::<Self>(surface);
        self.schedule_redraw();

        if !is_sync_subsurface(surface) {
            let root = root_surface(surface);
            if let Some(window) = self.window_for_surface(&root) {
                window.0.on_commit();
                if &root == surface {
                    self.window_committed(&window);
                }
            }
        }

        self.popups.commit(surface);
        if let Some(PopupKind::Xdg(popup)) = self.popups.find_popup(surface) {
            if !popup.is_initial_configure_sent() {
                // The first configure of a popup is always allowed.
                let _ = popup.send_configure();
            }
        }

        grabs::handle_commit(self, surface);
        self.layer_committed(surface);
        self.lock_surface_committed(surface);
    }
}

impl Boswas {
    /// Arranges layer surfaces after a commit and sends their first
    /// configure.
    fn layer_committed(&mut self, surface: &WlSurface) {
        let Some(output) = self
            .space
            .outputs()
            .find(|o| {
                layer_map_for_output(o)
                    .layer_for_surface(surface, WindowSurfaceType::TOPLEVEL)
                    .is_some()
            })
            .cloned()
        else {
            return;
        };
        let initial_configure_sent = smithay::wayland::compositor::with_states(surface, |states| {
            states
                .data_map
                .get::<LayerSurfaceData>()
                .is_some_and(|data| data.lock().is_ok_and(|data| data.initial_configure_sent))
        });
        let changed = {
            let mut map = layer_map_for_output(&output);
            let changed = map.arrange();
            if !initial_configure_sent {
                if let Some(layer) = map.layer_for_surface(surface, WindowSurfaceType::TOPLEVEL) {
                    layer.layer_surface().send_configure();
                }
            }
            changed
        };
        if changed {
            self.usable_area_changed(&output);
        }
        self.update_keyboard_focus();
    }
}

impl BufferHandler for Boswas {
    fn buffer_destroyed(&mut self, _buffer: &wl_buffer::WlBuffer) {}
}

impl ShmHandler for Boswas {
    fn shm_state(&self) -> &ShmState {
        &self.shm_state
    }
}

impl DmabufHandler for Boswas {
    fn dmabuf_state(&mut self) -> &mut DmabufState {
        &mut self.dmabuf_state
    }

    fn dmabuf_imported(&mut self, _global: &DmabufGlobal, dmabuf: Dmabuf, notifier: ImportNotifier) {
        if self.backend.import_dmabuf(&dmabuf) {
            let _ = notifier.successful::<Boswas>();
        } else {
            notifier.failed();
        }
    }
}

delegate_compositor!(Boswas);
delegate_shm!(Boswas);
delegate_dmabuf!(Boswas);
