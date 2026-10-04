"""Shared helpers for the preset tests (standard library only)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

PRESETS_DIR = Path(__file__).resolve().parent.parent
REPO = PRESETS_DIR.parent.parent
PRESETS_FILE = PRESETS_DIR / "presets.json"
INLINE_SVG = REPO / "build" / "scripts" / "inline-svg.py"

PRESET_IDS = ["horizon", "midnight", "aurora", "slate", "carbon",
              "pearl", "ocean", "ember", "nebula", "classic"]
HEX = re.compile(r"^#[0-9A-F]{6}$")
TEAL = ("17C6C0", "2ED3CD", "23,198,192", "46,211,205")


def load_presets() -> dict:
    with PRESETS_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)


def presets() -> list[dict]:
    return load_presets()["presets"]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_ini(path: Path) -> dict[str, dict[str, str]]:
    """Parse a KConfig/INI file into {group: {key: value}}. Group names keep
    KConfig's nesting syntax, e.g. "Colors:Header][Inactive"."""
    groups: dict[str, dict[str, str]] = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            if current in groups:
                raise AssertionError(f"{path}: duplicate group [{current}]")
            groups[current] = {}
            continue
        if current is None or "=" not in line:
            raise AssertionError(f"{path}: unexpected line {raw!r}")
        key, _, value = line.partition("=")
        if key in groups[current]:
            raise AssertionError(f"{path}: duplicate key {key} in [{current}]")
        groups[current][key] = value
    return groups


def parse_triplet(value: str) -> tuple[int, int, int]:
    parts = value.split(",")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not an R,G,B triplet: {value!r}")
    channels = tuple(int(p) for p in parts)
    if not all(0 <= c <= 255 for c in channels):
        raise ValueError(f"channel out of range: {value!r}")
    return channels


def to_rgb(color) -> tuple[int, int, int]:
    if isinstance(color, tuple):
        return color
    if color.startswith("#"):
        return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return parse_triplet(color)


def luminance(color) -> float:
    def lin(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def lab(color) -> tuple[float, float, float]:
    def lin(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in to_rgb(color))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a, b) -> float:
    la, lb = lab(a), lab(b)
    return sum((p - q) ** 2 for p, q in zip(la, lb)) ** 0.5


def hue_saturation(color) -> tuple[float, float]:
    r, g, b = (c / 255.0 for c in to_rgb(color))
    high, low = max(r, g, b), min(r, g, b)
    if high == low:
        return 0.0, 0.0
    d = high - low
    if high == r:
        h = ((g - b) / d) % 6
    elif high == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60.0, d / high
