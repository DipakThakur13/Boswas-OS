"""What a page shows besides its own content: native settings sections and program rows.

A SectionGroup is one settings section of ui/settings with its title above
it. The page activates the section when it becomes visible and deactivates
it when it is left; a section that fails to build or to activate is replaced
by a message, so that one broken section never takes the page with it. A
section that hides itself (the battery on a device without one) takes its
title with it: the group is shown only while the section is.

A ProgramTile is a row that starts another Boswas program of the catalog
(Files, the Compatibility Manager): a SettingsCard of the toolkit with the
program's icon, title and a one-line description, and a chevron telling
that it opens a separate window. It takes keyboard focus (Tab), opens with
Space or Enter, and the toolkit draws its focus ring. A program that is not
installed is disabled and says so; an optional one is hidden.
"""

from __future__ import annotations

import logging

import boswas_fluent as fluent
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..catalog import Program
from .common import SP, icon
from .settings import section_id

log = logging.getLogger("boswas_control_center.settings")

NOT_INSTALLED = "Not installed on this system"


class SectionGroup(QWidget):
    """One native settings section under its title."""

    def __init__(self, section_class: type, parent: QWidget | None = None):
        super().__init__(parent)
        self.section_class = section_class
        self.section_id = section_id(section_class)
        self.active = False
        self.failed = False
        self.rows = QVBoxLayout(self)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(4)
        self.title = fluent.Label(section_class.title, "body-strong", self)
        self.title.setContentsMargins(0, 8, 0, 4)
        self.rows.addWidget(self.title)
        self.setAccessibleName(section_class.title)
        self.problem = fluent.InfoBar("error", parent=self, closable=False)
        self.problem.hide()
        self.rows.addWidget(self.problem)
        self.section: QWidget | None = None
        try:
            self.section = section_class(self)
        except Exception as exc:              # the section's own bug: say so in its place, keep the page
            self._failed("could not be shown", exc)
        else:
            # A section may hide itself in its constructor, until it knows its state.
            hidden = self.section.isHidden() and self.section.testAttribute(
                Qt.WidgetAttribute.WA_WState_ExplicitShowHide)
            self.rows.addWidget(self.section)
            self.section.installEventFilter(self)
            if hidden:
                self.section.hide()
                self.hide()

    def eventFilter(self, watched, event) -> bool:
        # A section that hides itself has nothing to show on this device: no title without content.
        if watched is self.section and not self.failed:
            if event.type() == QEvent.Type.HideToParent:
                self.hide()
            elif event.type() == QEvent.Type.ShowToParent:
                self.show()
        return False

    def _failed(self, what: str, exc: Exception) -> None:
        self.failed = True
        log.exception("settings section %s %s", self.section_class.__name__, what, exc_info=exc)
        self.problem.show_message("error", f"These settings {what} ({type(exc).__name__}: {exc}).")
        if self.section is not None:
            self.section.hide()

    def activate(self) -> None:
        if self.section is None or self.active or self.failed:
            return
        self.active = True
        try:
            self.section.activate()
        except Exception as exc:
            self._failed("could not be loaded", exc)

    def deactivate(self) -> None:
        if self.section is None or not self.active:
            return
        self.active = False
        try:
            self.section.deactivate()
        except Exception as exc:
            log.exception("settings section %s failed to stop", self.section_class.__name__, exc_info=exc)


class ProgramTile(fluent.SettingsCard):
    activated_program = Signal(object)

    ICON = 24

    def __init__(self, program: Program, parent: QWidget | None = None):
        super().__init__(program.title, program.description, None, None, parent, clickable=True)
        self.program = program
        self.available: bool | None = None
        self.set_icon(icon(program.icon, "preferences-system", fallback=SP.SP_FileDialogDetailedView), self.ICON)
        self.setAccessibleDescription(f"{program.description}. Opens in a separate window.")
        self.button.setAccessibleDescription(self.accessibleDescription())
        self.setToolTip(program.description)
        self.clicked.connect(lambda: self.activated_program.emit(self.program))
        if program.optional:
            self.hide()                       # until the program is known to be installed

    def set_available(self, available: bool) -> None:
        self.available = available
        if self.program.optional:
            self.setVisible(available)
        self.setEnabled(available)
        description = self.program.description if available else NOT_INSTALLED
        self.description_label.setText(description)
        self.setToolTip(description)
        self.setAccessibleDescription(description + (". Opens in a separate window." if available else ""))
        self.button.setAccessibleDescription(self.accessibleDescription())

    def description(self) -> str:
        return self.program.description if getattr(self, "available", None) is not False else NOT_INSTALLED


class ProgramGroup(QWidget):
    """A titled list of program rows."""

    def __init__(self, title: str, programs: tuple[Program, ...], parent: QWidget | None = None):
        super().__init__(parent)
        self.rows = QVBoxLayout(self)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(4)
        self.title = None
        if title:
            self.title = fluent.Label(title, "body-strong", self)
            self.title.setContentsMargins(0, 8, 0, 4)
            self.rows.addWidget(self.title)
            self.setAccessibleName(title)
        self.tiles = [ProgramTile(program, self) for program in programs]
        for tile in self.tiles:
            self.rows.addWidget(tile)
        self.relayout()

    def relayout(self) -> None:
        """Show the group only while it has a row to show (never a title with nothing under it)."""
        self.setVisible(any(not tile.isHidden() for tile in self.tiles))

    def set_availability(self, programs: dict | None) -> None:
        for tile in self.tiles:
            if isinstance(programs, dict) and tile.program.id in programs:
                tile.set_available(bool(programs[tile.program.id]))
        self.relayout()

    def tile(self, program_id: str) -> ProgramTile:
        return next(t for t in self.tiles if t.program.id == program_id)
