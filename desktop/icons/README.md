# Boswas icon themes

Two icon packs, each as a pair of themes with the same icons:

| Pack | Themes | Palette | Generator | Draws |
|------|--------|---------|-----------|-------|
| **Boswas Prism** (the default of the native Boswas desktop) | `Boswas-Prism/`, `Boswas-Prism-Light/` | Prism, `desktop/ui/tokens.json` | `tools/build_prism.py` | Applications, places, devices, mimetypes, symbolic status and action icons |
| **Boswas** (legacy, navy and gold) | `Boswas/`, `Boswas-Light/` | Brand palette, `desktop/branding/README.md` | `tools/build_icons.py` | Applications and Windows mimetypes |

The two generators are independent: each draws its own shapes. The theme
names and the icon file names of both packs are an interface (the toolkit,
the shell, the compositor, the presets and the package tests use them) and
do not change when a design does. Everything a pack does not draw comes from
Breeze (and Adwaita, hicolor) through `Inherits`.

## Boswas Prism

| Theme | Directory | Inherits | Used with |
|-------|-----------|----------|-----------|
| Boswas Prism | `Boswas-Prism/` | `breeze-dark,breeze,Adwaita,hicolor` | Dark panels (the default): light symbolic icons |
| Boswas Prism Light | `Boswas-Prism-Light/` | `breeze,Adwaita,hicolor` | Light panels: dark symbolic icons |

### Files

| Path | What it is |
|------|------------|
| `tools/build_prism.py` | The generator: one function per design; reads every colour from `desktop/ui/tokens.json` and copies the Boswas OS mark from `desktop/branding/boswas-os-mark.svg` |
| `Boswas-Prism/scalable/{apps,places,devices,mimetypes,status,actions}/*.svg` | Generated icons (committed; do not edit by hand) |
| `Boswas-Prism-Light/scalable/{status,actions}/*.svg` | Generated symbolic icons in the dark ink (committed) |
| `Boswas-Prism/index.theme`, `Boswas-Prism-Light/index.theme` | Theme descriptions |
| `install.sh` | Installs a pack into a `DESTDIR` (`install.sh DESTDIR prism`) |
| `tools/contact_sheet.py` | Renders a review sheet (`--prism`) |
| `tests/test_prism.py` | Unit tests (standard library only) |

`Boswas-Prism-Light/` holds its `index.theme` and its own symbolic icons.
The coloured icons are the same in both themes: `install.sh` copies them
from `Boswas-Prism/` into both, as real files (no symlinks).

### Design rules

The pack follows the principles of the Fluent icons of Windows 11 (soft
gradients, light from the top, layered parts, soft shadows, rounded
geometry, a vivid but harmonious palette, thin rounded line icons). Every
drawing is Boswas's own: no Microsoft logo, icon, font or drawing is copied
or imitated, and there is no vendor logo anywhere (the web browser is a
generic globe, the Windows family a generic application window).

* **Grid.** 256x256 viewBox, scalable SVG, checked at 256, 48, 32 and 16 px
  on dark and on light backgrounds.
* **Palette.** Every colour is a token of `desktop/ui/tokens.json`: a
  palette colour (`color`), chosen through the colour roles of the pack
  (`icon`, see *Colour families*). The generator holds no colour value of
  its own. The only other colours are those of the Boswas OS mark. The
  retired teal accent is rejected by the tests.
* **Objects, no tile.** Applications, places, devices and mimetypes are
  objects with their own silhouette (a folder, a page, a gear, a camera, a
  console window, a calendar, a clock), not glyphs on a tile.
* **Light from the top.** Every part is filled with a linear gradient from
  the lighter shade of its family at its top to the deeper one at its
  bottom, leaning a little to the right (the same direction in every icon).
  A family has four shades, `light`, `from`, `to` and `deep`: a body runs
  from `from` to `to`, a lighter part (a front flap, a lid) from `light` to
  `from`, a part behind or in shade from `to` to `deep`; lit surfaces
  (screens, buttons, lights) run from `light` to `to` and spheres from
  `light` to `deep`.
* **Highlight.** The top edge of a part carries a highlight: the part again
  in white (55 %) that fades out a few units below its top.
* **Layers.** A part that lies on another casts a shade on it: a copy of the
  upper part, moved a few units, in `ink.950` at 18 % under it.
* **Contact shadow.** Every object stands on a soft shadow: its first shape
  is an ellipse filled with a radial gradient in `ink.950`, 42 % at the
  centre and fading out at the edge. No filters.
* **Edges.** White and metal parts have a faint outline in their family's
  edge colour (4 units), so that they stay visible on white; dark parts
  (the `glass` material) catch a rim of light in `slate.400`, brighter at
  the top, so that they stay visible on dark panels.
* **Corner radii.** 20 for bodies (folders, pages, windows, devices), 12
  for inner parts (screens, title bars, small windows), 6 for small parts
  (keys, contacts); round objects are circles.
* **Places.** Folders are sun-coloured: a deeper back with a tab, a sheet of
  paper and a lighter front flap with a highlight; the special folders show
  an azure emblem on the flap. The trash is a metal bin with a slate lid.
* **Devices.** Metal, slate and dark glass objects; screens are lit in azure
  (a wallpaper with a soft reflection); status lights in mint.
* **Mimetypes.** A white page with a folded corner (the fold casts a shade)
  and a symbol in the colour of the file family. Windows files show a small
  application window with a sun badge: shield (program), download arrow
  (installer package), shortcut arrow.
* **The mark.** `boswas-logo` (the taskbar's Start button) and its aliases
  are the Boswas OS mark alone: no tile, no shadow. Its paths and gradients
  are copied from `desktop/branding/boswas-os-mark.svg`, never redrawn; the
  gradient ids get the file's own prefix. `boswas-install` shows the mark,
  scaled by a group, with an azure download badge. The mark file must have
  the viewBox `0 0 256 256`, linear and radial gradients in user space with
  explicit coordinates and `#RRGGBB` stops, and then only `<path>` elements
  filled with one of the gradients or a hex colour; the generator stops with
  an error on anything else.
* **Symbolic icons (status, actions).** Line icons in the Fluent regular
  weight: one flat colour, strokes of 16 units (1 px at 16 px, 1.5 px at
  24 px) with round caps and joins and small rounded corners. Filled parts
  only where they carry meaning: a charge level, the source dot of a
  signal, a starred star, the recording dot, the dots of i and ?, the
  expander triangles. Cloud 50 in `Boswas-Prism` (dark panels) and Ink 900
  in `Boswas-Prism-Light`; the two sets differ in nothing else. A part that
  is off (a signal arc, a glyph under its slash) is the same colour at 35 %
  opacity. A charging battery shows a bolt that is knocked out of the
  charge bar where the bar is, and solid where it is not (one even-odd
  path, no mask). Straight lines and box edges lie on 8 + 16k units where
  the drawing allows it, the pixel centres at 16 px, so that a 1 px line
  stays sharp there (the caption buttons are tested for it).
* **Self-contained SVG for Qt, resvg and librsvg.** Only `path`, `g` (the
  scaled mark), linear and radial gradients in user space (a soft shadow is
  a radial gradient made elliptical by `gradientTransform`), solid fills
  and strokes, `opacity` and `fill-rule`. No `<image>`, external `href`,
  `<script>`, `<text>`, CSS, masks, clip paths, filters, `<use>` or nested
  `<svg>`. Every id is prefixed with the file's own name. UTF-8, LF.

### Colour families

The colour roles of the pack are the `icon` section of the tokens:

* `icon.hues`: the colour families of the objects, each with the shades
  `light`, `from`, `to` and `deep`, a `glyph` colour for a symbol drawn on
  it (at least 3:1 against `to`) and an `accent`;
* `icon.materials`: the neutral parts, with the same keys and an `edge`;
* `icon.light`: the highlight (white, 55 %), the shade where parts overlap
  (Ink 950, 18 %) and the contact shadow (Ink 950, 42 %).

| Family | Shades (`light`, `from`, `to`, `deep`) | Used for |
|--------|---------------------------------------|----------|
| Azure | 300, 400, 600, 700 | System and applications: the globe, Help, Accessibility, Bluetooth, Network, Users, title bars, folder emblems, screens, Boswas Store, the launcher |
| Sky | 300, 400, 600, 700 | The Windows compatibility family; the sky of pictures |
| Iris | 300, 400, 600, 700 | Settings and the accent: Control Center, Display, Clock, Personalization, Software Center, Device Agent, Lock Screen |
| Mint | 300, 400, 600, 700 | Security and updates: Security Center, Updates, Update Center, Wallet; charge levels and status lights |
| Sun | 300, 400, 600, 700 | Folders, padlocks, the bell, Emoji, the sun of Weather, the archive box, the badges of the Windows family |
| Coral | 300, 400, 600, 700 | Media and alerts: Media Player, Calendar, Document Viewer, Snipping Tool, Firewall, Report a Bug |
| Orchid | 300, 400, 600, 700 | Fonts; audio and font files |
| Slate | 300, 400, 600, 700 | Settings, Camera, keyboards, headphones, the external drive, the partition pie |
| Paper (material) | white, white, Neutral 100, Neutral 200 | Pages, sheets, window bodies, the cloud |
| Metal (material) | white, Neutral 50, Neutral 300, Neutral 400 | Devices, stands, keys, the trash |
| Glass (material) | Ink 500, Ink 500, Ink 900, Ink 950 | Dark bodies and bezels: Terminal, Calculator, the chip, the gauge, phones and screens |

The *Colour* column of the table below names the family a design is
mostly drawn in.

### Icons

324 designs, written as 583 files: 375 in `Boswas-Prism/` (99 applications,
19 places, 17 devices, 32 mimetypes, 48 status, 160 actions) and the 208
symbolic icons again in `Boswas-Prism-Light/`. Aliases are separate files
with the same drawing and their own id prefix. This table is printed by
`tools/build_prism.py --table`; the tests check that it matches.

<!-- BEGIN PRISM ICON TABLE -->
| Context | Design | Colour | Drawing | Theme icon names |
|---|---|---|---|---|
| apps | Settings | slate | Slate gear with a hub hole and a groove. | `preferences-system`, `systemsettings` |
| apps | System Information | azure | Processor chip with metal pins and an azure "i" disc. | `hwinfo` |
| apps | Terminal | glass | Dark console window with a white prompt chevron and a mint cursor. | `utilities-terminal`, `org.kde.konsole`, `boswas-terminal` |
| apps | File Manager | sun | Folder holding a white sheet and an azure sheet that rises out of it. | `system-file-manager`, `org.kde.dolphin`, `boswas-files` |
| apps | System Monitor | glass | Dark gauge with a mint to coral scale and a white needle. | `utilities-system-monitor`, `org.kde.plasma-systemmonitor` |
| apps | Web Browser | azure | Azure globe with white meridians and equator (a generic browser, not a vendor logo). | `web-browser`, `firefox-esr`, `konqueror` |
| apps | Calculator | glass | Dark calculator with an azure display, light keys and a coral equals key. | `accessories-calculator`, `org.kde.kcalc` |
| apps | Text Editor | azure | Page with azure text lines and a pencil writing on it. | `accessories-text-editor`, `kwrite`, `org.kde.kwrite`, `kate`, `org.kde.kate`, `gvim` |
| apps | Image Viewer | sky | Photo in a white frame (sky, hills and a sun) in front of an iris print. | `gwenview`, `org.kde.gwenview`, `preferences-desktop-wallpaper`, `boswas-photos` |
| apps | Document Viewer | coral | Open book on a coral cover, a sun bookmark ribbon. | `okular`, `org.kde.okular`, `boswas-document-viewer` |
| apps | Archive Manager | sun | Cardboard box with a lighter lid and a metal zip running down its front. | `utilities-file-archiver`, `ark`, `org.kde.ark` |
| apps | Screenshot | coral | A screen region (sky, hills) inside four coral selection corners. | `spectacle`, `org.kde.spectacle`, `boswas-snipping-tool` |
| apps | Help | azure | Azure disc with a white question mark. | `help-browser`, `org.kde.khelpcenter` |
| apps | Find Files | azure | Magnifying glass: metal ring around azure glass, dark handle. | `kfind` |
| apps | Wallet | mint | Mint wallet with a deeper pocket flap, a sun clasp and a card inside. | `kwalletmanager` |
| apps | Partition Manager | slate | Slate disc as a pie, one azure partition pulled out. | `partitionmanager` |
| apps | Menu Editor | azure | Menu card with slate rows, one highlighted in azure, and a pencil. | `kmenuedit` |
| apps | Process Viewer | glass | Dark console window with mint, sun and coral load bars. | `htop` |
| apps | Report a Bug | coral | Coral beetle with a dark head, legs and antennae. | `tools-report-bug` |
| apps | Emoji | sun | Sun-yellow face with dark eyes and a smile. | `preferences-desktop-emoticons` |
| apps | Network | azure | Azure wireless arcs above a sun dot. | `preferences-system-network` |
| apps | Bluetooth | azure | Azure capsule with a white Bluetooth rune. | `preferences-system-bluetooth` |
| apps | Audio | slate | Slate loudspeaker with azure sound waves. | `preferences-desktop-sound` |
| apps | Display | iris | Screen with an iris display on a metal stand. | `preferences-desktop-display`, `preferences-desktop-display-randr` |
| apps | Power | mint | Metal battery, a mint charge and a sun lightning bolt. | `preferences-system-power-management` |
| apps | Users | azure | Two people: an azure one in front of an iris one. | `preferences-system-users`, `system-users` |
| apps | Storage | metal | Two stacked metal drives with status lights. | `boswas-storage` |
| apps | Personalization | iris | Light paint palette with dots of coral, sun, mint and a large iris dot. | `preferences-desktop-theme-global` |
| apps | Privacy | sun | Sun padlock with a metal shackle and a dark keyhole. | `preferences-system-privacy`, `preferences-desktop-user-password` |
| apps | Devices | metal | Light mouse with an iris scroll wheel. | `preferences-desktop-peripherals`, `preferences-desktop-mouse` |
| apps | Updates | mint | Two mint arrows circling an azure download arrow. | `system-software-update` |
| apps | Notifications | sun | Sun bell with a coral clapper. | `preferences-desktop-notification-bell` |
| apps | Accessibility | azure | Azure disc with a white figure with outstretched arms. | `preferences-desktop-accessibility` |
| apps | Keyboard | iris | Slate keyboard, light keys and an iris space bar. | `preferences-desktop-keyboard` |
| apps | Language and Region | azure | Azure speech bubble with a white letter A in front of an iris one. | `preferences-desktop-locale` |
| apps | Firewall | coral | Coral brick wall with light mortar, one sun brick. | `preferences-security-firewall` |
| apps | Fonts | orchid | Card with a large orchid A and a dark small a. | `preferences-desktop-font` |
| apps | Window Management | azure | Two overlapping windows, iris behind, azure in front. | `preferences-system-windows`, `preferences-system-windows-actions` |
| apps | Calendar | coral | Calendar page with a coral header, metal binder rings and one coral day. | `office-calendar`, `x-office-calendar` |
| apps | Clock | iris | Clock: white face in an iris rim, dark hands and a coral second hand. | `preferences-system-time` |
| apps | Media Player | coral | Coral disc with a white play triangle. | `multimedia-player`, `applications-multimedia` |
| apps | Camera | slate | Slate camera with a glass lens in a metal ring, a coral shutter button. | `camera-photo` |
| apps | Boswas OS | mark | The Boswas OS mark (copied from desktop/branding/boswas-os-mark.svg), alone. | `boswas-logo`, `boswas-os`, `start-here`, `start-here-kde`, `start-here-kde-plasma`, `distributor-logo` |
| apps | Boswas Store | azure | Azure shopping bag with an iris handle and a sun tag. | `boswas-store` |
| apps | Compatibility Manager | sky | Application window with a sky title bar, a slate gear in front. | `boswas-compat-manager` |
| apps | Device Agent | iris | Laptop whose iris display shows a white heartbeat. | `boswas-device-agent` |
| apps | Security Center | mint | Mint shield with a white check mark. | `boswas-security-center` |
| apps | Control Center | iris | Iris gear with a toggle switch in front of it. | `boswas-control-center` |
| apps | Update Center | mint | Mint arrow circling a sun dot (a ring and a dot). | `boswas-update-center` |
| apps | Software Center | iris | Iris box with an azure tile and a coral disc rising out of it. | `boswas-software-center` |
| apps | Install Boswas OS | mark | The Boswas OS mark with an azure download badge. | `boswas-install` |
| apps | Windows Application | sky | Application window with a sky title bar, a sun sandbox shield. | `boswas-windows-application` |
| apps | Windows Installer | sky | Application window with a sky title bar, a sun badge with a download arrow. | `boswas-windows-installer` |
| apps | Windows Program Loader | sky | Application window with a sky title bar, a sun badge with a run (play) triangle. | `wine`, `wine-stable` |
| apps | Compatibility Status | sky | Application window with a sky title bar, a mint badge with a check mark. | `boswas-compat-status` |
| apps | Repair Application | sky | Application window with a sky title bar, a metal wrench. | `boswas-repair` |
| apps | Application Sandbox | sky | Sky shield enclosing an application window. | `boswas-sandbox` |
| apps | Application Policy | sky | Sky clipboard holding a sheet with an application window, metal clip. | `boswas-app-policy` |
| apps | Weather | sun | White cloud in front of a sun. | `boswas-weather` |
| apps | Boswas Launcher | azure | Three-by-three grid of azure application tiles, the centre one in sun. | `boswas-launcher`, `boswas-shell` |
| apps | Lock Screen | iris | Screen with an iris display and a sun padlock in front of it. | `boswas-lock` |
| places | Folder | sun | Folder: deeper back with a tab, a sheet of paper, lighter front flap. | `folder` |
| places | Open Folder | sun | Open folder: the front flap leans forward. | `folder-open` |
| places | Home | sun | Folder with a house. | `user-home` |
| places | Desktop | sun | Folder with a screen on its stand. | `user-desktop` |
| places | Documents | sun | Folder with text lines. | `folder-documents` |
| places | Downloads | sun | Folder with a download arrow. | `folder-download`, `folder-downloads` |
| places | Music | sun | Folder with a note. | `folder-music`, `folder-sound` |
| places | Pictures | sun | Folder with mountains and a sun. | `folder-pictures`, `folder-images` |
| places | Videos | sun | Folder with a play triangle. | `folder-videos`, `folder-video` |
| places | Templates | sun | Folder with a page layout. | `folder-templates` |
| places | Public | sun | Folder with three connected share nodes. | `folder-publicshare` |
| places | Network | sun | Folder with a small network of three nodes. | `network-workgroup` |
| places | Remote Folder | sun | Folder with a globe. | `folder-remote` |
| places | Trash | metal | Waste bin with a lid and handle. | `user-trash` |
| places | Trash (Full) | metal | Waste bin without its lid, crumpled paper inside, one sheet in the accent colour. | `user-trash-full` |
| devices | Computer | metal | Desktop computer: screen on a stand beside a metal tower with a power light. | `computer` |
| devices | Laptop | metal | Laptop: open lid with an azure display, metal base. | `computer-laptop` |
| devices | Display | metal | Screen with an azure display on a metal stand. | `video-display` |
| devices | Internal Drive | metal | Internal drive: metal slab with a deeper front strip and a status light. | `drive-harddisk` |
| devices | Removable Drive | metal | External drive: rounded slate slab with a white eject symbol. | `drive-removable-media` |
| devices | USB Stick | metal | USB stick: metal plug with two contacts, azure body with a light. | `drive-removable-media-usb` |
| devices | Memory Card | metal | Memory card: iris card with a cut corner, golden contacts and a white label. | `media-flash` |
| devices | Disc | metal | Disc: metal with a centre hole and two iridescent reflections. | `media-optical` |
| devices | Phone | metal | Phone: dark body with an azure display. | `phone` |
| devices | Printer | metal | Printer: paper in the feeder, metal body, printed page in the tray, status light. | `printer` |
| devices | Headphones | metal | Headphones: slate head band and ear cups with azure cushions. | `audio-headphones` |
| devices | Loudspeaker | metal | Loudspeaker: dark cabinet with a metal tweeter and woofer, azure dust cap. | `audio-card` |
| devices | Keyboard | metal | Keyboard: slate body, two rows of light keys and an azure space bar. | `input-keyboard` |
| devices | Mouse | metal | Mouse: light body with a button split and an azure scroll wheel. | `input-mouse` |
| devices | Webcam | metal | Webcam: round dark head on a metal foot, glass lens with an azure ring. | `camera-web` |
| devices | Wired Network | metal | Network socket: metal plate, dark port, golden contacts. | `network-wired` |
| devices | Wireless Network | metal | Wireless router: metal body with two antennas, status lights and azure radio waves. | `network-wireless` |
| mimetypes | Text | slate | Page with text lines. | `text-plain`, `text-x-generic` |
| mimetypes | Web Page | azure | Page with angle brackets and a slash. | `text-html` |
| mimetypes | Script | mint | Page with a prompt chevron and a cursor line. | `text-x-script` |
| mimetypes | Image | azure | Page with mountains and a sun. | `image-x-generic` |
| mimetypes | Audio | orchid | Page with two beamed notes. | `audio-x-generic` |
| mimetypes | Video | coral | Page with a film frame and a play triangle. | `video-x-generic` |
| mimetypes | PDF Document | coral | Page with a heading block and text lines. | `application-pdf` |
| mimetypes | Archive | slate | Page with a zip fastener. | `application-x-archive`, `application-zip`, `application-x-tar`, `application-x-7z-compressed`, `application-x-compressed-tar` |
| mimetypes | Package | sun | Page with a parcel. | `package-x-generic` |
| mimetypes | Software Package | iris | Page with an arrow into an open box. | `application-x-deb` |
| mimetypes | Android Package | mint | Page with a phone and a download arrow. | `application-vnd.android.package-archive` |
| mimetypes | Program | slate | Page with a gear. | `application-x-executable` |
| mimetypes | Library | slate | Page with three stacked layers. | `application-x-sharedlib` |
| mimetypes | Document | azure | Page with a heading and text lines. | `x-office-document` |
| mimetypes | Spreadsheet | mint | Page with a table grid. | `x-office-spreadsheet` |
| mimetypes | Presentation | sun | Page with a bar chart. | `x-office-presentation` |
| mimetypes | Font | orchid | Page with the letters "Aa" drawn as strokes. | `font-x-generic` |
| mimetypes | JSON Data | iris | Page with curly braces and two dots. | `application-json` |
| mimetypes | Unknown File | slate | Page with a question mark. | `unknown`, `application-octet-stream` |
| mimetypes | Windows Program | sky | Page with an application window and a sandbox shield badge. | `application-x-ms-dos-executable`, `application-x-msdownload`, `application-vnd.microsoft.portable-executable`, `application-x-ms-ne-executable` |
| mimetypes | Windows Installer Package | sky | Page with an application window and a download badge. | `application-x-msi` |
| mimetypes | Windows Shortcut | sky | Page with an application window and a shortcut-arrow badge. | `application-x-ms-shortcut` |
| mimetypes | Folder | sun | Folder: deeper back with a tab, a sheet of paper, lighter front flap. | `inode-directory` |
| status | Wireless Signal: None | symbolic | Wireless fan (dot and three arcs), all parts dimmed. | `network-wireless-signal-none-symbolic` |
| status | Wireless Signal: Weak | symbolic | Wireless fan (dot and three arcs), only the dot lit. | `network-wireless-signal-weak-symbolic` |
| status | Wireless Signal: Ok | symbolic | Wireless fan (dot and three arcs), dot and one arc lit. | `network-wireless-signal-ok-symbolic` |
| status | Wireless Signal: Good | symbolic | Wireless fan (dot and three arcs), dot and two arcs lit. | `network-wireless-signal-good-symbolic` |
| status | Wireless Signal: Excellent | symbolic | Wireless fan (dot and three arcs), all parts lit. | `network-wireless-signal-excellent-symbolic` |
| status | Wireless Off | symbolic | Wireless fan dimmed under a slash. | `network-wireless-offline-symbolic` |
| status | Wired Network | symbolic | Three connected network sockets. | `network-wired-symbolic` |
| status | Network Offline | symbolic | Network sockets dimmed under a slash. | `network-offline-symbolic` |
| status | Volume: Muted | symbolic | Speaker with a cross. | `audio-volume-muted-symbolic` |
| status | Volume: Low | symbolic | Speaker with three sound waves, one lit. | `audio-volume-low-symbolic` |
| status | Volume: Medium | symbolic | Speaker with three sound waves, two lit. | `audio-volume-medium-symbolic` |
| status | Volume: High | symbolic | Speaker with three sound waves, all lit. | `audio-volume-high-symbolic` |
| status | Battery: Empty | symbolic | Battery, empty. | `battery-empty-symbolic` |
| status | Battery: Caution | symbolic | Battery, nearly empty. | `battery-caution-symbolic` |
| status | Battery: Low | symbolic | Battery, a quarter full. | `battery-low-symbolic` |
| status | Battery: Good | symbolic | Battery, mostly full. | `battery-good-symbolic` |
| status | Battery: Full | symbolic | Battery, full. | `battery-full-symbolic` |
| status | Battery: Empty, Charging | symbolic | Battery, empty, with a charging bolt. | `battery-empty-charging-symbolic` |
| status | Battery: Caution, Charging | symbolic | Battery, nearly empty, with a charging bolt. | `battery-caution-charging-symbolic` |
| status | Battery: Low, Charging | symbolic | Battery, a quarter full, with a charging bolt. | `battery-low-charging-symbolic` |
| status | Battery: Good, Charging | symbolic | Battery, mostly full, with a charging bolt. | `battery-good-charging-symbolic` |
| status | Battery: Full, Charging | symbolic | Battery, full, with a charging bolt. | `battery-full-charging-symbolic` |
| status | Bluetooth On | symbolic | Bluetooth rune. | `bluetooth-active-symbolic` |
| status | Bluetooth Off | symbolic | Bluetooth rune dimmed under a slash. | `bluetooth-disabled-symbolic` |
| status | Notifications | symbolic | Bell. | `notification-symbolic` |
| status | Notifications Off | symbolic | Bell dimmed under a slash. | `notification-disabled-symbolic` |
| status | Warning | symbolic | Triangle with an exclamation mark. | `dialog-warning-symbolic` |
| status | Error | symbolic | Ring with a cross. | `dialog-error-symbolic` |
| status | Information | symbolic | Ring with the letter i. | `dialog-information-symbolic` |
| status | Password | symbolic | Key. | `dialog-password-symbolic` |
| status | Secure | symbolic | Shield with a check mark. | `security-high-symbolic` |
| status | Caps Lock | symbolic | Up arrow with a bar below. | `caps-lock-symbolic` |
| status | User | symbolic | Person: head and shoulders. | `avatar-default-symbolic` |
| status | Brightness | symbolic | Sun with eight rays. | `display-brightness-symbolic` |
| status | Night Light | symbolic | Crescent moon with two stars. | `night-light-symbolic` |
| status | Airplane Mode | symbolic | Airplane seen from above. | `airplane-mode-symbolic` |
| status | Wireless Network | symbolic | Wireless fan (dot and three arcs), all parts lit. | `network-wireless-symbolic` |
| status | VPN | symbolic | Shield with a keyhole. | `network-vpn-symbolic` |
| status | Microphone | symbolic | Microphone. | `audio-input-microphone-symbolic` |
| status | Microphone Off | symbolic | Microphone dimmed under a slash. | `microphone-sensitivity-muted-symbolic` |
| status | Headphones | symbolic | Headphones. | `audio-headphones-symbolic` |
| status | No Battery | symbolic | Battery outline under a slash. | `battery-missing-symbolic` |
| status | Question | symbolic | Ring with a question mark. | `dialog-question-symbolic` |
| status | Power Saver | symbolic | Leaf: the power saver profile. | `power-profile-power-saver-symbolic` |
| status | Balanced | symbolic | Gauge with its needle in the middle: the balanced profile. | `power-profile-balanced-symbolic` |
| status | Performance | symbolic | Lightning bolt: the performance profile. | `power-profile-performance-symbolic` |
| status | Update Available | symbolic | Ring with an arrow pointing up. | `software-update-available-symbolic` |
| status | Default | symbolic | Ring with a check mark. | `emblem-default-symbolic` |
| actions | Close Window | symbolic | Cross. | `window-close-symbolic` |
| actions | Maximize Window | symbolic | Square. | `window-maximize-symbolic` |
| actions | Minimize Window | symbolic | Horizontal line. | `window-minimize-symbolic` |
| actions | Restore Window | symbolic | Square in front of a second one. | `window-restore-symbolic` |
| actions | Search | symbolic | Magnifying glass. | `system-search-symbolic` |
| actions | All Applications | symbolic | Three-by-three grid of small filled squares. | `view-app-grid-symbolic` |
| actions | Lock Screen | symbolic | Padlock. | `system-lock-screen-symbolic` |
| actions | Shut Down | symbolic | Power symbol: broken ring with a vertical line. | `system-shutdown-symbolic` |
| actions | Restart | symbolic | Circular arrow. | `system-reboot-symbolic` |
| actions | Log Out | symbolic | Door frame with an arrow leaving it. | `system-log-out-symbolic` |
| actions | Sleep | symbolic | Crescent moon. | `system-suspend-symbolic` |
| actions | Next | symbolic | Chevron pointing right. | `go-next-symbolic` |
| actions | Previous | symbolic | Chevron pointing left. | `go-previous-symbolic` |
| actions | Up | symbolic | Chevron pointing up. | `go-up-symbolic` |
| actions | Down | symbolic | Chevron pointing down. | `go-down-symbolic` |
| actions | Menu | symbolic | Three horizontal lines. | `open-menu-symbolic` |
| actions | Add | symbolic | Plus. | `list-add-symbolic` |
| actions | Remove | symbolic | Minus. | `list-remove-symbolic` |
| actions | Delete | symbolic | Waste bin. | `edit-delete-symbolic` |
| actions | Done | symbolic | Check mark. | `emblem-ok-symbolic` |
| actions | Refresh | symbolic | Two arrows chasing each other around a circle. | `view-refresh-symbolic` |
| actions | Open | symbolic | Open folder. | `document-open-symbolic` |
| actions | Settings | symbolic | Gear. | `preferences-system-symbolic` |
| actions | Expand | symbolic | Filled triangle pointing down. | `pan-down-symbolic` |
| actions | Collapse | symbolic | Filled triangle pointing up. | `pan-up-symbolic` |
| actions | Show | symbolic | Eye. | `eye-open-symbolic` |
| actions | Hide | symbolic | Eye dimmed under a slash. | `eye-closed-symbolic` |
| actions | Play | symbolic | Triangle pointing right. | `media-playback-start-symbolic` |
| actions | Pause | symbolic | Two upright bars. | `media-playback-pause-symbolic` |
| actions | Stop | symbolic | Square. | `media-playback-stop-symbolic` |
| actions | Record | symbolic | Filled disc. | `media-record-symbolic` |
| actions | Next Track | symbolic | Triangle pointing right against a bar. | `media-skip-forward-symbolic` |
| actions | Previous Track | symbolic | Triangle pointing left against a bar. | `media-skip-backward-symbolic` |
| actions | Fast Forward | symbolic | Two triangles pointing right. | `media-seek-forward-symbolic` |
| actions | Rewind | symbolic | Two triangles pointing left. | `media-seek-backward-symbolic` |
| actions | Eject | symbolic | Triangle pointing up above a bar. | `media-eject-symbolic` |
| actions | Shuffle | symbolic | Two arrows crossing each other. | `media-playlist-shuffle-symbolic` |
| actions | Repeat | symbolic | Two arrows following each other around a loop. | `media-playlist-repeat-symbolic` |
| actions | Task View | symbolic | Two overlapping windows. | `view-task-symbolic` |
| actions | Selected | symbolic | Check mark. | `object-select-symbolic` |
| actions | Copy | symbolic | Sheet in front of a second one. | `edit-copy-symbolic` |
| actions | Cut | symbolic | Scissors. | `edit-cut-symbolic` |
| actions | Paste | symbolic | Clipboard. | `edit-paste-symbolic` |
| actions | Undo | symbolic | Arrow turning back to the left. | `edit-undo-symbolic` |
| actions | Redo | symbolic | Arrow turning forward to the right. | `edit-redo-symbolic` |
| actions | Clear | symbolic | Backspace key: a tag pointing left with a cross. | `edit-clear-symbolic` |
| actions | Find | symbolic | Magnifying glass. | `edit-find-symbolic` |
| actions | New Document | symbolic | Sheet with a folded corner. | `document-new-symbolic` |
| actions | Save | symbolic | Floppy disk. | `document-save-symbolic` |
| actions | Print | symbolic | Printer with paper in and out. | `document-print-symbolic` |
| actions | Edit | symbolic | Pencil. | `document-edit-symbolic` |
| actions | Folder | symbolic | Folder outline. | `folder-symbolic` |
| actions | New Folder | symbolic | Folder outline with a plus. | `folder-new-symbolic` |
| actions | Home | symbolic | House. | `go-home-symbolic` |
| actions | Home Folder | symbolic | House. | `user-home-symbolic` |
| actions | Trash | symbolic | Waste bin. | `user-trash-symbolic` |
| actions | Downloads | symbolic | Arrow pointing down into a tray. | `folder-download-symbolic` |
| actions | Upload | symbolic | Arrow pointing up out of a tray. | `document-send-symbolic` |
| actions | List View | symbolic | Three lines with bullets. | `view-list-symbolic` |
| actions | Grid View | symbolic | Two-by-two grid of squares. | `view-grid-symbolic` |
| actions | Zoom In | symbolic | Magnifying glass with a plus. | `zoom-in-symbolic` |
| actions | Zoom Out | symbolic | Magnifying glass with a minus. | `zoom-out-symbolic` |
| actions | Full Screen | symbolic | Four corners pointing outwards. | `view-fullscreen-symbolic` |
| actions | Leave Full Screen | symbolic | Four corners pointing inwards. | `view-restore-symbolic` |
| actions | Pin | symbolic | Pushpin. | `view-pin-symbolic` |
| actions | More | symbolic | Three dots in a column. | `view-more-symbolic` |
| actions | More Options | symbolic | Three dots in a row. | `view-more-horizontal-symbolic` |
| actions | Help | symbolic | Ring with a question mark. | `help-browser-symbolic` |
| actions | About | symbolic | Ring with the letter i. | `help-about-symbolic` |
| actions | Favourite | symbolic | Five-pointed star, filled. | `starred-symbolic` |
| actions | Not a Favourite | symbolic | Five-pointed star, outlined. | `non-starred-symbolic` |
| actions | Location | symbolic | Map pin. | `mark-location-symbolic` |
| actions | Bookmarks | symbolic | Ribbon. | `user-bookmarks-symbolic` |
| actions | Side Pane | symbolic | Window with a side pane. | `sidebar-show-symbolic` |
| actions | Sort Ascending | symbolic | Three lines growing longer downwards. | `view-sort-ascending-symbolic` |
| actions | Sort Descending | symbolic | Three lines growing shorter downwards. | `view-sort-descending-symbolic` |
| actions | Mail | symbolic | Envelope. | `mail-unread-symbolic` |
| actions | Unlocked | symbolic | Padlock with an open shackle. | `changes-allow-symbolic` |
| actions | Locked | symbolic | Padlock. | `changes-prevent-symbolic` |
| actions | New Tab | symbolic | Plus. | `tab-new-symbolic` |
| actions | Camera | symbolic | Photo camera. | `camera-photo-symbolic` |
| actions | Video Camera | symbolic | Video camera. | `camera-video-symbolic` |
| actions | Picture | symbolic | Framed picture: hills and a sun. | `image-x-generic-symbolic` |
| actions | Clock | symbolic | Clock face with two hands. | `preferences-system-time-symbolic` |
| actions | Calendar | symbolic | Calendar sheet with two rings. | `x-office-calendar-symbolic` |
| actions | Accessibility | symbolic | Person with open arms in a ring. | `preferences-desktop-accessibility-symbolic` |
| actions | Keyboard | symbolic | Keyboard. | `input-keyboard-symbolic` |
| actions | Mouse | symbolic | Mouse. | `input-mouse-symbolic` |
| actions | Display | symbolic | Monitor on a stand. | `video-display-symbolic` |
| actions | Drive | symbolic | Drive with an activity light. | `drive-harddisk-symbolic` |
| actions | Removable Drive | symbolic | USB stick. | `drive-removable-media-symbolic` |
| actions | Recent | symbolic | Clock hands inside an arrow turning back. | `document-open-recent-symbolic` |
| actions | Rotate Right | symbolic | Sheet with an arrow turning clockwise. | `object-rotate-right-symbolic` |
| actions | Rotate Left | symbolic | Sheet with an arrow turning anticlockwise. | `object-rotate-left-symbolic` |
| actions | Flip Horizontally | symbolic | Two triangles mirrored at an upright line, one filled. | `object-flip-horizontal-symbolic` |
| actions | Flip Vertically | symbolic | Two triangles mirrored at a level line, one filled. | `object-flip-vertical-symbolic` |
| actions | Zoom to Fit | symbolic | Picture inside four corners. | `zoom-fit-best-symbolic` |
| actions | Actual Size | symbolic | The figures 1:1. | `zoom-original-symbolic` |
| actions | Save As | symbolic | Floppy disk with a pencil. | `document-save-as-symbolic` |
| actions | Select All | symbolic | Square inside four corners. | `edit-select-all-symbolic` |
| actions | Touchpad | symbolic | Touchpad with its two buttons. | `input-touchpad-symbolic` |
| actions | Success | symbolic | Ring with a check mark. | `dialog-positive-symbolic` |
| actions | Extract | symbolic | Arrow rising out of an open box. | `archive-extract-symbolic` |
| actions | Add to Archive | symbolic | Arrow going down into an open box. | `archive-insert-symbolic` |
| actions | Properties | symbolic | Sheet with the letter i. | `document-properties-symbolic` |
| actions | Preview | symbolic | Sheet under a magnifying glass. | `document-preview-symbolic` |
| actions | Text Document | symbolic | Sheet with three lines of text. | `text-x-generic-symbolic` |
| actions | Log | symbolic | Sheet with three lines of text. | `text-x-log-symbolic` |
| actions | Log Viewer | symbolic | Sheet with three lines of text. | `utilities-log-viewer-symbolic` |
| actions | Clear History | symbolic | Arrow turning back around a cross. | `edit-clear-history-symbolic` |
| actions | Cancel | symbolic | Ring with a cross. | `process-stop-symbolic` |
| actions | Run | symbolic | Ring with a play triangle. | `system-run-symbolic` |
| actions | Install | symbolic | Arrow pointing down into a tray. | `run-install-symbolic` |
| actions | Install Software | symbolic | Arrow pointing down into a tray. | `system-software-install-symbolic` |
| actions | Details View | symbolic | Three rows, each with a short and a long cell. | `view-list-details-symbolic` |
| actions | Light | symbolic | Sun with eight rays. | `weather-clear-symbolic` |
| actions | Dark | symbolic | Crescent moon. | `weather-clear-night-symbolic` |
| actions | Keep on Top | symbolic | Pushpin. | `window-pin-symbolic` |
| actions | Flash | symbolic | Lightning bolt. | `camera-flash-symbolic` |
| actions | Switch Camera | symbolic | Camera with two arrows turning inside it. | `camera-switch-symbolic` |
| actions | Timer | symbolic | Stopwatch: a dial with a crown and one hand. | `timer-symbolic` |
| actions | Stopwatch | symbolic | Stopwatch: a dial with a crown and one hand. | `stopwatch-symbolic` |
| actions | Countdown | symbolic | Hourglass. | `hourglass-symbolic` |
| actions | Alarm | symbolic | Alarm clock: a dial with two bells and two feet. | `alarm-symbolic` |
| actions | World | symbolic | Globe with the equator and one meridian. | `globe-symbolic` |
| actions | Focus | symbolic | Target: two rings around a dot. | `focus-symbolic` |
| actions | Flag | symbolic | Flag on a pole. | `flag-symbolic` |
| actions | Torch | symbolic | Hand torch, lit. | `torch-symbolic` |
| actions | Repeat One | symbolic | The repeat loop around the figure 1. | `media-playlist-repeat-song-symbolic` |
| actions | Playlist | symbolic | Three lines of a list and a play triangle. | `playlist-symbolic` |
| actions | Music | symbolic | Two joined notes. | `audio-x-generic-symbolic` |
| actions | Video | symbolic | Frame of film with a play triangle. | `video-x-generic-symbolic` |
| actions | Mini Player | symbolic | Window with a smaller, filled one in its corner. | `view-dual-symbolic` |
| actions | Performance | symbolic | Frame with a pulse line: activity over time. | `utilities-system-monitor-symbolic` |
| actions | Services | symbolic | Three sheets lying on each other. | `view-layers-symbolic` |
| actions | Game Controller | symbolic | Game controller. | `input-gaming-symbolic` |
| actions | Tablet | symbolic | Drawing tablet with a pen resting on it. | `input-tablet-symbolic` |
| actions | Partly Cloudy | symbolic | Sun with rays behind a cloud to its lower right. | `weather-few-clouds-symbolic` |
| actions | Partly Cloudy at Night | symbolic | Crescent moon behind a cloud to its lower right. | `weather-few-clouds-night-symbolic` |
| actions | Overcast | symbolic | Filled cloud. | `weather-overcast-symbolic` |
| actions | Cloudy | symbolic | Cloud outline. | `weather-clouds-symbolic` |
| actions | Fog | symbolic | Cloud over two lines of mist. | `weather-fog-symbolic` |
| actions | Drizzle | symbolic | Cloud with three short drops under it. | `weather-showers-scattered-symbolic` |
| actions | Rain | symbolic | Cloud with three lines of rain under it. | `weather-showers-symbolic` |
| actions | Snow | symbolic | Cloud with three snowflakes under it. | `weather-snow-symbolic` |
| actions | Thunderstorm | symbolic | Cloud with a lightning bolt under it. | `weather-storm-symbolic` |
| actions | Wind | symbolic | Three lines of wind, two ending in a curl. | `weather-windy-symbolic` |
| actions | Humidity | symbolic | Water drop with a highlight. | `weather-humidity-symbolic` |
| actions | Pressure | symbolic | Gauge with its needle in the middle. | `weather-pressure-symbolic` |
| actions | Sunrise | symbolic | Half a sun on the horizon, an arrow up under it. | `daytime-sunrise-symbolic` |
| actions | Sunset | symbolic | Half a sun on the horizon, an arrow down under it. | `daytime-sunset-symbolic` |
| actions | Temperature | symbolic | Thermometer with its bulb filled. | `temperature-symbolic` |
| actions | New Snip | symbolic | Viewfinder corners around a dot. | `screenshot-symbolic` |
| actions | Pen | symbolic | Pencil, tip to the lower left. | `draw-freehand-symbolic` |
| actions | Highlighter | symbolic | Highlighter with a chisel tip over a line. | `draw-highlight-symbolic` |
| actions | Eraser | symbolic | Eraser block on a line. | `draw-eraser-symbolic` |
| actions | Crop | symbolic | Two crossing right angles. | `transform-crop-symbolic` |
| actions | Arrow | symbolic | Diagonal arrow pointing up and to the right. | `draw-arrow-symbolic` |
| actions | Rectangle | symbolic | Rounded rectangle outline. | `draw-rectangle-symbolic` |
| actions | Text | symbolic | Capital T. | `draw-text-symbolic` |
<!-- END PRISM ICON TABLE -->

Notes on names:

* **Every legacy name is kept.** Each icon name of the legacy pack exists in
  Prism, so a component that asks for `boswas-control-center` or
  `org.kde.dolphin` finds it in either pack.
* **Native desktop.** `boswas-launcher` (`boswas-shell`), `boswas-lock`,
  `boswas-files` and `boswas-terminal` are the names of the native shell;
  `boswas-photos`, `boswas-document-viewer`, `boswas-snipping-tool` and
  `boswas-weather` are the names the Boswas applications ask for.
  The shell loads the symbolic icons by the names in the table, from
  `scalable/status` and `scalable/actions`.
* **Folders.** `folder-downloads`, `folder-images`, `folder-video` and
  `folder-sound` are the names KDE's file manager asks for;
  `inode-directory` is the folder as a mimetype.
* **Windows programs.** As in the legacy pack, the program page is installed
  under all four names that shared-mime-info and KDE use for `.exe` files.

### Regenerating

The generator needs Python 3 (standard library only) and reads
`desktop/ui/tokens.json` and `desktop/branding/boswas-os-mark.svg`:

```sh
python3 desktop/icons/tools/build_prism.py          # rewrite both Prism themes
python3 desktop/icons/tools/build_prism.py --table  # print the table above
```

It is deterministic and removes SVGs it no longer generates. Re-run it after
changing a drawing, a token or the mark, and commit the regenerated files;
the tests fail when they are stale.

### Adding an icon

1. Write a drawing function in `tools/build_prism.py` with a one-line
   docstring describing the drawing. Return a list of elements. Coloured
   icons start with `shadow(...)` and are built from lit parts:
   `obj(d, family, tier)` (a part in a gradient of its family with the
   highlight along its top edge), `part(...)` (the gradient alone),
   `shade(d)` (the shade under an overlapping part), `edge(d)` (the outline
   of a white or metal part) and the shared objects (`folder`, `page`,
   `window`, `screen`, `disc`, `badge`, `pencil`). A family is a key of
   `icon.hues` or `icon.materials`; a tier is `body`, `light`, `deep`,
   `bright`, `full` or `rich`. Symbolic icons use `y_s` (a 16-unit line),
   `y_f` (a filled part) and `y_fs` (a filled part with rounded corners).
2. Add a row to the list of its context (`APP_ICONS`, `FOLDER_ICONS`,
   `DEVICE_ICONS`, `MIME_ICONS`, `STATUS_ICONS`, `ACTION_ICONS` and the
   other symbolic lists) with its colour family.
3. Run the generator, render a contact sheet (below) and look at the icon
   at 256, 48, 32 and 16 px next to its neighbours, on dark and on light.
4. Paste the new `--table` output between the markers above, add the name
   to the required list of `tests/test_prism.py` if a component depends on
   it, and run the tests.

### Previewing

```sh
python3 desktop/icons/tools/contact_sheet.py /tmp/prism.png --prism
python3 desktop/icons/tools/contact_sheet.py /tmp/prism-apps.png --prism --contexts apps --sizes 256,48,32,16
python3 desktop/icons/tools/contact_sheet.py /tmp/prism-some.png --prism --names boswas-files,boswas-terminal
python3 desktop/icons/tools/contact_sheet.py /tmp/prism-symbolic-light.png --prism --light \
    --contexts status,actions --sizes 64,24,16
```

`--light` puts the sheet on a light background and takes the symbolic icons
from `Boswas-Prism-Light/`; `--names` shows only the named icons (aliases
included), in the order given; `--theme DIR` shows the output of
`build_prism.py --output` instead of the committed themes.

### Installing

```sh
sh desktop/icons/install.sh "$DESTDIR" prism   # Boswas-Prism and Boswas-Prism-Light
sh desktop/icons/install.sh "$DESTDIR" all     # both packs
```

Files are installed 0644, directories 0755, icons as real files. It builds
no icon cache: `dh_icons` (or `gtk-update-icon-cache` in the package
scripts) does that. Select the theme with `Theme=Boswas-Prism` in the
`[Icons]` group of `kdeglobals`. Without a pack name `install.sh` installs
only the legacy pack, as it always did.

### Tests

`tests/test_prism.py` checks that every required name exists in both
installed themes; that every SVG parses, has the 256 viewBox, is
self-contained and uses only the elements above, with every gradient in
user space; that every colour is a token (or the mark's own, in the icons
that show the mark); that the colour families run from light to deep and
keep a symbol readable; that no coloured icon has a tile, that every object
stands on a contact shadow that fades out, that every part is lit from the
top and every highlight fades out downwards; that the logo icons carry the
mark's paths and gradients unchanged, `boswas-logo` and its aliases nothing
else; that symbolic icons have one colour, lines of 16 units with round
caps and joins, and differ between the themes only in the colour; that the
caption buttons are sharp at 16 px; that aliases match their design; that
both `index.theme` files are valid; that `install.sh` installs real files
with the right modes; that the table above is current; and that the
committed SVGs match a fresh run of the generator byte for byte.

## Boswas (legacy pack)

Two icon themes with the same icons:

| Theme | Directory | Inherits | Used with |
|-------|-----------|----------|-----------|
| Boswas | `Boswas/` | `breeze-dark,breeze,hicolor` | Dark panels (the default Boswas look) |
| Boswas Light | `Boswas-Light/` | `breeze,hicolor` | Light presets, so symbolic tray icons fall back to dark-on-light Breeze |

The themes only draw application, settings-category and Windows-file icons.
Everything else (toolbar actions, places, status and tray icons, symbolic
menu-category icons) comes from Breeze through `Inherits`.

### Files

| Path | What it is |
|------|------------|
| `tools/build_icons.py` | The generator: every glyph is defined here, one function per design |
| `tools/legacy-mark.svg` | The gold and silver Boswas OS mark this pack keeps drawing: a kept copy of the mark that `desktop/branding/tools/trace_mark.py` traced from `desktop/branding/source/boswas-os-logo.png` (do not edit) |
| `Boswas/scalable/apps/*.svg`, `Boswas/scalable/mimetypes/*.svg` | Generated icons (committed; do not edit by hand) |
| `Boswas/index.theme`, `Boswas-Light/index.theme` | Theme descriptions |
| `install.sh` | Installs both themes into a `DESTDIR` |
| `tools/contact_sheet.py` | Renders a review sheet of all icons (PNG, via `rsvg-convert`) |
| `tests/test_icons.py` | Unit tests (standard library only) |

`Boswas-Light/` holds only its `index.theme`: `install.sh` copies the icons
from `Boswas/` into both themes as real files (no symlinks).

### Design rules

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
  path data is copied from `tools/legacy-mark.svg` at build time, never
  redrawn. That file keeps the gold and silver mark this pack was designed
  with; the current Boswas OS mark (`desktop/branding/boswas-os-mark.svg`)
  is drawn by Boswas Prism.
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

### Icons

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
| Boswas OS | The traced Boswas OS mark in gold and silver (copied from tools/legacy-mark.svg). | `boswas-logo`, `boswas-os`, `start-here`, `start-here-kde`, `start-here-kde-plasma`, `distributor-logo` |
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

### Regenerating

The generator needs Python 3 (standard library only) and reads
`tools/legacy-mark.svg`:

```sh
python3 desktop/icons/tools/build_icons.py          # rewrite desktop/icons/Boswas/scalable
python3 desktop/icons/tools/build_icons.py --table  # print the table above
```

It is deterministic and removes SVGs it no longer generates. Commit the
regenerated files; the tests fail when they are stale.

### Adding an icon

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

### Previewing

`tools/contact_sheet.py` composes all icons at 128, 48 and 32 px with their
names and renders the sheet with `rsvg-convert`. Write previews outside the
repository (the tool refuses a path inside it):

```sh
python3 desktop/icons/tools/contact_sheet.py /tmp/boswas-icons.png
python3 desktop/icons/tools/contact_sheet.py /tmp/boswas-icons-light.png --light
python3 desktop/icons/tools/contact_sheet.py /tmp/boswas-icons-all.png --aliases
```

### Installing

```sh
sh desktop/icons/install.sh "$DESTDIR"          # the same as: install.sh "$DESTDIR" legacy
```

installs `$DESTDIR/usr/share/icons/Boswas/` and
`$DESTDIR/usr/share/icons/Boswas-Light/` (files 0644, directories 0755,
icons copied into both). It builds no icon cache: `dh_icons` (or
`gtk-update-icon-cache` in the package scripts) does that. Select the theme
with `Theme=Boswas` in the `[Icons]` group of `kdeglobals`.

### Tests

```sh
cd desktop/icons && python3 -B -m unittest discover -s tests -v
```

`tests/test_icons.py` checks that every required name exists in both
installed themes, that every SVG parses, has the 256 viewBox, is
self-contained and Qt-safe, uses only the palette (no teal) and
file-prefixed ids, that the logo carries the path data of the kept mark,
that aliases match their design, that both `index.theme` files are valid,
that `install.sh` installs real files with the right modes, that the table
above is current, and that the committed SVGs match a fresh run of the
generator byte for byte.
