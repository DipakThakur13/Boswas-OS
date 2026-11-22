"""VPN connections and network adapters of NetworkManager, read-only and without Qt.

  nmcli -t -f NAME,UUID,TYPE,DEVICE,ACTIVE connection show
                          the connections; VPN ones are of type vpn (a plugin:
                          OpenVPN, ...) or wireguard
  nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device
                          the adapters (sysinfo.parse_nmcli_devices)
  /sys/class/net/<if>/statistics/rx_bytes, tx_bytes
                          the data an adapter moved since the device started
  /sys/class/net/<if>/address
                          its hardware address, read only when asked for

A file to import is recognised by its content (import_kind): a WireGuard
configuration has an [Interface] section with a PrivateKey; an OpenVPN one
has "client", "remote" or inline certificates. NetworkManager names a
WireGuard connection after its file, which must then be a valid interface
name (at most 15 characters).

The changes themselves (connect, disconnect, remove, import, an adapter on
or off) are built by commands.nmcli_change_argv and asked of NetworkManager,
which asks polkit. No secret is ever an argument.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass

from . import sysinfo
from . import viewmodel as vm

NET_SYS = "/sys/class/net"
MAX_CONNECTIONS = 500
MAX_IMPORT = 1024 * 1024
VPN_TYPES = {"vpn": "VPN", "wireguard": "WireGuard"}
WIREGUARD_NAME_RE = re.compile(r"^[A-Za-z0-9_=+.-]{1,15}$")
SECRETS_WORDS = ("secrets were required", "no secrets", "secrets were not provided", "no agents were available")


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
