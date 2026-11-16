"""Icons of the transport controls and the navigation items.

Each icon is asked for by its freedesktop name first (``fluent.icon``), so
an icon of the Boswas Prism pack is used whenever the pack has one. The
pack has no media control icons yet; for those names a plain geometric
glyph is painted here, in the text colour of the current scheme (so it
follows dark and light like a symbolic icon). ``MISSING`` lists the names
that had to be painted.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

import boswas_fluent as fluent

# glyph -> the freedesktop icon name that replaces it when the icon theme has it
NAMES = {
    "play": "media-playback-start-symbolic", "pause": "media-playback-pause-symbolic",
    "stop": "media-playback-stop-symbolic", "next": "media-skip-forward-symbolic",
    "previous": "media-skip-backward-symbolic", "shuffle": "media-playlist-shuffle-symbolic",
    "repeat": "media-playlist-repeat-symbolic", "repeat-one": "media-playlist-repeat-song-symbolic",
    "fullscreen": "view-fullscreen-symbolic", "fullscreen-exit": "view-restore-symbolic",
    "mini-player": "view-dual-symbolic", "home": "go-home-symbolic", "queue": "view-list-symbolic",
    "playlist": "playlist-symbolic", "music": "audio-x-generic-symbolic", "video": "video-x-generic-symbolic",
    "stream": "network-wired-symbolic", "edit": "document-edit-symbolic", "artist": "avatar-default-symbolic",
}
MISSING: set[str] = set()


def _pen(colour: QColor, width: float = 1.3) -> QPen:
    return QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def _fill(painter: QPainter, colour: QColor, points: list[tuple[float, float]]) -> None:
    painter.setPen(_pen(colour, 1.0))
    painter.setBrush(colour)
    painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))


def _lines(painter: QPainter, colour: QColor, *paths: list[tuple[float, float]], width: float = 1.3) -> None:
    painter.setPen(_pen(colour, width))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for points in paths:
        path = QPainterPath(QPointF(*points[0]))
        for point in points[1:]:
            path.lineTo(QPointF(*point))
        painter.drawPath(path)


def _play(p: QPainter, c: QColor) -> None:
    _fill(p, c, [(5, 3), (13, 8), (5, 13)])


def _pause(p: QPainter, c: QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    p.drawRoundedRect(QRectF(4, 3, 3, 10), 0.8, 0.8)
    p.drawRoundedRect(QRectF(9, 3, 3, 10), 0.8, 0.8)


def _stop(p: QPainter, c: QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    p.drawRoundedRect(QRectF(4, 4, 8, 8), 1, 1)


def _next(p: QPainter, c: QColor) -> None:
    _fill(p, c, [(3.5, 4), (10, 8), (3.5, 12)])
    _lines(p, c, [(12, 4), (12, 12)], width=1.6)


def _previous(p: QPainter, c: QColor) -> None:
    _fill(p, c, [(12.5, 4), (6, 8), (12.5, 12)])
    _lines(p, c, [(4, 4), (4, 12)], width=1.6)


def _shuffle(p: QPainter, c: QColor) -> None:
    _lines(p, c, [(2, 5), (5, 5), (10, 11), (13.5, 11)], [(2, 11), (5, 11), (6.3, 9.4)], [(8.7, 6.6), (10, 5), (13.5, 5)],
           [(11.8, 3.2), (13.6, 5), (11.8, 6.8)], [(11.8, 9.2), (13.6, 11), (11.8, 12.8)])


def _repeat(p: QPainter, c: QColor) -> None:
    _lines(p, c, [(4.5, 8), (4.5, 6.5), (6, 5), (12.5, 5)], [(10.8, 3.2), (12.6, 5), (10.8, 6.8)],
           [(11.5, 8), (11.5, 9.5), (10, 11), (3.5, 11)], [(5.2, 9.2), (3.4, 11), (5.2, 12.8)])


def _repeat_one(p: QPainter, c: QColor) -> None:
    _repeat(p, c)
    _lines(p, c, [(7.3, 7.2), (8.2, 6.6), (8.2, 9.6)], width=1.1)


def _fullscreen(p: QPainter, c: QColor) -> None:
    _lines(p, c, [(3, 6.5), (3, 3), (6.5, 3)], [(9.5, 3), (13, 3), (13, 6.5)], [(13, 9.5), (13, 13), (9.5, 13)],
           [(6.5, 13), (3, 13), (3, 9.5)])


def _fullscreen_exit(p: QPainter, c: QColor) -> None:
    _lines(p, c, [(3, 6.5), (6.5, 6.5), (6.5, 3)], [(9.5, 3), (9.5, 6.5), (13, 6.5)], [(13, 9.5), (9.5, 9.5), (9.5, 13)],
           [(6.5, 13), (6.5, 9.5), (3, 9.5)])


def _mini_player(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(2, 3.5, 12, 9), 1.5, 1.5)
    p.setBrush(c)
    p.drawRoundedRect(QRectF(8, 8, 4.5, 3), 0.6, 0.6)


def _home(p: QPainter, c: QColor) -> None:
    _lines(p, c, [(2.5, 8), (8, 3), (13.5, 8)], [(4, 7), (4, 13), (6.8, 13), (6.8, 9.6), (9.2, 9.6), (9.2, 13), (12, 13),
                                                (12, 7)])


def _queue(p: QPainter, c: QColor) -> None:
    _lines(p, c, [(2.5, 4), (10, 4)], [(2.5, 8), (8, 8)], [(2.5, 12), (8, 12)])
    _fill(p, c, [(10.5, 7.5), (14, 10), (10.5, 12.5)])


def _playlist(p: QPainter, c: QColor) -> None:
    _lines(p, c, [(2.5, 4), (9, 4)], [(2.5, 7.5), (9, 7.5)], [(2.5, 11), (6.5, 11)], [(12.5, 11.2), (12.5, 4), (14, 5)])
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    p.drawEllipse(QRectF(9.4, 10, 3.4, 2.8))


def _music(p: QPainter, c: QColor) -> None:
    _lines(p, c, [(6.2, 11.6), (6.2, 3.6), (12.4, 2.6), (12.4, 10.4)], [(6.2, 6), (12.4, 5)])
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    p.drawEllipse(QRectF(3, 10.2, 3.6, 3))
    p.drawEllipse(QRectF(9.2, 9, 3.6, 3))


def _video(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(2, 3.5, 12, 9), 1.5, 1.5)
    _fill(p, c, [(6.8, 6), (10.2, 8), (6.8, 10)])


def _stream(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRectF(2.5, 2.5, 11, 11))
    p.drawEllipse(QRectF(5.6, 2.5, 4.8, 11))
    _lines(p, c, [(2.8, 8), (13.2, 8)])


def _edit(p: QPainter, c: QColor) -> None:
    _lines(p, c, [(3, 13), (3.6, 10.2), (10.6, 3.2), (12.8, 5.4), (5.8, 12.4), (3, 13)], [(9.4, 4.4), (11.6, 6.6)])


def _artist(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRectF(5.4, 2.6, 5.2, 5.2))
    path = QPainterPath(QPointF(3, 13.4))
    path.cubicTo(QPointF(3, 8.6), QPointF(13, 8.6), QPointF(13, 13.4))
    p.drawPath(path)


_DRAW: dict[str, Callable[[QPainter, QColor], None]] = {
    "play": _play, "pause": _pause, "stop": _stop, "next": _next, "previous": _previous, "shuffle": _shuffle,
    "repeat": _repeat, "repeat-one": _repeat_one, "fullscreen": _fullscreen, "fullscreen-exit": _fullscreen_exit,
    "mini-player": _mini_player, "home": _home, "queue": _queue, "playlist": _playlist, "music": _music,
    "video": _video, "stream": _stream, "edit": _edit, "artist": _artist,
}


def paint(painter: QPainter, rect: QRect | QRectF, name: str, color: str = "text") -> None:
    """Paint the glyph ``name`` into ``rect`` in a token colour."""
    box = QRectF(rect)
    side = min(box.width(), box.height())
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(box.center().x() - side / 2, box.center().y() - side / 2)
    painter.scale(side / 16, side / 16)
    _DRAW[name](painter, fluent.qcolor(color))
    painter.restore()


class _GlyphEngine(QIconEngine):
    def __init__(self, name: str, color: str) -> None:
        super().__init__()
        self._name, self._color = name, color

    def clone(self) -> "_GlyphEngine":
        return _GlyphEngine(self._name, self._color)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        paint(painter, rect, self._name, "text-disabled" if mode == QIcon.Mode.Disabled else self._color)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap

    def actualSize(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QSize:
        side = min(size.width(), size.height())
        return QSize(side, side)


_icons: dict[tuple[str, str], QIcon] = {}


def icon(name: str, color: str = "text") -> QIcon:
    """The icon ``name``: from the icon theme when it has one, else the glyph painted here."""
    key = (name, color)
    cached = _icons.get(key)
    if cached is not None:
        return cached
    themed = NAMES.get(name, "")
    if themed and fluent.icon_path(themed) is not None:
        result = fluent.icon(themed, color=color)
    else:
        MISSING.add(themed or name)
        result = QIcon(_GlyphEngine(name, color))
    _icons[key] = result
    return result
