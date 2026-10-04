# Changelog

All notable changes to Boswas OS. Versions follow `config/boswas/release.conf`.

## [1.0~alpha3] - 2026-10-04 (Device Agent, Compatibility Manager, Control Plane)

### Added
- **`boswas-device-agent` package**
  ([docs/device-management/README.md](docs/device-management/README.md)):
  - **Identity:** a persistent, random device identity
    (`/var/lib/boswas/agent/identity.json`). It is never derived from
    hardware identifiers and never silently regenerated; it can be
    pre-provisioned to survive reinstallation.
  - **Configuration:** `device.conf` gains agent settings (Control Plane URL
    and pinned CA, remote commands, heartbeat and inventory intervals,
    inventory, telemetry, update and logging policies). Validation refuses
    secrets and insecure files (`boswas-device config validate`).
  - **Inventory:** allowlisted: OS, Boswas packages, Windows applications
    aggregated over users, Windows runtime health; with `standard` also CPU,
    memory, model and disk. Never serial numbers, MAC addresses or user names.
  - **States:** normalised device states (INITIALIZING, READY, DEGRADED,
    OFFLINE, UPDATING, ERROR, MAINTENANCE) with validated transitions, and
    application states.
  - **`boswas-device-agent.service`** (root, sandboxed):
    - local management API with kernel-credential authorisation;
    - Control Plane client over mutual TLS: enrollment with a one-time token
      and a device-generated EC key, heartbeats, inventory, posture, events;
    - a bounded offline outbox with capped exponential back-off;
    - signed policies;
    - typed command execution, idempotent and expiring;
    - agent updates through `boswas-agent-update@.service`.
  - **`boswas-session-agent.service`** (systemd user unit): the backend of
    the Compatibility Manager, and the executor of remote application
    commands as the logged-in user, through `boswas-winapp`.
  - **`boswas-device`:** status, identity, configuration, inventory, policy,
    commands, events, enrollment, maintenance.
- **`boswas-compat-manager` package (Compatibility Manager, PySide6):**
  - **Pages:** dashboard; application library with filters (All, Installed,
    Running, Updates, Blocked, Repair Required, Unsupported); system status.
  - **Install:** checks the installer first and refuses 32-bit installers
    with the product message.
  - **Application details:** read-only permissions; logs with copy, save and
    clear.
  - **Actions:** launch, stop, repair, update and remove (with an explicit
    removal plan).
  - **Boundaries:** it never runs Windows code or touches prefixes itself.
  - **Dolphin action:** "Install with Boswas Compatibility Manager".
- **Boswas Control Plane** (`control-plane/`, server package
  `boswas-control-plane`, Python stdlib + SQLite, ADR-0017;
  [docs/device-management/control-plane.md](docs/device-management/control-plane.md)):
  - device registry, enrollment with a device CA;
  - one-time enrollment tokens; an enrolled device re-enrolls only with a
    token issued for its ID (`token create --device`);
  - heartbeat, offline detection, inventory and posture;
  - typed command system (lifecycle QUEUED → SENT → ACKNOWLEDGED → RUNNING →
    SUCCEEDED/FAILED, EXPIRED, CANCELLED; idempotency keys; expiry);
  - Ed25519-signed, versioned policies;
  - catalog of 64-bit applications with stored installers (32-bit entries
    are UNSUPPORTED_ARCHITECTURE and never installable);
  - hash-chained audit trail;
  - operator API tokens with roles;
  - HTTPS API `/api/v1` on two ports: the device port (mutual TLS) and the
    operator port (dashboard and operator API; no client-certificate
    request, so browsers show no certificate prompt);
  - web dashboard;
  - `boswas-cp` administration.
- **`boswas-winapp`:**
  - new commands `inspect` (dry run), `stop`, `upgrade`, `runtime`, and
    `logs --launch` and `--clear`;
  - normalised application states;
  - MSI architecture detection;
  - policy keys `BLOCKED_APPLICATIONS`, `ALLOWED_APPLICATIONS`;
  - the managed policy layer (`/var/lib/boswas/compat/policy.conf`).
- **Tests:**
  - unit tests for every component (WinCompat 91, device agent 96,
    Compatibility Manager 105, Control Plane 31 including the end-to-end
    agent ↔ Control Plane tests over mutual TLS, CLI 25);
  - a device management image check;
  - a device management runtime stage in the image (real Wine, session
    agent, Compatibility Manager window, Control Plane);
  - device management checks in the QEMU boot test under AppArmor;
  - new static checks (64-bit only, no shell execution, no Boswas ID,
    service hardening).
- **Documentation:**
  - device management and Control Plane guides;
  - ADR-0017 to ADR-0021;
  - updated compatibility, privacy, security, CLI and build documentation.

### Changed
- **New Boswas OS brand (ADR-0008 revised;
  [desktop/branding](desktop/branding/README.md)):**
  - **Logo:** the Boswas OS "b" mark (gold stem and dot, silver ring and
    swoosh) replaces the Boswas Group gear everywhere: launcher icon, About
    page, boot menu, wallpaper, login and lock screens, installer banner,
    Compatibility Manager icon and dashboard favicon. It is traced from the
    supplied logo (`tools/trace_mark.py`).
  - **Typeface:** Orbitron is the brand typeface. The wordmarks are
    Orbitron outlines, and `boswas-branding` installs the font (SIL OFL 1.1).
  - **Accent:** the logo's gold replaces teal in the KDE colour scheme, the
    boot menu, the installer and the Control Plane dashboard.
- **64-bit only is final (ADR-0014):** Boswas OS v1 runs x86_64 Windows
  applications only. Every 32-bit refusal says "This application requires
  32-bit Windows compatibility, which is not supported by Boswas OS." The
  build fails if `wine32` or i386 multiarch appear.
- **Agent messages (never deployed in M1):**
  - the heartbeat is now liveness only (`heartbeat-v2`);
  - its former content is the status report (`status-report-v1`);
  - typed commands (`device-command-v2`) replace the draft command set.
- **`/etc/boswas/device.conf`:** now configuration only; the agent keeps its
  state in `/var/lib/boswas/agent`. `boswas device status`, `boswas info`
  and the management checks read the agent's identity and status.
- **`boswas-winapp` and the agent binaries** ignore the unit-test
  environment hooks (`BOSWAS_SYSROOT`), so a user cannot point them at a
  policy of their own.
- **Image:** `boswas-os` depends on `boswas-device-agent` and
  `boswas-compat-manager`. The builder image gains PySide6, Qt's offscreen
  platform and openssl.

### Known limitations
- **No Boswas ID:** operators use local API tokens; devices use
  certificates.
- **Control Plane scope:** single tenant, one policy per device (no group
  hierarchy), SQLite, no HA.
- **Device certificates** last 365 days; renewal means re-enrollment.
  The device key is not TPM-backed yet.
- **`UPDATE_AGENT`** needs a Boswas APT repository (M7) to deliver new
  versions.
- **Remote application commands** go to the active user's session only.

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
- **Security review of WinCompat:** seven findings, all fixed with regression
  tests (docs/compatibility/README.md, "Security review"). The most severe
  was a planted symlink that made the installer clean-up delete files in the
  user's home.

### Changed
- **`test.sh`:** a stage that exits non-zero without recording a failure
  (a crashed test script) is now recorded as a FAIL instead of passing
  silently.
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
