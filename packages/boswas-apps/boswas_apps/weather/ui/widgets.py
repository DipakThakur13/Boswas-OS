"""The widgets of the forecast page that paint themselves: the hourly strip, the 10-day list, the
temperature range bar, the UV scale, the wind arrow; and the grid the detail cards sit in.

Everything is painted in token colours read from ``fluent.theme()`` while painting, so a change of
scheme needs nothing but a repaint.
"""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QScrollArea, QSizePolicy, QToolTip, QVBoxLayout,
                               QWidget)

import boswas_fluent as fluent

from .. import model

# Temperatures (°C) and the palette colours the range bars run through: cold blue to hot coral.
TEMPERATURE_STOPS = ((-15.0, "iris.400"), (-5.0, "azure.400"), (5.0, "sky.400"), (14.0, "mint.400"),
                     (22.0, "sun.400"), (28.0, "sun.600"), (35.0, "coral.500"))
UV_STOPS = ((0.0, "mint.500"), (3.0, "sun.400"), (6.0, "sun.600"), (8.0, "coral.500"), (11.0, "orchid.500"))
UV_SCALE_TOP = 12.0


def _mix(first: QColor, second: QColor, share: float) -> QColor:
    return QColor.fromRgbF(first.redF() + (second.redF() - first.redF()) * share,
                           first.greenF() + (second.greenF() - first.greenF()) * share,
                           first.blueF() + (second.blueF() - first.blueF()) * share)


def scale_colour(value: float, stops: tuple[tuple[float, str], ...]) -> QColor:
    """The colour of ``value`` on a scale of (value, token colour) stops, mixed between neighbours."""
    if value <= stops[0][0]:
        return fluent.qcolor(stops[0][1])
    for (low, low_name), (high, high_name) in zip(stops, stops[1:]):
        if value <= high:
            return _mix(fluent.qcolor(low_name), fluent.qcolor(high_name), (value - low) / (high - low))
    return fluent.qcolor(stops[-1][1])


def scroll_page(content: QWidget) -> QScrollArea:
    """``content`` in a page that scrolls vertically, with the side margins of a NavigationView page."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(32, 4, 32, 24)
    layout.addWidget(content)
    layout.addStretch(1)
    area.setWidget(holder)
    return area


def _scroll_area_of(widget: QWidget) -> QScrollArea | None:
    parent = widget.parentWidget()
    while parent is not None and not isinstance(parent, QScrollArea):
        parent = parent.parentWidget()
    return parent


class CardGrid(QWidget):
    """Cards in columns of equal width; the number of columns follows the width."""

    def __init__(self, parent: QWidget | None = None, *, column_width: int = 220, spacing: int = 8) -> None:
        super().__init__(parent)
        self._column_width = column_width
        self._widgets: list[QWidget] = []
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)

    def set_widgets(self, widgets: list[QWidget]) -> None:
        self._widgets = list(widgets)
        for widget in self._widgets:
            widget.setParent(self)
        self._columns = 0
        self._reflow()

    def columns(self) -> int:
        return self._columns

    def _reflow(self) -> None:
        spacing = self._grid.spacing()
        columns = max(1, (max(self.width(), 1) + spacing) // (self._column_width + spacing))
        columns = min(columns, max(1, len(self._widgets)))
        if columns == self._columns:
            return
        self._columns = columns
        for widget in self._widgets:
            self._grid.removeWidget(widget)
        for index, widget in enumerate(self._widgets):
            self._grid.addWidget(widget, index // columns, index % columns)
        for column in range(self._grid.columnCount()):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()


class DetailCard(fluent.Card):
    """One fact of the current weather: a heading with an icon, the value, a line that explains it."""

    def __init__(self, title: str, icon: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, padding=16, spacing=6)
        self.title = title
        head = QHBoxLayout()
        head.setSpacing(8)
        self.icon_widget = fluent.IconWidget(icon, 16, self)
        self.title_label = fluent.Label(title, "caption", self, tone="secondary")
        head.addWidget(self.icon_widget)
        head.addWidget(self.title_label, 1)
        self.body.addLayout(head)
        self.value_row = QHBoxLayout()
        self.value_row.setSpacing(12)
        self.value_label = fluent.Label("", "subtitle", self)
        self.value_row.addWidget(self.value_label, 1)
        self.body.addLayout(self.value_row)
        self.detail_label = fluent.Label("", "caption", self, tone="secondary", wrap=True)
        self.body.addWidget(self.detail_label)
        self.body.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_values(self, value: str, detail: str = "") -> None:
        self.value_label.setText(value)
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))
        self.setAccessibleName(f"{self.title}: {value}")
        self.setAccessibleDescription(detail)

    def value(self) -> str:
        return self.value_label.text()

    def detail(self) -> str:
        return self.detail_label.text()


class WindArrow(QWidget):
    """A compass ring with an arrow that points where the wind blows to."""

    def __init__(self, parent: QWidget | None = None, *, size: int = 32) -> None:
        super().__init__(parent)
        self._degrees: float | None = None
        self.setFixedSize(size, size)

    def set_direction(self, degrees: float | None) -> None:
        self._degrees = degrees
        self.setToolTip(f"From the {model.compass_name(degrees)}" if degrees is not None else "")
        self.update()

    def direction(self) -> float | None:
        return self._degrees

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        painter.setPen(QPen(fluent.qcolor("stroke-strong"), 1))
        painter.drawEllipse(box)
        if self._degrees is None:
            return
        painter.translate(box.center())
        painter.rotate(self._degrees + 180)          # the wind comes from _degrees and blows the other way
        radius = box.width() / 2
        arrow = QPolygonF([QPointF(0, -radius * 0.72), QPointF(radius * 0.36, radius * 0.5),
                           QPointF(0, radius * 0.28), QPointF(-radius * 0.36, radius * 0.5)])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fluent.qcolor("accent"))
        painter.drawPolygon(arrow)


class UvScale(QWidget):
    """The UV index on a coloured scale from low to extreme, with a mark at the value."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value: float | None = None
        self.setFixedHeight(12)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: float | None) -> None:
        self._value = value
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(6, self.height() / 2 - 2, max(1, self.width() - 12), 4)
        gradient = QLinearGradient(track.topLeft(), track.topRight())
        for value, name in UV_STOPS:
            gradient.setColorAt(min(1.0, value / UV_SCALE_TOP), fluent.qcolor(name))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(track, 2, 2)
        if self._value is not None:
            x = track.left() + track.width() * min(1.0, self._value / UV_SCALE_TOP)
            painter.setPen(QPen(fluent.qcolor("card"), 2))
            painter.setBrush(fluent.qcolor("text"))
            painter.drawEllipse(QPointF(x, self.height() / 2), 5, 5)


def paint_range(painter: QPainter, track: QRectF, span: tuple[float, float] | None, lowest: float,
                highest: float) -> None:
    """A day's range on the track of the 10-day list: the part from its low to its high in temperature colours."""
    radius = track.height() / 2
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fluent.qcolor("stroke-divider"))
    painter.drawRoundedRect(track, radius, radius)
    if span is None:
        return
    start, end = span
    left = track.left() + track.width() * start
    width = max(track.height(), track.width() * (end - start))
    left = min(left, track.right() - width)
    gradient = QLinearGradient(track.topLeft(), track.topRight())
    steps = 8
    for step in range(steps + 1):
        share = step / steps
        gradient.setColorAt(share, scale_colour(lowest + (highest - lowest) * share, TEMPERATURE_STOPS))
    painter.setBrush(gradient)
    painter.drawRoundedRect(QRectF(left, track.top(), width, track.height()), radius, radius)


class HourlyStrip(QWidget):
    """The next hours side by side: the time, the condition, the temperature on a curve, the chance of rain.

    It lies in a horizontal scroll area. Keyboard: Left and Right (Home, End) move between the hours;
    the hour is read out as the accessible name and the focus ring sits on it.
    """

    CELL = 64
    ICON = 24
    CURVE = 28

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hours: list[model.Hour] = []
        self._unit = "c"
        self._twelve = False
        self._now_first = False
        self._cursor = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleDescription("The next 24 hours. Use the Left and Right arrow keys to move between hours.")
        self._sync_size()

    def set_hours(self, hours: list[model.Hour], unit: str, twelve_hour: bool, *, now_first: bool) -> None:
        self._hours = list(hours)
        self._unit, self._twelve, self._now_first = unit, twelve_hour, now_first
        self._cursor = min(self._cursor, max(0, len(self._hours) - 1))
        self._sync_size()
        self._describe()
        self.update()

    def hours(self) -> list[model.Hour]:
        return list(self._hours)

    def cursor(self) -> int:
        return self._cursor

    def label(self, index: int) -> str:
        """The time over a column: "Now" first when the strip starts at the current hour."""
        if index == 0 and self._now_first:
            return "Now"
        return model.format_hour(self._hours[index].time, self._twelve)

    def describe(self, index: int) -> str:
        return model.describe_hour(self._hours[index], self._unit, self._twelve, now=index == 0 and self._now_first)

    def _metrics(self) -> tuple[QFontMetrics, QFontMetrics]:
        return QFontMetrics(fluent.font("caption")), QFontMetrics(fluent.font("body-strong"))

    def _sync_size(self) -> None:
        caption, strong = self._metrics()
        height = 8 + caption.height() + 8 + self.ICON + 8 + strong.height() + 4 + self.CURVE + 6 + caption.height() + 8
        self.setFixedSize(max(self.CELL, self.CELL * len(self._hours)), height)

    def sizeHint(self) -> QSize:
        return self.size()

    def cell_rect(self, index: int) -> QRect:
        return QRect(index * self.CELL, 0, self.CELL, self.height())

    def fluent_focus_rect(self) -> tuple[QWidget, QRect]:
        return self, self.cell_rect(self._cursor).adjusted(4, 3, -4, -3)

    def _describe(self) -> None:
        if self._hours:
            self.setAccessibleName(f"Hourly forecast: {self.describe(self._cursor)}")
        else:
            self.setAccessibleName("Hourly forecast")

    def set_cursor(self, index: int) -> None:
        if not self._hours:
            return
        self._cursor = max(0, min(len(self._hours) - 1, index))
        self._describe()
        area = _scroll_area_of(self)
        if area is not None:
            rect = self.cell_rect(self._cursor)
            area.ensureVisible(rect.center().x(), rect.center().y(), self.CELL, 0)
        self.update()

    def keyPressEvent(self, event) -> None:
        moves = {Qt.Key.Key_Left: self._cursor - 1, Qt.Key.Key_Right: self._cursor + 1, Qt.Key.Key_Home: 0,
                 Qt.Key.Key_End: len(self._hours) - 1}
        if event.key() in moves and self._hours:
            self.set_cursor(moves[event.key()])
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._hours:
            self.set_cursor(int(event.position().x()) // self.CELL)
        super().mousePressEvent(event)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip and self._hours:
            index = int(event.pos().x()) // self.CELL
            if 0 <= index < len(self._hours):
                QToolTip.showText(event.globalPos(), self.describe(index), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, _event) -> None:
        if not self._hours:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        caption_font, strong_font = fluent.font("caption"), fluent.font("body-strong")
        caption, strong = self._metrics()
        y_time = 8
        y_icon = y_time + caption.height() + 8
        y_text = y_icon + self.ICON + 8
        y_curve = y_text + strong.height() + 4
        y_rain = y_curve + self.CURVE + 6
        if self.hasFocus():
            fluent.paint.rounded_rect(painter, QRectF(self.cell_rect(self._cursor)).adjusted(4, 3, -4, -3),
                                      fluent.theme().radius("control"), "fill-subtle-hover")
        known = [hour.temperature for hour in self._hours if hour.temperature is not None]
        lowest, highest = (min(known), max(known)) if known else (0.0, 0.0)
        points: list[QPointF | None] = []
        for index, hour in enumerate(self._hours):
            cell = self.cell_rect(index)
            painter.setFont(caption_font)
            painter.setPen(fluent.qcolor("text" if index == 0 and self._now_first else "text-secondary"))
            painter.drawText(QRect(cell.left(), y_time, cell.width(), caption.height()),
                             int(Qt.AlignmentFlag.AlignCenter), self.label(index))
            icon = fluent.icon(model.condition_icon(hour.code, hour.is_day))
            icon.paint(painter, QRect(cell.center().x() - self.ICON // 2, y_icon, self.ICON, self.ICON))
            if hour.temperature is None:
                points.append(None)
                painter.setFont(strong_font)
                painter.setPen(fluent.qcolor("text-secondary"))
                painter.drawText(QRect(cell.left(), y_curve, cell.width(), self.CURVE),
                                 int(Qt.AlignmentFlag.AlignCenter), model.MISSING)
            else:
                share = (hour.temperature - lowest) / (highest - lowest) if highest > lowest else 0.5
                point = QPointF(cell.center().x(), y_curve + 4 + (self.CURVE - 8) * (1 - share))
                points.append(point)
                painter.setFont(strong_font)
                painter.setPen(fluent.qcolor("text"))
                painter.drawText(QRect(cell.left(), round(point.y()) - 6 - strong.height(), cell.width(),
                                       strong.height()), int(Qt.AlignmentFlag.AlignCenter),
                                 model.format_temperature(hour.temperature, self._unit))
            painter.setFont(caption_font)
            chance = hour.precipitation_probability
            painter.setPen(fluent.qcolor("info-text" if chance else "text-secondary"))
            painter.drawText(QRect(cell.left(), y_rain, cell.width(), caption.height()),
                             int(Qt.AlignmentFlag.AlignCenter), model.format_percent(chance))
        painter.setPen(QPen(fluent.qcolor("accent"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for first, second in zip(points, points[1:]):
            if first is not None and second is not None:
                painter.drawLine(first, second)
        painter.setPen(QPen(fluent.qcolor("card"), 2))
        painter.setBrush(fluent.qcolor("accent"))
        for point in points:
            if point is not None:
                painter.drawEllipse(point, 4, 4)


class DailyList(QWidget):
    """The days of the forecast, one row each: day, condition, chance of rain, low, range bar, high.

    The bars of all rows share one scale (the lowest low to the highest high), as in a weather app.
    Keyboard: Up and Down (Home, End) move between the days; the day is read out as the accessible name.
    """

    ROW = 52
    ICON = 24

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._days: list[model.Day] = []
        self._today = dt.date(2000, 1, 1)
        self._unit = "c"
        self._precipitation_unit = "mm"
        self._current: float | None = None
        self._twelve = False
        self._cursor = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleDescription("Use the Up and Down arrow keys to move between days.")
        self.setFixedHeight(self.ROW)

    def set_days(self, days: list[model.Day], today: dt.date, unit: str, precipitation_unit: str = "mm", *,
                 current: float | None = None, twelve_hour: bool = False) -> None:
        """Show ``days``; ``current`` (°C) is marked on today's bar."""
        self._days = list(days)
        self._today, self._unit, self._precipitation_unit = today, unit, precipitation_unit
        self._current, self._twelve = current, twelve_hour
        self._cursor = min(self._cursor, max(0, len(self._days) - 1))
        self.setFixedHeight(max(1, len(self._days)) * self.ROW)
        self._describe()
        self.update()

    def days(self) -> list[model.Day]:
        return list(self._days)

    def cursor(self) -> int:
        return self._cursor

    def extremes(self) -> tuple[float, float] | None:
        lows = [day.temperature_min for day in self._days if day.temperature_min is not None]
        highs = [day.temperature_max for day in self._days if day.temperature_max is not None]
        if not lows or not highs:
            return None
        return min(lows), max(highs)

    def describe(self, index: int) -> str:
        return model.describe_day(self._days[index], self._today, self._unit)

    def details(self, index: int) -> str:
        """More about a day, for its tool tip."""
        day = self._days[index]
        lines = [self.describe(index)]
        if day.sunrise is not None or day.sunset is not None:
            lines.append(f"Sunrise {model.format_clock(day.sunrise, self._twelve)}, "
                         f"sunset {model.format_clock(day.sunset, self._twelve)}")
        if day.precipitation_sum is not None:
            lines.append(f"Precipitation {model.format_precipitation(day.precipitation_sum, self._precipitation_unit)}")
        if day.uv_index_max is not None:
            category = model.uv_category(day.uv_index_max).lower()
            lines.append(f"UV index {model.format_uv(day.uv_index_max)} ({category})")
        return "\n".join(lines)

    def row_rect(self, index: int) -> QRect:
        return QRect(0, index * self.ROW, self.width(), self.ROW)

    def fluent_focus_rect(self) -> tuple[QWidget, QRect]:
        return self, self.row_rect(self._cursor).adjusted(3, 3, -3, -3)

    def _describe(self) -> None:
        self.setAccessibleName(f"10-day forecast: {self.describe(self._cursor)}" if self._days else "10-day forecast")

    def set_cursor(self, index: int) -> None:
        if not self._days:
            return
        self._cursor = max(0, min(len(self._days) - 1, index))
        self._describe()
        area = _scroll_area_of(self)
        if area is not None:
            rect = self.row_rect(self._cursor)
            target = self.mapTo(area.widget(), rect.center())
            area.ensureVisible(target.x(), target.y(), 0, self.ROW)
        self.update()

    def keyPressEvent(self, event) -> None:
        moves = {Qt.Key.Key_Up: self._cursor - 1, Qt.Key.Key_Down: self._cursor + 1, Qt.Key.Key_Home: 0,
                 Qt.Key.Key_End: len(self._days) - 1}
        if event.key() in moves and self._days:
            self.set_cursor(moves[event.key()])
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._days:
            self.set_cursor(int(event.position().y()) // self.ROW)
        super().mousePressEvent(event)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip and self._days:
            index = int(event.pos().y()) // self.ROW
            if 0 <= index < len(self._days):
                QToolTip.showText(event.globalPos(), self.details(index), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def layout_columns(self) -> dict[str, QRectF]:
        """Where the columns of a row lie (x and width; the row's own top and height)."""
        width = self.width()
        body = QFontMetrics(fluent.font("body"))
        day_width = max(body.horizontalAdvance("Today"), body.horizontalAdvance("Wed 30")) + 16
        number = body.horizontalAdvance("-30°") + 8
        rain = QFontMetrics(fluent.font("caption")).horizontalAdvance("100%") + 12
        bar = min(260.0, max(56.0, width * 0.3))
        right = width - 12
        high = QRectF(right - number, 0, number, self.ROW)
        track = QRectF(high.left() - 8 - bar, 0, bar, self.ROW)
        low = QRectF(track.left() - 8 - number, 0, number, self.ROW)
        chance = QRectF(low.left() - 8 - rain, 0, rain, self.ROW)
        day = QRectF(12, 0, day_width, self.ROW)
        icon = QRectF(day.right() + 4, 0, self.ICON, self.ROW)
        text = QRectF(icon.right() + 12, 0, max(0.0, chance.left() - icon.right() - 24), self.ROW)
        if text.width() < body.horizontalAdvance("Thunderstorm") * 0.8:
            text.setWidth(0)                 # too narrow for the words; the icon and the tool tip say it
        return {"day": day, "icon": icon, "text": text, "chance": chance, "low": low, "track": track, "high": high}

    def paintEvent(self, _event) -> None:
        if not self._days:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        columns = self.layout_columns()
        extremes = self.extremes()
        body_font, strong_font, caption_font = fluent.font("body"), fluent.font("body-strong"), fluent.font("caption")
        for index, day in enumerate(self._days):
            row = self.row_rect(index)
            top = row.top()

            def at(name: str) -> QRectF:
                rect = columns[name]
                return QRectF(rect.left(), top, rect.width(), self.ROW)

            if index == self._cursor and self.hasFocus():
                fluent.paint.rounded_rect(painter, QRectF(row).adjusted(3, 3, -3, -3),
                                          fluent.theme().radius("control"), "fill-subtle-hover")
            if index:
                painter.fillRect(QRectF(12, top, self.width() - 24, 1), fluent.qcolor("stroke-divider"))
            fluent.paint.elided(painter, at("day"), model.day_label(day.date, self._today),
                                strong_font if day.date == self._today else body_font, "text")
            icon_box = at("icon")
            fluent.icon(model.condition_icon(day.code)).paint(
                painter, QRect(round(icon_box.left()), round(icon_box.center().y() - self.ICON / 2), self.ICON,
                               self.ICON))
            if columns["text"].width() > 0:
                fluent.paint.elided(painter, at("text"), model.condition_text(day.code), body_font, "text-secondary")
            chance = day.precipitation_probability_max
            fluent.paint.elided(painter, at("chance"), model.format_percent(chance) if chance is not None else "",
                                caption_font, "info-text" if chance else "text-secondary",
                                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            fluent.paint.elided(painter, at("low"), model.format_temperature(day.temperature_min, self._unit),
                                body_font, "text-secondary", right)
            fluent.paint.elided(painter, at("high"), model.format_temperature(day.temperature_max, self._unit),
                                strong_font, "text", right)
            track = at("track")
            bar = QRectF(track.left(), track.center().y() - 2, track.width(), 4)
            if extremes is not None:
                span = model.range_span(day.temperature_min, day.temperature_max, *extremes)
                paint_range(painter, bar, span, *extremes)
                if day.date == self._today and self._current is not None:
                    mark = model.range_span(self._current, self._current, *extremes)
                    painter.setPen(QPen(fluent.qcolor("card"), 2))
                    painter.setBrush(fluent.qcolor("text"))
                    painter.drawEllipse(QPointF(bar.left() + bar.width() * mark[0], bar.center().y()), 5, 5)
            else:
                paint_range(painter, bar, None, 0.0, 0.0)
