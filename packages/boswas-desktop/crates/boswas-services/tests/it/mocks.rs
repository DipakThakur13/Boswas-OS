//! Stand-ins for the system's daemons, built on [`Peer`].

#![allow(dead_code)]

use crate::support::*;

pub const LOGIN1: &str = "org.freedesktop.login1";
pub const LOGIN1_PATH: &str = "/org/freedesktop/login1";
pub const LOGIN1_MANAGER: &str = "org.freedesktop.login1.Manager";
pub const LOGIN1_SESSION: &str = "org.freedesktop.login1.Session";
/// The session the harness's environment names (`XDG_SESSION_ID=7`).
pub const SESSION_PATH: &str = "/org/freedesktop/login1/session/_37";

/// logind with one session, "7". The answers of the `Can...` methods are
/// the properties of the object `/test` (interface `test`).
pub fn logind(bus: &TestBus) -> Peer {
    let peer = Peer::start(bus, &[]);
    peer.add(SESSION_PATH, LOGIN1_SESSION, props([("Id", owned("7")), ("Active", owned(true))]));
    peer.add(
        "/test",
        "test",
        props([
            ("CanSuspend", owned("yes")),
            ("CanHibernate", owned("no")),
            ("CanReboot", owned("challenge")),
            ("CanPowerOff", owned("yes")),
        ]),
    );
    peer.on(LOGIN1_MANAGER, "GetSession", |_, call| {
        let (id,): (String,) = call.args();
        if id == "7" {
            call.reply(&(path(SESSION_PATH),))
        } else {
            call.error("org.freedesktop.login1.NoSuchSession", "no such session")
        }
    });
    peer.on(LOGIN1_MANAGER, "GetSessionByPID", |_, call| call.reply(&(path(SESSION_PATH),)));
    for method in ["CanSuspend", "CanHibernate", "CanReboot", "CanPowerOff"] {
        peer.on(LOGIN1_MANAGER, method, move |peer, call| {
            let answer = peer.get("/test", "test", method).unwrap();
            call.reply(&(<&str>::try_from(&*answer).unwrap(),))
        });
    }
    for method in ["Suspend", "Hibernate", "Reboot", "PowerOff", "TerminateSession"] {
        peer.accept(LOGIN1_MANAGER, method);
    }
    peer.claim(LOGIN1);
    peer
}

pub const UPOWER: &str = "org.freedesktop.UPower";
pub const UPOWER_PATH: &str = "/org/freedesktop/UPower";
pub const UPOWER_DEVICE: &str = "org.freedesktop.UPower.Device";
pub const DISPLAY_DEVICE: &str = "/org/freedesktop/UPower/devices/DisplayDevice";

/// UPower with a battery at 80 %, discharging, two hours left.
pub fn upower(bus: &TestBus) -> Peer {
    let peer = Peer::start(bus, &[]);
    peer.add(UPOWER_PATH, UPOWER, props([("OnBattery", owned(true))]));
    peer.add(
        DISPLAY_DEVICE,
        UPOWER_DEVICE,
        props([
            ("Type", owned(2u32)),
            ("IsPresent", owned(true)),
            ("Percentage", owned(80.0f64)),
            ("State", owned(2u32)),
            ("TimeToEmpty", owned(7200i64)),
            ("TimeToFull", owned(0i64)),
        ]),
    );
    peer.claim(UPOWER);
    peer
}

pub const PROFILES: &str = "org.freedesktop.UPower.PowerProfiles";
pub const PROFILES_PATH: &str = "/org/freedesktop/UPower/PowerProfiles";
pub const LEGACY_PROFILES: &str = "net.hadess.PowerProfiles";
pub const LEGACY_PROFILES_PATH: &str = "/net/hadess/PowerProfiles";

/// power-profiles-daemon under `name` at `path`, with the three profiles.
pub fn power_profiles(bus: &TestBus, name: &str, object: &str) -> Peer {
    use std::collections::HashMap;
    use zbus::zvariant::Value;
    let peer = Peer::start(bus, &[]);
    let profiles: Vec<HashMap<&str, Value<'_>>> = ["power-saver", "balanced", "performance"]
        .into_iter()
        .map(|profile| HashMap::from([("Profile", Value::from(profile)), ("Driver", Value::from("test"))]))
        .collect();
    peer.add(object, name, props([("ActiveProfile", owned("balanced")), ("Profiles", owned(Value::from(profiles)))]));
    peer.claim(name);
    peer
}

// --- NetworkManager -----------------------------------------------------------------

pub const NM: &str = "org.freedesktop.NetworkManager";
pub const NM_PATH: &str = "/org/freedesktop/NetworkManager";
pub const NM_DEVICE: &str = "org.freedesktop.NetworkManager.Device";
pub const NM_WIRELESS: &str = "org.freedesktop.NetworkManager.Device.Wireless";
pub const NM_AP: &str = "org.freedesktop.NetworkManager.AccessPoint";
pub const NM_ACTIVE: &str = "org.freedesktop.NetworkManager.Connection.Active";
pub const NM_SETTINGS: &str = "org.freedesktop.NetworkManager.Settings";
pub const NM_SETTINGS_PATH: &str = "/org/freedesktop/NetworkManager/Settings";
pub const NM_CONNECTION: &str = "org.freedesktop.NetworkManager.Settings.Connection";
pub const ETHERNET: &str = "/org/freedesktop/NetworkManager/Devices/1";
pub const WIFI: &str = "/org/freedesktop/NetworkManager/Devices/2";

pub type Settings = std::collections::HashMap<String, std::collections::HashMap<String, zbus::zvariant::OwnedValue>>;

/// Security flags of an access point: (Flags, WpaFlags, RsnFlags).
pub const OPEN: (u32, u32, u32) = (0, 0, 0);
pub const WPA2: (u32, u32, u32) = (1, 0, 0x188);
pub const WPA3: (u32, u32, u32) = (1, 0, 0x488);
pub const ENTERPRISE: (u32, u32, u32) = (1, 0, 0x288);

/// NetworkManager with a wired device (disconnected) and a Wi-Fi device
/// (disconnected, no access points), no saved profiles.
#[derive(Clone)]
pub struct Nm {
    pub peer: Peer,
    profiles: std::sync::Arc<std::sync::Mutex<std::collections::BTreeMap<String, Settings>>>,
    counter: std::sync::Arc<std::sync::atomic::AtomicU32>,
}

impl Nm {
    pub fn start(bus: &TestBus) -> Nm {
        let peer = Peer::start(bus, &[]);
        let nm = Nm { peer: peer.clone(), profiles: Default::default(), counter: Default::default() };
        peer.add(
            NM_PATH,
            NM,
            props([
                ("Connectivity", owned(1u32)),
                ("State", owned(20u32)),
                ("WirelessEnabled", owned(true)),
                ("WwanEnabled", owned(true)),
                ("Devices", paths_value(&[ETHERNET, WIFI])),
                ("ActiveConnections", paths_value(&[])),
            ]),
        );
        peer.add(ETHERNET, NM_DEVICE, props([("DeviceType", owned(1u32)), ("State", owned(30u32)), ("Interface", owned("eth0"))]));
        peer.add(WIFI, NM_DEVICE, props([("DeviceType", owned(2u32)), ("State", owned(30u32)), ("Interface", owned("wlan0"))]));
        peer.add(
            WIFI,
            NM_WIRELESS,
            props([("AccessPoints", paths_value(&[])), ("ActiveAccessPoint", path_value("/")), ("LastScan", owned(1000i64))]),
        );
        peer.accept(NM_WIRELESS, "RequestScan");
        peer.accept(NM_DEVICE, "Disconnect");
        let this = nm.clone();
        peer.on(NM_SETTINGS, "ListConnections", move |_, call| {
            let list: Vec<zbus::zvariant::OwnedObjectPath> = this.profiles.lock().unwrap().keys().map(|profile| path(profile)).collect();
            call.reply(&(list,))
        });
        let this = nm.clone();
        peer.on(NM_CONNECTION, "GetSettings", move |_, call| match this.profiles.lock().unwrap().get(&call.path) {
            Some(settings) => call.reply(&(settings,)),
            None => call.error("org.freedesktop.DBus.Error.UnknownObject", "no such connection"),
        });
        let this = nm.clone();
        peer.on(NM_CONNECTION, "Delete", move |peer, call| {
            if this.profiles.lock().unwrap().remove(&call.path).is_none() {
                return call.error("org.freedesktop.DBus.Error.UnknownObject", "no such connection");
            }
            peer.signal(&call.path, NM_CONNECTION, "Removed", &());
            peer.signal(NM_SETTINGS_PATH, NM_SETTINGS, "ConnectionRemoved", &(path(&call.path),));
            Reply::Done
        });
        let this = nm.clone();
        peer.on(NM_CONNECTION, "Update", move |peer, call| {
            let (settings,): (Settings,) = call.args();
            this.profiles.lock().unwrap().insert(call.path.clone(), settings);
            peer.signal(&call.path, NM_CONNECTION, "Updated", &());
            Reply::Done
        });
        peer.on(NM, "ActivateConnection", |_, call| call.reply(&(path("/org/freedesktop/NetworkManager/ActiveConnection/1"),)));
        let this = nm.clone();
        peer.on(NM, "AddAndActivateConnection", move |_, call| {
            let (settings, _device, _ap): (Settings, zbus::zvariant::OwnedObjectPath, zbus::zvariant::OwnedObjectPath) = call.args();
            let profile = this.store_profile(settings);
            call.reply(&(path(&profile), path("/org/freedesktop/NetworkManager/ActiveConnection/1")))
        });
        peer.claim(NM);
        nm
    }

    fn store_profile(&self, settings: Settings) -> String {
        let number = self.counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
        let profile = format!("{NM_SETTINGS_PATH}/{number}");
        self.profiles.lock().unwrap().insert(profile.clone(), settings);
        self.peer.signal(NM_SETTINGS_PATH, NM_SETTINGS, "NewConnection", &(path(&profile),));
        profile
    }

    /// A saved Wi-Fi profile for `ssid`.
    pub fn add_profile(&self, ssid: &[u8]) -> String {
        use zbus::zvariant::Value;
        let mut settings = Settings::new();
        settings.insert("connection".into(), props([("type", owned("802-11-wireless")), ("id", owned("saved"))]));
        settings.insert("802-11-wireless".into(), props([("ssid", owned(Value::from(ssid.to_vec())))]));
        self.store_profile(settings)
    }

    pub fn profile(&self, profile: &str) -> Option<Settings> {
        let profiles = self.profiles.lock().unwrap();
        let settings = profiles.get(profile)?;
        Some(settings.iter().map(|(section, values)| (section.clone(), values.iter().map(|(key, value)| (key.clone(), value.try_clone().unwrap())).collect())).collect())
    }

    pub fn profile_paths(&self) -> Vec<String> {
        self.profiles.lock().unwrap().keys().cloned().collect()
    }

    /// Adds access point `number` to the Wi-Fi device.
    pub fn add_access_point(&self, number: u32, ssid: &[u8], strength: u8, security: (u32, u32, u32)) -> String {
        use zbus::zvariant::Value;
        let object = format!("{NM_PATH}/AccessPoint/{number}");
        self.peer.add(
            &object,
            NM_AP,
            props([
                ("Ssid", owned(Value::from(ssid.to_vec()))),
                ("Strength", owned(strength)),
                ("Flags", owned(security.0)),
                ("WpaFlags", owned(security.1)),
                ("RsnFlags", owned(security.2)),
            ]),
        );
        let mut list = self.access_points();
        list.push(object.clone());
        self.set_access_points(&list);
        object
    }

    pub fn remove_access_point(&self, object: &str) {
        let list: Vec<String> = self.access_points().into_iter().filter(|path| path != object).collect();
        self.set_access_points(&list);
        self.peer.remove(object);
    }

    fn access_points(&self) -> Vec<String> {
        let value = self.peer.get(WIFI, NM_WIRELESS, "AccessPoints").unwrap();
        let list: Vec<zbus::zvariant::OwnedObjectPath> = value.try_into().unwrap();
        list.into_iter().map(|path| path.to_string()).collect()
    }

    fn set_access_points(&self, list: &[String]) {
        let list: Vec<&str> = list.iter().map(String::as_str).collect();
        self.peer.set(WIFI, NM_WIRELESS, "AccessPoints", paths_value(&list));
    }

    /// Moves a device to a state, with the signal NetworkManager sends.
    pub fn device_state(&self, device: &str, new: u32, reason: u32) {
        let old = self.peer.get(device, NM_DEVICE, "State").map(|value| u32::try_from(value).unwrap()).unwrap_or(0);
        self.peer.set(device, NM_DEVICE, "State", owned(new));
        self.peer.signal(device, NM_DEVICE, "StateChanged", &(new, old, reason));
    }

    /// The Wi-Fi device is connected to `access_point`.
    pub fn connected_to(&self, access_point: &str) {
        self.peer.set(WIFI, NM_WIRELESS, "ActiveAccessPoint", path_value(access_point));
        self.device_state(WIFI, 100, 0);
    }
}

// --- BlueZ --------------------------------------------------------------------------

pub const BLUEZ: &str = "org.bluez";
pub const BLUEZ_PATH: &str = "/org/bluez";
pub const BLUEZ_ADAPTER: &str = "org.bluez.Adapter1";
pub const BLUEZ_DEVICE: &str = "org.bluez.Device1";
pub const BLUEZ_BATTERY: &str = "org.bluez.Battery1";
pub const BLUEZ_AGENT_MANAGER: &str = "org.bluez.AgentManager1";
pub const BLUEZ_AGENT: &str = "org.bluez.Agent1";
pub const HCI0: &str = "/org/bluez/hci0";

/// BlueZ with one powered adapter and no devices. Discovery, connecting
/// and removing do what the real daemon does to its properties.
pub fn bluez(bus: &TestBus) -> Peer {
    let peer = Peer::start(bus, &[]);
    peer.manage_objects("/");
    peer.add(BLUEZ_PATH, BLUEZ_AGENT_MANAGER, props([]));
    peer.add(HCI0, BLUEZ_ADAPTER, props([("Powered", owned(true)), ("Discovering", owned(false)), ("Address", owned("00:00:00:00:00:01"))]));
    for method in ["RegisterAgent", "RequestDefaultAgent", "UnregisterAgent"] {
        peer.accept(BLUEZ_AGENT_MANAGER, method);
    }
    peer.on(BLUEZ_ADAPTER, "StartDiscovery", |peer, call| {
        peer.set(&call.path, BLUEZ_ADAPTER, "Discovering", owned(true));
        Reply::Done
    });
    peer.on(BLUEZ_ADAPTER, "StopDiscovery", |peer, call| {
        peer.set(&call.path, BLUEZ_ADAPTER, "Discovering", owned(false));
        Reply::Done
    });
    peer.on(BLUEZ_ADAPTER, "RemoveDevice", |peer, call| {
        let (device,): (zbus::zvariant::OwnedObjectPath,) = call.args();
        peer.remove(device.as_str());
        Reply::Done
    });
    peer.on(BLUEZ_DEVICE, "Pair", |peer, call| {
        peer.set(&call.path, BLUEZ_DEVICE, "Paired", owned(true));
        Reply::Done
    });
    peer.on(BLUEZ_DEVICE, "Connect", |peer, call| {
        peer.set(&call.path, BLUEZ_DEVICE, "Connected", owned(true));
        Reply::Done
    });
    peer.on(BLUEZ_DEVICE, "Disconnect", |peer, call| {
        peer.set(&call.path, BLUEZ_DEVICE, "Connected", owned(false));
        Reply::Done
    });
    peer.accept(BLUEZ_DEVICE, "CancelPairing");
    peer.claim(BLUEZ);
    peer
}

/// Adds a device to the adapter; returns its object path.
pub fn bluez_device(peer: &Peer, address: &str, name: Option<&str>, paired: bool) -> String {
    let object = format!("{HCI0}/dev_{}", address.replace(':', "_"));
    let mut device = props([
        ("Address", owned(address)),
        ("Alias", owned(name.map(str::to_owned).unwrap_or_else(|| address.replace(':', "-")))),
        ("Paired", owned(paired)),
        ("Trusted", owned(paired)),
        ("Connected", owned(false)),
        ("Adapter", path_value(HCI0)),
    ]);
    if let Some(name) = name {
        device.insert("Name".into(), owned(name));
    }
    peer.add(&object, BLUEZ_DEVICE, device);
    object
}

// --- polkit -------------------------------------------------------------------------

pub const POLKIT: &str = "org.freedesktop.PolicyKit1";
pub const POLKIT_AUTHORITY: &str = "org.freedesktop.PolicyKit1.Authority";
pub const POLKIT_AGENT: &str = "org.freedesktop.PolicyKit1.AuthenticationAgent";

/// The polkit authority: it takes agent registrations.
pub fn polkit(bus: &TestBus) -> Peer {
    let peer = Peer::start(bus, &[]);
    peer.accept(POLKIT_AUTHORITY, "RegisterAuthenticationAgent");
    peer.accept(POLKIT_AUTHORITY, "UnregisterAuthenticationAgent");
    peer.claim(POLKIT);
    peer
}
