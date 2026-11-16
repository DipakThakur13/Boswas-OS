//! The console terminal: where passphrase keys come from.
//!
//! Keys are read from the virtual console in raw, no-echo mode, already
//! translated by the kernel with the keymap the initramfs loaded, exactly as
//! Plymouth and the plain console prompt get them. While the boot screen is
//! shown the console is in graphics mode so the kernel does not draw text
//! over it. Everything changed here is put back by [`Console::restore`],
//! which also runs on drop.

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use rustix::termios::{self, InputModes, LocalModes, OptionalActions, SpecialCodeIndex, Termios};

use crate::sys;

/// The default console device: the virtual console in the foreground.
pub const DEFAULT_TTY: &str = "/dev/tty0";

/// The console the boot screen takes keys from.
pub struct Console {
    file: File,
    path: PathBuf,
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
        Ok(Console { file, path, vt, saved_termios: None, saved_mode: None, graphics: false })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Whether this is a virtual console (and not, say, a pseudo-terminal).
    pub fn is_vt(&self) -> bool {
        self.vt.is_some()
    }

    /// Unbuffered, no-echo input; output processing stays as it was so that
    /// other writers to the console still get proper line breaks.
    pub fn set_raw(&mut self) -> io::Result<()> {
        let current = termios::tcgetattr(&self.file)?;
        if self.saved_termios.is_none() {
            self.saved_termios = Some(current.clone());
        }
        let mut raw = current;
        raw.local_modes.remove(
            LocalModes::ICANON
                | LocalModes::ECHO
                | LocalModes::ECHOE
                | LocalModes::ECHOK
                | LocalModes::ECHONL
                | LocalModes::ISIG
                | LocalModes::IEXTEN,
        );
        raw.input_modes.remove(
            InputModes::ICRNL
                | InputModes::INLCR
                | InputModes::IGNCR
                | InputModes::IXON
                | InputModes::ISTRIP
                | InputModes::BRKINT
                | InputModes::PARMRK,
        );
        raw.special_codes[SpecialCodeIndex::VMIN] = 1;
        raw.special_codes[SpecialCodeIndex::VTIME] = 0;
        termios::tcsetattr(&self.file, OptionalActions::Flush, &raw)?;
        Ok(())
    }

    /// Put the terminal settings back (keeps the console mode).
    pub fn restore_termios(&mut self) {
        if let Some(saved) = self.saved_termios.take() {
            let _ = termios::tcsetattr(&self.file, OptionalActions::Now, &saved);
        }
    }

    /// Stop (true) or resume (false) the kernel drawing text on the console.
    /// A no-op on terminals that are not virtual consoles.
    pub fn set_graphics(&mut self, on: bool) {
        if self.vt.is_none() {
            return;
        }
        if self.saved_mode.is_none() {
            self.saved_mode = sys::kd_get_mode(self.file.as_fd()).ok();
        }
        let mode = if on { sys::KD_GRAPHICS } else { sys::KD_TEXT };
        if sys::kd_set_mode(self.file.as_fd(), mode).is_ok() {
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

    /// Read what has been typed. Returns 0 when nothing is waiting.
    pub fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self.file.read(buf) {
            Ok(n) => Ok(n),
            Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted) => Ok(0),
            Err(e) => Err(e),
        }
    }

    /// Print on the text console (used while the boot screen is hidden).
    pub fn print(&mut self, text: &str) {
        let _ = self.file.write_all(text.as_bytes());
    }

    /// Undo everything: terminal settings and text mode.
    pub fn restore(&mut self) {
        self.restore_termios();
        if self.graphics || self.saved_mode.is_some() {
            if self.vt.is_some() {
                let _ = sys::kd_set_mode(self.file.as_fd(), sys::KD_TEXT);
            }
            self.graphics = false;
            self.saved_mode = None;
        }
    }

    /// Force text mode, whatever state this value believes the console is in
    /// (used by the supervisor after the worker died).
    pub fn force_text(&mut self) {
        if self.vt.is_some() {
            let _ = sys::kd_set_mode(self.file.as_fd(), sys::KD_TEXT);
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
