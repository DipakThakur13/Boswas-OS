//! boswas-shell: the Boswas OS desktop shell.
//!
//! One unprivileged program with three roles:
//!
//! * (no option)  the desktop: wallpaper, panel and launcher
//! * `--lock`     the lock screen (ext-session-lock, PAM)
//! * `--greeter`  the login screen (greetd)
//!
//! It replaces plasmashell, kscreenlocker's greeter and the SDDM theme. The
//! compositor starts it and hands it a trusted Wayland connection; if the
//! shell crashes the compositor restarts it and open windows are untouched.

mod apps;
mod control;
mod greetd;
mod launcher;
mod lock;
mod pam;
mod panel;
mod status;
mod ui;
mod wallpaper;

use std::{
    path::PathBuf,
    time::{Duration, Instant},
};

use boswas_ui::{Canvas, Scheme, Theme};
use smithay_client_toolkit::{
    compositor::{CompositorHandler, CompositorState},
    delegate_compositor, delegate_keyboard, delegate_layer, delegate_output, delegate_pointer, delegate_registry,
    delegate_seat, delegate_session_lock, delegate_shm,
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
        pointer::{PointerEvent, PointerEventKind, PointerHandler},
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
    shm::{slot::SlotPool, Shm, ShmHandler},
};
use tracing::{error, info, warn};
use wayland_client::{
    globals::registry_queue_init,
    protocol::{wl_keyboard, wl_output, wl_pointer, wl_seat, wl_shm, wl_surface},
    Connection, QueueHandle,
};

use crate::{
    apps::{App, Usage},
    control::{Control, ControlEvent, WindowInfo},
    launcher::{Key, KeyOutcome, Launcher, LauncherAction, Power},
    lock::{AuthState, AuthView, LockAction},
    panel::{PanelAction, PanelModel},
    status::Status,
    ui::{action_at, Frame, Rect, Resources, TextField},
    wallpaper::Background,
};

const USAGE: &str = "\
Usage: boswas-shell [--lock | --greeter]

The Boswas OS desktop shell: wallpaper, panel and launcher.

Options:
  --lock         show the lock screen and unlock after the user's password
  --greeter      show the login screen (run by greetd)
  -h, --help     show this help
  -V, --version  show the version
";

/// Applications pinned to the panel, by desktop file id, in order.
const PINNED: &[&str] = &[
    "com.boswas.ControlCenter",
    "org.kde.dolphin",
    "org.kde.konsole",
    "firefox-esr",
    "com.boswas.CompatibilityManager",
];

/// The PAM service the lock screen authenticates against.
const PAM_SERVICE: &str = "boswas-lock";
/// What greetd starts after a successful login.
const SESSION_COMMAND: &str = "/usr/bin/boswas-session";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Mode {
    Desktop,
    Lock,
    Greeter,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Role {
    Wallpaper,
    Panel,
    Launcher,
    /// Lock screen or greeter.
    Auth,
}

enum Hits {
    None,
    Panel(Vec<(Rect, PanelAction)>),
    Launcher(Vec<(Rect, LauncherAction)>),
    Auth(Vec<(Rect, LockAction)>),
}

/// The Wayland object behind a shell surface.
enum Handle {
    Layer(LayerSurface),
    Lock(SessionLockSurface),
}

struct Surface {
    role: Role,
    handle: Handle,
    output: wl_output::WlOutput,
    width: u32,
    height: u32,
    scale: i32,
    pool: Option<SlotPool>,
    configured: bool,
    dirty: bool,
    frame_pending: bool,
    pointer: Option<(f32, f32)>,
    hits: Hits,
}

impl Surface {
    fn wl_surface(&self) -> &wl_surface::WlSurface {
        match &self.handle {
            Handle::Layer(layer) => layer.wl_surface(),
            Handle::Lock(lock) => lock.wl_surface(),
        }
    }
}

/// The result of a password check done on a worker thread.
enum AuthResult {
    Unlocked,
    LoggedIn,
    Failed(String),
}

struct Shell {
    registry_state: RegistryState,
    seat_state: SeatState,
    output_state: OutputState,
    compositor_state: CompositorState,
    shm: Shm,
    layer_shell: Option<LayerShell>,
    session_lock_state: SessionLockState,
    session_lock: Option<SessionLock>,
    conn: Connection,
    qh: QueueHandle<Shell>,
    loop_handle: LoopHandle<'static, Shell>,
    auth_sender: Sender<AuthResult>,

    mode: Mode,
    res: Resources,
    background: Background,
    surfaces: Vec<Surface>,
    control: Option<Control>,
    keyboard: Option<wl_keyboard::WlKeyboard>,
    pointer: Option<wl_pointer::WlPointer>,
    modifiers: Modifiers,

    apps: Vec<App>,
    pinned: Vec<usize>,
    usage: Usage,
    windows: Vec<WindowInfo>,
    workspace: usize,
    status: Status,
    launcher: Launcher,
    auth: AuthView,
    /// Login name of the session's user (lock screen).
    user: String,
    user_name: String,
    exit: Option<i32>,
}

fn main() {
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

    let scheme = match std::env::var("BOSWAS_SCHEME").as_deref() {
        Ok("light") => Scheme::Light,
        _ => Scheme::Dark,
    };
    let (user, user_name) = pam::current_user();
    let locale = std::env::var("LC_MESSAGES").or_else(|_| std::env::var("LANG")).unwrap_or_default();
    let apps = if mode == Mode::Desktop {
        apps::load_apps(&apps::application_dirs(), &locale)
    } else {
        Vec::new()
    };
    let pinned = PINNED.iter().filter_map(|id| apps.iter().position(|a| a.id == *id)).collect();

    let mut auth = AuthView {
        display_name: user_name.clone(),
        ..AuthView::default()
    };
    match mode {
        Mode::Lock => auth.hint = "Enter your password to unlock".into(),
        Mode::Greeter => {
            auth.user_field = Some(TextField::default());
            auth.editing_user = true;
            auth.hint = "Sign in to Boswas OS".into();
        }
        Mode::Desktop => {}
    }

    let mut shell = Shell {
        registry_state: RegistryState::new(&globals),
        seat_state: SeatState::new(&globals, &qh),
        output_state: OutputState::new(&globals, &qh),
        compositor_state: CompositorState::bind(&globals, &qh)?,
        shm: Shm::bind(&globals, &qh)?,
        layer_shell: LayerShell::bind(&globals, &qh).ok(),
        session_lock_state: SessionLockState::new(&globals, &qh),
        session_lock: None,
        conn: conn.clone(),
        qh: qh.clone(),
        loop_handle: event_loop.handle(),
        auth_sender,
        mode,
        res: Resources::new(Theme::of(scheme)),
        background: Background::default(),
        surfaces: Vec::new(),
        control,
        keyboard: None,
        pointer: None,
        modifiers: Modifiers::default(),
        apps,
        pinned,
        usage: Usage::load(),
        windows: Vec::new(),
        workspace: 0,
        status: Status::read(),
        launcher: Launcher::default(),
        auth,
        user,
        user_name,
        exit: None,
    };

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

    WaylandSource::new(conn, event_queue).insert(event_loop.handle())?;

    // Compositor events (window list, launcher key).
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

    // The clock and status area: checked every second, redrawn on change.
    event_loop
        .handle()
        .insert_source(Timer::from_duration(Duration::from_secs(1)), |_, _, shell| {
            shell.tick();
            TimeoutAction::ToDuration(Duration::from_secs(1))
        })?;

    info!("boswas-shell {} started ({mode:?})", env!("CARGO_PKG_VERSION"));
    while shell.exit.is_none() {
        event_loop.dispatch(None, &mut shell)?;
        shell.redraw_dirty();
    }
    Ok(shell.exit.unwrap_or(0))
}

impl Shell {
    // ---------------------------------------------------------------------
    // Surfaces
    // ---------------------------------------------------------------------

    fn create_layer(&mut self, role: Role, output: &wl_output::WlOutput) {
        let Some(layer_shell) = &self.layer_shell else {
            return;
        };
        let surface = self.compositor_state.create_surface(&self.qh);
        let (layer_kind, namespace) = match role {
            Role::Wallpaper => (Layer::Background, "boswas-wallpaper"),
            Role::Panel => (Layer::Top, "boswas-panel"),
            Role::Launcher => (Layer::Overlay, "boswas-launcher"),
            Role::Auth => (Layer::Overlay, "boswas-greeter"),
        };
        let layer = layer_shell.create_layer_surface(&self.qh, surface, layer_kind, Some(namespace), Some(output));
        match role {
            Role::Wallpaper => {
                layer.set_anchor(Anchor::TOP | Anchor::BOTTOM | Anchor::LEFT | Anchor::RIGHT);
                layer.set_exclusive_zone(-1);
                layer.set_keyboard_interactivity(KeyboardInteractivity::None);
            }
            Role::Panel => {
                layer.set_anchor(Anchor::BOTTOM | Anchor::LEFT | Anchor::RIGHT);
                layer.set_size(0, panel::HEIGHT);
                layer.set_exclusive_zone(panel::HEIGHT as i32);
                layer.set_keyboard_interactivity(KeyboardInteractivity::None);
            }
            Role::Launcher => {
                // Covers the area above the panel, so a click anywhere
                // outside the flyout closes it.
                layer.set_anchor(Anchor::TOP | Anchor::BOTTOM | Anchor::LEFT | Anchor::RIGHT);
                layer.set_exclusive_zone(0);
                layer.set_keyboard_interactivity(KeyboardInteractivity::Exclusive);
            }
            Role::Auth => {
                layer.set_anchor(Anchor::TOP | Anchor::BOTTOM | Anchor::LEFT | Anchor::RIGHT);
                layer.set_exclusive_zone(-1);
                layer.set_keyboard_interactivity(KeyboardInteractivity::Exclusive);
            }
        }
        layer.commit();
        self.surfaces.push(Surface {
            role,
            handle: Handle::Layer(layer),
            output: output.clone(),
            width: 0,
            height: 0,
            scale: 1,
            pool: None,
            configured: false,
            dirty: true,
            frame_pending: false,
            pointer: None,
            hits: Hits::None,
        });
    }

    fn output_added(&mut self, output: &wl_output::WlOutput) {
        match self.mode {
            Mode::Desktop => {
                self.create_layer(Role::Wallpaper, output);
                self.create_layer(Role::Panel, output);
            }
            Mode::Greeter => self.create_layer(Role::Auth, output),
            Mode::Lock => {
                if let Some(lock) = self.session_lock.as_ref().filter(|lock| lock.is_locked()) {
                    let surface = self.compositor_state.create_surface(&self.qh);
                    let lock_surface = lock.create_lock_surface(surface, output, &self.qh);
                    self.push_lock_surface(lock_surface, output);
                }
            }
        }
    }

    fn push_lock_surface(&mut self, lock_surface: SessionLockSurface, output: &wl_output::WlOutput) {
        self.surfaces.push(Surface {
            role: Role::Auth,
            handle: Handle::Lock(lock_surface),
            output: output.clone(),
            width: 0,
            height: 0,
            scale: 1,
            pool: None,
            configured: false,
            dirty: true,
            frame_pending: false,
            pointer: None,
            hits: Hits::None,
        });
    }

    fn mark(&mut self, role: Role) {
        for surface in self.surfaces.iter_mut().filter(|s| s.role == role) {
            surface.dirty = true;
        }
    }

    fn redraw_dirty(&mut self) {
        for index in 0..self.surfaces.len() {
            if self.surfaces[index].dirty && !self.surfaces[index].frame_pending {
                self.draw(index);
            }
        }
    }

    fn draw(&mut self, index: usize) {
        let Shell {
            surfaces,
            res,
            background,
            shm,
            qh,
            apps,
            pinned,
            windows,
            workspace,
            status,
            launcher,
            auth,
            user_name,
            ..
        } = self;
        let Some(surface) = surfaces.get_mut(index) else {
            return;
        };
        if !surface.configured || surface.width == 0 || surface.height == 0 {
            return;
        }
        let Some(mut canvas) = Canvas::new(surface.width as f32, surface.height as f32, surface.scale as f32) else {
            return;
        };
        let pointer = surface.pointer;
        // The first output's auth surface takes the input.
        let is_first_auth = index == 0 || surface.role != Role::Auth;
        surface.hits = match surface.role {
            Role::Wallpaper => {
                background.paint(&mut canvas, res.theme);
                Hits::None
            }
            Role::Panel => {
                let mut hits = Vec::new();
                let model = PanelModel {
                    apps,
                    pinned,
                    windows,
                    workspace: *workspace,
                    status,
                    launcher_open: launcher.open,
                };
                panel::draw(
                    &mut Frame {
                        canvas: &mut canvas,
                        res,
                        pointer,
                        hits: &mut hits,
                    },
                    &model,
                );
                Hits::Panel(hits)
            }
            Role::Launcher => {
                let mut hits = Vec::new();
                launcher::draw(
                    &mut Frame {
                        canvas: &mut canvas,
                        res,
                        pointer,
                        hits: &mut hits,
                    },
                    launcher,
                    apps,
                    user_name,
                );
                Hits::Launcher(hits)
            }
            Role::Auth => {
                let mut hits = Vec::new();
                lock::draw(
                    &mut Frame {
                        canvas: &mut canvas,
                        res,
                        pointer,
                        hits: &mut hits,
                    },
                    auth,
                    status,
                    background,
                    is_first_auth,
                );
                Hits::Auth(hits)
            }
        };

        let (pw, ph) = canvas.pixel_size();
        let bytes = pw as usize * ph as usize * 4;
        if surface.pool.is_none() {
            surface.pool = SlotPool::new(bytes, shm).ok();
        }
        let Some(pool) = surface.pool.as_mut() else {
            warn!("no shared memory for a surface; not drawn");
            return;
        };
        let (buffer, data) = match pool.create_buffer(pw as i32, ph as i32, pw as i32 * 4, wl_shm::Format::Argb8888) {
            Ok(created) => created,
            Err(err) => {
                warn!("could not allocate a surface buffer: {err}");
                return;
            }
        };
        canvas.write_argb8888(data);
        let wl = surface.wl_surface().clone();
        wl.set_buffer_scale(surface.scale);
        wl.damage_buffer(0, 0, pw as i32, ph as i32);
        wl.frame(qh, wl.clone());
        if buffer.attach_to(&wl).is_err() {
            return;
        }
        wl.commit();
        surface.frame_pending = true;
        surface.dirty = false;
    }

    fn surface_index(&self, wl: &wl_surface::WlSurface) -> Option<usize> {
        self.surfaces.iter().position(|s| s.wl_surface() == wl)
    }

    // ---------------------------------------------------------------------
    // Desktop: panel and launcher
    // ---------------------------------------------------------------------

    fn control_event(&mut self, event: ControlEvent) {
        match event {
            ControlEvent::Windows(windows) => {
                if self.windows != windows {
                    self.windows = windows;
                    self.mark(Role::Panel);
                }
            }
            ControlEvent::Workspace { active, .. } => {
                self.workspace = active;
                self.mark(Role::Panel);
            }
            ControlEvent::Launcher => self.toggle_launcher(),
            ControlEvent::Lock(true) => self.close_launcher(),
            ControlEvent::Lock(false) => {}
            ControlEvent::Appearance { light, wallpaper } => {
                let scheme = if light { Scheme::Light } else { Scheme::Dark };
                if self.res.theme.scheme != scheme {
                    self.res = Resources::new(Theme::of(scheme));
                }
                self.background.load_picture(&wallpaper);
                for surface in &mut self.surfaces {
                    surface.dirty = true;
                }
            }
        }
    }

    fn toggle_launcher(&mut self) {
        if self.mode != Mode::Desktop {
            return;
        }
        if self.launcher.open {
            return self.close_launcher();
        }
        // Applications may have been installed since the shell started.
        let locale = std::env::var("LC_MESSAGES").or_else(|_| std::env::var("LANG")).unwrap_or_default();
        self.apps = apps::load_apps(&apps::application_dirs(), &locale);
        self.pinned = PINNED.iter().filter_map(|id| self.apps.iter().position(|a| a.id == *id)).collect();
        self.launcher.show(&self.apps, &self.usage.counts);
        let output = self.surfaces.iter().find(|s| s.role == Role::Panel).map(|s| s.output.clone());
        if let Some(output) = output {
            self.create_layer(Role::Launcher, &output);
        }
        self.mark(Role::Panel);
    }

    fn close_launcher(&mut self) {
        if !self.launcher.open {
            return;
        }
        self.launcher.hide();
        self.surfaces.retain(|s| s.role != Role::Launcher);
        self.mark(Role::Panel);
    }

    fn launch(&mut self, index: usize) {
        let Some(app) = self.apps.get(index).cloned() else {
            return;
        };
        match apps::launch(&app) {
            Ok(()) => self.usage.record(&app.id),
            Err(err) => warn!("could not start {}: {err}", app.name),
        }
        self.close_launcher();
    }

    fn power(&mut self, power: Power) {
        if !self.launcher.power_clicked(power, Instant::now()) {
            return self.mark(Role::Launcher);
        }
        self.close_launcher();
        match power {
            Power::Lock => self.send_control("lock"),
            Power::LogOut => self.send_control("quit"),
            Power::Restart => self.systemctl("reboot"),
            Power::ShutDown => self.systemctl("poweroff"),
        }
    }

    fn send_control(&mut self, cmd: &str) {
        if let Some(control) = &mut self.control {
            control.send(cmd, serde_json::json!({}));
        }
    }

    fn systemctl(&mut self, verb: &str) {
        if let Err(err) = apps::spawn(&["systemctl".into(), verb.into()]) {
            warn!("systemctl {verb}: {err}");
        }
    }

    fn panel_action(&mut self, action: PanelAction) {
        match action {
            PanelAction::Launcher => self.toggle_launcher(),
            PanelAction::Window(id) => {
                self.close_launcher();
                if let Some(control) = &mut self.control {
                    control.window("toggle-minimize", id);
                }
            }
            PanelAction::Launch(index) => self.launch(index),
        }
    }

    fn launcher_action(&mut self, action: LauncherAction) {
        match action {
            LauncherAction::Launch(index) => self.launch(index),
            LauncherAction::Power(power) => self.power(power),
            LauncherAction::Dismiss => self.close_launcher(),
            LauncherAction::Nothing => {}
        }
    }

    fn tick(&mut self) {
        let status = Status::read();
        if status != self.status {
            self.status = status;
            self.mark(Role::Panel);
            self.mark(Role::Auth);
        }
        if self.launcher.expire(Instant::now()) {
            self.mark(Role::Launcher);
        }
    }

    // ---------------------------------------------------------------------
    // Lock screen and greeter
    // ---------------------------------------------------------------------

    fn submit(&mut self) {
        if self.auth.state == AuthState::Checking {
            return;
        }
        if self.auth.editing_user {
            // Enter in the user name field moves on to the password.
            self.auth.editing_user = false;
            return self.mark(Role::Auth);
        }
        if self.auth.password.is_empty() {
            return;
        }
        let password = self.auth.password.take();
        self.auth.state = AuthState::Checking;
        self.mark(Role::Auth);
        let sender = self.auth_sender.clone();
        let spawned = match self.mode {
            Mode::Lock => {
                let user = self.user.clone();
                std::thread::Builder::new().name("auth".into()).spawn(move || {
                    let result = if pam::authenticate(PAM_SERVICE, &user, password) {
                        AuthResult::Unlocked
                    } else {
                        AuthResult::Failed("Incorrect password".into())
                    };
                    let _ = sender.send(result);
                })
            }
            _ => {
                let user = self.auth.user_field.as_ref().map(|f| f.text().to_owned()).unwrap_or_default();
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
            self.auth.state = AuthState::Failed("Could not check the password; try again".into());
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
                self.auth.state = AuthState::Failed(message);
                self.mark(Role::Auth);
            }
        }
    }

    fn auth_action(&mut self, action: LockAction) {
        match action {
            LockAction::Submit => self.submit(),
            LockAction::FocusUser => self.auth.editing_user = self.auth.user_field.is_some(),
            LockAction::FocusPassword => self.auth.editing_user = false,
        }
        self.mark(Role::Auth);
    }

    // ---------------------------------------------------------------------
    // Keyboard
    // ---------------------------------------------------------------------

    fn key_pressed(&mut self, event: KeyEvent) {
        let ctrl = self.modifiers.ctrl;
        if self.mode == Mode::Desktop {
            if !self.launcher.open {
                return;
            }
            let key = match event.keysym {
                Keysym::Escape => Some(Key::Escape),
                Keysym::Return | Keysym::KP_Enter => Some(Key::Enter),
                Keysym::BackSpace => Some(Key::Backspace),
                Keysym::Left => Some(Key::Left),
                Keysym::Right => Some(Key::Right),
                Keysym::Up => Some(Key::Up),
                Keysym::Down => Some(Key::Down),
                Keysym::u if ctrl => Some(Key::ClearLine),
                _ => None,
            };
            let outcome = match (key, &event.utf8) {
                (Some(key), _) => self.launcher.key(key, &self.apps, &self.usage.counts),
                (None, Some(text)) if !ctrl && !self.modifiers.alt => {
                    self.launcher.typed(text, &self.apps, &self.usage.counts)
                }
                _ => KeyOutcome::Ignored,
            };
            match outcome {
                KeyOutcome::Redraw => self.mark(Role::Launcher),
                KeyOutcome::Launch(index) => self.launch(index),
                KeyOutcome::Dismiss => self.close_launcher(),
                KeyOutcome::Ignored => {}
            }
            return;
        }

        if self.auth.state == AuthState::Checking {
            return;
        }
        match event.keysym {
            Keysym::Return | Keysym::KP_Enter => return self.submit(),
            Keysym::Tab | Keysym::ISO_Left_Tab if self.auth.user_field.is_some() => {
                self.auth.editing_user = !self.auth.editing_user;
            }
            Keysym::BackSpace => {
                self.auth.field().backspace();
            }
            Keysym::Escape => self.auth.field().clear(),
            Keysym::u if ctrl => self.auth.field().clear(),
            _ => match &event.utf8 {
                Some(text) if !ctrl && !self.modifiers.alt => {
                    self.auth.field().insert(text);
                }
                _ => return,
            },
        }
        if matches!(self.auth.state, AuthState::Failed(_)) {
            self.auth.state = AuthState::Idle;
        }
        self.mark(Role::Auth);
    }
}

// -------------------------------------------------------------------------
// Wayland handlers
// -------------------------------------------------------------------------

impl CompositorHandler for Shell {
    fn scale_factor_changed(&mut self, _: &Connection, _: &QueueHandle<Self>, surface: &wl_surface::WlSurface, factor: i32) {
        if let Some(index) = self.surface_index(surface) {
            let surface = &mut self.surfaces[index];
            let factor = factor.clamp(1, 4);
            if surface.scale != factor {
                surface.scale = factor;
                surface.dirty = true;
            }
        }
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

impl OutputHandler for Shell {
    fn output_state(&mut self) -> &mut OutputState {
        &mut self.output_state
    }

    fn new_output(&mut self, _: &Connection, _: &QueueHandle<Self>, output: wl_output::WlOutput) {
        self.output_added(&output);
    }

    fn update_output(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_output::WlOutput) {}

    fn output_destroyed(&mut self, _: &Connection, _: &QueueHandle<Self>, output: wl_output::WlOutput) {
        self.surfaces.retain(|s| s.output != output);
        if !self.surfaces.iter().any(|s| s.role == Role::Launcher) && self.launcher.open {
            self.launcher.hide();
        }
    }
}

impl LayerShellHandler for Shell {
    fn closed(&mut self, _: &Connection, _: &QueueHandle<Self>, layer: &LayerSurface) {
        let closed_launcher = self
            .surfaces
            .iter()
            .any(|s| s.role == Role::Launcher && matches!(&s.handle, Handle::Layer(l) if l == layer));
        self.surfaces.retain(|s| !matches!(&s.handle, Handle::Layer(l) if l == layer));
        if closed_launcher {
            self.launcher.hide();
            self.mark(Role::Panel);
        }
    }

    fn configure(&mut self, _: &Connection, _: &QueueHandle<Self>, layer: &LayerSurface, configure: LayerSurfaceConfigure, _serial: u32) {
        let Some(surface) = self
            .surfaces
            .iter_mut()
            .find(|s| matches!(&s.handle, Handle::Layer(l) if l == layer))
        else {
            return;
        };
        let (w, h) = configure.new_size;
        if w == 0 || h == 0 {
            return;
        }
        if (surface.width, surface.height) != (w, h) || !surface.configured {
            surface.width = w;
            surface.height = h;
            surface.configured = true;
            surface.dirty = true;
            surface.frame_pending = false;
        }
    }
}

impl SessionLockHandler for Shell {
    fn locked(&mut self, _: &Connection, qh: &QueueHandle<Self>, session_lock: SessionLock) {
        let outputs: Vec<wl_output::WlOutput> = self.output_state.outputs().collect();
        for output in outputs {
            let surface = self.compositor_state.create_surface(qh);
            let lock_surface = session_lock.create_lock_surface(surface, &output, qh);
            self.push_lock_surface(lock_surface, &output);
        }
    }

    fn finished(&mut self, _: &Connection, _: &QueueHandle<Self>, _: SessionLock) {
        // Another lock client holds the lock, or the compositor refused.
        warn!("the session lock was not granted to this lock screen");
        self.exit = Some(1);
    }

    fn configure(&mut self, _: &Connection, _: &QueueHandle<Self>, lock_surface: SessionLockSurface, configure: SessionLockSurfaceConfigure, _serial: u32) {
        let target = lock_surface.wl_surface().clone();
        let Some(index) = self.surface_index(&target) else {
            return;
        };
        let surface = &mut self.surfaces[index];
        let (w, h) = configure.new_size;
        if w == 0 || h == 0 {
            return;
        }
        surface.width = w;
        surface.height = h;
        surface.configured = true;
        surface.dirty = true;
        surface.frame_pending = false;
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

    fn press_key(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_keyboard::WlKeyboard, _: u32, event: KeyEvent) {
        self.key_pressed(event);
    }

    fn release_key(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_keyboard::WlKeyboard, _: u32, _: KeyEvent) {}

    fn update_modifiers(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_keyboard::WlKeyboard, _: u32, modifiers: Modifiers, _: u32) {
        self.modifiers = modifiers;
        if self.mode != Mode::Desktop && self.auth.caps_lock != modifiers.caps_lock {
            self.auth.caps_lock = modifiers.caps_lock;
            self.mark(Role::Auth);
        }
    }
}

impl PointerHandler for Shell {
    fn pointer_frame(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_pointer::WlPointer, events: &[PointerEvent]) {
        const BTN_LEFT: u32 = 0x110;
        for event in events {
            let Some(index) = self.surface_index(&event.surface) else {
                continue;
            };
            let position = (event.position.0 as f32, event.position.1 as f32);
            match event.kind {
                PointerEventKind::Enter { .. } | PointerEventKind::Motion { .. } => {
                    let surface = &mut self.surfaces[index];
                    // Redraw only when the pointer moves onto or off a
                    // control, not for every motion event.
                    let before = surface.pointer.and_then(|p| hit_index(&surface.hits, p));
                    let after = hit_index(&surface.hits, position);
                    surface.pointer = Some(position);
                    if before != after {
                        surface.dirty = true;
                    }
                }
                PointerEventKind::Leave { .. } => {
                    let surface = &mut self.surfaces[index];
                    surface.pointer = None;
                    surface.dirty = true;
                }
                PointerEventKind::Press { button, .. } if button == BTN_LEFT => {
                    let surface = &mut self.surfaces[index];
                    surface.pointer = Some(position);
                    match &surface.hits {
                        Hits::Panel(hits) => {
                            if let Some(action) = action_at(hits, position) {
                                self.panel_action(action);
                            }
                        }
                        Hits::Launcher(hits) => {
                            if let Some(action) = action_at(hits, position) {
                                self.launcher_action(action);
                            }
                        }
                        Hits::Auth(hits) => {
                            if let Some(action) = action_at(hits, position) {
                                self.auth_action(action);
                            }
                        }
                        Hits::None => {}
                    }
                }
                PointerEventKind::Axis { vertical, .. } => {
                    if self.surfaces[index].role == Role::Launcher {
                        let rows = if vertical.discrete != 0 {
                            vertical.discrete
                        } else {
                            (vertical.absolute / 40.0).round() as i32
                        };
                        if rows != 0 && self.launcher.scroll_by(rows) {
                            self.mark(Role::Launcher);
                        }
                    }
                }
                _ => {}
            }
        }
    }
}

/// Index of the topmost hit rectangle under a point, for hover tracking.
fn hit_index(hits: &Hits, point: (f32, f32)) -> Option<usize> {
    fn find<A>(hits: &[(Rect, A)], point: (f32, f32)) -> Option<usize> {
        hits.iter().rposition(|(rect, _)| rect.contains(point))
    }
    match hits {
        Hits::None => None,
        Hits::Panel(hits) => find(hits, point),
        Hits::Launcher(hits) => find(hits, point),
        Hits::Auth(hits) => find(hits, point),
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
delegate_registry!(Shell);
