//! The real-hardware backend.
//!
//! * session and device access through libseat (logind): the compositor
//!   runs as the logged-in user and never needs root
//! * displays through DRM/KMS with GBM buffers and an EGL/GLES renderer;
//!   planes, direct scan-out and damage are handled by Smithay's
//!   `DrmCompositor`
//! * input through libinput
//!
//! Outputs of the primary GPU are driven; connectors on a second GPU are
//! reported and left alone (multi-GPU output is a later milestone).

use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    time::Duration,
};

use smithay::{
    backend::{
        allocator::{
            dmabuf::Dmabuf,
            gbm::{GbmAllocator, GbmBufferFlags, GbmDevice},
            Fourcc,
        },
        drm::{
            compositor::FrameFlags,
            exporter::gbm::GbmFramebufferExporter,
            output::{DrmOutput, DrmOutputManager, DrmOutputRenderElements},
            DrmDevice, DrmDeviceFd, DrmEvent, DrmEventMetadata, DrmEventTime, DrmNode, NodeType,
        },
        egl::{context::ContextPriority, EGLContext, EGLDisplay},
        input::InputEvent,
        libinput::{LibinputInputBackend, LibinputSessionInterface},
        renderer::{gles::GlesRenderer, ImportDma, ImportMemWl},
        session::{libseat::LibSeatSession, Event as SessionEvent, Session},
        udev::{all_gpus, primary_gpu, UdevBackend, UdevEvent},
    },
    desktop::utils::OutputPresentationFeedback,
    input::keyboard::LedState,
    output::{Mode as OutputMode, Output, PhysicalProperties, Scale},
    reexports::{
        calloop::{
            timer::{TimeoutAction, Timer},
            RegistrationToken,
        },
        drm::control::{connector, crtc, Device as ControlDevice, ModeTypeFlags},
        input::{DeviceCapability, Libinput},
        rustix::fs::OFlags,
        wayland_protocols::wp::presentation_time::server::wp_presentation_feedback,
        wayland_server::backend::GlobalId,
    },
    utils::{Clock, DeviceFd, Monotonic},
    wayland::{dmabuf::DmabufFeedbackBuilder, presentation::Refresh},
};
use tracing::{debug, error, info, warn};

use super::Backend;
use crate::{render::OutputRenderElement, state::Boswas};

/// Scan-out formats tried in order.
const COLOR_FORMATS: &[Fourcc] = &[Fourcc::Argb8888, Fourcc::Xrgb8888, Fourcc::Abgr8888];

type OutputManager = DrmOutputManager<
    GbmAllocator<DrmDeviceFd>,
    GbmFramebufferExporter<DrmDeviceFd>,
    Option<OutputPresentationFeedback>,
    DrmDeviceFd,
>;
type KmsOutput = DrmOutput<
    GbmAllocator<DrmDeviceFd>,
    GbmFramebufferExporter<DrmDeviceFd>,
    Option<OutputPresentationFeedback>,
    DrmDeviceFd,
>;

/// One display.
struct Surface {
    output: Output,
    global: GlobalId,
    connector: connector::Handle,
    drm_output: KmsOutput,
    /// A frame is queued and its vblank has not arrived yet.
    pending: bool,
    /// A timer will send frame callbacks for a frame without damage.
    idle_callback_armed: bool,
}

/// The GPU that drives the displays.
struct Device {
    node: DrmNode,
    token: RegistrationToken,
    manager: OutputManager,
    renderer: GlesRenderer,
    surfaces: HashMap<crtc::Handle, Surface>,
}

pub struct Udev {
    session: LibSeatSession,
    device: Option<Device>,
    clock: Clock<Monotonic>,
    keyboards: Vec<smithay::reexports::input::Device>,
    active: bool,
}

/// Opens the session, the primary GPU and the input devices.
pub fn init(state: &mut Boswas) -> Result<(), Box<dyn std::error::Error>> {
    let (session, notifier) = LibSeatSession::new().map_err(|e| {
        format!("no session ({e}): boswas-compositor must be started from a login on a virtual terminal or by a display manager")
    })?;
    let seat = session.seat();

    // The GPU the firmware booted with, else the first one.
    let gpu: PathBuf = primary_gpu(&seat)
        .ok()
        .flatten()
        .or_else(|| all_gpus(&seat).ok().and_then(|gpus| gpus.into_iter().next()))
        .ok_or("no graphics device found")?;
    info!("using graphics device {}", gpu.display());

    state.backend = Backend::Udev(Box::new(Udev {
        session: session.clone(),
        device: None,
        clock: Clock::new(),
        keyboards: Vec::new(),
        active: true,
    }));
    with_udev(state, |udev, state| udev.add_device(state, &gpu))?;

    // Input.
    let mut libinput = Libinput::new_with_udev::<LibinputSessionInterface<LibSeatSession>>(session.clone().into());
    libinput
        .udev_assign_seat(&seat)
        .map_err(|()| "libinput could not open the seat's input devices")?;
    let input_backend = LibinputInputBackend::new(libinput.clone());
    state.loop_handle.insert_source(input_backend, |mut event, _, state| {
        match &mut event {
            InputEvent::DeviceAdded { device } => {
                configure_input_device(device, state);
                if device.has_capability(DeviceCapability::Keyboard) {
                    device.led_update(state.keyboard.led_state().into());
                    if let Backend::Udev(udev) = &mut state.backend {
                        udev.keyboards.push(device.clone());
                    }
                }
            }
            InputEvent::DeviceRemoved { device } => {
                if let Backend::Udev(udev) = &mut state.backend {
                    udev.keyboards.retain(|keyboard| keyboard != device);
                }
            }
            _ => {}
        }
        state.process_input_event(event);
    })?;

    // VT switches and suspend.
    state.loop_handle.insert_source(notifier, move |event, &mut (), state| match event {
        SessionEvent::PauseSession => {
            info!("session paused");
            libinput.suspend();
            if let Backend::Udev(udev) = &mut state.backend {
                udev.active = false;
                if let Some(device) = &mut udev.device {
                    device.manager.pause();
                    for surface in device.surfaces.values_mut() {
                        surface.pending = false;
                    }
                }
            }
        }
        SessionEvent::ActivateSession => {
            info!("session resumed");
            if libinput.resume().is_err() {
                warn!("libinput did not resume");
            }
            if let Backend::Udev(udev) = &mut state.backend {
                udev.active = true;
                if let Some(device) = &mut udev.device {
                    if let Err(err) = device.manager.activate(false) {
                        error!("could not reactivate the display device: {err}");
                    }
                    for surface in device.surfaces.values_mut() {
                        surface.pending = false;
                        surface.drm_output.reset_buffers();
                    }
                }
            }
            state.schedule_redraw();
        }
    })?;

    // Monitor hotplug.
    let udev_backend = UdevBackend::new(&seat)?;
    state.loop_handle.insert_source(udev_backend, |event, _, state| match event {
        UdevEvent::Changed { device_id } => {
            let _ = with_udev(state, |udev, state| {
                if udev.device.as_ref().is_some_and(|d| d.node.dev_id() == device_id) {
                    udev.scan_connectors(state);
                }
                Ok(())
            });
        }
        UdevEvent::Added { .. } => debug!("a graphics device appeared; outputs stay on the primary GPU"),
        UdevEvent::Removed { device_id } => {
            let _ = with_udev(state, |udev, state| {
                if udev.device.as_ref().is_some_and(|d| d.node.dev_id() == device_id) {
                    udev.remove_device(state);
                }
                Ok(())
            });
        }
    })?;
    Ok(())
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
        if !udev.active {
            return Ok(());
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

fn configure_input_device(device: &mut smithay::reexports::input::Device, state: &Boswas) {
    let config = &state.config.pointer;
    if device.config_tap_finger_count() > 0 {
        let _ = device.config_tap_set_enabled(config.tap_to_click);
    }
    if device.config_scroll_has_natural_scroll() {
        let _ = device.config_scroll_set_natural_scroll_enabled(config.natural_scroll);
    }
    if device.config_accel_is_available() {
        let _ = device.config_accel_set_speed(config.accel_speed);
    }
}

impl Udev {
    pub fn switch_vt(&mut self, vt: i32) {
        if let Err(err) = self.session.change_vt(vt) {
            warn!("could not switch to virtual terminal {vt}: {err}");
        }
    }

    pub fn import_dmabuf(&mut self, dmabuf: &Dmabuf) -> bool {
        self.device
            .as_mut()
            .is_some_and(|device| device.renderer.import_dmabuf(dmabuf, None).is_ok())
    }

    pub fn update_leds(&mut self, leds: LedState) {
        for keyboard in &mut self.keyboards {
            keyboard.led_update(leds.into());
        }
    }

    /// Answers pending screenshot requests with the first output.
    fn take_screenshots(&mut self, state: &mut Boswas) {
        if state.ipc.screenshots.is_empty() {
            return;
        }
        let requests = std::mem::take(&mut state.ipc.screenshots);
        let Some(device) = &mut self.device else {
            return;
        };
        let mut outputs: Vec<Output> = device.surfaces.values().map(|s| s.output.clone()).collect();
        outputs.sort_by_key(Output::name);
        for request in requests {
            let result = match outputs.first() {
                Some(output) => super::capture_gles(&mut device.renderer, state, output)
                    .and_then(|(w, h, rgba)| super::write_png(&request.path, w, h, rgba)),
                None => Err("no output is connected".into()),
            };
            state.ipc_screenshot_done(request, result);
        }
    }

    fn add_device(&mut self, state: &mut Boswas, path: &Path) -> Result<(), Box<dyn std::error::Error>> {
        let node = DrmNode::from_path(path)?;
        let fd = self.session.open(
            path,
            OFlags::RDWR | OFlags::CLOEXEC | OFlags::NOCTTY | OFlags::NONBLOCK,
        )?;
        let fd = DrmDeviceFd::new(DeviceFd::from(fd));
        let (drm, notifier) = DrmDevice::new(fd.clone(), true)?;
        let gbm = GbmDevice::new(fd)?;

        // SAFETY: the GBM device outlives the EGL display (both are owned by
        // `Device`, and the renderer holding the display is dropped first).
        let display = unsafe { EGLDisplay::new(gbm.clone())? };
        let context = EGLContext::new_with_priority(&display, ContextPriority::High)?;
        // SAFETY: the context was just created for this thread and is used
        // by this renderer only.
        let renderer = unsafe { GlesRenderer::new(context)? };

        let token = state.loop_handle.insert_source(notifier, move |event, metadata, state| match event {
            DrmEvent::VBlank(crtc) => {
                let _ = with_udev(state, |udev, state| {
                    udev.frame_finished(state, crtc, metadata);
                    Ok(())
                });
            }
            DrmEvent::Error(err) => error!("display device error: {err}"),
        })?;

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
        self.device = Some(Device {
            node,
            token,
            manager,
            renderer,
            surfaces: HashMap::new(),
        });
        self.scan_connectors(state);
        Ok(())
    }

    fn remove_device(&mut self, state: &mut Boswas) {
        if let Some(device) = self.device.take() {
            for (_, surface) in device.surfaces {
                state.space.unmap_output(&surface.output);
                state.display_handle.remove_global::<Boswas>(surface.global);
            }
            state.loop_handle.remove(device.token);
            if let Some(global) = state.dmabuf_global.take() {
                state.dmabuf_state.destroy_global::<Boswas>(&state.display_handle, global);
            }
        }
    }

    /// Brings outputs in line with the connected monitors.
    fn scan_connectors(&mut self, state: &mut Boswas) {
        let Some(device) = &mut self.device else {
            return;
        };
        let drm = device.manager.device();
        let Ok(resources) = drm.resource_handles() else {
            return warn!("could not read the display resources");
        };
        let connected: Vec<connector::Info> = resources
            .connectors()
            .iter()
            .filter_map(|handle| drm.get_connector(*handle, true).ok())
            .filter(|info| info.state() == connector::State::Connected && !info.modes().is_empty())
            .collect();

        // Monitors that went away.
        let gone: Vec<crtc::Handle> = device
            .surfaces
            .iter()
            .filter(|(_, surface)| !connected.iter().any(|info| info.handle() == surface.connector))
            .map(|(crtc, _)| *crtc)
            .collect();
        for crtc in gone {
            if let Some(surface) = device.surfaces.remove(&crtc) {
                info!("output {} disconnected", surface.output.name());
                state.space.unmap_output(&surface.output);
                state.display_handle.remove_global::<Boswas>(surface.global);
            }
        }

        // New monitors: each takes the first free CRTC one of its encoders
        // can drive.
        let mut taken: Vec<crtc::Handle> = device.surfaces.keys().copied().collect();
        let mut assignments = Vec::new();
        for info in connected {
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
                None => warn!("no free CRTC for connector {:?}-{}", info.interface(), info.interface_id()),
            }
        }
        for (info, crtc) in assignments {
            if let Err(err) = connect(device, state, &info, crtc) {
                warn!("could not set up {:?}-{}: {err}", info.interface(), info.interface_id());
            }
        }
        arrange_outputs(device, state);
        state.schedule_redraw();
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
        state.update_decorations();
        let (elements, clear) = state.output_elements(&mut device.renderer, &output);
        let result = surface
            .drm_output
            .render_frame(&mut device.renderer, &elements, clear, FrameFlags::DEFAULT);
        let (rendered, states) = match result {
            Ok(frame) => (!frame.is_empty, frame.states),
            Err(err) => {
                warn!("rendering {} failed: {err}", output.name());
                return;
            }
        };
        drop(elements);

        let time = state.start_time.elapsed();
        if rendered {
            let feedback = state.take_presentation_feedback(&output, &states);
            match surface.drm_output.queue_frame(Some(feedback)) {
                Ok(()) => surface.pending = true,
                Err(err) => warn!("could not queue a frame for {}: {err}", output.name()),
            }
            state.post_repaint(&output, &states, time);
        } else if !surface.idle_callback_armed {
            // Nothing changed on screen. Clients waiting for a frame
            // callback still get one, at the display's pace, so a client
            // that commits without damage cannot spin the compositor.
            surface.idle_callback_armed = true;
            let refresh = output.current_mode().map_or(60_000, |mode| mode.refresh.max(1));
            let delay = Duration::from_micros(1_000_000_000 / refresh as u64);
            let timer = Timer::from_duration(delay);
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
    }

    fn frame_finished(&mut self, state: &mut Boswas, crtc: crtc::Handle, metadata: &mut Option<DrmEventMetadata>) {
        let Some(surface) = self.device.as_mut().and_then(|d| d.surfaces.get_mut(&crtc)) else {
            return;
        };
        surface.pending = false;
        let refresh = surface.output.current_mode().map_or(60_000, |mode| mode.refresh.max(1));
        let frame = Duration::from_micros(1_000_000_000 / refresh as u64);
        match surface.drm_output.frame_submitted() {
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
                feedback.presented::<_, Monotonic>(time, Refresh::fixed(frame), sequence, flags);
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

/// Creates the output for a newly connected monitor.
fn connect(
    device: &mut Device,
    state: &mut Boswas,
    info: &connector::Info,
    crtc: crtc::Handle,
) -> Result<(), Box<dyn std::error::Error>> {
    let name = format!("{}-{}", info.interface().as_str(), info.interface_id());
    let mode = info
        .modes()
        .iter()
        .find(|mode| mode.mode_type().contains(ModeTypeFlags::PREFERRED))
        .or_else(|| info.modes().first())
        .copied()
        .ok_or("the monitor reports no mode")?;
    let (width_mm, height_mm) = info.size().unwrap_or((0, 0));
    let output = Output::new(
        name.clone(),
        PhysicalProperties {
            size: (width_mm as i32, height_mm as i32).into(),
            subpixel: info.subpixel().into(),
            make: "Unknown".into(),
            model: "Unknown".into(),
        },
    );
    let output_mode = OutputMode::from(mode);
    output.set_preferred(output_mode);
    output.change_current_state(
        Some(output_mode),
        None,
        Some(Scale::Fractional(state.config.output.scale)),
        None,
    );

    let planes = device.manager.device().planes(&crtc).ok();
    let drm_output = device
        .manager
        .initialize_output::<GlesRenderer, OutputRenderElement<GlesRenderer>>(
            crtc,
            mode,
            &[info.handle()],
            &output,
            planes,
            &mut device.renderer,
            &DrmOutputRenderElements::default(),
        )
        .map_err(|err| format!("{err}"))?;
    let global = output.create_global::<Boswas>(&state.display_handle);
    info!("output {name}: {}x{} @ {} mHz", output_mode.size.w, output_mode.size.h, output_mode.refresh);
    device.surfaces.insert(
        crtc,
        Surface {
            output,
            global,
            connector: info.handle(),
            drm_output,
            pending: false,
            idle_callback_armed: false,
        },
    );
    Ok(())
}

/// Places the outputs side by side, left to right, in connector order.
fn arrange_outputs(device: &Device, state: &mut Boswas) {
    let mut surfaces: Vec<&Surface> = device.surfaces.values().collect();
    surfaces.sort_by_key(|surface| surface.output.name());
    let mut x = 0;
    let mut changed = Vec::new();
    for surface in surfaces {
        let position = (x, 0);
        surface.output.change_current_state(None, None, None, Some(position.into()));
        state.space.map_output(&surface.output, position);
        x += state.space.output_geometry(&surface.output).map_or(0, |geo| geo.size.w);
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
