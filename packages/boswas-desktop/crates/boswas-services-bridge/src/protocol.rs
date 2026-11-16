//! The bridge's wire format: one JSON object per line in each direction.
//!
//! Events (standard output) carry a service's whole state, as the shell's
//! services report it:
//!
//! ```text
//! {"event":"ready","version":"3.0.0-alpha.1"}
//! {"event":"network","available":true,"wifi_enabled":true,"access_points":[...],...}
//! {"event":"bluetooth",...}  {"event":"audio",...}  {"event":"power",...}
//! {"event":"brightness","level":0.8}
//! {"event":"error","message":"..."}        a line that was not understood
//! ```
//!
//! Commands (standard input) are `{"cmd": NAME, ...}`; see [`command`].
//! A Wi-Fi passphrase travels on this pipe only, never on a command line.

use boswas_services::{
    AccessPoint, AudioDevice, AudioState, Battery, BluetoothDevice, BluetoothState, Command, Connectivity, Event,
    NetworkState, PowerProfile, PowerState, Secret, Service,
};
use serde_json::{json, Map, Value};

/// The longest line the bridge reads.
pub const MAX_LINE: usize = 16 * 1024;
/// An SSID is at most 32 bytes.
const MAX_SSID: usize = 32;
/// Room for a WPA passphrase (63 characters), a raw key (64) and enterprise
/// passwords.
const MAX_SECRET: usize = 256;

fn connectivity(value: Connectivity) -> &'static str {
    match value {
        Connectivity::None => "none",
        Connectivity::Limited => "limited",
        Connectivity::Portal => "portal",
        Connectivity::Full => "full",
    }
}

fn profile_name(profile: PowerProfile) -> &'static str {
    match profile {
        PowerProfile::PowerSaver => "power-saver",
        PowerProfile::Balanced => "balanced",
        PowerProfile::Performance => "performance",
    }
}

fn access_point(ap: &AccessPoint) -> Value {
    json!({
        "ssid": ap.ssid,
        "strength": ap.strength,
        "secured": ap.secured,
        "known": ap.known,
        "connected": ap.connected,
        "connecting": ap.connecting,
    })
}

fn network(state: &NetworkState) -> Value {
    json!({
        "event": "network",
        "available": state.available,
        "connectivity": connectivity(state.connectivity),
        "wired": state.wired,
        "wifi_present": state.wifi_present,
        "wifi_enabled": state.wifi_enabled,
        "wifi_connected": state.wifi_connected,
        "wifi_scanning": state.wifi_scanning,
        "access_points": state.access_points.iter().map(access_point).collect::<Vec<_>>(),
        "vpn": state.vpn,
        "airplane": state.airplane,
        "error": state.error,
    })
}

fn bluetooth_device(device: &BluetoothDevice) -> Value {
    json!({
        "address": device.address,
        "name": device.name,
        "icon": device.icon,
        "paired": device.paired,
        "connected": device.connected,
        "connecting": device.connecting,
        "battery": device.battery,
    })
}

fn bluetooth(state: &BluetoothState) -> Value {
    json!({
        "event": "bluetooth",
        "available": state.available,
        "enabled": state.enabled,
        "discovering": state.discovering,
        "devices": state.devices.iter().map(bluetooth_device).collect::<Vec<_>>(),
    })
}

fn audio_device(device: &AudioDevice) -> Value {
    json!({
        "id": device.id,
        "name": device.name,
        "volume": device.volume,
        "muted": device.muted,
        "default": device.is_default,
    })
}

fn audio(state: &AudioState) -> Value {
    json!({
        "event": "audio",
        "available": state.available,
        "output": state.output.as_ref().map(audio_device),
        "input": state.input.as_ref().map(audio_device),
        "outputs": state.outputs.iter().map(audio_device).collect::<Vec<_>>(),
        "inputs": state.inputs.iter().map(audio_device).collect::<Vec<_>>(),
    })
}

fn battery(battery: &Battery) -> Value {
    json!({
        "percent": battery.percent,
        "charging": battery.charging,
        "full": battery.full,
        "seconds_left": battery.seconds_left,
    })
}

fn power(state: &PowerState) -> Value {
    json!({
        "event": "power",
        "battery": state.battery.as_ref().map(battery),
        "on_ac": state.on_ac,
        "profile": state.profile.map(profile_name),
        "profiles": state.profiles.iter().copied().map(profile_name).collect::<Vec<_>>(),
        "can_suspend": state.can_suspend,
        "can_hibernate": state.can_hibernate,
    })
}

/// The line for an event, or `None` for events that are not Settings'
/// business (notifications, the tray, authentication).
pub fn event(event: &Event) -> Option<Value> {
    Some(match event {
        Event::Network(state) => network(state),
        Event::Bluetooth(state) => bluetooth(state),
        Event::Audio(state) => audio(state),
        Event::Power(state) => power(state),
        Event::Brightness(level) => json!({ "event": "brightness", "level": level.map(|b| b.level) }),
        _ => return None,
    })
}

pub fn ready() -> Value {
    json!({ "event": "ready", "version": env!("CARGO_PKG_VERSION") })
}

pub fn error(message: &str) -> Value {
    json!({ "event": "error", "message": message })
}

fn text(object: &mut Map<String, Value>, key: &str) -> Result<String, String> {
    match object.remove(key) {
        Some(Value::String(text)) => Ok(text),
        Some(_) => Err(format!("\"{key}\" must be text")),
        None => Err(format!("missing \"{key}\"")),
    }
}

fn flag(object: &Map<String, Value>, key: &str) -> Result<bool, String> {
    object.get(key).and_then(Value::as_bool).ok_or_else(|| format!("\"{key}\" must be true or false"))
}

fn number(object: &Map<String, Value>, key: &str, low: f64, high: f64) -> Result<f32, String> {
    let value = object.get(key).and_then(Value::as_f64).ok_or_else(|| format!("\"{key}\" must be a number"))?;
    if !value.is_finite() || value < low || value > high {
        return Err(format!("\"{key}\" must be between {low} and {high}"));
    }
    Ok(value as f32)
}

fn id(object: &Map<String, Value>) -> Result<u32, String> {
    object
        .get("id")
        .and_then(Value::as_u64)
        .and_then(|id| u32::try_from(id).ok())
        .ok_or_else(|| "\"id\" must be a device id".to_string())
}

fn ssid(object: &mut Map<String, Value>) -> Result<String, String> {
    let ssid = text(object, "ssid")?;
    if ssid.is_empty() || ssid.len() > MAX_SSID || ssid.contains('\0') {
        return Err("\"ssid\" must be 1 to 32 bytes".into());
    }
    Ok(ssid)
}

/// `AA:BB:CC:DD:EE:FF`, in upper case.
fn address(object: &mut Map<String, Value>) -> Result<String, String> {
    let address = text(object, "address")?;
    let parts: Vec<&str> = address.split(':').collect();
    let valid = parts.len() == 6 && parts.iter().all(|part| part.len() == 2 && part.bytes().all(|b| b.is_ascii_hexdigit()));
    if !valid {
        return Err("\"address\" must look like AA:BB:CC:DD:EE:FF".into());
    }
    Ok(address.to_ascii_uppercase())
}

/// Parses one command line.
///
/// | `cmd` | fields |
/// |---|---|
/// | `refresh` | `service`: `network`, `bluetooth`, `audio`, `power` or `brightness` |
/// | `wifi-set-enabled`, `airplane-mode`, `bluetooth-set-enabled`, `bluetooth-discover` | `enabled` |
/// | `wifi-scan`, `wifi-disconnect` | |
/// | `wifi-connect` | `ssid`, optional `passphrase` |
/// | `wifi-forget` | `ssid` |
/// | `bluetooth-connect`, `bluetooth-disconnect`, `bluetooth-forget` | `address` |
/// | `set-volume`, `set-input-volume` | `volume` 0 to 1.5 |
/// | `set-muted`, `set-input-muted` | `muted` |
/// | `set-default-output`, `set-default-input` | `id` |
/// | `set-brightness` | `level` 0 to 1 |
/// | `set-power-profile` | `profile`: `power-saver`, `balanced` or `performance` |
///
/// Anything else is an error: the bridge offers Settings' commands only,
/// not the shell's (no power actions, notifications or authentication).
pub fn command(line: &str) -> Result<Command, String> {
    if line.len() > MAX_LINE {
        return Err("the line is too long".into());
    }
    let Value::Object(mut object) = serde_json::from_str::<Value>(line).map_err(|_| "not a JSON object".to_string())? else {
        return Err("not a JSON object".into());
    };
    let cmd = text(&mut object, "cmd")?;
    Ok(match cmd.as_str() {
        "refresh" => Command::Refresh(match text(&mut object, "service")?.as_str() {
            "network" => Service::Network,
            "bluetooth" => Service::Bluetooth,
            "audio" => Service::Audio,
            "power" => Service::Power,
            "brightness" => Service::Brightness,
            _ => return Err("unknown \"service\"".into()),
        }),
        "wifi-set-enabled" => Command::WifiSetEnabled(flag(&object, "enabled")?),
        "wifi-scan" => Command::WifiScan,
        "wifi-connect" => {
            let ssid = ssid(&mut object)?;
            let passphrase = match object.remove("passphrase") {
                None | Some(Value::Null) => None,
                Some(Value::String(secret)) => {
                    let secret = Secret::new(secret);
                    if secret.expose().is_empty() || secret.expose().len() > MAX_SECRET || secret.expose().contains('\0') {
                        return Err("\"passphrase\" must be 1 to 256 bytes".into());
                    }
                    Some(secret)
                }
                Some(_) => return Err("\"passphrase\" must be text".into()),
            };
            Command::WifiConnect { ssid, passphrase }
        }
        "wifi-disconnect" => Command::WifiDisconnect,
        "wifi-forget" => Command::WifiForget { ssid: ssid(&mut object)? },
        "airplane-mode" => Command::AirplaneMode(flag(&object, "enabled")?),
        "bluetooth-set-enabled" => Command::BluetoothSetEnabled(flag(&object, "enabled")?),
        "bluetooth-discover" => Command::BluetoothDiscover(flag(&object, "enabled")?),
        "bluetooth-connect" => Command::BluetoothConnect { address: address(&mut object)? },
        "bluetooth-disconnect" => Command::BluetoothDisconnect { address: address(&mut object)? },
        "bluetooth-forget" => Command::BluetoothForget { address: address(&mut object)? },
        "set-volume" => Command::SetVolume(number(&object, "volume", 0.0, 1.5)?),
        "set-muted" => Command::SetMuted(flag(&object, "muted")?),
        "set-input-volume" => Command::SetInputVolume(number(&object, "volume", 0.0, 1.5)?),
        "set-input-muted" => Command::SetInputMuted(flag(&object, "muted")?),
        "set-default-output" => Command::SetDefaultOutput { id: id(&object)? },
        "set-default-input" => Command::SetDefaultInput { id: id(&object)? },
        "set-brightness" => Command::SetBrightness(number(&object, "level", 0.0, 1.0)?),
        "set-power-profile" => Command::SetPowerProfile(match text(&mut object, "profile")?.as_str() {
            "power-saver" => PowerProfile::PowerSaver,
            "balanced" => PowerProfile::Balanced,
            "performance" => PowerProfile::Performance,
            _ => return Err("unknown \"profile\"".into()),
        }),
        _ => return Err("unknown \"cmd\"".into()),
    })
}

#[cfg(test)]
mod tests {
    use boswas_services::{Brightness, Notification, PowerAction};

    use super::*;

    #[test]
    fn every_settings_command_is_understood() {
        let cases: Vec<(&str, Command)> = vec![
            (r#"{"cmd":"refresh","service":"audio"}"#, Command::Refresh(Service::Audio)),
            (r#"{"cmd":"wifi-set-enabled","enabled":false}"#, Command::WifiSetEnabled(false)),
            (r#"{"cmd":"wifi-scan"}"#, Command::WifiScan),
            (
                r#"{"cmd":"wifi-connect","ssid":"Home"}"#,
                Command::WifiConnect {
                    ssid: "Home".into(),
                    passphrase: None,
                },
            ),
            (
                r#"{"cmd":"wifi-connect","ssid":"Café 5G","passphrase":"correct horse"}"#,
                Command::WifiConnect {
                    ssid: "Café 5G".into(),
                    passphrase: Some(Secret::new("correct horse".into())),
                },
            ),
            (r#"{"cmd":"wifi-disconnect"}"#, Command::WifiDisconnect),
            (r#"{"cmd":"wifi-forget","ssid":"Home"}"#, Command::WifiForget { ssid: "Home".into() }),
            (r#"{"cmd":"airplane-mode","enabled":true}"#, Command::AirplaneMode(true)),
            (r#"{"cmd":"bluetooth-set-enabled","enabled":true}"#, Command::BluetoothSetEnabled(true)),
            (r#"{"cmd":"bluetooth-discover","enabled":false}"#, Command::BluetoothDiscover(false)),
            (
                r#"{"cmd":"bluetooth-connect","address":"aa:bb:cc:dd:ee:0f"}"#,
                Command::BluetoothConnect {
                    address: "AA:BB:CC:DD:EE:0F".into(),
                },
            ),
            (
                r#"{"cmd":"bluetooth-disconnect","address":"AA:BB:CC:DD:EE:FF"}"#,
                Command::BluetoothDisconnect {
                    address: "AA:BB:CC:DD:EE:FF".into(),
                },
            ),
            (
                r#"{"cmd":"bluetooth-forget","address":"AA:BB:CC:DD:EE:FF"}"#,
                Command::BluetoothForget {
                    address: "AA:BB:CC:DD:EE:FF".into(),
                },
            ),
            (r#"{"cmd":"set-volume","volume":0.5}"#, Command::SetVolume(0.5)),
            (r#"{"cmd":"set-muted","muted":true}"#, Command::SetMuted(true)),
            (r#"{"cmd":"set-input-volume","volume":1.5}"#, Command::SetInputVolume(1.5)),
            (r#"{"cmd":"set-input-muted","muted":false}"#, Command::SetInputMuted(false)),
            (r#"{"cmd":"set-default-output","id":42}"#, Command::SetDefaultOutput { id: 42 }),
            (r#"{"cmd":"set-default-input","id":7}"#, Command::SetDefaultInput { id: 7 }),
            (r#"{"cmd":"set-brightness","level":1}"#, Command::SetBrightness(1.0)),
            (
                r#"{"cmd":"set-power-profile","profile":"power-saver"}"#,
                Command::SetPowerProfile(PowerProfile::PowerSaver),
            ),
        ];
        for (line, expected) in cases {
            assert_eq!(command(line), Ok(expected), "{line}");
        }
    }

    #[test]
    fn hostile_and_out_of_scope_lines_are_refused() {
        for line in [
            "",
            "lock",
            "[]",
            "42",
            r#"{"ssid":"x"}"#,
            r#"{"cmd":7}"#,
            r#"{"cmd":"wifi-connect"}"#,
            r#"{"cmd":"wifi-connect","ssid":""}"#,
            r#"{"cmd":"wifi-connect","ssid":"123456789012345678901234567890123"}"#,
            r#"{"cmd":"wifi-connect","ssid":"x","passphrase":7}"#,
            r#"{"cmd":"wifi-connect","ssid":"x","passphrase":""}"#,
            r#"{"cmd":"wifi-connect","ssid":"a\u0000b"}"#,
            r#"{"cmd":"bluetooth-connect","address":"AA:BB:CC:DD:EE"}"#,
            r#"{"cmd":"bluetooth-connect","address":"AA:BB:CC:DD:EE:GG"}"#,
            r#"{"cmd":"bluetooth-connect","address":"/org/bluez/hci0"}"#,
            r#"{"cmd":"set-volume","volume":2}"#,
            r#"{"cmd":"set-volume","volume":-0.1}"#,
            r#"{"cmd":"set-volume","volume":"loud"}"#,
            r#"{"cmd":"set-brightness","level":1.01}"#,
            r#"{"cmd":"set-default-output","id":-1}"#,
            r#"{"cmd":"set-default-output","id":4294967296}"#,
            r#"{"cmd":"set-power-profile","profile":"turbo"}"#,
            r#"{"cmd":"refresh","service":"tray"}"#,
            // The shell's commands are not Settings' commands.
            r#"{"cmd":"power","action":"power-off"}"#,
            r#"{"cmd":"auth-respond","cookie":"1","response":"x"}"#,
            r#"{"cmd":"notification-dismiss","id":1}"#,
        ] {
            assert!(command(line).is_err(), "{line}");
        }
        let long = format!(r#"{{"cmd":"wifi-scan","pad":"{}"}}"#, "x".repeat(MAX_LINE));
        assert_eq!(command(&long), Err("the line is too long".into()));
        let secret = format!(r#"{{"cmd":"wifi-connect","ssid":"x","passphrase":"{}"}}"#, "p".repeat(MAX_SECRET + 1));
        assert!(command(&secret).is_err());
    }

    #[test]
    fn a_passphrase_is_never_printed() {
        let parsed = command(r#"{"cmd":"wifi-connect","ssid":"Home","passphrase":"hunter2hunter2"}"#).unwrap();
        assert!(!format!("{parsed:?}").contains("hunter2"));
    }

    #[test]
    fn states_become_one_object_each() {
        let state = NetworkState {
            available: true,
            connectivity: Connectivity::Full,
            wifi_present: true,
            wifi_enabled: true,
            wifi_connected: Some("Home".into()),
            access_points: vec![AccessPoint {
                ssid: "Home".into(),
                strength: 80,
                secured: true,
                known: true,
                connected: true,
                connecting: false,
            }],
            ..NetworkState::default()
        };
        let value = event(&Event::Network(state)).unwrap();
        assert_eq!(value["event"], "network");
        assert_eq!(value["connectivity"], "full");
        assert_eq!(value["wifi_connected"], "Home");
        assert_eq!(value["access_points"][0]["strength"], 80);
        assert_eq!(value["error"], Value::Null);
        // One line: no newline can come out of the serializer.
        assert!(!value.to_string().contains('\n'));

        let bt = BluetoothState {
            available: true,
            enabled: true,
            discovering: false,
            devices: vec![BluetoothDevice {
                address: "AA:BB:CC:DD:EE:FF".into(),
                name: "Head\nphones".into(),
                icon: "audio-headphones".into(),
                paired: true,
                connected: false,
                connecting: false,
                battery: Some(70),
            }],
        };
        let value = event(&Event::Bluetooth(bt)).unwrap();
        assert_eq!(value["devices"][0]["battery"], 70);
        assert!(!value.to_string().contains('\n'), "a newline in a device name is escaped");

        let device = AudioDevice {
            id: 5,
            name: "Speakers".into(),
            volume: 0.5,
            muted: false,
            is_default: true,
        };
        let value = event(&Event::Audio(AudioState {
            available: true,
            output: Some(device.clone()),
            input: None,
            outputs: vec![device],
            inputs: vec![],
        }))
        .unwrap();
        assert_eq!(value["output"]["id"], 5);
        assert_eq!(value["output"]["default"], true);
        assert_eq!(value["input"], Value::Null);

        let value = event(&Event::Power(PowerState {
            battery: Some(Battery {
                percent: 42,
                charging: true,
                full: false,
                seconds_left: Some(3600),
            }),
            on_ac: true,
            profile: Some(PowerProfile::Balanced),
            profiles: vec![PowerProfile::PowerSaver, PowerProfile::Balanced],
            ..PowerState::default()
        }))
        .unwrap();
        assert_eq!(value["battery"]["percent"], 42);
        assert_eq!(value["profile"], "balanced");
        assert_eq!(value["profiles"], json!(["power-saver", "balanced"]));

        assert_eq!(event(&Event::Brightness(Some(Brightness { level: 0.25 }))).unwrap()["level"], 0.25);
        assert_eq!(event(&Event::Brightness(None)).unwrap()["level"], Value::Null);
    }

    #[test]
    fn the_shells_events_are_not_forwarded() {
        let notification = Event::NotificationClosed { id: 1, withdrawn: false };
        assert_eq!(event(&notification), None);
        assert_eq!(event(&Event::Tray(Vec::new())), None);
        // The types exist so that this list is checked when the API grows.
        let _ = (std::mem::size_of::<Notification>(), PowerAction::Suspend);
    }
}
