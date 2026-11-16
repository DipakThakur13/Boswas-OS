//! What every service is built from: one thread with a small executor, a
//! bus connection driven on that thread, an inbox that bus signals are
//! forwarded to, debouncing, and "send the state only when it changed".

use std::{
    future::Future,
    rc::Rc,
    sync::Arc,
    thread::JoinHandle,
    time::{Duration, Instant},
};

use async_channel::{Receiver, Sender};
use async_executor::{LocalExecutor, Task};
use async_io::Timer;
use futures_lite::{future, StreamExt};
use serde::{de::DeserializeOwned, Serialize};
use zbus::{
    zvariant::{DynamicType, OwnedValue, Type, Value},
    Connection, MatchRule, Message, MessageStream,
};

use crate::{var::Props, Command, Environment, Event};

/// Where events go.
pub(crate) type Sink = Arc<dyn Fn(Event) + Send + Sync>;

/// The executor of a service thread. Tasks spawned on it must not hold it.
pub(crate) type Exec = Rc<LocalExecutor<'static>>;

/// How long an ordinary method call may take before the service gives up
/// on it. Calls that wait for a person (pairing, authentication) pass
/// their own limit.
pub(crate) const CALL_TIMEOUT: Duration = Duration::from_secs(5);

/// State changes are sent at most this often.
pub(crate) const MIN_INTERVAL: Duration = Duration::from_millis(200);

/// A burst of change signals is given this long to finish before the
/// state is read again.
pub(crate) const SETTLE: Duration = Duration::from_millis(25);

/// Signals a service has not read yet, per subscription. zbus stops
/// reading the socket when a subscription is full, so this is generous;
/// the forwarding task empties it as fast as the thread runs.
const SIGNAL_QUEUE: usize = 1024;

pub(crate) const DBUS: &str = "org.freedesktop.DBus";
pub(crate) const DBUS_PATH: &str = "/org/freedesktop/DBus";
pub(crate) const PROPERTIES: &str = "org.freedesktop.DBus.Properties";

/// What a service is given when it starts.
#[derive(Clone)]
pub(crate) struct Ctx {
    pub sink: Sink,
    pub env: Arc<Environment>,
    /// Commands that change the system are not to be obeyed (the lock
    /// screen and the greeter).
    pub restricted: bool,
    /// A restricted instance that may still suspend, restart and shut down
    /// (the login screen).
    pub power_actions: bool,
}

impl Ctx {
    pub fn emit(&self, event: Event) {
        (self.sink)(event);
    }
}

/// One service: it runs until the command channel closes.
pub(crate) trait Service: Sized {
    fn new(ctx: Ctx, ex: Exec) -> Self;
    async fn run(&mut self, commands: Receiver<Command>);
    /// Called once after `run` ended or was abandoned; it gets a second.
    async fn shutdown(&mut self) {}
}

/// Starts a service on its own thread. Closing `stop` (dropping its
/// sender) abandons `run` wherever it waits.
pub(crate) fn spawn<S: Service + 'static>(
    name: &str,
    ctx: Ctx,
    commands: Receiver<Command>,
    stop: Receiver<()>,
) -> std::io::Result<JoinHandle<()>> {
    std::thread::Builder::new()
        .name(format!("boswas-{name}"))
        .spawn(move || {
            let ex: Exec = Rc::new(LocalExecutor::new());
            let run = {
                let ex = ex.clone();
                async move {
                    let mut service = S::new(ctx, ex.clone());
                    let stopped = async {
                        // Nothing is ever sent: this ends when the sender
                        // is dropped.
                        let _ = stop.recv().await;
                    };
                    future::or(service.run(commands), stopped).await;
                    let _ = timeout(Duration::from_secs(1), service.shutdown()).await;
                    drop(service);
                    // Connections close in tasks of their own (see `Bus`).
                    let _ = timeout(Duration::from_secs(1), async {
                        while !ex.is_empty() {
                            Timer::after(Duration::from_millis(1)).await;
                        }
                    })
                    .await;
                }
            };
            async_io::block_on(ex.run(run));
        })
}

// --- time -------------------------------------------------------------------------

#[derive(Debug)]
pub(crate) struct TimedOut;

pub(crate) async fn timeout<T>(limit: Duration, work: impl Future<Output = T>) -> Result<T, TimedOut> {
    future::or(async { Ok(work.await) }, async {
        Timer::after(limit).await;
        Err(TimedOut)
    })
    .await
}

/// The earlier of two optional deadlines.
pub(crate) fn earliest(a: Option<Instant>, b: Option<Instant>) -> Option<Instant> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (a, None) => a,
        (None, b) => b,
    }
}

/// Coalesces change signals: after the first one the state is read once
/// the burst has settled, and never more often than [`MIN_INTERVAL`].
#[derive(Debug)]
pub(crate) struct Debounce {
    due: Option<Instant>,
    last: Option<Instant>,
    interval: Duration,
}

impl Default for Debounce {
    fn default() -> Self {
        Self::new(MIN_INTERVAL)
    }
}

impl Debounce {
    pub fn new(interval: Duration) -> Self {
        Self { due: None, last: None, interval }
    }

    /// Something changed.
    pub fn trigger(&mut self) {
        if self.due.is_none() {
            let now = Instant::now();
            let mut due = now + SETTLE;
            if let Some(last) = self.last {
                due = due.max(last + self.interval);
            }
            self.due = Some(due);
        }
    }

    /// Read as soon as possible (start-up, `Refresh`).
    pub fn trigger_now(&mut self) {
        self.due = Some(Instant::now());
    }

    pub fn deadline(&self) -> Option<Instant> {
        self.due
    }

    /// True when the state is to be read now; the caller then reads it.
    pub fn take_due(&mut self) -> bool {
        match self.due {
            Some(due) if due <= Instant::now() => {
                self.due = None;
                self.last = Some(Instant::now());
                true
            }
            _ => false,
        }
    }
}

/// Waits between attempts to reach something that is not there: 1, 2, 4 ...
/// seconds up to `max`.
#[derive(Debug)]
pub(crate) struct Backoff {
    next: Duration,
    max: Duration,
}

impl Backoff {
    pub fn new(max: Duration) -> Self {
        Self { next: Duration::from_secs(1), max }
    }

    pub fn reset(&mut self) {
        self.next = Duration::from_secs(1);
    }

    pub fn next(&mut self) -> Duration {
        let now = self.next;
        self.next = (self.next * 2).min(self.max);
        now
    }
}

/// Remembers the last state sent so that only changes go out.
#[derive(Debug)]
pub(crate) struct Latest<T> {
    last: Option<T>,
}

impl<T> Default for Latest<T> {
    fn default() -> Self {
        Self { last: None }
    }
}

impl<T: PartialEq + Clone> Latest<T> {
    /// Returns the value to send: when it differs from the last one sent,
    /// or when `force` is set.
    pub fn update(&mut self, value: T, force: bool) -> Option<T> {
        if force || self.last.as_ref() != Some(&value) {
            self.last = Some(value.clone());
            Some(value)
        } else {
            None
        }
    }

    pub fn get(&self) -> Option<&T> {
        self.last.as_ref()
    }
}

// --- waiting ----------------------------------------------------------------------

/// What woke a service.
pub(crate) enum Wake<M> {
    Command(Command),
    Message(M),
    Timer,
    /// The command channel closed: the service is to end.
    Closed,
}

/// Waits for a command, a message in the inbox or the deadline.
pub(crate) async fn wake<M>(
    commands: &Receiver<Command>,
    inbox: &Receiver<M>,
    deadline: Option<Instant>,
) -> Wake<M> {
    let command = async {
        match commands.recv().await {
            Ok(command) => Wake::Command(command),
            Err(_) => Wake::Closed,
        }
    };
    let message = async {
        match inbox.recv().await {
            Ok(message) => Wake::Message(message),
            Err(_) => future::pending().await,
        }
    };
    let timer = async {
        match deadline {
            Some(at) => {
                Timer::at(at).await;
                Wake::Timer
            }
            None => future::pending().await,
        }
    };
    future::or(command, future::or(message, timer)).await
}

/// Waits `duration` while the service has nothing to talk to, dropping the
/// commands that arrive. False when the command channel closed.
pub(crate) async fn idle(commands: &Receiver<Command>, duration: Duration, mut keep: impl FnMut(&Command) -> bool) -> IdleEnd {
    let deadline = Instant::now() + duration;
    loop {
        let next = future::or(
            async { Some(commands.recv().await) },
            async {
                Timer::at(deadline).await;
                None
            },
        )
        .await;
        match next {
            None => return IdleEnd::Elapsed,
            Some(Err(_)) => return IdleEnd::Closed,
            Some(Ok(command)) => {
                if keep(&command) {
                    return IdleEnd::Retry;
                }
            }
        }
    }
}

pub(crate) enum IdleEnd {
    Elapsed,
    /// A command asked to try again now (`Refresh`).
    Retry,
    Closed,
}

// --- the bus ----------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum BusKind {
    Session,
    System,
}

#[derive(Debug)]
pub(crate) enum CallError {
    TimedOut,
    Bus(zbus::Error),
}

impl CallError {
    /// The D-Bus error name, when the peer answered with an error.
    pub fn name(&self) -> Option<&str> {
        match self {
            CallError::Bus(zbus::Error::MethodError(name, _, _)) => Some(name.as_str()),
            _ => None,
        }
    }
}

impl std::fmt::Display for CallError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CallError::TimedOut => f.write_str("no answer in time"),
            CallError::Bus(zbus::Error::MethodError(name, text, _)) => {
                write!(f, "{}: {}", name.as_str(), text.as_deref().unwrap_or(""))
            }
            CallError::Bus(error) => write!(f, "{error}"),
        }
    }
}

impl From<zbus::Error> for CallError {
    fn from(error: zbus::Error) -> Self {
        CallError::Bus(error)
    }
}

impl From<TimedOut> for CallError {
    fn from(_: TimedOut) -> Self {
        CallError::TimedOut
    }
}

pub(crate) type CallResult<T> = Result<T, CallError>;

/// A bus connection driven by the service's own thread: zbus gets no
/// thread of its own, the ticker task runs its reader and its object
/// server between the service's own steps.
///
/// Dropping the `Bus` closes the connection: the ticker shuts the socket,
/// lets zbus's own tasks end (they hold the connection, so they must run
/// to their end for it to be freed) and then lets go of it.
pub(crate) struct Bus {
    pub conn: Connection,
    /// Nothing is sent; dropping it tells the ticker to close.
    _open: Sender<()>,
}

impl Bus {
    pub async fn connect(kind: BusKind, env: &Environment, ex: &Exec) -> CallResult<Bus> {
        use zbus::connection::Builder;
        let builder = match kind {
            BusKind::Session => match &env.session_bus {
                Some(address) => Builder::address(address.as_str())?,
                None => Builder::session()?,
            },
            BusKind::System => match &env.system_bus {
                Some(address) => Builder::address(address.as_str())?,
                None => Builder::system()?,
            },
        };
        let conn = timeout(Duration::from_secs(10), builder.internal_executor(false).build()).await??;
        let (open, dropped) = async_channel::bounded::<()>(1);
        ex.spawn({
            let conn = conn.clone();
            async move {
                let tick = async {
                    loop {
                        conn.executor().tick().await;
                        // One zbus task at a time, so a flood on the
                        // socket cannot starve the service's own tasks.
                        future::yield_now().await;
                    }
                };
                let closed = async {
                    let _ = dropped.recv().await;
                };
                future::or(tick, closed).await;
                // The service's tasks that were cancelled with the `Bus`
                // drop their subscriptions first.
                for _ in 0..4 {
                    future::yield_now().await;
                }
                let _ = conn.clone().close().await;
                let _ = timeout(Duration::from_millis(500), async {
                    while !conn.executor().is_empty() {
                        conn.executor().tick().await;
                    }
                })
                .await;
            }
        })
        .detach();
        Ok(Bus { conn, _open: open })
    }
}

/// Calls a method and returns the reply message.
pub(crate) async fn call_raw<B>(
    conn: &Connection,
    limit: Duration,
    destination: &str,
    path: &str,
    interface: &str,
    method: &str,
    body: &B,
) -> CallResult<Message>
where
    B: Serialize + DynamicType,
{
    let reply = timeout(limit, conn.call_method(Some(destination), path, Some(interface), method, body)).await??;
    Ok(reply)
}

/// Calls a method and reads the reply as `R`. A reply of another type is
/// an error, never a panic.
pub(crate) async fn call<B, R>(
    conn: &Connection,
    destination: &str,
    path: &str,
    interface: &str,
    method: &str,
    body: &B,
) -> CallResult<R>
where
    B: Serialize + DynamicType,
    R: DeserializeOwned + Type,
{
    call_within(conn, CALL_TIMEOUT, destination, path, interface, method, body).await
}

pub(crate) async fn call_within<B, R>(
    conn: &Connection,
    limit: Duration,
    destination: &str,
    path: &str,
    interface: &str,
    method: &str,
    body: &B,
) -> CallResult<R>
where
    B: Serialize + DynamicType,
    R: DeserializeOwned + Type,
{
    let reply = call_raw(conn, limit, destination, path, interface, method, body).await?;
    let body = reply.body();
    Ok(body.deserialize::<R>()?)
}

/// `org.freedesktop.DBus.Properties.GetAll`.
pub(crate) async fn get_all(conn: &Connection, destination: &str, path: &str, interface: &str) -> CallResult<Props> {
    call(conn, destination, path, PROPERTIES, "GetAll", &(interface,)).await
}

/// `org.freedesktop.DBus.Properties.Get`.
pub(crate) async fn get_property(
    conn: &Connection,
    destination: &str,
    path: &str,
    interface: &str,
    name: &str,
) -> CallResult<OwnedValue> {
    call(conn, destination, path, PROPERTIES, "Get", &(interface, name)).await
}

/// `org.freedesktop.DBus.Properties.Set`.
pub(crate) async fn set_property(
    conn: &Connection,
    destination: &str,
    path: &str,
    interface: &str,
    name: &str,
    value: Value<'_>,
) -> CallResult<()> {
    call_raw(conn, CALL_TIMEOUT, destination, path, PROPERTIES, "Set", &(interface, name, value)).await?;
    Ok(())
}

/// The unique name that owns `name`, if anything does. Does not start the
/// service.
pub(crate) async fn name_owner(conn: &Connection, name: &str) -> Option<String> {
    call::<_, String>(conn, DBUS, DBUS_PATH, DBUS, "GetNameOwner", &(name,)).await.ok()
}

/// A match rule for a signal. `None` leaves a part open.
pub(crate) fn signal_rule(
    sender: Option<&str>,
    interface: &str,
    member: Option<&str>,
    path: Option<&str>,
) -> zbus::Result<zbus::match_rule::Builder<'static>> {
    let mut builder = MatchRule::builder().msg_type(zbus::message::Type::Signal).interface(interface.to_owned())?;
    if let Some(sender) = sender {
        builder = builder.sender(sender.to_owned())?;
    }
    if let Some(member) = member {
        builder = builder.member(member.to_owned())?;
    }
    if let Some(path) = path {
        builder = builder.path(path.to_owned())?;
    }
    Ok(builder)
}

/// The rule for `NameOwnerChanged` of one name.
pub(crate) fn owner_rule(name: &str) -> zbus::Result<MatchRule<'static>> {
    Ok(signal_rule(Some(DBUS), DBUS, Some("NameOwnerChanged"), Some(DBUS_PATH))?.arg(0, name.to_owned())?.build())
}

/// Reads `NameOwnerChanged`: (name, old owner, new owner).
pub(crate) fn owner_change(message: &Message) -> Option<(String, String, String)> {
    let body = message.body();
    body.deserialize::<(String, String, String)>().ok()
}

/// Subscribes to the signals that match `rule`. A task on the service's
/// executor passes each one to `map` and puts what it returns in the
/// inbox, so the subscription never fills up while the service waits for
/// something else. When the connection is lost, `lost` is put in the
/// inbox. Dropping the returned task ends the subscription.
pub(crate) async fn subscribe<M: 'static>(
    conn: &Connection,
    ex: &Exec,
    rule: MatchRule<'static>,
    inbox: &Sender<M>,
    map: impl Fn(&Message) -> Option<M> + 'static,
    lost: impl FnOnce() -> M + 'static,
) -> CallResult<Task<()>> {
    let mut stream = timeout(CALL_TIMEOUT, MessageStream::for_match_rule(rule, conn, Some(SIGNAL_QUEUE))).await??;
    let inbox = inbox.clone();
    Ok(ex.spawn(async move {
        while let Some(Ok(message)) = stream.next().await {
            if let Some(mapped) = map(&message) {
                if inbox.send(mapped).await.is_err() {
                    return;
                }
            }
        }
        let _ = inbox.send(lost()).await;
    }))
}

/// A flag that keeps a burst of identical triggers down to one message in
/// the inbox: `raise` is true only for the first trigger until `clear`.
#[derive(Clone, Default)]
pub(crate) struct Flag(Rc<std::cell::Cell<bool>>);

impl Flag {
    pub fn raise(&self) -> bool {
        !self.0.replace(true)
    }

    pub fn clear(&self) {
        self.0.set(false);
    }
}

/// A child process that is killed (if it still runs) and reaped when
/// dropped, so that a task that is abandoned leaves no process behind.
pub(crate) struct Reaped(pub std::process::Child);

impl Drop for Reaped {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// Seconds since the Unix epoch.
pub(crate) fn unix_time() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debounce_spaces_reads() {
        let mut debounce = Debounce::new(Duration::from_millis(200));
        assert!(debounce.deadline().is_none());
        debounce.trigger_now();
        assert!(debounce.take_due());
        debounce.trigger();
        let due = debounce.deadline().unwrap();
        // The next read is a full interval after the one just done.
        assert!(due >= Instant::now() + Duration::from_millis(150));
        // More triggers do not move it.
        debounce.trigger();
        assert_eq!(debounce.deadline(), Some(due));
        assert!(!debounce.take_due());
    }

    #[test]
    fn latest_sends_changes_only() {
        let mut latest = Latest::default();
        assert_eq!(latest.update(1, false), Some(1));
        assert_eq!(latest.update(1, false), None);
        assert_eq!(latest.update(1, true), Some(1));
        assert_eq!(latest.update(2, false), Some(2));
    }

    #[test]
    fn backoff_grows_to_its_limit() {
        let mut backoff = Backoff::new(Duration::from_secs(4));
        let waits: Vec<u64> = (0..5).map(|_| backoff.next().as_secs()).collect();
        assert_eq!(waits, [1, 2, 4, 4, 4]);
        backoff.reset();
        assert_eq!(backoff.next().as_secs(), 1);
    }

    #[test]
    fn flag_raises_once() {
        let flag = Flag::default();
        assert!(flag.raise());
        assert!(!flag.raise());
        flag.clear();
        assert!(flag.raise());
    }
}
