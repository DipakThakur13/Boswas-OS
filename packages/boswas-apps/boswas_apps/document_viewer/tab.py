"""One open document: its page view and sidebar, the find bar, and the states around loading.

A tab is in one of four states: asking for the password, showing an error,
showing the document, or (briefly) nothing. Search runs on a thread of its
own over the text of the pages; results arrive page by page, so the first
hit shows at once in a long document and typing never waits.
"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import QFileSystemWatcher, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget

import boswas_fluent as fluent

from . import textsearch
from .document import FAILED, NEEDS_PASSWORD, READY, WRONG_PASSWORD, PdfDocument
from .render import Renderer
from .sidebar import Sidebar
from .view import PdfView

_SEARCH_DELAY_MS = 220
_BATCH_PAGES = 16


class _Searcher(QObject):
    """Finds a pattern in the pages of a document on a thread; hits are delivered in page order."""

    found = Signal(int, list, int)          # generation, hits, pages done
    finished = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.generation = 0
        self._thread: threading.Thread | None = None

    def start(self, document: PdfDocument, pattern) -> int:
        self.generation += 1
        generation = self.generation
        self._thread = threading.Thread(target=self._run, args=(document, pattern, generation),
                                        name="pdf-search", daemon=True)
        self._thread.start()
        return generation

    def cancel(self) -> None:
        self.generation += 1

    def wait(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _run(self, document: PdfDocument, pattern, generation: int) -> None:
        batch: list = []
        try:
            for page in range(document.page_count):
                if generation != self.generation:
                    return
                batch.extend(textsearch.find_in_page(pattern, document.text(page), page))
                if (page + 1) % _BATCH_PAGES == 0:
                    self.found.emit(generation, batch, page + 1)
                    batch = []
            if generation == self.generation:
                self.found.emit(generation, batch, document.page_count)
                self.finished.emit(generation)
        except RuntimeError:                # the tab was closed under the thread
            return


class FindBar(QWidget):
    query_changed = Signal()
    next_requested = Signal()
    previous_requested = Signal()
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.box = fluent.SearchBox(placeholder="Find in document")
        self.box.setFixedWidth(280)
        self.case = QToolButton()
        self.case.setText("Aa")
        self.case.setCheckable(True)
        self.case.setToolTip("Match upper and lower case")
        self.case.setAccessibleName("Match case")
        self.words = QToolButton()
        self.words.setText("“ab”")
        self.words.setCheckable(True)
        self.words.setToolTip("Whole words only")
        self.words.setAccessibleName("Whole words")
        self.previous = fluent.IconButton("go-up", "Previous match (Shift+F3)")
        self.next = fluent.IconButton("go-down", "Next match (F3)")
        self.count = fluent.Label("", "caption", tone="secondary")
        self.count.setMinimumWidth(96)
        self.notice = fluent.Label("", "caption", tone="secondary")
        self.close_button = fluent.IconButton(fluent.glyph_icon("close"), "Close the find bar (Esc)", icon_size=12)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(6)
        for widget in (self.box, self.case, self.words, self.previous, self.next, self.count, self.notice):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(self.close_button)
        self.box.textChanged.connect(lambda _text: self.query_changed.emit())
        self.case.toggled.connect(lambda _on: self.query_changed.emit())
        self.words.toggled.connect(lambda _on: self.query_changed.emit())
        self.box.query_submitted.connect(lambda _text: self.next_requested.emit())
        self.next.clicked.connect(self.next_requested)
        self.previous.clicked.connect(self.previous_requested)
        self.close_button.clicked.connect(self.closed)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.closed.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.previous_requested.emit()
            return
        super().keyPressEvent(event)


class DocumentTab(QWidget):
    changed = Signal()                       # title, state or anything the command bar shows
    status = Signal(str)                     # text for the status line (a link under the pointer)
    web_link = Signal(str)
    changed_on_disk = Signal()
    context_requested = Signal(QPoint)

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = os.path.abspath(path)
        self.document = PdfDocument(self.path, self)
        self.renderer: Renderer | None = None
        self.state = ""
        self.hits: list = []
        self._hits_by_page: dict[int, list[int]] = {}
        self._rects: dict[int, list] = {}
        self.current_hit = -1
        self.searching = False
        self._pending_first = False
        self._query = ""
        self._searcher = _Searcher(self)
        self._searcher.found.connect(self._found)
        self._searcher.finished.connect(self._search_finished)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._start_search)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._file_event)
        self._watcher.directoryChanged.connect(self._file_event)
        self._disk_timer = QTimer(self)
        self._disk_timer.setSingleShot(True)
        self._disk_timer.setInterval(600)
        self._disk_timer.timeout.connect(self._check_disk)
        self._signature: tuple | None = None

        self.view = PdfView()
        self.sidebar = Sidebar()
        self.find_bar = FindBar()
        self.find_bar.hide()
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.view)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([250, 900])
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.find_bar)
        content_layout.addWidget(self.splitter, 1)

        self.password_box = fluent.PasswordBox(placeholder="Password")
        self.password_box.setFixedWidth(280)
        self.unlock_button = fluent.Button("Open", accent=True)
        self.password_title = fluent.Label("This document is protected", "subtitle")
        self.password_text = fluent.Label("Enter its password to open it.", "body", tone="secondary")
        self.password_error = fluent.Label("", "caption", tone="danger")
        self.password_error.setMinimumHeight(self.password_error.fontMetrics().height() + 2)
        password_page = QWidget()
        password_layout = QVBoxLayout(password_page)
        password_layout.setSpacing(12)
        password_layout.addStretch(2)
        icon = fluent.IconWidget(fluent.icon("system-lock-screen", "dialog-password"), 40)
        for widget in (icon, self.password_title, self.password_text, self.password_box, self.password_error,
                       self.unlock_button):
            password_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        password_layout.addStretch(3)
        self.unlock_button.clicked.connect(self._unlock)
        self.password_box.returnPressed.connect(self._unlock)

        self.error_page = fluent.EmptyState("This document cannot be opened", "", "dialog-error", "Try again")
        self.error_page.action_triggered.connect(self.reload)

        self.stack = QStackedWidget()
        for page in (content, password_page, self.error_page):
            self.stack.addWidget(page)
        self._content, self._password_page = content, password_page
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        self.view.link_hovered.connect(self.status)
        self.view.web_link_clicked.connect(self.web_link)
        self.view.page_changed.connect(self.sidebar.set_current_page)
        self.view.page_changed.connect(lambda _page: self.changed.emit())
        self.view.zoom_changed.connect(lambda _zoom, _mode: self.changed.emit())
        self.view.selection_changed.connect(self.changed)
        self.view.jumped.connect(self.changed)
        self.view.context_requested.connect(self.context_requested)
        self.sidebar.page_requested.connect(self._sidebar_page)
        self.sidebar.result_requested.connect(self.go_to_hit)
        self.find_bar.query_changed.connect(self._query_edited)
        self.find_bar.next_requested.connect(self.find_next)
        self.find_bar.previous_requested.connect(self.find_previous)
        self.find_bar.closed.connect(self.close_find)

    # --- loading ---
    def title(self) -> str:
        return os.path.basename(self.path)

    def is_ready(self) -> bool:
        return self.state == READY

    def load(self, password: str | None = None, view_state: dict | None = None) -> str:
        """Open the file and show it, the password page or the error page; returns the state."""
        self._stop_work()
        self.state = self.document.load(password)
        if self.state == READY:
            self.renderer = Renderer(self.document.qt, self)
            self.view.set_document(self.document, self.renderer)
            self.sidebar.set_document(self.document, self.renderer)
            if view_state:
                self.view.restore_view(view_state)
            self.stack.setCurrentWidget(self._content)
            self.view.setFocus()
        elif self.state in (NEEDS_PASSWORD, WRONG_PASSWORD):
            self.password_error.setText("That password is not correct. Try again." if self.state == WRONG_PASSWORD
                                        else "")
            self.password_box.clear()
            self.stack.setCurrentWidget(self._password_page)
            self.password_box.setFocus()
        else:
            self.error_page.set_title(f"“{self.title()}” cannot be opened")
            self.error_page.set_text(self.document.error_text)
            self.stack.setCurrentWidget(self.error_page)
        self._watch()
        self.changed.emit()
        return self.state

    def _unlock(self) -> None:
        password = self.password_box.password()
        if password:
            self.load(password)

    def reload(self) -> None:
        """Read the file again and show the same place in it."""
        state = self.view.view_state() if self.is_ready() else None
        password = self.document.password or None
        query = self._query
        self.load(password, state)
        if query and self.is_ready():
            self._start_search()

    def _stop_work(self) -> None:
        self._searcher.cancel()
        self._searcher.wait()
        self._clear_hits()
        self.view.set_document(None, None)
        self.sidebar.set_document(None, None)
        if self.renderer is not None:
            self.renderer.stop()
            self.renderer.deleteLater()
            self.renderer = None

    def shutdown(self) -> None:
        """Stop the threads and close the file (the tab is being closed)."""
        self._search_timer.stop()
        self._disk_timer.stop()
        self._stop_work()
        self.document.close()

    # --- the file on disk ---
    def _stat(self) -> tuple | None:
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size, info.st_ino

    def _watch(self) -> None:
        self._signature = self._stat()
        watched = self._watcher.files() + self._watcher.directories()
        if watched:
            self._watcher.removePaths(watched)
        paths = [path for path in (self.path, os.path.dirname(self.path)) if os.path.exists(path)]
        if paths:
            self._watcher.addPaths(paths)

    def _file_event(self, _path: str) -> None:
        self._disk_timer.start()                   # programs write in several steps: wait until it is quiet

    def _check_disk(self) -> None:
        signature = self._stat()
        if signature is not None and signature != self._signature:
            self._signature = signature
            if self.path not in self._watcher.files():
                self._watcher.addPath(self.path)       # a replaced file is a new file to watch
            self.changed_on_disk.emit()

    # --- sidebar ---
    def _sidebar_page(self, page: int, y: float) -> None:
        self.view.go_to_page(page, y or None, record=True)
        self.changed.emit()

    # --- search ---
    def open_find(self) -> None:
        if not self.is_ready():
            return
        self.find_bar.show()
        selected = self.view.selected_text().strip()
        if selected and "\n" not in selected and len(selected) <= 200:
            self.find_bar.box.setText(selected)
        self.find_bar.box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.find_bar.box.selectAll()

    def close_find(self) -> None:
        self.find_bar.hide()
        self._searcher.cancel()
        self._search_timer.stop()
        self._clear_hits()
        self._query = ""
        self.find_bar.count.setText("")
        self.find_bar.notice.setText("")
        self.sidebar.set_results([], "")
        self.view.setFocus()

    def search_for(self, text: str) -> None:
        """Search for ``text`` now (the selection, from the context menu)."""
        if not self.is_ready():
            return
        self.find_bar.show()
        self.find_bar.box.setText(text)
        self._search_timer.stop()
        self._start_search()
        self.sidebar.show_tab("results")

    def _query_edited(self) -> None:
        self._search_timer.start()

    def _clear_hits(self) -> None:
        self.hits = []
        self._hits_by_page = {}
        self._rects = {}
        self.current_hit = -1
        self.searching = False
        self.view.set_hit_source(None)
        self.view.set_current_hit(-1, None)

    def _start_search(self) -> None:
        if not self.is_ready():
            return
        self._searcher.cancel()
        self._clear_hits()
        self._query = self.find_bar.box.query()
        pattern = textsearch.compile_query(self._query, self.find_bar.case.isChecked(), self.find_bar.words.isChecked())
        self.find_bar.notice.setText("")
        if pattern is None:
            self.find_bar.count.setText("")
            self.sidebar.set_results([], "")
            return
        self.searching = True
        self._pending_first = True
        self.find_bar.count.setText("Searching…")
        self.sidebar.set_results([], self._query, searching=True)
        self.view.set_hit_source(self._page_hit_rects)
        self._searcher.start(self.document, pattern)

    def _found(self, generation: int, hits: list, _pages_done: int) -> None:
        if generation != self._searcher.generation:
            return
        base = len(self.hits)
        self.hits.extend(hits)
        for offset, hit in enumerate(hits):
            self._hits_by_page.setdefault(hit.page, []).append(base + offset)
        self.sidebar.add_results(hits, self._query, True)
        if self._pending_first and self.hits and self.hits[-1].page >= self.view.current_page():
            self._pending_first = False
            self._select_hit(textsearch.first_at_or_after(self.hits, self.view.current_page()))
        self._update_count()
        if hits:
            self.view.viewport().update()

    def _search_finished(self, generation: int) -> None:
        if generation != self._searcher.generation:
            return
        self.searching = False
        if self._pending_first and self.hits:
            self._pending_first = False
            self._select_hit(textsearch.first_at_or_after(self.hits, self.view.current_page()))
        self.sidebar.add_results([], self._query, False)
        self._update_count()

    def _update_count(self) -> None:
        if not self.hits:
            self.find_bar.count.setText("Searching…" if self.searching else "No matches")
        elif self.current_hit >= 0:
            more = "+" if self.searching else ""
            self.find_bar.count.setText(f"{self.current_hit + 1:,} of {len(self.hits):,}{more}")
        else:
            self.find_bar.count.setText(f"{len(self.hits):,} matches")

    def _hit_rects(self, index: int) -> list:
        rects = self._rects.get(index)
        if rects is None:
            hit = self.hits[index]
            rects = self.document.rects_at(hit.page, hit.start, hit.length)
            if len(self._rects) > 20_000:
                self._rects.clear()
            self._rects[index] = rects
        return rects

    def _page_hit_rects(self, page: int) -> list:
        return [self._hit_rects(index) for index in self._hits_by_page.get(page, ()) if index != self.current_hit]

    def _select_hit(self, index: int) -> None:
        if not 0 <= index < len(self.hits):
            return
        self.current_hit = index
        hit = self.hits[index]
        rects = self._hit_rects(index)
        self.view.set_current_hit(hit.page, rects)
        self.view.show_rects(hit.page, rects)
        self.sidebar.select_result(index)
        self._update_count()

    def go_to_hit(self, index: int) -> None:
        self.find_bar.notice.setText("")
        self._select_hit(index)

    def find_next(self, forward: bool = True) -> None:
        if not self.is_ready():
            return
        if not self.find_bar.isVisible() or not self.find_bar.box.query():
            self.open_find()
            return
        if self._search_timer.isActive() or self.find_bar.box.query() != self._query:
            self._search_timer.stop()
            self._start_search()
            return
        index, wrapped = textsearch.step(len(self.hits), self.current_hit, forward)
        if index < 0:
            return
        if wrapped and self.searching:
            return                                   # more hits are still coming: do not wrap yet
        self.find_bar.notice.setText(("Continued from the top" if forward else "Continued from the bottom")
                                     if wrapped else "")
        self._select_hit(index)

    def find_previous(self) -> None:
        self.find_next(forward=False)
