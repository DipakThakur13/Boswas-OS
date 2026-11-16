"""The sidebar of a document: page thumbnails, the outline, and the list of search results."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPen
from PySide6.QtPdf import QPdfBookmarkModel
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QListView, QStackedWidget, QStyle, QStyledItemDelegate,
                               QTreeView, QVBoxLayout, QWidget)

import boswas_fluent as fluent

from .document import PdfDocument
from .render import Renderer, Request

THUMB_WIDTH = 132
_LABEL_HEIGHT = 24
_PADDING = 10
MAX_THUMBNAILS = 300                   # images kept; the list itself has a row for every page


class ThumbnailModel(QAbstractListModel):
    """One row per page. Images are rendered when a row is first painted and kept for the rows around it."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.document: PdfDocument | None = None
        self.renderer: Renderer | None = None
        self.images: dict[int, QImage] = {}
        self.asked: set[int] = set()
        self.current = 0
        self.ratio = 1.0
        self.cell = QSize(THUMB_WIDTH + 2 * _PADDING, 200)

    def set_document(self, document: PdfDocument | None, renderer: Renderer | None, ratio: float) -> None:
        self.beginResetModel()
        if self.renderer is not None:
            try:
                self.renderer.rendered.disconnect(self._rendered)
            except (RuntimeError, TypeError):
                pass
        self.document, self.renderer, self.ratio = document, renderer, ratio
        self.images.clear()
        self.asked.clear()
        self.current = 0
        if document is not None and document.sizes:
            width, height = document.sizes[0]
            tallest = min(2.0, max(0.5, height / width)) if width else 1.4
            self.cell = QSize(THUMB_WIDTH + 2 * _PADDING, round(THUMB_WIDTH * tallest) + _LABEL_HEIGHT + 2 * _PADDING)
        if renderer is not None:
            renderer.rendered.connect(self._rendered)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() or self.document is None else self.document.page_count

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.document is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.document.label(index.row())
        if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleTextRole):
            return f"Page {self.document.label(index.row())}"
        if role == Qt.ItemDataRole.SizeHintRole:
            return self.cell
        return None

    def thumb_size(self, page: int) -> QSize:
        width, height = self.document.sizes[page]
        room = self.cell.height() - _LABEL_HEIGHT - 2 * _PADDING
        scale = min(THUMB_WIDTH / width, room / height) if width and height else 1.0
        return QSize(max(1, round(width * scale)), max(1, round(height * scale)))

    def image(self, page: int) -> QImage | None:
        """The thumbnail of ``page`` if it is there; else it is asked for and None is returned."""
        found = self.images.get(page)
        if found is None and self.renderer is not None and page not in self.asked:
            size = self.thumb_size(page)
            self.asked.add(page)
            self.renderer.request(Request("thumb", page, max(1, round(size.width() * self.ratio)),
                                          max(1, round(size.height() * self.ratio))), 100 + abs(page - self.current))
        return found

    def _rendered(self, request: Request, image: QImage) -> None:
        if request.kind != "thumb" or self.document is None or not 0 <= request.page < self.document.page_count:
            return
        self.asked.discard(request.page)
        if image.isNull():
            return
        if len(self.images) >= MAX_THUMBNAILS:
            for page in sorted(self.images, key=lambda number: -abs(number - request.page))[:MAX_THUMBNAILS // 4]:
                del self.images[page]
        self.images[request.page] = image
        index = self.index(request.page)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])

    def set_current(self, page: int) -> None:
        if page == self.current:
            return
        old, self.current = self.current, page
        for row in (old, page):
            if 0 <= row < self.rowCount():
                index = self.index(row)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])


class _ThumbnailDelegate(QStyledItemDelegate):
    def sizeHint(self, _option, index: QModelIndex) -> QSize:
        return index.model().cell

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        model: ThumbnailModel = index.model()
        page = index.row()
        cell = QRect(option.rect)
        painter.save()
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if hovered or page == model.current:
            fluent.paint.rounded_rect(painter, cell.adjusted(2, 2, -2, -2), fluent.theme().radius("control"),
                                      "fill-subtle-hover")
        size = model.thumb_size(page)
        room = cell.height() - _LABEL_HEIGHT - 2 * _PADDING
        target = QRect(cell.left() + (cell.width() - size.width()) // 2,
                       cell.top() + _PADDING + (room - size.height()) // 2, size.width(), size.height())
        painter.fillRect(target, fluent.qcolor("white"))
        image = model.image(page)
        if image is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, image)
        if page == model.current:
            painter.setPen(QPen(fluent.qcolor("accent"), 2))
            painter.drawRect(QRectF(target).adjusted(-1, -1, 1, 1))
        else:
            painter.setPen(QPen(fluent.qcolor("stroke-surface"), 1))
            painter.drawRect(QRectF(target).adjusted(-0.5, -0.5, 0.5, 0.5))
        label = QRect(cell.left(), cell.bottom() - _LABEL_HEIGHT - _PADDING // 2, cell.width(), _LABEL_HEIGHT)
        fluent.paint.elided(painter, label, str(index.data() or ""), fluent.font("caption"),
                            "text" if page == model.current else "text-secondary", Qt.AlignmentFlag.AlignCenter)
        painter.restore()


class ResultsModel(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.hits: list = []
        self.labels = lambda page: str(page + 1)

    def set_hits(self, hits: list) -> None:
        self.beginResetModel()
        self.hits = hits
        self.endResetModel()

    def append(self, hits: list) -> None:
        if not hits:
            return
        self.beginInsertRows(QModelIndex(), len(self.hits), len(self.hits) + len(hits) - 1)
        self.hits.extend(hits)
        self.endInsertRows()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.hits)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.hits):
            return None
        hit = self.hits[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            # The match has to be in sight in a narrow list: only a few words lead up to it.
            before = hit.before if len(hit.before) <= 14 else "…" + hit.before[-13:].lstrip()
            return f"{self.labels(hit.page)}:  {before}{hit.text}{hit.after}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"Page {self.labels(hit.page)}\n{hit.line()}"
        return None


class Sidebar(QWidget):
    page_requested = Signal(int, float)        # page, points from the top of it
    result_requested = Signal(int)             # index of a search hit
    tab_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(200)
        self.pivot = fluent.Pivot()
        self.pivot.add_item("thumbnails", "Pages")
        self.pivot.add_item("outline", "Outline")
        self.pivot.add_item("results", "Results")
        self.thumbnails = QListView()
        self.thumbnail_model = ThumbnailModel(self)
        self.thumbnails.setModel(self.thumbnail_model)
        self.thumbnails.setItemDelegate(_ThumbnailDelegate(self.thumbnails))
        self.thumbnails.setUniformItemSizes(True)              # only the rows on screen are laid out and painted
        self.thumbnails.setViewMode(QListView.ViewMode.ListMode)
        self.thumbnails.setFrameShape(QFrame.Shape.NoFrame)
        self.thumbnails.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.thumbnails.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.thumbnails.verticalScrollBar().setSingleStep(40)
        self.thumbnails.setMouseTracking(True)
        self.thumbnails.setAccessibleName("Page thumbnails")
        self.thumbnails.clicked.connect(lambda index: self.page_requested.emit(index.row(), 0.0))
        self.thumbnails.activated.connect(lambda index: self.page_requested.emit(index.row(), 0.0))

        self.outline = QTreeView()
        self.outline.setHeaderHidden(True)
        self.outline.setFrameShape(QFrame.Shape.NoFrame)
        self.outline.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.outline.setAccessibleName("Outline")
        self.outline.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.outline.clicked.connect(self._outline_clicked)
        self.outline.activated.connect(self._outline_clicked)
        self.no_outline = fluent.Label("This document has no outline.", "body", tone="secondary", wrap=True)
        self.no_outline.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.no_outline.setContentsMargins(12, 24, 12, 0)
        self.outline_stack = QStackedWidget()
        self.outline_stack.addWidget(self.outline)
        self.outline_stack.addWidget(self.no_outline)

        self.results = QListView()
        self.results_model = ResultsModel(self)
        self.results.setModel(self.results_model)
        self.results.setFrameShape(QFrame.Shape.NoFrame)
        self.results.setUniformItemSizes(True)
        self.results.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setAccessibleName("Search results")
        self.results.clicked.connect(lambda index: self.result_requested.emit(index.row()))
        self.results.activated.connect(lambda index: self.result_requested.emit(index.row()))
        self.no_results = fluent.Label("Search the document with Ctrl+F; every match is listed here.", "body",
                                       tone="secondary", wrap=True)
        self.no_results.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.no_results.setContentsMargins(12, 24, 12, 0)
        self.results_stack = QStackedWidget()
        self.results_stack.addWidget(self.results)
        self.results_stack.addWidget(self.no_results)
        self.results_stack.setCurrentWidget(self.no_results)

        self.stack = QStackedWidget()
        for widget in (self.thumbnails, self.outline_stack, self.results_stack):
            self.stack.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.pivot)
        layout.addWidget(self.stack, 1)
        self.pivot.current_changed.connect(self._tab)

    def _tab(self, key: str) -> None:
        self.stack.setCurrentIndex(("thumbnails", "outline", "results").index(key))
        self.tab_changed.emit(key)

    def show_tab(self, key: str) -> None:
        if key in ("thumbnails", "outline", "results"):
            self.pivot.set_current(key)

    def set_document(self, document: PdfDocument | None, renderer: Renderer | None) -> None:
        self.thumbnail_model.set_document(document, renderer, self.devicePixelRatioF())
        self.results_model.labels = document.label if document is not None else (lambda page: str(page + 1))
        self.set_results([], "")
        if document is not None:
            self.outline.setModel(document.bookmarks())
            self.outline_stack.setCurrentWidget(self.outline if document.has_outline() else self.no_outline)
            self.outline.expandToDepth(0)
        else:
            self.outline.setModel(None)
            self.outline_stack.setCurrentWidget(self.no_outline)

    def set_current_page(self, page: int) -> None:
        self.thumbnail_model.set_current(page)
        if self.thumbnails.isVisible():
            self.thumbnails.scrollTo(self.thumbnail_model.index(page), QAbstractItemView.ScrollHint.EnsureVisible)

    def _outline_clicked(self, index: QModelIndex) -> None:
        page = index.data(QPdfBookmarkModel.Role.Page.value)
        location = index.data(QPdfBookmarkModel.Role.Location.value)
        if isinstance(page, int) and page >= 0:
            self.page_requested.emit(page, float(location.y()) if location is not None else 0.0)

    def set_results(self, hits: list, query: str, searching: bool = False) -> None:
        self.results_model.set_hits(list(hits))
        self._results_state(query, searching)

    def add_results(self, hits: list, query: str, searching: bool) -> None:
        self.results_model.append(hits)
        self._results_state(query, searching)

    def _results_state(self, query: str, searching: bool) -> None:
        if self.results_model.hits:
            self.results_stack.setCurrentWidget(self.results)
            return
        if not query:
            self.no_results.setText("Search the document with Ctrl+F; every match is listed here.")
        elif searching:
            self.no_results.setText("Searching…")
        else:
            self.no_results.setText(f"Nothing found for “{query}”.")
        self.results_stack.setCurrentWidget(self.no_results)

    def select_result(self, row: int) -> None:
        if 0 <= row < self.results_model.rowCount():
            index = self.results_model.index(row)
            self.results.setCurrentIndex(index)
            self.results.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)
