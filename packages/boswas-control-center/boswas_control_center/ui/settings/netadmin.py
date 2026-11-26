"""Network & internet: VPN connections, the network adapters and the wired connection's settings.

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
Page "ethernet":
  Wired connection     per wired adapter, as in Windows' Ethernet page: IP
                       assignment (Automatic (DHCP) or Manual: an IPv4
                       address, its prefix length, a gateway and DNS
                       servers), DNS server assignment (automatic or a list
                       of IPv4 and IPv6 servers), Metered connection, and the
                       adapter's properties (link speed, IPv4 and IPv6
                       addresses, gateways, DNS servers, MAC) with Copy. The
                       settings belong to the connection profile the adapter
                       uses, changed with `nmcli connection modify uuid UUID`
                       (validated properties only) and applied with
                       `nmcli connection up uuid UUID` when it is active.
                       Whether the cable is in is the bridge's
                       connectivity.EthernetSection above it.

NetworkManager asks polkit for the changes; reading runs on a worker thread.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QComboBox, QFormLayout, QLineEdit, QVBoxLayout, QWidget

from ... import commands, netconfig, probes, sysinfo
from ...errors import Failed
from . import context
from .system import CHANGE_TIMEOUT, READ_TIMEOUT, ToolSection, tool_failure
from .widgets import RowList, ask, window_of

NMCLI_CHANGE_TIMEOUT = 120.0
TYPE_ICONS = {"wifi": "network-wireless-symbolic", "ethernet": "network-wired-symbolic",
              "wireguard": "network-vpn-symbolic", "tun": "network-vpn-symbolic", "bt": "bluetooth-active-symbolic"}
METERED_TEXT = ("Programs that respect it download less, such as updates in the background, while this connection is "
                "used")
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


class IpDialog(fluent.ContentDialog):
    """Edit IP settings: Automatic (DHCP) or Manual with an IPv4 address, prefix length, gateway and DNS servers."""

    def __init__(self, parent: QWidget, settings: netconfig.WiredSettings):
        super().__init__(parent, "Edit IP settings", f"{settings.name}: how this computer gets its IPv4 address.",
                         primary="Save", close="Cancel")
        self.mode = QComboBox(self)
        self.mode.addItem("Automatic (DHCP)", False)
        self.mode.addItem("Manual", True)
        self.mode.setAccessibleName("IP assignment")
        self.content_layout.addWidget(self.mode)
        self.form_holder = QWidget(self)
        form = QFormLayout(self.form_holder)
        form.setContentsMargins(0, 8, 0, 0)
        address, _slash, prefix = (settings.addresses[0] if settings.addresses else "").partition("/")
        self.address = self._field("IP address", address, "192.168.1.20")
        self.prefix = self._field("Subnet prefix length", prefix, "24")
        self.gateway = self._field("Gateway", settings.gateway, "192.168.1.1")
        self.dns1 = self._field("Preferred DNS", settings.dns[0] if settings.dns else "", "")
        self.dns2 = self._field("Alternate DNS", settings.dns[1] if len(settings.dns) > 1 else "", "")
        for label, field in (("IP address", self.address), ("Subnet prefix length", self.prefix),
                             ("Gateway", self.gateway), ("Preferred DNS", self.dns1), ("Alternate DNS", self.dns2)):
            form.addRow(fluent.Label(label, "body", self.form_holder), field)
        self.content_layout.addWidget(self.form_holder)
        self.problem = fluent.Label("", "caption", self, tone="danger", wrap=True)
        self.content_layout.addWidget(self.problem)
        self.mode.setCurrentIndex(1 if settings.manual else 0)
        self.mode.currentIndexChanged.connect(self._check)
        for field in (self.address, self.prefix, self.gateway, self.dns1, self.dns2):
            field.textChanged.connect(self._check)
        self._check()

    def _field(self, name: str, text: str, placeholder: str) -> QLineEdit:
        field = QLineEdit(text, self)
        field.setAccessibleName(name)
        field.setPlaceholderText(placeholder)
        field.setMinimumWidth(220)
        return field

    def manual(self) -> bool:
        return bool(self.mode.currentData())

    def servers(self) -> list[str]:
        return [field.text().strip() for field in (self.dns1, self.dns2) if field.text().strip()]

    def problem_text(self) -> str:
        if not self.manual():
            return ""
        problem = netconfig.ipv4_problem(self.address.text(), self.prefix.text(), self.gateway.text())
        if problem:
            return problem
        return netconfig.dns_problem(self.servers()).replace("Type at least one DNS server.",
                                                              "Type at least one DNS server: a manual address gets "
                                                              "none from the network.")

    def _check(self, *_args) -> None:
        self.form_holder.setEnabled(self.manual())
        problem = self.problem_text()
        typed = any(field.text().strip() for field in (self.address, self.prefix))
        self.problem.setText(problem if typed else "")
        self.problem.setVisible(bool(problem and typed))
        self.primary_button.setEnabled(not problem)

    def words(self) -> tuple[str, ...]:
        if not self.manual():
            return netconfig.ip_words(False)
        return netconfig.ip_words(True, self.address.text(), self.prefix.text(), self.gateway.text(), self.servers())


class DnsDialog(fluent.ContentDialog):
    """Edit DNS settings: the network's servers (DHCP) or a list of up to three IPv4 and three IPv6 servers."""

    def __init__(self, parent: QWidget, settings: netconfig.WiredSettings):
        super().__init__(parent, "Edit DNS settings", f"{settings.name}: the servers that turn names into addresses.",
                         primary="Save", close="Cancel")
        self.mode = QComboBox(self)
        self.mode.addItem("Automatic (DHCP)", False)
        self.mode.addItem("Manual", True)
        self.mode.setAccessibleName("DNS server assignment")
        if settings.manual:                      # no DHCP: the servers can only be set by hand
            self.mode.model().item(0).setEnabled(False)
        self.content_layout.addWidget(self.mode)
        self.fields = [QLineEdit(settings.dns[index] if index < len(settings.dns) else "", self) for index in range(4)]
        for index, field in enumerate(self.fields):
            field.setAccessibleName(("Preferred DNS", "Alternate DNS", "Third DNS", "Fourth DNS")[index])
            field.setPlaceholderText("IPv4 or IPv6 address")
            self.content_layout.addWidget(field)
            field.textChanged.connect(self._check)
        self.problem = fluent.Label("", "caption", self, tone="danger", wrap=True)
        self.content_layout.addWidget(self.problem)
        self.mode.setCurrentIndex(1 if settings.manual_dns or settings.manual else 0)
        self.mode.currentIndexChanged.connect(self._check)
        self._check()

    def manual(self) -> bool:
        return bool(self.mode.currentData())

    def servers(self) -> list[str]:
        return [field.text().strip() for field in self.fields if field.text().strip()]

    def _check(self, *_args) -> None:
        for field in self.fields:
            field.setEnabled(self.manual())
        problem = netconfig.dns_problem(self.servers()) if self.manual() else ""
        typed = bool(self.servers())
        self.problem.setText(problem if typed else "")
        self.problem.setVisible(bool(problem and typed))
        self.primary_button.setEnabled(not problem)

    def words(self) -> tuple[str, ...]:
        return netconfig.dns_words(self.servers() if self.manual() else None)


class WiredAdapter(QWidget):
    """The settings and properties of one wired adapter."""

    def __init__(self, section: "WiredSection", device: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.section, self.device = section, device
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(4)
        self.heading = fluent.Label(device, "body-strong", self)
        layout.addWidget(self.heading)
        self.ip_button = fluent.Button("Edit", self)
        self.ip_button.clicked.connect(lambda: section.edit_ip(device))
        self.ip = fluent.SettingsCard("IP assignment", "", fluent.icon("network-wired-symbolic"), self.ip_button, self)
        self.dns_button = fluent.Button("Edit", self)
        self.dns_button.clicked.connect(lambda: section.edit_dns(device))
        self.dns = fluent.SettingsCard("DNS server assignment", "",
                                       fluent.icon("network-server-symbolic", "globe-symbolic"), self.dns_button, self)
        self.metered_switch = fluent.ToggleSwitch()
        self.metered_switch.clicked.connect(lambda on: section.set_metered(device, bool(on)))
        self.metered = fluent.SettingsCard("Metered connection", METERED_TEXT,
                                           fluent.icon("network-cellular-symbolic", "network-wired-symbolic"),
                                           self.metered_switch, self)
        self.missing = fluent.SettingsCard("No settings for this adapter yet",
                                           "NetworkManager makes a connection for it when a network cable is "
                                           "plugged in.", fluent.icon("network-wired-symbolic"), None, self)
        self.copy_button = fluent.Button("Copy", self, icon=fluent.icon("edit-copy-symbolic"))
        self.copy_button.setToolTip("Copy the properties as text")
        self.copy_button.clicked.connect(lambda: section.copy(device))
        self.properties = fluent.SettingsExpander("Properties", "", fluent.icon("dialog-information-symbolic"),
                                                  self.copy_button, self)
        self.rows: list[fluent.SettingsCard] = []
        for widget in (self.ip, self.dns, self.metered, self.missing, self.properties):
            layout.addWidget(widget)

    def update_state(self, device: sysinfo.NetDevice, settings: netconfig.WiredSettings | None,
                     details: netconfig.WiredDetails, busy: bool) -> None:
        self.heading.setText(f"{device.device} · {settings.name if settings else device.state_label}")
        for card in (self.ip, self.dns, self.metered):
            card.setVisible(settings is not None)
        self.missing.setVisible(settings is None)
        if settings is not None:
            self.ip.set_description(netconfig.ip_assignment_text(settings))
            self.dns.set_description(netconfig.dns_assignment_text(settings))
            if self.metered_switch.isChecked() != (settings.metered == "yes"):
                self.metered_switch.setChecked(settings.metered == "yes")
        for widget in (self.ip_button, self.dns_button, self.metered_switch):
            widget.setEnabled(not busy and settings is not None)
            widget.setAccessibleName({self.ip_button: f"Edit the IP assignment of {device.device}",
                                      self.dns_button: f"Edit the DNS servers of {device.device}",
                                      self.metered_switch: f"Metered connection on {device.device}"}[widget])
        rows = netconfig.detail_rows(details)
        while len(self.rows) < len(rows):
            self.rows.append(self.properties.add_row("", ""))
        for index, row in enumerate(self.rows):
            row.setVisible(index < len(rows))
            if index < len(rows):
                row.set_title(rows[index][0])
                row.set_description(rows[index][1])
        self.properties.header.set_description(
            f"{device.state_label}" + (f" · {netconfig.netinfo.speed_text(details.speed)}" if details.speed else ""))
        self.copy_button.setEnabled(bool(rows))


class WiredSection(ToolSection):
    page = "ethernet"
    title = "Wired connection"
    order = 20
    keywords = ("ip address", "static ip", "manual ip", "dhcp", "dns", "dns server", "gateway", "subnet", "metered",
                "mac address", "link speed", "ethernet settings")

    def __init__(self, parent: QWidget | None = None, *, runner=None, root: str | None = None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.root = context.root if root is None else root
        self.list = self.add(RowList(self))
        self.empty = self.add(fluent.SettingsCard("No wired network adapter was found", "", None, None, self))
        self.empty.hide()
        self.state: dict[str, tuple] = {}
        self.dialog: fluent.ContentDialog | None = None

    def read(self):
        if not self.runner.available(commands.EXECUTABLES["nmcli"]):
            raise Failed("NetworkManager's nmcli is not installed.")

        def run(*words: str) -> str:
            result = self.runner.run(commands.argv("nmcli", "-t", *words), READ_TIMEOUT)
            if result.code != 0:
                raise tool_failure("Reading the wired connection", result)
            return result.stdout

        devices = [d for d in sysinfo.parse_nmcli_devices(run("-f", "DEVICE,TYPE,STATE,CONNECTION", "device"))
                   if d.kind == "ethernet"]
        connections = netconfig.parse_wired_connections(run("-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "connection", "show"))
        root = probes.Root(self.root)
        state = {}
        for device in devices:
            profile = netconfig.profile_for(device.device, connections)
            settings = None
            if profile is not None:
                shown = run("-f", netconfig.WIRED_FIELDS, "connection", "show", "uuid", profile.uuid)
                settings = netconfig.parse_wired_settings(shown, profile.uuid, profile.name)
            try:
                shown = run("-f", netconfig.DEVICE_FIELDS, "device", "show", device.device)
            except (Failed, ValueError):
                shown = ""
            details = netconfig.parse_device_details(shown, netconfig.link_speed(root, device.device))
            state[device.device] = (device, settings, details, profile)
        return state

    def show_state(self, state) -> None:
        self.state = state
        self.show_notice("")
        self.list.sync(list(state), lambda name: WiredAdapter(self, name, self.list),
                       lambda name, row: row.update_state(*state[name][:3], self.busy))
        self.empty.setVisible(not state)

    def load_failed(self, exc: Exception) -> None:
        self.list.sync([], lambda _k: None, lambda _k, _r: None)
        self.show_notice(f"The wired connection's settings are not available: {exc}", "warning")

    def set_enabled(self, enabled: bool) -> None:
        for name in self.list.keys():
            device, settings, details, _profile = self.state[name]
            self.list.row(name).update_state(device, settings, details, not enabled)

    def deactivate(self) -> None:
        super().deactivate()
        if self.dialog is not None and self.dialog.is_open():
            self.dialog.reject()

    def _settings(self, device: str) -> tuple[netconfig.WiredSettings, netconfig.WiredConnection] | None:
        entry = self.state.get(device)
        if entry is None or entry[1] is None or entry[3] is None:
            return None
        return entry[1], entry[3]

    def _open(self, dialog: fluent.ContentDialog, device: str, what: str) -> None:
        self.dialog = dialog

        def finished(result) -> None:
            words = dialog.words() if result == fluent.ContentDialog.Result.PRIMARY else None
            dialog.deleteLater()
            self.dialog = None
            if words:
                self.modify(device, words, what)

        dialog.finished.connect(finished)
        dialog.open()

    def edit_ip(self, device: str) -> None:
        found = self._settings(device)
        if found is None or self.busy or (self.dialog is not None and self.dialog.is_open()):
            return
        self._open(IpDialog(self, found[0]), device, "The IP settings")

    def edit_dns(self, device: str) -> None:
        found = self._settings(device)
        if found is None or self.busy or (self.dialog is not None and self.dialog.is_open()):
            return
        self._open(DnsDialog(self, found[0]), device, "The DNS servers")

    def set_metered(self, device: str, metered: bool) -> None:
        if self.rendering:
            return
        self.modify(device, netconfig.metered_words(metered), "Metered connection")

    def modify(self, device: str, words: tuple[str, ...], what: str) -> None:
        """Change the profile the adapter uses; when it is active, apply it again so that the change counts now."""
        found = self._settings(device)
        if found is None:
            return
        settings, profile = found
        try:
            change = commands.nmcli_change_argv("connection", "modify", "uuid", profile.uuid, *words)
        except ValueError as exc:
            self.show_error(f"{what} could not be changed: {exc}")
            return

        def apply() -> None:
            result = self.runner.run(change, NMCLI_CHANGE_TIMEOUT)
            if result.code != 0:
                raise nmcli_failure(f"Changing {settings.name}", result)
            if profile.active:
                result = self.runner.run(commands.nmcli_change_argv("connection", "up", "uuid", profile.uuid),
                                         NMCLI_CHANGE_TIMEOUT)
                if result.code != 0:
                    raise nmcli_failure(f"Applying the change to {settings.name}", result)

        self.change(apply, f"{what}: saved for {settings.name}. " +
                    ("The connection was started again to use the change." if profile.active else
                     "The change is used when the connection starts."))

    def copy(self, device: str) -> None:
        entry = self.state.get(device)
        if entry is None:
            return
        rows = netconfig.detail_rows(entry[2])
        if rows:
            QGuiApplication.clipboard().setText(f"Ethernet ({device})\n" +
                                                "\n".join(f"  {label}: {value}" for label, value in rows))
            self.show_notice("Copied the properties to the clipboard.", "success")


SECTIONS = (VpnSection, AdaptersSection, WiredSection)
