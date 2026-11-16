//! Talking to the daemon from the command line client.

use std::io::{self, Read};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

use zeroize::Zeroizing;

use crate::proto::{self, FrameReader, Request, Response};

/// The control socket of the system's boot screen.
pub const DEFAULT_SOCKET: &str = "/run/boswas-bootsplash/socket";

/// How long a client waits for an answer to an ordinary request.
const REPLY_TIMEOUT: Duration = Duration::from_secs(3);

fn invalid(e: proto::ProtoError) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, e)
}

fn exchange(stream: &mut UnixStream, request: &Request) -> io::Result<Response> {
    stream.set_read_timeout(Some(REPLY_TIMEOUT))?;
    stream.set_write_timeout(Some(REPLY_TIMEOUT))?;
    proto::write_frame(&mut *stream, &request.encode())?;
    let payload = proto::read_frame(&mut *stream)?;
    Response::parse(&payload).map_err(invalid)
}

/// Send one request and wait for the answer.
pub fn request(socket: &Path, request: &Request) -> io::Result<Response> {
    exchange(&mut UnixStream::connect(socket)?, request)
}

/// How long a boot screen that was told to end by a signal gets to do so.
const END_TIMEOUT: Duration = Duration::from_secs(2);

/// End the boot screen, whatever state it is in. A boot screen that answers
/// is asked to quit (keeping its last frame for `retain` seconds, see the
/// `quit` request). One that accepts the connection but does not answer in
/// time is busy or stopped; it would keep the display and the console for
/// ever, and the login screen could not start. It is told to terminate,
/// and killed if that does not end it either: its supervisor then puts the
/// text console back. The process is the one that listens on the socket
/// (the kernel names it), so no pid file is needed to find it. Only when
/// the socket itself is gone is the pid file consulted, and the process it
/// names ended the same way if it is a boot screen.
///
/// An error means there was no boot screen to end, or it could not be ended.
pub fn quit(socket: &Path, retain: Option<u16>) -> io::Result<Response> {
    let mut stream = match UnixStream::connect(socket) {
        Ok(stream) => stream,
        Err(unreachable) => {
            return match boot_screen_of_pid_file(socket) {
                Some(pid) if terminate(pid) => Ok(Response::Ok),
                _ => Err(unreachable),
            };
        }
    };
    let listener = rustix::net::sockopt::socket_peercred(&stream).ok().map(|peer| peer.pid);
    let unanswered = match exchange(&mut stream, &Request::Quit { retain }) {
        Ok(response) => return Ok(response),
        Err(e) => e,
    };
    match listener {
        Some(pid) if terminate(pid) => Ok(Response::Ok),
        _ => Err(unanswered),
    }
}

/// The process named by the pid file next to `socket`, if it exists and is
/// a boot screen daemon (by the name it runs under).
fn boot_screen_of_pid_file(socket: &Path) -> Option<rustix::process::Pid> {
    let text = std::fs::read_to_string(socket.with_file_name(crate::runtime::PID_FILE)).ok()?;
    let number: i32 = text.trim().parse().ok()?;
    let name = std::fs::read(format!("/proc/{number}/cmdline")).ok()?;
    let name = name.split(|byte| *byte == 0).next()?;
    if !String::from_utf8_lossy(name).contains("boswas-bootsplashd") {
        return None;
    }
    rustix::process::Pid::from_raw(number)
}

/// End a process: ask it to terminate, kill it if it is still there after
/// [`END_TIMEOUT`]. True once it is gone.
fn terminate(pid: rustix::process::Pid) -> bool {
    use rustix::io::Errno;
    use rustix::process::{kill_process, test_kill_process, Signal};
    if Some(pid) == rustix::process::Pid::from_raw(std::process::id() as i32) {
        return false;
    }
    for signal in [Signal::TERM, Signal::KILL] {
        match kill_process(pid, signal) {
            Ok(()) => {}
            Err(Errno::SRCH) => return true,
            Err(_) => return false,
        }
        let deadline = std::time::Instant::now() + END_TIMEOUT;
        while std::time::Instant::now() < deadline {
            if matches!(test_kill_process(pid), Err(Errno::SRCH)) || ended(pid) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    false
}

/// Whether a process has ended and only waits to be collected by its
/// parent (it still has a process number then, but holds nothing).
fn ended(pid: rustix::process::Pid) -> bool {
    match std::fs::read_to_string(format!("/proc/{}/stat", pid.as_raw_nonzero())) {
        // The state is the field after the name, which is in parentheses.
        Ok(stat) => stat.rsplit(')').next().and_then(|rest| rest.split_whitespace().next()) == Some("Z"),
        Err(_) => false,
    }
}

/// True when the daemon answers.
pub fn ping(socket: &Path) -> bool {
    matches!(request(socket, &Request::Ping), Ok(Response::Ok))
}

/// Ask for a passphrase and wait, without a time limit, until the user has
/// entered it. `still_wanted` is checked about twice a second; when it turns
/// false the prompt is withdrawn and `Ok(None)` is returned.
pub fn ask_password(
    socket: &Path,
    prompt: &str,
    mut still_wanted: impl FnMut() -> bool,
) -> io::Result<Option<Zeroizing<Vec<u8>>>> {
    let mut stream = UnixStream::connect(socket)?;
    stream.set_write_timeout(Some(REPLY_TIMEOUT))?;
    proto::write_frame(&mut stream, &Request::Ask { prompt: prompt.to_owned() }.encode())?;
    stream.set_read_timeout(Some(Duration::from_millis(500)))?;
    let mut reader = FrameReader::new();
    let mut buf = Zeroizing::new([0u8; 2048]);
    loop {
        if let Some(payload) = reader.next_frame().map_err(invalid)? {
            return match Response::parse(&payload).map_err(invalid)? {
                Response::Password(pass) => Ok(Some(pass)),
                Response::Error(reason) => Err(io::Error::other(reason)),
                Response::Ok => Err(io::Error::new(io::ErrorKind::InvalidData, "unexpected reply")),
            };
        }
        match stream.read(&mut buf[..]) {
            Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "the boot screen went away")),
            Ok(n) => reader.feed(&buf[..n]),
            Err(e)
                if matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut | io::ErrorKind::Interrupted
                ) =>
            {
                if !still_wanted() {
                    return Ok(None);
                }
            }
            Err(e) => return Err(e),
        }
    }
}
