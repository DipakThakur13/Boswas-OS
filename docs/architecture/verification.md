# What has and has not been verified

The state of the native desktop on 2026-11-16 (development tree after
1.0~alpha3). The reports in `docs/reports/` describe the KDE Plasma image
of 2026-11-12 and are not repeated here.

"Verified" below means an automated test, or a check that a builder
recorded, in the build environment. That environment has no graphics
device, boots nothing, and runs none of the system daemons the desktop
talks to. Everything in [Not verified](#not-verified) follows from that.

## Automated tests

Counted from the sources (`#[test]` attributes; `Ran N tests` for the
Python suites, which were run). The numbers move as work lands.

| Suite | Tests | What they cover |
|-------|-------|-----------------|
| `boswas-ui` (Rust) | 26 | The token file as the programs read it, drawing, text, icons, the materials |
| `boswas-compositor`, unit | 72 | The configuration layers and their rules (a user cannot relax the lock policy or name a trusted program; a damaged file is ignored alone), the shortcut table, snapping and the layouts of the window picker, Task View, the snap layouts and the window menu, the frame geometry, the clip geometry, animation timelines, capture regions (also on a rotated output), EDID names |
| `boswas-compositor`, end to end | 11 | The real binary on its headless back end with real clients and the control socket: ordinary clients get no privileged protocol and a started program does; the limits of the public control socket; a window's life; the window picker, Task View, snap keys and window menu through injected keys; screen capture only after the user's answer and never while locked (with `grim`); an X11 program through Xwayland (with `xclock`); the lock across a crashed lock screen |
| `boswas-services`, unit | 59 | Parsers, limits, conversions and policies of the ten services |
| `boswas-services`, integration | 85 | Every service against two private `dbus-daemon` processes and scripted stand-ins (see below): notifications, tray, media, network, Bluetooth, audio, power, brightness, session, the polkit agent, start-up and shut-down, buses that go away, and that no secret reaches the log |
| `boswas-services-bridge` | 5 + 1 | The command parser, and the program itself: it reports states, answers bad lines and ends with its input |
| `boswas-shell` | 88 | Every surface drawn in both schemes and checked for controls inside the surface and reachable by keyboard; the taskbar's grouping and layout, Start's search, toasts and the notification centre, the dialogs (Run without a shell, the polkit dialog, the choice of administrator), the capture mark, pinned applications, the greetd messages, the PAM conversation, the users the greeter lists |
| `boswas-bootsplash` | 108 unit, 14 + 1 + 1 | Rendering, layout on eleven display sizes, damage, the protocol, both client dialects, the passphrase editor; the real programs headless with a pseudo-terminal; Debian's unmodified `askpass` in a scratch root (needs root and the helper, skipped otherwise); frame cost |
| `desktop/ui` (Python) | 11 | The token file and the generated KDE and Konsole colour schemes: references, the fixed values, the Fluent geometry, contrast, staleness |
| `desktop/ui/qt` (Python) | 131 | The Qt toolkit on Qt's offscreen platform: theme, style sheet, widgets, navigation, contrast, single instance, workers |
| `desktop/icons` (Python) | 42 | The icon packs |
| `desktop/presets` (Python) | 75 (2 skipped here) | The presets and `boswas-preset` |
| `boswas-compat-manager` (Python) | 107 | The Compatibility Manager on the toolkit |
| `boswas-control-center` (Python) | 584 (4 skipped) | The settings framework and every section against stand-ins (a fake bridge, a model of the compositor's files), the file writer against symbolic links and read-only directories, the safety rules (only one module writes files, only one starts programs, no KDE program is named), the Users page against a stand-in for AccountsService and a scripted `passwd`, SHA-512 crypt against the specification's vectors, the earlier pages |
| `boswas-apps` (Python), one suite per application | Terminal 302, Task Manager 261, Archive Manager 255, Files 229, Photos 204, Calculator 160, Media Player 136, Text Editor 131, Calendar 112, Reminders 103, Document Viewer 90, Clock 58, Camera 39 | Each application's logic (parsers, file operations, the terminal emulator against hostile input, 78 extraction tests with hand-built hostile archives) and its window through real clicks and keys on Qt's offscreen platform, with stand-ins where the real thing would touch the system |

One more `#[test]` in the compositor's end-to-end file is a helper
program for the others and is not counted. Three of the settings tests
run against the real compositor on its headless back end and are opt-in
(`BOSWAS_TEST_COMPOSITOR=1`).

`tests/static/test_sources.sh` checks, for this round: that a public
control connection gets exactly `version`, `lock`, `reload-config`,
`outputs` and `capture`; that the privileged protocols are filtered by
client trust; and that `unsafe` appears only in the seven audited files.

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

No package build of `boswas-desktop` or `boswas-branding` with this
round's changes is recorded in the builders' reports. `./test.sh
--packages-only` has to show that they still build and pass lintian.

## Not verified

Nothing below could be exercised in the build environment.

| Area | Not verified |
|------|--------------|
| Image | The ISO build with this manifest; whether Debian trixie resolves its dependencies and has every package under the name used (`xwayland`, `xdg-desktop-portal-wlr`, `pipewire-bin`, `wireplumber`, `upower`, `power-profiles-daemon`, `accountsservice`, the PySide6 modules); the verify hook; the QEMU boot test, whose checks were rewritten for the native desktop and have never run |
| Graphics hardware | The `udev` back end on any DRM/KMS device: modes, rotation, several monitors, hotplug, turning screens off, the gamma ramp of night light, OpenGL ES on a hardware driver (the clip shader, damage, direct scan-out), software rendering under `nomodeset` |
| Input hardware | libinput devices and their settings, touch screens, tablets and pens, touchpad gestures, keyboard LEDs |
| Login and lock | The greetd login on a real boot, PAM on the image, the text-login fallback, the live session's automatic login, suspend and resume, `loginctl lock-session` against the real logind |
| Boot screen | DRM and frame-buffer output, the hand-over from the firmware frame buffer and to the greeter, the initramfs scripts under Debian 13's initramfs-tools, a real LUKS unlock, Caps Lock on a real console, the animation in motion on a panel |
| System services | NetworkManager, BlueZ, PipeWire (`pw-dump`, `wpctl`), UPower, power-profiles-daemon, logind and polkit: every backend has run against stand-ins only. The polkit helper protocol, the shape of `pw-dump`'s JSON and NetworkManager's settings dictionaries were written without a real daemon to compare with |
| Authentication | A `pkexec` call answered by the shell's agent. Until that has run, no administrator action from the desktop is known to work |
| Screen capture | Screen sharing and screenshots through `xdg-desktop-portal-wlr`; capture of a rotated monitor end to end; what the consent dialog names for the portal back end |
| X11 | Xwayland on hardware; the clipboard between X11 and Wayland applications; Wine through Xwayland, and which display driver Wine chooses when both sockets are in its sandbox (ADR-0034) |
| Input methods | No input method has been run on the compositor; none is in the image |
| Applications on the desktop | No run of Firefox, Wine or Waydroid on the native compositor is recorded, and none of the Boswas applications or Control Center has been started in a real session (window activation, the primary selection, drag and drop between programs, an input method's window) |
| Applications and devices | See the next section: a camera, removable drives, sound, real documents. Printing was tried to a PDF file only, and the image has no print service (CUPS is not in the manifest) |
| Settings | Every section that talks to a system service ran against a stand-in: the real bridge reported "not available" here. Arranging several monitors is tested against a model only (the headless back end has one output and ignores a scale change on reload). `timedatectl` and `localectl` changes behind polkit |
| Performance | Frame times, memory and start-up time of the compositor and the shell. Only the boot screen has measured frame costs, on the build machine |
| Accessibility and review | No screen reader interface exists for the shell or for what the compositor draws. No external security review and no design review have taken place |

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
| Clock | No choice of alarm sound | The chime (Qt Multimedia crashes in this environment); logind's wake-up signal |
| Calendar | No accounts and no synchronisation; no printing; resizing by dragging only in the week and day views; it does not notice when another program changes its file; events of a hidden calendar do not remind | Reminders against the real notification service of the shell (tested against a stand-in on a private bus) |
| Control Center, settings | Editing a proxy; managing VPN connections; a test sound; an automatic suspend time; identifying monitors; a pointer theme; drawing tablets; more than four keyboard layouts; locking an account. Passwords set for other users are SHA-512 crypt, not Debian 13's default scheme, and are not checked by `pam_pwquality` | See the table above. For the Users page: the shape of `busctl`'s replies, the conversation with `passwd` under real PAM modules, polkit's questions through the shell's agent, signing in as a user it created, removing a user with and without the files, the picture on the login screen |

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
- Outputs on a second GPU, HDR, variable refresh rate and the NVIDIA
  proprietary driver are not supported (ADR-0036).
- The boot screen's text is ASCII and English (ADR-0038).
- The shortcuts are fixed; there is no shortcut editor.
- There is no office suite and no mail client in the image, and the
  Document Viewer reads PDF only (ADR-0040).
- There is no print service in the image: the applications' Print
  dialogs can print to a PDF file only.
