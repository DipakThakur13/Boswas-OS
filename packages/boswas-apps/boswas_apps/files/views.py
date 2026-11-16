"""The file views of a tab: Details (a tree view without a tree) and the list, tile and icon layouts.

Both views show the same model and share one selection. They paint their
rows themselves, in the toolkit's colours; no widget is made per file.
What a tab needs to know arrives as signals of ``FileView``.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import (QEvent, QItemSelection, QItemSelectionModel, QModelIndex, QPoint, QRect, QRectF, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QCursor, QDrag, QFontMetrics, QGuiApplication, QIcon, QPainter, QPixmap, QTextLayout, QTextOption
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHeaderView, QLineEdit, QListView, QRubberBand,
                               QStackedLayout, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeView, QWidget)

import boswas_fluent as fluent
from boswas_fluent import paint

from . import clipboard, dnd, mime, uiicons
from .listing import Entry
from .model import ENTRY_ROLE, FOLDER, TRASH, FolderModel
from .text import split_extension

ROW_HEIGHT = 32
# layout, icon size, cell size, flow
VIEW_SPECS = {
    "list": ("row", 20, QSize(264, 30), QListView.Flow.TopToBottom),
    "icons-small": ("row", 20, QSize(232, 30), QListView.Flow.LeftToRight),
    "tiles": ("tile", 48, QSize(272, 72), QListView.Flow.LeftToRight),
    "icons-medium": ("icon", 48, QSize(112, 112), QListView.Flow.LeftToRight),
    "icons-large": ("icon", 96, QSize(152, 164), QListView.Flow.LeftToRight),
    "icons-xlarge": ("icon", 256, QSize(296, 328), QListView.Flow.LeftToRight),
}
DEFAULT_WIDTHS = {"name": 320, "date": 150, "type": 180, "size": 100, "location": 200, "deleted": 150}
_TYPE_AHEAD_SECONDS = 1.0


def _wrapped(painter: QPainter, rect: QRectF, text: str, font, colour, lines: int) -> None:
    """Centred text in at most ``lines`` lines, breaking anywhere, the last line cut with an ellipsis."""
    metrics = QFontMetrics(font)
    layout = QTextLayout(text, font)
    option = QTextOption(Qt.AlignmentFlag.AlignHCenter)
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    rows = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(rect.width())
        rows.append((line.textStart(), line.textLength()))
        if len(rows) > lines:
            break
    layout.endLayout()
    painter.save()
    painter.setFont(font)
    painter.setPen(colour)
    y = rect.top()
    for number, (start, length) in enumerate(rows[:lines]):
        piece = text[start:start + length]
        if number == lines - 1 and len(rows) > lines:
            piece = metrics.elidedText(text[start:], Qt.TextElideMode.ElideRight, int(rect.width()))
        painter.drawText(QRectF(rect.left(), y, rect.width(), metrics.height()),
                         int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), piece.strip())
        y += metrics.height()
    painter.restore()


class ItemDelegate(QStyledItemDelegate):
    """Paints an entry in the layout of the current view mode and provides the editor for renaming."""

    def __init__(self, host: "FileView") -> None:
        super().__init__(host)
        self.host = host

    # --- geometry ---
    def sizeHint(self, option, index: QModelIndex) -> QSize:
        spec = VIEW_SPECS.get(self.host.view_mode)
        if spec is None:
            return QSize(100, ROW_HEIGHT)
        return spec[2]

    def name_rect(self, rect: QRect, mode: str) -> QRect:
        """Where the name is written (and edited) inside the cell ``rect``."""
        spec = VIEW_SPECS.get(mode)
        if spec is None:
            return QRect(rect.left() + 44, rect.top(), max(40, rect.width() - 48), rect.height())
        layout, icon, _cell, _flow = spec
        if layout == "row":
            return QRect(rect.left() + 12 + icon + 8, rect.top(), max(40, rect.width() - icon - 28), rect.height())
        if layout == "tile":
            return QRect(rect.left() + 12 + icon + 12, rect.top() + 10, max(40, rect.width() - icon - 32), 20)
        return QRect(rect.left() + 4, rect.top() + 8 + icon + 6, rect.width() - 8, rect.height() - icon - 18)

    # --- painting ---
    def _icon(self, painter: QPainter, entry: Entry, box: QRect, thumbnail: bool) -> None:
        pixmap = None
        if thumbnail and not entry.is_dir and not entry.broken:
            ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
            pixmap = self.host.services.thumbnailer.get(entry.path, entry.mtime, entry.size,
                                                        round(max(box.width(), box.height()) * ratio))
        if pixmap is not None and not pixmap.isNull():
            size = pixmap.size().scaled(box.size(), Qt.AspectRatioMode.KeepAspectRatio)
            if pixmap.width() <= box.width() and pixmap.height() <= box.height():
                size = pixmap.size()
            target = QRect(box.center().x() - size.width() // 2, box.center().y() - size.height() // 2,
                           size.width(), size.height())
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(target, pixmap)
            painter.restore()
        else:
            mime.entry_icon(entry).paint(painter, box, Qt.AlignmentFlag.AlignCenter)
        if entry.is_symlink:
            emblem = self.host.link_emblem
            if not emblem.isNull():
                side = max(10, box.width() // 3)
                emblem.paint(painter, QRect(box.left(), box.bottom() - side + 1, side, side))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        entry = index.data(ENTRY_ROLE)
        if entry is None:
            return
        host = self.host
        mode = host.view_mode
        model: FolderModel = host.model
        column = model.columns()[index.column()] if index.column() < len(model.columns()) else "name"
        rect = option.rect
        painter.save()
        cut = entry.path in host.services.cut_paths
        name_colour = fluent.qcolor("text-muted" if entry.broken else "text")
        secondary = fluent.qcolor("text-secondary")
        body = fluent.font("body")
        if mode == "details":
            if cut:
                painter.setOpacity(0.5)
            if column == "name":
                self._icon(painter, entry, QRect(rect.left() + 14, rect.center().y() - 9, 20, 20), False)
                if not host.is_editing(index):
                    paint.elided(painter, QRectF(self.name_rect(rect, mode)).adjusted(0, 0, -4, 0),
                                 model.text(entry, "name"), body, name_colour)
            else:
                flags = Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignRight if column == "size"
                                                         else Qt.AlignmentFlag.AlignLeft)
                paint.elided(painter, QRectF(rect).adjusted(8, 0, -12, 0), model.text(entry, column), body, secondary,
                             flags)
            painter.restore()
            return
        layout, icon, _cell, _flow = VIEW_SPECS[mode]
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        box = QRectF(rect).adjusted(2, 2, -2, -2)
        radius = fluent.theme().radius("control")
        if host.drop_row == index.row():
            paint.rounded_rect(painter, box, radius, "fill-control-hover", "accent")
        elif selected:
            paint.rounded_rect(painter, box, radius, "fill-control-hover" if not hovered else "fill-subtle-hover",
                               "accent")
        elif hovered:
            paint.rounded_rect(painter, box, radius, "fill-subtle-hover")
        if cut:
            painter.setOpacity(0.5)
        editing = host.is_editing(index)
        name = model.text(entry, "name")
        if layout == "row":
            self._icon(painter, entry, QRect(rect.left() + 12, rect.center().y() - icon // 2, icon, icon), False)
            if not editing:
                paint.elided(painter, QRectF(self.name_rect(rect, mode)).adjusted(0, 0, -4, 0), name, body, name_colour)
        elif layout == "tile":
            self._icon(painter, entry, QRect(rect.left() + 12, rect.center().y() - icon // 2, icon, icon), True)
            x = rect.left() + 12 + icon + 12
            width = rect.width() - icon - 32
            caption = fluent.font("caption")
            if not editing:
                paint.elided(painter, QRectF(x, rect.top() + 10, width, 20), name, body, name_colour)
            paint.elided(painter, QRectF(x, rect.top() + 30, width, 16), entry.type_text, caption, secondary)
            if not entry.is_dir:
                paint.elided(painter, QRectF(x, rect.top() + 46, width, 16), model.text(entry, "size"), caption,
                             secondary)
        else:
            self._icon(painter, entry, QRect(rect.center().x() - icon // 2 + 1, rect.top() + 8, icon, icon), True)
            if not editing:
                caption = fluent.font("caption") if icon < 96 else body
                _wrapped(painter, QRectF(self.name_rect(rect, mode)), name, caption, name_colour, 2)
        painter.restore()

    # --- renaming ---
    def createEditor(self, parent: QWidget, option, index: QModelIndex) -> QWidget:
        editor = QLineEdit(parent)
        editor.setAccessibleName("New name")
        editor.setMaxLength(255)
        if VIEW_SPECS.get(self.host.view_mode, ("", 0))[0] == "icon":
            editor.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        return editor

    def setEditorData(self, editor: QLineEdit, index: QModelIndex) -> None:
        name = index.data(Qt.ItemDataRole.EditRole) or ""
        entry = index.data(ENTRY_ROLE)
        editor.setText(name)
        stem = len(name) if entry is not None and entry.is_dir else len(split_extension(name)[0])
        # Qt selects everything when the editor gets the focus; the stem is selected right after.
        QTimer.singleShot(0, lambda: self._select_stem(editor, stem))

    @staticmethod
    def _select_stem(editor: QLineEdit, stem: int) -> None:
        try:
            editor.setSelection(0, stem)
        except RuntimeError:                # the editor is gone already
            pass

    def setModelData(self, editor: QLineEdit, model, index: QModelIndex) -> None:
        model.setData(index, editor.text(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor: QWidget, option, index: QModelIndex) -> None:
        mode = self.host.view_mode
        rect = self.name_rect(option.rect, mode)
        height = max(28, editor.sizeHint().height())
        layout = VIEW_SPECS.get(mode, ("details",))[0]
        if layout == "icon":
            editor.setGeometry(option.rect.left() + 2, rect.top() - 2, option.rect.width() - 4, height)
        else:
            editor.setGeometry(rect.left() - 8, rect.center().y() - height // 2 + 1, rect.width() + 4, height)


class _ViewBehaviour:
    """What the Details view and the grid view have in common. Mixed into a QAbstractItemView."""

    host: "FileView"

    def _setup(self, host: "FileView") -> None:
        self.host = host
        self._middle = QModelIndex()
        self._pressed = QModelIndex()
        self.setItemDelegate(ItemDelegate(host))        # one each: a view only closes editors of its own delegate
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setTabKeyNavigation(False)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        editing = self.state() == QAbstractItemView.State.EditingState
        if not editing:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and modifiers == Qt.KeyboardModifier.NoModifier:
                self.host.activate_selection()
                return
            if key == Qt.Key.Key_Backspace and modifiers == Qt.KeyboardModifier.NoModifier:
                self.host.up_requested.emit()
                return
            if key == Qt.Key.Key_Escape:
                self.host.escape_pressed.emit()
        super().keyPressEvent(event)

    def keyboardSearch(self, text: str) -> None:
        self.host.type_ahead(text)

    def mousePressEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.MiddleButton:
            self._middle = index
            return
        self._pressed = index if event.button() == Qt.MouseButton.LeftButton else QModelIndex()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        position = event.position().toPoint()
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.indexAt(position)
            if index.isValid() and index.row() == self._middle.row():
                self.host.middle_clicked.emit(self.host.model.entry(index))
            self._middle = QModelIndex()
            return
        super().mouseReleaseEvent(event)
        if (event.button() == Qt.MouseButton.LeftButton and self.host.single_click
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            index = self.indexAt(position)
            if index.isValid() and self._pressed.isValid() and index.row() == self._pressed.row() \
                    and self.host.hit_item(self, index, position):
                self.host.activate(self.host.model.entry(index))
        self._pressed = QModelIndex()

    def mouseDoubleClickEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid():
            if not self.host.single_click:
                self.host.activate(self.host.model.entry(index))
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        index = self.indexAt(event.pos())
        selection = self.selectionModel()
        if index.isValid() and (event.reason() == event.Reason.Keyboard or self.host.hit_item(self, index, event.pos())):
            if not selection.isSelected(index):
                selection.select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                 | QItemSelectionModel.SelectionFlag.Rows)
                selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
            entry = self.host.model.entry(index)
        else:
            if event.reason() != event.Reason.Keyboard:
                selection.clearSelection()
            entries = self.host.selected_entries()
            entry = entries[0] if entries else None
        self.host.context_requested.emit(entry, event.globalPos())

    def startDrag(self, _actions) -> None:
        self.host.start_drag()

    def dragEnterEvent(self, event) -> None:
        self.host.drag_event(self, event)

    def dragMoveEvent(self, event) -> None:
        self.host.drag_event(self, event)

    def dragLeaveEvent(self, event) -> None:
        self.host.set_drop_row(-1)
        event.accept()

    def dropEvent(self, event) -> None:
        self.host.drop_event(self, event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.host.focused.emit()


class DetailsView(_ViewBehaviour, QTreeView):
    """Rows with columns. Draws its rows itself and adds what a QTreeView lacks: rubber-band selection."""

    def __init__(self, host: "FileView") -> None:
        super().__init__(host)
        self._setup(host)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setIndentation(0)
        self.setUniformRowHeights(True)
        self.setAllColumnsShowFocus(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSortingEnabled(False)
        self.setExpandsOnDoubleClick(False)
        self.setAccessibleName("Files, details view")
        self._hover_row = -1
        self._band: QRubberBand | None = None
        self._band_origin: QPoint | None = None          # in content coordinates
        self._band_base: QItemSelection | None = None
        header = self.header()
        header.setSectionsClickable(True)
        header.setSectionsMovable(False)
        header.setStretchLastSection(False)
        header.setSortIndicatorShown(True)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setMinimumSectionSize(60)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setFixedHeight(32)

    # --- rows ---
    def _row_box(self, y: int, height: int) -> QRectF:
        header = self.header()
        width = max(header.length(), self.viewport().width())
        return QRectF(4 - self.horizontalOffset(), y + 1, width - 8, height - 2)

    def drawRow(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row = index.row()
        selected = self.selectionModel().isRowSelected(row, QModelIndex())
        hovered = row == self._hover_row
        y, height = option.rect.y(), option.rect.height()
        radius = fluent.theme().radius("control")
        box = self._row_box(y, height)
        if row == self.host.drop_row:
            paint.rounded_rect(painter, box, radius, "fill-control-hover", "accent")
        elif selected:
            paint.rounded_rect(painter, box, radius, "fill-subtle-hover" if not hovered else "fill-control-hover")
            pill = QRectF(box.left(), box.center().y() - 8, 3, 16)
            paint.rounded_rect(painter, pill, 1.5, "accent")
        elif hovered:
            paint.rounded_rect(painter, box, radius, "fill-subtle-pressed")
        header = self.header()
        cell = QStyleOptionViewItem(option)
        model = self.model()
        for visual in range(header.count()):
            logical = header.logicalIndex(visual)
            if header.isSectionHidden(logical):
                continue
            cell.rect = QRect(header.sectionViewportPosition(logical), y, header.sectionSize(logical), height)
            if cell.rect.right() < 0 or cell.rect.left() > self.viewport().width():
                continue
            self.itemDelegate().paint(painter, cell, model.index(row, logical))

    def _set_hover(self, row: int) -> None:
        if row != self._hover_row:
            self._hover_row = row
            self.viewport().update()

    def leaveEvent(self, event) -> None:
        self._set_hover(-1)
        super().leaveEvent(event)

    # --- rubber band ---
    def _content_point(self, point: QPoint) -> QPoint:
        return QPoint(point.x() + self.horizontalOffset(), point.y() + self.verticalOffset())

    def mousePressEvent(self, event) -> None:
        position = event.position().toPoint()
        index = self.indexAt(position)
        on_item = index.isValid() and self.host.hit_item(self, index, position)
        if event.button() == Qt.MouseButton.LeftButton and not on_item:
            # A press beside a name or below the rows starts a selection rectangle.
            self._band_origin = self._content_point(position)
            self._band_base = QItemSelection(self.selectionModel().selection()) \
                if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier) \
                else None
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._pressed = index
            if index.isValid():
                super().mousePressEvent(event)          # a plain click still selects the row
            elif self._band_base is None:
                self.selectionModel().clearSelection()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        position = event.position().toPoint()
        if self._band_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            origin = self._band_origin
            current = self._content_point(position)
            if self._band is None:
                if (current - origin).manhattanLength() < QGuiApplication.styleHints().startDragDistance():
                    return
                self._band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
                self._band.show()
            rect = QRect(origin, current).normalized()
            self._band.setGeometry(rect.translated(-self.horizontalOffset(), -self.verticalOffset()))
            self._select_band(rect)
            if position.y() < 0:
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - 16)
            elif position.y() > self.viewport().height():
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() + 16)
            return
        self._set_hover(self.indexAt(position).row())
        super().mouseMoveEvent(event)

    def _select_band(self, rect: QRect) -> None:
        model = self.model()
        count = model.rowCount()
        selection = QItemSelection(self._band_base) if self._band_base is not None else QItemSelection()
        first = max(0, rect.top() // ROW_HEIGHT)
        last = min(count - 1, rect.bottom() // ROW_HEIGHT)
        if count and first <= last:
            selection.merge(QItemSelection(model.index(first, 0), model.index(last, model.columnCount() - 1)),
                            QItemSelectionModel.SelectionFlag.Select)
        self.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def mouseReleaseEvent(self, event) -> None:
        if self._band_origin is not None and event.button() == Qt.MouseButton.LeftButton:
            banded = self._band is not None
            if self._band is not None:
                self._band.hide()
                self._band.deleteLater()
                self._band = None
            self._band_origin = None
            self._band_base = None
            if banded:
                self._pressed = QModelIndex()
                self.setState(QAbstractItemView.State.NoState)
                return
        super().mouseReleaseEvent(event)

    def sizeHintForRow(self, _row: int) -> int:
        return ROW_HEIGHT


class GridView(_ViewBehaviour, QListView):
    """List, tiles and icons: a QListView in the layout of the view mode."""

    def __init__(self, host: "FileView") -> None:
        super().__init__(host)
        self._setup(host)
        self.setUniformItemSizes(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(2000)
        self.setSelectionRectVisible(True)
        self.setWordWrap(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAccessibleName("Files")

    def apply_mode(self, mode: str) -> None:
        _layout, icon, cell, flow = VIEW_SPECS[mode]
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setFlow(flow)
        self.setWrapping(True)
        self.setIconSize(QSize(icon, icon))
        self.setGridSize(cell)
        self.setSpacing(0)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        horizontal = flow == QListView.Flow.TopToBottom
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if horizontal
                                          else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if horizontal
                                        else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scheduleDelayedItemsLayout()

    def wheelEvent(self, event) -> None:
        # The List layout scrolls sideways: let the wheel do that.
        if self.flow() == QListView.Flow.TopToBottom and event.angleDelta().y() and not event.angleDelta().x():
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)


class FileView(QWidget):
    """The two views of a tab behind one interface."""

    activated = Signal(object)                  # an Entry to open
    context_requested = Signal(object, QPoint)  # the entry under the pointer (or None), global position
    middle_clicked = Signal(object)
    selection_changed = Signal()
    dropped = Signal(list, str, str)            # paths, destination location, "copy" or "move"
    up_requested = Signal()
    escape_pressed = Signal()
    focused = Signal()
    mode_changed = Signal(str)

    def __init__(self, model: FolderModel, services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.services = services
        self.view_mode = "details"
        self.single_click = bool(services.settings.get("single_click"))
        self.drop_row = -1
        self.link_emblem: QIcon = uiicons.named("emblem-symbolic-link")
        self._typed = ""
        self._typed_at = 0.0
        self._drag_device: int | None = None
        self._saving = QTimer(self)
        self._saving.setSingleShot(True)
        self._saving.setInterval(400)
        self._saving.timeout.connect(self._save_widths)
        self.tree = DetailsView(self)
        self.grid = GridView(self)
        self.delegate = self.tree.itemDelegate()
        # One selection for both views. Only the view that is shown has the model: a hidden view would
        # still lay out every row each time the rows change.
        self._selection = QItemSelectionModel(model, self)
        self._attach(self.tree)
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self.tree)
        self._stack.addWidget(self.grid)
        header = self.tree.header()
        header.sectionClicked.connect(self._header_clicked)
        header.sectionResized.connect(lambda *_args: self._saving.start())
        self._selection.selectionChanged.connect(lambda *_args: self.selection_changed.emit())
        model.columns_changed.connect(self._columns_changed)
        model.layoutChanged.connect(self._sync_sort_indicator)
        self._columns_changed()
        self.set_view_mode(str(services.settings.get("view_mode")))

    # --- modes ---
    def _attach(self, view: QAbstractItemView) -> None:
        other = self.grid if view is self.tree else self.tree
        if other.model() is self.model:
            other.setModel(None)
        if view.model() is not self.model:
            view.setModel(self.model)
            view.setSelectionModel(self._selection)

    def current_view(self) -> QAbstractItemView:
        return self.tree if self.view_mode == "details" else self.grid

    def set_view_mode(self, mode: str) -> None:
        if mode != "details" and mode not in VIEW_SPECS:
            mode = "details"
        had_focus = self.tree.hasFocus() or self.grid.hasFocus()
        current = self._selection.currentIndex()
        self.view_mode = mode
        if mode == "details":
            self._attach(self.tree)
            self._columns_changed()
            self._stack.setCurrentWidget(self.tree)
        else:
            self.grid.apply_mode(mode)
            self._attach(self.grid)
            self._stack.setCurrentWidget(self.grid)
        view = self.current_view()
        if had_focus:
            view.setFocus()
        if current.isValid():
            view.scrollTo(current)
        self.mode_changed.emit(mode)

    def _columns_changed(self) -> None:
        if self.tree.model() is not self.model:
            return
        widths = self.services.settings.get("column_widths")
        header = self.tree.header()
        blocked = header.blockSignals(True)
        wide = len(self.model.columns()) > 4
        for section, name in enumerate(self.model.columns()):
            width = widths.get(name) if isinstance(widths, dict) else None
            if name == "name" and wide:
                width = min(int(width or 260), 260)         # five columns: leave room for the others
            header.resizeSection(section, int(width or DEFAULT_WIDTHS.get(name, 150)))
        header.blockSignals(blocked)
        self._sync_sort_indicator()

    def _save_widths(self) -> None:
        if self.tree.model() is not self.model:
            return
        widths = dict(self.services.settings.get("column_widths") or {})
        header = self.tree.header()
        wide = len(self.model.columns()) > 4
        for section, name in enumerate(self.model.columns()):
            if not (name == "name" and wide):
                widths[name] = header.sectionSize(section)
        self.services.settings.set("column_widths", widths)

    def _sync_sort_indicator(self, *_args) -> None:
        if self.tree.model() is not self.model:
            return
        section = self.model.column_of(self.model.sort_column)
        header = self.tree.header()
        if section >= 0:
            header.setSortIndicator(section, Qt.SortOrder.DescendingOrder if self.model.sort_descending
                                    else Qt.SortOrder.AscendingOrder)

    def _header_clicked(self, section: int) -> None:
        columns = self.model.columns()
        if not 0 <= section < len(columns):
            return
        column = columns[section]
        descending = not self.model.sort_descending if column == self.model.sort_column else False
        self.sort(column, descending)

    def sort(self, column: str, descending: bool) -> None:
        self.model.set_sort(column, descending)
        if column in ("name", "date", "type", "size"):
            self.services.settings.set("sort_column", column)
            self.services.settings.set("sort_descending", descending)
        self._sync_sort_indicator()

    # --- selection ---
    def selection_model(self) -> QItemSelectionModel:
        return self._selection

    def selected_rows(self) -> list[int]:
        rows: set[int] = set()
        for part in self._selection.selection():
            if part.left() == 0:
                rows.update(range(part.top(), part.bottom() + 1))
        return sorted(rows)

    def selected_entries(self) -> list[Entry]:
        entries = self.model.rows
        return [entries[row] for row in self.selected_rows() if row < len(entries)]

    def selected_count(self) -> int:
        return sum(part.bottom() - part.top() + 1 for part in self._selection.selection()
                   if part.left() == 0)

    def _select_range(self, first: int, last: int, flags) -> None:
        model = self.model
        self._selection.select(
            QItemSelection(model.index(first, 0), model.index(last, model.columnCount() - 1)), flags)

    def select_all(self) -> None:
        count = self.model.rowCount()
        if count:
            self._select_range(0, count - 1, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def select_none(self) -> None:
        self._selection.clearSelection()

    def invert_selection(self) -> None:
        count = self.model.rowCount()
        if count:
            self._select_range(0, count - 1, QItemSelectionModel.SelectionFlag.Toggle)

    def select_paths(self, paths: list[str], scroll: bool = True) -> int:
        """Select the rows of ``paths`` (replacing the selection); returns how many were found."""
        model = self.model
        selection = QItemSelection()
        first = QModelIndex()
        found = 0
        last_column = model.columnCount() - 1
        for path in paths:
            row = model.row_of(path)
            if row < 0:
                continue
            found += 1
            index = model.index(row, 0)
            if not first.isValid():
                first = index
            selection.merge(QItemSelection(index, model.index(row, last_column)),
                            QItemSelectionModel.SelectionFlag.Select)
        selector = self._selection
        selector.select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if first.isValid():
            selector.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)
            if scroll:
                self.current_view().scrollTo(first, QAbstractItemView.ScrollHint.PositionAtCenter)
        return found

    def current_entry(self) -> Entry | None:
        return self.model.entry(self._selection.currentIndex())

    # --- opening ---
    def activate(self, entry: Entry | None) -> None:
        if entry is not None:
            self.activated.emit(entry)

    def activate_selection(self) -> None:
        entries = self.selected_entries()
        folders = [entry for entry in entries if entry.is_dir]
        if len(folders) == 1 and len(entries) == 1:
            self.activated.emit(folders[0])
            return
        for entry in entries[:20]:
            if not entry.is_dir or len(entries) == 1:
                self.activated.emit(entry)

    def hit_item(self, view: QAbstractItemView, index: QModelIndex, position: QPoint) -> bool:
        """Whether ``position`` is on the icon or the name of the row (not on the empty part of it)."""
        if view is not self.tree:
            return True
        entry = self.model.entry(index)
        if entry is None or index.column() != 0:
            return False
        rect = view.visualRect(self.model.index(index.row(), 0))
        name = self.delegate.name_rect(rect, "details")
        width = QFontMetrics(fluent.font("body")).horizontalAdvance(self.model.text(entry, "name"))
        return rect.left() + 8 <= position.x() <= min(name.left() + width + 8, rect.right())

    def type_ahead(self, text: str) -> None:
        """Select the next entry whose name starts with what was typed in the last second."""
        if not text or not text.isprintable():
            return
        now = time.monotonic()
        if now - self._typed_at > _TYPE_AHEAD_SECONDS:
            self._typed = ""
        self._typed_at = now
        self._typed += text.casefold()
        rows = self.model.rows
        if not rows:
            return
        current = self._selection.currentIndex().row()
        prefix = self._typed
        if len(set(prefix)) == 1:               # the same letter again: the next entry that starts with it
            prefix = prefix[0]
            begin = current + 1
        else:
            begin = max(current, 0)
        count = len(rows)
        for offset in range(count):
            row = (begin + offset) % count
            if rows[row].name.casefold().startswith(prefix):
                index = self.model.index(row, 0)
                selector = self._selection
                selector.select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                | QItemSelectionModel.SelectionFlag.Rows)
                selector.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                self.current_view().scrollTo(index)
                return

    # --- renaming ---
    def is_editing(self, index: QModelIndex) -> bool:
        view = self.current_view()
        return (view.state() == QAbstractItemView.State.EditingState and index.column() == 0
                and view.currentIndex().row() == index.row())

    def rename_current(self) -> bool:
        """Open the inline editor on the current entry; False when there is nothing to rename."""
        entries = self.selected_entries()
        if not entries or self.model.mode == TRASH:
            return False
        selector = self._selection
        current = selector.currentIndex()
        if not current.isValid() or not selector.isRowSelected(current.row(), QModelIndex()):
            current = self.model.index_of(entries[0].path)
        index = self.model.index(current.row(), 0)
        view = self.current_view()
        selector.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
        view.scrollTo(index)
        view.setFocus()
        view.edit(index)
        return view.state() == QAbstractItemView.State.EditingState

    def editor(self) -> QLineEdit | None:
        """The inline rename editor while it is open."""
        view = self.current_view()
        if view.state() != QAbstractItemView.State.EditingState:
            return None
        for child in view.viewport().findChildren(QLineEdit):
            if child.isVisible():
                return child
        return None

    # --- drag and drop ---
    def start_drag(self) -> None:
        entries = self.selected_entries()
        if not entries or self.model.mode == TRASH:
            return
        view = self.current_view()
        drag = QDrag(view)
        drag.setMimeData(clipboard.mime_data([entry.path for entry in entries]))
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        mime.entry_icon(entries[0]).paint(painter, QRect(8, 8, 48, 48))
        if len(entries) > 1:
            label = str(len(entries)) if len(entries) < 1000 else "999+"
            caption = fluent.font("caption")
            width = max(20, QFontMetrics(caption).horizontalAdvance(label) + 10)
            badge = QRectF(64 - width, 44, width, 20)
            paint.rounded_rect(painter, badge, 10, "accent")
            paint.elided(painter, badge, label, caption, "on-accent", Qt.AlignmentFlag.AlignCenter)
        painter.end()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(16, 16))
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction | Qt.DropAction.LinkAction,
                  Qt.DropAction.MoveAction)
        self.set_drop_row(-1)

    def set_drop_row(self, row: int) -> None:
        if row != self.drop_row:
            self.drop_row = row
            self.tree.viewport().update()
            self.grid.viewport().update()

    def _drop_target(self, view: QAbstractItemView, position: QPoint) -> tuple[str | None, int]:
        """(destination location, row to highlight) for a drop at ``position``; (None, -1) when it cannot land."""
        model = self.model
        index = view.indexAt(position)
        entry = model.entry(index)
        if entry is not None and entry.is_dir and model.mode != TRASH and (
                view is not self.tree or self.hit_item(view, index, position) or index.column() == 0):
            return entry.path, index.row()
        if model.mode == FOLDER and model.location:
            return model.location, -1
        if model.mode == TRASH:
            return dnd.TRASH_TARGET, -1
        return None, -1

    def drag_event(self, view: QAbstractItemView, event) -> None:
        data = event.mimeData()
        if not clipboard.has_files(data):
            event.ignore()
            return
        if event.type() == QEvent.Type.DragEnter:
            self._drag_device = dnd.source_device(data)
        destination, row = self._drop_target(view, event.position().toPoint())
        if destination is None:
            self.set_drop_row(-1)
            event.ignore()
            return
        action = dnd.choose_action(event.modifiers(), event.possibleActions(), self._drag_device, destination)
        if action == Qt.DropAction.IgnoreAction:
            self.set_drop_row(-1)
            event.ignore()
            return
        self.set_drop_row(row)
        event.setDropAction(action)
        event.accept()

    def drop_event(self, view: QAbstractItemView, event) -> None:
        destination, _row = self._drop_target(view, event.position().toPoint())
        self.set_drop_row(-1)
        paths, _cut = clipboard.read(event.mimeData())
        if destination is None or not paths:
            event.ignore()
            return
        action = dnd.choose_action(event.modifiers(), event.possibleActions(), dnd.source_device(event.mimeData()),
                                   destination)
        if action == Qt.DropAction.IgnoreAction:
            event.ignore()
            return
        move = action == Qt.DropAction.MoveAction
        if destination != dnd.TRASH_TARGET:
            paths = [path for path in paths if path != destination]
            if move and all(os.path.dirname(path) == destination for path in paths):
                event.ignore()                  # dropped where it came from
                return
        if not paths:
            event.ignore()
            return
        event.setDropAction(action)
        event.accept()
        self.dropped.emit(paths, destination, "move" if move else "copy")

    def pointer_entry(self) -> Entry | None:
        view = self.current_view()
        return self.model.entry(view.indexAt(view.viewport().mapFromGlobal(QCursor.pos())))
