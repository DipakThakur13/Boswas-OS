"""Keyboard layouts by name (standard library only, no Qt).

The compositor's [keyboard] table holds XKB names: ``layout = "us,de"``,
``variant = ",neo"``, ``options = "grp:alt_shift_toggle"``. People choose
"German (Neo 2)". The names come from the list that ships with the XKB data
(xkb-data): /usr/share/X11/xkb/rules/evdev.lst, a text file of sections

    ! layout
      de              German
    ! variant
      neo             de: German (Neo 2)
    ! option
      grp             Switching to another layout
      grp:alt_shift_toggle Alt+Shift

parse() reads it and keeps what is well formed: a line it cannot use, a
name with characters no XKB name has, a variant of a layout that is not
listed, text that is not UTF-8 or far too much of it are left out, never an
error. Text from the file is only ever shown, never interpreted.

split() and join() convert between the two strings of the configuration
and the list of (layout, variant) pairs the settings page shows;
switch_option() and with_switch_option() read and replace the one
``grp:`` option that switches layouts among the other options.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

RULES_FILE = "/usr/share/X11/xkb/rules/evdev.lst"
MAX_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 5000                       # per section; the real file has about 500 variants
MAX_LAYOUTS = 4                          # XKB keeps four layouts at a time (the keymap drops a fifth)
SWITCH_PREFIX = "grp:"

_CODE = re.compile(r"^[A-Za-z0-9_./()+-]{1,64}$")
_OPTION = re.compile(r"^[A-Za-z0-9_+()-]{1,64}(:[A-Za-z0-9_+()-]{1,64})?$")


@dataclass(frozen=True)
class Variant:
    code: str
    name: str                            # "German (Neo 2)"


@dataclass(frozen=True)
class Layout:
    code: str
    name: str                            # "German"
    variants: tuple[Variant, ...] = ()


@dataclass(frozen=True)
class Option:
    code: str                            # "grp:alt_shift_toggle"
    name: str                            # "Alt+Shift"


@dataclass(frozen=True)
class Choice:
    """One thing the user can add: a layout, or a variant of one."""

    layout: str
    variant: str
    name: str

    @property
    def pair(self) -> tuple[str, str]:
        return self.layout, self.variant


def _words(text: str, limit: int = 120) -> str:
    """Text from the file as one clean line."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(cleaned.split())[:limit]


@dataclass(frozen=True)
class Catalog:
    """The layouts, variants and options of the XKB data."""

    layouts: dict[str, Layout] = field(default_factory=dict)
    options: tuple[Option, ...] = ()
    problem: str = ""                    # why the list is empty, in words for the user

    def name(self, layout: str, variant: str = "") -> str:
        """The name people know a layout by; for one that is not listed, its XKB names."""
        entry = self.layouts.get(layout)
        if entry is None:
            return f"{layout} ({variant})" if variant else layout
        if not variant:
            return entry.name
        found = next((item.name for item in entry.variants if item.code == variant), None)
        return found or f"{entry.name} ({variant})"

    def known(self, layout: str, variant: str = "") -> bool:
        entry = self.layouts.get(layout)
        return entry is not None and (not variant or any(item.code == variant for item in entry.variants))

    def choices(self) -> list[Choice]:
        """Every layout and every variant, sorted by name (a layout before its variants)."""
        found = []
        for layout in self.layouts.values():
            found.append(Choice(layout.code, "", layout.name))
            found.extend(Choice(layout.code, variant.code, variant.name) for variant in layout.variants)
        return sorted(found, key=lambda choice: (choice.name.casefold(), choice.layout, choice.variant))

    def search(self, query: str) -> list[Choice]:
        """The choices whose name or XKB names contain every word of ``query``; the best match first.

        A layout whose code or name is exactly the query comes first; then
        the choices whose words start with the query's words (before those
        that only contain them), a layout before variants, by name. An empty
        query gives every choice.
        """
        words = query.casefold().split()
        if not words:
            return self.choices()
        whole = " ".join(words)
        ranked = []
        for choice in self.choices():
            name = choice.name.casefold()
            codes = (choice.layout.casefold(), choice.variant.casefold())
            if not all(word in name or any(word in code for code in codes) for word in words):
                continue
            tokens = re.split(r"[^0-9a-z]+", name)
            exact = not choice.variant and whole in (name, codes[0])
            loose = sum(1 for word in words
                        if word not in codes and not any(token.startswith(word) for token in tokens))
            ranked.append((0 if exact else 1, loose, 0 if name.startswith(whole) else 1, bool(choice.variant), name,
                           choice.layout, choice.variant, choice))
        ranked.sort(key=lambda entry: entry[:7])
        return [entry[7] for entry in ranked]

    def switch_options(self) -> list[Option]:
        """The options that switch between layouts (``grp:...``), in the order of the file."""
        return [option for option in self.options if option.code.startswith(SWITCH_PREFIX)]


def parse(text: str) -> Catalog:
    """The catalog of an ``evdev.lst`` text (see the module text for what is left out)."""
    layouts: dict[str, tuple[str, list[Variant]]] = {}
    options: list[Option] = []
    section = ""
    counts = {"layout": 0, "variant": 0, "option": 0}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(raw) > 1000:
            continue
        if line.startswith("!"):
            section = line[1:].strip().casefold()
            continue
        if section not in counts or counts[section] >= MAX_ENTRIES:
            continue
        code, _, rest = line.partition(" ") if " " in line else line.partition("\t")
        code, rest = code.strip(), rest.strip()
        if "\t" in code:
            code, _, more = code.partition("\t")
            rest = f"{more} {rest}"
        description = _words(rest)
        if section == "layout":
            if _CODE.fullmatch(code) and description and code not in layouts:
                layouts[code] = (description, [])
                counts[section] += 1
        elif section == "variant":
            owner, colon, name = description.partition(":")
            owner, name = owner.strip(), name.strip()
            if (_CODE.fullmatch(code) and colon and name and owner in layouts
                    and all(variant.code != code for variant in layouts[owner][1])):
                layouts[owner][1].append(Variant(code, name))
                counts[section] += 1
        elif _OPTION.fullmatch(code) and ":" in code and description \
                and all(option.code != code for option in options):
            options.append(Option(code, description))
            counts[section] += 1
    return Catalog({code: Layout(code, name, tuple(variants)) for code, (name, variants) in layouts.items()},
                   tuple(options))


def load(path: str = RULES_FILE) -> Catalog:
    """The catalog of the installed XKB data; an empty one with ``problem`` set when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        return Catalog(problem=f"The list of keyboard layouts ({path}) cannot be read: {exc.strerror or exc}.")
    if len(data) > MAX_BYTES:
        return Catalog(problem=f"The list of keyboard layouts ({path}) is too large to be read.")
    catalog = parse(data.decode("utf-8", "replace"))
    if not catalog.layouts:
        return Catalog(problem=f"The list of keyboard layouts ({path}) names no layout.")
    return catalog


# --- the configuration's strings ------------------------------------------------------------------------------

def split(layout: str, variant: str = "") -> list[tuple[str, str]]:
    """``"us,de"`` and ``",neo"`` as [("us", ""), ("de", "neo")]. Empty layout entries are left out."""
    layouts = [part.strip() for part in layout.split(",")] if isinstance(layout, str) else []
    variants = [part.strip() for part in variant.split(",")] if isinstance(variant, str) else []
    pairs = []
    for index, code in enumerate(layouts):
        if code:
            pairs.append((code, variants[index] if index < len(variants) else ""))
    return pairs


def join(pairs: list[tuple[str, str]]) -> tuple[str, str]:
    """[("us", ""), ("de", "neo")] as the ``layout`` and ``variant`` strings ``"us,de"`` and ``",neo"``.

    The variant string is empty when no layout has a variant. ValueError
    for an empty list or a name XKB cannot have.
    """
    if not pairs:
        raise ValueError("at least one keyboard layout is needed")
    for layout, variant in pairs:
        if not isinstance(layout, str) or not _CODE.fullmatch(layout):
            raise ValueError(f"{layout!r} is not a layout name")
        if not isinstance(variant, str) or (variant and not _CODE.fullmatch(variant)):
            raise ValueError(f"{variant!r} is not a variant name")
    variants = [variant for _, variant in pairs]
    return ",".join(layout for layout, _ in pairs), (",".join(variants) if any(variants) else "")


def _options(options: str) -> list[str]:
    return [part.strip() for part in options.split(",") if part.strip()] if isinstance(options, str) else []


def switch_option(options: str) -> str:
    """The first ``grp:`` option in an options string, or ""."""
    return next((part for part in _options(options) if part.startswith(SWITCH_PREFIX)), "")


def with_switch_option(options: str, option: str) -> str:
    """``options`` with its ``grp:`` options replaced by ``option`` ("" for none); the others stay in place."""
    if option and (not option.startswith(SWITCH_PREFIX) or not _OPTION.fullmatch(option)):
        raise ValueError(f"{option!r} is not a layout switching option")
    kept = [part for part in _options(options) if not part.startswith(SWITCH_PREFIX)]
    return ",".join(kept + ([option] if option else []))
