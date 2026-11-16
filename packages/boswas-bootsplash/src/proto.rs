//! The local control protocol between the client and the daemon.
//!
//! A frame is a 16-bit big-endian length followed by that many bytes, at most
//! [`MAX_FRAME`]. A request is a verb, optionally followed by a space and an
//! argument (`PING`, `ASK Please unlock disk`, `QUIT RETAIN 6`). A response
//! is `OK`, `ERR reason`, or `PASS` and the passphrase bytes. Every length is
//! bounded and every parser returns an error instead of panicking: the peer
//! is root, but a confused peer must not be able to take the boot screen down.

use std::fmt;
use std::io::{self, Read, Write};

use zeroize::{Zeroize, Zeroizing};

use crate::scene::Mode;

/// Largest frame payload, in bytes.
pub const MAX_FRAME: usize = 4096;
/// Longest prompt or message text, in bytes.
pub const MAX_TEXT: usize = 1024;
/// Longest time `QUIT RETAIN` keeps the last frame, in seconds.
pub const MAX_RETAIN_SECS: u16 = 60;

/// Why a frame or payload was rejected.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProtoError {
    /// The length prefix was zero or above [`MAX_FRAME`].
    BadLength(usize),
    /// The payload is not a known, well-formed request or response.
    Malformed(&'static str),
}

impl fmt::Display for ProtoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ProtoError::BadLength(n) => write!(f, "bad frame length {n}"),
            ProtoError::Malformed(what) => write!(f, "malformed message: {what}"),
        }
    }
}

impl std::error::Error for ProtoError {}

/// What a client can ask of the daemon.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    /// Is the boot screen up?
    Ping,
    /// Show a passphrase prompt; the reply arrives when Enter is pressed.
    Ask { prompt: String },
    /// Show a message line (empty text clears it).
    Message { text: String },
    /// Boot progress in percent.
    Progress(u8),
    /// Switch between the boot, shutdown and reboot screens.
    Mode(Mode),
    /// Give the display back to the text console.
    Hide,
    /// Take the display again.
    Show,
    /// Leave. With `retain`, keep the last frame on screen for up to that
    /// many seconds, until the next program takes the display.
    Quit { retain: Option<u16> },
    /// The root file system moved (initramfs hand-over).
    NewRoot { path: String },
    /// Save the current frame as `NAME.png` (headless mode only).
    Snapshot { name: String },
}

/// The daemon's answer.
#[derive(Clone, PartialEq, Eq)]
pub enum Response {
    Ok,
    Error(String),
    Password(Zeroizing<Vec<u8>>),
}

impl fmt::Debug for Response {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Response::Ok => f.write_str("Ok"),
            Response::Error(e) => write!(f, "Error({e:?})"),
            Response::Password(_) => f.write_str("Password(..)"),
        }
    }
}

fn text(arg: &[u8], limit: usize) -> Result<String, ProtoError> {
    if arg.len() > limit {
        return Err(ProtoError::Malformed("text too long"));
    }
    Ok(String::from_utf8_lossy(arg).into_owned())
}

fn number(arg: &[u8]) -> Result<u32, ProtoError> {
    if arg.is_empty() || arg.len() > 5 || !arg.iter().all(u8::is_ascii_digit) {
        return Err(ProtoError::Malformed("not a number"));
    }
    std::str::from_utf8(arg).ok().and_then(|s| s.parse().ok()).ok_or(ProtoError::Malformed("not a number"))
}

fn split(payload: &[u8]) -> (&[u8], &[u8]) {
    match payload.iter().position(|&b| b == b' ') {
        Some(i) => (&payload[..i], &payload[i + 1..]),
        None => (payload, &[]),
    }
}

impl Request {
    /// The payload of this request (without the length prefix).
    pub fn encode(&self) -> Vec<u8> {
        match self {
            Request::Ping => b"PING".to_vec(),
            Request::Ask { prompt } => [b"ASK ", truncate(prompt, MAX_TEXT)].concat(),
            Request::Message { text } => [b"MSG ", truncate(text, MAX_TEXT)].concat(),
            Request::Progress(p) => format!("PROGRESS {}", (*p).min(100)).into_bytes(),
            Request::Mode(Mode::Boot) => b"MODE boot".to_vec(),
            Request::Mode(Mode::Shutdown) => b"MODE shutdown".to_vec(),
            Request::Mode(Mode::Reboot) => b"MODE reboot".to_vec(),
            Request::Hide => b"HIDE".to_vec(),
            Request::Show => b"SHOW".to_vec(),
            Request::Quit { retain: None } => b"QUIT".to_vec(),
            Request::Quit { retain: Some(secs) } => {
                format!("QUIT RETAIN {}", (*secs).min(MAX_RETAIN_SECS)).into_bytes()
            }
            Request::NewRoot { path } => [b"ROOT ", truncate(path, MAX_TEXT)].concat(),
            Request::Snapshot { name } => [b"SNAPSHOT ", truncate(name, 64)].concat(),
        }
    }

    /// Parse a request payload.
    pub fn parse(payload: &[u8]) -> Result<Request, ProtoError> {
        if payload.is_empty() || payload.len() > MAX_FRAME {
            return Err(ProtoError::BadLength(payload.len()));
        }
        let (verb, arg) = split(payload);
        match verb {
            b"PING" if arg.is_empty() => Ok(Request::Ping),
            b"ASK" => Ok(Request::Ask { prompt: text(arg, MAX_TEXT)? }),
            b"MSG" => Ok(Request::Message { text: text(arg, MAX_TEXT)? }),
            b"PROGRESS" => match number(arg)? {
                n @ 0..=100 => Ok(Request::Progress(n as u8)),
                _ => Err(ProtoError::Malformed("progress is 0 to 100")),
            },
            b"MODE" => std::str::from_utf8(arg)
                .ok()
                .and_then(Mode::parse)
                .map(Request::Mode)
                .ok_or(ProtoError::Malformed("unknown mode")),
            b"HIDE" if arg.is_empty() => Ok(Request::Hide),
            b"SHOW" if arg.is_empty() => Ok(Request::Show),
            b"QUIT" => {
                if arg.is_empty() {
                    return Ok(Request::Quit { retain: None });
                }
                let (word, secs) = split(arg);
                if word != b"RETAIN" {
                    return Err(ProtoError::Malformed("QUIT takes RETAIN [seconds]"));
                }
                let secs = if secs.is_empty() { 6 } else { number(secs)? };
                if secs > MAX_RETAIN_SECS as u32 {
                    return Err(ProtoError::Malformed("retain time too long"));
                }
                Ok(Request::Quit { retain: Some(secs as u16) })
            }
            b"ROOT" => {
                let path = text(arg, MAX_TEXT)?;
                if !path.starts_with('/') || path.contains('\0') {
                    return Err(ProtoError::Malformed("root must be an absolute path"));
                }
                Ok(Request::NewRoot { path })
            }
            b"SNAPSHOT" => {
                let ok = !arg.is_empty()
                    && arg.len() <= 64
                    && arg[0] != b'.'
                    && arg.iter().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'));
                if !ok {
                    return Err(ProtoError::Malformed("bad snapshot name"));
                }
                Ok(Request::Snapshot { name: String::from_utf8_lossy(arg).into_owned() })
            }
            _ => Err(ProtoError::Malformed("unknown request")),
        }
    }
}

fn truncate(s: &str, limit: usize) -> &[u8] {
    let mut end = s.len().min(limit);
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s.as_bytes()[..end]
}

impl Response {
    /// The payload of this response. A passphrase payload is wiped on drop.
    pub fn encode(&self) -> Zeroizing<Vec<u8>> {
        Zeroizing::new(match self {
            Response::Ok => b"OK".to_vec(),
            Response::Error(reason) => [b"ERR ", truncate(reason, 256)].concat(),
            Response::Password(pass) => {
                let mut out = Vec::with_capacity(5 + pass.len());
                out.extend_from_slice(b"PASS ");
                out.extend_from_slice(pass);
                out
            }
        })
    }

    /// Parse a response payload.
    pub fn parse(payload: &[u8]) -> Result<Response, ProtoError> {
        if payload.is_empty() || payload.len() > MAX_FRAME {
            return Err(ProtoError::BadLength(payload.len()));
        }
        if payload == b"OK" {
            return Ok(Response::Ok);
        }
        if let Some(reason) = payload.strip_prefix(b"ERR") {
            let reason = reason.strip_prefix(b" ").unwrap_or(reason);
            return Ok(Response::Error(String::from_utf8_lossy(reason).into_owned()));
        }
        if payload == b"PASS" {
            return Ok(Response::Password(Zeroizing::new(Vec::new())));
        }
        if let Some(pass) = payload.strip_prefix(b"PASS ") {
            return Ok(Response::Password(Zeroizing::new(pass.to_vec())));
        }
        Err(ProtoError::Malformed("unknown response"))
    }
}

/// Prefix a payload with its length. The result is wiped on drop.
pub fn frame(payload: &[u8]) -> Result<Zeroizing<Vec<u8>>, ProtoError> {
    if payload.is_empty() || payload.len() > MAX_FRAME {
        return Err(ProtoError::BadLength(payload.len()));
    }
    let mut out = Zeroizing::new(Vec::with_capacity(payload.len() + 2));
    out.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    out.extend_from_slice(payload);
    Ok(out)
}

/// Reassembles frames from a byte stream that arrives in arbitrary pieces.
#[derive(Default)]
pub struct FrameReader {
    buf: Vec<u8>,
}

impl fmt::Debug for FrameReader {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "FrameReader({} bytes buffered)", self.buf.len())
    }
}

impl FrameReader {
    pub fn new() -> Self {
        Self::default()
    }

    /// Add bytes read from the peer.
    pub fn feed(&mut self, bytes: &[u8]) {
        self.buf.extend_from_slice(bytes);
    }

    /// The next complete frame's payload, if one has arrived. An error means
    /// the stream is unusable and the connection must be closed.
    pub fn next_frame(&mut self) -> Result<Option<Zeroizing<Vec<u8>>>, ProtoError> {
        if self.buf.len() < 2 {
            return Ok(None);
        }
        let len = u16::from_be_bytes([self.buf[0], self.buf[1]]) as usize;
        if len == 0 || len > MAX_FRAME {
            self.buf.zeroize();
            return Err(ProtoError::BadLength(len));
        }
        if self.buf.len() < 2 + len {
            return Ok(None);
        }
        let payload = Zeroizing::new(self.buf[2..2 + len].to_vec());
        let mut rest = self.buf.split_off(2 + len);
        std::mem::swap(&mut self.buf, &mut rest);
        rest.zeroize();
        Ok(Some(payload))
    }
}

impl Drop for FrameReader {
    fn drop(&mut self) {
        self.buf.zeroize();
    }
}

/// Write one frame (blocking).
pub fn write_frame<W: Write>(mut out: W, payload: &[u8]) -> io::Result<()> {
    let framed = frame(payload).map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
    out.write_all(&framed)?;
    out.flush()
}

/// Read one frame (blocking). End of stream is `UnexpectedEof`.
pub fn read_frame<R: Read>(mut input: R) -> io::Result<Zeroizing<Vec<u8>>> {
    let mut header = [0u8; 2];
    input.read_exact(&mut header)?;
    let len = u16::from_be_bytes(header) as usize;
    if len == 0 || len > MAX_FRAME {
        return Err(io::Error::new(io::ErrorKind::InvalidData, ProtoError::BadLength(len)));
    }
    let mut payload = Zeroizing::new(vec![0u8; len]);
    input.read_exact(&mut payload)?;
    Ok(payload)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requests_round_trip() {
        let requests = [
            Request::Ping,
            Request::Ask { prompt: "Please unlock disk sda3_crypt".into() },
            Request::Ask { prompt: String::new() },
            Request::Message { text: "cryptsetup: sda3_crypt: set up successfully".into() },
            Request::Message { text: String::new() },
            Request::Progress(0),
            Request::Progress(100),
            Request::Mode(Mode::Boot),
            Request::Mode(Mode::Shutdown),
            Request::Mode(Mode::Reboot),
            Request::Hide,
            Request::Show,
            Request::Quit { retain: None },
            Request::Quit { retain: Some(6) },
            Request::NewRoot { path: "/root".into() },
            Request::Snapshot { name: "boot-1".into() },
        ];
        for request in requests {
            assert_eq!(Request::parse(&request.encode()), Ok(request.clone()), "{request:?}");
        }
    }

    #[test]
    fn responses_round_trip() {
        for response in [
            Response::Ok,
            Response::Error("busy".into()),
            Response::Password(Zeroizing::new(b"correct horse".to_vec())),
            Response::Password(Zeroizing::new(Vec::new())),
            Response::Password(Zeroizing::new(vec![0, 255, b' ', b'\n'])),
        ] {
            assert_eq!(Response::parse(&response.encode()), Ok(response.clone()));
        }
        assert_eq!(format!("{:?}", Response::Password(Zeroizing::new(b"secret".to_vec()))), "Password(..)");
    }

    #[test]
    fn malformed_requests_are_rejected() {
        for bad in [
            &b""[..],
            b"ping",
            b"PING now",
            b"PROGRESS",
            b"PROGRESS 101",
            b"PROGRESS -1",
            b"PROGRESS 1e3",
            b"PROGRESS 999999",
            b"MODE",
            b"MODE updates",
            b"HIDE 1",
            b"QUIT NOW",
            b"QUIT RETAIN x",
            b"QUIT RETAIN 61",
            b"ROOT relative/path",
            b"ROOT",
            b"SNAPSHOT",
            b"SNAPSHOT ../etc/passwd",
            b"SNAPSHOT .hidden",
            b"SNAPSHOT a/b",
            b"\xff\xfe",
            b"EXEC /bin/sh",
        ] {
            assert!(Request::parse(bad).is_err(), "{:?}", String::from_utf8_lossy(bad));
        }
        assert!(Request::parse(&[b'A'; MAX_FRAME + 1]).is_err());
        let long = [b"ASK ".as_slice(), &[b'x'; MAX_TEXT + 1]].concat();
        assert!(Request::parse(&long).is_err());
        assert!(Response::parse(b"WHAT").is_err());
        assert!(Response::parse(b"").is_err());
    }

    #[test]
    fn invalid_utf8_text_is_made_printable() {
        let request = Request::parse(b"MSG caf\xe9").unwrap();
        assert_eq!(request, Request::Message { text: "caf\u{fffd}".into() });
    }

    #[test]
    fn long_text_is_truncated_on_a_character_boundary_when_encoding() {
        let prompt = "\u{e9}".repeat(MAX_TEXT); // 2 bytes each
        let payload = Request::Ask { prompt }.encode();
        assert!(payload.len() <= 4 + MAX_TEXT);
        assert!(std::str::from_utf8(&payload).is_ok());
        assert!(Request::parse(&payload).is_ok());
    }

    #[test]
    fn frames_reassemble_from_any_split() {
        let a = frame(&Request::Ping.encode()).unwrap();
        let b = frame(&Request::Message { text: "hello".into() }.encode()).unwrap();
        let stream: Vec<u8> = [a.as_slice(), b.as_slice()].concat();
        for cut in 0..=stream.len() {
            let mut reader = FrameReader::new();
            let mut got = Vec::new();
            for part in [&stream[..cut], &stream[cut..]] {
                reader.feed(part);
                while let Some(payload) = reader.next_frame().unwrap() {
                    got.push(Request::parse(&payload).unwrap());
                }
            }
            assert_eq!(got, vec![Request::Ping, Request::Message { text: "hello".into() }], "cut at {cut}");
        }
    }

    #[test]
    fn bad_lengths_close_the_stream() {
        let mut reader = FrameReader::new();
        reader.feed(&[0, 0, b'x']);
        assert_eq!(reader.next_frame(), Err(ProtoError::BadLength(0)));
        let mut reader = FrameReader::new();
        reader.feed(&[0xff, 0xff]);
        assert_eq!(reader.next_frame(), Err(ProtoError::BadLength(0xffff)));
        assert!(frame(&[]).is_err());
        assert!(frame(&vec![0; MAX_FRAME + 1]).is_err());
        assert!(frame(&vec![0; MAX_FRAME]).is_ok());
    }

    #[test]
    fn blocking_helpers_round_trip() {
        let mut wire = Vec::new();
        write_frame(&mut wire, b"PING").unwrap();
        write_frame(&mut wire, b"OK").unwrap();
        let mut cursor = std::io::Cursor::new(wire);
        assert_eq!(&**read_frame(&mut cursor).unwrap(), b"PING");
        assert_eq!(&**read_frame(&mut cursor).unwrap(), b"OK");
        assert_eq!(read_frame(&mut cursor).unwrap_err().kind(), io::ErrorKind::UnexpectedEof);
        let mut bad = std::io::Cursor::new(vec![0xff, 0xff, 1, 2, 3]);
        assert_eq!(read_frame(&mut bad).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn random_bytes_never_panic() {
        let mut seed = 0x2545_f491_4f6c_dd1du64;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        for _ in 0..20_000 {
            let len = (next() % 64) as usize;
            let bytes: Vec<u8> = (0..len).map(|_| next() as u8).collect();
            let _ = Request::parse(&bytes);
            let _ = Response::parse(&bytes);
            let mut reader = FrameReader::new();
            reader.feed(&bytes);
            while let Ok(Some(payload)) = reader.next_frame() {
                let _ = Request::parse(&payload);
            }
        }
        // Verbs followed by random arguments.
        for verb in ["ASK", "MSG", "PROGRESS", "MODE", "QUIT", "ROOT", "SNAPSHOT", "PING", "HIDE", "SHOW"] {
            for _ in 0..2000 {
                let len = (next() % 40) as usize;
                let mut bytes = verb.as_bytes().to_vec();
                bytes.push(b' ');
                bytes.extend((0..len).map(|_| next() as u8));
                let _ = Request::parse(&bytes);
            }
        }
    }
}
