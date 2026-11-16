//! Prints every event of the services, for trying them on a real machine.
//!
//!     cargo run -p boswas-services --example monitor [-- --status | --settings]
//!
//! Without an argument everything runs, as in the desktop shell: this
//! program then is the session's notification server, tray and polkit
//! agent, so do not run it next to a shell that is. `--status` runs what
//! the lock screen runs (it shows the state and changes nothing but the
//! volume and the brightness), `--settings` the status services with
//! every command obeyed.
//!
//! A few commands can be typed, one per line:
//!
//!     refresh            re-read every status service
//!     scan               scan for Wi-Fi networks
//!     volume 0.4         set the output volume (0.0 to 1.5)
//!     mute on|off
//!     brightness 0.7     set the backlight (0.0 to 1.0)
//!     dnd on|off         do not disturb
//!     quit

use std::io::BufRead;

use boswas_services::{Command, Config, Event, Service, Services};

fn main() {
    let config = match std::env::args().nth(1).as_deref() {
        None => Config::desktop(),
        Some("--status") => Config::status_only(),
        Some("--settings") => Config::settings(),
        Some(_) => {
            eprintln!("usage: monitor [--status | --settings]");
            std::process::exit(2);
        }
    };
    let services = Services::start(config, |event| println!("{}", describe(&event)));

    for line in std::io::stdin().lock().lines().map_while(Result::ok) {
        let mut words = line.split_whitespace();
        let on = |word: Option<&str>| word == Some("on");
        match (words.next(), words.next()) {
            (Some("quit"), _) => break,
            (Some("refresh"), _) => {
                for service in [Service::Network, Service::Bluetooth, Service::Audio, Service::Power, Service::Brightness, Service::Tray, Service::Media] {
                    services.send(Command::Refresh(service));
                }
            }
            (Some("scan"), _) => services.send(Command::WifiScan),
            (Some("volume"), Some(level)) => match level.parse() {
                Ok(level) => services.send(Command::SetVolume(level)),
                Err(_) => eprintln!("volume takes a number"),
            },
            (Some("mute"), word) => services.send(Command::SetMuted(on(word))),
            (Some("brightness"), Some(level)) => match level.parse() {
                Ok(level) => services.send(Command::SetBrightness(level)),
                Err(_) => eprintln!("brightness takes a number"),
            },
            (Some("dnd"), word) => services.send(Command::SetDoNotDisturb(on(word))),
            (None, _) => {}
            _ => eprintln!("commands: refresh, scan, volume N, mute on|off, brightness N, dnd on|off, quit"),
        }
    }
    // Dropping the services stops their threads and child processes.
    drop(services);
}

/// An event as text. Pixmaps are named by their size instead of being
/// printed byte by byte.
fn describe(event: &Event) -> String {
    match event {
        Event::Tray(items) => {
            let items: Vec<String> = items
                .iter()
                .map(|item| {
                    let pixmap = item.icon_pixmap.as_ref().map(|pixmap| format!("{}x{} pixmap", pixmap.width, pixmap.height));
                    format!(
                        "{{ key: {:?}, id: {:?}, title: {:?}, icon: {:?}, status: {:?}, menu: {}, tooltip: {:?} }}",
                        item.key,
                        item.id,
                        item.title,
                        pixmap.unwrap_or_else(|| item.icon_name.clone()),
                        item.status,
                        item.has_menu,
                        item.tooltip
                    )
                })
                .collect();
            format!("Tray([{}])", items.join(", "))
        }
        other => format!("{other:?}"),
    }
}
