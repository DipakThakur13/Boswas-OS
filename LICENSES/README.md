# Licensing and attribution model

Boswas OS combines two clearly separated kinds of components.

## 1. Debian upstream components

Every package in the image that is not named `boswas-*` is an
**unmodified Debian package**, downloaded from the Debian archive at build
time. Each keeps its own licence. The licence and copyright of each package
are installed on every device in `/usr/share/doc/<package>/copyright`, and
the full list of packages and versions of an image is in its build manifest.

- **Source availability.** Sources for every Debian package version are
  available from Debian (https://sources.debian.org/ and
  https://snapshot.debian.org/). If images are ever distributed outside
  Boswas Group, enable source images in live-build (`--source true`) or
  otherwise provide the corresponding sources, as the GPL and other copyleft
  licences require.
- **Non-free firmware** comes from Debian's `non-free-firmware` archive
  area. It is redistributable firmware, the same Debian ships on its
  official installation media. Its terms are in each firmware package's
  copyright file.
- **Debian identity and trademarks.** Boswas OS keeps Debian's
  `/etc/os-release` and states that it is "based on Debian". It does not use
  the Debian logo as its own identity. The Debian Installer banner is
  replaced by the Boswas banner, and installer and system text continue to
  name Debian where it is upstream software.

## 2. Boswas-specific components

Everything in this repository (packaging, configuration, scripts, artwork,
tests and documentation), and the `boswas-*` packages built from it:

- **Licence:** `LicenseRef-Boswas-Internal`, see
  [LicenseRef-Boswas-Internal.txt](LicenseRef-Boswas-Internal.txt).
- **Boswas OS logo** (`desktop/branding/source/boswas-os-logo.png`, and the
  mark traced from it) and the **Boswas Group logo** (also in
  `desktop/branding/source/`, no longer used in artwork): trademarks and
  property of Boswas Group.

## Third-party material used at build time

| Material | Licence | Use |
|----------|---------|-----|
| Orbitron typeface (`desktop/fonts/orbitron/`, © 2018 The Orbitron Project Authors) | SIL Open Font License 1.1, Reserved Font Name "Orbitron" | Brand typeface: outlined into the wordmarks; installed unmodified by `boswas-branding` (static weights) with the licence in its copyright file |
| Lato typeface (Debian `fonts-lato`) | SIL Open Font License 1.1 | Rendered into raster artwork (secondary text); installed as a Debian package |
| live-build, debootstrap, Debian Installer, GRUB, shim | Their Debian licences | Unmodified build tooling and boot components |
| potrace | GPL-2.0-or-later | One-time tracing tool for the Boswas OS mark (not shipped) |
| fontTools (Debian `python3-fonttools`) | MIT | One-time tool that outlines the Orbitron wordmarks (not shipped) |

> The choice of `LicenseRef-Boswas-Internal` for Boswas-authored files is a
> placeholder pending confirmation by Boswas Group Legal. Choosing an open
> source licence for some components (e.g. the CLI) would not affect the
> Debian components.
