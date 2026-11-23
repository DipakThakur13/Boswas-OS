# Boswas Control Center

The settings hub of Boswas OS (`boswas-control-center`, package
`boswas-control-center`; Super+I). It is laid out like the Settings of
Windows 11 (ADR-0049): categories in a pane on the left, the pages of a
category as cards, and on each page the settings of its area, the state
of the device as the system reports it, and the actions that belong there.
This document describes what each page reads, what it can change, and the
rules that keep it safe.

Control Center was written for the KDE Plasma image, where its pages
opened KDE's settings modules. Those modules left with Plasma. The pages
now hold their settings themselves, as **native sections**: rows with a
switch, a list or a slider that apply at once and show a message next to
the control when a change could not be made (ADR-0041). No page depends
on a program of another desktop; the code names no KDE program, and a
test keeps it so.

## The window

- **The pane** holds the eleven categories: Home, System, Bluetooth &
  devices, Network & internet, Personalization, Apps, Accounts, Time &
  language, Accessibility, Privacy & security and Boswas Update. Up and
  Down switch between them.
- **A category page** lists its pages as cards, each with its icon and one
  line of what it holds. A page that has nothing to show on this device
  (no section and no content of its own) has no card, and a category
  without such a page is left out of the pane. The Install Boswas OS card
  appears only in a live session.
- **The title is the path** to the current place: System, a chevron,
  Display. A crumb, the back button or Alt+Left returns.
- **Size:** 1100 by 720 pixels at the start, 560 by 440 at least. Below a
  width of 720 pixels the pane shows the categories' icons only.
- **Keys:** Tab moves into the page, F5 refreshes it, Ctrl+F opens the
  Software Center's search, Ctrl+Q quits (an operation that cannot be
  interrupted safely finishes first).
- **One window per session.** Starting the program again brings the
  running window forward and shows the place the new command line names.

**Home** shows the device at a glance: a picture of its kind (laptop,
desktop computer, virtual machine), its name with Rename, its maker and
model; beside it the person signed in, the network connection and Boswas
Update, each opening its page. Below, in two columns where the window is
wide enough: recommended settings (dark mode and night light as switches,
links to Display and Sound), the Bluetooth devices, the background and
theme, and how full the storage is. Home reads public files
(`/etc/hostname`, `/etc/machine-info`, the DMI fields of
`/sys/class/dmi/id` without serial numbers), the services bridge and the
compositor's configuration; nothing privileged runs.

## Search

Typing in the search box above the categories lists the results in the
content area: categories, pages, sections and about forty single settings
(`catalog.SETTINGS`: "Rename this device", "Empty the Trash", "Sticky
keys", ...) whose names or keywords contain every word. Titles that are
the query come first, then titles that start with it, then titles that
contain every word, then matches by keyword only; places before sections
before single settings. A result opens its place and scrolls to its
section, and Enter opens the first. What this device does not show is not
found.

## Deep links

`boswas-control-center --page KEY [--section NAME]` opens a place
directly; `--help` and `boswas-control-center(1)` list every key. A
section is named by its title in lower case with dashes (`wi-fi`,
`night-light`, `date-time`); a name the page does not have is ignored.
The shell uses these links: quick settings ("More Wi-Fi settings" opens
`--page wifi`, "More Bluetooth settings" `--page bluetooth`, "More
sound settings" `--page sound`), Start's search, the user menu ("Account
settings", `--page users`), the notification centre's settings button
(`--page notifications`) and the menus of the desktop ("Display
settings", "Personalise") and of the taskbar's background ("Taskbar
settings", `--page taskbar`). A test checks that every key the shell
passes exists.

| Category (key) | Pages (keys) |
|----------------|--------------|
| Home (`home`) | – |
| System (`system`) | `display`, `sound`, `notifications`, `power`, `storage`, `multitasking`, `troubleshoot`, `recovery`, `about`, `install` (live session only) |
| Bluetooth & devices (`devices`) | `bluetooth`, `printers`, `cameras`, `mouse`, `touchpad`, `keyboard`, `usb`, `hardware` |
| Network & internet (`network`) | `wifi`, `ethernet`, `vpn`, `airplane`, `proxy`, `advanced-network` |
| Personalization (`personalization`) | `background`, `colours`, `themes`, `start`, `taskbar`, `fonts` |
| Apps (`apps`) | `installed-apps`, `applications` (the Software Center), `default-apps`, `startup`, `optional-features`, `windows`, `android` |
| Accounts (`accounts`) | `users`, `work` |
| Time & language (`time-language`) | `date-time`, `region` |
| Accessibility (`accessibility`) | `text-size`, `visual-effects`, `mouse-pointer`, `magnifier`, `color-filters`, `accessibility-keyboard` |
| Privacy & security (`privacy-security`) | `security`, `lock-screen`, `app-permissions`, `recent-files`, `privacy` (Diagnostics & feedback) |
| Boswas Update (`updates`) | `update-history`, `update-options` |

**Every key of earlier versions still works.** `system`, `devices`,
`network` and `personalization` were pages and are now category keys.
With `--section`, a section is also looked for where it moved
(`catalog.MOVED_SECTIONS`): `--page system --section date-time` opens
Time & language > Date & time, `--page network --section wi-fi` opens
Network & internet > Wi-Fi, `--page devices --section keyboard` opens
Bluetooth & devices > Keyboard. The section names of earlier versions are
mapped as well (`catalog.SECTION_ALIASES`: `other-connections` opens
Ethernet).

## Pages

"The bridge" is the services bridge and "the compositor's configuration"
the user's layer of the compositor's settings; both are described under
[Backends](#backends).

### System

| Page | What it holds | Backend |
|------|---------------|---------|
| Display | **Monitors**: a picture of the arrangement that is changed by dragging or from the keyboard; per monitor use, resolution, refresh rate, variable refresh rate (ADR-0048), scale and orientation. The scale box marks the scale the compositor recommends for the monitor, from its size and resolution as Windows does, "(Recommended)" (ADR-0055), and shows it while no scale is set; choosing it again removes the monitor's own setting, so the monitor stays automatic (where a scale is set for every monitor, the chosen one is written for the monitor instead). The resolution box marks the monitor's native resolution "(Recommended)". **Night light**: switch, strength, schedule | The compositor's configuration; `outputs` (with `recommended_scale`) |
| Sound | **Output** and **Input**: device, volume, mute, "Allow louder than 100 %" | The bridge |
| Notifications | **Do not disturb**: the switch the session starts with, and "Turn on do not disturb automatically" during set hours. **Notifications from apps**: every application that has sent notifications, with a switch for its banners (off: it still reaches the notification centre) | `[notifications]` of the compositor's configuration; the list of senders the shell keeps (`~/.local/state/boswas/notification-senders.json`), read only |
| Power & battery | **Battery**, **Power mode**, **Brightness**; **Screen** ("Turn off the screen after") | The bridge; `[power]` |
| Storage | The usage of `/` and `/home`; disks and file systems; **Free up space**: downloaded package files, old system logs, the thumbnail cache; **Cleanup recommendations**: the biggest items of the cache folder (the person ticks what to clear), the Trash with its size (Empty, after a question), downloads not changed for 14, 30, 60 or 90 days (ticked items go to the Trash); Browse files | statvfs, `lsblk -J -b` (no serial numbers); sysops `apt-clean` and `journal-vacuum`; `userfiles.py` for the user's files; `boswas-files /` |
| Multitasking | **Snap windows**: on or off, the snap layouts on the maximise button and when a window is dragged to the top of the screen. **Desktops**: whether the taskbar and Alt+Tab show the windows of the current desktop or of all | `[multitasking]` |
| Troubleshoot | Four checks, each run by its button, read-only, with each step Fine, a Problem or a Note and what helps: **Internet connection** (an adapter that is up, an address, a default route, name servers, then a name lookup of `deb.debian.org` and an HTTPS request to it; the page says before it runs that it contacts that host), **Sound**, **Bluetooth**, **Display** | `ip -j`, `/etc/resolv.conf`; the bridge; `systemctl is-active bluetooth.service`; `outputs` |
| Recovery | **Advanced start-up**: restart into the firmware settings, or into the boot loader's menu, each offered only where logind says the device supports it and after a question. A note says that there is no reset of the device: reinstalling starts again | busctl: logind's `CanRebootToFirmwareSetup` and `CanRebootToBootLoaderMenu`; `systemctl reboot --firmware-setup`, `systemctl reboot --boot-loader-menu=0` |
| About | Release, image, kernel, processor, memory, storage, the desktop (Boswas Desktop and its version), the Security and Windows Compatibility summaries, a system overview (load, network, the Android runtime, and pending updates and missing firmware where another page already found them); **Copy** puts the specifications on the clipboard; **Device name** with Rename (1 to 63 letters, digits and hyphens, not starting or ending with one, checked as it is typed) | `/usr/lib/boswas/release`, `image-info`, `/proc`, `/usr/share/boswas/compositor/version`; `hostnamectl --static set-hostname NAME` (systemd-hostnamed asks polkit) |
| Install Boswas OS | Live session only: explains the installation and offers a confirmed restart, so that the installer can be chosen in the boot menu. Nothing starts an installer | `systemctl reboot` |

### Bluetooth & devices

| Page | What it holds | Backend |
|------|---------------|---------|
| Bluetooth | The switch, **Add device**, paired devices (battery, Connect and Disconnect, Remove), found devices (Pair) | The bridge |
| Printers | With CUPS: the printers, their state and the default printer, "Print a test page" (CUPS's own test page, after a question), and a link to CUPS's own settings in the browser (`http://localhost:631/`). Printers on the network that support driverless printing appear by themselves. Without CUPS: "Install printing support" | `lpstat -e`, `lpstat -p -d`; `lp -d PRINTER /usr/share/cups/data/testprint`; sysops `apt-install cups` |
| Cameras | The cameras of the device, one per device, with the bus; Open Camera | `/sys/class/video4linux`; `boswas-camera` |
| Mouse | Pointer speed, enhance pointer precision, primary button, scroll direction, pointer size | `[pointer]` |
| Touchpad | Tap to click, scroll direction, speed, ignore while typing, three- and four-finger gestures. Without a touchpad the page says that the settings apply when one is connected | `[touchpad]` |
| Keyboard | Layouts (at most four, the limit of XKB), a second switching shortcut, repeat delay and rate, Num Lock | `[keyboard]` |
| USB | The USB devices connected now, with their makers and drivers; never serial numbers | `/sys/bus/usb/devices` |
| Hardware & Drivers | The PCI and USB devices with IDs, class and bound driver (never serial numbers); missing firmware with the Debian package that provides it | sysfs, `pci.ids`, `usb.ids`; sysops `firmware-report` (read-only, no password), `apt-install` |

### Network & internet

| Page | What it holds | Backend |
|------|---------------|---------|
| Wi-Fi | The switch, the connected network, the networks with Connect and Forget, Scan, a passphrase dialog | The bridge |
| Ethernet | The wired connection, connected or not | The bridge |
| VPN | The VPN and WireGuard connections of NetworkManager: Connect, Disconnect, Remove (after a question), and **Add**: a WireGuard or OpenVPN file the person chooses, recognised by its content and imported. A VPN that asks for a password or another secret when it connects cannot be connected from here: the desktop has no prompt for NetworkManager's secrets yet, and the page says so and suggests saving the secret with `nmtui` | `nmcli -t ... connection show`; `nmcli connection up\|down\|delete uuid UUID`, `nmcli connection import type openvpn\|wireguard file PATH` (NetworkManager asks polkit) |
| Airplane mode | The switch for every radio | The bridge |
| Proxy | The proxy variables of this session, read-only | The environment |
| Advanced network settings | **Network adapters**: every adapter with its state and connection, the data it moved since the device started, its hardware address when asked for, and Disable (after a question) or Enable. **Connection details**: addresses, gateways, name servers, hardware address, link speed, with Copy | `nmcli -t ... device`, `/sys/class/net`; `nmcli device disconnect\|connect IFNAME`; `ip -j`, `/etc/resolv.conf` |

### Personalization

| Page | What it holds | Backend |
|------|---------------|---------|
| Background | The Boswas wallpapers and the pictures of the user's Pictures folder as thumbnails, any PNG or JPEG file through Browse, or the default background | `[appearance] wallpaper` in `appearance.toml` |
| Colours | **Colour mode** (dark or light; a Boswas wallpaper changes to its artwork for the other mode, as a preset does). **Accent colour**: one of eight hues of the palette (iris, the default, azure, sky, mint, sun, coral, orchid, slate); the shell, the compositor's own surfaces and every Qt application take it. **Transparency effects**: off fills the taskbar, the flyouts and Task View's strip with their tint alone | `scheme` in `appearance.toml`; `accent` and `transparency` in the user's `compositor.toml` |
| Themes | The Boswas presets (light or dark, wallpaper, the colours of KDE applications) and "Reset to Boswas Default" | `boswas-preset --json list`, `apply`, `reset` |
| Start | Layout (more pins, default, more recommendations), recently added and most used applications under "Recommended", the folders next to the power button (Settings, Files, Documents, Downloads, Music, Pictures, Videos) | `[start]` |
| Taskbar | **Taskbar items**: Task View. **Taskbar behaviours**: alignment (centre or left), automatically hide the taskbar, badges on taskbar buttons (the number of notifications), seconds in the clock | `[taskbar]` |
| Fonts | The installed font families with a search box and a preview at a chosen size; **Add**: a `.ttf`, `.otf` or `.ttc` file, checked by its header and at most 64 MB, installed for this account only; a family the user installed can be removed after a question. Fonts of the system come with packages and are not removed here | `fc-list` (fixed format); the file is copied to `~/.local/share/fonts` (`userfiles.py`), then `fc-cache -f` on that folder |

### Apps

| Page | What it holds | Backend |
|------|---------------|---------|
| Installed apps | Every application with where it comes from, its version and size where known; search, a filter by source and a sort order. Each is removed the way it was installed: a Debian package through the system helper, the user's Flatpak application, an AppImage (its file and menu entry), a Windows application in the Compatibility Manager. Packages of Boswas OS, Flatpak applications installed for every user and entries of unknown origin have no Uninstall, and the row says why | Desktop entries, `dpkg-query -S` and `-W`, `flatpak list`, `~/Applications`, the session agent; sysops `apt-remove`, `flatpak uninstall --user`, `userfiles.py`, `boswas-compat-manager` |
| Software Center | Installed applications (start with `gio launch`); Debian packages (install, remove), Flatpak applications for the user, AppImages; install from a file, routed by its content (`.deb` → sysops `apt-install-local` with an "origin not verified" warning, Windows program → `boswas-compat-manager --install FILE`, APK → the Android page, AppImage → AppImages) | `apt-cache search/show/policy`, `flatpak remotes/list/search`; sysops `apt-install`, `apt-remove`, `flatpak-remote-enable` |
| Default apps | The application for each of eight kinds of file and link. Without a choice the system's defaults apply, which `boswas-apps` ships (ADR-0054: pictures in Photos, PDF in Document Viewer, text in Text Editor, music and videos in Media Player, folders in Files, the web in Firefox ESR, and so on); the shell opens files by the same files | `~/.config/mimeapps.list`; read with `/etc/xdg` and `/usr/share/applications/mimeapps.list` |
| Startup | Applications that start at sign-in: on or off, add, remove one the user added | `~/.config/autostart` |
| Optional features | Codecs, printing, a PDF printer, 7-Zip, Chinese, Japanese and Korean fonts, colour emoji, fonts for office documents, Flatpak, developer tools; and Firefox in each language whose locale is generated on this device. Install and Remove | `dpkg-query -W`, `locale -a`; sysops `apt-install`, `apt-remove` |
| Windows Compatibility | Runtime health and the Windows architectures it supports; opens the Compatibility Manager | Session agent `system.status`, `apps.list` |
| Android | Install, initialise, start, stop, restart, repair, update and remove the runtime; install an APK after inspection; open and uninstall apps; install F-Droid (labelled as not Google Play) | `boswas-android --json` |

### Accounts

| Page | What it holds | Backend |
|------|---------------|---------|
| Your info & other users | **Your account** (picture, name, user name, type; change picture, name, password), then the other users (account type, reset password, remove, optionally with the user's files) and **Add user** | AccountsService through `busctl`; `passwd` for the own password |
| Access work or school | Whether an organisation manages this device | The device agent's status |

### Time & language

| Page | What it holds | Backend |
|------|---------------|---------|
| Date & time | Automatic time, time zone (searchable), the time by hand while automatic time is off | `timedatectl`; systemd's service asks polkit |
| Language & region | The system language and the regional format, from the generated locales; they apply after signing in again | `localectl`, `locale -a` |

### Accessibility

| Page | What it holds | Backend |
|------|---------------|---------|
| Text size | 100 to 225 percent, previewed, applied with a button. The shell, the title bars (as far as their 32 px height allows, about 180 %) and every Qt application draw their text that much larger; nothing else grows | `[accessibility] text_scale` |
| Visual effects | Always show scroll bars (Qt applications), "Dismiss notifications after this amount of time" (5 seconds to 5 minutes, a minimum), Transparency effects (also on Colours), Animation effects. This is the one place for the animation switch | `[accessibility] always_show_scrollbars`, `[notifications] toast_seconds`, `[appearance] transparency` and `reduced_motion` |
| Mouse pointer | The pointer size, the same setting as on the Mouse page | `[pointer] cursor_size` |
| Magnifier | On or off, the zoom (100 to 1600 percent), the zoom increment of Super+Plus and Super+Minus; Super+Esc ends it | `[magnifier]` |
| Colour filters | On or off, the filter (greyscale, inverted, greyscale inverted, and corrections for deuteranopia, protanopia and tritanopia), and whether Super+Ctrl+C switches it. Where the compositor draws with pixman the controls are disabled and the page says why | `[color_filter]`; `color_filters` of the compositor's `version` answer |
| Keyboard | **Sticky keys**: Shift, Ctrl, Alt and AltGr pressed on their own stay down for the next key; pressed twice, they lock. **Filter keys**: slow keys (a key counts only after it was held) and bounce keys (a repeated press soon after its release is ignored) | `[keyboard] sticky_keys`, `sticky_keys_lock`, `filter_keys`, `slow_keys_ms`, `bounce_keys_ms` |

### Privacy & security

| Page | What it holds | Backend |
|------|---------------|---------|
| Boswas Security Center | AppArmor, the Windows-app sandbox, firewall, Secure Boot, disk encryption, screen lock, automatic security updates, device agent, device policy; each Secure, Warning, Attention, Info, Not available or Error, as text and icon | `boswas --json status` (its screen-lock check reads the compositor's lock policy), the device agent |
| Lock screen | "Lock automatically after", up to the limit of the device's policy, which the page shows as set by the organisation. "Show weather on the lock screen": the weather of the home place chosen in Weather, with the next hours; the page says that the lock screen itself never goes online (ADR-0051). Search finds the switch under "weather", "forecast" and "temperature" | `[lock] idle_seconds`, `[lock] show_weather` |
| App permissions | What sandboxed (Flatpak) applications were allowed or denied through the desktop portals (camera, microphone, location, screenshots, running in the background, ...), per application; **Revoke** forgets one decision after a question, so the application asks again. Native applications are not sandboxed and have the permissions of the account; the page says so | `flatpak permission-list`; `flatpak permission-remove TABLE OBJECT APP` (the user's own store) |
| Activity history | Clear the list of recently used files | `~/.local/share/recently-used.xbel` |
| Diagnostics & feedback | That Boswas OS collects no diagnostic data; what the device agent may send to an organisation that manages the device; the privacy statement | The device agent's configuration and status |

### Boswas Update

| Page | What it holds | Backend |
|------|---------------|---------|
| Boswas Update (the category itself) | Whether automatic security updates are on and when they last ran, the pending updates (security updates marked), Check for Updates, Install Updates (never removes packages); then the cards of its two pages | APT periodic settings and stamps, the unattended-upgrades log, `apt-get -s ... upgrade` as the user; sysops `apt-refresh`, `apt-upgrade` |
| Update history | What the package manager installed, upgraded and removed, newest first, and who started it (automatic updates, Control Center, a person). Where the log may not be read, the page says so | `/var/log/apt/history.log` and its rotated `.gz` files |
| Advanced options | The automatic update settings, the update source and channel, the device policy: the administrator's, shown and not changed | What the Update page read |

## Backends

**The services bridge.** Wi-Fi, Ethernet, airplane mode, Bluetooth,
Sound, Power and parts of Home and Troubleshoot talk to the system through
`/usr/libexec/boswas/boswas-services-bridge`, the same code the shell's
quick settings use (ADR-0035), so both always show the same state. One
bridge runs per window, from the first time such a page is opened until
the window closes. A Wi-Fi passphrase is written to the bridge's standard
input only. A switch shows the requested value until the service confirms
it; if it does not within six seconds the switch goes back and a message
says so. A service that is not running shows as not available.

**The compositor's configuration.** Display, the input devices, the look,
the taskbar, Start, multitasking, notifications, the accessibility
settings, the lock and screen times and the weather on the lock screen
are the compositor's settings.
Control Center writes the user's layer, `~/.config/boswas/compositor.toml`,
and asks the compositor to load it again (`reload-config`); it reads the
monitors with `outputs`. The colour mode and the background go to
`~/.config/boswas/appearance.toml`, the file `boswas-preset` writes;
everything else, the accent colour included, goes to `compositor.toml`,
because the next preset replaces `appearance.toml`. Control Center writes
no file of its own for the desktop's new settings: an administrator sets
other defaults in `/etc/boswas/compositor.toml`
([platform-contracts.md](../architecture/platform-contracts.md)).

- A display change that could leave the user without a picture
  (resolution, refresh rate, scale, orientation, turning a monitor off)
  is applied and then asks "Keep these display settings?" for fifteen
  seconds; without an answer, or on Escape, the previous file is put
  back.
- With no Boswas desktop running the settings are saved and apply at the
  next sign-in.
- Do not disturb, the magnifier and the colour filter work like night
  light: the setting is what a session starts with, and quick settings,
  the notification centre or the keys switch them for the session in
  between.
- Control Center never writes `[policy]`, `[shell]`, `[input_method]` or
  `[lock] command`: they are the administrator's.
- The file is rebuilt from what it held. Every documented key is kept.
  What cannot be kept (comments, a key the compositor does not know) is
  not dropped silently: the previous file is saved as
  `compositor.toml.bak` first and the page says so.

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

## Left out

Never a control that does nothing. Not offered:

- **Connecting a VPN that asks for a secret each time** (see VPN above),
  and importing other files than WireGuard and OpenVPN configurations.
- **Editing the proxy** (shown read-only), a test sound, an automatic
  suspend time (the compositor has no key for it), identifying monitors,
  a pointer theme or pointer colour (Boswas-Cursors is drawn in one
  colour when the package is built), drawing tablets, locking an
  account, setting the default printer.
- **Pausing updates:** nothing would resume them on time.
- **Resetting the device:** Recovery says to reinstall instead.
- **Notifications on the lock screen:** the lock screen is a program of
  its own that shows no notifications (ADR-0035).
- **Contrast themes:** they need a set of tokens of their own. The
  high-contrast preset Boswas Carbon covers applications only.
- **An "automatic" accent taken from the wallpaper:** the default accent
  is iris.
- **Underlined access keys** in menus and dialogs.
- **Functions that need an online service or a phone:** device
  activation, linking a phone, locating a lost device, online and family
  accounts. Also face sign-in, scanners, and a gaming category.

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
* Every other program has fixed forms, each built and checked in
  `commands.py`:
  - `apt-get` only as a simulation (`-s`); `apt-cache` only for search,
    show and policy; `apt-config shell` only; `dpkg-query` only to read
    which package owns a file (`-S`) and the state, size and version of
    packages (`-W` with a fixed format);
  - `flatpak` changes only the user's installation, and its permission
    store only with `permission-list` and `permission-remove TABLE OBJECT
    APP` with validated names;
  - `nmcli` for queries (`-t`), and for changes only what one builder
    makes, by connection UUID or adapter name: `connection up`, `down` and
    `delete uuid UUID`, `connection import type openvpn|wireguard file
    PATH`, `device connect` and `disconnect IFNAME`. No secret is ever on
    its command line; NetworkManager asks polkit;
  - `ip` only for three read-only JSON queries; `gio` only as `gio launch
    <desktop entry>`; `locale` only as `locale -a`;
  - `timedatectl` and `localectl` only in their fixed forms;
    `hostnamectl` only as `hostnamectl --static set-hostname NAME` with a
    validated name;
  - `busctl` only for calls to AccountsService with validated arguments,
    never with a password, and for logind's two read-only questions
    `CanRebootToFirmwareSetup` and `CanRebootToBootLoaderMenu`;
  - `systemctl` only as `reboot`, `reboot --firmware-setup`, `reboot
    --boot-loader-menu=0` and `is-active bluetooth.service`;
  - `fc-list` only with its fixed format; `fc-cache -f` only on the user's
    font folder; `lpstat` only as `-e` and `-p -d`; `lp` only for CUPS's
    own test page on a validated printer name;
  - `passwd` only without arguments, for the own password;
  - the Boswas programs `boswas`, `boswas-preset`, `boswas-android` (a
    fixed list of commands), `boswas-files`, `boswas-compat-manager` and
    `boswas-camera`.
* Only `userfiles.py` writes or deletes files, and only these, all in the
  user's home: `~/Applications` and
  `~/.local/share/applications/boswas-appimage-*.desktop`;
  `~/.cache/thumbnails`; through `write_config` the settings files
  `boswas/compositor.toml`, its backup, `boswas/appearance.toml`,
  `mimeapps.list` and `autostart/<id>.desktop` below the configuration
  directory; `~/.face`, the account picture; the list of recently used
  files, which it can empty; items of the cache folder the person picked;
  the Trash (emptied, and downloads the person picked moved into it,
  following the freedesktop Trash specification); and fonts in
  `~/.local/share/fonts`. A settings file is replaced atomically (a
  temporary file in the same directory, then a rename) with mode 0600.
  Names picked from a list are checked again: one component, a direct
  child of its folder. Nothing is written or deleted through a symbolic
  link; a link is removed as itself.
* Nothing privileged runs and nothing changes when the window opens or a
  page is shown. Every change starts with a click; administrator
  operations, restarts, and whatever removes, deletes or disables
  something first ask a question that says what will change. (Tested:
  opening every page, in an installed and a live session, records no
  privileged command, no change, no streamed operation and no question.)
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

`packages/boswas-control-center/tests` (688 tests, 5 skipped):

- the categories and pages, deep links, the moved sections and search,
  and the readers of the pages added with the categories: device name,
  storage cleanup, troubleshooters, printers and cameras, VPN connections
  and adapters, fonts, installed apps, optional features, app permissions
  and the update history (`test_places.py`);
- those pages in a whole window on a fake root with fake programs: what is
  shown, that nothing changes before a click and a confirmation, and the
  exact command or file change of each action (`test_pages_ui.py`);
- pure parsers and rules (`test_parsers.py`, `test_viewmodel.py`), the
  job protocol and the real streaming runner with a stand-in helper
  (`test_jobs.py`), the backend on a fake root and fake programs
  (`test_backend.py`, `test_functional_backend.py`), widget tests
  offscreen (`test_ui.py`, `test_functional_ui.py`, which also opens every
  page and checks that nothing privileged ran);
- static safety guarantees (`test_safety.py`): only one module writes
  files and one starts programs, every new program has its fixed forms, no
  secret reaches a command line, no KDE program is named, the man page
  lists every page;
- the settings: the registry and the section contract
  (`test_settings_framework.py`); every section with a fake bridge or
  against the real settings files read back as the compositor reads them
  (`test_settings_*.py`: taskbar and Start, colours, multitasking,
  notifications, accessibility among them); the bridge protocol
  (`test_servicebridge.py`); the compositor's keys, the rebuilding of the
  user's file and Settings' copy of the shipped defaults
  (`test_compositorconfig.py`, `test_shipped_defaults.py`, `test_xkb.py`);
  the file writer against symbolic links and read-only directories
  (`test_userfiles.py`); and that every page the shell links to exists.

**Not verified:** no page has run against a real system service: the
build environment has no NetworkManager, BlueZ, sound server,
AccountsService, PAM, polkit, CUPS, logind, systemd-hostnamed, Flatpak
permission store or fontconfig to run them against. The output formats of
`nmcli`, `lpstat`, `fc-list`, `dpkg-query` and `flatpak permission-list`
were written from their documentation and tested against fakes. Some package names of Optional features could not be checked
against the Debian archive (`gstreamer1.0-plugins-ugly`,
`gstreamer1.0-libav`, `printer-driver-cups-pdf`, `fonts-noto-cjk`,
`fonts-crosextra-carlito`, `fonts-crosextra-caladea`, `build-essential`,
the `firefox-esr-l10n-*` packages, `cups`), and installing `cups` may be
refused if APT would bring `cups-browsed` with it, which the image pins
out and the system helper's policy excludes. Three tests of the display
and input sections run against the real compositor on its headless back
end when `BOSWAS_TEST_COMPOSITOR=1` is set.
