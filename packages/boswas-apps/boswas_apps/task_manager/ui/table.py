"""The tables of Task Manager: a tree model that is updated in place, its view, heat-map cells and header.

``TreeModel.set_rows`` takes the rows as they should be now and changes
the model by the difference: rows that are gone are removed, new ones
inserted, moved ones moved (as a layout change that carries selection,
current row and expanded rows along), and ``dataChanged`` is emitted only
for rows whose ``sig`` differs. The model is never reset, so the view
keeps its scroll position and selection from tick to tick. Sorting
happens here too, in Python on the rows' own values, not in a proxy model
that would ask for every cell on every tick.
"""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QKeySequence, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHeaderView, QStyle, QStyleOptionViewItem, QTreeView,
                               QWidget)

import boswas_fluent as fluent
from boswas_fluent import paint
from boswas_fluent.polish import TreeItemDelegate

HEAT_ROLE = Qt.ItemDataRole.UserRole + 20
METRIC_ROLE = Qt.ItemDataRole.UserRole + 21
LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_RANGE_THRESHOLD = 12            # more changed rows than this under one parent: one dataChanged for all
HEADER_HEIGHT = 48
ROW_HEIGHT = 34


class Column:
    """One column: how a row shows in it and how rows are ordered by it."""

    def __init__(self, key: str, title: str, text: Callable[["Row"], str], sort: Callable[["Row"], object] | None = None,
                 *, right: bool = False, width: int = 100, heat: Callable[["Row"], float | None] | None = None,
                 icon: Callable[["Row"], QIcon | None] | None = None, descending_first: bool = False,
                 tooltip: bool = False) -> None:
        self.key, self.title, self.text = key, title, text
        self.sort = sort or (lambda row: text(row).casefold())
        self.right, self.width, self.heat, self.icon = right, width, heat, icon
        self.descending_first = descending_first      # numbers: the first click on the header shows the largest
        self.tooltip = tooltip                        # cells of this column show their whole text as a tool tip


class Row:
    """A row to show: ``key`` identifies it between updates, ``sig`` changes when what it shows changes."""
    __slots__ = ("key", "data", "sig", "children", "section", "header")

    def __init__(self, key: str, data: object, sig: object, children: list["Row"] | None = None,
                 section: int = 0, header: bool = False) -> None:
        self.key, self.data, self.sig, self.children = key, data, sig, children
        self.section, self.header = section, header


class _Node:
    __slots__ = ("id", "row", "parent", "children", "texts", "position")

    def __init__(self, node_id: int, row: Row | None, parent: "_Node | None", columns: int) -> None:
        self.id, self.row, self.parent = node_id, row, parent
        self.children: list[_Node] = []
        self.texts: list[str | None] = [None] * columns
        self.position = 0


class TreeModel(QAbstractItemModel):
    sort_changed = Signal(str, bool)

    def __init__(self, columns: list[Column], parent=None) -> None:
        super().__init__(parent)
        self.columns = list(columns)
        self._root = _Node(0, None, None, 0)
        self._nodes: dict[int, _Node] = {}
        self._by_key: dict[str, _Node] = {}
        self._next_id = 1
        self._rows: list[Row] = []
        self._sort_column = 0
        self._descending = False
        self._metrics: dict[str, str] = {}
        self._header_font = None
        self.updates = 0                 # how many times set_rows changed nothing structural (for tests)
        self.resets = 0                  # never incremented: the model is not reset

    # --- columns ---
    def set_columns(self, columns: list[Column]) -> None:
        """Replace the columns (the rows stay). The sort column is kept by key when it is still there."""
        key = self.columns[self._sort_column].key if self.columns else ""
        self.beginResetModel()
        self.columns = list(columns)
        self._sort_column = next((i for i, column in enumerate(columns) if column.key == key), 0)
        self._root.children.clear()
        self._nodes.clear()
        self._by_key.clear()
        self.endResetModel()
        self.set_rows(self._rows)

    def column_index(self, key: str) -> int:
        return next((i for i, column in enumerate(self.columns) if column.key == key), -1)

    def set_metric(self, key: str, text: str) -> None:
        """The figure shown above a column's title (the total of the column)."""
        if self._metrics.get(key) != text:
            self._metrics[key] = text
            section = self.column_index(key)
            if section >= 0:
                self.headerDataChanged.emit(Qt.Orientation.Horizontal, section, section)

    # --- sorting ---
    def sort_key(self) -> tuple[str, bool]:
        return self.columns[self._sort_column].key, self._descending

    def set_sort(self, key: str, descending: bool) -> None:
        column = self.column_index(key)
        if column >= 0 and (column, descending) != (self._sort_column, self._descending):
            self._sort_column, self._descending = column, descending
            self.set_rows(self._rows)
            self.sort_changed.emit(key, descending)

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        if 0 <= column < len(self.columns):
            self.set_sort(self.columns[column].key, order == Qt.SortOrder.DescendingOrder)

    def _ordered(self, rows: list[Row]) -> list[Row]:
        column = self.columns[self._sort_column]
        keyed, without = [], []
        for row in rows:
            if row.header:
                continue
            value = column.sort(row)
            (without if value is None else keyed).append((value, row))
        try:
            keyed.sort(key=lambda pair: pair[0], reverse=self._descending)
        except TypeError:                       # values of different kinds: order by their text
            keyed.sort(key=lambda pair: str(pair[0]), reverse=self._descending)
        ordered = [row for _value, row in keyed] + [row for _value, row in without]
        headers = [row for row in rows if row.header]
        if headers or any(row.section for row in rows):
            ordered.sort(key=lambda row: row.section)            # stable: the order inside a section stays
            for header in headers:
                position = next((i for i, row in enumerate(ordered) if row.section >= header.section), len(ordered))
                ordered.insert(position, header)
        return ordered

    # --- the update ---
    def rows(self) -> list[Row]:
        return self._rows

    def set_rows(self, rows: list[Row]) -> None:
        self._rows = rows
        changed: list[_Node] = []
        reorders: list[tuple[_Node, list[_Node]]] = []
        self._sync(self._root, rows, changed, reorders)
        if reorders:
            self.layoutAboutToBeChanged.emit()
            before = self.persistentIndexList()
            for parent, wanted in reorders:
                parent.children[:] = wanted
                for position, node in enumerate(wanted):
                    node.position = position
            after = []
            for index in before:
                node = self._nodes.get(index.internalId())
                after.append(self.createIndex(node.position, index.column(), node.id) if node is not None
                             else QModelIndex())
            self.changePersistentIndexList(before, after)
            self.layoutChanged.emit()
        else:
            self.updates += 1
        if changed:
            last = len(self.columns) - 1
            by_parent: dict[int, list[_Node]] = {}
            for node in changed:
                by_parent.setdefault(node.parent.id, []).append(node)
            for nodes in by_parent.values():
                if len(nodes) > _RANGE_THRESHOLD:
                    siblings = nodes[0].parent.children
                    self.dataChanged.emit(self.createIndex(0, 0, siblings[0].id),
                                          self.createIndex(len(siblings) - 1, last, siblings[-1].id))
                else:
                    for node in nodes:
                        self.dataChanged.emit(self.createIndex(node.position, 0, node.id),
                                              self.createIndex(node.position, last, node.id))

    def _index_of(self, node: _Node) -> QModelIndex:
        return QModelIndex() if node is self._root else self.createIndex(node.position, 0, node.id)

    def _make(self, row: Row, parent: _Node) -> _Node:
        node = _Node(self._next_id, row, parent, len(self.columns))
        self._next_id += 1
        self._nodes[node.id] = node
        self._by_key[row.key] = node
        if row.children:
            for position, child_row in enumerate(self._ordered(row.children)):
                child = self._make(child_row, node)
                child.position = position
                node.children.append(child)
        return node

    def _forget(self, node: _Node) -> None:
        for child in node.children:
            self._forget(child)
        self._nodes.pop(node.id, None)
        if self._by_key.get(node.row.key) is node:
            del self._by_key[node.row.key]

    def _sync(self, parent: _Node, rows: list[Row], changed: list[_Node], reorders: list) -> None:
        ordered = self._ordered(rows) if rows else []
        children = parent.children
        wanted_keys = {row.key for row in ordered}
        if len(wanted_keys) != len(ordered):                    # two rows with one key: keep the first
            seen: set[str] = set()
            ordered = [row for row in ordered if not (row.key in seen or seen.add(row.key))]
        if any(node.row.key not in wanted_keys for node in children):
            last = len(children) - 1
            while last >= 0:
                if children[last].row.key in wanted_keys:
                    last -= 1
                    continue
                first = last
                while first > 0 and children[first - 1].row.key not in wanted_keys:
                    first -= 1
                self.beginRemoveRows(self._index_of(parent), first, last)
                for node in children[first:last + 1]:
                    self._forget(node)
                del children[first:last + 1]
                for position in range(first, len(children)):
                    children[position].position = position
                self.endRemoveRows()
                last = first - 1
        existing = {node.row.key: node for node in children}
        fresh = [row for row in ordered if row.key not in existing]
        made: set[int] = set()
        if fresh:
            start = len(children)
            self.beginInsertRows(self._index_of(parent), start, start + len(fresh) - 1)
            for row in fresh:
                node = self._make(row, parent)
                node.position = len(children)
                children.append(node)
                existing[row.key] = node
                made.add(node.id)
            self.endInsertRows()
        columns = len(self.columns)
        wanted = []
        for row in ordered:
            node = existing[row.key]
            wanted.append(node)
            if node.id in made:
                continue
            old = node.row
            node.row = row
            if old.sig != row.sig:
                node.texts = [None] * columns
                changed.append(node)
            if row.children or node.children:
                self._sync(node, row.children or [], changed, reorders)
        if wanted != children:
            reorders.append((parent, wanted))

    # --- QAbstractItemModel ---
    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        node = self._nodes.get(parent.internalId()) if parent.isValid() else self._root
        if node is None or not 0 <= row < len(node.children) or not 0 <= column < len(self.columns):
            return QModelIndex()
        return self.createIndex(row, column, node.children[row].id)

    def parent(self, index: QModelIndex = QModelIndex()) -> QModelIndex:
        node = self._nodes.get(index.internalId()) if index.isValid() else None
        if node is None or node.parent is self._root or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.parent.position, 0, node.parent.id)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if not parent.isValid():
            return len(self._root.children)
        if parent.column() > 0:
            return 0
        node = self._nodes.get(parent.internalId())
        return len(node.children) if node is not None else 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.columns)

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        return self.rowCount(parent) > 0

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        node = self._nodes.get(index.internalId()) if index.isValid() else None
        if node is None:
            return Qt.ItemFlag.NoItemFlags
        if node.row.header:
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        node = self._nodes.get(index.internalId())
        if node is None:
            return None
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            text = node.texts[column]
            if text is None:
                if node.row.header and column:
                    text = ""
                else:
                    text = self.columns[column].text(node.row)
                node.texts[column] = text
            return text
        if role == HEAT_ROLE:
            heat = self.columns[column].heat
            return None if heat is None or node.row.header else heat(node.row)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(RIGHT if self.columns[column].right else LEFT)
        if role == Qt.ItemDataRole.DecorationRole:
            icon = self.columns[column].icon
            return None if icon is None or node.row.header else icon(node.row)
        if role == Qt.ItemDataRole.FontRole:
            return fluent.font("body-strong") if node.row.header else None
        if role == Qt.ItemDataRole.ToolTipRole:
            if self.columns[column].tooltip and not node.row.header:
                return self.data(index, Qt.ItemDataRole.DisplayRole)
            return None
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{self.columns[column].title}: {self.data(index, Qt.ItemDataRole.DisplayRole)}"
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(self.columns):
            return None
        column = self.columns[section]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.title
        if role == METRIC_ROLE:
            return self._metrics.get(column.key)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(RIGHT if column.right else LEFT)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            metric = self._metrics.get(column.key)
            return f"{column.title}, {metric}" if metric else column.title
        return None

    # --- finding rows ---
    def row_at(self, index: QModelIndex) -> Row | None:
        node = self._nodes.get(index.internalId()) if index.isValid() else None
        return node.row if node is not None else None

    def index_of_key(self, key: str) -> QModelIndex:
        node = self._by_key.get(key)
        return QModelIndex() if node is None else self.createIndex(node.position, 0, node.id)

    def top_rows(self) -> list[Row]:
        """The top-level rows in the order they are shown."""
        return [node.row for node in self._root.children]

    def child_rows(self, key: str) -> list[Row]:
        node = self._by_key.get(key)
        return [child.row for child in node.children] if node is not None else []

    def text(self, key: str, column_key: str) -> str:
        """What the cell of row ``key`` in column ``column_key`` shows ("" when there is no such cell)."""
        index = self.index_of_key(key)
        column = self.column_index(column_key)
        if not index.isValid() or column < 0:
            return ""
        return self.data(index.siblingAtColumn(column), Qt.ItemDataRole.DisplayRole) or ""


# --- painting --------------------------------------------------------------------------------------------

def heat_colour(level: float) -> QColor:
    """The fill of a heat-map cell: the accent colour, the more opaque the higher ``level`` (0..1)."""
    level = max(0.0, min(1.0, level))
    colour = fluent.qcolor("accent")
    colour.setAlphaF(0.07 + 0.45 * level ** 0.6)
    return colour


class CellDelegate(TreeItemDelegate):
    """Cells as the toolkit draws them, with the heat-map fill behind the columns that have one."""

    def __init__(self, parent=None, tree: bool = True) -> None:
        super().__init__(parent)
        self._tree = tree

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        level = index.data(HEAT_ROLE)
        if level is not None:
            painter.fillRect(option.rect.adjusted(1, 1, -1, -1), heat_colour(level))
        super().paint(painter, option, index)
        if index.column() == 0 and option.state & QStyle.StateFlag.State_Selected and not self._tree:
            # A table without a tree column has no branch area for the style to draw the selection mark in.
            rect = QRectF(option.rect)
            paint.rounded_rect(painter, QRectF(rect.left(), rect.center().y() - 8, 3, 16), 1.5, "accent")

    def sizeHint(self, option, index) -> QSize:
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), max(hint.height(), ROW_HEIGHT))


class MetricHeader(QHeaderView):
    """A header whose sections may show a figure above the title (the total of a resource column)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setDefaultAlignment(LEFT)
        self.setMinimumSectionSize(56)
        self.setFixedHeight(HEADER_HEIGHT)
        self.setSectionsMovable(False)
        self.setAccessibleName("Columns")

    def paintSection(self, painter: QPainter, rect: QRect, logical: int) -> None:
        model = self.model()
        if model is None or not rect.isValid():
            return
        painter.save()
        painter.setClipRect(rect)
        title = model.headerData(logical, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or ""
        metric = model.headerData(logical, Qt.Orientation.Horizontal, METRIC_ROLE)
        align = model.headerData(logical, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole)
        right = bool(int(align or 0) & int(Qt.AlignmentFlag.AlignRight))
        flags = (Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        box = QRectF(rect).adjusted(8, 0, -8, 0)
        painter.fillRect(QRect(rect.left(), rect.bottom(), rect.width(), 1), fluent.qcolor("stroke-divider"))
        if logical != self.logicalIndex(self.count() - 1):
            painter.fillRect(QRect(rect.right(), rect.top() + 10, 1, rect.height() - 20), fluent.qcolor("stroke-divider"))
        sorted_here = self.isSortIndicatorShown() and self.sortIndicatorSection() == logical
        if metric:
            paint.elided(painter, QRectF(box.left(), rect.top() + 6, box.width(), 20), metric,
                         fluent.font("body-strong"), "text", flags)
        paint.elided(painter, QRectF(box.left(), rect.bottom() - 22, box.width(), 18), title, fluent.font("caption"),
                     "text" if sorted_here else "text-secondary", flags)
        if sorted_here:
            glyph = "chevron-down" if self.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder else "chevron-up"
            paint.glyph(painter, QRectF(rect.center().x() - 6, rect.top() + 1, 12, 8), glyph, "text-secondary", 8)
        painter.restore()

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), HEADER_HEIGHT)


class TableView(QTreeView):
    """The view of a TreeModel: one selected row, sorting by header, type-to-find, a context menu signal."""

    context_requested = Signal(QPoint)           # global position; the row is the current one
    delete_pressed = Signal()
    row_activated = Signal()

    def __init__(self, model: TreeModel, parent: QWidget | None = None, *, tree: bool = False,
                 find_column: str | None = None, stretch: str | None = None) -> None:
        super().__init__(parent)
        self._model = model
        self._find_column = find_column
        self._stretch = stretch                 # the key of the column that takes the spare width (None: the last)
        self._typed = ""
        self._typed_at = 0.0
        self.setHeader(MetricHeader(self))
        self.setItemDelegate(CellDelegate(self, tree))
        self.setModel(model)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setUniformRowHeights(True)
        self.setAllColumnsShowFocus(True)
        self.setRootIsDecorated(tree)
        self.setItemsExpandable(tree)
        self.setIndentation(20 if tree else 0)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        header = self.header()
        header.setSectionsClickable(True)        # sorting is done by the model, not by QTreeView
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._header_clicked)
        model.sort_changed.connect(lambda _key, _descending: self._show_sort())
        model.modelReset.connect(self._apply_widths)
        model.rowsInserted.connect(self._span_headers)
        self._apply_widths()
        self._show_sort()

    def set_stretch(self, key: str | None) -> None:
        self._stretch = key
        self._apply_widths()

    def _apply_widths(self) -> None:
        header = self.header()
        stretch = self._model.column_index(self._stretch) if self._stretch else -1
        header.setStretchLastSection(stretch < 0)
        for section, column in enumerate(self._model.columns):
            header.setSectionResizeMode(section, QHeaderView.ResizeMode.Stretch if section == stretch
                                        else QHeaderView.ResizeMode.Interactive)
            if section != stretch:
                self.setColumnWidth(section, column.width)
        self._span_headers(QModelIndex(), 0, self._model.rowCount() - 1)
        self._show_sort()

    def _span_headers(self, parent: QModelIndex, first: int, last: int) -> None:
        if parent.isValid():
            return
        for row in range(first, last + 1):
            found = self._model.row_at(self._model.index(row, 0))
            if found is not None and found.header:
                self.setFirstColumnSpanned(row, QModelIndex(), True)

    def _show_sort(self) -> None:
        key, descending = self._model.sort_key()
        self.header().setSortIndicator(self._model.column_index(key), Qt.SortOrder.DescendingOrder if descending
                                       else Qt.SortOrder.AscendingOrder)

    def _header_clicked(self, section: int) -> None:
        column = self._model.columns[section]
        key, descending = self._model.sort_key()
        self._model.set_sort(column.key, (not descending) if key == column.key else column.descending_first)
        self._show_sort()

    # --- rows ---
    def current_row(self) -> Row | None:
        row = self._model.row_at(self.currentIndex())
        return None if row is None or row.header else row

    def select_key(self, key: str) -> bool:
        index = self._model.index_of_key(key)
        if not index.isValid():
            return False
        parent = index.parent()
        while parent.isValid():
            self.expand(parent)
            parent = parent.parent()
        self.setCurrentIndex(index)
        self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def rowsAboutToBeRemoved(self, parent: QModelIndex, start: int, end: int) -> None:
        # Qt would move the selection to a neighbour of a row that goes away; "End task" must never
        # find another process selected than the one the user chose.
        current = self.currentIndex()
        while current.isValid():
            if current.parent() == parent and start <= current.row() <= end:
                self.selectionModel().clear()
                self.setCurrentIndex(QModelIndex())
                break
            current = current.parent()
        super().rowsAboutToBeRemoved(parent, start, end)

    def keyboardSearch(self, search: str) -> None:
        """Typing selects the next row whose name starts with what was typed in the last second."""
        now = time.monotonic()
        self._typed = (self._typed + search if now - self._typed_at < 1.0 else search)
        self._typed_at = now
        wanted = self._typed.casefold()
        column = self._model.column_index(self._find_column) if self._find_column else 0
        start = self.currentIndex()
        index = start if start.isValid() and len(self._typed) > 1 else self.indexBelow(start) if start.isValid() \
            else self._model.index(0, 0)
        first_pass = True
        steps = 0
        while steps < 100000:
            if not index.isValid():
                if not first_pass:
                    return
                first_pass = False
                index = self._model.index(0, 0)
                if not index.isValid():
                    return
            row = self._model.row_at(index)
            if row is not None and not row.header:
                text = self._model.data(index.siblingAtColumn(max(column, 0)), Qt.ItemDataRole.DisplayRole) or ""
                if text.casefold().startswith(wanted):
                    self.setCurrentIndex(index.siblingAtColumn(0))
                    self.scrollTo(index)
                    return
            if not first_pass and start.isValid() and index == start.siblingAtColumn(index.column()):
                return
            index = self.indexBelow(index)
            steps += 1

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Delete):
            self.delete_pressed.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.current_row() is not None:
            index = self.currentIndex()
            if self._model.hasChildren(index.siblingAtColumn(0)):
                self.setExpanded(index.siblingAtColumn(0), not self.isExpanded(index.siblingAtColumn(0)))
            else:
                self.row_activated.emit()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and not self._model.hasChildren(index.siblingAtColumn(0)):
            row = self._model.row_at(index)
            if row is not None and not row.header:
                self.row_activated.emit()
                return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        if event.reason() == event.Reason.Mouse:
            index = self.indexAt(event.pos())
            if index.isValid():
                self.setCurrentIndex(index.siblingAtColumn(0))
            position = event.globalPos()
        else:
            rect = self.visualRect(self.currentIndex())
            position = self.viewport().mapToGlobal(rect.bottomLeft() + QPoint(24, 0))
        if self.current_row() is not None:
            self.context_requested.emit(position)

    def elided_width(self, text: str) -> int:
        return QFontMetrics(self.font()).horizontalAdvance(text)
