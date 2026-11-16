"""What the user chose: settings and pinned folders, kept as JSON (no Qt).

Stored in ``$XDG_CONFIG_HOME/com.boswas.Files/settings.json``. The file is
read with care (wrong types fall back to the defaults, unknown keys are
dropped) and written atomically.
"""

from __future__ import annotations

import json
import os

VIEW_MODES = ("details", "list", "tiles", "icons-small", "icons-medium", "icons-large", "icons-xlarge")
VIEW_LABELS = {"details": "Details", "list": "List", "tiles": "Tiles", "icons-small": "Small icons",
               "icons-medium": "Medium icons", "icons-large": "Large icons", "icons-xlarge": "Extra large icons"}
SORT_COLUMNS = ("name", "date", "type", "size")
_MAX_FILE = 1024 * 1024
_MAX_PINNED = 200

DEFAULTS: dict[str, object] = {
    "show_hidden": False,
    "single_click": False,
    "confirm_trash": False,
    "show_extensions": True,
    "view_mode": "details",             # one view for every folder (the choice is global, not per folder)
    "sort_column": "name",
    "sort_descending": False,
    "start_folder": "home",             # "home", "computer" or an absolute path
    "details_pane": False,
    "pinned": [],
    "navigation_width": 240,
    "details_width": 300,
    "column_widths": {},
}


class Settings:
    """The settings as attributes of ``values``; ``set`` validates, stores and saves."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.values: dict[str, object] = json.loads(json.dumps(DEFAULTS))
        self.error = ""
        self.load()

    @staticmethod
    def _valid(key: str, value: object) -> bool:
        if key not in DEFAULTS:
            return False
        if key == "view_mode":
            return value in VIEW_MODES
        if key == "sort_column":
            return value in SORT_COLUMNS
        if key == "start_folder":
            return isinstance(value, str) and (value in ("home", "computer") or value.startswith("/")) \
                and "\0" not in value and len(value) < 4096
        if key == "pinned":
            return isinstance(value, list) and len(value) <= _MAX_PINNED and all(
                isinstance(item, str) and item.startswith("/") and "\0" not in item and len(item) < 4096
                for item in value)
        if key in ("navigation_width", "details_width"):
            return isinstance(value, int) and not isinstance(value, bool) and 120 <= value <= 1200
        if key == "column_widths":
            return isinstance(value, dict) and len(value) <= 32 and all(
                isinstance(name, str) and isinstance(width, int) and not isinstance(width, bool)
                and 20 <= width <= 4000 for name, width in value.items())
        return isinstance(value, bool)

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.loads(handle.read(_MAX_FILE))
        except FileNotFoundError:
            return
        except (OSError, ValueError, RecursionError) as error:
            self.error = str(error)
            return
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if self._valid(key, value):
                self.values[key] = value

    def get(self, key: str):
        return self.values[key]

    def set(self, key: str, value: object) -> bool:
        """Store a value and save. False (and nothing changes) when the value is not valid for the key."""
        if not self._valid(key, value):
            return False
        if self.values.get(key) != value:
            self.values[key] = value
            self.save()
        return True

    def save(self) -> None:
        if not self.path:
            return
        partial = f"{self.path}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump(self.values, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(partial, self.path)
            self.error = ""
        except OSError as error:
            self.error = str(error)
            try:
                os.unlink(partial)
            except OSError:
                pass

    # --- pinned folders ---
    def pinned(self) -> list[str]:
        return list(self.values["pinned"])          # type: ignore[arg-type]

    def pin(self, path: str) -> bool:
        pinned = self.pinned()
        if path in pinned or not path.startswith("/"):
            return False
        return self.set("pinned", pinned + [path])

    def unpin(self, path: str) -> bool:
        pinned = self.pinned()
        if path not in pinned:
            return False
        pinned.remove(path)
        return self.set("pinned", pinned)

    def move_pin(self, path: str, position: int) -> bool:
        """Move a pinned folder to ``position`` in the list (clamped)."""
        pinned = self.pinned()
        if path not in pinned:
            return False
        pinned.remove(path)
        pinned.insert(max(0, min(position, len(pinned))), path)
        return self.set("pinned", pinned)
