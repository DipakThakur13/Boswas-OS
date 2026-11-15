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
 │               · Boswas Shell (panel, launcher, lock screen, greeter)                  │
 │               · Boswas UI tokens (Prism) · login through greetd                       │
 │ Applications  KDE and GTK applications as Wayland clients · APT · Flatpak (no         │
 │               remote) · Wine 10 in per-app sandboxes · Waydroid (opt-in)              │
 │ Security      AppArmor · nftables · auditd · polkit · sudo · pwquality · journald     │
 │ Foundation    Debian 13 "trixie" · Linux 6.12 · systemd · PAM · NetworkManager        │
 │ Boot & disk   UEFI (BIOS fallback) · shim → GRUB → kernel (Debian-signed)             │
 │               · boswas-bootsplash (boot screen, disk passphrase) · LUKS2              │
 └───────────────────────────────────────────────────────────────────────────────────────┘
```

## The desktop stack

The desktop is Boswas software from the boot screen to the panel
(ADR-0026 to ADR-0032). KDE Plasma, KWin, SDDM and Plymouth are not
installed. The Wayland protocol stays, because it is what applications
speak; KDE's applications (Dolphin, Konsole, Okular and others) run as
ordinary Wayland clients.

```
 kernel + initramfs ─▶ boswas-bootsplashd      boot screen, disk passphrase   (root, confined)
        │                    │ hands the screen over
        ▼                    ▼
 greetd (login daemon) ─▶ boswas-greeter       boswas-compositor + boswas-shell --greeter
        │  PAM                                 as the unprivileged user _greetd
        ▼
 boswas-session (the user) ─▶ boswas-compositor      display server, window manager, lock
                                 ├─▶ boswas-shell          panel, launcher, wallpaper   (trusted)
                                 ├─▶ boswas-shell --lock   lock screen, PAM boswas-lock (trusted)
                                 └─▶ applications          Wayland clients              (ordinary)
```

| Part | Package | Language | Role |
|------|---------|----------|------|
| `boswas-compositor` | boswas-desktop | Rust (Smithay 0.7) | Draws every window, routes all input, manages windows and workspaces, owns the session lock, decides per client which protocols it is offered |
| `boswas-shell` | boswas-desktop | Rust (boswas-ui) | Panel, launcher and wallpaper; with `--lock` the lock screen; with `--greeter` the login screen |
| `boswas-ui` | boswas-desktop (library) | Rust, no `unsafe` | Design tokens, software drawing, text and icons shared by the compositor's window frames and the shell |
| `boswas-session`, `boswas-greeter` | boswas-desktop | POSIX shell | Session start after login; the login screen's start with a text-login fallback |
| `boswas-bootsplashd`, `boswas-bootsplash` | boswas-bootsplash | Rust | Boot, disk-unlock and shutdown screens (in the initramfs and the system) |
| `desktop/ui/tokens.json` | boswas-branding | JSON | The Prism palette, geometry, type and motion: the one source for the programs above, the KDE colour schemes and the icon pack |

Trust: programs the compositor starts itself (the shell, the lock screen,
the greeter) get panels, the lock, the window list and clipboard control.
Every other program is an ordinary client and cannot read the screen,
other windows or keys typed elsewhere. Sandboxed clients (Flatpak) never
get the privileged protocols. Details: ADR-0026, ADR-0028 and
`docs/security/baseline.md`.

**State of verification.** The desktop packages build, pass lintian and
their tests (unit tests, and end-to-end tests of the compositor on its
headless back end), and the compositor has run nested with OpenGL ES.
Nothing of the new stack has yet booted: the image has not been built with
the new manifest, and the QEMU boot test, real graphics hardware, the
greetd login and the boot screen on an encrypted disk are not verified.

## Debian upstream and Boswas components

| Upstream (Debian 13, unmodified) | Boswas-specific (this repository) |
|----------------------------------|-----------------------------------|
| Every package in the image manifest, pulled from deb.debian.org at build time | `packages/boswas-*`: twelve native Debian packages (ten in Python or data only; `boswas-desktop` and `boswas-bootsplash` in Rust) |
| The Rust crates named in the two `Cargo.lock` files, pulled from crates.io at package build time (ADR-0032) | `desktop/ui/tokens.json`, the Boswas Prism icon pack |
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
| Desktop | **Native Boswas desktop implemented** (`boswas-desktop`: compositor, shell, lock screen, greeter; ADR-0026 to ADR-0028, ADR-0030). It replaces KDE Plasma, which shipped up to 1.0~alpha3. **Not yet boot-tested.** Open: notifications, tray, quick settings, screen capture, input methods, XWayland (see the roadmap) |
| Boot screen | **Implemented** (`boswas-bootsplash`, ADR-0029), replaces Plymouth. **Not yet boot-tested** |
| Boswas branding (boot menu, installer, boot screen, login, lock, desktop, icons, About) | **Implemented**; Boswas UI v2 tokens and the Prism icon pack (ADR-0031) |
| Secure Boot | **Boot chain ready:** the ISO boots with Secure Boot enforced via Debian's signed shim, GRUB and kernel; enforcement policy comes later |
| LUKS2 full-disk encryption | **Implemented** (installer policy: guided encrypted LVM) |
| AppArmor, nftables, auditd, sudo/pwquality, journald | **Implemented** (boswas-security) |
| USB / removable media policy | Framework only; not enforced |
| `boswas`, `boswas-info`, `boswas-status` | **Implemented**: read-only, `--json` |
| WinCompat (`boswas-winapp`, per-app prefixes, sandbox, AppArmor profile, manifests, catalog, policy) | **Implemented** (`boswas-compat`). **x86_64 / 64-bit Windows applications only** (ADR-0014, final) |
| Compatibility Manager | **Implemented** (`boswas-compat-manager`, PySide6), a client of the user's session agent |
| Control Center | **Implemented** (`boswas-control-center`, PySide6). Its own pages do not depend on Plasma; the entries that open Plasma settings modules are unavailable, because those modules are no longer installed |
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
| `/usr/share/boswas/ui/tokens.json` | boswas-branding | Boswas UI design tokens (ADR-0031) |
| `/usr/share/boswas/compositor/` | boswas-desktop | Compositor defaults (`compositor.toml`), the login screen's configuration (`greeter.toml`), `version` |
| `/etc/boswas/compositor.toml` | administrator (not shipped) | Compositor settings and `[policy]` for this device |
| `~/.config/boswas/compositor.toml`, `appearance.toml` | user, `boswas-preset` | The user's compositor settings (never `[policy]`); the look chosen with a preset |
| `/etc/boswas/greetd.toml` | boswas-desktop | Login configuration read by greetd (drop-in, ADR-0030) |
| `/etc/pam.d/boswas-lock` | boswas-desktop | PAM service of the lock screen |
| `$XDG_RUNTIME_DIR/boswas-<display>.sock` | boswas-compositor | Control socket: `version`, `lock`, `reload-config` for the user's programs |
| `/run/boswas-bootsplash/socket` | boswas-bootsplash | Boot screen control socket (root only) |
| `/var/lib/boswas/agent/` | boswas-device-agent | Device identity, status and inventory (public); credentials, applied policy, outbox, command ledger (root only) |
| `/var/lib/boswas/compat/` | boswas-device-agent | Managed WinCompat policy and catalog layer (from the Control Plane) |
| `/run/boswas-agent/agent.sock`, `$XDG_RUNTIME_DIR/boswas/session.sock` | device agent, session agent | Local management API |
| `/opt/boswas/` | reserved | Components that cannot be distro packages |
