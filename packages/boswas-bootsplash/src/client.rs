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

/// Send one request and wait for the answer.
pub fn request(socket: &Path, request: &Request) -> io::Result<Response> {
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(Some(REPLY_TIMEOUT))?;
    stream.set_write_timeout(Some(REPLY_TIMEOUT))?;
    proto::write_frame(&mut stream, &request.encode())?;
    let payload = proto::read_frame(&mut stream)?;
    Response::parse(&payload).map_err(invalid)
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
