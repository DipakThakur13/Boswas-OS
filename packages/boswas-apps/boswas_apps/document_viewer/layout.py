"""Where the pages of a document sit on the screen, and the arithmetic of zooming. No Qt.

Lengths of pages are in points (1/72 inch), everything on the screen in
logical pixels. At 100 % one point is 96/72 pixels, the size the page has
on paper on a 96 dpi screen ("actual size").

``build`` lays pages out in rows: one page per row, or two side by side,
optionally with the first page alone as a cover. Rows are centred, with a
gap between pages and a margin around everything. The result answers the
two questions the view asks all the time: which pages are between these two
heights, and which page is at this height.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

PX_PER_POINT = 96.0 / 72.0
ZOOM_MIN, ZOOM_MAX = 0.1, 8.0
ZOOM_STEPS = (0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0)
GAP = 12            # between pages
MARGIN = 16         # around the whole document
FIT_WIDTH, FIT_PAGE, CUSTOM = "fit-width", "fit-page", "custom"


def clamp_zoom(zoom: float) -> float:
    return max(ZOOM_MIN, min(ZOOM_MAX, float(zoom)))


def zoom_in(zoom: float) -> float:
    """The next step above ``zoom``."""
    for step in ZOOM_STEPS:
        if step > zoom * 1.001:
            return step
    return ZOOM_MAX


def zoom_out(zoom: float) -> float:
    for step in reversed(ZOOM_STEPS):
        if step < zoom * 0.999:
            return step
    return ZOOM_MIN


def rotated(width: float, height: float, rotation: int) -> tuple[float, float]:
    """The size of a page turned by ``rotation`` quarter turns."""
    return (height, width) if rotation % 2 else (width, height)


def rows_of(page_count: int, columns: int = 1, cover: bool = False) -> list[list[int]]:
    """Which pages share a row: ``[[0], [1, 2], [3, 4]]`` for two columns with a cover page."""
    if page_count <= 0:
        return []
    if columns <= 1:
        return [[page] for page in range(page_count)]
    rows: list[list[int]] = []
    start = 0
    if cover:
        rows.append([0])
        start = 1
    for first in range(start, page_count, 2):
        rows.append(list(range(first, min(first + 2, page_count))))
    return rows


def row_of_page(page: int, page_count: int, columns: int = 1, cover: bool = False) -> int:
    """The index of the row that holds ``page``."""
    if columns <= 1 or page_count <= 0:
        return max(0, min(page, page_count - 1))
    if cover:
        return 0 if page <= 0 else (page - 1) // 2 + 1
    return page // 2


@dataclass
class Layout:
    rects: dict[int, tuple[float, float, float, float]] = field(default_factory=dict)    # page → x, y, w, h
    rows: list[list[int]] = field(default_factory=list)
    row_tops: list[float] = field(default_factory=list)
    row_heights: list[float] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0

    def rect(self, page: int) -> tuple[float, float, float, float] | None:
        return self.rects.get(page)

    def row_at(self, y: float) -> int:
        """The row at height ``y`` (the nearest one above when ``y`` is in a gap); -1 without rows."""
        if not self.row_tops:
            return -1
        return max(0, bisect.bisect_right(self.row_tops, y) - 1)

    def pages_between(self, top: float, bottom: float) -> list[int]:
        """The pages that are at least partly between the heights ``top`` and ``bottom``."""
        if not self.row_tops or bottom <= top:
            return []
        first = self.row_at(top)
        pages: list[int] = []
        for index in range(first, len(self.rows)):
            if self.row_tops[index] >= bottom:
                break
            if self.row_tops[index] + self.row_heights[index] > top:
                pages.extend(self.rows[index])
        return pages

    def page_at(self, x: float, y: float) -> int:
        """The page under the point, or the nearest page of the row at that height; -1 without pages."""
        row = self.row_at(y)
        if row < 0:
            return -1
        best, distance = -1, float("inf")
        for page in self.rows[row]:
            left, _top, width, _height = self.rects[page]
            gap = 0.0 if left <= x <= left + width else min(abs(x - left), abs(x - left - width))
            if gap < distance:
                best, distance = page, gap
        return best

    def main_page(self, top: float, bottom: float) -> int:
        """The page that counts as "the current page" for a viewport from ``top`` to ``bottom``.

        The first page of the row that covers the middle of the viewport, or
        the row with the most of it when the middle is in a gap.
        """
        if not self.row_tops:
            return -1
        row = self.row_at((top + bottom) / 2)
        return self.rows[row][0]


def build(sizes: list[tuple[float, float]], zoom: float, rotation: int = 0, columns: int = 1, cover: bool = False,
          viewport_width: float = 0.0, only_row: int | None = None, viewport_height: float = 0.0) -> Layout:
    """Lay out pages of ``sizes`` (points) at ``zoom``.

    ``only_row`` lays out that one row alone (single-page mode): it is then
    centred in the height of the viewport too.
    """
    layout = Layout()
    all_rows = rows_of(len(sizes), columns, cover)
    if only_row is not None:
        all_rows = [all_rows[only_row]] if 0 <= only_row < len(all_rows) else []
    scale = zoom * PX_PER_POINT
    measured = []
    widest = 0.0
    for row in all_rows:
        # Whole pixels: a page image is then drawn one to one, which is what makes scrolling cheap.
        dimensions = [tuple(max(1.0, float(round(side))) for side in
                            rotated(sizes[page][0] * scale, sizes[page][1] * scale, rotation)) for page in row]
        width = sum(w for w, _h in dimensions) + GAP * (len(row) - 1)
        height = max(h for _w, h in dimensions)
        measured.append((row, dimensions, width, height))
        widest = max(widest, width)
    layout.width = max(widest + 2 * MARGIN, viewport_width)
    y = float(MARGIN)
    if only_row is not None and measured:
        y = max(float(MARGIN), (viewport_height - measured[0][3]) / 2)
    for row, dimensions, width, height in measured:
        x = float(round((layout.width - width) / 2))
        y = float(round(y))
        for page, (page_width, page_height) in zip(row, dimensions):
            layout.rects[page] = (x, y + float(round((height - page_height) / 2)), page_width, page_height)
            x += page_width + GAP
        layout.rows.append(row)
        layout.row_tops.append(y)
        layout.row_heights.append(height)
        y += height + GAP
    layout.height = (y - GAP + MARGIN) if measured else 0.0
    if only_row is not None:
        layout.height = max(layout.height, viewport_height)
    return layout


def fit_zoom(mode: str, sizes: list[tuple[float, float]], rotation: int, columns: int, cover: bool,
             viewport_width: float, viewport_height: float, row: int | None = None) -> float:
    """The zoom at which a row fills the width (``FIT_WIDTH``) or fits whole (``FIT_PAGE``) in the viewport.

    ``row`` is the row to fit (the current one); without it the widest and
    tallest row of the document decides, so nothing is ever cut off.
    """
    all_rows = rows_of(len(sizes), columns, cover)
    if not all_rows or viewport_width <= 0 or viewport_height <= 0:
        return 1.0
    chosen = [all_rows[row]] if row is not None and 0 <= row < len(all_rows) else all_rows
    best = ZOOM_MAX
    for pages in chosen:
        dimensions = [rotated(sizes[page][0], sizes[page][1], rotation) for page in pages]
        width = sum(w for w, _h in dimensions) * PX_PER_POINT
        height = max(h for _w, h in dimensions) * PX_PER_POINT
        if width <= 0 or height <= 0:
            continue
        room = viewport_width - 2 * MARGIN - GAP * (len(pages) - 1)
        zoom = room / width
        if mode == FIT_PAGE:
            zoom = min(zoom, (viewport_height - 2 * MARGIN) / height)
        best = min(best, zoom)
    return clamp_zoom(best)


def to_page(x: float, y: float, rect: tuple[float, float, float, float], size: tuple[float, float],
            rotation: int) -> tuple[float, float]:
    """A point of the layout as a point on the unrotated page, in points (origin top left)."""
    left, top, width, height = rect
    u = (x - left) / width if width else 0.0
    v = (y - top) / height if height else 0.0
    rotation %= 4
    if rotation == 1:
        u, v = v, 1 - u
    elif rotation == 2:
        u, v = 1 - u, 1 - v
    elif rotation == 3:
        u, v = 1 - v, u
    return u * size[0], v * size[1]


def from_page(px: float, py: float, rect: tuple[float, float, float, float], size: tuple[float, float],
              rotation: int) -> tuple[float, float]:
    """A point on the unrotated page (points) as a point of the layout."""
    left, top, width, height = rect
    u = px / size[0] if size[0] else 0.0
    v = py / size[1] if size[1] else 0.0
    rotation %= 4
    if rotation == 1:
        u, v = 1 - v, u
    elif rotation == 2:
        u, v = 1 - u, 1 - v
    elif rotation == 3:
        u, v = v, 1 - u
    return left + u * width, top + v * height


def rect_from_page(box: tuple[float, float, float, float], rect: tuple[float, float, float, float],
                   size: tuple[float, float], rotation: int) -> tuple[float, float, float, float]:
    """A rectangle on the unrotated page (x, y, w, h in points) as a rectangle of the layout."""
    x1, y1 = from_page(box[0], box[1], rect, size, rotation)
    x2, y2 = from_page(box[0] + box[2], box[1] + box[3], rect, size, rotation)
    return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)


_RANGE = re.compile(r"^\s*(\d+)\s*(?:[-–]\s*(\d+)\s*)?$")


def parse_pages(text: str, page_count: int) -> list[int] | None:
    """``"1-3, 7"`` as page indexes ``[0, 1, 2, 6]``; None when the text is not a list of pages of this document."""
    if not text.strip():
        return None
    pages: list[int] = []
    for part in text.split(","):
        match = _RANGE.match(part)
        if not match:
            return None
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if first < 1 or last < first or last > page_count:
            return None
        pages.extend(range(first - 1, last))
    return pages


def zoom_text(zoom: float) -> str:
    return f"{round(zoom * 100)} %"


def parse_zoom(text: str) -> float | None:
    """``"150 %"``, ``"150"`` or ``"1,5"`` typed into the zoom box as a zoom factor; None when it is no number."""
    cleaned = text.strip().rstrip("%").strip().replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value != value or value <= 0:
        return None
    return clamp_zoom(value / 100.0)


class History:
    """Where the reader has been: the pages links were followed from, for Back and Forward."""

    def __init__(self, limit: int = 100) -> None:
        self._back: list[tuple] = []
        self._forward: list[tuple] = []
        self._limit = limit

    def push(self, position: tuple) -> None:
        """Remember ``position`` (where the reader is) before jumping somewhere else."""
        if not self._back or self._back[-1] != position:
            self._back.append(position)
            del self._back[:-self._limit]
        self._forward.clear()

    def can_back(self) -> bool:
        return bool(self._back)

    def can_forward(self) -> bool:
        return bool(self._forward)

    def back(self, current: tuple) -> tuple | None:
        if not self._back:
            return None
        self._forward.append(current)
        return self._back.pop()

    def forward(self, current: tuple) -> tuple | None:
        if not self._forward:
            return None
        self._back.append(current)
        return self._forward.pop()

    def clear(self) -> None:
        self._back.clear()
        self._forward.clear()
