//! The headless backend: one virtual output rendered in software into
//! memory. It needs no GPU, display or seat, so the compositor can run in
//! containers and CI; the control socket's `screenshot` command writes what
//! the output shows as a PNG. Its refresh rate is 60 Hz unless
//! `--headless-refresh` names another, so frame pacing can be measured at
//! the rates of fast monitors too.

use std::time::{Duration, Instant};

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

use super::{refresh_interval, Backend, FALLBACK_REFRESH};
use crate::state::Boswas;

pub struct Headless {
    renderer: PixmanRenderer,
    target: Image<'static, 'static>,
    output: Output,
    damage: OutputDamageTracker,
    clock: Clock<Monotonic>,
    sequence: u64,
    /// One refresh of the output.
    frame: Duration,
}

/// When the next refresh after `due` is, as seen at `now`: refreshes follow
/// each other at a fixed pace, so the time a frame takes to draw does not
/// slow the refresh rate down; refreshes missed while the compositor was
/// busy are skipped, not caught up with.
fn next_refresh(due: Instant, now: Instant, frame: Duration) -> Instant {
    let next = due + frame;
    if next > now {
        return next;
    }
    let missed = now.duration_since(next).as_nanos() / frame.as_nanos().max(1) + 1;
    next + frame * u32::try_from(missed).unwrap_or(u32::MAX)
}

/// Creates the virtual output and starts the frame timer. `millihertz`:
/// the output's refresh rate (`None`: 60 Hz); `physical_mm`: the size it
/// reports, as a monitor's EDID does ((0, 0): none).
pub fn init(state: &mut Boswas, width: i32, height: i32, millihertz: Option<i32>, physical_mm: (i32, i32)) -> Result<(), Box<dyn std::error::Error>> {
    if !(64..=8192).contains(&width) || !(64..=8192).contains(&height) {
        return Err("the headless output must be between 64 and 8192 pixels on each side".into());
    }
    let refresh = millihertz.unwrap_or(FALLBACK_REFRESH);
    if !(1_000..=1_000_000).contains(&refresh) {
        return Err("the headless refresh rate must be between 1 and 1000 Hz".into());
    }
    let frame = refresh_interval(refresh);
    let mut renderer = PixmanRenderer::new()?;
    let size = Size::from((width, height));
    let target: Image<'static, 'static> = renderer.create_buffer(Fourcc::Argb8888, Size::from((width, height)))?;
    state.shm_state.update_formats(renderer.shm_formats());

    let output = Output::new(
        "HEADLESS-1".into(),
        PhysicalProperties {
            size: physical_mm.into(),
            subpixel: Subpixel::Unknown,
            make: "Boswas".into(),
            model: "Headless".into(),
        },
    );
    let mode = Mode { size, refresh };
    let recommended = crate::scale::recommended("HEADLESS-1", physical_mm, (width, height), "");
    super::RecommendedScale::record(&output, recommended);
    let scale = state.config.output_scale("HEADLESS-1", recommended);
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
        frame,
    }));

    let mut due = Instant::now() + frame;
    state.loop_handle.insert_source(Timer::from_deadline(due), move |_, _, state| {
        if state.redraw || state.needs_continuous_frames() {
            state.redraw = false;
            if let Backend::Headless(mut headless) = std::mem::take(&mut state.backend) {
                headless.render(state);
                state.backend = Backend::Headless(headless);
            }
        }
        due = next_refresh(due, Instant::now(), frame);
        TimeoutAction::ToInstant(due)
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
        let started = std::time::Instant::now();
        state.prepare_frame();
        let (elements, clear) = state.display_elements(&mut self.renderer, &self.output);
        let states = {
            let mut framebuffer = match self.renderer.bind(&mut self.target) {
                Ok(framebuffer) => framebuffer,
                Err(err) => return warn!("headless: could not bind the output buffer: {err}"),
            };
            // The same buffer is drawn into every frame, so its age is 1 and
            // only damaged regions are repainted.
            match self.damage.render_output(&mut self.renderer, &mut framebuffer, 1, &elements, clear) {
                Ok(result) => {
                    // No damage: nothing was drawn, and nothing is counted.
                    if let Some(damage) = result.damage {
                        let size = self.output.current_mode().map_or_else(Size::default, |mode| mode.size);
                        state.frame_stats.frame(&self.output.name(), size, started.elapsed(), Some(damage));
                    }
                    result.states
                }
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
            smithay::wayland::presentation::Refresh::fixed(self.frame),
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refreshes_keep_their_pace_whatever_a_frame_costs() {
        let frame = refresh_interval(144_000);
        let start = Instant::now();
        // On time, or woken a little late: the next refresh is one interval
        // after the last one was due, not after the wake-up.
        assert_eq!(next_refresh(start, start, frame), start + frame);
        assert_eq!(next_refresh(start, start + Duration::from_millis(3), frame), start + frame);
        // A frame that took longer than a refresh: the refreshes it missed
        // are skipped, and the pace stays on the same grid.
        assert_eq!(next_refresh(start, start + frame + Duration::from_millis(1), frame), start + frame * 2);
        assert_eq!(next_refresh(start, start + frame * 5, frame), start + frame * 6);
        // A hundred and forty-four refreshes take one second.
        let mut due = start;
        for _ in 0..144 {
            due = next_refresh(due, due, frame);
        }
        let second = due.duration_since(start);
        assert!(second <= Duration::from_secs(1) && second > Duration::from_millis(999), "{second:?}");
    }
}
