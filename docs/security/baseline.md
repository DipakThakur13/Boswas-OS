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
| Passwords | pam_pwquality: min 12 chars, 2 classes, dictionary and username checks, enforced for root. For a password an administrator sets for another user in Control Center > Users, which does not pass PAM, the dialog applies the length and class rules itself and nothing more (see [System applications and Settings](#system-applications-and-settings)) | (none) | (none) |
| Screen lock | Owned by the compositor (ADR-0028): locks after at most 10 minutes without input, before suspend and hibernate, and on Super+L. The maximum is a `[policy]` value that a user's configuration cannot raise. A crashed lock screen leaves the session locked and blank | `screen-lock` | WARN if the lock is off or users may turn it off |
| Desktop isolation | The compositor offers ordinary applications the application protocols only: no window list, no clipboard control, no panels or overlays, no input injection. Privileged protocols go only to programs the compositor starts itself; sandboxed clients never get them (ADR-0026). A program gets pictures of the screen only after the user has allowed it in a dialog of the shell, and a mark on the taskbar shows while it may (ADR-0037). X11 applications share one X server and are not isolated from each other (ADR-0034) | test suite | (none) |
| Login | greetd with the Boswas greeter, which runs as the unprivileged user `_greetd`; authentication by PAM in greetd. No automatic login on installed systems; text-login fallback (ADR-0030) | test suite | (none) |
| Boot screen and disk passphrase prompt | `boswas-bootsplash` (ADR-0029): root-only control socket with peer check; after start-up `no_new_privs`, a Landlock domain and three capabilities; the passphrase is wiped after use and never logged. Falls back to the text console | test suite | (none) |
| Kernel hardening | kptr/dmesg restrict, no unprivileged BPF, ptrace scope 1, no kexec, restricted SysRq, no ICMP redirects. **User namespaces stay enabled** (Flatpak and browser sandboxes) | (none) | (none) |
| Updates | unattended-upgrades: daily, Debian-Security origin only, no automatic reboot | `updates` | WARN if disabled or lists >7 days old |
| Repository trust | Only Debian archive sources signed by the Debian archive keyring. live-build's unsigned boot-medium source is removed by the installer (`preseed/late_command`) | `apt-trust` | **FAIL** for any `trusted=yes` source on an installed system; INFO for the boot medium in the live session |
| Logging | Persistent journal, 1 GB / 90 days maximum | (none) | (none) |
| Removable media | Policy file and reporting only (`MODE=allow`) | `usb-policy` | INFO, not enforced |
| Windows applications | `boswas-winapp`: one prefix per application, never as root, bubblewrap sandbox (no home, D-Bus, other prefixes; network and devices only when granted), `boswas-winapp` AppArmor profile enforced or nothing runs, catalog statuses incl. `blocked`, fail-closed policy ([details](../compatibility/README.md#security-model)) | `winapp-confinement` (as root) | WARN if the profile is not enforcing on an installed system |
| Device management | `boswas-device-agent.service` sandboxed (ProtectSystem=strict, one capability, no new privileges, system-call filter). Control Plane traffic over mutual TLS with a pinned CA. Only typed commands, no remote shell (ADR-0019). Policies Ed25519-signed and verified before use; they can never disable AppArmor (ADR-0020). Local API authorised by kernel peer credentials. Privacy allowlists on every message. Standalone without a Control Plane ([details](../device-management/README.md)) | `agent`, `enrollment` (informational) | (none) |
| Service minimisation | Network-listening and unmanaged-install software pinned out of the image: KDE Connect, Discover/PackageKit, cups-browsed, openssh-server, fingerprint PAM. The replaced desktop is pinned out as well (Plasma shell and session, KWin, SDDM, System Settings, Plymouth). The only X server is Xwayland, started by the compositor for the session, without an abstract socket (ADR-0034) | test suite | (none) |

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

The desktop is the native Boswas desktop (ADR-0026 to ADR-0038): a Wayland
compositor, a shell with the session's services, a lock screen, a login
screen and a boot screen, all written in Rust. In the shipped code
`unsafe` is confined to seven audited files, and the static test stage
fails if it appears in any other.

### What an ordinary application cannot do

An application that connects to the compositor (any Qt, GTK, Wine or
Android window) is an ordinary client. It cannot:

- **read the screen** or the contents of another window without the
  user's say. The screen-capture protocol (`wlr-screencopy`) is offered
  to programs outside a sandbox, but a program gets its first picture
  only after the user has answered the shell's question, see
  [Screen capture](#screen-capture).
- **see which windows exist**, their titles or their applications: the
  window list is offered to the shell only.
- **receive keys or pointer events meant for another window.** A focused
  client may ask to receive the window-management shortcuts itself (a
  remote desktop or a virtual machine); Super+L and virtual-terminal
  switching always stay with the compositor.
- **inject input**: there is no virtual pointer protocol, and the
  virtual-keyboard and input-method protocols are offered only to the
  input method the administrator configured (none is shipped).
- **read or replace the clipboard in the background**: it gets the
  selection only while it has keyboard focus. Clipboard control is offered
  to the shell only.
- **draw panels or overlays**, or become the lock screen: layer shell and
  session lock are offered to the shell only.
- **take focus without the user**: an activation request is honoured only
  with a token from the client that currently has focus. Any other
  request marks the window as wanting attention.
- **move or resize itself without a press** from the user, or position
  itself on the screen at all.
- **unlock the session, list windows, take a screenshot or end the
  session through the control socket**: a connection to the socket file
  gets `version`, `lock`, `reload-config`, `outputs` (the monitors and
  their modes, which every client sees anyway) and `capture`, which only
  opens the shell's capture screen for the user. A static test pins this
  set.

Windows and Android applications reach the screen through the same
interfaces and get no extra rights.

### X11 applications

The session has an X server again (Xwayland, ADR-0034). It is an
unprivileged client of the compositor: it gets no privileged protocol
and is not offered screen capture, and it has no abstract socket. But
X11 itself does not separate its clients:

- applications connected to the X server can see each other's windows
  and input, and inject input into each other. They cannot see Wayland
  applications;
- the X server is given the clipboard of Wayland applications only while
  an X11 window has the keyboard, and then every X11 application can
  read it;
- the WinCompat sandbox does not let a Windows application onto that X
  server: where it can give the compositor's Wayland socket it does not
  bind the X11 socket (`boswas_compat/sandbox.py`), so a sandboxed
  application stays an ordinary Wayland client and cannot watch X11
  windows. Wine's Wayland driver on Debian 13 has not been tested.

`[xwayland] enabled = false` in a system file turns the X server off,
and a user's file does not turn it back on (a user can turn it off for
their own session). A device that must never run X11 applications can
also leave the `xwayland` package out.

### Screen capture

- The Print key opens the shell's capture screen: the user sees the
  picture, chooses what to keep and confirms.
- A program that asks the compositor for pictures (a screenshot tool, a
  recorder, the portal back end behind screen sharing) waits until the
  shell has asked "Allow screen capture? NAME wants to see your screen."
  and the user has allowed it. Neither button is the default, Enter
  alone allows nothing, and "Allow" works only after the dialog has been
  visible for a moment.
- The permission covers that program while it keeps capturing and ends
  five seconds after its last picture. A refusal holds for twenty
  seconds. A mark on the taskbar shows while any program may capture; a
  click on it stops them all.
- Nothing is captured while the session is locked, and locking ends
  every permission.
- Sandboxed clients are not offered the protocol; they go through the
  portal.

Limits (ADR-0037): the name in the question is the kernel's name for the
process, which a program can set itself, so the question shows that a
capture starts, not who starts it. An allowed program sees everything on
every monitor. This does not stop a program that already runs unsandboxed
as the user from acting in other ways; it closes the silent, direct way
to the pixels. Capture through the portal has not been tested.

### Services in the shell

The shell runs the session's services (ADR-0035): it is the notification
service, the tray host and the polkit authentication agent, and it talks
to NetworkManager, BlueZ, UPower, logind and the sound server.

- **Input from other programs is not trusted.** Notifications, tray items
  and media players can send anything: values are read by type, text is
  stripped of control characters and bidirectional overrides and cut to
  fixed lengths, markup is removed and not interpreted, image data in
  notifications is dropped unread, tray pictures are size-limited, and
  every list is bounded.
- **Authentication.** Requests are accepted only from the connection that
  owns polkit's bus name. The password is checked by polkit's own setuid
  helper, not by the shell; the copy the shell holds is wiped after it
  was written to the helper. Five wrong answers fail a request. When
  several administrators may authorise an action, the dialog lets the
  person choose which of them authenticates.
- **Wi-Fi passphrases** are wiped from the shell's memory after use. The
  copy inside the D-Bus message to NetworkManager is not.
- **Bluetooth.** A device that asks to pair on its own is refused: the
  agent agrees only for a device the person asked to connect to in the
  last 60 seconds.
- **The lock screen and the login screen** run a restricted set: they
  show the state of the network and the battery, change nothing but the
  volume and the brightness, own no bus name, and register no agent.
- **Settings** gets the same backends through
  `boswas-services-bridge`, which runs as the user and offers no power
  actions, notifications or authentication.

None of this has run against the real daemons; the polkit helper
protocol in particular is tested against a stand-in only.

### System applications and Settings

The system applications (`boswas-apps`, ADR-0040) and Control Center are
ordinary clients of the compositor and get no extra rights. Their own
rules: they refuse to run as root; they start other programs with
argument lists and never through a shell; they parse file names, file
contents and terminal output with bounds-checked code and size limits;
Files asks before it starts a program, an executable script or a desktop
entry; the Archive Manager extracts without following symbolic links or
leaving the destination, skips device files, drops set-user-ID bits and
stops at the size of a decompression bomb; deleting goes to the Trash.

Control Center changes desktop settings through two interfaces only
(ADR-0041): the service bridge, and the user's layer of the compositor's
configuration, written by its one audited file writer. It never writes
`[policy]` or the trusted programs, and since the compositor checks each
file on its own, a damaged user file cannot remove an administrator's
policy.

User accounts are changed through AccountsService, which asks polkit. A
person's own password goes through `passwd` and PAM. A password that an
administrator sets for someone else is hashed in Control Center as
SHA-512 crypt and passed to the service over D-Bus, never on a command
line. Two limits follow from that path: the hash is not Debian 13's
default scheme (yescrypt), and the password does not pass through PAM.
The dialog therefore reads the device's `pwquality` settings and applies
their minimum length and number of character classes itself (12
characters and two classes with this baseline), and refuses the user
name; the dictionary and other `pam_pwquality` checks are not made on
this path.

Boswas maintains this code itself; Debian's security support covers Qt,
PySide6 and FFmpeg beneath it. None of the applications has run on the
desktop yet, and none has had a security review.

### Who is trusted

- **Programs the compositor starts itself**: `boswas-shell` (taskbar,
  Start, flyouts, wallpaper), `boswas-shell --lock`, on the login screen
  `boswas-shell --greeter`, and an input method if `[input_method]
  command` names one (none is shipped). Each gets a Wayland connection
  and a control descriptor that the compositor created for that one
  process. The input method's connection carries the input-method and
  virtual-keyboard protocols only, and its control descriptor is not a
  trusted one. A program cannot obtain them by connecting to a socket, and
  the shell removes both from the environment of every application it
  starts. Which programs these are is read from the system files only: a
  user's configuration cannot name them. A configured input method has
  the same trust as the shell.
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
screens, the screen-capture question (end-to-end tests against the real
compositor, headless); the policy layering; the service backends and the
polkit agent against stand-ins of the system daemons; the greeter's
message handling; the boot screen's protocol, sandbox and passphrase path
with Debian's unmodified unlock helper. **Not yet verified:** anything on
a real boot (QEMU or hardware) or against a real system service: the DRM
and input path, greetd login and PAM on the image, suspend and resume,
the boot screen on an encrypted disk, polkit, NetworkManager and BlueZ,
the portal. No external security review has taken place. The full list
is in [verification.md](../architecture/verification.md).

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
  is closed to it: `[policy]`, `[shell] command`, `[lock] command` and
  `[input_method] command` (the programs the compositor starts as
  trusted) are read from the system files only and ignored in
  `~/.config/boswas/compositor.toml`.
  But such a program can still edit the user's other files, such as
  `~/.profile`, and attach a debugger to the user's processes where the
  kernel allows it. Confining untrusted programs is the job of the
  sandboxes (Flatpak, WinCompat with AppArmor).
- **Desktop, X11:** X11 applications are not isolated from each other,
  and a Windows application started with `DISPLAY` set is one of them
  (see [X11 applications](#x11-applications)).
- **Desktop, screen capture:** the consent question names a process name
  that a program can choose, and allows all screens or none; the portal
  path is untested (see [Screen capture](#screen-capture)).
- **Desktop, missing controls:** D-Bus screen-saver requests are not
  handled (`loginctl lock-session` is, while the shell runs); no
  fingerprint or smart-card unlock; no limit on password attempts at the
  lock screen beyond PAM's delay.
- **Desktop, verification:** the native desktop and the boot screen have
  not run on a real boot yet, and the shell's services and its polkit
  agent have not run against the real daemons (see above). Their Rust
  dependencies come from crates.io without an automated advisory check
  (ADR-0032).
