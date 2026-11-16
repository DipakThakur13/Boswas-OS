"""Clock's settings (standard library only), kept in ``settings.json`` of its configuration directory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from . import model


@dataclass
class Settings:
    time_format: str = "locale"        # locale, 12 or 24
    session_minutes: int = 30          # the length the focus page starts with
    focus_minutes: int = 25            # the focus period aimed for
    break_minutes: int = 5
    skip_breaks: bool = False
    default_snooze: int = model.DEFAULT_SNOOZE
    alarm_sound: bool = True           # the reminder service plays a sound with alarms and timers

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Any) -> "Settings":
        settings = cls()
        if not isinstance(raw, dict):
            return settings

        def integer(name: str, low: int, high: int) -> None:
            value = raw.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
                setattr(settings, name, value)

        if raw.get("time_format") in ("locale", "12", "24"):
            settings.time_format = raw["time_format"]
        integer("session_minutes", model.FOCUS_MIN_TOTAL, model.FOCUS_MAX_TOTAL)
        integer("focus_minutes", 10, 120)
        integer("break_minutes", 1, 60)
        integer("default_snooze", 1, 120)
        for name in ("skip_breaks", "alarm_sound"):
            if isinstance(raw.get(name), bool):
                setattr(settings, name, raw[name])
        return settings
