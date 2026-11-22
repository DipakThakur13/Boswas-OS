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
| `boswas-ui` (Rust) | 34 | The token file as the programs read it, drawing, text (also without the font configuration, and without any font), icons, the materials, the contrast rules for every accent colour in both schemes |
| `boswas-compositor`, unit | 118 | The exit statuses, when a shell that ended is started again and when the login screen is given up, the messages for a missing seat or graphics device, the configuration layers and their rules (a user cannot relax the lock policy, name a trusted program or turn X11 back on; a damaged file is ignored alone), the shortcut table, snapping and the layouts of the window picker, Task View, the snap layouts and the window menu, the frame geometry, the clip geometry, animation timelines, capture regions (also on a rotated output), EDID names; the mode choice and frame pacing at high refresh rates (ADR-0048); the keys of ADR-0049 and their limits, the colour filter matrices, the magnifier's view, sticky and filter keys, snap layouts by dragging to the top, the title's text size |
| `boswas-compositor`, end to end | 19 | The real binary on its headless back end with real clients and the control socket: a login screen that keeps failing or cannot be started ends the compositor with status 4, and a desktop session stays; without a seat the compositor ends with status 3 and says why; a shell that says `quit` and leaves ends it normally; the login screen runs no X server; ordinary clients get no privileged protocol and a started program does; a configured input method gets its two protocols and nothing else; the limits of the public control socket; a client drawing at its output's refresh rate (60 and 144 Hz); a window's life; the window picker, Task View, snap keys and window menu through injected keys; screen capture only after the user's answer and never while locked (with `grim`); an X11 program through Xwayland (with `xclock`); the lock across a crashed lock screen |
| `boswas-services`, unit | 60 | Parsers, limits, conversions and policies of the ten services; what the lock screen, the login screen and Settings may each ask for |
| `boswas-services`, integration | 85 | Every service against two private `dbus-daemon` processes and scripted stand-ins (see below): notifications, tray, media, network, Bluetooth, audio, power, brightness, session, the polkit agent, start-up and shut-down, buses that go away, and that no secret reaches the log |
| `boswas-services-bridge` | 5 + 1 | The command parser, and the program itself: it reports states, answers bad lines and ends with its input |
| `boswas-shell` | 106 | Every surface drawn without any font; what is reported when no font is installed; every surface drawn in both schemes and checked for controls inside the surface and reachable by keyboard; the taskbar's grouping and layout, Start's search, toasts and the notification centre, the dialogs (Run without a shell, the polkit dialog, the choice of administrator), the capture mark, pinned applications, the greetd messages, the PAM conversation, the users the greeter lists; the desktop settings of the `config` event, the taskbar at the left and without Task View, Start's layouts and recommendations, the do-not-disturb schedule over midnight, the least time a toast stays, the list of notification senders and of applications first seen |
| `boswas-bootsplash` | 126 unit, 18 + 5 + 1 + 1 | Rendering, layout on eleven display sizes, damage, the protocol, both client dialects, the passphrase editor; the whole frame after a mode-set; devices handed over by the supervisor; the console left alone once a graphical session has it; the real programs headless with a pseudo-terminal (a second start, a boot screen that does not answer); the packaged units and initramfs hook; Debian's unmodified `askpass` in a scratch root (needs root and the helper, skipped otherwise); frame cost |
| `desktop/ui` (Python) | 11 | The token file and the generated KDE colour schemes: references, the fixed values, the Fluent geometry, contrast, staleness |
| `desktop/ui/qt` (Python) | 147 | The Qt toolkit on Qt's offscreen platform: theme, style sheet, widgets, navigation, contrast (every accent colour in both schemes), text size, scroll bars, single instance, workers |
| `desktop/icons` (Python) | 42 | The icon packs: the files match the generator, every required name exists, aliases repeat their design |
| `desktop/cursors` (Python) | 82 | The pointer theme: the sources match the generator, every CSS, X11 and toolkit name resolves after installation, the XCursor files are read back by libXcursor and match `xcursorgen` byte for byte, hotspots, animation frames, two installations are identical |
| `desktop/presets` (Python) | 72 (1 skipped here) | The presets and `boswas-preset`: it starts no program and names no KDE tool, writes its three files atomically, and refuses symbolic links |
| `boswas-compat-manager` (Python) | 111 (1 skipped here: `desktop-file-validate` is not installed) | The Compatibility Manager on the toolkit; the installer entry for Boswas Files and which application stays the default |
| `boswas-control-center` (Python) | 688 (5 skipped) | The categories, pages, deep links of earlier versions and search (ADR-0049); every page added with them in a whole window on a fake root with fake programs, with the exact command or file change of each action; the settings framework and every section against stand-ins (a fake bridge, the real settings files read back as the compositor reads them); the file writer against symbolic links and read-only directories; the safety rules (only one module writes files, only one starts programs, every program in its fixed forms, no KDE program is named, opening any page runs nothing privileged and changes nothing); that every page the shell links to exists; the Users page against a stand-in for AccountsService and a scripted `passwd`, SHA-512 crypt against the specification's vectors |
| `boswas-apps` (Python), one suite per application | Terminal 303, Task Manager 261, Archive Manager 255, Files 229, Photos 205, Calculator 161, Media Player 136, Text Editor 131, Calendar 118, Snipping Tool 114, Reminders 103, Weather 93, Document Viewer 90, Clock 58, Camera 40 (2,297 in 15 suites) | Each application's logic (parsers, file operations, the terminal emulator against hostile input, 78 extraction tests with hand-built hostile archives, the compositor's capture request against a stand-in socket, Open-Meteo answers written from its documentation) and its window through real clicks and keys on Qt's offscreen platform, with stand-ins where the real thing would touch the system |

One more `#[test]` in the compositor's end-to-end file is a helper
program for the others and is not counted. Three of the settings tests
run against the real compositor on its headless back end and are opt-in
(`BOSWAS_TEST_COMPOSITOR=1`).

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

### What the tests use in place of the real thing

- **System daemons:** scripted D-Bus peers that play NetworkManager,
  BlueZ, UPower, the power profiles daemon, logind, polkit, tray items
  and media players. `pw-dump`, `wpctl` and polkit's helper are shell
  scripts the tests write.
- **Graphics:** the compositor's headless back end, which renders in
  software. The clip shader for rounded corners and everything in the
  hardware back end are not executed by any test.
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

**Not shown by these boots:** anything about Debian 13 (its
initramfs-tools, on which the hook that copies the graphics drivers has
never run; its versions of greetd and libseat; its package names); the
image and its live session as built (the live-config component ran in a
stand-in for live-config, not under it); the boot loader; real graphics
hardware and hardware rendering; a login screen with a graphics device
and no connected monitor; the boot screen handing over to a real
compositor that takes the display (a stand-in program held the console);
rescue mode; a desktop shell that keeps failing (tests only); the
passphrase prompt of an encrypted disk.

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

## Not verified

Apart from the boots above, nothing below could be exercised in the build
environment.

| Area | Not verified |
|------|--------------|
| Image | The ISO build with this manifest; whether Debian trixie resolves its dependencies and has every package under the name used (`xwayland`, `xdg-desktop-portal-wlr`, `pipewire-bin`, `wireplumber`, `upower`, `power-profiles-daemon`, `accountsservice`, the PySide6 modules); the verify hook; the QEMU boot test, whose checks were rewritten for the native desktop and have never run |
| Graphics hardware | The `udev` back end on real hardware. In the virtual machine it drove one output with software rendering; not seen anywhere: a mode change, rotation, several monitors, monitor hotplug, turning screens off, the gamma ramp of night light, OpenGL ES on a hardware driver (the clip shader, damage, direct scan-out), a pointing device |
| Input hardware | libinput devices and their settings, touch screens, tablets and pens, touchpad gestures, keyboard LEDs |
| Pointer theme | Boswas-Cursors on a hardware cursor plane, at a fractional scale or on a HiDPI display; Xwayland, GTK and Qt clients picking it up in a real session; the `x-cursor-theme` alternative on a Debian 13 image (ADR-0043) |
| Login and lock | The greetd login, the text-login fallbacks and the automatic login on Debian 13 and on the image (they were booted on another base system, see above); PAM on the image; the live session under the real live-config; suspend and resume; `loginctl lock-session` against the real logind |
| Boot screen | On hardware: DRM and frame-buffer output and the hand-over from the firmware frame buffer (seen in the virtual machine only). The initramfs hook and scripts under Debian 13's initramfs-tools, and the size of the initramfs there; a legacy frame buffer (`efifb`, `vesafb`: the test kernel has neither, and a frame buffer device emulated by a DRM driver showed nothing of what was drawn into it); a real LUKS unlock; Caps Lock on a real console; the animation in motion on a panel |
| System services | NetworkManager, BlueZ, PipeWire (`pw-dump`, `wpctl`), UPower, power-profiles-daemon, logind and polkit: every backend has run against stand-ins only. The polkit helper protocol, the shape of `pw-dump`'s JSON and NetworkManager's settings dictionaries were written without a real daemon to compare with |
| Authentication | A `pkexec` call answered by the shell's agent. Until that has run, no administrator action from the desktop is known to work |
| Screen capture | Screen sharing and screenshots through `xdg-desktop-portal-wlr`; capture of a rotated monitor end to end; what the consent dialog names for the portal back end |
| X11 | Xwayland on hardware; the clipboard between X11 and Wayland applications; Wine's Wayland driver on Debian 13 (the sandbox gives a Windows application the Wayland socket and no X11 socket, ADR-0034) |
| Input methods | No input method has been run on the compositor; none is in the image |
| Applications on the desktop | No run of Firefox, Wine or Waydroid on the native compositor is recorded. Weather has never reached the real Open-Meteo service (the build environment cannot). The Boswas applications and Control Center have not been started in a real session on hardware (window activation, the primary selection, drag and drop between programs, an input method's window); see "Looked at by hand" for the headless run from the built packages. Applying a preset from Settings in a running session was not tried |
| Applications and devices | See the next section: a camera, removable drives, sound, real documents. Printing was tried to a PDF file only, and the image has no print service (CUPS is not in the manifest); Control Center's "Install printing support" has not run, and may be refused if APT would bring `cups-browsed`, which the image pins out and the system helper excludes |
| Settings | Every section that talks to a system service ran against a stand-in: the real bridge reported "not available" here. Arranging several monitors is tested against a model only (the headless back end has one output and ignores a scale change on reload). `timedatectl` and `localectl` changes behind polkit. The pages of ADR-0049 against the real NetworkManager (VPN, adapters), CUPS, logind (advanced start-up), systemd-hostnamed (renaming), the Flatpak permission store and fontconfig; the output formats of `nmcli`, `lpstat`, `fc-list`, `dpkg-query` and `flatpak permission-list`, written from their documentation; some package names of Optional features (`gstreamer1.0-plugins-ugly`, `gstreamer1.0-libav`, `printer-driver-cups-pdf`, `fonts-noto-cjk`, `fonts-crosextra-carlito`, `fonts-crosextra-caladea`, `build-essential`, `firefox-esr-l10n-*`, `cups`) against the Debian archive |
| Performance | Frame times on a real GPU, memory and start-up time of the compositor and the shell. Frame costs were measured on the build machine without a GPU and in the virtual machine only (ADR-0045, below) |
| Accessibility and review | No screen reader interface exists for the shell or for what the compositor draws. The colour-filter pass and the magnifier through the DRM back end are compiled only; sticky and filter keys have unit tests and have not run in the real compositor; not every shell surface was looked at with 225 % text. No external security review and no design review have taken place |

## The system applications and the settings, one by one

From the builders' reports. "Left out" is what the application does not
do on purpose or not yet; "not verified" is what could not be tried here.

| Application | Left out | Not verified |
|-------------|----------|--------------|
| Files | Thumbnails for videos and PDFs; password-protected ZIP archives; view settings per folder; special handling of hard links and sparse files | A real udisks service with its polkit question (mount, unlock, eject); notification of mount changes (a 15 second poll is the fallback); real drag gestures (the drop logic is tested) |
| Terminal | Split panes; screen-reader access to the text; the kitty keyboard protocol; sixel; an audible bell; blinking text; shaping across cells (Indic clusters are drawn cell by cell). The question before closing a busy tab does not appear for a program started with `-e` | A real Wayland session (activation, primary selection, an input method's window); a run as an ordinary user; automatic scrolling while dragging; the Hack font (rendered in DejaVu Sans Mono here). Tried with real bash, vim, tmux, less and top in the window |
| Text Editor | Syntax highlighting; spell checking; tear-off tabs; the page set-up is not remembered; saving by rename leaves other hard links with the old content | Started through its launcher (the environment is root) |
| Calculator | Graphing; currency conversion; a settings page; memory and history are not kept between runs | Its 739 expected results were written from the documented behaviour of the Windows calculator, not compared with a running one |
| Photos | XMP and IPTC data are lost on save; an edit of a picture above 48 megapixels is saved reduced; crop handles need a pointer; AVIF, HEIF and JPEG XL need `kimageformat6-plugins`; the gallery sorts by file time, not by the date in the picture | A real printer; "Set as desktop background" against the running compositor and "Show in Files" against the running file manager (tested through replaced hooks); animated WebP; a pinch on a touch screen |
| Document Viewer | PDF only; no annotations (Qt PDF cannot write); no selection across pages; open tabs are not restored | Only PDFs written by the tests were opened: no real-world file with pictures, CJK fonts, AES encryption or forms. Ran on one version of PySide6 (6.12) |
| Archive Manager | No encrypted archives are created; nothing is renamed inside an archive; entries cannot be dragged out; each start is its own window | 7z through `7z` 23.01 only (Debian 13 has 25.01); RAR with no real file |
| Media Player | No tags for Matroska, AVI, WMA and AIFF; no MPRIS track list; the mini player does not stay on top | Audible output, switching the output device, network streams and files with several tracks ran against a fake back end only. Real Qt Multimedia decoded mp3, flac, ogg, opus, wav, m4a, mp4, mkv, webm, mov and avi without a sound device. Video at 4K was not measured |
| Camera | Flash, exposure, focus and zoom appear only when the camera reports them | Everything: there is no camera here. Capture, recording, pause, unplugging and the error texts ran against a fake camera |
| Task Manager | No network column per process; no efficiency mode; no way to disconnect a session. A process that used almost no CPU shows a change of its memory or disk use up to 8 seconds late | A real Wayland session; a real GPU, battery and Wi-Fi adapter (fake sysfs in the tests); the polkit question when a service is started or stopped; "Open file location" against the running Files. Tried live against a real systemd user manager and a real logind on a private bus; its CPU figures were compared with `top` |
| Clock | No choice of alarm sound | The chime (Qt Multimedia crashes in this environment, see the note on Qt Multimedia above); logind's wake-up signal |
| Calendar | No accounts and no synchronisation; no printing; resizing by dragging only in the week and day views; it does not notice when another program changes its file; events of a hidden calendar do not remind | Reminders against the real notification service of the shell (tested against a stand-in on a private bus) |
| Control Center, settings | Editing a proxy; connecting a VPN that asks for a secret each time; a test sound; an automatic suspend time; identifying monitors; a pointer theme or colour; drawing tablets; more than four keyboard layouts; locking an account; setting the default printer; pausing updates; resetting the device; notifications on the lock screen; contrast themes; underlined access keys; an accent from the wallpaper; functions that need an online service or a phone (ADR-0049). Passwords set for other users are SHA-512 crypt, not Debian 13's default scheme, and only the length and character-class rules of `pwquality` are applied to them | See the table above. For the Users page: the shape of `busctl`'s replies, the conversation with `passwd` under real PAM modules, polkit's questions through the shell's agent, signing in as a user it created, removing a user with and without the files, the picture on the login screen |

Icons: the applications ask for some symbolic icons the Boswas Prism pack
does not draw (the reports name them). Ten were added; the others fall
back to a similar icon or to a text button.

## Known not to work, or left out

Limits that are decisions or known gaps, not untested paths. Each has its
ADR.

- No live blur: materials are drawn from the wallpaper (ADR-0036).
- X11 windows are drawn at scale 1 and enlarged on a scaled monitor; X11
  applications can observe each other (ADR-0034).
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
- The boot screen's text is ASCII and English (ADR-0038).
- With `nomodeset` there is no KMS device: the desktop does not start and
  the text login is shown. The boot screen does not draw on a 24-bit
  legacy frame buffer (ADR-0044).
- Between the boot screen and the login screen the display is an empty
  console for the moment the compositor needs to start (ADR-0044).
- The shortcuts are fixed; there is no shortcut editor.
- No contrast themes for the shell; colour filters need OpenGL ES and are
  not available where the compositor draws with pixman (ADR-0049).
- A VPN that asks for a password or another secret when it connects
  cannot be connected from the desktop: there is no secret agent for
  NetworkManager (ADR-0049).
- There is no office suite and no mail client in the image, and the
  Document Viewer reads PDF only (ADR-0040).
- There is no print service in the image: the applications' Print
  dialogs can print to a PDF file only.
