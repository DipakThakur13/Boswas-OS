"""This device: its name, what kind of device it is, its maker and model, and the person using it.

Pure functions over public files (no Qt, no process):

  /etc/hostname                  the static host name (the device name)
  /etc/machine-info              PRETTY_HOSTNAME and CHASSIS, if an administrator set them
  /sys/class/dmi/id/chassis_type the SMBIOS chassis type (laptop, desktop, ...)
  /sys/class/dmi/id/sys_vendor, product_name, product_version
                                 the maker and model (never the serial numbers
                                 next to them)
  /sys/hypervisor, /proc/cpuinfo whether the device is a virtual machine

The device name is changed with `hostnamectl --static set-hostname NAME`
(commands.hostnamectl_argv), which systemd-hostnamed authorises through
polkit. A name is 1-63 letters, digits and hyphens and does not start or end
with a hyphen (valid_name).
"""

from __future__ import annotations

import os
import pwd
from dataclasses import dataclass

from . import commands
from . import viewmodel as vm

HOSTNAME_FILE = "/etc/hostname"
MACHINE_INFO = "/etc/machine-info"
DMI = "/sys/class/dmi/id"
MAX_NAME = 63

# SMBIOS chassis types (DMTF DSP0134, 7.4.1) -> the kind of device.
LAPTOPS = {8, 9, 10, 14, 31, 32}          # portable, laptop, notebook, sub-notebook, convertible, detachable
TABLETS = {30}
DESKTOPS = {3, 4, 5, 6, 7, 13, 15, 16, 35, 36}
SERVERS = {17, 23, 25, 28, 29}
KINDS = {"laptop": "Laptop", "desktop": "Desktop computer", "tablet": "Tablet", "server": "Server",
         "vm": "Virtual machine", "": "Computer"}
ICONS = {"laptop": ("computer-laptop", "computer"), "tablet": ("computer-laptop", "computer"),
         "vm": ("computer", "video-display"), "desktop": ("computer",), "server": ("computer",), "": ("computer",)}
# Placeholder values firmware writes when the maker filled nothing in.
NOT_SET = {"", "to be filled by o.e.m.", "to be filled by oem", "system product name", "system manufacturer",
           "default string", "not applicable", "none", "o.e.m.", "oem", "not specified", "x.x"}
VIRTUAL_VENDORS = ("qemu", "kvm", "vmware", "innotek", "virtualbox", "xen", "bochs", "microsoft corporation",
                   "parallels", "red hat")


def valid_name(name: object) -> bool:
    return commands.valid_hostname(name)


def name_problem(name: str) -> str:
    """Why ``name`` cannot be the device name, in words for the person; "" when it can."""
    if not name:
        return "Enter a name."
    if len(name) > MAX_NAME:
        return f"A device name has at most {MAX_NAME} characters."
    if name.startswith("-") or name.endswith("-"):
        return "A device name cannot start or end with a hyphen."
    if not valid_name(name):
        return "Use only letters A to Z, digits and hyphens (no spaces)."
    return ""


def suggest_name(text: str) -> str:
    """A valid name from what was typed: spaces and other characters become hyphens ("Anna's PC" -> "Annas-PC")."""
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "-") else ("-" if ch in " _." else "")
                      for ch in text.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:MAX_NAME].strip("-")


def _value(text: str | None) -> str:
    value = vm.one_line(text or "", 120)
    return "" if value.casefold() in NOT_SET else value


def parse_machine_info(text: str | None) -> dict[str, str]:
    values = vm.parse_env(text or "")
    return {key: vm.one_line(values.get(key, ""), 120) for key in ("PRETTY_HOSTNAME", "CHASSIS", "ICON_NAME")}


def chassis_kind(chassis_type: str | None, vendor: str = "", hypervisor: bool = False, machine_chassis: str = "") -> str:
    """laptop, desktop, tablet, server, vm or "" (unknown) from the SMBIOS type and the vendor."""
    if machine_chassis in ("laptop", "desktop", "tablet", "server", "vm"):
        return machine_chassis
    if machine_chassis in ("convertible",):
        return "laptop"
    if hypervisor or any(word in vendor.casefold() for word in VIRTUAL_VENDORS[:7]):
        return "vm"
    try:
        number = int((chassis_type or "").strip())
    except ValueError:
        return ""
    for kind, numbers in (("laptop", LAPTOPS), ("tablet", TABLETS), ("desktop", DESKTOPS), ("server", SERVERS)):
        if number in numbers:
            return kind
    return ""


@dataclass(frozen=True)
class DeviceInfo:
    name: str                # the static host name, else the running one
    pretty: str              # PRETTY_HOSTNAME, if set
    kind: str                # laptop, desktop, tablet, server, vm or ""
    vendor: str
    model: str
    running_name: str = ""   # the kernel's host name now (differs until hostnamed applied a change)

    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, "Computer")

    @property
    def icons(self) -> tuple[str, ...]:
        return ICONS.get(self.kind, ("computer",))

    @property
    def model_text(self) -> str:
        """"Lenovo ThinkPad X1 Carbon Gen 11" or the kind of device when the firmware names no model."""
        if self.model and self.vendor and not self.model.casefold().startswith(self.vendor.casefold()):
            return f"{self.vendor} {self.model}"
        return self.model or self.vendor or self.kind_label


def read_device(root, uname_node: str | None = None) -> DeviceInfo:
    """This device, from the files of ``root`` (a probes.Root)."""
    static = vm.one_line((root.read(HOSTNAME_FILE, 4096) or "").strip().split("\n")[0], MAX_NAME + 1)
    info = parse_machine_info(root.read(MACHINE_INFO, 65536))
    running = uname_node if uname_node is not None else os.uname().nodename
    vendor = _value(root.read(f"{DMI}/sys_vendor", 512))
    model = _value(root.read(f"{DMI}/product_name", 512))
    version = _value(root.read(f"{DMI}/product_version", 512))
    if vendor.casefold() == "lenovo" and version and not model.casefold().startswith(version.casefold()[:4]):
        model = version                       # Lenovo puts the model name into product_version
    cpuinfo = root.read("/proc/cpuinfo", 65536) or ""
    hypervisor = root.exists("/sys/hypervisor/type") or " hypervisor" in cpuinfo.split("flags", 1)[-1][:4096]
    kind = chassis_kind(root.read(f"{DMI}/chassis_type", 64), vendor, hypervisor, info["CHASSIS"])
    name = static if valid_name(static) else vm.one_line(running, MAX_NAME)
    return DeviceInfo(name, info["PRETTY_HOSTNAME"], kind, vendor, model, vm.one_line(running, MAX_NAME))


@dataclass(frozen=True)
class Account:
    user: str
    real_name: str
    picture: str             # the path of ~/.face when it is a readable file, else ""

    @property
    def display_name(self) -> str:
        return self.real_name or self.user


def read_account(environ, uid: int | None = None) -> Account:
    """The person signed in: user name and full name (passwd), the picture ~/.face."""
    uid = os.getuid() if uid is None else uid
    try:
        entry = pwd.getpwuid(uid)
        user, gecos = entry.pw_name, entry.pw_gecos
    except KeyError:
        user, gecos = vm.one_line(environ.get("USER", ""), 32), ""
    real_name = vm.one_line(gecos.split(",")[0], 120)
    home = environ.get("HOME") or ""
    face = os.path.join(home, ".face") if home and os.path.isabs(home) else ""
    picture = face if face and os.path.isfile(face) and not os.path.islink(face) else ""
    return Account(vm.one_line(user, 32), real_name, picture)


def specifications(info: DeviceInfo, hardware: dict | None) -> list[tuple[str, str]]:
    """The rows of "Device specifications" (as on About), for copying."""
    hardware = hardware if isinstance(hardware, dict) else {}
    rows = [("Device name", info.name), ("Device", info.model_text)]
    if hardware.get("cpu_model"):
        threads = hardware.get("cpu_threads")
        rows.append(("Processor", f"{hardware['cpu_model']}" + (f" ({threads} threads)" if threads else "")))
    if hardware.get("memory_bytes"):
        rows.append(("Installed memory", vm.format_bytes(hardware["memory_bytes"])))
    if hardware.get("machine"):
        rows.append(("System type", f"{hardware['machine']}, {hardware.get('boot_mode') or 'unknown'} boot"))
    return rows
