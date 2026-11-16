//! The headless backend: one virtual output rendered in software into
//! memory. It needs no GPU, display or seat, so the compositor can run in
//! containers and CI; the control socket's `screenshot` command writes what
//! the output shows as a PNG.

use std::time::Duration;

use smithay::{
    backend::{
        allocator::Fourcc,
        renderer::{
            damage::OutputDamageTracker,
            element::memory::MemoryRenderBuffer,
            pixman::PixmanRenderer,
            Bind, ImportMemWl, Offscreen,
        },
    },
    output::{Mode, Output, PhysicalProperties, Scale, Subpixel},
    reexports::{
        calloop::timer::{TimeoutAction, Timer},
        pixman::Image,
        wayland_protocols::wp::presentation_time::server::wp_presentation_feedback,
    },
    utils::{Clock, Logical, Monotonic, Rectangle, Size, Transform},
};
use tracing::warn;

use super::Backend;
use crate::state::Boswas;

/// Interval between frames (60 Hz).
const FRAME: Duration = Duration::from_micros(16_667);

pub struct Headless {
    renderer: PixmanRenderer,
    target: Image<'static, 'static>,
    output: Output,
    damage: OutputDamageTracker,
    clock: Clock<Monotonic>,
    sequence: u64,
}

/// Creates the virtual output and starts the frame timer.
pub fn init(state: &mut Boswas, width: i32, height: i32) -> Result<(), Box<dyn std::error::Error>> {
    if !(64..=8192).contains(&width) || !(64..=8192).contains(&height) {
        return Err("the headless output must be between 64 and 8192 pixels on each side".into());
    }
    let mut renderer = PixmanRenderer::new()?;
    let size = Size::from((width, height));
    let target: Image<'static, 'static> = renderer.create_buffer(Fourcc::Argb8888, Size::from((width, height)))?;
    state.shm_state.update_formats(renderer.shm_formats());

    let output = Output::new(
        "HEADLESS-1".into(),
        PhysicalProperties {
            size: (0, 0).into(),
            subpixel: Subpixel::Unknown,
            make: "Boswas".into(),
            model: "Headless".into(),
        },
    );
    let mode = Mode {
        size,
        refresh: 60_000,
    };
    let scale = state
        .config
        .outputs
        .get("HEADLESS-1")
        .and_then(|settings| settings.scale)
        .unwrap_or(state.config.output.scale);
    output.create_global::<Boswas>(&state.display_handle);
    output.change_current_state(
        Some(mode),
        Some(Transform::Normal),
        Some(Scale::Fractional(scale)),
        Some((0, 0).into()),
    );
    output.set_preferred(mode);
    state.space.map_output(&output, (0, 0));

    let damage = OutputDamageTracker::from_output(&output);
    state.backend = Backend::Headless(Box::new(Headless {
        renderer,
        target,
        output,
        damage,
        clock: Clock::new(),
        sequence: 0,
    }));

    state.loop_handle.insert_source(Timer::from_duration(FRAME), |_, _, state| {
        if state.redraw || state.needs_continuous_frames() {
            state.redraw = false;
            if let Backend::Headless(mut headless) = std::mem::take(&mut state.backend) {
                headless.render(state);
                state.backend = Backend::Headless(headless);
            }
        }
        TimeoutAction::ToDuration(FRAME)
    })?;
    Ok(())
}

impl Headless {
    pub fn snapshot(
        &mut self,
        state: &Boswas,
        window: &crate::shell::element::WindowElement,
    ) -> Option<(MemoryRenderBuffer, Rectangle<f64, Logical>)> {
        super::snapshot::<_, Image<'static, 'static>>(&mut self.renderer, Fourcc::Argb8888, state, window)
    }

    fn render(&mut self, state: &mut Boswas) {
        state.prepare_frame();
        let (elements, clear) = state.output_elements(&mut self.renderer, &self.output);
        let states = {
            let mut framebuffer = match self.renderer.bind(&mut self.target) {
                Ok(framebuffer) => framebuffer,
                Err(err) => return warn!("headless: could not bind the output buffer: {err}"),
            };
            // The same buffer is drawn into every frame, so its age is 1 and
            // only damaged regions are repainted.
            match self.damage.render_output(&mut self.renderer, &mut framebuffer, 1, &elements, clear) {
                Ok(result) => result.states,
                Err(err) => return warn!("headless: rendering failed: {err}"),
            }
        };
        drop(elements);

        let time = state.start_time.elapsed();
        state.post_repaint(&self.output, &states, time);
        let mut feedback = state.take_presentation_feedback(&self.output, &states);
        self.sequence += 1;
        feedback.presented::<_, Monotonic>(
            self.clock.now(),
            smithay::wayland::presentation::Refresh::fixed(FRAME),
            self.sequence,
            wp_presentation_feedback::Kind::Vsync,
        );

        for request in std::mem::take(&mut state.ipc.screenshots) {
            let outputs = [self.output.clone()];
            let result = super::capture::<_, Image<'static, 'static>>(
                &mut self.renderer,
                Fourcc::Argb8888,
                state,
                &outputs,
                &request,
            );
            state.ipc_screenshot_done(request, result);
        }
        let outputs = [self.output.clone()];
        super::screencopy::<_, Image<'static, 'static>>(&mut self.renderer, Fourcc::Argb8888, state, &outputs);
    }
}
