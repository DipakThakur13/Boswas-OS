"""The user's settings: a small JSON file in the application's configuration directory.

Standard library only. A file that is missing, damaged or holds values
this version does not know gives the defaults for those values; saving
writes a temporary file and renames it.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass

from . import PAGES

# Seconds between two samples; None: only when the user asks (F5).
REFRESH = {"high": 0.5, "normal": 1.0, "low": 4.0, "paused": None}
REFRESH_TITLES = {"high": "High (every 0.5 s)", "normal": "Normal (every second)", "low": "Low (every 4 s)",
                  "paused": "Paused"}
THEMES = {"desktop": "Use the desktop setting", "light": "Light", "dark": "Dark"}
START_PAGES = tuple(page for page in PAGES if page != "settings")
CPU_VIEWS = ("overall", "cores")
_MAX_BYTES = 64 * 1024


@dataclass
class Settings:
    refresh: str = "normal"
    start_page: str = "processes"
    full_command_line: bool = False
    theme: str = "desktop"
    group_by_type: bool = True
    cpu_view: str = "overall"

    def interval(self) -> float | None:
        return REFRESH.get(self.refresh, 1.0)

    def validated(self) -> "Settings":
        """A copy in which every value this version does not know is the default."""
        defaults = Settings()
        return Settings(
            self.refresh if self.refresh in REFRESH else defaults.refresh,
            self.start_page if self.start_page in START_PAGES else defaults.start_page,
            self.full_command_line if isinstance(self.full_command_line, bool) else defaults.full_command_line,
            self.theme if self.theme in THEMES else defaults.theme,
            self.group_by_type if isinstance(self.group_by_type, bool) else defaults.group_by_type,
            self.cpu_view if self.cpu_view in CPU_VIEWS else defaults.cpu_view,
        )


def load(path: str | os.PathLike) -> Settings:
    """The settings stored at ``path``; defaults for whatever cannot be read."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            return Settings()
        values = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(values, dict):
        return Settings()
    defaults = asdict(Settings())
    known = {}
    for key, default in defaults.items():
        value = values.get(key, default)
        known[key] = value if isinstance(value, type(default)) else default
    return Settings(**known).validated()


def save(path: str | os.PathLike, settings: Settings) -> None:
    """Write the settings. Raises OSError when the file cannot be written."""
    directory = os.path.dirname(os.fspath(path)) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(settings.validated()), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
