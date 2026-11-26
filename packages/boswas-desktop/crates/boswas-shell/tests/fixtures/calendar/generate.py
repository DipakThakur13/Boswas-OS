#!/usr/bin/env python3
"""Writes calendar.json and expected.json next to this file (run it again to renew them; never edit them by hand).

calendar.json is a document in the Calendar application's own format, with
events of many recurrence rules, zones, all-day events, exceptions, extra
dates and a moved occurrence. expected.json is what Calendar's own model
(boswas_apps/calendar/model.py) says happens in one week of it, seen from
Europe/Berlin. The shell's widgets board reads the same document with its
own port of the rules; agenda::tests compares the two.

    python3 -B packages/boswas-desktop/crates/boswas-shell/tests/fixtures/calendar/generate.py
"""
from __future__ import annotations

import datetime as dt
import json
import random
import sys
import zoneinfo
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[5] / "boswas-apps"))
from boswas_apps.calendar import model  # noqa: E402

RULES = (
    "FREQ=DAILY", "FREQ=DAILY;INTERVAL=3", "FREQ=DAILY;COUNT=10", "FREQ=WEEKLY", "FREQ=WEEKLY;BYDAY=MO,WE,FR",
    "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH", "FREQ=WEEKLY;BYDAY=SA,SU;COUNT=6", "FREQ=MONTHLY", "FREQ=MONTHLY;BYMONTHDAY=-1",
    "FREQ=MONTHLY;BYDAY=2TU", "FREQ=MONTHLY;BYDAY=-1FR", "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1",
    "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1", "FREQ=YEARLY", "FREQ=YEARLY;BYMONTH=12;BYDAY=4TH",
    "FREQ=YEARLY;BYYEARDAY=1,100,200,300", "FREQ=YEARLY;BYWEEKNO=48;BYDAY=MO", "FREQ=DAILY;UNTIL=20261201",
    "FREQ=WEEKLY;UNTIL=20261205T120000Z", "FREQ=HOURLY;INTERVAL=7;COUNT=40", "FREQ=DAILY;BYHOUR=8,20",
    "FREQ=MINUTELY;INTERVAL=90;BYHOUR=10,11,12", "FREQ=MONTHLY;BYMONTHDAY=31", "FREQ=MONTHLY;INTERVAL=2;BYDAY=1MO,3MO",
)
ZONES = ("", "", "Europe/Berlin", "Asia/Kolkata", "America/New_York", "UTC", "Australia/Sydney")
LOCAL = "Europe/Berlin"


def main() -> None:
    rng = random.Random(7)
    events = []
    base = dt.datetime(2026, 11, 20)
    for n in range(80):
        start = base - dt.timedelta(days=rng.randint(0, 900)) + dt.timedelta(minutes=15 * rng.randint(0, 95))
        all_day = rng.random() < 0.2
        length = dt.timedelta(minutes=rng.choice([15, 30, 60, 90, 240]))
        event = {"uid": f"e{n}@fixture", "calendar": rng.choice(["a", "b"]), "summary": f"Event {n}", "all_day": all_day}
        if all_day:
            event["start"] = start.date().isoformat()
            event["end"] = (start.date() + dt.timedelta(days=rng.choice([1, 1, 2, 3]))).isoformat()
        else:
            event["start"] = start.isoformat(timespec="seconds")
            event["end"] = (start + length).isoformat(timespec="seconds")
            event["tz"] = rng.choice(ZONES)
        if rng.random() < 0.8:
            event["rrule"] = rng.choice(RULES)
            if rng.random() < 0.3:
                day = dt.datetime(2026, 11, 26 + rng.randint(0, 4), start.hour, start.minute)
                event["exdates"] = [day.date().isoformat() if all_day else day.isoformat(timespec="seconds")]
                extra = day + dt.timedelta(days=2, hours=1)
                event["rdates"] = [extra.date().isoformat() if all_day else extra.isoformat(timespec="seconds")]
        events.append(event)
    events.append({"uid": "daily@fixture", "calendar": "a", "summary": "Daily", "all_day": False,
                   "start": "2026-11-01T09:00:00", "end": "2026-11-01T09:30:00", "tz": "Europe/Berlin",
                   "rrule": "FREQ=DAILY"})
    events.append({"uid": "daily@fixture", "calendar": "a", "summary": "Daily (moved)", "all_day": False,
                   "start": "2026-11-28T15:00:00", "end": "2026-11-28T15:30:00", "tz": "Europe/Berlin",
                   "recurrence_id": "2026-11-28T09:00:00"})
    document = {"version": 1,
                "calendars": [{"id": "a", "name": "A", "color": "iris"}, {"id": "b", "name": "B", "color": "mint"}],
                "events": events}
    (HERE / "calendar.json").write_text(json.dumps(document, indent=0) + "\n", encoding="utf-8")
    data = model.CalendarData.from_json(document)
    local = zoneinfo.ZoneInfo(LOCAL)
    start = int(dt.datetime(2026, 11, 26, tzinfo=local).timestamp())
    end = int(dt.datetime(2026, 12, 3, tzinfo=local).timestamp())
    found = [{"summary": o.event.summary or "(No title)", "start": int(o.start_epoch), "end": int(o.end_epoch),
              "all_day": o.all_day} for o in data.occurrences(start, end, local)]
    expected = {"local": LOCAL, "start": start, "end": end, "occurrences": found}
    (HERE / "expected.json").write_text(json.dumps(expected, indent=0) + "\n", encoding="utf-8")
    print(f"{len(events)} events, {len(found)} occurrences between {start} and {end}")


if __name__ == "__main__":
    main()
