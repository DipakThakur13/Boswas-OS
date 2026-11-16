//! The console terminal: where passphrase keys come from.
//!
//! Keys are read from the virtual console in raw, no-echo mode, already
//! translated by the kernel with the keymap the initramfs loaded, exactly as
//! Plymouth and the plain console prompt get them. Other programs reset the
//! console's terminal settings during boot (systemd does when it starts), so
//! the daemon checks them again and again with [`Console::ensure_raw`].
//! While the boot screen is shown the console is in graphics mode so the
//! kernel does not draw text over it. Everything changed here is put back by
//! [`Console::restore`], which also runs on drop.
//!
//! Once another program runs a graphical session on the console (see
//! [`Console::taken_by_another`]) its text/graphics mode is no longer ours
//! to change: nothing here touches it then.
//!
//! A terminal can be hung up under the daemon: the kernel does that to a
//! console when a program that had it as its controlling terminal ends
//! (every command of systemd's emergency shell unit does), and a login
//! program does it on purpose. Every descriptor that was open then is dead
//! from that moment on and answers each call with an error, so the console
//! could not even be put back into text mode through it. [`Console::revive`]
//! notices and opens the terminal again, through the directory of its
//! device node, which is kept open because paths stop leading there for a
//! process left behind in the initramfs.

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use rustix::fs::{Mode, OFlags};
use rustix::termios::{self, InputModes, LocalModes, OptionalActions, SpecialCodeIndex, Termios};

use crate::sys;

/// The default console device: the virtual console in the foreground.
pub const DEFAULT_TTY: &str = "/dev/tty0";

/// The console the boot screen takes keys from.
pub struct Console {
    file: File,
    path: PathBuf,
    /// The directory of the terminal's device node, to open the terminal
    /// again after it was hung up.
    dir: Option<OwnedFd>,
    /// Hung up, and it could not be opened again.
    lost: bool,
    /// Number of the virtual console, when this is one.
    vt: Option<u16>,
    saved_termios: Option<Termios>,
    saved_mode: Option<i32>,
    graphics: bool,
}

impl std::fmt::Debug for Console {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Console")
            .field("path", &self.path)
            .field("vt", &self.vt)
            .field("graphics", &self.graphics)
            .finish()
    }
}

/// The local and input modes that must be off for unbuffered, unechoed,
/// untranslated input.
fn cooked_local() -> LocalModes {
    LocalModes::ICANON
        | LocalModes::ECHO
        | LocalModes::ECHOE
        | LocalModes::ECHOK
        | LocalModes::ECHONL
        | LocalModes::ISIG
        | LocalModes::IEXTEN
}

fn cooked_input() -> InputModes {
    InputModes::ICRNL
        | InputModes::INLCR
        | InputModes::IGNCR
        | InputModes::IXON
        | InputModes::ISTRIP
        | InputModes::BRKINT
        | InputModes::PARMRK
}

/// The settings with raw, no-echo input.
fn raw_of(mut settings: Termios) -> Termios {
    settings.local_modes.remove(cooked_local());
    settings.input_modes.remove(cooked_input());
    settings.special_codes[SpecialCodeIndex::VMIN] = 1;
    settings.special_codes[SpecialCodeIndex::VTIME] = 0;
    settings
}

/// Whether the settings are the ones [`raw_of`] produces.
fn is_raw(settings: &Termios) -> bool {
    !settings.local_modes.intersects(cooked_local())
        && !settings.input_modes.intersects(cooked_input())
        && settings.special_codes[SpecialCodeIndex::VMIN] == 1
        && settings.special_codes[SpecialCodeIndex::VTIME] == 0
}

/// Whether a console with this switching mode (`VT_GETMODE`) and keyboard
/// mode (`KDGKBMODE`) is run by a graphical session. logind, seatd and
/// display servers that manage the console themselves all take control of
/// console switching (`VT_PROCESS`), turn the console keyboard off
/// (`K_OFF`), or both; the boot screen does neither.
pub fn session_owns(vt_mode: Option<u8>, kb_mode: Option<i32>) -> bool {
    vt_mode == Some(sys::VT_PROCESS) || kb_mode == Some(sys::K_OFF)
}

fn open_tty(path: &Path) -> io::Result<File> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOCTTY | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
}

impl Console {
    /// Open a terminal. `/dev/tty0` is resolved to the console in the
    /// foreground (`/dev/ttyN`), so later mode changes apply to that console
    /// even if the user switches away. Terminals that are not virtual
    /// consoles (a pseudo-terminal in tests) work for input only.
    pub fn open(path: &Path) -> io::Result<Console> {
        let mut file = open_tty(path)?;
        let mut path = path.to_path_buf();
        let mut vt = None;
        if let Ok(active) = sys::vt_active(file.as_fd()) {
            vt = Some(active);
            if path == Path::new(DEFAULT_TTY) && active > 0 {
                let specific = PathBuf::from(format!("/dev/tty{active}"));
                if let Ok(f) = open_tty(&specific) {
                    file = f;
                    path = specific;
                }
            }
        }
        let dir = path.parent().and_then(|dir| {
            rustix::fs::open(dir, OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC, Mode::empty()).ok()
        });
        Ok(Console { file, path, dir, lost: false, vt, saved_termios: None, saved_mode: None, graphics: false })
    }

    /// Open the terminal again: by its name in the directory that was kept
    /// open, else by its path.
    fn reopen(&mut self) -> bool {
        let flags = OFlags::RDWR | OFlags::NOCTTY | OFlags::NONBLOCK | OFlags::CLOEXEC;
        let reopened = match (&self.dir, self.path.file_name()) {
            (Some(dir), Some(name)) => rustix::fs::openat(dir, name, flags, Mode::empty()).map(File::from).ok(),
            _ => open_tty(&self.path).ok(),
        };
        match reopened {
            Some(file) if termios::tcgetattr(&file).is_ok() => {
                self.file = file;
                self.lost = false;
                true
            }
            _ => {
                self.lost = true;
                false
            }
        }
    }

    /// Make sure the terminal can be used: if it was hung up since it was
    /// opened, open it again. Returns false when it is gone for good. The
    /// terminal settings are not touched (a hang-up resets them; see
    /// [`Console::ensure_raw`]).
    pub fn revive(&mut self) -> bool {
        if termios::tcgetattr(&self.file).is_ok() {
            self.lost = false;
            return true;
        }
        self.reopen()
    }

    /// Whether the terminal was hung up and could not be opened again:
    /// nothing can be read from it, and waiting for input on it would
    /// return at once for ever.
    pub fn lost(&self) -> bool {
        self.lost
    }

    /// Switch the console between text and graphics mode. A terminal that
    /// is hung up at just that moment is opened again and asked again.
    fn set_mode(&mut self, mode: i32) -> bool {
        for _ in 0..3 {
            if sys::kd_set_mode(self.file.as_fd(), mode).is_ok() {
                return true;
            }
            if !self.reopen() {
                return false;
            }
        }
        false
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Whether this is a virtual console (and not, say, a pseudo-terminal).
    pub fn is_vt(&self) -> bool {
        self.vt.is_some()
    }

    /// Unbuffered, no-echo input; output processing stays as it was so that
    /// other writers to the console still get proper line breaks. Input
    /// typed before the call is discarded.
    pub fn set_raw(&mut self) -> io::Result<()> {
        let current = termios::tcgetattr(&self.file)?;
        if self.saved_termios.is_none() {
            self.saved_termios = Some(current.clone());
        }
        termios::tcsetattr(&self.file, OptionalActions::Flush, &raw_of(current))?;
        Ok(())
    }

    /// Put the terminal back into raw, no-echo mode if another program
    /// changed its settings since [`Console::set_raw`]. Keys already typed
    /// are kept. Returns true when the settings had to be put back.
    pub fn ensure_raw(&mut self) -> bool {
        if self.saved_termios.is_none() || !self.revive() {
            return false; // never made raw, already given back, or gone
        }
        match termios::tcgetattr(&self.file) {
            Ok(current) if !is_raw(&current) => {
                termios::tcsetattr(&self.file, OptionalActions::Now, &raw_of(current)).is_ok()
            }
            _ => false,
        }
    }

    /// Put the terminal settings back (keeps the console mode).
    pub fn restore_termios(&mut self) {
        if let Some(saved) = self.saved_termios.take() {
            self.revive();
            let _ = termios::tcsetattr(&self.file, OptionalActions::Now, &saved);
        }
    }

    /// Whether another program runs a graphical session on this console now
    /// (a compositor started by the login manager). From then on the
    /// console's text/graphics mode belongs to that program.
    pub fn taken_by_another(&mut self) -> bool {
        if self.vt.is_none() {
            return false;
        }
        self.revive();
        session_owns(sys::vt_mode(self.file.as_fd()).ok(), sys::kb_mode(self.file.as_fd()).ok())
    }

    /// Stop (true) or resume (false) the kernel drawing text on the console.
    /// A no-op on terminals that are not virtual consoles, and on a console
    /// another program has taken.
    pub fn set_graphics(&mut self, on: bool) {
        if self.vt.is_none() || self.taken_by_another() {
            return;
        }
        if self.saved_mode.is_none() {
            self.saved_mode = sys::kd_get_mode(self.file.as_fd()).ok();
        }
        let mode = if on { sys::KD_GRAPHICS } else { sys::KD_TEXT };
        if self.set_mode(mode) {
            self.graphics = on;
        }
    }

    /// Whether the console is currently in graphics mode because of us.
    pub fn graphics(&self) -> bool {
        self.graphics
    }

    /// Leave the console in graphics mode when this value is dropped: the
    /// program that takes the display next owns the console from here on.
    pub fn disown_graphics(&mut self) {
        self.graphics = false;
        self.saved_mode = None;
    }

    /// Whether this console is (still) the one in the foreground.
    pub fn is_foreground(&self) -> bool {
        match (self.vt, sys::vt_active(self.file.as_fd())) {
            (Some(mine), Ok(active)) => mine == active,
            _ => true,
        }
    }

    /// Caps Lock state, when the console can tell.
    pub fn caps_lock(&self) -> bool {
        self.vt.is_some() && sys::kb_leds(self.file.as_fd()).map(|leds| leds & sys::LED_CAPS != 0).unwrap_or(false)
    }

    /// Read what has been typed. Returns 0 when nothing is waiting. A
    /// terminal that turns out to be hung up is opened again (or marked as
    /// lost, see [`Console::lost`]); what is typed from then on is read by
    /// the next call.
    pub fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self.file.read(buf) {
            Ok(0) => {
                // End of input on a terminal: it was hung up.
                self.reopen();
                Ok(0)
            }
            Ok(n) => Ok(n),
            Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted) => Ok(0),
            Err(e) => {
                self.reopen();
                Err(e)
            }
        }
    }

    /// Print on the text console (used while the boot screen is hidden).
    pub fn print(&mut self, text: &str) {
        let _ = self.file.write_all(text.as_bytes());
    }

    /// Undo everything: terminal settings and text mode. The mode is left
    /// alone on a console another program has taken.
    pub fn restore(&mut self) {
        self.restore_termios();
        if self.graphics || self.saved_mode.is_some() {
            if self.vt.is_some() && !self.taken_by_another() {
                self.set_mode(sys::KD_TEXT);
            }
            self.graphics = false;
            self.saved_mode = None;
        }
    }

    /// Force text mode, whatever state this value believes the console is in
    /// (used by the supervisor after the worker died), unless another
    /// program has taken the console.
    pub fn force_text(&mut self) {
        if self.vt.is_some() && !self.taken_by_another() {
            self.set_mode(sys::KD_TEXT);
        }
        self.graphics = false;
    }

    /// Save the terminal settings without changing them, so a later
    /// [`Console::restore_termios`] can put them back.
    pub fn remember_termios(&mut self) {
        if self.saved_termios.is_none() {
            self.saved_termios = termios::tcgetattr(&self.file).ok();
        }
    }
}

impl AsFd for Console {
    fn as_fd(&self) -> std::os::fd::BorrowedFd<'_> {
        self.file.as_fd()
    }
}

impl Drop for Console {
    fn drop(&mut self) {
        self.restore();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustix::pty::{grantpt, openpt, ptsname, unlockpt, OpenptFlags};

    /// A pseudo-terminal: the master end and the console opened on the slave.
    fn pty() -> (File, Console) {
        let master = openpt(OpenptFlags::RDWR | OpenptFlags::NOCTTY).unwrap();
        grantpt(&master).unwrap();
        unlockpt(&master).unwrap();
        let slave = ptsname(&master, Vec::new()).unwrap();
        let console = Console::open(Path::new(slave.to_str().unwrap())).unwrap();
        (File::from(master), console)
    }

    fn cooked(master: &File) -> bool {
        termios::tcgetattr(master).unwrap().local_modes.contains(LocalModes::ECHO | LocalModes::ICANON)
    }

    /// What systemd does to the console when it starts: sane line settings.
    fn reset(master: &File) {
        let mut sane = termios::tcgetattr(master).unwrap();
        sane.local_modes.insert(LocalModes::ICANON | LocalModes::ECHO | LocalModes::ISIG);
        sane.input_modes.insert(InputModes::ICRNL);
        termios::tcsetattr(master, OptionalActions::Now, &sane).unwrap();
    }

    #[test]
    fn raw_mode_is_put_back_after_another_program_reset_the_terminal() {
        let (mut master, mut console) = pty();
        assert!(cooked(&master));
        assert!(!console.ensure_raw(), "nothing to put back before raw mode was asked for");
        assert!(cooked(&master));
        console.set_raw().unwrap();
        assert!(!cooked(&master));
        assert!(!console.ensure_raw(), "untouched settings are left alone");

        reset(&master);
        assert!(cooked(&master));
        assert!(console.ensure_raw());
        assert!(!cooked(&master), "echo and line buffering are off again");
        assert!(!console.ensure_raw());

        // Keys typed before the settings are put back are not thrown away.
        reset(&master);
        master.write_all(b"k\r").unwrap();
        assert!(console.ensure_raw());
        let mut buf = [0u8; 8];
        let mut got = Vec::new();
        for _ in 0..50 {
            let n = console.read(&mut buf).unwrap();
            got.extend_from_slice(&buf[..n]);
            if got.len() >= 2 {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert_eq!(got.first(), Some(&b'k'), "{got:?}");

        // Giving the terminal back ends it: the settings are no longer ours.
        console.restore_termios();
        assert!(cooked(&master));
        assert!(!console.ensure_raw());
        assert!(cooked(&master));
    }

    #[test]
    fn a_terminal_that_was_hung_up_is_opened_again() {
        let (mut master, mut console) = pty();
        console.set_raw().unwrap();
        assert!(console.revive() && !console.lost());
        // Stand-in for the hang-up: the descriptor the console holds stops
        // being a terminal (every call on a hung-up terminal fails too).
        let path = console.path().to_path_buf();
        console.file = File::open("/dev/null").unwrap();
        assert!(termios::tcgetattr(&console.file).is_err());
        assert!(console.revive(), "found again through the directory of its device node");
        assert!(!console.lost());
        assert!(termios::tcgetattr(&console.file).is_ok());
        // It is the same terminal: what is typed arrives, raw mode can be put back.
        assert!(!console.ensure_raw(), "nothing changed the settings in between");
        master.write_all(b"x").unwrap();
        let mut buf = [0u8; 4];
        let mut got = 0;
        for _ in 0..50 {
            got = console.read(&mut buf).unwrap();
            if got > 0 {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert_eq!(&buf[..got], b"x");
        // Reading end-of-input also means a hang-up, and is handled the same way.
        console.file = File::open("/dev/null").unwrap();
        assert_eq!(console.read(&mut buf).unwrap(), 0);
        assert!(!console.lost() && termios::tcgetattr(&console.file).is_ok());
        // Putting the settings back works through the reopened terminal.
        console.file = File::open("/dev/null").unwrap();
        console.restore_termios();
        assert!(cooked(&master));

        // A terminal that no longer exists is lost, and says so.
        console.file = File::open("/dev/null").unwrap();
        console.dir = None;
        console.path = PathBuf::from("/nonexistent/tty");
        assert!(!console.revive() && console.lost());
        assert!(!console.ensure_raw());
        assert_ne!(console.path(), path);
    }

    #[test]
    fn a_console_run_by_a_graphical_session_is_recognised() {
        // The kernel's own console, or one the boot screen put into graphics mode.
        assert!(!session_owns(Some(0), Some(1)));
        assert!(!session_owns(Some(0), Some(3)));
        assert!(!session_owns(None, None));
        // logind and seatd: switching under the session's control, keyboard off.
        assert!(session_owns(Some(sys::VT_PROCESS), Some(sys::K_OFF)));
        assert!(session_owns(Some(sys::VT_PROCESS), Some(3)));
        assert!(session_owns(Some(0), Some(sys::K_OFF)));
        // A pseudo-terminal is nobody's console.
        let (_master, mut console) = pty();
        assert!(!console.is_vt() && !console.taken_by_another());
        console.set_graphics(true);
        assert!(!console.graphics());
    }
}
