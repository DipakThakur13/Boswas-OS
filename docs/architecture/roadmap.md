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
ADR-0032). The plan's exit criteria for that step are **not** met yet.

**What "done" means below.** *Implemented* = the code exists, builds as a
Debian package, is lintian-clean and passes its tests in the development
environment (unit tests; for the compositor also end-to-end tests on the
headless back end and a nested run with OpenGL ES). *Not boot-tested* =
it has not run in the built image, in QEMU or on hardware. No ISO has been
built from this manifest yet, and its dependency resolution on Debian 13
has not been checked. 1.0~alpha3 (Plasma) is the last release that booted.

### Compositor milestones (plan section 5)

| Milestone | Plan result | Status |
|-----------|-------------|--------|
| **C0** | Runs in a window on the developer machine and shows one client | **Implemented** (`--backend winit`; also `headless`) |
| **C1** | Runs on real hardware with one monitor, keyboard and mouse | **Implemented, not boot-tested**: `udev` back end (DRM/KMS, GBM, OpenGL ES, libinput, libseat) |
| **C2** | Complete window protocol support; the Boswas Shell runs on it | **Core implemented**: xdg-shell with popups and decorations, layer shell, selections and drag-and-drop, dmabuf, viewporter, presentation time, pointer constraints and gestures, activation, idle inhibit, security context; the shell runs on it. Not yet: touch screens, tablets, text input and input methods, output management |
| **C3** | Multiple monitors, hotplug, fractional scaling, XWayland | **Partly**: several monitors on the primary GPU and hotplug (not boot-tested), one fractional scale for all outputs. Open: per-output scale, a second GPU, **XWayland** |
| **C4** | Materials, shadows, rounded corners, full animation set, snap layouts | **Partly**: Boswas window frames with 8 px corners, pre-rendered shadows, snap layouts with preview. Open: the Horizon and Glass materials (blur), corner clipping of client content, window animations |
| **C5** | Screen capture, portals, input methods, screen-reader support, session lock | **Session lock implemented** (ADR-0028), `xdg-desktop-portal-gtk` for file dialogs. Open: screen capture and its portal, input methods, screen-reader support |
| **C6** | Whole team uses it daily; offered as a preview session | Open |
| **C7** | NVIDIA proprietary driver, variable refresh rate, performance hardening | Open |

Performance budgets (frame time, memory, start time, dropped frames): **not
measured.** There is no benchmark job yet, and nothing in this repository
states that a budget is met. The compositor draws no frame when nothing
changes; that part of the idle-power budget is by design, not by
measurement.

Security model: ordinary applications get no privileged interface
(implemented and tested, ADR-0026). The plan identifies trusted components
by the security-context protocol and a list of signed binaries; this
implementation identifies them by who started them (the compositor's own
children). Screen capture through a portal with a visible indicator is open.

### Shell and Boswas UI (plan section 6)

| Component | Status |
|-----------|--------|
| Taskbar | **Implemented**: panel at the bottom edge of every output with pinned and running applications, clock, battery and network state. Open: tray, other edges |
| Start and search | **Implemented**: launcher with a pinned grid, search over installed applications, and lock, log out, restart and shut down. Open: recent files, search in files and settings |
| Quick settings | Open (Wi-Fi, Bluetooth, volume, brightness, night light). Until then: `nmtui` or `nmcli`, `pavucontrol-qt` |
| Notification centre and calendar | Open. No notification daemon runs, so applications' notifications are not shown |
| On-screen displays | Open (volume, brightness, keyboard layout) |
| Desktop and wallpaper | **Implemented**: wallpaper (built-in Prism background or a preset's picture). Open: desktop icons, right-click menu |
| Lock screen and login screen | **Implemented** (ADR-0028, ADR-0030), password only. **Login not boot-tested** |
| Password and permission prompts | polkit agent of KDE (`polkit-kde-agent-1`) and the GTK portal; Boswas dialogs are open |
| Boswas UI tokens | **Implemented**: `desktop/ui/tokens.json`, version 2 (ADR-0031). The plan's navy and gold token sets were replaced by the Prism palette, as requested |
| Boswas UI delivery | In Rust (`boswas-ui`), not the Qt Quick style and QML module of the plan (ADR-0027). KDE colour schemes are generated; a Qt Widgets style and a GTK theme are open |
| Icons | **Implemented**: Boswas Prism and Boswas Prism Light, 178 designs (ADR-0031) |
| Boot screen | **Implemented, not boot-tested**: `boswas-bootsplash` (ADR-0029) |

### Planned work on the native desktop

In rough order of what a user misses first:

1. **Boot the image**: build the ISO with the new manifest, fix what the
   verify hook, the QEMU boot test and the first real machines show, and
   record the result in `docs/reports/` and the hardware list.
2. **Settings without Plasma**: network (Wi-Fi), display arrangement and
   scale, sound, power, users, keyboard layout and region in Control
   Center, in place of the Plasma modules that are no longer installed;
   output configuration in the compositor.
3. **Notifications, tray, quick settings and on-screen displays.**
4. **Screen capture**: screenshots and screen sharing through PipeWire and
   a portal, with a visible indicator.
5. **Input**: input methods (Indian and East Asian languages), touch
   screens, tablets, an on-screen keyboard.
6. **Accessibility**: screen-reader support in the shell, a high-contrast
   scheme. `reduced_motion` exists as a setting.
7. **XWayland** as an opt-in for X11-only applications.
8. **Graphics**: a second GPU, per-output scale, variable refresh rate,
   HDR, direct scan-out, the NVIDIA proprietary driver.
9. **Materials and motion**: blur, corner clipping, window animations.
10. **Session integration**: `loginctl lock-session`, fingerprint unlock
    where policy allows, user switching.
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
