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
| **M1 Windows/Wine platform** | `boswas-compat`: `boswas-winapp` (install, remove, list, launch, status, repair, logs), per-application prefixes, bubblewrap sandbox, `boswas-winapp` AppArmor profile, manifest format v1, layered catalog, WinCompat policy, "Run with Boswas" handler; device agent **interfaces** (models, schemas, identity, privacy guard, Windows inventory collector) | **Implemented in 1.0~alpha2**. Open items below |
| M2 Device agent | `boswas-device-agent` package and `boswas-device-agent.service`: device ID at first boot, local inventory/compliance, mutual-TLS client, heartbeat, credential store | Planned. Interfaces from M1 |
| M3 Control Plane foundation | `control-plane/` modular monolith (TypeScript, NestJS, PostgreSQL), device API, enrollment, audit events, Docker Compose dev deployment | Planned |
| M4 Policy engine | Signed, versioned policies (global → group → profile → audited override), device-side verification, `boswas policy status` states, manages `/etc/boswas/compat/policy.conf` | Planned |
| M5 Fleet management | Organisation/department/group/profile model, admin dashboard | Planned |
| M6 Boswas Store | `boswas-store` client and catalog API; Windows apps through WinCompat manifests | Planned |
| M7 Update infrastructure | Debian snapshot pinning, dev → qa → stable promotion, staged updates, rollback | Planned |
| M8 Hardware certification | `boswas hardware test`, `boswas-hardware-profile`, certification levels | Planned |
| M9 Production signing/release | Key custody, signed artifacts, SBOM, release channels | Planned |

Boswas ID is intentionally deferred (ADR-0015); only interfaces exist.

## Open items carried from Milestone 1

- **32-bit Windows applications (decision needed, ADR-0014).** Enable i386
  multiarch with `wine32`, or package a WoW64 Wine. Most installers are
  32-bit, so this limits which applications can be installed today.
- **First validated applications.** The system catalog is empty until the
  first 10–20 business applications are validated and their manifests pinned.
- **Production WinCompat policy.** `UNLISTED_APPS=deny` and
  `ALLOWED_STATUSES="approved tested"` for employee devices, delivered by the
  policy engine (M4).
- **Direct `/usr/bin/wine` use.** Not mediated today; restricting it is an
  M4 policy decision.
- **Boswas Compatibility Manager GUI.** A KDE front end over
  `boswas-winapp --json`.
- **Runtime components.** Boswas-packaged dependencies (VC++ runtimes and
  similar) need licensing review before manifests may request them.

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

## Decisions to freeze before Milestone 2

- **Supported hardware:** the first Hardware Compatibility List; whether BYOD
  is prohibited.
- **Control plane:** hosting, database, HA, secrets management and
  signing-key custody.
- **Windows runtime:** 32-bit support (ADR-0014) and the first Windows apps.
- **Data policy:** backup, retention, remote-wipe boundaries and employee
  privacy.
- **Emergency recovery:** a procedure that does not weaken normal controls.
