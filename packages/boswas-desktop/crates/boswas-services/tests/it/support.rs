//! What the tests are built from: private message buses, a collector for
//! events that waits on conditions, and a scriptable bus peer that plays
//! the foreign services (NetworkManager, BlueZ, UPower, logind, polkit,
//! tray items, media players).

#![allow(dead_code)]

use std::{
    collections::{BTreeMap, HashMap},
    io::{BufRead, BufReader},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command as Process, Stdio},
    sync::{Arc, Condvar, Mutex},
    time::{Duration, Instant},
};

use boswas_services::{Config, Environment, Event, Services};
use serde::Serialize;
use zbus::{
    blocking::{Connection, MessageIterator},
    message::{Message, Type as MessageType},
    zvariant::{DynamicType, OwnedObjectPath, OwnedValue, Value},
};

/// How long a test waits for something that must happen. Generous: the
/// machine may be busy with other builds.
pub const TIMEOUT: Duration = Duration::from_secs(20);

pub const PROPERTIES: &str = "org.freedesktop.DBus.Properties";
pub const OBJECT_MANAGER: &str = "org.freedesktop.DBus.ObjectManager";

// --- a private bus ----------------------------------------------------------------

/// A `dbus-daemon` of its own, with its socket and configuration in a
/// directory under /tmp (socket paths are short).
pub struct TestBus {
    daemon: Child,
    pub address: String,
    _dir: tempfile::TempDir,
}

impl TestBus {
    pub fn start() -> Self {
        let dir = tempfile::Builder::new().prefix("bsv").tempdir_in("/tmp").expect("a directory under /tmp");
        let socket = dir.path().join("bus");
        let config = dir.path().join("bus.conf");
        // No service directories: nothing on the machine can be started
        // by a test. Everybody may own and call everything.
        std::fs::write(
            &config,
            format!(
                r#"<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <keep_umask/>
  <listen>unix:path={}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
  <limit name="max_message_size">33554432</limit>
</busconfig>
"#,
                socket.display()
            ),
        )
        .unwrap();
        let mut daemon = Process::new("dbus-daemon")
            .arg(format!("--config-file={}", config.display()))
            .arg("--print-address")
            .arg("--nofork")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("dbus-daemon is installed");
        let mut address = String::new();
        BufReader::new(daemon.stdout.take().unwrap()).read_line(&mut address).unwrap();
        let address = address.trim().to_owned();
        assert!(address.starts_with("unix:"), "dbus-daemon printed {address:?}");
        Self { daemon, address, _dir: dir }
    }

    pub fn connect(&self) -> Connection {
        zbus::blocking::connection::Builder::address(self.address.as_str())
            .unwrap()
            .build()
            .expect("a connection to the test bus")
    }

    /// Stops the daemon: every connection to it breaks.
    pub fn stop(&mut self) {
        let _ = self.daemon.kill();
        let _ = self.daemon.wait();
    }
}

impl Drop for TestBus {
    fn drop(&mut self) {
        self.stop();
    }
}

// --- events -----------------------------------------------------------------------

/// Collects the events of a `Services` instance; tests wait on conditions
/// over them instead of sleeping.
#[derive(Clone, Default)]
pub struct Events(Arc<(Mutex<Vec<Event>>, Condvar)>);

impl Events {
    pub fn sink(&self) -> impl Fn(Event) + Send + Sync + 'static {
        let shared = self.0.clone();
        move |event| {
            shared.0.lock().unwrap().push(event);
            shared.1.notify_all();
        }
    }

    pub fn all(&self) -> Vec<Event> {
        self.0 .0.lock().unwrap().clone()
    }

    pub fn len(&self) -> usize {
        self.0 .0.lock().unwrap().len()
    }

    /// Waits until `find` returns something for the events so far.
    pub fn wait<T>(&self, what: &str, find: impl Fn(&[Event]) -> Option<T>) -> T {
        let deadline = Instant::now() + TIMEOUT;
        let mut events = self.0 .0.lock().unwrap();
        loop {
            if let Some(found) = find(&events) {
                return found;
            }
            let now = Instant::now();
            if now >= deadline {
                panic!("timed out waiting for {what}; events so far: {:#?}", *events);
            }
            events = self.0 .1.wait_timeout(events, deadline - now).unwrap().0;
        }
    }

    /// Waits until the latest event `pick` selects satisfies `good`.
    pub fn latest<T: Clone>(&self, what: &str, pick: impl Fn(&Event) -> Option<&T>, good: impl Fn(&T) -> bool) -> T {
        self.wait(what, |events| events.iter().rev().find_map(&pick).filter(|state| good(state)).cloned())
    }

    /// Waits for an event after position `from` that `pick` selects.
    pub fn after<T>(&self, from: usize, what: &str, pick: impl Fn(&Event) -> Option<T>) -> T {
        self.wait(what, |events| events.iter().skip(from).find_map(&pick))
    }

    /// How many events `pick` selects so far.
    pub fn count(&self, pick: impl Fn(&Event) -> bool) -> usize {
        self.0 .0.lock().unwrap().iter().filter(|event| pick(event)).count()
    }
}

/// Waits until `check` is true.
pub fn eventually(what: &str, check: impl Fn() -> bool) {
    let deadline = Instant::now() + TIMEOUT;
    while !check() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(5));
    }
}

// --- the harness ------------------------------------------------------------------

/// Two private buses (one plays the session bus, one the system bus), a
/// scratch directory for stand-in programs and files, and the events.
pub struct Harness {
    pub session: TestBus,
    pub system: TestBus,
    pub dir: tempfile::TempDir,
    pub events: Events,
}

impl Harness {
    pub fn new() -> Self {
        let dir = tempfile::Builder::new().prefix("bsv").tempdir_in("/tmp").unwrap();
        std::fs::create_dir_all(dir.path().join("backlight")).unwrap();
        std::fs::write(dir.path().join("users"), "root:x:0:0:root:/root:/bin/sh\nada:x:1000:1000:Ada:/home/ada:/bin/sh\n").unwrap();
        Self {
            session: TestBus::start(),
            system: TestBus::start(),
            dir,
            events: Events::default(),
        }
    }

    /// The environment that points the services at this harness. Programs
    /// that a test did not create do not exist.
    pub fn env(&self) -> Environment {
        let mut env = Environment::from_env();
        env.session_bus = Some(self.session.address.clone());
        env.system_bus = Some(self.system.address.clone());
        env.pw_dump = self.dir.path().join("pw-dump");
        env.wpctl = self.dir.path().join("wpctl");
        env.polkit_helper = self.dir.path().join("polkit-agent-helper-1");
        env.backlight_root = self.dir.path().join("backlight");
        env.users_file = self.dir.path().join("users");
        env.session_id = Some("7".into());
        env
    }

    pub fn start(&self, config: Config) -> Services {
        Services::start_with(config, self.env(), self.events.sink())
    }

    pub fn path(&self, name: &str) -> PathBuf {
        self.dir.path().join(name)
    }

    /// Writes an executable shell script into the scratch directory.
    pub fn script(&self, name: &str, body: &str) -> PathBuf {
        write_script(&self.path(name), body)
    }
}

pub fn write_script(path: &Path, body: &str) -> PathBuf {
    // Written under another name and renamed: a script that is being run
    // must not be open for writing.
    let staging = path.with_extension("new");
    std::fs::write(&staging, format!("#!/bin/sh\n{body}\n")).unwrap();
    std::fs::set_permissions(&staging, std::fs::Permissions::from_mode(0o755)).unwrap();
    std::fs::rename(&staging, path).unwrap();
    path.to_owned()
}

/// Only the status services, unrestricted (the desktop without the
/// session-bus services).
pub fn status_config() -> Config {
    let mut config = Config::status_only();
    // Any other service makes the instance unrestricted; the session
    // listener is the quietest.
    config.session = true;
    config
}

/// A configuration that runs exactly one of the session-bus services.
pub fn only(pick: impl FnOnce(&mut Config)) -> Config {
    let mut config = Config {
        notifications: false,
        tray: false,
        auth_agent: false,
        media: false,
        status: false,
        session: false,
    };
    pick(&mut config);
    config
}

// --- values -----------------------------------------------------------------------

pub fn owned<'a>(value: impl Into<Value<'a>>) -> OwnedValue {
    value.into().try_to_owned().unwrap()
}

pub type Props = HashMap<String, OwnedValue>;

/// Builds a property map.
pub fn props<const N: usize>(entries: [(&str, OwnedValue); N]) -> Props {
    entries.into_iter().map(|(name, value)| (name.to_owned(), value)).collect()
}

pub fn path(text: &str) -> OwnedObjectPath {
    OwnedObjectPath::try_from(text).unwrap()
}

pub fn path_value(text: &str) -> OwnedValue {
    owned(Value::ObjectPath(text.try_into().unwrap()))
}

pub fn paths_value(paths: &[&str]) -> OwnedValue {
    let list: Vec<zbus::zvariant::ObjectPath<'_>> = paths.iter().map(|path| (*path).try_into().unwrap()).collect();
    owned(Value::from(list))
}

// --- a scriptable peer ------------------------------------------------------------

/// A method call a peer received.
#[derive(Clone, Debug)]
pub struct Call {
    pub path: String,
    pub interface: String,
    pub member: String,
    pub sender: String,
    pub message: Message,
}

impl Call {
    pub fn args<T>(&self) -> T
    where
        T: serde::de::DeserializeOwned + zbus::zvariant::Type,
    {
        self.message.body().deserialize::<T>().expect("the arguments the test expects")
    }

    pub fn reply<B: Serialize + DynamicType>(&self, body: &B) -> Reply {
        Reply::Message(Message::method_return(&self.message.header()).unwrap().build(body).unwrap())
    }

    pub fn error(&self, name: &str, text: &str) -> Reply {
        Reply::Message(Message::error(&self.message.header(), name.to_owned()).unwrap().build(&(text,)).unwrap())
    }
}

/// What a handler does with a call.
pub enum Reply {
    Message(Message),
    /// An empty method return.
    Done,
    /// No answer at all: the caller waits.
    Silence,
}

type Handler = Arc<dyn Fn(&Peer, &Call) -> Reply + Send + Sync>;

#[derive(Default)]
struct PeerState {
    /// path -> interface -> properties
    objects: BTreeMap<String, BTreeMap<String, Props>>,
    /// (interface, member) -> handler; an empty interface matches any.
    handlers: HashMap<(String, String), Handler>,
    calls: Vec<Call>,
    /// The path that answers `GetManagedObjects`, when the peer has one.
    manager_root: Option<String>,
    /// `Properties.Set` is refused.
    read_only: bool,
}

/// A connection that answers method calls from a table of objects with
/// properties and from handlers, and records every call. It plays any
/// foreign service.
#[derive(Clone)]
pub struct Peer {
    pub conn: Connection,
    state: Arc<(Mutex<PeerState>, Condvar)>,
}

impl Peer {
    /// Connects and takes `names`.
    pub fn start(bus: &TestBus, names: &[&str]) -> Peer {
        let conn = bus.connect();
        let peer = Peer { conn: conn.clone(), state: Arc::default() };
        let incoming = MessageIterator::from(&conn);
        let worker = peer.clone();
        std::thread::Builder::new()
            .name("mock-peer".into())
            .spawn(move || {
                for message in incoming {
                    let Ok(message) = message else { break };
                    if message.message_type() == MessageType::MethodCall {
                        worker.dispatch(message);
                    }
                }
            })
            .unwrap();
        for name in names {
            conn.request_name(*name).expect("the name is free on a test bus");
        }
        peer
    }

    pub fn unique_name(&self) -> String {
        self.conn.unique_name().unwrap().to_string()
    }

    /// Disconnects: the peer vanishes from the bus without a word.
    pub fn vanish(&self) {
        let _ = self.conn.clone().close();
    }

    pub fn release(&self, name: &str) {
        self.conn.release_name(name).unwrap();
    }

    pub fn claim(&self, name: &str) {
        self.conn.request_name(name).unwrap();
    }

    fn dispatch(&self, message: Message) {
        let header = message.header();
        let call = Call {
            path: header.path().map(|path| path.to_string()).unwrap_or_default(),
            interface: header.interface().map(|name| name.to_string()).unwrap_or_default(),
            member: header.member().map(|name| name.to_string()).unwrap_or_default(),
            sender: header.sender().map(|name| name.to_string()).unwrap_or_default(),
            message: message.clone(),
        };
        let handler = {
            let mut state = self.state.0.lock().unwrap();
            state.calls.push(call.clone());
            self.state.1.notify_all();
            state
                .handlers
                .get(&(call.interface.clone(), call.member.clone()))
                .or_else(|| state.handlers.get(&(String::new(), call.member.clone())))
                .cloned()
        };
        let reply = match handler {
            Some(handler) => handler(self, &call),
            None => self.builtin(&call),
        };
        match reply {
            Reply::Message(message) => {
                let _ = self.conn.send(&message);
            }
            Reply::Done => {
                let _ = self.conn.reply(&header, &());
            }
            Reply::Silence => {}
        }
    }

    /// Properties and the object manager, from the table of objects.
    fn builtin(&self, call: &Call) -> Reply {
        let unknown = |what: &str| call.error("org.freedesktop.DBus.Error.UnknownMethod", what);
        match (call.interface.as_str(), call.member.as_str()) {
            (PROPERTIES, "GetAll") => {
                let Ok((interface,)) = call.message.body().deserialize::<(String,)>() else {
                    return call.error("org.freedesktop.DBus.Error.InvalidArgs", "GetAll(s)");
                };
                let state = self.state.0.lock().unwrap();
                match state.objects.get(&call.path) {
                    None => call.error("org.freedesktop.DBus.Error.UnknownObject", &call.path),
                    Some(object) => match object.get(&interface) {
                        Some(props) => call.reply(&(props,)),
                        None => call.error("org.freedesktop.DBus.Error.UnknownInterface", &interface),
                    },
                }
            }
            (PROPERTIES, "Get") => {
                let Ok((interface, name)) = call.message.body().deserialize::<(String, String)>() else {
                    return call.error("org.freedesktop.DBus.Error.InvalidArgs", "Get(ss)");
                };
                let state = self.state.0.lock().unwrap();
                match state.objects.get(&call.path).and_then(|object| object.get(&interface)).and_then(|props| props.get(&name)) {
                    Some(value) => call.reply(&(value,)),
                    None => call.error("org.freedesktop.DBus.Error.UnknownProperty", &name),
                }
            }
            (PROPERTIES, "Set") => {
                let Ok((interface, name, value)) = call.message.body().deserialize::<(String, String, OwnedValue)>() else {
                    return call.error("org.freedesktop.DBus.Error.InvalidArgs", "Set(ssv)");
                };
                if self.state.0.lock().unwrap().read_only {
                    return call.error("org.freedesktop.DBus.Error.PropertyReadOnly", &name);
                }
                self.set(&call.path, &interface, &name, value);
                Reply::Done
            }
            (OBJECT_MANAGER, "GetManagedObjects") => {
                let state = self.state.0.lock().unwrap();
                if state.manager_root.as_deref() != Some(call.path.as_str()) {
                    return unknown("no object manager here");
                }
                let objects: HashMap<OwnedObjectPath, &BTreeMap<String, Props>> =
                    state.objects.iter().filter(|(path, _)| **path != call.path).map(|(path, object)| (self::path(path), object)).collect();
                call.reply(&(objects,))
            }
            ("org.freedesktop.DBus.Peer", "Ping") => Reply::Done,
            _ => unknown(&format!("{}.{}", call.interface, call.member)),
        }
    }

    // --- scripting ---

    /// Answers `interface.member` with `handler` (an empty interface
    /// matches the member on any interface).
    pub fn on(&self, interface: &str, member: &str, handler: impl Fn(&Peer, &Call) -> Reply + Send + Sync + 'static) {
        self.state.0.lock().unwrap().handlers.insert((interface.to_owned(), member.to_owned()), Arc::new(handler));
    }

    /// Answers `interface.member` with an empty reply.
    pub fn accept(&self, interface: &str, member: &str) {
        self.on(interface, member, |_, _| Reply::Done);
    }

    pub fn manage_objects(&self, root: &str) {
        self.state.0.lock().unwrap().manager_root = Some(root.to_owned());
    }

    pub fn read_only(&self, yes: bool) {
        self.state.0.lock().unwrap().read_only = yes;
    }

    /// Adds an interface with properties to an object. With an object
    /// manager, `InterfacesAdded` is emitted.
    pub fn add(&self, path: &str, interface: &str, props: Props) {
        let root = {
            let mut state = self.state.0.lock().unwrap();
            state.objects.entry(path.to_owned()).or_default().insert(interface.to_owned(), props);
            state.manager_root.clone()
        };
        if let Some(root) = root {
            let state = self.state.0.lock().unwrap();
            let added: HashMap<&str, &Props> = [(interface, &state.objects[path][interface])].into();
            let _ = self.conn.emit_signal(None::<&str>, root.as_str(), OBJECT_MANAGER, "InterfacesAdded", &(self::path(path), added));
        }
    }

    /// Removes an object. With an object manager, `InterfacesRemoved` is
    /// emitted.
    pub fn remove(&self, path: &str) {
        let (interfaces, root) = {
            let mut state = self.state.0.lock().unwrap();
            let interfaces: Vec<String> = state.objects.remove(path).map(|object| object.into_keys().collect()).unwrap_or_default();
            (interfaces, state.manager_root.clone())
        };
        if let Some(root) = root {
            let _ = self.conn.emit_signal(None::<&str>, root.as_str(), OBJECT_MANAGER, "InterfacesRemoved", &(self::path(path), interfaces));
        }
    }

    /// Sets a property and emits `PropertiesChanged`.
    pub fn set(&self, path: &str, interface: &str, name: &str, value: OwnedValue) {
        self.set_quietly(path, interface, name, value.try_clone().unwrap());
        let changed: HashMap<&str, &OwnedValue> = [(name, &value)].into();
        let _ = self.conn.emit_signal(None::<&str>, path, PROPERTIES, "PropertiesChanged", &(interface, changed, Vec::<String>::new()));
    }

    /// Sets a property without telling anybody.
    pub fn set_quietly(&self, path: &str, interface: &str, name: &str, value: OwnedValue) {
        let mut state = self.state.0.lock().unwrap();
        state.objects.entry(path.to_owned()).or_default().entry(interface.to_owned()).or_default().insert(name.to_owned(), value);
    }

    pub fn get(&self, path: &str, interface: &str, name: &str) -> Option<OwnedValue> {
        let state = self.state.0.lock().unwrap();
        state.objects.get(path)?.get(interface)?.get(name).map(|value| value.try_clone().unwrap())
    }

    pub fn has_object(&self, path: &str) -> bool {
        self.state.0.lock().unwrap().objects.contains_key(path)
    }

    pub fn signal<B: Serialize + DynamicType>(&self, path: &str, interface: &str, member: &str, body: &B) {
        self.conn.emit_signal(None::<&str>, path, interface, member, body).unwrap();
    }

    /// Answers a call a handler left unanswered.
    pub fn answer<B: Serialize + DynamicType>(&self, call: &Call, body: &B) {
        self.conn.reply(&call.message.header(), body).unwrap();
    }

    pub fn fail(&self, call: &Call, name: &str, text: &str) {
        if let Reply::Message(message) = call.error(name, text) {
            self.conn.send(&message).unwrap();
        }
    }

    // --- what was called ---

    pub fn calls(&self, member: &str) -> Vec<Call> {
        self.state.0.lock().unwrap().calls.iter().filter(|call| call.member == member).cloned().collect()
    }

    pub fn all_calls(&self) -> Vec<Call> {
        self.state.0.lock().unwrap().calls.clone()
    }

    /// Waits for the `count`-th call (from 1) of `member`.
    pub fn wait_call_n(&self, member: &str, count: usize) -> Call {
        let deadline = Instant::now() + TIMEOUT;
        let mut state = self.state.0.lock().unwrap();
        loop {
            if let Some(call) = state.calls.iter().filter(|call| call.member == member).nth(count - 1) {
                return call.clone();
            }
            let now = Instant::now();
            if now >= deadline {
                let seen: Vec<String> = state.calls.iter().map(|call| format!("{}.{} on {}", call.interface, call.member, call.path)).collect();
                panic!("timed out waiting for call {count} of {member}; calls so far: {seen:#?}");
            }
            state = self.state.1.wait_timeout(state, deadline - now).unwrap().0;
        }
    }

    pub fn wait_call(&self, member: &str) -> Call {
        self.wait_call_n(member, 1)
    }

    /// Waits for a call of `member` that `good` accepts.
    pub fn wait_call_where(&self, member: &str, good: impl Fn(&Call) -> bool) -> Call {
        let deadline = Instant::now() + TIMEOUT;
        let mut state = self.state.0.lock().unwrap();
        loop {
            if let Some(call) = state.calls.iter().find(|call| call.member == member && good(call)) {
                return call.clone();
            }
            let now = Instant::now();
            assert!(now < deadline, "timed out waiting for a matching call of {member}");
            state = self.state.1.wait_timeout(state, deadline - now).unwrap().0;
        }
    }
}

// --- a client of our own services -------------------------------------------------

/// Listens for signals; the test reads them with a timeout.
pub struct Signals {
    receiver: std::sync::mpsc::Receiver<Message>,
}

impl Signals {
    /// Signals of `interface` from anybody, as seen by `conn`.
    pub fn listen(conn: &Connection, interface: &str) -> Signals {
        let rule = zbus::MatchRule::builder()
            .msg_type(MessageType::Signal)
            .interface(interface.to_owned())
            .unwrap()
            .build();
        let incoming = MessageIterator::for_match_rule(rule, conn, Some(1024)).unwrap();
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for message in incoming {
                let Ok(message) = message else { break };
                if sender.send(message).is_err() {
                    break;
                }
            }
        });
        Signals { receiver }
    }

    /// The next signal named `member`; others before it are skipped.
    pub fn next(&self, member: &str) -> Message {
        let deadline = Instant::now() + TIMEOUT;
        loop {
            let left = deadline.checked_duration_since(Instant::now()).unwrap_or_else(|| panic!("timed out waiting for the signal {member}"));
            let message = self.receiver.recv_timeout(left).unwrap_or_else(|_| panic!("timed out waiting for the signal {member}"));
            if message.header().member().map(|name| name.as_str() == member).unwrap_or(false) {
                return message;
            }
        }
    }

    /// The signals that have arrived and not been read.
    pub fn drain(&self) -> Vec<Message> {
        self.receiver.try_iter().collect()
    }
}

/// Waits until `name` has an owner on the bus.
pub fn wait_for_name(conn: &Connection, name: &str) -> String {
    let deadline = Instant::now() + TIMEOUT;
    loop {
        let owner = conn.call_method(
            Some("org.freedesktop.DBus"),
            "/org/freedesktop/DBus",
            Some("org.freedesktop.DBus"),
            "GetNameOwner",
            &(name,),
        );
        if let Ok(reply) = owner {
            return reply.body().deserialize::<String>().unwrap();
        }
        assert!(Instant::now() < deadline, "timed out waiting for {name} to be owned");
        std::thread::sleep(Duration::from_millis(5));
    }
}

/// Waits until `name` has no owner.
pub fn wait_for_no_name(conn: &Connection, name: &str) {
    eventually(&format!("{name} to be released"), || {
        conn.call_method(
            Some("org.freedesktop.DBus"),
            "/org/freedesktop/DBus",
            Some("org.freedesktop.DBus"),
            "GetNameOwner",
            &(name,),
        )
        .is_err()
    });
}
