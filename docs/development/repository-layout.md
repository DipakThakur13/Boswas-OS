# Repository layout

Only directories that have content exist. Directories from the long-term plan
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
│   │                                       Orbitron wordmarks, boot menu, Plymouth,
│   │                                       session splash, login logo, tools
│   ├── fonts/orbitron/                     brand typeface (SIL OFL 1.1)
│   ├── presets/                            the ten presets (Global Themes, colour
│   │                                       schemes, wallpapers, Konsole), boswas-preset
│   ├── icons/                              Boswas and Boswas Light icon themes
│   ├── launcher/                           Boswas Launcher applet
│   ├── terminal/                           Boswas prompt and welcome
│   └── defaults/                           KDE, SDDM, Firefox and session defaults
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
│   └── boswas-compat-manager/              Compatibility Manager (PySide6), unit + Qt tests
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
compatibility/… + packages/boswas-compat ──(packages/boswas-compat/debian/rules)──▶ boswas-compat.deb
packages/boswas-device-agent, packages/boswas-compat-manager ──(debian/rules)──▶ their .debs
control-plane/ (+ shared agent/compat modules) ──(control-plane/debian/rules)──▶ server/boswas-control-plane.deb
                                                                               │
config/live-build/ + .debs + rendered branding ──(build/scripts/prepare-live-config.sh)──▶ work/lb
                                                                               │
                                                                     lb build ─▶ ISO
```
