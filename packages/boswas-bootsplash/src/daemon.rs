//! The boot screen daemon: one single-threaded event loop.
//!
//! It owns the display, the console keyboard and the control socket. Each
//! turn of the loop waits for whichever comes first: a client request, a
//! key, a termination signal, or the time for the next animation frame.
//! Clients are local root processes (checked with `SO_PEERCRED`).

use std::collections::VecDeque;
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use rustix::event::{poll, PollFd, PollFlags, Timespec};

use crate::client::DEFAULT_SOCKET;
use crate::editor::{Key, KeyDecoder, Passphrase};
use crate::output::headless::HeadlessOutput;
use crate::output::{open_display, Output};
use crate::proto::{self, FrameReader, Request, Response};
use crate::sandbox;
use crate::scene::{Mode, PromptView, Scene, Tone, View};
use crate::sys;
use crate::tty::{Console, DEFAULT_TTY};

/// Seconds for one turn of the progress arc.
const TURN_SECS: f32 = 1.6;
/// Time between animation frames (30 frames a second at most).
const FRAME: Duration = Duration::from_millis(33);
/// How long an informational message stays on screen.
const INFO_TTL: Duration = Duration::from_secs(6);
/// How long an error message stays on screen.
const ERROR_TTL: Duration = Duration::from_secs(30);
/// How long the display may be missing before the text console takes over.
const LOST_GRACE: Duration = Duration::from_secs(2);
/// Most clients served at once, and most prompts waiting.
const MAX_CLIENTS: usize = 16;
const MAX_ASKS: usize = 8;

/// How the daemon was asked to run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Options {
    pub mode: Mode,
    /// Animate the progress arc.
    pub animate: bool,
    /// Start even if the kernel command line does not ask for a splash.
    pub force: bool,
    pub socket: PathBuf,
    /// Console to read keys from (default: the foreground virtual console).
    pub tty: Option<PathBuf>,
    /// Draw to memory at this size and save snapshots in the directory.
    pub headless: Option<(u32, u32, PathBuf)>,
    /// Run the worker in the foreground (tests, debugging).
    pub foreground: bool,
    pub sandbox: bool,
    /// How long to wait for a graphics device to appear, in milliseconds.
    pub wait_ms: u64,
    /// Enter the sandbox, check what it denies, report and exit.
    pub self_test: bool,
    /// Internal: which stage of the start-up chain this process is.
    pub stage: Stage,
}

/// The daemon starts in three steps: the launcher checks the command line
/// and waits for readiness, the supervisor restores the console if the
/// worker dies, and the worker is the boot screen itself.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stage {
    Launcher,
    Supervisor,
    Worker,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            mode: Mode::Boot,
            animate: true,
            force: false,
            socket: PathBuf::from(DEFAULT_SOCKET),
            tty: None,
            headless: None,
            foreground: false,
            sandbox: true,
            wait_ms: 1500,
            self_test: false,
            stage: Stage::Launcher,
        }
    }
}

pub const USAGE: &str = "\
Usage: boswas-bootsplashd [OPTIONS]

  --mode=boot|shutdown|reboot  which screen to show (default: boot)
  --static                     do not animate
  --force                      start even without `splash` on the kernel command line
  --socket=PATH                control socket (default: /run/boswas-bootsplash/socket)
  --tty=PATH                   console to read keys from (default: /dev/tty0)
  --wait=MILLISECONDS          how long to wait for a display (default: 1500)
  --headless=WIDTHxHEIGHT      draw to memory instead of a display (needs --out)
  --out=DIR                    where headless snapshots are written
  --foreground                 do not detach; run the boot screen in this process
  --no-sandbox                 keep all privileges (debugging only)
  --self-test                  enter the sandbox, report what it enforces, exit
  --help, --version
";

/// Parse the daemon's arguments (without the program name).
pub fn parse_args(args: &[String]) -> Result<Options, String> {
    let mut opts = Options::default();
    let mut headless_size = None;
    let mut out_dir = None;
    let mut i = 0;
    while i < args.len() {
        let (name, inline) = match args[i].split_once('=') {
            Some((name, value)) => (name, Some(value.to_owned())),
            None => (args[i].as_str(), None),
        };
        let value = |i: &mut usize| -> Result<String, String> {
            if let Some(v) = &inline {
                return Ok(v.clone());
            }
            *i += 1;
            args.get(*i).cloned().ok_or_else(|| format!("{name} needs a value"))
        };
        match name {
            "--mode" => {
                let v = value(&mut i)?;
                opts.mode = Mode::parse(&v).ok_or_else(|| format!("unknown mode {v}"))?;
            }
            "--socket" => opts.socket = PathBuf::from(value(&mut i)?),
            "--tty" => opts.tty = Some(PathBuf::from(value(&mut i)?)),
            "--out" => out_dir = Some(PathBuf::from(value(&mut i)?)),
            "--wait" => opts.wait_ms = value(&mut i)?.parse().map_err(|_| "--wait takes milliseconds".to_string())?,
            "--headless" => {
                let v = value(&mut i)?;
                let size = v.split_once('x').and_then(|(w, h)| Some((w.parse::<u32>().ok()?, h.parse::<u32>().ok()?)));
                headless_size = Some(size.ok_or_else(|| "--headless takes WIDTHxHEIGHT".to_string())?);
            }
            "--static" if inline.is_none() => opts.animate = false,
            "--force" if inline.is_none() => opts.force = true,
            "--foreground" if inline.is_none() => opts.foreground = true,
            "--no-sandbox" if inline.is_none() => opts.sandbox = false,
            "--self-test" if inline.is_none() => opts.self_test = true,
            "--supervise" if inline.is_none() => opts.stage = Stage::Supervisor,
            "--worker" if inline.is_none() => opts.stage = Stage::Worker,
            // Accepted for people used to plymouthd's command line.
            "--attach-to-session" | "--no-daemon" if inline.is_none() => {}
            "--pid-file" => {
                value(&mut i)?;
            }
            other => return Err(format!("unknown option {other}")),
        }
        i += 1;
    }
    match (headless_size, out_dir) {
        (Some((w, h)), Some(dir)) => opts.headless = Some((w, h, dir)),
        (None, None) => {}
        _ => return Err("--headless and --out go together".into()),
    }
    if opts.socket.as_os_str().is_empty() || !opts.socket.is_absolute() {
        return Err("--socket must be an absolute path".into());
    }
    if opts.foreground {
        opts.stage = Stage::Worker;
    }
    Ok(opts)
}

/// The pid file next to the control socket.
pub fn pid_path(socket: &Path) -> PathBuf {
    socket.with_file_name("pid")
}

/// Is a message about something going wrong?
pub fn tone_of(text: &str) -> Tone {
    let lower = text.to_ascii_lowercase();
    let bad = ["error", "fail", "incorrect", "wrong", "bad pass", "invalid", "denied", "exceeded"];
    if bad.iter().any(|word| lower.contains(word)) {
        Tone::Error
    } else {
        Tone::Info
    }
}

struct Client {
    id: u64,
    stream: UnixStream,
    reader: FrameReader,
}

struct Ask {
    client: u64,
    prompt: String,
}

struct Message {
    text: String,
    tone: Tone,
    expires: Instant,
}

/// Finds a display to draw on.
type Probe = Box<dyn FnMut() -> io::Result<Box<dyn Output>>>;

/// The running boot screen.
pub struct Daemon {
    opts: Options,
    output: Option<Box<dyn Output>>,
    /// Looks for a display again after the current one was lost.
    probe: Probe,
    lost_grace: Duration,
    scenes: Vec<Scene>,
    console: Option<Console>,
    listener: UnixListener,
    signals: OwnedFd,
    clients: Vec<Client>,
    next_client: u64,
    asks: VecDeque<Ask>,
    passphrase: Passphrase,
    decoder: KeyDecoder,
    mode: Mode,
    message: Option<Message>,
    progress: Option<u8>,
    caps_lock: bool,
    hidden: bool,
    /// Hidden by the daemon itself because the display was lost, not by a
    /// key or a request: shown again as soon as a display is back.
    auto_hidden: bool,
    lost_since: Option<Instant>,
    started: Instant,
    last_frame: Instant,
    next_probe: Instant,
    quit: Option<Option<u16>>,
}

fn bind(socket: &Path) -> io::Result<UnixListener> {
    if let Some(dir) = socket.parent() {
        if !dir.exists() {
            std::fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
        }
    }
    // A socket file left by a previous instance that no longer answers.
    if socket.exists() {
        if UnixStream::connect(socket).is_ok() {
            return Err(io::Error::new(io::ErrorKind::AddrInUse, "a boot screen is already running"));
        }
        std::fs::remove_file(socket)?;
    }
    let listener = UnixListener::bind(socket)?;
    std::fs::set_permissions(socket, std::fs::Permissions::from_mode(0o600))?;
    listener.set_nonblocking(true)?;
    Ok(listener)
}

fn peer_allowed(stream: &UnixStream) -> bool {
    match rustix::net::sockopt::socket_peercred(stream) {
        Ok(cred) => cred.uid.is_root() || cred.uid == rustix::process::geteuid(),
        Err(_) => false,
    }
}

impl Daemon {
    /// Open everything, show the first frame and drop privileges. On error
    /// nothing is left behind (console and display are restored on drop).
    pub fn start(opts: Options) -> Result<Daemon, String> {
        let signals =
            sys::signal_fd(&[libc::SIGTERM, libc::SIGINT, libc::SIGHUP]).map_err(|e| format!("signals: {e}"))?;

        // The console comes first: without a keyboard there can be no
        // passphrase prompt, and then the text console must do the asking.
        let tty_path = opts.tty.clone().unwrap_or_else(|| PathBuf::from(DEFAULT_TTY));
        let console = match Console::open(&tty_path) {
            Ok(console) => Some(console),
            Err(e) if opts.headless.is_some() && opts.tty.is_none() => {
                let _ = e;
                None
            }
            Err(e) => return Err(format!("console {}: {e}", tty_path.display())),
        };

        let output: Box<dyn Output> = match &opts.headless {
            Some((w, h, dir)) => {
                Box::new(HeadlessOutput::new(*w, *h, dir.clone()).map_err(|e| format!("headless output: {e}"))?)
            }
            None => open_display(Duration::from_millis(opts.wait_ms)).map_err(|e| e.to_string())?,
        };

        let listener = bind(&opts.socket).map_err(|e| format!("socket {}: {e}", opts.socket.display()))?;
        let pid_file = pid_path(&opts.socket);
        std::fs::write(&pid_file, format!("{}\n", std::process::id()))
            .map_err(|e| format!("{}: {e}", pid_file.display()))?;

        let probe: Probe = if opts.headless.is_some() {
            Box::new(|| Err(io::Error::new(io::ErrorKind::NotFound, "headless")))
        } else {
            Box::new(|| open_display(Duration::ZERO))
        };
        let mut daemon = Daemon::assemble(opts, output, console, listener, signals, probe);
        if let Some(console) = daemon.console.as_mut() {
            console.set_raw().map_err(|e| format!("console: {e}"))?;
            console.set_graphics(true);
        }
        daemon.render();
        // At boot a screen that cannot be drawn must not claim the
        // passphrase prompt: fail, and the text console asks instead. At
        // shutdown the display may still be held by the session that is
        // closing; keep trying.
        if daemon.output.is_none() && daemon.mode == Mode::Boot {
            return Err("the display could not be driven".into());
        }

        if daemon.opts.sandbox {
            let mut allowed = sandbox::Allowed::default();
            if let Some(dir) = daemon.opts.socket.parent() {
                allowed.state.push(dir.to_path_buf());
            }
            match &daemon.opts.headless {
                Some((_, _, dir)) => allowed.state.push(dir.clone()),
                None => {
                    sandbox::prepare_device_dir();
                    allowed.devices = sandbox::display_devices();
                }
            }
            sandbox::enter(&allowed);
        }
        Ok(daemon)
    }

    /// Put a daemon together from its parts (nothing is drawn or changed yet).
    fn assemble(
        opts: Options,
        output: Box<dyn Output>,
        console: Option<Console>,
        listener: UnixListener,
        signals: OwnedFd,
        probe: Probe,
    ) -> Daemon {
        let now = Instant::now();
        let mut daemon = Daemon {
            mode: opts.mode,
            opts,
            output: Some(output),
            probe,
            lost_grace: LOST_GRACE,
            scenes: Vec::new(),
            console,
            listener,
            signals,
            clients: Vec::new(),
            next_client: 1,
            asks: VecDeque::new(),
            passphrase: Passphrase::new(),
            decoder: KeyDecoder::new(),
            message: None,
            progress: None,
            caps_lock: false,
            hidden: false,
            auto_hidden: false,
            lost_since: None,
            started: now,
            last_frame: now,
            next_probe: now,
            quit: None,
        };
        daemon.build_scenes();
        daemon
    }

    fn build_scenes(&mut self) {
        self.scenes = match &self.output {
            Some(output) => output.sizes().into_iter().map(|(w, h)| Scene::new(w, h)).collect(),
            None => Vec::new(),
        };
    }

    fn view(&self) -> View {
        let phase = if self.opts.animate { (self.started.elapsed().as_secs_f32() / TURN_SECS).fract() } else { 0.0 };
        View {
            mode: self.mode,
            prompt: self.asks.front().map(|ask| PromptView {
                text: ask.prompt.clone(),
                dots: self.passphrase.chars(),
                caps_lock: self.caps_lock,
            }),
            message: self.message.as_ref().map(|m| (m.text.clone(), m.tone)),
            progress: self.progress,
            phase,
        }
    }

    /// Draw what changed and send it to the display.
    fn render(&mut self) {
        if self.hidden {
            return;
        }
        let view = self.view();
        let Some(output) = self.output.as_mut() else { return };
        let mut lost = false;
        for (index, scene) in self.scenes.iter_mut().enumerate() {
            let damage = scene.render(&view);
            if !damage.is_empty() && output.present(index, scene.canvas(), &damage).is_err() {
                lost = true;
                break;
            }
        }
        self.last_frame = Instant::now();
        if lost {
            self.lose_output();
        }
    }

    /// The display device disappeared (typically the firmware frame buffer
    /// being replaced by the real graphics driver): look for a new one.
    fn lose_output(&mut self) {
        if let Some(mut output) = self.output.take() {
            output.abandon();
        }
        self.scenes.clear();
        self.next_probe = Instant::now();
        self.lost_since.get_or_insert_with(Instant::now);
    }

    fn reprobe(&mut self) {
        if self.output.is_some() || Instant::now() < self.next_probe {
            return;
        }
        match (self.probe)() {
            Ok(output) => {
                self.output = Some(output);
                self.build_scenes();
                self.lost_since = None;
                if self.auto_hidden {
                    self.show();
                } else if self.hidden {
                    if let Some(output) = self.output.as_mut() {
                        output.hide();
                    }
                }
            }
            Err(_) => self.next_probe = Instant::now() + Duration::from_millis(250),
        }
    }

    fn prompting(&self) -> bool {
        !self.asks.is_empty()
    }

    fn timeout(&self) -> Duration {
        if self.output.is_none() || self.prompting() {
            return Duration::from_millis(250);
        }
        if self.hidden || !self.opts.animate {
            return Duration::from_millis(500);
        }
        FRAME.saturating_sub(self.last_frame.elapsed())
    }

    /// Run until asked to quit; then leave the screen as requested.
    pub fn run(mut self) {
        while self.quit.is_none() {
            self.turn();
        }
        let retain = self.quit.flatten();
        self.finish(retain);
    }

    fn turn(&mut self) {
        let timeout = self.timeout();
        let (accept, signal, keys, readable) = {
            let mut fds = Vec::with_capacity(3 + self.clients.len());
            fds.push(PollFd::new(&self.listener, PollFlags::IN));
            fds.push(PollFd::new(&self.signals, PollFlags::IN));
            let console_at = self.console.as_ref().map(|console| {
                fds.push(PollFd::new(console, PollFlags::IN));
                fds.len() - 1
            });
            let first_client = fds.len();
            for client in &self.clients {
                fds.push(PollFd::new(&client.stream, PollFlags::IN));
            }
            let spec = Timespec { tv_sec: timeout.as_secs() as i64, tv_nsec: timeout.subsec_nanos() as _ };
            let ready = poll(&mut fds, Some(&spec)).unwrap_or(0);
            let hit = |fd: &PollFd<'_>| !fd.revents().is_empty();
            let readable: Vec<u64> = if ready == 0 {
                Vec::new()
            } else {
                self.clients.iter().zip(&fds[first_client..]).filter(|(_, fd)| hit(fd)).map(|(c, _)| c.id).collect()
            };
            (
                ready > 0 && hit(&fds[0]),
                ready > 0 && hit(&fds[1]),
                ready > 0 && console_at.is_some_and(|i| hit(&fds[i])),
                readable,
            )
        };

        if signal && sys::read_signal(self.signals.as_fd()).is_some() {
            self.quit = Some(None);
            return;
        }
        if accept {
            self.accept();
        }
        if keys {
            self.read_keys();
        }
        for id in readable {
            self.read_client(id);
        }
        self.tick();
        self.render();
    }

    fn tick(&mut self) {
        let now = Instant::now();
        if self.message.as_ref().is_some_and(|m| now >= m.expires) {
            self.message = None;
        }
        if self.prompting() {
            self.caps_lock = self.console.as_ref().is_some_and(Console::caps_lock);
        }
        if let Some(output) = self.output.as_mut() {
            if !output.alive() {
                self.lose_output();
            }
        }
        self.reprobe();
        // No display for a while: do not leave a prompt where nobody can see
        // it. Fall back to the text console until a display returns.
        if self.output.is_none() && !self.hidden && self.lost_since.is_some_and(|t| t.elapsed() >= self.lost_grace) {
            self.hide();
            self.auto_hidden = true;
        }
    }

    fn accept(&mut self) {
        while let Ok((stream, _)) = self.listener.accept() {
            if !peer_allowed(&stream) || stream.set_nonblocking(true).is_err() {
                continue; // dropped: the peer sees the connection close
            }
            if self.clients.len() >= MAX_CLIENTS {
                // Make room by closing the oldest connection that is not
                // waiting for a passphrase, so idle or stuck clients cannot
                // lock everyone else out.
                let idle = self.clients.iter().find(|c| !self.asks.iter().any(|ask| ask.client == c.id)).map(|c| c.id);
                match idle {
                    Some(id) => self.drop_client(id),
                    None => continue,
                }
            }
            let id = self.next_client;
            self.next_client += 1;
            self.clients.push(Client { id, stream, reader: FrameReader::new() });
        }
    }

    fn read_client(&mut self, id: u64) {
        let Some(client) = self.clients.iter_mut().find(|c| c.id == id) else { return };
        let mut buf = [0u8; 4096];
        let mut requests = Vec::new();
        let mut closed = false;
        match client.stream.read(&mut buf) {
            Ok(0) => closed = true,
            Ok(n) => {
                client.reader.feed(&buf[..n]);
                loop {
                    match client.reader.next_frame() {
                        Ok(Some(payload)) => requests.push(Request::parse(&payload)),
                        Ok(None) => break,
                        Err(_) => {
                            closed = true;
                            break;
                        }
                    }
                }
            }
            Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted) => {}
            Err(_) => closed = true,
        }
        for request in requests {
            let response = match request {
                Ok(request) => self.handle(id, request),
                Err(e) => Some(Response::Error(e.to_string())),
            };
            if let Some(response) = response {
                if !self.reply(id, &response) {
                    closed = true;
                }
            }
        }
        if closed {
            self.drop_client(id);
        }
    }

    /// Send a response. False when the client is gone.
    fn reply(&mut self, id: u64, response: &Response) -> bool {
        let Some(client) = self.clients.iter_mut().find(|c| c.id == id) else { return false };
        let Ok(framed) = proto::frame(&response.encode()) else { return false };
        // A reply is a few bytes; a peer that does not take it within a
        // moment is not worth stalling the boot screen for.
        let sent = client.stream.set_nonblocking(false).is_ok()
            && client.stream.set_write_timeout(Some(Duration::from_millis(250))).is_ok()
            && client.stream.write_all(&framed).is_ok();
        let _ = client.stream.set_nonblocking(true);
        sent
    }

    fn drop_client(&mut self, id: u64) {
        self.clients.retain(|c| c.id != id);
        let was_front = self.asks.front().is_some_and(|ask| ask.client == id);
        self.asks.retain(|ask| ask.client != id);
        if was_front {
            self.passphrase.clear();
            self.begin_prompt();
        }
    }

    fn begin_prompt(&mut self) {
        self.passphrase.clear();
        if self.asks.is_empty() {
            return;
        }
        self.caps_lock = self.console.as_ref().is_some_and(Console::caps_lock);
        if self.hidden {
            self.begin_prompt_line();
        }
    }

    fn handle(&mut self, client: u64, request: Request) -> Option<Response> {
        match request {
            Request::Ping => Some(Response::Ok),
            Request::Ask { prompt } => {
                if self.console.is_none() {
                    return Some(Response::Error("no keyboard".into()));
                }
                if self.asks.len() >= MAX_ASKS {
                    return Some(Response::Error("too many prompts".into()));
                }
                self.asks.push_back(Ask { client, prompt });
                if self.asks.len() == 1 {
                    self.begin_prompt();
                }
                None // answered when Enter is pressed
            }
            Request::Message { text } => {
                if text.trim().is_empty() {
                    self.message = None;
                } else {
                    let tone = tone_of(&text);
                    let ttl = if tone == Tone::Error { ERROR_TTL } else { INFO_TTL };
                    if self.hidden {
                        let line = format!("\r\n{text}\r\n");
                        if let Some(console) = self.console.as_mut() {
                            console.print(&line);
                        }
                    }
                    self.message = Some(Message { text, tone, expires: Instant::now() + ttl });
                }
                Some(Response::Ok)
            }
            Request::Progress(percent) => {
                self.progress = Some(percent);
                Some(Response::Ok)
            }
            Request::Mode(mode) => {
                self.mode = mode;
                Some(Response::Ok)
            }
            Request::Hide => {
                self.hide();
                Some(Response::Ok)
            }
            Request::Show => {
                self.show();
                Some(Response::Ok)
            }
            Request::Quit { retain } => {
                self.quit = Some(retain);
                Some(Response::Ok)
            }
            Request::NewRoot { path } => {
                // Follow the system to its real root so device nodes stay
                // reachable by path after the initramfs is gone.
                let moved = rustix::process::chroot(&path).and_then(|()| rustix::process::chdir("/"));
                Some(match moved {
                    Ok(()) => Response::Ok,
                    Err(e) => Response::Error(format!("{path}: {e}")),
                })
            }
            Request::Snapshot { name } => {
                self.render();
                let saved = match (self.output.as_mut(), self.scenes.first()) {
                    (Some(output), Some(scene)) => output.snapshot(&name, scene.canvas()),
                    _ => Err(io::Error::new(io::ErrorKind::NotConnected, "no display")),
                };
                Some(match saved {
                    Ok(()) => Response::Ok,
                    Err(e) => Response::Error(e.to_string()),
                })
            }
        }
    }

    fn read_keys(&mut self) {
        let mut buf = [0u8; 256];
        let n = match self.console.as_mut().map(|c| c.read(&mut buf)) {
            Some(Ok(n)) => n,
            _ => 0,
        };
        let keys = self.decoder.decode(&buf[..n]);
        // The raw bytes may be passphrase characters.
        zeroize::Zeroize::zeroize(&mut buf);
        for key in keys {
            match key {
                Key::Escape => {
                    if self.hidden {
                        self.show();
                    } else {
                        self.hide();
                    }
                }
                Key::Enter if self.prompting() => self.finish_prompt(),
                key if self.prompting() => {
                    self.passphrase.apply(key);
                }
                _ => {} // nothing is asking: typed characters are dropped
            }
        }
    }

    fn finish_prompt(&mut self) {
        let Some(ask) = self.asks.pop_front() else { return };
        let response = Response::Password(self.passphrase.take());
        if !self.reply(ask.client, &response) {
            self.drop_client(ask.client);
        }
        drop(response);
        // The answer to the last attempt no longer applies.
        if self.message.as_ref().is_some_and(|m| m.tone == Tone::Error) {
            self.message = None;
        }
        self.begin_prompt();
    }

    fn hide(&mut self) {
        if self.hidden {
            return;
        }
        self.hidden = true;
        self.auto_hidden = false;
        if let Some(output) = self.output.as_mut() {
            output.hide();
        }
        if let Some(console) = self.console.as_mut() {
            console.set_graphics(false);
        }
        if self.prompting() {
            self.begin_prompt_line();
        }
    }

    /// While the boot screen is hidden, the prompt is a line on the console.
    fn begin_prompt_line(&mut self) {
        if let (Some(ask), Some(console)) = (self.asks.front(), self.console.as_mut()) {
            console.print(&format!("\r\n{} (typing is not shown): ", ask.prompt));
        }
    }

    fn show(&mut self) {
        if !self.hidden {
            return;
        }
        self.hidden = false;
        self.auto_hidden = false;
        if let Some(console) = self.console.as_mut() {
            console.set_graphics(true);
        }
        let shown = self.output.as_mut().map(|output| output.show());
        if matches!(shown, Some(Err(_))) {
            self.lose_output();
        }
        self.scenes.iter_mut().for_each(Scene::invalidate);
    }

    /// Stop serving and leave the screen: restored to the text console, or
    /// (with `retain`) showing the last frame until the next program takes
    /// the display, with the text console as the fallback when none does.
    fn finish(mut self, retain: Option<u16>) {
        // Let the client that asked us to quit read its answer, then go.
        self.clients.clear();
        self.asks.clear();
        self.passphrase.clear();
        let _ = std::fs::remove_file(&self.opts.socket);
        let _ = std::fs::remove_file(pid_path(&self.opts.socket));

        let retained = match (retain, self.output.as_mut()) {
            (Some(_), Some(output)) if !self.hidden => output.retain(),
            _ => false,
        };
        if !retained {
            return; // dropping the output and the console restores both
        }
        if let Some(console) = self.console.as_mut() {
            console.restore_termios();
        }
        let deadline = Instant::now() + Duration::from_secs(retain.unwrap_or(0) as u64);
        while Instant::now() < deadline {
            if sys::read_signal(self.signals.as_fd()).is_some() {
                break;
            }
            let taken = self.output.as_mut().is_none_or(|output| output.taken_over());
            if taken {
                // Another program drives the display now. Leave its picture
                // alone, and the console too if it is the one in use.
                if let Some(mut output) = self.output.take() {
                    output.abandon();
                }
                if let Some(console) = self.console.as_mut() {
                    if console.is_foreground() {
                        console.disown_graphics();
                    }
                }
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        // Nobody took the display: bring the text console back.
        if let Some(output) = self.output.as_mut() {
            let _ = output.show();
            output.hide();
        }
    }
}

impl Drop for Daemon {
    fn drop(&mut self) {
        self.passphrase.clear();
        // Restore the display before the console goes back to text mode.
        self.output = None;
    }
}

/// Run the worker stage: the boot screen itself. `ready` is called once the
/// first frame is on screen and the socket accepts clients.
pub fn run_worker(opts: Options, ready: impl FnOnce()) -> Result<(), String> {
    let daemon = Daemon::start(opts)?;
    ready();
    daemon.run();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn default_options() {
        let opts = parse_args(&[]).unwrap();
        assert_eq!(opts, Options::default());
        assert_eq!(opts.socket, PathBuf::from("/run/boswas-bootsplash/socket"));
        assert_eq!(pid_path(&opts.socket), PathBuf::from("/run/boswas-bootsplash/pid"));
        assert!(opts.sandbox && opts.animate && !opts.force);
    }

    #[test]
    fn option_forms() {
        let opts = parse_args(&args(&[
            "--mode=shutdown",
            "--static",
            "--socket",
            "/tmp/s",
            "--tty=/dev/pts/3",
            "--wait",
            "0",
        ]))
        .unwrap();
        assert_eq!(opts.mode, Mode::Shutdown);
        assert!(!opts.animate);
        assert_eq!(opts.socket, PathBuf::from("/tmp/s"));
        assert_eq!(opts.tty, Some(PathBuf::from("/dev/pts/3")));
        assert_eq!(opts.wait_ms, 0);
        let opts =
            parse_args(&args(&["--headless", "1920x1080", "--out=/tmp/o", "--foreground", "--no-sandbox"])).unwrap();
        assert_eq!(opts.headless, Some((1920, 1080, PathBuf::from("/tmp/o"))));
        assert_eq!(opts.stage, Stage::Worker);
        assert!(!opts.sandbox);
        assert_eq!(parse_args(&args(&["--supervise"])).unwrap().stage, Stage::Supervisor);
        assert_eq!(parse_args(&args(&["--worker"])).unwrap().stage, Stage::Worker);
        // plymouthd habits are tolerated.
        assert!(parse_args(&args(&["--mode=boot", "--attach-to-session", "--pid-file=/run/x"])).is_ok());
    }

    #[test]
    fn bad_options() {
        for bad in [
            &["--mode=updates"][..],
            &["--mode"],
            &["--headless=1920x1080"],
            &["--out=/tmp/o"],
            &["--headless=big", "--out=/tmp/o"],
            &["--socket=relative"],
            &["--wait=soon"],
            &["--static=yes"],
            &["--frobnicate"],
        ] {
            assert!(parse_args(&args(bad)).is_err(), "{bad:?}");
        }
    }

    use std::sync::{Arc, Mutex};

    /// What a mock display was asked to do, shared with the test.
    #[derive(Default)]
    struct Shared {
        log: Vec<&'static str>,
        fail_present: bool,
        gone: bool,
        taken_over: bool,
        display_available: bool,
        presents: usize,
    }

    struct Mock(Arc<Mutex<Shared>>);

    impl Output for Mock {
        fn name(&self) -> &'static str {
            "mock"
        }
        fn sizes(&self) -> Vec<(u32, u32)> {
            vec![(640, 480)]
        }
        fn present(
            &mut self,
            _index: usize,
            _canvas: &crate::gfx::Canvas,
            _damage: &[crate::gfx::Rect],
        ) -> io::Result<()> {
            let mut shared = self.0.lock().unwrap();
            if shared.fail_present {
                return Err(io::Error::from_raw_os_error(libc::ENODEV));
            }
            shared.presents += 1;
            Ok(())
        }
        fn alive(&mut self) -> bool {
            !self.0.lock().unwrap().gone
        }
        fn hide(&mut self) {
            self.0.lock().unwrap().log.push("hide");
        }
        fn show(&mut self) -> io::Result<()> {
            self.0.lock().unwrap().log.push("show");
            Ok(())
        }
        fn retain(&mut self) -> bool {
            self.0.lock().unwrap().log.push("retain");
            true
        }
        fn taken_over(&mut self) -> bool {
            self.0.lock().unwrap().taken_over
        }
        fn abandon(&mut self) {
            self.0.lock().unwrap().log.push("abandon");
        }
    }

    struct Rig {
        daemon: Daemon,
        shared: Arc<Mutex<Shared>>,
        dir: PathBuf,
        master: std::fs::File,
    }

    /// A daemon on a mock display, with a pseudo-terminal as its console.
    fn rig(name: &str) -> Rig {
        use rustix::pty::{grantpt, openpt, ptsname, unlockpt, OpenptFlags};
        let dir = crate::socket_test_dir().join(format!("bbs-unit-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let opts = Options { socket: dir.join("socket"), animate: false, ..Options::default() };
        let listener = bind(&opts.socket).unwrap();
        std::fs::write(pid_path(&opts.socket), "1\n").unwrap();
        let master = openpt(OpenptFlags::RDWR | OpenptFlags::NOCTTY).unwrap();
        grantpt(&master).unwrap();
        unlockpt(&master).unwrap();
        let slave = ptsname(&master, Vec::new()).unwrap();
        let mut console = Console::open(Path::new(slave.to_str().unwrap())).unwrap();
        console.set_raw().unwrap();
        let shared = Arc::new(Mutex::new(Shared::default()));
        let for_probe = shared.clone();
        let probe: Probe = Box::new(move || {
            if for_probe.lock().unwrap().display_available {
                Ok(Box::new(Mock(for_probe.clone())) as Box<dyn Output>)
            } else {
                Err(io::Error::new(io::ErrorKind::NotFound, "no display"))
            }
        });
        let signals = sys::signal_fd(&[libc::SIGUSR2]).unwrap();
        let mut daemon =
            Daemon::assemble(opts, Box::new(Mock(shared.clone())), Some(console), listener, signals, probe);
        daemon.lost_grace = Duration::ZERO;
        Rig { daemon, shared, dir, master: std::fs::File::from(master) }
    }

    fn console_text(master: &mut std::fs::File) -> String {
        let spec = Timespec { tv_sec: 0, tv_nsec: 200_000_000 };
        let ready = {
            let mut fds = [PollFd::new(&*master, PollFlags::IN)];
            poll(&mut fds, Some(&spec)).unwrap_or(0)
        };
        let mut buf = [0u8; 1024];
        let n = if ready > 0 { master.read(&mut buf).unwrap_or(0) } else { 0 };
        String::from_utf8_lossy(&buf[..n]).into_owned()
    }

    #[test]
    fn a_lost_display_falls_back_to_the_console_and_comes_back() {
        let Rig { mut daemon, shared, dir, mut master } = rig("lost");
        daemon.render();
        assert_eq!(shared.lock().unwrap().presents, 1, "the first frame is presented");
        daemon.asks.push_back(Ask { client: 99, prompt: "Please unlock disk sda3_crypt".into() });

        // The device disappears (firmware frame buffer replaced by the real driver).
        shared.lock().unwrap().fail_present = true;
        daemon.render();
        assert!(daemon.output.is_none() && daemon.lost_since.is_some());
        assert!(shared.lock().unwrap().log.contains(&"abandon"), "a dead device is not restored");
        // No new display in time: the prompt moves to the text console.
        daemon.tick();
        assert!(daemon.hidden && daemon.auto_hidden);
        assert!(console_text(&mut master).contains("Please unlock disk sda3_crypt"));

        // A display appears: the boot screen returns by itself.
        {
            let mut shared = shared.lock().unwrap();
            shared.fail_present = false;
            shared.display_available = true;
            shared.log.clear();
        }
        daemon.next_probe = Instant::now();
        daemon.tick();
        assert!(daemon.output.is_some() && !daemon.hidden && !daemon.auto_hidden && daemon.lost_since.is_none());
        assert_eq!(shared.lock().unwrap().log, vec!["show"]);
        daemon.render();
        assert_eq!(shared.lock().unwrap().presents, 2, "a full frame goes to the new display");
        drop(daemon);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_display_that_returns_stays_hidden_when_the_user_hid_the_screen() {
        let Rig { mut daemon, shared, dir, .. } = rig("userhid");
        daemon.render();
        daemon.hide(); // Escape, or `hide`
        assert_eq!(shared.lock().unwrap().log, vec!["hide"]);
        shared.lock().unwrap().gone = true;
        daemon.tick();
        assert!(daemon.output.is_none() && daemon.hidden && !daemon.auto_hidden);
        {
            let mut shared = shared.lock().unwrap();
            shared.gone = false;
            shared.display_available = true;
            shared.log.clear();
        }
        daemon.next_probe = Instant::now();
        daemon.tick();
        assert!(daemon.output.is_some() && daemon.hidden, "the user's choice is kept");
        assert_eq!(shared.lock().unwrap().log, vec!["hide"], "the new display is left to the console");
        daemon.show();
        assert!(!daemon.hidden);
        drop(daemon);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn quit_restores_the_console_and_removes_the_socket() {
        let Rig { daemon, shared, dir, .. } = rig("quit");
        let socket = daemon.opts.socket.clone();
        assert!(socket.exists());
        daemon.finish(None);
        assert!(!socket.exists() && !pid_path(&socket).exists());
        assert!(!shared.lock().unwrap().log.contains(&"retain"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn retain_leaves_the_picture_to_the_program_that_takes_the_display() {
        let Rig { mut daemon, shared, dir, .. } = rig("handover");
        daemon.render();
        let remote = shared.clone();
        let greeter = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(250));
            remote.lock().unwrap().taken_over = true;
        });
        let begun = Instant::now();
        daemon.finish(Some(10));
        greeter.join().unwrap();
        assert!(begun.elapsed() < Duration::from_secs(3), "the daemon leaves as soon as the display is taken");
        // It gave up the display without restoring the text console over the newcomer.
        assert_eq!(shared.lock().unwrap().log, vec!["retain", "abandon"]);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn retain_brings_the_console_back_when_nobody_takes_the_display() {
        let Rig { mut daemon, shared, dir, .. } = rig("nobody");
        daemon.render();
        let begun = Instant::now();
        daemon.finish(Some(1));
        assert!(begun.elapsed() >= Duration::from_secs(1));
        assert_eq!(shared.lock().unwrap().log, vec!["retain", "show", "hide"]);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_hidden_screen_is_not_retained() {
        let Rig { mut daemon, shared, dir, .. } = rig("hiddenquit");
        daemon.render();
        daemon.hide();
        let begun = Instant::now();
        daemon.finish(Some(5));
        assert!(begun.elapsed() < Duration::from_secs(1));
        assert_eq!(shared.lock().unwrap().log, vec!["hide"]);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn message_tones() {
        assert_eq!(tone_of("cryptsetup: ERROR: sda3_crypt: cryptsetup failed, bad password or options?"), Tone::Error);
        assert_eq!(tone_of("cryptsetup: ERROR: sda3_crypt: maximum number of tries exceeded"), Tone::Error);
        assert_eq!(tone_of("Incorrect passphrase"), Tone::Error);
        assert_eq!(tone_of("cryptsetup: sda3_crypt: set up successfully"), Tone::Info);
        assert_eq!(tone_of("Resuming from /dev/dm-1"), Tone::Info);
    }
}
