"""What is due when: the scheduling of the reminder service (standard library only; no Qt, no I/O but the state file).

``Engine.poll(now)`` answers, for one reading of the clocks, which alarms,
timers, focus periods and calendar reminders have become due since the
last poll, and sorts them into two lists:

  ring    shown now, each with its own notification: everything that is on
          time, alarms and timers up to ten minutes late, and reminders of
          calendar events that have not ended yet
  missed  everything older, which the service sums up in one notification
          ("3 missed reminders") instead of ringing for each

``Engine.next_due(now)`` is the instant the service has to wake up at next.

Nothing fires twice, nothing is lost:

* Every firing has a key (an alarm and its ring time, a timer and the
  count of its starts, an event's occurrence and the reminder's lead
  time). Fired keys are remembered in the state file for a few days, so a
  wall clock that is set back does not repeat them.
* ``checked_until`` is the instant up to which everything was handled. A
  poll looks at the window from there to now, however long the machine was
  off, asleep, or the clock was moved forward (at most two weeks back).
* Alarms only count from the moment they were switched on, timers and
  focus sessions are measured on the boot clock (see ``clock.model``), and a
  reminder is not reported for an event that was created or changed after
  it had already begun.
* A snoozed item is kept in the state file with its new time until it
  fires; an alarm that was shown but not answered when the service stopped
  is brought back when it starts again.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable

from ..clock import model as clock_model
from . import jsonfile

STATE_VERSION = 1
GRACE = 600.0                        # seconds late and still shown as if on time
MAX_CATCH_UP = 14 * 86400.0          # how far back a poll looks at most
HORIZON = 6 * 3600.0                 # how far ahead calendar reminders are worked out at once
FIRED_KEEP = 3 * 86400.0
MAX_FIRED = 5000
MAX_SNOOZED = 200
CLOCK_STEP_BACK = 2.0                # a wall clock this many seconds behind the last poll was set back
EVENT_SNOOZE_MINUTES = 10
KINDS = ("alarm", "timer", "focus", "event")


@dataclass(frozen=True)
class Item:
    """One thing that is due."""
    key: str
    kind: str                         # alarm, timer, focus or event
    source: str                       # the alarm's or timer's ID, the event's UID
    when: float                       # the instant it is (or was) due
    title: str
    detail: dict = field(default_factory=dict, compare=False)
    late: float = field(default=0.0, compare=False)      # seconds between ``when`` and the poll that found it
    snoozed: bool = field(default=False, compare=False)  # it comes back from a snooze

    def to_json(self) -> dict:
        return {"key": self.key, "kind": self.kind, "source": self.source, "when": self.when, "title": self.title,
                "detail": self.detail}

    @classmethod
    def from_json(cls, raw: Any) -> "Item | None":
        if not isinstance(raw, dict):
            return None
        key, kind, source, when, title = (raw.get(name) for name in ("key", "kind", "source", "when", "title"))
        if not (isinstance(key, str) and 0 < len(key) <= 700 and kind in KINDS and isinstance(source, str)
                and isinstance(when, (int, float)) and not isinstance(when, bool) and when == when
                and isinstance(title, str)):
            return None
        detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
        return cls(key, kind, source[:512], float(when), title[:500], _plain(detail))


def _plain(value: Any, depth: int = 0) -> Any:
    """JSON data reduced to what an Item's detail may hold: short strings, numbers, booleans, small dicts."""
    if isinstance(value, dict) and depth < 2:
        return {str(key)[:40]: _plain(item, depth + 1) for key, item in list(value.items())[:24]
                if isinstance(key, str)}
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if value == value and abs(value) < 1e15 else 0
    if isinstance(value, str):
        return value[:500]
    return None


class State:
    """What the service remembers between runs (``state.json``)."""

    def __init__(self) -> None:
        self.checked_until: float | None = None
        self.fired: dict[str, float] = {}
        self.snoozed: dict[str, dict] = {}          # key -> {"due": instant, "item": Item as JSON}
        self.ringing: dict[str, dict] = {}          # key -> Item as JSON: shown, not answered yet
        self.dirty = False

    @classmethod
    def from_json(cls, raw: Any) -> "State":
        state = cls()
        if not isinstance(raw, dict):
            return state
        checked = raw.get("checked_until")
        if isinstance(checked, (int, float)) and not isinstance(checked, bool) and checked == checked and checked > 0:
            state.checked_until = float(checked)
        if isinstance(raw.get("fired"), dict):
            for key, when in list(raw["fired"].items())[:MAX_FIRED]:
                if isinstance(key, str) and len(key) <= 700 and isinstance(when, (int, float)) \
                        and not isinstance(when, bool) and when == when:
                    state.fired[key] = float(when)
        if isinstance(raw.get("snoozed"), dict):
            for key, entry in list(raw["snoozed"].items())[:MAX_SNOOZED]:
                item = Item.from_json(entry.get("item")) if isinstance(entry, dict) else None
                due = entry.get("due") if isinstance(entry, dict) else None
                if item is not None and item.key == key and isinstance(due, (int, float)) \
                        and not isinstance(due, bool) and due == due:
                    state.snoozed[key] = {"due": float(due), "item": item.to_json()}
        if isinstance(raw.get("ringing"), dict):
            for key, entry in list(raw["ringing"].items())[:MAX_SNOOZED]:
                item = Item.from_json(entry)
                if item is not None and item.key == key:
                    state.ringing[key] = item.to_json()
        return state

    def to_json(self) -> dict:
        return {"version": STATE_VERSION, "checked_until": self.checked_until, "fired": self.fired,
                "snoozed": self.snoozed, "ringing": self.ringing}

    @classmethod
    def load(cls, path) -> "State":
        """The state in ``path``; a fresh one when the file is missing or damaged (the damaged file is set aside)."""
        try:
            return cls.from_json(jsonfile.read_json(path, max_bytes=4 * 1024 * 1024))
        except jsonfile.StoreError:
            jsonfile.set_aside(path)
            return cls()

    def save(self, path) -> None:
        jsonfile.write_json_atomic(path, self.to_json())
        self.dirty = False


@dataclass
class Batch:
    ring: list[Item] = field(default_factory=list)
    missed: list[Item] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.ring or self.missed)


class Engine:
    """Decides what is due. ``zone()`` gives the machine's time zone each time it is asked."""

    def __init__(self, state: State, zone: Callable[[], dt.tzinfo]) -> None:
        self.state = state
        self.zone = zone
        self.clock: dict = clock_model.empty_doc()
        self.calendar = None                         # a calendar.model.CalendarData, or None
        self._plan: tuple | None = None              # (calendar revision, zone, after, until, [ReminderDue])

    # --- what the service feeds in ---
    def set_clock(self, doc: dict) -> None:
        self.clock = doc

    def set_calendar(self, data) -> None:
        self.calendar = data
        self._plan = None

    def restore(self) -> None:
        """Call once after loading the state: alarms that were ringing when the service stopped come back."""
        for key, entry in list(self.state.ringing.items()):
            item = Item.from_json(entry)
            if item is not None and item.kind == "alarm" and key not in self.state.snoozed:
                self.state.snoozed[key] = {"due": item.when, "item": item.to_json()}
        if self.state.ringing:
            self.state.ringing.clear()
            self.state.dirty = True

    # --- candidates ---
    def _alarm_items(self, after: float, until: float, tz: dt.tzinfo) -> list[Item]:
        items = []
        for alarm in self.clock["alarms"]:
            rings = clock_model.alarm_occurrences(alarm, after, until, tz)
            if not rings:
                continue
            when = rings[-1]
            items.append(Item(f"alarm:{alarm['id']}:{int(when)}", "alarm", alarm["id"], when, alarm["name"], {
                "hour": alarm["hour"], "minute": alarm["minute"], "snooze": alarm["snooze"],
                "once": not alarm["days"], "times": len(rings), "armed": alarm["armed"]}))
        return items

    def _timer_items(self, now: clock_model.Now) -> list[Item]:
        items = []
        for timer in self.clock["timers"]:
            if clock_model.timer_phase(timer, now) != "done":
                continue
            when = clock_model.timer_due_wall(timer, now)
            assert when is not None
            items.append(Item(f"timer:{timer['id']}:{timer['run']}", "timer", timer["id"], min(when, now.wall),
                              timer["name"], {"duration": timer["duration"]}))
        return items

    def _focus_items(self, now: clock_model.Now) -> list[Item]:
        focus = self.clock["focus"]
        if focus["state"] != "running":
            return []
        position = clock_model.focus_position(focus, now)
        passed = [(index, end) for index, end in clock_model.focus_boundaries(focus) if position >= end]
        if not passed:
            return []
        segments = focus["segments"]
        # Boundaries that went by unseen: only the last one is worth telling; the others count as told.
        for index, _end in passed[:-1]:
            self.state.fired.setdefault(f"focus:{focus['run']}:{index}", now.wall)
        index, end = passed[-1]
        last = index == len(segments) - 1
        following = segments[index + 1] if not last else None
        focused = sum(segment["seconds"] for segment in segments[:index + 1] if segment["kind"] == "focus")
        elapsed = float(focus["position"]) + clock_model.elapsed_since(focus["anchor"], now)     # not capped
        return [Item(f"focus:{focus['run']}:{index}", "focus", str(focus["run"]), now.wall - (elapsed - end),
                     "Focus session", {"ended": segments[index]["kind"], "last": last, "focused": focused,
                                       "next": following["kind"] if following else "",
                                       "next_seconds": following["seconds"] if following else 0})]

    def _reminders(self, after: float, until: float, tz: dt.tzinfo) -> list:
        """The calendar reminders that fire in (after, until], from a plan that reaches some hours ahead."""
        if self.calendar is None:
            return []
        plan = self._plan
        key = (self.calendar.revision, tz)
        if plan is None or plan[:2] != key or plan[2] > after or plan[3] < until:
            reach = max(until, after) + HORIZON
            plan = (*key, after, reach, self.calendar.reminders_between(after, reach, tz))
            self._plan = plan
        return [due for due in plan[4] if after < due.when <= until]

    def _event_items(self, after: float, until: float, tz: dt.tzinfo) -> list[Item]:
        items = []
        for due in self._reminders(after, until, tz):
            occurrence = due.occurrence
            event = occurrence.event
            if event.modified and due.when < event.modified and occurrence.start_epoch <= event.modified:
                continue                # the event was written down after it had begun: nothing to remind of
            instance = occurrence.recurrence_id or str(int(occurrence.start_epoch))
            items.append(Item(f"event:{event.uid}:{instance}:{due.minutes}", "event", event.uid, due.when,
                              event.summary, {"start": occurrence.start_epoch, "end": occurrence.end_epoch,
                                              "all_day": occurrence.all_day, "location": event.location[:200],
                                              "recurrence_id": occurrence.recurrence_id,
                                              "minutes": due.minutes}))
        return items

    def _source_exists(self, item: Item) -> bool:
        if item.kind == "alarm":
            # An alarm that was switched off, switched on again or edited since has a new ``armed`` time.
            alarm = clock_model.find(self.clock["alarms"], item.source)
            return alarm is not None and alarm["armed"] == item.detail.get("armed", alarm["armed"])
        if item.kind == "timer":
            return clock_model.find(self.clock["timers"], item.source) is not None
        if item.kind == "event" and self.calendar is not None:
            return self.calendar.get(item.source) is not None or bool(self.calendar.overrides(item.source))
        return True

    # --- the poll ---
    def poll(self, now: clock_model.Now) -> Batch:
        state = self.state
        wall = now.wall
        if state.checked_until is None:
            state.checked_until = wall              # the very first run: nothing was missed
            state.dirty = True
        elif wall < state.checked_until - CLOCK_STEP_BACK:
            state.checked_until = wall              # the clock was set back: what fired then is in ``fired``
            state.dirty = True
            self._plan = None
        after = max(state.checked_until, wall - MAX_CATCH_UP)
        tz = self.zone()
        found = (self._alarm_items(after, wall, tz) + self._timer_items(now) + self._focus_items(now)
                 + self._event_items(after, wall, tz))
        fresh = [item for item in found if item.key not in state.fired]
        for key, entry in list(state.snoozed.items()):
            if entry["due"] > wall:
                continue
            del state.snoozed[key]
            state.dirty = True
            item = Item.from_json(entry["item"])
            if item is not None and self._source_exists(item):
                fresh.append(Item(item.key, item.kind, item.source, entry["due"], item.title, item.detail,
                                  snoozed=True))
        batch = Batch()
        for item in sorted(fresh, key=lambda entry: entry.when):
            late = max(0.0, wall - item.when)
            item = Item(item.key, item.kind, item.source, item.when, item.title, item.detail, late, item.snoozed)
            on_time = late <= GRACE
            if item.kind == "event" and not on_time:
                on_time = wall < float(item.detail.get("end", 0.0))
            (batch.ring if on_time else batch.missed).append(item)
            state.fired[item.key] = item.when
            if on_time:
                state.ringing[item.key] = item.to_json()
            state.dirty = True
        if wall > state.checked_until:
            state.checked_until = wall
        self._prune(wall)
        return batch

    def _prune(self, wall: float) -> None:
        fired = self.state.fired
        limit = wall - FIRED_KEEP
        if len(fired) > 64:
            for key in [key for key, when in fired.items() if when < limit]:
                del fired[key]
        if len(fired) > MAX_FIRED:
            for key, _when in sorted(fired.items(), key=lambda entry: entry[1])[:len(fired) - MAX_FIRED]:
                del fired[key]
        ringing = self.state.ringing
        if len(ringing) > MAX_SNOOZED:
            for key in list(ringing)[:len(ringing) - MAX_SNOOZED]:
                del ringing[key]

    def next_due(self, now: clock_model.Now) -> float | None:
        """The earliest instant after now at which something becomes due; None when nothing is in sight."""
        wall = now.wall
        tz = self.zone()
        times: list[float] = []
        for alarm in self.clock["alarms"]:
            ring = clock_model.alarm_next(alarm, wall, tz)
            if ring is not None:
                times.append(ring)
        for timer in self.clock["timers"]:
            if timer["state"] == "running" and f"timer:{timer['id']}:{timer['run']}" not in self.state.fired:
                due = clock_model.timer_due_wall(timer, now)
                if due is not None:
                    times.append(due)
        focus = self.clock["focus"]
        if focus["state"] == "running":
            position = clock_model.focus_position(focus, now)
            for _index, end in clock_model.focus_boundaries(focus):
                if end > position:
                    times.append(wall + end - position)
                    break
        if self.calendar is not None:
            upcoming = self._reminders(wall, wall + HORIZON / 2, tz)
            # Past the planned stretch nothing is known: come back before it ends and plan on.
            times.append(upcoming[0].when if upcoming else wall + HORIZON / 2)
        for entry in self.state.snoozed.values():
            times.append(entry["due"])
        return min(times) if times else None

    # --- answers to a notification ---
    def snooze(self, item: Item, wall: float, minutes: float) -> float:
        """Bring ``item`` back in ``minutes``; returns the instant."""
        due = wall + max(1.0, float(minutes) * 60)
        state = self.state
        if len(state.snoozed) >= MAX_SNOOZED and item.key not in state.snoozed:
            del state.snoozed[min(state.snoozed, key=lambda key: state.snoozed[key]["due"])]
        state.snoozed[item.key] = {"due": due, "item": item.to_json()}
        state.ringing.pop(item.key, None)
        state.dirty = True
        return due

    def hurry(self, wall: float, flag: str = "retry") -> bool:
        """Make the snoozed items whose detail has ``flag`` due now; True when there were any."""
        found = False
        for entry in self.state.snoozed.values():
            if entry["item"].get("detail", {}).get(flag) and entry["due"] > wall:
                entry["due"] = wall
                found = True
        if found:
            self.state.dirty = True
        return found

    def acknowledge(self, item: Item) -> None:
        """The user has seen ``item`` (dismissed it, or its notification went away)."""
        if self.state.ringing.pop(item.key, None) is not None:
            self.state.dirty = True

    def cancel_snoozes(self, kind: str, source: str) -> None:
        """Forget the snoozes of one alarm, timer or event (it was changed or removed)."""
        for key, entry in list(self.state.snoozed.items()):
            item = entry["item"]
            if item.get("kind") == kind and item.get("source") == source:
                del self.state.snoozed[key]
                self.state.dirty = True
