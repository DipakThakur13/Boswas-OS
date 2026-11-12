"""Hardware pages: the plain settings pages, Power and Storage.

Bluetooth, Display, Sound, Users and Devices are configured entirely in
KDE's modules, so those pages explain what is where and open the modules.
Power adds the battery state from /sys/class/power_supply. Storage shows the
usage of / and /home, the disks and file systems (lsblk, without serial
numbers) and three ways to free space, each confirmed first: the package
cache and the system journal (boswas-sysops, administrator rights) and the
user's thumbnail cache. Partitioning stays in KDE Partition Manager.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .. import commands, jobs, sysinfo
from .. import viewmodel as vm
from .base import Page
from .common import SP, Card, MessageBanner, StatusIcon, icon, muted_label, plain_label, title_label
from .jobs import JobPanel


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
    data_keys = ("storage", "live", "disks", "thumbnails")

    def build(self) -> None:
        self.disks = QVBoxLayout()
        self.disks.setSpacing(12)
        self.body.addLayout(self.disks)
        self.loading = muted_label("Reading disk usage…", parent=self.content)
        self.body.addWidget(self.loading)
        self.note = muted_label("", parent=self.content)
        self.note.hide()
        self.body.addWidget(self.note)

        self.devices = Card("Disks and file systems", self.content)
        self.devices_text = plain_label("", wrap=True, selectable=True, parent=self.devices)
        self.devices_text.setAccessibleName("Disks and file systems")
        self.devices.addWidget(self.devices_text)
        self.devices.addWidget(muted_label("Formatting and partitioning are done in KDE Partition Manager (below), "
                                           "never from here.", parent=self.devices))
        self.body.addWidget(self.devices)

        self.cleanup = Card("Free up space", self.content)
        self.cleanup_message = MessageBanner(parent=self.cleanup)
        self.cleanup.addWidget(self.cleanup_message)
        self.apt_button = self._cleanup_row("Downloaded package files",
                                            "Software packages kept after installation. They are downloaded "
                                            "again when needed.", "Remove Package Files…", self.clean_packages)
        self.journal_button = self._cleanup_row("Old system logs",
                                                "Keeps the newest 200 MB of the system journal and deletes older "
                                                "entries.", "Shrink System Logs…", self.clean_journal)
        self.thumbs_button = self._cleanup_row("Thumbnail cache",
                                               "Preview images of your files. They are created again when needed.",
                                               "Clear Thumbnails…", self.clean_thumbnails)
        self.thumbs_text = muted_label("", parent=self.cleanup)
        self.cleanup.addWidget(self.thumbs_text)
        self.panel = JobPanel(self.info.key, self.window_.jobs, self.content)
        self.body.addWidget(self.cleanup)
        self.body.addWidget(self.panel)
        self.add_modules()
        self.cards: dict[str, tuple] = {}
        self.cleaning = False
        self.window_.jobs.changed.connect(self._buttons)

    def _cleanup_row(self, title: str, description: str, label: str, slot) -> QPushButton:
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(title_label(title, 1.0, self.cleanup))
        text.addWidget(muted_label(description, parent=self.cleanup))
        button = QPushButton(label, self.cleanup)
        button.setIcon(icon("edit-clear", "edit-delete", fallback=SP.SP_TrashIcon))
        button.setAccessibleDescription(description)
        button.clicked.connect(slot)
        row.addLayout(text, 1)
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.cleanup.addLayout(row)
        return button

    def _buttons(self) -> None:
        jobs_ = self.window_.jobs
        reason = jobs_.busy_reason() if jobs_.busy() else ""
        admin = (self.store.value("programs") or {}).get("admin", True)
        for button in (self.apt_button, self.journal_button):
            why = reason or ("" if admin else commands.NO_ADMIN)
            button.setEnabled(not why)
            button.setToolTip(why or button.accessibleDescription())
        thumbs = self.store.value("thumbnails")
        why = "Clearing…" if self.cleaning else ("The thumbnail cache is empty." if thumbs is not None and not thumbs[1]
                                                 else "")
        self.thumbs_button.setEnabled(not why)
        self.thumbs_button.setToolTip(why or self.thumbs_button.accessibleDescription())

    def _render_devices(self) -> None:
        result = self.store.get("disks")
        if result is None:
            self.devices_text.setText("Reading disks…")
            return
        if not result.ok:
            self.devices_text.setText(f"The disks could not be listed: {result.error}")
            return
        lines = []
        for disk in result.value:
            size = vm.format_bytes(disk.size) if disk.size is not None else "unknown size"
            lines.append(f"{disk.title} ({disk.name}, {size}{', read-only' if disk.read_only else ''})")
            for volume in disk.volumes:
                lines.append("    " * volume.depth + sysinfo.volume_text(volume))
        self.devices_text.setText("\n".join(lines) or "No disks found.")

    # --- cleanup ---
    def clean_packages(self) -> None:
        if self.window_.ask("Remove downloaded package files?",
                            "The package files APT kept in /var/cache/apt/archives are deleted. Installed software "
                            "is not affected. You are asked for an administrator password.", "Remove"):
            self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("apt-clean",), "Removing downloaded package files",
                                                 self.info.key), self._cleaned)

    def clean_journal(self) -> None:
        if self.window_.ask("Shrink the system logs?",
                            "Older entries of the system journal are deleted until it uses at most 200 MB. Recent "
                            "logs are kept. You are asked for an administrator password.", "Shrink Logs"):
            self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("journal-vacuum",), "Shrinking the system logs",
                                                 self.info.key), self._cleaned)

    def _cleaned(self, outcome: jobs.Outcome) -> None:
        freed = outcome.data.get("freed_bytes") if outcome.ok else None
        if isinstance(freed, int) and not isinstance(freed, bool) and freed >= 0:
            self.cleanup_message.show_message("ok", f"{vm.format_bytes(freed)} freed.")
        self.store.request("storage", force=True)

    def clean_thumbnails(self) -> None:
        thumbs = self.store.value("thumbnails")
        size = f" ({thumbs[0]} files, {vm.format_bytes(thumbs[1])})" if thumbs else ""
        if not self.window_.ask("Clear the thumbnail cache?",
                                f"The preview images in ~/.cache/thumbnails{size} are deleted. Your files are not "
                                "affected.", "Clear"):
            return
        self.cleaning = True
        self._buttons()
        self.window_.runner.submit(self.window_.backend.clean_thumbnails,
                                   on_done=self._thumbnails_cleared, on_error=self._thumbnails_failed)

    def _thumbnails_cleared(self, freed: int) -> None:
        self.cleaning = False
        self.cleanup_message.show_message("ok", f"Thumbnail cache cleared: {vm.format_bytes(freed)} freed.")
        self.store.request("thumbnails", force=True)
        self.store.request("storage", force=True)

    def _thumbnails_failed(self, exc) -> None:
        self.cleaning = False
        self.cleanup_message.show_message("error", "The thumbnail cache could not be cleared. "
                                                   f"{self.window_.error_text(exc)}")
        self._buttons()

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
            self._render_devices()
            self._buttons()
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
        self._render_devices()
        thumbs = self.store.value("thumbnails")
        self.thumbs_text.setText(f"Thumbnail cache: {thumbs[0]} files, {vm.format_bytes(thumbs[1])}."
                                 if thumbs is not None else "")
        self._buttons()
