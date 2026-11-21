//! Starting the daemon: launcher, supervisor, worker.
//!
//! `boswas-bootsplashd` is started by an initramfs script or a systemd unit
//! and must return quickly, leave a daemon behind, and never make things
//! worse than no boot screen at all:
//!
//! 1. The **launcher** (the process the script started) checks the kernel
//!    command line, starts the supervisor, waits until the boot screen
//!    reports that it is up (or failed), and exits with that result.
//! 2. The **supervisor** takes the lock of the runtime directory, starts
//!    the worker and waits for it. While the worker runs it opens graphics
//!    devices for it on request ([`crate::broker`]). If the worker ends
//!    abnormally it puts the console back into text mode and removes the
//!    control socket and the pid file, so the passphrase prompt falls back
//!    to the text console instead of hiding behind a dead screen.
//! 3. The **worker** is the boot screen ([`crate::daemon`]).
//!
//! There is one boot screen at a time. A second start while one is running
//! normally finds it answering on its socket and only tells it which screen
//! to show. If it does not answer (it is busy, or stopped), the second
//! supervisor finds the lock taken and leaves without starting a worker:
//! nothing of the running boot screen is touched, neither its console nor
//! its files. A supervisor cleans up only after its own worker.
//!
//! Supervisor and worker run under the name `@boswas-bootsplashd`: systemd
//! leaves processes whose name starts with `@` alone when it stops
//! everything at shutdown, which keeps the shutdown screen up to the end.

use std::io::{BufRead, BufReader, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::Duration;

use rustix::event::{poll, PollFd, PollFlags, Timespec};

use crate::broker;
use crate::client;
use crate::cmdline;
use crate::daemon::{self, Options, Stage, StartError};
use crate::proto::Request;
use crate::runtime::RunDir;
use crate::sys;
use crate::tty::{Console, DEFAULT_TTY};

/// Name the daemon runs under (see the module documentation).
const DAEMON_NAME: &str = "@boswas-bootsplashd";
/// Longest the launcher waits for the boot screen to come up, on top of
/// the time the worker may wait for a display (`--wait`).
const START_TIMEOUT: Duration = Duration::from_secs(10);
/// Exit status of a worker that found another boot screen running.
pub const EXIT_RUNNING: i32 = 4;

/// What the supervisor and the worker report to the launcher, one line on
/// the pipe that is their standard output.
const READY: &str = "READY";
const RUNNING: &str = "RUNNING";
const FAILED: &str = "FAILED";

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

fn report(line: &str) {
    let mut out = std::io::stdout();
    let _ = writeln!(out, "{line}");
    let _ = out.flush();
}

/// Tell the running boot screen which screen to show.
fn announce(opts: &Options) {
    let _ = client::request(&opts.socket, &Request::Mode(opts.mode));
    if opts.mode != crate::scene::Mode::Boot {
        // Shutdown always gets its screen, even if the boot screen was
        // hidden with Escape earlier.
        let _ = client::request(&opts.socket, &Request::Show);
    }
}

fn launcher(opts: Options, args: &[String]) -> i32 {
    let mut extra: Vec<String> = vec!["--supervise".into()];
    if !opts.force && opts.headless.is_none() {
        let params = cmdline::parse(&kernel_command_line());
        if !params.splash {
            return 0; // no boot screen wanted: not an error
        }
        extra.extend(extra_options(&params, &opts));
    }
    // Already running (started in the initramfs, or kept from boot): just
    // make sure it shows the right screen.
    if client::ping(&opts.socket) {
        announce(&opts);
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

    // Wait for the outcome from the supervisor or the worker, but not for ever.
    let limit = START_TIMEOUT + Duration::from_millis(opts.wait_ms);
    let spec = Timespec { tv_sec: limit.as_secs() as i64, tv_nsec: 0 };
    let mut fds = [PollFd::new(&ready_rx, PollFlags::IN)];
    if poll(&mut fds, Some(&spec)).unwrap_or(0) == 0 {
        eprintln!("boswas-bootsplashd: the boot screen did not start in time");
        return 1;
    }
    let mut line = String::new();
    let _ = BufReader::new(std::fs::File::from(ready_rx)).read_line(&mut line);
    match line.trim() {
        READY => 0,
        RUNNING => {
            // A boot screen is running but did not answer the first time.
            // It was left alone; ask once more in case it was only busy.
            announce(&opts);
            0
        }
        _ => 1,
    }
}

/// Options the kernel command line adds to the ones the daemon was started
/// with (they come later on the worker's command line and so win).
fn extra_options(params: &cmdline::BootParams, opts: &Options) -> Vec<String> {
    let mut extra = Vec::new();
    if !params.animate {
        extra.push("--static".to_owned());
    }
    // With `nomodeset` no graphics driver will load: a display that is not
    // there now will not come, and a long wait for one only delays the boot.
    let short = Options::default().wait_ms;
    if !params.modeset && opts.wait_ms > short {
        extra.push(format!("--wait={short}"));
    }
    extra
}

fn supervisor(opts: Options, args: &[String]) -> i32 {
    let failed = |reason: String| {
        eprintln!("boswas-bootsplashd: {reason}");
        report(FAILED);
        1
    };
    // The runtime directory is opened, and the lock in it taken, before
    // anything else: whoever holds the lock is the one boot screen.
    let run = match RunDir::open(&opts.socket) {
        Ok(run) => run,
        Err(e) => return failed(format!("runtime directory of {}: {e}", opts.socket.display())),
    };
    let _lock = match run.lock() {
        Ok(Some(lock)) => lock,
        Ok(None) => {
            report(RUNNING);
            return 0;
        }
        Err(e) => return failed(format!("lock next to {}: {e}", opts.socket.display())),
    };
    // Remember how the console looked, to be able to put it back.
    let tty = opts.tty.clone().unwrap_or_else(|| DEFAULT_TTY.into());
    let mut console = Console::open(&tty).ok();
    if let Some(console) = console.as_mut() {
        console.remember_termios();
    }
    let Ok(exe) = std::env::current_exe() else { return failed("cannot find my own executable".into()) };
    // The channel on which the worker asks for graphics devices once it is
    // inside its sandbox. Without it the worker still runs; it then cannot
    // follow a change of graphics device that replaces the device directory.
    let devices = match &opts.headless {
        Some(_) => None,
        None => broker::Server::new(Path::new(broker::DEVICE_DIR)).ok().zip(broker::channel().ok()),
    };
    let (server, channel) = match devices {
        Some((server, (mine, theirs))) => (Some((server, mine)), Some(theirs)),
        None => (None, None),
    };
    let worker_args: Vec<&String> = args.iter().filter(|a| a.as_str() != "--supervise").collect();
    let mut command = Command::new(&exe);
    command.arg0(DAEMON_NAME).args(worker_args).arg("--worker").env_clear().current_dir("/");
    match channel {
        Some(theirs) => command.arg("--devices-on-stdin").stdin(Stdio::from(theirs)),
        None => command.stdin(Stdio::null()),
    };
    let spawned = command.spawn();
    // The worker's end of the channel must be open in the worker only, so
    // that the channel closes when the worker ends.
    drop(command);
    let mut child = match spawned {
        Ok(child) => child,
        Err(e) => return failed(format!("cannot start the worker: {e}")),
    };
    // Only the worker reacts to termination requests; this process must
    // outlive it to clean up.
    sys::ignore_signals(&[libc::SIGTERM, libc::SIGINT, libc::SIGHUP]);
    let status = attend(&mut child, server);
    if status.as_ref().is_some_and(|status| status.code() == Some(EXIT_RUNNING)) {
        // The worker found another boot screen (one that lost its
        // supervisor). Everything there is belongs to that one.
        report(RUNNING);
        return 0;
    }
    // The socket and the pid file: the worker removes them when it quits,
    // but by path, and paths may have stopped working for it. They are
    // removed here through the open directory, unless they are not this
    // worker's.
    let ours = run.files_belong_to(child.id());
    if ours {
        run.remove_files();
    }
    if status.is_some_and(|status| status.success()) {
        return 0;
    }
    // The worker died without tidying up: give the text console back.
    if ours {
        if let Some(console) = console.as_mut() {
            console.force_text();
            console.restore_termios();
        }
    }
    report(FAILED);
    1
}

/// Wait for the worker to end, opening graphics devices for it meanwhile.
fn attend(child: &mut Child, server: Option<(broker::Server, OwnedFd)>) -> Option<ExitStatus> {
    if let Some((server, channel)) = server {
        loop {
            if let Ok(Some(status)) = child.try_wait() {
                return Some(status);
            }
            let ready = {
                let mut fds = [PollFd::new(&channel, PollFlags::IN)];
                poll(&mut fds, Some(&Timespec { tv_sec: 1, tv_nsec: 0 })).unwrap_or(0)
            };
            // A closed channel means the worker has ended, or is ending.
            if ready > 0 && !server.serve(channel.as_fd()).unwrap_or(false) {
                break;
            }
        }
    }
    child.wait().ok()
}

fn worker(opts: Options) -> i32 {
    // /dev/null for standard output once readiness has been reported, and
    // for standard input once the channel to the supervisor has been taken
    // from it; it must be opened before the sandbox closes the file system.
    let null = std::fs::OpenOptions::new().read(true).write(true).open("/dev/null").ok();
    let devices = if opts.helper {
        let channel = std::io::stdin().as_fd().try_clone_to_owned().ok();
        if let Some(null) = &null {
            let _ = rustix::stdio::dup2_stdin(null);
        }
        channel.map(broker::Client::new)
    } else {
        None
    };
    let ready = move || {
        report(READY);
        if let Some(null) = &null {
            let _ = rustix::stdio::dup2_stdout(null);
        }
    };
    match daemon::run_worker(opts, devices, ready) {
        Ok(()) => 0,
        Err(StartError::Running) => {
            eprintln!("boswas-bootsplashd: {}", StartError::Running);
            EXIT_RUNNING
        }
        Err(StartError::Failed(reason)) => {
            eprintln!("boswas-bootsplashd: {reason}");
            1
        }
    }
}

/// Enter the daemon's sandbox in this process and try things it must deny.
/// Prints one line per restriction. Exit status: 0 when everything is
/// enforced; 3 when the kernel cannot enforce all of it, because it has no
/// Landlock or a Landlock without network rules (before Linux 6.7), and
/// everything it can enforce is; 1 when something that should be denied
/// was allowed.
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
    let tcp_line = match (tcp_denied, report.landlock_net) {
        (true, _) => "yes",
        (false, true) => "no",
        (false, false) => "no (this kernel's Landlock has no network rules)",
    };
    println!("TCP connections denied: {tcp_line}");
    if !(report.no_new_privs && read_denied && write_denied) || (report.landlock_net && !tcp_denied) {
        1
    } else if tcp_denied {
        0
    } else {
        3
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_kernel_command_line_adds_options() {
        let patient = daemon::parse_args(&["--mode=boot".to_owned(), "--wait=8000".to_owned()]).unwrap();
        let plain = cmdline::parse("quiet splash");
        assert!(extra_options(&plain, &patient).is_empty());
        assert_eq!(extra_options(&cmdline::parse("quiet splash boswas.splash=static"), &patient), ["--static"]);
        // No graphics driver will load: the long wait for one is cut short...
        let extra = extra_options(&cmdline::parse("quiet splash nomodeset"), &patient);
        assert_eq!(extra, ["--wait=1500"]);
        // ...and the worker, which gets these after its own options, obeys.
        let mut args = vec!["--mode=boot".to_owned(), "--wait=8000".to_owned()];
        args.extend(extra);
        assert_eq!(daemon::parse_args(&args).unwrap().wait_ms, Options::default().wait_ms);
        // A wait that is already short is left as it is.
        assert!(extra_options(&cmdline::parse("splash nomodeset"), &Options::default()).is_empty());
    }
}
