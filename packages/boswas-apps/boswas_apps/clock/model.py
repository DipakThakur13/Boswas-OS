"""Clock's data and the arithmetic on it (standard library only; no Qt).

The whole state of Clock is one JSON document, ``clock.json``:

    {"version": 1,
     "alarms":    [{"id", "name", "hour", "minute", "days": [0..6, Monday is 0], "snooze": minutes,
                    "enabled", "armed": when it was last switched on or edited}],
     "timers":    [{"id", "name", "duration": seconds, "state": idle|running|paused,
                    "left": seconds left at the anchor, "anchor", "run": counts the starts}],
     "stopwatch": {"state", "elapsed": seconds before the anchor, "anchor", "laps": [totals]},
     "focus":     {"state", "segments": [{"kind": focus|break, "seconds"}], "position", "anchor",
                   "run", "credited", "goal": minutes a day, "history": {"YYYY-MM-DD": seconds}},
     "world":     [{"zone": IANA name, "name": what the user sees}]}

Clock writes it; the reminder service reads it (and changes it when a
notification button restarts a timer or a one-time alarm has rung). Every
function here takes the document, or a part of it, as plain dicts and
changes it in place, so the same change can be applied to the copy in
memory and, under the file lock, to the copy on disk.

Running things are exact. An *anchor* remembers the moment something was
started on two clocks: the wall clock, and the boot clock (CLOCK_BOOTTIME:
monotonic, counts the time the machine is suspended) together with the
ID of the boot. While the machine has not been restarted the boot clock
measures the elapsed time, so setting the wall clock or changing the time
zone does not disturb a timer; after a restart the wall clock is used.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..reminders import timeutil

VERSION = 1
MAX_ALARMS = 200
MAX_TIMERS = 100
MAX_LAPS = 1000
MAX_WORLD = 100
MAX_NAME = 80
MAX_TIMER_SECONDS = 100 * 3600 - 1          # 99:59:59
MAX_HISTORY_DAYS = 800
SNOOZE_CHOICES = (5, 10, 15, 20, 30, 60)
DEFAULT_SNOOZE = 10
STATES = ("idle", "running", "paused")
FOCUS_MIN_TOTAL, FOCUS_MAX_TOTAL = 5, 240    # minutes
MIN_FOCUS_PERIOD = 10                        # minutes: a focus period is not planned shorter than this
DEFAULT_GOAL = 60                            # minutes a day


# --- the clocks ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Now:
    """One reading of the clocks: wall (seconds since the epoch), boot (seconds since boot), the boot's ID."""
    wall: float
    boot: float
    boot_id: str


_boot_id: str | None = None


def boot_id() -> str:
    """The ID of this boot (empty when the kernel does not tell)."""
    global _boot_id
    if _boot_id is None:
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
                _boot_id = handle.read(64).strip()
        except OSError:
            _boot_id = ""
    return _boot_id


def system_now() -> Now:
    """The clocks of this machine, now."""
    try:
        boot = time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        boot = time.monotonic()
    return Now(time.time(), boot, boot_id())


def make_anchor(now: Now) -> dict:
    return {"wall": now.wall, "boot": now.boot, "boot_id": now.boot_id}


def elapsed_since(anchor: dict | None, now: Now) -> float:
    """Seconds between an anchor and ``now``: by the boot clock within one boot, else by the wall clock."""
    if not anchor:
        return 0.0
    if anchor.get("boot_id") and anchor.get("boot_id") == now.boot_id:
        return max(0.0, now.boot - anchor["boot"])
    return max(0.0, now.wall - anchor["wall"])


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --- reading a document from outside ---------------------------------------------------------------------

def _number(value: Any, low: float, high: float, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return float(min(high, max(low, value)))


def _integer(value: Any, low: int, high: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return int(min(high, max(low, value)))


def _text(value: Any, limit: int = MAX_NAME) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\0", " ").split())[:limit]


def _identifier(value: Any) -> str:
    if isinstance(value, str) and 0 < len(value) <= 64 and all(c.isalnum() or c in "-_" for c in value):
        return value
    return ""


def _anchor(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    wall = value.get("wall")
    boot = value.get("boot")
    if isinstance(wall, bool) or not isinstance(wall, (int, float)) or wall != wall:
        return None
    return {"wall": float(wall), "boot": _number(boot, 0, 1e12, 0.0),
            "boot_id": value.get("boot_id") if isinstance(value.get("boot_id"), str) else ""}


def _alarm(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not _identifier(raw.get("id")):
        return None
    days = raw.get("days") if isinstance(raw.get("days"), list) else []
    snooze = _integer(raw.get("snooze"), 1, 120, DEFAULT_SNOOZE)
    return {
        "id": raw["id"],
        "name": _text(raw.get("name")),
        "hour": _integer(raw.get("hour"), 0, 23, 7),
        "minute": _integer(raw.get("minute"), 0, 59, 0),
        "days": sorted({day for day in days if isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6}),
        "snooze": snooze,
        "enabled": raw.get("enabled") is True,
        "armed": _number(raw.get("armed"), 0, 1e12, 0.0),
    }


def _timer(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not _identifier(raw.get("id")):
        return None
    duration = _integer(raw.get("duration"), 1, MAX_TIMER_SECONDS, 60)
    state = raw.get("state") if raw.get("state") in STATES else "idle"
    anchor = _anchor(raw.get("anchor"))
    if state == "running" and anchor is None:
        state = "idle"
    left = _number(raw.get("left"), 0, MAX_TIMER_SECONDS, duration)
    return {
        "id": raw["id"],
        "name": _text(raw.get("name")),
        "duration": duration,
        "state": state,
        "left": float(duration) if state == "idle" else left,
        "anchor": anchor if state == "running" else None,
        "run": _integer(raw.get("run"), 0, 2 ** 31, 0),
    }


def _stopwatch(raw: Any) -> dict:
    fresh = {"state": "idle", "elapsed": 0.0, "anchor": None, "laps": []}
    if not isinstance(raw, dict):
        return fresh
    state = raw.get("state") if raw.get("state") in STATES else "idle"
    anchor = _anchor(raw.get("anchor"))
    if state == "running" and anchor is None:
        state = "paused"
    if state == "idle":
        return fresh
    laps = []
    for lap in (raw.get("laps") if isinstance(raw.get("laps"), list) else [])[:MAX_LAPS]:
        value = _number(lap, 0, 1e9, -1.0)
        if value < 0 or (laps and value < laps[-1]):
            continue
        laps.append(value)
    return {"state": state, "elapsed": _number(raw.get("elapsed"), 0, 1e9, 0.0),
            "anchor": anchor if state == "running" else None, "laps": laps}


def _focus(raw: Any) -> dict:
    fresh = {"state": "idle", "segments": [], "position": 0.0, "anchor": None, "run": 0, "credited": 0.0,
             "goal": DEFAULT_GOAL, "history": {}}
    if not isinstance(raw, dict):
        return fresh
    fresh["goal"] = _integer(raw.get("goal"), 5, 24 * 60, DEFAULT_GOAL)
    fresh["run"] = _integer(raw.get("run"), 0, 2 ** 31, 0)
    history = {}
    if isinstance(raw.get("history"), dict):
        for key, value in raw["history"].items():
            if isinstance(key, str) and _is_day(key):
                seconds = _number(value, 0, 86400, 0.0)
                if seconds > 0:
                    history[key] = seconds
    for key in sorted(history)[:-MAX_HISTORY_DAYS]:
        del history[key]
    fresh["history"] = history
    state = raw.get("state") if raw.get("state") in STATES else "idle"
    segments = []
    for segment in (raw.get("segments") if isinstance(raw.get("segments"), list) else [])[:64]:
        if isinstance(segment, dict) and segment.get("kind") in ("focus", "break"):
            seconds = _integer(segment.get("seconds"), 1, FOCUS_MAX_TOTAL * 60, 0)
            if seconds:
                segments.append({"kind": segment["kind"], "seconds": seconds})
    anchor = _anchor(raw.get("anchor"))
    if not segments:
        state = "idle"
    if state == "running" and anchor is None:
        state = "paused"
    if state == "idle":
        return fresh
    total = sum(segment["seconds"] for segment in segments)
    fresh.update(state=state, segments=segments, position=_number(raw.get("position"), 0, total, 0.0),
                 anchor=anchor if state == "running" else None,
                 credited=_number(raw.get("credited"), 0, total, 0.0))
    return fresh


def _is_day(text: str) -> bool:
    try:
        return dt.date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def _world(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or timeutil.zone(raw.get("zone") if isinstance(raw.get("zone"), str) else "") is None:
        return None
    return {"zone": raw["zone"], "name": _text(raw.get("name")) or raw["zone"].rsplit("/", 1)[-1].replace("_", " ")}


def empty_doc() -> dict:
    return {"version": VERSION, "alarms": [], "timers": [], "stopwatch": _stopwatch(None), "focus": _focus(None),
            "world": []}


def normalize(raw: Any) -> dict:
    """A valid document from whatever the file held: wrong entries are dropped, values are clamped."""
    doc = empty_doc()
    if not isinstance(raw, dict):
        return doc
    for key, reader, limit in (("alarms", _alarm, MAX_ALARMS), ("timers", _timer, MAX_TIMERS)):
        seen = set()
        for entry in (raw.get(key) if isinstance(raw.get(key), list) else []):
            item = reader(entry)
            if item is not None and item["id"] not in seen and len(doc[key]) < limit:
                seen.add(item["id"])
                doc[key].append(item)
    doc["stopwatch"] = _stopwatch(raw.get("stopwatch"))
    doc["focus"] = _focus(raw.get("focus"))
    zones = set()
    for entry in (raw.get("world") if isinstance(raw.get("world"), list) else []):
        item = _world(entry)
        if item is not None and item["zone"] not in zones and len(doc["world"]) < MAX_WORLD:
            zones.add(item["zone"])
            doc["world"].append(item)
    return doc


def first_run_doc() -> dict:
    """What a new user finds: the four timers people set most."""
    doc = empty_doc()
    for minutes in (1, 3, 5, 10):
        doc["timers"].append(make_timer(minutes * 60, f"{minutes} min timer"))
    return doc


def find(items: list[dict], identifier: str) -> dict | None:
    for item in items:
        if item["id"] == identifier:
            return item
    return None


# --- alarms ----------------------------------------------------------------------------------------------

def make_alarm(hour: int, minute: int, *, name: str = "", days: tuple[int, ...] | list[int] = (),
               snooze: int = DEFAULT_SNOOZE, enabled: bool = True, now_wall: float = 0.0) -> dict:
    alarm = _alarm({"id": new_id(), "name": name, "hour": hour, "minute": minute, "days": list(days),
                    "snooze": snooze, "enabled": enabled, "armed": now_wall})
    assert alarm is not None
    return alarm


def arm_alarm(alarm: dict, enabled: bool, now_wall: float) -> None:
    """Switch an alarm on or off. Switching on (or editing) starts it afresh from ``now_wall``."""
    alarm["enabled"] = bool(enabled)
    alarm["armed"] = float(now_wall)


def _ring_time(day: dt.date, alarm: dict, tz: dt.tzinfo) -> float:
    return timeutil.to_epoch(dt.datetime(day.year, day.month, day.day, alarm["hour"], alarm["minute"]), tz)


def alarm_next(alarm: dict, after: float, tz: dt.tzinfo) -> float | None:
    """When the alarm rings next after the instant ``after``, or None (off, or a one-time alarm that is past).

    A repeating alarm rings on its weekdays at its local time. A one-time
    alarm rings once, the first time its local time comes after it was
    switched on. On the day summer time begins a time inside the skipped
    hour rings one hour later by the clock; on the day it ends an alarm in
    the repeated hour rings once, the first time.
    """
    if not alarm.get("enabled"):
        return None
    armed = float(alarm.get("armed") or 0.0)
    days = alarm.get("days") or []
    lower = armed if not days else max(after, armed)
    try:
        start = timeutil.from_epoch(lower, tz).date()
    except (OverflowError, ValueError):
        return None
    for offset in range(9):
        try:
            day = start + dt.timedelta(days=offset)
        except OverflowError:
            return None
        if days and day.weekday() not in days:
            continue
        ring = _ring_time(day, alarm, tz)
        if ring > lower:
            if not days:
                return ring if ring > after else None
            return ring
    return None


def alarm_occurrences(alarm: dict, after: float, until: float, tz: dt.tzinfo, limit: int = 64) -> list[float]:
    """The ring times in (``after``, ``until``], oldest first, at most the latest ``limit``."""
    found: list[float] = []
    cursor = after
    for _ in range(4000):
        ring = alarm_next(alarm, cursor, tz)
        if ring is None or ring > until:
            break
        found.append(ring)
        cursor = ring
    return found[-limit:]


# --- timers ----------------------------------------------------------------------------------------------

def make_timer(duration: int, name: str = "") -> dict:
    timer = _timer({"id": new_id(), "name": name, "duration": duration})
    assert timer is not None
    return timer


def timer_remaining(timer: dict, now: Now) -> float:
    """Seconds left; 0 when a running timer has finished."""
    if timer["state"] == "running":
        return max(0.0, timer["left"] - elapsed_since(timer["anchor"], now))
    if timer["state"] == "paused":
        return float(timer["left"])
    return float(timer["duration"])


def timer_phase(timer: dict, now: Now) -> str:
    """idle, running, paused, or done (it ran down and has not been reset)."""
    if timer["state"] == "running" and timer_remaining(timer, now) <= 0:
        return "done"
    return timer["state"]


def timer_due_wall(timer: dict, now: Now) -> float | None:
    """The wall-clock instant a running timer ends (in the past when it is done); None when it does not run."""
    if timer["state"] != "running":
        return None
    return now.wall + timer["left"] - elapsed_since(timer["anchor"], now)


def timer_start(timer: dict, now: Now) -> None:
    """Start an idle timer from its full duration, or go on with a paused one."""
    if timer["state"] == "running":
        return
    if timer["state"] == "idle":
        timer["left"] = float(timer["duration"])
    timer["state"] = "running"
    timer["anchor"] = make_anchor(now)
    timer["run"] = int(timer.get("run", 0)) + 1


def timer_pause(timer: dict, now: Now) -> None:
    if timer["state"] != "running":
        return
    timer["left"] = timer_remaining(timer, now)
    timer["state"] = "paused"
    timer["anchor"] = None


def timer_reset(timer: dict) -> None:
    timer["state"] = "idle"
    timer["left"] = float(timer["duration"])
    timer["anchor"] = None


def timer_restart(timer: dict, now: Now) -> None:
    timer_reset(timer)
    timer_start(timer, now)


def timer_set(timer: dict, duration: int, name: str) -> None:
    """Change length and name; a timer that is running or paused is reset."""
    timer["name"] = _text(name)
    duration = _integer(duration, 1, MAX_TIMER_SECONDS, timer["duration"])
    if duration != timer["duration"]:
        timer["duration"] = duration
        timer_reset(timer)


# --- stopwatch -------------------------------------------------------------------------------------------

def stopwatch_elapsed(watch: dict, now: Now) -> float:
    if watch["state"] == "running":
        return watch["elapsed"] + elapsed_since(watch["anchor"], now)
    return float(watch["elapsed"])


def stopwatch_start(watch: dict, now: Now) -> None:
    if watch["state"] == "running":
        return
    watch["state"] = "running"
    watch["anchor"] = make_anchor(now)


def stopwatch_pause(watch: dict, now: Now) -> None:
    if watch["state"] != "running":
        return
    watch["elapsed"] = stopwatch_elapsed(watch, now)
    watch["state"] = "paused"
    watch["anchor"] = None


def stopwatch_reset(watch: dict) -> None:
    watch.update(state="idle", elapsed=0.0, anchor=None, laps=[])


def stopwatch_lap(watch: dict, now: Now) -> bool:
    """Record a lap at the current time; False when the stopwatch does not run or the list is full."""
    if watch["state"] != "running" or len(watch["laps"]) >= MAX_LAPS:
        return False
    watch["laps"].append(stopwatch_elapsed(watch, now))
    return True


@dataclass(frozen=True)
class Lap:
    number: int
    split: float           # this lap alone
    total: float           # from the start to the end of this lap
    mark: str              # "fastest", "slowest" or ""


def stopwatch_laps(watch: dict) -> list[Lap]:
    """The laps, first lap first. Fastest and slowest are marked once there are two or more."""
    totals = watch["laps"]
    splits = [total - (totals[index - 1] if index else 0.0) for index, total in enumerate(totals)]
    fastest = slowest = -1
    if len(splits) >= 2 and max(splits) - min(splits) > 0:
        fastest = splits.index(min(splits))
        slowest = splits.index(max(splits))
    return [Lap(index + 1, split, totals[index],
                "fastest" if index == fastest else "slowest" if index == slowest else "")
            for index, split in enumerate(splits)]


# --- focus sessions --------------------------------------------------------------------------------------

def focus_plan(total_minutes: int, focus_minutes: int = 25, break_minutes: int = 5,
               skip_breaks: bool = False) -> list[dict]:
    """Split a session into focus periods and breaks.

    ``focus_minutes`` and ``break_minutes`` are the lengths aimed for. Breaks
    are placed after every focus period that leaves room for another focus
    period of at least 10 minutes; the focus time is then shared equally
    between the periods, so 60 minutes with 25/5 become 27:30, a break of
    5:00, and 27:30. The session always ends with a focus period.
    """
    total = _integer(total_minutes, FOCUS_MIN_TOTAL, FOCUS_MAX_TOTAL, 30) * 60
    period = _integer(focus_minutes, 5, 120, 25) * 60
    pause = _integer(break_minutes, 1, 60, 5) * 60
    breaks = 0
    if not skip_breaks:
        cycles, rest = divmod(total, period + pause)
        breaks = cycles if rest >= MIN_FOCUS_PERIOD * 60 else cycles - 1
        breaks = max(0, breaks)
        while breaks and (total - breaks * pause) / (breaks + 1) < MIN_FOCUS_PERIOD * 60:
            breaks -= 1
    focus_total = total - breaks * pause
    base, extra = divmod(focus_total, breaks + 1)
    segments: list[dict] = []
    for index in range(breaks + 1):
        segments.append({"kind": "focus", "seconds": base + (1 if index < extra else 0)})
        if index < breaks:
            segments.append({"kind": "break", "seconds": pause})
    return segments


def plan_breaks(segments: list[dict]) -> int:
    return sum(1 for segment in segments if segment["kind"] == "break")


def focus_total(focus: dict) -> float:
    return float(sum(segment["seconds"] for segment in focus["segments"]))


def focus_position(focus: dict, now: Now) -> float:
    """Seconds into the plan, at most its length."""
    position = float(focus["position"])
    if focus["state"] == "running":
        position += elapsed_since(focus["anchor"], now)
    return min(position, focus_total(focus))


@dataclass(frozen=True)
class FocusStatus:
    state: str             # idle, running, paused, or done (the plan ran to its end and was not settled yet)
    index: int             # the current segment
    kind: str              # focus or break ("" when idle)
    segment_left: float    # seconds left in the current segment
    segment_length: float
    total_left: float
    focused: float         # seconds of focus completed in this session so far
    period: int            # which focus period this is (1-based), counting the current one
    periods: int           # how many focus periods the plan has


def focus_status(focus: dict, now: Now) -> FocusStatus:
    if focus["state"] == "idle" or not focus["segments"]:
        return FocusStatus("idle", 0, "", 0.0, 0.0, 0.0, 0.0, 0, 0)
    position = focus_position(focus, now)
    total = focus_total(focus)
    periods = sum(1 for segment in focus["segments"] if segment["kind"] == "focus")
    start = 0.0
    focused = 0.0
    period = 0
    for index, segment in enumerate(focus["segments"]):
        end = start + segment["seconds"]
        if segment["kind"] == "focus":
            period += 1
        if position < end or index == len(focus["segments"]) - 1:
            if segment["kind"] == "focus":
                focused += min(position, end) - start
            state = "done" if position >= total else focus["state"]
            return FocusStatus(state, index, segment["kind"], max(0.0, end - position), float(segment["seconds"]),
                               max(0.0, total - position), focused, period, periods)
        if segment["kind"] == "focus":
            focused += segment["seconds"]
        start = end
    raise AssertionError("unreachable")


def focus_boundaries(focus: dict) -> list[tuple[int, float]]:
    """(index of the segment that ends, seconds into the plan at which it ends) for every segment."""
    marks = []
    offset = 0.0
    for index, segment in enumerate(focus["segments"]):
        offset += segment["seconds"]
        marks.append((index, offset))
    return marks


def focus_begin(focus: dict, segments: list[dict], now: Now) -> None:
    """Start a new session with the given plan."""
    focus.update(state="running", segments=[dict(segment) for segment in segments], position=0.0,
                 anchor=make_anchor(now), credited=0.0, run=int(focus.get("run", 0)) + 1)


def focus_pause(focus: dict, now: Now) -> None:
    if focus["state"] != "running":
        return
    focus["position"] = focus_position(focus, now)
    focus["state"] = "paused"
    focus["anchor"] = None


def focus_resume(focus: dict, now: Now) -> None:
    if focus["state"] != "paused":
        return
    focus["state"] = "running"
    focus["anchor"] = make_anchor(now)


def focus_settle(focus: dict, now: Now, tz: dt.tzinfo, *, stop: bool = False) -> float:
    """Add the focus time of the session that is not counted yet to the day's total; returns what was added.

    A session whose plan has run to its end, or that is stopped with
    ``stop=True``, ends here (the state becomes idle). The time is counted
    for the day the session ended on, or today while it goes on.
    """
    if focus["state"] == "idle":
        return 0.0
    status = focus_status(focus, now)
    added = max(0.0, status.focused - float(focus.get("credited", 0.0)))
    when = now.wall
    if focus["state"] == "running" and focus["anchor"] is not None:
        ended = now.wall - elapsed_since(focus["anchor"], now) + (focus_total(focus) - float(focus["position"]))
        when = min(when, ended)
    if added > 0:
        day = timeutil.from_epoch(when, tz).date().isoformat()
        history = focus["history"]
        history[day] = min(86400.0, float(history.get(day, 0.0)) + added)
        for key in sorted(history)[:-MAX_HISTORY_DAYS]:
            del history[key]
        focus["credited"] = status.focused
    if stop or status.state == "done":
        focus.update(state="idle", segments=[], position=0.0, anchor=None, credited=0.0)
    return added


@dataclass(frozen=True)
class FocusStats:
    today: float           # seconds of focus today, the running session included
    yesterday: float
    goal: int              # minutes
    streak: int            # days in a row on which the goal was reached, today included once it is reached
    reached: bool


def focus_stats(focus: dict, now: Now, tz: dt.tzinfo) -> FocusStats:
    today = timeutil.from_epoch(now.wall, tz).date()
    history = focus["history"]
    pending = 0.0
    if focus["state"] != "idle":
        pending = max(0.0, focus_status(focus, now).focused - float(focus.get("credited", 0.0)))
    done_today = float(history.get(today.isoformat(), 0.0)) + pending
    goal_seconds = focus["goal"] * 60
    reached = done_today >= goal_seconds
    streak = 1 if reached else 0
    day = today - dt.timedelta(days=1)
    while float(history.get(day.isoformat(), 0.0)) >= goal_seconds and streak < MAX_HISTORY_DAYS:
        streak += 1
        day -= dt.timedelta(days=1)
    yesterday = float(history.get((today - dt.timedelta(days=1)).isoformat(), 0.0))
    return FocusStats(done_today, yesterday, focus["goal"], streak, reached)


# --- world clock -----------------------------------------------------------------------------------------

def world_add(doc: dict, zone: str, name: str) -> bool:
    """Add a place; False when the zone is unknown, already listed, or the list is full."""
    entry = _world({"zone": zone, "name": name})
    if entry is None or len(doc["world"]) >= MAX_WORLD or any(item["zone"] == zone for item in doc["world"]):
        return False
    doc["world"].append(entry)
    return True


def world_remove(doc: dict, zone: str) -> bool:
    before = len(doc["world"])
    doc["world"] = [item for item in doc["world"] if item["zone"] != zone]
    return len(doc["world"]) != before


@dataclass(frozen=True)
class WorldTime:
    local: dt.datetime     # the wall time there
    offset: int            # seconds ahead of (+) or behind (-) the reference zone
    day_shift: int         # -1 yesterday, 0 today, +1 tomorrow, relative to the reference zone
    daytime: bool          # between 6 in the morning and 6 in the evening there
    abbreviation: str      # CET, PST, +0530 ...


def world_time(zone_name: str, instant: float, reference: dt.tzinfo) -> WorldTime | None:
    """The time in ``zone_name`` at ``instant``, compared with the zone ``reference``."""
    tz = timeutil.zone(zone_name)
    if tz is None:
        return None
    there = timeutil.to_aware(instant, tz)
    here = timeutil.to_aware(instant, reference)
    offset = timeutil.utc_offset(instant, tz) - timeutil.utc_offset(instant, reference)
    shift = (there.date() - here.date()).days
    return WorldTime(there.replace(tzinfo=None), offset, max(-1, min(1, shift)), 6 <= there.hour < 18,
                     there.tzname() or "")
