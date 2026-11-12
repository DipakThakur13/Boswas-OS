"""Disks, network and load as Control Center shows them (pure Python, no Qt, no I/O).

  lsblk -J -b -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,TRAN,RM,RO   (no serial numbers)
  nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device
  nmcli -t -f IP4.ADDRESS,IP4.GATEWAY,IP6.ADDRESS device show DEVICE
  nmcli -t radio wifi
  /proc/stat (two samples) and /proc/meminfo
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import viewmodel as vm

# --- disks -------------------------------------------------------------------------------------------------

TRANSPORTS = {"nvme": "NVMe", "sata": "SATA", "usb": "USB", "ata": "ATA", "scsi": "SCSI", "mmc": "SD/MMC",
              "virtio": "Virtual disk", "spi": "SPI"}


@dataclass(frozen=True)
class Volume:
    name: str
    kind: str                     # part | crypt | lvm | ...
    size: int | None
    fstype: str
    mountpoints: tuple[str, ...]
    depth: int


@dataclass(frozen=True)
class Disk:
    name: str
    model: str
    transport: str
    size: int | None
    removable: bool
    read_only: bool
    volumes: tuple[Volume, ...] = field(default=())

    @property
    def title(self) -> str:
        kind = TRANSPORTS.get(self.transport, "")
        parts = [self.model or f"Disk {self.name}"]
        if kind:
            parts.append(kind)
        if self.removable:
            parts.append("removable")
        return " · ".join(parts)


def _size(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _flag(value: object) -> bool:
    return value is True or value in ("1", 1)


def parse_lsblk(text: object) -> list[Disk]:
    """Disks with their partitions, encrypted volumes and LVM volumes (loop and ram devices left out)."""
    try:
        doc = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        raise ValueError("lsblk returned output that is not JSON") from None
    devices = doc.get("blockdevices") if isinstance(doc, dict) else None
    if not isinstance(devices, list):
        raise ValueError("lsblk returned an unexpected document")
    disks = []

    def volumes(children: object, depth: int) -> list[Volume]:
        found = []
        for child in children if isinstance(children, list) else []:
            if not isinstance(child, dict):
                continue
            mounts = tuple(vm.one_line(m, 200) for m in child.get("mountpoints") or [] if isinstance(m, str) and m)
            found.append(Volume(vm.one_line(child.get("name"), 60), vm.one_line(child.get("type"), 12),
                                _size(child.get("size")), vm.one_line(child.get("fstype"), 20), mounts, depth))
            found.extend(volumes(child.get("children"), depth + 1))
        return found

    for device in devices:
        if not isinstance(device, dict) or device.get("type") not in ("disk", "rom"):
            continue
        if device.get("type") == "rom" and not device.get("size"):
            continue
        disks.append(Disk(vm.one_line(device.get("name"), 40), vm.one_line(device.get("model"), 80),
                          vm.one_line(device.get("tran"), 12), _size(device.get("size")), _flag(device.get("rm")),
                          _flag(device.get("ro")), tuple(volumes(device.get("children"), 1))))
    return disks


def volume_text(volume: Volume) -> str:
    kind = {"part": "Partition", "crypt": "Encrypted volume", "lvm": "Logical volume", "raid1": "RAID",
            "rom": "Optical medium"}.get(volume.kind, volume.kind or "Volume")
    parts = [f"{kind} {volume.name}", vm.format_bytes(volume.size) if volume.size is not None else ""]
    if volume.fstype:
        parts.append(volume.fstype)
    parts.append("mounted at " + ", ".join(volume.mountpoints) if volume.mountpoints else "not mounted")
    return " · ".join(p for p in parts if p)


# --- network (nmcli terse output) --------------------------------------------------------------------------

DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,63}$")
STATE_LABELS = {"connected": "Connected", "disconnected": "Disconnected", "unavailable": "Unavailable",
                "unmanaged": "Not managed", "connecting": "Connecting", "deactivating": "Disconnecting"}
TYPE_LABELS = {"ethernet": "Wired", "wifi": "Wi-Fi", "wifi-p2p": "Wi-Fi Direct", "bridge": "Bridge",
               "loopback": "Loopback", "tun": "Tunnel", "wireguard": "WireGuard", "gsm": "Mobile broadband",
               "bt": "Bluetooth", "vlan": "VLAN"}
HIDDEN_TYPES = frozenset({"loopback", "wifi-p2p"})


def valid_device(name: object) -> bool:
    return isinstance(name, str) and bool(DEVICE_RE.fullmatch(name))


def split_terse(line: str) -> list[str]:
    """One nmcli -t line split at unescaped ':' (nmcli escapes ':' and '\\' with a backslash)."""
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


@dataclass(frozen=True)
class NetDevice:
    device: str
    kind: str
    state: str
    connection: str
    addresses: tuple[str, ...] = ()
    gateway: str = ""

    @property
    def kind_label(self) -> str:
        return TYPE_LABELS.get(self.kind, self.kind or "Network")

    @property
    def state_label(self) -> str:
        base = self.state.split(" ")[0]
        return STATE_LABELS.get(base, vm.one_line(self.state, 30).capitalize() or "Unknown")

    @property
    def connected(self) -> bool:
        return self.state.startswith("connected")

    @property
    def unavailable(self) -> bool:
        return self.state.startswith("unavailable")


def parse_nmcli_devices(text: object) -> list[NetDevice]:
    devices = []
    for line in text.splitlines() if isinstance(text, str) else []:
        if not line.strip():
            continue
        parts = (split_terse(line) + ["", "", "", ""])[:4]
        name, kind, state, connection = parts
        if not valid_device(name) or kind in HIDDEN_TYPES:
            continue
        devices.append(NetDevice(name, vm.one_line(kind, 20), vm.one_line(state, 40),
                                 vm.one_line(connection, 80) if connection != "--" else ""))
    return devices


def parse_nmcli_show(text: object) -> tuple[tuple[str, ...], str]:
    """(addresses, gateway) from `nmcli -t -f IP4.ADDRESS,IP4.GATEWAY,IP6.ADDRESS device show DEV`."""
    addresses, gateway = [], ""
    for line in text.splitlines() if isinstance(text, str) else []:
        key, _, value = line.partition(":")
        value = value.replace("\\:", ":").replace("\\\\", "\\").strip()
        if not value or value == "--":
            continue
        if key.startswith(("IP4.ADDRESS", "IP6.ADDRESS")) and len(addresses) < 8:
            if not value.lower().startswith("fe80:"):          # link-local addresses are noise here
                addresses.append(vm.one_line(value, 60))
        elif key == "IP4.GATEWAY":
            gateway = vm.one_line(value, 60)
    return tuple(addresses), gateway


def parse_radio(text: object) -> bool | None:
    value = text.strip().lower() if isinstance(text, str) else ""
    return True if value == "enabled" else (False if value == "disabled" else None)


@dataclass(frozen=True)
class NetworkState:
    devices: tuple[NetDevice, ...]
    wifi_enabled: bool | None

    @property
    def summary_line(self) -> str:
        connected = [d for d in self.devices if d.connected]
        if connected:
            first = connected[0]
            return f"Connected ({first.kind_label}" + (f": {first.connection})" if first.connection else ")")
        if any(d.unavailable for d in self.devices):
            return "Not connected (a network device is unavailable)"
        return "Not connected" if self.devices else "No network devices"


def network_summary(state: NetworkState | None, error: str | None, unavailable: bool) -> vm.Summary:
    if state is None:
        if unavailable:
            return vm.Summary(vm.UNAVAILABLE, "Network status is not available",
                              "NetworkManager's command-line tool (nmcli) is not installed.")
        if error:
            return vm.Summary(vm.ERROR, "The network status could not be read", vm.one_line(error, 300))
        return vm.Summary(vm.CHECKING, "Reading the network status…", "")
    if any(d.connected for d in state.devices):
        return vm.Summary(vm.SECURE, state.summary_line, "")
    if any(d.unavailable for d in state.devices):
        return vm.Summary(vm.WARNING, "Not connected",
                          "A network device is unavailable: it may be switched off, have no cable, or need firmware "
                          "or a driver. Hardware & Drivers can check for missing firmware.")
    return vm.Summary(vm.INFO, state.summary_line, "Connect to a network with Wi-Fi & Networking below.")


# --- load ------------------------------------------------------------------------------------------------------

def parse_proc_stat(text: object) -> tuple[int, int] | None:
    """(busy, total) jiffies of the aggregate "cpu" line of /proc/stat."""
    for line in text.splitlines() if isinstance(text, str) else []:
        if line.startswith("cpu "):
            values = [int(v) for v in line.split()[1:] if v.isdigit()]
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values[:8])
            return total - idle, total
    return None


def cpu_percent(first: tuple[int, int] | None, second: tuple[int, int] | None) -> int | None:
    if not first or not second or second[1] <= first[1]:
        return None
    return max(0, min(100, round((second[0] - first[0]) * 100 / (second[1] - first[1]))))


def parse_memory(text: object) -> tuple[int, int] | None:
    """(total, available) bytes from /proc/meminfo."""
    values: dict[str, int] = {}
    for line in text.splitlines() if isinstance(text, str) else []:
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key.strip()] = int(parts[0]) * 1024
    if "MemTotal" in values and "MemAvailable" in values:
        return values["MemTotal"], values["MemAvailable"]
    return None


def load_rows(load: object) -> list[tuple[str, str]]:
    doc = load if isinstance(load, dict) else {}
    rows = []
    cpu = doc.get("cpu_percent")
    rows.append(("Processor load", f"{cpu}%" if isinstance(cpu, int) else "Unknown"))
    memory = doc.get("memory")
    if isinstance(memory, (list, tuple)) and len(memory) == 2 and memory[0]:
        total, available = memory
        used = total - available
        rows.append(("Memory in use", f"{vm.format_bytes(used)} of {vm.format_bytes(total)} "
                                      f"({round(used * 100 / total)}%)"))
    else:
        rows.append(("Memory in use", "Unknown"))
    return rows
