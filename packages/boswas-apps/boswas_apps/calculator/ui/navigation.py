"""The navigation of the calculator: a list of modes that slides in over the window (the hamburger menu)."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

import boswas_fluent as fluent

KEY_ROLE = Qt.ItemDataRole.UserRole
PANE_WIDTH = 256
ROW_HEIGHT = 40


class _Delegate(QStyledItemDelegate):
    """Paints the entries: a section title, or a mode with the hover layer and the selection pill."""

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        rect = QRectF(option.rect)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        painter.save()
        if index.data(KEY_ROLE) is None:                       # a section title
            fluent.paint.elided(painter, rect.adjusted(16, 4, -8, 0), text, fluent.font("body-strong"), "text")
            painter.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        box = rect.adjusted(4, 2, -4, -2)
        if selected or hovered:
            fluent.paint.rounded_rect(painter, box, fluent.theme().radius("control"),
                                      "fill-subtle-pressed" if selected and hovered else "fill-subtle-hover")
        if selected:
            fluent.paint.rounded_rect(painter, QRectF(box.left(), rect.center().y() - 7.5, 3, 16), 1.5, "accent")
        fluent.paint.elided(painter, rect.adjusted(20, 0, -12, 0), text, fluent.font("body"), "text")
        painter.restore()


class _ModeList(QListWidget):
    """The list of the pane; the arrow keys skip the section titles."""

    def moveCursor(self, action, modifiers):
        index = super().moveCursor(action, modifiers)
        actions = QAbstractItemView.CursorAction
        step = -1 if action in (actions.MoveUp, actions.MovePageUp, actions.MovePrevious, actions.MoveLeft,
                                actions.MoveEnd) else 1
        row = index.row()
        while 0 <= row < self.count() and self.item(row).data(KEY_ROLE) is None:
            row += step
        if not 0 <= row < self.count():
            return self.currentIndex()
        return self.model().index(row, 0)


class NavigationOverlay(QWidget):
    """Covers the window while the list of modes is open; a click beside the list, or Escape, closes it.

    ``chosen(key)`` when a mode is picked. Keyboard: Up and Down, Enter to
    choose, Escape to close.
    """

    chosen = Signal(str)
    closed = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._animation: QVariantAnimation | None = None
        self.hide()
        self.pane = QFrame(self)
        self.pane.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self.pane)
        layout.setContentsMargins(0, 4, 0, 8)
        layout.setSpacing(4)
        top = QHBoxLayout()
        top.setContentsMargins(4, 0, 4, 0)
        self.close_button = fluent.IconButton("open-menu-symbolic", "Close navigation", self.pane, size=40)
        self.close_button.clicked.connect(self.close_pane)
        top.addWidget(self.close_button)
        top.addStretch(1)
        layout.addLayout(top)
        self.list = _ModeList(self.pane)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setItemDelegate(_Delegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setAccessibleName("Calculator modes")
        self.list.itemClicked.connect(self._activated)
        self.list.itemActivated.connect(self._activated)
        layout.addWidget(self.list, 1)

    def add_section(self, title: str) -> None:
        item = QListWidgetItem(title)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setData(KEY_ROLE, None)
        self.list.addItem(item)

    def add_item(self, key: str, title: str) -> None:
        item = QListWidgetItem(title)
        item.setData(KEY_ROLE, key)
        self.list.addItem(item)

    def keys(self) -> list[str]:
        return [self.list.item(row).data(KEY_ROLE) for row in range(self.list.count())
                if self.list.item(row).data(KEY_ROLE) is not None]

    def title(self, key: str) -> str:
        return self._item(key).text()

    def _item(self, key: str) -> QListWidgetItem:
        for row in range(self.list.count()):
            if self.list.item(row).data(KEY_ROLE) == key:
                return self.list.item(row)
        raise KeyError(key)

    def set_current(self, key: str) -> None:
        self.list.setCurrentItem(self._item(key))

    def click_item(self, key: str) -> None:
        """Choose a mode as a click on it does."""
        item = self._item(key)
        self.list.setCurrentItem(item)
        self._activated(item)

    def _activated(self, item: QListWidgetItem) -> None:
        key = item.data(KEY_ROLE)
        if key is not None:
            self.close_pane()
            self.chosen.emit(key)

    # --- opening and closing ---
    def is_open(self) -> bool:
        return self.isVisible()

    def open_pane(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self._slide(-PANE_WIDTH, 0)
        self.list.setFocus(Qt.FocusReason.PopupFocusReason)
        if self.list.currentItem() is not None:
            self.list.scrollToItem(self.list.currentItem())

    def close_pane(self) -> None:
        if not self.isVisible():
            return
        if self._animation is not None:
            self._animation.stop()
            self._animation = None
        self.hide()
        self.closed.emit()

    def _slide(self, start: int, end: int) -> None:
        if self._animation is not None:
            self._animation.stop()
            self._animation = None
        time = fluent.duration("fast")
        if time <= 0:
            self._place(end)
            return
        self._place(start)
        animation = QVariantAnimation(self)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setDuration(time)
        animation.setEasingCurve(fluent.easing("decelerate"))
        animation.valueChanged.connect(lambda value: self._place(int(value)))
        animation.start()
        self._animation = animation

    def _place(self, x: int) -> None:
        self.pane.setGeometry(x, 0, min(PANE_WIDTH, self.width()), self.height())
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place(self.pane.x() if self._animation is not None else 0)

    def mousePressEvent(self, event) -> None:
        if not self.pane.geometry().contains(event.position().toPoint()):
            self.close_pane()
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_pane()
            return
        event.accept()                       # nothing reaches the keypad underneath

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        box = QRectF(self.pane.geometry())
        radius = fluent.theme().radius("flyout")
        fluent.paint.shadow(painter, box, radius, "flyout")
        painter.setClipRect(self.rect())
        # Only the corners at the right are round: the pane starts beyond the left edge.
        fluent.paint.rounded_rect(painter, box.adjusted(-radius, 0, 0, 0), radius, "flyout", "stroke-surface")
