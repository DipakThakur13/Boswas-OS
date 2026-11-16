"""Recurrence rules of RFC 5545 (RRULE): parsing, writing, expanding (standard library only; no Qt).

A rule is expanded in the local wall time of the event (naive
``datetime``; an all-day event uses midnight), as the RFC prescribes, and
the caller converts the results to instants. The expansion follows
section 3.3.10: the BYxxx parts expand or limit the set of each period,
BYSETPOS picks from the set, invalid dates (30 February, a fifth Monday
that does not exist) are skipped. The start of the event (DTSTART) is
always the first occurrence, whether it matches the rule or not, and it
counts towards COUNT.

Rules come from files that anyone may have written, so the expansion is
bounded: a caller asks for a window and a maximum number of results, a
rule without COUNT is advanced to the window arithmetically instead of
being walked through, long empty stretches are jumped over, and a
generator stops after ``budget`` candidates or after too many periods in a
row without an occurrence. A rule that hits a bound simply has no further
occurrences; nothing hangs.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as dt
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Callable, Iterator

from ..reminders import textfmt, timeutil

FREQS = ("SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY")
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
MAX_RULE_TEXT = 4096
MAX_LIST = 800
MAX_INTERVAL = 1_000_000
MAX_COUNT = 1_000_000_000
DEFAULT_BUDGET = 200_000
COUNT_WALK = 50_000
# Periods in a row without an occurrence after which a rule is taken to have no more: generous for
# real rules (29 February comes every 8 years at worst), small enough to cost little.
MAX_EMPTY_PERIODS = {"YEARLY": 120, "MONTHLY": 400, "WEEKLY": 800}
MAX_EMPTY_DEFAULT = 4000
_ORDINALS = ("first", "second", "third", "fourth", "fifth")
_BYDAY = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")
_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATETIME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")


class RecurrenceError(ValueError):
    """A recurrence rule that cannot be understood."""


@dataclass(frozen=True)
class Rule:
    """One RRULE. ``until`` is naive for a local wall time (or a date) and aware (UTC) for an instant."""
    freq: str
    interval: int = 1
    count: int | None = None
    until: dt.datetime | None = None
    until_is_date: bool = False
    bysecond: tuple[int, ...] = ()
    byminute: tuple[int, ...] = ()
    byhour: tuple[int, ...] = ()
    byday: tuple[tuple[int, int], ...] = ()          # (ordinal or 0, weekday with Monday = 0)
    bymonthday: tuple[int, ...] = ()
    byyearday: tuple[int, ...] = ()
    byweekno: tuple[int, ...] = ()
    bymonth: tuple[int, ...] = ()
    bysetpos: tuple[int, ...] = ()
    wkst: int = 0


# --- text ------------------------------------------------------------------------------------------------

def _int_list(name: str, value: str, low: int, high: int, zero: bool = True) -> tuple[int, ...]:
    numbers = []
    parts = value.split(",")
    if len(parts) > MAX_LIST:
        raise RecurrenceError(f"{name} has too many values")
    for part in parts:
        part = part.strip()
        if not re.fullmatch(r"[+-]?\d{1,4}", part):
            raise RecurrenceError(f"{name}: {part[:20]!r} is not a number")
        number = int(part)
        if not low <= number <= high or (number == 0 and not zero):
            raise RecurrenceError(f"{name}: {number} is out of range")
        numbers.append(number)
    return tuple(dict.fromkeys(numbers))


def parse_until(value: str) -> tuple[dt.datetime, bool]:
    """(the UNTIL value, whether it was written as a date)."""
    value = value.strip().upper()
    match = _DATE.match(value)
    try:
        if match:
            year, month, day = (int(group) for group in match.groups())
            return dt.datetime(year, month, day, 23, 59, 59), True
        match = _DATETIME.match(value)
        if match:
            year, month, day, hour, minute, second = (int(group) for group in match.groups()[:6])
            moment = dt.datetime(year, month, day, hour, minute, min(second, 59))
            return (moment.replace(tzinfo=timeutil.UTC) if match.group(7) else moment), False
    except ValueError as exc:
        raise RecurrenceError(f"UNTIL: {value[:20]!r} is not a date") from exc
    raise RecurrenceError(f"UNTIL: {value[:20]!r} is not a date")


def parse_rrule(text: str) -> Rule:
    """A Rule from the value of an RRULE property (``FREQ=WEEKLY;BYDAY=MO,WE``). Raises RecurrenceError."""
    if not isinstance(text, str) or len(text) > MAX_RULE_TEXT:
        raise RecurrenceError("the rule is too long")
    text = text.strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    fields: dict[str, str] = {}
    for part in text.split(";"):
        if not part.strip():
            continue
        name, separator, value = part.partition("=")
        name = name.strip().upper()
        if not separator or not value.strip():
            raise RecurrenceError(f"{name[:20]!r} has no value")
        if name in fields:
            raise RecurrenceError(f"{name} is given twice")
        fields[name] = value.strip().upper()
    freq = fields.get("FREQ")
    if freq not in FREQS:
        raise RecurrenceError("FREQ is missing or unknown")
    values: dict = {"freq": freq}
    if "INTERVAL" in fields:
        if not fields["INTERVAL"].isdigit() or not 1 <= int(fields["INTERVAL"]) <= MAX_INTERVAL:
            raise RecurrenceError("INTERVAL must be a positive number")
        values["interval"] = int(fields["INTERVAL"])
    if "COUNT" in fields:
        if not fields["COUNT"].isdigit() or not 1 <= int(fields["COUNT"]) <= MAX_COUNT:
            raise RecurrenceError("COUNT must be a positive number")
        values["count"] = int(fields["COUNT"])
    if "UNTIL" in fields:
        values["until"], values["until_is_date"] = parse_until(fields["UNTIL"])
    if "WKST" in fields:
        if fields["WKST"] not in WEEKDAY_CODES:
            raise RecurrenceError("WKST is not a weekday")
        values["wkst"] = WEEKDAY_CODES.index(fields["WKST"])
    if "BYDAY" in fields:
        days = []
        parts = fields["BYDAY"].split(",")
        if len(parts) > MAX_LIST:
            raise RecurrenceError("BYDAY has too many values")
        for part in parts:
            match = _BYDAY.match(part.strip())
            if not match:
                raise RecurrenceError(f"BYDAY: {part[:20]!r} is not a weekday")
            ordinal = int(match.group(1)) if match.group(1) else 0
            if match.group(1) and not 1 <= abs(ordinal) <= 53:
                raise RecurrenceError(f"BYDAY: {part[:20]!r} is out of range")
            days.append((ordinal, WEEKDAY_CODES.index(match.group(2))))
        values["byday"] = tuple(dict.fromkeys(days))
    for name, key, low, high, zero in (("BYSECOND", "bysecond", 0, 60, True), ("BYMINUTE", "byminute", 0, 59, True),
                                       ("BYHOUR", "byhour", 0, 23, True),
                                       ("BYMONTHDAY", "bymonthday", -31, 31, False),
                                       ("BYYEARDAY", "byyearday", -366, 366, False),
                                       ("BYWEEKNO", "byweekno", -53, 53, False),
                                       ("BYMONTH", "bymonth", 1, 12, False),
                                       ("BYSETPOS", "bysetpos", -366, 366, False)):
        if name in fields:
            numbers = _int_list(name, fields[name], low, high, zero)
            if name == "BYSECOND":
                numbers = tuple(dict.fromkeys(min(59, number) for number in numbers))
            values[key] = numbers
    return Rule(**values)


def format_until(rule: Rule) -> str:
    until = rule.until
    assert until is not None
    if rule.until_is_date:
        return until.strftime("%Y%m%d")
    if until.tzinfo is not None:
        return until.astimezone(timeutil.UTC).strftime("%Y%m%dT%H%M%SZ")
    return until.strftime("%Y%m%dT%H%M%S")


def format_rrule(rule: Rule) -> str:
    """The value of an RRULE property for ``rule``."""
    parts = [f"FREQ={rule.freq}"]
    if rule.interval != 1:
        parts.append(f"INTERVAL={rule.interval}")
    if rule.count is not None:
        parts.append(f"COUNT={rule.count}")
    if rule.until is not None:
        parts.append(f"UNTIL={format_until(rule)}")
    if rule.bymonth:
        parts.append("BYMONTH=" + ",".join(str(n) for n in rule.bymonth))
    if rule.byweekno:
        parts.append("BYWEEKNO=" + ",".join(str(n) for n in rule.byweekno))
    if rule.byyearday:
        parts.append("BYYEARDAY=" + ",".join(str(n) for n in rule.byyearday))
    if rule.bymonthday:
        parts.append("BYMONTHDAY=" + ",".join(str(n) for n in rule.bymonthday))
    if rule.byday:
        parts.append("BYDAY=" + ",".join((str(n) if n else "") + WEEKDAY_CODES[day] for n, day in rule.byday))
    if rule.byhour:
        parts.append("BYHOUR=" + ",".join(str(n) for n in rule.byhour))
    if rule.byminute:
        parts.append("BYMINUTE=" + ",".join(str(n) for n in rule.byminute))
    if rule.bysecond:
        parts.append("BYSECOND=" + ",".join(str(n) for n in rule.bysecond))
    if rule.bysetpos:
        parts.append("BYSETPOS=" + ",".join(str(n) for n in rule.bysetpos))
    if rule.wkst != 0:
        parts.append(f"WKST={WEEKDAY_CODES[rule.wkst]}")
    return ";".join(parts)


# --- expansion -------------------------------------------------------------------------------------------

def _year_length(year: int) -> int:
    return 366 if _calendar.isleap(year) else 365


def _week_one(year: int, wkst: int) -> dt.date:
    """The first day of week 1 of ``year``: the first week (starting on ``wkst``) with four days in the year."""
    first = dt.date(year, 1, 1)
    offset = (first.weekday() - wkst) % 7
    return first - dt.timedelta(days=offset) if offset <= 3 else first + dt.timedelta(days=7 - offset)


def week_number(day: dt.date, wkst: int = 0) -> tuple[int, int]:
    """(the week number of ``day``, the number of weeks of the year that week belongs to)."""
    year = day.year
    if year < dt.MAXYEAR and day >= _week_one(year + 1, wkst):
        year += 1
    elif day < _week_one(year, wkst):
        year -= 1
    year = max(dt.MINYEAR, year)
    start = _week_one(year, wkst)
    weeks = ((_week_one(year + 1, wkst) if year < dt.MAXYEAR else start + dt.timedelta(days=364)) - start).days // 7
    return (day - start).days // 7 + 1, weeks


class _Expander:
    """The occurrences of one rule from one start, as a generator of naive datetimes."""

    def __init__(self, rule: Rule, start: dt.datetime) -> None:
        self.rule = rule
        self.start = start
        freq = rule.freq
        self.level = FREQS.index(freq)
        bymonth, bymonthday, byday = rule.bymonth, rule.bymonthday, rule.byday
        if not (rule.byweekno or rule.byyearday or bymonthday or byday):
            if freq == "YEARLY":
                if not bymonth:
                    bymonth = (start.month,)
                bymonthday = (start.day,)
            elif freq == "MONTHLY":
                bymonthday = (start.day,)
            elif freq == "WEEKLY":
                byday = ((0, start.weekday()),)
        self.bymonth = frozenset(bymonth)
        self.bymonthday = frozenset(bymonthday)
        self.byyearday = frozenset(rule.byyearday)
        self.byweekno = frozenset(rule.byweekno) if freq == "YEARLY" else frozenset()
        monthly_nth = freq == "MONTHLY" or (freq == "YEARLY" and bool(rule.bymonth))
        yearly_nth = freq == "YEARLY" and not rule.bymonth
        self.weekdays = frozenset(day for ordinal, day in byday if not ordinal or not (monthly_nth or yearly_nth))
        self.nth_month = frozenset((ordinal, day) for ordinal, day in byday if ordinal and monthly_nth)
        self.nth_year = frozenset((ordinal, day) for ordinal, day in byday if ordinal and yearly_nth)
        self.has_byday = bool(byday)
        self.hours = tuple(sorted(rule.byhour)) if rule.byhour else (start.hour,)
        self.minutes = tuple(sorted(rule.byminute)) if rule.byminute else (start.minute,)
        self.seconds = tuple(sorted(rule.bysecond)) if rule.bysecond else (start.second,)
        self.byhour = frozenset(rule.byhour)
        self.byminute = frozenset(rule.byminute)
        self.bysecond = frozenset(rule.bysecond)
        self.times = [dt.time(hour, minute, second) for hour in self.hours for minute in self.minutes
                      for second in self.seconds] if self.level >= FREQS.index("DAILY") else []
        self.day_filtered = bool(self.bymonth or self.bymonthday or self.byyearday or self.byweekno or self.has_byday)

    # --- which days ---
    def day_ok(self, day: dt.date) -> bool:
        if self.bymonth and day.month not in self.bymonth:
            return False
        if self.byweekno:
            number, weeks = week_number(day, self.rule.wkst)
            if number not in self.byweekno and number - weeks - 1 not in self.byweekno:
                return False
        if self.byyearday:
            number = day.timetuple().tm_yday
            if number not in self.byyearday and number - _year_length(day.year) - 1 not in self.byyearday:
                return False
        if self.bymonthday:
            length = _calendar.monthrange(day.year, day.month)[1]
            if day.day not in self.bymonthday and day.day - length - 1 not in self.bymonthday:
                return False
        if self.has_byday:
            weekday = day.weekday()
            if weekday in self.weekdays:
                return True
            if self.nth_month:
                length = _calendar.monthrange(day.year, day.month)[1]
                if ((day.day - 1) // 7 + 1, weekday) in self.nth_month:
                    return True
                if (-((length - day.day) // 7 + 1), weekday) in self.nth_month:
                    return True
            if self.nth_year:
                number = day.timetuple().tm_yday
                if ((number - 1) // 7 + 1, weekday) in self.nth_year:
                    return True
                if (-((_year_length(day.year) - number) // 7 + 1), weekday) in self.nth_year:
                    return True
            return False
        return True

    def _pick(self, moments: list[dt.datetime]) -> list[dt.datetime]:
        positions = self.rule.bysetpos
        if not positions or not moments:
            return moments
        chosen = set()
        for position in positions:
            index = position - 1 if position > 0 else len(moments) + position
            if 0 <= index < len(moments):
                chosen.add(index)
        return [moments[index] for index in sorted(chosen)]

    def _days_times(self, days: list[dt.date]) -> list[dt.datetime]:
        moments = [dt.datetime.combine(day, moment) for day in days if self.day_ok(day) for moment in self.times]
        return self._pick(moments)

    # --- period by period ---
    def periods(self, skip_to: dt.datetime | None) -> Iterator[list[dt.datetime]]:
        """The candidate sets of the periods, in order; with ``skip_to`` the periods before it are left out."""
        rule, start = self.rule, self.start
        interval = rule.interval
        freq = rule.freq
        try:
            if freq == "YEARLY":
                index = 0
                if skip_to is not None and skip_to.year > start.year:
                    index = max(0, (skip_to.year - start.year) // interval - 1)
                while True:
                    year = start.year + index * interval
                    if year > dt.MAXYEAR:
                        return
                    first = dt.date(year, 1, 1)
                    if self.bymonth and not (self.byweekno or self.byyearday):
                        days = [dt.date(year, month, day) for month in sorted(self.bymonth)
                                for day in range(1, _calendar.monthrange(year, month)[1] + 1)]
                    else:
                        days = [first + dt.timedelta(days=offset) for offset in range(_year_length(year))]
                    yield self._days_times(days)
                    index += 1
            elif freq == "MONTHLY":
                base = start.year * 12 + start.month - 1
                index = 0
                if skip_to is not None:
                    ahead = skip_to.year * 12 + skip_to.month - 1 - base
                    index = max(0, ahead // interval - 1)
                while True:
                    year, month = divmod(base + index * interval, 12)
                    if year > dt.MAXYEAR:
                        return
                    month += 1
                    days = [dt.date(year, month, day) for day in range(1, _calendar.monthrange(year, month)[1] + 1)]
                    yield self._days_times(days)
                    index += 1
            elif freq == "WEEKLY":
                first = start.date() - dt.timedelta(days=(start.weekday() - rule.wkst) % 7)
                index = 0
                if skip_to is not None:
                    index = max(0, (skip_to.date() - first).days // (7 * interval) - 1)
                while True:
                    week = first + dt.timedelta(days=7 * interval * index)
                    yield self._days_times([week + dt.timedelta(days=offset) for offset in range(7)])
                    index += 1
            elif freq == "DAILY":
                index = 0
                if skip_to is not None:
                    index = max(0, (skip_to.date() - start.date()).days // interval - 1)
                while True:
                    yield self._days_times([start.date() + dt.timedelta(days=interval * index)])
                    index += 1
            else:
                yield from self._small_periods(skip_to)
        except (OverflowError, ValueError):
            return                                   # past the last year datetime can hold

    def _small_periods(self, skip_to: dt.datetime | None) -> Iterator[list[dt.datetime]]:
        """HOURLY, MINUTELY and SECONDLY: one candidate set per step, with whole days and hours jumped over."""
        freq = self.rule.freq
        step = {"HOURLY": 3600, "MINUTELY": 60, "SECONDLY": 1}[freq] * self.rule.interval
        if freq == "HOURLY":
            base = self.start.replace(minute=0, second=0, microsecond=0)
        elif freq == "MINUTELY":
            base = self.start.replace(second=0, microsecond=0)
        else:
            base = self.start.replace(microsecond=0)
        index = 0
        if skip_to is not None and skip_to > base:
            index = max(0, int((skip_to - base).total_seconds()) // step - 1)

        def jump(index: int, target: dt.datetime) -> int:
            """The first step at or after ``target``."""
            seconds = int((target - base).total_seconds())
            return max(index + 1, -(-seconds // step))

        while True:
            moment = base + dt.timedelta(seconds=step * index)
            if self.day_filtered and not self.day_ok(moment.date()):
                index = jump(index, dt.datetime.combine(moment.date() + dt.timedelta(days=1), dt.time()))
                yield []
                continue
            if self.byhour and moment.hour not in self.byhour:
                index = jump(index, moment.replace(minute=0, second=0) + dt.timedelta(hours=1))
                yield []
                continue
            if freq == "HOURLY":
                found = [moment.replace(minute=minute, second=second) for minute in self.minutes
                         for second in self.seconds]
            elif freq == "MINUTELY":
                found = [] if (self.byminute and moment.minute not in self.byminute) else \
                    [moment.replace(second=second) for second in self.seconds]
            else:
                found = [] if ((self.byminute and moment.minute not in self.byminute)
                               or (self.bysecond and moment.second not in self.bysecond)) else [moment]
            yield self._pick(found)
            index += 1


def _walk(rule: Rule, start: dt.datetime, skip_to: dt.datetime | None,
          within: Callable[[dt.datetime], bool], budget: int, cut: list | None = None) -> Iterator[dt.datetime]:
    """The start, then what the rule gives after it, while ``within`` holds. COUNT is not looked at.

    When the walk stops because the budget is used up (and not because the
    rule ended), True is appended to ``cut``.
    """
    yield start
    if not within(start):
        return                   # a rule that ends before it begins still has its first occurrence
    expander = _Expander(rule, start)
    most_empty = MAX_EMPTY_PERIODS.get(rule.freq, MAX_EMPTY_DEFAULT)
    empty = 0
    spent = 0
    for candidates in expander.periods(skip_to):
        produced = False
        for moment in candidates:
            if moment <= start:
                continue
            if not within(moment):
                return
            spent += 1
            if spent > budget:
                if cut is not None:
                    cut.append(True)
                return
            produced = True
            yield moment
        if produced:
            empty = 0
        else:
            empty += 1
            spent += 1
            if empty > most_empty:
                return
            if spent > budget:
                if cut is not None:
                    cut.append(True)
                return


_STEP_SECONDS = {"SECONDLY": 1, "MINUTELY": 60, "HOURLY": 3600, "DAILY": 86400, "WEEKLY": 7 * 86400}


@lru_cache(maxsize=4096)
def _count_end(rule: Rule, start: dt.datetime) -> dt.datetime | None:
    """The last occurrence of a rule with COUNT (and no UNTIL in UTC); None when the series is taken as endless.

    A rule without BYxxx parts is computed; every other rule is walked
    through once, for at most COUNT_WALK occurrences. A series longer than
    that is treated as one that never ends.
    """
    count = rule.count
    assert count is not None
    until = rule.until
    plain = not (rule.bysecond or rule.byminute or rule.byhour or rule.byday or rule.bymonthday or rule.byyearday
                 or rule.byweekno or rule.bymonth or rule.bysetpos) and until is None
    try:
        if plain and rule.freq in _STEP_SECONDS:
            return start + dt.timedelta(seconds=_STEP_SECONDS[rule.freq] * rule.interval * (count - 1))
        if plain and rule.freq == "MONTHLY" and start.day <= 28:
            months = start.year * 12 + start.month - 1 + rule.interval * (count - 1)
            return start.replace(year=months // 12, month=months % 12 + 1)
        if plain and rule.freq == "YEARLY" and (start.month, start.day) != (2, 29):
            return start.replace(year=start.year + rule.interval * (count - 1))
    except (OverflowError, ValueError):
        return None

    def within(moment: dt.datetime) -> bool:
        return until is None or moment <= until

    last = start
    seen = 0
    cut: list = []
    for moment in _walk(replace(rule, count=None), start, None, within, COUNT_WALK, cut):
        seen += 1
        last = moment
        if seen >= count:
            return last
    return None if cut else last


def iterate(rule: Rule, start: dt.datetime, *, skip_to: dt.datetime | None = None,
            instant: Callable[[dt.datetime], float] | None = None, budget: int = DEFAULT_BUDGET
            ) -> Iterator[dt.datetime]:
    """The occurrences of ``rule`` for an event that starts at ``start`` (naive local), oldest first.

    ``skip_to``: occurrences before it may be left out (they are not even
    computed). ``instant(local)`` converts a wall time to seconds since the
    epoch; it is needed to compare with an UNTIL that is written in UTC
    (without it the wall time is taken as UTC).
    """
    start = start.replace(microsecond=0)
    until = rule.until
    until_instant = until.timestamp() if until is not None and until.tzinfo is not None else None
    last: dt.datetime | None = until if until_instant is None else None
    remaining = rule.count
    if remaining is not None and until_instant is None:
        # COUNT becomes a last occurrence, computed once, so the series can be entered anywhere.
        end = _count_end(rule, start)
        remaining = None
        if end is not None:
            last = end if last is None else min(last, end)

    def within(moment: dt.datetime) -> bool:
        if until_instant is not None:
            value = instant(moment) if instant is not None else moment.replace(tzinfo=timeutil.UTC).timestamp()
            return value <= until_instant
        return last is None or moment <= last

    if remaining is None:
        yield from _walk(rule, start, skip_to, within, budget)
        return
    for moment in _walk(rule, start, None, within, budget):      # COUNT together with an UNTIL in UTC
        yield moment
        remaining -= 1
        if remaining <= 0:
            return


def between(rule: Rule, start: dt.datetime, window_start: dt.datetime, window_end: dt.datetime, *,
            instant: Callable[[dt.datetime], float] | None = None, limit: int = 5000,
            budget: int = DEFAULT_BUDGET) -> list[dt.datetime]:
    """The occurrences with ``window_start <= occurrence < window_end`` (naive local), at most ``limit``."""
    found: list[dt.datetime] = []
    for moment in iterate(rule, start, skip_to=window_start, instant=instant, budget=budget):
        if moment >= window_end:
            break
        if moment >= window_start:
            found.append(moment)
            if len(found) >= limit:
                break
    return found


def first_from(rule: Rule, start: dt.datetime, moment: dt.datetime, *,
               instant: Callable[[dt.datetime], float] | None = None,
               budget: int = DEFAULT_BUDGET) -> dt.datetime | None:
    """The first occurrence at or after ``moment``, or None."""
    for found in iterate(rule, start, skip_to=moment, instant=instant, budget=budget):
        if found >= moment:
            return found
    return None


def count_before(rule: Rule, start: dt.datetime, moment: dt.datetime, *,
                 instant: Callable[[dt.datetime], float] | None = None, budget: int = DEFAULT_BUDGET) -> int:
    """How many occurrences lie before ``moment`` (to split a series that ends after COUNT times)."""
    total = 0
    for found in iterate(rule, start, instant=instant, budget=budget):
        if found >= moment:
            break
        total += 1
    return total


# --- rules people make in the editor ---------------------------------------------------------------------

def nth_weekday(day: dt.date) -> tuple[int, int]:
    """(which one in its month, weekday): the third Tuesday is (3, 1)."""
    return (day.day - 1) // 7 + 1, day.weekday()


def is_last_weekday(day: dt.date) -> bool:
    return day.day + 7 > _calendar.monthrange(day.year, day.month)[1]


def simple_rule(kind: str, start: dt.date, *, interval: int = 1, weekdays: tuple[int, ...] = (),
                monthly: str = "date", count: int | None = None, until: dt.date | None = None) -> Rule:
    """The rule the editor makes: ``kind`` daily, weekly, monthly or yearly.

    ``weekdays`` for weekly (default: the weekday of ``start``); ``monthly``:
    ``date`` (on day 17), ``weekday`` (on the third Tuesday) or ``last`` (on
    the last Tuesday). The end: ``count`` times, until the end of the day
    ``until``, or neither (for ever).
    """
    interval = max(1, min(MAX_INTERVAL, int(interval)))
    end: dict = {}
    if count is not None:
        end["count"] = max(1, min(MAX_COUNT, int(count)))
    elif until is not None:
        end["until"] = dt.datetime(until.year, until.month, until.day, 23, 59, 59)
        end["until_is_date"] = True
    if kind == "daily":
        return Rule("DAILY", interval, **end)
    if kind == "weekly":
        days = tuple(sorted(set(weekdays))) or (start.weekday(),)
        return Rule("WEEKLY", interval, byday=tuple((0, day) for day in days), **end)
    if kind == "monthly":
        if monthly == "weekday":
            return Rule("MONTHLY", interval, byday=(nth_weekday(start),), **end)
        if monthly == "last":
            return Rule("MONTHLY", interval, byday=((-1, start.weekday()),), **end)
        return Rule("MONTHLY", interval, bymonthday=(start.day,), **end)
    if kind == "yearly":
        return Rule("YEARLY", interval, **end)
    raise RecurrenceError(f"unknown kind of repeat: {kind!r}")


@dataclass(frozen=True)
class Simple:
    """A rule in the editor's terms (see ``simple_rule``); ``kind`` is ``custom`` for everything else."""
    kind: str
    interval: int = 1
    weekdays: tuple[int, ...] = ()
    monthly: str = "date"
    count: int | None = None
    until: dt.date | None = None


def classify(rule: Rule, start: dt.date) -> Simple:
    """What the editor shows for ``rule``. ``custom`` means: keep the rule as it is, it has parts the editor lacks."""
    until = rule.until
    until_day = None
    if until is not None:
        until_day = (until.astimezone(timeutil.UTC) if until.tzinfo is not None else until).date()
    common = dict(interval=rule.interval, count=rule.count, until=until_day)
    plain = replace(rule, interval=1, count=None, until=None, until_is_date=False, wkst=0)
    if plain == Rule("DAILY"):
        return Simple("daily", **common)
    if rule.freq == "WEEKLY" and plain == Rule("WEEKLY", byday=rule.byday) and all(not n for n, _ in rule.byday):
        days = tuple(sorted(day for _, day in rule.byday)) or (start.weekday(),)
        return Simple("weekly", weekdays=days, **common)
    if rule.freq == "MONTHLY":
        if plain in (Rule("MONTHLY"), Rule("MONTHLY", bymonthday=(start.day,))):
            return Simple("monthly", monthly="date", **common)
        if plain == Rule("MONTHLY", byday=(nth_weekday(start),)):
            return Simple("monthly", monthly="weekday", **common)
        if plain == Rule("MONTHLY", byday=((-1, start.weekday()),)) and is_last_weekday(start):
            return Simple("monthly", monthly="last", **common)
    if rule.freq == "YEARLY" and plain in (Rule("YEARLY"),
                                           Rule("YEARLY", bymonth=(start.month,), bymonthday=(start.day,)),
                                           Rule("YEARLY", bymonth=(start.month,))):
        return Simple("yearly", **common)
    return Simple("custom", **common)


def describe(rule: Rule, start: dt.date) -> str:
    """The rule in words: ``Every 2 weeks on Mon, Wed until 24 Dec 2026``."""
    simple = classify(rule, start)
    unit = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}.get(simple.kind)
    if unit is None:
        return "Custom: " + format_rrule(rule)
    text = f"Every {unit}" if simple.interval == 1 else f"Every {simple.interval} {unit}s"
    if simple.kind == "weekly":
        text += " on " + ", ".join(textfmt.WEEKDAYS_SHORT[day] for day in simple.weekdays)
    elif simple.kind == "monthly":
        if simple.monthly == "date":
            text += f" on day {start.day}"
        elif simple.monthly == "last":
            text += f" on the last {textfmt.WEEKDAYS[start.weekday()]}"
        else:
            ordinal, weekday = nth_weekday(start)
            text += f" on the {_ORDINALS[ordinal - 1]} {textfmt.WEEKDAYS[weekday]}"
    elif simple.kind == "yearly":
        text += f" on {start.day} {textfmt.MONTHS[start.month - 1]}"
    if simple.count is not None:
        text += f", {textfmt.plural(simple.count, 'time')}"
    elif simple.until is not None:
        text += f", until {simple.until.day} {textfmt.MONTHS_SHORT[simple.until.month - 1]} {simple.until.year}"
    return text
