//! boswas-shell: the Boswas OS desktop shell.
//!
//! One unprivileged program with three roles:
//!
//! * (no option)  the desktop: wallpaper, taskbar, Start, quick settings,
//!   notifications, on-screen displays, dialogs and screen capture
//! * `--lock`     the lock screen (ext-session-lock, PAM), with the weather
//!   of the user's home place when the Weather application keeps a summary
//! * `--greeter`  the login screen (greetd)
//!
//! The compositor starts it and hands it a trusted Wayland connection; if
//! the shell crashes the compositor restarts it and open windows are
//! untouched. This file holds the surfaces and the Wayland plumbing;
//! `desk.rs` holds what the desktop does, `input.rs` how the pointer and
//! the keyboard reach it.

mod action;
mod agenda;
mod apps;
mod calendar;
mod capture;
mod clipboard;
mod clipview;
mod config;
mod control;
mod damage;
mod datacontrol;
mod desk;
mod desktop_files;
mod desktop_icons;
mod dialogs;
mod emoji;
mod glass;
mod glyph;
mod greetd;
mod input;
mod lock;
mod magnify;
mod menu;
mod notify;
mod open;
mod osd;
mod pam;
mod present;
mod prewarm;
mod quick;
mod settings;
mod solid;
mod start;
mod state;
mod stats;
mod status;
mod svc;
mod taskbar;
mod ui;
mod wallpaper;
mod tzif;
mod weather;
mod widget_sources;
mod widgets;
// The shell round of controls that did nothing and Windows shortcuts.
mod accounts;
mod barfocus;
mod gsettings;
mod projection;
mod quicklinks;
#[cfg(test)]
mod round_pictures;
mod security;

use std::{
    collections::HashMap,
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

use boswas_services::{Command, Event, Services};
use boswas_ui::{canvas::PixelRect, material::Backdrop, tokens::tokens, Canvas, Scheme, Theme};
use smithay_client_toolkit::{
    compositor::{CompositorHandler, CompositorState, Region},
    data_device_manager::{
        data_device::{DataDevice, DataDeviceHandler},
        data_offer::{DataOfferHandler, DragOffer},
        data_source::{CopyPasteSource, DataSourceHandler},
        DataDeviceManagerState, WritePipe,
    },
    delegate_compositor, delegate_data_device, delegate_keyboard, delegate_layer, delegate_output, delegate_pointer,
    delegate_registry, delegate_seat, delegate_session_lock, delegate_shm,
    output::{OutputHandler, OutputState},
    reexports::{
        calloop::{
            channel::{self, Channel, Sender},
            generic::Generic,
            timer::{TimeoutAction, Timer},
            EventLoop, Interest, LoopHandle, Mode as LoopMode, PostAction,
        },
        calloop_wayland_source::WaylandSource,
    },
    registry::{ProvidesRegistryState, RegistryState},
    registry_handlers,
    seat::{
        keyboard::{KeyEvent, KeyboardHandler, Keysym, Modifiers},
        pointer::{PointerEvent, PointerHandler},
        Capability, SeatHandler, SeatState,
    },
    session_lock::{
        SessionLock, SessionLockHandler, SessionLockState, SessionLockSurface, SessionLockSurfaceConfigure,
    },
    shell::{
        wlr_layer::{
            Anchor, KeyboardInteractivity, Layer, LayerShell, LayerShellHandler, LayerSurface, LayerSurfaceConfigure,
        },
        WaylandSurface,
    },
    shm::{Shm, ShmHandler},
};
use tracing::{error, info, warn};
use wayland_client::{
    delegate_noop,
    globals::registry_queue_init,
    protocol::{
        wl_data_device::WlDataDevice, wl_data_device_manager::DndAction, wl_data_source::WlDataSource, wl_keyboard,
        wl_output, wl_pointer, wl_seat, wl_surface,
    },
    Connection, Dispatch, QueueHandle,
};
use wayland_protocols::ext::background_effect::v1::client::{
    ext_background_effect_manager_v1::ExtBackgroundEffectManagerV1, ext_background_effect_surface_v1::ExtBackgroundEffectSurfaceV1,
};
use wayland_protocols::wp::{
    alpha_modifier::v1::client::{wp_alpha_modifier_surface_v1::WpAlphaModifierSurfaceV1, wp_alpha_modifier_v1::WpAlphaModifierV1},
    fractional_scale::v1::client::{
        wp_fractional_scale_manager_v1::WpFractionalScaleManagerV1,
        wp_fractional_scale_v1::{self, WpFractionalScaleV1},
    },
    viewporter::client::{wp_viewport::WpViewport, wp_viewporter::WpViewporter},
};

use crate::{
    action::Action,
    apps::Usage,
    config::{RunHistory, ShellConfig},
    control::Control,
    lock::AuthView,
    notify::Notes,
    quick::Quick,
    start::Start,
    state::{Mode, State},
    status::Status,
    svc::Sys,
    ui::{Anim, Frame, Hit, Rect, Resources},
    wallpaper::Background,
};

const USAGE: &str = "\
Usage: boswas-shell [--lock | --greeter]

The Boswas OS desktop shell: wallpaper, taskbar, Start, quick settings and
notifications.

Options:
  --lock         show the lock screen and unlock after the user's password
  --greeter      show the login screen (run by greetd)
  -h, --help     show this help
  -V, --version  show the version
";

/// The PAM service the lock screen authenticates against.
const PAM_SERVICE: &str = "boswas-lock";
/// What greetd starts after a successful login.
const SESSION_COMMAND: &str = "/usr/bin/boswas-session";
/// Room around a flyout's panel for its shadow; also its distance from the
/// screen edge and the taskbar.
pub(crate) const PAD: f32 = 12.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Role {
    Wallpaper,
    Taskbar,
    /// Invisible, behind a flyout or menu: a click on it closes them.
    Dismiss,
    Flyout,
    /// A context menu; the number is its depth in the menu stack.
    Menu(usize),
    Toasts,
    Osd,
    Tooltip,
    /// The window previews of a taskbar button.
    Preview,
    /// Dims the desktop behind a dialog and takes its clicks.
    Smoke,
    Dialog,
    Capture,
    /// Lock screen or greeter.
    Auth,
    /// The field over a desktop icon's label while it is renamed.
    Rename,
    /// The security options screen (Ctrl+Alt+Del), over everything.
    Security,
}

/// The Wayland object behind a shell surface.
pub(crate) enum Handle {
    Layer(LayerSurface),
    Lock(SessionLockSurface),
}

pub(crate) struct Surface {
    pub role: Role,
    pub handle: Handle,
    pub output: wl_output::WlOutput,
    /// Size in logical pixels, from the compositor's configure.
    pub width: u32,
    pub height: u32,
    /// The scale the surface is drawn at (fractional when the compositor
    /// offers fractional scaling).
    pub scale: f32,
    viewport: Option<WpViewport>,
    fractional: Option<WpFractionalScaleV1>,
    pub configured: bool,
    /// The surface is to be drawn (or presented) again; `change` says
    /// whether its content changed.
    pub dirty: bool,
    /// What changed in the content since it was last drawn.
    pub change: Change,
    frame_pending: bool,
    pub pointer: Option<(f32, f32)>,
    /// The primary button is held on this surface.
    pub pressed: bool,
    pub hits: Vec<Hit>,
    /// The hit the pointer is on.
    pub hover: Option<usize>,
    /// Where the surface's top-left corner is on its output.
    pub origin: (f32, f32),
    /// Opening or closing transition.
    pub anim: Option<Anim>,
    /// The drawing last sent, as drawn, and a spare canvas of the same
    /// size to draw the next one on (see `present`).
    front: Option<Canvas>,
    spare: Option<Canvas>,
    /// The shared-memory buffers the drawing is handed over in.
    buffers: present::Buffers,
    /// The buffer attached last shows a frame of a transition composed
    /// here, not the drawing.
    composed: bool,
    /// The surface is declared opaque to the compositor.
    opaque: bool,
    /// Where the surface is anchored and its margins at rest (layer
    /// surfaces), for sliding it into place.
    anchor: Anchor,
    margin: (i32, i32, i32, i32),
    /// The compositor's control of the surface's opacity, for transitions.
    fade: Option<WpAlphaModifierSurfaceV1>,
    /// The opacity or the place last sent are not those at rest.
    faded: bool,
    /// When the surface was first marked as changed since it was last
    /// drawn (kept only for the statistics, `stats`).
    changed_at: Option<Instant>,
    /// The compositor's blur behind the surface's glass (glass.rs), and
    /// the region last sent.
    pub blur: Option<ExtBackgroundEffectSurfaceV1>,
    pub blur_region: Vec<(i32, i32, i32, i32)>,
}

impl Drop for Surface {
    fn drop(&mut self) {
        // The next surface of the same size (the next time a flyout
        // opens) takes over the memory.
        for canvas in [self.front.take(), self.spare.take()].into_iter().flatten() {
            present::recycle_canvas(canvas);
        }
        present::recycle_buffers(std::mem::take(&mut self.buffers));
        if let Some(effect) = self.blur.take() {
            effect.destroy();
        }
    }
}

/// What changed in a surface's content since it was last drawn.
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum Change {
    None,
    /// Only what lies in these rectangles (logical pixels).
    Parts(Vec<Rect>),
    All,
}

/// Separate rectangles a change is kept as at most; more are joined.
const MAX_PARTS: usize = 4;

impl Change {
    /// The change with `rect` added: joined with a rectangle it overlaps.
    fn with(self, rect: Rect) -> Change {
        let mut rects = match self {
            Change::All => return Change::All,
            Change::None => Vec::new(),
            Change::Parts(rects) => rects,
        };
        let mut rect = rect;
        while let Some(index) = rects.iter().position(|r| ui::overlap(r, &rect)) {
            rect = ui::bounds(&rects.swap_remove(index), &rect);
        }
        rects.push(rect);
        if rects.len() > MAX_PARTS {
            let all = rects.iter().fold(rects[0], |a, r| ui::bounds(&a, r));
            rects = vec![all];
        }
        Change::Parts(rects)
    }
}

impl Surface {
    pub fn wl_surface(&self) -> &wl_surface::WlSurface {
        match &self.handle {
            Handle::Layer(layer) => layer.wl_surface(),
            Handle::Lock(lock) => lock.wl_surface(),
        }
    }

    /// Marks the content as changed.
    pub fn touch(&mut self) {
        self.dirty = true;
        self.change = Change::All;
        stats::changed(&mut self.changed_at);
    }

    /// Marks the content as changed within `rect` (logical pixels) only.
    pub fn touch_rect(&mut self, rect: Rect) {
        self.dirty = true;
        self.change = std::mem::replace(&mut self.change, Change::None).with(rect);
        stats::changed(&mut self.changed_at);
    }

    /// The pointer moved from `from` to `to` (`None`: off the surface):
    /// what is drawn differently is the controls it entered or left (their
    /// hover look). A control covering the whole surface (the desktop, the
    /// invisible surface behind a menu) has no hover look.
    pub fn pointer_moved(&mut self, from: Option<(f32, f32)>, to: Option<(f32, f32)>) {
        for rect in ui::hover_changes(&self.hits, (self.width as f32, self.height as f32), from, to) {
            self.touch_rect(rect);
        }
    }

    /// The primary button went down or up: the controls under the pointer
    /// change their pressed look.
    pub fn press_changed(&mut self) {
        for rect in ui::press_changes(&self.hits, (self.width as f32, self.height as f32), self.pointer) {
            self.touch_rect(rect);
        }
    }
}

/// How a layer surface is created.
pub(crate) struct Spec {
    pub layer: Layer,
    pub namespace: &'static str,
    pub anchor: Anchor,
    /// 0 lets the compositor size that axis (with opposite anchors).
    pub size: (u32, u32),
    /// Top, right, bottom, left.
    pub margin: (i32, i32, i32, i32),
    pub exclusive: i32,
    pub keyboard: KeyboardInteractivity,
    /// The surface takes pointer input.
    pub input: bool,
    pub origin: (f32, f32),
    pub animate: bool,
}

/// The result of a password check done on a worker thread.
pub(crate) enum AuthResult {
    Unlocked,
    LoggedIn,
    Failed(String),
}

/// A request to the compositor whose reply the shell waits for.
pub(crate) enum Pending {
    Thumbnail { window: u64, path: PathBuf },
    Screenshot { path: PathBuf, whole: bool },
}

pub(crate) struct Shell {
    registry_state: RegistryState,
    seat_state: SeatState,
    output_state: OutputState,
    compositor_state: CompositorState,
    shm: Shm,
    layer_shell: Option<LayerShell>,
    session_lock_state: SessionLockState,
    session_lock: Option<SessionLock>,
    data_devices: Option<DataDeviceManagerState>,
    data_device: Option<DataDevice>,
    /// What the shell put on the clipboard (a captured picture).
    clipboard: Option<(CopyPasteSource, Arc<Vec<u8>>)>,
    /// Clipboard control: the clipboard history and the emoji panel's
    /// insertion (desktop only; datacontrol.rs).
    pub clip: Option<datacontrol::Watch>,
    viewporter: Option<WpViewporter>,
    fractional_scale: Option<WpFractionalScaleManagerV1>,
    /// The compositor fades surfaces for the shell (transitions).
    alpha_modifier: Option<WpAlphaModifierV1>,
    /// The compositor blurs behind the shell's glass (glass.rs), and
    /// whether it offers to now.
    background_effect: Option<ExtBackgroundEffectManagerV1>,
    pub blur_offered: bool,
    conn: Connection,
    qh: QueueHandle<Shell>,
    loop_handle: LoopHandle<'static, Shell>,
    auth_sender: Sender<AuthResult>,

    pub res: Resources,
    pub background: Background,
    pub surfaces: Vec<Surface>,
    /// Logical size of each output, learned from its full-screen surface.
    output_sizes: Vec<(wl_output::WlOutput, (f32, f32))>,
    pub control: Option<Control>,
    pub services: Option<Services>,
    #[cfg(debug_assertions)]
    pub fake: Option<svc::fake::Fake>,
    /// Lines of the development feed already read.
    #[cfg(debug_assertions)]
    pub feed_lines: usize,
    keyboard: Option<wl_keyboard::WlKeyboard>,
    pointer: Option<wl_pointer::WlPointer>,
    pub modifiers: Modifiers,
    /// Serial of the latest input event (for the clipboard).
    pub serial: u32,

    pub st: State,
    pub pending: HashMap<u64, Pending>,
    /// The compositor answered a thumbnail request with an error once.
    pub thumbnails_unsupported: bool,
    /// The flyout is playing its closing transition.
    pub flyout_closing: bool,
    /// The control the primary button went down on.
    pub press: Option<(usize, Action)>,
    /// A slider value waiting for its turn to be sent (commands are rate
    /// limited while dragging).
    pub slider_pending: Option<Command>,
    pub slider_sent: Instant,
    /// The tray item whose menu was asked for, and where it will open.
    pub tray_menu: Option<(String, f32, f32, wl_output::WlOutput)>,
    /// Files behind the shell's own notifications (captured pictures).
    pub shell_notes: HashMap<u32, PathBuf>,
    pub next_note_id: u32,
    /// The taskbars were made to hide themselves (Personalization >
    /// Taskbar > Automatically hide the taskbar).
    pub taskbar_auto_hide: bool,
    /// The auto-hiding taskbar is out of sight but for a sliver at the
    /// bottom edge.
    pub taskbar_hidden: bool,
    /// When the auto-hiding taskbar goes, unless something keeps it.
    pub taskbar_hide_at: Option<Instant>,
    pub exit: Option<i32>,
    /// Icons, text and panels made ready before they are first shown.
    pub prewarm: prewarm::Prewarm,
    /// Single-pixel buffers for the surfaces of one colour.
    solid: solid::Solid,
}

/// The C library's memory settings the compositor starts the shell with
/// (boswas-compositor's `SHELL_MALLOC_TUNABLES`): the shell's own, taken out
/// of the environment programs it starts inherit.
const MALLOC_TUNABLES: &str = "glibc.malloc.mmap_threshold=262144";

/// Whether the environment's `GLIBC_TUNABLES` is the one the compositor
/// gave the shell (and not one of the session's own).
fn ours(tunables: Option<&std::ffi::OsStr>) -> bool {
    tunables.is_some_and(|value| value == MALLOC_TUNABLES)
}

fn main() {
    // Read by the C library when the program started; programs the shell
    // starts get the session's environment without it.
    if ours(std::env::var_os("GLIBC_TUNABLES").as_deref()) {
        std::env::remove_var("GLIBC_TUNABLES");
    }
    let mut mode = Mode::Desktop;
    for arg in std::env::args().skip(1) {
        match arg.as_str() {
            "--lock" => mode = Mode::Lock,
            "--greeter" => mode = Mode::Greeter,
            "-h" | "--help" => {
                print!("{USAGE}");
                return;
            }
            "-V" | "--version" => {
                println!("boswas-shell {}", env!("CARGO_PKG_VERSION"));
                return;
            }
            other => {
                eprintln!("boswas-shell: unknown option: {other}\n\n{USAGE}");
                std::process::exit(2);
            }
        }
    }
    let filter = tracing_subscriber::EnvFilter::try_from_env("BOSWAS_LOG")
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    tracing_subscriber::fmt().with_env_filter(filter).with_writer(std::io::stderr).init();

    match run(mode) {
        Ok(code) => std::process::exit(code),
        Err(err) => {
            error!("{err}");
            std::process::exit(1);
        }
    }
}

/// What to tell the user when no usable font is installed (`None` when
/// there is one).
fn missing_fonts(mode: Mode, has_fonts: bool) -> Option<String> {
    if has_fonts {
        return None;
    }
    let consequence = match mode {
        Mode::Desktop => "the desktop cannot show any text and does not start",
        Mode::Greeter => "the login screen cannot show any text and does not start",
        Mode::Lock => "the lock screen shows no text (type the password and press Enter to unlock)",
    };
    Some(format!(
        "no usable font found in the fontconfig directories, /usr/share/fonts, /usr/local/share/fonts \
         or the user's font directories: {consequence}; install a font package such as fonts-inter"
    ))
}

fn run(mode: Mode) -> Result<i32, Box<dyn std::error::Error>> {
    // The control descriptor is taken (and made close-on-exec) before
    // anything else, so no child process can inherit it.
    let control = Control::from_env();
    let conn = Connection::connect_to_env()?;
    std::env::remove_var("WAYLAND_SOCKET");
    let (globals, event_queue) = registry_queue_init::<Shell>(&conn)?;
    let qh = event_queue.handle();
    let mut event_loop: EventLoop<'static, Shell> = EventLoop::try_new()?;
    let (auth_sender, auth_channel): (Sender<AuthResult>, Channel<AuthResult>) = channel::channel();
    let (service_sender, service_channel): (Sender<Event>, Channel<Event>) = channel::channel();

    let scheme = match std::env::var("BOSWAS_SCHEME").as_deref() {
        Ok("light") => Scheme::Light,
        _ => Scheme::Dark,
    };
    // Fonts are read once, here. A desktop or a login screen without a
    // single letter is of no use, and whoever started it must learn that
    // it failed.
    let res = Resources::new(Theme::of(scheme));
    if let Some(problem) = missing_fonts(mode, res.text.has_fonts()) {
        if mode != Mode::Lock {
            return Err(problem.into());
        }
        // The lock screen stays: without it the session could not be
        // unlocked at all.
        error!("{problem}");
    }
    let (user, user_name) = pam::current_user();
    let locale = std::env::var("LC_TIME")
        .or_else(|_| std::env::var("LC_MESSAGES"))
        .or_else(|_| std::env::var("LANG"))
        .unwrap_or_default();
    let apps = if mode == Mode::Desktop {
        apps::load_apps(&apps::application_dirs(), &locale)
    } else {
        Vec::new()
    };

    // Only the lock screen shows the weather: the login screen runs before
    // anyone's files are known. It is read now, for the first frame.
    let mut weather = weather::Watch::new(if mode == Mode::Lock { weather::summary_path() } else { None });
    weather.poll(chrono::Utc::now().timestamp());

    let mut auth = AuthView {
        display_name: user_name.clone(),
        ..AuthView::default()
    };
    match mode {
        Mode::Lock => auth.hint = "Enter your password to unlock".into(),
        Mode::Greeter => {
            auth.greeter = true;
            auth.hint = "Sign in to Boswas OS".into();
            auth.users = std::fs::read_to_string("/etc/passwd").map(|text| lock::login_users(&text)).unwrap_or_default();
            if auth.users.is_empty() {
                auth.other_user();
            } else {
                auth.select(0);
            }
        }
        Mode::Desktop => {}
    }
    // The account pictures: the user's own (desktop and lock screen), or
    // those of the people who can sign in (login screen; the service's
    // copies only).
    let mut pictures = accounts::Pictures::default();
    match mode {
        Mode::Greeter => {
            for (login, _) in &auth.users {
                pictures.refresh(login, None);
            }
        }
        _ => {
            pictures.refresh(&user, accounts::own_home().as_deref());
        }
    }

    let st = State {
        mode,
        apps,
        usage: Usage::load(),
        config: if mode == Mode::Desktop { ShellConfig::load() } else { ShellConfig::default() },
        run_history: if mode == Mode::Desktop { RunHistory::load() } else { RunHistory::default() },
        windows: Vec::new(),
        workspace: 0,
        status: Status::read(false),
        sys: Sys::default(),
        locked: false,
        user,
        user_name,
        locale,
        flyout: None,
        start: Start::default(),
        board: if mode == Mode::Desktop { widgets::Board::new() } else { widgets::Board::default() },
        desk: desktop_icons::Desk::default(),
        quick: Quick::default(),
        notes: Notes::default(),
        menus: Vec::new(),
        dialog: None,
        osd: None,
        tooltip: None,
        preview: None,
        capture: None,
        focus: None,
        night_light: false,
        capturing: Vec::new(),
        layouts: Vec::new(),
        layout: 0,
        thumbnails: HashMap::new(),
        auth,
        desktop: settings::Desktop::default(),
        desktop_known: false,
        dnd_schedule: None,
        senders: if mode == Mode::Desktop { config::Senders::load() } else { config::Senders::default() },
        seen: if mode == Mode::Desktop { config::SeenApps::load() } else { config::SeenApps::default() },
        weather,
        clips: if mode == Mode::Desktop { clipboard::History::load() } else { clipboard::History::default() },
        clipview: clipview::View::default(),
        emoji: if mode == Mode::Desktop { emoji::Panel::new() } else { emoji::Panel::default() },
        night_light_available: true,
        pictures,
        security: None,
        projection: projection::Projection::default(),
        bar_focus: false,
        gsettings: gsettings::Mirror::default(),
    };

    let mut shell = Shell {
        registry_state: RegistryState::new(&globals),
        seat_state: SeatState::new(&globals, &qh),
        output_state: OutputState::new(&globals, &qh),
        compositor_state: CompositorState::bind(&globals, &qh)?,
        shm: Shm::bind(&globals, &qh)?,
        layer_shell: LayerShell::bind(&globals, &qh).ok(),
        session_lock_state: SessionLockState::new(&globals, &qh),
        session_lock: None,
        data_devices: DataDeviceManagerState::bind(&globals, &qh).ok(),
        data_device: None,
        clipboard: None,
        clip: None,
        viewporter: globals.bind(&qh, 1..=1, ()).ok(),
        fractional_scale: globals.bind(&qh, 1..=1, ()).ok(),
        alpha_modifier: globals.bind(&qh, 1..=1, ()).ok(),
        background_effect: globals.bind(&qh, 1..=1, ()).ok(),
        blur_offered: false,
        conn: conn.clone(),
        qh: qh.clone(),
        loop_handle: event_loop.handle(),
        auth_sender,
        res,
        background: Background::default(),
        surfaces: Vec::new(),
        output_sizes: Vec::new(),
        control,
        services: None,
        #[cfg(debug_assertions)]
        fake: None,
        #[cfg(debug_assertions)]
        feed_lines: 0,
        keyboard: None,
        pointer: None,
        modifiers: Modifiers::default(),
        serial: 0,
        st,
        pending: HashMap::new(),
        thumbnails_unsupported: false,
        flyout_closing: false,
        press: None,
        slider_pending: None,
        slider_sent: Instant::now(),
        tray_menu: None,
        shell_notes: HashMap::new(),
        next_note_id: u32::MAX,
        taskbar_auto_hide: false,
        taskbar_hidden: false,
        taskbar_hide_at: None,
        exit: None,
        prewarm: prewarm::Prewarm::default(),
        solid: solid::Solid::bind(&globals, &qh),
    };

    // The desktop shares the background it draws; the lock screen starts
    // from it (wallpaper.rs).
    if let Some(file) = wallpaper::shared_file() {
        match mode {
            Mode::Desktop => shell.background.share_in(file),
            Mode::Lock => shell.background.adopt_shared(&file),
            Mode::Greeter => {}
        }
    }

    match mode {
        Mode::Lock => {
            // The session is already locked by the compositor; this asks to
            // be the client that shows the lock surfaces and may unlock.
            shell.session_lock = Some(
                shell
                    .session_lock_state
                    .lock(&qh)
                    .map_err(|_| "the compositor does not offer the session lock to this program")?,
            );
        }
        Mode::Desktop | Mode::Greeter => {
            if shell.layer_shell.is_none() {
                return Err("the compositor does not offer the layer shell to this program".into());
            }
        }
    }

    // The clipboard history and the emoji panel: the desktop only.
    if mode == Mode::Desktop {
        shell.clip = datacontrol::Watch::bind(&globals, &qh, &event_loop.handle());
    }

    WaylandSource::new(conn, event_queue).insert(event_loop.handle())?;

    // Compositor events (window list, shortcuts, appearance).
    if let Some(control) = &mut shell.control {
        control.subscribe();
        let watched = control.watch()?;
        event_loop
            .handle()
            .insert_source(Generic::new(watched, Interest::READ, LoopMode::Level), |_, _, shell| {
                let events = match shell.control.as_mut().map(Control::read_events) {
                    Some(Ok(events)) => events,
                    _ => {
                        // The compositor is gone; so is the session.
                        shell.exit = Some(0);
                        return Ok(PostAction::Remove);
                    }
                };
                for event in events {
                    shell.control_event(event);
                }
                Ok(PostAction::Continue)
            })?;
    }

    // Results of password checks.
    event_loop.handle().insert_source(auth_channel, |event, _, shell| {
        if let channel::Event::Msg(result) = event {
            shell.auth_finished(result);
        }
    })?;

    // The system services. The lock screen and the greeter only read
    // status; they neither show notifications nor answer polkit.
    event_loop.handle().insert_source(service_channel, |event, _, shell| {
        if let channel::Event::Msg(event) = event {
            shell.service_event(event);
        }
    })?;
    #[cfg(debug_assertions)]
    if svc::fake::enabled() {
        let fake = svc::fake::Fake::default();
        for event in fake.initial() {
            let _ = service_sender.send(event);
        }
        shell.fake = Some(fake);
        warn!("BOSWAS_SHELL_FAKE_SERVICES is set: the system services are simulated");
    }
    let simulated = {
        #[cfg(debug_assertions)]
        {
            shell.fake.is_some()
        }
        #[cfg(not(debug_assertions))]
        {
            false
        }
    };
    if !simulated {
        // The desktop runs everything; the lock screen only shows status
        // and changes the volume and brightness; the login screen may also
        // suspend, restart and shut down (its power menu).
        let config = match mode {
            Mode::Desktop => boswas_services::Config::desktop(),
            Mode::Greeter => boswas_services::Config::greeter(),
            _ => boswas_services::Config::status_only(),
        };
        shell.services = Some(Services::start(config, move |event| {
            let _ = service_sender.send(event);
        }));
    }

    // The clock: checked every second, redrawn when the minute changes.
    event_loop
        .handle()
        .insert_source(Timer::from_duration(Duration::from_secs(1)), |_, _, shell| {
            shell.tick();
            TimeoutAction::ToDuration(Duration::from_secs(1))
        })?;

    info!("boswas-shell {} started ({mode:?})", env!("CARGO_PKG_VERSION"));
    while shell.exit.is_none() {
        let now = Instant::now();
        let timeout = shell.next_deadline(now).map(|due| due.saturating_duration_since(now));
        event_loop.dispatch(timeout, &mut shell)?;
        shell.housekeeping(Instant::now());
        shell.redraw_dirty();
        shell.prewarm_tick();
    }
    Ok(shell.exit.unwrap_or(0))
}

impl Shell {
    // ---------------------------------------------------------------------
    // Surfaces
    // ---------------------------------------------------------------------

    /// Logical size of an output (a common size until it is known).
    pub fn output_size(&self, output: &wl_output::WlOutput) -> (f32, f32) {
        self.output_sizes.iter().find(|(o, _)| o == output).map_or((1280.0, 720.0), |(_, size)| *size)
    }

    /// The output things appear on when no surface says otherwise.
    pub fn primary_output(&self) -> Option<wl_output::WlOutput> {
        self.surfaces
            .iter()
            .find(|s| matches!(s.role, Role::Taskbar | Role::Auth))
            .map(|s| s.output.clone())
            .or_else(|| self.output_state.outputs().next())
    }

    fn new_surface(&mut self, role: Role, handle: Handle, output: &wl_output::WlOutput, origin: (f32, f32), animate: bool) {
        let wl = match &handle {
            Handle::Layer(layer) => layer.wl_surface().clone(),
            Handle::Lock(lock) => lock.wl_surface().clone(),
        };
        let viewport = self.viewporter.as_ref().map(|v| v.get_viewport(&wl, &self.qh, ()));
        // Fractional scaling needs the viewport to present the buffer at
        // its logical size.
        let fractional = match (&self.fractional_scale, &viewport) {
            (Some(manager), Some(_)) => Some(manager.get_fractional_scale(&wl, &self.qh, wl.clone())),
            _ => None,
        };
        // Surfaces that open and close with a transition let the compositor
        // fade them.
        let fade = self.alpha_modifier.as_ref().filter(|_| animate).map(|m| m.get_surface(&wl, &self.qh, ()));
        let blur = self.background_effect.as_ref().filter(|_| glass::may_blur(role)).map(|m| m.get_background_effect(&wl, &self.qh, ()));
        self.surfaces.push(Surface {
            role,
            handle,
            output: output.clone(),
            width: 0,
            height: 0,
            scale: 1.0,
            viewport,
            fractional,
            configured: false,
            dirty: true,
            change: Change::All,
            frame_pending: false,
            pointer: None,
            pressed: false,
            hits: Vec::new(),
            hover: None,
            origin,
            anim: animate.then(|| Anim::opening(Instant::now(), self.res.reduced_motion)),
            front: None,
            spare: None,
            buffers: present::Buffers::default(),
            composed: false,
            opaque: false,
            anchor: Anchor::empty(),
            margin: (0, 0, 0, 0),
            fade,
            faded: false,
            changed_at: None,
            blur,
            blur_region: Vec::new(),
        });
    }

    pub fn create_layer(&mut self, role: Role, output: &wl_output::WlOutput, spec: Spec) {
        let Some(layer_shell) = &self.layer_shell else {
            return;
        };
        let surface = self.compositor_state.create_surface(&self.qh);
        if !spec.input {
            // An empty input region: the pointer passes through.
            if let Ok(region) = Region::new(&self.compositor_state) {
                surface.set_input_region(Some(region.wl_region()));
            }
        }
        let layer = layer_shell.create_layer_surface(&self.qh, surface, spec.layer, Some(spec.namespace), Some(output));
        layer.set_anchor(spec.anchor);
        layer.set_size(spec.size.0, spec.size.1);
        layer.set_margin(spec.margin.0, spec.margin.1, spec.margin.2, spec.margin.3);
        layer.set_exclusive_zone(spec.exclusive);
        layer.set_keyboard_interactivity(spec.keyboard);
        layer.commit();
        self.new_surface(role, Handle::Layer(layer), output, spec.origin, spec.animate);
        if let Some(surface) = self.surfaces.last_mut() {
            surface.anchor = spec.anchor;
            surface.margin = spec.margin;
        }
    }

    /// Moves or resizes an existing layer surface.
    pub fn reshape(&mut self, role: Role, size: (u32, u32), margin: (i32, i32, i32, i32), origin: (f32, f32)) {
        for surface in self.surfaces.iter_mut().filter(|s| s.role == role) {
            if let Handle::Layer(layer) = &surface.handle {
                if (surface.width, surface.height) != size || surface.origin != origin {
                    layer.set_size(size.0, size.1);
                    layer.set_margin(margin.0, margin.1, margin.2, margin.3);
                    layer.commit();
                    surface.origin = origin;
                    surface.margin = margin;
                }
            }
            surface.touch();
        }
    }

    pub fn has(&self, role: Role) -> bool {
        self.surfaces.iter().any(|s| s.role == role)
    }

    pub fn remove(&mut self, role: Role) {
        self.surfaces.retain(|s| s.role != role);
    }

    /// Starts the closing transition of a role's surfaces; they are removed
    /// when it ends.
    pub fn fade_out(&mut self, role: Role) {
        let now = Instant::now();
        let reduced = self.res.reduced_motion;
        for surface in self.surfaces.iter_mut().filter(|s| s.role == role) {
            surface.anim = Some(Anim::closing(now, reduced));
            surface.dirty = true;
        }
    }

    fn output_added(&mut self, output: &wl_output::WlOutput) {
        match self.st.mode {
            Mode::Desktop => {
                // The wallpaper takes the keyboard on a click while it shows desktop icons.
                let keyboard = self.desk_keyboard();
                self.create_layer(Role::Wallpaper, output, Self::full_screen(Layer::Background, "boswas-wallpaper", keyboard, -1));
                self.create_taskbar(output);
            }
            Mode::Greeter => {
                self.create_layer(Role::Auth, output, Self::full_screen(Layer::Overlay, "boswas-greeter", KeyboardInteractivity::Exclusive, -1));
            }
            Mode::Lock => {
                if let Some(lock) = self.session_lock.as_ref().filter(|lock| lock.is_locked()) {
                    let surface = self.compositor_state.create_surface(&self.qh);
                    let lock_surface = lock.create_lock_surface(surface, output, &self.qh);
                    self.new_surface(Role::Auth, Handle::Lock(lock_surface), output, (0.0, 0.0), false);
                }
            }
        }
    }

    /// The taskbar of an output. An auto-hiding one reserves no room, so
    /// windows use the whole screen, and starts out of sight.
    pub fn create_taskbar(&mut self, output: &wl_output::WlOutput) {
        let height = tokens().shell("taskbar-height") as u32;
        let auto_hide = self.st.desktop.taskbar.auto_hide;
        self.taskbar_auto_hide = auto_hide;
        self.taskbar_hidden = auto_hide;
        self.create_layer(
            Role::Taskbar,
            output,
            Spec {
                layer: Layer::Top,
                namespace: "boswas-taskbar",
                anchor: Anchor::BOTTOM | Anchor::LEFT | Anchor::RIGHT,
                size: (0, height),
                margin: (0, 0, if auto_hide { -desk::taskbar_hidden_offset() } else { 0 }, 0),
                exclusive: if auto_hide { 0 } else { height as i32 },
                keyboard: KeyboardInteractivity::None,
                input: true,
                origin: (0.0, 0.0),
                animate: false,
            },
        );
    }

    pub fn mark(&mut self, role: Role) {
        for surface in self.surfaces.iter_mut().filter(|s| s.role == role) {
            surface.touch();
        }
    }

    pub fn mark_all(&mut self) {
        for surface in &mut self.surfaces {
            surface.touch();
        }
    }

    fn redraw_dirty(&mut self) {
        for index in 0..self.surfaces.len() {
            if self.surfaces[index].dirty && !self.surfaces[index].frame_pending {
                self.draw(index);
            }
        }
    }

    /// Makes the blurred wallpaper for an output available to the drawing
    /// code.
    fn prepare_backdrop(&mut self, size: (f32, f32)) {
        self.background.settle();
        let key = (size.0 as u32, size.1 as u32);
        if self.res.backdrop.is_some() && self.res.backdrop_key == key {
            return;
        }
        self.res.backdrop = self.background.reduced(self.res.theme, size.0, size.1).and_then(|reduced| Backdrop::new(&reduced, size.0, size.1));
        self.res.backdrop_key = key;
        self.res.backdrop_generation += 1;
    }

    fn draw(&mut self, index: usize) {
        let Some(surface) = self.surfaces.get(index) else {
            return;
        };
        if !surface.configured || surface.width == 0 || surface.height == 0 {
            return;
        }
        let started = Instant::now();
        let role = surface.role;
        let size = self.output_size(&surface.output);
        if !matches!(role, Role::Wallpaper | Role::Dismiss | Role::Smoke | Role::Capture) {
            self.prepare_backdrop(size);
        }
        let now = Instant::now();
        let first_auth = self.surfaces.iter().position(|s| s.role == Role::Auth) == Some(index);
        // The desktop icons are on the primary output's wallpaper.
        let icons_here = role == Role::Wallpaper && self.primary_output().as_ref() == Some(&self.surfaces[index].output);
        let Shell {
            surfaces,
            res,
            background,
            shm,
            qh,
            st,
            compositor_state,
            solid,
            ..
        } = self;
        let surface = &mut surfaces[index];
        let (w, h) = (surface.width as f32, surface.height as f32);
        let wl = surface.wl_surface().clone();

        // Invisible and dimming surfaces are one pixel, stretched.
        let stretched = matches!(role, Role::Dismiss | Role::Smoke) && surface.viewport.is_some();
        let (cw, ch, scale) = if stretched { (1.0, 1.0, 1.0) } else { (w, h, surface.scale) };
        let transition = surface.anim.as_ref().and_then(|anim| anim.frame(now));
        let presence = transition.unwrap_or(1.0);
        let animating = transition.is_some();
        // Start's page scrolling smoothly: drawn again at the display's pace
        // until it stands.
        let scrolling = role == Role::Flyout && st.flyout == Some(state::Flyout::Start) && st.start.animating(now);
        // While it opens or closes, a layer surface anchored to its top or
        // bottom edge is faded and slid by the compositor; others are
        // composed here, frame by frame.
        let slide = present::slide_direction(surface.anchor).filter(|_| surface.fade.is_some() && matches!(surface.handle, Handle::Layer(_)));
        let compose = animating && slide.is_none();

        // The content, drawn again where it changed: a part (a hover
        // change) on the spare canvas and copied over, or all of it on the
        // spare canvas, which then becomes the drawing.
        let fits = |c: &Canvas| c.width() == cw && c.height() == ch && c.scale() == scale;
        let front_fits = surface.front.as_ref().is_some_and(fits);
        let change = std::mem::replace(&mut surface.change, Change::None);
        // While Start's page scrolls, only its window changes.
        let page_window = start::page_window(Rect::new(PAD, PAD, w - 2.0 * PAD, h - 2.0 * PAD), st);
        let change = match change {
            _ if !front_fits => Change::All,
            change if scrolling => change.with(page_window),
            change => change,
        };
        // The capture view knows itself where it changed (a selection
        // being dragged): the picture under it is not drawn again.
        let change = match (change, st.capture.as_ref(), surface.front.as_ref()) {
            (Change::All, Some(capture), Some(front)) if role == Role::Capture && front_fits => {
                match capture.changes(&mut res.text, (w, h), scale, front.pixel_size()) {
                    Some(rects) if rects.is_empty() => Change::None,
                    Some(rects) => Change::Parts(rects),
                    None => Change::All,
                }
            }
            (change, _, _) => change,
        };
        let mut damage: Vec<PixelRect> = Vec::new();
        let mut glass_region: Option<Vec<(i32, i32, i32, i32)>> = None;
        if change != Change::None {
            let Some(mut canvas) = surface.spare.take().filter(fits).or_else(|| present::reuse_canvas(cw, ch, scale)).or_else(|| Canvas::new(cw, ch, scale)) else {
                return;
            };
            let parts = match &change {
                Change::Parts(rects) if front_fits && !matches!(role, Role::Dismiss | Role::Smoke) => {
                    let rects: Vec<PixelRect> = rects.iter().map(|r| ui::device_rect(r, scale)).collect();
                    canvas.begin_clip(&rects)
                }
                _ => Vec::new(),
            };
            if parts.is_empty() {
                canvas.reset();
            }
            let mut hits = Vec::new();
            res.glass_regions.clear();
            res.magnifying.clear();
            let panel = Rect::new(PAD, PAD, w - 2.0 * PAD, h - 2.0 * PAD);
            {
                let mut frame = Frame {
                    canvas: &mut canvas,
                    res,
                    pointer: surface.pointer,
                    pressed: surface.pressed,
                    hits: &mut hits,
                    focus: st.focus.as_ref(),
                    origin: surface.origin,
                };
                match role {
                    Role::Wallpaper => {
                        background.paint(frame.canvas, frame.res.theme);
                        frame.area(Rect::new(0.0, 0.0, w, h), Action::Desktop);
                        if icons_here {
                            desktop_icons::draw(&mut frame, st, (w, h));
                        }
                    }
                    Role::Taskbar => taskbar::draw(&mut frame, st),
                    Role::Dismiss => {
                        frame.area(Rect::new(0.0, 0.0, cw, ch), Action::Dismiss);
                    }
                    Role::Smoke => {
                        let smoke = tokens().material(frame.res.theme.scheme, "smoke");
                        frame.canvas.clear(smoke.tint.alpha(smoke.tint_opacity));
                        frame.area(Rect::new(0.0, 0.0, cw, ch), Action::None);
                    }
                    Role::Flyout => match st.flyout {
                        Some(state::Flyout::Start) => start::draw(&mut frame, st, panel),
                        Some(state::Flyout::Quick) => quick::draw(&mut frame, st, panel),
                        Some(state::Flyout::Notifications) => notify::draw_centre(&mut frame, st, panel),
                        Some(state::Flyout::TrayOverflow) => taskbar::draw_overflow(&mut frame, st, panel),
                        Some(state::Flyout::Clipboard) => clipview::draw(&mut frame, st, panel),
                        Some(state::Flyout::Emoji) => emoji::draw(&mut frame, st, panel),
                        Some(state::Flyout::Widgets) => widgets::draw(&mut frame, st, panel),
                        Some(state::Flyout::Projection) => projection::draw(&mut frame, st, panel),
                        None => {}
                    },
                    Role::Menu(level) => {
                        if let Some(menu) = st.menus.get(level) {
                            menu.draw(&mut frame, (PAD, PAD), level);
                        }
                    }
                    Role::Toasts => notify::draw_toasts(&mut frame, st, now),
                    Role::Osd => {
                        if let Some(osd) = &st.osd {
                            osd::draw(&mut frame, osd, panel);
                        }
                    }
                    Role::Tooltip => {
                        if let Some(tooltip) = &st.tooltip {
                            desk::draw_tooltip(&mut frame, &tooltip.text, panel);
                        }
                    }
                    Role::Preview => {
                        if let Some(preview) = &st.preview {
                            taskbar::draw_preview(&mut frame, st, preview, panel);
                        }
                    }
                    Role::Dialog => {
                        if let Some(dialog) = &st.dialog {
                            dialogs::draw(&mut frame, st, dialog, panel);
                        }
                    }
                    Role::Capture => {
                        if let Some(capture) = &st.capture {
                            capture::draw(&mut frame, capture);
                        }
                    }
                    Role::Auth => {
                        background.paint(frame.canvas, frame.res.theme);
                        lock::draw(&mut frame, st, first_auth);
                    }
                    Role::Rename => desktop_icons::draw_rename(&mut frame, st),
                    Role::Security => {
                        if let Some(security) = &st.security {
                            security::draw(&mut frame, security);
                        }
                    }
                }
            }
            canvas.end_clip();
            glass_region = Some(if res.live_blur && glass::may_blur(role) { glass::region_of(&res.glass_regions) } else { Vec::new() });
            // A stretched surface reacts over its whole area.
            if stretched {
                for hit in &mut hits {
                    hit.rect = Rect::new(0.0, 0.0, w, h);
                }
            }
            surface.hover = surface.pointer.and_then(|p| ui::hit_at(&hits, p));
            surface.hits = hits;
            let (pw, ph) = canvas.pixel_size();
            match surface.front.as_mut() {
                Some(front) if !parts.is_empty() => {
                    // The parts drawn again are compared with what was sent
                    // and copied over where they differ.
                    for clip in parts {
                        let Some(rect) = damage::changed_in(front.data(), canvas.data(), pw, clip) else {
                            continue;
                        };
                        damage::copy_rect(canvas.data(), front.pixmap_mut().data_mut(), pw, rect);
                        if surface.opaque && !damage::is_opaque_in(front.data(), pw, rect) {
                            surface.opaque = false;
                            wl.set_opaque_region(None);
                        }
                        damage.push(rect);
                    }
                    surface.spare = Some(canvas);
                }
                _ => {
                    // Only what changed since the last drawing is sent; a
                    // drawing that changed nothing (the pointer moved onto a
                    // part without a hover look) is not sent at all.
                    let changed = match surface.front.as_ref().filter(|_| front_fits) {
                        Some(front) => damage::changed_rect(front.data(), canvas.data(), pw, ph),
                        None => Some((0, 0, pw, ph)),
                    };
                    damage.extend(changed);
                    // A drawing without transparent pixels (the wallpaper,
                    // the taskbar) is opaque: the compositor need not blend
                    // it or draw behind it.
                    let opaque = damage::is_opaque(canvas.data());
                    if opaque != surface.opaque {
                        if !opaque {
                            wl.set_opaque_region(None);
                            surface.opaque = false;
                        } else if let Ok(region) = Region::new(&*compositor_state) {
                            region.add(0, 0, surface.width as i32, surface.height as i32);
                            wl.set_opaque_region(Some(region.wl_region()));
                            surface.opaque = true;
                        }
                    }
                    surface.spare = surface.front.replace(canvas);
                }
            }
        }
        let painted = started.elapsed();
        let Some(front) = surface.front.as_ref() else {
            return;
        };
        let (pw, ph) = front.pixel_size();
        for rect in &damage {
            surface.buffers.changed(*rect);
        }

        // The opacity and the place, where the compositor fades and slides.
        let mut moved = false;
        if let (Some(direction), Some(fade)) = (slide, &surface.fade) {
            if animating || surface.faded {
                fade.set_multiplier(present::alpha_multiplier(presence));
                if let Handle::Layer(layer) = &surface.handle {
                    let (top, right, bottom, left) = present::slid_margin(surface.margin, direction, present::slide_offset(presence));
                    layer.set_margin(top, right, bottom, left);
                }
                surface.faded = animating;
                moved = true;
            }
        }

        // What the compositor shows: the drawing, or a frame of a transition
        // composed from it.
        let shown = if compose {
            surface.spare.take().filter(fits).or_else(|| present::reuse_canvas(cw, ch, scale)).or_else(|| Canvas::new(cw, ch, scale)).map(|mut composed| {
                composed.reset();
                composed.draw_pixmap(front.pixmap(), 0.0, (1.0 - presence) * present::SLIDE, cw, ch, presence);
                composed
            })
        } else {
            None
        };
        let whole = vec![(0, 0, pw, ph)];
        // A surface of one colour (stretched) goes as a single-pixel buffer
        // where the compositor offers them (`solid.rs`).
        let single = match front.data().get(..4) {
            Some(p) if stretched && shown.is_none() && !damage.is_empty() => solid.buffer([p[0], p[1], p[2], p[3]], qh),
            _ => None,
        };
        let sent = match &shown {
            Some(composed) => Some((surface.buffers.fill_other(shm, composed), whole)),
            None if single.is_some() => None,
            None if !damage.is_empty() || surface.composed => {
                let rects = if surface.composed { whole } else { damage };
                Some((surface.buffers.fill(shm, front), rects))
            }
            None => None,
        };
        let mut attached = None;
        if let Some((buffer, rects)) = sent {
            let Some(buffer) = buffer else {
                warn!("no shared memory for a surface; not drawn");
                return;
            };
            match &surface.viewport {
                Some(viewport) => {
                    wl.set_buffer_scale(1);
                    viewport.set_destination(surface.width as i32, surface.height as i32);
                }
                None => wl.set_buffer_scale(scale.ceil().max(1.0) as i32),
            }
            if matches!(role, Role::Toasts | Role::Preview) {
                // Only what is drawn takes the pointer: clicks in the gaps
                // between toasts and around a preview reach the windows below.
                if let Ok(region) = Region::new(&*compositor_state) {
                    for (x, y, w, h) in ui::input_rects(&surface.hits) {
                        region.add(x, y, w, h);
                    }
                    wl.set_input_region(Some(region.wl_region()));
                }
            }
            for &(dx, dy, dw, dh) in &rects {
                wl.damage_buffer(dx as i32, dy as i32, dw as i32, dh as i32);
            }
            if buffer.attach_to(&wl).is_err() {
                return;
            }
            attached = rects.into_iter().reduce(present::union);
        }
        if let (Some(buffer), Some(viewport)) = (&single, &surface.viewport) {
            wl.set_buffer_scale(1);
            viewport.set_destination(surface.width as i32, surface.height as i32);
            wl.attach(Some(buffer), 0, 0);
            wl.damage_buffer(0, 0, 1, 1);
            attached = Some((0, 0, 1, 1));
        }
        if let Some(composed) = shown {
            surface.spare = Some(composed);
        }
        if attached.is_some() {
            surface.composed = compose;
        }
        let blur_changed = glass_region.is_some_and(|rects| glass::send_region(surface, compositor_state, rects));
        if attached.is_some() || moved || animating || scrolling || blur_changed {
            // Asked for a frame, so a transition goes on at the display's
            // pace.
            wl.frame(qh, wl.clone());
            wl.commit();
            surface.frame_pending = true;
        }
        surface.dirty = animating || scrolling;
        // Magnified icons on their way: their cells again on the next frame.
        for cell in std::mem::take(&mut res.magnifying) {
            surface.touch_rect(cell);
        }
        if scrolling {
            // The page moves on: the next frame (the one where it comes to
            // rest included) draws it again.
            surface.change = std::mem::replace(&mut surface.change, Change::None).with(page_window);
        }
        stats::drawn(&role, (pw, ph), painted, started.elapsed(), attached, &mut surface.changed_at);
    }

    pub fn surface_index(&self, wl: &wl_surface::WlSurface) -> Option<usize> {
        self.surfaces.iter().position(|s| s.wl_surface() == wl)
    }

    fn set_scale(&mut self, wl: &wl_surface::WlSurface, scale: f32, from_fractional: bool) {
        if let Some(index) = self.surface_index(wl) {
            let surface = &mut self.surfaces[index];
            // Once the compositor sends fractional scales, they win over
            // the integer scale.
            if !from_fractional && surface.fractional.is_some() && surface.scale.fract() != 0.0 {
                return;
            }
            let scale = scale.clamp(1.0, 4.0);
            if (surface.scale - scale).abs() > 1e-3 {
                surface.scale = scale;
                surface.touch();
            }
        }
    }

    fn configured(&mut self, wl: &wl_surface::WlSurface, size: (u32, u32)) {
        let Some(index) = self.surface_index(wl) else {
            return;
        };
        let (w, h) = size;
        if w == 0 || h == 0 {
            return;
        }
        let surface = &mut self.surfaces[index];
        if (surface.width, surface.height) != (w, h) || !surface.configured {
            surface.width = w;
            surface.height = h;
            surface.configured = true;
            surface.frame_pending = false;
            surface.touch();
        }
        // The taskbar sits at the bottom edge of its output.
        if surface.role == Role::Taskbar {
            let output = surface.output.clone();
            let height = self.output_size(&output).1;
            self.surfaces[index].origin = (0.0, height - h as f32);
        }
        let surface = &mut self.surfaces[index];
        // Full-screen surfaces tell the size of their output.
        if matches!(surface.role, Role::Wallpaper | Role::Auth) {
            let output = surface.output.clone();
            let size = (w as f32, h as f32);
            match self.output_sizes.iter_mut().find(|(o, _)| *o == output) {
                Some(entry) if entry.1 != size => {
                    entry.1 = size;
                    self.res.backdrop = None;
                    self.res.backdrop_generation += 1;
                }
                Some(_) => return,
                None => self.output_sizes.push((output.clone(), size)),
            }
            for taskbar in self.surfaces.iter_mut().filter(|s| s.role == Role::Taskbar && s.output == output) {
                taskbar.origin = (0.0, size.1 - taskbar.height as f32);
            }
            self.mark_all();
        }
    }

    /// The first moment something has to happen without an event.
    fn next_deadline(&self, now: Instant) -> Option<Instant> {
        let mut soonest: Option<Instant> = None;
        let mut consider = |at: Option<Instant>| {
            if let Some(at) = at {
                soonest = Some(soonest.map_or(at, |s| s.min(at)));
            }
        };
        consider(self.st.tooltip.as_ref().filter(|t| !t.shown).map(|t| t.due));
        consider(self.st.osd.as_ref().map(|o| o.until));
        consider(self.st.notes.next_deadline(now));
        consider(self.st.preview.as_ref().and_then(|p| p.left_at).map(|at| at + desk::PREVIEW_GRACE));
        consider(self.slider_pending.as_ref().map(|_| self.slider_sent + desk::SLIDER_INTERVAL));
        consider(self.taskbar_hide_at);
        consider(self.prewarm_deadline(now));
        // Finished transitions are cleaned up promptly.
        if self.surfaces.iter().any(|s| s.anim.is_some()) {
            consider(Some(now + Duration::from_millis(16)));
        }
        soonest
    }

    // ---------------------------------------------------------------------
    // Lock screen and greeter
    // ---------------------------------------------------------------------

    pub fn submit(&mut self) {
        use lock::AuthState;
        if self.st.auth.state == AuthState::Checking {
            return;
        }
        if self.st.auth.editing_user {
            // Enter in the user name field moves on to the password.
            self.st.auth.editing_user = false;
            return self.mark(Role::Auth);
        }
        if self.st.auth.password.is_empty() {
            return;
        }
        let password = self.st.auth.password.take();
        self.st.auth.state = AuthState::Checking;
        self.st.auth.reveal = false;
        self.mark(Role::Auth);
        let sender = self.auth_sender.clone();
        let spawned = match self.st.mode {
            Mode::Lock => {
                let user = self.st.user.clone();
                std::thread::Builder::new().name("auth".into()).spawn(move || {
                    let result = if pam::authenticate(PAM_SERVICE, &user, password) {
                        AuthResult::Unlocked
                    } else {
                        AuthResult::Failed("The password is incorrect. Try again.".into())
                    };
                    let _ = sender.send(result);
                })
            }
            _ => {
                let user = self.st.auth.login_name();
                let socket = std::env::var_os("GREETD_SOCK").map(PathBuf::from);
                std::thread::Builder::new().name("auth".into()).spawn(move || {
                    let result = match socket {
                        Some(socket) => match greetd::login(&socket, &user, password, &[SESSION_COMMAND.to_owned()]) {
                            Ok(()) => AuthResult::LoggedIn,
                            Err(message) => AuthResult::Failed(message),
                        },
                        None => AuthResult::Failed("The login service is not available".into()),
                    };
                    let _ = sender.send(result);
                })
            }
        };
        if spawned.is_err() {
            self.st.auth.state = AuthState::Failed("Could not check the password; try again".into());
        }
    }

    fn auth_finished(&mut self, result: AuthResult) {
        match result {
            AuthResult::Unlocked => {
                if let Some(lock) = self.session_lock.take() {
                    lock.unlock();
                    // The unlock must reach the compositor before this
                    // process exits, or the session would stay locked.
                    let _ = self.conn.roundtrip();
                }
                self.exit = Some(0);
            }
            AuthResult::LoggedIn => {
                // greetd starts the session once the greeter and its
                // compositor are gone.
                self.send_control("quit");
                self.exit = Some(0);
            }
            AuthResult::Failed(message) => {
                self.st.auth.state = lock::AuthState::Failed(message);
                self.mark(Role::Auth);
            }
        }
    }
}

// -------------------------------------------------------------------------
// Wayland handlers
// -------------------------------------------------------------------------

impl CompositorHandler for Shell {
    fn scale_factor_changed(&mut self, _: &Connection, _: &QueueHandle<Self>, surface: &wl_surface::WlSurface, factor: i32) {
        self.set_scale(surface, factor as f32, false);
    }

    fn transform_changed(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_surface::WlSurface, _: wl_output::Transform) {}

    fn frame(&mut self, _: &Connection, _: &QueueHandle<Self>, surface: &wl_surface::WlSurface, _time: u32) {
        if let Some(index) = self.surface_index(surface) {
            self.surfaces[index].frame_pending = false;
        }
    }

    fn surface_enter(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_surface::WlSurface, _: &wl_output::WlOutput) {}

    fn surface_leave(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_surface::WlSurface, _: &wl_output::WlOutput) {}
}

impl Dispatch<WpFractionalScaleV1, wl_surface::WlSurface> for Shell {
    fn event(shell: &mut Self, _: &WpFractionalScaleV1, event: wp_fractional_scale_v1::Event, surface: &wl_surface::WlSurface, _: &Connection, _: &QueueHandle<Self>) {
        if let wp_fractional_scale_v1::Event::PreferredScale { scale } = event {
            shell.set_scale(surface, scale as f32 / 120.0, true);
        }
    }
}

delegate_noop!(Shell: WpViewporter);
delegate_noop!(Shell: WpViewport);
delegate_noop!(Shell: WpFractionalScaleManagerV1);
delegate_noop!(Shell: WpAlphaModifierV1);
delegate_noop!(Shell: WpAlphaModifierSurfaceV1);

impl OutputHandler for Shell {
    fn output_state(&mut self) -> &mut OutputState {
        &mut self.output_state
    }

    fn new_output(&mut self, _: &Connection, _: &QueueHandle<Self>, output: wl_output::WlOutput) {
        self.output_added(&output);
    }

    fn update_output(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_output::WlOutput) {}

    fn output_destroyed(&mut self, _: &Connection, _: &QueueHandle<Self>, output: wl_output::WlOutput) {
        let lost_flyout = self.surfaces.iter().any(|s| s.output == output && s.role == Role::Flyout);
        self.surfaces.retain(|s| s.output != output);
        self.output_sizes.retain(|(o, _)| *o != output);
        if lost_flyout {
            self.drop_overlays();
        }
    }
}

impl LayerShellHandler for Shell {
    fn closed(&mut self, _: &Connection, _: &QueueHandle<Self>, layer: &LayerSurface) {
        let role = self.surfaces.iter().find(|s| matches!(&s.handle, Handle::Layer(l) if l == layer)).map(|s| s.role);
        self.surfaces.retain(|s| !matches!(&s.handle, Handle::Layer(l) if l == layer));
        if matches!(role, Some(Role::Flyout | Role::Menu(_) | Role::Dialog | Role::Capture | Role::Dismiss | Role::Smoke)) {
            self.drop_overlays();
        }
    }

    fn configure(&mut self, _: &Connection, _: &QueueHandle<Self>, layer: &LayerSurface, configure: LayerSurfaceConfigure, _serial: u32) {
        let wl = layer.wl_surface().clone();
        self.configured(&wl, configure.new_size);
    }
}

impl SessionLockHandler for Shell {
    fn locked(&mut self, _: &Connection, qh: &QueueHandle<Self>, session_lock: SessionLock) {
        let outputs: Vec<wl_output::WlOutput> = self.output_state.outputs().collect();
        for output in outputs {
            let surface = self.compositor_state.create_surface(qh);
            let lock_surface = session_lock.create_lock_surface(surface, &output, qh);
            self.new_surface(Role::Auth, Handle::Lock(lock_surface), &output, (0.0, 0.0), false);
        }
    }

    fn finished(&mut self, _: &Connection, _: &QueueHandle<Self>, _: SessionLock) {
        // Another lock client holds the lock, or the compositor refused.
        warn!("the session lock was not granted to this lock screen");
        self.exit = Some(1);
    }

    fn configure(&mut self, _: &Connection, _: &QueueHandle<Self>, lock_surface: SessionLockSurface, configure: SessionLockSurfaceConfigure, _serial: u32) {
        let wl = lock_surface.wl_surface().clone();
        self.configured(&wl, configure.new_size);
    }
}

impl SeatHandler for Shell {
    fn seat_state(&mut self) -> &mut SeatState {
        &mut self.seat_state
    }

    fn new_seat(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_seat::WlSeat) {}

    fn new_capability(&mut self, _: &Connection, qh: &QueueHandle<Self>, seat: wl_seat::WlSeat, capability: Capability) {
        if capability == Capability::Keyboard && self.keyboard.is_none() {
            let keyboard = self.seat_state.get_keyboard_with_repeat(
                qh,
                &seat,
                None,
                self.loop_handle.clone(),
                Box::new(|shell: &mut Shell, _, event| shell.key_pressed(event)),
            );
            match keyboard {
                Ok(keyboard) => self.keyboard = Some(keyboard),
                Err(err) => warn!("no keyboard: {err}"),
            }
        }
        if capability == Capability::Pointer && self.pointer.is_none() {
            match self.seat_state.get_pointer(qh, &seat) {
                Ok(pointer) => self.pointer = Some(pointer),
                Err(err) => warn!("no pointer: {err}"),
            }
        }
        if self.data_device.is_none() && self.st.mode == Mode::Desktop {
            self.data_device = self.data_devices.as_ref().map(|manager| manager.get_data_device(qh, &seat));
        }
        if self.st.mode == Mode::Desktop {
            self.clip_seat(qh, &seat);
        }
    }

    fn remove_capability(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_seat::WlSeat, capability: Capability) {
        if capability == Capability::Keyboard {
            if let Some(keyboard) = self.keyboard.take() {
                keyboard.release();
            }
        }
        if capability == Capability::Pointer {
            if let Some(pointer) = self.pointer.take() {
                pointer.release();
            }
        }
    }

    fn remove_seat(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_seat::WlSeat) {}
}

impl KeyboardHandler for Shell {
    fn enter(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_keyboard::WlKeyboard, _: &wl_surface::WlSurface, _: u32, _: &[u32], _: &[Keysym]) {}

    fn leave(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_keyboard::WlKeyboard, _: &wl_surface::WlSurface, _: u32) {}

    fn press_key(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_keyboard::WlKeyboard, serial: u32, event: KeyEvent) {
        self.serial = serial;
        self.key_pressed(event);
    }

    fn release_key(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_keyboard::WlKeyboard, _: u32, _: KeyEvent) {}

    fn update_modifiers(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_keyboard::WlKeyboard, _: u32, modifiers: Modifiers, _: u32) {
        self.modifiers = modifiers;
        if self.st.mode != Mode::Desktop && self.st.auth.caps_lock != modifiers.caps_lock {
            self.st.auth.caps_lock = modifiers.caps_lock;
            self.mark(Role::Auth);
        }
    }
}

impl PointerHandler for Shell {
    fn pointer_frame(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_pointer::WlPointer, events: &[PointerEvent]) {
        for event in events {
            self.pointer_event(event);
        }
    }
}

impl DataDeviceHandler for Shell {
    // A drag from another program: the desktop icons take files.
    fn enter(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &WlDataDevice, x: f64, y: f64, surface: &wl_surface::WlSurface) {
        self.desk_drag_enter(surface, x, y);
    }
    fn leave(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &WlDataDevice) {
        self.desk_drag_leave();
    }
    fn motion(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &WlDataDevice, x: f64, y: f64) {
        self.desk_drag_motion(x, y);
    }
    fn selection(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &WlDataDevice) {}
    fn drop_performed(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &WlDataDevice) {
        self.desk_drop();
    }
}

impl DataOfferHandler for Shell {
    fn source_actions(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &mut DragOffer, _: DndAction) {}
    fn selected_action(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &mut DragOffer, _: DndAction) {}
}

impl DataSourceHandler for Shell {
    fn accept_mime(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &WlDataSource, _: Option<String>) {}

    fn send_request(&mut self, _: &Connection, _: &QueueHandle<Self>, source: &WlDataSource, mime: String, pipe: WritePipe) {
        // Files the desktop icons copied.
        let Some(mut pipe) = self.desk_clipboard_send(source, &mime, pipe) else {
            return;
        };
        let Some((own, bytes)) = &self.clipboard else {
            return;
        };
        if own.inner() != source || mime != "image/png" {
            return;
        }
        // Written on a thread: the reader may be slow, the shell must not wait.
        let bytes = bytes.clone();
        let _ = std::thread::Builder::new().name("clipboard".into()).spawn(move || {
            use std::io::Write;
            let _ = pipe.write_all(&bytes);
        });
    }

    fn cancelled(&mut self, _: &Connection, _: &QueueHandle<Self>, source: &WlDataSource) {
        self.desk_clipboard_cancelled(source);
        // Another program took the clipboard: the picture can be freed.
        if self.clipboard.as_ref().is_some_and(|(own, _)| own.inner() == source) {
            self.clipboard = None;
        }
    }

    fn dnd_dropped(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &WlDataSource) {}
    fn dnd_finished(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &WlDataSource) {}
    fn action(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &WlDataSource, _: DndAction) {}
}

impl Shell {
    /// Puts a PNG on the clipboard.
    pub fn copy_png(&mut self, bytes: Vec<u8>) {
        let (Some(manager), Some(device)) = (&self.data_devices, &self.data_device) else {
            return;
        };
        let source = manager.create_copy_paste_source(&self.qh, ["image/png"]);
        source.set_selection(device, self.serial);
        self.clipboard = Some((source, Arc::new(bytes)));
    }
}

impl ShmHandler for Shell {
    fn shm_state(&mut self) -> &mut Shm {
        &mut self.shm
    }
}

impl ProvidesRegistryState for Shell {
    fn registry(&mut self) -> &mut RegistryState {
        &mut self.registry_state
    }
    registry_handlers![OutputState, SeatState];
}

delegate_compositor!(Shell);
delegate_output!(Shell);
delegate_shm!(Shell);
delegate_seat!(Shell);
delegate_keyboard!(Shell);
delegate_pointer!(Shell);
delegate_layer!(Shell);
delegate_session_lock!(Shell);
delegate_data_device!(Shell);
delegate_registry!(Shell);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_compositors_memory_setting_is_not_passed_on() {
        assert!(ours(Some(std::ffi::OsStr::new("glibc.malloc.mmap_threshold=262144"))));
        assert!(!ours(Some(std::ffi::OsStr::new("glibc.malloc.arena_max=2"))), "the session's own stays");
        assert!(!ours(None));
    }

    #[test]
    fn missing_fonts_are_reported_once_and_say_what_follows() {
        for mode in [Mode::Desktop, Mode::Greeter, Mode::Lock] {
            assert_eq!(missing_fonts(mode, true), None);
            let problem = missing_fonts(mode, false).unwrap();
            assert!(problem.starts_with("no usable font found"), "{problem}");
            assert!(problem.contains("/usr/share/fonts") && problem.contains("fonts-inter"), "{problem}");
            assert!(!problem.contains('\n'), "one line: {problem}");
        }
        assert!(missing_fonts(Mode::Greeter, false).unwrap().contains("login screen"));
        assert!(missing_fonts(Mode::Lock, false).unwrap().contains("press Enter to unlock"));
    }
}
