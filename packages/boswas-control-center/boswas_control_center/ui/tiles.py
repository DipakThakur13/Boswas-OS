"""Rows that open a KDE settings module or a program of the catalog.

A row is a SettingsCard of the toolkit: the module's icon, title and a
one-line description, and a chevron telling that it opens a separate
window. It takes keyboard focus (Tab), opens with Space or Enter, and the
toolkit draws its focus ring. A module that is not installed is disabled
and says so; an optional one is hidden.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..catalog import Module
from .common import SP, icon

NOT_INSTALLED = "Not installed on this system"


class ModuleTile(fluent.SettingsCard):
    activated_module = Signal(object)

    ICON = 24

    def __init__(self, module: Module, parent: QWidget | None = None):
        super().__init__(module.title, module.description, None, None, parent, clickable=True)
        self.module = module
        self.available: bool | None = None
        self.set_icon(icon(module.icon, "preferences-system", fallback=SP.SP_FileDialogDetailedView), self.ICON)
        self.setAccessibleDescription(f"{module.description}. Opens in a separate window.")
        self.button.setAccessibleDescription(self.accessibleDescription())
        self.setToolTip(module.description)
        self.clicked.connect(lambda: self.activated_module.emit(self.module))
        if module.optional:
            self.hide()                       # until the module is known to be installed

    def set_available(self, available: bool) -> None:
        self.available = available
        if self.module.optional:
            self.setVisible(available)
        self.setEnabled(available)
        description = self.module.description if available else NOT_INSTALLED
        self.description_label.setText(description)
        self.setToolTip(description)
        self.setAccessibleDescription(description + (". Opens in a separate window." if available else ""))
        self.button.setAccessibleDescription(self.accessibleDescription())

    def description(self) -> str:
        return self.module.description if getattr(self, "available", None) is not False else NOT_INSTALLED


class ModuleGroup(QWidget):
    """A titled list of module rows."""

    def __init__(self, title: str, modules: tuple[Module, ...], parent: QWidget | None = None, columns: int = 1):
        super().__init__(parent)
        self.columns = columns
        self.rows = QVBoxLayout(self)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(4)
        self.title = None
        if title:
            self.title = fluent.Label(title, "body-strong", self)
            self.title.setContentsMargins(0, 8, 0, 4)
            self.rows.addWidget(self.title)
            self.setAccessibleName(title)
        self.tiles = [ModuleTile(module, self) for module in modules]
        for tile in self.tiles:
            self.rows.addWidget(tile)
        self.relayout()

    def relayout(self) -> None:
        """Show the group only while it has a row to show."""
        shown = [t for t in self.tiles if not t.isHidden()]
        self.setVisible(bool(shown) or not self.tiles)

    def set_availability(self, kcms: dict | None, programs: dict | None) -> None:
        for tile in self.tiles:
            known = kcms if tile.module.kind == "kcm" else programs
            if isinstance(known, dict) and tile.module.id in known:
                tile.set_available(bool(known[tile.module.id]))
        self.relayout()

    def tile(self, module_id: str) -> ModuleTile:
        return next(t for t in self.tiles if t.module.id == module_id)
