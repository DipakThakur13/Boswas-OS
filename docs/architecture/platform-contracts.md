# Platform contracts: boswas-sysops, boswas-android, Control Center, Boswas Desktop

The interfaces between the privileged helper, the Android runtime manager and
Boswas Control Center. Each component is tested against this document with
fakes of the others; a change here changes all three.

Section 4 describes the interfaces of the native desktop (`boswas-desktop`,
ADR-0026) that other Boswas components use: its configuration files, its
control socket, the service bridge for Settings (ADR-0035), the design
tokens and the version file.

## 1. boswas-sysops: the only privileged helper

`/usr/libexec/boswas/sysops` (Python 3, standard library only) performs a
fixed set of system operations as root. It is started by the desktop user's
programs as

    pkexec /usr/libexec/boswas/sysops <operation> [arguments...]

Never setuid, never a daemon, never a shell, never an arbitrary command.

* Runs only as root started through pkexec (`PKEXEC_UID` set); otherwise it
  prints a `result` with code `INVALID_ARGUMENT` and exits 2.
* Every operation has its own polkit action `com.boswas.sysops.<operation>`
  (annotations `org.freedesktop.policykit.exec.path` =
  `/usr/libexec/boswas/sysops` and `org.freedesktop.policykit.exec.argv1` =
  `<operation>`). Read-only operations: `allow_active=yes`; everything else:
  `allow_active=auth_admin_keep`; `allow_any` and `allow_inactive` are `no`.
* Environment is replaced: `PATH=/usr/sbin:/usr/bin:/sbin:/bin`,
  `LC_ALL=C.UTF-8`, `DEBIAN_FRONTEND=noninteractive`,
  `APT_LISTCHANGES_FRONTEND=none`.
* Each invocation is logged to `/var/log/boswas/sysops.log` (0640 root:adm):
  time, requesting UID, operation, validated arguments, result code. Never
  secrets, never file contents.

### Output: JSON Lines on stdout (UTF-8, one object per line, flushed)

    {"type": "start", "schema": "boswas-sysops/1", "operation": "apt-install", "cancellable": true}
    {"type": "progress", "phase": "download", "percent": 42.0, "message": "Downloading firefox-esr"}
    {"type": "log", "level": "info", "message": "..."}
    {"type": "result", "ok": true, "code": "OK", "message": "One sentence for the user.", "data": {}}

* `phase`: `refresh`, `download`, `install`, `configure`, `remove`,
  `cleanup`, `verify`, `init`, `service`, `inspect`.
* `percent`: number 0–100 or `null` (unknown).
* `level`: `info` or `warning`.
* Exactly one `result` line, always last, also on failure.
* `cancellable` in `start` says whether the operation can be cancelled at all;
  `progress` lines may carry `"cancellable": false` when a non-cancellable
  phase begins (dpkg unpack/configure).

### Result codes and exit status

| code | exit | meaning |
|------|------|---------|
| `OK` | 0 | done |
| `FAILED` | 1 | the operation ran and failed (`data.detail`: last lines of the tool's output) |
| `INVALID_ARGUMENT` | 2 | argument rejected by validation |
| `NOT_ALLOWED` | 2 | refused by Boswas policy (excluded or protected package, policy file) |
| `NOT_FOUND` | 2 | no such package in the configured, signature-verified repositories |
| `UNTRUSTED` | 2 | source not verified (unsigned repository, keyring fingerprint mismatch) |
| `DISK_SPACE` | 1 | not enough free space |
| `LOCKED` | 3 | another package manager holds the dpkg/APT lock (`data.holder`: `{"pid": n, "command": "..."}` when known) |
| `CANCELLED` | 4 | cancelled by the user before anything was changed |
| `NETWORK` | 5 | a download failed (no network, mirror unreachable) |
| `UNSUPPORTED` | 6 | hardware or system cannot run this (e.g. no binder support) |

pkexec itself exits 126 (authentication dismissed or failed) or 127 (not
authorized / helper missing); callers report those as "not authorized" without
reading stdout.

### Cancellation

The caller writes `cancel\n` to the helper's stdin. It is honoured only while
nothing has been changed (APT index refresh, package download, Android image
download); the helper stops the child process and reports `CANCELLED`. During
dpkg unpack/configure it logs a `warning` ("cannot be cancelled while packages
are being installed") and continues. End of stdin is not a cancel request.

### Operations (v1)

| operation | arguments | polkit default | notes |
|-----------|-----------|----------------|-------|
| `apt-refresh` | – | auth_admin_keep | `apt-get update`; `data.errors` lists failed sources |
| `apt-upgrade` | – | auth_admin_keep | `apt-get --with-new-pkgs upgrade` (never removes packages); `data.upgraded` |
| `apt-install` | `PKG...` (1–50) | auth_admin_keep | names `^[a-z0-9][a-z0-9+.-]{0,99}(:(amd64\|i386\|all))?$`; signed repositories only (no `--allow-unauthenticated`); Boswas-excluded packages refused |
| `apt-remove` | `PKG...` (1–50) | auth_admin_keep | simulated first; refused if it would remove a protected package (`boswas-*`, essential/required, kernel, bootloader, desktop core, security baseline) |
| `apt-install-local` | `/abs/path/file.deb` | auth_admin_keep | policy `LOCAL_DEB` in `/etc/boswas/apps.conf` (`allow` default, `deny`); copied to a root-only directory before inspection; result says it was not verified by a signed repository |
| `apt-clean` | – | auth_admin_keep | `apt-get clean`; `data.freed_bytes` |
| `journal-vacuum` | – | auth_admin_keep | `journalctl --vacuum-size=200M`; `data.freed_bytes` |
| `firmware-report` | – | yes | read-only, see below |
| `flatpak-remote-enable` | `NAME` | auth_admin_keep | NAME must have `/usr/share/boswas/sysops/flatpak-remotes/NAME.flatpakrepo` and be allowed by `FLATPAK_REMOTES` in `/etc/boswas/apps.conf` |
| `flatpak-remote-disable` | `NAME` | auth_admin_keep | |
| `android-runtime-install` | – | auth_admin_keep | see section 2; policy `ANDROID_RUNTIME` (`allow` default, `deny`) |
| `android-runtime-remove` | `[--purge-data]` | auth_admin_keep | removes packages, repository entry, pin, module configuration; `--purge-data` also `/var/lib/waydroid` |
| `android-init` | – | auth_admin_keep | `waydroid init -s VANILLA` (downloads images, verified by Waydroid's checksums; cancellable while downloading). Never GAPPS. |
| `android-update` | – | auth_admin_keep | `waydroid upgrade` |
| `android-repair` | – | auth_admin_keep | stop container, `waydroid upgrade --offline`, reload binder module, start container |
| `android-service` | `start\|stop\|restart` | yes | `systemctl <verb> waydroid-container.service` |

`firmware-report` data:

    {"source": "journal" | "dmesg" | "none",
     "missing": [{"file": "rtl_nic/rtl8125b-2.fw", "driver": "r8169", "device": "0000:03:00.0",
                  "package": "firmware-realtek" | null, "installed": false, "candidate": "20250410-2" | null}]}

Package mapping uses `/usr/share/boswas/firmware/index.tsv` (generated at
image build time from the Debian Contents indices: `<path below
lib/firmware>\t<package>` per line), then `dpkg -S` for installed files.

## 2. boswas-android: Android runtime manager (Waydroid)

Python package `boswas_android` in `/usr/lib/boswas/python`, CLI
`/usr/bin/boswas-android [--json] <command>`. Runs as the desktop user;
privileged steps call `pkexec /usr/libexec/boswas/sysops android-*` and pass
its JSON Lines through unchanged (`--json`) or as text.

Data shipped by the package:

* `/usr/share/boswas/keyrings/waydroid.gpg`: the Waydroid repository signing
  key (binary keyring), fingerprint recorded in
* `/usr/share/boswas/android/runtime.conf` (KEY="value", parsed without
  evaluation): `REPO_URL`, `REPO_SUITE`, `REPO_COMPONENTS`, `KEY_FINGERPRINT`,
  `ALLOWED_PACKAGES` (space-separated: what the APT pin lets through from that
  repository), `IMAGE_TYPE="VANILLA"`, `FDROID_URL`, `FDROID_CERT_SHA256`.

sysops `android-runtime-install` writes, from that file only:
`/etc/apt/sources.list.d/boswas-waydroid.sources` (deb822, `Signed-By:` the
shipped keyring after checking its fingerprint),
`/etc/apt/preferences.d/boswas-waydroid.pref` (origin pinned to -1 except
`ALLOWED_PACKAGES`), `/etc/modprobe.d/boswas-android.conf`
(`options binder_linux devices=binder,hwbinder,vndbinder`),
`/etc/modules-load.d/boswas-android.conf` (`binder_linux`); then refreshes,
installs `waydroid`, loads the module and disables autostart of
`waydroid-container.service` (started on demand by `android-service start`).

### `boswas-android --json status`

    {"schema": "boswas-android/1",
     "runtime": {"installed": bool, "version": str|null, "repository": bool},
     "compatibility": {"supported": bool,
                       "checks": [{"id": "binder"|"wayland"|"cpu"|"kernel"|"gpu"|"disk"|"secure-boot",
                                   "status": "PASS"|"WARN"|"FAIL"|"UNKNOWN", "title": str, "detail": str}]},
     "initialized": bool,
     "images": {"type": "VANILLA"|null, "system": str|null, "vendor": str|null, "bytes": int|null},
     "service": {"active": bool|null},
     "session": {"running": bool, "state": str|null},
     "storage": {"images_bytes": int|null, "user_bytes": int|null},
     "network": {"interface": "waydroid0", "present": bool, "address": str|null},
     "gpu": {"acceleration": "hardware"|"software"|"unknown", "driver": str|null, "render_node": str|null},
     "google_play": {"available": false, "reason": str, "alternatives": ["fdroid", "local-apk"]}}

Other commands (all accept `--json`; JSON output always has `schema`):

| command | effect |
|---------|--------|
| `check` | `{"schema", "compatibility": {...}}` only |
| `apk inspect FILE` | `{"schema", "apk": {...}}` (below); never executes anything |
| `apk install FILE` | inspect, refuse incompatible or split APKs, then `waydroid app install`; Android verifies the signature |
| `app list` | `{"schema", "apps": [{"package", "name", "version"}]}` |
| `app launch PKG` / `app remove PKG` | |
| `session start` / `session stop` | user-level Waydroid session |
| `runtime install\|init\|update\|repair\|remove [--purge-data]` | sysops `android-*` |
| `service start\|stop\|restart` | sysops `android-service` |
| `fdroid install` | explicit only: download the F-Droid client from `FDROID_URL` over HTTPS, require signer certificate SHA-256 = `FDROID_CERT_SHA256`, install. Labelled "F-Droid", never "Google Play" |
| `google-play` | the feasibility assessment (`google_play` object) |
| `logs [--lines N]` | Boswas Android log and the Waydroid log where readable |
| `cleanup` | after `runtime remove`: the user's Android data and leftover `waydroid.*.desktop` entries (asks unless `--yes`) |

APK document:

    {"path": str, "size": int, "sha256": str,
     "package": str|null, "version_name": str|null, "version_code": int|null,
     "label": str|null, "min_sdk": int|null, "target_sdk": int|null,
     "abis": ["x86_64", "arm64-v8a", ...],          // from lib/<abi>/ entries; [] = no native code
     "split": {"is_split": bool, "name": str|null, "requires_splits": bool},
     "signing": {"schemes": ["v1", "v2", "v3"], "certificates": [{"sha256": str}]},
     "compatible": bool, "problems": [str]}          // problems explain every refusal

Compatibility: the runtime is x86_64 (Waydroid images); APKs with native code
only for ARM ABIs are refused ("needs an ARM translation layer, which Boswas
OS does not include"); split APKs (`split` attribute or `isSplitRequired`)
are refused with an explanation that all parts must be installed together.
Inspection never claims an APK is safe.

## 3. Control Center integration

Control Center reaches these only through its allowlisted runner
(`commands.EXECUTABLES`): `/usr/bin/pkexec` always with
`/usr/libexec/boswas/sysops` as first argument and an operation from a fixed
set, and `/usr/bin/boswas-android`. Long operations use a streaming call that
reads JSON Lines, shows progress, offers Cancel only while the last `start` /
`progress` line says `cancellable`, and reports success only on a `result`
line with `ok: true`.

Its settings sections (ADR-0041, ADR-0049) use these interfaces of
section 4 and nothing else of the desktop: the service bridge, started by
its fixed path without arguments, one per window; the compositor's user
configuration, which it writes through its one file writer
(`userfiles.write_config`) before sending `reload-config` to the public
control socket; `outputs` for the monitors; `version`, whose
`color_filters` says whether the colour filters can be offered; and the
shell's list of notification senders, which it reads and never writes. It
never writes `[policy]`, `[shell]`, `[input_method]` or `[lock] command`.
The colour mode and the wallpaper go to `appearance.toml`; every other
key, the accent colour included, goes to the user's `compositor.toml`,
because `boswas-preset` replaces `appearance.toml` as a whole.

## 4. Boswas Desktop: configuration, control socket, service bridge, tokens

### Compositor configuration (TOML)

Layers, later ones overriding earlier ones key by key:

| File | Owner | May contain |
|------|-------|-------------|
| `/usr/share/boswas/compositor/compositor.toml` | boswas-desktop (never edited) | everything, including `[policy]` |
| `/etc/boswas/compositor.toml` | the administrator (not shipped) | everything, including `[policy]` |
| `$XDG_CONFIG_HOME/boswas/compositor.toml` | the user (and Settings, which writes it for the user) | everything except `[policy]` and the `command` keys of `[shell]`, `[lock]` and `[input_method]`, which are ignored there with a warning |
| `$XDG_CONFIG_HOME/boswas/appearance.toml` | `boswas-preset` (rewritten as a whole), and Settings for `scheme` and `wallpaper` | the `[appearance]` table only; anything else makes the file ignored |

A file that does not parse or contains an unknown key is reported and
ignored as a whole; the session still starts. `--config FILE` replaces all
layers (the login screen uses `/usr/share/boswas/compositor/greeter.toml`).

| Table | Keys |
|-------|------|
| `[keyboard]` | `layout`, `variant`, `model`, `options` (XKB names); `repeat_delay` (ms, 100–2000); `repeat_rate` (per second, 1–100); `numlock` (on when the session starts). Accessibility: `sticky_keys` (Shift, Ctrl, Alt and AltGr pressed alone stay down for the next key; Super keeps its own behaviour), `sticky_keys_lock` (pressed twice, a modifier stays down until pressed again; default `true`), `filter_keys` (turns on the two times that follow), `slow_keys_ms` (0, the default, or 100–5000: a key counts only after it was held this long), `bounce_keys_ms` (0 or 100–5000, default 500: a key pressed again this soon after its release is ignored) |
| `[pointer]` | mice and trackballs: `natural_scroll`, `accel_speed` (−1.0 to 1.0), `accel_profile` (`"adaptive"` or `"flat"`), `left_handed`, `cursor_theme`, `cursor_size` (16–96) |
| `[touchpad]` | `tap_to_click`, `natural_scroll`, `accel_speed` (−1.0 to 1.0), `disable_while_typing`, `gestures` (three- and four-finger swipes) |
| `[output]` | `scale`: 1.0, 1.25, 1.5, 1.75 or 2.0, for every output that has no setting of its own |
| `[outputs.NAME]` | one monitor, by connector name (`eDP-1`, `HDMI-A-1`): `enabled`, `mode` (`"1920x1080"` or `"1920x1080@60"`, one of the modes the monitor offers), `scale`, `position` (`[x, y]` of its top-left corner in logical pixels), `rotation` (`"normal"`, `"90"`, `"180"`, `"270"`), `adaptive_sync` (variable refresh rate where the monitor offers it; default `false`; ADR-0048). An invalid value is ignored alone. The last enabled monitor is never turned off |
| `[night_light]` | `enabled`; `temperature` (2500–6500 kelvin); `scheduled` with `from` and `to` (`"HH:MM"`, local time) |
| `[power]` | `screen_off_seconds` (0: never; otherwise 30 to 21600) |
| `[lock]` | `idle_seconds` (0: never, if the policy allows); `command` (the lock screen program; system files only) |
| `[shell]` | `command` (the shell program; system files only) |
| `[input_method]` | `command` (the input method program, started as a trusted client; empty for none; system files only) |
| `[xwayland]` | `enabled` (default `true`) |
| `[apps]` | `terminal`, `files`, `settings`, `task_manager` (argument lists for Ctrl+Alt+T, Super+E, Super+I and Ctrl+Shift+Esc) |
| `[appearance]` | `scheme` (`"dark"` or `"light"`), `reduced_motion`, `wallpaper` (PNG or JPEG file), `accent` (a hue of the palette: `"iris"`, the default, `"azure"`, `"sky"`, `"mint"`, `"sun"`, `"coral"`, `"orchid"`, `"slate"`), `transparency` (default `true`; `false` fills the taskbar, the flyouts and Task View's strip with their tint alone) |
| `[accessibility]` | `text_scale` (percent of the type ramp, 100–225: the shell, the title bars and the Qt applications draw their text larger and nothing else), `always_show_scrollbars` (Qt applications show full scroll bars) |
| `[color_filter]` | `enabled`; `filter`: `"grayscale"` (default), `"inverted"`, `"grayscale_inverted"`, `"deuteranopia"`, `"protanopia"`, `"tritanopia"`; `shortcut` (Super+Ctrl+C switches the filter; default `false`). Drawn with OpenGL ES only |
| `[magnifier]` | `enabled`; `zoom` (percent, 100–1600, default 200); `step` (what Super+Plus and Super+Minus change: 25, 50, 100, the default, 150, 200 or 400) |
| `[taskbar]` | Read by the shell: `alignment` (`"center"`, the default, or `"left"`), `auto_hide`, `task_view` (default `true`), `clock_seconds`, `badges` (default `true`) |
| `[start]` | Read by the shell: `layout` (`"default"`, `"more_pins"`, `"more_recommendations"`), `recently_added` and `most_used` (default `true`), `folders` (from `"settings"`, `"files"`, `"documents"`, `"downloads"`, `"music"`, `"pictures"`, `"videos"`; default `["settings", "files"]`) |
| `[multitasking]` | `snap_windows`, `snap_layouts_on_maximize`, `snap_layouts_on_drag` (all default `true`; the last two need the first), `taskbar_windows` and `alt_tab_windows` (`"desktop"`, the default, or `"all"`) |
| `[notifications]` | Read by the shell: `do_not_disturb`, `do_not_disturb_scheduled` with `do_not_disturb_from` and `do_not_disturb_to` (`"HH:MM"`; defaults 22:00 and 07:00), `banners_off` (names applications send, at most 256), `toast_seconds` (5, the default, 7, 15, 30, 60 or 300) |
| `[policy]` | `lock_idle_max_seconds` (0: no limit; shipped: 600), `privileged_protocols` (`"shell"`, shipped; `"all"` for development only) |

Shipped policy: the session locks after at most 600 seconds without input,
and only programs the compositor starts get the privileged protocols.
`boswas security status` (check `screen-lock`) reads the two system files
to report the lock policy. `boswas-compositor(1)` is the reference for
every key.

`reload-config` applies the keyboard, pointer and touchpad settings, the
appearance, the cursor, the monitors, night light, the lock and
screen-off times and the tables added for ADR-0049 to the running
session; the shell gets its part in the `config` event. The shell
command, the input method command and `privileged_protocols` are fixed
for the session. `[night_light] enabled`, `[magnifier] enabled`,
`[color_filter] enabled` and `[notifications] do_not_disturb` are what a
session starts with and take effect again when they change; in between,
quick settings, the notification centre and the keys switch them for the
session. An administrator sets other defaults for these keys in
`/etc/boswas/compositor.toml`.

`boswas-preset apply` writes `appearance.toml` (scheme and wallpaper),
merges the preset's colours into the user's `kdeglobals` for KDE
applications, records the preset in `boswasrc`, and sends `reload-config`.
It writes the three files itself and starts no program (ADR-0042).

### Control socket

`$XDG_RUNTIME_DIR/boswas-<WAYLAND_DISPLAY>.sock`, a Unix stream socket,
mode 0600. The path is printed by `boswas-compositor --print-env` as
`BOSWAS_COMPOSITOR_SOCKET`. UTF-8, one JSON object per line in each
direction; a line longer than 64 KiB closes the connection; at most 32
connections.

```
-> {"id": 1, "cmd": "version"}
<- {"id": 1, "ok": true, "data": {"name": "boswas-compositor", "version": "1.0.0-alpha.3", "trusted": false, "locked": false, "color_filters": true}}
-> {"id": 2, "cmd": "windows"}
<- {"id": 2, "ok": false, "error": "\"windows\" is not available on this connection"}
```

`id` is optional and echoed. Two kinds of connection:

- **public**: any connection to the socket file;
- **trusted**: the descriptor the compositor passes to a program it starts
  itself (`BOSWAS_COMPOSITOR_FD`: the shell, the lock screen, the greeter,
  a configured input method). With `privileged_protocols = "all"` public
  connections are trusted too.

Commands of every connection (the static test stage pins this set):

| Command | Arguments | Reply data |
|---------|-----------|------------|
| `version` | – | `name`, `version`, `trusted`, `locked`, `color_filters` (whether the renderer can draw the colour filters: `true` with OpenGL ES, `false` with pixman and on the headless back end) |
| `lock` | – | `null`; the session is locked when the reply arrives |
| `reload-config` | – | `{"warnings": [...]}` |
| `outputs` | – | every monitor: `name`, `description`, `make`, `model`, `physical_width_mm`, `physical_height_mm`, `enabled`, `x`, `y`, `width`, `height`, `scale`, `rotation`, `mode` (`width`, `height`, `refresh` in millihertz), `modes` (the same, with `preferred`), `usable`. A monitor that is connected and turned off is `{name, enabled: false}`. This is what every client already sees as `wl_output`, plus the modes |
| `capture` | `mode`: `"region"` (default) or `"screen"` | `null`. Opens the shell's capture screen, where the user chooses and confirms; nothing is captured by the command itself |

Commands of a trusted connection:

| Command | Arguments | Reply data |
|---------|-----------|------------|
| `subscribe` | – | `windows`, `workspaces`, `outputs`, `locked`, `appearance`, `night_light`, `layout`, `overview`, `show_desktop`, `capturing`; events follow |
| `windows` | – | list of `{id, title, app_id, active, minimized, tile, maximized, fullscreen, urgent, x11, workspace, output, x, y, width, height}` (for an X11 window `app_id` is its class) |
| `workspaces` | – | `{active, count}` |
| `workspace` | `index` | `{active, count}` |
| `new-workspace` | – | `{active, count}` |
| `close-workspace` | `index` | `{active, count}`; its windows move to the neighbour |
| `move-to-workspace` | `window`, `index` | `null` |
| `activate`, `minimize`, `toggle-minimize`, `close` | `window` (id) | `null` |
| `tile` | `window`, `tile`: `floating`, `maximized`, `fullscreen`, `left`, `right`, `top-left`, `top-right`, `bottom-left`, `bottom-right` | `null` |
| `overview` | optional `active` (without it: toggle) | `{active}`: whether Task View is open |
| `show-desktop` | optional `active` | `{active}` |
| `night-light` | optional `enabled` (without it: toggle), optional `temperature` | `{enabled, temperature, active}` |
| `next-layout` | – | `{layouts, codes, active}` |
| `thumbnail` | `window`, `path` (absolute; PNG), optional `width` (16 to 1024, default 240) | answered after the next frame |
| `screenshot` | `path` (absolute; PNG), optional `output`, optional `display` | answered after the next frame. With `"display": true` the picture is what the output shows the person in front of it, magnified and through the colour filter (for tests); without it, as every other capture, the picture without either |
| `capture-answer` | `request` (the id of a `capture-request` event), `allow` | `null`, or an error when no such question is open |
| `capture-stop` | – | `null`; every permission to capture ends |
| `quit` | – | `null`; the session ends |
| `pointer`, `button`, `key`, `scroll` | test input injection | refused without `--test-input` |

Events after `subscribe`, as `{"event": NAME, "data": ...}`:

| Event | Data | Meaning |
|-------|------|---------|
| `windows` | the window list | a window appeared, changed or went away (at most once per turn of the event loop) |
| `workspace` | `{active, count}` | desktops changed |
| `outputs` | as the `outputs` reply | monitors changed |
| `lock` | `{locked}` | the session was locked or unlocked |
| `config` | `scheme`, `wallpaper`, `reduced_motion`, `accent`, `transparency`, `text_scale`; `taskbar` (`alignment`, `auto_hide`, `task_view`, `clock_seconds`, `badges`, and `windows` from `[multitasking] taskbar_windows`); `start` (`layout`, `recently_added`, `most_used`, `folders`); `notifications` (`do_not_disturb`, `scheduled`, `from`, `to`, `banners_off`, `toast_seconds`). The same object is `appearance` in the reply to `subscribe` | after a reload |
| `night-light` | `{enabled, temperature, active}` | `active`: the screens are tinted now |
| `layout` | `{layouts, codes, active}` | the keyboard layouts or the active one changed |
| `overview`, `show-desktop` | `{active}` | Task View or show desktop was toggled |
| `launcher`, `quick-settings`, `notifications`, `run` | `{}` | Super, Super+A, Super+N, Super+R |
| `capture` | `{mode}` | Print, Super+Shift+S or the public `capture` command |
| `taskbar-app` | `{index, shift}` | Super+1 to 9 |
| `media-key` | `{key, locked}` | a volume, brightness or media key |
| `bell` | `{}` | an application rang the system bell |
| `capture-request` | `{id, program}` | a program asks to see the screen; answer with `capture-answer` |
| `capture-request-closed` | `{id}` | the question is over (timed out, or the session locked) |
| `capturing` | `{programs}` | which programs may capture now |

Command line: `boswas-compositor --send version|lock|reload-config|outputs|capture
[--control-socket PATH]` sends one public command and prints the reply. The
sleep hook (`/usr/lib/systemd/system-sleep/boswas-lock`) and the boot test
lock that way; `boswas-preset` writes `reload-config` to the socket itself.
There is no command that unlocks.

The limits of a public connection, the trust of a started program, the
lock behaviour and the capture question are pinned down by
`packages/boswas-desktop/crates/boswas-compositor/tests/headless.rs`.

### Service bridge (for Settings)

`/usr/libexec/boswas/boswas-services-bridge` runs the network, Bluetooth,
audio, power and brightness backends of the shell (`boswas-services`,
ADR-0035) as the user who starts it. One JSON object per line in each
direction, on its standard input and output; a line longer than 16 KiB is
an error; it ends when its input closes. `BOSWAS_LOG` sets the log
filter; messages go to standard error.

Events (standard output). Each carries the whole state of one service:

    {"event": "ready", "version": "1.0.0-alpha.3"}
    {"event": "network", "available": true, "connectivity": "full", "wired": false, "wifi_present": true,
     "wifi_enabled": true, "wifi_connected": "Home", "wifi_scanning": false,
     "access_points": [{"ssid", "strength", "secured", "known", "connected", "connecting"}],
     "vpn": false, "airplane": false, "error": null}
    {"event": "bluetooth", ...}   {"event": "audio", ...}   {"event": "power", ...}
    {"event": "brightness", "level": 0.8}
    {"event": "error", "message": "..."}

`connectivity` is `none`, `limited`, `portal` or `full`. A daemon that is
not running shows as `available: false`, never as an error. `error` in a
network event is in exactly one event; the state without it follows. The
field lists of the other events are in
`packages/boswas-desktop/crates/boswas-services-bridge/src/protocol.rs`.

Commands (standard input), as `{"cmd": NAME, ...}`:

| `cmd` | Fields |
|-------|--------|
| `refresh` | `service`: `network`, `bluetooth`, `audio`, `power` or `brightness` |
| `wifi-set-enabled`, `airplane-mode`, `bluetooth-set-enabled`, `bluetooth-discover` | `enabled` |
| `wifi-scan`, `wifi-disconnect` | – |
| `wifi-connect` | `ssid` (at most 32 bytes), optional `passphrase` (1 to 256 bytes) |
| `wifi-forget` | `ssid` |
| `bluetooth-connect`, `bluetooth-disconnect`, `bluetooth-forget` | `address` (`AA:BB:CC:DD:EE:FF`) |
| `set-volume`, `set-input-volume` | `volume` 0 to 1.5 |
| `set-muted`, `set-input-muted` | `muted` |
| `set-default-output`, `set-default-input` | `id` |
| `set-brightness` | `level` 0 to 1 |
| `set-power-profile` | `profile`: `power-saver`, `balanced` or `performance` |

Anything else is answered with an `error` event. The bridge offers no
power actions, notifications or authentication. A Wi-Fi passphrase travels
on the pipe only, never on a command line.

### Environment of a session

`boswas-session` sets `XDG_CURRENT_DESKTOP=Boswas`,
`XDG_SESSION_DESKTOP=boswas`, `XDG_SESSION_TYPE=wayland`,
`QT_QPA_PLATFORM=wayland;xcb` and the other toolkit variables;
`WAYLAND_DISPLAY` is set by the compositor. `DISPLAY` names the session's
X server (ADR-0034) for the programs the compositor and the shell start,
and is removed when there is none; it is not published to the systemd
user manager or to D-Bus activation. Programs started by the shell never
inherit `WAYLAND_SOCKET` or `BOSWAS_COMPOSITOR_FD`. Components detect the
native desktop by `Boswas` in `XDG_CURRENT_DESKTOP`.

### Files other components read

| Path | Written by | Read by | Content |
|------|-----------|---------|---------|
| `/usr/share/boswas/compositor/version` | boswas-desktop (package build) | Control Center (About). `boswas info` asks dpkg for the version of `boswas-desktop` instead | The package version, one line (for example `1.0~alpha3`) |
| `/usr/share/boswas/ui/tokens.json` | boswas-branding | `boswas_fluent` (the Qt toolkit) at run time, and any tool that needs Boswas colours or geometry | Boswas UI tokens, version 3 (ADR-0033). The Rust programs compile the same file in and do not read it at run time |
| `/usr/lib/boswas/python/boswas_fluent/` | boswas-branding | the Boswas Qt applications | The toolkit (ADR-0039); its interface is `desktop/ui/qt/README.md` |
| `/usr/share/boswas/kde-settings/kdeglobals` | boswas-branding | KDE and Qt applications (through `XDG_CONFIG_DIRS`) | Prism colour scheme, `Boswas-Prism` icon theme, Breeze widget style where a user has installed it |
| `/usr/share/icons/Boswas-Cursors/` | boswas-branding | the compositor (`[pointer] cursor_theme`), every client through `XCURSOR_THEME`, and programs that load the theme `default` through Debian's `x-cursor-theme` alternative (registered by the package, priority 150) | The pointer theme (ADR-0043): XCursor files at the nominal sizes 24, 32, 48, 64 and 96 under `cursors/`, `index.theme`, and `cursor.theme` for the alternative |
| `$XDG_CONFIG_HOME/boswas/shell.toml` | boswas-shell (may be edited by hand) | boswas-shell | `[taskbar] pinned` and `[start] pinned`: lists of desktop file ids |
| `$XDG_STATE_HOME/boswas/notification-senders.json` (default `~/.local/state/boswas/`) | boswas-shell, when a notification arrives (atomically) | Control Center (System > Notifications), read only | A JSON list, most recent first, at most 100: `{"name", "icon", "desktop_entry", "last"}` per application that sent notifications; `name` is the `app_name` it sends, which `[notifications] banners_off` names; `last` is seconds since the epoch. No notification content |
| `$XDG_STATE_HOME/boswas/apps-seen.json` | boswas-shell | boswas-shell (Start's "Recently added") | A JSON object: desktop file id → when the shell first saw the application installed, in seconds since the epoch (0 for those installed when the shell first ran); at most 4000; removed applications are forgotten. Recently added means the last 14 days |
| `/usr/share/xdg-desktop-portal/boswas-portals.conf` | boswas-desktop | xdg-desktop-portal (`XDG_CURRENT_DESKTOP=Boswas`) | GTK back end by default; the wlroots back end for ScreenCast and Screenshot |
| `/etc/xdg/xdg-desktop-portal-wlr/Boswas` | boswas-desktop | xdg-desktop-portal-wlr | `chooser_type=none`: the first monitor is shared |
| `/etc/boswas/greetd.toml` | boswas-desktop; live-config appends `[initial_session]` on the live medium only | greetd | Login configuration |
| `/etc/pam.d/boswas-lock` | boswas-desktop | `boswas-shell --lock` (libpam) | `common-auth` and `common-account` |

The boot screen's control protocol (`/run/boswas-bootsplash/socket`, root
only) is described in `packages/boswas-bootsplash/README.md`.
