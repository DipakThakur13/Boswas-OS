"""Network & internet: VPN connections and the network adapters.

Page "vpn":
  VPN connections      the VPN and WireGuard connections of NetworkManager,
                       Connect / Disconnect (`nmcli connection up|down uuid`),
                       Remove after a question, and Add: a WireGuard or
                       OpenVPN file the person chooses is recognised by its
                       content and imported (`nmcli connection import type
                       ... file ...`). No secret is ever on a command line; a
                       VPN that asks for a password each time needs a
                       password prompt for NetworkManager, which the desktop
                       does not have yet: the page says so when it happens.
Page "advanced-network":
  Network adapters     every adapter with its state and connection, the data
                       it moved since the device started (sysfs), its
                       hardware address only when asked for, and Disable /
                       Enable (`nmcli device disconnect|connect`), disabling
                       after a question
(Connection details, on the same page, is connectivity.DetailsSection.)

NetworkManager asks polkit for the changes; reading runs on a worker thread.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtWidgets import QWidget

from ... import commands, netconfig, probes, sysinfo
from ...errors import Failed
from . import context
from .system import CHANGE_TIMEOUT, READ_TIMEOUT, ToolSection, tool_failure
from .widgets import RowList, ask, window_of

NMCLI_CHANGE_TIMEOUT = 120.0
TYPE_ICONS = {"wifi": "network-wireless-symbolic", "ethernet": "network-wired-symbolic",
              "wireguard": "network-vpn-symbolic", "tun": "network-vpn-symbolic", "bt": "bluetooth-active-symbolic"}
SECRETS_TEXT = ("This VPN asks for a password or another secret when it connects, and this desktop has no prompt "
                "for NetworkManager's secrets yet. Save the secret in the connection with nmtui in a terminal, or "
                "use a WireGuard or certificate-based connection.")


def nmcli_failure(what: str, result: commands.Completed) -> Failed:
    if netconfig.needs_secrets(result.stderr or result.stdout):
        return Failed(SECRETS_TEXT)
    return tool_failure(what, result)


class VpnSection(ToolSection):
    page = "vpn"
    title = "VPN connections"
    order = 10
    keywords = ("vpn", "wireguard", "openvpn", "add vpn", "import", "connect vpn", "tunnel")

    def __init__(self, parent: QWidget | None = None, *, runner=None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.add_button = fluent.Button("Add VPN…", self, accent=True)
        self.add_button.setAccessibleName("Add a VPN connection from a file")
        self.add_button.clicked.connect(self.add_vpn)
        self.header = self.add(fluent.SettingsCard(
            "Add a VPN connection", "From a WireGuard (.conf) or OpenVPN (.ovpn) file your VPN provider or "
            "organisation gave you", fluent.icon("network-vpn-symbolic"), self.add_button, self))
        self.list = self.add(RowList(self))
        self.empty = self.add(fluent.SettingsCard("No VPN connection yet", "", None, None, self))
        self.empty.hide()
        self.connections: list[netconfig.Connection] = []

    def read(self):
        if not self.runner.available(commands.EXECUTABLES["nmcli"]):
            raise Failed("NetworkManager's nmcli is not installed.")
        result = self.runner.run(commands.argv("nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "connection",
                                               "show"), READ_TIMEOUT)
        if result.code != 0:
            raise tool_failure("Reading the connections", result)
        return netconfig.parse_connections(result.stdout)

    def show_state(self, connections) -> None:
        self.connections = connections
        self.show_notice("")
        by_uuid = {c.uuid: c for c in connections}
        self.list.sync(list(by_uuid), self._row, lambda uuid, row: self._update(row, by_uuid[uuid]))
        self.empty.setVisible(not connections)
        self.add_button.setEnabled(not self.busy)

    def load_failed(self, exc: Exception) -> None:
        self.list.sync([], lambda _k: None, lambda _k, _r: None)
        self.add_button.setEnabled(False)
        self.show_notice(f"VPN connections are not available: {exc}", "warning")

    def _row(self, uuid: str) -> fluent.SettingsCard:
        card = fluent.SettingsCard("", "", fluent.icon("network-vpn-symbolic"), None, self.list)
        card.connect_button = fluent.Button("Connect", card)
        card.connect_button.clicked.connect(lambda _c=False, u=uuid: self.toggle(u))
        card.remove_button = fluent.Button("Remove", card, subtle=True)
        card.remove_button.clicked.connect(lambda _c=False, u=uuid: self.remove(u))
        card.set_control(card.connect_button)
        card.add_control(card.remove_button)
        return card

    def _update(self, card: fluent.SettingsCard, connection: netconfig.Connection) -> None:
        card.set_title(connection.name)
        card.set_description(f"{connection.kind_label} · " + ("Connected" if connection.active else "Not connected"))
        card.connect_button.setText("Disconnect" if connection.active else "Connect")
        card.connect_button.setAccessibleName(f"{'Disconnect' if connection.active else 'Connect'} {connection.name}")
        card.remove_button.setAccessibleName(f"Remove {connection.name}")
        for button in (card.connect_button, card.remove_button):
            button.setEnabled(not self.busy)

    def set_enabled(self, enabled: bool) -> None:
        self.add_button.setEnabled(enabled)
        for uuid in self.list.keys():
            row = self.list.row(uuid)
            for button in (row.connect_button, row.remove_button):
                button.setEnabled(enabled)

    def _connection(self, uuid: str) -> netconfig.Connection | None:
        return next((c for c in self.connections if c.uuid == uuid), None)

    def toggle(self, uuid: str) -> None:
        connection = self._connection(uuid)
        if connection is None:
            return
        verb = "down" if connection.active else "up"

        def apply() -> None:
            result = self.runner.run(commands.nmcli_change_argv("connection", verb, "uuid", uuid),
                                     NMCLI_CHANGE_TIMEOUT)
            if result.code != 0:
                raise nmcli_failure(f"{'Disconnecting' if verb == 'down' else 'Connecting'} {connection.name}",
                                    result)

        self.change(apply, f"{connection.name} is {'disconnected' if verb == 'down' else 'connected'}.")

    def remove(self, uuid: str) -> None:
        connection = self._connection(uuid)
        if connection is None or not ask(self, f"Remove {connection.name}?",
                                         "The VPN connection and the settings saved with it are deleted from "
                                         "NetworkManager. You need its file again to add it back.", "Remove"):
            return

        def apply() -> None:
            result = self.runner.run(commands.nmcli_change_argv("connection", "delete", "uuid", uuid),
                                     CHANGE_TIMEOUT)
            if result.code != 0:
                raise nmcli_failure(f"Removing {connection.name}", result)

        self.change(apply, f"{connection.name} was removed.")

    def add_vpn(self) -> None:
        window = window_of(self)
        if window is None:
            return
        path = window.choose_file("Choose a VPN configuration", "VPN configurations (*.conf *.ovpn);;All files (*)")
        if path:
            self.import_file(path)

    def import_file(self, path: str) -> None:
        kind, problem = netconfig.import_kind(path)
        if not kind:
            self.show_error(problem)
            return
        label = "WireGuard" if kind == "wireguard" else "OpenVPN"
        if not ask(self, f"Add this {label} connection?", f"{path}\n\nNetworkManager adds a connection from this "
                   "file, with the keys and certificates it contains. Add only files from your VPN provider or "
                   "organisation.", "Add"):
            return

        def apply() -> None:
            result = self.runner.run(commands.nmcli_change_argv("connection", "import", "type", kind, "file", path),
                                     CHANGE_TIMEOUT)
            if result.code != 0:
                text = " ".join((result.stderr or result.stdout).split()).casefold()
                if "plugin" in text and kind == "openvpn":
                    raise Failed("OpenVPN connections need NetworkManager's OpenVPN plugin (package "
                                 "network-manager-openvpn), which is not installed.")
                raise nmcli_failure("Adding the VPN connection", result)

        self.change(apply, f"The {label} connection was added. Connect it in the list.")


class AdaptersSection(ToolSection):
    page = "advanced-network"
    title = "Network adapters"
    order = 10
    keywords = ("network adapter", "adapter", "disable", "enable", "data usage", "mac address", "hardware address")

    def __init__(self, parent: QWidget | None = None, *, runner=None, root: str | None = None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.root = context.root if root is None else root
        self.list = self.add(RowList(self))
        self.empty = self.add(fluent.SettingsCard("No network adapter was found", "", None, None, self))
        self.empty.hide()
        self.devices: list[sysinfo.NetDevice] = []
        self.revealed: dict[str, str] = {}
        self.usage: dict[str, netconfig.Traffic] = {}

    def read(self):
        result = self.runner.run(commands.argv("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"),
                                 READ_TIMEOUT)
        if result.code != 0:
            raise tool_failure("Reading the network adapters", result)
        devices = [d for d in sysinfo.parse_nmcli_devices(result.stdout) if d.kind not in ("loopback", "wifi-p2p")]
        root = probes.Root(self.root)
        return devices, {d.device: netconfig.traffic(root, d.device) for d in devices}

    def show_state(self, state) -> None:
        self.devices, self.usage = state
        self.show_notice("")
        by_name = {d.device: d for d in self.devices}
        self.list.sync(list(by_name), self._row, lambda name, row: self._update(row, by_name[name]))
        self.empty.setVisible(not self.devices)

    def load_failed(self, exc: Exception) -> None:
        self.list.sync([], lambda _k: None, lambda _k, _r: None)
        self.show_notice(f"The network adapters cannot be listed: {exc}", "warning")

    def _row(self, name: str) -> fluent.SettingsExpander:
        card = fluent.SettingsExpander(name, "", None, None, self.list)
        card.switch = fluent.Button("Disable", card)
        card.switch.clicked.connect(lambda _c=False, n=name: self.toggle(n))
        card.header.set_control(card.switch)
        card.usage_row = card.add_row("Data used", "")
        card.mac_button = fluent.Button("Show", card)
        card.mac_button.setAccessibleName(f"Show the hardware address of {name}")
        card.mac_button.clicked.connect(lambda _c=False, n=name: self.reveal(n))
        card.mac_row = card.add_row("Hardware address", "Shown only when you ask", card.mac_button)
        return card

    def _update(self, card, device: sysinfo.NetDevice) -> None:
        card.header.set_title(f"{device.kind_label} ({device.device})")
        card.header.set_icon(fluent.icon(TYPE_ICONS.get(device.kind, "network-wired-symbolic")))
        state = device.state_label + (f" · {device.connection}" if device.connection else "")
        card.header.set_description(state)
        disconnected = device.state.startswith(("disconnected", "unavailable", "unmanaged"))
        card.switch.setText("Enable" if disconnected else "Disable")
        card.switch.setAccessibleName(f"{'Enable' if disconnected else 'Disable'} {device.device}")
        card.switch.setEnabled(not self.busy and not device.state.startswith(("unavailable", "unmanaged")))
        card.usage_row.set_description(netconfig.traffic_text(self.usage.get(device.device,
                                                                                netconfig.Traffic(None, None))))
        address = self.revealed.get(device.device)
        if address is not None:
            card.mac_row.set_description(address or "This adapter has no hardware address")
            card.mac_button.hide()

    def set_enabled(self, enabled: bool) -> None:
        for name in self.list.keys():
            self.list.row(name).switch.setEnabled(enabled)

    def reveal(self, name: str) -> None:
        self.revealed[name] = netconfig.hardware_address(probes.Root(self.root), name)
        row = self.list.row(name)
        device = next((d for d in self.devices if d.device == name), None)
        if row is not None and device is not None:
            self._update(row, device)

    def toggle(self, name: str) -> None:
        device = next((d for d in self.devices if d.device == name), None)
        if device is None:
            return
        connect = device.state.startswith("disconnected")
        if not connect and not ask(self, f"Disable {device.device}?", f"{device.kind_label} ({device.device}) "
                                   "disconnects now and stays disconnected until you enable it again or restart. "
                                   "If it is your only connection, you go offline.", "Disable"):
            return

        def apply() -> None:
            result = self.runner.run(commands.nmcli_change_argv("device", "connect" if connect else "disconnect",
                                                                name), NMCLI_CHANGE_TIMEOUT)
            if result.code != 0:
                raise nmcli_failure(f"{'Enabling' if connect else 'Disabling'} {name}", result)

        self.change(apply)


SECTIONS = (VpnSection, AdaptersSection)
