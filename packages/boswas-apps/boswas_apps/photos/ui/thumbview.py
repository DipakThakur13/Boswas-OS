"""Thumbnail views: the filmstrip of the viewer and the grid of the gallery.

Both paint themselves and are *virtualised*: they hold a plain list of
items, compute where each one lies by arithmetic, paint only what is
visible and ask the ``ThumbnailProvider`` only for the thumbnails on
screen (and a little ahead). 50 000 pictures cost a list of 50 000 small
records and nothing per frame.
"""

from __future__ import annotations

import bisect

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QScrollBar, QSizePolicy, QToolTip, QWidget

import boswas_fluent as fluent
from boswas_fluent import paint

from ..library import Item, Section
from ..thumbs import ThumbnailProvider


def draw_cover(painter: QPainter, rect: QRectF, pixmap: QPixmap, radius: float) -> None:
    """Draw ``pixmap`` so that it covers ``rect`` (centre crop), with round corners."""
    if pixmap.isNull() or rect.isEmpty():
        return
    width, height = pixmap.width(), pixmap.height()
    scale = max(rect.width() / width, rect.height() / height)
    source_w, source_h = rect.width() / scale, rect.height() / scale
    source = QRectF((width - source_w) / 2, (height - source_h) / 2, source_w, source_h)
    if pixmap.hasAlphaChannel():
        paint.rounded_rect(painter, rect, radius, "fill-control")      # something behind a transparent picture
    painter.save()
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.setClipPath(path)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawPixmap(rect, pixmap, source)
    painter.restore()


def draw_placeholder(painter: QPainter, rect: QRectF, radius: float, failed: bool) -> None:
    paint.rounded_rect(painter, rect, radius, "fill-control")
    if failed:
        side = min(24.0, rect.width() / 2, rect.height() / 2)
        icon = fluent.icon("image-x-generic")
        box = QRect(round(rect.center().x() - side / 2), round(rect.center().y() - side / 2), round(side), round(side))
        painter.save()
        painter.setOpacity(0.5)
        icon.paint(painter, box)
        painter.restore()


class Filmstrip(QWidget):
    """The pictures of a folder in a row; the current one is marked and kept in view."""

    activated = Signal(int)
    CELL = QSize(72, 54)
    GAP = 4
    PADDING = 8

    def __init__(self, provider: ThumbnailProvider, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._items: list[Item] = []
        self._current = -1
        self._scroll = 0.0
        self._hover = -1
        self._visible: dict[str, int] = {}
        self._animation: QVariantAnimation | None = None
        self.setFixedHeight(self.CELL.height() + 2 * self.PADDING)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Pictures in this folder")
        self.setAccessibleDescription("Use the Left and Right arrow keys to go to another picture")
        provider.ready.connect(self._thumbnail_changed)
        provider.failed.connect(self._thumbnail_changed)

    # --- content ---
    def items(self) -> list[Item]:
        return self._items

    def count(self) -> int:
        return len(self._items)

    def current(self) -> int:
        return self._current

    def set_items(self, items: list[Item], current: int = -1) -> None:
        self._items = list(items)
        self._current = current if 0 <= current < len(self._items) else -1
        self._hover = -1
        self._stop()
        self._scroll = self._centred(self._current)
        self._want()
        self.update()

    def set_current(self, index: int, animate: bool = True) -> None:
        if not 0 <= index < len(self._items):
            index = -1
        self._current = index
        target = self._centred(index)
        self._stop()
        time = fluent.duration("normal") if animate and self.isVisible() else 0
        if time <= 0 or abs(target - self._scroll) < 1:
            self._scroll = target
            self._want()
        else:
            animation = QVariantAnimation(self)
            animation.setStartValue(float(self._scroll))
            animation.setEndValue(float(target))
            animation.setDuration(time)
            animation.setEasingCurve(fluent.easing("decelerate"))
            animation.valueChanged.connect(self._set_scroll)
            animation.finished.connect(self._want)
            self._animation = animation
            animation.start()
        self.update()

    def _stop(self) -> None:
        if self._animation is not None:
            self._animation.stop()
            self._animation = None

    def _set_scroll(self, value) -> None:
        self._scroll = float(value)
        self.update()

    # --- geometry ---
    def _pitch(self) -> int:
        return self.CELL.width() + self.GAP

    def _content_width(self) -> int:
        return max(0, len(self._items) * self._pitch() - self.GAP)

    def _limit(self, scroll: float) -> float:
        content = self._content_width()
        if content <= self.width():
            return -(self.width() - content) / 2          # everything fits: centred
        return max(-float(self.PADDING), min(content - self.width() + self.PADDING, scroll))

    def _centred(self, index: int) -> float:
        if index < 0:
            return self._limit(0.0)
        return self._limit(index * self._pitch() + self.CELL.width() / 2 - self.width() / 2)

    def cell_rect(self, index: int) -> QRect:
        return QRect(round(index * self._pitch() - self._scroll), self.PADDING, self.CELL.width(), self.CELL.height())

    def index_at(self, point: QPoint) -> int:
        if not self.PADDING <= point.y() < self.PADDING + self.CELL.height():
            return -1
        position = point.x() + self._scroll
        index = int(position // self._pitch())
        if position < 0 or position - index * self._pitch() >= self.CELL.width():
            return -1
        return index if 0 <= index < len(self._items) else -1

    def visible_range(self) -> range:
        if not self._items:
            return range(0)
        first = max(0, int(self._scroll // self._pitch()))
        last = min(len(self._items) - 1, int((self._scroll + self.width()) // self._pitch()))
        return range(first, last + 1)

    def fluent_focus_rect(self) -> tuple[QWidget, QRect]:
        if self._current >= 0:
            return self, self.cell_rect(self._current)
        return self, self.rect().adjusted(2, 2, -2, -2)

    # --- thumbnails ---
    def _want(self) -> None:
        shown = self.visible_range()
        if not shown:
            self._visible = {}
            self._provider.forget(self)
            return
        ahead = range(max(0, shown.start - 6), min(len(self._items), shown.stop + 6))
        centre = self._current if self._current in ahead else (shown.start + shown.stop) // 2
        order = sorted(ahead, key=lambda index: abs(index - centre))
        self._visible = {self._items[index].path: index for index in ahead}
        self._provider.want(self, [(self._items[i].path, self._items[i].mtime, self._items[i].size) for i in order])

    def _thumbnail_changed(self, path: str) -> None:
        index = self._visible.get(path)
        if index is not None:
            self.update(self.cell_rect(index).adjusted(-3, -3, 3, 3))

    # --- events ---
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._stop()
        self._scroll = self._centred(self._current)
        self._want()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._scroll = self._centred(self._current)
        self._want()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._provider.forget(self)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self._stop()
            self._scroll = self._limit(self._scroll - delta)
            self._want()
            self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        hover = self.index_at(event.position().toPoint())
        if hover != self._hover:
            self._hover = hover
            self.setToolTip(self._items[hover].name if hover >= 0 else "")
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.index_at(event.position().toPoint())
            if index >= 0:
                self.activated.emit(index)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        radius = fluent.theme().radius("control")
        for index in self.visible_range():
            item = self._items[index]
            rect = QRectF(self.cell_rect(index))
            pixmap = self._provider.pixmap(item.path, item.mtime)
            if pixmap is not None:
                draw_cover(painter, rect, pixmap, radius)
            else:
                draw_placeholder(painter, rect, radius, self._provider.has_failed(item.path, item.mtime))
            if index == self._current:
                paint.rounded_rect(painter, rect.adjusted(-3, -3, 3, 3), radius + 3, None, "accent", 2)
            elif index == self._hover:
                paint.rounded_rect(painter, rect, radius, "fill-subtle-hover", "stroke-strong")


class ThumbGrid(QWidget):
    """A grid of thumbnails under optional section headings (months), with selection.

    Mouse: a click opens the picture (``activated``); the round mark in the
    corner of a thumbnail, Ctrl+click and Shift+click select; once
    something is selected a plain click selects too, as on a phone.
    Keyboard: arrows, Home, End, Page Up and Page Down move, Enter opens,
    Space selects, Shift+arrows extend, Ctrl+A selects all, Escape clears.
    """

    activated = Signal(int)
    selection_changed = Signal()
    context_requested = Signal(QPoint, int)
    TARGET_CELL = 172
    GAP = 4
    HEADER = 52
    MARGIN = 32               # the side margin of a NavigationView page
    MARK = 20                 # the selection mark in the corner of a thumbnail

    def __init__(self, provider: ThumbnailProvider, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._items: list[Item] = []
        self._sections: list[Section] = []
        self._starts: list[int] = []          # first item of each section
        self._tops: list[float] = []          # top of each section
        self._heights: list[float] = []
        self._columns = 1
        self._cell = float(self.TARGET_CELL)
        self._total = 0.0
        self._selected: set[int] = set()
        self._cursor = -1
        self._anchor = -1
        self._hover = -1
        self._visible: dict[str, int] = {}
        self.scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self.scrollbar.valueChanged.connect(self._scrolled)
        self.scrollbar.setAccessibleName("Scroll the pictures")
        self._want_timer = QTimer(self)
        self._want_timer.setSingleShot(True)
        self._want_timer.setInterval(0)
        self._want_timer.timeout.connect(self._want)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Pictures")
        self.setAccessibleDescription("Arrow keys move, Enter opens, Space selects")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        provider.ready.connect(self._thumbnail_changed)
        provider.failed.connect(self._thumbnail_changed)

    # --- content ---
    def set_items(self, items: list[Item], sections: list[Section] | None = None) -> None:
        """Show ``items``; ``sections`` (headings over runs of items) or None for one run without heading.

        The selection is cleared; the scroll position goes back to the top.
        """
        self._items = items
        self._sections = list(sections) if sections else ([Section("", 0, len(items))] if items else [])
        self._selected.clear()
        self._cursor = 0 if items else -1
        self._anchor = self._cursor
        self._hover = -1
        self._layout()
        self.scrollbar.setValue(0)
        self._want_timer.start()
        self.selection_changed.emit()
        self.update()

    def items(self) -> list[Item]:
        return self._items

    def count(self) -> int:
        return len(self._items)

    def item(self, index: int) -> Item:
        return self._items[index]

    # --- selection ---
    def selected(self) -> list[int]:
        return sorted(self._selected)

    def selected_items(self) -> list[Item]:
        return [self._items[index] for index in sorted(self._selected)]

    def set_selected(self, indexes) -> None:
        chosen = {index for index in indexes if 0 <= index < len(self._items)}
        if chosen != self._selected:
            self._selected = chosen
            self.selection_changed.emit()
            self.update()

    def select_all(self) -> None:
        self.set_selected(range(len(self._items)))

    def clear_selection(self) -> None:
        self.set_selected(())

    def toggle(self, index: int) -> None:
        if 0 <= index < len(self._items):
            chosen = set(self._selected)
            chosen.symmetric_difference_update({index})
            self._anchor = index
            self.set_selected(chosen)

    def cursor_index(self) -> int:
        return self._cursor

    def set_cursor_index(self, index: int, *, extend: bool = False) -> None:
        if not self._items:
            return
        index = max(0, min(len(self._items) - 1, index))
        self._cursor = index
        if extend:
            start = self._anchor if self._anchor >= 0 else index
            self.set_selected(range(min(start, index), max(start, index) + 1))
        else:
            self._anchor = index
        self.ensure_visible(index)
        self.update()

    # --- geometry ---
    def _content_width(self) -> int:
        return max(1, self.width() - 2 * self.MARGIN)

    def _layout(self) -> None:
        width = self._content_width()
        self._columns = max(1, (width + self.GAP) // (self.TARGET_CELL + self.GAP))
        self._cell = (width - (self._columns - 1) * self.GAP) / self._columns
        pitch = self._cell + self.GAP
        self._tops, self._heights = [], []
        self._starts = [section.start for section in self._sections]
        y = 0.0
        for section in self._sections:
            rows = -(-section.count // self._columns)
            head = self.HEADER if section.title else 0
            height = head + rows * pitch
            self._tops.append(y)
            self._heights.append(height)
            y += height
        self._total = y + 16
        page = max(1, self.height())
        self.scrollbar.setRange(0, max(0, round(self._total) - page))
        self.scrollbar.setPageStep(page)
        self.scrollbar.setSingleStep(max(16, round(pitch / 3)))
        self.scrollbar.setGeometry(self.width() - self.scrollbar.sizeHint().width() - 2, 0,
                                   self.scrollbar.sizeHint().width(), self.height())
        self.scrollbar.setVisible(self._total > page)

    def columns(self) -> int:
        return self._columns

    def content_height(self) -> float:
        return self._total

    def _section_of(self, index: int) -> int:
        return max(0, bisect.bisect_right(self._starts, index) - 1)

    def item_rect(self, index: int) -> QRectF:
        """Where item ``index`` lies in the widget now."""
        number = self._section_of(index)
        section = self._sections[number]
        local = index - section.start
        row, column = divmod(local, self._columns)
        pitch = self._cell + self.GAP
        head = self.HEADER if section.title else 0
        return QRectF(self.MARGIN + column * pitch, self._tops[number] + head + row * pitch - self.scrollbar.value(),
                      self._cell, self._cell)

    def header_rect(self, number: int) -> QRectF:
        return QRectF(self.MARGIN, self._tops[number] - self.scrollbar.value(), self._content_width(), self.HEADER)

    def index_at(self, point: QPoint) -> int:
        if not self._items:
            return -1
        y = point.y() + self.scrollbar.value()
        x = point.x() - self.MARGIN
        if x < 0 or y < 0:
            return -1
        number = bisect.bisect_right(self._tops, y) - 1
        if number < 0 or y >= self._tops[number] + self._heights[number]:
            return -1
        section = self._sections[number]
        pitch = self._cell + self.GAP
        local_y = y - self._tops[number] - (self.HEADER if section.title else 0)
        if local_y < 0:
            return -1
        row, column = int(local_y // pitch), int(x // pitch)
        if column >= self._columns or x - column * pitch >= self._cell or local_y - row * pitch >= self._cell:
            return -1
        local = row * self._columns + column
        return section.start + local if local < section.count else -1

    def mark_rect(self, index: int) -> QRectF:
        rect = self.item_rect(index)
        return QRectF(rect.right() - self.MARK - 8, rect.top() + 8, self.MARK, self.MARK)

    def visible_indexes(self, margin: float = 0.0) -> range:
        """The run of items that lie (partly) between the top and the bottom of the widget."""
        if not self._items:
            return range(0)
        top = self.scrollbar.value() - margin
        bottom = self.scrollbar.value() + self.height() + margin
        pitch = self._cell + self.GAP
        first_section = max(0, bisect.bisect_right(self._tops, top) - 1)
        first = last = None
        for number in range(first_section, len(self._sections)):
            section_top = self._tops[number]
            if section_top > bottom:
                break
            section = self._sections[number]
            body = section_top + (self.HEADER if section.title else 0)
            first_row = max(0, int((top - body) // pitch))
            last_row = int((bottom - body) // pitch)
            if last_row < 0:
                continue
            start = section.start + first_row * self._columns
            stop = min(section.start + section.count, section.start + (last_row + 1) * self._columns)
            if start >= stop:
                continue
            first = start if first is None else first
            last = stop
        if first is None:
            return range(0)
        return range(first, last)

    def ensure_visible(self, index: int) -> None:
        if not 0 <= index < len(self._items):
            return
        rect = self.item_rect(index)
        value = self.scrollbar.value()
        if rect.top() < 0:
            number = self._section_of(index)
            first_row = index - self._sections[number].start < self._columns
            head = self.HEADER if first_row and self._sections[number].title else 4
            self.scrollbar.setValue(round(value + rect.top() - head))
        elif rect.bottom() > self.height():
            self.scrollbar.setValue(round(value + rect.bottom() - self.height() + 4))

    def fluent_focus_rect(self) -> tuple[QWidget, QRect]:
        if 0 <= self._cursor < len(self._items):
            return self, self.item_rect(self._cursor).toRect()
        return self, QRect()

    # --- thumbnails ---
    def _scrolled(self, _value: int) -> None:
        self._want_timer.start()
        self.update()

    def _want(self) -> None:
        shown = self.visible_indexes()
        if not shown or not self.isVisible():
            self._visible = {}
            self._provider.forget(self)
            return
        ahead = self.visible_indexes(self.height() * 0.75)
        order = list(shown) + [index for index in ahead if index not in shown]
        self._visible = {self._items[index].path: index for index in order}
        self._provider.want(self, [(self._items[i].path, self._items[i].mtime, self._items[i].size) for i in order])

    def _thumbnail_changed(self, path: str) -> None:
        index = self._visible.get(path)
        if index is not None and index < len(self._items):
            self.update(self.item_rect(index).toAlignedRect())

    # --- events ---
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        anchor = self.visible_indexes()
        first = anchor.start if anchor else -1
        self._layout()
        if first >= 0 and self.scrollbar.value() > 0:
            self.scrollbar.setValue(round(self.item_rect(first).top() + self.scrollbar.value() - 4))
        self._want_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._layout()
        self._want_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._provider.forget(self)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        pixels = event.pixelDelta().y()
        if pixels:
            self.scrollbar.setValue(self.scrollbar.value() - pixels)
        elif delta:
            self.scrollbar.setValue(self.scrollbar.value() - round(delta / 120 * (self._cell + self.GAP) * 0.75))
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        hover = self.index_at(event.position().toPoint())
        if hover != self._hover:
            old = self._hover
            self._hover = hover
            for index in (old, hover):
                if 0 <= index < len(self._items):
                    self.update(self.item_rect(index).toAlignedRect())

    def leaveEvent(self, event) -> None:
        if self._hover >= 0:
            self._hover = -1
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position().toPoint()
        index = self.index_at(point)
        if index < 0:
            if not event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
                self.clear_selection()
            return
        modifiers = event.modifiers()
        self._cursor = index
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            start = self._anchor if self._anchor >= 0 else index
            self.set_selected(self._selected | set(range(min(start, index), max(start, index) + 1)))
        elif (modifiers & Qt.KeyboardModifier.ControlModifier or self._selected
              or self.mark_rect(index).adjusted(-6, -6, 6, 6).contains(event.position())):
            self.toggle(index)
        else:
            self._anchor = index
            self.activated.emit(index)
        self.update()
        event.accept()

    def contextMenuEvent(self, event) -> None:
        index = self.index_at(event.pos())
        if index >= 0 and index not in self._selected:
            self._cursor = self._anchor = index
            self.set_selected((index,))
        self.context_requested.emit(event.globalPos(), index)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()
        if not self._items:
            super().keyPressEvent(event)
            return
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        cursor = max(0, self._cursor)
        per_page = max(1, int(self.height() // (self._cell + self.GAP))) * self._columns
        moves = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -self._columns,
                 Qt.Key.Key_Down: self._columns, Qt.Key.Key_PageUp: -per_page, Qt.Key.Key_PageDown: per_page}
        if key in moves:
            self.set_cursor_index(cursor + moves[key], extend=shift)
        elif key == Qt.Key.Key_Home:
            self.set_cursor_index(0, extend=shift)
        elif key == Qt.Key.Key_End:
            self.set_cursor_index(len(self._items) - 1, extend=shift)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.activated.emit(cursor)
        elif key == Qt.Key.Key_Space:
            self.toggle(cursor)
        elif key == Qt.Key.Key_A and modifiers & Qt.KeyboardModifier.ControlModifier:
            self.select_all()
        elif key == Qt.Key.Key_Escape and self._selected:
            self.clear_selection()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            index = self.index_at(event.pos())
            if index >= 0:
                QToolTip.showText(event.globalPos(), self._items[index].name, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        if not self._items:
            return
        theme = fluent.theme()
        radius = theme.radius("control")
        clip = QRectF(event.rect())
        title_font = fluent.font("body-strong")
        count_font = fluent.font("caption")
        value = self.scrollbar.value()
        # headings of the sections in view
        first_section = max(0, bisect.bisect_right(self._tops, value) - 1)
        for number in range(first_section, len(self._sections)):
            if self._tops[number] - value > self.height():
                break
            section = self._sections[number]
            if not section.title:
                continue
            box = self.header_rect(number)
            if not box.intersects(clip):
                continue
            text_width = QFontMetrics(title_font).horizontalAdvance(section.title)
            paint.elided(painter, box.adjusted(0, 14, 0, -6), section.title, title_font, "text")
            count = f"{section.count} picture" + ("" if section.count == 1 else "s")
            paint.elided(painter, box.adjusted(text_width + 12, 16, 0, -6), count, count_font, "text-secondary")
        for index in self.visible_indexes():
            rect = self.item_rect(index)
            if not rect.intersects(clip):
                continue
            item = self._items[index]
            pixmap = self._provider.pixmap(item.path, item.mtime)
            if pixmap is not None:
                draw_cover(painter, rect, pixmap, radius)
            else:
                draw_placeholder(painter, rect, radius, self._provider.has_failed(item.path, item.mtime))
            chosen = index in self._selected
            hovered = index == self._hover
            if chosen:
                paint.rounded_rect(painter, rect, radius, "accent-subtle", "accent", 2)
            elif hovered:
                paint.rounded_rect(painter, rect, radius, "fill-subtle-hover", "stroke-strong")
            if chosen or hovered or self._selected:
                mark = self.mark_rect(index)
                if chosen:
                    paint.rounded_rect(painter, mark, self.MARK / 2, "accent")
                    paint.glyph(painter, mark, "check", "on-accent", 12)
                else:
                    paint.rounded_rect(painter, mark, self.MARK / 2, "flyout", "stroke-strong")
