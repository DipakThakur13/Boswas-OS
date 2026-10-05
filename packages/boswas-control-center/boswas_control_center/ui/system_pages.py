"""System, About Boswas OS and Install Boswas OS.

About reads the release identity (/usr/lib/boswas/release, image-info),
the kernel, processor, memory, storage, the Plasma version, and the
summaries of the Security and Windows Compatibility pages. Install is shown
only in a live session; its only action is a confirmed restart
(`systemctl reboot`) so the user can choose the installer in the boot menu.
Nothing ever starts an installer.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .. import viewmodel as vm
from .base import Page
from .common import (SP, Card, Collapsible, InfoGrid, StatusIcon, heading, icon, mark_icon, muted_label,
                     plain_label, title_label)


class SystemPage(Page):
    data_keys = ("agent",)

    def build(self) -> None:
        self.add_modules()
        self.device = Card("Device management", self.content)
        self.grid = InfoGrid(self.device)
        self.device.addWidget(self.grid)
        self.body.addWidget(self.device)

    def render(self) -> None:
        agent = self.store.get("agent")
        self.grid.set_rows(vm.agent_rows(self.store.value("agent"),
                                         agent.error if agent is not None and not agent.ok else None))


class AboutPage(Page):
    data_keys = ("release", "hardware", "storage", "plasma", "live", "security", "session_system", "windows_apps",
                 "agent", "agent_runtime")

    def build(self) -> None:
        self.heading.hide()
        self.description.hide()
        hero = QHBoxLayout()
        hero.setSpacing(18)
        self.mark = QLabel(self.content)
        self.mark.setPixmap(mark_icon().pixmap(QSize(72, 72)))
        self.mark.setFixedSize(72, 72)
        self.mark.setAccessibleName("Boswas OS logo")
        text = QVBoxLayout()
        text.setSpacing(2)
        self.name = heading("Boswas OS", 2.0, self.content)
        self.version = muted_label("", parent=self.content)
        text.addStretch(1)
        text.addWidget(self.name)
        text.addWidget(self.version)
        text.addStretch(1)
        hero.addWidget(self.mark)
        hero.addLayout(text, 1)
        self.copy_button = QPushButton("Copy to Clipboard", self.content)
        self.copy_button.setIcon(icon("edit-copy", fallback=SP.SP_FileDialogDetailedView))
        self.copy_button.setAccessibleDescription("Copies this information as text, for example for a support request")
        self.copy_button.clicked.connect(self.copy)
        hero.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addLayout(hero)

        self.card = Card("This device", self.content)
        self.grid = InfoGrid(self.card)
        self.card.addWidget(self.grid)
        self.body.addWidget(self.card)

        self.technical = Collapsible("Technical details", self.content)
        self.technical_grid = InfoGrid(self.technical.content)
        self.technical.content_layout.addWidget(self.technical_grid)
        self.body.addWidget(self.technical)
        self.licences = Collapsible("Open-source licences", self.content)
        self.licences.content_layout.addWidget(plain_label(vm.LICENCES_TEXT, wrap=True, selectable=True,
                                                           parent=self.licences.content))
        self.body.addWidget(self.licences)
        self.release = vm.release_info(None)
        self.rows: list[tuple[str, str]] = []
        self.technical_rows: list[tuple[str, str]] = []

    def _windows(self) -> vm.WindowsView | None:
        system, apps = self.store.get("session_system"), self.store.get("windows_apps")
        if system is None:
            return None
        return vm.windows_view(system.value if system.ok else None, system.error, apps.value if apps and apps.ok
                               else None, apps.error if apps else None, self.store.value("agent"))

    def _security(self) -> vm.SecurityView | None:
        status = self.store.get("security")
        if status is None:
            return None
        agent = self.store.get("agent")
        return vm.security_view(status.value if status.ok else None, status.error,
                                agent.value if agent is not None and agent.ok else None,
                                agent.error if agent is not None and not agent.ok else None,
                                self.store.value("agent_runtime"))

    def render(self) -> None:
        release = vm.release_info(self.store.value("release"))
        agent = self.store.get("agent")
        self.name.setText(release.name)
        self.version.setText(release.version_text)
        self.rows = vm.about_rows(release, self.store.value("hardware"), self.store.value("storage"),
                                  self.store.value("plasma") if self.store.get("plasma") is not None else None,
                                  self.store.value("live"), self._security(), self._windows(),
                                  self.store.value("agent"),
                                  agent.error if agent is not None and not agent.ok else None)
        if self.store.get("plasma") is None:
            self.rows = [(k, "Checking…" if k == "Desktop" else v) for k, v in self.rows]
        self.grid.set_rows(self.rows)
        self.technical_rows = vm.technical_rows(release, self.store.value("hardware"))
        self.technical_grid.set_rows(self.technical_rows)
        self.release = release

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(vm.about_text(self.release.name, self.rows, self.technical_rows))
        self.window_.show_status("Copied the device information to the clipboard.")


class InstallPage(Page):
    data_keys = ("live", "programs")

    def build(self) -> None:
        card = Card(parent=self.content, margins=22, spacing=12)
        top = QHBoxLayout()
        top.setSpacing(16)
        badge_icon = QLabel(card)
        badge_icon.setPixmap(icon("system-software-install", "drive-harddisk", fallback=SP.SP_DriveHDIcon)
                             .pixmap(QSize(48, 48)))
        badge_icon.setFixedSize(48, 48)
        top.addWidget(badge_icon, 0, Qt.AlignmentFlag.AlignTop)
        intro = QVBoxLayout()
        intro.setSpacing(3)
        intro.addWidget(title_label("This is a live session", 1.3, card))
        intro.addWidget(muted_label("Try Boswas OS without changing this computer, then install it when you are "
                                    "ready.", parent=card))
        intro.addStretch(1)
        top.addLayout(intro, 1)
        card.addLayout(top)
        card.layout_.addSpacing(4)
        for point in vm.INSTALL_POINTS:
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(StatusIcon(vm.INFO, 18, card), 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(plain_label(point, wrap=True, parent=card), 1)
            card.addLayout(row)
        actions = QHBoxLayout()
        self.restart_button = QPushButton("Restart to Install…", card)
        self.restart_button.setIcon(icon("system-reboot", fallback=SP.SP_BrowserReload))
        self.restart_button.setAccessibleDescription("Restarts the computer so you can choose Install Boswas OS in "
                                                     "the boot menu. You are asked to confirm first.")
        self.restart_button.clicked.connect(self.window_.restart_to_install)
        actions.addWidget(self.restart_button)
        actions.addWidget(muted_label(vm.INSTALL_BOOT_HINT, parent=card), 1)
        card.addLayout(actions)
        self.body.addWidget(card)

    def render(self) -> None:
        programs = self.store.value("programs") or {}
        self.restart_button.setEnabled(programs.get("systemctl", True))
