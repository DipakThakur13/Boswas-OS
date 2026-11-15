# boswas-bootsplash

The Boswas OS boot screen: the mark and name with a progress arc while the
system starts, the passphrase prompt for an encrypted disk, and the shutdown
and restart screens. Two small Rust programs take the place of Plymouth and
of the Plymouth theme in `desktop/branding/plymouth/`.

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
| Boot | The official mark (gold and silver, unchanged), the BOSWAS OS wordmark, a progress arc: a comet from `iris.400` to `azure.400` on an `ink.700` track. Messages from the boot scripts appear as one muted line |
| Disk unlock | The same lockup; the arc gives way to the prompt text, a pill field (`ink.800`, border `ink.600`, focus ring `iris.400`) with one dot per character typed (24 at most, so a long passphrase does not show its length), a Caps Lock hint (`sun.400`) and the last error (`coral.400`) |
| Shutdown, restart | The lockup, the arc, and "Shutting down" or "Restarting" |

Background `ink.950`. Sizes follow the display height (the mark is 18 % of
it, between 96 and 448 pixels), so the layout is the same from 800x600 to
4K. The lockup never moves; everything that changes is in a band below it.
Colours come from `desktop/ui/tokens.json` at build time.

Keys while a prompt is open: characters, Backspace, Ctrl-U (clear), Enter.
Escape switches between the boot screen and the text console at any time.

Kernel command line: the boot screen runs only with `splash`. `nosplash`
(or `boswas.splash=off`, or `plymouth.enable=0`) turns it off;
`boswas.splash=static` keeps the screen without animation.

Review frames:

```sh
cargo run --release --example frames -- /tmp/frames 1920x1080 1366x768
```

## How it renders

`src/scene.rs` keeps the picture as eight items in fixed slots (mark,
wordmark, arc, status line, message, prompt text, field, hint). A frame
compares the new items with the ones on screen and repaints only the
rectangles of items that changed, into an XRGB8888 canvas; the output copies
those rectangles to the display and tells the driver which ones changed.

| At 1920x1080 (release build, this machine) | Cost |
|---|---|
| First frame (scale the artwork, paint everything) | 6.6 ms |
| Animation frame | 66 µs: 3,364 pixels, 0.16 % of the screen |
| Keystroke in the passphrase field | 0.5 ms |

At 3840x2160 an animation frame is 0.24 ms. `tests/perf.rs` prints these
(`cargo test --release --test perf -- --nocapture`) and fails if a frame
touches 1 % of the screen or more. The animation runs at 30 frames a second
at most, driven by the clock, so a busy system makes it slower to update but
not faster or uneven.

Nothing is parsed at boot. `build.rs` rasterises
`desktop/branding/boswas-os-mark.svg`, `wordmark-boswas-os.svg` and the
printable ASCII glyphs of Orbitron Medium, and embeds them as plain bytes;
text is scaled from that atlas with sub-pixel placement. Characters outside
ASCII are drawn as `?`.

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
newer compiler than Debian 13 has.

Run-time dependencies: `drm`, `rustix`, `libc`, `landlock`, `zeroize`.
Build-time only: `resvg`, `fontdue`, `serde_json`.

## Tests

```sh
cargo test --locked
```

| Where | What |
|-------|------|
| Unit tests (`src/`) | Drawing and scaling, layout on eight display sizes, damage (an animation frame only changes the arc; typing only the field), the passphrase editor and key decoder, the protocol with malformed and random input, both command line dialects, the kernel command line, the frame buffer formats, the password agent's parser, and the daemon on a mock display: loss of the display, fallback to the console, recovery, and the hand-over on `quit --retain` |
| `tests/daemon.rs` | The real programs, headless, with a pseudo-terminal as the console: requests change the picture; the animation stays inside the arc; hostile input; passphrase entry through the client called `plymouth`; prompts queue and are withdrawn when a client dies; Escape; launcher, supervisor and worker (a killed worker leaves a restored console and no socket); the sandbox; the systemd password agent |
| `tests/askpass.rs` | Debian's real, unmodified `askpass` against the daemon, in a scratch root directory laid out like the initramfs (needs root and the helper; skipped otherwise): passphrase from the boot screen, FIFO next to it, daemon death, console fallback |
| `tests/perf.rs` | Frame cost and damage size |

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

Verified here (107 tests, Rust 1.85 and current stable):

* rendering, layout and damage, on review frames and in tests;
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
* console graphics mode, Caps Lock detection and key input on a real
  virtual console (the tests use a pseudo-terminal);
* the same with Debian 13's initramfs-tools, and the scripts running in a
  booting initramfs; the root switch (`new-root`); the systemd units at
  work (ordering, shutdown screen, hand-over to the greeter);
* a real LUKS unlock, in QEMU and on hardware.

The logic above these device layers (loss, recovery, hand-over, fallbacks)
is tested with a mock display.
