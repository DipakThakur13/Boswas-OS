"""Weather's settings, kept in ``settings.json`` of its configuration directory (standard library only)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import model, storage

FILE_NAME = "settings.json"
TIME_FORMATS = ("locale", "12", "24")
REFRESH_CHOICES = (0, 15, 30, 60)          # minutes; 0 is off


@dataclass
class Settings:
    temperature: str = "c"                 # model.TEMPERATURE_UNITS
    wind: str = "kmh"                      # model.WIND_UNITS
    precipitation: str = "mm"              # model.PRECIPITATION_UNITS
    pressure: str = "hpa"                  # model.PRESSURE_UNITS
    time_format: str = "locale"            # locale, 12 or 24
    refresh_minutes: int = 30

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def imperial(cls) -> "Settings":
        """The units of the United States, for a first start in a locale that uses them."""
        return cls(temperature="f", wind="mph", precipitation="in", pressure="inhg")

    @classmethod
    def from_json(cls, raw: Any, base: "Settings | None" = None) -> "Settings":
        """Settings from a document; what is missing or not a valid choice keeps the value of ``base``."""
        settings = cls(**asdict(base)) if base is not None else cls()
        if not isinstance(raw, dict):
            return settings
        for name, choices in (("temperature", model.TEMPERATURE_UNITS), ("wind", model.WIND_UNITS),
                              ("precipitation", model.PRECIPITATION_UNITS), ("pressure", model.PRESSURE_UNITS),
                              ("time_format", TIME_FORMATS)):
            value = raw.get(name)
            if isinstance(value, str) and value in choices:
                setattr(settings, name, value)
        minutes = raw.get("refresh_minutes")
        if not isinstance(minutes, bool) and isinstance(minutes, int) and minutes in REFRESH_CHOICES:
            settings.refresh_minutes = minutes
        return settings


def load(path: Path, default: Settings | None = None) -> tuple[Settings, bool]:
    """(the settings, whether the file existed). A missing or damaged file gives ``default``."""
    base = default if default is not None else Settings()
    try:
        raw = storage.read_json(path, 64 * 1024)
    except storage.StoreError:
        return Settings.from_json(None, base), True
    return Settings.from_json(raw, base), raw is not None


def save(path: Path, settings: Settings | dict) -> None:
    storage.write_json_atomic(path, settings.to_json() if isinstance(settings, Settings) else settings)
