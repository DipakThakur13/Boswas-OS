//! The tray: this service is the session's `org.kde.StatusNotifierWatcher`
//! and its only host. It follows the registered StatusNotifierItems and
//! fetches their menus (`com.canonical.dbusmenu`).
//!
//! Items are other programs. Nothing they are asked is waited for in the
//! service's loop, replies are bounded in size before they are decoded,
//! and every text and picture is cut to what the shell can show.

use std::{
    cell::RefCell,
    collections::{HashMap, HashSet},
    rc::Rc,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use async_channel::{Receiver, Sender};
use async_executor::Task;
use futures_lite::future;
use zbus::{
    fdo::{self, RequestNameFlags},
    message::Header,
    names::BusName,
    object_server::SignalEmitter,
    zvariant::{ObjectPath, OwnedValue, Value},
    Connection, Message,
};

use crate::{
    runtime::{
        call_raw, earliest, idle, name_owner, owner_change, signal_rule, subscribe, timeout, wake, Backoff, Bus,
        BusKind, CallError, CallResult, Ctx, Exec, IdleEnd, Latest, Service, Wake, CALL_TIMEOUT, DBUS, DBUS_PATH, MIN_INTERVAL,
        PROPERTIES, SETTLE,
    },
    text::strip_markup,
    var::{as_bool, as_dict, as_fields, as_i64, as_list, as_str, clean, label, prop, prop_bool, prop_path, prop_str, Props},
    Command, Event, MenuEntry, MenuToggle, Pixmap, Service as Which, TrayItem, TrayStatus,
};

const WATCHER: &str = "org.kde.StatusNotifierWatcher";
const WATCHER_PATH: &str = "/StatusNotifierWatcher";
const ITEM: &str = "org.kde.StatusNotifierItem";
const DEFAULT_ITEM_PATH: &str = "/StatusNotifierItem";
const MENU: &str = "com.canonical.dbusmenu";

/// Items beyond this are refused: a tray holds a dozen.
const MAX_ITEMS: usize = 64;
/// A reply with all properties of an item is decoded only up to this size;
/// a larger one (an item that sends big pixmaps) is asked for piece by
/// piece instead.
const MAX_PROPS_REPLY: usize = 256 * 1024;
/// One text property.
const MAX_TEXT_REPLY: usize = 64 * 1024;
/// The pixmap property, read without decoding it into values.
const MAX_PIXMAP_REPLY: usize = 8 * 1024 * 1024;
/// A menu layout.
const MAX_MENU_REPLY: usize = 512 * 1024;
/// A pixmap side an item may send, and the side the shell is given at most.
const MAX_SOURCE_SIDE: u32 = 1024;
const MAX_SIDE: u32 = 256;
/// Menus deeper or longer than this are cut.
const MAX_MENU_DEPTH: usize = 8;
const MAX_MENU_ENTRIES: usize = 1000;
/// After a menu was asked for, its changes are passed on for this long.
const MENU_FOLLOW: Duration = Duration::from_secs(60);

const ITEM_SIGNALS: [&str; 7] = ["NewIcon", "NewAttentionIcon", "NewOverlayIcon", "NewTitle", "NewToolTip", "NewStatus", "NewMenu"];
const MENU_PROPERTIES: [&str; 8] =
    ["label", "enabled", "visible", "type", "toggle-type", "toggle-state", "icon-name", "children-display"];

pub(crate) struct Tray {
    ctx: Ctx,
    ex: Exec,
    state: Latest<Vec<TrayItem>>,
    items: Vec<Item>,
    /// What `RegisteredStatusNotifierItems` answers.
    registered: Arc<Mutex<Vec<String>>>,
    /// Calls to items that are on their way.
    calls: Vec<Task<()>>,
    /// The menu the shell asked for last, followed for a while.
    menu: Option<MenuWatch>,
}

struct Item {
    /// Bus name and object path, as registered.
    key: String,
    /// Where calls go: the name the item registered with.
    bus: String,
    /// The connection behind it; signals carry this name.
    owner: String,
    path: String,
    /// What the shell sees; `None` until the item answered.
    shown: Option<TrayItem>,
    menu_path: Option<String>,
    is_menu: bool,
    due: Option<Instant>,
    last_read: Option<Instant>,
    reading: Option<Task<()>>,
}

struct MenuWatch {
    key: String,
    until: Instant,
    due: Option<Instant>,
    sent: Vec<MenuEntry>,
}

enum Msg {
    Register { bus: String, path: String },
    /// A name on the bus changed owner (`owner` is empty when it left).
    Owner { name: String, owner: String },
    /// An item said something about it changed.
    Changed { owner: String, path: String },
    Read { key: String, owner: String, read: Option<Box<ItemRead>> },
    MenuChanged { owner: String },
    Menu { key: String, entries: Vec<MenuEntry> },
    /// The item has no `Activate`: its menu is the answer to a click.
    NoActivate { key: String },
    Lost,
}

/// What an item answered.
struct ItemRead {
    item: TrayItem,
    menu_path: Option<String>,
    is_menu: bool,
}

/// Signals not yet handled, so that an item that signals without pause
/// puts one message in the inbox, not thousands.
type Pending = Rc<RefCell<HashSet<(String, String)>>>;

// --- the watcher object -----------------------------------------------------------

struct Watcher {
    inbox: Sender<Msg>,
    registered: Arc<Mutex<Vec<String>>>,
}

#[zbus::interface(name = "org.kde.StatusNotifierWatcher")]
impl Watcher {
    /// `service` is a bus name (the item is at `/StatusNotifierItem`) or
    /// an object path on the caller's connection.
    async fn register_status_notifier_item(&self, service: String, #[zbus(header)] header: Header<'_>) -> fdo::Result<()> {
        let invalid = || fdo::Error::InvalidArgs("a bus name or an object path is expected".into());
        if service.is_empty() || service.len() > 255 {
            return Err(invalid());
        }
        let (bus, path) = if service.starts_with('/') {
            let sender = header.sender().ok_or_else(invalid)?.to_string();
            (sender, service)
        } else {
            (service, DEFAULT_ITEM_PATH.to_owned())
        };
        if BusName::try_from(bus.as_str()).is_err() || ObjectPath::try_from(path.as_str()).is_err() {
            return Err(invalid());
        }
        let _ = self.inbox.send(Msg::Register { bus, path }).await;
        Ok(())
    }

    /// This service is the host; others may say they are one too.
    async fn register_status_notifier_host(&self, _service: String) {}

    #[zbus(property)]
    async fn registered_status_notifier_items(&self) -> Vec<String> {
        self.registered.lock().map(|list| list.clone()).unwrap_or_default()
    }

    #[zbus(property)]
    async fn is_status_notifier_host_registered(&self) -> bool {
        true
    }

    #[zbus(property)]
    async fn protocol_version(&self) -> i32 {
        0
    }

    #[zbus(signal)]
    async fn status_notifier_item_registered(emitter: &SignalEmitter<'_>, service: &str) -> zbus::Result<()>;

    #[zbus(signal)]
    async fn status_notifier_item_unregistered(emitter: &SignalEmitter<'_>, service: &str) -> zbus::Result<()>;

    #[zbus(signal)]
    async fn status_notifier_host_registered(emitter: &SignalEmitter<'_>) -> zbus::Result<()>;

    #[zbus(signal)]
    async fn status_notifier_host_unregistered(emitter: &SignalEmitter<'_>) -> zbus::Result<()>;
}

// --- the service ------------------------------------------------------------------

impl Service for Tray {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self {
            ctx,
            ex,
            state: Latest::default(),
            items: Vec::new(),
            registered: Arc::default(),
            calls: Vec::new(),
            menu: None,
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
                        Err(error) => tracing::debug!(%error, "tray: the session bus stopped answering"),
                    }
                }
                Err(error) => tracing::debug!(%error, "tray: no session bus"),
            }
            self.items.clear();
            self.calls.clear();
            self.menu = None;
            self.sync_registered();
            self.publish(false);
            match idle(&commands, backoff.next(), |command| matches!(command, Command::Refresh(Which::Tray))).await {
                IdleEnd::Closed => return,
                IdleEnd::Retry => self.publish(true),
                IdleEnd::Elapsed => {}
            }
        }
    }
}

impl Tray {
    fn publish(&mut self, force: bool) {
        let list: Vec<TrayItem> = self.items.iter().filter_map(|item| item.shown.clone()).collect();
        if let Some(list) = self.state.update(list, force) {
            self.ctx.emit(Event::Tray(list));
        }
    }

    fn sync_registered(&self) {
        if let Ok(mut list) = self.registered.lock() {
            *list = self.items.iter().map(|item| item.key.clone()).collect();
        }
    }

    async fn serve(&mut self, bus: &Bus, commands: &Receiver<Command>) -> CallResult<bool> {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        let conn = &bus.conn;
        let pending: Pending = Rc::default();
        let _subscriptions = self.subscribe(conn, &inbox, &pending).await?;
        conn.object_server()
            .at(WATCHER_PATH, Watcher { inbox: inbox.clone(), registered: self.registered.clone() })
            .await?;
        if claim(conn).await {
            watcher_signal(conn, "StatusNotifierHostRegistered", &()).await;
        }
        self.publish(true);

        loop {
            let mut deadline = self.items.iter().filter(|item| item.reading.is_none()).fold(None, |soonest, item| earliest(soonest, item.due));
            if let Some(menu) = &self.menu {
                deadline = earliest(deadline, menu.due);
            }
            match wake(commands, &messages, deadline).await {
                Wake::Closed => return Ok(true),
                Wake::Message(message) => match message {
                    Msg::Lost => return Ok(false),
                    Msg::Register { bus, path } => self.register(conn, bus, path).await,
                    Msg::Owner { name, owner } => {
                        if name == WATCHER {
                            if owner.is_empty() && claim(conn).await {
                                watcher_signal(conn, "StatusNotifierHostRegistered", &()).await;
                            }
                        } else {
                            self.owner_changed(conn, &name, &owner).await;
                        }
                    }
                    Msg::Changed { owner, path } => {
                        pending.borrow_mut().remove(&(owner.clone(), path.clone()));
                        for item in self.items.iter_mut().filter(|item| item.owner == owner && item.path == path) {
                            item.schedule();
                        }
                    }
                    Msg::Read { key, owner, read } => {
                        self.read_done(&key, &owner, read);
                        self.publish(false);
                    }
                    Msg::MenuChanged { owner } => {
                        pending.borrow_mut().remove(&(owner.clone(), String::new()));
                        let now = Instant::now();
                        if let Some(menu) = &mut self.menu {
                            if menu.until <= now {
                                self.menu = None;
                            } else if menu.due.is_none() && self.items.iter().any(|item| item.key == menu.key && item.owner == owner) {
                                menu.due = Some(now + MIN_INTERVAL);
                            }
                        }
                    }
                    Msg::Menu { key, entries } => {
                        let fresh = match &mut self.menu {
                            Some(menu) if menu.key == key => {
                                let changed = menu.sent != entries;
                                menu.sent = entries.clone();
                                changed
                            }
                            _ => {
                                self.menu = Some(MenuWatch { key: key.clone(), until: Instant::now() + MENU_FOLLOW, due: None, sent: entries.clone() });
                                true
                            }
                        };
                        if fresh {
                            self.ctx.emit(Event::TrayMenu { key, entries });
                        }
                    }
                    Msg::NoActivate { key } => self.open_menu(conn, &key, &inbox),
                },
                Wake::Command(command) => self.command(conn, command, &inbox),
                Wake::Timer => {
                    let now = Instant::now();
                    for item in &mut self.items {
                        if item.reading.is_none() && item.due.is_some_and(|due| due <= now) {
                            item.due = None;
                            item.last_read = Some(now);
                            item.reading = Some(self.ex.spawn(read_item(
                                conn.clone(),
                                item.key.clone(),
                                item.bus.clone(),
                                item.owner.clone(),
                                item.path.clone(),
                                inbox.clone(),
                            )));
                        }
                    }
                    let refetch = match &mut self.menu {
                        Some(menu) if menu.due.is_some_and(|due| due <= now) => {
                            menu.due = None;
                            Some(menu.key.clone())
                        }
                        _ => None,
                    };
                    if let Some(key) = refetch {
                        self.fetch_menu(conn, &key, &inbox, false);
                    }
                }
            }
        }
    }

    async fn subscribe(&self, conn: &Connection, inbox: &Sender<Msg>, pending: &Pending) -> CallResult<Vec<Task<()>>> {
        let mut tasks = Vec::new();
        // Every owner change: items are known by whatever name they chose.
        let owners = signal_rule(Some(DBUS), DBUS, Some("NameOwnerChanged"), Some(DBUS_PATH))?.build();
        tasks.push(
            subscribe(
                conn,
                &self.ex,
                owners,
                inbox,
                |message| {
                    let (name, _, owner) = owner_change(message)?;
                    Some(Msg::Owner { name, owner })
                },
                || Msg::Lost,
            )
            .await?,
        );
        let changed = pending.clone();
        tasks.push(
            subscribe(
                conn,
                &self.ex,
                signal_rule(None, ITEM, None, None)?.build(),
                inbox,
                move |message| {
                    let header = message.header();
                    if !ITEM_SIGNALS.contains(&header.member()?.as_str()) {
                        return None;
                    }
                    let entry = (header.sender()?.to_string(), header.path()?.to_string());
                    changed.borrow_mut().insert(entry.clone()).then_some(Msg::Changed { owner: entry.0, path: entry.1 })
                },
                || Msg::Lost,
            )
            .await?,
        );
        let changed = pending.clone();
        tasks.push(
            subscribe(
                conn,
                &self.ex,
                signal_rule(None, MENU, None, None)?.build(),
                inbox,
                move |message| {
                    let header = message.header();
                    if !matches!(header.member()?.as_str(), "LayoutUpdated" | "ItemsPropertiesUpdated") {
                        return None;
                    }
                    let owner = header.sender()?.to_string();
                    changed.borrow_mut().insert((owner.clone(), String::new())).then_some(Msg::MenuChanged { owner })
                },
                || Msg::Lost,
            )
            .await?,
        );
        Ok(tasks)
    }

    async fn register(&mut self, conn: &Connection, bus: String, path: String) {
        let key = format!("{bus}{path}");
        if let Some(item) = self.items.iter_mut().find(|item| item.key == key) {
            // Registered again (some toolkits do after the watcher
            // restarts): read it again.
            item.schedule();
            return;
        }
        if self.items.len() >= MAX_ITEMS {
            tracing::warn!("tray: too many items, one more is ignored");
            return;
        }
        // The connection behind the name; a name nobody owns is no item.
        let Some(owner) = name_owner(conn, &bus).await else {
            return;
        };
        self.items.push(Item {
            key: key.clone(),
            bus,
            owner,
            path,
            shown: None,
            menu_path: None,
            is_menu: false,
            due: Some(Instant::now()),
            last_read: None,
            reading: None,
        });
        self.sync_registered();
        watcher_signal(conn, "StatusNotifierItemRegistered", &(key.as_str(),)).await;
    }

    /// A name left the bus or moved to another connection.
    async fn owner_changed(&mut self, conn: &Connection, name: &str, owner: &str) {
        let mut gone = Vec::new();
        for item in &mut self.items {
            if owner.is_empty() && (item.owner == name || item.bus == name) {
                gone.push(item.key.clone());
            } else if !owner.is_empty() && item.bus == name && item.owner != owner {
                // Its well-known name moved to another connection.
                item.owner = owner.to_owned();
                item.reading = None;
                item.due = Some(Instant::now());
            }
        }
        if gone.is_empty() {
            return;
        }
        self.items.retain(|item| !gone.contains(&item.key));
        if self.menu.as_ref().is_some_and(|menu| gone.contains(&menu.key)) {
            self.menu = None;
        }
        self.sync_registered();
        for key in &gone {
            watcher_signal(conn, "StatusNotifierItemUnregistered", &(key.as_str(),)).await;
        }
        self.publish(false);
    }

    fn read_done(&mut self, key: &str, owner: &str, read: Option<Box<ItemRead>>) {
        let Some(item) = self.items.iter_mut().find(|item| item.key == key && item.owner == owner) else {
            return;
        };
        item.reading = None;
        // An item that stops answering keeps what it showed last; one that
        // never answered is not shown.
        if let Some(read) = read {
            item.shown = Some(read.item);
            item.menu_path = read.menu_path;
            item.is_menu = read.is_menu;
        }
    }

    fn command(&mut self, conn: &Connection, command: Command, inbox: &Sender<Msg>) {
        match command {
            Command::Refresh(Which::Tray) => {
                let now = Instant::now();
                for item in &mut self.items {
                    item.due = Some(now);
                }
                self.publish(true);
            }
            Command::TrayActivate { key, x, y } => {
                let Some(item) = self.items.iter().find(|item| item.key == key) else { return };
                if item.is_menu && item.menu_path.is_some() {
                    self.open_menu(conn, &key, inbox);
                    return;
                }
                let (conn, bus, path, inbox) = (conn.clone(), item.bus.clone(), item.path.clone(), inbox.clone());
                self.spawn_call(async move {
                    if let Err(error) = call_raw(&conn, CALL_TIMEOUT, &bus, &path, ITEM, "Activate", &(x, y)).await {
                        // Items made with libappindicator have a menu and
                        // nothing else.
                        if matches!(error.name(), Some("org.freedesktop.DBus.Error.UnknownMethod") | Some("org.freedesktop.DBus.Error.NotSupported")) {
                            let _ = inbox.send(Msg::NoActivate { key }).await;
                        }
                    }
                });
            }
            Command::TraySecondaryActivate { key, x, y } => self.item_call(conn, &key, "SecondaryActivate", (x, y)),
            Command::TrayScroll { key, delta, horizontal } => {
                // Items expect the wheel's angle: 120 for one step.
                let orientation = if horizontal { "horizontal" } else { "vertical" };
                self.item_call(conn, &key, "Scroll", (delta.saturating_mul(120), orientation));
            }
            Command::TrayMenu { key } => self.open_menu(conn, &key, inbox),
            Command::TrayMenuActivate { key, id } => {
                let Some((bus, menu)) = self.menu_of(&key) else { return };
                let conn = conn.clone();
                self.spawn_call(async move {
                    let body = (id, "clicked", Value::from(0i32), 0u32);
                    if let Err(error) = call_raw(&conn, CALL_TIMEOUT, &bus, &menu, MENU, "Event", &body).await {
                        tracing::debug!(%error, "tray: the menu did not take the click");
                    }
                });
            }
            _ => {}
        }
    }

    fn spawn_call(&mut self, work: impl std::future::Future<Output = ()> + 'static) {
        self.calls.retain(|task| !task.is_finished());
        self.calls.push(self.ex.spawn(work));
    }

    fn item_call<B>(&mut self, conn: &Connection, key: &str, method: &'static str, body: B)
    where
        B: serde::Serialize + zbus::zvariant::DynamicType + 'static,
    {
        let Some(item) = self.items.iter().find(|item| item.key == key) else { return };
        let (conn, bus, path) = (conn.clone(), item.bus.clone(), item.path.clone());
        self.spawn_call(async move {
            if let Err(error) = call_raw(&conn, CALL_TIMEOUT, &bus, &path, ITEM, method, &body).await {
                tracing::debug!(method, %error, "tray: the item refused");
            }
        });
    }

    fn menu_of(&self, key: &str) -> Option<(String, String)> {
        let item = self.items.iter().find(|item| item.key == key)?;
        Some((item.bus.clone(), item.menu_path.clone()?))
    }

    /// The shell asked for a menu: it is answered, with no entries when
    /// the item has none.
    fn open_menu(&mut self, conn: &Connection, key: &str, inbox: &Sender<Msg>) {
        if !self.items.iter().any(|item| item.key == key) {
            return;
        }
        // Asked anew: what was sent before does not count as "the same".
        self.menu = None;
        self.fetch_menu(conn, key, inbox, true);
    }

    fn fetch_menu(&mut self, conn: &Connection, key: &str, inbox: &Sender<Msg>, about_to_show: bool) {
        let (conn, inbox, key) = (conn.clone(), inbox.clone(), key.to_owned());
        let target = self.menu_of(&key);
        self.spawn_call(async move {
            let entries = match target {
                Some((bus, menu)) => read_menu(&conn, &bus, &menu, about_to_show).await.unwrap_or_default(),
                None => Vec::new(),
            };
            let _ = inbox.send(Msg::Menu { key, entries }).await;
        });
    }
}

impl Item {
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

/// Takes the watcher name if nobody has it. True when it is ours now.
async fn claim(conn: &Connection) -> bool {
    match conn.request_name_with_flags(WATCHER, RequestNameFlags::DoNotQueue.into()).await {
        Ok(_) => true,
        Err(zbus::Error::NameTaken) => {
            tracing::info!("tray: another StatusNotifierWatcher is running; waiting for it to leave");
            false
        }
        Err(error) => {
            tracing::warn!(%error, "tray: could not take the watcher name");
            false
        }
    }
}

async fn watcher_signal<B>(conn: &Connection, member: &str, body: &B)
where
    B: serde::Serialize + zbus::zvariant::DynamicType,
{
    if let Err(error) = conn.emit_signal(None::<&str>, WATCHER_PATH, WATCHER, member, body).await {
        tracing::debug!(%error, member, "tray: a signal was not sent");
    }
}

// --- reading an item --------------------------------------------------------------

async fn read_item(conn: Connection, key: String, bus: String, owner: String, path: String, inbox: Sender<Msg>) {
    let read = read_item_properties(&conn, &key, &bus, &path).await.map(Box::new);
    let _ = inbox.send(Msg::Read { key, owner, read }).await;
}

async fn read_item_properties(conn: &Connection, key: &str, bus: &str, path: &str) -> Option<ItemRead> {
    let props = match bounded_reply(conn, bus, path, "GetAll", &(ITEM,), MAX_PROPS_REPLY).await {
        Ok(reply) => reply.body().deserialize::<Props>().ok()?,
        // Too big to decode whole, or an item that only answers `Get`.
        Err(_) => read_piecewise(conn, bus, path).await?,
    };
    let mut read = describe(key, &props);
    if read.item.icon_name.is_empty() {
        let attention = read.item.status == TrayStatus::NeedsAttention;
        let mut pixmap = None;
        if attention {
            pixmap = read_pixmap(conn, bus, path, "AttentionIconPixmap").await;
        }
        if pixmap.is_none() {
            pixmap = read_pixmap(conn, bus, path, "IconPixmap").await;
        }
        read.item.icon_pixmap = pixmap;
    }
    Some(read)
}

/// Calls a method and refuses a reply whose body is larger than `max`
/// before any of it is decoded.
async fn bounded_reply<B>(conn: &Connection, bus: &str, path: &str, method: &str, body: &B, max: usize) -> CallResult<Message>
where
    B: serde::Serialize + zbus::zvariant::DynamicType,
{
    let reply = call_raw(conn, CALL_TIMEOUT, bus, path, PROPERTIES, method, body).await?;
    if reply.body().len() > max {
        return Err(CallError::Bus(zbus::Error::ExcessData));
    }
    Ok(reply)
}

/// The text properties one by one. `None` when the item answers none.
async fn read_piecewise(conn: &Connection, bus: &str, path: &str) -> Option<Props> {
    let get = |name: &'static str| async move {
        let reply = bounded_reply(conn, bus, path, "Get", &(ITEM, name), MAX_TEXT_REPLY).await.ok()?;
        let value = reply.body().deserialize::<OwnedValue>().ok()?;
        Some((name.to_owned(), value))
    };
    let (((id, title), (status, icon)), ((theme, attention), ((menu, is_menu), tooltip))) = future::zip(
        future::zip(future::zip(get("Id"), get("Title")), future::zip(get("Status"), get("IconName"))),
        future::zip(
            future::zip(get("IconThemePath"), get("AttentionIconName")),
            future::zip(future::zip(get("Menu"), get("ItemIsMenu")), get("ToolTip")),
        ),
    )
    .await;
    let props: Props = [id, title, status, icon, theme, attention, menu, is_menu, tooltip].into_iter().flatten().collect();
    (!props.is_empty()).then_some(props)
}

/// What the shell shows of an item's properties (without the pixmap).
fn describe(key: &str, props: &Props) -> ItemRead {
    let text = |name: &str| prop_str(props, name).map(|text| label(&text)).unwrap_or_default();
    let status = match prop_str(props, "Status").as_deref() {
        Some("Passive") => TrayStatus::Passive,
        Some("NeedsAttention") => TrayStatus::NeedsAttention,
        _ => TrayStatus::Active,
    };
    let mut icon_name = text("IconName");
    if status == TrayStatus::NeedsAttention {
        let attention = text("AttentionIconName");
        if !attention.is_empty() {
            icon_name = attention;
        }
    }
    let title = text("Title");
    // ToolTip is (icon name, icon pixmaps, title, text).
    let tooltip = prop(props, "ToolTip")
        .and_then(as_fields)
        .and_then(|fields| {
            let title = fields.get(2).and_then(as_str).map(label).unwrap_or_default();
            let body = fields.get(3).and_then(as_str).map(|text| label(&strip_markup(text))).unwrap_or_default();
            match (title.is_empty(), body.is_empty()) {
                (false, _) => Some(title),
                (true, false) => Some(body),
                (true, true) => None,
            }
        })
        .unwrap_or_else(|| title.clone());
    let menu_path = prop_path(props, "Menu")
        .or_else(|| prop_str(props, "Menu").filter(|path| ObjectPath::try_from(path.as_str()).is_ok()))
        .filter(|path| path != "/" && path != "/NO_DBUSMENU");
    let icon_theme_path = prop_str(props, "IconThemePath")
        .filter(|path| path.starts_with('/') && path.len() <= 4096 && !path.chars().any(char::is_control))
        .unwrap_or_default();
    ItemRead {
        item: TrayItem {
            key: key.to_owned(),
            id: text("Id"),
            title,
            icon_name,
            icon_theme_path,
            icon_pixmap: None,
            tooltip,
            status,
            has_menu: menu_path.is_some(),
        },
        menu_path,
        is_menu: prop_bool(props, "ItemIsMenu").unwrap_or(false),
    }
}

// --- pixmaps ----------------------------------------------------------------------

async fn read_pixmap(conn: &Connection, bus: &str, path: &str, property: &str) -> Option<Pixmap> {
    let reply = bounded_reply(conn, bus, path, "Get", &(ITEM, property), MAX_PIXMAP_REPLY).await.ok()?;
    let big_endian = matches!(reply.primary_header().endian_sig(), zbus::message::EndianSig::Big);
    let body = reply.body();
    let pixmaps = parse_pixmaps(body.data().bytes(), big_endian)?;
    choose_pixmap(&pixmaps).map(convert_pixmap)
}

/// One picture of an `a(iiay)` list, still in the message's bytes.
#[derive(Debug, PartialEq, Eq)]
struct RawPixmap<'a> {
    width: u32,
    height: u32,
    /// `width * height * 4` bytes: alpha, red, green, blue per pixel.
    argb: &'a [u8],
}

/// Reads the body of a `Properties.Get` reply that holds a variant of
/// type `a(iiay)`, straight from the wire format. The generic decoder
/// would turn every byte of a pixmap into a value many times its size;
/// this borrows the pixels instead. Pictures of impossible sizes are
/// skipped; a body of another shape is `None`.
fn parse_pixmaps(body: &[u8], big_endian: bool) -> Option<Vec<RawPixmap<'_>>> {
    const SIGNATURE: &[u8] = b"a(iiay)";
    let u32_at = |at: usize| -> Option<u32> {
        let bytes: [u8; 4] = body.get(at..at + 4)?.try_into().ok()?;
        Some(if big_endian { u32::from_be_bytes(bytes) } else { u32::from_le_bytes(bytes) })
    };
    let align = |at: usize, to: usize| at.div_ceil(to) * to;

    // The variant: signature length, signature, NUL.
    if usize::from(*body.first()?) != SIGNATURE.len() || body.get(1..1 + SIGNATURE.len())? != SIGNATURE || *body.get(1 + SIGNATURE.len())? != 0 {
        return None;
    }
    // The array: its length in bytes, then the elements from the next
    // multiple of 8 (structures are aligned to 8).
    let mut at = align(2 + SIGNATURE.len(), 4);
    let length = u32_at(at)? as usize;
    at = align(at + 4, 8);
    let end = at.checked_add(length)?;
    if end > body.len() {
        return None;
    }
    let mut pixmaps = Vec::new();
    while at < end {
        at = align(at, 8);
        let width = u32_at(at)? as i32;
        let height = u32_at(at + 4)? as i32;
        let bytes = u32_at(at + 8)? as usize;
        let start = at + 12;
        let stop = start.checked_add(bytes)?;
        if stop > end {
            return None;
        }
        let sides_ok = (1..=MAX_SOURCE_SIDE as i32).contains(&width) && (1..=MAX_SOURCE_SIDE as i32).contains(&height);
        if sides_ok && bytes == width as usize * height as usize * 4 {
            pixmaps.push(RawPixmap { width: width as u32, height: height as u32, argb: &body[start..stop] });
        }
        at = stop;
        if pixmaps.len() >= 64 {
            break;
        }
    }
    Some(pixmaps)
}

/// The picture nearest to what a tray shows: a side of 24 to 48 pixels.
/// Among those the largest; otherwise the nearest, where too small counts
/// double (scaling down looks better than scaling up).
fn choose_pixmap<'a, 'b>(pixmaps: &'a [RawPixmap<'b>]) -> Option<&'a RawPixmap<'b>> {
    let distance = |pixmap: &RawPixmap<'_>| {
        let side = pixmap.width.max(pixmap.height);
        if side < 24 {
            (24 - side) * 2
        } else {
            side.saturating_sub(48)
        }
    };
    pixmaps.iter().min_by(|a, b| distance(a).cmp(&distance(b)).then(b.width.max(b.height).cmp(&a.width.max(a.height))))
}

/// ARGB in network byte order to straight RGBA, scaled down by a whole
/// factor when a side is longer than the shell is given.
fn convert_pixmap(raw: &RawPixmap<'_>) -> Pixmap {
    let factor = raw.width.max(raw.height).div_ceil(MAX_SIDE).max(1);
    if factor == 1 {
        let mut rgba = Vec::with_capacity(raw.argb.len());
        for pixel in raw.argb.chunks_exact(4) {
            rgba.extend_from_slice(&[pixel[1], pixel[2], pixel[3], pixel[0]]);
        }
        return Pixmap { width: raw.width, height: raw.height, rgba };
    }
    let width = (raw.width / factor).max(1);
    let height = (raw.height / factor).max(1);
    let mut rgba = Vec::with_capacity((width * height * 4) as usize);
    for y in 0..height {
        for x in 0..width {
            // Average the block, weighting colour by alpha so transparent
            // pixels do not darken the edges.
            let (mut alpha, mut red, mut green, mut blue, mut count) = (0u64, 0u64, 0u64, 0u64, 0u64);
            for sy in (y * factor)..((y + 1) * factor).min(raw.height) {
                for sx in (x * factor)..((x + 1) * factor).min(raw.width) {
                    let at = ((sy * raw.width + sx) * 4) as usize;
                    let a = u64::from(raw.argb[at]);
                    alpha += a;
                    red += u64::from(raw.argb[at + 1]) * a;
                    green += u64::from(raw.argb[at + 2]) * a;
                    blue += u64::from(raw.argb[at + 3]) * a;
                    count += 1;
                }
            }
            match (red.checked_div(alpha), green.checked_div(alpha), blue.checked_div(alpha)) {
                (Some(red), Some(green), Some(blue)) => {
                    rgba.extend_from_slice(&[red as u8, green as u8, blue as u8, (alpha / count.max(1)) as u8]);
                }
                // A block that is transparent throughout.
                _ => rgba.extend_from_slice(&[0, 0, 0, 0]),
            }
        }
    }
    Pixmap { width, height, rgba }
}

// --- menus ------------------------------------------------------------------------

type Layout = (u32, (i32, HashMap<String, OwnedValue>, Vec<OwnedValue>));

async fn read_menu(conn: &Connection, bus: &str, menu: &str, about_to_show: bool) -> Option<Vec<MenuEntry>> {
    if about_to_show {
        // Many menus are filled only when they are about to be shown.
        // Whether the item answers does not matter.
        let _ = timeout(Duration::from_secs(2), conn.call_method(Some(bus), menu, Some(MENU), "AboutToShow", &(0i32,))).await;
    }
    let reply = call_raw(conn, CALL_TIMEOUT, bus, menu, MENU, "GetLayout", &(0i32, -1i32, MENU_PROPERTIES.as_slice())).await.ok()?;
    if reply.body().len() > MAX_MENU_REPLY {
        tracing::debug!("tray: a menu is too large to show");
        return None;
    }
    let body = reply.body();
    let (_, (_, _, children)) = body.deserialize::<Layout>().ok()?;
    let mut budget = MAX_MENU_ENTRIES;
    Some(menu_entries(&children, 1, &mut budget))
}

/// The entries of a list of `(ia{sv}av)` nodes. Hidden entries are left
/// out; nesting and the number of entries are bounded.
fn menu_entries(nodes: &[OwnedValue], depth: usize, budget: &mut usize) -> Vec<MenuEntry> {
    let values: Vec<&Value<'_>> = nodes.iter().map(|node| &**node).collect();
    menu_entries_of(&values, depth, budget)
}

fn menu_entries_of(nodes: &[&Value<'_>], depth: usize, budget: &mut usize) -> Vec<MenuEntry> {
    let mut entries = Vec::new();
    for node in nodes {
        if *budget == 0 {
            break;
        }
        let Some(fields) = as_fields(node) else { continue };
        let (Some(id), Some(props)) = (fields.first().and_then(as_i64), fields.get(1)) else { continue };
        let Ok(id) = i32::try_from(id) else { continue };
        let props: HashMap<&str, &Value<'_>> = as_dict(props).into_iter().collect();
        let get_str = |name: &str| props.get(name).and_then(|value| as_str(value));
        let get_bool = |name: &str| props.get(name).and_then(|value| as_bool(value));
        if get_bool("visible") == Some(false) {
            continue;
        }
        *budget -= 1;
        let checked = props.get("toggle-state").and_then(|value| as_i64(value)) == Some(1);
        let toggle = match get_str("toggle-type") {
            Some("checkmark") => MenuToggle::Checkmark(checked),
            Some("radio") => MenuToggle::Radio(checked),
            _ => MenuToggle::None,
        };
        let children = match fields.get(2).and_then(as_list) {
            Some(children) if depth < MAX_MENU_DEPTH => {
                let children: Vec<&Value<'_>> = children.iter().collect();
                menu_entries_of(&children, depth + 1, budget)
            }
            _ => Vec::new(),
        };
        entries.push(MenuEntry {
            id,
            label: get_str("label").map(menu_label).unwrap_or_default(),
            enabled: get_bool("enabled").unwrap_or(true),
            separator: get_str("type") == Some("separator"),
            toggle,
            icon_name: get_str("icon-name").map(|name| clean(name, 256, false)).unwrap_or_default(),
            children,
        });
    }
    entries
}

/// A menu label without its mnemonic marker: `_File` is "File", `__` is
/// an underscore.
fn menu_label(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '_' {
            if chars.peek() == Some(&'_') {
                chars.next();
                out.push('_');
            }
        } else {
            out.push(ch);
        }
    }
    label(&out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use zbus::zvariant::{serialized::Context, to_bytes, Endian, StructureBuilder};

    fn owned(value: Value<'_>) -> OwnedValue {
        value.try_to_owned().unwrap()
    }

    fn encode(pixmaps: &[(i32, i32, Vec<u8>)], endian: Endian) -> Vec<u8> {
        let context = Context::new_dbus(endian, 0);
        to_bytes(context, &Value::from(pixmaps.to_vec())).unwrap().bytes().to_vec()
    }

    #[test]
    fn pixmaps_are_read_from_the_wire_in_both_byte_orders() {
        let small = vec![7u8; 2 * 2 * 4];
        let odd = vec![9u8; 3 * 4];
        let large = vec![1u8; 32 * 32 * 4];
        let list = [(2, 2, small.clone()), (3, 1, odd.clone()), (32, 32, large.clone())];
        for endian in [Endian::Little, Endian::Big] {
            let bytes = encode(&list, endian);
            let pixmaps = parse_pixmaps(&bytes, endian == Endian::Big).unwrap();
            assert_eq!(pixmaps.len(), 3);
            assert_eq!(pixmaps[0], RawPixmap { width: 2, height: 2, argb: &small });
            assert_eq!(pixmaps[1], RawPixmap { width: 3, height: 1, argb: &odd });
            assert_eq!(pixmaps[2].argb, &large[..]);
        }
        assert_eq!(parse_pixmaps(&encode(&[], Endian::Little), false), Some(vec![]));
    }

    #[test]
    fn impossible_pixmaps_are_skipped_and_other_shapes_refused() {
        let list = [
            (0, 4, vec![]),
            (-2, -2, vec![0u8; 16]),
            (4, 4, vec![0u8; 10]),
            (5000, 1, vec![0u8; 20000]),
            (1, 1, vec![1, 2, 3, 4]),
        ];
        let bytes = encode(&list, Endian::Little);
        let pixmaps = parse_pixmaps(&bytes, false).unwrap();
        assert_eq!(pixmaps, vec![RawPixmap { width: 1, height: 1, argb: &[1, 2, 3, 4] }]);

        // Not a variant of a(iiay).
        let context = Context::new_dbus(Endian::Little, 0);
        let text = to_bytes(context, &Value::from("icon")).unwrap();
        assert_eq!(parse_pixmaps(text.bytes(), false), None);
        assert_eq!(parse_pixmaps(&[], false), None);
        // Cut short anywhere: never a panic, never a picture past the end.
        for cut in 0..bytes.len() {
            let _ = parse_pixmaps(&bytes[..cut], false);
        }
        // A length that points past the end.
        let mut lying = encode(&[(1, 1, vec![1, 2, 3, 4])], Endian::Little);
        lying[12..16].copy_from_slice(&u32::MAX.to_le_bytes());
        assert_eq!(parse_pixmaps(&lying, false), None);
        let mut lying = encode(&[(1, 1, vec![1, 2, 3, 4])], Endian::Little);
        lying[24..28].copy_from_slice(&0xffff_fff0u32.to_le_bytes());
        assert_eq!(parse_pixmaps(&lying, false), None);
    }

    #[test]
    fn the_pixmap_nearest_to_tray_size_is_chosen() {
        let data = vec![0u8; 512 * 512 * 4];
        let of = |side: u32| RawPixmap { width: side, height: side, argb: &data[..(side * side * 4) as usize] };
        let side = |sides: &[u32]| {
            let list: Vec<RawPixmap<'_>> = sides.iter().map(|side| of(*side)).collect();
            choose_pixmap(&list).map(|pixmap| pixmap.width)
        };
        assert_eq!(side(&[16, 22, 32, 48, 64, 256]), Some(48));
        assert_eq!(side(&[16, 24, 512]), Some(24));
        assert_eq!(side(&[16, 64]), Some(64));
        assert_eq!(side(&[22, 128]), Some(22));
        assert_eq!(side(&[512]), Some(512));
        assert_eq!(side(&[]), None);
    }

    #[test]
    fn pixmaps_become_straight_rgba_and_stay_small() {
        // One pixel: alpha 0x80, red 0x10, green 0x20, blue 0x30.
        let pixel = [0x80, 0x10, 0x20, 0x30];
        let converted = convert_pixmap(&RawPixmap { width: 1, height: 1, argb: &pixel });
        assert_eq!(converted, Pixmap { width: 1, height: 1, rgba: vec![0x10, 0x20, 0x30, 0x80] });

        // 512 x 512, opaque red: scaled to 256 x 256, still opaque red.
        let big: Vec<u8> = [0xff, 0xff, 0x00, 0x00].repeat(512 * 512);
        let converted = convert_pixmap(&RawPixmap { width: 512, height: 512, argb: &big });
        assert_eq!((converted.width, converted.height), (256, 256));
        assert_eq!(converted.rgba.len(), 256 * 256 * 4);
        assert!(converted.rgba.chunks_exact(4).all(|pixel| pixel == [0xff, 0x00, 0x00, 0xff]));

        // 600 x 300: a factor of 3 gives 200 x 100.
        let wide = vec![0xffu8; 600 * 300 * 4];
        let converted = convert_pixmap(&RawPixmap { width: 600, height: 300, argb: &wide });
        assert_eq!((converted.width, converted.height), (200, 100));

        // Transparent neighbours do not darken a pixel's colour.
        let mut block = [0u8; 2 * 300 * 4];
        block[..4].copy_from_slice(&[0xff, 0xc8, 0x64, 0x32]);
        let converted = convert_pixmap(&RawPixmap { width: 300, height: 2, argb: &block });
        assert_eq!(&converted.rgba[..3], &[0xc8, 0x64, 0x32]);
        assert!(converted.rgba[3] > 0 && converted.rgba[3] < 0xff);
    }

    fn item_props(entries: Vec<(&str, Value<'_>)>) -> Props {
        entries.into_iter().map(|(name, value)| (name.to_owned(), owned(value))).collect()
    }

    #[test]
    fn describes_an_item() {
        let tooltip = StructureBuilder::new()
            .add_field("")
            .add_field(Vec::<(i32, i32, Vec<u8>)>::new())
            .add_field("Volume")
            .add_field("<b>42 %</b>")
            .build()
            .unwrap();
        let props = item_props(vec![
            ("Id", Value::from("volume")),
            ("Title", Value::from("Volume control")),
            ("Status", Value::from("NeedsAttention")),
            ("IconName", Value::from("audio-volume-high")),
            ("AttentionIconName", Value::from("audio-volume-muted")),
            ("IconThemePath", Value::from("/opt/app/icons")),
            ("ToolTip", Value::from(tooltip)),
            ("Menu", Value::ObjectPath("/MenuBar".try_into().unwrap())),
            ("ItemIsMenu", Value::from(true)),
        ]);
        let read = describe(":1.5/StatusNotifierItem", &props);
        assert_eq!(
            read.item,
            TrayItem {
                key: ":1.5/StatusNotifierItem".into(),
                id: "volume".into(),
                title: "Volume control".into(),
                icon_name: "audio-volume-muted".into(),
                icon_theme_path: "/opt/app/icons".into(),
                icon_pixmap: None,
                tooltip: "Volume".into(),
                status: TrayStatus::NeedsAttention,
                has_menu: true,
            }
        );
        assert_eq!(read.menu_path.as_deref(), Some("/MenuBar"));
        assert!(read.is_menu);
    }

    #[test]
    fn describes_an_item_that_sends_nonsense() {
        let props = item_props(vec![
            ("Id", Value::from(12u32)),
            ("Title", Value::from("  a\u{202e}b\n")),
            ("Status", Value::from(true)),
            ("IconName", Value::from(vec![1u8, 2])),
            ("IconThemePath", Value::from("relative/path")),
            ("ToolTip", Value::from("not a structure")),
            ("Menu", Value::from("/NO_DBUSMENU")),
            ("ItemIsMenu", Value::from("yes")),
        ]);
        let read = describe("k", &props);
        assert_eq!(read.item.id, "");
        assert_eq!(read.item.title, "ab");
        assert_eq!(read.item.tooltip, "ab");
        assert_eq!(read.item.status, TrayStatus::Active);
        assert_eq!(read.item.icon_name, "");
        assert_eq!(read.item.icon_theme_path, "");
        assert!(!read.item.has_menu && read.menu_path.is_none() && !read.is_menu);
        let empty = describe("k", &Props::new());
        assert_eq!(empty.item.status, TrayStatus::Active);
    }

    fn node(id: i32, props: Vec<(&str, Value<'_>)>, children: Vec<Value<'static>>) -> Value<'static> {
        let props: HashMap<String, OwnedValue> = props.into_iter().map(|(name, value)| (name.to_owned(), owned(value))).collect();
        let children: Vec<OwnedValue> = children.into_iter().map(owned).collect();
        Value::from(StructureBuilder::new().add_field(id).add_field(props).add_field(children).build().unwrap())
            .try_to_owned()
            .unwrap()
            .into()
    }

    #[test]
    fn menu_layout_becomes_entries() {
        let layout = vec![
            node(1, vec![("label", Value::from("_Open file__name"))], vec![]),
            node(2, vec![("type", Value::from("separator"))], vec![]),
            node(3, vec![("label", Value::from("Hidden")), ("visible", Value::from(false))], vec![]),
            node(
                4,
                vec![("label", Value::from("_More")), ("children-display", Value::from("submenu")), ("icon-name", Value::from("go-next"))],
                vec![
                    node(5, vec![("label", Value::from("Loud")), ("toggle-type", Value::from("checkmark")), ("toggle-state", Value::from(1i32))], vec![]),
                    node(6, vec![("label", Value::from("FM")), ("toggle-type", Value::from("radio")), ("toggle-state", Value::from(0i32))], vec![]),
                    node(7, vec![("label", Value::from("Off")), ("enabled", Value::from(false))], vec![]),
                ],
            ),
        ];
        let nodes: Vec<OwnedValue> = layout.into_iter().map(owned).collect();
        let mut budget = MAX_MENU_ENTRIES;
        let entries = menu_entries(&nodes, 1, &mut budget);
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0].label, "Open file_name");
        assert!(entries[0].enabled && !entries[0].separator);
        assert!(entries[1].separator);
        assert_eq!(entries[2].icon_name, "go-next");
        let children = &entries[2].children;
        assert_eq!(children.len(), 3);
        assert_eq!(children[0].toggle, MenuToggle::Checkmark(true));
        assert_eq!(children[1].toggle, MenuToggle::Radio(false));
        assert!(!children[2].enabled);
        assert_eq!(budget, MAX_MENU_ENTRIES - 6);
    }

    #[test]
    fn menus_are_bounded_in_depth_and_length() {
        // A chain forty levels deep.
        let mut chain = node(100, vec![("label", Value::from("leaf"))], vec![]);
        for level in 0..40 {
            chain = node(level, vec![("label", Value::from("level"))], vec![chain]);
        }
        let mut budget = MAX_MENU_ENTRIES;
        let mut entries = menu_entries(&[owned(chain)], 1, &mut budget);
        let mut depth = 0;
        while let Some(entry) = entries.pop() {
            depth += 1;
            entries = entry.children;
        }
        assert_eq!(depth, MAX_MENU_DEPTH);

        // Three thousand entries side by side.
        let flat: Vec<OwnedValue> = (0..3000).map(|id| owned(node(id, vec![("label", Value::from("x"))], vec![]))).collect();
        let mut budget = MAX_MENU_ENTRIES;
        assert_eq!(menu_entries(&flat, 1, &mut budget).len(), MAX_MENU_ENTRIES);

        // Nodes that are not nodes are skipped.
        let junk = vec![owned(Value::from("text")), owned(Value::from(5u32)), owned(node(1, vec![], vec![]))];
        let mut budget = MAX_MENU_ENTRIES;
        assert_eq!(menu_entries(&junk, 1, &mut budget).len(), 1);
    }

    #[test]
    fn mnemonics_are_removed_from_labels() {
        assert_eq!(menu_label("_File"), "File");
        assert_eq!(menu_label("Save _As..."), "Save As...");
        assert_eq!(menu_label("snake__case"), "snake_case");
        assert_eq!(menu_label("___"), "_");
        assert_eq!(menu_label(""), "");
    }
}
