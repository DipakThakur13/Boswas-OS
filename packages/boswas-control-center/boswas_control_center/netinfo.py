"""Connection details and the session's proxy settings, read-only and without Qt.

Sources:

  ip -j addr show            the interfaces, their MAC and IP addresses (iproute2, JSON)
  ip -j -4 route show        the IPv4 default gateway of each interface
  ip -j -6 route show        the IPv6 default gateway of each interface
  /etc/resolv.conf           the DNS servers and search domains
  /sys/class/net/<if>/speed  the link speed of a wired interface (Mb/s)
  the environment            http_proxy, https_proxy, ftp_proxy, all_proxy, no_proxy

Everything read is treated as text from outside: sizes and counts are capped,
names and addresses are validated, and a document that is not what ip prints
gives an empty result instead of an exception. A password inside a proxy URL
is never shown.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass

MAX_INTERFACES = 64
MAX_ADDRESSES = 16
MAX_DNS = 8
RESOLV_CONF = "/etc/resolv.conf"
NET_SYS = "/sys/class/net"
IFNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,31}$")
MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
PROXY_VARIABLES = ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy")
PROXY_LABELS = {"http_proxy": "HTTP proxy", "https_proxy": "HTTPS proxy", "ftp_proxy": "FTP proxy",
                "all_proxy": "Proxy for everything else", "no_proxy": "No proxy for"}


@dataclass(frozen=True)
class Interface:
    name: str
    kind: str = "Network"            # Wi-Fi, Ethernet, or Network for anything else
    mac: str = ""
    up: bool = False
    ipv4: tuple[str, ...] = ()       # address/prefix
    ipv6: tuple[str, ...] = ()
    gateway4: str = ""
    gateway6: str = ""
    speed: int | None = None         # Mb/s, wired interfaces only

    @property
    def connected(self) -> bool:
        return self.up and bool(self.ipv4 or self.ipv6)


@dataclass(frozen=True)
class Details:
    interfaces: tuple[Interface, ...] = ()
    dns: tuple[str, ...] = ()
    search: tuple[str, ...] = ()


def _address(value: object) -> str:
    """A valid IPv4 or IPv6 address as text, else ""."""
    if not isinstance(value, str) or len(value) > 64:
        return ""
    try:
        return str(ipaddress.ip_address(value.split("%")[0]))
    except ValueError:
        return ""


def _load(text: object) -> list[dict]:
    if not isinstance(text, str) or len(text) > 4 * 1024 * 1024:
        return []
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return []
    return [item for item in doc if isinstance(item, dict)] if isinstance(doc, list) else []


def interface_kind(name: str, wireless: bool = False) -> str:
    if wireless or name.startswith(("wl", "wifi")):
        return "Wi-Fi"
    if name.startswith(("en", "eth")):
        return "Ethernet"
    return "Network"


def parse_addresses(text: object) -> list[Interface]:
    """The Ethernet-like interfaces of `ip -j addr show` (no loopback), with their addresses."""
    found: list[Interface] = []
    for item in _load(text)[:MAX_INTERFACES * 4]:
        name = item.get("ifname")
        if not isinstance(name, str) or not IFNAME_RE.fullmatch(name) or item.get("link_type") != "ether":
            continue
        flags = item.get("flags") if isinstance(item.get("flags"), list) else []
        mac = item.get("address") if isinstance(item.get("address"), str) and MAC_RE.fullmatch(item["address"]) \
            else ""
        v4, v6 = [], []
        infos = item.get("addr_info") if isinstance(item.get("addr_info"), list) else []
        for info in infos[:MAX_ADDRESSES * 4]:
            if not isinstance(info, dict):
                continue
            address = _address(info.get("local"))
            prefix = info.get("prefixlen")
            if not address or isinstance(prefix, bool) or not isinstance(prefix, int) or not 0 <= prefix <= 128:
                continue
            if info.get("family") == "inet" and len(v4) < MAX_ADDRESSES:
                v4.append(f"{address}/{prefix}")
            elif info.get("family") == "inet6" and len(v6) < MAX_ADDRESSES:
                v6.append(f"{address}/{prefix}")
        up = item.get("operstate") == "UP" or ("LOWER_UP" in flags and "UP" in flags
                                               and item.get("operstate") == "UNKNOWN")
        found.append(Interface(name, interface_kind(name), mac.lower(), up, tuple(v4), tuple(v6)))
        if len(found) >= MAX_INTERFACES:
            break
    return found


def parse_gateways(text: object) -> dict[str, str]:
    """{interface: gateway} from the default routes of `ip -j route show` (the first, lowest-metric one wins)."""
    gateways: dict[str, tuple[int, str]] = {}
    for item in _load(text)[:4096]:
        if item.get("dst") != "default":
            continue
        device, gateway = item.get("dev"), _address(item.get("gateway"))
        if not isinstance(device, str) or not IFNAME_RE.fullmatch(device) or not gateway:
            continue
        metric = item.get("metric") if isinstance(item.get("metric"), int) and not isinstance(item.get("metric"), bool) \
            else 0
        if device not in gateways or metric < gateways[device][0]:
            gateways[device] = (metric, gateway)
    return {device: gateway for device, (_metric, gateway) in gateways.items()}


def parse_resolv_conf(text: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(name servers, search domains) of resolv.conf."""
    servers: list[str] = []
    search: list[str] = []
    if not isinstance(text, str):
        return (), ()
    for line in text.splitlines()[:500]:
        words = line.split("#", 1)[0].split(";", 1)[0].split()
        if len(words) >= 2 and words[0] == "nameserver":
            address = _address(words[1])
            if address and address not in servers and len(servers) < MAX_DNS:
                servers.append(address)
        elif words and words[0] in ("search", "domain"):
            search = [w for w in words[1:MAX_DNS + 1] if re.fullmatch(r"[A-Za-z0-9._-]{1,253}", w)]
    return tuple(servers), tuple(search)


def parse_speed(text: object) -> int | None:
    """The link speed in Mb/s from /sys/class/net/<if>/speed; None when unknown (-1) or unreadable."""
    try:
        speed = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return speed if 0 < speed <= 1_000_000 else None


def combine(addresses: list[Interface], gateways4: dict[str, str], gateways6: dict[str, str],
            speeds: dict[str, int | None] | None = None, wireless: set[str] | None = None) -> tuple[Interface, ...]:
    """The interfaces with gateways, speeds and kinds filled in: connected ones first, then by name."""
    speeds, wireless = speeds or {}, wireless or set()
    result = []
    for item in addresses:
        kind = interface_kind(item.name, item.name in wireless)
        result.append(Interface(item.name, kind, item.mac, item.up, item.ipv4, item.ipv6,
                                gateways4.get(item.name, ""), gateways6.get(item.name, ""),
                                speeds.get(item.name) if kind != "Wi-Fi" else None))
    return tuple(sorted(result, key=lambda i: (not i.connected, i.kind != "Ethernet", i.name)))


def speed_text(speed: int | None) -> str:
    if speed is None:
        return ""
    return f"{speed / 1000:g} Gb/s" if speed >= 1000 else f"{speed} Mb/s"


def interface_rows(item: Interface, details: Details) -> list[tuple[str, str]]:
    """(label, value) rows of one interface, as shown and as copied."""
    rows = [("Status", "Connected" if item.connected else ("No address" if item.up else "Not connected"))]
    if item.ipv4:
        rows.append(("IPv4 address", ", ".join(item.ipv4)))
    if item.gateway4:
        rows.append(("IPv4 gateway", item.gateway4))
    if item.ipv6:
        rows.append(("IPv6 address", ", ".join(item.ipv6)))
    if item.gateway6:
        rows.append(("IPv6 gateway", item.gateway6))
    if item.connected and details.dns:
        rows.append(("DNS servers", ", ".join(details.dns)))
    if item.connected and details.search:
        rows.append(("Search domains", ", ".join(details.search)))
    if item.mac:
        rows.append(("Physical address (MAC)", item.mac))
    if item.speed is not None:
        rows.append(("Link speed", speed_text(item.speed)))
    return rows


def details_text(details: Details) -> str:
    """Everything shown, as plain text for the clipboard."""
    blocks = []
    for item in details.interfaces:
        lines = [f"{item.kind} ({item.name})"]
        lines += [f"  {label}: {value}" for label, value in interface_rows(item, details)]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def read(runner, argv, root: str = "/") -> Details:
    """Read everything (blocking: call it from a worker thread).

    ``runner`` is a commands.CommandRunner and ``argv`` commands.argv; ip
    not being installed raises the runner's CommandUnavailable.
    """
    def run(*args: str) -> str:
        result = runner.run(argv("ip", *args), 10.0)
        return result.stdout if result.code == 0 else ""

    addresses = parse_addresses(run("-j", "addr", "show"))
    gateways4 = parse_gateways(run("-j", "-4", "route", "show"))
    gateways6 = parse_gateways(run("-j", "-6", "route", "show"))
    base = root.rstrip("/")
    speeds: dict[str, int | None] = {}
    wireless: set[str] = set()
    for item in addresses:
        try:
            with open(f"{base}{NET_SYS}/{item.name}/speed", "rb") as handle:
                speeds[item.name] = parse_speed(handle.read(32).decode("ascii", "replace"))
        except OSError:
            speeds[item.name] = None
        try:
            with open(f"{base}{NET_SYS}/{item.name}/uevent", "rb") as handle:
                if b"DEVTYPE=wlan" in handle.read(4096):
                    wireless.add(item.name)
        except OSError:
            pass
    try:
        with open(f"{base}{RESOLV_CONF}", "rb") as handle:
            dns, search = parse_resolv_conf(handle.read(65536).decode("utf-8", "replace"))
    except OSError:
        dns, search = (), ()
    return Details(combine(addresses, gateways4, gateways6, speeds, wireless), dns, search)


# --- proxy ---------------------------------------------------------------------------------------------------

_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)?([^/@\s]*)@")


def mask_proxy(value: str) -> str:
    """A proxy URL without its password: "http://user:secret@host:3128" -> "http://user:***@host:3128"."""
    value = "".join(ch for ch in value if ch.isprintable())[:2048].strip()
    match = _USERINFO_RE.match(value)
    if match is None:
        return value
    user = match.group(2).split(":", 1)[0]
    hidden = f"{user}:***" if ":" in match.group(2) else user
    return f"{match.group(1) or ''}{hidden}@{value[match.end():]}"


def proxy_settings(environ) -> list[tuple[str, str]]:
    """(label, value) of the proxy variables set in the environment (lower case wins over upper case)."""
    rows = []
    for name in PROXY_VARIABLES:
        value = environ.get(name) or environ.get(name.upper()) or ""
        if isinstance(value, str) and value.strip():
            shown = value.strip()[:2048] if name == "no_proxy" else mask_proxy(value)
            rows.append((PROXY_LABELS[name], "".join(ch for ch in shown if ch.isprintable())))
    return rows
