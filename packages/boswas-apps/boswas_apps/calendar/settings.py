"""Calendar's settings (standard library only): what the settings page changes, kept in ``settings.json``."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

VIEWS = ("month", "week", "workweek", "day", "agenda")
VIEW_NAMES = {"month": "Month", "week": "Week", "workweek": "Work week", "day": "Day", "agenda": "Agenda"}
# Lead times offered for a reminder, in minutes before the start (None: no reminder).
REMINDER_CHOICES = (None, 0, 5, 10, 15, 30, 60, 120, 1440, 2880, 10080)
# For an all-day event the lead time counts from midnight: 9 in the morning of the day itself is -540.
ALL_DAY_REMINDER_CHOICES = (None, -540, 900, 2340, 9540)
LENGTH_CHOICES = (15, 30, 45, 60, 90, 120)


@dataclass
class Settings:
    week_start: int = -1               # -1: as the locale says; else 0 (Monday) ... 6 (Sunday)
    time_format: str = "locale"        # locale, 12 or 24
    default_reminder: int | None = 15  # minutes before; None: no reminder
    default_length: int = 60           # minutes
    work_start: int = 8                # hour the working day begins
    work_end: int = 17                 # hour it ends
    work_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    default_calendar: str = ""         # a calendar ID; empty: the first calendar
    first_view: str = "month"

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

        integer("week_start", -1, 6)
        integer("default_length", 5, 24 * 60)
        integer("work_start", 0, 23)
        integer("work_end", 1, 24)
        if settings.work_end <= settings.work_start:
            settings.work_start, settings.work_end = cls.work_start, cls.work_end
        if raw.get("time_format") in ("locale", "12", "24"):
            settings.time_format = raw["time_format"]
        if "default_reminder" in raw:
            value = raw["default_reminder"]
            if value is None:
                settings.default_reminder = None
            elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 8 * 7 * 24 * 60:
                settings.default_reminder = value
        days = raw.get("work_days")
        if isinstance(days, list):
            chosen = sorted({day for day in days if isinstance(day, int) and not isinstance(day, bool)
                             and 0 <= day <= 6})
            if chosen:
                settings.work_days = chosen
        if isinstance(raw.get("default_calendar"), str) and len(raw["default_calendar"]) <= 64:
            settings.default_calendar = raw["default_calendar"]
        if raw.get("first_view") in VIEWS:
            settings.first_view = raw["first_view"]
        return settings


FIELD_NAMES = tuple(item.name for item in fields(Settings))
