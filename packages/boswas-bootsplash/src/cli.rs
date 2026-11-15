//! The client program: parse the command line, talk to the daemon, and turn
//! the outcome into an exit status (0 done, 1 no boot screen or refused,
//! 2 usage error).

use std::io::Write;
use std::path::{Path, PathBuf};

use crate::agent;
use crate::client::{self, DEFAULT_SOCKET};
use crate::compat::{self, Command, Invocation};
use crate::proto::Response;

/// Run the client and return its exit status.
pub fn run(argv0: &str, args: &[String]) -> i32 {
    let plymouth = compat::is_plymouth(argv0);
    let Invocation { socket, commands } = match compat::parse(argv0, args) {
        Ok(invocation) => invocation,
        Err(reason) => {
            if plymouth {
                eprintln!("plymouth (boswas-bootsplash): {reason}");
                return 1;
            }
            eprintln!("boswas-bootsplash: {reason}\n{}", compat::USAGE);
            return 2;
        }
    };
    let socket = socket.unwrap_or_else(|| default_socket(argv0));
    for command in commands {
        let status = execute(&socket, command);
        if status != 0 {
            return status;
        }
    }
    0
}

/// The control socket to use when none was given: the system one, unless a
/// `boswas-bootsplash.socket` entry sits next to the program as it was
/// invoked. That is how the test suite points a client that must be called
/// `plymouth` (which has no socket option) at a test daemon; the system
/// program directories never contain such an entry.
pub fn default_socket(argv0: &str) -> PathBuf {
    if let Some(dir) = Path::new(argv0).parent().filter(|d| !d.as_os_str().is_empty()) {
        let adjacent = dir.join("boswas-bootsplash.socket");
        if adjacent.exists() {
            return adjacent;
        }
    }
    PathBuf::from(DEFAULT_SOCKET)
}

fn execute(socket: &Path, command: Command) -> i32 {
    match command {
        Command::Help => {
            print!("{}", compat::USAGE);
            0
        }
        Command::Version => {
            println!("boswas-bootsplash {}", crate::VERSION);
            0
        }
        Command::Accepted => i32::from(!client::ping(socket)),
        Command::Agent => agent::run(socket, Path::new(agent::ASK_DIR)),
        Command::Send(request) => match client::request(socket, &request) {
            Ok(Response::Ok) => 0,
            Ok(Response::Error(reason)) => {
                eprintln!("boswas-bootsplash: {reason}");
                1
            }
            Ok(Response::Password(_)) | Err(_) => 1,
        },
        Command::AskPassword { prompt } => match client::ask_password(socket, &prompt, || true) {
            Ok(Some(passphrase)) => {
                // Exactly the passphrase, no newline: the caller reads to end of file.
                let mut out = std::io::stdout();
                if out.write_all(&passphrase).and_then(|()| out.flush()).is_ok() {
                    0
                } else {
                    1
                }
            }
            Ok(None) | Err(_) => 1,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_system_socket_is_the_default() {
        assert_eq!(default_socket("/bin/plymouth"), PathBuf::from(DEFAULT_SOCKET));
        assert_eq!(default_socket("plymouth"), PathBuf::from(DEFAULT_SOCKET));
        assert_eq!(default_socket("/usr/bin/boswas-bootsplash"), PathBuf::from(DEFAULT_SOCKET));
        assert_eq!(default_socket(""), PathBuf::from(DEFAULT_SOCKET));
    }

    #[test]
    fn an_adjacent_socket_entry_is_used() {
        let dir = std::env::temp_dir().join(format!("boswas-bootsplash-cli-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("boswas-bootsplash.socket"), b"").unwrap();
        let argv0 = dir.join("plymouth");
        assert_eq!(default_socket(argv0.to_str().unwrap()), dir.join("boswas-bootsplash.socket"));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn usage_errors_and_missing_daemon() {
        let args = |list: &[&str]| list.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(run("boswas-bootsplash", &args(&["frobnicate"])), 2);
        assert_eq!(run("boswas-bootsplash", &args(&[])), 2);
        assert_eq!(run("plymouth", &args(&["frobnicate"])), 1);
        // No daemon behind this socket: every command fails with status 1.
        let nowhere = "--socket=/nonexistent/boswas-bootsplash/socket";
        assert_eq!(run("boswas-bootsplash", &args(&[nowhere, "ping"])), 1);
        assert_eq!(run("boswas-bootsplash", &args(&[nowhere, "message", "x"])), 1);
        assert_eq!(run("boswas-bootsplash", &args(&[nowhere, "ask-password"])), 1);
        assert_eq!(run("boswas-bootsplash", &args(&[nowhere, "quit"])), 1);
    }
}
