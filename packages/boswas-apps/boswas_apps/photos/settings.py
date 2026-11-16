"""What the user chose in Photos, kept as JSON in the configuration directory (standard library only).

Every value read from the file is checked: a value of the wrong type or
out of range is replaced by the default, unknown keys are dropped, and a
file that is missing, too large or no JSON gives the defaults. Saving is
atomic.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .fileutil import atomic_write

MAX_BYTES = 256 * 1024
MAX_FOLDERS = 64
WHEEL_MODES = ("zoom", "navigate")
INTERVAL_RANGE = (1, 120)
QUALITY_RANGE = (10, 100)


@dataclass
class Settings:
    include_pictures: bool = True              # the user's Pictures folder is part of the gallery
    folders: list[str] = field(default_factory=list)      # more folders of the gallery
    slideshow_interval: int = 5                # seconds a picture stays
    slideshow_loop: bool = True
    slideshow_shuffle: bool = False
    wheel: str = "zoom"                        # the mouse wheel zooms, or goes to the next picture
    confirm_delete: bool = True
    new_window: bool = False                   # a picture opened from outside gets its own window
    filmstrip: bool = True
    jpeg_quality: int = 90

    def gallery_roots(self, pictures: str) -> list[str]:
        roots = [pictures] if self.include_pictures else []
        return roots + [folder for folder in self.folders if folder not in roots]


def _clean(raw: object) -> Settings:
    settings = Settings()
    if not isinstance(raw, dict):
        return settings
    for item in fields(Settings):
        value = raw.get(item.name)
        default = getattr(settings, item.name)
        if isinstance(default, bool):
            if isinstance(value, bool):
                setattr(settings, item.name, value)
        elif isinstance(default, int):
            if isinstance(value, int) and not isinstance(value, bool):
                low, high = INTERVAL_RANGE if item.name == "slideshow_interval" else QUALITY_RANGE
                setattr(settings, item.name, max(low, min(high, value)))
        elif item.name == "wheel":
            if value in WHEEL_MODES:
                settings.wheel = value
        elif item.name == "folders":
            if isinstance(value, list):
                folders: list[str] = []
                for folder in value[:MAX_FOLDERS]:
                    if (isinstance(folder, str) and folder.startswith("/") and "\x00" not in folder
                            and len(folder) < 4096 and folder not in folders):
                        folders.append(os.path.normpath(folder))
                settings.folders = folders
    return settings


def load(path: Path) -> Settings:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        return Settings()
    if len(raw) > MAX_BYTES:
        return Settings()
    try:
        return _clean(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return Settings()


def save(settings: Settings, path: Path) -> None:
    """Write the settings; OSError when the directory cannot be written."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(asdict(_clean(asdict(settings))), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write(path, data, mode=0o600)
