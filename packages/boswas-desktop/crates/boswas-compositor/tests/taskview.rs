//! End-to-end test: Task View keeps what it drew. Its cards (the label,
//! and the highlight of the selected window, which goes while Task View
//! closes) and its strip of desktops are drawn when it first opens and
//! closes; opening and closing it again draws none of them again.

use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
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
use wayland_protocols::xdg::shell::client::{xdg_surface, xdg_toplevel, xdg_wm_base};

const CONFIG: &str = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = []\n[policy]\nlock_idle_max_seconds = 0\nprivileged_protocols = \"all\"\n";

struct Compositor {
    child: Child,
    _stdout: BufReader<std::process::ChildStdout>,
    runtime: tempfile::TempDir,
    display: String,
    control: PathBuf,
}

impl Compositor {
    fn start() -> Self {
        let runtime = tempfile::Builder::new().prefix("bw").tempdir_in("/tmp").unwrap();
        let config_path = runtime.path().join("compositor.toml");
        std::fs::write(&config_path, CONFIG).unwrap();
        let log_file = std::fs::File::create(runtime.path().join("compositor.log")).unwrap();
        let mut child = Command::new(env!("CARGO_BIN_EXE_boswas-compositor"))
            .args(["--backend", "headless", "--headless-size", "1280x800", "--test-input", "--print-env", "--config"])
            .arg(&config_path)
            .env("XDG_RUNTIME_DIR", runtime.path())
            .env("BOSWAS_LOG", "warn,boswas_compositor::stats=trace")
            .env_remove("WAYLAND_DISPLAY")
            .env_remove("WAYLAND_SOCKET")
            .stdout(Stdio::piped())
            .stderr(Stdio::from(log_file))
            .spawn()
            .expect("the compositor starts");
        let mut stdout = BufReader::new(child.stdout.take().unwrap());
        let (mut display, mut control) = (String::new(), PathBuf::new());
        for _ in 0..2 {
            let mut line = String::new();
            stdout.read_line(&mut line).unwrap();
            match line.trim_end().split_once('=') {
                Some(("WAYLAND_DISPLAY", value)) => display = value.to_owned(),
                Some(("BOSWAS_COMPOSITOR_SOCKET", value)) => control = PathBuf::from(value),
                _ => panic!("unexpected line: {line}"),
            }
        }
        Self { child, _stdout: stdout, runtime, display, control }
    }

    fn log(&self) -> String {
        std::fs::read_to_string(self.runtime.path().join("compositor.log")).unwrap_or_default()
    }

    /// Task View's drawings so far: cards, strips.
    fn drawn(&self) -> (usize, usize) {
        let log = self.log();
        (log.matches("task view: a card drawn").count(), log.matches("task view: the strip drawn").count())
    }

    fn frames(&self) -> usize {
        self.log().matches("stats: frame ").count()
    }

    fn command(&self, request: Value) -> Value {
        let mut stream = UnixStream::connect(&self.control).unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        stream.write_all((request.to_string() + "\n").as_bytes()).unwrap();
        let mut reply = String::new();
        BufReader::new(stream).read_line(&mut reply).unwrap();
        let reply: Value = serde_json::from_str(&reply).unwrap();
        assert_eq!(reply["ok"], true, "{request} -> {reply}");
        reply
    }

    /// Waits until the output has been drawn again (a frame after now) and
    /// any transition has run its course.
    fn next_frame(&self) {
        let before = self.frames();
        let deadline = Instant::now() + Duration::from_secs(10);
        while self.frames() == before && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        std::thread::sleep(Duration::from_millis(700));
    }
}

impl Drop for Compositor {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[derive(Default)]
struct State {
    configured: bool,
}

impl Dispatch<wl_registry::WlRegistry, GlobalListContents> for State {
    fn event(_: &mut Self, _: &wl_registry::WlRegistry, _: wl_registry::Event, _: &GlobalListContents, _: &Connection, _: &QueueHandle<Self>) {}
}

impl Dispatch<xdg_wm_base::XdgWmBase, ()> for State {
    fn event(_: &mut Self, base: &xdg_wm_base::XdgWmBase, event: xdg_wm_base::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let xdg_wm_base::Event::Ping { serial } = event {
            base.pong(serial);
        }
    }
}

impl Dispatch<xdg_surface::XdgSurface, ()> for State {
    fn event(state: &mut Self, surface: &xdg_surface::XdgSurface, event: xdg_surface::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let xdg_surface::Event::Configure { serial } = event {
            surface.ack_configure(serial);
            state.configured = true;
        }
    }
}

delegate_noop!(State: ignore wl_compositor::WlCompositor);
delegate_noop!(State: ignore wl_surface::WlSurface);
delegate_noop!(State: ignore wl_shm::WlShm);
delegate_noop!(State: ignore wl_shm_pool::WlShmPool);
delegate_noop!(State: ignore wl_buffer::WlBuffer);
delegate_noop!(State: ignore xdg_toplevel::XdgToplevel);

/// A 320 x 200 window of one colour; kept open while the returned values live.
fn open_window(conn: &Connection, title: &str) -> (wayland_client::EventQueue<State>, State, xdg_toplevel::XdgToplevel, wl_buffer::WlBuffer) {
    let (globals, mut queue) = registry_queue_init::<State>(conn).unwrap();
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
    let mut state = State::default();
    while !state.configured {
        queue.blocking_dispatch(&mut state).unwrap();
    }
    let (w, h) = (320i32, 200i32);
    let mut file = tempfile::tempfile().unwrap();
    for _ in 0..(w * h) {
        file.write_all(&0xFF40_6080u32.to_le_bytes()).unwrap();
    }
    file.flush().unwrap();
    use std::os::fd::AsFd;
    let pool = shm.create_pool(file.as_fd(), w * h * 4, &qh, ());
    let buffer = pool.create_buffer(0, w, h, w * 4, wl_shm::Format::Argb8888, &qh, ());
    surface.attach(Some(&buffer), 0, 0);
    surface.damage_buffer(0, 0, w, h);
    surface.commit();
    queue.roundtrip(&mut state).unwrap();
    (queue, state, toplevel, buffer)
}

#[test]
fn task_view_draws_its_cards_once_and_keeps_them() {
    let compositor = Compositor::start();
    let first_conn = Connection::from_socket(UnixStream::connect(compositor.runtime.path().join(&compositor.display)).unwrap()).unwrap();
    let second_conn = Connection::from_socket(UnixStream::connect(compositor.runtime.path().join(&compositor.display)).unwrap()).unwrap();
    let (mut first_queue, mut first, _first_window, _first_buffer) = open_window(&first_conn, "First");
    let (mut second_queue, mut second, _second_window, _second_buffer) = open_window(&second_conn, "Second");
    first_queue.roundtrip(&mut first).unwrap();
    second_queue.roundtrip(&mut second).unwrap();
    compositor.command(json!({"cmd": "pointer", "x": 5.0, "y": 5.0}));
    compositor.next_frame();
    assert_eq!(compositor.drawn(), (0, 0), "nothing of Task View is drawn before it opens");

    // The first opening draws a card per window and the strip.
    compositor.command(json!({"cmd": "overview", "active": true}));
    compositor.next_frame();
    let (cards, strips) = compositor.drawn();
    assert!(cards >= 2 && strips == 1, "{cards} cards, {strips} strips");
    compositor.command(json!({"cmd": "overview", "active": false}));
    compositor.next_frame();
    let after_first = compositor.drawn();

    // Opened and closed again: nothing new is drawn.
    for _ in 0..2 {
        compositor.command(json!({"cmd": "overview", "active": true}));
        compositor.next_frame();
        compositor.command(json!({"cmd": "overview", "active": false}));
        compositor.next_frame();
    }
    assert_eq!(compositor.drawn(), after_first, "Task View drew again what it had drawn before");
}
