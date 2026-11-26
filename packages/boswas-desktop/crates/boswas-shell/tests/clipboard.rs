//! End to end: the real compositor on its headless back end runs this
//! shell, and a small Wayland client (a window that copies, records the
//! keys it gets and reads the clipboard) stands in for an application.
//!
//! * What the window copies shows up in the clipboard history; Super+V,
//!   Down and Enter paste the older item: the window gets Ctrl+V and reads
//!   that item from the clipboard.
//! * What a password manager marks secret is not kept.
//! * Ctrl+P pins an item: it is written to the pinned file (0600).
//! * Super+., typing a name and Enter insert an emoji: the window gets
//!   Ctrl+V and reads the emoji, and then the clipboard holds again what
//!   the window had copied.
//!
//! Skipped (with a message) where the compositor was not built next to the
//! shell (`cargo test -p boswas-shell` alone) or no font is installed.
//!
//! For people to look at: `BOSWAS_TEST_SHOTS=DIR` keeps screenshots of the
//! two flyouts as they were open, `BOSWAS_TEST_SIZE=1366x768` sets the
//! screen and `BOSWAS_TEST_CONFIG` adds lines to the compositor's
//! configuration (`[appearance]` scheme, `[accessibility]` text_scale).

use std::{
    io::{BufRead, BufReader, Read, Write},
    os::{
        fd::AsFd,
        unix::{fs::PermissionsExt, net::UnixStream},
    },
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

use serde_json::{json, Value};
use wayland_client::{
    delegate_noop, event_created_child,
    globals::{registry_queue_init, GlobalListContents},
    protocol::{
        wl_buffer, wl_compositor, wl_data_device, wl_data_device_manager, wl_data_offer, wl_data_source, wl_keyboard,
        wl_registry, wl_seat, wl_shm, wl_shm_pool, wl_surface,
    },
    Connection, Dispatch, EventQueue, QueueHandle, WEnum,
};
use wayland_protocols::xdg::shell::client::{xdg_surface, xdg_toplevel, xdg_wm_base};

/// evdev key codes.
const KEY_LEFTMETA: u32 = 125;
const KEY_LEFTCTRL: u32 = 29;
const KEY_V: u32 = 47;
const KEY_P: u32 = 25;
const KEY_DOT: u32 = 52;
const KEY_DOWN: u32 = 108;
const KEY_ENTER: u32 = 28;
const KEY_ESC: u32 = 1;

struct Session {
    root: PathBuf,
    child: Child,
    control: BufReader<UnixStream>,
    display: String,
}

impl Drop for Session {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

impl Session {
    fn call(&mut self, request: Value) -> Value {
        let stream = self.control.get_mut();
        stream.write_all((request.to_string() + "\n").as_bytes()).unwrap();
        let mut reply = String::new();
        self.control.read_line(&mut reply).unwrap();
        serde_json::from_str(&reply).unwrap()
    }

    /// Presses the keys in order and lets them go in reverse.
    fn keys(&mut self, codes: &[u32]) {
        for code in codes {
            self.call(json!({"cmd": "key", "keycode": code, "pressed": true}));
            std::thread::sleep(Duration::from_millis(20));
        }
        for code in codes.iter().rev() {
            self.call(json!({"cmd": "key", "keycode": code, "pressed": false}));
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn type_text(&mut self, text: &str) {
        let codes: std::collections::HashMap<char, u32> = "qwertyuiop"
            .chars()
            .zip(16..)
            .chain("asdfghjkl".chars().zip(30..))
            .chain("zxcvbnm".chars().zip(44..))
            .collect();
        for c in text.chars() {
            self.keys(&[codes[&c]]);
        }
    }

    fn log(&self) -> String {
        std::fs::read_to_string(self.root.join("compositor.log")).unwrap_or_default()
    }

    /// A screenshot for people to look at, when they asked for them.
    fn shot(&mut self, name: &str) {
        let Some(dir) = std::env::var_os("BOSWAS_TEST_SHOTS").map(PathBuf::from) else {
            return;
        };
        std::fs::create_dir_all(&dir).unwrap();
        let reply = self.call(json!({"cmd": "screenshot", "path": dir.join(format!("{name}.png"))}));
        assert_eq!(reply["ok"], true, "{reply}");
    }
}

fn start() -> Option<Session> {
    let shell = PathBuf::from(env!("CARGO_BIN_EXE_boswas-shell"));
    let compositor = shell.with_file_name("boswas-compositor");
    if !compositor.is_file() {
        eprintln!("skipped: {} was not built", compositor.display());
        return None;
    }
    let root = PathBuf::from(format!("/tmp/bwc{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(root.join("home")).unwrap();
    std::fs::create_dir_all(root.join("run")).unwrap();
    std::fs::set_permissions(root.join("run"), std::fs::Permissions::from_mode(0o700)).unwrap();
    let config = root.join("compositor.toml");
    std::fs::write(
        &config,
        format!(
            "[policy]\nprivileged_protocols = \"all\"\nlock_idle_max_seconds = 0\n[lock]\nidle_seconds = 0\ncommand = [\"{0}\", \"--lock\"]\n[shell]\ncommand = [\"{0}\"]\n[xwayland]\nenabled = false\n{1}\n",
            shell.display(),
            std::env::var("BOSWAS_TEST_CONFIG").unwrap_or_else(|_| "[appearance]\nreduced_motion = true".into()).replace("\\n", "\n"),
        ),
    )
    .unwrap();
    let mut child = Command::new(&compositor)
        .args(["--backend", "headless", "--headless-size", &std::env::var("BOSWAS_TEST_SIZE").unwrap_or_else(|_| "1280x800".into()), "--test-input", "--print-env", "--config"])
        .arg(&config)
        .env("XDG_RUNTIME_DIR", root.join("run"))
        .env("HOME", root.join("home"))
        .env("BOSWAS_LOG", "info")
        .env_remove("XDG_CONFIG_HOME")
        .env_remove("XDG_DATA_HOME")
        .env_remove("XDG_STATE_HOME")
        .env_remove("WAYLAND_DISPLAY")
        .env_remove("WAYLAND_SOCKET")
        .env_remove("BOSWAS_SHELL_FAKE_SERVICES")
        .stdout(Stdio::piped())
        .stderr(Stdio::from(std::fs::File::create(root.join("compositor.log")).unwrap()))
        .spawn()
        .expect("the compositor starts");
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let (mut socket, mut display) = (None, String::new());
    for _ in 0..2 {
        let mut line = String::new();
        stdout.read_line(&mut line).unwrap();
        if let Some(value) = line.trim_end().strip_prefix("BOSWAS_COMPOSITOR_SOCKET=") {
            socket = Some(PathBuf::from(value));
        }
        if let Some(value) = line.trim_end().strip_prefix("WAYLAND_DISPLAY=") {
            display = value.to_owned();
        }
    }
    std::thread::spawn(move || {
        let mut sink = String::new();
        while stdout.read_line(&mut sink).is_ok_and(|n| n > 0) {
            sink.clear();
        }
    });
    let stream = UnixStream::connect(socket.expect("the control socket")).unwrap();
    stream.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    Some(Session { root, child, control: BufReader::new(stream), display })
}

/// The application: one window.
#[derive(Default)]
struct App {
    configured: bool,
    /// The serial of the keyboard's arrival (to copy with).
    entered: Option<u32>,
    keys: Vec<(u32, bool)>,
    selection: Option<wl_data_offer::WlDataOffer>,
    offered: Vec<String>,
}

/// What a copy of the window offers: (type, bytes).
struct Copied(Vec<(String, Vec<u8>)>);

impl Dispatch<wl_registry::WlRegistry, GlobalListContents> for App {
    fn event(_: &mut Self, _: &wl_registry::WlRegistry, _: wl_registry::Event, _: &GlobalListContents, _: &Connection, _: &QueueHandle<Self>) {}
}

impl Dispatch<xdg_wm_base::XdgWmBase, ()> for App {
    fn event(_: &mut Self, base: &xdg_wm_base::XdgWmBase, event: xdg_wm_base::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let xdg_wm_base::Event::Ping { serial } = event {
            base.pong(serial);
        }
    }
}

impl Dispatch<xdg_surface::XdgSurface, ()> for App {
    fn event(app: &mut Self, surface: &xdg_surface::XdgSurface, event: xdg_surface::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let xdg_surface::Event::Configure { serial } = event {
            surface.ack_configure(serial);
            app.configured = true;
        }
    }
}

impl Dispatch<wl_seat::WlSeat, ()> for App {
    fn event(_: &mut Self, seat: &wl_seat::WlSeat, event: wl_seat::Event, _: &(), _: &Connection, qh: &QueueHandle<Self>) {
        if let wl_seat::Event::Capabilities { capabilities: WEnum::Value(caps) } = event {
            if caps.contains(wl_seat::Capability::Keyboard) {
                seat.get_keyboard(qh, ());
            }
        }
    }
}

impl Dispatch<wl_keyboard::WlKeyboard, ()> for App {
    fn event(app: &mut Self, _: &wl_keyboard::WlKeyboard, event: wl_keyboard::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        match event {
            wl_keyboard::Event::Enter { serial, .. } => app.entered = Some(serial),
            wl_keyboard::Event::Leave { .. } => app.entered = None,
            wl_keyboard::Event::Key { key, state: WEnum::Value(state), .. } => app.keys.push((key, state == wl_keyboard::KeyState::Pressed)),
            _ => {}
        }
    }
}

impl Dispatch<wl_data_device::WlDataDevice, ()> for App {
    fn event(app: &mut Self, _: &wl_data_device::WlDataDevice, event: wl_data_device::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let wl_data_device::Event::Selection { id } = event {
            app.selection = id;
            app.offered.clear();
        }
    }

    event_created_child!(App, wl_data_device::WlDataDevice, [
        wl_data_device::EVT_DATA_OFFER_OPCODE => (wl_data_offer::WlDataOffer, ()),
    ]);
}

impl Dispatch<wl_data_offer::WlDataOffer, ()> for App {
    fn event(app: &mut Self, _: &wl_data_offer::WlDataOffer, event: wl_data_offer::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let wl_data_offer::Event::Offer { mime_type } = event {
            app.offered.push(mime_type);
        }
    }
}

impl Dispatch<wl_data_source::WlDataSource, Copied> for App {
    fn event(_: &mut Self, _: &wl_data_source::WlDataSource, event: wl_data_source::Event, copied: &Copied, _: &Connection, _: &QueueHandle<Self>) {
        if let wl_data_source::Event::Send { mime_type, fd } = event {
            if let Some((_, bytes)) = copied.0.iter().find(|(m, _)| *m == mime_type) {
                let _ = std::fs::File::from(fd).write_all(bytes);
            }
        }
    }
}

delegate_noop!(App: ignore wl_compositor::WlCompositor);
delegate_noop!(App: ignore wl_surface::WlSurface);
delegate_noop!(App: ignore wl_shm::WlShm);
delegate_noop!(App: ignore wl_shm_pool::WlShmPool);
delegate_noop!(App: ignore wl_buffer::WlBuffer);
delegate_noop!(App: ignore xdg_toplevel::XdgToplevel);
delegate_noop!(App: ignore wl_data_device_manager::WlDataDeviceManager);

struct Window {
    queue: EventQueue<App>,
    app: App,
    manager: wl_data_device_manager::WlDataDeviceManager,
    device: wl_data_device::WlDataDevice,
}

impl Window {
    fn open(conn: &Connection) -> Self {
        let (globals, mut queue) = registry_queue_init::<App>(conn).unwrap();
        let qh = queue.handle();
        let compositor: wl_compositor::WlCompositor = globals.bind(&qh, 1..=4, ()).unwrap();
        let shm: wl_shm::WlShm = globals.bind(&qh, 1..=1, ()).unwrap();
        let wm: xdg_wm_base::XdgWmBase = globals.bind(&qh, 1..=3, ()).unwrap();
        let seat: wl_seat::WlSeat = globals.bind(&qh, 1..=7, ()).unwrap();
        let manager: wl_data_device_manager::WlDataDeviceManager = globals.bind(&qh, 1..=3, ()).unwrap();
        let device = manager.get_data_device(&seat, &qh, ());
        let surface = compositor.create_surface(&qh, ());
        let xdg = wm.get_xdg_surface(&surface, &qh, ());
        let toplevel = xdg.get_toplevel(&qh, ());
        toplevel.set_title("Editor".into());
        toplevel.set_app_id("com.boswas.TextEditor".into());
        surface.commit();
        let mut app = App::default();
        while !app.configured {
            queue.blocking_dispatch(&mut app).unwrap();
        }
        let (w, h) = (480i32, 320i32);
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(format!("/tmp/bwc-buffer-{}", std::process::id()))
            .unwrap();
        file.set_len((w * h * 4) as u64).unwrap();
        let pool = shm.create_pool(file.as_fd(), w * h * 4, &qh, ());
        let buffer = pool.create_buffer(0, w, h, w * 4, wl_shm::Format::Xrgb8888, &qh, ());
        surface.attach(Some(&buffer), 0, 0);
        surface.commit();
        let _ = std::fs::remove_file(format!("/tmp/bwc-buffer-{}", std::process::id()));
        std::mem::forget((toplevel, xdg, surface, buffer, pool));
        let mut window = Self { queue, app, manager, device };
        window.until("the keyboard", |a| a.entered.is_some());
        window
    }

    fn until(&mut self, what: &str, done: impl Fn(&App) -> bool) {
        let deadline = Instant::now() + Duration::from_secs(15);
        while !done(&self.app) {
            assert!(Instant::now() < deadline, "timed out waiting for {what}");
            self.queue.roundtrip(&mut self.app).unwrap();
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    /// Copies text (with more types, such as a password manager's hint).
    fn copy(&mut self, text: &str, more: &[(&str, &str)]) {
        let qh = self.queue.handle();
        let mut offers = vec![("text/plain;charset=utf-8".to_owned(), text.as_bytes().to_vec())];
        offers.extend(more.iter().map(|(m, v)| ((*m).to_owned(), v.as_bytes().to_vec())));
        let source = self.manager.create_data_source(&qh, Copied(offers.clone()));
        for (mime, _) in &offers {
            source.offer(mime.clone());
        }
        self.device.set_selection(Some(&source), self.app.entered.expect("the keyboard"));
        self.queue.roundtrip(&mut self.app).unwrap();
        // The shell reads it on its own time.
        std::thread::sleep(Duration::from_millis(600));
        self.queue.roundtrip(&mut self.app).unwrap();
    }

    /// What a paste here would insert now.
    fn clipboard(&mut self) -> String {
        self.queue.roundtrip(&mut self.app).unwrap();
        let Some(offer) = self.app.selection.clone() else {
            return String::new();
        };
        let (mut reader, writer) = UnixStream::pair().unwrap();
        offer.receive("text/plain;charset=utf-8".into(), writer.as_fd());
        self.queue.roundtrip(&mut self.app).unwrap();
        drop(writer);
        reader.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        let mut text = String::new();
        let _ = reader.read_to_string(&mut text);
        text
    }

    /// Waits for Ctrl+V (pressed and let go).
    fn wait_for_paste(&mut self) {
        let keys = |a: &App| a.keys.iter().filter(|(k, _)| *k == KEY_LEFTCTRL || *k == KEY_V).count() >= 4;
        self.until("Ctrl+V", keys);
        let pasted: Vec<(u32, bool)> = self.app.keys.iter().copied().filter(|(k, _)| *k == KEY_LEFTCTRL || *k == KEY_V).collect();
        assert_eq!(pasted, [(KEY_LEFTCTRL, true), (KEY_V, true), (KEY_V, false), (KEY_LEFTCTRL, false)]);
    }
}

#[test]
fn copied_items_are_pasted_from_the_history_and_emoji_are_inserted() {
    let Some(mut session) = start() else {
        return;
    };
    std::thread::sleep(Duration::from_secs(3));
    if session.log().contains("no usable font") {
        eprintln!("skipped: no font is installed, so the desktop does not start");
        return;
    }
    let stream = UnixStream::connect(session.root.join("run").join(&session.display)).unwrap();
    let conn = Connection::from_socket(stream).unwrap();
    let mut window = Window::open(&conn);

    // Two copies, and a password a password manager copied.
    window.copy("the first thing copied", &[]);
    window.copy("the second thing copied", &[]);
    window.copy("hunter2", &[("x-kde-passwordManagerHint", "secret")]);
    assert_eq!(window.clipboard(), "hunter2");

    // Super+V, Down, Enter: the item under the newest one. The password is
    // not in the list, so that is the first thing copied.
    session.keys(&[KEY_LEFTMETA, KEY_V]);
    std::thread::sleep(Duration::from_millis(800));
    session.shot("clipboard");
    window.app.keys.clear();
    session.keys(&[KEY_DOWN]);
    session.keys(&[KEY_ENTER]);
    window.wait_for_paste();
    assert_eq!(window.clipboard(), "the first thing copied", "{}", session.log());

    // Pinned with Ctrl+P (it is the newest item now), in a private file.
    session.keys(&[KEY_LEFTMETA, KEY_V]);
    std::thread::sleep(Duration::from_millis(800));
    session.keys(&[KEY_LEFTCTRL, KEY_P]);
    let pinned = session.root.join("home/.local/state/boswas/clipboard-pinned.json");
    let deadline = Instant::now() + Duration::from_secs(5);
    while !pinned.exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(100));
    }
    let text = std::fs::read_to_string(&pinned).unwrap_or_else(|_| panic!("nothing pinned:\n{}", session.log()));
    let value: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(value["items"], json!([{ "kind": "text", "text": "the first thing copied" }]));
    assert!(!text.contains("hunter2"));
    assert_eq!(std::fs::metadata(&pinned).unwrap().permissions().mode() & 0o777, 0o600);
    session.keys(&[KEY_ESC]);
    std::thread::sleep(Duration::from_millis(500));

    // The emoji panel: Super+., a name, Enter.
    window.copy("keep me", &[]);
    window.app.keys.clear();
    session.keys(&[KEY_LEFTMETA, KEY_DOT]);
    std::thread::sleep(Duration::from_millis(800));
    session.shot("emoji");
    session.type_text("grinning");
    std::thread::sleep(Duration::from_millis(300));
    session.shot("emoji-search");
    session.keys(&[KEY_ENTER]);
    window.wait_for_paste();
    assert_eq!(window.clipboard(), "\u{1F600}", "the emoji is what the window pastes");
    // Then the clipboard is what it was.
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut now = String::new();
    while Instant::now() < deadline {
        now = window.clipboard();
        if now == "keep me" {
            break;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    assert_eq!(now, "keep me", "the clipboard is given back");
    // The emoji is remembered as recently used.
    let recent = std::fs::read_to_string(session.root.join("home/.local/state/boswas/emoji.json")).unwrap();
    assert!(recent.contains("\u{1F600}"), "{recent}");
    let log = session.log();
    assert!(!log.contains("panicked"), "{log}");
}
