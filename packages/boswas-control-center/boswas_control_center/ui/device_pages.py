"""Hardware pages: the plain settings pages, Power and Storage.

Network, Bluetooth, Display, Sound, Users and Devices are configured
entirely in KDE's modules, so those pages explain what is where and open the
modules. Power adds the battery state from /sys/class/power_supply; Storage
the usage of / and /home.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QVBoxLayout, QWidget

from .. import viewmodel as vm
from .base import Page
from .common import Card, StatusIcon, muted_label, plain_label, title_label


class SettingsPage(Page):
    """A page whose settings all live in KDE modules."""


class PowerPage(Page):
    data_keys = ("power",)

    def build(self) -> None:
        self.card = Card("Battery", self.content)
        self.summary = plain_label("Checking…", wrap=True, parent=self.card)
        self.card.addWidget(self.summary)
        self.batteries = QVBoxLayout()
        self.batteries.setSpacing(10)
        self.card.addLayout(self.batteries)
        self.body.addWidget(self.card)
        self.add_modules()
        self.battery_rows: list[QWidget] = []

    def render(self) -> None:
        result = self.store.get("power")
        if result is None:
            return
        view = vm.power_view(result.value if result.ok else [])
        self.summary.setText(view.summary)
        self.summary.setVisible(not view.batteries)
        for widget in self.battery_rows:
            widget.deleteLater()
        self.battery_rows = []
        for battery in view.batteries:
            row = QWidget(self.card)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(12)
            text = QVBoxLayout()
            text.setSpacing(2)
            text.addWidget(title_label(battery.name, 1.0, row))
            text.addWidget(muted_label(battery.status, parent=row))
            bar = QProgressBar(row)
            bar.setRange(0, 100)
            bar.setValue(battery.percent or 0)
            bar.setFormat(f"{battery.percent}%" if battery.percent is not None else "Unknown")
            bar.setAccessibleName(f"{battery.name} charge")
            layout.addWidget(StatusIcon(battery.state, 22, row), 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(text, 1)
            layout.addWidget(bar, 2)
            self.batteries.addWidget(row)
            self.battery_rows.append(row)
        self.card.title.setText("Battery" if view.batteries else "Power source")


class StoragePage(Page):
    data_keys = ("storage", "live")

    def build(self) -> None:
        self.disks = QVBoxLayout()
        self.disks.setSpacing(12)
        self.body.addLayout(self.disks)
        self.loading = muted_label("Reading disk usage…", parent=self.content)
        self.body.addWidget(self.loading)
        self.note = muted_label("", parent=self.content)
        self.note.hide()
        self.body.addWidget(self.note)
        self.add_modules()
        self.cards: dict[str, tuple] = {}

    def _card(self, row: vm.StorageRow) -> tuple:
        card = Card(f"{row.title} ({row.path})", self.content)
        line = QHBoxLayout()
        icon = StatusIcon(row.state, 20, card)
        text = plain_label("", wrap=True, selectable=True, parent=card)
        line.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
        line.addWidget(text, 1)
        bar = QProgressBar(card)
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setFixedHeight(10)
        bar.setAccessibleName(f"{row.title} space used")
        card.addLayout(line)
        card.addWidget(bar)
        self.disks.addWidget(card)
        return card, icon, text, bar

    def render(self) -> None:
        result = self.store.get("storage")
        if result is None:
            return
        rows = vm.storage_rows(result.value if result.ok else [])
        self.loading.setVisible(not rows)
        self.loading.setText("Disk usage is not available." if result is not None else "Reading disk usage…")
        for row in rows:
            if row.key not in self.cards:
                self.cards[row.key] = self._card(row)
            _card, icon, text, bar = self.cards[row.key]
            icon.set_state(row.state)
            low = " Space is running low." if row.state == vm.WARNING else ""
            text.setText(row.text + low)
            bar.setValue(row.percent_used)
            bar.setAccessibleDescription(f"{row.percent_used}% used")
        notes = []
        if len(rows) == 1 and rows[0].key == "system":
            notes.append("Your home folder is on the system disk.")
        if self.store.value("live") is True:
            notes.append("In this live session, changes are kept in memory and lost when you shut down.")
        self.note.setText(" ".join(notes))
        self.note.setVisible(bool(notes))
