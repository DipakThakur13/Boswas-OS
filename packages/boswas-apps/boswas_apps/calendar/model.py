"""Calendar's data: calendars, events, their occurrences and the edits on them (standard library only).

The store is one JSON document, ``calendar.json``:

    {"version": 1,
     "calendars": [{"id", "name", "color": a colour family of the Boswas palette, "visible"}],
     "events":    [{"uid", "calendar", "summary", "location", "description", "all_day",
                    "start", "end", "tz", "rrule", "rdates", "exdates", "recurrence_id",
                    "reminders": [minutes before the start], "created", "modified", "sequence", "extra"}]}

Times. ``start`` and ``end`` are local wall times in the zone ``tz``
(``2026-11-17T09:00:00``), or dates for an all-day event (``2026-11-17``;
``end`` is the day after the last day, as in iCalendar). ``tz`` is a name of
the time-zone database, ``UTC``, or empty for a *floating* time: the same
wall time wherever the computer is. All-day events are always floating.

Recurrence follows iCalendar. A recurring event (the *master*) has an
``rrule`` and/or ``rdates``; ``exdates`` lists the starts of occurrences
that were removed. A changed occurrence is a second event with the same
``uid`` whose ``recurrence_id`` is the start the occurrence had in the
series (an *override*). ``edit_occurrence`` and ``delete_occurrence``
implement "this event", "this and following events" and "all events" with
exactly these means: an override or an EXDATE; an UNTIL on the old series
plus a new series; a change of the master.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Iterable

from ..reminders import timeutil
from . import recurrence

VERSION = 1
COLORS = ("iris", "azure", "mint", "sun", "coral", "sky", "orchid", "slate")
MAX_CALENDARS = 64
MAX_EVENTS = 200_000
MAX_SUMMARY = 500
MAX_LOCATION = 500
MAX_DESCRIPTION = 64_000
MAX_REMINDERS = 8
MAX_REMINDER_MINUTES = 60 * 24 * 7 * 8          # eight weeks
MAX_DATES = 2000                                # RDATE / EXDATE entries kept per event
MAX_EXTRA_LINES = 64
MAX_EXTRA_LINE = 2000
MAX_OCCURRENCES = 20_000                        # per query
SCOPES = ("this", "following", "all")
Key = tuple[str, str]                           # (uid, recurrence_id)
DAY = dt.timedelta(days=1)


class ModelError(ValueError):
    """An edit that cannot be made; the message is fit for the user."""


def now_stamp() -> int:
    return int(dt.datetime.now(timeutil.UTC).timestamp())


def new_uid() -> str:
    return f"{uuid.uuid4()}@boswas-calendar"


# --- local times as text ---------------------------------------------------------------------------------

def parse_local(text: str) -> dt.date | dt.datetime:
    """``2026-11-17`` -> date, ``2026-11-17T09:00:00`` -> naive datetime. Raises ValueError."""
    if not isinstance(text, str) or len(text) > 19:
        raise ValueError("not a local time")
    if len(text) == 10:
        return dt.date.fromisoformat(text)
    value = dt.datetime.fromisoformat(text)
    if value.tzinfo is not None:
        raise ValueError("not a local time")
    return value.replace(microsecond=0)


def format_local(value: dt.date | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        return value.replace(microsecond=0, tzinfo=None).isoformat(timespec="seconds")
    return value.isoformat()


def as_datetime(value: dt.date | dt.datetime) -> dt.datetime:
    """A date as midnight; a datetime unchanged."""
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime(value.year, value.month, value.day)


@lru_cache(maxsize=4096)
def _parsed_rule(text: str) -> recurrence.Rule | None:
    try:
        return recurrence.parse_rrule(text)
    except recurrence.RecurrenceError:
        return None


def fold(text: str) -> str:
    """Lower case without accents, for searching."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


# --- calendars and events --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Calendar:
    id: str
    name: str
    color: str = "iris"
    visible: bool = True

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "color": self.color, "visible": self.visible}


@dataclass(frozen=True)
class Event:
    uid: str
    calendar: str
    summary: str = ""
    location: str = ""
    description: str = ""
    all_day: bool = False
    start: str = ""
    end: str = ""
    tz: str = ""
    rrule: str = ""
    rdates: tuple[str, ...] = ()
    exdates: tuple[str, ...] = ()
    recurrence_id: str = ""
    reminders: tuple[int, ...] = ()
    created: int = 0
    modified: int = 0
    sequence: int = 0
    extra: tuple[str, ...] = field(default=(), compare=True)

    @property
    def key(self) -> Key:
        return self.uid, self.recurrence_id

    @property
    def recurring(self) -> bool:
        return bool(self.rrule or self.rdates)

    @property
    def start_value(self) -> dt.date | dt.datetime:
        return parse_local(self.start)

    @property
    def end_value(self) -> dt.date | dt.datetime:
        return parse_local(self.end)

    @property
    def start_dt(self) -> dt.datetime:
        return as_datetime(parse_local(self.start))

    @property
    def end_dt(self) -> dt.datetime:
        return as_datetime(parse_local(self.end))

    @property
    def duration(self) -> dt.timedelta:
        return self.end_dt - self.start_dt

    def rule(self) -> recurrence.Rule | None:
        return _parsed_rule(self.rrule) if self.rrule else None

    def zone(self, local: dt.tzinfo) -> dt.tzinfo:
        """The zone the event's wall times are in: its own, or ``local`` for floating and all-day events."""
        if self.all_day or not self.tz:
            return local
        return timeutil.zone(self.tz) or local

    def title(self) -> str:
        return self.summary or "(No title)"

    def to_json(self) -> dict:
        data: dict[str, Any] = {"uid": self.uid, "calendar": self.calendar, "summary": self.summary,
                                "all_day": self.all_day, "start": self.start, "end": self.end}
        for name in ("location", "description", "tz", "rrule", "recurrence_id"):
            if getattr(self, name):
                data[name] = getattr(self, name)
        for name in ("rdates", "exdates", "reminders", "extra"):
            if getattr(self, name):
                data[name] = list(getattr(self, name))
        for name in ("created", "modified", "sequence"):
            if getattr(self, name):
                data[name] = getattr(self, name)
        return data


def _clean(value: Any, limit: int, *, lines: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\0", "").replace("\r\n", "\n").replace("\r", "\n")
    if not lines:
        value = " ".join(value.split())
    return value[:limit]


def _stamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return 0
    return int(max(0, min(value, 253402300799)))


def _local_list(values: Any, all_day: bool) -> tuple[str, ...]:
    found: list[str] = []
    for value in (values if isinstance(values, (list, tuple)) else [])[:MAX_DATES]:
        try:
            parsed = parse_local(value)
        except (ValueError, TypeError):
            continue
        if all_day:
            parsed = parsed.date() if isinstance(parsed, dt.datetime) else parsed
        else:
            parsed = as_datetime(parsed)
        found.append(format_local(parsed))
    return tuple(sorted(dict.fromkeys(found)))


def clean_reminders(values: Any) -> tuple[int, ...]:
    found = []
    for value in (values if isinstance(values, (list, tuple)) else []):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            continue
        minutes = int(value)
        if -MAX_REMINDER_MINUTES <= minutes <= MAX_REMINDER_MINUTES:
            found.append(minutes)
    return tuple(sorted(dict.fromkeys(found), reverse=True))[:MAX_REMINDERS]


def make_event(raw: Any, calendars: set[str] | None = None) -> Event | None:
    """A valid Event from untrusted data, or None when it has no usable identity or times.

    Texts are cut to their limits, an end before the start becomes the
    start (one day later for an all-day event), an unknown time zone makes
    the event floating, a rule that cannot be parsed is dropped.
    """
    if not isinstance(raw, dict):
        return None
    uid = raw.get("uid")
    if not isinstance(uid, str) or not uid.strip() or len(uid) > 512 or any(ord(c) < 0x20 for c in uid):
        return None
    calendar = raw.get("calendar")
    if not isinstance(calendar, str) or (calendars is not None and calendar not in calendars):
        return None
    try:
        start = parse_local(raw.get("start"))
        end = parse_local(raw.get("end")) if raw.get("end") else start
    except (ValueError, TypeError):
        return None
    all_day = raw.get("all_day") is True
    if all_day:
        start = start.date() if isinstance(start, dt.datetime) else start
        end = end.date() if isinstance(end, dt.datetime) else end
        if end <= start:
            try:
                end = start + DAY
            except OverflowError:
                return None
    else:
        start, end = as_datetime(start), as_datetime(end)
        if end < start:
            end = start
    tz = raw.get("tz") if isinstance(raw.get("tz"), str) else ""
    if all_day or (tz and timeutil.zone(tz) is None):
        tz = ""
    rrule = raw.get("rrule") if isinstance(raw.get("rrule"), str) else ""
    if rrule:
        rule = _parsed_rule(rrule.strip())
        rrule = recurrence.format_rrule(rule) if rule is not None else ""
    recurrence_id = ""
    if raw.get("recurrence_id"):
        try:
            recurrence_id = format_local(parse_local(raw.get("recurrence_id")))      # in the master's terms
        except (ValueError, TypeError):
            return None
        rrule = ""
    extra = tuple(line[:MAX_EXTRA_LINE] for line in (raw.get("extra") if isinstance(raw.get("extra"), list) else [])
                  if isinstance(line, str) and line and "\n" not in line and "\r" not in line)[:MAX_EXTRA_LINES]
    sequence = raw.get("sequence")
    return Event(
        uid=uid.strip(), calendar=calendar, summary=_clean(raw.get("summary"), MAX_SUMMARY),
        location=_clean(raw.get("location"), MAX_LOCATION),
        description=_clean(raw.get("description"), MAX_DESCRIPTION, lines=True),
        all_day=all_day, start=format_local(start), end=format_local(end), tz=tz, rrule=rrule,
        rdates=() if recurrence_id else _local_list(raw.get("rdates"), all_day),
        exdates=() if recurrence_id else _local_list(raw.get("exdates"), all_day),
        recurrence_id=recurrence_id, reminders=clean_reminders(raw.get("reminders")),
        created=_stamp(raw.get("created")), modified=_stamp(raw.get("modified")),
        sequence=int(sequence) if isinstance(sequence, int) and not isinstance(sequence, bool)
        and 0 <= sequence < 2 ** 31 else 0,
        extra=extra)


def make_calendar(raw: Any) -> Calendar | None:
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not 0 < len(identifier) <= 64 or not all(
            c.isalnum() or c in "-_" for c in identifier):
        return None
    name = _clean(raw.get("name"), 80) or "Calendar"
    color = raw.get("color") if raw.get("color") in COLORS else COLORS[0]
    return Calendar(identifier, name, color, raw.get("visible") is not False)


@dataclass(frozen=True)
class Occurrence:
    """One event on the calendar: a single event, or one instance of a series."""
    event: Event               # where the texts and reminders come from (the override, if there is one)
    master: Event              # the series it belongs to (the event itself when it does not repeat)
    start: dt.datetime         # wall time in the display zone
    end: dt.datetime
    start_epoch: float
    end_epoch: float
    recurrence_id: str         # the start this instance has in the series, in the master's terms; "" if single

    @property
    def all_day(self) -> bool:
        return self.event.all_day

    @property
    def recurring(self) -> bool:
        return self.master.recurring

    @property
    def is_override(self) -> bool:
        return bool(self.event.recurrence_id)

    @property
    def key(self) -> tuple[str, str]:
        return self.master.uid, self.recurrence_id

    @property
    def last_day(self) -> dt.date:
        """The last day the occurrence touches (an event that ends at midnight ends the day before)."""
        if self.end > self.start and self.end.time() == dt.time(0):
            return (self.end - DAY).date()
        return self.end.date()

    @property
    def multi_day(self) -> bool:
        return self.all_day or self.last_day > self.start.date()


@dataclass(frozen=True)
class Change:
    """An edit that can be undone: the events as they were and as they are now (None: no such event)."""
    label: str
    before: tuple[tuple[Key, Event | None], ...]
    after: tuple[tuple[Key, Event | None], ...]

    def reversed(self) -> "Change":
        return Change(self.label, self.after, self.before)


@dataclass(frozen=True)
class ReminderDue:
    when: float                # the instant the reminder fires
    minutes: int               # its lead time
    occurrence: Occurrence


class CalendarData:
    """The calendars and events in memory, with the queries the views need."""

    def __init__(self) -> None:
        self.calendars: list[Calendar] = []
        self._events: dict[Key, Event] = {}
        self._overrides: dict[str, dict[str, Event]] = {}
        self.revision = 0              # changes with every edit; views key their caches on it

    # --- file ---
    @classmethod
    def from_json(cls, raw: Any) -> "CalendarData":
        data = cls()
        if isinstance(raw, dict):
            seen = set()
            for entry in (raw.get("calendars") if isinstance(raw.get("calendars"), list) else []):
                calendar = make_calendar(entry)
                if calendar is not None and calendar.id not in seen and len(data.calendars) < MAX_CALENDARS:
                    seen.add(calendar.id)
                    data.calendars.append(calendar)
            for entry in (raw.get("events") if isinstance(raw.get("events"), list) else [])[:MAX_EVENTS]:
                event = make_event(entry, seen)
                if event is not None:
                    data._put(event)
        return data

    def to_json(self) -> dict:
        return {"version": VERSION, "calendars": [calendar.to_json() for calendar in self.calendars],
                "events": [event.to_json() for event in self._events.values()]}

    def ensure_default(self) -> bool:
        """Make sure there is at least one calendar; True when one was created."""
        if self.calendars:
            return False
        self.calendars.append(Calendar("personal", "Personal", "iris"))
        self.revision += 1
        return True

    # --- events ---
    def _put(self, event: Event) -> None:
        self._events[event.key] = event
        if event.recurrence_id:
            self._overrides.setdefault(event.uid, {})[event.recurrence_id] = event

    def _drop(self, key: Key) -> None:
        event = self._events.pop(key, None)
        if event is not None and event.recurrence_id:
            group = self._overrides.get(event.uid)
            if group is not None:
                group.pop(event.recurrence_id, None)
                if not group:
                    del self._overrides[event.uid]

    def __len__(self) -> int:
        return len(self._events)

    def events(self) -> Iterable[Event]:
        return self._events.values()

    def get(self, uid: str, recurrence_id: str = "") -> Event | None:
        return self._events.get((uid, recurrence_id))

    def overrides(self, uid: str) -> dict[str, Event]:
        return dict(self._overrides.get(uid, {}))

    def calendar(self, identifier: str) -> Calendar | None:
        for calendar in self.calendars:
            if calendar.id == identifier:
                return calendar
        return None

    def visible_ids(self) -> set[str]:
        return {calendar.id for calendar in self.calendars if calendar.visible}

    def apply(self, change: Change) -> None:
        """Make the events what ``change.after`` says (``change.reversed()`` undoes it)."""
        for key, event in change.after:
            self._drop(key)
            if event is not None:
                self._put(event)
        self.revision += 1

    def _change(self, label: str, updates: dict[Key, Event | None]) -> Change:
        before = tuple((key, self._events.get(key)) for key in updates)
        change = Change(label, before, tuple(updates.items()))
        self.apply(change)
        return change

    # --- calendars ---
    def add_calendar(self, name: str, color: str = "") -> Calendar:
        if len(self.calendars) >= MAX_CALENDARS:
            raise ModelError(f"There can be at most {MAX_CALENDARS} calendars.")
        used = {calendar.color for calendar in self.calendars}
        if color not in COLORS:
            color = next((candidate for candidate in COLORS if candidate not in used), COLORS[len(self.calendars)
                                                                                         % len(COLORS)])
        calendar = Calendar(uuid.uuid4().hex[:12], _clean(name, 80) or "Calendar", color)
        self.calendars.append(calendar)
        self.revision += 1
        return calendar

    def update_calendar(self, identifier: str, **fields: Any) -> Calendar:
        for index, calendar in enumerate(self.calendars):
            if calendar.id == identifier:
                if "name" in fields:
                    fields["name"] = _clean(fields["name"], 80) or calendar.name
                if "color" in fields and fields["color"] not in COLORS:
                    del fields["color"]
                self.calendars[index] = replace(calendar, **fields)
                self.revision += 1
                return self.calendars[index]
        raise ModelError("That calendar no longer exists.")

    def remove_calendar(self, identifier: str) -> tuple[Calendar, list[Event]]:
        """Remove a calendar and its events; returns them (to offer an undo). The last calendar stays."""
        calendar = self.calendar(identifier)
        if calendar is None:
            raise ModelError("That calendar no longer exists.")
        if len(self.calendars) <= 1:
            raise ModelError("The last calendar cannot be deleted.")
        removed = [event for event in self._events.values() if event.calendar == identifier]
        for event in removed:
            self._drop(event.key)
        self.calendars.remove(calendar)
        self.revision += 1
        return calendar, removed

    def restore_calendar(self, calendar: Calendar, events: list[Event], position: int | None = None) -> None:
        if self.calendar(calendar.id) is None:
            self.calendars.insert(len(self.calendars) if position is None else position, calendar)
        for event in events:
            self._put(event)
        self.revision += 1

    def count_events(self, identifier: str) -> int:
        return sum(1 for event in self._events.values() if event.calendar == identifier and not event.recurrence_id)

    # --- occurrences ---
    def _expand(self, master: Event, start: float, end: float, local: dt.tzinfo, by_start: bool,
                limit: int) -> list[Occurrence]:
        zone = master.zone(local)
        first = master.start_dt
        duration = master.duration
        overrides = self._overrides.get(master.uid, {})
        found: list[Occurrence] = []

        def emit(event: Event, moment: dt.datetime, length: dt.timedelta, tz: dt.tzinfo, rid: str) -> None:
            try:
                finish = moment + length
            except OverflowError:
                return
            begin_epoch = timeutil.to_epoch(moment, tz)
            end_epoch = timeutil.to_epoch(finish, tz)
            if by_start:
                if not start < begin_epoch <= end:
                    return
            elif not (begin_epoch < end and (end_epoch > start or (end_epoch == begin_epoch and begin_epoch >= start))):
                return
            if event.all_day:
                shown_start, shown_end = moment, finish
            else:
                shown_start, shown_end = timeutil.from_epoch(begin_epoch, local), timeutil.from_epoch(end_epoch, local)
            found.append(Occurrence(event, master, shown_start, shown_end, begin_epoch, end_epoch, rid))

        if not master.recurring:
            emit(master, first, duration, zone, "")
        else:
            as_date = master.all_day
            exdates = set(master.exdates)
            try:
                low = timeutil.from_epoch(start, zone) - duration - DAY
                high = timeutil.from_epoch(end, zone) + DAY
            except OverflowError:
                low, high = dt.datetime.min, dt.datetime.max
            moments: list[dt.datetime] = []
            rule = master.rule()
            if rule is not None:
                moments = recurrence.between(rule, first, low, high, limit=limit,
                                             instant=lambda moment: timeutil.to_epoch(moment, zone))
            elif low <= first < high:
                moments = [first]
            if master.rdates:
                extra = [as_datetime(parse_local(text)) for text in master.rdates]
                moments = sorted(set(moments) | {moment for moment in extra if low <= moment < high})
            for moment in moments:
                rid = format_local(moment.date() if as_date else moment)
                if rid in exdates or rid in overrides:
                    continue
                emit(master, moment, duration, zone, rid)
        for rid, override in overrides.items():
            emit(override, override.start_dt, override.duration, override.zone(local), rid)
        return found

    def occurrences(self, start: float, end: float, local: dt.tzinfo, *, calendars: set[str] | None = None,
                    by_start: bool = False, limit: int = MAX_OCCURRENCES) -> list[Occurrence]:
        """What happens between the instants ``start`` and ``end``, in order.

        An occurrence is included when it overlaps the window, or with
        ``by_start`` when it starts in (``start``, ``end``]. ``calendars`` limits
        the result to those calendar IDs (default: all).
        """
        found: list[Occurrence] = []
        for event in self._events.values():
            if event.recurrence_id:
                if (event.uid, "") in self._events:
                    continue                      # shown through its master
                master = event                    # an override whose series is not here stands alone
            else:
                master = event
            if calendars is not None and master.calendar not in calendars:
                continue
            if master.recurrence_id:
                found.extend(self._single(master, start, end, local, by_start))
            else:
                found.extend(self._expand(master, start, end, local, by_start, limit))
            if len(found) >= limit:
                break
        found.sort(key=lambda occ: (occ.start_epoch, not occ.all_day, -occ.end_epoch, fold(occ.event.summary),
                                    occ.event.uid))
        return found[:limit]

    def _single(self, event: Event, start: float, end: float, local: dt.tzinfo, by_start: bool) -> list[Occurrence]:
        zone = event.zone(local)
        begin_epoch = timeutil.to_epoch(event.start_dt, zone)
        end_epoch = timeutil.to_epoch(event.end_dt, zone)
        if by_start:
            if not start < begin_epoch <= end:
                return []
        elif not (begin_epoch < end and (end_epoch > start or (end_epoch == begin_epoch and begin_epoch >= start))):
            return []
        if event.all_day:
            shown = event.start_dt, event.end_dt
        else:
            shown = timeutil.from_epoch(begin_epoch, local), timeutil.from_epoch(end_epoch, local)
        return [Occurrence(event, event, shown[0], shown[1], begin_epoch, end_epoch, "")]

    def occurrence(self, uid: str, recurrence_id: str, local: dt.tzinfo) -> Occurrence | None:
        """The occurrence of the series ``uid`` that has ``recurrence_id`` (``""``: the event, or its first)."""
        master = self._events.get((uid, ""))
        if master is None:
            return None
        if not recurrence_id:
            if not master.recurring:
                found = self._single(master, float("-inf"), float("inf"), local, False)
                return found[0] if found else None
            return self.first_occurrence(master, local)
        try:
            moment = as_datetime(parse_local(recurrence_id))
        except ValueError:
            return None
        override = self._overrides.get(uid, {}).get(recurrence_id)
        if override is not None:
            zone = override.zone(local)
            begin, finish = timeutil.to_epoch(override.start_dt, zone), timeutil.to_epoch(override.end_dt, zone)
            shown = (override.start_dt, override.end_dt) if override.all_day else (
                timeutil.from_epoch(begin, local), timeutil.from_epoch(finish, local))
            return Occurrence(override, master, shown[0], shown[1], begin, finish, recurrence_id)
        zone = master.zone(local)
        begin = timeutil.to_epoch(moment, zone)
        for occ in self._expand(master, begin - 1, begin + 1, local, False, 16):
            if occ.recurrence_id == recurrence_id:
                return occ
        return None

    def first_occurrence(self, master: Event, local: dt.tzinfo, after: float | None = None) -> Occurrence | None:
        """The first occurrence of a series (that ends after the instant ``after``, when given)."""
        zone = master.zone(local)
        begin = timeutil.to_epoch(master.start_dt, zone) if after is None else after
        span = 86400.0 * 32
        for _ in range(12):                      # look further and further ahead: 32 days ... about 350 years
            found = self._expand(master, begin - (1 if after is None else 0), begin + span, local, False, 8)
            found.sort(key=lambda occ: occ.start_epoch)
            if found:
                return found[0]
            span *= 4
        return None

    def search(self, query: str, local: dt.tzinfo, now: float, limit: int = 500) -> list[Occurrence]:
        """Events whose title, place or notes contain every word of ``query``.

        One result per event: its next occurrence (its first one when the
        series is over), in order of time.
        """
        words = [word for word in fold(query).split() if word]
        if not words:
            return []
        found: list[Occurrence] = []
        for event in self._events.values():
            text = fold(" ".join((event.summary, event.location, event.description)))
            if not all(word in text for word in words):
                continue
            master = self._events.get((event.uid, ""), event)
            occurrence = None
            if event.recurrence_id or not master.recurring:
                single = self._single(event, float("-inf"), float("inf"), local, False)
                if single:
                    occurrence = replace(single[0], master=master, recurrence_id=event.recurrence_id)
            else:
                occurrence = self.first_occurrence(master, local, now) or self.first_occurrence(master, local)
            if occurrence is not None:
                found.append(occurrence)
            if len(found) >= limit:
                break
        found.sort(key=lambda occ: (occ.start_epoch, fold(occ.event.summary)))
        return found

    def reminders_between(self, after: float, until: float, local: dt.tzinfo, *, limit: int = 2000
                          ) -> list[ReminderDue]:
        """The reminders that fire in (``after``, ``until``], oldest first; events of hidden calendars have none."""
        visible = self.visible_ids()
        due: list[ReminderDue] = []
        for event in self._events.values():
            if event.calendar not in visible or (event.recurrence_id and (event.uid, "") in self._events):
                continue
            offsets = set(event.reminders)
            for override in self._overrides.get(event.uid, {}).values():
                offsets.update(override.reminders)
            if not offsets:
                continue
            low = after + min(offsets) * 60
            high = until + max(offsets) * 60
            if event.recurrence_id:
                occurrences = self._single(event, low, high, local, True)
            else:
                occurrences = self._expand(event, low, high, local, True, limit)
            for occurrence in occurrences:
                for minutes in occurrence.event.reminders:
                    when = occurrence.start_epoch - minutes * 60
                    if after < when <= until:
                        due.append(ReminderDue(when, minutes, occurrence))
            if len(due) >= limit * 4:
                break
        due.sort(key=lambda item: (item.when, item.occurrence.start_epoch, item.occurrence.event.uid))
        return due[:limit]

    # --- edits ---
    def add_event(self, event: Event, label: str = "Add event") -> Change:
        if event.key in self._events:
            raise ModelError("An event with this identity already exists.")
        if self.calendar(event.calendar) is None:
            raise ModelError("That calendar no longer exists.")
        if len(self._events) >= MAX_EVENTS:
            raise ModelError("The calendar is full.")
        return self._change(label, {event.key: _touched(event, created=True)})

    def add_events(self, events: list[Event], label: str = "Import events") -> Change:
        """Add events, replacing those that have the same UID (and recurrence ID). Used by the import."""
        updates: dict[Key, Event | None] = {}
        fresh = {event.uid for event in events if not event.recurrence_id}
        for uid in fresh:
            # A re-imported series replaces the old one whole, changed occurrences included.
            for rid in self._overrides.get(uid, {}):
                updates[(uid, rid)] = None
        for event in events:
            updates[event.key] = event
        if len(self._events) + len(updates) > MAX_EVENTS:
            raise ModelError("The calendar is full.")
        return self._change(label, updates)

    def replace_event(self, event: Event, label: str = "Edit event") -> Change:
        """Store a changed single event (or a changed master as it is, with its exceptions untouched)."""
        if event.key not in self._events:
            raise ModelError("That event no longer exists.")
        return self._change(label, {event.key: _touched(event, self._events[event.key])})

    def _series_keys(self, uid: str) -> list[Key]:
        return [(uid, "")] + [(uid, rid) for rid in self._overrides.get(uid, {})]

    def delete_occurrence(self, occurrence: Occurrence, scope: str = "all") -> Change:
        """Delete one event, or of a series: this occurrence, this and the following, or all."""
        master = self._events.get((occurrence.master.uid, ""))
        if master is None:
            # An override that stands alone.
            if occurrence.event.key not in self._events:
                raise ModelError("That event no longer exists.")
            return self._change("Delete event", {occurrence.event.key: None})
        rid = occurrence.recurrence_id
        if scope == "all" or not master.recurring or not rid:
            return self._change("Delete event", {key: None for key in self._series_keys(master.uid)})
        if scope == "this":
            updates: dict[Key, Event | None] = {
                master.key: _touched(replace(master, exdates=tuple(sorted({*master.exdates, rid}))), master)}
            if (master.uid, rid) in self._events:
                updates[(master.uid, rid)] = None
            return self._change("Delete occurrence", updates)
        if scope == "following":
            if not self._has_earlier(master, rid):
                return self._change("Delete event", {key: None for key in self._series_keys(master.uid)})
            updates = self._truncate(master, rid)
            return self._change("Delete this and following events", updates)
        raise ModelError(f"unknown scope {scope!r}")

    def _has_earlier(self, master: Event, rid: str) -> bool:
        """Whether the series has an occurrence before the one that started at ``rid``."""
        moment = as_datetime(parse_local(rid))
        if master.start_dt < moment:
            return True
        return any(as_datetime(parse_local(text)) < moment for text in master.rdates)

    def _truncate(self, master: Event, rid: str) -> dict[Key, Event | None]:
        """The updates that end a series just before the occurrence ``rid``."""
        moment = as_datetime(parse_local(rid))
        updates: dict[Key, Event | None] = {}
        rrule = master.rrule
        rule = master.rule()
        if rule is not None:
            if master.all_day:
                last = moment - DAY
                until, is_date = dt.datetime(last.year, last.month, last.day, 23, 59, 59), True
            elif master.tz:
                zone = timeutil.zone(master.tz) or timeutil.UTC
                instant = timeutil.to_epoch(moment, zone) - 1
                until, is_date = dt.datetime.fromtimestamp(instant, timeutil.UTC), False
            else:
                until, is_date = moment - dt.timedelta(seconds=1), False
            rrule = recurrence.format_rrule(replace(rule, until=until, until_is_date=is_date, count=None))
        kept = lambda text: as_datetime(parse_local(text)) < moment  # noqa: E731
        updates[master.key] = _touched(replace(master, rrule=rrule,
                                               rdates=tuple(text for text in master.rdates if kept(text)),
                                               exdates=tuple(text for text in master.exdates if kept(text))), master)
        for other in self._overrides.get(master.uid, {}):
            if not kept(other):
                updates[(master.uid, other)] = None
        return updates

    def edit_occurrence(self, occurrence: Occurrence, edited: Event, scope: str = "all") -> Change:
        """Store ``edited`` (the event as the editor shows it for this occurrence).

        ``edited.start`` and ``edited.end`` are the times of this occurrence.
        ``scope`` (for an occurrence of a series): ``this`` makes it an
        exception of the series; ``following`` ends the series before it and
        starts a new one with it; ``all`` changes the series, moving every
        occurrence by as much as this one was moved.
        """
        master = self._events.get((occurrence.master.uid, ""))
        if self.calendar(edited.calendar) is None:
            raise ModelError("That calendar no longer exists.")
        if master is None or not master.recurring or not occurrence.recurrence_id:
            current = self._events.get(occurrence.event.key)
            if current is None:
                raise ModelError("That event no longer exists.")
            stored = replace(edited, uid=current.uid, recurrence_id=current.recurrence_id,
                             exdates=current.exdates if edited.rrule == current.rrule else (),
                             rdates=current.rdates if edited.rrule == current.rrule else (),
                             created=current.created, extra=current.extra)
            if stored.recurrence_id:
                stored = replace(stored, rrule="", rdates=(), exdates=())
            return self._change("Edit event", {current.key: _touched(stored, current)})
        rid = occurrence.recurrence_id
        if scope == "this":
            existing = self._events.get((master.uid, rid))
            override = replace(edited, uid=master.uid, recurrence_id=rid, rrule="", rdates=(), exdates=(),
                               calendar=master.calendar, created=(existing or master).created,
                               extra=(existing.extra if existing else ()))
            return self._change("Edit occurrence", {override.key: _touched(override, existing)})
        original = as_datetime(parse_local(rid))
        if master.all_day != edited.all_day or master.tz != edited.tz:
            shift = None                          # the series changes its nature: exceptions cannot be kept
        else:
            base = occurrence.event if occurrence.is_override else None
            was = base.start_dt if base is not None else original
            shift = edited.start_dt - was
        if scope == "all":
            return self._change("Edit series", self._edit_all(master, edited, shift))
        if scope == "following":
            if not self._has_earlier(master, rid):
                return self._change("Edit series", self._edit_all(master, edited, shift))
            return self._change("Edit this and following events",
                                self._edit_following(master, edited, rid, original, shift))
        raise ModelError(f"unknown scope {scope!r}")

    def _shifted(self, texts: Iterable[str], shift: dt.timedelta, all_day: bool) -> tuple[str, ...]:
        moved = []
        for text in texts:
            try:
                moment = as_datetime(parse_local(text)) + shift
            except (OverflowError, ValueError):
                continue
            moved.append(format_local(moment.date() if all_day else moment))
        return tuple(sorted(dict.fromkeys(moved)))

    def _edit_all(self, master: Event, edited: Event, shift: dt.timedelta | None) -> dict[Key, Event | None]:
        updates: dict[Key, Event | None] = {}
        same_rule = edited.rrule == master.rrule
        keep = shift is not None and same_rule
        duration = edited.duration
        if shift is None:
            start = dt.datetime.combine(master.start_dt.date(), edited.start_dt.time())
        else:
            start = master.start_dt + shift
        end = start + duration
        as_date = edited.all_day
        new_master = replace(
            edited, uid=master.uid, recurrence_id="", created=master.created, extra=master.extra,
            start=format_local(start.date() if as_date else start), end=format_local(end.date() if as_date else end),
            rdates=self._shifted(master.rdates, shift, as_date) if keep and shift is not None else (),
            exdates=self._shifted(master.exdates, shift, as_date) if keep and shift is not None else ())
        updates[master.key] = _touched(new_master, master)
        for rid, override in self._overrides.get(master.uid, {}).items():
            updates[(master.uid, rid)] = None
            if keep and shift is not None:
                moved_id = self._shifted([rid], shift, as_date)
                if moved_id:
                    moved = replace(override, recurrence_id=moved_id[0], calendar=new_master.calendar)
                    updates[moved.key] = moved
        return updates

    def _edit_following(self, master: Event, edited: Event, rid: str, original: dt.datetime,
                        shift: dt.timedelta | None) -> dict[Key, Event | None]:
        updates = self._truncate(master, rid)
        uid = new_uid()
        rrule = edited.rrule
        rule = _parsed_rule(rrule) if rrule else None
        old_rule = master.rule()
        if rule is not None and rule.count is not None and old_rule is not None and edited.rrule == master.rrule:
            zone = master.zone(timeutil.UTC)
            gone = recurrence.count_before(old_rule, master.start_dt, original,
                                           instant=lambda moment: timeutil.to_epoch(moment, zone))
            rrule = recurrence.format_rrule(replace(rule, count=max(1, rule.count - gone)))
        keep = shift is not None and edited.rrule == master.rrule
        as_date = edited.all_day
        later = lambda text: as_datetime(parse_local(text)) >= original  # noqa: E731
        series = replace(
            edited, uid=uid, recurrence_id="", rrule=rrule, created=now_stamp(), sequence=0, extra=master.extra,
            rdates=self._shifted((t for t in master.rdates if later(t) and t != rid), shift, as_date)
            if keep and shift is not None else (),
            exdates=self._shifted((t for t in master.exdates if later(t)), shift, as_date)
            if keep and shift is not None else ())
        updates[series.key] = _touched(series, created=True)
        if keep and shift is not None:
            for other, override in self._overrides.get(master.uid, {}).items():
                if later(other) and other != rid:
                    moved_id = self._shifted([other], shift, as_date)
                    if moved_id:
                        moved = replace(override, uid=uid, recurrence_id=moved_id[0], calendar=series.calendar)
                        updates[moved.key] = moved
        return updates

    def move_occurrence(self, occurrence: Occurrence, start: dt.datetime, end: dt.datetime) -> Change:
        """Give one occurrence new times (wall times in the event's own zone). A series gets an exception."""
        event = occurrence.event
        as_date = event.all_day
        edited = replace(event, start=format_local(start.date() if as_date else start),
                         end=format_local(end.date() if as_date else end))
        if occurrence.recurring and occurrence.recurrence_id and not occurrence.is_override:
            edited = replace(edited, rrule="", rdates=(), exdates=())
        return self.edit_occurrence(occurrence, edited, "this")


def _touched(event: Event, previous: Event | None = None, *, created: bool = False) -> Event:
    """``event`` with its modification time set to now (and a higher sequence number than ``previous``)."""
    stamp = now_stamp()
    sequence = previous.sequence + 1 if previous is not None else event.sequence
    return replace(event, modified=stamp, created=event.created or (stamp if created or previous is None else 0),
                   sequence=sequence)


def editable(occurrence: Occurrence) -> Event:
    """The event as an editor shows it for ``occurrence``: with the times of this occurrence, in the
    event's own zone. This is what ``edit_occurrence`` expects back, changed."""
    event = occurrence.event
    if occurrence.is_override or not occurrence.recurrence_id:
        return event
    start = as_datetime(parse_local(occurrence.recurrence_id))
    end = start + event.duration
    as_date = event.all_day
    return replace(event, start=format_local(start.date() if as_date else start),
                   end=format_local(end.date() if as_date else end))


def event_from_times(calendar: str, start: dt.date | dt.datetime, end: dt.date | dt.datetime, *,
                     summary: str = "", tz: str = "", reminders: tuple[int, ...] = ()) -> Event:
    """A new event (with a new UID). Dates make an all-day event; ``end`` is exclusive."""
    all_day = not isinstance(start, dt.datetime)
    event = make_event({"uid": new_uid(), "calendar": calendar, "summary": summary, "all_day": all_day,
                        "start": format_local(start), "end": format_local(end), "tz": "" if all_day else tz,
                        "reminders": list(reminders)})
    if event is None:
        raise ModelError("These are not valid times for an event.")
    return event


def local_to_event_zone(moment: dt.datetime, event: Event, local: dt.tzinfo) -> dt.datetime:
    """A wall time of the display zone as a wall time of the event's zone."""
    zone = event.zone(local)
    if zone is local:
        return moment
    return timeutil.from_epoch(timeutil.to_epoch(moment, local), zone)
