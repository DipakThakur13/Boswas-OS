//! End to end: the real compositor on its headless back end runs this
//! shell; Print takes a screenshot, and a click on its notification in the
//! notification centre opens the picture with the default application for
//! PNG pictures, and "Open folder" opens the Screenshots folder with the
//! file manager. Both applications are stand-ins on a private
//! `XDG_DATA_DIRS` that write down how they were started.
//!
//! Skipped (with a message) where the compositor was not built next to the
//! shell (`cargo test -p boswas-shell` alone) or no font is installed (the
//! desktop does not start without one).

use std::{
    io::{BufRead, BufReader, Write},
    os::unix::{fs::PermissionsExt, net::UnixStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

use serde_json::{json, Value};

const SIZE: (u32, u32) = (1280, 800);
/// evdev key codes.
const KEY_ENTER: u32 = 28;
const KEY_SYSRQ: u32 = 99;
const BTN_LEFT: u32 = 0x110;

struct Session {
    root: PathBuf,
    child: Child,
    control: BufReader<UnixStream>,
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

    fn key(&mut self, code: u32) {
        for pressed in [true, false] {
            self.call(json!({"cmd": "key", "keycode": code, "pressed": pressed}));
            std::thread::sleep(Duration::from_millis(30));
        }
    }

    fn click(&mut self, x: f64, y: f64) {
        self.call(json!({"cmd": "pointer", "x": x, "y": y}));
        std::thread::sleep(Duration::from_millis(200));
        for pressed in [true, false] {
            self.call(json!({"cmd": "button", "button": BTN_LEFT, "pressed": pressed}));
            std::thread::sleep(Duration::from_millis(30));
        }
    }

    fn log(&self) -> String {
        std::fs::read_to_string(self.root.join("compositor.log")).unwrap_or_default()
    }
}

fn wait_for<T>(seconds: u64, mut probe: impl FnMut() -> Option<T>) -> Option<T> {
    let deadline = Instant::now() + Duration::from_secs(seconds);
    while Instant::now() < deadline {
        if let Some(value) = probe() {
            return Some(value);
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    None
}

fn stand_in(root: &Path, name: &str) -> PathBuf {
    let program = root.join("bin").join(name);
    let record = root.join(format!("{name}.txt"));
    std::fs::create_dir_all(program.parent().unwrap()).unwrap();
    std::fs::write(&program, format!("#!/bin/sh\nprintf '%s\\n' \"$@\" > '{}.part' && mv '{0}.part' '{0}'\n", record.display())).unwrap();
    std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o755)).unwrap();
    program
}

fn start() -> Option<Session> {
    let shell = PathBuf::from(env!("CARGO_BIN_EXE_boswas-shell"));
    let compositor = shell.with_file_name("boswas-compositor");
    if !compositor.is_file() {
        eprintln!("skipped: {} was not built", compositor.display());
        return None;
    }
    // Unix socket paths are short: a short directory under /tmp.
    let root = PathBuf::from(format!("/tmp/bwo{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let applications = root.join("share/applications");
    std::fs::create_dir_all(&applications).unwrap();
    std::fs::create_dir_all(root.join("home/Pictures")).unwrap();
    std::fs::create_dir_all(root.join("run")).unwrap();
    std::fs::set_permissions(root.join("run"), std::fs::Permissions::from_mode(0o700)).unwrap();
    let photos = stand_in(&root, "photos");
    let files = stand_in(&root, "files");
    let entry = |name: &str, exec: String, mime: &str| format!("[Desktop Entry]\nType=Application\nName={name}\nExec={exec}\nMimeType={mime}\n");
    std::fs::write(applications.join("com.boswas.Photos.desktop"), entry("Photos", format!("{} %F", photos.display()), "image/png;image/jpeg;")).unwrap();
    std::fs::write(applications.join("com.boswas.Files.desktop"), entry("Files", format!("{} %U", files.display()), "inode/directory;")).unwrap();
    // Another program that also opens PNG pictures, first by name: only the
    // defaults make Photos the one.
    std::fs::write(applications.join("aaa.viewer.desktop"), entry("A viewer", "/bin/false %f".into(), "image/png;")).unwrap();
    std::fs::write(applications.join("mimeapps.list"), "[Default Applications]\nimage/png=com.boswas.Photos.desktop\ninode/directory=com.boswas.Files.desktop\n").unwrap();
    let config = root.join("compositor.toml");
    std::fs::write(
        &config,
        format!(
            "[policy]\nprivileged_protocols = \"all\"\nlock_idle_max_seconds = 0\n[lock]\nidle_seconds = 0\ncommand = [\"{0}\", \"--lock\"]\n[shell]\ncommand = [\"{0}\"]\n[xwayland]\nenabled = false\n",
            shell.display()
        ),
    )
    .unwrap();
    let mut child = Command::new(&compositor)
        .args(["--backend", "headless", "--headless-size", &format!("{}x{}", SIZE.0, SIZE.1), "--test-input", "--print-env", "--config"])
        .arg(&config)
        .env("XDG_RUNTIME_DIR", root.join("run"))
        .env("HOME", root.join("home"))
        .env("XDG_DATA_DIRS", format!("{}:/usr/share", root.join("share").display()))
        .env("XDG_CURRENT_DESKTOP", "Boswas")
        .env("BOSWAS_LOG", "info")
        .env_remove("XDG_CONFIG_HOME")
        .env_remove("XDG_DATA_HOME")
        .env_remove("XDG_STATE_HOME")
        .env_remove("XDG_CONFIG_DIRS")
        .env_remove("WAYLAND_DISPLAY")
        .env_remove("WAYLAND_SOCKET")
        .env_remove("BOSWAS_SHELL_FAKE_SERVICES")
        .stdout(Stdio::piped())
        .stderr(Stdio::from(std::fs::File::create(root.join("compositor.log")).unwrap()))
        .spawn()
        .expect("the compositor starts");
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut socket = None;
    for _ in 0..2 {
        let mut line = String::new();
        stdout.read_line(&mut line).unwrap();
        if let Some(value) = line.trim_end().strip_prefix("BOSWAS_COMPOSITOR_SOCKET=") {
            socket = Some(PathBuf::from(value));
        }
    }
    // Programs the compositor starts write to this pipe: keep it open.
    std::thread::spawn(move || {
        let mut sink = String::new();
        while stdout.read_line(&mut sink).is_ok_and(|n| n > 0) {
            sink.clear();
        }
    });
    let stream = UnixStream::connect(socket.expect("the control socket")).unwrap();
    stream.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    Some(Session { root, child, control: BufReader::new(stream) })
}

#[test]
fn a_screenshot_opens_from_its_notification() {
    let Some(mut session) = start() else {
        return;
    };
    // Time for the shell to start and draw.
    std::thread::sleep(Duration::from_secs(3));
    if session.log().contains("no usable font") {
        eprintln!("skipped: no font is installed, so the desktop does not start");
        return;
    }
    // Print: the capture view of the whole screen; Enter takes it.
    session.key(KEY_SYSRQ);
    std::thread::sleep(Duration::from_millis(1500));
    session.key(KEY_ENTER);
    let folder = session.root.join("home/Pictures/Screenshots");
    let shot = wait_for(15, || folder.read_dir().ok()?.flatten().map(|e| e.path()).find(|p| p.extension().is_some_and(|e| e == "png")))
        .unwrap_or_else(|| panic!("no screenshot was saved:\n{}", session.log()));
    std::thread::sleep(Duration::from_millis(500));
    // The notification centre (the clock), then the notification: its
    // card is the first under the centre's title, at the top right.
    let (w, h) = (f64::from(SIZE.0), f64::from(SIZE.1));
    session.click(w - 60.0, h - 24.0);
    std::thread::sleep(Duration::from_millis(800));
    let card_x = w - 12.0 - 182.0;
    session.click(card_x, 132.0);
    let record = session.root.join("photos.txt");
    let started = wait_for(10, || std::fs::read_to_string(&record).ok()).unwrap_or_else(|| panic!("Photos was not started:\n{}", session.log()));
    assert_eq!(started.lines().collect::<Vec<_>>(), [shot.to_str().unwrap()], "Photos gets the picture as one argument");
    assert!(session.log().contains("com.boswas.Photos.desktop"));
    // Another screenshot; this time its button opens the folder in Files.
    session.key(KEY_SYSRQ);
    std::thread::sleep(Duration::from_millis(1500));
    session.key(KEY_ENTER);
    wait_for(15, || (folder.read_dir().ok()?.count() >= 2).then_some(())).expect("a second screenshot");
    std::thread::sleep(Duration::from_millis(500));
    session.click(w - 60.0, h - 24.0);
    std::thread::sleep(Duration::from_millis(800));
    // The newest card is first; its button is at the bottom of the card.
    session.click(card_x, 184.0);
    let record = session.root.join("files.txt");
    let started = wait_for(10, || std::fs::read_to_string(&record).ok()).unwrap_or_else(|| panic!("Files was not started:\n{}", session.log()));
    let uri = started.trim_end();
    assert!(uri.starts_with("file:///") && uri.ends_with("/home/Pictures/Screenshots"), "{uri}");
}
