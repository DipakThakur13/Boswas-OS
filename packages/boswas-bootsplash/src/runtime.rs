//! The daemon's runtime directory (`/run/boswas-bootsplash`): the control
//! socket, the pid file and the lock.
//!
//! * The **lock** (`lock`, an advisory lock on that file) is held for as
//!   long as a boot screen runs: by the supervisor, or by a worker started
//!   in the foreground. Whoever cannot take it knows a boot screen is
//!   running, even one too busy to answer on its socket, and leaves it and
//!   its files alone. The file itself is never removed.
//! * The supervisor keeps the directory open from before the worker starts
//!   and removes the worker's files through that descriptor. Paths stop
//!   meaning anything to it when the system leaves the initramfs (its root
//!   directory is the emptied initramfs then); the open directory is still
//!   the one in `/run`, which is moved to the real root, not replaced.

use std::ffi::OsString;
use std::fs::File;
use std::io::{self, Read};
use std::os::fd::OwnedFd;
use std::os::unix::fs::DirBuilderExt;
use std::path::Path;

use rustix::fs::{AtFlags, FlockOperation, Mode, OFlags};

/// Name of the pid file, next to the control socket.
pub const PID_FILE: &str = "pid";
/// Name of the lock file, next to the control socket.
pub const LOCK_FILE: &str = "lock";

/// The open runtime directory.
#[derive(Debug)]
pub struct RunDir {
    dir: OwnedFd,
    /// File name of the control socket inside the directory.
    socket: OsString,
}

/// Proof that this process is the one boot screen; released when dropped
/// or when the process ends.
#[derive(Debug)]
pub struct Lock(#[allow(dead_code)] OwnedFd);

impl RunDir {
    /// Open the directory of `socket`, creating it (for root only) if it
    /// does not exist yet.
    pub fn open(socket: &Path) -> io::Result<RunDir> {
        let invalid = || io::Error::new(io::ErrorKind::InvalidInput, "the socket path has no directory");
        let dir = socket.parent().filter(|d| !d.as_os_str().is_empty()).ok_or_else(invalid)?;
        let name = socket.file_name().ok_or_else(invalid)?;
        if !dir.exists() {
            std::fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
        }
        let dir = rustix::fs::open(dir, OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC, Mode::empty())?;
        Ok(RunDir { dir, socket: name.to_owned() })
    }

    /// Take the lock. `None` when another process holds it: a boot screen
    /// is running.
    pub fn lock(&self) -> io::Result<Option<Lock>> {
        let flags = OFlags::RDWR | OFlags::CREATE | OFlags::CLOEXEC | OFlags::NOFOLLOW;
        let file = rustix::fs::openat(&self.dir, LOCK_FILE, flags, Mode::from_raw_mode(0o600))?;
        match rustix::fs::flock(&file, FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => Ok(Some(Lock(file))),
            Err(rustix::io::Errno::WOULDBLOCK) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// The process the pid file names, if there is a pid file.
    pub fn pid(&self) -> Option<u32> {
        let flags = OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW;
        let fd = rustix::fs::openat(&self.dir, PID_FILE, flags, Mode::empty()).ok()?;
        let mut text = String::new();
        File::from(fd).take(32).read_to_string(&mut text).ok()?;
        text.trim().parse().ok()
    }

    /// Remove the control socket and the pid file.
    pub fn remove_files(&self) {
        let _ = rustix::fs::unlinkat(&self.dir, &*self.socket, AtFlags::empty());
        let _ = rustix::fs::unlinkat(&self.dir, PID_FILE, AtFlags::empty());
    }

    /// Whether the socket and pid file in the directory belong to the
    /// process `worker`, or to nobody: there is no pid file, or the process
    /// it names no longer exists. False when another live process owns them.
    pub fn files_belong_to(&self, worker: u32) -> bool {
        match self.pid() {
            None => true,
            Some(pid) => pid == worker || !process_exists(pid),
        }
    }
}

/// Whether a process with this number exists.
pub fn process_exists(pid: u32) -> bool {
    let Some(pid) = i32::try_from(pid).ok().and_then(rustix::process::Pid::from_raw) else {
        return false;
    };
    !matches!(rustix::process::test_kill_process(pid), Err(rustix::io::Errno::SRCH))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = crate::socket_test_dir().join(format!("bbs-run-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn one_holder_of_the_lock_at_a_time() {
        let dir = scratch("lock");
        let socket = dir.join("run").join("socket");
        let first = RunDir::open(&socket).unwrap();
        assert!(dir.join("run").is_dir(), "the directory is created");
        let held = first.lock().unwrap().expect("nobody holds the lock yet");
        // A second instance, with its own view of the directory, is refused...
        let second = RunDir::open(&socket).unwrap();
        assert!(second.lock().unwrap().is_none());
        assert!(first.lock().unwrap().is_none(), "even the holder cannot take it twice");
        // ...until the first one is gone. The file stays.
        drop(held);
        assert!(second.lock().unwrap().is_some());
        assert!(dir.join("run").join(LOCK_FILE).exists());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn files_are_removed_through_the_open_directory() {
        let dir = scratch("moved");
        let socket = dir.join("run").join("socket");
        let run = RunDir::open(&socket).unwrap();
        std::fs::write(&socket, b"").unwrap();
        std::fs::write(dir.join("run").join(PID_FILE), "4242\n").unwrap();
        assert_eq!(run.pid(), Some(4242));
        // The path the directory was opened by stops existing, as it does
        // for a process left behind in the initramfs.
        std::fs::rename(dir.join("run"), dir.join("elsewhere")).unwrap();
        assert!(!socket.exists());
        run.remove_files();
        assert!(!dir.join("elsewhere").join("socket").exists());
        assert!(!dir.join("elsewhere").join(PID_FILE).exists());
        assert_eq!(run.pid(), None);
        run.remove_files(); // nothing left: not an error
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn whose_files_are_in_the_directory() {
        let dir = scratch("owner");
        let socket = dir.join("run").join("socket");
        let run = RunDir::open(&socket).unwrap();
        let pid_file = dir.join("run").join(PID_FILE);
        assert!(run.files_belong_to(77), "no pid file: nobody else's");
        std::fs::write(&pid_file, "77\n").unwrap();
        assert!(run.files_belong_to(77));
        // Another process that is alive (this one) owns them: hands off.
        std::fs::write(&pid_file, format!("{}\n", std::process::id())).unwrap();
        assert!(!run.files_belong_to(77));
        // A process that no longer exists left them behind.
        let mut child = std::process::Command::new("true").spawn().unwrap();
        let gone = child.id();
        child.wait().unwrap();
        assert!(!process_exists(gone) && process_exists(std::process::id()));
        std::fs::write(&pid_file, format!("{gone}\n")).unwrap();
        assert!(run.files_belong_to(77));
        std::fs::write(&pid_file, "not a number").unwrap();
        assert!(run.files_belong_to(77));
        assert!(!process_exists(0) && !process_exists(u32::MAX));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
