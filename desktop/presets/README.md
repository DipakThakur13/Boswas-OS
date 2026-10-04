# Boswas OS visual presets

Ten complete looks for the Boswas OS desktop (KDE Plasma 6.3). A preset
bundles a Plasma Global Theme, a KDE colour scheme, a wallpaper (light and
dark artwork), a lock screen background, a Konsole colour scheme and profile,
and a preview image for the Boswas Control Center.

`presets.json` is the single source of truth. Everything in `color-schemes/`,
`konsole/`, `look-and-feel/`, `wallpapers/` and `previews/` is generated from
it by `tools/build_presets.py`: do not edit those files by hand.

## The design system

All ten presets are one family. Only the accent colour, light or dark, and
the mood (the wallpaper motif) change; everything else is shared.

* **Brand.** The Boswas OS mark (gold blade-shaped stem and dot, silver ring
  and swoosh), the navy and silver palette of `desktop/branding/README.md`,
  and the logo gold as the flagship accent. No teal (the retired accent) in
  any colour, artwork or terminal palette; the tests enforce it.
* **Typography.** Brand lettering in Orbitron (outlined wordmarks); the
  desktop UI in the KDE default face; terminals in Hack 11. The wallpapers
  carry no lettering.
* **Wallpaper geometry.** Every wallpaper is built from the mark itself:
  * the *orbit*: rings concentric with the mark's ring, around a focus point
    on the right (the left side stays calm for desktop icons and windows);
  * the *trail*: an arc on one ring ending in the *dot*, a satellite in the
    accent colour;
  * the *blade*: the mark's stem at large scale, as a glass plane, a light
    shaft, a dashed construction line (Carbon) or a hairline (Classic);
  * the mark at the focus, faint (10 to 30 % opacity, at most 600 px high on
    a 3840x2160 canvas): subtle, never a giant logo.

  The motif sets the mood: a planet horizon lit in gold (Horizon), aurora
  ribbons (Aurora), an engineering grid and dial (Carbon), calm swells
  (Ocean), a rising warm current with embers (Ember), nebula clouds and stars
  (Nebula), and so on. Artwork is smooth vector gradients without noise, so
  the PNGs stay small.
* **Light and dark artwork.** Every wallpaper package has both:
  `contents/images/` holds the light artwork and `contents/images_dark/` the
  dark one; Plasma picks `images_dark/` when the colour scheme is dark, and
  switches live when the scheme changes.
* **Lock screen.** A calmer variant of the preset's own artwork: the orbit
  moves right, the blade and the second trail go, the centre and left stay
  free for the clock and the password field.
* **Colour.** WCAG AA everywhere: normal text, secondary (muted) text, links
  and status colours reach 4.5:1 against every background they are used on
  (window, view, button, header, tooltip, complementary); selected text
  reaches 4.5:1 on the selection; focus decorations reach 3:1. **Carbon**,
  the high-contrast preset, reaches 7:1 for text (and 4.5:1 for focus).
  Selections that are light (gold, silver, ice blue...) carry dark text.
  Warnings (KDE "neutral") are orange or amber and always clearly different
  from the accent (CIELAB difference of at least 20; on Ember, whose accent
  is copper, the warning colour is amber).

### Tokens

| Preset | Variant | Window | View | Header | Selection / text | Accent | Text / muted | Warning | Terminal bg / fg |
|---|---|---|---|---|---|---|---|---|---|
| **Horizon** (`horizon`) | dark | `#111A2B` | `#0D1524` | `#0B1220` | `#D9B26E` / `#0B1220` | `#D9B26E` | `#E8EEF6` / `#A8B3C7` | `#F09646` | `#0D1524` / `#E8EEF6` |
| **Midnight** (`midnight`) | dark | `#0B0E14` | `#07090D` | `#06080B` | `#B39762` / `#0B0E14` | `#B39762` | `#D3DAE4` / `#8E98AA` | `#D98E4E` | `#07090D` / `#C9D1DC` |
| **Aurora** (`aurora`) | dark | `#0F1830` | `#0B1226` | `#0A1124` | `#7CC4FF` / `#06101F` | `#7CC4FF` | `#E6EEFA` / `#A3B2CC` | `#F2A04A` | `#0A1124` / `#E6EEFA` |
| **Slate** (`slate`) | dark | `#1E2530` | `#181E27` | `#171C24` | `#B4BFD1` / `#111821` | `#B4BFD1` | `#E9EDF3` / `#A9B3C2` | `#EE9A4C` | `#181E27` / `#E9EDF3` |
| **Carbon** (`carbon`) | dark | `#0C0D10` | `#000000` | `#000000` | `#FFC844` / `#000000` | `#FFC844` | `#FFFFFF` / `#C9CFD8` | `#FF9548` | `#000000` / `#FFFFFF` |
| **Pearl** (`pearl`) | light | `#F4F1EA` | `#FFFDF8` | `#E8E3D7` | `#D2AC66` / `#141C2B` | `#9A7431` | `#141C2B` / `#59606E` | `#9E4D00` | `#FFFDF8` / `#1B2333` |
| **Ocean** (`ocean`) | dark | `#0E1D33` | `#0A1628` | `#091426` | `#57A8EA` / `#04101E` | `#57A8EA` | `#E5EEF8` / `#A0B4CC` | `#F0A04B` | `#0A1628` / `#E5EEF8` |
| **Ember** (`ember`) | dark | `#1A1719` | `#141113` | `#120F11` | `#E28A5C` / `#1A0D07` | `#E28A5C` | `#F2EAE4` / `#B9ACA5` | `#E8C34E` | `#141113` / `#F2EAE4` |
| **Nebula** (`nebula`) | dark | `#16142E` | `#100E24` | `#0E0C20` | `#A68CFF` / `#120E26` | `#A68CFF` | `#ECEAFA` / `#AAA5CC` | `#F2A04E` | `#100E24` / `#ECEAFA` |
| **Classic** (`classic`) | light | `#EEF0F3` | `#FFFFFF` | `#E1E5EB` | `#2F6EB5` / `#FFFFFF` | `#2F6EB5` | `#1D2533` / `#586374` | `#984A00` | `#1D2330` / `#E6EAF0` |

The complete palettes (alternate backgrounds, buttons, tooltips, the
complementary set, links, the 16 terminal colours) and the wallpaper
parameters are in `presets.json`.

### The presets

| Preset | Intent | Wallpaper motif | Panel | Icons |
|---|---|---|---|---|
| **Horizon** | The flagship and the default for new users: deep navy, logo gold accent, silver detail. Modern, premium. Replaces the former "Boswas" Global Theme and "Boswas Dark" colours. | A planet horizon with a gold rim light under the orbit | floating | Boswas |
| **Midnight** | Developers and dim rooms: near-black surfaces, softened text (not pure white), muted gold. Low glare. | Almost nothing: faint meridians, a hairline blade | floating | Boswas |
| **Aurora** | Futuristic: Boswas navy lit by a luminous ice-blue accent; the gold stays in the mark. | Aurora ribbons and light filaments | floating | Boswas |
| **Slate** | Minimal enterprise: slate grey-blue, silver accent with dark text on selections. | A glass blade and one horizon hairline | floating | Boswas |
| **Carbon** | Engineering and security: black, white text, bright gold focus colour; 7:1 text contrast. | An engineering grid, dial ticks, the blade as a dashed construction line | floating | Boswas |
| **Pearl** | Clean light interface: ivory and pearl surfaces, navy text, gold highlights. | Pearl sheen and a glass blade with a gold edge | floating | Boswas-Light |
| **Ocean** | Calm everyday productivity: deep blues, ocean-blue accent. | Calm swells under a shaft of light | floating | Boswas |
| **Ember** | Creative workstation: warm dark surfaces, strong copper accent. | A warm current rising from the lower right, embers | floating | Boswas |
| **Nebula** | Research and AI: deep indigo and violet, lavender accent. | Nebula clouds and a sparse star field | floating | Boswas |
| **Classic** | A familiar, conservative, lightweight desktop: light neutral windows, a blue accent, a plain wallpaper (one gradient, about 120 KB). | None beyond the orbit | full width, not floating, labelled task manager | Boswas-Light |

Every Global Theme uses the Breeze application style and window decoration,
the Breeze cursors, and the Plasma style `default` (Breeze), which follows
the colour scheme: panels and widgets take each preset's own colours (ivory
on Pearl, navy on Horizon). `breeze-dark` and `breeze-light` would ignore
the preset palette.

The icon themes `Boswas` and `Boswas-Light`, the Boswas Launcher applet
(`com.boswas.launcher`), the splash screen (`com.boswas.splash`) and the
terminal start file (`/usr/share/boswas/terminal/bashrc`) are provided by
other parts of Boswas OS; the presets only refer to them.

### Panel layout

Every layout script starts from KDE's stock default panel
(`loadTemplate("org.kde.plasma.desktop.defaultPanel")`: launcher, pager, task
manager, system tray, clock, so Plasma upgrades keep working) and then:

* replaces Kickoff with the Boswas Launcher in the same position,
  configured with `icon=boswas-logo`, the favourites (Control Center,
  Dolphin, Konsole, Firefox ESR, Compatibility Manager, KCalc) and the system
  applications (Control Center, Security Center, Info Center). If the Boswas
  Launcher is not installed, Kickoff stays, with the same settings;
* pins Control Center, Dolphin, Konsole, Firefox ESR and Compatibility
  Manager in the task manager;
* sets the panel style: floating with the icon-only task manager, or, for
  Classic, a full-width non-floating panel with the labelled task manager
  (`org.kde.plasma.taskmanager`).

A Plasma 6 panel puts a new widget at its end (its view exists while the
script runs, so a written `AppletOrder` would be overwritten). To put a
replacement in the place of the widget it replaces, the script removes that
widget and every widget after it and creates them again, in order; they are
fresh from the template, so no setting is lost.

It uses only the Plasma 6.3 desktop scripting API (`knownWidgetTypes`,
`panel.widgetIds`, `widgetById()`, `widget.type`, `addWidget()`, `remove()`,
`writeConfig()`, `panel.floating`, `panel.lengthMode`, `panel.height`).

## Applying a preset

### Global Theme settings

The ten Global Themes appear in System Settings > Colors & Themes > Global
Theme as "Boswas Horizon" ... "Boswas Classic". Applying one there changes
the colours, icons, Plasma style, cursor and window decoration (and, if
"Desktop and window layout" is ticked, the panels). In Plasma 6.3 it does
**not** change the wallpaper, the lock screen or the terminal profile; use
`boswas-preset` (or the Boswas Control Center, which calls it) for the
whole preset.

### boswas-preset

```sh
boswas-preset list                 # the presets, * marks the current one
boswas-preset --json list          # {"presets": [{"id", "name", "description",
                                   #   "variant", "accent", "preview"}], "current"}
boswas-preset current              # the current preset id
boswas-preset apply ocean          # apply for the logged-in user
boswas-preset apply ocean --layout # ... and replace the panels with the preset's layout
```

`apply` refuses to run as root (exit 4) and runs, with fixed argument lists
and never a shell:

1. `plasma-apply-lookandfeel -a com.boswas.<id>` (plus `--resetLayout` with
   `--layout`): colour scheme, icons, Plasma style, cursor, window
   decoration, panels;
2. `plasma-apply-wallpaperimage /usr/share/wallpapers/Boswas-<Label>`: the
   wallpaper package, so Plasma shows the light or dark artwork to match the
   colour scheme;
3. `kwriteconfig6 --file kscreenlockerrc --group Greeter --group Wallpaper
   --group org.kde.image --group General --key Image
   file:///usr/share/boswas/presets/lock/<id>.png`: the lock screen;
4. `kwriteconfig6 --file konsolerc --group "Desktop Entry" --key
   DefaultProfile "Boswas <Label>.profile"`: the default Konsole profile;
5. `kwriteconfig6 --file boswasrc --group Preset --key Current <id>`.

Exit status: 0 success, 1 a step failed (the message names the step, the
remaining steps are not run) or the presets file is unreadable, 2 unknown
preset, 3 usage error, 4 run as root. With `--json`, success prints
`{"applied": "<id>", "layout": true|false}` and errors print
`{"error", "code", "step"}`.

`current` reads `Current` in `$XDG_CONFIG_HOME/boswasrc` (default
`~/.config/boswasrc`); when nothing is recorded there, it reports the preset
whose Global Theme is active (`[KDE] LookAndFeelPackage` in kdeglobals, the
user's or the system default), so a new user sees Horizon as current. The
preset list comes from `/usr/share/boswas/presets/presets.json`
(`BOSWAS_PRESETS_FILE` overrides it, for tests).

### Notes on Plasma 6.3 behaviour

* `plasma-apply-lookandfeel` never applies the Global Theme's wallpaper.
  `[Wallpaper] Image` in a theme's `defaults` is only the default for desktops
  that have no image set (read when the desktop loads); a new user's desktop
  therefore shows the default Global Theme's wallpaper. `boswas-preset`
  applies the wallpaper explicitly.
* The splash screen is applied with a Global Theme only when the theme
  package contains a splash itself, which these do not: the `[KSplash]`
  entries name `com.boswas.splash` for completeness, but the splash has to
  be set system-wide (`ksplashrc`).
* `--resetLayout` asks plasmashell to reload the layout and returns at once;
  the wallpaper step runs after it.

### Login screen

The login screen (SDDM) is system-wide and does not follow a user's preset.
Its background is `/usr/share/boswas/branding/login-background.png`, the
Horizon lock screen artwork. An administrator who wants the login screen to
match a preset can apply it, then use System Settings > Colors & Themes >
Login Screen (SDDM) > Apply Plasma Settings, which copies their Plasma
settings (colours, fonts, cursor theme) to the login screen. The lock screen
is per user and follows the preset.

## Files

| Path | What |
|---|---|
| `presets.json` | The presets: palettes, terminal colours, wallpaper parameters, panel style, icon theme |
| `tools/build_presets.py` | The generator (Python 3 standard library) |
| `tools/repack_png.py` | Re-deflates rendered PNGs at maximum compression (used by `install.sh`) |
| `color-schemes/Boswas<Label>.colors` | KDE colour schemes (generated) |
| `konsole/Boswas<Label>.colorscheme`, `konsole/Boswas <Label>.profile` | Konsole (generated) |
| `look-and-feel/com.boswas.<id>/` | Global Themes: `metadata.json`, `contents/defaults`, `contents/layouts/org.kde.plasma.desktop-layout.js` (generated) |
| `wallpapers/Boswas-<Label>/` | `metadata.json`, `light.svg`, `dark.svg`, `lock.svg` (generated) |
| `wallpapers/Boswas/metadata.json` | The default wallpaper name `Boswas` (the system defaults use it); `install.sh` fills it with Horizon's images |
| `previews/<id>.svg` | The wallpaper with a schematic window and panel in the preset colours (generated) |
| `boswas_preset/`, `bin/boswas-preset` | The `boswas-preset` tool |
| `install.sh` | Renders and installs everything into a DESTDIR |
| `tests/` | Unit tests |

## Regenerating and testing

```sh
python3 desktop/presets/tools/build_presets.py          # regenerate after editing presets.json
python3 desktop/presets/tools/build_presets.py --check  # exit 1 if generated files are stale
cd desktop/presets && python3 -B -m unittest discover -s tests -v
BOSWAS_PRESETS_FULL_INSTALL=1 python3 -B -m unittest discover -s tests -p test_install.py   # full install
```

The tests check `presets.json`, that the generated files are up to date, the
colour schemes (every group and key, valid colours, the contrast rules, the
warning colours), the Global Themes (metadata, references to existing colour
schemes, wallpapers, icon themes and `com.boswas.splash`, the layout
scripts), the Konsole files, the wallpaper sources (inlining and rendering
them), the absence of teal, and `boswas-preset` with a fake command runner.
`test_layout_execution.py` also runs the layout scripts, with KDE's default
panel template, against a mock of the Plasma scripting API; it needs the
`py_mini_racer` module (V8) and is skipped without it.

## Installing

```sh
desktop/presets/install.sh DESTDIR
```

needs `python3` and `rsvg-convert` (librsvg2-bin); with netpbm (`pngtopnm`,
`pnmtojpeg`) it also writes each Global Theme's `fullscreenpreview.jpg`. It
refuses to run if the generated files are stale, inlines the brand assets
with `build/scripts/inline-svg.py`, renders every wallpaper at 3840x2160
(one size; Plasma scales it), and installs files with mode 0644 (the tool
0755):

| Installed | From |
|---|---|
| `/usr/share/color-schemes/Boswas<Label>.colors` | `color-schemes/` |
| `/usr/share/plasma/look-and-feel/com.boswas.<id>/` | `look-and-feel/`, plus `contents/previews/preview.png` (600x338) and `fullscreenpreview.jpg` (1920x1080) |
| `/usr/share/wallpapers/Boswas-<Label>/` | `metadata.json`, `contents/images/3840x2160.png` (light), `contents/images_dark/3840x2160.png` (dark), `contents/screenshot.png` |
| `/usr/share/wallpapers/Boswas/` | Horizon's images (hard links) |
| `/usr/share/konsole/` | `konsole/` |
| `/usr/share/boswas/presets/lock/<id>.png` | `lock.svg`, 3840x2160 |
| `/usr/share/boswas/presets/previews/<id>.png` | `previews/<id>.svg`, 480x270 |
| `/usr/share/boswas/presets/presets.json` | `presets.json` |
| `/usr/share/boswas/branding/login-background.png` | Horizon's lock screen (hard link) |
| `/usr/lib/boswas/python/boswas_preset/`, `/usr/bin/boswas-preset` | the tool |

Everything installed takes about 27 MB (149 files; the hard-linked
copies are stored once).
