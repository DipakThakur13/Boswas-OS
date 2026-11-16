"""The graphs of the Performance page, drawn with QPainter in token colours.

``GraphWidget`` is the large graph: a frame in the colour of the resource,
a quiet grid that moves with time, the area under the first series as a
wash and the series as lines (a second series is dashed, so the two are
told apart without colour). Moving the pointer over it shows the values
at that moment. ``CoreGrid`` shows one little graph per logical processor,
or tiles when there are too many for graphs. ``CompositionBar`` is the
memory bar. ``paint_mini`` draws the small graph of the list on the left.

Every resource has its own hue, the same in the list and on its page; the
steps were chosen so that neighbours in the list are told apart with any
colour vision, in both schemes. Text is always in text colours.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

from .. import history
from ..history import Graph, format_value

# kind -> palette reference in the dark and in the light scheme
SERIES = {
    "cpu": ("azure.500", "azure.600"), "memory": ("orchid.500", "orchid.600"), "disk": ("mint.500", "mint.600"),
    "net": ("sun.700", "sun.700"), "gpu": ("iris.500", "iris.600"), "battery": ("sky.500", "sky.600"),
}
_GRID_COLUMNS = 6            # samples between two vertical grid lines
_GRID_ROWS = 4


def series_colour(kind: str, alpha: float = 1.0) -> QColor:
    theme = fluent.theme()
    dark, light = SERIES.get(kind, SERIES["cpu"])
    colour = QColor(theme.color(dark if theme.is_dark else light))
    if alpha < 1.0:
        colour.setAlphaF(alpha)
    return colour


def _paths(values, box: QRectF, top: float) -> tuple[QPainterPath, QPainterPath]:
    """(the line, the area under it) for one series; a None value breaks both."""
    line, area = QPainterPath(), QPainterPath()
    count = len(values)
    if count < 2 or top <= 0:
        return line, area
    step = box.width() / (count - 1)
    run: list[QPointF] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:                       # a single sample between two gaps: a short stroke
            run.append(QPointF(run[0].x() + min(step, 2.0), run[0].y()))
        line.moveTo(run[0])
        area.moveTo(QPointF(run[0].x(), box.bottom()))
        area.lineTo(run[0])
        for point in run[1:]:
            line.lineTo(point)
            area.lineTo(point)
        area.lineTo(QPointF(run[-1].x(), box.bottom()))
        area.closeSubpath()
        run.clear()

    for index, value in enumerate(values):
        if value is None:
            flush()
            continue
        ratio = max(0.0, min(1.0, value / top))
        run.append(QPointF(box.left() + index * step, box.bottom() - ratio * box.height()))
    flush()
    return line, area


def paint_series(painter: QPainter, box: QRectF, graph: Graph, kind: str, *, grid: bool = True, phase: int = 0,
                 width: float = 1.6) -> None:
    """Frame, grid and series of ``graph`` inside ``box``."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    colour = series_colour(kind)
    if grid:
        pen = QPen(series_colour(kind, 0.16), 1)
        painter.setPen(pen)
        length = max((len(series.values) for series in graph.series), default=history.LENGTH)
        step = box.width() / max(1, length - 1)
        offset = (length - 1 - phase) % _GRID_COLUMNS
        index = offset
        while index < length - 1:
            if index > 0:
                x = round(box.left() + index * step) + 0.5
                painter.drawLine(QPointF(x, box.top()), QPointF(x, box.bottom()))
            index += _GRID_COLUMNS
        for row in range(1, _GRID_ROWS):
            y = round(box.top() + row * box.height() / _GRID_ROWS) + 0.5
            painter.drawLine(QPointF(box.left(), y), QPointF(box.right(), y))
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setClipRect(box)
    top = graph.top()
    for number, series in enumerate(graph.series):
        line, area = _paths(series.values, box, top)
        if number == 0:
            painter.fillPath(area, series_colour(kind, 0.14))
        pen = QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        if series.dashed:
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([4, 3])
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(line)
    painter.setClipping(False)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setPen(QPen(colour, 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(QRectF(box.left() + 0.5, box.top() + 0.5, box.width() - 1, box.height() - 1))
    painter.restore()


def paint_mini(painter: QPainter, box: QRectF, graph: Graph | None, kind: str) -> None:
    """The little graph of the list: frame and series, no grid."""
    if graph is None:
        painter.save()
        painter.setPen(QPen(series_colour(kind), 1))
        painter.drawRect(QRectF(box.left() + 0.5, box.top() + 0.5, box.width() - 1, box.height() - 1))
        painter.restore()
        return
    paint_series(painter, box, graph, kind, grid=False, width=1.2)


class GraphWidget(QWidget):
    """One graph of a resource. ``set_graph`` gives it the series to show; it repaints when told to."""

    def __init__(self, parent: QWidget | None = None, *, minimum_height: int = 120) -> None:
        super().__init__(parent)
        self._graph: Graph | None = None
        self._kind = "cpu"
        self._phase = 0
        self._interval = 1.0
        self._hover: int | None = None
        self.setMinimumHeight(minimum_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setAccessibleName("Graph")

    def set_graph(self, graph: Graph | None, kind: str, interval: float = 1.0) -> None:
        self._graph, self._kind, self._interval = graph, kind, interval
        self._phase += 1
        if graph is not None:
            self.setAccessibleName(f"{graph.title} graph")
            newest = [f"{series.name} {format_value(graph.unit, series.values[-1])}" for series in graph.series
                      if series.values and series.values[-1] is not None]
            self.setAccessibleDescription(", ".join(newest) or "No samples yet")
        self.update()

    def graph(self) -> Graph | None:
        return self._graph

    def sizeHint(self) -> QSize:
        return QSize(480, 220)

    def _index_at(self, x: float) -> int | None:
        graph = self._graph
        if graph is None or not graph.series:
            return None
        count = len(graph.series[0].values)
        if count < 2 or self.width() < 2:
            return None
        return max(0, min(count - 1, round(x / (self.width() - 1) * (count - 1))))

    def describe(self, index: int) -> str:
        """What the tool tip says for sample ``index`` (0 is the oldest)."""
        graph = self._graph
        if graph is None:
            return ""
        count = len(graph.series[0].values)
        lines = []
        for series in graph.series:
            value = series.values[index]
            lines.append(f"{series.name}: {format_value(graph.unit, value) if value is not None else 'no sample'}")
        ago = (count - 1 - index) * self._interval
        lines.append("Now" if ago <= 0 else f"{ago:g} s ago")
        return "\n".join(lines)

    def mouseMoveEvent(self, event) -> None:
        index = self._index_at(event.position().x())
        if index != self._hover:
            self._hover = index
            self.update()
        if index is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self.describe(index), self)

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        box = QRectF(self.rect())
        if self._graph is None:
            return
        paint_series(painter, box, self._graph, self._kind, phase=self._phase)
        if self._hover is not None:
            count = len(self._graph.series[0].values)
            x = round(box.left() + self._hover * box.width() / max(1, count - 1)) + 0.5
            painter.setPen(QPen(fluent.qcolor("text-secondary"), 1))
            painter.drawLine(QPointF(x, box.top() + 1), QPointF(x, box.bottom() - 1))


class CoreGrid(QWidget):
    """Every logical processor at once: little graphs, or tiles when there are more than fit as graphs."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cores: list = []
        self._values: list[tuple[int, float]] = []
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setAccessibleName("Utilisation of every logical processor")

    def set_cores(self, cores: list, values: list[tuple[int, float]]) -> None:
        self._cores, self._values = cores, values
        busiest = max(values, key=lambda pair: pair[1], default=None)
        if busiest is not None:
            self.setAccessibleDescription(f"{len(values)} processors; the busiest, CPU {busiest[0]}, "
                                          f"is at {busiest[1]:.0f}%")
        self.update()

    def cells(self) -> list[tuple[int, QRectF]]:
        """(CPU number, rectangle) of every cell, row by row."""
        count = len(self._values)
        if not count or self.width() < 8 or self.height() < 8:
            return []
        graphs = bool(self._cores)
        aspect = 1.6 if graphs else 1.0
        columns = max(1, min(count, round(math.sqrt(count * self.width() / (self.height() * aspect)))))
        rows = math.ceil(count / columns)
        gap = 6 if graphs else 2
        width = (self.width() - gap * (columns - 1)) / columns
        height = (self.height() - gap * (rows - 1)) / rows
        return [(number, QRectF(index % columns * (width + gap), index // columns * (height + gap), width, height))
                for index, (number, _value) in enumerate(self._values)]

    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        values = dict(self._values)
        for number, rect in self.cells():
            if rect.contains(position):
                QToolTip.showText(event.globalPosition().toPoint(), f"CPU {number}: {values[number]:.0f}%", self)
                return
        QToolTip.hideText()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        cells = self.cells()
        if not cells:
            return
        if self._cores:
            series = dict(self._cores)
            for number, rect in cells:
                values = series.get(number)
                if values is not None:
                    paint_series(painter, rect, Graph("", [history.Series("", values)]), "cpu", grid=False, width=1.2)
            return
        values = dict(self._values)
        for number, rect in cells:
            level = max(0.0, min(1.0, values[number] / 100))
            painter.fillRect(rect, series_colour("cpu", 0.10 + 0.80 * level))


class CompositionBar(QWidget):
    """How the memory is used, as one bar: in use, modified, standby, free."""

    ALPHAS = (0.85, 0.5, 0.2, 0.0)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._segments: list[tuple[str, int]] = []
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setAccessibleName("Memory composition")

    def set_segments(self, segments: list[tuple[str, int]]) -> None:
        self._segments = segments
        self.setAccessibleDescription(", ".join(f"{label} {fluent_size(size)}" for label, size in segments))
        self.update()

    def rects(self) -> list[tuple[str, int, QRectF]]:
        total = sum(size for _label, size in self._segments)
        if total <= 0:
            return []
        result, x = [], 0.0
        for label, size in self._segments:
            width = self.width() * size / total
            result.append((label, size, QRectF(x, 0, width, self.height())))
            x += width
        return result

    def mouseMoveEvent(self, event) -> None:
        for label, size, rect in self.rects():
            if rect.contains(event.position()):
                QToolTip.showText(event.globalPosition().toPoint(), f"{label}: {fluent_size(size)}", self)
                return

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for index, (_label, _size, rect) in enumerate(self.rects()):
            alpha = self.ALPHAS[index] if index < len(self.ALPHAS) else 0.0
            inner = rect.adjusted(1 if index else 0, 0, -1, 0)           # a gap between neighbours
            if inner.width() < 1:
                continue
            if alpha:
                painter.fillRect(inner, series_colour("memory", alpha))
            painter.setPen(QPen(series_colour("memory"), 1))
            painter.drawRect(QRectF(inner.left() + 0.5, inner.top() + 0.5, max(0.0, inner.width() - 1),
                                    inner.height() - 1))


class Legend(QWidget):
    """What the marks of a graph or bar mean: a swatch and a label each, in text colour."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[tuple[str, str, float]] = []      # label, "line" / "dashed" / "fill", alpha
        self._kind = "cpu"
        self.setFixedHeight(20)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_entries(self, kind: str, entries: list[tuple[str, str, float]]) -> None:
        self._kind, self._entries = kind, entries
        self.setAccessibleName("Legend: " + ", ".join(label for label, _style, _alpha in entries))
        self.update()

    def entries(self) -> list[tuple[str, str, float]]:
        return self._entries

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        font = fluent.font("caption")
        painter.setFont(font)
        metrics = painter.fontMetrics()
        x = 0.0
        middle = self.height() / 2
        for label, style, alpha in self._entries:
            if style == "fill":
                swatch = QRectF(x, middle - 5, 14, 10)
                if alpha:
                    painter.fillRect(swatch, series_colour(self._kind, alpha))
                painter.setPen(QPen(series_colour(self._kind), 1))
                painter.drawRect(swatch.adjusted(0.5, 0.5, -0.5, -0.5))
            else:
                pen = QPen(series_colour(self._kind), 2)
                if style == "dashed":
                    pen.setStyle(Qt.PenStyle.CustomDashLine)
                    pen.setDashPattern([2, 1.5])
                painter.setPen(pen)
                painter.drawLine(QPointF(x, middle), QPointF(x + 18, middle))
            x += 24
            width = metrics.horizontalAdvance(label)
            paint.elided(painter, QRectF(x, 0, width + 2, self.height()), label, font, "text-secondary")
            x += width + 20


def fluent_size(value: int) -> str:
    from .. import textutil
    return textutil.size(value)
