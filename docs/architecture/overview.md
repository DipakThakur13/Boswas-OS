# Boswas OS v1: architecture overview

Boswas OS is the Boswas Group company-managed desktop operating system. It is
**not** a general-purpose distribution. It is a controlled workstation
platform in which Boswas owns:

- device policy
- identity
- the application lifecycle
- the security baseline
- the update process

The full target architecture is described in the *Boswas OS v1 Architecture &
Engineering Blueprint*. This document maps that blueprint onto this
repository and shows what exists today.

## Layers

```
 Boswas Control Plane (server)   devices · typed commands · signed policies · catalog · audit
            │  HTTPS + mutual TLS (device certificate)            (control-plane/, ADR-0017)
            ▼
 ┌──────────────────────────────── Boswas OS workstation ────────────────────────────────┐
 │ Boswas layer  boswas-os · boswas-cli · boswas-branding · boswas-security              │
 │               boswas-compat (WinCompat) · boswas-compat-manager · boswas-android      │
 │               boswas-device-agent · boswas-sysops · boswas-control-center             │
 │               later: store, updater, hardware                                         │
 │ Desktop       boswas-desktop: boswas-compositor (Wayland; Rust on Smithay)            │
 │               · Boswas Shell (taskbar, Start, quick settings, notifications,          │
 │                 lock screen, greeter) · session services · login through greetd       │
 │               · Boswas UI tokens (Fluent; Prism colours) · boswas_fluent (Qt toolkit) │
 │ Applications  boswas-apps (Files, Terminal, Text Editor, Photos, Media Player, ...)   │
 │               · Firefox ESR · other Wayland clients · X11 through Xwayland            │
 │               · APT · Flatpak (no remote) · Wine 10 in per-app sandboxes              │
 │               · Waydroid (opt-in)                                                     │
 │ Security      AppArmor · nftables · auditd · polkit · sudo · pwquality · journald     │
 │ Foundation    Debian 13 "trixie" · Linux 6.12 · systemd · PAM · NetworkManager        │
 │ Boot & disk   UEFI (BIOS fallback) · shim → GRUB → kernel (Debian-signed)             │
 │               · boswas-bootsplash (boot screen, disk passphrase) · LUKS2              │
 └───────────────────────────────────────────────────────────────────────────────────────┘
```

## The desktop stack

The desktop is Boswas software from the boot screen to the system
applications (ADR-0026 to ADR-0041). KDE Plasma, KWin, SDDM and Plymouth
are not installed, and since ADR-0040 KDE's applications are not either.
The Wayland protocol stays, because it is what applications speak: any
Wayland application (Firefox, a KDE or GTK application someone installs)
runs as an ordinary client, and applications that only speak X11 run
through Xwayland (ADR-0034). The look is Boswas UI: the Fluent design
language with Boswas's own colours, icons and typeface (ADR-0033).

```
 kernel + initramfs ─▶ boswas-bootsplashd      boot screen, disk passphrase   (root, confined)
        │                    │ hands the screen over
        ▼                    ▼
 greetd (login daemon) ─▶ boswas-greeter       boswas-compositor + boswas-shell --greeter
        │  PAM                                 as the unprivileged user _greetd
        ▼
 boswas-session (the user) ─▶ boswas-compositor      display server, window manager, lock
                                 ├─▶ boswas-shell          taskbar, Start, quick settings,
                                 │     └─ boswas-services  notifications, tray, polkit agent (trusted)
                                 ├─▶ boswas-shell --lock   lock screen, PAM boswas-lock     (trusted)
                                 ├─▶ Xwayland              X server for X11 applications    (ordinary)
                                 └─▶ applications          Wayland clients                  (ordinary)

 Settings (Control Center) ─▶ boswas-services-bridge       the same service backends, as the user
                           └▶ ~/.config/boswas/compositor.toml, then "reload-config" to the compositor
```

| Part | Package | Language | Role |
|------|---------|----------|------|
| `boswas-compositor` | boswas-desktop | Rust (Smithay 0.7) | Draws every window and its frame, animates, routes all input, manages windows, desktops and monitors, runs the X server for X11 applications, owns the session lock, decides per client which protocols it is offered, and asks the user before a program gets pictures of the screen |
| `boswas-shell` | boswas-desktop | Rust (boswas-ui) | The taskbar, Start, quick settings, the notification centre and toasts, the tray, the volume and brightness display, Run, screen capture, the polkit password dialog and the capture question; with `--lock` the lock screen; with `--greeter` the login screen |
| `boswas-services` | boswas-desktop (library) | Rust, no `unsafe` | What the shell shows and controls: notifications, tray, media players, network, Bluetooth, audio, power, brightness, session and authentication (ADR-0035) |
| `boswas-services-bridge` | boswas-desktop | Rust | The network, Bluetooth, audio, power and brightness backends for the Settings application, as JSON lines on its standard input and output |
| `boswas-ui` | boswas-desktop (library) | Rust, no `unsafe` | Design tokens, software drawing, text, icons and materials shared by the compositor's window frames and the shell |
| `boswas-session`, `boswas-greeter` | boswas-desktop | POSIX shell | Session start after login; the login screen's start, with greetd's text login when the compositor cannot start (ADR-0044) |
| `boswas-bootsplashd`, `boswas-bootsplash` | boswas-bootsplash | Rust | Boot, disk-unlock and shutdown screens (in the initramfs and the system), drawn from vector outlines at the display's size, with motion (ADR-0050) |
| `boswas_fluent` | boswas-branding | Python (PySide6) | The Qt toolkit of the Boswas applications: theme, style sheet and widgets from the design tokens (ADR-0039) |
| The system applications | boswas-apps | Python (PySide6 on `boswas_fluent`) | Files, Terminal, Text Editor, Calculator, Photos, Document Viewer, Archive Manager, Media Player, Camera, Clock, Calendar with its reminders service, Snipping Tool, Weather, Task Manager (ADR-0040, ADR-0046) |
| Control Center | boswas-control-center | Python (PySide6 on `boswas_fluent`) | Settings as native sections (ADR-0041), and the Security, Software and Update Centers |
| `desktop/ui/tokens.json` | boswas-branding | JSON | Boswas UI, version 3: the palette, geometry, materials, type and motion. The one source for the programs above, the KDE colour schemes and the icon pack |

Trust: programs the compositor starts itself (the shell, the lock screen,
the greeter, and an input method if the administrator configures one) get
panels, the lock, the window list, clipboard control and the input-method
protocols. Every other program is an ordinary client and cannot read
other windows or keys typed elsewhere. It gets pictures of the screen
only after the user has allowed it in a dialog of the shell, and a mark
on the taskbar shows for as long as it may (ADR-0037). Sandboxed clients
(Flatpak) never get the privileged protocols and are not offered screen
capture except through the portal. X11 applications can observe each
other, not Wayland applications. Details: ADR-0026, ADR-0028, ADR-0034,
ADR-0037 and `docs/security/baseline.md`.

**State of verification.** The desktop packages have automated tests
that run without hardware. The built packages have been booted in a
virtual machine on another distribution's base system, from the boot
screen through the login to the desktop, with every fallback to a text
login (ADR-0044). No image has been built from this manifest: the QEMU
boot test, Debian 13 itself, real graphics hardware, the boot screen on
an encrypted disk and every real system service are not verified.
[verification.md](verification.md) lists both sides.

## System applications

The system applications are Boswas programs in one package, `boswas-apps`
(ADR-0040): PySide6 on the toolkit `boswas_fluent`, each a sub-package of
`boswas_apps` in `/usr/lib/boswas/python` with a launcher in `/usr/bin`,
a desktop entry, a manual page and a test suite. They replace the KDE
applications the image had.

| Application | Program | What it is |
|-------------|---------|------------|
| Files | `boswas-files` | File manager: tabs, a navigation pane with volumes and the Trash, four layouts, background copy, move and delete with progress, conflicts and undo, ZIP and TAR, default applications |
| Terminal | `boswas-terminal` | Terminal with tabs and profiles on its own xterm-compatible emulator; 10 000 lines of scrollback with search |
| Text Editor | `boswas-text-editor` | Plain text with tabs, encodings and line endings kept, find and replace, session restore, printing |
| Calculator | `boswas-calculator` | Standard, scientific and programmer modes, date calculation, unit converters; exact arithmetic |
| Photos | `boswas-photos` | Viewer, gallery, slideshow and an editor (crop, straighten, adjustments, pen) |
| Document Viewer | `boswas-document-viewer` | PDF reader on Qt PDF: tabs, search, outline, text selection, printing |
| Archive Manager | `boswas-archive-manager` | ZIP and TAR archives (7z and RAR with the `7zip` package): browse, extract safely, create, test |
| Media Player | `boswas-media-player` | Music and video through Qt Multimedia: library from tags, queue, playlists, MPRIS |
| Camera | `boswas-camera` | Photos and videos from a camera of the computer |
| Clock | `boswas-clock` | Alarms, timers, stopwatch, world clock, focus sessions |
| Calendar | `boswas-calendar` | A calendar kept on this computer, with repeating events, reminders and iCalendar import and export |
| Snipping Tool | `boswas-snipping-tool` | Screenshots through the desktop's capture screen (an area or the whole screen, with a delay), marked up with pen, highlighter, arrows, rectangles, text and crop; copy, save, open in Photos (ADR-0046) |
| Weather | `boswas-weather` | Forecasts for the places the user adds, from Open-Meteo: current conditions, 24 hours, details and 10 days; the last forecast offline (ADR-0046). It keeps the lock screen's weather summary, which a systemd user timer also renews every half hour, whether or not the window is open (ADR-0051) |
| Reminders | `boswas-reminders` | No window: a session service that shows alarms, timers and calendar reminders as notifications while Clock and Calendar are closed |
| Task Manager | `boswas-task-manager` | Processes grouped by application, performance graphs (CPU, memory, disks, network, GPU, battery), start-up applications, users, details and services; ends tasks |

Rules they share: they run as the user and refuse root, start other
programs with argument lists and never through a shell, treat file names
and file contents as hostile (bounds-checked parsers, size caps,
extraction that cannot leave its folder), delete to the Trash, and keep
long work off the window's thread. What each leaves out, and what has not
been verified, is in ADR-0040 and [verification.md](verification.md).

## Settings

Settings are sections inside the pages of Control Center (ADR-0041), not
programs of another desktop. The window is laid out like the Settings of
Windows 11 (ADR-0049): eleven categories (Home, System, Bluetooth &
devices, Network & internet, Personalization, Apps, Accounts, Time &
language, Accessibility, Privacy & security, Boswas Update), their pages
as cards, a path above the page, and search through pages, sections and
single settings. A section is a widget with a small contract (page,
title, order, keywords, `activate`, `deactivate`), found through a
registry. There are two backends:

| Backend | Used for | How |
|---------|----------|-----|
| `boswas-services-bridge` | Wi-Fi and other connections, Bluetooth, sound, battery, power mode, brightness | The shell's own service backends as a child process of the window, JSON lines on its pipes (ADR-0035). Quick settings and Settings show the same state |
| The compositor's user configuration | Monitors, night light, mouse, touchpad, keyboard, colour mode, accent colour, transparency, background, motion, lock and screen-off timeouts; the taskbar, Start, multitasking and notifications; text size, scroll bars, magnifier, colour filters, sticky and filter keys | Control Center writes `~/.config/boswas/compositor.toml` and (colour mode and background only) `appearance.toml` through its one file writer (`userfiles.py`) and sends `reload-config` to the public control socket (ADR-0036); the compositor passes the shell's part on in its `config` event |

Date and time and region use `timedatectl` and `localectl`; default and
start-up applications are files in the user's configuration directory.
User accounts go through AccountsService and polkit (the own password
through `passwd`). The pages of ADR-0049 use the system's own tools, each
in fixed, validated forms: `hostnamectl` (the device name), NetworkManager
through `nmcli` (VPN connections, adapters), CUPS (`lpstat`, `lp`),
fontconfig (`fc-list`, `fc-cache`), `dpkg-query`, Flatpak's permission
store, logind (restart into the firmware settings or the boot menu) and
APT's history log; installing and removing packages goes through
`boswas-sysops`. `[policy]` and the trusted programs stay the
administrator's: Settings never writes them and shows a limit set by
policy as the organisation's. `docs/experience/control-center.md`
describes every page.

## Debian upstream and Boswas components

| Upstream (Debian 13, unmodified) | Boswas-specific (this repository) |
|----------------------------------|-----------------------------------|
| Every package in the image manifest, pulled from deb.debian.org at build time | `packages/boswas-*`: thirteen native Debian packages (eleven in Python or data only; `boswas-desktop` and `boswas-bootsplash` in Rust) |
| The Rust crates named in the two `Cargo.lock` files, pulled from crates.io at package build time (ADR-0032) | `desktop/ui/tokens.json`, the Boswas Prism icon pack, the Qt toolkit `desktop/ui/qt` |
| `live-build`, `debootstrap`, Debian Installer | `config/live-build/`: image configuration, package lists, boot menu, hooks |
| Debian-signed shim, GRUB and kernel (Secure Boot) | `installer/`: preseed policy and installer branding |
| `/etc/os-release` (Debian identity kept) | `/usr/lib/boswas/release`, `image-info`, `/etc/boswas/` |

Licences: Debian components keep their own (see
`/usr/share/doc/*/copyright` on any device). Boswas components are covered by
`LICENSES/`.

## What runs on a device today (development tree after 1.0~alpha3)

| Blueprint component | v1 alpha state |
|---------------------|----------------|
| Debian 13 base | **Implemented** |
| Desktop | **Native Boswas desktop implemented** (`boswas-desktop`; ADR-0026 to ADR-0028, ADR-0030, ADR-0033 to ADR-0037): compositor with Fluent window frames, animations, Task View, snap layouts, per-monitor settings, night light and Xwayland; shell with taskbar, Start, quick settings, notifications, tray, the polkit dialog and screen capture; lock screen and greeter. It replaces KDE Plasma, which shipped up to 1.0~alpha3. **Booted from the built packages in a virtual machine on another base system only (ADR-0044), not from the image or on hardware, and not run against real system services.** Open: a second GPU, HDR, live blur, a screen reader interface, contrast themes, an input method in the image (see the roadmap) |
| System applications | **Implemented** (`boswas-apps`, ADR-0040): Files, Terminal, Text Editor, Calculator, Photos, Document Viewer, Archive Manager, Media Player, Camera, Clock, Calendar, Snipping Tool, Weather, Task Manager and a reminders service, in place of KDE's applications. Firefox ESR is the browser. **Not run on the desktop yet** |
| Boot screen | **Implemented** (`boswas-bootsplash`, ADR-0029, in the Boswas UI look since ADR-0038, drawn from vectors with motion since ADR-0050), replaces Plymouth. Booted in a virtual machine from the packages (ADR-0044), with the picture before ADR-0050; **not yet on the image or on hardware** |
| Boswas branding (boot menu, installer, boot screen, login, lock, desktop, icons, pointer, About) | **Implemented**; Boswas UI v3 tokens (ADR-0033), the Prism icon pack (ADR-0031), the Boswas-Cursors pointer theme (ADR-0043) and the Qt toolkit `boswas_fluent` (ADR-0039) |
| Secure Boot | **Boot chain ready:** the ISO boots with Secure Boot enforced via Debian's signed shim, GRUB and kernel; enforcement policy comes later |
| LUKS2 full-disk encryption | **Implemented** (installer policy: guided encrypted LVM) |
| AppArmor, nftables, auditd, sudo/pwquality, journald | **Implemented** (boswas-security) |
| USB / removable media policy | Framework only; not enforced |
| `boswas`, `boswas-info`, `boswas-status` | **Implemented**: read-only, `--json` |
| WinCompat (`boswas-winapp`, per-app prefixes, sandbox, AppArmor profile, manifests, catalog, policy) | **Implemented** (`boswas-compat`). 64-bit and 32-bit Windows applications (ADR-0022, which supersedes ADR-0014) |
| Compatibility Manager | **Implemented** (`boswas-compat-manager`, PySide6 on `boswas_fluent`), a client of the user's session agent |
| Control Center | **Implemented** (`boswas-control-center`, PySide6 on `boswas_fluent`): laid out like Windows 11's Settings in eleven categories (ADR-0049), native settings sections in place of the Plasma modules (ADR-0041), user accounts included, the desktop's taskbar, Start, multitasking, notification, colour and accessibility settings, and the Security, Software and Update Centers. **Its settings have run against stand-ins of the system services only** |
| Device agent, enrollment, heartbeat | **Implemented** (`boswas-device-agent`: system service, per-user session agent, `boswas-device`). Standalone unless a Control Plane is configured |
| Control Plane, signed policy, fleet dashboard | **Implemented** as a separate server package (`boswas-control-plane`, not on devices); policy hierarchy and organisation model open |
| Boswas Store, app policy | Milestone 6. `boswas app` reserved |
| Boswas repositories and promotion | Milestone 7. See `update-architecture.md` |
| Hardware certification, signed releases | Milestones 8–9 |
| Boswas ID / SSO | **Deferred**. Interfaces only (ADR-0015); local administrator account |

## Data and privacy boundaries

The OS collects **no telemetry** in v1 alpha. Local logs (journal, audit)
are bounded and stay on the device. See `docs/security/privacy.md`.

## Filesystem contract

| Path | Owner | Content |
|------|-------|---------|
| `/usr/lib/boswas/release` | boswas-os | OS identity (from `config/boswas/release.conf`) |
| `/usr/lib/boswas/image-info` | image build | Build ID, date, git commit, config hash |
| `/etc/boswas/device.conf` | boswas-os (administrator) | Device agent configuration: Control Plane, inventory, telemetry, update and logging policies (**never secrets**; the agent never writes it) |
| `/etc/boswas/compat/` | boswas-compat | WinCompat policy and local compatibility manifests |
| `/etc/apparmor.d/boswas-winapp` | boswas-compat | AppArmor profile for Windows applications |
| `/usr/share/boswas/compat/` | boswas-compat | Wine runtime facts, system catalog, manifest schema |
| `~/.local/share/boswas/wine/<id>/` | boswas-winapp (user) | One application: record, logs, prefix (seen as `/var/lib/boswas/wine/<id>/` inside its sandbox) |
| `/etc/boswas/update.conf` | boswas-os | Update channel |
| `/etc/boswas/firewall/` | boswas-security | Firewall ruleset and local drop-ins |
| `/etc/boswas/usb-policy/` | boswas-security | Removable-media policy |
| `/usr/share/boswas/` | boswas-branding | Artwork, presets and defaults for KDE applications |
| `/usr/share/boswas/ui/tokens.json` | boswas-branding | Boswas UI design tokens, version 3 (ADR-0033) |
| `/usr/lib/boswas/python/boswas_fluent/` | boswas-branding | The Qt toolkit of the Boswas applications (ADR-0039) |
| `/usr/share/boswas/compositor/` | boswas-desktop | Compositor defaults (`compositor.toml`), the login screen's configuration (`greeter.toml`), `version` |
| `/etc/boswas/compositor.toml` | administrator (not shipped) | Compositor settings for this device, `[policy]` and the trusted programs (`[shell]`, `[lock]`, `[input_method]` `command`) |
| `~/.config/boswas/compositor.toml`, `appearance.toml` | user, Control Center, `boswas-preset` | The user's compositor settings (never `[policy]` or a trusted program), written by Settings; the colour mode and wallpaper chosen with a preset or in Settings |
| `~/.config/<application id>/`, `~/.local/state/<application id>/` | the Boswas applications | Each application's settings and state (for example `com.boswas.Terminal/settings.json`) |
| `~/.config/boswas/shell.toml` | boswas-shell | The applications pinned to the taskbar and to Start |
| `~/.local/state/boswas/weather.json` | Weather (`boswas-apps`) | The weather summary the lock screen shows (ADR-0051); the lock screen reads it and never uses the network |
| `/usr/lib/systemd/user/boswas-weather-update.timer`, `.service` | boswas-apps | Weather's update for the lock screen, every half hour during a Boswas session |
| `~/.local/state/boswas/launcher-usage.json`, `run-history.json`, `notification-senders.json`, `apps-seen.json` | boswas-shell | How often each application was started (ranks search); the last commands typed into Run; the applications that sent notifications (Settings reads it to turn banners off); when each application was first seen (Start's "Recently added"). Local only |
| `/usr/libexec/boswas/boswas-shell`, `boswas-services-bridge` | boswas-desktop | The shell; the service backends for Settings |
| `/usr/share/xdg-desktop-portal/boswas-portals.conf`, `/etc/xdg/xdg-desktop-portal-wlr/Boswas` | boswas-desktop | Which portal back end serves what (GTK; wlroots for screen sharing and screenshots, ADR-0037) |
| `/etc/boswas/greetd.toml` | boswas-desktop | Login configuration read by greetd (drop-in, ADR-0030) |
| `/etc/pam.d/boswas-lock` | boswas-desktop | PAM service of the lock screen |
| `$XDG_RUNTIME_DIR/boswas-<display>.sock` | boswas-compositor | Control socket: `version`, `lock`, `reload-config`, `outputs` and `capture` for the user's programs |
| `/run/boswas-bootsplash/socket` | boswas-bootsplash | Boot screen control socket (root only) |
| `/var/lib/boswas/agent/` | boswas-device-agent | Device identity, status and inventory (public); credentials, applied policy, outbox, command ledger (root only) |
| `/var/lib/boswas/compat/` | boswas-device-agent | Managed WinCompat policy and catalog layer (from the Control Plane) |
| `/run/boswas-agent/agent.sock`, `$XDG_RUNTIME_DIR/boswas/session.sock` | device agent, session agent | Local management API |
| `/opt/boswas/` | reserved | Components that cannot be distro packages |
