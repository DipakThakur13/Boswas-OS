"""Where things go on the calendar (standard library only): the weeks of a month, the bars of a week
row, the columns of overlapping events in a day."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from . import recurrence
from .model import DAY, Occurrence

MIN_BLOCK_MINUTES = 30          # an event is drawn at least this tall, and takes that room among its neighbours


def week_start_of(day: dt.date, first_weekday: int) -> dt.date:
    """The first day of the week ``day`` is in, for weeks that start on ``first_weekday`` (Monday is 0)."""
    return day - dt.timedelta(days=(day.weekday() - first_weekday) % 7)


def month_weeks(year: int, month: int, first_weekday: int) -> list[list[dt.date]]:
    """Six weeks of seven days that hold the month (a month grid never changes its height)."""
    first = week_start_of(dt.date(year, month, 1), first_weekday)
    return [[first + dt.timedelta(days=week * 7 + day) for day in range(7)] for week in range(6)]


def week_number(day: dt.date, first_weekday: int) -> int:
    """The number of the week ``day`` is in: ISO 8601 for weeks that start on Monday, else the first week
    with four days in the year, counted from ``first_weekday``."""
    return recurrence.week_number(day, first_weekday)[0]


def add_months(day: dt.date, months: int) -> dt.date:
    """The first day of the month ``months`` after (or before) the month of ``day``."""
    index = day.year * 12 + day.month - 1 + months
    index = max(12, min(index, 9999 * 12 + 11))
    return dt.date(index // 12, index % 12 + 1, 1)


@dataclass(frozen=True)
class Bar:
    """An occurrence in one week row of a month (or in the all-day strip of a week)."""
    occurrence: Occurrence
    first: int                 # the first column it covers
    last: int                  # the last column it covers
    row: int
    starts_before: bool        # it began before this row: draw the left end open
    ends_after: bool

    @property
    def spanning(self) -> bool:
        """Drawn as a filled bar (all-day and multi-day events) and not as a line of text."""
        return self.occurrence.multi_day


def week_bars(occurrences: list[Occurrence], days: list[dt.date], rows: int) -> tuple[list[Bar], dict[int, int]]:
    """Place the occurrences of a row of ``days`` into at most ``rows`` rows.

    Returns the bars that fit and, per column, how many occurrences do not
    (they are named by a "+N more" line, which takes the last row of that
    column). Bars that span days come first, longest first, so they stay in
    one row across the week.
    """
    first_day, last_day = days[0], days[-1]
    spans = []
    for occurrence in occurrences:
        start, end = occurrence.start.date(), occurrence.last_day
        if end < first_day or start > last_day:
            continue
        spans.append((occurrence, max(start, first_day), min(end, last_day), start < first_day, end > last_day))
    spans.sort(key=lambda item: (not item[0].multi_day, item[1], -(item[2] - item[1]).days, item[0].start_epoch,
                                 item[0].event.summary.casefold()))
    columns = len(days)
    taken: list[set[int]] = [set() for _ in range(columns)]
    count = [0] * columns
    placed: list[tuple] = []
    for occurrence, start, end, before, after in spans:
        first, last = (start - first_day).days, (end - first_day).days
        row = 0
        while any(row in taken[column] for column in range(first, last + 1)):
            row += 1
        for column in range(first, last + 1):
            taken[column].add(row)
            count[column] += 1
        placed.append((occurrence, first, last, row, before, after))
    bars: list[Bar] = []
    hidden = [0] * columns
    for occurrence, first, last, row, before, after in placed:
        # A column that has more than fits keeps its last row for the "+N more" line.
        if rows <= 0 or any(row >= rows - (1 if max(taken[column], default=-1) >= rows else 0)
                            for column in range(first, last + 1)):
            for column in range(first, last + 1):
                hidden[column] += 1
            continue
        bars.append(Bar(occurrence, first, last, row, before, after))
    return bars, {column: number for column, number in enumerate(hidden) if number}


@dataclass(frozen=True)
class Block:
    """A timed occurrence in the column of one day."""
    occurrence: Occurrence
    start: float               # minutes from midnight of that day, 0..1440
    end: float
    column: int                # which of the side-by-side columns it is in
    columns: int               # how many columns its group of overlapping events has


def day_blocks(occurrences: list[Occurrence], day: dt.date) -> list[Block]:
    """The timed occurrences that touch ``day``, laid out side by side where they overlap."""
    midnight = dt.datetime(day.year, day.month, day.day)
    items = []
    for occurrence in occurrences:
        if occurrence.all_day:
            continue
        start = (occurrence.start - midnight).total_seconds() / 60
        end = (occurrence.end - midnight).total_seconds() / 60
        if start >= 1440 or end < 0 or (end <= 0 and end != start):
            continue                    # another day; an event that ends at midnight belongs to the day before
        start = max(0.0, start)
        end = min(1440.0, max(end, start + MIN_BLOCK_MINUTES))
        if end - start < MIN_BLOCK_MINUTES:
            start = max(0.0, end - MIN_BLOCK_MINUTES)
        items.append((start, end, occurrence))
    items.sort(key=lambda item: (item[0], -item[1], item[2].event.summary.casefold()))
    blocks: list[Block] = []
    group: list[tuple[float, float, Occurrence, int]] = []
    group_end = 0.0
    column_ends: list[float] = []

    def flush() -> None:
        for start, end, occurrence, column in group:
            blocks.append(Block(occurrence, start, end, column, len(column_ends)))

    for start, end, occurrence in items:
        if group and start >= group_end:
            flush()
            group, column_ends = [], []
        for column, busy_until in enumerate(column_ends):
            if busy_until <= start:
                column_ends[column] = end
                break
        else:
            column = len(column_ends)
            column_ends.append(end)
        group.append((start, end, occurrence, column))
        group_end = max(group_end, end) if len(group) > 1 else end
    flush()
    return blocks


def all_day_part(occurrences: list[Occurrence]) -> list[Occurrence]:
    """The occurrences a week view shows in its top strip: all-day events and events longer than a day."""
    return [occurrence for occurrence in occurrences
            if occurrence.all_day or occurrence.end - occurrence.start >= DAY]
