# The Boswas OS experience

Boswas OS presents one product identity, from the boot menu to the
desktop. From the boot screen on, what the user sees is drawn by Boswas
components: the boot screen, the login screen, the desktop shell, the
window frames and the lock screen. They share one design system, **Boswas
UI** ([desktop/ui](../../desktop/ui/README.md)): the Fluent design
language (the layout, geometry, materials and motion that Windows 11
users know) with Boswas's own colours (the Prism palette), icons (Boswas
Prism) and typeface (Inter). The official mark keeps its gold and silver
and is the Start button; brand lettering stays in Orbitron (ADR-0033).

Design rule: **Boswas owns the desktop layer, Debian stays the system
layer**.

- The desktop is the package `boswas-desktop`: the compositor
  `boswas-compositor` (in place of KWin) and the shell `boswas-shell` (in
  place of Plasma's shell, screen locker and the SDDM login screen). The
  boot screen is `boswas-bootsplash` (in place of Plymouth). KDE Plasma,
  KWin, SDDM and Plymouth are not installed.
- The system applications are Boswas's own too (`boswas-apps`,
  ADR-0040): Files, Terminal, Text Editor, Calculator, Photos, Document
  Viewer, Archive Manager, Media Player, Camera, Clock, Calendar, Snipping
  Tool and Weather (ADR-0046). They,
  Control Center and the Compatibility Manager take their look from the
  Boswas Qt toolkit (ADR-0039). KDE's applications are no longer in the
  image.
- Other applications are unchanged. Firefox ESR and any Wayland
  application run as ordinary clients; a KDE application someone installs
  takes the Boswas Prism colours and icons from system-wide defaults.
  Applications that only speak X11 run through Xwayland (ADR-0034).
- Package management, the kernel, the boot chain (shim, GRUB, Secure Boot)
  and Debian's archive stay unmodified.
- What could not be changed safely is listed in
  [debian-references.md](debian-references.md).

**Status.** The native desktop is new. It is tested in the development
sandbox: unit tests, end-to-end tests of the compositor with real clients
on its headless back end, and tests of the service backends against
stand-ins of the system daemons. It has not yet run on real hardware or
against the real system services, and the image build and the boot test
with it are still to be done
([what is verified](../architecture/verification.md)).
[Known gaps](#known-gaps) lists what it does not do.

The system applications and the settings are described under
[Boswas applications](#boswas-applications) and [Settings](#settings).
They have their tests and have not run on the desktop yet.

## Identity

| Surface | What users see |
|---------|----------------|
| Boot menu (USB/DVD) | Boswas OS mark, Orbitron wordmark, "Boswas OS v3 Alpha - Live session" (default), "Install Boswas OS" |
| Boot, shutdown, restart, disk unlock | The Boswas boot screen: the mark and a ring of dots on a dark background, with no name written; for encrypted disks "Unlock your disk", the disk's name and a text box with one dot per character, a Caps Lock hint and a line for a rejected passphrase. Shutdown and restart show the ring and one line of text |
| Login screen | The Boswas login screen: first the clock over the background, then, after a key or a click, the people who can sign in and a password field. Another user name can be typed. A power button offers sleep, restart and shut down (the lock screen has none). If graphics cannot start, a text login takes its place |
| Desktop | The background (or the preset's wallpaper), the taskbar with the Boswas mark as the Start button, Boswas Prism icons |
| Lock screen | First the clock, then, after a key or a click, the user's name and a password field; applications are neither shown nor given input, and no notifications appear |
| Control Center > About Boswas OS, `hostnamectl`, console | "Boswas OS v3 Alpha" (`/etc/os-release`: `NAME="Boswas OS"`, `ID=boswas`, `ID_LIKE=debian`); the desktop is shown as "Boswas Desktop" with its version |
| Terminal | "Welcome to Boswas OS" summary and the Boswas prompt |
| Live session | User "Boswas OS Live"; "Live session" in Control Center and the terminal |

## Live USB: use first, install only by choice

Write the ISO to a USB stick and boot from it. The default boot entry
starts the **live session**:

```
Power on -> Boswas boot menu -> Boswas boot splash -> Boswas OS desktop (signed in as "Boswas OS Live")
```

- **Nothing installs on its own.** No installer, installation wizard or
  web page opens. The desktop is usable at once: applications, settings,
  the terminal, the Compatibility Manager and Control Center.
- **Internal disks are not touched.** The live session does not mount,
  partition, format or write internal disks, and changes no EFI boot
  entries. The boot test verifies this bit for bit against an internal
  disk.
- **Live changes are temporary.** Files and settings live in RAM and are
  lost at shutdown. Boswas OS v1 does not offer persistent live storage:
  it would add a writable partition to the stick and change what the
  live system is.
- **Installing is a deliberate choice:**
  - from the desktop: Applications > **Install Boswas OS** (or Control
    Center > Install Boswas OS) explains the steps and offers
    **Restart to Install…**;
  - in the boot menu: choose **Install Boswas OS**.

  The installer runs as its own boot mode, because it applies the Boswas
  installation policy (full-disk encryption, administrator account, root
  locked). It writes to a disk only after its own confirmation steps.
- **Validation:**
  - automated: `tests/boot/qemu_boot_test.py`, scenario `usb` (USB stick,
    UEFI with Secure Boot, internal disk checksum) and scenario `install`
    (the installed system boots through Secure Boot);
  - manual, on real hardware: [live-usb-checklist.md](live-usb-checklist.md).

## Desktop shell

`boswas-shell` draws everything that is not an application window. The
compositor starts it over a trusted connection and restarts it if it
exits; open windows are not affected. `boswas-shell(1)` is the reference.

- **Background:** drawn in code from the palette, exact at every
  resolution. A preset replaces it with the preset's wallpaper. A
  right-click on it opens a short menu.
- **Taskbar:** 48 px along the bottom edge of each screen.
  - In the centre, as one group: Start (the Boswas mark), Task View, and
    the pinned and running applications. A click on an application shows
    its window, starts it, or goes through its windows; the secondary
    button opens its menu (new window, pin or unpin, the application's
    own actions, close); the middle button starts another instance.
    Resting the pointer on a running application shows its windows. A
    mark under the icon shows running and active applications.
  - At the right: tray icons, the keyboard layout when there is more than
    one, the network, volume and battery icons (one button, which opens
    quick settings), and the clock (which opens notifications and the
    calendar).
  - While a program may capture the screen, a red mark sits in front of
    the tray; a click on it stops the capture.
- **Start:** opens with the Super key (tap it) or its button.
  - Search finds applications, pages of Boswas Control Center, and runs a
    command. It ranks applications by how often they were started and is
    local: nothing is sent anywhere.
  - Below it: the pinned applications, all applications by letter
    (Windows applications included, as the menu entries `boswas-winapp`
    creates), and the applications used most.
  - The user menu and the power menu. Signing out, restarting and
    shutting down ask first when windows are open.
- **Quick settings** (Super+A): Wi-Fi with the list of networks,
  Bluetooth with the list of devices, airplane mode, night light, the
  power mode, do not disturb, media controls, brightness, and volume with
  the choice of the sound output. A part whose system service is not
  running shows as not available.
- **Notifications** (Super+N): up to three at a time above the taskbar,
  and the notification centre with a calendar. While do not disturb is on
  only critical notifications pop up; nothing pops up while the session
  is locked. Notifications are kept in memory until they are dismissed
  or the shell restarts; they are not written to disk.
- **The volume and brightness keys** show a small display of the level,
  on the lock screen as well.
- **Run** (Super+R) starts a program with its arguments. There is no
  shell behind it and nothing is expanded.
- **Screen capture** (Print for the screen, Shift+Print or Super+Shift+S
  for a region): the capture screen shows the picture, the user chooses
  what to keep and confirms. The result is saved to the Screenshots
  folder under Pictures and copied to the clipboard.
- **Passwords for administrator actions:** when polkit needs a password
  (Control Center installing an update, for example) the shell shows the
  dialog, with a choice of administrator when several may authorise the
  action.
- **"Allow screen capture?":** when another program (a screenshot tool, a
  recorder, screen sharing in a browser) asks for pictures of the screen,
  the shell asks first. Neither answer is the default, and Enter alone
  allows nothing ([Screen capture by programs](#screen-capture-by-programs)).
- **Keyboard:** every flyout and dialog can be used without a pointer:
  Tab and Shift+Tab go through the controls, the arrow keys move to the
  nearest control in that direction, Enter or Space activates it, Escape
  closes.
- **Presets:** ten Boswas OS presets
  ([presets](../../desktop/presets/README.md)), chosen in Control Center >
  Personalization or with `boswas-preset`. A preset sets light or dark
  for the shell, the window frames and the Boswas applications, and the
  wallpaper; for KDE applications, where they are installed, also their
  colours. Nothing else: the Plasma themes, panel layouts and Konsole
  profiles the presets once carried are gone (ADR-0042). A new user
  starts with the Boswas Prism look, which is not one of the ten;
  `boswas-preset reset`, or "Reset to Boswas Default" in Control Center,
  returns to it.
- **Icons:** the Boswas Prism icon pack
  ([icons](../../desktop/icons/README.md)): applications, places, devices,
  file types, and the symbolic status and action icons. It draws every
  icon the Boswas applications and Settings ask for; what other programs
  ask for and it does not draw comes from Breeze's icon theme.
- **Pointer:** Boswas-Cursors
  ([cursors](../../desktop/cursors/README.md)), Boswas's own pointer
  theme: a white arrow with a dark outline, the same family for every
  shape, and a turning ring while a program is busy. It is the default of
  the compositor, of the login screen and of every program (ADR-0043).

The pins are kept in `~/.config/boswas/shell.toml`; how often each
application was started and the last commands typed into Run are in
`~/.local/state/boswas/`. None of it leaves the device.

## Windows and shortcuts

The compositor draws the window frames: a 32 px title bar with the
application's icon and title and the minimise, maximise and close
buttons, a thin border, rounded corners and a shadow. Applications that
draw their own frame (GTK applications, Firefox) keep it. Windows open,
close, minimise and come back with short animations, and desktops slide
when switched; `[appearance] reduced_motion` turns that off. X11
applications get the same frame and behave like every other window.
`boswas-compositor(1)` is the reference.

| Keys | Action |
|------|--------|
| Super (tap) | Open or close Start |
| Super+A | Quick settings |
| Super+N | Notifications and calendar |
| Super+R | Run |
| Super+E, Super+I, Ctrl+Alt+T, Ctrl+Shift+Esc | Files, Settings (Control Center), Terminal and Task Manager (the programs named in `[apps]`) |
| Super+L | Lock the session |
| Super+1 ... Super+9 | Start or switch to the taskbar application at that place; with Shift, another window of it |
| Alt+Tab, Alt+Shift+Tab | Pick a window, most recently used first; live previews, Escape cancels |
| Super+Tab | Task View: every window and the desktops |
| Alt+F4 | Close the window |
| Alt+Space | The window menu: Restore, Move, Size, Minimize, Maximize, Close |
| F11, Super+F11 | Full screen |
| Super+Up | Maximise; a snapped half goes to its upper quarter |
| Super+Down | Restore; a snapped half goes to its lower quarter; minimise a restored window |
| Super+Left, Super+Right | Snap to a half; at the edge, on to the next monitor |
| Super+Shift+Left, Super+Shift+Right | Move the window to the next monitor |
| Super+D | Show the desktop, and bring the windows back |
| Super+M, Super+Shift+M | Minimise every window; restore them |
| Super+Ctrl+D | New desktop |
| Super+Ctrl+Left, Super+Ctrl+Right | Previous or next desktop |
| Super+Ctrl+F4 | Close the desktop (its windows move to the neighbour) |
| Super+Space | Next keyboard layout |
| Print; Shift+Print, Super+Shift+S | Capture the screen; capture a region |
| Volume, brightness and media keys | Handled by the shell, also on the lock screen |
| Ctrl+Alt+F1 ... F12 | Switch virtual terminal |

With the pointer:

- drag a title bar to move a window; drag it to the top edge to maximise,
  to a side for that half, to the outer quarter of a side for that corner.
  A preview shows where the window will go;
- rest the pointer on the maximise button for the snap layouts (two
  halves, a half beside two quarters, four quarters) and click a zone;
- double-click a title bar to maximise; right-click it for the window
  menu, which also sends the window to another monitor or desktop; drag a
  border or a corner to resize;
- Super+left drag moves and Super+right drag resizes from anywhere on a
  window.

After Move or Size from the window menu the arrow keys move or resize the
window (with Ctrl by single pixels) until Enter or Escape.

On a touch screen a finger does what the pointer does on title bars,
buttons, the window picker and Task View. On a touchpad, three or four
fingers sideways go to the next or previous desktop, up opens Task View
and down shows the desktop. Drawing tablets and pens are supported with
pressure, tilt and their buttons. None of this has been tried on real
devices yet.

Shortcuts use the key's position on the Latin layout, so they work the
same while another keyboard layout is active. Super+L and the virtual
terminal keys work even when an application (a remote desktop, a virtual
machine) has taken the other shortcuts. The shortcuts are fixed in this
version; there is no shortcut editor.

Desktops are created and closed as needed: a session starts with one.
Task View shows them in a strip under the windows.

## Screen capture by programs

An application cannot read the screen on its own. When a program asks the
compositor for pictures of it, the shell asks "Allow screen capture? NAME
wants to see your screen." (ADR-0037):

- "Allow" covers that program while it keeps capturing (a recording, a
  shared screen) and ends a few seconds after its last picture. The next
  request asks again.
- "Don't allow" holds for twenty seconds, so a program that retries does
  not ask again and again.
- While any program may capture, a red mark on the taskbar says which. A
  click on it stops them all.
- Nothing is captured while the session is locked.
- Sandboxed applications (Flatpak) are not offered this at all; they go
  through the desktop portal, which is the program the question then
  names.

The name in the question is the name the program runs under, and a
program can choose it. An allowed program sees everything on every
screen until it stops. Screen sharing from browsers and meetings goes
through `xdg-desktop-portal-wlr`, which shares the first monitor; this
path has not been tested yet.

## Lock, login and boot

- **Lock screen.** The compositor owns the lock. Super+L, Lock in Start's
  user menu, ten minutes without input, suspend and hibernate lock the
  session: every screen is blanked and input is taken away from
  applications first, then the lock screen is started. It shows the
  clock and, after a key or a click, asks for the password, which it
  checks through PAM. If the lock screen crashes, the screens stay blank
  and it is started again; nothing but a correct password unlocks. A
  video player or presentation that asks the compositor to stay awake
  holds the idle lock off while it runs. The lock screen shows no
  notifications and offers no way to restart or shut down; the volume and
  brightness keys work.
  - An administrator sets the timeout in `/etc/boswas/compositor.toml`. A
    user may shorten it, not lengthen or disable it.
  - `loginctl lock-session` locks the session while the shell runs;
    scripts can also use `boswas-compositor --send lock`.
  - The screens turn off after five minutes without input (`[power]
    screen_off_seconds`) and back on with any input. That is separate
    from the lock.
- **Login screen.** greetd runs the Boswas login screen on the first
  virtual terminal: the clock, then the people who can sign in (regular
  accounts with a login shell) and the password. Another user name can be
  typed. After a successful login it starts `boswas-session`. The live
  session signs in automatically.
- **When the desktop cannot start.** A boot always ends at a way to sign
  in (ADR-0044). If the login screen cannot be shown (no usable graphics
  device, a login screen that keeps failing), greetd's text login appears
  with a line that says so; signing in there opens a shell with a note on
  how to start the desktop (`boswas-session`) and where its messages are
  (`journalctl -b -t boswas-session`; the login screen's are under
  `boswas-greeter`). If greetd itself fails, the ordinary text
  login of the first console is started.
- **Boot screen.** `boswas-bootsplash` shows the mark and a ring of dots
  from the initramfs to the login screen, the passphrase prompt of an
  encrypted disk, and the shutdown and restart screens. Escape switches
  between the boot screen and the text console. `nosplash` on the kernel
  command line turns it off, `boswas.splash=static` keeps it without
  motion
  ([packages/boswas-bootsplash](../../packages/boswas-bootsplash/README.md)).

## Settings

Settings live in **Boswas Control Center** (Super+I): each page holds its
settings as sections with switches, lists and sliders that apply at once
(ADR-0041, [control-center.md](control-center.md)). The search box above
the page list finds a setting and jumps to it.

| What | Where |
|------|-------|
| Wi-Fi (networks, passphrase, forget), Ethernet and VPN status, airplane mode, connection details | Control Center > Network; the common switches also in quick settings (Super+A) |
| Bluetooth devices (add, pair, connect, remove, battery) | Control Center > Bluetooth; quick settings |
| Sound output and input, volume, mute | Control Center > Sound; quick settings. Per-application volumes: `pavucontrol-qt` |
| Battery, power mode, brightness, the time until the screens turn off | Control Center > Power; quick settings |
| Monitors: arrangement by dragging, resolution, refresh rate, scale, orientation, on or off | Control Center > Display. A change that could leave you without a picture asks "Keep these display settings?" and goes back after fifteen seconds |
| Night light: switch, strength, schedule | Control Center > Display; quick settings |
| Mouse, touchpad, keyboard layouts and repeat | Control Center > Devices |
| Dark or light, background, motion | Control Center > Personalization. A preset (ten of them) sets light or dark, the wallpaper and the colours of KDE applications; `boswas-preset apply <id>` does the same |
| Lock timeout | Control Center > Security. It ends at the limit the organisation set, which the page shows |
| Date and time, time zone, region and language | Control Center > System |
| Default applications, start-up applications | Control Center > Applications |
| Your account (picture, name, password), other users, adding and removing users | Control Center > Users. Changes to other accounts ask for an administrator's password |
| Software, updates, security, Windows and Android applications | Control Center |

Display, input, appearance and the timeouts are the compositor's
settings: Control Center writes the user's file,
`~/.config/boswas/compositor.toml` (and the colour mode and background to
`appearance.toml`), and asks the compositor to load it again. The file can
also be edited by hand; `boswas-compositor(1)` lists every key,
`boswas-compositor --send reload-config` applies it and `--send outputs`
lists the monitors with their names and modes. System-wide values and the
policy are in `/etc/boswas/compositor.toml`, which Settings never writes.

Example, a user's `~/.config/boswas/compositor.toml`:

```toml
[keyboard]
layout = "us,in"

[touchpad]
natural_scroll = true

[outputs.HDMI-A-1]
scale = 1.5
position = [1920, 0]

[lock]
idle_seconds = 300
```

Super+Space goes to the next keyboard layout. A file that does not
parse, or that contains an unknown key, is reported in the log and
ignored as a whole; the other files stay in use. When Control Center
changes a setting it rewrites the file and keeps every documented key.
What it cannot keep (comments, a key the compositor does not know) is not
dropped silently: the previous file is saved as `compositor.toml.bak`
first, and Control Center says so.

Not in Settings yet: editing a proxy, managing VPN connections, drawing
tablets, an automatic suspend time.

## Known gaps

The native desktop does not have, or has not shown yet:

- **A run on real hardware or against the real system services.** See
  [what is verified](../architecture/verification.md). In particular the
  password dialog for administrator actions has only run against a
  stand-in for polkit.
- **Live blur.** The taskbar and the flyouts are drawn on a blurred copy
  of the wallpaper; a window behind them does not show through.
- **Sharp X11 windows on scaled monitors.** X11 applications are drawn at
  scale 1 and enlarged. They can also observe each other, as on any X
  server; they cannot observe Wayland applications.
- **Tested screen sharing.** The portal for it is installed and
  configured, shares the first monitor, and has not been run.
- **Bluetooth devices that need a PIN or passkey,** and **enterprise
  Wi-Fi (802.1X):** neither can be set up from quick settings or from
  Control Center.
- **An office suite, a mail client and a print service** (Print saves a
  PDF file).
- **What the system applications leave out** next to the KDE applications
  they replace, for example syntax highlighting, PDF annotations and
  split terminal panes (ADR-0040 has the list).
- **An input method in the image** (for example for Indic, Chinese,
  Japanese or Korean text entry) and an on-screen keyboard. The
  compositor supports an input method that the administrator configures;
  none has been run.
- **More than one GPU**, HDR and variable refresh rate.
- **A screen reader for the shell.** See [Accessibility](#accessibility).
- **Per-preset accents in the shell.** The taskbar, Start and the lock
  screen follow a preset's light or dark variant, not its accent colour.
- **A shortcut editor,** desktop icons, and a taskbar at another edge.

## Boswas applications

The system applications are one package, `boswas-apps` (ADR-0040). Each
follows its Windows 11 counterpart in layout and shortcuts, in dark and
light, and each has a manual page (`man boswas-files`, and so on).

| Application | What it is |
|-------------|------------|
| **Files** | The file manager: tabs, an address bar that turns into a path box (Ctrl+L), a navigation pane with Home, your folders, pinned folders, the volumes of the computer and the Trash; Details, List, Tiles and Icons layouts with picture thumbnails. Copying, moving and deleting run in the background with progress and a cancel button, ask when a name is taken, and can be undone. Delete moves to the Trash. A selection can be compressed to a ZIP archive, and ZIP and TAR archives extracted. A program or script is never started by opening it without a question |
| **Terminal** | The command line with tabs and profiles (Ctrl+Shift+T, Ctrl+Tab), scrollback with search (Ctrl+Shift+F), copy and paste (Ctrl+Shift+C and V), links that open with Ctrl+click, and colour schemes that follow the desktop. Editors, pagers and terminal multiplexers work as in an xterm |
| **Text Editor** | Plain text with tabs: the encoding and the line endings of a file are kept, find and replace with regular expressions, go to line, word wrap, zoom, printing. It restores the last session, unsaved text included |
| **Calculator** | Standard, scientific and programmer modes, date calculation and twelve unit converters, with memory and history. Arithmetic is exact: 0.1 + 0.2 is 0.3 |
| **Photos** | A viewer (zoom, filmstrip, rotate, delete with undo, print, set as background), a gallery by month, a slideshow, and an editor: crop, straighten, ten adjustments, looks, pen and highlighter. "Save as copy" is the default |
| **Document Viewer** | A PDF reader: tabs, thumbnails and outline, search, text selection and copy, continuous or page-wise view, presentation mode, printing |
| **Archive Manager** | Opens an archive like a folder; extracts, adds, deletes, tests, creates. ZIP and TAR (also compressed); 7z and RAR when the `7zip` package is installed |
| **Media Player** | Music and video: a library from the tags of your files, a play queue, playlists, a mini player, subtitle and audio tracks, and an MPRIS interface for the media keys and the shell's media controls |
| **Camera** | Photos and videos from a camera of the computer, with a timer, to Pictures/Camera Roll and Videos/Camera Roll |
| **Clock** | Alarms, timers, a stopwatch, a world clock and focus sessions |
| **Calendar** | A calendar kept on this computer: month, week and day views, repeating events, reminders, several calendars, import and export of `.ics` files. It has no accounts and does not synchronise |
| **Snipping Tool** | Screenshots of an area or the whole screen, now or after a delay, through the desktop's capture screen (the same one Print opens). The picture opens in an editor with a pen, a highlighter, arrows, rectangles, an eraser and crop, with undo; copy it, save it as PNG or JPEG or open it in Photos. It also marks up pictures you open |
| **Weather** | The places you add (it never uses your location): current conditions, the next 24 hours, details (wind, humidity, pressure, UV index, visibility, precipitation, sunrise and sunset) and 10 days, in the units you choose. It refreshes while open and shows the last forecast when there is no network. Data from Open-Meteo |
| **Task Manager** | Ctrl+Shift+Esc. What is running, by application, with CPU, memory and disk use, and "End task"; performance graphs for the processor, memory, disks, network, graphics and battery; start-up applications (switch them off or on); the signed-in users; every process in detail; services |
| **Boswas Control Center** | Settings (see [Settings](#settings)) and the three centres below. It is the application Super+I opens ([control-center.md](control-center.md)) |
| **Boswas Security Center** | Control Center's Security page: AppArmor, Windows-app sandbox, firewall, Secure Boot, disk encryption, screen lock, automatic security updates, device agent, device policy. Each item is Secure, Warning, Attention, Error or Not available, shown with an icon and text, never colour alone |
| **Boswas Software Center** | Control Center's Applications page: Boswas, Windows and native applications, searchable. Installing new native software comes with the Boswas Store (M6); there is no unmanaged package installer |
| **Boswas Update Center** | Control Center's Updates page: automatic security updates (read-only) |
| **Boswas Compatibility Manager** | Windows applications (64-bit and 32-bit) in isolated sandboxes. In Files, a Windows program or installer package has "Install with Boswas Compatibility Manager" under "Open with"; opening the file still means "Run with Boswas" |
| **About Boswas OS** | Version, architecture, kernel, CPU, memory, storage, desktop (Boswas Desktop and its version), security, Windows compatibility, device agent, live or installed. Technical details include the package base |

Alarms, timers and calendar reminders are shown as notifications even
while Clock and Calendar are closed: a small service, `boswas-reminders`,
starts with the session for that.

All of them run as you and refuse to run as administrator. What they
leave out next to the KDE applications they replace, and what has not
been tried yet (none has run on the desktop; the Camera has never seen a
camera), is in ADR-0040 and in
[what is verified](../architecture/verification.md).

## Terminal

Boswas Terminal starts bash with `/usr/share/boswas/terminal/bashrc`.

- **Your settings stay:** it runs your `~/.bashrc` first.
- **Welcome:** `boswas-welcome` shows the version, kernel, architecture,
  session (live or installed), AppArmor state and device agent state.
- **Prompt:** user and host in gold, the directory in silver, and a failed
  command's exit code in brackets.
- **Quiet option:** `touch ~/.config/boswas/no-welcome` turns the welcome
  off.

## Accessibility

- **Applications:** keyboard navigation and screen-reader support inside
  applications come from Qt and GTK, as before.
  - Boswas applications set accessible names.
  - They support full keyboard use (sidebar arrows, Tab order, Ctrl+F, F5).
  - They show status as icon plus text.
- **The shell:** every flyout and dialog, the lock screen and the login
  screen can be used from the keyboard, and so can the window picker,
  Task View and the window menu of the compositor. None of them exposes
  an accessibility tree yet, so a screen reader cannot read them; no
  screen reader is in the image.
- **High contrast:** Boswas Carbon is the high-contrast preset for
  applications (WCAG AAA body text). The shell uses the Prism dark or light
  theme, whose text meets WCAG AA.
- **Contrast:** the Prism colour schemes and every preset keep body text at
  4.5:1 or more; the tests check it.
- **Scaling:** 1.0 to 2.0 in steps of 0.25, for all screens (`[output]
  scale`) or per monitor (`[outputs.NAME] scale`).
- **Motion:** `[appearance] reduced_motion` turns the animations of
  windows, desktops and the shell off; the Boswas Qt applications follow
  the same setting. The boot screen's animation can be turned off with
  `boswas.splash=static` on the kernel command line.
- **Colour temperature:** night light, in quick settings or
  `[night_light]`.

## Performance

No performance figures have been measured on real hardware yet. The design
choices that matter:

- **The compositor draws only when something changed** and only the
  damaged regions; an idle desktop draws nothing.
- **The shell is one process** for the background, the taskbar and every
  flyout. It redraws a surface only when what it shows changes. The
  default background is rendered once per screen size, and the blurred
  copy of the wallpaper behind the taskbar and the flyouts once per
  wallpaper and blur radius.
- **The session's services are threads of the shell,** one per service,
  not processes of their own (ADR-0035). Each reads the system at most
  about five times a second, however many change signals arrive.
- **Control Center, the presets tool and the welcome banner run only when
  used;** the welcome reads files only, so terminals open instantly.
- **Boot screen:** nothing is parsed at boot (artwork and glyphs are
  embedded at build time), and a frame repaints only the items that
  changed.
- The device agent and session agent are the existing device-management
  services, unchanged.
