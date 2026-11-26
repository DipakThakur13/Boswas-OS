//! boswas-compositor: the Boswas OS Wayland compositor.
//!
//! The display server, window manager and effects engine of the Boswas
//! desktop. It replaces KWin and, with boswas-shell, the Plasma session.

mod animation;
mod backend;
mod config;
mod cursor;
mod effects;
mod exit;
mod focus;
mod handlers;
mod input;
mod ipc;
mod keyaccess;
mod paste;
mod render;
mod scale;
mod shell;
mod state;
mod stats;
mod supervise;

use std::{path::PathBuf, time::Duration};

use smithay::reexports::{
    calloop::{
        timer::{TimeoutAction, Timer},
        EventLoop,
    },
    wayland_server::Display,
};
use tracing::{error, info, warn};

use crate::{backend::Backend, config::Config, state::Boswas};

const USAGE: &str = "\
Usage: boswas-compositor [OPTIONS] [-- COMMAND [ARGS...]]

The Boswas OS Wayland compositor.

Options:
  --backend KIND       udev (real hardware), winit (a window) or headless;
                       default: winit inside another session, else udev
  --headless-size WxH  size of the headless output (default 1920x1080)
  --headless-refresh HZ
                       refresh rate of the headless output (default 60)
  --headless-physical-size WxH
                       size in millimetres the headless output reports, as
                       a monitor's EDID does (default: none)
  --socket NAME        Wayland socket name (default: the first free wayland-N)
  --config FILE        read only this configuration file
  --no-shell           do not start the shell
  --test-input         accept input-injection commands on trusted control
                       connections (tests only)
  --print-env          print WAYLAND_DISPLAY and BOSWAS_COMPOSITOR_SOCKET
                       once the compositor is ready
  --send COMMAND       send a command (version, lock, reload-config, outputs,
                       capture) to a running compositor, print its reply
                       and exit
  --control-socket PATH
                       the control socket --send talks to (default: the one
                       of WAYLAND_DISPLAY in XDG_RUNTIME_DIR)
  -h, --help           show this help
  -V, --version        show the version

COMMAND is started as an ordinary client once the compositor is ready.

Exit status: 0 the session ended as asked; 3 the compositor could not
start (no usable seat, no graphics device, no output); 4 the login screen
kept failing; 2 wrong usage; 1 any other error.
";

#[derive(Debug, Default)]
struct Args {
    backend: Option<String>,
    headless_size: Option<(i32, i32)>,
    /// Millihertz.
    headless_refresh: Option<i32>,
    /// Millimetres.
    headless_physical_size: Option<(i32, i32)>,
    socket: Option<String>,
    config: Option<PathBuf>,
    no_shell: bool,
    test_input: bool,
    print_env: bool,
    send: Option<String>,
    control_socket: Option<PathBuf>,
    command: Vec<String>,
}

fn parse_args(mut argv: impl Iterator<Item = String>) -> Result<Option<Args>, String> {
    let mut args = Args::default();
    while let Some(arg) = argv.next() {
        let mut value = |name: &str| argv.next().ok_or_else(|| format!("{name} needs a value"));
        match arg.as_str() {
            "-h" | "--help" => {
                print!("{USAGE}");
                return Ok(None);
            }
            "-V" | "--version" => {
                println!("boswas-compositor {}", env!("CARGO_PKG_VERSION"));
                return Ok(None);
            }
            "--backend" => args.backend = Some(value("--backend")?),
            "--headless-size" => {
                let text = value("--headless-size")?;
                let size = text
                    .split_once('x')
                    .and_then(|(w, h)| Some((w.parse().ok()?, h.parse().ok()?)))
                    .ok_or_else(|| format!("--headless-size: \"{text}\" is not WIDTHxHEIGHT"))?;
                args.headless_size = Some(size);
            }
            "--headless-refresh" => {
                let text = value("--headless-refresh")?;
                let hertz = text
                    .parse::<f64>()
                    .ok()
                    .filter(|hertz| (1.0..=1000.0).contains(hertz))
                    .ok_or_else(|| format!("--headless-refresh: \"{text}\" is not a rate from 1 to 1000 Hz"))?;
                args.headless_refresh = Some((hertz * 1000.0).round() as i32);
            }
            "--headless-physical-size" => {
                let text = value("--headless-physical-size")?;
                let size = text
                    .split_once('x')
                    .and_then(|(w, h)| Some((w.parse().ok()?, h.parse().ok()?)))
                    .filter(|(w, h): &(i32, i32)| (0..=10_000).contains(w) && (0..=10_000).contains(h))
                    .ok_or_else(|| format!("--headless-physical-size: \"{text}\" is not WIDTHxHEIGHT in millimetres"))?;
                args.headless_physical_size = Some(size);
            }
            "--socket" => args.socket = Some(value("--socket")?),
            "--config" => args.config = Some(PathBuf::from(value("--config")?)),
            "--no-shell" => args.no_shell = true,
            "--test-input" => args.test_input = true,
            "--print-env" => args.print_env = true,
            "--send" => args.send = Some(value("--send")?),
            "--control-socket" => args.control_socket = Some(PathBuf::from(value("--control-socket")?)),
            "--" => {
                args.command = argv.collect();
                break;
            }
            other => return Err(format!("unknown option: {other}")),
        }
    }
    Ok(Some(args))
}

fn main() {
    let args = match parse_args(std::env::args().skip(1)) {
        Ok(Some(args)) => args,
        Ok(None) => return,
        Err(message) => {
            eprintln!("boswas-compositor: {message}\n\n{USAGE}");
            std::process::exit(exit::USAGE);
        }
    };
    if let Some(command) = &args.send {
        match send(command, args.control_socket.as_deref()) {
            Ok(reply) => {
                println!("{reply}");
                let ok = serde_json::from_str::<serde_json::Value>(&reply).is_ok_and(|v| v["ok"] == true);
                std::process::exit(if ok { exit::NORMAL } else { exit::ERROR });
            }
            Err(message) => {
                eprintln!("boswas-compositor: {message}");
                std::process::exit(exit::ERROR);
            }
        }
    }
    let filter = tracing_subscriber::EnvFilter::try_from_env("BOSWAS_LOG")
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    tracing_subscriber::fmt().with_env_filter(filter).with_writer(std::io::stderr).init();

    // A failure is one line on standard error, and a status that tells the
    // caller what kind it was.
    if let Err(err) = run(args) {
        error!("{err}");
        std::process::exit(exit::status_of(err.as_ref()));
    }
}

/// Sends one command to a running compositor's public control socket and
/// returns its reply line.
fn send(command: &str, socket: Option<&std::path::Path>) -> Result<String, String> {
    use std::io::{BufRead, BufReader, Write};
    let path = match socket {
        Some(path) => path.to_owned(),
        None => {
            let runtime = std::env::var_os("XDG_RUNTIME_DIR").ok_or("XDG_RUNTIME_DIR is not set")?;
            let display = std::env::var("WAYLAND_DISPLAY").map_err(|_| "WAYLAND_DISPLAY is not set")?;
            PathBuf::from(runtime).join(format!("boswas-{display}.sock"))
        }
    };
    let mut stream =
        std::os::unix::net::UnixStream::connect(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    let timeout = Some(Duration::from_secs(5));
    stream.set_read_timeout(timeout).and_then(|()| stream.set_write_timeout(timeout)).map_err(|e| e.to_string())?;
    let request = serde_json::json!({ "cmd": command }).to_string() + "\n";
    stream.write_all(request.as_bytes()).map_err(|e| e.to_string())?;
    let mut reply = String::new();
    BufReader::new(stream).read_line(&mut reply).map_err(|e| e.to_string())?;
    if reply.is_empty() {
        return Err("the compositor closed the connection".into());
    }
    Ok(reply.trim_end().to_owned())
}

fn run(args: Args) -> Result<(), Box<dyn std::error::Error>> {
    let (mut config, warnings) = match &args.config {
        Some(path) => Config::load_from(&[path.as_path()], None),
        None => Config::load(),
    };
    for warning in warnings {
        warn!("configuration: {warning}");
    }
    if args.no_shell {
        config.shell.command.clear();
    }

    let backend = args.backend.clone().unwrap_or_else(|| {
        let nested = std::env::var_os("WAYLAND_DISPLAY").is_some() || std::env::var_os("DISPLAY").is_some();
        if nested { "winit" } else { "udev" }.to_owned()
    });

    let mut event_loop: EventLoop<'static, Boswas> = EventLoop::try_new()?;
    let display: Display<Boswas> = Display::new()?;
    let mut state = Boswas::new(
        display,
        event_loop.handle(),
        event_loop.get_signal(),
        config,
        Backend::None,
        args.socket.as_deref(),
    )?;
    state.ipc.test_input = args.test_input;

    match backend.as_str() {
        "headless" => {
            let (w, h) = args.headless_size.unwrap_or((1920, 1080));
            backend::headless::init(&mut state, w, h, args.headless_refresh, args.headless_physical_size.unwrap_or((0, 0)))?;
        }
        #[cfg(feature = "winit")]
        "winit" => backend::winit::init(&mut state)?,
        #[cfg(feature = "udev")]
        "udev" => backend::udev::init(&mut event_loop, &mut state)?,
        other => return Err(format!("backend \"{other}\" is not available in this build").into()),
    }

    if let Err(err) = state.ipc_listen() {
        warn!("no public control socket: {err}");
    }
    info!(
        "boswas-compositor {} ready on {} ({backend} backend)",
        env!("CARGO_PKG_VERSION"),
        state.socket_name.to_string_lossy()
    );
    if args.print_env {
        println!("WAYLAND_DISPLAY={}", state.socket_name.to_string_lossy());
        if let Some(path) = &state.ipc.socket_path {
            println!("BOSWAS_COMPOSITOR_SOCKET={}", path.display());
        }
        use std::io::Write;
        let _ = std::io::stdout().flush();
    }

    state.refresh_layouts();
    state.apply_night_light();
    // Before the shell, so that everything it starts knows the X display.
    // (The login screen runs no X server.)
    state.start_xwayland();
    state.start_shell();
    state.start_input_method();
    if !args.command.is_empty() {
        state.spawn(&args.command);
    }

    // Housekeeping once a second: idle lock, lock screen supervision,
    // exited children.
    state
        .loop_handle
        .insert_source(Timer::from_duration(Duration::from_secs(1)), |_, _, state| {
            state.check_idle();
            state.supervise_lock();
            // The night light schedule.
            state.apply_night_light();
            state.check_screen_off();
            state.reap_children();
            state.screencopy_tick();
            TimeoutAction::ToDuration(Duration::from_secs(1))
        })?;

    event_loop.run(None, &mut state, |state| {
        state.space.refresh();
        state.popups.cleanup();
        state.ipc_flush();
        Backend::tick(state);
        if let Err(err) = state.display_handle.flush_clients() {
            warn!("flushing Wayland clients failed: {err}");
        }
    })?;

    if let Some(path) = &state.ipc.socket_path {
        let _ = std::fs::remove_file(path);
    }
    if let Some(failure) = state.failure.take() {
        return Err(failure.into());
    }
    info!("boswas-compositor stopped");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Option<Args>, String> {
        parse_args(args.iter().map(|s| s.to_string()))
    }

    #[test]
    fn options_and_command_are_separated_by_a_double_dash() {
        let args = parse(&["--backend", "headless", "--headless-size", "1280x720", "--test-input", "--", "foot", "-e", "top"])
            .unwrap()
            .unwrap();
        assert_eq!(args.backend.as_deref(), Some("headless"));
        assert_eq!(args.headless_size, Some((1280, 720)));
        assert_eq!(args.headless_refresh, None, "60 Hz unless asked");
        let fast = parse(&["--backend", "headless", "--headless-refresh", "143.856"]).unwrap().unwrap();
        assert_eq!(fast.headless_refresh, Some(143_856));
        assert_eq!(fast.headless_physical_size, None, "no size unless asked");
        let sized = parse(&["--headless-physical-size", "597x336"]).unwrap().unwrap();
        assert_eq!(sized.headless_physical_size, Some((597, 336)));
        assert!(args.test_input);
        assert_eq!(args.command, ["foot", "-e", "top"]);
    }

    #[test]
    fn bad_arguments_are_rejected() {
        assert!(parse(&["--frobnicate"]).is_err());
        assert!(parse(&["--backend"]).is_err());
        assert!(parse(&["--headless-size", "big"]).is_err());
        assert!(parse(&["--headless-size", "12xx"]).is_err());
        for size in ["big", "10x", "-5x300", "20000x10"] {
            assert!(parse(&["--headless-physical-size", size]).is_err(), "{size}");
        }
        for rate in ["0", "1001", "fast", "-60", "NaN"] {
            assert!(parse(&["--headless-refresh", rate]).is_err(), "{rate}");
        }
        assert!(parse(&["--send"]).is_err());
    }

    #[test]
    fn send_reports_a_missing_compositor() {
        let args = parse(&["--send", "lock", "--control-socket", "/nonexistent/boswas.sock"]).unwrap().unwrap();
        assert_eq!(args.send.as_deref(), Some("lock"));
        let error = send("lock", args.control_socket.as_deref()).unwrap_err();
        assert!(error.contains("/nonexistent/boswas.sock"), "{error}");
    }
}
