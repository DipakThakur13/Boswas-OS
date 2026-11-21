"""Recorded-style answers of Open-Meteo (``data/``) and a stand-in for the network (no Qt).

The answers were written by hand after the documented format of the geocoding and forecast APIs:
Berlin on Saturday 21 November 2026 at 14:15 local time, Sydney the morning after, two searches and a
search that found nothing. They hold what real answers hold: units, nulls in arrays (a temperature,
a weather code, the chance of rain after day 8), fields the code does not ask for, a ``current``
without ``visibility`` and ``uv_index`` (Sydney).
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.parse
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
# 13:20 UTC: 14:20 in Berlin, the time of the Berlin answer's current conditions.
NOW = dt.datetime(2026, 11, 21, 13, 20, tzinfo=dt.timezone.utc).timestamp()


def raw(name: str) -> bytes:
    return (DATA / name).read_bytes()


def answer(name: str):
    return json.loads(raw(name).decode("utf-8"))


def place(name: str = "berlin"):
    """The first place of a recorded search: Berlin (Germany) or Sydney (Australia)."""
    from boswas_apps.weather import model
    return model.parse_places(answer(f"search-{name}.json"))[0]


class FakeNetwork:
    """``fetch(url) -> bytes`` for WeatherService: answers from ``data/``, failures on demand.

    ``urls`` records every request. ``error`` (an exception) is raised instead of answering.
    ``hold()`` makes requests wait until ``release()``, to see that they do not overlap.
    """

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.error: Exception | None = None
        self.overrides: dict[str, bytes] = {}         # "search" / "forecast" -> body
        self._gate = threading.Event()
        self._gate.set()
        self._lock = threading.Lock()
        self.active = 0
        self.most_active = 0

    def hold(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def count(self, kind: str) -> int:
        host = "geocoding-api" if kind == "search" else "api.open-meteo.com/v1/forecast"
        return sum(host in url for url in self.urls)

    def __call__(self, url: str) -> bytes:
        with self._lock:
            self.urls.append(url)
            self.active += 1
            self.most_active = max(self.most_active, self.active)
        try:
            self._gate.wait(10)
            if self.error is not None:
                raise self.error
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            if "geocoding-api" in url:
                if "search" in self.overrides:
                    return self.overrides["search"]
                name = query.get("name", "").casefold()
                if name.startswith("berl"):
                    return raw("search-berlin.json")
                if name.startswith("sydn"):
                    return raw("search-sydney.json")
                return raw("search-nothing.json")
            if "forecast" in self.overrides:
                return self.overrides["forecast"]
            return raw("forecast-sydney.json" if float(query["latitude"]) < 0 else "forecast-berlin.json")
        finally:
            with self._lock:
                self.active -= 1
