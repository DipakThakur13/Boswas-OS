"""FindBar: search in the scrollback of one terminal.

The bar sits at the top right corner of the terminal. Matches are found in
logical lines (a match may run across a wrap), newest first: Enter and the
up button go to the previous (older) match, Shift+Enter and the down button
to the next one. The search itself runs in short slices between events on
a snapshot of the lines, so a long scrollback never holds up typing, and it
runs again a moment after the terminal's content has changed.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

import boswas_fluent as fluent

from . import grid
from .view import TerminalView

SLICE_SECONDS = 0.006
TYPE_DELAY_MS = 120
REFRESH_DELAY_MS = 400
MAX_NEEDLE = 512


class FindBar(QWidget):
    closed = Signal()

    def __init__(self, view: TerminalView, parent: QWidget) -> None:
        super().__init__(parent)
        self.view = view
        self._matches: list[list[grid.Span]] = []
        self._current = -1
        self._search = None                 # the running search: a generator
        self._found: list[list[grid.Span]] = []
        self._keep_line: int | None = None
        self.setObjectName("terminalFindBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        self.box = fluent.SearchBox(self, placeholder="Find")
        self.box.setMaxLength(MAX_NEEDLE)
        self.box.setMinimumWidth(200)
        self.box.search_action.setVisible(False)
        self.count_label = fluent.Label("", "caption", self, tone="secondary")
        self.count_label.setMinimumWidth(76)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.previous_button = fluent.IconButton("go-up-symbolic", "Previous match (Enter)", self)
        self.next_button = fluent.IconButton("go-down-symbolic", "Next match (Shift+Enter)", self)
        self.case_button = QToolButton(self)
        self.case_button.setText("Aa")
        self.case_button.setCheckable(True)
        self.case_button.setAutoRaise(True)
        self.case_button.setFixedSize(32, 32)
        self.case_button.setToolTip("Match case")
        self.case_button.setAccessibleName("Match case")
        self.all_button = fluent.IconButton("view-list-symbolic", "Highlight all matches", self, checkable=True)
        self.all_button.setChecked(True)
        self.close_button = fluent.IconButton(fluent.glyph_icon("close"), "Close (Esc)", self, icon_size=12)
        for widget in (self.box, self.count_label, self.previous_button, self.next_button, self.case_button,
                       self.all_button, self.close_button):
            layout.addWidget(widget)
        self._type_timer = QTimer(self)
        self._type_timer.setSingleShot(True)
        self._type_timer.timeout.connect(self.restart)
        self._slice_timer = QTimer(self)
        self._slice_timer.setInterval(0)
        self._slice_timer.timeout.connect(self._slice)
        self.box.textChanged.connect(lambda _text: self._type_timer.start(TYPE_DELAY_MS))
        self.previous_button.clicked.connect(self.go_previous)
        self.next_button.clicked.connect(self.go_next)
        self.case_button.toggled.connect(lambda _on: self.restart())
        self.all_button.toggled.connect(lambda _on: self._show())
        self.close_button.clicked.connect(self.close_bar)
        self.view.content_changed.connect(self._content_changed)
        self.view.grid_resized.connect(lambda _c, _r: self._type_timer.start(TYPE_DELAY_MS))
        self.hide()

    # --- showing ---
    def open_bar(self) -> None:
        self.show()
        self.raise_()
        selected = self.view.selected_text()
        if selected and "\n" not in selected and len(selected) <= MAX_NEEDLE:
            self.box.setText(selected)
        self.box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.box.selectAll()
        self.restart()

    def close_bar(self) -> None:
        if not self.isVisible():
            return
        self._stop()
        self._matches = []
        self._current = -1
        self.view.set_matches([])
        self.hide()
        self.view.setFocus(Qt.FocusReason.OtherFocusReason)
        self.closed.emit()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        radius = fluent.theme().radius("card")
        fluent.paint.rounded_rect(painter, self.rect(), radius, "flyout", "stroke-surface")

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_bar()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.go_next()
            else:
                self.go_previous()
        elif key == Qt.Key.Key_F3:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.go_previous()
            else:
                self.go_next()
        else:
            super().keyPressEvent(event)

    # --- searching ---
    def needle(self) -> str:
        return self.box.text()

    def matches(self) -> list[list[grid.Span]]:
        return self._matches

    def current(self) -> int:
        return self._current

    def is_searching(self) -> bool:
        return self._search is not None

    def _stop(self) -> None:
        self._search = None
        self._slice_timer.stop()
        self._type_timer.stop()

    def restart(self) -> None:
        """Search again from the start (the text, the options or the terminal changed)."""
        self._stop()
        needle = self.needle()
        if not needle or not self.isVisible():
            self._matches, self._current = [], -1
            self._show()
            return
        if 0 <= self._current < len(self._matches):
            self._keep_line = self._matches[self._current][0][0]
        entries, first = self.view.snapshot()
        self._found = []
        self._search = grid.search(entries, grid.search_pattern(needle, self.case_button.isChecked()), first)
        self._slice_timer.start()

    def _slice(self) -> None:
        search = self._search
        if search is None:
            self._slice_timer.stop()
            return
        deadline = time.monotonic() + SLICE_SECONDS
        found = self._found
        for matches in search:
            if matches:
                found.extend(matches)
                if len(found) >= grid.MAX_MATCHES:
                    del found[grid.MAX_MATCHES:]
                    break
            if time.monotonic() > deadline:
                return
        self._search = None
        self._slice_timer.stop()
        self._matches = found
        self._found = []
        self._current = self._nearest(self._keep_line)
        self._keep_line = None
        self._show(reveal=True)

    def finish(self) -> None:
        """Run a pending search to its end now (tests)."""
        if self._type_timer.isActive():
            self._type_timer.stop()
            self.restart()
        while self._search is not None:
            self._slice()

    def _nearest(self, line: int | None) -> int:
        """The match to start from: the one at or before ``line``, else the last (the newest)."""
        if not self._matches:
            return -1
        if line is None:
            return len(self._matches) - 1
        best = 0
        for index, match in enumerate(self._matches):
            if match[0][0] <= line:
                best = index
            else:
                break
        return best

    def _show(self, reveal: bool = False) -> None:
        count = len(self._matches)
        if not self.needle():
            self.count_label.setText("")
        elif not count:
            self.count_label.setText("No results")
        else:
            more = "+" if count >= grid.MAX_MATCHES else ""
            self.count_label.setText(f"{self._current + 1} of {count}{more}")
        self.previous_button.setEnabled(count > 0)
        self.next_button.setEnabled(count > 0)
        if not self.isVisible():
            return
        if self.all_button.isChecked() or self._current < 0:
            self.view.set_matches(self._matches if self.all_button.isChecked() else [], self._current)
        else:
            self.view.set_matches([self._matches[self._current]], 0)
        if reveal and 0 <= self._current < count:
            self.view.reveal(self._matches[self._current][0][0])

    def go_previous(self) -> None:
        self._step(-1)

    def go_next(self) -> None:
        self._step(1)

    def _step(self, direction: int) -> None:
        if self._search is not None or self._type_timer.isActive():
            self.finish()
        if not self._matches:
            return
        self._current = (self._current + direction) % len(self._matches)
        self._show(reveal=True)

    def _content_changed(self) -> None:
        if self.isVisible() and self.needle() and self._search is None:
            self._type_timer.start(REFRESH_DELAY_MS)
