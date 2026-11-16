"""Icons of the Files window.

``symbolic(name)`` gives a one-colour action icon in the text colour of the
current scheme. Boswas Prism is asked first (through the toolkit). The
pack has few action icons, so the system icon theme (Breeze on Boswas OS)
is asked next; those icons come in their own fixed colour, and
``_TintEngine`` repaints them in the text colour each time they are drawn,
so they follow light and dark like the Prism ones.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QIcon, QIconEngine, QImage, QPainter, QPixmap

import boswas_fluent as fluent

_cache: dict[tuple, QIcon] = {}


class _TintEngine(QIconEngine):
    def __init__(self, base: QIcon) -> None:
        super().__init__()
        self._base = base

    def clone(self) -> "_TintEngine":
        return _TintEngine(self._base)

    def _render(self, size: QSize, mode: QIcon.Mode) -> QPixmap:
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        if not size.isEmpty():
            painter = QPainter(image)
            self._base.paint(painter, QRect(QPoint(0, 0), size))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(image.rect(), fluent.qcolor("text-disabled" if mode == QIcon.Mode.Disabled else "text"))
            painter.end()
        return QPixmap.fromImage(image)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self._render(size, mode)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        side = min(rect.width(), rect.height())
        pixmap = self._render(QSize(round(side * ratio), round(side * ratio)), mode)
        target = QRect(rect.x() + (rect.width() - side) // 2, rect.y() + (rect.height() - side) // 2, side, side)
        painter.drawPixmap(target, pixmap)


def symbolic(name: str, *fallbacks: str) -> QIcon:
    """The symbolic icon ``name`` (or the first fallback that exists), in the text colour. Empty when none exists."""
    key = (name, fallbacks)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    result = QIcon()
    for candidate in (name, *fallbacks):
        full = candidate if candidate.endswith("-symbolic") else candidate + "-symbolic"
        if fluent.icon_path(full) is not None:
            result = fluent.icon(full)
            break
        if QIcon.hasThemeIcon(full):
            result = QIcon(_TintEngine(QIcon.fromTheme(full)))
            break
    _cache[key] = result
    return result


def named(name: str, *fallbacks: str) -> QIcon:
    """A coloured icon (a folder, a drive, an application) from Boswas Prism or the system theme."""
    return fluent.icon(name, *fallbacks)


def app_icon(name: str) -> QIcon:
    """The icon of an application from its desktop entry: a theme name or an absolute path of an image."""
    key = ("app", name)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    result = QIcon()
    if name.startswith("/"):
        pixmap = QPixmap(name) if len(name) < 4096 else QPixmap()
        if not pixmap.isNull():
            result = QIcon(pixmap)
    elif name:
        result = fluent.icon(name, symbolic=False)
    if result.isNull():
        result = fluent.icon("application-x-executable")
    _cache[key] = result
    return result
