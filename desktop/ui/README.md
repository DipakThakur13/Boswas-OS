# Boswas UI

The design system of the native Boswas desktop: one token file, and what is
generated from it or built on it.

| Path | What it is |
|------|------------|
| `tokens.json` | The single source of truth: the palette, the dark and light themes, strokes, fills and materials, the icon hues, radii, spacing, type, elevation, motion and the fixed sizes of the shell |
| `tools/build_kde_colors.py` | Generates the KDE colour schemes from the tokens, and holds the terminal palette of Prism (Python 3 standard library) |
| `color-schemes/BoswasPrism.colors`, `color-schemes/BoswasPrismLight.colors` | The generated KDE schemes (committed; do not edit by hand) |
| `tests/test_tokens.py` | Unit tests (standard library only) |
| `qt/` | `boswas_fluent`, the PySide6 toolkit that gives the Boswas applications this look ([qt/README.md](qt/README.md), ADR-0039) |

Boswas UI is the **interface** design. The official mark keeps its own
gold and silver (`brand` in the token file, and
[`../branding/README.md`](../branding/README.md)); they are never used as
interface colours.

## The design language

`"name": "Boswas UI"`, `"version": 3`, `"palette": "Prism"`, and a `design`
field that says what version 3 is: **Fluent**. The layout, geometry,
materials and motion follow the Fluent (WinUI) design language, with
Windows 11 as the target look. The colours, the icons and the typefaces
are Boswas's own (ADR-0033).

| Taken from Fluent | Boswas's own |
|-------------------|--------------|
| The layout of the shell: a centred taskbar, Start, quick settings, a notification centre | The colours: the Prism hue families, with iris as the accent |
| Geometry: 8 px windows and flyouts, 4 px controls, a 32 px title bar with 46 px caption buttons, a 48 px taskbar | The icons: the Boswas Prism pack ([`../icons`](../icons/README.md)) |
| Materials: mica, acrylic, chrome, smoke | The typeface: Inter (Debian's `fonts-inter`) |
| The type ramp and the motion curves | The mark, which is the Start button |

No font, icon, logo or wallpaper of Microsoft is in this repository.

## tokens.json

### The palette (`color`)

Eleven families. A reference is `family.shade`, for example `iris.500`.

| Family | Role | Shades |
|--------|------|--------|
| `neutral` | Surfaces, borders and text of both themes | 25 `#FAFAFC`, 50 `#F3F3F5`, 100 `#E9E8ED`, 200 `#D9D8DF`, 300 `#BDBCC6`, 400 `#9F9EAA`, 500 `#65646F`, 600 `#4A4953`, 700 `#393840`, 800 `#2C2B32`, 900 `#201F25`, 950 `#18171C` |
| `iris` | The accent: selection, focus, active elements | 300 `#B7AAFF`, 400 `#9B8AFF`, 500 `#7C5CFF`, 600 `#6443E6`, 700 `#4C31BD` |
| `azure` | Information, links | 300 `#8FC2FF`, 400 `#5CA3FF`, 500 `#2F7FF0`, 600 `#1E63D0`, 700 `#164CA6` |
| `mint` | Success | 300 `#7FE5BF`, 400 `#45D3A0`, 500 `#17A877`, 600 `#0E8A60`, 700 `#0A6B4B` |
| `sun` | Warning | 300 `#FFDA8C`, 400 `#FFC24F`, 500 `#FFA91F`, 600 `#E3860A`, 700 `#B86605`, 800 `#8A4A00` |
| `coral` | Danger, errors | 300 `#FFA1AD`, 400 `#FF7788`, 500 `#EE4B66`, 600 `#CC3353`, 700 `#A22441` |
| `sky` | Icon hue | 300 `#8FDDF7`, 400 `#57C8F2`, 500 `#1A9FD9`, 600 `#0E80B8`, 700 `#0A6491` |
| `orchid` | Icon hue, visited links | 300 `#F0A8E9`, 400 `#E27DD8`, 500 `#C44FBA`, 600 `#A2399A`, 700 `#7E2A78` |
| `slate` | Neutral icon hue | 300 `#9BA2B9`, 400 `#747B95`, 500 `#565C76`, 600 `#3E435B`, 700 `#2C3044` |
| `ink` | Text on the accent in the dark theme, an icon hue, terminal colours | 950 `#0B0A12`, 900 `#12101C`, 800 `#1A1728`, 700 `#252136`, 600 `#332E48`, 500 `#4A4563` |
| `cloud` | Terminal colours, one group of the light KDE scheme | 50 `#F6F4FC`, 100 `#ECE9F7`, 200 `#D8D4E8`, 400 `#A7A2BF`, 600 `#5C5775` |

Plus `white` and `black`. Colours are upper-case `#RRGGBB`. The retired
teal accent of earlier releases is rejected by the tests. In version 2
`ink` and `cloud` were the surfaces and the text; version 3 moved those
roles to `neutral`.

### Themes (`semantic`)

Each theme maps roles to palette references:

| Role | Dark | Light |
|------|------|-------|
| `background` | `neutral.900` | `neutral.50` |
| `surface` | `neutral.800` | `white` |
| `surface-raised` | `neutral.700` | `neutral.25` |
| `surface-hover` | `neutral.600` | `neutral.100` |
| `chrome` (the taskbar) | `neutral.950` | `neutral.100` |
| `border` | `neutral.600` | `neutral.200` |
| `text` | `white` | `neutral.950` |
| `text-secondary` | `neutral.200` | `neutral.600` |
| `text-muted` | `neutral.300` | `neutral.500` |
| `accent` / `accent-hover` / `accent-pressed` | `iris.400` / `iris.300` / `iris.500` | `iris.600` / `iris.500` / `iris.700` |
| `on-accent` | `ink.950` | `white` |
| `focus` | `white` | `neutral.950` |
| `success`, `warning`, `danger`, `info` | `mint.400`, `sun.400`, `coral.400`, `azure.400` | `mint.600`, `sun.800`, `coral.600`, `azure.600` |

The dark theme has a light accent with dark text on it; the light theme a
dark accent with white text.

### Strokes, fills and materials

| Token | What it is |
|-------|------------|
| `stroke` | One-pixel outlines, as an opacity of white (dark theme) or black (light theme) over whatever lies beneath: `control`, `surface`, `divider`, `window` |
| `fill` | The state layers of controls, in the same way: `control`, `control-hover`, `control-pressed`, `subtle-hover`, `subtle-pressed`, `disabled`. "Subtle" controls have no fill at rest |
| `material` | `mica`, `acrylic`, `chrome` and `smoke`: a `tint`, a `tint-opacity` and a `blur` radius each. Mica is for window chrome and long-lived surfaces, acrylic for flyouts and menus, chrome for the taskbar; smoke dims what lies behind a dialog. `glass`: Boswas glass, the material of the Qt applications, the shell and the compositor's own surfaces (below) |

Mica, acrylic and chrome are drawn from the desktop wallpaper: blurred by
`blur` logical pixels and covered with the tint (ADR-0036). Since
ADR-0063 the shell's and the compositor's surfaces are Boswas glass
instead; what is left of the others: the active window's title bar is
`mica` over the wallpaper, the lock screen's weather card `acrylic`, the
snap layouts' fallback without glass the `acrylic` tint, and `smoke`
still dims what lies behind a dialog.

`material.glass` is what the Qt toolkit ([qt/README.md](qt/README.md#glass-glass)),
the shell and the compositor's own surfaces (`boswas-ui`'s `glass.rs`)
draw with:

| Key | What it is |
|-----|------------|
| `tint`, `tint-opacity` | The window backdrop: the desktop background covered with this tint. Each scheme (`dark`, `light`) gives its own `tint-opacity` (0.62 and 0.54), a minimum that the toolkit raises where a picture would leave text under 4.5:1 |
| `blur`, `saturation`, `noise` | The backdrop's blur (80 logical pixels, a Gaussian of sigma blur/2 as the shell's materials), how much its colours are saturated (1.3) and the opacity of its grain (0.012), so that wide soft areas do not band |
| `pane-tint`, `card-tint`, `flyout-tint` | The tints of the glass that lies on the backdrop: navigation panes (`@background`), cards and rows (`@surface`), flyouts, menus and dialogs (`@surface`) |
| `taskbar-tint`, `taskbar` | The shell's taskbar (ADR-0063): `@chrome` at 0.72, one value for both schemes (the tests pin the keys of each scheme's table), raised for legibility like the backdrop. It is neither a window backdrop nor a flyout and keeps the chrome colour below Start |
| `dark` / `light`: `pane`, `card`, `flyout` | Their opacities (dark 0.34, 0.56, 0.84; light 0.4, 0.7, 0.86): the more a surface stands over content, the more opaque |
| `dark` / `light`: `highlight`, `highlight-hover`, `glow`, `shadow` | The white top hairline of glass (stronger under the pointer), the white inner glow below the top edge and the black of the lower hairline |

With `[appearance] transparency = false` or a high-contrast preset the
toolkit, the shell and the compositor draw every surface opaque. The
Rust programs compile the token file in (ADR-0063): the shell's panels
are the window backdrop, its menus, tooltips, notifications and dialogs
and the compositor's Alt+Tab, window menu and snap layouts the flyout
glass, the taskbar `taskbar-tint` at `taskbar`. Where the compositor
draws with OpenGL ES the backdrop is the live blur of what lies behind
(its brightness kept within a band of the scheme, dark 0 to 0.07 and
light 0.6 to 1 in relative luminance, so that a tint can stay
translucent); with pixman it is the blurred wallpaper, as in the
toolkit.

### Icon hues (`icon.hues`)

The Boswas Prism icon pack gives each category one hue. A hue names the
tile gradient (`from`, `to`), the line colour (`glyph`) and the single
highlight (`accent`): `iris`, `azure`, `mint`, `sun`, `coral`, `sky`,
`orchid`, `slate` and `ink`. The tests keep every glyph at 3:1 or more
against both ends of its tile.

### Shape, type and motion

| Token | Value |
|-------|-------|
| `radius` | control 4, card 4, tooltip 4, window 8, flyout 8, pill 999 (logical pixels) |
| `spacing.unit` | 4 (every distance is a multiple) |
| `type.family` | Inter, then Noto Sans, Lato, DejaVu Sans |
| `type.display-family` | Orbitron. Kept in the file; no Boswas surface draws text in it any more (the wordmarks are outlines, see [`../branding`](../branding/README.md)) |
| `type.scale` | 12, 14, 16, 20, 28, 40 |
| `type.ramp` | Size, line height and weight of each step: caption 12/16/400, body 14/20/400, body-strong 14/20/600, body-large 16/22/400, subtitle 20/28/600, title 28/36/600, title-large 40/52/600 |
| `elevation` | Five levels as blur, y offset and opacity: desktop (none), tooltip (8, 4, 22 %), flyout (16, 8, 30 %), window (32, 12, 34 %), dialog (48, 20, 48 %) |
| `motion` | fast 150 ms, normal 200 ms, slow 250 ms. `curves`: `decelerate` (0.1, 0.9, 0.2, 1.0), `accelerate` (0.7, 0.0, 1.0, 0.5) and `standard` (0.8, 0.0, 0.2, 1.0), the control points of a cubic Bézier curve. With reduced motion every duration is 0 |

The desktop animates with these: windows open, close, minimise and come
back, desktops slide, flyouts open and close. `[appearance]
reduced_motion` in the compositor's configuration turns it off for the
compositor, the shell and the Qt toolkit.

### The shell's fixed sizes (`shell`)

Logical pixels, shared by the shell and the compositor's window frames:

| Token | Value |
|-------|-------|
| `taskbar-height`, `taskbar-button`, `taskbar-icon` | 48, 40, 24 |
| `taskbar-indicator`, `taskbar-indicator-active` | 6 by 3, 16 by 3 |
| `start-columns`, `start-columns-narrow`, `start-wide-output` | 8, 6, 1600 by 900: eight columns of pins on outputs at least that large (logical pixels), six on smaller ones |
| `start-cell`, `start-padding` | 96 by 88, 32: a pin's cell and the panel's side padding (Start is 832 wide with eight columns) |
| `start-height-fraction`, `start-max-height` | 0.85, 1200: Start's height as a share of the output's, and its limit |
| `start-rows`, `start-recommended-rows` | 2, 2: rows of pins and of recommendations in the default layout |
| `start-icon`, `start-list-icon` | 36, 24: pins and the grid; the list |
| `flyout-margin`, `quick-settings-width`, `notification-width`, `toast-width` | 12, 360, 364, 364 |
| `titlebar-height`, `caption-button`, `window-border`, `resize-margin` | 32, 46 by 32, 1, 8 |
| `caption-close-hover`, `caption-close-pressed` | `#C42B1C`, `#B02A1F`: the red of the close button, the only interface colours in the file that are not palette references |
| `tooltip-delay-ms` | 500 |

## Who reads the tokens

| Consumer | When | How |
|----------|------|-----|
| `packages/boswas-desktop/crates/boswas-ui` (the shell, the lock and login screens, the compositor's window frames) | Compile time | `include_str!` of `tokens.json`; parsed once at start-up |
| `packages/boswas-bootsplash` (the boot screen) | Build time | `build.rs` resolves the colours, the type ramp and the control radius it needs and embeds them |
| `qt/boswas_fluent` (the Qt toolkit of the Boswas applications) | Run time | Reads `/usr/share/boswas/ui/tokens.json`, or this file from a checkout; `BOSWAS_UI_TOKENS` names another file for tests |
| `../icons/tools/build_prism.py` (the Boswas Prism icon pack) | When the icons are regenerated | Every colour of an icon is a token; the generator holds none of its own |
| `tools/build_kde_colors.py` (colours of KDE applications) | When the schemes are regenerated | See below |
| Other tools on a device | Run time | `boswas-branding` installs the file as `/usr/share/boswas/ui/tokens.json` |

A change to `tokens.json` therefore needs: the colour schemes and the
icons regenerated and committed, and the Rust packages rebuilt. The Qt
applications pick it up with the next `boswas-branding`.

## The KDE colour schemes

KDE applications take their colours from a KDE colour scheme. They are no
longer in the image (ADR-0040) but run when a user installs one. Two
schemes map the tokens onto KDE's colour roles, so their windows match the
shell and the window frames:

| Scheme | File | Installed as |
|--------|------|--------------|
| Boswas Prism (dark, the default) | `color-schemes/BoswasPrism.colors` | `/usr/share/color-schemes/BoswasPrism.colors` |
| Boswas Prism Light | `color-schemes/BoswasPrismLight.colors` | `/usr/share/color-schemes/BoswasPrismLight.colors` |

The selection is the same in both: white text on `iris.600`. Warnings are
`sun.400` on dark and `sun.800` on light, so they stay readable. The
Boswas applications do not use these schemes: they take the tokens
through the Qt toolkit.

The same tool holds the terminal palette of Prism: `TERMINAL`, the token
references of the sixteen terminal colours for dark and for light, and
`terminal_colours()`, which resolves them. Boswas Terminal draws with
these colours (it keeps the same table in
`packages/boswas-apps/boswas_apps/terminal/schemes.py`, and its tests
compare the two). The tests here hold every terminal colour to 4.5:1
against its background. Nothing is generated from the table: the Konsole
colour schemes and profiles that used to be written from it are gone
(ADR-0042).

`boswas-branding` also builds the system-wide default `kdeglobals` for KDE
applications from `../defaults/kde-settings/kdeglobals.header` plus
`BoswasPrism.colors`, and installs it in `/usr/share/boswas/kde-settings`,
which `boswas-session` appends to `XDG_CONFIG_DIRS`. A user's own settings
and `/etc/xdg` take precedence; a preset replaces the colours for a user
([presets](../presets/README.md#applying-a-preset)).

Regenerate after changing `tokens.json`:

```sh
python3 desktop/ui/tools/build_kde_colors.py           # rewrite the two schemes
python3 desktop/ui/tools/build_kde_colors.py --check   # exit 1 if they are stale
```

The output is deterministic.

## Tests

```sh
cd desktop/ui && python3 -B -m unittest discover -s tests -v
```

They check that:

- every palette colour is upper-case hex and none is the retired teal;
- every reference in the themes and icon hues resolves;
- the specification values hold (window radius 8, control radius 4, grid 4,
  the type scale, motion between 150 and 250 ms, the desktop, window,
  flyout and dialog elevation levels);
- the Fluent geometry holds: flyouts share the window radius and cards the
  control radius, the taskbar is 48 high, the title bar 32 and a caption
  button 46 by 32, every stroke and fill opacity lies between 0 and 0.25,
  and every material's tint resolves;
- glass holds: its tints resolve, both schemes give every level as an
  opacity, blur, saturation and grain stay in range, and the pane, the card
  and the flyout grow more opaque in that order;
- icon glyphs stay readable on their tiles;
- the committed colour schemes are what the generator produces now, and
  it writes nothing else;
- text meets WCAG AA (4.5:1) on every background of both schemes, window
  titles are readable, the terminal colours are readable, and the schemes
  have every group KDE reads.

The toolkit has its own tests in `qt/tests`, among them contrast on every
surface and state layer ([qt/README.md](qt/README.md#contrast)).

`tests/unit/test_units.sh` runs them, and so does the build of
`boswas-branding`.
