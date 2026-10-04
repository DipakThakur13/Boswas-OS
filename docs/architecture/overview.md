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
 Boswas Control Plane (future)   identity · devices · policies · apps · updates · audit
            │  HTTPS + mTLS (Phase 7)
            ▼
 ┌──────────────────────────────── Boswas OS workstation ───────────────────────────────┐
 │ Boswas layer       boswas-os · boswas-cli · boswas-branding · boswas-security         │
 │                    (later: boswas-device-agent, boswas-id, boswas-store, boswas-compat) │
 │ Desktop            KDE Plasma 6.3 (Wayland, X11 fallback) · SDDM · Boswas theme         │
 │ Applications       APT · Flatpak (no remote until policy) · Wine 10 (64-bit)            │
 │ Security           AppArmor · nftables · auditd · polkit · sudo · pwquality · journald  │
 │ Foundation         Debian 13 "trixie" · Linux 6.12 · systemd · PAM · NetworkManager     │
 │ Boot & disk        UEFI (BIOS fallback) · shim → GRUB → kernel (Debian-signed) · LUKS2  │
 └───────────────────────────────────────────────────────────────────────────────────────┘
```

## Debian upstream and Boswas components

| Upstream (Debian 13, unmodified) | Boswas-specific (this repository) |
|----------------------------------|-----------------------------------|
| Every package in the image manifest, pulled from deb.debian.org at build time | `packages/boswas-*`: four native Debian packages |
| `live-build`, `debootstrap`, Debian Installer | `config/live-build/`: image configuration, package lists, boot menu, hooks |
| Debian-signed shim, GRUB and kernel (Secure Boot) | `installer/`: preseed policy and installer branding |
| `/etc/os-release` (Debian identity kept) | `/usr/lib/boswas/release`, `image-info`, `/etc/boswas/` |

Licences: Debian components keep their own (see
`/usr/share/doc/*/copyright` on any device). Boswas components are covered by
`LICENSES/`.

## What runs on a device today (v1 alpha)

| Blueprint component | v1 alpha state |
|---------------------|----------------|
| Debian 13 base, KDE Plasma desktop | **Implemented** |
| Boswas branding (boot menu, installer, login, lock, desktop, About) | **Implemented** |
| Secure Boot | **Boot chain ready:** the ISO boots with Secure Boot enforced via Debian's signed shim, GRUB and kernel; enforcement policy comes later |
| LUKS2 full-disk encryption | **Implemented** (installer policy: guided encrypted LVM) |
| AppArmor, nftables, auditd, sudo/pwquality, journald | **Implemented** (boswas-security) |
| USB / removable media policy | Framework only; not enforced |
| `boswas`, `boswas-info`, `boswas-status` | **Implemented**: read-only, `--json` |
| Device agent, enrollment, heartbeat | Phase 4. `/etc/boswas/device.conf` contract exists; nothing contacts a backend |
| Boswas ID / SSO login broker | Phase 7. Local administrator account only |
| Boswas Store, app policy | Phase 5. `boswas app` reserved |
| WinCompat (`boswas-winapp`, per-app prefixes) | Phase 6. Wine 10 runtime installed |
| Boswas repositories and promotion | Phase 7/8. See `update-architecture.md` |

## Data and privacy boundaries

The OS collects **no telemetry** in v1 alpha. Local logs (journal, audit)
are bounded and stay on the device. See `docs/security/privacy.md`.

## Filesystem contract

| Path | Owner | Content |
|------|-------|---------|
| `/usr/lib/boswas/release` | boswas-os | OS identity (from `config/boswas/release.conf`) |
| `/usr/lib/boswas/image-info` | image build | Build ID, date, git commit, config hash |
| `/etc/boswas/device.conf` | boswas-os → agent | Device ID, tenant, enrollment state (**never secrets**) |
| `/etc/boswas/update.conf` | boswas-os | Update channel |
| `/etc/boswas/firewall/` | boswas-security | Firewall ruleset and local drop-ins |
| `/etc/boswas/usb-policy/` | boswas-security | Removable-media policy |
| `/usr/share/boswas/` | boswas-branding | Artwork and KDE defaults |
| `/var/lib/boswas/` | future agent | Agent state and policy cache |
| `/opt/boswas/` | reserved | Components that cannot be distro packages |
