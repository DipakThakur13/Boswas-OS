//! End-to-end test: a layer surface that shows nothing (one fully
//! transparent pixel of `wp_single_pixel_buffer_v1`, stretched over the
//! output, as the shell puts behind a flyout to take the click that closes
//! it) costs no drawing: its coming adds no damage, so the output is not
//! drawn again for it, and it still takes the pointer. The same surface
//! with a visible colour is drawn.

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
    protocol::{wl_buffer, wl_compositor, wl_pointer, wl_registry, wl_seat, wl_surface},
    Connection, Dispatch, QueueHandle, WEnum,
};
use wayland_protocols::wp::{
    single_pixel_buffer::v1::client::wp_single_pixel_buffer_manager_v1,
    viewporter::client::{wp_viewport, wp_viewporter},
};
use wayland_protocols_wlr::layer_shell::v1::client::{zwlr_layer_shell_v1, zwlr_layer_surface_v1};

const CONFIG: &str = "[shell]\ncommand = []\n[lock]\nidle_seconds = 0\ncommand = []\n[appearance]\nreduced_motion = true\n[policy]\nlock_idle_max_seconds = 0\nprivileged_protocols = \"all\"\n";

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
            .args(["--backend", "headless", "--headless-size", "800x600", "--test-input", "--print-env", "--config"])
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

    /// The shares of the output drawn by each frame so far.
    fn frames(&self) -> Vec<f64> {
        std::fs::read_to_string(self.runtime.path().join("compositor.log"))
            .unwrap_or_default()
            .lines()
            .filter(|line| line.contains("stats: frame "))
            .filter_map(|line| line.rsplit("drawn=").next()?.trim().parse().ok())
            .collect()
    }

    fn command(&self, request: Value) -> Value {
        let mut stream = UnixStream::connect(&self.control).unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        stream.write_all((request.to_string() + "\n").as_bytes()).unwrap();
        let mut reply = String::new();
        BufReader::new(stream).read_line(&mut reply).unwrap();
        serde_json::from_str(&reply).unwrap()
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
    configured: Option<(u32, u32)>,
    /// The surface the pointer entered last.
    entered: Option<wl_surface::WlSurface>,
}

impl Dispatch<wl_registry::WlRegistry, GlobalListContents> for State {
    fn event(_: &mut Self, _: &wl_registry::WlRegistry, _: wl_registry::Event, _: &GlobalListContents, _: &Connection, _: &QueueHandle<Self>) {}
}

impl Dispatch<zwlr_layer_surface_v1::ZwlrLayerSurfaceV1, ()> for State {
    fn event(state: &mut Self, layer: &zwlr_layer_surface_v1::ZwlrLayerSurfaceV1, event: zwlr_layer_surface_v1::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let zwlr_layer_surface_v1::Event::Configure { serial, width, height } = event {
            layer.ack_configure(serial);
            state.configured = Some((width, height));
        }
    }
}

impl Dispatch<wl_seat::WlSeat, ()> for State {
    fn event(_: &mut Self, seat: &wl_seat::WlSeat, event: wl_seat::Event, _: &(), _: &Connection, qh: &QueueHandle<Self>) {
        if let wl_seat::Event::Capabilities { capabilities: WEnum::Value(caps) } = event {
            if caps.contains(wl_seat::Capability::Pointer) {
                seat.get_pointer(qh, ());
            }
        }
    }
}

impl Dispatch<wl_pointer::WlPointer, ()> for State {
    fn event(state: &mut Self, _: &wl_pointer::WlPointer, event: wl_pointer::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let wl_pointer::Event::Enter { surface, .. } = event {
            state.entered = Some(surface);
        }
    }
}

delegate_noop!(State: ignore wl_compositor::WlCompositor);
delegate_noop!(State: ignore wl_surface::WlSurface);
delegate_noop!(State: ignore wl_buffer::WlBuffer);
delegate_noop!(State: ignore zwlr_layer_shell_v1::ZwlrLayerShellV1);
delegate_noop!(State: ignore wp_viewporter::WpViewporter);
delegate_noop!(State: ignore wp_viewport::WpViewport);
delegate_noop!(State: ignore wp_single_pixel_buffer_manager_v1::WpSinglePixelBufferManagerV1);

#[test]
fn a_surface_that_shows_nothing_draws_nothing_and_still_takes_the_pointer() {
    let compositor = Compositor::start();
    let stream = UnixStream::connect(compositor.runtime.path().join(&compositor.display)).unwrap();
    let conn = Connection::from_socket(stream).unwrap();
    let (globals, mut queue) = registry_queue_init::<State>(&conn).unwrap();
    let qh = queue.handle();
    let wl_compositor: wl_compositor::WlCompositor = globals.bind(&qh, 1..=4, ()).unwrap();
    let layer_shell: zwlr_layer_shell_v1::ZwlrLayerShellV1 = globals.bind(&qh, 1..=4, ()).unwrap();
    let viewporter: wp_viewporter::WpViewporter = globals.bind(&qh, 1..=1, ()).unwrap();
    let pixels: wp_single_pixel_buffer_manager_v1::WpSinglePixelBufferManagerV1 = globals.bind(&qh, 1..=1, ()).unwrap();
    let _seat: wl_seat::WlSeat = globals.bind(&qh, 1..=7, ()).unwrap();
    let mut state = State::default();
    queue.roundtrip(&mut state).unwrap();

    // The output is drawn once at start (the background) and then left.
    std::thread::sleep(Duration::from_millis(400));
    let before = compositor.frames();
    assert!(!before.is_empty(), "the output was drawn at start (and frames are logged)");

    let surface = wl_compositor.create_surface(&qh, ());
    let layer = layer_shell.get_layer_surface(&surface, None, zwlr_layer_shell_v1::Layer::Overlay, "test-dismiss".into(), &qh, ());
    use zwlr_layer_surface_v1::Anchor;
    layer.set_anchor(Anchor::Top | Anchor::Bottom | Anchor::Left | Anchor::Right);
    layer.set_size(0, 0);
    layer.set_exclusive_zone(-1);
    surface.commit();
    while state.configured.is_none() {
        queue.blocking_dispatch(&mut state).unwrap();
    }
    let (w, h) = state.configured.unwrap();
    assert_eq!((w, h), (800, 600));
    let viewport = viewporter.get_viewport(&surface, &qh, ());
    viewport.set_destination(w as i32, h as i32);
    let transparent = pixels.create_u32_rgba_buffer(0, 0, 0, 0, &qh, ());
    surface.attach(Some(&transparent), 0, 0);
    surface.damage_buffer(0, 0, 1, 1);
    surface.commit();
    queue.roundtrip(&mut state).unwrap();
    std::thread::sleep(Duration::from_millis(400));
    assert_eq!(compositor.frames(), before, "the invisible surface made the output be drawn again");

    // It takes the pointer all the same.
    assert_eq!(compositor.command(json!({"cmd": "pointer", "x": 400.0, "y": 300.0}))["ok"], true);
    let deadline = Instant::now() + Duration::from_secs(10);
    while state.entered.as_ref() != Some(&surface) && Instant::now() < deadline {
        queue.roundtrip(&mut state).unwrap();
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(state.entered.as_ref(), Some(&surface), "the pointer entered the invisible surface");

    // With a colour that shows (half-transparent black), it is drawn over
    // the whole output.
    let drawn_before = compositor.frames().len();
    let smoke = pixels.create_u32_rgba_buffer(0, 0, 0, u32::MAX / 2, &qh, ());
    surface.attach(Some(&smoke), 0, 0);
    surface.damage_buffer(0, 0, 1, 1);
    surface.commit();
    queue.roundtrip(&mut state).unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while compositor.frames().len() == drawn_before && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    let after = compositor.frames();
    assert!(after[drawn_before..].iter().any(|share| *share > 0.99), "a visible colour is drawn: {:?}", &after[drawn_before..]);
}
