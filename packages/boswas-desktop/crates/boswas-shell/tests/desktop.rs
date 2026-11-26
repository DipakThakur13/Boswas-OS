//! End to end: the real compositor on its headless back end runs this
//! shell with a desktop folder. A click on the desktop gives it the
//! keyboard; Ctrl+Shift+N makes a folder and opens its name for renaming;
//! what is typed renames it; Delete moves it to the Trash (the freedesktop
//! specification); Super+W opens the widgets board, where Tab reaches the
//! first card and Enter opens its application (a stand-in that writes down
//! that it was started). Everything is checked in the files, not on the
//! screen.
//!
//! Skipped (with a message) where the compositor was not built next to the
//! shell (`cargo test -p boswas-shell` alone) or no font is installed.

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
const KEY_ESC: u32 = 1;
const KEY_TAB: u32 = 15;
const KEY_ENTER: u32 = 28;
const KEY_LEFTCTRL: u32 = 29;
const KEY_LEFTSHIFT: u32 = 42;
const KEY_N: u32 = 49;
const KEY_W: u32 = 17;
const KEY_DELETE: u32 = 111;
const KEY_LEFTMETA: u32 = 125;
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

    /// Presses the keys in order and lets them go in the reverse order.
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
        let codes: Vec<(char, u32)> = "qwertyuiop".chars().zip(16..).chain("asdfghjkl".chars().zip(30..)).chain("zxcvbnm".chars().zip(44..)).collect();
        for c in text.chars() {
            let code = codes.iter().find(|(k, _)| *k == c).map(|(_, code)| *code).expect("a letter");
            self.keys(&[code]);
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
    std::fs::write(&program, format!("#!/bin/sh\nprintf '%s\\n' started \"$@\" > '{}.part' && mv '{0}.part' '{0}'\n", record.display())).unwrap();
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
    let root = PathBuf::from(format!("/tmp/bwd{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let home = root.join("home");
    let applications = root.join("share/applications");
    std::fs::create_dir_all(&applications).unwrap();
    std::fs::create_dir_all(home.join("Desktop")).unwrap();
    std::fs::create_dir_all(home.join(".config")).unwrap();
    std::fs::write(home.join(".config/user-dirs.dirs"), "XDG_DESKTOP_DIR=\"$HOME/Desktop\"\n").unwrap();
    std::fs::write(home.join("Desktop/notes.txt"), "a note\n").unwrap();
    std::fs::create_dir_all(root.join("run")).unwrap();
    std::fs::set_permissions(root.join("run"), std::fs::Permissions::from_mode(0o700)).unwrap();
    let weather = stand_in(&root, "weather");
    std::fs::write(applications.join("com.boswas.Weather.desktop"), format!("[Desktop Entry]\nType=Application\nName=Weather\nExec={}\n", weather.display())).unwrap();
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
        .env("HOME", &home)
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
fn a_folder_is_made_renamed_and_trashed_on_the_desktop_and_the_board_opens_an_application() {
    let Some(mut session) = start() else {
        return;
    };
    std::thread::sleep(Duration::from_secs(3));
    if session.log().contains("no usable font") {
        eprintln!("skipped: no font is installed, so the desktop does not start");
        return;
    }
    let desktop = session.root.join("home/Desktop");
    // A click on the empty desktop gives it the keyboard.
    session.click(900.0, 400.0);
    std::thread::sleep(Duration::from_millis(400));
    // Ctrl+Shift+N: a new folder, its name ready to be typed over.
    session.keys(&[KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_N]);
    wait_for(10, || desktop.join("New folder").is_dir().then_some(())).unwrap_or_else(|| panic!("no new folder:\n{}", session.log()));
    std::thread::sleep(Duration::from_millis(800));
    session.type_text("plans");
    session.keys(&[KEY_ENTER]);
    wait_for(10, || desktop.join("plans").is_dir().then_some(())).unwrap_or_else(|| panic!("the folder was not renamed:\n{}", session.log()));
    assert!(!desktop.join("New folder").exists());
    // It is still selected: Delete moves it to the Trash.
    std::thread::sleep(Duration::from_millis(800));
    session.keys(&[KEY_DELETE]);
    let trash = session.root.join("home/.local/share/Trash");
    wait_for(10, || trash.join("files/plans").is_dir().then_some(())).unwrap_or_else(|| panic!("not in the Trash:\n{}", session.log()));
    let info = std::fs::read_to_string(trash.join("info/plans.trashinfo")).unwrap();
    assert!(info.starts_with("[Trash Info]\nPath=/") && info.contains("/home/Desktop/plans\nDeletionDate="), "{info}");
    assert!(!desktop.join("plans").exists());
    assert!(desktop.join("notes.txt").is_file(), "nothing else was touched");
    // Super+W: the widgets board; Tab reaches "Add widgets", then the first
    // card (the weather), and Enter opens Weather.
    session.keys(&[KEY_LEFTMETA, KEY_W]);
    std::thread::sleep(Duration::from_millis(1500));
    session.keys(&[KEY_TAB]);
    session.keys(&[KEY_TAB]);
    session.keys(&[KEY_ENTER]);
    let record = session.root.join("weather.txt");
    let started = wait_for(10, || std::fs::read_to_string(&record).ok()).unwrap_or_else(|| panic!("Weather was not started:\n{}", session.log()));
    assert_eq!(started.trim(), "started");
    // Nothing was arranged: the shell wrote no board of its own.
    session.keys(&[KEY_ESC]);
    assert!(!session.root.join("home/.config/boswas/shell.toml").exists());
}
