//! Assembling the picture of one output.
//!
//! Front to back: pointer, drag icon, then either the lock surface (nothing
//! else is drawn while the session is locked) or the desktop: the window
//! switcher, the snap preview, the overlay layer, a fullscreen window if
//! there is one, the top layer (the taskbar), Task View or the windows with
//! their frames, and the bottom and background layers (the wallpaper).

use std::time::{Duration, Instant};

use smithay::{
    backend::renderer::{
        element::{
            memory::MemoryRenderBufferRenderElement,
            solid::{SolidColorBuffer, SolidColorRenderElement},
            surface::{render_elements_from_surface_tree, WaylandSurfaceRenderElement},
            utils::{Relocate, RelocateRenderElement, RescaleRenderElement},
            AsRenderElements, Kind, RenderElement, RenderElementStates, Wrap,
        },
        Color32F, Renderer, Texture,
    },
    desktop::{
        layer_map_for_output,
        utils::{
            surface_presentation_feedback_flags_from_states, surface_primary_scanout_output,
            update_surface_primary_scanout_output, OutputPresentationFeedback,
        },
    },
    input::pointer::{CursorImageStatus, CursorImageSurfaceData},
    output::Output,
    reexports::wayland_server::protocol::wl_surface::WlSurface,
    render_elements,
    utils::{IsAlive, Logical, Physical, Point, Rectangle, Scale, Size},
    wayland::{
        compositor::{with_states, with_surface_tree_downward, SurfaceAttributes, TraversalAction},
        fractional_scale::with_fractional_scale,
        shell::wlr_layer::Layer,
    },
};

use crate::{
    effects::{BoswasRenderer, ColorMatrix, FilteredFrame},
    shell::element::{WindowElement, WindowRenderElement},
    state::Boswas,
};

/// A window drawn somewhere else, or at another size, than where it is.
pub type MovedElement<E> = RelocateRenderElement<RescaleRenderElement<E>>;

render_elements! {
    pub OutputElement<R, E> where R: BoswasRenderer;
    Window=Wrap<E>,
    Moved=MovedElement<E>,
    Surface=WaylandSurfaceRenderElement<R>,
    Memory=MemoryRenderBufferRenderElement<R>,
    Solid=SolidColorRenderElement,
    Blur=crate::glass::blur::BlurElement<R>,
}

/// The elements of one output.
pub type OutputRenderElement<R> = OutputElement<R, WindowRenderElement<R>>;

render_elements! {
    /// What goes on screen: the scene, enlarged by the magnifier, or the
    /// whole of it drawn through a colour filter.
    pub DisplayElement<R, E> where R: BoswasRenderer;
    Scene=Wrap<E>,
    Zoomed=MovedElement<E>,
    Filtered=FilteredFrame<R>,
}

/// The elements a backend shows on an output.
pub type DisplayRenderElement<R> = DisplayElement<R, OutputRenderElement<R>>;

impl<R: Renderer + BoswasRenderer, E: RenderElement<R>> std::fmt::Debug for DisplayElement<R, E>
where
    R::TextureId: Texture + 'static,
{
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Scene(_) => "Scene",
            Self::Zoomed(_) => "Zoomed",
            Self::Filtered(_) => "Filtered",
            Self::_GenericCatcher(_) => "_GenericCatcher",
        })
    }
}

/// Where the magnifier shows an output: the picture enlarged `zoom` times
/// about the pointer, which stays where it is on the screen (so the view
/// never leaves the screen's edges). Returns the offset that moves the
/// enlarged picture into place, in physical pixels.
pub fn magnifier_offset(pointer: Point<f64, Physical>, size: Size<i32, Physical>, zoom: f64) -> Point<i32, Physical> {
    let x = pointer.x.clamp(0.0, f64::from(size.w));
    let y = pointer.y.clamp(0.0, f64::from(size.h));
    Point::from((-(x * (zoom - 1.0)).round() as i32, -(y * (zoom - 1.0)).round() as i32))
}

impl<R: Renderer + BoswasRenderer, E: RenderElement<R>> std::fmt::Debug for OutputElement<R, E>
where
    R::TextureId: Texture + 'static,
{
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Window(_) => "Window",
            Self::Moved(_) => "Moved",
            Self::Surface(_) => "Surface",
            Self::Memory(_) => "Memory",
            Self::Solid(_) => "Solid",
            Self::Blur(_) => "Blur",
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
    /// Brings everything the next frame shows up to date: frame artwork,
    /// and animations that have run their course. Backends call this
    /// before [`Boswas::output_elements`].
    pub fn prepare_frame(&mut self) {
        let now = Instant::now();
        self.ghosts.retain(|ghost| !ghost.timeline.finished(now));
        self.overview_tick();
        self.snap_flyout_tick();
        self.update_decorations();
    }

    /// Whether animations are switched off.
    pub fn reduced_motion(&self) -> bool {
        self.config.appearance.reduced_motion
    }

    /// The colour filter in use, if any.
    pub fn active_color_filter(&self) -> Option<ColorMatrix> {
        self.color_filter_on.then(|| ColorMatrix::named(&self.config.color_filter.filter)).flatten()
    }

    /// What a backend shows on an output: [`Boswas::output_elements`],
    /// enlarged around the pointer while the magnifier is on, and drawn
    /// through the colour filter where the renderer can. Pictures taken of
    /// the screen (screenshots, screen capture) use the scene itself, as
    /// the filters and the magnifier are for the person in front of it.
    pub fn display_elements<R>(&mut self, renderer: &mut R, output: &Output) -> (Vec<DisplayRenderElement<R>>, Color32F)
    where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let (scene, clear) = self.output_elements(renderer, output);
        let scale = output.current_scale().fractional_scale();
        let size = output.current_mode().map(|mode| output.current_transform().transform_size(mode.size)).unwrap_or_default();
        let pointer = self.pointer.current_location();
        let geo = self.space.output_geometry(output);
        let zoom = self.magnifier_zoom;
        let elements: Vec<DisplayRenderElement<R>> = match geo.filter(|geo| zoom > 1.0 && geo.to_f64().contains(pointer)) {
            Some(geo) => {
                let local = (pointer - geo.loc.to_f64()).to_physical(scale);
                let offset = magnifier_offset(local, size, zoom);
                scene
                    .into_iter()
                    .map(|element| {
                        let enlarged = RescaleRenderElement::from_element(element, Point::from((0, 0)), zoom);
                        DisplayElement::Zoomed(RelocateRenderElement::from_element(enlarged, offset, Relocate::Relative))
                    })
                    .collect()
            }
            None => scene.into_iter().map(|element| DisplayElement::Scene(Wrap::from(element))).collect(),
        };
        if let Some(filter) = self.active_color_filter().filter(|_| R::FILTERS_COLOR) {
            if let Some(texture) = renderer.color_filter_frame(size, Scale::from(scale), &elements, clear) {
                self.filter_commit.increment();
                let frame = FilteredFrame::new(texture, filter, size, self.filter_id.clone(), self.filter_commit);
                return (vec![DisplayElement::Filtered(frame)], clear);
            }
        }
        (elements, clear)
    }

    /// The render elements of an output, front to back, and its clear
    /// colour. Call [`Boswas::prepare_frame`] first.
    pub fn output_elements<R>(&mut self, renderer: &mut R, output: &Output) -> (Vec<OutputRenderElement<R>>, Color32F)
    where
        R: BoswasRenderer,
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

        self.switcher_elements(renderer, output, scale, &mut elements);
        self.window_menu_elements(renderer, output, scale, &mut elements);
        self.snap_flyout_elements(renderer, output, scale, &mut elements);

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

        layer_elements(renderer, output, Layer::Overlay, scale, &mut elements);

        let fullscreen = self.fullscreen_window(output).filter(|_| self.overview.is_none());
        if let Some(window) = fullscreen {
            if let Some(origin) = self.window_origin(&window) {
                let location = (origin - output_geo.loc).to_physical_precise_round(scale);
                elements.extend(
                    window
                        .elements(renderer, location, scale, 1.0, false)
                        .into_iter()
                        .map(|e| OutputRenderElement::Window(Wrap::from(e))),
                );
            }
            self.blur_behind_surfaces(renderer, output, scale, &mut elements);
            return (elements, Color32F::BLACK);
        }

        layer_elements(renderer, output, Layer::Top, scale, &mut elements);
        if self.overview.is_some() {
            self.overview_elements(renderer, output, scale, &mut elements);
        } else {
            self.window_elements(renderer, output, scale, &mut elements);
        }
        layer_elements(renderer, output, Layer::Bottom, scale, &mut elements);
        layer_elements(renderer, output, Layer::Background, scale, &mut elements);
        self.blur_behind_surfaces(renderer, output, scale, &mut elements);
        (elements, background)
    }

    /// The windows of an output, and in front of them the pictures of the
    /// windows that are on their way out.
    fn window_elements<R>(
        &mut self,
        renderer: &mut R,
        output: &Output,
        scale: Scale<f64>,
        elements: &mut Vec<OutputRenderElement<R>>,
    ) where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let Some(output_geo) = self.space.output_geometry(output) else {
            return;
        };
        let now = Instant::now();
        let reduced = self.reduced_motion();

        for ghost in &self.ghosts {
            let Some(pose) = ghost.pose(now) else {
                continue;
            };
            if !pose.rect.overlaps(output_geo.to_f64()) || pose.rect.size.w < 1.0 || pose.rect.size.h < 1.0 {
                continue;
            }
            let location: Point<f64, Physical> = (pose.rect.loc - output_geo.loc.to_f64()).to_physical(scale);
            let size: Size<i32, Logical> = Size::from((pose.rect.size.w.round() as i32, pose.rect.size.h.round() as i32));
            if let Ok(element) = MemoryRenderBufferRenderElement::from_buffer(
                renderer,
                location,
                &ghost.buffer,
                Some(pose.alpha),
                None,
                Some(size),
                Kind::Unspecified,
            ) {
                elements.push(OutputRenderElement::Memory(element));
            }
        }

        let windows: Vec<WindowElement> = self.space.elements_for_output(output).cloned().collect();
        for window in windows.iter().rev() {
            let (Some(origin), Some(rect)) = (self.window_origin(window), self.window_rect(window)) else {
                continue;
            };
            let animation = window.data().animation.get().filter(|_| !reduced);
            match animation.and_then(|animation| animation.pose(rect.to_f64(), now)) {
                Some(pose) => {
                    let mut local = pose.rect;
                    local.loc -= output_geo.loc.to_f64();
                    elements.extend(
                        placed(renderer, window, local, scale, pose.alpha, true)
                            .into_iter()
                            .map(OutputRenderElement::Moved),
                    );
                }
                None => {
                    window.data().animation.set(None);
                    let location = (origin - output_geo.loc).to_physical_precise_round(scale);
                    elements.extend(
                        window
                            .elements(renderer, location, scale, 1.0, true)
                            .into_iter()
                            .map(|e| OutputRenderElement::Window(Wrap::from(e))),
                    );
                }
            }
        }
    }

    fn cursor_elements<R>(
        &mut self,
        renderer: &mut R,
        output: &Output,
        scale: Scale<f64>,
        elements: &mut Vec<OutputRenderElement<R>>,
    ) where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        if self.capture_without_cursor {
            return;
        }
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

    /// The render elements of a window's thumbnail: its frame with the
    /// top-left corner at the origin, scaled down to `width` pixels (never
    /// up), and the thumbnail's size.
    #[allow(clippy::type_complexity)]
    pub fn thumbnail_elements<R>(
        &mut self,
        renderer: &mut R,
        id: u64,
        width: u32,
    ) -> Result<(Vec<MovedElement<WindowRenderElement<R>>>, Size<i32, Physical>), String>
    where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        let window = self.window_by_id(id).ok_or("no such window")?;
        let frame = window.frame();
        if frame.size.w <= 0 || frame.size.h <= 0 {
            return Err("the window has no content yet".into());
        }
        let factor = (f64::from(width.clamp(16, 1024)) / f64::from(frame.size.w)).min(1.0);
        let size = Size::from((
            ((f64::from(frame.size.w) * factor).round() as i32).max(1),
            ((f64::from(frame.size.h) * factor).round() as i32).max(1),
        ));
        let target = Rectangle::new(Point::from((0.0, 0.0)), Size::from((f64::from(size.w), f64::from(size.h))));
        Ok((placed(renderer, &window, target, Scale::from(1.0), 1.0, false), size))
    }

    /// What a picture of a window for its exit animation is made of: the
    /// elements, the picture's size in pixels, the part of the desktop it
    /// covers and the scale it is drawn at. `None` when the window has
    /// nothing to show any more.
    #[allow(clippy::type_complexity)]
    pub fn snapshot_elements<R>(
        &self,
        renderer: &mut R,
        window: &WindowElement,
    ) -> Option<(Vec<WindowRenderElement<R>>, Size<i32, Physical>, Rectangle<f64, Logical>, f64)>
    where
        R: BoswasRenderer,
        R::TextureId: Clone + Send + Texture + 'static,
    {
        if !window.alive() {
            return None;
        }
        let data = window.data();
        let frame = window.frame();
        if frame.size.w <= 0 || frame.size.h <= 0 {
            return None;
        }
        let rect = self
            .window_rect(window)
            .unwrap_or_else(|| Rectangle::new(data.location.get(), frame.size));
        let scale = self
            .output_for_rect(rect)
            .map_or(1.0, |output| output.current_scale().fractional_scale());
        let margin = data.decoration.borrow().shadow_margin(window);
        let covered = Rectangle::new(
            rect.loc - Point::from((margin, margin)),
            rect.size + Size::from((2 * margin, 2 * margin)),
        );
        let size: Size<i32, Physical> = covered.size.to_f64().to_physical(scale).to_i32_ceil();
        if size.w <= 0 || size.h <= 0 || size.w > 16384 || size.h > 16384 {
            return None;
        }
        let origin: Point<i32, Physical> =
            (Point::<i32, Logical>::from((margin, margin)) - frame.loc).to_physical_precise_round(scale);
        let elements = window.elements(renderer, origin, Scale::from(scale), 1.0, margin > 0);
        (!elements.is_empty()).then_some((elements, size, covered.to_f64(), scale))
    }

    /// Every surface tree that is shown on an output.
    fn for_each_surface_on(&self, output: &Output, mut f: impl FnMut(&WlSurface)) {
        if self.lock.locked {
            if let Some(surface) = self.lock.surfaces.get(output) {
                f(surface.wl_surface());
            }
        } else {
            for window in self.space.elements_for_output(output) {
                if let Some(surface) = window.wl_surface() {
                    f(&surface);
                }
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

    /// Whether something on screen changes without client commits (an
    /// animated cursor, a window animation, the switcher about to appear),
    /// so outputs must keep producing frames.
    pub fn needs_continuous_frames(&self) -> bool {
        if matches!(&self.cursor_status, CursorImageStatus::Named(icon) if self.cursor_theme.is_animated(*icon)) {
            return true;
        }
        if self.reduced_motion() {
            return false;
        }
        !self.ghosts.is_empty()
            || self.space.elements().any(|window| window.data().animation.get().is_some())
            || self.switcher_animating()
            || self.overview_animating()
            || self.snap_flyout_pending()
    }
}

/// The surfaces of one layer of an output, front to back.
pub(crate) fn layer_elements<R>(
    renderer: &mut R,
    output: &Output,
    layer: Layer,
    scale: Scale<f64>,
    elements: &mut Vec<OutputRenderElement<R>>,
) where
    R: BoswasRenderer,
    R::TextureId: Clone + Send + Texture + 'static,
{
    let layers = layer_map_for_output(output);
    for surface in layers.layers_on(layer).rev() {
        let Some(geo) = layers.layer_geometry(surface) else {
            continue;
        };
        elements.extend(
            AsRenderElements::<R>::render_elements::<WaylandSurfaceRenderElement<R>>(
                surface,
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

/// A window drawn so that its frame fills `target` (output-local logical
/// coordinates) as far as its proportions allow, centred in it.
pub fn placed<R>(
    renderer: &mut R,
    window: &WindowElement,
    target: Rectangle<f64, Logical>,
    scale: Scale<f64>,
    alpha: f32,
    shadow: bool,
) -> Vec<MovedElement<WindowRenderElement<R>>>
where
    R: BoswasRenderer,
    R::TextureId: Clone + Send + Texture + 'static,
{
    let frame = window.frame();
    if frame.size.w <= 0 || frame.size.h <= 0 || target.size.w <= 0.0 || target.size.h <= 0.0 {
        return Vec::new();
    }
    let factor = (target.size.w / f64::from(frame.size.w)).min(target.size.h / f64::from(frame.size.h));
    let drawn = (f64::from(frame.size.w) * factor, f64::from(frame.size.h) * factor);
    let corner: Point<f64, Logical> = Point::from((
        target.loc.x + (target.size.w - drawn.0) / 2.0,
        target.loc.y + (target.size.h - drawn.1) / 2.0,
    ));
    // Drawn with the frame's corner at the origin, scaled about the origin,
    // then moved into place.
    let origin: Point<i32, Physical> = (Point::<i32, Logical>::from((0, 0)) - frame.loc).to_physical_precise_round(scale);
    let offset: Point<i32, Physical> = corner.to_physical(scale).to_i32_round();
    window
        .elements(renderer, origin, scale, alpha, shadow)
        .into_iter()
        .map(|element| {
            RelocateRenderElement::from_element(
                RescaleRenderElement::from_element(element, Point::from((0, 0)), factor),
                offset,
                Relocate::Relative,
            )
        })
        .collect()
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_magnifier_keeps_the_pointer_where_it_is() {
        let size = Size::from((1920, 1080));
        for (x, y) in [(0.0, 0.0), (960.0, 540.0), (1919.0, 1079.0), (300.0, 900.0)] {
            for zoom in [1.5, 2.0, 16.0] {
                let offset = magnifier_offset(Point::from((x, y)), size, zoom);
                // The point under the pointer is drawn under the pointer.
                assert!((x * zoom + f64::from(offset.x) - x).abs() <= 0.5, "{x} {zoom} {offset:?}");
                assert!((y * zoom + f64::from(offset.y) - y).abs() <= 0.5, "{y} {zoom} {offset:?}");
                // The enlarged picture still covers the whole screen.
                assert!(offset.x <= 0 && offset.y <= 0);
                assert!(f64::from(size.w) * zoom + f64::from(offset.x) >= f64::from(size.w) - 0.5);
                assert!(f64::from(size.h) * zoom + f64::from(offset.y) >= f64::from(size.h) - 0.5);
            }
        }
        assert_eq!(magnifier_offset(Point::from((5000.0, -40.0)), size, 2.0), Point::from((-1920, 0)), "kept on the screen");
        assert_eq!(magnifier_offset(Point::from((500.0, 500.0)), size, 1.0), Point::from((0, 0)));
    }
}
