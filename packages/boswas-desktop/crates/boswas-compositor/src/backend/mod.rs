//! Output and input backends.
//!
//! * `udev`: real hardware (DRM/KMS, libinput, libseat)
//! * `winit`: a window on a developer's desktop
//! * `headless`: an in-memory output for tests and CI

pub mod headless;
#[cfg(feature = "udev")]
pub mod udev;
#[cfg(feature = "winit")]
pub mod winit;

use std::{io::BufWriter, path::Path};

use smithay::backend::allocator::dmabuf::Dmabuf;

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

/// Writes opaque RGBA pixels as a PNG.
pub fn write_png(path: &Path, width: u32, height: u32, mut rgba: Vec<u8>) -> Result<(), String> {
    if rgba.len() != width as usize * height as usize * 4 {
        return Err("the frame has an unexpected size".into());
    }
    // An output is opaque; drop whatever alpha the renderer left behind.
    for pixel in rgba.chunks_exact_mut(4) {
        pixel[3] = 255;
    }
    let file = std::fs::File::create(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut encoder = png::Encoder::new(BufWriter::new(file), width, height);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder.write_header().map_err(|e| e.to_string())?;
    writer.write_image_data(&rgba).map_err(|e| e.to_string())?;
    writer.finish().map_err(|e| e.to_string())
}

/// Renders an output once more into an off-screen texture and returns its
/// RGBA pixels, for screenshots on the GL backends.
#[cfg(any(feature = "udev", feature = "winit"))]
pub fn capture_gles(
    renderer: &mut smithay::backend::renderer::gles::GlesRenderer,
    state: &mut crate::state::Boswas,
    output: &smithay::output::Output,
) -> Result<(u32, u32, Vec<u8>), String> {
    use smithay::{
        backend::{
            allocator::Fourcc,
            renderer::{damage::OutputDamageTracker, gles::GlesTexture, Bind, ExportMem, Offscreen},
        },
        utils::{Rectangle, Size, Transform},
    };
    let mode = output.current_mode().ok_or("the output has no mode")?;
    let size = mode.size;
    let (elements, clear) = state.output_elements(renderer, output);
    let mut texture: GlesTexture = renderer
        .create_buffer(Fourcc::Abgr8888, Size::from((size.w, size.h)))
        .map_err(|e| e.to_string())?;
    let mut framebuffer = renderer.bind(&mut texture).map_err(|e| e.to_string())?;
    let mut tracker = OutputDamageTracker::new(size, output.current_scale().fractional_scale(), Transform::Normal);
    tracker
        .render_output(renderer, &mut framebuffer, 0, &elements, clear)
        .map_err(|e| e.to_string())?;
    let mapping = renderer
        .copy_framebuffer(&framebuffer, Rectangle::from_size(Size::from((size.w, size.h))), Fourcc::Abgr8888)
        .map_err(|e| e.to_string())?;
    let bytes = renderer.map_texture(&mapping).map_err(|e| e.to_string())?.to_vec();
    Ok((size.w as u32, size.h as u32, bytes))
}
