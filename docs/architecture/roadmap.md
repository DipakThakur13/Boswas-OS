# Implementation roadmap

## Foundation (v1 alpha, 1.0~alpha1)

| Phase | Scope | Status |
|-------|-------|--------|
| **0 Repository bootstrap** | Repository layout, docs, build tooling, licensing model | **Done** |
| **1 Debian live-build** | trixie configuration, KDE, package manifest, first ISO | **Done** |
| **2 Branding** | Theme, wallpaper, boot menu, installer, login/lock, About, `boswas-info`/`boswas-status` | **Done.** The boot screen of installed systems is now `boswas-bootsplash` (see below) |
| **3 Security baseline** | AppArmor, nftables, audit, LUKS2 install policy, sudo/pwquality, sysctl, journald | **Baseline done**; USB enforcement and systemd unit hardening pending |

## Enterprise platform (ADR-0016)

A milestone is complete only when code, tests, documentation, a security
review and an integration test all exist.

| Milestone | Scope | Status |
|-----------|-------|--------|
| **M1 Windows/Wine platform** | `boswas-compat`: `boswas-winapp` (install, remove, list, launch, status, repair, logs), per-application prefixes, bubblewrap sandbox, `boswas-winapp` AppArmor profile, manifest format v1, layered catalog, WinCompat policy, "Run with Boswas" handler; device agent **interfaces** (models, schemas, identity, privacy guard, Windows inventory collector) | **Implemented in 1.0~alpha2** |
| **M2 Device agent** | `boswas-device-agent`: persistent device identity, `device.conf` configuration and validation, allowlisted inventory, device and application states, `boswas-device-agent.service` (sandboxed), local management API, per-user session agent, mutual-TLS client, enrollment, heartbeat, offline outbox, typed commands, `boswas-device` | **Implemented in 1.0~alpha3** |
| **M3 Control Plane foundation** | `control-plane/` modular monolith (**Python stdlib + SQLite**, ADR-0017): device registry, enrollment with one-time tokens and a device CA, heartbeat, inventory, typed command system with lifecycle, application catalog and installer artifacts, hash-chained audit trail | **Implemented in 1.0~alpha3** (no Docker Compose: a Debian package and `boswas-cp`) |
| **M4 Policy engine** | Signed, versioned policies (Ed25519), device-side verification and anti-rollback, managed WinCompat policy layer, policy-limited commands | **Core implemented in 1.0~alpha3**. One policy per device; the global → group → profile → override hierarchy is open |
| **M5 Fleet management** | Admin dashboard, remote application management | **Dashboard implemented in 1.0~alpha3**. Organisation/department/group model open |
| **Compatibility Manager** | `boswas-compat-manager` (PySide6): dashboard, library, install (64-bit and 32-bit since ADR-0022), details, permissions (display-only), logs, repair, update, remove, system status | **Implemented in 1.0~alpha3** |
| **Cross-platform applications** | 32-bit Windows (ADR-0022), installer firmware and offline installation (ADR-0023), `boswas-sysops` (ADR-0024), Android via Waydroid opt-in (ADR-0025), functional Control Center | **Implemented after 1.0~alpha3** (see `docs/reports/2026-11-12-release-readiness.md` for what is verified) |
| M6 Boswas Store | `boswas-store` client and catalog API; Windows apps through WinCompat manifests | Planned |
| M7 Update infrastructure | Debian snapshot pinning, dev → qa → stable promotion, staged updates, rollback | Planned |
| M8 Hardware certification | `boswas hardware test`, `boswas-hardware-profile`, certification levels | Planned |
| M9 Production signing/release | Key custody, signed artifacts, SBOM, release channels | Planned |

Boswas ID is intentionally deferred (ADR-0015); only interfaces exist.

## Native desktop (Desktop Platform project plan, Draft 1)

The project plan replaces KDE Plasma in stages: a Boswas shell on KWin first
(its Phase 1), the compositor as a preview session after eight weeks of
daily use (Phase 2), and the compositor as the default only with the public
Beta (Phase 4). This tree takes the step at once, as requested: the image
manifest installs `boswas-desktop` and `boswas-bootsplash` and pins out the
Plasma shell, KWin, SDDM, System Settings and Plymouth (ADR-0026 to
ADR-0032). A second round turned that first version into a desktop in
the Fluent design language with the functions a user expects of one,
its own system applications and native settings (ADR-0033 to ADR-0041);
later rounds laid Settings out like Windows 11's and gave the desktop the
functions behind it (ADR-0049). The plan's exit criteria are **not** met
yet.

**What "done" means below.** *Implemented* = the code exists and passes
its tests in the development environment: unit tests, for the compositor
also end-to-end tests on the headless back end, for the service backends
integration tests against stand-ins of the system daemons. *Not
boot-tested* = it has not run in the built image, in QEMU or on hardware.
No ISO has been built from this manifest yet, its dependency resolution
on Debian 13 has not been checked, and nothing has run against a real
NetworkManager, BlueZ, PipeWire, UPower or polkit. 1.0~alpha3 (Plasma) is
the last release that booted. [verification.md](verification.md) has the
full account.

### Compositor milestones (plan section 5)

| Milestone | Plan result | Status |
|-----------|-------------|--------|
| **C0** | Runs in a window on the developer machine and shows one client | **Implemented** (`--backend winit`; also `headless`) |
| **C1** | Runs on real hardware with one monitor, keyboard and mouse | **Implemented, not boot-tested**: `udev` back end (DRM/KMS, GBM, OpenGL ES, libinput, libseat) |
| **C2** | Complete window protocol support; the Boswas Shell runs on it | **Implemented**: xdg-shell with popups and decorations, layer shell, selections and drag-and-drop, dmabuf, viewporter, presentation time, pointer constraints and gestures, activation, idle inhibit and idle notification, security context, touch, tablets, text input, xdg-foreign, alpha modifier, content type, the system bell and the background effect for live blur (ADR-0063); input method and virtual keyboard for the configured input method only (ADR-0036); the shell runs on it. Not offered: an output-management protocol (monitors are set in the configuration file) |
| **C3** | Multiple monitors, hotplug, fractional scaling, XWayland | **Implemented, not boot-tested**: several monitors on the primary GPU with hotplug, and mode, scale, position, rotation and on or off per monitor (ADR-0036), each starting at the scale recommended for its size and resolution (ADR-0055); X11 applications through Xwayland (ADR-0034), drawn at scale 1. Open: a second GPU |
| **C4** | Materials, shadows, rounded corners, full animation set, snap layouts | **Implemented, with one decision against the plan** (ADR-0036): Fluent window frames with 8 px corners and shadows, client content clipped to the rounded corners on OpenGL ES, animations for opening, closing, minimising, restoring and switching desktops, snap layouts by dragging, by keys and from the maximise button. The shell and the compositor's own surfaces are Boswas glass, blurring the windows behind them live on OpenGL ES and drawn from the wallpaper with pixman (ADR-0063); not yet seen on a real GPU |
| **C5** | Screen capture, portals, input methods, screen-reader support, session lock | **Partly**: session lock (ADR-0028); screen capture for the user (the shell's capture screen) and for other programs behind the user's consent (ADR-0037); `xdg-desktop-portal-gtk` for file dialogs and `xdg-desktop-portal-wlr` for screen sharing, the latter **not tested**; input-method support in the compositor, with no input method in the image. Open: screen-reader support |
| **C6** | Whole team uses it daily; offered as a preview session | Open |
| **C7** | NVIDIA proprietary driver, performance hardening (variable refresh rate and high refresh rates: ADR-0048) | Open |

Performance budgets (frame time, memory, start time, dropped frames): **not
measured.** There is no benchmark job yet, and nothing in this repository
states that a budget is met. *(Since ADR-0056 the frame costs of the
shell and the compositor were measured on the build machine without a GPU,
and a release-only test bounds the cost of drawing Start; since ADR-0064
another bounds a slice of the drawing ahead of time.)* The compositor
draws no frame when nothing changes; that part of the idle-power budget is
by design, not by measurement. The boot screen is the exception: its frame
costs are measured on the build machine by a test
(`packages/boswas-bootsplash`).

Security model: ordinary applications get no privileged interface
(implemented and tested, ADR-0026). The plan identifies trusted components
by the security-context protocol and a list of signed binaries; this
implementation identifies them by who started them (the compositor's own
children). Screen capture is not a privileged interface: every program
outside a sandbox may ask, the user is asked each time, and a mark on the
taskbar shows while a program may capture (ADR-0037, tested with a
screenshot tool; the portal path is not tested).

### Shell and Boswas UI (plan section 6)

| Component | Status |
|-----------|--------|
| Taskbar | **Implemented**: at the bottom edge of every output, with Start, Task View and the pinned and running applications as one centred group (indicators, context menus, window previews), the tray with its overflow and menus, the keyboard layout, network, sound and battery state, and the clock. Its settings (ADR-0049): the group at the left, automatic hiding, the Task View button, seconds in the clock, notification badges; the widgets button with the weather (ADR-0059). Open: other edges |
| Start and search | **Implemented**: search over applications, settings pages and commands; one scrolling page as in the 2025 Windows 11 Start, with the pinned applications, recommendations (recently added and most used) and all applications by category, as a grid or as a list with a letter index, sized to the screen (ADR-0053); the user menu and the power menu, folders next to it; three layouts (ADR-0049). Open: recent files, search in files |
| Quick settings | **Implemented** (ADR-0035): Wi-Fi with the list of networks, Bluetooth with the list of devices, airplane mode, night light, power mode, do not disturb, media controls, brightness and volume with the choice of output. **Not run against the real services.** Open: enterprise Wi-Fi, Bluetooth devices that need a PIN |
| Notification centre and calendar | **Implemented** (ADR-0035): the shell is the notification service; toasts with actions and progress, a centre grouped by application, a month calendar, do not disturb, with a daily schedule, banners off per application and the time a toast stays (ADR-0049). Notifications are kept in memory only. Open: notifications on the lock screen (left out by design) |
| On-screen displays | **Implemented** for the volume and brightness keys. Open: keyboard layout (the taskbar shows it) |
| Desktop and wallpaper | **Implemented**: wallpaper (built-in background or a preset's picture) and desktop icons on the primary output, with their menus, the Trash, the clipboard and drops from other programs (ADR-0060). Open: icons on other outputs, dragging icons into other programs |
| Widgets, clipboard history, emoji | **Implemented** (ADR-0058, ADR-0059): the widgets board (Super+W) from the applications' own files, without online feeds; the clipboard history (Super+V) with pinned items; the emoji and symbols panel (Super+.). Run on the headless compositor only |
| Lock screen and login screen | **Implemented** (ADR-0028, ADR-0030), password only; the login screen lists the people who can sign in, and the lock screen shows the weather of the home place chosen in Weather (ADR-0051). A boot always ends at a way to sign in (ADR-0044). Login booted in a virtual machine from the packages; **not on the image or on hardware** |
| Password and permission prompts | **Implemented** (ADR-0035, ADR-0037): the shell is the polkit agent and draws the password dialog, with a choice of administrator; it also asks before a program may capture the screen. File dialogs of sandboxed applications come from the GTK portal. **The polkit dialog has not run against the real polkit** |
| Boswas UI tokens | **Implemented**: `desktop/ui/tokens.json`, version 3 (ADR-0033): the Fluent design language with the Prism colours. The plan's navy and gold token sets were replaced by the Prism palette, as requested |
| Boswas UI delivery | In Rust (`boswas-ui`) for the shell and the window frames, not the Qt Quick style and QML module of the plan (ADR-0027), and as the Qt Widgets toolkit `boswas_fluent` for the Boswas applications (ADR-0039), with Boswas glass on a blurred copy of the desktop background (ADR-0057). KDE colour schemes are generated. The shell and the compositor's own surfaces draw the same glass, with live blur on OpenGL ES (ADR-0063). GTK programs and Firefox get dark mode and the accent through GSettings (ADR-0062). Open: a GTK theme, live blur in the Qt applications |
| Icons | **Implemented**: Boswas Prism and Boswas Prism Light, 300 designs in 535 files (ADR-0031), including every icon name the system applications, Settings and the Compatibility Manager ask for |
| Pointer | **Implemented**: Boswas-Cursors, Boswas's own XCursor theme, the default of the compositor, the login screen and every program (ADR-0043). Not yet seen on hardware |
| Boot screen | **Implemented**: `boswas-bootsplash` (ADR-0029), in the Boswas UI look since ADR-0038, drawn from vectors with motion since ADR-0050. Booted in a virtual machine from the packages (ADR-0044), with the picture before ADR-0050; **not on the image, an encrypted disk or hardware** |
| System applications | **Implemented, not run on the desktop** (`boswas-apps`, ADR-0040): Files, Terminal, Text Editor, Calculator, Photos, Document Viewer, Archive Manager, Media Player, Camera, Clock, Calendar, Task Manager and a reminders service replace KDE's applications in the image. Open: what each leaves out (ADR-0040), an office suite, a mail client, a print service |
| Settings | **Implemented against stand-ins** (ADR-0041, ADR-0049): Control Center laid out like Windows 11's Settings, in eleven categories with search and the deep links of earlier versions; native sections for network (VPN and adapters included), Bluetooth, printers, cameras, sound, power, display, night light, mouse, touchpad, keyboard, notifications, multitasking, personalization (accent colour, taskbar, Start, fonts), storage cleanup, troubleshooters, advanced start-up, installed apps, optional features, app permissions, update history, lock and screen timeouts, date and time, region, default and start-up applications, user accounts, accessibility; the clipboard history, desktop icons, wired IP and DNS settings, ejecting USB drives (ADR-0058 to ADR-0061). Open: proxy editing, VPNs that ask for a password (no secret agent), drawing tablets, an automatic suspend time, pausing updates |

### Done since the first version of the native desktop

These were items of the planned work of the first version. Each is
implemented and tested as described in its ADR, and none has run on a
booted system.

| Item | ADR |
|------|-----|
| Notifications, tray, quick settings, on-screen displays for volume and brightness | ADR-0035 |
| The polkit password dialog in the shell, in place of KDE's agent | ADR-0035 |
| `loginctl lock-session` | ADR-0035 |
| Screen capture: the shell's capture screen, and capture by other programs behind the user's consent with a visible indicator | ADR-0037 |
| X11 applications through Xwayland, on by default | ADR-0034 |
| Per-monitor mode, scale, position and rotation in the compositor's configuration | ADR-0036 |
| Corner clipping of client content, window animations | ADR-0036 |
| Touch screens, tablets, touchpad gestures; input-method support in the compositor | ADR-0036 |
| Night light, turning the screens off after a time without input | ADR-0036 |
| A Qt Widgets look for the Boswas applications | ADR-0039 |
| Boswas's own system applications in place of KDE's | ADR-0040 |
| Settings without Plasma: native sections in Control Center, display arrangement included | ADR-0041 |
| Control Center laid out like Windows 11's Settings; settings for the taskbar, Start, multitasking and notifications; the accent colour; text size, magnifier, colour filters, sticky and filter keys | ADR-0049 |
| A shell that draws only what changed, with the compositor running its transitions | ADR-0056 |
| Glass in the Boswas applications, icons that grow under the pointer | ADR-0057 |
| Clipboard history, emoji and symbols panel, widgets board, desktop icons | ADR-0058 to ADR-0060 |
| Controls that only pretended: new windows, Photos' background, the pointer size for X11 programs, activity history, Ethernet and USB settings, printing support | ADR-0061 |
| The shell's controls that only pretended (new windows, night light, the battery, the lock screen's keys, account pictures, dark mode for GTK programs, Num Lock) and the Windows shortcuts and right-click menus, Ctrl+Alt+Del and Super+P | ADR-0062 |
| One glass for the shell, the compositor and the applications; live blur on OpenGL ES; icons that grow under the pointer in the shell | ADR-0063 |
| No full-screen redraws for what appears in front; the snipping overlay, the lock screen's start, Task View and the preparation of the flyouts made cheap; memory given back | ADR-0064 |

### Planned work on the native desktop

In rough order of what a user misses first:

1. **Boot the image**: build the ISO with the new manifest, fix what the
   verify hook, the QEMU boot test and the first real machines show, and
   record the result in `docs/reports/` and the hardware list. Then run
   the service backends against the real daemons, first of all the
   polkit agent: no administrator action from the desktop works without
   it.
2. **Settings**: run every section against the real services, the Users
   page first (AccountsService, polkit, `passwd`), then the pages of
   ADR-0049 (NetworkManager, CUPS, logind, systemd-hostnamed, the Flatpak
   permission store); then what ADR-0041 and ADR-0049 leave out, among
   them a secret agent for NetworkManager.
3. **System applications**: run every application on the desktop and on
   real devices (a camera, removable drives, sound);
   a print service in the image, so that Print reaches a printer; then
   what ADR-0040 leaves out, by demand.
4. **Screen sharing through the portal**: test `xdg-desktop-portal-wlr`
   with PipeWire and a browser; a chooser for one monitor or one window.
5. **Windows applications and X11**: decide whether the WinCompat
   sandbox keeps offering the X11 socket now that the session has an X
   server (ADR-0034), and test Wine on the desktop either way.
6. **Input**: an input method in the image (Indian and East Asian
   languages), an on-screen keyboard; pairing Bluetooth devices that need
   a PIN; enterprise Wi-Fi.
7. **Accessibility**: screen-reader support in the shell and for what
   the compositor draws, contrast themes for the shell. Text size, the
   magnifier, colour filters, sticky and filter keys, always visible
   scroll bars and `reduced_motion` exist (ADR-0049); they have not run on
   real hardware.
8. **Graphics**: a second GPU, HDR, the NVIDIA
   proprietary driver; sharp X11 windows on scaled monitors. Direct
   scan-out is left to Smithay's DRM compositor and has not been observed
   on hardware.
9. **Materials**: live blur in the Qt applications through the protocol
   the compositor offers since ADR-0063, and live blur measured on a real
   GPU.
10. **Session integration**: fingerprint unlock where policy allows, user
    switching, notifications that survive a restart of the shell.
11. **Build**: vendored crates for offline builds, an advisory check and an
    SBOM for the Rust dependencies (ADR-0032); the performance benchmark
    job of the plan.

## Decided

- **64-bit and 32-bit Windows applications (ADR-0022, supersedes ADR-0014).**
  `wine32:i386` multiarch; the system stays amd64.
- **Android applications through Waydroid, opt-in; no Google Play (ADR-0025).**
  Google Play needs a licence Boswas does not have. F-Droid and local APK
  files are the supported alternatives.
- **Boswas ID stays deferred (ADR-0015).** Only interfaces exist.

## Open items

- **Physical hardware validation.** No physical PC has been certified; the
  validation matrix in `docs/deployment/hardware-compatibility.md` lists what
  was tested only in a VM. The native desktop makes this more urgent:
  graphics, input and suspend now depend on Boswas code that has not run on
  hardware.
- **Android on real hardware.** Waydroid needs a GPU with open drivers for
  acceleration; ARM-only apps cannot run (no redistributable translation).
  The Waydroid repository is outside Debian security support.
- **First validated applications.** The system catalog is empty until the
  first 10–20 business applications are validated and their manifests pinned.
- **Production WinCompat policy.** The Control Plane's `default` policy
  already uses `UNLISTED_APPS=deny` and `approved tested`. The shipped local
  policy stays permissive for unmanaged alpha devices.
- **Direct `/usr/bin/wine` use.** Not mediated.
- **Runtime components.** Boswas-packaged dependencies (VC++ runtimes and
  similar) need licensing review before manifests may request them.
- **Control Plane:**
  - policy hierarchy (group, profile, override) and an organisation model;
  - high availability, and PostgreSQL behind `store.Store`;
  - certificate renewal before the 365-day device certificates expire (today:
    re-enrollment);
  - an external anchor for the audit hash chain.
- **Device agent:**
  - TPM-backed credential store;
  - `UPDATE_AGENT` needs a Boswas APT repository (M7) to be useful;
  - application commands go to the active user only (no per-user targeting).

## Open items carried from Phases 2–3

- Installed-system boot branding:
  - the boot screen and the graphical LUKS passphrase prompt are
    `boswas-bootsplash` (ADR-0029); they still have to be verified on a
    real boot
  - a GRUB background on `/boot`
- USB storage enforcement: udev authorisation plus udisks2 polkit rules,
  driven by `/etc/boswas/usb-policy/policy.conf`.
- Boswas AppArmor profiles for other high-risk applications (browsers). The
  Wine profile exists since M1.
- Pin the build to a `snapshot.debian.org` timestamp and a builder image
  digest, for bit-for-bit reproducible releases (M7).
- Sign release artifacts once the release signing key custody is defined (M9).
- Decide whether the live session keeps live-config's passwordless sudo
  (convenient for diagnostics, but too permissive for a production image).
- Original Boswas Group font file, for UI-wide typography.

## Decisions still to freeze

- **Supported hardware:** the first Hardware Compatibility List; whether BYOD
  is prohibited.
- **Control Plane operations:** hosting, HA, backups of
  `/var/lib/boswas-control-plane`, and custody of the device CA and policy
  signing keys. They are files on the server in 1.0~alpha3.
- **Windows applications:** the first validated 64-bit applications.
- **Data policy:** backup, retention, remote-wipe boundaries and employee
  privacy.
- **Emergency recovery:** a procedure that does not weaken normal controls.
