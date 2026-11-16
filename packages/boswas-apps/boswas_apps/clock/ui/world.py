"""The World clock page: the time here and elsewhere, a slider to compare other times of day."""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QSlider, QVBoxLayout, QWidget

import boswas_fluent as fluent

from ...reminders import textfmt, timeutil
from .. import cities, model
from .common import Context
from .widgets import Ticker, scroll_page

STEP_MINUTES = 15
RANGE_STEPS = 24 * 60 // STEP_MINUTES          # a day back and a day ahead


class AddCityDialog(fluent.ContentDialog):
    def __init__(self, parent: QWidget, taken: set[str]) -> None:
        super().__init__(parent, "Add a city", primary="Add", close="Cancel")
        self._taken = taken
        holder = QWidget(self)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.search = fluent.SearchBox(holder, placeholder="Search for a city or a country")
        self.results = QListWidget(holder)
        self.results.setAccessibleName("Cities")
        self.results.setMinimumHeight(260)
        layout.addWidget(self.search)
        layout.addWidget(self.results, 1)
        self.set_content(holder)
        self.search.textChanged.connect(self._fill)
        self.results.currentRowChanged.connect(lambda _row: self._sync())
        self.results.itemDoubleClicked.connect(lambda _item: self.accept() if self.place() is not None else None)
        self.search.query_submitted.connect(lambda _text: self.accept() if self.place() is not None else None)
        self._fill("")

    def open(self) -> None:
        super().open()
        self.search.setFocus()

    def _fill(self, text: str) -> None:
        self.results.clear()
        for place in cities.search(text):
            item = QListWidgetItem(place.label)
            item.setData(Qt.ItemDataRole.UserRole, place)
            if place.zone in self._taken:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setText(f"{place.label} (already added)")
            self.results.addItem(item)
        for row in range(self.results.count()):
            if self.results.item(row).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.results.setCurrentRow(row)
                break
        self._sync()

    def _sync(self) -> None:
        self.primary_button.setEnabled(self.place() is not None)

    def place(self) -> cities.Place | None:
        item = self.results.currentItem()
        if item is None or not item.flags() & Qt.ItemFlag.ItemIsEnabled:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.search.hasFocus():
            self.results.setFocus()
            return
        super().keyPressEvent(event)


class ClockRow(fluent.Card):
    """One place: day or night, its name, how far it is from here, its time."""

    def __init__(self, page: "WorldPage", zone: str | None, name: str) -> None:
        super().__init__(padding=16, spacing=0)
        self.zone = zone                                # None: the local time
        row = QHBoxLayout()
        row.setSpacing(16)
        self.icon = fluent.IconWidget("display-brightness-symbolic", 24, self)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.name_label = fluent.Label(name, "body-strong", self)
        self.detail_label = fluent.Label("", "caption", self, tone="secondary")
        texts.addWidget(self.name_label)
        texts.addWidget(self.detail_label)
        self.time_label = fluent.Label("", "title", self)
        row.addWidget(self.icon)
        row.addLayout(texts, 1)
        row.addWidget(self.time_label)
        if zone is not None:
            self.remove_button = fluent.IconButton("edit-delete-symbolic", f"Remove {name}", self)
            self.remove_button.clicked.connect(lambda: page.remove_city(zone))
            row.addWidget(self.remove_button)
        self.body.addLayout(row)
        self._daytime: bool | None = None

    def show_time(self, moment: dt.datetime, detail: str, daytime: bool, twelve_hour: bool) -> None:
        self.time_label.setText(textfmt.time_of(moment, twelve_hour))
        self.detail_label.setText(detail)
        if daytime != self._daytime:
            self._daytime = daytime
            self.icon.set_icon("display-brightness-symbolic" if daytime else "night-light-symbolic")
            self.icon.setToolTip("Daytime" if daytime else "Night")
        self.setAccessibleName(f"{self.name_label.text()}: {self.time_label.text()}, {detail}, "
                               f"{'day' if daytime else 'night'}")


class WorldPage(QWidget):
    def __init__(self, context: Context, window) -> None:
        super().__init__()
        self.context = context
        self._window = window
        self.rows: list[ClockRow] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        bar_holder = QHBoxLayout()
        bar_holder.setContentsMargins(32, 0, 32, 0)
        self.commands = fluent.CommandBar(self)
        self.commands.add_command("Add a city", "list-add-symbolic", self.add_city, shortcut="Ctrl+N")
        bar_holder.addWidget(self.commands)
        layout.addLayout(bar_holder)
        content = QWidget()
        self.list_layout = QVBoxLayout(content)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(4)
        self.compare = fluent.Card(padding=16, spacing=8)
        head = QHBoxLayout()
        self.compare_label = fluent.Label("", "body-strong", self.compare)
        self.now_button = fluent.Button("Back to now", self.compare)
        self.now_button.clicked.connect(lambda: self.slider.setValue(0))
        head.addWidget(self.compare_label, 1)
        head.addWidget(self.now_button)
        self.slider = QSlider(Qt.Orientation.Horizontal, self.compare)
        self.slider.setRange(-RANGE_STEPS, RANGE_STEPS)
        self.slider.setPageStep(4)
        self.slider.setAccessibleName("Compare another time")
        self.slider.setToolTip("Move to see what time it is elsewhere at another time here")
        self.slider.valueChanged.connect(lambda _value: self.ticker.kick())
        self.compare.body.addLayout(head)
        self.compare.body.addWidget(self.slider)
        self.list_layout.addWidget(self.compare)
        self.list_layout.addSpacing(8)
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(4)
        self.list_layout.addLayout(self.rows_layout)
        layout.addWidget(scroll_page(content), 1)
        self.ticker = Ticker(self, self._tick)
        context.store.changed.connect(self.refresh)
        context.settings.changed.connect(self.refresh)
        self.refresh()

    def offset_seconds(self) -> int:
        return self.slider.value() * STEP_MINUTES * 60

    def refresh(self) -> None:
        places = self.context.store.doc["world"]
        wanted = [None] + [place["zone"] for place in places]
        if wanted != [row.zone for row in self.rows]:
            for row in self.rows:
                self.rows_layout.removeWidget(row)
                row.deleteLater()
            self.rows = [ClockRow(self, None, "Local time")]
            self.rows += [ClockRow(self, place["zone"], place["name"]) for place in places]
            for row in self.rows:
                self.rows_layout.addWidget(row)
        self.ticker.kick()

    def _tick(self) -> int | None:
        wall = self.context.now().wall
        shown = wall + self.offset_seconds()
        here = self.context.zone()
        twelve = self.context.twelve_hour()
        local = timeutil.from_epoch(shown, here)
        offset = self.offset_seconds()
        if offset:
            self.compare_label.setText(f"{textfmt.duration_words(abs(offset))} "
                                       f"{'from now' if offset > 0 else 'ago'}: {textfmt.short_date(local.date())}, "
                                       f"{textfmt.time_of(local, twelve)} here")
        else:
            self.compare_label.setText("Now. Move the slider to compare another time.")
        self.now_button.setEnabled(bool(offset))
        for row in self.rows:
            if row.zone is None:
                row.show_time(local, f"{textfmt.long_date(local.date())} · {timeutil.zone_key(here)}",
                              6 <= local.hour < 18, twelve)
                continue
            there = model.world_time(row.zone, shown, here)
            if there is None:
                continue
            day = {-1: "Yesterday", 0: "Today", 1: "Tomorrow"}[there.day_shift]
            row.show_time(there.local, f"{day}, {textfmt.offset_words(there.offset).lower()}", there.daytime, twelve)
        return int((60 - wall % 60) * 1000) + 50

    def add_city(self) -> None:
        taken = {place["zone"] for place in self.context.store.doc["world"]}
        dialog = AddCityDialog(self._window, taken)
        result = self._window.run_dialog(dialog)
        place = dialog.place()
        dialog.deleteLater()
        if result != fluent.ContentDialog.Result.PRIMARY or place is None:
            return
        if len(taken) >= model.MAX_WORLD:
            self._window.report("warning", f"There can be at most {model.MAX_WORLD} cities.")
            return
        self.context.store.mutate(lambda doc: model.world_add(doc, place.zone, place.name) and None)

    def remove_city(self, zone: str) -> None:
        places = self.context.store.doc["world"]
        entry = next((place for place in places if place["zone"] == zone), None)
        if entry is None:
            return
        position = places.index(entry)
        removed = dict(entry)
        self.context.store.mutate(lambda doc: model.world_remove(doc, zone) and None)

        def undo(doc: dict) -> None:
            if all(place["zone"] != zone for place in doc["world"]):
                doc["world"].insert(min(position, len(doc["world"])), dict(removed))
        self._window.offer_undo(f"{removed['name']} removed.", lambda: self.context.store.mutate(undo))
