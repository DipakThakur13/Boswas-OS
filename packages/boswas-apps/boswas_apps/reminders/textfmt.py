"""Words for times and durations, shared by the reminder service, Clock and Calendar (no Qt)."""

from __future__ import annotations

import datetime as dt

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAYS_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def clock_time(hour: int, minute: int, twelve_hour: bool = False) -> str:
    """``07:05`` or ``7:05 AM``."""
    if not twelve_hour:
        return f"{hour:02d}:{minute:02d}"
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def time_of(moment: dt.datetime | dt.time, twelve_hour: bool = False) -> str:
    return clock_time(moment.hour, moment.minute, twelve_hour)


def short_date(day: dt.date, today: dt.date | None = None) -> str:
    """``Today``, ``Tomorrow``, ``Yesterday``, ``Tue 17 Nov`` or, in another year, ``Tue 17 Nov 2027``."""
    if today is not None:
        delta = (day - today).days
        if delta == 0:
            return "Today"
        if delta == 1:
            return "Tomorrow"
        if delta == -1:
            return "Yesterday"
    text = f"{WEEKDAYS_SHORT[day.weekday()]} {day.day} {MONTHS_SHORT[day.month - 1]}"
    if today is None or day.year != today.year:
        text += f" {day.year}"
    return text


def long_date(day: dt.date) -> str:
    """``Tuesday, 17 November 2026``."""
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]} {day.year}"


def duration_words(seconds: float, *, short: bool = False) -> str:
    """``7 hours 12 minutes``, ``1 day 2 hours``, ``45 seconds``; ``short``: ``7 h 12 min``."""
    seconds = max(0, int(round(seconds)))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} d" if short else plural(days, "day"))
    if hours:
        parts.append(f"{hours} h" if short else plural(hours, "hour"))
    if minutes and not days:
        parts.append(f"{minutes} min" if short else plural(minutes, "minute"))
    if not parts:
        parts.append(f"{secs} s" if short else plural(secs, "second"))
    return " ".join(parts[:2])


def until_words(seconds: float) -> str:
    """How long until something that is ``seconds`` away: ``in 7 hours 12 minutes``, ``in less than a minute``.

    Whole minutes, rounded up, so an alarm 30 seconds past the minute still
    reads as the minute people expect.
    """
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return "in less than a minute"
    minutes = -(-int(seconds) // 60)
    return "in " + duration_words(minutes * 60)


def countdown(seconds: float, *, hours: bool | None = None) -> str:
    """``05:00`` or ``1:05:00``: whole seconds, rounded up, so a timer shows 00:01 until it is over."""
    whole = max(0, -(-int(seconds * 1000) // 1000))
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    if h or hours:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def stopwatch(seconds: float) -> str:
    """``00:01:23.45``: hundredths, rounded down."""
    hundredths = max(0, int(seconds * 100))
    whole, fraction = divmod(hundredths, 100)
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{fraction:02d}"


def weekdays_text(days: list[int] | tuple[int, ...]) -> str:
    """``Once``, ``Every day``, ``Weekdays``, ``Weekends`` or ``Mon, Wed, Fri``."""
    chosen = sorted(set(days))
    if not chosen:
        return "Once"
    if chosen == [0, 1, 2, 3, 4, 5, 6]:
        return "Every day"
    if chosen == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if chosen == [5, 6]:
        return "Weekends"
    return ", ".join(WEEKDAYS_SHORT[day] for day in chosen)


def offset_words(seconds: int) -> str:
    """A zone's difference to here: ``Same time``, ``3 hours ahead``, ``5 hours 30 minutes behind``."""
    if seconds == 0:
        return "Same time"
    return f"{duration_words(abs(seconds))} {'ahead' if seconds > 0 else 'behind'}"


def before_words(minutes: int) -> str:
    """A reminder's lead time: ``At time of event``, ``15 minutes before``, ``1 day before``, ``2 hours after``."""
    if minutes == 0:
        return "At time of event"
    amount = abs(minutes)
    for size, word in ((10080, "week"), (1440, "day"), (60, "hour")):
        if amount % size == 0:
            text = plural(amount // size, word)
            break
    else:
        text = plural(amount, "minute")
    return f"{text} {'before' if minutes > 0 else 'after'}"
