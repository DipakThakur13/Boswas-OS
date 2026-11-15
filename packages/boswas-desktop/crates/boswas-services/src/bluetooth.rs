//! Bluetooth: BlueZ on the system bus, read through its object manager.
//!
//! The service also registers the pairing agent. The agent has no way to
//! show a code or ask a question, so its policy is fixed: it agrees only
//! to what concerns a device the person asked to connect to in the last
//! minute, and refuses everything else.

use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use async_channel::{Receiver, Sender};
use async_executor::Task;
use zbus::{
    message::Header,
    zvariant::{ObjectPath, OwnedObjectPath, Value},
    Connection,
};

use crate::{
    runtime::{
        call, call_raw, earliest, idle, name_owner, owner_rule, set_property, signal_rule, subscribe, wake, Backoff, Bus, BusKind,
        CallResult, Ctx, Debounce, Exec, Flag, IdleEnd, Latest, Service, Wake, CALL_TIMEOUT, PROPERTIES,
    },
    var::{clean, prop_bool, prop_path, prop_str, prop_u32, Props},
    BluetoothDevice, BluetoothState, Command, Event, Service as Which,
};

const BLUEZ: &str = "org.bluez";
const BLUEZ_PATH: &str = "/org/bluez";
const ADAPTER: &str = "org.bluez.Adapter1";
const DEVICE: &str = "org.bluez.Device1";
const BATTERY: &str = "org.bluez.Battery1";
const AGENT_MANAGER: &str = "org.bluez.AgentManager1";
const OBJECT_MANAGER: &str = "org.freedesktop.DBus.ObjectManager";
const AGENT_PATH: &str = "/org/boswas/shell/BluetoothAgent";

/// How long after `BluetoothConnect` the agent agrees to pair with that
/// device.
const ASKED_WINDOW: Duration = Duration::from_secs(60);
/// Discovery is stopped after this long: it costs power and airtime.
const DISCOVERY_TIME: Duration = Duration::from_secs(60);
/// Pairing waits for the person at the other device.
const PAIR_TIMEOUT: Duration = Duration::from_secs(60);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(40);
/// Devices shown at most; a crowded room has dozens.
const MAX_DEVICES: usize = 256;

type Asked = Arc<Mutex<HashMap<String, Instant>>>;
type Objects = HashMap<OwnedObjectPath, HashMap<String, Props>>;

pub(crate) struct Bluetooth {
    ctx: Ctx,
    ex: Exec,
    /// The connection, kept here so that the agent can be unregistered
    /// when the service is stopped in the middle of waiting.
    bus: Option<Bus>,
    state: Latest<BluetoothState>,
    /// The adapters of the last read: (path, powered).
    adapters: Vec<(String, bool)>,
    /// The devices of the last read, by address.
    devices: HashMap<String, Known>,
    /// Device path to when the person asked to connect to it.
    asked: Asked,
    /// The connection of BlueZ: only it may talk to the agent.
    bluez_owner: Arc<Mutex<Option<String>>>,
    /// Connection attempts on their way, by address.
    connecting: HashMap<String, Task<()>>,
    /// Other calls on their way.
    calls: Vec<Task<()>>,
    /// The adapters this service started discovery on, and until when.
    discovery: Option<(Vec<String>, Instant)>,
    agent_registered: bool,
}

struct Known {
    path: String,
    adapter: String,
    paired: bool,
}

enum Msg {
    Changed,
    /// BlueZ came or went.
    Owner,
    ConnectDone { address: String },
    Lost,
}

// --- the agent --------------------------------------------------------------------

struct Agent {
    asked: Asked,
    bluez_owner: Arc<Mutex<Option<String>>>,
}

#[derive(Debug, zbus::DBusError)]
#[zbus(prefix = "org.bluez.Error")]
enum AgentError {
    #[zbus(error)]
    ZBus(zbus::Error),
    Rejected(String),
}

/// The whole policy: was this device asked for, and recently enough?
fn solicited(asked_at: Option<Instant>, now: Instant) -> bool {
    asked_at.is_some_and(|at| now.saturating_duration_since(at) <= ASKED_WINDOW)
}

impl Agent {
    fn decide(&self, device: &ObjectPath<'_>, header: &Header<'_>) -> Result<(), AgentError> {
        let from_bluez = match (header.sender(), self.bluez_owner.lock()) {
            (Some(sender), Ok(owner)) => owner.as_deref() == Some(sender.as_str()),
            _ => false,
        };
        if !from_bluez {
            return Err(AgentError::Rejected("only the Bluetooth daemon may ask".into()));
        }
        let asked_at = self.asked.lock().ok().and_then(|asked| asked.get(device.as_str()).copied());
        if solicited(asked_at, Instant::now()) {
            Ok(())
        } else {
            tracing::info!(device = %device, "bluetooth: refused a pairing request nobody asked for");
            Err(AgentError::Rejected("pairing with this device was not asked for".into()))
        }
    }
}

#[zbus::interface(name = "org.bluez.Agent1")]
impl Agent {
    async fn release(&self) {}

    async fn cancel(&self) {}

    /// "Is this the code the other device shows?" Nobody can look, so the
    /// answer depends only on whether the device was asked for.
    async fn request_confirmation(&self, device: ObjectPath<'_>, _passkey: u32, #[zbus(header)] header: Header<'_>) -> Result<(), AgentError> {
        self.decide(&device, &header)
    }

    async fn request_authorization(&self, device: ObjectPath<'_>, #[zbus(header)] header: Header<'_>) -> Result<(), AgentError> {
        self.decide(&device, &header)
    }

    async fn authorize_service(&self, device: ObjectPath<'_>, _uuid: String, #[zbus(header)] header: Header<'_>) -> Result<(), AgentError> {
        self.decide(&device, &header)
    }

    // There is no keyboard and no display behind this agent: codes can be
    // neither typed nor shown, so these are always refused.

    async fn request_pin_code(&self, _device: ObjectPath<'_>) -> Result<String, AgentError> {
        Err(AgentError::Rejected("no way to enter a PIN".into()))
    }

    async fn request_passkey(&self, _device: ObjectPath<'_>) -> Result<u32, AgentError> {
        Err(AgentError::Rejected("no way to enter a passkey".into()))
    }

    async fn display_pin_code(&self, _device: ObjectPath<'_>, _pincode: String) -> Result<(), AgentError> {
        Err(AgentError::Rejected("no way to show a PIN".into()))
    }

    async fn display_passkey(&self, _device: ObjectPath<'_>, _passkey: u32, _entered: u16) -> Result<(), AgentError> {
        Err(AgentError::Rejected("no way to show a passkey".into()))
    }
}

// --- the service ------------------------------------------------------------------

impl Service for Bluetooth {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self {
            ctx,
            ex,
            bus: None,
            state: Latest::default(),
            adapters: Vec::new(),
            devices: HashMap::new(),
            asked: Arc::default(),
            bluez_owner: Arc::default(),
            connecting: HashMap::new(),
            calls: Vec::new(),
            discovery: None,
            agent_registered: false,
        }
    }

    async fn run(&mut self, commands: Receiver<Command>) {
        let mut backoff = Backoff::new(Duration::from_secs(30));
        loop {
            match Bus::connect(BusKind::System, &self.ctx.env, &self.ex).await {
                Ok(bus) => {
                    backoff.reset();
                    let conn = bus.conn.clone();
                    self.bus = Some(bus);
                    match self.serve(&conn, &commands).await {
                        // `shutdown` unregisters the agent.
                        Ok(true) => return,
                        Ok(false) => {}
                        Err(error) => tracing::debug!(%error, "bluetooth: the system bus stopped answering"),
                    }
                    self.bus = None;
                }
                Err(error) => tracing::debug!(%error, "bluetooth: no system bus"),
            }
            self.forget();
            self.publish(BluetoothState::default(), false);
            match idle(&commands, backoff.next(), |command| matches!(command, Command::Refresh(Which::Bluetooth))).await {
                IdleEnd::Closed => return,
                IdleEnd::Retry => self.publish(BluetoothState::default(), true),
                IdleEnd::Elapsed => {}
            }
        }
    }

    async fn shutdown(&mut self) {
        self.connecting.clear();
        self.calls.clear();
        if let Some(bus) = self.bus.take() {
            self.unregister(&bus.conn).await;
        }
    }
}

impl Bluetooth {
    fn publish(&mut self, state: BluetoothState, force: bool) {
        if let Some(state) = self.state.update(state, force) {
            self.ctx.emit(Event::Bluetooth(state));
        }
    }

    fn forget(&mut self) {
        self.adapters.clear();
        self.devices.clear();
        self.connecting.clear();
        self.calls.clear();
        self.discovery = None;
        self.agent_registered = false;
        if let Ok(mut owner) = self.bluez_owner.lock() {
            *owner = None;
        }
    }

    async fn serve(&mut self, conn: &Connection, commands: &Receiver<Command>) -> CallResult<bool> {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        let flag = Flag::default();
        let _subscriptions = self.subscribe(conn, &inbox, &flag).await?;
        if !self.ctx.restricted {
            // The lock screen and the greeter pair nothing; the agent
            // belongs to the desktop.
            conn.object_server()
                .at(AGENT_PATH, Agent { asked: self.asked.clone(), bluez_owner: self.bluez_owner.clone() })
                .await?;
        }
        let mut debounce = Debounce::default();
        debounce.trigger_now();
        let mut force = false;
        loop {
            let deadline = earliest(debounce.deadline(), self.discovery.as_ref().map(|(_, until)| *until));
            match wake(commands, &messages, deadline).await {
                Wake::Closed => return Ok(true),
                Wake::Message(Msg::Lost) => return Ok(false),
                Wake::Message(Msg::Changed) => debounce.trigger(),
                Wake::Message(Msg::Owner) => {
                    // A new BlueZ knows nothing of our agent or of what
                    // was on its way.
                    self.agent_registered = false;
                    self.connecting.clear();
                    self.discovery = None;
                    debounce.trigger();
                }
                Wake::Message(Msg::ConnectDone { address }) => {
                    self.connecting.remove(&address);
                    debounce.trigger();
                }
                Wake::Command(Command::Refresh(Which::Bluetooth)) => {
                    force = true;
                    debounce.trigger_now();
                }
                Wake::Command(command) => {
                    if !self.ctx.restricted {
                        self.command(conn, command, &inbox);
                        debounce.trigger();
                    }
                }
                Wake::Timer => {
                    if self.discovery.as_ref().is_some_and(|(_, until)| *until <= Instant::now()) {
                        self.stop_discovery(conn);
                    }
                    if debounce.take_due() {
                        flag.clear();
                        let state = self.read(conn).await;
                        self.publish(state, std::mem::take(&mut force));
                    }
                }
            }
        }
    }

    async fn subscribe(&self, conn: &Connection, inbox: &Sender<Msg>, flag: &Flag) -> CallResult<Vec<Task<()>>> {
        let mut tasks = Vec::new();
        tasks.push(subscribe(conn, &self.ex, owner_rule(BLUEZ)?, inbox, |_| Some(Msg::Owner), || Msg::Lost).await?);
        let changed = flag.clone();
        tasks.push(
            subscribe(
                conn,
                &self.ex,
                signal_rule(Some(BLUEZ), PROPERTIES, Some("PropertiesChanged"), None)?.path_namespace(BLUEZ_PATH)?.build(),
                inbox,
                move |_| changed.raise().then_some(Msg::Changed),
                || Msg::Lost,
            )
            .await?,
        );
        for member in ["InterfacesAdded", "InterfacesRemoved"] {
            let changed = flag.clone();
            tasks.push(
                subscribe(
                    conn,
                    &self.ex,
                    signal_rule(Some(BLUEZ), OBJECT_MANAGER, Some(member), None)?.build(),
                    inbox,
                    move |_| changed.raise().then_some(Msg::Changed),
                    || Msg::Lost,
                )
                .await?,
            );
        }
        Ok(tasks)
    }

    async fn read(&mut self, conn: &Connection) -> BluetoothState {
        // BlueZ is not started by asking: without it there is no adapter.
        let owner = name_owner(conn, BLUEZ).await;
        if let Ok(mut known) = self.bluez_owner.lock() {
            *known = owner.clone();
        }
        if owner.is_none() {
            self.adapters.clear();
            self.devices.clear();
            return BluetoothState::default();
        }
        if !self.agent_registered && !self.ctx.restricted {
            self.agent_registered = register_agent(conn).await;
        }
        let objects: Objects = match call(conn, BLUEZ, "/", OBJECT_MANAGER, "GetManagedObjects", &()).await {
            Ok(objects) => objects,
            Err(error) => {
                tracing::debug!(%error, "bluetooth: BlueZ did not list its objects");
                return self.state.get().cloned().unwrap_or_default();
            }
        };
        let (state, adapters, devices) = describe(&objects, |address| self.connecting.contains_key(address));
        self.adapters = adapters;
        self.devices = devices;
        state
    }

    fn command(&mut self, conn: &Connection, command: Command, inbox: &Sender<Msg>) {
        match command {
            Command::BluetoothSetEnabled(on) => {
                for (adapter, _) in self.adapters.clone() {
                    let conn = conn.clone();
                    self.spawn(async move {
                        if let Err(error) = set_property(&conn, BLUEZ, &adapter, ADAPTER, "Powered", Value::from(on)).await {
                            tracing::warn!(%error, "bluetooth: the adapter was not switched");
                        }
                    });
                }
            }
            Command::BluetoothDiscover(true) => {
                let adapters: Vec<String> = self.adapters.iter().filter(|(_, powered)| *powered).map(|(path, _)| path.clone()).collect();
                if adapters.is_empty() {
                    return;
                }
                for adapter in &adapters {
                    self.adapter_call(conn, adapter, "StartDiscovery");
                }
                self.discovery = Some((adapters, Instant::now() + DISCOVERY_TIME));
            }
            Command::BluetoothDiscover(false) => self.stop_discovery(conn),
            Command::BluetoothConnect { address } => {
                let Some(device) = self.devices.get(&address) else { return };
                // From now on, and for a minute, the agent agrees to pair
                // with this device.
                if let Ok(mut asked) = self.asked.lock() {
                    let now = Instant::now();
                    asked.retain(|_, at| solicited(Some(*at), now));
                    asked.insert(device.path.clone(), now);
                }
                if self.connecting.contains_key(&address) {
                    return;
                }
                let (conn, path, paired, inbox) = (conn.clone(), device.path.clone(), device.paired, inbox.clone());
                let done = address.clone();
                let task = self.ex.spawn(async move {
                    if let Err(error) = pair_and_connect(&conn, &path, paired).await {
                        tracing::warn!(%error, "bluetooth: could not connect");
                    }
                    let _ = inbox.send(Msg::ConnectDone { address: done }).await;
                });
                self.connecting.insert(address, task);
            }
            Command::BluetoothDisconnect { address } => {
                let Some(device) = self.devices.get(&address) else { return };
                let (conn, path) = (conn.clone(), device.path.clone());
                // An attempt on its way is given up, pairing included.
                let was_connecting = self.connecting.remove(&address).is_some();
                self.spawn(async move {
                    if was_connecting {
                        let _ = call_raw(&conn, CALL_TIMEOUT, BLUEZ, &path, DEVICE, "CancelPairing", &()).await;
                    }
                    if let Err(error) = call_raw(&conn, CALL_TIMEOUT, BLUEZ, &path, DEVICE, "Disconnect", &()).await {
                        tracing::debug!(%error, "bluetooth: could not disconnect");
                    }
                });
            }
            Command::BluetoothForget { address } => {
                let Some(device) = self.devices.get(&address) else { return };
                let (conn, path, adapter) = (conn.clone(), device.path.clone(), device.adapter.clone());
                self.connecting.remove(&address);
                if let Ok(mut asked) = self.asked.lock() {
                    asked.remove(&path);
                }
                self.spawn(async move {
                    let Ok(device) = ObjectPath::try_from(path.as_str()) else { return };
                    if let Err(error) = call_raw(&conn, CALL_TIMEOUT, BLUEZ, &adapter, ADAPTER, "RemoveDevice", &(device,)).await {
                        tracing::warn!(%error, "bluetooth: the device was not removed");
                    }
                });
            }
            _ => {}
        }
    }

    fn spawn(&mut self, work: impl std::future::Future<Output = ()> + 'static) {
        self.calls.retain(|task| !task.is_finished());
        self.calls.push(self.ex.spawn(work));
    }

    fn adapter_call(&mut self, conn: &Connection, adapter: &str, method: &'static str) {
        let (conn, adapter) = (conn.clone(), adapter.to_owned());
        self.spawn(async move {
            if let Err(error) = call_raw(&conn, CALL_TIMEOUT, BLUEZ, &adapter, ADAPTER, method, &()).await {
                tracing::debug!(method, %error, "bluetooth: the adapter refused");
            }
        });
    }

    fn stop_discovery(&mut self, conn: &Connection) {
        if let Some((adapters, _)) = self.discovery.take() {
            for adapter in &adapters {
                self.adapter_call(conn, adapter, "StopDiscovery");
            }
        }
    }

    async fn unregister(&mut self, conn: &Connection) {
        if self.agent_registered {
            if let Ok(path) = ObjectPath::try_from(AGENT_PATH) {
                let _ = call_raw(conn, Duration::from_millis(500), BLUEZ, BLUEZ_PATH, AGENT_MANAGER, "UnregisterAgent", &(path,)).await;
            }
            self.agent_registered = false;
        }
    }
}

/// Registers the agent and makes it the default, so that requests nobody
/// in this session started arrive here (and are refused).
async fn register_agent(conn: &Connection) -> bool {
    let Ok(path) = ObjectPath::try_from(AGENT_PATH) else {
        return false;
    };
    // "NoInputNoOutput": the agent can neither show nor ask for a code.
    // BlueZ then pairs without one ("just works") where the device allows.
    match call_raw(conn, CALL_TIMEOUT, BLUEZ, BLUEZ_PATH, AGENT_MANAGER, "RegisterAgent", &(&path, "NoInputNoOutput")).await {
        Ok(_) => {}
        Err(error) if error.name() == Some("org.bluez.Error.AlreadyExists") => {}
        Err(error) => {
            tracing::debug!(%error, "bluetooth: the pairing agent was not registered");
            return false;
        }
    }
    if let Err(error) = call_raw(conn, CALL_TIMEOUT, BLUEZ, BLUEZ_PATH, AGENT_MANAGER, "RequestDefaultAgent", &(&path,)).await {
        tracing::debug!(%error, "bluetooth: the pairing agent is not the default one");
    }
    true
}

/// Pairs if needed, trusts the device (so that it may reconnect by
/// itself) and connects.
async fn pair_and_connect(conn: &Connection, path: &str, paired: bool) -> CallResult<()> {
    if !paired {
        match call_raw(conn, PAIR_TIMEOUT, BLUEZ, path, DEVICE, "Pair", &()).await {
            Ok(_) => {}
            Err(error) if error.name() == Some("org.bluez.Error.AlreadyExists") => {}
            Err(error) => return Err(error),
        }
        if let Err(error) = set_property(conn, BLUEZ, path, DEVICE, "Trusted", Value::from(true)).await {
            tracing::debug!(%error, "bluetooth: the device was not marked as trusted");
        }
    }
    match call_raw(conn, CONNECT_TIMEOUT, BLUEZ, path, DEVICE, "Connect", &()).await {
        Ok(_) => Ok(()),
        // Pairing connects some devices already.
        Err(error) if error.name() == Some("org.bluez.Error.AlreadyConnected") => Ok(()),
        Err(error) => Err(error),
    }
}

/// `AA:BB:CC:DD:EE:FF`.
fn plausible_address(address: &str) -> bool {
    address.len() == 17
        && address.bytes().enumerate().all(|(index, byte)| if index % 3 == 2 { byte == b':' } else { byte.is_ascii_hexdigit() })
}

/// What the shell sees of BlueZ's objects, and what the service keeps to
/// act on: the adapters and the devices by address.
fn describe(objects: &Objects, connecting: impl Fn(&str) -> bool) -> (BluetoothState, Vec<(String, bool)>, HashMap<String, Known>) {
    let mut adapters: Vec<(String, bool)> = Vec::new();
    let mut discovering = false;
    for (path, interfaces) in objects {
        if let Some(adapter) = interfaces.get(ADAPTER) {
            adapters.push((path.to_string(), prop_bool(adapter, "Powered").unwrap_or(false)));
            discovering |= prop_bool(adapter, "Discovering").unwrap_or(false);
        }
    }
    adapters.sort();

    let mut known = HashMap::new();
    let mut devices = Vec::new();
    for (path, interfaces) in objects {
        let Some(device) = interfaces.get(DEVICE) else { continue };
        let Some(address) = prop_str(device, "Address").map(|address| address.to_ascii_uppercase()).filter(|address| plausible_address(address)) else {
            continue;
        };
        let paired = prop_bool(device, "Paired").unwrap_or(false);
        let connected = prop_bool(device, "Connected").unwrap_or(false);
        // A device that was only seen and has no name is noise in a list
        // for people (BlueZ's alias for it is its address).
        let name = prop_str(device, "Name").or_else(|| (paired || connected).then(|| prop_str(device, "Alias")).flatten());
        let Some(name) = name else { continue };
        let name = prop_str(device, "Alias").map(|alias| clean(&alias, 64, false)).filter(|alias| !alias.is_empty()).unwrap_or_else(|| clean(&name, 64, false));
        if name.is_empty() {
            continue;
        }
        let icon = prop_str(device, "Icon")
            .filter(|icon| icon.len() <= 64 && icon.chars().all(|ch| ch.is_ascii_alphanumeric() || ch == '-' || ch == '_'))
            .unwrap_or_default();
        let battery = interfaces.get(BATTERY).and_then(|battery| prop_u32(battery, "Percentage")).map(|percent| percent.min(100) as u8);
        known.insert(
            address.clone(),
            Known {
                path: path.to_string(),
                adapter: prop_path(device, "Adapter").unwrap_or_else(|| parent_path(path.as_str())),
                paired,
            },
        );
        devices.push(BluetoothDevice {
            connecting: !connected && connecting(&address),
            address,
            name,
            icon,
            paired,
            connected,
            battery,
        });
    }
    // Paired devices first, then the discovered ones, each by name.
    devices.sort_by(|a, b| {
        b.paired.cmp(&a.paired).then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase())).then_with(|| a.address.cmp(&b.address))
    });
    devices.truncate(MAX_DEVICES);
    let state = BluetoothState {
        available: !adapters.is_empty(),
        enabled: adapters.iter().any(|(_, powered)| *powered),
        discovering,
        devices,
    };
    (state, adapters, known)
}

/// `/org/bluez/hci0/dev_X` is a device of `/org/bluez/hci0`.
fn parent_path(path: &str) -> String {
    match path.rfind('/') {
        Some(0) | None => "/".to_owned(),
        Some(index) => path[..index].to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use zbus::zvariant::OwnedValue;

    #[test]
    fn the_agent_agrees_only_to_what_was_asked_for_recently() {
        let now = Instant::now();
        assert!(!solicited(None, now), "an unsolicited request is refused");
        assert!(solicited(Some(now), now));
        assert!(solicited(Some(now - Duration::from_secs(59)), now));
        assert!(!solicited(Some(now - Duration::from_secs(61)), now), "a minute later it is unsolicited again");
        assert!(!solicited(Some(now - Duration::from_secs(3600)), now));
    }

    #[test]
    fn addresses() {
        assert!(plausible_address("AA:BB:CC:DD:EE:FF"));
        assert!(plausible_address("00:1a:7D:da:71:13"));
        assert!(!plausible_address("AA:BB:CC:DD:EE"));
        assert!(!plausible_address("AA-BB-CC-DD-EE-FF"));
        assert!(!plausible_address("AA:BB:CC:DD:EE:FG"));
        assert!(!plausible_address("AA:BB:CC:DD:EE:FF:00"));
        assert!(!plausible_address(""));
    }

    fn props(entries: Vec<(&str, Value<'_>)>) -> Props {
        entries.into_iter().map(|(name, value)| (name.to_owned(), OwnedValue::try_from(value).unwrap())).collect()
    }

    fn object(path: &str, interfaces: Vec<(&str, Props)>) -> (OwnedObjectPath, HashMap<String, Props>) {
        (OwnedObjectPath::try_from(path).unwrap(), interfaces.into_iter().map(|(name, props)| (name.to_owned(), props)).collect())
    }

    #[test]
    fn describes_adapters_and_devices() {
        let objects: Objects = [
            object("/org/bluez/hci0", vec![(ADAPTER, props(vec![("Powered", Value::from(true)), ("Discovering", Value::from(true))]))]),
            object(
                "/org/bluez/hci0/dev_1",
                vec![
                    (
                        DEVICE,
                        props(vec![
                            ("Address", Value::from("aa:bb:cc:00:00:01")),
                            ("Name", Value::from("WH-1000")),
                            ("Alias", Value::from("My headphones")),
                            ("Icon", Value::from("audio-headphones")),
                            ("Paired", Value::from(true)),
                            ("Connected", Value::from(true)),
                            ("Adapter", Value::ObjectPath("/org/bluez/hci0".try_into().unwrap())),
                        ]),
                    ),
                    (BATTERY, props(vec![("Percentage", Value::U8(70))])),
                ],
            ),
            object(
                "/org/bluez/hci0/dev_2",
                vec![(DEVICE, props(vec![("Address", Value::from("AA:BB:CC:00:00:02")), ("Name", Value::from("Zebra keyboard")), ("Alias", Value::from("Zebra keyboard"))]))],
            ),
            object(
                "/org/bluez/hci0/dev_3",
                vec![(DEVICE, props(vec![("Address", Value::from("AA:BB:CC:00:00:03")), ("Name", Value::from("apple mouse")), ("Alias", Value::from("apple mouse"))]))],
            ),
            // Seen but nameless: left out.
            object("/org/bluez/hci0/dev_4", vec![(DEVICE, props(vec![("Address", Value::from("AA:BB:CC:00:00:04")), ("Alias", Value::from("AA-BB-CC-00-00-04"))]))]),
            // Nonsense: left out.
            object("/org/bluez/hci0/dev_5", vec![(DEVICE, props(vec![("Address", Value::from(5u32)), ("Name", Value::from("x"))]))]),
            object("/org/bluez/hci0/dev_6", vec![(DEVICE, props(vec![("Address", Value::from("not an address")), ("Name", Value::from("x"))]))]),
        ]
        .into();
        let (state, adapters, known) = describe(&objects, |address| address == "AA:BB:CC:00:00:03");
        assert!(state.available && state.enabled && state.discovering);
        assert_eq!(adapters, [("/org/bluez/hci0".to_owned(), true)]);
        let names: Vec<&str> = state.devices.iter().map(|device| device.name.as_str()).collect();
        assert_eq!(names, ["My headphones", "apple mouse", "Zebra keyboard"]);
        assert_eq!(
            state.devices[0],
            BluetoothDevice {
                address: "AA:BB:CC:00:00:01".into(),
                name: "My headphones".into(),
                icon: "audio-headphones".into(),
                paired: true,
                connected: true,
                connecting: false,
                battery: Some(70),
            }
        );
        assert!(state.devices[1].connecting);
        assert_eq!(known["AA:BB:CC:00:00:02"].adapter, "/org/bluez/hci0");
        assert_eq!(known.len(), 3);
    }

    #[test]
    fn no_adapter_means_unavailable() {
        let (state, adapters, known) = describe(&Objects::new(), |_| false);
        assert_eq!(state, BluetoothState::default());
        assert!(adapters.is_empty() && known.is_empty());
        assert_eq!(parent_path("/org/bluez/hci0/dev_1"), "/org/bluez/hci0");
        assert_eq!(parent_path("/x"), "/");
    }
}
