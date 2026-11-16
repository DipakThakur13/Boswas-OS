//! The daemon end to end, in headless mode: control protocol, rendering,
//! passphrase entry through a pseudo-terminal, the Plymouth-compatible
//! client, start-up and clean-up, and the sandbox.

mod common;

use std::os::unix::net::UnixDatagram;
use std::process::{Command, Stdio};
use std::time::Duration;

use boswas_bootsplash::assets;
use boswas_bootsplash::gfx::Rect;
use boswas_bootsplash::proto::{self, Request, Response};
use boswas_bootsplash::scene::Mode;
use common::*;

fn prompt_shown(frame: &Frame) -> bool {
    frame.prompt_shown()
}

fn ring_shown(frame: &Frame) -> bool {
    frame.ring_shown()
}

/// The boot screen without a prompt: the ring, and no text box.
fn ring_alone(frame: &Frame) -> bool {
    frame.ring_shown() && !frame.prompt_shown()
}

const REJECTED: &str = "cryptsetup: ERROR: sda3_crypt: cryptsetup failed, bad password or options?";

#[test]
fn control_requests_change_the_picture() {
    let mut splash = Splash::start("control", "1280x720", &["--static"]);
    splash.ok(Request::Ping);
    let frame = splash.snapshot();
    let l = frame.layout();
    let screen = Rect::new(0, 0, 1280, 720);
    assert_eq!((frame.width, frame.height), (1280, 720));
    assert_eq!(frame.pixel(3, 3), assets::BACKGROUND);
    // The boot screen: the mark, the ring, and nothing else.
    assert!(frame.ink(&l.mark) > (l.mark.area() / 6) as usize, "the mark is drawn");
    assert!(ring_shown(&frame));
    assert_eq!(frame.ink(&screen), frame.ink(&l.mark) + frame.ink(&l.spinner), "no wordmark, no text");

    // A message is one line under the ring, and goes away again.
    splash.ok(Request::Message { text: "cryptsetup: sda3_crypt: set up successfully".into() });
    let frame = splash.snapshot();
    let message = frame.ink(&l.boot_message);
    assert!(message > 300, "an informational message is shown");
    assert_eq!(frame.tinted(&l.boot_message, assets::DANGER), 0);
    splash.ok(Request::Message { text: "cryptsetup: ERROR: sda3_crypt: maximum number of tries exceeded".into() });
    let frame = splash.snapshot();
    assert!(frame.tinted(&l.boot_message, assets::DANGER) > 200, "an error message is in the danger colour");
    splash.ok(Request::Message { text: String::new() });
    assert_eq!(splash.snapshot().ink(&l.boot_message), 0);

    // Progress is a percentage on the same line.
    splash.ok(Request::Progress(60));
    let percentage = splash.snapshot().ink(&l.boot_message);
    assert!(percentage > 30 && percentage < message / 3, "three characters, \"60%\": {percentage}");

    // Shutdown: the ring and its line in the middle of the screen, no mark.
    splash.ok(Request::Mode(Mode::Shutdown));
    let frame = splash.snapshot();
    assert!(frame.ink(&l.status) > 200, "shutdown shows its status line");
    assert!(frame.count(&l.busy_spinner, assets::TEXT) >= 6, "and the ring above it");
    let drawn = frame.ink(&l.busy_spinner) + frame.ink(&l.status) + frame.ink(&l.busy_message);
    assert_eq!(frame.ink(&screen), drawn, "and nothing else");
    assert!(frame.ink(&l.busy_spinner) < 200, "five dots, not the mark");
    splash.ok(Request::Mode(Mode::Reboot));
    assert_ne!(splash.snapshot().ink(&l.status), frame.ink(&l.status), "restart says something else");
    splash.ok(Request::Mode(Mode::Boot));
    let frame = splash.snapshot();
    assert_eq!(frame.ink(&l.status), 0);
    assert!(frame.ink(&l.mark) > (l.mark.area() / 6) as usize && ring_shown(&frame));

    splash.ok(Request::Quit { retain: None });
    let status = splash.wait_exit(Duration::from_secs(5)).expect("the daemon exits when told to quit");
    assert!(status.success());
    assert!(!exists(&splash.socket), "the socket is removed");
    assert!(!exists(&splash.socket.with_file_name("pid")), "the pid file is removed");
}

#[test]
fn the_animation_advances_and_stays_inside_the_ring() {
    let mut splash = Splash::start("animate", "1024x768", &[]);
    let ring = splash.snapshot().layout().spinner;
    assert!(ring.area() < 48 * 48, "the ring is small: {ring:?}");
    // A second and a half of the ring: whatever changes between two frames
    // is inside its rectangle, and it does change.
    let mut last = splash.snapshot();
    let mut moved = 0;
    for _ in 0..15 {
        std::thread::sleep(Duration::from_millis(100));
        let frame = splash.snapshot();
        let changed = frame.changed(&last);
        assert!(ring.contains(&changed), "pixels changed outside the ring: {changed:?}");
        moved += usize::from(!changed.is_empty());
        last = frame;
    }
    assert!(moved >= 10, "the dots moved in {moved} of 15 frames");
    // Nothing but the mark and the dots is on screen.
    let l = last.layout();
    assert_eq!(last.ink(&Rect::new(0, 0, 1024, 768)), last.ink(&l.mark) + last.ink(&ring));
}

#[test]
fn malformed_requests_do_not_hurt_the_daemon() {
    let splash = Splash::start("malformed", "800x600", &["--static"]);
    // Unknown verb, bad argument: an error reply.
    let reply = splash.raw(&proto::frame(b"EXEC /bin/sh").unwrap()).unwrap();
    assert!(reply.starts_with(b"ERR"));
    let reply = splash.raw(&proto::frame(b"PROGRESS 500").unwrap()).unwrap();
    assert!(reply.starts_with(b"ERR"));
    let reply = splash.raw(&proto::frame(b"SNAPSHOT ../../etc/x").unwrap()).unwrap();
    assert!(reply.starts_with(b"ERR"));
    // Impossible lengths: the connection is closed without an answer.
    assert!(splash.raw(&[0xff, 0xff, 1, 2, 3]).is_none());
    assert!(splash.raw(&[0, 0]).is_none());
    // Random bytes on many connections.
    let mut seed = 0x1234_5678_9abc_def0u64;
    for _ in 0..200 {
        let bytes: Vec<u8> = (0..(seed % 300) as usize)
            .map(|_| {
                seed ^= seed << 13;
                seed ^= seed >> 7;
                seed ^= seed << 17;
                seed as u8
            })
            .collect();
        seed = seed.wrapping_add(0x9e37_79b9_7f4a_7c15);
        if let Ok(mut stream) = std::os::unix::net::UnixStream::connect(&splash.socket) {
            use std::io::Write;
            let _ = stream.write_all(&bytes);
        }
    }
    // A client that connects and says nothing, and one that sends half a frame.
    let _idle = std::os::unix::net::UnixStream::connect(&splash.socket).unwrap();
    let mut half = std::os::unix::net::UnixStream::connect(&splash.socket).unwrap();
    std::io::Write::write_all(&mut half, &[0, 10, b'P']).unwrap();
    splash.ok(Request::Ping);
    splash.ok(Request::Message { text: "still here".into() });
}

#[test]
fn passphrase_through_the_plymouth_compatible_client() {
    let mut splash = Splash::start("plymouth", "1366x768", &[]);
    // What /lib/cryptsetup/askpass does: `plymouth --ping`, then
    // `plymouth ask-for-password --prompt PROMPT` with stdout on a pipe,
    // reading until end of file.
    assert!(splash.plymouth().arg("--ping").status().unwrap().success());
    let ask = splash
        .plymouth()
        .args(["ask-for-password", "--prompt", "Please unlock disk sda3_crypt"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let frame = splash.wait_for("the prompt", prompt_shown);
    let l = frame.layout();
    assert!(frame.ink(&l.title) > 300, "the title is shown");
    assert!(frame.ink(&l.detail) > 100, "the name of the disk is shown");
    assert!(frame.count(&l.text_box, assets::ACCENT) > 600, "the text box has the accent line of a focused control");
    assert!(frame.ink(&l.prompt_line) > 100, "the hint is shown");
    assert!(frame.ink(&l.mark) > (l.mark.area() / 6) as usize, "the mark stays");
    assert_eq!(frame.ink(&l.spinner), 0, "the ring gives way to the prompt");
    assert!(matches!(frame.typed(), Some((0, _))), "an empty box: {:?}", frame.typed());

    // Typing shows dots, never characters; editing keys work.
    let dots = |n: usize| move |f: &Frame| matches!(f.typed(), Some((typed, _)) if typed == n);
    splash.pty.type_keys(b"wrong");
    splash.wait_for("five dots", dots(5));
    splash.pty.type_keys(b"\x7f\x7f");
    splash.wait_for("three dots", dots(3));
    splash.pty.type_keys(b"\x15");
    splash.wait_for("an empty box", dots(0));
    // The caret blinks: both halves of the blink are seen.
    splash.wait_for("the caret", |f| f.typed() == Some((0, true)));
    splash.wait_for("the caret to go out", |f| f.typed() == Some((0, false)));
    // Arrow keys and other sequences are not part of the passphrase.
    splash.pty.type_keys(b"\x1b[A");
    splash.pty.type_keys("correct h\u{f6}rse \u{20ac}".as_bytes());
    splash.pty.type_keys(b"\r");

    let (code, stdout) = wait_child(ask, Duration::from_secs(5));
    assert_eq!(code, Some(0));
    assert_eq!(stdout, "correct h\u{f6}rse \u{20ac}".as_bytes(), "exactly the passphrase, no newline");
    splash.wait_for("the ring again", ring_alone);

    // Messages and the other commands scripts use.
    assert!(splash
        .plymouth()
        .args(["message", "--text=cryptsetup: sda3_crypt: set up successfully"])
        .status()
        .unwrap()
        .success());
    splash.wait_for("the message", |f| f.ink(&f.layout().boot_message) > 300);
    assert!(splash.plymouth().args(["display-message", "--text="]).status().unwrap().success());
    assert!(splash.plymouth().args(["update", "--status=x"]).status().unwrap().success());
    assert!(!splash.plymouth().arg("frobnicate").stderr(Stdio::null()).status().unwrap().success());
    assert!(splash.plymouth().arg("quit").status().unwrap().success());
    assert!(splash.wait_exit(Duration::from_secs(5)).unwrap().success());
    // No daemon: the ping fails, which is what sends askpass to the console.
    assert_eq!(splash.plymouth().arg("--ping").status().unwrap().code(), Some(1));
    let ask = splash.plymouth().args(["ask-for-password", "--prompt", "x"]).stdout(Stdio::piped()).spawn().unwrap();
    let (code, stdout) = wait_child(ask, Duration::from_secs(5));
    assert_eq!((code, stdout.len()), (Some(1), 0));
}

#[test]
fn a_rejected_passphrase_is_reported_and_the_picture_never_shows_what_was_typed() {
    // Without animation every frame is the same for the same state, so
    // frames can be compared pixel for pixel.
    let mut splash = Splash::start("rejected", "1366x768", &["--static"]);
    let ask = |splash: &Splash| {
        splash
            .plymouth()
            .args(["ask-for-password", "--prompt", "Please unlock disk sda3_crypt"])
            .stdout(Stdio::piped())
            .spawn()
            .unwrap()
    };
    let first = ask(&splash);
    let empty = splash.wait_for("the prompt", prompt_shown);
    let l = empty.layout();
    assert_eq!(empty.typed(), Some((0, true)));
    assert_eq!(empty.ink(&l.prompt_second_line), 0, "one line under the box: the hint");

    // Five characters are five dots, whatever the characters are: the
    // frame for one passphrase is the frame for any other of that length.
    splash.pty.type_keys(b"wrong");
    let wrong = splash.wait_for("five dots", |f| f.typed() == Some((5, true)));
    assert!(l.text_box.contains(&wrong.changed(&empty)), "typing changes nothing outside the box");
    splash.pty.type_keys(b"\x15");
    splash.wait_for("an empty box", |f| f.typed() == Some((0, true)));
    let other = "MW@\u{e9}\u{20ac}";
    splash.pty.type_keys(other.as_bytes());
    let frame = splash.wait_for("five dots again", |f| f.typed() == Some((5, true)));
    assert!(frame.rgb == wrong.rgb, "the picture depends only on the number of characters");
    // A long passphrase shows 24 dots and no more.
    splash.pty.type_keys(&[b'x'; 40]);
    let long = splash.wait_for("the most dots", |f| f.typed() == Some((24, true)));
    splash.pty.type_keys(&[b'y'; 40]);
    std::thread::sleep(Duration::from_millis(300));
    assert!(splash.snapshot().rgb == long.rgb);
    splash.pty.type_keys(b"\r");
    let (code, stdout) = wait_child(first, Duration::from_secs(5));
    assert_eq!((code, stdout.len()), (Some(0), other.len() + 80), "all of it reaches the client");
    splash.wait_for("the ring again", ring_alone);

    // cryptsetup rejects it: the initramfs script reports that and asks again.
    assert!(splash.plymouth().args(["message", &format!("--text={REJECTED}")]).status().unwrap().success());
    let frame =
        splash.wait_for("the error under the ring", |f| f.tinted(&f.layout().boot_message, assets::DANGER) > 100);
    assert!(ring_shown(&frame));
    let second = ask(&splash);
    let frame = splash.wait_for("the prompt again", prompt_shown);
    assert_eq!(frame.typed(), Some((0, true)), "what was typed before is gone");
    assert!(frame.tinted(&l.prompt_line, assets::DANGER) > 150, "the error is under the box, in the danger colour");
    assert_eq!(frame.tinted(&l.prompt_second_line, assets::DANGER), 0);
    assert!(frame.ink(&l.prompt_second_line) > 100, "and the hint is under the error");
    // A short line: no wider than the box, where the script's own message
    // would run far beyond it.
    if assets::TYPEFACE == "Inter" {
        let beside = Rect::new(l.prompt_line.x, l.prompt_line.y, l.text_box.x - l.prompt_line.x, l.prompt_line.h);
        assert!(beside.w > 100 && frame.ink(&beside) == 0);
    }
    // The box is where it was (no animation: it does not shake).
    assert!(l.prompt_line.union(&l.prompt_second_line).contains(&frame.changed(&empty)));

    // The next attempt: Enter takes the error away with the prompt.
    splash.pty.type_keys(b"right\r");
    let (code, stdout) = wait_child(second, Duration::from_secs(5));
    assert_eq!((code, stdout), (Some(0), b"right".to_vec()));
    let frame = splash.wait_for("the ring again", ring_alone);
    assert_eq!(frame.ink(&l.boot_message), 0, "the error does not outlive the attempt it was about");
}

#[test]
fn a_prompt_ends_when_its_client_goes_away() {
    let mut splash = Splash::start("cancel", "1024x768", &["--static"]);
    let mut ask = splash.client().args(["ask-password", "--prompt", "First"]).stdout(Stdio::piped()).spawn().unwrap();
    splash.wait_for("the prompt", prompt_shown);
    splash.pty.type_keys(b"half typed");
    // askpass kills the client with SIGKILL when another method wins.
    ask.kill().unwrap();
    ask.wait().unwrap();
    splash.wait_for("the ring again", ring_alone);

    // Two prompts queue up and each gets its own answer.
    let first = splash.client().args(["ask-password", "--prompt", "One"]).stdout(Stdio::piped()).spawn().unwrap();
    splash.wait_for("the first prompt", prompt_shown);
    let second = splash.client().args(["ask-password", "--prompt", "Two"]).stdout(Stdio::piped()).spawn().unwrap();
    std::thread::sleep(Duration::from_millis(200));
    // What was typed for the cancelled prompt is gone.
    splash.pty.type_keys(b"alpha\r");
    let (code, out) = wait_child(first, Duration::from_secs(5));
    assert_eq!((code, out), (Some(0), b"alpha".to_vec()));
    splash.wait_for("the second prompt", prompt_shown);
    splash.pty.type_keys(b"beta\r");
    let (code, out) = wait_child(second, Duration::from_secs(5));
    assert_eq!((code, out), (Some(0), b"beta".to_vec()));
    // An empty passphrase is a valid answer.
    let third = splash.client().args(["ask-password"]).stdout(Stdio::piped()).spawn().unwrap();
    splash.wait_for("the third prompt", prompt_shown);
    splash.pty.type_keys(b"\r");
    let (code, out) = wait_child(third, Duration::from_secs(5));
    assert_eq!((code, out.len()), (Some(0), 0));
}

#[test]
fn a_dead_daemon_fails_the_waiting_client_promptly() {
    let mut splash = Splash::start("dead", "800x600", &["--static"]);
    let ask = splash.client().args(["ask-password", "--prompt", "Unlock"]).stdout(Stdio::piped()).spawn().unwrap();
    splash.wait_for("the prompt", prompt_shown);
    splash.kill();
    let (code, out) = wait_child(ask, Duration::from_secs(3));
    assert_eq!(code, Some(1));
    assert!(out.is_empty());
}

#[test]
fn escape_hides_the_screen_and_the_console_takes_over() {
    let mut splash = Splash::start("hide", "800x600", &["--static"]);
    let before = splash.snapshot();
    splash.pty.type_keys(b"\x1b");
    std::thread::sleep(Duration::from_millis(150));
    // Hidden: messages and prompts go to the text console, the picture is frozen.
    splash.ok(Request::Message { text: "cryptsetup: sda3_crypt: set up successfully".into() });
    let ask = splash
        .client()
        .args(["ask-password", "--prompt", "Please unlock disk sda3_crypt"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let text = splash.pty.read_output(Duration::from_millis(800));
    assert!(text.contains("set up successfully"), "console output: {text:?}");
    assert!(text.contains("Please unlock disk sda3_crypt"), "console output: {text:?}");
    assert!(splash.snapshot().rgb == before.rgb, "nothing is drawn while hidden");
    splash.pty.type_keys(b"typed blind\r");
    let (code, out) = wait_child(ask, Duration::from_secs(5));
    assert_eq!((code, out), (Some(0), b"typed blind".to_vec()));
    assert!(
        !splash.pty.read_output(Duration::from_millis(200)).contains("typed blind"),
        "the passphrase is never echoed"
    );
    // Escape again brings the boot screen back, up to date.
    splash.pty.type_keys(b"\x1b");
    splash.wait_for("the message on screen", |f| f.ink(&f.layout().boot_message) > 300);
    // The client commands do the same.
    assert!(splash.client().arg("hide").status().unwrap().success());
    assert!(splash.client().arg("show").status().unwrap().success());
}

#[test]
fn launcher_supervisor_and_worker() {
    let dir = temp_dir("launch");
    let socket = dir.join("run").join("socket");
    let pid_file = dir.join("run").join("pid");
    let pty = open_pty();
    assert!(pty.cooked());
    let launch = || {
        Command::new(DAEMON)
            .arg("--headless=640x480")
            .arg(format!("--out={}", dir.join("out").display()))
            .arg(format!("--socket={}", socket.display()))
            .arg(format!("--tty={}", pty.path.display()))
            .stdin(Stdio::null())
            .status()
            .unwrap()
    };
    let pid_of = || std::fs::read_to_string(&pid_file).unwrap().trim().parse::<u32>().unwrap();
    let name_of = |pid: u32| {
        std::fs::read(format!("/proc/{pid}/cmdline"))
            .map(|c| String::from_utf8_lossy(&c).into_owned())
            .unwrap_or_default()
    };
    let parent_of = |pid: u32| -> u32 {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
        stat.rsplit(')').next().unwrap().split_whitespace().nth(1).unwrap().parse().unwrap()
    };

    // The launcher returns once the boot screen is up.
    assert!(launch().success());
    assert!(boswas_bootsplash::client::ping(&socket));
    let worker = pid_of();
    let supervisor = parent_of(worker);
    assert!(name_of(worker).starts_with("@boswas-bootsplashd"), "{}", name_of(worker));
    assert!(name_of(supervisor).starts_with("@boswas-bootsplashd"), "{}", name_of(supervisor));
    assert!(!pty.cooked(), "the console is in raw mode while the boot screen runs");

    // Starting again does not start a second one.
    assert!(launch().success());
    assert_eq!(pid_of(), worker);

    // The worker dies: the supervisor restores the console and cleans up.
    Command::new("kill").args(["-9", &worker.to_string()]).status().unwrap();
    wait_until("the supervisor to clean up", Duration::from_secs(5), || !exists(&socket) && !exists(&pid_file));
    wait_until("the console settings to return", Duration::from_secs(5), || pty.cooked());
    wait_until("the supervisor to exit", Duration::from_secs(5), || {
        !std::path::Path::new(&format!("/proc/{supervisor}")).exists()
    });
    assert!(!boswas_bootsplash::client::ping(&socket));

    // A clean quit: both processes leave, nothing stays behind.
    assert!(launch().success());
    let worker = pid_of();
    let supervisor = parent_of(worker);
    assert_eq!(boswas_bootsplash::client::request(&socket, &Request::Quit { retain: None }).unwrap(), Response::Ok);
    wait_until("the daemon to leave", Duration::from_secs(5), || {
        !std::path::Path::new(&format!("/proc/{worker}")).exists()
            && !std::path::Path::new(&format!("/proc/{supervisor}")).exists()
    });
    assert!(!exists(&socket) && !exists(&pid_file));
    assert!(pty.cooked());

    // The supervisor ignores termination signals; the worker quits cleanly on one.
    assert!(launch().success());
    let worker = pid_of();
    let supervisor = parent_of(worker);
    Command::new("kill").args(["-TERM", &supervisor.to_string()]).status().unwrap();
    std::thread::sleep(Duration::from_millis(200));
    assert!(boswas_bootsplash::client::ping(&socket), "the supervisor survives SIGTERM");
    Command::new("kill").args(["-TERM", &worker.to_string()]).status().unwrap();
    wait_until("the daemon to leave", Duration::from_secs(5), || !exists(&socket));
    wait_until("the console settings to return", Duration::from_secs(5), || pty.cooked());
    let _ = std::fs::remove_dir_all(&dir);
}

/// A boot screen started the way the system starts it (launcher,
/// supervisor, worker), headless, on a pseudo-terminal.
struct Launched {
    dir: std::path::PathBuf,
    socket: std::path::PathBuf,
    pid_file: std::path::PathBuf,
    pty: Pty,
    /// Every worker seen, to make sure none outlives the test.
    workers: std::cell::RefCell<Vec<u32>>,
}

impl Launched {
    fn new(name: &str) -> Launched {
        let dir = temp_dir(name);
        let socket = dir.join("run").join("socket");
        let pid_file = dir.join("run").join("pid");
        let launched = Launched { dir, socket, pid_file, pty: open_pty(), workers: Default::default() };
        assert!(launched.launch(&[]).success());
        launched
    }

    fn command(&self, extra: &[&str]) -> Command {
        let mut command = Command::new(DAEMON);
        command
            .arg("--headless=640x480")
            .arg(format!("--out={}", self.dir.join("out").display()))
            .arg(format!("--socket={}", self.socket.display()))
            .arg(format!("--tty={}", self.pty.path.display()))
            .args(extra)
            .stdin(Stdio::null());
        command
    }

    fn launch(&self, extra: &[&str]) -> std::process::ExitStatus {
        self.command(extra).status().unwrap()
    }

    fn worker(&self) -> u32 {
        let pid = std::fs::read_to_string(&self.pid_file).unwrap().trim().parse::<u32>().unwrap();
        self.workers.borrow_mut().push(pid);
        pid
    }

    fn signal(&self, signal: &str, pid: u32) {
        Command::new("kill").args([signal, &pid.to_string()]).status().unwrap();
    }
}

impl Drop for Launched {
    fn drop(&mut self) {
        for pid in self.workers.borrow().iter() {
            let _ = Command::new("kill").args(["-KILL", &pid.to_string()]).stderr(Stdio::null()).status();
        }
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn running(pid: u32) -> bool {
    std::path::Path::new(&format!("/proc/{pid}")).exists()
}

#[test]
fn a_second_start_leaves_a_boot_screen_that_does_not_answer_alone() {
    let first = Launched::new("second");
    let worker = first.worker();
    assert!(!first.pty.cooked());
    let lock = first.dir.join("run").join("lock");
    assert!(exists(&lock), "the lock is in the runtime directory");

    // The boot screen stops answering (here: stopped; on a device: busy for
    // longer than a client waits). The system starts it "again".
    first.signal("-STOP", worker);
    assert!(!boswas_bootsplash::client::ping(&first.socket));
    let begun = std::time::Instant::now();
    assert!(first.launch(&["--mode=shutdown"]).success(), "a boot screen is running: not a failure");
    assert!(begun.elapsed() < Duration::from_secs(15));
    // Nothing of the first one was touched: its files are there, its console
    // is still in raw mode, and it is the same process.
    assert!(exists(&first.socket) && exists(&first.pid_file));
    assert_eq!(first.worker(), worker);
    assert!(!first.pty.cooked(), "the console of the running boot screen was not reset");
    assert!(running(worker));
    // A worker started in the foreground is refused the same way.
    let status = first.command(&["--foreground"]).stderr(Stdio::null()).status().unwrap();
    assert_eq!(status.code(), Some(4));
    assert!(exists(&first.socket) && exists(&first.pid_file) && !first.pty.cooked());

    // It comes back and serves as before; the hand-over still works.
    first.signal("-CONT", worker);
    wait_until("the boot screen to answer again", Duration::from_secs(5), || {
        boswas_bootsplash::client::ping(&first.socket)
    });
    let quit = Command::new(CLIENT).arg("--socket").arg(&first.socket).arg("quit").status().unwrap();
    assert!(quit.success());
    wait_until("the daemon to leave", Duration::from_secs(5), || !running(worker) && !exists(&first.socket));
    assert!(!exists(&first.pid_file));
    wait_until("the console settings to return", Duration::from_secs(5), || first.pty.cooked());
    // The lock is free again: the next start is a new boot screen.
    assert!(first.launch(&[]).success());
    assert_ne!(first.worker(), worker);
}

#[test]
fn quit_ends_a_boot_screen_that_does_not_answer() {
    let stuck = Launched::new("stuck");
    let worker = stuck.worker();
    stuck.signal("-STOP", worker);
    // The hand-over to the login screen: the boot screen must be gone
    // afterwards, or nothing else can use the display.
    let begun = std::time::Instant::now();
    let quit = Command::new(CLIENT).arg("--socket").arg(&stuck.socket).args(["quit", "--retain"]).status().unwrap();
    assert!(quit.success(), "the boot screen was ended");
    assert!(begun.elapsed() < Duration::from_secs(12), "{:?}", begun.elapsed());
    wait_until("the worker to be gone", Duration::from_secs(5), || !running(worker));
    // Its supervisor put the console back and removed its files.
    wait_until("the supervisor to clean up", Duration::from_secs(5), || {
        !exists(&stuck.socket) && !exists(&stuck.pid_file)
    });
    wait_until("the console settings to return", Duration::from_secs(5), || stuck.pty.cooked());
    // Without a boot screen, quit reports that there was none.
    let quit = Command::new(CLIENT).arg("--socket").arg(&stuck.socket).arg("quit").status().unwrap();
    assert_eq!(quit.code(), Some(1));

    // A boot screen whose socket has been removed is found through its pid file.
    assert!(stuck.launch(&[]).success());
    let worker = stuck.worker();
    std::fs::remove_file(&stuck.socket).unwrap();
    let quit = Command::new(CLIENT).arg("--socket").arg(&stuck.socket).arg("quit").status().unwrap();
    assert!(quit.success());
    wait_until("the worker to be gone", Duration::from_secs(5), || !running(worker));
    wait_until("the console settings to return", Duration::from_secs(5), || stuck.pty.cooked());
    wait_until("the pid file to be removed", Duration::from_secs(5), || !exists(&stuck.pid_file));
    // A pid file that names some other program is not acted on.
    std::fs::create_dir_all(stuck.pid_file.parent().unwrap()).unwrap();
    std::fs::write(&stuck.pid_file, format!("{}\n", std::process::id())).unwrap();
    let quit = Command::new(CLIENT).arg("--socket").arg(&stuck.socket).arg("quit").status().unwrap();
    assert_eq!(quit.code(), Some(1), "this test is not a boot screen and is still alive");
}

#[test]
fn the_supervisor_cleans_up_where_paths_no_longer_lead() {
    let moved = Launched::new("moved");
    let worker = moved.worker();
    // After the switch from the initramfs to the real root the supervisor's
    // paths lead nowhere. Here: the runtime directory gets another name.
    let elsewhere = moved.dir.join("elsewhere");
    std::fs::rename(moved.dir.join("run"), &elsewhere).unwrap();
    assert!(exists(&elsewhere.join("socket")) && exists(&elsewhere.join("pid")));
    moved.signal("-KILL", worker);
    wait_until("the supervisor to clean up", Duration::from_secs(5), || {
        !exists(&elsewhere.join("socket")) && !exists(&elsewhere.join("pid"))
    });
    wait_until("the console settings to return", Duration::from_secs(5), || moved.pty.cooked());
}

#[test]
fn raw_mode_returns_after_the_console_was_reset() {
    let mut splash = Splash::start("reset", "800x600", &["--static"]);
    assert!(!splash.pty.cooked());
    // What systemd does to the console a moment after it starts.
    let reset = |pty: &Pty| {
        use rustix::termios::{tcgetattr, tcsetattr, LocalModes, OptionalActions};
        let mut sane = tcgetattr(&pty.master).unwrap();
        sane.local_modes.insert(LocalModes::ECHO | LocalModes::ICANON | LocalModes::ISIG);
        tcsetattr(&pty.master, OptionalActions::Now, &sane).unwrap();
    };
    reset(&splash.pty);
    assert!(splash.pty.cooked());
    wait_until("raw mode to return", Duration::from_secs(3), || !splash.pty.cooked());
    // Escape works at once, without Enter: the screen hides and comes back.
    let before = splash.snapshot();
    splash.pty.type_keys(b"\x1b");
    std::thread::sleep(Duration::from_millis(200));
    splash.ok(Request::Message { text: "cryptsetup: sda3_crypt: set up successfully".into() });
    assert!(splash.snapshot().rgb == before.rgb, "hidden: nothing is drawn");
    splash.pty.type_keys(b"\x1b");
    splash.wait_for("the message on screen", |f| f.ink(&f.layout().boot_message) > 300);
    // The console is reset again just before a passphrase is asked for:
    // what is typed is still not echoed and arrives without line editing.
    reset(&splash.pty);
    let ask = splash
        .client()
        .args(["ask-password", "--prompt", "Please unlock disk sda3_crypt"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    splash.wait_for("the prompt", prompt_shown);
    assert!(!splash.pty.cooked());
    let _ = splash.pty.read_output(Duration::from_millis(100));
    splash.pty.type_keys(b"open sesame\r");
    let (code, out) = wait_child(ask, Duration::from_secs(5));
    assert_eq!((code, out), (Some(0), b"open sesame".to_vec()));
    assert!(!splash.pty.read_output(Duration::from_millis(200)).contains("sesame"), "the passphrase is never echoed");
}

#[test]
fn a_start_that_fails_reports_failure_and_leaves_nothing_behind() {
    let dir = temp_dir("fail");
    let socket = dir.join("run").join("socket");
    // The console does not exist: the boot screen must refuse to start.
    let status = Command::new(DAEMON)
        .arg("--headless=640x480")
        .arg(format!("--out={}", dir.join("out").display()))
        .arg(format!("--socket={}", socket.display()))
        .arg("--tty=/nonexistent/tty")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(1));
    assert!(!exists(&socket));
    assert!(!boswas_bootsplash::client::ping(&socket));
    // Usage errors.
    let status = Command::new(DAEMON).arg("--frobnicate").stderr(Stdio::null()).status().unwrap();
    assert_eq!(status.code(), Some(2));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn quit_with_retain_falls_back_to_a_normal_quit_without_a_display_to_keep() {
    let mut splash = Splash::start("retain", "640x480", &["--static"]);
    assert!(splash.client().args(["quit", "--retain=2"]).status().unwrap().success());
    // The headless output has nothing to hand over, so the daemon leaves at once.
    assert!(splash.wait_exit(Duration::from_secs(2)).expect("exits without lingering").success());
    assert!(splash.pty.cooked());
}

#[test]
fn the_sandbox_denies_what_it_should() {
    let output = Command::new(DAEMON).arg("--self-test").output().unwrap();
    let report = String::from_utf8_lossy(&output.stdout);
    match output.status.code() {
        Some(0) => {
            assert!(report.contains("no_new_privs: yes"), "{report}");
            assert!(report.contains("landlock: full") || report.contains("landlock: partial"), "{report}");
            assert!(report.contains("reading files outside the sandbox denied: yes"), "{report}");
            assert!(report.contains("creating files outside the sandbox denied: yes"), "{report}");
            assert!(report.contains("TCP connections denied: yes"), "{report}");
        }
        Some(3) => eprintln!("skipped: this kernel has no Landlock\n{report}"),
        other => panic!("sandbox self-test failed ({other:?}):\n{report}"),
    }
}

#[test]
fn the_running_daemon_is_sandboxed() {
    let splash = Splash::start("sandboxed", "640x480", &["--static"]);
    let pid = splash.child.as_ref().unwrap().id();
    let status = std::fs::read_to_string(format!("/proc/{pid}/status")).unwrap();
    assert!(status.lines().any(|l| l.starts_with("NoNewPrivs:") && l.ends_with('1')), "{status}");
    // The daemon can still write a snapshot into its own directory...
    splash.ok(Request::Snapshot { name: "inside".into() });
    assert!(splash.out.join("inside.png").exists());
    // ...and a chroot request to a path that does not exist is refused, not fatal.
    assert!(matches!(splash.request(Request::NewRoot { path: "/nonexistent/root".into() }), Response::Error(_)));
    splash.ok(Request::Ping);
}

#[test]
fn systemd_password_requests_are_answered_on_the_boot_screen() {
    let mut splash = Splash::start("agent", "1024x768", &["--static"]);
    let ask_dir = splash.dir.join("ask-password");
    std::fs::create_dir_all(&ask_dir).unwrap();
    let reply_path = ask_dir.join("sck.1a2b");
    let reply = UnixDatagram::bind(&reply_path).unwrap();
    reply.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let (socket, dir) = (splash.socket.clone(), ask_dir.clone());
    let agent = std::thread::spawn(move || boswas_bootsplash::agent::run(&socket, &dir));
    std::thread::sleep(Duration::from_millis(100));
    // systemd writes the request under a temporary name and renames it.
    let request = format!(
        "[Ask]\nPID={}\nSocket={}\nAcceptCached=0\nEcho=0\nNotAfter=0\nMessage=Please enter passphrase for disk data:\n",
        std::process::id(),
        reply_path.display()
    );
    std::fs::write(ask_dir.join("tmp.1a2b"), request).unwrap();
    std::fs::rename(ask_dir.join("tmp.1a2b"), ask_dir.join("ask.1a2b")).unwrap();
    splash.wait_for("the prompt", prompt_shown);
    splash.pty.type_keys(b"second volume\r");
    let mut buf = [0u8; 128];
    let n = reply.recv(&mut buf).expect("the agent answers on the request's socket");
    assert_eq!(&buf[..n], b"+second volume");
    std::fs::remove_file(ask_dir.join("ask.1a2b")).unwrap();

    // A request that disappears while the prompt is open withdraws the prompt.
    std::fs::write(ask_dir.join("ask.3c4d"), format!("[Ask]\nSocket={}\nMessage=Another:\n", reply_path.display()))
        .unwrap();
    splash.wait_for("the second prompt", prompt_shown);
    std::fs::remove_file(ask_dir.join("ask.3c4d")).unwrap();
    splash.wait_for("the ring again", ring_alone);

    // The agent leaves when the boot screen does.
    splash.ok(Request::Quit { retain: None });
    assert_eq!(agent.join().unwrap(), 0);
}
