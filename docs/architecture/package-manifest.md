# Package manifest: what Boswas adds and why

The image is built from Debian's `debootstrap` base (priority
required/important) plus the **top-level** packages below. APT resolves their
dependencies and Debian "Recommends". The complete resolved list (about 1,400
packages) is written per build to
`build/output/Boswas-OS-v1-alpha-amd64.manifest.txt`.

Source of truth: `config/live-build/config/package-lists/*.list.chroot`.
This page must be updated whenever a list changes.

## Base (`boswas-base.list.chroot`)

| Package | Why |
|---------|-----|
| systemd-timesyncd | Correct time for certificates, logs, audit |
| dbus-user-session | Per-user D-Bus, required by modern desktops |
| locales, console-setup, keyboard-configuration, tzdata | Localisation |
| bash-completion, less, man-db | Usable shell and documentation |
| network-manager | Network management (Wi-Fi, VPN, wired) used by Plasma |
| ca-certificates, curl, wget | TLS trust store and HTTP clients |
| openssh-client | Outbound SSH for engineers. No server |
| sudo | Explicit, auditable administration |
| git, vim, nano, rsync, unzip, htop | Baseline admin and engineering tools |
| pciutils, usbutils, dmidecode, lshw | Hardware inventory and support (future agent) |
| gnupg | Signature verification |
| python3 | Runtime of the `boswas` CLI |
| cryptsetup, cryptsetup-initramfs, lvm2 | LUKS2 + LVM full-disk encryption, unlocked at boot |
| efibootmgr, mokutil | UEFI boot entries; Secure Boot state and MOK management |
| tpm2-tools | TPM 2.0 inspection (future sealing of keys) |
| fwupd | Firmware updates from LVFS |

## Desktop (`boswas-desktop.list.chroot`)

| Package | Why |
|---------|-----|
| kde-plasma-desktop | Debian's minimal Plasma 6.3 metapackage (not the full KDE suite) |
| kwin-wayland | Wayland session (default in Plasma 6). X11 stays as a fallback |
| sddm, sddm-theme-breeze, kde-config-sddm | Login manager with the Breeze theme (Boswas background) |
| plasma-nm, plasma-pa, powerdevil, bluedevil, kscreen | Network, audio, power, Bluetooth and display settings |
| systemsettings, kinfocenter | Settings and About (Boswas branding) |
| xdg-desktop-portal-kde, xdg-user-dirs | Portals for Flatpak/sandboxed apps; standard user folders |
| pipewire-audio | Audio stack |
| konsole, dolphin, kwrite, ark, okular, gwenview, kde-spectacle, kcalc, plasma-systemmonitor | Core apps: terminal, files, editor, archives, PDF, images, screenshots, calculator, system monitor |
| firefox-esr | Browser on Debian's long-term security-supported channel |
| fonts-lato | Secondary text in Boswas artwork (the brand typeface, Orbitron, is not in Debian and ships in `boswas-branding`) |
| fonts-liberation2 | Metric-compatible with common Windows fonts (documents, Wine) |
| fonts-noto-core | Broad Unicode coverage |

## Security (`boswas-security.list.chroot`)

| Package | Why |
|---------|-----|
| apparmor, apparmor-utils | Mandatory access control and its tooling |
| auditd | Security audit trail |
| nftables | Host firewall |
| polkitd, pkexec | Least-privilege authorisation of desktop actions |
| libpam-pwquality | Password quality enforcement |
| unattended-upgrades | Automatic Debian security updates |

## Compatibility (`boswas-compat.list.chroot`)

| Package | Why |
|---------|-----|
| wine, wine64 | Windows application compatibility (Wine 10, 64-bit programs), run only through `boswas-winapp` sandboxes |
| wine32:i386 | 32-bit Windows programs (ADR-0022). Debian 13's wine64 has no 32-bit Windows modules; `:i386` makes live-build enable i386 as the only foreign dpkg architecture (about 1 GB installed with its i386 libraries). The system stays amd64 |
| libfuse2t64 | FUSE 2 library for AppImages built with the older AppImage runtime (fuse3 is already installed) |
| flatpak, bubblewrap | Application sandboxing. bubblewrap isolates every Windows application (boswas-compat); Flatpak has no remote configured until an administrator enables an approved one (`boswas-sysops flatpak-remote-enable`) |

## Hardware (`boswas-hardware.list.chroot`)

The selected redistributable firmware is listed in
`docs/deployment/hardware-compatibility.md`. firmware-cirrus (laptop
speaker amplifiers), firmware-bnx2/bnx2x (Broadcom NetXtreme II),
firmware-libertas and firmware-zd1211 (older Wi-Fi) were added with the
installer firmware work (ADR-0023). Independently, the **installer medium**
carries every Debian firmware package in `/firmware` (`lb config
--firmware-binary true`), so the installer can load firmware before any
network exists; `config/boswas/firmware-required.list` is the build gate.

## Boswas (`boswas-components.list.chroot`)

| Package | Why |
|---------|-----|
| boswas-os | Release identity, `/etc/boswas`, login banner; depends on the others |
| boswas-cli | `boswas`, `boswas-info`, `boswas-status` |
| boswas-branding | Visual identity and KDE defaults |
| boswas-security | Security baseline configuration |
| boswas-compat | WinCompat: `boswas-winapp`, Wine AppArmor profile, compatibility catalog and policy. Depends on `wine`, `wine64`, `bubblewrap`, `apparmor` (all already in the manifest) |
| boswas-device-agent | Device agent service, per-user session agent, `boswas-device`. Depends on `boswas-compat`, `openssl` (device key, policy signatures) and `systemd` |
| boswas-compat-manager | Compatibility Manager GUI. Depends on PySide6 (`python3-pyside6.qtcore`, `qtgui`, `qtwidgets`, Debian's LGPL Qt bindings, about 30 MB with their Qt libraries) and `boswas-device-agent` |
| boswas-sysops | The only privileged helper (ADR-0024): fixed operations started with pkexec, one polkit action each. Depends on `apt`, `pkexec`, `polkitd` |
| boswas-android | Android runtime manager and APK inspection (ADR-0025). The Android runtime (Waydroid) itself is **not** in the image: it is installed on request after installation |

The Control Plane (`boswas-control-plane`, a server package), the Android
runtime (`waydroid`, `lxc`, `python3-gbinder`) and any Google Play / Google
Mobile Services component are deliberately not part of the image; the
verify hook fails the build if they appear. The only foreign dpkg
architecture is i386, for wine32 (ADR-0022).

## Deliberately excluded

Pinned out in `config/live-build/config/archives/boswas-exclude.pref.chroot`;
the build fails if anything requires them.

| Package | Why excluded |
|---------|--------------|
| kdeconnect | Opens LAN ports and pairs personal phones |
| plasma-discover (+ backends), packagekit | Unmanaged software installation path |
| libpam-fprintd, fprintd | Biometric PAM is a policy decision |
| openssh-server | No inbound remote access by default |
| cups-browsed | Network-listening print discovery; printing comes later |
| plasma-welcome | Upstream first-login wizard ("Welcome to Debian", KDE community and donation links); Boswas onboarding comes later |

Not selected (would arrive with a profile later): LibreOffice, printing
(CUPS), the KDE PIM suite, games, `task-kde-desktop`.
