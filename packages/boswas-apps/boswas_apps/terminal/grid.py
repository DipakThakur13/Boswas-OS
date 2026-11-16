"""The text in the terminal's lines: selections, words, addresses and search (no Qt).

Lines are addressed as the emulator's ``display_line`` does: an index into
scrollback and screen together, and a column counted in cells. A row that
ended by wrapping continues on the next row; such rows form one logical
line, and everything here that reads text (copying, double-click, address
detection, search) works on logical lines, so a long command or address
that wraps is one piece.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

Position = tuple[int, int]          # (line, column)
Span = tuple[int, int, int]         # (line, first column, column after the last)

WORD_CHARACTERS = "-./?%&#_=+@~"
MAX_LOGICAL_ROWS = 256
MAX_MATCHES = 50000

# An address ends at white space, quotes, angle brackets and control characters.
_URL = re.compile(r"(?:https?://|ftp://|file://|mailto:)[^\s<>\"'`\x00-\x20\x7f-\x9f─-▟]+", re.IGNORECASE)
_TRAILING = ".,;:!?'\"*"
_PAIRS = {")": "(", "]": "[", "}": "{", ">": "<"}
MAX_URL = 2083


def cells_text(cells: Sequence[str] | str) -> str:
    """The characters of a row of cells."""
    return cells if type(cells) is str else "".join(cells)


def _starts(cells: Sequence[str] | str) -> list[int] | None:
    """For rows whose cells are not one character each: the text offset at which each cell starts."""
    if type(cells) is str:
        return None
    starts = []
    offset = 0
    simple = True
    for cell in cells:
        starts.append(offset)
        size = len(cell)
        offset += size
        if size != 1:
            simple = False
    return None if simple else starts


def column_at(cells: Sequence[str] | str, offset: int) -> int:
    """The cell that holds the character at text offset ``offset`` of a row."""
    starts = _starts(cells)
    if starts is None:
        return offset
    return max(0, bisect_right(starts, offset) - 1)


def offset_at(cells: Sequence[str] | str, column: int) -> int:
    """The text offset of the first character of cell ``column`` (the length of the text beyond the row)."""
    if type(cells) is str:
        return min(column, len(cells))
    return sum(len(cell) for cell in cells[:max(0, column)])


class LogicalLine:
    """A line of text as the program wrote it: one row, or several rows joined where they wrapped."""

    __slots__ = ("first", "rows", "offsets", "text")

    def __init__(self, first: int, rows: list) -> None:
        self.first = first                      # index of the first row
        self.rows = rows                        # the cells of each row
        self.offsets: list[int] = []            # text offset at which each row starts
        parts = []
        offset = 0
        for cells in rows:
            self.offsets.append(offset)
            text = cells_text(cells)
            parts.append(text)
            offset += len(text)
        self.text = "".join(parts)

    def offset(self, line: int, column: int) -> int:
        """Text offset of a cell (clamped to the text of its row)."""
        row = max(0, min(line - self.first, len(self.rows) - 1))
        return self.offsets[row] + offset_at(self.rows[row], column)

    def position(self, offset: int) -> Position:
        """The cell of a text offset."""
        row = max(0, bisect_right(self.offsets, offset) - 1)
        return self.first + row, column_at(self.rows[row], offset - self.offsets[row])

    def spans(self, start: int, end: int) -> list[Span]:
        """The cells of the text range [start, end) as one span a row."""
        if end <= start:
            return []
        out: list[Span] = []
        first_row = max(0, bisect_right(self.offsets, start) - 1)
        last_row = max(0, bisect_right(self.offsets, end - 1) - 1)
        for row in range(first_row, last_row + 1):
            cells = self.rows[row]
            base = self.offsets[row]
            row_length = len(cells_text(cells)) if row == len(self.rows) - 1 else self.offsets[row + 1] - base
            low = max(start - base, 0)
            high = min(end - base, row_length)
            if high <= low:
                continue
            first_column = column_at(cells, low)
            last_column = column_at(cells, high - 1)
            if last_column + 1 < len(cells) and cells[last_column + 1] == "":
                last_column += 1                # the second half of a wide character
            out.append((self.first + row, first_column, last_column + 1))
        return out


def logical_line(term, line: int) -> LogicalLine:
    """The logical line that row ``line`` of ``term`` belongs to (at most MAX_LOGICAL_ROWS rows of it)."""
    total = term.total_lines()
    line = max(0, min(line, total - 1))
    first = line
    while first > 0 and line - first < MAX_LOGICAL_ROWS and term.display_line(first - 1)[2]:
        first -= 1
    rows = []
    index = first
    while index < total:
        cells, _runs, wrapped = term.display_line(index)
        rows.append(cells)
        index += 1
        if not wrapped or len(rows) >= 2 * MAX_LOGICAL_ROWS:
            break
    return LogicalLine(first, rows)


# --- words ------------------------------------------------------------------------------------------------

def _kind(char: str) -> int:
    if char.isalnum() or char in WORD_CHARACTERS:
        return 1
    if char.isspace():
        return 2
    return 0


def word_range(text: str, offset: int) -> tuple[int, int]:
    """The word around ``offset`` in ``text`` as [start, end): letters, digits and the characters of paths
    and options; a run of blanks; or a single other character."""
    if not text:
        return 0, 0
    offset = max(0, min(offset, len(text) - 1))
    kind = _kind(text[offset])
    if kind == 0:
        return offset, offset + 1
    start = offset
    while start > 0 and _kind(text[start - 1]) == kind:
        start -= 1
    end = offset + 1
    while end < len(text) and _kind(text[end]) == kind:
        end += 1
    return start, end


def word_at(term, line: int, column: int) -> tuple[Position, Position]:
    """The first and the last cell of the word at a cell."""
    logical = logical_line(term, line)
    row = logical.rows[max(0, min(line - logical.first, len(logical.rows) - 1))]
    if column >= len(row) or not cells_text(row[column:]).strip(" "):
        # beyond the text of the row: the blanks up to the end of the row
        text_end = len(cells_text(row).rstrip(" ")) if type(row) is str else _used_cells(row)
        return (line, min(text_end, column)), (line, max(column, text_end))
    start, end = word_range(logical.text, logical.offset(line, column))
    spans = logical.spans(start, end)
    if not spans:
        return (line, column), (line, column)
    return (spans[0][0], spans[0][1]), (spans[-1][0], spans[-1][2] - 1)


def _used_cells(cells: Sequence[str]) -> int:
    used = len(cells)
    while used and cells[used - 1] == " ":
        used -= 1
    return used


def line_range(term, line: int) -> tuple[int, int]:
    """First and last row of the logical line a row belongs to."""
    logical = logical_line(term, line)
    return logical.first, logical.first + len(logical.rows) - 1


# --- addresses --------------------------------------------------------------------------------------------

def find_urls(text: str) -> list[tuple[int, int, str]]:
    """Web, mail and file addresses in ``text`` as (start, end, address).

    Punctuation that ends a sentence is not part of the address, nor is a
    closing bracket whose opening bracket is outside it.
    """
    out = []
    for match in _URL.finditer(text):
        url = match.group()
        while url:
            last = url[-1]
            if last in _TRAILING:
                url = url[:-1]
            elif last in _PAIRS and url.count(last) > url.count(_PAIRS[last]):
                url = url[:-1]
            else:
                break
        scheme_end = url.find(":") + 1
        rest = url[scheme_end:].lstrip("/")
        if rest and len(url) <= MAX_URL:
            out.append((match.start(), match.start() + len(url), url))
    return out


def url_at(term, line: int, column: int) -> tuple[str, list[Span]] | None:
    """The address written at a cell and the cells it covers, or None."""
    if not 0 <= line < term.total_lines() or column < 0:
        return None
    logical = logical_line(term, line)
    row = logical.rows[max(0, min(line - logical.first, len(logical.rows) - 1))]
    if column >= len(row):
        return None
    if "://" not in logical.text and "mailto:" not in logical.text.lower():
        return None
    offset = logical.offset(line, column)
    for start, end, url in find_urls(logical.text):
        if start <= offset < end:
            return url, logical.spans(start, end)
    return None


# --- selections -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Selection:
    """Selected cells: from ``start`` to ``end`` in reading order, both included; or the rectangle between
    them (``block``)."""

    start: Position
    end: Position
    block: bool = False

    @staticmethod
    def between(a: Position, b: Position, block: bool = False) -> "Selection":
        return Selection(min(a, b), max(a, b), block)

    def span(self, line: int, cols: int) -> tuple[int, int] | None:
        """The selected columns [first, after last) of a row, or None."""
        (first_line, first_column), (last_line, last_column) = self.start, self.end
        if not first_line <= line <= last_line:
            return None
        if self.block:
            low, high = min(first_column, last_column), max(first_column, last_column)
            return low, min(high + 1, cols)
        low = first_column if line == first_line else 0
        high = last_column + 1 if line == last_line else cols
        return (low, min(high, cols)) if high > low else None

    def contains(self, line: int, column: int) -> bool:
        span = self.span(line, 1 << 30)
        return span is not None and span[0] <= column < span[1]


def selection_text(term, selection: Selection) -> str:
    """The text of a selection: rows joined where they wrapped, blanks at line ends removed."""
    (first_line, first_column), (last_line, last_column) = selection.start, selection.end
    total = term.total_lines()
    first_line = max(0, first_line)
    last_line = min(last_line, total - 1)
    out: list[str] = []
    low, high = min(first_column, last_column), max(first_column, last_column) + 1
    for line, (cells, _runs, wrapped) in enumerate(term.iter_lines(first_line, last_line + 1), first_line):
        last = line == last_line
        if selection.block:
            start, end = low, high
        else:
            start = first_column if line == first_line else 0
            end = last_column + 1 if last else len(cells)
        start = max(0, min(start, len(cells)))
        if 0 < start < len(cells) and cells[start] == "":
            start -= 1                          # the selection starts in the middle of a wide character
        piece = cells_text(cells[start:end])
        if selection.block or not wrapped or last and end < len(cells):
            out.append(piece.rstrip(" "))
            if not last:
                out.append("\n")
        else:
            out.append(piece)
    return "".join(out)


# --- search -----------------------------------------------------------------------------------------------

def search_pattern(needle: str, match_case: bool) -> re.Pattern:
    """A pattern that finds ``needle`` literally."""
    return re.compile(re.escape(needle), 0 if match_case else re.IGNORECASE)


def search(entries: Iterable[tuple], pattern: re.Pattern, first: int = 0) -> Iterator[list[list[Span]]]:
    """Find ``pattern`` in lines (``display_line`` tuples, the first of them having index ``first``).

    A generator that yields once a logical line: the matches found in it,
    each match a list of spans (more than one when the match runs across a
    wrap). The caller can stop between lines and continue later.
    """
    rows: list = []
    start = first
    index = first
    for cells, _runs, wrapped in entries:
        rows.append(cells)
        index += 1
        if wrapped and len(rows) < 2 * MAX_LOGICAL_ROWS:
            continue
        if len(rows) == 1 and type(cells) is str:
            yield [[(start, match.start(), match.end())] for match in pattern.finditer(cells) if match.end() > match.start()]
        else:
            logical = LogicalLine(start, rows)
            yield [logical.spans(match.start(), match.end()) for match in pattern.finditer(logical.text)
                   if match.end() > match.start()]
        rows = []
        start = index
    if rows:
        logical = LogicalLine(start, rows)
        yield [logical.spans(match.start(), match.end()) for match in pattern.finditer(logical.text)
               if match.end() > match.start()]
