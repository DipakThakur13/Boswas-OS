"""The page frame: heading, description, scrollable content, module tiles.

A page is built the first time it is shown (MainWindow builds pages lazily)
and declares the data keys it shows; the store loads them on the Runner's
thread pool and the page redraws in render() whenever one of them changes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ..catalog import PageInfo
from .common import heading, muted_label
from .tiles import ModuleGroup

CONTENT_MAX_WIDTH = 980


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
        row.addStretch(0)
        row.addWidget(self.content, 1)
        row.addStretch(0)
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addLayout(row)
        holder_layout.addStretch(1)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll)
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(32, 24, 32, 28)
        self.body.setSpacing(16)
        self.heading = heading(info.heading, parent=self.content)
        self.description = muted_label(info.description, parent=self.content)
        header = QVBoxLayout()
        header.setSpacing(4)
        header.addWidget(self.heading)
        header.addWidget(self.description)
        self.body.addLayout(header)
        self.modules: ModuleGroup | None = None
        self.build()
        self.body.addStretch(1)
        self.store.changed.connect(self._changed)

    # --- to override ----------------------------------------------------------------------------------
    def build(self) -> None:
        """Create the page's widgets (called once)."""
        self.add_modules()

    def render(self) -> None:
        """Redraw from the store."""

    # --- helpers --------------------------------------------------------------------------------------
    def keys(self) -> tuple[str, ...]:
        extra = ("kcms", "programs") if self.info.modules else ()
        return tuple(dict.fromkeys(self.data_keys + extra))

    def add_modules(self, title: str | None = None) -> ModuleGroup | None:
        if not self.info.modules:
            return None
        self.modules = ModuleGroup(title or self.info.modules_title, self.info.modules, self.content)
        for tile in self.modules.tiles:
            tile.activated_module.connect(self.window_.open_module)
        self.body.addWidget(self.modules)
        return self.modules

    def activate(self, force: bool = False) -> None:
        for key in self.keys():
            self.store.request(key, force)
        self._render_all()

    def refresh(self) -> None:
        self.activate(force=True)

    def _changed(self, key: str) -> None:
        if key in self.keys():
            self._render_all()

    def _render_all(self) -> None:
        if self.modules is not None:
            self.modules.set_availability(self.store.value("kcms"), self.store.value("programs"))
        self.render()
