"""The editor page: crop, adjust, filter, markup. Nothing is written until the user saves.

The page holds the untouched picture and an ``editops.EditState``. Every
control changes the state; a worker renders the state on a small copy of
the picture for the preview (the newest state wins, older requests are
skipped), and on the full picture when the user saves. Undo and redo walk
a list of states.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut, QTransform
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QComboBox, QGridLayout, QHBoxLayout, QScrollArea,
                               QSizePolicy, QSlider, QStackedWidget, QToolButton, QVBoxLayout, QWidget, QFrame)

import boswas_fluent as fluent
from boswas_fluent import paint

from .. import decode, editops, exif, imageio
from ..fileutil import free_name
from .common import BannerHolder, Surface, action_icon, checker_brush

PREVIEW_SIDE = 1280
THUMB_SIDE = 96
PANEL_WIDTH = 304
HANDLE = 14                    # reach of a crop handle, logical pixels
ASPECTS = (("free", "Free", None), ("original", "Original", 0.0), ("1:1", "Square (1:1)", 1.0),
           ("4:3", "4:3", 4 / 3), ("3:2", "3:2", 3 / 2), ("16:9", "16:9", 16 / 9))
PEN_COLOURS = (("coral.500", "Red"), ("sun.400", "Yellow"), ("mint.500", "Green"), ("azure.500", "Blue"),
               ("iris.500", "Violet"), ("white", "White"), ("black", "Black"))
ADJUST_GROUPS = (
    ("Light", (("brightness", "Brightness"), ("exposure", "Exposure"), ("contrast", "Contrast"),
               ("highlights", "Highlights"), ("shadows", "Shadows"), ("vignette", "Vignette"))),
    ("Colour", (("saturation", "Saturation"), ("warmth", "Warmth"), ("tint", "Tint"))),
    ("Detail", (("sharpness", "Sharpness"),)),
)


class EditCanvas(QWidget):
    """The preview of the edit, with the crop frame or the markup drawing on top."""

    crop_changed = Signal(object, bool)          # (x, y, w, h) in fractions; True when the drag ended
    stroke_drawn = Signal(object)                # a list of points in full-size source pixels
    erased = Signal(float, float, bool)          # a point in full-size source pixels; True when the drag ended
    PADDING = 24

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._alpha = False
        self._mode = "view"
        self._crop = (0.0, 0.0, 1.0, 1.0)
        self.aspect: float | None = None             # width / height of the crop in pixels, when locked
        self._strokes: tuple[editops.Stroke, ...] = ()
        self._to_image = QTransform()                # full-size source pixels -> pixels of the preview image
        self._source_scale = 1.0                     # length in the preview image / length in the full picture
        self._drag: str | None = None
        self._drag_start = QPointF()
        self._drag_crop = self._crop
        self._live: list[QPointF] = []               # the stroke being drawn, widget coordinates
        self.tool = editops.PEN
        self.pen_colour = fluent.theme().color("black")
        self.pen_width = 4.0                         # in full-size source pixels
        self.setMouseTracking(True)
        self.setMinimumSize(160, 120)
        self.setAccessibleName("Picture being edited")
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setProperty("fluentFocusRing", False)

    # --- content ---
    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._drag = None
        self._live = []
        self.setCursor(Qt.CursorShape.CrossCursor if mode == "markup" else Qt.CursorShape.ArrowCursor)
        self.update()

    def mode(self) -> str:
        return self._mode

    def set_preview(self, image: QImage, to_image: QTransform, source_scale: float) -> None:
        self._image = image
        self._alpha = image.hasAlphaChannel()
        self._to_image = QTransform(to_image)
        self._source_scale = source_scale
        self.update()

    def preview(self) -> QImage | None:
        return self._image

    def set_crop(self, crop: tuple[float, float, float, float] | None) -> None:
        self._crop = crop or (0.0, 0.0, 1.0, 1.0)
        self.update()

    def crop(self) -> tuple[float, float, float, float]:
        return self._crop

    def set_strokes(self, strokes) -> None:
        self._strokes = tuple(strokes)
        self.update()

    # --- geometry ---
    def image_rect(self) -> QRectF:
        """Where the preview lies in the widget."""
        if self._image is None or self._image.isNull():
            return QRectF()
        area = QRectF(self.rect()).adjusted(self.PADDING, self.PADDING, -self.PADDING, -self.PADDING)
        if area.width() <= 0 or area.height() <= 0:
            return QRectF()
        scale = min(area.width() / self._image.width(), area.height() / self._image.height())
        width, height = self._image.width() * scale, self._image.height() * scale
        return QRectF(area.center().x() - width / 2, area.center().y() - height / 2, width, height)

    def crop_rect(self) -> QRectF:
        box = self.image_rect()
        x, y, w, h = self._crop
        return QRectF(box.x() + x * box.width(), box.y() + y * box.height(), w * box.width(), h * box.height())

    def source_to_widget(self) -> QTransform:
        """Full-size source pixels -> widget coordinates."""
        box = self.image_rect()
        if box.isEmpty() or self._image is None:
            return QTransform()
        scale = box.width() / self._image.width()
        return self._to_image * QTransform.fromScale(scale, scale) * QTransform.fromTranslate(box.x(), box.y())

    def widget_scale(self) -> float:
        """Widget pixels per full-size source pixel."""
        box = self.image_rect()
        if box.isEmpty() or self._image is None:
            return 1.0
        return self._source_scale * box.width() / self._image.width()

    def _handle_at(self, point: QPointF) -> str | None:
        rect = self.crop_rect()
        if rect.isEmpty():
            return None
        near_left = abs(point.x() - rect.left()) <= HANDLE
        near_right = abs(point.x() - rect.right()) <= HANDLE
        near_top = abs(point.y() - rect.top()) <= HANDLE
        near_bottom = abs(point.y() - rect.bottom()) <= HANDLE
        inside_x = rect.left() - HANDLE <= point.x() <= rect.right() + HANDLE
        inside_y = rect.top() - HANDLE <= point.y() <= rect.bottom() + HANDLE
        if not (inside_x and inside_y):
            return None
        vertical = "t" if near_top else "b" if near_bottom else ""
        horizontal = "l" if near_left else "r" if near_right else ""
        if vertical or horizontal:
            return vertical + horizontal
        return "move" if rect.contains(point) else None

    _CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                "move": Qt.CursorShape.SizeAllCursor}

    def drag_crop(self, handle: str, dx: float, dy: float, start: tuple[float, float, float, float] | None = None,
                  ) -> tuple[float, float, float, float]:
        """The crop after its ``handle`` ("tl", "r", "move", ...) was dragged by (dx, dy) fractions of the
        picture from ``start``. Honours the locked aspect and the edges of the picture."""
        x, y, w, h = start or self._crop
        least = editops.MIN_CROP
        if handle == "move":
            return (max(0.0, min(1.0 - w, x + dx)), max(0.0, min(1.0 - h, y + dy)), w, h)
        left, top, right, bottom = x, y, x + w, y + h
        if "l" in handle:
            left = max(0.0, min(right - least, left + dx))
        if "r" in handle:
            right = min(1.0, max(left + least, right + dx))
        if "t" in handle:
            top = max(0.0, min(bottom - least, top + dy))
        if "b" in handle:
            bottom = min(1.0, max(top + least, bottom + dy))
        if self.aspect and self._image is not None:
            # the aspect is in pixels: turn it into a ratio of fractions
            ratio = self.aspect * self._image.height() / self._image.width()
            width, height = right - left, bottom - top
            if handle in ("l", "r"):
                height = width / ratio
            elif handle in ("t", "b"):
                width = height * ratio
            elif width / max(height, 1e-9) > ratio:
                width = height * ratio
            else:
                height = width / ratio
            centre_x, centre_y = (x + x + w) / 2, (y + y + h) / 2
            if handle in ("t", "b"):
                left = max(0.0, min(1.0 - width, centre_x - width / 2))
                right = left + width
            elif "l" in handle:
                left = right - width
            else:
                right = left + width
            if handle in ("l", "r"):
                top = max(0.0, min(1.0 - height, centre_y - height / 2))
                bottom = top + height
            elif "t" in handle:
                top = bottom - height
            else:
                bottom = top + height
            # shrink evenly when the locked shape pushed a side out of the picture
            over = max(0.0 - left, right - 1.0, 0.0 - top, bottom - 1.0, 0.0)
            if over > 0:
                shrink = max(0.0, 1 - over / max(right - left, bottom - top, 1e-9) * 2)
                mid_x, mid_y = (left + right) / 2, (top + bottom) / 2
                half_w, half_h = (right - left) / 2 * shrink, (bottom - top) / 2 * shrink
                left, right, top, bottom = mid_x - half_w, mid_x + half_w, mid_y - half_h, mid_y + half_h
                shift_x = max(0.0, -left) - max(0.0, right - 1.0)
                shift_y = max(0.0, -top) - max(0.0, bottom - 1.0)
                left, right, top, bottom = left + shift_x, right + shift_x, top + shift_y, bottom + shift_y
        return (left, top, max(least, right - left), max(least, bottom - top))

    # --- mouse ---
    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._image is None:
            return
        point = event.position()
        if self._mode == "crop":
            self._drag = self._handle_at(point)
            self._drag_start = point
            self._drag_crop = self._crop
        elif self._mode == "markup" and self.image_rect().contains(point):
            if self.tool == "eraser":
                self._drag = "erase"
                self._erase_at(point, False)
            else:
                self._drag = "draw"
                self._live = [point]
            self.update()

    def mouseMoveEvent(self, event) -> None:
        point = event.position()
        if self._mode == "crop":
            if self._drag is None:
                handle = self._handle_at(point)
                self.setCursor(self._CURSORS.get(handle, Qt.CursorShape.ArrowCursor))
                return
            box = self.image_rect()
            if box.isEmpty():
                return
            delta = point - self._drag_start
            self._crop = self.drag_crop(self._drag, delta.x() / box.width(), delta.y() / box.height(),
                                        self._drag_crop)
            self.crop_changed.emit(self._crop, False)
            self.update()
        elif self._drag == "draw":
            if (point - self._live[-1]).manhattanLength() >= 2:
                self._live.append(point)
                self.update()
        elif self._drag == "erase":
            self._erase_at(point, False)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        drag, self._drag = self._drag, None
        if drag is None:
            return
        if self._mode == "crop":
            self.crop_changed.emit(self._crop, True)
        elif drag == "draw":
            inverse, ok = self.source_to_widget().inverted()
            points = [inverse.map(point) for point in self._live] if ok else []
            self._live = []
            if points:
                self.stroke_drawn.emit([(point.x(), point.y()) for point in points])
            self.update()
        elif drag == "erase":
            self._erase_at(event.position(), True)

    def _erase_at(self, point: QPointF, final: bool) -> None:
        inverse, ok = self.source_to_widget().inverted()
        if ok:
            mapped = inverse.map(point)
            self.erased.emit(mapped.x(), mapped.y(), final)

    # --- painting ---
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        box = self.image_rect()
        if box.isEmpty():
            return
        if self._alpha:
            painter.setBrushOrigin(box.topLeft())
            painter.fillRect(box, checker_brush(self.devicePixelRatioF()))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(box, self._image)
        painter.save()
        painter.setClipRect(box)
        editops.draw_strokes(painter, self._strokes, self.source_to_widget(), self.widget_scale())
        if self._live:
            stroke = editops.Stroke(self.tool, self.pen_colour, self.pen_width, ())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(editops.stroke_pen(stroke, self.widget_scale()))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(editops.stroke_path([(p.x(), p.y()) for p in self._live]))
        painter.restore()
        if self._mode == "crop":
            self._paint_crop(painter, box)

    def _paint_crop(self, painter: QPainter, box: QRectF) -> None:
        rect = self.crop_rect()
        outside = QPainterPath()
        outside.addRect(box)
        inner = QPainterPath()
        inner.addRect(rect)
        shade = fluent.qcolor("black")
        shade.setAlphaF(0.55)
        painter.fillPath(outside.subtracted(inner), shade)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        dark, light = fluent.qcolor("black"), fluent.qcolor("white")
        thirds = QColor(light)
        thirds.setAlphaF(0.5)
        painter.setPen(QPen(thirds, 1))
        for step in (1, 2):
            x = rect.left() + rect.width() * step / 3
            y = rect.top() + rect.height() * step / 3
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        painter.setPen(QPen(dark, 3))
        painter.drawRect(rect)
        painter.setPen(QPen(light, 1))
        painter.drawRect(rect)
        # corner and edge handles
        length = min(20.0, rect.width() / 3, rect.height() / 3)
        for pen in (QPen(dark, 6), QPen(light, 4)):
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            for x, sx in ((rect.left(), 1), (rect.right(), -1)):
                for y, sy in ((rect.top(), 1), (rect.bottom(), -1)):
                    painter.drawLine(QPointF(x, y), QPointF(x + sx * length, y))
                    painter.drawLine(QPointF(x, y), QPointF(x, y + sy * length))
            middle_x, middle_y = rect.center().x(), rect.center().y()
            painter.drawLine(QPointF(middle_x - length / 2, rect.top()), QPointF(middle_x + length / 2, rect.top()))
            painter.drawLine(QPointF(middle_x - length / 2, rect.bottom()),
                             QPointF(middle_x + length / 2, rect.bottom()))
            painter.drawLine(QPointF(rect.left(), middle_y - length / 2), QPointF(rect.left(), middle_y + length / 2))
            painter.drawLine(QPointF(rect.right(), middle_y - length / 2),
                             QPointF(rect.right(), middle_y + length / 2))


class AdjustSlider(QWidget):
    """One adjustment: its name, its value and a slider."""

    moved = Signal(str, float)        # while the slider moves
    released = Signal(str)            # the change is complete

    def __init__(self, name: str, label: str, parent: QWidget | None = None, *, minimum: int = -100,
                 maximum: int = 100, divisor: float = 100.0, suffix: str = "") -> None:
        super().__init__(parent)
        self.name = name
        self._divisor = divisor
        self._suffix = suffix
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        head = QHBoxLayout()
        self.label = fluent.Label(label, "body", self)
        self.value_label = fluent.Label("0", "caption", self, tone="secondary")
        head.addWidget(self.label, 1)
        head.addWidget(self.value_label)
        layout.addLayout(head)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(minimum, maximum)
        self.slider.setPageStep(10)
        self.slider.setAccessibleName(label)
        layout.addWidget(self.slider)
        self.slider.valueChanged.connect(self._changed)
        self.slider.sliderReleased.connect(lambda: self.released.emit(self.name))

    def value(self) -> float:
        return self.slider.value() / self._divisor

    def set_value(self, value: float) -> None:
        blocked = self.slider.blockSignals(True)
        self.slider.setValue(round(value * self._divisor))
        self.slider.blockSignals(blocked)
        self._show()

    def _show(self) -> None:
        value = self.slider.value()
        if self._suffix:
            text = f"{value / self._divisor:.1f}{self._suffix}"
        elif self.slider.minimum() < 0:
            text = f"{value:+d}" if value else "0"
        else:
            text = str(value)
        self.value_label.setText(text)

    def _changed(self, _value: int) -> None:
        self._show()
        self.moved.emit(self.name, self.value())
        if not self.slider.isSliderDown():
            self.released.emit(self.name)


class ColourSwatch(QAbstractButton):
    """A round colour to pick for the pen."""

    def __init__(self, colour: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.colour = colour
        self.setCheckable(True)
        self.setFixedSize(32, 32)
        self.setToolTip(name)
        self.setAccessibleName(name)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = QPointF(self.width() / 2, self.height() / 2)
        if self.isChecked():
            painter.setPen(QPen(fluent.qcolor("accent"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, 14, 14)
        painter.setPen(QPen(fluent.qcolor("stroke-strong"), 1))
        painter.setBrush(QColor(self.colour))
        painter.drawEllipse(centre, 10, 10)


class FilterButton(QAbstractButton):
    """A look of the Filter tab: a small preview and its name."""

    SIZE = QSize(88, 92)

    def __init__(self, key: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.setText(text)
        self.setCheckable(True)
        self.setFixedSize(self.SIZE)
        self.setAccessibleName(text)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._picture = QPixmap()

    def set_picture(self, image: QImage) -> None:
        self._picture = QPixmap.fromImage(image)
        self.update()

    def paintEvent(self, _event) -> None:
        from .thumbview import draw_cover
        painter = QPainter(self)
        radius = fluent.theme().radius("control")
        box = QRectF(4, 4, self.width() - 8, 62)
        if self._picture.isNull():
            paint.rounded_rect(painter, box, radius, "fill-control")
        else:
            draw_cover(painter, box, self._picture, radius)
        if self.isChecked():
            paint.rounded_rect(painter, box.adjusted(-3, -3, 3, 3), radius + 3, None, "accent", 2)
        elif self.underMouse():
            paint.rounded_rect(painter, box, radius, None, "stroke-strong")
        paint.elided(painter, QRectF(0, 70, self.width(), 20), self.text(),
                     fluent.font("body-strong" if self.isChecked() else "caption"), "text",
                     Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)


def _preview_job(image: QImage) -> QImage:
    size = decode.fit_size(image.width(), image.height(), PREVIEW_SIDE)
    if size == image.size():
        return image
    return image.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


def _filter_thumbnails(source: QImage, state: editops.EditState) -> dict[str, QImage]:
    base = editops.render_geometry(source, state)
    size = decode.fit_size(base.width(), base.height(), THUMB_SIDE)
    base = base.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    thumbs = {"": base}
    for name, look in editops.FILTERS.items():
        thumbs[name] = editops.apply_adjustments(base, look)
    return thumbs


def _save_job(image: QImage, state: editops.EditState, target: str, fmt: str, quality: int,
              block: bytes | None, had_exif: bool) -> imageio.SaveReport:
    return imageio.save(editops.render(image, state, 1.0), target, fmt, quality, exif_block=block, had_exif=had_exif)


class EditorPage(QWidget):
    """Edits one picture. ``saved(report)`` after a file was written, ``closed`` when the user leaves."""

    saved = Signal(object)
    closed = Signal()

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.host = host
        self.worker = fluent.Worker(self, max_threads=1)          # previews, newest state only
        self.file_worker = fluent.Worker(self, max_threads=1)
        self._loaded: decode.Loaded | None = None
        self._had_exif = False
        self._source: QImage | None = None           # the small copy the preview is rendered from
        self._scale = 1.0
        self._state = editops.EditState()
        self._history: list[editops.EditState] = [self._state]
        self._position = 0
        self._rendering = False
        self._stale = False
        self._thumbs_for: tuple | None = None
        self._saving = False
        self._eraser_changed = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # --- top bar ---
        self.top_bar = Surface(self, fill=None, line="stroke-divider")
        self.top_bar.setFixedHeight(52)
        bar = QHBoxLayout(self.top_bar)
        bar.setContentsMargins(12, 8, 12, 8)
        bar.setSpacing(8)
        self.cancel_button = fluent.Button("Cancel", self.top_bar)
        self.cancel_button.setToolTip("Leave without saving (Esc)")
        self.cancel_button.clicked.connect(self.cancel)
        bar.addWidget(self.cancel_button)
        self.tabs = fluent.Pivot(self.top_bar)
        for key, text in (("crop", "Crop"), ("adjust", "Adjust"), ("filter", "Filter"), ("markup", "Markup")):
            self.tabs.add_item(key, text)
        self.tabs.setAccessibleName("Editing tools")
        bar.addStretch(1)
        bar.addWidget(self.tabs)
        bar.addStretch(1)
        self.undo_button = fluent.IconButton(action_icon("edit-undo"), "Undo (Ctrl+Z)", self.top_bar)
        self.redo_button = fluent.IconButton(action_icon("edit-redo"), "Redo (Ctrl+Y)", self.top_bar)
        self.reset_button = fluent.Button("Reset", self.top_bar, subtle=True)
        self.reset_button.setToolTip("Undo every change")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.reset_button.clicked.connect(self.reset)
        bar.addWidget(self.undo_button)
        bar.addWidget(self.redo_button)
        bar.addWidget(self.reset_button)
        self.save_copy_button = fluent.Button("Save as copy", self.top_bar, accent=True)
        self.save_copy_button.setToolTip("Save the edited picture as a new file (Ctrl+S)")
        self.save_copy_button.clicked.connect(self.save_copy)
        self.save_button = fluent.Button("Save", self.top_bar)
        self.save_button.setToolTip("Replace the original file")
        self.save_button.clicked.connect(self.save_over)
        bar.addWidget(self.save_button)
        bar.addWidget(self.save_copy_button)
        outer.addWidget(self.top_bar)
        self.banner = fluent.InfoBar("info", parent=self)
        self.banner.hide()
        outer.addWidget(BannerHolder(self.banner, self, margins=(12, 4, 12, 0)))

        # --- body ---
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.canvas = EditCanvas(self)
        self.canvas.crop_changed.connect(self._crop_dragged)
        self.canvas.stroke_drawn.connect(self._stroke_drawn)
        self.canvas.erased.connect(self._erase)
        self.spinner = fluent.ProgressRing(self.canvas, size=32)
        self.spinner.hide()
        body.addWidget(self.canvas, 1)
        self.panel = Surface(self, fill="layer")
        self.panel.setFixedWidth(PANEL_WIDTH)
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        self.panels = QStackedWidget(self.panel)
        panel_layout.addWidget(self.panels)
        body.addWidget(self.panel)
        outer.addLayout(body, 1)
        self._pages: dict[str, QWidget] = {}
        for key, builder in (("crop", self._crop_panel), ("adjust", self._adjust_panel),
                             ("filter", self._filter_panel), ("markup", self._markup_panel)):
            page = self._scrolling(builder())
            self._pages[key] = page
            self.panels.addWidget(page)
        self.tabs.current_changed.connect(self.set_mode)

        for keys, slot in (("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo), ("Ctrl+Shift+Z", self.redo),
                           ("Ctrl+S", self.save_copy), ("Escape", self.cancel)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
        self.set_mode("crop")

    # --- building the panels ---
    def _scrolling(self, content: QWidget) -> QScrollArea:
        area = QScrollArea(self.panel)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        area.setWidget(content)
        area.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        return area

    @staticmethod
    def _panel() -> tuple[QWidget, QVBoxLayout]:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(8)
        return widget, layout

    def _crop_panel(self) -> QWidget:
        widget, layout = self._panel()
        layout.addWidget(fluent.Label("Shape", "body-strong", widget))
        self.aspect_box = QComboBox(widget)
        self.aspect_box.setAccessibleName("Shape of the crop")
        for key, text, _ratio in ASPECTS:
            self.aspect_box.addItem(text, key)
        self.aspect_box.currentIndexChanged.connect(self._aspect_chosen)
        layout.addWidget(self.aspect_box)
        self.flip_aspect_button = fluent.Button("Swap width and height", widget)
        self.flip_aspect_button.setCheckable(True)
        self.flip_aspect_button.setToolTip("Turn the shape on its side: 4:3 becomes 3:4")
        self.flip_aspect_button.setEnabled(False)
        self.flip_aspect_button.toggled.connect(lambda _checked: self._aspect_chosen())
        layout.addWidget(self.flip_aspect_button)
        layout.addSpacing(12)
        layout.addWidget(fluent.Label("Straighten", "body-strong", widget))
        self.straighten = AdjustSlider("straighten", "Angle", widget, minimum=-450, maximum=450, divisor=10.0,
                                       suffix="°")
        self.straighten.moved.connect(lambda _name, value: self._apply(self._state.with_straighten(value), False))
        self.straighten.released.connect(lambda _name: self._commit())
        layout.addWidget(self.straighten)
        layout.addSpacing(12)
        layout.addWidget(fluent.Label("Turn and mirror", "body-strong", widget))
        grid = QGridLayout()
        grid.setSpacing(8)
        self.rotate_left_button = fluent.Button("Left", widget, icon=action_icon("object-rotate-left"))
        self.rotate_right_button = fluent.Button("Right", widget, icon=action_icon("object-rotate-right"))
        self.flip_h_button = fluent.Button("Mirror", widget, icon=action_icon("object-flip-horizontal"))
        self.flip_v_button = fluent.Button("Flip", widget, icon=action_icon("object-flip-vertical"))
        self.rotate_left_button.setToolTip("Turn a quarter to the left")
        self.rotate_right_button.setToolTip("Turn a quarter to the right")
        self.flip_h_button.setToolTip("Mirror left to right")
        self.flip_v_button.setToolTip("Flip top to bottom")
        self.rotate_left_button.clicked.connect(lambda: self._turn(self._state.rotated(False)))
        self.rotate_right_button.clicked.connect(lambda: self._turn(self._state.rotated(True)))
        self.flip_h_button.clicked.connect(lambda: self._turn(self._state.flipped(True)))
        self.flip_v_button.clicked.connect(lambda: self._turn(self._state.flipped(False)))
        grid.addWidget(self.rotate_left_button, 0, 0)
        grid.addWidget(self.rotate_right_button, 0, 1)
        grid.addWidget(self.flip_h_button, 1, 0)
        grid.addWidget(self.flip_v_button, 1, 1)
        layout.addLayout(grid)
        layout.addSpacing(12)
        self.size_label = fluent.Label("", "caption", widget, tone="secondary", wrap=True)
        layout.addWidget(self.size_label)
        self.reset_crop_button = fluent.Button("Reset crop", widget, subtle=True)
        self.reset_crop_button.clicked.connect(self._reset_crop)
        layout.addWidget(self.reset_crop_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return widget

    def _adjust_panel(self) -> QWidget:
        widget, layout = self._panel()
        self.sliders: dict[str, AdjustSlider] = {}
        for title, entries in ADJUST_GROUPS:
            layout.addWidget(fluent.Label(title, "body-strong", widget))
            for name, label in entries:
                slider = AdjustSlider(name, label, widget, minimum=0 if name == "sharpness" else -100)
                slider.moved.connect(lambda n, value: self._apply(self._state.with_adjust(**{n: value}), False))
                slider.released.connect(lambda _n: self._commit())
                self.sliders[name] = slider
                layout.addWidget(slider)
            layout.addSpacing(8)
        self.reset_adjust_button = fluent.Button("Reset adjustments", widget, subtle=True)
        self.reset_adjust_button.clicked.connect(
            lambda: self._apply(self._state.with_adjust(**{name: 0.0 for name in editops.ADJUSTMENT_NAMES})))
        layout.addWidget(self.reset_adjust_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return widget

    def _filter_panel(self) -> QWidget:
        widget, layout = self._panel()
        layout.addWidget(fluent.Label("Looks", "body-strong", widget))
        grid = QGridLayout()
        grid.setSpacing(0)
        self.filter_group = QButtonGroup(widget)
        self.filter_group.setExclusive(True)
        self.filter_buttons: dict[str, FilterButton] = {}
        for index, (key, text) in enumerate([("", "Original")] + [(name, name) for name in editops.FILTERS]):
            button = FilterButton(key, text, widget)
            self.filter_group.addButton(button)
            self.filter_buttons[key] = button
            button.clicked.connect(lambda _checked=False, k=key: self._filter_chosen(k))
            grid.addWidget(button, index // 3, index % 3)
        layout.addLayout(grid)
        layout.addSpacing(8)
        self.filter_strength = AdjustSlider("strength", "Strength", widget, minimum=0, maximum=100)
        self.filter_strength.moved.connect(
            lambda _n, value: self._apply(self._state.with_filter(self._state.filter, value), False))
        self.filter_strength.released.connect(lambda _n: self._commit())
        layout.addWidget(self.filter_strength)
        layout.addStretch(1)
        return widget

    def _markup_panel(self) -> QWidget:
        widget, layout = self._panel()
        layout.addWidget(fluent.Label("Tool", "body-strong", widget))
        self.tool_box = fluent.SegmentedControl(widget)
        for key, text in ((editops.PEN, "Pen"), (editops.HIGHLIGHTER, "Highlighter"), ("eraser", "Eraser")):
            self.tool_box.add_item(key, text)
        self.tool_box.setAccessibleName("Drawing tool")
        self.tool_box.current_changed.connect(self._tool_chosen)
        layout.addWidget(self.tool_box)
        layout.addSpacing(12)
        self.colour_title = fluent.Label("Colour", "body-strong", widget)
        layout.addWidget(self.colour_title)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.colour_group = QButtonGroup(widget)
        self.colour_group.setExclusive(True)
        self.swatches: list[ColourSwatch] = []
        theme = fluent.theme()
        for reference, name in PEN_COLOURS:
            swatch = ColourSwatch(theme.color(reference), name, widget)
            self.colour_group.addButton(swatch)
            swatch.clicked.connect(lambda _checked=False, s=swatch: self._colour_chosen(s.colour))
            self.swatches.append(swatch)
            row.addWidget(swatch)
        row.addStretch(1)
        self.colour_holder = QWidget(widget)
        self.colour_holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.colour_holder)
        layout.addSpacing(12)
        self.width_slider = AdjustSlider("width", "Thickness", widget, minimum=1, maximum=40, divisor=1.0)
        self.width_slider.set_value(6)
        self.width_slider.moved.connect(lambda _n, _value: self._sync_pen())
        layout.addWidget(self.width_slider)
        layout.addSpacing(12)
        self.clear_markup_button = fluent.Button("Erase all drawing", widget, subtle=True)
        self.clear_markup_button.clicked.connect(
            lambda: self._apply(self._state.without_strokes(range(len(self._state.strokes)))))
        layout.addWidget(self.clear_markup_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.swatches[0].setChecked(True)
        return widget

    # --- starting and leaving ---
    def start(self, loaded: decode.Loaded, had_exif: bool = False) -> None:
        """Begin editing ``loaded`` (the picture stays untouched)."""
        self._loaded = loaded
        self._had_exif = had_exif
        self._source = None
        self._state = editops.EditState()
        self._history = [self._state]
        self._position = 0
        self._thumbs_for = None
        self._saving = False
        self.banner.hide()
        notes = []
        if loaded.info.animated:
            notes.append("Only the first picture of this animation is edited.")
        if loaded.info.vector:
            notes.append("This drawing is edited as a picture of "
                         f"{loaded.image.width()} × {loaded.image.height()} pixels.")
        if loaded.reduced:
            notes.append(f"This picture is edited and saved at {loaded.image.width()} × {loaded.image.height()} "
                         "pixels.")
        if notes:
            self.banner.show_message("info", " ".join(notes))
        self.canvas.set_strokes(())
        self.canvas.set_crop(None)
        self.spinner.show()
        self._place_spinner()
        overwrite = (imageio.format_for_path(loaded.info.path) is not None and not loaded.info.animated
                     and not loaded.info.vector and loaded.info.frames <= 1)
        self.save_button.setVisible(overwrite)
        self.tabs.set_current("crop")
        self.set_mode("crop")
        self._sync_controls()
        self._sync_pen()
        self.worker.submit(_preview_job, loaded.image, on_done=self._source_ready)

    def _source_ready(self, image: QImage) -> None:
        if self._loaded is None:
            return
        self._source = image
        self._scale = image.width() / max(1, self._loaded.image.width())
        self.spinner.hide()
        self._render()

    def is_dirty(self) -> bool:
        return not self._state.is_identity()

    def state(self) -> editops.EditState:
        return self._state

    def cancel(self) -> None:
        if self._saving:
            return
        if self.is_dirty() and not self.host.ask("Discard your changes?",
                                                 "The picture has changes that were not saved.",
                                                 "Discard", "Keep editing"):
            return
        self.finish()

    def finish(self) -> None:
        self._loaded = None
        self._source = None
        self.closed.emit()

    # --- state ---
    def _apply(self, state: editops.EditState, commit: bool = True) -> None:
        if self._loaded is None:
            return
        self._state = state
        if commit:
            self._commit()
        self._sync_controls(sliders=commit)
        self._render()

    def _commit(self) -> None:
        if self._history[self._position] == self._state:
            return
        del self._history[self._position + 1:]
        self._history.append(self._state)
        del self._history[:-200]
        self._position = len(self._history) - 1
        self._sync_controls()

    def can_undo(self) -> bool:
        return self._position > 0

    def can_redo(self) -> bool:
        return self._position < len(self._history) - 1

    def undo(self) -> None:
        if self.can_undo() and not self._saving:
            self._position -= 1
            self._state = self._history[self._position]
            self._sync_controls()
            self._render()

    def redo(self) -> None:
        if self.can_redo() and not self._saving:
            self._position += 1
            self._state = self._history[self._position]
            self._sync_controls()
            self._render()

    def reset(self) -> None:
        if not self._saving:
            self._apply(editops.EditState())

    def set_mode(self, mode: str) -> None:
        if self.tabs.current() != mode:
            self.tabs.set_current(mode)
        self.panels.setCurrentWidget(self._pages[mode])
        self.canvas.set_mode(mode if mode in ("crop", "markup") else "view")
        if self._loaded is not None:
            self._render()
            if mode == "filter":
                self._refresh_filter_thumbnails()

    def mode(self) -> str:
        return self.tabs.current() or "crop"

    def _sync_controls(self, sliders: bool = True) -> None:
        state = self._state
        self.undo_button.setEnabled(self.can_undo())
        self.redo_button.setEnabled(self.can_redo())
        self.reset_button.setEnabled(not state.is_identity())
        if sliders:
            for name, slider in self.sliders.items():
                slider.set_value(getattr(state.adjust, name))
            self.straighten.set_value(state.straighten)
            self.filter_strength.set_value(state.filter_strength)
        self.filter_strength.setEnabled(bool(state.filter))
        button = self.filter_buttons.get(state.filter)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self.canvas.set_crop(state.crop)
        self.canvas.set_strokes(state.strokes)
        self.clear_markup_button.setEnabled(bool(state.strokes))
        self.reset_crop_button.setEnabled(state.has_geometry())
        self.reset_adjust_button.setEnabled(not state.adjust.is_identity())
        if self._loaded is not None:
            size = editops.geometry(self._loaded.image.size(), state).size
            self.size_label.setText(f"The picture will be {size.width()} × {size.height()} pixels.")

    # --- preview ---
    def _render(self) -> None:
        if self._source is None or self._loaded is None:
            return
        if self._rendering:
            self._stale = True
            return
        self._rendering = True
        self._stale = False
        state, source, scale = self._state, self._source, self._scale
        crop = self.mode() != "crop"
        self.worker.submit(editops.render, source, state, scale, crop=crop, markup=False,
                           on_done=lambda image, s=state, c=crop: self._rendered(image, s, c),
                           on_error=lambda _exc: self._render_failed())

    def _rendered(self, image: QImage, state: editops.EditState, crop: bool) -> None:
        self._rendering = False
        if self._source is None or self._loaded is None:
            return
        placed = editops.geometry(self._source.size(), state)
        to_image = QTransform.fromScale(self._scale, self._scale) * (placed.to_output if crop else placed.to_canvas)
        self.canvas.set_preview(image, to_image, self._scale)
        if self._stale:
            self._render()
        elif self.mode() == "filter":
            self._refresh_filter_thumbnails()

    def _render_failed(self) -> None:
        self._rendering = False
        if self._stale:
            self._render()

    def preview_idle(self) -> bool:
        """Whether the preview on screen shows the current state (tests)."""
        return not self._rendering and not self._stale and self.worker.pending() == 0

    def _refresh_filter_thumbnails(self) -> None:
        if self._source is None:
            return
        state = self._state
        key = (state.orientation, state.straighten, state.crop, id(self._source))
        if key == self._thumbs_for:
            return
        self._thumbs_for = key
        self.worker.submit(_filter_thumbnails, self._source, state, on_done=self._filter_thumbnails_ready)

    def _filter_thumbnails_ready(self, thumbs: dict[str, QImage]) -> None:
        for key, image in thumbs.items():
            button = self.filter_buttons.get(key)
            if button is not None:
                button.set_picture(image)

    def _place_spinner(self) -> None:
        self.spinner.move((self.canvas.width() - self.spinner.width()) // 2,
                          (self.canvas.height() - self.spinner.height()) // 2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_spinner()

    # --- crop ---
    def _canvas_size(self) -> QSize:
        return editops.geometry(self._loaded.image.size(), self._state).canvas

    def _aspect_ratio(self) -> float | None:
        key = self.aspect_box.currentData()
        ratio = next(r for k, _t, r in ASPECTS if k == key)
        if ratio is None or self._loaded is None:
            return None
        if ratio == 0.0:
            canvas = self._canvas_size()
            ratio = canvas.width() / max(1, canvas.height())
        if self.flip_aspect_button.isChecked():
            ratio = 1 / ratio
        return ratio

    def _aspect_chosen(self, *_args) -> None:
        key = self.aspect_box.currentData()
        self.flip_aspect_button.setEnabled(key not in ("free", "1:1"))
        ratio = self._aspect_ratio()
        self.canvas.aspect = ratio
        if ratio is None or self._loaded is None:
            return
        canvas = self._canvas_size()
        self._apply(self._state.with_crop(editops.aspect_crop(canvas.width(), canvas.height(), ratio,
                                                              self._state.crop)))

    def _crop_dragged(self, crop, final: bool) -> None:
        self._apply(self._state.with_crop(tuple(crop)), final)

    def _turn(self, state: editops.EditState) -> None:
        # a locked shape would no longer match the turned crop: back to a free crop
        blocked = self.aspect_box.blockSignals(True)
        self.aspect_box.setCurrentIndex(0)
        self.aspect_box.blockSignals(blocked)
        self.flip_aspect_button.setEnabled(False)
        self.canvas.aspect = None
        self._apply(state)

    def _reset_crop(self) -> None:
        blocked = self.aspect_box.blockSignals(True)
        self.aspect_box.setCurrentIndex(0)
        self.aspect_box.blockSignals(blocked)
        self.canvas.aspect = None
        from dataclasses import replace
        self._apply(replace(self._state, orientation=1, straighten=0.0, crop=None))

    # --- filter ---
    def _filter_chosen(self, key: str) -> None:
        strength = self._state.filter_strength if self._state.filter else 1.0
        self._apply(self._state.with_filter(key, strength if key else 1.0))

    # --- markup ---
    def _stroke_width(self) -> float:
        """The thickness chosen, in pixels of the full picture (it grows with the picture)."""
        if self._loaded is None:
            return 4.0
        size = self._loaded.image.size()
        return self.width_slider.value() * max(size.width(), size.height()) / 800.0

    def _sync_pen(self) -> None:
        tool = self.tool_box.current() or editops.PEN
        self.canvas.tool = tool
        checked = self.colour_group.checkedButton()
        self.canvas.pen_colour = checked.colour if isinstance(checked, ColourSwatch) else fluent.theme().color("black")
        width = self._stroke_width()
        self.canvas.pen_width = width * 3 if tool == editops.HIGHLIGHTER else width
        drawing = tool != "eraser"
        self.colour_title.setEnabled(drawing)
        self.colour_holder.setEnabled(drawing)

    def _tool_chosen(self, _key: str) -> None:
        self._sync_pen()

    def _colour_chosen(self, _colour: str) -> None:
        self._sync_pen()

    def _stroke_drawn(self, points) -> None:
        self._sync_pen()
        stroke = editops.Stroke(self.canvas.tool, self.canvas.pen_colour, self.canvas.pen_width,
                                tuple((float(x), float(y)) for x, y in points))
        self._apply(self._state.with_stroke(stroke))

    def _erase(self, x: float, y: float, final: bool) -> None:
        radius = self._stroke_width()
        hit = [index for index, stroke in enumerate(self._state.strokes) if editops.stroke_hit(stroke, x, y, radius)]
        if hit:
            self._eraser_changed = True
            self._apply(self._state.without_strokes(hit), False)
        if final and self._eraser_changed:
            self._eraser_changed = False
            self._commit()

    # --- saving ---
    def _suggested_copy(self) -> tuple[str, str]:
        path = self._loaded.info.path
        fmt = imageio.format_for_path(path) or "png"
        stem = os.path.splitext(os.path.basename(path))[0]
        return free_name(os.path.join(os.path.dirname(path), f"{stem} (edited).{imageio.WRITERS[fmt][1]}")), fmt

    def save_copy(self) -> None:
        if self._loaded is None or self._saving:
            return
        suggested, fmt = self._suggested_copy()
        target = self.host.choose_save_path("Save as copy", suggested, fmt)
        if not target:
            return
        chosen = imageio.format_for_path(target)
        if chosen is None:
            self.banner.show_message("error", "Give the file a name that ends in .jpg, .png, .webp, .bmp or .tif.")
            return
        self._save(target, chosen)

    def save_over(self) -> None:
        if self._loaded is None or self._saving or not self.save_button.isVisible():
            return
        path = self._loaded.info.path
        fmt = imageio.format_for_path(path)
        if fmt is None:
            return
        if not self.host.ask("Replace the original?",
                             f"“{os.path.basename(path)}” will be overwritten with the edited picture. "
                             "This cannot be undone.", "Replace", "Cancel"):
            return
        self._save(path, fmt)

    def _save(self, target: str, fmt: str) -> None:
        loaded = self._loaded
        self._saving = True
        self.spinner.show()
        self._place_spinner()
        for widget in (self.save_button, self.save_copy_button, self.cancel_button, self.panel, self.tabs):
            widget.setEnabled(False)
        block = imageio.read_exif_block(loaded.info.path) if loaded.info.format == "jpeg" else None
        self.file_worker.submit(_save_job, loaded.image, self._state, target, fmt, self.host.settings.jpeg_quality,
                                block, self._had_exif, on_done=self._save_done, on_error=self._save_failed)

    def _saving_over(self) -> None:
        self._saving = False
        self.spinner.hide()
        for widget in (self.save_button, self.save_copy_button, self.cancel_button, self.panel, self.tabs):
            widget.setEnabled(True)

    def _save_done(self, report: imageio.SaveReport) -> None:
        self._saving_over()
        self.saved.emit(report)

    def _save_failed(self, error: Exception) -> None:
        self._saving_over()
        text = str(error) if isinstance(error, imageio.SaveError) else "The picture could not be saved."
        self.banner.show_message("error", text)

    def shutdown(self) -> None:
        self.worker.shutdown()
        self.file_worker.shutdown()
