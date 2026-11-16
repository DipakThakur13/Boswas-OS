//! The compositor's state: every protocol global, the desktop (windows,
//! layers, workspaces), the seat and the session lock.

use std::{
    collections::{HashMap, HashSet},
    ffi::OsString,
    os::unix::net::UnixStream,
    process::Child,
    sync::Arc,
    time::{Duration, Instant},
};

use boswas_ui::{text::TextEngine, Scheme, Theme};
use smithay::{
    desktop::{layer_map_for_output, PopupManager, Space, WindowSurfaceType},
    input::{
        keyboard::{KeyboardHandle, XkbConfig},
        pointer::{CursorImageStatus, PointerHandle},
        Seat, SeatState,
    },
    output::Output,
    reexports::{
        calloop::{generic::Generic, Interest, LoopHandle, LoopSignal, Mode, PostAction},
        wayland_server::{
            backend::{ClientData, ClientId, DisconnectReason},
            protocol::wl_surface::WlSurface,
            Client, Display, DisplayHandle,
        },
    },
    utils::{Logical, Point, Rectangle, Size},
    wayland::{
        alpha_modifier::AlphaModifierState,
        compositor::{CompositorClientState, CompositorState},
        content_type::ContentTypeState,
        cursor_shape::CursorShapeManagerState,
        dmabuf::{DmabufGlobal, DmabufState},
        foreign_toplevel_list::ForeignToplevelListState,
        fractional_scale::FractionalScaleManagerState,
        idle_inhibit::IdleInhibitManagerState,
        idle_notify::IdleNotifierState,
        input_method::InputMethodManagerState,
        keyboard_shortcuts_inhibit::KeyboardShortcutsInhibitState,
        output::OutputManagerState,
        pointer_constraints::PointerConstraintsState,
        pointer_gestures::PointerGesturesState,
        presentation::PresentationState,
        relative_pointer::RelativePointerManagerState,
        security_context::{SecurityContext, SecurityContextState},
        selection::{
            data_device::DataDeviceState, primary_selection::PrimarySelectionState,
            wlr_data_control::DataControlState,
        },
        session_lock::{LockSurface, SessionLockManagerState},
        shell::{
            wlr_layer::{Layer, WlrLayerShellState},
            xdg::{decoration::XdgDecorationState, XdgShellState},
        },
        shm::ShmState,
        single_pixel_buffer::SinglePixelBufferState,
        socket::ListeningSocketSource,
        tablet_manager::TabletManagerState,
        text_input::TextInputManagerState,
        viewporter::ViewporterState,
        virtual_keyboard::VirtualKeyboardManagerState,
        xdg_activation::XdgActivationState,
        xdg_foreign::XdgForeignState,
        xdg_system_bell::XdgSystemBellState,
        xwayland_shell::XWaylandShellState,
    },
    xwayland::X11Wm,
};
use tracing::{info, warn};

use crate::{
    animation::{Ghost, GhostKind},
    backend::Backend,
    config::Config,
    cursor::CursorTheme,
    handlers::screencopy::Screencopy,
    ipc::Ipc,
    render::Overlays,
    shell::{
        appicon::AppIcons,
        decoration::{Button, Decoration, RESIZE_MARGIN},
        element::{Tile, WindowElement},
        grabs::ResizeEdge,
        overview::Overview,
        snapflyout::SnapFlyout,
        switcher::Switcher,
        windowmenu::{KeyAdjust, WindowMenu},
    },
};

/// The most exit animations at once; more (a session closing all its
/// windows) simply end without one.
const MAX_GHOSTS: usize = 8;

/// How far a client is trusted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Trust {
    /// Started by the compositor itself (the shell, the lock screen): may
    /// use the privileged protocols.
    Shell,
    /// An ordinary application.
    Normal,
    /// Connected through a security context (Flatpak and other sandboxes):
    /// never sees a privileged protocol.
    Sandboxed,
}

/// Data attached to every Wayland client.
pub struct ClientState {
    pub compositor_state: CompositorClientState,
    pub trust: Trust,
    pub security_context: Option<SecurityContext>,
}

impl ClientState {
    pub fn new(trust: Trust) -> Self {
        Self {
            compositor_state: CompositorClientState::default(),
            trust,
            security_context: None,
        }
    }
}

impl ClientData for ClientState {
    fn initialized(&self, _client_id: ClientId) {}
    fn disconnected(&self, _client_id: ClientId, _reason: DisconnectReason) {}
}

/// The session lock, owned by the compositor: once `locked` is set nothing
/// but the lock surfaces is drawn or receives input until a lock client
/// unlocks, even if that client crashes in between.
#[derive(Default)]
pub struct Lock {
    pub locked: bool,
    pub surfaces: HashMap<Output, LockSurface>,
    /// When the lock program was last started.
    pub client_started: Option<Instant>,
}

/// What lies under a point of the desktop.
#[derive(Debug, Clone)]
pub enum Under {
    /// A client surface and the global position of its origin.
    Surface(WlSurface, Point<f64, Logical>),
    /// The title bar of a window (or one of its buttons).
    TitleBar(WindowElement, Option<Button>),
    /// The resize border of a floating window.
    Resize(WindowElement, ResizeEdge),
}

impl Under {
    /// The pointer focus this target gives.
    pub fn focus(&self) -> Option<(WlSurface, Point<f64, Logical>)> {
        match self {
            Under::Surface(surface, origin) => Some((surface.clone(), *origin)),
            _ => None,
        }
    }
}

/// Protocol state that is never consulted after start-up. Each value owns
/// a protocol global, which exists for as long as the value does.
#[allow(dead_code)]
struct Globals {
    xdg_decoration_state: XdgDecorationState,
    output_manager_state: OutputManagerState,
    viewporter_state: ViewporterState,
    fractional_scale_state: FractionalScaleManagerState,
    presentation_state: PresentationState,
    cursor_shape_state: CursorShapeManagerState,
    single_pixel_buffer_state: SinglePixelBufferState,
    security_context_state: SecurityContextState,
    idle_inhibit_state: IdleInhibitManagerState,
    pointer_constraints_state: PointerConstraintsState,
    relative_pointer_state: RelativePointerManagerState,
    pointer_gestures_state: PointerGesturesState,
    text_input_state: TextInputManagerState,
    input_method_state: InputMethodManagerState,
    virtual_keyboard_state: VirtualKeyboardManagerState,
    alpha_modifier_state: AlphaModifierState,
    content_type_state: ContentTypeState,
    tablet_manager_state: TabletManagerState,
    system_bell_state: XdgSystemBellState,
}

pub struct Boswas {
    pub start_time: Instant,
    pub display_handle: DisplayHandle,
    pub loop_handle: LoopHandle<'static, Boswas>,
    pub loop_signal: LoopSignal,
    pub socket_name: OsString,
    pub config: Config,
    pub theme: &'static Theme,
    text: Option<TextEngine>,
    pub backend: Backend,

    // Desktop
    pub space: Space<WindowElement>,
    pub popups: PopupManager,
    /// Every toplevel, in creation order. Stacking lives in the space, focus
    /// history in `focus_history`.
    pub windows: Vec<WindowElement>,
    /// Window ids, least recently focused first.
    pub focus_history: Vec<u64>,
    pub workspace: usize,
    pub workspace_count: usize,
    next_window_id: u64,
    pub(crate) cascade: i32,
    /// Where a dragged window would snap to; drawn as an overlay.
    pub snap_preview: Option<Rectangle<i32, Logical>>,
    /// The window switcher, while Alt is held after Alt+Tab.
    pub switcher: Option<Switcher>,
    /// The windows "show desktop" minimized, to bring back on the next
    /// toggle; `None` while the desktop is not being shown.
    pub show_desktop: Option<Vec<u64>>,
    /// Whether night light is switched on (the schedule may still keep the
    /// screen neutral) and its colour temperature.
    pub night_light: (bool, u32),
    /// The colour temperature the outputs currently show (`None`: neutral).
    pub night_light_applied: Option<u32>,
    /// The names of the keyboard layouts and the index of the active one.
    pub layouts: (Vec<String>, u32),
    /// Task View, while it is open.
    pub overview: Option<Overview>,
    /// The snap layouts flyout of a maximize button, while it is open.
    pub snap_flyout: Option<SnapFlyout>,
    /// Screen capture for other programs, and who may capture.
    pub screencopy: Screencopy,
    /// The picture being rendered is a capture that asked for no pointer.
    pub capture_without_cursor: bool,
    /// The window menu (Restore, Move, Size, ...), while it is open.
    pub window_menu: Option<WindowMenu>,
    /// The window being moved or resized with the arrow keys.
    pub key_adjust: Option<KeyAdjust>,
    /// The window whose maximize button the pointer rests on, and since
    /// when.
    pub maximize_hover: Option<(u64, Instant)>,
    /// Whether the backend's renderer clips window content to rounded
    /// corners (the GL renderer does). Frames draw their border to match.
    pub rounds_corners: bool,
    /// Pictures of windows that are on their way out (closing, minimizing,
    /// the desktop that was left).
    pub ghosts: Vec<Ghost>,
    /// Application icons for frames, the switcher and Task View.
    pub app_icons: AppIcons,
    /// Connected outputs the configuration turned off.
    pub disabled_outputs: Vec<String>,
    /// The screens are turned off for idleness; any input turns them on.
    pub outputs_off: bool,

    // Protocol state
    _globals: Globals,
    pub compositor_state: CompositorState,
    pub xdg_shell_state: XdgShellState,
    pub xdg_activation_state: XdgActivationState,
    pub layer_shell_state: WlrLayerShellState,
    pub session_lock_state: SessionLockManagerState,
    pub shm_state: ShmState,
    pub dmabuf_state: DmabufState,
    pub dmabuf_global: Option<DmabufGlobal>,
    pub seat_state: SeatState<Boswas>,
    pub data_device_state: DataDeviceState,
    pub primary_selection_state: PrimarySelectionState,
    pub data_control_state: DataControlState,
    pub foreign_toplevel_state: ForeignToplevelListState,
    pub shortcuts_inhibit_state: KeyboardShortcutsInhibitState,
    pub xdg_foreign_state: XdgForeignState,
    pub idle_notifier_state: IdleNotifierState<Boswas>,
    pub xwayland_shell_state: XWaylandShellState,
    /// The X11 window manager, while XWayland runs.
    pub xwm: Option<X11Wm>,
    /// The X display applications reach XWayland on.
    pub x_display: Option<u32>,

    // Seat
    pub seat: Seat<Boswas>,
    pub pointer: PointerHandle<Boswas>,
    pub keyboard: KeyboardHandle<Boswas>,
    pub cursor_status: CursorImageStatus,
    pub cursor_theme: CursorTheme,
    pub dnd_icon: Option<WlSurface>,
    /// Set while Super is held and nothing else has been pressed: releasing
    /// it then opens the launcher.
    pub super_tap: bool,
    /// Keys whose press was a shortcut; their release is swallowed too.
    pub suppressed_keys: HashSet<u32>,
    /// The title bar button the left mouse button went down on.
    pub pressed_button: Option<(u64, Button)>,
    /// Window and time of the last title bar click (double-click maximizes).
    pub last_titlebar_click: Option<(u64, Instant)>,
    pub overlays: Overlays,

    // Session
    pub lock: Lock,
    pub last_input: Instant,
    pub idle_inhibitors: Vec<WlSurface>,
    pub ipc: Ipc,
    pub children: Vec<Child>,
    shell_started: Vec<Instant>,
    /// The input method's control connection (its liveness) and when it
    /// was last started.
    input_method: (Option<u64>, Vec<Instant>),
    /// A three- or four-finger swipe on the touchpad the compositor is
    /// following: finger count and the distance so far.
    pub swipe: Option<(u32, Point<f64, Logical>)>,
    /// The touch points that act as the pointer, because they went down on
    /// something the compositor draws itself.
    pub touch_pointer: Option<smithay::backend::input::TouchSlot>,
    /// Outputs that need a new frame.
    pub redraw: bool,
}

impl Boswas {
    pub fn new(
        display: Display<Boswas>,
        loop_handle: LoopHandle<'static, Boswas>,
        loop_signal: LoopSignal,
        config: Config,
        backend: Backend,
        socket: Option<&str>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let dh = display.handle();
        let trust_all = config.trust_all_clients();
        // Privileged globals are visible to the programs the compositor
        // started itself; `policy.privileged_protocols = "all"` (development)
        // widens that to every client outside a sandbox.
        let privileged = move |client: &Client| match client.get_data::<ClientState>().map(|c| c.trust) {
            Some(Trust::Shell) => true,
            Some(Trust::Normal) => trust_all,
            _ => false,
        };
        let not_sandboxed =
            |client: &Client| client.get_data::<ClientState>().is_some_and(|c| c.trust != Trust::Sandboxed);

        let compositor_state = CompositorState::new::<Self>(&dh);
        let xdg_shell_state = XdgShellState::new::<Self>(&dh);
        let xdg_decoration_state = XdgDecorationState::new::<Self>(&dh);
        let xdg_activation_state = XdgActivationState::new::<Self>(&dh);
        let layer_shell_state = WlrLayerShellState::new_with_filter::<Self, _>(&dh, privileged);
        // Screen capture is behind the user's consent, not behind trust:
        // every program outside a sandbox may ask.
        let screencopy = Screencopy::new(&dh, not_sandboxed);
        let session_lock_state = SessionLockManagerState::new::<Self, _>(&dh, privileged);
        let shm_state = ShmState::new::<Self>(&dh, vec![]);
        let dmabuf_state = DmabufState::new();
        let output_manager_state = OutputManagerState::new_with_xdg_output::<Self>(&dh);
        let mut seat_state = SeatState::new();
        let data_device_state = DataDeviceState::new::<Self>(&dh);
        let primary_selection_state = PrimarySelectionState::new::<Self>(&dh);
        let data_control_state =
            DataControlState::new::<Self, _>(&dh, Some(&primary_selection_state), privileged);
        let viewporter_state = ViewporterState::new::<Self>(&dh);
        let fractional_scale_state = FractionalScaleManagerState::new::<Self>(&dh);
        let presentation_state = PresentationState::new::<Self>(&dh, libc_clock_monotonic());
        let cursor_shape_state = CursorShapeManagerState::new::<Self>(&dh);
        let single_pixel_buffer_state = SinglePixelBufferState::new::<Self>(&dh);
        let security_context_state = SecurityContextState::new::<Self, _>(&dh, not_sandboxed);
        let foreign_toplevel_state = ForeignToplevelListState::new_with_filter::<Self>(&dh, privileged);
        let idle_inhibit_state = IdleInhibitManagerState::new::<Self>(&dh);
        let pointer_constraints_state = PointerConstraintsState::new::<Self>(&dh);
        let relative_pointer_state = RelativePointerManagerState::new::<Self>(&dh);
        let pointer_gestures_state = PointerGesturesState::new::<Self>(&dh);
        // Text input is for every application. Being the input method, or a
        // virtual keyboard, means seeing or making key strokes for all of
        // them: that is for the program the administrator configured, which
        // the compositor starts itself.
        let text_input_state = TextInputManagerState::new::<Self>(&dh);
        let input_method_state = InputMethodManagerState::new::<Self, _>(&dh, privileged);
        let virtual_keyboard_state = VirtualKeyboardManagerState::new::<Self, _>(&dh, privileged);
        let alpha_modifier_state = AlphaModifierState::new::<Self>(&dh);
        let content_type_state = ContentTypeState::new::<Self>(&dh);
        let tablet_manager_state = TabletManagerState::new::<Self>(&dh);
        let system_bell_state = XdgSystemBellState::new::<Self>(&dh);
        let xdg_foreign_state = XdgForeignState::new::<Self>(&dh);
        let idle_notifier_state = IdleNotifierState::new(&dh, loop_handle.clone());
        let xwayland_shell_state = XWaylandShellState::new::<Self>(&dh);
        let shortcuts_inhibit_state = KeyboardShortcutsInhibitState::new::<Self>(&dh);

        let mut seat: Seat<Self> = seat_state.new_wl_seat(&dh, "seat0");
        let xkb = XkbConfig {
            layout: &config.keyboard.layout,
            variant: &config.keyboard.variant,
            model: &config.keyboard.model,
            options: Some(config.keyboard.options.clone()).filter(|o| !o.is_empty()),
            ..XkbConfig::default()
        };
        let keyboard = match seat.add_keyboard(xkb, config.keyboard.repeat_delay, config.keyboard.repeat_rate) {
            Ok(keyboard) => keyboard,
            Err(err) => {
                warn!("keyboard layout \"{}\" rejected ({err:?}); using the default", config.keyboard.layout);
                seat.add_keyboard(XkbConfig::default(), config.keyboard.repeat_delay, config.keyboard.repeat_rate)
                    .map_err(|e| format!("no usable keyboard layout: {e:?}"))?
            }
        };
        let pointer = seat.add_pointer();

        let socket_name = Self::listen(display, &loop_handle, socket)?;
        let theme = Theme::of(if config.appearance.scheme == "light" {
            Scheme::Light
        } else {
            Scheme::Dark
        });
        let cursor_theme = CursorTheme::load(&config.pointer.cursor_theme, config.pointer.cursor_size);

        Ok(Self {
            start_time: Instant::now(),
            display_handle: dh,
            loop_handle,
            loop_signal,
            socket_name,
            theme,
            text: None,
            backend,
            space: Space::default(),
            popups: PopupManager::default(),
            windows: Vec::new(),
            focus_history: Vec::new(),
            workspace: 0,
            workspace_count: 1,
            next_window_id: 1,
            cascade: 0,
            snap_preview: None,
            switcher: None,
            show_desktop: None,
            night_light: (config.night_light.enabled, config.night_light.temperature),
            night_light_applied: None,
            layouts: (Vec::new(), 0),
            overview: None,
            snap_flyout: None,
            screencopy,
            capture_without_cursor: false,
            window_menu: None,
            key_adjust: None,
            maximize_hover: None,
            rounds_corners: false,
            ghosts: Vec::new(),
            app_icons: AppIcons::new(config.appearance.scheme == "light"),
            disabled_outputs: Vec::new(),
            outputs_off: false,
            _globals: Globals {
                xdg_decoration_state,
                output_manager_state,
                viewporter_state,
                fractional_scale_state,
                presentation_state,
                cursor_shape_state,
                single_pixel_buffer_state,
                security_context_state,
                idle_inhibit_state,
                pointer_constraints_state,
                relative_pointer_state,
                pointer_gestures_state,
                text_input_state,
                input_method_state,
                virtual_keyboard_state,
                alpha_modifier_state,
                content_type_state,
                tablet_manager_state,
                system_bell_state,
            },
            compositor_state,
            xdg_shell_state,
            xdg_activation_state,
            layer_shell_state,
            session_lock_state,
            shm_state,
            dmabuf_state,
            dmabuf_global: None,
            seat_state,
            data_device_state,
            primary_selection_state,
            data_control_state,
            foreign_toplevel_state,
            shortcuts_inhibit_state,
            xdg_foreign_state,
            idle_notifier_state,
            xwayland_shell_state,
            xwm: None,
            x_display: None,
            seat,
            pointer,
            keyboard,
            cursor_status: CursorImageStatus::default_named(),
            cursor_theme,
            dnd_icon: None,
            super_tap: false,
            suppressed_keys: HashSet::new(),
            pressed_button: None,
            last_titlebar_click: None,
            overlays: Overlays::default(),
            lock: Lock::default(),
            last_input: Instant::now(),
            idle_inhibitors: Vec::new(),
            ipc: Ipc::default(),
            children: Vec::new(),
            shell_started: Vec::new(),
            input_method: (None, Vec::new()),
            swipe: None,
            touch_pointer: None,
            redraw: true,
            config,
        })
    }

    /// Opens the public Wayland socket and adds the display to the loop.
    fn listen(
        display: Display<Boswas>,
        loop_handle: &LoopHandle<'static, Boswas>,
        name: Option<&str>,
    ) -> Result<OsString, Box<dyn std::error::Error>> {
        let source = match name {
            Some(name) => ListeningSocketSource::with_name(name)?,
            None => ListeningSocketSource::new_auto()?,
        };
        let socket_name = source.socket_name().to_os_string();
        loop_handle.insert_source(source, |stream, _, state| {
            if let Err(err) = state
                .display_handle
                .insert_client(stream, Arc::new(ClientState::new(Trust::Normal)))
            {
                warn!("could not accept a Wayland client: {err}");
            }
        })?;
        loop_handle.insert_source(
            Generic::new(display, Interest::READ, Mode::Level),
            |_, display, state| {
                // SAFETY: the display is owned by this source and is never
                // dropped while the event loop runs.
                unsafe {
                    if let Err(err) = display.get_mut().dispatch_clients(state) {
                        warn!("dispatching Wayland clients failed: {err}");
                    }
                }
                Ok(PostAction::Continue)
            },
        )?;
        Ok(socket_name)
    }

    pub fn next_window_id(&mut self) -> u64 {
        let id = self.next_window_id;
        self.next_window_id += 1;
        id
    }

    /// The text engine; system fonts are scanned on first use.
    pub fn text(&mut self) -> &mut TextEngine {
        self.text.get_or_insert_with(TextEngine::new)
    }

    /// Where windows fly to when they are minimized: the middle of the
    /// taskbar of the output a rectangle is on.
    pub fn taskbar_point(&self, rect: Rectangle<i32, Logical>) -> Point<f64, Logical> {
        let Some(output) = self.output_for_rect(rect) else {
            return Point::from((f64::from(rect.loc.x), f64::from(rect.loc.y + rect.size.h)));
        };
        let geo = self.space.output_geometry(&output).unwrap_or_default();
        let usable = self.usable_area(&output);
        // The taskbar is whatever the shell reserved at the bottom.
        let reserved = (geo.loc.y + geo.size.h) - (usable.loc.y + usable.size.h);
        Point::from((
            f64::from(geo.loc.x) + f64::from(geo.size.w) / 2.0,
            f64::from(geo.loc.y + geo.size.h) - f64::from(reserved) / 2.0,
        ))
    }

    /// Takes a picture of a window and lets it play an exit animation.
    pub fn leave_ghost(&mut self, window: &WindowElement, kind: GhostKind) {
        if self.reduced_motion() || self.lock.locked || self.ghosts.len() >= MAX_GHOSTS {
            return;
        }
        let mut backend = std::mem::take(&mut self.backend);
        let picture = backend.snapshot(self, window);
        self.backend = backend;
        if let Some((buffer, rect)) = picture {
            self.ghosts.push(Ghost::new(buffer, rect, kind));
            self.schedule_redraw();
        }
    }

    /// Asks for a new frame on every output.
    pub fn schedule_redraw(&mut self) {
        self.redraw = true;
    }

    // ---------------------------------------------------------------------
    // Outputs
    // ---------------------------------------------------------------------

    /// The output under the pointer, or the first one.
    pub fn active_output(&self) -> Option<Output> {
        let pos = self.pointer.current_location();
        self.space
            .outputs()
            .find(|o| self.space.output_geometry(o).is_some_and(|g| g.to_f64().contains(pos)))
            .or_else(|| self.space.outputs().next())
            .cloned()
    }

    /// The output a rectangle is mostly on.
    pub fn output_for_rect(&self, rect: Rectangle<i32, Logical>) -> Option<Output> {
        self.space
            .outputs()
            .filter_map(|o| {
                let geo = self.space.output_geometry(o)?;
                let overlap = geo.intersection(rect)?;
                Some((overlap.size.w * overlap.size.h, o))
            })
            .max_by_key(|(area, _)| *area)
            .map(|(_, o)| o.clone())
            .or_else(|| self.active_output())
    }

    /// The part of an output not reserved by panels, in global coordinates.
    pub fn usable_area(&self, output: &Output) -> Rectangle<i32, Logical> {
        let geo = self.space.output_geometry(output).unwrap_or_default();
        let mut zone = layer_map_for_output(output).non_exclusive_zone();
        zone.loc += geo.loc;
        if zone.size.w <= 0 || zone.size.h <= 0 {
            geo
        } else {
            zone
        }
    }

    /// The largest output scale, used for artwork shared across outputs.
    pub fn max_scale(&self) -> f64 {
        self.space
            .outputs()
            .map(|o| o.current_scale().fractional_scale())
            .fold(1.0, f64::max)
    }

    // ---------------------------------------------------------------------
    // Windows
    // ---------------------------------------------------------------------

    pub fn window_by_id(&self, id: u64) -> Option<WindowElement> {
        self.windows.iter().find(|w| w.id() == id).cloned()
    }

    /// The window a surface belongs to (its toplevel surface only).
    pub fn window_for_surface(&self, surface: &WlSurface) -> Option<WindowElement> {
        self.windows.iter().find(|w| w.wl_surface().as_ref() == Some(surface)).cloned()
    }

    /// The surface the keyboard is on.
    pub fn keyboard_surface(&self) -> Option<WlSurface> {
        self.keyboard.current_focus().map(|focus| focus.surface().clone())
    }

    /// The focused window, if the keyboard focus is on a window.
    pub fn focused_window(&self) -> Option<WindowElement> {
        let focus = self.keyboard.current_focus()?;
        self.window_for_surface(&root_surface(focus.surface()))
    }

    /// Global position of a window's element origin (where the space draws
    /// it), as opposed to its frame's top-left corner.
    pub fn window_origin(&self, window: &WindowElement) -> Option<Point<i32, Logical>> {
        let location = self.space.element_location(window)?;
        Some(location - window.frame().loc)
    }

    /// The frame rectangle of a mapped window in global coordinates.
    pub fn window_rect(&self, window: &WindowElement) -> Option<Rectangle<i32, Logical>> {
        let location = self.space.element_location(window)?;
        Some(Rectangle::new(location, window.frame().size))
    }

    /// The fullscreen window on an output's current workspace, if any.
    pub fn fullscreen_window(&self, output: &Output) -> Option<WindowElement> {
        let geo = self.space.output_geometry(output)?;
        self.space
            .elements()
            .rev()
            .find(|w| {
                w.data().tile.get() == Tile::Fullscreen
                    && self.window_rect(w).is_some_and(|r| r.overlaps(geo))
            })
            .cloned()
    }

    // ---------------------------------------------------------------------
    // Hit testing
    // ---------------------------------------------------------------------

    /// What is under a global position, front to back.
    pub fn under(&self, pos: Point<f64, Logical>) -> Option<Under> {
        let output = self
            .space
            .outputs()
            .find(|o| self.space.output_geometry(o).is_some_and(|g| g.to_f64().contains(pos)))?;
        let output_geo = self.space.output_geometry(output)?;

        if self.lock.locked {
            let surface = self.lock.surfaces.get(output)?;
            return Some(Under::Surface(surface.wl_surface().clone(), output_geo.loc.to_f64()));
        }

        let layers = layer_map_for_output(output);
        let layer_under = |layer: Layer| {
            let local = pos - output_geo.loc.to_f64();
            let surface = layers.layer_under(layer, local)?;
            let geo = layers.layer_geometry(surface)?;
            let (wl, loc) = surface.surface_under(local - geo.loc.to_f64(), WindowSurfaceType::ALL)?;
            Some(Under::Surface(wl, (loc + geo.loc + output_geo.loc).to_f64()))
        };

        if let Some(under) = layer_under(Layer::Overlay) {
            return Some(under);
        }
        if let Some(window) = self.fullscreen_window(output) {
            let origin = self.window_origin(&window)?;
            return window
                .surface_under(pos - origin.to_f64())
                .map(|(surface, loc)| Under::Surface(surface, loc + origin.to_f64()));
        }
        if let Some(under) = layer_under(Layer::Top) {
            return Some(under);
        }
        for window in self.space.elements().rev() {
            if let Some(under) = self.window_under(window, pos) {
                return Some(under);
            }
        }
        layer_under(Layer::Bottom).or_else(|| layer_under(Layer::Background))
    }

    fn window_under(&self, window: &WindowElement, pos: Point<f64, Logical>) -> Option<Under> {
        let origin = self.window_origin(window)?.to_f64();
        let rect = self.window_rect(window)?.to_f64();
        let local = pos - origin;
        // Popups and subsurfaces may reach outside the frame.
        if let Some((surface, loc)) = window.surface_under(local) {
            return Some(Under::Surface(surface, loc + origin));
        }
        let data = window.data();
        let in_frame = rect.contains(pos);
        if in_frame && window.titlebar_height() > 0 && pos.y < rect.loc.y + f64::from(window.titlebar_height()) {
            let button = Decoration::button_at(rect.size.w as i32, pos - rect.loc);
            return Some(Under::TitleBar(window.clone(), button));
        }
        if !in_frame && data.ssd.get() && data.tile.get() == Tile::Floating {
            let m = f64::from(RESIZE_MARGIN);
            let grown = Rectangle::new(
                Point::from((rect.loc.x - m, rect.loc.y - m)),
                Size::from((rect.size.w + 2.0 * m, rect.size.h + 2.0 * m)),
            );
            if grown.contains(pos) {
                let mut edge = ResizeEdge::empty();
                if pos.x < rect.loc.x {
                    edge |= ResizeEdge::LEFT;
                } else if pos.x >= rect.loc.x + rect.size.w {
                    edge |= ResizeEdge::RIGHT;
                }
                if pos.y < rect.loc.y {
                    edge |= ResizeEdge::TOP;
                } else if pos.y >= rect.loc.y + rect.size.h {
                    edge |= ResizeEdge::BOTTOM;
                }
                return Some(Under::Resize(window.clone(), edge));
            }
        }
        None
    }

    // ---------------------------------------------------------------------
    // Trusted programs
    // ---------------------------------------------------------------------

    /// Starts a program as a trusted client: it inherits one end of a fresh
    /// socket pair as `WAYLAND_SOCKET` (and one for the control socket as
    /// `BOSWAS_COMPOSITOR_FD`), so the privilege is tied to the process the
    /// compositor itself started and cannot be claimed by any other client.
    pub fn spawn_trusted(&mut self, command: &[String]) -> Result<Option<u64>, String> {
        let (program, args) = command.split_first().ok_or("empty command")?;
        let (wl_ours, wl_theirs) = UnixStream::pair().map_err(|e| e.to_string())?;
        let (ipc_ours, ipc_theirs) = UnixStream::pair().map_err(|e| e.to_string())?;
        for fd in [&wl_theirs, &ipc_theirs] {
            // Let exactly these two descriptors survive exec.
            rustix::io::fcntl_setfd(fd, rustix::io::FdFlags::empty()).map_err(|e| e.to_string())?;
        }
        use std::os::fd::AsRawFd;
        let mut cmd = std::process::Command::new(program);
        cmd.args(args)
            .env("WAYLAND_SOCKET", wl_theirs.as_raw_fd().to_string())
            .env("BOSWAS_COMPOSITOR_FD", ipc_theirs.as_raw_fd().to_string())
            .env("WAYLAND_DISPLAY", &self.socket_name)
            .env("XDG_CURRENT_DESKTOP", "Boswas")
            .stdin(std::process::Stdio::null());
        self.x11_environment(&mut cmd);
        let child = cmd.spawn().map_err(|e| format!("{program}: {e}"))?;
        drop((wl_theirs, ipc_theirs));
        self.display_handle
            .insert_client(wl_ours, Arc::new(ClientState::new(Trust::Shell)))
            .map_err(|e| e.to_string())?;
        let connection = self.ipc_accept(ipc_ours, true);
        self.children.push(child);
        Ok(connection)
    }

    /// Points a program at this session's X server, or at none: a stale
    /// `DISPLAY` from the environment the session was started in must not
    /// reach applications.
    fn x11_environment(&self, command: &mut std::process::Command) {
        match self.x_display {
            Some(display) => command.env("DISPLAY", format!(":{display}")),
            None => command.env_remove("DISPLAY"),
        };
    }

    /// Starts an ordinary program on this compositor's socket.
    pub fn spawn(&mut self, command: &[String]) {
        let Some((program, args)) = command.split_first() else {
            return;
        };
        let mut cmd = std::process::Command::new(program);
        cmd.args(args)
            .env("WAYLAND_DISPLAY", &self.socket_name)
            .env("XDG_CURRENT_DESKTOP", "Boswas")
            .env_remove("WAYLAND_SOCKET")
            .env_remove("BOSWAS_COMPOSITOR_FD")
            .stdin(std::process::Stdio::null());
        self.x11_environment(&mut cmd);
        let result = cmd.spawn();
        match result {
            Ok(child) => self.children.push(child),
            Err(err) => warn!("could not start {program}: {err}"),
        }
    }

    /// Starts the shell, unless it has been restarting in a loop.
    pub fn start_shell(&mut self) {
        let command = self.config.shell.command.clone();
        if command.is_empty() {
            return;
        }
        let now = Instant::now();
        self.shell_started.retain(|t| now.duration_since(*t) < Duration::from_secs(60));
        if self.shell_started.len() >= 5 {
            warn!("the shell exited five times within a minute; not restarting it again");
            return;
        }
        self.shell_started.push(now);
        match self.spawn_trusted(&command) {
            Ok(connection) => {
                self.ipc.shell = connection;
                info!("started the shell: {}", command.join(" "));
            }
            Err(err) => warn!("could not start the shell: {err}"),
        }
    }

    /// Starts the configured input method, unless it has been restarting
    /// in a loop.
    pub fn start_input_method(&mut self) {
        let command = self.config.input_method.command.clone();
        if command.is_empty() {
            return;
        }
        let now = Instant::now();
        self.input_method.1.retain(|t| now.duration_since(*t) < Duration::from_secs(60));
        if self.input_method.1.len() >= 3 {
            return;
        }
        self.input_method.1.push(now);
        match self.spawn_trusted(&command) {
            Ok(connection) => {
                self.input_method.0 = connection;
                info!("started the input method: {}", command.join(" "));
            }
            Err(err) => warn!("could not start the input method: {err}"),
        }
    }

    /// Collects exited children; restarts the shell and the input method
    /// when they are gone.
    pub fn reap_children(&mut self) {
        self.children.retain_mut(|child| !matches!(child.try_wait(), Ok(Some(_)) | Err(_)));
        if !self.config.shell.command.is_empty() && !self.ipc.has_shell() {
            self.start_shell();
        }
        if !self.config.input_method.command.is_empty() && !self.input_method.0.is_some_and(|id| self.ipc.is_open(id)) {
            self.start_input_method();
        }
    }
}

impl Boswas {
    /// Re-reads the configuration files and applies what can change while
    /// the session runs. Returns the warnings of the reload.
    pub fn reload_config(&mut self) -> Vec<String> {
        let (config, warnings) = Config::load();
        self.keyboard.change_repeat_info(config.keyboard.repeat_rate, config.keyboard.repeat_delay);
        let xkb = XkbConfig {
            layout: &config.keyboard.layout,
            variant: &config.keyboard.variant,
            model: &config.keyboard.model,
            options: Some(config.keyboard.options.clone()).filter(|o| !o.is_empty()),
            ..XkbConfig::default()
        };
        let keyboard = self.keyboard.clone();
        if let Err(err) = keyboard.set_xkb_config(self, xkb) {
            warn!("keyboard layout \"{}\" rejected: {err:?}", config.keyboard.layout);
        }
        self.theme = Theme::of(if config.appearance.scheme == "light" {
            Scheme::Light
        } else {
            Scheme::Dark
        });
        self.cursor_theme = CursorTheme::load(&config.pointer.cursor_theme, config.pointer.cursor_size);
        if (config.appearance.scheme == "light") != self.app_icons.is_light() {
            self.app_icons = AppIcons::new(config.appearance.scheme == "light");
        }
        if config.night_light != self.config.night_light {
            self.night_light = (config.night_light.enabled, config.night_light.temperature);
        }
        // The shell command and the trust policy are fixed for the session.
        let shell = std::mem::take(&mut self.config.shell);
        let input_method = std::mem::take(&mut self.config.input_method);
        let policy = std::mem::take(&mut self.config.policy.privileged_protocols);
        self.config = config;
        self.config.shell = shell;
        self.config.input_method = input_method;
        self.config.policy.privileged_protocols = policy;
        let config = self.config.clone();
        self.backend.config_changed(&config);
        self.apply_night_light();
        let night_light = self.night_light_json();
        self.ipc_event("night-light", night_light);
        self.refresh_layouts();
        self.schedule_redraw();
        let appearance = self.appearance_json();
        self.ipc_event("config", appearance);
        warnings
    }
}

impl Boswas {
    /// Turns the screens back on after they were turned off for idleness.
    pub fn wake_outputs(&mut self) {
        if std::mem::take(&mut self.outputs_off) {
            self.backend.set_outputs_powered(true);
            self.schedule_redraw();
        }
    }

    /// Turns the screens off after the configured time without input,
    /// unless something on screen asks them to stay on (a video, a
    /// presentation). Called once a second.
    pub fn check_screen_off(&mut self) {
        let limit = self.config.power.screen_off_seconds;
        if limit == 0 || self.outputs_off || !self.idle_inhibitors.is_empty() {
            return;
        }
        if self.last_input.elapsed() >= Duration::from_secs(u64::from(limit)) {
            info!("idle for {limit} s; turning the screens off");
            self.outputs_off = true;
            self.backend.set_outputs_powered(false);
        }
    }

    /// Re-reads the layout names from the keymap (after it was set) and
    /// tells the shell when the list or the active layout changed.
    pub fn refresh_layouts(&mut self) {
        let keyboard = self.keyboard.clone();
        let layouts = keyboard.with_xkb_state(self, |context| {
            let xkb = context.xkb().lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            let names: Vec<String> = xkb.layouts().map(|layout| xkb.layout_name(layout).to_owned()).collect();
            (names, xkb.active_layout().0)
        });
        if layouts != self.layouts {
            self.layouts = layouts;
            let layout = self.layout_json();
            self.ipc_event("layout", layout);
        }
    }

    /// Whether the screens are tinted right now: night light is switched on
    /// and, with a schedule, the clock is inside it.
    pub fn night_light_active(&self) -> bool {
        use chrono::Timelike;
        let now = chrono::Local::now();
        let settings = crate::config::NightLight {
            enabled: self.night_light.0,
            temperature: self.night_light.1,
            ..self.config.night_light.clone()
        };
        settings.active_at(now.hour() * 60 + now.minute())
    }

    /// Switches night light on or off and sets its colour temperature.
    pub fn set_night_light(&mut self, enabled: bool, temperature: Option<u32>) {
        let temperature = temperature.unwrap_or(self.night_light.1).clamp(2500, 6500);
        self.night_light = (enabled, temperature);
        self.apply_night_light();
        let state = self.night_light_json();
        self.ipc_event("night-light", state);
    }

    /// Brings the outputs' colour in line with the night light state. Called
    /// on changes and once a second for the schedule.
    pub fn apply_night_light(&mut self) {
        let wanted = self.night_light_active().then_some(self.night_light.1);
        if wanted != self.night_light_applied {
            self.night_light_applied = wanted;
            self.backend.set_color_temperature(wanted);
            self.schedule_redraw();
        }
    }
}

/// The toplevel surface a (sub)surface belongs to.
pub fn root_surface(surface: &WlSurface) -> WlSurface {
    let mut root = surface.clone();
    while let Some(parent) = smithay::wayland::compositor::get_parent(&root) {
        root = parent;
    }
    root
}

/// `CLOCK_MONOTONIC`, the clock presentation feedback is reported in.
fn libc_clock_monotonic() -> u32 {
    1
}
