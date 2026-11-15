//! The notification server: `org.freedesktop.Notifications` on the session
//! bus (Desktop Notifications Specification 1.2).
//!
//! Applications are not trusted: sizes are capped, markup is removed, and
//! only the hints the shell shows are read.

use std::{
    collections::{BTreeMap, HashMap},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

use async_channel::{Receiver, Sender};
use zbus::{
    fdo::{self, RequestNameFlags},
    object_server::SignalEmitter,
    zvariant::{OwnedValue, Value},
    Connection,
};

use crate::{
    runtime::{idle, owner_change, owner_rule, subscribe, unix_time, wake, Backoff, Bus, BusKind, CallResult, Ctx, Exec, IdleEnd, Service, Wake, PROPERTIES},
    text::strip_markup,
    var::{as_bool, as_i64, as_str, clean, file_uri_path},
    Command, Event, Notification, Urgency,
};

pub(crate) const NAME: &str = "org.freedesktop.Notifications";
pub(crate) const PATH: &str = "/org/freedesktop/Notifications";

// What is refused outright (bytes, as sent).
const MAX_APP_NAME: usize = 1024;
const MAX_ICON: usize = 4096;
const MAX_SUMMARY: usize = 4096;
const MAX_BODY: usize = 64 * 1024;
const MAX_ACTION_STRINGS: usize = 64;
const MAX_ACTION_TEXT: usize = 1024;
const MAX_HINTS: usize = 64;

// What is shown (characters, after cleaning).
const SHOWN_APP_NAME: usize = 128;
const SHOWN_SUMMARY: usize = 256;
const SHOWN_BODY: usize = 4096;
const SHOWN_ACTION: usize = 128;
const SHOWN_ACTIONS: usize = 16;

/// Notifications kept open at once. Beyond it the oldest are closed, so an
/// application that never closes what it sends cannot grow the list
/// without bound.
const MAX_LIVE: usize = 500;

// Reasons of the NotificationClosed signal.
const EXPIRED: u32 = 1;
const DISMISSED: u32 = 2;
const CLOSED: u32 = 3;

pub(crate) struct Notifications {
    ctx: Ctx,
    ex: Exec,
    /// The id the next new notification gets. Ids start at 1 and are not
    /// used twice.
    next_id: u32,
    live: BTreeMap<u32, Live>,
    inhibited: Arc<AtomicBool>,
}

/// A notification that has not been closed.
struct Live {
    expires: Option<Instant>,
    /// It stays open when one of its actions is invoked.
    resident: bool,
    critical: bool,
    action_keys: Vec<String>,
}

/// The arguments of `Notify`, as sent.
struct Request {
    app_name: String,
    replaces_id: u32,
    app_icon: String,
    summary: String,
    body: String,
    actions: Vec<String>,
    hints: HashMap<String, OwnedValue>,
    expire_timeout: i32,
}

enum Msg {
    Notify { request: Box<Request>, reply: Sender<u32> },
    Close { id: u32 },
    /// The owner of the notification name changed.
    Owner { owner: String },
    Lost,
}

/// The object on the bus. It only checks sizes and hands over to the
/// service's loop, which owns the state.
struct Server {
    inbox: Sender<Msg>,
    inhibited: Arc<AtomicBool>,
}

#[zbus::interface(name = "org.freedesktop.Notifications")]
impl Server {
    #[allow(clippy::too_many_arguments)]
    async fn notify(
        &self,
        app_name: String,
        replaces_id: u32,
        app_icon: String,
        summary: String,
        body: String,
        actions: Vec<String>,
        hints: HashMap<String, OwnedValue>,
        expire_timeout: i32,
    ) -> fdo::Result<u32> {
        let too_big = |what: &str| Err(fdo::Error::LimitsExceeded(format!("the notification's {what} is too large")));
        if app_name.len() > MAX_APP_NAME {
            return too_big("application name");
        }
        if app_icon.len() > MAX_ICON {
            return too_big("icon");
        }
        if summary.len() > MAX_SUMMARY {
            return too_big("summary");
        }
        if body.len() > MAX_BODY {
            return too_big("body");
        }
        if actions.len() > MAX_ACTION_STRINGS || actions.iter().any(|text| text.len() > MAX_ACTION_TEXT) {
            return too_big("list of actions");
        }
        if hints.len() > MAX_HINTS {
            return too_big("list of hints");
        }
        let stopping = || fdo::Error::Failed("the notification service is stopping".into());
        let (reply, answer) = async_channel::bounded(1);
        let request = Box::new(Request { app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout });
        self.inbox.send(Msg::Notify { request, reply }).await.map_err(|_| stopping())?;
        match answer.recv().await {
            Ok(0) | Err(_) => Err(stopping()),
            Ok(id) => Ok(id),
        }
    }

    async fn close_notification(&self, id: u32) {
        let _ = self.inbox.send(Msg::Close { id }).await;
    }

    async fn get_capabilities(&self) -> Vec<String> {
        // No "body-markup": markup is accepted and removed, not shown.
        ["actions", "body", "icon-static", "persistence"].map(str::to_owned).into()
    }

    #[zbus(out_args("name", "vendor", "version", "spec_version"))]
    async fn get_server_information(&self) -> (String, String, String, String) {
        ("Boswas Shell".into(), "Boswas OS".into(), env!("CARGO_PKG_VERSION").into(), "1.2".into())
    }

    /// Do not disturb is on (the property KDE's server has under the same
    /// name).
    #[zbus(property)]
    async fn inhibited(&self) -> bool {
        self.inhibited.load(Ordering::Relaxed)
    }

    #[zbus(signal)]
    async fn notification_closed(emitter: &SignalEmitter<'_>, id: u32, reason: u32) -> zbus::Result<()>;

    #[zbus(signal)]
    async fn action_invoked(emitter: &SignalEmitter<'_>, id: u32, action_key: &str) -> zbus::Result<()>;
}

impl Service for Notifications {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self {
            ctx,
            ex,
            next_id: 1,
            live: BTreeMap::new(),
            inhibited: Arc::default(),
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
                        Err(error) => tracing::debug!(%error, "notifications: the session bus stopped answering"),
                    }
                }
                Err(error) => tracing::debug!(%error, "notifications: no session bus"),
            }
            // The applications are gone with the bus; so is what they sent.
            for id in std::mem::take(&mut self.live).into_keys() {
                self.ctx.emit(Event::NotificationClosed { id, withdrawn: true });
            }
            if let IdleEnd::Closed = idle(&commands, backoff.next(), |_| false).await {
                return;
            }
        }
    }
}

impl Notifications {
    async fn serve(&mut self, bus: &Bus, commands: &Receiver<Command>) -> CallResult<bool> {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        let conn = &bus.conn;
        let _owner = subscribe(
            conn,
            &self.ex,
            owner_rule(NAME)?,
            &inbox,
            |message| owner_change(message).map(|(_, _, owner)| Msg::Owner { owner }),
            || Msg::Lost,
        )
        .await?;
        conn.object_server()
            .at(PATH, Server { inbox: inbox.clone(), inhibited: self.inhibited.clone() })
            .await?;
        self.claim(conn).await;

        loop {
            let deadline = self.live.values().filter_map(|live| live.expires).min();
            match wake(commands, &messages, deadline).await {
                Wake::Closed => return Ok(true),
                Wake::Message(Msg::Lost) => return Ok(false),
                Wake::Message(Msg::Owner { owner }) => {
                    // Whoever had the name gave it up: it is ours to take.
                    if owner.is_empty() {
                        self.claim(conn).await;
                    }
                }
                Wake::Message(Msg::Notify { request, reply }) => {
                    let id = self.accept(conn, *request).await;
                    let _ = reply.send(id).await;
                }
                Wake::Message(Msg::Close { id }) => {
                    if self.live.remove(&id).is_some() {
                        closed(conn, id, CLOSED).await;
                        self.ctx.emit(Event::NotificationClosed { id, withdrawn: true });
                    }
                }
                Wake::Command(Command::NotificationAction { id, action }) => {
                    let Some(live) = self.live.get(&id) else { continue };
                    if action != "default" && !live.action_keys.contains(&action) {
                        continue;
                    }
                    let resident = live.resident;
                    signal(conn, "ActionInvoked", &(id, action.as_str())).await;
                    if !resident {
                        self.live.remove(&id);
                        closed(conn, id, DISMISSED).await;
                    }
                }
                Wake::Command(Command::NotificationDismiss { id }) => {
                    if self.live.remove(&id).is_some() {
                        closed(conn, id, DISMISSED).await;
                    }
                }
                Wake::Command(Command::SetDoNotDisturb(on)) => {
                    if self.inhibited.swap(on, Ordering::Relaxed) != on {
                        let changed: HashMap<&str, Value<'_>> = HashMap::from([("Inhibited", Value::from(on))]);
                        let _ = conn
                            .emit_signal(None::<&str>, PATH, PROPERTIES, "PropertiesChanged", &(NAME, changed, Vec::<&str>::new()))
                            .await;
                    }
                }
                Wake::Command(_) => {}
                Wake::Timer => {
                    let now = Instant::now();
                    let due: Vec<u32> = self
                        .live
                        .iter()
                        .filter(|(_, live)| live.expires.is_some_and(|at| at <= now))
                        .map(|(id, _)| *id)
                        .collect();
                    for id in due {
                        self.live.remove(&id);
                        closed(conn, id, EXPIRED).await;
                        self.ctx.emit(Event::NotificationClosed { id, withdrawn: false });
                    }
                }
            }
        }
    }

    /// Asks for the name. When another server has it nothing is shown and
    /// nothing is taken from it; `Msg::Owner` tells when it is free.
    async fn claim(&self, conn: &Connection) {
        match conn.request_name_with_flags(NAME, RequestNameFlags::DoNotQueue.into()).await {
            Ok(_) => tracing::debug!("notifications: serving {NAME}"),
            Err(zbus::Error::NameTaken) => tracing::info!("notifications: another notification server is running; waiting for it to leave"),
            Err(error) => tracing::warn!(%error, "notifications: could not take the bus name"),
        }
    }

    /// Records a notification, tells the shell and returns its id (0 when
    /// there are no ids left).
    async fn accept(&mut self, conn: &Connection, request: Request) -> u32 {
        let replaces_existing = request.replaces_id != 0 && self.live.contains_key(&request.replaces_id);
        let id = if replaces_existing {
            request.replaces_id
        } else {
            if self.next_id == u32::MAX {
                return 0;
            }
            let id = self.next_id;
            self.next_id += 1;
            id
        };

        let hints = Hints::read(&request.hints);
        let actions: Vec<(String, String)> = request
            .actions
            .chunks_exact(2)
            .filter(|pair| !pair[0].is_empty())
            .take(SHOWN_ACTIONS)
            .map(|pair| (pair[0].clone(), clean(&pair[1], SHOWN_ACTION, false)))
            .collect();
        // Critical notifications stay until dismissed; 0 asks for the same
        // and -1 leaves the time to the server, which leaves it to the
        // shell. Only a positive time runs a timer here.
        let timeout_ms = match request.expire_timeout {
            ms if ms > 0 && hints.urgency != Urgency::Critical => Some(ms as u32),
            _ => None,
        };
        let notification = Notification {
            id,
            app_name: clean(&request.app_name, SHOWN_APP_NAME, false),
            app_icon: hints.image.clone().or_else(|| icon(&request.app_icon)).unwrap_or_default(),
            desktop_entry: hints.desktop_entry.clone(),
            summary: clean(&request.summary, SHOWN_SUMMARY, false),
            body: clean(&strip_markup(&request.body), SHOWN_BODY, true),
            actions,
            urgency: hints.urgency,
            timeout_ms,
            transient: hints.transient,
            progress: hints.progress,
            time: unix_time(),
            replaces_existing,
        };
        self.live.insert(
            id,
            Live {
                expires: timeout_ms.map(|ms| Instant::now() + Duration::from_millis(u64::from(ms))),
                resident: hints.resident,
                critical: hints.urgency == Urgency::Critical,
                action_keys: notification.actions.iter().map(|(key, _)| key.clone()).collect(),
            },
        );
        self.ctx.emit(Event::Notify(notification));

        while self.live.len() > MAX_LIVE {
            // The oldest that is not critical, else the oldest.
            let victim = self
                .live
                .iter()
                .find(|(other, live)| **other != id && !live.critical)
                .or_else(|| self.live.iter().find(|(other, _)| **other != id))
                .map(|(other, _)| *other);
            let Some(victim) = victim else { break };
            self.live.remove(&victim);
            closed(conn, victim, EXPIRED).await;
            self.ctx.emit(Event::NotificationClosed { id: victim, withdrawn: true });
        }
        id
    }
}

async fn signal<B>(conn: &Connection, member: &str, body: &B)
where
    B: serde::Serialize + zbus::zvariant::DynamicType,
{
    if let Err(error) = conn.emit_signal(None::<&str>, PATH, NAME, member, body).await {
        tracing::debug!(%error, member, "notifications: a signal was not sent");
    }
}

async fn closed(conn: &Connection, id: u32, reason: u32) {
    signal(conn, "NotificationClosed", &(id, reason)).await;
}

/// The hints the shell shows. Everything else an application sends in the
/// dictionary (image data, sounds, positions) is dropped unread.
struct Hints {
    urgency: Urgency,
    transient: bool,
    resident: bool,
    desktop_entry: Option<String>,
    progress: Option<u8>,
    image: Option<String>,
}

impl Hints {
    fn read(hints: &HashMap<String, OwnedValue>) -> Hints {
        let get = |name: &str| hints.get(name).map(|value| &**value);
        // Booleans arrive as booleans or, from older toolkits, as numbers.
        let flag = |name: &str| get(name).and_then(|value| as_bool(value).or_else(|| as_i64(value).map(|n| n != 0))).unwrap_or(false);
        let urgency = match get("urgency").and_then(as_i64) {
            Some(0) => Urgency::Low,
            Some(2) => Urgency::Critical,
            _ => Urgency::Normal,
        };
        let desktop_entry = get("desktop-entry")
            .and_then(as_str)
            .map(|text| clean(text, 256, false))
            .filter(|text| !text.is_empty() && !text.contains('/'));
        let progress = get("value").and_then(as_i64).map(|value| value.clamp(0, 100) as u8);
        let image = get("image-path").or_else(|| get("image_path")).and_then(as_str).and_then(icon);
        Hints {
            urgency,
            transient: flag("transient"),
            resident: flag("resident"),
            desktop_entry,
            progress,
            image,
        }
    }
}

/// An icon as the shell takes it: an absolute path (from a path or a
/// `file://` URI) or a freedesktop icon name. Anything else is no icon.
fn icon(text: &str) -> Option<String> {
    if text.is_empty() || text.len() > MAX_ICON {
        return None;
    }
    if text.starts_with("file://") {
        return file_uri_path(text).filter(|path| !path.chars().any(char::is_control));
    }
    if text.starts_with('/') {
        return (!text.chars().any(char::is_control)).then(|| text.to_owned());
    }
    let name_like = text.len() <= 256 && text.chars().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.' | '+'));
    name_like.then(|| text.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hints(entries: Vec<(&str, Value<'_>)>) -> HashMap<String, OwnedValue> {
        entries.into_iter().map(|(name, value)| (name.to_owned(), value.try_to_owned().unwrap())).collect()
    }

    #[test]
    fn reads_the_hints_it_knows() {
        let read = Hints::read(&hints(vec![
            ("urgency", Value::U8(2)),
            ("transient", Value::Bool(true)),
            ("resident", Value::I32(1)),
            ("desktop-entry", Value::from("org.example.Mail")),
            ("value", Value::I32(250)),
            ("image-path", Value::from("file:///tmp/a%20face.png")),
            ("image-data", Value::from(vec![0u8; 64])),
        ]));
        assert_eq!(read.urgency, Urgency::Critical);
        assert!(read.transient && read.resident);
        assert_eq!(read.desktop_entry.as_deref(), Some("org.example.Mail"));
        assert_eq!(read.progress, Some(100));
        assert_eq!(read.image.as_deref(), Some("/tmp/a face.png"));
    }

    #[test]
    fn hints_of_the_wrong_type_are_defaults() {
        let read = Hints::read(&hints(vec![
            ("urgency", Value::from("very")),
            ("transient", Value::from("yes")),
            ("desktop-entry", Value::U32(7)),
            ("value", Value::from("half")),
            ("image-path", Value::Bool(true)),
        ]));
        assert_eq!(read.urgency, Urgency::Normal);
        assert!(!read.transient && !read.resident);
        assert_eq!(read.desktop_entry, None);
        assert_eq!(read.progress, None);
        assert_eq!(read.image, None);
        // A desktop entry is an id, not a path.
        let read = Hints::read(&hints(vec![("desktop-entry", Value::from("../../etc/passwd"))]));
        assert_eq!(read.desktop_entry, None);
    }

    #[test]
    fn icons_are_names_or_absolute_paths() {
        assert_eq!(icon("dialog-information").as_deref(), Some("dialog-information"));
        assert_eq!(icon("/usr/share/pixmaps/a.png").as_deref(), Some("/usr/share/pixmaps/a.png"));
        assert_eq!(icon("file:///home/a/b.png").as_deref(), Some("/home/a/b.png"));
        assert_eq!(icon(""), None);
        assert_eq!(icon("https://example.org/track.png"), None);
        assert_eq!(icon("relative/path.png"), None);
        assert_eq!(icon("/tmp/a\nb"), None);
        assert_eq!(icon(&"a".repeat(5000)), None);
    }
}
