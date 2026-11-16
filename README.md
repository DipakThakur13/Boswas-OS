# Boswas OS

**Boswas OS v1** is the Boswas Group company-managed desktop operating
system, built on **Debian 13 "trixie"** with the **native Boswas desktop**:
its own Wayland compositor, shell, lock and login screens and boot screen,
written in Rust, its own system applications and settings, all in the
Fluent design language with Boswas's own colours, icons and typeface. It
is an internal platform for Boswas Group employees and company-owned
hardware, not a public Linux distribution.

> **Status: v1 alpha (1.0~alpha3). Not production-ready.**
> This release contains:
> - **the native Boswas desktop** (unreleased, see
>   [Status of the native desktop](#status-of-the-native-desktop)):
>   `boswas-compositor` (Rust, on Smithay) in place of KWin, `boswas-shell`
>   (taskbar, Start, quick settings, notifications, lock screen, login
>   screen) in place of Plasma and SDDM, and `boswas-bootsplash` in place
>   of Plymouth. Wayland applications run on it as ordinary clients, and
>   X11 applications run through Xwayland
> - **the Boswas system applications** (`boswas-apps`, unreleased): Files,
>   Terminal, Text Editor, Calculator, Photos, Document Viewer, Archive
>   Manager, Media Player, Camera, Clock, Calendar and Task Manager, in
>   place of KDE's applications
> - **native settings** in Boswas Control Center, in place of Plasma's
>   settings modules: network, Bluetooth, sound, power, display, input,
>   personalization, date and time, user accounts
> - the Boswas OS experience: Boswas OS identity, Boswas UI (the Fluent
>   design language with the Prism colours, in one token file), the Boswas
>   Prism icon pack, the Boswas-Cursors pointer theme, ten presets, Boswas
>   Control Center (with the Security, Software and Update Centers) and a
>   branded terminal, on a Debian 13 base
> - a Live USB that boots straight to a usable desktop; installation only
>   by explicit choice
> - security baseline and encrypted installer
> - `boswas` status tools
> - **WinCompat:** Windows applications in isolated, AppArmor-confined Wine
>   sandboxes (`boswas-winapp`); **64-bit and 32-bit Windows applications**
>   (32-bit through Debian's `wine32:i386`, ADR-0022)
> - **Android applications (opt-in):** the Android runtime manager
>   (`boswas-android`, Waydroid installed on request after installation),
>   APK inspection; Google Play is not offered (ADR-0025)
> - **Applications and system management:** Control Center installs, updates
>   and removes software and reports hardware, firmware, storage and network
>   state through one narrowly scoped privileged helper (`boswas-sysops`)
> - **Installer firmware:** every Debian firmware package on the medium, so
>   the installer loads e.g. Realtek RTL8125B firmware without a network;
>   installation needs no internet connection (ADR-0023)
> - **Compatibility Manager:** the graphical application manager
> - **Device agent:** persistent identity, state, inventory, a local
>   management API; standalone unless a Control Plane is configured
> - **Boswas Control Plane** (a separate server package): device registry,
>   typed remote management (no remote shell), signed policies, the
>   application catalog, the audit trail and a web dashboard
> - reproducible, script-driven builds
>
> The Store, update infrastructure, hardware certification and signed
> releases follow in later milestones; Boswas ID is deferred (see
> `docs/architecture/roadmap.md`).

Debian remains upstream. This repository contains Boswas-specific
packaging, configuration, artwork, scripts, tests and documentation, and
the source of the Boswas components (the desktop and the boot screen in
Rust, the tools in Python). Every other component is an unmodified Debian
package. The Wayland protocol itself stays: it is what every application
speaks, and the native compositor implements it.

## Status of the native desktop

The native desktop replaces KDE Plasma, KWin, SDDM and Plymouth in the image
manifest (they are pinned out; ADRs in
[docs/architecture/decisions.md](docs/architecture/decisions.md)). It is new
and has **not yet booted**, neither in a virtual machine nor on real
hardware, and it has not run against the real system services.

What it does today:

- **Windows:** Fluent window frames with rounded corners and shadows,
  animations, snap layouts, a window picker (Alt+Tab), Task View with
  several desktops, a window menu, and the Windows shortcuts.
- **Shell:** a centred taskbar with Start and search, the tray and the
  clock; quick settings for Wi-Fi, Bluetooth, sound, brightness, night
  light and the power mode; notifications with a calendar; Run; screen
  capture; the password dialog for administrator actions.
- **Displays and input:** settings per monitor (mode, scale, position,
  rotation), night light, touch screens, touchpad gestures, drawing
  tablets, support for an input method.
- **System applications** (`boswas-apps`): Files, Terminal, Text Editor,
  Calculator, Photos, Document Viewer, Archive Manager, Media Player,
  Camera, Clock and Calendar with reminders, Task Manager. They run as
  the user, start nothing through a shell and treat files as hostile
  input.
- **Settings** in Control Center: Wi-Fi, Bluetooth, sound, power,
  monitors (arrangement, resolution, scale, rotation), night light,
  mouse, touchpad, keyboard layouts, colour mode and background, lock and
  screen timeouts, date and time, region, default and start-up
  applications, user accounts.
- **Other applications:** any Wayland application, and X11 applications
  through Xwayland. A program gets pictures of the screen only after the
  user has allowed it.

| Verified in the development sandbox | Not yet verified |
|-------------------------------------|------------------|
| Rust unit tests of the compositor, shell, service backends, UI library and boot screen | Building the ISO with the new manifest |
| Compositor end-to-end tests on its headless backend with real clients: protocol trust, the control socket, window management, screen capture behind consent, an X11 program, the session lock | The QEMU boot test (`tests/boot/qemu_boot_test.py`, rewritten for the new desktop) |
| The service backends against stand-ins of NetworkManager, BlueZ, UPower, logind, polkit and the sound server | The same backends against the real daemons; first of all the polkit agent, which every administrator action from the desktop needs |
| The Qt toolkit, the system applications and the settings sections on Qt's offscreen platform, with stand-ins for cameras, drives and services | The hardware path: DRM/KMS, libinput, libseat (compiled, not run) |
| Three opt-in settings tests against the real compositor on its headless backend | The greetd login on a real boot; the boot screen on real hardware, including the disk-unlock prompt |
| | The system applications and Control Center on the running desktop, and with real devices (a camera, removable drives, sound) |
| | Screen sharing through the portal; Wine and Waydroid on the new desktop |
| | Whether Debian trixie resolves the new image manifest's dependencies and has every package under the name used |

The full account, with test counts, is in
[docs/architecture/verification.md](docs/architecture/verification.md).

Known gaps: no live blur behind translucent surfaces, X11 windows are not
sharp on scaled monitors and X11 applications are not isolated from each
other, Bluetooth devices that need a PIN and enterprise Wi-Fi cannot be
set up from the desktop, no input method is in the image, one GPU only, no
HDR or variable refresh rate, no screen reader for the shell. The full
list is in [docs/experience/README.md](docs/experience/README.md#known-gaps).

What the system applications and the settings leave out is listed in
ADR-0040 and ADR-0041: among other things there is no office suite and
no print service (printing means a PDF file), and the Document Viewer
reads PDF only.

## Build

### Requirements

- **Either** a Debian 13 (trixie) host with root,
- **or** any host with Docker or Podman, including Windows with Docker
  Desktop (WSL2 backend), macOS and other Linux distributions. The build runs
  in a `debian:trixie` builder container automatically.
- About 25 GB of free disk space, a network connection to deb.debian.org
  and, for the Rust packages (`boswas-desktop`, `boswas-bootsplash`), to
  crates.io, and a correct system clock.

### Commands

```sh
git clone <boswas-os repository> && cd boswas-os
./build.sh
```

On Windows, run this from Git Bash or WSL with Docker Desktop running.

A first build takes roughly 30–60 minutes, depending on the network; later
builds reuse the package cache. Results:

```
build/output/
  Boswas-OS-v1-alpha-amd64.iso            bootable live + installer ISO (BIOS and UEFI, Secure Boot)
  Boswas-OS-v1-alpha-amd64.sha256         checksum (sha256sum -c)
  Boswas-OS-v1-alpha-amd64.manifest.txt   build facts + full package list
  boswas-control-plane_<version>_all.deb  the Control Plane server package (not part of the ISO)
build/manifest/<build-id>.json            machine-readable build record
build/logs/                               build and test logs
```

### Test

```sh
./test.sh              # static, package, image and QEMU boot tests
./test.sh --no-boot    # skip the (slow without KVM) boot test
```

### Make targets

`make help` lists them: `build`, `iso`, `packages`, `test`, `test-quick`,
`test-boot`, `clean` and `distclean`.

Details: [docs/development/building.md](docs/development/building.md).

## Try or install

Write the ISO to a USB stick, boot it in UEFI mode with Secure Boot enabled,
and choose one of:

- **Live session:** user `boswas`, password `live`, nothing is saved.
- **Install Boswas OS:** guided installation with LVM inside LUKS2. You set
  the administrator account and the encryption passphrase; root stays locked.

Details: [docs/deployment/installation.md](docs/deployment/installation.md).

On the device:

```sh
boswas-info                 # identity, build, compliance summary
boswas-status               # posture checks (firewall, AppArmor, encryption, ...)
boswas --json device status # machine-readable output
boswas-winapp install ~/Downloads/setup.exe   # Windows application, own sandbox
boswas-winapp list
boswas-compat-manager       # the same, graphically ("Compatibility Manager")
boswas-device status        # device agent: state, identity, Control Plane connection
```

## What is in v1 alpha

| Area | Implementation |
|------|----------------|
| Base | Debian 13.x trixie, amd64, Linux 6.12, systemd |
| Desktop | The native Boswas desktop (`boswas-desktop`): `boswas-compositor`, a Wayland compositor in Rust on Smithay (Fluent window frames, animations, snap layouts, Task View with several desktops, per-monitor settings, night light, Xwayland for X11 applications, compositor-owned session lock, privileged protocols only for its own shell, screen capture only with the user's consent), and `boswas-shell` (taskbar, Start, quick settings, notifications, tray, the polkit dialog, lock screen, login screen behind greetd). Ten presets ([docs/experience](docs/experience/README.md)) |
| Applications | The Boswas system applications (`boswas-apps`, PySide6 on the Boswas Qt toolkit): Files, Terminal, Text Editor, Calculator, Photos, Document Viewer, Archive Manager, Media Player, Camera, Clock, Calendar, Task Manager; Firefox ESR as the browser. Boswas Control Center with native settings and the Security, Software and Update Centers ([docs/experience/control-center.md](docs/experience/control-center.md)) |
| Design system | Boswas UI v3: the Fluent design language with Boswas's own colours (the Prism palette), icons and typeface, as design tokens in one file ([desktop/ui](desktop/ui/README.md)). The shell, the window frames, the boot screen, the Qt toolkit of the Boswas applications ([desktop/ui/qt](desktop/ui/qt/README.md)), the KDE colour schemes, the Boswas Prism icon pack ([desktop/icons](desktop/icons/README.md)) and the Boswas-Cursors pointer theme ([desktop/cursors](desktop/cursors/README.md)) all read it |
| Branding | Built from the official Boswas Group logo and lettering ([desktop/branding](desktop/branding/README.md)) |
| Boot | GRUB for BIOS and UEFI; Debian-signed shim/GRUB/kernel for Secure Boot; Boswas boot menu; the Boswas boot screen (`boswas-bootsplash`, Rust: boot, shutdown, restart and the disk-unlock prompt, [packages/boswas-bootsplash](packages/boswas-bootsplash/README.md)) |
| Installer | Debian Installer (live mode), Boswas banner, policy: full-disk encryption, root locked, host `boswas-device`; firmware pool on the medium, no network needed, signed APT sources and an installation report written at the end |
| Security | nftables (inbound deny), AppArmor, auditd rules, sudo/pwquality policy, kernel hardening, no SSH server, security-only automatic updates, enforced screen lock |
| Tools | `boswas`, `boswas-info`, `boswas-status` (read-only, `--json`) |
| Compatibility | Wine 10, **64-bit and 32-bit Windows applications** (`wine32:i386`). `boswas-winapp`: one prefix per application, bubblewrap sandbox, `boswas-winapp` AppArmor profile, PE header detection, compatibility manifests and catalog, policy, "Run with Boswas". Compatibility Manager (PySide6). Linux applications: APT (signed Debian repositories), Flatpak (approved remotes only), AppImage. Android: `boswas-android` + Waydroid (opt-in) |
| Device management | `boswas-device-agent` (sandboxed service, per-user session agent, `boswas-device`), mutual TLS to the Boswas Control Plane (`control-plane/`, server package): enrollment, heartbeat, inventory, typed commands, signed policies, catalog, audit, dashboard |

## Documentation

| Topic | Document |
|-------|----------|
| Architecture overview | [docs/architecture/overview.md](docs/architecture/overview.md) |
| Decisions (ADRs) | [docs/architecture/decisions.md](docs/architecture/decisions.md) |
| Package manifest and rationale | [docs/architecture/package-manifest.md](docs/architecture/package-manifest.md) |
| Update channels and repositories | [docs/architecture/update-architecture.md](docs/architecture/update-architecture.md) |
| Roadmap | [docs/architecture/roadmap.md](docs/architecture/roadmap.md) |
| What has and has not been verified | [docs/architecture/verification.md](docs/architecture/verification.md) |
| Building | [docs/development/building.md](docs/development/building.md) |
| Repository layout | [docs/development/repository-layout.md](docs/development/repository-layout.md) |
| Security baseline | [docs/security/baseline.md](docs/security/baseline.md) |
| Privacy | [docs/security/privacy.md](docs/security/privacy.md) |
| Installation | [docs/deployment/installation.md](docs/deployment/installation.md) |
| Hardware Compatibility List | [docs/deployment/hardware-compatibility.md](docs/deployment/hardware-compatibility.md) |
| CLI reference | [docs/administration/cli.md](docs/administration/cli.md) |
| Windows compatibility | [docs/compatibility/README.md](docs/compatibility/README.md) |
| Android applications | [docs/android/README.md](docs/android/README.md) |
| Privileged helper (boswas-sysops) | [docs/administration/sysops.md](docs/administration/sysops.md) |
| Platform contracts (sysops, Android, Control Center, the desktop's configuration, control socket and service bridge) | [docs/architecture/platform-contracts.md](docs/architecture/platform-contracts.md) |
| The Boswas OS experience | [docs/experience/README.md](docs/experience/README.md) |
| Boswas UI tokens and the Prism palette | [desktop/ui/README.md](desktop/ui/README.md) |
| The Qt toolkit of the Boswas applications | [desktop/ui/qt/README.md](desktop/ui/qt/README.md) |
| Boot screen | [packages/boswas-bootsplash/README.md](packages/boswas-bootsplash/README.md) |
| Debian references audit | [docs/experience/debian-references.md](docs/experience/debian-references.md) |
| Live USB checklist | [docs/experience/live-usb-checklist.md](docs/experience/live-usb-checklist.md) |
| Device agent | [docs/device-management/README.md](docs/device-management/README.md) |
| Control Plane | [docs/device-management/control-plane.md](docs/device-management/control-plane.md) |

## Licensing and attribution

Boswas OS is based on Debian. Debian components keep their own licences
(`/usr/share/doc/*/copyright` on every device). The system presents itself
as Boswas OS (`ID=boswas`), while `ID_LIKE=debian`, `/etc/debian_version`
and the package archive keep Debian tooling working (ADR-0005). Boswas-authored files: see
[LICENSES/](LICENSES/README.md). Security reports: [SECURITY.md](SECURITY.md).
Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).
