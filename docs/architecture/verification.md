# What has and has not been verified

The state of the native desktop on 2026-11-16 (3.0~alpha1). The reports in
`docs/reports/` describe the KDE Plasma image of 2026-11-12 and are not
repeated here.

"Verified" below means an automated test, or a check that a builder
recorded, in the build environment. That environment has no graphics
device, cannot reach a Debian mirror and cannot build the image. Since
the boot failure of 2026-11-16 (ADR-0044) the built packages are also
booted there in a virtual machine, on another distribution's base system;
see [Booted in a virtual machine](#booted-in-a-virtual-machine) for what
that does and does not show. Everything in [Not verified](#not-verified)
follows from these limits.

## Automated tests

Counted from the sources (`#[test]` attributes; `Ran N tests` for the
Python suites, which were run). The numbers move as work lands.

| Suite | Tests | What they cover |
|-------|-------|-----------------|
| `boswas-ui` (Rust) | 53 | The token file as the programs read it, drawing, text (also without the font configuration, and without any font), icons (looked up theme by theme as the icon theme specification says: a program's own PNG icon in `hicolor` beats Breeze's SVG of it, generic actions still come from Breeze, ADR-0052), the materials, the contrast rules for every accent colour in both schemes; drawing a part again under a clip, the stretched shadow, kept shaped text (ADR-0056); colour emoji (ADR-0058); glass (ADR-0063): both schemes have it, a tint raised only as far as text needs, glass over the luminosity band translucent and legible, the grain, translucent glass without its own shadow, the edges, a glass recipe saturating and graining the backdrop, a kept fill equal to one drawn directly; the faster paths of ADR-0064, each pixel for pixel against tiny-skia: rectangles on the pixel grid, a picture laid on an empty canvas, a kept picture blended in only the parts drawn, a rectangle drawn in parts, conversion to ARGB in any rectangle, large shadows let go of and drawn again the same |
| `boswas-compositor`, unit | 157 | The exit statuses, when a shell that ended is started again and when the login screen is given up, the messages for a missing seat or graphics device, the configuration layers and their rules (a user cannot relax the lock policy, name a trusted program or turn X11 back on; a damaged file is ignored alone), the shortcut table, snapping and the layouts of the window picker, Task View, the snap layouts and the window menu, the frame geometry, the clip geometry, animation timelines, capture regions (also on a rotated output), EDID names; the mode choice and frame pacing at high refresh rates (ADR-0048); the keys of ADR-0049 and their limits, the colour filter matrices, the magnifier's view, sticky and filter keys, snap layouts by dragging to the top, the title's text size; `[lock] show_weather` as a setting of the user (ADR-0051); the recommended scale for built-in panels and external monitors of many sizes, the cases that mean 100 % (no size, a size of the wrong shape, more than 1.6 m, virtual graphics devices), and which scale setting wins, an old explicit 1.0 included (ADR-0055); Super+V, Super+., Super+; and Super+W (Ctrl+V stays the application's), `[clipboard]`, `[desktop]` and `[taskbar] widgets` with their defaults and checks, the terminals that paste with Ctrl+Shift+V (ADR-0058 to ADR-0060); the shortcuts of ADR-0062 (their neighbours keep their keys, Ctrl+Alt+Del through an inhibitor, none while locked), night light turned on or off against a schedule, the projection choices, Num Lock over a reload, the settings page command; glass (ADR-0063): the background effect protocol's regions and double buffering, the passes against the token's blur, the reach and the damage it grows, the halved capture, the band and the saturation, the high-contrast preset record, and on Mesa's software rasterizer (llvmpipe, EGL surfaceless; the two tests pass without running where EGL gives no renderer) the shaders against a reference on the processor, no new blur for a still scene, drawing only inside the region's staircase, the band on white and on a dark green; ADR-0064: the elements in front filled with spacers up to a fixed count, the memory setting for the shell's trust only, Task View's blurred desktop in the output's order showing the same pixels, the darkened desktop standing for its two layers with pixman and with OpenGL ES (llvmpipe) at 100, 125 and 200 %, and let go of three minutes after Task View last faded |
| `boswas-compositor`, end to end | 29 | The real binary on its headless back end with real clients and the control socket: a login screen that keeps failing or cannot be started ends the compositor with status 4, and a desktop session stays; without a seat the compositor ends with status 3 and says why; a shell that says `quit` and leaves ends it normally; the login screen runs no X server; ordinary clients get no privileged protocol and a started program does; a configured input method gets its two protocols and nothing else; the limits of the public control socket; a client drawing at its output's refresh rate (60 and 144 Hz); a window's life; the window picker, Task View, snap keys and window menu through injected keys; screen capture only after the user's answer and never while locked (with `grim`); an X11 program through Xwayland (with `xclock`); the lock across a crashed lock screen; the lock screen learning `[lock] show_weather` (ADR-0051); a 27-inch 3840x2160 monitor (`--headless-physical-size 597x336`) getting 150 % and `recommended_scale`, a scale that is set winning over it, and 100 % without a size (ADR-0055); an output with nothing new is not drawn and frame callbacks still come at its pace (ADR-0056); the shell's `paste`: Ctrl+V for an editor, Ctrl+Shift+V for `foot`, the window reads the clipboard, nothing while locked, refused on the public socket (ADR-0058); Num Lock at start, after a change in Settings and over a reload, night light turned on now holding against the schedule until Settings changes it, every shortcut of ADR-0062 reaching a subscribed client, Super+Home, Super+Shift+Up and Down, and `project` with one monitor (ADR-0062); `tests/invisible.rs`: a layer surface of one fully transparent single pixel adds no damage and still takes the pointer, and a small surface appearing in front and going damages only itself; `tests/taskview.rs`: Task View opened and closed again draws no card and no strip (ADR-0064) |
| `boswas-services`, unit | 60 | Parsers, limits, conversions and policies of the ten services; what the lock screen, the login screen and Settings may each ask for |
| `boswas-services`, integration | 85 | Every service against two private `dbus-daemon` processes and scripted stand-ins (see below): notifications, tray, media, network, Bluetooth, audio, power, brightness, session, the polkit agent, start-up and shut-down, buses that go away, and that no secret reaches the log |
| `boswas-services-bridge` | 5 + 1 | The command parser, and the program itself: it reports states, answers bad lines and ends with its input |
| `boswas-shell` | 241 + 4 | Every surface drawn without any font; what is reported when no font is installed; every surface drawn in both schemes and checked for controls inside the surface and reachable by keyboard; the taskbar's grouping and layout, Start's search, toasts and the notification centre, the dialogs (Run without a shell, the polkit dialog, the choice of administrator), the capture mark, pinned applications, the greetd messages, the PAM conversation, the users the greeter lists; the desktop settings of the `config` event, the taskbar at the left and without Task View, Start's layouts and recommendations, the do-not-disturb schedule over midnight, the least time a toast stays, the list of notification senders and of applications first seen; the lock screen's weather summary (every bad value rejects it; only a regular file of the user, at most 16 KB, not through a link, not blocked by a FIFO; old and future summaries; the hours that are still ahead), the card's place at every text size clear of the clock, the hint and the status icons, and that it appears only in the lock screen's clock view on the interactive output, with the setting on (ADR-0051); Start (ADR-0053): the default pins and that choosing a view does not freeze the taskbar's defaults, the category rules, smooth scrolling and revealing the focused control, the keyboard reaching every part of the page, and a bound on the cost of drawing it again; opening files (ADR-0054): types from the name, the order of the `mimeapps.list` files, defaults that are not installed, added, removed and hidden entries, sub-directory ids, `Exec` turned into an argument list for hostile file names, the shipped default applications (each declared by the entry it names), the fallback to `xdg-open`. Drawing (ADR-0056): a hover redrawn in part is pixel for pixel the whole drawing (Start in its three views, the taskbar, quick settings, the notification centre, a menu and the lock screen, at 100 and 150 %), a pass across Start's controls redraws under 5 % of its pixels, Start opens without rasterising an icon once the icons are prepared, and in release builds a budget for Start at 200 % (45 ms for a whole frame, 6 ms for a hover change). The clipboard history and the emoji panel (ADR-0058): the cap, duplicates and Clear all, the secret hints, the choice of type to read, rich text to text, damaged, linked and oversized pinned files, the transfer limits, emoji search, recent items and tones, the Turn-on edit, both flyouts at 100, 175 and 225 % with and without fonts and the emoji font. The widgets board (ADR-0059): the arrangement and its file, the taskbar's button and its place, Calendar's repeating events (and a committed copy of a cross-check against Calendar's own model, `tests/fixtures/calendar/`), time zones from `/usr/share/zoneinfo`, Clock's file, the newest picture. Desktop icons (ADR-0060): the grid and the order, the places file, names and free names, the Trash, copying and moving without links or overwriting, the launcher rule, the View menu's edit of `compositor.toml`, the menus, the rename field. The end-to-end tests run the real compositor and shell: `tests/opening.rs` (Print, a click on the notice, and a stand-in Photos gets the picture; "Open folder", and a stand-in Files gets the folder's `file://` address), `tests/clipboard.rs` (copy, Super+V, Down and Enter paste the right item; a password manager's item is not kept; Ctrl+P writes the pinned file with mode 0600; Super+. inserts an emoji and the clipboard is given back; with the history off nothing is kept until Turn on) and `tests/desktop.rs` (Ctrl+Shift+N makes a folder, typing renames it, Delete trashes it with its info file; Super+W, Tab and Enter open Weather). The round of ADR-0062: the new-window action and its stand-in, every right-click menu with the tools installed and missing, every Settings page key against Control Center's catalogue, the GSettings mapping and "only when different", the account picture's limits and round crop, the security screen's choices and keyboard order, the projection flyout, the taskbar's keyboard focus within the hover rule, the level's place on the lock screen, the night light tile's explanation and the battery button, the new control events. Glass (ADR-0063): the blur region's staircase, which surfaces ask for blur, the magnifier's targets, caps and easing, the sign-in view's darkening, a taskbar hover that fills nothing again. ADR-0064: a snipping drag drawn in parts equal to the whole drawing at 100, 125 and 200 %, with only strips drawn for small moves, and the selection showing the captured pixels; the shared background read back only whole and valid, and shown by the lock screen as the desktop drew it; the background drawn on several threads equal to the old one; the single-pixel colour channels; the compositor's memory setting not passed on; the preparation in slices, Start's panel made on another thread equal to one drawn directly, no icon looked up on the event loop, and in release builds no slice over 8 ms |
| `boswas-bootsplash` | 161 unit, 19 + 5 + 1 + 1 | The rasteriser (exact coverage, non-zero winding, reused buffers); the vector mark against resvg's rendering of the same SVG and each stage of its assembly; glyph outlines and kerning; the background's bloom and dither; the timing curves and the choreography (intro, fades, glides, breathing, a progress, the farewell, nothing moving with `--static`) (ADR-0050); rendering, layout on eleven display sizes (portrait included), damage, the protocol, both client dialects, the passphrase editor; the whole frame after a mode-set; devices handed over by the supervisor; the console left alone once a graphical session has it; the real programs headless with a pseudo-terminal (a second start, a boot screen that does not answer); the packaged units and initramfs hook; Debian's unmodified `askpass` in a scratch root (needs root and the helper, skipped otherwise); frame cost and damage of the intro, the ring, the glow, the glide, the caret and the text box |
| `desktop/ui` (Python) | 12 | The token file and the generated KDE colour schemes: references, the fixed values, the Fluent geometry, contrast, staleness; `material.glass` (ADR-0057) |
| `desktop/ui/qt` (Python) | 181 | The Qt toolkit on Qt's offscreen platform: theme, style sheet, widgets, navigation, contrast (every accent colour in both schemes), text size, scroll bars, single instance, workers; icons: a program's own icon in `hicolor` before Qt's theme (ADR-0052); Boswas glass (ADR-0057): legibility on black, white and saturated pictures, transparency off and the high-contrast preset, one backdrop per size step, popups and dialogs as glass, glass surfaces drawn once per size; the magnifier over time, with reduced motion and with the keyboard focus, no text moving; `--new-window` and the window a later start goes to (ADR-0061); a window's new backdrop made only after a pause in a resize, identical at rest (ADR-0064) |
| `desktop/icons` (Python) | 52 | The icon packs: the files match the generator, every required name exists, aliases repeat their design; every coloured Prism object fills 88 to 94 % of its square, centred, with a low shadow inside the canvas; no icon is named after another program; resolved the way the icon theme specification says, with stand-ins for `hicolor`, Breeze, Breeze Dark and Adwaita, a program's own icon wins over Breeze's drawing of it (ADR-0052) |
| `desktop/cursors` (Python) | 82 | The pointer theme: the sources match the generator, every CSS, X11 and toolkit name resolves after installation, the XCursor files are read back by libXcursor and match `xcursorgen` byte for byte, hotspots, animation frames, two installations are identical |
| `desktop/presets` (Python) | 72 (1 skipped here) | The presets and `boswas-preset`: it starts no program and names no KDE tool, writes its three files atomically, and refuses symbolic links |
| `boswas-compat-manager` (Python) | 111 (1 skipped here: `desktop-file-validate` is not installed) | The Compatibility Manager on the toolkit; the installer entry for Boswas Files and which application stays the default |
| `boswas-control-center` (Python) | 730 (5 skipped) | The categories, pages, deep links of earlier versions and search (ADR-0049), the lock screen's weather switch and its search words (ADR-0051), the recommended scale on the Display page (marked, shown without a setting, left automatic when chosen, written when a scale for every monitor is set; ADR-0055), another program's own icon in the application lists (ADR-0052); rows under the pointer grey and never black, with real hover events, in both schemes with glass on and off (ADR-0057); System > Clipboard (ADR-0058); the Widgets switch and the Desktop icons section, with the real compositor accepting every key written (ADR-0059, ADR-0060); Brightness on Display, the Ethernet section's `nmcli` commands, Eject's `udisksctl` steps, printing without recommendations, the pointer size passed to the user manager, and clearing the applications' recent lists (ADR-0061); every page added with them in a whole window on a fake root with fake programs, with the exact command or file change of each action; the settings framework and every section against stand-ins (a fake bridge, the real settings files read back as the compositor reads them); the file writer against symbolic links and read-only directories; the safety rules (only one module writes files, only one starts programs, every program in its fixed forms, no KDE program is named, opening any page runs nothing privileged and changes nothing); that every page the shell links to exists; the Users page against a stand-in for AccountsService and a scripted `passwd`, SHA-512 crypt against the specification's vectors |
| `boswas-apps` (Python), one suite per application | Terminal 303, Task Manager 261, Archive Manager 256, Files 230, Photos 208, Calculator 163, Media Player 136, Text Editor 132, Weather 129, Calendar 118, Snipping Tool 115, Reminders 103, Document Viewer 92, Clock 58, Camera 40 (2,344 in 15 suites) | Each application's logic (parsers, file operations, the terminal emulator against hostile input, 78 extraction tests with hand-built hostile archives, the compositor's capture request against a stand-in socket, Open-Meteo answers written from its documentation, Weather's summary for the lock screen and its update without a window: the setting in the compositor's three files, the units and clock from the locale, offline and root behaviour, atomic 0600 writes; Photos' background written to `appearance.toml`; `--new-window` in Document Viewer, Photos, Calculator, Archive Manager and Snipping Tool; Files' content and Calculator's keys as glass) and its window through real clicks and keys on Qt's offscreen platform, with stand-ins where the real thing would touch the system |
| `boswas-sysops` (Python) | 123 | The privileged helper's operations against stand-ins; `apt-install --no-recommends` (ADR-0061) |
| `packages/boswas-desktop/tools` (Python) | 4 (1 skipped where `unicode-data` is not installed) | The emoji data's generator: the committed `emoji.tsv` against Debian's Unicode data, `--check` (ADR-0058) |

One more `#[test]` in the compositor's end-to-end file is a helper
program for the others and is not counted, four more in the shell write
pictures of the lock screen, of Start, of the clipboard and emoji
flyouts and of the shell's menus and screens of ADR-0062 for people to
look at, and one measures how long the emoji panel takes to draw (all
five ignored unless asked for). On the tree with the work of ADR-0056 to
ADR-0061 merged, the desktop workspace's `cargo test --locked` passed 544
tests, failed none and ignored 5; with ADR-0062 merged it passed 583 and
ignored 6, and with ADR-0063 as well (7306311) 612 passed, none failed,
6 ignored. The release-only frame budgets of ADR-0056 passed on the
branch of ADR-0062. With ADR-0064 merged up to its Task View fold
(7f4b86b) 632 passed, none failed, 6 ignored; on the merged tree with the
fold (4b3b232) the package build's release run with Rust 1.85 passed 635
and ignored 6, and with the two fixes after it (08bafcd) 637. The release-only budgets, with the limit on a
slice of the preparation of ADR-0064, passed on that work's branch.
Three of the settings tests run against the real compositor on its
headless back end and are opt-in (`BOSWAS_TEST_COMPOSITOR=1`).

`tests/static/test_sources.sh` also runs two parts of the boot as they are
shipped, with stand-ins around them (ADR-0044): the live-config component
of the image (which user the live session signs in, for five kernel
command lines and sets of users) and the login screen's start script (what
follows each exit status of the compositor).

`tests/static/test_sources.sh` checks, for this round: that a public
control connection gets exactly `version`, `lock`, `reload-config`,
`outputs` and `capture`; that the privileged protocols are filtered by
client trust; that `unsafe` appears only in the seven audited files; that
the applications and the toolkit start no shell; and that `boswas-preset`
and the packages name no Plasma, Konsole or Dolphin program (ADR-0042).
The package stage (`tests/packages/test_packages.sh`) also checks that
`boswas-apps` ships Weather's update for the lock screen as a hardened
user service with its timer, part of the Boswas session and enabled for
every user by the postinst (ADR-0051); that `boswas-desktop` depends on
`xdg-utils`; and that `boswas-apps` ships the default applications in
`/usr/share/applications/mimeapps.list` (pictures in Photos, folders in
Files, web addresses in Firefox ESR), and that every desktop entry it
names but Firefox ESR's is installed by the packages (ADR-0054); and,
since ADR-0062, that `boswas-desktop` depends on `libglib2.0-bin`,
`dconf-gsettings-backend` and `gsettings-desktop-schemas`. The
unit stage (`tests/unit/test_units.sh`) and the package build of
`boswas-desktop` also run the tests of the emoji data's generator
(ADR-0058); the public control commands are unchanged, so the static
stage's pinned set still holds (`paste`, `project`, `projection` and
`night-light` are trusted commands). The static stage passed 82 checks on
the trees of ADR-0062 and ADR-0063, and of ADR-0064 (7f4b86b).

### What the tests use in place of the real thing

- **System daemons:** scripted D-Bus peers that play NetworkManager,
  BlueZ, UPower, the power profiles daemon, logind, polkit, tray items
  and media players. `pw-dump`, `wpctl` and polkit's helper are shell
  scripts the tests write.
- **Graphics:** the compositor's headless back end, which renders in
  software. The clip shader for rounded corners and everything in the
  hardware back end are not executed by any test. The blur shaders of
  ADR-0063 run on Mesa's software rasterizer through EGL's surfaceless
  platform, where the machine has it.
- **Input:** key and pointer events injected through the control socket
  (`--test-input`).
- **Console and display of the boot screen:** a pseudo-terminal and a
  headless output or a mock display.
- **Qt:** the offscreen platform. It cannot drag, has no session to
  activate a window in and no clipboard of other programs.
- **Devices and services of the applications:** a fake camera, a fake
  media back end for sound output and device switching, a private
  `dbus-daemon` with stand-ins for the notification service and an MPRIS
  client, a fake services bridge for the settings.
- **The user:** the build environment runs as root, and the applications
  refuse to run as root. Their tests patch that check or run the logic
  below it; the launchers were not started. Files' suite also passed as
  an unprivileged user.

Three end-to-end tests of the compositor need a program that is not a
build dependency (`grim`, `Xwayland`, `xclock`) and skip themselves
without it.

### Looked at by hand during development

Recorded by the builders; not repeatable from the test suite.

- The boot screen's frames as still pictures at 1024x768, 1366x768,
  1920x1080, 2560x1600 and 3840x2160; once, `mkinitramfs` with the
  package installed (with the initramfs tools of another distribution)
  and its unlock helper against the packaged daemon; a scratch package
  build of `boswas-bootsplash`, lintian-clean
  (`packages/boswas-bootsplash/README.md`).
- The boot screen drawn from vectors (ADR-0050), as review frames at
  1366x768, 1920x1080, 3840x2160 and 1080x1920: every state, the intro
  frame by frame, the ring's cycle, the glide to the unlock screen and
  back, the fades, the bloom with its contrast raised to look for bands,
  and a 25-frames-a-second preview made from the frames. Not at a
  display's refresh rate.
- The lock screen's weather card (ADR-0051): the headless compositor with
  the real `boswas-shell --lock` at 1920x1080 and 1366x768, on bright and
  dark wallpapers, at 100, 175 and 225 % text, in both schemes; while
  locked, the setting turned off and on with `reload-config`, the summary
  removed, a summary 1.5 hours old ("Updated at"), and a damaged one.
- Start of ADR-0053, as pictures of the shell: the category, grid and
  list views, at the top and scrolled, at 1920x1080, 1366x768 and
  2560x1440, at 100 and 175 % text, in both schemes, with every pin shown,
  with the index of letters, searching, and with the taskbar at the left;
  and live on the headless compositor: scrolling, the keys, the index,
  Show all, the right-click menus, and Start at 125 % scale. The
  screenshot's toast and its entry in the notification centre (ADR-0054);
  Settings > System > Display with the recommended scale (ADR-0055); and
  contact sheets of the Prism icons before and after ADR-0052, at 16 to
  96 px.
- The round of ADR-0056 to ADR-0061 (2026-11-26):
  - Drawing (ADR-0056): screenshots of the build before and after
    compared pixel by pixel at 1920x1080 and 125 % and at 2560x1440 and
    200 % (identical but for at most one colour level in 3 to 11 pixels of
    shadow corners, and a taskbar tool tip caught mid-fade); the
    compositor's fade and slide of a closing flyout frame by frame.
  - Boswas glass (ADR-0057): the black row in the Software Center's list
    reproduced on the headless compositor with a real pointer, and grey
    after; Installed apps on the Ocean, Classic and Pearl wallpapers,
    dark and light, at 175 % text, with transparency off and with the
    keyboard focus, with an icon growing (at rest, half-way, settled);
    other Control Center pages, dialogs and message boxes; Files,
    Calculator, Weather, Text Editor and Task Manager. A live tool tip was
    not caught.
  - The clipboard history and the emoji panel (ADR-0058), live with the
    real compositor and shell at 1920x1080 and 1366x768, at 100 and 175 %
    text, dark and light, with a search, and the card shown with the
    history off; the tests' pictures at 225 %; System > Clipboard.
  - The widgets board and the desktop icons (ADR-0059, ADR-0060), live
    with simulated services and a home with real Calendar and Clock files
    and a weather summary: every card, the weather on the taskbar's
    button, a card's menu, removing and adding cards; selecting by click,
    Ctrl and band, both menus, a new folder with the rename field, F2,
    Delete, copy and paste, a PDF opened by a double-click, icons moved
    with their places kept, View > Large icons, a file dropped from a
    PySide6 window; at 1920x1080 and 1366x768, 100, 175 and 225 % text,
    both schemes, dark and light wallpapers.
  - Control Center (ADR-0061): Display with Brightness, Power & battery,
    Ethernet and its IP dialog, USB, Activity history and Mouse pointer,
    whole windows in both schemes.
- The round of ADR-0062 and ADR-0063 (2026-11-26 and 27):
  - The shell's controls and shortcuts (ADR-0062), as pictures of the
    shell in both schemes at 100 and 200 %: every right-click menu,
    Start's application menu, the security options, the projection
    flyout with one and two monitors, the night light tile tinting and
    not available, the account picture in Start, on the lock screen and
    on the login screen, the level on the lock screen. Live on the
    headless compositor with the real shell, simulated services, injected
    input and logging stand-ins for the Boswas programs and `gsettings`:
    each menu entry and key started exactly the expected command (Super+X
    and Enter `boswas-control-center --page installed-apps`, the clock's
    menu `--page date-time`, the battery `--page power`, Ctrl+Alt+Del >
    Change a password `--page users --section your-account`, Super+U
    `--page accessibility`, the middle button and Super+Shift+4 the
    application's new-window action); `gsettings` was only read when the
    scheme already matched; Super+Shift+Up and Down on a `foot` window; a
    volume key on the real lock screen at 200 %; a `~/.face` picture on
    the lock screen and in Start.
  - Glass (ADR-0063): every glass surface of the shell and the
    compositor (the desktop, Start at rest and under the pointer, quick
    settings, notifications, a menu, a tooltip, the clipboard and emoji
    panels, the widgets board, the volume display, a toast, Alt+Tab, the
    window menu, the snap layouts, Task View, the quick-link menu, the
    projection flyout, the security screen, the lock screen and its
    sign-in view) on pixman, dark and light, at 100 and 200 %, and once
    each with transparency off and with the Carbon preset; live blur over
    a window of colour bands, nested on llvmpipe under Xvfb at 1280x800
    and 100 %, in both schemes. Contrast measured on those screenshots:
    see [Glass, measured](#glass-measured). An icon growing was caught
    nearly settled only (a screenshot is slower than the 150 ms).
- The second performance round (ADR-0064, 2026-11-26 and 27):
  screenshots of the builds before and after compared at 1920x1080 and
  125 % and at 2560x1440 and 200 % (the desktop, Start at rest, under
  the pointer and a page down, quick settings, notifications, a tooltip
  at rest): identical but for the clock and a taskbar tooltip caught
  mid-fade; the same shots against the tree with glass, at the same two
  sizes, identical but for the clock. The snipping selection differs on
  purpose: its frame is crisp and on the captured pixels (looked at side
  by side at both sizes). Task View at
  rest against the build before the single fading picture: identical at
  1920x1080 and 100 %, and but for the clock at 2560x1440 and 200 %;
  with reduced motion it opens and closes in one frame and ends as the
  animated one does. The magnifier still eases.
- The shell's surfaces under the headless compositor, with simulated
  services (debug builds contain a stand-in).
- A window's rounded lower corner in a nested run on OpenGL ES with
  Mesa's software driver.
- The desktop functions of ADR-0049, on the headless back end and nested
  on OpenGL ES with Mesa's software driver (llvmpipe): the taskbar centred
  and at the left, the accent colours, the taskbar hiding and coming
  back, Start at 175 % text, the snap layouts when a window is dragged to
  the top, the magnifier at 200 and 300 %, and all six colour filters.

- The desktop run from the built packages. The `.deb` files of the final
  build were unpacked (no maintainer script ran) and laid over `/usr` and
  `/etc` in a private mount namespace, so every program, data file and
  default was at its installed path. In it the packaged compositor ran on
  its headless back end (software rendering, no system services) as an
  unprivileged user, with the packaged shell and, started by their
  packaged launchers, all twelve system applications, Settings and Task
  Manager as Wayland clients: window frames, the taskbar with running
  marks, Start with the pinned applications, Task View with live previews,
  two windows snapped side by side, quick settings, in the dark and the
  light scheme; the Boswas-Cursors pointer; the presets listed in Settings
  by the packaged `boswas-preset`; the reminder service running for Clock.
  Every application was also looked at snapped to half of a 1600 pixel
  screen. That showed one defect, Calendar's toolbar drawn over itself at
  that width; it was corrected (the pane folds away, the toolbar takes a
  second row) and has tests now. Media Player and Camera were started with
  Qt's PulseAudio back end, see the note on Qt Multimedia below.
- Earlier, from the source tree: the same run with debug builds, whose
  shell contains stand-in services, so quick settings and the
  notification centre showed networks, devices and notifications.

Package builds: every package was built with `dpkg-buildpackage` and its
test suites in this build environment (an Ubuntu-based sandbox with the
1.85 Rust toolchain and build dependencies not checked, not the Debian 13
builder container), followed by lintian and the package test stage.
Result of the final run (2026-11-16, the tree with the pointer theme and
without the Plasma and Konsole data): the 13 device packages and the
Control Plane built with all their test suites (351 Rust tests in
`boswas-desktop`, 124 in `boswas-bootsplash`, 3,585 Python tests in the
other packages); lintian reported no errors or warnings; the package stage
passed 94 checks, failed none and skipped three for programs that are not
installed here (`apparmor_parser`, `desktop-file-validate`,
`update-desktop-database`); the static stage passed 72 checks; the unit
stage (`tests/unit/test_units.sh`) passed all 27 of its lines, the Rust
suites among them. After two late corrections (Calendar's narrow window,
and the directory of the default cursor theme's link in
`boswas-branding`) the two packages were rebuilt with their tests and the
package stage and lintian were run again on the final set, with the same
result. The builder container and `./test.sh` on Debian 13 still have to
confirm it.

The run after the boot corrections of ADR-0044 (2026-11-16, version
3.0~alpha1): the 13 device packages and the Control Plane built with all
their test suites (377 Rust tests in `boswas-desktop`, 151 in
`boswas-bootsplash`, 3,636 Python tests in the other packages); lintian
reported no errors or warnings; the package stage passed 100 checks,
failed none and skipped the same three; the static stage passed 81
checks. `boswas-desktop` was built twice more after late changes to its
session scripts (the compositor's messages go to the journal; the lines
shown with the text login fit a console), each time with its tests, and
lintian and the package stage were repeated on the final set. The boots
listed under "Seen on the final packages" ran on the build before the
change of wording; the `nomodeset` boot was repeated after it.

The run after the Control Center work of ADR-0049 (2026-11-22, version
3.0~alpha1, the same build environment): the 13 device packages and the
Control Plane built with all their test suites (428 Rust tests in
`boswas-desktop`, 154 in `boswas-bootsplash`, 3,929 Python tests in the
other device packages, 50 in the Control Plane; none failed); lintian
reported no errors or warnings; the package stage passed 100 checks,
failed none and skipped the same three; the static stage passed 82
checks. The unit stage and the boot in the virtual machine were not
repeated for this run. The first attempt stopped at `boswas-branding`:
its tests read the boot menu's theme, which the package build did not
copy; `build-packages.sh` now copies `config/live-build` too.

The run after the new boot screen (ADR-0050) and the lock screen's
weather (ADR-0051) (2026-11-22, version 3.0~alpha1, the same build
environment): the 13 device packages and the Control Plane built with all
their test suites (439 Rust tests in `boswas-desktop` with two ignored,
187 in `boswas-bootsplash`, 3,968 Python tests in the other device
packages, 50 in the Control Plane; none failed); lintian reported no
errors or warnings; the package stage passed 101 checks (the new one: the
weather update's user units and the postinst that enables the timer),
failed none and skipped the same three; the static stage passed 82
checks. The unit stage and the boot in the virtual machine were not
repeated for this run, so the new boot screen has not yet been seen on a
display.

The run after the icons, Start, opening files and the recommended scale
(ADR-0052 to ADR-0055) (2026-11-23, version 3.0~alpha1, the same build
environment): the 13 device packages and the Control Plane built with all
their test suites (458 Rust tests in `boswas-desktop` with three ignored,
187 in `boswas-bootsplash`, 3,980 Python tests in the other device
packages, 50 in the Control Plane; none failed); lintian reported no
errors or warnings; the package stage passed 103 checks (the two new
ones: boswas-desktop depends on xdg-utils, and boswas-apps installs
`/usr/share/applications/mimeapps.list` with entries the image has),
failed none and skipped the same three; the static stage passed 82
checks. After the build one guard was added to the recommended scale (an
aspect ratio stored as the size counts as unknown on built-in panels too);
the compositor's 123 unit tests were run again with it, the package was
not rebuilt. The unit stage and the boot in the virtual machine were not
repeated.

The run after the work of ADR-0056 to ADR-0064 (2026-11-27, version
3.0~alpha1, the same build environment, with Rust 1.85 as Debian 13 has
it, on the merged tree 4b3b232): the 13 device packages and the Control
Plane built with all their test suites (635 Rust tests in
`boswas-desktop` with six ignored, 187 in `boswas-bootsplash`, 4,066
Python tests in the other device packages, 50 in the Control Plane; none
failed); lintian reported no errors or warnings; the package stage passed
104 checks (the new one: boswas-desktop depends on `libglib2.0-bin`,
`dconf-gsettings-backend` and `gsettings-desktop-schemas`), failed none
and skipped the same three; the static stage passed 82 checks. Before
the build, `cargo check --locked --workspace --all-targets` with Rust
1.85 gave no warning in either Rust package. After it, two fixes went in
(08bafcd: the spacers counted again once live blur is in, and the drawn
background following the scheme, each with a test), and `boswas-desktop`
was built again with them: 637 Rust tests passed, none failed, six
ignored; with that package the package stage again passed 104 checks and
lintian reported nothing. The unit stage and the boot in the virtual
machine were not repeated.

Qt Multimedia in this environment: the sandbox has PySide6 6.12, whose
audio back end is PipeWire, and `libpipewire` without its configuration
files. Creating an audio output then crashes inside Qt, which is why the
Clock's chime was not heard and why Media Player and Camera were started
with `QT_AUDIO_BACKEND=pulseaudio` for the run above (they start and show
"no camera" and "nothing played" without a sound server). Debian 13 has
Qt 6.8, whose audio back end is PulseAudio, and the image installs
PipeWire with its configuration; the crash has not been tried there.

## Booted in a virtual machine

After the boot failure of 2026-11-16 the built packages were booted, and
every correction of ADR-0044 was checked by a boot where one was possible.

**What the machine is.** QEMU without hardware acceleration, standard VGA
(the `bochs` driver) or the firmware frame buffer (`simpledrm`), 2 GB of
memory, keyboard input through the monitor. The root file system is
Ubuntu 24.04 (kernel 6.8, systemd 255, greetd 0.9.0, libseat 0.8.0,
Mesa's llvmpipe), because no Debian mirror can be reached from the build
environment. The `.deb` files are installed with apt, which resolves
their dependencies from that archive; the initramfs is made by that
system's initramfs-tools. It is not the image: there is no live-build,
no boot loader (the kernel is started directly), no encrypted disk, no
AppArmor profile set of the image, and none of the image's other
packages.

**Seen on the final packages** (screenshots and the serial log of each
boot were read):

- **A plain boot** (`quiet splash`): the boot screen (the mark and the
  ring) from the initramfs, an empty console for a moment, then the login
  screen with the clock. Signing in as the test user by keyboard opened
  the desktop (wallpaper, taskbar, clock). No unit failed; the messages of
  both compositors were in the journal and none on the console.
- **The live session**, with the image's live-config component and
  settings file run at boot the way live-config runs them
  (`username=boswas`): the live user was signed in and the desktop
  appeared without a login.
- **The same with a user that does not exist** (`username=ghost`): the
  component wrote no automatic sign-in and said so; the login screen
  appeared.
- **greetd told to sign in a user that does not exist** (the state of the
  image before the correction): greetd failed five times with
  `pam_acct_mgmt: AUTH_ERR`, `boswas-login-fallback.service` started
  getty, and `login:` was on the first console.
- **The firmware frame buffer** (`vga=0x318`, the `bochs` driver kept
  from loading): the boot screen complete at 1024x768, then the complete
  login screen, drawn in software.
- **`nomodeset`**: the compositor ended with status 3 after its 10 second
  wait ("no graphics device appeared ... `nomodeset` on the kernel command
  line turns them all off" in the journal), and greetd's text login was
  on screen with the line that says so.

**Seen during the work, on earlier builds of the same day** (the code
paths did not change afterwards, but these boots were not repeated on the
final packages):

- Without `polkitd` (removed by hand): logind refuses to activate the
  session, the compositor ends with status 3 after about 4.5 seconds and
  says so, the text login appears.
- Without the font configuration (`fontconfig-config` removed by hand):
  the login screen appears, with text in Inter.
- With the shell's program moved away: status 4 after three tries, the
  text login with the line "the graphical login could not start
  (status 4)".
- A graphics driver that is loaded four seconds after the compositor
  started; the firmware frame buffer replaced by the driver before the
  compositor started, and under a running desktop with a window open (the
  window, wallpaper and taskbar are back on the new device).
- A greetd that fails five times: `boswas-login-fallback.service` starts
  getty, and `login:` is on the first console.
- The boot screen: the whole picture after `vga=0x318`; the graphics
  device replaced while it runs inside its sandbox (the supervisor hands
  the new one over); a second start while a boot screen is stopped; the
  hand-over ending a boot screen that does not answer; Escape after the
  switch to the real root; the worker killed; a driver that loads late;
  no display at all (the unit waits 9 seconds once, then the text login);
  emergency mode (text mode and systemd's message on screen).

**Not shown by these boots:** the boot screen as it is drawn since
ADR-0050 (they ran the previous picture: a bitmap of the mark and a ring
of dots; the device layers and the start-up did not change); anything
about Debian 13 (its initramfs-tools, on which the hook that copies the
graphics drivers has never run; its versions of greetd and libseat; its
package names); the image and its live session as built (the live-config
component ran in a stand-in for live-config, not under it); the boot
loader; real graphics hardware and hardware rendering; a login screen with
a graphics device and no connected monitor; the boot screen handing over
to a real compositor that takes the display (a stand-in program held the
console); rescue mode; a desktop shell that keeps failing (tests only);
the passphrase prompt of an encrypted disk.

## Snipping Tool on the real desktop

On 2026-11-21 (ADR-0046) the Snipping Tool ran against the real
compositor and shell on the headless back end, as an ordinary user, as a
Wayland client, with input injected through the control socket: a
full-screen snip and a rectangle snip (500 x 320, the area that was
dragged) were saved by the shell and opened in the editor; the tool's
own window was not in the picture; the capture screen kept the keyboard
while the window was back underneath; a pen stroke followed the injected
pointer; Ctrl+N with unsaved marks asked first; a snip confirmed after
eight seconds opened; a snip cancelled with Escape ended in "Snip
cancelled" with the previous picture back once the pointer reached the
window. Not tried there: the delay, Save as with the real file chooser,
the clipboard, Open in Photos.

## Frame cost, measured

Measured on 2026-11-21 for ADR-0045 with the compositor's frame
statistics, the real shell and a terminal (`foot`), input injected
through the control socket at 120 Hz, three seconds per case, on the
build machine (4 cores, no GPU). CPU time is that of the compositor
process, its rendering threads included.

| Case | Before | After |
|------|--------|-------|
| Pointer crossing between a window and the desktop (OpenGL ES in software) | 3 full-screen redraws a second, 0.58 s CPU | none, 0.21 s CPU |
| Window drag, CPU per frame: OpenGL ES in software / pixman | about 19 ms / 1.4 ms | about 17 ms / 1.4 ms (pixman is now used on such machines) |
| Task View, CPU per frame: OpenGL ES in software / pixman | about 60 ms / 6 ms | about 48 ms / 6 ms |
| Start opened and closed twice: shell CPU | 0.72-0.81 s | 0.24-0.26 s |
| Hovering over Start's items: shell CPU | 0.40 s | 0.19 s |

In the virtual machine of ADR-0044 (QEMU without hardware acceleration,
standard VGA, 1280x800), with the new compositor and shell: a window
drag ran at one or two frames per second with OpenGL ES in software and
at 57 frames per second, about 11 ms each, with pixman; pointer motion at
57 frames per second with either. Before frames were paced, a terminal
redrawing drove the compositor to 198 frames per second there.

The built `boswas-desktop` package of that change (390 Rust tests,
lintian clean, the package stage passing 100 checks) was booted there on
standard VGA and on the firmware frame buffer (`vga=0x318`, simpledrm):
the log said "drawing with pixman (software)" for the login screen and
for the session, and a keyboard sign-in opened the desktop.

Not measured: any real GPU, where only the shell, opaque-region and
pacing changes apply, and a hypervisor with 3D acceleration.

Measured on 2026-11-26 for ADR-0056 with the per-frame statistics
(`boswas_compositor::stats=trace`, `boswas_shell::stats=debug`): release
builds, the headless compositor with the real shell and two `foot`
windows, input injected through the control socket, at 1920x1080 and
2560x1440, scales 1, 1.25, 1.5 and 2, at 60 and 144 Hz (16
configurations), the builds before and after run back to back in each.
A single worst frame can be the scheduler of the shared 4-core machine,
so the 99th percentile is the figure to trust. A frame is late when it
came more than 1.5 refreshes after the one before while something moved.
Pooled over the 16 configurations, in ms (median / 95th / 99th
percentile / worst):

| Case | Before | After |
|------|--------|-------|
| Start opened and closed, shell | 8.7 / 21.5 / 217 / 382; 69 % of the frames late | 0.0 / 2.3 / 10.2 / 21.0; 9 % late |
| Hovering in Start, shell | 9.3 / 17.4 / 24.7 / 32.9 | 0.0 / 2.4 / 8.3 / 13.2 |
| Scrolling Start with the wheel, shell | 9.3 / 18.5 / 26.7 / 37.7; 68 % late | 0.0 / 12.0 / 16.0 / 21.4; 11 % late |
| Quick settings, shell | 99th 57, worst 136 | 99th 3.9, worst 6.7 |
| Notification centre, shell | 99th 114, worst 190 | 99th 7.3, worst 12.8 |
| Alt+Tab, compositor | 11.4 / 159 / 247 / 258 | 2.1 / 33 / 44 / 45 |
| Task View, compositor | 99th 26.9 | 99th 23.0 (unchanged within noise) |

At 2560x1440, 200 % and 144 Hz the first opening of Start took 375 ms
before and 17 ms after, a step of Alt+Tab 18.6 and 2.2 ms; at 60 Hz the
median time from a hover to its commit went from 12.7 to 1.3 ms. Not
changed: the snipping overlay (about 50 ms a frame at 200 %), the lock
screen's first frame (90 to 240 ms), Task View's opening frame (30 to
50 ms); ADR-0064 took them on (below). Memory at 2560x1440 and 200 %: the
shell 127 MB after the start (72 before) and 156 MB after opening the
flyouts (145); the compositor about 14 MB more. Not measured: a real GPU,
a real vertical blank, the DRM and nested back ends, more than one
output.

Measured on 2026-11-26 and 27 for ADR-0064 with the same set-up and
statistics, the builds before and after run back to back: release builds
of the tree before that round (with ADR-0056 to ADR-0061) and of the
finished round (with glass, ADR-0063), pooled over 1920x1080 at 100 % and
2560x1440 at 200 %, each at 60 and 144 Hz. In ms (median / 95th / 99th
percentile / worst), frames over 6.9 ms (the budget at 144 Hz), and
late frames (more than 1.5 refreshes after the one before) among the
frames drawn while something moved:

| Case | Before | After |
|------|--------|-------|
| Start opened and closed, compositor | 4.3 / 9.6 / 14.4 / 22.3; 113 over 6.9; 68 of 516 late | 3.3 / 5.7 / 8.3 / 10.8; 9 over 6.9; 10 of 580 late |
| Start opened and closed, shell | 0.0 / 3.2 / 12.1 / 26.7 | 0.0 / 2.6 / 6.6 / 14.0 |
| Hovering in Start, shell; compositor | 0.0 / 3.4 / 10.9 / 11.7; 4.5 / 7.4 at the 95th / 99th | 0.6 / 1.5 / 4.0 / 10.1; 3.4 / 4.9 |
| The flyouts in turn, compositor | 2.6 / 8.6 / 13.0 / 24.5; 129 over 6.9; 79 of 1,249 late | 1.8 / 4.7 / 5.9 / 11.0; 6 over 6.9; 12 of 1,316 late |
| Quick settings, notification centre, compositor (95th / 99th / worst) | 1.9 / 4.1 / 18.3; 3.9 / 5.8 / 14.1 | 1.4 / 2.1 / 5.9; 2.7 / 4.0 / 4.9 |
| Hovering on the taskbar, shell | 1.7 / 3.3 / 3.7 / 4.2 (with glass, before the round: 1.5 / 2.8 / 3.8 / 4.0) | 0.7 / 1.6 / 1.9 / 2.0 |
| Dragging a snipping selection, shell | 24.5 / 43.1 / 45.6 / 66.3; 367 drawings over 16.7 ms; 118 of 1,139 late | 0.5 / 1.3 / 2.7 / 19.6; 3 over 16.7 ms; 7 of 1,234 late |
| A toast, shell (95th / 99th) | 57.0 / 57.2 | 13.0 / 15.2 |
| Task View, compositor | 0.2 / 10.2 / 29.4 / 73.6; 16 over 16.7 ms | 0.3 / 11.7 / 19.7 / 52.2; 11 over 16.7 ms |
| A change of theme, shell (99th, light and dark) | 28.4 and 26.4 | 29.7 and 37.1 |
| Dragging, resizing, snapping a window | | about the same |

Task View per configuration (compositor, 95th / 99th percentile / worst):
at 1920x1080 and 144 Hz 6.1 / 9.4 / 28.6 before and 6.4 / 8.8 / 49.2
after (within the budget at the 95th percentile); at 2560x1440, 200 % and
144 Hz 10.9 / 32.9 / 48.2 and 12.0 / 19.3 / 51.2. Its first frame on the
first opening is 61 ms at 2560x1440 and 50 ms at 1920x1080 (48 and 23 to
28 before), while the darkened picture is made; a second opening's first
frames are 12 to 13 ms at 2560x1440 (32 to 34 before). With glass but
before the single fading picture its 95th percentile was 15.2 ms, and
before the blurred desktop went in ARGB order 55.9.

The lock screen, from the lock request to its first frame on screen, two
runs per configuration: 142 to 172 ms before and 72 to 94 after at
1920x1080, 207 to 301 and 107 to 117 at 2560x1440 and 200 %. The
preparation of Start, quick settings and the notification centre ran in
one block of 84 to 310 ms before; after, in 24 to 31 slices of at most
2 to 4.5 ms. Measured on the first part of the work (five configurations,
with 1920x1080 at 125 % too): the capture view's first frame after Print,
the shell's 95th percentile, 57 → 13 ms.

Memory at 2560x1440 and 200 %: the shell 101 MB after the start and
119 MB after opening the flyouts, against 117.6 and 133.8 MB without the
mmap threshold, with the same frame times (opening Start, compositor 95th
percentile 5.6 to 5.7 ms with it, 5.1 to 5.4 without); the compositor
148 MB after Task View (117 without the single fading picture) and
134 MB once that is let go. The single slowest frames are the scheduler
of the shared machine more often than the code; the 99th percentile is
the figure to trust. Not measured: a real GPU or OpenGL ES, the DRM back
end, more than one output, the time from input to commit during the
preparation, the three-minute releases end to end.

## Glass, measured

For ADR-0063, on the build machine (4 cores, no GPU). **Contrast** of
the worst muted text over glass, measured on screenshots as the darkest
1 % of the background, grain included (4.5:1 is the rule):

| Run | Start | Quick settings | Menu | Taskbar |
|-----|-------|----------------|------|---------|
| pixman, dark, 100 % and 200 % | 6.73 to 6.98 | 7.28 to 7.86 | 7.05 to 7.24 | 6.90 to 7.90 |
| pixman, light, 100 % (Pearl) and 200 % (Horizon) | 4.63 to 4.85 | 4.63 to 4.82 | 5.47 to 5.48 | 4.38 to 4.55 |
| live blur, light, colour bands behind | 4.55 | 4.59 | 5.38 | 4.40 |
| live blur, dark, colour bands behind | 6.45 or more | | | |

The taskbar shows no muted text (its clock is above 13:1); body text is
above 4.6:1 everywhere (secondary text 5.5 to 10, text 9 to 16). The
measurement found two faults, both corrected: the band did not lift
colours of middle brightness, and a panel's own shadow showed through
translucent glass.

**Cost** on pixman, with the measuring set-up of ADR-0056 (the builds
before and after run back to back), pooled over 1920x1080 at 100 % and
2560x1440 at 200 %, each at 60 and 144 Hz; the shell's frames in ms (95th
/ 99th percentile / worst):

| Case | Before | With glass |
|------|--------|------------|
| Start opened and closed | 3.0 / 16.6 / 19.3 | 6.9 / 13.9 / 26.5 |
| Hovering in Start | 2.9 / 11.7 / 12.2 | 6.7 / 9.6 / 18.9 (680 drawings where 205, for the magnifier; 112 late frames where 161) |
| Quick settings | 2.6 / 4.2 / 5.3 | 5.0 / 10.5 / 11.9 |
| Notification centre | 3.3 / 9.4 / 12.5 | 5.4 / 8.6 / 9.5 |
| Hovering on the taskbar | median 1.6, 99th 3.5 | median 1.5, 99th 4.1 |

The compositor's frames did not change (Start opening, 99th percentile
14.4 and 15.5 ms). An earlier build of the glass was clearly slower; two
corrections (the glow shades only its own rows, in tiles aligned to
device pixels, and kept pictures are laid on only where something is
drawn again) brought a taskbar hover drawn alone in release, at 5120x96
pixels, from 4.3 to 1.4 ms. End to end at 2560x1440, 200 % and 144 Hz
the taskbar's partial redraws are still slower than before glass (median
4.3 where 2.8 ms, over two pairs of runs); the cause was not found.
After ADR-0064 a taskbar hover costs the shell less than before glass
(see [Frame cost, measured](#frame-cost-measured)). Live blur's cost was
not measured: llvmpipe says nothing about a GPU.

## Not verified

Apart from the boots above, nothing below could be exercised in the build
environment.

| Area | Not verified |
|------|--------------|
| Image | The ISO build with this manifest; whether Debian trixie resolves its dependencies and has every package under the name used (`xwayland`, `xdg-desktop-portal-wlr`, `pipewire-bin`, `wireplumber`, `upower`, `power-profiles-daemon`, `accountsservice`, the PySide6 modules); the verify hook; the QEMU boot test, whose checks were rewritten for the native desktop and have never run |
| Graphics hardware | The `udev` back end on real hardware. The drawing of ADR-0056 on a GPU: partial uploads of the kept buffers on OpenGL ES, the compositor's fades through `wp_alpha_modifier` at a real vertical blank. In the virtual machine it drove one output with software rendering; not seen anywhere: a mode change, rotation, several monitors, monitor hotplug, turning screens off, the gamma ramp of night light, OpenGL ES on a hardware driver (the clip shader, damage, direct scan-out), a pointing device. The recommended scale (ADR-0055) from a real monitor's EDID and the DRM driver's name read on a device (the virtual machine's boots ran before it); only the headless back end with a given size was tried. Live blur (ADR-0063) on a GPU, on the DRM back end, at scales other than 100 %, on several or rotated outputs, and what it costs there; the check for a gamma ramp that makes night light available, and Super+P switching real monitors (Duplicate, Second screen only and several monitors never ran: the headless back end has one) (ADR-0062). The spacers that keep the windows' places, the single-pixel buffers and Task View's single fading picture on OpenGL ES and on a GPU (only pixman was measured; the picture's likeness on OpenGL ES was tested on llvmpipe) (ADR-0064) |
| Input hardware | libinput devices and their settings, touch screens, tablets and pens, touchpad gestures, keyboard LEDs; the shortcuts of ADR-0058 and ADR-0059 (Super+V, Super+., Super+;, Super+W) and the paste's key lookup on a real keyboard and with other layouts (keys were injected through the control socket, with the US layout); dragging desktop icons with a real pointing device (ADR-0060); the shortcuts of ADR-0062 on a real keyboard, the volume and brightness keys on the lock and login screens with real devices, the Num Lock light, and Ctrl+Alt+Del through a real program that takes the shortcuts (unit tests only) |
| Icons | The order in which KDE's `KIconLoader` looks icons up (ADR-0052 assumes it searches `hicolor` last, as Qt's loader does); Qt's loader was checked with Qt 6.12, not with Debian 13's Qt 6.8 |
| Pointer theme | Boswas-Cursors on a hardware cursor plane, at a fractional scale or on a HiDPI display; Xwayland, GTK and Qt clients picking it up in a real session; the `x-cursor-theme` alternative on a Debian 13 image (ADR-0043) |
| Login and lock | The greetd login, the text-login fallbacks and the automatic login on Debian 13 and on the image (they were booted on another base system, see above); PAM on the image; the live session under the real live-config; suspend and resume; `loginctl lock-session` against the real logind; unlocking with the weather card shown, under real PAM (ADR-0051); the account picture from a real AccountsService on the login screen (ADR-0062); for the background the lock screen takes from the desktop (ADR-0064), the file's mode and the refusal of a link are in the code and not tested, and the lock screen's sign-in view was not timed |
| Boot screen | On hardware: DRM and frame-buffer output and the hand-over from the firmware frame buffer (seen in the virtual machine only). The initramfs hook and scripts under Debian 13's initramfs-tools, and the size of the initramfs there; a legacy frame buffer (`efifb`, `vesafb`: the test kernel has neither, and a frame buffer device emulated by a DRM driver showed nothing of what was drawn into it); a real LUKS unlock; Caps Lock on a real console; the animation in motion on a panel. The picture of ADR-0050 on any display or in the virtual machine (only still frames and a preview made from them); the cost of its intro on a slow machine with a large, fast display (0.45 s of the build machine's processor for the 1.6 s intro at 3840x2160 and 240 frames a second); its background on a 16-bit (RGB565) frame buffer, where it is not dithered |
| System services | NetworkManager, BlueZ, PipeWire (`pw-dump`, `wpctl`), UPower, power-profiles-daemon, logind and polkit: every backend has run against stand-ins only. The polkit helper protocol, the shape of `pw-dump`'s JSON and NetworkManager's settings dictionaries were written without a real daemon to compare with |
| Authentication | A `pkexec` call answered by the shell's agent. Until that has run, no administrator action from the desktop is known to work |
| Screen capture | Screen sharing and screenshots through `xdg-desktop-portal-wlr`; capture of a rotated monitor end to end; what the consent dialog names for the portal back end |
| X11 | Xwayland on hardware; the clipboard between X11 and Wayland applications, and the clipboard history and the emoji panel with X11 programs (ADR-0058); Wine's Wayland driver on Debian 13 (the sandbox gives a Windows application the Wayland socket and no X11 socket, ADR-0034) |
| Input methods | No input method has been run on the compositor; none is in the image |
| Applications on the desktop | No run of Firefox, Wine or Waydroid on the native compositor is recorded, and no real GTK, Qt or Electron program has read what the clipboard history or the emoji panel pasted, or the clipboard they gave back; no real password manager's copy was offered to the history (ADR-0058). Boswas glass (ADR-0057) has not been seen at a scale above 1, with a live tool tip, or while transparency, the wallpaper or a preset changed in a running session. Two windows of one application in a real session, and the desktop icons' clipboard and drag and drop with a real Files (ADR-0060, ADR-0061), were not tried; and Firefox ESR's own icon (ADR-0052) has not been seen there: the icon tests use stand-ins. Opening a screenshot ran with stand-ins for Photos and Files (ADR-0054); the shipped `mimeapps.list` has not yet been checked at its installed path by a package build. Weather has never reached the real Open-Meteo service (the build environment cannot). Weather's update for the lock screen has not run under a real systemd user manager (its sandboxing options need unprivileged user namespaces there, which Debian 13 enables by default), and that `boswas-session.target` starts its timer is not shown; nor locales where glibc and Qt choose differently between the 12- and the 24-hour clock (ADR-0051). The Boswas applications and Control Center have not been started in a real session on hardware (window activation, the primary selection, drag and drop between programs, an input method's window); see "Looked at by hand" for the headless run from the built packages. Applying a preset from Settings in a running session was not tried (since ADR-0063 it also turns the shell's and the compositor's glass off for Carbon; the path, `boswas-preset apply` writing its record and then sending `reload-config`, was read, not run). GTK programs and Firefox following the dark mode and accent the shell writes to GSettings: only a stand-in `gsettings` ran, and whether Debian 13's GTK portal passes `accent-color` on is not known (ADR-0062). The Qt applications do not ask for live blur yet; XWayland windows asking for it were not tried (ADR-0063). Resizing and scrolling Control Center and Files with glass on a running compositor: only the toolkit's backdrop store was timed (ADR-0064) |
| Applications and devices | See the next section: a camera, removable drives, sound, real documents. Printing was tried to a PDF file only, and the image has no print service (CUPS is not in the manifest); Control Center's "Install printing support" has not run. Since ADR-0061 it installs `cups` without what it only recommends, so `cups-browsed` is not asked for; that `cups-browsed` is only a recommendation was checked in Ubuntu 24.04's metadata, not Debian 13's, and without `avahi-daemon`, which it then no longer brings, CUPS discovers no network printers. Ejecting a USB drive from Settings (`udisksctl`) has not met a real drive or UDisks2 |
| Settings | Every section that talks to a system service ran against a stand-in: the real bridge reported "not available" here. Arranging several monitors is tested against a model only (the headless back end has one output and ignores a scale change on reload). `timedatectl` and `localectl` changes behind polkit. The pages of ADR-0049 against the real NetworkManager (VPN, adapters), CUPS, logind (advanced start-up), systemd-hostnamed (renaming), the Flatpak permission store and fontconfig; the output formats of `nmcli`, `lpstat`, `fc-list`, `dpkg-query` and `flatpak permission-list`, written from their documentation, and the terse output of `nmcli` for the Ethernet section of ADR-0061 (also whether `ipv6.dns` and `ipv6.ignore-auto-dns` are accepted with IPv6 turned off); the pointer size passed to a real systemd user manager and D-Bus activation environment; some package names of Optional features (`gstreamer1.0-plugins-ugly`, `gstreamer1.0-libav`, `printer-driver-cups-pdf`, `fonts-noto-cjk`, `fonts-crosextra-carlito`, `fonts-crosextra-caladea`, `build-essential`, `firefox-esr-l10n-*`, `cups`) against the Debian archive |
| Performance | Frame times on a real GPU, and start-up time of the compositor and the shell. Frame costs and memory were measured on the build machine without a GPU and in the virtual machine only (ADR-0045, ADR-0056, ADR-0063, ADR-0064, above); the cost of live blur was not measured at all. For ADR-0064: the time from input to commit while the preparation runs, the release of spare canvases and of Task View's picture three minutes on, and more than one output |
| Accessibility and review | No screen reader interface exists for the shell or for what the compositor draws. The colour-filter pass and the magnifier through the DRM back end are compiled only; sticky and filter keys have unit tests and have not run in the real compositor; not every shell surface was looked at with 225 % text (Start of ADR-0053 neither). Start's smooth scrolling with a notched wheel on the live compositor (injected scrolling only). No external security review and no design review have taken place |

## The system applications and the settings, one by one

From the builders' reports. "Left out" is what the application does not
do on purpose or not yet; "not verified" is what could not be tried here.

| Application | Left out | Not verified |
|-------------|----------|--------------|
| Files | Thumbnails for videos and PDFs; password-protected ZIP archives; view settings per folder; special handling of hard links and sparse files | A real udisks service with its polkit question (mount, unlock, eject); notification of mount changes (a 15 second poll is the fallback); real drag gestures (the drop logic is tested) |
| Terminal | Split panes; screen-reader access to the text; the kitty keyboard protocol; sixel; an audible bell; blinking text; shaping across cells (Indic clusters are drawn cell by cell). The question before closing a busy tab does not appear for a program started with `-e` | A real Wayland session (activation, primary selection, an input method's window); a run as an ordinary user; automatic scrolling while dragging; the Hack font (rendered in DejaVu Sans Mono here). Tried with real bash, vim, tmux, less and top in the window |
| Text Editor | Syntax highlighting; spell checking; tear-off tabs; the page set-up is not remembered; saving by rename leaves other hard links with the old content | Started through its launcher (the environment is root) |
| Calculator | Graphing; currency conversion; a settings page; memory and history are not kept between runs | Its 739 expected results were written from the documented behaviour of the Windows calculator, not compared with a running one |
| Photos | XMP and IPTC data are lost on save; an edit of a picture above 48 megapixels is saved reduced; crop handles need a pointer; AVIF, HEIF and JPEG XL need `kimageformat6-plugins`; the gallery sorts by file time, not by the date in the picture | A real printer; "Set as desktop background" against the running compositor (since ADR-0061 it writes `appearance.toml`, and Control Center's reader of that file was shown to read the result) and "Show in Files" against the running file manager (tested through replaced hooks); animated WebP; a pinch on a touch screen |
| Document Viewer | PDF only; no annotations (Qt PDF cannot write); no selection across pages; open tabs are not restored | Only PDFs written by the tests were opened: no real-world file with pictures, CJK fonts, AES encryption or forms. Ran on one version of PySide6 (6.12) |
| Archive Manager | No encrypted archives are created; nothing is renamed inside an archive; entries cannot be dragged out; each start is its own window | 7z through `7z` 23.01 only (Debian 13 has 25.01); RAR with no real file |
| Media Player | No tags for Matroska, AVI, WMA and AIFF; no MPRIS track list; the mini player does not stay on top | Audible output, switching the output device, network streams and files with several tracks ran against a fake back end only. Real Qt Multimedia decoded mp3, flac, ogg, opus, wav, m4a, mp4, mkv, webm, mov and avi without a sound device. Video at 4K was not measured |
| Camera | Flash, exposure, focus and zoom appear only when the camera reports them | Everything: there is no camera here. Capture, recording, pause, unplugging and the error texts ran against a fake camera |
| Task Manager | No network column per process; no efficiency mode; no way to disconnect a session. A process that used almost no CPU shows a change of its memory or disk use up to 8 seconds late | A real Wayland session; a real GPU, battery and Wi-Fi adapter (fake sysfs in the tests); the polkit question when a service is started or stopped; "Open file location" against the running Files. Tried live against a real systemd user manager and a real logind on a private bus; its CPU figures were compared with `top` |
| Clock | No choice of alarm sound | The chime (Qt Multimedia crashes in this environment, see the note on Qt Multimedia above); logind's wake-up signal |
| Calendar | No accounts and no synchronisation; no printing; resizing by dragging only in the week and day views; it does not notice when another program changes its file; events of a hidden calendar do not remind | Reminders against the real notification service of the shell (tested against a stand-in on a private bus) |
| Control Center, settings | Editing a proxy; connecting a VPN that asks for a secret each time; a test sound; an automatic suspend time; identifying monitors; a pointer theme or colour; drawing tablets; more than four keyboard layouts; locking an account; setting the default printer; pausing updates; resetting the device; notifications on the lock screen; contrast themes; underlined access keys; an accent from the wallpaper; functions that need an online service or a phone (ADR-0049); manual IPv6 addresses and DNS over HTTPS on the Ethernet page (ADR-0061). Passwords set for other users are SHA-512 crypt, not Debian 13's default scheme, and only the length and character-class rules of `pwquality` are applied to them | See the table above. For the Users page: the shape of `busctl`'s replies, the conversation with `passwd` under real PAM modules, polkit's questions through the shell's agent, signing in as a user it created, removing a user with and without the files, the picture on the login screen |

Icons: the applications ask for some symbolic icons the Boswas Prism pack
does not draw (the reports name them). Ten were added; the others fall
back to a similar icon or to a text button.

## Known not to work, or left out

Limits that are decisions or known gaps, not untested paths. Each has its
ADR.

- Live blur on OpenGL ES only: with pixman the glass of the shell and the
  compositor shows the blurred wallpaper (ADR-0063), and the Boswas glass
  of the Qt applications is made from the desktop background, as a
  window of its size centred on the screen would cover it (ADR-0057);
  the toolkit does not ask for live blur yet.
- X11 windows are drawn at scale 1 and enlarged on a scaled monitor,
  which since the recommended scale (ADR-0055) is the default on many
  laptops; X11 applications can observe each other (ADR-0034).
- Bluetooth devices that need a PIN or passkey cannot be paired from the
  shell; enterprise Wi-Fi (802.1X) cannot be set up there; tray icons of
  the older X11 kind are not shown (ADR-0035).
- The screen-capture question names a process name that a program can
  choose, and allows everything or nothing (ADR-0037).
- Screen sharing through the portal shares the first monitor; there is no
  chooser (ADR-0037).
- Outputs on a second GPU, HDR and the NVIDIA proprietary driver are not
  supported (ADR-0036). Variable refresh rate exists (ADR-0048) and has not
  run on a real monitor.
- The boot screen's text is ASCII and English (ADR-0038). On a 16-bit
  frame buffer its background is not dithered and may show bands
  (ADR-0050).
- The weather on the lock screen (ADR-0051): the hours are labelled in the
  place's local time; `XDG_STATE_HOME` set only in `~/.profile` is not
  seen by the systemd user manager, so the update then writes where the
  lock screen does not look; for a user who never opened Weather the
  update's unit creates two empty directories (nothing is sent); a
  configuration file with a mistake that the compositor rejects can make
  Weather and the compositor disagree about `[lock] show_weather`, at
  worst leaving the card off.
- With `nomodeset` there is no KMS device: the desktop does not start and
  the text login is shown. The boot screen does not draw on a 24-bit
  legacy frame buffer (ADR-0044).
- Between the boot screen and the login screen the display is an empty
  console for the moment the compositor needs to start (ADR-0044).
- The shortcuts are fixed; there is no shortcut editor.
- The lock screen appears 72 to 117 ms after the lock request, not yet
  within one or two frames; on pixman Task View at 2560x1440, 200 % and
  144 Hz is about 1 ms a frame over the budget, and its first opening
  after the start or a change of wallpaper is slower than before glass
  (ADR-0064).
- Super+K (Cast) does nothing; the Ctrl+Alt+Del screen has no Switch
  user (greetd cannot keep a session while another person signs in);
  Uninstall in Start's menus opens Installed apps, not the application
  (ADR-0062).
- Desktop icons are on the primary monitor only and cannot be dragged
  into other programs; an item on another drive cannot be moved to the
  Trash (ADR-0060). The widgets board shows no online feeds (ADR-0059).
- No contrast themes for the shell; colour filters need OpenGL ES and are
  not available where the compositor draws with pixman (ADR-0049).
- A VPN that asks for a password or another secret when it connects
  cannot be connected from the desktop: there is no secret agent for
  NetworkManager (ADR-0049).
- There is no office suite and no mail client in the image, and the
  Document Viewer reads PDF only (ADR-0040).
- There is no print service in the image: the applications' Print
  dialogs can print to a PDF file only.
