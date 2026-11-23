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
| network-manager | Network management (Wi-Fi, VPN, wired). Used by the shell's quick settings and network state (`boswas-services`, ADR-0035), Control Center and the device agent. `nmtui` and `nmcli` stay available in a terminal |
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
screen and the login screen are the package `boswas-desktop`, the boot
screen is `boswas-bootsplash` and the system applications are
`boswas-apps` (all three in `boswas-components.list.chroot`; ADR-0026 to
ADR-0030, ADR-0033 to ADR-0040). This list holds what they need around
them and the browser. KDE Plasma is not installed, and neither are KDE's
applications any more (see [Replaced by the Boswas
applications](#replaced-by-the-boswas-applications)); they still run as
ordinary Wayland clients when someone installs them.

| Package | Why |
|---------|-----|
| greetd | Login daemon (ADR-0030). `boswas-desktop` points it at `/etc/boswas/greetd.toml` with a systemd drop-in; it runs the Boswas greeter and, as a fallback, its own text login (`agreety`). If greetd fails for good, `boswas-login-fallback.service` starts getty on the first console (ADR-0044) |
| xwayland | The X server for X11 applications (ADR-0034). `boswas-compositor` starts it and manages its windows like any other (`[xwayland] enabled`) |
| xdg-desktop-portal-gtk | Portal back end for file dialogues and settings of Flatpak and other sandboxed applications (`XDG_CURRENT_DESKTOP=Boswas`, `boswas-portals.conf`). It has no screen-capture portal |
| xdg-desktop-portal-wlr | Portal back end for screen sharing and screenshots (browsers, meetings, sandboxed applications). It asks the compositor for pictures through wlr-screencopy; the compositor asks the user first (ADR-0037). Configured without a chooser program (`/etc/xdg/xdg-desktop-portal-wlr/Boswas`): it shares the first monitor. Not tested in this build environment |
| pipewire-bin, wireplumber | `pw-dump` and `wpctl`: how the shell and Settings read and set sound devices and volumes (`boswas-services`) |
| upower | Battery state for the taskbar, quick settings and Settings |
| power-profiles-daemon | The power modes (power saver, balanced, performance) in quick settings and Settings |
| accountsservice | User accounts in Settings (name, picture, password, adding and removing users), authorised through polkit |
| xdg-user-dirs | Standard user folders (Documents, Pictures, ...), which Files, Photos, the Media Player, the Camera and the shell's screen capture use |
| xdg-utils | `xdg-open` and its companions: how other programs, and the Qt toolkit where there is no portal, open files and web addresses with the default application. The shell opens its own files (a screenshot, Start's folders) itself by the same rules and uses `xdg-open` only when it finds no application (ADR-0054). No package of the image installed it before, so the shell's `xdg-open` did nothing; `boswas-desktop` now depends on it |
| qt6-wayland | Qt 6 platform plugin for Wayland: Control Center, the Compatibility Manager and the KDE applications draw on the compositor directly. The session asks Qt for Wayland first and X11 second (`QT_QPA_PLATFORM=wayland;xcb`) |
| breeze-icon-theme | Fallback for icons the Boswas Prism pack does not draw: the generic icons of other programs and the icons of programs that bring none of their own. A program's own icon in `hicolor` comes before Breeze's drawing of it (ADR-0052). Data only; it is the one KDE package left on the image (ADR-0043) |
| pipewire-audio | Audio stack |
| pavucontrol-qt | Per-application volumes; the shell's quick settings and Settings cover devices and the main volume |
| firefox-esr | Browser on Debian's long-term security-supported channel |
| fonts-hack | Monospace font of Boswas Terminal (also a Recommends of `boswas-apps`); Plasma used to pull it in |
| fonts-inter | Inter, the Boswas UI typeface (ADR-0031, ADR-0033): shell, window titles, lock and login screens and the Boswas Qt applications. Also a build dependency of `boswas-bootsplash`, which embeds the outlines of its glyphs (ADR-0038, ADR-0050) |
| fonts-lato | Secondary text in Boswas artwork (the brand typeface, Orbitron, is not in Debian and ships in `boswas-branding`) |
| fonts-liberation2 | Metric-compatible with common Windows fonts (documents, Wine) |
| fonts-noto-core | Broad Unicode coverage |

### Replaced by the Boswas applications

These were the "core applications" of the list until the Boswas
applications landed (ADR-0040). They are not pinned out, and they run on
the desktop when someone installs them.

| Package | Replaced by (`boswas-apps`) |
|---------|-----------------------------|
| konsole | Boswas Terminal (`boswas-terminal`), which also provides `x-terminal-emulator` |
| dolphin | Boswas Files (`boswas-files`) |
| kwrite | Boswas Text Editor (`boswas-text-editor`) |
| ark | Boswas Archive Manager (`boswas-archive-manager`) |
| okular | Boswas Document Viewer (`boswas-document-viewer`), for PDF only |
| gwenview | Boswas Photos (`boswas-photos`) |
| kcalc | Boswas Calculator (`boswas-calculator`) |

### Removed with the native desktop

These were in the list up to 1.0~alpha3. `polkit-kde-agent-1`, which the
first version of the native desktop added, is gone again: the shell is
the polkit agent (ADR-0035).

| Package | Why removed |
|---------|-------------|
| kde-plasma-desktop | The Plasma metapackage. Replaced by `boswas-desktop`; pinned out |
| kwin-wayland | KDE's compositor. Replaced by `boswas-compositor` (ADR-0026); pinned out |
| sddm, sddm-theme-breeze, kde-config-sddm | Login manager, its theme and its settings module. Replaced by greetd with the Boswas greeter (ADR-0030); `sddm` is pinned out |
| systemsettings | Plasma's settings application. It configures Plasma and KWin, which are gone; pinned out. Control Center is the settings hub |
| kinfocenter | Built on System Settings and the Plasma workspace. Control Center's Hardware and About pages cover it |
| plasma-nm, plasma-pa, bluedevil | Plasma applets and settings modules for network, audio and Bluetooth; they live in the Plasma shell and System Settings. NetworkManager, PipeWire and BlueZ themselves stay. The shell's quick settings take their place (ADR-0035) |
| powerdevil | Plasma's power management service. systemd-logind handles the lid and the power key; idle locking is the compositor's (ADR-0028), which also turns the screens off after a time without input (ADR-0036); the power mode is set in quick settings through `power-profiles-daemon`. Display dimming is open |
| kscreen | Display configuration through KWin's interface, which the new compositor does not have. Monitors are set per connector in the compositor's configuration (ADR-0036) |
| xdg-desktop-portal-kde | Portal back end that needs KWin and the Plasma session. Replaced by `xdg-desktop-portal-gtk` |
| kde-spectacle | Screenshot tool. The shell's screen capture (the Print key) takes its place; other capture tools go through the compositor's consent question (ADR-0037) |
| plasma-systemmonitor | Depends on the Plasma libraries and services. Boswas Task Manager (`boswas-apps`) takes its place; `htop` stays in the base list |

## Security (`boswas-security.list.chroot`)

| Package | Why |
|---------|-----|
| apparmor, apparmor-utils | Mandatory access control and its tooling |
| auditd | Security audit trail |
| nftables | Host firewall |
| polkitd, pkexec | Least-privilege authorisation of desktop actions. `polkitd` is also what logind asks before it activates the login screen's session, so `boswas-desktop` depends on it (ADR-0044) |
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
`docs/deployment/hardware-compatibility.md`. `bluez`, the Bluetooth
service the shell's quick settings talk to, and `bluez-firmware` are in
this list too. firmware-cirrus (laptop
speaker amplifiers), firmware-bnx2/bnx2x (Broadcom NetXtreme II),
firmware-libertas and firmware-zd1211 (older Wi-Fi) were added with the
installer firmware work (ADR-0023). Independently, the **installer medium**
carries every Debian firmware package in `/firmware` (`lb config
--firmware-binary true`), so the installer can load firmware before any
network exists; `config/boswas/firmware-required.list` is the build gate.

## Boswas (`boswas-components.list.chroot`)

| Package | Why |
|---------|-----|
| boswas-os | Release identity, `/etc/boswas`, login banner; depends on the others, `boswas-apps` included |
| boswas-cli | `boswas`, `boswas-info`, `boswas-status` |
| boswas-branding | Visual identity: artwork, the Boswas UI tokens, the Prism colour schemes and icon pack, the ten presets, defaults for KDE applications, and the Qt toolkit `boswas_fluent` in `/usr/lib/boswas/python` (ADR-0039). Because of the toolkit it depends on PySide6: `python3-pyside6.qtcore`, `qtgui`, `qtwidgets`, `qtsvg`, `qtdbus` and `qtnetwork`. Also depends on `python3`; recommends `breeze-icon-theme`, `fonts-inter` and `fonts-lato`. `libkf6config-bin` (KDE's `kwriteconfig6`) is gone from its relations, as `konsole` was before: `boswas-preset` writes the user's files itself and starts no program (ADR-0042). Its build no longer needs `netpbm` |
| boswas-security | Security baseline configuration |
| boswas-compat | WinCompat: `boswas-winapp`, Wine AppArmor profile, compatibility catalog and policy. Depends on `wine`, `wine64`, `bubblewrap`, `apparmor` (all already in the manifest) |
| boswas-device-agent | Device agent service, per-user session agent, `boswas-device`. Depends on `boswas-compat`, `openssl` (device key, policy signatures) and `systemd` |
| boswas-compat-manager | Compatibility Manager GUI. Depends on PySide6 (`python3-pyside6.qtcore`, `qtgui`, `qtwidgets`, Debian's LGPL Qt bindings, about 30 MB with their Qt libraries), on `boswas-branding` for the toolkit and on `boswas-device-agent`. It ships an "Open with" entry for Windows installers and, with it, `/usr/share/applications/boswas-mimeapps.list`, which keeps "Run with Boswas" (`boswas-compat`) their default application (ADR-0042); nothing is installed for Dolphin any more |
| boswas-sysops | The only privileged helper (ADR-0024): fixed operations started with pkexec, one polkit action each. Depends on `apt`, `pkexec`, `polkitd` |
| boswas-control-center | Control Center with native settings (ADR-0041), laid out like Windows 11's Settings (ADR-0049), Security Center and Software Center (PySide6, on the toolkit of `boswas-branding`). Depends on `python3-pyside6.qtcore`, `qtgui`, `qtwidgets`, `qtnetwork` and `qtdbus`, `qt6-svg-plugins`, `boswas-branding`, `boswas-cli`, `boswas-device-agent`, `libglib2.0-bin` (`gio launch`), `systemd` (`timedatectl`, `localectl`, `hostnamectl`, `systemctl`, `busctl`), `passwd` (the own password), `tzdata` and `xkb-data` (the time zones and keyboard layouts it offers). Recommends `boswas-desktop` (the service bridge), `boswas-apps`, `fontconfig` (the Fonts page: `fc-list`, `fc-cache`), `accountsservice` (the Users page), `boswas-compat-manager`, `boswas-sysops`, `boswas-android`, `network-manager`, `iproute2`, `locales`, `flatpak`, `pci.ids` and `usb.ids`. `kde-cli-tools`, `libkf6kcmutils-bin`, `dolphin` and `kinfocenter` are gone from its relations |
| boswas-apps | The system applications (ADR-0040): Files, Terminal, Text Editor, Calculator, Task Manager, Photos, Media Player, Camera, Document Viewer, Archive Manager, Clock, Calendar and the reminders service. Python, `all`. **Depends** on `python3` (3.11 or later), `python3-pyside6.qtcore`, `qtgui`, `qtwidgets`, `qtsvg`, `qtdbus`, `qtnetwork`, `qtmultimedia` (Media Player, Camera), `qtpdf` (Document Viewer) and `qtprintsupport` (printing), `qt6-image-formats-plugins` (picture formats), `shared-mime-info` (file types), `tzdata` (Clock, Calendar) and `boswas-branding` (the toolkit). **Recommends** `7zip` (7z archives), `boswas-desktop`, `fonts-hack`, `fonts-noto-color-emoji`, `udisks2` (mounting and ejecting drives in Files) and `xdg-user-dirs`; of these `7zip`, `fonts-noto-color-emoji` and `udisks2` are not in a package list and reach the image as Recommends (ADR-0003). **Suggests**, which are not installed: `7zip-rar` (RAR), `kimageformat6-plugins` (AVIF, HEIF, JPEG XL), `python3-zstandard`, `sound-theme-freedesktop` (the Camera's shutter sound). **Provides** `x-terminal-emulator`: the maintainer scripts register `/usr/bin/boswas-terminal` as an alternative for it with priority 40 and remove it again. `debian/rules` installs the reminders service's desktop entry as `/etc/xdg/autostart/com.boswas.Reminders.desktop` (for the Boswas session only), not among the applications, the system's default applications as `/usr/share/applications/mimeapps.list` (pictures in Photos, folders in Files, the web in Firefox ESR and the rest; ADR-0054), and the systemd user units `boswas-weather-update.service` and `.timer` in `/usr/lib/systemd/user/`, the half-hourly update of the lock screen's weather; the postinst enables the timer for every user (ADR-0051) |
| boswas-android | Android runtime manager and APK inspection (ADR-0025). The Android runtime (Waydroid) itself is **not** in the image: it is installed on request after installation |
| boswas-desktop | The native desktop (ADR-0026 to ADR-0028, ADR-0030, ADR-0033 to ADR-0037): `boswas-compositor`, `boswas-shell` (taskbar, Start, quick settings, notifications, the polkit agent, lock screen, greeter), `boswas-services-bridge` (the same service backends for Settings), the session, the greetd drop-in and the lock screen's PAM service. Rust, `amd64`. Depends on `greetd`, `dbus-user-session`, `libegl1`, `libgles2`, `libgl1-mesa-dri`, `libpam-runtime`, `xkb-data`, `xdg-utils` (`xdg-open`, the shell's last resort for opening a file, ADR-0054), on what the login screen cannot start without (ADR-0044: `polkitd`, which logind asks before it activates the session; `fontconfig-config` and `fonts-inter` for text; `libpam-systemd` for the login session) and, through its shared libraries, on libseat, libinput, libudev, GBM, pixman, xkbcommon and PAM. Recommends `boswas-branding` (which holds the pointer theme, `Boswas-Cursors`), `qt6-wayland`, `xwayland`, `xdg-desktop-portal-gtk`, `xdg-desktop-portal-wlr`, `network-manager`, `bluez`, `pipewire-bin`, `wireplumber`, `upower`, `power-profiles-daemon` (all in the manifest): each is optional at run time, and what it serves shows as not available without it |
| boswas-bootsplash | The boot, disk-unlock and shutdown screens (ADR-0029, ADR-0038). Rust, `amd64`. Depends on `initramfs-tools`; conflicts with `plymouth`. Build-depends on `fonts-inter`: the glyphs of the typeface are built into the program, and the font itself is not installed by this package |

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

`xwayland` is the only X server the manifest names (ADR-0034, which
supersedes the "no X server" of ADR-0026). `xserver-xorg` is not named
and not pinned.

**Not checked against Debian 13.** The build environment has no Debian
trixie package index. The names of the PySide6 module packages
(`python3-pyside6.qtmultimedia`, `python3-pyside6.qtpdf`,
`python3-pyside6.qtprintsupport`, `python3-pyside6.qtdbus`,
`python3-pyside6.qtnetwork`, `python3-pyside6.qtsvg`) and of the
packages this round added (`xwayland`, `xdg-desktop-portal-wlr`,
`pipewire-bin`, `wireplumber`, `upower`, `power-profiles-daemon`,
`accountsservice`, `7zip`, `udisks2`, `qt6-image-formats-plugins`) could
not be verified there, and the manifest has not been resolved by APT.
The first image build will show which of them need another name.

## Build-time only: Rust crates

`boswas-desktop` and `boswas-bootsplash` are compiled from the crates named
in `packages/boswas-desktop/Cargo.lock` and
`packages/boswas-bootsplash/Cargo.lock`, downloaded from crates.io during
the package build (ADR-0032). They are linked into the programs and are
not Debian packages on the device, so they do not appear in the image's
package manifest. The direct dependencies:

| Crate | Used by | Why |
|-------|---------|-----|
| smithay 0.7 | compositor | Wayland server protocols, DRM/KMS, GBM, EGL/GLES, libinput and libseat bindings, the software (pixman) renderer, the X11 window manager for Xwayland |
| smithay-client-toolkit 0.19, wayland-client, wayland-protocols | shell (and the compositor's tests) | Wayland client side: layer shell, session lock |
| tiny-skia 0.11 | boswas-ui, compositor, shell | Software 2D drawing |
| cosmic-text 0.12 | boswas-ui | Text shaping and font fallback |
| resvg 0.45 | boswas-ui, bootsplash (build time) | SVG icons; the boot screen reads the mark's paths and gradients with its usvg at build time, and renders a reference picture of the mark for its tests (ADR-0050) |
| image 0.25, png 0.17 | boswas-ui, compositor | Wallpaper decoding (PNG, JPEG); PNG application icons, in the shell and since ADR-0052 in the compositor (`boswas-ui`'s `images` feature); screenshots and window thumbnails |
| xcursor 0.3 | compositor | Cursor themes |
| toml 0.8, serde, serde_json | compositor, shell, services, bridge | Configuration; the control socket; the bridge's JSON lines |
| rustix 1, libc 0.2, bitflags 2 | compositor, shell, bootsplash | System calls |
| zeroize 1 | shell, services, bridge, bootsplash | Wiping passwords, Wi-Fi passphrases and the disk passphrase from memory |
| chrono 0.4 | shell, compositor | The clock and the calendar; the schedule of night light |
| zbus 5, async-io 2, async-executor 1, async-channel 2, futures-lite 2 | services | D-Bus (NetworkManager, BlueZ, UPower, logind, polkit, notifications, tray, media players) on a small executor per service; no tokio |
| tracing, tracing-subscriber | compositor, shell | Logging |
| drm 0.14 | bootsplash | DRM/KMS ioctls (no libdrm) |
| landlock 0.4 | bootsplash | The daemon's sandbox |
| ttf-parser 0.21 | bootsplash (build time) | Reading the glyph outlines, advances and kerning of Inter (`fonts-inter` on the build machine); it replaced fontdue (ADR-0050) |
| tempfile 3 | compositor, services, bridge (tests only) | Scratch directories |
