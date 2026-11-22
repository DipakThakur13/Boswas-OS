#!/usr/bin/env python3
"""Render a contact sheet of a Boswas icon theme, for review.

Usage: contact_sheet.py OUTPUT.png [--theme DIR] [--light] [--aliases]
                        [--columns N] [--sizes 128,48,32]
       contact_sheet.py OUTPUT.png --prism [--contexts apps,places,...]
                        [--names NAME,...] [--light] [--aliases] [--columns N]
                        [--sizes 128,48,32]

Without --prism the sheet shows the legacy Boswas theme (build_icons.py).
With --prism it shows Boswas Prism (build_prism.py): every context, or the
ones named by --contexts, or only the icons named by --names (theme names,
aliases included, in the order given); --light takes the symbolic icons
from Boswas-Prism-Light and puts everything on a light background.

Each icon is shown at the given sizes (largest first) with its theme name.
The sheet is composed as SVG (OUTPUT.svg, kept next to the PNG) and rendered
with rsvg-convert. Labels use <text>; that is fine here because the sheet is
a review aid, never shipped. Write previews outside the repository: the tool
refuses an OUTPUT inside it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
sys.path.insert(0, str(HERE))
import build_icons  # noqa: E402  (same directory)

PAD = 16
LABEL = 22


def symbol(svg_text: str, sym_id: str) -> str:
    body = re.sub(r"<\?xml[^>]*\?>\s*", "", svg_text)
    body = re.sub(r"<svg\b[^>]*>", f'<symbol id="{sym_id}" viewBox="0 0 256 256">', body, count=1)
    return body.replace("</svg>", "</symbol>")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("--theme", type=Path, default=None,
                        help="theme directory (default: Boswas, or Boswas-Prism with --prism)")
    parser.add_argument("--prism", action="store_true", help="show Boswas Prism instead of the legacy theme")
    parser.add_argument("--contexts", default="", help="with --prism: comma-separated contexts (default: all)")
    parser.add_argument("--names", default="", help="with --prism: comma-separated icon names (default: all)")
    parser.add_argument("--light", action="store_true", help="light background")
    parser.add_argument("--aliases", action="store_true", help="also show alias files")
    parser.add_argument("--columns", type=int, default=6)
    parser.add_argument("--sizes", default="128,48,32")
    args = parser.parse_args(argv)

    output = args.output.resolve()
    if REPO in output.parents:
        parser.error("write previews outside the repository")
    sizes = [int(s) for s in args.sizes.split(",")]

    entries = []
    if args.prism:
        import build_prism  # same directory
        contexts = [c for c in args.contexts.split(",") if c] or list(build_prism.CONTEXTS)
        unknown = sorted(set(contexts) - set(build_prism.CONTEXTS))
        if unknown:
            parser.error(f"unknown context: {', '.join(unknown)}")
        dark = args.theme or build_prism.ICONS_DIR / build_prism.DARK
        light = dark.parent / build_prism.LIGHT
        names = [x for x in args.names.split(",") if x]
        found = {}
        for theme, context, file_name, design, _title, _glyph, _hue in build_prism.all_files():
            symbolic = context in build_prism.SYMBOLIC
            wanted = build_prism.LIGHT if symbolic and args.light else build_prism.DARK
            chosen = file_name in names if names else (args.aliases or file_name == design)
            if theme == wanted and context in contexts and chosen:
                root = light if theme == build_prism.LIGHT else dark
                found.setdefault(file_name, root / "scalable" / context / f"{file_name}.svg")
        missing = [x for x in names if x not in found]
        if missing:
            parser.error(f"no such icon: {', '.join(missing)}")
        entries = [found[x] for x in names] if names else list(found.values())
    else:
        theme_dir = args.theme or build_icons.DEFAULT_OUTPUT
        for kind, file_name, design, _title, _glyph in build_icons.all_files():
            if args.aliases or file_name == design:
                entries.append(theme_dir / "scalable" / kind / f"{file_name}.svg")

    big = sizes[0]
    cell_w = big + sum(s + 8 for s in sizes[1:]) + 2 * PAD
    cell_w = max(cell_w, 250)
    cell_h = big + LABEL + 2 * PAD
    cols = args.columns
    rows = (len(entries) + cols - 1) // cols
    width, height = cols * cell_w, rows * cell_h
    if args.prism:
        bg, fg = ("#F6F4FC", "#332E48") if args.light else ("#12101C", "#A7A2BF")
    else:
        bg, fg = ("#EFF1F5", "#232A36") if args.light else ("#111A2B", "#A8B3C7")

    defs, uses = [], []
    for i, path in enumerate(entries):
        sym = f"sheet-icon-{i}"
        defs.append(symbol(path.read_text(encoding="utf-8"), sym))
        x0 = (i % cols) * cell_w + PAD
        y0 = (i // cols) * cell_h + PAD
        x = x0
        for s in sizes:
            y = y0 + (big - s)
            uses.append(f'<use href="#{sym}" x="{x}" y="{y}" width="{s}" height="{s}"/>')
            x += s + 8
        uses.append(f'<text x="{x0}" y="{y0 + big + 17}" font-family="sans-serif" '
                    f'font-size="13" fill="{fg}">{path.stem}</text>')

    sheet = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}">\n<defs>\n' + "\n".join(defs) + "\n</defs>\n"
             f'<rect width="{width}" height="{height}" fill="{bg}"/>\n' + "\n".join(uses) + "\n</svg>\n")
    svg_path = output.with_suffix(".svg")
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(sheet, encoding="utf-8", newline="\n")
    subprocess.run(["rsvg-convert", "-o", str(output), str(svg_path)], check=True)
    print(f"{len(entries)} icons -> {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
