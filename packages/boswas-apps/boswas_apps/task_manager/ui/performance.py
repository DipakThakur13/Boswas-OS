"""The Performance page: a list of resources with little graphs, and the page of the selected one."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QListWidget, QListWidgetItem,
                               QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

import boswas_fluent as fluent
from boswas_fluent import paint

from .. import history
from ..history import PerfItem
from ..sampler import Sample
from .graph import CompositionBar, CoreGrid, GraphWidget, Legend, paint_mini
from .pages import Page

KEY_ROLE = Qt.ItemDataRole.UserRole
_LIST_WIDTH = 252
_ROW_HEIGHT = 68


class _ListDelegate(QStyledItemDelegate):
    """A row of the resource list: the little graph, the name, one or two lines of figures."""

    def __init__(self, page: "PerformancePage") -> None:
        super().__init__(page)
        self._page = page

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
        text_left = graph_box.right() + 12
        width = rect.right() - text_left - 6
        paint.elided(painter, QRectF(text_left, rect.top() + 7, width, 20), item.title, fluent.font("body"), "text")
        y = rect.top() + 27
        for line in item.summary[:2]:
            paint.elided(painter, QRectF(text_left, y, width, 16), line, fluent.font("caption"), "text-secondary")
            y += 16


class _Pairs(QWidget):
    """Label and value pairs in a grid; the labels and values are updated in place."""

    def __init__(self, big: bool, columns: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._big, self._columns = big, columns
        self._labels: list[str] = []
        self._values: list[fluent.Label] = []
        self._widgets: list[QWidget] = []
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(24 if big else 12)
        self._grid.setVerticalSpacing(10 if big else 4)

    def set_pairs(self, pairs: list[tuple[str, str]]) -> None:
        labels = [label for label, _value in pairs]
        if labels != self._labels:
            while self._grid.count():
                self._grid.takeAt(0)
            for widget in self._widgets:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
            self._labels, self._values, self._widgets = labels, [], []
            for index, label in enumerate(labels):
                name = fluent.Label(label, "caption" if self._big else "body", tone="secondary")
                value = fluent.Label("", "body-large" if self._big else "body", selectable=not self._big)
                value.setAccessibleName(label.rstrip(":"))
                name.setParent(self)
                value.setParent(self)
                self._widgets += [name, value]
                if self._big:
                    cell = QVBoxLayout()
                    cell.setSpacing(0)
                    cell.addWidget(name)
                    cell.addWidget(value)
                    self._grid.addLayout(cell, index // self._columns, index % self._columns)
                else:
                    value.setWordWrap(True)
                    self._grid.addWidget(name, index, 0, Qt.AlignmentFlag.AlignTop)
                    self._grid.addWidget(value, index, 1)
                self._values.append(value)
            if not self._big:
                self._grid.setColumnStretch(1, 1)
        for widget, (_label, value) in zip(self._values, pairs):
            if widget.text() != value:
                widget.setText(value)

    def value(self, label: str) -> str:
        return self._values[self._labels.index(label)].text() if label in self._labels else ""


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
            text = f"{item.title}: {', '.join(item.summary)}"
            if entry.data(Qt.ItemDataRole.AccessibleTextRole) != text:
                entry.setText(item.title)
                entry.setData(Qt.ItemDataRole.AccessibleTextRole, text)
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
