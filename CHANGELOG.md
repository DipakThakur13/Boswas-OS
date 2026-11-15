# Changelog

All notable changes to Boswas OS. Versions follow `config/boswas/release.conf`.

## [Unreleased] - 2026-11-15 (native Boswas desktop; cross-platform applications, installer firmware, functional Control Center)

Baseline audit: [docs/reports/2026-11-12-baseline-audit.md](docs/reports/2026-11-12-baseline-audit.md).
Release report: [docs/reports/2026-11-12-release-readiness.md](docs/reports/2026-11-12-release-readiness.md).
Both reports describe the KDE Plasma image of 2026-11-12, before the native
desktop.

**Native Boswas desktop (2026-11-15).** KDE Plasma, KWin, SDDM and Plymouth
are replaced by Boswas components written in Rust: a Wayland compositor, a
shell with lock and login screens, and a boot screen. The Wayland protocol
stays (applications speak it), and KDE applications keep running as ordinary
clients. Entries marked *(native desktop)* below belong to this change.

Verification status of the native desktop:

- **Verified in the development sandbox:** the Rust unit tests; the
  compositor end-to-end tests on the headless backend with real Wayland
  clients; a nested run with the GLES renderer; all package builds and
  lintian; the static, unit and package test stages.
- **Not yet verified:** the ISO build with the new manifest; the QEMU boot
  test; the hardware path (DRM/KMS, libinput, libseat); the greetd login on
  a real boot; the boot screen on real hardware and its disk-unlock prompt;
  dependency resolution of the new image manifest on Debian trixie.

### Fixed
- **Installer firmware (`rtl_nic/rtl8125b-2.fw` prompt):** the ISO had no
  firmware pool for the installer (`--firmware-binary false`), which runs its
  own kernel and initrd. The medium now carries every Debian firmware package
  under `/firmware` with the installer's index. The installer loads missing
  firmware before any network exists and installs it on the target
  (ADR-0023). `config/boswas/firmware-required.list` is a build gate for both
  the installer medium and the image; `tests/installer/` runs the installer's
  own firmware code against the built medium.
- **Installation without internet access:** the installer no longer uses a
  network mirror or the security/updates services, so an unreachable mirror
  or a NIC without a driver cannot stop an installation. The new
  `/usr/lib/boswas/installer/finish-install` writes signature-verified Debian
  sources (`debian.sources`, deb822) when none are configured, removes the live
  medium's unsigned pool, and writes `/var/log/boswas/install-report.txt`.

### Added
- **`boswas-desktop`** *(native desktop)*: a new package with three Rust
  crates and the session files.
  - **`boswas-compositor`**, the Wayland compositor (on Smithay), in place
    of KWin. Server-side window frames (title bar, shadow, close, maximise
    and minimise buttons), move and resize, snapping to halves and quarters
    with a preview, maximise, fullscreen, minimise, four workspaces, a
    most-recently-used window switcher, popups, clipboard and drag and drop,
    fractional scaling, pointer constraints, idle inhibition, xdg-activation.
    Back ends: `udev` (DRM/KMS, libinput, libseat), `winit` (nested, for
    development) and `headless` (software rendering, for tests).
    Configuration in TOML layers (`/usr/share/boswas/compositor/`,
    `/etc/boswas/`, `~/.config/boswas/`); a control socket that offers
    `version`, `lock` and `reload-config` to everyone and everything else
    to the shell only (`boswas-compositor --send lock`).
  - **`boswas-shell`**, in place of plasmashell, the KDE screen locker and
    the SDDM greeter: the desktop background, a 48 px panel (launcher
    button, pinned and running applications, network and battery status,
    clock), the launcher (Super key, local search, application grid, lock,
    log out, restart, shut down), the lock screen (`--lock`, PAM service
    `boswas-lock`) and the login screen (`--greeter`, behind greetd).
  - **`boswas-ui`**: the drawing, text and icon library of the shell and
    the window frames, in safe Rust (`#![forbid(unsafe_code)]`).
  - **Session:** `boswas-session` (in place of `startplasma-wayland`),
    `boswas-session.target`, the greetd configuration
    (`/etc/boswas/greetd.toml`, enabled through a systemd drop-in, with a
    text-login fallback), the portal configuration, and a system-sleep
    hook that locks the session before suspend and hibernate.
- **`boswas-bootsplash`** *(native desktop)*: a new package, the Boswas boot
  screen in Rust in place of Plymouth: boot, shutdown, restart and the
  disk-unlock prompt, on DRM dumb buffers or the frame buffer, repainting
  only what changed. Its client is installed under Plymouth's name inside
  the initramfs only, so the disk-unlock scripts work unchanged; a systemd
  password agent serves prompts after the initramfs
  ([packages/boswas-bootsplash](packages/boswas-bootsplash/README.md)).
- **Boswas UI v2 and the Prism palette** *(native desktop)*:
  `desktop/ui/tokens.json` is the single source of colours (ink, cloud,
  iris, azure, mint, sun, coral, sky, orchid, slate), radii, spacing, type,
  elevation and motion. The shell, the window frames, the boot screen and
  the icon generator read it; the KDE colour schemes `BoswasPrism` (dark,
  the default) and `BoswasPrismLight` are generated from it
  ([desktop/ui](desktop/ui/README.md)).
- **Boswas Prism icon pack** *(native desktop)*: the themes `Boswas-Prism`
  and `Boswas-Prism-Light`, 178 designs in 289 SVG files (applications,
  places, devices, mimetypes, and symbolic status and action icons in a
  light and a dark variant), every colour taken from the tokens. It is the
  default icon theme; the earlier `Boswas` and `Boswas-Light` themes stay
  installed ([desktop/icons](desktop/icons/README.md)).
- **Image** *(native desktop)*: `boswas-desktop`, `boswas-bootsplash`,
  `greetd`, `polkit-kde-agent-1`, `xdg-desktop-portal-gtk`, `qt6-wayland`,
  `breeze-cursor-theme`, `breeze-icon-theme`, `kde-style-breeze`,
  `pavucontrol-qt` and `fonts-inter`. The live session signs in through
  greetd's initial session.
- **Tests** *(native desktop)*: compositor end-to-end tests
  (`packages/boswas-desktop/crates/boswas-compositor/tests/headless.rs`:
  ordinary clients are offered no privileged protocol, a program the
  compositor starts is, the public control socket is limited, a window is
  mapped, listed, tiled, drawn and closed, a locked session stays locked
  when the lock screen dies, and only a lock client unlocks it); the Rust
  suites of both packages and the `desktop/ui` tests in
  `tests/unit/test_units.sh`; static checks for the Cargo versions, the
  trust rules, the files allowed to contain `unsafe`, the greetd and boot
  screen wiring and the icon pack; package, image, security and boot tests
  rewritten for the new desktop (the image and boot stages have not run
  yet).
- **Build** *(native desktop)*: the builder container and
  `build/scripts/build-packages.sh` gain the Rust toolchain (Debian's
  rustc and cargo 1.85) and the libraries the desktop links to. Cargo
  downloads the crates pinned in each `Cargo.lock` from crates.io.
- **32-bit Windows applications (ADR-0022, supersedes ADR-0014):**
  `wine32:i386` (i386 is the only foreign architecture; the system stays
  amd64). PE detection cross-checks the machine field with the
  optional-header magic, and refuses damaged headers, DLLs and scripts with
  the reason. The device agent and Control Plane accept x86 entries. The VM
  test runs a real 32-bit program built from source and checks that it ran
  under WoW64, confined.
- **`boswas-sysops`** (ADR-0024): the only privileged helper. It offers fixed
  operations (APT, cleanup, firmware report, Flatpak remotes, Android
  runtime), each its own polkit action, with JSON Lines progress, safe
  cancellation and fixed exit codes.
- **`boswas-android`** (ADR-0025): Android runtime manager (Waydroid, opt-in
  after installation, VANILLA images), APK inspection (manifest, ABIs, split
  packages, signer) and install/launch/remove. Google Play is **not**
  offered. F-Droid is offered with a pinned signer certificate, alongside
  local APK files.
- **Control Center:** functional Update Center, Android page, application hub
  (Debian packages, Flatpak, AppImage, install from file), Hardware & Drivers
  (devices, drivers, missing firmware), Storage cleanup and live Network
  status ([docs/experience/control-center.md](docs/experience/control-center.md)).
- **Firmware in the image:** firmware-cirrus, firmware-bnx2, firmware-bnx2x,
  firmware-libertas, firmware-zd1211; `/usr/share/boswas/firmware/index.tsv`.
- **libfuse2t64** for AppImages built with the older runtime.
- **Tests:** offline installation scenario (`--scenario install-offline`),
  32-bit WinCompat checks in the VM, installer firmware stage, image checks
  for firmware, sources, Android and Google components.
- **Build:** the previous image is kept in `build/previous/<build ID>/`
  instead of being deleted.

### Changed
- **`boswas-branding`** *(native desktop)*: the default look is Boswas
  Prism (colour scheme `BoswasPrism`, icon theme `Boswas-Prism`, Breeze
  widget style), set as defaults for KDE applications in
  `/usr/share/boswas/kde-settings`. It installs the tokens
  (`/usr/share/boswas/ui/tokens.json`) and both icon packs, and no longer
  has a postinst or triggers (it selected the Plymouth theme).
- **`boswas-preset`** *(native desktop)*: on the native desktop `apply`
  writes `~/.config/boswas/appearance.toml` (light or dark, wallpaper),
  merges the preset's colours into the user's `kdeglobals` for KDE
  applications, sets the Konsole profile and asks the compositor to reload.
  The new `reset` command returns to the default look (Boswas Prism on the
  native desktop, Horizon with its layout on Plasma); Control Center's
  "Reset to Boswas Default" button runs it. The default Konsole profile is
  now `Boswas Prism`, generated from the tokens with its colour scheme.
  On a Plasma session `apply` works as before
  ([desktop/presets](desktop/presets/README.md)).
- **`boswas-cli`** *(native desktop)*: `boswas-info` shows `Boswas Desktop
  <version>`; the `screen-lock` check reads the compositor's lock policy
  (`/usr/share/boswas/compositor/compositor.toml`,
  `/etc/boswas/compositor.toml`) and falls back to the KDE keys on a Plasma
  image.
- **Control Center** *(native desktop)*: About Boswas OS shows the desktop
  (Boswas Desktop and its version) where it showed the Plasma version.
- **`boswas-sysops`** *(native desktop)*: the package policy refuses to
  install the pinned Plasma, KWin, SDDM, System Settings and Plymouth
  packages, and protects `greetd` in place of SDDM, plasma-workspace and
  kwin-wayland.
- **`boswas-compat`** *(native desktop)*: the `display` grant also binds the
  compositor's Wayland socket into the sandbox, and the `boswas-winapp`
  AppArmor profile includes the Wayland abstraction. Without XWayland,
  Windows applications draw through Wine's Wayland driver.
- **`boswas-android`** *(native desktop)*: the session check asks for a
  Wayland session instead of naming Plasma.
- **`boswas-os`** *(native desktop)*: depends on `boswas-desktop` and
  `boswas-bootsplash`.

### Removed
- **From the image** *(native desktop)*: `kde-plasma-desktop`,
  `kwin-wayland`, `sddm`, `sddm-theme-breeze`, `kde-config-sddm`,
  `plasma-nm`, `plasma-pa`, `powerdevil`, `bluedevil`, `kscreen`,
  `systemsettings`, `kinfocenter`, `xdg-desktop-portal-kde`,
  `kde-spectacle`, `plasma-systemmonitor` and Plymouth. APT pins
  (`boswas-exclude.pref`) keep `plasma-workspace`, `plasma-desktop`,
  `kde-plasma-desktop`, `kwin-wayland`, `kwin-x11`, `sddm`,
  `systemsettings`, `plymouth`, `plymouth-themes` and `plymouth-label`
  out; the image verify hook fails the build if one is installed.
- **From the repository** *(native desktop)*: the Plymouth theme
  (`desktop/branding/plymouth/`), the KSplash theme
  (`desktop/branding/splash/`), the Boswas Launcher applet metadata
  (`desktop/launcher/`, Kickoff under the Boswas name), the SDDM
  configuration and theme settings (`desktop/defaults/sddm/`), the Plasma
  environment script (`desktop/defaults/plasma-workspace-env/`) and the
  `ksplashrc` and `kscreenlockerrc` defaults.

### Security
- **Protocol trust** *(native desktop)*: ordinary applications get the
  application protocols only. Layer shell, session lock, the window list
  and clipboard control are offered only to programs the compositor starts
  itself, over a connection it creates for them; sandboxed clients
  (security context) never get them.
- **Compositor-owned lock** *(native desktop)*: locking blanks every output
  and takes input away from applications before the lock screen starts. If
  the lock screen exits without unlocking, the session stays locked and
  blank and the lock screen is started again. Only a lock client's unlock
  request, sent after PAM accepted the password, ends the lock.
- **Lock policy** *(native desktop)*: the session locks after ten minutes
  without input and before suspend. A user may shorten the timeout but not
  lengthen or disable it (`[policy] lock_idle_max_seconds`, read from the
  system files only).
- **Privileges** *(native desktop)*: the compositor and the shell run as
  the logged-in user, without setuid bits or capabilities; device access
  comes from the login session (libseat, logind). The boot screen daemon
  sets `no_new_privs`, enters a Landlock domain where the kernel has
  Landlock, and keeps three capabilities.
- **Memory safety** *(native desktop)*: the new components are Rust.
  `unsafe` is confined to a short list of audited source files (the PAM
  binding, descriptor passing, the EGL context, the boot screen's system
  calls); a static test fails if it appears anywhere else.

### Known limitations
- **Not yet run on real hardware** *(native desktop)*: see the verification
  status above.
- **No XWayland** *(native desktop)*: applications that only speak X11 do
  not run.
- **No notification service, system tray or quick settings** *(native
  desktop)*: there is no graphical Wi-Fi, Bluetooth, volume or brightness
  control in the panel. Wi-Fi is set up with `nmtui` in a terminal, volume
  with `pavucontrol-qt`.
- **Control Center** *(native desktop)*: pages and buttons that open Plasma
  settings modules (Wi-Fi and network, Bluetooth, Display, Sound, Power,
  Users, Devices, and the KDE modules of Personalization, Security, Privacy
  and System) show them as unavailable.
- **No screen capture or screen sharing, no input methods** *(native
  desktop)*.
- **Displays** *(native desktop)*: one GPU only; no HDR and no variable
  refresh rate; one scale for all outputs.
- **`loginctl lock-session` is not handled** *(native desktop)*: lock with
  Super+L, from the launcher, or with `boswas-compositor --send lock`.
- **Presets** *(native desktop)*: the panel, launcher and lock screen
  follow a preset's light or dark variant and wallpaper, not its accent
  colour. The Plasma Global Themes are still generated and installed but
  are used only on a Plasma session.

## [1.0~alpha3] - 2026-10-04 (Device Agent, Compatibility Manager, Control Plane, Boswas OS experience)

### Added
- **The Boswas OS experience** ([docs/experience/README.md](docs/experience/README.md)):
  - **Identity (ADR-0005 revised):** the system presents itself as Boswas
    OS. `os-release` (`ID=boswas`, `ID_LIKE=debian`), console banners and
    motd, boot medium name, About page and live user all say Boswas OS.
    The remaining Debian references and why they stay:
    [docs/experience/debian-references.md](docs/experience/debian-references.md).
  - **Boot:** the Boswas OS Plymouth theme for boot, shutdown and the
    disk-encryption passphrase; a hidden GRUB menu on installed systems,
    keeping Debian's EFI directory for Secure Boot.
  - **Login and session:** a Boswas login screen logo and background, and
    the Boswas session start-up splash.
  - **Ten presets** (Horizon, the default, Midnight, Aurora, Slate,
    Carbon, Pearl, Ocean, Ember, Nebula, Classic). Each brings a Global
    Theme, colour scheme, light, dark and lock wallpapers, panel layout,
    icon treatment and Konsole profile; switch with `boswas-preset` or in
    Control Center ([desktop/presets](desktop/presets/README.md)).
  - **Icons:** the Boswas and Boswas Light icon themes, 61 designs for
    system settings, applications, Boswas applications and Windows
    compatibility ([desktop/icons](desktop/icons/README.md)).
  - **Desktop:** Boswas Launcher (KDE's launcher under the Boswas name)
    and a Boswas panel layout.
  - **Boswas Control Center** (new package `boswas-control-center`): the
    settings hub with the Boswas Security Center, Software Center, Update
    Center, About Boswas OS and, in live sessions, Install Boswas OS.
  - **Terminal:** the Boswas welcome and prompt.
  - **Live USB:** the live desktop starts directly, and installation is
    only by explicit choice (application menu, Control Center or boot
    menu). The new release-blocker boot test (scenario `usb`) checks this
    on a UEFI/Secure Boot USB boot, including that the internal disk is
    unchanged. An opt-in installed-system test is available
    (`./test.sh --install-test`).
- **`boswas-device-agent` package**
  ([docs/device-management/README.md](docs/device-management/README.md)):
  - **Identity:** a persistent, random device identity
    (`/var/lib/boswas/agent/identity.json`). It is never derived from
    hardware identifiers and never silently regenerated; it can be
    pre-provisioned to survive reinstallation.
  - **Configuration:** `device.conf` gains agent settings (Control Plane URL
    and pinned CA, remote commands, heartbeat and inventory intervals,
    inventory, telemetry, update and logging policies). Validation refuses
    secrets and insecure files (`boswas-device config validate`).
  - **Inventory:** allowlisted: OS, Boswas packages, Windows applications
    aggregated over users, Windows runtime health; with `standard` also CPU,
    memory, model and disk. Never serial numbers, MAC addresses or user names.
  - **States:** normalised device states (INITIALIZING, READY, DEGRADED,
    OFFLINE, UPDATING, ERROR, MAINTENANCE) with validated transitions, and
    application states.
  - **`boswas-device-agent.service`** (root, sandboxed):
    - local management API with kernel-credential authorisation;
    - Control Plane client over mutual TLS: enrollment with a one-time token
      and a device-generated EC key, heartbeats, inventory, posture, events;
    - a bounded offline outbox with capped exponential back-off;
    - signed policies;
    - typed command execution, idempotent and expiring;
    - agent updates through `boswas-agent-update@.service`.
  - **`boswas-session-agent.service`** (systemd user unit): the backend of
    the Compatibility Manager, and the executor of remote application
    commands as the logged-in user, through `boswas-winapp`.
  - **`boswas-device`:** status, identity, configuration, inventory, policy,
    commands, events, enrollment, maintenance.
- **`boswas-compat-manager` package (Compatibility Manager, PySide6):**
  - **Pages:** dashboard; application library with filters (All, Installed,
    Running, Updates, Blocked, Repair Required, Unsupported); system status.
  - **Install:** checks the installer first and refuses 32-bit installers
    with the product message.
  - **Application details:** read-only permissions; logs with copy, save and
    clear.
  - **Actions:** launch, stop, repair, update and remove (with an explicit
    removal plan).
  - **Boundaries:** it never runs Windows code or touches prefixes itself.
  - **Dolphin action:** "Install with Boswas Compatibility Manager".
- **Boswas Control Plane** (`control-plane/`, server package
  `boswas-control-plane`, Python stdlib + SQLite, ADR-0017;
  [docs/device-management/control-plane.md](docs/device-management/control-plane.md)):
  - device registry, enrollment with a device CA;
  - one-time enrollment tokens; an enrolled device re-enrolls only with a
    token issued for its ID (`token create --device`);
  - heartbeat, offline detection, inventory and posture;
  - typed command system (lifecycle QUEUED → SENT → ACKNOWLEDGED → RUNNING →
    SUCCEEDED/FAILED, EXPIRED, CANCELLED; idempotency keys; expiry);
  - Ed25519-signed, versioned policies;
  - catalog of 64-bit applications with stored installers (32-bit entries
    are UNSUPPORTED_ARCHITECTURE and never installable);
  - hash-chained audit trail;
  - operator API tokens with roles;
  - HTTPS API `/api/v1` on two ports: the device port (mutual TLS) and the
    operator port (dashboard and operator API; no client-certificate
    request, so browsers show no certificate prompt);
  - web dashboard;
  - `boswas-cp` administration.
- **`boswas-winapp`:**
  - new commands `inspect` (dry run), `stop`, `upgrade`, `runtime`, and
    `logs --launch` and `--clear`;
  - normalised application states;
  - MSI architecture detection;
  - policy keys `BLOCKED_APPLICATIONS`, `ALLOWED_APPLICATIONS`;
  - the managed policy layer (`/var/lib/boswas/compat/policy.conf`).
- **Tests:**
  - unit tests for every component (WinCompat 91, device agent 96,
    Compatibility Manager 105, Control Plane 31 including the end-to-end
    agent ↔ Control Plane tests over mutual TLS, CLI 25);
  - a device management image check;
  - a device management runtime stage in the image (real Wine, session
    agent, Compatibility Manager window, Control Plane);
  - device management checks in the QEMU boot test under AppArmor;
  - new static checks (64-bit only, no shell execution, no Boswas ID,
    service hardening).
- **Documentation:**
  - device management and Control Plane guides;
  - ADR-0017 to ADR-0021;
  - updated compatibility, privacy, security, CLI and build documentation.

### Changed
- **New Boswas OS brand (ADR-0008 revised;
  [desktop/branding](desktop/branding/README.md)):**
  - **Logo:** the Boswas OS "b" mark (gold stem and dot, silver ring and
    swoosh) replaces the Boswas Group gear everywhere: launcher icon, About
    page, boot menu, wallpaper, login and lock screens, installer banner,
    Compatibility Manager icon and dashboard favicon. It is traced from the
    supplied logo (`tools/trace_mark.py`).
  - **Typeface:** Orbitron is the brand typeface. The wordmarks are
    Orbitron outlines, and `boswas-branding` installs the font (SIL OFL 1.1).
  - **Accent:** the logo's gold replaces teal in the KDE colour scheme, the
    boot menu, the installer and the Control Plane dashboard.
- **64-bit only is final (ADR-0014):** Boswas OS v1 runs x86_64 Windows
  applications only. Every 32-bit refusal says "This application requires
  32-bit Windows compatibility, which is not supported by Boswas OS." The
  build fails if `wine32` or i386 multiarch appear.
- **Agent messages (never deployed in M1):**
  - the heartbeat is now liveness only (`heartbeat-v2`);
  - its former content is the status report (`status-report-v1`);
  - typed commands (`device-command-v2`) replace the draft command set.
- **`/etc/boswas/device.conf`:** now configuration only; the agent keeps its
  state in `/var/lib/boswas/agent`. `boswas device status`, `boswas info`
  and the management checks read the agent's identity and status.
- **`boswas-winapp` and the agent binaries** ignore the unit-test
  environment hooks (`BOSWAS_SYSROOT`), so a user cannot point them at a
  policy of their own.
- **Image:** `boswas-os` depends on `boswas-device-agent` and
  `boswas-compat-manager`. The builder image gains PySide6, Qt's offscreen
  platform and openssl.

### Known limitations
- **No Boswas ID:** operators use local API tokens; devices use
  certificates.
- **Control Plane scope:** single tenant, one policy per device (no group
  hierarchy), SQLite, no HA.
- **Device certificates** last 365 days; renewal means re-enrollment.
  The device key is not TPM-backed yet.
- **`UPDATE_AGENT`** needs a Boswas APT repository (M7) to deliver new
  versions.
- **Remote application commands** go to the active user's session only.

## [1.0~alpha2] - 2026-10-04 (Milestone 1: Windows compatibility platform)

### Added
- **`boswas-compat` package (WinCompat).**
  - **`boswas-winapp`:** `install`, `remove`, `list`, `launch`, `status`,
    `repair`, `logs`, `catalog` and `manifest validate`, with `--json` and
    stable exit codes.
  - **Prefixes:** one Wine prefix per application, owned by the user,
    stored in `~/.local/share/boswas/wine/<id>/` and seen as
    `/var/lib/boswas/wine/<id>/` inside its sandbox. Windows software never
    runs as root.
  - **bubblewrap sandbox per application:**
    - never visible: the user's home, `/home`, D-Bus, `/run`, or other
      prefixes;
    - only when granted: network, display, audio, GPU and folders;
    - cleared environment.
  - **AppArmor profile `boswas-winapp`:**
    - attached to the in-sandbox runner;
    - grants no capabilities, mounts, user namespaces or D-Bus;
    - nothing in a prefix may run as a Linux program;
    - Wine refuses to start unless the profile is enforcing.
  - **Compatibility manifest format v1:**
    - JSON schema plus a stdlib validator;
    - statuses `unknown`, `untested`, `experimental`, `tested`,
      `approved`, `blocked`;
    - pinned installer SHA-256 for validated statuses.
  - **Layered compatibility catalog:** managed, local and system layers;
    `blocked` in any layer wins.
  - **Policy:** `/etc/boswas/compat/policy.conf`, fail-closed.
  - **Prefix defaults:** no crash dialog; `winemenubuilder` disabled.
  - **Installer handling:** detection by signature, PE architecture check,
    NSIS/Inno/MSI silent switches.
  - **"Run with Boswas":** handler for `.exe` and `.msi`.
- **Device agent foundations** (`packages/boswas-device-agent/`, not
  packaged yet):
  - API v1 message models and JSON schemas (heartbeat, enrollment,
    compliance, commands);
  - `ControlPlaneClient`, `CredentialStore`, `PolicyVerifier`,
    `InventoryCollector` and `CommandHandler` interfaces, plus an offline
    client;
  - random UUID v4 device identity and validated `device.conf` writer;
  - closed privacy allowlists;
  - Windows application inventory collector;
  - Boswas ID boundary interfaces only.
- **`device.conf` contract:** `DEVICE_PROFILE`, `DEVICE_CERTIFICATE`
  (public certificate reference).
- **`boswas` CLI:**
  - `boswas winapp ...` runs `boswas-winapp`;
  - `boswas device status` shows profile and certificate;
  - new posture check `winapp-confinement`.
- **Tests:**
  - unit tests for WinCompat and the agent;
  - static checks for manifests and profile consistency;
  - package checks for `boswas-compat`;
  - image checks;
  - an end-to-end WinCompat runtime test in the image (real Wine and
    bubblewrap, isolation probes, policy refusals);
  - WinCompat checks in the QEMU boot test under the real kernel's AppArmor.
  - The Windows test application is built from source with MinGW-w64.
- **Documentation:** compatibility guide, device-management guide, ADRs
  0011–0016, milestone roadmap.
- **Security review of WinCompat:** seven findings, all fixed with regression
  tests (docs/compatibility/README.md, "Security review"). The most severe
  was a planted symlink that made the installer clean-up delete files in the
  user's home.

### Changed
- **`test.sh`:** a stage that exits non-zero without recording a failure
  (a crashed test script) is now recorded as a FAIL instead of passing
  silently.
- **Builder image:** gains `dh-apparmor`, `apparmor` and
  `gcc-mingw-w64-x86-64-win32`.
- **Build inputs:** the package build and the config hash include
  `compatibility/`.
- **Image:** `boswas-os` depends on `boswas-compat`. The image verify hook
  checks `boswas-winapp` and compiles the profile.

### Known limitations
- **64-bit Windows programs only.** 32-bit installers are refused with a
  clear message; adding `wine32` or a WoW64 Wine is an open decision
  (ADR-0014).
- **Empty catalog.** The system catalog is empty until applications are
  validated.
- **Permissive alpha policy.** The shipped policy allows unlisted
  applications, with no network.
- **Direct Wine use.** Direct `/usr/bin/wine` use is not mediated.
- **X11.** X11 applications can observe other X11 clients (Xwayland only
  under Plasma Wayland).
- **No device agent service yet:** no backend contact (Milestone 2).

## [1.0~alpha1] - 2026-10-04 (v1 Alpha)

First bootable foundation. Phases 0–2 complete, Phase 3 baseline.

### Added
- **Repository and build system:**
  - Repository structure and architecture documentation (ADRs, roadmap,
    update architecture, privacy, HCL).
  - `build.sh`: native on Debian 13, or containerised on other hosts. It
    produces the ISO, SHA-256 checksum, manifest and JSON build record.
  - `clean.sh`, `test.sh` and a `Makefile`.
- **Image:**
  - Debian 13 "trixie" live-build configuration with an explicit,
    documented package manifest and build-time exclusions.
  - KDE Plasma 6.3 desktop.
  - GRUB boot menu for BIOS and UEFI; Debian-signed Secure Boot chain.
  - Debian Installer in live mode. Its preseed enforces encrypted LVM
    (LUKS2), locks root and defaults the hostname to `boswas-device`.
- **Branding:** built from the official Boswas Group logo and lettering
  (traced vectors): boot menu, installer banner, Plasma global theme, Boswas
  Dark colour scheme, wallpaper, login and lock screens, launcher icon and
  About page.
- **`boswas-security`:**
  - nftables firewall (inbound deny)
  - systemd preset (firewall on; SSH off unless provisioned)
  - kernel hardening sysctls
  - auditd rules
  - journald retention
  - sudo and pwquality policy
  - sshd policy
  - security-only unattended upgrades
  - USB policy framework
- **`boswas-cli`:** `boswas`, `boswas-info` and `boswas-status`, with
  `--json` output, stable exit codes and unit tests.
- **`boswas-os`:** release identity, `/etc/boswas` (device and update
  configuration contracts) and a login banner.
- **Tests:** static checks, lintian, package manifest resolution, ISO and
  image content, security baseline, Wine, and a QEMU boot test (UEFI Secure
  Boot plus serial functional checks).

### Known limitations
- No device agent, Boswas ID, Store or WinCompat tooling yet (Phases 4–7).
- **Windows apps:** 64-bit only (no `wine32`).
- **USB policy:** not enforced.
- **Installed system:** no graphical boot splash.
- **Release artifacts:** not yet signed. Builds are not yet pinned to a
  Debian snapshot.
