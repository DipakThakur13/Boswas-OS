# Privacy: what administrators can and cannot see

Boswas OS is a company-managed system. Employees are entitled to know exactly
what is collected, who can see it, and what is never collected.

## 1.0~alpha3: nothing leaves an unenrolled device

The device agent runs on every device. A device sends nothing to Boswas
Group unless an administrator configures a Control Plane in
`/etc/boswas/device.conf` and enrolls it.

**Never on any device:**

- telemetry about how people use the device;
- crash reporting;
- reporting of local application launches.

Debian's popularity-contest is disabled at install. Without enrollment, the
only outbound connections the OS itself makes are:

- APT and unattended-upgrades to the Debian archive
- NTP time synchronisation
- NetworkManager connectivity checks, if enabled by Debian defaults
- Weather's update for the lock screen, every half hour during a
  session, only once the user has added a place in Weather and only while
  the lock screen shows the weather (see
  [The desktop](#the-desktop-what-stays-in-the-users-home))

An **enrolled** device additionally talks to its Control Plane over mutual
TLS, exactly as listed in "Device agent messages" below.

## Data categories

| Category | Examples | Collected locally | Sent centrally | Who may see it |
|----------|----------|-------------------|----------------|----------------|
| Device management | OS build, device ID, enrollment state, hardware model, agent version, device state | `boswas-device status`, `boswas device status` | Enrolled devices: heartbeat, inventory (see below) | Control Plane operators (viewer and above), audited |
| Security posture | Secure Boot, TPM, encryption, firewall, AppArmor and audit state | `boswas status` (on demand; the agent assesses it with each inventory) | Enrolled devices with `TELEMETRY_POLICY=security`: check results only | Control Plane operators |
| Security events | Changes to identity files, sudoers, security config, kernel modules, clock | auditd log (root-only), max retention set by the journal policy | **Not forwarded** in 1.0~alpha3 (only the agent's own management and security events) | Local administrators |
| Application inventory | Boswas packages and Windows apps | dpkg database; per-user WinCompat records (`~/.local/share/boswas/wine/<id>/app.json`) | Enrolled devices: application IDs, versions, statuses, states and installation **counts**, never user names | Control Plane operators |
| User content | Documents, browser history, e-mail, files in /home | Never by Boswas OS | **Never** | Only the user (and company-approved backup, once defined) |
| Private user activity | Keystrokes, screen contents, command lines, file reads | **Never** by the system. The desktop keeps a few small records in the user's own home for the user's convenience (see [The desktop](#the-desktop-what-stays-in-the-users-home)) | **Never** | Nobody but the user |

## The desktop: what stays in the user's home

The desktop shell (`boswas-shell`) writes these files in the user's home
directory. Nothing reads them but the shell and, for the list of
notification senders, Control Center; the device agent does not, and its
messages have closed field lists (below). The three files of ADR-0058
and ADR-0060 are written atomically with mode 0600 and never through a
symbolic link.

| File | Content | Why |
|------|---------|-----|
| `~/.local/state/boswas/launcher-usage.json` | How often each application was started: an application ID and a count, no times | Ranks search results in Start and fills its list of the most used applications |
| `~/.local/state/boswas/run-history.json` | The last 50 commands typed into the Run dialog | The arrow keys recall them |
| `~/.local/state/boswas/notification-senders.json` | The applications that sent notifications: the name each gives itself, its icon and desktop entry, and when it last sent one; at most 100, never what a notification said | Settings > System > Notifications lists them, to turn their banners off (ADR-0049) |
| `~/.local/state/boswas/apps-seen.json` | When each installed application was first seen | Start's "Recently added" (the last two weeks) |
| `~/.config/boswas/shell.toml` | The applications pinned to the taskbar and to Start, how Start lists all applications (by category, as a grid or as a list), and which cards the widgets board shows, in what order and size | The user's arrangement |
| `~/.local/state/boswas/clipboard-pinned.json` | The clipboard items the user pinned, and nothing else: text, lists of files, pictures of at most 256 KB; at most 25 items and 4 MB, in a directory of mode 0700 | Pinned items stay after signing out (ADR-0058) |
| `~/.local/state/boswas/emoji.json` | The emoji, kaomoji and symbols used last (at most 40) and the chosen skin tone | The emoji panel's "recently used" (ADR-0058) |
| `~/.local/state/boswas/desktop-layout.json` | Where desktop icons were put by hand (grid cells, by file name) and the order chosen with Sort by | The desktop keeps the user's arrangement (ADR-0060) |

Each is an ordinary file of the user and can be deleted; the shell reads
it when it starts.

- **Notifications** are held in the shell's memory until they are
  dismissed. They are not written to disk and not sent anywhere.
- **The clipboard history** (Super+V, ADR-0058) keeps the last 25 things
  the user copied (text, pictures up to 8 MB, lists of files) in the
  shell's memory only, so it is gone when the user signs out; only items
  the user pins are written, to `clipboard-pinned.json` above. It never
  keeps what a program marks as secret or transient: KDE's password
  manager hint (`x-kde-passwordManagerHint`, which password managers such
  as KeePassXC set; the shell reads that hint alone first, so the secret
  itself is not even requested), the concealed and transient markers of
  other systems, and Windows' "not for history" flag through Wine. Nothing
  copied while the session is locked is kept, and the lock screen and the
  login screen do not watch the clipboard. It is cleared by Clear all in
  the flyout (pinned items stay), by Settings > System > Clipboard >
  "Clear clipboard data" (everything copied before then except pinned
  items, and the clipboard itself if it still holds such an item), by
  unpinning or deleting an item, and by signing out; "Clipboard history"
  off keeps nothing but the pinned items. While an emoji is typed in, what
  was on the clipboard (a secret included) is held in the shell's memory
  until it is put back, at most about two seconds after the paste. Only
  the shell can read other programs' clipboard (clipboard control is
  offered to it alone), and nothing is sent anywhere.
- **The widgets board** (Super+W, ADR-0059) reads files the user's own
  applications keep (Weather's summary for the lock screen, Calendar's
  events, Clock's world clocks and alarms, the newest picture in
  Pictures), `/proc` for the processor and memory while it is open, and
  the shell's own media and battery state. It uses no network at all and
  shows no news or other feed: the only weather it shows is what Weather
  fetched for the place the user added (see Weather below).
- **Desktop icons** (ADR-0060) show the desktop folder. Thumbnails of
  pictures are made by the shell and kept in memory only, not written to
  `~/.cache/thumbnails` and not sent anywhere.
- **Account pictures** (ADR-0062): the shell shows the picture chosen in
  Settings in Start, on the lock screen and on the login screen. It reads
  `~/.face` and the copy AccountsService keeps in
  `/var/lib/AccountsService/icons/` (readable by every user of the device,
  as AccountsService makes it, so that the login screen can show it), at
  most 8 MB, and keeps a small round copy in memory. The login screen
  shows the pictures of the people it lists to anyone at the device; an
  account without a picture shows the person glyph.
- **Dark mode for GTK programs** (ADR-0062): the shell writes two values
  to the user's GSettings database (dconf), `color-scheme` and
  `accent-color` of `org.gnome.desktop.interface`: whether the desktop is
  dark and the name of an accent colour. Any program of the user may read
  them, which is their purpose; nothing else is written there.
- **Glass** (ADR-0063): for its own glass when it draws with pixman, the
  compositor reads back a small picture (480 pixels wide) of the shell's
  background surface, the wallpaper with the desktop icons, and keeps it
  in memory, blurred; it decodes no file. Live blur draws the windows
  behind a glass surface inside the compositor; the program that asked
  for it receives no pixels.
- **Screenshots.** The screen is captured only when the user asks (the
  Print key) or when the user allows a program that asked, in a dialog of
  the shell, with a mark on the taskbar for as long as that program may
  capture (`docs/security/baseline.md`). Pictures the user takes are
  saved to the user's Pictures folder. Neither Boswas OS nor the device
  agent captures the screen, and there is no remote command for it.
- **Wi-Fi passphrases** typed in quick settings or in Control Center go
  to NetworkManager, which stores the connection as it does for every
  other tool.
- **The Boswas applications** keep their settings and state in the
  user's home, under `~/.config/<application id>/` and
  `~/.local/state/<application id>/` (for example
  `com.boswas.Terminal`): window sizes, recent files, the Media Player's
  library index and play queue, the Text Editor's last session including
  text that was not saved, Clock's alarms and Calendar's events. None of
  it is sent anywhere. The applications use the network only where that
  is the function the user asked for (the Media Player playing an
  address); the Calculator and the Calendar never do.
- **Weather** (ADR-0046) is the one Boswas application that contacts an
  outside service by itself, and only once the user has added a place:
  it asks Open-Meteo (`geocoding-api.open-meteo.com` for the place names
  the user types into its search, `api.open-meteo.com` for the forecast of
  the coordinates of the places the user saved), when it opens, when the
  user refreshes and at the refresh interval the user chose while its
  window is open. Since ADR-0051 it also asks without its window, for
  the lock screen: the systemd user timer `boswas-weather-update.timer`
  runs `boswas-weather --update-lock-screen` a minute after the session
  starts and then every 30 minutes (up to three minutes later at random),
  for one place only, the home place (or the first place). It sends
  nothing when the user has added no place, or when "Show weather on the
  lock screen" is off, and uses a forecast that is less than ten minutes
  old instead of asking again. Open-Meteo sees the device's address
  and those place names or coordinates (rounded to four decimals, about
  10 m); no account, key or other identifier is sent. The app does not
  locate the device: places are only what the user picked. The places and
  the last forecast stay in the user's home
  (`~/.config/com.boswas.Weather/`, `~/.cache/com.boswas.Weather/`).
- **The lock screen** shows the weather of that place: its name, the
  temperature, the condition and the next hours, to anyone at the device,
  without signing in. That is why it can be turned off: Settings > Privacy
  & security > Lock screen > "Show weather on the lock screen" (the
  compositor's `[lock] show_weather`, on by default). The lock screen
  itself never uses the network; it reads the summary that Weather writes
  to `~/.local/state/boswas/weather.json` (mode 0600), which Weather
  removes when the setting is off or there is no place.
- **Snipping Tool** captures the screen only through the desktop's
  capture screen, where the user chooses the area and confirms; the
  pictures are saved where the user saves them.
- **Settings** (Control Center) writes and deletes only the files listed
  in `docs/experience/control-center.md`, all in the user's home: its
  settings files, and, when the person asks, items of the cache folder,
  the Trash, old downloads (moved to the Trash) and fonts. Privacy &
  security > Activity history > Clear empties the list of recently used
  files that applications share (`~/.local/share/recently-used.xbel`) and
  the recent lists of the Boswas applications that keep one: Text
  Editor's recent files, Document Viewer's recent documents with the page
  and zoom each was left at, Media Player's recently played items and
  where each video was stopped, Archive Manager's recent archives
  (ADR-0061). The files themselves, the applications' other settings and
  playlists stay; Files and Photos keep no such list; an application that
  is open writes its list again when it next saves it, so it should be
  closed first. Diagnostics &
  feedback says that Boswas OS collects no diagnostic data. The internet
  troubleshooter (System > Troubleshoot) looks up `deb.debian.org` and
  makes one HTTPS request to it, only when the person runs it and after
  saying so; Debian's server sees the device's address.

## Windows applications (WinCompat)

- **Local data only.** `boswas-winapp` keeps, per user and application, an
  installation record and logs of what the Windows program printed. Both
  stay in the user's home and are never sent anywhere.
- **What root sees.** Root's inventory listing (`--all-users`) reads only
  IDs, versions, statuses and states.
- **What a Windows application sees.** Only its own prefix and the folders
  its manifest grants; never the rest of the user's home.

## Device agent messages (enrolled devices only)

Every message has a closed field list
(`packages/boswas-device-agent/boswas_agent/privacy.py`, schemas in
`packages/boswas-device-agent/schemas/`). The agent checks each message
before queuing it, and the Control Plane checks it again and refuses
anything outside the list.

| Message | Content | Controlled by |
|---------|---------|---------------|
| Enrollment (once) | Device ID, certificate request, OS versions, hardware model facts (vendor, model, firmware version, CPU model, memory, TPM version, boot mode; **no serial numbers**), profile, optional device name, the one-time token | Administrator action |
| Heartbeat | Device ID, agent version, device state, applied policy version, inventory revision, number of pending results, time | `HEARTBEAT_INTERVAL` |
| Inventory | OS versions, Boswas package versions, Windows applications aggregated over users (ID, version, status, architecture, state, number of installations, number running; **no user names**), Windows runtime health; with `standard` also CPU, memory, model, firmware version and disk size | `INVENTORY_POLICY` (off, minimal, standard) |
| Status and compliance | Posture check IDs and results (`firewall: PASS`, …), compliance counts, OS versions, uptime, update channel | `TELEMETRY_POLICY` (none, security) |
| Events | Management actions and security events: remote installs, removals, launches and repairs, policy changes, policy refusals of remote commands | Always (only for management actions) |
| Command results | Command ID, status, short typed facts (application ID, version, state, booleans), error code and message; **never program output or logs** | Always |

A test fails if any other field is added. Changing these lists requires
updating this page first.

**What administrators see:** the dashboard shows exactly these documents,
the command history and the audit trail. It does not show who uses which
application: inventory is aggregated per device, and local launches are
never reported.

**Who sees what an administrator did:** every operator action is recorded
with its actor in the Control Plane's hash-chained audit trail. On the
device, `boswas-device commands` and the Compatibility Manager's activity
list show the remote commands that were run.

## Explicit non-goals

Boswas OS does **not** and will not implement:

- keystroke logging
- screen capture or screen recording by the system, by administrators or
  by the Control Plane (the user's own screenshots, and capture by a
  program the user allows each time, are functions of the desktop: see
  [The desktop](#the-desktop-what-stays-in-the-users-home))
- silent inspection of personal files or browsing
- logging of the commands users type (the audit rules exclude `execve` on purpose)

## Rules for any future monitoring

Any future collection must be:

1. documented in this file **before** it ships;
2. controlled by Boswas policy (visible in `boswas policy status`);
3. limited to the stated purpose and minimal fields, with defined retention;
4. readable only by roles with a need, with every administrator access
   itself recorded in the audit trail.

## Local logs

- **Journal:** persistent, capped at 1 GB and 90 days.
- **Audit log:** `/var/log/audit`, root-only, rotated by auditd.
- **Firewall:** dropped inbound connection attempts are logged rate-limited,
  as packet headers only (no payload).
