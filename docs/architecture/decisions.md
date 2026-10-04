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
packages built from `packages/` (five since 1.0~alpha2):

- `boswas-os`: release identity and `/etc/boswas`; depends on the others.
- `boswas-cli`: `boswas`, `boswas-info`, `boswas-status`.
- `boswas-branding`: KDE look-and-feel, wallpaper, login, About and icons.
- `boswas-security`: the security baseline.
- `boswas-compat`: WinCompat (`boswas-winapp`, the Wine AppArmor profile,
  the compatibility catalog and its policy); since 1.0~alpha2.

The packages reference shared sources in the monorepo (`security/`,
`desktop/`, `compatibility/`, `config/boswas/`). `debian/rules` installs
every file with an explicit mode. `packages/boswas-device-agent/` holds the
agent's interfaces and has no `debian/` directory until Milestone 2, so it is
not built into the image.

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

## ADR-0005 Debian identity is preserved

**Decision.** `/etc/os-release` stays Debian's (`ID=debian`). Boswas identity
lives in `/usr/lib/boswas/release`, `/usr/lib/boswas/image-info`,
`/etc/issue.d/boswas.issue` and the KDE About page (`kcm-about-distrorc`).
`GRUB_DISTRIBUTOR` is **not** changed.

**Why.**

- Vendor software, scripts and Debian tooling check `ID=debian`.
- Keeping it preserves Debian attribution, as the Debian derivative and
  trademark guidance asks.
- Changing `GRUB_DISTRIBUTOR` renames the EFI directory, but Debian's
  *signed* GRUB looks for its configuration in `/EFI/debian`. Rebranding it
  would break Secure Boot boots.

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

## ADR-0008 Brand lettering is traced from official artwork

**Decision.** The Boswas Group typeface is not available as a font file, so
the brand lettering is vectorised from the official logo images with potrace
(`desktop/branding/tools/trace_brand.py`).

- **Lettering:** "BOSWAS OS" is composed from the traced glyphs. B, O, S, W,
  A, G, R, U and P are available.
- **Secondary text:** Lato.

**Revisit when** Boswas Group supplies the original font file with a licence
permitting embedding. It can then be used for any text, including the
desktop UI.

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
that is left to the policy engine (Milestone 4).

## ADR-0013 Compatibility manifest format v1 and a layered catalog

**Decision.**

- **Format:** applications are described by JSON manifests, normatively
  defined by `compatibility/manifests/schema/manifest-v1.schema.json` with a
  stdlib validator kept in sync by a unit test.
- **Statuses:** `unknown`, `untested`, `experimental`, `tested`, `approved`,
  `blocked`.
- **Pinning:** `tested` and `approved` must pin the installer SHA-256.
- **Layers:** the catalog has three layers, `managed` (Control Plane,
  reserved), `local` (administrator) and `system` (package). The highest
  wins, except that `blocked` in any layer wins.
- **Recomputation:** sandbox grants are recomputed from the catalog and
  policy at every launch.
- **Replaced format:** the earlier planned statuses (`gold`, `silver`, ...)
  are replaced.

**Why.**

- A validated status must refer to exactly one installer.
- Blocks must not be undone by a lower-trust layer.
- Nothing an application writes can widen its own sandbox.

## ADR-0014 64-bit-only Windows runtime retained for Milestone 1

**Decision.**

- **Runtime unchanged:** WinCompat ships with Debian's `wine64` only,
  without `wine32` or i386 multiarch.
- **Early refusal:** `boswas-winapp` reads the PE header and refuses 32-bit
  programs (exit 4).
- **Ready for later:** the runtime declares its architectures in
  `runtime.conf`, so adding a 32-bit runtime later is a data change.

**Why.** Enabling i386 multiarch adds roughly 300 MB and a second copy of the
library attack surface. It is a product decision, not an implementation
detail, and has been an open item since v1 alpha. Many installers are 32-bit,
so this is the main compatibility limit to resolve next (roadmap).

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

The Control Plane is a modular monolith (TypeScript/NestJS, PostgreSQL, Redis
only where useful), not microservices.

**Why.** Each milestone depends on the contracts of the previous ones:
WinCompat inventory feeds the agent, the agent feeds the Control Plane, and
signed policy needs both.
