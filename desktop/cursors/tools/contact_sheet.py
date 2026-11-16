#!/usr/bin/env python3
"""Render a contact sheet of the Boswas cursor theme, for review.

Usage: contact_sheet.py OUTPUT.png [--theme DIR] [--sizes 32,64] [--zoom N]
                        [--hotspots] [--columns N] [--only NAME,NAME,...]

The sheet shows the pixels of the installed XCursor files, not the SVG
sources: without --theme the tool runs install.sh into a temporary directory
first (DIR is an installed theme, .../usr/share/icons/Boswas-Cursors).
Every shape is drawn at the given nominal sizes on a light and on a dark
background; animated shapes show three of their frames. --zoom N magnifies
every pixel N times, without smoothing, to judge the edges; --hotspots marks
the hotspot pixel in red.

The sheet is composed as SVG (OUTPUT.svg, kept next to the PNG) from PNG
tiles and rendered with rsvg-convert. Labels use <text>; that is fine here
because the sheet is a review aid, never shipped. Write previews outside the
repository: the tool refuses an OUTPUT inside it.

Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import base64
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CURSORS_DIR = HERE.parent
REPO = CURSORS_DIR.parent.parent
sys.path.insert(0, str(HERE))
import xcursor  # noqa: E402  (same directory)

LIGHT, DARK = (0xF3, 0xF3, 0xF5), (0x20, 0x1F, 0x25)
PAGE, LABEL = "#65646F", "#FFFFFF"
MARK = (0xFF, 0x20, 0x40)
PAD = 8
LABEL_HEIGHT = 24


def write_png(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return (xcursor.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def tile(image, background, zoom: int, hotspot: bool) -> bytes:
    """The image over `background`, every pixel `zoom` times as large, as a PNG."""
    rows = []
    for y in range(image.height):
        row = bytearray()
        for x in range(image.width):
            i = 4 * (y * image.width + x)
            blue, green, red, alpha = image.pixels[i:i + 4]
            if hotspot and (x, y) == (image.xhot, image.yhot):
                pixel = bytes(MARK)
            else:
                pixel = bytes(min(255, c + (b * (255 - alpha) + 127) // 255)
                              for c, b in zip((red, green, blue), background))
            row += pixel * zoom
        rows.extend([bytes(row)] * zoom)
    return write_png(image.width * zoom, image.height * zoom, b"".join(rows))


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("--theme", type=Path, default=None, help="an installed theme (default: run install.sh)")
    parser.add_argument("--sizes", default="32,64", help="nominal sizes to show (default: 32,64)")
    parser.add_argument("--zoom", type=int, default=1, help="magnify every pixel N times")
    parser.add_argument("--hotspots", action="store_true", help="mark the hotspot pixel")
    parser.add_argument("--columns", type=int, default=0, help="shapes per row (default: as many as fit 1500 px)")
    parser.add_argument("--only", default="", help="comma-separated shapes (default: all)")
    args = parser.parse_args(argv)

    output = args.output.resolve()
    if REPO in output.parents:
        parser.error("write previews outside the repository")
    sizes = [int(s) for s in args.sizes.split(",")]
    zoom = max(1, args.zoom)
    manifest = xcursor.load_manifest(CURSORS_DIR / "Boswas-Cursors" / "cursors.json")
    only = [name for name in args.only.split(",") if name]

    with tempfile.TemporaryDirectory() as tmp:
        theme = args.theme
        if theme is None:
            subprocess.run(["sh", str(CURSORS_DIR / "install.sh"), tmp], check=True, stderr=subprocess.DEVNULL)
            theme = Path(tmp) / "usr/share/icons/Boswas-Cursors"
        cells = []
        for cursor in manifest["cursors"]:
            if only and cursor["name"] not in only:
                continue
            images = xcursor.decode((theme / "cursors" / cursor["name"]).read_bytes())
            count = len(cursor["frames"])
            for frame in ([0] if count == 1 else [0, count // 3, 2 * count // 3]):
                label = cursor["name"] + (f" ({frame + 1}/{count})" if count > 1 else "")
                picked = []
                for size in sizes:
                    of_size = [image for image in images if image.size == size]
                    if len(of_size) != count:
                        raise SystemExit(f"contact_sheet: {cursor['name']} has no images of size {size}")
                    picked.append(of_size[frame])
                cells.append((label, picked))

    widths = [image.width * zoom for image in cells[0][1]]
    tallest = max(image.height * zoom for image in cells[0][1])
    cell_w = sum(widths) + PAD * (len(widths) + 1)
    cell_h = LABEL_HEIGHT + 2 * tallest + 3 * PAD
    columns = args.columns or max(1, min(len(cells), 1500 // cell_w))
    rows = (len(cells) + columns - 1) // columns
    width, height = columns * cell_w, rows * cell_h

    parts = []
    for index, (label, picked) in enumerate(cells):
        x0 = (index % columns) * cell_w
        y0 = (index // columns) * cell_h
        parts.append(f'<text x="{x0 + PAD}" y="{y0 + 15}" font-family="sans-serif" font-size="11" '
                     f'fill="{LABEL}">{label}</text>')
        for band, background in enumerate((LIGHT, DARK)):
            y = y0 + LABEL_HEIGHT + band * (tallest + PAD)
            colour = "#%02X%02X%02X" % background
            parts.append(f'<rect x="{x0 + PAD // 2}" y="{y - PAD // 2}" width="{cell_w - PAD}" '
                         f'height="{tallest + PAD}" fill="{colour}"/>')
            x = x0 + PAD
            for image in picked:
                data = base64.b64encode(tile(image, background, zoom, args.hotspots)).decode("ascii")
                parts.append(f'<image x="{x}" y="{y}" width="{image.width * zoom}" height="{image.height * zoom}" '
                             f'href="data:image/png;base64,{data}"/>')
                x += image.width * zoom + PAD

    sheet = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}">\n<rect width="{width}" height="{height}" fill="{PAGE}"/>\n'
             + "\n".join(parts) + "\n</svg>\n")
    svg_path = output.with_suffix(".svg")
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(sheet, encoding="utf-8", newline="\n")
    subprocess.run(["rsvg-convert", "-o", str(output), str(svg_path)], check=True)
    print(f"{len(cells)} cells, sizes {sizes}, zoom {zoom} -> {output} ({width}x{height})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
