#!/usr/bin/env python3
"""Vectorise the Boswas OS logo (source/boswas-os-logo.png) into SVG.

The logo is a "b" monogram: a gold stem and dot, a silver ring and swoosh, on
navy. It is supplied as a raster image only, so it is traced instead of
redrawn by hand:

  1. Every pixel's ink coverage is its brightness above the navy background,
     relative to the brightest pixel nearby, so the anti-aliased edges of
     light and dark parts of the gradients land at the same 50 % contour.
  2. Connected shapes are classed as gold or silver by their colour (gold is
     much redder than blue; silver is slightly bluer).
  3. Each class is upscaled 4x and traced with potrace.
  4. The gold and the silver shading are each fitted with a linear gradient
     (least squares over the fully covered pixels).

Outputs (written to desktop/branding/, committed to the repository):

  boswas-os-mark.svg        the mark in full colour, transparent background
  boswas-os-mark-mono.svg   the mark in one colour (white), for recolouring
                            with data-fill (see build/scripts/inline-svg.py)
  boswas-os-mark-flat.svg   the mark in flat gold and silver (small sizes,
                            and wherever gradient references are not allowed)

and control-plane/dashboard/favicon.svg: the flat mark on a navy tile, with
no references of any kind (the dashboard's tests forbid href and url()).

Requires: python3, netpbm (pngtopnm, pamscale), potrace.
Run from the repository root:  python3 desktop/branding/tools/trace_mark.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path

BRANDING = Path(__file__).resolve().parents[1]
SOURCE = BRANDING / "source" / "boswas-os-logo.png"
FAVICON = BRANDING.parents[1] / "control-plane" / "dashboard" / "favicon.svg"
FLAT = {"gold": "#E3BF7E", "silver": "#AEB7C7"}      # brand Gold 500 / Silver 400
UPSCALE = 4          # trace at 4x for sub-pixel edge placement
WINDOW = 5           # pixels: neighbourhood for the local "full ink" brightness
MARGIN = 12          # source pixels of padding around the mark in the viewBox
SOLID = 0.97         # coverage above which a pixel counts for gradient fitting


def read_ppm(png: Path) -> tuple[int, int, bytes]:
    ppm = subprocess.run(["pngtopnm", str(png)], check=True, capture_output=True).stdout
    fields, pos = [], 0
    while len(fields) < 4:                       # magic, width, height, maxval
        while ppm[pos:pos + 1].isspace():
            pos += 1
        start = pos
        while not ppm[pos:pos + 1].isspace():
            pos += 1
        fields.append(ppm[start:pos])
    pos += 1
    if fields[0] != b"P6" or fields[3] != b"255":
        raise SystemExit(f"{png}: expected an 8-bit RGB image")
    w, h = int(fields[1]), int(fields[2])
    return w, h, ppm[pos:pos + 3 * w * h]


def max_filter(values: list[float], w: int, h: int, r: int) -> list[float]:
    """Separable maximum over a (2r+1)^2 window."""
    rows = [0.0] * (w * h)
    for y in range(h):
        line = values[y * w:(y + 1) * w]
        for x in range(w):
            rows[y * w + x] = max(line[max(0, x - r):x + r + 1])
    out = [0.0] * (w * h)
    for x in range(w):
        col = rows[x::w]
        for y in range(h):
            out[y * w + x] = max(col[max(0, y - r):y + r + 1])
    return out


def label(mask: list[bool], w: int, h: int) -> tuple[list[int], int]:
    labels, count = [0] * (w * h), 0
    for start in range(w * h):
        if not mask[start] or labels[start]:
            continue
        count += 1
        labels[start] = count
        queue = deque([start])
        while queue:
            i = queue.popleft()
            x, y = i % w, i // w
            for j in (i - 1 if x else -1, i + 1 if x < w - 1 else -1, i - w if y else -1, i + w if y < h - 1 else -1):
                if j >= 0 and mask[j] and not labels[j]:
                    labels[j] = count
                    queue.append(j)
    return labels, count


def fit_gradient(points: list[tuple[int, int, tuple[int, int, int]]], box) -> tuple:
    """Least-squares plane per channel; returns (x1, y1, colour1, x2, y2, colour2) along its slope."""
    n = len(points)
    sx = sum(p[0] for p in points) / n
    sy = sum(p[1] for p in points) / n
    sxx = sum((p[0] - sx) ** 2 for p in points)
    syy = sum((p[1] - sy) ** 2 for p in points)
    sxy = sum((p[0] - sx) * (p[1] - sy) for p in points)
    det = sxx * syy - sxy * sxy
    planes = []
    for c in range(3):
        mean = sum(p[2][c] for p in points) / n
        sxc = sum((p[0] - sx) * (p[2][c] - mean) for p in points)
        syc = sum((p[1] - sy) * (p[2][c] - mean) for p in points)
        planes.append((mean, (sxc * syy - syc * sxy) / det, (syc * sxx - sxc * sxy) / det))
    # Direction of fastest change of brightness.
    gx = sum(p[1] for p in planes)
    gy = sum(p[2] for p in planes)
    norm = (gx * gx + gy * gy) ** 0.5 or 1.0
    ux, uy = gx / norm, gy / norm
    x0, y0, x1, y1 = box
    ts = [(cx - sx) * ux + (cy - sy) * uy for cx in (x0, x1) for cy in (y0, y1)]
    t1, t2 = min(ts), max(ts)

    def colour(t):
        x, y = sx + ux * t, sy + uy * t
        return tuple(max(0, min(255, round(m + a * (x - sx) + b * (y - sy)))) for m, a, b in planes)
    return (sx + ux * t1, sy + uy * t1, colour(t1), sx + ux * t2, sy + uy * t2, colour(t2))


def trace(coverage: list[float], w: int, h: int, tmp: Path, name: str) -> tuple[str, str]:
    """Trace a coverage map (1 = ink); returns (transform, path data) in 4x pixel units."""
    pgm = tmp / f"{name}.pgm"
    with open(pgm, "wb") as fh:
        fh.write(f"P5\n{w} {h}\n255\n".encode())
        fh.write(bytes(255 - round(255 * min(1.0, max(0.0, v))) for v in coverage))
    big = tmp / f"{name}-4x.pgm"
    with open(big, "wb") as fh:
        subprocess.run(["pamscale", "-filter=triangle", str(UPSCALE), str(pgm)], check=True, stdout=fh)
    svg = subprocess.run(["potrace", "--svg", "--flat", "-k", "0.5", "-t", "40", "-a", "1.0", "-O", "0.4",
                          "-o", "-", str(big)], check=True, capture_output=True, text=True).stdout
    transform = re.search(r'<g transform="([^"]+)"', svg).group(1)
    path = re.search(r'<path d="([^"]+)"', svg, re.S).group(1)
    return transform, " ".join(path.split())


def hexc(c) -> str:
    return "#{:02X}{:02X}{:02X}".format(*c)


def main() -> int:
    w, h, rgb = read_ppm(SOURCE)
    bg = tuple(rgb[i] for i in range(3))                       # top-left pixel: the navy background
    lum = [0.0] * (w * h)
    for i in range(w * h):
        r, g, b = rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]
        lum[i] = max(0.0, 0.299 * (r - bg[0]) + 0.587 * (g - bg[1]) + 0.114 * (b - bg[2]))
    local = max_filter(lum, w, h, WINDOW)
    coverage = [lum[i] / local[i] if local[i] > 24 else 0.0 for i in range(w * h)]

    labels, count = label([c >= 0.5 for c in coverage], w, h)
    redness, sizes = [0.0] * (count + 1), [0] * (count + 1)
    for i in range(w * h):
        if labels[i] and coverage[i] >= SOLID:
            redness[labels[i]] += rgb[3 * i] - rgb[3 * i + 2]
            sizes[labels[i]] += 1
    shapes = {n: ("gold" if redness[n] / sizes[n] > 30 else "silver") for n in range(1, count + 1)
              if sizes[n] > 500}                              # ignore specks
    if sorted(shapes.values()) != ["gold", "gold", "silver", "silver"]:
        raise SystemExit(f"expected 2 gold and 2 silver shapes, found {shapes}")

    # Edge pixels below 50 % belong to the shape next to them.
    owner = list(labels)
    for i in range(w * h):
        if coverage[i] > 0 and not labels[i]:
            x, y = i % w, i // w
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    j = (y + dy) * w + (x + dx)
                    if 0 <= x + dx < w and 0 <= y + dy < h and labels[j] in shapes:
                        owner[i] = labels[j]
                        break
                if owner[i]:
                    break

    xs = [i % w for i in range(w * h) if owner[i] in shapes]
    ys = [i // w for i in range(w * h) if owner[i] in shapes]
    box = (min(xs) - MARGIN, min(ys) - MARGIN, max(xs) + 1 + MARGIN, max(ys) + 1 + MARGIN)

    paths, gradients = {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        for cls in ("gold", "silver"):
            cov = [coverage[i] if shapes.get(owner[i]) == cls else 0.0 for i in range(w * h)]
            paths[cls] = trace(cov, w, h, Path(tmp), cls)
            solid = [(i % w, i // w, (rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]))
                     for i in range(0, w * h, 3) if shapes.get(owner[i]) == cls and coverage[i] >= SOLID]
            cls_xs = [i % w for i in range(w * h) if shapes.get(owner[i]) == cls]
            cls_ys = [i // w for i in range(w * h) if shapes.get(owner[i]) == cls]
            gradients[cls] = fit_gradient(solid, (min(cls_xs), min(cls_ys), max(cls_xs), max(cls_ys)))

    bx, by, bw, bh = box[0], box[1], box[2] - box[0], box[3] - box[1]
    scale = 1.0 / UPSCALE

    def shape_group(cls: str, fill: str) -> str:
        transform, d = paths[cls]
        return (f'  <g transform="scale({scale}) {transform}">\n'
                f'    <path fill="{fill}" d="{d}"/>\n  </g>\n')

    def to_path_space(x: float, y: float) -> tuple[float, float]:
        # userSpaceOnUse is the painted path's own coordinate system: undo potrace's
        # "translate(tx,ty) scale(sx,sy)" and the 4x upscale.
        tx, ty, sx, sy = (float(v) for v in re.findall(r"-?[0-9.]+", paths["gold"][0]))
        return (x * UPSCALE - tx) / sx, (y * UPSCALE - ty) / sy

    def gradient(cls: str) -> str:
        x1, y1, c1, x2, y2, c2 = gradients[cls]
        (x1, y1), (x2, y2) = to_path_space(x1, y1), to_path_space(x2, y2)
        return (f'    <linearGradient id="boswas-mark-{cls}" gradientUnits="userSpaceOnUse" '
                f'x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}">\n'
                f'      <stop offset="0" stop-color="{hexc(c1)}"/>\n'
                f'      <stop offset="1" stop-color="{hexc(c2)}"/>\n'
                f'    </linearGradient>\n')

    header = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{bw}" height="{bh}" '
              f'viewBox="{bx} {by} {bw} {bh}">\n')
    note = ("  <!-- Generated by desktop/branding/tools/trace_mark.py from source/boswas-os-logo.png;"
            " do not edit by hand. -->\n")
    colour = (header + "  <title>Boswas OS mark</title>\n" + note + "  <defs>\n" + gradient("gold") +
              gradient("silver") + "  </defs>\n" + shape_group("silver", "url(#boswas-mark-silver)") +
              shape_group("gold", "url(#boswas-mark-gold)") + "</svg>\n")
    mono = (header + "  <title>Boswas OS mark (single colour)</title>\n" + note +
            shape_group("silver", "#FFFFFF") + shape_group("gold", "#FFFFFF") + "</svg>\n")
    flat_shapes = shape_group("silver", FLAT["silver"]) + shape_group("gold", FLAT["gold"])
    flat = (header + "  <title>Boswas OS mark (flat colours)</title>\n" + note + flat_shapes + "</svg>\n")
    # Favicon: the mark centred in a 64 x 64 navy tile, as a nested <svg> (no references).
    mark_h = 46.0
    mark_w = mark_h * bw / bh
    favicon = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Boswas">\n'
               "  <!-- Generated by desktop/branding/tools/trace_mark.py; do not edit by hand. -->\n"
               '  <rect width="64" height="64" rx="14" fill="#111A2B"/>\n'
               f'  <svg x="{(64 - mark_w) / 2:.2f}" y="{(64 - mark_h) / 2:.2f}" width="{mark_w:.2f}" '
               f'height="{mark_h:.2f}" viewBox="{bx} {by} {bw} {bh}">\n'
               + "".join("  " + line + "\n" for line in flat_shapes.splitlines()) + "  </svg>\n</svg>\n")
    (BRANDING / "boswas-os-mark.svg").write_text(colour)
    (BRANDING / "boswas-os-mark-mono.svg").write_text(mono)
    (BRANDING / "boswas-os-mark-flat.svg").write_text(flat)
    FAVICON.write_text(favicon)
    for cls in ("gold", "silver"):
        x1, y1, c1, x2, y2, c2 = gradients[cls]
        print(f"{cls}: {hexc(c1)} -> {hexc(c2)}")
    print(f"viewBox {bx} {by} {bw} {bh}; wrote boswas-os-mark.svg and boswas-os-mark-mono.svg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
