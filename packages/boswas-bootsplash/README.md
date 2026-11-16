# boswas-bootsplash

The Boswas OS boot screen: the mark and a ring of dots on a dark, almost
empty screen while the system starts, the passphrase prompt for an encrypted
disk, and the shutdown and restart screens. It follows Boswas UI, the Fluent
design language of the desktop (`desktop/ui/tokens.json`). Two small Rust
programs take the place of Plymouth and of the Plymouth theme in
`desktop/branding/plymouth/`.

| Program | Installed as | What it is |
|---------|--------------|------------|
| `boswas-bootsplashd` | `/usr/libexec/boswas/boswas-bootsplashd` | The daemon: owns the display, the console keyboard and the control socket |
| `boswas-bootsplash` | `/usr/bin/boswas-bootsplash`, and `/bin/plymouth` **inside the initramfs only** | The client: `ping`, `ask-password`, `message`, `mode`, `hide`, `show`, `quit`, the systemd password agent |

**Status.** Everything that can run without a graphics device is tested
here (see [Tests](#tests)). The DRM/KMS and frame buffer back ends, the
initramfs scripts and the systemd units have not yet run on a real boot:
that needs the image build and the QEMU boot test (`./test.sh`), and
hardware. [What is verified](#what-is-verified-and-what-is-not) lists both
sides.

## What the user sees

| Screen | Content |
|--------|---------|
| Boot | The official mark (gold and silver, unchanged) and nothing written: no name. The mark is centred a little above the middle, 12 % of the display's shorter side high (never more than 160 logical pixels). Lower down, 28 % of the height below the middle, a ring 40 pixels across: five white dots that chase each other round it. They bunch up over the top, sweep round, spread out and fade. A message from the boot scripts, when one is sent, is one line under the ring (body text, `text-secondary`; an error in `danger`) and goes away after six seconds (an error after thirty, or with the next passphrase); a reported progress is a percentage on that line |
| Disk unlock | The mark stays where it is. Under it: "Unlock your disk" (subtitle), the name of the disk (body, `text-secondary`), and a text box: 320 by 36, corners rounded by 4, the subtle fill and one-pixel outline of a control, and the two-pixel accent line (`iris.400`) of a focused one along its bottom edge. One dot per character typed, from the left with 12 pixels of padding (24 at most, so a long passphrase does not show its length), then a caret that blinks. Under the box a hint (caption, `text-muted`): "Press Enter to unlock", or "Caps Lock is on" in `warning` |
| Rejected passphrase | The prompt returns with "That passphrase did not work. Try again." in `danger` under the box (the hint moves down a line), and the box shakes sideways for 0.4 seconds |
| Shutdown, restart | The ring in the middle of the screen and one line under it (body-large): "Shutting down" or "Restarting". No mark |

The background is one solid colour, `neutral.950`; the text is white
(`text` of the dark theme) in Inter, the Boswas UI typeface. Colours, the
type ramp (caption 12, body 14, body-large 16, subtitle 20 semibold) and the
control radius come from `desktop/ui/tokens.json` at build time.

Sizes are logical pixels. A logical pixel is one display pixel up to 1080
lines and grows with the height above that (1.33 at 2560x1440, 1.48 at
2560x1600, 2 at 3840x2160, at most 3.2), so the text box is 320 by 36 real
pixels at 1024x768, 1366x768 and 1920x1080, and 640 by 72 at 4K. A display
with fewer than 600 lines, or narrower than 480 pixels, gets a smaller unit
so that the layout fits. Outlines, the caret and the distance between dots
are whole pixels at every scale.

What the unlock screen says comes from the prompt of the program that asks.
The prompts of cryptsetup's initramfs scripts (`Please unlock disk NAME`),
of systemd (`Please enter passphrase for disk NAME:`) and of cryptsetup
itself (`Enter passphrase for NAME:`) become the title "Unlock your disk"
and the name; any other prompt is shown as it came, under "Enter your
passphrase". A message that reports a rejected passphrase (it contains
`bad password`, `bad passphrase`, `incorrect pass`, `wrong pass` or `no key
available`; cryptsetup's scripts send `cryptsetup failed, bad password or
options?`) is shown as the one short sentence above; any other error is
shown as it came, shortened to its line. The scripts send that message
after Enter and then ask again, so between the two the prompt is closed and
the boot screen with its ring shows while the passphrase is checked.

Keys while a prompt is open: characters, Backspace, Ctrl-U (clear), Enter.
Escape switches between the boot screen and the text console at any time.

Kernel command line: the boot screen runs only with `splash`. `nosplash`
(or `boswas.splash=off`, or `plymouth.enable=0`) turns it off;
`boswas.splash=static` keeps the screen without animation: the dots of the
ring stand still, the caret does not blink and the box does not shake.

Review frames (boot, boot-message, prompt-empty, prompt, prompt-wrong,
prompt-shake, shutdown, reboot, and three consecutive frames of the ring,
as PNG files for each size):

```sh
cargo run --release --example frames -- /tmp/frames 1920x1080 1366x768
```

## How it renders

`src/scene.rs` keeps the picture as nine items in fixed slots (mark, ring,
status line, message, title, name of the disk, text box, caret, hint). A
frame compares the new items with the ones on screen and repaints only the
rectangles of items that changed, into an XRGB8888 canvas; the output copies
those rectangles to the display and tells the driver which ones changed. An
animation frame is the ring's rectangle (the background colour and up to
five small discs); a blink of the caret is the caret's own pixels; a
keystroke or a frame of the shake is the text box. Nothing else is touched,
and a frame that changes nothing is not sent to the display at all.

| At 1920x1080 (release build, this machine) | Cost |
|---|---|
| First frame (scale the mark, paint everything) | 4.5 ms |
| Animation frame | 3 µs: 1,764 pixels (42 by 42), 0.085 % of the screen |
| Blink of the caret | 1 µs: 18 pixels |
| Keystroke in the text box, or a frame of its shake | 0.17 ms: 12,844 pixels, 0.62 % |

At 3840x2160 an animation frame is 8 µs (6,724 pixels) and a keystroke
0.65 ms. `tests/perf.rs` prints these (`cargo test --release --test perf --
--nocapture`) and fails if an animation frame redraws anything but the ring,
a blink anything but the caret, or the ring is a quarter of a percent of the
screen or more. Rendering a frame allocates no memory (the picture borrows
the prompt and the message; the list of changed rectangles is reused). The
animation runs at 30 frames a second at most, driven by the clock, so a busy
system makes it slower to update but not faster or uneven; while a prompt is
open the loop wakes only when the caret is due to change, four times a
second for Caps Lock, and for the frames of a shake.

The ring is arithmetic, not artwork (`dot_at` in `src/scene.rs`): each dot
goes round twice in three seconds, entering and leaving at the bottom; a
slow steady drift carries it over the top, and three eased runs (out, in and
out, in) take it up the side, once round, and away. The dots follow each
other 0.2 seconds apart, so they are close together where the path is slow
and far apart where it is fast. A cycle is 4.2 seconds.

Nothing is parsed at boot. `build.rs` rasterises
`desktop/branding/boswas-os-mark.svg` (360 pixels high) and the printable
ASCII glyphs of Inter Regular (at 52 pixels) and Inter SemiBold (at 64),
and embeds them as plain bytes; text is scaled down from those atlases with
sub-pixel placement and without kerning. Characters outside ASCII are drawn
as `?`.

Inter is not in the repository. `build.rs` reads `Inter-Regular.otf` and
`Inter-SemiBold.otf` from `/usr/share/fonts/opentype/inter`, where the
Debian package `fonts-inter` installs them; it is a build dependency of the
Debian package, whose build fails without it
(`BOSWAS_BOOTSPLASH_REQUIRE_INTER=1` in `debian/rules`). A plain `cargo
build` on a machine without Inter prints a warning and makes the atlases
from the repository's brand typeface instead (Orbitron Medium and SemiBold),
so the crate still builds and its tests pass; `assets::TYPEFACE` says which
one a build has. `BOSWAS_BOOTSPLASH_INTER_DIR` names another directory to
take the two Inter files from. Only glyph bitmaps are embedded (Inter is
under the SIL Open Font License 1.1; `debian/copyright` carries its notice
and the licence); no font file is installed or copied into the initramfs.

| Embedded in the daemon | Bytes | gzip -6 |
|---|---|---|
| The mark, 290x360 premultiplied RGBA | 417,600 | 17,191 |
| Inter Regular, 95 glyphs at 52 pixels | 80,109 | 21,662 |
| Inter SemiBold, 95 glyphs at 64 pixels | 130,068 | 26,980 |

As packaged (stripped), the two programs together are 1.76 MB in the
initramfs before compression (the daemon 1.31 MB, the client 0.45 MB) and
0.61 MB after `gzip -6`. The two font files themselves would add 1.23 MB
(0.48 MB compressed), which is why the glyphs are built in instead.

## Outputs

| Output | When | Notes |
|--------|------|-------|
| DRM/KMS dumb buffers (`src/output/drm.rs`) | A `/dev/dri/card*` with a connected display | No GPU acceleration, no libdrm (the `drm` crate issues the ioctls). Every connected display gets its own buffer and layout. The mode already set is kept when possible. What the display showed before (the kernel console) is put back on hide and quit |
| Frame buffer (`src/output/fbdev.rs`) | No DRM device, `/dev/fb0` exists | 32 bits per pixel with 8-bit channels, or RGB565 |
| Headless (`src/output/headless.rs`) | `--headless=WxH --out=DIR` | For tests and review; frames are saved on request |

Graphics devices change during boot: the firmware frame buffer
(`simpledrm`) is removed when the real driver loads. The daemon notices
(any call on the old device fails), looks for a new device every 250 ms and
continues there. If no display comes back within two seconds it switches to
the text console, so a prompt is never left where nobody can see it, and
returns to the boot screen when a display appears.

## Processes

`boswas-bootsplashd` starts in three steps (`src/launch.rs`):

1. the **launcher** (the process the script started) reads the kernel
   command line, starts the supervisor, waits until the boot screen reports
   that it is up, and exits with that result;
2. the **supervisor** starts the worker and waits. If the worker ends
   abnormally it puts the console back into text mode and removes the
   control socket;
3. the **worker** is the boot screen (`src/daemon.rs`): one single-threaded
   event loop over the control socket, the console and a timer.

Supervisor and worker run as `@boswas-bootsplashd`: systemd leaves processes
whose name starts with `@` alone in its final clean-up, which keeps the
shutdown screen up until the machine powers off.

## Control protocol

`/run/boswas-bootsplash/socket`, a Unix stream socket, mode 0600 in a 0700
directory; the daemon also checks that the peer is root (`SO_PEERCRED`). A
frame is a 16-bit big-endian length and at most 4096 bytes:

| Request | Reply |
|---------|-------|
| `PING` | `OK` |
| `ASK prompt` | `PASS passphrase`, when Enter is pressed |
| `MSG text` | `OK` (empty text clears the line) |
| `PROGRESS 0..100`, `MODE boot\|shutdown\|reboot`, `HIDE`, `SHOW` | `OK` |
| `QUIT`, `QUIT RETAIN seconds` | `OK` |
| `ROOT /path` | `OK` or `ERR reason` |
| `SNAPSHOT name` | `OK` (headless output only) |

Anything else gets `ERR reason`; a frame with an impossible length closes
the connection. A prompt whose client disconnects is withdrawn. When the
client limit (16) is reached, the oldest connection that is not waiting for
a passphrase is closed.

## Disk unlock in the initramfs

Debian's cryptsetup-initramfs asks for the passphrase with
`/lib/cryptsetup/askpass`. Its source (`debian/askpass.c`, read for this
package) tries four methods in order: systemd, a FIFO, Plymouth, the
console.

* For Plymouth it runs `/bin/plymouth --ping` and, if that succeeds,
  `/bin/plymouth ask-for-password --prompt PROMPT` with standard output on a
  pipe, and takes everything written there until end of file as the
  passphrase. A trailing `: ` or `:` is removed from the prompt first.
* The Plymouth method is marked `no_more`: when it is active the console
  method is not started, so the two never read the same keyboard. The FIFO
  (`/lib/cryptsetup/passfifo`, used by `cryptroot-unlock`) stays active
  next to it; if it delivers first, askpass kills the Plymouth client.
* If the client's output ends without a passphrase, askpass returns an empty
  one. cryptsetup rejects it and the initramfs asks again.

So the initramfs hook installs the client as `/bin/plymouth` in the
initramfs image, and the client, when called by that name, understands the
commands askpass and the initramfs scripts use (`src/compat.rs`): `--ping`,
`ask-for-password --prompt`, `message --text`, `display-message --text`,
`hide-message`, `hide-splash`, `show-splash`, `quit [--retain-splash]`,
`update-root-fs --new-root-dir`. Other Plymouth commands that scripts issue
for progress are accepted and ignored; unknown ones fail, as with Plymouth.
`ask-for-password --command` is not supported.

No file named `plymouth` is installed on the root file system, and the
package conflicts with `plymouth`.

What happens when something goes wrong:

| Situation | Result |
|-----------|--------|
| No `splash`, or `nosplash` | The daemon does not start. `--ping` fails; askpass asks on the console |
| No display, or no console keyboard, at start | The daemon exits with an error. Same as above |
| The boot screen cannot draw its first frame | The same: it does not claim the prompt |
| The daemon dies while a prompt is open | The supervisor restores the text console. The client exits; askpass returns an empty passphrase; the next attempt is asked on the console |
| The display disappears for more than two seconds | The prompt moves to the text console (typing is not echoed); the boot screen returns with the display |
| The initramfs gives up (panic) | `scripts/panic/boswas-bootsplash` quits the boot screen so the emergency shell is visible |

## Start-up and shut-down

| File | Does |
|------|------|
| `initramfs/hook` | Copies both programs into the initramfs, links the client as `/bin/plymouth` there, and names the small early graphics drivers |
| `initramfs/conf-hook` | `FRAMEBUFFER=y`, so initramfs-tools includes graphics drivers (as Plymouth's does) |
| `initramfs/init-premount` | Starts the daemon after udev has loaded the drivers and before the root device is unlocked |
| `initramfs/init-bottom` | `new-root $rootmnt`: the daemon follows the system to its real root |
| `initramfs/panic` | Quits the boot screen |
| `boswas-bootsplash-start.service` | Starts the boot screen if the initramfs did not (wanted by `sysinit.target`) |
| `boswas-bootsplash-quit.service` | `quit --retain` before `greetd.service`, `display-manager.service` and `getty@tty1.service` (wanted by `multi-user.target`) |
| `boswas-bootsplash-poweroff.service`, `-halt.service`, `-reboot.service` | The shutdown and restart screens |
| `boswas-bootsplash-ask-password.path`, `.service` | A systemd password agent: later requests (a second encrypted volume) appear on the boot screen |
| `systemd-ask-password-console.path.d/50-boswas-bootsplash.conf` | Keeps systemd's console agent off while the boot screen runs; it would compete for the same keystrokes |

`quit --retain` gives up the display but leaves the last frame on it, so the
greeter's compositor can take over without a blank screen. The daemon exits
as soon as another program shows its own picture. If nothing does within
six seconds, it restores the text console and exits. This hand-over is
free of a blank only if the greeter uses the virtual console the boot screen
is on (tty1); a switch to another console shows the text console briefly.

## What the daemon gives up

After the display, the console and the socket are open (`src/sandbox.rs`):

* `no_new_privs`;
* a Landlock domain: file access only below `/dev/dri`, to `/dev/fb0` and
  in `/run/boswas-bootsplash`; no TCP; where the kernel supports it, no
  signals to and no abstract sockets of other processes;
* all capabilities except `CAP_SYS_ADMIN` (to become DRM master again after
  hiding or a driver change), `CAP_SYS_TTY_CONFIG` (console text and
  graphics mode) and `CAP_SYS_CHROOT` (`new-root`).

It is best effort: a kernel without Landlock still gets the rest.
`boswas-bootsplashd --self-test` enters the sandbox and reports what is
enforced (exit status 0: all of it; 3: the kernel has no Landlock; 1: a
denial is missing). There is no seccomp filter.

Other properties: no network code; no environment variable changes the
daemon's behaviour and it clears the environment of the processes it
starts; message and frame sizes are bounded; the passphrase lives in one
fixed buffer that is wiped when taken, cleared or dropped, is never
formatted or logged, and is wiped from the reply frame after sending.
`unsafe` code is confined to `src/sys.rs` (console and frame buffer ioctls,
`mmap` for the frame buffer, `signalfd`); the rest of the crate is built
with `#![deny(unsafe_code)]`.

## Building

```sh
cargo build --release --locked      # needs Rust 1.85 (Debian 13) or newer
```

The crates in `Cargo.lock` are downloaded from crates.io, so the build needs
network access to it; this also applies to the Debian package build
(`debian/rules`). `fontdue` is pinned to 0.9.3 because later versions need a
newer compiler than Debian 13 has. The Inter fonts come from the Debian
package `fonts-inter` on the build machine (see [How it renders](#how-it-renders)).

Run-time dependencies: `drm`, `rustix`, `libc`, `landlock`, `zeroize`.
Build-time only: `resvg`, `fontdue`, `serde_json`.

## Tests

```sh
cargo test --locked
```

| Where | What |
|-------|------|
| Unit tests (`src/`) | Drawing and scaling; the layout on eleven display sizes (everything on the display, in order, on one axis; the mark, the ring and the text box at their sizes); what each screen is made of, with the styles and colours of the token file; the path of a dot and the spacing of the dots; damage (an animation frame only changes the ring, a blink only the caret, typing and the shake only the text box) and that a frame leaves nothing of earlier frames behind; dots, not characters, capped at 24; the rejected-passphrase line and the shake; Caps Lock; hostile text; the passphrase editor and key decoder; the protocol with malformed and random input; both command line dialects; the kernel command line; the frame buffer formats; the password agent's parser; and the daemon on a mock display: what it presents for an animation frame, a blink and a shake, when its loop wakes, loss of the display, fallback to the console, recovery, and the hand-over on `quit --retain` |
| `tests/daemon.rs` | The real programs, headless, with a pseudo-terminal as the console: requests change the picture (message, error, progress, shutdown, restart); the animation stays inside the ring; hostile input; passphrase entry through the client called `plymouth`, with the caret blinking; a rejected passphrase is reported on the unlock screen, and the picture is the same for any passphrase of a length; prompts queue and are withdrawn when a client dies; Escape; launcher, supervisor and worker (a killed worker leaves a restored console and no socket); the sandbox; the systemd password agent |
| `tests/askpass.rs` | Debian's real, unmodified `askpass` against the daemon, in a scratch root directory laid out like the initramfs (needs root and the helper; skipped otherwise): passphrase from the boot screen, FIFO next to it, daemon death, console fallback |
| `tests/perf.rs` | Frame cost and damage size: the ring, the caret, a keystroke, the shake |

The askpass helper is not in this repository. To run that test:

```sh
apt-get source cryptsetup          # or the debian.tar.xz of the source package
sh tests/build-askpass.sh cryptsetup-*/debian/askpass.c
cargo test --test askpass -- --nocapture
```

Test hook: a client started without `--socket` uses
`boswas-bootsplash.socket` in the directory it was invoked from, if that
exists, instead of the system socket. The tests use it for the client
called `plymouth`, which has no socket option. Program directories on a
device never contain such an entry.

## What is verified and what is not

Verified here (124 tests, Rust 1.85 and current stable):

* rendering, layout and damage, on review frames (looked at, at 1024x768,
  1366x768, 1920x1080, 2560x1600 and 3840x2160) and in tests;
* the control protocol, both client dialects, passphrase entry and editing
  through a terminal;
* the unmodified Debian `askpass` (cryptsetup 2.7.5) obtaining the
  passphrase through the client installed as `/bin/plymouth`, the FIFO
  path, and the console fallback;
* start-up chain, clean-up after a killed worker, sandbox enforcement
  (Landlock fully enforced on this kernel), the password agent;
* the Debian package builds with Rust 1.85 and is lintian-clean;
  `systemd-analyze verify` accepts the units;
* checked once by hand, not part of the test suite: with the package
  installed, `mkinitramfs` (initramfs-tools 0.142 and cryptsetup-initramfs
  from Ubuntu 24.04, kernel 6.8 modules) produces an initramfs that contains
  both programs, `/usr/bin/plymouth` as a link to the client, and the three
  scripts in the right order (`init-bottom` after `udev`); inside that
  initramfs the distribution's own `askpass` got the passphrase from the
  packaged daemon.

Not verified (no graphics device, no boot, in this environment):

* the DRM/KMS output on any driver, the frame buffer output, the switch
  from the firmware frame buffer to the real driver, multiple displays;
* how the animation looks in motion on a real display: frames were reviewed
  as still pictures and the path of the dots is tested as numbers, but
  nothing here has watched the ring turn, the caret blink or the box shake
  at 30 frames a second on a panel;
* console graphics mode, Caps Lock detection and key input on a real
  virtual console (the tests use a pseudo-terminal);
* the same with Debian 13's initramfs-tools, and the scripts running in a
  booting initramfs; the root switch (`new-root`); the systemd units at
  work (ordering, shutdown screen, hand-over to the greeter);
* a real LUKS unlock, in QEMU and on hardware.

The logic above these device layers (loss, recovery, hand-over, fallbacks)
is tested with a mock display.
