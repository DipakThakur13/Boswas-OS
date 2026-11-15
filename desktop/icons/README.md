# Boswas icon themes

Two icon packs, each as a pair of themes with the same icons:

| Pack | Themes | Palette | Generator | Draws |
|------|--------|---------|-----------|-------|
| **Boswas Prism** (the default of the native Boswas desktop) | `Boswas-Prism/`, `Boswas-Prism-Light/` | Prism, `desktop/ui/tokens.json` | `tools/build_prism.py` | Applications, places, devices, mimetypes, symbolic status and action icons |
| **Boswas** (legacy, navy and gold) | `Boswas/`, `Boswas-Light/` | Brand palette, `desktop/branding/README.md` | `tools/build_icons.py` | Applications and Windows mimetypes |

Both packs draw the same glyph shapes for the 61 legacy designs:
`build_prism.py` imports the glyph geometry of `build_icons.py` and gives it
new paints. Everything a pack does not draw comes from Breeze (and Adwaita,
hicolor) through `Inherits`.

## Boswas Prism

| Theme | Directory | Inherits | Used with |
|-------|-----------|----------|-----------|
| Boswas Prism | `Boswas-Prism/` | `breeze-dark,breeze,Adwaita,hicolor` | Dark panels (the default): light symbolic icons |
| Boswas Prism Light | `Boswas-Prism-Light/` | `breeze,Adwaita,hicolor` | Light panels: dark symbolic icons |

### Files

| Path | What it is |
|------|------------|
| `tools/build_prism.py` | The generator: tile, objects and symbolic glyphs, one function per design; reads every colour from `desktop/ui/tokens.json` |
| `tools/build_icons.py` | The legacy generator, imported for the shared glyph geometry and the traced mark |
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

* **Grid.** 256x256 viewBox, scalable SVG, checked at 256, 48, 32 and 16 px
  on dark and on light backgrounds.
* **Palette.** Every colour is a token of `desktop/ui/tokens.json` (`color`),
  resolved when the generator runs; the generator holds no colour value of
  its own. The only other colours are the gold and silver of the official
  mark. The retired teal accent is rejected by the tests.
* **Tile (applications).** A squircle, not a rounded square: the
  superellipse `|x|^5 + |y|^5 = 116^5` around the centre (232 px), drawn as
  cubic Bezier curves. It carries a diagonal gradient in the hue of the
  icon's category (`icon.hues.<hue>.from` at the top left to `.to` at the
  bottom right) and a soft highlight (white, 24 % at the top edge, fading
  out at the middle). No edge stroke.
* **Glyph.** The line art of the legacy pack (stroke width 14, round caps and
  joins, inside roughly 56..200) in the hue's `glyph` colour: white, or
  Ink 900 on the light Sun tile.
* **One accent.** Each icon has exactly one element in the hue's `accent`
  colour: the part that carries the meaning. A symbol on an accent badge is
  Ink 900 or white, whichever contrasts more.
* **Overlaps.** A halo in the tile's own gradient is drawn under an accent
  that overlaps a line. The highlight is drawn above the last halo, so the
  halo matches the lit tile exactly and only interrupts the line. A white
  highlight does not change a white glyph below it.
* **The mark.** `boswas-logo` (and `start-here`, `distributor-logo`) and
  `boswas-install` show the official mark on the Ink tile. Its path data and
  gradients are copied from `desktop/branding/boswas-os-mark.svg`, never
  redrawn, and the highlight stays under it, on the tile.
* **Contrast.** Glyph against both tile stops: at least 3:1 (WCAG relative
  luminance). Accent against both stops: at least 2:1. Contrast guard: when
  a `from` stop gives the glyph less than 3:1, the generator uses the next
  shade of the same colour scale. With the current tokens this moves the Sky
  tile from Sky 500 (2.998:1 against white) to Sky 600.
* **Places.** Folders are objects, not tiles: an Azure folder with a darker
  back, a sheet of paper and a lighter front flap, and a white emblem for
  the special folders. The trash is a Slate bin.
* **Devices.** Slate objects with dark glass, light parts and one accent (a
  reflection, a status light, a scroll wheel).
* **Mimetypes.** A light page with a folded corner and a faint outline (for
  white backgrounds), a band at the bottom and a line-art glyph, both in the
  hue of the file family. Windows files show the application window with a
  Sun badge: shield (program), download arrow (installer package), shortcut
  arrow. No Microsoft, Android, Debian or other vendor logo anywhere.
* **Symbolic icons (status, actions).** One flat colour, no tile, strokes of
  22 units (1.4 px at 16 px). Cloud 50 in `Boswas-Prism` (dark panels) and
  Ink 900 in `Boswas-Prism-Light`; the two sets differ in nothing else. A
  part that is off (a signal arc, a glyph under its slash) is the same
  colour at 35 % opacity. A charging battery shows a bolt that is knocked
  out of the charge bar where the bar is, and solid where it is not (one
  even-odd path, no mask).
* **Self-contained SVG for Qt, resvg and librsvg.** Only `path`, `g` (the
  mark), linear gradients in user space, solid fills and strokes, `opacity`
  and `fill-rule`. No `<image>`, external `href`, `<script>`, `<text>`, CSS,
  masks, clip paths, filters, `<use>` or nested `<svg>`. Every id is
  prefixed with the file's own name. UTF-8, LF.

### Hues

One hue per category (`icon.hues` in the tokens):

| Hue | Used for |
|-----|----------|
| Iris | System and settings: Settings, System Information, Display, Users, Devices, Keyboard, Language, Window Management, Clock, Menu Editor, Control Center, Software Center, Device Agent, Launcher, Lock Screen |
| Azure | Files, network, internet and documents: File Manager, Find Files, Web Browser, Text Editor, Help, Network, Bluetooth, Accessibility, Boswas Store; all folders |
| Mint | Security and updates: Security Center, Privacy, Firewall, Wallet, Updates, Update Center |
| Sun | Images, notifications and energy: Image Viewer, Notifications, Power (a light tile with an Ink glyph and an Iris accent) |
| Coral | Multimedia and alerts: Media Player, Audio, Camera, Screenshot, Calendar, Document Viewer, Report a Bug |
| Sky | The Windows compatibility family |
| Orchid | Personalization: Personalization, Fonts, Emoji |
| Slate | Utilities and hardware: Terminal, System Monitor, Process Viewer, Calculator, Archive Manager, Partition Manager, Storage; all devices and the trash |
| Ink | The tile of the official mark: Boswas OS, Install Boswas OS |

Mimetypes use the hue of their family: Slate (text, archives, programs,
unknown files), Azure (web pages, documents), Mint (scripts, spreadsheets,
Android packages), Sun (images, parcels, presentations), Coral (video,
PDF), Orchid (audio, fonts), Iris (software packages, JSON), Sky (Windows).

### Icons

254 designs, written as 441 files: 302 in `Boswas-Prism/` (95 applications,
19 places, 17 devices, 32 mimetypes, 48 status, 91 actions) and the 139
symbolic icons again in `Boswas-Prism-Light/`. Aliases are separate files
with the same drawing and their own id prefix. This table is printed by
`tools/build_prism.py --table`; the tests check that it matches.

<!-- BEGIN PRISM ICON TABLE -->
| Context | Design | Hue | Glyph | Theme icon names |
|---|---|---|---|---|
| apps | Settings | iris | Eight-tooth gear, accent hub. | `preferences-system`, `systemsettings` |
| apps | System Information | iris | Processor chip with pins, accent "i" (dot and blade stem). | `hwinfo` |
| apps | Terminal | slate | Console window with a prompt chevron, accent cursor. | `utilities-terminal`, `org.kde.konsole`, `boswas-terminal` |
| apps | File Manager | azure | Folder, accent pocket edge. | `system-file-manager`, `org.kde.dolphin`, `boswas-files` |
| apps | System Monitor | slate | Screen with an accent activity trace. | `utilities-system-monitor`, `org.kde.plasma-systemmonitor` |
| apps | Web Browser | azure | Globe with equator, accent meridian (a generic browser, not a vendor logo). | `web-browser`, `firefox-esr`, `konqueror` |
| apps | Calculator | slate | Calculator with plus, minus, times and equals keys, accent display. | `accessories-calculator`, `org.kde.kcalc` |
| apps | Text Editor | azure | Page with text lines, accent pencil. | `accessories-text-editor`, `kwrite`, `org.kde.kwrite`, `kate`, `org.kde.kate`, `gvim` |
| apps | Image Viewer | sun | Picture frame with mountains, accent sun. | `gwenview`, `org.kde.gwenview`, `preferences-desktop-wallpaper` |
| apps | Document Viewer | coral | Open book, accent bookmark ribbon. | `okular`, `org.kde.okular` |
| apps | Archive Manager | slate | Archive box with lid, accent handle slot. | `utilities-file-archiver`, `ark`, `org.kde.ark` |
| apps | Screenshot | coral | Viewfinder corners, accent shutter dot. | `spectacle`, `org.kde.spectacle` |
| apps | Help | azure | Ring, accent question mark. | `help-browser`, `org.kde.khelpcenter` |
| apps | Find Files | azure | Magnifying glass, accent handle. | `kfind` |
| apps | Wallet | mint | Wallet with a pocket flap, accent clasp. | `kwalletmanager` |
| apps | Partition Manager | slate | Disk as a pie, one accent partition pulled out. | `partitionmanager` |
| apps | Menu Editor | iris | Menu lines, accent pencil. | `kmenuedit` |
| apps | Process Viewer | slate | Console window with load bars, the tallest in the accent colour. | `htop` |
| apps | Report a Bug | coral | Beetle with legs and antennae, accent head. | `tools-report-bug` |
| apps | Emoji | orchid | Smiling face, accent smile. | `preferences-desktop-emoticons` |
| apps | Network | azure | Wireless arcs, accent source dot. | `preferences-system-network` |
| apps | Bluetooth | azure | Bluetooth rune, accent pairing dots. | `preferences-system-bluetooth` |
| apps | Audio | coral | Speaker, accent sound waves. | `preferences-desktop-sound` |
| apps | Display | iris | Monitor on a stand, accent foot. | `preferences-desktop-display`, `preferences-desktop-display-randr` |
| apps | Power | sun | Battery, accent lightning bolt. | `preferences-system-power-management` |
| apps | Users | iris | Two people, the one behind in the accent colour. | `preferences-system-users`, `system-users` |
| apps | Storage | slate | Two stacked drive units, accent activity light. | `boswas-storage` |
| apps | Personalization | orchid | Paint palette, one accent paint dot. | `preferences-desktop-theme-global` |
| apps | Privacy | mint | Padlock, accent keyhole. | `preferences-system-privacy`, `preferences-desktop-user-password` |
| apps | Devices | iris | Mouse, accent scroll wheel. | `preferences-desktop-peripherals`, `preferences-desktop-mouse` |
| apps | Updates | mint | Two circling arrows around an accent download arrow. | `system-software-update` |
| apps | Notifications | sun | Bell, accent clapper. | `preferences-desktop-notification-bell` |
| apps | Accessibility | azure | Ring, accent figure with outstretched arms. | `preferences-desktop-accessibility` |
| apps | Keyboard | iris | Keyboard with two key rows, accent space bar. | `preferences-desktop-keyboard` |
| apps | Language and Region | iris | Speech bubble, accent letter A (drawn as strokes). | `preferences-desktop-locale` |
| apps | Firewall | mint | Brick wall, one accent brick. | `preferences-security-firewall` |
| apps | Fonts | orchid | Letters "Aa" drawn as strokes, the small a in the accent colour. | `preferences-desktop-font` |
| apps | Window Management | iris | Two overlapping windows, accent title bar on the front one. | `preferences-system-windows`, `preferences-system-windows-actions` |
| apps | Calendar | coral | Calendar page with binder rings and days, one accent day. | `office-calendar`, `x-office-calendar` |
| apps | Clock | iris | Clock face, accent minute hand. | `preferences-system-time` |
| apps | Media Player | coral | Ring, accent play triangle. | `multimedia-player`, `applications-multimedia` |
| apps | Camera | coral | Camera body with lens ring, accent lens centre. | `camera-photo` |
| apps | Boswas OS | ink | The official Boswas OS mark (copied from desktop/branding/boswas-os-mark.svg). | `boswas-logo`, `boswas-os`, `start-here`, `start-here-kde`, `start-here-kde-plasma`, `distributor-logo` |
| apps | Boswas Store | azure | Shopping bag, accent handle. | `boswas-store` |
| apps | Compatibility Manager | sky | Application window, accent gear. | `boswas-compat-manager` |
| apps | Device Agent | iris | Laptop, accent heartbeat on the screen. | `boswas-device-agent` |
| apps | Security Center | mint | Shield, accent check mark. | `boswas-security-center` |
| apps | Control Center | iris | Three sliders, the middle knob in the accent colour. | `boswas-control-center` |
| apps | Update Center | mint | Circling arrow around an accent dot (the ring and dot of the mark). | `boswas-update-center` |
| apps | Software Center | iris | Grid of application tiles, one an accent disc. | `boswas-software-center` |
| apps | Install Boswas OS | ink | The official mark with an accent download badge. | `boswas-install` |
| apps | Windows Application | sky | Application window, accent sandbox shield. | `boswas-windows-application` |
| apps | Windows Installer | sky | Application window, accent badge with a download arrow. | `boswas-windows-installer` |
| apps | Windows Program Loader | sky | Application window, accent badge with a run (play) triangle. | `wine`, `wine-stable` |
| apps | Compatibility Status | sky | Application window, accent badge with a check mark. | `boswas-compat-status` |
| apps | Repair Application | sky | Application window, accent wrench. | `boswas-repair` |
| apps | Application Sandbox | sky | Shield enclosing an accent application window. | `boswas-sandbox` |
| apps | Application Policy | sky | Clipboard holding an application window, accent clip. | `boswas-app-policy` |
| apps | Boswas Launcher | iris | Three-by-three grid of application tiles, the centre one in the accent colour. | `boswas-launcher`, `boswas-shell` |
| apps | Lock Screen | iris | Screen with a padlock, accent lock body. | `boswas-lock` |
| places | Folder | azure | Folder: darker back with a tab, a sheet of paper, lighter front flap. | `folder` |
| places | Open Folder | azure | Open folder: the front flap leans forward. | `folder-open` |
| places | Home | azure | Folder with a house. | `user-home` |
| places | Desktop | azure | Folder with a screen on its stand. | `user-desktop` |
| places | Documents | azure | Folder with text lines. | `folder-documents` |
| places | Downloads | azure | Folder with a download arrow. | `folder-download`, `folder-downloads` |
| places | Music | azure | Folder with a note. | `folder-music`, `folder-sound` |
| places | Pictures | azure | Folder with mountains and a sun. | `folder-pictures`, `folder-images` |
| places | Videos | azure | Folder with a play triangle. | `folder-videos`, `folder-video` |
| places | Templates | azure | Folder with a page layout. | `folder-templates` |
| places | Public | azure | Folder with three connected share nodes. | `folder-publicshare` |
| places | Network | azure | Folder with a small network of three nodes. | `network-workgroup` |
| places | Remote Folder | azure | Folder with a globe. | `folder-remote` |
| places | Trash | slate | Waste bin with a lid and handle. | `user-trash` |
| places | Trash (Full) | slate | Waste bin without its lid, crumpled paper inside, one piece in the accent colour. | `user-trash-full` |
| devices | Computer | slate | Desktop computer: screen on a stand beside a tower, accent reflection and power light. | `computer` |
| devices | Laptop | slate | Laptop: open lid with an accent reflection, lighter base. | `computer-laptop` |
| devices | Display | slate | Screen on a stand, accent reflection. | `video-display` |
| devices | Internal Drive | slate | Internal drive: slab with a darker front strip, accent activity light. | `drive-harddisk` |
| devices | Removable Drive | slate | External drive: rounded slab with an accent eject symbol. | `drive-removable-media` |
| devices | USB Stick | slate | USB stick: metal plug with two contacts, body with an accent light. | `drive-removable-media-usb` |
| devices | Memory Card | slate | Memory card: cut corner, contact strips, accent label line. | `media-flash` |
| devices | Disc | slate | Disc with a centre hole and two accent reflections. | `media-optical` |
| devices | Phone | slate | Phone: tall body, dark screen with an accent reflection. | `phone` |
| devices | Printer | slate | Printer: paper in the feeder, printed page in the tray, accent status light. | `printer` |
| devices | Headphones | slate | Headphones: head band and two ear cups with accent pads. | `audio-headphones` |
| devices | Loudspeaker | slate | Loudspeaker: cabinet with a tweeter and a woofer, accent dust cap. | `audio-card` |
| devices | Keyboard | slate | Keyboard: two rows of light keys, accent space bar. | `input-keyboard` |
| devices | Mouse | slate | Mouse: body with button split, accent scroll wheel. | `input-mouse` |
| devices | Webcam | slate | Webcam: round head on a foot, dark lens with an accent centre. | `camera-web` |
| devices | Wired Network | slate | Network socket: dark port, accent contacts. | `network-wired` |
| devices | Wireless Network | slate | Wireless router: body with two antennas and status lights, accent radio waves. | `network-wireless` |
| mimetypes | Text | slate | Page with text lines. | `text-plain`, `text-x-generic` |
| mimetypes | Web Page | azure | Page with angle brackets and a slash. | `text-html` |
| mimetypes | Script | mint | Page with a prompt chevron and a cursor line. | `text-x-script` |
| mimetypes | Image | sun | Page with mountains and a sun. | `image-x-generic` |
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
| mimetypes | Folder | azure | Folder: darker back with a tab, a sheet of paper, lighter front flap. | `inode-directory` |
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
| status | Default | symbolic | Disc with a check mark. | `emblem-default-symbolic` |
| actions | Close Window | symbolic | Cross. | `window-close-symbolic` |
| actions | Maximize Window | symbolic | Square. | `window-maximize-symbolic` |
| actions | Minimize Window | symbolic | Horizontal line. | `window-minimize-symbolic` |
| actions | Restore Window | symbolic | Square in front of a second one. | `window-restore-symbolic` |
| actions | Search | symbolic | Magnifying glass. | `system-search-symbolic` |
| actions | All Applications | symbolic | Three-by-three grid of squares. | `view-app-grid-symbolic` |
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
| actions | Play | symbolic | Filled triangle pointing right. | `media-playback-start-symbolic` |
| actions | Pause | symbolic | Two upright bars. | `media-playback-pause-symbolic` |
| actions | Stop | symbolic | Filled square. | `media-playback-stop-symbolic` |
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
<!-- END PRISM ICON TABLE -->

Notes on names:

* **Every legacy name is kept.** Each icon name of the legacy pack exists in
  Prism, so a component that asks for `boswas-control-center` or
  `org.kde.dolphin` finds it in either pack.
* **Native desktop.** `boswas-launcher` (`boswas-shell`), `boswas-lock`,
  `boswas-files` and `boswas-terminal` are the names of the native shell.
  The shell loads the symbolic icons by the names in the table, from
  `scalable/status` and `scalable/actions`.
* **Folders.** `folder-downloads`, `folder-images`, `folder-video` and
  `folder-sound` are the names KDE's file manager asks for;
  `inode-directory` is the folder as a mimetype.
* **Windows programs.** As in the legacy pack, the program page is installed
  under all four names that shared-mime-info and KDE use for `.exe` files.

### Regenerating

The generator needs Python 3 (standard library only) and reads
`desktop/ui/tokens.json`, `desktop/branding/boswas-os-mark.svg` and
`tools/build_icons.py`:

```sh
python3 desktop/icons/tools/build_prism.py          # rewrite both Prism themes
python3 desktop/icons/tools/build_prism.py --table  # print the table above
```

It is deterministic and removes SVGs it no longer generates. Re-run it after
changing a glyph (in either generator) or a token, and commit the
regenerated files; the tests fail when they are stale.

### Adding an icon

1. Write a glyph function in `tools/build_prism.py` with a one-line
   docstring describing the drawing. Return a list of elements built with
   `S(d, paint, width)` (line art), `F(d, paint)` (filled shape) and
   `FS(d, paint, width)` (filled shape with softened corners). A paint is a
   name with a `$`: `$glyph` and `$accent` on a tile, `$c300` to `$c700`
   (shades of the icon's hue) and `$strong` on objects and pages, `$sym` in a
   symbolic icon, any palette colour such as `$ink-900`, or a gradient of
   `GRADIENTS`. Symbolic glyphs use `y_s`, `y_f` and `y_fs`.
2. Add a row to the list of its context (`app_icons`, `FOLDER_ICONS`,
   `DEVICE_ICONS`, `MIME_ICONS`, `STATUS_ICONS`, `ACTION_ICONS`) with its
   hue. A design that should exist in both packs goes into
   `tools/build_icons.py` instead, with its hue in `APP_HUES`.
3. Run the generator, render a contact sheet (below) and look at the icon
   at 256, 48, 32 and 16 px next to its neighbours, on dark and on light.
4. Paste the new `--table` output between the markers above, add the name
   to the required list of `tests/test_prism.py` if a component depends on
   it, and run the tests.

### Previewing

```sh
python3 desktop/icons/tools/contact_sheet.py /tmp/prism.png --prism
python3 desktop/icons/tools/contact_sheet.py /tmp/prism-apps.png --prism --contexts apps
python3 desktop/icons/tools/contact_sheet.py /tmp/prism-symbolic-light.png --prism --light \
    --contexts status,actions --sizes 64,24,16
```

`--light` puts the sheet on a light background and takes the symbolic icons
from `Boswas-Prism-Light/`.

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
installed themes, that every SVG parses, has the 256 viewBox, is
self-contained and uses only the elements above, that every colour is a
token (or the mark's own), that the tiles keep the contrast rules and use
nothing but their hue, that symbolic icons have one colour and differ
between the themes only in it, that the logo carries the official mark's
path data, that aliases match their design, that both `index.theme` files
are valid, that `install.sh` installs real files with the right modes, that
the table above is current, and that the committed SVGs match a fresh run
of the generator byte for byte.

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

### Regenerating

The generator needs Python 3 (standard library only) and reads
`desktop/branding/boswas-os-mark.svg`:

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
file-prefixed ids, that the logo carries the official mark's path data,
that aliases match their design, that both `index.theme` files are valid,
that `install.sh` installs real files with the right modes, that the table
above is current, and that the committed SVGs match a fresh run of the
generator byte for byte.
