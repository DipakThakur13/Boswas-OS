#!/usr/bin/env python3
"""Set the Boswas OS wordmarks in Inter SemiBold, as outlines.

Inter is the typeface of the Boswas interface (type.family in
desktop/ui/tokens.json). The wordmarks are converted to SVG paths, so
artwork renders identically everywhere without the font (Qt cannot use
<text> with a font it does not have, and GRUB and the Debian Installer get
pre-rendered images anyway).

  Inter-SemiBold.otf from the Debian package fonts-inter (SIL OFL 1.1),
  read from /usr/share/fonts/opentype/inter (or --font PATH)

The letters are spaced with the font's own kerning (its GPOS "kern"
feature) and a display tracking of TRACKING em, and drawn at 1000 units to
the em.

Outputs (written to desktop/branding/, committed to the repository):

  wordmark-boswas-os.svg   Boswas OS
  wordmark-boswas.svg      Boswas
  wordmark-os.svg          OS

All outputs are white (#FFFFFF) on a transparent background, cropped to the
ink (from the cap height to the baseline, with the overshoot of round
letters), and are recoloured with data-fill (see build/scripts/inline-svg.py).

Requires: python3, python3-fonttools, fonts-inter.
Run from anywhere:

  python3 desktop/branding/tools/build_wordmarks.py            # rewrite in place
  python3 desktop/branding/tools/build_wordmarks.py --check    # fail if stale
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

BRANDING = Path(__file__).resolve().parents[1]
FONT_DIRS = (Path("/usr/share/fonts/opentype/inter"), Path("/usr/share/fonts/truetype/inter"))
FONT_FILES = ("Inter-SemiBold.otf", "Inter-SemiBold.ttf")
EM = 1000.0            # output units per em
TRACKING = -0.01       # letter spacing, in em (display sizes set Inter a little tighter)
WORDMARKS = {"wordmark-boswas-os.svg": "Boswas OS", "wordmark-boswas.svg": "Boswas", "wordmark-os.svg": "OS"}


def find_font(argv: list[str]) -> Path:
    if "--font" in argv:
        return Path(argv[argv.index("--font") + 1])
    for directory in FONT_DIRS:
        for name in FONT_FILES:
            if (directory / name).is_file():
                return directory / name
    raise SystemExit("Inter SemiBold not found: install fonts-inter, or pass --font PATH")


def kerning(font: TTFont, left: str, right: str) -> float:
    """The pair adjustment (font units) of the GPOS kern feature for two glyphs."""
    if "GPOS" not in font:
        return 0.0
    gpos = font["GPOS"].table
    indices = sorted({index for record in gpos.FeatureList.FeatureRecord if record.FeatureTag == "kern"
                      for index in record.Feature.LookupListIndex})
    total = 0.0
    for index in indices:
        lookup = gpos.LookupList.Lookup[index]
        for sub in lookup.SubTable:
            kind = lookup.LookupType
            if kind == 9:                                  # extension
                kind, sub = sub.ExtensionLookupType, sub.ExtSubTable
            if kind != 2 or left not in sub.Coverage.glyphs:
                continue
            value = None
            if sub.Format == 1:
                pairs = sub.PairSet[sub.Coverage.glyphs.index(left)].PairValueRecord
                value = next((p.Value1 for p in pairs if p.SecondGlyph == right), None)
                if value is None:
                    continue                               # not this subtable: try the next
            elif sub.Format == 2:
                class1 = sub.ClassDef1.classDefs.get(left, 0) if sub.ClassDef1 else 0
                class2 = sub.ClassDef2.classDefs.get(right, 0) if sub.ClassDef2 else 0
                value = sub.Class1Record[class1].Class2Record[class2].Value1
            total += getattr(value, "XAdvance", 0) or 0
            break                                          # the first subtable that applies wins
    return total


def outline(font: TTFont, text: str) -> tuple[str, tuple[float, float, float, float]]:
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    scale = EM / font["head"].unitsPerEm
    pen = SVGPathPen(glyphs, ntos=lambda v: f"{v:.1f}".rstrip("0").rstrip(".") or "0")
    bounds = BoundsPen(glyphs)
    names = [cmap[ord(char)] for char in text]
    x = 0.0
    for index, (char, name) in enumerate(zip(text, names)):
        if char != " ":
            # Font units are y-up; SVG is y-down.
            transform = (scale, 0, 0, -scale, x, 0)
            glyphs[name].draw(TransformPen(pen, transform))
            glyphs[name].draw(TransformPen(bounds, transform))
        if index < len(text) - 1:
            x += (glyphs[name].width + kerning(font, name, names[index + 1])) * scale + TRACKING * EM
    return pen.getCommands(), bounds.bounds


def wordmarks(font: TTFont) -> dict[str, str]:
    family = font["name"].getDebugName(16) or font["name"].getDebugName(1).split()[0]
    style = font["name"].getDebugName(17) or font["name"].getDebugName(2)
    version = font["head"].fontRevision
    out = {}
    for file_name, text in WORDMARKS.items():
        d, (x0, y0, x1, y1) = outline(font, text)
        w, h = x1 - x0, y1 - y0
        out[file_name] = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
            f'viewBox="{x0:.1f} {y0:.1f} {w:.1f} {h:.1f}">\n'
            f'  <title>{text} ({family} {style} {version:.3f}, kerned, tracking {TRACKING} em)</title>\n'
            f'  <!-- Generated by desktop/branding/tools/build_wordmarks.py; do not edit by hand. -->\n'
            f'  <path fill="#FFFFFF" d="{d}"/>\n'
            f'</svg>\n')
    return out


def main(argv: list[str]) -> int:
    font = TTFont(find_font(argv))
    stale = []
    for file_name, svg in wordmarks(font).items():
        target = BRANDING / file_name
        if "--check" in argv:
            if not target.is_file() or target.read_text(encoding="utf-8") != svg:
                stale.append(file_name)
            continue
        target.write_text(svg, encoding="utf-8", newline="\n")
        size = re.search(r'width="([0-9]+)" height="([0-9]+)"', svg).groups()
        print(f"{file_name}: {WORDMARKS[file_name]!r} {size[0]} x {size[1]} units (1000 to the em)")
    if stale:
        print("stale (re-run desktop/branding/tools/build_wordmarks.py): " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
