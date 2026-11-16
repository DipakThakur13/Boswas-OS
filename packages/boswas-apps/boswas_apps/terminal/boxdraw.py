"""Box drawing, block and Braille characters drawn as shapes.

A font's line drawing glyphs rarely meet from cell to cell once the cell
size has been rounded to whole pixels. The characters U+2500 to U+259F and
the Braille patterns are therefore drawn here, from one cell edge to the
other, so frames and bars in programs are continuous at every font size
and scale. What each box drawing character consists of is read from its
Unicode name ("BOX DRAWINGS DOWN LIGHT AND RIGHT HEAVY").
"""

from __future__ import annotations

import unicodedata

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

_WEIGHTS = {"LIGHT": 1, "HEAVY": 2, "SINGLE": 1, "DOUBLE": 3}
_DIRECTIONS = {"UP": ("u",), "DOWN": ("d",), "LEFT": ("l",), "RIGHT": ("r",), "VERTICAL": ("u", "d"),
               "HORIZONTAL": ("l", "r")}
_DASHES = {"DOUBLE": 2, "TRIPLE": 3, "QUADRUPLE": 4}
_QUADRANTS = {0x2596: (0, 0, 1, 0), 0x2597: (0, 0, 0, 1), 0x2598: (1, 0, 0, 0), 0x2599: (1, 0, 1, 1),
              0x259A: (1, 0, 0, 1), 0x259B: (1, 1, 1, 0), 0x259C: (1, 1, 0, 1), 0x259D: (0, 1, 0, 0),
              0x259E: (0, 1, 1, 0), 0x259F: (0, 1, 1, 1)}          # upper left, upper right, lower left, lower right
_specs: dict[str, tuple | None] = {}


def handles(char: str) -> bool:
    """Whether ``draw`` draws this character."""
    return "─" <= char <= "▟" or "⠀" <= char <= "⣿"


def _spec(char: str) -> tuple | None:
    spec = _specs.get(char, 0)
    if spec == 0:
        spec = _specs[char] = _parse(char)
    return spec          # type: ignore[return-value]


def _parse(char: str) -> tuple | None:
    words = unicodedata.name(char, "").split()
    if words[:2] != ["BOX", "DRAWINGS"]:
        return None
    words = words[2:]
    if "DASH" in words:
        if len(words) == 4 and words[0] in ("LIGHT", "HEAVY") and words[1] in _DASHES:
            return ("dash", words[3] == "HORIZONTAL", _WEIGHTS[words[0]], _DASHES[words[1]])
        return None
    if "ARC" in words:
        return ("arc", "UP" in words, "LEFT" in words)
    if "DIAGONAL" in words:
        if "CROSS" in words:
            return ("diagonal", True, True)
        rising = words[-4:] == ["RIGHT", "TO", "LOWER", "LEFT"]
        return ("diagonal", rising, not rising)
    arms = {"u": 0, "d": 0, "l": 0, "r": 0}
    inherited = 0
    group: list[str] = []
    for word in [*words, "AND"]:
        if word != "AND":
            group.append(word)
            continue
        weights = [_WEIGHTS[item] for item in group if item in _WEIGHTS]
        weight = weights[0] if weights else inherited
        if group and group[0] in _WEIGHTS:
            inherited = weight
        for item in group:
            for arm in _DIRECTIONS.get(item, ()):
                arms[arm] = weight
        group = []
    if not any(arms.values()):
        return None
    return ("lines", arms["u"], arms["d"], arms["l"], arms["r"])


def draw(painter: QPainter, char: str, x: int, y: int, width: int, height: int, colour: QColor) -> bool:
    """Draw ``char`` into the cell at (x, y) of ``width`` by ``height`` pixels. False: not a character drawn here."""
    code = ord(char)
    if 0x2580 <= code <= 0x259F:
        _block(painter, code, x, y, width, height, colour)
        return True
    if 0x2800 <= code <= 0x28FF:
        _braille(painter, code - 0x2800, x, y, width, height, colour)
        return True
    spec = _spec(char)
    if spec is None:
        return False
    light = max(1, round(width / 8))
    kind = spec[0]
    if kind == "lines":
        _lines(painter, spec[1], spec[2], spec[3], spec[4], x, y, width, height, colour, light)
    elif kind == "dash":
        _dash(painter, spec[1], light * spec[2], spec[3], x, y, width, height, colour)
    elif kind == "arc":
        _arc(painter, spec[1], spec[2], x, y, width, height, colour, light)
    else:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(QRectF(x, y, width, height))
        painter.setPen(QPen(colour, light))
        if spec[1]:
            painter.drawLine(QPointF(x + width, y), QPointF(x, y + height))
        if spec[2]:
            painter.drawLine(QPointF(x, y), QPointF(x + width, y + height))
        painter.restore()
    return True


def _thickness(weight: int, light: int) -> int:
    return light * 2 if weight == 2 else light


def _lines(painter: QPainter, up: int, down: int, left: int, right: int, x: int, y: int, width: int, height: int,
           colour: QColor, light: int) -> None:
    fill = painter.fillRect
    cx, cy = x + width // 2, y + height // 2
    right_edge, bottom_edge = x + width, y + height
    t = light
    # positions of the two lines of a double: a is the left or upper one, b the right or lower one
    xa, xb = cx - t // 2 - t, cx - t // 2 + t
    ya, yb = cy - t // 2 - t, cy - t // 2 + t
    vertical = max(_thickness(up, light) if up in (1, 2) else 0, _thickness(down, light) if down in (1, 2) else 0)
    horizontal = max(_thickness(left, light) if left in (1, 2) else 0,
                     _thickness(right, light) if right in (1, 2) else 0)
    double_vertical = up == 3 or down == 3
    double_horizontal = left == 3 or right == 3

    # --- horizontal arms ---
    for arm, other, leftward in ((left, right, True), (right, left, False)):
        if not arm:
            continue
        if arm == 3:
            for line_y, near, far in ((ya, up, down), (yb, down, up)):
                if near == 3:
                    inner = xa + t if leftward else xb          # stops at the line of the double it meets
                elif far == 3:
                    inner = xb + t if leftward else xa          # goes around the corner
                elif vertical:
                    inner = cx - vertical // 2 + (vertical if leftward else 0)
                else:
                    inner = cx if other else cx - t // 2 + (t if leftward else 0)
                if leftward:
                    fill(x, line_y, inner - x, t, colour)
                else:
                    fill(inner, line_y, right_edge - inner, t, colour)
        else:
            thick = _thickness(arm, light)
            top = cy - thick // 2
            if double_vertical:
                inner = cx if other else (xa + t if leftward else xb)
            elif vertical:
                inner = cx - vertical // 2 + (vertical if leftward else 0)
            else:
                inner = cx if other else cx - thick // 2 + (thick if leftward else 0)
            if leftward:
                fill(x, top, inner - x, thick, colour)
            else:
                fill(inner, top, right_edge - inner, thick, colour)

    # --- vertical arms ---
    for arm, other, upward in ((up, down, True), (down, up, False)):
        if not arm:
            continue
        if arm == 3:
            for line_x, near, far in ((xa, left, right), (xb, right, left)):
                if near == 3:
                    inner = ya + t if upward else yb
                elif far == 3:
                    inner = yb + t if upward else ya
                elif horizontal:
                    inner = cy - horizontal // 2 + (horizontal if upward else 0)
                else:
                    inner = cy if other else cy - t // 2 + (t if upward else 0)
                if upward:
                    fill(line_x, y, t, inner - y, colour)
                else:
                    fill(line_x, inner, t, bottom_edge - inner, colour)
        else:
            thick = _thickness(arm, light)
            start = cx - thick // 2
            if double_horizontal:
                inner = cy if other else (ya + t if upward else yb)
            elif horizontal:
                inner = cy - horizontal // 2 + (horizontal if upward else 0)
            else:
                inner = cy if other else cy - thick // 2 + (thick if upward else 0)
            if upward:
                fill(start, y, thick, inner - y, colour)
            else:
                fill(start, inner, thick, bottom_edge - inner, colour)


def _dash(painter: QPainter, horizontal: bool, thick: int, count: int, x: int, y: int, width: int, height: int,
          colour: QColor) -> None:
    length = width if horizontal else height
    gap = max(1, round(length / (count * 4)))
    for index in range(count):
        start = round(index * length / count)
        end = round((index + 1) * length / count) - gap
        if end <= start:
            end = start + 1
        if horizontal:
            painter.fillRect(x + start, y + height // 2 - thick // 2, end - start, thick, colour)
        else:
            painter.fillRect(x + width // 2 - thick // 2, y + start, thick, end - start, colour)


def _arc(painter: QPainter, up: bool, left: bool, x: int, y: int, width: int, height: int, colour: QColor,
         light: int) -> None:
    # the centre lines of the light strokes, as _lines places them
    cx = x + width // 2 - light // 2 + light / 2
    cy = y + height // 2 - light // 2 + light / 2
    radius = min(width, height) / 2
    end_x = x if left else x + width
    end_y = y if up else y + height
    path = QPainterPath(QPointF(cx, end_y))
    path.lineTo(cx, cy + (-radius if up else radius))
    path.quadTo(cx, cy, cx + (-radius if left else radius), cy)
    path.lineTo(end_x, cy)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(colour, light, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.restore()


def _block(painter: QPainter, code: int, x: int, y: int, width: int, height: int, colour: QColor) -> None:
    fill = painter.fillRect
    if code == 0x2580:
        fill(x, y, width, height // 2, colour)
    elif 0x2581 <= code <= 0x2588:
        part = round(height * (code - 0x2580) / 8)
        fill(x, y + height - part, width, part, colour)
    elif 0x2589 <= code <= 0x258F:
        fill(x, y, round(width * (0x2590 - code) / 8), height, colour)
    elif code == 0x2590:
        fill(x + width // 2, y, width - width // 2, height, colour)
    elif 0x2591 <= code <= 0x2593:
        shade = QColor(colour)
        shade.setAlphaF(colour.alphaF() * (code - 0x2590) / 4)
        fill(x, y, width, height, shade)
    elif code == 0x2594:
        fill(x, y, width, max(1, round(height / 8)), colour)
    elif code == 0x2595:
        part = max(1, round(width / 8))
        fill(x + width - part, y, part, height, colour)
    else:
        upper_left, upper_right, lower_left, lower_right = _QUADRANTS[code]
        half_w, half_h = width // 2, height // 2
        if upper_left:
            fill(x, y, half_w, half_h, colour)
        if upper_right:
            fill(x + half_w, y, width - half_w, half_h, colour)
        if lower_left:
            fill(x, y + half_h, half_w, height - half_h, colour)
        if lower_right:
            fill(x + half_w, y + half_h, width - half_w, height - half_h, colour)


def _braille(painter: QPainter, bits: int, x: int, y: int, width: int, height: int, colour: QColor) -> None:
    if not bits:
        return
    dot_w = max(1, round(width / 5))
    dot_h = max(1, round(height / 10))
    size = max(dot_w, min(dot_h, dot_w * 2))
    columns = (x + round(width * 0.25) - size // 2, x + round(width * 0.75) - size // 2)
    rows = [y + round(height * (2 * row + 1) / 8) - size // 2 for row in range(4)]
    for bit, column, row in ((0x01, 0, 0), (0x02, 0, 1), (0x04, 0, 2), (0x40, 0, 3), (0x08, 1, 0), (0x10, 1, 1),
                             (0x20, 1, 2), (0x80, 1, 3)):
        if bits & bit:
            painter.fillRect(columns[column], rows[row], size, size, colour)
