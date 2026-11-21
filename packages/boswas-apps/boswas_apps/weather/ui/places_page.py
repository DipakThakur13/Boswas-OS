"""Manage places: search for a place and add it; reorder, remove, and choose the home place."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import service
from ..model import Place
from ..places import MAX_PLACES
from .widgets import scroll_page

if TYPE_CHECKING:
    from .window import WeatherWindow

SEARCH_DELAY_MS = 400            # typing pauses this long before a search is sent
PRIVACY = ("Weather never uses your location. Search for a city or town and add it: only the places you add "
           "are looked up.")


def coordinates(place: Place) -> str:
    """ "52.52° N, 13.41° E"."""
    return (f"{abs(place.latitude):.2f}° {'N' if place.latitude >= 0 else 'S'}, "
            f"{abs(place.longitude):.2f}° {'E' if place.longitude >= 0 else 'W'}")


class PlaceRow(fluent.ListItem):
    """One of the user's places: open it with a click; home, up, down and remove buttons at its end."""

    def __init__(self, page: "PlacesPage", place: Place, position: int, count: int, home: bool) -> None:
        region = place.region()
        subtitle = " · ".join(part for part in (region, "Home" if home else "") if part)
        super().__init__(place.name, subtitle, "starred-symbolic" if home else "mark-location-symbolic")
        self.place = place
        self.button.setToolTip(f"Show the forecast for {place.label()}")
        self.button.setAccessibleDescription(
            f"{subtitle}. Alt+Up and Alt+Down move it, Delete removes it." if subtitle else
            "Alt+Up and Alt+Down move it, Delete removes it.")
        tools = QWidget()
        row = QHBoxLayout(tools)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.home_button = fluent.IconButton("starred-symbolic" if home else "non-starred-symbolic",
                                             f"{place.name} is your home place" if home else
                                             f"Make {place.name} your home place", tools, checkable=True)
        self.home_button.setChecked(home)
        self.up_button = fluent.IconButton("go-up-symbolic", f"Move {place.name} up", tools)
        self.down_button = fluent.IconButton("go-down-symbolic", f"Move {place.name} down", tools)
        self.remove_button = fluent.IconButton("edit-delete-symbolic", f"Remove {place.name}", tools)
        self.up_button.setEnabled(position > 0)
        self.down_button.setEnabled(position < count - 1)
        for button in (self.home_button, self.up_button, self.down_button, self.remove_button):
            row.addWidget(button)
        self.set_trailing(tools)
        key = place.key
        self.clicked.connect(lambda: page.window.open_place(key))
        self.home_button.clicked.connect(lambda: page.toggle_home(key))
        self.up_button.clicked.connect(lambda: page.move(key, -1, "up"))
        self.down_button.clicked.connect(lambda: page.move(key, 1, "down"))
        self.remove_button.clicked.connect(lambda: page.window.remove_place(key))
        self.button.installEventFilter(page)
        self.button.setProperty("weatherPlace", key)

    def control(self, name: str) -> QWidget:
        return {"row": self.button, "home": self.home_button, "up": self.up_button, "down": self.down_button,
                "remove": self.remove_button}.get(name, self.button)


class ResultRow(fluent.ListItem):
    """A place the search found: a click adds it to My places."""

    def __init__(self, page: "PlacesPage", place: Place, added: bool) -> None:
        super().__init__(place.name, place.region() or coordinates(place), "mark-location-symbolic")
        self.place = place
        self.added = added
        if added:
            self.set_trailing(fluent.Label("Added", "caption", tone="secondary"))
            self.button.setToolTip(f"{place.label()} is in My places")
            self.button.setAccessibleDescription(f"{place.region()}. Already in My places")
        else:
            self.set_trailing(fluent.IconWidget("list-add-symbolic", 16))
            self.button.setToolTip(f"Add {place.label()} ({coordinates(place)})")
            self.button.setAccessibleName(f"Add {place.name}")
        self.clicked.connect(lambda: page.add(self.place))
        self.button.installEventFilter(page)


class PlacesPage(QWidget):
    def __init__(self, window: "WeatherWindow") -> None:
        super().__init__()
        self.window = window
        self.results: list[Place] = []
        self.result_rows: list[ResultRow] = []
        self.place_rows: list[PlaceRow] = []
        self._query = ""                         # the text of the search that is running or was last run
        self._pending: str | None = None         # a newer text, searched when the running search is done
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        search_row = QHBoxLayout()
        search_row.setSpacing(12)
        self.search_box = fluent.SearchBox(content, placeholder="Search for a city or town")
        self.search_box.setMaxLength(service.MAX_QUERY)
        self.search_box.installEventFilter(self)
        self.search_ring = fluent.ProgressRing(content, size=20)
        self.search_ring.setAccessibleName("Searching")
        self.search_ring.hide()
        search_row.addWidget(self.search_box, 1)
        search_row.addWidget(self.search_ring)
        body.addLayout(search_row)
        self.results_card = fluent.Card(content, padding=4, spacing=2)
        self.results_label = fluent.Label("", "body", self.results_card, tone="secondary", wrap=True)
        self.results_label.setContentsMargins(12, 8, 12, 8)
        self.results_card.add(self.results_label)
        self.results_box = QVBoxLayout()
        self.results_box.setSpacing(2)
        self.results_card.add_layout(self.results_box)
        self.results_card.hide()
        body.addWidget(self.results_card)

        body.addSpacing(16)
        heading = QHBoxLayout()
        self.heading_label = fluent.Label("My places", "body-strong", content)
        self.count_label = fluent.Label("", "caption", content, tone="secondary")
        heading.addWidget(self.heading_label)
        heading.addStretch(1)
        heading.addWidget(self.count_label)
        body.addLayout(heading)
        self.places_card = fluent.Card(content, padding=4, spacing=2)
        self.places_box = QVBoxLayout()
        self.places_box.setSpacing(2)
        self.places_card.add_layout(self.places_box)
        body.addWidget(self.places_card)
        self.empty = fluent.EmptyState("No places yet", PRIVACY, "mark-location-symbolic", None, content)
        body.addWidget(self.empty)
        self.privacy_label = fluent.Label(PRIVACY, "caption", content, tone="secondary", wrap=True)
        body.addSpacing(8)
        body.addWidget(self.privacy_label)
        layout.addWidget(scroll_page(content), 1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(SEARCH_DELAY_MS)
        self.timer.timeout.connect(lambda: self.search(self.search_box.text()))
        self.search_box.textChanged.connect(self._typed)
        self.search_box.query_submitted.connect(self._submitted)
        self.search_box.cleared.connect(self.clear_results)
        self.refresh()

    # --- my places ---
    def refresh(self, focus: tuple[str, str] | None = None) -> None:
        """Build the rows of My places again (after a change); ``focus`` = (place key, control) gets the focus."""
        for row in self.place_rows:
            self.places_box.removeWidget(row)
            row.hide()
            row.deleteLater()
        places = self.window.places
        self.place_rows = [PlaceRow(self, place, index, len(places.places), place.key == places.home)
                           for index, place in enumerate(places.places)]
        for row in self.place_rows:
            self.places_box.addWidget(row)
        self.empty.setVisible(not self.place_rows)
        self.places_card.setVisible(bool(self.place_rows))
        self.privacy_label.setVisible(bool(self.place_rows))
        self.count_label.setText(f"{len(self.place_rows)} of {MAX_PLACES}" if self.place_rows else "")
        self._mark_added()
        if focus is not None:
            key, name = focus
            row = next((row for row in self.place_rows if row.place.key == key), None)
            if row is not None:
                control = row.control(name)
                if not control.isEnabled():
                    control = row.control("row")
                control.setFocus(Qt.FocusReason.OtherFocusReason)

    def row(self, key: str) -> PlaceRow | None:
        return next((row for row in self.place_rows if row.place.key == key), None)

    def move(self, key: str, step: int, control: str = "row") -> None:
        if self.window.move_place(key, step):
            self.refresh((key, control))

    def toggle_home(self, key: str) -> None:
        self.window.set_home(None if self.window.places.home == key else key)
        self.refresh((key, "home"))

    # --- search ---
    def _typed(self, text: str) -> None:
        if service.search_query(text):
            self.timer.start()
        else:
            self.timer.stop()
            self._pending = None
            if not text.strip():
                self.clear_results()

    def _submitted(self, text: str) -> None:
        self.timer.stop()
        if service.search_query(text):
            self.search(text)

    def search(self, text: str) -> None:
        """Ask the service for places; one search at a time, the newest text wins."""
        query = service.search_query(text)
        if not query:
            return
        if self.window.worker.busy("search"):
            self._pending = query
            return
        self._pending = None
        self._query = query
        self.search_ring.show()
        self.window.worker.submit(self.window.service.search, query, key="search",
                                  on_done=lambda places, q=query: self._found(q, places),
                                  on_error=lambda error, q=query: self._failed(q, error))

    def searching(self) -> bool:
        return self.window.worker.busy("search") or self._pending is not None

    def _next(self) -> bool:
        pending, self._pending = self._pending, None
        if pending is not None and pending != self._query:
            self.search(pending)
            return True
        self.search_ring.hide()
        return False

    def _found(self, query: str, places: list[Place]) -> None:
        if self._next() or service.search_query(self.search_box.text()) != query:
            return                       # the text changed meanwhile: that search's answer is what counts
        self.window.dismiss_banner("search")
        self.show_results(query, places)

    def _failed(self, query: str, error: Exception) -> None:
        if self._next():
            return
        self.window.show_error(error, lambda q=query: self.search(q), context="search")

    def show_results(self, query: str, places: list[Place]) -> None:
        self.clear_results(keep_text=True)
        self.results = list(places)
        if not places:
            self.results_label.setText(f"No places match “{query}”. Check the spelling or try a nearby town.")
        else:
            self.results_label.setText(f"Places that match “{query}”: select one to add it.")
        taken = set(self.window.places.keys())
        self.result_rows = [ResultRow(self, place, place.key in taken) for place in places]
        for row in self.result_rows:
            row.setEnabled(not row.added)
            self.results_box.addWidget(row)
        self.results_card.show()

    def clear_results(self, keep_text: bool = False) -> None:
        for row in self.result_rows:
            self.results_box.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.result_rows = []
        self.results = []
        if not keep_text:
            self.results_card.hide()

    def _mark_added(self) -> None:
        if self.results:
            taken = set(self.window.places.keys())
            if any((place.key in taken) != row.added for place, row in zip(self.results, self.result_rows)):
                focus = next((index for index, row in enumerate(self.result_rows) if row.button.hasFocus()), None)
                self.show_results(self._query, self.results)
                self._focus_result(focus)

    def add(self, place: Place) -> None:
        index = next((i for i, row in enumerate(self.result_rows) if row.place.key == place.key), None)
        if self.window.add_place(place):
            self._focus_result(index)

    def _focus_result(self, index: int | None) -> None:
        if index is None:
            return
        enabled = [row for row in self.result_rows[index:] + self.result_rows[:index][::-1] if row.isEnabled()]
        if enabled:
            enabled[0].button.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.search_box.setFocus(Qt.FocusReason.OtherFocusReason)

    def focus_search(self) -> None:
        self.search_box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_box.selectAll()

    # --- keys: arrows between rows, Alt+arrows to move a place, Delete to remove it ---
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress:
            return False
        key, modifiers = event.key(), event.modifiers()
        if obj is self.search_box:
            if key == Qt.Key.Key_Down:
                rows = [row for row in self.result_rows if row.isEnabled()] or self.place_rows
                if rows:
                    rows[0].button.setFocus(Qt.FocusReason.TabFocusReason)
                    return True
            return False
        place_key = obj.property("weatherPlace")
        if place_key and modifiers & Qt.KeyboardModifier.AltModifier and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.move(place_key, -1 if key == Qt.Key.Key_Up else 1)
            return True
        if place_key and key == Qt.Key.Key_Delete:
            self.window.remove_place(place_key)
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and not modifiers & Qt.KeyboardModifier.AltModifier:
            buttons = [row.button for row in (*self.result_rows, *self.place_rows) if row.isEnabled()]
            if obj in buttons:
                index = buttons.index(obj) + (-1 if key == Qt.Key.Key_Up else 1)
                if index < 0:
                    self.search_box.setFocus(Qt.FocusReason.BacktabFocusReason)
                elif index < len(buttons):
                    buttons[index].setFocus(Qt.FocusReason.TabFocusReason)
                return True
        return False
