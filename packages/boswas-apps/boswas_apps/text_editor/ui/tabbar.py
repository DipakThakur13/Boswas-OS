"""The row of tabs: drag to reorder, a dot on a tab with unsaved changes, a close button on each."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QTabBar, QWidget

import boswas_fluent as fluent

MIN_TAB, MAX_TAB = 96, 220


class _Dot(QWidget):
    """The mark of unsaved changes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(10, 16)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAccessibleName("Unsaved changes")

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        fluent.paint.rounded_rect(painter, self.rect().adjusted(1, 4, -1, -4), 4, "text")


class TabBar(QTabBar):
    """The tabs of a window. ``close_requested(index)``, ``new_requested`` (a double click on the empty part),
    ``menu_requested(index, global position)``; ``currentChanged`` and ``tabMoved`` are QTabBar's."""

    close_requested = Signal(int)
    new_requested = Signal()
    menu_requested = Signal(int, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setExpanding(False)
        self.setDocumentMode(True)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setSelectionBehaviorOnRemove(QTabBar.SelectionBehavior.SelectPreviousTab)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Tabs")
        self.tabCloseRequested.connect(self.close_requested)

    def tabSizeHint(self, index: int) -> QSize:
        hint = super().tabSizeHint(index)
        return QSize(max(MIN_TAB, min(MAX_TAB, hint.width())), hint.height())

    def minimumTabSizeHint(self, index: int) -> QSize:
        return QSize(MIN_TAB, super().minimumTabSizeHint(index).height())

    def set_modified(self, index: int, modified: bool) -> None:
        """Show or remove the dot of unsaved changes on a tab."""
        has_dot = self.tabButton(index, QTabBar.ButtonPosition.LeftSide) is not None
        if modified and not has_dot:
            self.setTabButton(index, QTabBar.ButtonPosition.LeftSide, _Dot(self))
        elif not modified and has_dot:
            self.setTabButton(index, QTabBar.ButtonPosition.LeftSide, None)

    def is_marked_modified(self, index: int) -> bool:
        return self.tabButton(index, QTabBar.ButtonPosition.LeftSide) is not None

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0:
                self.close_requested.emit(index)
                return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self.tabAt(event.position().toPoint()) < 0:
            self.new_requested.emit()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        index = self.tabAt(event.pos())
        if index >= 0:
            self.menu_requested.emit(index, event.globalPos())
