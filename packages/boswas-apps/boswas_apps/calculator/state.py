"""What the calculator remembers between runs: the last mode and the choices of each page (no Qt).

One small JSON file, ``state.json`` in the application's state directory.
It is read with a size cap and every value is checked against what the
program itself could have written; anything else is ignored, so a damaged
or foreign file gives the defaults. Writing replaces the file atomically.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import numeric, units
from .engine import MODES, RADIXES, WORD_SIZES

FILE_NAME = "state.json"
MAX_BYTES = 64 * 1024
DATE = "date"
SHIFT_MODES = ("arithmetic", "logical", "rotate", "rotate_carry")
DATE_MODES = ("difference", "shift")
PAGES = (*MODES, DATE, *(category.key for category in units.CATEGORIES))


def defaults() -> dict:
    return {
        "page": MODES[0],
        "angle": numeric.DEG,
        "radix": 10,
        "bits": 64,
        "shift": SHIFT_MODES[0],
        "date_mode": DATE_MODES[0],
        "units": {category.key: [category.default_from, category.default_to] for category in units.CATEGORIES},
    }


def validated(raw: object) -> dict:
    """``raw`` (parsed JSON of unknown origin) reduced to a valid state; missing or bad entries are defaults."""
    state = defaults()
    if not isinstance(raw, dict):
        return state
    if raw.get("page") in PAGES:
        state["page"] = raw["page"]
    if raw.get("angle") in numeric.ANGLE_UNITS:
        state["angle"] = raw["angle"]
    if raw.get("radix") in RADIXES and not isinstance(raw.get("radix"), bool):
        state["radix"] = raw["radix"]
    if raw.get("bits") in WORD_SIZES and not isinstance(raw.get("bits"), bool):
        state["bits"] = raw["bits"]
    if raw.get("shift") in SHIFT_MODES:
        state["shift"] = raw["shift"]
    if raw.get("date_mode") in DATE_MODES:
        state["date_mode"] = raw["date_mode"]
    chosen = raw.get("units")
    if isinstance(chosen, dict):
        for category in units.CATEGORIES:
            pair = chosen.get(category.key)
            keys = category.keys()
            if (isinstance(pair, list) and len(pair) == 2 and all(isinstance(key, str) for key in pair)
                    and pair[0] in keys and pair[1] in keys):
                state["units"][category.key] = list(pair)
    return state


def load(directory: str | os.PathLike) -> dict:
    """The remembered state from ``directory`` (the defaults when there is none or it cannot be read)."""
    path = Path(directory) / FILE_NAME
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        return defaults()
    if len(data) > MAX_BYTES:
        return defaults()
    try:
        return validated(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return defaults()


def save(directory: str | os.PathLike, state: dict) -> bool:
    """Write the state; True when the file was replaced. A failure leaves the old file as it was."""
    target = Path(directory) / FILE_NAME
    data = json.dumps(validated(state), indent=1, sort_keys=True).encode("utf-8")
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=str(target.parent))
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return True
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False
