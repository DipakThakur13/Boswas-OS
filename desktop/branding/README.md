# Boswas brand assets

## Sources of truth

| File | What it is |
|------|------------|
| `source/boswas-group-logo.png` | Official Boswas Group logo (gear, crescent, "BOSWAS GROUP"), as supplied by Boswas Group |
| `source/boswas-group-wordmark.png` | Official "BOSWAS GROUP" wordmark in the Boswas Group typeface |

The Boswas Group typeface is not available to the build as a font file. To
use *the actual typeface* rather than a look-alike, the lettering is
**vectorised from the official artwork** with `tools/trace_brand.py`
(potrace). The traced SVGs are committed and must not be edited by hand:

| Traced asset | Content |
|--------------|---------|
| `boswas-group-logo.svg` | Full logo |
| `boswas-symbol.svg` | Gear and crescent only, for icons and small sizes |
| `wordmark-boswas-group.svg` | BOSWAS GROUP |
| `wordmark-boswas.svg` / `wordmark-group.svg` | Single words |
| `wordmark-os.svg` | "OS" (the O and S glyphs of BOSWAS) |
| `wordmark-boswas-os.svg` | BOSWAS OS, set with the original word spacing |

All traced assets are white on transparent. Composed artwork references them
with `<image href=... data-fill="#RRGGBB">`. `build/scripts/inline-svg.py`
inlines and recolours them, because Qt (KDE icons, the About page) cannot
follow external SVG references.

## Composed artwork

| File | Used for |
|------|----------|
| `boswas-mark.svg` | App/launcher icon (`boswas-logo` in the hicolor theme) |
| `boswas-about-logo.svg` | System Settings > About this System |
| `boot-splash.svg` | Live/installer ISO boot menu (GRUB, 800x600) |
| `../wallpapers/Boswas/*.svg` | Desktop wallpaper, login and lock screen |
| `../../installer/branding/installer-banner.svg` | Debian Installer banner |

## Typography

* **Brand lettering** (BOSWAS, BOSWAS OS, BOSWAS GROUP) always uses the
  traced Boswas Group glyphs.
* **Secondary text** (versions, "Installer", UI) uses Lato, the text face of
  the Boswas OS architecture documents. The traced glyph set has only
  B, O, S, W, A, G, R, U and P, so other words cannot be set in the brand
  face.
* **To extend:** if Boswas Group provides the original font file (TTF/OTF)
  with a licence that permits embedding/redistribution, add it under
  `desktop/fonts/` with its licence. It can then be used for any text,
  including the KDE UI and login screen.

## Re-tracing

```sh
sudo apt-get install potrace netpbm
python3 desktop/branding/tools/trace_brand.py
```

## Palette

| Token | Hex | Use |
|-------|-----|-----|
| Navy 950 | `#090F1B` | Deep background |
| Navy 900 | `#0B1220` | Headers, banner |
| Navy 800 | `#111A2B` | Window background |
| Teal 500 | `#17C6C0` | Accent, selection, focus |
| Teal 400 | `#2ED3CD` | Hover, highlights |
| Text | `#E8EEF6` | Primary text and lettering on dark |
| Muted | `#A8B3C7` | Secondary text |
