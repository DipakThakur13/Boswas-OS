# boswas-bootsplash

The Boswas OS boot screen: the mark, which assembles itself as the screen
opens, on a dark background lit softly behind it in the brand's blues, with
a progress ring close under it while the system starts; the passphrase
prompt for an encrypted disk, which slides in as the mark glides up; and the
shutdown and restart screens. Everything is drawn at run time from vector
outlines at the exact size of the display, and moves at its refresh rate.
It follows Boswas UI, the Fluent design language of the desktop
(`desktop/ui/tokens.json`). Two small Rust
programs take the place of Plymouth and of the Plymouth theme in
`desktop/branding/plymouth/`.

| Program | Installed as | What it is |
|---------|--------------|------------|
| `boswas-bootsplashd` | `/usr/libexec/boswas/boswas-bootsplashd` | The daemon: owns the display, the console keyboard and the control socket |
| `boswas-bootsplash` | `/usr/bin/boswas-bootsplash`, and `/bin/plymouth` **inside the initramfs only** | The client: `ping`, `ask-password`, `message`, `mode`, `hide`, `show`, `quit`, the systemd password agent |

**Status.** Everything that can run without a graphics device is tested
here (see [Tests](#tests)). The DRM/KMS back end, the initramfs scripts and
the systemd units have been booted in a virtual machine (QEMU, emulated
`bochs` graphics, with and without a firmware frame buffer first). Nothing
has run on real graphics hardware yet, and not with Debian 13's own
initramfs-tools. [What is verified](#what-is-verified-and-what-is-not) lists
both sides.

## What the user sees

| Screen | Content |
|--------|---------|
| Boot | The official mark, unchanged in shape and colour, and nothing written: no name. Its ink is 16 % of the display's shorter side high (200 logical pixels at most), its middle at 44 % of the height. Behind it the background is lit by a soft bloom (see below), and a warm glow surrounds the mark's dot. Close under the mark (three tenths of its height below it) a progress ring 34 pixels across: a white arc with round ends, 3.5 pixels wide, that lengthens from a short dash to three quarters of the ring and shortens again while it turns, once round in two seconds. When the boot reports a percentage, the ring shows it instead: an arc from 12 o'clock over that share of the ring, on a faint full ring (white at 16 %), gliding to each new value in a quarter of a second. While the ring turns, the dot's glow rises and falls gently, once every four seconds. A message from the boot scripts, when one is sent, is one line 14 pixels under the ring (body text, `text-secondary`; an error in `danger`) and goes away after six seconds (an error after thirty, or with the next passphrase); a reported progress is also a percentage on that line |
| Intro | Once, when the boot screen starts (1.6 seconds): the mark fades in and grows from 94 % to its size; its stem rises from a disc at its foot, its bowl sweeps round from the upper cut over the top to the lower cut with a soft leading edge, and its dot pops in with a small overshoot and a warm glow that settles. Then the ring fades in under it. Only the mark's rectangle (and at the end the ring's) is redrawn |
| Disk unlock | The mark glides up and shrinks to three quarters while the ring fades out, and the prompt fades in and rises the last 12 pixels into place (0.32 seconds; the mark moves first, the prompt comes in once the mark is nearly there). The mark then heads a column centred a little above the middle: "Unlock your disk" (subtitle), the name of the disk (body, `text-secondary`), and a text box: 320 by 36, corners rounded by 4, the translucent white fill and one-pixel outline of a control (`fill.dark.control` and `stroke.dark.control` over the background), and the two-pixel accent line (`iris.400`) of a focused one along its bottom edge. One dot per character typed, from the left with 12 pixels of padding (24 at most, so a long passphrase does not show its length), then a caret that blinks. Under the box a hint (caption, `text-muted`): "Press Enter to unlock", or "Caps Lock is on" in `warning`. When the prompt is answered or withdrawn, the reverse: it fades out with its dots, and the mark glides back down to the ring |
| Rejected passphrase | The prompt returns with "That passphrase did not work. Try again." in `danger` under the box (the hint moves down a line), and the box shakes sideways for 0.4 seconds |
| Shutdown, restart | The mark and the ring where they are at boot, and under the ring one line (body-large): "Shutting down" or "Restarting"; a message, when sent, under that. No intro: everything fades in together in a quarter of a second |
| The end | `quit`: everything fades to the background in a fifth of a second, then the text console returns. `quit --retain` (the hand-over to the login screen): only the ring and the text fade; the mark stays, its glow calm (and complete, should the intro still be running), and that is the frame left for the next program |

The background is `neutral.950` with two soft blooms, as on the boot menu
(`desktop/branding/boot-splash.svg`): `azure.500` up and to the left of the
mark, where the light comes from (20 % at its centre), and `iris.500` below
it to the right (13 %), each fading smoothly to nothing over 3.75 and 3.4
times the side of the mark's square box. To keep the gentle shading free
of bands on 8-bit displays it is dithered: noise of just under half a step,
fixed for each pixel, is added before the colour is rounded, so the average
of a few pixels follows the exact shade and a flat area stays exactly
`neutral.950`. The glow round the dot is `sun.400`, at most 2.1 times the
dot's radius (it fills the counter and stops at the bowl), 20 % at rest and
30 % at the top of a breath. The text is white (`text` of the dark theme)
in Inter, the Boswas UI typeface. Colours, the type ramp (caption 12, body
14, body-large 16, subtitle 20 semibold), the control radius, the control
fill and outline, and the motion curves and durations come from
`desktop/ui/tokens.json` at build time; the mark's colours from its SVG.

Motion is eased with the timing curves of the token file: `decelerate` for
what appears and settles (the mark's fade, its growth, the stem, the glide,
the prompt, a progress), `accelerate` for what leaves (the fade at the end),
`standard` for the bowl's sweep and the glow settling, and a small overshoot
for the dot. Everything that moves is a function of the clock
(`src/motion.rs`), so a busy machine shows fewer frames, never slower or
uneven motion.

Sizes are logical pixels. A logical pixel is one display pixel up to 1080
lines on the shorter side and grows with it above that (1.33 at 2560x1440,
1.48 at 2560x1600, 2 at 3840x2160, at most 3.2), so the text box is 320 by
36 real pixels at 1024x768, 1366x768, 1920x1080 and on a 1080x1920 portrait
display, and 640 by 72 at 4K. A display with fewer than 600 lines, or
narrower than 480 pixels, gets a smaller unit so that the layout fits.
Outlines, the caret and the distance between dots are whole pixels at every
scale. (The shorter side, rather than the height, decides since this
version: portrait displays had units of 1.78 at 1080x1920.)

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
after Enter and then ask again, so between the two the prompt glides away
and the boot screen with its ring shows while the passphrase is checked.

Keys while a prompt is open: characters, Backspace, Ctrl-U (clear), Enter.
They are taken at once, also while the prompt glides in. Escape switches
between the boot screen and the text console at any time.

Kernel command line: the boot screen runs only with `splash`. `nosplash`
(or `boswas.splash=off`, or `plymouth.enable=0`) turns it off;
`boswas.splash=static` keeps the screen without animation: the mark is
whole at once, the ring stands still (a three-quarter arc, or the reported
share), nothing glides or fades, the glow does not breathe, the caret does
not blink and the box does not shake.

Review frames and an animated preview:

```sh
cargo run --release --example frames -- /tmp/frames 1920x1080 1366x768 3840x2160 1080x1920
```

For each size it writes the states as PNG files (boot, boot-static,
boot-message, boot-progress, twelve frames of the intro, six of the ring's
cycle and three consecutive ones, three of the glide, the unlock screen
empty, typed, rejected and shaking, shutdown, its fade-in and restart, two
frames of the fade at the end and its last one, and the retained frame), a
contact sheet of the intro, the ring and the glide (`WxH-contact.png`), and,
where `ffmpeg` is installed, `WxH-motion.gif`: a scripted boot (intro, ring,
a passphrase typed, rejected and typed again, the hand-over) at 25 frames a
second, the middle of the screen scaled to 720 pixels across.

## How it renders

Nothing is parsed at boot, and nothing is a picture scaled to fit: what is
embedded is vector geometry, drawn at run time at the exact pixel size of
each display.

* **Assets** (`build.rs`). The mark's SVG is read with usvg: its filled
  paths, flattened to line segments (no segment strays more than 0.02 of a
  pixel from the curve at the largest size the mark is ever drawn), with
  their paints (linear and radial gradients with their stops and
  opacities, in the mark's 256-unit view box), and the geometry of the
  three parts the intro moves, worked out from the paths: the stem's
  straight length, the bowl's centre, radius and the arc between its cuts,
  the dot's centre and radius. Paths are assigned to parts by the ids of
  their gradients (`boswas-mark-stem…`, `-bowl…`, `-dot…`; anything else
  appears with the mark as a whole), so a future change of the mark is
  followed automatically. The printable ASCII glyphs of Inter Regular and
  SemiBold are read with ttf-parser: outlines flattened the same way (in em
  units), advances and the pair kerning of the font's `kern` feature
  (GPOS). The colours, the type ramp, the control radius and layers and the
  motion curves come from the token file.
* **Rasteriser** (`src/raster.rs`). Exact-area coverage, as font
  rasterisers compute it: every edge adds the signed area it sweeps on each
  pixel row to an accumulation buffer, and a running sum along each row
  gives each pixel the exact share of it the shape covers (non-zero
  winding). No supersampling. Its buffers are kept, so drawing allocates
  nothing once they have grown.
* **The mark** (`src/mark.rs`) is filled path by path, back to front, with
  its gradients evaluated in its own units at every pixel (from a table of
  256 premultiplied colours per gradient), the shading overlays reusing the
  coverage of the shape below them. A test compares it with resvg's
  rendering of the same SVG: on average less than a third of a colour level
  apart, at most a few levels inside the shapes. During the intro the parts
  are moved as the geometry allows: the upper half of the stem moves down
  (the stem rises from a disc, its gradient stretched over its length), a
  soft-edged sweep round the bowl's centre reveals the bowl, the dot is
  scaled about its centre. Once drawn for a pose, the mark is kept and only
  copied until the pose changes.
* **Text** (`src/text.rs`): each glyph is rasterised from its outline at
  the exact size (sizes are not rounded to whole pixels) and at four
  sub-pixel positions the first time it is needed, and kept; pairs are
  kerned. Characters outside ASCII are drawn as `?`.
* **Background** (`src/backdrop.rs`): worked out per pixel (two blooms with
  a smooth fall-off and the dither), only on the rows and columns a bloom
  reaches, flat elsewhere. The first frame keeps what it painted where
  things move (the mark in both its places and the ring), and later frames
  copy it from there.
* **Ring**: an arc with round ends from its signed distance, anti-aliased,
  over its track when it shows a percentage.

`src/scene.rs` keeps the picture as ten items in fixed slots (the dot's
glow, the mark, the ring, the status line, the message, title, name of the
disk, text box, caret, hint), each with its rectangle, its state and its
opacity in 256 steps. A frame compares the new items with the ones on
screen and repaints only the rectangles of items that changed, into an
XRGB8888 canvas; the output copies those rectangles to the display and
tells the driver which ones changed. A frame of the ring is the ring's
rectangle (its background and the arc); a step of the glow's breath is the
glow's square inside the mark's box; a blink of the caret is the caret's
own pixels; a keystroke or a frame of the shake is the text box; an intro
frame is the mark's rectangle (and, as it fades in, the ring's). Nothing
else is touched, and a frame that changes nothing is not sent to the
display at all.

| Release build, this machine | 1920x1080 | 3840x2160 |
|---|---|---|
| First frame (background, mark, everything) | 20 ms | 82 ms |
| Frame of the ring | 10 µs: 1,296 pixels (36 by 36), 0.062 % of the screen | 34 µs: 4,900 pixels, 0.059 % |
| A second of the ring at 240 frames a second | 2.4 ms | 8.1 ms |
| Step of the glow's breath (at most about 30 a second) | 25 µs: 3,481 pixels | 90 µs: 12,996 pixels |
| Intro frame | 0.35 ms (median): at most 23,824 pixels, 1.15 % | 1.4 ms: at most 93,292 pixels, 1.12 % |
| The whole intro at 240 frames a second (384 frames) | 0.11 to 0.18 s | 0.43 to 0.45 s (about 0.11 s at 60 Hz) |
| The glide to the unlock screen at 240 frames a second | 34 ms | 0.13 s |
| Keystroke in the text box, or a frame of its shake | 0.18 ms: 12,844 pixels, 0.62 % | 0.72 ms: 49,876 pixels, 0.60 % |
| Blink of the caret | 1.2 µs: 18 pixels | 2.5 µs |

At 1366x768 a frame of the ring is the same 1,296 pixels (0.124 % of the
screen). `tests/perf.rs` prints these (`cargo test --release --test perf --
--nocapture`) and fails if a frame of the ring redraws anything but the
ring, a step of the breath anything but the glow, a blink anything but the
caret, a keystroke or a shake anything but the text box, an intro frame
anything but the mark and the ring; if the ring is a quarter of a percent
of the screen or more, an intro frame five percent, a keystroke two; or if a
second of the ring at 240 frames costs half a second of processor time. The
limits are loose enough for an unoptimised build (there, a frame of the
ring is 64 µs at 1080p and the intro at 4K 3.9 s at 240 frames a second).
Rendering a frame of the ring allocates no memory (the picture borrows the
prompt and the message; the list of changed rectangles, the line of text
being drawn and the rasteriser's buffers are reused).

The animation is paced to the display: one frame per refresh of the mode
the display shows (as a rule the one the boot screen found on it, see
below), 240 frames a second at most, and 60 on the legacy frame buffer,
which does not tell its refresh rate. The rate is worked out from the
mode's timings. Frames keep a fixed pace and everything that moves comes
from the clock, so a busy system makes it update less often but not faster
or uneven. While a settled prompt is open the loop wakes only when the
caret is due to change, four times a second for Caps Lock, and for the
frames of a shake; while the glide runs, at the display's pace.

The ring is arithmetic (`ring_arc` in `src/scene.rs`): in each two-second
cycle the arc's head runs 256 degrees ahead while its tail holds (from 14
to 270 degrees), then the tail catches up, both runs eased; all the while
the ring turns steadily by the remaining 104 degrees, so the arc goes round
exactly once per cycle and cycles join without a seam. The cycle begins as
the ring fades in, with its shortest arc.

Inter is not in the repository. `build.rs` reads `Inter-Regular.otf` and
`Inter-SemiBold.otf` from `/usr/share/fonts/opentype/inter`, where the
Debian package `fonts-inter` installs them; it is a build dependency of the
Debian package, whose build fails without it
(`BOSWAS_BOOTSPLASH_REQUIRE_INTER=1` in `debian/rules`). A plain `cargo
build` on a machine without Inter prints a warning and takes the outlines
from the repository's brand typeface instead (Orbitron Medium and
SemiBold), so the crate still builds and its tests pass; `assets::TYPEFACE`
says which one a build has. `BOSWAS_BOOTSPLASH_INTER_DIR` names another
directory to take the two Inter files from. Only glyph outlines, advances
and kerning are embedded (Inter is under the SIL Open Font License 1.1;
`debian/copyright` carries its notice and the licence); no font file is
installed or copied into the initramfs.

| Embedded in the daemon | Points | Bytes (about) |
|---|---|---|
| The mark: six paths, their gradients, the geometry of its parts | 1,250 | 10 KB |
| Inter Regular: 95 glyphs, 1,047 kerning pairs | 6,235 | 64 KB |
| Inter SemiBold: 95 glyphs, 1,232 kerning pairs | 6,711 | 69 KB |

As built with Rust 1.85 and stripped, the two programs together are 1.44 MB
in the initramfs before compression (the daemon 0.96 MB, the client 0.48
MB) and 0.71 MB after `gzip -6`. (With the bitmap of the mark and the glyph
atlases of the previous version they were 1.76 MB and 0.61 MB: coordinates
compress less well than mostly empty bitmaps.) The two font files
themselves would add 1.23 MB (0.48 MB compressed). For the tests only,
`build.rs` also renders the mark with resvg; that rendering is not part of
the programs.

## Outputs

| Output | When | Notes |
|--------|------|-------|
| DRM/KMS dumb buffers (`src/output/drm.rs`) | A `/dev/dri/card*` with a connected display | No GPU acceleration, no libdrm (the `drm` crate issues the ioctls). Every connected display gets its own buffer and layout. The mode already set is kept when possible; the animation runs at that mode's refresh rate (the fastest display's, 240 frames a second at most). A buffer is filled before it is put on its display and again right after: the first mode-set can empty it (seen with `bochs` after it had replaced the firmware frame buffer; only the ring was visible then). What the display showed before (the kernel console) is put back on hide and quit |
| Frame buffer (`src/output/fbdev.rs`) | No DRM device, `/dev/fb0` exists | 32 bits per pixel with 8-bit channels, or RGB565. Before every frame the device is asked whether it is still the one that was opened; once it is not (a real driver replaced `efifb` or `vesafb`), nothing more is written into the old mapping and the daemon looks for the new device. It cannot keep a frame for the next program, so `quit --retain` is a plain quit here. Its refresh rate is unknown: frames are drawn 60 times a second. In RGB565 the background's gentle shading is truncated to 5 and 6 bits without dithering of its own and may show bands |
| Headless (`src/output/headless.rs`) | `--headless=WxH --out=DIR` | For tests and review; frames are saved on request |

Graphics devices change during boot: the firmware frame buffer
(`simpledrm`) is removed when the real driver loads. This is the normal
course of a boot on Intel, AMD and NVIDIA hardware whenever the real driver
loads after the boot screen has started. The daemon notices (any call on the
old device fails), looks for a new device every 250 ms and continues there
with a complete frame. If no display comes back within two seconds it
switches to the text console, so a prompt is never left where nobody can see
it, and returns to the boot screen when a display appears.

The new device is not where the sandbox allows the daemon to look: the
kernel removes `/dev/dri` with the old device and makes it again for the new
one, and a Landlock rule belongs to the directory it was made for, not to
its name. So the worker asks its supervisor, which is not sandboxed, and the
supervisor opens the devices and passes the open descriptors over a socket
pair (`src/broker.rs`). The supervisor looks names up relative to a
descriptor of `/dev` that it holds from its start (`/dev` is the same file
system for the whole boot), answers one request only ("the graphics
devices"), and passes nothing but character devices with the DRM or frame
buffer device number. The alternative, allowing all of `/dev` in the
sandbox, would give a compromised worker the disks.

## Processes

`boswas-bootsplashd` starts in three steps (`src/launch.rs`):

1. the **launcher** (the process the script started) reads the kernel
   command line, starts the supervisor, waits until the boot screen reports
   that it is up, and exits with that result;
2. the **supervisor** takes the lock of the runtime directory, starts the
   worker and waits. Meanwhile it opens graphics devices for the worker on
   request (see [Outputs](#outputs)). If the worker ends abnormally it puts
   the console back into text mode and removes the control socket and the
   pid file. It does that through the directory it opened at its start, so
   it works after the system has left the initramfs, where the supervisor's
   paths no longer lead anywhere;
3. the **worker** is the boot screen (`src/daemon.rs`): one single-threaded
   event loop over the control socket, the console and a timer.

Supervisor and worker run as `@boswas-bootsplashd`: systemd leaves processes
whose name starts with `@` alone in its final clean-up, which keeps the
shutdown screen up until the machine powers off.

There is one boot screen at a time, and a start never disturbs one that is
running. `/run/boswas-bootsplash/lock` is locked (`flock`) by the supervisor
for as long as it lives. A second start normally finds the boot screen
answering on its socket and only tells it which screen to show. If the boot
screen does not answer within three seconds (busy, or stopped), the second
supervisor finds the lock taken, starts no worker and touches nothing: not
the console, not the socket, not the pid file. A worker that finds a socket
still served although the lock was free (its supervisor was killed) leaves
in the same way, before it has touched the console. A supervisor removes the
socket and the pid file only if the pid file names its own worker or a
process that no longer exists.

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
| `QUIT`, `QUIT RETAIN seconds` | `OK`, once the screen has faded (a fifth of a second; at once without animation or while hidden) |
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
| The daemon dies while a prompt is open | The supervisor restores the text console and removes the socket and the pid file (also after the switch to the real root). The client exits; askpass returns an empty passphrase; the next attempt is asked on the console |
| The display disappears for more than two seconds | The prompt moves to the text console (typing is not echoed); the boot screen returns with the display |
| The real graphics driver replaces the firmware frame buffer | The boot screen continues on the new device, which its supervisor opens for it |
| Another program resets the console's terminal settings (systemd does, about two seconds after the switch to the real root) | Raw, no-echo input is put back: within a quarter of a second while the screen is shown, on every turn of the loop while a prompt is open, and always before a prompt opens |
| The boot screen is started while one is running that does not answer | Nothing is started and nothing of the running one is touched |
| The boot screen does not answer `quit` | The client terminates it (`SIGTERM`, then `SIGKILL`); the supervisor restores the text console. The process is the one listening on the socket, as the kernel reports it; no pid file is needed. If the socket itself is gone, the process named by the pid file is ended the same way, provided it is a boot screen |
| The initramfs gives up (panic) | `scripts/panic/boswas-bootsplash` quits the boot screen so the emergency shell is visible |
| The system enters emergency or rescue mode | A drop-in for `emergency.service` and `rescue.service` quits the boot screen before the shell starts |
| The console is hung up under the daemon (the kernel does it when a program that had the console as its controlling terminal ends, which every command of `emergency.service` does) | The daemon's descriptor of the console is dead from then on. The daemon and its supervisor open the console again, through the directory of its device node, before they read keys or change its mode; without that the console stayed in graphics mode and the emergency shell was invisible |

## Start-up and shut-down

| File | Does |
|------|------|
| `initramfs/hook` | Copies both programs into the initramfs, links the client as `/bin/plymouth` there, and copies the graphics drivers: every DRM driver with its firmware (`copy_modules_dir kernel/drivers/gpu/drm`, without `mga r128 savage sis tdfx via panfrost`, as Debian's Plymouth hook does), plus the firmware frame buffer drivers and the small drivers of virtual machines by name. With `MODULES=dep` only the drivers of the machine's own graphics devices are taken; with `MODULES=list`, none beyond the named ones |
| `initramfs/conf-hook` | `FRAMEBUFFER=y`. Ubuntu's initramfs-tools adds its own frame buffer drivers and set-up script for it; Debian's ignores it, which is why the hook copies the drivers itself. Nothing of this package depends on the option |
| `initramfs/init-premount` | Starts the daemon after udev has loaded the drivers and before the root device is unlocked |
| `initramfs/init-bottom` | `new-root $rootmnt`: the worker follows the system to its real root |
| `initramfs/panic` | Quits the boot screen |
| `boswas-bootsplash-start.service` | Starts the boot screen if the initramfs did not (wanted by `sysinit.target`). It runs once the device manager has started, which can be before a graphics driver has found its display, so the daemon is given eight seconds to wait for one (`--wait=8000`); it returns as soon as there is a display. See below |
| `boswas-bootsplash-quit.service` | `quit --retain` before `greetd.service`, `display-manager.service` and `getty@tty1.service` (wanted by `multi-user.target`). It has no condition on a file of the boot screen |
| `emergency.service.d/`, `rescue.service.d/50-boswas-bootsplash.conf` | `quit` before the emergency or rescue shell starts, so the shell is visible and has the keyboard |
| `boswas-bootsplash-poweroff.service`, `-halt.service`, `-reboot.service` | The shutdown and restart screens |
| `boswas-bootsplash-ask-password.path`, `.service` | A systemd password agent: later requests (a second encrypted volume) appear on the boot screen |
| `systemd-ask-password-console.path.d/50-boswas-bootsplash.conf` | Keeps systemd's console agent off while the boot screen runs; it would compete for the same keystrokes |

Waiting for a display in `boswas-bootsplash-start.service` was chosen over
ordering the unit after `systemd-udev-settle.service`: that service makes
every boot wait for all devices, and systemd discourages depending on it.
The unit is ordered after it (no dependency) for systems where something
else pulls it in. The wait costs nothing when a display is there, which
includes every boot where the initramfs already started the boot screen. A
machine with no display at all waits the eight seconds once per boot, with
`sysinit.target` behind the unit, and continues on the text console; with
`nomodeset` on the kernel command line no driver will load, and the wait is
the daemon's default of a second and a half.

`quit --retain` first lets the ring and the text fade (a fifth of a
second, before it answers, so the next unit starts after it), then gives up
the display and the keyboard but leaves the last frame, the mark on its
background, on the display until another program puts its own picture
there, for six seconds at most. What the user sees then depends on that program:

* The login manager greetd clears the text console of its virtual terminal
  and switches the console to text mode before it starts the login screen's
  compositor. The kernel's console is then the "other program": the frame
  goes away at that moment and the display shows an empty console until the
  compositor has drawn its first frame. `--retain` does not prevent that
  blank; it keeps the boot screen's picture up to the moment greetd takes
  the console, instead of dropping to the console's old contents first.
* A program that takes the display directly (a compositor started on the
  same virtual terminal without clearing it) replaces the frame with its
  own picture, with nothing in between.

The daemon exits as soon as its frame is no longer what the display shows.
If the six seconds pass first, it looks before it touches anything: when
another program holds the display (the display cannot be taken back) or
runs a graphical session on the console (the console's switching is under
that program's control, or its keyboard is off, which is what logind and
seatd set up), the daemon changes nothing on either and exits. Its frame
cannot outlive it (the kernel removes a program's frame buffer with the
program), so the display is dark from then until that program draws. Only
when neither is the case does it put the text console back. The same check
guards every place where the daemon or its supervisor would put the console
into text mode: `hide`, the fallback after a lost display, a plain `quit`,
termination by a signal, and the clean-up after a worker that died. A boot
screen is never the reason a compositor cannot take the display, or loses
it.

## What the daemon gives up

After the display, the console and the socket are open (`src/sandbox.rs`):

* `no_new_privs`;
* a Landlock domain: file access only below `/dev/dri`, to `/dev/fb0`, to
  the console device it reads keys from (to open it again after a hang-up)
  and in `/run/boswas-bootsplash`; no TCP (Linux 6.7 and later); where the
  kernel supports it, no signals to and no abstract sockets of other
  processes. The rules name the
  directory and the device as they were at that moment. Graphics devices
  that appear later under a new `/dev/dri` reach the worker as open
  descriptors from its supervisor (see [Outputs](#outputs)); no rule is
  added for them;
* all capabilities except `CAP_SYS_ADMIN` (to become DRM master again after
  hiding or a driver change), `CAP_SYS_TTY_CONFIG` (console text and
  graphics mode) and `CAP_SYS_CHROOT` (`new-root`).

It is best effort: a kernel without Landlock still gets the rest.
`boswas-bootsplashd --self-test` enters the sandbox and reports what is
enforced (exit status 0: all of it; 3: the kernel cannot enforce all of
it, having no Landlock or a Landlock without network rules (before Linux
6.7), and the rest holds; 1: a denial is missing). There is no seccomp
filter.

Other properties: no network code; no environment variable changes the
daemon's behaviour and it clears the environment of the processes it
starts; message and frame sizes are bounded; the passphrase lives in one
fixed buffer that is wiped when taken, cleared or dropped, is never
formatted or logged, and is wiped from the reply frame after sending.
`unsafe` code is confined to `src/sys.rs` (console and frame buffer ioctls,
`mmap` for the frame buffer, `signalfd`); the rest of the crate is built
with `#![deny(unsafe_code)]`. The supervisor is not sandboxed; it reads one
kind of message from the worker and no other input.

## Building

```sh
cargo build --release --locked      # needs Rust 1.85 (Debian 13) or newer
```

The crates in `Cargo.lock` are downloaded from crates.io, so the build needs
network access to it; this also applies to the Debian package build
(`debian/rules`). The Inter fonts come from the Debian package
`fonts-inter` on the build machine (see [How it renders](#how-it-renders)).

Run-time dependencies: `drm`, `rustix`, `libc`, `landlock`, `zeroize`.
Build-time only: `resvg` (with `usvg`), `ttf-parser`, `serde_json`.

## Tests

```sh
cargo test --locked
```

| Where | What |
|-------|------|
| Unit tests (`src/`) | The rasteriser (exact coverage of whole and partial pixels, total coverage equal to a polygon's area, non-zero winding, clipping, reused buffers); the mark drawn by it against resvg's rendering of the same SVG, at any size and position, and each stage of its assembly (the stem from a disc at its foot, the bowl's sweep in order round its centre, the dot growing and overshooting); the geometry of the mark's parts read from its SVG; flattening (the dot's area to 0.3 %); glyph outlines, kerning, and glyphs covering exactly their outline's area at any size and sub-pixel position; text fitting and fading; the background (flat away from the mark, the same however often it is painted, dithered so that its average follows the exact shade where rounding alone would band); the timing curves of the token file, the intro (in order, eased, ending exactly at rest), the fade-in, breathing, the glide there and back, a progress, the farewell with and without the kept frame, nothing moving with `--static`; the layout on eleven display sizes, portrait included (everything on the display, in order, on one axis; the mark, the ring and the text box at their sizes; the ring close under the mark; the unlock column); what each screen is made of, with the styles and colours of the token file; the ring's arc (both ends always moving clockwise, never jumping, once round a cycle) and a progress filling it from the top; damage (a frame of the ring only changes the ring, a breath only the glow, intro frames only the mark and the ring, the glide only the mark, the ring and the prompt, a blink only the caret, typing and the shake only the text box) and that a frame reached through any others is the frame painted from nothing; the prompt never over the mark during the glide; the fade to the background and the kept frame; the translucent text box over the bloom; dots, not characters, capped at 24; the rejected-passphrase line and the shake; Caps Lock; hostile text; the passphrase editor and key decoder; the protocol with malformed and random input; both command line dialects; the kernel command line; the frame buffer formats, and that nothing is written to a frame buffer that stopped answering; which rectangles go to a DRM buffer that was just put on its display; the password agent's parser; the lock, and removing files through the open runtime directory; passing graphics devices from supervisor to worker (order, a device directory that is made again, late and missing answers, only device nodes); raw mode put back after a reset of the terminal, a hung-up terminal opened again, how a console run by a graphical session is recognised; and the daemon on a mock display: what it presents for a frame of the ring, the intro, the glide in and out of a prompt (and that the loop then rests until the caret), a blink and a shake, a progress, the farewell before `quit` is answered, when its loop wakes, loss of the display, fallback to the console, recovery, the console settings while shown, hidden and prompting, and the hand-over on `quit --retain`, including a display that another program holds when the time is up |
| `tests/daemon.rs` | The real programs, headless, with a pseudo-terminal as the console: requests change the picture (message, error, progress on the line and the ring, shutdown, restart); the intro stays in the mark's and the ring's rectangles, then the ring (and the glow) in theirs; `quit` is answered after the fade, at once without animation; hostile input; passphrase entry through the client called `plymouth`, with the caret blinking, a prompt that arrives during the intro; a rejected passphrase is reported on the unlock screen, and the picture is the same for any passphrase of a length; prompts queue and are withdrawn when a client dies; Escape; raw mode returns after the terminal was reset, and a passphrase typed after a reset is not echoed; launcher, supervisor and worker (a killed worker leaves a restored console and no socket, also when the supervisor's paths no longer lead to them); a second start, and a worker in the foreground, leave a stopped boot screen untouched; `quit` ends a stopped boot screen; the sandbox; the systemd password agent |
| `tests/packaging.rs` | The unit files, the initramfs hook and scripts and `debian/rules` as text: the start unit's wait, no file condition on the hand-over, the emergency and rescue drop-ins, the hook copying the DRM drivers, no script behind an option Debian never sets |
| `tests/askpass.rs` | Debian's real, unmodified `askpass` against the daemon, in a scratch root directory laid out like the initramfs (needs root and the helper; skipped otherwise): passphrase from the boot screen, FIFO next to it, daemon death, console fallback |
| `tests/perf.rs` | Frame cost and damage size: the first frame, the intro, the ring, a breath of the glow, the glide, the caret, a keystroke, the shake |

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

Verified by tests (187 tests, Rust 1.85 and 1.97, release and unoptimised
builds; clippy clean):

* rendering, layout and damage, in tests and on review frames, looked at at
  1366x768, 1920x1080, 3840x2160 and 1080x1920 (portrait): every state, the
  intro frame by frame, the ring's cycle, the glide, the fades; the bloom
  checked for bands with its contrast raised; the motion seen as the
  animated preview's frames and contact sheets;
* the vector mark against resvg's rendering of the same SVG, and the
  coverage of glyphs against their outlines' area;
* the control protocol, both client dialects, passphrase entry and editing
  through a terminal;
* the unmodified Debian `askpass` (cryptsetup 2.7.5) obtaining the
  passphrase through the client installed as `/bin/plymouth`, the FIFO
  path, and the console fallback;
* start-up chain, the lock, clean-up after a killed worker, a second start
  against a stopped boot screen, `quit` against a stopped boot screen,
  sandbox enforcement (Landlock fully enforced on this kernel), the password
  agent;
* the Debian package builds with Rust 1.85 and is lintian-clean.

Verified by booting a virtual machine (QEMU without KVM, `-vga std`, kernel
6.8 with Ubuntu 24.04's userland and initramfs-tools 0.142, systemd 255,
greetd 0.9; screen dumps and the state of console, devices and processes
read from inside the machine):

* a plain boot: the boot screen is up about ten seconds after the virtual
  machine starts and stays until the hand-over; `quit --retain`, then
  greetd's empty console, then whatever the login manager shows;
* `vga=0x318` (firmware frame buffer, replaced by `bochs` in the initramfs
  before the daemon starts): the complete picture, where only the ring was
  visible before;
* `vga=0x318 modprobe.blacklist=bochs` and the driver loaded twelve seconds
  into the real root, with `/dev/dri` removed and made again by the kernel:
  the worker is refused the new directory by Landlock, gets the new device
  from its supervisor, and the boot screen continues at the new resolution;
* no graphics device when `boswas-bootsplash-start.service` runs and the
  driver loaded three seconds later: the boot screen appears; with no
  device at all the unit takes its eight seconds and the boot continues on
  the text console;
* the worker stopped (`SIGSTOP`) while the start unit runs: nothing is
  started, its console, socket and pid file are untouched, and it carries on
  when continued; never continued: the hand-over unit ends it and the
  console is back in text mode before greetd starts;
* the worker killed after the switch to the real root: its supervisor
  removes the socket and the pid file and restores the text console;
* raw keyboard mode is still set thirty seconds after the switch to the
  real root, and Escape hides and shows the boot screen at once;
* emergency mode (`systemd.unit=emergency.target`): the boot screen ends,
  the console is in text mode and the emergency shell's prompt is on it
  (the console had been hung up under the daemon by then);
* a stand-in for a compositor takes the console during `quit --retain`
  (graphics mode, keyboard off, switching under its control) and draws
  nothing: when the time is up the daemon exits and the console is still
  as that program set it;
* the frame buffer output on the frame buffer device of `simpledrm`: the
  picture is in the device's memory (read back), and when `bochs` replaces
  the device the daemon stops using it at once (it unmaps and closes it)
  and continues on the new DRM device.

Not verified:

* this version of the picture (vector mark, bloom, ring, intro, glides and
  fades) in a virtual machine or on a display at all: the boots above were
  made with the previous one (a bitmap of the mark and a ring of dots). The
  device layers and the start-up did not change; the hand-over now waits a
  fifth of a second for the fade before `quit --retain` is answered;
* any real graphics hardware: i915, amdgpu, radeon, nouveau, hybrid
  graphics, multiple displays, and efifb or vesafb as the frame buffer;
* that the frame buffer output is seen on a display: the test machine's
  only frame buffer device is the one a DRM driver emulates, and with the
  kernel's deferred console take-over that device is not put on the display
  by drawing into it (nothing asks the driver to show it). The daemon uses
  a frame buffer device only when it finds no DRM device with a connected
  display;
* the hand-over to a compositor that actually takes the display (the test
  machine's login screen does not start; the rule that the daemon never
  takes a display or console back is tested with a mock display and with
  the stand-in above);
* Debian 13's initramfs-tools: the hook was run by Ubuntu's, where the
  initramfs grew from 65 MB to 143 MB because Ubuntu's `linux-firmware`
  contains every driver's firmware (41 MB of it for `nouveau` alone); what
  Debian's builds with the firmware packages of the Boswas image has not
  been measured. `MODULES=dep` has not been run;
* how the animation looks in motion on a real display (it has been seen as
  still frames and as a 25-frames-a-second preview made from them, not at a
  display's refresh rate), and how long the intro takes to draw on a slow
  machine with a large, fast display (0.45 s of this machine's processor
  for the 1.6 s intro at 3840x2160 and 240 frames a second);
* how the background's shading looks on a 16-bit (RGB565) frame buffer,
  where it is not dithered;
* Caps Lock detection on a real virtual console;
* a real LUKS unlock, in QEMU and on hardware; the shutdown and restart
  screens on a booted system.

The logic above the device layers (loss, recovery, hand-over, fallbacks)
is tested with a mock display.
