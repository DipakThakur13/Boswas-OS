#!/usr/bin/env python3
"""Vectorise the official Boswas Group artwork into the SVG brand assets.

The Boswas Group typeface is only available as artwork (no font file), so the
brand lettering is traced from the official images instead of approximated
with a look-alike font:

  source/boswas-group-logo.png      gear logo with "BOSWAS GROUP" (white on dark)
  source/boswas-group-wordmark.png  "BOSWAS GROUP" wordmark (black on white)

Outputs (written to desktop/branding/, committed to the repository):

  boswas-group-logo.svg   full logo: gear, crescent and lettering
  boswas-symbol.svg       gear + crescent only (icons, small sizes)
  wordmark-boswas.svg     "BOSWAS"
  wordmark-group.svg      "GROUP"
  wordmark-os.svg         "OS"  (the O and S glyphs of "BOSWAS")

All outputs are white (#FFFFFF) on a transparent background; build scripts
recolour copies with sed where needed.

Requires: python3, netpbm (pngtopnm, pamscale), potrace.
Run from the repository root:  python3 desktop/branding/tools/trace_brand.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path

BRANDING = Path(__file__).resolve().parents[1]
SOURCE = BRANDING / "source"
UPSCALE = 4          # trace at 4x for smooth curves
MARGIN = 6           # source pixels of padding around each crop
THRESHOLD = 128      # ink is anything darker than this (after normalisation)


class Gray:
    """8-bit grayscale raster where 0 = ink, 255 = paper."""

    def __init__(self, width: int, height: int, data: bytearray):
        self.w, self.h, self.data = width, height, data

    @classmethod
    def from_png(cls, png: Path, invert: bool) -> "Gray":
        ppm = subprocess.run(["pngtopnm", "-mix", "-background=white", str(png)],
                             check=True, capture_output=True).stdout
        fields, pos = [], 0
        while len(fields) < 4:  # magic, width, height, maxval
            while ppm[pos:pos + 1].isspace():
                pos += 1
            start = pos
            while not ppm[pos:pos + 1].isspace():
                pos += 1
            fields.append(ppm[start:pos])
        pos += 1
        magic, w, h = fields[0], int(fields[1]), int(fields[2])
        pixels = ppm[pos:]
        data = bytearray(w * h)
        step = 3 if magic == b"P6" else 1
        for i in range(w * h):
            if step == 3:
                r, g, b = pixels[3 * i], pixels[3 * i + 1], pixels[3 * i + 2]
                v = (299 * r + 587 * g + 114 * b) // 1000
            else:
                v = pixels[i]
            data[i] = 255 - v if invert else v
        return cls(w, h, data)

    def ink(self, x: int, y: int) -> bool:
        return self.data[y * self.w + x] < THRESHOLD

    def crop(self, x0: int, y0: int, x1: int, y1: int, keep=None) -> "Gray":
        """Crop [x0,x1) x [y0,y1); pixels outside `keep` (a set of indices) become paper."""
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.w, x1), min(self.h, y1)
        out = bytearray()
        for y in range(y0, y1):
            for x in range(x0, x1):
                idx = y * self.w + x
                out.append(self.data[idx] if keep is None or idx in keep else 255)
        return Gray(x1 - x0, y1 - y0, out)

    def hconcat(self, gap: int, other: "Gray") -> "Gray":
        """Place `other` to the right of self with `gap` columns of paper between."""
        if self.h != other.h:
            raise ValueError("heights differ")
        out = bytearray()
        for y in range(self.h):
            out += self.data[y * self.w:(y + 1) * self.w]
            out += bytes([255]) * gap
            out += other.data[y * other.w:(y + 1) * other.w]
        return Gray(self.w + gap + other.w, self.h, out)

    def write_pgm(self, path: Path) -> None:
        path.write_bytes(b"P5\n%d %d\n255\n" % (self.w, self.h) + bytes(self.data))


def components(img: Gray) -> list[dict]:
    """8-connected components of ink pixels with bounding boxes."""
    seen = bytearray(img.w * img.h)
    found = []
    for start in range(img.w * img.h):
        if seen[start] or img.data[start] >= THRESHOLD:
            continue
        queue, pixels = deque([start]), []
        seen[start] = 1
        while queue:
            idx = queue.popleft()
            pixels.append(idx)
            y, x = divmod(idx, img.w)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < img.w and 0 <= ny < img.h:
                        n = ny * img.w + nx
                        if not seen[n] and img.data[n] < THRESHOLD:
                            seen[n] = 1
                            queue.append(n)
        xs = [i % img.w for i in pixels]
        ys = [i // img.w for i in pixels]
        found.append({"pixels": pixels, "box": (min(xs), min(ys), max(xs) + 1, max(ys) + 1)})
    return found


def grow(pixels, img: Gray, radius: int = 2) -> set[int]:
    """Pixel set plus a small halo so anti-aliased edges survive masking."""
    out = set()
    for idx in pixels:
        y, x = divmod(idx, img.w)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                nx, ny = x + dx, y + dy
                if 0 <= nx < img.w and 0 <= ny < img.h:
                    out.add(ny * img.w + nx)
    return out


def union_box(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def trace(img: Gray, out: Path, title: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src, big = Path(tmp) / "in.pgm", Path(tmp) / "big.pgm"
        img.write_pgm(src)
        with big.open("wb") as fh:
            subprocess.run(["pamscale", str(UPSCALE), str(src)], check=True, stdout=fh)
        svg = subprocess.run(
            ["potrace", "--svg", "--flat", "--color", "#FFFFFF", "--turdsize", "8",
             "--alphamax", "1.0", "--opttolerance", "0.2", "--resolution", str(72 * UPSCALE),
             "-o", "-", str(big)],
            check=True, capture_output=True, text=True).stdout
    # Replace potrace's generator comment/metadata with provenance + a title.
    body = svg[svg.index("<svg"):]
    body = body.replace("<metadata>\nCreated by potrace 1.16, written by Peter Selinger 2001-2019\n</metadata>\n", "")
    header = ('<?xml version="1.0" standalone="no"?>\n'
              f"<!-- {title}. Traced from desktop/branding/source/ by tools/trace_brand.py "
              "(potrace). Do not edit by hand; re-run the tool. -->\n")
    out.write_text(header + body)
    print(f"wrote {out.relative_to(BRANDING.parents[1])}")


def glyph_runs(img: Gray, box) -> list[tuple[int, int]]:
    """Horizontal [x0, x1) ranges of glyphs inside `box`, split on blank columns."""
    x0, y0, x1, y1 = box
    runs, start = [], None
    for x in range(x0, x1 + 1):
        has_ink = x < x1 and any(img.ink(x, y) for y in range(y0, y1))
        if has_ink and start is None:
            start = x
        elif not has_ink and start is not None:
            runs.append((start, x))
            start = None
    return runs


def main() -> int:
    # --- Wordmark: BOSWAS GROUP, black on white --------------------------------
    word = Gray.from_png(SOURCE / "boswas-group-wordmark.png", invert=False)
    comps = [c for c in components(word) if len(c["pixels"]) > 20]
    box = union_box([c["box"] for c in comps])
    glyphs = glyph_runs(word, box)
    if len(glyphs) != 11:
        sys.exit(f"expected 11 glyphs in BOSWAS GROUP, found {len(glyphs)}")
    y0, y1 = box[1] - MARGIN, box[3] + MARGIN
    crop = lambda a, b, m=MARGIN: word.crop(glyphs[a][0] - m, y0, glyphs[b][1] + m, y1)  # noqa: E731
    trace(crop(0, 10), BRANDING / "wordmark-boswas-group.svg", "Boswas wordmark: BOSWAS GROUP")
    trace(crop(0, 5), BRANDING / "wordmark-boswas.svg", "Boswas wordmark: BOSWAS")
    trace(crop(6, 10), BRANDING / "wordmark-group.svg", "Boswas wordmark: GROUP")
    trace(crop(1, 2), BRANDING / "wordmark-os.svg", "Boswas wordmark: OS")
    # "BOSWAS OS": the O and S glyphs set after BOSWAS with the original
    # word space measured between BOSWAS and GROUP.
    word_space = glyphs[6][0] - glyphs[5][1]
    boswas_os = crop(0, 5).hconcat(word_space - 2 * MARGIN, crop(1, 2))
    trace(boswas_os, BRANDING / "wordmark-boswas-os.svg", "Boswas wordmark: BOSWAS OS")

    # --- Logo: white artwork on dark, inverted so the artwork is ink -----------
    logo = Gray.from_png(SOURCE / "boswas-group-logo.png", invert=True)
    comps = sorted((c for c in components(logo) if len(c["pixels"]) > 20),
                   key=lambda c: (c["box"][2] - c["box"][0]) * (c["box"][3] - c["box"][1]),
                   reverse=True)
    full_box = union_box([c["box"] for c in comps])
    trace(logo.crop(full_box[0] - MARGIN, full_box[1] - MARGIN, full_box[2] + MARGIN, full_box[3] + MARGIN),
          BRANDING / "boswas-group-logo.svg", "Boswas Group logo")
    # The gear and the crescent are the two largest shapes; the rest is lettering.
    symbol = comps[:2]
    keep = grow([p for c in symbol for p in c["pixels"]], logo)
    sbox = union_box([c["box"] for c in symbol])
    trace(logo.crop(sbox[0] - MARGIN, sbox[1] - MARGIN, sbox[2] + MARGIN, sbox[3] + MARGIN, keep=keep),
          BRANDING / "boswas-symbol.svg", "Boswas symbol (gear and crescent)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
