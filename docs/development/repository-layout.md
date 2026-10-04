# Repository layout

Only directories that have content exist. Directories from the long-term plan
(`profiles/`, `services/`, `compatibility/`, `debian/patches/`, and the
packages `boswas-device-agent`, `boswas-id`, `boswas-store`, `boswas-compat`,
`boswas-updater`) are created by the phase that implements them.

```
.
├── build.sh, clean.sh, test.sh, Makefile   entry points
├── build/
│   ├── container/Containerfile             Debian 13 builder image (non-Debian hosts)
│   ├── scripts/                            build steps (packages, staging, branding, manifest)
│   └── output/ logs/ manifest/ cache/      generated, git-ignored
├── config/
│   ├── boswas/                             release identity + /etc/boswas templates
│   ├── live-build/                         live-build tree: auto/, package lists,
│   │                                       exclusion pins, boot menu, hooks
│   └── apt/                                Boswas repository template (disabled)
├── desktop/
│   ├── branding/                           official Boswas Group artwork (source/),
│   │                                       traced vector assets, icons, boot splash
│   ├── wallpapers/Boswas/                  wallpaper and login background (SVG)
│   ├── themes/                             Plasma global theme, Boswas Dark colours
│   └── defaults/                           KDE, SDDM and Plasma session defaults
├── installer/
│   ├── configuration/preseed.cfg           Debian Installer policy (encrypted install)
│   └── branding/                           installer banner
├── security/                               baseline sources: firewall, hardening,
│                                           audit, usb-policy
├── packages/                               native Debian packages (debian/ + code)
│   ├── boswas-os/  boswas-cli/  boswas-branding/  boswas-security/
├── tests/                                  static, packages, build, security,
│                                           compatibility, boot (QEMU)
├── docs/                                   architecture, development, security,
│                                           deployment, administration, compatibility
└── LICENSES/                               licensing and attribution model
```

## How content becomes a device file

```
security/firewall/nftables.conf ──(packages/boswas-security/debian/rules)──▶ boswas-security.deb
desktop/…  ─────────────────────(packages/boswas-branding/debian/rules)──▶ boswas-branding.deb
                                                                               │
config/live-build/ + .debs + rendered branding ──(build/scripts/prepare-live-config.sh)──▶ work/lb
                                                                               │
                                                                     lb build ─▶ ISO
```
