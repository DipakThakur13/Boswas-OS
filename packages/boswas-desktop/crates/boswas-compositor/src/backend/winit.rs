//! The nested backend: the compositor in a window of another Wayland or X11
//! session, for development. Its output has the refresh rate of the monitor
//! the window is on (as far as the host tells), and frames follow the host's
//! redraw requests.

use smithay::{
    backend::{
        allocator::dmabuf::Dmabuf,
        allocator::Fourcc,
        renderer::{
            damage::OutputDamageTracker,
            element::memory::MemoryRenderBuffer,
            gles::{GlesRenderer, GlesTexture},
            ImportDma, ImportMemWl,
        },
        winit::{self, WinitEvent, WinitGraphicsBackend},
    },
    output::{Mode, Output, PhysicalProperties, Scale, Subpixel},
    reexports::wayland_protocols::wp::presentation_time::server::wp_presentation_feedback,
    utils::{Clock, Logical, Monotonic, Rectangle, Transform},
    wayland::{dmabuf::DmabufFeedbackBuilder, presentation::Refresh},
};
use tracing::warn;

use super::{output_refresh_interval, Backend, FALLBACK_REFRESH};
use crate::state::Boswas;

/// The refresh rate of the monitor the window is on, in millihertz.
fn host_refresh(backend: &WinitGraphicsBackend<GlesRenderer>) -> i32 {
    backend
        .window()
        .current_monitor()
        .and_then(|monitor| monitor.refresh_rate_millihertz())
        .and_then(|millihertz| i32::try_from(millihertz).ok())
        .filter(|millihertz| (1_000..=1_000_000).contains(millihertz))
        .unwrap_or(FALLBACK_REFRESH)
}

pub struct Winit {
    backend: WinitGraphicsBackend<GlesRenderer>,
    output: Output,
    damage: OutputDamageTracker,
    clock: Clock<Monotonic>,
    sequence: u64,
}

pub fn init(state: &mut Boswas) -> Result<(), Box<dyn std::error::Error>> {
    let (mut backend, winit) = winit::init::<GlesRenderer>().map_err(|e| format!("could not open a window: {e}"))?;
    crate::effects::init_gles(backend.renderer());
    state.rounds_corners = true;
    let mode = Mode {
        size: backend.window_size(),
        refresh: host_refresh(&backend),
    };
    let output = Output::new(
        "WINIT-1".into(),
        PhysicalProperties {
            size: (0, 0).into(),
            subpixel: Subpixel::Unknown,
            make: "Boswas".into(),
            model: "Nested".into(),
        },
    );
    output.create_global::<Boswas>(&state.display_handle);
    // The window's framebuffer is upside down relative to the output.
    output.change_current_state(
        Some(mode),
        Some(Transform::Flipped180),
        Some(Scale::Fractional(state.config.output.scale)),
        Some((0, 0).into()),
    );
    output.set_preferred(mode);
    state.space.map_output(&output, (0, 0));

    state.shm_state.update_formats(backend.renderer().shm_formats());
    let formats = backend.renderer().dmabuf_formats();
    if let Some(node) = smithay::backend::egl::EGLDevice::device_for_display(backend.renderer().egl_context().display())
        .ok()
        .and_then(|device| device.try_get_render_node().ok().flatten())
    {
        if let Ok(feedback) = DmabufFeedbackBuilder::new(node.dev_id(), formats).build() {
            let global = state
                .dmabuf_state
                .create_global_with_default_feedback::<Boswas>(&state.display_handle, &feedback);
            state.dmabuf_global = Some(global);
        }
    }

    let damage = OutputDamageTracker::from_output(&output);
    state.backend = Backend::Winit(Box::new(Winit {
        backend,
        output,
        damage,
        clock: Clock::new(),
        sequence: 0,
    }));

    state.loop_handle.insert_source(winit, |event, _, state| match event {
        WinitEvent::Resized { size, .. } => {
            if let Backend::Winit(winit) = &mut state.backend {
                // A new size; the window may be on another monitor now.
                let mode = Mode {
                    size,
                    refresh: host_refresh(&winit.backend),
                };
                winit.output.change_current_state(Some(mode), None, None, None);
                winit.output.set_preferred(mode);
                let output = winit.output.clone();
                smithay::desktop::layer_map_for_output(&output).arrange();
                state.usable_area_changed(&output);
            }
            state.schedule_redraw();
        }
        WinitEvent::Input(event) => state.process_input_event(event),
        WinitEvent::Redraw => {
            if let Backend::Winit(mut winit) = std::mem::take(&mut state.backend) {
                winit.render(state);
                state.backend = Backend::Winit(winit);
            }
        }
        WinitEvent::CloseRequested => state.loop_signal.stop(),
        WinitEvent::Focus(_) => {}
    })?;
    Ok(())
}

impl Winit {
    pub fn import_dmabuf(&mut self, dmabuf: &Dmabuf) -> bool {
        self.backend.renderer().import_dmabuf(dmabuf, None).is_ok()
    }

    /// Asks the host for a frame.
    pub fn request_redraw(&self) {
        self.backend.window().request_redraw();
    }

    pub fn snapshot(
        &mut self,
        state: &Boswas,
        window: &crate::shell::element::WindowElement,
    ) -> Option<(MemoryRenderBuffer, Rectangle<f64, Logical>)> {
        super::snapshot::<_, GlesTexture>(self.backend.renderer(), Fourcc::Abgr8888, state, window)
    }

    fn render(&mut self, state: &mut Boswas) {
        let started = std::time::Instant::now();
        state.redraw = false;
        state.prepare_frame();
        let size = self.backend.window_size();
        let age = self.backend.buffer_age().unwrap_or(0);
        let states = {
            let (renderer, mut framebuffer) = match self.backend.bind() {
                Ok(bound) => bound,
                Err(err) => return warn!("winit: could not bind the window: {err}"),
            };
            let (elements, clear) = state.output_elements(renderer, &self.output);
            match self.damage.render_output(renderer, &mut framebuffer, age, &elements, clear) {
                Ok(result) => {
                    if let Some(damage) = result.damage {
                        state.frame_stats.frame(&self.output.name(), size, started.elapsed(), Some(damage));
                    }
                    result.states
                }
                Err(err) => return warn!("winit: rendering failed: {err}"),
            }
        };
        if let Err(err) = self.backend.submit(Some(&[Rectangle::from_size(size)])) {
            warn!("winit: could not present: {err}");
        }

        for request in std::mem::take(&mut state.ipc.screenshots) {
            let outputs = [self.output.clone()];
            let result = super::capture::<_, GlesTexture>(
                self.backend.renderer(),
                Fourcc::Abgr8888,
                state,
                &outputs,
                &request,
            );
            state.ipc_screenshot_done(request, result);
        }
        let outputs = [self.output.clone()];
        super::screencopy::<_, GlesTexture>(self.backend.renderer(), Fourcc::Abgr8888, state, &outputs);

        let time = state.start_time.elapsed();
        state.post_repaint(&self.output, &states, time);
        let mut feedback = state.take_presentation_feedback(&self.output, &states);
        self.sequence += 1;
        feedback.presented::<_, Monotonic>(
            self.clock.now(),
            Refresh::fixed(output_refresh_interval(&self.output)),
            self.sequence,
            wp_presentation_feedback::Kind::Vsync,
        );
        if state.needs_continuous_frames() {
            self.backend.window().request_redraw();
        }
    }
}
