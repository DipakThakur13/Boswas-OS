//! The network service against a stand-in NetworkManager.

use boswas_services::{AccessPoint, Command, Connectivity, Event, NetworkState, Secret, Service};
use zbus::zvariant::OwnedObjectPath;

use crate::{mocks::*, support::*};

fn network(event: &Event) -> Option<&NetworkState> {
    match event {
        Event::Network(state) => Some(state),
        _ => None,
    }
}

fn ap<'a>(state: &'a NetworkState, ssid: &str) -> &'a AccessPoint {
    state.access_points.iter().find(|ap| ap.ssid == ssid).unwrap_or_else(|| panic!("no network {ssid} in {state:#?}"))
}

fn text(settings: &Settings, section: &str, key: &str) -> Option<String> {
    settings.get(section)?.get(key).and_then(|value| <&str>::try_from(&**value).ok()).map(str::to_owned)
}

#[test]
fn without_networkmanager_the_network_is_unavailable() {
    let harness = Harness::new();
    let services = harness.start(status_config());
    assert_eq!(harness.events.latest("the first state", network, |_| true), NetworkState::default());
    // Commands go nowhere and break nothing.
    services.send(Command::WifiScan);
    services.send(Command::WifiConnect { ssid: "x".into(), passphrase: None });
    services.send(Command::AirplaneMode(true));
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Network));
    assert!(!harness.events.after(before, "the state again", |event| network(event).cloned()).available);
}

#[test]
fn state_is_read_and_follows_networkmanager() {
    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    nm.add_access_point(1, b"Home", 40, WPA2);
    nm.add_access_point(2, b"Home", 75, WPA2);
    nm.add_access_point(3, b"Cafe", 60, OPEN);
    nm.add_access_point(4, b"", 90, WPA2);
    nm.add_access_point(5, &[0, 0, 0, 0], 91, WPA2);
    nm.add_profile(b"Home");
    let _services = harness.start(status_config());

    let state = harness.events.latest("the networks", network, |state| state.access_points.len() == 2);
    assert_eq!(
        state,
        NetworkState {
            available: true,
            connectivity: Connectivity::None,
            wired: false,
            wifi_present: true,
            wifi_enabled: true,
            wifi_connected: None,
            wifi_scanning: false,
            // One entry per SSID with the strongest signal, strongest
            // first; hidden networks are left out.
            access_points: vec![
                AccessPoint { ssid: "Home".into(), strength: 75, secured: true, known: true, connected: false, connecting: false },
                AccessPoint { ssid: "Cafe".into(), strength: 60, secured: false, known: false, connected: false, connecting: false },
            ],
            vpn: false,
            airplane: false,
            error: None,
        }
    );

    // The cable is plugged in.
    nm.device_state(ETHERNET, 100, 0);
    nm.peer.set(NM_PATH, NM, "Connectivity", owned(4u32));
    harness.events.latest("wired with internet", network, |state| state.wired && state.connectivity == Connectivity::Full);
    nm.peer.set(NM_PATH, NM, "Connectivity", owned(2u32));
    harness.events.latest("a portal", network, |state| state.connectivity == Connectivity::Portal);
    nm.peer.set(NM_PATH, NM, "Connectivity", owned(3u32));
    harness.events.latest("limited", network, |state| state.connectivity == Connectivity::Limited);

    // A VPN comes up.
    let vpn = "/org/freedesktop/NetworkManager/ActiveConnection/9";
    nm.peer.add(vpn, NM_ACTIVE, props([("Type", owned("wireguard")), ("State", owned(1u32)), ("Vpn", owned(false))]));
    nm.peer.set(NM_PATH, NM, "ActiveConnections", paths_value(&[vpn]));
    nm.peer.set(vpn, NM_ACTIVE, "State", owned(2u32));
    harness.events.latest("the VPN", network, |state| state.vpn);
    nm.peer.set(NM_PATH, NM, "ActiveConnections", paths_value(&[]));
    harness.events.latest("no VPN", network, |state| !state.vpn);

    // Signal strengths change, a network goes out of range.
    nm.peer.set(&format!("{NM_PATH}/AccessPoint/3"), NM_AP, "Strength", owned(88u8));
    let state = harness.events.latest("the cafe first", network, |state| state.access_points[0].ssid == "Cafe");
    assert_eq!(state.access_points[0].strength, 88);
    nm.remove_access_point(&format!("{NM_PATH}/AccessPoint/3"));
    harness.events.latest("only home", network, |state| state.access_points.len() == 1);
    nm.remove_access_point(&format!("{NM_PATH}/AccessPoint/2"));
    harness.events.latest("the weaker home", network, |state| state.access_points[0].strength == 40);

    // NetworkManager stops and starts again.
    nm.peer.release(NM);
    harness.events.latest("unavailable", network, |state| !state.available && state.access_points.is_empty());
    nm.peer.claim(NM);
    harness.events.latest("available again", network, |state| state.available && state.access_points.len() == 1);
}

#[test]
fn wifi_switch_and_scan() {
    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    let services = harness.start(status_config());
    harness.events.latest("wifi", network, |state| state.wifi_present && state.wifi_enabled);

    services.send(Command::WifiSetEnabled(false));
    let set = nm.peer.wait_call("Set");
    let (interface, name, value): (String, String, zbus::zvariant::OwnedValue) = set.args();
    assert_eq!((interface.as_str(), name.as_str(), bool::try_from(value).unwrap()), (NM, "WirelessEnabled", false));
    harness.events.latest("wifi off", network, |state| !state.wifi_enabled);
    services.send(Command::WifiSetEnabled(true));
    harness.events.latest("wifi on", network, |state| state.wifi_enabled);

    services.send(Command::WifiScan);
    assert_eq!(nm.peer.wait_call("RequestScan").path, WIFI);
    harness.events.latest("scanning", network, |state| state.wifi_scanning);
    // The scan finds a network and finishes.
    nm.add_access_point(1, b"Found", 50, WPA3);
    nm.peer.set(WIFI, NM_WIRELESS, "LastScan", owned(2000i64));
    let state = harness.events.latest("the scan result", network, |state| !state.wifi_scanning);
    assert_eq!(ap(&state, "Found").strength, 50);
    assert!(ap(&state, "Found").secured);

    // A scan NetworkManager refuses is not shown as running.
    nm.peer.on(NM_WIRELESS, "RequestScan", |_, call| call.error("org.freedesktop.NetworkManager.Device.NotAllowed", "too soon"));
    services.send(Command::WifiScan);
    nm.peer.wait_call_n("RequestScan", 2);
    nm.add_access_point(2, b"Later", 30, OPEN);
    let state = harness.events.latest("the later network", network, |state| state.access_points.len() == 2);
    assert!(!state.wifi_scanning);
}

#[test]
fn connecting_to_a_known_network_activates_its_profile() {
    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    let weak = nm.add_access_point(1, b"Home", 30, WPA2);
    let strong = nm.add_access_point(2, b"Home", 80, WPA2);
    let profile = nm.add_profile(b"Home");
    let services = harness.start(status_config());
    harness.events.latest("the known network", network, |state| state.access_points.len() == 1 && state.access_points[0].known);

    services.send(Command::WifiConnect { ssid: "Home".into(), passphrase: None });
    let call = nm.peer.wait_call("ActivateConnection");
    let (connection, device, access_point): (OwnedObjectPath, OwnedObjectPath, OwnedObjectPath) = call.args();
    assert_eq!((connection.as_str(), device.as_str(), access_point.as_str()), (profile.as_str(), WIFI, strong.as_str()));
    let state = harness.events.latest("connecting", network, |state| ap(state, "Home").connecting);
    assert!(!ap(&state, "Home").connected && state.wifi_connected.is_none());

    nm.device_state(WIFI, 50, 0);
    nm.connected_to(&strong);
    let state = harness.events.latest("connected", network, |state| state.wifi_connected.as_deref() == Some("Home"));
    assert!(ap(&state, "Home").connected && !ap(&state, "Home").connecting);
    assert_eq!(state.error, None);
    assert!(nm.peer.calls("AddAndActivateConnection").is_empty());
    let _ = weak;

    // Disconnect.
    services.send(Command::WifiDisconnect);
    assert_eq!(nm.peer.wait_call("Disconnect").path, WIFI);
    nm.peer.set(WIFI, NM_WIRELESS, "ActiveAccessPoint", path_value("/"));
    nm.device_state(WIFI, 30, 39);
    harness.events.latest("disconnected", network, |state| state.wifi_connected.is_none() && !ap(state, "Home").connected);

    // Forget: the profile is deleted and the network is not known any more.
    services.send(Command::WifiForget { ssid: "Home".into() });
    assert_eq!(nm.peer.wait_call("Delete").path, profile);
    harness.events.latest("forgotten", network, |state| !ap(state, "Home").known);
    assert!(nm.profile_paths().is_empty());
    // Forgetting what is not saved does nothing.
    services.send(Command::WifiForget { ssid: "Nowhere".into() });
}

#[test]
fn connecting_with_a_passphrase_creates_a_profile() {
    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    let home = nm.add_access_point(1, b"Home", 80, WPA2);
    let modern = nm.add_access_point(2, b"Modern", 70, WPA3);
    let cafe = nm.add_access_point(3, b"Cafe", 60, OPEN);
    let services = harness.start(status_config());
    harness.events.latest("three networks", network, |state| state.access_points.len() == 3);

    services.send(Command::WifiConnect { ssid: "Home".into(), passphrase: Some(Secret::new("correct horse".into())) });
    let call = nm.peer.wait_call("AddAndActivateConnection");
    let (settings, device, access_point): (Settings, OwnedObjectPath, OwnedObjectPath) = call.args();
    assert_eq!((device.as_str(), access_point.as_str()), (WIFI, home.as_str()));
    assert_eq!(text(&settings, "connection", "type").as_deref(), Some("802-11-wireless"));
    let ssid: Vec<u8> = settings["802-11-wireless"]["ssid"].try_clone().unwrap().try_into().unwrap();
    assert_eq!(ssid, b"Home");
    assert_eq!(text(&settings, "802-11-wireless-security", "key-mgmt").as_deref(), Some("wpa-psk"));
    assert_eq!(text(&settings, "802-11-wireless-security", "psk").as_deref(), Some("correct horse"));
    nm.connected_to(&home);
    let state = harness.events.latest("connected and known", network, |state| ap(state, "Home").connected && ap(state, "Home").known);
    assert_eq!(state.error, None);

    // WPA3 only: the same passphrase goes in as SAE.
    services.send(Command::WifiConnect { ssid: "Modern".into(), passphrase: Some(Secret::new("battery staple".into())) });
    let call = nm.peer.wait_call_n("AddAndActivateConnection", 2);
    let (settings, _, access_point): (Settings, OwnedObjectPath, OwnedObjectPath) = call.args();
    assert_eq!(access_point.as_str(), modern);
    assert_eq!(text(&settings, "802-11-wireless-security", "key-mgmt").as_deref(), Some("sae"));
    nm.connected_to(&modern);
    harness.events.latest("connected to the modern one", network, |state| ap(state, "Modern").connected);

    // An open network needs no passphrase and gets no security settings.
    services.send(Command::WifiConnect { ssid: "Cafe".into(), passphrase: None });
    let call = nm.peer.wait_call_n("AddAndActivateConnection", 3);
    let (settings, _, access_point): (Settings, OwnedObjectPath, OwnedObjectPath) = call.args();
    assert_eq!(access_point.as_str(), cafe);
    assert!(!settings.contains_key("802-11-wireless-security"));
}

#[test]
fn a_failed_attempt_is_reported_once_and_leaves_no_profile() {
    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    nm.add_access_point(1, b"Home", 80, WPA2);
    nm.add_access_point(2, b"Office", 50, ENTERPRISE);
    let services = harness.start(status_config());
    harness.events.latest("the networks", network, |state| state.access_points.len() == 2);

    // A secured network without a passphrase: refused before anything is
    // sent to NetworkManager.
    services.send(Command::WifiConnect { ssid: "Home".into(), passphrase: None });
    let state = harness.events.latest("the missing passphrase", network, |state| state.error.is_some());
    assert!(state.error.unwrap().contains("passphrase"));
    // An enterprise network cannot be joined with a passphrase alone.
    services.send(Command::WifiConnect { ssid: "Office".into(), passphrase: Some(Secret::new("pw".into())) });
    harness.events.wait("the enterprise refusal", |events| {
        events.iter().filter_map(network).filter_map(|state| state.error.clone()).find(|error| error.contains("sign-in"))
    });
    // A network that is not there.
    services.send(Command::WifiConnect { ssid: "Ghost".into(), passphrase: None });
    harness.events.wait("the out-of-range refusal", |events| {
        events.iter().filter_map(network).filter_map(|state| state.error.clone()).find(|error| error.contains("range"))
    });
    assert!(nm.peer.calls("AddAndActivateConnection").is_empty());

    // The wrong passphrase: NetworkManager fails the activation.
    services.send(Command::WifiConnect { ssid: "Home".into(), passphrase: Some(Secret::new("wrong one".into())) });
    nm.peer.wait_call("AddAndActivateConnection");
    harness.events.latest("connecting", network, |state| ap(state, "Home").connecting);
    assert_eq!(nm.profile_paths().len(), 1);
    nm.device_state(WIFI, 50, 0);
    nm.device_state(WIFI, 60, 0);
    nm.device_state(WIFI, 120, 7);
    nm.device_state(WIFI, 30, 0);
    harness.events.wait("the failure", |events| {
        events.iter().filter_map(network).filter_map(|state| state.error.clone()).find(|error| error.contains("missing or wrong"))
    });
    // The profile made for the attempt is gone again, the network is not
    // "known", and the error was sent exactly once.
    nm.peer.wait_call("Delete");
    let state = harness.events.latest("settled", network, |state| state.error.is_none() && !ap(state, "Home").connecting && !ap(state, "Home").known);
    assert!(nm.profile_paths().is_empty());
    assert_eq!(state.wifi_connected, None);
    let reported = harness.events.count(|event| network(event).is_some_and(|state| state.error.as_deref().is_some_and(|error| error.contains("missing or wrong"))));
    assert_eq!(reported, 1);
    // Refresh does not bring it back.
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Network));
    assert_eq!(harness.events.after(before, "the state again", |event| network(event).cloned()).error, None);

    // NetworkManager refusing the request itself is reported too.
    nm.peer.on(NM, "AddAndActivateConnection", |_, call| {
        call.error("org.freedesktop.NetworkManager.Settings.Connection.InvalidProperty", "802-11-wireless-security.psk: property is invalid")
    });
    services.send(Command::WifiConnect { ssid: "Home".into(), passphrase: Some(Secret::new("short".into())) });
    harness.events.wait("the refusal", |events| {
        events.iter().filter_map(network).filter_map(|state| state.error.clone()).find(|error| error.contains("not valid"))
    });
}

#[test]
fn a_new_passphrase_for_a_known_network_updates_its_profile() {
    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    let home = nm.add_access_point(1, b"Home", 80, WPA2);
    let profile = nm.add_profile(b"Home");
    let services = harness.start(status_config());
    harness.events.latest("the known network", network, |state| state.access_points.len() == 1 && state.access_points[0].known);

    services.send(Command::WifiConnect { ssid: "Home".into(), passphrase: Some(Secret::new("the new one".into())) });
    let update = nm.peer.wait_call("Update");
    assert_eq!(update.path, profile);
    let call = nm.peer.wait_call("ActivateConnection");
    let (connection, _, access_point): (OwnedObjectPath, OwnedObjectPath, OwnedObjectPath) = call.args();
    assert_eq!((connection.as_str(), access_point.as_str()), (profile.as_str(), home.as_str()));
    let settings = nm.profile(&profile).unwrap();
    assert_eq!(text(&settings, "802-11-wireless-security", "psk").as_deref(), Some("the new one"));
    assert_eq!(text(&settings, "802-11-wireless-security", "key-mgmt").as_deref(), Some("wpa-psk"));
    // What the profile had before is kept.
    assert_eq!(text(&settings, "connection", "id").as_deref(), Some("saved"));
    assert_eq!(nm.profile_paths().len(), 1);
    // A failure of a profile that existed before does not delete it.
    nm.device_state(WIFI, 120, 8);
    harness.events.wait("the failure", |events| events.iter().filter_map(network).find(|state| state.error.is_some()).cloned());
    harness.events.latest("still known", network, |state| state.error.is_none() && ap(state, "Home").known);
    assert_eq!(nm.profile_paths().len(), 1);
}

#[test]
fn airplane_mode_switches_every_radio() {
    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    // A modem, so that mobile broadband counts as a radio.
    let modem = "/org/freedesktop/NetworkManager/Devices/3";
    nm.peer.add(modem, NM_DEVICE, props([("DeviceType", owned(8u32)), ("State", owned(30u32))]));
    nm.peer.set(NM_PATH, NM, "Devices", paths_value(&[ETHERNET, WIFI, modem]));
    let bluez = bluez(&harness.system);
    let services = harness.start(status_config());
    harness.events.latest("radios on", network, |state| state.available && state.wifi_enabled && !state.airplane);

    services.send(Command::AirplaneMode(true));
    let state = harness.events.latest("airplane mode", network, |state| state.airplane);
    assert!(!state.wifi_enabled);
    assert!(!bool::try_from(nm.peer.get(NM_PATH, NM, "WwanEnabled").unwrap()).unwrap());
    assert!(!bool::try_from(bluez.get(HCI0, BLUEZ_ADAPTER, "Powered").unwrap()).unwrap());

    // One radio switched on by hand ends airplane mode.
    bluez.set(HCI0, BLUEZ_ADAPTER, "Powered", owned(true));
    harness.events.latest("not airplane mode with Bluetooth on", network, |state| !state.airplane);
    bluez.set(HCI0, BLUEZ_ADAPTER, "Powered", owned(false));
    harness.events.latest("airplane mode again", network, |state| state.airplane);

    services.send(Command::AirplaneMode(false));
    let state = harness.events.latest("radios back", network, |state| !state.airplane && state.wifi_enabled);
    assert!(state.available);
    assert!(bool::try_from(nm.peer.get(NM_PATH, NM, "WwanEnabled").unwrap()).unwrap());
    eventually("Bluetooth back on", || bool::try_from(bluez.get(HCI0, BLUEZ_ADAPTER, "Powered").unwrap()).unwrap());

    // Wi-Fi off alone, with Bluetooth on, is not airplane mode.
    services.send(Command::WifiSetEnabled(false));
    let state = harness.events.latest("wifi off", network, |state| !state.wifi_enabled);
    assert!(!state.airplane);
}

#[test]
fn a_networkmanager_that_lies_or_hangs_does_not_break_the_service() {
    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    use zbus::zvariant::Value;
    // An access point with every property of the wrong type, one with an
    // SSID that is too long, one with control characters, one that is
    // listed but does not exist.
    nm.peer.add(
        &format!("{NM_PATH}/AccessPoint/1"),
        NM_AP,
        props([("Ssid", owned("a string")), ("Strength", owned("strong")), ("Flags", owned(true)), ("WpaFlags", owned(-1i32)), ("RsnFlags", owned(1.5f64))]),
    );
    nm.peer.add(&format!("{NM_PATH}/AccessPoint/2"), NM_AP, props([("Ssid", owned(Value::from(vec![b'x'; 5000]))), ("Strength", owned(50u8))]));
    nm.peer.add(
        &format!("{NM_PATH}/AccessPoint/3"),
        NM_AP,
        props([("Ssid", owned(Value::from(b"Evil\x1b[2J\nNet".to_vec()))), ("Strength", owned(250u8)), ("Flags", owned(0u32))]),
    );
    let listed: Vec<String> = (1..=4).map(|n| format!("{NM_PATH}/AccessPoint/{n}")).collect();
    let listed: Vec<&str> = listed.iter().map(String::as_str).collect();
    nm.peer.set_quietly(WIFI, NM_WIRELESS, "AccessPoints", paths_value(&listed));
    nm.peer.set_quietly(WIFI, NM_WIRELESS, "LastScan", owned("yesterday"));
    nm.peer.set_quietly(NM_PATH, NM, "Connectivity", owned("full"));
    nm.peer.set_quietly(NM_PATH, NM, "ActiveConnections", owned(7u32));
    // Profiles: one whose settings are nonsense.
    nm.peer.on(NM_CONNECTION, "GetSettings", |_, call| call.reply(&("settings", 5u32)));
    nm.add_profile(b"Home");

    let services = harness.start(status_config());
    let state = harness.events.latest("a state", network, |state| state.available);
    assert_eq!(state.access_points, vec![AccessPoint { ssid: "Evil[2J Net".into(), strength: 100, secured: false, known: false, connected: false, connecting: false }]);
    assert_eq!(state.connectivity, Connectivity::None);

    // NetworkManager stops answering one call: the state shown stays, and
    // when it answers again the service catches up.
    nm.peer.on("", "GetAll", |_, _| Reply::Silence);
    nm.peer.signal(NM_PATH, PROPERTIES, "PropertiesChanged", &(NM, std::collections::HashMap::<&str, Value<'_>>::new(), Vec::<&str>::new()));
    nm.peer.wait_call_where("GetAll", |call| call.path == NM_PATH);
    services.send(Command::WifiSetEnabled(false));
    nm.peer.wait_call("Set");
    // Dropping while a call is unanswered is prompt.
    let started = std::time::Instant::now();
    drop(services);
    assert!(started.elapsed() < std::time::Duration::from_secs(3), "dropping took {:?}", started.elapsed());
}
