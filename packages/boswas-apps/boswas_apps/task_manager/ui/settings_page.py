"""The Settings page."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFrame, QScrollArea, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import PAGE_TITLES
from ..settings import REFRESH_TITLES, START_PAGES, THEMES
from .pages import Page


def _combo(options: dict[str, str], current: str) -> QComboBox:
    box = QComboBox()
    for key, title in options.items():
        box.addItem(title, key)
    box.setCurrentIndex(max(0, box.findData(current)))
    box.setMinimumWidth(220)
    return box


class SettingsPage(Page):
    key = "settings"
    searchable = False

    def __init__(self, window) -> None:
        super().__init__(window)
        settings = window.settings
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(4)
        self.refresh_box = _combo(REFRESH_TITLES, settings.refresh)
        self.page_box = _combo({key: PAGE_TITLES[key] for key in START_PAGES}, settings.start_page)
        self.command_switch = fluent.ToggleSwitch(checked=settings.full_command_line)
        self.theme_box = _combo(THEMES, settings.theme)
        layout.addWidget(fluent.Label("General", "body-strong"))
        layout.addSpacing(4)
        layout.addWidget(fluent.SettingsCard(
            "Real-time update speed", "How often the figures are read. Paused reads them only when you press F5.",
            "view-refresh-symbolic", self.refresh_box))
        layout.addWidget(fluent.SettingsCard(
            "Default start page", "The page Task Manager opens with.", "go-home-symbolic", self.page_box))
        layout.addWidget(fluent.SettingsCard(
            "Always show the full command line", "Adds a Command line column to the Processes page.",
            "view-list-symbolic", self.command_switch))
        layout.addSpacing(12)
        layout.addWidget(fluent.Label("Appearance", "body-strong"))
        layout.addSpacing(4)
        layout.addWidget(fluent.SettingsCard(
            "App theme", "Task Manager follows the desktop's light or dark setting unless you choose one here.",
            "preferences-desktop-theme-global", self.theme_box))
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 0, 24, 16)
        outer.addWidget(scroll)
        self.refresh_box.currentIndexChanged.connect(self._changed)
        self.page_box.currentIndexChanged.connect(self._changed)
        self.theme_box.currentIndexChanged.connect(self._changed)
        self.command_switch.toggled.connect(self._changed)

    def _changed(self, *_args) -> None:
        settings = self.window.settings
        settings.refresh = self.refresh_box.currentData()
        settings.start_page = self.page_box.currentData()
        settings.theme = self.theme_box.currentData()
        settings.full_command_line = self.command_switch.isChecked()
        self.window.settings_changed()
