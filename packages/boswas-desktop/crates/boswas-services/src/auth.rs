//! The polkit authentication agent of the session.
//!
//! polkit calls `BeginAuthentication` when an action needs the person to
//! authenticate. The agent runs polkit's own helper
//! (`polkit-agent-helper-1`), which talks to PAM and tells polkit the
//! result itself; the agent only carries prompts to the shell and answers
//! back. The password passes through this process on its way to the
//! helper's standard input and is wiped from the buffer it was copied to.

use std::{
    collections::{HashMap, VecDeque},
    io::{BufRead, BufReader, Read, Write},
    os::unix::fs::MetadataExt,
    path::Path,
    process::{Child, ChildStdin, Command as Process, Stdio},
    time::{Duration, Instant},
};

use async_channel::{Receiver, Sender};
use async_io::Timer;
use zbus::{
    message::Header,
    zvariant::{OwnedValue, Value},
    Connection,
};
use zeroize::Zeroizing;

use crate::{
    logind::{own_session, OwnSession, LOGIN1},
    runtime::{call_raw, idle, name_owner, owner_rule, subscribe, wake, Backoff, Bus, BusKind, CallResult, Ctx, Exec, IdleEnd, Reaped, Service, Wake, CALL_TIMEOUT},
    var::{as_u32, clean},
    AuthProgress, AuthRequest, Command, Event, Secret,
};

const AUTHORITY: &str = "org.freedesktop.PolicyKit1";
const AUTHORITY_PATH: &str = "/org/freedesktop/PolicyKit1/Authority";
const AUTHORITY_INTERFACE: &str = "org.freedesktop.PolicyKit1.Authority";
const AGENT_PATH: &str = "/org/boswas/shell/AuthenticationAgent";

/// Requests that wait while one is shown. polkit sends one per action; a
/// queue this long means something is asking in a loop.
const MAX_QUEUE: usize = 16;
/// Wrong answers accepted for one request before it is failed.
const MAX_ATTEMPTS: u32 = 5;
/// The longest line read from the helper.
const MAX_LINE: usize = 4096;
const MAX_USERS: usize = 16;

pub(crate) struct Auth {
    ctx: Ctx,
    ex: Exec,
    /// The connection, kept here so that the agent can be unregistered
    /// when the service is stopped in the middle of waiting.
    bus: Option<Bus>,
    session: Option<OwnSession>,
    registered: bool,
    /// The connection of polkit when the agent was registered; a new one
    /// knows nothing of the agent or of the requests of the old one.
    authority: Option<String>,
    current: Option<Active>,
    queue: VecDeque<Request>,
    /// Counts the helpers started, to tell an old one's output from the
    /// current one's.
    generation: u64,
}

/// One `BeginAuthentication` call.
struct Request {
    cookie: String,
    action_id: String,
    message: String,
    icon_name: String,
    users: Vec<String>,
    /// Answers the call.
    done: Sender<Outcome>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Outcome {
    Authorized,
    Cancelled,
    Failed,
}

/// The request the shell shows.
struct Active {
    request: Request,
    /// The user the helper authenticates: the first of the request's
    /// users until the shell chooses another.
    user: String,
    helper: Option<Helper>,
    /// The helper asked something and waits for the answer.
    awaiting: bool,
    attempts: u32,
    /// What PAM said went wrong in this attempt.
    last_error: Option<String>,
}

/// A running helper. Dropping it kills the program.
struct Helper {
    child: Child,
    input: ChildStdin,
}

impl Drop for Helper {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

enum Msg {
    Begin(Box<Request>),
    /// polkit withdrew a request.
    Cancel { cookie: String },
    Helper { generation: u64, line: String },
    HelperEnded { generation: u64 },
    /// polkit or logind came or went.
    Owner,
    Lost,
}

// --- the agent object -------------------------------------------------------------

struct Agent {
    inbox: Sender<Msg>,
    users_file: std::path::PathBuf,
}

#[derive(Debug, zbus::DBusError)]
#[zbus(prefix = "org.freedesktop.PolicyKit1.Error")]
enum AgentError {
    #[zbus(error)]
    ZBus(zbus::Error),
    Failed(String),
    Cancelled(String),
    NotAuthorized(String),
}

/// A request to type a password is only believed from polkit itself: the
/// caller must be the connection that owns polkit's name right now (the
/// bus is asked each time, so there is no moment in which an old answer
/// is trusted).
async fn from_authority(conn: &Connection, header: &Header<'_>) -> Result<(), AgentError> {
    let authority = name_owner(conn, AUTHORITY).await;
    let trusted = match (header.sender(), authority) {
        (Some(sender), Some(authority)) => sender.as_str() == authority,
        _ => false,
    };
    if trusted {
        Ok(())
    } else {
        tracing::warn!("auth: an authentication request that does not come from polkit was refused");
        Err(AgentError::NotAuthorized("only the polkit authority may call the agent".into()))
    }
}

#[zbus::interface(name = "org.freedesktop.PolicyKit1.AuthenticationAgent")]
impl Agent {
    /// Returns when the attempt is over: authenticated, cancelled or failed.
    #[allow(clippy::too_many_arguments)]
    async fn begin_authentication(
        &self,
        action_id: String,
        message: String,
        icon_name: String,
        _details: HashMap<String, String>,
        cookie: String,
        identities: Vec<(String, HashMap<String, OwnedValue>)>,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> Result<(), AgentError> {
        from_authority(conn, &header).await?;
        // The cookie goes to the helper as one line.
        if cookie.is_empty() || cookie.len() > 1024 || cookie.chars().any(char::is_control) {
            return Err(AgentError::Failed("the cookie is not usable".into()));
        }
        let uids: Vec<u32> = identities
            .iter()
            .filter(|(kind, _)| kind == "unix-user")
            .filter_map(|(_, details)| details.get("uid").and_then(|uid| as_u32(uid)))
            .take(MAX_USERS)
            .collect();
        let users = user_names(&self.users_file, &uids, own_uid()).await;
        if users.is_empty() {
            return Err(AgentError::Failed("there is nobody to authenticate as".into()));
        }
        let (done, outcome) = async_channel::bounded(1);
        let request = Request {
            cookie,
            action_id: clean(&action_id, 256, false),
            message: clean(&message, 1024, true),
            icon_name: clean(&icon_name, 128, false),
            users,
            done,
        };
        let stopping = || AgentError::Cancelled("the agent is stopping".into());
        self.inbox.send(Msg::Begin(Box::new(request))).await.map_err(|_| stopping())?;
        match outcome.recv().await {
            Ok(Outcome::Authorized) => Ok(()),
            Ok(Outcome::Failed) => Err(AgentError::Failed("authentication failed".into())),
            Ok(Outcome::Cancelled) | Err(_) => Err(AgentError::Cancelled("authentication was cancelled".into())),
        }
    }

    async fn cancel_authentication(
        &self,
        cookie: String,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> Result<(), AgentError> {
        from_authority(conn, &header).await?;
        let _ = self.inbox.send(Msg::Cancel { cookie }).await;
        Ok(())
    }
}

// --- the service ------------------------------------------------------------------

impl Service for Auth {
    fn new(ctx: Ctx, ex: Exec) -> Self {
        Self {
            ctx,
            ex,
            bus: None,
            session: None,
            registered: false,
            authority: None,
            current: None,
            queue: VecDeque::new(),
            generation: 0,
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
                        // `shutdown` unregisters.
                        Ok(true) => return,
                        Ok(false) => {}
                        Err(error) => tracing::debug!(%error, "auth: the system bus stopped answering"),
                    }
                    self.abandon_all();
                    self.registered = false;
                    self.bus = None;
                }
                Err(error) => tracing::debug!(%error, "auth: no system bus"),
            }
            if let IdleEnd::Closed = idle(&commands, backoff.next(), |_| false).await {
                return;
            }
        }
    }

    async fn shutdown(&mut self) {
        self.abandon_all();
        if let Some(bus) = self.bus.take() {
            self.unregister(&bus.conn).await;
        }
    }
}

impl Auth {
    async fn serve(&mut self, conn: &Connection, commands: &Receiver<Command>) -> CallResult<bool> {
        let (inbox, messages) = async_channel::unbounded::<Msg>();
        let mut subscriptions = Vec::new();
        for name in [AUTHORITY, LOGIN1] {
            subscriptions.push(subscribe(conn, &self.ex, owner_rule(name)?, &inbox, |_| Some(Msg::Owner), || Msg::Lost).await?);
        }
        conn.object_server()
            .at(
                AGENT_PATH,
                Agent { inbox: inbox.clone(), users_file: self.ctx.env.users_file.clone() },
            )
            .await?;
        self.register(conn).await;
        loop {
            match wake(commands, &messages, None).await {
                Wake::Closed => return Ok(true),
                Wake::Timer => {}
                Wake::Message(Msg::Lost) => return Ok(false),
                Wake::Message(Msg::Owner) => {
                    // Whoever asked is gone if polkit is; and a new polkit
                    // (or a session that only now exists) needs the agent
                    // registered again.
                    let authority = name_owner(conn, AUTHORITY).await;
                    if self.authority != authority {
                        self.abandon_all();
                        self.registered = false;
                    }
                    if !self.registered {
                        self.register(conn).await;
                    }
                }
                Wake::Message(Msg::Begin(request)) => self.begin(*request, &inbox),
                Wake::Message(Msg::Cancel { cookie }) => self.cancel(&cookie, &inbox),
                Wake::Message(Msg::Helper { generation, line }) => {
                    if generation == self.generation {
                        self.helper_said(&line, &inbox);
                    }
                }
                Wake::Message(Msg::HelperEnded { generation }) => {
                    if generation == self.generation && self.current.as_ref().is_some_and(|active| active.helper.is_some()) {
                        // It ended without a verdict.
                        self.attempt_failed(&inbox);
                    }
                }
                Wake::Command(Command::AuthRespond { cookie, response }) => self.respond(&cookie, &response),
                Wake::Command(Command::AuthCancel { cookie }) => self.cancel(&cookie, &inbox),
                Wake::Command(Command::AuthChooseUser { cookie, user }) => self.choose_user(&cookie, &user, &inbox),
                Wake::Command(_) => {}
            }
        }
    }

    /// Registers the agent for our session. Without a session or without
    /// polkit there is nothing to register with; `Msg::Owner` tries again.
    async fn register(&mut self, conn: &Connection) {
        self.session = own_session(conn, &self.ctx.env).await;
        let Some(session) = &self.session else {
            tracing::debug!("auth: this process is in no logind session; no agent is registered");
            return;
        };
        let subject = ("unix-session", HashMap::from([("session-id", Value::from(session.id.as_str()))]));
        // Set before the answer is in: if the service is stopped while it
        // waits, the agent is unregistered all the same.
        self.registered = true;
        let registration = call_raw(
            conn,
            CALL_TIMEOUT,
            AUTHORITY,
            AUTHORITY_PATH,
            AUTHORITY_INTERFACE,
            "RegisterAuthenticationAgent",
            &(subject, locale().as_str(), AGENT_PATH),
        )
        .await;
        match registration {
            Ok(_) => {
                self.authority = name_owner(conn, AUTHORITY).await;
                tracing::debug!("auth: registered as the session's authentication agent");
            }
            Err(error) => {
                self.registered = false;
                self.authority = None;
                tracing::debug!(%error, "auth: polkit did not take the agent");
            }
        }
    }

    async fn unregister(&mut self, conn: &Connection) {
        let (true, Some(session)) = (self.registered, &self.session) else {
            return;
        };
        let subject = ("unix-session", HashMap::from([("session-id", Value::from(session.id.as_str()))]));
        let _ = call_raw(
            conn,
            Duration::from_millis(500),
            AUTHORITY,
            AUTHORITY_PATH,
            AUTHORITY_INTERFACE,
            "UnregisterAuthenticationAgent",
            &(subject, AGENT_PATH),
        )
        .await;
        self.registered = false;
    }

    fn progress(&self, cookie: &str, progress: AuthProgress) {
        self.ctx.emit(Event::AuthProgress { cookie: cookie.to_owned(), progress });
    }

    fn begin(&mut self, request: Request, inbox: &Sender<Msg>) {
        let duplicate = self.current.as_ref().is_some_and(|active| active.request.cookie == request.cookie)
            || self.queue.iter().any(|queued| queued.cookie == request.cookie);
        if duplicate || self.queue.len() >= MAX_QUEUE {
            let _ = request.done.try_send(Outcome::Failed);
            return;
        }
        self.queue.push_back(request);
        self.next(inbox);
    }

    /// Shows the next request if none is shown.
    fn next(&mut self, inbox: &Sender<Msg>) {
        while self.current.is_none() {
            let Some(request) = self.queue.pop_front() else { return };
            // polkit may have given up on it while it waited.
            if request.done.is_closed() {
                continue;
            }
            self.ctx.emit(Event::AuthRequest(AuthRequest {
                cookie: request.cookie.clone(),
                action_id: request.action_id.clone(),
                message: request.message.clone(),
                icon_name: request.icon_name.clone(),
                users: request.users.clone(),
            }));
            let user = request.users.first().cloned().unwrap_or_default();
            self.current = Some(Active { request, user, helper: None, awaiting: false, attempts: 0, last_error: None });
            if !self.start_helper(inbox) {
                self.finish(Outcome::Failed, inbox);
            }
        }
    }

    /// Starts the helper for the current request's user and gives it the
    /// cookie; a helper that still runs is killed. False when it could not
    /// be started.
    fn start_helper(&mut self, inbox: &Sender<Msg>) -> bool {
        self.generation += 1;
        let generation = self.generation;
        let Some(active) = &mut self.current else { return false };
        active.helper = None;
        active.awaiting = false;
        active.last_error = None;
        if !plausible_user(&active.user) {
            return false;
        }
        let spawned = Process::new(&self.ctx.env.polkit_helper)
            .arg(&active.user)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn();
        let mut child = match spawned {
            Ok(child) => child,
            Err(error) => {
                tracing::warn!(%error, "auth: polkit's helper could not be started");
                return false;
            }
        };
        let (Some(mut input), Some(output)) = (child.stdin.take(), child.stdout.take()) else {
            let _ = child.kill();
            let _ = child.wait();
            return false;
        };
        // The helper reads the cookie first.
        let sent = input.write_all(active.request.cookie.as_bytes()).and_then(|()| input.write_all(b"\n")).and_then(|()| input.flush());
        let inbox = inbox.clone();
        let reader = std::thread::Builder::new().name("boswas-auth-reader".into()).spawn(move || {
            read_helper(output, |line| inbox.try_send(Msg::Helper { generation, line }).is_ok());
            let _ = inbox.try_send(Msg::HelperEnded { generation });
        });
        let helper = Helper { child, input };
        if sent.is_err() || reader.is_err() {
            return false;
        }
        active.helper = Some(helper);
        true
    }

    fn helper_said(&mut self, line: &str, inbox: &Sender<Msg>) {
        let Some(active) = &mut self.current else { return };
        let cookie = active.request.cookie.clone();
        let text = |rest: &str| clean(&unescape(rest), 512, true);
        if let Some(rest) = line.strip_prefix("PAM_PROMPT_ECHO_OFF") {
            active.awaiting = true;
            self.progress(&cookie, AuthProgress::Prompt { text: text(rest), echo: false });
        } else if let Some(rest) = line.strip_prefix("PAM_PROMPT_ECHO_ON") {
            active.awaiting = true;
            self.progress(&cookie, AuthProgress::Prompt { text: text(rest), echo: true });
        } else if let Some(rest) = line.strip_prefix("PAM_ERROR_MSG") {
            let message = text(rest);
            active.last_error = Some(message.clone());
            self.progress(&cookie, AuthProgress::Info(message));
        } else if let Some(rest) = line.strip_prefix("PAM_TEXT_INFO") {
            self.progress(&cookie, AuthProgress::Info(text(rest)));
        } else if line.trim_end() == "SUCCESS" {
            // The helper has told polkit; the call can return.
            self.finish(Outcome::Authorized, inbox);
        } else if line.trim_end() == "FAILURE" {
            self.attempt_failed(inbox);
        } else {
            tracing::debug!("auth: the helper printed a line that is not part of its protocol");
        }
    }

    /// The attempt failed: the request stays open and a new helper asks
    /// again, until too many attempts have failed.
    fn attempt_failed(&mut self, inbox: &Sender<Msg>) {
        let Some(active) = &mut self.current else { return };
        active.attempts += 1;
        let cookie = active.request.cookie.clone();
        let reason = active.last_error.take().filter(|text| !text.is_empty()).unwrap_or_else(|| "Authentication failed.".into());
        if active.attempts >= MAX_ATTEMPTS {
            self.progress(&cookie, AuthProgress::Failed(reason));
            self.finish(Outcome::Failed, inbox);
            return;
        }
        self.progress(&cookie, AuthProgress::Failed(reason));
        if !self.start_helper(inbox) {
            self.finish(Outcome::Failed, inbox);
        }
    }

    /// Writes the answer to the helper. The copy made for the write is
    /// wiped; the command's own `Secret` wipes itself when dropped.
    fn respond(&mut self, cookie: &str, response: &Secret) {
        let Some(active) = &mut self.current else { return };
        if active.request.cookie != cookie || !active.awaiting {
            return;
        }
        let Some(helper) = &mut active.helper else { return };
        // The helper reads one line: anything after a line break would be
        // taken as the answer to its next question.
        let answer = response.expose().split(['\n', '\r', '\0']).next().unwrap_or("");
        let mut line = Zeroizing::new(Vec::with_capacity(answer.len() + 1));
        line.extend_from_slice(answer.as_bytes());
        line.push(b'\n');
        active.awaiting = false;
        if helper.input.write_all(&line).and_then(|()| helper.input.flush()).is_err() {
            // The helper is gone; its end is reported by the reader.
            tracing::debug!("auth: the helper did not take the answer");
        }
    }

    /// The person wants to authenticate as another of the users polkit
    /// offered: the running helper is killed and one for that user asks
    /// anew. Anybody polkit did not offer is refused.
    fn choose_user(&mut self, cookie: &str, user: &str, inbox: &Sender<Msg>) {
        let Some(active) = &mut self.current else { return };
        if active.request.cookie != cookie {
            return;
        }
        if !active.request.users.iter().any(|offered| offered == user) {
            tracing::warn!("auth: a user polkit did not offer was chosen; ignored");
            return;
        }
        active.user = user.to_owned();
        if !self.start_helper(inbox) {
            self.finish(Outcome::Failed, inbox);
        }
    }

    /// The shell or polkit withdrew a request.
    fn cancel(&mut self, cookie: &str, inbox: &Sender<Msg>) {
        if self.current.as_ref().is_some_and(|active| active.request.cookie == cookie) {
            self.finish(Outcome::Cancelled, inbox);
        } else if let Some(index) = self.queue.iter().position(|queued| queued.cookie == cookie) {
            if let Some(request) = self.queue.remove(index) {
                let _ = request.done.try_send(Outcome::Cancelled);
            }
        }
    }

    /// Ends the current request: the helper is killed, the shell closes
    /// its dialog, polkit's call returns, the next request is shown.
    fn finish(&mut self, outcome: Outcome, inbox: &Sender<Msg>) {
        if let Some(active) = self.current.take() {
            drop(active.helper);
            self.progress(&active.request.cookie, AuthProgress::Done { authorized: outcome == Outcome::Authorized });
            let _ = active.request.done.try_send(outcome);
        }
        self.next(inbox);
    }

    /// polkit or the bus is gone, or the service stops: nothing can be
    /// authenticated any more.
    fn abandon_all(&mut self) {
        if let Some(active) = self.current.take() {
            drop(active.helper);
            self.progress(&active.request.cookie, AuthProgress::Done { authorized: false });
            let _ = active.request.done.try_send(Outcome::Cancelled);
        }
        for request in self.queue.drain(..) {
            let _ = request.done.try_send(Outcome::Cancelled);
        }
    }
}

// --- the helper's protocol --------------------------------------------------------

/// Reads the helper's output line by line, each at most `MAX_LINE` bytes.
fn read_helper(output: impl Read, mut sink: impl FnMut(String) -> bool) {
    let mut reader = BufReader::new(output);
    loop {
        let mut line = Vec::new();
        match (&mut reader).take(MAX_LINE as u64).read_until(b'\n', &mut line) {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
        if line.last() != Some(&b'\n') && line.len() >= MAX_LINE {
            // A line without end is not the helper's protocol.
            return;
        }
        while matches!(line.last(), Some(b'\n') | Some(b'\r')) {
            line.pop();
        }
        if !sink(String::from_utf8_lossy(&line).into_owned()) {
            return;
        }
    }
}

/// Undoes the escaping the helper applies to PAM's text (`\n`, `\t`,
/// `\\`, `\"`, octal `\NNN`), and drops the space after the keyword.
fn unescape(text: &str) -> String {
    let text = text.strip_prefix(' ').unwrap_or(text);
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'\\' || index + 1 == bytes.len() {
            out.push(bytes[index]);
            index += 1;
            continue;
        }
        index += 1;
        match bytes[index] {
            b'n' => out.push(b'\n'),
            b't' => out.push(b'\t'),
            b'r' => out.push(b'\r'),
            b'b' => out.push(8),
            b'f' => out.push(12),
            b'v' => out.push(11),
            b'0'..=b'7' => {
                let mut value = 0u32;
                let mut digits = 0;
                while digits < 3 && index < bytes.len() && (b'0'..=b'7').contains(&bytes[index]) {
                    value = value * 8 + u32::from(bytes[index] - b'0');
                    index += 1;
                    digits += 1;
                }
                out.push(value as u8);
                continue;
            }
            other => out.push(other),
        }
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// The locale polkit should phrase its messages in.
fn locale() -> String {
    let chosen = ["LC_ALL", "LC_MESSAGES", "LANG"].into_iter().find_map(|name| std::env::var(name).ok().filter(|value| !value.is_empty()));
    match chosen {
        Some(locale) if locale.len() <= 64 && locale.chars().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '@' | '-')) => locale,
        _ => "C".to_owned(),
    }
}

// --- users ------------------------------------------------------------------------

/// The user this process runs as, from the owner of its `/proc` entry.
fn own_uid() -> Option<u32> {
    std::fs::metadata("/proc/self").ok().map(|meta| meta.uid())
}

/// A name the helper can be given as an argument.
fn plausible_user(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && !name.starts_with('-')
        && name.chars().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.' | '@' | '$'))
}

/// The name of `uid` in a file of the `/etc/passwd` format.
fn name_in(users: &str, uid: u32) -> Option<String> {
    users.lines().find_map(|line| {
        let mut fields = line.split(':');
        let name = fields.next()?;
        let _hash = fields.next()?;
        (fields.next()?.parse::<u32>().ok()? == uid && plausible_user(name)).then(|| name.to_owned())
    })
}

/// The names of the users polkit lets authenticate, the session's own
/// user first. Users the local file does not list (directory services)
/// are looked up with `getent`.
async fn user_names(users_file: &Path, uids: &[u32], own: Option<u32>) -> Vec<String> {
    let mut contents = String::new();
    if let Ok(file) = std::fs::File::open(users_file) {
        let _ = file.take(8 * 1024 * 1024).read_to_string(&mut contents);
    }
    let mut ordered: Vec<u32> = Vec::new();
    for uid in own.into_iter().filter(|own| uids.contains(own)).chain(uids.iter().copied()) {
        if !ordered.contains(&uid) {
            ordered.push(uid);
        }
    }
    let mut names = Vec::new();
    for uid in ordered {
        let name = match name_in(&contents, uid) {
            Some(name) => Some(name),
            None => getent(uid).await,
        };
        if let Some(name) = name.filter(|name| !names.contains(name)) {
            names.push(name);
        }
    }
    names
}

/// Asks the name service switch for a user (`getent passwd UID`).
async fn getent(uid: u32) -> Option<String> {
    let child = Process::new("/usr/bin/getent")
        .arg("passwd")
        .arg(uid.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut child = Reaped(child);
    let started = Instant::now();
    loop {
        match child.0.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if started.elapsed() < Duration::from_secs(2) => {
                Timer::after(Duration::from_millis(5)).await;
            }
            _ => return None,
        }
    }
    let mut output = String::new();
    child.0.stdout.take()?.take(4096).read_to_string(&mut output).ok()?;
    name_in(&output, uid)
}

#[cfg(test)]
mod tests {
    use super::*;

    const USERS: &str = "root:x:0:0:root:/root:/bin/bash\n# a comment\nbroken line\nada:x:1000:1000:Ada L,,,:/home/ada:/bin/bash\n-rf:x:1001:1001::/:/bin/sh\nbad name:x:1002:1002::/:/bin/sh\nlate:x:1000:1000::/:/bin/sh\n";

    #[test]
    fn names_come_from_the_user_file() {
        assert_eq!(name_in(USERS, 0).as_deref(), Some("root"));
        assert_eq!(name_in(USERS, 1000).as_deref(), Some("ada"), "the first entry of a uid counts");
        assert_eq!(name_in(USERS, 1001), None, "a name that would read as an option is refused");
        assert_eq!(name_in(USERS, 1002), None);
        assert_eq!(name_in(USERS, 4242), None);
        assert_eq!(name_in("", 0), None);
        assert!(plausible_user("svc_backup$"));
        assert!(plausible_user("first.last@example.org"));
        assert!(!plausible_user(""));
        assert!(!plausible_user("a/b"));
        assert!(!plausible_user(&"n".repeat(65)));
    }

    #[test]
    fn the_sessions_own_user_comes_first() {
        let file = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(file.path(), USERS).unwrap();
        let names = |uids: &[u32], own| async_io::block_on(user_names(file.path(), uids, own));
        assert_eq!(names(&[0, 1000], Some(1000)), ["ada", "root"]);
        assert_eq!(names(&[0, 1000], Some(0)), ["root", "ada"]);
        assert_eq!(names(&[0, 1000], Some(555)), ["root", "ada"]);
        assert_eq!(names(&[1000, 1000, 0, 0], None), ["ada", "root"]);
        assert!(names(&[], Some(0)).is_empty());
    }

    #[test]
    fn helper_text_is_unescaped() {
        assert_eq!(unescape(" Password: "), "Password: ");
        assert_eq!(unescape(" line one\\nline two\\ttab"), "line one\nline two\ttab");
        assert_eq!(unescape(" back\\\\slash and \\\"quotes\\\""), "back\\slash and \"quotes\"");
        assert_eq!(unescape(" caf\\303\\251"), "café");
        assert_eq!(unescape(" trailing\\"), "trailing\\");
        assert_eq!(unescape(""), "");
        assert_eq!(unescape(" \\777\\8"), "\u{fffd}8");
    }

    #[test]
    fn helper_lines_are_bounded() {
        let mut lines = Vec::new();
        read_helper("PAM_PROMPT_ECHO_OFF Password: \r\nSUCCESS\nno newline at the end".as_bytes(), |line| {
            lines.push(line);
            true
        });
        assert_eq!(lines, ["PAM_PROMPT_ECHO_OFF Password: ", "SUCCESS", "no newline at the end"]);

        // A line without end stops the reading instead of growing.
        let endless = vec![b'x'; MAX_LINE * 3];
        let mut count = 0;
        read_helper(&endless[..], |_| {
            count += 1;
            true
        });
        assert_eq!(count, 0);
        // The sink can stop it.
        let mut count = 0;
        read_helper("a\nb\nc\n".as_bytes(), |_| {
            count += 1;
            count < 2
        });
        assert_eq!(count, 2);
    }

    #[test]
    fn locale_is_a_locale_name() {
        let chosen = locale();
        assert!(!chosen.is_empty() && chosen.len() <= 64);
        assert!(chosen.chars().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '@' | '-')));
    }
}
