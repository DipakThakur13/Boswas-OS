"""The colour schemes of the terminal (no Qt).

"Boswas Prism" comes from the UI tokens: the sixteen terminal colours, the
background and the foreground are the ones ``desktop/ui/tools/build_kde_colors.py``
defines for terminals (``TERMINAL`` below is that tool's table of token
references; a test compares the two). The scheme named ``prism`` follows the desktop:
it is the dark one while the desktop is dark and the light one otherwise.

The classic schemes are data files beside this module (``schemes/*.json``);
no colour value is written in the code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from boswas_fluent.tokens import Theme

# (normal, intense) for black, red, green, yellow, blue, magenta, cyan, white: token references per scheme.
TERMINAL = {
    "dark": [("ink.600", "ink.500"), ("coral.400", "coral.300"), ("mint.400", "mint.300"), ("sun.400", "sun.300"),
             ("azure.400", "azure.300"), ("orchid.400", "orchid.300"), ("sky.400", "sky.300"),
             ("cloud.200", "white")],
    "light": [("ink.900", "ink.600"), ("coral.700", "coral.700"), ("mint.700", "mint.700"), ("sun.800", "sun.800"),
              ("azure.700", "azure.700"), ("orchid.700", "orchid.700"), ("sky.700", "sky.700"),
              ("cloud.600", "ink.500")],
}
FOLLOW = "prism"                         # the scheme key that follows the desktop
DEFAULT_SCHEME = FOLLOW
_DIRECTORY = Path(__file__).resolve().parent / "schemes"
_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CUBE = (0, 95, 135, 175, 215, 255)


@dataclass(frozen=True)
class Scheme:
    key: str
    name: str
    dark: bool
    foreground: str
    background: str
    cursor: str
    selection: str
    selection_text: str
    colors: tuple[str, ...]              # the 16 ANSI colours: 8 normal, 8 bright


def prism(mode: str, theme: Theme | None = None) -> Scheme:
    """Boswas Prism, ``dark`` or ``light``, from the tokens."""
    theme = theme if theme is not None and theme.scheme == mode else Theme(mode, theme.tokens if theme else None)
    table = TERMINAL[mode]
    colors = tuple(theme.color(normal) for normal, _intense in table) \
        + tuple(theme.color(intense) for _normal, intense in table)
    name = "Boswas Prism Dark" if mode == "dark" else "Boswas Prism Light"
    return Scheme(f"prism-{mode}", name, mode == "dark", theme.color("text"), theme.color("background"),
                  theme.color("text"), theme.color("accent"), theme.color("on-accent"), colors)


def _load(path: Path) -> Scheme | None:
    try:
        if path.stat().st_size > 65536:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    colors = data.get("colors")
    single = [data.get(key) for key in ("foreground", "background", "cursor", "selection", "selection-text")]
    if (not isinstance(colors, list) or len(colors) != 16 or not isinstance(data.get("name"), str)
            or not all(isinstance(value, str) and _COLOUR.match(value) for value in [*colors, *single])):
        return None
    return Scheme(path.stem, data["name"][:64], bool(data.get("dark")), single[0].upper(), single[1].upper(),
                  single[2].upper(), single[3].upper(), single[4].upper(), tuple(value.upper() for value in colors))


@lru_cache(maxsize=1)
def classic() -> dict[str, Scheme]:
    """The schemes that are not made from the tokens, by key."""
    out: dict[str, Scheme] = {}
    try:
        files = sorted(_DIRECTORY.glob("*.json"))
    except OSError:
        files = []
    for path in files:
        scheme = _load(path)
        if scheme is not None:
            out[scheme.key] = scheme
    return out


def names() -> list[tuple[str, str]]:
    """(key, name) of every scheme, in the order the settings list them."""
    out = [(FOLLOW, "Boswas Prism (follows the desktop)"), ("prism-dark", "Boswas Prism Dark"),
           ("prism-light", "Boswas Prism Light")]
    out.extend((key, scheme.name) for key, scheme in classic().items())
    return out


def is_known(key: str) -> bool:
    return key in (FOLLOW, "prism-dark", "prism-light") or key in classic()


def get(key: str, desktop: str = "dark", theme: Theme | None = None) -> Scheme:
    """The scheme ``key`` (``prism`` follows ``desktop``, ``dark`` or ``light``); unknown keys give Prism."""
    if key == "prism-dark":
        return prism("dark", theme)
    if key == "prism-light":
        return prism("light", theme)
    found = classic().get(key)
    if found is not None:
        return found
    return prism("light" if desktop == "light" else "dark", theme)


def channels(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def palette(scheme: Scheme) -> list[tuple[int, int, int]]:
    """The 256 indexed colours: the scheme's sixteen, the 6x6x6 colour cube, the grey ramp."""
    out = [channels(colour) for colour in scheme.colors]
    out.extend((r, g, b) for r in _CUBE for g in _CUBE for b in _CUBE)
    out.extend((8 + 10 * step,) * 3 for step in range(24))
    return out
