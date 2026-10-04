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

**Decision.** Everything Boswas adds to a device lives in four native Debian
packages built from `packages/`:

- `boswas-os`: release identity and `/etc/boswas`; depends on the others.
- `boswas-cli`: `boswas`, `boswas-info`, `boswas-status`.
- `boswas-branding`: KDE look-and-feel, wallpaper, login, About and icons.
- `boswas-security`: the security baseline.

The packages reference shared sources in the monorepo (`security/`,
`desktop/`, `config/boswas/`). `debian/rules` installs every file with an
explicit mode.

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
