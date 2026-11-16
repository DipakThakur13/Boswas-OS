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
packages built from `packages/` (seven in 1.0~alpha3; added since:
`boswas-control-center`, `boswas-sysops` (ADR-0024), `boswas-android`
(ADR-0025), `boswas-desktop` (ADR-0026), `boswas-bootsplash`
(ADR-0029) and `boswas-apps` (ADR-0040)):

- `boswas-os`: release identity and `/etc/boswas`; depends on the others.
- `boswas-cli`: `boswas`, `boswas-info`, `boswas-status`.
- `boswas-branding`: artwork, design tokens, colour schemes, presets,
  icons and About (until ADR-0026 also the KDE look-and-feel and the login
  theme; since ADR-0039 also the Qt toolkit `boswas_fluent`).
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

*Revised with the native desktop (ADR-0026).* The mechanism stays, for KDE
applications only: `kdeglobals` now selects the Prism colour scheme and
icon pack (ADR-0031), `boswas-session` appends the directory to
`XDG_CONFIG_DIRS`, and `kscreenlockerrc` is gone, because the screen lock
is the compositor's (ADR-0028). The text below describes the Plasma-based
releases up to 1.0~alpha3.

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

*Since ADR-0031* the desktop UI uses the Prism palette with Inter as its
text face. The mark, the wordmarks and Orbitron are unchanged; gold stays a
brand colour and is no longer the UI accent.

*Since ADR-0033 and ADR-0038* no Boswas screen sets running text in
Orbitron any more: the clock of the lock and login screens and the text of
the boot screen are Inter, and the boot screen shows the mark without the
wordmark. Orbitron remains the typeface of the wordmarks (boot menu,
installer banner) and stays installed.

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
clients under Plasma Wayland; not applicable since ADR-0026, which has no
X server: Wine draws through its Wayland driver). Direct `/usr/bin/wine` use is not mediated;
restricting it remains an open item (roadmap).

*The parenthesis above is superseded by ADR-0034:* the session runs
Xwayland again, so the limit applies to the X11 clients of that server,
and a Windows application started with `DISPLAY` set is one of them.

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

> **Superseded by ADR-0022 (2026-11-12):** the product owner asked for 32-bit
> Windows application support; Boswas OS now ships `wine32:i386`. The text
> below is kept as the record of the earlier decision.

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
  PySide6 (Qt Widgets, LGPL) application (since ADR-0039 styled by the
  shared toolkit `boswas_fluent` instead of a style sheet of its own).
- **What it talks to:** the user's session agent for application operations,
  and read-only device-agent operations for status.
- **What it never does:** start processes, touch prefixes, manifests,
  AppArmor or bubblewrap, or edit permissions.
- **Default handler unchanged:** "Run with Boswas" stays the default handler
  for Windows executables. The Manager adds a Dolphin service menu and its
  own install flow. *(Since ADR-0042 an "Open with" entry takes the place
  of the service menu; the default handler is still unchanged.)*

**Why.**

- **No duplicated security logic:** none of it exists in the GUI.
- **Least privilege:** the GUI runs as the user with no privileges at all.
- **Testable without a desktop:** the UI is tested against a fake backend,
  and the backend is tested without a GUI.

## ADR-0022 64-bit and 32-bit Windows applications (wine32 from i386 multiarch)

Supersedes ADR-0014 (product owner request, 2026-11-12).

**Decision.**

- **Runtime:** Debian 13's Wine 10.0. `libwine:amd64` contains only
  `x86_64-windows` modules, so the 64-bit package cannot run 32-bit programs
  (no new-style WoW64 in this build). Boswas OS therefore installs `wine32`
  from the **i386** architecture (dpkg multiarch, enabled by live-build from
  the `wine32:i386` package-list entry). The operating system stays amd64:
  kernel, userland and every other package are 64-bit; i386 is the only
  foreign architecture and only Wine's 32-bit side and its libraries use it.
  The preloaders (`wine*-preloader`) are not installed.
- **One loader, one prefix:** the sandbox always starts `/usr/lib/wine/wine64`;
  for a 32-bit program Wine itself starts `/usr/lib/wine/wine`
  (`WINELOADER` without its "64" suffix) in the same win64 prefix
  (`C:\Windows\SysWOW64`). Per-application prefixes, bubblewrap and the
  `boswas-winapp` AppArmor profile are unchanged; the profile gains exactly
  one rule (`/usr/lib/wine/wine mrix`).
- **Architecture from the header only:** `boswas-winapp` reads the COFF
  machine field and cross-checks the optional-header magic (x86 = PE32,
  x86_64/arm64 = PE32+). Inconsistent headers, DLLs, non-executable images,
  scripts and text files are refused with the reason. The file name never
  matters.
- **When wine32 is missing** (an administrator removed it): x86 is reported
  unavailable at every start, and 32-bit software is refused with *"This
  application requires 32-bit Windows compatibility, which is not installed
  on this system."* plus the package name.
- **Fleet:** the Control Plane accepts x86 catalog entries whose installer
  really is x86; each device decides again from its own runtime before it
  downloads anything.

**Why.** Many business applications and installers are still 32-bit. Debian's
multiarch Wine is the packaged, security-supported way to run them on
Debian 13. Cost: about 1 GB more installed software (libwine:i386 alone is
623 MB) and a second library stack to keep updated. Debian security updates
cover it, and the build records the ISO size.

## ADR-0023 Installer firmware from the medium; installation needs no network

**Decision.**

- **Firmware pool:** `lb config --firmware-binary true`. The ISO carries
  every Debian firmware package under `/firmware` with `Contents-firmware`
  and DEP-11 modalias patterns, the same layout as Debian's official media.
  hw-detect loads missing firmware from it before the network is configured,
  and installs the packages it used on the target.
- **Required firmware:** `config/boswas/firmware-required.list` lists the
  firmware that must reach both the installer and the image: common
  Ethernet, Wi-Fi, Bluetooth, GPU and audio, including the reported
  `rtl_nic/rtl8125b-2.fw`. The build fails if any file cannot be loaded
  from the medium (`0150-boswas-firmware.hook.binary`) or is missing from
  the image (`0100-boswas-verify.hook.chroot`). The live image's own
  firmware list grows by firmware-cirrus, bnx2, bnx2x, libertas and zd1211.
- **No network during installation:** `apt-setup/use_mirror false` and no
  security/updates services. Every package is on the medium, and an
  unreachable mirror can no longer stop an installation.
  `/usr/lib/boswas/installer/finish-install` (late_command) writes
  signature-verified Debian sources (deb822, `Signed-By` the Debian archive
  keyring, including security and non-free-firmware) when none are
  configured. It also removes the live medium's unsigned pool and writes
  `/var/log/boswas/install-report.txt`.
- **Firmware index:** `/usr/share/boswas/firmware/index.tsv` (built from
  Debian's Contents indices) names the package for a firmware file a driver
  could not load (`boswas-sysops firmware-report`).
- **Known limitation (inherited from Debian):** the installer cannot load
  `firmware-sof-signed` (Intel SOF audio DSP). Its preinst calls
  `dpkg-maintscript-helper`, which Debian Installer lacks, so hw-detect
  discards the package. Audio is not needed to install, and the installed
  system has the package. The list marks these entries `# image-only`, and
  the installer test reports them as a limitation, not a pass.
- **Not done:** pruning irrelevant packages (switch ASICs, datacenter GPU
  firmware, about 95 MB) from the pool. live-build already generated the
  pool's APT index, and the saving is about 3 %.

**Why.** The installer is a separate system with its own kernel and initrd.
Without the pool it cannot satisfy a firmware request, and Ethernet firmware
cannot come from the internet that the same Ethernet is supposed to reach.
Root cause and evidence: `docs/reports/2026-11-12-baseline-audit.md`.

## ADR-0024 One narrowly scoped privileged helper (boswas-sysops)

**Decision.** System changes requested from the desktop go through
`pkexec /usr/libexec/boswas/sysops <operation> [arguments]` only:

- a fixed set of operations (APT refresh/upgrade/install/remove/local .deb,
  cleanup, firmware report, Flatpak remotes, Android runtime), each its own
  polkit action (`exec.argv1`). Read-only operations need no password;
  everything else needs administrator authentication (`auth_admin_keep`);
- validated arguments, no shell, signed repositories only (never
  `--allow-unauthenticated`), protected and excluded packages refused;
- JSON Lines progress and exactly one result line. Cancellation is honoured
  only before anything is changed. Exit codes are fixed. Every call is logged
  to `/var/log/boswas/sysops.log`.

Contract: `docs/architecture/platform-contracts.md`. Control Center and
`boswas-android` run as the user and call it. PackageKit and Discover stay
excluded (ADR-0003).

**Why.** Least privilege. The GUI never runs as root, there is no general
root command executor, and each operation can be reviewed, tested and
authorized on its own.

## ADR-0025 Android applications through Waydroid, opt-in; Google Play not offered

**Decision.**

- **Runtime:** Waydroid (LineageOS, x86_64 images). It is the maintained
  runtime for Plasma on Wayland, and Debian's signed kernel has binder as a
  module (`binder_linux`, loads under Secure Boot lockdown; legacy device
  nodes via `options binder_linux devices=binder,hwbinder,vndbinder`). No
  kernel change and no unsigned module are needed.
- **Opt-in after installation:** the image contains only the manager
  (`boswas-android`). On request, `boswas-sysops android-runtime-install`
  enables the Waydroid project's APT repository with its pinned keyring
  (fingerprint checked; APT pin lets through only the four Waydroid
  packages), installs Waydroid, and hides Waydroid's own launcher and APK
  handler (which offer Google Apps images and skip Boswas's checks).
  `android-init` downloads **VANILLA** images only, verified by Waydroid's
  checksums. Nothing starts at boot.
- **APKs:** inspected before installation (manifest, ABIs, split packages,
  signer certificate). ARM-only apps are refused, because no
  redistributable ARM translation exists. Android verifies signatures;
  Boswas never re-signs or modifies an APK.
- **Google Play: not offered.** Google Mobile Services are licensed by Google
  only to makers of Play Protect certified devices. Boswas OS has no licence
  and Waydroid is not certified. Waydroid's GAPPS images redistribute
  Google's apps without that licence. Boswas never downloads them, never
  works around certification or Play Integrity, and never asks for Google
  credentials. The alternatives are F-Droid (labelled as F-Droid, client
  signer certificate pinned) and local APK files. A licensed source would be
  a new decision.

**Why.** A real, maintained Android runtime without changing the kernel or
weakening Secure Boot, and an honest position on Google Play.

## ADR-0026 A native compositor (boswas-compositor) replaces KWin and KDE Plasma

> **Partly superseded (2026-11-16).** The decision itself stands: the
> compositor, the three trust levels and the control socket. These
> statements of the first version no longer hold and are marked where they
> appear below; the text is kept as the record of that version:
>
> - "No XWayland" and "X11-only applications do not run": ADR-0034.
> - The missing desktop functions (notifications, tray, quick settings):
>   ADR-0035. Screen capture: ADR-0037.
> - Four workspaces, one scale for all outputs, no input methods, no
>   output configuration: ADR-0036.
> - The public control socket now also answers `outputs` and `capture`
>   (ADR-0036, ADR-0037).
> - `loginctl lock-session`: handled through the shell (ADR-0035).
> - "KDE's applications therefore stay" as the system applications:
>   ADR-0040. Settings pages: ADR-0041.

**Decision.**

- **What is replaced.** KDE's compositor (KWin), the Plasma shell, the
  Plasma session, SDDM and System Settings are removed from the image and
  pinned out (`plasma-workspace`, `plasma-desktop`, `kde-plasma-desktop`,
  `kwin-wayland`, `kwin-x11`, `sddm`, `systemsettings`). The build fails if
  a dependency brings one back.
- **What "replacing Wayland" means.** The program that implements Wayland
  is replaced, not the protocol. Wayland is the interface every desktop
  application on Debian speaks (Qt, GTK, Firefox, Wine 10). A different
  protocol would mean that no existing application runs. KDE's
  applications (Dolphin, Konsole, KWrite, Ark, Okular, Gwenview, KCalc)
  therefore stay: they are ordinary Wayland clients of the new compositor.
  *(They still run, but since ADR-0040 the image installs Boswas's own
  applications in their place.)*
- **The compositor.** `boswas-compositor` (package `boswas-desktop`),
  written in Rust on the Smithay library (0.7). It is the display server,
  the window manager and the effects engine in one process, and it runs as
  the logged-in user.
  - **Back ends:** `udev` for hardware (DRM/KMS, GBM, EGL with OpenGL ES,
    libinput; devices are opened through libseat and logind, so there is no
    setuid bit and no capability); `winit` (a window inside another
    session, for development); `headless` (software rendering with pixman,
    for tests).
  - **Window management:** floating windows with Boswas frames drawn by the
    compositor (a client may ask to draw its own), snap layouts by dragging
    or with Super+arrow keys, four workspaces, Alt+Tab in
    most-recently-used order, maximise, fullscreen and minimise, one scale
    for all outputs (1.0 to 2.0 in steps of 0.25). *(Superseded by
    ADR-0036: desktops are created and closed as needed, and each monitor
    has its own settings.)*
  - **Rendering:** damage-tracked. A frame is drawn only when something
    changed and the previous one has been shown.
- **Client trust.** Every client has one of three levels:
  - `Shell`: a program the compositor started itself. The compositor
    creates a connected Wayland socket (`WAYLAND_SOCKET`) and a control
    descriptor (`BOSWAS_COMPOSITOR_FD`) for that one child. No program can
    reach this level through the socket file.
  - `Normal`: any program that connects to the Wayland socket.
  - `Sandboxed`: a client connected through `wp_security_context_v1`
    (Flatpak).

  The privileged globals are filtered per client and offered to `Shell`
  clients only: layer shell (panels and overlays), session lock, the window
  list (`ext-foreign-toplevel-list`) and clipboard control
  (`wlr-data-control`). Protocols for screen capture, input injection,
  global key grabs and output configuration are not implemented, so no
  client has them. *(Superseded in two points. Screen capture is offered
  to every client outside a sandbox and guarded by the user's consent:
  ADR-0037. The input-method and virtual-keyboard protocols exist and
  join the privileged set: ADR-0036. There is still no protocol for
  global key grabs or for output configuration.)*
- **`policy.privileged_protocols`.** `"shell"` is the shipped value. `"all"`
  offers the privileged globals to `Normal` clients too; it exists for
  development. The `[policy]` table is read from the system files only
  (ADR-0028), and sandboxed clients never get the privileged globals.
- **Control socket.** `$XDG_RUNTIME_DIR/boswas-<WAYLAND_DISPLAY>.sock`
  (mode 0600), one JSON object per line. A connection to the socket file
  may send `version`, `lock` and `reload-config`. The window list, window
  control, workspaces, screenshots and `quit` need the descriptor of a
  `Shell` client. Contract: `docs/architecture/platform-contracts.md`.
  *(Since ADR-0036 and ADR-0037 a connection to the socket file may also
  send `outputs` and `capture`.)*
- **No XWayland.** The compositor starts no X server. *(Superseded by
  ADR-0034.)*

**Why.**

- Boswas decides what an application may see and do on the desktop. An
  ordinary application cannot read the screen, other windows, the clipboard
  history or keys typed elsewhere, and that rule is enforced in one place
  that Boswas owns.
- The process that sees every key stroke and every pixel is written in a
  memory-safe language (ADR-0032).
- Smithay supplies the protocol and device plumbing, so the Boswas code is
  mostly policy: trust, window management, the lock.
- One configuration with an administrator layer that a user cannot relax,
  instead of KDE Kiosk keys spread over several files.

**Alternatives.** Keeping KWin and writing only a shell would leave the
trust decisions to KDE's protocol policy and keep the Plasma session.
wlroots (C) has broader hardware coverage today but not the memory safety.
A compositor without a library would repeat years of protocol work.

**Consequences.**

- **X11-only applications do not run.** Wine uses its Wayland driver: the
  WinCompat sandbox binds the Wayland socket and sets no `DISPLAY`. The
  earlier limit "X11 clients can observe each other" (ADR-0012) no longer
  applies, because there is no X server. *(Superseded by ADR-0034, which
  also says what Xwayland means for Wine.)*
- **Missing desktop functions**, all planned work (see the roadmap): a
  notification daemon, a system tray, quick settings for Wi-Fi, Bluetooth
  and volume, a screen-capture portal (screenshots and screen sharing from
  applications), input methods, outputs on a second GPU, HDR and variable
  refresh rate, per-output scale, a display configuration tool. Wi-Fi is
  set up with `nmtui` or `nmcli` for now; volume with `pavucontrol-qt`.
  *(Superseded: notifications, the tray and quick settings exist,
  ADR-0035; screen capture for applications, ADR-0037; input-method
  support and per-monitor settings, ADR-0036. Still missing: outputs on a
  second GPU, HDR and variable refresh rate. The display configuration
  tool belongs to the settings pages, ADR-0041.)*
- **Control Center:** the entries that open Plasma settings modules
  (`kcmshell6`: network, Bluetooth, display, sound, power, users, input
  devices, region, notifications and others) are unavailable, because the
  modules are no longer installed. Its own pages (updates, security,
  software, storage, hardware, compatibility, Android, about) do not
  depend on Plasma. Native settings pages are planned work. *(Superseded
  by ADR-0041: the pages hold native settings sections.)*
- **`loginctl lock-session`** is not handled; locking goes through the
  compositor (ADR-0028). *(Superseded by ADR-0035: the shell passes
  logind's lock request on to the compositor.)*
- **Maintenance:** Boswas now owns a compositor. Hardware regressions that
  KWin's developers would have fixed are ours to find and fix.
- **Not verified yet:** the `udev` back end has been compiled but has not
  run on a real DRM device, in QEMU or on hardware. The image has not been
  built with the new manifest, and its dependency resolution on Debian 13
  is untested. Verified in the development environment: unit tests, six
  end-to-end tests that drive the real binary on its headless back end
  (trust filtering, control-socket limits, window life cycle, lock), a
  nested run on the `winit` back end with OpenGL ES, the package build and
  lintian. *(The numbers are those of the first version; the current
  state is in `verification.md`. The `udev` back end has still not run on
  a device.)*

**Revisit when** hardware validation shows a class of devices the `udev`
back end cannot drive, or an application Boswas must support needs X11
(then: XWayland as an opt-in, confined client). *(X11 support was
decided in ADR-0034, on by default and not as a confined client.)*

## ADR-0027 Boswas Shell is written in Rust on boswas-ui, not in Qt/QML

**Decision.** The panel, the launcher, the wallpaper, the lock screen and
the login greeter are one Rust program, `boswas-shell`
(`/usr/libexec/boswas/boswas-shell`; `--lock` and `--greeter` select the
other two roles). It draws with `boswas-ui`, a small library in the same
workspace: tiny-skia (software rasteriser), cosmic-text (text shaping,
system fonts) and resvg (SVG icons). It is a Wayland client of the
compositor (layer shell and session lock, through smithay-client-toolkit)
and reads windows and workspaces from the control descriptor (ADR-0026).

The project plan's first draft names Qt 6/QML for the shell. This decision
replaces that part of the plan.

*Since ADR-0035* the same program also draws Start, quick settings, the
notification centre, toasts, the tray and the dialogs, and runs the
session's services. The decision to write it in Rust on `boswas-ui`
stands; the two consequences marked below changed.

**Why.**

- **One memory-safe toolchain** for every process that holds privileged
  protocol access or sees a password: compositor, panel, lock screen and
  greeter.
- **No QML engine and no JavaScript runtime** in the lock screen and the
  greeter. Both run before or instead of a user session, and the greeter
  handles the password of every user.
- **Small footprint:** no Qt Quick, scene graph or GPU context per shell
  surface. The shell draws into shared-memory buffers and redraws only on
  change.
- **One renderer and one token file** for the window frames (drawn by the
  compositor) and the shell, so they cannot drift apart (ADR-0031).
- `boswas-ui` forbids `unsafe` code. SVG icons are rendered without
  following external references.

**Consequences.**

- **No accessibility stack in the shell yet:** no screen-reader interface
  (AT-SPI), no on-screen keyboard. Qt would have provided the first.
- **Boswas maintains its own toolkit:** every new control (a slider, a
  list, a menu) is written and tested here. Today it has what the panel,
  the launcher and the password prompt need, no more. *(The last sentence
  is superseded by ADR-0035: the shell now has flyouts, menus, buttons,
  sliders, switches, scroll bars and text fields, all written here.)*
- **Text:** shaping and font fallback come from cosmic-text; complex
  scripts and right-to-left layout in the shell have not been reviewed.
- **Applications are unaffected:** Control Center and the Compatibility
  Manager stay PySide6/Qt applications, and KDE applications keep the
  Breeze widget style with the Prism colour scheme. *(Since ADR-0039 the
  two Boswas applications take their look from the toolkit
  `boswas_fluent`; KDE applications are as described.)*
- **Not verified yet:** the shell has run against the headless and nested
  compositor (panel, launcher, search, lock) but not on hardware; the
  greeter's conversation with greetd has unit tests for the message format
  and has not run against a real greetd. *(Still so for hardware and
  greetd; the current list is in `verification.md`.)*

**Revisit when** the shell needs views that are costly to build by hand
(a settings application, rich notifications) or when an accessibility
requirement is set. A Qt shell would then be a `Shell`-trust client like
any other; the compositor does not change. *(Notifications were built in
the shell, ADR-0035. Settings is a Qt application, not part of the shell:
ADR-0039 and ADR-0041. The accessibility point is still open.)*

## ADR-0028 The compositor owns the session lock; a user cannot relax the lock timeout policy

**Decision.**

- **Locking is a compositor state.** `lock_session` first blanks every
  output and takes pointer and keyboard away from applications, and only
  then starts the lock screen (`boswas-shell --lock`) as a `Shell` client.
  The lock screen uses `ext-session-lock-v1`; its surfaces replace the
  blank screen when they arrive.
- **Only the lock screen unlocks.** The lock ends when a lock client sends
  `unlock_and_destroy`, which `boswas-shell` does after PAM has accepted
  the password (service `boswas-lock`: `common-auth` and `common-account`,
  no session, nothing changed). There is no control-socket command that
  unlocks.
- **A lock screen that dies leaves the session locked.** The outputs stay
  blank, applications get no input, and the lock screen is started again
  after three seconds.
- **When the session locks:**
  - Super+L;
  - after `lock.idle_seconds` without input (shipped: 600), unless a
    client inhibits idling (a video, a presentation);
  - before suspend and hibernate:
    `/usr/lib/systemd/system-sleep/boswas-lock` sends `lock` to every
    Boswas compositor and systemd waits for it;
  - `boswas-compositor --send lock`. `lock` is one of the three commands
    every connection may send: any program of the user may lock, none may
    unlock.
- **Policy.** `[policy] lock_idle_max_seconds` (shipped: 600) is the
  longest timeout. A user may choose a shorter one in
  `~/.config/boswas/compositor.toml`; a longer one, or 0 ("never"), is cut
  back to the maximum. The `[policy]` table is read from
  `/usr/share/boswas/compositor/compositor.toml` and
  `/etc/boswas/compositor.toml` only and is ignored in a user's file. This
  replaces `kscreenlockerrc` with KDE Kiosk keys (ADR-0007).
- **Status.** `boswas security status` reads the compositor's policy for
  its `screen-lock` check.

**Why.** A lock that is a separate program on top of the desktop fails
open: if it crashes, the session is visible. With the lock inside the
compositor, the failure mode is a blank, locked screen. The password is
checked in the lock screen, not in the compositor, so the process that
handles every window never sees it.

**Consequences.**

- `loginctl lock-session` and the `org.freedesktop.ScreenSaver` D-Bus
  interface are not handled. Tools that lock that way do nothing.
  *(Superseded for `loginctl lock-session` by ADR-0035: while the shell
  runs, logind's lock request locks the session. logind's unlock request
  is ignored, and the ScreenSaver interface is still not provided.)*
- The sleep hook locks through the control sockets in `/run/user/*`; a
  session whose compositor does not answer is not locked by it.
- There is no way to unlock a session whose lock screen cannot start (for
  example a damaged installation) other than ending the session from a
  virtual terminal. That is intended.
- No fingerprint or smart-card unlock: the PAM service is password-only
  (biometric PAM is pinned out, ADR-0004).
- **Which programs are trusted is system-only too:** `[lock] command` and
  `[shell] command` name programs the compositor starts as trusted
  clients, so they are read from the system files only; in a user's file
  they are ignored with a warning, like `[policy]` (unit test
  `a_user_cannot_name_the_trusted_programs`).
- **Limit of "cannot relax":** this is not a boundary against a program
  that runs unsandboxed as the same user. Such a program cannot change
  the policy or the trusted programs through the configuration, but it
  can still act as the user in other ways (edit `~/.profile`, attach a
  debugger to the user's processes where the kernel allows it). Confining
  untrusted programs is the job of the sandboxes.
- **Not verified yet:** suspend and resume on hardware, and PAM with a
  real account database on the image. Verified: unit tests of the policy
  layering, and end-to-end tests that the session stays locked across
  two crashed lock screens and unlocks only through a lock client.

**Revisit when** a managed policy needs more than a maximum timeout (lock
on smart-card removal, a grace period), or when logind integration is
needed for remote "lock this device" commands. *(The logind lock request
is handled since ADR-0035.)*

*Since ADR-0036 and ADR-0037* the same rule covers one more program:
`[input_method] command` is read from the system files only, like
`[lock] command` and `[shell] command`. And a locked session gives no
pictures of the screen to any program.

## ADR-0029 boswas-bootsplash replaces Plymouth

*What the boot screen shows and which typeface it uses were changed by
ADR-0038. The programs, the display and unlock paths, the sandbox and the
failure behaviour decided here are unchanged.*

**Decision.** The boot screen, the disk passphrase prompt and the shutdown
and restart screens are one Rust package, `boswas-bootsplash`. Plymouth is
removed, pinned out (`plymouth`, `plymouth-themes`, `plymouth-label`) and
the package conflicts with it.

- **Programs:** `boswas-bootsplashd` (the daemon: display, console
  keyboard, control socket) and `boswas-bootsplash` (the client and the
  systemd password agent).
- **Display:** DRM/KMS dumb buffers, or the legacy frame buffer when no
  DRM device exists. No GPU acceleration and no libdrm. The daemon follows
  the hand-over from the firmware frame buffer to the real driver.
- **Rendering:** the picture is a fixed set of items; a frame repaints only
  the rectangles of items that changed. The artwork, the typeface and the
  colours (from the token file, ADR-0031) are compiled in, so nothing is
  parsed or loaded from disk at boot.
- **Disk unlock:** the daemon starts in the initramfs. Debian's
  `cryptsetup-initramfs` asks for the passphrase through `/bin/plymouth`,
  so the client is installed under that name **inside the initramfs
  only** and understands the commands the unlock helper and the initramfs
  scripts use. No file named `plymouth` exists on the root file system.
  Later password requests (a second encrypted volume) reach the boot
  screen through a systemd password agent.
- **Least privilege:** after the display, the console and the socket are
  open, the daemon sets `no_new_privs`, enters a Landlock domain (graphics
  devices and its runtime directory only, no TCP) and keeps three
  capabilities. The socket is root-only and the peer is checked. The
  passphrase lives in one buffer that is wiped after use and is never
  logged.
- **Failure:** without `splash`, or if the boot screen cannot start or
  dies, the passphrase is asked for on the text console as before. A
  supervising process restores the text console.
- **Hand-over:** `quit --retain` leaves the last frame on screen until the
  greeter's compositor shows its own (ADR-0030).

Details: `packages/boswas-bootsplash/README.md`.

**Why.** The boot screen runs as root in the initramfs and reads the disk
passphrase. Plymouth is a C daemon with a plugin loader, a theme script
interpreter and image and font loading at boot; none of that is needed for
one fixed screen. A program with the screen compiled in has far less to
parse, is memory-safe outside one audited module (ADR-0032), and confines
itself once it is running. Repainting only what changed keeps the
animation cheap on machines without a working GPU driver.

**Consequences.**

- Boswas maintains the compatibility with Debian's unlock helper. A change
  in how `cryptsetup-initramfs` calls Plymouth must be followed; the image
  check fails the build if the client is missing from an initramfs.
- Text is ASCII from a built-in glyph atlas; other characters are drawn as
  `?`. Prompts are in English. `ask-for-password --command` is not
  supported.
- No per-preset boot themes: the boot screen is the same for every preset.
- Landlock is best effort: a kernel without it still gets `no_new_privs`
  and the reduced capabilities.
- **Not verified yet:** the DRM and frame-buffer outputs, the initramfs
  scripts and the systemd units have not run on a real boot, in QEMU or on
  hardware, and no LUKS volume has been unlocked through it. Verified in
  the development environment: 107 unit and integration tests (scene and
  damage, control protocol, Plymouth-compatible client, password agent,
  the unlock helper's call sequence in a scratch root), the package build
  and lintian. *(124 tests since ADR-0038; what is not verified is
  unchanged.)*

**Revisit when** the boot test or hardware validation shows a display the
daemon cannot drive, or when localised boot text is required.

## ADR-0030 greetd is the login daemon, with the Boswas greeter

**Decision.**

- **greetd replaces SDDM.** greetd (Debian package `greetd`) owns the
  login: it runs a greeter as the unprivileged user `_greetd`, performs
  PAM authentication itself, and starts the user's session.
- **The greeter is the Boswas desktop.** `/usr/libexec/boswas/boswas-greeter`
  starts `boswas-compositor` with `greeter.toml` (no lock, no applications)
  and `boswas-shell --greeter` as its only `Shell` client. The greeter
  shares its drawing and its password field with the lock screen.
- **Configuration through a drop-in.** `boswas-desktop` ships
  `/etc/boswas/greetd.toml` and
  `/usr/lib/systemd/system/greetd.service.d/10-boswas.conf`, which starts
  greetd with that file on the first virtual terminal after the boot
  screen. greetd's own `/etc/greetd/config.toml` is not edited (ADR-0006).
- **Session.** After login greetd starts `/usr/bin/boswas-session`: the
  login profile, the session environment, the compositor, then the systemd
  user target `boswas-session.target`, which pulls in
  `graphical-session.target` (the device session agent, the polkit agent,
  XDG autostart entries). *(Since ADR-0035 the polkit agent is the shell,
  which the compositor starts; the target no longer starts one.)*
- **Fallback.** If the graphical greeter fails within ten seconds of its
  start, `boswas-greeter` runs greetd's text login (`agreety`) with the
  same session command, so there is always a way to sign in. *(Changed by
  ADR-0044: the fallback follows the compositor's exit status, the text
  login opens a shell and not the desktop, and a greetd that fails is
  followed by getty.)*
- **Live medium.** live-config appends an `[initial_session]` for the live
  user to `/etc/boswas/greetd.toml` at boot *(only when that user exists,
  ADR-0044)*. The shipped file contains
  none, and the image check fails the build if it does: an installed
  system always shows the login screen.

**Why.** greetd is small, has no user interface of its own and speaks a
simple, documented protocol (JSON over a Unix socket), so the login screen
can be the same code as the lock screen under the same compositor. SDDM
would bring Qt Quick and its own theme format back for one screen, and
its Wayland greeter depends on a compositor Boswas no longer ships.

**Consequences.**

- One session type. There is no session chooser; `boswas.desktop` is
  installed in `/usr/share/wayland-sessions` for tools that list sessions.
- No user list with pictures, no password-change dialogue for expired
  passwords, no remote or guest login. The greeter asks for a user name
  and a password. *(Since the shell of ADR-0035 the greeter lists the
  people who can sign in, by name and without pictures: regular accounts
  with a login shell, at most eight, read from `/etc/passwd`. Another
  user name can still be typed. The rest of this point stands.)*
- The greeter leaves authentication to greetd and its PAM service
  (`/etc/pam.d/greetd` from the Debian package, built on the `login`
  stack). The password goes to greetd once, over its socket, and is wiped
  from the greeter's memory.
- `sddm` is no longer a protected package of `boswas-sysops`; `greetd` is.
- **Not verified yet:** a login on a real boot (greetd, PAM, logind seat
  hand-over to the user's compositor), the text-login fallback, and the
  live session's automatic login. *(Since ADR-0044 these have been booted
  in a virtual machine, on another distribution's base system; not yet on
  Debian 13 or on hardware.)* Verified: the drop-in and configuration
  contents by static and package tests, and the greeter's message encoding
  by unit tests.

**Revisit when** multi-user features are needed (user switching, a user
list), or if greetd leaves Debian.

## ADR-0031 Boswas UI v2: one token file ("Prism") for colour, geometry, type and motion

> **Values superseded by ADR-0033 (2026-11-16):** the token file is now
> version 3 and follows the Fluent design language. The mechanism decided
> here stands: one file, the same consumers, generated outputs, tests.
> The text below is kept as the record of version 2.

**Decision.**

- **Single source.** `desktop/ui/tokens.json` (Boswas UI, version 2)
  defines the palette, the semantic dark and light themes, icon hues,
  corner radii, the spacing unit, the type scale, elevation and motion.
  Nothing else in the repository defines a UI colour.
- **Values from the project plan:** window radius 8 px, control radius
  4 px, a 4 px spacing grid, type sizes 12/14/16/20/28/40, motion of 150
  to 250 ms with an ease-out curve, four elevation levels, Inter as the UI
  typeface (Debian's `fonts-inter`).
- **Palette "Prism":** ink and cloud neutrals with iris as the accent, and
  azure, mint, sun, coral, sky, orchid and slate families. It replaces the
  navy, gold and silver scheme of the desktop. The Boswas OS mark keeps its
  gold and silver (ADR-0008); gold is a brand colour, no longer the UI
  accent.
- **Consumers:**
  - `boswas-ui` compiles the file in (compositor frames, shell, lock
    screen, greeter);
  - `boswas-bootsplash` reads it at build time;
  - `desktop/ui/tools/build_kde_colors.py` generates the KDE colour
    schemes `BoswasPrism` and `BoswasPrismLight`, so KDE applications
    match;
  - `desktop/icons/tools/build_prism.py` generates the icon themes
    `Boswas-Prism` (default) and `Boswas-Prism-Light`: 264 designs, 461
    SVG files (300 designs in 535 files since ADR-0043);
  - `boswas-branding` installs the file as
    `/usr/share/boswas/ui/tokens.json` for other tools.
- **Checks.** `desktop/ui/tests` fails the build when a reference does not
  resolve, a plan value changes, an icon tile has less than 3:1 contrast,
  a generated colour scheme misses WCAG AA for text, or a generated file
  is stale.
- **Earlier themes.** The icon themes `Boswas` and `Boswas-Light` stay
  installed and selectable.

**Why.** Three programs in two languages and two generators draw Boswas
surfaces. Without one source they drift, and a contrast fix in one place
is missed in another. A data file with tests makes the design system
reviewable and lets a designer change a value without reading Rust.

**Consequences.**

- A token change needs the Rust packages rebuilt; there is no run-time
  theming of the shell beyond dark and light.
- The ten presets still change wallpaper, the scheme (dark or light) and
  the colours of KDE applications, but the shell and the window frames
  ignore a preset's accent colour. The Plasma Global Theme data of the
  presets is still generated and is unused. *(It was removed later:
  ADR-0042.)*
- Third-party icon coverage is what the pack contains; other icons fall
  back to Breeze (`breeze-icon-theme`).
- The palette was chosen and contrast-checked by tooling; it has had no
  design review and no test with users.

**Revisit when** a designer delivers a reviewed palette (it replaces the
values, not the mechanism) or presets must recolour the shell.

## ADR-0032 Rust build policy: Debian's toolchain, a committed lock file, crates from crates.io

**Decision.**

- **Toolchain:** the `rustc` and `cargo` of Debian 13 (1.85). Both
  workspaces declare `rust-version = "1.85"`; `boswas-desktop` uses
  resolver 3, which only selects dependency versions that build with it.
  No rustup and no nightly features in a package build.
- **Dependencies:** `Cargo.lock` is committed for `packages/boswas-desktop`
  and `packages/boswas-bootsplash`, and `debian/rules` builds and tests
  with `--locked`. cargo downloads exactly those crate versions from
  crates.io during the package build and verifies their checksums against
  the lock file. `CARGO_HOME` and the build products stay inside the
  package's build directory.
- **Build settings:** release profile with thin LTO and integer overflow
  checks on (`boswas-desktop` also aborts on a panic instead of
  unwinding); build paths are remapped out of the binaries.
- **`unsafe`:** in shipped code (`src/`) confined to an allowlist that
  `tests/static/test_sources.sh` enforces. `boswas-ui` forbids it.
  `boswas-bootsplash` denies it outside `src/sys.rs`. In `boswas-desktop`
  it is allowed in the compositor's `backend/udev.rs`, `ipc.rs` and
  `state.rs` (descriptors handed to child processes, device and EGL
  set-up) and in the shell's `control.rs` and `pam.rs` (the inherited
  descriptor, the libpam binding). A new source file with `unsafe` fails
  the static stage until it is reviewed and listed. Test programs are not
  shipped and are not covered by the list. *(Since ADR-0037 the list has
  one more file, the compositor's `handlers/screencopy.rs`: the copy into
  a program's shared-memory buffer. The crates added since,
  `boswas-services` and `boswas-services-bridge`, contain no `unsafe`.)*

**Why.** Debian's toolchain is the one that receives Debian's security
support and is in the builder image; a second toolchain would be one more
thing to pin and trust. A committed lock file makes the dependency set
reviewable and identical on every build host. Debian 13 does not package
Smithay or several of its dependencies, so building only from
Debian-packaged crates is not possible today.

**Trade-off.** The package build needs network access to crates.io, like
the image build needs the Debian archive, and the crates are not covered
by Debian's security team: Boswas must follow advisories for the locked
versions itself. Vendoring the crate sources (`cargo vendor`) into the
repository or a Boswas archive is the follow-up: it gives offline and
reproducible builds and a place to audit. It was not done now because it
adds a large source tree that has to be reviewed and licensed as a whole.

**Consequences.**

- `boswas-desktop` and `boswas-bootsplash` are `Architecture: amd64`
  packages; the other Boswas packages stay `all`.
- The builder image gains `cargo`, `rustc`, `pkgconf` and the development
  packages of libseat, libinput, libudev, GBM, pixman, xkbcommon and PAM.
- A package build downloads and compiles a few hundred crates (the lock
  files name 329 and 86 packages, not all of them for Linux), which takes
  several minutes. `build-packages.sh` builds in a scratch tree, so nothing
  is reused between builds. *(366 and 86 since the service backends of
  ADR-0035, which added zbus and its dependencies. The boot screen
  package also needs `fonts-inter` at build time, ADR-0038.)*
- No automated advisory check (`cargo audit`) and no SBOM for the crates
  yet (M9).

**Revisit when** vendoring is in place (then: `--offline` in
`debian/rules`), or when Debian packages the needed crates.

## ADR-0033 Boswas UI v3: the Fluent design language with Boswas's own colours, icons and typeface

Supersedes the values of ADR-0031. Its mechanism (one token file, its
consumers, generated outputs, tests) is unchanged.

**Decision.**

- **The design language is Fluent.** The desktop follows the Fluent
  (WinUI) design language, with Windows 11 as the target look: the layout
  of the shell (a centred taskbar, Start, quick settings, a notification
  centre), the geometry of windows and controls, the materials and the
  motion curves. `desktop/ui/tokens.json` is now version 3 and says so in
  its `design` field.
- **What is Boswas's own.** The colours (the Prism hue families, with iris
  as the accent), the icons (the Boswas Prism pack), the typeface (Inter)
  and the mark, which is the Start button. Nothing of Microsoft's is
  taken as an asset: no font, icon, logo or wallpaper of Microsoft is in
  the repository, and `type.family` names Inter, Noto Sans, Lato and
  DejaVu Sans.
- **What version 3 changes in the token file:**
  - `neutral`, a grey family of twelve steps: the surface, border and
    text roles of both themes are now steps of it, or white. `ink` and
    `cloud` stay in the palette for the icon pack, the terminal colours
    and one group of the light KDE colour scheme.
  - The dark theme's accent is `iris.400` with dark text on it
    (`on-accent` is `ink.950`); the light theme keeps `iris.600` with
    white text. The focus colour is white on dark and `neutral.950` on
    light. A new role, `chrome`, is the taskbar's.
  - `stroke` and `fill`: one-pixel outlines and the state layers of
    controls, as opacities of white (dark theme) or black (light theme)
    over whatever lies beneath.
  - `material`: mica, acrylic, chrome and smoke, each a tint, a tint
    opacity and a blur radius. How they are drawn is ADR-0036.
  - `radius`: windows and flyouts 8, controls, cards and tooltips 4.
  - `type.ramp`: caption 12, body and body-strong 14, body-large 16,
    subtitle 20, title 28, title-large 40, each with its line height and
    weight. The sizes are those of `type.scale`, which is unchanged.
  - `elevation`: five levels (desktop, tooltip, window, flyout, dialog).
  - `motion.curves`: decelerate, accelerate and standard, as the control
    points of a cubic Bézier curve. The durations stay 150, 200 and
    250 ms.
  - `shell`: the fixed sizes of the shell and the window frames in
    logical pixels, among them a 48 px taskbar, a 32 px title bar and
    46 by 32 px caption buttons, and two literal reds for the hover and
    pressed states of the close button. In the token file those two are
    the only interface colours that are not palette references.
- **Consumers.** Those of ADR-0031, and the Qt toolkit `boswas_fluent`
  (ADR-0039), which reads the installed file at run time. `boswas-ui`
  exposes the new sections. The KDE and Konsole colour schemes were
  regenerated. The icon hues are unchanged.
- **Checks.** `desktop/ui/tests` gains `test_the_fluent_geometry`: flyouts
  share the window radius and cards the control radius, the taskbar,
  title bar and caption buttons have the sizes above, every stroke and
  fill opacity lies between 0 and 0.25, and every material's tint
  resolves. The checks of ADR-0031 still run, among them WCAG AA for the
  text of the generated colour schemes. The toolkit has its own contrast
  tests (ADR-0039).

**Why.** Windows 11 was set as the target look of the desktop for this
round, as a product decision, like the Prism palette before it
(ADR-0031). A design language can be followed in its layout, geometry and
motion. Logos, wallpapers, icons and typefaces are another company's
property and are not taken; Boswas has its own. Keeping every value in
the one token file keeps the shell, the window frames, the boot screen,
the Qt toolkit and the colours of KDE applications in step.

**Consequences.**

- Of the values ADR-0031 lists, window radius 8, control radius 4, the
  4 px grid, the type sizes, 150 to 250 ms and Inter still hold, and
  `test_the_specification_values` still pins them. "Four elevation
  levels" is now five, and "ink and cloud neutrals" no longer describes
  the themes.
- `type.display-family` (Orbitron) is still in the file and `boswas-ui`
  still exposes it, but no Boswas surface draws text in it any more
  (ADR-0008).
- As before, a token change needs the Rust packages rebuilt, and a preset
  changes light or dark and the wallpaper of the shell, not its accent.
- The resemblance to another vendor's desktop is deliberate. The
  repository records no legal review of it and no design review; the
  layout follows an existing design, and the palette is as unreviewed as
  ADR-0031 says.

**Revisit when** a designer delivers a reviewed palette or layout (the
values change, the mechanism stays), or when a review asks for more
distance from the model.

## ADR-0034 X11 applications run through Xwayland

Supersedes "No XWayland" in ADR-0026.

**Decision.**

- **One X server per session.** `boswas-compositor` starts Xwayland
  (Debian package `xwayland`) and is its window manager. `[xwayland]
  enabled` is `true` by default; the package is in the image and a
  Recommends of `boswas-desktop`. Where the program is missing, the
  session runs without X11 and the compositor logs that.
- **An X11 window is an ordinary window:** the Boswas frame unless the
  application draws its own, snapping, the taskbar, the window picker and
  Task View. Menus and tooltips are shown where they place themselves.
- **The X server is an unprivileged client.** It connects to the
  compositor like an application and is offered none of the privileged
  protocols and not the screen-capture protocol (ADR-0037). It listens on
  its socket file only: the abstract socket is switched off, because it
  would be reachable from every network namespace, sandboxes included.
- **Clipboard.** Shared in both directions. The X server is given the
  clipboard of Wayland applications only while an X11 window has the
  keyboard; the compositor cannot tell the X11 applications apart, so
  that gate is for all of them together.
- **`DISPLAY`.** The compositor sets it for the programs it starts (the
  shell, and through the shell the applications) and removes a stale one
  when there is no X server. `boswas-session-ready` does not publish it
  to the systemd user manager or to D-Bus activation.
- **Wayland stays the first choice.** The session still tells the
  toolkits to use Wayland (`QT_QPA_PLATFORM=wayland;xcb`,
  `GDK_BACKEND=wayland` and the others in `boswas-session`).

**Why.** Without an X server, applications that only speak X11 did not
run at all (ADR-0026). Xwayland is the X server Debian packages for use
under a Wayland compositor, and Smithay supplies the window-manager side,
so the Boswas code is the mapping of X11 windows onto the desktop's own
windows.

**Alternatives.** ADR-0026 expected X11 support as an opt-in with a
confined client. It is on by default instead, so that an X11 application
works without a setting, and it is not confined beyond what the first two
points above say. Starting one X server per application, each in its own
sandbox, would restore isolation between X11 applications; it was not
attempted.

**Consequences.**

- **X11 has no isolation between its clients.** Applications connected to
  the X server can see each other's windows and input. They cannot see
  Wayland applications. The known limit of ADR-0012 applies again.
- **Windows applications stay Wayland clients.** The session now has an
  X server and `DISPLAY` is set for what the shell starts, but the
  WinCompat sandbox does not bind the X11 socket when it can give the
  compositor's Wayland socket (`boswas_compat/sandbox.py`; it binds X11
  only in a session that has no Wayland socket). Clients of one X server
  can watch each other, and a sandboxed application must not be able to
  watch other windows, so ADR-0026's statement holds for Windows
  applications: Wine draws through its Wayland driver. Not verified:
  Wine's Wayland driver on Debian 13. Letting chosen Windows
  applications onto Xwayland would be a policy decision of its own.
- **Scaling.** X11 windows are drawn at scale 1 and enlarged on a scaled
  monitor, so they look soft there.
- **The administrator can turn it off.** When a system file sets
  `[xwayland] enabled = false`, a user's file does not turn the X server
  back on; a user can turn it off for their own session. A device that
  must never run X11 applications can also leave the package out.
- **Not verified yet:** Xwayland on real graphics hardware; the clipboard
  between X11 and Wayland applications, which has no automated test; Wine
  through Xwayland. Verified: one end-to-end test starts a real X11
  program through Xwayland on the headless back end and checks that its
  window is listed as an X11 window, gets a frame, is maximised and
  closes. The test is skipped where Xwayland or the test program is not
  installed.

**Revisit when** X11 applications have to be kept apart from each other
(then: one confined X server per application), when the WinCompat
sandbox should stop offering the X11 socket so that Wine stays on
Wayland, or when X11 windows must be sharp on scaled monitors.

## ADR-0035 The shell hosts the session's services; one set of backends serves the shell and Settings

Replaces the "missing desktop functions" of ADR-0026 for notifications,
the tray and quick settings, and the KDE polkit agent of the first
version.

**Decision.**

- **One crate talks to the system.** `boswas-services` (in the
  `boswas-desktop` workspace) holds ten services: notifications, tray,
  media players, network, Bluetooth, audio, power, brightness, session
  and authentication. Each runs on its own thread, takes commands and
  reports its whole state as an event whenever it changes. It uses zbus
  on async-io; there is no tokio.
- **The shell runs them in its own process.** In its desktop role
  `boswas-shell` therefore:
  - owns `org.freedesktop.Notifications` on the session bus and shows
    what applications send as toasts and in the notification centre;
  - owns `org.kde.StatusNotifierWatcher` and shows the tray items
    registered there, with their menus;
  - is registered with polkit as the session's authentication agent and
    draws the password dialog itself, with a choice of administrator when
    several may authorise the action. The password is checked by polkit's
    own helper (`polkit-agent-helper-1`), not by Boswas code;
  - shows and changes Wi-Fi, Bluetooth, sound, brightness and the power
    mode in quick settings, and controls media players (MPRIS);
  - passes logind's lock request (`loginctl lock-session`) and the
    announcement of sleep on to the compositor as `lock`.

  KDE's polkit agent is no longer started by the session and no longer in
  the image.
- **Three sets of services.** The desktop runs everything. The lock
  screen and the login screen run the five status services only and obey
  nothing but refresh, volume, mute and brightness: they own no bus name,
  show no notifications, register no pairing or authentication agent.
  The third set is for Settings.
- **A bridge for Settings.** `/usr/libexec/boswas/boswas-services-bridge`
  runs the network, Bluetooth, audio, power and brightness services for
  the Settings application, which is written in Python, and speaks JSON
  lines on its standard input and output. It offers the commands of
  those five services only: no power actions, no notifications, no
  authentication. It runs as the user who started it and ends when its
  input closes. A Wi-Fi passphrase travels on that pipe and never on a
  command line. Contract: `docs/architecture/platform-contracts.md`.
- **What the services talk to:** NetworkManager, BlueZ, UPower,
  power-profiles-daemon and logind over D-Bus; the sound server through
  `pw-dump` and `wpctl`; the backlight through `/sys/class/backlight` and
  logind. A daemon that is not running is "not available", never an
  error, and is picked up when it appears.
- **Other programs are not trusted.** What notification senders, tray
  items, media players and even the system daemons send is read by type,
  stripped of control characters, cut to fixed lengths and bounded in
  number. Notification markup is removed, not interpreted. Signals of the
  system daemons are accepted from the owner of the daemon's name only.
  `packages/boswas-desktop/crates/boswas-services/README.md` lists the
  limits.

**Why.**

- The surfaces that show these things (toasts, the tray, the password
  dialog) are overlays, and overlays are offered only to programs the
  compositor starts itself (ADR-0026). With the services in the shell
  there is no second trusted program, and no protocol between a daemon
  and the surface that shows its state.
- The shell stays a plain event loop: no call to the system runs on the
  thread that draws.
- Quick settings and Settings show the same state and behave the same,
  because one implementation is behind both. A second implementation in
  Python would have had to repeat the handling of secrets and of
  untrusted input.
- The lock screen and the login screen must not show notifications or
  take over authentication; the restricted set makes that a property of
  the process, not of the drawing code.

**Alternatives.** The first version started KDE's polkit agent from the
session target and had neither a notification service nor a tray. An
agent or a notification daemon as a separate program would be an ordinary
client: it could show a normal window, not an overlay above a full-screen
application.

**Consequences.**

- **The services live and die with the shell.** If the shell exits, the
  compositor starts it again; until then the bus names are free, and what
  was held in memory is gone: the open notifications (they are not
  written to disk) and a pending authentication, whose helper is ended.
- **Administrator actions depend on this agent.** `pkexec`, and with it
  every `boswas-sysops` operation that Control Center starts (ADR-0024),
  needs an agent to ask for the password. No other graphical agent is in
  the image. The conversation with polkit's helper is tested against a
  stand-in only (see below), and the variant of polkit that reaches its
  helper through a socket is not handled.
- **Bluetooth:** the pairing agent declares that it can neither show nor
  ask for a code, and agrees only to a request that concerns a device the
  person asked to connect to in the last 60 seconds. Devices that insist
  on a PIN or passkey cannot be paired from the shell.
- **Wi-Fi:** open, OWE, WEP, WPA-PSK and SAE networks. Enterprise
  networks (802.1X) are refused with a message. A new profile is saved
  for all users when NetworkManager lets the person change system
  connections, and as the person's own profile otherwise.
- **Tray:** StatusNotifierItem only. The older X11 tray protocol
  (XEmbed) is not implemented, so an X11 application that offers only
  that has no tray icon.
- **Packages:** `network-manager`, `bluez`, `polkitd`, `pipewire-bin`,
  `wireplumber`, `upower` and `power-profiles-daemon` join the Recommends
  of `boswas-desktop`; the last four are also new in the image. Each is
  optional at run time. `polkit-kde-agent-1` leaves both.
- **Rust dependencies:** zbus 5 and the async-io family. The lock file of
  `boswas-desktop` grows from 329 to 366 packages (ADR-0032).
- **Not verified yet:** everything against the real daemons. The
  integration tests run each service against two private `dbus-daemon`
  processes and scripted stand-ins for NetworkManager, BlueZ, UPower, the
  power profiles daemon, logind, polkit, tray items and media players;
  `pw-dump`, `wpctl` and the polkit helper are shell scripts the tests
  write. The builder's report names as the largest risks the polkit
  helper protocol, the shape of `pw-dump`'s JSON and the arguments of
  `wpctl`, NetworkManager's settings dictionaries, and BlueZ pairing with
  PIN devices. The Debian package names of the run-time tools were taken
  from another distribution's index and are not checked against trixie.

**Revisit when** the shell's restarts cost too much state (then: the
notification history on disk, or the services in a process of their
own), when devices with a PIN have to be paired or enterprise Wi-Fi set
up from the desktop, or when the first run against real polkit shows
that the helper protocol differs.

## ADR-0036 Window management and effects: what the compositor draws itself, and how

Replaces the window-management bullet of ADR-0026 (four workspaces, one
scale for all outputs) and its statements that input methods and output
configuration are missing. The feature list is in
`boswas-compositor(1)`; this record holds the decisions behind it.

**Decision.**

- **The compositor assembles each output's picture itself** instead of
  handing its windows to Smithay's generic space renderer. Per-window
  transforms, the window picker, Task View and the clipped corners all
  need that.
- **An animation never changes where a window is.** Opening, closing,
  minimising, restoring and the slide between desktops change only how a
  window, or a picture of it taken as it leaves, is drawn for a few
  frames. `[appearance] reduced_motion` turns all of it off.
- **Rounded corners by a clip shader, on OpenGL ES only.** The frame
  (title bar, border, shadow) is drawn by the compositor with 8 px
  corners. The client's content below the title bar is a rectangle; on
  the OpenGL ES renderer a fragment shader fades it out outside the
  frame's rounded lower corners. The software renderer, which only the
  headless back end uses, draws content unclipped and gives those frames
  square lower corners.
- **Materials are drawn from the wallpaper, not from what is behind a
  surface.** The shell keeps a reduced copy of each output's wallpaper,
  blurs it once per blur radius and covers it with the material's tint
  (`material` in the token file). The taskbar and the flyouts therefore
  show the blurred wallpaper even where a window lies behind them. The
  compositor's own overlays (the window picker, the snap layouts, the
  window menu, Task View) are filled with the material's tint alone.
  There is no live blur of windows.
- **Night light is a gamma ramp.** The compositor loads a ramp for the
  colour temperature into each display controller (the DRM gamma
  tables). `[night_light]` switches it on, sets 2500 to 6500 kelvin and
  can schedule it by the clock; quick settings switch it through the
  shell's control connection. No client protocol for gamma is offered.
- **Each monitor has its own settings.** `[outputs.NAME]`, by connector
  name: on or off, mode, scale, position and rotation, applied at start,
  on hotplug and on `reload-config`. The last enabled monitor is never
  turned off. `[output] scale` is the default for monitors without a
  setting. There is no output-configuration protocol; a program reads the
  monitors and their modes with the public `outputs` command and changes
  them by writing the user's file and asking for a reload.
- **Desktops are created and closed as needed.** A session starts with
  one; Task View and Super+Ctrl+D add more.
- **Shortcuts follow Windows,** and stay fixed: there is no shortcut
  editor.
- **Input methods are a trusted program.** Every application may use
  text input (`text-input-v3`). Being the input method, or a virtual
  keyboard, means seeing or making key strokes for all applications, so
  those two protocols join the privileged set, and the compositor starts
  the program that `[input_method] command` names as a trusted client.
  Only the system files may name it (ADR-0028). None is configured and
  none is installed in the image.
- **Screens turn off** after `[power] screen_off_seconds` without input
  (shipped: 300) and back on with any input. This is separate from the
  lock.
- **Taking focus needs the user.** An activation token that does not come
  from the focused client marks the window as wanting attention; it does
  not move the focus.

**Why.**

- On OpenGL ES the clip is part of drawing the content; the software
  renderer has no shaders. Square lower corners in tests are a difference
  in looks only.
- A material made from the wallpaper is computed once per wallpaper and
  blur radius. Blurring what really lies behind a surface would be work
  in the compositor on every frame in which that changes, and was not
  attempted. Mica, the model for the long-lived surfaces, does not
  sample the windows behind it either.
- A gamma ramp is loaded when the setting changes and when a display is
  set up, not per frame, and rendering does not change.
- Per-monitor settings in the same layered file as every other setting
  keep one mechanism: the administrator's layer, the user's layer, a
  reload.
- An input method sees what is typed everywhere. Deciding which program
  that is belongs to the administrator, like the lock screen and the
  shell.

**Consequences.**

- **A configured input method gets a trust level of its own.** It is
  offered the input-method and virtual-keyboard protocols (it sees what
  is typed and types itself) and no other privileged protocol, and its
  control connection offers what the public socket offers. An end-to-end
  test checks both. Which program it is stays the administrator's
  decision (system files only).
- **Night light needs a driver with gamma tables.** On a display
  controller without them nothing is tinted (a debug message says so).
  The nested and headless back ends have no colour pipeline: night light
  can be switched there and changes nothing.
- **No blur of windows:** a flyout over a window shows the wallpaper's
  colours, not the window's. Qt applications get the material tints as
  opaque fills (ADR-0039).
- **No protocol means a file.** A program that changes the monitors
  writes `compositor.toml` and asks for a reload. Control Center's
  Display page does exactly that (ADR-0041).
- **Still missing:** outputs on a second GPU, HDR, variable refresh rate,
  the NVIDIA proprietary driver, a screen reader interface for what the
  compositor draws.
- **Not verified yet:** everything that needs hardware. Modes, rotation,
  the gamma ramp, turning screens off, libinput's per-device settings,
  touch screens, tablets and touchpad gestures on real devices are
  compile-checked only. The clip shader is covered by no automated test,
  because the tests run on the software renderer; during development it
  was looked at once in a nested run on OpenGL ES with Mesa's software
  driver, never on a hardware driver. No input method has been run.
  Verified: unit tests of the clip geometry, the layouts, the shortcut
  table, the configuration rules and the animation timelines, and
  end-to-end tests on the headless back end that drive the window
  picker, Task View, the snap keys and the window menu through injected
  keys.

**Revisit when** hardware shows a driver on which the clip shader or the
gamma ramp fails, when translucent surfaces must show the windows behind
them, or when an application needs an output-configuration protocol.

## ADR-0037 Screen capture for other programs, behind the user's consent

Replaces ADR-0026's statement that no screen-capture protocol exists. It
is the one protocol for reading the desktop that is guarded by a question
to the user and not by client trust.

**Decision.**

- **The protocol.** The compositor implements `wlr-screencopy`, which
  screenshot tools, screen recorders and the wlroots portal back end
  use. A program asks for a picture of an output, or of a region of it,
  and gets it copied into a shared-memory buffer.
- **Who is offered it.** Every client outside a sandbox. Sandboxed
  clients (connected through the security-context protocol, as Flatpak
  does) are not offered it, and neither is the X server (ADR-0034).
- **Nobody gets a picture without the user's say.** The first frame a
  program asks for waits while the compositor asks the shell, and the
  shell asks the user: "Allow screen capture? NAME wants to see your
  screen." NAME is what the kernel calls the process.
  - Neither answer is the default; Enter alone allows nothing.
  - "Allow" works only after the dialog has been on screen for 0.7
    seconds, so a click or a key press that was meant for something else
    cannot answer it.
  - The question is answered "no" without being shown when the session
    is locked, when another dialog or the capture screen is open, or
    when no shell is listening. It counts as "no" after 60 seconds.
- **What "Allow" covers.** That program's connection, for as long as it
  keeps capturing: a recording or a shared screen goes on without more
  questions. Five seconds after its last picture the permission is gone,
  and the next request asks again. It covers every output.
- **What "Don't allow" covers.** That connection and that program name
  for twenty seconds, so that a tool which retries, or is started again
  and again, cannot nag.
- **An indicator and a stop.** While any program may capture, a red mark
  on the taskbar names it. A click on the mark ends every permission at
  once.
- **Never while locked.** Locking ends every permission and every open
  question, and no frame is filled while the session is locked.
- **The shell's own capture is separate.** The Print key, Super+Shift+S
  and the public `capture` command open the shell's capture screen. The
  shell is a trusted client and gets its picture without a question; the
  user chooses a region or the whole screen there and confirms, and the
  result goes to the Screenshots folder and the clipboard. The public
  command only opens that screen.
- **Portal.** `boswas-portals.conf` sends the ScreenCast and Screenshot
  portals to `xdg-desktop-portal-wlr` and everything else to the GTK back
  end. The wlroots back end is configured without a chooser program
  (`/etc/xdg/xdg-desktop-portal-wlr/Boswas`) and shares the first
  monitor. It reaches the compositor through this same protocol and
  therefore through the same question.

**Why.**

- **Why not by trust.** Trust has three levels, and the highest is only
  for programs the compositor starts itself (ADR-0026). A screenshot
  tool, a recorder and the portal back end are started by the user or by
  the session's service manager, so they are ordinary clients. Offering
  capture to trusted clients only would keep all of them out, and
  telling them apart by name would trust a name that any process can
  give itself.
- **Why a question in the shell.** Only a trusted client can draw an
  overlay and take the keyboard for it. The compositor decides, the shell
  only carries the question and the answer over its control connection,
  and the public control socket cannot answer.
- **What the question guards against:** a program of the user reading
  the screen silently or in the background. With the question, a capture
  the user did not start becomes visible at the moment it begins, cannot
  be confirmed by accident, is shown for as long as it lasts, and can be
  stopped.

**Consequences.**

- **It does not prove which program asks.** The name shown is the file
  name of the program's executable (not the kernel's short process name,
  which is cut to fifteen characters and which a process can set
  itself). It is still only a name: a program can be installed under any
  name. The question says that some program wants the screen now;
  whether it is the one the user just started is the user's judgement.
- **It is all or nothing.** An allowed program can take pictures of
  everything on every monitor, other applications included, until it
  stops. There is no choice of one window or one monitor, and no
  permission that is remembered.
- **Through the portal the question names the portal.** For screen
  sharing from a browser or a sandboxed application the program that
  asks the compositor is the portal back end, not the application that
  started the sharing. The portal's own dialogs, if it shows any, are not
  part of Boswas.
- **It is not a boundary against a program that already runs unsandboxed
  as the user** and can act in other ways (ADR-0028 says the same of the
  lock policy). It closes the direct, silent way to the pixels.
- **`unsafe`:** the copy into the program's buffer is the one `unsafe`
  block; the file joins the audited list (ADR-0032).
- The image gains `xdg-desktop-portal-wlr`, and the public control socket
  gains `capture`.
- **Not verified yet:** screen sharing and screenshots through the
  portal (no portal ran in the build environment); capture of a rotated
  monitor beyond the unit test of its
  region arithmetic; capture on real hardware; what the question shows
  for the portal back end. Verified: end-to-end tests with a real
  screenshot tool (`grim`) on the headless back end: no picture without
  a shell to ask, a picture of the output's size after "allow", a
  region, no picture and no second question after "don't allow", and
  nothing while the session is locked. They are skipped where `grim` is
  not installed.

**Revisit when** users need to share one window or one monitor (then: a
chooser, in the shell), when the name in the question must be something a
program cannot choose (the executable's path, or a sandbox's
application ID), or when the first test with a real portal shows what
the back end needs.

## ADR-0038 The boot screen follows Boswas UI: the mark and a ring of dots, text in Inter

Changes what ADR-0029's boot screen shows. The programs, the unlock
path, the sandbox and the failure behaviour of ADR-0029 are unchanged.

**Decision.**

- **Boot:** a solid `neutral.950` background, the Boswas mark in its own
  gold and silver, and under it a ring of five white dots that chase each
  other round. No wordmark and no name. A message from the boot scripts
  is one line under the ring; a reported progress is a percentage on that
  line.
- **Disk unlock:** the mark stays. Under it "Unlock your disk", the name
  of the disk, and a text box with the outline and the accent line of a
  focused Boswas UI control: one dot per character (24 at most, so a long
  passphrase does not show its length), a blinking caret, and a hint
  that turns into "Caps Lock is on". A rejected passphrase is reported in
  the danger colour and the box shakes for 0.4 seconds.
- **Shutdown and restart:** the ring and one line, "Shutting down" or
  "Restarting". No mark.
- **Typeface: Inter, as a glyph atlas made at build time.** `build.rs`
  rasterises the printable ASCII glyphs of Inter Regular and Inter
  SemiBold from the Debian package `fonts-inter`, which becomes a build
  dependency, and embeds the bitmaps. No font file is installed by the
  package or copied into the initramfs, and nothing is parsed at boot,
  as before. The Debian package build fails without Inter. A plain
  `cargo build` on a machine without it warns and uses Orbitron from the
  repository, so the crate still builds and its tests pass.
- **Scale rule:** one logical pixel is one display pixel up to 1080
  lines and grows with the height above that, to 3.2 at most. A display
  with fewer than 600 lines, or narrower than 480 pixels, gets a smaller
  unit so that the layout fits.
- **Damage:** an animation frame repaints only the ring, a blink only
  the caret, a keystroke or a frame of the shake only the text box, and
  rendering a frame allocates no memory.
- **`boswas.splash=static`** on the kernel command line keeps the screen
  without motion: the dots stand still, the caret does not blink, the
  box does not shake.

**Why.**

- The boot screen is the first Boswas surface a user sees and the last
  one before the login screen. With the desktop on Boswas UI v3
  (ADR-0033), the first version's wordmark, progress arc and Orbitron
  text were a different design.
- Embedding glyph bitmaps keeps ADR-0029's property that the process
  holding the disk passphrase parses nothing at boot. The two font files
  would also add 1.23 MB to the initramfs; the atlases add about 0.2 MB.
- The scale rule gives the unlock box the size a text box has on the
  desktop (320 by 36 pixels) on panels up to 1080 lines, and the same
  proportions on larger ones (640 by 72 at 3840x2160).

**Consequences.**

- `boswas-bootsplash` has a new build dependency, `fonts-inter`, and
  embeds glyph bitmaps of a font under the SIL Open Font License 1.1.
  `debian/copyright` carries the notice and the licence.
- Text is still ASCII and English; other characters are drawn as `?`.
- A build without Inter looks different from the packaged one.
  `assets::TYPEFACE` says which typeface a build has.
- 124 tests where ADR-0029 had 107, the frame-cost test among them.
- **Not verified yet:** as in ADR-0029, nothing on a real display. The
  frames were reviewed as still pictures at five sizes; nobody has
  watched the ring turn, the caret blink or the box shake on a panel.
  Caps Lock detection has run on a pseudo-terminal only. No real LUKS
  boot.

**Revisit when** the boot test or hardware shows the scale rule wrong for
a class of panels, or when localised boot text is required (then: more
glyphs in the atlas, or a font in the initramfs after all).

## ADR-0039 boswas_fluent: one Qt toolkit for Boswas applications, installed by boswas-branding

**Decision.**

- **One library for the Qt applications.** `boswas_fluent`
  (`desktop/ui/qt`) is a PySide6 (Qt 6 Widgets) library that gives an
  application the Boswas UI look from the design tokens (ADR-0033): a
  theme and a generated style sheet for the standard Qt widgets on top of
  Qt's Fusion style, and its own widgets where Qt has none (navigation
  view, settings cards and expanders, command bar, info bars, content
  dialogs, flyouts, toggle switches), the keyboard focus ring, the Boswas
  Prism icons and Inter.
- **It follows the desktop.** The scheme and the reduced-motion setting
  come from the `[appearance]` table the compositor reads; the two user
  files are watched, and a change re-styles every open window.
- **Rules it makes checkable.** No colour, radius, font size or duration
  outside the theme (the library's tests fail if one of its modules
  contains a colour value). Text from outside is plain text. Nothing
  blocks the GUI thread. Applications refuse to run as root and start
  other programs with argument lists only. `desktop/ui/qt/README.md` is
  the reference for application authors.
- **Where it is installed.** `boswas-branding` installs it as
  `/usr/lib/boswas/python/boswas_fluent`, the private module directory
  the Boswas programs already put on their path (ADR-0010), and depends
  on the PySide6 modules it uses: QtCore, QtGui, QtWidgets, QtSvg,
  QtDBus and QtNetwork.
- **First users.** Control Center and the Compatibility Manager drop
  their own style sheets and use it.

**Why.**

- Two applications had two style sheets, and each further Qt application
  would have brought another. One library with the tokens behind it is
  the idea of ADR-0031 applied to Qt: the look is data and is tested
  once.
- KDE's Breeze style with a colour scheme gives KDE applications the
  Boswas colours, not the Boswas geometry or controls.
- `boswas-branding` already installs what the toolkit reads at run time,
  the token file and the icon packs, so the library and its data arrive
  and are updated together.

**Consequences.**

- `boswas-branding` is no longer a pure data package: it depends on
  PySide6, and `boswas-compat-manager` now depends on it.
- The library reads the installed token file at run time, unlike the
  Rust programs: a token change reaches the Qt applications with an
  update of `boswas-branding` alone.
- **Not implemented:** real mica and acrylic (windows and popups are
  filled with the material tints, opaque: blurring is the compositor's
  job and Qt cannot ask for it); shadows of menus, combo box popups and
  tooltips; animated state changes of standard widgets; the urgency and
  the action buttons of a notification sent through `fluent.notify`. The
  README lists the rest.
- KDE applications (Dolphin, Konsole and the others) do not use it. They
  keep the Breeze style with the Prism colour scheme, so two looks were on
  the desktop until the system applications moved to the toolkit as well
  (ADR-0040).
- Window frames are the compositor's; the toolkit draws none.
- **Not verified yet:** on a real session. Its tests run on Qt's
  offscreen platform; the widget tests skip themselves where PySide6 is
  not installed, which is the case in the package build of
  `boswas-branding` (PySide6 is not a build dependency).

**Revisit when** a Boswas application needs a toolkit other than Qt
Widgets, or when the compositor can give a client a blurred backdrop.

## ADR-0040 The system applications are Boswas programs on boswas_fluent, in one package (boswas-apps)

Replaces the part of ADR-0026 that kept KDE's applications as the
system applications.

**Decision.**

- **Boswas writes the system applications itself.** They are PySide6
  programs on the toolkit `boswas_fluent` (ADR-0039), in the package
  `boswas-apps`. The image installs them in place of the KDE
  applications, which leave the package list; KDE applications still run
  if someone installs them.

  | Application | Program | Takes the place of |
  |-------------|---------|--------------------|
  | Files | `boswas-files` | Dolphin |
  | Terminal | `boswas-terminal` | Konsole |
  | Text Editor | `boswas-text-editor` | KWrite |
  | Calculator | `boswas-calculator` | KCalc |
  | Photos | `boswas-photos` | Gwenview |
  | Document Viewer | `boswas-document-viewer` | Okular, for PDF |
  | Archive Manager | `boswas-archive-manager` | Ark |
  | Media Player | `boswas-media-player` | nothing: the image had no player |
  | Camera | `boswas-camera` | nothing: the image had no camera application |
  | Clock | `boswas-clock` | nothing |
  | Calendar | `boswas-calendar` | nothing (the KDE PIM suite was never in the image) |
  | Task Manager | `boswas-task-manager` | plasma-systemmonitor, which left with Plasma |
  | Reminders | `boswas-reminders` | a session service without a window: it shows alarms, timers and calendar reminders as notifications while Clock and Calendar are closed |
- **One package, one module directory.** Each application is a
  sub-package of `boswas_apps` in the private module directory
  `/usr/lib/boswas/python` (ADR-0010), with a launcher
  `/usr/bin/boswas-<name>` that runs Python isolated (`python3 -IB`), a
  desktop entry `com.boswas.<Name>.desktop` whose name is also the
  window's application ID, a manual page and its own test suite. At run
  time the applications share nothing but the toolkit.
- **The same rules for all of them:**
  - **As the user.** Every application refuses to start as root.
  - **No shell.** Other programs are started with an argument list. The
    terminal starts the login shell, or the command after `-e`, directly
    on a pseudo-terminal.
  - **What comes from outside is hostile.** File names, file contents and
    what a program prints are parsed by bounds-checked code with size
    caps, and nothing is evaluated or unpickled. Photos reads EXIF and
    the Media Player reads tags with parsers of their own. The Text
    Editor refuses files that are not text, asks above 32 MB and refuses
    above 512 MB. Photos shows pictures above 48 megapixels from a
    reduced copy.
  - **Opening is not running.** Files starts a program, an executable
    script or a desktop entry only after a question. The Document Viewer
    shows the address of a web link and opens it only on request.
  - **Extraction cannot leave its folder.** The Archive Manager walks
    paths from a descriptor of the destination without following
    symbolic links, refuses `..`, makes absolute paths relative, creates
    symbolic links last, skips devices, drops set-user-ID bits, and stops
    at a size, ratio and count that would mean a decompression bomb.
  - **Deleting goes to the Trash.** Files and Photos move files to the
    freedesktop.org Trash and can undo that. Deleting for good is a
    separate key and asks first.
  - **Saving replaces atomically:** a temporary file in the same folder,
    then a rename (Text Editor, Photos, Archive Manager).
  - **Nothing blocks the window.** Long work runs on worker threads with
    progress and a way to cancel.
  - **No network of their own,** except where it is the function: the
    Media Player plays http and https addresses the user gives it. The
    Calculator has no currency converter for that reason.
  - **Python's standard library and PySide6 only.** The terminal's
    emulator, the EXIF reader and the tag readers are part of the
    applications. For 7z and RAR archives the Archive Manager runs `7z`
    where it is installed.
- **Desktop defaults.** Ctrl+Alt+T, Super+E and Ctrl+Shift+Esc start the
  Boswas terminal, file manager and task manager; the taskbar and Start
  of a new user pin the Boswas applications; a desktop entry that asks
  for a terminal runs in `boswas-terminal`, which the package also
  registers as Debian's `x-terminal-emulator`. Files answers
  `org.freedesktop.FileManager1`, so other programs can show a file in
  it. The reminders service starts with the Boswas session through an
  XDG autostart entry.

**Why.**

- **One look and one behaviour.** KDE's applications keep the Breeze
  widgets with the Prism colours (ADR-0039): next to the shell they are
  a second design. The Boswas applications take the Fluent layout,
  light and dark, and reduced motion from the toolkit, and follow their
  Windows 11 counterparts in layout and shortcuts, which is the target
  of ADR-0033.
- **Rules that can be read and tested.** "Never as root, never through a
  shell, never trust a file" is a property of code Boswas owns and tests,
  application by application.
- **One stack.** Control Center and the Compatibility Manager are PySide6
  programs already (ADR-0021); the applications add no second toolkit and
  no new language.
- **One package** keeps the applications on one version of the toolkit
  and one release step.

**Alternatives.** Keeping KDE's applications, as the first version of
the native desktop did. They are mature and more complete, and Debian's
security team covers them. That was given up for the single look and the
single set of rules.

**Consequences.**

- **Boswas maintains them.** A terminal emulator, a file manager, an
  archive reader and parsers for EXIF and media tags are now Boswas
  code. Debian's security support covers Qt, PySide6 and FFmpeg beneath
  them, not this code.
- **One program runs in the background.** `boswas-reminders` stays in
  the session so that an alarm rings and a reminder shows while Clock and
  Calendar are closed. No other application keeps running after its
  window closes.
- **They do less than what they replace.** Left out on purpose, by
  application:
  - Terminal: split panes, sixel graphics, the kitty keyboard protocol,
    an audible bell, screen-reader access to the text, shaping across
    cells (Indic clusters are drawn cell by cell).
  - Files: thumbnails for videos and PDFs, password-protected ZIP
    archives, view settings per folder (layout and sort order are the
    same everywhere).
  - Text Editor: syntax highlighting, spell checking. Saving by rename
    leaves other hard links to a file with the old content.
  - Calculator: graphing, currency conversion; memory and history do not
    outlive the window.
  - Photos: XMP and IPTC data are lost when a picture is saved (the
    window says so); an edited picture above 48 megapixels is saved
    reduced; AVIF, HEIF and JPEG XL need `kimageformat6-plugins`.
  - Document Viewer: PDF only; no annotations (Qt PDF cannot write); no
    selection across pages.
  - Archive Manager: no encrypted archives are created, nothing is
    renamed inside an archive; 7z and RAR need the `7zip` package.
  - Media Player: no tags for Matroska, AVI, WMA and AIFF files; the mini
    player does not stay on top.
  - Calendar: a calendar kept on this computer only, with iCalendar
    import and export; no accounts and no synchronisation; no printing.
    Events of a hidden calendar do not remind.
  - Clock: no choice of alarm sound.
  - Task Manager: no network column per process, no way to disconnect
    another user's session.
- **What can be played or shown follows Qt:** media through Qt
  Multimedia's FFmpeg back end, PDF through Qt PDF, pictures through Qt's
  image plugins.
- **Printing means a PDF file for now.** The applications print through
  Qt's print dialog, and the image has no print service (the manifest
  does not select CUPS).
- **Not verified yet:** none of them has run on the Boswas desktop. The
  build environment runs as root, which the applications refuse, so they
  were exercised through their tests on Qt's offscreen platform and not
  through their launchers. The Camera has run against a stand-in camera
  only. No printer, no real removable drive with udisks and polkit,
  no PDF from outside the tests, no audible sound. `verification.md` has
  the list per application.

**Revisit when** an application needs more than Qt Widgets can give, when
maintaining one of them costs more than it returns (then: the Debian
application again, with the two looks), or when a format the business
needs is beyond Qt (office documents, for example, which no application
here opens).

## ADR-0041 Settings are native sections of Control Center, on two backends

Replaces ADR-0026's "native settings pages are planned work".

**Decision.**

- **Sections instead of KDE modules.** The pages of Control Center no
  longer show tiles that start Plasma's settings modules. Each area holds
  its settings in the window itself, as sections with rows that apply at
  once. No page depends on a program of another desktop: every KDE helper
  is gone from the code (a safety test fails if one is named), and
  applications are started with `gio launch`.
- **The section contract.** A section is a `QWidget` subclass with four
  class attributes (`page`, `title`, `order`, `keywords`) and two methods:
  `activate()` when its page becomes visible and `deactivate()` when it
  is hidden. Building it does no input or output. The registry
  (`ui/settings/__init__.py`) imports a fixed list of modules; a module
  that fails to import or a class that breaks the contract is left out
  with a log line, a section that fails becomes a message on its page,
  and a page with nothing on it is hidden. The sidebar search finds
  sections by title and keyword, and `--page` with `--section` opens one
  directly, which the shell's quick settings use.
- **Backend one: the service bridge** (ADR-0035), for Wi-Fi and other
  connections, Bluetooth, sound, battery, power mode and brightness. One
  `boswas-services-bridge` runs per window, from the first page that
  needs it until the window closes. Quick settings and Settings therefore
  show the same state. A switch shows the requested value until the
  service confirms it and goes back with a message when it does not.
- **Backend two: the compositor's configuration,** for monitors, night
  light, mouse, touchpad, keyboard, colour mode, background, motion, the
  lock timeout and the screen-off timeout. Settings writes the user's
  layer (`~/.config/boswas/compositor.toml`, and the colour mode and
  background to `appearance.toml`), then sends `reload-config` to the
  public control socket and reads the monitors with `outputs`
  (ADR-0036). A display change that could leave the user without a
  picture asks "Keep these display settings?" and reverts after fifteen
  seconds. With no desktop running the settings are saved and apply at
  the next sign-in.
- **The rest uses the system's own interfaces:** `timedatectl` and
  `localectl` in fixed forms for date, time, region and language (for a
  change that needs administrator rights systemd's services ask polkit,
  and the shell's agent shows the question); `mimeapps.list` for default
  applications; `~/.config/autostart` for start-up applications.
- **User accounts go through AccountsService.** The Users page asks the
  service with `busctl` and validated arguments for everything that is
  not a secret; polkit decides, and the shell's agent asks. A person's
  own password is changed by `passwd` on a pseudo-terminal, so the
  system's password rules apply. Another user's password, or a new
  user's, is turned into a SHA-512 crypt hash in Control Center and given
  to the service over D-Bus. No password and no hash is ever part of a
  command line, and nothing typed is kept or logged. The account that is
  signed in and the last administrator cannot be removed, and the last
  administrator cannot be made a standard user. Without AccountsService
  the page lists the accounts of `/etc/passwd` read-only, and only the
  own password can be changed.
- **One audited file writer.** `userfiles.py` stays the only module of
  Control Center that writes or deletes files (a safety test enforces
  it). Its `write_config` accepts a short allow-list of names
  (`boswas/compositor.toml`, its backup, `boswas/appearance.toml`,
  `mimeapps.list`, `autostart/<id>.desktop`), replaces the file
  atomically with mode 0600, and writes nothing through a symbolic link.
- **What stays the administrator's.** Settings never writes `[policy]`,
  `[shell]`, `[input_method]` or `[lock] command`; the compositor would
  ignore them in a user's file anyway (ADR-0028). The lock timeout it
  offers ends at the policy's maximum, and the page says that the
  organisation set the limit.
- **A damaged file is ignored alone.** The compositor now checks each
  configuration file on its own: a file with an unknown key or a value of
  the wrong kind is reported and ignored as a whole, and what the other
  files say stays in use. Before, such a file in the user's directory
  made the compositor fall back to its built-in settings for everything,
  which also dropped an administrator's stricter lock policy.

**Why.**

- The Plasma modules left with Plasma (ADR-0026), and their tiles were
  dead.
- The shell already had backends for the network, Bluetooth, sound and
  power (ADR-0035). A second implementation in Python would have had to
  repeat the handling of secrets and of what the daemons send, and the
  two could have disagreed about the state.
- Display and input settings have one home, the compositor's layered
  file (ADR-0036). Writing the user's layer and asking for a reload
  needs no new protocol and no new privilege: the reload re-reads files
  the user controls anyway, and the policy layer is out of reach.
- One writer keeps the answer to "which files can this program change?"
  short enough to review.

**Consequences.**

- **Settings rebuilds the user's file.** Every documented key the user
  had is kept; comments and anything the compositor does not know are
  not. When something cannot be kept, the previous file is copied to
  `compositor.toml.bak` first and the user is told.
- **Passwords set for others bypass PAM.** They are SHA-512 crypt (5000
  rounds), not the yescrypt that Debian 13 uses by default, because the
  hash is made in Control Center. Because PAM does not see them, the
  dialog reads the device's `pwquality` settings and applies their
  minimum length and number of character classes itself (12 characters
  and two classes with the security baseline) and refuses the user name;
  the other `pam_pwquality` checks are not made on this path. A person's
  own password goes through `passwd` and PAM and gets the system's
  scheme and rules.
- **Left out:** editing a proxy (shown read-only), managing VPN
  connections (status only), a test sound, an automatic suspend time (the
  compositor has no key for it), identifying monitors, a pointer theme,
  drawing tablets, more than four keyboard layouts (the limit of XKB),
  locking an account.
- `boswas-control-center` no longer depends on `kde-cli-tools` and
  `libkf6kcmutils-bin`; it depends on `libglib2.0-bin`, `systemd`,
  `passwd`, `tzdata`, `xkb-data` and the QtNetwork and QtDBus modules
  instead, and recommends `accountsservice`.
- **Not verified yet:** every section that talks to a system service ran
  against stand-ins; the real bridge reported "not available" in the
  build environment, which has no NetworkManager, BlueZ, sound server or
  polkit. The display and input sections have three opt-in tests against
  the real compositor on its headless back end, which has one output and
  ignores a scale change on reload: arranging several monitors is tested
  against a model only. The Users page has seen no AccountsService, no
  polkit and no PAM: the shape of `busctl`'s replies, the conversation
  with `passwd` (a PAM module that opens the terminal itself would not be
  answered) and signing in with an account it created are all untried.

**Revisit when** the first run on a real system shows that the
conversation with `passwd` or AccountsService differs, when passwords set
for others must use the system's hashing scheme, when an administrator
must lock a setting that is not policy today, or when settings have to
follow a user between devices.

## ADR-0042 Nothing is built for KDE programs that are not shipped; the presets are native only

Follows ADR-0026 (Plasma, KWin and SDDM left the image) and ADR-0040
(KDE's applications left it). Until now the tree still generated,
installed and tested data and code for four of those programs.

**Decision.**

- **Removed: Plasma.** The ten Global Themes of the presets
  (`desktop/presets/look-and-feel/`, installed under
  `/usr/share/plasma/look-and-feel`), their panel layout scripts and
  previews, and the JPEG preview that made `netpbm` a build dependency of
  `boswas-branding`. In `boswas-preset`: the Plasma branch
  (`plasma-apply-lookandfeel`, `plasma-apply-wallpaperimage`, the
  `kscreenlockerrc` entry), the `--layout` option, the test of
  `XDG_CURRENT_DESKTOP` that chose between two ways of applying, and the
  fallback of `current` to an active Global Theme. In `presets.json`
  (now schema 2): `icon_theme`, `plasma_style` and `panel`; the one thing
  the preview picture needed of `panel` is now `preview`. In Control
  Center: the "Also apply the panel layout" box and the reset that
  applied Horizon with its panels.
- **Removed: Konsole.** The colour schemes and profiles of the ten
  presets and of Prism (`/usr/share/konsole`), the default profile
  (`konsolerc`), the step of `boswas-preset` that switched it, and the
  `terminal` palettes of `presets.json`. The terminal palette of Prism
  stays as a table in `desktop/ui/tools/build_kde_colors.py`, because
  Boswas Terminal draws with it and its tests compare the two.
- **Removed: System Settings and Dolphin.** `kcm-about-distrorc`, the
  file of the About module of Plasma's System Settings, and the service
  menu the Compatibility Manager installed for Dolphin
  (`/usr/share/kio/servicemenus`).
- **`boswas-preset` starts no program.** It ran KDE's `kwriteconfig6` to
  record the applied preset (`[Preset] Current` in `~/.config/boswasrc`).
  It now edits that file itself and keeps every other group and key, so
  `boswas-branding` no longer depends on `libkf6config-bin`. Files
  written by earlier versions have the same format and are read as
  before.
- **One careful way to write the user's files.** `appearance.toml`,
  `kdeglobals` and `boswasrc` are written the same way: the new content
  of all three is prepared before the first is written; each is replaced
  atomically (a new file with mode 0600 and an unpredictable name in the
  same directory, synced, renamed); a target that is a symbolic link or
  not a regular file is refused, and so is a link in the place of
  `~/.config/boswas`. This is what Control Center's `userfiles.py` does
  for the same directory (ADR-0041).
- **What stays, for KDE applications a user installs.** They run as
  ordinary clients, so three things remain: the `.colors` schemes in
  `/usr/share/color-schemes` (the ten presets and the two Prism
  schemes); the default `kdeglobals` in `/usr/share/boswas/kde-settings`
  (Prism colours, the Boswas Prism icons, and the Breeze widget style
  where the user has that style installed: the image no longer brings
  `kde-style-breeze`, ADR-0043), which `boswas-session` appends to
  `XDG_CONFIG_DIRS`; and the step of `boswas-preset` that merges a
  preset's colours into the user's `kdeglobals`. The icons of the pack
  that are named after KDE applications stay as well.
- **Windows installers from Boswas Files.** In the place of the service
  menu the Compatibility Manager ships a desktop entry,
  `com.boswas.CompatibilityManager.Install.desktop`: hidden from the
  menu, declaring the four types of Windows programs and installer
  packages, running `boswas-compat-manager --install %f`. File managers
  offer it under "Open with". "Run with Boswas" (ADR-0021) stays what
  opens such a file, by two means. The file name of the new entry sorts
  after `boswas-winapp-install.desktop`, which is the order of
  `mimeinfo.cache` that `xdg-mime` and GLib fall back to. And the package
  ships `/usr/share/applications/boswas-mimeapps.list`, the defaults of
  the Boswas desktop for these four types, because Boswas Files orders
  the applications of a type by name and would otherwise open with the
  new entry. A user's own choice still comes first.

**Why.**

- **It was dead weight that looked alive.** More than seventy installed
  files and three test modules described a desktop nobody can start from
  the image. A reader of the tree could not tell which path a preset
  really takes.
- **A tool that runs another desktop's programs depends on them.** One
  key in one file kept a KDE Frameworks tool in the dependencies of the
  package every Boswas application needs.
- **The old writers were not careful.** They wrote to a temporary file
  with a fixed name, followed a symbolic link when reading, replaced a
  link by a file without saying so, and could stop after the first of
  the files had changed.
- **A second handler for a type is a risk to the default.** Nothing in
  the tree named a default application for Windows programs: the order
  of names decided. Adding an entry without fixing the default would
  have changed what a double click does in Boswas Files.

**Consequences.**

- `boswas-preset apply ID --layout` is a usage error now, and the JSON of
  `apply` has no `layout` field. `current` no longer reports a preset
  because of a `LookAndFeelPackage` left in a `kdeglobals`.
- The three user files get mode 0600 (they had the mode the umask gave).
  A user whose `kdeglobals`, `boswasrc` or `appearance.toml` is a
  symbolic link, as some dotfile managers make them, gets an error that
  names the file; earlier versions replaced the link by a file.
- A preset no longer changes anything in Konsole, and Konsole or Plasma
  installed by hand find no Boswas profile or Global Theme.
- `/usr/share/applications/boswas-mimeapps.list` belongs to
  `boswas-compat-manager`. Another default application of the Boswas
  desktop needs a line in that file, or the file moved to another package.
- **Left as they are:** the wallpaper directories keep their layout
  (`metadata.json`, `contents/images`, `contents/images_dark`), which
  Control Center reads. The lock screen art, the login background and
  `boswas-about-logo.svg` are still installed and nothing reads them. The
  preview pictures still sketch the panel as the Plasma image had it.
  (The live-session script's lines for `kcm-about-distrorc` were removed
  with ADR-0043.)
- **Not verified yet:** no preset was applied in a running Boswas
  session, and the entry was not tried from Boswas Files on a device.
  Which application is the default was checked in a scratch directory
  only: with `update-desktop-database` 0.27, `gio mime` (GLib 2.80) and
  `xdg-mime` 1.1.3, each with and without the list of defaults, and with
  the application index of Boswas Files in a test. The versions of
  Debian 13 on an image were not run.

**Revisit when** presets must recolour the shell (ADR-0031), a KDE
application becomes part of the image again, the Boswas desktop needs
defaults for more file types than these four, or users with linked
configuration files need presets to follow the link.

## ADR-0043 The pointer theme is Boswas's own (Boswas-Cursors); no KDE library is left on the image

Follows ADR-0031 (the icon pack) and ADR-0042. After those, two things on
screen and on the image still came from KDE: the pointer (`breeze_cursors`
from `breeze-cursor-theme`) and the widget style `kde-style-breeze`, kept
for KDE applications the image no longer has.

**Decision.**

- **Boswas-Cursors.** `desktop/cursors/` holds an XCursor theme drawn for
  Boswas OS: one family (a white arrow with a dark outline and a soft
  shadow), 28 drawings, every CSS cursor name the compositor asks for, the
  X11 and toolkit names as symbolic links, and a turning ring for `wait`
  and `progress` (24 frames of 40 ms). Like the icon pack it is generated:
  `tools/build_cursors.py` writes the SVG sources, which are committed and
  checked against the generator; `install.sh` renders them with
  `rsvg-convert` at the nominal sizes 24, 32, 48, 64 and 96 and writes the
  XCursor files with a writer of its own (`tools/xcursor.py`), so the build
  needs no `xcursorgen`. The colours are token references
  (`desktop/ui/tokens.json`).
- **Where it applies.** `boswas-branding` installs the theme in
  `/usr/share/icons/Boswas-Cursors`. It is the compositor's default
  (`[pointer] cursor_theme` in the shipped `compositor.toml` and
  `greeter.toml`) and the `XCURSOR_THEME` the session and the login screen
  export. For programs that load the theme called `default` instead (GTK
  and X11 clients), the package registers the theme as Debian's
  `x-cursor-theme` alternative with priority 150, above Adwaita (90) and
  Breeze (102); the alternative is removed with the package. The package
  also ships the directory of the alternative's link
  (`/usr/share/icons/default`), as Debian's cursor themes do, so the
  registration does not depend on one of them being installed. A user's
  choice in Settings (`cursor_theme`, `cursor_size`) is unchanged.
- **What left the image.** `breeze-cursor-theme` and `kde-style-breeze`
  are no longer in the package lists, and `boswas-desktop` no longer
  recommends the former. No package of the KDE Frameworks is installed by
  a Boswas list or dependency any more (`libkf6config-bin` went with
  ADR-0042). `breeze-icon-theme` stays: it is data only, the fallback for
  icons of programs that are not Boswas's.
- **Icons the applications asked for.** The Prism pack gained 36 symbolic
  icons (300 designs, 535 files), so every icon name that the system
  applications, Settings and the Compatibility Manager request is drawn in
  the pack and none falls back to Breeze. Boswas Photos and Boswas
  Document Viewer use their own names (`boswas-photos`,
  `boswas-document-viewer`) instead of the names of the KDE programs they
  replaced.
- **Settings' copy of the compositor's defaults** named Konsole and Dolphin
  as terminal and file manager. It names Boswas Terminal and Boswas Files
  now, and a test compares the copy with the file the compositor ships.

**Consequences.**

- The installed theme is about 9 MB; `wait` and `progress` are 2.9 MB each
  (24 frames at five sizes, uncompressed as XCursor files are).
- A KDE application a user installs draws with Qt's own style unless the
  user also installs a KDE style. The shipped `kdeglobals` still names
  Breeze, which takes effect where that style is present.
- Eight one-way resize names and the edge names link to the four double
  arrows; there are no one-way arrows.
- A change of `white`, `neutral.950` or the light accent in the tokens
  needs the sources regenerated; the install script and the tests refuse
  stale sources.

**Verified.** The theme's 82 tests (the sources match the generator; every
name resolves after installation; the files are read back by libXcursor
and match `xcursorgen` byte for byte; two installations are identical). The
packaged compositor, run headless as an unprivileged user from the built
packages, drew the arrow and the text cursor of this theme.

**Not verified.** The pointer on a hardware cursor plane, at a fractional
scale or on a HiDPI display; Xwayland, GTK and Qt clients picking the theme
up in a real session; the alternative on a Debian 13 image; twelve of the
32 legacy hash names, which are from memory (listed in the theme's README).

**Revisit when** a designer reviews the shapes, or a display scale shows
that more sizes are needed.

## ADR-0044 A boot always ends at a way to sign in

**Context.** On 2026-11-16 the image was started and stopped at a black
screen with a blinking text cursor. The packages were then installed in a
virtual machine (QEMU without hardware acceleration; an Ubuntu 24.04 root
file system with systemd 255, greetd 0.9.0 and libseat 0.8.0, because the
build environment cannot reach a Debian mirror) and booted until every
step from the boot loader to the desktop had been seen.

The cause on the live medium: the live-config component of the image wrote
greetd's automatic sign-in for the user in `LIVE_USERNAME`. Nothing set
that variable, so it held live-config's own default, `user`, while the
live user of the image is `boswas`. greetd refuses a session for a user
that does not exist, the unit failed five times in a few seconds, systemd
stopped starting it, and the first console had no login of any kind: the
greetd drop-in conflicts with `getty@tty1`.

The boots showed more ways to the same screen:

- logind activates a session only when polkit allows it. Without
  `polkitd` (it was a recommendation) libseat fell back to a mode in which
  an unprivileged compositor cannot open the graphics device.
- Without `/etc/fonts/fonts.conf` (package `fontconfig-config`, which
  nothing depended on) or without a font, the shell ended on its first
  line of text. The compositor started it again without a pause, for
  ever, behind a black screen.
- The text login that `boswas-greeter` fell back to started
  `boswas-session`, the desktop that had just failed.
- The fallback applied only to a compositor that failed within ten
  seconds; one that started and then had nothing to show never ended.
- The boot screen drew only its moving ring after the display's driver
  had replaced the firmware frame buffer, could not open a graphics device
  that appeared after it had entered its sandbox, and could be left
  running, holding the console, by a second start or a daemon that did
  not answer.

**Decision.** A boot ends at the login screen, or at a text login, never
at a console without one. Each layer has a way down to the next.

- **The live user.** `config/boswas/live-config.conf` sets
  `LIVE_USERNAME="boswas"`, the one place the name is written. The
  component reads `username=` and `noautologin` from the kernel command
  line as live-config's own components do (each component is a process of
  its own and parses them itself), and writes an automatic sign-in only
  when that user exists. Otherwise the login screen is shown.
- **Exit statuses of the compositor** are a contract with the login
  screen's start script: 0 the session ended as asked, 1 any other error,
  2 usage, 3 it could not start (no usable seat, no graphics device within
  the wait, or, for the login screen, no output), 4 the login screen kept
  failing (three starts in a row that each ended within 30 seconds). One
  line on standard error says what was wrong.
- **From the login screen to greetd's text login.** `boswas-greeter` runs
  `agreety` when the compositor ends with 3 or 4, or with any other
  failure within 30 seconds of its start; a compositor that fails later is
  started again by greetd. The text login starts
  `/usr/libexec/boswas/boswas-text-session`: a login shell with a note
  that says how to start the desktop (`boswas-session`) and where the log
  is. It does not start the desktop itself.
- **From greetd to getty.** The greetd drop-in names
  `OnFailure=boswas-login-fallback.service`. systemd triggers it at every
  failed attempt, so the unit waits until greetd is either running again
  or has failed for good, and only then starts `getty@tty1.service`.
  Naming getty directly in `OnFailure=` was tried first and failed in the
  virtual machine: getty was started and stopped with every restart of
  greetd until it ran into its own start limit.
- **What the login screen cannot start without is a dependency.**
  `boswas-desktop` depends on `polkitd`, `fontconfig-config`,
  `fonts-inter` and `libpam-systemd`. The first and the third were
  recommendations; the other two came with other packages by chance.
- **The shell and fonts.** The text engine looks in `/usr/share/fonts`,
  `/usr/local/share/fonts` and the user's font directories when
  fontconfig yields nothing. With no font at all the desktop shell and
  the login screen end with status 1 and one line that names the remedy.
  The lock screen stays, without text: ending it would leave a session
  that cannot be unlocked.
- **The shell under the compositor.** A shell that ends is started again
  at once; one that keeps ending within 30 seconds of its start is
  started after a pause that doubles from 2 seconds to a minute. A
  desktop session is never given up, because the user's applications are
  in it. The login screen is given up after three failed starts
  (status 4). The login screen's compositor starts no X server.
- **Seat and graphics device.** The compositor waits up to 4 seconds for
  the seat to be enabled before it opens a device, and up to 10 seconds
  for a graphics device that is not there yet. When the device in use
  goes away (the firmware frame buffer, replaced by the display's own
  driver) it takes the next one and the windows stay. libseat's messages
  are in the compositor's log. A login screen without an output for 5
  seconds ends with status 3; a desktop session waits for a monitor.
- **Software rendering.** On a device without a hardware renderer the
  compositor waits for the frame to be complete before it is shown.
  Before, a frame could be put on a firmware frame buffer half drawn and
  the missing parts were not drawn again.
- **End of the login screen.** The greeter says `quit` and closes its
  connection in one step; the compositor dropped the request with the
  connection and was stopped by greetd five seconds later. Requests sent
  before the close are carried out.
- **The session script** skips a profile file with a syntax error instead
  of ending with it.
- **Messages go to the journal.** The compositor writes to standard
  error, which greetd leaves on the terminal: its lines were visible on
  the console between the boot screen and the login screen, and were in no
  log afterwards. `boswas-greeter` and `boswas-session` start the
  compositor through `systemd-cat` (identifiers `boswas-greeter` and
  `boswas-session`) when the journal is there; the exit status stays the
  compositor's. The text session's note names both.
- **The boot screen** (`boswas-bootsplash`, follows ADR-0029 and
  ADR-0038):
  - It draws the whole picture again after it has set a mode on a display.
  - Its supervisor, which is outside the sandbox, opens graphics devices
    for the sandboxed worker and passes them over the socket pair between
    them: only character devices with the DRM or frame buffer major
    number, opened relative to a `/dev` descriptor held from the start.
    The worker's Landlock rules gain the console node and nothing else,
    so `/dev` with its disks stays closed to it.
  - One boot screen at a time: the supervisor holds
    `/run/boswas-bootsplash/lock`. A second start leaves a running one
    alone. `quit` terminates a daemon that does not answer within three
    seconds, and the hand-over unit no longer depends on a pid file.
  - It never puts a console back into text mode, and never takes a
    display back, once another program runs a graphical session there.
  - Raw keyboard mode is put back when the console is reset during boot,
    so Escape keeps working after the switch to the real root.
  - It stops drawing on a legacy frame buffer that has been replaced and
    moves to the DRM device.
  - The initramfs hook copies the DRM drivers itself, with the selection
    Debian's Plymouth hook makes (Debian's initramfs-tools has no
    `FRAMEBUFFER` option; `MODULES=dep` takes this machine's drivers
    only). The start unit lets the daemon wait up to eight seconds for a
    display that is not there yet.
  - Emergency and rescue mode end the boot screen before their shell
    starts (drop-ins for `emergency.service` and `rescue.service`; ADR-0006).
- **Boot loader.** On BIOS machines the live medium's menu loads GRUB's
  video modules and asks for a 1024x768 mode for the kernel when none is
  set; installed systems keep GRUB's mode (`GRUB_GFXPAYLOAD_LINUX=keep`).
  The boot screen then has a frame buffer before the display's driver is
  loaded.

**Why.** The failure that was reported had one cause, but the system had
no layer that could fail without taking the login with it. A managed
device that shows a cursor and nothing else cannot be repaired by the
person in front of it, and cannot be reached to find out why. A text
login is a poor desktop and a sufficient way in.

**Consequences.**

- `polkitd` and `fonts-inter` are installed wherever `boswas-desktop` is.
- An image built with a wrong live user name shows the login screen
  instead of the desktop. The image check fails the build when
  `LIVE_USERNAME` is not set in the installed settings file or the
  component does not read `username=`.
- With the DRM drivers and their firmware the initramfs is as large as
  one made for Plymouth. In the virtual machine, whose firmware package
  holds every vendor's files, it grew from 65 MB to 143 MB; the logo
  appeared after the same 11 seconds as before.
- The boot screen's last frame does not stay until the login screen
  draws: greetd switches to and clears the console first, so the display
  is an empty console for the moment the compositor needs to start.
- The boot screen refuses a 24-bit legacy frame buffer (it draws on the
  DRM device in that case), and a frame buffer device that a DRM driver
  emulates did not show what was drawn into it in the virtual machine.
- **Verified** by boots of the built packages in the virtual machine:
  the login screen and a sign-in to the desktop; an automatic sign-in;
  the text login when the automatic sign-in names a missing user, without
  polkit, with the shell missing, under `nomodeset` and when greetd
  fails; the login screen without the font configuration; a graphics
  driver that loads late, or replaces the firmware frame buffer before
  or under the running compositor; a complete desktop on the firmware
  frame buffer; the boot screen items above. `tests/static/` runs the
  live-config component and the greeter script with stand-ins.
- **Not verified:** Debian 13 itself (its initramfs-tools, its libseat
  and greetd versions, its package names), the ISO build and the live
  medium as built, the boot loader changes, real hardware, a login screen
  without any output, the boot screen's hand-over to a compositor other
  than through a stand-in, rescue mode, and a desktop shell that keeps
  failing on a booted system (tests only).

**Revisit when** the image is built and booted on Debian 13, and when
hardware validation shows a path to a screen without a login that this
list does not cover.
