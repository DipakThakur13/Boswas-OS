//! The daemon end to end, in headless mode: control protocol, rendering,
//! passphrase entry through a pseudo-terminal, the Plymouth-compatible
//! client, start-up and clean-up, and the sandbox.

mod common;

use std::os::unix::net::UnixDatagram;
use std::process::{Command, Stdio};
use std::time::Duration;

use boswas_bootsplash::assets;
use boswas_bootsplash::proto::{self, Request, Response};
use boswas_bootsplash::scene::Mode;
use common::*;

fn prompt_shown(frame: &Frame) -> bool {
    frame.count(&frame.layout().field, assets::FIELD_FILL) > 500
}

fn arc_shown(frame: &Frame) -> bool {
    frame.count(&frame.layout().arc, assets::TRACK) > 20
}

#[test]
fn control_requests_change_the_picture() {
    let mut splash = Splash::start("control", "1280x720", &["--static"]);
    splash.ok(Request::Ping);
    let frame = splash.snapshot();
    let l = frame.layout();
    assert_eq!((frame.width, frame.height), (1280, 720));
    assert_eq!(frame.pixel(3, 3), assets::BACKGROUND);
    assert!(frame.count(&l.wordmark, assets::TEXT) > 300, "the wordmark is drawn");
    assert!(arc_shown(&frame));
    assert_eq!(frame.count(&l.boot_message, assets::BACKGROUND), l.boot_message.area() as usize);

    splash.ok(Request::Message { text: "cryptsetup: sda3_crypt: set up successfully".into() });
    let frame = splash.snapshot();
    assert!(frame.count(&l.boot_message, assets::TEXT_MUTED) > 50, "an informational message is muted");
    splash.ok(Request::Message {
        text: "cryptsetup: ERROR: sda3_crypt: cryptsetup failed, bad password or options?".into(),
    });
    let frame = splash.snapshot();
    assert!(frame.count(&l.boot_message, assets::DANGER) > 50, "an error message is in the danger colour");
    splash.ok(Request::Message { text: String::new() });
    let frame = splash.snapshot();
    assert_eq!(frame.count(&l.boot_message, assets::BACKGROUND), l.boot_message.area() as usize);

    splash.ok(Request::Progress(60));
    assert!(splash.snapshot().count(&l.arc, assets::ACCENT_DEEP) > 20);
    splash.ok(Request::Mode(Mode::Shutdown));
    assert!(splash.snapshot().count(&l.status, assets::TEXT_SECONDARY) > 50, "shutdown shows its status line");
    splash.ok(Request::Mode(Mode::Boot));
    assert_eq!(splash.snapshot().count(&l.status, assets::TEXT_SECONDARY), 0);

    splash.ok(Request::Quit { retain: None });
    let status = splash.wait_exit(Duration::from_secs(5)).expect("the daemon exits when told to quit");
    assert!(status.success());
    assert!(!exists(&splash.socket), "the socket is removed");
    assert!(!exists(&splash.socket.with_file_name("pid")), "the pid file is removed");
}

#[test]
fn the_animation_advances_and_stays_inside_the_arc() {
    let mut splash = Splash::start("animate", "1024x768", &[]);
    let first = splash.snapshot();
    std::thread::sleep(Duration::from_millis(250));
    let second = splash.snapshot();
    let arc = first.layout().arc;
    let mut changed_inside = 0;
    for y in 0..first.height as i32 {
        for x in 0..first.width as i32 {
            if first.pixel(x, y) != second.pixel(x, y) {
                let inside = x >= arc.x && x < arc.right() && y >= arc.y && y < arc.bottom();
                assert!(inside, "pixel {x},{y} changed outside the arc");
                changed_inside += 1;
            }
        }
    }
    assert!(changed_inside > 20, "the comet moved");
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
    assert!(frame.count(&l.prompt, assets::TEXT) > 100, "the prompt text is shown");
    assert!(frame.count(&l.field, assets::ACCENT) > 100, "the field has its focus ring");
    assert!(!arc_shown(&frame), "the arc gives way to the prompt");
    let empty = frame.count(&l.field, assets::TEXT);

    // Typing shows dots, never characters; editing keys work.
    splash.pty.type_keys(b"wrong");
    let frame = splash.wait_for("five dots", |f| f.count(&f.layout().field, assets::TEXT) > empty + 60);
    let five = frame.count(&l.field, assets::TEXT);
    splash.pty.type_keys(b"\x7f\x7f");
    splash.wait_for("three dots", |f| f.count(&f.layout().field, assets::TEXT) < five);
    splash.pty.type_keys(b"\x15");
    splash.wait_for("an empty field", |f| f.count(&f.layout().field, assets::TEXT) == empty);
    // Arrow keys and other sequences are not part of the passphrase.
    splash.pty.type_keys(b"\x1b[A");
    splash.pty.type_keys("correct h\u{f6}rse \u{20ac}".as_bytes());
    splash.pty.type_keys(b"\r");

    let (code, stdout) = wait_child(ask, Duration::from_secs(5));
    assert_eq!(code, Some(0));
    assert_eq!(stdout, "correct h\u{f6}rse \u{20ac}".as_bytes(), "exactly the passphrase, no newline");
    splash.wait_for("the arc again", arc_shown);

    // Messages and the other commands scripts use.
    assert!(splash
        .plymouth()
        .args(["message", "--text=cryptsetup: sda3_crypt: set up successfully"])
        .status()
        .unwrap()
        .success());
    splash.wait_for("the message", |f| f.count(&f.layout().boot_message, assets::TEXT_MUTED) > 50);
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
fn a_prompt_ends_when_its_client_goes_away() {
    let mut splash = Splash::start("cancel", "1024x768", &["--static"]);
    let mut ask = splash.client().args(["ask-password", "--prompt", "First"]).stdout(Stdio::piped()).spawn().unwrap();
    splash.wait_for("the prompt", prompt_shown);
    splash.pty.type_keys(b"half typed");
    // askpass kills the client with SIGKILL when another method wins.
    ask.kill().unwrap();
    ask.wait().unwrap();
    splash.wait_for("the arc again", arc_shown);

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
    splash.wait_for("the message on screen", |f| f.count(&f.layout().boot_message, assets::TEXT_MUTED) > 50);
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
    splash.wait_for("the arc again", arc_shown);

    // The agent leaves when the boot screen does.
    splash.ok(Request::Quit { retain: None });
    assert_eq!(agent.join().unwrap(), 0);
}
