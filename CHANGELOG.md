# Changelog

All notable changes to Boswas OS. Versions follow `config/boswas/release.conf`.

## [3.0~alpha1] - 2026-11-16 (Boswas OS v3 Alpha: native Boswas desktop in the Fluent design language, Boswas system applications and settings; cross-platform applications, installer firmware, functional Control Center)

Baseline audit: [docs/reports/2026-11-12-baseline-audit.md](docs/reports/2026-11-12-baseline-audit.md).
Release report: [docs/reports/2026-11-12-release-readiness.md](docs/reports/2026-11-12-release-readiness.md).
Both reports describe the KDE Plasma image of 2026-11-12, before the native
desktop.

**Native Boswas desktop (2026-11-15 and 2026-11-16).** KDE Plasma, KWin,
SDDM and Plymouth are replaced by Boswas components written in Rust: a
Wayland compositor, a shell with lock and login screens, and a boot
screen. A second round gave that desktop the Fluent design language (the
look Windows 11 users know, with Boswas's own colours, icons and
typeface), the functions a desktop needs around its windows
(notifications, quick settings, a tray, screen capture, X11
applications), Boswas's own system applications in place of KDE's, and
native settings. The Wayland protocol stays (applications speak it).
Entries marked *(native desktop)* below belong to this change; they
describe the desktop as it is now, not the first version of 2026-11-15.

Verification status of the native desktop
([docs/architecture/verification.md](docs/architecture/verification.md)):

- **Verified in the development sandbox:** the Rust unit tests; the
  compositor end-to-end tests on the headless backend with real clients;
  the service backends against stand-ins of the system daemons; the Qt
  toolkit, the system applications and the settings on Qt's offscreen
  platform.
- **Booted in a virtual machine** (the built packages on another
  distribution's base system, software rendering; not the image): the boot
  screen, the login screen, a sign-in to the desktop, the live session's
  automatic sign-in, and every text-login fallback of ADR-0044.
- **Not yet verified:** the ISO build with the new manifest; the QEMU boot
  test; anything on Debian 13 itself; the hardware path (DRM/KMS on real
  graphics hardware, libinput, libseat); the greetd login on the image;
  the boot screen on real hardware and its disk-unlock prompt;
  every service backend against the real daemon (NetworkManager, BlueZ,
  PipeWire, UPower, polkit); the applications on the running desktop and
  with real devices; screen sharing through the portal; dependency
  resolution of the new image manifest on Debian trixie.

### Fixed
- **A laggy desktop** *(native desktop)*: moving windows, opening Start
  and Task View were slow, worst in virtual machines (ADR-0045).
  - Without a hardware GPU the compositor drew with Mesa's software OpenGL;
    in a QEMU virtual machine a window could be dragged at one or two
    frames per second. It now draws with pixman there (57 frames per
    second for the same drag), and wherever OpenGL ES cannot start.
    Windows have square corners in that mode. `BOSWAS_RENDERER` chooses
    the renderer.
  - On virtual graphics adapters and the firmware frame buffer, which
    have no vertical blank, the compositor drew as fast as the processor
    allowed (up to 198 frames per second). Frames are paced to the
    display's refresh rate.
  - The shell sent its surfaces whole on every change: moving the pointer
    between a window and the desktop redrew the whole screen. It now
    sends only what changed, nothing when nothing visible changed, and
    declares its opaque surfaces; title bars declare theirs.
  - Start, quick settings and the notification centre redrew their
    shadow as 24 stacked rectangles and their translucent background
    from the blurred wallpaper on every hover change; both are drawn once
    and kept. Opening and closing Start costs a third of the CPU time it
    did.
  - `BOSWAS_LOG=info,boswas_compositor::stats=debug` makes the compositor
    report per output, once a second, the frames it drew, what they cost
    and how much of the screen they drew again.
- **A boot that stopped at a blinking cursor** *(native desktop)*: the
  live medium did not reach the desktop; the screen stayed black with a
  text cursor and there was no way to sign in. The live session's
  automatic sign-in named a user that does not exist (live-config's own
  default, `user`, instead of the live user `boswas`), greetd refused it
  five times and stopped, and nothing else offered a login on the first
  console. The live user's name is now set in one place and read the way
  live-config reads it (also `username=` on the kernel command line), and
  an automatic sign-in is written only for a user that exists (ADR-0044).
- **A boot always ends at a way to sign in** *(native desktop)*: looking
  for the cause by booting the packages in a virtual machine showed more
  ways to the same black screen, each corrected (ADR-0044):
  - If greetd fails for good, the text login (`getty`) is started on the
    first console (`boswas-login-fallback.service`).
  - If the login screen cannot start, the text login of greetd is shown.
    It used to start the same desktop that had just failed; it now opens
    a shell (`boswas-text-session`) with a note on how to start the
    desktop and where the log is. The compositor says by its exit status
    whether trying again is pointless (3: no seat, no graphics device or
    no output; 4: the login screen kept failing).
  - Without polkit the compositor could not open the graphics device (the
    login service refuses to activate the session), and without the font
    configuration or a font the shell ended at once, again and again.
    `polkitd`, `fontconfig-config`, `fonts-inter` and `libpam-systemd` are
    dependencies of `boswas-desktop` now, not recommendations. The shell
    also finds fonts without the font configuration and ends with one
    clear line when there is none; the compositor names the causes when it
    gets no seat, and the log has what libseat reported.
  - The compositor waits for a graphics device that appears a moment
    later, and moves to the native driver's device when that replaces
    the firmware frame buffer under it. Windows stay.
  - On a firmware frame buffer (software rendering, "safe graphics")
    parts of the first pictures were missing; frames are completed before
    they are shown.
  - After a login on the login screen its compositor did not end until
    greetd stopped it (about five seconds); it ends at once.
  - A desktop shell that keeps failing is started again with a growing
    pause instead of in a tight loop; the login screen is given up after
    three failed starts and the text login is shown.
  - A syntax error in `/etc/profile` or `~/.profile` no longer ends the
    session before it starts.
  - The compositor's messages appeared as text on the console between the
    boot screen and the login screen and were kept in no log. They go to
    the journal (`journalctl -b -t boswas-session`, `-t boswas-greeter`).
  - The boot screen showed only its moving ring after the display's
    driver had replaced the firmware frame buffer; it draws the whole
    picture again. It can follow a graphics device that appears after it
    has entered its sandbox (its supervisor opens the device for it),
    there is only ever one boot screen, one that does not answer is
    terminated at the hand-over, the Escape key keeps working after the
    switch to the real root, and emergency and rescue mode end it before
    their shell starts. The initramfs brings the graphics drivers itself
    on Debian.
  - The boot menu of the live medium sets a graphics mode for the kernel
    on BIOS machines, and installed systems keep the boot loader's mode,
    so the boot screen has a frame buffer to draw on before the graphics
    driver is loaded.
- **Android image download** (from the unmerged branch
  `feature/m2-device-management`): a downloaded image whose checksum does
  not match is reported as a network error that can be tried again, and
  the boot test tries the download three times.
- **Calendar in a narrow window** *(native desktop)*: snapped to half of
  the screen, the toolbar was drawn over itself (the month cut off, the
  search box on top of the view switch). Below 900 pixels the pane with
  the month and the calendars now folds away behind a button, a "new
  event" button takes its place in the toolbar, and the view switch and
  the search box take a second row when one row cannot hold them. Found
  by looking at every application at half-screen width in a run from the
  built packages.
- **Settings' copy of the compositor's defaults** *(native desktop)* named
  Konsole and Dolphin as terminal and file manager; it names Boswas
  Terminal and Boswas Files, and a test compares it with the file the
  compositor ships.
- **Installer firmware (`rtl_nic/rtl8125b-2.fw` prompt):** the ISO had no
  firmware pool for the installer (`--firmware-binary false`), which runs its
  own kernel and initrd. The medium now carries every Debian firmware package
  under `/firmware` with the installer's index. The installer loads missing
  firmware before any network exists and installs it on the target
  (ADR-0023). `config/boswas/firmware-required.list` is a build gate for both
  the installer medium and the image; `tests/installer/` runs the installer's
  own firmware code against the built medium.
- **Installation without internet access:** the installer no longer uses a
  network mirror or the security/updates services, so an unreachable mirror
  or a NIC without a driver cannot stop an installation. The new
  `/usr/lib/boswas/installer/finish-install` writes signature-verified Debian
  sources (`debian.sources`, deb822) when none are configured, removes the live
  medium's unsigned pool, and writes `/var/log/boswas/install-report.txt`.
- **A damaged settings file is ignored alone** *(native desktop)*: the
  compositor now checks each of its configuration files on its own. A
  file with an unknown key or a value of the wrong kind is reported and
  ignored as a whole, and the other files stay in use. In the first
  version of the native desktop such a file in a user's directory made the
  compositor fall back to its built-in settings for everything, which
  also dropped an administrator's stricter lock policy.

### Added
- **A new Boswas OS logo** (ADR-0047): a lowercase "b" drawn from geometry
  (a pill-shaped stem in front of a round bowl, a warm dot in the counter)
  in the palette's blues and sun, with wordmarks in Inter. It replaces the
  traced gold and silver mark on the boot screen, the boot menu, the
  installer, the Start button, About, the login logo, the wallpapers and
  the dashboard's favicon.
- **A new icon design** (ADR-0047), inspired by Windows 11's icons and
  drawn by Boswas: application, folder, device and file icons are objects
  with their own shape, soft top-lit gradients and a contact shadow,
  without tiles; line icons are thinner and rounder. Every icon keeps its
  name, so applications pick the new design up unchanged.
- **Snipping Tool** (`boswas-snipping-tool`, in `boswas-apps`; ADR-0046):
  screenshots of an area or the whole screen, now or after 3, 5 or 10
  seconds, taken through the desktop's capture screen (the user drags or
  confirms there, as with Print). The snip opens in an editor: pen,
  highlighter, arrows, rectangles, text, an eraser and crop in eight
  colours and three widths, with undo for every step, zoom and pan; copy,
  save as PNG or JPEG, open in Photos. Pictures can also be opened or
  pasted for markup. Pinned to Start for new users.
- **Weather** (`boswas-weather`, in `boswas-apps`; ADR-0046): forecasts
  for the places the user adds (it never locates the device), from
  Open-Meteo: current conditions, the next 24 hours, details (wind,
  humidity, pressure, UV index, visibility, precipitation, sunrise and
  sunset, cloud cover) and 10 days, in the units chosen; the last forecast
  is shown when there is no network. What it sends, and to whom, is in
  `docs/security/privacy.md`. Pinned to Start for new users.
- **Icons** for both, for the weather conditions and details, and for the
  drawing tools, in the Boswas Prism pack.
- **`boswas-desktop`** *(native desktop)*: a new package with five Rust
  crates and the session files.
  - **`boswas-compositor`**, the Wayland compositor (on Smithay), in place
    of KWin (ADR-0026, ADR-0036).
    - **Windows:** Fluent window frames (a title bar with the
      application's icon, minimise, maximise and close buttons, rounded
      corners, a shadow), move and resize, also from the keyboard, a
      window menu (Alt+Space), maximise, full screen, minimise.
    - **Arranging:** snapping to halves and quarters by dragging and with
      Super and the arrow keys, snap layouts from the maximise button, a
      window picker with live previews (Alt+Tab), Task View with desktops
      that are created and closed as needed (Super+Tab), show desktop.
    - **Motion:** windows open, close, minimise and come back with short
      animations and desktops slide; one setting turns it off.
    - **Displays:** resolution, refresh rate, scale, position and rotation
      per monitor, night light (also by schedule), and the screens turn
      off after a time without input.
    - **Input:** touch screens, touchpad gestures (three fingers for
      desktops and Task View), drawing tablets, several keyboard layouts
      (Super+Space), support for an input method that the administrator
      configures.
    - **X11 applications** run through Xwayland and are windows like any
      other (ADR-0034).
    - **Screen capture for other programs,** each time only after the
      user has allowed it (ADR-0037, see Security).
    - Back ends: `udev` (DRM/KMS, libinput, libseat), `winit` (nested, for
      development) and `headless` (software rendering, for tests).
      Configuration in TOML layers (`/usr/share/boswas/compositor/`,
      `/etc/boswas/`, `~/.config/boswas/`); a control socket that offers
      `version`, `lock`, `reload-config`, `outputs` and `capture` to
      everyone and everything else to the shell only.
  - **`boswas-shell`**, in place of plasmashell, the KDE screen locker and
    the SDDM greeter (ADR-0027, ADR-0035).
    - **Taskbar:** Start, Task View and the pinned and running
      applications in the centre, with window previews and menus; the
      tray, the keyboard layout, network, sound and battery, and the
      clock at the right.
    - **Start:** search over applications, settings and commands, pinned
      applications, all applications by letter, the most used ones, the
      user and power menus.
    - **Quick settings** (Super+A): Wi-Fi with the list of networks,
      Bluetooth with the list of devices, airplane mode, night light,
      power mode, do not disturb, media controls, brightness, volume and
      the choice of the sound output.
    - **Notifications** (Super+N): toasts, a notification centre and a
      calendar; do not disturb.
    - A display for the volume and brightness keys, Run (Super+R), screen
      capture (Print), and the password dialog for administrator actions,
      with a choice of administrator.
    - The lock screen (`--lock`, PAM service `boswas-lock`) and the login
      screen (`--greeter`, behind greetd), which lists the people who can
      sign in.
  - **`boswas-services`**: what the shell shows and controls, one thread
    per service: notifications, tray, media players, network, Bluetooth,
    audio, power, brightness, session and the polkit agent (ADR-0035).
  - **`boswas-services-bridge`**: the same network, Bluetooth, audio,
    power and brightness backends for the settings application.
  - **`boswas-ui`**: the drawing, text, icon and material library of the
    shell and the window frames, in safe Rust
    (`#![forbid(unsafe_code)]`).
  - **Session:** `boswas-session` (in place of `startplasma-wayland`),
    `boswas-session.target`, the greetd configuration
    (`/etc/boswas/greetd.toml`, enabled through a systemd drop-in, with a
    text-login fallback), the portal configuration (the GTK back end, and
    the wlroots back end for screen sharing), and a system-sleep hook
    that locks the session before suspend and hibernate.
- **`boswas-bootsplash`** *(native desktop)*: a new package, the Boswas boot
  screen in Rust in place of Plymouth: boot, shutdown, restart and the
  disk-unlock prompt, on DRM dumb buffers or the frame buffer, repainting
  only what changed. It shows the Boswas mark and a ring of dots, and for
  an encrypted disk a text box in the style of the desktop; its text is
  set in Inter (ADR-0038). Its client is installed under Plymouth's name
  inside the initramfs only, so the disk-unlock scripts work unchanged; a
  systemd password agent serves prompts after the initramfs
  ([packages/boswas-bootsplash](packages/boswas-bootsplash/README.md)).
- **Boswas UI v3: the Fluent design language** *(native desktop)*:
  `desktop/ui/tokens.json` is the single source of the look: the layout,
  geometry, materials and motion of Fluent, with Boswas's own colours
  (the Prism hues on neutral greys, iris as the accent), icons and
  typeface (Inter). No font, icon, logo or wallpaper of Microsoft is
  used. The shell, the window frames, the boot screen, the Qt toolkit and
  the icon generator read the file; the KDE colour schemes `BoswasPrism`
  (dark, the default) and `BoswasPrismLight` are generated from it
  (ADR-0033, [desktop/ui](desktop/ui/README.md)).
- **`boswas_fluent`, the Qt toolkit of the Boswas applications** *(native
  desktop)*: a PySide6 library, installed by `boswas-branding`, that gives
  an application the Boswas UI look from the same tokens: a style for the
  standard Qt widgets, navigation view, settings cards, command bar,
  dialogs, flyouts, toggle switches, the keyboard focus ring. It follows
  the desktop's light or dark scheme while running (ADR-0039,
  [desktop/ui/qt](desktop/ui/qt/README.md)).
- **`boswas-apps`, the Boswas system applications** *(native desktop)*: a
  new package with the applications of the desktop, built on the toolkit,
  in place of KDE's (ADR-0040).
  - **Files** (in place of Dolphin): tabs, a navigation pane with the
    volumes and the Trash, four layouts with thumbnails, copy, move and
    delete in the background with progress, conflict questions and undo,
    ZIP and TAR, "Open with".
  - **Terminal** (in place of Konsole): tabs and profiles on its own
    xterm-compatible emulator, scrollback with search, links, colour
    schemes that follow the desktop.
  - **Text Editor** (in place of KWrite): tabs, encodings and line endings
    kept as they were, find and replace with regular expressions, session
    restore, printing.
  - **Calculator** (in place of KCalc): standard, scientific and
    programmer modes, date calculation and unit converters, with exact
    arithmetic.
  - **Photos** (in place of Gwenview): viewer, gallery, slideshow and an
    editor (crop, straighten, adjustments, looks, pen).
  - **Document Viewer** (in place of Okular): a PDF reader with tabs,
    search, outline, text selection and printing.
  - **Archive Manager** (in place of Ark): ZIP and TAR archives, 7z and
    RAR with the `7zip` package; extraction that cannot write outside its
    folder.
  - **Media Player:** music and video, a library from the files' tags, a
    queue, playlists, and the MPRIS interface that the shell's media
    controls use.
  - **Camera:** photos and videos from a camera of the computer.
  - **Clock:** alarms, timers, stopwatch, world clock, focus sessions.
  - **Calendar:** a calendar kept on the computer, with repeating events,
    reminders and iCalendar import and export. A small session service
    shows alarms and reminders while Clock and Calendar are closed.
  - **Task Manager** (Ctrl+Shift+Esc): processes grouped by application
    with "End task", performance graphs, start-up applications, users,
    details and services.
  - All of them run as the user (they refuse root), start other programs
    without a shell, and treat file names and file contents as hostile.
- **Boswas-Cursors pointer theme** *(native desktop, ADR-0043)*: the
  pointers are Boswas's own instead of KDE's Breeze: one family of 28
  drawings (a white arrow with a dark outline), every CSS cursor name, the
  X11 and toolkit names, an animated ring for busy states, sizes 24 to 96.
  It is the compositor's and the login screen's default, the session's
  `XCURSOR_THEME`, and Debian's default cursor theme (`x-cursor-theme`
  alternative) for GTK and X11 programs. Generated from
  `desktop/cursors`, installed by `boswas-branding`.
- **Boswas Prism icon pack** *(native desktop)*: the themes `Boswas-Prism`
  and `Boswas-Prism-Light`, 300 designs in 535 SVG files (applications,
  places, devices, mimetypes, and symbolic status and action icons in a
  light and a dark variant), every colour taken from the tokens. It is the
  default icon theme; the earlier `Boswas` and `Boswas-Light` themes stay
  installed ([desktop/icons](desktop/icons/README.md)).
- **Image** *(native desktop)*: `boswas-desktop`, `boswas-bootsplash`,
  `boswas-apps`, `greetd`, `xwayland`, `xdg-desktop-portal-gtk`,
  `xdg-desktop-portal-wlr`, `pipewire-bin`, `wireplumber`, `upower`,
  `power-profiles-daemon`, `accountsservice`, `qt6-wayland`,
  `breeze-icon-theme`, `pavucontrol-qt` and `fonts-inter`
  (`breeze-cursor-theme` and `kde-style-breeze` were dropped again when
  the pointer theme became Boswas's own, ADR-0043). The live session signs in through
  greetd's initial session.
- **Tests** *(native desktop)*: compositor end-to-end tests
  (`packages/boswas-desktop/crates/boswas-compositor/tests/headless.rs`:
  ordinary clients are offered no privileged protocol, a program the
  compositor starts is, the public control socket is limited, a window is
  mapped, listed, tiled, drawn and closed, the window picker, Task View
  and the window menu work from the keyboard, a program sees the screen
  only after the user allowed it and never while locked, an X11 program
  gets a window, a locked session stays locked when the lock screen dies,
  and only a lock client unlocks it); integration tests of the service
  backends against private D-Bus daemons and scripted stand-ins; the Rust
  suites of both packages, the `desktop/ui` and toolkit tests and one
  suite per application in `tests/unit/test_units.sh`; static checks for
  the Cargo versions, the trust rules, the commands of the public control
  socket, the files allowed to contain `unsafe`, the greetd and boot
  screen wiring and the icon pack; package, image, security and boot
  tests rewritten for the new desktop (the image and boot stages have not
  run yet).
- **Build** *(native desktop)*: the builder container and
  `build/scripts/build-packages.sh` gain the Rust toolchain (Debian's
  rustc and cargo 1.85) and the libraries the desktop links to. Cargo
  downloads the crates pinned in each `Cargo.lock` from crates.io.
  `boswas-bootsplash` needs `fonts-inter` at build time.
- **32-bit Windows applications (ADR-0022, supersedes ADR-0014):**
  `wine32:i386` (i386 is the only foreign architecture; the system stays
  amd64). PE detection cross-checks the machine field with the
  optional-header magic, and refuses damaged headers, DLLs and scripts with
  the reason. The device agent and Control Plane accept x86 entries. The VM
  test runs a real 32-bit program built from source and checks that it ran
  under WoW64, confined.
- **`boswas-sysops`** (ADR-0024): the only privileged helper. It offers fixed
  operations (APT, cleanup, firmware report, Flatpak remotes, Android
  runtime), each its own polkit action, with JSON Lines progress, safe
  cancellation and fixed exit codes.
- **`boswas-android`** (ADR-0025): Android runtime manager (Waydroid, opt-in
  after installation, VANILLA images), APK inspection (manifest, ABIs, split
  packages, signer) and install/launch/remove. Google Play is **not**
  offered. F-Droid is offered with a pinned signer certificate, alongside
  local APK files.
- **Control Center:** functional Update Center, Android page, application hub
  (Debian packages, Flatpak, AppImage, install from file), Hardware & Drivers
  (devices, drivers, missing firmware), Storage cleanup and live Network
  status ([docs/experience/control-center.md](docs/experience/control-center.md)).
- **Firmware in the image:** firmware-cirrus, firmware-bnx2, firmware-bnx2x,
  firmware-libertas, firmware-zd1211; `/usr/share/boswas/firmware/index.tsv`.
- **libfuse2t64** for AppImages built with the older runtime.
- **Tests:** offline installation scenario (`--scenario install-offline`),
  32-bit WinCompat checks in the VM, installer firmware stage, image checks
  for firmware, sources, Android and Google components.
- **Build:** the previous image is kept in `build/previous/<build ID>/`
  instead of being deleted.

### Changed
- **`boswas-branding`** *(native desktop)*: the default look is Boswas
  Prism (colour scheme `BoswasPrism`, icon theme `Boswas-Prism`, Breeze
  widget style), set as defaults for KDE applications in
  `/usr/share/boswas/kde-settings`. It installs the tokens
  (`/usr/share/boswas/ui/tokens.json`), both icon packs and the Qt toolkit
  `boswas_fluent` (and therefore depends on PySide6), and no longer has a
  postinst or triggers (it selected the Plymouth theme).
- **`boswas-preset`** *(native desktop)*: `apply` writes
  `~/.config/boswas/appearance.toml` (light or dark, wallpaper), merges
  the preset's colours into the user's `kdeglobals` for KDE applications,
  records the choice in `~/.config/boswasrc` and asks the compositor to
  reload. The new `reset` command returns to the default look, Boswas
  Prism; Control Center's "Reset to Boswas Default" button runs it.
  - The tool writes the three files itself and starts no program. It no
    longer runs KDE's `kwriteconfig6`, so `boswas-branding` no longer
    depends on `libkf6config-bin`; a `boswasrc` written by an earlier
    version is read as before.
  - The new content of all three files is prepared before the first is
    written. Each is replaced atomically with mode 0600, and a file that
    is a symbolic link or not a regular file is refused with a message
    that names it (earlier versions replaced a link by a file).
  - It has one way of applying: the Plasma branch, the `--layout` option
    and the `layout` field of the JSON answer are gone (ADR-0042,
    [desktop/presets](desktop/presets/README.md)).
- **`boswas-cli`** *(native desktop)*: `boswas-info` shows `Boswas Desktop
  <version>`; the `screen-lock` check reads the compositor's lock policy
  (`/usr/share/boswas/compositor/compositor.toml`,
  `/etc/boswas/compositor.toml`) and falls back to the KDE keys on a Plasma
  image.
- **Control Center** *(native desktop)*: settings are native sections of
  its pages, in place of the Plasma settings modules, and apply at once
  (ADR-0041):
  - Network (Wi-Fi with passphrase entry, Ethernet and VPN status,
    airplane mode, connection details), Bluetooth, Sound and Power,
    through the same backends as the shell's quick settings;
  - Display (monitors arranged by dragging; resolution, refresh rate,
    scale and orientation, with a fifteen second "Keep these display
    settings?" that reverts), night light, mouse, touchpad, keyboard
    layouts, colour mode, background, motion, and the lock and screen-off
    times, by writing the compositor's user configuration;
  - date and time, region and language, default and start-up
    applications, recent files;
  - user accounts: your picture, name and password, other people's
    accounts, adding and removing users, through AccountsService.
  - The sidebar search finds a setting and jumps to it; a second start
    brings the running window forward. The program is built on the Boswas
    Qt toolkit, and no KDE helper program is used any more.
  - About Boswas OS shows the desktop (Boswas Desktop and its version)
    where it showed the Plasma version.
  - Personalization lists and applies the presets and resets to the
    default look. The box that also applied a preset's panel layout is
    gone with the layouts (ADR-0042).
  - In the Bluetooth list a game controller and a tablet show their own
    icons (they had the mouse's).
- **Compatibility Manager** *(native desktop)*: built on the Boswas Qt
  toolkit instead of a style sheet of its own. "Install with Boswas
  Compatibility Manager" is now a desktop entry for Windows programs and
  installer packages, which Boswas Files offers under "Open with", in
  place of the Dolphin service menu. Opening such a file still means "Run
  with Boswas": the package ships the Boswas desktop's defaults for these
  types (`/usr/share/applications/boswas-mimeapps.list`), because Files
  would otherwise go by the name of the application (ADR-0042).
- **`boswas-sysops`** *(native desktop)*: the package policy refuses to
  install the pinned Plasma, KWin, SDDM, System Settings and Plymouth
  packages, and protects `greetd` in place of SDDM, plasma-workspace and
  kwin-wayland.
- **`boswas-compat`** *(native desktop)*: the `display` grant also binds the
  compositor's Wayland socket into the sandbox, and the `boswas-winapp`
  AppArmor profile includes the Wayland abstraction. Without `DISPLAY`
  Windows applications draw through Wine's Wayland driver. Since the
  session runs Xwayland, a Windows application started from Start has
  `DISPLAY` and gets the X11 socket as well (ADR-0034); which driver Wine
  then uses has not been tested.
- **`boswas-android`** *(native desktop)*: the session check asks for a
  Wayland session instead of naming Plasma.
- **`boswas-os`** *(native desktop)*: depends on `boswas-desktop`,
  `boswas-bootsplash` and `boswas-apps`.

### Removed
- **From the image** *(native desktop)*: `kde-plasma-desktop`,
  `kwin-wayland`, `sddm`, `sddm-theme-breeze`, `kde-config-sddm`,
  `plasma-nm`, `plasma-pa`, `powerdevil`, `bluedevil`, `kscreen`,
  `systemsettings`, `kinfocenter`, `xdg-desktop-portal-kde`,
  `kde-spectacle`, `plasma-systemmonitor` and Plymouth. APT pins
  (`boswas-exclude.pref`) keep `plasma-workspace`, `plasma-desktop`,
  `kde-plasma-desktop`, `kwin-wayland`, `kwin-x11`, `sddm`,
  `systemsettings`, `plymouth`, `plymouth-themes` and `plymouth-label`
  out; the image verify hook fails the build if one is installed.
- **KDE's applications from the image** *(native desktop)*: `konsole`,
  `dolphin`, `kwrite`, `ark`, `okular`, `gwenview` and `kcalc`. The
  Boswas applications take their place, and Boswas Terminal is the
  system's `x-terminal-emulator`. The KDE applications are not pinned out
  and still run when someone installs them.
- **From the repository** *(native desktop)*: the Plymouth theme
  (`desktop/branding/plymouth/`), the KSplash theme
  (`desktop/branding/splash/`), the Boswas Launcher applet metadata
  (`desktop/launcher/`, Kickoff under the Boswas name), the SDDM
  configuration and theme settings (`desktop/defaults/sddm/`), the Plasma
  environment script (`desktop/defaults/plasma-workspace-env/`) and the
  `ksplashrc` and `kscreenlockerrc` defaults.
- **What was still built for KDE programs that are not shipped**
  *(native desktop, ADR-0042)*:
  - Plasma: the Global Themes of the ten presets with their panel layout
    scripts and previews (`/usr/share/plasma/look-and-feel/com.boswas.*`),
    and the `netpbm` build dependency their JPEG preview needed;
  - Konsole: the colour schemes and profiles of the presets and of Prism
    (`/usr/share/konsole`) and the default profile (`konsolerc`);
  - System Settings: `kcm-about-distrorc`, the file of its About module;
  - Dolphin: the service menu of the Compatibility Manager
    (`/usr/share/kio/servicemenus`).
  - What KDE applications a user installs need stays: the colour schemes
    in `/usr/share/color-schemes`, the `kdeglobals` defaults in
    `/usr/share/boswas/kde-settings`, and the merge of a preset's colours
    into the user's `kdeglobals`.

### Security
- **Protocol trust** *(native desktop)*: ordinary applications get the
  application protocols only. Layer shell, session lock, the window list,
  clipboard control and the input-method protocols are offered only to
  programs the compositor starts itself, over a connection it creates for
  them; sandboxed clients (security context) never get them.
- **Screen capture only with the user's consent** *(native desktop)*: a
  program that asks for pictures of the screen (a screenshot tool, a
  recorder, screen sharing) waits until the user has allowed it in a
  dialog of the shell. Neither answer is the default. A mark on the
  taskbar shows while a program may capture, and a click on it stops the
  capture. Nothing is captured while the session is locked, and
  sandboxed programs are not offered the protocol. The question names the
  program by its process name, which a program can choose (ADR-0037).
- **X11 applications are not isolated from each other** *(native
  desktop)*: they share one X server, as on any X11 system. They cannot
  observe Wayland applications (ADR-0034).
- **Input from other programs is not trusted** *(native desktop)*: what
  applications send as notifications, tray items and media information is
  read with fixed limits, and markup is removed. The polkit agent accepts
  requests from polkit only, and the password is checked by polkit's own
  helper.
- **The system applications** *(native desktop)* refuse to run as root,
  start nothing through a shell, never start a file by opening it without
  a question, and extract archives without following links or leaving
  the destination.
- **Settings cannot touch policy** *(native desktop)*: Control Center
  writes the user's layer of the compositor's configuration through one
  audited file writer and never `[policy]` or the trusted programs.
- **Compositor-owned lock** *(native desktop)*: locking blanks every output
  and takes input away from applications before the lock screen starts. If
  the lock screen exits without unlocking, the session stays locked and
  blank and the lock screen is started again. Only a lock client's unlock
  request, sent after PAM accepted the password, ends the lock.
- **Lock policy** *(native desktop)*: the session locks after ten minutes
  without input and before suspend. A user may shorten the timeout but not
  lengthen or disable it (`[policy] lock_idle_max_seconds`, read from the
  system files only).
- **Privileges** *(native desktop)*: the compositor and the shell run as
  the logged-in user, without setuid bits or capabilities; device access
  comes from the login session (libseat, logind). The boot screen daemon
  sets `no_new_privs`, enters a Landlock domain where the kernel has
  Landlock, and keeps three capabilities.
- **Memory safety** *(native desktop)*: the compositor, the shell, its
  service backends and the boot screen are Rust. `unsafe` is confined to
  seven audited source files (the PAM binding, descriptor passing, the
  EGL context, the copy into a capturing program's buffer, the boot
  screen's system calls); a static test fails if it appears anywhere
  else.

### Known limitations
- **Not yet run on real hardware or against real system services**
  *(native desktop)*: see the verification status above. The password
  dialog for administrator actions in particular has run against a
  stand-in for polkit only.
- **X11 applications** *(native desktop)* are drawn at scale 1 and look
  soft on a scaled monitor.
- **No live blur** *(native desktop)*: the taskbar and the flyouts are
  drawn on a blurred copy of the wallpaper; windows behind them do not
  show through.
- **Quick settings and Settings** *(native desktop)*: Bluetooth devices
  that need a PIN cannot be paired, and enterprise Wi-Fi (802.1X) cannot
  be set up. Control Center shows the proxy read-only and VPN connections
  as status only. A password that an administrator sets for another user
  is stored as SHA-512 crypt, not with Debian's default scheme.
- **Screen sharing** *(native desktop)*: the portal for it is installed
  and has not been tested; it shares the first monitor.
- **Input methods** *(native desktop)*: the compositor supports one, none
  is in the image.
- **The system applications** *(native desktop)* do less than what they
  replace: among other things there is no syntax highlighting in the Text
  Editor, the Document Viewer reads PDF only and cannot annotate, Photos
  loses XMP and IPTC data on save, the Terminal has no split panes. None
  has run on the desktop yet, and the Camera has run against a stand-in
  only. The list is in ADR-0040 and in the verification document.
- **Displays** *(native desktop)*: one GPU only; no HDR and no variable
  refresh rate.
- **Accessibility** *(native desktop)*: no screen reader can read the
  shell or the compositor's own surfaces.
- **Presets** *(native desktop)*: the taskbar, Start and the lock screen
  follow a preset's light or dark variant and wallpaper, not its accent
  colour. The preview pictures still sketch the panel of the Plasma image.
  The presets' lock screen art is installed and not shown. No preset has
  been applied in a running session since the tool writes its files
  itself; the tests run in a temporary home.

## [1.0~alpha3] - 2026-10-04 (Device Agent, Compatibility Manager, Control Plane, Boswas OS experience)

### Added
- **The Boswas OS experience** ([docs/experience/README.md](docs/experience/README.md)):
  - **Identity (ADR-0005 revised):** the system presents itself as Boswas
    OS. `os-release` (`ID=boswas`, `ID_LIKE=debian`), console banners and
    motd, boot medium name, About page and live user all say Boswas OS.
    The remaining Debian references and why they stay:
    [docs/experience/debian-references.md](docs/experience/debian-references.md).
  - **Boot:** the Boswas OS Plymouth theme for boot, shutdown and the
    disk-encryption passphrase; a hidden GRUB menu on installed systems,
    keeping Debian's EFI directory for Secure Boot.
  - **Login and session:** a Boswas login screen logo and background, and
    the Boswas session start-up splash.
  - **Ten presets** (Horizon, the default, Midnight, Aurora, Slate,
    Carbon, Pearl, Ocean, Ember, Nebula, Classic). Each brings a Global
    Theme, colour scheme, light, dark and lock wallpapers, panel layout,
    icon treatment and Konsole profile; switch with `boswas-preset` or in
    Control Center ([desktop/presets](desktop/presets/README.md)).
  - **Icons:** the Boswas and Boswas Light icon themes, 61 designs for
    system settings, applications, Boswas applications and Windows
    compatibility ([desktop/icons](desktop/icons/README.md)).
  - **Desktop:** Boswas Launcher (KDE's launcher under the Boswas name)
    and a Boswas panel layout.
  - **Boswas Control Center** (new package `boswas-control-center`): the
    settings hub with the Boswas Security Center, Software Center, Update
    Center, About Boswas OS and, in live sessions, Install Boswas OS.
  - **Terminal:** the Boswas welcome and prompt.
  - **Live USB:** the live desktop starts directly, and installation is
    only by explicit choice (application menu, Control Center or boot
    menu). The new release-blocker boot test (scenario `usb`) checks this
    on a UEFI/Secure Boot USB boot, including that the internal disk is
    unchanged. An opt-in installed-system test is available
    (`./test.sh --install-test`).
- **`boswas-device-agent` package**
  ([docs/device-management/README.md](docs/device-management/README.md)):
  - **Identity:** a persistent, random device identity
    (`/var/lib/boswas/agent/identity.json`). It is never derived from
    hardware identifiers and never silently regenerated; it can be
    pre-provisioned to survive reinstallation.
  - **Configuration:** `device.conf` gains agent settings (Control Plane URL
    and pinned CA, remote commands, heartbeat and inventory intervals,
    inventory, telemetry, update and logging policies). Validation refuses
    secrets and insecure files (`boswas-device config validate`).
  - **Inventory:** allowlisted: OS, Boswas packages, Windows applications
    aggregated over users, Windows runtime health; with `standard` also CPU,
    memory, model and disk. Never serial numbers, MAC addresses or user names.
  - **States:** normalised device states (INITIALIZING, READY, DEGRADED,
    OFFLINE, UPDATING, ERROR, MAINTENANCE) with validated transitions, and
    application states.
  - **`boswas-device-agent.service`** (root, sandboxed):
    - local management API with kernel-credential authorisation;
    - Control Plane client over mutual TLS: enrollment with a one-time token
      and a device-generated EC key, heartbeats, inventory, posture, events;
    - a bounded offline outbox with capped exponential back-off;
    - signed policies;
    - typed command execution, idempotent and expiring;
    - agent updates through `boswas-agent-update@.service`.
  - **`boswas-session-agent.service`** (systemd user unit): the backend of
    the Compatibility Manager, and the executor of remote application
    commands as the logged-in user, through `boswas-winapp`.
  - **`boswas-device`:** status, identity, configuration, inventory, policy,
    commands, events, enrollment, maintenance.
- **`boswas-compat-manager` package (Compatibility Manager, PySide6):**
  - **Pages:** dashboard; application library with filters (All, Installed,
    Running, Updates, Blocked, Repair Required, Unsupported); system status.
  - **Install:** checks the installer first and refuses 32-bit installers
    with the product message.
  - **Application details:** read-only permissions; logs with copy, save and
    clear.
  - **Actions:** launch, stop, repair, update and remove (with an explicit
    removal plan).
  - **Boundaries:** it never runs Windows code or touches prefixes itself.
  - **Dolphin action:** "Install with Boswas Compatibility Manager".
- **Boswas Control Plane** (`control-plane/`, server package
  `boswas-control-plane`, Python stdlib + SQLite, ADR-0017;
  [docs/device-management/control-plane.md](docs/device-management/control-plane.md)):
  - device registry, enrollment with a device CA;
  - one-time enrollment tokens; an enrolled device re-enrolls only with a
    token issued for its ID (`token create --device`);
  - heartbeat, offline detection, inventory and posture;
  - typed command system (lifecycle QUEUED → SENT → ACKNOWLEDGED → RUNNING →
    SUCCEEDED/FAILED, EXPIRED, CANCELLED; idempotency keys; expiry);
  - Ed25519-signed, versioned policies;
  - catalog of 64-bit applications with stored installers (32-bit entries
    are UNSUPPORTED_ARCHITECTURE and never installable);
  - hash-chained audit trail;
  - operator API tokens with roles;
  - HTTPS API `/api/v1` on two ports: the device port (mutual TLS) and the
    operator port (dashboard and operator API; no client-certificate
    request, so browsers show no certificate prompt);
  - web dashboard;
  - `boswas-cp` administration.
- **`boswas-winapp`:**
  - new commands `inspect` (dry run), `stop`, `upgrade`, `runtime`, and
    `logs --launch` and `--clear`;
  - normalised application states;
  - MSI architecture detection;
  - policy keys `BLOCKED_APPLICATIONS`, `ALLOWED_APPLICATIONS`;
  - the managed policy layer (`/var/lib/boswas/compat/policy.conf`).
- **Tests:**
  - unit tests for every component (WinCompat 91, device agent 96,
    Compatibility Manager 105, Control Plane 31 including the end-to-end
    agent ↔ Control Plane tests over mutual TLS, CLI 25);
  - a device management image check;
  - a device management runtime stage in the image (real Wine, session
    agent, Compatibility Manager window, Control Plane);
  - device management checks in the QEMU boot test under AppArmor;
  - new static checks (64-bit only, no shell execution, no Boswas ID,
    service hardening).
- **Documentation:**
  - device management and Control Plane guides;
  - ADR-0017 to ADR-0021;
  - updated compatibility, privacy, security, CLI and build documentation.

### Changed
- **New Boswas OS brand (ADR-0008 revised;
  [desktop/branding](desktop/branding/README.md)):**
  - **Logo:** the Boswas OS "b" mark (gold stem and dot, silver ring and
    swoosh) replaces the Boswas Group gear everywhere: launcher icon, About
    page, boot menu, wallpaper, login and lock screens, installer banner,
    Compatibility Manager icon and dashboard favicon. It is traced from the
    supplied logo (`tools/trace_mark.py`).
  - **Typeface:** Orbitron is the brand typeface. The wordmarks are
    Orbitron outlines, and `boswas-branding` installs the font (SIL OFL 1.1).
  - **Accent:** the logo's gold replaces teal in the KDE colour scheme, the
    boot menu, the installer and the Control Plane dashboard.
- **64-bit only is final (ADR-0014):** Boswas OS v1 runs x86_64 Windows
  applications only. Every 32-bit refusal says "This application requires
  32-bit Windows compatibility, which is not supported by Boswas OS." The
  build fails if `wine32` or i386 multiarch appear.
- **Agent messages (never deployed in M1):**
  - the heartbeat is now liveness only (`heartbeat-v2`);
  - its former content is the status report (`status-report-v1`);
  - typed commands (`device-command-v2`) replace the draft command set.
- **`/etc/boswas/device.conf`:** now configuration only; the agent keeps its
  state in `/var/lib/boswas/agent`. `boswas device status`, `boswas info`
  and the management checks read the agent's identity and status.
- **`boswas-winapp` and the agent binaries** ignore the unit-test
  environment hooks (`BOSWAS_SYSROOT`), so a user cannot point them at a
  policy of their own.
- **Image:** `boswas-os` depends on `boswas-device-agent` and
  `boswas-compat-manager`. The builder image gains PySide6, Qt's offscreen
  platform and openssl.

### Known limitations
- **No Boswas ID:** operators use local API tokens; devices use
  certificates.
- **Control Plane scope:** single tenant, one policy per device (no group
  hierarchy), SQLite, no HA.
- **Device certificates** last 365 days; renewal means re-enrollment.
  The device key is not TPM-backed yet.
- **`UPDATE_AGENT`** needs a Boswas APT repository (M7) to deliver new
  versions.
- **Remote application commands** go to the active user's session only.

## [1.0~alpha2] - 2026-10-04 (Milestone 1: Windows compatibility platform)

### Added
- **`boswas-compat` package (WinCompat).**
  - **`boswas-winapp`:** `install`, `remove`, `list`, `launch`, `status`,
    `repair`, `logs`, `catalog` and `manifest validate`, with `--json` and
    stable exit codes.
  - **Prefixes:** one Wine prefix per application, owned by the user,
    stored in `~/.local/share/boswas/wine/<id>/` and seen as
    `/var/lib/boswas/wine/<id>/` inside its sandbox. Windows software never
    runs as root.
  - **bubblewrap sandbox per application:**
    - never visible: the user's home, `/home`, D-Bus, `/run`, or other
      prefixes;
    - only when granted: network, display, audio, GPU and folders;
    - cleared environment.
  - **AppArmor profile `boswas-winapp`:**
    - attached to the in-sandbox runner;
    - grants no capabilities, mounts, user namespaces or D-Bus;
    - nothing in a prefix may run as a Linux program;
    - Wine refuses to start unless the profile is enforcing.
  - **Compatibility manifest format v1:**
    - JSON schema plus a stdlib validator;
    - statuses `unknown`, `untested`, `experimental`, `tested`,
      `approved`, `blocked`;
    - pinned installer SHA-256 for validated statuses.
  - **Layered compatibility catalog:** managed, local and system layers;
    `blocked` in any layer wins.
  - **Policy:** `/etc/boswas/compat/policy.conf`, fail-closed.
  - **Prefix defaults:** no crash dialog; `winemenubuilder` disabled.
  - **Installer handling:** detection by signature, PE architecture check,
    NSIS/Inno/MSI silent switches.
  - **"Run with Boswas":** handler for `.exe` and `.msi`.
- **Device agent foundations** (`packages/boswas-device-agent/`, not
  packaged yet):
  - API v1 message models and JSON schemas (heartbeat, enrollment,
    compliance, commands);
  - `ControlPlaneClient`, `CredentialStore`, `PolicyVerifier`,
    `InventoryCollector` and `CommandHandler` interfaces, plus an offline
    client;
  - random UUID v4 device identity and validated `device.conf` writer;
  - closed privacy allowlists;
  - Windows application inventory collector;
  - Boswas ID boundary interfaces only.
- **`device.conf` contract:** `DEVICE_PROFILE`, `DEVICE_CERTIFICATE`
  (public certificate reference).
- **`boswas` CLI:**
  - `boswas winapp ...` runs `boswas-winapp`;
  - `boswas device status` shows profile and certificate;
  - new posture check `winapp-confinement`.
- **Tests:**
  - unit tests for WinCompat and the agent;
  - static checks for manifests and profile consistency;
  - package checks for `boswas-compat`;
  - image checks;
  - an end-to-end WinCompat runtime test in the image (real Wine and
    bubblewrap, isolation probes, policy refusals);
  - WinCompat checks in the QEMU boot test under the real kernel's AppArmor.
  - The Windows test application is built from source with MinGW-w64.
- **Documentation:** compatibility guide, device-management guide, ADRs
  0011–0016, milestone roadmap.
- **Security review of WinCompat:** seven findings, all fixed with regression
  tests (docs/compatibility/README.md, "Security review"). The most severe
  was a planted symlink that made the installer clean-up delete files in the
  user's home.

### Changed
- **`test.sh`:** a stage that exits non-zero without recording a failure
  (a crashed test script) is now recorded as a FAIL instead of passing
  silently.
- **Builder image:** gains `dh-apparmor`, `apparmor` and
  `gcc-mingw-w64-x86-64-win32`.
- **Build inputs:** the package build and the config hash include
  `compatibility/`.
- **Image:** `boswas-os` depends on `boswas-compat`. The image verify hook
  checks `boswas-winapp` and compiles the profile.

### Known limitations
- **64-bit Windows programs only.** 32-bit installers are refused with a
  clear message; adding `wine32` or a WoW64 Wine is an open decision
  (ADR-0014).
- **Empty catalog.** The system catalog is empty until applications are
  validated.
- **Permissive alpha policy.** The shipped policy allows unlisted
  applications, with no network.
- **Direct Wine use.** Direct `/usr/bin/wine` use is not mediated.
- **X11.** X11 applications can observe other X11 clients (Xwayland only
  under Plasma Wayland).
- **No device agent service yet:** no backend contact (Milestone 2).

## [1.0~alpha1] - 2026-10-04 (v1 Alpha)

First bootable foundation. Phases 0–2 complete, Phase 3 baseline.

### Added
- **Repository and build system:**
  - Repository structure and architecture documentation (ADRs, roadmap,
    update architecture, privacy, HCL).
  - `build.sh`: native on Debian 13, or containerised on other hosts. It
    produces the ISO, SHA-256 checksum, manifest and JSON build record.
  - `clean.sh`, `test.sh` and a `Makefile`.
- **Image:**
  - Debian 13 "trixie" live-build configuration with an explicit,
    documented package manifest and build-time exclusions.
  - KDE Plasma 6.3 desktop.
  - GRUB boot menu for BIOS and UEFI; Debian-signed Secure Boot chain.
  - Debian Installer in live mode. Its preseed enforces encrypted LVM
    (LUKS2), locks root and defaults the hostname to `boswas-device`.
- **Branding:** built from the official Boswas Group logo and lettering
  (traced vectors): boot menu, installer banner, Plasma global theme, Boswas
  Dark colour scheme, wallpaper, login and lock screens, launcher icon and
  About page.
- **`boswas-security`:**
  - nftables firewall (inbound deny)
  - systemd preset (firewall on; SSH off unless provisioned)
  - kernel hardening sysctls
  - auditd rules
  - journald retention
  - sudo and pwquality policy
  - sshd policy
  - security-only unattended upgrades
  - USB policy framework
- **`boswas-cli`:** `boswas`, `boswas-info` and `boswas-status`, with
  `--json` output, stable exit codes and unit tests.
- **`boswas-os`:** release identity, `/etc/boswas` (device and update
  configuration contracts) and a login banner.
- **Tests:** static checks, lintian, package manifest resolution, ISO and
  image content, security baseline, Wine, and a QEMU boot test (UEFI Secure
  Boot plus serial functional checks).

### Known limitations
- No device agent, Boswas ID, Store or WinCompat tooling yet (Phases 4–7).
- **Windows apps:** 64-bit only (no `wine32`).
- **USB policy:** not enforced.
- **Installed system:** no graphical boot splash.
- **Release artifacts:** not yet signed. Builds are not yet pinned to a
  Debian snapshot.
