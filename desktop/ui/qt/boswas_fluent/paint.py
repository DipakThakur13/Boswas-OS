"""Painting helpers for widgets that draw themselves, in token colours.

Every function takes colours by token name (``"card"``, ``"stroke-surface"``,
``"fill-control-hover"``: anything ``Theme.rgba`` accepts) and looks them up
in the current theme when it paints, so a widget built on them follows a
scheme change with a plain ``update()``.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen

from . import icons
from .theming import current_theme, qcolor

STATUS_KINDS = ("info", "success", "warning", "danger", "attention", "neutral", "busy")
_STATUS_COLOURS = {"info": "info", "success": "success", "warning": "warning", "danger": "danger",
                   "attention": "danger", "neutral": "text-muted", "busy": "accent"}
_ALIASES = {"error": "danger", "critical": "danger", "ok": "success"}


def status_kind(kind: str) -> str:
    """Normalise a status name: ``error`` and ``critical`` are ``danger``, ``ok`` is ``success``."""
    kind = _ALIASES.get(kind, kind)
    return kind if kind in STATUS_KINDS else "info"


def status_colour(kind: str) -> str:
    """The token colour name of a status kind."""
    return _STATUS_COLOURS[status_kind(kind)]


def rounded_rect(painter: QPainter, rect: QRect | QRectF, radius: float, fill: str | QColor | None = None,
                 stroke: str | QColor | None = None, width: float = 1.0) -> None:
    """A rounded rectangle whose outline lies inside ``rect``, on whole pixels."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    box = QRectF(rect)
    if stroke is not None:
        half = width / 2
        box = box.adjusted(half, half, -half, -half)
        painter.setPen(QPen(qcolor(stroke) if isinstance(stroke, str) else stroke, width))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
    if fill is not None:
        painter.setBrush(qcolor(fill) if isinstance(fill, str) else fill)
    else:
        painter.setBrush(Qt.BrushStyle.NoBrush)
    radius = max(0.0, min(radius, box.width() / 2, box.height() / 2))
    painter.drawRoundedRect(box, radius, radius)
    painter.restore()


def card(painter: QPainter, rect: QRect | QRectF, *, fill: str = "card", stroke: str | None = "stroke-surface",
         radius: float | None = None) -> None:
    """The surface of a card: filled, with the one-pixel surface outline and the card radius."""
    rounded_rect(painter, rect, current_theme().radius("card") if radius is None else radius, fill, stroke)


def shadow(painter: QPainter, rect: QRect | QRectF, radius: float, elevation: str = "flyout") -> None:
    """A soft shadow around ``rect`` (drawn outside it), from an elevation token."""
    blur, offset, alpha = current_theme().elevation(elevation)
    if blur <= 0 or alpha <= 0:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    steps = max(4, min(int(blur), 24))
    base = QRectF(rect).translated(0, offset / 2)
    tint = qcolor("smoke")
    for step in range(steps):
        spread = (step + 0.5) * blur / steps / 2
        # A falling curve: most of the darkness sits close to the edge.
        strength = alpha * (1 - step / steps) ** 2 / steps * 2.2
        colour = QColor(tint)
        colour.setAlphaF(max(0.0, min(1.0, strength)))
        painter.setPen(QPen(colour, blur / steps / 2 + 1))
        painter.drawRoundedRect(base.adjusted(-spread, -spread, spread, spread), radius + spread, radius + spread)
    painter.restore()


def glyph(painter: QPainter, rect: QRect | QRectF, name: str, color: str = "text-secondary", size: int = 12,
          enabled: bool = True) -> None:
    """One of the toolkit's glyphs (chevron-down, close, check, more, ...) centred in ``rect``."""
    box = QRectF(rect)
    target = QRect(round(box.center().x() - size / 2), round(box.center().y() - size / 2), size, size)
    mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
    icons.glyph_icon(name, color).paint(painter, target, Qt.AlignmentFlag.AlignCenter, mode)


def status_badge(painter: QPainter, rect: QRect | QRectF, kind: str) -> None:
    """A status symbol with its own shape: i (info), check (success), triangle (warning), x (danger),
    ! (attention), dash (neutral), open ring (busy). No state is told by colour alone."""
    kind = status_kind(kind)
    theme = current_theme()
    box = QRectF(rect)
    side = min(box.width(), box.height())
    box = QRectF(box.center().x() - side / 2, box.center().y() - side / 2, side, side)
    tint = qcolor(_STATUS_COLOURS[kind], theme)
    mark = qcolor("on-accent" if kind not in ("neutral",) else "surface", theme)

    def point(x: float, y: float) -> QPointF:
        return QPointF(box.left() + x * side, box.top() + y * side)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    line = QPen(mark, max(1.4, side * 0.11), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    if kind == "busy":
        painter.setPen(QPen(tint, max(1.5, side * 0.11), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(box.adjusted(side * 0.12, side * 0.12, -side * 0.12, -side * 0.12), 90 * 16, -270 * 16)
    elif kind == "warning":
        path = QPainterPath(point(0.5, 0.08))
        path.lineTo(point(0.96, 0.9))
        path.lineTo(point(0.04, 0.9))
        path.closeSubpath()
        painter.setPen(QPen(tint, max(1.0, side * 0.08), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(tint)
        painter.drawPath(path)
        painter.setPen(line)
        painter.drawLine(point(0.5, 0.4), point(0.5, 0.6))
        painter.drawPoint(point(0.5, 0.76))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(tint)
        painter.drawEllipse(box.adjusted(side * 0.02, side * 0.02, -side * 0.02, -side * 0.02))
        painter.setPen(line)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if kind == "success":
            path = QPainterPath(point(0.29, 0.52))
            path.lineTo(point(0.44, 0.66))
            path.lineTo(point(0.72, 0.36))
            painter.drawPath(path)
        elif kind == "attention":
            painter.drawLine(point(0.5, 0.27), point(0.5, 0.55))
            painter.drawPoint(point(0.5, 0.73))
        elif kind == "danger":
            painter.drawLine(point(0.35, 0.35), point(0.65, 0.65))
            painter.drawLine(point(0.65, 0.35), point(0.35, 0.65))
        elif kind == "info":
            painter.drawPoint(point(0.5, 0.28))
            painter.drawLine(point(0.5, 0.46), point(0.5, 0.73))
        else:
            painter.drawLine(point(0.33, 0.5), point(0.67, 0.5))
    painter.restore()


def elided(painter: QPainter, rect: QRect | QRectF, text: str, font: QFont, color: str | QColor,
           flags: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) -> None:
    """One line of plain text, cut with an ellipsis when it does not fit."""
    painter.save()
    painter.setFont(font)
    painter.setPen(qcolor(color) if isinstance(color, str) else color)
    box = QRectF(rect)
    painter.drawText(box, int(flags), QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight,
                                                                    math.floor(box.width())))
    painter.restore()
