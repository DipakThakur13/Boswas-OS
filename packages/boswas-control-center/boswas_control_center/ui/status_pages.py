"""Windows Compatibility and Privacy.

Windows Compatibility reads the session agent (system.status, apps.list),
including the Windows architectures the runtime supports, and opens the
Compatibility Manager; it never starts Wine or a Windows application.
Privacy shows what the device agent may send, from its configuration and
status. (The Update Center is in updates.py.)
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
        self.note = muted_label("", parent=self.content)
        self.body.addWidget(self.note)

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
        self.note.setText(f"Boswas OS runs {view.support[0].lower() + view.support[1:]}. Each application runs in "
                          "its own sandbox and can reach only the folders it was granted.")
        programs = self.store.value("programs") or {}
        self.open_button.setEnabled(programs.get("compat-manager", True))


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
