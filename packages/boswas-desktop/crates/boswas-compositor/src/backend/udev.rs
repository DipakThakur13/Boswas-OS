//! The real-hardware backend.
//!
//! * session and device access through libseat (logind): the compositor
//!   runs as the logged-in user and never needs root
//! * displays through DRM/KMS with GBM buffers and an EGL/GLES renderer;
//!   planes, direct scan-out and damage are handled by Smithay's
//!   `DrmCompositor`
//! * where OpenGL ES would only run in software (a virtual machine without
//!   3D acceleration, the firmware framebuffer, `nomodeset`-like set-ups) or
//!   does not start at all, frames are drawn with pixman into the device's
//!   own buffers instead: for a desktop's flat, damaged rectangles that is
//!   several times cheaper than Mesa's software OpenGL (`BOSWAS_RENDERER`
//!   chooses: `auto`, `gles` or `pixman`)
//! * input through libinput
//!
//! Outputs of one GPU are driven: the one the firmware booted with, else
//! the first that works; connectors on a second GPU are left alone
//! (multi-GPU output is a later milestone). Devices come and go while the
//! compositor runs: during boot the driver may bind a moment after the
//! compositor started, and a native driver replaces the firmware's
//! framebuffer device. When the device in use goes away, or none is in use,
//! the next one that appears is taken and the session's windows stay.
//!
//! Start-up fails with [`Failure::Startup`] and a message that names the
//! likely causes when the seat is not handed over, when no graphics device
//! appears, and (for the login screen) when no output can be set up.

use std::{
    collections::HashMap,
    os::fd::OwnedFd,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use smithay::{
    backend::{
        allocator::{
            dmabuf::{AsDmabuf, Dmabuf},
            dumb::{DumbAllocator, DumbBuffer},
            gbm::{GbmAllocator, GbmBufferFlags, GbmDevice},
            Allocator, Fourcc, Modifier,
        },
        drm::{
            compositor::{FrameFlags, PrimaryPlaneElement},
            exporter::gbm::GbmFramebufferExporter,
            output::{DrmOutput, DrmOutputManager, DrmOutputRenderElements},
            DrmDevice, DrmDeviceFd, DrmEvent, DrmEventMetadata, DrmEventTime, DrmNode, NodeType, VrrSupport,
        },
        egl::{context::ContextPriority, EGLContext, EGLDevice, EGLDisplay},
        input::InputEvent,
        libinput::{LibinputInputBackend, LibinputSessionInterface},
        renderer::{
            element::memory::MemoryRenderBuffer,
            gles::{GlesRenderer, GlesTexture},
            pixman::PixmanRenderer,
            Bind, ImportDma, ImportMemWl,
        },
        session::{libseat::LibSeatSession, Event as SessionEvent, Session},
        udev::{all_gpus, primary_gpu, UdevBackend, UdevEvent},
    },
    desktop::utils::OutputPresentationFeedback,
    input::keyboard::LedState,
    output::{Mode as OutputMode, Output, PhysicalProperties, Scale},
    reexports::{
        calloop::{
            timer::{TimeoutAction, Timer},
            EventLoop, RegistrationToken,
        },
        drm::{
            self,
            control::{connector, crtc, Device as ControlDevice, ModeFlags, ModeTypeFlags},
        },
        input::{DeviceCapability, Libinput},
        pixman::Image,
        rustix::fs::OFlags,
        wayland_protocols::wp::presentation_time::server::wp_presentation_feedback,
        wayland_server::backend::GlobalId,
    },
    utils::{Clock, DeviceFd, Monotonic, Physical, Point, Rectangle, Size},
    wayland::{dmabuf::DmabufFeedbackBuilder, presentation::Refresh},
};
use tracing::{debug, error, info, warn};

use super::{output_refresh_interval, seatlog, AdaptiveSync, Backend};
use crate::{exit::Failure, render::OutputRenderElement, state::Boswas};

/// Scan-out formats tried in order.
const COLOR_FORMATS: &[Fourcc] = &[Fourcc::Argb8888, Fourcc::Xrgb8888, Fourcc::Abgr8888];

/// How long the seat may take to hand the devices over after it was opened
/// (logind enables the session once its virtual terminal is in front).
const SEAT_WAIT: Duration = Duration::from_secs(4);
/// How long the first graphics device may take to appear: during boot the
/// compositor can be started before the driver has bound.
const DEVICE_WAIT: Duration = Duration::from_secs(10);
/// How long the login screen waits for a first output before it gives way
/// to the text login.
const OUTPUT_WAIT: Duration = Duration::from_secs(5);

/// A device number (`dev_t`), as udev reports it.
type DeviceId = u64;

type Feedback = Option<OutputPresentationFeedback>;
type GlesAllocator = GbmAllocator<DrmDeviceFd>;
type GlesExporter = GbmFramebufferExporter<DrmDeviceFd>;

/// The parts that draw a device's frames with one renderer: the output
/// manager, the renderer and the device's outputs (by CRTC).
struct Stack<A, F, R>
where
    A: Allocator,
    F: smithay::backend::drm::exporter::ExportFramebuffer<A::Buffer>,
    F::Framebuffer: std::fmt::Debug + 'static,
{
    manager: DrmOutputManager<A, F, Feedback, DrmDeviceFd>,
    renderer: R,
    outputs: HashMap<crtc::Handle, DrmOutput<A, F, Feedback, DrmDeviceFd>>,
}

/// Smithay's dumb-buffer allocator, as the output manager needs it (it
/// clones its allocator; the allocator is only the device descriptor).
#[derive(Debug, Clone)]
struct Dumb(DrmDeviceFd);

impl Allocator for Dumb {
    type Buffer = DumbBuffer;
    type Error = std::io::Error;

    fn create_buffer(&mut self, width: u32, height: u32, fourcc: Fourcc, modifiers: &[Modifier]) -> Result<DumbBuffer, Self::Error> {
        DumbAllocator::new(self.0.clone()).create_buffer(width, height, fourcc, modifiers)
    }
}

/// How a device's frames are drawn. (There is one device, so the size of
/// the variants does not matter.)
#[allow(clippy::large_enum_variant)]
enum Gpu {
    /// OpenGL ES on GBM buffers: a hardware renderer, rounded corners,
    /// direct scan-out and the cursor plane.
    Gles(Stack<GlesAllocator, GlesExporter, GlesRenderer>),
    /// pixman on dumb buffers: where OpenGL ES would run in software.
    Pixman(Stack<Dumb, DrmDeviceFd, PixmanRenderer>),
}

/// Runs `$body` with `$stack` bound to the device's stack, whichever
/// renderer it has. The body is compiled once for each.
macro_rules! with_stack {
    ($gpu:expr, $stack:ident => $body:expr) => {
        match $gpu {
            Gpu::Gles($stack) => $body,
            Gpu::Pixman($stack) => $body,
        }
    };
}

impl Gpu {
    fn drm(&self) -> &DrmDevice {
        with_stack!(self, stack => stack.manager.device())
    }

    fn name(&self) -> &'static str {
        match self {
            Gpu::Gles(_) => "OpenGL ES",
            Gpu::Pixman(_) => "pixman (software)",
        }
    }
}

/// One display.
struct Surface {
    output: Output,
    global: GlobalId,
    connector: connector::Handle,
    /// The modes the monitor offers.
    modes: Vec<drm::control::Mode>,
    /// A frame is queued and its vblank has not arrived yet.
    pending: bool,
    /// A timer will send frame callbacks for a frame without damage.
    idle_callback_armed: bool,
    /// When the last frame was taken as shown (see `frame_finished`).
    last_shown: Option<Instant>,
}

/// The GPU that drives the displays.
struct Device {
    node: DrmNode,
    path: PathBuf,
    token: RegistrationToken,
    gpu: Gpu,
    surfaces: HashMap<crtc::Handle, Surface>,
}

impl Device {
    /// Forgets an output's display side (the monitor went away or was
    /// turned off).
    fn remove_surface(&mut self, crtc: crtc::Handle) -> Option<Surface> {
        with_stack!(&mut self.gpu, stack => {
            stack.outputs.remove(&crtc);
        });
        self.surfaces.remove(&crtc)
    }
}

/// Which renderer draws the frames (`BOSWAS_RENDERER`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RendererChoice {
    /// OpenGL ES on a hardware renderer, pixman otherwise.
    Auto,
    Gles,
    Pixman,
}

impl RendererChoice {
    fn from_env(value: Option<&str>) -> Self {
        match value.map(str::trim).map(str::to_ascii_lowercase).as_deref() {
            Some("gles" | "gl" | "opengl") => RendererChoice::Gles,
            Some("pixman" | "software") => RendererChoice::Pixman,
            _ => RendererChoice::Auto,
        }
    }
}

pub struct Udev {
    session: LibSeatSession,
    /// The seat's name (`seat0`).
    seat: String,
    device: Option<Device>,
    /// Why the last graphics device that was tried could not be used.
    device_error: Option<String>,
    /// Why there is no output, when there is none.
    output_error: Option<String>,
    clock: Clock<Monotonic>,
    libinput: Option<Libinput>,
    /// Every input device, to mirror keyboard LEDs and to apply settings
    /// again when the configuration changes.
    inputs: Vec<smithay::reexports::input::Device>,
    /// The seat is enabled: this session owns the devices (it does not
    /// before logind activated it, and while another virtual terminal is in
    /// front).
    active: bool,
    /// The night light colour temperature the outputs should show.
    temperature: Option<u32>,
    /// The screens are on (they are turned off after a time without input).
    powered: bool,
    /// The output settings changed; they are applied before the next frame.
    outputs_changed: bool,
}

/// Opens the session, a graphics device and the input devices.
pub fn init(
    event_loop: &mut EventLoop<'static, Boswas>,
    state: &mut Boswas,
) -> Result<(), Box<dyn std::error::Error>> {
    seatlog::install();
    let (session, notifier) = LibSeatSession::new().map_err(|err| {
        Failure::Startup(no_seat_message(&format!("no seat could be opened ({err})"), &seatlog::errors()))
    })?;
    let seat = session.seat();

    state.backend = Backend::Udev(Box::new(Udev {
        session: session.clone(),
        seat: seat.clone(),
        device: None,
        device_error: None,
        output_error: None,
        clock: Clock::new(),
        libinput: None,
        inputs: Vec::new(),
        active: session.is_active(),
        temperature: None,
        powered: true,
        outputs_changed: false,
    }));

    // The seat being enabled and disabled: at start, on VT switches and
    // around suspend.
    state.loop_handle.insert_source(notifier, |event, &mut (), state| {
        let _ = with_udev(state, |udev, state| {
            match event {
                SessionEvent::PauseSession => udev.paused(),
                SessionEvent::ActivateSession => udev.activated(state),
            }
            Ok(())
        });
    })?;

    // No device can be opened before the seat is enabled. An opened seat
    // that never is, is the sign of a session that does not own it.
    if !wait_for(event_loop, state, SEAT_WAIT, |udev| udev.active)? {
        let problem = format!("the seat {seat} was opened but not enabled within {} s", SEAT_WAIT.as_secs());
        return Err(Failure::Startup(no_seat_message(&problem, &seatlog::errors())).into());
    }

    // Graphics devices that appear and go away, and monitor hotplug.
    let udev_backend = UdevBackend::new(&seat)?;
    state.loop_handle.insert_source(udev_backend, |event, _, state| {
        let _ = with_udev(state, |udev, state| {
            match event {
                UdevEvent::Added { path, .. } => udev.device_added(state, &path),
                UdevEvent::Changed { device_id } => udev.device_changed(state, device_id),
                UdevEvent::Removed { device_id } => udev.device_removed(state, device_id),
            }
            Ok(())
        });
    })?;
    with_udev(state, |udev, state| {
        udev.find_device(state);
        if udev.device.is_none() {
            info!("no usable graphics device yet; waiting up to {} s for one", DEVICE_WAIT.as_secs());
        }
        Ok(())
    })?;
    if !wait_for(event_loop, state, DEVICE_WAIT, |udev| udev.device.is_some())? {
        let tried = with_udev(state, |udev, _| Ok(udev.device_error.take()))?;
        return Err(Failure::Startup(no_device_message(DEVICE_WAIT, tried.as_deref())).into());
    }

    // Input.
    let mut libinput = Libinput::new_with_udev::<LibinputSessionInterface<LibSeatSession>>(session.clone().into());
    libinput
        .udev_assign_seat(&seat)
        .map_err(|()| "libinput could not open the seat's input devices")?;
    let input_backend = LibinputInputBackend::new(libinput.clone());
    if let Backend::Udev(udev) = &mut state.backend {
        udev.libinput = Some(libinput);
    }
    state.loop_handle.insert_source(input_backend, |mut event, _, state| {
        match &mut event {
            InputEvent::DeviceAdded { device } => {
                configure_input_device(device, &state.config);
                if device.has_capability(DeviceCapability::Keyboard) {
                    device.led_update(state.keyboard.led_state().into());
                }
                if let Backend::Udev(udev) = &mut state.backend {
                    udev.inputs.push(device.clone());
                }
            }
            InputEvent::DeviceRemoved { device } => {
                if let Backend::Udev(udev) = &mut state.backend {
                    udev.inputs.retain(|known| known != device);
                }
            }
            _ => {}
        }
        state.process_input_event(event);
    })?;

    // A desktop session without a monitor waits for one (a docked laptop
    // with its lid closed, a monitor that is switched on later). The login
    // screen has no reason to exist without one: the text login is shown
    // instead, on whatever the kernel's console reaches.
    let has_outputs = |udev: &Udev| udev.device.as_ref().is_some_and(|device| !device.surfaces.is_empty());
    if state.config.is_login_screen() {
        if !wait_for(event_loop, state, OUTPUT_WAIT, has_outputs)? {
            let problem = with_udev(state, |udev, _| Ok(udev.output_error.take()))?;
            return Err(Failure::Startup(no_output_message(OUTPUT_WAIT, problem.as_deref())).into());
        }
    } else if !matches!(&state.backend, Backend::Udev(udev) if has_outputs(udev)) {
        warn!("no monitor could be set up yet; the desktop appears when one is connected");
    }
    Ok(())
}

/// Runs the event loop until `done` holds for the backend, for `limit` at
/// most. Returns whether it holds.
fn wait_for(
    event_loop: &mut EventLoop<'static, Boswas>,
    state: &mut Boswas,
    limit: Duration,
    done: impl Fn(&Udev) -> bool,
) -> Result<bool, Box<dyn std::error::Error>> {
    let deadline = Instant::now() + limit;
    loop {
        if matches!(&state.backend, Backend::Udev(udev) if done(udev)) {
            return Ok(true);
        }
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Ok(false);
        }
        event_loop.dispatch(Some(left.min(Duration::from_millis(250))), state)?;
    }
}

/// The one line that explains a seat that cannot be used: what happened,
/// what libseat itself reported, and what usually causes it.
fn no_seat_message(problem: &str, libseat_errors: &[String]) -> String {
    let reported = if libseat_errors.is_empty() {
        String::new()
    } else {
        format!("; libseat reported: {}", libseat_errors.join("; "))
    };
    format!(
        "no usable seat: {problem}{reported}. Likely causes: boswas-compositor was not started from a login \
         session on a virtual terminal (by greetd, or from a login on a text console); logind refused to \
         activate the session because polkit is not running (package polkitd); another display server \
         holds the seat"
    )
}

/// The one line that explains a missing graphics device. `tried` is why the
/// last device that was there could not be used.
fn no_device_message(waited: Duration, tried: Option<&str>) -> String {
    match tried {
        Some(tried) => format!(
            "no usable graphics device within {} s (last tried: {tried}). The device needs a DRM/KMS driver \
             and an EGL driver (package libgl1-mesa-dri)",
            waited.as_secs()
        ),
        None => format!(
            "no graphics device appeared within {} s: no DRM/KMS driver is bound to a display adapter \
             (\"nomodeset\" on the kernel command line turns them all off)",
            waited.as_secs()
        ),
    }
}

/// The one line that explains why the login screen has no output.
fn no_output_message(waited: Duration, problem: Option<&str>) -> String {
    format!(
        "no output within {} s: {}. The login screen needs a connected monitor the graphics device can drive",
        waited.as_secs(),
        problem.unwrap_or("the graphics device reports no connected monitor")
    )
}

/// Runs `f` with the udev backend taken out of the state, so it can borrow
/// both.
fn with_udev<T>(
    state: &mut Boswas,
    f: impl FnOnce(&mut Udev, &mut Boswas) -> Result<T, Box<dyn std::error::Error>>,
) -> Result<T, Box<dyn std::error::Error>> {
    match std::mem::take(&mut state.backend) {
        Backend::Udev(mut udev) => {
            let result = f(&mut udev, state);
            state.backend = Backend::Udev(udev);
            result
        }
        other => {
            state.backend = other;
            Err("the udev backend is not running".into())
        }
    }
}

/// Renders the outputs that need it. Called once per event-loop turn.
pub fn tick(state: &mut Boswas) {
    if !(state.redraw || state.needs_continuous_frames()) {
        return;
    }
    let _ = with_udev(state, |udev, state| {
        if !udev.active || !udev.powered {
            return Ok(());
        }
        if std::mem::take(&mut udev.outputs_changed) {
            udev.apply_output_settings(state);
        }
        state.redraw = false;
        let crtcs: Vec<crtc::Handle> = udev.device.iter().flat_map(|d| d.surfaces.keys().copied()).collect();
        for crtc in crtcs {
            udev.render(state, crtc);
        }
        udev.take_screenshots(state);
        Ok(())
    });
}

/// Applies the `[pointer]` or `[touchpad]` settings to a device. A device
/// that can tap is a touchpad; settings a device does not have are skipped.
fn configure_input_device(device: &mut smithay::reexports::input::Device, config: &crate::config::Config) {
    use smithay::reexports::input::AccelProfile;
    if !device.has_capability(DeviceCapability::Pointer) {
        return;
    }
    let touchpad = device.config_tap_finger_count() > 0;
    let (natural, speed) = if touchpad {
        (config.touchpad.natural_scroll, config.touchpad.accel_speed)
    } else {
        (config.pointer.natural_scroll, config.pointer.accel_speed)
    };
    if device.config_scroll_has_natural_scroll() {
        let _ = device.config_scroll_set_natural_scroll_enabled(natural);
    }
    if device.config_accel_is_available() {
        let _ = device.config_accel_set_speed(speed);
    }
    if touchpad {
        let _ = device.config_tap_set_enabled(config.touchpad.tap_to_click);
        if device.config_dwt_is_available() {
            let _ = device.config_dwt_set_enabled(config.touchpad.disable_while_typing);
        }
    } else {
        let profile = if config.pointer.accel_profile == "flat" {
            AccelProfile::Flat
        } else {
            AccelProfile::Adaptive
        };
        if device.config_accel_profiles().contains(&profile) {
            let _ = device.config_accel_set_profile(profile);
        }
    }
    if device.config_left_handed_is_available() {
        let _ = device.config_left_handed_set(!touchpad && config.pointer.left_handed);
    }
}

/// The red, green and blue gains of a colour temperature in kelvin
/// (6500 K is neutral; lower is warmer), after Tanner Helland's fit of the
/// black-body curve.
pub fn temperature_gains(kelvin: u32) -> [f64; 3] {
    let t = f64::from(kelvin.clamp(1000, 6500)) / 100.0;
    let green = (99.470_802_586_1 * t.ln() - 161.119_568_166_1) / 255.0;
    let blue = if t <= 19.0 {
        0.0
    } else {
        (138.517_731_223_1 * (t - 10.0).ln() - 305.044_792_730_7) / 255.0
    };
    // Normalise so that 6500 K leaves the picture untouched.
    let neutral_green = (99.470_802_586_1 * 65f64.ln() - 161.119_568_166_1) / 255.0;
    let neutral_blue = (138.517_731_223_1 * 55f64.ln() - 305.044_792_730_7) / 255.0;
    [1.0, (green / neutral_green).clamp(0.0, 1.0), (blue / neutral_blue).clamp(0.0, 1.0)]
}

impl Udev {
    /// The seat was disabled: another virtual terminal is in front, or the
    /// system is about to sleep. The devices must not be used until it is
    /// enabled again.
    fn paused(&mut self) {
        info!("session paused");
        self.active = false;
        if let Some(libinput) = &self.libinput {
            libinput.suspend();
        }
        if let Some(device) = &mut self.device {
            with_stack!(&mut device.gpu, stack => stack.manager.pause());
            for surface in device.surfaces.values_mut() {
                surface.pending = false;
            }
        }
    }

    /// The seat was enabled: at start, or after [`Udev::paused`].
    fn activated(&mut self, state: &mut Boswas) {
        info!("session active");
        self.active = true;
        if let Some(libinput) = &mut self.libinput {
            if libinput.resume().is_err() {
                warn!("libinput did not resume");
            }
        }
        match &mut self.device {
            Some(device) => {
                with_stack!(&mut device.gpu, stack => {
                    if let Err(err) = stack.manager.activate(false) {
                        error!("could not reactivate the display device: {err}");
                    }
                    for output in stack.outputs.values_mut() {
                        output.reset_buffers();
                    }
                });
                for surface in device.surfaces.values_mut() {
                    surface.pending = false;
                }
                // The device's state was read back from the hardware: the
                // variable refresh rate a program on another virtual terminal
                // left there is set to the configuration again.
                self.outputs_changed = true;
            }
            // A device that appeared while the seat was away.
            None => self.find_device(state),
        }
        state.schedule_redraw();
    }

    /// Takes a graphics device into use when none is: the one the firmware
    /// booted with first, else any other of the seat that works.
    fn find_device(&mut self, state: &mut Boswas) {
        if self.device.is_some() || !self.active {
            return;
        }
        let mut candidates: Vec<PathBuf> = primary_gpu(&self.seat).ok().flatten().into_iter().collect();
        for gpu in all_gpus(&self.seat).unwrap_or_default() {
            if !candidates.contains(&gpu) {
                candidates.push(gpu);
            }
        }
        for path in candidates {
            match self.add_device(state, &path) {
                Ok(()) => {
                    info!("using graphics device {}", path.display());
                    self.device_error = None;
                    self.scan_connectors(state);
                    return;
                }
                Err(err) => {
                    warn!("graphics device {} cannot be used: {err}", path.display());
                    self.device_error = Some(format!("{}: {err}", path.display()));
                }
            }
        }
    }

    /// A graphics device appeared. It is taken when none is in use: the
    /// first device of a late driver, or the native driver's device that
    /// replaces the firmware framebuffer's.
    fn device_added(&mut self, state: &mut Boswas, path: &Path) {
        if self.device.is_some() {
            return debug!("graphics device {} appeared; outputs stay on the device in use", path.display());
        }
        info!("graphics device {} appeared", path.display());
        self.find_device(state);
    }

    /// A graphics device changed: a monitor was connected or disconnected.
    fn device_changed(&mut self, state: &mut Boswas, device_id: DeviceId) {
        match &self.device {
            Some(device) if device.node.dev_id() == device_id => self.scan_connectors(state),
            Some(_) => {}
            None => self.find_device(state),
        }
    }

    /// A graphics device went away. When it was the one in use, its outputs
    /// go with it; the windows stay and move to the outputs of the next
    /// device.
    fn device_removed(&mut self, state: &mut Boswas, device_id: DeviceId) {
        if !self.device.as_ref().is_some_and(|device| device.node.dev_id() == device_id) {
            return;
        }
        self.remove_device(state);
        self.find_device(state);
        if self.device.is_none() {
            warn!("no graphics device is left; the session goes on and is shown again when one appears");
        }
    }

    pub fn switch_vt(&mut self, vt: i32) {
        if let Err(err) = self.session.change_vt(vt) {
            warn!("could not switch to virtual terminal {vt}: {err}");
        }
    }

    pub fn import_dmabuf(&mut self, dmabuf: &Dmabuf) -> bool {
        self.device.as_mut().is_some_and(|device| {
            with_stack!(&mut device.gpu, stack => stack.renderer.import_dmabuf(dmabuf, None).is_ok())
        })
    }

    pub fn update_leds(&mut self, leds: LedState) {
        for device in &mut self.inputs {
            if device.has_capability(DeviceCapability::Keyboard) {
                device.led_update(leds.into());
            }
        }
    }

    pub fn snapshot(
        &mut self,
        state: &Boswas,
        window: &crate::shell::element::WindowElement,
    ) -> Option<(MemoryRenderBuffer, smithay::utils::Rectangle<f64, smithay::utils::Logical>)> {
        let device = self.device.as_mut()?;
        match &mut device.gpu {
            Gpu::Gles(stack) => super::snapshot::<_, GlesTexture>(&mut stack.renderer, Fourcc::Abgr8888, state, window),
            Gpu::Pixman(stack) => {
                super::snapshot::<_, Image<'static, 'static>>(&mut stack.renderer, Fourcc::Argb8888, state, window)
            }
        }
    }

    /// Applies the input settings of a changed configuration to the devices
    /// that are already connected.
    pub fn config_changed(&mut self, config: &crate::config::Config) {
        for device in &mut self.inputs {
            configure_input_device(device, config);
        }
        self.outputs_changed = true;
    }

    /// Turns every screen off, or lets the next frame turn them on again.
    pub fn set_outputs_powered(&mut self, on: bool) {
        self.powered = on;
        if on {
            return;
        }
        if let Some(device) = &mut self.device {
            for (crtc, surface) in device.surfaces.iter_mut() {
                surface.pending = false;
                let cleared = with_stack!(&mut device.gpu, stack => stack
                    .outputs
                    .get_mut(crtc)
                    .map(|output| output.with_compositor(|compositor| compositor.clear()).map_err(|err| err.to_string())));
                if let Some(Err(err)) = cleared {
                    warn!("could not turn {} off: {err}", surface.output.name());
                }
            }
        }
    }

    /// Night light: loads a gamma ramp for the colour temperature on every
    /// output (`None`: the neutral ramp).
    pub fn set_color_temperature(&mut self, temperature: Option<u32>) {
        self.temperature = temperature;
        let Some(device) = &self.device else {
            return;
        };
        for crtc in device.surfaces.keys() {
            load_gamma(device.gpu.drm(), *crtc, temperature);
        }
    }

    /// Answers pending screenshot requests with the first output.
    fn take_screenshots(&mut self, state: &mut Boswas) {
        if state.ipc.screenshots.is_empty() && !state.screencopy.has_ready() {
            return;
        }
        let requests = std::mem::take(&mut state.ipc.screenshots);
        let Some(device) = &mut self.device else {
            return;
        };
        let mut outputs: Vec<Output> = device.surfaces.values().map(|s| s.output.clone()).collect();
        outputs.sort_by_key(Output::name);
        match &mut device.gpu {
            Gpu::Gles(stack) => {
                super::screencopy::<_, GlesTexture>(&mut stack.renderer, Fourcc::Abgr8888, state, &outputs);
                for request in requests {
                    let result =
                        super::capture::<_, GlesTexture>(&mut stack.renderer, Fourcc::Abgr8888, state, &outputs, &request);
                    state.ipc_screenshot_done(request, result);
                }
            }
            Gpu::Pixman(stack) => {
                super::screencopy::<_, Image<'static, 'static>>(&mut stack.renderer, Fourcc::Argb8888, state, &outputs);
                for request in requests {
                    let result = super::capture::<_, Image<'static, 'static>>(
                        &mut stack.renderer,
                        Fourcc::Argb8888,
                        state,
                        &outputs,
                        &request,
                    );
                    state.ipc_screenshot_done(request, result);
                }
            }
        }
    }

    /// Opens a graphics device through the seat and prepares rendering on
    /// it. Its outputs are set up by [`Udev::scan_connectors`].
    fn add_device(&mut self, state: &mut Boswas, path: &Path) -> Result<(), Box<dyn std::error::Error>> {
        let node = DrmNode::from_path(path)?;
        let fd = self.session.open(
            path,
            OFlags::RDWR | OFlags::CLOEXEC | OFlags::NOCTTY | OFlags::NONBLOCK,
        )?;
        let fd = DeviceFd::from(fd);
        match Self::prepare_device(state, node, path, DrmDeviceFd::new(fd.clone())) {
            Ok(device) => {
                self.device = Some(device);
                Ok(())
            }
            Err(err) => {
                // The seat gets the device back, so it can be opened again.
                self.release(fd, path);
                Err(err)
            }
        }
    }

    /// Gives an opened device back to the seat, once nothing else holds it.
    fn release(&mut self, fd: DeviceFd, path: &Path) {
        match TryInto::<OwnedFd>::try_into(fd) {
            Ok(fd) => {
                if let Err(err) = self.session.close(fd) {
                    debug!("closing {}: {err}", path.display());
                }
            }
            Err(_) => debug!("{} is still in use somewhere; it closes with its last user", path.display()),
        }
    }

    fn prepare_device(
        state: &mut Boswas,
        node: DrmNode,
        path: &Path,
        fd: DrmDeviceFd,
    ) -> Result<Device, Box<dyn std::error::Error>> {
        let (drm, notifier) = DrmDevice::new(fd.clone(), true)?;
        let choice = RendererChoice::from_env(std::env::var("BOSWAS_RENDERER").ok().as_deref());
        let gpu = match choice {
            RendererChoice::Pixman => pixman_stack(state, drm, &fd)?,
            RendererChoice::Gles => gles_stack(state, node, drm, &fd)?.map_err(|(_, err)| err)?,
            RendererChoice::Auto => match gles_stack(state, node, drm, &fd)? {
                Ok(gpu) => gpu,
                Err((drm, reason)) => match pixman_stack(state, drm, &fd) {
                    Ok(gpu) => {
                        info!("{}: {reason}; frames are drawn with pixman", path.display());
                        gpu
                    }
                    Err(err) => {
                        // pixman cannot draw on this device either: OpenGL
                        // ES in software is still better than nothing.
                        warn!("{}: pixman cannot draw on it ({err}); using OpenGL ES after all", path.display());
                        let (drm, _) = DrmDevice::new(fd.clone(), true)?;
                        gles_stack(state, node, drm, &fd)?.map_err(|(_, err)| err)?
                    }
                },
            },
        };
        info!("{}: drawing with {}", path.display(), gpu.name());
        state.rounds_corners = matches!(gpu, Gpu::Gles(_));

        // The last step that can fail: from here on the device is in use.
        let token = state.loop_handle.insert_source(notifier, move |event, metadata, state| match event {
            DrmEvent::VBlank(crtc) => {
                let _ = with_udev(state, |udev, state| {
                    udev.frame_finished(state, crtc, metadata);
                    Ok(())
                });
            }
            DrmEvent::Error(err) => error!("display device error: {err}"),
        })?;
        Ok(Device {
            node,
            path: path.to_owned(),
            token,
            gpu,
            surfaces: HashMap::new(),
        })
    }

    /// Takes the device in use out of use: its outputs disappear (windows
    /// stay where they are) and the seat gets the device back.
    fn remove_device(&mut self, state: &mut Boswas) {
        let Some(device) = self.device.take() else {
            return;
        };
        info!("graphics device {} went away", device.path.display());
        let Device { path, token, gpu, surfaces, .. } = device;
        let fd = gpu.drm().device_fd().device_fd();
        for (_, surface) in surfaces {
            state.space.unmap_output(&surface.output);
            state.display_handle.remove_global::<Boswas>(surface.global);
        }
        state.loop_handle.remove(token);
        if let Some(global) = state.dmabuf_global.take() {
            state.dmabuf_state.destroy_global::<Boswas>(&state.display_handle, global);
        }
        // The outputs and the renderer go before the device they draw on.
        with_stack!(gpu, stack => {
            let Stack { manager, renderer, outputs } = stack;
            drop(outputs);
            drop(renderer);
            drop(manager);
        });
        self.release(fd, &path);
        self.output_error = None;
        let outputs = state.outputs_json();
        state.ipc_event("outputs", outputs);
        state.schedule_redraw();
    }

    /// Brings outputs in line with the connected monitors.
    fn scan_connectors(&mut self, state: &mut Boswas) {
        let Some(device) = &mut self.device else {
            return;
        };
        let drm = device.gpu.drm();
        let Ok(resources) = drm.resource_handles() else {
            return warn!("could not read the display resources");
        };
        let connected: Vec<connector::Info> = resources
            .connectors()
            .iter()
            .filter_map(|handle| drm.get_connector(*handle, true).ok())
            .filter(|info| info.state() == connector::State::Connected && !info.modes().is_empty())
            .collect();
        // Why a connected monitor could not be set up (the last reason).
        let mut problem = connected.is_empty().then(|| {
            format!("{} reports no connected monitor", device.path.display())
        });

        // Monitors that went away.
        let gone: Vec<crtc::Handle> = device
            .surfaces
            .iter()
            .filter(|(_, surface)| !connected.iter().any(|info| info.handle() == surface.connector))
            .map(|(crtc, _)| *crtc)
            .collect();
        // The display side of removed outputs goes once the device has been
        // read (below), before new outputs may take their CRTCs.
        let mut dropped: Vec<crtc::Handle> = Vec::new();
        for crtc in gone {
            if let Some(surface) = device.surfaces.remove(&crtc) {
                dropped.push(crtc);
                info!("output {} disconnected", surface.output.name());
                state.space.unmap_output(&surface.output);
                state.display_handle.remove_global::<Boswas>(surface.global);
            }
        }

        // Monitors the configuration turns off are known but not driven.
        state.disabled_outputs.clear();
        let mut off: Vec<connector::Handle> = Vec::new();
        for info in &connected {
            let name = connector_name(info);
            if state.config.outputs.get(&name).and_then(|settings| settings.enabled) == Some(false) {
                state.disabled_outputs.push(name);
                off.push(info.handle());
            }
        }
        // Never turn the last monitor off: there would be no way back.
        if off.len() == connected.len() {
            if let Some(first) = off.first().copied() {
                off.retain(|handle| *handle != first);
                state.disabled_outputs.remove(0);
                warn!("every monitor is turned off in the configuration; keeping one on");
            }
        }
        let turned_off: Vec<crtc::Handle> = device
            .surfaces
            .iter()
            .filter(|(_, surface)| off.contains(&surface.connector))
            .map(|(crtc, _)| *crtc)
            .collect();
        for crtc in turned_off {
            if let Some(surface) = device.surfaces.remove(&crtc) {
                dropped.push(crtc);
                info!("output {} turned off", surface.output.name());
                state.space.unmap_output(&surface.output);
                state.display_handle.remove_global::<Boswas>(surface.global);
            }
        }

        // New monitors: each takes the first free CRTC one of its encoders
        // can drive.
        let mut taken: Vec<crtc::Handle> = device.surfaces.keys().copied().collect();
        let mut assignments = Vec::new();
        for info in connected {
            if off.contains(&info.handle()) {
                continue;
            }
            if device.surfaces.values().any(|surface| surface.connector == info.handle()) {
                continue;
            }
            let crtc = info
                .encoders()
                .iter()
                .filter_map(|encoder| drm.get_encoder(*encoder).ok())
                .flat_map(|encoder| resources.filter_crtcs(encoder.possible_crtcs()))
                .find(|crtc| !taken.contains(crtc));
            match crtc {
                Some(crtc) => {
                    taken.push(crtc);
                    assignments.push((info, crtc));
                }
                None => {
                    warn!("no free CRTC for connector {}", connector_name(&info));
                    problem = Some(format!("{} has no free CRTC for {}", device.path.display(), connector_name(&info)));
                }
            }
        }
        for crtc in dropped {
            device.remove_surface(crtc);
        }
        for (info, crtc) in assignments {
            if let Err(err) = connect(device, state, &info, crtc) {
                warn!("could not set up {}: {err}", connector_name(&info));
                problem = Some(format!("{} could not be set up on {}: {err}", connector_name(&info), device.path.display()));
            }
        }
        arrange_outputs(device, state);
        self.output_error = if device.surfaces.is_empty() { problem } else { None };
        let outputs = state.outputs_json();
        state.ipc_event("outputs", outputs);
        state.schedule_redraw();
    }

    /// Brings the outputs in line with a changed configuration: turns
    /// monitors on and off, and sets mode, variable refresh rate, scale,
    /// rotation and position of the ones that stay.
    fn apply_output_settings(&mut self, state: &mut Boswas) {
        if let Some(device) = &mut self.device {
            let crtcs: Vec<crtc::Handle> = device.surfaces.keys().copied().collect();
            for crtc in crtcs {
                let Some(surface) = device.surfaces.get_mut(&crtc) else {
                    continue;
                };
                let name = surface.output.name();
                let settings = state.config.outputs.get(&name).cloned().unwrap_or_default();
                // Mode.
                if let Some(mode) = configured_mode(&surface.modes, &settings) {
                    if surface.output.current_mode() != Some(OutputMode::from(mode)) {
                        let result: Result<(), String> = match &mut device.gpu {
                            Gpu::Gles(stack) => match stack.outputs.get_mut(&crtc) {
                                Some(output) => output
                                    .use_mode::<_, OutputRenderElement<GlesRenderer>>(
                                        mode,
                                        &mut stack.renderer,
                                        &DrmOutputRenderElements::default(),
                                    )
                                    .map_err(|err| err.to_string()),
                                None => Err("the output is not set up".into()),
                            },
                            Gpu::Pixman(stack) => match stack.outputs.get_mut(&crtc) {
                                Some(output) => output
                                    .use_mode::<_, OutputRenderElement<PixmanRenderer>>(
                                        mode,
                                        &mut stack.renderer,
                                        &DrmOutputRenderElements::default(),
                                    )
                                    .map_err(|err| err.to_string()),
                                None => Err("the output is not set up".into()),
                            },
                        };
                        match result {
                            Ok(()) => {
                                surface.pending = false;
                                surface.output.change_current_state(Some(OutputMode::from(mode)), None, None, None);
                                info!("output {name}: mode {}", describe(&mode));
                            }
                            Err(err) => warn!("output {name}: could not set the mode {}: {err}", describe(&mode)),
                        }
                    }
                }
                // Variable refresh rate.
                let sync = apply_adaptive_sync(&device.gpu, crtc, surface.connector, &name, settings.adaptive_sync.unwrap_or(false));
                sync.record(&surface.output);
                // Scale and rotation.
                let scale = settings.scale.unwrap_or(state.config.output.scale);
                let transform = rotation(settings.rotation.as_deref());
                surface.output.change_current_state(None, Some(transform), Some(Scale::Fractional(scale)), None);
            }
        }
        // Monitors turned on or off, and everything placed again.
        self.scan_connectors(state);
    }

    fn render(&mut self, state: &mut Boswas, crtc: crtc::Handle) {
        let Some(device) = &mut self.device else {
            return;
        };
        let Some(surface) = device.surfaces.get_mut(&crtc) else {
            return;
        };
        if surface.pending {
            // The frame after the pending vblank picks the change up.
            state.redraw = true;
            return;
        }
        let output = surface.output.clone();
        let started = Instant::now();
        state.prepare_frame();
        let drawn: Drawn = with_stack!(&mut device.gpu, stack => {
            let (elements, clear) = state.output_elements(&mut stack.renderer, &output);
            match stack.outputs.get_mut(&crtc) {
                None => Drawn::Failed("the output is not set up".into()),
                Some(drm_output) => match drm_output.render_frame(&mut stack.renderer, &elements, clear, FrameFlags::DEFAULT) {
                    Err(err) => Drawn::Failed(err.to_string()),
                    Ok(frame) => {
                        // Where the kernel cannot wait for the renderer
                        // itself (no fence the driver takes: a software
                        // renderer, a firmware framebuffer), the frame must
                        // be complete before it is handed over. A device that
                        // copies the frame to the screen at that moment
                        // (simpledrm) would otherwise show whatever part was
                        // drawn by then, and never the rest of it.
                        let unfinished = match &frame.primary_element {
                            PrimaryPlaneElement::Swapchain(element) if frame.needs_sync() => Some(element.sync.clone()),
                            _ => None,
                        };
                        // What the frame drew again on the primary plane, for
                        // the statistics (`None`: the whole output, or a
                        // client buffer shown directly).
                        let damage: Option<Vec<Rectangle<i32, Physical>>> = match &frame.primary_element {
                            PrimaryPlaneElement::Swapchain(element) if state.frame_stats.enabled() => {
                                element.damage.raw().next().map(|rects| {
                                    rects
                                        .map(|r| Rectangle::new(Point::from((r.loc.x, r.loc.y)), Size::from((r.size.w, r.size.h))))
                                        .collect()
                                })
                            }
                            _ => None,
                        };
                        let rendered = !frame.is_empty;
                        let states = frame.states;
                        drop(elements);
                        if rendered {
                            if let Some(sync) = unfinished {
                                if sync.wait().is_err() {
                                    debug!("waiting for the frame of {} was interrupted", output.name());
                                }
                            }
                            if let Some(mode) = output.current_mode() {
                                state.frame_stats.frame(&output.name(), mode.size, started.elapsed(), damage.as_deref());
                            }
                            let feedback = state.take_presentation_feedback(&output, &states);
                            let queued = drm_output.queue_frame(Some(feedback)).map_err(|err| err.to_string());
                            Drawn::Queued(states, queued)
                        } else {
                            Drawn::Empty
                        }
                    }
                },
            }
        });

        let time = state.start_time.elapsed();
        match drawn {
            Drawn::Failed(err) => warn!("rendering {} failed: {err}", output.name()),
            Drawn::Queued(states, queued) => {
                match queued {
                    Ok(()) => surface.pending = true,
                    Err(err) => warn!("could not queue a frame for {}: {err}", output.name()),
                }
                state.post_repaint(&output, &states, time);
            }
            Drawn::Empty if !surface.idle_callback_armed => {
                // Nothing changed on screen. Clients waiting for a frame
                // callback still get one, at the display's pace, so a client
                // that commits without damage cannot spin the compositor.
                surface.idle_callback_armed = true;
                let timer = Timer::from_duration(output_refresh_interval(&output));
                let inserted = state.loop_handle.insert_source(timer, move |_, _, state| {
                    if let Backend::Udev(udev) = &mut state.backend {
                        if let Some(surface) = udev.device.as_mut().and_then(|d| d.surfaces.get_mut(&crtc)) {
                            surface.idle_callback_armed = false;
                        }
                    }
                    let empty = smithay::backend::renderer::element::RenderElementStates::default();
                    let time = state.start_time.elapsed();
                    state.post_repaint(&output, &empty, time);
                    TimeoutAction::Drop
                });
                if inserted.is_err() {
                    surface.idle_callback_armed = false;
                }
            }
            Drawn::Empty => {}
        }
    }

    fn frame_finished(&mut self, state: &mut Boswas, crtc: crtc::Handle, metadata: &mut Option<DrmEventMetadata>) {
        let Some(device) = self.device.as_mut() else {
            return;
        };
        let Some(surface) = device.surfaces.get_mut(&crtc) else {
            return;
        };
        // One refresh of the mode in use (with variable refresh: of its
        // highest rate, which is also the soonest the next frame can come).
        let frame = output_refresh_interval(&surface.output);
        // Devices without a vertical blank (the virtual graphics of most
        // virtual machines, the firmware framebuffer) report a frame as
        // shown the moment it is queued. Drawing the next one at once would
        // run the compositor and its clients as fast as the processor
        // allows, for frames nobody sees. A frame is therefore taken as
        // shown no sooner than one refresh after the last; on a display
        // with a real vertical blank they are that far apart anyway.
        let now = Instant::now();
        if let Some(due) = early_until(surface.last_shown, now, frame) {
            let timer = Timer::from_duration(due.saturating_duration_since(now));
            let inserted = state.loop_handle.insert_source(timer, move |_, _, state| {
                let _ = with_udev(state, |udev, state| {
                    udev.frame_finished(state, crtc, &mut None);
                    Ok(())
                });
                TimeoutAction::Drop
            });
            if inserted.is_ok() {
                // Still pending: nothing is drawn for this output until then.
                return;
            }
        }
        surface.last_shown = Some(now);
        surface.pending = false;
        let submitted = with_stack!(&mut device.gpu, stack => stack
            .outputs
            .get_mut(&crtc)
            .map(|output| output.frame_submitted().map_err(|err| err.to_string())));
        match submitted.unwrap_or(Ok(None)) {
            Ok(Some(Some(mut feedback))) => {
                let hardware_time = metadata.as_ref().and_then(|m| match m.time {
                    DrmEventTime::Monotonic(time) if !time.is_zero() => Some(time),
                    _ => None,
                });
                let sequence = metadata.as_ref().map_or(0, |m| u64::from(m.sequence));
                let (time, flags) = match hardware_time {
                    Some(time) => (
                        time.into(),
                        wp_presentation_feedback::Kind::Vsync
                            | wp_presentation_feedback::Kind::HwClock
                            | wp_presentation_feedback::Kind::HwCompletion,
                    ),
                    None => (self.clock.now(), wp_presentation_feedback::Kind::Vsync),
                };
                let refresh = if AdaptiveSync::of(&surface.output).enabled {
                    Refresh::variable(frame)
                } else {
                    Refresh::fixed(frame)
                };
                feedback.presented::<_, Monotonic>(time, refresh, sequence, flags);
            }
            Ok(_) => {}
            Err(err) => debug!("frame not submitted: {err}"),
        }
        // Whatever changed while the frame was on its way is drawn now.
        if state.redraw || state.needs_continuous_frames() {
            state.redraw = false;
            let crtcs: Vec<crtc::Handle> = self.device.iter().flat_map(|d| d.surfaces.keys().copied()).collect();
            for crtc in crtcs {
                self.render(state, crtc);
            }
        }
    }
}

/// Loads the gamma ramp of a colour temperature on one CRTC.
fn load_gamma(drm: &DrmDevice, crtc: crtc::Handle, temperature: Option<u32>) {
    let size = match drm.get_crtc(crtc) {
        Ok(info) => info.gamma_length() as usize,
        Err(err) => return debug!("no gamma information for {crtc:?}: {err}"),
    };
    if size < 2 {
        return debug!("{crtc:?} has no gamma ramp; night light cannot tint this output");
    }
    let gains = temperature.map_or([1.0; 3], temperature_gains);
    let ramp = |gain: f64| -> Vec<u16> {
        (0..size)
            .map(|i| (i as f64 / (size - 1) as f64 * gain * f64::from(u16::MAX)).round() as u16)
            .collect()
    };
    if let Err(err) = drm.set_gamma(crtc, &ramp(gains[0]), &ramp(gains[1]), &ramp(gains[2])) {
        warn!("could not load the night light gamma ramp: {err}");
    }
}

/// When a frame reported as shown at `now` is early, the moment it is to be
/// taken as shown: one refresh (`frame`) after the last one, `last`. A
/// tenth of a refresh is allowed for timing jitter.
fn early_until(last: Option<Instant>, now: Instant, frame: Duration) -> Option<Instant> {
    let due = last? + frame;
    (now + frame / 10 < due).then_some(due)
}

/// How drawing a frame of an output went.
enum Drawn {
    /// A frame was drawn and handed to the display (or not, with why).
    Queued(smithay::backend::renderer::element::RenderElementStates, Result<(), String>),
    /// Nothing on the output changed.
    Empty,
    Failed(String),
}

/// OpenGL ES on GBM buffers. `Ok(Err((drm, why)))` when OpenGL ES would only
/// run in software or cannot start: the device is handed back for pixman.
#[allow(clippy::type_complexity)]
fn gles_stack(
    state: &mut Boswas,
    node: DrmNode,
    drm: DrmDevice,
    fd: &DrmDeviceFd,
) -> Result<Result<Gpu, (DrmDevice, String)>, Box<dyn std::error::Error>> {
    let gbm = match GbmDevice::new(fd.clone()) {
        Ok(gbm) => gbm,
        Err(err) => return Ok(Err((drm, format!("no GBM ({err})")))),
    };
    // SAFETY: the GBM device outlives the EGL display (both are owned by
    // the stack, and the renderer holding the display is dropped first).
    let display = match unsafe { EGLDisplay::new(gbm.clone()) } {
        Ok(display) => display,
        Err(err) => return Ok(Err((drm, format!("OpenGL ES is not available ({err})")))),
    };
    let software = EGLDevice::device_for_display(&display).is_ok_and(|device| device.is_software());
    if software && RendererChoice::from_env(std::env::var("BOSWAS_RENDERER").ok().as_deref()) == RendererChoice::Auto {
        return Ok(Err((drm, "OpenGL ES would only run in software".into())));
    }
    let context = match EGLContext::new_with_priority(&display, ContextPriority::High) {
        Ok(context) => context,
        Err(err) => return Ok(Err((drm, format!("no OpenGL ES context ({err})")))),
    };
    // SAFETY: the context was just created for this thread and is used by
    // this renderer only.
    let mut renderer = match unsafe { GlesRenderer::new(context) } {
        Ok(renderer) => renderer,
        Err(err) => return Ok(Err((drm, format!("OpenGL ES could not be set up ({err})")))),
    };
    crate::effects::init_gles(&mut renderer);

    state.shm_state.update_formats(renderer.shm_formats());
    let render_node = node.node_with_type(NodeType::Render).and_then(Result::ok).unwrap_or(node);
    let dmabuf_formats = renderer.dmabuf_formats();
    match DmabufFeedbackBuilder::new(render_node.dev_id(), dmabuf_formats).build() {
        Ok(feedback) => {
            let global = state
                .dmabuf_state
                .create_global_with_default_feedback::<Boswas>(&state.display_handle, &feedback);
            state.dmabuf_global = Some(global);
        }
        Err(err) => warn!("no linux-dmabuf: {err}"),
    }

    let allocator = GbmAllocator::new(gbm.clone(), GbmBufferFlags::RENDERING | GbmBufferFlags::SCANOUT);
    let exporter = GbmFramebufferExporter::new(gbm.clone(), Some(render_node));
    let render_formats = renderer.egl_context().dmabuf_render_formats().clone();
    let manager = DrmOutputManager::new(
        drm,
        allocator,
        exporter,
        Some(gbm),
        COLOR_FORMATS.iter().copied(),
        render_formats,
    );
    Ok(Ok(Gpu::Gles(Stack {
        manager,
        renderer,
        outputs: HashMap::new(),
    })))
}

/// pixman on dumb buffers, mapped into the compositor. Fails when the
/// device's buffers cannot be drawn into that way.
fn pixman_stack(state: &mut Boswas, drm: DrmDevice, fd: &DrmDeviceFd) -> Result<Gpu, Box<dyn std::error::Error>> {
    let mut renderer = PixmanRenderer::new()?;
    // A trial buffer: the device must hand its buffers out for mapping.
    let mut allocator = Dumb(fd.clone());
    {
        let buffer = allocator
            .create_buffer(64, 64, Fourcc::Xrgb8888, &[Modifier::Linear])
            .map_err(|err| format!("no dumb buffer: {err}"))?;
        let mut dmabuf = buffer.export().map_err(|err| format!("dumb buffers cannot be exported: {err}"))?;
        renderer
            .bind(&mut dmabuf)
            .map_err(|err| format!("dumb buffers cannot be mapped: {err}"))?;
    }
    state.shm_state.update_formats(renderer.shm_formats());
    // No linux-dmabuf: clients of a software-only system draw into shared
    // memory, and a device offered for client rendering would be wrong.
    if let Some(global) = state.dmabuf_global.take() {
        state.dmabuf_state.destroy_global::<Boswas>(&state.display_handle, global);
    }
    let render_formats = Bind::<Dmabuf>::supported_formats(&renderer).unwrap_or_default();
    let manager = DrmOutputManager::new(
        drm,
        allocator,
        fd.clone(),
        None::<GbmDevice<DrmDeviceFd>>,
        COLOR_FORMATS.iter().copied(),
        render_formats,
    );
    Ok(Gpu::Pixman(Stack {
        manager,
        renderer,
        outputs: HashMap::new(),
    }))
}

/// Creates the output for a newly connected monitor.
fn connect(
    device: &mut Device,
    state: &mut Boswas,
    info: &connector::Info,
    crtc: crtc::Handle,
) -> Result<(), Box<dyn std::error::Error>> {
    let name = connector_name(info);
    let settings = state.config.outputs.get(&name).cloned().unwrap_or_default();
    let modes: Vec<drm::control::Mode> = info.modes().to_vec();
    let chosen = configured_mode(&modes, &settings).ok_or("the monitor reports no mode")?;
    let (width_mm, height_mm) = info.size().unwrap_or((0, 0));
    let (make, model) = read_edid(device.gpu.drm(), info)
        .map(|edid| super::edid::make_and_model(&edid))
        .unwrap_or((None, None));
    let output = Output::new(
        name.clone(),
        PhysicalProperties {
            size: (width_mm as i32, height_mm as i32).into(),
            subpixel: info.subpixel().into(),
            make: make.unwrap_or_else(|| "Unknown".into()),
            model: model.unwrap_or_else(|| "Unknown".into()),
        },
    );
    // The modes Settings may offer: every one that can be chosen.
    for offered in modes.iter().filter(|mode| usable(mode)) {
        output.add_mode(OutputMode::from(*offered));
    }
    // "Preferred" is what the compositor uses without a setting.
    if let Some(default) = default_mode(&modes) {
        output.set_preferred(OutputMode::from(default));
    }

    // The chosen mode first; should the driver refuse it (a cable or link
    // that cannot carry the highest rate the monitor lists), the mode the
    // monitor prefers.
    let mut candidates = vec![chosen];
    if let Some(preferred) = preferred_mode(&modes).filter(|preferred| *preferred != chosen) {
        candidates.push(preferred);
    }
    let planes = device.gpu.drm().planes(&crtc).ok();
    let mut refused: Option<(drm::control::Mode, String)> = None;
    let mut mode = None;
    for candidate in candidates {
        if let Some((previous, why)) = &refused {
            warn!("output {name}: {} could not be set ({why}); using the mode the monitor prefers", describe(previous));
        }
        output.change_current_state(
            Some(OutputMode::from(candidate)),
            Some(rotation(settings.rotation.as_deref())),
            Some(Scale::Fractional(settings.scale.unwrap_or(state.config.output.scale))),
            None,
        );
        let initialized: Result<(), String> = match &mut device.gpu {
            Gpu::Gles(stack) => stack
                .manager
                .initialize_output::<GlesRenderer, OutputRenderElement<GlesRenderer>>(
                    crtc,
                    candidate,
                    &[info.handle()],
                    &output,
                    planes.clone(),
                    &mut stack.renderer,
                    &DrmOutputRenderElements::default(),
                )
                .map(|drm_output| {
                    stack.outputs.insert(crtc, drm_output);
                })
                .map_err(|err| format!("{err}")),
            Gpu::Pixman(stack) => stack
                .manager
                .initialize_output::<PixmanRenderer, OutputRenderElement<PixmanRenderer>>(
                    crtc,
                    candidate,
                    &[info.handle()],
                    &output,
                    planes.clone(),
                    &mut stack.renderer,
                    &DrmOutputRenderElements::default(),
                )
                .map(|drm_output| {
                    stack.outputs.insert(crtc, drm_output);
                })
                .map_err(|err| format!("{err}")),
        };
        match initialized {
            Ok(()) => {
                mode = Some(candidate);
                break;
            }
            Err(err) => refused = Some((candidate, err)),
        }
    }
    let Some(mode) = mode else {
        return Err(refused.map_or_else(|| "no mode could be set".into(), |(_, why)| why).into());
    };
    let output_mode = OutputMode::from(mode);
    let global = output.create_global::<Boswas>(&state.display_handle);
    if state.night_light_applied.is_some() {
        load_gamma(device.gpu.drm(), crtc, state.night_light_applied);
    }
    let sync = apply_adaptive_sync(&device.gpu, crtc, info.handle(), &name, settings.adaptive_sync.unwrap_or(false));
    sync.record(&output);
    info!(
        "output {name}: {}x{} @ {} mHz{}",
        output_mode.size.w,
        output_mode.size.h,
        output_mode.refresh,
        match (sync.supported, sync.enabled) {
            (_, true) => ", variable refresh rate on",
            (true, false) => ", variable refresh rate offered (off)",
            (false, _) => "",
        }
    );
    device.surfaces.insert(
        crtc,
        Surface {
            output,
            global,
            connector: info.handle(),
            modes,
            pending: false,
            idle_callback_armed: false,
            last_shown: None,
        },
    );
    Ok(())
}

/// Turns variable refresh rate on or off (`wanted`, from `[outputs.NAME]
/// adaptive_sync`) where the monitor and its driver offer it, and returns
/// what the output then does. With pixman it is never offered: its dumb
/// buffers are written by the processor and the display gains nothing from
/// waiting for them. A monitor that needs a mode set to change it (HDMI)
/// gets one with its next frame.
fn apply_adaptive_sync(gpu: &Gpu, crtc: crtc::Handle, connector: connector::Handle, name: &str, wanted: bool) -> AdaptiveSync {
    let Gpu::Gles(stack) = gpu else {
        return AdaptiveSync::default();
    };
    let Some(drm_output) = stack.outputs.get(&crtc) else {
        return AdaptiveSync::default();
    };
    drm_output.with_compositor(|compositor| {
        let supported = matches!(
            compositor.vrr_supported(connector),
            Ok(VrrSupport::Supported | VrrSupport::RequiresModeset)
        );
        let wanted = wanted && supported;
        if compositor.vrr_enabled() != wanted {
            match compositor.use_vrr(wanted) {
                Ok(()) => info!("output {name}: variable refresh rate {}", if wanted { "on" } else { "off" }),
                Err(err) => warn!("output {name}: could not turn variable refresh rate {}: {err}", if wanted { "on" } else { "off" }),
            }
        }
        AdaptiveSync {
            supported,
            enabled: compositor.vrr_enabled(),
        }
    })
}

/// The connector's name as the desktop knows the output: `eDP-1`.
fn connector_name(info: &connector::Info) -> String {
    format!("{}-{}", info.interface().as_str(), info.interface_id())
}

/// The mode a monitor flags as preferred, else its first. On almost every
/// monitor with a high refresh rate this is its 60 Hz mode: it is the
/// monitor's native size, not the mode to use (see [`default_mode`]).
fn preferred_mode(modes: &[drm::control::Mode]) -> Option<drm::control::Mode> {
    modes
        .iter()
        .find(|mode| mode.mode_type().contains(ModeTypeFlags::PREFERRED))
        .or_else(|| modes.first())
        .copied()
}

/// The refresh rate of a mode in millihertz, from its own timings (pixel
/// clock and totals, as `OutputMode::from` computes it). `None` for timings
/// that make no sense and for rates outside 1 to 1000 Hz, the range the
/// configuration accepts: such a mode is never chosen, and nothing is ever
/// paced faster than the mode really refreshes.
fn refresh_millihertz(mode: &drm::control::Mode) -> Option<i32> {
    let (_, _, htotal) = mode.hsync();
    let (_, _, vtotal) = mode.vsync();
    if mode.clock() == 0 || htotal == 0 || vtotal == 0 {
        return None;
    }
    let millihertz = OutputMode::from(*mode).refresh;
    (1_000..=1_000_000).contains(&millihertz).then_some(millihertz)
}

/// Whether a mode may be chosen: progressive (interlaced modes are never
/// used) and with a sensible refresh rate.
fn usable(mode: &drm::control::Mode) -> bool {
    !mode.flags().contains(ModeFlags::INTERLACE) && refresh_millihertz(mode).is_some()
}

/// Of `modes`, the usable one with the highest refresh rate; between equal
/// rates the one the monitor prefers, then the first (the kernel's order).
fn fastest<'a>(modes: impl Iterator<Item = &'a drm::control::Mode>) -> Option<drm::control::Mode> {
    modes
        .filter(|mode| usable(mode))
        .min_by_key(|mode| {
            let preferred = mode.mode_type().contains(ModeTypeFlags::PREFERRED);
            (std::cmp::Reverse(refresh_millihertz(mode)), !preferred)
        })
        .copied()
}

/// The mode of a monitor without a `mode` setting: its native size (that of
/// the mode it prefers) at the highest refresh rate it offers there, so a
/// 144 Hz monitor runs at 144 Hz and not at the 60 Hz its preferred mode
/// usually has. Only modes the monitor reports are considered, never one
/// made up. Without a usable mode at the native size: the largest size
/// that has one; without any: the preferred mode, whatever it is.
fn default_mode(modes: &[drm::control::Mode]) -> Option<drm::control::Mode> {
    let native = preferred_mode(modes)?.size();
    fastest(modes.iter().filter(|mode| mode.size() == native))
        .or_else(|| {
            let area = |mode: &drm::control::Mode| u32::from(mode.size().0) * u32::from(mode.size().1);
            let largest = modes.iter().filter(|mode| usable(mode)).map(area).max()?;
            fastest(modes.iter().filter(|mode| area(mode) == largest))
        })
        .or_else(|| preferred_mode(modes))
}

/// The usable mode of the wanted size whose refresh rate is nearest the
/// wanted one (the highest, when none is asked for).
fn pick_mode(modes: &[drm::control::Mode], wanted: (i32, i32, Option<i32>)) -> Option<drm::control::Mode> {
    let (width, height, refresh) = wanted;
    let sized = modes
        .iter()
        .filter(|mode| (i32::from(mode.size().0), i32::from(mode.size().1)) == (width, height));
    match refresh {
        None => fastest(sized),
        Some(wanted) => sized
            .filter(|mode| usable(mode))
            .min_by_key(|mode| refresh_millihertz(mode).map_or(i32::MAX, |millihertz| (millihertz - wanted).abs()))
            .copied(),
    }
}

/// A mode for the log: `2560x1440 at 143.999 Hz`.
fn describe(mode: &drm::control::Mode) -> String {
    let (width, height) = mode.size();
    let rate = refresh_millihertz(mode).unwrap_or_default();
    format!("{width}x{height} at {}.{:03} Hz", rate / 1000, rate % 1000)
}

/// The mode an output's settings ask for, else its default.
fn configured_mode(modes: &[drm::control::Mode], settings: &crate::config::OutputSettings) -> Option<drm::control::Mode> {
    settings
        .mode
        .as_deref()
        .and_then(crate::config::parse_mode)
        .and_then(|wanted| pick_mode(modes, wanted))
        .or_else(|| default_mode(modes))
}

/// The output transform of a configured rotation.
fn rotation(setting: Option<&str>) -> smithay::utils::Transform {
    use smithay::utils::Transform;
    match setting {
        Some("90") => Transform::_90,
        Some("180") => Transform::_180,
        Some("270") => Transform::_270,
        _ => Transform::Normal,
    }
}

/// Reads a connector's EDID property.
fn read_edid(drm: &DrmDevice, info: &connector::Info) -> Option<Vec<u8>> {
    let properties = drm.get_properties(info.handle()).ok()?;
    let (handles, values) = properties.as_props_and_values();
    for (handle, value) in handles.iter().zip(values) {
        let property = drm.get_property(*handle).ok()?;
        if property.name().to_str() == Ok("EDID") && *value != 0 {
            return drm.get_property_blob(*value).ok();
        }
    }
    None
}

/// Places the outputs: at their configured position, and those without one
/// side by side, left to right, in connector order, after the others.
fn arrange_outputs(device: &Device, state: &mut Boswas) {
    let mut surfaces: Vec<&Surface> = device.surfaces.values().collect();
    surfaces.sort_by_key(|surface| surface.output.name());
    let configured = |surface: &Surface| {
        state
            .config
            .outputs
            .get(&surface.output.name())
            .and_then(|settings| settings.position)
            .map(|[x, y]| (x, y))
    };
    // First the outputs with a position, then the rest to the right of
    // everything placed so far.
    let mut placed: Vec<(Output, (i32, i32))> = Vec::new();
    let mut right_edge = 0;
    for surface in surfaces.iter().filter(|surface| configured(surface).is_some()) {
        let position = configured(surface).unwrap_or_default();
        placed.push((surface.output.clone(), position));
    }
    for (output, position) in &placed {
        output.change_current_state(None, None, None, Some((*position).into()));
        state.space.map_output(output, *position);
        let width = state.space.output_geometry(output).map_or(0, |geo| geo.size.w);
        right_edge = right_edge.max(position.0 + width);
    }
    let mut changed: Vec<Output> = placed.into_iter().map(|(output, _)| output).collect();
    for surface in surfaces.iter().filter(|surface| configured(surface).is_none()) {
        let position = (right_edge, 0);
        surface.output.change_current_state(None, None, None, Some(position.into()));
        state.space.map_output(&surface.output, position);
        right_edge += state.space.output_geometry(&surface.output).map_or(0, |geo| geo.size.w);
        changed.push(surface.output.clone());
    }
    for output in changed {
        smithay::desktop::layer_map_for_output(&output).arrange();
        state.usable_area_changed(&output);
    }
    // A window that was on a removed monitor comes back into view.
    let reachable = |state: &Boswas, point| state.space.outputs().any(|o| state.space.output_geometry(o).is_some_and(|g| g.contains(point)));
    for window in state.windows.clone() {
        let location = window.data().location.get();
        if window.data().mapped.get() && !reachable(state, location) {
            let first = state.space.outputs().next().cloned();
            if let Some(output) = first {
                let area = state.usable_area(&output);
                state.move_window(&window, area.loc + smithay::utils::Point::from((32, 32)));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frames_are_not_taken_as_shown_faster_than_the_display_refreshes() {
        let frame = Duration::from_micros(16_667);
        let start = Instant::now();
        // The first frame, and one that arrives a refresh after the last.
        assert_eq!(early_until(None, start, frame), None);
        assert_eq!(early_until(Some(start), start + frame, frame), None);
        // A little early (timing jitter) still counts.
        assert_eq!(early_until(Some(start), start + frame - Duration::from_millis(1), frame), None);
        // At once, as a device without a vertical blank reports it: held
        // back until a refresh has passed.
        assert_eq!(early_until(Some(start), start + Duration::from_millis(2), frame), Some(start + frame));
        // After a pause, nothing is held back.
        assert_eq!(early_until(Some(start), start + Duration::from_secs(5), frame), None);
        // At 144 Hz a frame a 144th of a second after the last is on time,
        // and one sooner is held back to that moment, not to a 60th.
        let fast = super::super::refresh_interval(144_000);
        assert_eq!(early_until(Some(start), start + fast, fast), None);
        assert_eq!(early_until(Some(start), start + Duration::from_millis(1), fast), Some(start + fast));
        assert!(fast < Duration::from_millis(7));
    }

    /// The kernel's `struct drm_mode_modeinfo` (include/uapi/drm/drm_mode.h),
    /// which `drm::control::Mode` wraps: a monitor's modes are built here as
    /// the kernel would report them.
    #[repr(C)]
    struct ModeInfo {
        clock: u32,
        hdisplay: u16,
        hsync_start: u16,
        hsync_end: u16,
        htotal: u16,
        hskew: u16,
        vdisplay: u16,
        vsync_start: u16,
        vsync_end: u16,
        vtotal: u16,
        vscan: u16,
        vrefresh: u32,
        flags: u32,
        type_: u32,
        name: [u8; 32],
    }

    /// The timings of a mode of `width` by `height` refreshing `hz` times a
    /// second: a pixel clock for blanking of 160 by 40 pixels.
    fn info(width: u16, height: u16, hz: f64, preferred: bool, interlaced: bool) -> ModeInfo {
        let (htotal, vtotal) = (width + 160, height + 40);
        let frames = if interlaced { hz / 2.0 } else { hz };
        ModeInfo {
            clock: (f64::from(htotal) * f64::from(vtotal) * frames / 1000.0).round() as u32,
            hdisplay: width,
            hsync_start: width + 48,
            hsync_end: width + 80,
            htotal,
            hskew: 0,
            vdisplay: height,
            vsync_start: height + 3,
            vsync_end: height + 8,
            vtotal,
            vscan: 0,
            vrefresh: hz.round() as u32,
            flags: if interlaced { ModeFlags::INTERLACE.bits() } else { 0 },
            type_: ModeTypeFlags::DRIVER.bits() | if preferred { ModeTypeFlags::PREFERRED.bits() } else { 0 },
            name: [0; 32],
        }
    }

    fn from_info(info: ModeInfo) -> drm::control::Mode {
        // SAFETY: `Mode` is `#[repr(transparent)]` over the kernel's
        // `drm_mode_modeinfo`, whose fields `ModeInfo` repeats in order;
        // `transmute` refuses to compile if the sizes differ.
        unsafe { std::mem::transmute::<ModeInfo, drm::control::Mode>(info) }
    }

    fn mode(width: u16, height: u16, hz: f64, preferred: bool, interlaced: bool) -> drm::control::Mode {
        from_info(info(width, height, hz, preferred, interlaced))
    }

    fn shown(mode: Option<drm::control::Mode>) -> Option<(u16, u16, i32)> {
        mode.map(|mode| (mode.size().0, mode.size().1, (f64::from(refresh_millihertz(&mode).unwrap()) / 1000.0).round() as i32))
    }

    #[test]
    fn a_monitor_runs_at_its_native_size_and_highest_refresh_rate() {
        // A 1440p 165 Hz monitor as it reports itself: the 60 Hz mode is the
        // preferred one, the faster ones are listed after it.
        let monitor = [
            mode(2560, 1440, 59.951, true, false),
            mode(2560, 1440, 165.0, false, false),
            mode(2560, 1440, 144.0, false, false),
            mode(2560, 1440, 120.0, false, false),
            mode(1920, 1080, 240.0, false, false),
            mode(1920, 1080, 60.0, false, false),
            mode(1280, 720, 60.0, false, false),
        ];
        assert_eq!(shown(preferred_mode(&monitor)), Some((2560, 1440, 60)), "what the monitor flags");
        assert_eq!(shown(default_mode(&monitor)), Some((2560, 1440, 165)), "the default: native size, highest rate");
        assert_eq!(shown(configured_mode(&monitor, &Default::default())), Some((2560, 1440, 165)));
        // A faster mode at a smaller size does not move the default off the native size.
        assert_ne!(shown(default_mode(&monitor)), Some((1920, 1080, 240)));

        // `mode` in the configuration still decides; without a rate it is the highest at that size.
        let set = |mode: &str| crate::config::OutputSettings { mode: Some(mode.into()), ..Default::default() };
        assert_eq!(shown(configured_mode(&monitor, &set("2560x1440@60"))), Some((2560, 1440, 60)));
        assert_eq!(shown(configured_mode(&monitor, &set("2560x1440@144"))), Some((2560, 1440, 144)));
        assert_eq!(shown(configured_mode(&monitor, &set("2560x1440@100"))), Some((2560, 1440, 120)), "the nearest");
        assert_eq!(shown(configured_mode(&monitor, &set("1920x1080"))), Some((1920, 1080, 240)));
        assert_eq!(shown(configured_mode(&monitor, &set("3840x2160"))), Some((2560, 1440, 165)), "not offered: the default");

        // An ordinary 60 Hz monitor keeps its 60 Hz mode.
        let office = [mode(1920, 1080, 60.0, true, false), mode(1920, 1080, 50.0, false, false), mode(1680, 1050, 60.0, false, false)];
        assert_eq!(shown(default_mode(&office)), Some((1920, 1080, 60)));
        // Between equal rates, the mode the monitor prefers.
        let twins = [mode(1920, 1080, 144.0, false, false), mode(1920, 1080, 144.0, true, false)];
        assert!(default_mode(&twins).unwrap().mode_type().contains(ModeTypeFlags::PREFERRED));
        // No preferred mode: the first one's size, as the kernel lists the largest first.
        let unflagged = [mode(3840, 2160, 60.0, false, false), mode(3840, 2160, 120.0, false, false), mode(1920, 1080, 240.0, false, false)];
        assert_eq!(shown(default_mode(&unflagged)), Some((3840, 2160, 120)));
        assert_eq!(default_mode(&[]), None);
    }

    #[test]
    fn interlaced_and_impossible_modes_are_never_chosen() {
        // A television: its preferred mode is interlaced, and so is the
        // "120 Hz" one (60 frames a second, two fields each).
        let television = [
            mode(1920, 1080, 60.0, true, true),
            mode(1920, 1080, 120.0, false, true),
            mode(1920, 1080, 50.0, false, false),
            mode(1280, 720, 60.0, false, false),
        ];
        assert_eq!(shown(default_mode(&television)), Some((1920, 1080, 50)), "the progressive mode at the native size");
        let set = |mode: &str| crate::config::OutputSettings { mode: Some(mode.into()), ..Default::default() };
        assert_eq!(shown(configured_mode(&television, &set("1920x1080@120"))), Some((1920, 1080, 50)), "not the interlaced one");
        // Only interlaced at the native size: the largest progressive size.
        let old = [mode(1920, 1080, 60.0, true, true), mode(1280, 720, 60.0, false, false), mode(720, 480, 60.0, false, false)];
        assert_eq!(shown(default_mode(&old)), Some((1280, 720, 60)));
        // Nothing but interlaced modes: the monitor's own choice, rather than no picture.
        let only = [mode(1920, 1080, 60.0, true, true)];
        assert!(default_mode(&only).unwrap().flags().contains(ModeFlags::INTERLACE));

        // Timings that give no rate, or one no display has, are never taken
        // as the fastest mode: frames would be paced to that rate. The rate
        // is the one the timings give, not the number the mode claims.
        let no_total = from_info(ModeInfo { htotal: 0, ..info(2560, 1440, 144.0, false, false) });
        let no_clock = from_info(ModeInfo { clock: 0, ..info(2560, 1440, 144.0, false, false) });
        let absurd = mode(2560, 1440, 1500.0, false, false);
        let boasting = from_info(ModeInfo { vrefresh: 240, ..info(2560, 1440, 75.0, false, false) });
        for impossible in [no_total, no_clock, absurd] {
            assert_eq!(refresh_millihertz(&impossible), None);
        }
        assert_eq!(shown(Some(boasting)), Some((2560, 1440, 75)));
        let monitor = [mode(2560, 1440, 60.0, true, false), no_total, no_clock, absurd, boasting];
        assert_eq!(shown(default_mode(&monitor)), Some((2560, 1440, 75)));
        let monitor = [mode(2560, 1440, 60.0, true, false), no_total, no_clock, absurd];
        assert_eq!(shown(default_mode(&monitor)), Some((2560, 1440, 60)));
    }

    #[test]
    fn the_renderer_is_chosen_by_name() {
        assert_eq!(RendererChoice::from_env(None), RendererChoice::Auto);
        assert_eq!(RendererChoice::from_env(Some("auto")), RendererChoice::Auto);
        assert_eq!(RendererChoice::from_env(Some("nonsense")), RendererChoice::Auto);
        assert_eq!(RendererChoice::from_env(Some(" GLES ")), RendererChoice::Gles);
        assert_eq!(RendererChoice::from_env(Some("pixman")), RendererChoice::Pixman);
        assert_eq!(RendererChoice::from_env(Some("software")), RendererChoice::Pixman);
    }

    #[test]
    fn a_seat_that_cannot_be_used_explains_itself_in_one_line() {
        let denied = ["Could not activate session: Access denied".to_owned()];
        let message = no_seat_message("the seat seat0 was opened but not enabled within 4 s", &denied);
        assert!(message.starts_with("no usable seat: the seat seat0 was opened but not enabled within 4 s"), "{message}");
        assert!(message.contains("libseat reported: Could not activate session: Access denied"), "{message}");
        // The three usual causes are named.
        for cause in ["login session on a virtual terminal", "polkit is not running", "another display server holds the seat"] {
            assert!(message.contains(cause), "{cause}: {message}");
        }
        assert!(!message.contains('\n') && !message.contains("  "), "one line: {message:?}");
        // Without anything from libseat there is nothing to quote.
        let message = no_seat_message("no seat could be opened (Failed to open session: No such device)", &[]);
        assert!(!message.contains("libseat reported"), "{message}");
        assert!(message.contains("polkit"), "{message}");
    }

    #[test]
    fn a_missing_graphics_device_says_whether_one_was_there() {
        let none = no_device_message(Duration::from_secs(10), None);
        assert!(none.starts_with("no graphics device appeared within 10 s"), "{none}");
        assert!(none.contains("nomodeset"), "{none}");
        let unusable = no_device_message(Duration::from_secs(10), Some("/dev/dri/card0: EGL is not available"));
        assert!(unusable.contains("last tried: /dev/dri/card0: EGL is not available"), "{unusable}");
        for message in [none, unusable] {
            assert!(!message.contains('\n') && !message.contains("  "), "one line: {message:?}");
        }
    }

    #[test]
    fn a_login_screen_without_an_output_says_why() {
        let unplugged = no_output_message(Duration::from_secs(5), None);
        assert!(unplugged.starts_with("no output within 5 s: the graphics device reports no connected monitor"), "{unplugged}");
        let failed = no_output_message(Duration::from_secs(5), Some("Virtual-1 could not be set up on /dev/dri/card0: no supported format"));
        assert!(failed.contains("Virtual-1 could not be set up on /dev/dri/card0: no supported format"), "{failed}");
        assert!(!failed.contains('\n') && !failed.contains("  "), "one line: {failed:?}");
    }
}
