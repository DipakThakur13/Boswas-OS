"""Network and Bluetooth settings.

Page "network":
  Wi-Fi               the master switch, the connected network, the visible
                      networks (strongest first) with Connect, Forget and a
                      passphrase dialog, and Scan
  Other connections   Ethernet (connected or not), VPN (status only) and the
                      airplane mode switch
  Connection details  per interface: addresses, gateways, DNS, MAC, link
                      speed (ip -j, /etc/resolv.conf, /sys/class/net), with Copy
  Proxy               the proxy variables of this session, read-only
Page "bluetooth":
  Bluetooth           the master switch, Add device (discovery), paired
                      devices with Connect / Disconnect / Remove, and the
                      devices found with Pair

Wi-Fi, the other connections and Bluetooth show the services bridge's state
(ui/bridgeclient.py) and change things by its commands, so they always agree
with the shell's quick settings. A Wi-Fi passphrase goes from the password
box into the command for the bridge and nowhere else; the box is emptied
when the dialog closes.
"""

from __future__ import annotations

import time

import boswas_fluent as fluent
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QGridLayout, QVBoxLayout, QWidget

from ... import commands, netinfo
from ... import servicebridge as sb
from ...errors import CommandUnavailable, user_message
from .. import bridgeclient
from . import context
from .widgets import BridgeSection, Busy, ElidedLabel, RowList, Section, set_checked

CONNECT_SECONDS = 50.0
STATUS_HEIGHT = 52             # the passphrase dialog's line for "Connecting…" or an error
BLUETOOTH_SECONDS = 45.0
SIGNAL_WORDS = {"excellent": "Excellent signal", "good": "Good signal", "ok": "Fair signal", "weak": "Weak signal",
                "none": "Very weak signal"}
# BlueZ's icon names -> icons of Boswas Prism.
BLUETOOTH_ICONS = {
    "audio-headphones": "audio-headphones-symbolic", "audio-headset": "audio-headphones-symbolic",
    "audio-card": "audio-volume-high-symbolic", "input-keyboard": "input-keyboard-symbolic",
    "input-mouse": "input-mouse-symbolic", "input-tablet": "input-mouse-symbolic", "phone": "phone",
    "computer": "computer", "camera-photo": "camera-photo-symbolic", "camera-video": "camera-video-symbolic",
    "printer": "printer", "input-gaming": "input-mouse-symbolic",
}
BLUETOOTH_KINDS = {
    "audio-headphones": "Headphones", "audio-headset": "Headset", "audio-card": "Audio device",
    "input-keyboard": "Keyboard", "input-mouse": "Mouse", "input-tablet": "Tablet", "phone": "Phone",
    "computer": "Computer", "camera-photo": "Camera", "camera-video": "Camera", "printer": "Printer",
    "input-gaming": "Game controller",
}


def signal_icon(strength: int):
    return fluent.icon(f"network-wireless-signal-{sb.signal_bars(strength)}-symbolic")


class PassphraseDialog(fluent.ContentDialog):
    """Asks for the password of a Wi-Fi network and stays open while the connection is tried.

    ``submitted(passphrase)`` is emitted for Connect (or Enter). The owner
    then calls ``fail(text)`` to show why it did not work and let the person
    try again, or closes the dialog. The password box is emptied whenever
    the dialog closes.
    """

    submitted = Signal(str)

    def __init__(self, parent: QWidget, ssid: str):
        super().__init__(parent, f"Connect to {ssid}", "Enter the password of this Wi-Fi network.",
                         primary="Connect", close="Cancel")
        self.ssid = ssid
        self.box = fluent.PasswordBox(self, placeholder="Password")
        self.box.setAccessibleName(f"Password for {ssid}")
        self.box.setMaxLength(sb.MAX_SECRET_BYTES)
        # The dialog is placed when it opens: the room for "Connecting…" or the error is there from the start.
        self.status = QWidget(self)
        self.status.setMinimumHeight(STATUS_HEIGHT)
        status = QVBoxLayout(self.status)
        status.setContentsMargins(0, 0, 0, 0)
        self.problem = fluent.InfoBar("error", parent=self.status, closable=False)
        self.problem.hide()
        self.busy = Busy("Connecting…", self.status)
        status.addWidget(self.problem)
        status.addWidget(self.busy)
        status.addStretch(1)
        self.content_layout.addWidget(self.box)
        self.content_layout.addWidget(self.status)
        # Connect must not close the dialog: it stays until the network accepts the password.
        self.primary_button.clicked.disconnect()
        self.primary_button.clicked.connect(self.submit)
        self.primary_button.setEnabled(False)
        self.box.textChanged.connect(self._sync)
        self.trying = False

    def _sync(self) -> None:
        self.primary_button.setEnabled(bool(self.box.text()) and not self.trying)

    def open(self) -> None:
        super().open()
        self.box.setFocus(Qt.FocusReason.PopupFocusReason)

    def submit(self) -> None:
        secret = self.box.text()
        if self.trying or not secret:
            return
        if len(secret.encode("utf-8")) > sb.MAX_SECRET_BYTES:
            self.fail("This password is too long.")
            return
        self.trying = True
        self.problem.hide()
        # Read-only, not disabled: the focus stays in the box, so a second Enter does not land on Cancel.
        self.box.setReadOnly(True)
        self.busy.set_busy(True)
        self._sync()
        self.submitted.emit(secret)

    def fail(self, text: str) -> None:
        """The attempt did not work: say why and let the person correct the password."""
        self.trying = False
        self.busy.set_busy(False)
        self.problem.show_message("error", text)
        self.box.setReadOnly(False)
        self.box.setFocus(Qt.FocusReason.OtherFocusReason)
        self.box.selectAll()
        self._sync()

    def done(self, result) -> None:
        self.box.clear()
        self.box.setReadOnly(False)
        self.trying = False
        self.busy.set_busy(False)
        super().done(result)


class NetworkRow(fluent.SettingsCard):
    """One visible Wi-Fi network."""

    connect_requested = Signal(str)
    forget_requested = Signal(str)

    def __init__(self, ssid: str, parent: QWidget | None = None):
        super().__init__(ssid, "", None, None, parent)
        self.ssid = ssid
        self.point: sb.AccessPoint | None = None
        self.connect_button = fluent.Button("Connect", self)
        self.connect_button.setAccessibleName(f"Connect to {ssid}")
        self.connect_button.clicked.connect(lambda: self.connect_requested.emit(self.ssid))
        self.set_control(self.connect_button)
        self.forget_button = fluent.Button("Forget", self, subtle=True)
        self.forget_button.setAccessibleName(f"Forget {ssid}")
        self.forget_button.setToolTip("Remove the saved password and settings of this network")
        self.forget_button.clicked.connect(lambda: self.forget_requested.emit(self.ssid))
        self.add_control(self.forget_button)
        self.busy = Busy("Connecting…", self)
        self.add_control(self.busy)
        self.lock = fluent.IconWidget(fluent.icon("changes-prevent-symbolic"), 16, self)
        self.lock.setToolTip("Secured: a password is needed")
        self.lock.setAccessibleName("Secured")
        self.add_control(self.lock)

    def update_point(self, point: sb.AccessPoint, enabled: bool, connecting: bool) -> None:
        self.point = point
        self.set_icon(signal_icon(point.strength))
        words = [SIGNAL_WORDS[sb.signal_bars(point.strength)], "Secured" if point.secured else "Open"]
        if point.known:
            words.append("Saved")
        self.set_description(" · ".join(words))
        self.lock.setVisible(point.secured)
        self.forget_button.setVisible(point.known)
        self.busy.set_busy(connecting)
        self.connect_button.setVisible(not connecting)
        self.connect_button.setEnabled(enabled)
        self.forget_button.setEnabled(enabled and not connecting)


class WifiSection(BridgeSection):
    page = "network"
    title = "Wi-Fi"
    order = 10
    keywords = ("wifi", "wi-fi", "wlan", "wireless", "network", "password", "ssid", "hotspot", "scan", "internet")
    service = sb.NETWORK
    missing = "NetworkManager is not running, so Wi-Fi cannot be shown or changed."

    def __init__(self, parent: QWidget | None = None, *, bridge=None):
        super().__init__(parent, bridge=bridge)
        self.toggle = fluent.ToggleSwitch()
        self.master = self.add(fluent.SettingsCard("Wi-Fi", "", fluent.icon("network-wireless-symbolic"),
                                                   self.toggle, self))
        self.toggle.toggled.connect(self._toggled)
        self.disconnect_button = fluent.Button("Disconnect", self)
        self.current = self.add(fluent.SettingsCard("", "", None, self.disconnect_button, self))
        self.disconnect_button.clicked.connect(self._disconnect)
        self.scan_button = fluent.Button("Scan", self, icon=fluent.icon("view-refresh-symbolic"))
        self.scan_button.setToolTip("Look for Wi-Fi networks again")
        self.scan_button.clicked.connect(self._scan)
        self.header = self.add(fluent.SettingsCard("Available networks", "", None, self.scan_button, self))
        self.scanning = Busy("Scanning…", self)
        self.header.add_control(self.scanning)
        self.networks = RowList(self)
        self.add(self.networks)
        self.dialog: PassphraseDialog | None = None
        self._closed_dialog: PassphraseDialog | None = None    # deleted when the next one opens or the page is left
        self.attempt: str | None = None            # the SSID a connection was asked for here
        self._seen_error: str | None = None

    # --- showing ---
    def render(self, state: sb.NetworkState) -> None:
        usable = self.usable()
        present = state.wifi_present
        enabled = bool(self.shown("wifi", state.wifi_enabled)) and present
        set_checked(self.toggle, enabled)
        self.toggle.setEnabled(usable and present and not state.airplane)
        if not present:
            text = "This device has no Wi-Fi adapter." if usable else ""
        elif state.airplane:
            text = "Off while airplane mode is on"
        elif not state.wifi_enabled:
            text = "Off"
        elif state.wifi_connected:
            text = f"Connected to {state.wifi_connected}"
        else:
            text = "On, not connected"
        self.master.set_description(text)

        self._follow_attempt(state)
        points = sorted(state.access_points, key=lambda p: (-p.strength, p.ssid.casefold()))
        on = state.wifi_enabled and present
        connected = next((p for p in points if p.connected or p.ssid == state.wifi_connected), None)
        self.current.setVisible(on and state.wifi_connected is not None)
        if state.wifi_connected is not None:
            self.current.set_title(state.wifi_connected)
            self.current.set_icon(signal_icon(connected.strength if connected is not None else 0))
            words = ["Connected"]
            if connected is not None:
                words.append("secured" if connected.secured else "open")
            words.append({"full": "internet access", "limited": "no internet access",
                          "portal": "sign-in to the network needed", "none": "no internet access"}[
                              state.connectivity])
            self.current.set_description(", ".join(words))
            self.disconnect_button.setAccessibleName(f"Disconnect from {state.wifi_connected}")
            self.disconnect_button.setEnabled(usable)

        others = [p for p in points if p is not connected]
        self.header.setVisible(on)
        count = len(others)
        self.header.set_description("Scanning…" if state.wifi_scanning and not count else
                                    "No other networks in range" if not count else
                                    "1 network in range" if count == 1 else f"{count} networks in range")
        self.scanning.set_busy(state.wifi_scanning)
        self.scan_button.setEnabled(usable and not state.wifi_scanning)
        by_ssid = {p.ssid: p for p in others}
        self.networks.sync([p.ssid for p in others] if on else [], self._row,
                           lambda ssid, row: row.update_point(by_ssid[ssid], usable,
                                                              by_ssid[ssid].connecting or ssid == self.attempt))

    def _row(self, ssid: str) -> NetworkRow:
        row = NetworkRow(ssid, self.networks)
        row.connect_requested.connect(self.connect_to)
        row.forget_requested.connect(self.forget)
        return row

    def _follow_attempt(self, state: sb.NetworkState) -> None:
        """What became of the connection asked for here; and an error of an attempt made elsewhere."""
        error = state.error
        if self.attempt is not None:
            if state.wifi_connected == self.attempt:
                self._attempt_over()
                if self.dialog is not None:
                    self.dialog.done(fluent.ContentDialog.Result.PRIMARY)
            elif error is not None:
                self._attempt_failed(error)
            self._seen_error = error
            return
        if error is not None and error != self._seen_error:
            self.show_error(error)             # the bridge reports an error in one state only
        self._seen_error = error

    def _attempt_over(self) -> None:
        self.attempt = None
        self._pending.pop("connect", None)

    def _attempt_failed(self, text: str) -> None:
        ssid = self.attempt
        self._attempt_over()
        if self.dialog is not None and self.dialog.is_open() and self.dialog.ssid == ssid:
            self.dialog.fail(text)
        else:
            self.show_error(f"Could not connect to {ssid}. {text}")

    def _check_pending(self) -> None:
        if self.attempt is not None and "connect" in self._pending:
            if self._pending["connect"][1] <= time.monotonic():
                self._attempt_failed("Connecting took too long.")
        super()._check_pending()

    # --- changes ---
    def _toggled(self, checked: bool) -> None:
        if self.rendering:
            return
        if self.send(sb.command("wifi-set-enabled", enabled=checked),
                     f"Wi-Fi could not be turned {'on' if checked else 'off'}."):
            self.expect("wifi", checked, f"Wi-Fi could not be turned {'on' if checked else 'off'}.")
        self.refresh()

    def _scan(self) -> None:
        self.send(sb.command("wifi-scan"), "The scan could not be started.")

    def _disconnect(self) -> None:
        self.send(sb.command("wifi-disconnect"), "The network could not be disconnected.")

    def connect_to(self, ssid: str) -> None:
        point = next((p for p in self.state().access_points if p.ssid == ssid), None)
        if point is None:
            self.show_error(f"{ssid} is not in range any more.")
            return
        if point.secured and not point.known:
            self._ask_passphrase(ssid)
        else:
            self._connect(ssid, None)

    def _connect(self, ssid: str, passphrase: str | None) -> None:
        fields = {"ssid": ssid} if passphrase is None else {"ssid": ssid, "passphrase": passphrase}
        try:
            command = sb.command("wifi-connect", **fields)
        except ValueError:
            self._attempt_refused(ssid, "This password cannot be used (it is empty or too long).")
            return
        if not self.send(command, f"Could not connect to {ssid}."):
            self._attempt_refused(ssid, "The settings service is not running.")
            return
        self.attempt = ssid
        self._seen_error = None
        self.expect("connect", ssid, "", CONNECT_SECONDS)
        self.refresh()

    def _attempt_refused(self, ssid: str, text: str) -> None:
        if self.dialog is not None and self.dialog.is_open() and self.dialog.ssid == ssid:
            self.dialog.fail(text)

    def _drop_closed_dialog(self) -> None:
        if self._closed_dialog is not None:
            self._closed_dialog.deleteLater()
            self._closed_dialog = None

    def _ask_passphrase(self, ssid: str) -> None:
        if self.dialog is not None and self.dialog.is_open():
            return
        self._drop_closed_dialog()
        dialog = PassphraseDialog(self, ssid)
        dialog.submitted.connect(lambda secret, s=ssid: self._connect(s, secret))
        dialog.finished.connect(self._dialog_closed)
        self.dialog = dialog
        dialog.open()

    def _dialog_closed(self, _result=None) -> None:
        dialog, self.dialog = self.dialog, None
        if dialog is not None:
            if self.attempt == dialog.ssid and self.state().wifi_connected != dialog.ssid:
                # Cancelled while trying: the attempt goes on in NetworkManager; stop following it here.
                self._attempt_over()
            self._drop_closed_dialog()
            self._closed_dialog = dialog
        if self.active:
            self.refresh()

    def forget(self, ssid: str) -> None:
        if fluent.ask(self, f"Forget {ssid}?", "The saved password and settings of this network are removed. To "
                      "connect again you have to enter the password.", "Forget", "Cancel"):
            self.send(sb.command("wifi-forget", ssid=ssid), f"{ssid} could not be forgotten.")

    def deactivate(self) -> None:
        if self.dialog is not None and self.dialog.is_open():
            self.dialog.reject()
        self._drop_closed_dialog()
        self.attempt = None
        super().deactivate()


class ConnectionsSection(BridgeSection):
    page = "network"
    title = "Other connections"
    order = 20
    keywords = ("ethernet", "wired", "cable", "lan", "vpn", "airplane", "flight mode", "aeroplane", "radio")
    service = sb.NETWORK
    missing = "NetworkManager is not running."

    def __init__(self, parent: QWidget | None = None, *, bridge=None):
        super().__init__(parent, bridge=bridge)
        self.ethernet = self.add(fluent.SettingsCard("Ethernet", "", fluent.icon("network-wired-symbolic"), None, self))
        self.vpn = self.add(fluent.SettingsCard("VPN", "", fluent.icon("network-vpn-symbolic"), None, self))
        self.airplane_toggle = fluent.ToggleSwitch()
        self.airplane = self.add(fluent.SettingsCard("Airplane mode", "Turns off Wi-Fi, Bluetooth and every other "
                                                     "radio", fluent.icon("airplane-mode-symbolic"),
                                                     self.airplane_toggle, self))
        self.airplane_toggle.toggled.connect(self._airplane_toggled)

    def show_notice(self, text: str, severity: str = "info") -> None:
        pass                                   # the Wi-Fi section above says once that the service is missing

    def render(self, state: sb.NetworkState) -> None:
        usable = self.usable()
        self.ethernet.set_description("Connected" if state.wired else
                                      "Not connected: no cable, or no wired network adapter" if usable else "")
        self.vpn.set_description(("A VPN connection is active." if state.vpn else "No VPN connection is active.")
                                 + " VPN profiles are added with NetworkManager's own tools (nmcli connection "
                                 "import), not here.")
        set_checked(self.airplane_toggle, bool(self.shown("airplane", state.airplane)))
        self.airplane_toggle.setEnabled(usable)

    def _airplane_toggled(self, checked: bool) -> None:
        if self.rendering:
            return
        failed = f"Airplane mode could not be turned {'on' if checked else 'off'}."
        if self.send(sb.command("airplane-mode", enabled=checked), failed):
            self.expect("airplane", checked, failed)
        self.refresh()


class InterfaceCard(fluent.SettingsExpander):
    """The details of one network interface."""

    def __init__(self, item: netinfo.Interface, details: netinfo.Details, parent: QWidget | None = None):
        icon = "network-wireless-symbolic" if item.kind == "Wi-Fi" else "network-wired-symbolic"
        super().__init__(f"{item.kind} ({item.name})", "", fluent.icon(icon), None, parent, expanded=item.connected)
        self.grid_holder = QWidget(self)
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(52, 12, 16, 12)
        self.grid.setHorizontalSpacing(32)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(1, 1)
        self.add_widget(self.grid_holder)
        self.labels: list[tuple[fluent.Label, fluent.Label]] = []
        self.update_item(item, details)

    def update_item(self, item: netinfo.Interface, details: netinfo.Details) -> None:
        rows = netinfo.interface_rows(item, details)
        self.header.set_description(rows[0][1] + (f" · {item.ipv4[0].split('/')[0]}" if item.ipv4 else ""))
        while len(self.labels) < len(rows):
            index = len(self.labels)
            key = fluent.Label("", "body", self.grid_holder, tone="secondary")
            value = fluent.Label("", "body", self.grid_holder, wrap=True, selectable=True)
            self.grid.addWidget(key, index, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(value, index, 1)
            self.labels.append((key, value))
        for index, (key, value) in enumerate(self.labels):
            visible = index < len(rows)
            key.setVisible(visible)
            value.setVisible(visible)
            if visible:
                key.setText(rows[index][0])
                value.setText(rows[index][1])
                value.setAccessibleName(rows[index][0])

    def values(self) -> dict[str, str]:
        return {k.text(): v.text() for k, v in self.labels if not k.isHidden()}


def read_details() -> netinfo.Details:
    """Blocking: the interfaces, addresses, gateways and DNS servers of this device."""
    return netinfo.read(context.runner(), commands.argv, context.root)


class DetailsSection(Section):
    page = "network"
    title = "Connection details"
    order = 30
    keywords = ("ip address", "ipv4", "ipv6", "gateway", "dns", "mac address", "link speed", "properties")

    def __init__(self, parent: QWidget | None = None, *, reader=None, bridge=None):
        super().__init__(parent)
        self.reader = reader or read_details
        self._bridge = bridge
        self.details = netinfo.Details()
        self.copy_button = fluent.Button("Copy", self, icon=fluent.icon("edit-copy-symbolic"))
        self.copy_button.setToolTip("Copy the connection details as text")
        self.copy_button.clicked.connect(self.copy)
        self.refresh_button = fluent.IconButton(fluent.icon("view-refresh-symbolic"), "Read the details again", self)
        self.refresh_button.clicked.connect(self.reload)
        self.summary = self.add(fluent.SettingsCard("Addresses of this device", "Reading…", None, self.copy_button,
                                                    self))
        self.summary.add_control(self.refresh_button)
        self.interfaces = RowList(self)
        self.add(self.interfaces)
        self.loading = False
        self._generation = 0

    def activate(self) -> None:
        if self.active:
            return
        self.active = True
        if self._bridge is None:
            self._bridge = bridgeclient.for_widget(self)
        self._bridge.network_changed.connect(self._network_changed)
        self.reload()

    def deactivate(self) -> None:
        if not self.active:
            return
        self.active = False
        self._generation += 1                      # an answer still on its way is dropped
        try:
            self._bridge.network_changed.disconnect(self._network_changed)
        except (RuntimeError, TypeError):
            pass

    def _network_changed(self, _state=None) -> None:
        self.reload()                              # a connection came or went: the addresses changed

    def reload(self) -> None:
        if self.loading:
            self._again = True
            return
        self.loading, self._again = True, False
        self._generation += 1
        generation = self._generation
        fluent.run_in_thread(self.reader, lambda details: self._loaded(generation, details),
                             lambda exc: self._failed(generation, exc))

    def _loaded(self, generation: int, details: netinfo.Details) -> None:
        self.loading = False
        if generation != self._generation or not self.active:
            return
        self.details = details
        self.clear_error()
        by_name = {item.name: item for item in details.interfaces}
        self.interfaces.sync(list(by_name), lambda name: InterfaceCard(by_name[name], details, self.interfaces),
                             lambda name, card: card.update_item(by_name[name], details))
        connected = [item for item in details.interfaces if item.connected]
        self.summary.set_description("No network adapter was found." if not details.interfaces else
                                     "No connection has an address right now." if not connected else
                                     ", ".join(f"{i.kind}: {(i.ipv4 or i.ipv6)[0].split('/')[0]}" for i in connected))
        self.copy_button.setEnabled(bool(details.interfaces))
        if getattr(self, "_again", False):
            self.reload()

    def _failed(self, generation: int, exc: Exception) -> None:
        self.loading = False
        if generation != self._generation or not self.active:
            return
        self.details = netinfo.Details()
        self.interfaces.sync([], lambda name: None, lambda name, card: None)
        self.copy_button.setEnabled(False)
        if isinstance(exc, CommandUnavailable):
            self.summary.set_description("Not available: the ip tool (package iproute2) is not installed.")
        else:
            self.summary.set_description("The connection details could not be read.")
            self.show_error(user_message(exc))

    def copy(self) -> None:
        text = netinfo.details_text(self.details)
        if text:
            QGuiApplication.clipboard().setText(text)
            self.show_notice("Copied the connection details to the clipboard.", "success")


class ProxySection(Section):
    page = "network"
    title = "Proxy"
    order = 40
    keywords = ("proxy", "http_proxy", "https_proxy", "no_proxy")

    def __init__(self, parent: QWidget | None = None, *, environ=None):
        super().__init__(parent)
        self.environ = context.environ() if environ is None else environ
        self.card = self.add(fluent.SettingsExpander("Proxy of this session", "", None, None, self))
        self.card.add_row("Where this comes from", "The proxy is set by environment variables (http_proxy, "
                          "https_proxy, no_proxy) when you sign in, for example by your administrator in "
                          "/etc/environment. Control Center shows them and does not change them.")
        self.value_rows: list[fluent.SettingsCard] = []

    def activate(self) -> None:
        self.active = True
        settings = netinfo.proxy_settings(self.environ)
        for row in self.value_rows:
            row.hide()
        for index, (label, value) in enumerate(settings):
            if index >= len(self.value_rows):
                self.value_rows.append(self.card.add_row("", "", ElidedLabel("", self, width=380)))
            row = self.value_rows[index]
            row.set_title(label)
            row.control().set_full_text(value)
            row.control().setAccessibleName(label)
            row.show()
        self.card.header.set_description("No proxy is set: applications connect directly." if not settings else
                                         "Applications started in this session use a proxy.")
        self.card.set_expanded(bool(settings), animate=False)

    def values(self) -> list[tuple[str, str]]:
        return [(row.title(), row.control().full_text()) for row in self.value_rows if not row.isHidden()]


# --- Bluetooth -------------------------------------------------------------------------------------------------

class DeviceRow(fluent.SettingsCard):
    """One Bluetooth device, paired or found."""

    connect_requested = Signal(str)
    disconnect_requested = Signal(str)
    remove_requested = Signal(str)

    def __init__(self, address: str, parent: QWidget | None = None):
        super().__init__("", "", None, None, parent)
        self.address = address
        self.device: sb.BluetoothDevice | None = None
        self.action = fluent.Button("Connect", self)
        self.action.clicked.connect(self._act)
        self.set_control(self.action)
        self.remove_button = fluent.Button("Remove", self, subtle=True)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.address))
        self.add_control(self.remove_button)
        self.busy = Busy("Connecting…", self)
        self.add_control(self.busy)

    def _act(self) -> None:
        if self.device is not None and self.device.connected:
            self.disconnect_requested.emit(self.address)
        else:
            self.connect_requested.emit(self.address)

    def update_device(self, device: sb.BluetoothDevice, enabled: bool, connecting: bool) -> None:
        self.device = device
        name = device.name or device.address
        self.set_title(name)
        self.set_icon(fluent.icon(BLUETOOTH_ICONS.get(device.icon, "bluetooth-active-symbolic")))
        words = []
        if connecting:
            words.append("Connecting…" if device.paired else "Pairing…")
        elif device.connected:
            words.append("Connected")
        elif device.paired:
            words.append("Paired, not connected")
        else:
            words.append("Not paired")
        if device.icon in BLUETOOTH_KINDS:
            words.append(BLUETOOTH_KINDS[device.icon])
        if device.battery is not None and device.connected:
            words.append(f"Battery {device.battery} %")
        self.set_description(" · ".join(words))
        self.busy.set_busy(connecting, "Connecting…" if device.paired else "Pairing…")
        self.action.setVisible(not connecting)
        self.action.setText("Disconnect" if device.connected else "Connect" if device.paired else "Pair")
        self.action.setAccessibleName(f"{self.action.text()} {name}")
        self.action.setEnabled(enabled)
        self.remove_button.setVisible(device.paired)
        self.remove_button.setAccessibleName(f"Remove {name}")
        self.remove_button.setEnabled(enabled and not connecting)


class BluetoothSection(BridgeSection):
    page = "bluetooth"
    title = "Bluetooth"
    order = 10
    keywords = ("bluetooth", "pair", "headphones", "headset", "speaker", "keyboard", "mouse", "phone", "wireless",
                "add device")
    service = sb.BLUETOOTH

    def __init__(self, parent: QWidget | None = None, *, bridge=None):
        super().__init__(parent, bridge=bridge)
        self.toggle = fluent.ToggleSwitch()
        self.master = self.add(fluent.SettingsCard("Bluetooth", "", fluent.icon("bluetooth-active-symbolic"),
                                                   self.toggle, self))
        self.toggle.toggled.connect(self._toggled)
        self.add_button = fluent.Button("Add device", self, accent=True)
        self.add_button.clicked.connect(self._discover)
        self.add_card = self.add(fluent.SettingsCard("Add a device", "", fluent.icon("list-add-symbolic"),
                                                     self.add_button, self))
        self.searching = Busy("Searching…", self)
        self.add_card.add_control(self.searching)
        self.paired_title = fluent.Label("Paired devices", "body-strong", self)
        self.paired_title.setContentsMargins(0, 8, 0, 4)
        self.add(self.paired_title)
        self.paired = RowList(self)
        self.add(self.paired)
        self.found_title = fluent.Label("Devices found", "body-strong", self)
        self.found_title.setContentsMargins(0, 8, 0, 4)
        self.add(self.found_title)
        self.found = RowList(self)
        self.add(self.found)
        self.attempts: dict[str, bool] = {}        # address -> the device was seen connecting since

    def available(self, state) -> bool:
        return state.available

    def refresh(self) -> None:
        super().refresh()
        if self.bridge.status() == sb.RUNNING and not self.state().available:
            self.show_notice("This device has no Bluetooth adapter, or the Bluetooth service is not running.")

    def render(self, state: sb.BluetoothState) -> None:
        usable = self.usable()
        enabled = bool(self.shown("enabled", state.enabled))
        set_checked(self.toggle, enabled and state.available)
        self.toggle.setEnabled(usable)
        connected = [d for d in state.devices if d.connected]
        self.master.set_description("" if not state.available else "Off" if not state.enabled else
                                    "On, connected to " + ", ".join(d.name or d.address for d in connected[:3])
                                    if connected else "On" if state.enabled else "Off")
        self.master.set_icon(fluent.icon("bluetooth-active-symbolic" if state.enabled else
                                         "bluetooth-disabled-symbolic"))
        on = state.available and state.enabled
        discovering = bool(self.shown("discovering", state.discovering)) and on
        self.add_card.setVisible(on)
        self.add_card.set_description("Put the device into pairing mode. It appears below when it is found."
                                      if discovering else "Headphones, keyboards, mice, phones and other devices")
        self.add_button.setText("Stop searching" if discovering else "Add device")
        self.add_button.set_accent(not discovering)
        self.add_button.setEnabled(usable)
        self.searching.set_busy(discovering)

        by_address = {d.address: d for d in state.devices}
        failed = []
        for address in list(self.attempts):
            device = by_address.get(address)
            if device is None or device.connected:   # done (or gone): the attempt is over
                del self.attempts[address]
                self._pending.pop(address, None)
            elif device.connecting:
                self.attempts[address] = True
            elif self.attempts[address]:              # it was connecting and gave up
                del self.attempts[address]
                failed.append(self._pending.pop(address, (None, 0, ""))[2])
        if failed:
            self.show_error(" ".join(dict.fromkeys(text for text in failed if text)))

        def update(address: str, row: DeviceRow) -> None:
            device = by_address[address]
            row.update_device(device, usable, device.connecting or address in self.attempts)

        paired = [d.address for d in state.devices if d.paired] if on else []
        found = [d.address for d in state.devices if not d.paired] if on else []
        self.paired.sync(paired, lambda a: self._row(a, self.paired), update)
        self.found.sync(found, lambda a: self._row(a, self.found), update)
        self.paired_title.setVisible(bool(paired))
        self.found_title.setVisible(bool(found))

    def _row(self, address: str, parent: QWidget) -> DeviceRow:
        row = DeviceRow(address, parent)
        row.connect_requested.connect(self.connect_device)
        row.disconnect_requested.connect(self.disconnect_device)
        row.remove_requested.connect(self.remove_device)
        return row

    def _name(self, address: str) -> str:
        device = next((d for d in self.state().devices if d.address == address), None)
        return (device.name if device is not None and device.name else address)

    def _toggled(self, checked: bool) -> None:
        if self.rendering:
            return
        failed = f"Bluetooth could not be turned {'on' if checked else 'off'}."
        if self.send(sb.command("bluetooth-set-enabled", enabled=checked), failed):
            self.expect("enabled", checked, failed)
        self.refresh()

    def _discover(self) -> None:
        wanted = not self.state().discovering
        failed = "The search for devices could not be started." if wanted else "The search could not be stopped."
        if self.send(sb.command("bluetooth-discover", enabled=wanted), failed):
            self.expect("discovering", wanted, failed)
        self.refresh()

    def connect_device(self, address: str) -> None:
        name = self._name(address)
        if self.send(sb.command("bluetooth-connect", address=address), f"Could not connect to {name}."):
            self.attempts[address] = False
            self.expect(address, True, f"Could not connect to {name}. Make sure it is switched on, in range and, "
                        "for a new device, in pairing mode.", BLUETOOTH_SECONDS)
        self.refresh()

    def _check_pending(self) -> None:
        now = time.monotonic()
        for address in [a for a in self.attempts if a in self._pending and self._pending[a][1] <= now]:
            del self.attempts[address]
        super()._check_pending()

    def disconnect_device(self, address: str) -> None:
        self.send(sb.command("bluetooth-disconnect", address=address),
                  f"{self._name(address)} could not be disconnected.")

    def remove_device(self, address: str) -> None:
        name = self._name(address)
        if fluent.ask(self, f"Remove {name}?", "The device is unpaired. To use it again you have to pair it "
                      "again.", "Remove", "Cancel"):
            self.send(sb.command("bluetooth-forget", address=address), f"{name} could not be removed.")

    def deactivate(self) -> None:
        if self.active and self.state().discovering and self.bridge.status() == sb.RUNNING:
            try:
                self.bridge.send(sb.command("bluetooth-discover", enabled=False))    # do not keep the radio searching
            except ValueError:
                pass
        self.attempts.clear()
        super().deactivate()


SECTIONS = (WifiSection, ConnectionsSection, DetailsSection, ProxySection, BluetoothSection)
