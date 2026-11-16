"""The views of Calendar: month, week (also work week and day), agenda, and the small month of the pane.

The month and the week paint themselves: an event that spans days is one
bar across them, events that overlap in a day stand side by side, the
current time is a line. Both are worked with the pointer (click, double
click, drag to move, drag an edge to resize, drag across empty room to
create) and with the keyboard (arrows move the chosen day or time, Enter
makes an event there or opens the chosen event, Tab goes through the
events on show, Delete deletes).
"""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetricsF, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QListWidget, QListWidgetItem, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

import boswas_fluent as fluent

from ...reminders import textfmt
from .. import layout, model
from .common import Context, calendar_colors, color_dot

DAY = model.DAY
ROW = 22                 # the height of one event line in a month cell or in the all-day strip
HOUR = 52                # the height of an hour in the week view
GUTTER = 58              # the width of the hour labels
SNAP = 15                # minutes a dragged event snaps to
EDGE = 7                 # pixels at the lower edge of an event that resize it
OCC_ROLE = Qt.ItemDataRole.UserRole + 1


def _paint_bar(painter: QPainter, rect: QRectF, occurrence: model.Occurrence, text: str, context: Context, *,
               filled: bool, selected: bool, open_left: bool = False, open_right: bool = False) -> None:
    """One event: a filled bar with a stripe, or (not ``filled``) a dot and a line of text."""
    calendar = context.store.data.calendar(occurrence.master.calendar)
    fill, stripe = calendar_colors(calendar.color if calendar else "")
    radius = fluent.theme().radius("control")
    if filled or selected:
        fluent.paint.rounded_rect(painter, rect, radius, fill, "accent" if selected else None, 1.5)
        if not open_left:
            painter.fillRect(QRectF(rect.left() + 1, rect.top() + 3, 3, rect.height() - 6), stripe)
        text_rect = rect.adjusted(8, 0, -4, 0)
    else:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(stripe)
        painter.drawEllipse(QPointF(rect.left() + 6, rect.center().y()), 3.5, 3.5)
        painter.restore()
        text_rect = rect.adjusted(14, 0, -2, 0)
    fluent.paint.elided(painter, text_rect, text, fluent.font("caption"), "text")


class EventView(QWidget):
    """What the month and the week views have in common: the range, the chosen event, the signals."""

    activated = Signal(object)                    # an Occurrence: open it
    create_requested = Signal(object, object)     # start and end: datetimes, or dates for an all-day event
    menu_requested = Signal(object, object)       # an Occurrence, a global position
    day_requested = Signal(object)                # a date: show that day
    delete_requested = Signal(object)             # an Occurrence
    moved = Signal(object, object, object)        # an Occurrence, its new start and end (display wall times)
    day_changed = Signal(object)                  # the chosen date

    def __init__(self, context: Context, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.anchor = context.today()                   # a day of the period on show
        self.selected_key: tuple | None = None          # (uid, recurrence ID, start) of the chosen event
        self._cache: tuple | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        context.store.changed.connect(self.invalidate)
        context.store.settings_changed.connect(self.invalidate)

    # --- data ---
    def shown_days(self) -> list[dt.date]:
        raise NotImplementedError

    def invalidate(self) -> None:
        self._cache = None
        self.update()

    def occurrences(self) -> list[model.Occurrence]:
        days = self.shown_days()
        key = (self.context.store.data.revision, days[0], days[-1], self.context.zone())
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, self.context.occurrences(days[0], days[-1]))
        return self._cache[1]

    @staticmethod
    def key_of(occurrence: model.Occurrence) -> tuple:
        return occurrence.master.uid, occurrence.recurrence_id, occurrence.start

    def selected(self) -> model.Occurrence | None:
        if self.selected_key is None:
            return None
        return next((occ for occ in self.occurrences() if self.key_of(occ) == self.selected_key), None)

    def select(self, occurrence: model.Occurrence | None) -> None:
        self.selected_key = self.key_of(occurrence) if occurrence is not None else None
        self.update()

    def set_anchor(self, day: dt.date) -> None:
        if day != self.anchor:
            self.anchor = day
            self.selected_key = None
            self.invalidate()
            self.day_changed.emit(day)

    def cycle_event(self, step: int) -> bool:
        """Choose the next (or previous) event on show; False when there is none to go to."""
        occurrences = self.occurrences()
        if not occurrences:
            return False
        keys = [self.key_of(occurrence) for occurrence in occurrences]
        if self.selected_key in keys:
            index = keys.index(self.selected_key) + step
            if not 0 <= index < len(keys):
                self.select(None)
                return False
        else:
            index = 0 if step > 0 else len(keys) - 1
        self.select(occurrences[index])
        return True

    def focusNextPrevChild(self, forward: bool) -> bool:
        if self.cycle_event(1 if forward else -1):
            return True
        return super().focusNextPrevChild(forward)

    def _event_key(self, event) -> bool:
        """Keys that mean the same in every view; True when the key was used."""
        chosen = self.selected()
        key = event.key()
        if chosen is not None and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.activated.emit(chosen)
        elif chosen is not None and key == Qt.Key.Key_Delete:
            self.delete_requested.emit(chosen)
        elif chosen is not None and key == Qt.Key.Key_Menu:
            self.menu_requested.emit(chosen, self.mapToGlobal(self.rect().center()))
        elif chosen is not None and key == Qt.Key.Key_Escape:
            self.select(None)
        else:
            return False
        return True


# --- month -------------------------------------------------------------------------------------------------

class MonthView(EventView):
    HEADER = 30
    DAY_LINE = 26

    def __init__(self, context: Context, parent: QWidget | None = None) -> None:
        super().__init__(context, parent)
        self.setMinimumSize(420, 320)
        self.setAccessibleName("Month")
        self.setAccessibleDescription("Arrow keys choose a day, Enter makes an event, Tab goes through the events")
        self._hits: list[tuple[QRectF, object]] = []     # (rectangle, Occurrence or ("more", date))
        self._drag: dict | None = None

    def weeks(self) -> list[list[dt.date]]:
        return layout.month_weeks(self.anchor.year, self.anchor.month, self.context.first_weekday())

    def shown_days(self) -> list[dt.date]:
        weeks = self.weeks()
        return [weeks[0][0], weeks[-1][-1]]

    def title(self) -> str:
        return f"{textfmt.MONTHS[self.anchor.month - 1]} {self.anchor.year}"

    def step(self, count: int) -> None:
        target = layout.add_months(self.anchor, count)
        import calendar as _calendar
        day = min(self.anchor.day, _calendar.monthrange(target.year, target.month)[1])
        self.set_anchor(target.replace(day=day))

    # --- geometry ---
    def _cell(self) -> tuple[float, float]:
        return self.width() / 7, max(1.0, (self.height() - self.HEADER) / 6)

    def cell_rect(self, week: int, column: int) -> QRectF:
        width, height = self._cell()
        return QRectF(column * width, self.HEADER + week * height, width, height)

    def day_at(self, position: QPointF | QPoint) -> dt.date | None:
        width, height = self._cell()
        if position.y() < self.HEADER or not 0 <= position.x() < self.width():
            return None
        week, column = int((position.y() - self.HEADER) // height), int(position.x() // width)
        weeks = self.weeks()
        return weeks[week][column] if 0 <= week < 6 and 0 <= column < 7 else None

    def rows_fit(self) -> int:
        return max(0, int((self._cell()[1] - self.DAY_LINE - 2) // ROW))

    def hit(self, position: QPointF | QPoint):
        point = QPointF(position)
        for rect, target in reversed(self._hits):
            if rect.contains(point):
                return target
        return None

    def rect_of(self, occurrence: model.Occurrence) -> QRectF | None:
        """Where an occurrence was painted last (its first bar), for tests and for placing a flyout."""
        key = self.key_of(occurrence)
        for rect, target in self._hits:
            if isinstance(target, model.Occurrence) and self.key_of(target) == key:
                return rect
        return None

    # --- painting ---
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        self.paint(painter, QRectF(self.rect()), interactive=True)

    def paint(self, painter: QPainter, area: QRectF, *, interactive: bool = False) -> None:
        """Paint the month into ``area`` (the widget, or a page of the printer)."""
        context = self.context
        weeks = self.weeks()
        today = context.today()
        width, height = area.width() / 7, max(1.0, (area.height() - self.HEADER) / 6)
        rows = max(0, int((height - self.DAY_LINE - 2) // ROW))
        occurrences = self.occurrences()
        caption = fluent.font("caption")
        hits: list[tuple[QRectF, object]] = []
        painter.setFont(caption)
        for column, day in enumerate(weeks[0]):
            painter.setPen(fluent.qcolor("text-secondary"))
            painter.drawText(QRectF(area.left() + column * width + 8, area.top(), width - 12, self.HEADER),
                             int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                             textfmt.WEEKDAYS_SHORT[day.weekday()])
        drag_days = self._drag_range() if interactive else None
        for week, days in enumerate(weeks):
            top = area.top() + self.HEADER + week * height
            for column, day in enumerate(days):
                cell = QRectF(area.left() + column * width, top, width, height)
                if day.month == self.anchor.month:
                    painter.fillRect(cell.adjusted(0.5, 0.5, -0.5, -0.5), fluent.qcolor("layer"))
                if drag_days is not None and drag_days[0] <= day <= drag_days[1]:
                    painter.fillRect(cell.adjusted(1, 1, -1, -1), fluent.qcolor("accent-subtle"))
                painter.setPen(QPen(fluent.qcolor("stroke-divider"), 1))
                painter.drawRect(cell.adjusted(0, 0, -0.5, -0.5))
                if interactive and day == self.anchor:
                    fluent.paint.rounded_rect(painter, cell.adjusted(1.5, 1.5, -1.5, -1.5), 3, None, "accent", 1.5)
                number = str(day.day) if day.day != 1 else f"{day.day} {textfmt.MONTHS_SHORT[day.month - 1]}"
                number_rect = QRectF(cell.left() + 6, cell.top() + 3, max(22.0, QFontMetricsF(caption)
                                     .horizontalAdvance(number) + 10), 20)
                if day == today:
                    fluent.paint.rounded_rect(painter, number_rect, 10, "accent")
                    painter.setPen(fluent.qcolor("on-accent"))
                else:
                    painter.setPen(fluent.qcolor("text" if day.month == self.anchor.month else "text-muted"))
                painter.setFont(caption)
                painter.drawText(number_rect, int(Qt.AlignmentFlag.AlignCenter), number)
            bars, more = layout.week_bars(occurrences, days, rows)
            for bar in bars:
                occurrence = bar.occurrence
                rect = QRectF(area.left() + bar.first * width + 3, top + self.DAY_LINE + bar.row * ROW,
                              (bar.last - bar.first + 1) * width - 6, ROW - 2)
                text = occurrence.event.title()
                if not occurrence.all_day and not bar.starts_before:
                    text = f"{context.time_text(occurrence.start)} {text}"
                _paint_bar(painter, rect, occurrence, text, context, filled=bar.spanning,
                           selected=interactive and self.key_of(occurrence) == self.selected_key,
                           open_left=bar.starts_before, open_right=bar.ends_after)
                hits.append((rect, occurrence))
            for column, count in more.items():
                rect = QRectF(area.left() + column * width + 3, top + self.DAY_LINE + max(0, rows - 1) * ROW,
                              width - 6, ROW - 2)
                if rows == 0:
                    rect = QRectF(area.left() + column * width + width - 44, top + 3, 40, 20)
                fluent.paint.elided(painter, rect.adjusted(6, 0, 0, 0), f"+{count} more" if rows else f"+{count}",
                                    caption, "accent-text")
                hits.append((rect, ("more", days[column])))
        if interactive:
            self._hits = hits

    # --- pointer ---
    def _drag_range(self) -> tuple[dt.date, dt.date] | None:
        drag = self._drag
        if drag is None or drag["kind"] != "range" or drag["day"] == drag["origin"]:
            return None
        return min(drag["origin"], drag["day"]), max(drag["origin"], drag["day"])

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        target = self.hit(event.position())
        day = self.day_at(event.position())
        if isinstance(target, model.Occurrence):
            self.select(target)
            if day is not None:
                self._drag = {"kind": "event", "occurrence": target, "origin": day, "day": day}
        elif isinstance(target, tuple):
            self.day_requested.emit(target[1])
        elif day is not None:
            self.select(None)
            self._set_day(day)
            self._drag = {"kind": "range", "origin": day, "day": day}

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            return
        day = self.day_at(event.position())
        if day is not None and day != self._drag["day"]:
            self._drag["day"] = day
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        drag, self._drag = self._drag, None
        if drag is None or event.button() != Qt.MouseButton.LeftButton:
            return
        self.update()
        days = (drag["day"] - drag["origin"]).days
        if drag["kind"] == "event" and days:
            occurrence = drag["occurrence"]
            shift = dt.timedelta(days=days)
            self.moved.emit(occurrence, occurrence.start + shift, occurrence.end + shift)
        elif drag["kind"] == "range" and days:
            first, last = sorted((drag["origin"], drag["day"]))
            self.create_requested.emit(first, last + DAY)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag = None
        target = self.hit(event.position())
        day = self.day_at(event.position())
        if isinstance(target, model.Occurrence):
            self.activated.emit(target)
        elif target is None and day is not None:
            self.create_requested.emit(day, None)

    def contextMenuEvent(self, event) -> None:
        target = self.hit(event.pos())
        if isinstance(target, model.Occurrence):
            self.select(target)
            self.menu_requested.emit(target, event.globalPos())

    # --- keyboard ---
    def _set_day(self, day: dt.date) -> None:
        if day != self.anchor:
            self.anchor = day
            self.invalidate()
            self.day_changed.emit(day)

    def keyPressEvent(self, event) -> None:
        if self._event_key(event):
            return
        steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -7, Qt.Key.Key_Down: 7}
        key = event.key()
        try:
            if key in steps:
                self._set_day(self.anchor + dt.timedelta(days=steps[key]))
            elif key == Qt.Key.Key_PageUp:
                self.step(-1)
            elif key == Qt.Key.Key_PageDown:
                self.step(1)
            elif key == Qt.Key.Key_Home:
                self._set_day(self.context.today())
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.create_requested.emit(self.anchor, None)
            else:
                super().keyPressEvent(event)
        except OverflowError:
            pass


# --- week, work week, day ----------------------------------------------------------------------------------

class _WeekHeader(QWidget):
    """The names of the days and, under them, the strip of all-day events."""

    NAMES = 44

    def __init__(self, view: "WeekView") -> None:
        super().__init__(view)
        self.view = view
        self._hits: list[tuple[QRectF, object]] = []
        self.rows = 0
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def sizeHint(self) -> QSize:
        return QSize(400, self.NAMES + self.rows * ROW + 6)

    def refresh(self) -> None:
        days = self.view.shown_days()
        strip = layout.all_day_part(self.view.occurrences())
        bars, _more = layout.week_bars(strip, days, 99)
        rows = min(4, max((bar.row for bar in bars), default=-1) + 1)
        if rows != self.rows:
            self.rows = rows
            self.setFixedHeight(self.NAMES + rows * ROW + 6)
        self.update()

    def column_at(self, x: float) -> int | None:
        days = self.view.shown_days()
        width = (self.width() - GUTTER) / len(days)
        column = int((x - GUTTER) // width) if x >= GUTTER else -1
        return column if 0 <= column < len(days) else None

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        self.paint(painter, QRectF(self.rect()), interactive=True)

    def paint(self, painter: QPainter, area: QRectF, *, interactive: bool = False) -> None:
        view, context = self.view, self.view.context
        days = view.shown_days()
        today = context.today()
        width = (area.width() - GUTTER) / len(days)
        hits: list[tuple[QRectF, object]] = []
        for column, day in enumerate(days):
            left = area.left() + GUTTER + column * width
            painter.setFont(fluent.font("caption"))
            painter.setPen(fluent.qcolor("accent-text" if day == today else "text-secondary"))
            painter.drawText(QRectF(left + 8, area.top() + 2, width - 10, 16), int(Qt.AlignmentFlag.AlignLeft),
                             textfmt.WEEKDAYS_SHORT[day.weekday()] if len(days) > 1 else textfmt.WEEKDAYS[day.weekday()])
            painter.setFont(fluent.font("subtitle"))
            painter.setPen(fluent.qcolor("accent-text" if day == today else "text"))
            painter.drawText(QRectF(left + 8, area.top() + 16, width - 10, 26), int(Qt.AlignmentFlag.AlignLeft),
                             str(day.day))
            painter.setPen(QPen(fluent.qcolor("stroke-divider"), 1))
            painter.drawLine(QPointF(left, area.top() + self.NAMES), QPointF(left, area.bottom()))
        strip = layout.all_day_part(view.occurrences())
        bars, more = layout.week_bars(strip, days, 4 if len(strip) > 4 else 99)
        for bar in bars:
            if bar.row >= 4:
                continue
            rect = QRectF(area.left() + GUTTER + bar.first * width + 2, area.top() + self.NAMES + bar.row * ROW,
                          (bar.last - bar.first + 1) * width - 4, ROW - 2)
            occurrence = bar.occurrence
            _paint_bar(painter, rect, occurrence, occurrence.event.title(), context, filled=True,
                       selected=interactive and view.key_of(occurrence) == view.selected_key,
                       open_left=bar.starts_before, open_right=bar.ends_after)
            hits.append((rect, occurrence))
        for column, count in more.items():
            rect = QRectF(area.left() + GUTTER + column * width + 2, area.top() + self.NAMES + 3 * ROW, width - 4,
                          ROW - 2)
            fluent.paint.elided(painter, rect.adjusted(6, 0, 0, 0), f"+{count} more", fluent.font("caption"),
                                "accent-text")
            hits.append((rect, ("more", days[column])))
        painter.setPen(QPen(fluent.qcolor("stroke-divider"), 1))
        painter.drawLine(QPointF(area.left(), area.bottom() - 0.5), QPointF(area.right(), area.bottom() - 0.5))
        if interactive:
            self._hits = hits

    def hit(self, position):
        for rect, target in reversed(self._hits):
            if rect.contains(QPointF(position)):
                return target
        return None

    def mousePressEvent(self, event) -> None:
        self.view.setFocus(Qt.FocusReason.MouseFocusReason)
        target = self.hit(event.position())
        if isinstance(target, model.Occurrence):
            self.view.select(target)
        elif isinstance(target, tuple):
            self.view.day_requested.emit(target[1])
        else:
            self.view.select(None)

    def mouseDoubleClickEvent(self, event) -> None:
        target = self.hit(event.position())
        column = self.column_at(event.position().x())
        if isinstance(target, model.Occurrence):
            self.view.activated.emit(target)
        elif target is None and column is not None:
            day = self.view.shown_days()[column]
            self.view.create_requested.emit(day, day + DAY)

    def contextMenuEvent(self, event) -> None:
        target = self.hit(event.pos())
        if isinstance(target, model.Occurrence):
            self.view.select(target)
            self.view.menu_requested.emit(target, event.globalPos())


class _WeekGrid(QWidget):
    """The hours of the days, with the timed events."""

    def __init__(self, view: "WeekView") -> None:
        super().__init__(view)
        self.view = view
        self._hits: list[tuple[QRectF, model.Occurrence]] = []
        self._drag: dict | None = None
        self.setFixedHeight(24 * HOUR)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    # --- geometry ---
    def column_width(self) -> float:
        return (self.width() - GUTTER) / len(self.view.shown_days())

    def moment_at(self, position, snap: int = SNAP) -> dt.datetime | None:
        """The wall time under a point, snapped to ``snap`` minutes."""
        days = self.view.shown_days()
        column = int((position.x() - GUTTER) // self.column_width()) if position.x() >= GUTTER else -1
        if not 0 <= column < len(days):
            return None
        minutes = max(0, min(24 * 60, int(position.y() / HOUR * 60)))
        minutes = min(24 * 60 - snap, minutes // snap * snap)
        day = days[column]
        return dt.datetime(day.year, day.month, day.day) + dt.timedelta(minutes=minutes)

    def block_rect(self, block: layout.Block, column: int, left: float = 0.0, width: float | None = None) -> QRectF:
        column_width = self.column_width() if width is None else width
        room = column_width - 10
        x = left + GUTTER + column * column_width + 2 + block.column * room / block.columns
        return QRectF(x, block.start / 60 * HOUR + 1, room / block.columns - 2,
                      max(18.0, (block.end - block.start) / 60 * HOUR - 2))

    def rect_of(self, occurrence: model.Occurrence) -> QRectF | None:
        key = self.view.key_of(occurrence)
        for rect, target in self._hits:
            if self.view.key_of(target) == key:
                return rect
        return None

    def hit(self, position) -> model.Occurrence | None:
        for rect, target in reversed(self._hits):
            if rect.contains(QPointF(position)):
                return target
        return None

    # --- painting ---
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        self.paint(painter, QRectF(self.rect()), interactive=True)

    def paint(self, painter: QPainter, area: QRectF, *, interactive: bool = False, first_hour: int = 0,
              last_hour: int = 24) -> None:
        """Paint the hours ``first_hour`` to ``last_hour`` into ``area`` (the widget shows all 24)."""
        view, context = self.view, self.view.context
        settings = context.store.settings
        days = view.shown_days()
        hour = area.height() / (last_hour - first_hour)
        scale = hour / HOUR
        width = (area.width() - GUTTER) / len(days)
        today = context.today()
        occurrences = [occ for occ in view.occurrences() if not (occ.all_day or occ.end - occ.start >= DAY)]
        painter.setFont(fluent.font("caption"))

        def y_of(minutes: float) -> float:
            return area.top() + (minutes / 60 - first_hour) * hour

        for column, day in enumerate(days):
            left = area.left() + GUTTER + column * width
            working = day.weekday() in settings.work_days
            start = max(first_hour, settings.work_start) if working else last_hour
            end = min(last_hour, settings.work_end) if working else last_hour
            if start < end:
                painter.fillRect(QRectF(left, y_of(start * 60), width, (end - start) * hour), fluent.qcolor("layer"))
            painter.setPen(QPen(fluent.qcolor("stroke-divider"), 1))
            painter.drawLine(QPointF(left, area.top()), QPointF(left, area.bottom()))
        for number in range(first_hour, last_hour):
            y = y_of(number * 60)
            painter.setPen(QPen(fluent.qcolor("stroke-divider"), 1))
            painter.drawLine(QPointF(area.left() + GUTTER - 6, y), QPointF(area.right(), y))
            if number > first_hour:
                painter.setPen(fluent.qcolor("text-secondary"))
                painter.drawText(QRectF(area.left(), y - 9, GUTTER - 10, 18),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                                 context.hour_text(number))
        slot = view.slot if interactive else None
        if slot is not None and slot.date() in days and view.selected_key is None:
            column = days.index(slot.date())
            minutes = slot.hour * 60 + slot.minute
            fluent.paint.rounded_rect(painter, QRectF(area.left() + GUTTER + column * width + 1, y_of(minutes) + 1,
                                                      width - 2, hour / 2 - 2), 3, "accent-subtle", "accent", 1.5)
        drag = self._drag if interactive else None
        if drag is not None and drag["kind"] == "range" and drag["end"] != drag["start"]:
            begin, finish = sorted((drag["start"], drag["end"]))
            if begin.date() in days:
                column = days.index(begin.date())
                top = y_of(begin.hour * 60 + begin.minute)
                bottom = y_of((finish - dt.datetime.combine(begin.date(), dt.time())).total_seconds() / 60)
                fluent.paint.rounded_rect(painter, QRectF(area.left() + GUTTER + column * width + 1, top,
                                                          width - 2, max(4.0, bottom - top)), 3, "accent-subtle",
                                          "accent", 1.5)
        hits: list[tuple[QRectF, model.Occurrence]] = []
        for column, day in enumerate(days):
            for block in layout.day_blocks(occurrences, day):
                occurrence = block.occurrence
                shown = block
                if drag is not None and drag["kind"] in ("move", "resize") \
                        and view.key_of(drag["occurrence"]) == view.key_of(occurrence):
                    continue                        # painted below, where it is being dragged to
                room = width - 10
                rect = QRectF(area.left() + GUTTER + column * width + 2 + shown.column * room / shown.columns,
                              y_of(shown.start) + 1, room / shown.columns - 2,
                              max(18.0 * scale, (shown.end - shown.start) / 60 * hour - 2))
                if rect.bottom() < area.top() or rect.top() > area.bottom():
                    continue
                self._paint_block(painter, rect, occurrence, interactive)
                hits.append((rect, occurrence))
        if drag is not None and drag["kind"] in ("move", "resize"):
            start, end = drag["start"], drag["end"]
            if start.date() in days:
                column = days.index(start.date())
                minutes = start.hour * 60 + start.minute
                length = max(SNAP, (end - start).total_seconds() / 60)
                rect = QRectF(area.left() + GUTTER + column * width + 2, y_of(minutes) + 1, width - 12,
                              max(18.0, min(length, 24 * 60 - minutes) / 60 * hour - 2))
                self._paint_block(painter, rect, drag["occurrence"], True, label=(
                    f"{context.time_text(start)} – {context.time_text(end)}"))
        if today in days:
            now = context.now_local()
            y = y_of(now.hour * 60 + now.minute + now.second / 60)
            if area.top() <= y <= area.bottom():
                left = area.left() + GUTTER + days.index(today) * width
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(QPen(fluent.qcolor("accent"), 2))
                painter.drawLine(QPointF(left, y), QPointF(left + width, y))
                painter.setBrush(fluent.qcolor("accent"))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QPointF(left, y), 4.5, 4.5)
        if interactive:
            self._hits = hits

    def _paint_block(self, painter: QPainter, rect: QRectF, occurrence: model.Occurrence, interactive: bool,
                     label: str | None = None) -> None:
        view, context = self.view, self.view.context
        calendar = context.store.data.calendar(occurrence.master.calendar)
        fill, stripe = calendar_colors(calendar.color if calendar else "")
        selected = interactive and view.key_of(occurrence) == view.selected_key
        fluent.paint.rounded_rect(painter, rect, fluent.theme().radius("control"), fill,
                                  "accent" if selected else "stroke-surface", 1.5 if selected else 1)
        painter.fillRect(QRectF(rect.left() + 1, rect.top() + 3, 3, rect.height() - 6), stripe)
        caption = fluent.font("caption")
        line = QFontMetricsF(caption).height()
        text = rect.adjusted(8, 2, -3, -2)
        fluent.paint.elided(painter, QRectF(text.left(), text.top(), text.width(), line), occurrence.event.title(),
                            fluent.font("caption"), "text")
        details = [label or f"{context.time_text(occurrence.start)} – {context.time_text(occurrence.end)}"]
        if occurrence.event.location:
            details.append(occurrence.event.location)
        for index, detail in enumerate(details, 1):
            if text.height() >= line * (index + 1) - 2:
                fluent.paint.elided(painter, QRectF(text.left(), text.top() + line * index, text.width(), line),
                                    detail, caption, "text-secondary")

    # --- pointer ---
    def mousePressEvent(self, event) -> None:
        view = self.view
        view.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position = event.position()
        target = self.hit(position)
        moment = self.moment_at(position)
        if target is not None:
            view.select(target)
            rect = self.rect_of(target)
            if moment is not None and rect is not None:
                kind = "resize" if position.y() >= rect.bottom() - EDGE and rect.height() > 2 * EDGE else "move"
                self._drag = {"kind": kind, "occurrence": target, "origin": moment, "start": target.start,
                              "end": target.end, "moved": False}
        elif moment is not None:
            view.select(None)
            slot = self.moment_at(position, 30)
            view.set_slot(slot)
            self._drag = {"kind": "range", "start": slot, "end": slot, "moved": False}

    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        drag = self._drag
        if drag is None:
            target = self.hit(position)
            rect = self.rect_of(target) if target is not None else None
            edge = rect is not None and position.y() >= rect.bottom() - EDGE and rect.height() > 2 * EDGE
            self.setCursor(Qt.CursorShape.SizeVerCursor if edge else Qt.CursorShape.ArrowCursor)
            return
        if drag["kind"] == "range":
            moment = self.moment_at(position, 30)
            if moment is not None and moment.date() == drag["start"].date():
                end = moment + dt.timedelta(minutes=30) if moment >= drag["start"] else moment
                if end != drag["end"]:
                    drag["end"], drag["moved"] = end, True
                    self.update()
            return
        moment = self.moment_at(position)
        if moment is None:
            return
        occurrence = drag["occurrence"]
        if drag["kind"] == "move":
            shift = moment - drag["origin"]
            start, end = occurrence.start + shift, occurrence.end + shift
        else:
            start = occurrence.start
            end = max(start + dt.timedelta(minutes=SNAP), moment + dt.timedelta(minutes=SNAP))
            if end.date() > start.date() and end.time() != dt.time(0):
                end = dt.datetime.combine(start.date() + DAY, dt.time(0))
        if (start, end) != (drag["start"], drag["end"]):
            drag.update(start=start, end=end, moved=True)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        drag, self._drag = self._drag, None
        if drag is None or event.button() != Qt.MouseButton.LeftButton:
            return
        self.update()
        if not drag["moved"]:
            return
        if drag["kind"] == "range":
            begin, finish = sorted((drag["start"], drag["end"]))
            if finish > begin:
                self.view.create_requested.emit(begin, finish)
        else:
            occurrence = drag["occurrence"]
            if (drag["start"], drag["end"]) != (occurrence.start, occurrence.end):
                self.view.moved.emit(occurrence, drag["start"], drag["end"])

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag = None
        target = self.hit(event.position())
        if target is not None:
            self.view.activated.emit(target)
            return
        moment = self.moment_at(event.position(), 30)
        if moment is not None:
            self.view.create_requested.emit(moment, None)

    def contextMenuEvent(self, event) -> None:
        target = self.hit(event.pos())
        if target is not None:
            self.view.select(target)
            self.view.menu_requested.emit(target, event.globalPos())


class WeekView(EventView):
    """Seven days, the working days, or one day: columns of hours under a strip of all-day events."""

    def __init__(self, context: Context, parent: QWidget | None = None) -> None:
        super().__init__(context, parent)
        self.mode = "week"                               # week, workweek or day
        self.slot: dt.datetime | None = None             # the chosen half hour
        self.setAccessibleName("Week")
        self.setAccessibleDescription("Arrow keys choose a time, Enter makes an event, Tab goes through the events")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = _WeekHeader(self)
        self.grid = _WeekGrid(self)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setWidget(self.grid)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.addWidget(self.header)
        self._header_row = header_row
        outer.addLayout(header_row)
        outer.addWidget(self.scroll, 1)
        self._scrolled = False
        self._minute = QTimer(self)                      # moves the line of the current time
        self._minute.setSingleShot(True)
        self._minute.timeout.connect(self._minute_passed)
        self.setMinimumSize(420, 320)

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.setAccessibleName({"week": "Week", "workweek": "Work week", "day": "Day"}[mode])
            self.invalidate()

    def shown_days(self) -> list[dt.date]:
        if self.mode == "day":
            return [self.anchor]
        first = layout.week_start_of(self.anchor, self.context.first_weekday())
        days = [first + dt.timedelta(days=offset) for offset in range(7)]
        if self.mode == "workweek":
            working = [day for day in days if day.weekday() in self.context.store.settings.work_days]
            return working or days
        return days

    def title(self) -> str:
        days = self.shown_days()
        first, last = days[0], days[-1]
        if first == last:
            return textfmt.long_date(first)
        if first.month == last.month:
            return f"{first.day} – {last.day} {textfmt.MONTHS[last.month - 1]} {last.year}"
        if first.year == last.year:
            return (f"{first.day} {textfmt.MONTHS_SHORT[first.month - 1]} – {last.day} "
                    f"{textfmt.MONTHS_SHORT[last.month - 1]} {last.year}")
        return (f"{first.day} {textfmt.MONTHS_SHORT[first.month - 1]} {first.year} – {last.day} "
                f"{textfmt.MONTHS_SHORT[last.month - 1]} {last.year}")

    def step(self, count: int) -> None:
        try:
            self.set_anchor(self.anchor + dt.timedelta(days=count * (1 if self.mode == "day" else 7)))
        except OverflowError:
            pass

    def invalidate(self) -> None:
        super().invalidate()
        self.header.refresh()
        self.grid.update()

    def update(self) -> None:                             # noqa: A003 - repaint the parts with the whole
        super().update()
        self.header.update()
        self.grid.update()

    def set_slot(self, slot: dt.datetime | None) -> None:
        self.slot = slot
        if slot is not None and slot.date() not in self.shown_days():
            self.set_anchor(slot.date())
        self.update()

    def scroll_to_hour(self, hour: float) -> None:
        self.scroll.verticalScrollBar().setValue(int(hour * HOUR))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.header.refresh()
        if not self._scrolled:
            self._scrolled = True
            QTimer.singleShot(0, self, lambda: self.scroll_to_hour(max(0, self.context.store.settings.work_start - 1)))
        self._minute_passed()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._minute.stop()                              # nothing ticks while the view is hidden

    def _minute_passed(self) -> None:
        self.grid.update()
        if self.isVisible():
            self._minute.start(int((60 - self.context.now() % 60) * 1000) + 50)

    def _align_header(self) -> None:
        # The header lines up with the grid, which is narrower by the scroll bar.
        self._header_row.setContentsMargins(0, 0, max(0, self.width() - self.grid.width()), 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._align_header()
        QTimer.singleShot(0, self, self._align_header)

    def keyPressEvent(self, event) -> None:
        if self._event_key(event):
            return
        key = event.key()
        days = self.shown_days()
        slot = self.slot
        if slot is None or slot.date() not in days:
            day = self.anchor if self.anchor in days else days[0]
            slot = dt.datetime(day.year, day.month, day.day, self.context.store.settings.work_start)
        try:
            if key == Qt.Key.Key_Up:
                slot = max(dt.datetime.combine(slot.date(), dt.time(0)), slot - dt.timedelta(minutes=30))
            elif key == Qt.Key.Key_Down:
                slot = min(dt.datetime.combine(slot.date(), dt.time(23, 30)), slot + dt.timedelta(minutes=30))
            elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                index = days.index(slot.date()) + (1 if key == Qt.Key.Key_Right else -1)
                if 0 <= index < len(days):
                    slot = dt.datetime.combine(days[index], slot.time())
                else:
                    self.step(1 if index > 0 else -1)
                    fresh = self.shown_days()
                    slot = dt.datetime.combine(fresh[0] if index > 0 else fresh[-1], slot.time())
            elif key == Qt.Key.Key_PageUp:
                self.step(-1)
                return
            elif key == Qt.Key.Key_PageDown:
                self.step(1)
                return
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.create_requested.emit(slot, None)
                return
            else:
                super().keyPressEvent(event)
                return
        except OverflowError:
            return
        self.select(None)
        self.set_slot(slot)
        self.scroll.ensureVisible(0, int((slot.hour + slot.minute / 60) * HOUR), 0, HOUR)


# --- agenda ------------------------------------------------------------------------------------------------

class AgendaView(QWidget):
    """A list of what comes, day by day; also the list of search results."""

    activated = Signal(object)
    menu_requested = Signal(object, object)
    delete_requested = Signal(object)
    create_requested = Signal(object, object)
    DAYS = 30

    def __init__(self, context: Context, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.anchor = context.today()
        self.query: str | None = None                    # not None: show the events that match it
        layout_ = QVBoxLayout(self)
        layout_.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget(self)
        self.list.setAccessibleName("Agenda")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setIconSize(QSize(14, 14))
        self.list.itemActivated.connect(self._activated)
        self.list.itemDoubleClicked.connect(self._activated)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.empty = fluent.EmptyState("Nothing planned", "There are no events in these days.",
                                       "x-office-calendar-symbolic", "New event")
        self.empty.action_triggered.connect(lambda: self.create_requested.emit(self.anchor, None))
        layout_.addWidget(self.list, 1)
        layout_.addWidget(self.empty, 1)
        self.setFocusProxy(self.list)
        context.store.changed.connect(self.refresh)
        context.store.settings_changed.connect(self.refresh)
        self.refresh()

    def shown_days(self) -> list[dt.date]:
        return [self.anchor, self.anchor + dt.timedelta(days=self.DAYS - 1)]

    def title(self) -> str:
        if self.query is not None:
            return f"Results for “{self.query}”"
        first, last = self.shown_days()
        return (f"{first.day} {textfmt.MONTHS_SHORT[first.month - 1]} – {last.day} "
                f"{textfmt.MONTHS_SHORT[last.month - 1]} {last.year}")

    def set_anchor(self, day: dt.date) -> None:
        if day != self.anchor:
            self.anchor = day
            self.refresh()

    def step(self, count: int) -> None:
        try:
            self.set_anchor(self.anchor + dt.timedelta(days=count * self.DAYS))
        except OverflowError:
            pass

    def set_query(self, query: str | None) -> None:
        self.query = query
        self.refresh()

    def occurrences(self) -> list[model.Occurrence]:
        if self.query is not None:
            visible = self.context.store.data.visible_ids()
            return [occ for occ in self.context.store.data.search(self.query, self.context.zone(), self.context.now())
                    if occ.master.calendar in visible]
        first, last = self.shown_days()
        return self.context.occurrences(first, last)

    def refresh(self) -> None:
        chosen = self.selected()
        chosen_key = EventView.key_of(chosen) if chosen is not None else None
        self.list.clear()
        context = self.context
        today = context.today()
        occurrences = self.occurrences()
        current_day = None
        first_day = self.anchor if self.query is None else dt.date.min
        for occurrence in occurrences:
            day = max(occurrence.start.date(), first_day)
            if day != current_day:
                current_day = day
                header = QListWidgetItem(textfmt.long_date(day) + (" · Today" if day == today else ""))
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                header.setFont(fluent.font("body-strong"))
                self.list.addItem(header)
            if occurrence.all_day:
                when = "All day"
            elif occurrence.start.date() < day:
                when = f"until {context.time_text(occurrence.end)}"
            else:
                when = f"{context.time_text(occurrence.start)} – {context.time_text(occurrence.end)}"
            text = f"{when}    {occurrence.event.title()}"
            if occurrence.event.location:
                text += f"  ·  {occurrence.event.location}"
            if occurrence.recurring:
                text += "  ·  repeats"
            item = QListWidgetItem(text)
            calendar = context.store.data.calendar(occurrence.master.calendar)
            item.setIcon(QIcon(color_dot(calendar.color if calendar else "")))
            item.setData(OCC_ROLE, occurrence)
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, calendar.name if calendar else "")
            self.list.addItem(item)
            if chosen_key is not None and EventView.key_of(occurrence) == chosen_key:
                self.list.setCurrentItem(item)
        self.list.setVisible(bool(occurrences))
        self.empty.setVisible(not occurrences)
        if self.query is not None:
            self.empty.set_title("No events found")
            self.empty.set_text("No event has all of these words in its title, place or notes.")
        else:
            self.empty.set_title("Nothing planned")
            self.empty.set_text("There are no events in these days.")
        if self.empty.action_button is not None:
            self.empty.action_button.setVisible(self.query is None)

    def items(self) -> list[model.Occurrence]:
        return [self.list.item(row).data(OCC_ROLE) for row in range(self.list.count())
                if self.list.item(row).data(OCC_ROLE) is not None]

    def selected(self) -> model.Occurrence | None:
        item = self.list.currentItem()
        return item.data(OCC_ROLE) if item is not None else None

    def select(self, occurrence: model.Occurrence | None) -> None:
        for row in range(self.list.count()):
            data = self.list.item(row).data(OCC_ROLE)
            if data is not None and occurrence is not None and EventView.key_of(data) == EventView.key_of(occurrence):
                self.list.setCurrentRow(row)
                return

    def _activated(self, item: QListWidgetItem) -> None:
        occurrence = item.data(OCC_ROLE)
        if occurrence is not None:
            self.activated.emit(occurrence)

    def _menu(self, position) -> None:
        item = self.list.itemAt(position)
        if item is not None and item.data(OCC_ROLE) is not None:
            self.list.setCurrentItem(item)
            self.menu_requested.emit(item.data(OCC_ROLE), self.list.viewport().mapToGlobal(position))

    def keyPressEvent(self, event) -> None:
        chosen = self.selected()
        if chosen is not None and event.key() == Qt.Key.Key_Delete:
            self.delete_requested.emit(chosen)
        else:
            super().keyPressEvent(event)


# --- the small month of the pane ---------------------------------------------------------------------------

class MiniMonth(QWidget):
    """A month to pick a day from, with the numbers of the weeks."""

    date_selected = Signal(object)
    CELL = 32
    HEAD = 36

    def __init__(self, context: Context, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.month = context.today().replace(day=1)
        self.selected = context.today()
        self.shown: tuple[dt.date, dt.date] | None = None     # the days the main view shows
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Pick a date")
        self.previous_button = fluent.IconButton("go-up-symbolic", "Previous month", self, size=28, icon_size=14)
        self.next_button = fluent.IconButton("go-down-symbolic", "Next month", self, size=28, icon_size=14)
        self.previous_button.clicked.connect(lambda: self.show_month(layout.add_months(self.month, -1)))
        self.next_button.clicked.connect(lambda: self.show_month(layout.add_months(self.month, 1)))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        context.store.settings_changed.connect(self.update)

    def sizeHint(self) -> QSize:
        return QSize(8 * self.CELL, self.HEAD + 7 * (self.CELL - 4) + 4)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.next_button.move(self.width() - 30, 4)
        self.previous_button.move(self.width() - 60, 4)

    def show_month(self, month: dt.date) -> None:
        self.month = month.replace(day=1)
        self.update()

    def set_selected(self, day: dt.date, shown: tuple[dt.date, dt.date] | None = None) -> None:
        self.selected = day
        self.shown = shown
        if (day.year, day.month) != (self.month.year, self.month.month):
            self.month = day.replace(day=1)
        self.setAccessibleDescription(textfmt.long_date(day))
        self.update()

    def weeks(self) -> list[list[dt.date]]:
        return layout.month_weeks(self.month.year, self.month.month, self.context.first_weekday())

    def _rect(self, week: int, column: int) -> QRectF:
        row = self.CELL - 4
        return QRectF((column + 1) * self.CELL, self.HEAD + (week + 1) * row, self.CELL, row)

    def day_at(self, position) -> dt.date | None:
        for week, days in enumerate(self.weeks()):
            for column, day in enumerate(days):
                if self._rect(week, column).contains(QPointF(position)):
                    return day
        return None

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        caption = fluent.font("caption")
        today = self.context.today()
        painter.setFont(fluent.font("body-strong"))
        painter.setPen(fluent.qcolor("text"))
        painter.drawText(QRectF(self.CELL + 6, 0, self.width() - self.CELL - 70, self.HEAD),
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         f"{textfmt.MONTHS[self.month.month - 1]} {self.month.year}")
        painter.setFont(caption)
        weeks = self.weeks()
        row = self.CELL - 4
        for column, day in enumerate(weeks[0]):
            painter.setPen(fluent.qcolor("text-secondary"))
            painter.drawText(QRectF((column + 1) * self.CELL, self.HEAD, self.CELL, row),
                             int(Qt.AlignmentFlag.AlignCenter), textfmt.WEEKDAYS_SHORT[day.weekday()][:2])
        for week, days in enumerate(weeks):
            painter.setPen(fluent.qcolor("text-muted"))
            painter.drawText(QRectF(0, self.HEAD + (week + 1) * row, self.CELL, row), int(Qt.AlignmentFlag.AlignCenter),
                             str(layout.week_number(days[0], self.context.first_weekday())))
            for column, day in enumerate(days):
                rect = self._rect(week, column)
                circle = QRectF(rect.center().x() - 13, rect.center().y() - 13, 26, 26)
                if self.shown is not None and self.shown[0] <= day <= self.shown[1]:
                    painter.fillRect(rect.adjusted(0, 1, 0, -1), fluent.qcolor("fill-subtle-hover"))
                if day == today:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(fluent.qcolor("accent"))
                    painter.drawEllipse(circle)
                    painter.setPen(fluent.qcolor("on-accent"))
                else:
                    painter.setPen(fluent.qcolor("text" if day.month == self.month.month else "text-muted"))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), str(day.day))
                if day == self.selected and day != today:
                    painter.setPen(QPen(fluent.qcolor("accent"), 1.5))
                    painter.drawEllipse(circle)

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        day = self.day_at(event.position())
        if day is not None and event.button() == Qt.MouseButton.LeftButton:
            self.set_selected(day)
            self.date_selected.emit(day)

    def wheelEvent(self, event) -> None:
        self.show_month(layout.add_months(self.month, -1 if event.angleDelta().y() > 0 else 1))

    def keyPressEvent(self, event) -> None:
        steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -7, Qt.Key.Key_Down: 7}
        key = event.key()
        try:
            if key in steps:
                day = self.selected + dt.timedelta(days=steps[key])
            elif key == Qt.Key.Key_PageUp:
                day = layout.add_months(self.selected, -1)
            elif key == Qt.Key.Key_PageDown:
                day = layout.add_months(self.selected, 1)
            elif key == Qt.Key.Key_Home:
                day = self.context.today()
            else:
                super().keyPressEvent(event)
                return
        except OverflowError:
            return
        self.set_selected(day)
        self.date_selected.emit(day)
