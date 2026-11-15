//! Client for greetd, the login daemon the greeter talks to.
//!
//! Wire format: a native-endian `u32` length followed by one JSON object
//! (greetd-ipc(7)). A login is `create_session`, then answers to the
//! authentication questions, then `start_session`.

use std::{
    io::{Read, Write},
    os::unix::net::UnixStream,
    path::Path,
    time::Duration,
};

use serde_json::{json, Value};
use zeroize::Zeroizing;

/// Largest greetd message accepted.
const MAX_MESSAGE: u32 = 64 * 1024;
/// Most authentication questions answered in one login.
const MAX_ROUNDS: usize = 8;

fn send(stream: &mut impl Write, message: &Value) -> std::io::Result<()> {
    let body = message.to_string();
    stream.write_all(&(body.len() as u32).to_ne_bytes())?;
    stream.write_all(body.as_bytes())
}

fn receive(stream: &mut impl Read) -> std::io::Result<Value> {
    let mut length = [0u8; 4];
    stream.read_exact(&mut length)?;
    let length = u32::from_ne_bytes(length);
    if length > MAX_MESSAGE {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "oversized greetd message"));
    }
    let mut body = vec![0u8; length as usize];
    stream.read_exact(&mut body)?;
    serde_json::from_slice(&body).map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
}

/// Runs one login over an open connection. On success greetd starts
/// `command` as the user once the greeter exits.
pub fn login_on(
    stream: &mut (impl Read + Write),
    user: &str,
    password: Zeroizing<String>,
    command: &[String],
) -> Result<(), String> {
    let io = |e: std::io::Error| format!("The login service did not answer ({e})");
    send(stream, &json!({ "type": "create_session", "username": user })).map_err(io)?;
    let mut answered_secret = false;
    for _ in 0..MAX_ROUNDS {
        let reply = receive(stream).map_err(io)?;
        match reply.get("type").and_then(Value::as_str) {
            Some("success") => {
                send(stream, &json!({ "type": "start_session", "cmd": command, "env": [] })).map_err(io)?;
                let started = receive(stream).map_err(io)?;
                return match started.get("type").and_then(Value::as_str) {
                    Some("success") => Ok(()),
                    _ => Err("The session could not be started".into()),
                };
            }
            Some("auth_message") => {
                let kind = reply.get("auth_message_type").and_then(Value::as_str).unwrap_or("");
                let response = match kind {
                    // The password is offered once; a second secret prompt
                    // means it was not accepted.
                    "secret" if !answered_secret => {
                        answered_secret = true;
                        json!({ "type": "post_auth_message_response", "response": password.as_str() })
                    }
                    "secret" | "visible" => {
                        let _ = send(stream, &json!({ "type": "cancel_session" }));
                        return Err("Incorrect user name or password".into());
                    }
                    _ => json!({ "type": "post_auth_message_response" }),
                };
                send(stream, &response).map_err(io)?;
            }
            _ => {
                let _ = send(stream, &json!({ "type": "cancel_session" }));
                let _ = receive(stream);
                return Err("Incorrect user name or password".into());
            }
        }
    }
    let _ = send(stream, &json!({ "type": "cancel_session" }));
    Err("Sign-in needs a step this screen does not support".into())
}

/// Logs in through the greetd socket at `socket`.
pub fn login(socket: &Path, user: &str, password: Zeroizing<String>, command: &[String]) -> Result<(), String> {
    let mut stream = UnixStream::connect(socket).map_err(|e| format!("The login service is not available ({e})"))?;
    // PAM delays failed attempts; allow for that, but never hang forever.
    let _ = stream.set_read_timeout(Some(Duration::from_secs(60)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(10)));
    login_on(&mut stream, user, password, command)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    /// A scripted peer: yields the queued replies, records what was sent.
    struct Script {
        replies: Cursor<Vec<u8>>,
        sent: Vec<u8>,
    }

    impl Script {
        fn new(replies: &[Value]) -> Self {
            let mut bytes = Vec::new();
            for reply in replies {
                send(&mut bytes, reply).unwrap();
            }
            Self {
                replies: Cursor::new(bytes),
                sent: Vec::new(),
            }
        }

        fn requests(&self) -> Vec<Value> {
            let mut cursor = Cursor::new(self.sent.clone());
            let mut out = Vec::new();
            while let Ok(message) = receive(&mut cursor) {
                out.push(message);
            }
            out
        }
    }

    impl Read for Script {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            self.replies.read(buf)
        }
    }

    impl Write for Script {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            self.sent.extend_from_slice(buf);
            Ok(buf.len())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    fn command() -> Vec<String> {
        vec!["/usr/bin/boswas-session".into()]
    }

    #[test]
    fn a_correct_password_starts_the_session() {
        let mut peer = Script::new(&[
            json!({"type": "auth_message", "auth_message_type": "secret", "auth_message": "Password: "}),
            json!({"type": "success"}),
            json!({"type": "success"}),
        ]);
        assert_eq!(login_on(&mut peer, "asha", Zeroizing::new("pw".into()), &command()), Ok(()));
        let requests = peer.requests();
        assert_eq!(requests[0], json!({"type": "create_session", "username": "asha"}));
        assert_eq!(requests[1], json!({"type": "post_auth_message_response", "response": "pw"}));
        assert_eq!(requests[2], json!({"type": "start_session", "cmd": ["/usr/bin/boswas-session"], "env": []}));
    }

    #[test]
    fn a_wrong_password_cancels_the_session() {
        let mut peer = Script::new(&[
            json!({"type": "auth_message", "auth_message_type": "secret", "auth_message": "Password: "}),
            json!({"type": "error", "error_type": "auth_error", "description": "pam_authenticate: AUTH_ERR"}),
            json!({"type": "success"}),
        ]);
        let result = login_on(&mut peer, "asha", Zeroizing::new("nope".into()), &command());
        assert_eq!(result, Err("Incorrect user name or password".into()));
        assert_eq!(peer.requests().last(), Some(&json!({"type": "cancel_session"})));
        assert!(!peer.requests().iter().any(|r| r["type"] == "start_session"));
    }

    #[test]
    fn the_password_is_never_sent_twice_or_to_a_visible_prompt() {
        let mut peer = Script::new(&[
            json!({"type": "auth_message", "auth_message_type": "info", "auth_message": "Welcome"}),
            json!({"type": "auth_message", "auth_message_type": "secret", "auth_message": "Password: "}),
            json!({"type": "auth_message", "auth_message_type": "secret", "auth_message": "Password: "}),
        ]);
        assert!(login_on(&mut peer, "asha", Zeroizing::new("pw".into()), &command()).is_err());
        let with_password = peer.requests().iter().filter(|r| r["response"] == "pw").count();
        assert_eq!(with_password, 1);

        let mut peer = Script::new(&[json!({"type": "auth_message", "auth_message_type": "visible", "auth_message": "OTP: "})]);
        assert!(login_on(&mut peer, "asha", Zeroizing::new("pw".into()), &command()).is_err());
        assert!(!peer.requests().iter().any(|r| r["response"] == "pw"));
    }

    #[test]
    fn broken_peers_are_errors_not_panics() {
        let mut closed = Script::new(&[]);
        assert!(login_on(&mut closed, "asha", Zeroizing::new("pw".into()), &command()).is_err());
        let mut huge = Script {
            replies: Cursor::new(u32::MAX.to_ne_bytes().to_vec()),
            sent: Vec::new(),
        };
        assert!(login_on(&mut huge, "asha", Zeroizing::new("pw".into()), &command()).is_err());
        let mut garbage = Script {
            replies: Cursor::new([5u32.to_ne_bytes().as_slice(), b"notjs"].concat()),
            sent: Vec::new(),
        };
        assert!(login_on(&mut garbage, "asha", Zeroizing::new("pw".into()), &command()).is_err());
        let mut endless = Script::new(&vec![json!({"type": "auth_message", "auth_message_type": "info", "auth_message": "…"}); 20]);
        assert!(login_on(&mut endless, "asha", Zeroizing::new("pw".into()), &command()).is_err());
    }
}
