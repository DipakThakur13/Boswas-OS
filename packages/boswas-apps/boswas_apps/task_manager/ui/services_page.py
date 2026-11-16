"""The Services page: the services of the system's and of the user's systemd manager."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QStackedLayout, QVBoxLayout

import boswas_fluent as fluent

from ..services import Unit, can_start, can_stop
from .pages import Page
from .table import Column, Row, TableView, TreeModel

SCOPES = ("system", "user")
_SCOPE_NAMES = {"system": "the system's services", "user": "your own services"}


class ServicesPage(Page):
    key = "services"
    search_hint = "Filter by name, description or PID"
    REFRESH_MS = 10000

    def __init__(self, window) -> None:
        super().__init__(window)
        columns = [
            Column("name", "Name", lambda row: row.data.name, width=280),
            Column("pid", "PID", lambda row: str(row.data.pid) if row.data.pid else "",
                   lambda row: row.data.pid or None, right=True, width=76, descending_first=True),
            Column("description", "Description", lambda row: row.data.description, width=320, tooltip=True),
            Column("status", "Status", lambda row: row.data.status, width=120),
            Column("startup", "Startup", lambda row: row.data.enabled.capitalize(), width=100),
            Column("scope", "Runs for", lambda row: row.data.scope_title, width=90),
        ]
        self.model = TreeModel(columns, self)
        self.view = TableView(self.model, self, tree=False, stretch="description")
        self.view.setAccessibleName("Services")
        self.empty = fluent.EmptyState("Services are not available", "", "dialog-information-symbolic")
        self.nothing = fluent.EmptyState("Nothing matches", "No service matches what you typed in the search box.",
                                         "system-search-symbolic")
        self.note = fluent.InfoBar("warning", closable=False)
        self.note.hide()
        self.stack = QStackedLayout()
        for widget in (self.view, self.empty, self.nothing):
            self.stack.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 0, 20, 16)
        layout.setSpacing(8)
        layout.addWidget(self.note)
        layout.addLayout(self.stack, 1)
        self.units: dict[str, list[Unit]] = {}
        self.errors: dict[str, str] = {}
        self.loaded = False
        self.busy = ""                              # the key of the unit an action is running on
        bar = self.bar
        self.start_action = bar.add_command("Start", "media-playback-start-symbolic", lambda: self.act("start"))
        self.stop_action = bar.add_command("Stop", "media-playback-stop-symbolic", lambda: self.act("stop"))
        self.restart_action = bar.add_command("Restart", "view-refresh-symbolic", lambda: self.act("restart"))
        self.details_action = bar.add_command("Go to details", None, self._details, primary=False)
        self.copy_action = bar.add_command("Copy name", "edit-copy-symbolic", self._copy, primary=False)
        self.view.context_requested.connect(self._context)
        self.view.selectionModel().currentChanged.connect(lambda *_args: self.sync_commands())
        self.timer = QTimer(self)
        self.timer.setInterval(self.REFRESH_MS)
        self.timer.timeout.connect(lambda: self.load(False))
        self.sync_commands()

    # --- loading ---
    def activated(self) -> None:
        self.load(not self.loaded)
        if self.window.backend.threaded:
            self.timer.start()

    def deactivated(self) -> None:
        self.timer.stop()

    def refresh(self) -> None:
        self.load(True)

    def load(self, full: bool) -> None:
        for scope in SCOPES:
            self.window.call(self.window.backend.list_services, scope, full,
                             done=lambda units, scope=scope: self._loaded(scope, units),
                             failed=lambda error, scope=scope: self._failed(scope, error), key=f"services-{scope}")

    def _loaded(self, scope: str, units: list[Unit]) -> None:
        self.units[scope] = units
        self.errors.pop(scope, None)
        self.loaded = True
        self.rebuild()

    def _failed(self, scope: str, error: Exception) -> None:
        self.units.pop(scope, None)
        self.errors[scope] = str(error)
        self.rebuild()

    def set_filter(self, text: str) -> None:
        super().set_filter(text)
        self.rebuild()

    def rebuild(self) -> None:
        needle = self.filter_text
        rows = []
        total = 0
        for scope in SCOPES:
            for unit in self.units.get(scope, ()):
                total += 1
                if needle and not (needle in unit.name.casefold() or needle in unit.description.casefold()
                                   or needle == str(unit.pid)):
                    continue
                rows.append(Row(unit.key, unit, unit))
        self.model.set_rows(rows)
        if not self.units and self.errors:
            reasons = sorted({reason.rstrip(".") + "." for reason in self.errors.values()})
            self.empty.set_text("systemd could not be asked for its services. " + " ".join(reasons))
            self.stack.setCurrentWidget(self.empty)
            self.note.hide()
        else:
            self.stack.setCurrentWidget(self.nothing if total and not rows else self.view)
            if self.errors:
                scope, reason = next(iter(self.errors.items()))
                self.note.show_message("warning", f"Only {_SCOPE_NAMES['user' if scope == 'system' else 'system']} "
                                                  f"are listed: {_SCOPE_NAMES[scope]} could not be read. {reason}")
            else:
                self.note.hide()
        self.sync_commands()

    # --- commands ---
    def selected(self) -> Unit | None:
        row = self.view.current_row()
        return row.data if row is not None else None

    def sync_commands(self) -> None:
        unit = self.selected()
        idle = not self.busy
        self.start_action.setEnabled(idle and unit is not None and can_start(unit))
        self.stop_action.setEnabled(idle and unit is not None and can_stop(unit))
        self.restart_action.setEnabled(idle and unit is not None and can_stop(unit))
        self.details_action.setEnabled(unit is not None and unit.pid > 0)
        self.copy_action.setEnabled(unit is not None)

    def act(self, action: str) -> None:
        unit = self.selected()
        if unit is None or self.busy:
            return
        self.busy = unit.key
        self.sync_commands()
        doing = {"start": "Starting", "stop": "Stopping", "restart": "Restarting"}[action]
        self.window.notify("info", f"{doing} {unit.name}… The system may ask you to confirm that it is you.")
        self.window.call(self.window.backend.service_action, unit.scope, action, unit.name,
                         done=self._acted, failed=self._refused)

    def _acted(self, message: str) -> None:
        self.busy = ""
        self.window.notify("success", message)
        self.load(False)

    def _refused(self, error: Exception) -> None:
        self.busy = ""
        self.window.notify("error", str(error))
        self.load(False)

    def _details(self) -> None:
        unit = self.selected()
        if unit is not None and unit.pid:
            self.window.go_to_pid(unit.pid)

    def _copy(self) -> None:
        unit = self.selected()
        if unit is not None:
            self.window.copy_text(unit.name, "The name was copied.")

    def copy_selection(self) -> str:
        unit = self.selected()
        if unit is None:
            return ""
        return "\t".join((unit.name, str(unit.pid or ""), unit.description, unit.status, unit.enabled, unit.scope_title))

    def context_menu(self) -> fluent.MenuFlyout | None:
        unit = self.selected()
        if unit is None:
            return None
        menu = fluent.MenuFlyout(self.view)
        idle = not self.busy
        menu.add_item("Start", lambda: self.act("start"), icon="media-playback-start-symbolic",
                      enabled=idle and can_start(unit))
        menu.add_item("Stop", lambda: self.act("stop"), icon="media-playback-stop-symbolic",
                      enabled=idle and can_stop(unit))
        menu.add_item("Restart", lambda: self.act("restart"), icon="view-refresh-symbolic",
                      enabled=idle and can_stop(unit))
        menu.addSeparator()
        menu.add_item("Go to details", self._details, enabled=unit.pid > 0)
        menu.add_item("Copy name", self._copy, icon="edit-copy-symbolic")
        return menu

    def _context(self, position) -> None:
        menu = self.context_menu()
        if menu is not None:
            self._menu = menu
            menu.popup(position)
