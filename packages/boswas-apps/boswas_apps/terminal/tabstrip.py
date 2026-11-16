"""TabStrip: the row of tabs above the terminals.

Each tab shows its title, a mark when something happened in it while it was
in the background (a dot for output, a bell for a bell) and a close button.
The tab of the terminal on screen is filled with that terminal's background
colour, so tab and terminal read as one surface. Tabs are reordered by
dragging, renamed in place (double-click or F2), closed with the middle
button, and reached from the keyboard with Left and Right while the strip
has the focus.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QLineEdit, QSizePolicy, QToolTip, QWidget

import boswas_fluent as fluent

TAB_HEIGHT = 32
STRIP_HEIGHT = 40
MAX_WIDTH = 220
MIN_WIDTH = 76
CLOSE_SIZE = 20
MAX_TITLE = 256


@dataclass(eq=False)
class _Tab:
    title: str
    indicator: str = ""              # "", "activity" or "bell"
    background: QColor | None = None
    foreground: QColor | None = None
    icon: str = ""


class TabStrip(QWidget):
    current_changed = Signal(int)
    close_requested = Signal(int)
    tab_moved = Signal(int, int)             # from, to
    renamed = Signal(int, str)               # index, the new title ('' gives the tab its own title back)
    menu_requested = Signal(int, object)     # index, global position
    new_tab_requested = Signal()             # double-click on the empty part of the strip

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tabs: list[_Tab] = []
        self._current = -1
        self._hover = -1
        self._hover_close = False
        self._pressed = -1
        self._pressed_close = False
        self._press_x = 0
        self._dragging = False
        self._scroll = 0
        self._editor: QLineEdit | None = None
        self._editing = -1
        self.setFixedHeight(STRIP_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Tabs")

    # --- tabs ---
    def count(self) -> int:
        return len(self._tabs)

    def current(self) -> int:
        return self._current

    def title(self, index: int) -> str:
        return self._tabs[index].title

    def indicator(self, index: int) -> str:
        return self._tabs[index].indicator

    def add_tab(self, title: str, index: int | None = None, icon: str = "") -> int:
        index = len(self._tabs) if index is None else max(0, min(index, len(self._tabs)))
        self._tabs.insert(index, _Tab(title[:MAX_TITLE], icon=icon))
        if self._current >= index:
            self._current += 1
        self._changed()
        return index

    def remove_tab(self, index: int) -> None:
        if not 0 <= index < len(self._tabs):
            return
        self.cancel_rename()
        del self._tabs[index]
        if self._current > index or self._current >= len(self._tabs):
            self._current -= 1
        if self._current < 0 and self._tabs:
            self._current = 0
        self._hover = -1
        self._changed()

    def set_current(self, index: int) -> None:
        if 0 <= index < len(self._tabs) and index != self._current:
            self._current = index
            self._ensure_visible(index)
            self.update()
            self.current_changed.emit(index)
        elif 0 <= index < len(self._tabs):
            self._ensure_visible(index)

    def set_title(self, index: int, title: str) -> None:
        if 0 <= index < len(self._tabs) and self._tabs[index].title != title[:MAX_TITLE]:
            self._tabs[index].title = title[:MAX_TITLE]
            self.update()

    def set_indicator(self, index: int, indicator: str) -> None:
        if 0 <= index < len(self._tabs) and self._tabs[index].indicator != indicator:
            self._tabs[index].indicator = indicator
            self.update()

    def set_colours(self, index: int, background: QColor | None, foreground: QColor | None) -> None:
        """The fill and text colour of the tab while it is the current one (None: the page colours)."""
        if 0 <= index < len(self._tabs):
            self._tabs[index].background = background
            self._tabs[index].foreground = foreground
            self.update()

    def move_tab(self, source: int, target: int) -> None:
        if source == target or not (0 <= source < len(self._tabs) and 0 <= target < len(self._tabs)):
            return
        current = self._tabs[self._current] if 0 <= self._current < len(self._tabs) else None
        self._tabs.insert(target, self._tabs.pop(source))
        if current is not None:
            self._current = self._tabs.index(current)
        self.update()
        self.tab_moved.emit(source, target)

    def _changed(self) -> None:
        self.updateGeometry()
        self._clamp_scroll()
        self.update()

    # --- geometry ---
    def sizeHint(self) -> QSize:
        return QSize(max(1, len(self._tabs)) * MAX_WIDTH, STRIP_HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(MIN_WIDTH, STRIP_HEIGHT)

    def _tab_width(self) -> int:
        count = len(self._tabs)
        if not count:
            return MAX_WIDTH
        return max(MIN_WIDTH, min(MAX_WIDTH, self.width() // count))

    def tab_rect(self, index: int) -> QRect:
        width = self._tab_width()
        return QRect(index * width - self._scroll, STRIP_HEIGHT - TAB_HEIGHT, width, TAB_HEIGHT)

    def close_rect(self, index: int) -> QRect:
        rect = self.tab_rect(index)
        return QRect(rect.right() - 6 - CLOSE_SIZE, rect.top() + (TAB_HEIGHT - CLOSE_SIZE) // 2, CLOSE_SIZE,
                     CLOSE_SIZE)

    def tab_at(self, position: QPoint) -> int:
        if position.y() < STRIP_HEIGHT - TAB_HEIGHT - 4:
            return -1
        index = (position.x() + self._scroll) // self._tab_width()
        return index if 0 <= index < len(self._tabs) else -1

    def _clamp_scroll(self) -> None:
        limit = max(0, len(self._tabs) * self._tab_width() - self.width())
        self._scroll = max(0, min(self._scroll, limit))

    def _ensure_visible(self, index: int) -> None:
        width = self._tab_width()
        left = index * width
        if left < self._scroll:
            self._scroll = left
        elif left + width > self._scroll + self.width():
            self._scroll = left + width - self.width()
        self._clamp_scroll()
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._clamp_scroll()
        if self._current >= 0:
            self._ensure_visible(self._current)
        self._place_editor()

    # --- painting ---
    def _shows_close(self, index: int) -> bool:
        return index == self._current or index == self._hover

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = fluent.theme()
        radius = theme.radius("card")
        font = fluent.font("caption")
        painter.setFont(font)
        metrics = QFontMetrics(font)
        for index, tab in enumerate(self._tabs):
            rect = self.tab_rect(index)
            if rect.right() < 0 or rect.left() > self.width():
                continue
            current = index == self._current
            box = QRectF(rect)
            if current:
                fill = tab.background if tab.background is not None else fluent.qcolor("background")
                path = QPainterPath()
                path.addRoundedRect(box.adjusted(0, 0, 0, radius), radius, radius)
                painter.save()
                painter.setClipRect(box)
                painter.fillPath(path, fill)
                painter.restore()
                text_colour = tab.foreground if tab.foreground is not None else fluent.qcolor("text")
            else:
                if index == self._hover or index == self._pressed:
                    fluent.paint.rounded_rect(painter, box.adjusted(2, 2, -2, -4), theme.radius("control"),
                                              "fill-subtle-pressed" if index == self._pressed else "fill-subtle-hover")
                elif index + 1 != self._current and index + 1 < len(self._tabs) and index + 1 != self._hover:
                    painter.fillRect(QRectF(rect.right(), rect.top() + 9, 1, TAB_HEIGHT - 18),
                                     fluent.qcolor("stroke-divider"))
                text_colour = fluent.qcolor("text-secondary")
            left = rect.left() + 12
            if tab.indicator:
                mark = QRectF(left, rect.center().y() - 3, 8, 8)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fluent.qcolor("accent" if tab.indicator == "activity" else "warning"))
                if tab.indicator == "bell":
                    bell = QPainterPath()             # a small bell: a dome on a bar with a clapper
                    bell.moveTo(mark.left() + 1, mark.bottom() - 2)
                    bell.quadTo(mark.left() + 1.5, mark.top(), mark.center().x(), mark.top())
                    bell.quadTo(mark.right() - 1.5, mark.top(), mark.right() - 1, mark.bottom() - 2)
                    bell.closeSubpath()
                    painter.drawPath(bell)
                    painter.drawRect(QRectF(mark.left(), mark.bottom() - 2, mark.width(), 1.2))
                    painter.drawEllipse(QRectF(mark.center().x() - 1, mark.bottom() - 0.6, 2, 2))
                else:
                    painter.drawEllipse(mark.adjusted(1, 1, -1, -1))
                left += 14
            elif tab.icon:
                fluent.icon(tab.icon, color=None).paint(painter, QRect(left, rect.center().y() - 7, 16, 16))
                left += 22
            right = rect.right() - 10
            if self._shows_close(index):
                close = self.close_rect(index)
                if index == self._hover and self._hover_close:
                    fluent.paint.rounded_rect(painter, close, theme.radius("control"),
                                              "fill-subtle-pressed" if self._pressed_close else "fill-subtle-hover")
                colour = "text" if current and tab.foreground is None else "text-secondary"
                if current and tab.foreground is not None:
                    self._paint_cross(painter, close, tab.foreground)
                else:
                    fluent.paint.glyph(painter, close, "close", colour, 10)
                right = close.left() - 4
            if index != self._editing:
                painter.setPen(text_colour)
                text = metrics.elidedText(tab.title, Qt.TextElideMode.ElideRight, max(0, right - left))
                painter.drawText(QRect(left, rect.top(), max(0, right - left), TAB_HEIGHT),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)

    @staticmethod
    def _paint_cross(painter: QPainter, rect: QRect, colour: QColor) -> None:
        from PySide6.QtGui import QPen
        painter.save()
        painter.setPen(QPen(colour, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        centre = QRectF(rect).center()
        painter.drawLine(centre.x() - 4, centre.y() - 4, centre.x() + 4, centre.y() + 4)
        painter.drawLine(centre.x() + 4, centre.y() - 4, centre.x() - 4, centre.y() + 4)
        painter.restore()

    def fluent_focus_rect(self) -> tuple[QWidget, QRect]:
        index = self._current if self._current >= 0 else 0
        return self, self.tab_rect(index).adjusted(1, 1, -1, -1) if self._tabs else self.rect()

    # --- mouse ---
    def _set_hover(self, index: int, on_close: bool) -> None:
        if (index, on_close) != (self._hover, self._hover_close):
            self._hover, self._hover_close = index, on_close
            self.update()

    def mouseMoveEvent(self, event) -> None:
        position = event.position().toPoint()
        if self._pressed >= 0 and not self._pressed_close and event.buttons() & Qt.MouseButton.LeftButton:
            if not self._dragging and abs(position.x() - self._press_x) >= QApplication.startDragDistance():
                self._dragging = True
            if self._dragging:
                target = max(0, min((position.x() + self._scroll) // self._tab_width(), len(self._tabs) - 1))
                if target != self._pressed:
                    self.move_tab(self._pressed, target)
                    self._pressed = target
                return
        index = self.tab_at(position)
        self._set_hover(index, index >= 0 and self._shows_close(index) and self.close_rect(index).contains(position))

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._set_hover(-1, False)

    def mousePressEvent(self, event) -> None:
        position = event.position().toPoint()
        index = self.tab_at(position)
        self.commit_rename()
        if index < 0:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = index
            self._press_x = position.x()
            self._dragging = False
            self._pressed_close = self._shows_close(index) and self.close_rect(index).contains(position)
            if not self._pressed_close:
                self.set_current(index)
            self.update()
        elif event.button() == Qt.MouseButton.MiddleButton:
            self.close_requested.emit(index)

    def mouseReleaseEvent(self, event) -> None:
        position = event.position().toPoint()
        pressed, on_close = self._pressed, self._pressed_close
        self._pressed, self._pressed_close, self._dragging = -1, False, False
        self.update()
        if event.button() == Qt.MouseButton.LeftButton and pressed >= 0 and on_close \
                and pressed < len(self._tabs) and self.close_rect(pressed).contains(position):
            self.close_requested.emit(pressed)

    def mouseDoubleClickEvent(self, event) -> None:
        position = event.position().toPoint()
        index = self.tab_at(position)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if index < 0:
            self.new_tab_requested.emit()
        elif not (self._shows_close(index) and self.close_rect(index).contains(position)):
            self.start_rename(index)

    def contextMenuEvent(self, event) -> None:
        if event.reason() == event.Reason.Mouse:
            index = self.tab_at(event.pos())
            position = event.globalPos()
        else:
            index = self._current
            position = self.mapToGlobal(self.tab_rect(max(0, index)).bottomLeft())
        if index >= 0:
            self.menu_requested.emit(index, position)
        event.accept()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta and len(self._tabs) * self._tab_width() > self.width():
            self._scroll -= delta
            self._clamp_scroll()
            self._place_editor()
            self.update()
            event.accept()
        else:
            event.ignore()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            index = self.tab_at(event.pos())
            if index >= 0:
                on_close = self._shows_close(index) and self.close_rect(index).contains(event.pos())
                QToolTip.showText(event.globalPos(), "Close tab (Ctrl+Shift+W)" if on_close else self._tabs[index].title,
                                  self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    # --- keyboard ---
    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Left and self._current > 0:
            self.set_current(self._current - 1)
        elif key == Qt.Key.Key_Right and self._current < len(self._tabs) - 1:
            self.set_current(self._current + 1)
        elif key == Qt.Key.Key_Home and self._tabs:
            self.set_current(0)
        elif key == Qt.Key.Key_End and self._tabs:
            self.set_current(len(self._tabs) - 1)
        elif key == Qt.Key.Key_F2 and self._current >= 0:
            self.start_rename(self._current)
        elif key == Qt.Key.Key_Delete and self._current >= 0:
            self.close_requested.emit(self._current)
        else:
            super().keyPressEvent(event)

    # --- renaming ---
    def start_rename(self, index: int) -> None:
        if not 0 <= index < len(self._tabs):
            return
        self.cancel_rename()
        self._ensure_visible(index)
        editor = QLineEdit(self._tabs[index].title, self)
        editor.setMaxLength(MAX_TITLE)
        editor.setAccessibleName("Tab title")
        editor.setFont(fluent.font("caption"))
        editor.installEventFilter(self)
        editor.returnPressed.connect(self.commit_rename)
        self._editor, self._editing = editor, index
        self._place_editor()
        editor.show()
        editor.selectAll()
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self.update()

    def _place_editor(self) -> None:
        if self._editor is not None and 0 <= self._editing < len(self._tabs):
            rect = self.tab_rect(self._editing)
            self._editor.setGeometry(rect.left() + 6, rect.top() + 2, max(40, rect.width() - 12), TAB_HEIGHT - 4)

    def is_renaming(self) -> bool:
        return self._editor is not None

    def rename_editor(self) -> QLineEdit | None:
        return self._editor

    def commit_rename(self) -> None:
        editor, index = self._editor, self._editing
        if editor is None:
            return
        text = editor.text().strip()
        self._close_editor()
        if 0 <= index < len(self._tabs):
            self.renamed.emit(index, text)

    def cancel_rename(self) -> None:
        if self._editor is not None:
            self._close_editor()

    def _close_editor(self) -> None:
        editor = self._editor
        self._editor, self._editing = None, -1
        if editor is not None:
            editor.removeEventFilter(self)
            editor.hide()
            editor.deleteLater()
        self.update()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._editor:
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.cancel_rename()
                return True
            if event.type() == QEvent.Type.FocusOut:
                self.commit_rename()
        return super().eventFilter(watched, event)
