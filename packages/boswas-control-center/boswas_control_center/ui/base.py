"""The page frame: description, scrollable content, settings sections, program rows.

A page is built the first time it is shown (MainWindow builds pages lazily)
and declares the data keys it shows; the store loads them on the Runner's
thread pool and the page redraws in render() whenever one of them changes.
The page title is shown by the window's NavigationView, above the page;
``heading`` holds the same text for assistive technology and is not shown
a second time.

The native settings sections of the page (ui/settings) are placed where the
page calls add_settings(): each under its title, in the order the sections
declare. They are activated when the page becomes the current one and
deactivated when it is left or the window closes.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ..catalog import PageInfo
from . import settings
from .common import heading, muted_label
from .tiles import ProgramGroup, SectionGroup

CONTENT_MAX_WIDTH = 1000


class Page(QWidget):
    data_keys: tuple[str, ...] = ()

    def __init__(self, info: PageInfo, window, parent: QWidget | None = None):
        super().__init__(parent)
        self.info, self.window_, self.store = info, window, window.store
        self.setAccessibleName(info.heading)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget(self.scroll)
        self.content = QWidget(holder)
        self.content.setMaximumWidth(CONTENT_MAX_WIDTH)
        self.content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.content, 1)
        row.addStretch(0)
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addLayout(row)
        holder_layout.addStretch(1)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll)
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(32, 0, 32, 28)
        self.body.setSpacing(16)
        self.heading = heading(info.heading, parent=self.content)
        self.heading.hide()                       # the NavigationView shows the title above the page
        self.description = muted_label(info.description, parent=self.content)
        self.body.addWidget(self.description)
        self.programs: ProgramGroup | None = None
        self.section_groups: list[SectionGroup] = []
        self.active = False
        self.build()
        self.body.addStretch(1)
        self.store.changed.connect(self._changed)

    # --- to override ----------------------------------------------------------------------------------
    def build(self) -> None:
        """Create the page's widgets (called once)."""
        self.add_settings()

    def render(self) -> None:
        """Redraw from the store."""

    # --- helpers --------------------------------------------------------------------------------------
    def keys(self) -> tuple[str, ...]:
        extra = ("programs",) if self.info.programs else ()
        return tuple(dict.fromkeys(self.data_keys + extra))

    def add_settings(self, programs_title: str | None = None) -> None:
        """Put the page's native settings sections here, then the rows that start its programs."""
        self.add_sections()
        self.add_programs(programs_title)

    def add_sections(self) -> list[SectionGroup]:
        added = []
        for section_class in settings.sections_for(self.info.key):
            group = SectionGroup(section_class, self.content)
            self.body.addWidget(group)
            self.section_groups.append(group)
            added.append(group)
        return added

    def add_programs(self, title: str | None = None) -> ProgramGroup | None:
        if not self.info.programs:
            return None
        self.programs = ProgramGroup(title or self.info.programs_title, self.info.programs, self.content)
        for tile in self.programs.tiles:
            tile.activated_program.connect(self.window_.open_program)
        self.body.addWidget(self.programs)
        return self.programs

    @property
    def sections(self) -> list[QWidget]:
        """The section widgets of this page, in the order shown."""
        return [group.section for group in self.section_groups if group.section is not None]

    def section(self, name: str | type) -> QWidget | None:
        """A section of this page by class, class name, title or command-line name."""
        group = self.section_group(name)
        return group.section if group is not None else None

    def section_group(self, name: str | type) -> SectionGroup | None:
        for group in self.section_groups:
            cls = group.section_class
            if name is cls or (isinstance(name, str) and name.casefold() in (cls.__name__.casefold(),
                                                                             cls.title.casefold(), group.section_id)):
                return group
        return None

    def scroll_to_section(self, name: str | type) -> bool:
        """Scroll so that the section's title is at the top of the page (once the layout has settled)."""
        group = self.section_group(name)
        if group is None:
            return False

        def scroll() -> None:
            try:
                top = group.mapTo(self.scroll.widget(), QPoint(0, 0)).y()
                self.scroll.verticalScrollBar().setValue(max(0, top - 4))
            except RuntimeError:                  # the page was closed in between
                pass

        scroll()
        # Sections fill in as their state arrives and the page grows: place it again after that. The
        # page is the timers' context: they do not fire once it is gone.
        QTimer.singleShot(0, self, scroll)
        QTimer.singleShot(250, self, scroll)
        return True

    def activate(self, force: bool = False) -> None:
        for key in self.keys():
            self.store.request(key, force)
        self._render_all()
        if force:
            self.deactivate()
        self.active = True
        for group in self.section_groups:
            group.activate()

    def deactivate(self) -> None:
        """The page was left or the window closes: the sections stop watching."""
        self.active = False
        for group in self.section_groups:
            group.deactivate()

    def refresh(self) -> None:
        self.activate(force=True)

    def _changed(self, key: str) -> None:
        if key in self.keys():
            self._render_all()

    def _render_all(self) -> None:
        if self.programs is not None:
            self.programs.set_availability(self.store.value("programs"))
        self.render()
