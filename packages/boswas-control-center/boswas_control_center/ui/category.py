"""Category pages, the cards that lead to a category's pages, and the list of search results.

A category page (System, Bluetooth & devices, ...) lists its pages as cards:
icon, title, one line of what is there, and a chevron. A card opens its page
(click, Space or Enter); a page that has nothing to show on this device, or
the Install page outside a live session, has no card. Pages with content of
their own that are also categories (Boswas Update) put the same cards below
their content.

SearchResults is the content area while something is typed into the search
box of the pane: one row per category, page, section or single setting that
matches (catalog.search), with the path to it; a row opens its place.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from .. import catalog
from .base import CONTENT_MAX_WIDTH, Page
from .tiles import place_icon

class CategoryPage(Page):
    """A category: its pages as cards."""

    def build(self) -> None:
        self.add_subpages()


class ResultRow(fluent.SettingsCard):
    def __init__(self, entry: catalog.Entry, parent: QWidget | None = None):
        place = catalog.BY_KEY[entry.page]
        where = " › ".join(catalog.path(entry.page))
        description = where if entry.kind in ("section", "setting") else (
            place.card_text if entry.kind == "page" else place.description)
        if entry.kind == "page":
            description = f"{where} · {description}"
        super().__init__(entry.title, description, None, None, parent, clickable=True)
        self.entry = entry
        self.set_icon(place_icon(place), 20)


class SearchResults(QWidget):
    """What the search box found, as rows that open their place."""

    chosen = Signal(object)              # the catalog.Entry of the row that was clicked

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget(self.scroll)
        self.content = QWidget(holder)
        self.content.setMaximumWidth(CONTENT_MAX_WIDTH)
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
        self.body.setSpacing(4)
        self.summary = fluent.Label("", "body", self.content, tone="secondary", wrap=True)
        self.summary.setContentsMargins(0, 0, 0, 8)
        self.body.addWidget(self.summary)
        self.rows: list[ResultRow] = []
        self.empty = fluent.EmptyState("No results", "Nothing matches. Try another word, for example “display”, "
                                       "“password” or “printer”.", fluent.icon("system-search-symbolic"),
                                       parent=self.content)
        self.empty.hide()
        self.body.addWidget(self.empty)
        self.list = QVBoxLayout()
        self.list.setSpacing(4)
        self.body.addLayout(self.list)
        self.setAccessibleName("Search results")
        self.query = ""

    def show_results(self, query: str, entries: list[catalog.Entry]) -> None:
        self.query = query
        for old in self.rows:
            self.list.removeWidget(old)
            old.hide()
            old.deleteLater()
        self.rows = []
        for entry in entries:
            row = ResultRow(entry, self.content)
            row.clicked.connect(lambda e=entry: self.chosen.emit(e))
            self.list.addWidget(row)
            self.rows.append(row)
        count = len(entries)
        self.summary.setText(f"{count} result{'s' if count != 1 else ''} for “{query}”" if count else
                             f"No results for “{query}”")
        self.summary.setVisible(bool(count))
        self.empty.setVisible(not count)
        self.scroll.verticalScrollBar().setValue(0)

    def titles(self) -> list[str]:
        return [row.entry.title for row in self.rows]
