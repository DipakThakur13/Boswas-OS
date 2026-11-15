# Boswas OS v1 security baseline

Implemented by the `boswas-security` package, the installer preseed and the
image manifest, and for the desktop by `boswas-desktop` and
`boswas-bootsplash` (see [Desktop security model](#desktop-security-model)).
`boswas security status` reports each control on a device.
Results are a **local self-assessment**, not remote attestation.

## Controls

| Control | v1 alpha implementation | Status check | Alpha level |
|---------|-------------------------|--------------|-------------|
| Boot chain | Debian-signed shim → GRUB → kernel. The ISO boots with Secure Boot enforced | `secure-boot` | WARN if disabled (required for production) |
| Hardware root of trust | TPM 2.0 detected and reported. Not yet used for keys | `tpm` | WARN if absent |
| Disk encryption | Installer policy: guided LVM inside LUKS2 (cryptsetup default), passphrase asked interactively | `disk-encryption` | **FAIL** if root is not encrypted, WARN on LUKS1 |
| Host firewall | nftables: inbound default drop, forward drop, outbound allowed; rate-limited drop log | `firewall` | **FAIL** if inactive |
| Mandatory access control | AppArmor (Debian kernel default LSM) with Debian's shipped profiles | `apparmor` | **FAIL** if disabled or profiles not loaded on an installed system. WARN in the live session: Debian skips profile loading on live media |
| Audit | auditd + rules for identity, privilege and security-config changes, kernel modules, time | `audit` | **FAIL** if inactive |
| Remote access | No SSH server installed. A fresh install of openssh-server is not enabled automatically (systemd preset). If provisioned: keys only, no root, 3 tries | `ssh-server` | WARN if running |
| Administrative access | Root locked by the installer. The installer-created user is in `sudo`; sudo with password, `use_pty`, 5-minute timestamp. No NOPASSWD, no hidden accounts | `root-account` (as root) | WARN if root has a password |
| Passwords | pam_pwquality: min 12 chars, 2 classes, dictionary and username checks, enforced for root | (none) | (none) |
| Screen lock | Owned by the compositor (ADR-0028): locks after at most 10 minutes without input, before suspend and hibernate, and on Super+L. The maximum is a `[policy]` value that a user's configuration cannot raise. A crashed lock screen leaves the session locked and blank | `screen-lock` | WARN if the lock is off or users may turn it off |
| Desktop isolation | The compositor offers ordinary applications the application protocols only: no screen capture, no window list, no clipboard control, no panels or overlays, no input injection. Privileged protocols go only to programs the compositor starts itself; sandboxed clients never get them (ADR-0026) | test suite | (none) |
| Login | greetd with the Boswas greeter, which runs as the unprivileged user `_greetd`; authentication by PAM in greetd. No automatic login on installed systems; text-login fallback (ADR-0030) | test suite | (none) |
| Boot screen and disk passphrase prompt | `boswas-bootsplash` (ADR-0029): root-only control socket with peer check; after start-up `no_new_privs`, a Landlock domain and three capabilities; the passphrase is wiped after use and never logged. Falls back to the text console | test suite | (none) |
| Kernel hardening | kptr/dmesg restrict, no unprivileged BPF, ptrace scope 1, no kexec, restricted SysRq, no ICMP redirects. **User namespaces stay enabled** (Flatpak and browser sandboxes) | (none) | (none) |
| Updates | unattended-upgrades: daily, Debian-Security origin only, no automatic reboot | `updates` | WARN if disabled or lists >7 days old |
| Repository trust | Only Debian archive sources signed by the Debian archive keyring. live-build's unsigned boot-medium source is removed by the installer (`preseed/late_command`) | `apt-trust` | **FAIL** for any `trusted=yes` source on an installed system; INFO for the boot medium in the live session |
| Logging | Persistent journal, 1 GB / 90 days maximum | (none) | (none) |
| Removable media | Policy file and reporting only (`MODE=allow`) | `usb-policy` | INFO, not enforced |
| Windows applications | `boswas-winapp`: one prefix per application, never as root, bubblewrap sandbox (no home, D-Bus, other prefixes; network and devices only when granted), `boswas-winapp` AppArmor profile enforced or nothing runs, catalog statuses incl. `blocked`, fail-closed policy ([details](../compatibility/README.md#security-model)) | `winapp-confinement` (as root) | WARN if the profile is not enforcing on an installed system |
| Device management | `boswas-device-agent.service` sandboxed (ProtectSystem=strict, one capability, no new privileges, system-call filter). Control Plane traffic over mutual TLS with a pinned CA. Only typed commands, no remote shell (ADR-0019). Policies Ed25519-signed and verified before use; they can never disable AppArmor (ADR-0020). Local API authorised by kernel peer credentials. Privacy allowlists on every message. Standalone without a Control Plane ([details](../device-management/README.md)) | `agent`, `enrollment` (informational) | (none) |
| Service minimisation | Network-listening and unmanaged-install software pinned out of the image: KDE Connect, Discover/PackageKit, cups-browsed, openssh-server, fingerprint PAM. The replaced desktop is pinned out as well (Plasma shell and session, KWin, SDDM, System Settings, Plymouth), and no X server is installed | test suite | (none) |

Compliance states:

- `COMPLIANT`: every scored check passes.
- `COMPLIANT_WITH_WARNINGS`: no failures, but WARN or UNKNOWN results.
- `NON_COMPLIANT`: at least one FAIL. `boswas status` exits with code 1.

## Principles

- **No security through obscurity.** Every control is a documented file in
  this repository and on the device.
- **No backdoors.** No hidden administrator, shared password, embedded key or
  remote-access service. Administrative access is the explicit sudo group
  created by the person installing the device.
- **No secrets in images or Git.** `device.conf` holds identifiers only;
  credentials will live in the TPM / OS secret store (Phase 4+).
- **Untrusted installers never run as root.** `boswas-winapp` refuses root
  and runs Windows installers as the user, in per-application prefixes,
  sandboxed and AppArmor-confined.
- **Upstream first.** Debian mechanisms only; no Debian file is modified (see
  ADR-0006).

## Desktop security model

The desktop is the native Boswas desktop (ADR-0026 to ADR-0032): a Wayland
compositor, a shell, a lock screen, a login screen and a boot screen, all
written in Rust. In the shipped code `unsafe` is confined to six audited
files, and the static test stage fails if it appears in any other.

### What an ordinary application cannot do

An application that connects to the compositor (any Qt, GTK, Wine or
Android window) is an ordinary client. It cannot:

- **read the screen** or the contents of another window: no screen-capture
  protocol is offered to applications. (Consequence: screenshots and screen
  sharing from applications do not work yet. A portal with a visible
  indicator is planned.)
- **see which windows exist**, their titles or their applications: the
  window list is offered to the shell only.
- **receive keys or pointer events meant for another window.** A focused
  client may ask to receive the window-management shortcuts itself (a
  remote desktop or a virtual machine); Super+L and virtual-terminal
  switching always stay with the compositor.
- **inject input**: there is no virtual keyboard or pointer protocol.
- **read or replace the clipboard in the background**: it gets the
  selection only while it has keyboard focus. Clipboard control is offered
  to the shell only.
- **draw panels or overlays**, or become the lock screen: layer shell and
  session lock are offered to the shell only.
- **take focus without the user**: an activation request is honoured only
  with a token from the client that currently has focus.
- **move or resize itself without a press** from the user, or position
  itself on the screen at all.
- **unlock the session, list windows, take a screenshot or end the
  session through the control socket**: a connection to the socket file
  gets `version`, `lock` and `reload-config`.

Windows and Android applications reach the screen through the same
interfaces and get no extra rights. The WinCompat sandbox binds only the
Wayland socket when the display is granted; there is no X server, so the
earlier X11 caveat (clients observing each other) no longer applies.

### Who is trusted

- **Programs the compositor starts itself**: `boswas-shell` (panel,
  launcher, wallpaper), `boswas-shell --lock` and, on the login screen,
  `boswas-shell --greeter`. Each gets a Wayland connection and a control
  descriptor that the compositor created for that one process. A program
  cannot obtain them by connecting to a socket, and the shell removes both
  from the environment of every application it starts.
- **Sandboxed clients** (connected through the security-context protocol,
  as Flatpak does) never get privileged protocols, whatever the policy.
- **`policy.privileged_protocols = "all"`** is a development setting that
  trusts every unsandboxed client. It can only be set in the system files
  (`/usr/share/boswas/compositor/compositor.toml`,
  `/etc/boswas/compositor.toml`); the image check fails the build if the
  shipped value is not `"shell"`.

### Lock

- Locking blanks every output and takes input away from applications
  before the lock screen is started. Only a lock client's unlock request,
  sent after PAM accepted the password, ends the lock.
- If the lock screen exits or crashes, the session stays locked and blank
  and the lock screen is started again after three seconds.
- The lock screen checks the password of the user who owns the session
  with PAM service `boswas-lock` (`common-auth`, `common-account`). An
  expired or locked account does not unlock. The typed password is kept in
  memory that is wiped, is shown as dots and is never logged. The
  compositor never sees it.
- Any program of the user may lock (`boswas-compositor --send lock`); the
  suspend hook uses that. Nothing but the lock screen unlocks.

### Login

- greetd starts the greeter as the system user `_greetd`. The greeter is
  the compositor with `greeter.toml`: no applications, no terminal
  shortcut, no lock, and the greeter as its only trusted program.
- The greeter sends the user name and the password to greetd over greetd's
  socket once and wipes its copy; greetd runs PAM and starts the session as
  the user.
- The shipped `/etc/boswas/greetd.toml` signs nobody in automatically (the
  build fails otherwise). Only the live medium adds an automatic login for
  the live user, at boot.

### Boot screen

`boswas-bootsplashd` runs as root in the initramfs and reads the disk
passphrase from the console keyboard. Its control socket is root-only and
checks the peer. Once the display, the console and the socket are open it
sets `no_new_privs`, enters a Landlock domain limited to the graphics
devices and its runtime directory with no TCP, and keeps three
capabilities. Nothing is parsed from disk at boot: the artwork and the
font are compiled in. If it cannot start or dies, the passphrase is asked
for on the text console.

### Privileges

- The compositor, the shell and the lock screen run as the logged-in
  user. Graphics and input devices are opened through libseat and logind;
  nothing in `boswas-desktop` is setuid, setgid or carries a file
  capability (checked by the package and image tests).
- Configuration is layered so that policy cannot be relaxed from a user's
  home directory: `[policy]` is read from the system files only.

### State of verification

Tested in the development environment: the trust filtering, the limits of
the public control socket, the lock staying in place across crashed lock
screens (end-to-end tests against the real compositor, headless); the
policy layering; the greeter's message handling; the boot screen's
protocol, sandbox and passphrase path with Debian's unmodified unlock
helper. **Not yet verified:** anything on a real boot (QEMU or hardware):
the DRM and input path, greetd login and PAM on the image, suspend and
resume, the boot screen on an encrypted disk. No external security review
has taken place.

## Live session

The live ISO session is a demonstration and diagnostics environment. It is
not a managed device. Upstream Debian behaviour differs there:

- **AppArmor:** profiles are not loaded (Debian's `apparmor.service`
  carries `ConditionPathExists=!/run/live/overlay/work`).
- **APT:** the boot medium's package pool is an unsigned APT source.

`boswas status` reports both instead of hiding them. live-config creates the `boswas` user (password
`live`) with passwordless sudo, and nothing is persisted. Whether this
default stays for production images is an open item (see the roadmap). The
installed system never inherits the live user.

## Known gaps (tracked in the roadmap)

- **USB storage:** not enforced.
- **AppArmor:** Boswas profile for Windows applications only (`boswas-winapp`);
  browsers use Debian's profiles.
- **Secure Boot:** not enforced as a policy; the device only reports its state.
- **TPM:** not yet used to protect LUKS or device keys.
- **Release artifacts:** not yet signed.
- **systemd units:** the device agent and the Control Plane are sandboxed;
  the per-user session agent only sets NoNewPrivileges (bubblewrap, which it
  starts through boswas-winapp, needs user namespaces).
- **Device credentials:** the device key is a root-only file, not yet
  TPM-backed. Device certificates last 365 days; renewal means re-enrollment.
- **Windows applications:** 64-bit and 32-bit (ADR-0022). The i386
  libraries of wine32 are a second library stack that Debian security
  updates must keep current.
- **Direct Wine use:** running `/usr/bin/wine` directly is not mediated (it runs with the user's normal rights).
- **Desktop, same-user programs:** the trust model separates applications
  from the shell at the protocol level. It is not a boundary against a
  program that runs **unsandboxed as the same user**. The configuration
  is closed to it: `[policy]`, `[shell] command` and `[lock] command`
  (the programs the compositor starts as trusted) are read from the
  system files only and ignored in `~/.config/boswas/compositor.toml`.
  But such a program can still edit the user's other files, such as
  `~/.profile`, and attach a debugger to the user's processes where the
  kernel allows it. Confining untrusted programs is the job of the
  sandboxes (Flatpak, WinCompat with AppArmor).
- **Desktop, missing controls:** no screen-capture portal with consent and
  indicator; `loginctl lock-session` and D-Bus screen-saver requests are
  not handled; no fingerprint or smart-card unlock; no limit on password
  attempts at the lock screen beyond PAM's delay.
- **Desktop, verification:** the native desktop and the boot screen have
  not run on a real boot yet (see above), and their Rust dependencies come
  from crates.io without an automated advisory check (ADR-0032).
