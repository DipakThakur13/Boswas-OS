//! MPRIS media players on the session bus (`org.mpris.MediaPlayer2.*`).

use std::time::{Duration, Instant};

use async_channel::{Receiver, Sender};
use async_executor::Task;
use futures_lite::future;
use zbus::Connection;

use crate::{
    runtime::{
        call, call_raw, earliest, get_all, idle, name_owner, owner_change, signal_rule, subscribe, wake, Backoff, Bus, BusKind,
        CallResult, Ctx, Exec, IdleEnd, Latest, Service, Wake, CALL_TIMEOUT, DBUS, DBUS_PATH, MIN_INTERVAL, PROPERTIES,
        SETTLE,
    },
    var::{as_dict, as_list, as_str, file_uri_path, label, prop, prop_bool, prop_str, Props},
    Command, Event, MediaPlayer, Service as Which,
};

const PREFIX: &str = "org.mpris.MediaPlayer2.";
const NAMESPACE: &str = "org.mpris.MediaPlayer2";
const PATH: &str = "/org/mpris/MediaPlayer2";
const ROOT: &str = "org.mpris.MediaPlayer2";
const PLAYER: &str = "org.mpris.MediaPlayer2.Player";

/// More players than this are not followed; a session has a handful.
const MAX_PLAYERS: usize = 32;

pub(crate) struct Media {
    ctx: Ctx,
    ex: Exec,
    state: Latest<Vec<MediaPlayer>>,
    players: Vec<Player>,
    /// Counts up each time a player becomes the active one.
    activity: u64,
    /// Calls to players that are on their way.
    calls: Vec<Task<()>>,
}

struct Player {
    /// The well-known name.
    name: String,
    /// The connection that owns it; signals carry this name.
    owner: String,
    shown: Option<MediaPlayer>,
    /// When it last started playing or changed track; higher is later.
    active: u64,
    /// When to read it again; `None` when it is up to date.
    due: Option<Instant>,
    last_read: Option<Instant>,
    /// The read that is on its way. A player is another program: it is
    /// never waited for in the service's loop.
    reading: Option<Task<()>>,
}

enum Msg {
    /// A player name appeared, moved or went away.
    Owner { name: String, owner: String },
    /// A player's properties changed (by its unique name).
    Changed { owner: String },
    /// A read of a player finished; `player` is `None` when it did not
    /// answer as a player.
    Read { name: String, owner: String, root: Props, player: Option<Props> },
    Lost,
}

impl Service for Media {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self {
            ctx,
            ex,
            state: Latest::default(),
            players: Vec::new(),
            activity: 0,
            calls: Vec::new(),
        }
    }

    async fn run(&mut self, commands: Receiver<Command>) {
        let mut backoff = Backoff::new(Duration::from_secs(30));
        loop {
            match Bus::connect(BusKind::Session, &self.ctx.env, &self.ex).await {
                Ok(bus) => {
                    backoff.reset();
                    match self.serve(&bus, &commands).await {
                        Ok(true) => return,
                        Ok(false) => {}
                        Err(error) => tracing::debug!(%error, "media: the session bus stopped answering"),
                    }
                }
                Err(error) => tracing::debug!(%error, "media: no session bus"),
            }
            self.players.clear();
            self.calls.clear();
            self.publish(false);
            match idle(&commands, backoff.next(), |command| matches!(command, Command::Refresh(Which::Media))).await {
                IdleEnd::Closed => return,
                IdleEnd::Retry => self.publish(true),
                IdleEnd::Elapsed => {}
            }
        }
    }
}

impl Media {
    fn publish(&mut self, force: bool) {
        let mut order: Vec<&Player> = self.players.iter().filter(|player| player.shown.is_some()).collect();
        // Most recently active first; players that never played keep the
        // order they appeared in.
        order.sort_by(|a, b| b.active.cmp(&a.active));
        let list: Vec<MediaPlayer> = order.into_iter().filter_map(|player| player.shown.clone()).collect();
        if let Some(list) = self.state.update(list, force) {
            self.ctx.emit(Event::Media(list));
        }
    }

    async fn serve(&mut self, bus: &Bus, commands: &Receiver<Command>) -> CallResult<bool> {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        let conn = &bus.conn;
        let _subscriptions = self.subscribe(conn, &inbox).await?;

        let names: Vec<String> = call(conn, DBUS, DBUS_PATH, DBUS, "ListNames", &()).await?;
        for name in names.into_iter().filter(|name| name.starts_with(PREFIX)) {
            if let Some(owner) = name_owner(conn, &name).await {
                self.owner_changed(name, owner);
            }
        }
        self.publish(true);

        loop {
            let deadline = self
                .players
                .iter()
                .filter(|player| player.reading.is_none())
                .fold(None, |soonest, player| earliest(soonest, player.due));
            match wake(commands, &messages, deadline).await {
                Wake::Closed => return Ok(true),
                Wake::Message(Msg::Lost) => return Ok(false),
                Wake::Message(Msg::Owner { name, owner }) => {
                    self.owner_changed(name, owner);
                    self.publish(false);
                }
                Wake::Message(Msg::Changed { owner }) => {
                    for player in self.players.iter_mut().filter(|player| player.owner == owner) {
                        player.schedule();
                    }
                }
                Wake::Message(Msg::Read { name, owner, root, player }) => {
                    self.read_done(&name, &owner, root, player);
                    self.publish(false);
                }
                Wake::Command(Command::Refresh(Which::Media)) => {
                    let now = Instant::now();
                    for player in &mut self.players {
                        player.due = Some(now);
                    }
                    self.publish(true);
                }
                Wake::Command(Command::MediaPlayPause { key }) => self.control(conn, &key, "PlayPause"),
                Wake::Command(Command::MediaNext { key }) => self.control(conn, &key, "Next"),
                Wake::Command(Command::MediaPrevious { key }) => self.control(conn, &key, "Previous"),
                Wake::Command(_) => {}
                Wake::Timer => {
                    let now = Instant::now();
                    for player in &mut self.players {
                        if player.reading.is_none() && player.due.is_some_and(|due| due <= now) {
                            player.due = None;
                            player.last_read = Some(now);
                            player.reading = Some(self.ex.spawn(read(conn.clone(), player.name.clone(), player.owner.clone(), inbox.clone())));
                        }
                    }
                }
            }
        }
    }

    async fn subscribe(&self, conn: &Connection, inbox: &Sender<Msg>) -> CallResult<Vec<Task<()>>> {
        let owners = signal_rule(Some(DBUS), DBUS, Some("NameOwnerChanged"), Some(DBUS_PATH))?.arg0ns(NAMESPACE)?.build();
        let owners = subscribe(
            conn,
            &self.ex,
            owners,
            inbox,
            |message| {
                let (name, _, owner) = owner_change(message)?;
                name.starts_with(PREFIX).then_some(Msg::Owner { name, owner })
            },
            || Msg::Lost,
        )
        .await?;
        // Any sender: which player a signal is from is decided by the
        // unique name the bus stamped on it.
        let changes = signal_rule(None, PROPERTIES, Some("PropertiesChanged"), Some(PATH))?.build();
        let changes = subscribe(
            conn,
            &self.ex,
            changes,
            inbox,
            |message| {
                let owner = message.header().sender()?.to_string();
                Some(Msg::Changed { owner })
            },
            || Msg::Lost,
        )
        .await?;
        Ok(vec![owners, changes])
    }

    fn owner_changed(&mut self, name: String, owner: String) {
        if !plausible_player(&name) {
            return;
        }
        let position = self.players.iter().position(|player| player.name == name);
        if owner.is_empty() {
            if let Some(index) = position {
                self.players.remove(index);
            }
            return;
        }
        match position {
            Some(index) => {
                let player = &mut self.players[index];
                player.owner = owner;
                player.reading = None;
                player.due = Some(Instant::now());
            }
            None if self.players.len() < MAX_PLAYERS => self.players.push(Player {
                name,
                owner,
                shown: None,
                active: 0,
                due: Some(Instant::now()),
                last_read: None,
                reading: None,
            }),
            None => tracing::debug!("media: too many players, one more is ignored"),
        }
    }

    fn read_done(&mut self, name: &str, owner: &str, root: Props, player: Option<Props>) {
        // The name may have moved to another connection while the read
        // was on its way; that owner is read on its own.
        let Some(entry) = self.players.iter_mut().find(|entry| entry.name == name && entry.owner == owner) else {
            return;
        };
        entry.reading = None;
        let Some(player) = player else {
            // It does not answer as a player: not shown, and read again
            // when it says something changed.
            entry.shown = None;
            return;
        };
        let new = describe(name, &root, &player);
        let became_active = match &entry.shown {
            Some(old) => new.playing && (!old.playing || old.title != new.title),
            None => new.playing,
        };
        if became_active {
            self.activity += 1;
            entry.active = self.activity;
        }
        entry.shown = Some(new);
    }

    /// Sends a transport command to a player this service knows; a key
    /// that names anything else is ignored.
    fn control(&mut self, conn: &Connection, key: &str, method: &'static str) {
        let Some(player) = self.players.iter().find(|player| player.name == key && player.shown.is_some()) else {
            return;
        };
        self.calls.retain(|task| !task.is_finished());
        let conn = conn.clone();
        let name = player.name.clone();
        self.calls.push(self.ex.spawn(async move {
            if let Err(error) = call_raw(&conn, CALL_TIMEOUT, &name, PATH, PLAYER, method, &()).await {
                tracing::debug!(player = %name, method, %error, "media: the player refused");
            }
        }));
    }
}

impl Player {
    fn schedule(&mut self) {
        if self.due.is_none() {
            let mut due = Instant::now() + SETTLE;
            if let Some(last) = self.last_read {
                due = due.max(last + MIN_INTERVAL);
            }
            self.due = Some(due);
        }
    }
}

/// Reads both interfaces of a player and puts the result in the inbox.
async fn read(conn: Connection, name: String, owner: String, inbox: Sender<Msg>) {
    let (root, player) = future::zip(get_all(&conn, &name, PATH, ROOT), get_all(&conn, &name, PATH, PLAYER)).await;
    let _ = inbox
        .send(Msg::Read {
            name,
            owner,
            root: root.unwrap_or_default(),
            player: player.ok(),
        })
        .await;
}

/// `org.mpris.MediaPlayer2.` followed by the player's own part.
fn plausible_player(name: &str) -> bool {
    name.len() <= 255 && name.strip_prefix(PREFIX).is_some_and(|rest| !rest.is_empty())
}

fn describe(name: &str, root: &Props, player: &Props) -> MediaPlayer {
    let mut title = String::new();
    let mut artist = String::new();
    let mut art_path = None;
    if let Some(metadata) = prop(player, "Metadata") {
        for (key, value) in as_dict(metadata) {
            match key {
                "xesam:title" => title = as_str(value).map(label).unwrap_or_default(),
                "xesam:artist" => {
                    // A list of names; some players send a single string.
                    let names: Vec<String> = match as_list(value) {
                        Some(list) => list.iter().filter_map(as_str).map(label).filter(|name| !name.is_empty()).take(8).collect(),
                        None => as_str(value).map(label).into_iter().collect(),
                    };
                    artist = names.join(", ");
                }
                "mpris:artUrl" => art_path = as_str(value).and_then(file_uri_path),
                _ => {}
            }
        }
    }
    let identity = prop_str(root, "Identity").map(|text| label(&text)).filter(|text| !text.is_empty());
    let fallback = name.strip_prefix(PREFIX).unwrap_or(name);
    let can_control = prop_bool(player, "CanControl").unwrap_or(true);
    let can = |property: &str| can_control && prop_bool(player, property).unwrap_or(false);
    MediaPlayer {
        key: name.to_owned(),
        identity: identity.unwrap_or_else(|| label(fallback)),
        desktop_entry: prop_str(root, "DesktopEntry").map(|text| label(&text)).filter(|text| !text.is_empty()),
        title,
        artist,
        art_path,
        playing: prop_str(player, "PlaybackStatus").as_deref() == Some("Playing"),
        can_play_pause: can("CanPause") || can("CanPlay"),
        can_next: can("CanGoNext"),
        can_previous: can("CanGoPrevious"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use zbus::zvariant::{OwnedValue, Value};

    fn owned(value: Value<'_>) -> OwnedValue {
        value.try_to_owned().unwrap()
    }

    #[test]
    fn describes_a_player() {
        let mut metadata: HashMap<&str, Value<'_>> = HashMap::new();
        metadata.insert("xesam:title", Value::from("Blue in Green"));
        metadata.insert("xesam:artist", Value::from(vec!["Miles Davis", "Bill Evans"]));
        metadata.insert("mpris:artUrl", Value::from("file:///tmp/cover%201.png"));
        let mut player = Props::new();
        player.insert("Metadata".into(), owned(Value::from(metadata)));
        player.insert("PlaybackStatus".into(), owned(Value::from("Playing")));
        player.insert("CanGoNext".into(), owned(Value::from(true)));
        player.insert("CanPause".into(), owned(Value::from(true)));
        let mut root = Props::new();
        root.insert("Identity".into(), owned(Value::from("Music")));
        root.insert("DesktopEntry".into(), owned(Value::from("org.example.Music")));
        let shown = describe("org.mpris.MediaPlayer2.music", &root, &player);
        assert_eq!(shown.identity, "Music");
        assert_eq!(shown.desktop_entry.as_deref(), Some("org.example.Music"));
        assert_eq!(shown.title, "Blue in Green");
        assert_eq!(shown.artist, "Miles Davis, Bill Evans");
        assert_eq!(shown.art_path.as_deref(), Some("/tmp/cover 1.png"));
        assert!(shown.playing && shown.can_play_pause && shown.can_next && !shown.can_previous);
    }

    #[test]
    fn remote_art_and_wrong_types_are_dropped() {
        let mut metadata: HashMap<&str, Value<'_>> = HashMap::new();
        metadata.insert("xesam:title", Value::from(42u32));
        metadata.insert("xesam:artist", Value::from("Solo"));
        metadata.insert("mpris:artUrl", Value::from("https://example.org/cover.png"));
        let mut player = Props::new();
        player.insert("Metadata".into(), owned(Value::from(metadata)));
        player.insert("PlaybackStatus".into(), owned(Value::from(3u32)));
        player.insert("CanControl".into(), owned(Value::from(false)));
        player.insert("CanPause".into(), owned(Value::from(true)));
        let shown = describe("org.mpris.MediaPlayer2.x", &Props::new(), &player);
        assert_eq!(shown.identity, "x");
        assert_eq!(shown.title, "");
        assert_eq!(shown.artist, "Solo");
        assert_eq!(shown.art_path, None);
        assert!(!shown.playing && !shown.can_play_pause);
    }

    #[test]
    fn player_names() {
        assert!(plausible_player("org.mpris.MediaPlayer2.vlc"));
        assert!(!plausible_player("org.mpris.MediaPlayer2."));
        assert!(!plausible_player("org.example.Other"));
    }
}
