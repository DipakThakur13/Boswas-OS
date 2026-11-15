//! Command line of the client, in two dialects.
//!
//! `boswas-bootsplash` has its own small set of subcommands. Inside the
//! initramfs the same binary is also installed as `/bin/plymouth`, because
//! that is the name Debian's disk-unlock helper (`/lib/cryptsetup/askpass`)
//! and the initramfs scripts call:
//!
//! ```text
//! plymouth --ping
//! plymouth ask-for-password --prompt "Please unlock disk sda3_crypt"
//! plymouth message --text="cryptsetup: sda3_crypt: set up successfully"
//! plymouth display-message --text=...      plymouth hide-message --text=...
//! plymouth hide-splash    plymouth show-splash    plymouth quit
//! plymouth update-root-fs --new-root-dir=/root
//! ```
//!
//! Only this subset is implemented. Other Plymouth commands that scripts may
//! issue are accepted and do nothing; anything unknown is an error, as it is
//! with Plymouth itself.

use std::path::PathBuf;

use crate::proto::Request;
use crate::scene::Mode;

/// One thing the client was asked to do.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Command {
    /// Send a request; succeed when the daemon answers OK.
    Send(Request),
    /// Ask for a passphrase and write it to standard output.
    AskPassword {
        prompt: String,
    },
    /// A Plymouth command with no meaning here: succeeds if the daemon runs.
    Accepted,
    /// Answer systemd's password requests (`boswas-bootsplash agent`).
    Agent,
    Help,
    Version,
}

/// A parsed command line.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Invocation {
    /// Control socket to use instead of the system one.
    pub socket: Option<PathBuf>,
    pub commands: Vec<Command>,
}

/// Default time the last frame stays up after `quit --retain`, in seconds.
pub const DEFAULT_RETAIN_SECS: u16 = 6;

pub const USAGE: &str = "\
Usage: boswas-bootsplash [--socket PATH] COMMAND

Commands:
  ping                       succeed if the boot screen is running
  ask-password [--prompt T]  show a passphrase prompt; print the passphrase
  message [TEXT]             show a message line (no text clears it)
  progress PERCENT           report boot progress (0 to 100)
  mode boot|shutdown|reboot  switch the screen
  hide                       give the display back to the text console
  show                       take the display again
  quit [--retain[=SECONDS]]  leave; --retain keeps the last frame until the
                             next program takes the display
  new-root DIR               follow the switch to the real root file system
  agent                      answer systemd password requests on the boot screen
  --help, --version
";

/// True when the program was started under the name `plymouth`.
pub fn is_plymouth(argv0: &str) -> bool {
    argv0.rsplit('/').next() == Some("plymouth")
}

/// Parse the arguments (without the program name) in the dialect that
/// `argv0` selects.
pub fn parse(argv0: &str, args: &[String]) -> Result<Invocation, String> {
    if is_plymouth(argv0) {
        parse_plymouth(args)
    } else {
        parse_native(args)
    }
}

/// Value of `--name=value` or `--name value` at position `i`.
fn option_value(args: &[String], i: &mut usize, name: &str) -> Option<String> {
    let arg = &args[*i];
    if let Some(value) = arg.strip_prefix(name).and_then(|rest| rest.strip_prefix('=')) {
        return Some(value.to_owned());
    }
    if arg == name {
        if let Some(value) = args.get(*i + 1) {
            *i += 1;
            return Some(value.clone());
        }
        return Some(String::new());
    }
    None
}

fn parse_native(args: &[String]) -> Result<Invocation, String> {
    let mut socket = None;
    let mut i = 0;
    while i < args.len() {
        if let Some(path) = option_value(args, &mut i, "--socket") {
            if path.is_empty() {
                return Err("--socket needs a path".into());
            }
            socket = Some(PathBuf::from(path));
            i += 1;
        } else {
            break;
        }
    }
    let Some(name) = args.get(i) else {
        return Err("missing command".into());
    };
    let rest = &args[i + 1..];
    let no_args =
        |command: Command| if rest.is_empty() { Ok(command) } else { Err(format!("{name} takes no arguments")) };
    let command = match name.as_str() {
        "-h" | "--help" | "help" => Command::Help,
        "--version" | "version" => Command::Version,
        "ping" => no_args(Command::Send(Request::Ping))?,
        "hide" => no_args(Command::Send(Request::Hide))?,
        "show" => no_args(Command::Send(Request::Show))?,
        "agent" => no_args(Command::Agent)?,
        "ask-password" => {
            let mut prompt = String::from("Passphrase");
            let mut j = 0;
            while j < rest.len() {
                match option_value(rest, &mut j, "--prompt") {
                    Some(text) => prompt = text,
                    None => return Err(format!("ask-password: unknown option {}", rest[j])),
                }
                j += 1;
            }
            Command::AskPassword { prompt }
        }
        "message" => Command::Send(Request::Message { text: rest.join(" ") }),
        "progress" => match rest {
            [value] => match value.parse::<u8>() {
                Ok(p) if p <= 100 => Command::Send(Request::Progress(p)),
                _ => return Err("progress is a number from 0 to 100".into()),
            },
            _ => return Err("progress takes one number".into()),
        },
        "mode" => match rest {
            [value] => Command::Send(Request::Mode(
                Mode::parse(value).ok_or_else(|| "mode is boot, shutdown or reboot".to_string())?,
            )),
            _ => return Err("mode takes one name".into()),
        },
        "quit" => match rest {
            [] => Command::Send(Request::Quit { retain: None }),
            [flag] if flag == "--retain" => Command::Send(Request::Quit { retain: Some(DEFAULT_RETAIN_SECS) }),
            [flag] if flag.starts_with("--retain=") => match flag["--retain=".len()..].parse::<u16>() {
                Ok(secs) if secs <= crate::proto::MAX_RETAIN_SECS => {
                    Command::Send(Request::Quit { retain: Some(secs) })
                }
                _ => return Err("--retain takes a number of seconds up to 60".into()),
            },
            _ => return Err("quit takes only --retain[=SECONDS]".into()),
        },
        "new-root" => match rest {
            [dir] if dir.starts_with('/') => Command::Send(Request::NewRoot { path: dir.clone() }),
            _ => return Err("new-root takes one absolute directory".into()),
        },
        "snapshot" => match rest {
            [name] => Command::Send(Request::Snapshot { name: name.clone() }),
            _ => return Err("snapshot takes one file name".into()),
        },
        other => return Err(format!("unknown command {other}")),
    };
    Ok(Invocation { socket, commands: vec![command] })
}

fn parse_plymouth(args: &[String]) -> Result<Invocation, String> {
    let mut commands = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let arg = args[i].as_str();
        // Options of a subcommand: everything up to the next non-option.
        let options = |i: &mut usize| -> Vec<(String, Option<String>)> {
            let mut found = Vec::new();
            while *i + 1 < args.len() && args[*i + 1].starts_with("--") {
                *i += 1;
                let (name, value) = match args[*i].split_once('=') {
                    Some((name, value)) => (name.to_owned(), Some(value.to_owned())),
                    None => (args[*i].clone(), None),
                };
                // `--name value`: the value is the next word, unless that is another option.
                let value = match value {
                    Some(v) => Some(v),
                    None if takes_value(&name) && *i + 1 < args.len() => {
                        *i += 1;
                        Some(args[*i].clone())
                    }
                    None => None,
                };
                found.push((name, value));
            }
            found
        };
        let get = |opts: &[(String, Option<String>)], name: &str| {
            opts.iter().find(|(n, _)| n == name).map(|(_, v)| v.clone().unwrap_or_default())
        };
        match arg {
            "--ping" => commands.push(Command::Send(Request::Ping)),
            "--quit" => commands.push(Command::Send(Request::Quit { retain: None })),
            "--show-splash" | "show-splash" => commands.push(Command::Send(Request::Show)),
            "--hide-splash" | "hide-splash" => commands.push(Command::Send(Request::Hide)),
            "--help" | "-h" => commands.push(Command::Help),
            "--version" => commands.push(Command::Version),
            "--ask-for-password" => commands.push(Command::AskPassword { prompt: "Password".into() }),
            "--sysinit" | "--wait" | "--has-active-vt" | "--debug" | "pause-progress" | "unpause-progress"
            | "deactivate" | "reactivate" | "report-error" | "reload" => commands.push(Command::Accepted),
            "quit" => {
                let opts = options(&mut i);
                let retain = get(&opts, "--retain-splash").map(|_| DEFAULT_RETAIN_SECS);
                commands.push(Command::Send(Request::Quit { retain }));
            }
            "ask-for-password" => {
                let opts = options(&mut i);
                if get(&opts, "--command").is_some() {
                    return Err("ask-for-password --command is not supported".into());
                }
                let prompt = get(&opts, "--prompt").unwrap_or_else(|| "Password".into());
                commands.push(Command::AskPassword { prompt });
            }
            "display-message" | "message" => {
                let opts = options(&mut i);
                commands.push(Command::Send(Request::Message { text: get(&opts, "--text").unwrap_or_default() }));
            }
            "hide-message" => {
                options(&mut i);
                commands.push(Command::Send(Request::Message { text: String::new() }));
            }
            "update" | "watch-keystroke" | "ignore-keystroke" => {
                options(&mut i);
                commands.push(Command::Accepted);
            }
            "system-update" => {
                let opts = options(&mut i);
                match get(&opts, "--progress").and_then(|p| p.parse::<u8>().ok()) {
                    Some(p) => commands.push(Command::Send(Request::Progress(p.min(100)))),
                    None => commands.push(Command::Accepted),
                }
            }
            "change-mode" => {
                let opts = options(&mut i);
                let mode = if get(&opts, "--shutdown").is_some() {
                    Mode::Shutdown
                } else if get(&opts, "--reboot").is_some() {
                    Mode::Reboot
                } else {
                    Mode::Boot
                };
                commands.push(Command::Send(Request::Mode(mode)));
            }
            "update-root-fs" => {
                let opts = options(&mut i);
                match get(&opts, "--new-root-dir") {
                    Some(dir) if dir.starts_with('/') => commands.push(Command::Send(Request::NewRoot { path: dir })),
                    Some(_) => return Err("update-root-fs: --new-root-dir must be absolute".into()),
                    None => commands.push(Command::Accepted),
                }
            }
            "ask-question" => return Err("ask-question is not supported".into()),
            _ => {
                if let Some(dir) = arg.strip_prefix("--newroot=") {
                    if !dir.starts_with('/') {
                        return Err("--newroot must be absolute".into());
                    }
                    commands.push(Command::Send(Request::NewRoot { path: dir.to_owned() }));
                } else if arg.starts_with("--update=")
                    || arg.starts_with("--debug-file=")
                    || arg.starts_with("--ignore-keystroke=")
                    || arg.starts_with("--watch-keystroke=")
                {
                    commands.push(Command::Accepted);
                } else {
                    return Err(format!("unknown command {arg}"));
                }
            }
        }
        i += 1;
    }
    if commands.is_empty() {
        return Err("missing command".into());
    }
    Ok(Invocation { socket: None, commands })
}

/// Plymouth subcommand options that are followed by a value.
fn takes_value(name: &str) -> bool {
    matches!(
        name,
        "--prompt"
            | "--command"
            | "--number-of-tries"
            | "--text"
            | "--status"
            | "--progress"
            | "--new-root-dir"
            | "--keys"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    fn plymouth(list: &[&str]) -> Result<Vec<Command>, String> {
        parse("/bin/plymouth", &args(list)).map(|inv| inv.commands)
    }

    fn native(list: &[&str]) -> Result<Vec<Command>, String> {
        parse("/usr/bin/boswas-bootsplash", &args(list)).map(|inv| inv.commands)
    }

    #[test]
    fn dialect_follows_the_program_name() {
        assert!(is_plymouth("/bin/plymouth"));
        assert!(is_plymouth("plymouth"));
        assert!(!is_plymouth("/usr/bin/boswas-bootsplash"));
        assert!(!is_plymouth("/bin/plymouthd"));
        assert!(!is_plymouth("not-plymouth"));
    }

    #[test]
    fn what_askpass_runs() {
        // debian/askpass.c: system("/bin/plymouth --ping") and
        // execl(..., "ask-for-password", "--prompt", prompt, NULL).
        assert_eq!(plymouth(&["--ping"]), Ok(vec![Command::Send(Request::Ping)]));
        assert_eq!(
            plymouth(&["ask-for-password", "--prompt", "Please unlock disk sda3_crypt"]),
            Ok(vec![Command::AskPassword { prompt: "Please unlock disk sda3_crypt".into() }])
        );
        assert_eq!(
            plymouth(&["ask-for-password", "--prompt=Enter pin for card: "]),
            Ok(vec![Command::AskPassword { prompt: "Enter pin for card: ".into() }])
        );
        // A prompt that looks like an option is still the prompt.
        assert_eq!(
            plymouth(&["ask-for-password", "--prompt", "--weird"]),
            Ok(vec![Command::AskPassword { prompt: "--weird".into() }])
        );
        assert_eq!(plymouth(&["ask-for-password"]), Ok(vec![Command::AskPassword { prompt: "Password".into() }]));
        assert!(plymouth(&["ask-for-password", "--command=/bin/true"]).is_err());
    }

    #[test]
    fn what_the_initramfs_scripts_run() {
        assert_eq!(
            plymouth(&["message", "--text=cryptsetup: sda3_crypt: set up successfully"]),
            Ok(vec![Command::Send(Request::Message { text: "cryptsetup: sda3_crypt: set up successfully".into() })])
        );
        assert_eq!(
            plymouth(&["display-message", "--text", "Resuming from /dev/dm-1"]),
            Ok(vec![Command::Send(Request::Message { text: "Resuming from /dev/dm-1".into() })])
        );
        assert_eq!(
            plymouth(&["display-message", "--text="]),
            Ok(vec![Command::Send(Request::Message { text: String::new() })])
        );
        assert_eq!(
            plymouth(&["hide-message", "--text=x"]),
            Ok(vec![Command::Send(Request::Message { text: String::new() })])
        );
        assert_eq!(plymouth(&["quit"]), Ok(vec![Command::Send(Request::Quit { retain: None })]));
        assert_eq!(
            plymouth(&["quit", "--retain-splash"]),
            Ok(vec![Command::Send(Request::Quit { retain: Some(DEFAULT_RETAIN_SECS) })])
        );
        assert_eq!(plymouth(&["hide-splash"]), Ok(vec![Command::Send(Request::Hide)]));
        assert_eq!(plymouth(&["--show-splash"]), Ok(vec![Command::Send(Request::Show)]));
        assert_eq!(
            plymouth(&["update-root-fs", "--new-root-dir=/root"]),
            Ok(vec![Command::Send(Request::NewRoot { path: "/root".into() })])
        );
        assert_eq!(plymouth(&["update-root-fs", "--read-write"]), Ok(vec![Command::Accepted]));
        assert_eq!(plymouth(&["--newroot=/root"]), Ok(vec![Command::Send(Request::NewRoot { path: "/root".into() })]));
    }

    #[test]
    fn other_plymouth_commands() {
        for accepted in [
            &["--sysinit"][..],
            &["--wait"],
            &["update", "--status=networking"],
            &["--update=fsck"],
            &["pause-progress"],
            &["unpause-progress"],
            &["deactivate"],
            &["reactivate"],
            &["watch-keystroke", "--keys=cC", "--command=true"],
            &["system-update"],
        ] {
            assert_eq!(plymouth(accepted), Ok(vec![Command::Accepted]), "{accepted:?}");
        }
        assert_eq!(plymouth(&["system-update", "--progress=40"]), Ok(vec![Command::Send(Request::Progress(40))]));
        assert_eq!(plymouth(&["change-mode", "--shutdown"]), Ok(vec![Command::Send(Request::Mode(Mode::Shutdown))]));
        assert_eq!(plymouth(&["change-mode", "--reboot"]), Ok(vec![Command::Send(Request::Mode(Mode::Reboot))]));
        assert_eq!(plymouth(&["change-mode", "--updates"]), Ok(vec![Command::Send(Request::Mode(Mode::Boot))]));
        assert_eq!(
            plymouth(&["--ping", "--quit"]),
            Ok(vec![Command::Send(Request::Ping), Command::Send(Request::Quit { retain: None })])
        );
        assert!(plymouth(&[]).is_err());
        assert!(plymouth(&["ask-question", "--prompt=x"]).is_err());
        assert!(plymouth(&["--get-splash-plugin-path"]).is_err());
        assert!(plymouth(&["frobnicate"]).is_err());
        assert!(plymouth(&["update-root-fs", "--new-root-dir=relative"]).is_err());
    }

    #[test]
    fn native_commands() {
        assert_eq!(native(&["ping"]), Ok(vec![Command::Send(Request::Ping)]));
        assert_eq!(native(&["ask-password"]), Ok(vec![Command::AskPassword { prompt: "Passphrase".into() }]));
        assert_eq!(
            native(&["ask-password", "--prompt", "Unlock"]),
            Ok(vec![Command::AskPassword { prompt: "Unlock".into() }])
        );
        assert_eq!(
            native(&["message", "two", "words"]),
            Ok(vec![Command::Send(Request::Message { text: "two words".into() })])
        );
        assert_eq!(native(&["message"]), Ok(vec![Command::Send(Request::Message { text: String::new() })]));
        assert_eq!(native(&["progress", "40"]), Ok(vec![Command::Send(Request::Progress(40))]));
        assert_eq!(native(&["mode", "reboot"]), Ok(vec![Command::Send(Request::Mode(Mode::Reboot))]));
        assert_eq!(native(&["hide"]), Ok(vec![Command::Send(Request::Hide)]));
        assert_eq!(native(&["show"]), Ok(vec![Command::Send(Request::Show)]));
        assert_eq!(native(&["quit"]), Ok(vec![Command::Send(Request::Quit { retain: None })]));
        assert_eq!(native(&["quit", "--retain"]), Ok(vec![Command::Send(Request::Quit { retain: Some(6) })]));
        assert_eq!(native(&["quit", "--retain=2"]), Ok(vec![Command::Send(Request::Quit { retain: Some(2) })]));
        assert_eq!(native(&["new-root", "/root"]), Ok(vec![Command::Send(Request::NewRoot { path: "/root".into() })]));
        assert_eq!(native(&["agent"]), Ok(vec![Command::Agent]));
        assert_eq!(native(&["--help"]), Ok(vec![Command::Help]));
        assert_eq!(native(&["--version"]), Ok(vec![Command::Version]));
    }

    #[test]
    fn native_usage_errors() {
        for bad in [
            &[][..],
            &["ping", "extra"],
            &["progress"],
            &["progress", "101"],
            &["progress", "x"],
            &["mode"],
            &["mode", "updates"],
            &["quit", "--retain=61"],
            &["quit", "--now"],
            &["new-root", "relative"],
            &["ask-password", "--command", "x"],
            &["--socket"],
            &["plymouth"],
            &["--ping"],
        ] {
            assert!(native(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn socket_option() {
        let inv = parse("boswas-bootsplash", &args(&["--socket", "/tmp/s", "ping"])).unwrap();
        assert_eq!(inv.socket, Some(PathBuf::from("/tmp/s")));
        let inv = parse("boswas-bootsplash", &args(&["--socket=/tmp/t", "hide"])).unwrap();
        assert_eq!(inv.socket, Some(PathBuf::from("/tmp/t")));
        assert_eq!(inv.commands, vec![Command::Send(Request::Hide)]);
    }
}
