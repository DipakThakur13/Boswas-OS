# Boswas Control Center

The settings hub of Boswas OS (`boswas-control-center`, package
`boswas-control-center`; Super+I). Every page shows the device as the
system reports it, holds the settings of its area and offers its actions.
This document describes what each page reads, what it can change, and the
rules that keep it safe.

Control Center was written for the KDE Plasma image, where its pages
opened KDE's settings modules. Those modules left with Plasma. The pages
now hold their settings themselves, as **native sections**: rows with a
switch, a list or a slider that apply at once and show a message next to
the control when a change could not be made (ADR-0041). No page depends
on a program of another desktop; the code names no KDE program, and a
test keeps it so.

Only one Control Center runs in a session: starting it again brings the
window forward. `--page PAGE` and `--section SECTION` open a place
directly (`boswas-control-center --page network --section wi-fi`), which
the shell's quick settings use. The search box above the page list finds
a page by its name and by the settings it holds.

## Settings sections

| Page | Sections | Backend |
|------|----------|---------|
| Network | **Wi-Fi** (switch, the connected network, the list of networks with Connect and Forget, Scan, a passphrase dialog); **Other connections** (Ethernet, VPN status, airplane mode); **Connection details** (addresses and name servers, with Copy); **Proxy** (shown, not editable) | The service bridge; `ip -j` and `resolv.conf` for the details |
| Bluetooth | The switch, **Add device**, paired devices (battery, Connect and Disconnect, Remove), found devices (Pair) | The service bridge |
| Sound | **Output** and **Input**: device, volume, mute, "Allow louder than 100 %" | The service bridge |
| Power | **Battery**, **Power mode**, **Brightness**; **Screen** ("Turn off the screen after") | The service bridge; the compositor's configuration for the screen time |
| Display | **Monitors**: a picture of the arrangement that is changed by dragging or from the keyboard, and per monitor use, resolution, refresh rate, scale and orientation; **Night light**: switch, strength, schedule | The compositor's configuration |
| Devices | **Mouse** (speed, precision, primary button, scroll direction, pointer size); **Touchpad** (tap, scroll direction, speed, ignore while typing, gestures); **Keyboard** (layouts, a second switching shortcut, repeat delay and rate, Num Lock) | The compositor's configuration |
| Personalization | **Colour mode** (dark or light), **Background**, **Motion**; the presets (`boswas-preset`) | The compositor's configuration |
| Security | **Lock screen**: "Lock automatically after", up to the limit of the device's policy, which the page shows as set by the organisation; the Security Center | The compositor's configuration |
| System | **Date & time** (automatic time, time zone, manual time), **Region & language** | `timedatectl`, `localectl`; systemd's services ask polkit where needed |
| Applications | **Default applications** (eight kinds), **Start-up applications**; the Software Center | `~/.config/mimeapps.list`, `~/.config/autostart` |
| Privacy | **Recent files** (clear the list) | The user's `recently-used.xbel` |
| Users | **Your account** (picture, name, user name, type; change picture, name, password); **Other users** (account type, reset password, remove, optionally with the user's files); **Add user** | AccountsService through `busctl`; `passwd` for the own password |

**The service bridge.** Network, Bluetooth, Sound and Power talk to the
system through `/usr/libexec/boswas/boswas-services-bridge`, the same code
the shell's quick settings use (ADR-0035), so both always show the same
state. One bridge runs per window, from the first time such a page is
opened until the window closes. A Wi-Fi passphrase is written to the
bridge's standard input only. A switch shows the requested value until
the service confirms it; if it does not within six seconds the switch
goes back and a message says so. A service that is not running shows as
not available.

**The compositor's configuration.** Display, Devices, the appearance and
the lock and screen times are the compositor's settings. Control Center
writes the user's layer, `~/.config/boswas/compositor.toml` (the colour
mode and the background go to `~/.config/boswas/appearance.toml`), and
asks the compositor to load it again (`reload-config`); it reads the
monitors with `outputs`.

- A display change that could leave the user without a picture
  (resolution, refresh rate, scale, orientation, turning a monitor off)
  is applied and then asks "Keep these display settings?" for fifteen
  seconds; without an answer, or on Escape, the previous file is put
  back.
- With no Boswas desktop running the settings are saved and apply at the
  next sign-in.
- Control Center never writes `[policy]`, `[shell]`, `[input_method]` or
  `[lock] command`: they are the administrator's.
- The file is rebuilt from what it held. Every documented key is kept.
  What cannot be kept (comments, a key the compositor does not know) is
  not dropped silently: the previous file is saved as
  `compositor.toml.bak` first and the page says so.
- At most four keyboard layouts can be set (the limit of XKB).

**User accounts.** The Users page asks AccountsService with `busctl` for
everything that is not a secret; where administrator rights are needed
the service asks polkit, and the shell shows the question.

- Your own password is changed by `passwd` on a pseudo-terminal, so the
  system's password rules apply.
- Another user's password, or a new user's, is turned into a SHA-512
  crypt hash in Control Center and given to the service over D-Bus. No
  password and no hash is part of a command line, and nothing typed is
  kept or logged.
- The account you are signed in with and the last administrator cannot be
  removed; the last administrator cannot be made a standard user. Adding
  a user is undone if setting the password fails.
- Without AccountsService the accounts of `/etc/passwd` are shown
  read-only, and only your own password can be changed.

**Left out** (never a control that does nothing): editing the proxy,
managing VPN connections, a test sound, an automatic suspend time,
identifying monitors, a pointer theme, drawing tablets, locking an
account.

## Pages

| Page | Reads | Actions |
|------|-------|---------|
| Personalization | `boswas-preset --json list` | apply a preset (light or dark, wallpaper, application colours); the sections above |
| Storage | statvfs of `/` and `/home`; `lsblk -J -b` (no serial numbers); size of `~/.cache/thumbnails` | remove downloaded package files (sysops `apt-clean`), shrink the system journal (sysops `journal-vacuum`), clear the thumbnail cache; browse the file system in Files (`boswas-files`) |
| Applications (Software Center) | desktop entries, Windows apps (session agent), `apt-cache search/show/policy`, `flatpak remotes/list/search`, AppImages in `~/Applications` | start an application (`gio launch`); install/remove Debian packages (sysops `apt-install`/`apt-remove`); enable Flathub (sysops `flatpak-remote-enable`); install/uninstall/update Flatpak apps for the user; add/remove AppImages; install from file (by content: `.deb` → sysops `apt-install-local` with an "origin not verified" warning, Windows program → `boswas-compat-manager --install FILE`, APK → Android page, AppImage → AppImages) |
| Windows Compatibility | session agent `system.status` (runtime health, supported Windows architectures), `apps.list` | open the Compatibility Manager |
| Android | `boswas-android --json status`, `app list`, `apk inspect FILE` | install, initialize, start, stop, restart, repair, update, remove the runtime; install an APK after inspection; open and uninstall apps; install F-Droid (labelled as not Google Play) |
| Security | `boswas --json status` (its screen-lock check reads the compositor's lock policy), device agent status and runtime check | the Lock screen section above |
| Updates | APT periodic settings and stamps, unattended-upgrades log, `apt-get -s ... upgrade` (simulation as the user) | check for updates (sysops `apt-refresh`), install updates (sysops `apt-upgrade`, never removes packages) |
| Hardware & Drivers | `/sys/bus/pci/devices`, `/sys/bus/usb/devices` (IDs, class, bound driver; never serial numbers), `pci.ids`/`usb.ids` | check for missing firmware (sysops `firmware-report`, read-only, no password), install the Debian package that provides a missing file (sysops `apt-install`) |
| About Boswas OS | release, image info, hardware, the desktop (Boswas Desktop and its version from `/usr/share/boswas/compositor/version`), plus a system overview: load (`/proc/stat`, `/proc/meminfo`), network, Android runtime, and pending updates / firmware only if another page already found them in this window | copy the information |

A page that has nothing to show on a system is left out of the list.

## Safety rules

* Control Center runs as the desktop user; it refuses to start as root.
* Only `commands.py` starts processes, from an allowlist of absolute paths,
  with validated arguments and a timeout, never through a shell. The one
  other child process is the service bridge, started by its fixed path
  without arguments (`ui/bridgeclient.py`).
* Administrator work goes only to `/usr/libexec/boswas/sysops` (package
  `boswas-sysops`) as `pkexec /usr/libexec/boswas/sysops <operation> ...`.
  `commands.sysops_argv()` accepts only the operations Control Center uses and
  validates each operation's arguments (package names, one absolute path,
  remote names); `pkexec` cannot be started any other way, and is never
  started detached or with `run()`.
* `apt-get` is only ever run as a simulation (`-s`); `apt-cache` only for
  search, show and policy; `flatpak` changes only the user's installation;
  `nmcli` and `ip` only for queries; `gio` only as `gio launch <desktop
  entry>`; `timedatectl` and `localectl` only in their fixed forms;
  `busctl` only for calls to AccountsService with validated arguments,
  never with a password; `passwd` only without arguments, for the own
  password.
* Only `userfiles.py` writes or deletes files, and only these, all in the
  user's home: `~/Applications`,
  `~/.local/share/applications/boswas-appimage-*.desktop`,
  `~/.cache/thumbnails`, and through `write_config` the settings files
  `boswas/compositor.toml`, its backup, `boswas/appearance.toml`,
  `mimeapps.list` and `autostart/<id>.desktop` below the configuration
  directory; `~/.face`, the account picture; and the list of recently
  used files, which it can empty. A settings file is replaced atomically (a temporary file in
  the same directory, then a rename) with mode 0600, and nothing is
  written through a symbolic link.
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
safety guarantees (`test_safety.py`). For the settings: the registry and
the section contract (`test_settings_framework.py`), every section with a
fake bridge or a model of the compositor's files
(`test_settings_*.py`), the bridge protocol (`test_servicebridge.py`),
the compositor's keys and the rebuilding of the user's file
(`test_compositorconfig.py`, `test_xkb.py`), and the file writer against
symbolic links and read-only directories (`test_userfiles.py`).

No section has run against a real system service: the build environment
has no NetworkManager, BlueZ, sound server, AccountsService, PAM or
polkit. Three tests of the
display and input sections run against the real compositor on its
headless back end when `BOSWAS_TEST_COMPOSITOR=1` is set.
