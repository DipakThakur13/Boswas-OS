"""Bluetooth & devices: printers, cameras and USB devices.

Page "printers":
  Printers      with CUPS installed: the printers (`lpstat -e`, `lpstat -p
                -d`), their state and the default, and "Print a test page"
                (CUPS's own test page, `lp -d PRINTER`, after a question);
                a link to CUPS's own printer settings in the browser. Without
                CUPS: "Install printing support", which installs the package
                cups through the system helper (sysops apt-install
                --no-recommends: cups-daemon recommends cups-browsed, which
                Boswas policy excludes; with the usual question and an
                administrator password)
Page "cameras":
  Cameras       the cameras of /sys/class/video4linux (peripherals.cameras);
                the page's program row opens the Camera application
Page "usb":
  USB devices   the connected USB devices as the Hardware page reads them
                (no serial numbers); a drive on one has "Open in Files"
                (boswas-files at its mounted folder, else at This PC) and
                "Eject" (udisksctl unmount, lock and power-off, the steps
                Files takes through UDisks2; polkit decides), unless the
                system uses it (peripherals.usb_drives)

Reading happens on a worker thread when the page is shown; nothing is
changed without a click and a question.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ... import commands, jobs, peripherals, probes
from ...errors import CommandFailed, CommandUnavailable, Failed, user_message
from ..jobs import JobPanel
from . import context
from .system import READ_TIMEOUT, ToolSection, tool_failure
from .widgets import RowList, ask, window_of

PRINT_TIMEOUT = 60.0
EJECT_TIMEOUT = 300.0          # each step may wait for polkit's question, and for the data to be written
CUPS_WEB = "http://localhost:631/"
USB_ICONS = {"Input": "input-keyboard-symbolic", "Storage": "drive-removable-media-symbolic",
             "Audio": "audio-headphones-symbolic", "Imaging": "camera-photo-symbolic", "Printer": "document-print-symbolic",
             "Video": "camera-video-symbolic", "Communication": "network-wired-symbolic",
             "Wireless": "bluetooth-active-symbolic", "Hub": "view-grid-symbolic"}


class PrintersSection(ToolSection):
    page = "printers"
    title = "Printers"
    order = 10
    keywords = ("printer", "printers", "print", "test page", "default printer", "cups", "install printer")

    def __init__(self, parent: QWidget | None = None, *, runner=None, root: str | None = None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.root = context.root if root is None else root
        self.install_button = fluent.Button("Install Printing Support…", self, accent=True)
        self.install_button.setAccessibleName("Install printing support")
        self.install_button.clicked.connect(self.install)
        self.missing = self.add(fluent.SettingsCard(
            "Printing support is not installed", "Boswas OS prints through CUPS. Installing it adds the package "
            "cups from Debian's signed repositories.", fluent.icon("document-print-symbolic"), self.install_button,
            self))
        self.panel_holder = self.add(QWidget(self))
        self.panel_holder.hide()
        self.list = self.add(RowList(self))
        self.empty = self.add(fluent.SettingsCard(
            "No printer was found", "Printers on your network that support driverless printing appear here by "
            "themselves. Others are added in CUPS's printer settings.", fluent.icon("document-print-symbolic"), None,
            self))
        self.web_button = fluent.Button("Open", self)
        self.web_button.setAccessibleName("Open CUPS's printer settings in the browser")
        self.web_button.clicked.connect(lambda: fluent.open_url(CUPS_WEB))
        self.web = self.add(fluent.SettingsCard(
            "Printer settings in the browser", f"CUPS's own pages ({CUPS_WEB}): add printers, set options, see "
            "print jobs", fluent.icon("globe-symbolic"), self.web_button, self))
        for widget in (self.missing, self.list, self.empty, self.web):
            widget.hide()
        self.printers: list[peripherals.Printer] = []
        self.installed: bool | None = None
        self.panel: JobPanel | None = None

    def activate(self) -> None:
        window = window_of(self)
        if window is not None and self.panel is None:
            self.panel = JobPanel(self.page, window.jobs, self.panel_holder)
            layout = QVBoxLayout(self.panel_holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.panel)
            self.panel_holder.show()
            window.jobs.changed.connect(self._jobs_changed)
        super().activate()

    def read(self):
        installed = peripherals.cups_installed(probes.Root(self.root))
        if not installed:
            return False, [], ""
        if not self.runner.available(commands.EXECUTABLES["lpstat"]):
            return True, [], "lpstat (package cups-client) is not installed"
        listed = self.runner.run(commands.argv("lpstat", "-e"), READ_TIMEOUT)
        status = self.runner.run(commands.argv("lpstat", "-p", "-d"), READ_TIMEOUT)
        problem = ""
        if listed.code != 0 and status.code != 0:
            problem = " ".join((status.stderr or listed.stderr or "").split())[:200] or "lpstat failed."
        names = peripherals.parse_destinations(listed.stdout) if listed.code == 0 else []
        return True, peripherals.parse_printers(status.stdout if status.code == 0 else "", names), problem

    def show_state(self, state) -> None:
        installed, printers, problem = state
        self.installed, self.printers = installed, printers
        self.missing.setVisible(not installed)
        self.web.setVisible(bool(installed))
        self.list.sync([p.name for p in printers], self._row, self._update_row)
        self.empty.setVisible(bool(installed) and not printers)
        if problem:
            self.show_notice(f"The printing service did not answer ({problem}). It may still be starting.",
                             "warning")
        else:
            self.show_notice("")
        self._jobs_changed()

    def _row(self, name: str) -> fluent.SettingsCard:
        button = fluent.Button("Print a test page", self.list)
        button.setAccessibleName(f"Print a test page on {name}")
        button.clicked.connect(lambda _c=False, n=name: self.test_page(n))
        return fluent.SettingsCard(name, "", fluent.icon("printer", "document-print-symbolic"), button, self.list)

    def _update_row(self, name: str, card: fluent.SettingsCard) -> None:
        printer = next(p for p in self.printers if p.name == name)
        card.set_description(printer.state + (" · Default printer" if printer.default else ""))
        card.control().setEnabled(printer.enabled and not self.busy and
                                  self.runner.available(commands.EXECUTABLES["lp"]))
        card.control().setToolTip("" if printer.enabled else "This printer is stopped; start it again in the "
                                                                "printer settings in the browser.")

    def set_enabled(self, enabled: bool) -> None:
        for name in self.list.keys():
            row = self.list.row(name)
            if row is not None:
                self._update_row(name, row)
        self._jobs_changed()

    def _jobs_changed(self) -> None:
        window = window_of(self)
        admin = self.runner.available(commands.EXECUTABLES[commands.ADMIN_TOOL])
        busy = window is not None and window.jobs.busy()
        why = (window.jobs.busy_reason() if busy else "") or ("" if admin else commands.NO_ADMIN) or \
            ("" if window is not None else "Not available here.")
        self.install_button.setEnabled(not why and not self.busy)
        self.install_button.setToolTip(why or "Installs the package cups (administrator password)")

    def install(self) -> None:
        window = window_of(self)
        if window is None or not ask(self, "Install printing support?",
                                     "The package cups (the printing system CUPS) and what it needs are installed "
                                     "from Debian's signed repositories, without the network printer browser "
                                     "cups-browsed, which Boswas OS leaves out. You are asked for an administrator "
                                     "password.", "Install"):
            return
        window.jobs.start(jobs.JobSpec(jobs.SYSOPS, peripherals.PRINTING_INSTALL,
                                       "Installing printing support", self.page), lambda _o: self.reload())

    def test_page(self, name: str) -> None:
        if not ask(self, f"Print a test page on {name}?", "CUPS's test page is sent to this printer.", "Print"):
            return

        def apply() -> None:
            result = self.runner.run(commands.lp_test_page_argv(name), PRINT_TIMEOUT)
            if result.code != 0:
                raise tool_failure("Printing the test page", result)

        self.change(apply, f"The test page was sent to {name}.")


class CamerasSection(ToolSection):
    page = "cameras"
    title = "Connected cameras"
    order = 10
    keywords = ("camera", "webcam", "video", "connected cameras")

    def __init__(self, parent: QWidget | None = None, *, root: str | None = None):
        super().__init__(parent)
        self.root = context.root if root is None else root
        self.list = self.add(RowList(self))
        self.empty = self.add(fluent.SettingsCard(
            "No camera was found", "Connect a camera; a USB camera appears here as soon as it is plugged in.",
            fluent.icon("camera-web", "camera-photo-symbolic"), None, self))
        self.empty.hide()
        self.cameras: list[peripherals.Camera] = []

    def read(self):
        return peripherals.cameras(probes.Root(self.root))

    def show_state(self, cameras) -> None:
        self.cameras = cameras
        by_node = {camera.node: camera for camera in cameras}
        self.list.sync(list(by_node), lambda node: fluent.SettingsCard("", "", fluent.icon("camera-web"), None,
                                                                       self.list),
                       lambda node, card: (card.set_title(by_node[node].name),
                                           card.set_description(f"{by_node[node].bus} camera · /dev/{node}")))
        self.empty.setVisible(not cameras)


class UsbSection(ToolSection):
    page = "usb"
    title = "USB devices"
    order = 10
    keywords = ("usb", "connected devices", "usb stick", "usb drive", "plugged in", "eject", "safely remove")

    def __init__(self, parent: QWidget | None = None, *, root: str | None = None, runner=None):
        super().__init__(parent)
        self.root = context.root if root is None else root
        self.runner = runner or context.runner()
        self.list = self.add(RowList(self))
        self.empty = self.add(fluent.SettingsCard("No USB device is connected", "", fluent.icon(
            "drive-removable-media-symbolic"), None, self))
        self.empty.hide()
        self.add(fluent.Label("Eject a drive before you unplug it, so that everything is written to it.",
                              "caption", self, tone="secondary", wrap=True))
        self.devices: list = []
        self.drives: dict[str, list[peripherals.Drive]] = {}

    def read(self):
        root = probes.Root(self.root)
        drives: dict[str, list[peripherals.Drive]] = {}
        for drive in peripherals.usb_drives(root):
            drives.setdefault(drive.usb, []).append(drive)
        return peripherals.usb(root), drives

    def show_state(self, state) -> None:
        devices, self.drives = state
        self.devices = devices
        by_address = {device.address: device for device in devices}

        def create(address: str) -> fluent.SettingsCard:
            card = fluent.SettingsCard("", "", None, None, self.list)
            card.open_button = fluent.Button("Open in Files", card)
            card.open_button.clicked.connect(lambda _c=False, a=address: self.open_in_files(a))
            card.eject_button = fluent.Button("Eject", card, icon=fluent.icon("media-eject-symbolic"))
            card.eject_button.clicked.connect(lambda _c=False, a=address: self.eject(a))
            card.set_control(card.open_button)
            card.add_control(card.eject_button)
            return card

        def update(address: str, card: fluent.SettingsCard) -> None:
            device = by_address[address]
            drives = self.drives.get(address, [])
            card.set_title(device.name)
            parts = [device.vendor, "Storage" if drives and device.category != "Storage" else device.category,
                     f"driver {device.driver}" if device.driver else "no driver in use"]
            for drive in drives:
                parts.append(", ".join(point for _device, point in drive.mounts) if drive.mounts else "not opened")
                if drive.in_use_by_system:
                    parts.append("in use by the system")
            card.set_description(" · ".join(part for part in parts if part))
            card.set_icon(fluent.icon("drive-removable-media-symbolic" if drives else
                                      USB_ICONS.get(device.category, "drive-removable-media-symbolic")))
            card.open_button.setVisible(bool(drives))
            card.eject_button.setVisible(bool(drives))
            card.open_button.setAccessibleName(f"Open {device.name} in Files")
            card.eject_button.setAccessibleName(f"Eject {device.name}")
            card.open_button.setEnabled(not self.busy and self.runner.available(commands.EXECUTABLES["boswas-files"]))
            ejectable = bool(drives) and not any(drive.in_use_by_system for drive in drives)
            card.eject_button.setEnabled(not self.busy and ejectable and
                                         self.runner.available(commands.EXECUTABLES["udisksctl"]))
            card.eject_button.setToolTip("" if ejectable else "The system uses this drive; it cannot be ejected here.")

        self.list.sync(list(by_address), create, update)
        self.empty.setVisible(not devices)

    def set_enabled(self, enabled: bool) -> None:
        if self.devices:
            self.show_state((self.devices, self.drives))         # the buttons follow self.busy

    def _name(self, address: str) -> str:
        return next((device.name for device in self.devices if device.address == address), "The drive")

    def open_in_files(self, address: str) -> None:
        drives = self.drives.get(address, [])
        if not drives:
            return
        try:
            self.runner.start(commands.files_argv(drives[0].folder))
        except (CommandFailed, CommandUnavailable, ValueError) as exc:
            self.show_error(f"Files could not be opened. {user_message(exc)}")

    def eject(self, address: str) -> None:
        drives = self.drives.get(address, [])
        if not drives or any(drive.in_use_by_system for drive in drives):
            return
        name = self._name(address)
        steps = [commands.udisksctl_argv(action, device) for drive in drives
                 for action, device in peripherals.eject_steps(drive)]

        def apply() -> None:
            for command in steps:
                result = self.runner.run(command, EJECT_TIMEOUT)
                if result.code != 0:
                    detail = " ".join((result.stderr or result.stdout or "").split()).casefold()
                    if "busy" in detail or "target is busy" in detail:
                        raise Failed(f"{name} is in use. Close the files and folders on it, then eject it again.")
                    raise tool_failure(f"Ejecting {name}", result)

        self.change(apply, f"{name} can be removed now.")


SECTIONS = (PrintersSection, CamerasSection, UsbSection)
