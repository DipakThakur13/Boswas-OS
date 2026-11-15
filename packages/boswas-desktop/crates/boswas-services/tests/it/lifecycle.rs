//! All services together: starting, the restricted instance of the lock
//! screen, buses going away, dropping, the environment, and what ends up
//! in the log.

use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Write},
    process::{Command as Process, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex, OnceLock,
    },
    time::{Duration, Instant},
};

use boswas_services::{AuthProgress, Command, Config, Event, PowerAction, PowerProfile, Secret, Service, Services};
use zbus::zvariant::Value;

use crate::{mocks::*, support::*};

const NOTIFICATIONS: &str = "org.freedesktop.Notifications";
const NOTIFICATIONS_PATH: &str = "/org/freedesktop/Notifications";
const WATCHER: &str = "org.kde.StatusNotifierWatcher";

fn notify(conn: &zbus::blocking::Connection, summary: &str) -> u32 {
    let hints: HashMap<&str, Value<'_>> = HashMap::new();
    let reply = conn
        .call_method(Some(NOTIFICATIONS), NOTIFICATIONS_PATH, Some(NOTIFICATIONS), "Notify", &("Tests", 0u32, "", summary, "", Vec::<&str>::new(), hints, -1i32))
        .unwrap();
    reply.body().deserialize().unwrap()
}

fn fake_sound(harness: &Harness) {
    let dir = harness.dir.path().display().to_string();
    std::fs::write(
        harness.path("pw.json"),
        r#"[ { "id": 34, "type": "PipeWire:Interface:Metadata", "props": { "metadata.name": "default" },
               "metadata": [ { "subject": 0, "key": "default.audio.sink", "value": { "name": "speakers" } } ] },
             { "id": 50, "type": "PipeWire:Interface:Node",
               "info": { "props": { "media.class": "Audio/Sink", "node.name": "speakers", "node.description": "Speakers" },
                         "params": { "Props": [ { "mute": false, "channelVolumes": [ 1.0 ] } ] } } } ]
"#,
    )
    .unwrap();
    harness.script("pw-dump", &format!("echo $$ > {dir}/pw-dump.pid\nexec tail -n +1 -f {dir}/pw.json"));
    harness.script("wpctl", &format!("echo \"$*\" >> {dir}/wpctl.log"));
}

fn fake_backlight(harness: &Harness, logind: &Peer) {
    let dir = harness.path("backlight").join("panel");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("type"), "raw\n").unwrap();
    std::fs::write(dir.join("max_brightness"), "100\n").unwrap();
    std::fs::write(dir.join("brightness"), "50\n").unwrap();
    let root = harness.path("backlight");
    logind.on(LOGIN1_SESSION, "SetBrightness", move |_, call| {
        let (_, name, value): (String, String, u32) = call.args();
        std::fs::write(root.join(name).join("brightness"), format!("{value}\n")).unwrap();
        Reply::Done
    });
}

/// Every kind of state event has arrived at least once.
fn all_states_reported(events: &[Event]) -> Option<()> {
    let has = |pick: fn(&Event) -> bool| events.iter().any(pick);
    (has(|e| matches!(e, Event::Network(_)))
        && has(|e| matches!(e, Event::Bluetooth(_)))
        && has(|e| matches!(e, Event::Audio(_)))
        && has(|e| matches!(e, Event::Power(_)))
        && has(|e| matches!(e, Event::Brightness(_)))
        && has(|e| matches!(e, Event::Tray(_)))
        && has(|e| matches!(e, Event::Media(_))))
    .then_some(())
}

#[test]
fn the_desktop_reports_the_state_of_every_service_even_with_nothing_there() {
    let harness = Harness::new();
    let services = harness.start(Config::desktop());
    harness.events.wait("a first state of every service", all_states_reported);
    // Nothing is there: every state says so.
    for event in harness.events.all() {
        match event {
            Event::Network(state) => assert!(!state.available),
            Event::Bluetooth(state) => assert!(!state.available),
            Event::Audio(state) => assert!(!state.available),
            Event::Power(state) => assert!(state.battery.is_none() && state.profiles.is_empty()),
            Event::Brightness(level) => assert!(level.is_none()),
            Event::Tray(items) => assert!(items.is_empty()),
            Event::Media(players) => assert!(players.is_empty()),
            other => panic!("unexpected event {other:?}"),
        }
    }
    // Every Refresh is answered.
    for service in [Service::Network, Service::Bluetooth, Service::Audio, Service::Power, Service::Brightness, Service::Tray, Service::Media] {
        let before = harness.events.len();
        services.send(Command::Refresh(service));
        harness.events.after(before, "the answer to Refresh", |event| {
            let answers = matches!(
                (service, event),
                (Service::Network, Event::Network(_))
                    | (Service::Bluetooth, Event::Bluetooth(_))
                    | (Service::Audio, Event::Audio(_))
                    | (Service::Power, Event::Power(_))
                    | (Service::Brightness, Event::Brightness(_))
                    | (Service::Tray, Event::Tray(_))
                    | (Service::Media, Event::Media(_))
            );
            answers.then_some(())
        });
    }
    // The session-bus names are served.
    let client = harness.session.connect();
    wait_for_name(&client, NOTIFICATIONS);
    wait_for_name(&client, WATCHER);
}

#[test]
fn commands_for_services_that_do_not_run_are_dropped() {
    let harness = Harness::new();
    let services = harness.start(only(|config| config.session = true));
    for command in [
        Command::Refresh(Service::Network),
        Command::WifiScan,
        Command::BluetoothDiscover(true),
        Command::SetVolume(0.5),
        Command::SetBrightness(0.5),
        Command::Power(PowerAction::Suspend),
        Command::NotificationDismiss { id: 1 },
        Command::SetDoNotDisturb(true),
        Command::TrayMenu { key: "k".into() },
        Command::MediaNext { key: "k".into() },
        Command::AuthCancel { cookie: "c".into() },
        Command::AuthChooseUser { cookie: "c".into(), user: "u".into() },
    ] {
        services.send(command);
    }
    drop(services);
    assert!(harness.events.all().is_empty());
}

#[test]
fn the_lock_screen_shows_the_state_and_changes_only_volume_and_brightness() {
    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    nm.add_access_point(1, b"Home", 70, WPA2);
    nm.add_profile(b"Home");
    let bluez = bluez(&harness.system);
    bluez_device(&bluez, "AA:BB:CC:00:00:01", Some("Headset"), true);
    let upower = upower(&harness.system);
    let profiles = power_profiles(&harness.system, PROFILES, PROFILES_PATH);
    let logind = logind(&harness.system);
    let polkit = polkit(&harness.system);
    fake_sound(&harness);
    fake_backlight(&harness, &logind);
    let services = harness.start(Config::status_only());

    // The state is all there.
    harness.events.wait("the state of the machine", |events| {
        let network = events.iter().any(|e| matches!(e, Event::Network(s) if s.access_points.len() == 1));
        let bluetooth = events.iter().any(|e| matches!(e, Event::Bluetooth(s) if s.devices.len() == 1));
        let audio = events.iter().any(|e| matches!(e, Event::Audio(s) if s.output.is_some()));
        let power = events.iter().any(|e| matches!(e, Event::Power(s) if s.battery.is_some() && s.profile.is_some() && s.can_power_off));
        let brightness = events.iter().any(|e| matches!(e, Event::Brightness(Some(_))));
        (network && bluetooth && audio && power && brightness).then_some(())
    });

    // Everything that would change the system is ignored...
    for command in [
        Command::WifiSetEnabled(false),
        Command::WifiScan,
        Command::WifiConnect { ssid: "Home".into(), passphrase: None },
        Command::WifiConnect { ssid: "Home".into(), passphrase: Some(Secret::new("a passphrase".into())) },
        Command::WifiDisconnect,
        Command::WifiForget { ssid: "Home".into() },
        Command::AirplaneMode(true),
        Command::BluetoothSetEnabled(false),
        Command::BluetoothDiscover(true),
        Command::BluetoothConnect { address: "AA:BB:CC:00:00:01".into() },
        Command::BluetoothDisconnect { address: "AA:BB:CC:00:00:01".into() },
        Command::BluetoothForget { address: "AA:BB:CC:00:00:01".into() },
        Command::SetDefaultOutput { id: 50 },
        Command::SetDefaultInput { id: 50 },
        Command::SetPowerProfile(PowerProfile::Performance),
        Command::Power(PowerAction::Suspend),
        Command::Power(PowerAction::Hibernate),
        Command::Power(PowerAction::Reboot),
        Command::Power(PowerAction::PowerOff),
        Command::Power(PowerAction::LogOut),
        Command::NotificationDismiss { id: 1 },
        Command::SetDoNotDisturb(true),
        Command::TrayActivate { key: "k".into(), x: 0, y: 0 },
        Command::MediaPlayPause { key: "org.mpris.MediaPlayer2.x".into() },
        Command::AuthRespond { cookie: "c".into(), response: Secret::new("pw".into()) },
        Command::AuthCancel { cookie: "c".into() },
        Command::AuthChooseUser { cookie: "c".into(), user: "root".into() },
    ] {
        services.send(command);
    }
    // ...and the volume and the brightness are obeyed.
    services.send(Command::SetVolume(0.25));
    services.send(Command::SetBrightness(0.8));
    harness.events.latest("the brightness", |event| match event { Event::Brightness(level) => Some(level), _ => None }, |level| level.is_some_and(|l| (l.level - 0.8).abs() < 1e-6));
    eventually("wpctl to be run", || std::fs::read_to_string(harness.path("wpctl.log")).unwrap_or_default().contains("set-volume -l 1.5 50 0.250"));
    // Each service has dealt with the commands sent before its Refresh.
    for service in [Service::Network, Service::Bluetooth, Service::Power] {
        let before = harness.events.len();
        services.send(Command::Refresh(service));
        harness.events.after(before, "the answer to Refresh", |event| matches!(event, Event::Network(_) | Event::Bluetooth(_) | Event::Power(_)).then_some(()));
    }

    let reads = ["GetAll", "Get", "GetManagedObjects", "ListConnections", "GetSettings", "GetSession", "GetSessionByPID", "CanSuspend", "CanHibernate", "CanReboot", "CanPowerOff"];
    for (name, peer) in [("NetworkManager", &nm.peer), ("BlueZ", &bluez), ("UPower", &upower), ("power profiles", &profiles)] {
        let writes: Vec<String> = peer.all_calls().into_iter().map(|call| call.member).filter(|member| !reads.contains(&member.as_str())).collect();
        assert!(writes.is_empty(), "{name} was told to {writes:?}");
    }
    let writes: Vec<String> = logind.all_calls().into_iter().map(|call| call.member).filter(|member| !reads.contains(&member.as_str())).collect();
    assert_eq!(writes, ["SetBrightness"]);
    // It takes over nothing: no notification server, no tray, no agents.
    assert!(polkit.all_calls().is_empty());
    let client = harness.session.connect();
    for name in [NOTIFICATIONS, WATCHER] {
        let owner = client.call_method(Some("org.freedesktop.DBus"), "/org/freedesktop/DBus", Some("org.freedesktop.DBus"), "GetNameOwner", &(name,));
        assert!(owner.is_err(), "{name} is owned");
    }
    assert!(harness.events.all().iter().all(|event| matches!(
        event,
        Event::Network(_) | Event::Bluetooth(_) | Event::Audio(_) | Event::Power(_) | Event::Brightness(_)
    )));
}

#[test]
fn dropping_stops_threads_connections_and_child_processes_promptly() {
    let harness = Harness::new();
    let _nm = Nm::start(&harness.system);
    let bluez = bluez(&harness.system);
    let _upower = upower(&harness.system);
    let _logind = logind(&harness.system);
    let polkit = polkit(&harness.system);
    fake_sound(&harness);
    let services = harness.start(Config::desktop());
    harness.events.wait("everything running", |events| {
        (all_states_reported(events).is_some() && events.iter().any(|e| matches!(e, Event::Audio(s) if s.available))).then_some(())
    });
    let client = harness.session.connect();
    wait_for_name(&client, NOTIFICATIONS);
    wait_for_name(&client, WATCHER);
    polkit.wait_call("RegisterAuthenticationAgent");
    bluez.wait_call("RegisterAgent");
    let monitor: u32 = std::fs::read_to_string(harness.path("pw-dump.pid")).unwrap().trim().parse().unwrap();
    let system = harness.system.connect();
    let connections = |conn: &zbus::blocking::Connection| {
        let reply = conn.call_method(Some("org.freedesktop.DBus"), "/org/freedesktop/DBus", Some("org.freedesktop.DBus"), "ListNames", &()).unwrap();
        reply.body().deserialize::<Vec<String>>().unwrap().into_iter().filter(|name| name.starts_with(':')).count()
    };
    let before = (connections(&client), connections(&system));

    let started = Instant::now();
    drop(services);
    let took = started.elapsed();
    assert!(took < Duration::from_secs(3), "dropping took {took:?}");

    // Names are released, agents unregistered, every connection of the
    // services is closed, the monitor is gone, and nothing more is said.
    wait_for_no_name(&client, NOTIFICATIONS);
    wait_for_no_name(&client, WATCHER);
    polkit.wait_call("UnregisterAuthenticationAgent");
    bluez.wait_call("UnregisterAgent");
    // Session bus: notifications, tray, media. System bus: network,
    // Bluetooth, power, session, auth (brightness connects on first use).
    eventually("the session-bus connections to close", || connections(&client) == before.0 - 3);
    eventually("the system-bus connections to close", || connections(&system) == before.1 - 5);
    eventually("the monitor to be killed", || !std::path::Path::new(&format!("/proc/{monitor}/cmdline")).exists() || std::fs::read_to_string(format!("/proc/{monitor}/stat")).map(|stat| stat.contains(") Z")).unwrap_or(true));
    let count = harness.events.len();
    notify_is_unanswered(&client);
    assert_eq!(harness.events.len(), count);
}

fn notify_is_unanswered(conn: &zbus::blocking::Connection) {
    let hints: HashMap<&str, Value<'_>> = HashMap::new();
    let result = conn.call_method(Some(NOTIFICATIONS), NOTIFICATIONS_PATH, Some(NOTIFICATIONS), "Notify", &("Tests", 0u32, "", "anybody?", "", Vec::<&str>::new(), hints, -1i32));
    assert!(result.is_err());
}

#[test]
fn the_buses_can_go_away_under_the_services() {
    let mut harness = Harness::new();
    let nm = Nm::start(&harness.system);
    nm.add_access_point(1, b"Home", 70, OPEN);
    let _bluez = bluez(&harness.system);
    let _upower = upower(&harness.system);
    let services = harness.start(Config::desktop());
    harness.events.wait("the state", |events| {
        let network = events.iter().any(|e| matches!(e, Event::Network(s) if s.access_points.len() == 1));
        let bluetooth = events.iter().any(|e| matches!(e, Event::Bluetooth(s) if s.available));
        let power = events.iter().any(|e| matches!(e, Event::Power(s) if s.battery.is_some()));
        (network && bluetooth && power && all_states_reported(events).is_some()).then_some(())
    });
    let client = harness.session.connect();
    wait_for_name(&client, NOTIFICATIONS);
    let id = notify(&client, "before the bus goes");
    harness.events.wait("the notification", |events| events.iter().find(|e| matches!(e, Event::Notify(_))).map(|_| ()));

    harness.system.stop();
    harness.events.latest("no network", |e| match e { Event::Network(s) => Some(s), _ => None }, |state| !state.available && state.access_points.is_empty());
    harness.events.latest("no Bluetooth", |e| match e { Event::Bluetooth(s) => Some(s), _ => None }, |state| !state.available);
    harness.events.latest("no battery", |e| match e { Event::Power(s) => Some(s), _ => None }, |state| state.battery.is_none());

    harness.session.stop();
    // What applications had sent is gone with them.
    harness.events.wait("the notification to be withdrawn", |events| {
        events.iter().find(|e| matches!(e, Event::NotificationClosed { id: closed, withdrawn: true } if *closed == id)).map(|_| ())
    });
    // Commands go nowhere; dropping is still prompt.
    services.send(Command::WifiScan);
    services.send(Command::Refresh(Service::Power));
    services.send(Command::NotificationDismiss { id });
    services.send(Command::Power(PowerAction::Suspend));
    let started = Instant::now();
    drop(services);
    assert!(started.elapsed() < Duration::from_secs(3), "dropping took {:?}", started.elapsed());
}

#[test]
fn the_services_come_back_when_a_bus_does() {
    let mut harness = Harness::new();
    let first = upower(&harness.system);
    let _ = first;
    let _services = harness.start(only(|config| {
        config.status = true;
        config.notifications = true;
    }));
    let battery = |events: &Events, what: &str, present: bool| {
        events.latest(what, |e| match e { Event::Power(s) => Some(s), _ => None }, |state| state.battery.is_some() == present);
    };
    battery(&harness.events, "the battery", true);
    let client = harness.session.connect();
    wait_for_name(&client, NOTIFICATIONS);
    assert_eq!(notify(&client, "one"), 1);

    // The system bus restarts; UPower comes back on it.
    harness.system.restart();
    battery(&harness.events, "no battery while the bus is gone", false);
    let _second = upower(&harness.system);
    battery(&harness.events, "the battery again", true);

    // The session bus restarts: the notification server takes its name
    // again and does not hand out the ids it used before.
    harness.session.restart();
    harness.events.wait("the first notification to be withdrawn", |events| {
        events.iter().find(|e| matches!(e, Event::NotificationClosed { id: 1, withdrawn: true })).map(|_| ())
    });
    let client = loop {
        // The daemon may not listen yet.
        if let Ok(conn) = zbus::blocking::connection::Builder::address(harness.session.address.as_str()).unwrap().build() {
            break conn;
        }
        std::thread::yield_now();
    };
    wait_for_name(&client, NOTIFICATIONS);
    assert_eq!(notify(&client, "two"), 2);
    harness.events.wait("the second notification", |events| events.iter().find(|e| matches!(e, Event::Notify(n) if n.id == 2)).map(|_| ()));
}

// --- the environment ----------------------------------------------------------------

/// `Services::start` takes the bus addresses from the process environment:
/// `DBUS_SESSION_BUS_ADDRESS` as every D-Bus program does, and
/// `BOSWAS_SERVICES_SYSTEM_BUS` for the system bus. The environment of a
/// running test cannot be changed safely, so this test runs itself again
/// as a child process with those variables set.
#[test]
fn bus_addresses_come_from_the_environment() {
    if std::env::var_os("BOSWAS_SERVICES_TEST_CHILD").is_some() {
        child_with_real_environment();
        return;
    }
    let harness = Harness::new();
    let _upower = upower(&harness.system);
    let _logind = logind(&harness.system);
    let client = harness.session.connect();
    let mut child = Process::new(std::env::current_exe().unwrap())
        .args(["--exact", "lifecycle::bus_addresses_come_from_the_environment", "--nocapture", "--test-threads=1"])
        .env("BOSWAS_SERVICES_TEST_CHILD", "1")
        .env("DBUS_SESSION_BUS_ADDRESS", &harness.session.address)
        .env("BOSWAS_SERVICES_SYSTEM_BUS", &harness.system.address)
        .env("XDG_SESSION_ID", "7")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let (lines, incoming) = std::sync::mpsc::channel::<String>();
    let output = child.stdout.take().unwrap();
    std::thread::spawn(move || {
        for line in BufReader::new(output).lines().map_while(Result::ok) {
            if lines.send(line).is_err() {
                break;
            }
        }
    });
    let wait_line = |what: &str| {
        let deadline = Instant::now() + TIMEOUT;
        loop {
            let left = deadline.checked_duration_since(Instant::now()).unwrap_or_else(|| panic!("the child did not report {what}"));
            let line = incoming.recv_timeout(left).unwrap_or_else(|_| panic!("the child did not report {what}"));
            if line.contains(what) {
                return;
            }
        }
    };
    // The battery is the stand-in UPower's on our private "system" bus.
    wait_line("CHILD battery 80");
    // The notification server is on our private session bus.
    wait_for_name(&client, NOTIFICATIONS);
    notify(&client, "over the private bus");
    wait_line("CHILD notification over the private bus");
    // Closing its input ends the child.
    drop(child.stdin.take());
    let status = child.wait().unwrap();
    assert!(status.success());
}

fn child_with_real_environment() {
    let services = Services::start(Config::desktop(), |event| {
        let mut out = std::io::stdout().lock();
        match event {
            Event::Power(state) => {
                if let Some(battery) = state.battery {
                    let _ = writeln!(out, "CHILD battery {}", battery.percent);
                }
            }
            Event::Notify(notification) => {
                let _ = writeln!(out, "CHILD notification {}", notification.summary);
            }
            _ => {}
        }
        let _ = out.flush();
    });
    // Until the parent closes our input.
    let mut line = String::new();
    let _ = std::io::stdin().lock().read_line(&mut line);
    drop(services);
}

// --- the log ------------------------------------------------------------------------

/// Collects everything logged through `tracing` while it is switched on.
struct LogCapture {
    on: AtomicBool,
    text: Mutex<String>,
}

struct Writer<'a>(&'a mut String);

impl tracing::field::Visit for Writer<'_> {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        use std::fmt::Write;
        let _ = write!(self.0, "{}={:?} ", field.name(), value);
    }
}

impl tracing::Subscriber for &'static LogCapture {
    fn register_callsite(&self, _: &'static tracing::Metadata<'static>) -> tracing::subscriber::Interest {
        // Asked again for every event: the capture is switched on and off.
        tracing::subscriber::Interest::sometimes()
    }

    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        self.on.load(Ordering::Relaxed)
    }

    // Each line is put together before the lock is taken: formatting a
    // value may log by itself, and the lock is not re-entrant.

    fn new_span(&self, attributes: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        let mut line = String::new();
        attributes.record(&mut Writer(&mut line));
        line.push('\n');
        self.text.lock().unwrap().push_str(&line);
        tracing::span::Id::from_u64(1)
    }

    fn record(&self, _: &tracing::span::Id, values: &tracing::span::Record<'_>) {
        let mut line = String::new();
        values.record(&mut Writer(&mut line));
        line.push('\n');
        self.text.lock().unwrap().push_str(&line);
    }

    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}

    fn event(&self, event: &tracing::Event<'_>) {
        let mut line = String::from(event.metadata().target());
        line.push(' ');
        event.record(&mut Writer(&mut line));
        line.push('\n');
        self.text.lock().unwrap().push_str(&line);
    }

    fn enter(&self, _: &tracing::span::Id) {}

    fn exit(&self, _: &tracing::span::Id) {}
}

fn log_capture() -> &'static LogCapture {
    static CAPTURE: OnceLock<&'static LogCapture> = OnceLock::new();
    CAPTURE.get_or_init(|| {
        let capture: &'static LogCapture = Box::leak(Box::new(LogCapture { on: AtomicBool::new(false), text: Mutex::new(String::new()) }));
        tracing::subscriber::set_global_default(capture).expect("no other subscriber in the tests");
        capture
    })
}

#[test]
fn secrets_do_not_reach_the_log_at_any_level() {
    const PASSPHRASE: &str = "wifi-passphrase-zebra-7731";
    const PASSWORD: &str = "account-password-quartz-9942";
    const WRONG: &str = "wrong-guess-walrus-1187";

    let harness = Harness::new();
    let nm = Nm::start(&harness.system);
    let home = nm.add_access_point(1, b"Home", 80, WPA2);
    let known = nm.add_access_point(2, b"Known", 60, WPA2);
    nm.add_profile(b"Known");
    let polkit = polkit(&harness.system);
    let _logind = logind(&harness.system);
    let dir = harness.dir.path().display().to_string();
    harness.script(
        "polkit-agent-helper-1",
        &format!("read cookie\necho 'PAM_PROMPT_ECHO_OFF Password: '\nread answer\nif [ \"$answer\" = {PASSWORD} ]; then echo SUCCESS; else echo FAILURE; fi\necho done >> {dir}/helper.log"),
    );

    // Everything the services and zbus log, down to TRACE, from here on.
    let capture = log_capture();
    capture.on.store(true, Ordering::Relaxed);
    let mut config = Config::settings();
    config.auth_agent = true;
    let services = harness.start(config);
    harness.events.latest("the networks", |e| match e { Event::Network(s) => Some(s), _ => None }, |state| state.access_points.len() == 2);

    // A new network with a passphrase, and a new passphrase for a known one.
    services.send(Command::WifiConnect { ssid: "Home".into(), passphrase: Some(Secret::new(PASSPHRASE.into())) });
    nm.peer.wait_call("AddAndActivateConnection");
    nm.connected_to(&home);
    services.send(Command::WifiConnect { ssid: "Known".into(), passphrase: Some(Secret::new(PASSPHRASE.into())) });
    nm.peer.wait_call("Update");
    nm.peer.wait_call("ActivateConnection");
    // The activation fails: the error is logged and reported, the secret is not.
    nm.device_state(WIFI, 40, 0);
    nm.device_state(WIFI, 120, 7);
    let _ = known;
    harness.events.wait("the failure", |events| events.iter().find(|e| matches!(e, Event::Network(s) if s.error.is_some())).map(|_| ()));

    // An authentication with a wrong and then the right password.
    let registration = polkit.wait_call("RegisterAuthenticationAgent");
    let conn = polkit.conn.clone();
    let agent = registration.sender.clone();
    let call = std::thread::spawn(move || {
        let identities: Vec<(&str, HashMap<&str, Value<'_>>)> = vec![("unix-user", HashMap::from([("uid", Value::U32(0))]))];
        let details: HashMap<&str, &str> = HashMap::new();
        conn.call_method(Some(agent.as_str()), "/org/boswas/shell/AuthenticationAgent", Some(POLKIT_AGENT), "BeginAuthentication", &("a.b", "m", "", details, "cookie-log", identities)).is_ok()
    });
    let prompts = |count: usize| {
        harness.events.wait("a prompt", |events| {
            (events.iter().filter(|e| matches!(e, Event::AuthProgress { progress: AuthProgress::Prompt { .. }, .. })).count() >= count).then_some(())
        })
    };
    prompts(1);
    let wrong = Command::AuthRespond { cookie: "cookie-log".into(), response: Secret::new(WRONG.into()) };
    tracing::debug!(command = ?wrong, "a caller logs the command it sends");
    services.send(wrong);
    prompts(2);
    services.send(Command::AuthRespond { cookie: "cookie-log".into(), response: Secret::new(PASSWORD.into()) });
    assert!(call.join().unwrap(), "the right password authorizes");
    drop(services);
    capture.on.store(false, Ordering::Relaxed);

    let log = capture.text.lock().unwrap().clone();
    // The capture worked: the services' and zbus's own lines are there...
    assert!(log.contains("boswas_services"), "nothing of the services was captured");
    assert!(log.contains("zbus"), "nothing of zbus was captured");
    assert!(log.contains("Secret(..)"));
    // ...and no secret is.
    for secret in [PASSPHRASE, PASSWORD, WRONG] {
        assert!(!log.contains(secret), "a secret reached the log");
    }
    // The passphrase did reach NetworkManager, the password the helper.
    let (settings, _, _): (Settings, zbus::zvariant::OwnedObjectPath, zbus::zvariant::OwnedObjectPath) = nm.peer.wait_call("AddAndActivateConnection").args();
    assert_eq!(<&str>::try_from(&*settings["802-11-wireless-security"]["psk"]).unwrap(), PASSPHRASE);
}
