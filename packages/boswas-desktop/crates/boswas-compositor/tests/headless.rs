//! End-to-end tests: the real compositor binary on its headless backend,
//! driven by real Wayland clients and the control socket.
//!
//! What they pin down:
//!
//! * an ordinary client is offered the application protocols and none of
//!   the privileged ones; a program the compositor starts itself is
//! * the public control socket answers `version`, `lock` and
//!   `reload-config` and refuses everything else
//! * a window is mapped, listed, tiled, drawn (checked in a screenshot) and
//!   closed
//! * a locked session stays locked when the lock screen dies, and unlocks
//!   only when a lock client says so
//! * a login screen that keeps failing ends the compositor with the exit
//!   status its start script falls back to the text login on, while a
//!   desktop session stays and tries its shell again with a growing pause
//! * a seat that cannot be opened ends the compositor with the start-up
//!   status and a message that names the causes

use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

use serde_json::{json, Value};
use wayland_client::{
    delegate_noop,
    globals::{registry_queue_init, GlobalListContents},
    protocol::{wl_buffer, wl_compositor, wl_registry, wl_shm, wl_shm_pool, wl_surface},
    Connection, Dispatch, QueueHandle,
};
use wayland_protocols::{
    ext::session_lock::v1::client::{ext_session_lock_manager_v1, ext_session_lock_v1},
    xdg::shell::client::{xdg_surface, xdg_toplevel, xdg_wm_base},
};

const PRIVILEGED: &[&str] = &[
    "zwlr_layer_shell_v1",
    "ext_session_lock_manager_v1",
    "ext_foreign_toplevel_list_v1",
    "zwlr_data_control_manager_v1",
    // Being the input method or a virtual keyboard means seeing or making
    // every key stroke.
    "zwp_input_method_manager_v2",
    "zwp_virtual_keyboard_manager_v1",
];
const APPLICATION: &[&str] = &[
    "wl_compositor",
    "wl_shm",
    "wl_seat",
    "wl_output",
    "wl_data_device_manager",
    "xdg_wm_base",
    "zxdg_decoration_manager_v1",
    "xdg_activation_v1",
    "wp_viewporter",
    "wp_fractional_scale_manager_v1",
    "wp_presentation",
    "wp_cursor_shape_manager_v1",
    "zwp_pointer_constraints_v1",
    "zwp_relative_pointer_manager_v1",
    "zwp_idle_inhibit_manager_v1",
    "wp_security_context_manager_v1",
    "zwp_text_input_manager_v3",
    "ext_idle_notifier_v1",
    "zxdg_exporter_v2",
    "zxdg_importer_v2",
    "wp_alpha_modifier_v1",
    "wp_content_type_manager_v1",
    "zwp_tablet_manager_v2",
    "xdg_system_bell_v1",
];

/// A compositor process with its own runtime directory.
struct Compositor {
    child: Child,
    /// Kept open: programs the compositor starts inherit this pipe as their
    /// standard output and would die writing to a closed one.
    _stdout: BufReader<std::process::ChildStdout>,
    runtime: tempfile::TempDir,
    display: String,
    control: PathBuf,
}

impl Compositor {
    /// Starts the compositor with `config` (TOML). `role` names the helper
    /// (see `trusted_helper`) that programs started by the compositor run.
    fn start(config: &str, role: &str) -> Self {
        // Unix socket paths are short; keep the directory name short too.
        let runtime = tempfile::Builder::new().prefix("bw").tempdir_in("/tmp").unwrap();
        let config_path = runtime.path().join("compositor.toml");
        std::fs::write(&config_path, config.replace("@HELPER@", &helper_command())).unwrap();
        let log = std::fs::File::create(runtime.path().join("compositor.log")).unwrap();
        let mut child = Command::new(env!("CARGO_BIN_EXE_boswas-compositor"))
            .args(["--backend", "headless", "--headless-size", "800x600", "--test-input", "--print-env"])
            .arg("--config")
            .arg(&config_path)
            .env("XDG_RUNTIME_DIR", runtime.path())
            .env("BOSWAS_TEST_ROLE", role)
            .env("BOSWAS_TEST_OUT", runtime.path().join("helper.out"))
            .env_remove("WAYLAND_DISPLAY")
            .env_remove("WAYLAND_SOCKET")
            .env_remove("BOSWAS_LOG")
            .stdout(Stdio::piped())
            .stderr(Stdio::from(log))
            .spawn()
            .expect("the compositor starts");
        let mut stdout = BufReader::new(child.stdout.take().unwrap());
        let mut display = String::new();
        let mut control = PathBuf::new();
        for _ in 0..2 {
            let mut line = String::new();
            stdout.read_line(&mut line).expect("the compositor prints its environment");
            match line.trim_end().split_once('=') {
                Some(("WAYLAND_DISPLAY", value)) => display = value.to_owned(),
                Some(("BOSWAS_COMPOSITOR_SOCKET", value)) => control = PathBuf::from(value),
                _ => panic!("unexpected line: {line}"),
            }
        }
        Self {
            child,
            _stdout: stdout,
            runtime,
            display,
            control,
        }
    }

    fn wayland(&self) -> Connection {
        let stream = UnixStream::connect(self.runtime.path().join(&self.display)).expect("the Wayland socket");
        Connection::from_socket(stream).unwrap()
    }

    fn control(&self) -> Control {
        Control::connect(&self.control)
    }

    fn helper_output(&self) -> String {
        std::fs::read_to_string(self.runtime.path().join("helper.out")).unwrap_or_default()
    }

    /// What the compositor wrote to standard error so far.
    fn log(&self) -> String {
        std::fs::read_to_string(self.runtime.path().join("compositor.log")).unwrap_or_default()
    }

    /// Waits for the compositor to end by itself and returns its exit
    /// status.
    fn wait_for_exit(&mut self) -> Option<i32> {
        wait_for("the compositor to end", || self.child.try_wait().unwrap()).code()
    }
}

/// The lines the compositor logged as errors.
fn error_lines(log: &str) -> Vec<&str> {
    log.lines().filter(|line| line.contains(" ERROR ")).collect()
}

impl Drop for Compositor {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// A control-socket connection.
struct Control {
    reader: BufReader<UnixStream>,
}

impl Control {
    fn connect(path: &Path) -> Self {
        let stream = UnixStream::connect(path).expect("the control socket");
        stream.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        Self {
            reader: BufReader::new(stream),
        }
    }

    fn raw(&mut self, line: &str) -> Option<Value> {
        self.reader.get_mut().write_all(line.as_bytes()).ok()?;
        let mut reply = String::new();
        self.reader.read_line(&mut reply).ok().filter(|n| *n > 0)?;
        serde_json::from_str(&reply).ok()
    }

    fn call(&mut self, request: Value) -> Value {
        self.raw(&(request.to_string() + "\n")).expect("a reply")
    }

    fn ok(&mut self, request: Value) -> Value {
        let reply = self.call(request.clone());
        assert_eq!(reply["ok"], true, "{request} -> {reply}");
        reply["data"].clone()
    }
}

fn wait_for<T>(what: &str, mut probe: impl FnMut() -> Option<T>) -> T {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(value) = probe() {
            return value;
        }
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// This test binary, run as a helper program: `trusted_helper` below.
fn helper_command() -> String {
    let exe = std::env::current_exe().unwrap();
    format!("[{:?}, \"trusted_helper\", \"--exact\", \"--ignored\", \"--nocapture\"]", exe.to_str().unwrap())
}

// ---------------------------------------------------------------------------
// A minimal Wayland client
// ---------------------------------------------------------------------------

#[derive(Default)]
struct Client {
    configured: bool,
    closed: bool,
    locked: Option<bool>,
}

impl Dispatch<wl_registry::WlRegistry, GlobalListContents> for Client {
    fn event(_: &mut Self, _: &wl_registry::WlRegistry, _: wl_registry::Event, _: &GlobalListContents, _: &Connection, _: &QueueHandle<Self>) {}
}

impl Dispatch<xdg_wm_base::XdgWmBase, ()> for Client {
    fn event(_: &mut Self, base: &xdg_wm_base::XdgWmBase, event: xdg_wm_base::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let xdg_wm_base::Event::Ping { serial } = event {
            base.pong(serial);
        }
    }
}

impl Dispatch<xdg_surface::XdgSurface, ()> for Client {
    fn event(state: &mut Self, surface: &xdg_surface::XdgSurface, event: xdg_surface::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let xdg_surface::Event::Configure { serial } = event {
            surface.ack_configure(serial);
            state.configured = true;
        }
    }
}

impl Dispatch<xdg_toplevel::XdgToplevel, ()> for Client {
    fn event(state: &mut Self, _: &xdg_toplevel::XdgToplevel, event: xdg_toplevel::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let xdg_toplevel::Event::Close = event {
            state.closed = true;
        }
    }
}

impl Dispatch<ext_session_lock_v1::ExtSessionLockV1, ()> for Client {
    fn event(state: &mut Self, _: &ext_session_lock_v1::ExtSessionLockV1, event: ext_session_lock_v1::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        match event {
            ext_session_lock_v1::Event::Locked => state.locked = Some(true),
            ext_session_lock_v1::Event::Finished => state.locked = Some(false),
            _ => {}
        }
    }
}

delegate_noop!(Client: ignore wl_compositor::WlCompositor);
delegate_noop!(Client: ignore wl_surface::WlSurface);
delegate_noop!(Client: ignore wl_shm::WlShm);
delegate_noop!(Client: ignore wl_shm_pool::WlShmPool);
delegate_noop!(Client: ignore wl_buffer::WlBuffer);
delegate_noop!(Client: ignore ext_session_lock_manager_v1::ExtSessionLockManagerV1);

/// The interfaces a connection is offered.
fn globals_of(conn: &Connection) -> Vec<String> {
    let (globals, _queue) = registry_queue_init::<Client>(conn).unwrap();
    globals.contents().with_list(|list| list.iter().map(|g| g.interface.clone()).collect())
}

/// Opens a 320x200 toplevel filled with one opaque colour (ARGB).
fn open_window(conn: &Connection, title: &str, argb: u32) -> (wayland_client::EventQueue<Client>, Client, xdg_toplevel::XdgToplevel) {
    let (globals, mut queue) = registry_queue_init::<Client>(conn).unwrap();
    let qh = queue.handle();
    let compositor: wl_compositor::WlCompositor = globals.bind(&qh, 1..=4, ()).unwrap();
    let shm: wl_shm::WlShm = globals.bind(&qh, 1..=1, ()).unwrap();
    let wm: xdg_wm_base::XdgWmBase = globals.bind(&qh, 1..=3, ()).unwrap();
    let surface = compositor.create_surface(&qh, ());
    let xdg = wm.get_xdg_surface(&surface, &qh, ());
    let toplevel = xdg.get_toplevel(&qh, ());
    toplevel.set_title(title.into());
    toplevel.set_app_id("com.boswas.test".into());
    surface.commit();
    let mut client = Client::default();
    while !client.configured {
        queue.blocking_dispatch(&mut client).unwrap();
    }
    let (w, h) = (320i32, 200i32);
    let mut file = tempfile::tempfile().unwrap();
    for _ in 0..(w * h) {
        file.write_all(&argb.to_le_bytes()).unwrap();
    }
    file.flush().unwrap();
    use std::os::fd::AsFd;
    let pool = shm.create_pool(file.as_fd(), w * h * 4, &qh, ());
    let buffer = pool.create_buffer(0, w, h, w * 4, wl_shm::Format::Argb8888, &qh, ());
    surface.attach(Some(&buffer), 0, 0);
    surface.damage_buffer(0, 0, w, h);
    surface.commit();
    queue.roundtrip(&mut client).unwrap();
    (queue, client, toplevel)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

const LOCKED_DOWN: &str = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = []\n[policy]\nlock_idle_max_seconds = 0\n";

#[test]
fn ordinary_clients_get_application_protocols_only() {
    let compositor = Compositor::start(LOCKED_DOWN, "none");
    let globals = globals_of(&compositor.wayland());
    for interface in APPLICATION {
        assert!(globals.iter().any(|g| g == interface), "{interface} is missing from {globals:?}");
    }
    for interface in PRIVILEGED {
        assert!(!globals.iter().any(|g| g == interface), "{interface} is offered to an ordinary client");
    }
}

#[test]
fn the_public_control_socket_is_limited() {
    let compositor = Compositor::start(LOCKED_DOWN, "none");
    let mut control = compositor.control();
    let version = control.ok(json!({"cmd": "version"}));
    assert_eq!(version["name"], "boswas-compositor");
    assert_eq!(version["trusted"], false);
    assert_eq!(version["locked"], false);
    for cmd in [
        "windows", "subscribe", "workspaces", "quit", "screenshot", "thumbnail", "activate", "close", "tile",
        "overview", "show-desktop", "night-light", "next-layout", "new-workspace", "close-workspace",
        "move-to-workspace", "pointer", "key", "button",
    ] {
        let reply = control.call(json!({"cmd": cmd, "id": 7, "path": "/tmp/boswas-test-shot.png", "window": 1}));
        assert_eq!(reply["ok"], false, "{cmd} must be refused on a public connection: {reply}");
        assert_eq!(reply["id"], 7);
    }
    control.ok(json!({"cmd": "reload-config"}));
    // The monitors are no secret, and Settings needs their modes.
    let outputs = control.ok(json!({"cmd": "outputs"}));
    assert_eq!(outputs[0]["name"], "HEADLESS-1");
    assert_eq!((outputs[0]["width"].as_i64(), outputs[0]["height"].as_i64()), (Some(800), Some(600)));
    assert_eq!(outputs[0]["modes"][0]["preferred"], true);
    assert_eq!(outputs[0]["mode"]["refresh"], 60_000, "60 Hz without --headless-refresh");
    // The headless output has no variable refresh rate to offer.
    assert_eq!(outputs[0]["adaptive_sync"], json!({ "supported": false, "enabled": false }));
    // Starting the capture screen is allowed (the user confirms there);
    // without a shell there is nobody to show it, and nothing is captured.
    control.ok(json!({"cmd": "capture"}));
    for garbage in ["not json\n", "[1, 2]\n", "{}\n", "{\"cmd\": 5}\n", "\"version\"\n"] {
        let reply = control.raw(garbage).expect("garbage gets an error reply, not a hang-up");
        assert_eq!(reply["ok"], false, "{garbage:?}");
    }
    // Still alive and answering after the garbage.
    control.ok(json!({"cmd": "version"}));
    // An endless line is cut off instead of being buffered.
    let mut flood = compositor.control();
    let chunk = "x".repeat(8192);
    let mut refused = false;
    for _ in 0..64 {
        if flood.reader.get_mut().write_all(chunk.as_bytes()).is_err() {
            refused = true;
            break;
        }
    }
    let mut line = String::new();
    let ended = matches!(flood.reader.read_line(&mut line), Ok(0) | Err(_));
    assert!(refused || ended, "an oversized request must close the connection");
    compositor.control().ok(json!({"cmd": "version"}));
}

#[test]
fn a_program_the_compositor_starts_is_trusted() {
    let config = "[shell]\ncommand = @HELPER@\n[lock]\nidle_seconds = 0\ncommand = []\n[policy]\nlock_idle_max_seconds = 0\n";
    let compositor = Compositor::start(config, "shell");
    let report = wait_for("the helper's report", || {
        let text = compositor.helper_output();
        text.contains("done").then_some(text)
    });
    for interface in PRIVILEGED {
        assert!(report.contains(&format!("global {interface}\n")), "{interface} is not offered to the shell:\n{report}");
    }
    assert!(report.contains("ipc version trusted=true"), "{report}");
    assert!(report.contains("ipc windows ok=true"), "{report}");
    assert!(report.contains("ipc subscribe ok=true"), "{report}");
    // The same compositor still refuses the public socket.
    let reply = compositor.control().call(json!({"cmd": "windows"}));
    assert_eq!(reply["ok"], false);
    // And an ordinary client still sees no privileged global.
    let globals = globals_of(&compositor.wayland());
    assert!(!globals.iter().any(|g| PRIVILEGED.contains(&g.as_str())), "{globals:?}");
}

#[test]
fn a_window_is_mapped_listed_tiled_drawn_and_closed() {
    // Development policy: the public control socket is trusted, so the test
    // can drive the window manager and take screenshots.
    let config = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = []\n[policy]\nlock_idle_max_seconds = 0\nprivileged_protocols = \"all\"\n";
    let compositor = Compositor::start(config, "none");
    let mut control = compositor.control();
    assert_eq!(control.ok(json!({"cmd": "version"}))["trusted"], true);
    assert_eq!(control.ok(json!({"cmd": "windows"})), json!([]));

    let conn = compositor.wayland();
    let (mut queue, mut client, _toplevel) = open_window(&conn, "Integration test", 0xFF00_C000);
    let window = wait_for("the window in the list", || {
        control.ok(json!({"cmd": "windows"})).as_array().and_then(|list| list.first().cloned())
    });
    assert_eq!(window["title"], "Integration test");
    assert_eq!(window["app_id"], "com.boswas.test");
    assert_eq!(window["active"], true);
    assert_eq!(window["tile"], "floating");
    assert_eq!((window["width"].as_i64(), window["height"].as_i64()), (Some(320), Some(200)));
    let (x, y) = (window["x"].as_i64().unwrap(), window["y"].as_i64().unwrap());
    assert!((0..800 - 320).contains(&x) && (0..600 - 200).contains(&y), "placed on screen: {x},{y}");

    // The screenshot shows the client's pixels where the window is, once
    // the window has finished appearing (it opens with a short animation,
    // during which it is translucent).
    let shot = compositor.runtime.path().join("shot.png");
    let screenshot = |control: &mut Control| -> Vec<u8> {
        let reply = control.ok(json!({"cmd": "screenshot", "path": shot}));
        assert_eq!((reply["width"].as_i64(), reply["height"].as_i64()), (Some(800), Some(600)));
        let decoder = png::Decoder::new(std::io::BufReader::new(std::fs::File::open(&shot).unwrap()));
        let mut reader = decoder.read_info().unwrap();
        let mut pixels = vec![0u8; reader.output_buffer_size()];
        reader.next_frame(&mut pixels).unwrap();
        pixels
    };
    let pixel = |pixels: &[u8], px: i64, py: i64| {
        let i = ((py * 800 + px) * 4) as usize;
        (pixels[i], pixels[i + 1], pixels[i + 2])
    };
    let pixels = wait_for("the window to be fully shown", || {
        let pixels = screenshot(&mut control);
        (pixel(&pixels, x + 160, y + 100) == (0x00, 0xC0, 0x00)).then_some(pixels)
    });
    let corner = pixel(&pixels, 799, 599);
    assert_ne!(corner, (0x00, 0xC0, 0x00));
    assert!(corner.0 < 48 && corner.1 < 48 && corner.2 < 60, "the background is the dark theme colour: {corner:?}");

    // Snap to the left half through the control socket.
    let id = window["id"].clone();
    control.ok(json!({"cmd": "tile", "window": id, "tile": "left"}));
    let tiled = wait_for("the window to be tiled", || {
        queue.roundtrip(&mut client).ok()?;
        let w = control.ok(json!({"cmd": "windows"}))[0].clone();
        (w["tile"] == "left").then_some(w)
    });
    assert_eq!((tiled["x"].as_i64(), tiled["y"].as_i64()), (Some(0), Some(0)));
    assert_eq!(control.call(json!({"cmd": "tile", "window": id, "tile": "sideways"}))["ok"], false);
    assert_eq!(control.call(json!({"cmd": "activate", "window": 999}))["ok"], false);

    // A thumbnail of the window: scaled down, never up, with its own colour.
    let thumb = compositor.runtime.path().join("thumb.png");
    let reply = control.ok(json!({"cmd": "thumbnail", "window": id, "path": thumb, "width": 100}));
    assert_eq!(reply["width"], 100);
    let decoder = png::Decoder::new(std::io::BufReader::new(std::fs::File::open(&thumb).unwrap()));
    let mut reader = decoder.read_info().unwrap();
    let mut small = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut small).unwrap();
    assert_eq!(info.width, 100);
    assert_eq!(i64::from(info.height), reply["height"].as_i64().unwrap());
    let middle = ((info.height / 2 * info.width + info.width / 2) * 4) as usize;
    assert_eq!(&small[middle..middle + 4], &[0x00, 0xC0, 0x00, 0xFF]);
    assert_eq!(control.call(json!({"cmd": "thumbnail", "window": 999, "path": thumb}))["ok"], false);
    assert_eq!(control.call(json!({"cmd": "thumbnail", "window": id, "path": "relative.png"}))["ok"], false);

    // Desktops: a session starts with one; the window stays on it.
    assert_eq!(control.ok(json!({"cmd": "workspaces"})), json!({"active": 0, "count": 1}));
    assert_eq!(control.ok(json!({"cmd": "workspace", "index": 1}))["active"], 0, "there is no second desktop yet");
    assert_eq!(control.ok(json!({"cmd": "new-workspace"}))["count"], 2);
    assert_eq!(control.ok(json!({"cmd": "workspace", "index": 1}))["active"], 1);
    assert_eq!(control.ok(json!({"cmd": "windows"}))[0]["workspace"], 0);
    assert_eq!(control.ok(json!({"cmd": "workspace", "index": 99}))["active"], 1, "an unknown desktop is ignored");
    // Closing the first desktop moves its window to the one that remains.
    control.ok(json!({"cmd": "move-to-workspace", "window": id, "index": 1}));
    assert_eq!(control.ok(json!({"cmd": "windows"}))[0]["workspace"], 1);
    control.ok(json!({"cmd": "move-to-workspace", "window": id, "index": 0}));
    assert_eq!(control.ok(json!({"cmd": "close-workspace", "index": 0})), json!({"active": 0, "count": 1}));
    assert_eq!(control.ok(json!({"cmd": "windows"}))[0]["workspace"], 0);
    assert_eq!(control.ok(json!({"cmd": "close-workspace", "index": 0}))["count"], 1, "the last desktop stays");

    // Show desktop hides the window and brings it back.
    assert_eq!(control.ok(json!({"cmd": "show-desktop"}))["active"], true);
    assert_eq!(control.ok(json!({"cmd": "windows"}))[0]["minimized"], true);
    assert_eq!(control.ok(json!({"cmd": "show-desktop"}))["active"], false);
    let restored = control.ok(json!({"cmd": "windows"}))[0].clone();
    assert_eq!((restored["minimized"].clone(), restored["active"].clone()), (json!(false), json!(true)));

    // Night light and the keyboard layout are reported and switchable.
    let night = control.ok(json!({"cmd": "night-light", "enabled": true, "temperature": 3000}));
    assert_eq!((night["enabled"].clone(), night["temperature"].clone()), (json!(true), json!(3000)));
    assert_eq!(control.ok(json!({"cmd": "night-light", "enabled": false}))["enabled"], false);
    assert_eq!(control.ok(json!({"cmd": "next-layout"}))["active"], 0, "one layout: nothing to switch to");

    // Closing asks the client; when it goes, the list is empty again.
    control.ok(json!({"cmd": "close", "window": id}));
    wait_for("the close request", || {
        queue.roundtrip(&mut client).ok()?;
        client.closed.then_some(())
    });
    drop((queue, conn));
    wait_for("the window to disappear", || {
        (control.ok(json!({"cmd": "windows"})) == json!([])).then_some(())
    });
}

#[test]
fn the_input_method_gets_its_two_protocols_and_no_other_privilege() {
    let config = "[shell]\ncommand = []\n[input_method]\ncommand = @HELPER@\n[lock]\nidle_seconds = 0\ncommand = []\n[policy]\nlock_idle_max_seconds = 0\n";
    // The helper reports what it is offered, as it does for the shell.
    let compositor = Compositor::start(config, "shell");
    let report = wait_for("the helper's report", || {
        let text = compositor.helper_output();
        text.contains("done").then_some(text)
    });
    for interface in ["zwp_input_method_manager_v2", "zwp_virtual_keyboard_manager_v1"] {
        assert!(report.contains(&format!("global {interface}\n")), "{interface} is not offered to the input method:\n{report}");
    }
    for interface in ["zwlr_layer_shell_v1", "ext_session_lock_manager_v1", "ext_foreign_toplevel_list_v1", "zwlr_data_control_manager_v1"] {
        assert!(!report.contains(&format!("global {interface}\n")), "{interface} is offered to the input method:\n{report}");
    }
    // Its control connection is what the public socket is: no window list,
    // no events.
    assert!(report.contains("ipc version trusted=false"), "{report}");
    assert!(report.contains("ipc windows ok=false"), "{report}");
    assert!(report.contains("ipc subscribe ok=false"), "{report}");
}

/// Presses or releases a key by its evdev code.
fn key(control: &mut Control, code: u32, pressed: bool) {
    control.ok(json!({"cmd": "key", "keycode": code, "pressed": pressed}));
}

/// A full key stroke.
fn tap(control: &mut Control, code: u32) {
    key(control, code, true);
    key(control, code, false);
}

const KEY_ESC: u32 = 1;
const KEY_TAB: u32 = 15;
const KEY_ENTER: u32 = 28;
const KEY_LEFTALT: u32 = 56;
const KEY_LEFTMETA: u32 = 125;
const KEY_LEFT: u32 = 105;
const KEY_UP: u32 = 103;
const KEY_DOWN: u32 = 108;

#[test]
fn the_switcher_task_view_and_snap_keys_manage_windows() {
    let config = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = []\n[appearance]\nreduced_motion = true\n[policy]\nlock_idle_max_seconds = 0\nprivileged_protocols = \"all\"\n";
    let compositor = Compositor::start(config, "none");
    let mut control = compositor.control();
    let conn = compositor.wayland();
    let (mut first_queue, mut first_client, _first) = open_window(&conn, "First", 0xFF40_4040);
    let second_conn = compositor.wayland();
    let (mut second_queue, mut second_client, _second) = open_window(&second_conn, "Second", 0xFF80_8080);
    let mut pump = |control: &mut Control| {
        first_queue.roundtrip(&mut first_client).unwrap();
        second_queue.roundtrip(&mut second_client).unwrap();
        control.ok(json!({"cmd": "windows"}))
    };
    let active = |windows: &Value| -> String {
        windows
            .as_array()
            .unwrap()
            .iter()
            .find(|w| w["active"] == true)
            .map(|w| w["title"].as_str().unwrap().to_owned())
            .unwrap_or_default()
    };
    wait_for("both windows", || (pump(&mut control).as_array().unwrap().len() == 2).then_some(()));
    assert_eq!(active(&pump(&mut control)), "Second", "the newest window has the keyboard");

    // Alt+Tab goes to the window used before; nothing changes until Alt is
    // released, and Escape leaves everything as it was.
    key(&mut control, KEY_LEFTALT, true);
    tap(&mut control, KEY_TAB);
    assert_eq!(active(&pump(&mut control)), "Second", "still choosing");
    key(&mut control, KEY_LEFTALT, false);
    assert_eq!(active(&pump(&mut control)), "First");
    key(&mut control, KEY_LEFTALT, true);
    tap(&mut control, KEY_TAB);
    tap(&mut control, KEY_ESC);
    key(&mut control, KEY_LEFTALT, false);
    assert_eq!(active(&pump(&mut control)), "First", "Escape cancels the switcher");
    // Two steps come back to where it started.
    key(&mut control, KEY_LEFTALT, true);
    tap(&mut control, KEY_TAB);
    tap(&mut control, KEY_TAB);
    key(&mut control, KEY_LEFTALT, false);
    assert_eq!(active(&pump(&mut control)), "First");

    // Task View: Super+Tab opens it, applications lose the keyboard, Tab
    // moves the selection and Enter goes to the selected window.
    key(&mut control, KEY_LEFTMETA, true);
    tap(&mut control, KEY_TAB);
    key(&mut control, KEY_LEFTMETA, false);
    assert_eq!(control.ok(json!({"cmd": "overview"}))["active"], false, "the command toggled it off again");
    assert_eq!(control.ok(json!({"cmd": "overview", "active": true}))["active"], true);
    assert_eq!(active(&pump(&mut control)), "", "no application has the keyboard in Task View");
    // One frame, so Task View knows where its cells are.
    let shot = compositor.runtime.path().join("task-view.png");
    control.ok(json!({"cmd": "screenshot", "path": shot}));
    tap(&mut control, KEY_TAB);
    tap(&mut control, KEY_ENTER);
    assert_eq!(control.ok(json!({"cmd": "overview", "active": false}))["active"], false);
    assert_eq!(active(&pump(&mut control)), "Second", "Tab then Enter chose the other window");
    // Escape leaves Task View with the window that was active.
    control.ok(json!({"cmd": "overview", "active": true}));
    tap(&mut control, KEY_ESC);
    assert_eq!(active(&pump(&mut control)), "Second");

    // Super+arrows: left half, upper-left quarter, maximized, and back down.
    let tile = |control: &mut Control, windows: Value| -> String {
        let _ = control;
        windows.as_array().unwrap().iter().find(|w| w["title"] == "Second").unwrap()["tile"].as_str().unwrap().to_owned()
    };
    let press = |control: &mut Control, code: u32| {
        key(control, KEY_LEFTMETA, true);
        tap(control, code);
        // Another key in between, so releasing Super is not a tap (Start).
        key(control, KEY_LEFTMETA, false);
    };
    for (code, expected) in [(KEY_LEFT, "left"), (KEY_UP, "top-left"), (KEY_UP, "maximized"), (KEY_DOWN, "floating")] {
        press(&mut control, code);
        let windows = pump(&mut control);
        assert_eq!(tile(&mut control, windows), expected);
    }
    press(&mut control, KEY_DOWN);
    let windows = pump(&mut control);
    let second = windows.as_array().unwrap().iter().find(|w| w["title"] == "Second").unwrap().clone();
    assert_eq!(second["minimized"], true, "Super+Down on a floating window minimizes it");
    assert_eq!(active(&windows), "First");
}

const KEY_SPACE: u32 = 57;
const KEY_RIGHT: u32 = 106;
const KEY_LEFTCTRL: u32 = 29;

#[test]
fn the_window_menu_maximizes_restores_and_moves_with_the_keyboard() {
    let config = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = []\n[appearance]\nreduced_motion = true\n[policy]\nlock_idle_max_seconds = 0\nprivileged_protocols = \"all\"\n";
    let compositor = Compositor::start(config, "none");
    let mut control = compositor.control();
    let conn = compositor.wayland();
    let (mut queue, mut client, _toplevel) = open_window(&conn, "Menu", 0xFF40_4040);
    let mut window = |control: &mut Control| -> Option<Value> {
        queue.roundtrip(&mut client).unwrap();
        control.ok(json!({"cmd": "windows"})).as_array().unwrap().first().cloned()
    };
    wait_for("the window", || window(&mut control));
    let alt_space = |control: &mut Control| {
        key(control, KEY_LEFTALT, true);
        tap(control, KEY_SPACE);
        key(control, KEY_LEFTALT, false);
    };

    // A floating window's menu starts at Move (Restore is not available):
    // three rows down is Maximize.
    alt_space(&mut control);
    for _ in 0..3 {
        tap(&mut control, KEY_DOWN);
    }
    tap(&mut control, KEY_ENTER);
    assert_eq!(window(&mut control).unwrap()["tile"], "maximized");
    // The menu's picture is drawn without trouble.
    alt_space(&mut control);
    let shot = compositor.runtime.path().join("window-menu.png");
    control.ok(json!({"cmd": "screenshot", "path": shot}));
    // Now Restore is the first row.
    tap(&mut control, KEY_ENTER);
    assert_eq!(window(&mut control).unwrap()["tile"], "floating");

    // Escape closes the menu and nothing happens; Enter then belongs to
    // the application again.
    alt_space(&mut control);
    tap(&mut control, KEY_ESC);
    tap(&mut control, KEY_ENTER);
    let before = window(&mut control).unwrap();
    assert_eq!(before["tile"], "floating");
    assert_eq!(before["minimized"], false);

    // Move: the arrow keys move the window, Ctrl for single pixels, and
    // Enter finishes.
    alt_space(&mut control);
    tap(&mut control, KEY_ENTER);
    tap(&mut control, KEY_RIGHT);
    tap(&mut control, KEY_DOWN);
    key(&mut control, KEY_LEFTCTRL, true);
    tap(&mut control, KEY_LEFT);
    key(&mut control, KEY_LEFTCTRL, false);
    tap(&mut control, KEY_ENTER);
    let after = window(&mut control).unwrap();
    assert_eq!(after["x"].as_i64().unwrap() - before["x"].as_i64().unwrap(), 23);
    assert_eq!(after["y"].as_i64().unwrap() - before["y"].as_i64().unwrap(), 24);
    tap(&mut control, KEY_RIGHT);
    assert_eq!(window(&mut control).unwrap()["x"], after["x"], "the move is over");

    // Minimize from the menu: Move, Size, Minimize.
    alt_space(&mut control);
    tap(&mut control, KEY_DOWN);
    tap(&mut control, KEY_DOWN);
    tap(&mut control, KEY_ENTER);
    assert_eq!(window(&mut control).unwrap()["minimized"], true);
}

/// Whether a program is on the PATH.
fn installed(program: &str) -> bool {
    std::env::var_os("PATH")
        .is_some_and(|path| std::env::split_paths(&path).any(|dir| dir.join(program).is_file()))
}

impl Control {
    /// Reads until the event `name` arrives and returns its data; replies
    /// and other events in between are skipped.
    fn event(&mut self, name: &str) -> Value {
        loop {
            let mut line = String::new();
            let read = self.reader.read_line(&mut line).unwrap_or_else(|err| panic!("no \"{name}\" event: {err}"));
            assert!(read > 0, "the connection closed before \"{name}\"");
            let message: Value = serde_json::from_str(&line).expect("a JSON line");
            if message["event"] == name {
                return message["data"].clone();
            }
        }
    }

    /// Sends a request without waiting for its reply.
    fn send(&mut self, request: Value) {
        self.reader.get_mut().write_all((request.to_string() + "\n").as_bytes()).unwrap();
    }
}

/// Runs grim (a screenshot tool that speaks wlr-screencopy) against a
/// compositor; returns whether it got its picture and where it is.
fn grim(compositor: &Compositor, name: &str, args: &[&str]) -> std::thread::JoinHandle<(bool, PathBuf)> {
    let path = compositor.runtime.path().join(name);
    let mut command = Command::new("grim");
    command
        .args(args)
        .arg(&path)
        .env("XDG_RUNTIME_DIR", compositor.runtime.path())
        .env("WAYLAND_DISPLAY", &compositor.display)
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    std::thread::spawn(move || (command.status().expect("grim runs").success(), path))
}

/// Width and height from a PNG file's header.
fn png_size(path: &Path) -> (u32, u32) {
    let bytes = std::fs::read(path).expect("the picture");
    assert_eq!(&bytes[..8], b"\x89PNG\r\n\x1a\n");
    let number = |at: usize| u32::from_be_bytes(bytes[at..at + 4].try_into().unwrap());
    (number(16), number(20))
}

const CAPTURE_CONFIG: &str = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = []\n[appearance]\nreduced_motion = true\n[policy]\nlock_idle_max_seconds = 0\nprivileged_protocols = \"all\"\n";

#[test]
fn a_program_sees_the_screen_only_after_the_user_allowed_it() {
    if !installed("grim") {
        eprintln!("skipped: grim is not installed");
        return;
    }
    let compositor = Compositor::start(CAPTURE_CONFIG, "none");

    // Without a shell nobody can ask the user: the answer is no.
    let (ok, path) = grim(&compositor, "nobody.png", &[]).join().unwrap();
    assert!(!ok && !path.exists(), "no capture without a way to ask");

    let mut shell = compositor.control();
    let state = shell.ok(json!({"cmd": "subscribe"}));
    assert_eq!(state["capturing"]["programs"], json!([]));

    // Allowed: the picture has the output's size, and the shell is told
    // who is capturing.
    let shot = grim(&compositor, "allowed.png", &[]);
    let request = shell.event("capture-request");
    assert_eq!(request["program"], "grim");
    shell.send(json!({"cmd": "capture-answer", "request": request["id"], "allow": true}));
    assert_eq!(shell.event("capturing")["programs"], json!(["grim"]));
    let (ok, path) = shot.join().unwrap();
    assert!(ok);
    assert_eq!(png_size(&path), (800, 600));

    // Every run of the tool is asked about again. A region is cut out of
    // the output, with the pointer when that is wanted.
    let shot = grim(&compositor, "region.png", &["-c", "-g", "10,20 200x100"]);
    let request = shell.event("capture-request");
    shell.send(json!({"cmd": "capture-answer", "request": request["id"], "allow": true}));
    let (ok, path) = shot.join().unwrap();
    assert!(ok);
    assert_eq!(png_size(&path), (200, 100));

    // Refused: no picture, and the same tool is not asked about again at
    // once (the next question would have the next id; there is none).
    let shot = grim(&compositor, "refused.png", &[]);
    let request = shell.event("capture-request");
    let asked = request["id"].as_u64().unwrap();
    shell.send(json!({"cmd": "capture-answer", "request": asked, "allow": false}));
    let (ok, path) = shot.join().unwrap();
    assert!(!ok && !path.exists());
    let (ok, _) = grim(&compositor, "again.png", &[]).join().unwrap();
    assert!(!ok, "a refusal holds for a while");
    let mut other = compositor.control();
    let reply = other.call(json!({"cmd": "capture-answer", "request": asked + 1, "allow": true}));
    assert_eq!(reply["ok"], false, "no second question was asked: {reply}");
}

#[test]
fn nothing_is_captured_while_the_session_is_locked() {
    if !installed("grim") {
        eprintln!("skipped: grim is not installed");
        return;
    }
    let compositor = Compositor::start(CAPTURE_CONFIG, "none");
    let mut shell = compositor.control();
    shell.ok(json!({"cmd": "subscribe"}));
    let mut control = compositor.control();
    control.ok(json!({"cmd": "lock"}));
    let (ok, path) = grim(&compositor, "locked.png", &[]).join().unwrap();
    assert!(!ok && !path.exists());
    let reply = control.call(json!({"cmd": "capture-answer", "request": 1, "allow": true}));
    assert_eq!(reply["ok"], false, "the user is not even asked: {reply}");
}

#[test]
fn an_x11_application_gets_a_window_like_any_other() {
    // Needs the XWayland server and one small X11 program; without them
    // (a minimal build environment) there is nothing to test.
    if !installed("Xwayland") || !installed("xclock") {
        eprintln!("skipped: Xwayland or xclock is not installed");
        return;
    }
    let config = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = []\n[apps]\nfiles = [\"xclock\"]\n[appearance]\nreduced_motion = true\n[policy]\nlock_idle_max_seconds = 0\nprivileged_protocols = \"all\"\n";
    let compositor = Compositor::start(config, "none");
    let mut control = compositor.control();
    // Super+E starts the "files" program, here an X11 clock. The compositor
    // gives it DISPLAY; XWayland comes up when the clock connects.
    const KEY_E: u32 = 18;
    key(&mut control, KEY_LEFTMETA, true);
    tap(&mut control, KEY_E);
    key(&mut control, KEY_LEFTMETA, false);
    let window = wait_for("the X11 window", || {
        control
            .ok(json!({"cmd": "windows"}))
            .as_array()
            .and_then(|list| list.first().cloned())
            .filter(|w| w["active"] == true)
    });
    assert_eq!(window["x11"], true);
    assert_eq!(window["app_id"], "XClock", "the X11 class is the application id");
    assert_eq!(window["tile"], "floating");
    let (width, height) = (window["width"].as_i64().unwrap(), window["height"].as_i64().unwrap());
    assert!(width >= 100 && height >= 132, "the frame adds a title bar: {width}x{height}");

    // It is managed like every window: maximized through the control
    // socket it fills the screen, and closing it removes it.
    let id = window["id"].clone();
    control.ok(json!({"cmd": "tile", "window": id, "tile": "maximized"}));
    let maximized = wait_for("the X11 window to be maximized", || {
        let w = control.ok(json!({"cmd": "windows"}))[0].clone();
        (w["tile"] == "maximized").then_some(w)
    });
    assert_eq!((maximized["width"].as_i64(), maximized["height"].as_i64()), (Some(800), Some(600)));
    let shot = compositor.runtime.path().join("x11.png");
    control.ok(json!({"cmd": "screenshot", "path": shot}));
    control.ok(json!({"cmd": "close", "window": id}));
    wait_for("the X11 window to close", || {
        (control.ok(json!({"cmd": "windows"})) == json!([])).then_some(())
    });
}

#[test]
fn a_locked_session_stays_locked_when_the_lock_screen_dies() {
    // The lock program takes the lock and exits without unlocking, every
    // time it is started.
    let config = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = @HELPER@\n[policy]\nlock_idle_max_seconds = 0\n";
    let compositor = Compositor::start(config, "lock-and-die");
    let mut control = compositor.control();
    assert_eq!(control.ok(json!({"cmd": "version"}))["locked"], false);
    control.ok(json!({"cmd": "lock"}));
    // From the reply on, and across at least two crashed lock screens, the
    // session is never unlocked.
    let started = Instant::now();
    let mut samples = 0;
    while compositor.helper_output().matches("locked").count() < 2 {
        assert_eq!(control.ok(json!({"cmd": "version"}))["locked"], true, "unlocked after {:?}", started.elapsed());
        samples += 1;
        assert!(started.elapsed() < Duration::from_secs(30), "the lock screen was not restarted");
        std::thread::sleep(Duration::from_millis(100));
    }
    assert!(samples > 10, "only {samples} samples");
    assert_eq!(control.ok(json!({"cmd": "version"}))["locked"], true);
    assert!(!compositor.helper_output().contains("finished"), "a new lock screen must be able to take over");
}

#[test]
fn only_a_lock_client_unlocks_the_session() {
    let config = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = @HELPER@\n[policy]\nlock_idle_max_seconds = 0\n";
    let compositor = Compositor::start(config, "lock-and-unlock");
    let mut control = compositor.control();
    control.ok(json!({"cmd": "lock"}));
    assert_eq!(control.ok(json!({"cmd": "version"}))["locked"], true, "locked before the lock screen is even up");
    wait_for("the lock screen to unlock", || {
        (control.ok(json!({"cmd": "version"}))["locked"] == false).then_some(())
    });
    assert!(compositor.helper_output().contains("locked"));
}

/// The rest of a configuration whose shell is under test.
const NO_LOCK: &str = "[lock]\nidle_seconds = 0\ncommand = []\n[policy]\nlock_idle_max_seconds = 0\n";

#[test]
fn a_login_screen_that_keeps_failing_ends_the_compositor_with_its_own_status() {
    // The shell is the login screen (`--greeter`) and ends at once.
    let config = format!("[shell]\ncommand = [\"/bin/sh\", \"-c\", \"exit 7\", \"--greeter\"]\n{NO_LOCK}");
    let mut compositor = Compositor::start(&config, "none");
    // (Within the patience of `wait_for`: three starts and one pause.)
    assert_eq!(compositor.wait_for_exit(), Some(4), "{}", compositor.log());
    let log = compositor.log();
    let errors = error_lines(&log);
    assert_eq!(errors.len(), 1, "one line says why: {log}");
    for part in ["the login screen (/bin/sh -c exit 7 --greeter) ended 3 times in a row", "exit status: 7", "text login"] {
        assert!(errors[0].contains(part), "{part}: {}", errors[0]);
    }
    assert_eq!(log.matches("started the shell").count(), 3, "{log}");
    assert!(!compositor.control.exists(), "the control socket is removed on the way out");
}

#[test]
fn a_login_screen_that_cannot_be_started_ends_the_compositor_too() {
    let config = format!("[shell]\ncommand = [\"/nonexistent/boswas-shell\", \"--greeter\"]\n{NO_LOCK}");
    let mut compositor = Compositor::start(&config, "none");
    assert_eq!(compositor.wait_for_exit(), Some(4), "{}", compositor.log());
    let log = compositor.log();
    let errors = error_lines(&log);
    assert_eq!(errors.len(), 1, "{log}");
    assert!(errors[0].contains("it could not be started: /nonexistent/boswas-shell"), "{}", errors[0]);
}

#[test]
fn a_desktop_session_stays_when_its_shell_keeps_failing() {
    let config = format!("[shell]\ncommand = [\"/bin/sh\", \"-c\", \"exit 7\"]\n{NO_LOCK}");
    let mut compositor = Compositor::start(&config, "none");
    // The pauses grow: 2 s after the second failed start, 4 s after the third.
    for pause in ["tried again in 2 s", "tried again in 4 s"] {
        wait_for(pause, || compositor.log().contains(pause).then_some(()));
    }
    let log = compositor.log();
    assert!(log.contains("ended 3 times in a row right after it started (last: exit status: 7); the session stays"), "{log}");
    assert!(error_lines(&log).is_empty(), "{log}");
    // The session is still there for its applications.
    assert!(compositor.child.try_wait().unwrap().is_none(), "the compositor ended: {log}");
    let conn = compositor.wayland();
    let (mut queue, mut client, _toplevel) = open_window(&conn, "Still here", 0xff20_4060);
    let mut control = compositor.control();
    wait_for("the window", || {
        queue.roundtrip(&mut client).unwrap();
        control.raw("{\"cmd\":\"version\"}\n").filter(|reply| reply["ok"] == true)
    });
}

#[test]
fn a_shell_that_says_quit_and_leaves_ends_the_compositor_normally() {
    // What the login screen does after a successful login: it asks the
    // compositor to end and is gone before the compositor has read that.
    let config = format!("[shell]\ncommand = @HELPER@\n{NO_LOCK}");
    let mut compositor = Compositor::start(&config, "quit-and-leave");
    assert_eq!(compositor.wait_for_exit(), Some(0), "{}", compositor.log());
    let log = compositor.log();
    assert_eq!(log.matches("started the shell").count(), 1, "the shell is not started again: {log}");
    assert!(log.contains("boswas-compositor stopped"), "{log}");
    assert!(error_lines(&log).is_empty(), "{log}");
}

#[test]
fn the_login_screen_runs_no_x_server() {
    // What the shell is given as its X display, in a desktop session and on
    // the login screen.
    let display_of = |greeter: &str| {
        let script = "echo display=${DISPLAY:-none} > \\\"$BOSWAS_TEST_OUT\\\"; exec sleep 30";
        let config = format!("[shell]\ncommand = [\"/bin/sh\", \"-c\", \"{script}\"{greeter}]\n{NO_LOCK}");
        let compositor = Compositor::start(&config, "none");
        let output = wait_for("the shell", || Some(compositor.helper_output()).filter(|out| out.contains("display=")));
        (output.trim().to_owned(), compositor.log())
    };
    let (display, log) = display_of(", \"--greeter\"");
    assert_eq!(display, "display=none", "{log}");
    assert!(!log.contains("XWayland"), "the X server is not even looked for: {log}");
    if installed("Xwayland") {
        let (display, log) = display_of("");
        assert!(display.starts_with("display=:"), "{display}: {log}");
    }
}

#[cfg(feature = "udev")]
#[test]
fn without_a_seat_the_compositor_ends_with_the_start_up_status_and_says_why() {
    // libseat is told to use a seat daemon that is not there, so no seat
    // can be opened wherever this test runs.
    let runtime = tempfile::Builder::new().prefix("bw").tempdir_in("/tmp").unwrap();
    let config = runtime.path().join("compositor.toml");
    std::fs::write(&config, "[shell]\ncommand = []\n").unwrap();
    let started = Instant::now();
    let output = Command::new(env!("CARGO_BIN_EXE_boswas-compositor"))
        .args(["--backend", "udev", "--config"])
        .arg(&config)
        .env("XDG_RUNTIME_DIR", runtime.path())
        .env("LIBSEAT_BACKEND", "seatd")
        .env("SEATD_SOCK", runtime.path().join("no-seatd.sock"))
        .env_remove("WAYLAND_DISPLAY")
        .env_remove("WAYLAND_SOCKET")
        .env_remove("BOSWAS_LOG")
        .stdin(Stdio::null())
        .output()
        .expect("the compositor starts");
    let log = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(3), "{log}");
    assert!(started.elapsed() < Duration::from_secs(15), "it does not wait for a seat that cannot come: {:?}", started.elapsed());
    let errors = error_lines(&log);
    assert_eq!(errors.len(), 1, "one line says why: {log}");
    for part in ["no usable seat: no seat could be opened", "virtual terminal", "polkit is not running", "another display server"] {
        assert!(errors[0].contains(part), "{part}: {}", errors[0]);
    }
    // What libseat itself had to say is in the log, and quoted in the reason.
    assert!(log.lines().any(|line| line.contains(" libseat: ")), "{log}");
    assert!(errors[0].contains("libseat reported: "), "{}", errors[0]);
}

/// Not a test of its own: the body of the helper program the compositor
/// starts in the tests above (this binary with `--ignored --exact`).
#[test]
#[ignore = "helper program for the tests above"]
fn trusted_helper() {
    let Ok(role) = std::env::var("BOSWAS_TEST_ROLE") else {
        return;
    };
    let out = PathBuf::from(std::env::var("BOSWAS_TEST_OUT").unwrap());
    let report = |line: &str| {
        let mut file = std::fs::OpenOptions::new().create(true).append(true).open(&out).unwrap();
        writeln!(file, "{line}").unwrap();
    };
    // The trusted Wayland connection the compositor handed over.
    let conn = Connection::connect_to_env().expect("WAYLAND_SOCKET");
    match role.as_str() {
        "shell" => {
            for interface in globals_of(&conn) {
                report(&format!("global {interface}"));
            }
            use std::os::fd::FromRawFd;
            let fd: i32 = std::env::var("BOSWAS_COMPOSITOR_FD").unwrap().parse().unwrap();
            // SAFETY: the descriptor was passed to this process by the
            // compositor for exactly this use.
            let stream = unsafe { UnixStream::from_raw_fd(fd) };
            stream.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
            let mut control = Control {
                reader: BufReader::new(stream),
            };
            let version = control.call(json!({"cmd": "version"}));
            report(&format!("ipc version trusted={}", version["data"]["trusted"]));
            for cmd in ["windows", "subscribe"] {
                report(&format!("ipc {cmd} ok={}", control.call(json!({"cmd": cmd}))["ok"]));
            }
            report("done");
            // Stay alive like a shell would; the compositor is killed by the test.
            std::thread::sleep(Duration::from_secs(60));
        }
        "quit-and-leave" => {
            use std::os::fd::FromRawFd;
            let fd: i32 = std::env::var("BOSWAS_COMPOSITOR_FD").unwrap().parse().unwrap();
            // SAFETY: the descriptor was passed to this process by the
            // compositor for exactly this use.
            let mut stream = unsafe { UnixStream::from_raw_fd(fd) };
            stream.write_all(b"{\"cmd\":\"quit\"}\n").unwrap();
            // Closed at once: the request and the end of the connection
            // reach the compositor together.
            drop(stream);
            std::thread::sleep(Duration::from_millis(300));
        }
        "lock-and-die" | "lock-and-unlock" => {
            let (globals, mut queue) = registry_queue_init::<Client>(&conn).unwrap();
            let qh = queue.handle();
            let manager: ext_session_lock_manager_v1::ExtSessionLockManagerV1 = globals.bind(&qh, 1..=1, ()).unwrap();
            let lock = manager.lock(&qh, ());
            let mut client = Client::default();
            while client.locked.is_none() {
                queue.blocking_dispatch(&mut client).unwrap();
            }
            report(if client.locked == Some(true) { "locked" } else { "finished" });
            if role == "lock-and-unlock" && client.locked == Some(true) {
                std::thread::sleep(Duration::from_millis(300));
                lock.unlock_and_destroy();
                queue.roundtrip(&mut client).unwrap();
            }
            // "lock-and-die": exit here, holding the lock, like a crash.
        }
        _ => {}
    }
}
