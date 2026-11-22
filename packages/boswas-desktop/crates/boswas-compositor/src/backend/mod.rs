//! Output and input backends.
//!
//! * `udev`: real hardware (DRM/KMS, libinput, libseat)
//! * `winit`: a window on a developer's desktop
//! * `headless`: an in-memory output for tests and CI

#[cfg(any(feature = "udev", test))]
pub mod edid;
pub mod headless;
#[cfg(feature = "udev")]
mod seatlog;
#[cfg(feature = "udev")]
pub mod udev;
#[cfg(feature = "winit")]
pub mod winit;

use std::{cell::Cell, io::BufWriter, path::Path, time::Duration};

use smithay::{
    backend::{
        allocator::{dmabuf::Dmabuf, Fourcc},
        renderer::{
            damage::OutputDamageTracker,
            element::{memory::MemoryRenderBuffer, RenderElement},
            Bind, Color32F, ExportMem, Offscreen, Renderer, Texture,
        },
    },
    output::Output,
    utils::{Logical, Physical, Rectangle, Size, Transform},
};

use crate::{
    effects::BoswasRenderer,
    ipc::{CaptureTarget, ScreenshotRequest},
    shell::element::WindowElement,
    state::Boswas,
};

/// The refresh rate an output is taken to have when it reports none.
pub const FALLBACK_REFRESH: i32 = 60_000;

/// The time one refresh of a mode takes (`millihertz`; nonsense rates count
/// as [`FALLBACK_REFRESH`]). Frame pacing, idle frame callbacks and the
/// presentation feedback all use this, so nothing assumes 60 Hz.
pub fn refresh_interval(millihertz: i32) -> Duration {
    let millihertz = if (1_000..=1_000_000).contains(&millihertz) { millihertz } else { FALLBACK_REFRESH };
    Duration::from_nanos(1_000_000_000_000 / millihertz as u64)
}

/// The refresh interval of an output's current mode.
pub fn output_refresh_interval(output: &Output) -> Duration {
    refresh_interval(output.current_mode().map_or(FALLBACK_REFRESH, |mode| mode.refresh))
}

/// Variable refresh rate (adaptive sync) of an output, as the `outputs`
/// command reports it: whether the monitor and its driver offer it, and
/// whether it is on (`[outputs.NAME] adaptive_sync`). Kept with the output,
/// so the report does not need the backend.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct AdaptiveSync {
    pub supported: bool,
    pub enabled: bool,
}

impl AdaptiveSync {
    /// What was recorded for `output` (nothing: not supported).
    pub fn of(output: &Output) -> Self {
        output.user_data().get::<Cell<AdaptiveSync>>().map(Cell::get).unwrap_or_default()
    }

    /// Records what `output` offers and does now.
    pub fn record(self, output: &Output) {
        output.user_data().insert_if_missing(|| Cell::new(AdaptiveSync::default()));
        if let Some(cell) = output.user_data().get::<Cell<AdaptiveSync>>() {
            cell.set(self);
        }
    }
}

/// The running backend. While a backend renders it is taken out of the
/// state (leaving `None`), so rendering can borrow the rest of the state.
#[derive(Default)]
pub enum Backend {
    #[default]
    None,
    Headless(Box<headless::Headless>),
    #[cfg(feature = "winit")]
    Winit(Box<winit::Winit>),
    #[cfg(feature = "udev")]
    Udev(Box<udev::Udev>),
}

impl Backend {
    /// Switches to another virtual terminal (real hardware only).
    pub fn switch_vt(&mut self, vt: i32) {
        match self {
            #[cfg(feature = "udev")]
            Backend::Udev(udev) => udev.switch_vt(vt),
            _ => {
                let _ = vt;
            }
        }
    }

    /// Renders what needs rendering. The headless and nested backends pace
    /// themselves; real hardware renders as soon as something changed and
    /// no frame is in flight.
    pub fn tick(state: &mut crate::state::Boswas) {
        #[cfg(feature = "udev")]
        if matches!(state.backend, Backend::Udev(_)) {
            udev::tick(state);
        }
        #[cfg(feature = "winit")]
        if state.redraw {
            if let Backend::Winit(winit) = &state.backend {
                winit.request_redraw();
            }
        }
        let _ = state;
    }

    /// Mirrors the keyboard LEDs (Caps Lock, Num Lock) on real keyboards.
    pub fn update_leds(&mut self, leds: smithay::input::keyboard::LedState) {
        match self {
            #[cfg(feature = "udev")]
            Backend::Udev(udev) => udev.update_leds(leds),
            _ => {
                let _ = leds;
            }
        }
    }

    /// Tints the outputs to a colour temperature in kelvin (night light), or
    /// back to neutral. Real hardware loads a gamma ramp; the test backends
    /// have no colour pipeline to change.
    pub fn set_color_temperature(&mut self, temperature: Option<u32>) {
        match self {
            #[cfg(feature = "udev")]
            Backend::Udev(udev) => udev.set_color_temperature(temperature),
            _ => {
                let _ = temperature;
            }
        }
    }

    /// Takes a picture of a window as it is drawn now, for its exit
    /// animation: the picture and the part of the desktop it covers.
    pub fn snapshot(
        &mut self,
        state: &mut Boswas,
        window: &WindowElement,
    ) -> Option<(MemoryRenderBuffer, Rectangle<f64, Logical>)> {
        match self {
            Backend::Headless(headless) => headless.snapshot(state, window),
            #[cfg(feature = "winit")]
            Backend::Winit(winit) => winit.snapshot(state, window),
            #[cfg(feature = "udev")]
            Backend::Udev(udev) => udev.snapshot(state, window),
            Backend::None => None,
        }
    }

    /// Turns every screen off (and stops rendering to them) or back on.
    pub fn set_outputs_powered(&mut self, on: bool) {
        match self {
            #[cfg(feature = "udev")]
            Backend::Udev(udev) => udev.set_outputs_powered(on),
            _ => {
                let _ = on;
            }
        }
    }

    /// Applies the device settings of a changed configuration.
    pub fn config_changed(&mut self, config: &crate::config::Config) {
        match self {
            #[cfg(feature = "udev")]
            Backend::Udev(udev) => udev.config_changed(config),
            _ => {
                let _ = config;
            }
        }
    }

    /// Whether the renderer can import a client's dmabuf.
    pub fn import_dmabuf(&mut self, dmabuf: &Dmabuf) -> bool {
        match self {
            #[cfg(feature = "winit")]
            Backend::Winit(winit) => winit.import_dmabuf(dmabuf),
            #[cfg(feature = "udev")]
            Backend::Udev(udev) => udev.import_dmabuf(dmabuf),
            _ => {
                let _ = dmabuf;
                false
            }
        }
    }
}

/// Writes RGBA pixels as a PNG. An output is opaque, so `opaque` drops
/// whatever alpha the renderer left behind; otherwise the pixels are taken
/// as premultiplied (as renderers produce them) and stored straight.
pub fn write_png(path: &Path, width: u32, height: u32, mut rgba: Vec<u8>, opaque: bool) -> Result<(), String> {
    if rgba.len() != width as usize * height as usize * 4 {
        return Err("the frame has an unexpected size".into());
    }
    for pixel in rgba.chunks_exact_mut(4) {
        if opaque {
            pixel[3] = 255;
        } else if pixel[3] != 255 && pixel[3] != 0 {
            let a = u32::from(pixel[3]);
            for channel in &mut pixel[..3] {
                *channel = ((u32::from(*channel) * 255 + a / 2) / a).min(255) as u8;
            }
        }
    }
    let file = std::fs::File::create(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut encoder = png::Encoder::new(BufWriter::new(file), width, height);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder.write_header().map_err(|e| e.to_string())?;
    writer.write_image_data(&rgba).map_err(|e| e.to_string())?;
    writer.finish().map_err(|e| e.to_string())
}

/// Renders elements into a fresh off-screen buffer of `format` and returns
/// its pixels as RGBA bytes (premultiplied).
pub fn render_offscreen<R, T, E>(
    renderer: &mut R,
    format: Fourcc,
    size: Size<i32, Physical>,
    scale: f64,
    elements: &[E],
    clear: Color32F,
) -> Result<Vec<u8>, String>
where
    R: Renderer + Offscreen<T> + Bind<T> + ExportMem,
    R::TextureId: Texture + 'static,
    R::Error: std::fmt::Display,
    E: RenderElement<R>,
{
    render_offscreen_as(renderer, format, size, scale, Transform::Normal, elements, clear)
}

/// [`render_offscreen`] for a buffer in an output's own orientation: `size`
/// is the output's mode and `transform` its rotation, as on the real
/// screen.
pub fn render_offscreen_as<R, T, E>(
    renderer: &mut R,
    format: Fourcc,
    size: Size<i32, Physical>,
    scale: f64,
    transform: Transform,
    elements: &[E],
    clear: Color32F,
) -> Result<Vec<u8>, String>
where
    R: Renderer + Offscreen<T> + Bind<T> + ExportMem,
    R::TextureId: Texture + 'static,
    R::Error: std::fmt::Display,
    E: RenderElement<R>,
{
    let mut target: T = renderer
        .create_buffer(format, Size::from((size.w, size.h)))
        .map_err(|e| e.to_string())?;
    let mut framebuffer = renderer.bind(&mut target).map_err(|e| e.to_string())?;
    let mut tracker = OutputDamageTracker::new(size, scale, transform);
    tracker
        .render_output(renderer, &mut framebuffer, 0, elements, clear)
        .map_err(|e| e.to_string())?;
    let mapping = renderer
        .copy_framebuffer(&framebuffer, Rectangle::from_size(Size::from((size.w, size.h))), Fourcc::Abgr8888)
        .map_err(|e| e.to_string())?;
    Ok(renderer.map_texture(&mapping).map_err(|e| e.to_string())?.to_vec())
}

/// Renders a window into a memory buffer: the picture its exit animation
/// shows after the window itself is gone.
pub fn snapshot<R, T>(
    renderer: &mut R,
    format: Fourcc,
    state: &Boswas,
    window: &WindowElement,
) -> Option<(MemoryRenderBuffer, Rectangle<f64, Logical>)>
where
    R: BoswasRenderer + Offscreen<T> + Bind<T> + ExportMem,
    R::TextureId: Clone + Send + Texture + 'static,
    R::Error: std::fmt::Display,
{
    let (elements, size, rect, scale) = state.snapshot_elements(renderer, window)?;
    let rgba = render_offscreen(renderer, format, size, scale, &elements, Color32F::TRANSPARENT)
        .map_err(|err| tracing::debug!("no picture for an exit animation: {err}"))
        .ok()?;
    let buffer = MemoryRenderBuffer::from_slice(&rgba, Fourcc::Abgr8888, (size.w, size.h), 1, Transform::Normal, None);
    Some((buffer, rect))
}

/// Fills the buffers of the programs that are allowed to capture the screen
/// (see `handlers::screencopy`) with what `outputs` show now. Called by a
/// backend after it has rendered a frame.
pub fn screencopy<R, T>(renderer: &mut R, format: Fourcc, state: &mut Boswas, outputs: &[Output])
where
    R: BoswasRenderer + Offscreen<T> + Bind<T> + ExportMem,
    R::TextureId: Clone + Send + Texture + 'static,
    R::Error: std::fmt::Display,
{
    if !state.screencopy.has_ready() {
        return;
    }
    let time = state.start_time.elapsed();
    for frame in state.screencopy.take_ready() {
        let Some(output) = outputs.iter().find(|output| **output == frame.output) else {
            frame.fail();
            continue;
        };
        let Some(mode) = output.current_mode().filter(|_| !state.lock.locked) else {
            frame.fail();
            continue;
        };
        let scale = output.current_scale().fractional_scale();
        state.capture_without_cursor = !frame.overlay_cursor;
        let (elements, clear) = state.output_elements(renderer, output);
        state.capture_without_cursor = false;
        match render_offscreen_as(renderer, format, mode.size, scale, output.current_transform(), &elements, clear) {
            Ok(rgba) => frame.finish(&rgba, mode.size, time),
            Err(error) => {
                tracing::debug!("screen capture: {error}");
                frame.fail();
            }
        }
    }
}

/// Answers one capture request of the control socket: a picture of an
/// output, or the thumbnail of a window. `outputs` are the backend's
/// outputs; `format` is a buffer format its renderer can render into.
pub fn capture<R, T>(
    renderer: &mut R,
    format: Fourcc,
    state: &mut Boswas,
    outputs: &[Output],
    request: &ScreenshotRequest,
) -> Result<serde_json::Value, String>
where
    R: BoswasRenderer + Offscreen<T> + Bind<T> + ExportMem,
    R::TextureId: Clone + Send + Texture + 'static,
    R::Error: std::fmt::Display,
{
    match &request.target {
        CaptureTarget::Output(name) => {
            let output = match name {
                Some(name) => outputs.iter().find(|o| &o.name() == name).ok_or("no such output")?,
                None => outputs.first().ok_or("no output is connected")?,
            };
            let size = output.current_mode().ok_or("the output has no mode")?.size;
            let scale = output.current_scale().fractional_scale();
            let (elements, clear) = state.output_elements(renderer, output);
            let rgba = render_offscreen(renderer, format, size, scale, &elements, clear)?;
            write_png(&request.path, size.w as u32, size.h as u32, rgba, true)?;
            Ok(serde_json::json!({ "path": request.path, "output": output.name(), "width": size.w, "height": size.h }))
        }
        CaptureTarget::Window { id, width } => {
            let (elements, size) = state.thumbnail_elements(renderer, *id, *width)?;
            let rgba = render_offscreen(renderer, format, size, 1.0, &elements, Color32F::TRANSPARENT)?;
            write_png(&request.path, size.w as u32, size.h as u32, rgba, false)?;
            Ok(serde_json::json!({ "path": request.path, "width": size.w, "height": size.h }))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_refresh_lasts_as_long_as_the_mode_says() {
        assert_eq!(refresh_interval(60_000), Duration::from_nanos(16_666_666));
        assert_eq!(refresh_interval(144_000), Duration::from_nanos(6_944_444));
        assert_eq!(refresh_interval(239_757), Duration::from_nanos(4_170_889));
        assert_eq!(refresh_interval(1_000_000), Duration::from_millis(1));
        // A rate no display has is taken as 60 Hz, never as an endless or
        // zero-length refresh.
        for nonsense in [0, -60_000, 999, 1_000_001, i32::MAX] {
            assert_eq!(refresh_interval(nonsense), refresh_interval(FALLBACK_REFRESH), "{nonsense}");
        }
    }

    #[test]
    fn adaptive_sync_is_kept_with_its_output() {
        let output = Output::new(
            "DP-1".into(),
            smithay::output::PhysicalProperties {
                size: (0, 0).into(),
                subpixel: smithay::output::Subpixel::Unknown,
                make: "Test".into(),
                model: "Test".into(),
            },
        );
        assert_eq!(AdaptiveSync::of(&output), AdaptiveSync::default(), "nothing recorded: not supported");
        AdaptiveSync { supported: true, enabled: true }.record(&output);
        assert_eq!(AdaptiveSync::of(&output), AdaptiveSync { supported: true, enabled: true });
        AdaptiveSync { supported: true, enabled: false }.record(&output);
        assert_eq!(AdaptiveSync::of(&output), AdaptiveSync { supported: true, enabled: false });
        assert_eq!(output_refresh_interval(&output), refresh_interval(FALLBACK_REFRESH), "no mode yet");
        output.change_current_state(Some(smithay::output::Mode { size: (2560, 1440).into(), refresh: 165_000 }), None, None, None);
        assert_eq!(output_refresh_interval(&output), Duration::from_nanos(6_060_606));
    }
}
