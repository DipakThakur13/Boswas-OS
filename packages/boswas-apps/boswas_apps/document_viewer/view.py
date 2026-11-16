"""The page view: pages of a PDF document, scrolled, zoomed, selected and clicked.

``PdfView`` paints only from images it already has; everything else is
asked of the ``Renderer`` thread. A page whose sharp image has not arrived
yet is drawn from the image of the previous zoom, stretched, so zooming and
fast scrolling never wait. Above a size limit a page is not rendered whole
any more: a coarse image of the whole page stays underneath and the part
that is on the screen is rendered at full resolution on top.

Images are kept within a memory budget; the pages furthest from the one
being read are dropped first.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QFrame

import boswas_fluent as fluent

from . import layout as lay
from . import textsearch
from .document import Link, PdfDocument
from .render import Renderer, Request

FULL_PAGE_PIXELS = 12_000_000          # up to this many pixels a page is rendered whole
BASE_PIXELS = 3_000_000                # above it: the coarse whole page underneath has about this many
CACHE_BYTES = 192 * 1024 * 1024        # memory for page images of one view
LINE_STEP = 48
SELECT, HAND = "select", "hand"


def _tint(name: str, alpha: float) -> QColor:
    colour = QColor(fluent.qcolor(name))
    colour.setAlphaF(alpha)
    return colour


class PdfView(QAbstractScrollArea):
    page_changed = Signal(int)               # the page that counts as current
    zoom_changed = Signal(float, str)        # zoom factor, zoom mode
    link_hovered = Signal(str)               # what a link under the pointer leads to; "" when none
    web_link_clicked = Signal(str)           # the address of a web link the user clicked
    jumped = Signal()                        # an internal link was followed (the history has a new entry)
    selection_changed = Signal()
    context_requested = Signal(QPoint)       # global position
    view_changed = Signal()                  # zoom, rotation or arrangement changed

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Document pages")
        self.viewport().setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.viewport().setAutoFillBackground(False)
        self.grabGesture(Qt.GestureType.PinchGesture)
        self.verticalScrollBar().setSingleStep(LINE_STEP)
        self.horizontalScrollBar().setSingleStep(LINE_STEP)
        self.document: PdfDocument | None = None
        self.renderer: Renderer | None = None
        self.zoom = 1.0
        self.zoom_mode = lay.FIT_WIDTH
        self.rotation = 0
        self.continuous = True
        self.columns = 1
        self.cover = False
        self.tool = SELECT
        self.history = lay.History()
        self._layout = lay.Layout()
        self._row = 0                                    # the row shown when not continuous
        self._page = -1
        self._pages: dict[int, tuple[QImage, tuple]] = {}
        self._details: dict[int, tuple[QImage, tuple, tuple]] = {}
        self._selection_page = -1
        self._selection = None                           # QPdfSelection
        self._selection_rects: list[tuple] = []
        self._hit_source = None                          # page → list of rectangle lists
        self._current_hit: tuple[int, list] | None = None
        self._press: QPoint | None = None
        self._last_press: QPoint | None = None
        self._press_link: Link | None = None
        self._press_page = -1
        self._press_point = (0.0, 0.0)
        self._panning = False
        self._clicks = 0
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(QApplication.doubleClickInterval())
        self._schedule = QTimer(self)
        self._schedule.setSingleShot(True)
        self._schedule.setInterval(0)
        self._schedule.timeout.connect(self._request_renders)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.horizontalScrollBar().valueChanged.connect(self._scrolled)

    # --- the document ---
    def set_document(self, document: PdfDocument | None, renderer: Renderer | None) -> None:
        if self.renderer is not None:
            try:
                self.renderer.rendered.disconnect(self._rendered)
            except (RuntimeError, TypeError):
                pass
        self.document = document
        self.renderer = renderer
        self._pages.clear()
        self._details.clear()
        self.clear_selection()
        self._current_hit = None
        self._page = -1
        self._row = 0
        self.history.clear()
        if renderer is not None:
            renderer.rendered.connect(self._rendered)
        self._relayout()
        self.verticalScrollBar().setValue(0)
        self._update_page()
        self.viewport().update()

    def page_count(self) -> int:
        return self.document.page_count if self.document is not None else 0

    def current_page(self) -> int:
        return max(0, self._page)

    # --- geometry ---
    def _sizes(self) -> list[tuple[float, float]]:
        return self.document.sizes if self.document is not None else []

    def _fit_room(self) -> tuple[float, float]:
        # Room for fitting: the viewport as it is when the vertical scroll bar is shown (it almost always is).
        size = self.maximumViewportSize()
        bar = self.verticalScrollBar().sizeHint().width() if self.continuous and self.page_count() > 1 else 0
        return max(50.0, size.width() - bar), max(50.0, float(size.height()))

    def _relayout(self) -> None:
        sizes = self._sizes()
        if not sizes:
            self._layout = lay.Layout()
            self.horizontalScrollBar().setRange(0, 0)
            self.verticalScrollBar().setRange(0, 0)
            return
        rows = lay.rows_of(len(sizes), self.columns, self.cover)
        self._row = max(0, min(self._row, len(rows) - 1))
        if self.zoom_mode != lay.CUSTOM:
            width, height = self._fit_room()
            self.zoom = lay.fit_zoom(self.zoom_mode, sizes, self.rotation, self.columns, self.cover, width, height,
                                     None if self.continuous else self._row)
        viewport = self.viewport().size()
        self._layout = lay.build(sizes, self.zoom, self.rotation, self.columns, self.cover, viewport.width(),
                                 None if self.continuous else self._row, viewport.height())
        self.horizontalScrollBar().setRange(0, max(0, round(self._layout.width) - viewport.width()))
        self.verticalScrollBar().setRange(0, max(0, round(self._layout.height) - viewport.height()))
        self.horizontalScrollBar().setPageStep(viewport.width())
        self.verticalScrollBar().setPageStep(max(LINE_STEP, viewport.height() - LINE_STEP))
        self._schedule.start()

    def _offset(self) -> tuple[int, int]:
        return self.horizontalScrollBar().value(), self.verticalScrollBar().value()

    def page_rect(self, page: int) -> QRectF | None:
        """Where ``page`` is in the viewport now (it may be outside it); None when it is not laid out."""
        rect = self._layout.rect(page)
        if rect is None:
            return None
        ox, oy = self._offset()
        return QRectF(rect[0] - ox, rect[1] - oy, rect[2], rect[3])

    def _visible_pages(self, margin: float = 0.0) -> list[int]:
        _ox, oy = self._offset()
        return self._layout.pages_between(oy - margin, oy + self.viewport().height() + margin)

    def _page_at(self, position: QPoint | QPointF) -> tuple[int, float, float]:
        """The page under a viewport position and the point on it in points; page -1 when outside every page."""
        ox, oy = self._offset()
        x, y = position.x() + ox, position.y() + oy
        page = self._layout.page_at(x, y)
        rect = self._layout.rect(page) if page >= 0 else None
        if rect is None:
            return -1, 0.0, 0.0
        inside = rect[0] <= x <= rect[0] + rect[2] and rect[1] <= y <= rect[1] + rect[3]
        px, py = lay.to_page(x, y, rect, self._sizes()[page], self.rotation)
        return (page if inside else -1), px, py

    def _nearest_page_point(self, position: QPoint | QPointF, page: int) -> tuple[float, float]:
        ox, oy = self._offset()
        rect = self._layout.rect(page)
        x = min(max(position.x() + ox, rect[0]), rect[0] + rect[2])
        y = min(max(position.y() + oy, rect[1]), rect[1] + rect[3])
        return lay.to_page(x, y, rect, self._sizes()[page], self.rotation)

    # --- anchors: keeping a point of the document under a point of the viewport ---
    def _anchor(self, position: QPointF) -> tuple[int, float, float] | None:
        ox, oy = self._offset()
        x, y = position.x() + ox, position.y() + oy
        page = self._layout.page_at(x, y)
        rect = self._layout.rect(page) if page >= 0 else None
        if rect is None or not rect[2] or not rect[3]:
            return None
        return page, (x - rect[0]) / rect[2], (y - rect[1]) / rect[3]

    def _restore(self, anchor: tuple[int, float, float] | None, position: QPointF) -> None:
        if anchor is None:
            return
        page, fx, fy = anchor
        if not self.continuous:
            self._row = lay.row_of_page(page, self.page_count(), self.columns, self.cover)
        rect = self._layout.rect(page)
        if rect is None:
            return
        self.horizontalScrollBar().setValue(round(rect[0] + fx * rect[2] - position.x()))
        self.verticalScrollBar().setValue(round(rect[1] + fy * rect[3] - position.y()))

    def _top_anchor(self) -> tuple[tuple[int, float, float] | None, QPointF]:
        position = QPointF(self.viewport().width() / 2, 0)
        page = self.current_page()
        rect = self.page_rect(page)
        if rect is not None and self.continuous:
            return (page, 0.5, (0 - rect.top()) / rect.height() if rect.height() else 0.0), position
        return self._anchor(position), position

    # --- zoom, rotation, arrangement ---
    def set_zoom(self, zoom: float, mode: str = lay.CUSTOM, at: QPointF | None = None) -> None:
        """Zoom to a factor, or to a fit mode (``zoom`` is then ignored); ``at`` stays where it is on the screen."""
        if at is None:
            anchor, position = self._top_anchor()
        else:
            anchor, position = self._anchor(at), at
        self.zoom_mode = mode
        if mode == lay.CUSTOM:
            self.zoom = lay.clamp_zoom(zoom)
        self._details.clear()
        self._relayout()
        self._restore(anchor, position)
        self._update_page()
        self.zoom_changed.emit(self.zoom, self.zoom_mode)
        self.view_changed.emit()
        self.viewport().update()

    def zoom_in(self) -> None:
        self.set_zoom(lay.zoom_in(self.zoom), at=QPointF(self.viewport().rect().center()))

    def zoom_out(self) -> None:
        self.set_zoom(lay.zoom_out(self.zoom), at=QPointF(self.viewport().rect().center()))

    def rotate(self, quarter_turns: int) -> None:
        page = self.current_page()
        self.rotation = (self.rotation + quarter_turns) % 4
        self._pages.clear()
        self._details.clear()
        self._relayout()
        self.go_to_page(page, record=False)
        self.view_changed.emit()
        self.viewport().update()

    def set_arrangement(self, continuous: bool, columns: int, cover: bool) -> None:
        page = self.current_page()
        self.continuous, self.columns, self.cover = bool(continuous), 2 if columns == 2 else 1, bool(cover)
        self._details.clear()
        self._row = lay.row_of_page(page, self.page_count(), self.columns, self.cover)
        self._relayout()
        self.go_to_page(page, record=False)
        self.zoom_changed.emit(self.zoom, self.zoom_mode)
        self.view_changed.emit()
        self.viewport().update()

    def set_tool(self, tool: str) -> None:
        self.tool = HAND if tool == HAND else SELECT
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self.tool == HAND else Qt.CursorShape.ArrowCursor)

    def view_state(self) -> dict:
        """What is needed to show the document the same way again."""
        rect = self.page_rect(self.current_page())
        offset = 0.0
        if rect is not None and rect.height() > 0:
            offset = max(0.0, min(1.0, -rect.top() / rect.height()))
        return {"page": self.current_page(), "zoom": self.zoom, "mode": self.zoom_mode, "rotation": self.rotation,
                "continuous": self.continuous, "columns": self.columns, "cover": self.cover, "offset": offset}

    def restore_view(self, state: dict) -> None:
        self.zoom_mode = state.get("mode", lay.FIT_WIDTH)
        self.zoom = lay.clamp_zoom(state.get("zoom", 1.0))
        self.rotation = int(state.get("rotation", 0)) % 4
        self.continuous = bool(state.get("continuous", True))
        self.columns = 2 if state.get("columns") == 2 else 1
        self.cover = bool(state.get("cover", False))
        page = max(0, min(int(state.get("page", 0)), self.page_count() - 1))
        self._row = lay.row_of_page(page, self.page_count(), self.columns, self.cover)
        self._pages.clear()
        self._details.clear()
        self._relayout()
        self.go_to_page(page, record=False, fraction=float(state.get("offset", 0.0)))
        self.zoom_changed.emit(self.zoom, self.zoom_mode)

    # --- moving ---
    def position(self) -> tuple:
        return self.current_page(), self.verticalScrollBar().value()

    def go_to_page(self, page: int, y: float | None = None, *, record: bool = False, fraction: float = 0.0) -> None:
        """Show ``page`` at the top of the view (``y``: points from the top of the page)."""
        count = self.page_count()
        if count <= 0:
            return
        page = max(0, min(page, count - 1))
        if record:
            self.history.push(self.position())
        if not self.continuous:
            row = lay.row_of_page(page, count, self.columns, self.cover)
            if row != self._row:
                self._row = row
                self._details.clear()
                self._relayout()
        rect = self._layout.rect(page)
        if rect is None:
            return
        top = rect[1]
        if y is not None and self._sizes()[page][1] > 0 and self.rotation == 0:
            top += rect[3] * max(0.0, min(1.0, y / self._sizes()[page][1]))
        top += rect[3] * max(0.0, min(1.0, fraction))
        if self.continuous and not fraction and not y:
            # The top of the page with the gap above it; the first page with the margin, so the view starts at 0.
            top -= lay.MARGIN if rect[1] <= lay.MARGIN else lay.GAP
        self.verticalScrollBar().setValue(round(top))
        if self._layout.width <= self.viewport().width():
            self.horizontalScrollBar().setValue(0)
        self._update_page(force=page if not self.continuous else None)
        self.viewport().update()
        self._schedule.start()

    def next_page(self) -> None:
        self._step_row(1)

    def previous_page(self) -> None:
        self._step_row(-1)

    def _step_row(self, delta: int) -> None:
        count = self.page_count()
        rows = lay.rows_of(count, self.columns, self.cover)
        if not rows:
            return
        row = lay.row_of_page(self.current_page(), count, self.columns, self.cover) + delta
        if 0 <= row < len(rows):
            self.go_to_page(rows[row][0])

    def go_back(self) -> None:
        target = self.history.back(self.position())
        if target is not None:
            self._go_to_position(target)

    def go_forward(self) -> None:
        target = self.history.forward(self.position())
        if target is not None:
            self._go_to_position(target)

    def _go_to_position(self, target: tuple) -> None:
        page, value = target
        self.go_to_page(page)
        if self.continuous:
            self.verticalScrollBar().setValue(value)
        self.jumped.emit()

    def _scrolled(self) -> None:
        self._update_page()
        self.viewport().update()
        self._schedule.start()

    def _update_page(self, force: int | None = None) -> None:
        if force is not None:
            page = force
        elif not self._layout.rows:
            page = -1
        elif self.continuous:
            _ox, oy = self._offset()
            page = self._layout.main_page(oy, oy + self.viewport().height())
        else:
            page = self._layout.rows[0][0]
        if page != self._page:
            self._page = page
            if page >= 0:
                self.page_changed.emit(page)

    # --- rendering ---
    def _target(self, page: int) -> tuple[int, int]:
        rect = self._layout.rect(page)
        ratio = self.devicePixelRatioF()
        return max(1, round(rect[2] * ratio)), max(1, round(rect[3] * ratio))

    def _request_renders(self) -> None:
        if self.renderer is None or self.document is None or not self._layout.rows:
            return
        height = self.viewport().height()
        ox, oy = self._offset()
        visible = self._visible_pages()
        near = self._visible_pages(margin=height) if self.continuous else visible
        centre = self.current_page()
        wanted_pages, wanted_details = set(), set()
        ratio = self.devicePixelRatioF()
        for page in near:
            width, height_px = self._target(page)
            on_screen = page in visible
            priority = (0 if on_screen else 20) + abs(page - centre)
            if width * height_px <= FULL_PAGE_PIXELS:
                key = (width, height_px, self.rotation)
                request = Request("page", page, width, height_px, self.rotation)
            else:
                scale = (BASE_PIXELS / (width * height_px)) ** 0.5
                base = (max(1, round(width * scale)), max(1, round(height_px * scale)))
                key = (base[0], base[1], self.rotation)
                request = Request("page", page, base[0], base[1], self.rotation)
                if on_screen:
                    detail = self._detail_request(page, width, height_px, ratio, ox, oy)
                    if detail is not None:
                        wanted_details.add(detail)
                        self.renderer.request(detail, priority + 1)
            cached = self._pages.get(page)
            if cached is None or cached[1] != key:
                wanted_pages.add(request)
                self.renderer.request(request, priority)
        self.renderer.keep_only("page", wanted_pages)
        self.renderer.keep_only("detail", wanted_details)

    def _detail_request(self, page: int, width: int, height: int, ratio: float, ox: int, oy: int) -> Request | None:
        rect = self._layout.rect(page)
        view = QRectF(ox, oy, self.viewport().width(), self.viewport().height())
        part = QRectF(rect[0], rect[1], rect[2], rect[3]).intersected(view)
        if part.isEmpty():
            return None
        local = part.translated(-rect[0], -rect[1])
        current = self._details.get(page)
        if current is not None and current[1] == (width, height, self.rotation):
            have = QRectF(*current[2])
            if have.contains(local):
                return None
        grown = local.adjusted(-local.width() * 0.3, -local.height() * 0.3, local.width() * 0.3, local.height() * 0.3)
        grown = grown.intersected(QRectF(0, 0, rect[2], rect[3]))
        clip = (int(grown.left() * ratio), int(grown.top() * ratio),
                max(1, min(width, round(grown.width() * ratio))), max(1, min(height, round(grown.height() * ratio))))
        return Request("detail", page, width, height, self.rotation, clip)

    def _rendered(self, request: Request, image: QImage) -> None:
        if image.isNull() or request.kind not in ("page", "detail") or self._layout.rect(request.page) is None \
                and self.continuous:
            return
        if request.rotation != self.rotation:
            return
        if request.kind == "page":
            self._pages[request.page] = (image, (request.width, request.height, request.rotation))
            self._evict()
        else:
            ratio = self.devicePixelRatioF()
            x, y, w, h = request.clip
            self._details[request.page] = (image, (request.width, request.height, request.rotation),
                                           (x / ratio, y / ratio, w / ratio, h / ratio))
            for page in [page for page in self._details if page not in self._visible_pages()]:
                del self._details[page]
        self.viewport().update()

    def _evict(self) -> None:
        total = sum(image.sizeInBytes() for image, _key in self._pages.values())
        if total <= CACHE_BYTES:
            return
        centre = self.current_page()
        keep = set(self._visible_pages())
        for page in sorted(self._pages, key=lambda number: -abs(number - centre)):
            if total <= CACHE_BYTES:
                break
            if page in keep:
                continue
            total -= self._pages[page][0].sizeInBytes()
            del self._pages[page]

    def cached_bytes(self) -> int:
        return sum(image.sizeInBytes() for image, _key in self._pages.values())

    def has_sharp_image(self, page: int) -> bool:
        """Whether ``page`` is drawn at the resolution of the current zoom (tests wait for this)."""
        if self._layout.rect(page) is None:
            return False
        width, height = self._target(page)
        cached = self._pages.get(page)
        if width * height <= FULL_PAGE_PIXELS:
            return cached is not None and cached[1] == (width, height, self.rotation)
        detail = self._details.get(page)
        return detail is not None and detail[1] == (width, height, self.rotation)

    # --- painting ---
    def paintEvent(self, _event) -> None:
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), fluent.qcolor("background"))
        if not self._layout.rows:
            return
        ox, oy = self._offset()
        paper = fluent.qcolor("white")
        outline = QPen(fluent.qcolor("stroke-surface"), 1)
        sizes = self._sizes()
        for page in self._visible_pages():
            rect = self._layout.rect(page)
            target = QRectF(rect[0] - ox, rect[1] - oy, rect[2], rect[3])
            self._paint_shadow(painter, target)
            painter.fillRect(target, paper)
            cached = self._pages.get(page)
            if cached is not None:
                image = cached[0]
                exact = abs(image.width() - target.width() * self.devicePixelRatioF()) < 1.5
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, not exact)
                painter.drawImage(target, image)
            detail = self._details.get(page)
            if detail is not None and detail[1] == (*self._target(page), self.rotation):
                x, y, w, h = detail[2]
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
                painter.drawImage(QRectF(target.left() + x, target.top() + y, w, h), detail[0])
            self._paint_marks(painter, page, rect, sizes[page], ox, oy)
            painter.setPen(outline)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(target.adjusted(-0.5, -0.5, 0.5, 0.5))

    @staticmethod
    def _paint_shadow(painter: QPainter, target: QRectF) -> None:
        # A soft edge below and beside the paper, from plain one-pixel frames: this runs for every page of
        # every frame while scrolling, so it has to cost next to nothing.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for step, alpha in enumerate((0.16, 0.09, 0.05, 0.02), start=1):
            painter.setPen(QPen(_tint("smoke", alpha), 1))
            painter.drawRect(target.adjusted(-step + 0.5, -step + 1.5, step - 0.5, step + 0.5))

    def _paint_marks(self, painter: QPainter, page: int, rect: tuple, size: tuple, ox: int, oy: int) -> None:
        def box(area: tuple) -> QRectF:
            x, y, w, h = lay.rect_from_page(area, rect, size, self.rotation)
            return QRectF(x - ox, y - oy, w, h)

        painter.setPen(Qt.PenStyle.NoPen)
        if self._hit_source is not None:
            painter.setBrush(_tint("warning", 0.38))
            for rectangles in self._hit_source(page):
                for area in rectangles:
                    painter.drawRect(box(area))
        if self._current_hit is not None and self._current_hit[0] == page:
            painter.setBrush(_tint("accent", 0.42))
            painter.setPen(QPen(fluent.qcolor("accent"), 1.5))
            for area in self._current_hit[1]:
                painter.drawRect(box(area).adjusted(-1, -1, 1, 1))
            painter.setPen(Qt.PenStyle.NoPen)
        if self._selection_page == page and self._selection_rects:
            painter.setBrush(_tint("accent", 0.30))
            for area in self._selection_rects:
                painter.drawRect(box(area))

    # --- search marks ---
    def set_hit_source(self, source) -> None:
        """``source(page)`` gives the rectangles (in points) of every search hit on a page; None removes the marks."""
        self._hit_source = source
        self.viewport().update()

    def set_current_hit(self, page: int, rectangles: list | None) -> None:
        self._current_hit = (page, rectangles) if rectangles else None
        self.viewport().update()

    def show_rects(self, page: int, rectangles: list) -> None:
        """Scroll so that the first of ``rectangles`` on ``page`` is comfortably in view."""
        if not rectangles:
            self.go_to_page(page)
            return
        if not self.continuous:
            self.go_to_page(page)
        rect = self._layout.rect(page)
        if rect is None:
            return
        x, y, w, h = lay.rect_from_page(rectangles[0], rect, self._sizes()[page], self.rotation)
        ox, oy = self._offset()
        height, width = self.viewport().height(), self.viewport().width()
        if not (oy + 40 <= y and y + h <= oy + height - 40):
            self.verticalScrollBar().setValue(round(y - height / 3))
        if not (ox <= x and x + w <= ox + width):
            self.horizontalScrollBar().setValue(round(x - width / 3))
        self._update_page()

    # --- selection ---
    def has_selection(self) -> bool:
        return self._selection is not None and bool(self._selection_rects)

    def selected_text(self) -> str:
        return self._selection.text() if self._selection is not None else ""

    def clear_selection(self) -> None:
        had = self._selection is not None
        self._selection, self._selection_page, self._selection_rects = None, -1, []
        if had:
            self.selection_changed.emit()
            self.viewport().update()

    def _set_selection(self, page: int, selection) -> None:
        if selection is None or not selection.isValid():
            return
        self._selection_page = page
        self._selection = selection
        self._selection_rects = [(box.x(), box.y(), box.width(), box.height())
                                 for box in (polygon.boundingRect() for polygon in selection.bounds())]
        self.selection_changed.emit()
        self.viewport().update()

    def select_all_on_page(self) -> None:
        if self.document is not None and self.page_count():
            page = self.current_page()
            self._set_selection(page, self.document.qt.getAllText(page))

    def _select_range(self, page: int, start: int, end: int) -> None:
        if end > start:
            text = self.document.text(page)
            first = textsearch.utf16_length(text[:start])
            length = textsearch.utf16_length(text[start:end])
            self._set_selection(page, self.document.qt.getSelectionAtIndex(page, first, length))

    def _select_unit(self, page: int, x: float, y: float, line: bool) -> None:
        index16 = self.document.index_at(page, x, y)
        if index16 < 0:
            return
        text = self.document.text(page)
        index = index16
        if not text.isascii():                           # UTF-16 units back to characters
            units = 0
            for index, character in enumerate(text):
                if units >= index16:
                    break
                units += 2 if ord(character) > 0xFFFF else 1
        start, end = (textsearch.line_at if line else textsearch.word_at)(text, index)
        self._select_range(page, start, end)

    def copy_selection(self) -> bool:
        text = self.selected_text()
        if not text:
            return False
        QGuiApplication.clipboard().setText(text.replace("\r\n", "\n"))
        return True

    # --- links ---
    def _link_at(self, position: QPoint | QPointF) -> Link | None:
        if self.document is None:
            return None
        page, x, y = self._page_at(position)
        return self.document.link_at(page, x, y) if page >= 0 else None

    def link_text(self, link: Link) -> str:
        if link.url:
            return link.url
        return f"Go to page {self.document.label(link.page)}"

    def follow(self, link: Link) -> None:
        if link.url:
            self.web_link_clicked.emit(link.url)
        elif link.page >= 0:
            self.go_to_page(link.page, link.y or None, record=True)
            self.jumped.emit()

    # --- input ---
    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() != Qt.MouseButton.LeftButton or self.document is None:
            return
        position = event.position()
        self._press = position.toPoint()
        self._press_link = self._link_at(position)
        self._panning = False
        if self._press_link is not None:
            return
        near = self._last_press is not None and (self._press - self._last_press).manhattanLength() <= 6
        self._clicks = self._clicks + 1 if self._click_timer.isActive() and near else 1
        self._last_press = self._press
        self._click_timer.start()
        page, x, y = self._page_at(position)
        self._press_page, self._press_point = page, (x, y)
        if self.tool == HAND:
            self._panning = True
            self._pan_from = (position.toPoint(), *self._offset())
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif page >= 0 and self._clicks >= 2:
            self._select_unit(page, x, y, line=self._clicks >= 3)
        else:
            self.clear_selection()

    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        if self._press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            link = self._link_at(position)
            if link is not None:
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                self.link_hovered.emit(self.link_text(link))
            else:
                page, _x, _y = self._page_at(position)
                shape = Qt.CursorShape.OpenHandCursor if self.tool == HAND else (
                    Qt.CursorShape.IBeamCursor if page >= 0 else Qt.CursorShape.ArrowCursor)
                self.viewport().setCursor(shape)
                self.link_hovered.emit("")
            return
        if self._panning:
            start, ox, oy = self._pan_from
            delta = position.toPoint() - start
            self.horizontalScrollBar().setValue(ox - delta.x())
            self.verticalScrollBar().setValue(oy - delta.y())
            return
        if self._press_link is not None or self._press_page < 0 or self._clicks >= 2:
            return
        if (position.toPoint() - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        self._click_timer.stop()                    # a drag is not the first half of a double click
        x, y = self._nearest_page_point(position, self._press_page)
        selection = self.document.qt.getSelection(self._press_page, QPointF(*self._press_point), QPointF(x, y))
        if selection.isValid():
            self._set_selection(self._press_page, selection)
        self._autoscroll(position)

    def _autoscroll(self, position: QPointF) -> None:
        if position.y() < 0:
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - LINE_STEP // 2)
        elif position.y() > self.viewport().height():
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() + LINE_STEP // 2)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        link, self._press_link = self._press_link, None
        pressed, self._press = self._press, None
        if self._panning:
            self._panning = False
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        if link is not None and pressed is not None and self._link_at(event.position()) == link:
            self.follow(link)

    def contextMenuEvent(self, event) -> None:
        if self.document is not None:
            self.context_requested.emit(event.globalPos())

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self.set_zoom(self.zoom * (1.1 ** steps), at=event.position())
            event.accept()
            return
        bar = self.verticalScrollBar()
        if not self.continuous and event.angleDelta().y():
            down = event.angleDelta().y() < 0
            if (down and bar.value() >= bar.maximum()) or (not down and bar.value() <= bar.minimum()):
                before = self._row
                self._step_row(1 if down else -1)
                if self._row != before and not down:
                    bar.setValue(bar.maximum())
                event.accept()
                return
        super().wheelEvent(event)

    def event(self, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Gesture:
            pinch = event.gesture(Qt.GestureType.PinchGesture)
            if pinch is not None:
                factor = pinch.scaleFactor()
                if factor and abs(factor - 1.0) > 0.001:
                    centre = self.viewport().mapFromGlobal(pinch.centerPoint().toPoint())
                    self.set_zoom(self.zoom * factor, at=QPointF(centre))
                event.accept()
                return True
        elif kind == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self.set_zoom(self.zoom * (1.0 + event.value()), at=event.position())
                return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        vertical, horizontal = self.verticalScrollBar(), self.horizontalScrollBar()
        page_step = max(LINE_STEP, self.viewport().height() - LINE_STEP)
        if modifiers not in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
            super().keyPressEvent(event)
            return
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if key == Qt.Key.Key_Down:
            self._scroll_or_flip(LINE_STEP)
        elif key == Qt.Key.Key_Up:
            self._scroll_or_flip(-LINE_STEP)
        elif key == Qt.Key.Key_Left:
            horizontal.setValue(horizontal.value() - LINE_STEP) if horizontal.maximum() else self.previous_page()
        elif key == Qt.Key.Key_Right:
            horizontal.setValue(horizontal.value() + LINE_STEP) if horizontal.maximum() else self.next_page()
        elif key == Qt.Key.Key_PageDown or (key == Qt.Key.Key_Space and not shift):
            self._scroll_or_flip(page_step)
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_Backspace) or (key == Qt.Key.Key_Space and shift):
            self._scroll_or_flip(-page_step)
        elif key == Qt.Key.Key_Home:
            self.go_to_page(0)
        elif key == Qt.Key.Key_End:
            self.go_to_page(self.page_count() - 1)
            if self.continuous:
                vertical.setValue(vertical.maximum())
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _scroll_or_flip(self, delta: int) -> None:
        bar = self.verticalScrollBar()
        if not self.continuous and ((delta > 0 and bar.value() >= bar.maximum())
                                    or (delta < 0 and bar.value() <= bar.minimum())):
            before = self._row
            self._step_row(1 if delta > 0 else -1)
            if self._row != before and delta < 0:
                bar.setValue(bar.maximum())
            return
        bar.setValue(bar.value() + delta)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # What is at the top left corner stays there (a scroll bar that appears resizes the viewport too,
        # and must not undo the anchor of a zoom).
        position = QPointF(0, 0)
        anchor = self._anchor(position)
        self._relayout()
        self._restore(anchor, position)
        self._update_page()
        if self.zoom_mode != lay.CUSTOM:
            self.zoom_changed.emit(self.zoom, self.zoom_mode)

    def cursor_position(self) -> QPoint:
        return self.viewport().mapFromGlobal(QCursor.pos())

    def visible_rect_of(self, page: int) -> QRect:
        rect = self.page_rect(page)
        return rect.toAlignedRect().intersected(self.viewport().rect()) if rect is not None else QRect()
