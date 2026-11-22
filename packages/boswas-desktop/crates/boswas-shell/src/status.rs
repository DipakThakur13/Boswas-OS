//! What the panel's status area shows: clock, battery and network. All of
//! it is read from the kernel's files; nothing here talks to the network.

use std::path::Path;

/// The battery, when the machine has one.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Battery {
    /// Charge in percent, 0 to 100.
    pub percent: u8,
    pub charging: bool,
}

impl Battery {
    /// The status icon name for this charge.
    pub fn icon(&self) -> &'static str {
        match (self.percent, self.charging) {
            (0..=5, false) => "battery-empty-symbolic",
            (6..=15, false) => "battery-caution-symbolic",
            (16..=40, false) => "battery-low-symbolic",
            (41..=80, false) => "battery-good-symbolic",
            (_, false) => "battery-full-symbolic",
            (0..=5, true) => "battery-empty-charging-symbolic",
            (6..=15, true) => "battery-caution-charging-symbolic",
            (16..=40, true) => "battery-low-charging-symbolic",
            (41..=80, true) => "battery-good-charging-symbolic",
            (_, true) => "battery-full-charging-symbolic",
        }
    }
}

/// The network link the panel shows.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Network {
    Offline,
    Wired,
    /// Wireless with link quality in percent.
    Wireless(u8),
}

impl Network {
    pub fn icon(&self) -> &'static str {
        match self {
            Network::Offline => "network-offline-symbolic",
            Network::Wired => "network-wired-symbolic",
            Network::Wireless(0..=19) => "network-wireless-signal-none-symbolic",
            Network::Wireless(20..=39) => "network-wireless-signal-weak-symbolic",
            Network::Wireless(40..=59) => "network-wireless-signal-ok-symbolic",
            Network::Wireless(60..=79) => "network-wireless-signal-good-symbolic",
            Network::Wireless(_) => "network-wireless-signal-excellent-symbolic",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Status {
    /// `14:05`
    pub time: String,
    /// `Sun 15 Nov`
    pub date: String,
    pub battery: Option<Battery>,
    pub network: Network,
}

impl Status {
    /// The status now; `seconds`: the time with seconds (the taskbar's
    /// clock can show them).
    pub fn read(seconds: bool) -> Self {
        let now = chrono::Local::now();
        Self {
            time: now.format(if seconds { "%H:%M:%S" } else { "%H:%M" }).to_string(),
            date: now.format("%a %-d %b").to_string(),
            battery: read_battery(Path::new("/sys/class/power_supply")),
            network: read_network(Path::new("/sys/class/net"), Path::new("/proc/net/wireless")),
        }
    }
}

fn read_trimmed(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    Some(text.trim().to_owned())
}

/// The first battery under a `power_supply` directory.
pub fn read_battery(dir: &Path) -> Option<Battery> {
    let mut names: Vec<_> = std::fs::read_dir(dir).ok()?.flatten().map(|e| e.path()).collect();
    names.sort();
    names.into_iter().find_map(|supply| {
        if read_trimmed(&supply.join("type"))? != "Battery" {
            return None;
        }
        // Peripheral batteries (mice, headsets) report a "Device" scope.
        if read_trimmed(&supply.join("scope")).is_some_and(|scope| scope == "Device") {
            return None;
        }
        let percent = read_trimmed(&supply.join("capacity"))?.parse::<u8>().ok()?.min(100);
        let status = read_trimmed(&supply.join("status")).unwrap_or_default();
        Some(Battery {
            percent,
            charging: status == "Charging" || status == "Full",
        })
    })
}

/// The best link that is up: wired before wireless.
pub fn read_network(net: &Path, wireless: &Path) -> Network {
    let Ok(entries) = std::fs::read_dir(net) else {
        return Network::Offline;
    };
    let mut best = Network::Offline;
    for interface in entries.flatten().map(|e| e.path()) {
        let name = interface.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        if name == "lo" || read_trimmed(&interface.join("operstate")).as_deref() != Some("up") {
            continue;
        }
        if interface.join("wireless").exists() || interface.join("phy80211").exists() {
            if best == Network::Offline {
                best = Network::Wireless(wireless_quality(wireless, &name).unwrap_or(60));
            }
        } else if interface.join("device").exists() {
            // A physical wired interface (bridges and tunnels have no device).
            return Network::Wired;
        }
    }
    best
}

/// Link quality of an interface from `/proc/net/wireless`, in percent
/// (the kernel reports it out of 70).
fn wireless_quality(path: &Path, interface: &str) -> Option<u8> {
    let text = std::fs::read_to_string(path).ok()?;
    let line = text.lines().find(|l| l.trim_start().starts_with(&format!("{interface}:")))?;
    let link: f32 = line.split_whitespace().nth(2)?.trim_end_matches('.').parse().ok()?;
    Some(((link / 70.0) * 100.0).clamp(0.0, 100.0) as u8)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("boswas-shell-status-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn battery_icons_cover_every_charge() {
        for percent in 0..=100u8 {
            for charging in [false, true] {
                let icon = Battery { percent, charging }.icon();
                assert!(icon.starts_with("battery-") && icon.ends_with("-symbolic"));
                assert_eq!(icon.contains("charging"), charging);
            }
        }
        assert_eq!(Battery { percent: 3, charging: false }.icon(), "battery-empty-symbolic");
        assert_eq!(Battery { percent: 100, charging: true }.icon(), "battery-full-charging-symbolic");
    }

    #[test]
    fn the_system_battery_is_found_and_peripherals_are_not() {
        let dir = scratch("battery");
        for (name, kind, scope, capacity, status) in [
            ("AC", "Mains", None, None, None),
            ("hid-mouse", "Battery", Some("Device"), Some("12"), Some("Discharging")),
            ("BAT0", "Battery", None, Some("87"), Some("Charging")),
        ] {
            let supply = dir.join(name);
            fs::create_dir_all(&supply).unwrap();
            fs::write(supply.join("type"), format!("{kind}\n")).unwrap();
            for (file, value) in [("scope", scope), ("capacity", capacity), ("status", status)] {
                if let Some(value) = value {
                    fs::write(supply.join(file), format!("{value}\n")).unwrap();
                }
            }
        }
        assert_eq!(read_battery(&dir), Some(Battery { percent: 87, charging: true }));
        fs::write(dir.join("BAT0/capacity"), "250\n").unwrap();
        assert_eq!(read_battery(&dir).unwrap().percent, 100, "nonsense values are clamped");
        fs::write(dir.join("BAT0/capacity"), "many\n").unwrap();
        assert_eq!(read_battery(&dir), None);
        assert_eq!(read_battery(&dir.join("missing")), None);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn network_prefers_wired_and_reads_wireless_quality() {
        let dir = scratch("net");
        let iface = |name: &str, state: &str, wireless: bool, physical: bool| {
            let path = dir.join("net").join(name);
            fs::create_dir_all(&path).unwrap();
            fs::write(path.join("operstate"), format!("{state}\n")).unwrap();
            if wireless {
                fs::create_dir_all(path.join("wireless")).unwrap();
            }
            if physical {
                fs::create_dir_all(path.join("device")).unwrap();
            }
        };
        let proc = dir.join("wireless");
        fs::write(
            &proc,
            "Inter-| sta-|   Quality        |   Discarded packets\n face | tus | link level noise |  nwid\n wlan0: 0000   35.  -75.  -256        0\n",
        )
        .unwrap();
        iface("lo", "unknown", false, false);
        iface("docker0", "up", false, false);
        assert_eq!(read_network(&dir.join("net"), &proc), Network::Offline, "virtual bridges do not count");
        iface("wlan0", "up", true, true);
        assert_eq!(read_network(&dir.join("net"), &proc), Network::Wireless(50));
        assert_eq!(Network::Wireless(50).icon(), "network-wireless-signal-ok-symbolic");
        iface("enp3s0", "down", false, true);
        assert_eq!(read_network(&dir.join("net"), &proc), Network::Wireless(50));
        iface("enp3s0", "up", false, true);
        assert_eq!(read_network(&dir.join("net"), &proc), Network::Wired);
        assert_eq!(read_network(&dir.join("nope"), &proc), Network::Offline);
        let _ = fs::remove_dir_all(dir);
    }
}
