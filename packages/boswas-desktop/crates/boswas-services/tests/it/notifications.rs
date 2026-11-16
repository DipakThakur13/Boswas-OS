//! The notification server, driven by a real client on the bus.

use std::collections::HashMap;

use boswas_services::{Command, Event, Notification, Urgency};
use zbus::{
    blocking::Connection,
    zvariant::{OwnedValue, Value},
};

use crate::support::*;

const NAME: &str = "org.freedesktop.Notifications";
const PATH: &str = "/org/freedesktop/Notifications";

struct Note<'a> {
    app: &'a str,
    replaces: u32,
    icon: &'a str,
    summary: &'a str,
    body: &'a str,
    actions: Vec<&'a str>,
    hints: HashMap<&'a str, Value<'a>>,
    timeout: i32,
}

impl Default for Note<'_> {
    fn default() -> Self {
        Note { app: "Tests", replaces: 0, icon: "", summary: "Hello", body: "", actions: vec![], hints: HashMap::new(), timeout: -1 }
    }
}

fn notify(conn: &Connection, note: Note<'_>) -> zbus::Result<u32> {
    let reply = conn.call_method(
        Some(NAME),
        PATH,
        Some(NAME),
        "Notify",
        &(note.app, note.replaces, note.icon, note.summary, note.body, note.actions, note.hints, note.timeout),
    )?;
    Ok(reply.body().deserialize::<u32>().unwrap())
}

fn shown(event: &Event) -> Option<Notification> {
    match event {
        Event::Notify(notification) => Some(notification.clone()),
        _ => None,
    }
}

fn shown_with_id(events: &Events, id: u32) -> Notification {
    events.wait("the notification", |events| events.iter().rev().filter_map(shown).find(|n| n.id == id))
}

fn closed(events: &Events, id: u32) -> bool {
    events.wait("the notification to close", |events| {
        events.iter().find_map(|event| match event {
            Event::NotificationClosed { id: closed, withdrawn } if *closed == id => Some(*withdrawn),
            _ => None,
        })
    })
}

fn setup() -> (Harness, boswas_services::Services, Connection) {
    let harness = Harness::new();
    let services = harness.start(only(|config| config.notifications = true));
    let client = harness.session.connect();
    wait_for_name(&client, NAME);
    (harness, services, client)
}

#[test]
fn a_notification_reaches_the_shell_as_plain_text() {
    let (harness, _services, client) = setup();
    let before = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
    let id = notify(
        &client,
        Note {
            app: "Mail",
            icon: "mail-unread",
            summary: "New message\nfrom Ada",
            body: "<b>Hi</b> &amp; welcome<br/>See <a href=\"https://example.org\">this</a> &lt;now&gt;",
            actions: vec!["default", "Open", "reply", "_Reply", "", "no key", "dangling"],
            hints: HashMap::from([
                ("urgency", Value::U8(0)),
                ("transient", Value::Bool(true)),
                ("desktop-entry", Value::from("org.example.Mail")),
                ("value", Value::I32(40)),
            ]),
            timeout: -1,
            ..Note::default()
        },
    )
    .unwrap();
    assert_eq!(id, 1, "ids start at 1");
    let notification = shown_with_id(&harness.events, 1);
    assert!(notification.time >= before && notification.time <= before + 60);
    assert_eq!(
        notification,
        Notification {
            id: 1,
            app_name: "Mail".into(),
            app_icon: "mail-unread".into(),
            desktop_entry: Some("org.example.Mail".into()),
            summary: "New message from Ada".into(),
            body: "Hi & welcome\nSee this <now>".into(),
            actions: vec![("default".into(), "Open".into()), ("reply".into(), "_Reply".into())],
            urgency: Urgency::Low,
            timeout_ms: None,
            transient: true,
            progress: Some(40),
            time: notification.time,
            replaces_existing: false,
        }
    );
}

#[test]
fn ids_count_up_and_replacing_keeps_the_id() {
    let (harness, _services, client) = setup();
    assert_eq!(notify(&client, Note { summary: "one", ..Note::default() }).unwrap(), 1);
    assert_eq!(notify(&client, Note { summary: "two", ..Note::default() }).unwrap(), 2);
    // Replacing a live notification keeps its id.
    assert_eq!(notify(&client, Note { summary: "one again", replaces: 1, ..Note::default() }).unwrap(), 1);
    let replaced = harness.events.wait("the replacement", |events| events.iter().filter_map(shown).find(|n| n.summary == "one again"));
    assert!(replaced.replaces_existing);
    // Replacing something that does not exist makes a new notification
    // with a new id; ids are not handed out twice.
    assert_eq!(notify(&client, Note { summary: "three", replaces: 77, ..Note::default() }).unwrap(), 3);
    let fresh = harness.events.wait("the third", |events| events.iter().filter_map(shown).find(|n| n.summary == "three"));
    assert!(!fresh.replaces_existing);
    // A closed id is not live any more: "replacing" it is a new one too.
    client.call_method(Some(NAME), PATH, Some(NAME), "CloseNotification", &(2u32,)).unwrap();
    assert!(closed(&harness.events, 2));
    assert_eq!(notify(&client, Note { summary: "four", replaces: 2, ..Note::default() }).unwrap(), 4);
    // Another client gets the next id, not its own sequence.
    let other = harness.session.connect();
    assert_eq!(notify(&other, Note::default()).unwrap(), 5);
}

#[test]
fn the_server_describes_itself() {
    let (_harness, _services, client) = setup();
    let reply = client.call_method(Some(NAME), PATH, Some(NAME), "GetServerInformation", &()).unwrap();
    let (name, vendor, version, spec): (String, String, String, String) = reply.body().deserialize().unwrap();
    assert_eq!((name.as_str(), vendor.as_str(), spec.as_str()), ("Boswas Shell", "Boswas OS", "1.2"));
    assert_eq!(version, env!("CARGO_PKG_VERSION"));
    let reply = client.call_method(Some(NAME), PATH, Some(NAME), "GetCapabilities", &()).unwrap();
    let capabilities: Vec<String> = reply.body().deserialize().unwrap();
    assert_eq!(capabilities, ["actions", "body", "icon-static", "persistence"]);
}

#[test]
fn closing_by_the_application_withdraws_it() {
    let (harness, _services, client) = setup();
    let signals = Signals::listen(&client, NAME);
    let id = notify(&client, Note::default()).unwrap();
    client.call_method(Some(NAME), PATH, Some(NAME), "CloseNotification", &(id,)).unwrap();
    assert!(closed(&harness.events, id), "closed by its application: withdrawn");
    let signal = signals.next("NotificationClosed");
    assert_eq!(signal.body().deserialize::<(u32, u32)>().unwrap(), (id, 3));
    // Closing it again, or closing what never existed, says nothing.
    client.call_method(Some(NAME), PATH, Some(NAME), "CloseNotification", &(id,)).unwrap();
    client.call_method(Some(NAME), PATH, Some(NAME), "CloseNotification", &(4040u32,)).unwrap();
    let second = notify(&client, Note::default()).unwrap();
    client.call_method(Some(NAME), PATH, Some(NAME), "CloseNotification", &(second,)).unwrap();
    let signal = signals.next("NotificationClosed");
    assert_eq!(signal.body().deserialize::<(u32, u32)>().unwrap(), (second, 3));
    assert_eq!(harness.events.count(|event| matches!(event, Event::NotificationClosed { .. })), 2);
}

#[test]
fn a_timeout_expires_at_the_service_but_critical_ones_stay() {
    let (harness, services, client) = setup();
    let signals = Signals::listen(&client, NAME);
    let critical = notify(
        &client,
        Note { summary: "disk failing", timeout: 30, actions: vec!["ok", "OK"], hints: HashMap::from([("urgency", Value::U8(2))]), ..Note::default() },
    )
    .unwrap();
    let never = notify(&client, Note { summary: "sticky", timeout: 0, ..Note::default() }).unwrap();
    let brief = notify(&client, Note { summary: "brief", timeout: 120, ..Note::default() }).unwrap();
    assert_eq!(shown_with_id(&harness.events, brief).timeout_ms, Some(120));
    assert_eq!(shown_with_id(&harness.events, critical).timeout_ms, None);
    assert_eq!(shown_with_id(&harness.events, critical).urgency, Urgency::Critical);
    assert_eq!(shown_with_id(&harness.events, never).timeout_ms, None);

    assert!(!closed(&harness.events, brief), "expired: the toast goes, the entry stays");
    let signal = signals.next("NotificationClosed");
    assert_eq!(signal.body().deserialize::<(u32, u32)>().unwrap(), (brief, 1));

    // The critical one asked for 30 ms and is still there after the brief
    // one's 120 ms: its action can still be invoked.
    services.send(Command::NotificationAction { id: critical, action: "ok".into() });
    let signal = signals.next("ActionInvoked");
    assert_eq!(signal.body().deserialize::<(u32, String)>().unwrap(), (critical, "ok".to_owned()));
    // So is the one that asked never to expire.
    services.send(Command::NotificationDismiss { id: never });
    loop {
        let signal = signals.next("NotificationClosed");
        let (id, reason): (u32, u32) = signal.body().deserialize().unwrap();
        if id == never {
            assert_eq!(reason, 2);
            break;
        }
    }
    assert_eq!(harness.events.count(|event| matches!(event, Event::NotificationClosed { .. })), 1);
}

#[test]
fn actions_and_dismissal_are_signalled_to_the_application() {
    let (harness, services, client) = setup();
    let signals = Signals::listen(&client, NAME);
    let plain = notify(&client, Note { actions: vec!["open", "Open", "later", "Later"], ..Note::default() }).unwrap();
    let resident = notify(
        &client,
        Note { actions: vec!["pause", "Pause"], hints: HashMap::from([("resident", Value::Bool(true))]), ..Note::default() },
    )
    .unwrap();
    let dismissed = notify(&client, Note::default()).unwrap();
    shown_with_id(&harness.events, dismissed);

    // An action the notification does not have, and an id that does not
    // exist: nothing is sent for them.
    services.send(Command::NotificationAction { id: plain, action: "format-disk".into() });
    services.send(Command::NotificationAction { id: 9999, action: "open".into() });
    services.send(Command::NotificationDismiss { id: 9999 });

    services.send(Command::NotificationAction { id: plain, action: "later".into() });
    let signal = signals.next("ActionInvoked");
    assert_eq!(signal.body().deserialize::<(u32, String)>().unwrap(), (plain, "later".to_owned()));
    let signal = signals.next("NotificationClosed");
    assert_eq!(signal.body().deserialize::<(u32, u32)>().unwrap(), (plain, 2));

    // A resident notification stays open after its action...
    services.send(Command::NotificationAction { id: resident, action: "pause".into() });
    services.send(Command::NotificationAction { id: resident, action: "default".into() });
    let signal = signals.next("ActionInvoked");
    assert_eq!(signal.body().deserialize::<(u32, String)>().unwrap(), (resident, "pause".to_owned()));
    let signal = signals.next("ActionInvoked");
    assert_eq!(signal.body().deserialize::<(u32, String)>().unwrap(), (resident, "default".to_owned()));

    // ...until it is dismissed.
    services.send(Command::NotificationDismiss { id: dismissed });
    services.send(Command::NotificationDismiss { id: resident });
    let signal = signals.next("NotificationClosed");
    assert_eq!(signal.body().deserialize::<(u32, u32)>().unwrap(), (dismissed, 2));
    let signal = signals.next("NotificationClosed");
    assert_eq!(signal.body().deserialize::<(u32, u32)>().unwrap(), (resident, 2));

    // The closed one takes no more actions.
    services.send(Command::NotificationAction { id: plain, action: "open".into() });
    let marker = notify(&client, Note::default()).unwrap();
    client.call_method(Some(NAME), PATH, Some(NAME), "CloseNotification", &(marker,)).unwrap();
    let signal = signals.next("NotificationClosed");
    assert_eq!(signal.body().deserialize::<(u32, u32)>().unwrap(), (marker, 3));
    assert!(signals.drain().is_empty());
    // What the shell closed itself is not reported back to it.
    assert_eq!(harness.events.count(|event| matches!(event, Event::NotificationClosed { withdrawn: false, .. })), 0);
}

#[test]
fn do_not_disturb_is_reported_to_applications() {
    let (harness, services, client) = setup();
    let inhibited = || {
        let reply = client.call_method(Some(NAME), PATH, Some(PROPERTIES), "Get", &(NAME, "Inhibited")).unwrap();
        bool::try_from(reply.body().deserialize::<OwnedValue>().unwrap()).unwrap()
    };
    assert!(!inhibited());
    let signals = Signals::listen(&client, PROPERTIES);
    services.send(Command::SetDoNotDisturb(true));
    let signal = signals.next("PropertiesChanged");
    let (interface, changed, _): (String, HashMap<String, OwnedValue>, Vec<String>) = signal.body().deserialize().unwrap();
    assert_eq!(interface, NAME);
    assert!(bool::try_from(&*changed["Inhibited"]).unwrap());
    assert!(inhibited());
    // Notifications still reach the shell, which decides what to show.
    let id = notify(&client, Note::default()).unwrap();
    shown_with_id(&harness.events, id);
    services.send(Command::SetDoNotDisturb(false));
    signals.next("PropertiesChanged");
    assert!(!inhibited());
}

#[test]
fn absurd_sizes_and_wrong_types_are_refused() {
    let (harness, _services, client) = setup();
    let big = "x".repeat(1024 * 1024);
    let refused = |note: Note<'_>| match notify(&client, note) {
        Err(zbus::Error::MethodError(name, _, _)) => name.to_string(),
        other => panic!("expected a refusal, got {other:?}"),
    };
    let limits = "org.freedesktop.DBus.Error.LimitsExceeded";
    assert_eq!(refused(Note { summary: &big, ..Note::default() }), limits);
    assert_eq!(refused(Note { body: &big, ..Note::default() }), limits);
    assert_eq!(refused(Note { app: &big, ..Note::default() }), limits);
    assert_eq!(refused(Note { icon: &big, ..Note::default() }), limits);
    assert_eq!(refused(Note { actions: vec!["a"; 500], ..Note::default() }), limits);
    assert_eq!(refused(Note { actions: vec!["key", &big], ..Note::default() }), limits);
    let names: Vec<String> = (0..200).map(|n| format!("hint-{n}")).collect();
    let many: HashMap<&str, Value<'_>> = names.iter().map(|name| (name.as_str(), Value::U8(1))).collect();
    assert_eq!(refused(Note { hints: many, ..Note::default() }), limits);

    // The wrong arguments altogether.
    assert!(client.call_method(Some(NAME), PATH, Some(NAME), "Notify", &("just a string",)).is_err());
    assert!(client.call_method(Some(NAME), PATH, Some(NAME), "Notify", &(1u32, 2u32, 3u32)).is_err());
    assert!(client.call_method(Some(NAME), PATH, Some(NAME), "CloseNotification", &("one",)).is_err());
    assert!(client.call_method(Some(NAME), PATH, Some(NAME), "Explode", &()).is_err());
    assert_eq!(harness.events.count(|event| matches!(event, Event::Notify(_))), 0);

    // Large but sane input is cut for display; hints of the wrong type are
    // ignored; control characters do not reach the shell.
    let long_body = format!("{}<b>end</b>", "ä".repeat(30_000));
    let id = notify(
        &client,
        Note {
            app: "App\u{7}\u{1b}[31m",
            summary: &"s".repeat(4000),
            body: &long_body,
            icon: "../../etc/shadow",
            actions: (0..30).flat_map(|_| ["k", "label"]).collect(),
            hints: HashMap::from([
                ("urgency", Value::from("critical!")),
                ("value", Value::from(vec![1u8, 2, 3])),
                ("desktop-entry", Value::from("../../x")),
                ("image-data", Value::from(vec![0u8; 100_000])),
                ("image-path", Value::from("https://example.org/track-me.png")),
            ]),
            timeout: i32::MIN,
            ..Note::default()
        },
    )
    .unwrap();
    let notification = shown_with_id(&harness.events, id);
    assert_eq!(notification.app_name, "App[31m");
    assert_eq!(notification.summary.chars().count(), 256);
    assert_eq!(notification.body.chars().count(), 4096);
    assert_eq!(notification.app_icon, "");
    assert_eq!(notification.actions.len(), 16);
    assert_eq!(notification.urgency, Urgency::Normal);
    assert_eq!(notification.progress, None);
    assert_eq!(notification.desktop_entry, None);
    assert_eq!(notification.timeout_ms, None);
}

#[test]
fn image_path_is_used_as_the_icon() {
    let (harness, _services, client) = setup();
    let id = notify(
        &client,
        Note { icon: "chat", hints: HashMap::from([("image-path", Value::from("file:///tmp/ada%20l.png"))]), ..Note::default() },
    )
    .unwrap();
    assert_eq!(shown_with_id(&harness.events, id).app_icon, "/tmp/ada l.png");
    let id = notify(&client, Note { icon: "file:///usr/share/icons/a.png", ..Note::default() }).unwrap();
    assert_eq!(shown_with_id(&harness.events, id).app_icon, "/usr/share/icons/a.png");
}

#[test]
fn a_client_that_vanishes_leaves_the_server_working() {
    let (harness, _services, client) = setup();
    for _ in 0..20 {
        let fleeting = harness.session.connect();
        // Send without waiting for the reply, then disconnect.
        let call = zbus::message::Message::method_call(PATH, "Notify")
            .unwrap()
            .destination(NAME)
            .unwrap()
            .interface(NAME)
            .unwrap()
            .build(&("Fleeting", 0u32, "", "gone", "", Vec::<&str>::new(), HashMap::<&str, Value<'_>>::new(), -1i32))
            .unwrap();
        fleeting.send(&call).unwrap();
        fleeting.close().unwrap();
    }
    let id = notify(&client, Note { summary: "still here", ..Note::default() }).unwrap();
    assert_eq!(shown_with_id(&harness.events, id).summary, "still here");
}

#[test]
fn the_oldest_notifications_are_closed_when_an_application_floods() {
    let (harness, _services, client) = setup();
    let signals = Signals::listen(&client, NAME);
    let critical = notify(&client, Note { hints: HashMap::from([("urgency", Value::U8(2))]), ..Note::default() }).unwrap();
    assert_eq!(critical, 1);
    for _ in 0..501 {
        notify(&client, Note::default()).unwrap();
    }
    // 502 were sent and 500 are kept: the two oldest that are not critical
    // went, as withdrawn so the shell forgets them too.
    assert!(closed(&harness.events, 2));
    assert!(closed(&harness.events, 3));
    let first = signals.next("NotificationClosed").body().deserialize::<(u32, u32)>().unwrap();
    let second = signals.next("NotificationClosed").body().deserialize::<(u32, u32)>().unwrap();
    assert_eq!((first, second), ((2, 1), (3, 1)));
    assert_eq!(harness.events.count(|event| matches!(event, Event::NotificationClosed { .. })), 2);
}

#[test]
fn another_notification_server_is_left_alone_until_it_leaves() {
    let harness = Harness::new();
    let other = Peer::start(&harness.session, &[NAME]);
    other.on(NAME, "Notify", |_, call| call.reply(&(4242u32,)));
    let services = harness.start(only(|config| config.notifications = true));
    let client = harness.session.connect();
    // The other server keeps answering; ours shows nothing.
    assert_eq!(notify(&client, Note::default()).unwrap(), 4242);
    services.send(Command::SetDoNotDisturb(true));
    assert_eq!(notify(&client, Note::default()).unwrap(), 4242);
    assert_eq!(harness.events.count(|event| matches!(event, Event::Notify(_))), 0);

    // It leaves: ours takes the name and serves.
    let previous = other.unique_name();
    other.vanish();
    eventually("our server to take the name", || wait_for_name(&client, NAME) != previous);
    let id = notify(&client, Note { summary: "ours now", ..Note::default() }).unwrap();
    assert_eq!(id, 1);
    assert_eq!(shown_with_id(&harness.events, 1).summary, "ours now");
}

#[test]
fn dropping_the_services_releases_the_name() {
    let (harness, services, client) = setup();
    notify(&client, Note::default()).unwrap();
    drop(services);
    wait_for_no_name(&client, NAME);
    drop(harness);
}
