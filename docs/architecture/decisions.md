# Architecture decision records

Short records of decisions taken while building Boswas OS v1. Each one names
the decision, why it was taken, and what would make us revisit it.

## ADR-0001 Debian 13 "trixie" is upstream, consumed, not forked

**Decision.** Boswas OS is built from Debian 13 packages pulled from the Debian
archive at build time. The repository contains only Boswas-specific
packaging, configuration, artwork, scripts, tests and documentation. No
Debian source package is vendored or modified. `debian/patches/` is not
created until a patch is actually needed.

**Why.** Security fixes come from Debian's security team, and the licensing
and attribution of every component stay intact. A fork would make Boswas
Group responsible for maintaining thousands of packages.

## ADR-0002 Boswas customisation ships as Debian packages

**Decision.** Everything Boswas adds to a device lives in native Debian
packages built from `packages/` (seven since 1.0~alpha3):

- `boswas-os`: release identity and `/etc/boswas`; depends on the others.
- `boswas-cli`: `boswas`, `boswas-info`, `boswas-status`.
- `boswas-branding`: KDE look-and-feel, wallpaper, login, About and icons.
- `boswas-security`: the security baseline.
- `boswas-compat`: WinCompat (`boswas-winapp`, the Wine AppArmor profile,
  the compatibility catalog and its policy); since 1.0~alpha2.
- `boswas-device-agent`: the device agent, the session agent and
  `boswas-device`; since 1.0~alpha3.
- `boswas-compat-manager`: the Compatibility Manager GUI; since 1.0~alpha3.

The packages reference shared sources in the monorepo (`security/`,
`desktop/`, `compatibility/`, `config/boswas/`). `debian/rules` installs
every file with an explicit mode. The Control Plane server package
(`control-plane/`, `boswas-control-plane`) is built alongside them but is
never part of the device image.

**Why.** Installed devices can then be updated through APT like the rest of
the system, which a one-off image customisation cannot do. Explicit modes
also make the build independent of the checkout's filesystem (Windows
checkouts report every file as 0777).

## ADR-0003 live-build image, Debian Installer in live mode

**Decision.**

- The ISO is built with Debian `live-build` (trixie: 1:20250505).
- **Recommends:** APT Recommends are installed, matching Debian's own desktop
  images, and specific unwanted packages are pinned out (ADR-0004).
- **Firmware:** selected explicitly rather than live-build's "all firmware" mode.
- **Bootloaders:** GRUB for both BIOS and UEFI, one branded menu.
- **Secure Boot:** `--uefi-secure-boot auto` uses Debian's signed shim, GRUB
  and kernel.
- **Installer:** the Debian Installer runs in *live* mode, copying the
  tested image to disk.

**Why.** These are upstream Debian mechanisms. Installing the exact image
that was tested makes devices predictable.

**Calamares** was not chosen: it would add a second installer to maintain.
The Debian Installer's preseeding is declarative and already supports guided
LUKS.

## ADR-0004 Explicit manifest with build-time exclusions

**Decision.** The top-level packages are listed by purpose in
`config/live-build/config/package-lists/`, about 90 entries. Packages
deliberately kept out are pinned to priority -1 in
`config/archives/boswas-exclude.pref.chroot`, each with its reason:

- KDE Connect
- Plasma Discover and PackageKit
- fingerprint PAM
- openssh-server
- cups-browsed
- the KDE Welcome Center

The full resolved package list of every build is recorded in its manifest.

**Why.** "Install Recommends, but never these" is reviewable and fails loudly:
if a hard dependency ever needs an excluded package, the build fails instead
of silently shipping it. The pins are build-time only; live-build removes
them from the image.

## ADR-0005 Boswas OS is the user-facing identity; Debian stays the technical base

*Revised in 1.0~alpha3.* This decision first kept Debian's `/etc/os-release`
(`ID=debian`) and showed Boswas only on Boswas surfaces. Boswas Group has
since decided that users must experience the system as Boswas OS, with no
unnecessary Debian branding, while the technical base stays intact.

**Decision.** User-facing identity is Boswas OS; technical, legal and
package-management identifiers stay Debian's.

- **os-release:** `boswas-os` diverts base-files' `/usr/lib/os-release` and
  ships `NAME="Boswas OS"`, `PRETTY_NAME="Boswas OS v1 Alpha"`, `ID=boswas`,
  `ID_LIKE=debian`, `VERSION_CODENAME=trixie`, `LOGO=boswas-logo`. Live
  images get the same content in their `/etc/os-release` copy (live-build
  writes one at bootstrap; the `boswas-os` postinst refreshes it).
- **Console:** `/etc/issue` and `/etc/issue.net` are base-files conffiles,
  which Debian Policy forbids diverting. The `boswas-os` postinst replaces
  their text, and `/etc/motd`'s (no package owns it), only while it is
  Debian's unmodified default. The motd keeps its legal notice in Boswas
  wording. The originals are kept in `/var/lib/boswas/os/` and restored on
  removal. An administrator's own banner is never touched.
- **Boot medium:** `.disk/info` names Boswas OS (binary hook); the boot menu,
  boot splash, login, lock and About page show only Boswas OS.
- **Live session:** the live user is "Boswas OS Live".
- **Kept as Debian, on purpose:**
  - `/etc/debian_version`, the package archive (`deb.debian.org`),
    `ID_LIKE=debian` and every package name;
  - `GRUB_DISTRIBUTOR="Debian"` on installed systems (pinned in
    `/etc/default/grub.d/10-boswas.cfg`), because Debian's signed GRUB looks
    for its configuration in `/EFI/debian`. Renaming the EFI directory would
    break Secure Boot. The GRUB menu is hidden (Esc or Shift shows it), so
    users do not normally see its "Debian GNU/Linux" entries;
  - the Debian Installer's own text, which cannot be changed without
    rebuilding it (its banner is Boswas OS);
  - copyright files and licence notices (`/usr/share/doc/*/copyright`) and
    technical details (`boswas-info` "Package base", About > Technical
    details).

**Why.**

- **Product identity:** users see one product, Boswas OS.
- **Compatibility:** `ID_LIKE=debian` and the unchanged codename satisfy
  tools that look for a Debian-like system; the package archive and
  `debian_version` keep apt, unattended-upgrades and dpkg unchanged.
- **Honesty and licences:** attribution stays where licences and
  maintainers need it, without branding the user interface.

**Revisit when** Boswas operates its own archive (M7): the codename and
archive identity can then become Boswas-owned as well.

## ADR-0006 Security baseline as drop-ins, desktop-safe hardening

**Decision.** `boswas-security` only adds drop-in files and never modifies a
file owned by another package:

- **Firewall:** a `nftables.service` drop-in loads
  `/etc/boswas/firewall/nftables.conf`. Debian's `/etc/nftables.conf` is
  left alone, because Debian Policy forbids diverting another package's
  conffile.
- **Kernel hardening:** in `/usr/lib/sysctl.d`.
- **Audit rules:** in `/etc/audit/rules.d`.
- **Other settings:** the `sudoers.d`, `pwquality.conf.d`, `sshd_config.d`
  and `apt.conf.d` drop-in directories.

- **Service enable policy:** a systemd preset,
  `/usr/lib/systemd/system-preset/80-boswas.preset`, enables `nftables` and
  keeps a freshly installed `openssh-server` disabled.
  - **Why a preset:** Debian 13's `deb-systemd-helper` enables new units via
    `systemctl preset` and deliberately never re-enables a unit that its
    package installed disabled.
  - **How it was found:** the build's verify hook caught exactly this:
    `nftables.service` was left disabled by a `deb-systemd-helper enable`
    call.
  - **How it applies:** the postinst applies the preset to `nftables` once,
    on first installation.

Debian's `hardening-runtime` package was evaluated and not used. It sets
`user.max_user_namespaces=0`, which breaks bubblewrap/Flatpak and the
Firefox sandbox, and adds `nosmt`/`slub_debug` (a performance cost) to the
kernel command line.

## ADR-0007 KDE defaults through an XDG configuration directory

**Decision.** Boswas KDE defaults are installed in
`/usr/share/boswas/kde-settings`:

- `kdeglobals`, which selects the Boswas look-and-feel and colour scheme
- `kscreenlockerrc`, the enforced screen lock
- `kcm-about-distrorc`

An env script appends that directory to `XDG_CONFIG_DIRS` for Plasma
sessions. This is the mechanism Debian's `desktop-base` uses.

**Why.** `desktop-base` owns `/etc/xdg/kcm-about-distrorc`, so shipping that
path would conflict. Appending also keeps `/etc/xdg` (local admin) and user
settings at higher precedence, while Kiosk `[$i]` keys still lock the
screen-lock policy.

## ADR-0008 Boswas OS brand: the "b" mark and the Orbitron typeface

*Revised in 1.0~alpha3.* The first version of this decision traced the Boswas
Group gear logo and its lettering from images, because no font file existed.
Boswas Group has since supplied the Boswas OS logo and chosen Orbitron as the
brand typeface, so this decision replaces that one.

**Decision.**

- **Logo:** the Boswas OS mark, a "b" monogram with a gold stem and dot and a
  silver ring and swoosh (`desktop/branding/source/boswas-os-logo.png`). It
  replaces the Boswas Group gear everywhere. Only a raster original exists,
  so it is vectorised with potrace (`desktop/branding/tools/trace_mark.py`):
  - colour by colour;
  - with its gold and silver shading fitted as linear gradients.
- **Brand typeface:** Orbitron (SIL OFL 1.1).
  - **Wordmarks:** set in Orbitron SemiBold with 0.12 em tracking and
    converted to outlines (`tools/build_wordmarks.py`), so Qt, GRUB and the
    installer render them without the font.
  - **Packaging:** Debian does not package Orbitron, so `boswas-branding`
    installs the six static weights unmodified, with the licence.
- **Text face:** Lato for secondary text in artwork; the KDE default for the
  desktop UI. Orbitron is a display face and is not used for running text.
- **Accent:** the logo's gold (`#D9B26E`) replaces teal in the colour
  scheme, the boot menu, the installer, the Compatibility Manager icon and
  the Control Plane dashboard.

**Revisit when** a designer's vector original of the mark exists. It then
replaces the traced SVG.

## ADR-0009 Reproducible, containerised builds

**Decision.** `build.sh` runs natively on Debian 13 as root, or re-executes
itself in `build/container/Containerfile` (a `debian:trixie` image) on any
other host. The container is privileged, because live-build mounts `/proc`,
`/sys` and `/dev` inside its chroot.

Every build records:

- the Debian suite and the resolved package list
- the git commit and whether the tree was dirty
- a configuration hash
- the live-build and debootstrap versions
- the builder image

`SOURCE_DATE_EPOCH` follows the commit time for clean, committed trees.

**Not yet.** Bit-for-bit reproducibility additionally needs the package
inputs pinned to a `snapshot.debian.org` timestamp. That is planned for the
release pipeline (Phase 4), together with a pinned builder image digest.

## ADR-0010 Python standard library for the CLI

**Decision.** `boswas` is Python 3 (Debian's 3.13), standard library only.

- Code is installed in `/usr/lib/boswas/python`.
- It runs with `python3 -IB`: isolated, with no bytecode written into `/usr`.
- `--json` emits documents with a versioned schema (`boswas-cli/1`).

**Why.** It needs no extra dependencies, is easy to audit, and gives
structured output for the future agent and control plane.

## ADR-0011 WinCompat prefixes live in the user's home, seen at /var/lib/boswas/wine inside the sandbox

**Decision.**

- **On the host:** each Windows application gets its own Wine prefix in
  `~/.local/share/boswas/wine/<id>/sandbox/prefix`, owned by the user who
  installed it.
- **Inside the application's sandbox:** that state appears as
  `/var/lib/boswas/wine/<id>/`. The AppArmor profile only grants that view.

**Why.**

- **Wine requirement:** Wine refuses a prefix not owned by the user who runs
  it, and Windows software must never run as root.
- **No new attack surface:** a host-wide `/var/lib/boswas/wine` would need a
  privileged helper or a world-writable directory.
- **Per-user isolation:** per-user storage keeps users' applications apart.
- **Stable confinement:** the fixed sandbox path keeps the AppArmor profile
  independent of home-directory locations, and the profile can deny all
  home directories outright.

**Revisit when** shared, machine-wide Windows applications are required.
That needs a dedicated service account per application and a reviewed
display-sharing design.

## ADR-0012 Two isolation layers for Windows applications: bubblewrap and AppArmor

**Decision.**

- **bubblewrap (per application):** every Windows process runs inside a
  sandbox that decides what exists for the application, from its manifest
  and the device policy.
- **AppArmor (upper bound):** the `boswas-winapp` profile, attached by path
  to the in-sandbox runner, sets the upper bound for every application.
- **Fail closed:** the runner refuses to start Wine unless its own label is
  `boswas-winapp (enforce)`.

**Why.**

- **Wine is not a sandbox:** Windows programs are arbitrary native code.
- **bubblewrap:** removes what an application must not see (home, D-Bus,
  network, other prefixes).
- **AppArmor:** limits what it may do with what it sees (no exec of dropped
  binaries, no mounts or capabilities, read-only system), even if a sandbox
  grant is wrong.

**Alternatives.**

- **Flatpak-packaged Wine:** a second runtime stack outside Debian's
  security support.
- **A named profile entered with aa-exec:** fragile if a caller forgets the
  transition.
- **A system-wide profile on /usr/lib/wine/wine64:** would also confine
  unrelated use.

**Known limits.** X11 clients can observe each other (limited to Xwayland
clients under Plasma Wayland). Direct `/usr/bin/wine` use is not mediated;
restricting it remains an open item (roadmap).

## ADR-0013 Compatibility manifest format v1 and a layered catalog

**Decision.**

- **Format:** applications are described by JSON manifests, normatively
  defined by `compatibility/manifests/schema/manifest-v1.schema.json` with a
  stdlib validator kept in sync by a unit test.
- **Statuses:** `unknown`, `untested`, `experimental`, `tested`, `approved`,
  `blocked`.
- **Pinning:** `tested` and `approved` must pin the installer SHA-256.
- **Layers:** the catalog has three layers, `managed` (written by the
  device agent from the Control Plane catalog, since 1.0~alpha3), `local`
  (administrator) and `system` (package). The highest wins, except that
  `blocked` in any layer wins.
- **Recomputation:** sandbox grants are recomputed from the catalog and
  policy at every launch.
- **Replaced format:** the earlier planned statuses (`gold`, `silver`, ...)
  are replaced.

**Why.**

- A validated status must refer to exactly one installer.
- Blocks must not be undone by a lower-trust layer.
- Nothing an application writes can widen its own sandbox.

## ADR-0014 Boswas OS v1 runs 64-bit Windows applications only (final)

**Decision (final for v1, confirmed with 1.0~alpha3).**

- **Runtime:** WinCompat ships Debian's `wine64` only. There is no `wine32`,
  no i386 multiarch, no WoW64 Wine and no 32-bit fallback of any kind.
- **Refusal, everywhere, with one message:** 32-bit Windows software is
  refused before anything is created or run, with *"This application
  requires 32-bit Windows compatibility, which is not supported by Boswas
  OS."*:
  - `boswas-winapp install` and `inspect` read the PE header (and the
    Template property of .msi packages);
  - the Compatibility Manager shows the same sentence;
  - the device agent refuses 32-bit catalog entries before downloading;
  - the Control Plane marks them `UNSUPPORTED_ARCHITECTURE` and never turns
    them into install commands.
- **Not a TODO:** the documentation, the GUI and the CLI never suggest
  installing `wine32` or enabling i386. Guard rails fail the build if
  `wine32` or a foreign dpkg architecture ever appears in the image.

**Why.**

- **Attack surface:** i386 multiarch adds roughly 300 MB and a second copy
  of the library stack to maintain and patch.
- **Product scope:** v1 targets the 64-bit business applications Boswas
  validates. Many installers are 32-bit stubs, and the catalog accepts only
  64-bit installers.

**Revisit** only as a new product decision for a later major release.

## ADR-0015 Device identity is separate from user identity; Boswas ID deferred

**Decision.**

- **Device identity:** a random UUID v4 plus, after enrollment, a device
  certificate whose private key stays in the agent's credential store.
- **User identity:** not implemented. Interfaces only (`IdentityProvider`,
  `IdentityContext`, `AuthenticatedPrincipal`), with a `NoIdentityProvider`.
- **Never used for authentication:** the hostname, MAC or IP addresses,
  user names and serial numbers.
- **Closed messages:** device messages have closed field allowlists
  (`privacy.py`).

**Why.**

- Devices must be manageable before and without Boswas ID.
- Mixing the two identities would make device trust depend on a user
  session.
- Closed allowlists turn "no invasive monitoring" into a tested property
  instead of a promise.

## ADR-0016 Enterprise platform delivered in milestones

**Decision.** The enterprise platform is delivered in nine milestones (see
`roadmap.md`), each with code, tests, documentation and a security review
before it counts as complete:

1. Windows/Wine platform (this release)
2. Device agent
3. Control Plane foundation
4. Policy engine
5. Fleet management
6. Boswas Store
7. Update infrastructure
8. Hardware certification
9. Production signing and release

The Control Plane is a modular monolith, not microservices. (The stack named
here originally, TypeScript/NestJS with PostgreSQL, was replaced by ADR-0017.)

**Why.** Each milestone depends on the contracts of the previous ones:
WinCompat inventory feeds the agent, the agent feeds the Control Plane, and
signed policy needs both.

1.0~alpha3 delivers the device agent (M2), a Control Plane covering the
foundation, signed policy and fleet scope of M3–M5 (single tenant, no
organisation hierarchy), and the Compatibility Manager in one release.

## ADR-0017 Control Plane in the Python standard library with SQLite

**Decision.**

- **Language and dependencies:** the Control Plane (`control-plane/`,
  package `boswas-control-plane`) is written for Python 3 with the standard
  library only, like every other Boswas component (ADR-0010).
- **Storage:** SQLite with WAL, behind one repository class (`store.Store`),
  so PostgreSQL can replace it without touching the domain code.
- **Cryptography:** the `openssl` command, for the device CA, CSRs and
  Ed25519 policy signatures. TLS uses Python's `ssl` module.
- **Shared code:** the device agent's protocol modules (typed commands,
  signed policies, privacy allowlists) and boswas-compat's manifest
  validator. The Control Plane installs them in its own module directory.
- **Packaging:** a server package, built by `build-packages.sh` into
  `server/`, never part of the device image.
- **Two listeners with disjoint APIs:** the device port (8443) asks for a
  client certificate and serves enrollment and the device API; the operator
  port (9443) never asks for one and serves the dashboard and the operator
  API.

**Why.**

- **One language and audit model:** no third-party dependency tree.
- **Two ports:** a TLS server that requests a client certificate makes
  browsers show a certificate picker (Python's `ssl` cannot name the
  acceptable CAs), so the dashboard needs a port without that request.
  Separate ports also let the firewall keep the operator API on the
  administration network while devices reach the device port.
- **Debian support:** security fixes come from Debian's `python3` and
  `openssl`.
- **One codebase for one contract:** the device and the server check
  commands and policies with the same code.
- **Testable everywhere:** it runs inside `test.sh` and against the QEMU VM.

**Trade-off.** Python's HTTP server is not a high-throughput web server. It
is sized for a single company's fleet. A reverse proxy in front of it must
pass client certificates through (TLS terminates in the Control Plane).

**Revisit when** the fleet outgrows one SQLite file or one process. The
repository interface is the seam for that change.

## ADR-0018 Device agent: a root service and a per-user session agent

**Decision.**

- **`boswas-device-agent.service` (root, sandboxed):** the device's
  identity, configuration, state, inventory, posture and Control Plane
  conversation, and the local management API (`/run/boswas-agent/agent.sock`).
- **`boswas-session-agent.service` (systemd user unit, one per logged-in
  user):**
  - the backend of the Compatibility Manager
    (`$XDG_RUNTIME_DIR/boswas/session.sock`, owner only);
  - the hand of the device agent for remote application commands.
  - It runs `boswas-winapp` as that user.
- **Ownership of state:**

  | Owner | State |
  |-------|-------|
  | boswas-compat (`boswas-winapp`) | Application runtime state: records, prefixes, logs, locks |
  | Device agent | Device state, identity and local inventory |
  | Control Plane | Fleet state: registry, commands, policies, catalog, audit |
  | GUI | Presentation state only |

- **Agent state location:** the agent never writes `/etc/boswas/device.conf`
  (an administrator's conffile). Its state lives in
  `/var/lib/boswas/agent`; managed WinCompat data lives in
  `/var/lib/boswas/compat`.

**Why.**

- **Never as root:** Windows applications belong to a user and never run as
  root (ADR-0011), so the root agent cannot run them itself.
- **One path for everything:** routing every operation through the same
  `boswas-winapp` keeps policy, manifests, bubblewrap and AppArmor in one
  place.
- **Clean upgrades:** keeping agent state out of conffiles avoids dpkg
  conffile prompts on upgrades.

## ADR-0019 Typed management commands only; no remote shell

**Decision.**

- **A closed set of types:** remote management uses `INSTALL_APPLICATION`,
  `UPDATE_APPLICATION`, `REMOVE_APPLICATION`, `LAUNCH_APPLICATION`,
  `STOP_APPLICATION`, `REPAIR_APPLICATION`, `REFRESH_INVENTORY`,
  `APPLY_POLICY` and `UPDATE_AGENT`.
- **What every command carries:**
  - a validated payload;
  - a target device;
  - an expiry of at most 7 days;
  - an opaque actor reference;
  - an audit trail.
- **Payloads come from the catalog:** the Control Plane builds install
  payloads itself; an operator names an application, never a manifest,
  path or program.
- **The device decides again:** it re-validates every command with the same
  code, refuses unknown types and expired commands, records executed
  command IDs (idempotency), and checks the applied policy's
  `allowed_commands`.
- **No escape hatches:** no type carries a shell command, script, program
  path or code. Destructive device operations (wipe, factory reset) do not
  exist. `UPDATE_AGENT` installs a named package version from the device's
  signed APT sources, and only when the device and its policy both allow
  agent updates.

**Why.** The Control Plane manages an OS platform; it is not a remote shell.
A compromised Control Plane can then do no more than these operations, and
the device still enforces its own policy.

## ADR-0020 Signed policies and managed layers

**Decision.**

- **Signed, versioned policies:** the Control Plane signs every version of a
  device policy (`boswas-policy/1`) with an Ed25519 key. Devices pin the
  public key at enrollment.
- **What a device checks before applying:**
  1. the signature;
  2. a strict, closed document schema;
  3. that the policy is not a rollback (sequence and issue time);
  4. that the rendered WinCompat policy parses in boswas-compat without a
     single problem.

  Anything else is rejected and the current policy stays.
- **Policies cannot weaken confinement:** `require_apparmor` must be true.
- **Managed layers:**
  - the agent writes the WinCompat policy as
    `/var/lib/boswas/compat/policy.conf`, which boswas-compat then uses
    instead of the local `/etc` file;
  - catalog manifests from the Control Plane go to the existing managed
    catalog layer;
  - unenrolling removes both, so the local policy applies again.
- **Policy keys:** `BLOCKED_APPLICATIONS` and `ALLOWED_APPLICATIONS` (by
  application ID) join the WinCompat policy keys.

**Why.** A valid local security policy must never be replaced by malformed
or unverified remote data, and an untrusted network position must not be
able to install a policy.

## ADR-0021 The Compatibility Manager is a client of the local API

**Decision.**

- **Toolkit:** the Compatibility Manager (`boswas-compat-manager`) is a
  PySide6 (Qt Widgets, LGPL) application.
- **What it talks to:** the user's session agent for application operations,
  and read-only device-agent operations for status.
- **What it never does:** start processes, touch prefixes, manifests,
  AppArmor or bubblewrap, or edit permissions.
- **Default handler unchanged:** "Run with Boswas" stays the default handler
  for Windows executables. The Manager adds a Dolphin service menu and its
  own install flow.

**Why.**

- **No duplicated security logic:** none of it exists in the GUI.
- **Least privilege:** the GUI runs as the user with no privileges at all.
- **Testable without a desktop:** the UI is tested against a fake backend,
  and the backend is tested without a GUI.
