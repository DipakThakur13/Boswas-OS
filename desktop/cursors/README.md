# Boswas Cursors

The pointer cursor theme of Boswas OS: an XCursor theme named
`Boswas-Cursors` (shown as "Boswas Cursors"). Every shape is drawn in this
directory, from scratch, as one family: a white body with a thin dark
outline and a soft shadow to the lower right, in the manner of the pointers
of Windows 11. No drawing is copied or traced from another theme; only the
names of the cursor files are shared with other themes, because clients ask
for cursors by name.

| Path | What it is |
|------|------------|
| `tools/build_cursors.py` | The source of truth: draws every shape, knows its hotspot, its frames and its other names |
| `Boswas-Cursors/src/*.svg` | The generated drawings (committed; do not edit by hand) |
| `Boswas-Cursors/cursors.json` | Generated list of shapes: hotspot, frames, delays, alternate names (committed) |
| `Boswas-Cursors/index.theme` | Generated theme description (committed) |
| `tools/xcursor.py` | PNG reader and XCursor writer and reader (Python standard library) |
| `install.sh` | Renders the drawings and installs the theme into a `DESTDIR` |
| `tools/contact_sheet.py` | Renders a review sheet from an installed theme |
| `tests/` | Unit tests (standard library only) |

## Design rules

* **One family.** White fill, outline one unit wide, the same shadow under
  every shape (offset 0.6/1.2 units, Gaussian blur 0.9, 34 % black). Outer
  corners are round because the outline is; inner corners are sharp.
* **Colours from the tokens.** `desktop/ui/tokens.json` gives the fill
  (`white`), the outline (`neutral.950`) and the one accent: the turning arc
  of the busy ring uses the light theme's `accent` (`iris.600`), fading to
  white along its tail. The generator holds no colour value of its own
  except the black of the shadow.
* **Grid.** 32 x 32 units for the nominal size 24, so one unit is one pixel
  at the default size and the arrow is about 12 x 19 pixels there. Edges
  that follow an axis sit on whole units: they are sharp at the nominal
  sizes 24, 48 and 96 and soft by a third of a pixel at 32 and 64.
* **Parts.** A shape is a list of parts (filled paths, or lines with round
  ends). Each part is drawn twice: in the outline colour, one unit larger on
  every side, then in white. Parts that overlap therefore merge into one
  silhouette with one outline: the fingers and the palm of a hand, the two
  arrows of `move`, the ring and the bar of `not-allowed`.
* **Thin shapes** (`text`, `vertical-text`, `crosshair`) have a white core
  one unit wide, centred on the pixel 15; the double arrows have a shaft two
  units wide, centred between the pixels 15 and 16.
* **Hotspots** are stored in grid units; `install.sh` turns them into the
  pixel that contains that point at each size. The arrow's hotspot is the
  tip of its outline, the hand's the tip of the index finger, the
  magnifier's the middle of the lens, the pencil's its point.
* **Animation.** `wait` and `progress` turn once in 0.96 s: 24 frames of
  40 ms. Only the arc moves; the ring and, in `progress`, the arrow are the
  same in every frame.

## Generating

```sh
python3 desktop/cursors/tools/build_cursors.py            # rewrite Boswas-Cursors/
python3 desktop/cursors/tools/build_cursors.py --check    # exit 1 if the committed files differ
python3 desktop/cursors/tools/build_cursors.py --table    # the table of shapes below
```

Re-run the generator after changing a drawing or `desktop/ui/tokens.json`,
and commit the result. `install.sh` and the tests refuse sources that differ
from the generator.

## Installing

```sh
sh desktop/cursors/install.sh DESTDIR
```

needs `python3` and `rsvg-convert` (librsvg2-bin) and writes

```
DESTDIR/usr/share/icons/Boswas-Cursors/index.theme
DESTDIR/usr/share/icons/Boswas-Cursors/cursor.theme               for the default-theme alternative
DESTDIR/usr/share/icons/Boswas-Cursors/cursors/<shape>         XCursor file
DESTDIR/usr/share/icons/Boswas-Cursors/cursors/<other name>    symbolic link to a shape
```

Each drawing is rendered at the nominal sizes 24, 32, 48, 64 and 96; the
images are 32, 43, 64, 85 and 128 pixels wide and high (the grid is a third
larger than the nominal size, as the arrow sits in a corner of it). The
XCursor files are written by `tools/xcursor.py`; `xcursorgen` is not needed.
There is no timestamp in the output: with the same librsvg, two runs give
the same bytes. The installed theme is about 9 MB, of which `wait` and
`progress` are 2.9 MB each (24 frames at five sizes, uncompressed like every
XCursor file).

`index.theme` has no `Inherits` line: every name the desktop asks for is in
the theme, and nothing is taken from another theme.

`cursor.theme` is what Debian's `x-cursor-theme` alternative points
`/usr/share/icons/default/index.theme` at (`Inherits=Boswas-Cursors`). The
package that installs the theme, boswas-branding, registers it, so programs
that load the theme called `default` (GTK and X11 clients that do not read
`XCURSOR_THEME`) get the same pointers as the compositor.

To look at the result:

```sh
python3 desktop/cursors/tools/xcursor.py info DESTDIR/usr/share/icons/Boswas-Cursors/cursors/default
python3 desktop/cursors/tools/contact_sheet.py /tmp/sheet.png                  # sizes 32 and 64, light and dark
python3 desktop/cursors/tools/contact_sheet.py /tmp/zoom.png --sizes 24 --zoom 8 --hotspots
```

## Shapes

28 shapes are drawn; every other name is a symbolic link to one of them.
The hotspot is in grid units.

| Shape | Shows | Hotspot | Alternate names (links) |
| --- | --- | --- | --- |
| `default` | arrow | 2.5, 2.5 | `arrow`, `left_arrow`, `left_ptr`, `top_left_arrow` |
| `context-menu` | arrow with a menu | 2.5, 2.5 | none |
| `help` | arrow with a question mark | 2.5, 2.5 | `5c6cd98b3f3ebcb1f9c7f1c204630408`, `d9ce0ab605698f320427677b458ad60b`, `dnd-ask`, `left_ptr_help`, `question_arrow`, `whats_this` |
| `pointer` | pointing hand | 11.5, 3.5 | `9d800788f1b08800ae810202380a0822`, `e29285e634086352946a0e7090d73106`, `hand`, `hand1`, `hand2`, `pointing_hand` |
| `progress` | arrow with a turning ring (24 frames, 40 ms each) | 2.5, 2.5 | `00000000000000020006000e7e9ffc3f`, `08e8e1c95fe2fc01f976f1e063a24ccd`, `3ecb610c1bf2410f44200f48c40d3599`, `half-busy`, `left_ptr_watch` |
| `wait` | turning ring (24 frames, 40 ms each) | 16, 16 | `0426c94ea35c87780ff01dc239897213`, `watch` |
| `cell` | thick plus | 15.5, 15.5 | `plus` |
| `crosshair` | thin cross | 15.5, 15.5 | `cross`, `cross_reverse`, `diamond_cross`, `tcross` |
| `text` | I-beam | 15.5, 15.5 | `ibeam`, `xterm` |
| `vertical-text` | I-beam on its side | 15.5, 15.5 | none |
| `alias` | arrow with a link badge | 2.5, 2.5 | `3085a0e285430894940527032f8b26df`, `640fb0e74195791501fd1ed57b41487f`, `a2a266d0498c3104214a47bd64ab0fc8`, `dnd-link`, `link` |
| `copy` | arrow with a plus badge | 2.5, 2.5 | `1081e37283d90000800003c07f3ef6bf`, `6407b0e94181790501fd1e167b474872`, `b66166c04f8c3109214a4fbd64a50fc8`, `dnd-copy` |
| `move` | four arrows | 16, 16 | `all-resize`, `fleur`, `size_all` |
| `no-drop` | arrow with a forbidden badge | 2.5, 2.5 | `dnd-no-drop` |
| `not-allowed` | crossed circle | 16, 16 | `03b6e0fcb3499374a867c041f52298f0`, `circle`, `crossed_circle`, `forbidden` |
| `grab` | open hand | 15.5, 14.5 | `5aca4d189052212118709018842178c0`, `openhand` |
| `grabbing` | closed hand | 15.5, 14.5 | `208530c400c041818281048008011002`, `4498f0e0c1937ffe01fd06f973665830`, `9081237383d90e509aa00f00170e968f`, `closedhand`, `dnd-move`, `dnd-none`, `fcf21c00b30f7e3f83fe0dfd12e71cff`, `fist` |
| `ew-resize` | horizontal double arrow | 16, 16 | `028006030e0e7ebffc7f7070c0600140`, `e-resize`, `h_double_arrow`, `left_side`, `right_side`, `sb_h_double_arrow`, `size_hor`, `w-resize` |
| `ns-resize` | vertical double arrow | 16, 16 | `00008160000006810000408080010102`, `bottom_side`, `n-resize`, `s-resize`, `sb_v_double_arrow`, `size_ver`, `top_side`, `v_double_arrow` |
| `nesw-resize` | double arrow, lower left to upper right | 16, 16 | `50585d75b494802d0151028115016902`, `bottom_left_corner`, `fcf1c3c7cd4491d801f1e1c78f100000`, `fd_double_arrow`, `ne-resize`, `size_bdiag`, `sw-resize`, `top_right_corner` |
| `nwse-resize` | double arrow, upper left to lower right | 16, 16 | `38c5dff7c7b8962045400281044508d2`, `bd_double_arrow`, `bottom_right_corner`, `c7088f0f3e6c8088236ef8e1e3e70000`, `nw-resize`, `se-resize`, `size_fdiag`, `top_left_corner` |
| `col-resize` | two bars with arrows left and right | 16, 16 | `043a9f68147c53184671403ffa811cc5`, `14fef782d02440884392942c11205230`, `split_h` |
| `row-resize` | two bars with arrows up and down | 16, 16 | `2870a09082c103050810ffdffffe0204`, `c07385c7190e701020ff7ffffd08103c`, `split_v` |
| `all-scroll` | dot with four arrow heads | 16, 16 | none |
| `zoom-in` | magnifier with a plus | 13, 13 | `f41c0e382c94c0958e07017e42b00462` |
| `zoom-out` | magnifier with a minus | 13, 13 | `f41c0e382c97c0938e07017e42800402` |
| `up-arrow` | arrow that points up | 15.5, 3.5 | `center_ptr`, `sb_up_arrow`, `up_arrow` |
| `pencil` | pencil | 4.6, 27.4 | none |

All 34 names of the CSS `cursor` property are there, 26 as drawings and 8
as links; these are the names the compositor asks for (crate `cursor-icon`,
`packages/boswas-desktop/crates/boswas-compositor/src/cursor.rs`). The theme
has 128 names in all: 28 files and 100 links.

### What is a link to another shape

* **The eight directions** (`e-resize`, `n-resize`, `ne-resize`,
  `nw-resize`, `s-resize`, `se-resize`, `sw-resize`, `w-resize`) and the
  X11 names of the window edges and corners (`left_side`,
  `top_left_corner`, ...) are links to the four double arrows. There is no
  drawing of an arrow that points one way to an edge.
* **Drag and drop.** `dnd-copy` is `copy`, `dnd-link` is `alias`,
  `dnd-no-drop` is `no-drop`; `dnd-move` and `dnd-none` are the closed hand
  (`grabbing`); `dnd-ask` is the arrow with the question mark (`help`).
* **X11 and toolkit names** (`left_ptr`, `xterm`, `watch`, `hand2`,
  `size_ver`, `split_h`, ...) are links to the shape with the same meaning.
  `tcross`, `cross_reverse` and `diamond_cross` are the plain `crosshair`;
  `plus` is `cell`; `fist` is `grabbing`; `circle` is `not-allowed`.
* **`pencil`** is a drawing of its own, not a link to `default`.
* **`up-arrow`** (Qt's `up_arrow`, X11's `center_ptr` and `sb_up_arrow`) is
  a drawing of its own; it is not a CSS name.

Not in the theme: `blank` (Qt and the compositor hide the pointer
themselves), the other X11 font cursors (`X_cursor`, `pirate`, `gumby`,
...), and cursors for single directions as said above.

### Names with two meanings

A few legacy names do not mean the same thing everywhere. Qt 6 tries a list
of names per cursor and takes the first that exists; with this theme the
first name of every list exists, so the later ones do not matter to Qt.

| Name | Here | Elsewhere |
| --- | --- | --- |
| `sb_h_double_arrow`, `sb_v_double_arrow` | the plain double arrows (`ew-resize`, `ns-resize`), as in the X11 cursor font | Qt lists them for the splitters, after `split_h`, `col-resize` and `split_v`, `row-resize` |
| `fleur` | `move` (four arrows) | Qt lists it for the open hand, after `openhand` and `grab` |
| `9d800788f1b08800ae810202380a0822` | `pointer`, as cursor themes link it | Qt lists it for the open hand, last, after `openhand` |
| `move` | four arrows (the CSS meaning) | Qt lists it for a move by drag and drop, after `dnd-move` (here the closed hand) |
| `hand1` | `pointer`, as Qt lists it | some themes link it to the open hand |
| `circle` | `not-allowed`, as Qt lists it | the X11 cursor font draws a plain circle |

### Hash names

Old X11 clients (Qt 3, Qt 4, KDE 3) ask libXcursor for a cursor by the hash
of its bitmap, 32 hexadecimal digits. 32 such names are links here.

* 20 of them were read from the name lists inside the Qt 6 libraries
  installed on the machine the theme was written on (`libQt6XcbQpa`,
  `libQt6WaylandClient`); the list says which cursor each belongs to. Tests
  check that Qt's lists resolve to the right shapes.
* 12 are written from memory of the lists that common cursor themes carry
  and could not be checked against a second source here:
  `3ecb610c1bf2410f44200f48c40d3599` (`progress`);
  `3085a0e285430894940527032f8b26df`, `640fb0e74195791501fd1ed57b41487f`,
  `a2a266d0498c3104214a47bd64ab0fc8` (`alias`);
  `1081e37283d90000800003c07f3ef6bf`, `6407b0e94181790501fd1e167b474872`,
  `b66166c04f8c3109214a4fbd64a50fc8` (`copy`);
  `4498f0e0c1937ffe01fd06f973665830`, `9081237383d90e509aa00f00170e968f`,
  `fcf21c00b30f7e3f83fe0dfd12e71cff` (`grabbing`, as the move cursor of a
  drag); `f41c0e382c94c0958e07017e42b00462` (`zoom-in`);
  `f41c0e382c97c0938e07017e42800402` (`zoom-out`). A wrong digit in one of
  these would only leave a link nobody asks for.

## Tests

```sh
cd desktop/cursors && python3 -B -m unittest discover -s tests
```

* `test_generated.py`: the committed files match the generator; `--check`
  reports edited, missing and stale files; the manifest has every CSS and
  legacy name once; the SVGs use only the token colours and stay inside the
  grid; this README's table matches `--table`.
* `test_xcursor.py`: the PNG reader with all five row filters and the other
  colour types, premultiplication, the exact bytes of an XCursor file, what
  the reader and the manifest check refuse, links and replaced files. When
  `xcursorgen` is installed, the writer's output must equal its output byte
  for byte; when libXcursor is installed, it must read the files back.
* `test_install.py`: `install.sh` refuses a missing `DESTDIR`, a missing
  renderer and edited sources. With `rsvg-convert` it installs the theme
  into a temporary directory and checks every name, link, size, hotspot and
  delay, the pictures (not empty, not cut off, white with a dark rim, the
  arrow's size, colour only in the busy ring, the arc turning clockwise),
  that a second install gives identical bytes, and, when libXcursor is
  installed, that it finds every name of the theme at every size.

Without `rsvg-convert` the tests that install the theme are skipped; the
cross-checks are skipped without `xcursorgen` or libXcursor.

## What was not verified

* The theme has not been seen in a running session: not in boswas-compositor
  on real hardware, not in an Xwayland, GTK or Qt client. What was checked
  is the files: by the tests above, and once by hand with the Rust crates
  the compositor uses (`xcursor` 0.3.11, `cursor-icon` 1.2.0), which found
  and parsed all 34 CSS names plus `dnd-ask` and `all-resize` through
  `XCURSOR_PATH`.
* libwayland-cursor (GTK and Qt clients on Wayland) was not run against the
  theme; it reads the same format as libXcursor.
* The look was judged on rendered contact sheets (sizes 24 to 96, light and
  dark backgrounds), not on a display, and not on a HiDPI one.
* The drawings were rendered with librsvg 2.58. Another version may round a
  pixel of the blurred shadow differently; the output is then still
  reproducible for that version.
* The 12 hash names listed above.
* Whether the 40 ms frames of the busy ring look smooth in the compositor.
