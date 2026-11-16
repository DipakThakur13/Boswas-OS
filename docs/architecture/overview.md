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
 │ Applications  KDE and GTK applications as Wayland clients · X11 through Xwayland      │
 │               · APT · Flatpak (no remote) · Wine 10 in per-app sandboxes              │
 │               · Waydroid (opt-in)                                                     │
 │ Security      AppArmor · nftables · auditd · polkit · sudo · pwquality · journald     │
 │ Foundation    Debian 13 "trixie" · Linux 6.12 · systemd · PAM · NetworkManager        │
 │ Boot & disk   UEFI (BIOS fallback) · shim → GRUB → kernel (Debian-signed)             │
 │               · boswas-bootsplash (boot screen, disk passphrase) · LUKS2              │
 └───────────────────────────────────────────────────────────────────────────────────────┘
```

## The desktop stack

The desktop is Boswas software from the boot screen to the taskbar
(ADR-0026 to ADR-0039). KDE Plasma, KWin, SDDM and Plymouth are not
installed. The Wayland protocol stays, because it is what applications
speak; KDE's applications (Dolphin, Konsole, Okular and others) run as
ordinary Wayland clients, and applications that only speak X11 run
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
```

| Part | Package | Language | Role |
|------|---------|----------|------|
| `boswas-compositor` | boswas-desktop | Rust (Smithay 0.7) | Draws every window and its frame, animates, routes all input, manages windows, desktops and monitors, runs the X server for X11 applications, owns the session lock, decides per client which protocols it is offered, and asks the user before a program gets pictures of the screen |
| `boswas-shell` | boswas-desktop | Rust (boswas-ui) | The taskbar, Start, quick settings, the notification centre and toasts, the tray, the volume and brightness display, Run, screen capture, the polkit password dialog and the capture question; with `--lock` the lock screen; with `--greeter` the login screen |
| `boswas-services` | boswas-desktop (library) | Rust, no `unsafe` | What the shell shows and controls: notifications, tray, media players, network, Bluetooth, audio, power, brightness, session and authentication (ADR-0035) |
| `boswas-services-bridge` | boswas-desktop | Rust | The network, Bluetooth, audio, power and brightness backends for the Settings application, as JSON lines on its standard input and output |
| `boswas-ui` | boswas-desktop (library) | Rust, no `unsafe` | Design tokens, software drawing, text, icons and materials shared by the compositor's window frames and the shell |
| `boswas-session`, `boswas-greeter` | boswas-desktop | POSIX shell | Session start after login; the login screen's start with a text-login fallback |
| `boswas-bootsplashd`, `boswas-bootsplash` | boswas-bootsplash | Rust | Boot, disk-unlock and shutdown screens (in the initramfs and the system) |
| `boswas_fluent` | boswas-branding | Python (PySide6) | The Qt toolkit of the Boswas applications: theme, style sheet and widgets from the design tokens (ADR-0039). Control Center and the Compatibility Manager use it |
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
that run without hardware, and nothing of the stack has yet booted: no
image has been built from this manifest, and the QEMU boot test, real
graphics hardware, the greetd login, the boot screen on an encrypted disk
and every real system service are not verified.
[verification.md](verification.md) lists both sides.

## System applications

*Reserved for the applications of `boswas-apps` (ADR-0040). To be written
when the work lands. Until then the image list names KDE's applications
(Dolphin, Konsole, KWrite, Ark, Okular, Gwenview, KCalc) and Firefox
ESR.*

## Settings

*Reserved for the native settings pages of Control Center (ADR-0041). To
be written when the work lands. `docs/experience/control-center.md`
describes Control Center as it was before them.*

## Debian upstream and Boswas components

| Upstream (Debian 13, unmodified) | Boswas-specific (this repository) |
|----------------------------------|-----------------------------------|
| Every package in the image manifest, pulled from deb.debian.org at build time | `packages/boswas-*`: twelve native Debian packages (ten in Python or data only; `boswas-desktop` and `boswas-bootsplash` in Rust) |
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
| Desktop | **Native Boswas desktop implemented** (`boswas-desktop`; ADR-0026 to ADR-0028, ADR-0030, ADR-0033 to ADR-0037): compositor with Fluent window frames, animations, Task View, snap layouts, per-monitor settings, night light and Xwayland; shell with taskbar, Start, quick settings, notifications, tray, the polkit dialog and screen capture; lock screen and greeter. It replaces KDE Plasma, which shipped up to 1.0~alpha3. **Not yet boot-tested, and not run against real system services.** Open: a second GPU, HDR and variable refresh rate, live blur, a screen reader interface, an input method in the image (see the roadmap) |
| System applications | KDE's applications and Firefox ESR, as clients of the native desktop. Boswas's own applications: reserved, ADR-0040 |
| Boot screen | **Implemented** (`boswas-bootsplash`, ADR-0029, in the Boswas UI look since ADR-0038), replaces Plymouth. **Not yet boot-tested** |
| Boswas branding (boot menu, installer, boot screen, login, lock, desktop, icons, About) | **Implemented**; Boswas UI v3 tokens (ADR-0033), the Prism icon pack (ADR-0031) and the Qt toolkit `boswas_fluent` (ADR-0039) |
| Secure Boot | **Boot chain ready:** the ISO boots with Secure Boot enforced via Debian's signed shim, GRUB and kernel; enforcement policy comes later |
| LUKS2 full-disk encryption | **Implemented** (installer policy: guided encrypted LVM) |
| AppArmor, nftables, auditd, sudo/pwquality, journald | **Implemented** (boswas-security) |
| USB / removable media policy | Framework only; not enforced |
| `boswas`, `boswas-info`, `boswas-status` | **Implemented**: read-only, `--json` |
| WinCompat (`boswas-winapp`, per-app prefixes, sandbox, AppArmor profile, manifests, catalog, policy) | **Implemented** (`boswas-compat`). 64-bit and 32-bit Windows applications (ADR-0022, which supersedes ADR-0014) |
| Compatibility Manager | **Implemented** (`boswas-compat-manager`, PySide6 on `boswas_fluent`), a client of the user's session agent |
| Control Center | **Implemented** (`boswas-control-center`, PySide6 on `boswas_fluent`). Its own pages do not depend on Plasma. Native settings pages in place of the Plasma modules: reserved, ADR-0041 |
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
| `~/.config/boswas/compositor.toml`, `appearance.toml` | user, `boswas-preset` | The user's compositor settings (never `[policy]` or a trusted program); the look chosen with a preset |
| `~/.config/boswas/shell.toml` | boswas-shell | The applications pinned to the taskbar and to Start |
| `~/.local/state/boswas/launcher-usage.json`, `run-history.json` | boswas-shell | How often each application was started (ranks search); the last commands typed into Run. Local only |
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
