//! The Bluetooth service and its pairing agent against a stand-in BlueZ.

use boswas_services::{BluetoothDevice, BluetoothState, Command, Config, Event, Service};
use zbus::zvariant::{OwnedObjectPath, OwnedValue};

use crate::{mocks::*, support::*};

const HEADSET: &str = "AA:BB:CC:00:00:01";
const KEYBOARD: &str = "AA:BB:CC:00:00:02";
const STRANGER: &str = "AA:BB:CC:00:00:99";

fn bluetooth(event: &Event) -> Option<&BluetoothState> {
    match event {
        Event::Bluetooth(state) => Some(state),
        _ => None,
    }
}

fn device<'a>(state: &'a BluetoothState, address: &str) -> &'a BluetoothDevice {
    state.devices.iter().find(|device| device.address == address).unwrap_or_else(|| panic!("no device {address} in {state:#?}"))
}

/// The agent our service registered: (its bus name, its object path).
fn agent(bluez: &Peer) -> (String, String) {
    let call = bluez.wait_call("RegisterAgent");
    let (object, capability): (OwnedObjectPath, String) = call.args();
    // No display and no keyboard: the agent cannot show or take a code.
    assert_eq!(capability, "NoInputNoOutput");
    bluez.wait_call("RequestDefaultAgent");
    (call.sender, object.to_string())
}

/// Calls a method of the agent as `caller`; the error name if it refuses.
fn ask<B: serde::Serialize + zbus::zvariant::DynamicType>(caller: &Peer, agent: &(String, String), method: &str, body: &B) -> Result<(), String> {
    match caller.conn.call_method(Some(agent.0.as_str()), agent.1.as_str(), Some(BLUEZ_AGENT), method, body) {
        Ok(_) => Ok(()),
        Err(zbus::Error::MethodError(name, _, _)) => Err(name.to_string()),
        Err(other) => Err(other.to_string()),
    }
}

#[test]
fn without_bluez_bluetooth_is_unavailable() {
    let harness = Harness::new();
    let services = harness.start(status_config());
    assert_eq!(harness.events.latest("the first state", bluetooth, |_| true), BluetoothState::default());
    services.send(Command::BluetoothSetEnabled(true));
    services.send(Command::BluetoothConnect { address: HEADSET.into() });
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Bluetooth));
    assert!(!harness.events.after(before, "the state again", |event| bluetooth(event).cloned()).available);
}

#[test]
fn adapter_and_devices_are_read_and_followed() {
    let harness = Harness::new();
    let bluez = bluez(&harness.system);
    let headset = bluez_device(&bluez, HEADSET, Some("Headset"), true);
    bluez.set_quietly(&headset, BLUEZ_DEVICE, "Icon", owned("audio-headset"));
    bluez.set_quietly(&headset, BLUEZ_DEVICE, "Alias", owned("My headset"));
    bluez.set_quietly(&headset, BLUEZ_DEVICE, "Connected", owned(true));
    bluez.add(&headset, BLUEZ_BATTERY, props([("Percentage", owned(64u8))]));
    bluez_device(&bluez, KEYBOARD, Some("apple keyboard"), false);
    bluez_device(&bluez, "AA:BB:CC:00:00:03", Some("Zeppelin"), true);
    // Seen in passing, without a name: not for the list.
    bluez_device(&bluez, "AA:BB:CC:00:00:04", None, false);
    let services = harness.start(status_config());

    let state = harness.events.latest("the devices", bluetooth, |state| state.devices.len() == 3);
    assert!(state.available && state.enabled && !state.discovering);
    // Paired first, then discovered, each by name.
    let names: Vec<&str> = state.devices.iter().map(|device| device.name.as_str()).collect();
    assert_eq!(names, ["My headset", "Zeppelin", "apple keyboard"]);
    assert_eq!(
        state.devices[0],
        BluetoothDevice {
            address: HEADSET.into(),
            name: "My headset".into(),
            icon: "audio-headset".into(),
            paired: true,
            connected: true,
            connecting: false,
            battery: Some(64),
        }
    );

    bluez.set(&headset, BLUEZ_BATTERY, "Percentage", owned(63u8));
    harness.events.latest("the battery level", bluetooth, |state| device(state, HEADSET).battery == Some(63));

    services.send(Command::BluetoothSetEnabled(false));
    let set = bluez.wait_call("Set");
    assert_eq!(set.path, HCI0);
    let (interface, name, value): (String, String, OwnedValue) = set.args();
    assert_eq!((interface.as_str(), name.as_str(), bool::try_from(value).unwrap()), (BLUEZ_ADAPTER, "Powered", false));
    harness.events.latest("Bluetooth off", bluetooth, |state| state.available && !state.enabled);
    services.send(Command::BluetoothSetEnabled(true));
    harness.events.latest("Bluetooth on", bluetooth, |state| state.enabled);

    // BlueZ stops and starts.
    bluez.release(BLUEZ);
    harness.events.latest("unavailable", bluetooth, |state| !state.available && state.devices.is_empty());
    bluez.claim(BLUEZ);
    harness.events.latest("available again", bluetooth, |state| state.available && state.devices.len() == 3);
    // The agent is registered with the new daemon too.
    bluez.wait_call_n("RegisterAgent", 2);

    // The adapter is unplugged.
    bluez.remove(&headset);
    bluez.remove(HCI0);
    harness.events.latest("no adapter", bluetooth, |state| !state.available);
}

#[test]
fn discovery_finds_devices() {
    let harness = Harness::new();
    let bluez = bluez(&harness.system);
    let services = harness.start(status_config());
    harness.events.latest("the adapter", bluetooth, |state| state.available);

    services.send(Command::BluetoothDiscover(true));
    assert_eq!(bluez.wait_call("StartDiscovery").path, HCI0);
    harness.events.latest("discovering", bluetooth, |state| state.discovering);
    bluez_device(&bluez, KEYBOARD, Some("Keyboard"), false);
    let state = harness.events.latest("the found device", bluetooth, |state| state.devices.len() == 1);
    assert_eq!(state.devices[0].name, "Keyboard");
    assert!(!state.devices[0].paired);

    services.send(Command::BluetoothDiscover(false));
    assert_eq!(bluez.wait_call("StopDiscovery").path, HCI0);
    harness.events.latest("not discovering", bluetooth, |state| !state.discovering);
    // Stopping what was not started asks BlueZ nothing.
    services.send(Command::BluetoothDiscover(false));
    services.send(Command::Refresh(Service::Bluetooth));
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Bluetooth));
    harness.events.after(before, "the state again", |event| bluetooth(event).cloned());
    assert_eq!(bluez.calls("StopDiscovery").len(), 1);
}

#[test]
fn connecting_pairs_trusts_and_connects() {
    let harness = Harness::new();
    let bluez = bluez(&harness.system);
    let keyboard = bluez_device(&bluez, KEYBOARD, Some("Keyboard"), false);
    let headset = bluez_device(&bluez, HEADSET, Some("Headset"), true);
    // Pairing takes a while: the test answers it.
    bluez.on(BLUEZ_DEVICE, "Pair", |_, _| Reply::Silence);
    let services = harness.start(status_config());
    harness.events.latest("the devices", bluetooth, |state| state.devices.len() == 2);

    services.send(Command::BluetoothConnect { address: KEYBOARD.into() });
    let pair = bluez.wait_call("Pair");
    assert_eq!(pair.path, keyboard);
    let state = harness.events.latest("connecting", bluetooth, |state| device(state, KEYBOARD).connecting);
    assert!(!device(&state, KEYBOARD).connected && !device(&state, HEADSET).connecting);
    // The service is not stuck while pairing waits.
    bluez.set(&headset, BLUEZ_DEVICE, "Alias", owned("Renamed"));
    harness.events.latest("a change during pairing", bluetooth, |state| device(state, HEADSET).name == "Renamed");
    // A second request for the same device does not start a second pairing.
    services.send(Command::BluetoothConnect { address: KEYBOARD.into() });

    bluez.set(&keyboard, BLUEZ_DEVICE, "Paired", owned(true));
    bluez.answer(&pair, &());
    let connect = bluez.wait_call("Connect");
    assert_eq!(connect.path, keyboard);
    let state = harness.events.latest("paired and connected", bluetooth, |state| {
        let device = device(state, KEYBOARD);
        device.paired && device.connected && !device.connecting
    });
    assert_eq!(state.devices.len(), 2);
    // Between pairing and connecting the device was marked as trusted.
    let order: Vec<String> = bluez.all_calls().into_iter().filter(|call| call.path == keyboard).map(|call| call.member).collect();
    assert_eq!(order, ["Pair", "Set", "Connect"]);
    assert!(bool::try_from(bluez.get(&keyboard, BLUEZ_DEVICE, "Trusted").unwrap()).unwrap());
    assert_eq!(bluez.calls("Pair").len(), 1);

    // A paired device is only connected.
    services.send(Command::BluetoothConnect { address: HEADSET.into() });
    harness.events.latest("the headset connected", bluetooth, |state| device(state, HEADSET).connected);
    assert_eq!(bluez.calls("Pair").len(), 1);

    services.send(Command::BluetoothDisconnect { address: HEADSET.into() });
    harness.events.latest("the headset disconnected", bluetooth, |state| !device(state, HEADSET).connected);
    assert_eq!(bluez.wait_call("Disconnect").path, headset);

    services.send(Command::BluetoothForget { address: KEYBOARD.into() });
    let remove = bluez.wait_call("RemoveDevice");
    assert_eq!(remove.path, HCI0);
    assert_eq!(remove.args::<(OwnedObjectPath,)>().0.as_str(), keyboard);
    harness.events.latest("the keyboard forgotten", bluetooth, |state| state.devices.len() == 1);

    // Addresses that are no device of BlueZ's are not acted on.
    services.send(Command::BluetoothConnect { address: STRANGER.into() });
    services.send(Command::BluetoothDisconnect { address: "../../org/bluez".into() });
    services.send(Command::BluetoothForget { address: String::new() });
    services.send(Command::BluetoothDisconnect { address: HEADSET.into() });
    bluez.wait_call_n("Disconnect", 2);
    assert_eq!(bluez.calls("RemoveDevice").len(), 1);
}

#[test]
fn a_failed_connection_stops_showing_as_connecting() {
    let harness = Harness::new();
    let bluez = bluez(&harness.system);
    bluez_device(&bluez, HEADSET, Some("Headset"), true);
    bluez.on(BLUEZ_DEVICE, "Connect", |_, _| Reply::Silence);
    let services = harness.start(status_config());
    harness.events.latest("the device", bluetooth, |state| state.devices.len() == 1);
    services.send(Command::BluetoothConnect { address: HEADSET.into() });
    let connect = bluez.wait_call("Connect");
    harness.events.latest("connecting", bluetooth, |state| device(state, HEADSET).connecting);
    bluez.fail(&connect, "org.bluez.Error.Failed", "br-connection-page-timeout");
    harness.events.latest("not connecting any more", bluetooth, |state| !device(state, HEADSET).connecting && !device(state, HEADSET).connected);

    // Giving up an attempt on its way: pairing is cancelled.
    let keyboard = bluez_device(&bluez, KEYBOARD, Some("Keyboard"), false);
    bluez.on(BLUEZ_DEVICE, "Pair", |_, _| Reply::Silence);
    harness.events.latest("two devices", bluetooth, |state| state.devices.len() == 2);
    services.send(Command::BluetoothConnect { address: KEYBOARD.into() });
    bluez.wait_call("Pair");
    harness.events.latest("connecting", bluetooth, |state| device(state, KEYBOARD).connecting);
    services.send(Command::BluetoothDisconnect { address: KEYBOARD.into() });
    assert_eq!(bluez.wait_call("CancelPairing").path, keyboard);
    harness.events.latest("given up", bluetooth, |state| !device(state, KEYBOARD).connecting);
}

#[test]
fn the_agent_refuses_pairing_nobody_asked_for() {
    let harness = Harness::new();
    let bluez = bluez(&harness.system);
    let keyboard = bluez_device(&bluez, KEYBOARD, Some("Keyboard"), false);
    let stranger = bluez_device(&bluez, STRANGER, Some("Free headphones"), false);
    bluez.on(BLUEZ_DEVICE, "Pair", |_, _| Reply::Silence);
    let services = harness.start(status_config());
    harness.events.latest("the devices", bluetooth, |state| state.devices.len() == 2);
    let agent = agent(&bluez);
    let rejected = Err("org.bluez.Error.Rejected".to_owned());

    // A device asks to pair out of the blue: every way of asking is refused.
    assert_eq!(ask(&bluez, &agent, "RequestConfirmation", &(path(&stranger), 123456u32)), rejected);
    assert_eq!(ask(&bluez, &agent, "RequestAuthorization", &(path(&stranger),)), rejected);
    assert_eq!(ask(&bluez, &agent, "AuthorizeService", &(path(&stranger), "0000110b-0000-1000-8000-00805f9b34fb")), rejected);
    assert_eq!(ask(&bluez, &agent, "RequestPinCode", &(path(&stranger),)), rejected);
    assert_eq!(ask(&bluez, &agent, "RequestPasskey", &(path(&stranger),)), rejected);
    assert_eq!(ask(&bluez, &agent, "DisplayPinCode", &(path(&stranger), "0000")), rejected);
    assert_eq!(ask(&bluez, &agent, "DisplayPasskey", &(path(&stranger), 1u32, 0u16)), rejected);
    // So is the keyboard, which nobody asked for yet.
    assert_eq!(ask(&bluez, &agent, "RequestConfirmation", &(path(&keyboard), 123456u32)), rejected);

    // The person asks to connect to the keyboard.
    services.send(Command::BluetoothConnect { address: KEYBOARD.into() });
    let pair = bluez.wait_call("Pair");
    assert_eq!(ask(&bluez, &agent, "RequestConfirmation", &(path(&keyboard), 123456u32)), Ok(()));
    assert_eq!(ask(&bluez, &agent, "RequestAuthorization", &(path(&keyboard),)), Ok(()));
    assert_eq!(ask(&bluez, &agent, "AuthorizeService", &(path(&keyboard), "00001124-0000-1000-8000-00805f9b34fb")), Ok(()));
    // That does not open the door for anybody else,
    assert_eq!(ask(&bluez, &agent, "RequestConfirmation", &(path(&stranger), 123456u32)), rejected);
    assert_eq!(ask(&bluez, &agent, "RequestAuthorization", &(path("/org/bluez/hci0/dev_AA_BB_CC_00_00_02/x"),)), rejected);
    // nor for somebody who is not BlueZ asking about the keyboard,
    let impostor = Peer::start(&harness.system, &[]);
    assert_eq!(ask(&impostor, &agent, "RequestConfirmation", &(path(&keyboard), 123456u32)), rejected);
    // nor does it make a code typable.
    assert_eq!(ask(&bluez, &agent, "RequestPinCode", &(path(&keyboard),)), rejected);
    // Release and Cancel are taken without complaint.
    assert_eq!(ask(&bluez, &agent, "Cancel", &()), Ok(()));
    assert_eq!(ask(&bluez, &agent, "Release", &()), Ok(()));
    // Nonsense arguments are an error, not a crash.
    assert!(ask(&bluez, &agent, "RequestConfirmation", &("not a path", "not a number")).is_err());

    bluez.answer(&pair, &());
    bluez.wait_call("Connect");

    // Forgetting the device closes the door again at once.
    services.send(Command::BluetoothForget { address: KEYBOARD.into() });
    bluez.wait_call("RemoveDevice");
    assert_eq!(ask(&bluez, &agent, "RequestConfirmation", &(path(&keyboard), 123456u32)), rejected);

    // Dropping the services takes the agent back.
    drop(services);
    let unregister = bluez.wait_call("UnregisterAgent");
    assert_eq!(unregister.args::<(OwnedObjectPath,)>().0.as_str(), agent.1);
}

#[test]
fn the_lock_screen_registers_no_agent_and_changes_nothing() {
    let harness = Harness::new();
    let bluez = bluez(&harness.system);
    bluez_device(&bluez, HEADSET, Some("Headset"), true);
    let services = harness.start(Config::status_only());
    harness.events.latest("the device", bluetooth, |state| state.devices.len() == 1);
    services.send(Command::BluetoothSetEnabled(false));
    services.send(Command::BluetoothDiscover(true));
    services.send(Command::BluetoothConnect { address: HEADSET.into() });
    services.send(Command::BluetoothDisconnect { address: HEADSET.into() });
    services.send(Command::BluetoothForget { address: HEADSET.into() });
    // Refresh is obeyed; by the time it is answered the commands before it
    // have been dealt with.
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Bluetooth));
    harness.events.after(before, "the state again", |event| bluetooth(event).cloned());
    let members: Vec<String> = bluez.all_calls().into_iter().map(|call| call.member).collect();
    assert!(members.iter().all(|member| member == "GetManagedObjects"), "{members:?}");
}

#[test]
fn a_bluez_that_lies_does_not_break_the_service() {
    let harness = Harness::new();
    let bluez = bluez(&harness.system);
    bluez.add(
        "/org/bluez/hci0/dev_liar",
        BLUEZ_DEVICE,
        props([("Address", owned(17u32)), ("Name", owned(true)), ("Alias", owned(vec![1u8])), ("Paired", owned("yes")), ("Connected", owned(2u8))]),
    );
    let long = bluez_device(&bluez, KEYBOARD, Some(&format!("K\u{1b}[31m{}", "e".repeat(10_000))), true);
    bluez.set_quietly(&long, BLUEZ_DEVICE, "Icon", owned("../../../etc/passwd"));
    bluez.add(&long, BLUEZ_BATTERY, props([("Percentage", owned(250u8))]));
    bluez.set_quietly(HCI0, BLUEZ_ADAPTER, "Powered", owned("on"));
    for index in 0..400 {
        bluez_device(&bluez, &format!("AA:BB:CC:00:{:02X}:{:02X}", index / 256 + 1, index % 256), Some(&format!("crowd {index:03}")), false);
    }
    let _services = harness.start(status_config());
    let state = harness.events.latest("the devices", bluetooth, |state| state.devices.len() >= 200);
    assert_eq!(state.devices.len(), 256, "the list is bounded");
    assert!(state.available && !state.enabled);
    let keyboard = device(&state, KEYBOARD);
    assert!(keyboard.name.starts_with("K[31m"));
    assert_eq!(keyboard.name.chars().count(), 64);
    assert_eq!(keyboard.icon, "");
    assert_eq!(keyboard.battery, Some(100));
    assert!(state.devices.iter().all(|device| device.address.len() == 17));
}
