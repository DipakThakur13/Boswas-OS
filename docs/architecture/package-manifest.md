# Package manifest: what Boswas adds and why

The image is built from Debian's `debootstrap` base (priority
required/important) plus the **top-level** packages below. APT resolves their
dependencies and Debian "Recommends". The complete resolved list is written
per build to `build/output/Boswas-OS-v1-alpha-amd64.manifest.txt` (about
1,400 packages in the Plasma-based 1.0~alpha3 image; not yet counted for the
native desktop, which has not been built into an image).

Source of truth: `config/live-build/config/package-lists/*.list.chroot`.
This page must be updated whenever a list changes.

## Base (`boswas-base.list.chroot`)

| Package | Why |
|---------|-----|
| systemd-timesyncd | Correct time for certificates, logs, audit |
| dbus-user-session | Per-user D-Bus, required by modern desktops |
| locales, console-setup, keyboard-configuration, tzdata | Localisation |
| bash-completion, less, man-db | Usable shell and documentation |
| network-manager | Network management (Wi-Fi, VPN, wired). Used by Control Center, the device agent and the panel's network state; set up with `nmtui` or `nmcli` until the native network settings exist |
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

The desktop itself is Boswas software: the compositor, the shell, the lock
screen and the login screen are the package `boswas-desktop`, and the boot
screen is `boswas-bootsplash` (both in `boswas-components.list.chroot`;
ADR-0026 to ADR-0030). This list holds what they need around them and the
applications. KDE Plasma is not installed; KDE's applications are, as
ordinary Wayland clients.

| Package | Why |
|---------|-----|
| greetd | Login daemon (ADR-0030). `boswas-desktop` points it at `/etc/boswas/greetd.toml` with a systemd drop-in; it runs the Boswas greeter and, as a fallback, its own text login (`agreety`) |
| polkit-kde-agent-1 | Authentication dialogue for polkit (`pkexec`, `boswas-sysops`). Started by `boswas-session.target`. A Boswas dialogue is planned |
| xdg-desktop-portal-gtk | Portal back end for file dialogues and settings of Flatpak and other sandboxed applications (`XDG_CURRENT_DESKTOP=Boswas`, `boswas-portals.conf`). It has no screen-capture portal |
| xdg-user-dirs | Standard user folders |
| qt6-wayland | Qt 6 platform plugin for Wayland: Control Center, the Compatibility Manager and the KDE applications draw on the compositor directly (there is no X server) |
| breeze-cursor-theme | Cursor theme the compositor and the applications load (`breeze_cursors`) |
| breeze-icon-theme | Fallback for icons the Boswas Prism pack does not contain |
| kde-style-breeze | Widget style of the KDE and PySide6 applications (`widgetStyle=Breeze`), coloured by the generated Prism colour scheme |
| pipewire-audio | Audio stack |
| pavucontrol-qt | Volume and device control until the shell has quick settings (replaces the Plasma audio applet) |
| konsole, dolphin, kwrite, ark, okular, gwenview, kcalc | Core applications: terminal, files, editor, archives, PDF, images, calculator |
| firefox-esr | Browser on Debian's long-term security-supported channel |
| fonts-hack | Monospace font of the Boswas terminal profiles (`Font=Hack,11`); Plasma used to pull it in |
| fonts-inter | Inter, the Boswas UI typeface (ADR-0031): shell, window titles, lock and login screens |
| fonts-lato | Secondary text in Boswas artwork (the brand typeface, Orbitron, is not in Debian and ships in `boswas-branding`) |
| fonts-liberation2 | Metric-compatible with common Windows fonts (documents, Wine) |
| fonts-noto-core | Broad Unicode coverage |

### Removed with the native desktop

These were in the list up to 1.0~alpha3.

| Package | Why removed |
|---------|-------------|
| kde-plasma-desktop | The Plasma metapackage. Replaced by `boswas-desktop`; pinned out |
| kwin-wayland | KDE's compositor. Replaced by `boswas-compositor` (ADR-0026); pinned out |
| sddm, sddm-theme-breeze, kde-config-sddm | Login manager, its theme and its settings module. Replaced by greetd with the Boswas greeter (ADR-0030); `sddm` is pinned out |
| systemsettings | Plasma's settings application. It configures Plasma and KWin, which are gone; pinned out. Control Center is the settings hub |
| kinfocenter | Built on System Settings and the Plasma workspace. Control Center's Hardware and About pages cover it |
| plasma-nm, plasma-pa, bluedevil | Plasma applets and settings modules for network, audio and Bluetooth; they live in the Plasma shell and System Settings. NetworkManager, PipeWire and BlueZ themselves stay. Native quick settings are planned |
| powerdevil | Plasma's power management service. systemd-logind handles the lid and the power key; idle locking is the compositor's (ADR-0028). Display dimming and power profiles are open |
| kscreen | Display configuration through KWin's interface, which the new compositor does not have. Output configuration is planned |
| xdg-desktop-portal-kde | Portal back end that needs KWin and the Plasma session. Replaced by `xdg-desktop-portal-gtk` |
| kde-spectacle | Screenshot tool; it needs a screen-capture interface the compositor does not offer to applications yet |
| plasma-systemmonitor | Depends on the Plasma libraries and services. `htop` stays in the base list |

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
| boswas-branding | Visual identity: artwork, the Boswas UI tokens, the Prism colour schemes and icon pack, the ten presets, defaults for KDE applications |
| boswas-security | Security baseline configuration |
| boswas-compat | WinCompat: `boswas-winapp`, Wine AppArmor profile, compatibility catalog and policy. Depends on `wine`, `wine64`, `bubblewrap`, `apparmor` (all already in the manifest) |
| boswas-device-agent | Device agent service, per-user session agent, `boswas-device`. Depends on `boswas-compat`, `openssl` (device key, policy signatures) and `systemd` |
| boswas-compat-manager | Compatibility Manager GUI. Depends on PySide6 (`python3-pyside6.qtcore`, `qtgui`, `qtwidgets`, Debian's LGPL Qt bindings, about 30 MB with their Qt libraries) and `boswas-device-agent` |
| boswas-sysops | The only privileged helper (ADR-0024): fixed operations started with pkexec, one polkit action each. Depends on `apt`, `pkexec`, `polkitd` |
| boswas-control-center | Control Center, Security Center and Software Center (PySide6). Depends on `kde-cli-tools` and `libkf6kcmutils-bin`; the Plasma settings modules it can open are no longer installed (see the roadmap) |
| boswas-android | Android runtime manager and APK inspection (ADR-0025). The Android runtime (Waydroid) itself is **not** in the image: it is installed on request after installation |
| boswas-desktop | The native desktop (ADR-0026 to ADR-0028, ADR-0030): `boswas-compositor`, `boswas-shell` (panel, launcher, lock screen, greeter), the session, the greetd drop-in and the lock screen's PAM service. Rust, `amd64`. Depends on `greetd`, `dbus-user-session`, `libegl1`, `libgles2`, `libgl1-mesa-dri`, `libpam-runtime`, `xkb-data` and, through its shared libraries, libseat, libinput, libudev, GBM, pixman, xkbcommon and PAM. Recommends `boswas-branding`, `breeze-cursor-theme`, `fonts-inter`, `polkit-kde-agent-1`, `qt6-wayland`, `xdg-desktop-portal-gtk` (all in the manifest) |
| boswas-bootsplash | The boot, disk-unlock and shutdown screens (ADR-0029). Rust, `amd64`. Depends on `initramfs-tools`; conflicts with `plymouth` |

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
| plasma-workspace, plasma-desktop, kde-plasma-desktop, kwin-wayland, kwin-x11, sddm, systemsettings | Replaced by the native Boswas desktop (ADR-0026). The Plasma shell and session, KWin, SDDM and System Settings must not come back through a dependency or a Recommends |
| plymouth, plymouth-themes, plymouth-label | Replaced by `boswas-bootsplash` (ADR-0029), which conflicts with Plymouth: two boot screens in one initramfs would both answer the disk passphrase request |

Not selected (would arrive with a profile later): LibreOffice, printing
(CUPS), the KDE PIM suite, games, `task-kde-desktop`.

No X server is installed on purpose (no `xwayland`, no `xserver-xorg`): the
compositor does not start one (ADR-0026). They are not pinned; nothing in
the manifest asks for them.

## Build-time only: Rust crates

`boswas-desktop` and `boswas-bootsplash` are compiled from the crates named
in `packages/boswas-desktop/Cargo.lock` and
`packages/boswas-bootsplash/Cargo.lock`, downloaded from crates.io during
the package build (ADR-0032). They are linked into the programs and are
not Debian packages on the device, so they do not appear in the image's
package manifest. The direct dependencies:

| Crate | Used by | Why |
|-------|---------|-----|
| smithay 0.7 | compositor | Wayland server protocols, DRM/KMS, GBM, EGL/GLES, libinput and libseat bindings, the software (pixman) renderer |
| smithay-client-toolkit 0.19, wayland-client, wayland-protocols | shell (and the compositor's tests) | Wayland client side: layer shell, session lock |
| tiny-skia 0.11 | boswas-ui, compositor, shell | Software 2D drawing |
| cosmic-text 0.12 | boswas-ui | Text shaping and font fallback |
| resvg 0.45 | boswas-ui, bootsplash (build time) | SVG icons; the boot artwork is rasterised at build time |
| image 0.25, png 0.17 | boswas-ui, compositor | Wallpaper decoding (PNG, JPEG); screenshots |
| xcursor 0.3 | compositor | Cursor themes |
| toml 0.8, serde, serde_json | compositor, shell | Configuration; the control socket |
| rustix 1, libc 0.2, bitflags 2 | compositor, shell, bootsplash | System calls |
| zeroize 1 | shell, bootsplash | Wiping passwords and the disk passphrase from memory |
| chrono 0.4 | shell | The panel clock |
| tracing, tracing-subscriber | compositor, shell | Logging |
| drm 0.14 | bootsplash | DRM/KMS ioctls (no libdrm) |
| landlock 0.4 | bootsplash | The daemon's sandbox |
| fontdue 0.9.3 | bootsplash (build time) | Rasterising the glyph atlas |
| tempfile 3 | compositor (tests only) | Scratch directories |
