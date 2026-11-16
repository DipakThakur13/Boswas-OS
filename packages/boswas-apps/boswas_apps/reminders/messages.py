"""The notifications of the reminder service in words (standard library only; no Qt)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from . import textfmt, timeutil
from .engine import EVENT_SNOOZE_MINUTES, Item

LOW, NORMAL, CRITICAL = 0, 1, 2
STAYS = 0                 # expire_timeout: until the user dismisses it
SERVER_DEFAULT = -1
CLOCK, CALENDAR = "clock", "calendar"


@dataclass(frozen=True)
class Message:
    app: str                                   # clock or calendar: whose notification this is
    summary: str
    body: str
    actions: tuple[tuple[str, str], ...]       # (key, label); the key comes back when the button is pressed
    urgency: int
    timeout: int
    sound: str                                 # alarm, timer, reminder or "" (silent)


def _when_text(instant: float, wall: float, tz: dt.tzinfo, twelve_hour: bool) -> str:
    moment = timeutil.from_epoch(instant, tz)
    today = timeutil.from_epoch(wall, tz).date()
    return f"{textfmt.short_date(moment.date(), today)} {textfmt.time_of(moment, twelve_hour)}"


def event_time_text(detail: dict, wall: float, tz: dt.tzinfo, twelve_hour: bool) -> str:
    """``Today 10:00 – 11:00``, ``Tomorrow, all day``, ``Tue 17 Nov 10:00 – Wed 18 Nov 09:00``."""
    start = timeutil.from_epoch(float(detail.get("start", wall)), tz)
    end = timeutil.from_epoch(float(detail.get("end", detail.get("start", wall))), tz)
    today = timeutil.from_epoch(wall, tz).date()
    if detail.get("all_day"):
        last = (end - dt.timedelta(days=1)).date() if end > start else start.date()
        text = textfmt.short_date(start.date(), today)
        if last > start.date():
            text += f" – {textfmt.short_date(last, today)}"
        return f"{text}, all day"
    text = f"{textfmt.short_date(start.date(), today)} {textfmt.time_of(start, twelve_hour)}"
    if end > start:
        if end.date() == start.date():
            text += f" – {textfmt.time_of(end, twelve_hour)}"
        else:
            text += f" – {textfmt.short_date(end.date(), today)} {textfmt.time_of(end, twelve_hour)}"
    return text


def _relative(start: float, wall: float) -> str:
    delta = start - wall
    if abs(delta) < 45:
        return "now"
    if delta > 0:
        return textfmt.until_words(delta)
    return f"started {textfmt.duration_words(-delta)} ago"


def compose(item: Item, wall: float, tz: dt.tzinfo, twelve_hour: bool = False) -> Message:
    """The notification for an item that rings."""
    detail = item.detail
    if item.kind == "alarm":
        time_text = textfmt.clock_time(int(detail.get("hour", 0)), int(detail.get("minute", 0)), twelve_hour)
        body = time_text
        if item.snoozed and not detail.get("retry"):
            body += " · snoozed"
        elif item.late > 90:
            body += f" · {textfmt.duration_words(item.late)} ago"
        snooze = int(detail.get("snooze", 10))
        return Message(CLOCK, item.title or "Alarm", body,
                       (("snooze", f"Snooze {snooze} min"), ("dismiss", "Dismiss")), CRITICAL, STAYS, "alarm")
    if item.kind == "timer":
        body = f"{textfmt.duration_words(float(detail.get('duration', 0)))} are up"
        if float(detail.get("duration", 0)) in (1.0, 60.0, 3600.0, 86400.0):
            body = f"{textfmt.duration_words(float(detail.get('duration', 0)))} is up"
        if item.late > 90:
            body += f" · {textfmt.duration_words(item.late)} ago"
        return Message(CLOCK, item.title or "Timer", body, (("dismiss", "Dismiss"), ("restart", "Restart")),
                       NORMAL, STAYS, "timer")
    if item.kind == "focus":
        if detail.get("last"):
            summary = "Focus session complete"
            body = f"You focused for {textfmt.duration_words(float(detail.get('focused', 0)))}."
        elif detail.get("ended") == "focus":
            summary = "Time for a break"
            body = f"Take {textfmt.duration_words(float(detail.get('next_seconds', 0)))}, then focus again."
        else:
            summary = "Break is over"
            body = f"Back to focus for {textfmt.duration_words(float(detail.get('next_seconds', 0)))}."
        return Message(CLOCK, summary, body, (("dismiss", "Dismiss"),), NORMAL, SERVER_DEFAULT, "reminder")
    body = event_time_text(detail, wall, tz, twelve_hour)
    if not detail.get("all_day"):
        body += f" · {_relative(float(detail.get('start', wall)), wall)}"
    if detail.get("location"):
        body += f"\n{detail['location']}"
    return Message(CALENDAR, item.title or "(No title)", body,
                   (("open", "Open"), ("snooze", f"Snooze {EVENT_SNOOZE_MINUTES} min"), ("dismiss", "Dismiss")),
                   NORMAL, STAYS, "reminder")


def missed_line(item: Item, wall: float, tz: dt.tzinfo, twelve_hour: bool = False) -> str:
    """One line of the summary of what was missed."""
    detail = item.detail
    if item.kind == "alarm":
        text = f"{_when_text(item.when, wall, tz, twelve_hour)} · Alarm"
        if item.title:
            text += f": {item.title}"
        times = int(detail.get("times", 1))
        return text + (f" ({times} times)" if times > 1 else "")
    if item.kind == "timer":
        return f"{_when_text(item.when, wall, tz, twelve_hour)} · Timer{': ' + item.title if item.title else ''}"
    if item.kind == "focus":
        what = "Focus session ended" if detail.get("last") else "Focus period ended"
        return f"{_when_text(item.when, wall, tz, twelve_hour)} · {what}"
    return f"{event_time_text(detail, wall, tz, twelve_hour)} · {item.title or '(No title)'}"


def compose_missed(items: list[Item], wall: float, tz: dt.tzinfo, twelve_hour: bool = False,
                   shown: int = 5) -> list[Message]:
    """One quiet notification per application for everything that was missed (none when nothing was)."""
    messages = []
    for app, kinds, label in ((CLOCK, ("alarm", "timer", "focus"), "Open Clock"),
                              (CALENDAR, ("event",), "Open Calendar")):
        mine = sorted((item for item in items if item.kind in kinds), key=lambda item: item.when, reverse=True)
        if not mine:
            continue
        if len(mine) == 1:
            item = mine[0]
            summary = {"alarm": "Missed alarm", "timer": "A timer finished", "focus": "Focus session ended",
                       "event": "Missed reminder"}[item.kind]
        elif app == CLOCK:
            summary = f"{len(mine)} missed alarms and timers"
        else:
            summary = f"{len(mine)} missed reminders"
        lines = [missed_line(item, wall, tz, twelve_hour) for item in mine[:shown]]
        if len(mine) > shown:
            lines.append(f"and {len(mine) - shown} more")
        messages.append(Message(app, summary, "\n".join(lines), (("open", label), ("dismiss", "Dismiss")),
                                NORMAL, SERVER_DEFAULT, ""))
    return messages
