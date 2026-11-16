"""What /sys says about the hardware: CPU topology and frequency, disks, network interfaces, GPUs, batteries.

Standard library only. Every function takes ``sys`` (the path of the sysfs
tree, ``/sys`` on a real system) so tests can point it at a directory they
built. Nothing here raises for a file that is missing or unreadable: the
value is None, 0 or left out.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

from .procfs import read_int, read_text
from .textutil import clean

_CPU_DIR = re.compile(r"^cpu(\d+)$")
_CARD = re.compile(r"^card(\d+)$")
MAX_FREQUENCY_FILES = 64          # of the cpufreq policies, at most this many are read per sample
# Whole-disk devices that are not storage hardware: loop files, RAM disks, compressed RAM, device mapper and
# software RAID (their traffic is counted on the disks underneath), network block devices.
_VIRTUAL_DISKS = ("loop", "ram", "zram", "dm-", "md", "nbd", "fd")


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _text(path: str, limit: int = 256) -> str:
    return clean(read_text(path, 4096) or "", limit)


# --- CPU ---------------------------------------------------------------------------------------------

class Topology(NamedTuple):
    logical: int             # CPUs that are online
    sockets: int
    cores: int


def cpu_numbers(sys: str) -> list[int]:
    """The numbers of the CPUs the kernel knows, in order."""
    numbers = []
    for name in _listdir(os.path.join(sys, "devices/system/cpu")):
        match = _CPU_DIR.match(name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def cpu_topology(sys: str) -> Topology:
    """Sockets, cores and logical processors from the topology directories (CPUs that are offline have none)."""
    base = os.path.join(sys, "devices/system/cpu")
    packages: set[int] = set()
    cores: set[tuple[int, int]] = set()
    logical = 0
    for number in cpu_numbers(sys):
        topology = os.path.join(base, f"cpu{number}", "topology")
        package = read_int(os.path.join(topology, "physical_package_id"))
        core = read_int(os.path.join(topology, "core_id"))
        if package is None and core is None:
            continue
        logical += 1
        package = 0 if package is None or package < 0 else package
        packages.add(package)
        if core is not None:
            cores.add((package, core))
    return Topology(logical, len(packages), len(cores))


def cpu_caches(sys: str) -> dict[str, int]:
    """Cache sizes in bytes summed over the machine: keys ``L1d``, ``L1i``, ``L2``, ``L3``."""
    base = os.path.join(sys, "devices/system/cpu")
    seen: set[tuple[str, str, str]] = set()
    totals: dict[str, int] = {}
    for number in cpu_numbers(sys):
        cache = os.path.join(base, f"cpu{number}", "cache")
        for index in _listdir(cache):
            if not index.startswith("index"):
                continue
            path = os.path.join(cache, index)
            level = read_text(os.path.join(path, "level"), 16) or ""
            kind = read_text(os.path.join(path, "type"), 32) or ""
            shared = read_text(os.path.join(path, "shared_cpu_list"), 4096) or str(number)
            if not level.isdigit() or (level, kind, shared) in seen:
                continue
            seen.add((level, kind, shared))
            size = _cache_size(read_text(os.path.join(path, "size"), 32) or "")
            if size:
                suffix = {"Data": "d", "Instruction": "i"}.get(kind, "") if level == "1" else ""
                key = f"L{level}{suffix}"
                totals[key] = totals.get(key, 0) + size
    return totals


def _cache_size(text: str) -> int:
    match = re.match(r"^(\d+)\s*([KMG]?)", text)
    if not match:
        return 0
    return int(match.group(1)) * {"": 1, "K": 1024, "M": 1048576, "G": 1073741824}[match.group(2)]


def _policies(sys: str) -> list[str]:
    base = os.path.join(sys, "devices/system/cpu/cpufreq")
    names = [name for name in _listdir(base) if name.startswith("policy") and name[6:].isdigit()]
    names.sort(key=lambda name: int(name[6:]))
    return [os.path.join(base, name) for name in names]


class Frequencies(NamedTuple):
    current_mhz: float | None        # the mean over the policies that were read
    base_mhz: float | None           # the nominal frequency, when the driver tells it
    max_mhz: float | None
    min_mhz: float | None


def cpu_frequency_limits(sys: str) -> tuple[float | None, float | None, float | None]:
    """(base, maximum, minimum) frequency in MHz of the first policy; None where the driver has no such file."""
    policies = _policies(sys)
    if not policies:
        return None, None, None

    def mhz(name: str) -> float | None:
        value = read_int(os.path.join(policies[0], name))
        return value / 1000 if value and value > 0 else None

    return mhz("base_frequency"), mhz("cpuinfo_max_freq"), mhz("cpuinfo_min_freq")


def cpu_current_mhz(sys: str) -> float | None:
    """The mean current frequency in MHz, from ``scaling_cur_freq`` of at most MAX_FREQUENCY_FILES policies
    spread evenly over the machine. None without cpufreq."""
    policies = _policies(sys)
    if not policies:
        return None
    step = max(1, -(-len(policies) // MAX_FREQUENCY_FILES))
    values = []
    for path in policies[::step]:
        value = read_int(os.path.join(path, "scaling_cur_freq"))
        if value and value > 0:
            values.append(value)
    return sum(values) / len(values) / 1000 if values else None


# --- disks -------------------------------------------------------------------------------------------

class DiskInfo(NamedTuple):
    name: str                # sda, nvme0n1, vda, mmcblk0
    model: str
    capacity: int            # bytes
    kind: str                # "SSD", "HDD", "SSD (NVMe)", "Removable", "SD/eMMC", "Virtual disk", "Optical"
    removable: bool
    devices: tuple[str, ...]  # "major:minor" of the disk and its partitions
    partitions: tuple[str, ...]


def disk_names(sys: str) -> list[str]:
    """The storage disks: whole block devices with hardware behind them and a medium in them."""
    names = []
    for name in sorted(_listdir(os.path.join(sys, "block"))):
        if name.startswith(_VIRTUAL_DISKS):
            continue
        path = os.path.join(sys, "block", name)
        if not os.path.exists(os.path.join(path, "device")):
            continue
        if not (read_int(os.path.join(path, "size")) or 0):
            continue                     # an empty card reader or optical drive
        names.append(name)
    return names


def disk_info(sys: str, name: str) -> DiskInfo:
    path = os.path.join(sys, "block", name)
    device = os.path.join(path, "device")
    model = _text(os.path.join(device, "model")) or _text(os.path.join(device, "name"))
    vendor = _text(os.path.join(device, "vendor"))
    if vendor.startswith("0x"):          # virtio: a bus ID, not a name
        vendor = ""
    if vendor and vendor.upper() not in ("ATA", "NVME") and not model.lower().startswith(vendor.lower()):
        model = f"{vendor} {model}".strip()
    removable = read_int(os.path.join(path, "removable")) == 1
    rotational = read_int(os.path.join(path, "queue/rotational"))
    if name.startswith("nvme"):
        kind = "SSD (NVMe)"
    elif name.startswith("mmcblk"):
        kind = "SD/eMMC"
    elif name.startswith("sr"):
        kind = "Optical"
    elif removable:
        kind = "Removable"
    elif name.startswith(("vd", "xvd")):
        kind = "Virtual disk"
    elif rotational == 1:
        kind = "HDD"
    elif rotational == 0:
        kind = "SSD"
    else:
        kind = "Disk"
    devices = []
    own = read_text(os.path.join(path, "dev"), 32)
    if own:
        devices.append(own)
    partitions = []
    for entry in sorted(_listdir(path)):
        if entry.startswith(name) and os.path.exists(os.path.join(path, entry, "partition")):
            partitions.append(entry)
            number = read_text(os.path.join(path, entry, "dev"), 32)
            if number:
                devices.append(number)
    return DiskInfo(name, model, (read_int(os.path.join(path, "size")) or 0) * 512, kind, removable,
                    tuple(devices), tuple(partitions))


# --- network -----------------------------------------------------------------------------------------

class NetInfo(NamedTuple):
    name: str
    kind: str                # "Ethernet", "Wi-Fi", "Virtual", "Loopback", ...
    state: str               # operstate: up, down, dormant, unknown, ...
    speed_mbps: int | None   # None: the driver does not report one (wireless, virtual, link down)
    mac: str
    physical: bool


def net_info(sys: str, name: str) -> NetInfo:
    path = os.path.join(sys, "class/net", name)
    arp_type = read_int(os.path.join(path, "type"))
    physical = os.path.exists(os.path.join(path, "device"))
    if arp_type == 772:
        kind = "Loopback"
    elif os.path.exists(os.path.join(path, "wireless")) or os.path.exists(os.path.join(path, "phy80211")):
        kind = "Wi-Fi"
    elif os.path.exists(os.path.join(path, "bridge")):
        kind = "Bridge"
    elif os.path.exists(os.path.join(path, "tun_flags")):
        kind = "Tunnel"
    elif arp_type == 1 and physical:
        kind = "Ethernet"
    elif physical:
        kind = "Network adapter"
    else:
        kind = "Virtual"
    speed = read_int(os.path.join(path, "speed"))
    return NetInfo(name, kind, _text(os.path.join(path, "operstate"), 32) or "unknown",
                   speed if speed and speed > 0 else None, _text(os.path.join(path, "address"), 64), physical)


def net_names(sys: str) -> list[str]:
    return sorted(_listdir(os.path.join(sys, "class/net")))


# --- GPU ---------------------------------------------------------------------------------------------

PCI_VENDORS = {
    0x1002: "AMD", 0x1022: "AMD", 0x8086: "Intel", 0x10DE: "NVIDIA", 0x1AF4: "Red Hat (virtio)",
    0x15AD: "VMware", 0x1234: "QEMU", 0x1B36: "Red Hat (QXL)", 0x80EE: "VirtualBox", 0x1A03: "ASPEED",
    0x102B: "Matrox", 0x1414: "Hyper-V", 0x14E4: "Broadcom", 0x5143: "Qualcomm", 0x13B5: "Arm",
    0x1010: "Imagination", 0x106B: "Apple", 0x1D17: "Zhaoxin", 0x0014: "Loongson",
}
PCI_IDS = ("usr/share/misc/pci.ids", "usr/share/hwdata/pci.ids")
_MAX_PCI_IDS = 8 * 1024 * 1024


class GpuInfo(NamedTuple):
    card: str                # card0
    vendor: str              # a name, or "Vendor 1234" when the ID is not known, or ""
    model: str               # from pci.ids when that file is installed, else the device ID or the driver's name
    driver: str
    bus_id: str              # "8086:9a49" for a PCI device, the compatible string for a platform device
    busy_percent: int | None           # None: the driver does not report utilisation
    memory_used: int | None
    memory_total: int | None


def _uevent(path: str) -> dict[str, str]:
    result = {}
    for line in (read_text(path, 16384) or "").split("\n"):
        key, equals, value = line.partition("=")
        if equals:
            result[key] = value
    return result


def pci_names(root: str, vendor: int, device: int) -> tuple[str, str]:
    """(vendor name, device name) from the pci.ids database under ``root``; "" for what is not found."""
    for relative in PCI_IDS:
        path = os.path.join(root, relative)
        try:
            if os.path.getsize(path) > _MAX_PCI_IDS:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                vendor_name = ""
                wanted_vendor, wanted_device = f"{vendor:04x}", f"{device:04x}"
                for line in handle:
                    if not line or line[0] == "#":
                        continue
                    if not vendor_name:
                        if line[:4] == wanted_vendor and line[4:6] == "  ":
                            vendor_name = clean(line[6:].strip(), 120)
                    elif line[0] != "\t":
                        break                       # the next vendor: the device is not listed
                    elif line[1:5] == wanted_device and line[5:7] == "  ":
                        return vendor_name, clean(line[7:].strip(), 160)
                if vendor_name:
                    return vendor_name, ""
        except OSError:
            continue
    return "", ""


def gpu_cards(sys: str) -> list[str]:
    cards = [name for name in _listdir(os.path.join(sys, "class/drm")) if _CARD.match(name)]
    return sorted(cards, key=lambda name: int(name[4:]))


def gpu_info(sys: str, card: str, root: str = "/") -> GpuInfo:
    """Identification of a DRM card and, where the driver exposes them, utilisation and video memory."""
    device = os.path.join(sys, "class/drm", card, "device")
    event = _uevent(os.path.join(device, "uevent"))
    driver = clean(event.get("DRIVER", ""), 64)
    if not driver:
        try:
            driver = clean(os.path.basename(os.readlink(os.path.join(device, "driver"))), 64)
        except OSError:
            driver = ""
    vendor_id = read_int(os.path.join(device, "vendor"))
    device_id = read_int(os.path.join(device, "device"))
    vendor = model = bus_id = ""
    if vendor_id is not None and device_id is not None:
        bus_id = f"{vendor_id:04x}:{device_id:04x}"
        vendor, model = pci_names(root, vendor_id, device_id)
        short = PCI_VENDORS.get(vendor_id)
        if short:
            vendor = short
        elif not vendor:
            vendor = f"Vendor {vendor_id:04x}"
        if not model:
            model = f"Device {device_id:04x}"
    else:
        bus_id = clean(event.get("OF_COMPATIBLE_0", "") or event.get("MODALIAS", ""), 120)
        model = bus_id.split(",")[-1] if bus_id else (driver or card)
    return GpuInfo(card, vendor, model, driver, bus_id, gpu_busy(sys, card),
                   read_int(os.path.join(device, "mem_info_vram_used")),
                   read_int(os.path.join(device, "mem_info_vram_total")))


def gpu_busy(sys: str, card: str) -> int | None:
    """``gpu_busy_percent`` of the card (amdgpu and a few others), clamped to 0..100; None when absent."""
    value = read_int(os.path.join(sys, "class/drm", card, "device/gpu_busy_percent"))
    return None if value is None else max(0, min(100, value))


# --- battery -----------------------------------------------------------------------------------------

class Battery(NamedTuple):
    name: str
    percent: float | None
    status: str              # Charging, Discharging, Full, Not charging, Unknown
    power_w: float | None    # the rate of charge or discharge, always positive
    energy_wh: float | None
    full_wh: float | None
    design_wh: float | None
    seconds_left: float | None       # to empty when discharging, to full when charging
    cycles: int | None
    technology: str
    model: str


def _micro(event: dict[str, str], key: str) -> float | None:
    value = event.get(f"POWER_SUPPLY_{key}")
    try:
        return abs(int(value)) / 1e6 if value is not None else None
    except ValueError:
        return None


def battery(sys: str, name: str) -> Battery | None:
    """One power supply as a Battery, or None when it is not the battery of this computer (a mains
    adapter, or the battery of a mouse or a headset, which the kernel marks ``scope=Device``)."""
    event = _uevent(os.path.join(sys, "class/power_supply", name, "uevent"))
    if event.get("POWER_SUPPLY_TYPE") != "Battery" or event.get("POWER_SUPPLY_SCOPE") == "Device":
        return None
    if event.get("POWER_SUPPLY_PRESENT") == "0":
        return None
    voltage = _micro(event, "VOLTAGE_NOW") or _micro(event, "VOLTAGE_MIN_DESIGN")

    def energy(kind: str) -> float | None:
        value = _micro(event, f"ENERGY_{kind}")
        if value is None and voltage:
            charge = _micro(event, f"CHARGE_{kind}")
            value = charge * voltage if charge is not None else None
        return value

    now, full, design = energy("NOW"), energy("FULL"), energy("FULL_DESIGN")
    power = _micro(event, "POWER_NOW")
    if power is None and voltage:
        current = _micro(event, "CURRENT_NOW")
        power = current * voltage if current is not None else None
    percent = None
    try:
        percent = float(event["POWER_SUPPLY_CAPACITY"])
    except (KeyError, ValueError):
        if now is not None and full:
            percent = 100 * now / full
    if percent is not None:
        percent = max(0.0, min(100.0, percent))
    status = clean(event.get("POWER_SUPPLY_STATUS", "Unknown"), 32) or "Unknown"
    left = None
    if power and power > 0.01 and now is not None:
        if status == "Discharging":
            left = now / power * 3600
        elif status == "Charging" and full is not None and full >= now:
            left = (full - now) / power * 3600
    cycles = None
    try:
        cycles = int(event["POWER_SUPPLY_CYCLE_COUNT"])
        cycles = cycles if cycles > 0 else None
    except (KeyError, ValueError):
        pass
    maker = clean(event.get("POWER_SUPPLY_MANUFACTURER", ""), 64)
    model = clean(event.get("POWER_SUPPLY_MODEL_NAME", ""), 64)
    return Battery(name, percent, status, power, now, full, design, left, cycles,
                   clean(event.get("POWER_SUPPLY_TECHNOLOGY", ""), 32), f"{maker} {model}".strip())


def battery_names(sys: str) -> list[str]:
    return sorted(_listdir(os.path.join(sys, "class/power_supply")))
