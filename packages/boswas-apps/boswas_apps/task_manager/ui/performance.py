"""The Performance page: a list of resources with little graphs, and the page of the selected one."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QScrollArea,
                               QSizePolicy, QStyle, QStyledItemDelegate, QToolTip, QVBoxLayout, QWidget)

import boswas_fluent as fluent
from boswas_fluent import paint

from .. import history
from ..history import PerfItem
from ..sampler import Sample
from .graph import CompositionBar, CoreGrid, GraphWidget, Legend, paint_mini
from .pages import Page

KEY_ROLE = Qt.ItemDataRole.UserRole
_LEFT = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
_LIST_WIDTH = 252
_ROW_HEIGHT = 68


class _ListDelegate(QStyledItemDelegate):
    """A row of the resource list: the little graph, the name, one or two lines of figures."""

    def __init__(self, page: "PerformancePage") -> None:
        super().__init__(page)
        self._page = page
        self._theme = None

    def _prepare(self) -> None:
        self._theme = fluent.theme()
        self._body, self._caption = fluent.font("body"), fluent.font("caption")
        self._body_metrics, self._caption_metrics = QFontMetrics(self._body), QFontMetrics(self._caption)
        self._text, self._secondary = fluent.qcolor("text"), fluent.qcolor("text-secondary")

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return QSize(_LIST_WIDTH - 24, _ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        item = self._page.window.history.item(index.data(KEY_ROLE) or "")
        rect = QRectF(option.rect).adjusted(4, 2, -4, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        radius = fluent.theme().radius("control")
        if selected or hovered:
            paint.rounded_rect(painter, rect, radius, "fill-subtle-pressed" if selected and hovered
                               else "fill-subtle-hover")
        if selected:
            paint.rounded_rect(painter, QRectF(rect.left(), rect.center().y() - 8, 3, 16), 1.5, "accent")
        if item is None:
            return
        graph_box = QRectF(rect.left() + 12, rect.top() + 10, 68, rect.height() - 20)
        paint_mini(painter, graph_box, item.graphs[0] if item.graphs else None, item.kind)
        if self._theme is not fluent.theme():
            self._prepare()
        text_left = int(graph_box.right()) + 12
        width = int(rect.right()) - text_left - 6
        elide = Qt.TextElideMode.ElideRight
        painter.setFont(self._body)
        painter.setPen(self._text)
        painter.drawText(QRect(text_left, int(rect.top()) + 7, width, 20), _LEFT,
                         self._body_metrics.elidedText(item.title, elide, width))
        painter.setFont(self._caption)
        painter.setPen(self._secondary)
        y = int(rect.top()) + 27
        for line in item.summary[:2]:
            painter.drawText(QRect(text_left, y, width, 16), _LEFT, self._caption_metrics.elidedText(line, elide, width))
            y += 16


class _Pairs(QWidget):
    """Label and value pairs, painted in a grid.

    ``big``: a small label above a larger value, ``columns`` cells per row
    (the figures under the graph). Otherwise label and value side by side,
    one pair per line (the facts beside them). The values change every
    second, so the widget paints the text itself instead of keeping a
    QLabel per value: nothing is laid out again when a number changes.
    """

    def __init__(self, big: bool, columns: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._big, self._columns = big, columns
        self._pairs: list[tuple[str, str]] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Figures" if big else "Facts")

    def set_pairs(self, pairs: list[tuple[str, str]]) -> None:
        if pairs == self._pairs:
            return
        resized = len(pairs) != len(self._pairs)
        self._pairs = list(pairs)
        self.setAccessibleDescription("; ".join(f"{label.rstrip(':')} {value}" for label, value in pairs))
        if resized:
            self.updateGeometry()
        self.update()

    def value(self, label: str) -> str:
        return next((value for name, value in self._pairs if name == label), "")

    def _rows(self) -> int:
        return -(-len(self._pairs) // self._columns) if self._big else len(self._pairs)

    def sizeHint(self) -> QSize:
        return QSize(260 if self._big else 240, self._rows() * (48 if self._big else 22))

    def minimumSizeHint(self) -> QSize:
        return QSize(180, self.sizeHint().height())

    def paintEvent(self, _event) -> None:
        if not self._pairs:
            return
        painter = QPainter(self)
        elide = Qt.TextElideMode.ElideRight
        secondary, text = fluent.qcolor("text-secondary"), fluent.qcolor("text")
        if self._big:
            small, large = fluent.font("caption"), fluent.font("body-large")
            small_metrics, large_metrics = QFontMetrics(small), QFontMetrics(large)
            cell = self.width() // self._columns
            for index, (label, value) in enumerate(self._pairs):
                left, top = index % self._columns * cell, index // self._columns * 48
                width = cell - 12
                painter.setFont(small)
                painter.setPen(secondary)
                painter.drawText(QRect(left, top, width, 18), _LEFT, small_metrics.elidedText(label, elide, width))
                painter.setFont(large)
                painter.setPen(text)
                painter.drawText(QRect(left, top + 18, width, 24), _LEFT, large_metrics.elidedText(value, elide, width))
            return
        body = fluent.font("body")
        metrics = QFontMetrics(body)
        painter.setFont(body)
        label_width = min(max(metrics.horizontalAdvance(label) for label, _value in self._pairs) + 12,
                          self.width() // 2)
        for index, (label, value) in enumerate(self._pairs):
            top = index * 22
            painter.setPen(secondary)
            painter.drawText(QRect(0, top, label_width - 8, 22), _LEFT, metrics.elidedText(label, elide, label_width - 8))
            painter.setPen(text)
            width = self.width() - label_width
            painter.drawText(QRect(label_width, top, width, 22), _LEFT, metrics.elidedText(value, elide, width))

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip and not self._big:
            index = event.pos().y() // 22                 # a value that is cut off shows whole as a tool tip
            if 0 <= index < len(self._pairs):
                QToolTip.showText(event.globalPos(), f"{self._pairs[index][0]} {self._pairs[index][1]}", self)
                return True
        return super().event(event)


class _GraphBlock(QWidget):
    """One graph with what stands around it: what it measures, the top of the scale, the time span."""

    def __init__(self, tall: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        top = QHBoxLayout()
        self.title = fluent.Label("", "caption", tone="secondary")
        self.top_label = fluent.Label("", "caption", tone="secondary")
        top.addWidget(self.title, 1)
        top.addWidget(self.top_label)
        layout.addLayout(top)
        self.graph = GraphWidget(minimum_height=150 if tall else 70)
        layout.addWidget(self.graph, 3 if tall else 1)
        bottom = QHBoxLayout()
        self.span = fluent.Label("", "caption", tone="secondary")
        self.zero = fluent.Label("0", "caption", tone="secondary")
        bottom.addWidget(self.span, 1)
        bottom.addWidget(self.zero)
        layout.addLayout(bottom)


class PerformancePage(Page):
    key = "performance"
    searchable = False

    def __init__(self, window) -> None:
        super().__init__(window)
        self.wanted_key = "cpu"                # what the user chose; shown when the system has it
        self.current_key = "cpu"
        outer = QHBoxLayout(self)
        outer.setContentsMargins(20, 0, 20, 16)
        outer.setSpacing(16)
        self.list = QListWidget()
        self.list.setAccessibleName("Resources")
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setFixedWidth(_LIST_WIDTH)
        self.list.setItemDelegate(_ListDelegate(self))
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMouseTracking(True)
        self.list.currentItemChanged.connect(self._selected)
        outer.addWidget(self.list)

        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 8, 0)
        column.setSpacing(8)
        head = QHBoxLayout()
        self.title = fluent.Label("", "subtitle")
        self.heading = fluent.Label("", "body", tone="secondary")
        self.heading.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(self.title)
        head.addWidget(self.heading, 1)
        column.addLayout(head)
        self.cpu_view = fluent.SegmentedControl()
        self.cpu_view.add_item("overall", "Overall utilisation")
        self.cpu_view.add_item("cores", "Logical processors")
        self.cpu_view.set_current(window.settings.cpu_view)
        self.cpu_view.current_changed.connect(self._cpu_view_changed)
        column.addWidget(self.cpu_view, 0, Qt.AlignmentFlag.AlignLeft)
        self.blocks = [_GraphBlock(True), _GraphBlock(False)]
        column.addWidget(self.blocks[0], 3)
        self.cores = CoreGrid()
        column.addWidget(self.cores, 3)
        self.legend = Legend()
        column.addWidget(self.legend)
        column.addWidget(self.blocks[1], 1)
        self.composition_title = fluent.Label("Memory composition", "caption", tone="secondary")
        self.composition = CompositionBar()
        self.composition_legend = Legend()
        column.addWidget(self.composition_title)
        column.addWidget(self.composition)
        column.addWidget(self.composition_legend)
        self.note = fluent.InfoBar("info", closable=False)
        column.addWidget(self.note)
        figures = QHBoxLayout()
        figures.setSpacing(32)
        self.stats = _Pairs(True, 3)
        self.info = _Pairs(False, 1)
        figures.addWidget(self.stats, 3, Qt.AlignmentFlag.AlignTop)
        figures.addWidget(self.info, 2, Qt.AlignmentFlag.AlignTop)
        column.addSpacing(4)
        column.addLayout(figures)
        column.addStretch(0)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setAccessibleName("Resource")
        outer.addWidget(scroll, 1)
        self.empty = fluent.EmptyState("Collecting the first sample", "The graphs appear in a moment.",
                                       "utilities-system-monitor")
        self.sync()

    def parts(self) -> frozenset[str]:
        return frozenset({"cpu", "memory", "disks", "net", "gpu", "battery"})

    def _cpu_view_changed(self, key: str) -> None:
        self.window.settings.cpu_view = key
        self.window.save_settings()
        self.sync()

    def _selected(self, current: QListWidgetItem | None, _previous) -> None:
        if current is not None:
            self.wanted_key = self.current_key = current.data(KEY_ROLE)
            self.sync()

    def select(self, key: str) -> bool:
        for row in range(self.list.count()):
            if self.list.item(row).data(KEY_ROLE) == key:
                self.list.setCurrentRow(row)
                return True
        return False

    def keys(self) -> list[str]:
        return [self.list.item(row).data(KEY_ROLE) for row in range(self.list.count())]

    def apply(self, sample: Sample) -> None:
        self.sync()                           # the window has put the sample into the history already

    def activated(self) -> None:
        self.sync()

    def settings_changed(self) -> None:
        self.cpu_view.set_current(self.window.settings.cpu_view)
        self.sync()

    def span_text(self) -> str:
        interval = self.window.settings.interval()
        if interval is None:
            return "Paused"
        seconds = history.LENGTH * interval
        return f"{seconds:g} seconds" if seconds < 120 else f"{seconds / 60:g} minutes"

    def sync(self) -> None:
        """Bring the list and the page of the selected resource up to date with the history."""
        items = self.window.history.items()
        keys = [item.key for item in items]
        if keys != self.keys():
            blocked = self.list.blockSignals(True)
            self.list.clear()
            for item in items:
                entry = QListWidgetItem(item.title)
                entry.setData(KEY_ROLE, item.key)
                entry.setSizeHint(QSize(_LIST_WIDTH - 24, _ROW_HEIGHT))
                self.list.addItem(entry)
            self.list.blockSignals(blocked)
            self.current_key = self.wanted_key if self.wanted_key in keys else (keys[0] if keys else "")
            if self.current_key in keys:
                blocked = self.list.blockSignals(True)
                self.list.setCurrentRow(keys.index(self.current_key))
                self.list.blockSignals(blocked)
        for row, item in enumerate(items):
            entry = self.list.item(row)
            if entry.text() != item.title:               # (a disk that got another number)
                entry.setText(item.title)
        self.list.viewport().update()
        self.show_item(self.window.history.item(self.current_key))

    def show_item(self, item: PerfItem | None) -> None:
        if item is None:
            self.title.setText("")
            self.heading.setText("")
            for widget in (*self.blocks, self.cores, self.legend, self.cpu_view, self.composition_title,
                           self.composition, self.composition_legend, self.note):
                widget.hide()
            self.stats.set_pairs([])
            self.info.set_pairs([])
            return
        interval = self.window.settings.interval() or 1.0
        self.title.setText(item.title)
        self.heading.setText(item.heading)
        self.heading.setToolTip(item.heading)
        is_cpu = item.kind == "cpu"
        show_cores = is_cpu and self.window.settings.cpu_view == "cores" and bool(item.core_values)
        self.cpu_view.setVisible(is_cpu and len(item.core_values) > 1)
        self.cores.setVisible(show_cores)
        if show_cores:
            self.cores.set_cores(item.cores, item.core_values)
        for number, block in enumerate(self.blocks):
            graph = item.graphs[number] if number < len(item.graphs) else None
            visible = graph is not None and not (show_cores and number == 0)
            block.setVisible(visible)
            if graph is None:
                continue
            block.title.setText(graph.title)
            block.top_label.setText(graph.top_label())
            block.span.setText(self.span_text())
            block.graph.set_graph(graph, item.kind, interval)
        paired = next((graph for graph in item.graphs if len(graph.series) > 1), None)
        self.legend.setVisible(paired is not None)
        if paired is not None:
            self.legend.set_entries(item.kind, [(series.name, "dashed" if series.dashed else "line", 1.0)
                                                for series in paired.series])
        has_bar = bool(item.composition)
        for widget in (self.composition_title, self.composition, self.composition_legend):
            widget.setVisible(has_bar)
        if has_bar:
            self.composition.set_segments(item.composition)
            self.composition_legend.set_entries("memory", [
                (label, "fill", alpha) for (label, _size), alpha in zip(item.composition, CompositionBar.ALPHAS)])
        self.note.setVisible(bool(item.note))
        if item.note:
            self.note.set_message(item.note)
        self.stats.set_pairs(item.stats)
        self.info.set_pairs(item.info)
