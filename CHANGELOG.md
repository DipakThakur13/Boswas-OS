# Changelog

All notable changes to Boswas OS. Versions follow `config/boswas/release.conf`.

## [1.0~alpha2] - 2026-10-04 (Milestone 1: Windows compatibility platform)

### Added
- **`boswas-compat` package (WinCompat).**
  - **`boswas-winapp`:** `install`, `remove`, `list`, `launch`, `status`,
    `repair`, `logs`, `catalog` and `manifest validate`, with `--json` and
    stable exit codes.
  - **Prefixes:** one Wine prefix per application, owned by the user,
    stored in `~/.local/share/boswas/wine/<id>/` and seen as
    `/var/lib/boswas/wine/<id>/` inside its sandbox. Windows software never
    runs as root.
  - **bubblewrap sandbox per application:**
    - never visible: the user's home, `/home`, D-Bus, `/run`, or other
      prefixes;
    - only when granted: network, display, audio, GPU and folders;
    - cleared environment.
  - **AppArmor profile `boswas-winapp`:**
    - attached to the in-sandbox runner;
    - grants no capabilities, mounts, user namespaces or D-Bus;
    - nothing in a prefix may run as a Linux program;
    - Wine refuses to start unless the profile is enforcing.
  - **Compatibility manifest format v1:**
    - JSON schema plus a stdlib validator;
    - statuses `unknown`, `untested`, `experimental`, `tested`,
      `approved`, `blocked`;
    - pinned installer SHA-256 for validated statuses.
  - **Layered compatibility catalog:** managed, local and system layers;
    `blocked` in any layer wins.
  - **Policy:** `/etc/boswas/compat/policy.conf`, fail-closed.
  - **Prefix defaults:** no crash dialog; `winemenubuilder` disabled.
  - **Installer handling:** detection by signature, PE architecture check,
    NSIS/Inno/MSI silent switches.
  - **"Run with Boswas":** handler for `.exe` and `.msi`.
- **Device agent foundations** (`packages/boswas-device-agent/`, not
  packaged yet):
  - API v1 message models and JSON schemas (heartbeat, enrollment,
    compliance, commands);
  - `ControlPlaneClient`, `CredentialStore`, `PolicyVerifier`,
    `InventoryCollector` and `CommandHandler` interfaces, plus an offline
    client;
  - random UUID v4 device identity and validated `device.conf` writer;
  - closed privacy allowlists;
  - Windows application inventory collector;
  - Boswas ID boundary interfaces only.
- **`device.conf` contract:** `DEVICE_PROFILE`, `DEVICE_CERTIFICATE`
  (public certificate reference).
- **`boswas` CLI:**
  - `boswas winapp ...` runs `boswas-winapp`;
  - `boswas device status` shows profile and certificate;
  - new posture check `winapp-confinement`.
- **Tests:**
  - unit tests for WinCompat and the agent;
  - static checks for manifests and profile consistency;
  - package checks for `boswas-compat`;
  - image checks;
  - an end-to-end WinCompat runtime test in the image (real Wine and
    bubblewrap, isolation probes, policy refusals);
  - WinCompat checks in the QEMU boot test under the real kernel's AppArmor.
  - The Windows test application is built from source with MinGW-w64.
- **Documentation:** compatibility guide, device-management guide, ADRs
  0011–0016, milestone roadmap.

### Changed
- **Builder image:** gains `dh-apparmor`, `apparmor` and
  `gcc-mingw-w64-x86-64-win32`.
- **Build inputs:** the package build and the config hash include
  `compatibility/`.
- **Image:** `boswas-os` depends on `boswas-compat`. The image verify hook
  checks `boswas-winapp` and compiles the profile.

### Known limitations
- **64-bit Windows programs only.** 32-bit installers are refused with a
  clear message; adding `wine32` or a WoW64 Wine is an open decision
  (ADR-0014).
- **Empty catalog.** The system catalog is empty until applications are
  validated.
- **Permissive alpha policy.** The shipped policy allows unlisted
  applications, with no network.
- **Direct Wine use.** Direct `/usr/bin/wine` use is not mediated.
- **X11.** X11 applications can observe other X11 clients (Xwayland only
  under Plasma Wayland).
- **No device agent service yet:** no backend contact (Milestone 2).

## [1.0~alpha1] - 2026-10-04 (v1 Alpha)

First bootable foundation. Phases 0–2 complete, Phase 3 baseline.

### Added
- **Repository and build system:**
  - Repository structure and architecture documentation (ADRs, roadmap,
    update architecture, privacy, HCL).
  - `build.sh`: native on Debian 13, or containerised on other hosts. It
    produces the ISO, SHA-256 checksum, manifest and JSON build record.
  - `clean.sh`, `test.sh` and a `Makefile`.
- **Image:**
  - Debian 13 "trixie" live-build configuration with an explicit,
    documented package manifest and build-time exclusions.
  - KDE Plasma 6.3 desktop.
  - GRUB boot menu for BIOS and UEFI; Debian-signed Secure Boot chain.
  - Debian Installer in live mode. Its preseed enforces encrypted LVM
    (LUKS2), locks root and defaults the hostname to `boswas-device`.
- **Branding:** built from the official Boswas Group logo and lettering
  (traced vectors): boot menu, installer banner, Plasma global theme, Boswas
  Dark colour scheme, wallpaper, login and lock screens, launcher icon and
  About page.
- **`boswas-security`:**
  - nftables firewall (inbound deny)
  - systemd preset (firewall on; SSH off unless provisioned)
  - kernel hardening sysctls
  - auditd rules
  - journald retention
  - sudo and pwquality policy
  - sshd policy
  - security-only unattended upgrades
  - USB policy framework
- **`boswas-cli`:** `boswas`, `boswas-info` and `boswas-status`, with
  `--json` output, stable exit codes and unit tests.
- **`boswas-os`:** release identity, `/etc/boswas` (device and update
  configuration contracts) and a login banner.
- **Tests:** static checks, lintian, package manifest resolution, ISO and
  image content, security baseline, Wine, and a QEMU boot test (UEFI Secure
  Boot plus serial functional checks).

### Known limitations
- No device agent, Boswas ID, Store or WinCompat tooling yet (Phases 4–7).
- **Windows apps:** 64-bit only (no `wine32`).
- **USB policy:** not enforced.
- **Installed system:** no graphical boot splash.
- **Release artifacts:** not yet signed. Builds are not yet pinned to a
  Debian snapshot.
