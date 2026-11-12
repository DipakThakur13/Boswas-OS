"""Network: the connections right now, the Wi-Fi radio, connect and disconnect.

Read from NetworkManager with nmcli (devices, state, addresses). Switching
the Wi-Fi radio and connecting or disconnecting a device go through nmcli
too, and NetworkManager's own polkit rules decide whether the user may.
Choosing a new Wi-Fi network and entering its password stay in KDE's network
module: Control Center never handles Wi-Fi secrets.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .. import sysinfo
from .base import Page
from .common import SP, Card, MessageBanner, StatusIcon, SummaryCard, icon, muted_label, plain_label, title_label
from .. import viewmodel as vm


class NetworkPage(Page):
    data_keys = ("network",)

    def build(self) -> None:
        self.summary = SummaryCard(self.content)
        self.wifi_button = QPushButton("Turn Wi-Fi Off", self.summary)
        self.wifi_button.setIcon(icon("network-wireless", fallback=SP.SP_DriveNetIcon))
        self.wifi_button.setAccessibleDescription("Switches the Wi-Fi radio on or off")
        self.wifi_button.clicked.connect(self.toggle_wifi)
        self.hardware_button = QPushButton("Check Hardware & Drivers", self.summary)
        self.hardware_button.setIcon(icon("preferences-devices-tree", "hwinfo", fallback=SP.SP_ComputerIcon))
        self.hardware_button.setAccessibleDescription("Opens Hardware & Drivers to check for missing firmware")
        self.hardware_button.clicked.connect(lambda: self.window_.show_page("hardware"))
        self.summary.actions.addWidget(self.wifi_button)
        self.summary.actions.addWidget(self.hardware_button)
        self.body.addWidget(self.summary)
        self.message = MessageBanner(parent=self.content)
        self.body.addWidget(self.message)
        self.connections = Card("Connections", self.content)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(10)
        self.connections.addLayout(self.rows)
        self.empty = muted_label("", parent=self.connections)
        self.connections.addWidget(self.empty)
        self.body.addWidget(self.connections)
        self.add_modules()
        self.row_widgets: list[QWidget] = []
        self.device_buttons: dict[str, QPushButton] = {}
        self.busy = False

    def render(self) -> None:
        result = self.store.get("network")
        state = result.value if result is not None and result.ok else None
        self.summary.set_summary(sysinfo.network_summary(state, result.error if result is not None else None,
                                                         bool(result is not None and result.unavailable)))
        for widget in self.row_widgets:
            widget.deleteLater()
        self.row_widgets = []
        self.device_buttons = {}
        devices = state.devices if state is not None else ()
        for device in devices:
            self._row(device)
        self.empty.setText("" if devices else ("Reading the network status…" if result is None else
                                                "No network devices are available."))
        self.empty.setVisible(not devices)
        wifi = state.wifi_enabled if state is not None else None
        has_wifi = any(d.kind == "wifi" for d in devices)
        self.wifi_button.setText("Turn Wi-Fi Off" if wifi else "Turn Wi-Fi On")
        self.wifi_button.setVisible(wifi is not None and (has_wifi or wifi is False))
        self.wifi_button.setEnabled(not self.busy)
        self.hardware_button.setVisible(any(d.unavailable for d in devices))

    def _row(self, device: sysinfo.NetDevice) -> None:
        row = QWidget(self.connections)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        state = vm.SECURE if device.connected else (vm.WARNING if device.unavailable else vm.INFO)
        layout.addWidget(StatusIcon(state, 20, row), 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        title = f"{device.kind_label}: {device.device}"
        text.addWidget(title_label(title, 1.0, row))
        detail = device.state_label + (f" · {device.connection}" if device.connection else "")
        text.addWidget(muted_label(detail, parent=row))
        if device.addresses:
            text.addWidget(plain_label("Addresses: " + ", ".join(device.addresses)
                                       + (f" · gateway {device.gateway}" if device.gateway else ""),
                                       wrap=True, selectable=True, parent=row))
        if device.unavailable:
            text.addWidget(muted_label("Unavailable: no cable or signal, switched off, or missing firmware or "
                                       "driver.", parent=row))
        layout.addLayout(text, 1)
        if device.kind in ("ethernet", "wifi") and not device.unavailable and device.state != "unmanaged":
            button = QPushButton("Disconnect" if device.connected else "Connect", row)
            button.setAccessibleName(f"{'Disconnect' if device.connected else 'Connect'} {device.device}")
            button.setAccessibleDescription("Uses NetworkManager; choosing a new Wi-Fi network is done in Wi-Fi & "
                                            "Networking")
            button.clicked.connect(lambda _c=False, d=device: self.toggle_device(d))
            button.setEnabled(not self.busy)
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
            self.device_buttons[device.device] = button
        self.rows.addWidget(row)
        self.row_widgets.append(row)

    # --- actions ---
    def _run(self, fn, *args, done: str, failed: str) -> None:
        self.busy = True
        self.render()
        self.window_.runner.submit(fn, *args, on_done=lambda _r: self._finished(done, None),
                                   on_error=lambda exc: self._finished(failed, exc))

    def _finished(self, text: str, exc) -> None:
        self.busy = False
        if exc is None:
            self.message.show_message("ok", text)
        else:
            self.message.show_message("error", f"{text} {self.window_.error_text(exc)}")
        self.store.request("network", force=True)
        self.render()

    def toggle_wifi(self) -> None:
        state = self.store.value("network")
        if state is None or state.wifi_enabled is None:
            return
        enable = not state.wifi_enabled
        text = ("Wi-Fi is switched on. Known networks connect automatically." if enable else
                "Wi-Fi is switched off and wireless connections end.")
        if not self.window_.ask("Turn Wi-Fi " + ("on?" if enable else "off?"), text,
                                "Turn On" if enable else "Turn Off"):
            return
        self._run(self.window_.backend.set_wifi, enable, done="Wi-Fi is " + ("on." if enable else "off."),
                  failed="Wi-Fi could not be switched.")

    def toggle_device(self, device: sysinfo.NetDevice) -> None:
        connect = not device.connected
        text = (f"{device.device} connects using its saved settings." if connect else
                f"{device.device} disconnects" + (f" from {device.connection}" if device.connection else "") +
                " until you connect it again.")
        if not self.window_.ask(("Connect " if connect else "Disconnect ") + f"{device.device}?", text,
                                "Connect" if connect else "Disconnect"):
            return
        self._run(self.window_.backend.device_connection, device.device, connect,
                  done=f"{device.device} {'connected' if connect else 'disconnected'}.",
                  failed=f"{device.device} could not be {'connected' if connect else 'disconnected'}.")
