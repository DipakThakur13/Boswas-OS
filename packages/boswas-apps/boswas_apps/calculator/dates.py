"""Date calculation: the difference between two dates, and a date plus or minus a span (no Qt).

Months are counted the way a person counts them: one month after the 31st
of January is the last day of February. The difference between two dates
is the largest number of whole months that fits when counted from the
earlier date, then the days that remain, shown as years, months, weeks and
days, together with the total in days.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

MIN_DATE, MAX_DATE = date.min, date.max
MAX_SPAN = 999                       # the largest number of years, months or days that can be added


class DateRangeError(ValueError):
    """The result lies before the year 1 or after the year 9999."""


@dataclass(frozen=True)
class DateDifference:
    years: int
    months: int
    weeks: int
    days: int
    total_days: int

    def is_zero(self) -> bool:
        return self.total_days == 0


def add_months(start: date, months: int) -> date:
    """``start`` moved by whole months; a day the target month does not have becomes its last day."""
    index = start.year * 12 + (start.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    if not MIN_DATE.year <= year <= MAX_DATE.year:
        raise DateRangeError("the date is out of range")
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def difference(first: date, second: date) -> DateDifference:
    """The span between two dates, whichever comes first."""
    start, end = (first, second) if first <= second else (second, first)
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if add_months(start, months) > end:
        months -= 1
    rest = (end - add_months(start, months)).days
    return DateDifference(months // 12, months % 12, rest // 7, rest % 7, (end - start).days)


def shift(start: date, *, years: int = 0, months: int = 0, days: int = 0, subtract: bool = False) -> date:
    """``start`` plus (or minus) years and months, then days. Raises DateRangeError outside the calendar."""
    for amount in (years, months, days):
        if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= MAX_SPAN:
            raise ValueError("a span is a whole number from 0 to 999")
    sign = -1 if subtract else 1
    moved = add_months(start, sign * (years * 12 + months))
    try:
        return moved + timedelta(days=sign * days)
    except OverflowError:
        raise DateRangeError("the date is out of range") from None


def _count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


def describe(span: DateDifference) -> str:
    """``1 year, 2 months, 3 weeks, 4 days``; ``Same dates`` for no difference."""
    if span.is_zero():
        return "Same dates"
    parts = [_count(number, word) for number, word in ((span.years, "year"), (span.months, "month"),
                                                       (span.weeks, "week"), (span.days, "day")) if number]
    return ", ".join(parts)


def describe_total(span: DateDifference) -> str:
    """``431 days``; empty when the span is less than a week (the first line says it all)."""
    if span.is_zero() or (span.years == 0 and span.months == 0 and span.weeks == 0):
        return ""
    return _count(span.total_days, "day")
