"""The Boswas UI tokens as a theme (standard library only, no Qt).

``desktop/ui/tokens.json`` is the single source of truth of the design
system. This module finds the file, checks it, and answers every question
the toolkit has about colour, shape, type and motion through ``Theme``.
No colour value is written anywhere else in the toolkit; where the token
file has no entry for something a control needs (the colour of disabled
text, the tint of a warning bar), the value is *derived* from tokens with
a fixed ratio, listed in ``DERIVED``.

Where the token file is read from, first match wins:

1. the file named by the environment variable ``BOSWAS_UI_TOKENS`` (tests);
2. ``tokens.json`` two directories above this package, when the toolkit runs
   from the source tree (``desktop/ui/qt/boswas_fluent`` -> ``desktop/ui``);
3. ``/usr/share/boswas/ui/tokens.json`` (installed by boswas-branding).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

TOKENS_ENV = "BOSWAS_UI_TOKENS"
INSTALLED_TOKENS = Path("/usr/share/boswas/ui/tokens.json")
SCHEMES = ("dark", "light")
DEFAULT_SCHEME = "dark"
# The palette hues the desktop's accent colour can be (``[appearance] accent``); the first is the token file's
# own accent. Another hue takes the same shades of its family as the token file's accent takes of its own.
ACCENTS = ("iris", "azure", "sky", "mint", "sun", "coral", "orchid", "slate")
DEFAULT_ACCENT = ACCENTS[0]
# The roles that follow the accent colour.
ACCENT_ROLES = ("accent", "accent-hover", "accent-pressed")
# Accessibility > Text size: the type ramp in percent of its own sizes.
TEXT_SCALE_RANGE = (100, 225)

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MAX_TOKEN_BYTES = 1 << 20

# Ratios for colours the token file does not name. Each derived colour is a
# token colour at an opacity, or two token colours mixed; never a new hue.
DERIVED = {
    # opacity of 'text' for disabled text and glyphs (exempt from contrast rules, as in WCAG 1.4.3)
    "text-disabled": {"dark": 0.36, "light": 0.36},
    # opacity of 'text' for the strong outline of check boxes, radio buttons, the rest line of text
    # boxes and the rail of sliders (a control boundary: 3:1 against the surface)
    "stroke-strong": {"dark": 0.54, "light": 0.5},
    # opacity of the fill colour for a disabled accent control
    "accent-disabled": {"dark": 0.16, "light": 0.22},
    # how much of a status colour is mixed into the surface behind an InfoBar or a Badge
    "status-tint": {"dark": 0.16, "light": 0.12},
    # how much of 'surface' is mixed into 'background' for the content layer of a NavigationView
    "layer": {"dark": 0.5, "light": 0.5},
    # opacity of the accent colour for selected text ranges that keep the text colour
    "accent-subtle": {"dark": 0.24, "light": 0.16},
}
# The contrast (WCAG 2.1, 1.4.3) the toolkit guarantees for text: a colour that is used for text, or
# behind text, and misses this ratio with the tokens as they are, is moved towards a colour that has
# it, in steps of CONTRAST_STEP, and only as far as needed (Theme._readable).
TEXT_CONTRAST = 4.5
CONTRAST_STEP = 0.05


class TokenError(ValueError):
    """The token file is missing, unreadable or lacks something the toolkit needs."""


# --- colour arithmetic -----------------------------------------------------------------------------------

def parse_hex(colour: str) -> tuple[int, int, int]:
    """``#RRGGBB`` -> (r, g, b)."""
    if not isinstance(colour, str) or not _HEX.match(colour):
        raise TokenError(f"not a #RRGGBB colour: {colour!r}")
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def blend(base: str, over: str, alpha: float) -> str:
    """``over`` at opacity ``alpha`` (0..1) on top of the opaque colour ``base``."""
    b, o = parse_hex(base), parse_hex(over)
    return to_hex(tuple(b[i] + (o[i] - b[i]) * alpha for i in range(3)))


def luminance(colour: str) -> float:
    """WCAG relative luminance."""
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = parse_hex(colour)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two opaque colours (1..21)."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def css_rgba(colour: str, alpha: float) -> str:
    """A colour for a Qt style sheet: ``#RRGGBB`` when opaque, else ``rgba(r, g, b, a)``."""
    if alpha >= 1:
        return colour.upper()
    r, g, b = parse_hex(colour)
    return f"rgba({r}, {g}, {b}, {round(alpha * 255)})"


# --- the file ----------------------------------------------------------------------------------------------

def tokens_path() -> Path:
    """The token file this process uses (see the module text for the order)."""
    override = os.environ.get(TOKENS_ENV)
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    checkout = here.parents[2] / "tokens.json"
    if here.parents[1].name == "qt" and here.parents[2].name == "ui" and os.path.isfile(checkout):
        return checkout
    return INSTALLED_TOKENS


def load_tokens(path: str | os.PathLike | None = None) -> dict:
    """Read and check a token file."""
    source = Path(path) if path is not None else tokens_path()
    try:
        with open(source, "rb") as handle:
            raw = handle.read(_MAX_TOKEN_BYTES + 1)
    except OSError as exc:
        raise TokenError(f"cannot read the Boswas UI tokens ({source}): {exc.strerror or exc}") from exc
    if len(raw) > _MAX_TOKEN_BYTES:
        raise TokenError(f"{source}: larger than {_MAX_TOKEN_BYTES} bytes")
    try:
        tokens = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenError(f"{source}: not valid JSON ({exc})") from exc
    _check(tokens, str(source))
    return tokens


def _check(tokens: object, source: str) -> None:
    if not isinstance(tokens, dict):
        raise TokenError(f"{source}: the top level is not an object")
    for key in ("color", "semantic", "stroke", "fill", "material", "radius", "spacing", "type", "motion"):
        if not isinstance(tokens.get(key), dict):
            raise TokenError(f"{source}: '{key}' is missing")
    for scheme in SCHEMES:
        for group in ("semantic", "stroke", "fill"):
            if not isinstance(tokens[group].get(scheme), dict):
                raise TokenError(f"{source}: '{group}.{scheme}' is missing")
    # Building both themes resolves every reference and every value the toolkit uses.
    for scheme in SCHEMES:
        theme = Theme(scheme, tokens)
        for name in COLOUR_NAMES:
            theme.color(name)
        for name in ("control", "card", "flyout", "tooltip", "pill"):
            theme.radius(name)
        for name in RAMP:
            theme.type(name)
        for name in ("fast", "normal", "slow"):
            theme.duration(name)


@lru_cache(maxsize=4)
def _cached_tokens(path: str, _mtime: float) -> dict:
    return load_tokens(path)


def default_tokens() -> dict:
    """The tokens of this process, read once (again when the file changes)."""
    path = tokens_path()
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise TokenError(f"cannot read the Boswas UI tokens ({path}): {exc.strerror or exc}") from exc
    return _cached_tokens(str(path), mtime)


# --- the theme ---------------------------------------------------------------------------------------------

# Semantic roles of tokens.json that every theme must define.
ROLES = ("background", "surface", "surface-raised", "surface-hover", "chrome", "border", "text", "text-secondary",
         "text-muted", "accent", "accent-hover", "accent-pressed", "on-accent", "focus", "success", "warning",
         "danger", "info")
STATUSES = ("info", "success", "warning", "danger")
FILLS = ("control", "control-hover", "control-pressed", "subtle-hover", "subtle-pressed", "disabled")
STROKES = ("control", "surface", "divider", "window")
RAMP = ("caption", "body", "body-strong", "body-large", "subtitle", "title", "title-large")
# Derived opaque colours (see DERIVED and Theme.color).
DERIVED_COLOURS = ("layer", "card", "flyout", "tooltip", "smoke", "input-active", "control-solid", "info-tint",
                   "success-tint", "warning-tint", "danger-tint", "accent-tint", "neutral-tint",
                   # colours for text on the page: the role itself where it is readable enough
                   "accent-text", "accent-text-strong", "info-text", "success-text", "warning-text", "danger-text",
                   # colours behind 'on-accent' text
                   "accent-fill-hover", "accent-fill-pressed", "info-fill", "success-fill", "warning-fill",
                   "danger-fill")
# Derived colours that are another role, chosen by scheme.
_SCHEME_ROLES = {
    # the fill of a text box that has the focus: darker than the page in dark, the page's brightest in light
    "input-active": {"dark": "background", "light": "surface"},
    # an opaque control surface that sits on a track: the thumb of a slider
    "control-solid": {"dark": "surface-hover", "light": "surface"},
}
COLOUR_NAMES = ROLES + DERIVED_COLOURS


@dataclass(frozen=True)
class TypeStyle:
    """One step of the type ramp, in logical pixels."""
    size: int
    line_height: int
    weight: int


class Theme:
    """The tokens seen through one scheme (``"dark"`` or ``"light"``). Immutable.

    Colours are returned as ``#RRGGBB`` strings; ``Theme.rgba`` gives a colour
    with its opacity for the translucent layers (fills and strokes), which are
    defined as an opacity over whatever lies beneath.

    ``accent`` is a hue of ``ACCENTS`` (Personalization > Colours): the accent
    roles take the same shades of that hue as the token file's accent takes of
    its own, and ``accent`` itself is moved towards the text colour just far
    enough for ``on-accent`` text to keep 4.5:1, as the derived fills are.
    ``text_scale`` (percent, 100 to 225; Accessibility > Text size) enlarges
    the type ramp and nothing else.
    """

    accent: str = DEFAULT_ACCENT
    text_scale: int = 100

    def __init__(self, scheme: str = DEFAULT_SCHEME, tokens: Mapping | None = None, *,
                 accent: str | None = None, text_scale: int = 100):
        if scheme not in SCHEMES:
            raise TokenError(f"unknown scheme {scheme!r} (expected one of {', '.join(SCHEMES)})")
        if accent is not None and accent not in ACCENTS:
            raise TokenError(f"unknown accent colour {accent!r} (expected one of {', '.join(ACCENTS)})")
        if isinstance(text_scale, bool) or not isinstance(text_scale, int) \
                or not TEXT_SCALE_RANGE[0] <= text_scale <= TEXT_SCALE_RANGE[1]:
            raise TokenError(f"a text size of {text_scale!r} percent is not offered")
        self.scheme = scheme
        self.tokens = tokens if tokens is not None else default_tokens()
        self.accent = accent or DEFAULT_ACCENT
        self.text_scale = text_scale
        self._cache: dict[str, str] = {}

    def __repr__(self) -> str:
        extra = "".join((f", accent={self.accent!r}" if self.accent != DEFAULT_ACCENT else "",
                         f", text_scale={self.text_scale}" if self.text_scale != 100 else ""))
        return f"Theme({self.scheme!r}{extra})"

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Theme) and other.scheme == self.scheme and other.tokens == self.tokens
                and other.accent == self.accent and other.text_scale == self.text_scale)

    def __hash__(self) -> int:
        return hash((self.scheme, id(self.tokens), self.accent, self.text_scale))

    @property
    def is_dark(self) -> bool:
        return self.scheme == "dark"

    # --- colours ---
    def _palette(self, reference: str) -> str:
        colours = self.tokens["color"]
        if reference in ("white", "black"):
            value = colours.get(reference)
        else:
            family, _, shade = reference.partition(".")
            value = colours.get(family, {}).get(shade) if isinstance(colours.get(family), dict) else None
        if not isinstance(value, str) or not _HEX.match(value):
            raise TokenError(f"unknown colour reference {reference!r}")
        return value.upper()

    def color(self, name: str) -> str:
        """An opaque colour as ``#RRGGBB``.

        ``name`` is a semantic role (``"accent"``, ``"text-secondary"``; an
        optional leading ``@`` is accepted), a derived colour (``"layer"``,
        ``"card"``, ``"warning-tint"``, ...), a palette reference
        (``"iris.500"``, ``"white"``) or a literal ``#RRGGBB``.
        """
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        key = name[1:] if name.startswith("@") else name
        roles = self.tokens["semantic"][self.scheme]
        if key in ACCENT_ROLES and self.accent != DEFAULT_ACCENT:
            value = self._accent_role(key)
        elif key in roles:
            value = self._palette(roles[key]) if not str(roles[key]).startswith("#") else str(roles[key]).upper()
        elif key in DERIVED_COLOURS:
            value = self._derived(key)
        elif key.startswith("#"):
            parse_hex(key)
            value = key.upper()
        else:
            value = self._palette(key)
        self._cache[name] = value
        return value

    def _accent_role(self, key: str) -> str:
        """An accent role in the chosen hue: the token file's shade of it (``iris.400`` -> ``azure.400``)."""
        reference = str(self.tokens["semantic"][self.scheme].get(key, ""))
        family, dot, shade = reference.partition(".")
        if not dot:
            raise TokenError(f"semantic.{self.scheme}.{key} is not a palette shade; the accent cannot change")
        value = self._palette(f"{self.accent}.{shade}")
        if key == "accent":
            value = self._readable(value, [self.color("on-accent")], (self.color("text"),))
        return value

    def _derived(self, key: str) -> str:
        if key == "layer":
            return blend(self.color("background"), self.color("surface"), DERIVED["layer"][self.scheme])
        if key in ("card", "flyout"):
            return self.color("surface")              # acrylic without the blur: its tint, opaque
        if key == "tooltip":
            return self.color("surface-raised")
        if key == "smoke":
            return self.color(self._material("smoke")["tint"])
        if key in _SCHEME_ROLES:
            return self.color(_SCHEME_ROLES[key][self.scheme])
        if key.endswith("-tint"):
            status = key[:-5]
            if status == "neutral":
                return self.color("surface")
            return blend(self.color("surface"), self.color(status), DERIVED["status-tint"][self.scheme])
        if key == "accent-text-strong":
            # the link colour while hovered or pressed: readable on the state layers too
            behind = [self.over(layer, base) for layer in ("fill-subtle-hover", "fill-subtle-pressed")
                      for base in ("background", "surface")]
            return self._readable(self.color("accent-text"), behind, (self.color("text"),))
        if key.endswith("-text"):
            behind = [self.color(surface) for surface in ("background", "layer", "surface")]
            return self._readable(self.color(key[:-5]), behind, (self.color("text"),))
        if key in ("accent-fill-hover", "accent-fill-pressed"):
            role = "accent-hover" if key.endswith("hover") else "accent-pressed"
            return self._readable(self.color(role), [self.color("on-accent")],
                                  (self.color("accent"), self.color("text")))
        if key.endswith("-fill"):
            return self._readable(self.color(key[:-5]), [self.color("on-accent")], (self.color("text"),))
        raise TokenError(f"unknown derived colour {key!r}")

    @staticmethod
    def _readable(colour: str, others: list[str], towards: tuple[str, ...]) -> str:
        """``colour``, moved towards the colours of ``towards`` (one after the other) just far enough to
        have TEXT_CONTRAST against every colour of ``others``; unchanged when it already has."""
        def enough(candidate: str) -> bool:
            return all(contrast(candidate, other) >= TEXT_CONTRAST for other in others)
        if enough(colour):
            return colour
        candidate = colour
        for target in towards:
            start = candidate
            for step in range(1, round(1 / CONTRAST_STEP) + 1):
                candidate = blend(start, target, step * CONTRAST_STEP)
                if enough(candidate):
                    return candidate
        return candidate

    def _material(self, name: str) -> Mapping:
        material = self.tokens["material"].get(name)
        if not isinstance(material, dict) or "tint" not in material:
            raise TokenError(f"unknown material {name!r}")
        return material

    def rgba(self, name: str) -> tuple[str, float]:
        """A translucent layer as (``#RRGGBB``, opacity 0..1).

        ``fill-<name>`` (control, control-hover, control-pressed, subtle-hover,
        subtle-pressed, disabled), ``stroke-<name>`` (control, surface, divider,
        window), ``stroke-strong``, ``text-disabled``, ``accent-disabled``,
        ``accent-subtle`` and ``smoke``. Any opaque colour name is accepted too
        and comes back with opacity 1.
        """
        if name.startswith("fill-") and name[5:] in self.tokens["fill"][self.scheme]:
            layer = self.tokens["fill"][self.scheme]
            return self.color(layer["color"]), self._opacity(layer[name[5:]], name)
        if name.startswith("stroke-") and name[7:] in self.tokens["stroke"][self.scheme]:
            layer = self.tokens["stroke"][self.scheme]
            return self.color(layer["color"]), self._opacity(layer[name[7:]], name)
        if name in ("stroke-strong", "text-disabled"):
            return self.color("text"), DERIVED[name][self.scheme]
        if name == "accent-disabled":
            return self.color(self.tokens["fill"][self.scheme]["color"]), DERIVED[name][self.scheme]
        if name == "accent-subtle":
            return self.color("accent"), DERIVED[name][self.scheme]
        if name == "smoke":
            return self.color("smoke"), self._opacity(self._material("smoke").get("tint-opacity"), name)
        return self.color(name), 1.0

    @staticmethod
    def _opacity(value: object, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise TokenError(f"{name}: not an opacity between 0 and 1: {value!r}")
        return float(value)

    def over(self, name: str, base: str = "surface") -> str:
        """The layer ``name`` (see ``rgba``) painted on the opaque colour ``base``, as ``#RRGGBB``."""
        colour, alpha = self.rgba(name)
        return blend(self.color(base), colour, alpha)

    def css(self, name: str) -> str:
        """The colour or layer ``name`` for a Qt style sheet (``#RRGGBB`` or ``rgba(...)``)."""
        return css_rgba(*self.rgba(name))

    def status(self, kind: str) -> str:
        """The colour of a status: info, success, warning or danger (``error`` and ``ok`` are accepted)."""
        return self.color(_STATUS_ALIASES.get(kind, kind))

    # --- shape, type, motion ---
    def radius(self, name: str = "control") -> int:
        value = self.tokens["radius"].get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TokenError(f"unknown radius {name!r}")
        return value

    def spacing(self, steps: float = 1) -> int:
        """``steps`` times the spacing unit (4 logical pixels)."""
        unit = self.tokens["spacing"].get("unit")
        if isinstance(unit, bool) or not isinstance(unit, int) or unit <= 0:
            raise TokenError("spacing.unit is missing")
        return round(unit * steps)

    def type(self, name: str = "body") -> TypeStyle:
        """A step of the type ramp: caption, body, body-strong, body-large, subtitle, title, title-large
        (at the desktop's text size)."""
        entry = self.tokens["type"].get("ramp", {}).get(name)
        if (not isinstance(entry, list) or len(entry) != 3
                or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in entry)):
            raise TokenError(f"unknown type style {name!r}")
        size, line_height, weight = entry
        if self.text_scale != 100:
            size, line_height = round(size * self.text_scale / 100), round(line_height * self.text_scale / 100)
        return TypeStyle(size, line_height, weight)

    @property
    def families(self) -> tuple[str, ...]:
        """The interface typeface and its fallbacks, in order."""
        families = self.tokens["type"].get("family")
        if not isinstance(families, list) or not families or not all(isinstance(f, str) and f for f in families):
            raise TokenError("type.family is missing")
        return tuple(families)

    def duration(self, name: str = "normal") -> int:
        """A motion duration in milliseconds: fast, normal or slow (not adjusted for reduced motion)."""
        value = self.tokens["motion"].get(f"{name}-ms")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TokenError(f"unknown duration {name!r}")
        return value

    def curve(self, name: str = "decelerate") -> tuple[float, float, float, float]:
        """A cubic Bezier easing curve (x1, y1, x2, y2): decelerate, accelerate or standard."""
        value = self.tokens["motion"].get("curves", {}).get(name)
        if not isinstance(value, list) or len(value) != 4 or not all(isinstance(v, (int, float)) for v in value):
            raise TokenError(f"unknown curve {name!r}")
        return tuple(float(v) for v in value)       # type: ignore[return-value]

    def elevation(self, name: str = "flyout") -> tuple[int, int, float]:
        """A shadow as (blur, y offset, opacity): tooltip, flyout, dialog or window."""
        value = self.tokens.get("elevation", {}).get(name)
        if not isinstance(value, dict):
            raise TokenError(f"unknown elevation {name!r}")
        return int(value.get("blur", 0)), int(value.get("y", 0)), float(value.get("alpha", 0.0))


_STATUS_ALIASES = {"error": "danger", "critical": "danger", "ok": "success", "attention": "accent",
                   "neutral": "text-secondary"}
