//! wlr-layer-shell: the panel, the launcher, wallpapers and overlays.
//!
//! The global is only visible to trusted clients (see `state.rs`).

use smithay::{
    delegate_layer_shell,
    desktop::{layer_map_for_output, LayerSurface, PopupKind},
    output::Output,
    reexports::wayland_server::protocol::wl_output::WlOutput,
    wayland::shell::{
        wlr_layer::{Layer, LayerSurface as WlrLayerSurface, WlrLayerShellHandler, WlrLayerShellState},
        xdg::PopupSurface,
    },
};
use tracing::warn;

use crate::state::Boswas;

impl WlrLayerShellHandler for Boswas {
    fn shell_state(&mut self) -> &mut WlrLayerShellState {
        &mut self.layer_shell_state
    }

    fn new_layer_surface(&mut self, surface: WlrLayerSurface, output: Option<WlOutput>, _layer: Layer, namespace: String) {
        let output = output
            .as_ref()
            .and_then(Output::from_resource)
            .or_else(|| self.active_output());
        let Some(output) = output else {
            surface.send_close();
            return;
        };
        let mut map = layer_map_for_output(&output);
        if let Err(err) = map.map_layer(&LayerSurface::new(surface, namespace)) {
            warn!("could not map a layer surface: {err}");
        }
    }

    fn new_popup(&mut self, _parent: WlrLayerSurface, popup: PopupSurface) {
        if let Err(err) = self.popups.track_popup(PopupKind::Xdg(popup)) {
            warn!("could not track a layer popup: {err}");
        }
    }

    fn layer_destroyed(&mut self, surface: WlrLayerSurface) {
        let outputs: Vec<Output> = self.space.outputs().cloned().collect();
        for output in outputs {
            let found = {
                let mut map = layer_map_for_output(&output);
                let layer = map.layers().find(|l| l.layer_surface() == &surface).cloned();
                if let Some(layer) = &layer {
                    map.unmap_layer(layer);
                }
                layer.is_some()
            };
            if found {
                self.usable_area_changed(&output);
            }
        }
        self.update_keyboard_focus();
        self.schedule_redraw();
    }
}
delegate_layer_shell!(Boswas);
