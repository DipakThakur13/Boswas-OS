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

use crate::broker;
use crate::client::DEFAULT_SOCKET;
use crate::editor::{Key, KeyDecoder, Passphrase};
use crate::output::headless::HeadlessOutput;
use crate::output::{open_display, reopen_display, Output};
use crate::proto::{self, FrameReader, Request, Response};
use crate::runtime::{Lock, RunDir};
use crate::sandbox;
use crate::scene::{self, Mode, PromptView, Scene, Tone, View};
use crate::sys;
use crate::tty::{Console, DEFAULT_TTY};

/// Animation frames a second at most, however fast the display refreshes:
/// a frame costs microseconds, but the machine is busy starting up.
pub const MAX_FPS: u32 = 240;
/// Animation frames a second where the display does not tell its refresh
/// rate (the legacy frame buffer).
pub const FALLBACK_FPS: u32 = 60;
/// How often the loop looks at slow things while nothing moves: the Caps
/// Lock state during a prompt, a display that went away.
const POLL: Duration = Duration::from_millis(250);
/// How long an informational message stays on screen.
const INFO_TTL: Duration = Duration::from_secs(6);
/// How long an error message stays on screen.
const ERROR_TTL: Duration = Duration::from_secs(30);
/// How long the display may be missing before the text console takes over.
const LOST_GRACE: Duration = Duration::from_secs(2);
/// How long the worker waits for the supervisor to hand over the graphics
/// devices when it cannot open them itself.
const HELPER_WAIT: Duration = Duration::from_millis(500);
/// Most clients served at once, and most prompts waiting.
const MAX_CLIENTS: usize = 16;
const MAX_ASKS: usize = 8;

/// How the daemon was asked to run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Options {
    pub mode: Mode,
    /// Animate: the ring turns, the caret blinks, a rejected passphrase
    /// shakes the text box.
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
    /// Internal: standard input is the channel to the supervisor, which
    /// opens graphics devices for the sandboxed worker (see `broker`).
    pub helper: bool,
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
            helper: false,
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
            "--devices-on-stdin" if inline.is_none() => opts.helper = true,
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
    socket.with_file_name(crate::runtime::PID_FILE)
}

/// Why the boot screen did not start.
#[derive(Debug, PartialEq, Eq)]
pub enum StartError {
    /// Another boot screen is running. Nothing of it was touched.
    Running,
    Failed(String),
}

impl std::fmt::Display for StartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StartError::Running => f.write_str("a boot screen is already running"),
            StartError::Failed(reason) => f.write_str(reason),
        }
    }
}

impl From<String> for StartError {
    fn from(reason: String) -> Self {
        StartError::Failed(reason)
    }
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
    /// When the next animation frame is due.
    next_frame: Instant,
    /// When the console's terminal settings are looked at next.
    next_console_check: Instant,
    /// When the caret last started a blink: the prompt appeared or a key
    /// was pressed.
    caret_from: Instant,
    /// When the text box started shaking, while it does.
    shake_from: Option<Instant>,
    next_probe: Instant,
    quit: Option<Option<u16>>,
    /// Held while this process is the boot screen and no supervisor holds
    /// it (a worker in the foreground).
    lock: Option<Lock>,
}

/// The time between animation frames on a display that refreshes
/// `millihertz` times a thousand seconds: one frame per refresh, at most
/// [`MAX_FPS`]; [`FALLBACK_FPS`] where the display does not say (`None`) or
/// reports a rate no display has.
pub fn frame_interval(millihertz: Option<u32>) -> Duration {
    let millihertz = millihertz
        .filter(|rate| (10_000..=1_000_000).contains(rate))
        .unwrap_or(FALLBACK_FPS * 1000)
        .min(MAX_FPS * 1000);
    Duration::from_nanos(1_000_000_000_000 / u64::from(millihertz))
}

/// When the animation frame after one drawn at `now` is due, `due` being
/// when that one was due. Frames keep to a fixed pace, so the time spent
/// drawing and waking up does not slow the animation down, and frames
/// missed while the machine was busy are skipped rather than caught up
/// with. A frame drawn early (a key, a message) leaves the pace alone.
fn next_frame(due: Instant, now: Instant, frame: Duration) -> Instant {
    if due > now {
        return due;
    }
    let missed = now.duration_since(due).as_nanos() / frame.as_nanos().max(1) + 1;
    due + frame * u32::try_from(missed).unwrap_or(u32::MAX)
}

/// Whether a boot screen answers connections on this socket.
fn served(socket: &Path) -> bool {
    socket.exists() && UnixStream::connect(socket).is_ok()
}

fn bind(socket: &Path) -> io::Result<UnixListener> {
    if let Some(dir) = socket.parent() {
        if !dir.exists() {
            std::fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
        }
    }
    // A socket file left by a previous instance that no longer answers.
    if socket.exists() {
        if served(socket) {
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
    /// nothing is left behind (console and display are restored on drop),
    /// and nothing of a boot screen that is already running is touched.
    /// `devices` is the channel to the supervisor, when there is one.
    pub fn start(opts: Options, devices: Option<broker::Client>) -> Result<Daemon, StartError> {
        let signals =
            sys::signal_fd(&[libc::SIGTERM, libc::SIGINT, libc::SIGHUP]).map_err(|e| format!("signals: {e}"))?;

        // One boot screen at a time, decided before the console or the
        // display is touched. Under a supervisor, the supervisor holds the
        // lock; in the foreground this process does. A socket that still
        // answers although the lock was free belongs to a worker whose
        // supervisor is gone: it is left alone as well.
        let lock = if opts.foreground {
            let taken = RunDir::open(&opts.socket).and_then(|run| run.lock());
            match taken.map_err(|e| format!("lock next to {}: {e}", opts.socket.display()))? {
                Some(lock) => Some(lock),
                None => return Err(StartError::Running),
            }
        } else {
            None
        };
        if served(&opts.socket) {
            return Err(StartError::Running);
        }

        // The console comes first: without a keyboard there can be no
        // passphrase prompt, and then the text console must do the asking.
        let tty_path = opts.tty.clone().unwrap_or_else(|| PathBuf::from(DEFAULT_TTY));
        let console = match Console::open(&tty_path) {
            Ok(console) => Some(console),
            Err(e) if opts.headless.is_some() && opts.tty.is_none() => {
                let _ = e;
                None
            }
            Err(e) => return Err(format!("console {}: {e}", tty_path.display()).into()),
        };

        let output: Box<dyn Output> = match &opts.headless {
            Some((w, h, dir)) => {
                Box::new(HeadlessOutput::new(*w, *h, dir.clone()).map_err(|e| format!("headless output: {e}"))?)
            }
            None => open_display(Duration::from_millis(opts.wait_ms)).map_err(|e| e.to_string())?,
        };

        let listener = bind(&opts.socket).map_err(|e| format!("socket {}: {e}", opts.socket.display()))?;
        let (socket, pid_file) = (opts.socket.clone(), pid_path(&opts.socket));
        // From here on a failure must take the socket and the pid file away again.
        let started = (|| {
            std::fs::write(&pid_file, format!("{}\n", std::process::id()))
                .map_err(|e| format!("{}: {e}", pid_file.display()))?;

            let probe: Probe = if opts.headless.is_some() {
                Box::new(|| Err(io::Error::new(io::ErrorKind::NotFound, "headless")))
            } else {
                let mut devices = devices;
                Box::new(move || reopen_display(devices.as_mut(), HELPER_WAIT))
            };
            let mut daemon = Daemon::assemble(opts, output, console, listener, signals, probe);
            daemon.lock = lock;
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
                return Err("the display could not be driven".to_string());
            }
            Ok(daemon)
        })();
        let daemon = match started {
            Ok(daemon) => daemon,
            Err(reason) => {
                let _ = std::fs::remove_file(&socket);
                let _ = std::fs::remove_file(&pid_file);
                return Err(reason.into());
            }
        };

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
            // The console it already has open, to open it again after a
            // hang-up (see `Console::revive`).
            if let Some(console) = &daemon.console {
                allowed.devices.push(console.path().to_path_buf());
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
            next_frame: now,
            next_console_check: now,
            caret_from: now,
            shake_from: None,
            next_probe: now,
            quit: None,
            lock: None,
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

    /// Draw what changed and send it to the display.
    fn render(&mut self) {
        if self.hidden {
            return;
        }
        let now = Instant::now();
        // Without animation the ring rests, the caret stays and nothing shakes.
        let animate = self.opts.animate;
        let phase = if animate {
            (now.duration_since(self.started).as_secs_f32() / scene::SPINNER_SECS).fract()
        } else {
            scene::REST_PHASE
        };
        let caret = !animate || scene::caret_visible(now.duration_since(self.caret_from));
        let shake = match self.shake_from {
            Some(since) if animate => scene::shake_offset(now.duration_since(since)),
            _ => 0.0,
        };
        // The view borrows the prompt and the message; nothing is copied for a frame.
        let (passphrase, caps_lock) = (&self.passphrase, self.caps_lock);
        let view = View {
            mode: self.mode,
            prompt: self.asks.front().map(|ask| PromptView {
                text: &ask.prompt,
                dots: passphrase.chars(),
                caps_lock,
                caret,
                shake,
            }),
            message: self.message.as_ref().map(|m| (m.text.as_str(), m.tone)),
            progress: self.progress,
            phase,
        };
        let Some(output) = self.output.as_mut() else { return };
        let mut lost = false;
        for (index, scene) in self.scenes.iter_mut().enumerate() {
            if scene.render(&view).is_empty() {
                continue;
            }
            if output.present(index, scene.canvas(), scene.damage()).is_err() {
                lost = true;
                break;
            }
        }
        self.next_frame = next_frame(self.next_frame, Instant::now(), self.frame_interval());
        if lost {
            self.lose_output();
        }
    }

    /// The display device disappeared (typically the firmware frame buffer
    /// being replaced by the real graphics driver): look for a new one. Its
    /// scenes are new too, so the first frame on it is a complete one.
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

    /// How long the loop may sleep: until the next frame of whatever moves.
    fn timeout(&self) -> Duration {
        if self.output.is_none() {
            return POLL;
        }
        let moving = self.opts.animate && !self.hidden;
        if self.prompting() {
            if !moving {
                return POLL;
            }
            if self.shake_from.is_some() {
                return self.next_frame.saturating_duration_since(Instant::now());
            }
            // The caret changes every CARET_BLINK; nothing else moves.
            let blink = scene::CARET_BLINK.as_nanos();
            let into = self.caret_from.elapsed().as_nanos() % blink;
            return Duration::from_nanos((blink - into) as u64).min(POLL);
        }
        if !moving {
            return Duration::from_millis(500);
        }
        self.next_frame.saturating_duration_since(Instant::now())
    }

    /// The time between animation frames on the display in use.
    fn frame_interval(&self) -> Duration {
        frame_interval(self.output.as_ref().and_then(|output| output.refresh_millihertz()))
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
            // A console that was hung up and could not be opened again is
            // not waited for: it would report "ready" for ever.
            let console_at = self.console.as_ref().filter(|console| !console.lost()).map(|console| {
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
        // Other programs reset the console's terminal settings while the
        // system starts (systemd does, a moment after it took over from the
        // initramfs): echo and line buffering come back. Put raw mode back,
        // every turn while a passphrase may be typed and a few times a
        // second while the screen is shown. A screen hidden without a
        // prompt leaves the console to whoever uses it. A console that was
        // hung up in the meantime is opened again on the way.
        if self.prompting() || now >= self.next_console_check {
            self.next_console_check = now + POLL;
            if let Some(console) = self.console.as_mut() {
                if self.asks.is_empty() && self.hidden {
                    console.revive();
                } else {
                    console.ensure_raw();
                }
            }
        }
        if self.shake_from.is_some_and(|since| now.duration_since(since) >= scene::SHAKE) {
            self.shake_from = None;
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
        self.shake_from = None;
        if self.asks.is_empty() {
            return;
        }
        // Never read a passphrase from a console that echoes.
        if let Some(console) = self.console.as_mut() {
            console.ensure_raw();
        }
        self.caps_lock = self.console.as_ref().is_some_and(Console::caps_lock);
        let now = Instant::now();
        self.caret_from = now;
        // The disk-unlock scripts report a rejected passphrase with a
        // message and then ask again: the new prompt opens with a shake.
        if self.message.as_ref().is_some_and(|m| m.tone == Tone::Error && now < m.expires) {
            self.shake_from = Some(now);
        }
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
                    let now = Instant::now();
                    if tone == Tone::Error && self.prompting() {
                        self.shake_from = Some(now);
                    }
                    self.message = Some(Message { text, tone, expires: now + ttl });
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
                    // The caret is solid while typing, as in any text box.
                    self.caret_from = Instant::now();
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
    /// Whatever happens, a display or a console that another program has
    /// taken in the meantime is left exactly as that program has it.
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
                // Another program drives the display now.
                self.leave_to_the_newcomer();
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        // Nobody showed a picture in time. That does not mean nobody is
        // there: a compositor may hold the display (then the display cannot
        // be taken back) or run its session on the console without having
        // drawn yet. Both are then its to use, and putting the text console
        // back would take them away from it.
        let console_taken = self.console.as_mut().is_some_and(Console::taken_by_another);
        let display_free = self.output.as_mut().is_some_and(|output| output.show().is_ok());
        if display_free && !console_taken {
            // Really nobody: bring the text console back.
            if let Some(output) = self.output.as_mut() {
                output.hide();
            }
            return;
        }
        if display_free {
            // Taken back a moment ago, only to find out: give it up again.
            if let Some(output) = self.output.as_mut() {
                output.retain();
            }
        }
        self.leave_to_the_newcomer();
    }

    /// Give up the display without putting the previous picture back, and
    /// the console, if it is the one in use, without putting it back into
    /// text mode: both belong to the program that took them.
    fn leave_to_the_newcomer(&mut self) {
        if let Some(mut output) = self.output.take() {
            output.abandon();
        }
        if let Some(console) = self.console.as_mut() {
            if console.is_foreground() {
                console.disown_graphics();
            }
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
pub fn run_worker(opts: Options, devices: Option<broker::Client>, ready: impl FnOnce()) -> Result<(), StartError> {
    let daemon = Daemon::start(opts, devices)?;
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
        /// Another program holds the display: it cannot be taken back.
        show_refused: bool,
        display_available: bool,
        presents: usize,
        /// The rectangles of the last frame presented.
        damage: Vec<crate::gfx::Rect>,
        /// The display's refresh rate (millihertz), if it tells.
        refresh: Option<u32>,
    }

    struct Mock(Arc<Mutex<Shared>>);

    impl Output for Mock {
        fn name(&self) -> &'static str {
            "mock"
        }
        fn sizes(&self) -> Vec<(u32, u32)> {
            vec![(640, 480)]
        }
        fn refresh_millihertz(&self) -> Option<u32> {
            self.0.lock().unwrap().refresh
        }
        fn present(
            &mut self,
            _index: usize,
            _canvas: &crate::gfx::Canvas,
            damage: &[crate::gfx::Rect],
        ) -> io::Result<()> {
            let mut shared = self.0.lock().unwrap();
            if shared.fail_present {
                return Err(io::Error::from_raw_os_error(libc::ENODEV));
            }
            shared.presents += 1;
            shared.damage = damage.to_vec();
            Ok(())
        }
        fn alive(&mut self) -> bool {
            !self.0.lock().unwrap().gone
        }
        fn hide(&mut self) {
            self.0.lock().unwrap().log.push("hide");
        }
        fn show(&mut self) -> io::Result<()> {
            let mut shared = self.0.lock().unwrap();
            shared.log.push("show");
            if shared.show_refused {
                return Err(io::Error::from_raw_os_error(libc::EBUSY));
            }
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
        rig_with(name, false)
    }

    fn rig_with(name: &str, animate: bool) -> Rig {
        use rustix::pty::{grantpt, openpt, ptsname, unlockpt, OpenptFlags};
        let dir = crate::socket_test_dir().join(format!("bbs-unit-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let opts = Options { socket: dir.join("socket"), animate, ..Options::default() };
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
    fn retain_leaves_a_display_that_another_program_holds_alone() {
        // The compositor holds the display but has not drawn its first
        // frame when the time is up.
        let Rig { mut daemon, shared, dir, .. } = rig("held");
        daemon.render();
        shared.lock().unwrap().show_refused = true;
        daemon.finish(Some(1));
        // The display could not be taken back, so nothing is put back on it:
        // no "hide" (which restores the text console's picture).
        assert_eq!(shared.lock().unwrap().log, vec!["retain", "show", "abandon"]);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn the_console_settings_are_put_back_when_another_program_resets_them() {
        use rustix::termios::{tcgetattr, tcsetattr, LocalModes, OptionalActions};
        let Rig { mut daemon, dir, mut master, .. } = rig("reset");
        let cooked = |master: &std::fs::File| {
            tcgetattr(master).unwrap().local_modes.contains(LocalModes::ECHO | LocalModes::ICANON)
        };
        let reset = |master: &std::fs::File| {
            let mut sane = tcgetattr(master).unwrap();
            sane.local_modes.insert(LocalModes::ECHO | LocalModes::ICANON | LocalModes::ISIG);
            tcsetattr(master, OptionalActions::Now, &sane).unwrap();
        };
        daemon.render();
        assert!(!cooked(&master));
        // systemd resets the console a moment after the switch to the real
        // root. The next look at the console puts raw mode back...
        reset(&master);
        daemon.next_console_check = Instant::now();
        daemon.tick();
        assert!(!cooked(&master), "raw mode is back while the screen is shown");
        // ...but not more often than a few times a second while nothing is asked.
        reset(&master);
        daemon.tick();
        assert!(cooked(&master));
        // A prompt opens: never with echo on.
        ask(&mut daemon, 7);
        assert!(!cooked(&master), "raw mode is put back before a passphrase is read");
        // While the prompt is open every turn of the loop checks.
        reset(&master);
        daemon.tick();
        assert!(!cooked(&master));
        type_keys(&mut daemon, &mut master, b"secret");
        assert_eq!(daemon.passphrase.chars(), 6);
        assert!(!console_text(&mut master).contains("secret"), "nothing typed is echoed");
        type_keys(&mut daemon, &mut master, b"\r");
        assert!(!daemon.prompting());

        // Hidden with Escape and nothing asked: the console is left to its user.
        daemon.hide();
        reset(&master);
        daemon.next_console_check = Instant::now();
        daemon.tick();
        assert!(cooked(&master));
        // A prompt on the hidden screen is a line on the console, still without echo.
        ask(&mut daemon, 8);
        assert!(!cooked(&master));
        drop(daemon);
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

    const REJECTED: &str = "cryptsetup: ERROR: sda3_crypt: cryptsetup failed, bad password or options?";

    fn ask(daemon: &mut Daemon, client: u64) {
        assert!(daemon.handle(client, Request::Ask { prompt: "Please unlock disk sda3_crypt".into() }).is_none());
    }

    /// Type on the console and let the daemon read it. The terminal hands
    /// the bytes over a moment after they are written, so wait for them.
    fn type_keys(daemon: &mut Daemon, master: &mut std::fs::File, keys: &[u8]) {
        master.write_all(keys).unwrap();
        let spec = Timespec { tv_sec: 5, tv_nsec: 0 };
        let ready = {
            let mut fds = [PollFd::new(daemon.console.as_ref().unwrap(), PollFlags::IN)];
            poll(&mut fds, Some(&spec)).unwrap_or(0)
        };
        assert_eq!(ready, 1, "the keys reach the console");
        daemon.read_keys();
    }

    #[test]
    fn an_animation_frame_presents_only_the_ring() {
        let Rig { mut daemon, shared, dir, .. } = rig_with("ring", true);
        let layout = crate::scene::Layout::new(640, 480);
        daemon.render();
        assert_eq!(shared.lock().unwrap().damage, [crate::gfx::Rect::new(0, 0, 640, 480)]);
        assert!(daemon.timeout() <= frame_interval(None), "the next frame is due within a sixtieth of a second");
        std::thread::sleep(Duration::from_millis(40));
        daemon.render();
        assert_eq!(shared.lock().unwrap().damage, [layout.spinner]);
        assert_eq!(shared.lock().unwrap().presents, 2);
        drop(daemon);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn frames_are_paced_to_the_refresh_rate_of_the_display() {
        let hz = |interval: Duration| (1.0 / interval.as_secs_f64()).round() as u32;
        assert_eq!(hz(frame_interval(Some(144_000))), 144);
        assert_eq!(hz(frame_interval(Some(59_940))), 60);
        assert_eq!(hz(frame_interval(Some(165_000))), 165);
        assert_eq!(hz(frame_interval(Some(240_000))), 240);
        assert_eq!(hz(frame_interval(Some(24_000))), 24, "a slow display: one frame per refresh");
        // Faster displays: the ring is not drawn more than 240 times a second.
        assert_eq!(hz(frame_interval(Some(360_000))), MAX_FPS);
        // A display that does not say (the legacy frame buffer), or says nonsense: 60.
        for unknown in [None, Some(0), Some(500), Some(5_000_000)] {
            assert_eq!(hz(frame_interval(unknown)), FALLBACK_FPS, "{unknown:?}");
        }

        // Frames keep their pace: the next one is a frame after the one that
        // was due, not after the moment the last one was drawn.
        let frame = frame_interval(Some(144_000));
        let start = Instant::now();
        assert_eq!(next_frame(start, start, frame), start + frame);
        assert_eq!(next_frame(start, start + Duration::from_millis(2), frame), start + frame);
        // Drawn early (a key was pressed): the pace stays.
        assert_eq!(next_frame(start + frame, start, frame), start + frame);
        // Late by more than a frame: the missed ones are skipped.
        assert_eq!(next_frame(start, start + frame * 3 + Duration::from_micros(10), frame), start + frame * 4);
    }

    #[test]
    fn the_ring_turns_once_per_refresh() {
        // Turns of the loop as the daemon runs them, for a third of a second
        // each. (The console stays open: a hung-up one would wake the loop.)
        for (refresh, fps) in [(None, 60.0), (Some(144_000), 144.0), (Some(360_000), 240.0)] {
            let Rig { mut daemon, shared, dir, master: _console } = rig_with("pace", true);
            shared.lock().unwrap().refresh = refresh;
            daemon.render();
            let first = shared.lock().unwrap().presents;
            let begun = Instant::now();
            while begun.elapsed() < Duration::from_millis(330) {
                daemon.turn();
            }
            let rate = (shared.lock().unwrap().presents - first) as f64 / begun.elapsed().as_secs_f64();
            assert!(rate > fps * 0.8 && rate < fps * 1.05, "{rate:.0} frames a second on a display of {refresh:?} mHz");
            drop(daemon);
            let _ = std::fs::remove_dir_all(dir);
        }
    }

    #[test]
    fn a_still_screen_presents_nothing_and_sleeps() {
        let Rig { mut daemon, shared, dir, .. } = rig("still");
        daemon.render();
        std::thread::sleep(Duration::from_millis(40));
        daemon.render();
        assert_eq!(shared.lock().unwrap().presents, 1, "without animation the ring rests");
        assert_eq!(daemon.timeout(), Duration::from_millis(500));
        // A prompt: the caret stays, a rejected passphrase does not shake the box.
        daemon.message =
            Some(Message { text: REJECTED.into(), tone: Tone::Error, expires: Instant::now() + ERROR_TTL });
        ask(&mut daemon, 7);
        daemon.render();
        daemon.caret_from -= scene::CARET_BLINK;
        std::thread::sleep(Duration::from_millis(40));
        daemon.render();
        assert_eq!(shared.lock().unwrap().presents, 2, "one frame for the prompt, none after it");
        assert_eq!(daemon.timeout(), POLL);
        drop(daemon);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn the_caret_blinks_and_typing_restarts_it() {
        let Rig { mut daemon, shared, dir, mut master } = rig_with("caret", true);
        let layout = crate::scene::Layout::new(640, 480);
        daemon.render();
        ask(&mut daemon, 7);
        assert!(daemon.shake_from.is_none(), "a first prompt opens at rest");
        daemon.render();
        let frames = shared.lock().unwrap().presents;
        // Nothing moves until the caret is due to change; the loop wakes for that.
        daemon.render();
        assert_eq!(shared.lock().unwrap().presents, frames);
        assert!(daemon.timeout() <= POLL.min(scene::CARET_BLINK));
        daemon.caret_from -= scene::CARET_BLINK;
        daemon.render();
        assert_eq!(shared.lock().unwrap().damage, [layout.caret_rect(0, 0)], "the caret goes out: only it is redrawn");
        daemon.caret_from = Instant::now() - (scene::CARET_BLINK - Duration::from_millis(100));
        let wake = daemon.timeout();
        assert!(wake > Duration::from_millis(20) && wake <= Duration::from_millis(100), "{wake:?}");
        // A key puts the caret back at once.
        type_keys(&mut daemon, &mut master, b"a");
        assert!(daemon.caret_from.elapsed() < Duration::from_millis(200));
        daemon.render();
        assert_eq!(shared.lock().unwrap().damage, [layout.field]);
        assert_eq!(daemon.passphrase.chars(), 1);
        drop(daemon);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_rejected_passphrase_shakes_the_next_prompt() {
        let Rig { mut daemon, shared, dir, mut master } = rig_with("shake", true);
        let layout = crate::scene::Layout::new(640, 480);
        daemon.render();
        ask(&mut daemon, 7);
        type_keys(&mut daemon, &mut master, b"wrong\r");
        assert!(!daemon.prompting(), "Enter answers the prompt");
        // What cryptsetup's initramfs script does next: a message, then a new prompt.
        assert_eq!(daemon.handle(8, Request::Message { text: REJECTED.into() }), Some(Response::Ok));
        assert!(daemon.shake_from.is_none(), "nothing shakes while no prompt is open");
        ask(&mut daemon, 9);
        assert!(daemon.shake_from.is_some(), "the prompt that follows a rejection opens with a shake");
        assert!(daemon.timeout() <= frame_interval(None));
        daemon.render();
        // Mid-swing the box is redrawn, and nothing but the box.
        daemon.shake_from = Some(Instant::now() - scene::SHAKE / 12);
        daemon.render();
        assert_eq!(shared.lock().unwrap().damage, [layout.field]);
        // It ends by itself, at rest.
        daemon.shake_from = Some(Instant::now() - scene::SHAKE);
        daemon.tick();
        assert!(daemon.shake_from.is_none());
        daemon.render();
        assert_eq!(shared.lock().unwrap().damage, [layout.field], "back at rest");
        // An error that arrives while the prompt is open shakes it too...
        assert_eq!(daemon.handle(8, Request::Message { text: REJECTED.into() }), Some(Response::Ok));
        assert!(daemon.shake_from.is_some());
        // ...an ordinary message does not, and Enter takes the error away.
        daemon.shake_from = None;
        assert_eq!(daemon.handle(8, Request::Message { text: "Second volume".into() }), Some(Response::Ok));
        assert!(daemon.shake_from.is_none());
        assert_eq!(daemon.handle(8, Request::Message { text: REJECTED.into() }), Some(Response::Ok));
        type_keys(&mut daemon, &mut master, b"\r");
        assert!(daemon.message.is_none() && daemon.shake_from.is_none());
        drop(daemon);
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
