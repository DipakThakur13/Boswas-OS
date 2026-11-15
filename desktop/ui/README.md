# Boswas UI

The design system of the native Boswas desktop: one token file, and what is
generated from it.

| Path | What it is |
|------|------------|
| `tokens.json` | The single source of truth: the Prism palette, the dark and light themes, the icon hues, radii, spacing, type, elevation and motion |
| `tools/build_kde_colors.py` | Generates the KDE colour schemes from the tokens (Python 3 standard library) |
| `color-schemes/BoswasPrism.colors`, `color-schemes/BoswasPrismLight.colors` | The generated schemes (committed; do not edit by hand) |
| `tests/test_tokens.py` | Unit tests (standard library only) |

Boswas UI is the **interface** palette. The official mark keeps its own
gold and silver (`brand` in the token file, and
[`../branding/README.md`](../branding/README.md)); they are never used as
interface colours.

## tokens.json

`"name": "Boswas UI"`, `"version": 2`, `"palette": "Prism"`.

### The Prism palette (`color`)

Ten families. A reference is `family.shade`, for example `iris.500`.

| Family | Role | Shades |
|--------|------|--------|
| `ink` | Dark surfaces, text on light | 950 `#0B0A12`, 900 `#12101C`, 800 `#1A1728`, 700 `#252136`, 600 `#332E48`, 500 `#4A4563` |
| `cloud` | Light surfaces, text on dark | 50 `#F6F4FC`, 100 `#ECE9F7`, 200 `#D8D4E8`, 400 `#A7A2BF`, 600 `#5C5775` |
| `iris` | The accent: selection, focus, active elements | 300 `#B7AAFF`, 400 `#9B8AFF`, 500 `#7C5CFF`, 600 `#6443E6`, 700 `#4C31BD` |
| `azure` | Information, links | 300 `#8FC2FF`, 400 `#5CA3FF`, 500 `#2F7FF0`, 600 `#1E63D0`, 700 `#164CA6` |
| `mint` | Success | 300 `#7FE5BF`, 400 `#45D3A0`, 500 `#17A877`, 600 `#0E8A60`, 700 `#0A6B4B` |
| `sun` | Warning | 300 `#FFDA8C`, 400 `#FFC24F`, 500 `#FFA91F`, 600 `#E3860A`, 700 `#B86605`, 800 `#8A4A00` |
| `coral` | Danger, errors | 300 `#FFA1AD`, 400 `#FF7788`, 500 `#EE4B66`, 600 `#CC3353`, 700 `#A22441` |
| `sky` | Icon hue | 300 `#8FDDF7`, 400 `#57C8F2`, 500 `#1A9FD9`, 600 `#0E80B8`, 700 `#0A6491` |
| `orchid` | Icon hue, visited links | 300 `#F0A8E9`, 400 `#E27DD8`, 500 `#C44FBA`, 600 `#A2399A`, 700 `#7E2A78` |
| `slate` | Neutral icon hue | 300 `#9BA2B9`, 400 `#747B95`, 500 `#565C76`, 600 `#3E435B`, 700 `#2C3044` |

Plus `white` and `black`. Colours are upper-case `#RRGGBB`. The retired
teal accent of earlier releases is rejected by the tests.

### Themes (`semantic`)

Each theme maps roles to palette references:

| Role | Dark | Light |
|------|------|-------|
| `background` | `ink.950` | `cloud.50` |
| `surface` | `ink.900` | `white` |
| `surface-raised` | `ink.800` | `cloud.100` |
| `surface-hover` | `ink.700` | `cloud.200` |
| `border` | `ink.600` | `cloud.200` |
| `text` | `cloud.50` | `ink.900` |
| `text-secondary` | `cloud.200` | `ink.600` |
| `text-muted` | `cloud.400` | `cloud.600` |
| `accent` / `accent-hover` / `accent-pressed` | `iris.500` / `iris.400` / `iris.600` | `iris.600` / `iris.500` / `iris.700` |
| `on-accent` | `white` | `white` |
| `focus` | `iris.300` | `iris.500` |
| `success`, `warning`, `danger`, `info` | `mint.400`, `sun.400`, `coral.400`, `azure.400` | `mint.600`, `sun.800`, `coral.600`, `azure.600` |

### Icon hues (`icon.hues`)

The Boswas Prism icon pack gives each category one hue. A hue names the
tile gradient (`from`, `to`), the line colour (`glyph`) and the single
highlight (`accent`): `iris`, `azure`, `mint`, `sun`, `coral`, `sky`,
`orchid`, `slate` and `ink`. The tests keep every glyph at 3:1 or more
against both ends of its tile.

### Shape, type and motion

| Token | Value |
|-------|-------|
| `radius` | control 4, window 8, card 8, flyout 12, pill 999 (logical pixels) |
| `spacing.unit` | 4 (every distance is a multiple) |
| `type.family` | Inter, then Noto Sans, Lato, DejaVu Sans |
| `type.display-family` | Orbitron (the clock of the lock and login screens, the boot screen) |
| `type.scale` | 12, 14, 16, 20, 28, 40 |
| `elevation` | Four levels: desktop (none), window (blur 24, y 8, 35 %), flyout (blur 32, y 12, 45 %), dialog (blur 48, y 16, 55 %) |
| `motion` | fast 150 ms, normal 200 ms, slow 250 ms, curve ease-out-cubic; with reduced motion every duration is 0 |

The motion tokens are defined for the animations to come; the desktop does
not animate yet.

## Who reads the tokens

| Consumer | When | How |
|----------|------|-----|
| `packages/boswas-desktop/crates/boswas-ui` (the shell, the lock and login screens, the compositor's window frames) | Compile time | `include_str!` of `tokens.json`; parsed once at start-up |
| `packages/boswas-bootsplash` (the boot screen) | Build time | `build.rs` resolves the colours it needs and embeds them |
| `../icons/tools/build_prism.py` (the Boswas Prism icon pack) | When the icons are regenerated | Every colour of an icon is a token; the generator holds none of its own |
| `tools/build_kde_colors.py` (colours of KDE applications) | When the schemes are regenerated | See below |
| Other tools on a device | Run time | `boswas-branding` installs the file as `/usr/share/boswas/ui/tokens.json` |

A change to `tokens.json` therefore needs: the colour schemes and the
icons regenerated and committed, and the Rust packages rebuilt.

## The KDE colour schemes

KDE applications (Dolphin, Konsole, Okular, ...) take their colours from a
KDE colour scheme. Two schemes map the tokens onto KDE's colour roles, so
application windows match the shell and the window frames:

| Scheme | File | Installed as |
|--------|------|--------------|
| Boswas Prism (dark, the default) | `color-schemes/BoswasPrism.colors` | `/usr/share/color-schemes/BoswasPrism.colors` |
| Boswas Prism Light | `color-schemes/BoswasPrismLight.colors` | `/usr/share/color-schemes/BoswasPrismLight.colors` |

The selection is the same in both: white text on `iris.600`. Warnings are
`sun.400` on dark and `sun.800` on light, so they stay readable.

Konsole draws its terminal with a colour scheme of its own, chosen by a
profile. The same tool writes both from the tokens: `konsole/BoswasPrism.colorscheme`
and `konsole/BoswasPrismLight.colorscheme`, and the profiles
`konsole/Boswas Prism.profile` (the default, named in
`../defaults/kde-settings/konsolerc`) and `konsole/Boswas Prism Light.profile`,
installed to `/usr/share/konsole/`. The tests hold every terminal colour
to 4.5:1 against its background.

`boswas-branding` also builds the system-wide default `kdeglobals` for KDE
applications from `../defaults/kde-settings/kdeglobals.header` plus
`BoswasPrism.colors`, and installs it in `/usr/share/boswas/kde-settings`,
which `boswas-session` appends to `XDG_CONFIG_DIRS`. A user's own settings
and `/etc/xdg` take precedence; a preset replaces the colours for a user
([presets](../presets/README.md#the-native-boswas-desktop)).

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
  the type scale, motion between 150 and 250 ms, four elevation levels);
- icon glyphs stay readable on their tiles;
- the committed colour schemes are what the generator produces now;
- text meets WCAG AA (4.5:1) on every background of both schemes, window
  titles are readable, and the schemes have every group KDE reads.

`tests/unit/test_units.sh` runs them, and so does the build of
`boswas-branding`.
