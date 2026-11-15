//! The network: NetworkManager on the system bus.
//!
//! The state is read whole from NetworkManager whenever it signals a
//! change (coalesced), so what the shell shows never drifts from what
//! NetworkManager knows. Airplane mode also switches BlueZ's adapters.

use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use async_channel::{Receiver, Sender};
use async_executor::Task;
use zbus::{
    zvariant::{OwnedObjectPath, OwnedValue, Value},
    Connection,
};

use crate::{
    runtime::{
        call, call_raw, earliest, get_all, idle, name_owner, owner_rule, set_property, signal_rule, subscribe, wake, Backoff, Bus,
        BusKind, CallError, CallResult, Ctx, Debounce, Exec, Flag, IdleEnd, Latest, Service, TimedOut, Wake, CALL_TIMEOUT,
        PROPERTIES,
    },
    var::{as_bytes, clean, prop, prop_bool, prop_i64, prop_path, prop_paths, prop_str, prop_u32, Props},
    AccessPoint, Command, Connectivity, Event, NetworkState, Secret, Service as Which,
};

const NM: &str = "org.freedesktop.NetworkManager";
const NM_PATH: &str = "/org/freedesktop/NetworkManager";
const NM_DEVICE: &str = "org.freedesktop.NetworkManager.Device";
const NM_WIRELESS: &str = "org.freedesktop.NetworkManager.Device.Wireless";
const NM_AP: &str = "org.freedesktop.NetworkManager.AccessPoint";
const NM_ACTIVE: &str = "org.freedesktop.NetworkManager.Connection.Active";
const NM_SETTINGS_PATH: &str = "/org/freedesktop/NetworkManager/Settings";
const NM_SETTINGS: &str = "org.freedesktop.NetworkManager.Settings";
const NM_CONNECTION: &str = "org.freedesktop.NetworkManager.Settings.Connection";
const BLUEZ: &str = "org.bluez";
const BLUEZ_ADAPTER: &str = "org.bluez.Adapter1";
const OBJECT_MANAGER: &str = "org.freedesktop.DBus.ObjectManager";

// NetworkManager's numbers.
const DEVICE_ETHERNET: u32 = 1;
const DEVICE_WIFI: u32 = 2;
const DEVICE_MODEM: u32 = 8;
const STATE_DISCONNECTED: u32 = 30;
const STATE_PREPARE: u32 = 40;
const STATE_SECONDARIES: u32 = 90;
const STATE_ACTIVATED: u32 = 100;
const STATE_FAILED: u32 = 120;
const ACTIVE_ACTIVATED: u32 = 2;

// Bounds on what is read: a machine has a few devices, a busy place a few
// hundred access points.
const MAX_DEVICES: usize = 64;
const MAX_ACCESS_POINTS: usize = 512;
const MAX_ACTIVE: usize = 32;
const MAX_PROFILES: usize = 1024;

/// A scan that NetworkManager does not report as finished is forgotten
/// after this long.
const SCAN_TIMEOUT: Duration = Duration::from_secs(15);
/// So is a connection attempt (NetworkManager gives up earlier).
const ATTEMPT_TIMEOUT: Duration = Duration::from_secs(90);

pub(crate) struct Network {
    ctx: Ctx,
    ex: Exec,
    state: Latest<NetworkState>,
    /// Saved Wi-Fi profiles by SSID, read again when the settings change.
    profiles: HashMap<String, Vec<String>>,
    profiles_stale: bool,
    /// Whether there are Bluetooth adapters and whether one is powered.
    radios: Radios,
    radios_stale: bool,
    /// The networks of the last read, for `WifiConnect`.
    networks: HashMap<String, Net>,
    /// The Wi-Fi devices of the last read.
    wifi_devices: Vec<WifiDevice>,
    scan: Option<Scan>,
    attempt: Option<Attempt>,
    /// A failure to show once: it goes out with the next state and is
    /// then forgotten.
    error: Option<String>,
    /// What airplane mode switched off, to switch it on again.
    restore: Option<Restore>,
}

#[derive(Clone, Debug, Default)]
struct Radios {
    /// (adapter path, powered)
    adapters: Vec<(String, bool)>,
}

/// One network (SSID) as last seen: where to connect to it.
#[derive(Clone, Debug)]
struct Net {
    ssid: Vec<u8>,
    device: String,
    access_point: String,
    security: Security,
}

#[derive(Clone, Debug)]
struct WifiDevice {
    path: String,
    state: u32,
    last_scan: i64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Security {
    Open,
    /// Encrypted without a passphrase (enhanced open).
    Owe,
    Wep,
    Psk,
    Sae,
    Enterprise,
}

struct Scan {
    started: Instant,
    /// Each device's `LastScan` when the scan was asked for.
    before: HashMap<String, i64>,
}

struct Attempt {
    ssid: String,
    device: String,
    started: Instant,
    /// The profile this attempt created; deleted again if it fails, so a
    /// wrong passphrase does not leave a "known" network behind.
    created: Option<String>,
    /// The device has started activating since the attempt began. A
    /// "failed" or "activated" seen before that is the end of whatever the
    /// device did before, not of this attempt.
    activating: bool,
}

struct Restore {
    wwan: bool,
    adapters: Vec<String>,
}

enum Msg {
    Changed,
    Profiles,
    Radios,
    DeviceState { path: String, new: u32, reason: u32 },
    Lost,
}

impl Service for Network {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self {
            ctx,
            ex,
            state: Latest::default(),
            profiles: HashMap::new(),
            profiles_stale: true,
            radios: Radios::default(),
            radios_stale: true,
            networks: HashMap::new(),
            wifi_devices: Vec::new(),
            scan: None,
            attempt: None,
            error: None,
            restore: None,
        }
    }

    async fn run(&mut self, commands: Receiver<Command>) {
        let mut backoff = Backoff::new(Duration::from_secs(30));
        loop {
            match Bus::connect(BusKind::System, &self.ctx.env, &self.ex).await {
                Ok(bus) => {
                    backoff.reset();
                    match self.serve(&bus, &commands).await {
                        Ok(true) => return,
                        Ok(false) => {}
                        Err(error) => tracing::debug!(%error, "network: the system bus stopped answering"),
                    }
                }
                Err(error) => tracing::debug!(%error, "network: no system bus"),
            }
            self.forget();
            self.publish(NetworkState::default(), false);
            match idle(&commands, backoff.next(), |command| matches!(command, Command::Refresh(Which::Network))).await {
                IdleEnd::Closed => return,
                IdleEnd::Retry => self.publish(NetworkState::default(), true),
                IdleEnd::Elapsed => {}
            }
        }
    }
}

impl Network {
    fn publish(&mut self, state: NetworkState, force: bool) {
        if let Some(state) = self.state.update(state, force) {
            self.ctx.emit(Event::Network(state));
        }
    }

    /// NetworkManager is gone: nothing that was read is true any more.
    fn forget(&mut self) {
        self.profiles.clear();
        self.profiles_stale = true;
        self.radios_stale = true;
        self.networks.clear();
        self.wifi_devices.clear();
        self.scan = None;
        self.attempt = None;
    }

    async fn serve(&mut self, bus: &Bus, commands: &Receiver<Command>) -> CallResult<bool> {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        let conn = &bus.conn;
        let flag = Flag::default();
        let _subscriptions = self.subscribe(conn, &inbox, &flag).await?;
        let mut debounce = Debounce::default();
        debounce.trigger_now();
        let mut force = false;
        loop {
            let mut deadline = debounce.deadline();
            deadline = earliest(deadline, self.scan.as_ref().map(|scan| scan.started + SCAN_TIMEOUT));
            deadline = earliest(deadline, self.attempt.as_ref().map(|attempt| attempt.started + ATTEMPT_TIMEOUT));
            match wake(commands, &messages, deadline).await {
                Wake::Closed => return Ok(true),
                Wake::Message(Msg::Lost) => return Ok(false),
                Wake::Message(Msg::Changed) => debounce.trigger(),
                Wake::Message(Msg::Profiles) => {
                    self.profiles_stale = true;
                    debounce.trigger();
                }
                Wake::Message(Msg::Radios) => {
                    self.radios_stale = true;
                    debounce.trigger();
                }
                Wake::Message(Msg::DeviceState { path, new, reason }) => {
                    self.device_state(conn, &path, new, reason).await;
                    debounce.trigger();
                }
                Wake::Command(Command::Refresh(Which::Network)) => {
                    self.profiles_stale = true;
                    self.radios_stale = true;
                    force = true;
                    debounce.trigger_now();
                }
                Wake::Command(command) => {
                    // The lock screen and the greeter show the network
                    // and change nothing.
                    if !self.ctx.restricted {
                        self.command(conn, command).await;
                        debounce.trigger();
                    }
                }
                Wake::Timer => {
                    let now = Instant::now();
                    let mut changed = false;
                    if self.scan.as_ref().is_some_and(|scan| scan.started + SCAN_TIMEOUT <= now) {
                        self.scan = None;
                        changed = true;
                    }
                    if self.attempt.as_ref().is_some_and(|attempt| attempt.started + ATTEMPT_TIMEOUT <= now) {
                        self.attempt = None;
                        self.error = Some("Connecting took too long.".into());
                        changed = true;
                    }
                    if changed {
                        debounce.trigger_now();
                    }
                    if debounce.take_due() {
                        flag.clear();
                        match self.read(conn).await {
                            Ok(state) => {
                                // An error goes out in one state only: the
                                // state without it follows by itself.
                                if state.error.is_some() {
                                    debounce.trigger();
                                }
                                self.publish(state, std::mem::take(&mut force));
                            }
                            Err(TimedOut) => {
                                // NetworkManager is busy; what is shown
                                // stays until it answers.
                                tracing::debug!("network: NetworkManager did not answer in time");
                                debounce.trigger();
                            }
                        }
                    }
                }
            }
        }
    }

    async fn subscribe(&self, conn: &Connection, inbox: &Sender<Msg>, flag: &Flag) -> CallResult<Vec<Task<()>>> {
        let mut tasks = Vec::new();
        // A daemon came or went: everything of it is read again.
        tasks.push(subscribe(conn, &self.ex, owner_rule(NM)?, inbox, |_| Some(Msg::Profiles), || Msg::Lost).await?);
        tasks.push(subscribe(conn, &self.ex, owner_rule(BLUEZ)?, inbox, |_| Some(Msg::Radios), || Msg::Lost).await?);
        // Every property change of NetworkManager's objects: devices,
        // access points, active connections.
        let changed = flag.clone();
        tasks.push(
            subscribe(
                conn,
                &self.ex,
                signal_rule(Some(NM), PROPERTIES, Some("PropertiesChanged"), None)?.path_namespace(NM_PATH)?.build(),
                inbox,
                move |_| changed.raise().then_some(Msg::Changed),
                || Msg::Lost,
            )
            .await?,
        );
        tasks.push(
            subscribe(
                conn,
                &self.ex,
                signal_rule(Some(NM), NM_DEVICE, Some("StateChanged"), None)?.build(),
                inbox,
                |message| {
                    let path = message.header().path()?.to_string();
                    let body = message.body();
                    let (new, _old, reason) = body.deserialize::<(u32, u32, u32)>().ok()?;
                    Some(Msg::DeviceState { path, new, reason })
                },
                || Msg::Lost,
            )
            .await?,
        );
        for (interface, member) in [
            (NM_SETTINGS, "NewConnection"),
            (NM_SETTINGS, "ConnectionRemoved"),
            (NM_CONNECTION, "Updated"),
            (NM_CONNECTION, "Removed"),
        ] {
            tasks.push(
                subscribe(
                    conn,
                    &self.ex,
                    signal_rule(Some(NM), interface, Some(member), None)?.build(),
                    inbox,
                    |_| Some(Msg::Profiles),
                    || Msg::Lost,
                )
                .await?,
            );
        }
        // Bluetooth adapters, for airplane mode.
        tasks.push(
            subscribe(
                conn,
                &self.ex,
                signal_rule(Some(BLUEZ), PROPERTIES, Some("PropertiesChanged"), None)?.arg(0, BLUEZ_ADAPTER)?.build(),
                inbox,
                |_| Some(Msg::Radios),
                || Msg::Lost,
            )
            .await?,
        );
        for member in ["InterfacesAdded", "InterfacesRemoved"] {
            tasks.push(
                subscribe(
                    conn,
                    &self.ex,
                    signal_rule(Some(BLUEZ), OBJECT_MANAGER, Some(member), None)?.build(),
                    inbox,
                    |_| Some(Msg::Radios),
                    || Msg::Lost,
                )
                .await?,
            );
        }
        Ok(tasks)
    }

    // --- reading ---

    /// Reads everything. `Err` when NetworkManager stopped answering in
    /// the middle; objects that vanish while being read are skipped.
    async fn read(&mut self, conn: &Connection) -> Result<NetworkState, TimedOut> {
        if self.radios_stale {
            self.radios = read_radios(conn).await;
            self.radios_stale = false;
        }
        if name_owner(conn, NM).await.is_none() {
            self.forget();
            return Ok(NetworkState { error: self.error.take(), ..NetworkState::default() });
        }
        let Some(manager) = props_of(conn, NM_PATH, NM).await? else {
            self.forget();
            return Ok(NetworkState { error: self.error.take(), ..NetworkState::default() });
        };
        if self.profiles_stale {
            self.profiles = read_profiles(conn).await?;
            self.profiles_stale = false;
        }

        let mut wired = false;
        let mut modem = false;
        let mut wifi_devices = Vec::new();
        // SSID -> (strength, security, access point, device)
        let mut networks: HashMap<String, Net> = HashMap::new();
        let mut strengths: HashMap<String, u8> = HashMap::new();
        let mut connected: Option<String> = None;
        let mut connecting: Vec<String> = Vec::new();

        for device in prop_paths(&manager, "Devices").into_iter().take(MAX_DEVICES) {
            let Some(props) = props_of(conn, &device, NM_DEVICE).await? else { continue };
            let state = prop_u32(&props, "State").unwrap_or(0);
            match prop_u32(&props, "DeviceType") {
                Some(DEVICE_ETHERNET) => wired |= state == STATE_ACTIVATED,
                Some(DEVICE_MODEM) => modem = true,
                Some(DEVICE_WIFI) => {
                    let Some(wireless) = props_of(conn, &device, NM_WIRELESS).await? else { continue };
                    let active = prop_path(&wireless, "ActiveAccessPoint").filter(|path| path != "/");
                    wifi_devices.push(WifiDevice {
                        path: device.clone(),
                        state,
                        last_scan: prop_i64(&wireless, "LastScan").unwrap_or(-1),
                    });
                    for access_point in prop_paths(&wireless, "AccessPoints").into_iter().take(MAX_ACCESS_POINTS) {
                        let Some(ap) = props_of(conn, &access_point, NM_AP).await? else { continue };
                        let Some((name, ssid)) = prop(&ap, "Ssid").and_then(|value| as_bytes(value, 32)).and_then(ssid_name) else {
                            continue;
                        };
                        let strength = prop_u32(&ap, "Strength").unwrap_or(0).min(100) as u8;
                        let security = security(
                            prop_u32(&ap, "Flags").unwrap_or(0),
                            prop_u32(&ap, "WpaFlags").unwrap_or(0),
                            prop_u32(&ap, "RsnFlags").unwrap_or(0),
                        );
                        if active.as_deref() == Some(access_point.as_str()) {
                            if state == STATE_ACTIVATED {
                                connected = Some(name.clone());
                            } else if (STATE_PREPARE..=STATE_SECONDARIES).contains(&state) {
                                connecting.push(name.clone());
                            }
                        }
                        // The strongest access point stands for its network.
                        if strengths.get(&name).is_none_or(|best| strength > *best) {
                            strengths.insert(name.clone(), strength);
                            networks.insert(name, Net { ssid, device: device.clone(), access_point, security });
                        }
                    }
                }
                _ => {}
            }
        }

        let mut vpn = false;
        for active in prop_paths(&manager, "ActiveConnections").into_iter().take(MAX_ACTIVE) {
            let Some(props) = props_of(conn, &active, NM_ACTIVE).await? else { continue };
            let is_vpn = prop_bool(&props, "Vpn").unwrap_or(false)
                || matches!(prop_str(&props, "Type").as_deref(), Some("vpn") | Some("wireguard"));
            vpn |= is_vpn && prop_u32(&props, "State") == Some(ACTIVE_ACTIVATED);
        }

        // A scan is over when a device reports a newer one.
        if let Some(scan) = &self.scan {
            let finished = wifi_devices.iter().any(|device| scan.before.get(&device.path).is_some_and(|before| device.last_scan != *before));
            if finished || wifi_devices.is_empty() {
                self.scan = None;
            }
        }
        if let Some(attempt) = &self.attempt {
            if connected.as_deref() == Some(attempt.ssid.as_str()) {
                self.attempt = None;
            } else {
                connecting.push(attempt.ssid.clone());
            }
        }

        let wifi_enabled = prop_bool(&manager, "WirelessEnabled").unwrap_or(false);
        let wwan_enabled = prop_bool(&manager, "WwanEnabled").unwrap_or(false);
        let mut access_points: Vec<AccessPoint> = networks
            .iter()
            .map(|(name, net)| AccessPoint {
                ssid: name.clone(),
                strength: strengths.get(name).copied().unwrap_or(0),
                secured: !matches!(net.security, Security::Open | Security::Owe),
                known: self.profiles.contains_key(name),
                connected: connected.as_deref() == Some(name.as_str()),
                connecting: connected.as_deref() != Some(name.as_str()) && connecting.contains(name),
            })
            .collect();
        access_points.sort_by(|a, b| b.strength.cmp(&a.strength).then_with(|| a.ssid.cmp(&b.ssid)));

        let wifi_present = !wifi_devices.is_empty();
        let bluetooth_present = !self.radios.adapters.is_empty();
        let bluetooth_on = self.radios.adapters.iter().any(|(_, powered)| *powered);
        // Every radio the machine has is off.
        let airplane = (wifi_present || bluetooth_present) && !(wifi_present && wifi_enabled) && !(modem && wwan_enabled) && !bluetooth_on;

        self.networks = networks;
        self.wifi_devices = wifi_devices;
        Ok(NetworkState {
            available: true,
            connectivity: connectivity(prop_u32(&manager, "Connectivity"), prop_u32(&manager, "State")),
            wired,
            wifi_present,
            wifi_enabled,
            wifi_connected: connected,
            wifi_scanning: self.scan.is_some(),
            access_points,
            vpn,
            airplane,
            error: self.error.take(),
        })
    }

    // --- commands ---

    async fn command(&mut self, conn: &Connection, command: Command) {
        match command {
            Command::WifiSetEnabled(on) => {
                log(set_property(conn, NM, NM_PATH, NM, "WirelessEnabled", Value::from(on)).await, "switching Wi-Fi");
            }
            Command::WifiScan => self.start_scan(conn).await,
            Command::WifiConnect { ssid, passphrase } => self.connect(conn, ssid, passphrase).await,
            Command::WifiDisconnect => {
                self.attempt = None;
                for device in &self.wifi_devices {
                    if device.state > STATE_DISCONNECTED {
                        log(call_raw(conn, CALL_TIMEOUT, NM, &device.path, NM_DEVICE, "Disconnect", &()).await, "disconnecting");
                    }
                }
            }
            Command::WifiForget { ssid } => {
                for profile in self.profiles.get(&ssid).cloned().unwrap_or_default() {
                    log(call_raw(conn, CALL_TIMEOUT, NM, &profile, NM_CONNECTION, "Delete", &()).await, "forgetting a network");
                }
                self.profiles_stale = true;
            }
            Command::AirplaneMode(on) => self.airplane(conn, on).await,
            _ => {}
        }
    }

    async fn start_scan(&mut self, conn: &Connection) {
        let mut before = HashMap::new();
        for device in &self.wifi_devices {
            let options: HashMap<&str, Value<'_>> = HashMap::new();
            match call_raw(conn, CALL_TIMEOUT, NM, &device.path, NM_WIRELESS, "RequestScan", &(options,)).await {
                Ok(_) => {
                    before.insert(device.path.clone(), device.last_scan);
                }
                // NetworkManager refuses a scan right after another one.
                Err(error) => tracing::debug!(%error, "network: no scan"),
            }
        }
        self.scan = (!before.is_empty()).then(|| Scan { started: Instant::now(), before });
    }

    async fn connect(&mut self, conn: &Connection, name: String, passphrase: Option<Secret>) {
        self.error = None;
        self.attempt = None;
        let Some(net) = self.networks.get(&name).cloned() else {
            self.error = Some("The network is not in range any more.".into());
            return;
        };
        let profile = self.profiles.get(&name).and_then(|profiles| profiles.first().cloned());
        let started = match (profile, passphrase) {
            (Some(profile), None) => activate(conn, &profile, &net).await.map(|()| None),
            (Some(profile), Some(passphrase)) => match update_passphrase(conn, &profile, &net, &passphrase).await {
                Ok(()) => activate(conn, &profile, &net).await.map(|()| None),
                Err(error) => Err(error),
            },
            (None, passphrase) => add_and_activate(conn, &name, &net, passphrase.as_ref()).await.map(Some),
        };
        match started {
            Ok(created) => {
                self.attempt = Some(Attempt { ssid: name, device: net.device, started: Instant::now(), created, activating: false });
                if self.attempt.as_ref().is_some_and(|attempt| attempt.created.is_some()) {
                    self.profiles_stale = true;
                }
            }
            Err(message) => self.error = Some(message),
        }
    }

    /// A device changed state: the end of our connection attempt shows
    /// here, with NetworkManager's reason when it failed.
    async fn device_state(&mut self, conn: &Connection, path: &str, new: u32, reason: u32) {
        let Some(attempt) = self.attempt.as_mut().filter(|attempt| attempt.device == path) else {
            return;
        };
        match new {
            STATE_PREPARE..=STATE_SECONDARIES => attempt.activating = true,
            STATE_ACTIVATED if attempt.activating => self.attempt = None,
            STATE_FAILED if attempt.activating => {
                let created = attempt.created.clone();
                self.error = Some(failure_text(reason).into());
                self.attempt = None;
                if let Some(created) = created {
                    log(call_raw(conn, CALL_TIMEOUT, NM, &created, NM_CONNECTION, "Delete", &()).await, "removing the failed profile");
                    self.profiles_stale = true;
                }
            }
            _ => {}
        }
    }

    async fn airplane(&mut self, conn: &Connection, on: bool) {
        if on {
            let manager = get_all(conn, NM, NM_PATH, NM).await.unwrap_or_default();
            let radios = read_radios(conn).await;
            let restore = Restore {
                wwan: prop_bool(&manager, "WwanEnabled").unwrap_or(false),
                adapters: radios.adapters.iter().filter(|(_, powered)| *powered).map(|(path, _)| path.clone()).collect(),
            };
            log(set_property(conn, NM, NM_PATH, NM, "WirelessEnabled", Value::from(false)).await, "switching Wi-Fi off");
            if restore.wwan {
                log(set_property(conn, NM, NM_PATH, NM, "WwanEnabled", Value::from(false)).await, "switching mobile broadband off");
            }
            for adapter in &restore.adapters {
                log(set_property(conn, BLUEZ, adapter, BLUEZ_ADAPTER, "Powered", Value::from(false)).await, "switching Bluetooth off");
            }
            // Asked twice, the first memory is the true one.
            if self.restore.is_none() {
                self.restore = Some(restore);
            }
        } else {
            // Wi-Fi comes back in any case; the rest only if this
            // instance switched it off.
            log(set_property(conn, NM, NM_PATH, NM, "WirelessEnabled", Value::from(true)).await, "switching Wi-Fi on");
            if let Some(restore) = self.restore.take() {
                if restore.wwan {
                    log(set_property(conn, NM, NM_PATH, NM, "WwanEnabled", Value::from(true)).await, "switching mobile broadband on");
                }
                for adapter in &restore.adapters {
                    log(set_property(conn, BLUEZ, adapter, BLUEZ_ADAPTER, "Powered", Value::from(true)).await, "switching Bluetooth on");
                }
            }
        }
        self.radios_stale = true;
    }
}

fn log<T>(result: CallResult<T>, what: &str) {
    if let Err(error) = result {
        tracing::warn!(%error, "network: {what} failed");
    }
}

/// The properties of an object. `Ok(None)` when the object is not there
/// (it vanished, or it has no such interface).
async fn props_of(conn: &Connection, path: &str, interface: &str) -> Result<Option<Props>, TimedOut> {
    match get_all(conn, NM, path, interface).await {
        Ok(props) => Ok(Some(props)),
        Err(CallError::TimedOut) => Err(TimedOut),
        Err(_) => Ok(None),
    }
}

/// An SSID as a name to show and as the bytes it is. Hidden networks
/// (empty or all zero) have no name and are left out.
fn ssid_name(bytes: Vec<u8>) -> Option<(String, Vec<u8>)> {
    if bytes.is_empty() || bytes.iter().all(|byte| *byte == 0) {
        return None;
    }
    let name = clean(&String::from_utf8_lossy(&bytes), 32, false);
    (!name.is_empty()).then_some((name, bytes))
}

fn security(flags: u32, wpa: u32, rsn: u32) -> Security {
    const PRIVACY: u32 = 0x1;
    const KEY_MGMT_PSK: u32 = 0x100;
    const KEY_MGMT_802_1X: u32 = 0x200;
    const KEY_MGMT_SAE: u32 = 0x400;
    const KEY_MGMT_OWE: u32 = 0x800;
    const KEY_MGMT_EAP_SUITE_B_192: u32 = 0x2000;
    let key_management = wpa | rsn;
    if key_management & (KEY_MGMT_802_1X | KEY_MGMT_EAP_SUITE_B_192) != 0 {
        Security::Enterprise
    } else if key_management & KEY_MGMT_PSK != 0 {
        Security::Psk
    } else if key_management & KEY_MGMT_SAE != 0 {
        Security::Sae
    } else if key_management & KEY_MGMT_OWE != 0 {
        Security::Owe
    } else if flags & PRIVACY != 0 || key_management != 0 {
        Security::Wep
    } else {
        Security::Open
    }
}

fn connectivity(connectivity: Option<u32>, state: Option<u32>) -> Connectivity {
    match connectivity {
        Some(4) => Connectivity::Full,
        Some(3) => Connectivity::Limited,
        Some(2) => Connectivity::Portal,
        Some(1) => Connectivity::None,
        // Unknown (checking is switched off): go by the overall state.
        _ => match state {
            Some(70) => Connectivity::Full,
            Some(50) | Some(60) => Connectivity::Limited,
            _ => Connectivity::None,
        },
    }
}

/// What to tell the person for NetworkManager's reason of a failed
/// activation.
fn failure_text(reason: u32) -> &'static str {
    match reason {
        7 => "The passphrase is missing or wrong.",
        8..=11 => "Could not join the network. Check the passphrase.",
        5 | 6 | 15 | 16 | 17 => "The network did not give this device an address.",
        53 => "The network is out of range.",
        _ => "Could not connect to the network.",
    }
}

type Settings = HashMap<String, HashMap<String, OwnedValue>>;

/// The saved Wi-Fi profiles: SSID (as shown) to connection paths.
async fn read_profiles(conn: &Connection) -> Result<HashMap<String, Vec<String>>, TimedOut> {
    let mut profiles: HashMap<String, Vec<String>> = HashMap::new();
    let list: Vec<OwnedObjectPath> = match call(conn, NM, NM_SETTINGS_PATH, NM_SETTINGS, "ListConnections", &()).await {
        Ok(list) => list,
        Err(CallError::TimedOut) => return Err(TimedOut),
        Err(_) => return Ok(profiles),
    };
    for path in list.into_iter().take(MAX_PROFILES) {
        let settings: Settings = match call(conn, NM, path.as_str(), NM_CONNECTION, "GetSettings", &()).await {
            Ok(settings) => settings,
            Err(CallError::TimedOut) => return Err(TimedOut),
            Err(_) => continue,
        };
        let is_wifi = settings.get("connection").and_then(|section| prop_str(section, "type")).as_deref() == Some("802-11-wireless");
        let ssid = settings.get("802-11-wireless").and_then(|section| prop(section, "ssid")).and_then(|value| as_bytes(value, 32));
        if let (true, Some((name, _))) = (is_wifi, ssid.and_then(ssid_name)) {
            profiles.entry(name).or_default().push(path.to_string());
        }
    }
    Ok(profiles)
}

/// BlueZ's adapters and whether they are powered. Does not start BlueZ.
async fn read_radios(conn: &Connection) -> Radios {
    if name_owner(conn, BLUEZ).await.is_none() {
        return Radios::default();
    }
    type Objects = HashMap<OwnedObjectPath, HashMap<String, Props>>;
    let objects: Objects = call(conn, BLUEZ, "/", OBJECT_MANAGER, "GetManagedObjects", &()).await.unwrap_or_default();
    let mut adapters: Vec<(String, bool)> = objects
        .iter()
        .filter_map(|(path, interfaces)| {
            let adapter = interfaces.get(BLUEZ_ADAPTER)?;
            Some((path.to_string(), prop_bool(adapter, "Powered").unwrap_or(false)))
        })
        .collect();
    adapters.sort();
    Radios { adapters }
}

fn object_path(path: &str) -> Result<zbus::zvariant::ObjectPath<'_>, String> {
    zbus::zvariant::ObjectPath::try_from(path).map_err(|_| "Could not connect to the network.".to_owned())
}

/// What NetworkManager said when it refused, for the person.
fn refusal(error: CallError) -> String {
    tracing::debug!(%error, "network: NetworkManager refused the connection");
    match error.name() {
        Some(name) if name.ends_with(".InvalidProperty") || name.ends_with(".InvalidSetting") => {
            "The passphrase is not valid for this network.".into()
        }
        _ => "Could not connect to the network.".into(),
    }
}

async fn activate(conn: &Connection, profile: &str, net: &Net) -> Result<(), String> {
    let body = (object_path(profile)?, object_path(&net.device)?, object_path(&net.access_point)?);
    call_raw(conn, CALL_TIMEOUT, NM, NM_PATH, NM, "ActivateConnection", &body).await.map_err(refusal)?;
    Ok(())
}

/// The security settings for a passphrase, by what the network uses.
fn security_settings<'a>(net: &Net, passphrase: Option<&'a Secret>) -> Result<Option<HashMap<&'static str, Value<'a>>>, String> {
    let needs = |kind: &str| format!("This network needs {kind}.");
    let secret = |passphrase: Option<&'a Secret>| passphrase.map(|secret| Value::from(secret.expose())).ok_or_else(|| needs("a passphrase"));
    Ok(match net.security {
        Security::Open => None,
        Security::Owe => Some(HashMap::from([("key-mgmt", Value::from("owe"))])),
        Security::Wep => Some(HashMap::from([("key-mgmt", Value::from("none")), ("wep-key0", secret(passphrase)?)])),
        Security::Psk => Some(HashMap::from([("key-mgmt", Value::from("wpa-psk")), ("psk", secret(passphrase)?)])),
        Security::Sae => Some(HashMap::from([("key-mgmt", Value::from("sae")), ("psk", secret(passphrase)?)])),
        Security::Enterprise => return Err(needs("sign-in details that are set up in the network settings")),
    })
}

/// Creates a profile for the network and connects. Returns the new
/// profile's path.
async fn add_and_activate(conn: &Connection, name: &str, net: &Net, passphrase: Option<&Secret>) -> Result<String, String> {
    let mut settings: HashMap<&str, HashMap<&str, Value<'_>>> = HashMap::new();
    settings.insert("connection", HashMap::from([("type", Value::from("802-11-wireless")), ("id", Value::from(name))]));
    settings.insert("802-11-wireless", HashMap::from([("ssid", Value::from(net.ssid.clone())), ("mode", Value::from("infrastructure"))]));
    if let Some(security) = security_settings(net, passphrase)? {
        settings.insert("802-11-wireless-security", security);
    }
    let body = (settings, object_path(&net.device)?, object_path(&net.access_point)?);
    let reply = call_raw(conn, CALL_TIMEOUT, NM, NM_PATH, NM, "AddAndActivateConnection", &body).await.map_err(refusal)?;
    let body = reply.body();
    let (profile, _active) = body.deserialize::<(OwnedObjectPath, OwnedObjectPath)>().map_err(|_| "Could not connect to the network.".to_owned())?;
    Ok(profile.to_string())
}

/// Stores a new passphrase in a saved profile (the old one was wrong).
async fn update_passphrase(conn: &Connection, profile: &str, net: &Net, passphrase: &Secret) -> Result<(), String> {
    let saved: Settings = call(conn, NM, profile, NM_CONNECTION, "GetSettings", &()).await.map_err(refusal)?;
    let mut settings: HashMap<&str, HashMap<&str, Value<'_>>> = saved
        .iter()
        .map(|(section, values)| {
            let values = values.iter().filter_map(|(key, value)| Some((key.as_str(), Value::from(value.try_clone().ok()?)))).collect();
            (section.as_str(), values)
        })
        .collect();
    if let Some(security) = security_settings(net, Some(passphrase))? {
        settings.entry("802-11-wireless-security").or_default().extend(security);
    }
    call_raw(conn, CALL_TIMEOUT, NM, profile, NM_CONNECTION, "Update", &(settings,)).await.map_err(refusal)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn security_from_the_flags() {
        assert_eq!(security(0, 0, 0), Security::Open);
        assert_eq!(security(1, 0, 0), Security::Wep);
        assert_eq!(security(1, 0x104, 0), Security::Psk);
        assert_eq!(security(1, 0, 0x188), Security::Psk);
        // WPA2/WPA3 transition: the passphrase works as WPA2.
        assert_eq!(security(1, 0, 0x588), Security::Psk);
        assert_eq!(security(1, 0, 0x488), Security::Sae);
        assert_eq!(security(1, 0, 0x288), Security::Enterprise);
        assert_eq!(security(1, 0, 0x2000), Security::Enterprise);
        assert_eq!(security(0, 0, 0x800), Security::Owe);
    }

    #[test]
    fn connectivity_from_the_numbers() {
        assert_eq!(connectivity(Some(4), Some(70)), Connectivity::Full);
        assert_eq!(connectivity(Some(3), Some(60)), Connectivity::Limited);
        assert_eq!(connectivity(Some(2), Some(60)), Connectivity::Portal);
        assert_eq!(connectivity(Some(1), Some(20)), Connectivity::None);
        assert_eq!(connectivity(Some(0), Some(70)), Connectivity::Full);
        assert_eq!(connectivity(None, Some(50)), Connectivity::Limited);
        assert_eq!(connectivity(Some(99), None), Connectivity::None);
    }

    #[test]
    fn ssids() {
        assert_eq!(ssid_name(b"Home".to_vec()), Some(("Home".into(), b"Home".to_vec())));
        assert_eq!(ssid_name(vec![]), None);
        assert_eq!(ssid_name(vec![0, 0, 0]), None);
        // Not UTF-8: shown with replacement characters, connected to by
        // its bytes.
        let (name, bytes) = ssid_name(vec![0xff, b'a']).unwrap();
        assert_eq!(name, "\u{fffd}a");
        assert_eq!(bytes, [0xff, b'a']);
        // Control characters do not reach the shell.
        assert_eq!(ssid_name(b"a\x1b[31mb\n".to_vec()).unwrap().0, "a[31mb");
    }

    #[test]
    fn security_settings_by_kind() {
        let net = |security| Net { ssid: b"x".to_vec(), device: "/d".into(), access_point: "/a".into(), security };
        let secret = Secret::new("correct horse".into());
        assert!(security_settings(&net(Security::Open), None).unwrap().is_none());
        assert!(security_settings(&net(Security::Open), Some(&secret)).unwrap().is_none());
        let psk = security_settings(&net(Security::Psk), Some(&secret)).unwrap().unwrap();
        assert_eq!(psk["key-mgmt"], Value::from("wpa-psk"));
        assert_eq!(psk["psk"], Value::from("correct horse"));
        let sae = security_settings(&net(Security::Sae), Some(&secret)).unwrap().unwrap();
        assert_eq!(sae["key-mgmt"], Value::from("sae"));
        let wep = security_settings(&net(Security::Wep), Some(&secret)).unwrap().unwrap();
        assert_eq!(wep["key-mgmt"], Value::from("none"));
        assert!(wep.contains_key("wep-key0"));
        assert!(security_settings(&net(Security::Psk), None).is_err());
        assert!(security_settings(&net(Security::Enterprise), Some(&secret)).is_err());
        let owe = security_settings(&net(Security::Owe), None).unwrap().unwrap();
        assert_eq!(owe["key-mgmt"], Value::from("owe"));
    }

    #[test]
    fn failure_reasons_have_words() {
        assert!(failure_text(7).contains("passphrase"));
        assert!(failure_text(9).contains("passphrase"));
        assert!(failure_text(53).contains("range"));
        assert!(failure_text(16).contains("address"));
        assert!(!failure_text(9999).is_empty());
    }
}
