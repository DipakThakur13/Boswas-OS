"""The marks on a snip, the crop and the undo history (standard library only).

Every mark is a vector in the pixels of the original picture: a pen or
highlighter stroke (a list of points), an arrow (tail and head), a
rectangle (two corners) or a text (its top left corner, the text and the
size of its box). Marks are never burnt into the picture while editing;
``picture.render`` draws them at the picture's full resolution on export.

A ``State`` is immutable: the marks and the crop (a rectangle of the
original picture, or None for all of it). Every edit makes a new state and
``History`` keeps the list for undo and redo; a crop is an edit like any
other, so it is undone the same way.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Iterable

KINDS = ("pen", "highlighter", "arrow", "rectangle", "text")
SIZES = ("Thin", "Medium", "Thick")
# Line widths (for text: the height of the letters) in picture pixels, by size.
WIDTHS = {
    "pen": (2.0, 4.0, 8.0),
    "highlighter": (12.0, 20.0, 32.0),
    "arrow": (2.0, 4.0, 8.0),
    "rectangle": (2.0, 4.0, 8.0),
    "text": (16.0, 24.0, 40.0),
}
HIGHLIGHTER_OPACITY = 0.4
MAX_POINTS = 20_000               # points of one stroke
MAX_ITEMS = 5_000                 # marks on one picture
MAX_TEXT = 500                    # characters of one text
MAX_WIDTH = 400.0
MAX_COORDINATE = 1_000_000.0
MIN_CROP = 8                      # the smallest crop, in picture pixels
HISTORY_LIMIT = 200
HANDLES = ("tl", "t", "tr", "r", "br", "b", "bl", "l")
_COLOUR = re.compile(r"#[0-9A-Fa-f]{6}")

Point = tuple[float, float]


@dataclass(frozen=True)
class Rect:
    """A rectangle; ``width`` and ``height`` are never negative."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @classmethod
    def from_points(cls, a: Point, b: Point) -> "Rect":
        return cls(min(a[0], b[0]), min(a[1], b[1]), abs(a[0] - b[0]), abs(a[1] - b[1]))

    @classmethod
    def from_edges(cls, left: float, top: float, right: float, bottom: float) -> "Rect":
        return cls(left, top, max(0.0, right - left), max(0.0, bottom - top))

    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.right and self.y <= y <= self.bottom

    def intersects(self, other: "Rect") -> bool:
        return self.x < other.right and other.x < self.right and self.y < other.bottom and other.y < self.bottom

    def expanded(self, amount: float) -> "Rect":
        return Rect(self.x - amount, self.y - amount, self.width + 2 * amount, self.height + 2 * amount)

    def inside(self, bounds: "Rect") -> bool:
        return (bounds.x <= self.x and bounds.y <= self.y and self.right <= bounds.right
                and self.bottom <= bounds.bottom)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
        and abs(value) <= MAX_COORDINATE


def _point(value: object) -> Point:
    if not (isinstance(value, (tuple, list)) and len(value) == 2 and all(_finite(v) for v in value)):
        raise ValueError(f"not a point: {value!r}")
    return (float(value[0]), float(value[1]))


def clean_text(text: str) -> str:
    """Text for a text mark: one line, no control characters, at most ``MAX_TEXT`` characters."""
    text = "".join(character if character.isprintable() else " " for character in text)
    return " ".join(text.split())[:MAX_TEXT]


@dataclass(frozen=True)
class Item:
    """One mark. Use the functions ``stroke``, ``shape`` and ``text_item`` to make one (they check it)."""

    kind: str
    colour: str                                 # "#RRGGBB"
    width: float                                # line width; for text the height of the letters
    points: tuple[Point, ...]
    text: str = ""
    box: tuple[float, float] = (0.0, 0.0)       # text: the size of the text, measured when it was placed

    def bounds(self) -> Rect:
        """What the mark covers, its line width and arrow head included."""
        if self.kind == "text":
            x, y = self.points[0]
            return Rect(x, y, self.box[0], self.box[1])
        points = list(self.points)
        if self.kind == "arrow":
            points += list(arrow_head(self.points[0], self.points[1], self.width))
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        half = self.width / 2
        return Rect.from_edges(min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half)

    def segments(self) -> list[tuple[Point, Point]]:
        """The lines the mark is drawn with (for the eraser)."""
        if self.kind in ("pen", "highlighter"):
            if len(self.points) == 1:
                return [(self.points[0], self.points[0])]
            return list(zip(self.points, self.points[1:]))
        if self.kind == "arrow":
            tail, head = self.points
            left, tip, right = arrow_head(tail, head, self.width)
            return [(tail, tip), (left, tip), (tip, right), (right, left)]
        if self.kind == "rectangle":
            box = Rect.from_points(*self.points)
            corners = [(box.x, box.y), (box.right, box.y), (box.right, box.bottom), (box.x, box.bottom)]
            return list(zip(corners, corners[1:] + corners[:1]))
        return []

    def hit(self, x: float, y: float, radius: float) -> bool:
        """Whether a circle of ``radius`` around (x, y) touches the mark."""
        if self.kind == "text":
            return self.bounds().expanded(radius).contains(x, y)
        reach = radius + self.width / 2
        if not self.bounds().expanded(radius).contains(x, y):
            return False
        return any(distance_to_segment((x, y), a, b) <= reach for a, b in self.segments())

    def translated(self, dx: float, dy: float) -> "Item":
        return replace(self, points=tuple((x + dx, y + dy) for x, y in self.points))


def _check(kind: str, colour: str, width: float) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown kind of mark {kind!r}")
    if not isinstance(colour, str) or not _COLOUR.fullmatch(colour):
        raise ValueError(f"not a colour: {colour!r}")
    if not _finite(width) or not 0 < width <= MAX_WIDTH:
        raise ValueError(f"not a width: {width!r}")


def stroke(kind: str, colour: str, width: float, points: Iterable[Point]) -> Item:
    """A pen or highlighter stroke through ``points`` (at most ``MAX_POINTS`` are kept)."""
    if kind not in ("pen", "highlighter"):
        raise ValueError(f"not a stroke: {kind!r}")
    _check(kind, colour, width)
    kept = []
    for point in points:
        if len(kept) >= MAX_POINTS:
            break
        kept.append(_point(point))
    if not kept:
        raise ValueError("a stroke needs a point")
    return Item(kind, colour.upper(), float(width), tuple(kept))


def shape(kind: str, colour: str, width: float, start: Point, end: Point) -> Item:
    """An arrow from ``start`` to ``end`` or a rectangle with these corners."""
    if kind not in ("arrow", "rectangle"):
        raise ValueError(f"not a shape: {kind!r}")
    _check(kind, colour, width)
    return Item(kind, colour.upper(), float(width), (_point(start), _point(end)))


def text_item(colour: str, size: float, position: Point, text: str, box: tuple[float, float]) -> Item:
    """A text at ``position`` (its top left corner); ``box`` is its measured width and height."""
    _check("text", colour, size)
    text = clean_text(text)
    if not text:
        raise ValueError("a text mark needs text")
    width, height = _point(box)
    return Item("text", colour.upper(), float(size), (_point(position),), text, (max(0.0, width), max(0.0, height)))


def distance_to_segment(point: Point, a: Point, b: Point) -> float:
    (px, py), (ax, ay), (bx, by) = point, a, b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def arrow_head(tail: Point, head: Point, width: float) -> tuple[Point, Point, Point]:
    """The triangle of an arrow's head: (left corner, tip, right corner). The tip is ``head``."""
    dx, dy = head[0] - tail[0], head[1] - tail[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return head, head, head
    ux, uy = dx / length, dy / length
    size = min(max(10.0, width * 4.0), length * 0.9)
    half = size * 0.6
    base = (head[0] - ux * size, head[1] - uy * size)
    return (base[0] - uy * half, base[1] + ux * half), head, (base[0] + uy * half, base[1] - ux * half)


def arrow_shaft_end(tail: Point, head: Point, width: float) -> Point:
    """Where an arrow's line stops: inside its head, so that the line's end does not show beside the tip."""
    left, tip, right = arrow_head(tail, head, width)
    base = ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)
    return ((base[0] + tip[0]) / 2, (base[1] + tip[1]) / 2)


def constrained(kind: str, start: Point, end: Point) -> Point:
    """``end`` with Shift held: a square for a rectangle, a multiple of 45 degrees for an arrow."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    if kind == "rectangle":
        side = max(abs(dx), abs(dy))
        return (start[0] + math.copysign(side, dx or 1), start[1] + math.copysign(side, dy or 1))
    length = math.hypot(dx, dy)
    angle = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
    return (start[0] + length * math.cos(angle), start[1] + length * math.sin(angle))


# --- the state of an edit ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class State:
    items: tuple[Item, ...] = ()
    crop: Rect | None = None            # in pixels of the original picture; None: all of it

    def visible(self, width: int, height: int) -> Rect:
        """The part of a ``width`` x ``height`` picture that is shown and exported."""
        return self.crop if self.crop is not None else Rect(0, 0, width, height)

    def is_full(self) -> bool:
        return len(self.items) >= MAX_ITEMS

    def adding(self, item: Item) -> "State":
        if self.is_full():
            return self
        return replace(self, items=self.items + (item,))

    def erasing(self, indexes: Iterable[int]) -> "State":
        gone = set(indexes)
        return replace(self, items=tuple(item for index, item in enumerate(self.items) if index not in gone))

    def cleared(self) -> "State":
        return replace(self, items=())

    def cropped(self, rect: Rect) -> "State":
        """Crop to ``rect`` (original pixels). Marks that lie wholly outside the new picture are dropped."""
        items = tuple(item for item in self.items if item.bounds().expanded(1).intersects(rect))
        return State(items, rect)

    def hits(self, x: float, y: float, radius: float) -> list[int]:
        """The indexes of the marks a circle of ``radius`` around (x, y) touches."""
        return [index for index, item in enumerate(self.items) if item.hit(x, y, radius)]


class History:
    """The states of an edit, for undo and redo, and which one was saved last."""

    def __init__(self, state: State | None = None, limit: int = HISTORY_LIMIT) -> None:
        self.limit = max(2, limit)
        self.reset(state)

    def reset(self, state: State | None = None) -> None:
        self._states = [state if state is not None else State()]
        self._index = 0
        self._saved: int | None = 0

    @property
    def state(self) -> State:
        return self._states[self._index]

    def push(self, state: State) -> None:
        """A new edit: it becomes the current state and what could be redone is gone."""
        if state == self.state:
            return
        del self._states[self._index + 1:]
        if self._saved is not None and self._saved > self._index:
            self._saved = None
        self._states.append(state)
        self._index += 1
        overflow = len(self._states) - self.limit
        if overflow > 0:
            del self._states[:overflow]
            self._index -= overflow
            if self._saved is not None:
                self._saved = self._saved - overflow if self._saved >= overflow else None

    def can_undo(self) -> bool:
        return self._index > 0

    def can_redo(self) -> bool:
        return self._index < len(self._states) - 1

    def undo(self) -> bool:
        if not self.can_undo():
            return False
        self._index -= 1
        return True

    def redo(self) -> bool:
        if not self.can_redo():
            return False
        self._index += 1
        return True

    def is_modified(self) -> bool:
        """Whether the current state differs from the one saved (or opened) last."""
        return self._saved != self._index

    def mark_saved(self) -> None:
        self._saved = self._index

    def __len__(self) -> int:
        return len(self._states)


# --- crop arithmetic ---------------------------------------------------------------------------------------

def handle_at(rect: Rect, x: float, y: float, reach: float) -> str | None:
    """Which handle of a crop frame is at (x, y): a corner or side ("tl", "t", ..., "l"), "move" inside the
    frame, None outside. ``reach`` is how far from a side still counts as on it."""
    if not rect.expanded(reach).contains(x, y):
        return None
    near_left, near_right = abs(x - rect.x) <= reach, abs(x - rect.right) <= reach
    near_top, near_bottom = abs(y - rect.y) <= reach, abs(y - rect.bottom) <= reach
    if near_left and near_right:                  # a frame narrower than two reaches: the nearer side wins
        near_left, near_right = abs(x - rect.x) <= abs(x - rect.right), abs(x - rect.x) > abs(x - rect.right)
    if near_top and near_bottom:
        near_top, near_bottom = abs(y - rect.y) <= abs(y - rect.bottom), abs(y - rect.y) > abs(y - rect.bottom)
    vertical = "t" if near_top else "b" if near_bottom else ""
    horizontal = "l" if near_left else "r" if near_right else ""
    if vertical or horizontal:
        return vertical + horizontal
    return "move"


def drag_crop(start: Rect, handle: str, dx: float, dy: float, bounds: Rect, minimum: float = MIN_CROP) -> Rect:
    """The crop frame after ``handle`` of ``start`` was dragged by (dx, dy), kept inside ``bounds`` and at
    least ``minimum`` wide and high (or as large as ``bounds`` when that is smaller)."""
    least_w, least_h = min(minimum, bounds.width), min(minimum, bounds.height)
    if handle == "move":
        x = max(bounds.x, min(bounds.right - start.width, start.x + dx))
        y = max(bounds.y, min(bounds.bottom - start.height, start.y + dy))
        return Rect(x, y, start.width, start.height)
    left, top, right, bottom = start.x, start.y, start.right, start.bottom
    if "l" in handle:
        left = max(bounds.x, min(right - least_w, left + dx))
    if "r" in handle:
        right = min(bounds.right, max(left + least_w, right + dx))
    if "t" in handle:
        top = max(bounds.y, min(bottom - least_h, top + dy))
    if "b" in handle:
        bottom = min(bounds.bottom, max(top + least_h, bottom + dy))
    return Rect.from_edges(left, top, right, bottom)


def crop_between(a: Point, b: Point, bounds: Rect) -> Rect:
    """A new crop frame dragged from ``a`` to ``b``, kept inside ``bounds``."""
    def clamp(point: Point) -> Point:
        return (max(bounds.x, min(bounds.right, point[0])), max(bounds.y, min(bounds.bottom, point[1])))
    return Rect.from_points(clamp(a), clamp(b))


def snapped(rect: Rect, bounds: Rect) -> Rect | None:
    """``rect`` on whole pixels inside ``bounds``; None when nothing of it is left or it is all of ``bounds``."""
    left = max(math.ceil(bounds.x), round(rect.x))
    top = max(math.ceil(bounds.y), round(rect.y))
    right = min(math.floor(bounds.right), round(rect.right))
    bottom = min(math.floor(bounds.bottom), round(rect.bottom))
    if right - left < 1 or bottom - top < 1:
        return None
    result = Rect(left, top, right - left, bottom - top)
    if (result.x, result.y, result.width, result.height) == (bounds.x, bounds.y, bounds.width, bounds.height):
        return None
    return result
