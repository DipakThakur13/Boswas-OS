//! The shell's view of the system services: the latest state each service
//! reported (see `boswas_services::api`), and what the status icons show.
//!
//! Debug builds can also run without the real services: with
//! `BOSWAS_SHELL_FAKE_SERVICES` set, a small in-process stand-in reports a
//! plausible system and answers commands, so every flyout can be exercised
//! on a development machine. Release builds do not contain it.

use boswas_services::{AudioState, BluetoothState, Brightness, Connectivity, Event, MediaPlayer, NetworkState, PowerProfile, PowerState, TrayItem};

/// What changed when an event was applied; tells the shell what to redraw.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Changed {
    pub status: bool,
    pub tray: bool,
    pub media: bool,
}

/// The latest state of every service.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Sys {
    pub network: NetworkState,
    pub bluetooth: BluetoothState,
    pub audio: AudioState,
    pub power: PowerState,
    /// A power state was received: logind and UPower are being asked.
    pub power_known: bool,
    pub brightness: Option<Brightness>,
    pub tray: Vec<TrayItem>,
    pub media: Vec<MediaPlayer>,
}

impl Sys {
    /// Stores the state an event carries. Events that are not state
    /// (notifications, session, authentication) are left to the caller.
    pub fn apply(&mut self, event: &Event) -> Changed {
        let mut changed = Changed::default();
        match event {
            Event::Network(state) => {
                changed.status = self.network != *state;
                self.network = state.clone();
            }
            Event::Bluetooth(state) => {
                changed.status = self.bluetooth != *state;
                self.bluetooth = state.clone();
            }
            Event::Audio(state) => {
                changed.status = self.audio != *state;
                self.audio = state.clone();
            }
            Event::Power(state) => {
                changed.status = self.power != *state || !self.power_known;
                self.power = state.clone();
                self.power_known = true;
            }
            Event::Brightness(state) => {
                changed.status = self.brightness != *state;
                self.brightness = *state;
            }
            Event::Tray(items) => {
                changed.tray = self.tray != *items;
                self.tray = items.clone();
            }
            Event::Media(players) => {
                changed.media = self.media != *players;
                self.media = players.clone();
            }
            _ => {}
        }
        changed
    }

    /// The taskbar's network icon.
    pub fn network_icon(&self) -> &'static str {
        let n = &self.network;
        if n.airplane {
            return "airplane-mode-symbolic";
        }
        if n.wired && n.connectivity != Connectivity::None {
            return "network-wired-symbolic";
        }
        if let Some(ssid) = &n.wifi_connected {
            let strength = n.access_points.iter().find(|ap| &ap.ssid == ssid).map_or(70, |ap| ap.strength);
            return wifi_icon(strength);
        }
        if n.wifi_present && !n.wifi_enabled {
            return "network-wireless-offline-symbolic";
        }
        "network-offline-symbolic"
    }

    /// One line describing the network for tooltips.
    pub fn network_text(&self) -> String {
        let n = &self.network;
        if !n.available {
            return "Network status is not available".into();
        }
        if n.airplane {
            return "Airplane mode".into();
        }
        let link = match (&n.wifi_connected, n.wired) {
            (_, true) => "Wired network".to_owned(),
            (Some(ssid), false) => ssid.clone(),
            (None, false) => return "Not connected".into(),
        };
        match n.connectivity {
            Connectivity::Full => format!("{link}\nInternet access"),
            Connectivity::Portal => format!("{link}\nSign-in needed"),
            Connectivity::Limited | Connectivity::None => format!("{link}\nNo internet"),
        }
    }

    pub fn volume_icon(&self) -> &'static str {
        match &self.audio.output {
            Some(device) => volume_icon(device.volume, device.muted),
            None => "audio-volume-muted-symbolic",
        }
    }

    pub fn volume_text(&self) -> String {
        match &self.audio.output {
            Some(device) if device.muted => format!("{}: muted", device.name),
            Some(device) => format!("{}: {}%", device.name, (device.volume * 100.0).round() as u32),
            None => "No audio output".into(),
        }
    }

    pub fn battery_icon(&self) -> Option<&'static str> {
        self.power.battery.as_ref().map(|b| battery_icon(b.percent, b.charging || b.full))
    }

    pub fn battery_text(&self) -> Option<String> {
        let battery = self.power.battery.as_ref()?;
        let mut text = format!("Battery {}%", battery.percent);
        if battery.full {
            text.push_str(", fully charged");
        } else if let Some(seconds) = battery.seconds_left {
            let (h, m) = (seconds / 3600, seconds % 3600 / 60);
            let left = if h > 0 { format!("{h} h {m} min") } else { format!("{m} min") };
            text.push_str(&if battery.charging { format!(", {left} until full") } else { format!(", {left} left") });
        } else if battery.charging {
            text.push_str(", charging");
        }
        Some(text)
    }
}

pub fn wifi_icon(strength: u8) -> &'static str {
    match strength {
        0..=19 => "network-wireless-signal-none-symbolic",
        20..=39 => "network-wireless-signal-weak-symbolic",
        40..=59 => "network-wireless-signal-ok-symbolic",
        60..=79 => "network-wireless-signal-good-symbolic",
        _ => "network-wireless-signal-excellent-symbolic",
    }
}

pub fn volume_icon(volume: f32, muted: bool) -> &'static str {
    if muted || volume <= 0.005 {
        "audio-volume-muted-symbolic"
    } else if volume < 0.34 {
        "audio-volume-low-symbolic"
    } else if volume < 0.67 {
        "audio-volume-medium-symbolic"
    } else {
        "audio-volume-high-symbolic"
    }
}

pub fn battery_icon(percent: u8, charging: bool) -> &'static str {
    match (percent, charging) {
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

pub fn profile_name(profile: PowerProfile) -> &'static str {
    match profile {
        PowerProfile::PowerSaver => "Power saver",
        PowerProfile::Balanced => "Balanced",
        PowerProfile::Performance => "Performance",
    }
}

/// The profile after `current` among the available ones.
pub fn next_profile(current: PowerProfile, available: &[PowerProfile]) -> Option<PowerProfile> {
    let position = available.iter().position(|p| *p == current)?;
    available.get((position + 1) % available.len()).copied().filter(|p| *p != current)
}

#[cfg(any(debug_assertions, test))]
#[cfg_attr(not(debug_assertions), allow(dead_code))]
pub mod fake {
    //! A stand-in for the system services, for development and for the
    //! unit tests only.
    use boswas_services::*;

    /// Whether the stand-in was asked for (`BOSWAS_SHELL_FAKE_SERVICES`).
    pub fn enabled() -> bool {
        std::env::var_os("BOSWAS_SHELL_FAKE_SERVICES").is_some()
    }

    pub struct Fake {
        pub network: NetworkState,
        pub bluetooth: BluetoothState,
        pub audio: AudioState,
        pub power: PowerState,
        pub brightness: f32,
    }

    fn ap(ssid: &str, strength: u8, secured: bool, known: bool, connected: bool) -> AccessPoint {
        AccessPoint { ssid: ssid.into(), strength, secured, known, connected, connecting: false }
    }

    impl Default for Fake {
        fn default() -> Self {
            let speakers = AudioDevice { id: 41, name: "Speakers".into(), volume: 0.62, muted: false, is_default: true };
            let headset = AudioDevice { id: 57, name: "USB headset".into(), volume: 0.4, muted: false, is_default: false };
            let mic = AudioDevice { id: 44, name: "Internal microphone".into(), volume: 0.8, muted: false, is_default: true };
            Self {
                network: NetworkState {
                    available: true,
                    connectivity: Connectivity::Full,
                    wifi_present: true,
                    wifi_enabled: true,
                    wifi_connected: Some("Boswas Office".into()),
                    access_points: vec![
                        ap("Boswas Office", 86, true, true, true),
                        ap("Boswas Guest", 71, false, false, false),
                        ap("Chai Point 5G", 55, true, false, false),
                        ap("Asha's iPhone", 43, true, true, false),
                        ap("JioFiber-2841", 28, true, false, false),
                    ],
                    ..NetworkState::default()
                },
                bluetooth: BluetoothState {
                    available: true,
                    enabled: true,
                    discovering: false,
                    devices: vec![
                        BluetoothDevice { address: "AA:BB:CC:00:11:22".into(), name: "WH-1000XM5".into(), icon: "audio-headphones".into(), paired: true, connected: true, connecting: false, battery: Some(80) },
                        BluetoothDevice { address: "AA:BB:CC:00:11:23".into(), name: "MX Keys".into(), icon: "input-keyboard".into(), paired: true, connected: false, connecting: false, battery: None },
                        BluetoothDevice { address: "AA:BB:CC:00:11:24".into(), name: "Pixel 9".into(), icon: "phone".into(), paired: false, connected: false, connecting: false, battery: None },
                    ],
                },
                audio: AudioState { available: true, output: Some(speakers.clone()), input: Some(mic.clone()), outputs: vec![speakers, headset], inputs: vec![mic] },
                power: PowerState {
                    battery: Some(Battery { percent: 76, charging: false, full: false, seconds_left: Some(4 * 3600 + 1260) }),
                    on_ac: false,
                    profile: Some(PowerProfile::Balanced),
                    profiles: vec![PowerProfile::PowerSaver, PowerProfile::Balanced, PowerProfile::Performance],
                    can_suspend: true,
                    can_hibernate: false,
                    can_reboot: true,
                    can_power_off: true,
                },
                brightness: 0.7,
            }
        }
    }

    /// What one line of the development feed stands for. The feed is a
    /// file named by `BOSWAS_SHELL_DEBUG_FEED`; lines appended to it are
    /// read once a second. A line is either a compositor message
    /// (`{"event": "run", "data": {}}`) or one of
    /// `{"debug": "auth"}` and `{"debug": "notify", "summary": "...",
    /// "body": "...", "critical": true}`.
    pub fn feed_line(line: &str, id: u32) -> Vec<Event> {
        let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
            return Vec::new();
        };
        let text = |key: &str| value.get(key).and_then(|v| v.as_str()).unwrap_or_default().to_owned();
        match value.get("debug").and_then(|v| v.as_str()) {
            Some("auth") => vec![
                Event::AuthRequest(AuthRequest {
                    cookie: format!("debug-{id}"),
                    action_id: "org.boswas.sysops.packages.install".into(),
                    message: "Authentication is required to install software".into(),
                    icon_name: String::new(),
                    users: vec!["asha".into()],
                }),
                Event::AuthProgress { cookie: format!("debug-{id}"), progress: AuthProgress::Prompt { text: "Password: ".into(), echo: false } },
            ],
            Some("notify") => vec![Event::Notify(Notification {
                id,
                app_name: "Feed".into(),
                app_icon: "dialog-information-symbolic".into(),
                desktop_entry: None,
                summary: text("summary"),
                body: text("body"),
                actions: Vec::new(),
                urgency: if value.get("critical").and_then(|v| v.as_bool()) == Some(true) { Urgency::Critical } else { Urgency::Normal },
                timeout_ms: None,
                transient: false,
                progress: None,
                time: std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs()),
                replaces_existing: false,
            })],
            _ => Vec::new(),
        }
    }

    impl Fake {
        /// Everything a real service start would report first.
        pub fn initial(&self) -> Vec<Event> {
            let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
            let note = |id: u32, app: &str, icon: &str, summary: &str, body: &str, actions: &[(&str, &str)], progress: Option<u8>, age: u64| Notification {
                id,
                app_name: app.into(),
                app_icon: icon.into(),
                desktop_entry: None,
                summary: summary.into(),
                body: body.into(),
                actions: actions.iter().map(|(k, l)| ((*k).to_owned(), (*l).to_owned())).collect(),
                urgency: Urgency::Normal,
                timeout_ms: Some(600_000),
                transient: false,
                progress,
                time: now.saturating_sub(age),
                replaces_existing: false,
            };
            vec![
                Event::Network(self.network.clone()),
                Event::Bluetooth(self.bluetooth.clone()),
                Event::Audio(self.audio.clone()),
                Event::Power(self.power.clone()),
                Event::Brightness(Some(Brightness { level: self.brightness })),
                Event::Media(vec![MediaPlayer {
                    key: "org.mpris.MediaPlayer2.demo".into(),
                    identity: "Media Player".into(),
                    desktop_entry: None,
                    title: "Raag Yaman".into(),
                    artist: "Hariprasad Chaurasia".into(),
                    art_path: None,
                    playing: true,
                    can_play_pause: true,
                    can_next: true,
                    can_previous: true,
                }]),
                Event::Tray(vec![
                    TrayItem { key: ":1.50/StatusNotifierItem".into(), id: "updates".into(), title: "Updates".into(), icon_name: "system-software-update".into(), icon_theme_path: String::new(), icon_pixmap: None, tooltip: "Boswas OS is up to date".into(), status: TrayStatus::Active, has_menu: true },
                    TrayItem { key: ":1.61/StatusNotifierItem".into(), id: "security".into(), title: "Security".into(), icon_name: "security-high-symbolic".into(), icon_theme_path: String::new(), icon_pixmap: None, tooltip: "Device is protected".into(), status: TrayStatus::Active, has_menu: false },
                    TrayItem { key: ":1.70/StatusNotifierItem".into(), id: "archive".into(), title: "Archive".into(), icon_name: "utilities-file-archiver".into(), icon_theme_path: String::new(), icon_pixmap: None, tooltip: "Archive".into(), status: TrayStatus::Passive, has_menu: false },
                ]),
                Event::Notify(note(1, "Files", "system-file-manager", "Copy finished", "24 items were copied to Documents.", &[("default", "Open"), ("open", "Open folder")], None, 340)),
                Event::Notify(note(2, "Boswas Update Center", "system-software-update", "Updates are ready", "Security updates for 3 packages will be installed tonight.", &[("details", "Details"), ("later", "Later")], None, 95)),
                Event::Notify(note(3, "Firefox", "firefox-esr", "Download", "debian-13-amd64-netinst.iso", &[], Some(64), 20)),
            ]
        }

        /// Applies a command to the stand-in and reports the new state.
        pub fn apply(&mut self, command: &Command) -> Vec<Event> {
            match command {
                Command::WifiSetEnabled(on) => {
                    self.network.wifi_enabled = *on;
                    if !*on {
                        self.network.wifi_connected = None;
                        self.network.connectivity = Connectivity::None;
                        self.network.access_points.iter_mut().for_each(|ap| ap.connected = false);
                    }
                    vec![Event::Network(self.network.clone())]
                }
                Command::WifiConnect { ssid, passphrase } => {
                    let needs = self.network.access_points.iter().any(|ap| &ap.ssid == ssid && ap.secured && !ap.known);
                    if needs && passphrase.as_ref().is_none_or(|p| p.expose().chars().count() < 8) {
                        self.network.error = Some(format!("Could not connect to {ssid}: the passphrase was not accepted"));
                    } else {
                        self.network.error = None;
                        for ap in &mut self.network.access_points {
                            ap.connected = &ap.ssid == ssid;
                            ap.known |= ap.connected;
                        }
                        self.network.wifi_connected = Some(ssid.clone());
                        self.network.connectivity = Connectivity::Full;
                    }
                    vec![Event::Network(self.network.clone())]
                }
                Command::WifiDisconnect => {
                    self.network.wifi_connected = None;
                    self.network.connectivity = Connectivity::None;
                    self.network.access_points.iter_mut().for_each(|ap| ap.connected = false);
                    vec![Event::Network(self.network.clone())]
                }
                Command::WifiForget { ssid } => {
                    for ap in self.network.access_points.iter_mut().filter(|ap| &ap.ssid == ssid) {
                        ap.known = false;
                    }
                    vec![Event::Network(self.network.clone())]
                }
                Command::AirplaneMode(on) => {
                    self.network.airplane = *on;
                    self.network.wifi_enabled = !*on;
                    self.bluetooth.enabled = !*on;
                    vec![Event::Network(self.network.clone()), Event::Bluetooth(self.bluetooth.clone())]
                }
                Command::BluetoothSetEnabled(on) => {
                    self.bluetooth.enabled = *on;
                    vec![Event::Bluetooth(self.bluetooth.clone())]
                }
                Command::BluetoothDiscover(on) => {
                    self.bluetooth.discovering = *on;
                    vec![Event::Bluetooth(self.bluetooth.clone())]
                }
                Command::BluetoothConnect { address } | Command::BluetoothDisconnect { address } => {
                    let connect = matches!(command, Command::BluetoothConnect { .. });
                    for device in self.bluetooth.devices.iter_mut().filter(|d| &d.address == address) {
                        device.connected = connect;
                        device.paired |= connect;
                    }
                    vec![Event::Bluetooth(self.bluetooth.clone())]
                }
                Command::BluetoothForget { address } => {
                    self.bluetooth.devices.retain(|d| &d.address != address);
                    vec![Event::Bluetooth(self.bluetooth.clone())]
                }
                Command::SetVolume(v) | Command::SetInputVolume(v) => {
                    let input = matches!(command, Command::SetInputVolume(_));
                    if let Some(device) = if input { self.audio.input.as_mut() } else { self.audio.output.as_mut() } {
                        device.volume = v.clamp(0.0, 1.5);
                    }
                    vec![Event::Audio(self.audio.clone())]
                }
                Command::SetMuted(m) => {
                    if let Some(device) = self.audio.output.as_mut() {
                        device.muted = *m;
                    }
                    vec![Event::Audio(self.audio.clone())]
                }
                Command::SetDefaultOutput { id } => {
                    for device in &mut self.audio.outputs {
                        device.is_default = device.id == *id;
                    }
                    self.audio.output = self.audio.outputs.iter().find(|d| d.is_default).cloned();
                    vec![Event::Audio(self.audio.clone())]
                }
                Command::SetBrightness(level) => {
                    self.brightness = level.clamp(0.0, 1.0);
                    vec![Event::Brightness(Some(Brightness { level: self.brightness }))]
                }
                Command::SetPowerProfile(profile) => {
                    self.power.profile = Some(*profile);
                    vec![Event::Power(self.power.clone())]
                }
                // The stand-in's password is "boswas".
                Command::AuthRespond { cookie, response } => {
                    if response.expose() == "boswas" {
                        vec![Event::AuthProgress { cookie: cookie.clone(), progress: AuthProgress::Done { authorized: true } }]
                    } else {
                        vec![
                            Event::AuthProgress { cookie: cookie.clone(), progress: AuthProgress::Failed("Sorry, that password is not right.".into()) },
                            Event::AuthProgress { cookie: cookie.clone(), progress: AuthProgress::Prompt { text: "Password: ".into(), echo: false } },
                        ]
                    }
                }
                Command::AuthCancel { cookie } => {
                    vec![Event::AuthProgress { cookie: cookie.clone(), progress: AuthProgress::Done { authorized: false } }]
                }
                _ => Vec::new(),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use boswas_services::{AccessPoint, AudioDevice, Battery};

    #[test]
    fn events_replace_state_and_report_what_changed() {
        let mut sys = Sys::default();
        let network = NetworkState { available: true, wired: true, connectivity: Connectivity::Full, ..NetworkState::default() };
        assert!(sys.apply(&Event::Network(network.clone())).status);
        assert!(!sys.apply(&Event::Network(network)).status, "the same state again changes nothing");
        assert_eq!(sys.network_icon(), "network-wired-symbolic");
        assert!(sys.apply(&Event::Power(PowerState::default())).status, "the first power state is news even when empty");
        assert!(sys.power_known);
        assert!(sys.apply(&Event::Tray(vec![])) == Changed::default());
        assert!(sys.apply(&Event::Brightness(Some(Brightness { level: 0.5 }))).status);
    }

    #[test]
    fn status_icons_follow_the_state() {
        let mut sys = Sys::default();
        assert_eq!(sys.network_icon(), "network-offline-symbolic");
        assert_eq!(sys.network_text(), "Network status is not available");
        sys.network = NetworkState {
            available: true,
            wifi_present: true,
            wifi_enabled: true,
            wifi_connected: Some("Home".into()),
            connectivity: Connectivity::Portal,
            access_points: vec![AccessPoint { ssid: "Home".into(), strength: 45, secured: true, known: true, connected: true, connecting: false }],
            ..NetworkState::default()
        };
        assert_eq!(sys.network_icon(), "network-wireless-signal-ok-symbolic");
        assert_eq!(sys.network_text(), "Home\nSign-in needed");
        sys.network.wifi_enabled = false;
        sys.network.wifi_connected = None;
        assert_eq!(sys.network_icon(), "network-wireless-offline-symbolic");
        sys.network.airplane = true;
        assert_eq!(sys.network_icon(), "airplane-mode-symbolic");

        assert_eq!(sys.volume_icon(), "audio-volume-muted-symbolic");
        assert_eq!(sys.volume_text(), "No audio output");
        sys.audio.output = Some(AudioDevice { id: 1, name: "Speakers".into(), volume: 0.5, muted: false, is_default: true });
        assert_eq!(sys.volume_icon(), "audio-volume-medium-symbolic");
        assert_eq!(sys.volume_text(), "Speakers: 50%");
        assert_eq!(volume_icon(0.9, true), "audio-volume-muted-symbolic");
        assert_eq!(volume_icon(0.1, false), "audio-volume-low-symbolic");
        assert_eq!(volume_icon(1.2, false), "audio-volume-high-symbolic");

        assert_eq!(sys.battery_icon(), None);
        sys.power.battery = Some(Battery { percent: 12, charging: false, full: false, seconds_left: Some(1500) });
        assert_eq!(sys.battery_icon(), Some("battery-caution-symbolic"));
        assert_eq!(sys.battery_text().unwrap(), "Battery 12%, 25 min left");
        sys.power.battery = Some(Battery { percent: 100, charging: true, full: true, seconds_left: None });
        assert_eq!(sys.battery_icon(), Some("battery-full-charging-symbolic"));
        assert_eq!(sys.battery_text().unwrap(), "Battery 100%, fully charged");
        sys.power.battery = Some(Battery { percent: 60, charging: true, full: false, seconds_left: Some(5400) });
        assert_eq!(sys.battery_text().unwrap(), "Battery 60%, 1 h 30 min until full");
    }

    #[test]
    fn power_profiles_cycle_through_what_is_available() {
        let all = [PowerProfile::PowerSaver, PowerProfile::Balanced, PowerProfile::Performance];
        assert_eq!(next_profile(PowerProfile::Balanced, &all), Some(PowerProfile::Performance));
        assert_eq!(next_profile(PowerProfile::Performance, &all), Some(PowerProfile::PowerSaver));
        assert_eq!(next_profile(PowerProfile::Balanced, &[PowerProfile::Balanced]), None);
        assert_eq!(next_profile(PowerProfile::Balanced, &[]), None);
        assert_eq!(profile_name(PowerProfile::PowerSaver), "Power saver");
    }

    #[test]
    fn the_stand_in_answers_commands() {
        use boswas_services::{Command, Secret};
        let mut fake = fake::Fake::default();
        assert!(fake.initial().len() > 8);
        let events = fake.apply(&Command::WifiConnect { ssid: "Chai Point 5G".into(), passphrase: Some(Secret::new("short".into())) });
        assert!(matches!(&events[..], [Event::Network(n)] if n.error.is_some() && n.wifi_connected.as_deref() == Some("Boswas Office")));
        let events = fake.apply(&Command::WifiConnect { ssid: "Chai Point 5G".into(), passphrase: Some(Secret::new("long enough".into())) });
        assert!(matches!(&events[..], [Event::Network(n)] if n.error.is_none() && n.wifi_connected.as_deref() == Some("Chai Point 5G")));
        let events = fake.apply(&Command::SetVolume(2.0));
        assert!(matches!(&events[..], [Event::Audio(a)] if a.output.as_ref().unwrap().volume == 1.5));
        assert!(fake.apply(&Command::WifiScan).is_empty());
    }
}
