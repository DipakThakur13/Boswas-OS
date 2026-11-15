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

Keys read: ``scheme`` (``"dark"`` or ``"light"``) and ``reduced_motion``
(a boolean). A file that is missing, too large, not TOML or has a value of
the wrong type is skipped; the defaults are dark and motion on.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .tokens import DEFAULT_SCHEME, SCHEMES

SYSTEM_FILES = (Path("/usr/share/boswas/compositor/compositor.toml"), Path("/etc/boswas/compositor.toml"))
_MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class Appearance:
    scheme: str = DEFAULT_SCHEME
    reduced_motion: bool = False


def config_home() -> Path:
    """``$XDG_CONFIG_HOME`` when it is an absolute path, else ``~/.config``."""
    value = os.environ.get("XDG_CONFIG_HOME") or ""
    return Path(value) if value.startswith("/") else Path.home() / ".config"


def user_dir() -> Path:
    """``$XDG_CONFIG_HOME/boswas``: where the user's desktop configuration lives."""
    return config_home() / "boswas"


def appearance_path() -> Path:
    return user_dir() / "appearance.toml"


def _table(path: Path, only_appearance: bool = False) -> dict | None:
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
    table = document.get("appearance")
    if not isinstance(table, dict) or (only_appearance and len(document) != 1):
        return None
    return table


def read_appearance(system_files: tuple[Path, ...] | None = None, user: Path | None = None) -> Appearance:
    """The look the desktop is configured for (see the module text)."""
    directory = user if user is not None else user_dir()
    layers = [(path, False) for path in (SYSTEM_FILES if system_files is None else system_files)]
    layers += [(directory / "compositor.toml", False), (directory / "appearance.toml", True)]
    scheme, reduced = DEFAULT_SCHEME, False
    for path, only in layers:
        table = _table(path, only)
        if table is None:
            continue
        if table.get("scheme") in SCHEMES:
            scheme = table["scheme"]
        if isinstance(table.get("reduced_motion"), bool):
            reduced = table["reduced_motion"]
    return Appearance(scheme, reduced)
