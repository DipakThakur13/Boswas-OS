"""One tab: a location with its history, its model and views, and the pages shown instead of an empty view."""

from __future__ import annotations

import os

from PySide6.QtCore import QFileSystemWatcher, QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import QStackedLayout, QTabBar, QWidget

import boswas_fluent as fluent

from . import clipboard, dnd, places
from .computer import VolumesPage
from .listing import Entry
from .model import FOLDER, SEARCH, TRASH, FolderModel
from .text import display_name, format_size, plural
from .views import FileView

_REFRESH_MS = 350


class FolderTab(QWidget):
    """What one tab shows. The window listens to its signals and runs the commands."""

    location_changed = Signal()             # location, title or history changed
    status_changed = Signal()               # row count, selection or loading changed

    def __init__(self, services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.location = ""
        self.back: list[str] = []
        self.forward: list[str] = []
        self.search_query = ""
        self._select: list[str] = []
        self._rename_after = False
        self.model = FolderModel(services, self)
        self.view = FileView(self.model, services, self)
        self.empty = fluent.EmptyState("", "", None, None, self)
        self.volumes_page = VolumesPage(services, self)
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        for page in (self.view, self.empty, self.volumes_page):
            self._stack.addWidget(page)
        self.watcher = QFileSystemWatcher(self)
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self._refresh_now)
        self._loading_note = QTimer(self)
        self._loading_note.setSingleShot(True)
        self._loading_note.setInterval(300)
        self._loading_note.timeout.connect(self._update_page)
        self.watcher.directoryChanged.connect(self._directory_changed)
        self.model.loading_changed.connect(self._loading_changed)
        self.model.load_finished.connect(self._loaded)
        self.model.rows_changed.connect(self._rows_changed)
        self.view.selection_changed.connect(self.status_changed)

    # --- where ---
    def title(self) -> str:
        if self.search_query:
            return f"{self.search_query} – Search"
        return display_name(places.title(self.location))

    def is_folder(self) -> bool:
        """A real folder is shown (not the Trash, This PC or search results)."""
        return bool(self.location) and not places.is_virtual(self.location) and not self.search_query

    def directory(self) -> str | None:
        """The folder new files go to and a terminal opens in; None in the Trash and on This PC."""
        return self.location if self.location and not places.is_virtual(self.location) else None

    def navigate(self, location: str, *, record: bool = True, select: list[str] | None = None) -> None:
        """Show ``location``. A path of a file shows its folder with the file selected."""
        if not places.is_virtual(location):
            location = os.path.normpath(location)
            if not os.path.isdir(location) and os.path.lexists(location):
                select = [location]
                location = os.path.dirname(location)
        if record and self.location and location != self.location:
            self.back.append(self.location)
            del self.back[:-100]
            self.forward.clear()
        self.location = location
        self.search_query = ""
        self._select = list(select or [])
        self._rename_after = False
        self._load()
        self.location_changed.emit()

    def _watch(self) -> None:
        watched = self.watcher.directories()
        if watched:
            self.watcher.removePaths(watched)
        targets: list[str] = []
        if self.location == places.TRASH:
            targets = [os.path.join(directory, "files") for directory, _top in self.services.trash.trash_dirs()]
            targets.append(os.path.dirname(self.services.trash.home_trash))
        elif self.location and not places.is_virtual(self.location):
            targets = [self.location]
        targets = [target for target in targets if os.path.isdir(target)]
        if targets:
            self.watcher.addPaths(targets)

    def _load(self) -> None:
        self._refresh.stop()
        self._watch()
        if self.location == places.COMPUTER:
            self.model.cancel()
            self._update_page()
            return
        if self.location == places.TRASH:
            self.model.load(TRASH, self.location)
        elif self.search_query:
            self.model.load(SEARCH, self.location, self.search_query)
        else:
            self.model.load(FOLDER, self.location)

    def go_back(self) -> None:
        if self.back:
            self.forward.append(self.location)
            self.navigate(self.back.pop(), record=False)

    def go_forward(self) -> None:
        if self.forward:
            self.back.append(self.location)
            self.navigate(self.forward.pop(), record=False)

    def parent_location(self) -> str | None:
        if self.search_query:
            return self.location
        return places.parent(self.location)

    def go_up(self) -> None:
        if self.search_query:
            self.stop_search()
            return
        target = places.parent(self.location)
        if target is not None:
            came_from = self.location
            self.navigate(target, select=[came_from] if not places.is_virtual(came_from) else None)

    def refresh(self, select: list[str] | None = None, rename: bool = False) -> None:
        """Read the location again now; afterwards select ``select`` (and start renaming it)."""
        if select is not None:
            self._select = list(select)
            self._rename_after = rename
        self._refresh.stop()
        if self.location == places.COMPUTER:
            self.services.volumes.refresh()
            return
        if self.location == places.TRASH:
            self._watch()
        self.model.refresh()

    def _directory_changed(self, _path: str) -> None:
        if not self._refresh.isActive():
            # A folder that takes long to read is read again less often.
            self._refresh.start(max(_REFRESH_MS, int(self.model.last_duration * 3000)))

    def _refresh_now(self) -> None:
        if self.model.is_loading:
            self._refresh.start(_REFRESH_MS)
            return
        if self.location == places.TRASH:
            self._watch()
        if self.location and self.location != places.COMPUTER:
            self.model.refresh()

    # --- search ---
    def start_search(self, query: str) -> None:
        query = query.strip()
        if not query or not self.location or places.is_virtual(self.location):
            return
        self.search_query = query
        self._select = []
        self.model.load(SEARCH, self.location, query)
        self.location_changed.emit()

    def stop_search(self) -> None:
        if self.search_query:
            self.search_query = ""
            self._load()
            self.location_changed.emit()

    # --- pages ---
    def _loading_changed(self, loading: bool) -> None:
        if loading:
            self._loading_note.start()
        self._update_page()
        self.status_changed.emit()

    def _rows_changed(self) -> None:
        self._update_page()
        self.status_changed.emit()

    def _loaded(self, _error: str) -> None:
        self._update_page()
        if self._select:
            found = self.view.select_paths(self._select)
            rename = self._rename_after and found == 1
            self._select = []
            self._rename_after = False
            if rename:
                QTimer.singleShot(0, self.view.rename_current)
        self.status_changed.emit()

    def _update_page(self) -> None:
        model = self.model
        if self.location == places.COMPUTER:
            self._stack.setCurrentWidget(self.volumes_page)
            return
        if model.rows:
            self._stack.setCurrentWidget(self.view)
            return
        if model.is_loading:
            if self._loading_note.isActive():
                return                      # not long enough yet to say so
            self._show_empty("Searching…" if self.search_query else "Loading…", "", "view-refresh-symbolic")
        elif model.error:
            self._show_empty("This folder cannot be shown", model.error, "dialog-warning-symbolic")
        elif self.search_query:
            self._show_empty("No results", f"Nothing in this folder has “{self.search_query}” in its name.",
                             "system-search-symbolic")
        elif self.location == places.TRASH:
            self._show_empty("The Trash is empty", "Deleted files and folders are kept here until you empty it.",
                             "user-trash")
        elif model.total_count():
            self._show_empty("Only hidden items", "This folder holds hidden files only. Ctrl+H shows them.", "folder")
        else:
            self._show_empty("This folder is empty", "", "folder")

    def _show_empty(self, title: str, text: str, icon: str) -> None:
        self.empty.set_title(title)
        self.empty.set_text(text)
        self.empty.set_icon(icon)
        self._stack.setCurrentWidget(self.empty)

    def shows_view(self) -> bool:
        return self._stack.currentWidget() is self.view

    def page(self) -> QWidget:
        return self._stack.currentWidget()

    # --- status ---
    def status_texts(self) -> tuple[str, str]:
        """(how many items, what is selected) for the status bar."""
        if self.location == places.COMPUTER:
            return plural(len(self.services.volumes.volumes), "volume"), ""
        model = self.model
        count = len(model.rows)
        if model.is_loading:
            left = (f"Searching… {plural(count, 'result')}" if self.search_query
                    else f"Loading… {plural(count, 'item')}" if count else "Loading…")
        elif self.search_query:
            left = plural(count, "result")
        else:
            left = plural(count, "item")
        selected = self.view.selected_count()
        if not selected:
            return left, ""
        right = f"{plural(selected, 'item')} selected"
        if selected <= 5000:
            entries = self.view.selected_entries()
            if entries and not any(entry.is_dir for entry in entries):
                right += f"  {format_size(sum(entry.size for entry in entries))}"
        return left, right

    def selected_entries(self) -> list[Entry]:
        return self.view.selected_entries() if self.shows_view() else []

    def focus_view(self) -> None:
        if self.location == places.COMPUTER:
            self.volumes_page.list.setFocus()
        else:
            self.view.current_view().setFocus()

    def shutdown(self) -> None:
        self._refresh.stop()
        self.model.shutdown()


class TabStrip(QTabBar):
    """The tabs of a window. Files can be dropped on a tab; a middle click closes it."""

    dropped = Signal(int, list, str)            # tab index, paths, "copy" or "move"
    middle_clicked = Signal(int)
    context_requested = Signal(int, QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setExpanding(False)
        self.setDocumentMode(True)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setAcceptDrops(True)
        self.setAccessibleName("Tabs")
        self.destination_of = lambda index: None     # set by the window: the folder of a tab, or None
        self._device: int | None = None
        self._middle = -1

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._middle = self.tabAt(event.position().toPoint())
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0 and index == self._middle:
                self.middle_clicked.emit(index)
            self._middle = -1
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        index = self.tabAt(event.pos())
        if index >= 0:
            self.context_requested.emit(index, event.globalPos())

    def _action(self, event):
        index = self.tabAt(event.position().toPoint())
        destination = self.destination_of(index) if index >= 0 else None
        if destination is None or not clipboard.has_files(event.mimeData()):
            return -1, None, Qt.DropAction.IgnoreAction
        return index, destination, dnd.choose_action(event.modifiers(), event.possibleActions(), self._device,
                                                     destination)

    def dragEnterEvent(self, event) -> None:
        self._device = dnd.source_device(event.mimeData())
        self.dragMoveEvent(event)

    def dragMoveEvent(self, event) -> None:
        _index, _destination, action = self._action(event)
        if action == Qt.DropAction.IgnoreAction:
            event.ignore()
            return
        event.setDropAction(action)
        event.accept()

    def dropEvent(self, event) -> None:
        self._device = dnd.source_device(event.mimeData())
        index, _destination, action = self._action(event)
        paths, _cut = clipboard.read(event.mimeData())
        if action == Qt.DropAction.IgnoreAction or not paths:
            event.ignore()
            return
        event.setDropAction(action)
        event.accept()
        self.dropped.emit(index, paths, "move" if action == Qt.DropAction.MoveAction else "copy")
