//! MPRIS players.

use std::collections::HashMap;

use boswas_services::{Command, Event, MediaPlayer, Service};
use zbus::zvariant::Value;

use crate::support::*;

const PATH: &str = "/org/mpris/MediaPlayer2";
const ROOT: &str = "org.mpris.MediaPlayer2";
const PLAYER: &str = "org.mpris.MediaPlayer2.Player";

fn media(event: &Event) -> Option<&Vec<MediaPlayer>> {
    match event {
        Event::Media(players) => Some(players),
        _ => None,
    }
}

fn metadata(title: &str, artists: &[&str], art: &str) -> zbus::zvariant::OwnedValue {
    let mut map: HashMap<&str, Value<'_>> = HashMap::new();
    map.insert("xesam:title", Value::from(title));
    map.insert("xesam:artist", Value::from(artists.to_vec()));
    if !art.is_empty() {
        map.insert("mpris:artUrl", Value::from(art));
    }
    owned(Value::from(map))
}

fn player(harness: &Harness, name: &str, status: &str, title: &str) -> Peer {
    let peer = Peer::start(&harness.session, &[]);
    peer.add(PATH, ROOT, props([("Identity", owned(format!("{name} player"))), ("DesktopEntry", owned(format!("org.example.{name}")))]));
    peer.add(
        PATH,
        PLAYER,
        props([
            ("PlaybackStatus", owned(status)),
            ("Metadata", metadata(title, &["Ada", "Grace"], "file:///tmp/cover%20art.png")),
            ("CanGoNext", owned(true)),
            ("CanGoPrevious", owned(false)),
            ("CanPause", owned(true)),
            ("CanPlay", owned(true)),
            ("CanControl", owned(true)),
        ]),
    );
    for method in ["PlayPause", "Next", "Previous"] {
        peer.accept(PLAYER, method);
    }
    peer.claim(&format!("org.mpris.MediaPlayer2.{name}"));
    peer
}

#[test]
fn players_are_listed_followed_and_controlled() {
    let harness = Harness::new();
    // One player runs before the services start, one starts later.
    let radio = player(&harness, "radio", "Paused", "News");
    let services = harness.start(only(|config| config.media = true));
    let players = harness.events.latest("the first player", media, |players| players.len() == 1);
    assert_eq!(
        players[0],
        MediaPlayer {
            key: "org.mpris.MediaPlayer2.radio".into(),
            identity: "radio player".into(),
            desktop_entry: Some("org.example.radio".into()),
            title: "News".into(),
            artist: "Ada, Grace".into(),
            art_path: Some("/tmp/cover art.png".into()),
            playing: false,
            can_play_pause: true,
            can_next: true,
            can_previous: false,
        }
    );

    let music = player(&harness, "music", "Playing", "Track 1");
    let players = harness.events.latest("both players", media, |players| players.len() == 2);
    // The one that plays is the most recently active.
    assert_eq!(players[0].key, "org.mpris.MediaPlayer2.music");
    assert!(players[0].playing);

    // The radio starts playing: it moves to the front.
    radio.set(PATH, PLAYER, "PlaybackStatus", owned("Playing"));
    harness.events.latest("the radio first", media, |players| players.len() == 2 && players[0].key.ends_with("radio") && players[0].playing);
    // A new track on the other one moves that one to the front.
    music.set(PATH, PLAYER, "Metadata", metadata("Track 2", &["Linus"], "https://example.org/remote.png"));
    let players = harness.events.latest("the music first", media, |players| players[0].title == "Track 2");
    assert_eq!(players[0].artist, "Linus");
    assert_eq!(players[0].art_path, None, "only local files are passed on as cover art");
    // Pausing does not reorder.
    music.set(PATH, PLAYER, "PlaybackStatus", owned("Paused"));
    harness.events.latest("paused, still first", media, |players| !players[0].playing && players[0].key.ends_with("music"));

    services.send(Command::MediaPlayPause { key: "org.mpris.MediaPlayer2.music".into() });
    assert_eq!(music.wait_call("PlayPause").path, PATH);
    services.send(Command::MediaNext { key: "org.mpris.MediaPlayer2.radio".into() });
    radio.wait_call("Next");
    services.send(Command::MediaPrevious { key: "org.mpris.MediaPlayer2.radio".into() });
    radio.wait_call("Previous");
    // A key that is no player the service knows is not called.
    let bystander = Peer::start(&harness.session, &["org.example.Bystander"]);
    services.send(Command::MediaPlayPause { key: "org.example.Bystander".into() });
    services.send(Command::MediaNext { key: "org.mpris.MediaPlayer2.nobody".into() });
    services.send(Command::MediaNext { key: "org.mpris.MediaPlayer2.music".into() });
    music.wait_call("Next");
    assert!(bystander.all_calls().is_empty());

    // Refresh sends the list again.
    let before = harness.events.len();
    services.send(Command::Refresh(Service::Media));
    assert_eq!(harness.events.after(before, "the list again", |event| media(event).cloned()).len(), 2);

    // A player that quits leaves the list.
    radio.vanish();
    let players = harness.events.latest("one player", media, |players| players.len() == 1);
    assert_eq!(players[0].key, "org.mpris.MediaPlayer2.music");
    music.release("org.mpris.MediaPlayer2.music");
    harness.events.latest("no players", media, |players| players.is_empty());
}

#[test]
fn without_players_the_list_is_empty() {
    let harness = Harness::new();
    let _other = Peer::start(&harness.session, &["org.example.NotAPlayer", "org.mpris.MediaPlayer2Imposter"]);
    let _services = harness.start(only(|config| config.media = true));
    assert!(harness.events.latest("the empty list", media, |_| true).is_empty());
}

#[test]
fn hostile_players_do_not_hurt_the_list() {
    let harness = Harness::new();
    let services = harness.start(only(|config| config.media = true));
    harness.events.latest("the empty list", media, |players| players.is_empty());

    // Owns a player name but answers nothing.
    let silent = Peer::start(&harness.session, &[]);
    silent.on("", "GetAll", |_, _| Reply::Silence);
    silent.claim("org.mpris.MediaPlayer2.silent");
    // Answers with the wrong types and enormous text.
    let liar = Peer::start(&harness.session, &[]);
    liar.add(PATH, ROOT, props([("Identity", owned(77u32)), ("DesktopEntry", owned(false))]));
    let mut map: HashMap<&str, Value<'_>> = HashMap::new();
    let long = "t".repeat(200_000);
    map.insert("xesam:title", Value::from(long.as_str()));
    map.insert("xesam:artist", Value::from(5u8));
    map.insert("mpris:artUrl", Value::from("file://evil.example/etc/passwd"));
    liar.add(
        PATH,
        PLAYER,
        props([("PlaybackStatus", owned(1u32)), ("Metadata", owned(Value::from(map))), ("CanGoNext", owned("yes")), ("CanPause", owned(1i32))]),
    );
    liar.claim("org.mpris.MediaPlayer2.liar");
    let honest = player(&harness, "honest", "Playing", "Real");

    let players = harness.events.latest("the players that answered", media, |players| {
        players.len() == 2 && players.iter().any(|player| player.key.ends_with("honest"))
    });
    let liar_shown = players.iter().find(|player| player.key.ends_with("liar")).unwrap();
    assert_eq!(liar_shown.identity, "liar");
    assert_eq!(liar_shown.desktop_entry, None);
    assert_eq!(liar_shown.title.chars().count(), 256);
    assert_eq!(liar_shown.artist, "");
    assert_eq!(liar_shown.art_path, None);
    assert!(!liar_shown.playing && !liar_shown.can_next && !liar_shown.can_play_pause);

    // A flood of change signals from the liar is coalesced.
    let before = harness.events.len();
    for _ in 0..2000 {
        liar.signal(PATH, PROPERTIES, "PropertiesChanged", &(PLAYER, HashMap::<&str, Value<'_>>::new(), Vec::<&str>::new()));
    }
    honest.set(PATH, PLAYER, "PlaybackStatus", owned("Paused"));
    harness.events.latest("the honest change", media, |players| players.iter().any(|player| player.key.ends_with("honest") && !player.playing));
    assert!(harness.events.len() - before < 20);

    // Commands for the silent one go nowhere and block nothing.
    services.send(Command::MediaPlayPause { key: "org.mpris.MediaPlayer2.silent".into() });
    services.send(Command::MediaPlayPause { key: "org.mpris.MediaPlayer2.honest".into() });
    honest.wait_call("PlayPause");
    silent.vanish();
    liar.vanish();
    harness.events.latest("only the honest one", media, |players| players.len() == 1);
}
