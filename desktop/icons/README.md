# Boswas icon theme

Two icon themes with the same icons:

| Theme | Directory | Inherits | Used with |
|-------|-----------|----------|-----------|
| Boswas | `Boswas/` | `breeze-dark,breeze,hicolor` | Dark panels (the default Boswas look) |
| Boswas Light | `Boswas-Light/` | `breeze,hicolor` | Light presets, so symbolic tray icons fall back to dark-on-light Breeze |

The themes only draw application, settings-category and Windows-file icons.
Everything else (toolbar actions, places, status and tray icons, symbolic
menu-category icons) comes from Breeze through `Inherits`.

## Files

| Path | What it is |
|------|------------|
| `tools/build_icons.py` | The generator: every glyph is defined here, one function per design |
| `Boswas/scalable/apps/*.svg`, `Boswas/scalable/mimetypes/*.svg` | Generated icons (committed; do not edit by hand) |
| `Boswas/index.theme`, `Boswas-Light/index.theme` | Theme descriptions |
| `install.sh` | Installs both themes into a `DESTDIR` |
| `tools/contact_sheet.py` | Renders a review sheet of all icons (PNG, via `rsvg-convert`) |
| `tests/test_icons.py` | Unit tests (standard library only) |

`Boswas-Light/` holds only its `index.theme`: `install.sh` copies the icons
from `Boswas/` into both themes as real files (no symlinks).

## Design rules

The icons are one family with the Boswas OS launcher icon
(`desktop/branding/boswas-mark.svg`) and follow the palette in
`desktop/branding/README.md`.

* **Grid.** 256x256 viewBox, scalable SVG. KDE renders them at 16 to 512 px;
  they are checked at 256, 48, 32, 22 and 16 px.
* **Tile.** Applications and settings categories sit on the launcher tile:
  a rounded square `x=8 y=8 240x240 rx=56`, gradient Navy `#162034` to
  `#080C16` (top left to bottom right), with a 3 px gold edge (`#FBD693` at
  85 % to `#B5935C` at 45 %). The edge keeps the tile visible on dark panels.
* **Glyph.** Bold geometric line art, stroke width 14, round caps and joins,
  inside roughly 56..200. Silver gradient `#D4DDEC` to `#6E7A8F`, lit from the
  top left; the same gradient vector (72,48 to 184,208) is used in every
  icon so they shade alike. Filled details (dots, knobs) use the same paint.
* **One gold accent.** Each icon has exactly one gold element (`#FBD693` to
  `#B5935C`, same vector): the part that carries the meaning (the cursor of
  the terminal, the pencil of the editor, the badge on a Windows icon).
* **Brand geometry.** The ring and dot of the mark recur where they fit (the
  hub of the settings gear, the lens of the camera, the update center), and
  the "i" of System Information has the blade-shaped stem of the mark. The
  logo itself appears only in `boswas-logo` and `boswas-install`, and its
  path data is copied from `desktop/branding/boswas-os-mark.svg` at build
  time, never redrawn.
* **Overlaps.** When an accent overlaps a line (a pencil over a page, a badge
  over a window), a halo in the tile gradient is drawn under it. The tile
  gradient is in user space, so the halo is invisible and only interrupts the
  line. No masks or clip paths.
* **Windows compatibility.** A generic application window (frame and title
  bar) with a gold badge: shield (application), download arrow (installer),
  run triangle (program loader), check (status), gear (Compatibility
  Manager), wrench (repair). No Microsoft logo, and no Wine, Firefox or KDE
  logos anywhere: the browser is a generic globe.
* **Mimetypes.** Flat light pages (gradient `#E8EEF6` to `#AEB7C7`, folded
  corner in silver, faint `#3C485C` outline for white backgrounds) with the
  application window in Navy 800 `#111A2B` and a gold badge. Same 256 grid
  as the apps.
* **Self-contained, Qt-safe SVG.** KDE draws icons with Qt's SVG renderer,
  which supports a subset of SVG. The icons use only paths, rectangles,
  solid colours and linear gradients in user space (a horizontal or
  vertical stroke has a bounding box without area, which an
  `objectBoundingBox` gradient would not paint). No `<image>`, external
  `href`, `<script>`, `<text>`, CSS, masks, clip paths, filters or nested
  `<svg>`. Every id is prefixed with the file's own name (for example
  `utilities-terminal-silver`), so files can be combined. UTF-8, LF.
* **Palette only.** Every colour comes from the brand palette; the retired
  teal accent is rejected by the tests (as by `tests/static/test_sources.sh`).

## Icons

61 designs, written as 96 files (aliases are separate files with the same
drawing and their own id prefix). This table is printed by
`tools/build_icons.py --table`; the tests check that it matches.

<!-- BEGIN ICON TABLE -->
| Design | Glyph | Theme icon names |
|---|---|---|
| Settings | Eight-tooth gear, gold hub. | `preferences-system`, `systemsettings` |
| System Information | Processor chip with pins, gold "i" (dot and blade stem). | `hwinfo` |
| Terminal | Console window with a prompt chevron, gold cursor. | `utilities-terminal`, `org.kde.konsole` |
| File Manager | Folder, gold pocket edge. | `system-file-manager`, `org.kde.dolphin` |
| System Monitor | Screen with a gold activity trace. | `utilities-system-monitor`, `org.kde.plasma-systemmonitor` |
| Web Browser | Globe with equator, gold meridian (a generic browser, not a vendor logo). | `web-browser`, `firefox-esr`, `konqueror` |
| Calculator | Calculator with plus, minus, times and equals keys, gold display. | `accessories-calculator`, `org.kde.kcalc` |
| Text Editor | Page with text lines, gold pencil. | `accessories-text-editor`, `kwrite`, `org.kde.kwrite`, `kate`, `org.kde.kate`, `gvim` |
| Image Viewer | Picture frame with mountains, gold sun. | `gwenview`, `org.kde.gwenview`, `preferences-desktop-wallpaper` |
| Document Viewer | Open book, gold bookmark ribbon. | `okular`, `org.kde.okular` |
| Archive Manager | Archive box with lid, gold handle slot. | `utilities-file-archiver`, `ark`, `org.kde.ark` |
| Screenshot | Viewfinder corners, gold shutter dot. | `spectacle`, `org.kde.spectacle` |
| Help | Ring, gold question mark. | `help-browser`, `org.kde.khelpcenter` |
| Find Files | Magnifying glass, gold handle. | `kfind` |
| Wallet | Wallet with a pocket flap, gold clasp. | `kwalletmanager` |
| Partition Manager | Disk as a pie, one gold partition pulled out. | `partitionmanager` |
| Menu Editor | Menu lines, gold pencil. | `kmenuedit` |
| Process Viewer | Console window with load bars, the tallest gold. | `htop` |
| Report a Bug | Beetle with legs and antennae, gold head. | `tools-report-bug` |
| Emoji | Smiling face, gold smile. | `preferences-desktop-emoticons` |
| Network | Wireless arcs, gold source dot. | `preferences-system-network` |
| Bluetooth | Bluetooth rune, gold pairing dots. | `preferences-system-bluetooth` |
| Audio | Speaker, gold sound waves. | `preferences-desktop-sound` |
| Display | Monitor on a stand, gold foot. | `preferences-desktop-display`, `preferences-desktop-display-randr` |
| Power | Battery, gold lightning bolt. | `preferences-system-power-management` |
| Users | Two people, the one behind in gold. | `preferences-system-users`, `system-users` |
| Storage | Two stacked drive units, gold activity light. | `boswas-storage` |
| Personalization | Paint palette, one gold paint dot. | `preferences-desktop-theme-global` |
| Privacy | Padlock, gold keyhole. | `preferences-system-privacy`, `preferences-desktop-user-password` |
| Devices | Mouse, gold scroll wheel. | `preferences-desktop-peripherals`, `preferences-desktop-mouse` |
| Updates | Two circling arrows around a gold download arrow. | `system-software-update` |
| Notifications | Bell, gold clapper. | `preferences-desktop-notification-bell` |
| Accessibility | Ring, gold figure with outstretched arms. | `preferences-desktop-accessibility` |
| Keyboard | Keyboard with two key rows, gold space bar. | `preferences-desktop-keyboard` |
| Language and Region | Speech bubble, gold letter A (drawn as strokes). | `preferences-desktop-locale` |
| Firewall | Brick wall, one gold brick. | `preferences-security-firewall` |
| Fonts | Letters "Aa" drawn as strokes, the small a in gold. | `preferences-desktop-font` |
| Window Management | Two overlapping windows, gold title bar on the front one. | `preferences-system-windows`, `preferences-system-windows-actions` |
| Calendar | Calendar page with binder rings and days, one gold day. | `office-calendar`, `x-office-calendar` |
| Clock | Clock face, gold minute hand. | `preferences-system-time` |
| Media Player | Ring, gold play triangle. | `multimedia-player`, `applications-multimedia` |
| Camera | Camera body with lens ring, gold lens centre. | `camera-photo` |
| Boswas OS | The official Boswas OS mark (copied from desktop/branding/boswas-os-mark.svg). | `boswas-logo`, `boswas-os`, `start-here`, `start-here-kde`, `start-here-kde-plasma`, `distributor-logo` |
| Boswas Store | Shopping bag, gold handle. | `boswas-store` |
| Compatibility Manager | Application window, gold gear. | `boswas-compat-manager` |
| Device Agent | Laptop, gold heartbeat on the screen. | `boswas-device-agent` |
| Security Center | Shield, gold check mark. | `boswas-security-center` |
| Control Center | Three sliders, the middle knob gold. | `boswas-control-center` |
| Update Center | Circling arrow around a gold dot (the ring and dot of the mark). | `boswas-update-center` |
| Software Center | Grid of application tiles, one a gold disc. | `boswas-software-center` |
| Install Boswas OS | The official mark with a gold download badge. | `boswas-install` |
| Windows Application | Application window, gold sandbox shield. | `boswas-windows-application` |
| Windows Installer | Application window, gold badge with a download arrow. | `boswas-windows-installer` |
| Windows Program Loader | Application window, gold badge with a run (play) triangle. | `wine`, `wine-stable` |
| Compatibility Status | Application window, gold badge with a check mark. | `boswas-compat-status` |
| Repair Application | Application window, gold wrench. | `boswas-repair` |
| Application Sandbox | Shield enclosing a gold application window. | `boswas-sandbox` |
| Application Policy | Clipboard holding an application window, gold clip. | `boswas-app-policy` |
| Windows Program (mimetype) | Page with an application window and a gold sandbox shield. | `application-x-ms-dos-executable`, `application-x-msdownload`, `application-vnd.microsoft.portable-executable`, `application-x-ms-ne-executable` |
| Windows Installer Package (mimetype) | Page with an application window and a gold download badge. | `application-x-msi` |
| Windows Shortcut (mimetype) | Page with an application window and a gold shortcut-arrow badge. | `application-x-ms-shortcut` |
<!-- END ICON TABLE -->

Notes on names:

* **Windows programs.** Debian's shared-mime-info names `.exe` and `.dll`
  files `application/vnd.microsoft.portable-executable` (a subclass of
  `application/x-msdownload`, whose alias is `application/x-ms-dos-executable`),
  and KDE looks up the icon of the exact type before its generic icon. The
  program page is therefore installed under all four names.
* **KDE System Settings** uses the `preferences-*` names for its modules, so
  those modules show Boswas tiles; modules without a Boswas icon keep
  Breeze's.
* Kickoff's menu categories use `*-symbolic` names and stay Breeze.

## Regenerating

The generator needs Python 3 (standard library only) and reads
`desktop/branding/boswas-os-mark.svg`:

```sh
python3 desktop/icons/tools/build_icons.py          # rewrite desktop/icons/Boswas/scalable
python3 desktop/icons/tools/build_icons.py --table  # print the table above
```

It is deterministic and removes SVGs it no longer generates. Commit the
regenerated files; the tests fail when they are stale.

## Adding an icon

1. Write a glyph function in `tools/build_icons.py` (`def g_name():`), with
   a one-line docstring describing the drawing. Return a list of elements
   built with the helpers: `stroke(d)` (silver line art, width 14),
   `stroke(d, GOLD)` or `solid(d, GOLD)` for the one accent,
   `fill(d)` for silver dots, `knock(d)` for a halo under an overlapping
   accent. Path helpers: `rr`, `circle`, `ellipse`, `arc`, `line`, `poly`,
   `polar`, `gear`, `shield`, `pencil`, `arrowhead`; `window()` and
   `badge_disc()` for Windows-compatibility icons.
2. Add a row to `ICONS`: theme name, title, glyph, `APPS` or `MIME`, aliases.
3. Run the generator, render a contact sheet (below) and look at the icon
   at 256, 48 and 32 px next to its neighbours.
4. Paste the new `--table` output between the markers above, add the name
   to the test's required list if a component depends on it, and run the
   tests.

## Previewing

`tools/contact_sheet.py` composes all icons at 128, 48 and 32 px with their
names and renders the sheet with `rsvg-convert`. Write previews outside the
repository (the tool refuses a path inside it):

```sh
python3 desktop/icons/tools/contact_sheet.py /tmp/boswas-icons.png
python3 desktop/icons/tools/contact_sheet.py /tmp/boswas-icons-light.png --light
python3 desktop/icons/tools/contact_sheet.py /tmp/boswas-icons-all.png --aliases
```

## Installing

```sh
sh desktop/icons/install.sh "$DESTDIR"
```

installs `$DESTDIR/usr/share/icons/Boswas/` and
`$DESTDIR/usr/share/icons/Boswas-Light/` (files 0644, directories 0755,
icons copied into both). It builds no icon cache: `dh_icons` (or
`gtk-update-icon-cache` in the package scripts) does that. Select the theme
with `Theme=Boswas` in the `[Icons]` group of `kdeglobals`.

## Tests

```sh
cd desktop/icons && python3 -B -m unittest discover -s tests -v
```

They check that every required name exists in both installed themes, that
every SVG parses, has the 256 viewBox, is self-contained and Qt-safe, uses
only the palette (no teal) and file-prefixed ids, that the logo carries the
official mark's path data, that aliases match their design, that both
`index.theme` files are valid, that `install.sh` installs real files with
the right modes, that the table above is current, and that the committed
SVGs match a fresh run of the generator byte for byte.
