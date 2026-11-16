"""Time zones and local times (standard library only: zoneinfo).

Everything that schedules works with two kinds of values: instants
(seconds since the epoch, UTC) and local wall times (naive ``datetime``)
that belong to a time zone. ``to_epoch`` turns the second into the first
by the rules of RFC 5545: a local time that does not exist (the hour
skipped when summer time begins) is read with the UTC offset from before
the gap, so 02:30 becomes 03:30; a local time that happens twice (when
summer time ends) means the first of the two.

``system_zone`` is the time zone of the machine: ``$TZ``, the target of
``/etc/localtime``, ``/etc/timezone``; when none names a zone of the
time-zone database, the fixed offset the C library reports.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
import zoneinfo
from functools import lru_cache

UTC = dt.timezone.utc
EPOCH = dt.datetime(1970, 1, 1)
_ZONE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+\-]*(/[A-Za-z0-9][A-Za-z0-9_+\-]*){0,3}$")
_LOCALTIME = "/etc/localtime"
_TIMEZONE_FILE = "/etc/timezone"


def valid_zone_name(name: object) -> bool:
    """Whether ``name`` looks like a key of the time-zone database (it is not looked up)."""
    return isinstance(name, str) and len(name) <= 64 and bool(_ZONE_NAME.match(name)) and ".." not in name


@lru_cache(maxsize=256)
def zone(name: str) -> zoneinfo.ZoneInfo | None:
    """The zone ``name`` of the time-zone database, or None when there is none of that name."""
    if not valid_zone_name(name):
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None


@lru_cache(maxsize=1)
def zone_names() -> tuple[str, ...]:
    """Every zone name of the time-zone database on this system, sorted."""
    try:
        names = zoneinfo.available_timezones()
    except Exception:                                   # a damaged tzdata installation
        names = set()
    return tuple(sorted(name for name in names if valid_zone_name(name) and zone(name) is not None))


def _name_from_tz_variable(value: str) -> str | None:
    value = value.lstrip(":")
    marker = "zoneinfo/"
    if value.startswith("/") and marker in value:
        value = value.split(marker, 1)[1]
    return value if zone(value) is not None else None


def system_zone_name() -> str | None:
    """The name of the machine's time zone in the time-zone database, when it can be told."""
    variable = os.environ.get("TZ")
    if variable:
        return _name_from_tz_variable(variable)
    try:
        target = os.path.realpath(_LOCALTIME)
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        name = target.split("zoneinfo/", 1)[1]
        for prefix in ("posix/", "right/"):
            if name.startswith(prefix):
                name = name[len(prefix):]
        if zone(name) is not None:
            return name
    try:
        with open(_TIMEZONE_FILE, encoding="ascii", errors="replace") as handle:
            name = handle.readline(128).strip()
    except OSError:
        name = ""
    if name and zone(name) is not None:
        return name
    if not os.path.exists(_LOCALTIME):
        return "UTC"
    return None


def system_zone() -> dt.tzinfo:
    """The machine's time zone: a ZoneInfo when its name is known, else the current fixed offset."""
    name = system_zone_name()
    if name is not None:
        found = zone(name)
        if found is not None:
            return found
    try:
        time.tzset()
        return dt.timezone(dt.timedelta(seconds=time.localtime().tm_gmtoff))
    except (AttributeError, OSError, ValueError):
        return UTC


def zone_key(tz: dt.tzinfo) -> str:
    """A name for ``tz``: the database key of a ZoneInfo, ``UTC``, or ``+HH:MM`` for a fixed offset."""
    key = getattr(tz, "key", None)
    if isinstance(key, str):
        return key
    offset = tz.utcoffset(None)
    if offset is None or not offset:
        return "UTC"
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def to_epoch(local: dt.datetime, tz: dt.tzinfo) -> float:
    """The instant of the wall time ``local`` (naive) in ``tz``, by the rules in the module text."""
    aware = local.replace(tzinfo=tz, fold=0)
    offset = aware.utcoffset() or dt.timedelta(0)
    return (local - EPOCH - offset).total_seconds()


def from_epoch(instant: float, tz: dt.tzinfo) -> dt.datetime:
    """The wall time (naive) in ``tz`` at ``instant``."""
    return to_aware(instant, tz).replace(tzinfo=None)


def to_aware(instant: float, tz: dt.tzinfo) -> dt.datetime:
    """``instant`` as an aware datetime in ``tz`` (clamped to the years datetime can hold)."""
    try:
        return dt.datetime.fromtimestamp(instant, tz)
    except (OverflowError, OSError, ValueError):
        return dt.datetime.max.replace(tzinfo=tz) if instant > 0 else dt.datetime.min.replace(tzinfo=tz)


def utc_offset(instant: float, tz: dt.tzinfo) -> int:
    """Seconds east of UTC in ``tz`` at ``instant``."""
    offset = to_aware(instant, tz).utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def exists(local: dt.datetime, tz: dt.tzinfo) -> bool:
    """False for a wall time inside the gap at the start of summer time."""
    return from_epoch(to_epoch(local, tz), tz) == local


def is_ambiguous(local: dt.datetime, tz: dt.tzinfo) -> bool:
    """True for a wall time that happens twice (the hour repeated at the end of summer time)."""
    first = local.replace(tzinfo=tz, fold=0).utcoffset()
    second = local.replace(tzinfo=tz, fold=1).utcoffset()
    return first != second and exists(local, tz)
