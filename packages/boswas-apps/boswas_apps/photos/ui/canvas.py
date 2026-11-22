"""ImageCanvas: one picture with zoom, pan and pinch.

The canvas knows two sizes: the *natural* size of the picture (its real
pixel count) and the size of the image it holds, which may be smaller (a
preview shown while the picture is still being decoded, or the reduced
copy of a very large picture). Zoom is measured against the natural size:
100 % is one picture pixel on one screen pixel.

Painting has two speeds:

* while the user zooms or drags, the image is drawn with nearest-pixel
  scaling, which costs almost nothing;
* a moment after the view comes to rest, a properly filtered version of
  the visible part is made on a worker thread (area-averaged when the
  picture is shown smaller than it is, rendered from the vectors for an
  SVG) and replaces the fast one.

Zooming keeps the point under the pointer (or between the fingers) where
it is. In the fitted state the picture follows the size of the widget.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QMovie, QPainter
from PySide6.QtWidgets import QApplication, QPinchGesture, QWidget

import boswas_fluent as fluent

from .. import decode
from .common import checker_brush

MAX_ZOOM = 64.0
SETTLE_MS = 140
WHEEL_STEP = 1.0015            # zoom factor per unit of wheel angle: one notch (120) is about 20 %
BUTTON_STEP = 1.25
NEAREST_ABOVE = 4.0            # beyond this magnification single pixels are shown as squares
MOVIE_PIXELS = 16_000_000      # animations with larger frames are shown as a still picture


class ImageCanvas(QWidget):
    """Shows one picture; see the module text."""

    zoom_changed = Signal()
    step_requested = Signal(int)          # the wheel or a mouse button asks for the next (+1) or previous (-1)
    drag_started = Signal()               # the user drags the picture out of the window

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._natural = QSize()
        self._alpha = False
        self._svg: bytes | None = None
        self._movie: QMovie | None = None
        self._zoom = 1.0
        self._target_zoom = 1.0
        self._pos = QPointF()
        self._auto = True
        self._interacting = False
        self._generation = 0
        self._hq: tuple[tuple, QImage, QRectF] | None = None
        self._hq_pending: tuple | None = None
        self._press: QPointF | None = None
        self._press_pos = QPointF()
        self._mode = ""                    # "", "pan", "drag"
        self._wheel_rest = 0
        self._animation: fluent.Animation | None = None
        self._anchor = QPointF()
        self.wheel_mode = "zoom"
        self.worker = fluent.Worker(self, max_threads=1)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._settled)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("fluentFocusRing", False)
        self.setMouseTracking(True)
        self.setAccessibleName("Picture")
        self.grabGesture(Qt.GestureType.PinchGesture)
        self.setMinimumSize(80, 60)

    # --- content ---
    def has_picture(self) -> bool:
        return self._image is not None and not self._image.isNull()

    def image(self) -> QImage | None:
        return self._image

    def natural_size(self) -> QSize:
        return QSize(self._natural)

    def set_picture(self, image: QImage, natural: QSize | None = None, *, svg: bytes | None = None,
                    alpha: bool | None = None, keep_view: bool = False) -> None:
        """Show ``image``. ``natural`` is the real size of the picture when ``image`` is a smaller copy.

        ``keep_view=True`` keeps zoom and position: the same picture in
        better quality (the full decode replacing the preview).
        """
        self._stop_movie()
        same = keep_view and self.has_picture() and natural is not None and natural == self._natural
        self._image = image
        self._natural = QSize(natural) if natural is not None and natural.isValid() else image.size()
        self._alpha = image.hasAlphaChannel() if alpha is None else alpha
        self._svg = svg
        self._generation += 1
        self._hq = None
        if not same:
            self._stop_animation()
            self._auto = True
            self._zoom = self._target_zoom = self.default_zoom()
            self._centre()
            self.zoom_changed.emit()
        self._interacting = False
        self._settle.start()
        self._update_cursor()
        self.update()

    def set_movie(self, path: str) -> bool:
        """Play the animation in ``path`` in place of the still picture. False when it cannot be played."""
        self._stop_movie()
        if self._natural.width() * self._natural.height() > MOVIE_PIXELS:
            return False
        movie = QMovie(path, parent=self)
        movie.setCacheMode(QMovie.CacheMode.CacheNone)
        if not movie.isValid():
            movie.deleteLater()
            return False
        movie.frameChanged.connect(self._frame_changed)
        self._movie = movie
        if self.isVisible():
            movie.start()
        return True

    def is_animating(self) -> bool:
        return self._movie is not None and self._movie.state() == QMovie.MovieState.Running

    def clear(self) -> None:
        self._stop_movie()
        self._stop_animation()
        self._image = None
        self._natural = QSize()
        self._svg = None
        self._hq = None
        self._generation += 1
        self._update_cursor()
        self.update()

    def _stop_movie(self) -> None:
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None

    def _frame_changed(self, _frame: int) -> None:
        self.update()

    def shutdown(self) -> None:
        self._stop_movie()
        self._settle.stop()
        self.worker.shutdown()

    # --- zoom arithmetic ---
    def _ratio(self) -> float:
        return self.devicePixelRatioF() or 1.0

    def zoom(self) -> float:
        """Screen pixels per picture pixel: 1.0 is 100 %."""
        return self._zoom

    def zoom_percent(self) -> int:
        return max(1, round(self._zoom * 100))

    def is_fitted(self) -> bool:
        return self._auto

    def fit_zoom(self) -> float:
        """The zoom at which the whole picture just fills the widget."""
        if self._natural.isEmpty() or self.width() <= 0 or self.height() <= 0:
            return 1.0
        ratio = self._ratio()
        return min(self.width() * ratio / self._natural.width(), self.height() * ratio / self._natural.height())

    def default_zoom(self) -> float:
        """Fitted, but a picture smaller than the widget is not blown up."""
        return min(self.fit_zoom(), 1.0)

    def min_zoom(self) -> float:
        return min(self.default_zoom(), 1.0)

    def max_zoom(self) -> float:
        return max(MAX_ZOOM, self.fit_zoom())

    def _scale(self) -> float:
        return self._zoom / self._ratio()

    def picture_rect(self) -> QRectF:
        """Where the picture lies in the widget (logical pixels); at least one screen pixel on each side."""
        scale = self._scale()
        least = 1.0 / self._ratio()
        return QRectF(self._pos.x(), self._pos.y(), max(least, self._natural.width() * scale),
                      max(least, self._natural.height() * scale))

    def can_pan(self) -> bool:
        if not self.has_picture():
            return False
        rect = self.picture_rect()
        return rect.width() > self.width() + 0.5 or rect.height() > self.height() + 0.5

    def _centre(self) -> None:
        rect = self.picture_rect()
        self._pos = QPointF((self.width() - rect.width()) / 2, (self.height() - rect.height()) / 2)

    def _clamp(self) -> None:
        rect = self.picture_rect()
        x, y = self._pos.x(), self._pos.y()
        x = (self.width() - rect.width()) / 2 if rect.width() <= self.width() else min(0.0, max(self.width() - rect.width(), x))
        y = (self.height() - rect.height()) / 2 if rect.height() <= self.height() else min(0.0, max(self.height() - rect.height(), y))
        self._pos = QPointF(x, y)

    def _apply_zoom(self, zoom: float, anchor: QPointF) -> None:
        zoom = max(self.min_zoom(), min(self.max_zoom(), zoom))
        old_scale = self._scale()
        if old_scale <= 0:
            return
        picture_x = (anchor.x() - self._pos.x()) / old_scale
        picture_y = (anchor.y() - self._pos.y()) / old_scale
        self._zoom = zoom
        scale = self._scale()
        self._pos = QPointF(anchor.x() - picture_x * scale, anchor.y() - picture_y * scale)
        self._clamp()
        self._touch()
        self.zoom_changed.emit()

    def _touch(self) -> None:
        """The view changed through interaction: paint fast now, properly when it rests."""
        self._interacting = True
        self._hq = None
        self._settle.start()
        self._update_cursor()
        self.update()

    def _stop_animation(self) -> None:
        if self._animation is not None:
            self._animation.stop()
            self._animation = None

    def set_zoom(self, zoom: float, anchor: QPointF | None = None, *, animate: bool = True) -> None:
        """Zoom to ``zoom`` (1.0 = 100 %), keeping ``anchor`` (default: the centre) in place."""
        if not self.has_picture():
            return
        self._auto = False
        self._zoom_towards(zoom, anchor, animate)

    def _zoom_towards(self, zoom: float, anchor: QPointF | None, animate: bool) -> None:
        zoom = max(self.min_zoom(), min(self.max_zoom(), zoom))
        self._target_zoom = zoom
        self._anchor = QPointF(anchor) if anchor is not None else QPointF(self.width() / 2, self.height() / 2)
        self._stop_animation()
        time = fluent.duration("fast") if animate and self.isVisible() else 0
        if time <= 0 or abs(zoom - self._zoom) < 1e-9:
            self._apply_zoom(zoom, self._anchor)
            return
        animation = fluent.Animation(self)
        animation.setStartValue(float(self._zoom))
        animation.setEndValue(float(zoom))
        animation.setDuration(time)
        animation.setEasingCurve(fluent.easing("decelerate"))
        animation.valueChanged.connect(lambda value: self._apply_zoom(float(value), self._anchor))
        animation.finished.connect(self._animation_done)
        self._animation = animation
        animation.start()

    def _animation_done(self) -> None:
        self._animation = None
        if self._auto:
            self._zoom = self._target_zoom = self.default_zoom()
            self._centre()
            self.zoom_changed.emit()
            self.update()

    def zoom_in(self) -> None:
        self.set_zoom(self._target_zoom * BUTTON_STEP)

    def zoom_out(self) -> None:
        target = self._target_zoom / BUTTON_STEP
        if target <= self.default_zoom() * 1.0001:
            self.zoom_fit()
        else:
            self.set_zoom(target)

    def zoom_fit(self, *, animate: bool = True) -> None:
        """Back to the fitted state, which follows the size of the widget."""
        if not self.has_picture():
            return
        self._auto = True
        self._zoom_towards(self.default_zoom(), None, animate)

    def zoom_actual(self, anchor: QPointF | None = None, *, animate: bool = True) -> None:
        """100 %: one picture pixel on one screen pixel."""
        self.set_zoom(1.0, anchor, animate=animate)

    def toggle_zoom(self, anchor: QPointF | None = None) -> None:
        """Double click: from fitted to 100 % (or, for a small picture, to filling the widget) and back."""
        if not self.has_picture():
            return
        if self._auto:
            other = 1.0 if self.default_zoom() < 1.0 else self.fit_zoom()
            if abs(other - self._zoom) > 1e-6:
                self.set_zoom(other, anchor)
        else:
            self.zoom_fit()

    def pan_by(self, dx: float, dy: float) -> None:
        if self.can_pan():
            self._pos = QPointF(self._pos.x() + dx, self._pos.y() + dy)
            self._clamp()
            self._touch()

    # --- settled rendering ---
    def _view_key(self) -> tuple:
        return (self._generation, round(self._zoom, 6), round(self._pos.x(), 2), round(self._pos.y(), 2),
                self.width(), self.height())

    def _settled(self) -> None:
        if self._animation is not None or self._mode == "pan":
            self._settle.start()
            return
        self._interacting = False
        self._request_hq()
        self.update()

    def _request_hq(self) -> None:
        if not self.has_picture() or self._movie is not None or not self.isVisible():
            return
        key = self._view_key()
        if self._hq is not None and self._hq[0] == key or self._hq_pending == key:
            return
        picture = self.picture_rect()
        visible = picture.intersected(QRectF(self.rect()))
        if visible.isEmpty():
            return
        ratio = self._ratio()
        image = self._image
        if self._svg is not None:
            size = QSize(max(1, round(self._natural.width() * self._zoom)),
                         max(1, round(self._natural.height() * self._zoom)))
            clip = QRectF((visible.x() - picture.x()) * ratio, (visible.y() - picture.y()) * ratio,
                          visible.width() * ratio, visible.height() * ratio)
            device = QSize(max(1, round(visible.width() * ratio)), max(1, round(visible.height() * ratio)))
            data = self._svg
            target = visible

            def job() -> QImage:
                return decode.render_svg(decode.svg_renderer(data), size, clip, device)
        else:
            per_x = image.width() / picture.width()          # image pixels per logical pixel
            per_y = image.height() / picture.height()
            if per_x * ratio <= 1.0 and per_y * ratio <= 1.0:
                return                                        # shown at or above its size: the painter filters
            left = max(0, math.floor((visible.x() - picture.x()) * per_x))
            top = max(0, math.floor((visible.y() - picture.y()) * per_y))
            right = min(image.width(), math.ceil((visible.right() - picture.x()) * per_x))
            bottom = min(image.height(), math.ceil((visible.bottom() - picture.y()) * per_y))
            source = QRect(left, top, max(1, right - left), max(1, bottom - top))
            target = QRectF(picture.x() + source.x() / per_x, picture.y() + source.y() / per_y,
                            source.width() / per_x, source.height() / per_y)
            device = QSize(max(1, round(target.width() * ratio)), max(1, round(target.height() * ratio)))

            def job() -> QImage:
                return image.copy(source).scaled(device, Qt.AspectRatioMode.IgnoreAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation)
        self._hq_pending = key
        self.worker.submit(job, on_done=lambda result, k=key, t=target: self._hq_done(k, result, t),
                           on_error=lambda _exc, k=key: self._hq_failed(k))

    def _hq_done(self, key: tuple, image: QImage, target: QRectF) -> None:
        if self._hq_pending == key:
            self._hq_pending = None
        if key == self._view_key() and not self._interacting:
            self._hq = (key, image, target)
            self.update()
        elif not self._interacting:
            self._request_hq()

    def _hq_failed(self, key: tuple) -> None:
        if self._hq_pending == key:
            self._hq_pending = None

    def is_settled(self) -> bool:
        """Whether what is on screen is the final rendering of the current view (tests)."""
        if self._interacting or self._animation is not None or self._settle.isActive():
            return False
        return self._hq_pending is None

    def has_fine_rendering(self) -> bool:
        return self._hq is not None and self._hq[0] == self._view_key()

    # --- painting ---
    def paintEvent(self, _event) -> None:
        if not self.has_picture():
            return
        painter = QPainter(self)
        picture = self.picture_rect()
        visible = picture.intersected(QRectF(self.rect()))
        if visible.isEmpty():
            return
        if self._alpha:
            painter.setBrushOrigin(picture.topLeft())
            painter.fillRect(visible, checker_brush(self._ratio()))
        if self._movie is not None and self._movie.currentFrameNumber() >= 0:
            frame = self._movie.currentImage()
            if not frame.isNull():
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._zoom < NEAREST_ABOVE)
                painter.drawImage(picture, frame)
                return
        if self._hq is not None and self._hq[0] == self._view_key():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.drawImage(self._hq[2], self._hq[1])
            return
        image = self._image
        per_x = image.width() / picture.width()
        per_y = image.height() / picture.height()
        source = QRectF((visible.x() - picture.x()) * per_x, (visible.y() - picture.y()) * per_y,
                        visible.width() * per_x, visible.height() * per_y)
        magnification = 1.0 / (per_x * self._ratio()) if per_x > 0 else 1.0
        smooth = not self._interacting and magnification < NEAREST_ABOVE
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, smooth)
        painter.drawImage(visible, image, source)

    # --- events ---
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._movie is not None and self._movie.state() != QMovie.MovieState.Running:
            self._movie.start()
        self._settle.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self._movie is not None:
            self._movie.setPaused(True)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self.has_picture():
            return
        if self._auto:
            self._stop_animation()
            self._zoom = self._target_zoom = self.default_zoom()
            self._centre()
            self.zoom_changed.emit()
        else:
            if self._zoom < self.min_zoom():
                self._zoom = self._target_zoom = self.min_zoom()
                self.zoom_changed.emit()
            self._clamp()
        self._hq = None
        self._settle.start()
        self._update_cursor()

    def wheelEvent(self, event) -> None:
        if not self.has_picture():
            event.ignore()
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            delta = event.pixelDelta().y()
        control = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if control or self.wheel_mode == "zoom":
            if delta:
                self._auto = False
                self._zoom_towards(self._target_zoom * WHEEL_STEP ** delta, event.position(), True)
                if abs(self._target_zoom - self.default_zoom()) < 1e-9 and self._target_zoom <= self.default_zoom():
                    self._auto = True               # zoomed all the way out: fitted again
        else:
            self._wheel_rest += delta
            while abs(self._wheel_rest) >= 120:
                step = 1 if self._wheel_rest < 0 else -1
                self._wheel_rest += 120 * step
                self.step_requested.emit(step)
        event.accept()

    def event(self, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.NativeGesture and self.has_picture():
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self._auto = False
                self._stop_animation()
                self._apply_zoom(self._zoom * (1.0 + event.value()), event.position())
                self._target_zoom = self._zoom
                return True
        elif kind == QEvent.Type.Gesture and self.has_picture():
            pinch = event.gesture(Qt.GestureType.PinchGesture)
            if isinstance(pinch, QPinchGesture):
                self.pinch(pinch.scaleFactor(), QPointF(self.mapFromGlobal(pinch.centerPoint().toPoint())))
                event.accept(pinch)
                return True
        return super().event(event)

    def pinch(self, factor: float, centre: QPointF) -> None:
        """One step of a pinch: zoom by ``factor`` around ``centre`` (widget coordinates)."""
        if factor <= 0 or not self.has_picture():
            return
        self._auto = False
        self._stop_animation()
        self._apply_zoom(self._zoom * factor, centre)
        self._target_zoom = self._zoom

    def _update_cursor(self) -> None:
        if self._mode == "pan":
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self.can_pan():
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, event) -> None:
        button = event.button()
        if button == Qt.MouseButton.BackButton:
            self.step_requested.emit(-1)
        elif button == Qt.MouseButton.ForwardButton:
            self.step_requested.emit(1)
        elif button == Qt.MouseButton.LeftButton and self.has_picture():
            self._press = event.position()
            self._press_pos = QPointF(self._pos)
            self._mode = ""
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        else:
            super().mousePressEvent(event)
            return
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._press is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        delta = event.position() - self._press
        if not self._mode:
            if delta.manhattanLength() < QApplication.startDragDistance():
                return
            inside = self.picture_rect().contains(self._press)
            self._mode = "pan" if self.can_pan() else ("drag" if inside else "none")
            if self._mode == "pan":
                self._stop_animation()
                self._auto = False
                self._update_cursor()
            elif self._mode == "drag":
                self._press = None
                self._mode = ""
                self.drag_started.emit()
                return
        if self._mode == "pan":
            self._pos = QPointF(self._press_pos.x() + delta.x(), self._press_pos.y() + delta.y())
            self._clamp()
            self._touch()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = None
            self._mode = ""
            self._update_cursor()
            self._settle.start()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.has_picture():
            self._press = None
            self.toggle_zoom(event.position())
            event.accept()

    def to_picture(self, point: QPointF | QPoint) -> QPointF:
        """A widget position as a position in the picture (natural pixels)."""
        scale = self._scale()
        return QPointF((point.x() - self._pos.x()) / scale, (point.y() - self._pos.y()) / scale)
