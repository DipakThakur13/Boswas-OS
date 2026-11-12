"""Hardware & Drivers: the devices in this computer, their drivers, missing firmware.

Devices are read from sysfs (identifiers, class, bound driver; never serial
numbers). "Check Firmware" runs boswas-sysops firmware-report, which reads
the kernel log for firmware the drivers asked for and could not load (no
password: it is read-only), and offers to install the Debian package that
provides a missing file (apt-install: administrator rights, confirmation).
No driver is ever downloaded from anywhere else.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import commands, hardware, jobs
from .. import viewmodel as vm
from .base import Page
from .common import SP, Card, StatusIcon, SummaryCard, icon, muted_label, plain_label, title_label
from .jobs import JobPanel


class HardwarePage(Page):
    data_keys = ("pci", "usb")

    def build(self) -> None:
        jobs_ = self.window_.jobs
        self.summary = SummaryCard(self.content)
        self.check_button = QPushButton("Check Firmware", self.summary)
        self.check_button.setIcon(icon("search", "edit-find", fallback=SP.SP_FileDialogContentsView))
        self.check_button.setAccessibleDescription("Reads the kernel log for firmware the drivers could not load")
        self.check_button.clicked.connect(self.check_firmware)
        self.summary.actions.addWidget(self.check_button)
        self.body.addWidget(self.summary)
        self.panel = JobPanel(self.info.key, jobs_, self.content)
        self.body.addWidget(self.panel)

        self.firmware = Card("Firmware", self.content)
        self.firmware_icon = StatusIcon(parent=self.firmware)
        head = QHBoxLayout()
        self.firmware_headline = title_label("Not checked yet", 1.0, self.firmware)
        head.addWidget(self.firmware_icon, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.firmware_headline, 1)
        self.firmware.addLayout(head)
        self.firmware_detail = muted_label("Choose Check Firmware to find firmware that drivers asked for and could "
                                           "not load since this computer started.", parent=self.firmware)
        self.firmware.addWidget(self.firmware_detail)
        self.firmware_rows = QVBoxLayout()
        self.firmware_rows.setSpacing(8)
        self.firmware.addLayout(self.firmware_rows)
        self.body.addWidget(self.firmware)
        self.firmware_widgets: list[QWidget] = []
        self.install_buttons: dict[str, QPushButton] = {}

        self.devices = Card("Devices", self.content)
        self.tree = QTreeWidget(self.devices)
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Device", "Type", "Driver", "Bus address"])
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setAccessibleName("Devices and their drivers")
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setMinimumHeight(280)
        self.devices_note = muted_label("", parent=self.devices)
        self.devices.addWidget(self.devices_note)
        self.devices.addWidget(self.tree)
        self.body.addWidget(self.devices)
        self.add_modules()
        jobs_.changed.connect(self._buttons)

    def _changed(self, key: str) -> None:
        if key == "firmware":
            self._render_firmware()
            self._buttons()
        else:
            super()._changed(key)

    def render(self) -> None:
        pci_result, usb_result = self.store.get("pci"), self.store.get("usb")
        pci = pci_result.value if pci_result is not None and pci_result.ok else []
        usb = usb_result.value if usb_result is not None and usb_result.ok else []
        if pci_result is None:
            self.summary.set_summary(vm.Summary(vm.CHECKING, "Reading devices…", ""))
        else:
            self.summary.set_summary(hardware.hardware_summary(pci, usb, pci_result.error).summary)
        self.tree.clear()
        groups = (("PCI devices", pci), ("USB devices", usb))
        for title, rows in groups:
            if not rows:
                continue
            group = QTreeWidgetItem([f"{title} ({len(rows)})", "", "", ""])
            group.setFirstColumnSpanned(True)
            self.tree.addTopLevelItem(group)
            for device in rows:
                item = QTreeWidgetItem([device.name, device.category, device.driver_text, device.address])
                item.setToolTip(0, f"{device.vendor} · {device.vendor_id}:{device.device_id}")
                item.setData(0, Qt.ItemDataRole.AccessibleTextRole, device.name)
                font = item.font(0)
                font.setBold(device.highlighted)
                item.setFont(0, font)
                group.addChild(item)
            group.setExpanded(True)
        names = ("" if self.store.value("pci") is None or any(d.vendor and not d.vendor.startswith("Vendor ")
                                                              for d in pci)
                 else "Device names are not available (the pci.ids database is not installed); IDs are shown. ")
        self.devices_note.setText(names + "Bold entries are network, graphics, storage and audio devices.")
        self._render_firmware()
        self._buttons()

    def _render_firmware(self) -> None:
        for widget in self.firmware_widgets:
            widget.deleteLater()
        self.firmware_widgets = []
        self.install_buttons = {}
        report = self.store.value("firmware")
        if report is None:
            return
        source, missing = report
        summary = hardware.firmware_summary(source, missing)
        self.firmware_icon.set_state(summary.state)
        self.firmware_headline.setText(summary.headline)
        self.firmware_detail.setText(summary.detail)
        for entry in missing:
            row = QWidget(self.firmware)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            text = QVBoxLayout()
            text.setSpacing(2)
            text.addWidget(plain_label(entry.file, selectable=True, parent=row))
            text.addWidget(muted_label(entry.explanation, parent=row))
            layout.addLayout(text, 1)
            if entry.installable:
                button = QPushButton(f"Install {entry.package}…", row)
                button.setIcon(icon("download", "system-software-install", fallback=SP.SP_ArrowDown))
                button.setAccessibleDescription(f"Installs the Debian package {entry.package} (administrator rights)")
                button.clicked.connect(lambda _checked=False, p=entry.package: self.install(p))
                layout.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
                self.install_buttons[entry.package] = button
            self.firmware_rows.addWidget(row)
            self.firmware_widgets.append(row)

    def _buttons(self) -> None:
        jobs_ = self.window_.jobs
        busy = jobs_.busy()
        available = (self.store.value("programs") or {}).get("admin", True)
        reason = jobs_.busy_reason() if busy else ("" if available else
                                                   commands.NO_ADMIN)
        self.check_button.setEnabled(not reason)
        self.check_button.setToolTip(reason or self.check_button.accessibleDescription())
        for button in self.install_buttons.values():
            button.setEnabled(not reason)
            button.setToolTip(reason or button.accessibleDescription())

    # --- actions ---
    def check_firmware(self) -> None:
        # Read-only and allowed for the active user without a password (polkit allow_active=yes).
        self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("firmware-report",), "Checking for missing firmware",
                                             self.info.key), self._firmware_done)

    def _firmware_done(self, outcome: jobs.Outcome) -> None:
        if outcome.ok:
            self.store.put("firmware", hardware.firmware_report(outcome.data))

    def install(self, package: str) -> None:
        if not self.window_.ask(f"Install {package}?",
                                f"The firmware package {package} is installed from Debian's signed repository. "
                                "The device may need a restart (or reconnecting) to use the firmware. You are asked "
                                "for an administrator password.", "Install"):
            return
        self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("apt-install", package), f"Installing {package}",
                                             self.info.key), lambda outcome, p=package: self._installed(p, outcome))

    def _installed(self, package: str, outcome: jobs.Outcome) -> None:
        if not outcome.ok:
            return
        report = self.store.value("firmware")
        if report is not None:
            source, missing = report
            self.store.put("firmware", (source, [m for m in missing if m.package != package]))
        self.window_.show_status(f"{package} was installed. Restart the computer (or reconnect the device) so the "
                                 "driver loads the firmware, then check again.")
