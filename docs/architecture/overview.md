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
 ┌──────────────────────────────── Boswas OS workstation ───────────────────────────────┐
 │ Boswas layer       boswas-os · boswas-cli · boswas-branding · boswas-security         │
 │                    boswas-compat (WinCompat) · boswas-device-agent · boswas-compat-    │
 │                    manager  ·  later: store, updater, hardware                         │
 │ Desktop            KDE Plasma 6.3 (Wayland, X11 fallback) · SDDM · Boswas theme         │
 │ Applications       APT · Flatpak (no remote) · Wine 10 (64-bit) in per-app sandboxes    │
 │ Security           AppArmor · nftables · auditd · polkit · sudo · pwquality · journald  │
 │ Foundation         Debian 13 "trixie" · Linux 6.12 · systemd · PAM · NetworkManager     │
 │ Boot & disk        UEFI (BIOS fallback) · shim → GRUB → kernel (Debian-signed) · LUKS2  │
 └───────────────────────────────────────────────────────────────────────────────────────┘
```

## Debian upstream and Boswas components

| Upstream (Debian 13, unmodified) | Boswas-specific (this repository) |
|----------------------------------|-----------------------------------|
| Every package in the image manifest, pulled from deb.debian.org at build time | `packages/boswas-*`: five native Debian packages |
| `live-build`, `debootstrap`, Debian Installer | `config/live-build/`: image configuration, package lists, boot menu, hooks |
| Debian-signed shim, GRUB and kernel (Secure Boot) | `installer/`: preseed policy and installer branding |
| `/etc/os-release` (Debian identity kept) | `/usr/lib/boswas/release`, `image-info`, `/etc/boswas/` |

Licences: Debian components keep their own (see
`/usr/share/doc/*/copyright` on any device). Boswas components are covered by
`LICENSES/`.

## What runs on a device today (1.0~alpha3)

| Blueprint component | v1 alpha state |
|---------------------|----------------|
| Debian 13 base, KDE Plasma desktop | **Implemented** |
| Boswas branding (boot menu, installer, login, lock, desktop, About) | **Implemented** |
| Secure Boot | **Boot chain ready:** the ISO boots with Secure Boot enforced via Debian's signed shim, GRUB and kernel; enforcement policy comes later |
| LUKS2 full-disk encryption | **Implemented** (installer policy: guided encrypted LVM) |
| AppArmor, nftables, auditd, sudo/pwquality, journald | **Implemented** (boswas-security) |
| USB / removable media policy | Framework only; not enforced |
| `boswas`, `boswas-info`, `boswas-status` | **Implemented**: read-only, `--json` |
| WinCompat (`boswas-winapp`, per-app prefixes, sandbox, AppArmor profile, manifests, catalog, policy) | **Implemented** (`boswas-compat`). **x86_64 / 64-bit Windows applications only** (ADR-0014, final) |
| Compatibility Manager | **Implemented** (`boswas-compat-manager`, PySide6), a client of the user's session agent |
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
| `/usr/share/boswas/` | boswas-branding | Artwork and KDE defaults |
| `/var/lib/boswas/agent/` | boswas-device-agent | Device identity, status and inventory (public); credentials, applied policy, outbox, command ledger (root only) |
| `/var/lib/boswas/compat/` | boswas-device-agent | Managed WinCompat policy and catalog layer (from the Control Plane) |
| `/run/boswas-agent/agent.sock`, `$XDG_RUNTIME_DIR/boswas/session.sock` | device agent, session agent | Local management API |
| `/opt/boswas/` | reserved | Components that cannot be distro packages |
