"""Hardware & Drivers rules (pure Python, no Qt, no I/O).

Devices come from sysfs (probes.pci_devices, probes.usb_devices): vendor,
device and class IDs and the bound driver. Names come from the pci.ids and
usb.ids databases when installed. Serial numbers are never read. The
firmware report comes from boswas-sysops ``firmware-report`` (contract,
section 1): missing firmware files the kernel asked for, with the package
that provides each one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import viewmodel as vm

HEX4 = re.compile(r"^[0-9a-f]{4}$")
# PCI base classes worth highlighting (class code >> 16).
PCI_CLASSES = {0x01: "Storage", 0x02: "Network", 0x03: "Graphics", 0x04: "Multimedia", 0x05: "Memory",
               0x06: "Bridge", 0x07: "Communication", 0x08: "System", 0x09: "Input", 0x0C: "Serial bus",
               0x0D: "Wireless", 0x10: "Encryption", 0x11: "Signal processing", 0x12: "Processing accelerator",
               0x13: "Instrumentation"}
HIGHLIGHT = frozenset({"Network", "Wireless", "Graphics", "Storage", "Multimedia"})
USB_CLASSES = {0x01: "Audio", 0x02: "Communication", 0x03: "Input", 0x06: "Imaging", 0x07: "Printer",
               0x08: "Storage", 0x09: "Hub", 0x0A: "Communication", 0x0B: "Smart card", 0x0E: "Video",
               0xE0: "Wireless", 0xEF: "Miscellaneous", 0xFF: "Vendor specific"}


def _hex(value: object) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    text = text[2:] if text.startswith("0x") else text
    return text if HEX4.fullmatch(text) else ""


def parse_ids(text: object, wanted: set[tuple[str, str]]) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Vendor and device names for the wanted (vendor, device) IDs from a pci.ids/usb.ids file.

    Format: "vvvv  Vendor name" lines, "\\tdddd  Device name" below them, deeper
    levels (subsystems, interfaces) and class sections ("C xx ...") ignored.
    """
    vendors: dict[str, str] = {}
    devices: dict[tuple[str, str], str] = {}
    want_vendors = {v for v, _d in wanted}
    current = None
    for line in text.splitlines() if isinstance(text, str) else []:
        if not line or line.startswith("#"):
            continue
        if line.startswith("C ") or line.startswith("L ") or line.startswith("HID ") or line.startswith("R "):
            current = None                     # class/language sections at the end of the file
            continue
        if not line.startswith("\t"):
            vid = line[:4].lower()
            current = vid if HEX4.fullmatch(vid) else None
            if current in want_vendors:
                vendors[current] = vm.one_line(line[4:], 120)
            continue
        if current not in want_vendors or line.startswith("\t\t"):
            continue
        did = line[1:5].lower()
        if (current, did) in wanted:
            devices[(current, did)] = vm.one_line(line[5:], 160)
    return vendors, devices


@dataclass(frozen=True)
class Device:
    bus: str                    # pci | usb
    address: str                # 0000:03:00.0, 1-2
    vendor_id: str
    device_id: str
    category: str               # Network, Graphics, ...
    name: str
    vendor: str
    driver: str                 # "" when no driver is bound

    @property
    def highlighted(self) -> bool:
        return self.category in HIGHLIGHT

    @property
    def driver_text(self) -> str:
        return self.driver or "No driver in use"


def pci_category(class_code: object) -> str:
    text = class_code.strip().lower() if isinstance(class_code, str) else ""
    text = text[2:] if text.startswith("0x") else text
    if not re.fullmatch(r"[0-9a-f]{6}", text):
        return "Other"
    return PCI_CLASSES.get(int(text, 16) >> 16, "Other")


def pci_rows(raw: object, names: tuple[dict, dict] | None = None) -> list[Device]:
    """probes.pci_devices() entries -> Devices (named when pci.ids is installed)."""
    vendors, devices = names or ({}, {})
    rows = []
    for item in raw if isinstance(raw, list) else []:
        item = item if isinstance(item, dict) else {}
        vid, did = _hex(item.get("vendor")), _hex(item.get("device"))
        address = vm.one_line(item.get("address"), 20)
        if not vid or not did or not address:
            continue
        category = pci_category(item.get("class"))
        vendor = vendors.get(vid, "")
        name = devices.get((vid, did)) or f"{category} device {vid}:{did}"
        rows.append(Device("pci", address, vid, did, category, name, vendor or f"Vendor {vid}",
                           vm.one_line(item.get("driver"), 40)))
    rows.sort(key=lambda d: (not d.highlighted, d.category, d.address))
    return rows


def usb_rows(raw: object, names: tuple[dict, dict] | None = None) -> list[Device]:
    """probes.usb_devices() entries -> Devices (root hubs left out)."""
    vendors, devices = names or ({}, {})
    rows = []
    for item in raw if isinstance(raw, list) else []:
        item = item if isinstance(item, dict) else {}
        vid, did = _hex(item.get("vendor")), _hex(item.get("product_id"))
        address = vm.one_line(item.get("address"), 20)
        if not vid or not did or not address or vid == "1d6b":          # 1d6b: Linux Foundation root hubs
            continue
        try:
            klass = int(str(item.get("class") or "0"), 16)
        except ValueError:
            klass = 0
        drivers = sorted({vm.one_line(d, 40) for d in item.get("drivers") or [] if isinstance(d, str) and d})
        category = USB_CLASSES.get(klass, "") or ("Input" if "usbhid" in drivers else "USB device")
        name = vm.one_line(item.get("product"), 120) or devices.get((vid, did)) or f"USB device {vid}:{did}"
        vendor = vm.one_line(item.get("manufacturer"), 120) or vendors.get(vid, "") or f"Vendor {vid}"
        driver = ", ".join(d for d in drivers if d != "usb")
        rows.append(Device("usb", address, vid, did, category, name, vendor, driver))
    rows.sort(key=lambda d: (d.category, d.address))
    return rows


def wanted_ids(raw: object, device_key: str = "device") -> set[tuple[str, str]]:
    return {(_hex(i.get("vendor")), _hex(i.get(device_key))) for i in raw if isinstance(i, dict)} \
        if isinstance(raw, list) else set()


@dataclass(frozen=True)
class HardwareSummary:
    summary: vm.Summary
    without_driver: tuple[Device, ...]


def hardware_summary(pci: list[Device], usb: list[Device], error: str | None = None) -> HardwareSummary:
    if error and not pci:
        return HardwareSummary(vm.Summary(vm.ERROR, "The hardware could not be listed", vm.one_line(error, 300)), ())
    if not pci and not usb:
        return HardwareSummary(vm.Summary(vm.UNAVAILABLE, "No devices were found",
                                          "This system does not expose PCI or USB devices in /sys."), ())
    missing = tuple(d for d in pci if not d.driver and d.category in HIGHLIGHT)
    if missing:
        names = ", ".join(d.name for d in missing[:3])
        return HardwareSummary(vm.Summary(vm.WARNING, f"{len(missing)} important device"
                                          f"{'s have' if len(missing) != 1 else ' has'} no driver in use",
                                          f"{names}. Check for missing firmware below; some devices are not "
                                          "supported by Linux."), missing)
    return HardwareSummary(vm.Summary(vm.SECURE, "Every important device has a driver",
                                      f"{len(pci)} PCI and {len(usb)} USB devices detected."), ())


# --- firmware report (boswas-sysops firmware-report) --------------------------------------------------------

@dataclass(frozen=True)
class MissingFirmware:
    file: str
    driver: str
    device: str
    package: str
    installed: bool
    candidate: str

    @property
    def installable(self) -> bool:
        return bool(self.package) and not self.installed and bool(self.candidate)

    @property
    def explanation(self) -> str:
        who = f"{self.driver} ({self.device})" if self.driver and self.device else (self.driver or self.device
                                                                                     or "a driver")
        if not self.package:
            return (f"Needed by {who}. No Debian package provides this file, so this device may not work. Ask "
                    "your administrator or the device vendor.")
        if self.installed:
            return (f"Needed by {who}. The package {self.package} is installed, but this file is not in it: the "
                    "device may need newer firmware than this release provides.")
        if not self.candidate:
            return (f"Needed by {who}. Provided by {self.package}, which is not available from the configured "
                    "repositories (check the network and the package sources).")
        return f"Needed by {who}. Provided by the Debian package {self.package} ({self.candidate})."


FIRMWARE_FILE_RE = re.compile(r"^[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*$")


def firmware_report(data: object) -> tuple[str, list[MissingFirmware]]:
    """(source, missing files) from the result data of firmware-report."""
    doc = data if isinstance(data, dict) else {}
    source = doc.get("source") if doc.get("source") in ("journal", "dmesg", "none") else "none"
    missing = []
    seen = set()
    for item in doc.get("missing") or []:
        item = item if isinstance(item, dict) else {}
        name = item.get("file")
        if not isinstance(name, str) or ".." in name.split("/") or not FIRMWARE_FILE_RE.fullmatch(name) \
                or name in seen:
            continue
        seen.add(name)
        package = item.get("package") if isinstance(item.get("package"), str) and \
            re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,99}", item["package"]) else ""
        missing.append(MissingFirmware(name, vm.one_line(item.get("driver"), 40), vm.one_line(item.get("device"), 40),
                                       package, item.get("installed") is True,
                                       vm.one_line(item.get("candidate"), 60) if isinstance(item.get("candidate"),
                                                                                             str) else ""))
    return source, missing


def firmware_summary(source: str, missing: list[MissingFirmware]) -> vm.Summary:
    if source == "none":
        return vm.Summary(vm.UNAVAILABLE, "The kernel log could not be read",
                          "Missing firmware cannot be detected on this system.")
    if not missing:
        return vm.Summary(vm.SECURE, "No missing firmware", "The kernel did not report missing firmware since this "
                                                            "boot.")
    installable = sum(1 for m in missing if m.installable)
    detail = (f"{installable} can be installed from Debian packages." if installable else
              "None of them can be installed from the configured repositories.")
    return vm.Summary(vm.WARNING, f"{len(missing)} firmware file{'s are' if len(missing) != 1 else ' is'} missing",
                      detail)
