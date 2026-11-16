"""Widgets the pages of Clock share: the ring, the big time, the grid of cards, the ticker."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QGridLayout, QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

import boswas_fluent as fluent

from ...reminders import textfmt


class Ticker(QObject):
    """Calls ``tick()`` while ``page`` is on screen, and not at all while it is hidden or minimised.

    ``tick`` updates what the page shows and returns the milliseconds until
    it wants to be called again (None: not until something changes; the page
    then calls ``kick()``). Nothing repaints in between.
    """

    def __init__(self, page: QWidget, tick: Callable[[], int | None]) -> None:
        super().__init__(page)
        self._page = page
        self._tick = tick
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self.kick)
        page.installEventFilter(self)

    def is_active(self) -> bool:
        return self._timer.isActive()

    def _on_screen(self) -> bool:
        return self._page.isVisible() and not self._page.window().isMinimized()

    def kick(self) -> None:
        """Update now and plan the next update."""
        self._timer.stop()
        if not self._on_screen():
            return
        delay = self._tick()
        if delay is not None:
            self._timer.start(max(1, int(delay)))

    def stop(self) -> None:
        self._timer.stop()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._page:
            if event.type() == QEvent.Type.Show:
                QTimer.singleShot(0, self.kick)
            elif event.type() == QEvent.Type.Hide:
                self._timer.stop()
        return False


def until_next(seconds: float, step: float = 1.0) -> int:
    """Milliseconds until a value that counts in ``seconds`` crosses the next multiple of ``step``."""
    rest = seconds % step
    return int((rest if rest > 0.004 else step) * 1000) + 3


class RingProgress(QWidget):
    """A ring that empties as time runs out, with the time in its middle. It scales with its size."""

    def __init__(self, parent: QWidget | None = None, *, minimum: int = 160) -> None:
        super().__init__(parent)
        self._fraction = 0.0
        self._text = ""
        self._caption = ""
        self._state = "idle"                    # idle, running, paused, done
        self._minimum = minimum
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(minimum, minimum)

    def set_progress(self, fraction: float, text: str, caption: str = "", state: str = "running") -> None:
        fraction = max(0.0, min(1.0, fraction))
        if (fraction, text, caption, state) == (self._fraction, self._text, self._caption, self._state):
            return
        self._fraction, self._text, self._caption, self._state = fraction, text, caption, state
        self.setAccessibleName(text)
        self.setAccessibleDescription(caption)
        self.update()

    def text(self) -> str:
        return self._text

    def state(self) -> str:
        return self._state

    def sizeHint(self) -> QSize:
        return QSize(max(self._minimum, 200), max(self._minimum, 200))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return width

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        stroke = max(6.0, side / 26)
        box = QRectF((self.width() - side) / 2 + stroke, (self.height() - side) / 2 + stroke,
                     side - 2 * stroke, side - 2 * stroke)
        painter.setPen(QPen(fluent.qcolor("stroke-strong" if self._state != "idle" else "stroke-surface"), stroke))
        painter.drawEllipse(box)
        colour = {"paused": "text-secondary", "done": "success"}.get(self._state, "accent")
        if self._state == "done":
            painter.setPen(QPen(fluent.qcolor(colour), stroke))
            painter.drawEllipse(box)
        elif self._fraction > 0 and self._state != "idle":
            painter.setPen(QPen(fluent.qcolor(colour), stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(box, 90 * 16, round(-360 * 16 * self._fraction))
        font = QFont(fluent.font("title"))
        font.setPixelSize(max(18, int(side / (4.6 if len(self._text) <= 5 else 5.6))))
        painter.setFont(font)
        painter.setPen(fluent.qcolor("text"))
        inner = box.adjusted(stroke, stroke, -stroke, -stroke)
        painter.drawText(inner, int(Qt.AlignmentFlag.AlignCenter), self._text)
        if self._caption:
            small = QFont(fluent.font("caption"))
            small.setPixelSize(max(12, int(side / 17)))
            painter.setFont(small)
            painter.setPen(fluent.qcolor("text-secondary"))
            caption_box = QRectF(inner.left(), inner.center().y() + font.pixelSize() * 0.75, inner.width(),
                                 small.pixelSize() * 1.6)
            painter.drawText(caption_box, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                             self._caption)


class TimeDisplay(QWidget):
    """A time in large figures that fill the widget. Every figure has the same width, so nothing jitters."""

    def __init__(self, parent: QWidget | None = None, text: str = "00:00:00.00") -> None:
        super().__init__(parent)
        self._text = text
        self._dim_from: int | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 64)
        self.setAccessibleName(text)

    def set_text(self, text: str, dim_from: int | None = None) -> None:
        """Show ``text``; the characters from index ``dim_from`` on are drawn smaller and paler (the hundredths)."""
        if text == self._text and dim_from == self._dim_from:
            return
        self._text, self._dim_from = text, dim_from
        self.setAccessibleName(text)
        self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:
        return QSize(520, 140)

    def _cells(self, font: QFont, small: QFont) -> tuple[list[float], float]:
        metrics, small_metrics = QFontMetricsF(font), QFontMetricsF(small)
        digit = max(metrics.horizontalAdvance(character) for character in "0123456789")
        small_digit = max(small_metrics.horizontalAdvance(character) for character in "0123456789")
        widths = []
        for index, character in enumerate(self._text):
            minor = self._dim_from is not None and index >= self._dim_from
            if character.isdigit():
                widths.append(small_digit if minor else digit)
            else:
                widths.append((small_metrics if minor else metrics).horizontalAdvance(character))
        return widths, sum(widths)

    def font_size(self) -> int:
        """The pixel size of the figures at the current size of the widget."""
        size = max(16, int(self.height() * 0.78))
        font = QFont(fluent.font("title-large"))
        for _ in range(3):
            font.setPixelSize(size)
            small = QFont(font)
            small.setPixelSize(max(10, int(size * 0.6)))
            _widths, total = self._cells(font, small)
            if total <= self.width() - 16 or size <= 16:
                break
            size = max(16, int(size * (self.width() - 16) / total))
        return size

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        size = self.font_size()
        font = QFont(fluent.font("title-large"))
        font.setPixelSize(size)
        small = QFont(font)
        small.setPixelSize(max(10, int(size * 0.6)))
        widths, total = self._cells(font, small)
        metrics = QFontMetricsF(font)
        x = (self.width() - total) / 2
        baseline = (self.height() + metrics.ascent() - metrics.descent()) / 2
        for index, character in enumerate(self._text):
            minor = self._dim_from is not None and index >= self._dim_from
            painter.setFont(small if minor else font)
            painter.setPen(fluent.qcolor("text-secondary" if minor else "text"))
            advance = QFontMetricsF(painter.font()).horizontalAdvance(character)
            painter.drawText(QPointF(x + (widths[index] - advance) / 2, baseline), character)
            x += widths[index]


class CardGrid(QWidget):
    """Cards in columns of equal width; the number of columns follows the width."""

    def __init__(self, parent: QWidget | None = None, *, column_width: int = 300, spacing: int = 12) -> None:
        super().__init__(parent)
        self._column_width = column_width
        self._widgets: list[QWidget] = []
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)

    def set_widgets(self, widgets: list[QWidget]) -> None:
        for widget in self._widgets:
            if widget not in widgets:
                self._grid.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()
        self._widgets = list(widgets)
        self._columns = 0
        self._reflow()

    def widgets(self) -> list[QWidget]:
        return list(self._widgets)

    def columns(self) -> int:
        return self._columns

    def _reflow(self) -> None:
        spacing = self._grid.spacing()
        columns = max(1, (max(self.width(), 1) + spacing) // (self._column_width + spacing))
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


def scroll_page(content: QWidget) -> QScrollArea:
    """``content`` in a page that scrolls vertically, with the margins of a NavigationView page."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(32, 4, 32, 24)
    layout.addWidget(content)
    layout.addStretch(1)
    area.setWidget(holder)
    return area


class DayButton(QAbstractButton):
    """One weekday as a round toggle (or, not enabled, as a mark that only shows)."""

    def __init__(self, day: int, parent: QWidget | None = None, *, size: int = 32) -> None:
        super().__init__(parent)
        self.day = day
        self.setCheckable(True)
        self.setFixedSize(size, size)
        self.setText(textfmt.WEEKDAYS_SHORT[day][:2])
        self.setAccessibleName(textfmt.WEEKDAYS[day])
        self.setToolTip(textfmt.WEEKDAYS[day])
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fluent.qcolor("accent"))
            painter.drawEllipse(box)
            painter.setPen(fluent.qcolor("on-accent"))
        else:
            hovered = self.underMouse() and self.isEnabled()
            painter.setPen(QPen(fluent.qcolor("stroke-strong" if self.isEnabled() else "stroke-surface"), 1))
            painter.setBrush(fluent.qcolor("fill-subtle-hover") if hovered else Qt.BrushStyle.NoBrush)
            painter.drawEllipse(box)
            painter.setPen(fluent.qcolor("text" if self.isEnabled() else "text-muted"))
        painter.setFont(fluent.font("caption"))
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())


class DayPicker(QWidget):
    """The seven weekdays, starting with the first day of the week of the locale."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None, *, first_day: int = 0, size: int = 32,
                 editable: bool = True) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6 if editable else 4)
        self.buttons: dict[int, DayButton] = {}
        for offset in range(7):
            day = (first_day + offset) % 7
            button = DayButton(day, self, size=size)
            if not editable:
                button.setEnabled(False)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.toggled.connect(lambda _checked: self.changed.emit())
            self.buttons[day] = button
            layout.addWidget(button)
        layout.addStretch(1)

    def days(self) -> list[int]:
        return sorted(day for day, button in self.buttons.items() if button.isChecked())

    def set_days(self, days) -> None:
        for day, button in self.buttons.items():
            blocked = button.blockSignals(True)
            button.setChecked(day in days)
            button.blockSignals(blocked)
        self.update()
