# Boswas OS

**Boswas OS v1** is the Boswas Group company-managed desktop operating
system, built on **Debian 13 "trixie"** with **KDE Plasma**. It is an
internal platform for Boswas Group employees and company-owned hardware, not
a public Linux distribution.

> **Status: v1 alpha. Not production-ready.**
> This release is a bootable foundation:
> - Debian 13 base, KDE Plasma desktop and Boswas branding
> - security baseline and encrypted installer
> - `boswas` status tools
> - reproducible, script-driven builds
>
> Device management, Boswas ID, the Boswas Store and WinCompat follow in
> later phases (see `docs/architecture/roadmap.md`).

Debian remains upstream. This repository contains only Boswas-specific
packaging, configuration, artwork, scripts, tests and documentation; every
other component is an unmodified Debian package.

## Build

### Requirements

- **Either** a Debian 13 (trixie) host with root,
- **or** any host with Docker or Podman, including Windows with Docker
  Desktop (WSL2 backend), macOS and other Linux distributions. The build runs
  in a `debian:trixie` builder container automatically.
- About 25 GB of free disk space, a network connection to deb.debian.org and
  a correct system clock.

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
```

## What is in v1 alpha

| Area | Implementation |
|------|----------------|
| Base | Debian 13.x trixie, amd64, Linux 6.12, systemd |
| Desktop | KDE Plasma 6.3 (Wayland), Boswas global theme, Boswas Dark colours, wallpaper, login and lock screens, About page |
| Branding | Built from the official Boswas Group logo and lettering ([desktop/branding](desktop/branding/README.md)) |
| Boot | GRUB for BIOS and UEFI; Debian-signed shim/GRUB/kernel for Secure Boot; branded boot menu |
| Installer | Debian Installer (live mode), Boswas banner, policy: full-disk encryption, root locked, host `boswas-device` |
| Security | nftables (inbound deny), AppArmor, auditd rules, sudo/pwquality policy, kernel hardening, no SSH server, security-only automatic updates, enforced screen lock |
| Tools | `boswas`, `boswas-info`, `boswas-status` (read-only, `--json`) |
| Compatibility | Wine 10 (64-bit), Flatpak + bubblewrap (no remote until policy) |

## Documentation

| Topic | Document |
|-------|----------|
| Architecture overview | [docs/architecture/overview.md](docs/architecture/overview.md) |
| Decisions (ADRs) | [docs/architecture/decisions.md](docs/architecture/decisions.md) |
| Package manifest and rationale | [docs/architecture/package-manifest.md](docs/architecture/package-manifest.md) |
| Update channels and repositories | [docs/architecture/update-architecture.md](docs/architecture/update-architecture.md) |
| Roadmap | [docs/architecture/roadmap.md](docs/architecture/roadmap.md) |
| Building | [docs/development/building.md](docs/development/building.md) |
| Repository layout | [docs/development/repository-layout.md](docs/development/repository-layout.md) |
| Security baseline | [docs/security/baseline.md](docs/security/baseline.md) |
| Privacy | [docs/security/privacy.md](docs/security/privacy.md) |
| Installation | [docs/deployment/installation.md](docs/deployment/installation.md) |
| Hardware Compatibility List | [docs/deployment/hardware-compatibility.md](docs/deployment/hardware-compatibility.md) |
| CLI reference | [docs/administration/cli.md](docs/administration/cli.md) |
| Windows compatibility | [docs/compatibility/README.md](docs/compatibility/README.md) |

## Licensing and attribution

Boswas OS is based on Debian. Debian components keep their own licences
(`/usr/share/doc/*/copyright` on every device), and Debian's identity stays
intact (`/etc/os-release`). Boswas-authored files: see
[LICENSES/](LICENSES/README.md). Security reports: [SECURITY.md](SECURITY.md).
Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).
