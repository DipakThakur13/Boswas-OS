//! Starting the daemon: launcher, supervisor, worker.
//!
//! `boswas-bootsplashd` is started by an initramfs script or a systemd unit
//! and must return quickly, leave a daemon behind, and never make things
//! worse than no boot screen at all:
//!
//! 1. The **launcher** (the process the script started) checks the kernel
//!    command line, starts the supervisor, waits until the boot screen
//!    reports that it is up (or failed), and exits with that result.
//! 2. The **supervisor** starts the worker and waits for it. If the worker
//!    ends abnormally it puts the console back into text mode and removes
//!    the control socket, so the passphrase prompt falls back to the text
//!    console instead of hiding behind a dead screen.
//! 3. The **worker** is the boot screen ([`crate::daemon`]).
//!
//! Supervisor and worker run under the name `@boswas-bootsplashd`: systemd
//! leaves processes whose name starts with `@` alone when it stops
//! everything at shutdown, which keeps the shutdown screen up to the end.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use rustix::event::{poll, PollFd, PollFlags, Timespec};

use crate::client;
use crate::cmdline;
use crate::daemon::{self, Options, Stage};
use crate::proto::Request;
use crate::sys;
use crate::tty::{Console, DEFAULT_TTY};

/// Name the daemon runs under (see the module documentation).
const DAEMON_NAME: &str = "@boswas-bootsplashd";
/// Longest the launcher waits for the boot screen to come up.
const START_TIMEOUT: Duration = Duration::from_secs(10);

/// Run the stage selected by the options; returns the exit code.
pub fn main(opts: Options, args: &[String]) -> i32 {
    if opts.self_test {
        return self_test(&opts);
    }
    match opts.stage {
        Stage::Launcher => launcher(opts, args),
        Stage::Supervisor => supervisor(opts, args),
        Stage::Worker => worker(opts),
    }
}

fn kernel_command_line() -> String {
    std::fs::read_to_string("/proc/cmdline").unwrap_or_default()
}

fn launcher(opts: Options, args: &[String]) -> i32 {
    let mut extra: Vec<&str> = vec!["--supervise"];
    if !opts.force && opts.headless.is_none() {
        let params = cmdline::parse(&kernel_command_line());
        if !params.splash {
            return 0; // no boot screen wanted: not an error
        }
        if !params.animate {
            extra.push("--static");
        }
    }
    // Already running (started in the initramfs, or kept from boot): just
    // make sure it shows the right screen.
    if client::ping(&opts.socket) {
        let _ = client::request(&opts.socket, &Request::Mode(opts.mode));
        if opts.mode != crate::scene::Mode::Boot {
            // Shutdown always gets its screen, even if the boot screen was
            // hidden with Escape earlier.
            let _ = client::request(&opts.socket, &Request::Show);
        }
        return 0;
    }

    let Ok(exe) = std::env::current_exe() else {
        eprintln!("boswas-bootsplashd: cannot find my own executable");
        return 1;
    };
    let Ok((ready_rx, ready_tx)) = rustix::pipe::pipe() else {
        eprintln!("boswas-bootsplashd: cannot create a pipe");
        return 1;
    };
    let spawned = Command::new(&exe)
        .arg0(DAEMON_NAME)
        .args(args)
        .args(&extra)
        .env_clear()
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(Stdio::from(ready_tx))
        .process_group(0)
        .spawn();
    if let Err(e) = spawned {
        eprintln!("boswas-bootsplashd: cannot start: {e}");
        return 1;
    }

    // Wait for "READY" (or a failure) from the worker, but not for ever.
    let spec = Timespec { tv_sec: START_TIMEOUT.as_secs() as i64, tv_nsec: 0 };
    let mut fds = [PollFd::new(&ready_rx, PollFlags::IN)];
    if poll(&mut fds, Some(&spec)).unwrap_or(0) == 0 {
        eprintln!("boswas-bootsplashd: the boot screen did not start in time");
        return 1;
    }
    let mut line = String::new();
    let _ = BufReader::new(std::fs::File::from(ready_rx)).read_line(&mut line);
    if line.trim() == "READY" {
        0
    } else {
        1
    }
}

fn supervisor(opts: Options, args: &[String]) -> i32 {
    // Remember how the console looked, to be able to put it back.
    let tty = opts.tty.clone().unwrap_or_else(|| DEFAULT_TTY.into());
    let mut console = Console::open(&tty).ok();
    if let Some(console) = console.as_mut() {
        console.remember_termios();
    }
    let Ok(exe) = std::env::current_exe() else { return 1 };
    let worker_args: Vec<&String> = args.iter().filter(|a| a.as_str() != "--supervise").collect();
    let child = Command::new(&exe)
        .arg0(DAEMON_NAME)
        .args(worker_args)
        .arg("--worker")
        .env_clear()
        .current_dir("/")
        .stdin(Stdio::null())
        .spawn();
    let mut child = match child {
        Ok(child) => child,
        Err(e) => {
            eprintln!("boswas-bootsplashd: cannot start the worker: {e}");
            println!("FAILED");
            return 1;
        }
    };
    // Only the worker reacts to termination requests; this process must
    // outlive it to clean up.
    sys::ignore_signals(&[libc::SIGTERM, libc::SIGINT, libc::SIGHUP]);
    let clean = matches!(child.wait(), Ok(status) if status.success());
    if clean {
        return 0;
    }
    // The worker died without tidying up: give the text console back.
    if let Some(console) = console.as_mut() {
        console.force_text();
        console.restore_termios();
    }
    remove_runtime_files(&opts.socket);
    let mut out = std::io::stdout();
    let _ = writeln!(out, "FAILED");
    let _ = out.flush();
    1
}

/// Remove the control socket and pid file of a daemon that is gone.
pub fn remove_runtime_files(socket: &Path) {
    let _ = std::fs::remove_file(socket);
    let _ = std::fs::remove_file(daemon::pid_path(socket));
}

fn worker(opts: Options) -> i32 {
    // /dev/null for standard output once readiness has been reported; it
    // must be opened before the sandbox closes the file system.
    let null = std::fs::OpenOptions::new().write(true).open("/dev/null").ok();
    let ready = move || {
        let mut out = std::io::stdout();
        let _ = writeln!(out, "READY");
        let _ = out.flush();
        if let Some(null) = &null {
            let _ = rustix::stdio::dup2_stdout(null);
        }
    };
    match daemon::run_worker(opts, ready) {
        Ok(()) => 0,
        Err(reason) => {
            eprintln!("boswas-bootsplashd: {reason}");
            1
        }
    }
}

/// Enter the daemon's sandbox in this process and try things it must deny.
/// Prints one line per restriction. Exit status: 0 when everything is
/// enforced, 3 when the kernel has no Landlock (the other restrictions
/// still apply), 1 when something that should be denied was allowed.
pub fn self_test(opts: &Options) -> i32 {
    use std::io::ErrorKind::PermissionDenied;
    let mut allowed = crate::sandbox::Allowed { devices: crate::sandbox::display_devices(), state: Vec::new() };
    if let Some(dir) = opts.socket.parent().filter(|d| d.is_dir()) {
        allowed.state.push(dir.to_path_buf());
    }
    let report = crate::sandbox::enter(&allowed);
    let yes_no = |b: bool| if b { "yes" } else { "no" };
    println!("no_new_privs: {}", yes_no(report.no_new_privs));
    println!("landlock: {}", report.landlock);
    println!("capabilities reduced: {}", yes_no(report.capabilities_dropped));
    if matches!(report.landlock, "none" | "error") {
        return if report.no_new_privs && report.landlock == "none" { 3 } else { 1 };
    }
    let read_denied = matches!(std::fs::File::open("/etc/passwd"), Err(e) if e.kind() == PermissionDenied);
    let probe = std::env::temp_dir().join(format!("boswas-bootsplash-self-test-{}", std::process::id()));
    let write_denied = match std::fs::File::create(&probe) {
        Err(e) => e.kind() == PermissionDenied,
        Ok(_) => {
            let _ = std::fs::remove_file(&probe);
            false
        }
    };
    let loopback = std::net::SocketAddr::from(([127, 0, 0, 1], 9));
    let tcp_denied = matches!(std::net::TcpStream::connect_timeout(&loopback, Duration::from_millis(300)), Err(e) if e.kind() == PermissionDenied);
    println!("reading files outside the sandbox denied: {}", yes_no(read_denied));
    println!("creating files outside the sandbox denied: {}", yes_no(write_denied));
    println!("TCP connections denied: {}", yes_no(tcp_denied));
    if report.no_new_privs && read_denied && write_denied && tcp_denied {
        0
    } else {
        1
    }
}
