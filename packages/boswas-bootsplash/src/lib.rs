//! The Boswas OS boot screen: boot, disk-unlock passphrase prompt and
//! shutdown, drawn by one small program instead of Plymouth.
//!
//! * [`scene`] decides what is on screen and repaints only what changed;
//!   [`gfx`], [`text`] and [`assets`] are the software renderer and the
//!   artwork embedded at build time.
//! * [`output`] puts frames on a display: DRM/KMS dumb buffers, a legacy
//!   frame buffer, or files (headless, for tests).
//! * [`daemon`] is the event loop; [`launch`] starts it safely; [`tty`] and
//!   [`editor`] handle the console keyboard and the passphrase;
//!   [`sandbox`] is what the daemon gives up once running; [`runtime`] is
//!   its lock and runtime directory; [`broker`] is how the sandboxed daemon
//!   gets a graphics device that appeared after it started.
//! * [`proto`], [`client`], [`compat`], [`cli`] and [`agent`] are the control
//!   protocol and the command line client, including the Plymouth-compatible
//!   subset used inside the initramfs and the systemd password agent.
//!
//! The crate forbids `unsafe` code everywhere except the `sys` module, which
//! wraps the few system calls that have no safe binding.
#![deny(unsafe_code)]

pub mod agent;
pub mod assets;
pub mod broker;
pub mod cli;
pub mod client;
pub mod cmdline;
pub mod compat;
pub mod daemon;
pub mod editor;
pub mod gfx;
pub mod launch;
pub mod output;
pub mod png;
pub mod proto;
pub mod runtime;
pub mod sandbox;
pub mod scene;
pub mod sys;
pub mod text;
pub mod tty;

/// Version shown by `--version` (the Debian package version).
pub const VERSION: &str = "3.0~alpha1";

#[cfg(test)]
mod tests {
    #[test]
    fn the_version_is_the_package_version() {
        let changelog = include_str!("../debian/changelog");
        let first = changelog.lines().next().unwrap();
        assert_eq!(first, format!("boswas-bootsplash ({}) trixie; urgency=medium", super::VERSION));
    }
}

/// Where unit tests put Unix sockets. A socket path holds about a hundred
/// bytes, so a long `TMPDIR` (some build environments have one) cannot be
/// used for them.
#[cfg(test)]
pub(crate) fn socket_test_dir() -> std::path::PathBuf {
    let dir = std::env::temp_dir();
    if dir.as_os_str().len() <= 40 {
        dir
    } else {
        std::path::PathBuf::from("/tmp")
    }
}
