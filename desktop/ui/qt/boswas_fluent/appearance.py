"""What the desktop says about the look: light or dark, and reduced motion (standard library only).

The native Boswas desktop keeps the look in the ``[appearance]`` table of
its configuration (docs/architecture/platform-contracts.md). This module
reads that table the way the compositor does, later files overriding
earlier ones key by key:

1. ``/usr/share/boswas/compositor/compositor.toml``
2. ``/etc/boswas/compositor.toml``
3. ``$XDG_CONFIG_HOME/boswas/compositor.toml``
4. ``$XDG_CONFIG_HOME/boswas/appearance.toml`` (written by boswas-preset; used
   only when it holds nothing but the ``[appearance]`` table)

Keys read: ``scheme`` (``"dark"`` or ``"light"``), ``reduced_motion`` (a
boolean) and ``accent`` (a hue of ``tokens.ACCENTS``) of ``[appearance]``;
``text_scale`` (percent, 100 to 225) and ``always_show_scrollbars`` (a
boolean) of ``[accessibility]``, which only the three ``compositor.toml``
files hold. A file that is missing, too large or not TOML is skipped, and so
is a value of the wrong type or out of range; the defaults are dark, motion
on, the iris accent, text at 100 % and thin scroll bars.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .tokens import ACCENTS, DEFAULT_ACCENT, DEFAULT_SCHEME, SCHEMES, TEXT_SCALE_RANGE

SYSTEM_FILES = (Path("/usr/share/boswas/compositor/compositor.toml"), Path("/etc/boswas/compositor.toml"))
_MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class Appearance:
    scheme: str = DEFAULT_SCHEME
    reduced_motion: bool = False
    accent: str = DEFAULT_ACCENT
    text_scale: int = 100
    always_show_scrollbars: bool = False


def config_home() -> Path:
    """``$XDG_CONFIG_HOME`` when it is an absolute path, else ``~/.config``."""
    value = os.environ.get("XDG_CONFIG_HOME") or ""
    return Path(value) if value.startswith("/") else Path.home() / ".config"


def user_dir() -> Path:
    """``$XDG_CONFIG_HOME/boswas``: where the user's desktop configuration lives."""
    return config_home() / "boswas"


def appearance_path() -> Path:
    return user_dir() / "appearance.toml"


def _document(path: Path, only_appearance: bool = False) -> dict | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_BYTES:
        return None
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if only_appearance and (len(document) != 1 or not isinstance(document.get("appearance"), dict)):
        return None
    return document


def _table(document: dict, name: str) -> dict:
    table = document.get(name)
    return table if isinstance(table, dict) else {}


def read_appearance(system_files: tuple[Path, ...] | None = None, user: Path | None = None) -> Appearance:
    """The look the desktop is configured for (see the module text)."""
    directory = user if user is not None else user_dir()
    layers = [(path, False) for path in (SYSTEM_FILES if system_files is None else system_files)]
    layers += [(directory / "compositor.toml", False), (directory / "appearance.toml", True)]
    scheme, reduced, accent, text_scale, scrollbars = DEFAULT_SCHEME, False, DEFAULT_ACCENT, 100, False
    for path, only in layers:
        document = _document(path, only)
        if document is None:
            continue
        look, access = _table(document, "appearance"), _table(document, "accessibility")
        if look.get("scheme") in SCHEMES:
            scheme = look["scheme"]
        if isinstance(look.get("reduced_motion"), bool):
            reduced = look["reduced_motion"]
        if look.get("accent") in ACCENTS:
            accent = look["accent"]
        size = access.get("text_scale")
        if isinstance(size, int) and not isinstance(size, bool):
            text_scale = max(TEXT_SCALE_RANGE[0], min(TEXT_SCALE_RANGE[1], size))     # clamped, as the compositor does
        if isinstance(access.get("always_show_scrollbars"), bool):
            scrollbars = access["always_show_scrollbars"]
    return Appearance(scheme, reduced, accent, text_scale, scrollbars)
