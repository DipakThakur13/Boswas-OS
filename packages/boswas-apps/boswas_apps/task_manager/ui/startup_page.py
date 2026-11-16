"""The Startup apps page: the XDG autostart entries of this desktop, switched on and off per user."""

from __future__ import annotations

from PySide6.QtWidgets import QStackedLayout, QVBoxLayout

import boswas_fluent as fluent

from ..desktopfile import AppIndex, StartupEntry, StartupError
from .dialogs import AddStartupDialog, app_icon
from .pages import Page
from .table import Column, Row, TableView, TreeModel


class StartupPage(Page):
    key = "startup"
    search_hint = "Filter by name, publisher or command"

    def __init__(self, window) -> None:
        super().__init__(window)
        columns = [
            Column("name", "Name", lambda row: row.data.name, width=280, icon=lambda row: app_icon(row.data.icon)),
            Column("publisher", "Publisher", lambda row: row.data.publisher, width=150),
            Column("status", "Status", lambda row: "Enabled" if row.data.enabled else "Disabled", width=100),
            Column("source", "Installed for", lambda row: "Everyone" if row.data.source == "System" else "You",
                   width=110),
            Column("command", "Command", lambda row: row.data.command, width=420, tooltip=True),
        ]
        self.model = TreeModel(columns, self)
        self.view = TableView(self.model, self, tree=False)
        self.view.setAccessibleName("Startup apps")
        self.empty = fluent.EmptyState("No startup apps", "Programs that start when you sign in are listed here.",
                                       "media-playback-start-symbolic", "Add an app")
        self.empty.action_triggered.connect(self.add)
        self.nothing = fluent.EmptyState("Nothing matches", "No startup app matches what you typed in the search box.",
                                         "system-search-symbolic")
        self.stack = QStackedLayout()
        for widget in (self.view, self.empty, self.nothing):
            self.stack.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 0, 20, 16)
        layout.addLayout(self.stack, 1)
        self.entries: list[StartupEntry] = []
        bar = self.bar
        self.enable_action = bar.add_command("Enable", "object-select-symbolic", lambda: self.set_enabled(True))
        self.disable_action = bar.add_command("Disable", "window-close-symbolic", lambda: self.set_enabled(False))
        self.add_action = bar.add_command("Add", "list-add-symbolic", self.add)
        self.remove_action = bar.add_command("Remove", "edit-delete-symbolic", self.remove, primary=False)
        self.location_action = bar.add_command("Open file location", "folder-symbolic", self.open_location,
                                               primary=False)
        self.view.context_requested.connect(self._context)
        self.view.delete_pressed.connect(self.remove)
        self.view.selectionModel().currentChanged.connect(lambda *_args: self.sync_commands())
        self.sync_commands()

    def activated(self) -> None:
        self.load()

    def refresh(self) -> None:
        self.load()

    def load(self) -> None:
        self.window.call(self.window.backend.list_startup, done=self._loaded,
                         failed=lambda error: self.window.notify("error",
                                                                 f"The startup apps could not be read: {error}"),
                         key="startup")

    def _loaded(self, entries: list[StartupEntry]) -> None:
        self.entries = entries
        self.rebuild()

    def set_filter(self, text: str) -> None:
        super().set_filter(text)
        self.rebuild()

    def rebuild(self) -> None:
        needle = self.filter_text
        rows = [Row(entry.file_id, entry, entry) for entry in self.entries
                if not needle or needle in entry.name.casefold() or needle in entry.publisher.casefold()
                or needle in entry.command.casefold()]
        self.model.set_rows(rows)
        self.stack.setCurrentWidget(self.empty if not self.entries else self.nothing if not rows else self.view)
        self.sync_commands()

    def selected(self) -> StartupEntry | None:
        row = self.view.current_row()
        return row.data if row is not None else None

    def sync_commands(self) -> None:
        entry = self.selected()
        self.enable_action.setEnabled(entry is not None and not entry.enabled)
        self.disable_action.setEnabled(entry is not None and entry.enabled)
        self.remove_action.setEnabled(entry is not None and entry.source == "User")
        self.location_action.setEnabled(entry is not None)

    def set_enabled(self, enabled: bool) -> None:
        entry = self.selected()
        if entry is None:
            return
        try:
            self.window.backend.set_startup_enabled(entry.file_id, enabled)
        except StartupError as error:
            self.window.notify("error", str(error))
            return
        self.window.notify("success", f"{entry.name} will start when you sign in." if enabled
                           else f"{entry.name} will no longer start when you sign in.")
        self.load()

    def add(self) -> None:
        self.window.call(self.window.backend.list_applications, done=self._choose,
                         failed=lambda error: self.window.notify("error",
                                                                 f"The applications could not be read: {error}"))

    def _choose(self, applications) -> None:
        self.window.applications_loaded(AppIndex(applications))
        dialog = AddStartupDialog(self.window, applications)

        def finished(result) -> None:
            path = dialog.chosen()
            if result == fluent.ContentDialog.Result.PRIMARY and path:
                self._add(path)

        self.window.open_dialog(dialog, finished)

    def _add(self, path: str) -> None:
        try:
            file_id = self.window.backend.add_startup(path)
        except StartupError as error:
            self.window.notify("error", str(error))
            return
        self.window.notify("success", "The app was added and will start when you sign in.")
        self.load()
        self.view.select_key(file_id)

    def remove(self) -> None:
        entry = self.selected()
        if entry is None or entry.source != "User":
            return
        if not self.window.ask("Remove this startup app?",
                               f"{entry.name} will no longer start when you sign in. The application itself stays "
                               "installed.", "Remove"):
            return
        try:
            self.window.backend.remove_startup(entry.file_id)
        except StartupError as error:
            self.window.notify("error", str(error))
            return
        self.load()

    def open_location(self) -> None:
        entry = self.selected()
        if entry is not None:
            self.window.show_file(entry.path)

    def copy_selection(self) -> str:
        entry = self.selected()
        if entry is None:
            return ""
        return "\t".join((entry.name, entry.publisher, "Enabled" if entry.enabled else "Disabled", entry.command))

    def context_menu(self) -> fluent.MenuFlyout | None:
        entry = self.selected()
        if entry is None:
            return None
        menu = fluent.MenuFlyout(self.view)
        if entry.enabled:
            menu.add_item("Disable", lambda: self.set_enabled(False))
        else:
            menu.add_item("Enable", lambda: self.set_enabled(True))
        menu.addSeparator()
        menu.add_item("Open file location", self.open_location, icon="folder-symbolic")
        if entry.source == "User":
            menu.add_item("Remove", self.remove, icon="edit-delete-symbolic", shortcut="Del")
        return menu

    def _context(self, position) -> None:
        menu = self.context_menu()
        if menu is not None:
            self._menu = menu
            menu.popup(position)
