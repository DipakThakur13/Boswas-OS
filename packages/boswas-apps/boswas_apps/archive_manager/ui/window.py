"""The window of Archive Manager: an archive shown like a folder in the Files application."""

from __future__ import annotations

import os
import shutil
import tempfile

from PySide6.QtCore import QModelIndex, QPoint, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QSplitter,
                               QStackedWidget, QTreeView, QVBoxLayout, QWidget)

import boswas_fluent as fluent

from .. import APP_ID, DISPLAY_NAME, create, extract, formats, model, names, previewing, reader
from ..errors import ArchiveError, Cancelled, PasswordRequired, UnsupportedFormat, WrongPassword
from ..jobs import OVERWRITE, PASSWORD, SIZE, Job
from ..state import State
from . import dialogs, listmodel
from .dialogs import Result
from .preview import PreviewPane, decode_image

_COLUMN_WIDTHS = {listmodel.NAME: 300, listmodel.SIZE: 88, listmodel.PACKED: 98, listmodel.MODIFIED: 140,
                  listmodel.TYPE: 152, listmodel.CRC: 94, listmodel.MODE: 104, listmodel.LOCATION: 200}
_SEARCH_DELAY_MS = 160
_PREVIEW_DELAY_MS = 140


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


class ArchiveWindow(fluent.FluentWindow):
    """One archive (or the start page when none is open)."""

    def __init__(self, path: str | None = None, *, password: str | None = None) -> None:
        super().__init__(APP_ID, DISPLAY_NAME, icon="utilities-file-archiver", size=(1240, 740),
                         minimum_size=(680, 440))
        self.worker = fluent.Worker(self, max_threads=3)
        self.preview_worker = fluent.Worker(self, max_threads=1)      # previews never hold up an operation
        try:
            self.state = State(fluent.state_dir(APP_ID))
        except OSError:
            self.state = State(None)
        self.archive: reader.Archive | None = None
        self.path: str | None = None
        self.password = password
        self.folder = ""
        self._back: list[str] = []
        self._forward: list[str] = []
        self.job: Job | None = None
        self._job_kind = ""
        self.dialog: fluent.ContentDialog | None = None
        self._preview_job: Job | None = None
        self._preview_entry = None
        self._temporary: str | None = None
        self._opened = 0
        self._after_reload: str | None = None
        self._last_result = None
        self._others: list[ArchiveWindow] = []
        # Replaceable in tests: nothing below opens a file chooser or another application directly.
        self.choose_files = self._choose_files
        self.choose_folder = self._choose_folder
        self.choose_archive = self._choose_archive
        self.open_path = fluent.open_path
        self._build()
        self._shortcuts()
        self.setAcceptDrops(True)
        self._sync()
        if path:
            self.open_archive(path)

    # --- construction ---
    def _build(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.commands = fluent.CommandBar()
        add = self.commands.add_command
        self.new_action = add("New", "document-new", self.new_archive, shortcut="Ctrl+N",
                              tooltip="Create a new archive")
        self.open_action = add("Open", "document-open", self.browse_archive, shortcut="Ctrl+O",
                               tooltip="Open an archive")
        self.commands.add_separator()
        self.extract_all_action = add("Extract all", "archive-extract", self.extract_all, shortcut="Ctrl+E",
                                      tooltip="Extract everything in the archive")
        self.extract_action = add("Extract", "archive-extract", self.extract_selected, shortcut="Ctrl+Shift+E",
                                  tooltip="Extract the selected items")
        self.commands.add_separator()
        self.add_files_action = add("Add files", "archive-insert", self.add_files,
                                    tooltip="Add files to this folder of the archive")
        self.add_folder_action = add("Add folder", "archive-insert-directory", self.add_folder,
                                     tooltip="Add a folder to this folder of the archive")
        self.delete_action = add("Delete", "edit-delete", self.delete_selected, tooltip="Delete from the archive (Del)")
        self.commands.add_separator()
        self.test_action = add("Test", "emblem-ok", self.test_archive, tooltip="Check the archive for damage")
        self.properties_action = add("Properties", "document-properties", self.show_properties,
                                     shortcut="Alt+Return")
        self.open_entry_action = add("Open selected item", "document-open", self.open_selected, primary=False)
        self.reload_action = add("Reload", "view-refresh", self.reload, shortcut="F5", primary=False)
        self.select_all_action = add("Select all", None, self.select_all, primary=False)
        self.preview_action = add("Preview pane", "view-preview", self._toggle_preview, shortcut="Alt+P",
                                  primary=False, checkable=True)
        self.close_action = add("Close archive", None, self.close_archive, shortcut="Ctrl+W", primary=False)
        top = QHBoxLayout()
        top.setContentsMargins(12, 8, 12, 4)
        top.addWidget(self.commands)
        outer.addLayout(top)

        self.back_button = fluent.IconButton("go-previous", "Back (Alt+Left)")
        self.forward_button = fluent.IconButton("go-next", "Forward (Alt+Right)")
        self.up_button = fluent.IconButton("go-up", "Up one folder (Alt+Up)")
        self.back_button.clicked.connect(self.go_back)
        self.forward_button.clicked.connect(self.go_forward)
        self.up_button.clicked.connect(self.go_up)
        self.breadcrumb = fluent.BreadcrumbBar()
        self.breadcrumb.item_clicked.connect(self._crumb_clicked)
        self.search = fluent.SearchBox(placeholder="Search in the archive")
        self.search.setFixedWidth(260)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._apply_search)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        self.navigation_row = QWidget()
        nav = QHBoxLayout(self.navigation_row)
        nav.setContentsMargins(12, 4, 12, 8)
        nav.setSpacing(4)
        for button in (self.back_button, self.forward_button, self.up_button):
            nav.addWidget(button)
        nav.addSpacing(8)
        nav.addWidget(self.breadcrumb, 1)
        nav.addSpacing(8)
        nav.addWidget(self.search)
        outer.addWidget(self.navigation_row)

        self.banner = fluent.InfoBar("info")
        self.banner.action_triggered.connect(self._banner_action)
        self.banner.hide()
        self._banner_do = None
        banner_holder = QVBoxLayout()
        banner_holder.setContentsMargins(12, 0, 12, 4)
        banner_holder.addWidget(self.banner)
        outer.addLayout(banner_holder)

        self.stack = QStackedWidget()
        self.start_page = self._build_start()
        self.loading_page = self._build_loading()
        self.browser = self._build_browser()
        self.error_page = fluent.EmptyState("This file cannot be opened", "", "dialog-error", "Open another archive")
        self.error_page.action_triggered.connect(self.browse_archive)
        for page in (self.start_page, self.loading_page, self.browser, self.error_page):
            self.stack.addWidget(page)
        outer.addWidget(self.stack, 1)

        self.progress = dialogs.ProgressPanel()
        self.progress.cancel_requested.connect(self.cancel_job)
        self.progress_card = fluent.Card(padding=12)
        self.progress_card.add(self.progress)
        self.progress_holder = QWidget()
        holder = QVBoxLayout(self.progress_holder)
        holder.setContentsMargins(12, 8, 12, 0)
        holder.addWidget(self.progress_card)
        self.progress_holder.hide()
        outer.addWidget(self.progress_holder)

        self.status_left = fluent.Label("", "caption", tone="secondary")
        self.status_right = fluent.Label("", "caption", tone="secondary")
        self.status_row = QWidget()
        status = QHBoxLayout(self.status_row)
        status.setContentsMargins(16, 6, 16, 8)
        status.addWidget(self.status_left, 1)
        status.addWidget(self.status_right)
        outer.addWidget(self.status_row)
        self.set_content(central)

    def _build_start(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 8, 32, 24)
        layout.setSpacing(12)
        self.welcome = fluent.EmptyState("Open an archive",
                                         "Drop an archive here, or open one to see what is inside and extract it.",
                                         "utilities-file-archiver", "Open archive")
        self.welcome.action_triggered.connect(self.browse_archive)
        self.welcome.setSizePolicy(self.welcome.sizePolicy().horizontalPolicy(), self.welcome.sizePolicy().Policy.Fixed)
        self.new_button = fluent.Button("Create a new archive")
        self.new_button.clicked.connect(self.new_archive)
        layout.addStretch(2)
        layout.addWidget(self.welcome)
        layout.addWidget(self.new_button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(12)
        self.recent_title = fluent.Label("Recent archives", "body-strong")
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(2)
        recent_holder = QWidget()
        recent_holder.setMaximumWidth(640)
        recent_layout = QVBoxLayout(recent_holder)
        recent_layout.setContentsMargins(0, 0, 0, 0)
        recent_layout.setSpacing(8)
        recent_layout.addWidget(self.recent_title)
        recent_layout.addLayout(self.recent_box)
        centred = QHBoxLayout()
        centred.addStretch(1)
        centred.addWidget(recent_holder, 4)
        centred.addStretch(1)
        layout.addLayout(centred)
        self.formats_label = fluent.Label(self._formats_text(), "caption", tone="secondary", wrap=True)
        self.formats_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(3)
        layout.addWidget(self.formats_label)
        self._fill_recent()
        return page

    @staticmethod
    def _formats_text() -> str:
        parts = []
        for item in formats.support():
            text = f"{item.title}: {item.state}"
            if item.note:
                text += f" ({item.note})"
            parts.append(text)
        return "  ·  ".join(parts)

    def _fill_recent(self) -> None:
        while self.recent_box.count():
            widget = self.recent_box.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.recent_items = []
        for path in self.state.recent[:5]:
            item = fluent.ListItem(names.display(os.path.basename(path)), names.display(os.path.dirname(path)),
                                   "utilities-file-archiver")
            item.clicked.connect(lambda path=path: self.open_archive(path))
            self.recent_box.addWidget(item)
            self.recent_items.append(item)
        self.recent_title.setVisible(bool(self.recent_items))

    def _build_loading(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        self.loading_ring = fluent.ProgressRing(size=40)
        self.loading_label = fluent.Label("Opening…", "body", tone="secondary")
        self.loading_cancel = fluent.Button("Cancel")
        self.loading_cancel.clicked.connect(self.cancel_job)
        for widget in (self.loading_ring, self.loading_label, self.loading_cancel):
            layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
            layout.addSpacing(8)
        layout.addStretch(1)
        return page

    def _build_browser(self) -> QWidget:
        self.model = listmodel.EntryModel(self)
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setRootIsDecorated(False)
        self.view.setItemsExpandable(False)
        self.view.setUniformRowHeights(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(listmodel.NAME, Qt.SortOrder.AscendingOrder)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setAccessibleName("Contents of the archive")
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.activated.connect(self._activated)
        self.view.doubleClicked.connect(self._activated)
        header = self.view.header()
        header.setStretchLastSection(False)
        header.setSectionsMovable(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column, width in _COLUMN_WIDTHS.items():
            header.resizeSection(column, width)
        header.setMinimumSectionSize(72)
        header.setSectionResizeMode(listmodel.NAME, QHeaderView.ResizeMode.Stretch)   # the name takes what is left
        self.view.selectionModel().selectionChanged.connect(lambda *_args: self._selection_changed())
        self.empty_folder = fluent.EmptyState("This folder is empty", "", "folder")
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.view)
        self.list_stack.addWidget(self.empty_folder)

        self.preview = PreviewPane()
        self.preview.password_requested.connect(self._preview_password)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._load_preview)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.list_stack)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([920, 300])
        self.preview.setVisible(self.state.preview)
        self.preview_action.setChecked(self.state.preview)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.addWidget(self.splitter)
        return holder

    def _shortcuts(self) -> None:
        for keys, slot in (("Alt+Left", self.go_back), ("Alt+Right", self.go_forward), ("Alt+Up", self.go_up),
                           ("Ctrl+F", self.focus_search), ("Ctrl+A", self.select_all), ("Ctrl+Q", self.close)):
            self.add_shortcut(keys, slot)
        for keys, slot in (("Backspace", self.go_up), ("Delete", self.delete_selected), ("Escape", self._escape)):
            shortcut = QShortcut(QKeySequence(keys), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(slot)

    # --- file choosers (replaced in tests) ---
    def _choose_files(self, title: str, directory: str = "") -> list[str]:
        files, _filter = QFileDialog.getOpenFileNames(self, title, directory or os.path.expanduser("~"))
        return list(files)

    def _choose_folder(self, title: str, directory: str = "") -> str | None:
        return QFileDialog.getExistingDirectory(self, title, directory or os.path.expanduser("~")) or None

    def _choose_archive(self) -> str | None:
        start = os.path.dirname(self.path) if self.path else os.path.expanduser("~")
        path, _filter = QFileDialog.getOpenFileName(self, "Open archive", start, formats.OPEN_FILTER)
        return path or None

    # --- messages ---
    def tell_banner(self, severity: str, message: str, title: str = "", action: str | None = None, do=None) -> None:
        self._banner_do = do
        self.banner.set_action(action)
        self.banner.show_message(severity, message, title)

    def _banner_action(self) -> None:
        if self._banner_do is not None:
            self._banner_do()

    def _show_dialog(self, dialog: fluent.ContentDialog, on_finished) -> None:
        self.dialog = dialog

        def finished(result) -> None:
            if self.dialog is dialog:
                self.dialog = None
            on_finished(result)
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.open()

    @staticmethod
    def _error_text(error: BaseException) -> str:
        if isinstance(error, ArchiveError):
            return str(error)
        if isinstance(error, OSError):
            return f"The system reports: {error.strerror or error}."
        return f"Something unexpected went wrong ({type(error).__name__}: {error})."

    # --- state of the commands ---
    def busy(self) -> bool:
        return self.job is not None

    def selected_entries(self) -> list[model.Entry]:
        if self.archive is None:
            return []
        rows = self.view.selectionModel().selectedRows()
        return [entry for entry in (self.model.entry(index) for index in rows) if entry is not None]

    def _sync(self) -> None:
        open_ = self.archive is not None
        idle = not self.busy()
        selected = self.selected_entries() if open_ else []
        writable = open_ and self.archive.writable
        self.extract_all_action.setEnabled(open_ and idle and bool(self.archive.entries))
        self.extract_action.setEnabled(open_ and idle and bool(selected))
        for action in (self.add_files_action, self.add_folder_action):
            action.setEnabled(writable and idle and not self.model.searching)
        self.delete_action.setEnabled(writable and idle and bool(selected))
        self.test_action.setEnabled(open_ and idle)
        self.properties_action.setEnabled(open_)
        self.open_entry_action.setEnabled(open_ and idle and len(selected) == 1)
        self.reload_action.setEnabled(open_ and idle)
        self.select_all_action.setEnabled(open_)
        self.close_action.setEnabled(open_ and idle)
        self.new_action.setEnabled(idle)
        self.open_action.setEnabled(idle)
        why = ""
        if open_ and not writable:
            why = self._read_only_reason()
        for action, text in ((self.add_files_action, "Add files to this folder of the archive"),
                             (self.add_folder_action, "Add a folder to this folder of the archive"),
                             (self.delete_action, "Delete from the archive (Del)")):
            action.setToolTip(f"{text}\n{why}" if why else text)
        self.navigation_row.setVisible(open_)
        self.status_row.setVisible(open_)
        self.back_button.setEnabled(bool(self._back))
        self.forward_button.setEnabled(bool(self._forward))
        self.up_button.setEnabled(open_ and (bool(self.folder) or self.model.searching))
        self._status()

    def _read_only_reason(self) -> str:
        archive = self.archive
        if archive.warning:
            return "A damaged archive cannot be changed."
        if archive.encrypted:
            return "A password-protected archive cannot be changed here."
        return f"{archive.format.title}s can only be read."

    def _status(self) -> None:
        if self.archive is None:
            return
        shown = self.model.entries()
        selected = self.selected_entries()
        left = _plural(len(shown), "item")
        if self.model.searching:
            left = f"{_plural(len(shown), 'result')} for “{self.search.query()}”"
        if selected:
            size = sum(entry.size for entry in selected if entry.kind == model.FILE and entry.size > 0)
            left += f"  ·  {len(selected):,} selected"
            if any(entry.kind == model.FILE for entry in selected):
                left += f" ({model.human_size(size)})"
        self.status_left.setText(left)
        totals = self.archive.listing.totals
        right = f"{self.archive.format.title}  ·  {_plural(totals.files, 'file')}"
        if not totals.unknown_sizes and totals.files:
            right += f"  ·  {model.human_size(totals.size)} unpacked"
        right += f"  ·  {model.human_size(self.archive.file_size)} on disk"
        if self.archive.encrypted:
            right += "  ·  password-protected"
        self.status_right.setText(right)

    # --- opening and closing ---
    def browse_archive(self) -> None:
        if self.busy():
            return
        path = self.choose_archive()
        if path:
            self.open_archive(path)

    def open_archive(self, path: str, password: str | None = None) -> None:
        """Read the archive at ``path`` on a worker thread and show it."""
        if self.busy():
            return
        path = os.path.abspath(path)
        self._drop_archive()
        self.banner.hide()
        self.loading_label.setText(f"Opening {names.display(os.path.basename(path))}…")
        self.stack.setCurrentWidget(self.loading_page)
        self.setWindowTitle(f"{names.display(os.path.basename(path))} – {DISPLAY_NAME}")
        self._pending_path = path

        def work(handler):
            return reader.open_archive(path, password, handler.cancel,
                                       progress=lambda count: handler.progress(count))

        job = self._start_job("open", work, panel=False)
        job.handler.progressed.connect(lambda count: self.loading_label.setText(
            f"Opening {names.display(os.path.basename(path))}… {count:,} items"))
        job.finished.connect(lambda archive: self._opened_archive(path, archive, password))
        job.failed.connect(lambda error: self._open_failed(path, error, password))

    def _opened_archive(self, path: str, archive: reader.Archive, password: str | None) -> None:
        self._end_job()
        self.archive = archive
        self.path = path
        if password:
            self.password = password
        self.state.add_recent(path)
        totals = archive.listing.totals
        sample = archive.entries[:5000]
        self.view.setColumnHidden(listmodel.CRC, not any(entry.crc is not None for entry in sample))
        self.view.setColumnHidden(listmodel.MODE, not any(entry.mode is not None for entry in sample))
        self.view.setColumnHidden(listmodel.PACKED, not any(entry.packed >= 0 for entry in sample)
                                  or archive.format.family in ("tar", "stream"))
        self.stack.setCurrentWidget(self.browser)
        self._back.clear()
        self._forward.clear()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        target = self._after_reload if self._after_reload and archive.listing.has_folder(self._after_reload) else ""
        self._after_reload = None
        self._show_folder(target)
        if archive.warning:
            self.tell_banner("warning", archive.warning)
        elif not totals.files and not totals.folders:
            self.tell_banner("info", "This archive is empty.")
        self.view.setFocus()
        self._sync()

    def _open_failed(self, path: str, error: Exception, password: str | None) -> None:
        self._end_job()
        if isinstance(error, PasswordRequired):
            dialog = dialogs.PasswordDialog(self, os.path.basename(path), wrong=isinstance(error, WrongPassword))

            def answered(result) -> None:
                if result == Result.PRIMARY:
                    self.open_archive(path, dialog.password())
                else:
                    self._show_start()

            self._show_dialog(dialog, answered)
            return
        if isinstance(error, Cancelled):
            self._show_start()
            return
        self.state.forget(path)
        self._fill_recent()
        text = self._error_text(error)
        if isinstance(error, UnsupportedFormat) and error.needs:
            text += f" Install the package “{error.needs}” to open it."
        self.error_page.set_title(f"“{names.display(os.path.basename(path))}” cannot be opened")
        self.error_page.set_text(text)
        self.stack.setCurrentWidget(self.error_page)
        self.setWindowTitle(DISPLAY_NAME)
        self._sync()

    def _drop_archive(self) -> None:
        self._cancel_preview()
        if self.archive is not None:
            self.archive.close()
        self.archive = None
        self.path = None
        self.password = None if self._after_reload is None else self.password
        self.folder = ""
        self.model.set_entries([])

    def _show_start(self) -> None:
        self._fill_recent()
        self.stack.setCurrentWidget(self.start_page)
        self.setWindowTitle(DISPLAY_NAME)
        self._sync()

    def close_archive(self) -> None:
        if self.busy():
            return
        if self.archive is None:
            self.close()
            return
        self._drop_archive()
        self.banner.hide()
        self._show_start()

    def reload(self) -> None:
        if self.archive is None or self.busy():
            return
        self._after_reload = self.folder
        self.open_archive(self.path, self.password)

    # --- jobs ---
    def _start_job(self, kind: str, function, *, title: str = "", panel: bool = True) -> Job:
        job = Job(self.worker, function, self)
        self.job = job
        self._job_kind = kind
        job.handler.asked.connect(self._asked)
        if panel:
            self.progress.start(title)
            self.progress_holder.show()
            job.handler.progressed.connect(self.progress.show_progress)
        self._sync()
        job.start()
        return job

    def _end_job(self) -> None:
        job, self.job = self.job, None
        self._job_kind = ""
        self.progress_holder.hide()
        if self.dialog is not None and isinstance(self.dialog, (dialogs.OverwriteDialog, dialogs.PasswordDialog)):
            self.dialog.reject()
        if job is not None:
            job.deleteLater()
        self._sync()

    def cancel_job(self) -> None:
        if self.job is not None:
            self.job.cancel()

    def _asked(self, kind: str, payload: object) -> None:
        job = self.job
        if job is None:
            return
        if kind == OVERWRITE:
            dialog = dialogs.OverwriteDialog(self, payload)
            self._show_dialog(dialog, lambda _result: job.handler.answer(dialog.decision))
        elif kind == SIZE:
            dialog = fluent.ContentDialog(self, payload.title(), payload.text(), primary="Extract anyway",
                                          close="Cancel", default="close")
            self._show_dialog(dialog, lambda result: job.handler.answer(result == Result.PRIMARY))
        elif kind == PASSWORD:
            dialog = dialogs.PasswordDialog(self, os.path.basename(self.path or ""), wrong=bool(payload))
            self._show_dialog(dialog, lambda result: job.handler.answer(
                dialog.password() if result == Result.PRIMARY else None))

    def _job_failed(self, error: Exception, what: str) -> None:
        self._end_job()
        if isinstance(error, Cancelled):
            self.tell_banner("info", f"{what} was cancelled.")
        else:
            self.tell_banner("error", self._error_text(error), f"{what} failed")

    # --- navigation ---
    def _show_folder(self, folder: str) -> None:
        self.folder = folder
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        entries = self.archive.listing.folder(folder)
        self.model.set_entries(entries)
        self.view.setColumnHidden(listmodel.LOCATION, True)
        self.list_stack.setCurrentWidget(self.view if entries else self.empty_folder)
        self.empty_folder.set_title("This folder is empty" if self.archive.entries else "This archive is empty")
        self.breadcrumb.set_items([names.display(os.path.basename(self.path))]
                                  + [names.display(part) for part in names.split(folder)])
        self.view.scrollToTop()
        self.view.clearSelection()
        if entries:
            self.view.setCurrentIndex(self.model.index(0, 0))
            self.view.clearSelection()
        self._selection_changed()

    def go_to(self, folder: str) -> None:
        if self.archive is None or not self.archive.listing.has_folder(folder):
            return
        if folder != self.folder or self.model.searching:
            self._back.append(self.folder)
            self._forward.clear()
        self._show_folder(folder)

    def go_back(self) -> None:
        if self.archive is not None and self._back:
            self._forward.append(self.folder)
            self._show_folder(self._back.pop())

    def go_forward(self) -> None:
        if self.archive is not None and self._forward:
            self._back.append(self.folder)
            self._show_folder(self._forward.pop())

    def go_up(self) -> None:
        if self.archive is None:
            return
        if self.model.searching:
            self._show_folder(self.folder)
        elif self.folder:
            child = self.folder
            self.go_to(self.folder.rpartition("/")[0])
            for row, entry in enumerate(self.model.entries()):
                if entry.path == child:
                    self.view.setCurrentIndex(self.model.index(row, 0))
                    break

    def _crumb_clicked(self, index: int) -> None:
        self.go_to("/".join(names.split(self.folder)[:index]))

    def focus_search(self) -> None:
        if self.archive is not None:
            self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.search.selectAll()

    def _apply_search(self) -> None:
        if self.archive is None:
            return
        query = self.search.query()
        if not query:
            if self.model.searching:
                self._show_folder(self.folder)
            return
        found = self.archive.listing.search(query)
        self.model.set_entries(found, searching=True)
        self.view.setColumnHidden(listmodel.LOCATION, False)
        self.empty_folder.set_title("Nothing found")
        self.list_stack.setCurrentWidget(self.view if found else self.empty_folder)
        self._selection_changed()

    def _escape(self) -> None:
        if self.model.searching:
            self._show_folder(self.folder)
        else:
            self.view.clearSelection()

    def select_all(self) -> None:
        if self.archive is not None:
            self.view.selectAll()
            self.view.setFocus()

    def _activated(self, index: QModelIndex) -> None:
        entry = self.model.entry(index)
        if entry is None:
            return
        if entry.kind == model.DIR:
            self.go_to(entry.path)
        else:
            self.open_entry(entry)

    def _selection_changed(self) -> None:
        self._sync()
        if self.preview.isVisible():
            self._preview_timer.start()

    def _context_menu(self, position: QPoint) -> None:
        if self.archive is None:
            return
        menu = self.build_context_menu()
        menu.popup(self.view.viewport().mapToGlobal(position))

    def build_context_menu(self) -> fluent.MenuFlyout:
        selected = self.selected_entries()
        idle = not self.busy()
        menu = fluent.MenuFlyout(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.add_item("Open", self.open_selected, icon="document-open", enabled=idle and len(selected) == 1)
        menu.add_item("Extract…", self.extract_selected, icon="archive-extract", shortcut="Ctrl+Shift+E",
                      enabled=idle and bool(selected))
        menu.add_item("Delete", self.delete_selected, icon="edit-delete", shortcut="Del",
                      enabled=self.delete_action.isEnabled())
        menu.addSeparator()
        menu.add_item("Select all", self.select_all, shortcut="Ctrl+A")
        menu.add_item("Properties", self.show_properties, icon="document-properties", shortcut="Alt+Return")
        return menu

    # --- preview ---
    def _toggle_preview(self) -> None:
        visible = self.preview_action.isChecked()
        self.preview.setVisible(visible)
        self.state.preview = visible
        self.state.save()
        if visible:
            self._preview_timer.start()
        else:
            self._cancel_preview()

    def _cancel_preview(self) -> None:
        self._preview_timer.stop()
        if self._preview_job is not None:
            self._preview_job.abandon()
            self._preview_job.deleteLater()
            self._preview_job = None

    def _load_preview(self) -> None:
        self._cancel_preview()
        if self.archive is None or not self.preview.isVisible():
            return
        selected = self.selected_entries()
        if len(selected) != 1:
            self._preview_entry = None
            self.preview.show_nothing("Select a file to preview it." if not selected
                                      else f"{len(selected):,} items selected.")
            return
        entry = selected[0]
        self._preview_entry = entry
        self.preview.show_entry(entry)
        if entry.kind == model.DIR:
            inside = len(self.archive.listing.folder(entry.path))
            self.preview.show_message(f"Folder with {_plural(inside, 'item')}.")
            return
        if entry.kind != model.FILE:
            target = f" to “{names.display(entry.link)}”" if entry.link else ""
            self.preview.show_message(f"{model.type_text(entry)}{target}.")
            return
        if entry.encrypted and not self.password:
            self.preview.show_message("This file is encrypted.", unlock=True)
            return
        image = previewing.is_image_name(entry.path)
        if image and entry.size > previewing.IMAGE_LIMIT:
            self.preview.show_message("This picture is too large to preview.")
            return
        limit = previewing.IMAGE_LIMIT if image else previewing.TEXT_LIMIT
        archive, password = self.archive, self.password
        self.preview.show_message("Loading…")

        def work(handler):
            with archive.open_entry(entry, password) as source:
                data = bytearray()
                while len(data) <= limit:
                    if handler.cancel.is_set():
                        raise Cancelled()
                    piece = source.read(min(1 << 16, limit + 1 - len(data)))
                    if not piece:
                        break
                    data += piece
            data = bytes(data)
            if image:
                return "image", decode_image(data[:limit]), False
            return "text", previewing.as_text(data[:limit]), len(data) > limit

        job = Job(self.preview_worker, work, self)
        self._preview_job = job
        job.finished.connect(lambda result: self._preview_ready(entry, result))
        job.failed.connect(lambda error: self._preview_failed(entry, error))
        job.start()

    def _preview_ready(self, entry, result) -> None:
        if entry is not self._preview_entry:
            return
        kind, content, truncated = result
        if content is None:
            self.preview.show_message("No preview for this kind of file." if kind == "text"
                                      else "This picture cannot be shown.")
        elif kind == "image":
            self.preview.show_image(content)
        else:
            self.preview.show_text(content, truncated) if content else self.preview.show_message("The file is empty.")

    def _preview_failed(self, entry, error: Exception) -> None:
        if entry is not self._preview_entry or isinstance(error, Cancelled):
            return
        if isinstance(error, PasswordRequired):
            self.password = None
            self.preview.show_message("That password is not correct." if isinstance(error, WrongPassword)
                                      else "This file is encrypted.", unlock=True)
        else:
            self.preview.show_message(self._error_text(error))

    def _preview_password(self) -> None:
        dialog = dialogs.PasswordDialog(self, os.path.basename(self.path or ""))

        def answered(result) -> None:
            if result == Result.PRIMARY:
                self.password = dialog.password()
                self._load_preview()

        self._show_dialog(dialog, answered)

    # --- extracting ---
    def extract_all(self) -> None:
        if self.archive is not None and not self.busy():
            self._ask_extract(None)

    def extract_selected(self) -> None:
        selected = self.selected_entries()
        if self.archive is None or self.busy() or not selected:
            return
        chosen: list[model.Entry] = []
        seen: set[int] = set()
        for entry in selected:
            for found in self.archive.listing.descendants(entry):
                if id(found) not in seen:
                    seen.add(id(found))
                    chosen.append(found)
        self._ask_extract(chosen, selected)

    def _ask_extract(self, entries: list[model.Entry] | None, selected: list[model.Entry] | None = None) -> None:
        directory = os.path.dirname(self.path)
        folder_name = formats.strip_extension(os.path.basename(self.path))
        top = self.archive.listing.folder("")
        if entries is None:
            title = f"Extract “{names.display(os.path.basename(self.path))}”"
            make_folder = len(top) != 1
        else:
            title = f"Extract {_plural(len(selected), 'selected item')}"
            make_folder = False
        dialog = dialogs.ExtractDialog(self, title=title, destination=directory, folder_name=folder_name,
                                       make_folder=make_folder, policy=self.state.policy,
                                       open_after=self.state.open_after,
                                       choose_folder=lambda *args: self.choose_folder(*args))

        def answered(result) -> None:
            if result != Result.PRIMARY:
                return
            self.state.policy = dialog.chosen_policy()
            self.state.open_after = dialog.open_after.isChecked()
            self.state.save()
            base = "" if entries is None or self.model.searching else self.folder
            self.start_extract(dialog.target(), entries, base, dialog.chosen_policy(), dialog.open_after.isChecked())

        self._show_dialog(dialog, answered)

    def start_extract(self, target: str, entries: list[model.Entry] | None, base: str, policy: str,
                      open_after: bool) -> None:
        if not target or not os.path.isabs(target):
            # Never a folder nobody named: a relative path would mean "wherever the program was started".
            self.tell_banner("error", "Choose the folder to extract to by its whole path.", "Nothing was extracted")
            return
        archive, password = self.archive, self.password

        def work(handler):
            return extract.extract(archive, target, entries=entries, base=base, policy=policy, password=password,
                                   handler=handler, cancel=handler.cancel)

        job = self._start_job("extract", work, title=f"Extracting to {names.display(target)}")
        job.finished.connect(lambda result: self._extracted(result, open_after))
        job.failed.connect(lambda error: self._job_failed(error, "Extracting"))

    def _extracted(self, result: extract.Result, open_after: bool) -> None:
        self._end_job()
        self._last_result = result
        if result.password:
            self.password = result.password
        summary = result.summary()
        if result.cancelled and not result.files and not result.skipped and not result.failed:
            self.tell_banner("info", "Nothing was extracted.", "Cancelled")
            return
        if result.clean:
            self.tell_banner("success", f"{summary} They are in {names.display(result.destination)}.", "",
                             "Open folder", lambda: self.open_path(result.destination))
            if open_after:
                self.open_path(result.destination)
            return
        self.tell_banner("warning", summary, "Not everything was extracted" if not result.cancelled else "Cancelled",
                         "Details", self.show_last_issues)

    def show_last_issues(self) -> None:
        result = self._last_result
        if result is None:
            return
        self._show_dialog(dialogs.IssuesDialog(self, "What was left out", result.summary(),
                                               [("Skipped", result.skipped), ("Failed", result.failed)],
                                               list(result.notes)), lambda _result: None)

    # --- opening an entry ---
    def open_selected(self) -> None:
        selected = self.selected_entries()
        if len(selected) == 1:
            self._activated(self.model.index(self.model.row_of(selected[0]), 0))

    def _temporary_folder(self) -> str:
        if self._temporary is None:
            try:
                base = str(fluent.cache_dir(APP_ID))
            except OSError:
                base = None
            self._temporary = tempfile.mkdtemp(prefix="opened-", dir=base)
        self._opened += 1
        folder = os.path.join(self._temporary, str(self._opened))
        os.mkdir(folder, 0o700)
        return folder

    def open_entry(self, entry: model.Entry) -> None:
        """Extract one file into a private temporary folder and hand it to its default application."""
        if self.archive is None or self.busy():
            return
        if entry.kind != model.FILE:
            self.tell_banner("info", f"“{names.display(entry.base)}” is a {model.type_text(entry).lower()}; "
                             "extract it to use it.")
            return
        if previewing.is_program(entry.path) or (entry.mode is not None and entry.mode & 0o111
                                                 and not model.extension(entry.path)):
            self._refuse_program(entry)
            return
        archive, password = self.archive, self.password
        try:
            folder = self._temporary_folder()
        except OSError as error:
            self.tell_banner("error", self._error_text(error), "The file could not be opened")
            return

        def work(handler):
            result = extract.extract(archive, folder, entries=[entry], base=entry.parent, policy=extract.REPLACE,
                                     password=password, handler=handler, cancel=handler.cancel)
            path = os.path.join(folder, result.top_level[0]) if result.files == 1 and result.top_level else None
            head = b""
            if path is not None:
                os.chmod(path, 0o400)               # a copy to look at: changes would not reach the archive
                with open(path, "rb") as handle:
                    head = handle.read(8)
            return result, path, head

        job = self._start_job("open-entry", work, title=f"Opening {names.display(entry.base)}")
        job.finished.connect(lambda outcome: self._entry_ready(entry, *outcome))
        job.failed.connect(lambda error: self._job_failed(error, "Opening the file"))

    def _refuse_program(self, entry: model.Entry) -> None:
        self.tell_banner("warning", f"“{names.display(entry.base)}” is a program, a script or an installer. "
                         "Archive Manager does not start those from inside an archive: extract it first, then "
                         "decide whether to run it.", "Not opened")

    def _entry_ready(self, entry: model.Entry, result: extract.Result, path: str | None, head: bytes) -> None:
        self._end_job()
        if result.password:
            self.password = result.password
        if path is None:
            if not result.cancelled:
                reason = (result.failed or result.skipped or [reader.Issue("", "it could not be extracted")])[0].reason
                self.tell_banner("error", f"“{names.display(entry.base)}”: {reason}.", "The file could not be opened")
            return
        if previewing.is_program(entry.path, head):
            self._refuse_program(entry)
            return
        if not self.open_path(path):
            self.tell_banner("warning", "No application could be started for this file.", "Not opened")

    # --- changing the archive ---
    def add_files(self) -> None:
        if self.archive is None or self.busy() or not self.archive.writable:
            return
        self.add_paths(self.choose_files("Add files to the archive", os.path.dirname(self.path)))

    def add_folder(self) -> None:
        if self.archive is None or self.busy() or not self.archive.writable:
            return
        chosen = self.choose_folder("Add a folder to the archive", os.path.dirname(self.path))
        if chosen:
            self.add_paths([chosen])

    def add_paths(self, paths: list[str]) -> None:
        """Add files and folders to the folder that is shown, after asking about what they would replace."""
        if not paths or self.archive is None or self.busy():
            return
        if not self.archive.writable:
            self.tell_banner("warning", self._read_only_reason(), "Nothing was added")
            return
        archive, folder = self.archive, self.folder
        paths = [os.path.abspath(path) for path in paths]

        def check(handler):
            return create.replaced_by(archive, paths, folder)

        job = self._start_job("check", check, title="Looking at what is being added")
        job.finished.connect(lambda replaced: self._confirm_add(paths, replaced))
        job.failed.connect(lambda error: self._job_failed(error, "Adding"))

    def _confirm_add(self, paths: list[str], replaced: list[str]) -> None:
        self._end_job()
        if not replaced:
            self._run_add(paths)
            return
        shown = ", ".join(f"“{names.display(name)}”" for name in replaced[:3])
        more = f" and {len(replaced) - 3:,} more" if len(replaced) > 3 else ""
        dialog = fluent.ContentDialog(self, "Replace what is in the archive?",
                                      f"{shown}{more} already exist{'s' if len(replaced) == 1 else ''} in the "
                                      "archive and will be replaced.", primary="Replace", close="Cancel",
                                      default="close")
        self._show_dialog(dialog, lambda result: self._run_add(paths) if result == Result.PRIMARY else None)

    def _run_add(self, paths: list[str]) -> None:
        archive, folder = self.archive, self.folder

        def work(handler):
            return create.add(archive, paths, folder, handler=handler, cancel=handler.cancel)

        job = self._start_job("add", work, title="Adding to the archive")
        job.finished.connect(lambda result: self._changed(result, "added"))
        job.failed.connect(lambda error: self._job_failed(error, "Adding"))

    def delete_selected(self) -> None:
        selected = self.selected_entries()
        if self.archive is None or self.busy() or not selected or not self.archive.writable:
            return
        doomed: list[model.Entry] = []
        seen: set[int] = set()
        for entry in selected:
            for found in self.archive.listing.descendants(entry):
                if id(found) not in seen:
                    seen.add(id(found))
                    doomed.append(found)
        what = f"“{names.display(selected[0].base)}”" if len(selected) == 1 else _plural(len(selected), "item")
        inside = len(doomed) - sum(1 for entry in selected if not entry.synthetic)
        extra = f" and the {_plural(inside, 'item')} inside" if inside > 0 else ""
        dialog = fluent.ContentDialog(self, "Delete from the archive?",
                                      f"{what}{extra} will be removed from the archive for good. "
                                      "The archive is written anew; this cannot be undone.",
                                      primary="Delete", close="Cancel", default="close")

        def answered(result) -> None:
            if result != Result.PRIMARY:
                return
            archive = self.archive

            def work(handler):
                return create.delete(archive, doomed, handler=handler, cancel=handler.cancel)

            job = self._start_job("delete", work, title="Deleting from the archive")
            job.finished.connect(lambda outcome: self._changed(outcome, "deleted"))
            job.failed.connect(lambda error: self._job_failed(error, "Deleting"))

        self._show_dialog(dialog, answered)

    def _changed(self, result: create.Result, verb: str) -> None:
        self._end_job()
        if result.cancelled:
            self.tell_banner("info", "The archive was not changed.", "Cancelled")
            return
        self._last_result = None
        folder = self.folder
        problems = len(result.skipped) + len(result.failed)
        if verb == "added":
            count = result.files + result.folders + result.links
            message = f"{_plural(count, 'item')} added."
        else:
            message = f"{_plural(result.removed, 'item')} deleted."
        self._after_reload = folder
        self.open_archive(self.path, self.password)
        if problems:
            self._change_issues = result
            self.tell_banner("warning", f"{message} {_plural(problems, 'item')} could not be packed.", "",
                             "Details", self._show_change_issues)
        else:
            self.tell_banner("success", message)

    def _show_change_issues(self) -> None:
        result = self._change_issues
        self._show_dialog(dialogs.IssuesDialog(self, "What was left out", "", [("Skipped", result.skipped),
                                                                             ("Failed", result.failed)],
                                               list(result.notes)), lambda _result: None)

    def new_archive(self, sources: list[str] | None = None) -> None:
        if self.busy():
            return
        directory = os.path.dirname(self.path) if self.path else os.path.expanduser("~")
        sources = list(sources or [])
        name = "Archive"
        if len(sources) == 1:
            name = os.path.basename(sources[0].rstrip("/")) or name
            directory = os.path.dirname(os.path.abspath(sources[0]))
        dialog = dialogs.NewArchiveDialog(self, sources=sources, directory=directory, name=name,
                                          choose_files=lambda *args: self.choose_files(*args),
                                          choose_folder=lambda *args: self.choose_folder(*args))

        def answered(result) -> None:
            if result != Result.PRIMARY:
                return
            output = dialog.output()
            if not output:
                return
            options = create.Options(format=dialog.format.currentData(), level=dialog.level.currentData(),
                                     follow_symlinks=dialog.follow.isChecked(),
                                     exclude_hidden=dialog.hidden.isChecked())
            paths = dialog.source_paths()
            if os.path.lexists(output):
                confirm = fluent.ContentDialog(self, "Replace the existing file?",
                                               f"“{names.display(os.path.basename(output))}” already exists in "
                                               "that folder.", primary="Replace", close="Cancel", default="close")
                self._show_dialog(confirm, lambda answer: self.start_create(output, paths, options)
                                  if answer == Result.PRIMARY else None)
            else:
                self.start_create(output, paths, options)

        self._show_dialog(dialog, answered)

    def start_create(self, output: str, paths: list[str], options: create.Options) -> None:
        if not output or not os.path.isabs(output):
            self.tell_banner("error", "Choose the folder for the archive by its whole path.", "No archive was created")
            return

        def work(handler):
            return create.create(output, paths, options, handler, handler.cancel)

        job = self._start_job("create", work, title=f"Creating {names.display(os.path.basename(output))}")
        job.finished.connect(lambda result: self._created(result))
        job.failed.connect(lambda error: self._job_failed(error, "Creating the archive"))

    def _created(self, result: create.Result) -> None:
        self._end_job()
        if result.cancelled:
            self.tell_banner("info", "No archive was created.", "Cancelled")
            return
        self.open_archive(result.path)
        problems = len(result.skipped) + len(result.failed)
        count = result.files + result.folders + result.links
        message = f"Packed {_plural(count, 'item')} into {model.human_size(result.size)}."
        if problems:
            self._change_issues = result
            self.tell_banner("warning", f"{message} {_plural(problems, 'item')} could not be packed.", "",
                             "Details", self._show_change_issues)
        else:
            self.tell_banner("success", message)

    # --- test and properties ---
    def test_archive(self) -> None:
        if self.archive is None or self.busy():
            return
        archive, password = self.archive, self.password
        totals = archive.listing.totals
        total = -1 if totals.unknown_sizes else totals.size

        def work(handler):
            opened, found = extract.unlock(archive, password, handler)
            if not opened:
                raise Cancelled()
            issues = archive.test(found, lambda done, entry: handler.progress(
                extract.Progress(names.display(entry.path), done, total, 0, 0)), handler.cancel)
            return issues, found

        job = self._start_job("test", work, title="Testing the archive")
        job.finished.connect(lambda outcome: self._tested(*outcome))
        job.failed.connect(lambda error: self._job_failed(error, "Testing"))

    def _tested(self, issues: list, password: str | None) -> None:
        self._end_job()
        if password:
            self.password = password
        totals = self.archive.listing.totals
        if not issues:
            self.tell_banner("success", f"No damage found in {_plural(totals.files, 'file')}.", "The archive is fine")
            return
        self._test_issues = issues
        self.tell_banner("error", f"{_plural(len(issues), 'problem')} found.", "The archive is damaged", "Details",
                         lambda: self._show_dialog(dialogs.IssuesDialog(
                             self, "Damage found", f"{_plural(len(self._test_issues), 'problem')} found while "
                             "reading the archive.", [("Damaged", self._test_issues)]), lambda _result: None))

    def show_properties(self) -> None:
        if self.archive is None:
            return
        selected = self.selected_entries()
        if len(selected) == 1 and self.view.hasFocus():
            entry = selected[0]
            dialog = dialogs.PropertiesDialog(self, names.display(entry.base), dialogs.entry_rows(entry))
        else:
            dialog = dialogs.PropertiesDialog(self, "Archive properties", dialogs.archive_rows(self.archive, self.path))
        self._show_dialog(dialog, lambda _result: None)

    # --- drag and drop ---
    @staticmethod
    def _dropped_paths(event) -> list[str]:
        data = event.mimeData()
        if not data.hasUrls():
            return []
        return [url.toLocalFile() for url in data.urls()[:500] if url.isLocalFile() and url.toLocalFile()]

    def dragEnterEvent(self, event) -> None:
        if self._dropped_paths(event) and not self.busy():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = self._dropped_paths(event)
        if not paths or self.busy():
            return
        event.acceptProposedAction()
        self.handle_drop(paths)

    def handle_drop(self, paths: list[str]) -> None:
        """Dropped files: open an archive when none is open, else add them to the one that is."""
        if self.archive is None:
            self.open_archive(paths[0])
        else:
            self.add_paths(paths)

    # --- closing ---
    def handle_arguments(self, arguments: list[str]) -> None:
        paths = [argument for argument in arguments if argument and not argument.startswith("-")]
        if paths and not self.busy():
            self.open_archive(paths[0])

    def closeEvent(self, event) -> None:
        if self.job is not None and self._job_kind in ("extract", "create", "add", "delete"):
            if not self.ask("Stop and close?", "An operation is still running. It will be stopped.", "Stop and close",
                            "Keep working"):
                event.ignore()
                return
        if self.job is not None:
            self.job.abandon()
            self.job = None
        self._cancel_preview()
        self.worker.shutdown()
        self.preview_worker.shutdown()
        if self.archive is not None:
            self.archive.close()
            self.archive = None
        if self._temporary is not None:
            # Our own folder, filled by our own extractor; the copies in it are read-only on purpose.
            for directory, _folders, files in os.walk(self._temporary):
                for name in files:
                    try:
                        os.chmod(os.path.join(directory, name), 0o600)
                    except OSError:
                        pass
            shutil.rmtree(self._temporary, ignore_errors=True)
            self._temporary = None
        self.state.save()
        super().closeEvent(event)
