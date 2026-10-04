#!/usr/bin/env python3
"""Inline <image href="*.svg"> references so an SVG becomes self-contained.

Boswas branding sources reference the traced brand assets with <image> for
readability. librsvg can follow such references, but Qt (KDE icons, the About
page logo) cannot, so every SVG that ships is passed through this tool.

Supported on each <image> element:
  x, y, width, height       placement box (user units)
  preserveAspectRatio       xMin|xMid|xMax + YMin|YMid|YMax, always "meet"
  data-fill="#RRGGBB"       recolour the referenced artwork
  opacity                   copied to the inlined group

Usage: inline-svg.py INPUT.svg OUTPUT.svg   (references resolve next to INPUT)
"""

from __future__ import annotations

import copy
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG)
ET.register_namespace("xlink", XLINK)


def _num(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    match = re.match(r"\s*(-?[0-9.]+)", value)
    return float(match.group(1)) if match else default


def _viewbox(root: ET.Element) -> tuple[float, float, float, float]:
    if root.get("viewBox"):
        x, y, w, h = (float(v) for v in re.split(r"[\s,]+", root.get("viewBox").strip()))
        return x, y, w, h
    return 0.0, 0.0, _num(root.get("width")), _num(root.get("height"))


def _recolour(element: ET.Element, fill: str) -> None:
    for node in element.iter():
        if node.get("fill") not in (None, "none"):
            node.set("fill", fill)


def inline(path: Path, depth: int = 0) -> ET.Element:
    if depth > 4:
        raise RuntimeError(f"{path}: <image> nesting too deep")
    tree = ET.parse(path)
    root = tree.getroot()
    parents = {child: parent for parent in root.iter() for child in parent}
    for image in list(root.iter(f"{{{SVG}}}image")):
        href = image.get("href") or image.get(f"{{{XLINK}}}href") or ""
        if not href.endswith(".svg"):
            continue
        child = inline(path.parent / href, depth + 1)
        vx, vy, vw, vh = _viewbox(child)
        x, y = _num(image.get("x")), _num(image.get("y"))
        w, h = _num(image.get("width"), vw), _num(image.get("height"), vh)
        scale = min(w / vw, h / vh)
        align = image.get("preserveAspectRatio", "xMidYMid")
        free_x, free_y = w - vw * scale, h - vh * scale
        dx = {"xMin": 0.0, "xMax": free_x}.get(align[:4], free_x / 2)
        dy = {"YMin": 0.0, "YMax": free_y}.get(align[4:8], free_y / 2)
        group = ET.Element(f"{{{SVG}}}g", {
            "transform": f"translate({x + dx - vx * scale:.3f} {y + dy - vy * scale:.3f}) scale({scale:.6f})"
        })
        for attr in ("opacity", "id", "class"):
            if image.get(attr):
                group.set(attr, image.get(attr))
        for node in child:
            if node.tag in (f"{{{SVG}}}title", f"{{{SVG}}}desc", f"{{{SVG}}}metadata"):
                continue
            group.append(copy.deepcopy(node))
        if image.get("data-fill"):
            _recolour(group, image.get("data-fill"))
        parent = parents[image]
        parent.insert(list(parent).index(image), group)
        parent.remove(image)
    return root


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write(__doc__)
        return 2
    root = inline(Path(argv[1]))
    ET.ElementTree(root).write(argv[2], encoding="unicode", xml_declaration=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
