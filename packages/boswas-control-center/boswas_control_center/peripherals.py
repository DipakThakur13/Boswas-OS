"""Cameras, printers and USB devices, read-only and without Qt.

  Cameras   /sys/class/video4linux/video*/: ``name`` and ``index``. A
            camera's driver offers several nodes (the picture, its
            metadata, ...); the node with index 0 of each device is the one
            that delivers pictures, so one camera is listed per device. The
            bus comes from the device link (USB, PCI, built in).
  Printers  CUPS: `lpstat -e` lists the printers, `lpstat -p -d` their state
            and the default (in the C locale). CUPS is installed when its
            scheduler /usr/sbin/cupsd (package cups-daemon, pulled in by cups)
            is there; then a test page is CUPS's own file
            /usr/share/cups/data/testprint, printed with `lp -d PRINTER FILE`.
  USB       the devices of /sys/bus/usb/devices as the Hardware page reads
            them (probes.usb_devices, hardware.usb_rows): names, makers,
            drivers; never serial numbers.
  Drives    the disks of /sys/block that sit on a USB device (the path of
            the disk in /sys/devices goes through it), their partitions and
            unlocked encrypted volumes (holders), and where these are mounted
            (/proc/self/mountinfo, by device number). Eject unmounts each
            mounted file system, locks each unlocked volume and powers the
            drive off (eject_steps, with udisksctl as Files does through
            UDisks2). A drive with a file system mounted anywhere but below
            /media or /run/media (the boot medium of a live session, a
            system disk) is in use by the system and offers no Eject.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import hardware, probes
from . import viewmodel as vm

V4L_DIR = "/sys/class/video4linux"
CUPSD = "/usr/sbin/cupsd"
PRINTING_PACKAGES = ("cups",)
# Installed without what they only recommend: cups-daemon recommends cups-browsed, which Boswas policy excludes
# (the image pins it out, boswas-sysops refuses it), so a plain install of cups would be refused as a whole.
PRINTING_INSTALL = ("apt-install", "--no-recommends", *PRINTING_PACKAGES)
MAX_CAMERAS = 32
MAX_PRINTERS = 200
BLOCK_DIR = "/sys/block"
MOUNTINFO = "/proc/self/mountinfo"
MAX_DRIVES = 64
USER_MOUNTS = ("/media/", "/run/media/")
_USB_ADDRESS = re.compile(r"^[0-9]{1,3}-[0-9]{1,3}(\.[0-9]{1,3}){0,6}$")
_DISK_NAME = re.compile(r"^(sd[a-z]{1,3}|mmcblk[0-9]{1,3})$")
_DEV_NUMBER = re.compile(r"^[0-9]{1,6}:[0-9]{1,8}$")
PRINTER_RE = re.compile(r"^[A-Za-z0-9_.@][A-Za-z0-9_.@+-]{0,126}$")


@dataclass(frozen=True)
class Camera:
    node: str                # video0
    name: str
    bus: str                 # USB, PCI or Built in


def cameras(root) -> list[Camera]:
    """The cameras: one per device, the node that delivers pictures (``root``: a probes.Root)."""
    found: dict[str, Camera] = {}
    nodes = [n for n in root.listdir(V4L_DIR) if re.fullmatch(r"video[0-9]{1,3}", n)]
    nodes.sort(key=lambda n: int(n[5:]))
    for node in nodes:
        base = f"{V4L_DIR}/{node}"
        index = (root.read(f"{base}/index", 32) or "0").strip()
        if index not in ("", "0"):
            continue                          # metadata and further streams of the same camera
        name = vm.one_line((root.read(f"{base}/name", 256) or "").strip(), 120) or "Camera"
        try:
            device = os.path.realpath(root(f"{base}/device"))
        except OSError:
            device = node
        parts = device.split(os.sep)
        if any(part.startswith("usb") for part in parts):
            bus = "USB"
        elif any(part.startswith("pci") for part in parts):
            bus = "PCI"
        else:
            bus = "Built in"
        if device not in found:
            found[device] = Camera(node, name, bus)
        if len(found) >= MAX_CAMERAS:
            break
    return list(found.values())


@dataclass(frozen=True)
class Printer:
    name: str
    state: str               # Idle, Printing, Stopped, ...
    enabled: bool
    default: bool = False


def parse_destinations(text: str) -> list[str]:
    """The printer names of `lpstat -e` (one per line)."""
    names = []
    for line in (text or "").splitlines()[:MAX_PRINTERS]:
        name = line.strip()
        if PRINTER_RE.fullmatch(name) and name not in names:
            names.append(name)
    return names


_PRINTER_LINE = re.compile(r"^printer (\S+) (?:is (idle)|(now printing)|(disabled))")


def parse_printers(text: str, names: list[str] | None = None) -> list[Printer]:
    """Printers from `lpstat -p -d` (C locale); ``names`` from `lpstat -e` adds printers it did not mention."""
    states: dict[str, tuple[str, bool]] = {}
    default = ""
    for line in (text or "").splitlines()[:MAX_PRINTERS * 4]:
        match = _PRINTER_LINE.match(line.strip())
        if match is not None and PRINTER_RE.fullmatch(match.group(1)):
            name = match.group(1)
            if match.group(2):
                states[name] = ("Ready", True)
            elif match.group(3):
                states[name] = ("Printing", True)
            else:
                states[name] = ("Stopped", False)
        elif line.startswith("system default destination:"):
            value = line.split(":", 1)[1].strip()
            default = value if PRINTER_RE.fullmatch(value) else ""
    ordered = list(dict.fromkeys([*(names or []), *states]))
    return [Printer(name, *states.get(name, ("Ready", True)), default=name == default) for name in ordered]


def cups_installed(root) -> bool:
    return root.exists(CUPSD)


def usb(root) -> list[hardware.Device]:
    """The USB devices of this computer (no root hubs, no serial numbers), named from usb.ids when it is there."""
    raw = probes.usb_devices(root)
    text = probes.ids_text(root, probes.USB_IDS)
    names = hardware.parse_ids(text, hardware.wanted_ids(raw, "product_id")) if text else None
    return hardware.usb_rows(raw, names)


# --- drives on USB ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Drive:
    disk: str                                   # /dev/sdb
    usb: str                                    # the address of the USB device it is on (1-4)
    name: str                                   # vendor and model, or the disk's name
    size: int                                   # bytes
    mounts: tuple[tuple[str, str], ...] = ()    # (block device, mount point) of each mounted file system on it
    unlocked: tuple[str, ...] = ()              # partitions whose encrypted volume is open (locked again on eject)

    @property
    def in_use_by_system(self) -> bool:
        """A file system of it is mounted outside /media and /run/media: the system uses it (never ejected here)."""
        return any(not point.startswith(USER_MOUNTS) for _device, point in self.mounts)

    @property
    def folder(self) -> str:
        """Where Files shows it: its first mounted file system, else This PC (Files mounts it with a click)."""
        return self.mounts[0][1] if self.mounts else "computer:///"


def parse_mountinfo(text: str) -> dict[str, list[str]]:
    """{"major:minor": [mount points]} from /proc/self/mountinfo (octal escapes such as \\040 decoded)."""
    found: dict[str, list[str]] = {}
    for line in (text or "").splitlines()[:10000]:
        fields = line.split(" ")
        if len(fields) < 5 or not _DEV_NUMBER.fullmatch(fields[2]):
            continue
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        if point.startswith("/") and "\0" not in point:
            found.setdefault(fields[2], []).append(point)
    return found


def _number(root, path: str) -> str:
    value = (root.read(path, 32) or "").strip()
    return value if _DEV_NUMBER.fullmatch(value) else ""


def _usb_address(root, name: str) -> str:
    """The USB device a disk is on: the last component of its /sys/devices path that names one ("" for none)."""
    try:
        real = os.path.realpath(root(f"{BLOCK_DIR}/{name}"))
    except OSError:
        return ""
    found = ""
    for part in real.split(os.sep):
        if _USB_ADDRESS.fullmatch(part):
            found = part
    return found


def usb_drives(root) -> list[Drive]:
    """The disks on USB (``root``: a probes.Root), with their mounted file systems and open encrypted volumes."""
    mounts = parse_mountinfo(root.read(MOUNTINFO, 4 * 1024 * 1024) or "")
    drives = []
    for name in root.listdir(BLOCK_DIR):
        if not _DISK_NAME.fullmatch(name) or len(drives) >= MAX_DRIVES:
            continue
        usb_address = _usb_address(root, name)
        if not usb_address:
            continue
        base = f"{BLOCK_DIR}/{name}"
        parts = [child for child in root.listdir(base)
                 if child.startswith(name) and root.exists(f"{base}/{child}/partition")]
        volumes = [(child, f"{base}/{child}") for child in parts] or [(name, base)]
        mounted: list[tuple[str, str]] = []
        unlocked: list[str] = []
        for volume, path in volumes:
            for point in mounts.get(_number(root, f"{path}/dev"), []):
                mounted.append((f"/dev/{volume}", point))
            for holder in root.listdir(f"{path}/holders"):
                if not re.fullmatch(r"dm-[0-9]{1,4}", holder):
                    continue
                unlocked.append(f"/dev/{volume}")
                for point in mounts.get(_number(root, f"{BLOCK_DIR}/{holder}/dev"), []):
                    mounted.append((f"/dev/{holder}", point))
        vendor = vm.one_line((root.read(f"{base}/device/vendor", 128) or "").strip(), 60)
        model = vm.one_line((root.read(f"{base}/device/model", 128) or "").strip(), 80)
        sectors = (root.read(f"{base}/size", 32) or "").strip()
        drives.append(Drive(f"/dev/{name}", usb_address, " ".join(part for part in (vendor, model) if part) or name,
                            int(sectors) * 512 if sectors.isdigit() else 0, tuple(mounted),
                            tuple(dict.fromkeys(unlocked))))
    return drives


def eject_steps(drive: Drive) -> list[tuple[str, str]]:
    """(udisksctl action, block device) to eject a drive: unmount every file system (open volumes first), lock
    every open volume, power the drive off."""
    if drive.in_use_by_system:
        raise ValueError(f"{drive.name} is in use by the system and is not ejected")
    holders = [device for device, _point in drive.mounts if device.startswith("/dev/dm-")]
    others = [device for device, _point in drive.mounts if not device.startswith("/dev/dm-")]
    steps = [("unmount", device) for device in dict.fromkeys(holders + others)]
    steps += [("lock", device) for device in drive.unlocked]
    steps.append(("power-off", drive.disk))
    return steps
