//! Assembling the picture of one output.
//!
//! Front to back: pointer, drag icon, snap preview, then either the lock
//! surface (nothing else is drawn while the session is locked), a
//! fullscreen window under the overlay layer, or the desktop: overlay and
//! top layers, windows with their frames, bottom and background layers.

use std::time::Duration;

use smithay::{
    backend::renderer::{
        element::{
            memory::MemoryRenderBufferRenderElement,
            solid::{SolidColorBuffer, SolidColorRenderElement},
            surface::{render_elements_from_surface_tree, WaylandSurfaceRenderElement},
            AsRenderElements, Kind, RenderElement, RenderElementStates, Wrap,
        },
        Color32F, ImportAll, ImportMem, Renderer, Texture,
    },
    desktop::{
        layer_map_for_output,
        space::{space_render_elements, SpaceRenderElements},
        utils::{
            surface_presentation_feedback_flags_from_states, surface_primary_scanout_output,
            update_surface_primary_scanout_output, OutputPresentationFeedback,
        },
    },
    input::pointer::{CursorImageStatus, CursorImageSurfaceData},
    output::Output,
    reexports::wayland_server::protocol::wl_surface::WlSurface,
    render_elements,
    utils::{IsAlive, Point, Scale},
    wayland::{
        compositor::{with_states, with_surface_tree_downward, SurfaceAttributes, TraversalAction},
        fractional_scale::with_fractional_scale,
        shell::wlr_layer::Layer,
    },
};

use crate::{
    shell::element::{WindowElement, WindowRenderElement},
    state::Boswas,
};

render_elements! {
    pub OutputElement<R, E> where R: ImportAll + ImportMem;
    Space=SpaceRenderElements<R, E>,
    Window=Wrap<E>,
    Surface=WaylandSurfaceRenderElement<R>,
    Memory=MemoryRenderBufferRenderElement<R>,
    Solid=SolidColorRenderElement,
}

/// The elements of one output.
pub type OutputRenderElement<R> = OutputElement<R, WindowRenderElement<R>>;

impl<R: Renderer + ImportAll + ImportMem, E: RenderElement<R>> std::fmt::Debug for OutputElement<R, E> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Space(_) => "Space",
            Self::Window(_) => "Window",
            Self::Surface(_) => "Surface",
            Self::Memory(_) => "Memory",
            Self::Solid(_) => "Solid",
            Self::_GenericCatcher(_) => "_GenericCatcher",
        })
    }
}

/// Overlays the compositor draws itself.
#[derive(Default)]
pub struct Overlays {
    snap_preview: SolidColorBuffer,
}

impl Boswas {
    /// The render elements of an output, front to back, and its clear
    /// colour. Call [`Boswas::update_decorations`] first.
    pub fn output_elements<R>(&mut self, renderer: &mut R, output: &Output) -> (Vec<OutputRenderElement<R>>, Color32F)
    where
        R: Renderer + ImportAll + ImportMem,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let scale = Scale::from(output.current_scale().fractional_scale());
        let Some(output_geo) = self.space.output_geometry(output) else {
            return (Vec::new(), Color32F::BLACK);
        };
        let background = Color32F::from(self.theme.background.to_f32());
        let mut elements: Vec<OutputRenderElement<R>> = Vec::new();

        self.cursor_elements(renderer, output, scale, &mut elements);

        if self.lock.locked {
            // Only the lock screen. Without a lock surface the output is
            // blank in the theme's background colour.
            if let Some(surface) = self.lock.surfaces.get(output).filter(|s| s.alive()) {
                elements.extend(render_elements_from_surface_tree(
                    renderer,
                    surface.wl_surface(),
                    (0, 0),
                    scale,
                    1.0,
                    Kind::Unspecified,
                ));
            }
            return (elements, background);
        }

        if let Some(rect) = self.snap_preview.filter(|rect| rect.overlaps(output_geo)) {
            let accent = self.theme.accent;
            self.overlays.snap_preview.update(rect.size, Color32F::from(accent.alpha(0.28).to_premultiplied_f32()));
            let location = (rect.loc - output_geo.loc).to_physical_precise_round(scale);
            elements.push(OutputRenderElement::Solid(SolidColorRenderElement::from_buffer(
                &self.overlays.snap_preview,
                location,
                scale,
                1.0,
                Kind::Unspecified,
            )));
        }

        if let Some(window) = self.fullscreen_window(output) {
            let layers = layer_map_for_output(output);
            for layer in layers.layers_on(Layer::Overlay).rev() {
                if let Some(geo) = layers.layer_geometry(layer) {
                    elements.extend(
                        AsRenderElements::<R>::render_elements::<WaylandSurfaceRenderElement<R>>(
                            layer,
                            renderer,
                            geo.loc.to_physical_precise_round(scale),
                            scale,
                            1.0,
                        )
                        .into_iter()
                        .map(OutputRenderElement::Surface),
                    );
                }
            }
            if let Some(origin) = self.window_origin(&window) {
                let location = (origin - output_geo.loc).to_physical_precise_round(scale);
                elements.extend(
                    AsRenderElements::<R>::render_elements::<WindowRenderElement<R>>(&window, renderer, location, scale, 1.0)
                        .into_iter()
                        .map(|e| OutputRenderElement::Window(Wrap::from(e))),
                );
            }
            return (elements, Color32F::BLACK);
        }

        match space_render_elements::<_, WindowElement, _>(renderer, [&self.space], output, 1.0) {
            Ok(space) => elements.extend(space.into_iter().map(OutputRenderElement::Space)),
            Err(_) => tracing::warn!("output {} has no mode; nothing to draw", output.name()),
        }
        (elements, background)
    }

    fn cursor_elements<R>(
        &mut self,
        renderer: &mut R,
        output: &Output,
        scale: Scale<f64>,
        elements: &mut Vec<OutputRenderElement<R>>,
    ) where
        R: Renderer + ImportAll + ImportMem,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let Some(output_geo) = self.space.output_geometry(output) else {
            return;
        };
        let pointer = self.pointer.current_location();
        if !output_geo.to_f64().contains(pointer) {
            return;
        }
        let local = pointer - output_geo.loc.to_f64();

        // A client's cursor surface may have died with the client.
        if let CursorImageStatus::Surface(surface) = &self.cursor_status {
            if !surface.alive() {
                self.cursor_status = CursorImageStatus::default_named();
            }
        }
        match &self.cursor_status {
            CursorImageStatus::Hidden => {}
            CursorImageStatus::Surface(surface) => {
                let hotspot = with_states(surface, |states| {
                    states
                        .data_map
                        .get::<CursorImageSurfaceData>()
                        .and_then(|data| data.lock().ok().map(|data| data.hotspot))
                        .unwrap_or_default()
                });
                let location = (local - hotspot.to_f64()).to_physical_precise_round(scale);
                elements.extend(render_elements_from_surface_tree(
                    renderer,
                    surface,
                    location,
                    scale,
                    1.0,
                    Kind::Cursor,
                ));
            }
            CursorImageStatus::Named(icon) => {
                let frame = self.cursor_theme.frame(*icon, self.start_time.elapsed());
                let location = (local - frame.hotspot.to_f64()).to_physical_precise_round::<_, i32>(scale);
                if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                    renderer,
                    location.to_f64(),
                    &frame.buffer,
                    None,
                    None,
                    None,
                    Kind::Cursor,
                ) {
                    elements.push(OutputRenderElement::Memory(element));
                }
            }
        }

        if let Some(icon) = self.dnd_icon.as_ref().filter(|icon| icon.alive()) {
            let location: Point<i32, _> = local.to_physical_precise_round(scale);
            elements.extend(render_elements_from_surface_tree(
                renderer,
                icon,
                location,
                scale,
                1.0,
                Kind::Unspecified,
            ));
        }
    }

    /// Every surface tree that is shown on an output.
    fn for_each_surface_on(&self, output: &Output, mut f: impl FnMut(&WlSurface)) {
        if self.lock.locked {
            if let Some(surface) = self.lock.surfaces.get(output) {
                f(surface.wl_surface());
            }
        } else {
            for window in self.space.elements_for_output(output) {
                f(window.wl_surface());
            }
            for layer in layer_map_for_output(output).layers() {
                f(layer.wl_surface());
            }
        }
        if let CursorImageStatus::Surface(surface) = &self.cursor_status {
            f(surface);
        }
        if let Some(icon) = &self.dnd_icon {
            f(icon);
        }
    }

    /// After an output was rendered: records which output each surface is
    /// shown on, tells clients their preferred scale, and sends frame
    /// callbacks so clients draw their next frame.
    pub fn post_repaint(&mut self, output: &Output, states: &RenderElementStates, time: Duration) {
        let throttle = Some(Duration::from_millis(995));
        let scale = output.current_scale().fractional_scale();
        self.for_each_surface_on(output, |root| {
            with_surface_tree_downward(
                root,
                (),
                |_, _, _| TraversalAction::DoChildren(()),
                |surface, surface_states, _| {
                    update_surface_primary_scanout_output(
                        surface,
                        output,
                        surface_states,
                        states,
                        smithay::backend::renderer::element::default_primary_scanout_output_compare,
                    );
                    with_fractional_scale(surface_states, |fractional| fractional.set_preferred_scale(scale));
                },
                |_, _, _| true,
            );
        });

        if self.lock.locked {
            if let Some(surface) = self.lock.surfaces.get(output) {
                send_frames_surface_tree(surface.wl_surface(), output, time, throttle);
            }
        } else {
            for window in self.space.elements_for_output(output) {
                window.send_frame(output, time, throttle, surface_primary_scanout_output);
            }
            for layer in layer_map_for_output(output).layers() {
                layer.send_frame(output, time, throttle, surface_primary_scanout_output);
            }
            // Popups of windows are covered by `Window::send_frame`.
        }
        if let CursorImageStatus::Surface(surface) = &self.cursor_status {
            send_frames_surface_tree(surface, output, time, throttle);
        }
        if let Some(icon) = &self.dnd_icon {
            send_frames_surface_tree(icon, output, time, throttle);
        }
    }

    /// Collects the presentation-time feedback requested for this frame.
    pub fn take_presentation_feedback(&self, output: &Output, states: &RenderElementStates) -> OutputPresentationFeedback {
        let mut feedback = OutputPresentationFeedback::new(output);
        if self.lock.locked {
            return feedback;
        }
        for window in self.space.elements_for_output(output) {
            window.take_presentation_feedback(&mut feedback, surface_primary_scanout_output, |surface, _| {
                surface_presentation_feedback_flags_from_states(surface, states)
            });
        }
        for layer in layer_map_for_output(output).layers() {
            layer.take_presentation_feedback(&mut feedback, surface_primary_scanout_output, |surface, _| {
                surface_presentation_feedback_flags_from_states(surface, states)
            });
        }
        feedback
    }

    /// Whether something on screen animates without client commits (an
    /// animated cursor), so outputs must keep producing frames.
    pub fn needs_continuous_frames(&self) -> bool {
        matches!(&self.cursor_status, CursorImageStatus::Named(icon) if self.cursor_theme.is_animated(*icon))
    }
}

/// Sends the frame callbacks of a surface tree that is not a window.
fn send_frames_surface_tree(surface: &WlSurface, output: &Output, time: Duration, throttle: Option<Duration>) {
    let _ = (output, throttle);
    with_surface_tree_downward(
        surface,
        (),
        |_, _, _| TraversalAction::DoChildren(()),
        |_, states, _| {
            let callbacks: Vec<_> = states
                .cached_state
                .get::<SurfaceAttributes>()
                .current()
                .frame_callbacks
                .drain(..)
                .collect();
            for callback in callbacks {
                callback.done(time.as_millis() as u32);
            }
        },
        |_, _, _| true,
    );
}
