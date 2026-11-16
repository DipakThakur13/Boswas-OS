"""The rows of a folder view: a table model filled by a loader thread.

A folder is read, typed and sorted on a thread (``Loader``); the model
receives whole sorted lists and swaps them in, so the GUI thread never
reads a directory and never sorts a large one. A folder with a hundred
thousand entries shows its first rows after a moment and stays scrollable
while the rest arrives. The model holds plain ``Entry`` objects; views ask
for the visible rows only.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QDateTime, QLocale, QMimeData, QModelIndex, Qt, QThread, Signal
from PySide6.QtGui import QStandardItemModel

from . import clipboard, listing, mime, search
from .fileops import describe_error
from .listing import Entry
from .text import display_name, format_size, split_extension

ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
FOLDER, TRASH, SEARCH = "folder", "trash", "search"
COLUMNS = {
    FOLDER: ("name", "date", "type", "size"),
    TRASH: ("name", "location", "deleted", "type", "size"),
    SEARCH: ("name", "location", "date", "type", "size"),
}
HEADERS = {"name": "Name", "date": "Date modified", "type": "Type", "size": "Size", "location": "Folder",
           "deleted": "Date deleted"}
_date_format: str | None = None


def format_time(timestamp: float) -> str:
    """A time as the locale writes short dates, always with a four-digit year: 15/11/2026 19:27."""
    global _date_format
    if not timestamp:
        return ""
    locale = QLocale()
    if _date_format is None:
        if locale.language() == QLocale.Language.C:
            _date_format = "yyyy-MM-dd HH:mm"       # no regional format chosen: the unambiguous one
        else:
            date = locale.dateFormat(QLocale.FormatType.ShortFormat)
            if "yyyy" not in date:
                date = date.replace("yy", "yyyy")
            _date_format = f"{date} {locale.timeFormat(QLocale.FormatType.ShortFormat)}"
    try:
        return locale.toString(QDateTime.fromSecsSinceEpoch(int(timestamp)), _date_format)
    except (OverflowError, ValueError):
        return ""


class Loader(QThread):
    """Reads one location and emits it as sorted lists: partial ones while it reads, then the complete one."""

    snapshot = Signal(int, object, bool, str)       # generation, (rows, rows by name), finished, error text

    def __init__(self, generation: int, mode: str, location: str, query: str, sort: tuple[str, bool], trash,
                 hidden: bool, parent=None) -> None:
        super().__init__(parent)
        self.generation, self.mode, self.location, self.query = generation, mode, location, query
        self.sort = sort
        self.trash = trash
        self.hidden = hidden
        self._cancelled = False
        self.duration = 0.0

    def cancel(self) -> None:
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled

    def _batches(self):
        if self.mode == FOLDER:
            yield from listing.scan(self.location, self.cancelled, 2000)
        elif self.mode == SEARCH:
            yield from search.search(self.location, self.query, self.cancelled, hidden=self.hidden)
        else:
            batch = []
            for item in self.trash.items():
                try:
                    entry = listing.entry_for(item.files_path, os.path.basename(item.original_path))
                except OSError:
                    continue
                entry.hidden = False
                entry.location = os.path.dirname(item.original_path)
                entry.deleted = item.deleted
                entry.extra = item
                batch.append(entry)
            yield batch

    def _emit(self, by_name: list[Entry], finished: bool, error: str = "") -> None:
        column, descending = self.sort
        rows = listing.sort_entries(list(by_name), column, descending, presorted=True)
        self.snapshot.emit(self.generation, (rows, list(by_name) if finished else None), finished, error)

    def run(self) -> None:
        started = time.monotonic()
        by_name: list[Entry] = []           # kept in name order: a new batch is sorted and merged in
        error = ""
        emitted = started
        interval = 0.15
        try:
            for batch in self._batches():
                if self._cancelled:
                    return
                for entry in batch:
                    mime.fill(entry)
                    entry.name_key()
                batch.sort(key=Entry.name_key)
                by_name.extend(batch)
                by_name.sort(key=Entry.name_key)        # two sorted runs: a merge
                now = time.monotonic()
                if now - emitted >= interval and (batch or self.mode != SEARCH):
                    self._emit(by_name, False)
                    emitted = time.monotonic()
                    interval = min(1.5, interval * 1.5) if self.mode != SEARCH else 0.2
        except OSError as failure:
            error = describe_error(failure)
            if isinstance(failure, FileNotFoundError):
                error = "This folder does not exist."
            elif isinstance(failure, PermissionError):
                error = "You do not have permission to open this folder."
            elif isinstance(failure, NotADirectoryError):
                error = "This is not a folder."
        if self._cancelled:
            return
        self.duration = time.monotonic() - started
        self._emit(by_name, True, error)


class FolderModel(QStandardItemModel):
    """The entries of one location (a folder, the Trash, search results).

    The base class is QStandardItemModel for one reason: its row count,
    column count, indexes and flags are answered in C++. The views ask
    those for every row whenever the rows change; answering from Python a
    hundred thousand times takes seconds. No QStandardItem is ever made:
    the rows are the ``Entry`` objects of ``rows`` and ``data`` reads them.
    """

    columns_changed = Signal()

    loading_changed = Signal(bool)
    load_finished = Signal(str)             # the error text, empty when the location was read
    rows_changed = Signal()
    rename_requested = Signal(object, str)  # entry, new name

    def __init__(self, services, parent=None) -> None:
        super().__init__(0, len(COLUMNS[FOLDER]), parent)
        self.services = services
        self.mode = FOLDER
        self.location = ""
        self.query = ""
        self.error = ""
        self.is_loading = False
        self.last_duration = 0.0
        settings = services.settings
        self.sort_column = str(settings.get("sort_column"))
        self.sort_descending = bool(settings.get("sort_descending"))
        self.show_hidden = bool(settings.get("show_hidden"))
        self.show_extensions = bool(settings.get("show_extensions"))
        self._all: list[Entry] = []
        self._by_name: list[Entry] = []         # the same entries in name order, for re-sorting
        self._rows: list[Entry] = []
        self._row_of: dict[str, int] | None = None
        self._generation = 0
        self._loader: Loader | None = None
        self._retired: list[Loader] = []
        self._keep_rows = False
        services.thumbnailer.ready.connect(self._thumbnail_ready)
        services.cut_changed.connect(self._repaint)

    # --- loading ---
    def load(self, mode: str, location: str, query: str = "") -> None:
        """Show another location. The rows are cleared at once and arrive as they are read."""
        self._start(mode, location, query, keep=False)

    def refresh(self) -> None:
        """Read the same location again; the rows shown stay until the new ones are complete."""
        if self.location:
            self._start(self.mode, self.location, self.query, keep=True)

    def _start(self, mode: str, location: str, query: str, keep: bool) -> None:
        self.cancel()
        self._generation += 1
        columns_change = COLUMNS[mode] != COLUMNS[self.mode]
        if not keep or columns_change:
            self._all, self._by_name, self._rows, self._row_of = [], [], [], None
            self.setRowCount(0)
            self.mode = mode
            if columns_change:
                self.setColumnCount(len(COLUMNS[mode]))
                self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(COLUMNS[mode]) - 1)
                self.columns_changed.emit()
            self.rows_changed.emit()
        self.mode, self.location, self.query = mode, location, query
        self.error = ""
        self._keep_rows = keep
        sort = (self.sort_column if self.sort_column in COLUMNS[mode] else "name", self.sort_descending)
        loader = Loader(self._generation, mode, location, query, sort, self.services.trash, self.show_hidden)
        loader.snapshot.connect(self._snapshot)
        loader.finished.connect(lambda done=loader: self._retire(done))
        self._loader = loader
        self._set_loading(True)
        loader.start()

    def cancel(self) -> None:
        """Stop reading (the rows read so far stay)."""
        if self._loader is not None:
            self._loader.cancel()
            self._retired.append(self._loader)
            self._loader = None
        self._set_loading(False)

    def _retire(self, loader: Loader) -> None:
        if loader in self._retired:
            self._retired.remove(loader)

    def shutdown(self) -> None:
        self.cancel()
        for loader in list(self._retired):
            loader.wait(2000)
        self._retired.clear()

    def _set_loading(self, loading: bool) -> None:
        if loading != self.is_loading:
            self.is_loading = loading
            self.loading_changed.emit(loading)

    def _snapshot(self, generation: int, lists: tuple, finished: bool, error: str) -> None:
        if generation != self._generation:
            return
        entries, by_name = lists
        if finished or not self._keep_rows:
            self._all = entries
            if by_name is not None:
                self._by_name = by_name
            self._apply()
        if finished:
            loader = self._loader
            if loader is not None:
                self.last_duration = loader.duration
                self._retired.append(loader)
                self._loader = None
            self.error = error
            self._set_loading(False)
            self.load_finished.emit(error)

    def wait(self, timeout: float = 10.0) -> bool:
        """Process events until the location is read (tests)."""
        from PySide6.QtCore import QCoreApplication
        deadline = time.monotonic() + timeout
        while self.is_loading and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.001)
        return not self.is_loading

    # --- rows ---
    def _visible(self, entries: list[Entry]) -> list[Entry]:
        if self.mode != FOLDER or self.show_hidden:
            return entries
        return [entry for entry in entries if not entry.hidden]

    def _apply(self) -> None:
        self._set_rows(self._visible(self._all))

    def _set_rows(self, rows: list[Entry]) -> None:
        """Show ``rows``. Selected and current rows follow their entries (by path) to the new positions."""
        if len(rows) > len(self._rows):
            self.setRowCount(len(rows))             # the new rows are empty until the lists are swapped
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        if old:
            by_path = {entry.path: row for row, entry in enumerate(rows)}
            current = self._rows
            new = []
            for index in old:
                row = by_path.get(current[index.row()].path, -1) if 0 <= index.row() < len(current) else -1
                new.append(self.index(row, index.column()) if row >= 0 else QModelIndex())
            self._rows = rows
            self.changePersistentIndexList(old, new)
        else:
            self._rows = rows
        self._row_of = None
        self.layoutChanged.emit()
        if self.rowCount() > len(rows):
            self.setRowCount(len(rows))
        self.rows_changed.emit()

    def set_sort(self, column: str, descending: bool) -> None:
        if column not in listing.SORT_COLUMNS:
            return
        self.sort_column, self.sort_descending = column, descending
        if self._loader is not None:
            self._loader.sort = (column, descending)
            return
        # The entries are kept in name order, so another column or direction is a quick, stable pass.
        self._all = listing.sort_entries(list(self._by_name), column, descending, presorted=True)
        self._apply()

    def set_show_hidden(self, show: bool) -> None:
        if show != self.show_hidden:
            self.show_hidden = show
            if self.mode == SEARCH:
                self.refresh()
            else:
                self._apply()

    def set_show_extensions(self, show: bool) -> None:
        if show != self.show_extensions:
            self.show_extensions = show
            self._repaint()

    def _repaint(self) -> None:
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, 0))

    # --- access ---
    @property
    def rows(self) -> list[Entry]:
        return self._rows

    def total_count(self) -> int:
        """Every entry read, hidden ones included."""
        return len(self._all)

    def columns(self) -> tuple[str, ...]:
        return COLUMNS[self.mode]

    def column_of(self, name: str) -> int:
        columns = self.columns()
        return columns.index(name) if name in columns else -1

    def entry(self, index: QModelIndex | int) -> Entry | None:
        row = index if isinstance(index, int) else (index.row() if index.isValid() else -1)
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def row_of(self, path: str) -> int:
        if self._row_of is None:
            self._row_of = {entry.path: row for row, entry in enumerate(self._rows)}
        return self._row_of.get(path, -1)

    def index_of(self, path: str) -> QModelIndex:
        row = self.row_of(path)
        return self.index(row, 0) if row >= 0 else QModelIndex()

    def _thumbnail_ready(self, path: str) -> None:
        row = self.row_of(path)
        if row >= 0:
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])

    # --- what the views read ---
    def shown_name(self, entry: Entry) -> str:
        name = entry.name
        if not self.show_extensions and not entry.is_dir:
            name = split_extension(name)[0] or name
        return display_name(name)

    def text(self, entry: Entry, column: str) -> str:
        if column == "name":
            return self.shown_name(entry)
        if column == "date":
            return format_time(entry.mtime)
        if column == "type":
            return entry.type_text
        if column == "size":
            return "" if entry.is_dir else format_size(entry.size)
        if column == "location":
            return display_name(entry.location)
        if column == "deleted":
            return format_time(entry.deleted)
        return ""

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        entry = self._rows[index.row()]
        column = COLUMNS[self.mode][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self.text(entry, column)
        if role == ENTRY_ROLE:
            return entry
        if role == Qt.ItemDataRole.EditRole:
            return entry.name if column == "name" else None
        if role == Qt.ItemDataRole.DecorationRole:
            return mime.entry_icon(entry) if column == "name" else None
        if role == Qt.ItemDataRole.TextAlignmentRole and column == "size":
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole and column == "name":
            return display_name(entry.name)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{self.shown_name(entry)}, {entry.type_text}"
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(COLUMNS[self.mode]):
            return None
        column = COLUMNS[self.mode][section]
        if role == Qt.ItemDataRole.DisplayRole:
            if column == "location" and self.mode == TRASH:
                return "Original location"
            return HEADERS[column]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int((Qt.AlignmentFlag.AlignRight if column == "size" else Qt.AlignmentFlag.AlignLeft)
                       | Qt.AlignmentFlag.AlignVCenter)
        return None

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:
        entry = self.entry(index)
        if role != Qt.ItemDataRole.EditRole or entry is None or index.column() != 0 or self.mode == TRASH:
            return False
        name = str(value)
        if name and name != entry.name:
            self.rename_requested.emit(entry, name)
        return True

    def mimeTypes(self) -> list[str]:
        return ["text/uri-list"]

    def mimeData(self, indexes) -> QMimeData:
        rows = sorted({index.row() for index in indexes if index.isValid()})
        return clipboard.mime_data([self._rows[row].path for row in rows if row < len(self._rows)])

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction | Qt.DropAction.LinkAction

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
