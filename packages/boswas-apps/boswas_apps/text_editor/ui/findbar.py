"""The find bar: find and replace over the current tab, floating at the top of the text."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

import boswas_fluent as fluent

from ..search import Options

OPTION_LABELS = (("match_case", "Match case"), ("whole_word", "Whole word"), ("wrap_around", "Wrap around"),
                 ("regex", "Regular expressions"))


class _Field(QLineEdit):
    """A line of the find bar: Enter and Shift+Enter, Escape."""

    submitted = Signal(bool)                     # True: backwards
    escaped = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.submitted.emit(bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            return
        if event.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        super().keyPressEvent(event)


class FindBar(QWidget):
    """Find (and, opened further, replace). The window does the searching; the bar asks for it."""

    find_requested = Signal(bool)                # True: backwards
    query_changed = Signal()
    replace_requested = Signal()
    replace_all_requested = Signal()
    closed = Signal()

    def __init__(self, settings: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.expand_button = fluent.IconButton(fluent.glyph_icon("chevron-down"), "Show replace", self, icon_size=12,
                                               checkable=True)
        self.expand_button.clicked.connect(lambda checked: self.set_replace_visible(checked))
        self.find_edit = _Field(self)
        self.find_edit.setPlaceholderText("Find")
        self.find_edit.setAccessibleName("Find")
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.setMinimumWidth(180)
        self.count_label = fluent.Label("", "caption", self, tone="secondary")
        self.count_label.setMinimumWidth(64)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_button = fluent.IconButton("go-down-symbolic", "Find next (F3)", self)
        self.previous_button = fluent.IconButton("go-up-symbolic", "Find previous (Shift+F3)", self)
        self.options_button = fluent.IconButton("view-more-horizontal-symbolic", "Search options", self)
        self.close_button = fluent.IconButton(fluent.glyph_icon("close"), "Close (Esc)", self, icon_size=12)
        for widget in (self.expand_button, self.find_edit, self.count_label, self.next_button, self.previous_button,
                       self.options_button, self.close_button):
            row.addWidget(widget, 1 if widget is self.find_edit else 0)
        outer.addLayout(row)
        self.replace_row = QWidget(self)
        replace = QHBoxLayout(self.replace_row)
        replace.setContentsMargins(36, 0, 0, 0)
        replace.setSpacing(4)
        self.replace_edit = _Field(self.replace_row)
        self.replace_edit.setPlaceholderText("Replace")
        self.replace_edit.setAccessibleName("Replace with")
        self.replace_button = fluent.Button("Replace", self.replace_row)
        self.replace_all_button = fluent.Button("Replace all", self.replace_row)
        replace.addWidget(self.replace_edit, 1)
        replace.addWidget(self.replace_button)
        replace.addWidget(self.replace_all_button)
        outer.addWidget(self.replace_row)
        self.replace_row.hide()

        self.options_menu = fluent.MenuFlyout(self)
        self.option_actions = {}
        for key, label in OPTION_LABELS:
            action = self.options_menu.add_item(label, checkable=True, checked=bool(settings[key]))
            action.toggled.connect(lambda checked, k=key: self._option_toggled(k, checked))
            self.option_actions[key] = action
        self.options_button.clicked.connect(lambda: self.options_menu.exec_at(self.options_button))

        self.find_edit.textChanged.connect(lambda _text: self.query_changed.emit())
        self.find_edit.submitted.connect(self.find_requested)
        self.find_edit.escaped.connect(self.close_bar)
        self.replace_edit.submitted.connect(lambda _backwards: self.replace_requested.emit())
        self.replace_edit.escaped.connect(self.close_bar)
        self.next_button.clicked.connect(lambda: self.find_requested.emit(False))
        self.previous_button.clicked.connect(lambda: self.find_requested.emit(True))
        self.replace_button.clicked.connect(self.replace_requested)
        self.replace_all_button.clicked.connect(self.replace_all_requested)
        self.close_button.clicked.connect(self.close_bar)
        self.setTabOrder(self.find_edit, self.replace_edit)
        self.hide()

    def _option_toggled(self, key: str, checked: bool) -> None:
        self.settings[key] = checked
        self.query_changed.emit()

    def options(self) -> Options:
        return Options(match_case=bool(self.settings["match_case"]), whole_word=bool(self.settings["whole_word"]),
                       regex=bool(self.settings["regex"]), wrap_around=bool(self.settings["wrap_around"]))

    def query(self) -> str:
        return self.find_edit.text()

    def replacement(self) -> str:
        return self.replace_edit.text()

    def set_replace_visible(self, visible: bool) -> None:
        self.replace_row.setVisible(visible)
        self.expand_button.setChecked(visible)
        self.expand_button.setIcon(fluent.glyph_icon("chevron-up" if visible else "chevron-down"))
        self.expand_button.setToolTip("Hide replace" if visible else "Show replace")
        self.adjustSize()

    def replace_visible(self) -> bool:
        return not self.replace_row.isHidden()

    def open_bar(self, text: str | None = None, *, replace: bool = False) -> None:
        if replace:
            self.set_replace_visible(True)
        if text:
            self.find_edit.setText(text)
        self.adjustSize()
        self.show()
        self.raise_()
        self.find_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.find_edit.selectAll()

    def close_bar(self) -> None:
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def set_count(self, text: str, *, problem: bool = False) -> None:
        self.count_label.setText(text)
        self.count_label.set_tone("danger" if problem else "secondary")
        self.count_label.setToolTip(text if problem else "")

    def set_read_only(self, read_only: bool) -> None:
        for widget in (self.replace_button, self.replace_all_button):
            widget.setEnabled(not read_only)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        fluent.paint.rounded_rect(painter, self.rect(), fluent.theme().radius("flyout"), "flyout", "stroke-surface")
