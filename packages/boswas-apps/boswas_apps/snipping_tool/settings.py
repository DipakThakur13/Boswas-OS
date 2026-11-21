"""The Snipping Tool's settings (standard library only), kept in ``settings.json`` of its configuration directory."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MODES = ("region", "screen")
DELAYS = (0, 3, 5, 10)
MAX_BYTES = 64 * 1024
FILE_NAME = "settings.json"


@dataclass
class Settings:
    mode: str = "region"            # what New takes: an area ("region") or the whole screen ("screen")
    delay: int = 0                  # seconds before a snip
    auto_copy: bool = True          # copy the picture to the clipboard after every change
    ask_to_save: bool = True        # ask before changes are thrown away (closing, a new snip, opening)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Any) -> "Settings":
        """Settings from parsed JSON; whatever is missing or wrong keeps its default."""
        settings = cls()
        if not isinstance(raw, dict):
            return settings
        if raw.get("mode") in MODES:
            settings.mode = raw["mode"]
        delay = raw.get("delay")
        if isinstance(delay, int) and not isinstance(delay, bool) and delay in DELAYS:
            settings.delay = delay
        for name in ("auto_copy", "ask_to_save"):
            if isinstance(raw.get(name), bool):
                setattr(settings, name, raw[name])
        return settings


def load(path: str | os.PathLike) -> Settings:
    """Read the settings; a file that is missing, too long or not valid gives the defaults."""
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    except OSError:
        return Settings()
    if len(data) > MAX_BYTES:
        return Settings()
    try:
        return Settings.from_json(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return Settings()


def write_atomic(path: str | os.PathLike, data: bytes, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` in one step: a temporary file in the same folder, then ``os.replace``."""
    target = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save(settings: Settings, path: str | os.PathLike) -> None:
    write_atomic(path, (json.dumps(settings.to_json(), indent=2, sort_keys=True) + "\n").encode("utf-8"))
