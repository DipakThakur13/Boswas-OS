# Implementation roadmap

| Phase | Scope | Status |
|-------|-------|--------|
| **0 Repository bootstrap** | Repository layout, docs, build tooling, licensing model | **Done** |
| **1 Debian live-build** | trixie configuration, KDE, package manifest, first ISO | **Done** |
| **2 Branding** | Theme, wallpaper, boot menu, installer, login/lock, About, `boswas-info`/`boswas-status` | **Done** (installed-system boot splash pending, see below) |
| **3 Security baseline** | AppArmor, nftables, audit, LUKS2 install policy, sudo/pwquality, sysctl, journald | **Baseline done**; USB enforcement, Boswas AppArmor profiles, systemd unit hardening pending |
| 4 Boswas agent | `boswas-device-agent.service`, device identity, inventory, heartbeat, local policy | Planned. `/etc/boswas/device.conf` contract in place |
| 5 Application management | Package policy (`/etc/boswas/app-policy/`), Boswas Store client, `boswas app` | Planned |
| 6 Windows compatibility | `boswas-winapp`, per-app prefixes, manifests, i386/WoW64 decision | Planned. Wine 10 (64-bit) shipped |
| 7 Central control | Control Plane API client (mTLS), Boswas ID (OIDC) interfaces, update channels | Planned |
| 8 Enterprise deployment | Hardware profiles, enrollment, centralised policy, controlled updates | Planned |

## Open items carried from Phases 2–3

- Installed-system boot branding:
  - a Plymouth theme, which also gives a graphical LUKS passphrase prompt
  - a GRUB background on `/boot`
- USB storage enforcement: udev authorisation plus udisks2 polkit rules,
  driven by `/etc/boswas/usb-policy/policy.conf`.
- Boswas AppArmor profiles for high-risk applications (Wine, browsers).
- Pin the build to a `snapshot.debian.org` timestamp and a builder image
  digest, for bit-for-bit reproducible releases.
- Sign release artifacts (ISO checksum signature) once the release signing
  key custody is defined.
- Decide whether the live session keeps live-config's passwordless sudo
  (convenient for diagnostics, but too permissive for a production image).
- Original Boswas Group font file, for UI-wide typography.

## Decisions to freeze before Phase 4 (from the blueprint)

- **Supported hardware:** the first Hardware Compatibility List; whether BYOD
  is prohibited.
- **Identity:** Boswas ID OIDC endpoints and the Linux login strategy.
- **Control plane:** hosting, database, HA, secrets management and
  signing-key custody.
- **First Windows apps:** the first 10–20 business-critical Windows
  applications.
- **Data policy:** backup, retention, remote-wipe boundaries and employee
  privacy.
- **Emergency recovery:** a procedure that does not weaken normal controls.
