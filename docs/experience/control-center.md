# Boswas Control Center

The settings hub of Boswas OS (`boswas-control-center`, package
`boswas-control-center`). Every page shows the device as the system reports
it and offers the actions of its area. This document describes what each
page reads, what it can change, and the rules that keep it safe.

Control Center was written for the KDE Plasma image, where it also opened
KDE's settings modules (`kcmshell6 <module>`) for what Plasma configured.
On the native Boswas desktop those modules are not installed. Control
Center checks for each module before offering it: the tile of a missing
module is disabled and says "Not installed on this system" (an optional one
is hidden), so no button fails when clicked.
[On the native desktop](#on-the-native-desktop) lists what that means page
by page.

## Pages

| Page | Reads | Actions |
|------|-------|---------|
| Personalization | `boswas-preset --json list` | apply a preset (on the native desktop: light or dark, wallpaper, application colours, Konsole profile); KDE appearance modules where installed |
| Network | `nmcli -t` (devices, state, addresses, Wi-Fi radio) | Wi-Fi on/off, connect/disconnect a device (NetworkManager decides through its own polkit rules); new networks and passwords in KDE's network module where installed, otherwise `nmtui` in a terminal |
| Storage | statvfs of `/` and `/home`; `lsblk -J -b` (no serial numbers); size of `~/.cache/thumbnails` | remove downloaded package files (sysops `apt-clean`), shrink the system journal (sysops `journal-vacuum`), clear the thumbnail cache; partitioning only in KDE Partition Manager |
| Applications (Software Center) | desktop entries, Windows apps (session agent), `apt-cache search/show/policy`, `flatpak remotes/list/search`, AppImages in `~/Applications` | install/remove Debian packages (sysops `apt-install`/`apt-remove`); enable Flathub (sysops `flatpak-remote-enable`); install/uninstall/update Flatpak apps for the user; add/remove AppImages; install from file (by content: `.deb` → sysops `apt-install-local` with an "origin not verified" warning, Windows program → `boswas-compat-manager --install FILE`, APK → Android page, AppImage → AppImages) |
| Windows Compatibility | session agent `system.status` (runtime health, supported Windows architectures), `apps.list` | open the Compatibility Manager |
| Android | `boswas-android --json status`, `app list`, `apk inspect FILE` | install, initialize, start, stop, restart, repair, update, remove the runtime; install an APK after inspection; open and uninstall apps; install F-Droid (labelled as not Google Play) |
| Security | `boswas --json status` (its screen-lock check reads the compositor's lock policy), device agent status and runtime check | KDE firewall and screen-lock modules where installed |
| Updates | APT periodic settings and stamps, unattended-upgrades log, `apt-get -s ... upgrade` (simulation as the user) | check for updates (sysops `apt-refresh`), install updates (sysops `apt-upgrade`, never removes packages) |
| Hardware & Drivers | `/sys/bus/pci/devices`, `/sys/bus/usb/devices` (IDs, class, bound driver; never serial numbers), `pci.ids`/`usb.ids` | check for missing firmware (sysops `firmware-report`, read-only, no password), install the Debian package that provides a missing file (sysops `apt-install`) |
| About Boswas OS | release, image info, hardware, the desktop (Boswas Desktop and its version from `/usr/share/boswas/compositor/version`; the Plasma version on a Plasma image), plus a system overview: load (`/proc/stat`, `/proc/meminfo`), network, Android runtime, and pending updates / firmware only if another page already found them in this window | copy the information |

Bluetooth, Display, Sound, Power, Users, Devices, Privacy and System explain
the state where Boswas has data and open the KDE modules where they are
installed.

## On the native desktop

The pages built on Boswas tools work as described above: Personalization
(presets), Network (state, Wi-Fi on and off, connect and disconnect),
Storage, Applications, Windows Compatibility, Android, Security, Updates,
Hardware & Drivers and About Boswas OS.

What is not available, because the Plasma settings modules and Plasma's
services behind them are not in the image:

| Page | Unavailable | Use instead |
|------|-------------|-------------|
| Personalization | Global Theme, Colours, Wallpaper, Icons, Fonts, Splash Screen modules. "Reset to Boswas Default" returns to the Boswas Prism look a new user starts with (`boswas-preset reset`) | A preset; `~/.config/boswas/compositor.toml` for the cursor; [returning to Prism](../../desktop/presets/README.md#the-native-boswas-desktop) |
| Network | Wi-Fi & Networking (new networks, passwords, VPN) and Proxy modules | `nmtui` in Konsole for a new network; the page's own switches for the rest |
| Bluetooth | The whole page's module | `bluetoothctl` in a terminal, where the Bluetooth service is installed |
| Display | Display Configuration, Night Light | `[output] scale` in the compositor configuration; no arrangement, resolution or night-light setting yet |
| Sound | Sound module | `pavucontrol-qt` (PulseAudio Volume Control, in the launcher) |
| Power | Power Management module | Lid, power button and suspend follow systemd-logind's defaults; no screen-dimming or power-profile setting yet |
| Users | Users module | An administrator uses `adduser`, `passwd` and `usermod` in a terminal |
| Devices | Mouse, Keyboard, Touchpad, Drawing Tablet modules | `[keyboard]` and `[pointer]` in the compositor configuration; no tablet mapping yet |
| Security, Privacy | Firewall and Screen Locking modules; Recent Files, KDE Wallet, KDE User Feedback | The firewall is the Boswas nftables baseline; the lock timeout is `[lock] idle_seconds`, bounded by the device policy |
| System | Region & Language, Date & Time, Notifications | `localectl` and `timedatectl` (administrator); there is no notification service yet |
| Applications | Default Applications, Autostart modules | `xdg-mime default` and `xdg-settings`; `~/.config/autostart` |
| Storage, Hardware & Drivers | KDE Partition Manager and System Information (KInfoCenter), unless installed | The pages' own lists |

These are gaps of the first version of the native desktop, not design
decisions: native settings pages for display, input, sound, network and
users are future work (see the roadmap).

## Safety rules

* Control Center runs as the desktop user; it refuses to start as root.
* Only `commands.py` starts processes, from an allowlist of absolute paths,
  with validated arguments and a timeout, never through a shell.
* Administrator work goes only to `/usr/libexec/boswas/sysops` (package
  `boswas-sysops`) as `pkexec /usr/libexec/boswas/sysops <operation> ...`.
  `commands.sysops_argv()` accepts only the operations Control Center uses and
  validates each operation's arguments (package names, one absolute path,
  remote names); `pkexec` cannot be started any other way, and is never
  started detached or with `run()`.
* `apt-get` is only ever run as a simulation (`-s`); `apt-cache` only for
  search, show and policy; `flatpak` changes only the user's installation;
  `nmcli` only for queries, the Wi-Fi radio and connecting devices.
* Only `userfiles.py` writes or deletes files, and only in `~/Applications`,
  `~/.local/share/applications/boswas-appimage-*.desktop` and
  `~/.cache/thumbnails` (no symbolic links followed out of them).
* Nothing privileged runs when the window opens or a page is shown. Every
  change starts with a click and a confirmation that says what will change.
  (Tested: opening every page, in an installed and a live session, records no
  privileged command and no state change.)
* Success is shown only when the helper's final `result` line says
  `ok: true` and it exited with status 0. Authorization cancelled (exit 126 or
  127), contract error codes, and output without a result are failures with a
  message; Retry is offered for `NETWORK` and `LOCKED`.
* One long operation at a time across the whole window. Cancel is offered only
  while the helper says the phase can be cancelled; it writes `cancel` to the
  helper's standard input. Closing the window during a phase that cannot be
  cancelled (dpkg running) keeps the window open; it can close by itself when
  the operation has finished.
* Unknown states say "Unknown" or "Not available", never "enabled". Google Play
  is shown exactly as `boswas-android` assesses it; Control Center never offers
  it on its own.

## Contracts

Control Center is written against
[docs/architecture/platform-contracts.md](../architecture/platform-contracts.md).
Assumptions where the contract leaves details open:

* A `boswas-android --json` command that is not a privileged step may print
  one JSON document with `schema` instead of JSON Lines; it counts as success
  only with exit status 0 and without `"ok": false`.
* Flatpak remotes are enabled system-wide by sysops; to install for the user,
  Control Center first adds the same remote to the user's installation from
  the pinned file `/usr/share/boswas/sysops/flatpak-remotes/<name>.flatpakrepo`
  (`flatpak remote-add --user --if-not-exists`), only for remotes the
  administrator enabled.
* `apt-clean` and `journal-vacuum` report `data.freed_bytes`;
  `firmware-report` reports `data.source` and `data.missing` as in the
  contract.

## Tests

`packages/boswas-control-center/tests`: pure parsers and rules
(`test_parsers.py`, `test_viewmodel.py`), the job protocol and the real
streaming runner with a stand-in helper (`test_jobs.py`), the backend on a
fake root and fake programs (`test_backend.py`, `test_functional_backend.py`),
widget tests offscreen (`test_ui.py`, `test_functional_ui.py`) and static
safety guarantees (`test_safety.py`).
