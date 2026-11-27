"""VPN connections, network adapters and wired connections of NetworkManager, read-only and without Qt.

  nmcli -t -f NAME,UUID,TYPE,DEVICE,ACTIVE connection show
                          the connections; VPN ones are of type vpn (a plugin:
                          OpenVPN, ...) or wireguard
  nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device
                          the adapters (sysinfo.parse_nmcli_devices)
  /sys/class/net/<if>/statistics/rx_bytes, tx_bytes
                          the data an adapter moved since the device started
  /sys/class/net/<if>/address
                          its hardware address, read only when asked for
  nmcli -t -f WIRED_FIELDS connection show uuid UUID
                          a wired connection's settings: IP assignment
                          (ipv4.method, addresses, gateway), DNS servers and
                          whether they replace the network's, metered
  nmcli -t -f DEVICE_FIELDS device show IFNAME
                          what the adapter has now: hardware address, IPv4
                          and IPv6 addresses, gateways, DNS servers
  /sys/class/net/<if>/speed
                          its link speed (netinfo.parse_speed)

nmcli's terse output puts a backslash before a colon in a value (IPv6
addresses, hardware addresses); values with several items separate them by
commas.

A file to import is recognised by its content (import_kind): a WireGuard
configuration has an [Interface] section with a PrivateKey; an OpenVPN one
has "client", "remote" or inline certificates. NetworkManager names a
WireGuard connection after its file, which must then be a valid interface
name (at most 15 characters).

The changes themselves (connect, disconnect, remove, import, an adapter on
or off, a wired connection's IP assignment, DNS servers and metered state:
ip_words, dns_words, metered_words) are built by commands.nmcli_change_argv
and asked of NetworkManager, which asks polkit. No secret is ever an argument.
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
from dataclasses import dataclass

from . import commands, netinfo, sysinfo
from . import viewmodel as vm

NET_SYS = "/sys/class/net"
MAX_CONNECTIONS = 500
MAX_IMPORT = 1024 * 1024
VPN_TYPES = {"vpn": "VPN", "wireguard": "WireGuard"}
WIREGUARD_NAME_RE = re.compile(r"^[A-Za-z0-9_=+.-]{1,15}$")
SECRETS_WORDS = ("secrets were required", "no secrets", "secrets were not provided", "no agents were available")
ETHERNET_TYPE = "802-3-ethernet"
WIRED_FIELDS = ("connection.id,connection.uuid,connection.metered,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,"
                "ipv4.ignore-auto-dns,ipv6.dns,ipv6.ignore-auto-dns")
DEVICE_FIELDS = "GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.GATEWAY,IP6.DNS"
MAX_FIELD_LINES = 400


@dataclass(frozen=True)
class Connection:
    name: str
    uuid: str
    kind: str                # vpn or wireguard (others are not kept)
    device: str
    active: bool

    @property
    def kind_label(self) -> str:
        return VPN_TYPES.get(self.kind, "VPN")


def parse_connections(text: str) -> list[Connection]:
    """The VPN and WireGuard connections of `nmcli -t -f NAME,UUID,TYPE,DEVICE,ACTIVE connection show`."""
    from .commands import NMCLI_UUID_RE
    found = []
    for line in (text or "").splitlines()[:MAX_CONNECTIONS]:
        parts = (sysinfo.split_terse(line) + ["", "", "", "", ""])[:5]
        name, uuid, kind, device, active = parts
        if kind not in VPN_TYPES or not NMCLI_UUID_RE.fullmatch(uuid):
            continue
        found.append(Connection(vm.one_line(name, 120) or uuid, uuid, kind,
                                "" if device in ("", "--") else vm.one_line(device, 40),
                                active.strip() == "yes"))
    found.sort(key=lambda c: (not c.active, c.name.casefold()))
    return found


def import_kind(path: str) -> tuple[str, str]:
    """("openvpn" | "wireguard", "") for a file NetworkManager can import, else ("", why not)."""
    try:
        with open(path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW)) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return "", "This is not a regular file."
            if info.st_size > MAX_IMPORT:
                return "", "This file is too large to be a VPN configuration."
            text = handle.read(MAX_IMPORT).decode("utf-8", "replace")
    except OSError as exc:
        return "", f"The file could not be opened ({exc.strerror or exc})."
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith(("#", ";"))]
    lowered = [line.casefold() for line in lines]
    if "[interface]" in lowered and any(line.startswith("privatekey") for line in lowered):
        stem = os.path.splitext(os.path.basename(path))[0]
        if not WIREGUARD_NAME_RE.fullmatch(stem):
            return "", ("NetworkManager names a WireGuard connection after its file, and this file's name is not a "
                        "valid interface name. Rename the file to at most 15 letters, digits, - or _ (for example "
                        "wg-office.conf), then try again.")
        return "wireguard", ""
    if any(line == "client" or line.startswith(("remote ", "<ca>", "<cert>", "dev tun", "dev tap", "auth-user-pass"))
           for line in lowered):
        return "openvpn", ""
    return "", "This file is not a WireGuard or OpenVPN configuration."


def needs_secrets(text: str) -> bool:
    return any(word in (text or "").casefold() for word in SECRETS_WORDS)


@dataclass(frozen=True)
class Traffic:
    received: int | None
    sent: int | None


def traffic(root, device: str) -> Traffic:
    """The bytes an adapter received and sent since the device started (``root``: a probes.Root)."""
    if not sysinfo.valid_device(device):
        return Traffic(None, None)

    def number(name: str) -> int | None:
        text = (root.read(f"{NET_SYS}/{device}/statistics/{name}", 64) or "").strip()
        return int(text) if text.isdigit() else None

    return Traffic(number("rx_bytes"), number("tx_bytes"))


def hardware_address(root, device: str) -> str:
    """The hardware (MAC) address of an adapter, "" when it has none or it cannot be read."""
    if not sysinfo.valid_device(device):
        return ""
    value = (root.read(f"{NET_SYS}/{device}/address", 64) or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", value) else ""


def traffic_text(value: Traffic) -> str:
    if value.received is None and value.sent is None:
        return "Data used: not known"
    return (f"Received {vm.format_bytes(value.received or 0)} · sent {vm.format_bytes(value.sent or 0)} "
            "since this device started")


# --- wired connections ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WiredConnection:
    """A wired (Ethernet) connection profile of `nmcli -t -f NAME,UUID,TYPE,DEVICE,ACTIVE connection show`."""

    name: str
    uuid: str
    device: str
    active: bool


def parse_wired_connections(text: str) -> list[WiredConnection]:
    """The Ethernet connection profiles, active ones first, in nmcli's order otherwise."""
    found = []
    for line in (text or "").splitlines()[:MAX_CONNECTIONS]:
        name, uuid, kind, device, active = (sysinfo.split_terse(line) + ["", "", "", "", ""])[:5]
        if kind != ETHERNET_TYPE or not commands.NMCLI_UUID_RE.fullmatch(uuid):
            continue
        found.append(WiredConnection(vm.one_line(name, 120) or uuid, uuid,
                                     "" if device in ("", "--") else vm.one_line(device, 40), active.strip() == "yes"))
    found.sort(key=lambda c: not c.active)
    return found


def profile_for(device: str, connections: list[WiredConnection]) -> WiredConnection | None:
    """The profile an adapter uses: the one active on it, else the first that is not active elsewhere."""
    for connection in connections:
        if connection.device == device:
            return connection
    return next((c for c in connections if not c.active and not c.device), None)


def parse_fields(text: str) -> dict[str, list[str]]:
    """``FIELD:value`` lines of nmcli's terse output: field -> values (FIELD[1], FIELD[2] gather under FIELD)."""
    fields: dict[str, list[str]] = {}
    for line in (text or "").splitlines()[:MAX_FIELD_LINES]:
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key = re.sub(r"\[[0-9]+\]$", "", key.strip())
        value = value.replace("\\:", ":").replace("\\\\", "\\").strip()
        if value and value != "--":
            fields.setdefault(key, []).append(vm.one_line(value, 400))
    return fields


def _items(values: list[str], version: int | None = None) -> tuple[str, ...]:
    """The comma-separated items of a field (only valid addresses of ``version`` when it is given)."""
    items = [item.strip() for value in values for item in value.split(",") if item.strip()]
    if version is not None:
        items = [item for item in items if _address(item.split("/")[0]) == version]
    return tuple(dict.fromkeys(items))[:16]


def _address(text: str) -> int:
    try:
        return ipaddress.ip_address(text).version
    except ValueError:
        return 0


@dataclass(frozen=True)
class WiredSettings:
    """What a wired connection is set to (its profile, not what it has now)."""

    uuid: str
    name: str
    method: str = "auto"                  # auto (DHCP), manual, or what else NetworkManager knows
    addresses: tuple[str, ...] = ()       # ADDRESS/PREFIX
    gateway: str = ""
    dns4: tuple[str, ...] = ()
    dns6: tuple[str, ...] = ()
    ignore_auto_dns: bool = False
    metered: str = "unknown"              # yes, no, unknown (NetworkManager guesses)

    @property
    def manual(self) -> bool:
        return self.method == "manual"

    @property
    def dns(self) -> tuple[str, ...]:
        return self.dns4 + self.dns6

    @property
    def manual_dns(self) -> bool:
        return bool(self.dns)


def _first(fields: dict[str, list[str]], key: str, default: str = "") -> str:
    return (fields.get(key) or [default])[0]


def _gateway(fields: dict[str, list[str]], key: str, version: int) -> str:
    value = _first(fields, key)
    return value if _address(value) == version else ""


def parse_wired_settings(text: str, uuid: str = "", name: str = "") -> WiredSettings:
    """A wired connection's settings from `nmcli -t -f WIRED_FIELDS connection show uuid UUID`."""
    fields = parse_fields(text)
    found = _first(fields, "connection.uuid", uuid)
    metered = _first(fields, "connection.metered", "unknown").split(" ")[0]
    return WiredSettings(
        uuid=found if commands.NMCLI_UUID_RE.fullmatch(found) else uuid,
        name=_first(fields, "connection.id", name) or name,
        method=_first(fields, "ipv4.method", "auto"),
        addresses=_items(fields.get("ipv4.addresses", []), 4),
        gateway=_gateway(fields, "ipv4.gateway", 4),
        dns4=_items(fields.get("ipv4.dns", []), 4),
        dns6=_items(fields.get("ipv6.dns", []), 6),
        ignore_auto_dns=_first(fields, "ipv4.ignore-auto-dns", "no") == "yes",
        metered=metered if metered in ("yes", "no") else "unknown")


@dataclass(frozen=True)
class WiredDetails:
    """What the adapter has now."""

    mac: str = ""
    ipv4: tuple[str, ...] = ()
    gateway4: str = ""
    ipv6: tuple[str, ...] = ()
    gateway6: str = ""
    dns: tuple[str, ...] = ()
    speed: int | None = None


def parse_device_details(text: str, speed: int | None = None) -> WiredDetails:
    """What an adapter has now, from `nmcli -t -f DEVICE_FIELDS device show IFNAME` and its link speed."""
    fields = parse_fields(text)
    mac = _first(fields, "GENERAL.HWADDR").lower()
    return WiredDetails(
        mac=mac if re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac) else "",
        ipv4=_items(fields.get("IP4.ADDRESS", []), 4), gateway4=_gateway(fields, "IP4.GATEWAY", 4),
        ipv6=_items(fields.get("IP6.ADDRESS", []), 6), gateway6=_gateway(fields, "IP6.GATEWAY", 6),
        dns=_items(fields.get("IP4.DNS", []), 4) + _items(fields.get("IP6.DNS", []), 6),
        speed=speed)


def link_speed(root, device: str) -> int | None:
    """The link speed in Mb/s from /sys/class/net/<if>/speed (``root``: a probes.Root)."""
    if not sysinfo.valid_device(device):
        return None
    return netinfo.parse_speed(root.read(f"{NET_SYS}/{device}/speed", 32))


def ip_assignment_text(settings: WiredSettings) -> str:
    if settings.manual:
        parts = [", ".join(settings.addresses) or "no address"]
        if settings.gateway:
            parts.append(f"gateway {settings.gateway}")
        return "Manual: " + " · ".join(parts)
    if settings.method == "auto":
        return "Automatic (DHCP)"
    return f"Set to “{vm.one_line(settings.method, 30)}” in NetworkManager"


def dns_assignment_text(settings: WiredSettings) -> str:
    if settings.manual_dns:
        return "Manual: " + ", ".join(settings.dns) + ("" if settings.ignore_auto_dns or settings.manual
                                                       else " (and the network's)")
    return "Automatic (DHCP)" if not settings.manual else "None"


def detail_rows(details: WiredDetails) -> list[tuple[str, str]]:
    """(label, value) of the adapter's properties, as shown and copied."""
    rows = []
    if details.speed is not None:
        rows.append(("Link speed", netinfo.speed_text(details.speed)))
    if details.ipv4:
        rows.append(("IPv4 address", ", ".join(details.ipv4)))
    if details.gateway4:
        rows.append(("IPv4 gateway", details.gateway4))
    if details.ipv6:
        rows.append(("IPv6 address", ", ".join(details.ipv6)))
    if details.gateway6:
        rows.append(("IPv6 gateway", details.gateway6))
    if details.dns:
        rows.append(("DNS servers", ", ".join(details.dns)))
    if details.mac:
        rows.append(("Physical address (MAC)", details.mac))
    return rows


def ipv4_problem(address: str, prefix: str, gateway: str) -> str:
    """Why a manual IPv4 address, prefix length and gateway cannot be used ("" when they can)."""
    address, prefix, gateway = address.strip(), prefix.strip(), gateway.strip()
    if not commands.valid_ip_address(address, 4):
        return "Type an IPv4 address of this computer, such as 192.168.1.20."
    if not (prefix.isascii() and prefix.isdigit() and 1 <= int(prefix) <= 32):
        return "The subnet prefix length is a number from 1 to 32 (24 for a mask of 255.255.255.0)."
    if not commands.valid_ipv4_interface(f"{address}/{int(prefix)}"):
        return "This address is the network's own address or its broadcast address; choose another."
    if gateway:
        if not commands.valid_ip_address(gateway, 4):
            return "The gateway is an IPv4 address, such as 192.168.1.1."
        network = ipaddress.ip_interface(f"{address}/{int(prefix)}").network
        if ipaddress.ip_address(gateway) not in network:
            return f"The gateway must be in the same network as the address ({network})."
        if gateway == address:
            return "The gateway is another device: it cannot be this computer's own address."
    return ""


def dns_problem(servers: list[str]) -> str:
    """Why a list of DNS servers cannot be used ("" when it can): IPv4 or IPv6 addresses, three of each at most."""
    servers = [server.strip() for server in servers if server.strip()]
    if not servers:
        return "Type at least one DNS server."
    for server in servers:
        if not (commands.valid_ip_address(server, 4) or commands.valid_ip_address(server, 6)):
            return f"“{vm.one_line(server, 60)}” is not an IPv4 or IPv6 address."
    if len(set(servers)) != len(servers):
        return "A server is named twice."
    for version in (4, 6):
        if sum(1 for server in servers if _address(server) == version) > commands.MAX_DNS_SERVERS:
            return f"At most {commands.MAX_DNS_SERVERS} servers of each kind (IPv4, IPv6)."
    return ""


def dns_words(servers: list[str] | None) -> tuple[str, ...]:
    """The properties for DNS servers: automatic (None or empty) or exactly these (the network's are not used)."""
    servers = [server.strip() for server in servers or [] if server.strip()]
    if not servers:
        return ("ipv4.dns", "", "ipv4.ignore-auto-dns", "no", "ipv6.dns", "", "ipv6.ignore-auto-dns", "no")
    if dns_problem(servers):
        raise ValueError(dns_problem(servers))
    v4 = ",".join(server for server in servers if _address(server) == 4)
    v6 = ",".join(server.lower() for server in servers if _address(server) == 6)
    return ("ipv4.dns", v4, "ipv4.ignore-auto-dns", "yes", "ipv6.dns", v6, "ipv6.ignore-auto-dns", "yes")


def ip_words(manual: bool, address: str = "", prefix: str = "", gateway: str = "",
             dns: list[str] | None = None) -> tuple[str, ...]:
    """The properties for the IP assignment: automatic (DHCP; the address and gateway set by hand are removed),
    or manual with an address, a prefix length, a gateway and the DNS servers (a manual address has no DHCP to
    ask for them)."""
    if not manual:
        return ("ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "")
    problem = ipv4_problem(address, prefix, gateway)
    if problem:
        raise ValueError(problem)
    words = ("ipv4.method", "manual", "ipv4.addresses", f"{address.strip()}/{int(prefix)}", "ipv4.gateway",
             gateway.strip())
    return words + (dns_words(dns) if dns else ())


def metered_words(metered: bool) -> tuple[str, ...]:
    return ("connection.metered", "yes" if metered else "no")
