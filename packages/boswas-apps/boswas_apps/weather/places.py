"""The user's places, kept in ``places.json`` of Weather's configuration directory (standard library only).

    {"version": 1, "home": "2950159", "places": [{"id": 2950159, "name": "Berlin", "latitude": 52.52437, ...}]}

The order of ``places`` is the order of the navigation pane; ``home`` is the key of the place Weather
opens with (or null). Nothing is ever added without the user choosing it: there is no geolocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import storage
from .model import Place

VERSION = 1
MAX_PLACES = 50
MAX_BYTES = 256 * 1024
FILE_NAME = "places.json"


@dataclass
class PlaceList:
    places: list[Place] = field(default_factory=list)
    home: str | None = None

    def keys(self) -> list[str]:
        return [place.key for place in self.places]

    def index(self, key: str) -> int | None:
        return next((index for index, place in enumerate(self.places) if place.key == key), None)

    def get(self, key: str | None) -> Place | None:
        index = self.index(key) if key is not None else None
        return self.places[index] if index is not None else None

    def is_full(self) -> bool:
        return len(self.places) >= MAX_PLACES

    def add(self, place: Place) -> bool:
        """Add a place at the end; False when it is there already or the list is full."""
        if self.index(place.key) is not None or self.is_full():
            return False
        self.places.append(place)
        return True

    def insert(self, position: int, place: Place) -> bool:
        """Put a place back at a position (taking a removal back)."""
        if self.index(place.key) is not None or self.is_full():
            return False
        self.places.insert(max(0, min(position, len(self.places))), place)
        return True

    def remove(self, key: str) -> tuple[int, Place] | None:
        """Remove a place; (its position, the place) or None. The home place stops being home."""
        index = self.index(key)
        if index is None:
            return None
        place = self.places.pop(index)
        if self.home == key:
            self.home = None
        return index, place

    def move(self, key: str, step: int) -> bool:
        """Move a place ``step`` positions (negative: up); False when it cannot move."""
        index = self.index(key)
        if index is None:
            return False
        target = max(0, min(len(self.places) - 1, index + step))
        if target == index:
            return False
        self.places.insert(target, self.places.pop(index))
        return True

    def set_home(self, key: str | None) -> bool:
        if key is not None and self.index(key) is None:
            return False
        self.home = key
        return True

    def home_place(self) -> Place | None:
        return self.get(self.home)

    def to_json(self) -> dict:
        return {"version": VERSION, "home": self.home, "places": [place.to_json() for place in self.places]}

    @classmethod
    def from_json(cls, raw: Any) -> "PlaceList":
        """The places of a document; what is not a valid place is skipped, the rest is kept."""
        result = cls()
        if not isinstance(raw, dict):
            return result
        entries = raw.get("places")
        if isinstance(entries, list):
            for entry in entries[:MAX_PLACES * 2]:
                place = Place.from_json(entry)
                if place is not None:
                    result.add(place)
        home = raw.get("home")
        if isinstance(home, str) and result.index(home) is not None:
            result.home = home
        return result


def load(path: Path) -> tuple[PlaceList, str]:
    """(the places, a problem to tell the user or ""). A damaged file is set aside and Weather starts empty."""
    try:
        raw = storage.read_json(path, MAX_BYTES)
    except storage.StoreError as exc:
        aside = storage.set_aside(path)
        where = f" It was kept as {aside.name}." if aside is not None else ""
        return PlaceList(), f"Your places could not be read: {exc}.{where}"
    if raw is None:
        return PlaceList(), ""
    if not isinstance(raw, dict) or not isinstance(raw.get("places"), list):
        aside = storage.set_aside(path)
        where = f" It was kept as {aside.name}." if aside is not None else ""
        return PlaceList(), f"Your places could not be read: {path.name} is not a list of places.{where}"
    return PlaceList.from_json(raw), ""


def save(path: Path, places: PlaceList | dict) -> None:
    """Write the places atomically (a document from ``to_json`` may be given, made on another thread)."""
    storage.write_json_atomic(path, places.to_json() if isinstance(places, PlaceList) else places)
