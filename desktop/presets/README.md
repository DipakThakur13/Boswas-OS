# Boswas OS visual presets

Ten looks for the Boswas OS desktop. A preset bundles light or dark, a
wallpaper (light and dark artwork), lock screen art, a colour scheme for KDE
applications, and a preview image for the Boswas Control Center.

Applying a preset sets what the Boswas desktop (`boswas-compositor`,
`boswas-shell`) takes from it: light or dark for the shell, the window
frames and the lock screen, and the wallpaper. For KDE applications a user
may have installed it also sets their colours. A new user starts with the
Boswas Prism look ([`../ui/`](../ui/README.md)), which is not one of the ten
presets.

The presets were first made for the KDE Plasma image. What only Plasma and
Konsole used is gone (ADR-0042 in
[`docs/architecture/decisions.md`](../../docs/architecture/decisions.md)):
the Global Themes with their panel layouts, the Konsole colour schemes and
profiles, and the Plasma branch of `boswas-preset`.

`presets.json` is the single source of truth. Everything in `color-schemes/`,
`wallpapers/` and `previews/` is generated from it by
`tools/build_presets.py`: do not edit those files by hand.

## The design system

All ten presets are one family. Only the accent colour, light or dark, and
the mood (the wallpaper motif) change; everything else is shared.

* **Brand.** The Boswas OS mark (a lowercase "b": a pill-shaped stem in
  front of a round bowl, a warm dot in the counter; drawn by
  `desktop/branding/tools/build_mark.py`). The presets keep their own
  palettes (navy, silver and gold among them); the mark brings its blues.
  No teal (the retired accent) in any colour or artwork; the tests enforce
  it.
* **Typography.** Brand lettering in Inter (outlined wordmarks). The
  wallpapers carry no lettering.
* **Wallpaper geometry.** Every wallpaper is built from the mark itself:
  * the *orbit*: rings concentric with the mark's bowl, around a focus point
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
  dark one. `boswas-preset` names the one that matches the preset's variant;
  Control Center (Personalization > Colour mode) switches to the other one
  when the user changes the mode.
* **Lock screen art.** A calmer variant of the preset's own artwork: the
  orbit moves right, the blade and the second trail go, the centre and left
  stay free for a clock and a password field. It is rendered and installed;
  the lock screen of the Boswas desktop does not show it (see
  [What a preset does not change](#what-a-preset-does-not-change)).
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

| Preset | Variant | Window | View | Header | Selection / text | Accent | Text / muted | Warning |
|---|---|---|---|---|---|---|---|---|
| **Horizon** (`horizon`) | dark | `#111A2B` | `#0D1524` | `#0B1220` | `#D9B26E` / `#0B1220` | `#D9B26E` | `#E8EEF6` / `#A8B3C7` | `#F09646` |
| **Midnight** (`midnight`) | dark | `#0B0E14` | `#07090D` | `#06080B` | `#B39762` / `#0B0E14` | `#B39762` | `#D3DAE4` / `#8E98AA` | `#D98E4E` |
| **Aurora** (`aurora`) | dark | `#0F1830` | `#0B1226` | `#0A1124` | `#7CC4FF` / `#06101F` | `#7CC4FF` | `#E6EEFA` / `#A3B2CC` | `#F2A04A` |
| **Slate** (`slate`) | dark | `#1E2530` | `#181E27` | `#171C24` | `#B4BFD1` / `#111821` | `#B4BFD1` | `#E9EDF3` / `#A9B3C2` | `#EE9A4C` |
| **Carbon** (`carbon`) | dark | `#0C0D10` | `#000000` | `#000000` | `#FFC844` / `#000000` | `#FFC844` | `#FFFFFF` / `#C9CFD8` | `#FF9548` |
| **Pearl** (`pearl`) | light | `#F4F1EA` | `#FFFDF8` | `#E8E3D7` | `#D2AC66` / `#141C2B` | `#9A7431` | `#141C2B` / `#59606E` | `#9E4D00` |
| **Ocean** (`ocean`) | dark | `#0E1D33` | `#0A1628` | `#091426` | `#57A8EA` / `#04101E` | `#57A8EA` | `#E5EEF8` / `#A0B4CC` | `#F0A04B` |
| **Ember** (`ember`) | dark | `#1A1719` | `#141113` | `#120F11` | `#E28A5C` / `#1A0D07` | `#E28A5C` | `#F2EAE4` / `#B9ACA5` | `#E8C34E` |
| **Nebula** (`nebula`) | dark | `#16142E` | `#100E24` | `#0E0C20` | `#A68CFF` / `#120E26` | `#A68CFF` | `#ECEAFA` / `#AAA5CC` | `#F2A04E` |
| **Classic** (`classic`) | light | `#EEF0F3` | `#FFFFFF` | `#E1E5EB` | `#2F6EB5` / `#FFFFFF` | `#2F6EB5` | `#1D2533` / `#586374` | `#984A00` |

These are the colours of the scheme for KDE applications and of the preview
picture. The complete palettes (alternate backgrounds, buttons, tooltips,
the complementary set, links) and the wallpaper parameters are in
`presets.json`.

### The presets

| Preset | Intent | Wallpaper motif |
|---|---|---|
| **Horizon** | The flagship: deep navy, logo gold accent, silver detail. Modern, premium. Its wallpaper is also installed under the default wallpaper name `Boswas`. | A planet horizon with a gold rim light under the orbit |
| **Midnight** | Developers and dim rooms: near-black surfaces, softened text (not pure white), muted gold. Low glare. | Almost nothing: faint meridians, a hairline blade |
| **Aurora** | Futuristic: Boswas navy lit by a luminous ice-blue accent; the gold stays in the mark. | Aurora ribbons and light filaments |
| **Slate** | Minimal enterprise: slate grey-blue, silver accent with dark text on selections. | A glass blade and one horizon hairline |
| **Carbon** | Engineering and security: black, white text, bright gold focus colour; 7:1 text contrast. | An engineering grid, dial ticks, the blade as a dashed construction line |
| **Pearl** | Clean light interface: ivory and pearl surfaces, navy text, gold highlights. | Pearl sheen and a glass blade with a gold edge |
| **Ocean** | Calm everyday productivity: deep blues, ocean-blue accent. | Calm swells under a shaft of light |
| **Ember** | Creative workstation: warm dark surfaces, strong copper accent. | A warm current rising from the lower right, embers |
| **Nebula** | Research and AI: deep indigo and violet, lavender accent. | Nebula clouds and a sparse star field |
| **Classic** | A familiar, conservative look: light neutral windows, a blue accent, a plain wallpaper (one gradient, about 120 KB). | None beyond the orbit |

### Previews

`previews/<id>.svg` is the wallpaper with a schematic window and a panel in
the preset's colours; Control Center shows it on the preset's card. The
sketch still shows the panel as the Plasma image had it: floating, and for
Classic full width with labelled tasks (`"preview": {"panel": ...}` in
`presets.json` selects the drawing). It is a drawing only: the Boswas
desktop has one panel, and a preset does not change it.

## Applying a preset

```sh
boswas-preset list                 # the presets, * marks the current one
boswas-preset --json list          # {"presets": [{"id", "name", "description",
                                   #   "variant", "accent", "preview"}], "current"}
boswas-preset current              # the current preset id
boswas-preset apply ocean          # apply for the signed-in user
boswas-preset reset                # back to the default look, Boswas Prism
```

Control Center > Personalization calls the same commands. `apply` and
`reset` refuse to run as root (exit 4).

`boswas-preset apply <id>` does this, itself; it starts no other program:

1. writes `~/.config/boswas/appearance.toml`:

   ```toml
   # Written by boswas-preset; changes made here are replaced by the next preset.
   [appearance]
   scheme = "dark"
   wallpaper = "/usr/share/wallpapers/Boswas-Ocean/contents/images_dark/3840x2160.png"
   ```

   `scheme` is the preset's variant (light or dark) and `wallpaper` the
   matching artwork. `boswas-compositor` reads the file as its last
   configuration layer (it may contain the `[appearance]` table only) and
   passes both values to the shell: window frames, panel, launcher and lock
   screen switch between the Prism dark and light themes, and the shell
   shows the wallpaper;
2. merges the colour groups (`[Colors:*]`, `[WM]`) of
   `/usr/share/color-schemes/Boswas<Label>.colors` into the user's
   `~/.config/kdeglobals` and sets `ColorScheme=Boswas<Label>` there. KDE
   applications read their colours from that file; every other group in it
   is kept;
3. records the choice: `Current=<id>` in the `[Preset]` group of
   `~/.config/boswasrc`. Every other group and key of that file is kept;
4. asks the running compositor to re-read its configuration
   (`reload-config` on its control socket). Without a running compositor
   the look applies at the next login.

`~/.config` stands for `$XDG_CONFIG_HOME` when that is an absolute path.

The three files are written the same way. The new content of all three is
prepared first, so a file that cannot be used stops the command before
anything has changed. Each file is then replaced atomically: a new file with
mode 0600 in the same directory, synced, then renamed over the old one. A
target that is a symbolic link, or anything but a regular file, is refused
(the step fails and names the path), and so is a symbolic link in the place
of `~/.config/boswas`: nothing is read or written through a link below the
configuration directory. A `kdeglobals` or `boswasrc` larger than 1 MiB is
refused as well. Bytes that are not UTF-8 in those two files are kept as
they are.

Running applications pick the new colours up when they next read
`kdeglobals`; an application that is already open may need a restart.

`boswas-preset current` reports the recorded choice, if it names one of the
presets. Files written by earlier versions, which ran KDE's `kwriteconfig6`
for this, have the same format and are read as before. A user who has not
applied a preset has the Boswas Prism look, and `current` prints nothing
(`{"current": null}` with `--json`).

`boswas-preset reset` returns to Prism after a preset: it removes
`~/.config/boswas/appearance.toml` (the compositor and the shell fall back
to Prism), merges the Boswas Prism colours
(`/usr/share/color-schemes/BoswasPrism.colors`) into `~/.config/kdeglobals`,
removes `Current` from `~/.config/boswasrc` (and the file, if nothing else
is in it) and asks the compositor to reload. `~/.config/boswas/compositor.toml`,
the user's own settings, is not touched. Control Center's "Reset to Boswas
Default" button runs it.

Exit status: 0 success, 1 a step failed (the message names the step:
`appearance`, `colours` or `record`) or the presets file is unreadable,
2 unknown preset, 3 usage error, 4 run as root. With `--json`, success
prints `{"applied": "<id>"}` or `{"reset": true, "default": "Boswas Prism"}`
and errors print `{"error", "code", "step"}`.

The preset list comes from `/usr/share/boswas/presets/presets.json`
(`BOSWAS_PRESETS_FILE` overrides it and `BOSWAS_COLOR_SCHEMES` the directory
of the colour schemes, for tests). Identifiers and labels in it are checked
before they become parts of file names.

### What a preset does not change

- the accent colour of the panel, launcher, window frames and lock screen:
  they use the Prism accent in every preset;
- the icon theme (Boswas Prism stays) and the panel;
- the colours of Boswas Terminal: it has its own colour schemes. Its
  default one follows light or dark like the rest of the desktop and takes
  no colour from a preset;
- the lock and login screens' background. The lock screen is per user: it
  follows the preset's light or dark variant and has the same background as
  the desktop. The login screen (`boswas-shell --greeter`) is system-wide,
  in the Prism dark theme with the default background. The lock screen art
  (`/usr/share/boswas/presets/lock/<id>.png`) and
  `/usr/share/boswas/branding/login-background.png` (a copy of Horizon's)
  are installed and nothing in the tree reads them.

## Files

| Path | What |
|---|---|
| `presets.json` | The presets: names, light or dark, palettes, wallpaper parameters, the panel of the preview sketch |
| `tools/build_presets.py` | The generator (Python 3 standard library) |
| `tools/repack_png.py` | Re-deflates rendered PNGs at maximum compression (used by `install.sh`) |
| `color-schemes/Boswas<Label>.colors` | Colour schemes for KDE applications (generated) |
| `wallpapers/Boswas-<Label>/` | `metadata.json`, `light.svg`, `dark.svg`, `lock.svg` (generated) |
| `wallpapers/Boswas/metadata.json` | The default wallpaper name `Boswas`; `install.sh` fills it with Horizon's images |
| `previews/<id>.svg` | The wallpaper with a schematic window and panel in the preset colours (generated) |
| `boswas_preset/`, `bin/boswas-preset` | The `boswas-preset` tool |
| `man/boswas-preset.1` | Its manual page |
| `install.sh` | Renders and installs everything into a DESTDIR |
| `tests/` | Unit tests |

## Regenerating and testing

```sh
python3 desktop/presets/tools/build_presets.py          # regenerate after editing presets.json
python3 desktop/presets/tools/build_presets.py --check  # exit 1 if generated files are stale
cd desktop/presets && python3 -B -m unittest discover -s tests -v
BOSWAS_PRESETS_FULL_INSTALL=1 python3 -B -m unittest discover -s tests -p test_install.py   # full install
```

The tests check `presets.json`, that the generated files are up to date and
that nothing is generated for Plasma or Konsole, the colour schemes (every
group and key, valid colours, the contrast rules, the warning colours), the
wallpaper sources (inlining and rendering them), the absence of teal, and
`boswas-preset` in a temporary home: the three files it writes, the merge
into `kdeglobals`, what stays in `boswasrc`, the refusal of symbolic links
and other odd targets, a failing write, `reset`, the request to the
compositor (a socket below `/tmp` stands in for it), and that the tool
starts no program and names no KDE program.

## Installing

```sh
desktop/presets/install.sh DESTDIR
```

needs `python3` and `rsvg-convert` (librsvg2-bin). It refuses to run if the
generated files are stale, inlines the brand assets with
`build/scripts/inline-svg.py`, renders every wallpaper at 3840x2160 (one
size; the shell scales it), and installs files with mode 0644 (the tool
0755):

| Installed | From |
|---|---|
| `/usr/share/color-schemes/Boswas<Label>.colors` | `color-schemes/` |
| `/usr/share/wallpapers/Boswas-<Label>/` | `metadata.json`, `contents/images/3840x2160.png` (light), `contents/images_dark/3840x2160.png` (dark), `contents/screenshot.png` (480x270) |
| `/usr/share/wallpapers/Boswas/` | Horizon's images (copies) |
| `/usr/share/boswas/presets/lock/<id>.png` | `lock.svg`, 3840x2160 |
| `/usr/share/boswas/presets/previews/<id>.png` | `previews/<id>.svg`, 480x270 |
| `/usr/share/boswas/presets/presets.json` | `presets.json` |
| `/usr/share/boswas/branding/login-background.png` | Horizon's lock screen art (copy) |
| `/usr/lib/boswas/python/boswas_preset/`, `/usr/bin/boswas-preset` | the tool |

The wallpaper directories keep the layout Control Center reads
(`metadata.json` with the name, `contents/images/` and
`contents/images_dark/`).

Everything installed takes about 27 MB (79 files; the second names are
plain copies, because Debian packages should not contain hard links).
