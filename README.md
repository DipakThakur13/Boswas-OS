# Boswas OS

**Boswas OS v1** is the Boswas Group company-managed desktop operating
system, built on **Debian 13 "trixie"** with **KDE Plasma**. It is an
internal platform for Boswas Group employees and company-owned hardware, not
a public Linux distribution.

> **Status: v1 alpha (1.0~alpha3). Not production-ready.**
> This release contains:
> - the Boswas OS experience: Boswas OS identity, boot splash, login,
>   ten presets, Boswas icons, Boswas Launcher, Boswas Control Center (with
>   the Security, Software and Update Centers) and a branded terminal, on
>   KDE Plasma and a Debian 13 base
> - a Live USB that boots straight to a usable desktop; installation only
>   by explicit choice
> - security baseline and encrypted installer
> - `boswas` status tools
> - **WinCompat:** Windows applications in isolated, AppArmor-confined Wine
>   sandboxes (`boswas-winapp`); **x86_64 / 64-bit Windows applications only**
>   (32-bit is intentionally not supported)
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
| Desktop | KDE Plasma 6.3 (Wayland) with the Boswas OS experience: ten presets (Global Themes, colour schemes, wallpapers, Konsole profiles), Boswas icon themes, Boswas Launcher, Boswas splash, login and lock screens, Boswas Control Center ([docs/experience](docs/experience/README.md)) |
| Branding | Built from the official Boswas Group logo and lettering ([desktop/branding](desktop/branding/README.md)) |
| Boot | GRUB for BIOS and UEFI; Debian-signed shim/GRUB/kernel for Secure Boot; Boswas boot menu and Plymouth boot, shutdown and passphrase splash |
| Installer | Debian Installer (live mode), Boswas banner, policy: full-disk encryption, root locked, host `boswas-device` |
| Security | nftables (inbound deny), AppArmor, auditd rules, sudo/pwquality policy, kernel hardening, no SSH server, security-only automatic updates, enforced screen lock |
| Tools | `boswas`, `boswas-info`, `boswas-status` (read-only, `--json`) |
| Compatibility | Wine 10, **x86_64 / 64-bit Windows applications only**. `boswas-winapp`: one prefix per application, bubblewrap sandbox, `boswas-winapp` AppArmor profile, compatibility manifests and catalog, policy, "Run with Boswas". Compatibility Manager (PySide6). Flatpak without a remote |
| Device management | `boswas-device-agent` (sandboxed service, per-user session agent, `boswas-device`), mutual TLS to the Boswas Control Plane (`control-plane/`, server package): enrollment, heartbeat, inventory, typed commands, signed policies, catalog, audit, dashboard |

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
| The Boswas OS experience | [docs/experience/README.md](docs/experience/README.md) |
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
