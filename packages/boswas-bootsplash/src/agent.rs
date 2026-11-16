//! Password agent for systemd, so that passphrases asked for after the
//! initramfs (a second encrypted volume, for example) appear on the boot
//! screen instead of on a text console hidden behind it.
//!
//! This follows systemd's Password Agents specification: requests are
//! `ask.*` files in `/run/systemd/ask-password/`, each naming a datagram
//! socket in the same directory to which the answer is sent: `+` followed
//! by the passphrase, or `-` to decline. The agent runs only while the boot
//! screen does and exits when it is gone.

use std::collections::HashSet;
use std::os::unix::net::UnixDatagram;
use std::path::{Path, PathBuf};
use std::time::Duration;

use rustix::event::{poll, PollFd, PollFlags, Timespec};
use rustix::fs::inotify;
use zeroize::Zeroizing;

use crate::client;

/// Where systemd publishes password requests.
pub const ASK_DIR: &str = "/run/systemd/ask-password";

/// One request, as far as this agent cares.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AskRequest {
    pub message: String,
    pub socket: PathBuf,
    /// CLOCK_MONOTONIC microseconds after which the request is void (0: never).
    pub not_after: u64,
}

/// Parse an `ask.*` file. Returns `None` for anything that is not a
/// well-formed request with a reply socket inside the ask-password directory.
pub fn parse_ask(contents: &str, dir: &Path) -> Option<AskRequest> {
    let mut in_ask = false;
    let (mut message, mut socket, mut not_after) = (None, None, 0u64);
    for line in contents.lines().take(64) {
        let line = line.trim();
        if line.starts_with('[') {
            in_ask = line == "[Ask]";
            continue;
        }
        if !in_ask {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else { continue };
        match key {
            "Message" => message = Some(value.to_owned()),
            "Socket" => socket = Some(PathBuf::from(value)),
            "NotAfter" => not_after = value.parse().unwrap_or(0),
            _ => {}
        }
    }
    let socket = socket?;
    // The reply socket is created by systemd next to the request file.
    let inside = socket.parent() == Some(dir)
        && socket.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("sck.") && !n.contains(".."));
    if !inside {
        return None;
    }
    let message = message.unwrap_or_else(|| "Passphrase".into());
    let message = message.trim().trim_end_matches(':').trim().to_owned();
    Some(AskRequest { message, socket, not_after })
}

fn monotonic_micros() -> u64 {
    let now = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
    now.tv_sec as u64 * 1_000_000 + now.tv_nsec as u64 / 1000
}

fn pending(dir: &Path) -> Vec<PathBuf> {
    let mut found: Vec<PathBuf> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("ask.")))
        .collect();
    found.sort();
    found
}

fn answer(request: &AskRequest, passphrase: Option<&[u8]>) {
    let mut reply = Zeroizing::new(Vec::with_capacity(1 + passphrase.map_or(0, <[u8]>::len)));
    match passphrase {
        Some(pass) => {
            reply.push(b'+');
            reply.extend_from_slice(pass);
        }
        None => reply.push(b'-'),
    }
    if let Ok(socket) = UnixDatagram::unbound() {
        let _ = socket.send_to(&reply, &request.socket);
    }
}

/// Serve requests in `dir` through the boot screen at `socket` until the
/// boot screen is gone. Returns the process exit code.
pub fn run(socket: &Path, dir: &Path) -> i32 {
    if !client::ping(socket) {
        return 1;
    }
    let watch = inotify::init(inotify::CreateFlags::CLOEXEC | inotify::CreateFlags::NONBLOCK).ok();
    if let Some(fd) = &watch {
        let _ = inotify::add_watch(fd, dir, inotify::WatchFlags::CLOSE_WRITE | inotify::WatchFlags::MOVED_TO);
    }
    let mut handled: HashSet<PathBuf> = HashSet::new();
    loop {
        let waiting = pending(dir);
        handled.retain(|path| waiting.contains(path));
        for path in waiting {
            if handled.contains(&path) {
                continue;
            }
            let Some(request) = std::fs::read_to_string(&path).ok().and_then(|text| parse_ask(&text, dir)) else {
                continue; // not complete yet, or not for us
            };
            handled.insert(path.clone());
            if request.not_after != 0 && monotonic_micros() > request.not_after {
                continue;
            }
            // Withdraw the prompt if the request disappears (answered
            // elsewhere, or timed out) while the user is still typing.
            let still_wanted = || path.exists() && (request.not_after == 0 || monotonic_micros() <= request.not_after);
            match client::ask_password(socket, &request.message, still_wanted) {
                Ok(Some(pass)) => answer(&request, Some(&pass)),
                Ok(None) => {}
                Err(_) => return 0, // the boot screen is gone: so are we
            }
        }
        // Sleep until the directory changes, checking on the daemon meanwhile.
        match &watch {
            Some(fd) => {
                let spec = Timespec { tv_sec: 1, tv_nsec: 0 };
                let mut fds = [PollFd::new(fd, PollFlags::IN)];
                if poll(&mut fds, Some(&spec)).unwrap_or(0) > 0 {
                    let mut drain = [0u8; 4096];
                    while rustix::io::read(fd, &mut drain).is_ok_and(|n| n > 0) {}
                }
            }
            None => std::thread::sleep(Duration::from_millis(500)),
        }
        if !client::ping(socket) {
            return 0;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DIR: &str = "/run/systemd/ask-password";

    #[test]
    fn parses_a_systemd_request() {
        let text = "[Ask]\nPID=312\nSocket=/run/systemd/ask-password/sck.8b1c2f\nAcceptCached=0\nEcho=0\n\
                    NotAfter=0\nMessage=Please enter passphrase for disk data (luks-1234):\nIcon=drive-harddisk\n\
                    Id=cryptsetup:/dev/sdb1\n";
        let request = parse_ask(text, Path::new(DIR)).unwrap();
        assert_eq!(request.message, "Please enter passphrase for disk data (luks-1234)");
        assert_eq!(request.socket, PathBuf::from("/run/systemd/ask-password/sck.8b1c2f"));
        assert_eq!(request.not_after, 0);
    }

    #[test]
    fn deadline_is_read() {
        let text = "[Ask]\nSocket=/run/systemd/ask-password/sck.1\nNotAfter=123456789\nMessage=Unlock\n";
        assert_eq!(parse_ask(text, Path::new(DIR)).unwrap().not_after, 123_456_789);
    }

    #[test]
    fn refuses_requests_it_should_not_answer() {
        let dir = Path::new(DIR);
        // No socket, a socket elsewhere, a socket with a strange name, keys outside [Ask].
        assert!(parse_ask("[Ask]\nMessage=x\n", dir).is_none());
        assert!(parse_ask("[Ask]\nSocket=/tmp/evil\nMessage=x\n", dir).is_none());
        assert!(parse_ask("[Ask]\nSocket=/run/systemd/ask-password/../private/sck.1\nMessage=x\n", dir).is_none());
        assert!(parse_ask("[Ask]\nSocket=/run/systemd/ask-password/ask.1\nMessage=x\n", dir).is_none());
        assert!(parse_ask("[Other]\nSocket=/run/systemd/ask-password/sck.1\n", dir).is_none());
        assert!(parse_ask("", dir).is_none());
        assert!(parse_ask("\u{0}\u{1}garbage=[Ask]", dir).is_none());
    }

    #[test]
    fn a_missing_message_gets_a_default() {
        let request = parse_ask("[Ask]\nSocket=/run/systemd/ask-password/sck.2\n", Path::new(DIR)).unwrap();
        assert_eq!(request.message, "Passphrase");
    }

    #[test]
    fn pending_lists_only_ask_files() {
        let dir = std::env::temp_dir().join(format!("boswas-bootsplash-agent-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        for name in ["ask.b", "ask.a", "sck.a", "tmp.x"] {
            std::fs::write(dir.join(name), b"").unwrap();
        }
        let names: Vec<String> =
            pending(&dir).iter().map(|p| p.file_name().unwrap().to_string_lossy().into_owned()).collect();
        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(names, ["ask.a", "ask.b"]);
        assert!(pending(Path::new("/nonexistent")).is_empty());
    }

    #[test]
    fn the_answer_format() {
        let dir = crate::socket_test_dir().join(format!("boswas-bootsplash-answer-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("sck.t");
        let receiver = UnixDatagram::bind(&path).unwrap();
        receiver.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let request = AskRequest { message: "m".into(), socket: path, not_after: 0 };
        let mut buf = [0u8; 64];
        answer(&request, Some(b"secret"));
        let n = receiver.recv(&mut buf).unwrap();
        assert_eq!(&buf[..n], b"+secret");
        answer(&request, None);
        let n = receiver.recv(&mut buf).unwrap();
        assert_eq!(&buf[..n], b"-");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
