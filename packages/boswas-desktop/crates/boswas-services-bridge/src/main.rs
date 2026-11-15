//! boswas-services-bridge: the shell's service backends for the Settings
//! application.
//!
//! Settings is written in Python; the code that talks to NetworkManager,
//! BlueZ, the sound server, UPower and the backlight is the Rust crate the
//! shell uses. This program runs those backends and speaks JSON lines on
//! its standard input and output (see [`protocol`]), so that both show the
//! same state and behave the same. It runs as the user who started it, with
//! that user's rights, and ends when its standard input closes.

use std::{
    io::{BufRead, Read, Write},
    process::ExitCode,
    sync::{Arc, Mutex},
};

use boswas_services::{Config, Services};
use zeroize::Zeroize;

mod protocol;

const USAGE: &str = "\
Usage: boswas-services-bridge [--help | --version]

Network, Bluetooth, sound, power and brightness for the Settings
application. Reads one JSON command per line on standard input and writes
one JSON event per line on standard output; ends when the input closes.
";

fn write_line(out: &Mutex<std::io::Stdout>, value: &serde_json::Value) -> bool {
    let Ok(mut out) = out.lock() else {
        return false;
    };
    writeln!(out, "{value}").and_then(|()| out.flush()).is_ok()
}

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        None => {}
        Some("-h" | "--help") => {
            print!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        Some("-V" | "--version") => {
            println!("boswas-services-bridge {}", env!("CARGO_PKG_VERSION"));
            return ExitCode::SUCCESS;
        }
        Some(other) => {
            eprintln!("boswas-services-bridge: unknown option {other}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    }
    let filter = tracing_subscriber::EnvFilter::try_from_env("BOSWAS_LOG").unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn"));
    tracing_subscriber::fmt().with_env_filter(filter).with_writer(std::io::stderr).init();

    let out = Arc::new(Mutex::new(std::io::stdout()));
    if !write_line(&out, &protocol::ready()) {
        return ExitCode::FAILURE;
    }
    let sink = Arc::clone(&out);
    // Status services with every status command: not the lock screen's
    // restricted set. `session` only listens to logind; its events are not
    // forwarded.
    let config = Config {
        session: true,
        ..Config::status_only()
    };
    let services = Services::start(config, move |event| {
        if let Some(line) = protocol::event(&event) {
            write_line(&sink, &line);
        }
    });

    let stdin = std::io::stdin();
    let mut input = stdin.lock();
    let mut line = String::new();
    loop {
        line.zeroize();
        // One byte more than allowed, so that an over-long line is noticed
        // without reading it whole into memory.
        let mut limited = Read::take(&mut input, protocol::MAX_LINE as u64 + 1);
        match limited.read_line(&mut line) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
        if !line.ends_with('\n') && line.len() > protocol::MAX_LINE {
            // Drop the rest of the over-long line.
            let mut rest = Vec::new();
            let _ = input.read_until(b'\n', &mut rest);
            rest.zeroize();
            write_line(&out, &protocol::error("the line is too long"));
            continue;
        }
        let text = line.trim();
        if text.is_empty() {
            continue;
        }
        match protocol::command(text) {
            Ok(command) => services.send(command),
            Err(message) => {
                if !write_line(&out, &protocol::error(&message)) {
                    break;
                }
            }
        }
    }
    line.zeroize();
    drop(services);
    ExitCode::SUCCESS
}
