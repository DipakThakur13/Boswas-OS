//! The tray: stand-in StatusNotifierItems register with our watcher.

use std::collections::HashMap;

use boswas_services::{Command, Event, MenuEntry, MenuToggle, Pixmap, Service, TrayItem, TrayStatus};
use zbus::{
    blocking::Connection,
    zvariant::{OwnedValue, StructureBuilder, Value},
};

use crate::support::*;

const WATCHER: &str = "org.kde.StatusNotifierWatcher";
const WATCHER_PATH: &str = "/StatusNotifierWatcher";
const ITEM: &str = "org.kde.StatusNotifierItem";
const ITEM_PATH: &str = "/StatusNotifierItem";
const MENU: &str = "com.canonical.dbusmenu";
const MENU_PATH: &str = "/MenuBar";

fn tray(event: &Event) -> Option<&Vec<TrayItem>> {
    match event {
        Event::Tray(items) => Some(items),
        _ => None,
    }
}

fn setup() -> (Harness, boswas_services::Services, Connection) {
    let harness = Harness::new();
    let services = harness.start(only(|config| config.tray = true));
    let client = harness.session.connect();
    wait_for_name(&client, WATCHER);
    (harness, services, client)
}

/// An item with an icon name and a menu.
fn item(harness: &Harness, id: &str, object: &str) -> Peer {
    let peer = Peer::start(&harness.session, &[]);
    peer.add(
        object,
        ITEM,
        props([
            ("Id", owned(id)),
            ("Title", owned(format!("{id} title"))),
            ("Status", owned("Active")),
            ("IconName", owned(format!("{id}-icon"))),
            ("IconThemePath", owned("")),
            ("Menu", path_value(MENU_PATH)),
            ("ItemIsMenu", owned(false)),
        ]),
    );
    peer.accept(ITEM, "Activate");
    peer.accept(ITEM, "SecondaryActivate");
    peer.accept(ITEM, "Scroll");
    peer
}

fn register(peer: &Peer, service: &str) {
    peer.conn.call_method(Some(WATCHER), WATCHER_PATH, Some(WATCHER), "RegisterStatusNotifierItem", &(service,)).unwrap();
}

fn node(id: i32, props: Vec<(&str, Value<'_>)>, children: Vec<OwnedValue>) -> OwnedValue {
    let props: HashMap<String, OwnedValue> = props.into_iter().map(|(name, value)| (name.to_owned(), owned(value))).collect();
    owned(Value::from(StructureBuilder::new().add_field(id).add_field(props).add_field(children).build().unwrap()))
}

fn pixmaps(list: Vec<(i32, i32, Vec<u8>)>) -> OwnedValue {
    owned(Value::from(list))
}

#[test]
fn items_register_by_name_and_by_path_and_are_shown_in_order() {
    let (harness, _services, client) = setup();
    assert!(harness.events.latest("the empty tray", tray, |_| true).is_empty());
    let signals = Signals::listen(&client, WATCHER);

    // The KDE way: a bus name, the item at /StatusNotifierItem.
    let first = item(&harness, "mail", ITEM_PATH);
    register(&first, &first.unique_name());
    // The Ayatana way: an object path on the caller's connection.
    let second = item(&harness, "chat", "/org/ayatana/NotificationItem/chat");
    register(&second, "/org/ayatana/NotificationItem/chat");
    // A well-known name.
    let third = item(&harness, "sync", ITEM_PATH);
    third.claim("org.example.Sync");
    register(&third, "org.example.Sync");

    let items = harness.events.latest("three items", tray, |items| items.len() == 3);
    let keys: Vec<&str> = items.iter().map(|item| item.key.as_str()).collect();
    let expected = [
        format!("{}{ITEM_PATH}", first.unique_name()),
        format!("{}/org/ayatana/NotificationItem/chat", second.unique_name()),
        format!("org.example.Sync{ITEM_PATH}"),
    ];
    assert_eq!(keys, expected);
    assert_eq!(
        items[0],
        TrayItem {
            key: expected[0].clone(),
            id: "mail".into(),
            title: "mail title".into(),
            icon_name: "mail-icon".into(),
            icon_theme_path: String::new(),
            icon_pixmap: None,
            tooltip: "mail title".into(),
            status: TrayStatus::Active,
            has_menu: true,
        }
    );

    // The watcher told everybody, and answers its properties.
    for key in &expected {
        let signal = signals.next("StatusNotifierItemRegistered");
        assert_eq!(&signal.body().deserialize::<String>().unwrap(), key);
    }
    let get = |name: &str| {
        let reply = client.call_method(Some(WATCHER), WATCHER_PATH, Some(PROPERTIES), "Get", &(WATCHER, name)).unwrap();
        reply.body().deserialize::<OwnedValue>().unwrap()
    };
    assert_eq!(<Vec<String>>::try_from(get("RegisteredStatusNotifierItems")).unwrap(), expected);
    assert!(bool::try_from(get("IsStatusNotifierHostRegistered")).unwrap());
    assert_eq!(i32::try_from(get("ProtocolVersion")).unwrap(), 0);
    client.call_method(Some(WATCHER), WATCHER_PATH, Some(WATCHER), "RegisterStatusNotifierHost", &("org.example.Host",)).unwrap();

    // Registering twice does not show it twice.
    register(&first, &first.unique_name());
    // An item that leaves the bus leaves the tray.
    second.vanish();
    let items = harness.events.latest("two items", tray, |items| items.len() == 2);
    assert_eq!(items[0].id, "mail");
    assert_eq!(items[1].id, "sync");
    let signal = signals.next("StatusNotifierItemUnregistered");
    assert_eq!(signal.body().deserialize::<String>().unwrap(), expected[1]);
    // So does one that gives up its name.
    third.release("org.example.Sync");
    harness.events.latest("one item", tray, |items| items.len() == 1 && items[0].id == "mail");
}

#[test]
fn the_host_is_announced_when_the_watcher_starts() {
    let harness = Harness::new();
    let client = harness.session.connect();
    let signals = Signals::listen(&client, WATCHER);
    let _services = harness.start(only(|config| config.tray = true));
    signals.next("StatusNotifierHostRegistered");
}

#[test]
fn items_are_followed_when_they_signal_changes() {
    let (harness, services, _client) = setup();
    let peer = item(&harness, "vol", ITEM_PATH);
    let tooltip = StructureBuilder::new()
        .add_field("")
        .add_field(Vec::<(i32, i32, Vec<u8>)>::new())
        .add_field("")
        .add_field("<b>Volume</b> 40&#37;")
        .build()
        .unwrap();
    peer.set_quietly(ITEM_PATH, ITEM, "ToolTip", owned(Value::from(tooltip)));
    peer.set_quietly(ITEM_PATH, ITEM, "IconThemePath", owned("/opt/vol/icons"));
    register(&peer, &peer.unique_name());
    let shown = harness.events.latest("the item", tray, |items| items.len() == 1);
    assert_eq!(shown[0].tooltip, "Volume 40%");
    assert_eq!(shown[0].icon_theme_path, "/opt/vol/icons");

    peer.set_quietly(ITEM_PATH, ITEM, "IconName", owned("vol-muted"));
    peer.signal(ITEM_PATH, ITEM, "NewIcon", &());
    harness.events.latest("the new icon", tray, |items| items[0].icon_name == "vol-muted");

    peer.set_quietly(ITEM_PATH, ITEM, "Title", owned("Sound"));
    peer.signal(ITEM_PATH, ITEM, "NewTitle", &());
    harness.events.latest("the new title", tray, |items| items[0].title == "Sound");

    peer.set_quietly(ITEM_PATH, ITEM, "Status", owned("Passive"));
    peer.signal(ITEM_PATH, ITEM, "NewStatus", &("Passive",));
    harness.events.latest("the new status", tray, |items| items[0].status == TrayStatus::Passive);

    // Needing attention shows the attention icon.
    peer.set_quietly(ITEM_PATH, ITEM, "AttentionIconName", owned("vol-alert"));
    peer.set_quietly(ITEM_PATH, ITEM, "Status", owned("NeedsAttention"));
    peer.signal(ITEM_PATH, ITEM, "NewAttentionIcon", &());
    peer.signal(ITEM_PATH, ITEM, "NewStatus", &("NeedsAttention",));
    harness.events.latest("the attention icon", tray, |items| {
        items[0].status == TrayStatus::NeedsAttention && items[0].icon_name == "vol-alert"
    });

    peer.set_quietly(ITEM_PATH, ITEM, "ToolTip", owned("gone"));
    peer.signal(ITEM_PATH, ITEM, "NewToolTip", &());
    harness.events.latest("the title as tooltip", tray, |items| items[0].tooltip == "Sound");

    // A change without a signal is seen on Refresh, which also sends the
    // list again.
    peer.set_quietly(ITEM_PATH, ITEM, "Title", owned("Quiet change"));
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Tray));
    harness.events.after(before, "the list again", |event| tray(event).cloned());
    harness.events.latest("the refreshed title", tray, |items| items[0].title == "Quiet change");

    // Somebody else's signal on the same path changes nothing of ours.
    let other = Peer::start(&harness.session, &[]);
    other.signal(ITEM_PATH, ITEM, "NewIcon", &());
}

#[test]
fn pixmaps_are_converted_and_capped() {
    let (harness, _services, _client) = setup();
    // No icon name: the pixmap is used. ARGB, most significant byte first.
    let peer = item(&harness, "px", ITEM_PATH);
    peer.set_quietly(ITEM_PATH, ITEM, "IconName", owned(""));
    let tiny = [0x80u8, 0x11, 0x22, 0x33].repeat(16 * 16);
    let fits = [0xffu8, 0xaa, 0xbb, 0xcc].repeat(32 * 32);
    let broken = vec![0u8; 77];
    peer.set_quietly(ITEM_PATH, ITEM, "IconPixmap", pixmaps(vec![(16, 16, tiny), (64, 64, broken), (32, 32, fits)]));
    register(&peer, &peer.unique_name());
    let shown = harness.events.latest("the item", tray, |items| items.len() == 1);
    let pixmap = shown[0].icon_pixmap.clone().expect("a pixmap");
    assert_eq!((pixmap.width, pixmap.height), (32, 32));
    assert_eq!(pixmap.rgba, [0xaau8, 0xbb, 0xcc, 0xff].repeat(32 * 32));
    assert_eq!(shown[0].icon_name, "");

    // A huge one (4 MiB, too big to decode with the other properties) is
    // still read, and scaled down to 256 x 256.
    let huge = [0xffu8, 0x10, 0x20, 0x30].repeat(1024 * 1024);
    peer.set_quietly(ITEM_PATH, ITEM, "IconPixmap", pixmaps(vec![(1024, 1024, huge)]));
    peer.signal(ITEM_PATH, ITEM, "NewIcon", &());
    let shown = harness.events.latest("the scaled pixmap", tray, |items| {
        items[0].icon_pixmap.as_ref().is_some_and(|pixmap| pixmap.width == 256)
    });
    let pixmap = shown[0].icon_pixmap.clone().unwrap();
    assert_eq!(pixmap, Pixmap { width: 256, height: 256, rgba: [0x10u8, 0x20, 0x30, 0xff].repeat(256 * 256) });
    assert_eq!(shown[0].title, "px title");

    // An impossible one is no icon, not a crash.
    peer.set_quietly(ITEM_PATH, ITEM, "IconPixmap", pixmaps(vec![(5000, 5000, vec![1, 2, 3]), (-1, 4, vec![0; 16])]));
    peer.signal(ITEM_PATH, ITEM, "NewIcon", &());
    harness.events.latest("no pixmap", tray, |items| items[0].icon_pixmap.is_none());

    // With an icon name the pixmap is not sent along.
    peer.set_quietly(ITEM_PATH, ITEM, "IconPixmap", pixmaps(vec![(1, 1, vec![1, 2, 3, 4])]));
    peer.set_quietly(ITEM_PATH, ITEM, "IconName", owned("named"));
    peer.signal(ITEM_PATH, ITEM, "NewIcon", &());
    harness.events.latest("the icon name", tray, |items| items[0].icon_name == "named" && items[0].icon_pixmap.is_none());
}

#[test]
fn clicks_and_scrolling_reach_the_item() {
    let (harness, services, _client) = setup();
    let peer = item(&harness, "app", ITEM_PATH);
    register(&peer, &peer.unique_name());
    let key = harness.events.latest("the item", tray, |items| items.len() == 1)[0].key.clone();

    services.send(Command::TrayActivate { key: key.clone(), x: 100, y: 200 });
    assert_eq!(peer.wait_call("Activate").args::<(i32, i32)>(), (100, 200));
    services.send(Command::TraySecondaryActivate { key: key.clone(), x: 3, y: 4 });
    assert_eq!(peer.wait_call("SecondaryActivate").args::<(i32, i32)>(), (3, 4));
    services.send(Command::TrayScroll { key: key.clone(), delta: -2, horizontal: false });
    assert_eq!(peer.wait_call("Scroll").args::<(i32, String)>(), (-240, "vertical".to_owned()));
    services.send(Command::TrayScroll { key: key.clone(), delta: 1, horizontal: true });
    assert_eq!(peer.wait_call_n("Scroll", 2).args::<(i32, String)>(), (120, "horizontal".to_owned()));
    services.send(Command::TrayScroll { key: key.clone(), delta: i32::MAX, horizontal: true });
    assert_eq!(peer.wait_call_n("Scroll", 3).args::<(i32, String)>().0, i32::MAX);

    // A key that names no item goes nowhere.
    services.send(Command::TrayActivate { key: "org.freedesktop.DBus/".into(), x: 0, y: 0 });
    services.send(Command::TrayMenuActivate { key: ":1.999/StatusNotifierItem".into(), id: 1 });
    services.send(Command::TrayActivate { key, x: 7, y: 7 });
    assert_eq!(peer.wait_call_n("Activate", 2).args::<(i32, i32)>(), (7, 7));
    assert_eq!(peer.calls("Activate").len(), 2);
}

fn menu(peer: &Peer) {
    peer.on(MENU, "AboutToShow", |_, call| call.reply(&(false,)));
    peer.on(MENU, "GetLayout", |peer, call| {
        let (parent, depth, _names): (i32, i32, Vec<String>) = call.args();
        assert_eq!((parent, depth), (0, -1));
        let extra = peer.get("/test", "test", "extra").is_some();
        let mut children = vec![
            node(1, vec![("label", Value::from("_Open"))], vec![]),
            node(2, vec![("type", Value::from("separator"))], vec![]),
            node(
                3,
                vec![("label", Value::from("_Mode")), ("children-display", Value::from("submenu"))],
                vec![
                    node(4, vec![("label", Value::from("Fast")), ("toggle-type", Value::from("radio")), ("toggle-state", Value::from(1i32))], vec![]),
                    node(5, vec![("label", Value::from("Slow")), ("toggle-type", Value::from("radio")), ("toggle-state", Value::from(0i32))], vec![]),
                ],
            ),
            node(6, vec![("label", Value::from("Hidden")), ("visible", Value::from(false))], vec![]),
            node(7, vec![("label", Value::from("_Quit")), ("icon-name", Value::from("application-exit")), ("enabled", Value::from(false))], vec![]),
        ];
        if extra {
            children.push(node(8, vec![("label", Value::from("New entry"))], vec![]));
        }
        let root = (0i32, HashMap::<String, OwnedValue>::new(), children);
        call.reply(&(7u32, root))
    });
    peer.accept(MENU, "Event");
}

fn entry(id: i32, label: &str) -> MenuEntry {
    MenuEntry { id, label: label.into(), enabled: true, separator: false, toggle: MenuToggle::None, icon_name: String::new(), children: vec![] }
}

fn menu_event(events: &Events, from: usize, key: &str) -> Vec<MenuEntry> {
    events.after(from, "the menu", |event| match event {
        Event::TrayMenu { key: of, entries } if of == key => Some(entries.clone()),
        _ => None,
    })
}

#[test]
fn menus_are_fetched_followed_and_clicked() {
    let (harness, services, _client) = setup();
    let peer = item(&harness, "app", ITEM_PATH);
    menu(&peer);
    register(&peer, &peer.unique_name());
    let key = harness.events.latest("the item", tray, |items| items.len() == 1 && items[0].has_menu)[0].key.clone();

    services.send(Command::TrayMenu { key: key.clone() });
    let entries = menu_event(&harness.events, 0, &key);
    assert_eq!(
        entries,
        vec![
            entry(1, "Open"),
            MenuEntry { separator: true, ..entry(2, "") },
            MenuEntry {
                children: vec![
                    MenuEntry { toggle: MenuToggle::Radio(true), ..entry(4, "Fast") },
                    MenuEntry { toggle: MenuToggle::Radio(false), ..entry(5, "Slow") },
                ],
                ..entry(3, "Mode")
            },
            MenuEntry { enabled: false, icon_name: "application-exit".into(), ..entry(7, "Quit") },
        ]
    );
    // The item was told the menu is about to show before it was read, and
    // only the properties the shell shows were asked for.
    let calls: Vec<String> = peer.all_calls().into_iter().filter(|call| call.interface == MENU).map(|call| call.member).collect();
    assert_eq!(calls, ["AboutToShow", "GetLayout"]);
    let (_, _, names): (i32, i32, Vec<String>) = peer.wait_call("GetLayout").args();
    assert!(names.contains(&"label".to_owned()) && !names.contains(&"icon-data".to_owned()));

    services.send(Command::TrayMenuActivate { key: key.clone(), id: 4 });
    let click = peer.wait_call("Event");
    assert_eq!(click.path, MENU_PATH);
    let (id, kind, _data, _time): (i32, String, OwnedValue, u32) = click.args();
    assert_eq!((id, kind.as_str()), (4, "clicked"));

    // The menu changes while it is open: the shell gets the new one.
    let before = harness.events.len();
    peer.set_quietly("/test", "test", "extra", owned(true));
    peer.signal(MENU_PATH, MENU, "LayoutUpdated", &(8u32, 0i32));
    let entries = menu_event(&harness.events, before, &key);
    assert_eq!(entries.last().unwrap(), &entry(8, "New entry"));
}

#[test]
fn an_item_without_activate_or_marked_as_menu_answers_a_click_with_its_menu() {
    let (harness, services, _client) = setup();
    // libappindicator items have no Activate method.
    let indicator = Peer::start(&harness.session, &[]);
    indicator.add(
        "/org/ayatana/NotificationItem/ind",
        ITEM,
        props([("Id", owned("ind")), ("IconName", owned("ind")), ("Menu", path_value(MENU_PATH))]),
    );
    menu(&indicator);
    register(&indicator, "/org/ayatana/NotificationItem/ind");
    let key = harness.events.latest("the indicator", tray, |items| items.len() == 1 && items[0].has_menu)[0].key.clone();
    services.send(Command::TrayActivate { key: key.clone(), x: 1, y: 2 });
    indicator.wait_call("Activate");
    assert_eq!(menu_event(&harness.events, 0, &key).len(), 4);

    // An item that says it is a menu is not sent Activate at all.
    let menu_item = item(&harness, "menu-only", ITEM_PATH);
    menu_item.set_quietly(ITEM_PATH, ITEM, "ItemIsMenu", owned(true));
    menu(&menu_item);
    register(&menu_item, &menu_item.unique_name());
    let key = harness.events.latest("both items", tray, |items| items.len() == 2)[1].key.clone();
    services.send(Command::TrayActivate { key: key.clone(), x: 1, y: 2 });
    assert_eq!(menu_event(&harness.events, 0, &key).len(), 4);
    assert!(menu_item.calls("Activate").is_empty());

    // An item without a menu answers a menu request with no entries.
    let bare = item(&harness, "bare", ITEM_PATH);
    bare.set_quietly(ITEM_PATH, ITEM, "Menu", path_value("/NO_DBUSMENU"));
    register(&bare, &bare.unique_name());
    let key = harness.events.latest("three items", tray, |items| items.len() == 3 && !items[2].has_menu)[2].key.clone();
    services.send(Command::TrayMenu { key: key.clone() });
    assert!(menu_event(&harness.events, 0, &key).is_empty());
}

#[test]
fn hostile_items_do_not_hurt_the_tray() {
    let (harness, services, client) = setup();
    // Nonsense registrations are refused.
    for bad in ["", "not a name", "/not a path", &"x".repeat(5000)] {
        let result = client.call_method(Some(WATCHER), WATCHER_PATH, Some(WATCHER), "RegisterStatusNotifierItem", &(bad,));
        assert!(result.is_err(), "{bad:?} was accepted");
    }
    assert!(client.call_method(Some(WATCHER), WATCHER_PATH, Some(WATCHER), "RegisterStatusNotifierItem", &(7u32,)).is_err());
    // A name nobody owns is no item.
    client.call_method(Some(WATCHER), WATCHER_PATH, Some(WATCHER), "RegisterStatusNotifierItem", &("org.example.Nobody",)).unwrap();

    // An item that never answers.
    let silent = Peer::start(&harness.session, &[]);
    silent.on("", "GetAll", |_, _| Reply::Silence);
    silent.on("", "Get", |_, _| Reply::Silence);
    register(&silent, &silent.unique_name());
    // An item whose properties are all of the wrong type, with a menu
    // that answers garbage.
    let liar = Peer::start(&harness.session, &[]);
    liar.add(
        ITEM_PATH,
        ITEM,
        props([
            ("Id", owned(1u32)),
            ("Title", owned(vec![1u8, 2, 3])),
            ("Status", owned(false)),
            ("IconName", owned(-5i32)),
            ("IconPixmap", owned("a picture, honest")),
            ("ToolTip", owned(9.5f64)),
            ("Menu", path_value(MENU_PATH)),
            ("ItemIsMenu", owned("maybe")),
        ]),
    );
    liar.on(MENU, "GetLayout", |_, call| call.reply(&("no layout", 3u8)));
    register(&liar, &liar.unique_name());
    // An item with text that must not reach the shell as it is.
    let loud = item(&harness, "loud", ITEM_PATH);
    loud.set_quietly(ITEM_PATH, ITEM, "Title", owned(format!("a\u{1b}[2J\u{202e}b{}", "x".repeat(100_000))));
    register(&loud, &loud.unique_name());
    // An honest one, registered last.
    let honest = item(&harness, "honest", ITEM_PATH);
    register(&honest, &honest.unique_name());

    let items = harness.events.latest("the items that answered", tray, |items| items.iter().any(|item| item.id == "honest") && items.len() == 3);
    assert_eq!(items[0].key, format!("{}{ITEM_PATH}", liar.unique_name()));
    assert_eq!((items[0].id.as_str(), items[0].title.as_str(), items[0].icon_name.as_str()), ("", "", ""));
    assert_eq!(items[0].status, TrayStatus::Active);
    assert!(items[0].icon_pixmap.is_none());
    assert!(items[1].title.starts_with("a[2Jb"));
    assert_eq!(items[1].title.chars().count(), 256);

    // The liar's menu is empty rather than an error; the silent item's
    // click goes nowhere; the tray keeps working.
    let liar_key = items[0].key.clone();
    services.send(Command::TrayMenu { key: liar_key.clone() });
    assert!(menu_event(&harness.events, 0, &liar_key).is_empty());
    services.send(Command::TrayActivate { key: format!("{}{ITEM_PATH}", silent.unique_name()), x: 0, y: 0 });
    services.send(Command::TrayActivate { key: items[2].key.clone(), x: 5, y: 6 });
    honest.wait_call("Activate");

    // A flood of change signals does not flood the shell.
    let before = harness.events.len();
    for _ in 0..2000 {
        loud.signal(ITEM_PATH, ITEM, "NewIcon", &());
    }
    loud.set_quietly(ITEM_PATH, ITEM, "Title", owned("calm"));
    loud.signal(ITEM_PATH, ITEM, "NewTitle", &());
    harness.events.latest("the title after the flood", tray, |items| items.iter().any(|item| item.title == "calm"));
    assert!(harness.events.len() - before < 20, "the flood reached the shell");

    // The silent one vanishes in the middle of being asked.
    silent.vanish();
    liar.vanish();
    harness.events.latest("the two that stayed", tray, |items| items.len() == 2);
}

#[test]
fn the_tray_takes_no_more_items_than_it_can_show() {
    let (harness, _services, _client) = setup();
    let peer = Peer::start(&harness.session, &[]);
    for index in 0..80 {
        let object = format!("/items/i{index}");
        peer.add(&object, ITEM, props([("Id", owned(format!("i{index}"))), ("IconName", owned("x"))]));
        register(&peer, &object);
    }
    let marker = item(&harness, "marker", ITEM_PATH);
    register(&marker, &marker.unique_name());
    let items = harness.events.latest("a full tray", tray, |items| items.len() == 64);
    assert_eq!(items[63].id, "i63");
    // All of one connection's items go when it goes.
    peer.vanish();
    let second = item(&harness, "second", ITEM_PATH);
    eventually("room for another item", || {
        register(&second, &second.unique_name());
        harness.events.all().iter().rev().find_map(tray).is_some_and(|items| items.len() == 1 && items[0].id == "second")
    });
}

#[test]
fn another_watcher_is_left_alone_until_it_leaves() {
    let harness = Harness::new();
    let other = Peer::start(&harness.session, &[WATCHER]);
    other.accept(WATCHER, "RegisterStatusNotifierItem");
    let _services = harness.start(only(|config| config.tray = true));
    assert!(harness.events.latest("the empty tray", tray, |_| true).is_empty());
    let peer = item(&harness, "early", ITEM_PATH);
    register(&peer, &peer.unique_name());
    other.wait_call("RegisterStatusNotifierItem");

    let previous = other.unique_name();
    other.vanish();
    let client = harness.session.connect();
    eventually("our watcher to take the name", || wait_for_name(&client, WATCHER) != previous);
    register(&peer, &peer.unique_name());
    harness.events.latest("the item", tray, |items| items.len() == 1);
}
