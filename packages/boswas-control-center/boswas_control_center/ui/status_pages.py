"""Windows Compatibility, Boswas Update Center and Privacy.

Windows Compatibility reads the session agent (system.status, apps.list)
and opens the Compatibility Manager; it never starts Wine or a Windows
application. The Update Center reads APT's periodic settings and stamps
(read-only) and explains that updates install automatically. Privacy shows
what the device agent may send, from its configuration and status.
"""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton

from .. import viewmodel as vm
from ..catalog import COMPAT_MANAGER
from .base import Page
from .common import SP, Card, InfoGrid, SummaryCard, icon, muted_label, plain_label


class WindowsPage(Page):
    data_keys = ("session_system", "windows_apps", "agent")

    def build(self) -> None:
        self.summary = SummaryCard(self.content)
        self.open_button = QPushButton("Open Compatibility Manager", self.summary)
        self.open_button.setIcon(icon("boswas-compat-manager", "application-x-ms-dos-executable",
                                      fallback=SP.SP_DesktopIcon))
        self.open_button.setAccessibleDescription("Install, start and manage Windows applications")
        self.open_button.clicked.connect(lambda: self.window_.open_module(COMPAT_MANAGER))
        self.summary.actions.addWidget(self.open_button)
        self.body.addWidget(self.summary)
        self.details = Card("Environment", self.content)
        self.grid = InfoGrid(self.details)
        self.details.addWidget(self.grid)
        self.body.addWidget(self.details)
        note = muted_label("Boswas OS runs x86_64 (64-bit) Windows applications only. Each application runs in its "
                           "own sandbox and can reach only the folders it was granted.", parent=self.content)
        self.body.addWidget(note)

    def view(self) -> vm.WindowsView:
        system, apps, agent = (self.store.get(k) for k in self.data_keys)
        return vm.windows_view(system.value if system is not None and system.ok else None,
                               system.error if system is not None and not system.ok else None,
                               apps.value if apps is not None and apps.ok else None,
                               apps.error if apps is not None and not apps.ok else None,
                               agent.value if agent is not None and agent.ok else None,
                               loading=self.store.loading("session_system"))

    def render(self) -> None:
        view = self.view()
        self.summary.set_summary(view.summary)
        self.grid.set_rows(view.rows)
        programs = self.store.value("programs") or {}
        self.open_button.setEnabled(programs.get("compat-manager", True))


class UpdatesPage(Page):
    data_keys = ("updates", "agent_config", "live")

    def build(self) -> None:
        self.summary = SummaryCard(self.content)
        self.body.addWidget(self.summary)
        self.details = Card("Details", self.content)
        self.grid = InfoGrid(self.details)
        self.details.addWidget(self.grid)
        self.body.addWidget(self.details)
        self.log_card = Card("Recent automatic updates", self.content)
        self.log = plain_label("", wrap=True, selectable=True, parent=self.log_card)
        self.log_note = muted_label("", parent=self.log_card)
        self.log_card.addWidget(self.log)
        self.log_card.addWidget(self.log_note)
        self.body.addWidget(self.log_card)

    def render(self) -> None:
        result = self.store.get("updates")
        if result is None:
            self.summary.set_summary(vm.Summary(vm.CHECKING, "Reading the update settings…", ""))
            return
        settings = vm.device_settings(self.store.value("agent_config"))
        view = vm.updates_view(result.value if result.ok else None, settings.get("UPDATE_POLICY"),
                               self.store.value("live") is True)
        self.summary.set_summary(view.summary)
        self.grid.set_rows(view.rows)
        self.log.setText("\n".join(view.log_lines))
        self.log.setVisible(bool(view.log_lines))
        self.log_note.setText(view.log_note)
        self.log_note.setVisible(bool(view.log_note))


class PrivacyPage(Page):
    data_keys = ("agent", "agent_config")

    def build(self) -> None:
        self.summary = SummaryCard(self.content)
        self.body.addWidget(self.summary)
        self.details = Card("What the device agent may send", self.content)
        self.grid = InfoGrid(self.details)
        self.details.addWidget(self.grid)
        self.body.addWidget(self.details)
        self.add_modules("Privacy settings")

    def render(self) -> None:
        agent = self.store.get("agent")
        view = vm.privacy_view(self.store.value("agent"), self.store.value("agent_config"),
                               agent.error if agent is not None and not agent.ok else None)
        self.summary.set_summary(view.summary)
        self.grid.set_rows(view.rows)
