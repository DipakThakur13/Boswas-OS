//! What the daemon gives up once it is running.
//!
//! After the display, the console and the control socket are open, the
//! worker keeps only what it still needs:
//!
//! * `no_new_privs`, so nothing it could ever execute gains privileges;
//! * a Landlock domain that allows file access only below the graphics
//!   device directory, the frame buffer device, the console device it
//!   already has open and its own runtime directory, denies TCP entirely
//!   (Linux 6.7 and later), and (where the kernel supports it) blocks
//!   signals and abstract-socket connections to other processes. The rules
//!   belong to the directories
//!   and device nodes that exist when the sandbox is entered; graphics
//!   devices under a device directory made later come from the supervisor
//!   as open descriptors ([`crate::broker`]);
//! * three capabilities out of root's full set: `CAP_SYS_ADMIN` (to become
//!   DRM master again after the screen was hidden or the graphics driver
//!   changed), `CAP_SYS_TTY_CONFIG` (console text/graphics mode) and
//!   `CAP_SYS_CHROOT` (to follow the switch from the initramfs to the real
//!   root).
//!
//! Everything is best effort: an old kernel without Landlock still gets the
//! other restrictions, and a failure here never stops the boot screen.

use std::path::{Path, PathBuf};

use landlock::{
    Access, AccessFs, AccessNet, CompatLevel, Compatible, PathBeneath, PathFd, Ruleset, RulesetAttr,
    RulesetCreatedAttr, RulesetStatus, Scope, ABI,
};
use rustix::thread::{CapabilitySet, CapabilitySets};

/// What was actually enforced.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Report {
    pub no_new_privs: bool,
    /// `full`, `partial`, `none` (kernel without Landlock) or `error`.
    pub landlock: &'static str,
    /// The kernel's Landlock has network rules (ABI 4, Linux 6.7), so TCP is
    /// denied. An older Landlock enforces the file rules only.
    pub landlock_net: bool,
    pub capabilities_dropped: bool,
}

/// Paths the sandboxed daemon may still use.
#[derive(Clone, Debug, Default)]
pub struct Allowed {
    /// Device nodes or directories of device nodes: read, write, ioctl.
    pub devices: Vec<PathBuf>,
    /// Directories it owns: create, write and remove files and sockets.
    pub state: Vec<PathBuf>,
}

/// The ruleset's status and the kernel's Landlock ABI. The ABI is only
/// reported, never used to choose rules: those are the same on every kernel.
fn landlock(allowed: &Allowed) -> Result<(RulesetStatus, ABI), Box<dyn std::error::Error>> {
    let abi = ABI::V6;
    let device_access = AccessFs::ReadFile | AccessFs::WriteFile | AccessFs::ReadDir | AccessFs::IoctlDev;
    let state_access = AccessFs::ReadFile
        | AccessFs::WriteFile
        | AccessFs::ReadDir
        | AccessFs::RemoveFile
        | AccessFs::MakeReg
        | AccessFs::MakeSock
        | AccessFs::Truncate;
    let mut ruleset = Ruleset::default()
        .set_compatibility(CompatLevel::BestEffort)
        .handle_access(AccessFs::from_all(abi))?
        .handle_access(AccessNet::from_all(abi))?
        .scope(Scope::AbstractUnixSocket | Scope::Signal)?
        .create()?;
    let rules =
        allowed.devices.iter().map(|p| (p, device_access)).chain(allowed.state.iter().map(|p| (p, state_access)));
    for (path, access) in rules {
        let Ok(meta) = std::fs::metadata(path) else { continue };
        // Directory-only rights are not valid on a single file.
        let access = if meta.is_dir() { access } else { access & AccessFs::from_file(abi) };
        ruleset = ruleset.add_rule(PathBeneath::new(PathFd::new(path)?, access))?;
    }
    let status = ruleset.restrict_self()?;
    Ok((status.ruleset, ABI::from(status.landlock)))
}

fn drop_capabilities() -> bool {
    if !rustix::process::geteuid().is_root() {
        return false;
    }
    let Ok(current) = rustix::thread::capabilities(None) else { return false };
    let keep =
        (CapabilitySet::SYS_ADMIN | CapabilitySet::SYS_TTY_CONFIG | CapabilitySet::SYS_CHROOT) & current.permitted;
    let _ = rustix::thread::clear_ambient_capability_set();
    rustix::thread::set_capabilities(
        None,
        CapabilitySets { effective: keep, permitted: keep, inheritable: CapabilitySet::empty() },
    )
    .is_ok()
}

/// Apply the restrictions to the calling (single-threaded) process.
pub fn enter(allowed: &Allowed) -> Report {
    let (landlock, landlock_net) = match landlock(allowed) {
        Ok((RulesetStatus::FullyEnforced, kernel)) => ("full", kernel >= ABI::V4),
        Ok((RulesetStatus::PartiallyEnforced, kernel)) => ("partial", kernel >= ABI::V4),
        Ok((RulesetStatus::NotEnforced, _)) => ("none", false),
        Err(_) => ("error", false),
    };
    // Landlock sets no_new_privs itself; set it regardless for kernels without Landlock.
    let no_new_privs = rustix::thread::set_no_new_privs(true).is_ok();
    let capabilities_dropped = drop_capabilities();
    Report { no_new_privs, landlock, landlock_net, capabilities_dropped }
}

/// The device paths the display back ends use.
pub fn display_devices() -> Vec<PathBuf> {
    vec![PathBuf::from(crate::output::drm::DRI_DIR), PathBuf::from(crate::output::fbdev::DEFAULT_DEVICE)]
}

/// Make sure the DRM device directory exists before the sandbox is entered,
/// so that the first graphics driver, loaded later, puts its device into a
/// directory the sandbox allows. (A directory the kernel made itself is
/// removed with its last device and made again for the next driver; for
/// that case see [`crate::broker`].)
pub fn prepare_device_dir() {
    let dir = Path::new(crate::output::drm::DRI_DIR);
    if !dir.exists() && Path::new("/dev").is_dir() {
        let _ = std::fs::create_dir(dir);
    }
}
