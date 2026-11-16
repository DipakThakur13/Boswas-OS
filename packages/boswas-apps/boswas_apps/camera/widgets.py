"""The custom widgets of Camera: icons, the viewfinder, the shutter button and the thumbnail of the last capture.

The panels beside the picture and everything on them use the colours of
the toolkit's theme. The picture area itself is black where the camera
picture does not reach, and the flash of the shutter animation is white,
as on every camera.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QIcon, QIconEngine, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

# name -> freedesktop icon names tried in order; the first one the icon theme has is used
NAMES = {
    "photo": ("camera-photo-symbolic",), "video": ("camera-video-symbolic",),
    "switch": ("camera-switch-symbolic", "view-refresh-symbolic"),
    "timer": ("timer-symbolic", "preferences-system-time-symbolic"), "grid": ("view-grid-symbolic",),
    "mirror": ("object-flip-horizontal-symbolic",), "flash": ("camera-flash-symbolic",),
    "torch": ("torch-symbolic",), "adjust": ("display-brightness-symbolic",),
    "microphone": ("audio-input-microphone-symbolic",), "microphone-muted": ("microphone-sensitivity-muted-symbolic",),
    "pause": ("media-playback-pause-symbolic",), "resume": ("media-playback-start-symbolic",),
    "settings": ("preferences-system-symbolic",), "back": ("go-previous-symbolic",),
}
MISSING: set[str] = set()


def _pen(colour: QColor, width: float = 1.3) -> QPen:
    return QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def _mirror(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(QPointF(8, 2.5), QPointF(8, 13.5))
    p.drawPolygon(QPolygonF([QPointF(6, 4.5), QPointF(6, 11.5), QPointF(2, 11.5)]))
    p.setBrush(c)
    p.drawPolygon(QPolygonF([QPointF(10, 4.5), QPointF(10, 11.5), QPointF(14, 11.5)]))


def _flash(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.0))
    p.setBrush(c)
    p.drawPolygon(QPolygonF([QPointF(9.5, 1.8), QPointF(4, 9), QPointF(7.6, 9), QPointF(6.5, 14.2), QPointF(12, 7),
                             QPointF(8.4, 7)]))


def _torch(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolygon(QPolygonF([QPointF(5, 6.5), QPointF(11, 6.5), QPointF(9.6, 9), QPointF(9.6, 14), QPointF(6.4, 14),
                             QPointF(6.4, 9)]))
    for x1, y1, x2, y2 in ((8, 1.5, 8, 4), (3.6, 2.6, 5, 4.4), (12.4, 2.6, 11, 4.4)):
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


_DRAW = {"mirror": _mirror, "flash": _flash, "torch": _torch}


class _GlyphEngine(QIconEngine):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def clone(self) -> "_GlyphEngine":
        return _GlyphEngine(self._name)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        side = min(rect.width(), rect.height())
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(rect.center().x() + 0.5 - side / 2, rect.center().y() + 0.5 - side / 2)
        painter.scale(side / 16, side / 16)
        _DRAW[self._name](painter, fluent.qcolor("text-disabled" if mode == QIcon.Mode.Disabled else "text"))
        painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap


_icons: dict[str, QIcon] = {}


def icon(name: str) -> QIcon:
    """The icon ``name`` from the icon theme; the three the pack does not have are painted here."""
    cached = _icons.get(name)
    if cached is not None:
        return cached
    result = QIcon()
    for candidate in NAMES[name]:
        if fluent.icon_path(candidate) is not None:
            result = fluent.icon(candidate)
            break
    if result.isNull():
        MISSING.add(NAMES[name][0])
        result = QIcon(_GlyphEngine(name)) if name in _DRAW else fluent.icon(NAMES[name][-1])
    _icons[name] = result
    return result


class Viewfinder(QWidget):
    """The live picture: it fills the widget (cropped to fit), optionally mirrored, with grid, countdown and badge."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame: object = None
        self._image: QImage | None = None
        self.mirror = False
        self.grid = False
        self.countdown = 0
        self.badge = ""
        self.badge_live = False
        self._flash = 0.0
        self._animation: QVariantAnimation | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(160, 120)
        self.setAccessibleName("Camera preview")

    def set_frame(self, frame: object) -> None:
        self._frame = frame
        self._image = frame if isinstance(frame, QImage) else None
        self.update()

    def has_frame(self) -> bool:
        return self._frame is not None

    def image(self) -> QImage | None:
        if self._image is None and self._frame is not None:
            image = self._frame.toImage()
            self._image = image if not image.isNull() else None
        return self._image

    def set_options(self, *, mirror: bool | None = None, grid: bool | None = None) -> None:
        if mirror is not None:
            self.mirror = mirror
        if grid is not None:
            self.grid = grid
        self.update()

    def set_countdown(self, seconds: int) -> None:
        self.countdown = max(0, seconds)
        self.setAccessibleDescription(f"{seconds} seconds until the capture" if seconds else "")
        self.update()

    def set_badge(self, text: str, live: bool = False) -> None:
        self.badge, self.badge_live = text, live
        self.update()

    def flash(self) -> bool:
        """The shutter animation: the picture flashes white and fades back. False (nothing) with reduced motion."""
        duration = fluent.duration("slow")
        if duration <= 0:
            return False
        if self._animation is not None:
            self._animation.stop()
        animation = QVariantAnimation(self)
        animation.setStartValue(0.85)
        animation.setEndValue(0.0)
        animation.setDuration(duration)
        animation.setEasingCurve(fluent.easing("decelerate"))
        animation.valueChanged.connect(self._set_flash)
        animation.finished.connect(lambda: self._set_flash(0.0))
        self._animation = animation
        animation.start()
        return True

    def is_flashing(self) -> bool:
        return self._flash > 0

    def _set_flash(self, value) -> None:
        self._flash = float(value)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        image = self.image()
        if image is not None and image.width() > 0 and image.height() > 0:
            size = image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding)
            target = QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2, size.width(),
                           size.height())
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            if self.mirror:
                painter.translate(self.width(), 0)
                painter.scale(-1, 1)
            painter.drawImage(target, image)
            painter.restore()
        if self.grid:
            painter.save()
            pen = QPen(fluent.qcolor("on-accent" if fluent.theme().is_dark else "surface"), 1)
            painter.setOpacity(0.55)
            painter.setPen(pen)
            for step in (1, 2):
                x, y = self.width() * step // 3, self.height() * step // 3
                painter.drawLine(x, 0, x, self.height())
                painter.drawLine(0, y, self.width(), y)
            painter.restore()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.badge:
            font = fluent.font("body-strong")
            width = painter.fontMetrics().horizontalAdvance(self.badge) + (44 if self.badge_live else 28)
            painter.setFont(font)
            width = max(width, 72)
            pill = QRectF((self.width() - width) / 2, 16, width, 32)
            paint.rounded_rect(painter, pill, 16, "surface", "stroke-surface")
            text = QRectF(pill)
            if self.badge_live:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fluent.qcolor("danger"))
                painter.drawEllipse(QPointF(pill.left() + 18, pill.center().y()), 5, 5)
                text.setLeft(pill.left() + 22)
            paint.elided(painter, text, self.badge, font, "text", Qt.AlignmentFlag.AlignCenter)
        if self.countdown:
            side = 120
            circle = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
            painter.setOpacity(0.9)
            paint.rounded_rect(painter, circle, side / 2, "surface", "stroke-surface")
            painter.setOpacity(1.0)
            paint.elided(painter, circle, str(self.countdown), fluent.font("title-large"), "text",
                         Qt.AlignmentFlag.AlignCenter)
        if self._flash > 0:
            painter.setOpacity(self._flash)
            painter.fillRect(self.rect(), Qt.GlobalColor.white)


class ShutterButton(QAbstractButton):
    """The big round button: takes a photo, or starts and stops a recording."""

    SIZE = 72

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = "photo"
        self.recording = False
        self.counting = False
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.sync()

    def set_state(self, mode: str, recording: bool, counting: bool) -> None:
        self.mode, self.recording, self.counting = mode, recording, counting
        self.sync()

    def sync(self) -> None:
        if self.counting:
            text = "Cancel the timer"
        elif self.mode == "photo":
            text = "Take photo"
        else:
            text = "Stop recording" if self.recording else "Start recording"
        self.setToolTip(f"{text} (Space)")
        self.setAccessibleName(text)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        ring = fluent.qcolor("text" if enabled else "text-disabled")
        box = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        painter.setPen(QPen(ring, 3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(box)
        inner = box.adjusted(7, 7, -7, -7)
        if self.isDown():
            inner = inner.adjusted(2, 2, -2, -2)
        video = self.mode == "video"
        fill = fluent.qcolor("text-disabled" if not enabled else ("danger" if video else "text"))
        if enabled and self.underMouse() and not self.isDown():
            painter.setOpacity(0.88)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        if self.counting or (video and self.recording):
            square = QRectF(0, 0, 24, 24)
            square.moveCenter(box.center())
            painter.drawRoundedRect(square, 5, 5)
        else:
            painter.drawEllipse(inner)


class Thumbnail(QAbstractButton):
    """The last capture as a small picture; a click opens it."""

    SIZE = 56

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self.video = False
        self.path = ""
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_capture("", None)

    def set_capture(self, path: str, pixmap: QPixmap | None) -> None:
        self.path = path
        self.video = path.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".avi"))
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self.setEnabled(bool(path))
        text = "Open the last capture" if path else "No captures yet"
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.update()

    def has_picture(self) -> bool:
        return self._pixmap is not None

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = fluent.theme().radius("card")
        if self._pixmap is not None:
            path = QPainterPath()
            path.addRoundedRect(box, radius, radius)
            painter.setClipPath(path)
            source = QRectF(self._pixmap.rect())
            side = min(source.width(), source.height())
            source = QRectF((source.width() - side) / 2, (source.height() - side) / 2, side, side)
            painter.drawPixmap(box, self._pixmap, source)
            painter.setClipping(False)
            paint.rounded_rect(painter, box, radius, None, "stroke-strong" if self.underMouse() else "stroke-surface")
        else:
            paint.rounded_rect(painter, box, radius, "fill-control", "stroke-surface")
            glyph = icon("video" if self.video else "photo")
            mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
            glyph.paint(painter, QRect(self.width() // 2 - 10, self.height() // 2 - 10, 20, 20),
                        Qt.AlignmentFlag.AlignCenter, mode)
