"""Cleanup recommendations on System > Storage: temporary files, the Trash, old downloads.

  Temporary files   the biggest items of the cache folder with their sizes;
                    the person ticks what to clear, then confirms
  Trash             its size and number of items; Empty Trash after a question
  Downloads         what was not changed for 14, 30, 60 or 90 days, biggest
                    first; the ticked items go to the Trash (nothing is
                    deleted until the Trash is emptied)

Nothing is preselected and nothing is removed without a confirmation that
names the amount. The folders are read on a worker thread
(boswas_control_center.cleanup); deleting and moving is userfiles.py's.
The thumbnail cache has its own row above, on the Storage page.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtWidgets import QCheckBox, QComboBox, QWidget

from ... import cleanup, userfiles
from ... import viewmodel as vm
from ...errors import Failed
from .system import FilesSection
from .widgets import ask

MAX_SHOWN = 15


class PickList:
    """Rows with a check box each, inside an expander; what is ticked is what the action takes."""

    def __init__(self, expander: fluent.SettingsExpander, empty: str):
        self.expander = expander
        self.empty_text = empty
        self.rows: list[tuple[fluent.SettingsCard, QCheckBox, cleanup.Item]] = []
        self.empty = expander.add_row(empty, "")

    def show(self, items: list[cleanup.Item], on_change) -> None:
        for card, _box, _item in self.rows:
            card.hide()
            card.deleteLater()
        self.rows = []
        self.empty.setVisible(not items)
        for item in items[:MAX_SHOWN]:
            box = QCheckBox()
            box.setAccessibleName(f"Select {item.name}")
            detail = vm.format_bytes(item.size) + (f" · {item.files} files" if item.folder else "")
            if item.name in cleanup.IN_USE:
                detail += " · also cleared by “Clear Thumbnails” above"
            card = self.expander.add_row(item.name, detail, box,
                                         "folder-symbolic" if item.folder else "text-x-generic-symbolic")
            box.toggled.connect(lambda _on: on_change())
            self.rows.append((card, box, item))

    def picked(self) -> list[cleanup.Item]:
        return [item for _card, box, item in self.rows if box.isChecked()]

    def set_enabled(self, enabled: bool) -> None:
        for _card, box, _item in self.rows:
            box.setEnabled(enabled)


class CleanupSection(FilesSection):
    page = "storage"
    title = "Cleanup recommendations"
    order = 10
    keywords = ("clean up", "cleanup", "temporary files", "cache", "trash", "recycle bin", "downloads",
                "free up space", "storage sense", "old files")

    def __init__(self, parent: QWidget | None = None, *, environ=None):
        super().__init__(parent, environ=environ)
        self.days = 30
        self.cache_button = fluent.Button("Clear Selected…", self)
        self.cache_button.setAccessibleName("Clear the selected temporary files")
        self.cache_button.clicked.connect(self.clear_cache)
        self.cache = self.add(fluent.SettingsExpander(
            "Temporary files", "Reading…", fluent.icon("edit-clear-symbolic"), self.cache_button, self))
        self.cache_list = PickList(self.cache, "Your cache folder is empty.")
        self.cache_note = self.cache.add_row(
            "Programs keep these to start and work faster, and create them again when needed. Close a program "
            "before you clear its files.", "")

        self.trash_button = fluent.Button("Empty Trash…", self)
        self.trash_button.setAccessibleName("Empty the Trash")
        self.trash_button.clicked.connect(self.empty_trash)
        self.trash = self.add(fluent.SettingsCard("Trash", "Reading…", fluent.icon("user-trash-symbolic"),
                                                  self.trash_button, self))

        self.age = QComboBox(self)
        for days in cleanup.DOWNLOAD_AGES:
            self.age.addItem(f"Not changed for {days} days", days)
        self.age.setCurrentIndex(cleanup.DOWNLOAD_AGES.index(self.days))
        self.age.setAccessibleName("Show downloads not changed for")
        self.age.activated.connect(self._age_chosen)
        self.downloads_button = fluent.Button("Move to Trash…", self)
        self.downloads_button.setAccessibleName("Move the selected downloads to the Trash")
        self.downloads_button.clicked.connect(self.trash_downloads)
        self.downloads = self.add(fluent.SettingsExpander(
            "Downloads", "Reading…", fluent.icon("folder-download-symbolic"), self.downloads_button, self))
        self.downloads.header.add_control(self.age)
        self.downloads_list = PickList(self.downloads, "Nothing in Downloads is that old.")
        self.state: tuple | None = None
        self._buttons()

    def watched(self) -> list[str]:
        return []

    def activate(self) -> None:
        super().activate()
        self._poll.stop()                      # a scan of the cache folder is not repeated every few seconds

    # --- reading ---
    def read(self):
        environ = self.environ
        return (cleanup.cache_items(environ), cleanup.trash_usage(environ),
                cleanup.old_downloads(environ, self.days), cleanup.downloads_dir(environ))

    def show_state(self, state) -> None:
        self.state = state
        cache, trash, downloads, folder = state
        total = cleanup.total(cache)
        self.cache.header.set_description(
            f"{vm.format_bytes(total)} in your cache folder; the biggest items are listed" if cache else
            "Your cache folder is empty")
        self.cache_list.show(cache, self._buttons)
        self.trash.set_description("The Trash is empty" if not trash.items else
                                   f"{trash.items} item{'s' if trash.items != 1 else ''}, {vm.format_bytes(trash.size)}"
                                   " · deleted for good when it is emptied")
        self.downloads.header.set_description(
            "There is no Downloads folder" if folder is None else
            f"{len(downloads)} item{'s' if len(downloads) != 1 else ''} not changed for {self.days} days, "
            f"{vm.format_bytes(cleanup.total(downloads))} · selected items go to the Trash")
        self.downloads_list.show(downloads, self._buttons)
        self._buttons()

    def _buttons(self) -> None:
        idle = not self.busy and self.state is not None
        self.cache_button.setEnabled(idle and bool(self.cache_list.picked()))
        self.downloads_button.setEnabled(idle and bool(self.downloads_list.picked()))
        trash = self.state[1] if self.state is not None else None
        self.trash_button.setEnabled(idle and trash is not None and trash.items > 0)
        self.age.setEnabled(idle)

    def set_enabled(self, enabled: bool) -> None:
        self.cache_list.set_enabled(enabled)
        self.downloads_list.set_enabled(enabled)
        self._buttons()

    def _age_chosen(self, _index: int) -> None:
        self.days = self.age.currentData()
        self.reload()

    # --- actions ---
    def clear_cache(self) -> None:
        picked = self.cache_list.picked()
        if not picked:
            return
        size = vm.format_bytes(cleanup.total(picked))
        names = ", ".join(item.name for item in picked[:5]) + (" and more" if len(picked) > 5 else "")
        if not ask(self, "Clear these temporary files?", f"{names} ({size}) are deleted from your cache folder. "
                   "Programs create them again when they need them; close those programs first.", "Clear"):
            return
        environ, wanted = self.environ, [item.name for item in picked]

        def apply() -> None:
            try:
                userfiles.clear_cache_items(environ, wanted)
            except userfiles.UserFileError as exc:
                raise Failed(str(exc)) from None

        self.change(apply, f"Temporary files cleared: about {size} freed.")

    def empty_trash(self) -> None:
        trash = self.state[1] if self.state is not None else None
        if trash is None or not trash.items:
            return
        if not ask(self, "Empty the Trash?", f"{trash.items} item{'s' if trash.items != 1 else ''} "
                   f"({vm.format_bytes(trash.size)}) are deleted for good. This cannot be undone.", "Empty Trash"):
            return
        environ = self.environ

        def apply() -> None:
            try:
                userfiles.empty_trash(environ)
            except userfiles.UserFileError as exc:
                raise Failed(str(exc)) from None

        self.change(apply, f"The Trash was emptied: {vm.format_bytes(trash.size)} freed.")

    def trash_downloads(self) -> None:
        picked = self.downloads_list.picked()
        if not picked:
            return
        size = vm.format_bytes(cleanup.total(picked))
        if not ask(self, "Move these downloads to the Trash?",
                   f"{len(picked)} item{'s' if len(picked) != 1 else ''} ({size}) move from Downloads to the Trash. "
                   "You can still restore them from the Trash; the space is freed when the Trash is emptied.",
                   "Move to Trash"):
            return
        environ, wanted = self.environ, [item.name for item in picked]

        def apply() -> None:
            try:
                userfiles.trash_downloads(environ, wanted)
            except userfiles.UserFileError as exc:
                raise Failed(str(exc)) from None

        self.change(apply, f"Moved to the Trash: {size}. Empty the Trash to free the space.")


SECTIONS = (CleanupSection,)
