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
| `desktop/ui/qt` (Python) | 128 | The Qt toolkit on Qt's offscreen platform: theme, style sheet, widgets, navigation, contrast, single instance, workers |
| `desktop/icons` (Python) | 42 | The icon packs |
| `desktop/presets` (Python) | 75 (2 skipped here) | The presets and `boswas-preset` |
| `boswas-compat-manager` (Python) | 107 | The Compatibility Manager on the toolkit |

One more `#[test]` in the compositor's end-to-end file is a helper
program for the others and is not counted. The suites of Control Center
and of the system applications are not listed: both are being changed
(ADR-0040, ADR-0041).

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
- **Qt:** the offscreen platform.

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
| Applications on the desktop | No run of KDE's applications, Firefox, Wine or Waydroid on the native compositor is recorded |
| Performance | Frame times, memory and start-up time of the compositor and the shell. Only the boot screen has measured frame costs, on the build machine |
| Accessibility and review | No screen reader interface exists for the shell or for what the compositor draws. No external security review and no design review have taken place |

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
