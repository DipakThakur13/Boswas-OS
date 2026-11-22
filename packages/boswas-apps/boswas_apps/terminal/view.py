"""TerminalView: the widget that shows a terminal and takes keyboard and mouse.

Drawing. The cell grid is drawn into an image whose pixels are device
pixels: the cell size is a whole number of device pixels at every scale
factor, each glyph sits on the grid, and the image is copied to the screen
without scaling, so there are no seams between cells at 125 % or 150 %.
Only rows that changed are drawn again, at most about sixty times a
second. Text is drawn a run at a time (cells that share their attributes):
the font is given a letter spacing that makes its advance exactly one cell,
so a run of ordinary characters is one drawText call; characters of
fallback fonts, wide characters and combining sequences are placed cell by
cell; box drawing and block characters are drawn as shapes (boxdraw).

Lines are addressed in two ways. An *index* is what the emulator's
``display_line`` takes: scrollback first, then the screen. An *absolute*
line number is the index plus the number of lines the scrollback has
dropped, so it stays the same while output scrolls; the selection, the
search matches and the hovered link are kept in absolute line numbers.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QImage, QPainter,
                           QPainterPath, QPen, QTransform)
from PySide6.QtWidgets import QApplication, QScrollBar, QWidget

import boswas_fluent as fluent

from . import boxdraw, grid, keys, schemes
from . import settings as settings_module
from .emulator import (BOLD, DEFAULT_ATTR, DIM, INVERSE, INVISIBLE, ITALIC, MOUSE_ANY, MOUSE_BUTTON, MOUSE_OFF,
                       OVERLINE, RGB, STRIKE, UNDERLINE_MASK, UNDERLINE_SHIFT, Terminal)
from .wcwidth import clusters, wcwidth

FRAME_MS = 16                    # the frame time when the screen reports no refresh rate
SYNC_TIMEOUT = 0.15              # seconds a program may hold back drawing (synchronized output)
PADDING = 8                      # logical pixels around the grid
WHEEL_LINES = 3
BELL_INTERVAL = 0.2
FLASH_MS = 120
_DECORATED = UNDERLINE_MASK | STRIKE | OVERLINE

_KEY_NAMES = {
    Qt.Key.Key_Up: "Up", Qt.Key.Key_Down: "Down", Qt.Key.Key_Left: "Left", Qt.Key.Key_Right: "Right",
    Qt.Key.Key_Home: "Home", Qt.Key.Key_End: "End", Qt.Key.Key_Insert: "Insert", Qt.Key.Key_Delete: "Delete",
    Qt.Key.Key_PageUp: "PageUp", Qt.Key.Key_PageDown: "PageDown", Qt.Key.Key_Return: "Enter",
    Qt.Key.Key_Enter: "KP_Enter", Qt.Key.Key_Tab: "Tab", Qt.Key.Key_Backtab: "Backtab",
    Qt.Key.Key_Backspace: "Backspace", Qt.Key.Key_Escape: "Escape",
}
_KEY_NAMES.update({getattr(Qt.Key, f"Key_F{number}"): f"F{number}" for number in range(1, 21)})
_MODIFIER_KEYS = (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr,
                  Qt.Key.Key_CapsLock, Qt.Key.Key_NumLock, Qt.Key.Key_ScrollLock, Qt.Key.Key_Super_L,
                  Qt.Key.Key_Super_R, Qt.Key.Key_Hyper_L, Qt.Key.Key_Hyper_R)
_BUTTONS = {Qt.MouseButton.LeftButton: 0, Qt.MouseButton.MiddleButton: 1, Qt.MouseButton.RightButton: 2}


def default_font_family() -> str:
    """The first installed family of the list every Boswas terminal uses, else the system's fixed font."""
    installed = set(QFontDatabase.families())
    for family in settings_module.FONT_CHOICES[:-1]:
        if family in installed:
            return family
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def _mix(first: QColor, second: QColor, amount: float) -> QColor:
    return QColor(round(first.red() + (second.red() - first.red()) * amount),
                  round(first.green() + (second.green() - first.green()) * amount),
                  round(first.blue() + (second.blue() - first.blue()) * amount))


class TerminalView(QWidget):
    """A terminal on screen. The owner connects ``send`` to the child process and calls ``feed`` with its output."""

    send = Signal(object)                    # bytes for the child: keys, mouse reports, pasted text, answers
    grid_resized = Signal(int, int)          # columns, rows
    title_changed = Signal(str)
    cwd_changed = Signal(str)
    bell_rang = Signal()
    content_changed = Signal()               # something new was drawn
    menu_requested = Signal(object, str)     # global position, the address under the pointer or ""
    link_activated = Signal(str)
    clipboard_denied = Signal()              # a program tried to write the clipboard without permission
    zoom_changed = Signal(float)
    colours_changed = Signal()               # the scheme or a colour set by the program changed

    def __init__(self, parent: QWidget | None = None, *, cols: int = 80, rows: int = 24,
                 scrollback: int = settings_module.SCROLLBACK_DEFAULT) -> None:
        super().__init__(parent)
        self.term = Terminal(cols, rows, scrollback)
        term = self.term
        term.on_reply = self._reply
        term.on_title = self.title_changed.emit
        term.on_cwd = self.cwd_changed.emit
        term.on_bell = self._bell
        term.on_clipboard = self._clipboard_write
        term.on_clipboard_denied = self.clipboard_denied.emit
        term.on_colours = self._colours_changed
        term.colour_provider = self._colour_for_program
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self.setProperty("fluentFocusRing", False)        # the cursor shows where the keyboard goes
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setAccessibleName("Terminal")
        self.setAccessibleDescription("The screen of the terminal")
        # what the user chose
        self._family = ""
        self._font_size = settings_module.FONT_SIZE_DEFAULT
        self._zoom = 0.0
        self._scheme_key = schemes.DEFAULT_SCHEME
        self._cursor_shape = "bar"
        self._cursor_blinks = True
        self._bell_style = "visual"
        self.copy_on_select = False
        self.ctrl_c_copies = True
        self.paste_guard = None              # callable(text) -> bool: may this text be pasted?
        # geometry, in device pixels
        self._dpr = 1.0
        self._cell_w = 8
        self._cell_h = 16
        self._baseline = 12
        self._pad_x = PADDING
        self._pad_y = PADDING
        self._image: QImage | None = None
        self._fonts: list[QFont] = []
        self._cluster_fonts: list[QFont] = []
        self._metrics: QFontMetricsF | None = None
        self._line = 1
        self._underline_y = 14
        self._strike_y = 8
        self._char_kinds: dict[str, int] = {}
        self._advances: dict[tuple[str, int], float] = {}
        # colours
        self._scheme = schemes.get(self._scheme_key, fluent.manager().scheme, fluent.theme())
        self._styles: dict[tuple, tuple] = {}
        self._rgb: dict[int, QColor] = {}
        self._palette: list[QColor] = []
        self._fg = QColor()
        self._bg = QColor()
        self._cursor_colour = QColor()
        self._selection_bg = QColor()
        self._selection_fg = QColor()
        self._match_bg = QColor()
        self._match_fg = QColor()
        self._current_bg = QColor()
        self._current_fg = QColor()
        # view state
        self._scroll_offset = 0              # lines scrolled back from the bottom
        self._first = 0                      # index of the first line shown
        self._full = True                    # draw every row at the next frame
        self._painted_cursor: tuple | None = None
        self._blink_on = True
        self._flash = False
        self._last_bell = 0.0
        self._last_frame = 0.0
        self._sync_since = 0.0
        self._selection: grid.Selection | None = None       # in absolute lines
        self._selection_snapshot: dict[int, str] = {}
        self._anchor: tuple | None = None                   # the gesture: (mode, first position, last position)
        self._dragging = False
        self._pressed_link = ""
        self._clicks = 0
        self._click_time = 0.0
        self._click_cell = (-1, -1)
        self._mouse_cell = (-1, -1)
        self._mouse_buttons = 0
        self._wheel = 0.0
        self._matches: dict[int, list[tuple[int, int, bool]]] = {}
        self._hover_url = ""
        self._hover_spans: list[grid.Span] = []
        self._preedit = ""
        self._font_painter_state = (-1, None)
        self._scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self._scrollbar.setCursor(Qt.CursorShape.ArrowCursor)
        self._scrollbar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scrollbar.setAccessibleName("Scrollback")
        self._scrollbar.valueChanged.connect(self._scrollbar_moved)
        self._scrollbar.hide()
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.timeout.connect(self._frame)
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._blink)
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(50)
        self._drag_timer.timeout.connect(self._drag_scroll)
        self._drag_direction = 0
        fluent.manager().changed.connect(self._theme_changed)
        self._rebuild_fonts()
        self._rebuild_colours()
        term.dark = self._scheme.dark

    # --- configuration ------------------------------------------------------------------------------------
    def configure(self, profile: settings_module.Profile, options: settings_module.Settings) -> None:
        """Take the look and behaviour of a profile and the application's settings."""
        changed_font = (profile.font_family, profile.font_size) != (self._family, self._font_size)
        self._family = profile.font_family
        self._font_size = profile.font_size
        self._cursor_shape = profile.cursor_shape
        self._cursor_blinks = profile.cursor_blink
        self._bell_style = profile.bell
        self.copy_on_select = options.copy_on_select
        self.ctrl_c_copies = options.ctrl_c_copies
        self.term.allow_clipboard_write = options.clipboard_write
        self.term.set_scrollback(profile.scrollback)
        if profile.scheme != self._scheme_key:
            self._scheme_key = profile.scheme
            self._theme_changed()
        if changed_font:
            self._rebuild_fonts()
            self._layout()
        self._sync_blink()
        self._full = True
        self.schedule_frame()

    def scheme(self) -> schemes.Scheme:
        return self._scheme

    def background(self) -> QColor:
        return QColor(self._bg)

    def foreground(self) -> QColor:
        return QColor(self._fg)

    def _theme_changed(self, *_args) -> None:
        self._scheme = schemes.get(self._scheme_key, fluent.manager().scheme, fluent.theme())
        self._rebuild_colours()
        self.term.report_scheme(self._scheme.dark)
        self._full = True
        self.schedule_frame()

    def _colours_changed(self) -> None:
        self._rebuild_colours()
        self._full = True
        self.schedule_frame()

    def _rebuild_colours(self) -> None:
        scheme = self._scheme
        table = schemes.palette(scheme)
        overrides = self.term.colours
        for index, colour in overrides.items():
            if isinstance(index, int) and 0 <= index < 256:
                table[index] = colour
        self._palette = [QColor(*colour) for colour in table]
        fg = QColor(*overrides["fg"]) if "fg" in overrides else QColor(scheme.foreground)
        bg = QColor(*overrides["bg"]) if "bg" in overrides else QColor(scheme.background)
        if self.term.reverse_video:
            fg, bg = bg, fg
        self._fg, self._bg = fg, bg
        self._cursor_colour = QColor(*overrides["cursor"]) if "cursor" in overrides else QColor(scheme.cursor)
        self._selection_bg = QColor(scheme.selection)
        self._selection_fg = QColor(scheme.selection_text)
        # search matches: the scheme's yellow behind the match in focus, a tint of it behind the others
        yellow = QColor(*table[3])
        self._current_bg = yellow
        self._current_fg = QColor(scheme.background)
        self._match_bg = _mix(bg, yellow, 0.38)
        self._match_fg = QColor(fg)
        self._styles = {}
        self._rgb = {}
        self._reverse = self.term.reverse_video
        self.colours_changed.emit()

    def _colour_for_program(self, key: object) -> tuple[int, int, int] | None:
        """The colour a program asks about (OSC 4, 10, 11, 12)."""
        if key == "fg":
            colour = self._fg
        elif key == "bg":
            colour = self._bg
        elif key == "cursor":
            colour = self._cursor_colour
        elif isinstance(key, int) and 0 <= key < len(self._palette):
            colour = self._palette[key]
        else:
            return None
        return colour.red(), colour.green(), colour.blue()

    # --- fonts and geometry -------------------------------------------------------------------------------
    def font_family(self) -> str:
        return self._family or default_font_family()

    def font_points(self) -> float:
        """The font size in points, zoom included."""
        return max(settings_module.FONT_SIZE_MIN, min(self._font_size + self._zoom, settings_module.FONT_SIZE_MAX))

    def _rebuild_fonts(self) -> None:
        self._dpr = max(0.5, float(self.devicePixelRatioF()))
        device = QImage(1, 1, QImage.Format.Format_RGB32)
        base = QFont(self.font_family())
        base.setStyleHint(QFont.StyleHint.Monospace)
        base.setFixedPitch(True)
        base.setKerning(False)
        base.setPointSizeF(self.font_points() * self._dpr)
        metrics = QFontMetricsF(base, device)
        advance = metrics.horizontalAdvance("M")
        self._cell_w = max(1, round(advance))
        height = metrics.ascent() + metrics.descent()
        self._cell_h = max(1, math.ceil(height - 0.01))
        self._baseline = round((self._cell_h - height) / 2 + metrics.ascent())
        self._line = max(1, round(self._cell_h / 16))
        self._underline_y = min(self._cell_h - self._line, self._baseline + max(1, round(metrics.underlinePos())))
        self._strike_y = max(0, self._baseline - round(metrics.strikeOutPos()))
        self._metrics = metrics
        self._fonts = []
        self._cluster_fonts = []
        for bold, italic in ((False, False), (True, False), (False, True), (True, True)):
            font = QFont(base)
            font.setBold(bold)
            font.setItalic(italic)
            plain = QFont(font)
            own = QFontMetricsF(font, device).horizontalAdvance("M")
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, self._cell_w - own)
            font.setStyleStrategy(QFont.StyleStrategy.PreferNoShaping)
            self._fonts.append(font)
            self._cluster_fonts.append(plain)
        self._char_kinds = {}
        self._advances = {}
        self._pad_x = self._pad_y = round(PADDING * self._dpr)
        self.term.cell_width, self.term.cell_height = self._cell_w, self._cell_h

    def cell_size(self) -> tuple[int, int]:
        """Width and height of a cell in device pixels."""
        return self._cell_w, self._cell_h

    def grid_size(self) -> tuple[int, int]:
        return self.term.cols, self.term.rows

    def size_for(self, cols: int, rows: int) -> QSize:
        """The widget size (logical pixels) that shows a grid of this size."""
        bar = self._scrollbar.sizeHint().width()
        width = (cols * self._cell_w + 2 * self._pad_x) / self._dpr + bar
        height = (rows * self._cell_h + 2 * self._pad_y) / self._dpr
        return QSize(math.ceil(width), math.ceil(height))

    def sizeHint(self) -> QSize:
        return self.size_for(self.term.cols, self.term.rows)

    def minimumSizeHint(self) -> QSize:
        return self.size_for(4, 2)

    def _layout(self) -> None:
        """Fit the grid to the widget: called when the size, the font or the scale changes."""
        dpr = self._dpr
        bar = self._scrollbar.sizeHint().width()
        self._scrollbar.setGeometry(self.width() - bar, 0, bar, self.height())
        width = math.ceil(self.width() * dpr)
        height = math.ceil(self.height() * dpr)
        if width <= 0 or height <= 0:
            return
        cols = max(2, (math.floor((self.width() - bar) * dpr) - 2 * self._pad_x) // self._cell_w)
        rows = max(1, (math.floor(self.height() * dpr) - 2 * self._pad_y) // self._cell_h)
        term = self.term
        if (cols, rows) != (term.cols, term.rows):
            term.resize(cols, rows)
            self.clear_selection()
            self._matches = {}
            self._hover_spans = []
            self._hover_url = ""
            self._scroll_offset = min(self._scroll_offset, len(term.history))
            self.grid_resized.emit(term.cols, term.rows)
        if self._image is None or self._image.width() != width or self._image.height() != height:
            self._image = QImage(width, height, QImage.Format.Format_RGB32)
        self._full = True
        self.schedule_frame()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if abs(self.devicePixelRatioF() - self._dpr) > 1e-6:
            self._rebuild_fonts()
        self._layout()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if abs(self.devicePixelRatioF() - self._dpr) > 1e-6:
            self._rebuild_fonts()
        self._layout()
        self._sync_blink()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self._rebuild_fonts()
            self._layout()
        return super().event(event)

    # --- zoom ---
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        low = settings_module.FONT_SIZE_MIN - self._font_size
        high = settings_module.FONT_SIZE_MAX - self._font_size
        zoom = max(low, min(float(zoom), high))
        if zoom != self._zoom:
            self._zoom = zoom
            self._rebuild_fonts()
            self._layout()
            self.zoom_changed.emit(zoom)

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom + 1)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom - 1)

    def zoom_reset(self) -> None:
        self.set_zoom(0)

    # --- output -------------------------------------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        """Output of the child process."""
        try:
            self.term.feed(data)
        except Exception:                    # a bug in the emulator must not take the session down
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)
            self.term.recover()
        self.schedule_frame()

    def write_text(self, text: str) -> None:
        """Show text of the terminal's own (a message about the child process), starting on a fresh line."""
        term = self.term
        prefix = "" if term.x == 0 else "\r\n"
        term.feed_text("\x1b[0m" + prefix + text.replace("\n", "\r\n"))
        self.schedule_frame()

    def _reply(self, data: bytes) -> None:
        self.send.emit(data)

    def schedule_frame(self) -> None:
        if not self._frame_timer.isActive():
            elapsed = (time.monotonic() - self._last_frame) * 1000
            self._frame_timer.start(max(0, int(self.frame_ms() - elapsed)))

    def frame_ms(self) -> int:
        """Milliseconds between two redraws: one refresh of the screen the
        terminal is on (16 at 60 Hz, 6 at 144 Hz), so output appears as
        fast as the display shows it."""
        return max(1, int(1000 / fluent.refresh_rate(self)))

    def flush(self) -> None:
        """Draw what is pending now (tests, screenshots)."""
        self._frame_timer.stop()
        self._frame(force=True)

    def _frame(self, force: bool = False) -> None:
        term = self.term
        now = time.monotonic()
        if term.sync_output and not force:
            if not self._sync_since:
                self._sync_since = now
            if now - self._sync_since < SYNC_TIMEOUT:
                self._frame_timer.start(self.frame_ms())
                return
        self._sync_since = 0.0
        self._last_frame = now
        if self._image is None:
            return
        everything, rows, scrolled = term.take_dirty()
        changed = bool(everything or rows or scrolled)
        history = len(term.history)
        if term.alt_active:
            self._scroll_offset = 0
        elif self._scroll_offset:
            # stay on the lines the user is reading while new ones arrive below
            self._scroll_offset = min(self._scroll_offset + scrolled, history)
        if self._reverse != term.reverse_video:
            self._rebuild_colours()
            everything = True
        first = history - self._scroll_offset
        if first != self._first or scrolled or self._full:
            everything = True
        self._first = first
        self._full = False
        if changed and self._selection is not None:
            self._validate_selection()
        count = term.rows
        cursor = self._cursor_state()
        if everything:
            dirty = range(count)
        else:
            offset = self._scroll_offset
            marked = {row + offset for row in rows if row + offset < count}
            previous = self._painted_cursor
            if previous != cursor:
                if previous is not None and 0 <= previous[1] < count:
                    marked.add(previous[1])
                if cursor is not None and 0 <= cursor[1] < count:
                    marked.add(cursor[1])
            dirty = sorted(marked)
        if dirty:
            self._paint_rows(dirty, cursor)
            top = self._pad_y + dirty[0] * self._cell_h
            bottom = self._pad_y + (dirty[-1] + 1) * self._cell_h
            if everything:
                self.update()
            else:
                dpr = self._dpr
                self.update(0, math.floor(top / dpr), self.width(), math.ceil(bottom / dpr) - math.floor(top / dpr))
        self._painted_cursor = cursor
        self._sync_scrollbar()
        if changed:
            self.content_changed.emit()

    # --- painting -----------------------------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        if self._image is None:
            self._layout()
            if self._image is None:
                return
        if self._full:
            self.flush()
        painter = QPainter(self)
        painter.fillRect(event.rect(), self._bg)
        device = painter.deviceTransform()
        inverse, invertible = device.inverted()
        image = self._image
        if invertible:
            # Device pixels, one to one: the image is not scaled, whatever the scale factor of the screen.
            painter.setWorldTransform(QTransform.fromTranslate(round(device.dx()), round(device.dy())) * inverse)
            dpr = self._dpr
            rect = event.rect()
            left = max(0, math.floor(rect.left() * dpr))
            top = max(0, math.floor(rect.top() * dpr))
            right = min(image.width(), math.ceil((rect.right() + 1) * dpr))
            bottom = min(image.height(), math.ceil((rect.bottom() + 1) * dpr))
            if right > left and bottom > top:
                painter.drawImage(QPoint(left, top), image, QRect(left, top, right - left, bottom - top))
            if self._flash:
                tint = QColor(self._fg)
                tint.setAlphaF(0.16)
                painter.fillRect(0, 0, image.width(), image.height(), tint)
        painter.end()

    def _paint_rows(self, rows, cursor: tuple | None) -> None:
        image = self._image
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self._font_painter_state = (-1, None)
        term = self.term
        total = term.total_lines()
        dropped = term.history.dropped
        cols = term.cols
        width = image.width()
        cell_h = self._cell_h
        selection = self._selection
        matches = self._matches
        hover = self._hover_spans
        first_row = rows[0] if rows else 0
        if first_row == 0:
            painter.fillRect(0, 0, width, self._pad_y, self._bg)
        if rows and rows[-1] == term.rows - 1:
            below = self._pad_y + term.rows * cell_h
            painter.fillRect(0, below, width, image.height() - below, self._bg)
        for row in rows:
            y = self._pad_y + row * cell_h
            index = self._first + row
            painter.fillRect(0, y, width, cell_h, self._bg)
            if index >= total or index < 0:
                continue
            cells, runs, _wrapped = term.display_line(index)
            self._draw_cells(painter, y, cells, runs, 0, len(cells), None, None)
            absolute = index + dropped
            if matches:
                for start, end, current in matches.get(absolute, ()):
                    if current:
                        self._draw_cells(painter, y, cells, runs, start, min(end, cols), self._current_fg,
                                         self._current_bg)
                    else:
                        self._draw_cells(painter, y, cells, runs, start, min(end, cols), self._match_fg,
                                         self._match_bg)
            if selection is not None:
                span = selection.span(absolute, cols)
                if span is not None:
                    self._draw_cells(painter, y, cells, runs, span[0], span[1], self._selection_fg,
                                     self._selection_bg)
            for line, start, end in hover:
                if line == absolute:
                    painter.fillRect(self._pad_x + start * self._cell_w, y + self._underline_y,
                                     (min(end, cols) - start) * self._cell_w, self._line, self._fg)
            if cursor is not None and cursor[1] == row:
                self._draw_cursor(painter, y, cells, runs, cursor)
        painter.end()

    def _style(self, attr: tuple) -> tuple:
        """(foreground, background or None, font index, flags, underline colour or None, link) of an attribute."""
        fg_value, bg_value, flags, ul_value, link = attr
        fg = self._colour(fg_value) if fg_value else self._fg
        bg = self._colour(bg_value) if bg_value else None
        if flags & INVERSE:
            fg, bg = (bg if bg is not None else self._bg), fg
        if flags & DIM:
            fg = _mix(fg, bg if bg is not None else self._bg, 0.4)
        style = (fg, bg, (1 if flags & BOLD else 0) | (2 if flags & ITALIC else 0), flags,
                 self._colour(ul_value) if ul_value else None, link)
        if len(self._styles) > 8192:
            self._styles.clear()
        self._styles[attr] = style
        return style

    def _colour(self, value: int) -> QColor:
        if value & RGB:
            colour = self._rgb.get(value)
            if colour is None:
                if len(self._rgb) > 8192:
                    self._rgb.clear()
                colour = self._rgb[value] = QColor((value >> 16) & 255, (value >> 8) & 255, value & 255)
            return colour
        return self._palette[value & 255]

    def _draw_cells(self, painter: QPainter, y: int, cells, runs, first: int, last: int, fg_override: QColor | None,
                    bg_override: QColor | None) -> None:
        """Draw the cells [first, last) of a row at pixel row ``y``; the overrides replace the cells' own colours."""
        cell_w, cell_h, pad_x = self._cell_w, self._cell_h, self._pad_x
        base = y + self._baseline
        if bg_override is not None and last > first:
            painter.fillRect(pad_x + first * cell_w, y, (last - first) * cell_w, cell_h, bg_override)
        count = len(cells)
        if last > count:
            last = count
        if first >= last:
            return
        is_text = type(cells) is str
        styles = self._styles
        fonts = self._fonts
        font_now, pen_now = self._font_painter_state
        position = 0
        if runs is None:
            runs = (count, DEFAULT_ATTR)
        for at in range(0, len(runs), 2):
            start = position
            position += runs[at]
            if position <= first:
                continue
            if start >= last:
                break
            attr = runs[at + 1]
            style = styles.get(attr)
            if style is None:
                style = self._style(attr)
            fg, bg, font_index, flags, ul_colour, link = style
            if start < first:
                start = first
            end = position if position < last else last
            x = pad_x + start * cell_w
            if bg is not None and bg_override is None:
                painter.fillRect(x, y, (end - start) * cell_w, cell_h, bg)
            if flags & INVISIBLE:
                continue
            pen = fg_override if fg_override is not None else fg
            segment = cells[start:end]
            text = segment if is_text else "".join(segment)
            if text.isascii() and len(text) == end - start:
                text = text.rstrip(" ")
                if text:
                    if font_now != font_index:
                        painter.setFont(fonts[font_index])
                        font_now = font_index
                    if pen_now is not pen:
                        painter.setPen(pen)
                        pen_now = pen
                    painter.drawText(x, base, text)
            elif text.strip(" "):
                self._font_painter_state = (font_now, pen_now)
                self._draw_special(painter, x, y, segment, font_index, pen)
                font_now, pen_now = self._font_painter_state
            if flags & _DECORATED or link:
                self._decorate(painter, x, y, (end - start) * cell_w, flags, pen, ul_colour, link)
        self._font_painter_state = (font_now, pen_now)

    def _kind(self, char: str) -> int:
        """0: one cell wide and in the terminal font; 1: drawn as a shape; 2: anything else."""
        if boxdraw.handles(char):
            kind = 1
        elif wcwidth(char) == 1 and self._metrics is not None and self._metrics.inFontUcs4(ord(char)):
            kind = 0
        else:
            kind = 2
        if len(self._char_kinds) > 16384:
            self._char_kinds.clear()
        self._char_kinds[char] = kind
        return kind

    def _draw_special(self, painter: QPainter, x: int, y: int, segment, font_index: int, pen: QColor) -> None:
        """Cells that are not plain ASCII: runs of ordinary characters together, everything else cell by cell."""
        cell_w = self._cell_w
        base = y + self._baseline
        kinds = self._char_kinds
        font_now, pen_now = self._font_painter_state
        count = len(segment)
        run_start = -1
        run: list[str] = []

        def flush() -> None:
            nonlocal run_start, font_now, pen_now
            if run:
                if font_now != font_index:
                    painter.setFont(self._fonts[font_index])
                    font_now = font_index
                if pen_now is not pen:
                    painter.setPen(pen)
                    pen_now = pen
                painter.drawText(x + run_start * cell_w, base, "".join(run))
                run.clear()
            run_start = -1

        column = 0
        while column < count:
            cell = segment[column]
            if cell == " " or cell == "":
                if run:
                    run.append(" ")
                column += 1
                continue
            if len(cell) == 1:
                kind = kinds.get(cell)
                if kind is None:
                    kind = self._kind(cell)
                if kind == 0:
                    if not run:
                        run_start = column
                    run.append(cell)
                    column += 1
                    continue
                if kind == 1:
                    flush()
                    if boxdraw.draw(painter, cell, x + column * cell_w, y, cell_w, self._cell_h, pen):
                        column += 1
                        continue
            flush()
            span = 2 if column + 1 < count and segment[column + 1] == "" else 1
            if font_now != 4 + font_index:
                painter.setFont(self._cluster_fonts[font_index])
                font_now = 4 + font_index
            if pen_now is not pen:
                painter.setPen(pen)
                pen_now = pen
            self._draw_cluster(painter, x + column * cell_w, base, span * cell_w, cell, font_index)
            column += span
        flush()
        self._font_painter_state = (font_now, pen_now)

    def _draw_cluster(self, painter: QPainter, x: int, base: int, width: int, text: str, font_index: int) -> None:
        """One cell's characters from whatever font has them, fitted into ``width`` pixels."""
        key = (text, font_index)
        advance = self._advances.get(key)
        if advance is None:
            if len(self._advances) > 8192:
                self._advances.clear()
            advance = QFontMetricsF(self._cluster_fonts[font_index], self._image).horizontalAdvance(text)
            self._advances[key] = advance
        if advance <= width + 0.5:
            painter.drawText(QPointF(x + max(0.0, (width - advance) / 2), base), text)
        else:
            painter.save()
            painter.translate(x, 0)
            painter.scale(width / advance, 1.0)
            painter.drawText(QPointF(0, base), text)
            painter.restore()

    def _decorate(self, painter: QPainter, x: int, y: int, width: int, flags: int, pen: QColor,
                  ul_colour: QColor | None, link: int) -> None:
        line = self._line
        style = (flags & UNDERLINE_MASK) >> UNDERLINE_SHIFT
        under = y + self._underline_y
        colour = ul_colour if ul_colour is not None else pen
        if style == 1:
            painter.fillRect(x, under, width, line, colour)
        elif style == 2:
            upper = max(y, min(under - line, y + self._cell_h - 3 * line))
            painter.fillRect(x, upper, width, line, colour)
            painter.fillRect(x, upper + 2 * line, width, line, colour)
        elif style == 3:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(QRect(x, y, width, self._cell_h))
            painter.setPen(QPen(colour, line))
            height = max(1.0, min(2.0 * line, self._cell_h - self._underline_y - line / 2))
            period = max(4.0, self._cell_w / 1.0)
            path = QPainterPath(QPointF(x, under + line / 2))
            position = float(x)
            direction = 1
            while position < x + width:
                path.quadTo(position + period / 4, under + line / 2 + direction * height, position + period / 2,
                            under + line / 2)
                position += period / 2
                direction = -direction
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.restore()
            self._font_painter_state = (self._font_painter_state[0], None)
        elif style in (4, 5):
            on, off = (line, line) if style == 4 else (3 * line, 2 * line)
            position = x
            while position < x + width:
                painter.fillRect(position, under, min(on, x + width - position), line, colour)
                position += on + off
        elif link:
            position = x
            while position < x + width:                 # a hyperlink: dotted until the pointer is over it
                painter.fillRect(position, under, min(line, x + width - position), line, colour)
                position += 3 * line
        if flags & STRIKE:
            painter.fillRect(x, y + self._strike_y, width, line, pen)
        if flags & OVERLINE:
            painter.fillRect(x, y, width, line, pen)

    # --- cursor -------------------------------------------------------------------------------------------
    def _cursor_shape_now(self) -> tuple[str, bool]:
        """(shape, blinks) from what the program asked for, else from the profile."""
        style = self.term.cursor_style
        if style == 0:
            shape, blinks = self._cursor_shape, self._cursor_blinks
        else:
            shape = ("block", "block", "underline", "underline", "bar", "bar")[style - 1]
            blinks = style % 2 == 1
        if self.term.cursor_blink is not None and style == 0:
            blinks = self.term.cursor_blink
        return shape, blinks

    def _cursor_state(self) -> tuple | None:
        """What the cursor looks like now: (column, display row, shape, focused, preedit) or None when hidden."""
        term = self.term
        if not term.cursor_visible:
            return None
        row = term.y + self._scroll_offset
        if row >= term.rows:
            return None
        shape, blinks = self._cursor_shape_now()
        focused = self.hasFocus()
        if focused and blinks and not self._blink_on and not self._preedit:
            return None
        return (term.x, row, shape, focused, self._preedit)

    def _draw_cursor(self, painter: QPainter, y: int, cells, runs, cursor: tuple) -> None:
        column, _row, shape, focused, preedit = cursor
        cell_w, cell_h = self._cell_w, self._cell_h
        count = len(cells)
        if 0 < column < count and cells[column] == "":
            column -= 1
        span = 2 if column + 1 < count and cells[column + 1] == "" else 1
        x = self._pad_x + column * cell_w
        colour = self._cursor_colour
        if preedit:
            # text being composed by an input method: shown at the cursor, underlined
            position = x
            limit = self._pad_x + self.term.cols * cell_w
            painter.setPen(self._fg)
            self._font_painter_state = (-1, None)
            for text, width in clusters(preedit):
                if position + width * cell_w > limit:
                    break
                painter.fillRect(position, y, width * cell_w, cell_h, self._bg)
                painter.setFont(self._cluster_fonts[0])
                self._draw_cluster(painter, position, y + self._baseline, width * cell_w, text, 0)
                painter.fillRect(position, y + self._underline_y, width * cell_w, self._line, self._fg)
                position += width * cell_w
            painter.fillRect(position, y, max(1, round(1.5 * self._dpr)), cell_h, colour)
            return
        if not focused:
            line = self._line
            width = span * cell_w
            painter.fillRect(x, y, width, line, colour)
            painter.fillRect(x, y + cell_h - line, width, line, colour)
            painter.fillRect(x, y, line, cell_h, colour)
            painter.fillRect(x + width - line, y, line, cell_h, colour)
        elif shape == "block":
            self._draw_cells(painter, y, cells, runs, column, column + span, self._bg, colour)
            if column >= count:
                painter.fillRect(x, y, cell_w, cell_h, colour)
        elif shape == "underline":
            thickness = max(2, round(cell_h / 8))
            painter.fillRect(x, y + cell_h - thickness, span * cell_w, thickness, colour)
        else:
            painter.fillRect(x, y, max(1, round(1.5 * self._dpr)), cell_h, colour)

    def _sync_blink(self) -> None:
        _shape, blinks = self._cursor_shape_now()
        wanted = blinks and self.hasFocus() and self.isVisible() and self.term.cursor_visible
        if wanted and not self._blink_timer.isActive():
            interval = QApplication.cursorFlashTime() // 2
            if interval > 0:
                self._blink_timer.start(interval)
        elif not wanted and self._blink_timer.isActive():
            self._blink_timer.stop()
        if not wanted and not self._blink_on:
            self._blink_on = True
            self.schedule_frame()

    def _blink(self) -> None:
        _shape, blinks = self._cursor_shape_now()
        if not (blinks and self.hasFocus() and self.isVisible() and self.term.cursor_visible):
            self._sync_blink()
            return
        self._blink_on = not self._blink_on
        self.schedule_frame()

    def _show_cursor_now(self) -> None:
        """Typing restarts the blink with the cursor shown."""
        if not self._blink_on:
            self._blink_on = True
        if self._blink_timer.isActive():
            self._blink_timer.start()

    def is_blinking(self) -> bool:
        return self._blink_timer.isActive()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        if self.term.focus_events:
            self.send.emit(b"\x1b[I")
        self._blink_on = True
        self._sync_blink()
        self.schedule_frame()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if self.term.focus_events:
            self.send.emit(b"\x1b[O")
        self._preedit = ""
        self._sync_blink()
        self.schedule_frame()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._sync_blink()

    # --- bell ---
    def _bell(self) -> None:
        now = time.monotonic()
        if now - self._last_bell < BELL_INTERVAL:
            return
        self._last_bell = now
        self.bell_rang.emit()
        if self._bell_style == "visual" and self.isVisible() and fluent.duration("fast") > 0:
            self._flash = True
            self.update()
            QTimer.singleShot(FLASH_MS, self._end_flash)

    def _end_flash(self) -> None:
        self._flash = False
        self.update()

    def is_flashing(self) -> bool:
        return self._flash

    # --- scrolling ----------------------------------------------------------------------------------------
    def scroll_offset(self) -> int:
        """Lines the view is scrolled back from the bottom."""
        return self._scroll_offset

    def set_scroll_offset(self, offset: int) -> None:
        limit = 0 if self.term.alt_active else len(self.term.history)
        offset = max(0, min(int(offset), limit))
        if offset != self._scroll_offset:
            self._scroll_offset = offset
            self._full = True
            self.schedule_frame()

    def scroll_lines(self, lines: int) -> None:
        """Scroll back (positive) or forward (negative)."""
        self.set_scroll_offset(self._scroll_offset + lines)

    def scroll_pages(self, pages: int) -> None:
        self.scroll_lines(pages * max(1, self.term.rows - 1))

    def scroll_to_bottom(self) -> None:
        self.set_scroll_offset(0)

    def scroll_to_top(self) -> None:
        self.set_scroll_offset(len(self.term.history))

    def reveal(self, absolute: int) -> None:
        """Scroll so that the line with this absolute number is on screen (in the middle if it was not)."""
        term = self.term
        index = absolute - term.history.dropped
        history = len(term.history)
        first = history - self._scroll_offset
        if first <= index < first + term.rows:
            return
        self.set_scroll_offset(history - (index - term.rows // 2))

    def _sync_scrollbar(self) -> None:
        term = self.term
        history = 0 if term.alt_active else len(term.history)
        bar = self._scrollbar
        if history <= 0:
            if bar.isVisible():
                bar.hide()
            return
        bar.blockSignals(True)
        bar.setRange(0, history)
        bar.setPageStep(term.rows)
        bar.setSingleStep(1)
        bar.setValue(history - self._scroll_offset)
        bar.blockSignals(False)
        if not bar.isVisible():
            bar.show()

    def _scrollbar_moved(self, value: int) -> None:
        self.set_scroll_offset(len(self.term.history) - value)

    def scrollbar(self) -> QScrollBar:
        return self._scrollbar

    # --- positions ----------------------------------------------------------------------------------------
    def cell_at(self, position: QPointF | QPoint) -> tuple[int, int]:
        """(column, display row) of a point in the widget, clamped to the grid."""
        term = self.term
        column = int((position.x() * self._dpr - self._pad_x) // self._cell_w)
        row = int((position.y() * self._dpr - self._pad_y) // self._cell_h)
        return max(0, min(column, term.cols - 1)), max(0, min(row, term.rows - 1))

    def cell_rect(self, column: int, row: int) -> QRect:
        """The logical rectangle of a cell (tests, the input method)."""
        dpr = self._dpr
        x = (self._pad_x + column * self._cell_w) / dpr
        y = (self._pad_y + row * self._cell_h) / dpr
        return QRect(math.floor(x), math.floor(y), max(1, round(self._cell_w / dpr)),
                     max(1, round(self._cell_h / dpr)))

    def _absolute(self, row: int) -> int:
        """The absolute line number shown in a display row."""
        return self._first + row + self.term.history.dropped

    def _index(self, absolute: int) -> int:
        return absolute - self.term.history.dropped

    # --- selection ----------------------------------------------------------------------------------------
    def has_selection(self) -> bool:
        return self._selection is not None

    def selection(self) -> grid.Selection | None:
        return self._selection

    def _indexed_selection(self) -> grid.Selection | None:
        selection = self._selection
        if selection is None:
            return None
        dropped = self.term.history.dropped
        return grid.Selection((selection.start[0] - dropped, selection.start[1]),
                              (selection.end[0] - dropped, selection.end[1]), selection.block)

    def selected_text(self) -> str:
        selection = self._indexed_selection()
        return grid.selection_text(self.term, selection) if selection is not None else ""

    def set_selection(self, selection: grid.Selection | None) -> None:
        """Select cells (absolute line numbers) or nothing."""
        if selection == self._selection:
            return
        self._selection = selection
        self._selection_snapshot = {}
        if selection is not None:
            term = self.term
            dropped = term.history.dropped
            screen = dropped + len(term.history)
            total = term.total_lines()
            for absolute in range(max(selection.start[0], screen), selection.end[0] + 1):
                index = absolute - dropped
                if index < total:
                    self._selection_snapshot[absolute] = term.line_text(index)
        self._full = True
        self.schedule_frame()

    def clear_selection(self) -> None:
        self._anchor = None
        self.set_selection(None)

    def select_all(self) -> None:
        term = self.term
        dropped = term.history.dropped
        last = term.total_lines() - 1
        while last > 0 and not term.line_text(last):
            last -= 1
        self.set_selection(grid.Selection((dropped, 0), (dropped + last, term.cols - 1)))
        self._selection_made()

    def _validate_selection(self) -> None:
        """Forget the selection when the text under it has changed."""
        term = self.term
        dropped = term.history.dropped
        screen = dropped + len(term.history)
        total = term.total_lines()
        selection = self._selection
        if selection is None:
            return
        if selection.start[0] < dropped:
            self.clear_selection()
            return
        snapshot = self._selection_snapshot
        for absolute in list(snapshot):
            if absolute < screen:
                del snapshot[absolute]             # in the scrollback now: it cannot change any more
                continue
            index = absolute - dropped
            if index >= total or term.line_text(index) != snapshot[absolute]:
                self.clear_selection()
                return

    def _selection_made(self) -> None:
        text = self.selected_text()
        if not text:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard.supportsSelection():
            clipboard.setText(text, clipboard.Mode.Selection)
        if self.copy_on_select:
            clipboard.setText(text)

    def copy(self) -> bool:
        """Put the selected text on the clipboard; False when nothing is selected."""
        text = self.selected_text()
        if not text:
            return False
        QGuiApplication.clipboard().setText(text)
        return True

    def paste(self) -> None:
        self.paste_text(QGuiApplication.clipboard().text())

    def paste_primary(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard.supportsSelection():
            self.paste_text(clipboard.text(clipboard.Mode.Selection))
        else:
            self.paste_text(clipboard.text())

    def paste_text(self, text: str) -> None:
        """Send text to the program as a paste (asking ``paste_guard`` first)."""
        if not text:
            return
        if self.paste_guard is not None and not self.paste_guard(text):
            return
        self.send.emit(keys.encode_paste(text, self.term.bracketed_paste))
        self.scroll_to_bottom()

    def _clipboard_write(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)

    def _extend_selection(self, column: int, row: int, block: bool) -> None:
        anchor = self._anchor
        if anchor is None:
            return
        mode, first, last = anchor
        term = self.term
        position = (self._absolute(row), column)
        dropped = term.history.dropped
        if mode == "word":
            start, end = grid.word_at(term, position[0] - dropped, column)
            start, end = (start[0] + dropped, start[1]), (end[0] + dropped, end[1])
            self.set_selection(grid.Selection(min(first, start), max(last, end)))
        elif mode == "line":
            low, high = grid.line_range(term, position[0] - dropped)
            self.set_selection(grid.Selection(min(first, (low + dropped, 0)),
                                              max(last, (high + dropped, term.cols - 1))))
        elif position == first and not self._dragging:
            self.set_selection(None)
        else:
            self.set_selection(grid.Selection.between(first, position, block) if not block
                               else grid.Selection(first, position, True))

    def _drag_scroll(self) -> None:
        if not self._dragging or not self._drag_direction:
            self._drag_timer.stop()
            return
        self.scroll_lines(self._drag_direction)
        self._frame(force=True)
        position = self.mapFromGlobal(QCursor.pos())
        column, row = self.cell_at(position)
        self._extend_selection(column, row, self._anchor is not None and self._anchor[0] == "block")

    # --- links ---
    def link_at(self, column: int, row: int) -> tuple[str, list[grid.Span]]:
        """The address at a display cell and the cells it covers (absolute lines): an OSC 8 hyperlink, else an
        address written in the text."""
        term = self.term
        index = self._first + row
        dropped = term.history.dropped
        if not 0 <= index < term.total_lines():
            return "", []
        cells, runs, _wrapped = term.display_line(index)
        if runs is not None:
            position = 0
            for at in range(0, len(runs), 2):
                start = position
                position += runs[at]
                if start <= column < position:
                    link = runs[at + 1][4]
                    if link and term.links.get(link):
                        return term.links[link], [(index + dropped, start, position)]
                    break
        found = grid.url_at(term, index, column)
        if found is None:
            return "", []
        url, spans = found
        return url, [(line + dropped, start, end) for line, start, end in spans]

    def _set_hover(self, url: str, spans: list[grid.Span]) -> None:
        if spans != self._hover_spans:
            self._hover_spans = spans
            self._hover_url = url
            self._full = True
            self.schedule_frame()
            self.setToolTip(f"{url}\nCtrl+click to open" if url else "")
        self._update_pointer()

    def _update_pointer(self) -> None:
        reporting = self.term.mouse_mode != MOUSE_OFF
        control = bool(QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier)
        if self._hover_url and control:
            shape = Qt.CursorShape.PointingHandCursor
        elif reporting:
            shape = Qt.CursorShape.ArrowCursor
        else:
            shape = Qt.CursorShape.IBeamCursor
        if self.cursor().shape() != shape:
            self.setCursor(shape)

    def hovered_link(self) -> str:
        return self._hover_url

    # --- search ---
    def set_matches(self, matches: list[list[grid.Span]], current: int = -1) -> None:
        """Highlight search matches (spans in absolute lines); ``current`` is the index of the one in focus."""
        table: dict[int, list[tuple[int, int, bool]]] = {}
        for number, match in enumerate(matches):
            for line, start, end in match:
                table.setdefault(line, []).append((start, end, number == current))
        self._matches = table
        self._full = True
        self.schedule_frame()

    def snapshot(self) -> tuple[list, int]:
        """Every line now (scrollback and screen) and the absolute number of the first: for a search."""
        term = self.term
        return list(term.iter_lines()), term.history.dropped

    # --- mouse --------------------------------------------------------------------------------------------
    def _modifiers(self, event) -> int:
        modifiers = event.modifiers()
        return ((keys.SHIFT if modifiers & Qt.KeyboardModifier.ShiftModifier else 0)
                | (keys.ALT if modifiers & Qt.KeyboardModifier.AltModifier else 0)
                | (keys.CTRL if modifiers & Qt.KeyboardModifier.ControlModifier else 0))

    def _reports_mouse(self, event) -> bool:
        return (self.term.mouse_mode != MOUSE_OFF and not self._scroll_offset
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

    def _report(self, kind: str, button: int, column: int, row: int, modifiers: int) -> None:
        term = self.term
        data = keys.encode_mouse(kind, button, column, row, modifiers & ~keys.SHIFT, mode=term.mouse_mode,
                                 encoding=term.mouse_encoding)
        if data:
            self.send.emit(data)

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        column, row = self.cell_at(event.position())
        button = event.button()
        if self._reports_mouse(event):
            if button in _BUTTONS:
                self._mouse_buttons |= 1 << _BUTTONS[button]
                self._report("press", _BUTTONS[button], column, row, self._modifiers(event))
            event.accept()
            return
        if button == Qt.MouseButton.MiddleButton:
            self.paste_primary()
        elif button == Qt.MouseButton.LeftButton:
            self._left_press(event, column, row)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        self.mousePressEvent(event)

    def _left_press(self, event, column: int, row: int) -> None:
        now = time.monotonic()
        interval = QApplication.doubleClickInterval() / 1000
        if now - self._click_time <= interval and self._click_cell == (column, row):
            self._clicks = self._clicks % 3 + 1
        else:
            self._clicks = 1
        self._click_time = now
        self._click_cell = (column, row)
        modifiers = event.modifiers()
        term = self.term
        dropped = term.history.dropped
        position = (self._absolute(row), column)
        self._pressed_link = ""
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            self._pressed_link = self.link_at(column, row)[0]
            if self._pressed_link:
                # Ctrl+click on an address opens it (on release, unless the pointer was dragged away)
                self._clicks = 1
                self._dragging = True
                self._anchor = ("char", position, position)
                self.set_selection(None)
                return
        self._dragging = True
        if modifiers & Qt.KeyboardModifier.ShiftModifier and self._anchor is not None and self._clicks == 1:
            self._extend_selection(column, row, self._anchor[0] == "block")
            return
        if self._clicks == 2:
            start, end = grid.word_at(term, position[0] - dropped, column)
            start, end = (start[0] + dropped, start[1]), (end[0] + dropped, end[1])
            self._anchor = ("word", start, end)
            self.set_selection(grid.Selection(start, end))
        elif self._clicks == 3:
            low, high = grid.line_range(term, position[0] - dropped)
            start, end = (low + dropped, 0), (high + dropped, term.cols - 1)
            self._anchor = ("line", start, end)
            self.set_selection(grid.Selection(start, end))
        else:
            block = bool(modifiers & Qt.KeyboardModifier.AltModifier)
            self._anchor = ("block" if block else "char", position, position)
            self.set_selection(None)

    def mouseMoveEvent(self, event) -> None:
        column, row = self.cell_at(event.position())
        term = self.term
        if self._mouse_buttons or (term.mouse_mode == MOUSE_ANY and self._reports_mouse(event)
                                   and not event.buttons()):
            if (column, row) != self._mouse_cell and term.mouse_mode in (MOUSE_BUTTON, MOUSE_ANY):
                self._mouse_cell = (column, row)
                button = next((number for number in range(3) if self._mouse_buttons & (1 << number)), 3)
                self._report("move", button, column, row, self._modifiers(event))
            return
        if self._dragging and event.buttons() & Qt.MouseButton.LeftButton and self._anchor is not None:
            y = event.position().y()
            self._drag_direction = 1 if y < 0 else -1 if y > self.height() else 0
            if self._drag_direction and not self._drag_timer.isActive():
                self._drag_timer.start()
            if (column, row) != self._mouse_cell or self._selection is None:
                self._mouse_cell = (column, row)
                self._pressed_link = ""
                self._extend_selection(column, row, self._anchor[0] == "block")
            return
        if (column, row) != self._mouse_cell:
            self._mouse_cell = (column, row)
            inside = self.rect().contains(event.position().toPoint())
            url, spans = self.link_at(column, row) if inside else ("", [])
            self._set_hover(url, spans)
        else:
            self._update_pointer()

    def mouseReleaseEvent(self, event) -> None:
        column, row = self.cell_at(event.position())
        button = event.button()
        if button in _BUTTONS and self._mouse_buttons & (1 << _BUTTONS[button]):
            self._mouse_buttons &= ~(1 << _BUTTONS[button])
            self._report("release", _BUTTONS[button], column, row, self._modifiers(event))
            return
        if button != Qt.MouseButton.LeftButton:
            return
        was_dragging = self._dragging
        self._dragging = False
        self._drag_timer.stop()
        self._drag_direction = 0
        if self._pressed_link and self._selection is None:
            link, self._pressed_link = self._pressed_link, ""
            self.link_activated.emit(link)
            return
        if was_dragging and self._selection is not None:
            self._selection_made()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._mouse_cell = (-1, -1)
        self._set_hover("", [])

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        modifiers = event.modifiers()
        term = self.term
        self._wheel += delta
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            steps = int(self._wheel / 120)
            if steps:
                self._wheel -= steps * 120
                self.set_zoom(self._zoom + steps)
        elif self._reports_mouse(event):
            steps = int(self._wheel / 120)
            if steps:
                self._wheel -= steps * 120
                column, row = self.cell_at(event.position())
                for _ in range(min(abs(steps), 10)):
                    self._report("press", 4 if steps > 0 else 5, column, row, self._modifiers(event))
        elif term.alt_active and term.alt_scroll and not modifiers & Qt.KeyboardModifier.ShiftModifier:
            lines = int(self._wheel / (120 / WHEEL_LINES))
            if lines:
                self._wheel -= lines * (120 / WHEEL_LINES)
                key = keys.encode_key("Up" if lines > 0 else "Down", app_cursor=term.app_cursor)
                self.send.emit(key * min(abs(lines), 30))
        else:
            lines = int(self._wheel / (120 / WHEEL_LINES))
            if lines:
                self._wheel -= lines * (120 / WHEEL_LINES)
                self.scroll_lines(lines)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        if event.reason() == event.Reason.Mouse and self.term.mouse_mode != MOUSE_OFF \
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier and not self._scroll_offset:
            event.accept()
            return
        if event.reason() == event.Reason.Mouse:
            column, row = self.cell_at(event.pos())
            link = self.link_at(column, row)[0]
            position = event.globalPos()
        else:
            link = ""
            rect = self.cell_rect(self.term.x, self.term.y)
            position = self.mapToGlobal(rect.bottomLeft())
        self.menu_requested.emit(position, link)
        event.accept()

    # --- keyboard -----------------------------------------------------------------------------------------
    def focusNextPrevChild(self, _next: bool) -> bool:
        return False                                 # Tab belongs to the program in the terminal

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in _MODIFIER_KEYS:
            if key == Qt.Key.Key_Control:
                self._update_pointer()
            return
        modifiers = event.modifiers()
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        term = self.term
        if shift and not alt and not term.alt_active:
            if key == Qt.Key.Key_PageUp and not control:
                self.scroll_pages(1)
                return
            if key == Qt.Key.Key_PageDown and not control:
                self.scroll_pages(-1)
                return
            if control and key == Qt.Key.Key_Up:
                self.scroll_lines(1)
                return
            if control and key == Qt.Key.Key_Down:
                self.scroll_lines(-1)
                return
            if control and key == Qt.Key.Key_Home:
                self.scroll_to_top()
                return
            if control and key == Qt.Key.Key_End:
                self.scroll_to_bottom()
                return
        if shift and not control and not alt and key == Qt.Key.Key_Insert:
            self.paste()
            return
        if control and not shift and not alt and key == Qt.Key.Key_Insert:
            self.copy()
            return
        if control and not shift and not alt and key == Qt.Key.Key_C and self.ctrl_c_copies \
                and self._selection is not None:
            self.copy()
            self.clear_selection()
            return
        data = self._encode(event)
        if not data:
            return
        self.send.emit(data)
        if self._selection is not None:
            self.clear_selection()
        self.scroll_to_bottom()
        self._show_cursor_now()

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Control:
            self._update_pointer()

    def _encode(self, event) -> bytes:
        key = event.key()
        modifiers = self._modifiers(event)
        term = self.term
        text = event.text()
        name = _KEY_NAMES.get(key)
        keypad = bool(event.modifiers() & Qt.KeyboardModifier.KeypadModifier)
        if name is not None:
            if name == "KP_Enter" and not keypad:
                name = "Enter"
            return keys.encode_key(name, modifiers, app_cursor=term.app_cursor, app_keypad=term.app_keypad,
                                   newline_mode=term.lnm)
        # With Num Lock on, the numeric keypad types its digits and signs in every mode, as it does in
        # xterm (whose terminfo entry has no codes for them); only Enter follows the keypad mode.
        base = chr(key).lower() if 0x20 <= key < 0x7F else ""
        if key == Qt.Key.Key_Space:
            base = " "
        if not base and not text:
            return b""
        return keys.encode_key(base or text, modifiers, text, app_cursor=term.app_cursor,
                               app_keypad=term.app_keypad, newline_mode=term.lnm)

    def inputMethodEvent(self, event) -> None:
        commit = event.commitString()
        if commit:
            self.send.emit(commit.encode("utf-8", "replace"))
            self.scroll_to_bottom()
        self._preedit = event.preeditString()[:64]
        self._full = True
        self.schedule_frame()
        event.accept()

    def inputMethodQuery(self, query):
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            return self.cell_rect(self.term.x, min(self.term.y + self._scroll_offset, self.term.rows - 1))
        if query == Qt.InputMethodQuery.ImEnabled:
            return True
        return super().inputMethodQuery(query)
