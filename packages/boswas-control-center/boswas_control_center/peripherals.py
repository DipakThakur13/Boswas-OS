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
MAX_CAMERAS = 32
MAX_PRINTERS = 200
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
