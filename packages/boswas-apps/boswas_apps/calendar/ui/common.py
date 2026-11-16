"""What the views and dialogs of Calendar share: the context, the colours of a calendar, texts."""

from __future__ import annotations

import datetime as dt
from typing import Callable

from PySide6.QtCore import QLocale, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap

import boswas_fluent as fluent

from ...reminders import textfmt, timeutil
from .. import model
from ..store import CalendarStore


class Context:
    """The store, the clock and the zone the views show times in."""

    def __init__(self, store: CalendarStore, *, now: Callable[[], float] | None = None,
                 zone: Callable[[], dt.tzinfo] = timeutil.system_zone) -> None:
        self.store = store
        self._now = now
        self.zone = zone

    def now(self) -> float:
        if self._now is not None:
            return self._now()
        return dt.datetime.now(timeutil.UTC).timestamp()

    def today(self) -> dt.date:
        return timeutil.from_epoch(self.now(), self.zone()).date()

    def now_local(self) -> dt.datetime:
        return timeutil.from_epoch(self.now(), self.zone())

    def twelve_hour(self) -> bool:
        choice = self.store.settings.time_format
        if choice in ("12", "24"):
            return choice == "12"
        return "ap" in QLocale().timeFormat(QLocale.FormatType.ShortFormat).lower()

    def first_weekday(self) -> int:
        """The day weeks start on (Monday is 0): the setting, else the locale's."""
        chosen = self.store.settings.week_start
        if 0 <= chosen <= 6:
            return chosen
        return (QLocale().firstDayOfWeek().value - 1) % 7

    def time_text(self, moment: dt.datetime | dt.time) -> str:
        return textfmt.time_of(moment, self.twelve_hour())

    def hour_text(self, hour: int) -> str:
        if self.twelve_hour():
            return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"
        return f"{hour:02d}:00"

    def edit_format(self) -> str:
        return "h:mm AP" if self.twelve_hour() else "HH:mm"

    def occurrences(self, first: dt.date, last: dt.date) -> list[model.Occurrence]:
        """What the visible calendars have from the start of ``first`` to the end of ``last``."""
        zone = self.zone()
        begin = timeutil.to_epoch(dt.datetime(first.year, first.month, first.day), zone)
        end_day = last + model.DAY
        end = timeutil.to_epoch(dt.datetime(end_day.year, end_day.month, end_day.day), zone)
        return self.store.data.occurrences(begin, end, zone, calendars=self.store.data.visible_ids())

    def span_text(self, occurrence: model.Occurrence) -> str:
        """``Tue 17 Nov 2026, 09:00 – 10:00``, ``Tue 17 Nov 2026, all day`` and the like."""
        start, end = occurrence.start, occurrence.end
        if occurrence.all_day:
            last = occurrence.last_day
            text = textfmt.short_date(start.date())
            return f"{text} – {textfmt.short_date(last)}, all day" if last > start.date() else f"{text}, all day"
        text = f"{textfmt.short_date(start.date())}, {self.time_text(start)}"
        if end > start:
            text += f" – {self.time_text(end)}" if end.date() == start.date() else \
                f" – {textfmt.short_date(end.date())}, {self.time_text(end)}"
        return text


def calendar_colors(name: str) -> tuple[QColor, QColor]:
    """(the fill behind an event of a calendar of this colour, the colour of its stripe and dot)."""
    theme = fluent.theme()
    family = name if name in model.COLORS else model.COLORS[0]
    fill = fluent.blend(theme.color("surface"), theme.color(f"{family}.500"), 0.36 if theme.is_dark else 0.2)
    return QColor(fill), QColor(theme.color(f"{family}.400" if theme.is_dark else f"{family}.600"))


def color_dot(name: str, size: int = 14) -> QPixmap:
    """A filled circle in the colour of a calendar, for lists and menus."""
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(calendar_colors(name)[1])
    painter.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    painter.end()
    return pixmap


COLOR_NAMES = {"iris": "Iris", "azure": "Azure", "mint": "Mint", "sun": "Sun", "coral": "Coral", "sky": "Sky",
               "orchid": "Orchid", "slate": "Slate"}
