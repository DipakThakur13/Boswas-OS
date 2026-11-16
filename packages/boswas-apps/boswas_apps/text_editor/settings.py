"""What the editor remembers: the user's settings and the recent files (standard library only, no Qt).

Both are small JSON files, read with a size cap and checked value by value
(a damaged or foreign file gives the defaults) and written atomically.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import codec

SETTINGS_FILE = "settings.json"
RECENT_FILE = "recent.json"
MAX_BYTES = 256 * 1024
MAX_RECENT = 10
MAX_PATH = 4096
ZOOM_STEPS = tuple(range(10, 501, 10))
THEMES = ("system", "light", "dark")

DEFAULTS: dict[str, object] = {
    "font_family": "",              # empty: the system's fixed-width font
    "font_size": 11,
    "font_bold": False,
    "font_italic": False,
    "word_wrap": True,
    "status_bar": True,
    "line_numbers": False,
    "auto_indent": True,
    "tab_width": 8,
    "spaces_for_tabs": False,
    "trim_trailing": False,
    "restore_session": True,
    "default_line_ending": codec.LF,
    "theme": "system",
    "match_case": False,
    "whole_word": False,
    "wrap_around": True,
    "regex": False,
}
_RANGES = {"font_size": (6, 96), "tab_width": (1, 16)}
_CHOICES = {"default_line_ending": tuple(codec.LINE_ENDINGS), "theme": THEMES}


def validated(raw: object) -> dict[str, object]:
    """``raw`` (parsed JSON of unknown origin) as valid settings; a bad entry becomes its default."""
    settings = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return settings
    for key, default in DEFAULTS.items():
        value = raw.get(key)
        if isinstance(default, bool):
            if isinstance(value, bool):
                settings[key] = value
        elif isinstance(default, int):
            low, high = _RANGES[key]
            if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
                settings[key] = value
        elif key in _CHOICES:
            if value in _CHOICES[key]:
                settings[key] = value
        elif isinstance(value, str) and len(value) <= 200 and value.isprintable():
            settings[key] = value
    return settings


def _read(path: Path) -> object:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_BYTES:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def write_atomically(path: Path, data: bytes) -> bool:
    """Replace ``path`` with ``data`` (mode 0600); False when that failed, the old file is untouched then."""
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


def load(directory: str | os.PathLike) -> dict[str, object]:
    return validated(_read(Path(directory) / SETTINGS_FILE))


def save(directory: str | os.PathLike, settings: dict[str, object]) -> bool:
    data = json.dumps(validated(settings), indent=1, sort_keys=True).encode("utf-8")
    return write_atomically(Path(directory) / SETTINGS_FILE, data)


def valid_path(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_PATH and value.startswith("/") and "\x00" not in value)


def load_recent(directory: str | os.PathLike) -> list[str]:
    raw = _read(Path(directory) / RECENT_FILE)
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for value in raw:
        if valid_path(value) and value not in seen:
            seen.append(value)
    return seen[:MAX_RECENT]


def add_recent(recent: list[str], path: str) -> list[str]:
    """``recent`` with ``path`` first (once), cut to the ten most recent."""
    if not valid_path(path):
        return list(recent)
    return [path, *(other for other in recent if other != path)][:MAX_RECENT]


def save_recent(directory: str | os.PathLike, recent: list[str]) -> bool:
    paths = [path for path in recent if valid_path(path)][:MAX_RECENT]
    return write_atomically(Path(directory) / RECENT_FILE, json.dumps(paths, indent=1).encode("utf-8"))


def next_zoom(zoom: int, step: int) -> int:
    """The zoom one step up (+1) or down (-1) from ``zoom`` percent, within 10 to 500."""
    return max(ZOOM_STEPS[0], min(ZOOM_STEPS[-1], (zoom // 10 + step) * 10))
