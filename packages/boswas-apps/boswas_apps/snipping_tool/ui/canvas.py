"""SnipCanvas: the picture with its marks, zoom and pan, the drawing tools and the crop frame.

Zoom is measured against the picture's pixels on the screen's pixels:
100 % shows one picture pixel on one device pixel, so a snip of a HiDPI
screen at 100 % looks as large as the screen did. In the fitted state the
picture follows the size of the widget (never enlarged beyond 100 %).

The canvas does not change the edit: it reports what the user drew
(``item_added``), erased (``items_erased``) or framed for a crop, and the
window turns that into a new ``annotations.State``.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PySide6.QtWidgets import QLineEdit, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

from .. import annotations, picture
from ..annotations import Rect, State

MARGIN = 24                    # free space around the fitted picture, logical pixels
ERASER_RADIUS = 8.0            # logical pixels
HANDLE_REACH = 10.0            # how near a crop handle counts as on it, logical pixels
MIN_DRAG = 3.0                 # a shape shorter than this (logical pixels) is a click, not a shape
POINT_SPACING = 1.5            # the least distance between the points of a stroke, logical pixels
WHEEL_STEP = 1.0015            # zoom factor per unit of wheel angle: one notch is about 20 %
BUTTON_STEP = 1.25
MAX_ZOOM = 32.0
PAN_STEP = 48.0
DRAWING_TOOLS = ("pen", "highlighter", "arrow", "rectangle", "text")
_CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
            "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
            "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
            "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
            "move": Qt.CursorShape.SizeAllCursor}


class _TextEditor(QLineEdit):
    """The box a text mark is typed into; Enter places it, Esc drops it, leaving it places it too."""

    committed = Signal()
    cancelled = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setPlaceholderText("Type text")
        self.setAccessibleName("Text to place on the snip")
        self.setMaxLength(annotations.MAX_TEXT)
        self._done = False
        self.returnPressed.connect(self._commit)
        self.textChanged.connect(self._fit)

    def _commit(self) -> None:
        if not self._done:
            self._done = True
            self.committed.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._done = True
            self.cancelled.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self._commit()

    def _fit(self) -> None:
        metrics = QFontMetricsF(self.font())
        text = self.text() or self.placeholderText()
        width = metrics.horizontalAdvance(text) + metrics.height() + 24
        self.resize(max(80, min(int(width), max(80, self.parentWidget().width() - self.x() - 4))), self.height())


class SnipCanvas(QWidget):
    """Shows the picture of an edit; see the module text."""

    item_added = Signal(object)          # an annotations.Item
    items_erased = Signal(object)        # a tuple of indexes into the state's items
    crop_changed = Signal()              # the crop frame moved
    zoom_changed = Signal()
    text_editing = Signal(bool)          # a text is being typed (True) or no longer (False)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._pixmap: QPixmap | None = None
        self._state = State()
        self.family = fluent.font("body").family()
        self.tool = ""
        self.colour = fluent.qcolor("black").name().upper()
        self.size_index = 1
        self._scale = 1.0                  # logical pixels per picture pixel
        self._auto = True
        self._offset = QPointF()           # where the visible picture's top left corner is
        self._drag: str | None = None
        self._press = QPointF()
        self._press_offset = QPointF()
        self._live: list[tuple[float, float]] = []
        self._last_widget_point = QPointF()
        self._shape: tuple[tuple[float, float], tuple[float, float]] | None = None
        self._erasing: set[int] = set()
        self._frame: Rect | None = None   # the crop frame (picture pixels) while the crop tool is in use
        self._frame_start: Rect | None = None
        self._anchor = (0.0, 0.0)
        self._hover: QPointF | None = None
        self._space = False
        self._editor: _TextEditor | None = None
        self._editor_point = (0.0, 0.0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("fluentFocusRing", False)
        self.setMouseTracking(True)
        self.setAccessibleName("Snip")
        self.setAccessibleDescription("The picture being marked up. Arrow keys move it, Ctrl+plus and Ctrl+minus "
                                      "zoom.")
        self.setMinimumSize(120, 90)

    # --- content ---
    def has_picture(self) -> bool:
        return self._image is not None and not self._image.isNull()

    def image(self) -> QImage | None:
        return self._image

    def set_picture(self, image: QImage | None) -> None:
        self.finish_text(commit=False)
        self._image = image
        self._pixmap = QPixmap.fromImage(image) if image is not None and not image.isNull() else None
        self._state = State()
        self._drag = None
        self._live = []
        self._shape = None
        self._erasing = set()
        self._frame = self.visible() if self.tool == "crop" and self.has_picture() else None
        self._auto = True
        self._refit()
        self._update_cursor()
        self.update()

    def state(self) -> State:
        return self._state

    def set_state(self, state: State) -> None:
        """Show another state of the edit (after an edit, undo or redo)."""
        old_visible = self.visible()
        self._state = state
        self._erasing = set()
        if self.tool == "crop":
            self._frame = self.visible()
        if self.visible() != old_visible:
            if self._auto:
                self._refit()
            else:
                self._scale = max(self.min_scale(), min(self._scale, self.max_scale()))
                self._centre()
            self.zoom_changed.emit()
        self.update()

    def visible(self) -> Rect:
        if not self.has_picture():
            return Rect(0, 0, 0, 0)
        return self._state.visible(self._image.width(), self._image.height())

    def set_tool(self, tool: str) -> None:
        if tool == self.tool:
            return
        self.finish_text(commit=True)
        self._drag = None
        self._live = []
        self._shape = None
        self.tool = tool
        self._frame = self.visible() if tool == "crop" and self.has_picture() else None
        self._update_cursor()
        self.update()

    def width_for(self, kind: str) -> float:
        return annotations.WIDTHS[kind][self.size_index]

    # --- zoom arithmetic ---
    def _ratio(self) -> float:
        return self.devicePixelRatioF() or 1.0

    def fit_scale(self) -> float:
        box = self.visible()
        if box.is_empty():
            return 1.0
        width = max(1.0, self.width() - 2 * MARGIN)
        height = max(1.0, self.height() - 2 * MARGIN)
        return min(width / box.width, height / box.height)

    def default_scale(self) -> float:
        """Fitted, but never larger than 100 %."""
        return min(self.fit_scale(), 1.0 / self._ratio())

    def min_scale(self) -> float:
        return min(self.default_scale(), 0.05)

    def max_scale(self) -> float:
        return max(MAX_ZOOM / self._ratio(), self.fit_scale())

    def zoom(self) -> float:
        """Device pixels per picture pixel: 1.0 is 100 %."""
        return self._scale * self._ratio()

    def zoom_percent(self) -> int:
        return max(1, round(self.zoom() * 100))

    def is_fitted(self) -> bool:
        return self._auto

    def picture_rect(self) -> QRectF:
        box = self.visible()
        return QRectF(self._offset.x(), self._offset.y(), box.width * self._scale, box.height * self._scale)

    def transform(self) -> QTransform:
        """Picture pixels -> widget coordinates."""
        box = self.visible()
        return QTransform().translate(self._offset.x(), self._offset.y()).scale(self._scale, self._scale) \
            .translate(-box.x, -box.y)

    def to_picture(self, point: QPointF) -> tuple[float, float]:
        box = self.visible()
        return (box.x + (point.x() - self._offset.x()) / self._scale,
                box.y + (point.y() - self._offset.y()) / self._scale)

    def to_widget(self, x: float, y: float) -> QPointF:
        box = self.visible()
        return QPointF(self._offset.x() + (x - box.x) * self._scale, self._offset.y() + (y - box.y) * self._scale)

    def _widget_rect(self, rect: Rect) -> QRectF:
        top_left = self.to_widget(rect.x, rect.y)
        return QRectF(top_left.x(), top_left.y(), rect.width * self._scale, rect.height * self._scale)

    def _centre(self) -> None:
        rect = self.picture_rect()
        self._offset = QPointF((self.width() - rect.width()) / 2, (self.height() - rect.height()) / 2)
        self._clamp()

    def _clamp(self) -> None:
        rect = self.picture_rect()
        x, y = self._offset.x(), self._offset.y()
        if rect.width() + 2 * MARGIN <= self.width():
            x = (self.width() - rect.width()) / 2
        else:
            x = min(MARGIN, max(self.width() - rect.width() - MARGIN, x))
        if rect.height() + 2 * MARGIN <= self.height():
            y = (self.height() - rect.height()) / 2
        else:
            y = min(MARGIN, max(self.height() - rect.height() - MARGIN, y))
        self._offset = QPointF(x, y)

    def _refit(self) -> None:
        if not self.has_picture():
            return
        self._scale = self.default_scale()
        self._centre()
        self.zoom_changed.emit()

    def can_pan(self) -> bool:
        rect = self.picture_rect()
        return self.has_picture() and (rect.width() + 2 * MARGIN > self.width() + 0.5
                                       or rect.height() + 2 * MARGIN > self.height() + 0.5)

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        """Zoom to ``zoom`` (1.0 = 100 %), keeping ``anchor`` (default: the centre of the widget) in place."""
        if not self.has_picture():
            return
        anchor = QPointF(anchor) if anchor is not None else QPointF(self.width() / 2, self.height() / 2)
        scale = max(self.min_scale(), min(self.max_scale(), zoom / self._ratio()))
        picture_x, picture_y = self.to_picture(anchor)
        self._auto = False
        self._scale = scale
        box = self.visible()
        self._offset = QPointF(anchor.x() - (picture_x - box.x) * scale, anchor.y() - (picture_y - box.y) * scale)
        self._clamp()
        self._update_cursor()
        self._reposition_editor()
        self.zoom_changed.emit()
        self.update()

    def zoom_in(self) -> None:
        self.set_zoom(self.zoom() * BUTTON_STEP)

    def zoom_out(self) -> None:
        target = self.zoom() / BUTTON_STEP
        if target / self._ratio() <= self.default_scale() * 1.0001:
            self.zoom_fit()
        else:
            self.set_zoom(target)

    def zoom_fit(self) -> None:
        self._auto = True
        self._refit()
        self._update_cursor()
        self._reposition_editor()
        self.update()

    def zoom_actual(self) -> None:
        self.set_zoom(1.0)

    def toggle_zoom(self) -> None:
        """Between fitted and 100 % (for a snip smaller than the window: between 100 % and filling it)."""
        if not self._auto:
            self.zoom_fit()
        elif abs(self.zoom() - 1.0) > 1e-6:
            self.zoom_actual()
        else:
            self.set_zoom(self.fit_scale() * self._ratio())

    def pan_by(self, dx: float, dy: float) -> None:
        if self.can_pan():
            self._offset = QPointF(self._offset.x() + dx, self._offset.y() + dy)
            self._clamp()
            self._reposition_editor()
            self.update()

    # --- the crop frame ---
    def crop_frame(self) -> Rect | None:
        return self._frame

    def set_crop_frame(self, frame: Rect) -> None:
        if self.tool == "crop":
            self._frame = frame
            self.crop_changed.emit()
            self.update()

    def chosen_crop(self) -> Rect | None:
        """The frame on whole pixels, or None when it would not change the picture."""
        if self._frame is None:
            return None
        return annotations.snapped(self._frame, self.visible())

    # --- text ---
    def is_editing_text(self) -> bool:
        return self._editor is not None

    def text_editor(self) -> QLineEdit | None:
        return self._editor

    def _start_text(self, point: QPointF) -> None:
        self.finish_text(commit=True)
        self._editor_point = self.to_picture(point)
        editor = _TextEditor(self)
        size = self.width_for("text") * self._scale
        editor.setFont(picture.text_font(self.family, max(8.0, size)))
        editor.committed.connect(lambda: self.finish_text(commit=True))
        editor.cancelled.connect(lambda: self.finish_text(commit=False))
        self._editor = editor
        self._reposition_editor()
        editor.show()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self.text_editing.emit(True)

    def _reposition_editor(self) -> None:
        if self._editor is None:
            return
        point = self.to_widget(*self._editor_point)
        metrics = QFontMetricsF(self._editor.font())
        height = int(metrics.height() + 16)
        self._editor.setGeometry(round(point.x()) - 8, round(point.y()) - 8, self._editor.width(), height)
        self._editor._fit()

    def finish_text(self, commit: bool = True) -> None:
        """Place the text being typed (``commit``) or drop it."""
        editor, self._editor = self._editor, None
        if editor is None:
            return
        text = annotations.clean_text(editor.text())
        editor.hide()
        editor.deleteLater()
        if commit and text and self.has_picture():
            size = self.width_for("text")
            box = picture.measure_text(text, size, self.family)
            self.item_added.emit(annotations.text_item(self.colour, size, self._editor_point, text, box))
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.text_editing.emit(False)

    # --- painting ---
    def paintEvent(self, _event) -> None:
        if not self.has_picture():
            return
        painter = QPainter(self)
        rect = self.picture_rect()
        paint.shadow(painter, rect, 0, "flyout")
        box = self.visible()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom() < 2.0)
        painter.drawPixmap(rect, self._pixmap, QRectF(box.x, box.y, box.width, box.height))
        painter.save()
        painter.setClipRect(rect)
        painter.setTransform(self.transform(), True)
        items = [item for index, item in enumerate(self._state.items) if index not in self._erasing]
        live = self._live_item()
        if live is not None:
            items.append(live)
        picture.draw_items(painter, items, self.family)
        painter.restore()
        if self.tool == "crop" and self._frame is not None:
            self._paint_crop(painter, rect)
        elif self.tool == "eraser" and self._hover is not None and rect.contains(self._hover):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(fluent.qcolor("black"), 3))
            painter.drawEllipse(self._hover, ERASER_RADIUS, ERASER_RADIUS)
            painter.setPen(QPen(fluent.qcolor("white"), 1.5))
            painter.drawEllipse(self._hover, ERASER_RADIUS, ERASER_RADIUS)

    def _live_item(self) -> annotations.Item | None:
        try:
            if self._drag == "draw" and self._live:
                return annotations.stroke(self.tool, self.colour, self.width_for(self.tool), self._live)
            if self._drag == "shape" and self._shape is not None:
                return annotations.shape(self.tool, self.colour, self.width_for(self.tool), *self._shape)
        except ValueError:
            return None
        return None

    def _paint_crop(self, painter: QPainter, picture_rect: QRectF) -> None:
        frame = self._widget_rect(self._frame)
        outside = QPainterPath()
        outside.addRect(picture_rect)
        inner = QPainterPath()
        inner.addRect(frame)
        painter.fillPath(outside.subtracted(inner), fluent.qcolor("smoke"))
        dark, light = fluent.qcolor("black"), fluent.qcolor("white")
        thirds = QColor(light)
        thirds.setAlphaF(0.5)
        painter.setPen(QPen(thirds, 1))
        for step in (1, 2):
            x = frame.left() + frame.width() * step / 3
            y = frame.top() + frame.height() * step / 3
            painter.drawLine(QPointF(x, frame.top()), QPointF(x, frame.bottom()))
            painter.drawLine(QPointF(frame.left(), y), QPointF(frame.right(), y))
        painter.setPen(QPen(dark, 3))
        painter.drawRect(frame)
        painter.setPen(QPen(light, 1))
        painter.drawRect(frame)
        length = min(20.0, frame.width() / 3, frame.height() / 3)
        for pen in (QPen(dark, 6), QPen(light, 4)):
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            for x, sx in ((frame.left(), 1), (frame.right(), -1)):
                for y, sy in ((frame.top(), 1), (frame.bottom(), -1)):
                    painter.drawLine(QPointF(x, y), QPointF(x + sx * length, y))
                    painter.drawLine(QPointF(x, y), QPointF(x, y + sy * length))
            middle_x, middle_y = frame.center().x(), frame.center().y()
            painter.drawLine(QPointF(middle_x - length / 2, frame.top()), QPointF(middle_x + length / 2, frame.top()))
            painter.drawLine(QPointF(middle_x - length / 2, frame.bottom()),
                             QPointF(middle_x + length / 2, frame.bottom()))
            painter.drawLine(QPointF(frame.left(), middle_y - length / 2), QPointF(frame.left(), middle_y + length / 2))
            painter.drawLine(QPointF(frame.right(), middle_y - length / 2),
                             QPointF(frame.right(), middle_y + length / 2))
        snapped = self.chosen_crop() or self.visible()
        label = f"{int(snapped.width)} × {int(snapped.height)}"
        font = fluent.font("caption")
        metrics = QFontMetricsF(font)
        tag = QRectF(0, 0, metrics.horizontalAdvance(label) + 16, metrics.height() + 6)
        top = frame.top() - tag.height() - 6 if frame.top() - tag.height() - 6 >= 0 else frame.top() + 6
        tag.moveTo(max(4.0, min(frame.left(), self.width() - tag.width() - 4)), top)
        backdrop = fluent.qcolor("black")
        backdrop.setAlphaF(0.72)
        paint.rounded_rect(painter, tag, fluent.theme().radius("control"), backdrop)
        paint.elided(painter, tag, label, font, light, Qt.AlignmentFlag.AlignCenter)

    # --- pointer ---
    def _update_cursor(self) -> None:
        if self._drag == "pan":
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self.tool in ("pen", "highlighter", "arrow", "rectangle"):
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif self.tool == "text":
            self.setCursor(Qt.CursorShape.IBeamCursor)
        elif self.tool == "eraser":
            self.setCursor(Qt.CursorShape.BlankCursor if self._hover is not None else Qt.CursorShape.ArrowCursor)
        elif self.tool == "crop":
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif self.can_pan() or self._space:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def _eraser_radius(self) -> float:
        return ERASER_RADIUS / self._scale

    def _erase_at(self, point: QPointF) -> None:
        x, y = self.to_picture(point)
        hits = set(self._state.hits(x, y, self._eraser_radius())) - self._erasing
        if hits:
            self._erasing |= hits
            self.update()

    def mousePressEvent(self, event) -> None:
        if not self.has_picture():
            super().mousePressEvent(event)
            return
        point = event.position()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        button = event.button()
        if button == Qt.MouseButton.MiddleButton or (button == Qt.MouseButton.LeftButton
                                                      and (self._space or not self.tool)):
            self._drag = "pan"
            self._press = point
            self._press_offset = QPointF(self._offset)
            self._update_cursor()
            event.accept()
            return
        if button != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        x, y = self.to_picture(point)
        self._last_widget_point = point
        if self.tool in ("pen", "highlighter"):
            self._drag = "draw"
            self._live = [(x, y)]
        elif self.tool in ("arrow", "rectangle"):
            self._drag = "shape"
            self._shape = ((x, y), (x, y))
        elif self.tool == "eraser":
            self._drag = "erase"
            self._erasing = set()
            self._erase_at(point)
        elif self.tool == "crop" and self._frame is not None:
            handle = annotations.handle_at(self._frame, x, y, HANDLE_REACH / self._scale)
            self._frame_start = self._frame
            self._press = point
            if handle == "move" and self._frame == self.visible():
                handle = None                       # a frame round everything has nowhere to move: start a new one
            if handle is not None:
                self._drag = handle
            elif self.picture_rect().contains(point):
                self._drag = "new"
                self._anchor = (x, y)
        elif self.tool == "text":
            if self.picture_rect().contains(point):
                self._start_text(point)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        point = event.position()
        self._hover = point
        if self._drag is None:
            if self.tool == "crop" and self._frame is not None:
                x, y = self.to_picture(point)
                handle = annotations.handle_at(self._frame, x, y, HANDLE_REACH / self._scale)
                self.setCursor(_CURSORS.get(handle, Qt.CursorShape.CrossCursor))
            elif self.tool == "eraser":
                self._update_cursor()
                self.update()
            return
        x, y = self.to_picture(point)
        if self._drag == "pan":
            delta = point - self._press
            self._offset = QPointF(self._press_offset.x() + delta.x(), self._press_offset.y() + delta.y())
            self._clamp()
            self._reposition_editor()
        elif self._drag == "draw":
            if (point - self._last_widget_point).manhattanLength() >= POINT_SPACING \
                    and len(self._live) < annotations.MAX_POINTS:
                self._live.append((x, y))
                self._last_widget_point = point
        elif self._drag == "shape" and self._shape is not None:
            start = self._shape[0]
            end = annotations.constrained(self.tool, start, (x, y)) \
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else (x, y)
            self._shape = (start, end)
        elif self._drag == "erase":
            self._erase_at(point)
        elif self._drag == "new":
            self._frame = annotations.crop_between(self._anchor, (x, y), self.visible())
            self.crop_changed.emit()
        elif self._frame_start is not None:
            delta = point - self._press
            self._frame = annotations.drag_crop(self._frame_start, self._drag, delta.x() / self._scale,
                                                delta.y() / self._scale, self.visible())
            self.crop_changed.emit()
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton) or self._drag is None:
            super().mouseReleaseEvent(event)
            return
        drag, self._drag = self._drag, None
        if drag == "draw":
            live, self._live = self._live, []
            if live:
                self.item_added.emit(annotations.stroke(self.tool, self.colour, self.width_for(self.tool), live))
        elif drag == "shape":
            shape, self._shape = self._shape, None
            if shape is not None:
                start, end = shape
                length = math.hypot(end[0] - start[0], end[1] - start[1]) * self._scale
                if length >= MIN_DRAG:
                    self.item_added.emit(annotations.shape(self.tool, self.colour, self.width_for(self.tool),
                                                           start, end))
        elif drag == "erase":
            erased, self._erasing = self._erasing, set()
            if erased:
                self.items_erased.emit(tuple(sorted(erased)))
        elif drag == "new" and self._frame is not None:
            least = min(annotations.MIN_CROP, self.visible().width, self.visible().height)
            if self._frame.width < least or self._frame.height < least:
                self._frame = self._frame_start
                self.crop_changed.emit()
        self._update_cursor()
        self.update()
        event.accept()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._hover = None
        if self.tool == "eraser":
            self._update_cursor()
            self.update()

    def wheelEvent(self, event) -> None:
        if not self.has_picture():
            event.ignore()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta:
                self.set_zoom(self.zoom() * WHEEL_STEP ** delta, event.position())
        else:
            pixels = event.pixelDelta()
            if not pixels.isNull():
                dx, dy = pixels.x(), pixels.y()
            else:
                dx, dy = event.angleDelta().x() / 120 * PAN_STEP, event.angleDelta().y() / 120 * PAN_STEP
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                dx, dy = dy, dx
            self.pan_by(dx, dy)
        event.accept()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        steps = {Qt.Key.Key_Left: (PAN_STEP, 0), Qt.Key.Key_Right: (-PAN_STEP, 0), Qt.Key.Key_Up: (0, PAN_STEP),
                 Qt.Key.Key_Down: (0, -PAN_STEP)}
        if key in steps and self.has_picture() and not event.modifiers():
            self.pan_by(*steps[key])
            event.accept()
            return
        if key == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space = True
            self._update_cursor()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space = False
            self._update_cursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self.has_picture():
            return
        if self._auto:
            self._refit()
        else:
            self._scale = max(self.min_scale(), self._scale)
            self._clamp()
        self._reposition_editor()
        self._update_cursor()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self._space = False

