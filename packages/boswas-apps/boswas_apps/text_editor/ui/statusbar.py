"""The status bar: position, size, zoom, line ending and encoding of the current tab."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

import boswas_fluent as fluent

from .. import codec


class _Choice(QToolButton):
    """A cell of the status bar that opens a menu."""

    def __init__(self, tooltip: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setAutoRaise(True)
        self.setFont(fluent.font("caption"))
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(24)


class StatusBar(QWidget):
    """``Ln 3, Col 7 | 120 characters``, and at the right the zoom, the line ending and the encoding.

    The three at the right are buttons: ``zoom_chosen(percent or step)``,
    ``line_ending_chosen(kind)``, ``encoding_chosen(key)``, ``reopen_chosen(key)``.
    """

    zoom_step = Signal(int)                 # +1, -1 or 0 (back to 100 %)
    line_ending_chosen = Signal(str)
    encoding_chosen = Signal(str)
    reopen_chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(32)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(16)
        self.position_label = fluent.Label("Ln 1, Col 1", "caption", self, tone="secondary")
        self.count_label = fluent.Label("0 characters", "caption", self, tone="secondary")
        self.zoom_button = _Choice("Zoom", self)
        self.line_ending_button = _Choice("Line ending", self)
        self.encoding_button = _Choice("Encoding", self)
        layout.addWidget(self.position_label)
        layout.addWidget(self.count_label)
        layout.addStretch(1)
        for button in (self.zoom_button, self.line_ending_button, self.encoding_button):
            layout.addWidget(button)
        self.zoom_button.clicked.connect(lambda: self.zoom_menu().exec_at(self.zoom_button, "top"))
        self.line_ending_button.clicked.connect(
            lambda: self.line_ending_menu().exec_at(self.line_ending_button, "top"))
        self.encoding_button.clicked.connect(lambda: self.encoding_menu().exec_at(self.encoding_button, "top"))
        self._line_ending = codec.LF
        self._encoding = codec.UTF8
        self._can_reopen = False
        self.setAccessibleName("Status")

    def set_position(self, line: int, column: int) -> None:
        self.position_label.setText(f"Ln {line}, Col {column}")

    def set_counts(self, characters: int, selected: int) -> None:
        if selected:
            self.count_label.setText(f"{selected:,} of {characters:,} characters")
        else:
            self.count_label.setText("1 character" if characters == 1 else f"{characters:,} characters")

    def set_zoom(self, percent: int) -> None:
        self.zoom_button.setText(f"{percent}%")

    def set_file_state(self, line_ending: str, encoding: codec.Encoding, *, can_reopen: bool,
                       enabled: bool = True) -> None:
        self._line_ending, self._encoding, self._can_reopen = line_ending, encoding, can_reopen
        self.line_ending_button.setText(codec.LINE_ENDING_LABELS[line_ending])
        self.encoding_button.setText(encoding.label)
        for button in (self.line_ending_button, self.encoding_button):
            button.setEnabled(enabled)

    # --- menus (built when opened; tests read them) ---
    def zoom_menu(self) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        menu.add_item("Zoom in", lambda: self.zoom_step.emit(1), shortcut="Ctrl++")
        menu.add_item("Zoom out", lambda: self.zoom_step.emit(-1), shortcut="Ctrl+-")
        menu.add_item("Restore default zoom", lambda: self.zoom_step.emit(0), shortcut="Ctrl+0")
        return menu

    def line_ending_menu(self) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        for kind, label in codec.LINE_ENDING_LABELS.items():
            menu.add_item(label, lambda k=kind: self.line_ending_chosen.emit(k), checkable=True,
                          checked=kind == self._line_ending)
        return menu

    def encoding_menu(self) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        choices = list(codec.CHOICES)
        if self._encoding not in choices:
            choices.append(self._encoding)
        save = menu.add_submenu("Save with encoding")
        for encoding in choices:
            save.add_item(encoding.label, lambda e=encoding: self.encoding_chosen.emit(e.key), checkable=True,
                          checked=encoding == self._encoding)
        reopen = menu.add_submenu("Reopen with encoding")
        reopen.setEnabled(self._can_reopen)
        for encoding in codec.CHOICES:
            reopen.add_item(encoding.label, lambda e=encoding: self.reopen_chosen.emit(e.key))
        return menu

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(0, 0, self.width(), 1, fluent.qcolor("stroke-divider"))
