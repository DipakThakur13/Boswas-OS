"""The terminal itself: escape sequence parser, screen, scrollback and modes (no Qt).

``Terminal.feed(bytes)`` takes what the child process wrote and updates the
screen; what the terminal answers (device reports, the cursor position)
goes to ``on_reply``. The widget reads the result through ``display_line``,
the cursor attributes and the mode attributes, and repaints the rows named
by ``take_dirty``.

The emulation is that of an xterm with 256 and 24-bit colours: UTF-8 with
wide and combining characters, cursor addressing and editing, scroll
regions, tab stops, the alternate screen, SGR attributes with underline
styles and colours, DEC special graphics, bracketed paste, mouse and focus
reporting modes, window title (OSC 0/2), current directory (OSC 7),
hyperlinks (OSC 8), the clipboard write of OSC 52 behind a permission, and
the status and attribute reports programs ask for.

Everything that arrives is untrusted. Parameter counts, parameter values,
string lengths, combining sequences, the number of distinct attributes and
hyperlinks and the memory of the scrollback are capped; a sequence that is
not understood is dropped whole and never printed.

Cells: a line is two lists of equal length, ``chars`` and ``attrs``. A cell
holds one character with its combining marks; the second cell of a wide
character holds the empty string. An attribute is the tuple
``(foreground, background, flags, underline colour, hyperlink id)``; colours
are 0 (default), ``PALETTE | index`` or ``RGB | 0xRRGGBB``.

Lines that scroll off the top of the main screen go to the scrollback in a
compact form, ``(cells, runs, wrapped)``: ``cells`` is a string when every
cell is one character and a tuple of strings otherwise, ``runs`` is None
when the line has only default attributes and else ``(count, attr, count,
attr, ...)``. ``display_line`` gives screen lines in the same form.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re
import socket
from collections import deque
from itertools import groupby, islice
from typing import Callable, Iterator
from urllib.parse import unquote, urlsplit

from .wcwidth import wcwidth

# --- limits ----------------------------------------------------------------------------------------------
MIN_COLS, MIN_ROWS = 2, 1
MAX_COLS, MAX_ROWS = 1000, 500
MAX_PARAMS = 32                 # parameters of one control sequence that are looked at
MAX_PARAM_CHARS = 256           # characters of the parameter part of a control sequence
MAX_STRING = 1 << 18            # characters kept of an OSC or DCS string
MAX_TITLE = 1024
MAX_URI = 2083
MAX_PATH = 4096
MAX_CLIPBOARD = 1 << 17         # bytes an application may put on the clipboard at once
MAX_CLUSTER = 16                # characters in one cell (base and combining marks)
MAX_LINKS = 4096
MAX_ATTRS = 8192
MAX_TITLE_STACK = 10
MAX_SCROLLBACK = 1_000_000
HISTORY_BYTES = 64 << 20        # estimated memory of the scrollback of one terminal

# --- attributes ------------------------------------------------------------------------------------------
PALETTE = 1 << 24
RGB = 2 << 24
BOLD, DIM, ITALIC, BLINK, INVERSE, INVISIBLE, STRIKE, OVERLINE = 1, 2, 4, 8, 16, 32, 64, 128
UNDERLINE_SHIFT = 8
UNDERLINE_MASK = 7 << UNDERLINE_SHIFT     # 1 single, 2 double, 3 curly, 4 dotted, 5 dashed
DEFAULT_ATTR = (0, 0, 0, 0, 0)

MOUSE_OFF, MOUSE_X10, MOUSE_NORMAL, MOUSE_BUTTON, MOUSE_ANY = 0, 9, 1000, 1002, 1003
ENCODING_DEFAULT, ENCODING_UTF8, ENCODING_SGR, ENCODING_URXVT = 0, 1005, 1006, 1015

_GROUND, _ESC, _ESC_INTERMEDIATE, _CSI, _CSI_IGNORE, _STRING, _STRING_ESC = range(7)
_OSC, _DCS, _IGNORED = "osc", "dcs", "ignored"

_PRINTABLE = re.compile(r"[^\x00-\x1f\x7f-\x9f]+")
_CSI_SEQUENCE = re.compile(r"\x1b\[([<=>?]?)([0-9;:]*)([ -/]?)([@-~])")
_STRING_END = re.compile(r"[\x07\x18\x1a\x1b]")
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_HEX_COLOUR = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{9}|[0-9a-fA-F]{12})$")
_RGB_COLOUR = re.compile(r"^rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})$")

# DEC Special Graphics (ESC ( 0): the line drawing characters of the VT100.
_DEC_GRAPHICS = str.maketrans(
    "_`abcdefghijklmnopqrstuvwxyz{|}~",
    " ◆▒␉␌␍␊°±␤␋┘┐┌└┼⎺⎻"
    "─⎼⎽├┤┴┬│≤≥π≠£·")


_special: re.Pattern | None = None


def _special_pattern() -> re.Pattern:
    """Matches runs of characters that do not take exactly one cell (built on first use from the Unicode data).

    Every character outside the Basic Multilingual Plane is looked at one by
    one; inside it the pattern lists the wide and the zero-width ranges.
    """
    global _special
    if _special is None:
        ranges: list[str] = []
        start = None
        for code in range(0x300, 0x10000):
            odd = wcwidth(chr(code)) != 1
            if odd and start is None:
                start = code
            elif not odd and start is not None:
                ranges.append(f"\\u{start:04x}-\\u{code - 1:04x}")
                start = None
        if start is not None:
            ranges.append(f"\\u{start:04x}-\\uffff")
        _special = re.compile("[" + "".join(ranges) + "\\U00010000-\\U0010ffff]+")
    return _special


class Line:
    """One row of the screen."""

    __slots__ = ("chars", "attrs", "wrapped")

    def __init__(self, cols: int, attr: tuple = DEFAULT_ATTR) -> None:
        self.chars = [" "] * cols
        self.attrs = [attr] * cols
        self.wrapped = False         # the text continues on the next row (the row ended by wrapping)


def compact(line: Line) -> tuple:
    """A screen line in the compact form of the scrollback (see the module text)."""
    chars, attrs, wrapped = line.chars, line.attrs, line.wrapped
    count = len(chars)
    text = "".join(chars)
    simple = len(text) == count
    first = attrs[0] if count else DEFAULT_ATTR
    if attrs.count(first) == count:
        if first == DEFAULT_ATTR:
            if simple:
                return (text if wrapped else text.rstrip(" "), None, wrapped)
            used = count
            if not wrapped:
                while used and chars[used - 1] == " ":
                    used -= 1
            return (tuple(chars[:used]), None, wrapped)
        return (text if simple else tuple(chars), (count, first), wrapped)
    used = count
    if not wrapped:
        if simple:
            # the usual case: nothing but default blanks after the text
            used = len(text.rstrip(" "))
            if attrs[used:].count(DEFAULT_ATTR) != count - used:
                used = count
        while used and chars[used - 1] == " " and attrs[used - 1] == DEFAULT_ATTR:
            used -= 1
    runs: list = []
    for attr, group in groupby(attrs[:used] if used != count else attrs):
        runs.append(len(list(group)))
        runs.append(attr)
    return (text[:used] if simple else tuple(chars[:used]), tuple(runs) or None, wrapped)


def expand(entry: tuple, cols: int) -> Line:
    """A compact line as a screen line of ``cols`` cells."""
    cells, runs, wrapped = entry
    line = Line.__new__(Line)
    chars = list(cells)
    count = len(chars)
    if runs is None:
        attrs = [DEFAULT_ATTR] * count
    else:
        attrs = []
        for index in range(0, len(runs), 2):
            attrs.extend([runs[index + 1]] * runs[index])
    if count < cols:
        chars.extend(" " * (cols - count))
        attrs.extend([DEFAULT_ATTR] * (cols - count))
    elif count > cols:
        del chars[cols:]
        del attrs[cols:]
        if chars and _is_wide(chars[-1]):
            chars[-1] = " "
    line.chars, line.attrs, line.wrapped = chars, attrs, bool(wrapped)
    return line


def expand_attrs(runs: tuple | None, count: int) -> list:
    """The attribute of every cell of a compact line."""
    if runs is None:
        return [DEFAULT_ATTR] * count
    attrs: list = []
    for index in range(0, len(runs), 2):
        attrs.extend([runs[index + 1]] * runs[index])
    if len(attrs) < count:
        attrs.extend([DEFAULT_ATTR] * (count - len(attrs)))
    return attrs


def _is_wide(cell: str) -> bool:
    return cell >= "ᄀ" and wcwidth(cell[0]) == 2


def _entry(cells: list, attrs: list, wrapped: bool) -> tuple:
    line = Line.__new__(Line)
    line.chars, line.attrs, line.wrapped = cells, attrs, wrapped
    return compact(line) if cells else ("", None, wrapped)


def _entry_cost(entry: tuple) -> int:
    cells, runs, _wrapped = entry
    cost = 120 + (len(cells) if type(cells) is str else 64 * len(cells))
    if runs is not None:
        cost += 56 * len(runs)
    return cost


def reflow(entries: list, cols: int, marks: list[tuple[int, int]]) -> tuple[list, list[tuple[int, int]]]:
    """Wrap compact lines again for a width of ``cols`` cells.

    Rows that were joined by wrapping are joined and cut at the new width; a
    row that fits and was not wrapped is kept as it is. ``marks`` are
    positions ``(row, column)`` in ``entries`` (the cursor, the first row of
    the screen); their new positions are returned with the new rows.
    """
    out: list = []
    wanted: dict[int, list[int]] = {}
    for slot, (row, _column) in enumerate(marks):
        wanted.setdefault(row, []).append(slot)
    moved = list(marks)
    index, count = 0, len(entries)
    while index < count:
        entry = entries[index]
        cells, runs, wrapped = entry
        if not wrapped and len(cells) <= cols:
            if index in wanted:
                for slot in wanted[index]:
                    moved[slot] = (len(out), min(marks[slot][1], cols - 1))
            out.append(entry)
            index += 1
            continue
        # a logical line: this row and the rows it wrapped onto
        all_cells: list = []
        all_attrs: list = []
        offsets: list[tuple[int, int]] = []        # (mark slot, offset in the logical line)
        while True:
            cells, runs, wrapped = entries[index]
            row_cells = list(cells)
            row_attrs = expand_attrs(runs, len(row_cells))
            for slot in wanted.get(index, ()):
                column = marks[slot][1]
                if column > len(row_cells) and not wrapped:
                    pad = column - len(row_cells)
                    row_cells.extend(" " * pad)
                    row_attrs.extend([DEFAULT_ATTR] * pad)
                offsets.append((slot, len(all_cells) + min(column, len(row_cells))))
            all_cells.extend(row_cells)
            all_attrs.extend(row_attrs)
            index += 1
            if not wrapped or index >= count:
                break
        total = len(all_cells)
        start = 0
        while True:
            end = min(start + cols, total)
            padded = False
            if end < total and all_cells[end] == "" and end - 1 > start:
                end -= 1                           # a wide character does not fit: it goes to the next row
                padded = True
            row_cells = all_cells[start:end]
            row_attrs = all_attrs[start:end]
            if padded:
                row_cells.append(" ")
                row_attrs.append(DEFAULT_ATTR)
            if row_cells and row_cells[0] == "":
                row_cells[0] = " "                 # half of a wide character that had to be cut (one column)
            last = end >= total
            for slot, offset in offsets:
                if start <= offset < end or (last and offset >= end):
                    moved[slot] = (len(out), min(offset - start, cols - 1))
            out.append(_entry(row_cells, row_attrs, not last))
            start = end
            if last:
                break
    return out, moved


class History:
    """The scrollback: compact lines, at most ``max_lines`` of them and about ``max_bytes`` of memory."""

    def __init__(self, max_lines: int = 10000, max_bytes: int = HISTORY_BYTES) -> None:
        self.lines: deque = deque()
        self.max_lines = max(0, min(int(max_lines), MAX_SCROLLBACK))
        self.max_bytes = max_bytes
        self.cost = 0
        self.dropped = 0            # lines that have left the scrollback at its top, ever

    def __len__(self) -> int:
        return len(self.lines)

    def push(self, entry: tuple) -> None:
        cells, runs, _wrapped = entry
        cost = 120 + (len(cells) if type(cells) is str else 64 * len(cells))
        if runs is not None:
            cost += 56 * len(runs)
        lines = self.lines
        lines.append(entry)
        self.cost += cost
        if len(lines) > self.max_lines or self.cost > self.max_bytes:
            self.trim()

    def trim(self) -> None:
        lines = self.lines
        while lines and (len(lines) > self.max_lines or self.cost > self.max_bytes):
            self.cost -= _entry_cost(lines.popleft())
            self.dropped += 1

    def clear(self) -> None:
        self.dropped += len(self.lines)
        self.lines.clear()
        self.cost = 0

    def replace(self, entries: list) -> None:
        """Take new content after a reflow."""
        self.lines = deque(entries)
        self.cost = sum(_entry_cost(entry) for entry in entries)
        self.trim()

    def set_max_lines(self, max_lines: int) -> None:
        self.max_lines = max(0, min(int(max_lines), MAX_SCROLLBACK))
        self.trim()


def _nothing(*_args) -> None:
    return None


def parse_colour(spec: str) -> tuple[int, int, int] | None:
    """An X11 colour specification (``rgb:RR/GG/BB`` or ``#RRGGBB``, 1 to 4 digits a channel) as (r, g, b)."""
    match = _RGB_COLOUR.match(spec)
    if match:
        return tuple(int(part, 16) * 255 // ((1 << (4 * len(part))) - 1) for part in match.groups())  # type: ignore
    match = _HEX_COLOUR.match(spec)
    if match:
        digits = match.group(1)
        size = len(digits) // 3
        return tuple(int(digits[i * size:(i + 1) * size], 16) >> max(0, 4 * size - 8) if size > 1
                     else int(digits[i], 16) * 17 for i in range(3))  # type: ignore[return-value]
    return None


class Terminal:
    """A terminal of ``cols`` by ``rows`` cells with a scrollback of ``scrollback`` lines."""

    def __init__(self, cols: int = 80, rows: int = 24, scrollback: int = 10000) -> None:
        self.cols = max(MIN_COLS, min(int(cols), MAX_COLS))
        self.rows = max(MIN_ROWS, min(int(rows), MAX_ROWS))
        self.history = History(scrollback)
        self.main_lines: list[Line] = [Line(self.cols) for _ in range(self.rows)]
        self.alt_lines: list[Line] | None = None
        self.lines = self.main_lines
        self.alt_active = False
        self.dirty: set[int] = set()
        self.dirty_all = True
        self.scrolled = 0                   # lines pushed to the scrollback since take_dirty
        self.title = ""
        self.cwd = ""
        self.links: dict[int, str] = {}
        self._link_ids: dict[tuple[str, str], int] = {}
        self._next_link = 1
        self._attr_cache: dict[tuple, tuple] = {}
        self._sgr_cache: dict[tuple, tuple] = {}
        self._title_stack: list[str] = []
        self.colours: dict = {}             # colours an application set: palette index or "fg", "bg", "cursor"
        self.allow_clipboard_write = False
        self.dark = True                    # what the widget shows; answered to the colour scheme query
        self.cell_width = 8                 # in pixels, for the window size reports
        self.cell_height = 16
        self.hostname = socket.gethostname()
        # what the terminal tells its owner
        self.on_reply: Callable[[bytes], None] = _nothing
        self.on_title: Callable[[str], None] = _nothing
        self.on_bell: Callable[[], None] = _nothing
        self.on_cwd: Callable[[str], None] = _nothing
        self.on_clipboard: Callable[[str], None] = _nothing
        self.on_clipboard_denied: Callable[[], None] = _nothing
        self.on_colours: Callable[[], None] = _nothing
        self.colour_provider: Callable[[object], tuple[int, int, int] | None] = lambda _key: None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = _GROUND
        self._params = ""
        self._intermediate = ""
        self._string_kind = _IGNORED
        self._string: list[str] = []
        self._string_length = 0
        self._bel_terminated = False
        self._csi = self._build_csi_table()
        self._reset_state()

    # --- state -------------------------------------------------------------------------------------------
    def _reset_state(self) -> None:
        self.x = 0
        self.y = 0
        self.wrapnext = False
        self.top = 0
        self.bottom = self.rows - 1
        self._fg = self._bg = self._flags = self._ul = self._link = 0
        self.attr = DEFAULT_ATTR
        self.erase_attr = DEFAULT_ATTR
        self.autowrap = True
        self.origin = False
        self.insert = False
        self.lnm = False
        self.app_cursor = False
        self.app_keypad = False
        self.cursor_visible = True
        self.cursor_style = 0               # DECSCUSR: 0 the user's choice, 1/2 block, 3/4 underline, 5/6 bar
        self.cursor_blink: bool | None = None     # mode 12: None the user's choice
        self.reverse_video = False
        self.bracketed_paste = False
        self.focus_events = False
        self.mouse_mode = MOUSE_OFF
        self.mouse_encoding = ENCODING_DEFAULT
        self.alt_scroll = True
        self.sync_output = False
        self.scheme_reports = False
        self.g = ["B", "B"]
        self.gl = 0
        self._translate: dict | None = None
        self._last = ""
        self._saved: list[tuple | None] = [None, None]
        self.tabstops = [column % 8 == 0 for column in range(self.cols)]

    def reset(self) -> None:
        """A full reset (RIS): modes, attributes and both screens; the scrollback and the title stay."""
        if self.alt_active:
            self._leave_alt()
        self.main_lines[:] = [Line(self.cols) for _ in range(self.rows)]
        self._reset_state()
        if self.colours:
            self.colours.clear()
            self.on_colours()
        self.dirty_all = True

    def soft_reset(self) -> None:
        """DECSTR: modes and attributes back to their defaults; the screen content stays."""
        self.cursor_visible = True
        self.origin = False
        self.autowrap = True
        self.insert = False
        self.app_cursor = False
        self.app_keypad = False
        self.top, self.bottom = 0, self.rows - 1
        self._fg = self._bg = self._flags = self._ul = 0
        self._update_attr()
        self.g = ["B", "B"]
        self.gl = 0
        self._translate = None
        self.wrapnext = False
        self._saved[1 if self.alt_active else 0] = None
        self.cursor_style = 0

    def recover(self) -> None:
        """Bring the parser back to its ground state (after an internal error)."""
        self._state = _GROUND
        self._params = self._intermediate = ""
        self._string = []
        self._string_length = 0
        self._decoder.reset()
        self.x = max(0, min(self.x, self.cols - 1))
        self.y = max(0, min(self.y, self.rows - 1))
        self.dirty_all = True

    def take_dirty(self) -> tuple[bool, set[int], int]:
        """(everything changed, rows that changed, lines that went to the scrollback) since the last call."""
        result = (self.dirty_all, self.dirty, self.scrolled)
        self.dirty_all = False
        self.dirty = set()
        self.scrolled = 0
        return result

    # --- content for the widget ---------------------------------------------------------------------------
    def total_lines(self) -> int:
        """Lines of scrollback and screen together; ``display_line`` takes an index below this."""
        return len(self.history.lines) + self.rows

    def display_line(self, index: int) -> tuple:
        """Line ``index`` (scrollback first, then the screen) as ``(cells, runs, wrapped)``."""
        history = self.history.lines
        count = len(history)
        if index < count:
            return history[index]
        line = self.lines[index - count]
        attrs = line.attrs
        first = attrs[0]
        if attrs.count(first) == len(attrs):
            return (line.chars, None if first == DEFAULT_ATTR else (len(attrs), first), line.wrapped)
        runs: list = []
        for attr, group in groupby(attrs):
            runs.append(len(list(group)))
            runs.append(attr)
        return (line.chars, runs, line.wrapped)

    def iter_lines(self, start: int = 0, stop: int | None = None) -> Iterator[tuple]:
        """``display_line`` for a range of lines, without paying for random access into the scrollback."""
        history = self.history.lines
        count = len(history)
        total = count + self.rows
        stop = total if stop is None else min(stop, total)
        start = max(0, start)
        if start < count:
            yield from islice(history, start, min(stop, count))
        for index in range(max(start, count), stop):
            yield self.display_line(index)

    def line_text(self, index: int) -> str:
        """The text of a line without trailing blanks."""
        cells = self.display_line(index)[0]
        return (cells if type(cells) is str else "".join(cells)).rstrip(" ")

    def screen_text(self) -> list[str]:
        """The text of the screen rows (tests, accessibility)."""
        return ["".join(line.chars).rstrip(" ") for line in self.lines]

    def link_at(self, index: int, column: int) -> str:
        """The address of the OSC 8 hyperlink of a cell, or ''."""
        if not 0 <= index < self.total_lines():
            return ""
        cells, runs, _wrapped = self.display_line(index)
        if runs is None or column < 0:
            return ""
        position = 0
        for at in range(0, len(runs), 2):
            position += runs[at]
            if column < position:
                return self.links.get(runs[at + 1][4], "") if runs[at + 1][4] else ""
        return ""

    def set_scrollback(self, lines: int) -> None:
        self.history.set_max_lines(lines)

    # --- input from the child -----------------------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        """Take bytes the child process wrote."""
        text = self._decoder.decode(data)
        if text:
            self.feed_text(text)

    def feed_text(self, text: str) -> None:
        position, end = 0, len(text)
        printable, sequence, table = _PRINTABLE.match, _CSI_SEQUENCE.match, self._csi
        while position < end:
            if self._state != _GROUND:
                position = self._slow(text, position)
                continue
            match = printable(text, position)
            if match is not None:
                run = match.group()
                if self._translate is None and run.isascii():
                    self._put_narrow(run)
                else:
                    self._print(run)
                position = match.end()
                if position >= end:
                    break
            char = text[position]
            if char == "\n":
                y = self.y
                if y == self.bottom:
                    self._scroll_up(1)
                elif y < self.rows - 1:
                    self.y = y + 1
                self.wrapnext = False
                if self.lnm:
                    self.x = 0
                position += 1
            elif char == "\r":
                self.x = 0
                self.wrapnext = False
                position += 1
            elif char == "\x1b":
                match = sequence(text, position)
                if match is not None:
                    position = match.end()
                    private, params, intermediate, final = match.groups()
                    if private or intermediate:
                        final = private + intermediate + final
                    handler = table.get(final)
                    if handler is not None and len(params) <= MAX_PARAM_CHARS:
                        handler(params)
                else:
                    self._state = _ESC
                    self._intermediate = ""
                    position += 1
            else:
                self._control(char)
                position += 1

    def _control(self, char: str) -> None:
        if char == "\x08":
            if self.x > 0:
                self.x -= 1
            self.wrapnext = False
        elif char == "\t":
            self._tab(1)
        elif char in "\n\x0b\x0c":
            self._index()
            self.wrapnext = False
            if self.lnm:
                self.x = 0
        elif char == "\r":
            self.x = 0
            self.wrapnext = False
        elif char == "\x07":
            self.on_bell()
        elif char == "\x0e":
            self.gl = 1
            self._update_charset()
        elif char == "\x0f":
            self.gl = 0
            self._update_charset()
        # NUL, ENQ, DEL, the C1 controls and the rest: nothing

    def _slow(self, text: str, position: int) -> int:
        """One step of the parser outside the ground state; returns the new position."""
        state = self._state
        char = text[position]
        if state == _STRING:
            match = _STRING_END.search(text, position)
            stop = match.start() if match is not None else len(text)
            if stop > position and self._string_kind != _IGNORED and self._string_length < MAX_STRING:
                piece = text[position:min(stop, position + MAX_STRING - self._string_length)]
                self._string.append(piece)
                self._string_length += len(piece)
            if match is None:
                return len(text)
            char = text[stop]
            if char == "\x1b":
                self._state = _STRING_ESC
            elif char == "\x07" and self._string_kind == _OSC:
                self._bel_terminated = True
                self._finish_string()
            elif char in "\x18\x1a":
                self._state = _GROUND
                self._string = []
            return stop + 1
        if state == _STRING_ESC:
            if char == "\\":
                self._bel_terminated = False
                self._finish_string()
                return position + 1
            self._string = []                  # the string was cut off: drop it and start the new sequence
            self._state = _ESC
            self._intermediate = ""
            return position
        if char < " ":
            if char == "\x1b":
                self._state = _ESC
                self._intermediate = ""
            elif char in "\x18\x1a":
                self._state = _GROUND
            else:
                self._control(char)            # a control character inside a sequence acts at once
            return position + 1
        if state == _ESC:
            if char == "[":
                self._state = _CSI
                self._params = self._intermediate = ""
            elif char == "]":
                self._begin_string(_OSC)
            elif char == "P":
                self._begin_string(_DCS)
            elif char in "X^_":
                self._begin_string(_IGNORED)
            elif " " <= char <= "/":
                self._intermediate = char
                self._state = _ESC_INTERMEDIATE
            elif "0" <= char <= "~":
                self._state = _GROUND
                self._dispatch_esc("", char)
            else:
                self._state = _GROUND          # DEL or something that is not ASCII: the sequence is dropped
        elif state == _ESC_INTERMEDIATE:
            if " " <= char <= "/":
                if len(self._intermediate) < 4:
                    self._intermediate += char
            elif "0" <= char <= "~":
                self._state = _GROUND
                self._dispatch_esc(self._intermediate, char)
            else:
                self._state = _GROUND
        elif state == _CSI:
            if "0" <= char <= "?":
                if self._intermediate or len(self._params) >= MAX_PARAM_CHARS:
                    self._state = _CSI_IGNORE
                else:
                    self._params += char
            elif " " <= char <= "/":
                if len(self._intermediate) >= 2:
                    self._state = _CSI_IGNORE
                else:
                    self._intermediate += char
            elif "@" <= char <= "~":
                self._state = _GROUND
                params = self._params
                private = ""
                if params and params[0] in "<=>?":
                    private, params = params[0], params[1:]
                if not any(mark in params for mark in "<=>?") and len(self._intermediate) <= 1:
                    self._dispatch_csi(private, params, self._intermediate, char)
            elif char != "\x7f":
                self._state = _GROUND
        elif state == _CSI_IGNORE:
            if "@" <= char <= "~":
                self._state = _GROUND
            elif not " " <= char <= "?" and char != "\x7f":
                self._state = _GROUND
        else:
            self._state = _GROUND
        return position + 1

    def _begin_string(self, kind: str) -> None:
        self._state = _STRING
        self._string_kind = kind
        self._string = []
        self._string_length = 0

    def _finish_string(self) -> None:
        self._state = _GROUND
        kind, data = self._string_kind, "".join(self._string)
        self._string = []
        self._string_length = 0
        if kind == _OSC:
            self._dispatch_osc(data)
        elif kind == _DCS:
            self._dispatch_dcs(data)

    # --- printing -----------------------------------------------------------------------------------------
    def _print(self, text: str) -> None:
        """Put printable text at the cursor."""
        if self._translate is not None:
            text = text.translate(self._translate)
        if text.isascii() or max(text) < "\u0300":
            self._put_narrow(text)
            return
        special = _special_pattern()
        position = 0
        for match in special.finditer(text):
            start = match.start()
            if start > position:
                self._put_narrow(text[position:start])
            self._put_special(match.group())
            position = match.end()
        if position < len(text):
            self._put_narrow(text[position:])

    def _put_narrow(self, text: str) -> None:
        """Text in which every character takes exactly one cell."""
        cols = self.cols
        lines = self.lines
        attr = self.attr
        x = self.x
        count = len(text)
        position = 0
        while position < count:
            if self.wrapnext:
                self.wrapnext = False
                if self.autowrap:
                    lines[self.y].wrapped = True
                    self._index()
                    x = 0
                else:
                    position = count - 1          # no wrapping: only the last character stays in the last column
            y = self.y
            line = lines[y]
            chars = line.chars
            room = cols - x
            take = count - position
            if take > room:
                take = room
            end = x + take
            if self.insert:
                self._insert_blanks(line, x, take)
            else:
                if chars[x] == "" and x > 0:
                    chars[x - 1] = " "            # the first half of a wide character loses its second half
                if end < cols and chars[end] == "":
                    chars[end] = " "
            chars[x:end] = text[position:position + take] if take != count else text
            line.attrs[x:end] = [attr] * take
            self.dirty.add(y)
            position += take
            x = end
            if x >= cols:
                x = cols - 1
                self.wrapnext = True
        self.x = x
        self._last = text[-1]

    def _put_special(self, text: str) -> None:
        """Characters that take two cells or none."""
        cols = self.cols
        lines = self.lines
        attr = self.attr
        for char in text:
            width = wcwidth(char)
            if width <= 0:
                if width == 0:
                    self._combine(char)
                continue
            x = self.x
            if self.wrapnext:
                self.wrapnext = False
                if self.autowrap:
                    lines[self.y].wrapped = True
                    self._index()
                    x = 0
            if width == 2 and x >= cols - 1:
                if self.autowrap:
                    line = lines[self.y]
                    self._erase_cells(line, x, cols, attr)
                    line.wrapped = True
                    self._index()
                    x = 0
                else:
                    x = cols - 2
            y = self.y
            line = lines[y]
            chars = line.chars
            end = x + width
            if self.insert:
                self._insert_blanks(line, x, width)
            else:
                if chars[x] == "" and x > 0:
                    chars[x - 1] = " "
                if end < cols and chars[end] == "":
                    chars[end] = " "
            chars[x] = char
            line.attrs[x] = attr
            if width == 2:
                chars[x + 1] = ""
                line.attrs[x + 1] = attr
            self.dirty.add(y)
            if end >= cols:
                self.x = cols - 1
                self.wrapnext = True
            else:
                self.x = end
            self._last = char

    def _combine(self, char: str) -> None:
        """A zero-width character joins the cell before the cursor."""
        x = self.x if self.wrapnext else self.x - 1
        if x < 0:
            return
        chars = self.lines[self.y].chars
        if chars[x] == "" and x > 0:
            x -= 1
        cell = chars[x]
        if cell and len(cell) < MAX_CLUSTER:
            chars[x] = cell + char
            self.dirty.add(self.y)

    def _insert_blanks(self, line: Line, x: int, count: int) -> None:
        cols = self.cols
        chars, attrs = line.chars, line.attrs
        count = min(count, cols - x)
        if count <= 0:
            return
        if chars[x] == "" and x > 0:
            chars[x - 1] = " "
            chars[x] = " "
        chars[x:x] = " " * count
        del chars[cols:]
        attrs[x:x] = [self.erase_attr] * count
        del attrs[cols:]
        if _is_wide(chars[cols - 1]):
            chars[cols - 1] = " "
        if x + count < cols and chars[x + count] == "":
            chars[x + count] = " "

    def _erase_cells(self, line: Line, start: int, end: int, attr: tuple | None = None) -> None:
        """Blank the cells [start, end) of a line."""
        chars = line.chars
        if start >= end:
            return
        if chars[start] == "" and start > 0:
            chars[start - 1] = " "
        if end < self.cols and chars[end] == "":
            chars[end] = " "
        count = end - start
        chars[start:end] = " " * count
        line.attrs[start:end] = [self.erase_attr if attr is None else attr] * count

    # --- cursor and scrolling -----------------------------------------------------------------------------
    def _index(self) -> None:
        y = self.y
        if y == self.bottom:
            self._scroll_up(1)
        elif y < self.rows - 1:
            self.y = y + 1

    def _reverse_index(self) -> None:
        if self.y == self.top:
            self._scroll_down(1)
        elif self.y > 0:
            self.y -= 1
        self.wrapnext = False

    def _scroll_up(self, count: int) -> None:
        top, bottom, lines = self.top, self.bottom, self.lines
        if count > bottom - top + 1:
            count = bottom - top + 1
        if count <= 0:
            return
        if top == 0 and not self.alt_active:
            push = self.history.push
            for row in range(count):
                push(compact(lines[row]))
            self.scrolled += count
        cols, attr = self.cols, self.erase_attr
        if top == 0 and bottom == self.rows - 1:
            del lines[:count]
            for _ in range(count):
                lines.append(Line(cols, attr))
            self.dirty_all = True
        else:
            del lines[top:top + count]
            at = bottom - count + 1
            lines[at:at] = [Line(cols, attr) for _ in range(count)]
            self.dirty.update(range(top, bottom + 1))

    def _scroll_down(self, count: int) -> None:
        top, bottom, lines = self.top, self.bottom, self.lines
        count = min(count, bottom - top + 1)
        if count <= 0:
            return
        cols, attr = self.cols, self.erase_attr
        del lines[bottom - count + 1:bottom + 1]
        lines[top:top] = [Line(cols, attr) for _ in range(count)]
        self.dirty.update(range(top, bottom + 1))

    def _tab(self, count: int) -> None:
        x, last, stops = self.x, self.cols - 1, self.tabstops
        for _ in range(min(count, self.cols)):
            x += 1
            while x < last and not stops[x]:
                x += 1
            if x >= last:
                x = last
                break
        self.x = x

    def _back_tab(self, count: int) -> None:
        x, stops = self.x, self.tabstops
        for _ in range(min(count, self.cols)):
            x -= 1
            while x > 0 and not stops[x]:
                x -= 1
            if x <= 0:
                x = 0
                break
        self.x = x
        self.wrapnext = False

    def _move_to(self, x: int, y: int) -> None:
        """Absolute cursor position (0-based); ``y`` counts from the top margin in origin mode."""
        if self.origin:
            y = min(self.top + y, self.bottom)
        self.x = max(0, min(x, self.cols - 1))
        self.y = max(0, min(y, self.rows - 1))
        self.wrapnext = False

    def _save_cursor(self) -> None:
        self._saved[1 if self.alt_active else 0] = (self.x, self.y, self._fg, self._bg, self._flags, self._ul,
                                                    self.g[0], self.g[1], self.gl, self.origin, self.wrapnext)

    def _restore_cursor(self) -> None:
        saved = self._saved[1 if self.alt_active else 0]
        if saved is None:
            self.x = self.y = 0
            self._fg = self._bg = self._flags = self._ul = 0
            self.g = ["B", "B"]
            self.gl = 0
            self.origin = False
            self.wrapnext = False
        else:
            (x, y, self._fg, self._bg, self._flags, self._ul, g0, g1, self.gl, self.origin, wrapnext) = saved
            self.x = max(0, min(x, self.cols - 1))
            self.y = max(0, min(y, self.rows - 1))
            self.g = [g0, g1]
            self.wrapnext = bool(wrapnext and self.x == self.cols - 1)
        self._update_charset()
        self._update_attr()

    def _update_charset(self) -> None:
        self._translate = _DEC_GRAPHICS if self.g[self.gl] == "0" else None

    def _update_attr(self) -> None:
        key = (self._fg, self._bg, self._flags, self._ul, self._link)
        if key == DEFAULT_ATTR:
            self.attr = self.erase_attr = DEFAULT_ATTR
            return
        cache = self._attr_cache
        attr = cache.get(key)
        if attr is None:
            if len(cache) >= MAX_ATTRS:
                cache.clear()
            attr = cache[key] = key
        self.attr = attr
        if self._bg == 0:
            self.erase_attr = DEFAULT_ATTR
        else:
            erase = (0, self._bg, 0, 0, 0)
            self.erase_attr = cache.setdefault(erase, erase)

    # --- screens ------------------------------------------------------------------------------------------
    def _enter_alt(self) -> None:
        self.alt_lines = [Line(self.cols) for _ in range(self.rows)]
        self.lines = self.alt_lines
        self.alt_active = True
        self.dirty_all = True

    def _leave_alt(self) -> None:
        if not self.alt_active:
            return
        self.lines = self.main_lines
        self.alt_lines = None
        self.alt_active = False
        self._saved[1] = None
        self.dirty_all = True

    def _clear_screen(self) -> None:
        cols, attr = self.cols, self.erase_attr
        self.lines[:] = [Line(cols, attr) for _ in range(self.rows)]
        self.dirty_all = True

    # --- ESC ----------------------------------------------------------------------------------------------
    def _dispatch_esc(self, intermediate: str, final: str) -> None:
        if not intermediate:
            if final == "7":
                self._save_cursor()
            elif final == "8":
                self._restore_cursor()
            elif final == "D":
                self._index()
                self.wrapnext = False
            elif final == "E":
                self._index()
                self.x = 0
                self.wrapnext = False
            elif final == "H":
                self.tabstops[self.x] = True
            elif final == "M":
                self._reverse_index()
            elif final == "c":
                self.reset()
            elif final == "=":
                self.app_keypad = True
            elif final == ">":
                self.app_keypad = False
            elif final == "Z":
                self._reply("\x1b[?62;22c")
            return
        if intermediate in ("(", ")"):
            self.g[0 if intermediate == "(" else 1] = "0" if final == "0" else "B"
            self._update_charset()
        elif intermediate == "#" and final == "8":
            cols = self.cols
            for line in self.lines:
                line.chars = ["E"] * cols
                line.attrs = [DEFAULT_ATTR] * cols
                line.wrapped = False
            self.dirty_all = True
            self.top, self.bottom = 0, self.rows - 1
            self._move_to(0, 0)

    # --- CSI ----------------------------------------------------------------------------------------------
    def _build_csi_table(self) -> dict:
        return {
            "@": self._csi_ich, "A": self._csi_cuu, "B": self._csi_cud, "C": self._csi_cuf, "D": self._csi_cub,
            "E": self._csi_cnl, "F": self._csi_cpl, "G": self._csi_cha, "H": self._csi_cup, "I": self._csi_cht,
            "J": self._csi_ed, "K": self._csi_el, "L": self._csi_il, "M": self._csi_dl, "P": self._csi_dch,
            "S": self._csi_su, "T": self._csi_sd, "X": self._csi_ech, "Z": self._csi_cbt, "`": self._csi_cha,
            "a": self._csi_cuf, "b": self._csi_rep, "c": self._csi_da, "d": self._csi_vpa, "e": self._csi_cud,
            "f": self._csi_cup, "g": self._csi_tbc, "h": self._csi_sm, "l": self._csi_rm, "m": self._sgr,
            "n": self._csi_dsr, "r": self._csi_decstbm, "s": self._csi_save, "t": self._csi_window,
            "u": self._csi_restore,
            "?J": self._csi_ed, "?K": self._csi_el, "?h": self._csi_decset, "?l": self._csi_decrst,
            "?n": self._csi_decdsr, "?$p": self._csi_decrqm, "$p": self._csi_rqm, "!p": self._csi_decstr,
            " q": self._csi_decscusr, ">c": self._csi_da2, ">q": self._csi_xtversion,
        }

    def _dispatch_csi(self, private: str, params: str, intermediate: str, final: str) -> None:
        if len(params) > MAX_PARAM_CHARS:
            return
        handler = self._csi.get(private + intermediate + final if private or intermediate else final)
        if handler is not None:
            handler(params)

    @staticmethod
    def _numbers(params: str) -> list[int]:
        if not params:
            return []
        out = []
        for part in params.split(";", MAX_PARAMS)[:MAX_PARAMS]:
            if ":" in part:
                part = part.partition(":")[0]
            out.append(0 if not part else 65535 if len(part) > 5 else min(int(part), 65535))
        return out

    @staticmethod
    def _count(params: str) -> int:
        """The first parameter as a count: missing or 0 means 1."""
        if not params:
            return 1
        part = params.partition(";")[0].partition(":")[0]
        if not part:
            return 1
        return 65535 if len(part) > 5 else (min(int(part), 65535) or 1)

    def _csi_ich(self, params: str) -> None:
        self._insert_blanks(self.lines[self.y], self.x, self._count(params))
        self.dirty.add(self.y)
        self.wrapnext = False

    def _csi_cuu(self, params: str) -> None:
        count, y = self._count(params), self.y
        self.y = max(y - count, self.top if y >= self.top else 0)
        self.wrapnext = False

    def _csi_cud(self, params: str) -> None:
        count, y = self._count(params), self.y
        self.y = min(y + count, self.bottom if y <= self.bottom else self.rows - 1)
        self.wrapnext = False

    def _csi_cuf(self, params: str) -> None:
        self.x = min(self.x + self._count(params), self.cols - 1)
        self.wrapnext = False

    def _csi_cub(self, params: str) -> None:
        self.x = max(self.x - self._count(params), 0)
        self.wrapnext = False

    def _csi_cnl(self, params: str) -> None:
        self._csi_cud(params)
        self.x = 0

    def _csi_cpl(self, params: str) -> None:
        self._csi_cuu(params)
        self.x = 0

    def _csi_cha(self, params: str) -> None:
        self.x = min(self._count(params), self.cols) - 1
        self.wrapnext = False

    def _csi_vpa(self, params: str) -> None:
        self._move_to(self.x, self._count(params) - 1)

    def _csi_cup(self, params: str) -> None:
        if not params:
            self._move_to(0, 0)
            return
        numbers = self._numbers(params)
        row = numbers[0] or 1
        column = (numbers[1] if len(numbers) > 1 else 1) or 1
        self._move_to(column - 1, row - 1)

    def _csi_cht(self, params: str) -> None:
        self._tab(self._count(params))

    def _csi_cbt(self, params: str) -> None:
        self._back_tab(self._count(params))

    def _csi_ed(self, params: str) -> None:
        mode = self._numbers(params)[0] if params else 0
        lines, cols, rows, attr = self.lines, self.cols, self.rows, self.erase_attr
        y = self.y
        if mode == 0:
            self._erase_cells(lines[y], self.x, cols)
            lines[y].wrapped = False
            for row in range(y + 1, rows):
                lines[row] = Line(cols, attr)
            self.dirty.update(range(y, rows))
        elif mode == 1:
            for row in range(y):
                lines[row] = Line(cols, attr)
            self._erase_cells(lines[y], 0, min(self.x + 1, cols))
            self.dirty.update(range(0, y + 1))
        elif mode == 2:
            self._clear_screen()
        elif mode == 3:
            self.history.clear()
            self.dirty_all = True
        self.wrapnext = False

    def _csi_el(self, params: str) -> None:
        mode = self._numbers(params)[0] if params else 0
        y = self.y
        line = self.lines[y]
        if mode == 0:
            self._erase_cells(line, self.x, self.cols)
            line.wrapped = False
        elif mode == 1:
            self._erase_cells(line, 0, min(self.x + 1, self.cols))
        elif mode == 2:
            self.lines[y] = Line(self.cols, self.erase_attr)
        else:
            return
        self.dirty.add(y)
        self.wrapnext = False

    def _csi_il(self, params: str) -> None:
        y, bottom = self.y, self.bottom
        if not self.top <= y <= bottom:
            return
        count = min(self._count(params), bottom - y + 1)
        lines = self.lines
        del lines[bottom - count + 1:bottom + 1]
        lines[y:y] = [Line(self.cols, self.erase_attr) for _ in range(count)]
        self.dirty.update(range(y, bottom + 1))
        self.x = 0
        self.wrapnext = False

    def _csi_dl(self, params: str) -> None:
        y, bottom = self.y, self.bottom
        if not self.top <= y <= bottom:
            return
        count = min(self._count(params), bottom - y + 1)
        lines = self.lines
        del lines[y:y + count]
        at = bottom - count + 1
        lines[at:at] = [Line(self.cols, self.erase_attr) for _ in range(count)]
        self.dirty.update(range(y, bottom + 1))
        self.x = 0
        self.wrapnext = False

    def _csi_dch(self, params: str) -> None:
        cols, x, y = self.cols, self.x, self.y
        line = self.lines[y]
        chars, attrs = line.chars, line.attrs
        count = min(self._count(params), cols - x)
        if chars[x] == "" and x > 0:
            chars[x - 1] = " "
        del chars[x:x + count]
        chars.extend(" " * count)
        del attrs[x:x + count]
        attrs.extend([self.erase_attr] * count)
        if chars[x] == "":
            chars[x] = " "
        self.dirty.add(y)
        self.wrapnext = False

    def _csi_ech(self, params: str) -> None:
        self._erase_cells(self.lines[self.y], self.x, min(self.x + self._count(params), self.cols))
        self.dirty.add(self.y)
        self.wrapnext = False

    def _csi_su(self, params: str) -> None:
        self._scroll_up(min(self._count(params), self.rows))

    def _csi_sd(self, params: str) -> None:
        if ";" not in params:                       # with more parameters it is a mouse tracking request
            self._scroll_down(min(self._count(params), self.rows))

    def _csi_rep(self, params: str) -> None:
        if self._last:
            self._print(self._last * min(self._count(params), self.cols * self.rows))

    def _csi_tbc(self, params: str) -> None:
        mode = self._numbers(params)[0] if params else 0
        if mode == 0:
            self.tabstops[self.x] = False
        elif mode == 3:
            self.tabstops = [False] * self.cols

    def _csi_decstbm(self, params: str) -> None:
        numbers = self._numbers(params)
        top = (numbers[0] if numbers else 0) or 1
        bottom = (numbers[1] if len(numbers) > 1 else 0) or self.rows
        bottom = min(bottom, self.rows)
        if top < bottom:
            self.top, self.bottom = top - 1, bottom - 1
            self._move_to(0, 0)

    def _csi_save(self, params: str) -> None:
        if not params:
            self._save_cursor()

    def _csi_restore(self, params: str) -> None:
        if not params:
            self._restore_cursor()

    def _csi_decstr(self, _params: str) -> None:
        self.soft_reset()

    def _csi_decscusr(self, params: str) -> None:
        style = self._numbers(params)[0] if params else 0
        if style <= 6:
            self.cursor_style = style

    def _csi_da(self, params: str) -> None:
        if not params or params == "0":
            self._reply("\x1b[?62;22c")             # a VT220 with ANSI colour

    def _csi_da2(self, params: str) -> None:
        if not params or params == "0":
            # A VT220 at "xterm patch 277": programs that look at the number (vim) then use the SGR mouse
            # protocol. Not a VT420 (41): tmux would take that as support for left and right margins.
            self._reply("\x1b[>1;277;0c")

    def _csi_xtversion(self, params: str) -> None:
        if not params or params == "0":
            self._reply("\x1bP>|boswas-terminal(1.0)\x1b\\")

    def _csi_dsr(self, params: str) -> None:
        mode = self._numbers(params)[0] if params else 0
        if mode == 5:
            self._reply("\x1b[0n")
        elif mode == 6:
            row = self.y - self.top if self.origin else self.y
            self._reply(f"\x1b[{row + 1};{self.x + 1}R")

    def _csi_decdsr(self, params: str) -> None:
        mode = self._numbers(params)[0] if params else 0
        if mode == 6:
            row = self.y - self.top if self.origin else self.y
            self._reply(f"\x1b[?{row + 1};{self.x + 1}R")
        elif mode == 15:
            self._reply("\x1b[?13n")                # no printer
        elif mode == 25:
            self._reply("\x1b[?20n")                # user-defined keys are unlocked
        elif mode == 26:
            self._reply("\x1b[?27;1;0;0n")          # keyboard: North American
        elif mode == 996:
            self._reply(f"\x1b[?997;{1 if self.dark else 2}n")

    def _csi_window(self, params: str) -> None:
        numbers = self._numbers(params)
        if not numbers:
            return
        operation = numbers[0]
        if operation == 14:
            self._reply(f"\x1b[4;{self.rows * self.cell_height};{self.cols * self.cell_width}t")
        elif operation == 16:
            self._reply(f"\x1b[6;{self.cell_height};{self.cell_width}t")
        elif operation in (18, 19):
            self._reply(f"\x1b[{operation - 10};{self.rows};{self.cols}t")
        elif operation == 22:
            if len(self._title_stack) < MAX_TITLE_STACK:
                self._title_stack.append(self.title)
        elif operation == 23:
            if self._title_stack:
                self._set_title(self._title_stack.pop())
        # moving, resizing and reading the title back are not done: a program has no business with them

    # --- modes --------------------------------------------------------------------------------------------
    def _csi_sm(self, params: str) -> None:
        for mode in self._numbers(params):
            if mode == 4:
                self.insert = True
            elif mode == 20:
                self.lnm = True

    def _csi_rm(self, params: str) -> None:
        for mode in self._numbers(params):
            if mode == 4:
                self.insert = False
            elif mode == 20:
                self.lnm = False

    def _csi_decset(self, params: str) -> None:
        for mode in self._numbers(params):
            self._set_private(mode, True)

    def _csi_decrst(self, params: str) -> None:
        for mode in self._numbers(params):
            self._set_private(mode, False)

    def _set_private(self, mode: int, on: bool) -> None:
        if mode == 1:
            self.app_cursor = on
        elif mode == 5:
            if self.reverse_video != on:
                self.reverse_video = on
                self.dirty_all = True
        elif mode == 6:
            self.origin = on
            self._move_to(0, 0)
        elif mode == 7:
            self.autowrap = on
            if not on:
                self.wrapnext = False
        elif mode == 12:
            self.cursor_blink = on
        elif mode == 25:
            self.cursor_visible = on
        elif mode == 66:
            self.app_keypad = on
        elif mode in (MOUSE_X10, MOUSE_NORMAL, MOUSE_BUTTON, MOUSE_ANY):
            if on:
                self.mouse_mode = mode
            elif self.mouse_mode == mode:
                self.mouse_mode = MOUSE_OFF
        elif mode in (ENCODING_UTF8, ENCODING_SGR, ENCODING_URXVT):
            if on:
                self.mouse_encoding = mode
            elif self.mouse_encoding == mode:
                self.mouse_encoding = ENCODING_DEFAULT
        elif mode == 1004:
            self.focus_events = on
        elif mode == 1007:
            self.alt_scroll = on
        elif mode in (47, 1047):
            if on and not self.alt_active:
                self._enter_alt()
            elif not on:
                self._leave_alt()
        elif mode == 1048:
            if on:
                self._save_cursor()
            else:
                self._restore_cursor()
        elif mode == 1049:
            if on:
                if not self.alt_active:
                    self._save_cursor()
                    self._enter_alt()
                else:
                    self._clear_screen()
            elif self.alt_active:
                self._leave_alt()
                self._restore_cursor()
        elif mode == 2004:
            self.bracketed_paste = on
        elif mode == 2026:
            self.sync_output = on
        elif mode == 2031:
            self.scheme_reports = on

    def _private_state(self, mode: int) -> bool | None:
        simple = {1: self.app_cursor, 5: self.reverse_video, 6: self.origin, 7: self.autowrap,
                  25: self.cursor_visible, 66: self.app_keypad, 1004: self.focus_events, 1007: self.alt_scroll,
                  47: self.alt_active, 1047: self.alt_active, 1049: self.alt_active, 2004: self.bracketed_paste,
                  2026: self.sync_output, 2031: self.scheme_reports}
        if mode in simple:
            return bool(simple[mode])
        if mode == 12:
            return bool(self.cursor_blink) if self.cursor_blink is not None else self.cursor_style in (0, 1, 3, 5)
        if mode in (MOUSE_X10, MOUSE_NORMAL, MOUSE_BUTTON, MOUSE_ANY):
            return self.mouse_mode == mode
        if mode in (ENCODING_UTF8, ENCODING_SGR, ENCODING_URXVT):
            return self.mouse_encoding == mode
        return None

    def _csi_decrqm(self, params: str) -> None:
        mode = self._numbers(params)[0] if params else 0
        state = self._private_state(mode)
        self._reply(f"\x1b[?{mode};{0 if state is None else 1 if state else 2}$y")

    def _csi_rqm(self, params: str) -> None:
        mode = self._numbers(params)[0] if params else 0
        state = {4: self.insert, 20: self.lnm}.get(mode)
        self._reply(f"\x1b[{mode};{0 if state is None else 1 if state else 2}$y")

    # --- SGR ----------------------------------------------------------------------------------------------
    def _sgr(self, params: str) -> None:
        if not params or params == "0":
            self._fg = self._bg = self._flags = self._ul = 0
            if self._link:
                self._update_attr()
            else:
                self.attr = self.erase_attr = DEFAULT_ATTR
            return
        cache_key = (params, self.attr)
        cached = self._sgr_cache.get(cache_key)
        if cached is not None:
            self.attr, self.erase_attr, self._fg, self._bg, self._flags, self._ul = cached
            return
        fg, bg, flags, ul = self._fg, self._bg, self._flags, self._ul
        parts = params.split(";", MAX_PARAMS)
        count = min(len(parts), MAX_PARAMS)
        index = 0
        while index < count:
            part = parts[index]
            index += 1
            if ":" in part:
                sub = part.split(":", 8)
                code = _number(sub[0])
                if code == 4:
                    style = _number(sub[1]) if len(sub) > 1 else 1
                    if style <= 5:
                        flags = (flags & ~UNDERLINE_MASK) | (style << UNDERLINE_SHIFT)
                elif code in (38, 48, 58):
                    colour = _sub_colour(sub)
                    if colour is not None:
                        if code == 38:
                            fg = colour
                        elif code == 48:
                            bg = colour
                        else:
                            ul = colour
                continue
            code = _number(part)
            if code == 0:
                fg = bg = flags = ul = 0
            elif code == 1:
                flags |= BOLD
            elif code == 2:
                flags |= DIM
            elif code == 3:
                flags |= ITALIC
            elif code == 4:
                flags = (flags & ~UNDERLINE_MASK) | (1 << UNDERLINE_SHIFT)
            elif code in (5, 6):
                flags |= BLINK
            elif code == 7:
                flags |= INVERSE
            elif code == 8:
                flags |= INVISIBLE
            elif code == 9:
                flags |= STRIKE
            elif code == 21:
                flags = (flags & ~UNDERLINE_MASK) | (2 << UNDERLINE_SHIFT)
            elif code == 22:
                flags &= ~(BOLD | DIM)
            elif code == 23:
                flags &= ~ITALIC
            elif code == 24:
                flags &= ~UNDERLINE_MASK
            elif code == 25:
                flags &= ~BLINK
            elif code == 27:
                flags &= ~INVERSE
            elif code == 28:
                flags &= ~INVISIBLE
            elif code == 29:
                flags &= ~STRIKE
            elif 30 <= code <= 37:
                fg = PALETTE | (code - 30)
            elif code == 39:
                fg = 0
            elif 40 <= code <= 47:
                bg = PALETTE | (code - 40)
            elif code == 49:
                bg = 0
            elif code == 53:
                flags |= OVERLINE
            elif code == 55:
                flags &= ~OVERLINE
            elif code == 59:
                ul = 0
            elif 90 <= code <= 97:
                fg = PALETTE | (code - 82)
            elif 100 <= code <= 107:
                bg = PALETTE | (code - 92)
            elif code in (38, 48, 58):
                colour = None
                kind = parts[index] if index < count else ""
                if kind == "5" and index + 1 < count:
                    colour = PALETTE | (_number(parts[index + 1]) & 255)
                    index += 2
                elif kind == "2" and index + 3 < count:
                    colour = RGB | ((_number(parts[index + 1]) & 255) << 16) \
                        | ((_number(parts[index + 2]) & 255) << 8) | (_number(parts[index + 3]) & 255)
                    index += 4
                else:
                    index = count                    # malformed: the rest of the sequence means nothing
                if colour is not None:
                    if code == 38:
                        fg = colour
                    elif code == 48:
                        bg = colour
                    else:
                        ul = colour
        self._fg, self._bg, self._flags, self._ul = fg, bg, flags, ul
        self._update_attr()
        if len(self._sgr_cache) >= MAX_ATTRS:
            self._sgr_cache.clear()
        self._sgr_cache[cache_key] = (self.attr, self.erase_attr, fg, bg, flags, ul)

    def _sgr_report(self) -> str:
        """The current attributes as the parameters of an SGR sequence (for DECRQSS)."""
        out = ["0"]
        flags = self._flags
        for flag, code in ((BOLD, "1"), (DIM, "2"), (ITALIC, "3"), (BLINK, "5"), (INVERSE, "7"), (INVISIBLE, "8"),
                           (STRIKE, "9"), (OVERLINE, "53")):
            if flags & flag:
                out.append(code)
        style = (flags & UNDERLINE_MASK) >> UNDERLINE_SHIFT
        if style:
            out.append("4" if style == 1 else f"4:{style}")
        for colour, base in ((self._fg, 38), (self._bg, 48), (self._ul, 58)):
            if colour & PALETTE:
                index = colour & 255
                if base != 58 and index < 8:
                    out.append(str(base - 8 + index))
                elif base != 58 and index < 16:
                    out.append(str(base + 52 + index - 8))
                else:
                    out.append(f"{base}:5:{index}")
            elif colour & RGB:
                out.append(f"{base}:2::{(colour >> 16) & 255}:{(colour >> 8) & 255}:{colour & 255}")
        return ";".join(out)

    # --- OSC and DCS --------------------------------------------------------------------------------------
    def _dispatch_osc(self, data: str) -> None:
        code, _separator, rest = data.partition(";")
        if not code.isascii() or not code.isdigit() or len(code) > 4:
            return
        number = int(code)
        if number in (0, 2):
            self._set_title(rest)
        elif number == 4:
            self._osc_palette(rest)
        elif number in (10, 11, 12):
            for offset, spec in enumerate(rest.split(";", 3)[:3]):
                if number + offset <= 12:
                    self._osc_dynamic(number + offset, spec)
        elif number == 104:
            self._osc_reset_palette(rest)
        elif number in (110, 111, 112):
            if self.colours.pop({110: "fg", 111: "bg", 112: "cursor"}[number], None) is not None:
                self.on_colours()
                self.dirty_all = True
        elif number == 7:
            self._osc_cwd(rest)
        elif number == 8:
            self._osc_link(rest)
        elif number == 52:
            self._osc_clipboard(rest)

    def _string_terminator(self) -> str:
        return "\x07" if self._bel_terminated else "\x1b\\"

    def _set_title(self, text: str) -> None:
        title = _UNSAFE_TEXT.sub("", text)[:MAX_TITLE]
        if title != self.title:
            self.title = title
            self.on_title(title)

    def _osc_palette(self, rest: str) -> None:
        parts = rest.split(";", 33)[:32]
        changed = False
        for at in range(0, len(parts) - 1, 2):
            index, spec = parts[at], parts[at + 1]
            if not (index.isascii() and index.isdigit() and len(index) <= 3 and int(index) < 256):
                continue
            number = int(index)
            if spec == "?":
                colour = self.colours.get(number) or self.colour_provider(number)
                if colour is not None:
                    self._reply(f"\x1b]4;{number};{_colour_spec(colour)}{self._string_terminator()}")
            else:
                colour = parse_colour(spec)
                if colour is not None:
                    self.colours[number] = colour
                    changed = True
        if changed:
            self.on_colours()
            self.dirty_all = True

    def _osc_reset_palette(self, rest: str) -> None:
        if not rest:
            removed = [key for key in self.colours if isinstance(key, int)]
        else:
            removed = [int(part) for part in rest.split(";", 256)[:256]
                       if part.isascii() and part.isdigit() and len(part) <= 3 and int(part) in self.colours]
        for key in removed:
            self.colours.pop(key, None)
        if removed:
            self.on_colours()
            self.dirty_all = True

    def _osc_dynamic(self, number: int, spec: str) -> None:
        key = {10: "fg", 11: "bg", 12: "cursor"}[number]
        if spec == "?":
            colour = self.colours.get(key) or self.colour_provider(key)
            if colour is not None:
                self._reply(f"\x1b]{number};{_colour_spec(colour)}{self._string_terminator()}")
            return
        colour = parse_colour(spec)
        if colour is not None:
            self.colours[key] = colour
            self.on_colours()
            self.dirty_all = True

    def _osc_cwd(self, rest: str) -> None:
        if len(rest) > 3 * MAX_PATH:
            return
        try:
            parts = urlsplit(rest)
        except ValueError:
            return
        if parts.scheme != "file" or parts.netloc not in ("", "localhost", self.hostname):
            return
        path = unquote(parts.path, errors="surrogateescape")
        if not path.startswith("/") or "\0" in path or len(path) > MAX_PATH:
            return
        if path != self.cwd:
            self.cwd = path
            self.on_cwd(path)

    def _osc_link(self, rest: str) -> None:
        params, separator, uri = rest.partition(";")
        if not separator:
            return
        if not uri or len(uri) > MAX_URI or _UNSAFE_TEXT.search(uri):
            self._link = 0
        else:
            name = ""
            for item in params.split(":", 8)[:8]:
                if item.startswith("id="):
                    name = item[3:259]
            key = (name, uri)
            link = self._link_ids.get(key)
            if link is None:
                if len(self.links) >= MAX_LINKS:
                    for old in list(islice(self.links, MAX_LINKS // 2)):
                        del self.links[old]
                    self._link_ids = {k: v for k, v in self._link_ids.items() if v in self.links}
                link = self._next_link
                self._next_link += 1
                self.links[link] = uri
                self._link_ids[key] = link
            self._link = link
        self._update_attr()

    def _osc_clipboard(self, rest: str) -> None:
        targets, separator, payload = rest.partition(";")
        if not separator or payload == "?":
            return                                   # reading the clipboard is never answered
        if targets and not any(target in targets for target in "cs"):
            return                                   # only the clipboard, not the primary selection
        if not self.allow_clipboard_write:
            self.on_clipboard_denied()
            return
        if len(payload) > (MAX_CLIPBOARD * 4 + 2) // 3 + 4:
            return
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return
        text = raw[:MAX_CLIPBOARD].decode("utf-8", "replace").replace("\0", "")
        self.on_clipboard(text)

    def _dispatch_dcs(self, data: str) -> None:
        if data.startswith("$q"):
            request = data[2:10]
            if request == "m":
                answer = f"1$r{self._sgr_report()}m"
            elif request == "r":
                answer = f"1$r{self.top + 1};{self.bottom + 1}r"
            elif request == " q":
                answer = f"1$r{self.cursor_style} q"
            else:
                answer = "0$r"
            self._reply(f"\x1bP{answer}\x1b\\")
        elif data.startswith("+q"):
            names = data[2:].split(";", 16)[:16]
            known = {"TN": "xterm-256color", "name": "xterm-256color", "Co": "256", "colors": "256", "RGB": "8/8/8"}
            for name in names:
                try:
                    plain = bytes.fromhex(name).decode("ascii")
                except (ValueError, UnicodeDecodeError):
                    plain = ""
                if plain in known:
                    self._reply(f"\x1bP1+r{name}={known[plain].encode('ascii').hex()}\x1b\\")
                else:
                    self._reply("\x1bP0+r\x1b\\")
        # everything else (sixel, user-defined keys, pass-through) is dropped

    def _reply(self, text: str) -> None:
        self.on_reply(text.encode("utf-8", "replace"))

    def report_scheme(self, dark: bool) -> None:
        """The widget's colour scheme turned dark or light; told to a program that asked to be told."""
        if dark != self.dark:
            self.dark = dark
            if self.scheme_reports:
                self._reply(f"\x1b[?997;{1 if dark else 2}n")

    # --- size ---------------------------------------------------------------------------------------------
    def resize(self, cols: int, rows: int) -> None:
        """Change the size. Lines of the main screen and the scrollback are wrapped again for the new width."""
        cols = max(MIN_COLS, min(int(cols), MAX_COLS))
        rows = max(MIN_ROWS, min(int(rows), MAX_ROWS))
        if cols == self.cols and rows == self.rows:
            return
        if self.alt_active:
            saved = self._saved[0]
            x, y = self._reflow_main(cols, rows, saved[0] if saved else 0, saved[1] if saved else 0)
            if saved is not None:
                self._saved[0] = (x, y, *saved[2:10], False)
            self.alt_lines = self._resize_plain(self.alt_lines or [], cols, rows)
            self.lines = self.alt_lines
            self.x = min(self.x, cols - 1)
            self.y = min(self.y, rows - 1)
        else:
            self.x, self.y = self._reflow_main(cols, rows, self.x, self.y)
            self.lines = self.main_lines
        old_cols = self.cols
        self.cols, self.rows = cols, rows
        self.top, self.bottom = 0, rows - 1
        self.wrapnext = False
        if cols > old_cols:
            self.tabstops.extend(column % 8 == 0 for column in range(old_cols, cols))
        else:
            del self.tabstops[cols:]
        self.dirty_all = True

    def _resize_plain(self, lines: list[Line], cols: int, rows: int) -> list[Line]:
        """The alternate screen at a new size: rows are cut or padded, nothing is wrapped again."""
        if len(lines) > rows:
            surplus = len(lines) - rows
            above = min(surplus, max(0, self.y - (rows - 1)))
            del lines[:above]
            self.y -= above
            del lines[rows:]
        for line in lines:
            count = len(line.chars)
            if count < cols:
                line.chars.extend(" " * (cols - count))
                line.attrs.extend([DEFAULT_ATTR] * (cols - count))
            elif count > cols:
                del line.chars[cols:]
                del line.attrs[cols:]
                if _is_wide(line.chars[-1]):
                    line.chars[-1] = " "
        while len(lines) < rows:
            lines.append(Line(cols))
        return lines

    def _reflow_main(self, cols: int, rows: int, x: int, y: int) -> tuple[int, int]:
        """Give the main screen and the scrollback the new size; returns where the position (x, y) went."""
        lines = self.main_lines
        history = self.history
        y = max(0, min(y, len(lines) - 1))
        used = len(lines)
        while used > y + 1 and _blank(lines[used - 1]):
            used -= 1
        count = len(history.lines)
        entries = list(history.lines)
        entries.extend(compact(line) for line in lines[:used])
        marks = [(count + y, x), (count, 0)]
        if cols != self.cols:
            entries, marks = reflow(entries, cols, marks)
        (cursor_row, cursor_x), (top_row, _column) = marks
        total = len(entries)
        if y >= len(lines) - 1:
            start = max(0, total - rows)            # the cursor was on the last row: it stays there
        else:
            start = max(top_row, total - rows, cursor_row - rows + 1)
            start = max(0, min(start, cursor_row))
        history.replace(entries[:start])
        screen = [expand(entry, cols) for entry in entries[start:start + rows]]
        while len(screen) < rows:
            screen.append(Line(cols))
        self.main_lines = screen
        return max(0, min(cursor_x, cols - 1)), max(0, min(cursor_row - start, rows - 1))


def _blank(line: Line) -> bool:
    chars = line.chars
    return chars.count(" ") == len(chars) and not line.wrapped and all(attr[1] == 0 for attr in set(line.attrs))


def _number(text: str) -> int:
    """A parameter as a number: empty is 0, anything huge is capped."""
    if not text:
        return 0
    if len(text) > 5 or not text.isdigit() or not text.isascii():
        return 65535 if text.isascii() and text.isdigit() else 0
    return int(text)


def _sub_colour(sub: list[str]) -> int | None:
    """The colour of ``38:5:n``, ``38:2:r:g:b`` or ``38:2:space:r:g:b``."""
    kind = sub[1] if len(sub) > 1 else ""
    if kind == "5" and len(sub) > 2:
        return PALETTE | (_number(sub[2]) & 255)
    if kind == "2" and len(sub) >= 5:
        r, g, b = sub[-3:] if len(sub) >= 6 else sub[2:5]
        return RGB | ((_number(r) & 255) << 16) | ((_number(g) & 255) << 8) | (_number(b) & 255)
    return None


def _colour_spec(colour: tuple[int, int, int]) -> str:
    return "rgb:" + "/".join(f"{channel * 257:04x}" for channel in colour)
