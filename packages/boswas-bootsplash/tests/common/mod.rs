//! Shared test harness: a boot screen daemon in headless mode, with a
//! pseudo-terminal standing in for the console keyboard.
#![allow(dead_code)]

use std::io::{BufRead, BufReader, Read, Write};
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use boswas_bootsplash::assets;
use boswas_bootsplash::backdrop::Backdrop;
use boswas_bootsplash::client;
use boswas_bootsplash::gfx::{Rect, Rgb};
use boswas_bootsplash::proto::{self, Request, Response};
use boswas_bootsplash::scene::Layout;

pub const DAEMON: &str = env!("CARGO_BIN_EXE_boswas-bootsplashd");
pub const CLIENT: &str = env!("CARGO_BIN_EXE_boswas-bootsplash");

static COUNTER: AtomicU32 = AtomicU32::new(0);

/// A fresh private directory under the test binary's temporary directory.
pub fn temp_dir(name: &str) -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    // Unix socket paths are short (108 bytes): keep the directory name small.
    // The directory holds the daemon's Unix socket, and a socket path holds
    // about a hundred bytes: a long TMPDIR cannot be used.
    let base = std::env::temp_dir();
    let base = if base.as_os_str().len() <= 40 { base } else { std::path::PathBuf::from("/tmp") };
    let dir = base.join(format!("bbs-{}-{n}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// A pseudo-terminal: the daemon reads the slave, the test types on the master.
pub struct Pty {
    pub master: std::fs::File,
    pub path: PathBuf,
}

pub fn open_pty() -> Pty {
    use rustix::pty::{grantpt, openpt, ptsname, unlockpt, OpenptFlags};
    let master: OwnedFd = openpt(OpenptFlags::RDWR | OpenptFlags::NOCTTY).expect("open a pseudo-terminal");
    grantpt(&master).unwrap();
    unlockpt(&master).unwrap();
    let name = ptsname(&master, Vec::new()).unwrap();
    let path = PathBuf::from(name.to_str().unwrap());
    Pty { master: std::fs::File::from(master), path }
}

impl Pty {
    pub fn type_keys(&mut self, keys: &[u8]) {
        self.master.write_all(keys).unwrap();
        self.master.flush().unwrap();
    }

    /// Whether the terminal is in its normal line mode (echo and canonical input).
    pub fn cooked(&self) -> bool {
        let t = rustix::termios::tcgetattr(&self.master).unwrap();
        t.local_modes.contains(rustix::termios::LocalModes::ECHO | rustix::termios::LocalModes::ICANON)
    }

    /// Everything the daemon printed on the terminal within `wait`.
    pub fn read_output(&mut self, wait: Duration) -> String {
        use rustix::event::{poll, PollFd, PollFlags, Timespec};
        let deadline = Instant::now() + wait;
        let mut out = Vec::new();
        while Instant::now() < deadline {
            let spec = Timespec { tv_sec: 0, tv_nsec: 50_000_000 };
            let ready = {
                let mut fds = [PollFd::new(&self.master, PollFlags::IN)];
                poll(&mut fds, Some(&spec)).unwrap_or(0)
            };
            if ready > 0 {
                let mut buf = [0u8; 1024];
                match self.master.read(&mut buf) {
                    Ok(n) if n > 0 => out.extend_from_slice(&buf[..n]),
                    _ => break,
                }
            }
        }
        String::from_utf8_lossy(&out).into_owned()
    }
}

/// A decoded snapshot.
pub struct Frame {
    pub width: u32,
    pub height: u32,
    pub rgb: Vec<u8>,
    /// The background the boot screen paints at this size.
    pub backdrop: Backdrop,
}

impl Frame {
    pub fn parse_ppm(data: &[u8]) -> Frame {
        let mut fields = Vec::new();
        let mut pos = 0;
        while fields.len() < 4 {
            let start = pos;
            while !data[pos].is_ascii_whitespace() {
                pos += 1;
            }
            fields.push(std::str::from_utf8(&data[start..pos]).unwrap().to_owned());
            pos += 1;
        }
        assert_eq!(fields[0], "P6");
        let (width, height) = (fields[1].parse().unwrap(), fields[2].parse().unwrap());
        let rgb = data[pos..].to_vec();
        assert_eq!(rgb.len(), (width * height * 3) as usize);
        let backdrop = Layout::new(width, height).backdrop();
        Frame { width, height, rgb, backdrop }
    }

    pub fn pixel(&self, x: i32, y: i32) -> Rgb {
        let i = ((y as u32 * self.width + x as u32) * 3) as usize;
        Rgb::new(self.rgb[i], self.rgb[i + 1], self.rgb[i + 2])
    }

    /// Number of pixels of exactly `colour` inside `rect`.
    pub fn count(&self, rect: &Rect, colour: Rgb) -> usize {
        let mut n = 0;
        for y in rect.y..rect.bottom() {
            for x in rect.x..rect.right() {
                if self.pixel(x, y) == colour {
                    n += 1;
                }
            }
        }
        n
    }

    /// Number of pixels inside `rect` that are not the background (the
    /// boot screen's background is a bloom; the library says what it is at
    /// each pixel). Text is counted this way: its thin strokes rarely cover
    /// a whole pixel, so few of its pixels have exactly the text colour.
    pub fn ink(&self, rect: &Rect) -> usize {
        let mut n = 0;
        for y in rect.y..rect.bottom() {
            for x in rect.x..rect.right() {
                n += usize::from(self.pixel(x, y) != self.backdrop.pixel(x, y));
            }
        }
        n
    }

    /// Number of pixels inside `rect` drawn in `colour` over the background
    /// at a third of full coverage or more (every channel is the same mix
    /// of the two): tells a hue from white or grey text. Channels in which
    /// the colour and the background are close tell nothing.
    pub fn tinted(&self, rect: &Rect, colour: Rgb) -> usize {
        let mut n = 0;
        for y in rect.y..rect.bottom() {
            for x in rect.x..rect.right() {
                let (p, bg) = (self.pixel(x, y), self.backdrop.pixel(x, y));
                let shares: Vec<f32> = [(p.r, bg.r, colour.r), (p.g, bg.g, colour.g), (p.b, bg.b, colour.b)]
                    .into_iter()
                    .filter(|&(_, bg, c)| (c as i32 - bg as i32).abs() >= 40)
                    .map(|(p, bg, c)| (p as f32 - bg as f32) / (c as f32 - bg as f32))
                    .collect();
                let low = shares.iter().copied().fold(1.0, f32::min);
                let high = shares.iter().copied().fold(0.0, f32::max);
                n += usize::from(shares.len() >= 2 && low >= 0.33 && high <= 1.02 && high - low <= 0.06);
            }
        }
        n
    }

    /// The bounding rectangle of the pixels that differ from `other`.
    pub fn changed(&self, other: &Frame) -> Rect {
        let mut area = Rect::default();
        for y in 0..self.height as i32 {
            for x in 0..self.width as i32 {
                if self.pixel(x, y) != other.pixel(x, y) {
                    area = area.union(&Rect::new(x, y, 1, 1));
                }
            }
        }
        area
    }

    pub fn layout(&self) -> Layout {
        Layout::new(self.width, self.height)
    }

    /// Whether the unlock screen is shown, the glide to it over: the accent
    /// line of its text box, in place and fully faded in.
    pub fn prompt_shown(&self) -> bool {
        self.count(&self.layout().text_box, assets::ACCENT) > 300
    }

    /// Whether the ring of the boot screen is shown: its arc, fully faded in.
    pub fn ring_shown(&self) -> bool {
        self.count(&self.layout().ring, assets::TEXT) >= 6
    }

    /// Number of pixels that differ from `other` outside all of `allowed`.
    pub fn changed_outside(&self, other: &Frame, allowed: &[Rect]) -> usize {
        let mut n = 0;
        for y in 0..self.height as i32 {
            for x in 0..self.width as i32 {
                let inside = allowed.iter().any(|r| r.contains(&Rect::new(x, y, 1, 1)));
                n += usize::from(!inside && self.pixel(x, y) != other.pixel(x, y));
            }
        }
        n
    }

    /// How many dots the text box shows, and whether its caret is visible.
    /// `None` if the box holds anything else in the text colour. For
    /// displays where a logical pixel is one pixel: a dot then has 16
    /// pixels of exactly the text colour, and the caret is 1 by 18.
    pub fn typed(&self) -> Option<(usize, bool)> {
        let layout = self.layout();
        assert_eq!((layout.unit, layout.caret), (1.0, (1, 18)));
        let white = self.count(&layout.text_box, assets::TEXT);
        match white % 16 {
            0 => Some((white / 16, false)),
            2 if white >= 18 => Some(((white - 18) / 16, true)),
            _ => None,
        }
    }
}

/// A running headless boot screen.
pub struct Splash {
    pub dir: PathBuf,
    pub socket: PathBuf,
    pub out: PathBuf,
    pub pty: Pty,
    pub child: Option<Child>,
    snapshots: u32,
}

impl Splash {
    /// Start the worker in the foreground and wait until it is ready.
    pub fn start(name: &str, size: &str, extra: &[&str]) -> Splash {
        let dir = temp_dir(name);
        let socket = dir.join("run").join("socket");
        Self::start_at(dir, socket, size, extra)
    }

    /// The same, with the test's directory and the control socket chosen by
    /// the caller.
    pub fn start_at(dir: PathBuf, socket: PathBuf, size: &str, extra: &[&str]) -> Splash {
        let out = dir.join("out");
        let pty = open_pty();
        let mut child = Command::new(DAEMON)
            .arg(format!("--headless={size}"))
            .arg(format!("--out={}", out.display()))
            .arg(format!("--socket={}", socket.display()))
            .arg(format!("--tty={}", pty.path.display()))
            .arg("--foreground")
            .args(extra)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .spawn()
            .expect("start the daemon");
        let mut line = String::new();
        BufReader::new(child.stdout.take().unwrap()).read_line(&mut line).unwrap();
        assert_eq!(line.trim(), "READY", "the daemon did not report readiness");
        Splash { dir, socket, out, pty, child: Some(child), snapshots: 0 }
    }

    pub fn request(&self, request: Request) -> Response {
        client::request(&self.socket, &request).expect("the daemon answers")
    }

    pub fn ok(&self, request: Request) {
        assert_eq!(self.request(request.clone()), Response::Ok, "{request:?}");
    }

    /// Send a raw payload and return the raw answer, or `None` if the
    /// daemon closed the connection instead.
    pub fn raw(&self, payload: &[u8]) -> Option<Vec<u8>> {
        let mut stream = UnixStream::connect(&self.socket).unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        stream.write_all(payload).unwrap();
        proto::read_frame(&mut stream).ok().map(|p| p.to_vec())
    }

    pub fn snapshot(&mut self) -> Frame {
        self.snapshots += 1;
        let name = format!("frame-{}.ppm", self.snapshots);
        self.ok(Request::Snapshot { name: name.clone() });
        let path = self.out.join(name);
        let frame = Frame::parse_ppm(&std::fs::read(&path).unwrap());
        std::fs::remove_file(path).unwrap();
        frame
    }

    /// Take snapshots until `condition` holds; panics after five seconds.
    pub fn wait_for(&mut self, what: &str, condition: impl Fn(&Frame) -> bool) -> Frame {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let frame = self.snapshot();
            if condition(&frame) {
                return frame;
            }
            assert!(Instant::now() < deadline, "timed out waiting for {what}");
            std::thread::sleep(Duration::from_millis(30));
        }
    }

    /// A client program run against this daemon.
    pub fn client(&self) -> Command {
        let mut command = Command::new(CLIENT);
        command.arg("--socket").arg(&self.socket).stdin(Stdio::null());
        command
    }

    /// A directory in which the client is called `plymouth` and finds this
    /// daemon, as the initramfs would have it in `/bin`.
    pub fn plymouth_dir(&self) -> PathBuf {
        let bin = self.dir.join("bin");
        if !bin.exists() {
            std::fs::create_dir_all(&bin).unwrap();
            std::os::unix::fs::symlink(CLIENT, bin.join("plymouth")).unwrap();
            std::os::unix::fs::symlink(&self.socket, bin.join("boswas-bootsplash.socket")).unwrap();
        }
        bin
    }

    pub fn plymouth(&self) -> Command {
        let mut command = Command::new(self.plymouth_dir().join("plymouth"));
        command.stdin(Stdio::null());
        command
    }

    /// Wait for the daemon to exit by itself.
    pub fn wait_exit(&mut self, limit: Duration) -> Option<std::process::ExitStatus> {
        let mut child = self.child.take()?;
        let deadline = Instant::now() + limit;
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                return Some(status);
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    pub fn kill(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

impl Drop for Splash {
    fn drop(&mut self) {
        self.kill();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Wait for a child process, but not for ever.
pub fn wait_child(mut child: Child, limit: Duration) -> (Option<i32>, Vec<u8>) {
    let deadline = Instant::now() + limit;
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            let mut out = Vec::new();
            if let Some(mut stdout) = child.stdout.take() {
                stdout.read_to_end(&mut out).unwrap();
            }
            return (status.code(), out);
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("a child process did not finish within {limit:?}");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

pub fn wait_until(what: &str, limit: Duration, condition: impl Fn() -> bool) {
    let deadline = Instant::now() + limit;
    while !condition() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(20));
    }
}

pub fn exists(path: &Path) -> bool {
    path.symlink_metadata().is_ok()
}
