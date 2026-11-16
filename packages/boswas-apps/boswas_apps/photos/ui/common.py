"""Shared pieces of the Photos widgets: icons for commands, buttons that sit on top of a picture.

Colours come from the toolkit's tokens (``fluent.qcolor``); nothing here
names a colour of its own.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QFontMetrics, QIcon, QIconEngine, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractButton, QLabel, QSizePolicy, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

CHECKER = 8            # side of a square of the transparency pattern, logical pixels


class _TintedEngine(QIconEngine):
    """A one-colour icon of the system icon theme, painted in the text colour of the current scheme.

    The Boswas Prism pack has few action icons; the others come from the
    theme it inherits. The toolkit recolours only Prism's own symbolic
    icons, so the same is done here for the inherited ones: every pixel
    takes the token colour, the shape (alpha) stays.
    """

    def __init__(self, base: QIcon, tint: str = "text") -> None:
        super().__init__()
        self._base = base
        self._tint = tint

    def clone(self) -> "_TintedEngine":
        return _TintedEngine(self._base, self._tint)

    def _render(self, size: QSize, mode: QIcon.Mode) -> QPixmap:
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        if not size.isEmpty():
            painter = QPainter(image)
            self._base.paint(painter, QRect(QPoint(0, 0), size), Qt.AlignmentFlag.AlignCenter, QIcon.Mode.Normal)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(image.rect(), fluent.qcolor("text-disabled" if mode == QIcon.Mode.Disabled
                                                         else self._tint))
            painter.end()
        return QPixmap.fromImage(image)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self._render(size, mode)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        pixmap = self._render(size * scale, mode)
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        side = min(rect.width(), rect.height())
        pixmap = self._render(QSize(round(side * ratio), round(side * ratio)), mode)
        target = QRect(rect.x() + (rect.width() - side) // 2, rect.y() + (rect.height() - side) // 2, side, side)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(target, pixmap)
        painter.restore()

    def actualSize(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QSize:
        side = min(size.width(), size.height())
        return QSize(side, side)


_action_icons: dict[tuple, QIcon] = {}


def action_icon(name: str, *fallbacks: str, color: str = "text") -> QIcon:
    """A one-colour icon for a command: from Boswas Prism when it has one, else from the inherited
    icon theme, tinted to the text colour. An empty icon when nobody has it."""
    key = (name, fallbacks, color, QIcon.themeName())
    cached = _action_icons.get(key)
    if cached is not None:
        return cached
    result = QIcon()
    for candidate in (name, *fallbacks):
        symbolic = candidate if candidate.endswith("-symbolic") else f"{candidate}-symbolic"
        if fluent.icon_path(symbolic) is not None:
            result = fluent.icon(symbolic, color=color)
            break
    else:
        for candidate in (name, *fallbacks):
            symbolic = candidate if candidate.endswith("-symbolic") else f"{candidate}-symbolic"
            for theme_name in (symbolic, candidate):
                if QIcon.hasThemeIcon(theme_name):
                    result = QIcon(_TintedEngine(QIcon.fromTheme(theme_name), color))
                    break
            if not result.isNull():
                break
    if len(_action_icons) > 256:
        _action_icons.clear()
    _action_icons[key] = result
    return result


def checker_brush(ratio: float = 1.0) -> QBrush:
    """The pattern behind transparent pictures, in two surface colours of the theme."""
    side = max(1, round(CHECKER * ratio))
    tile = QPixmap(side * 2, side * 2)
    tile.fill(fluent.qcolor("surface"))
    painter = QPainter(tile)
    dark = fluent.qcolor("surface-hover")
    painter.fillRect(0, 0, side, side, dark)
    painter.fillRect(side, side, side, side, dark)
    painter.end()
    tile.setDevicePixelRatio(ratio)
    return QBrush(tile)


class OverlayButton(QAbstractButton):
    """A round-cornered icon button with its own surface, for controls that lie on top of a picture."""

    def __init__(self, icon: QIcon, tooltip: str, parent: QWidget | None = None, *, size: QSize = QSize(40, 40),
                 icon_size: int = 16) -> None:
        super().__init__(parent)
        self.setIcon(icon)
        self._icon_size = icon_size
        self.setFixedSize(size)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def sizeHint(self) -> QSize:
        return self.size()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        radius = fluent.theme().radius("control")
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        paint.rounded_rect(painter, box, radius, "flyout", "stroke-surface")
        if self.isDown():
            paint.rounded_rect(painter, box, radius, "fill-subtle-pressed")
        elif self.underMouse() and self.isEnabled():
            paint.rounded_rect(painter, box, radius, "fill-subtle-hover")
        side = self._icon_size
        target = QRect((self.width() - side) // 2, (self.height() - side) // 2, side, side)
        mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        self.icon().paint(painter, target, Qt.AlignmentFlag.AlignCenter, mode)


class ElidedLabel(QLabel):
    """A one-line plain-text label that cuts its text with an ellipsis in the middle (file names)."""

    def __init__(self, text: str = "", style: str = "body", parent: QWidget | None = None, *,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideMiddle) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self._full = text
        self._mode = mode
        self.setFont(fluent.font(style))
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        self._apply()

    def full_text(self) -> str:
        return self._full

    def set_text(self, text: str) -> None:
        self._full = text
        self.setAccessibleName(text)
        self._apply()

    def _apply(self) -> None:
        metrics = QFontMetrics(self.font())
        shown = metrics.elidedText(self._full, self._mode, max(0, self.width() - 2))
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self._full) + 4, metrics.height() + 2)


class Surface(QWidget):
    """A widget filled with a token colour (bars that lie on top of the picture in full screen)."""

    def __init__(self, parent: QWidget | None = None, *, fill: str = "background", line: str | None = None) -> None:
        super().__init__(parent)
        self._fill = fill
        self._line = line

    def set_fill(self, fill: str | None) -> None:
        self._fill = fill
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self._fill:
            painter.fillRect(self.rect(), fluent.qcolor(self._fill))
        if self._line:
            painter.fillRect(0, self.height() - 1, self.width(), 1, fluent.qcolor(self._line))
