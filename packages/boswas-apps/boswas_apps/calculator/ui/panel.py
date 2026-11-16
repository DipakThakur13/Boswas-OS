"""The History and Memory lists: beside the keypad in a wide window, over it in a narrow one."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent

from ..model import Calculator
from .widgets import SUBTLE, KeyButton, RecordRow

HISTORY, MEMORY = "history", "memory"


class _List(QScrollArea):
    """Rows under each other, or one sentence when there are none."""

    def __init__(self, empty_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(2)
        self.empty_label = fluent.Label(empty_text, "body", self._content, wrap=True)
        self.empty_label.setContentsMargins(8, 8, 8, 8)
        self._layout.addWidget(self.empty_label)
        self._layout.addStretch(1)
        self.setWidget(self._content)
        self.rows: list[QWidget] = []

    def set_rows(self, rows: list[QWidget]) -> None:
        for row in self.rows:
            self._layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self.rows = rows
        for index, row in enumerate(rows):
            self._layout.insertWidget(index + 1, row)
        self.empty_label.setVisible(not rows)


class MemoryRow(QWidget):
    """A memory entry: the value (click to recall) and its own MC, M+ and M− keys."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.value_row = RecordRow("", text, self)
        self.value_row.setAccessibleName(f"Memory {text}")
        layout.addWidget(self.value_row)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 8, 4)
        buttons.setSpacing(4)
        buttons.addStretch(1)
        self.clear_button = KeyButton("MC", None, SUBTLE, "Clear memory item", self, style="caption")
        self.add_button = KeyButton("M+", None, SUBTLE, "Add to memory item", self, style="caption")
        self.subtract_button = KeyButton("M−", None, SUBTLE, "Subtract from memory item", self, style="caption")
        for button in (self.clear_button, self.add_button, self.subtract_button):
            button.setFixedSize(40, 28)
            buttons.addWidget(button)
        layout.addLayout(buttons)


class RecordPanel(QWidget):
    """History and Memory of the current mode.

    ``changed`` is emitted after the panel changed the calculator (a recall,
    a memory key, a clear), ``recalled`` after an entry was put on the
    display (the overlay closes on it).
    """

    changed = Signal()
    recalled = Signal()

    def __init__(self, calculator: Calculator, parent: QWidget | None = None, *, surface: bool = False) -> None:
        super().__init__(parent)
        self.calculator = calculator
        self._surface = surface
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(4)
        self.pivot = fluent.Pivot(self)
        self.pivot.add_item(HISTORY, "History")
        self.pivot.add_item(MEMORY, "Memory")
        self.pivot.current_changed.connect(self._tab_changed)
        layout.addWidget(self.pivot)
        self.stack = QStackedWidget(self)
        self.history_list = _List("There’s no history yet.", self.stack)
        self.memory_list = _List("There’s nothing saved in memory.", self.stack)
        self.stack.addWidget(self.history_list)
        self.stack.addWidget(self.memory_list)
        layout.addWidget(self.stack, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.clear_button = fluent.IconButton("edit-delete-symbolic", "Clear all history", self)
        self.clear_button.clicked.connect(self.clear_current)
        footer.addWidget(self.clear_button)
        layout.addLayout(footer)
        self.refresh()

    # --- state ---
    def current_tab(self) -> str:
        return self.pivot.current() or HISTORY

    def show_tab(self, tab: str) -> None:
        if tab == HISTORY and not self.calculator.has_history():
            tab = MEMORY
        self.pivot.set_current(tab)
        self._tab_changed(tab)

    def _tab_changed(self, tab: str) -> None:
        self.stack.setCurrentWidget(self.history_list if tab == HISTORY else self.memory_list)
        self._sync_clear()

    def _sync_clear(self) -> None:
        history = self.current_tab() == HISTORY
        self.clear_button.setToolTip("Clear all history" if history else "Clear all memory")
        self.clear_button.setAccessibleName(self.clear_button.toolTip())
        self.clear_button.setVisible(bool(self.calculator.history if history else self.calculator.memory))

    def refresh(self) -> None:
        """Rebuild both lists from the calculator."""
        calculator = self.calculator
        has_history = calculator.has_history()
        self.pivot.item(HISTORY).setVisible(has_history)
        if not has_history and self.current_tab() == HISTORY:
            self.pivot.set_current(MEMORY)
        rows: list[QWidget] = []
        for index, item in enumerate(calculator.history):
            row = RecordRow(item.expression, item.result)
            row.clicked.connect(lambda _checked=False, i=index: self._recall_history(i))
            rows.append(row)
        self.history_list.set_rows(rows)
        rows = []
        for index in range(len(calculator.memory)):
            row = MemoryRow(calculator.memory_text(index))
            row.value_row.clicked.connect(lambda _checked=False, i=index: self._memory(calculator.memory_recall, i,
                                                                                      recall=True))
            row.clear_button.clicked.connect(lambda _checked=False, i=index: self._memory(calculator.memory_clear, i))
            row.add_button.clicked.connect(lambda _checked=False, i=index: self._memory(calculator.memory_add, i))
            row.subtract_button.clicked.connect(
                lambda _checked=False, i=index: self._memory(calculator.memory_subtract, i))
            rows.append(row)
        self.memory_list.set_rows(rows)
        self._tab_changed(self.current_tab())

    # --- actions ---
    def _recall_history(self, index: int) -> None:
        if self.calculator.recall_history(index):
            self.changed.emit()
            self.recalled.emit()

    def _memory(self, action, index: int, *, recall: bool = False) -> None:
        action(index)
        self.changed.emit()
        if recall:
            self.recalled.emit()

    def clear_current(self) -> None:
        if self.current_tab() == HISTORY:
            self.calculator.clear_history()
        else:
            self.calculator.memory_clear()
        self.changed.emit()

    def paintEvent(self, _event) -> None:
        if self._surface:
            painter = QPainter(self)
            fluent.paint.rounded_rect(painter, self.rect(), fluent.theme().radius("flyout"), "flyout",
                                      "stroke-surface")
