# Implementation roadmap

## Foundation (v1 alpha, 1.0~alpha1)

| Phase | Scope | Status |
|-------|-------|--------|
| **0 Repository bootstrap** | Repository layout, docs, build tooling, licensing model | **Done** |
| **1 Debian live-build** | trixie configuration, KDE, package manifest, first ISO | **Done** |
| **2 Branding** | Theme, wallpaper, boot menu, installer, login/lock, About, `boswas-info`/`boswas-status` | **Done** (installed-system boot splash pending, see below) |
| **3 Security baseline** | AppArmor, nftables, audit, LUKS2 install policy, sudo/pwquality, sysctl, journald | **Baseline done**; USB enforcement and systemd unit hardening pending |

## Enterprise platform (ADR-0016)

A milestone is complete only when code, tests, documentation, a security
review and an integration test all exist.

| Milestone | Scope | Status |
|-----------|-------|--------|
| **M1 Windows/Wine platform** | `boswas-compat`: `boswas-winapp` (install, remove, list, launch, status, repair, logs), per-application prefixes, bubblewrap sandbox, `boswas-winapp` AppArmor profile, manifest format v1, layered catalog, WinCompat policy, "Run with Boswas" handler; device agent **interfaces** (models, schemas, identity, privacy guard, Windows inventory collector) | **Implemented in 1.0~alpha2** |
| **M2 Device agent** | `boswas-device-agent`: persistent device identity, `device.conf` configuration and validation, allowlisted inventory, device and application states, `boswas-device-agent.service` (sandboxed), local management API, per-user session agent, mutual-TLS client, enrollment, heartbeat, offline outbox, typed commands, `boswas-device` | **Implemented in 1.0~alpha3** |
| **M3 Control Plane foundation** | `control-plane/` modular monolith (**Python stdlib + SQLite**, ADR-0017): device registry, enrollment with one-time tokens and a device CA, heartbeat, inventory, typed command system with lifecycle, application catalog and installer artifacts, hash-chained audit trail | **Implemented in 1.0~alpha3** (no Docker Compose: a Debian package and `boswas-cp`) |
| **M4 Policy engine** | Signed, versioned policies (Ed25519), device-side verification and anti-rollback, managed WinCompat policy layer, policy-limited commands | **Core implemented in 1.0~alpha3**. One policy per device; the global → group → profile → override hierarchy is open |
| **M5 Fleet management** | Admin dashboard, remote application management | **Dashboard implemented in 1.0~alpha3**. Organisation/department/group model open |
| **Compatibility Manager** | `boswas-compat-manager` (PySide6): dashboard, library, install with 32-bit refusal, details, permissions (display-only), logs, repair, update, remove, system status | **Implemented in 1.0~alpha3** |
| M6 Boswas Store | `boswas-store` client and catalog API; Windows apps through WinCompat manifests | Planned |
| M7 Update infrastructure | Debian snapshot pinning, dev → qa → stable promotion, staged updates, rollback | Planned |
| M8 Hardware certification | `boswas hardware test`, `boswas-hardware-profile`, certification levels | Planned |
| M9 Production signing/release | Key custody, signed artifacts, SBOM, release channels | Planned |

Boswas ID is intentionally deferred (ADR-0015); only interfaces exist.

## Decided

- **64-bit Windows applications only (ADR-0014, final for v1).** There is no
  `wine32`, i386 multiarch or WoW64, and 32-bit software is refused everywhere
  with one message. This is a product limitation, not an open item.
- **Boswas ID stays deferred (ADR-0015).** Only interfaces exist.

## Open items

- **First validated applications.** The system catalog is empty until the
  first 10–20 business applications are validated and their manifests pinned.
- **Production WinCompat policy.** The Control Plane's `default` policy
  already uses `UNLISTED_APPS=deny` and `approved tested`. The shipped local
  policy stays permissive for unmanaged alpha devices.
- **Direct `/usr/bin/wine` use.** Not mediated.
- **Runtime components.** Boswas-packaged dependencies (VC++ runtimes and
  similar) need licensing review before manifests may request them.
- **Control Plane:**
  - policy hierarchy (group, profile, override) and an organisation model;
  - high availability, and PostgreSQL behind `store.Store`;
  - certificate renewal before the 365-day device certificates expire (today:
    re-enrollment);
  - an external anchor for the audit hash chain.
- **Device agent:**
  - TPM-backed credential store;
  - `UPDATE_AGENT` needs a Boswas APT repository (M7) to be useful;
  - application commands go to the active user only (no per-user targeting).

## Open items carried from Phases 2–3

- Installed-system boot branding:
  - a Plymouth theme, which also gives a graphical LUKS passphrase prompt
  - a GRUB background on `/boot`
- USB storage enforcement: udev authorisation plus udisks2 polkit rules,
  driven by `/etc/boswas/usb-policy/policy.conf`.
- Boswas AppArmor profiles for other high-risk applications (browsers). The
  Wine profile exists since M1.
- Pin the build to a `snapshot.debian.org` timestamp and a builder image
  digest, for bit-for-bit reproducible releases (M7).
- Sign release artifacts once the release signing key custody is defined (M9).
- Decide whether the live session keeps live-config's passwordless sudo
  (convenient for diagnostics, but too permissive for a production image).
- Original Boswas Group font file, for UI-wide typography.

## Decisions still to freeze

- **Supported hardware:** the first Hardware Compatibility List; whether BYOD
  is prohibited.
- **Control Plane operations:** hosting, HA, backups of
  `/var/lib/boswas-control-plane`, and custody of the device CA and policy
  signing keys. They are files on the server in 1.0~alpha3.
- **Windows applications:** the first validated 64-bit applications.
- **Data policy:** backup, retention, remote-wipe boundaries and employee
  privacy.
- **Emergency recovery:** a procedure that does not weaken normal controls.
