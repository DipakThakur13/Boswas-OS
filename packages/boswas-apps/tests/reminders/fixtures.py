"""Stand-ins and builders shared by the tests of the reminder service."""

from __future__ import annotations

import datetime as dt

from boswas_apps.calendar import model as calendar_model
from boswas_apps.clock import model as clock_model
from boswas_apps.reminders import timeutil

BERLIN = timeutil.zone("Europe/Berlin")
NEW_YORK = timeutil.zone("America/New_York")


def at(*args, zone=BERLIN) -> float:
    """The instant of a wall time in Berlin (or ``zone``)."""
    return timeutil.to_epoch(dt.datetime(*args), zone)


class FakeClock:
    """The clocks of a machine that the test moves by hand."""

    def __init__(self, wall: float, boot: float = 1000.0, boot_id: str = "boot-1") -> None:
        self.wall, self.boot, self.boot_id = wall, boot, boot_id

    def __call__(self) -> clock_model.Now:
        return clock_model.Now(self.wall, self.boot, self.boot_id)

    def advance(self, seconds: float) -> clock_model.Now:
        """Time passes (the machine may be asleep: the boot clock counts that too)."""
        self.wall += seconds
        self.boot += seconds
        return self()

    def set_wall(self, wall: float) -> clock_model.Now:
        """Somebody sets the wall clock; no time passes."""
        self.wall = wall
        return self()

    def reboot(self, off_seconds: float) -> clock_model.Now:
        self.wall += off_seconds
        self.boot = 5.0
        self.boot_id = self.boot_id + "+"
        return self()


def calendar_with(*events: dict, hidden: bool = False) -> calendar_model.CalendarData:
    data = calendar_model.CalendarData.from_json({
        "calendars": [{"id": "c1", "name": "Personal", "color": "iris", "visible": not hidden}],
        "events": [{"calendar": "c1", **event} for event in events]})
    return data


def event(uid: str, start: str, end: str, **fields) -> dict:
    return {"uid": uid, "start": start, "end": end, "summary": fields.pop("summary", uid), **fields}
