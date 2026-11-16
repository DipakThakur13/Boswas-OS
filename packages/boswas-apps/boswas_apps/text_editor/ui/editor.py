"""The text area and one tab of the editor: the text of a file with what the editor knows about the file."""

from __future__ import annotations

import os

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QFontMetricsF, QPainter, QTextCursor, QTextOption
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import codec, fileio

PARAGRAPH = chr(0x2029)              # what a Qt document has between its lines
INSERT_CHUNK = 512 * 1024              # characters put into the document per step while a file loads
# The text area is the page itself, not a text box on a page: the frame, fill and padding the application
# style sheet gives every QPlainTextEdit are taken off. No colour, radius or font is set here; the toolkit
# has no frameless variant of a text area (as QLineEdit[frame="false"] is for a line edit).
_PLAIN_AREA = "QPlainTextEdit { border: none; border-radius: 0; background: transparent; padding: 0; }"


class LineNumbers(QWidget):
    """The margin with the line numbers of a text area."""

    def __init__(self, area: "TextArea") -> None:
        super().__init__(area)
        self.area = area
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def sizeHint(self) -> QSize:
        return QSize(self.area.line_number_width(), 0)

    def paintEvent(self, event) -> None:
        area = self.area
        painter = QPainter(self)
        painter.setFont(area.font())
        painter.setPen(fluent.qcolor("text-muted"))
        block = area.firstVisibleBlock()
        top = round(area.blockBoundingGeometry(block).translated(area.contentOffset()).top())
        height = area.fontMetrics().height()
        current = area.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            bottom = top + round(area.blockBoundingRect(block).height())
            if block.isVisible() and bottom >= event.rect().top():
                if block.blockNumber() == current:
                    painter.setPen(fluent.qcolor("text"))
                painter.drawText(QRect(0, top, self.width() - 8, height),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                                 str(block.blockNumber() + 1))
                if block.blockNumber() == current:
                    painter.setPen(fluent.qcolor("text-muted"))
            block = block.next()
            top = bottom


class TextArea(QPlainTextEdit):
    """The text of a tab: a plain text editor with indentation keys, zoom and optional line numbers."""

    zoom_requested = Signal(int)                 # +1 or -1 (Ctrl and the wheel)
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(_PLAIN_AREA)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setAccessibleName("Text")
        self.auto_indent = True
        self.spaces_for_tabs = False
        self.tab_width = 8
        self._base_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._zoom = 100
        self._numbers = LineNumbers(self)
        self._numbers.hide()
        self.setTabChangesFocus(False)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.blockCountChanged.connect(lambda _count: self._sync_margin())
        self.updateRequest.connect(self._update_numbers)
        self.cursorPositionChanged.connect(self._numbers.update)
        self.document().setDocumentMargin(8)
        self.document().setModified(False)     # setting the margin counts as a change of the document

    # --- appearance ---
    def apply_settings(self, settings: dict) -> None:
        """Font, wrapping, indentation and line numbers from the settings."""
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        if settings.get("font_family"):
            font = QFont(str(settings["font_family"]))
        font.setPointSizeF(float(settings["font_size"]))
        font.setBold(bool(settings["font_bold"]))
        font.setItalic(bool(settings["font_italic"]))
        self._base_font = font
        self.auto_indent = bool(settings["auto_indent"])
        self.spaces_for_tabs = bool(settings["spaces_for_tabs"])
        self.tab_width = int(settings["tab_width"])
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth if settings["word_wrap"]
                             else QPlainTextEdit.LineWrapMode.NoWrap)
        self._numbers.setVisible(bool(settings["line_numbers"]))
        self._apply_font()

    def zoom(self) -> int:
        return self._zoom

    def set_zoom(self, percent: int) -> None:
        self._zoom = percent
        self._apply_font()

    def _apply_font(self) -> None:
        font = QFont(self._base_font)
        font.setPointSizeF(max(1.0, self._base_font.pointSizeF() * self._zoom / 100))
        self.setFont(font)
        self.setTabStopDistance(QFontMetricsF(font).horizontalAdvance(" ") * self.tab_width)
        self._sync_margin()

    def line_numbers_shown(self) -> bool:
        return not self._numbers.isHidden()

    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits

    def _sync_margin(self) -> None:
        width = self.line_number_width() if self.line_numbers_shown() else 0
        self.setViewportMargins(width, 0, 0, 0)
        rect = self.contentsRect()
        self._numbers.setGeometry(rect.left(), rect.top(), width, rect.height())

    def _update_numbers(self, rect: QRect, dy: int) -> None:
        if not self.line_numbers_shown():
            return
        if dy:
            self._numbers.scroll(0, dy)
        else:
            self._numbers.update(0, rect.y(), self._numbers.width(), rect.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_margin()

    # --- text ---
    def raw_text(self) -> str:
        """The text with ``\\n`` line ends, exactly as typed (toPlainText would turn no-break spaces into spaces)."""
        return self.document().toRawText().replace(PARAGRAPH, "\n")

    def position(self) -> tuple[int, int]:
        """(line, column) of the cursor, both from 1."""
        cursor = self.textCursor()
        return cursor.blockNumber() + 1, cursor.positionInBlock() + 1

    def go_to_line(self, line: int) -> bool:
        block = self.document().findBlockByNumber(line - 1)
        if not block.isValid():
            return False
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        self.centerCursor()
        return True

    def select_range(self, start: int, end: int) -> None:
        cursor = self.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.centerCursor() if not self.viewport().rect().contains(self.cursorRect()) else None

    def replace_all_text(self, text: str) -> None:
        """Replace the whole text as one step that Undo takes back; the cursor keeps its line and column."""
        line, column = self.position()
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text)
        cursor.endEditBlock()
        block = self.document().findBlockByNumber(min(line, self.blockCount()) - 1)
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + min(column - 1, max(block.length() - 1, 0)))
        self.setTextCursor(cursor)

    # --- indentation ---
    def _indent_unit(self) -> str:
        return " " * self.tab_width if self.spaces_for_tabs else "\t"

    def _selected_blocks(self) -> tuple[int, int]:
        cursor = self.textCursor()
        document = self.document()
        first = document.findBlock(cursor.selectionStart()).blockNumber()
        last_block = document.findBlock(cursor.selectionEnd())
        last = last_block.blockNumber()
        if cursor.hasSelection() and cursor.selectionEnd() == last_block.position() and last > first:
            last -= 1                      # a selection that ends at the start of a line does not include it
        return first, last

    def indent_selection(self) -> None:
        first, last = self._selected_blocks()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for number in range(first, last + 1):
            block = self.document().findBlockByNumber(number)
            if block.length() > 1:         # an empty line stays empty
                edit = QTextCursor(block)
                edit.insertText(self._indent_unit())
        cursor.endEditBlock()

    def unindent_selection(self) -> None:
        first, last = self._selected_blocks()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for number in range(first, last + 1):
            block = self.document().findBlockByNumber(number)
            text = block.text()
            remove = 0
            if text.startswith("\t"):
                remove = 1
            else:
                while remove < self.tab_width and remove < len(text) and text[remove] == " ":
                    remove += 1
            if remove:
                edit = QTextCursor(block)
                edit.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, remove)
                edit.removeSelectedText()
        cursor.endEditBlock()

    def keyPressEvent(self, event) -> None:
        key, modifiers = event.key(), event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if self.isReadOnly():
            super().keyPressEvent(event)
            return
        cursor = self.textCursor()
        if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and modifiers == Qt.KeyboardModifier.ShiftModifier):
            self.unindent_selection()
            return
        if key == Qt.Key.Key_Tab and modifiers == Qt.KeyboardModifier.NoModifier:
            first, last = self._selected_blocks()
            if cursor.hasSelection() and last > first:
                self.indent_selection()
            elif self.spaces_for_tabs:
                column = cursor.positionInBlock() if not cursor.hasSelection() else 0
                cursor.insertText(" " * (self.tab_width - column % self.tab_width))
            else:
                cursor.insertText("\t")
            return
        if (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and modifiers == Qt.KeyboardModifier.NoModifier
                and self.auto_indent):
            text = cursor.block().text()[:cursor.positionInBlock()] if not cursor.hasSelection() else \
                self.document().findBlock(cursor.selectionStart()).text()
            indent = text[:len(text) - len(text.lstrip(" \t"))]
            cursor.insertText("\n" + indent)
            self.ensureCursorVisible()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoom_requested.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    # --- files dropped on the text open as tabs; dropped text is inserted as always ---
    @staticmethod
    def _local_files(mime) -> list[str]:
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()] if mime.hasUrls() else []

    def dragEnterEvent(self, event) -> None:
        if self._local_files(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if self._local_files(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        files = self._local_files(event.mimeData())
        if files:
            event.acceptProposedAction()
            self.files_dropped.emit(files)
            return
        super().dropEvent(event)

    def insertFromMimeData(self, source) -> None:
        # Pasted or dropped text comes in as plain text with \n line ends.
        if source.hasText():
            self.insertPlainText(codec.normalize_line_endings(source.text()).replace("\x00", ""))


class EditorTab(QWidget):
    """One tab: the text area, the message bar above it and what is known about the file."""

    title_changed = Signal()
    load_finished = Signal(bool)                 # True: the text is in; False: cancelled

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path: str | None = None
        self.untitled = title
        self.encoding = codec.UTF8
        self.line_ending = codec.LF
        self.signature: fileio.Signature | None = None
        self.loading = False
        self.saving = False
        self.missing_on_disk = False              # a path given to open that does not exist yet
        self.change_noticed: fileio.Signature | None = None
        self._pending: list[str] = []
        self._inserted = 0
        self._total = 0
        self._cancel_load = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.info = fluent.InfoBar("info", parent=self)
        self.info.hide()
        self._info_slot = None
        self.info.action_triggered.connect(lambda: self._info_slot() if self._info_slot is not None else None)
        holder = QHBoxLayout()
        holder.setContentsMargins(8, 4, 8, 0)
        holder.addWidget(self.info)
        layout.addLayout(holder)
        self.area = TextArea(self)
        layout.addWidget(self.area, 1)

        self.progress = QWidget(self)
        box = QVBoxLayout(self.progress)
        box.setSpacing(12)
        box.addStretch(1)
        self.ring = fluent.ProgressRing(self.progress, size=40, indeterminate=False)
        self.progress_label = fluent.Label("Opening…", "body", self.progress, tone="secondary")
        self.cancel_button = fluent.Button("Cancel", self.progress)
        self.cancel_button.clicked.connect(self.cancel_load)
        for widget in (self.ring, self.progress_label, self.cancel_button):
            box.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        box.addStretch(1)
        self.progress.hide()
        self._insert_timer = QTimer(self)
        self._insert_timer.setInterval(0)
        self._insert_timer.timeout.connect(self._insert_step)
        self.area.document().modificationChanged.connect(lambda _changed: self.title_changed.emit())

    # --- names ---
    def name(self) -> str:
        return os.path.basename(self.path) if self.path else self.untitled

    def is_modified(self) -> bool:
        return self.area.document().isModified()

    def is_pristine(self) -> bool:
        """An untitled tab nobody typed into: opening a file may take its place."""
        return self.path is None and not self.is_modified() and self.area.document().isEmpty() and not self.loading

    def text(self) -> str:
        return self.area.raw_text()

    def characters(self) -> int:
        return self.area.document().characterCount() - 1

    # --- loading ---
    def show_progress(self, text: str, fraction: float | None = None) -> None:
        self.loading = True
        self.area.setReadOnly(True)
        self.progress_label.setText(text)
        if fraction is not None:
            self.ring.set_value(fraction * 100)
        self.progress.setGeometry(self.rect())
        self.progress.show()
        self.progress.raise_()

    def set_progress(self, fraction: float) -> None:
        self.ring.set_value(max(0.0, min(1.0, fraction)) * 100)

    def cancel_load(self) -> None:
        self._cancel_load = True

    def load_cancelled(self) -> bool:
        return self._cancel_load

    def finish_progress(self) -> None:
        self.loading = False
        self._cancel_load = False
        self.progress.hide()
        self.area.setReadOnly(False)

    def set_text(self, text: str, *, modified: bool = False, cursor: int = 0) -> None:
        """Put text into the tab piece by piece, so a large file does not hold up the window.

        ``load_finished`` is emitted when it is all in (or the loading was cancelled).
        """
        document = self.area.document()
        document.setUndoRedoEnabled(False)
        self.area.setPlainText("")
        self._final = (modified, cursor)
        if len(text) <= INSERT_CHUNK:
            self.area.setPlainText(text)
            self._text_in()
            return
        self._pending = []
        start = 0
        while start < len(text):
            end = min(len(text), start + INSERT_CHUNK)
            if end < len(text):
                newline = text.rfind("\n", start, end)
                if newline > start:
                    end = newline + 1
                elif 0xD800 <= ord(text[end - 1]) <= 0xDBFF:
                    end -= 1
            self._pending.append(text[start:end])
            start = end
        self._pending.reverse()
        self._inserted, self._total = 0, len(text)
        self.show_progress(f"Opening {self.name()}…", 0.5)
        self._insert_timer.start()

    def _insert_step(self) -> None:
        if self._cancel_load:
            self._insert_timer.stop()
            self._pending = []
            self.area.setPlainText("")
            self.area.document().setUndoRedoEnabled(True)
            self.finish_progress()
            self.load_finished.emit(False)
            return
        if not self._pending:
            self._insert_timer.stop()
            self._text_in()
            return
        piece = self._pending.pop()
        cursor = QTextCursor(self.area.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(piece)
        self._inserted += len(piece)
        self.set_progress(0.5 + 0.5 * self._inserted / max(self._total, 1))

    def _text_in(self) -> None:
        modified, position = self._final
        document = self.area.document()
        document.setUndoRedoEnabled(True)
        document.setModified(modified)
        cursor = self.area.textCursor()
        cursor.setPosition(min(position, max(document.characterCount() - 1, 0)))
        self.area.setTextCursor(cursor)
        self.finish_progress()
        self.title_changed.emit()
        self.load_finished.emit(True)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.progress.isVisible():
            self.progress.setGeometry(self.rect())

    # --- messages ---
    def show_message(self, severity: str, message: str, action: str | None = None, slot=None) -> None:
        """Show the message bar of the tab, optionally with one action button."""
        self.info.show_message(severity, message)
        self._info_slot = slot if self.info.set_action(action) is not None else None

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), fluent.qcolor("layer"))
