# Repository layout

Only directories that have content exist. Development build trees of the
Rust packages (`packages/*/target/`) are git-ignored and are left out of a
package build. Directories from the long-term plan
(`services/`, `hardware/`, `release/`, `profiles/`, `debian/patches/`, and
the packages `boswas-store`, `boswas-updater`, `boswas-hardware`) are
created by the milestone that implements them.

```
.
├── build.sh, clean.sh, test.sh, Makefile   entry points
├── build/
│   ├── container/Containerfile             Debian 13 builder image (non-Debian hosts)
│   ├── scripts/                            build steps (packages, staging, branding, manifest)
│   └── output/ logs/ manifest/ cache/      generated, git-ignored
├── compatibility/                          WinCompat data shipped by boswas-compat
│   ├── wine/runtime.conf                   Wine runtime facts (loader, server, architectures)
│   ├── manifests/                          schema/ (format v1), catalog/ (system layer), examples/
│   ├── prefixes/defaults.reg               defaults applied to every new prefix
│   ├── installers/types.json               installer kinds, silent switches
│   ├── runners/winapp-exec                 in-sandbox runner (AppArmor attachment point)
│   ├── policies/                           policy.conf, apparmor/boswas-winapp
│   └── desktop/                            "Run with Boswas" handler
├── config/
│   ├── boswas/                             release identity + /etc/boswas templates
│   ├── live-build/                         live-build tree: auto/, package lists,
│   │                                       exclusion pins, boot menu, hooks
│   └── apt/                                Boswas repository template (disabled)
├── desktop/
│   ├── branding/                           Boswas OS logo (source/), traced mark,
│   │                                       Orbitron wordmarks, boot menu, login logo,
│   │                                       tools
│   ├── ui/                                 Boswas UI: tokens.json (the Prism palette,
│   │                                       geometry, type, motion; ADR-0031), the KDE
│   │                                       colour schemes generated from it, tools, tests
│   ├── fonts/orbitron/                     brand typeface (SIL OFL 1.1)
│   ├── presets/                            the ten presets (colour schemes, wallpapers,
│   │                                       Konsole; Plasma Global Themes, unused on the
│   │                                       native desktop), boswas-preset
│   ├── icons/                              Boswas-Prism and Boswas-Prism-Light (the
│   │                                       default icon pack, generated from the tokens),
│   │                                       the earlier Boswas and Boswas-Light themes
│   ├── terminal/                           Boswas prompt and welcome
│   └── defaults/                           defaults for KDE applications and Firefox
├── installer/
│   ├── configuration/preseed.cfg           Debian Installer policy (encrypted install)
│   └── branding/                           installer banner
├── security/                               baseline sources: firewall, hardening,
│                                           audit, usb-policy
├── packages/                               Debian packages (debian/ + code)
│   ├── boswas-os/  boswas-cli/  boswas-branding/  boswas-security/
│   ├── boswas-compat/                      boswas-winapp (Python, stdlib), unit tests
│   ├── boswas-device-agent/                device agent, session agent, boswas-device,
│   │                                       message schemas, systemd units, unit tests
│   ├── boswas-compat-manager/              Compatibility Manager (PySide6), unit + Qt tests
│   ├── boswas-control-center/              Control Center (PySide6), unit + Qt tests
│   ├── boswas-sysops/                      the privileged helper and its polkit actions
│   ├── boswas-android/                     Android runtime manager (Waydroid, opt-in)
│   ├── boswas-desktop/                     the native desktop (Rust workspace, ADR-0026):
│   │   ├── Cargo.toml, Cargo.lock          workspace and the pinned crate versions
│   │   ├── crates/boswas-ui/               design tokens, drawing, text, icons (no unsafe)
│   │   ├── crates/boswas-compositor/       the Wayland compositor (Smithay): src/backend
│   │   │                                   (udev, winit, headless), src/handlers
│   │   │                                   (protocols), src/shell (windows, frames,
│   │   │                                   grabs), ipc.rs (control socket), tests/
│   │   │                                   (end-to-end, headless)
│   │   ├── crates/boswas-shell/            panel, launcher, wallpaper, lock screen,
│   │   │                                   greeter, PAM and greetd clients
│   │   ├── config/                         compositor.toml (defaults and policy),
│   │   │                                   greeter.toml
│   │   ├── session/                        boswas-session, greeter, greetd drop-in and
│   │   │                                   configuration, PAM service, sleep hook
│   │   └── man/  debian/
│   └── boswas-bootsplash/                  boot, disk-unlock and shutdown screens (Rust,
│                                           ADR-0029): src/, initramfs/ hooks, systemd/
│                                           units, tests/, README.md
├── control-plane/                          Control Plane server (boswas-cp; Python stdlib,
│                                           SQLite), dashboard, systemd unit, tests;
│                                           Debian package built into server/, not the image
├── tests/                                  static, unit, packages, build, security,
│                                           compatibility (incl. fixtures/), device
│                                           (image + runtime), boot (QEMU)
├── docs/                                   architecture, development, security, deployment,
│                                           administration, compatibility, device-management
└── LICENSES/                               licensing and attribution model
```

## How content becomes a device file

```
security/firewall/nftables.conf ──(packages/boswas-security/debian/rules)──▶ boswas-security.deb
desktop/…  ─────────────────────(packages/boswas-branding/debian/rules)──▶ boswas-branding.deb
desktop/ui/tokens.json ─┬─(build_kde_colors.py, build_prism.py; committed output)──▶ colour schemes, icons
                        ├─(compiled in: boswas-ui)──▶ packages/boswas-desktop ──(cargo, debian/rules)──▶ boswas-desktop.deb
                        └─(build.rs, with desktop/branding artwork)──▶ packages/boswas-bootsplash ──▶ boswas-bootsplash.deb
crates.io (versions in Cargo.lock) ──(cargo --locked, at package build time)──▶ the two Rust packages
compatibility/… + packages/boswas-compat ──(packages/boswas-compat/debian/rules)──▶ boswas-compat.deb
packages/boswas-device-agent, packages/boswas-compat-manager ──(debian/rules)──▶ their .debs
control-plane/ (+ shared agent/compat modules) ──(control-plane/debian/rules)──▶ server/boswas-control-plane.deb
                                                                               │
config/live-build/ + .debs + rendered branding ──(build/scripts/prepare-live-config.sh)──▶ work/lb
                                                                               │
                                                                     lb build ─▶ ISO
```
