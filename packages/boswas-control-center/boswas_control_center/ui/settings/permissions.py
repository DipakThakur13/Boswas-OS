"""Privacy & security > App permissions: the portal permissions of sandboxed (Flatpak) applications.

One expander per application with what it was allowed or denied (camera,
microphone, location, screenshots, running in the background, ...), read
from the user's permission store with `flatpak permission-list`; Revoke
forgets one decision (`flatpak permission-remove TABLE OBJECT APP`) after a
question, so that the application has to ask again. Above them, the plain
statement that native applications are not sandboxed. Reading runs on a
worker thread; the store is the user's own, no administrator is involved.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtWidgets import QWidget

from ... import commands, permissions
from ...errors import Failed
from . import context
from .system import CHANGE_TIMEOUT, ToolSection, tool_failure
from .widgets import RowList, ask

FLATPAK_TIMEOUT = 60.0


class AppPermissionsSection(ToolSection):
    page = "app-permissions"
    title = "App permissions"
    order = 10
    keywords = ("permissions", "camera access", "microphone access", "location", "screenshot", "background apps",
                "revoke", "flatpak", "portal", "privacy")

    def __init__(self, parent: QWidget | None = None, *, runner=None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.native = self.add(fluent.SettingsCard("Applications that are not sandboxed", permissions.NOT_SANDBOXED,
                                                   fluent.icon("dialog-information-symbolic"), None, self))
        self.list = self.add(RowList(self, applications=True))
        self.empty = self.add(fluent.SettingsCard(
            "No sandboxed application asked for a permission", "When a Flatpak application asks for the camera, your "
            "location or a screenshot, your answer is listed here and can be taken back.",
            fluent.icon("changes-prevent-symbolic", "security-high-symbolic"), None, self))
        self.empty.hide()
        self.permissions: list[permissions.Permission] = []

    def read(self) -> list[permissions.Permission]:
        if not self.runner.available(commands.EXECUTABLES["flatpak"]):
            raise Failed("Flatpak is not installed, so there are no sandboxed applications on this device.")
        result = self.runner.run(commands.argv("flatpak", "permission-list"), FLATPAK_TIMEOUT)
        if result.code != 0:
            raise tool_failure("Reading the permissions", result)
        return permissions.parse_list(result.stdout)

    def show_state(self, found: list[permissions.Permission]) -> None:
        self.permissions = found
        self.show_notice("")
        groups = permissions.by_app(found)
        self.list.sync(list(groups), self._row, lambda app, row: self._update(row, groups[app]))
        self.empty.setVisible(not found)

    def load_failed(self, exc: Exception) -> None:
        self.list.sync([], lambda _k: None, lambda _k, _r: None)
        self.empty.hide()
        self.show_notice(str(exc), "info")

    def _row(self, app: str) -> fluent.SettingsExpander:
        card = fluent.SettingsExpander(app, "", fluent.icon("boswas-sandbox", "application-x-executable"), None,
                                       self.list)
        card.rows = []
        return card

    def _update(self, card: fluent.SettingsExpander, items: list[permissions.Permission]) -> None:
        for row in card.rows:
            row.hide()
            row.deleteLater()
        card.rows = []
        allowed = sum(1 for p in items if p.value.startswith("Allowed"))
        card.header.set_description(f"{len(items)} permission{'s' if len(items) != 1 else ''}, {allowed} allowed")
        for permission in items:
            button = fluent.Button("Revoke", card)
            button.setAccessibleName(f"Revoke {permission.title} for {permission.app}")
            button.clicked.connect(lambda _c=False, p=permission: self.revoke(p))
            button.setEnabled(not self.busy)
            row = card.add_row(permission.title, permission.value, button, permission.icon)
            card.rows.append(row)

    def set_enabled(self, enabled: bool) -> None:
        for app in self.list.keys():
            for row in self.list.row(app).rows:
                control = row.control()
                if control is not None:
                    control.setEnabled(enabled)

    def revoke(self, permission: permissions.Permission) -> None:
        if not ask(self, f"Revoke {permission.title.lower()} for {permission.app}?",
                   "Your earlier answer is forgotten. The application has to ask again the next time it wants this.",
                   "Revoke"):
            return

        def apply() -> None:
            result = self.runner.run(commands.permission_remove_argv(permission.table, permission.ident,
                                                                     permission.app), CHANGE_TIMEOUT)
            if result.code != 0:
                raise tool_failure("Revoking the permission", result)

        self.change(apply, f"{permission.title} was revoked for {permission.app}.")



SECTIONS = (AppPermissionsSection,)
