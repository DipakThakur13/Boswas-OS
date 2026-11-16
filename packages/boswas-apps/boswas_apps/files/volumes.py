"""The volumes of this computer: /proc/self/mountinfo, UDisks2 data, free space (no Qt).

``parse_mountinfo`` and ``displayable`` give the mounted volumes without
any service. ``UDisksReader`` reads the block devices and drives of UDisks2
through a small bus interface (the QtDBus one is in ``udisks.py``, tests
use a fake); ``assemble`` joins both into the list the user sees.

UDisks2 is read property by property, with replies of simple types only
(strings, numbers, byte strings, object paths). That is deliberate:
PySide6 cannot take apart nested D-Bus containers, so GetManagedObjects
is of no use to this program.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable

from .text import format_size

MOUNTINFO = "/proc/self/mountinfo"
_MAX_MOUNTINFO = 4 * 1024 * 1024
_OCTAL = re.compile(r"\\([0-7]{3})")

PSEUDO_FILESYSTEMS = frozenset({
    "autofs", "bdev", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts", "devtmpfs",
    "efivarfs", "fuse.gvfsd-fuse", "fuse.portal", "fuse.snapfuse", "fuse.lxcfs", "fusectl", "hugetlbfs", "mqueue",
    "nsfs", "overlay", "pipefs", "proc", "pstore", "ramfs", "rpc_pipefs", "securityfs", "selinuxfs", "sockfs",
    "squashfs", "sysfs", "tmpfs", "tracefs", "erofs", "fuse.rofiles-fuse", "nfsd", "rootfs",
})
NETWORK_FILESYSTEMS = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "davfs", "fuse.davfs2",
                                 "9p", "ceph", "glusterfs", "fuse.rclone", "afs", "ncpfs"})
_HIDDEN_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/boot", "/efi", "/snap", "/var", "/usr", "/nix", "/tmp",
                    "/etc", "/lib", "/bin", "/sbin", "/opt", "/srv", "/root")
_SHOWN_PREFIXES = ("/run/media/", "/media/", "/mnt/")


@dataclass(frozen=True)
class Mount:
    mount_point: str
    source: str
    fstype: str
    device: tuple[int, int]             # major, minor
    options: str = ""
    root: str = "/"

    @property
    def read_only(self) -> bool:
        return "ro" in self.options.split(",")


def _unescape(text: str) -> str:
    return _OCTAL.sub(lambda match: chr(int(match.group(1), 8) & 0xFF), text)


def parse_mountinfo(text: str) -> list[Mount]:
    """The mounts of a ``/proc/self/mountinfo`` text, in mount order. Lines that do not parse are skipped."""
    mounts = []
    for line in text.splitlines():
        fields = line.split(" ")
        try:
            separator = fields.index("-", 6)
            major, _, minor = fields[2].partition(":")
            mount = Mount(mount_point=_unescape(fields[4]), source=_unescape(fields[separator + 2]),
                          fstype=fields[separator + 1], device=(int(major), int(minor)), options=fields[5],
                          root=_unescape(fields[3]))
        except (ValueError, IndexError):
            continue
        if mount.mount_point.startswith("/"):
            mounts.append(mount)
    return mounts


def read_mounts(path: str = MOUNTINFO) -> list[Mount]:
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            return parse_mountinfo(handle.read(_MAX_MOUNTINFO))
    except OSError:
        return []


def effective(mounts: list[Mount]) -> list[Mount]:
    """One mount per mount point: the one mounted last, which is the one a path reaches."""
    latest: dict[str, Mount] = {}
    for mount in mounts:
        latest.pop(mount.mount_point, None)
        latest[mount.mount_point] = mount
    return list(latest.values())


def is_displayable(mount: Mount, user_home: str = "") -> bool:
    """Whether a mount is a volume a person thinks of: the root, disks, sticks, network shares."""
    point = mount.mount_point
    if point == "/":
        return True
    if mount.fstype in PSEUDO_FILESYSTEMS:
        return False
    if point.startswith(_SHOWN_PREFIXES):
        return True
    if user_home and (point == user_home or point.startswith(user_home.rstrip("/") + "/")):
        return True
    return not any(point == prefix or point.startswith(prefix + "/") for prefix in _HIDDEN_PREFIXES)


def mount_of(path: str, mounts: list[Mount]) -> Mount | None:
    """The mount that holds ``path`` (the longest mount point that is a prefix of it)."""
    best = None
    for mount in effective(mounts):
        point = mount.mount_point.rstrip("/")
        if (path == point or path.startswith(point + "/") or point == "") and (
                best is None or len(point) >= len(best.mount_point.rstrip("/"))):
            best = mount
    return best


def usage(path: str) -> tuple[int, int] | None:
    """(total bytes, bytes free for the user) of the file system at ``path``, or None."""
    try:
        info = os.statvfs(path)
    except OSError:
        return None
    return info.f_frsize * info.f_blocks, info.f_frsize * info.f_bavail


def labels(directory: str = "/dev/disk/by-label") -> dict[str, str]:
    """Device node (resolved) -> file system label, from udev's by-label links."""
    result: dict[str, str] = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return result
    for name in names[:4096]:
        try:
            target = os.path.realpath(os.path.join(directory, name))
        except OSError:
            continue
        label = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda match: chr(int(match.group(1), 16)), name)
        result.setdefault(target, label)
    return result


@dataclass
class Volume:
    """One entry under This PC."""

    key: str                            # stable while the volume exists: the mount point or the UDisks object
    name: str
    mount_point: str | None = None
    device: str = ""
    fstype: str = ""
    total: int = 0
    free: int = 0
    kind: str = "disk"                  # root, disk, removable, optical, network, encrypted
    read_only: bool = False
    object_path: str = ""               # the UDisks2 block device, when UDisks2 knows it
    drive_path: str = ""
    backing_path: str = ""              # the encrypted device this volume was unlocked from
    can_mount: bool = False
    can_unmount: bool = False
    can_unlock: bool = False
    can_eject: bool = False
    can_power_off: bool = False
    description: str = ""               # the drive: vendor and model

    @property
    def mounted(self) -> bool:
        return self.mount_point is not None

    @property
    def used(self) -> int:
        return max(0, self.total - self.free)

    @property
    def icon(self) -> str:
        return {"root": "drive-harddisk", "disk": "drive-harddisk", "removable": "drive-removable-media-usb",
                "optical": "media-optical", "network": "folder-remote",
                "encrypted": "drive-harddisk"}.get(self.kind, "drive-harddisk")

    @property
    def space_text(self) -> str:
        if self.mounted and self.total:
            return f"{format_size(self.free)} free of {format_size(self.total)}"
        if self.can_unlock:
            return f"{format_size(self.total)}, locked" if self.total else "Locked"
        if not self.mounted:
            return f"{format_size(self.total)}, not mounted" if self.total else "Not mounted"
        return ""


def _mount_name(mount: Mount, label: str) -> str:
    if label:
        return label
    if mount.mount_point == "/":
        return "File system"
    return os.path.basename(mount.mount_point.rstrip("/")) or mount.mount_point


def _text(value: object) -> str:
    """A UDisks byte string (NUL-terminated) or string as text."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
    return value if isinstance(value, str) else ""


def default_devnum(device: str) -> tuple[int, int] | None:
    try:
        info = os.stat(device)
    except OSError:
        return None
    return os.major(info.st_rdev), os.minor(info.st_rdev)


def assemble(mounts: list[Mount], udisks: dict | None = None, *, user_home: str = "",
             usage_of: Callable[[str], tuple[int, int] | None] = usage,
             label_of: dict[str, str] | None = None,
             devnum_of: Callable[[str], tuple[int, int] | None] = default_devnum) -> list[Volume]:
    """The volumes to show: displayable mounts, completed and extended by what UDisks2 knows.

    ``udisks`` is the result of ``UDisksReader.read`` (``{"blocks": {...},
    "drives": {...}}``) or None when UDisks2 is not there; then every volume
    is a mounted one and none offers an action.
    """
    label_of = label_of or {}
    shown = [mount for mount in effective(mounts) if is_displayable(mount, user_home)]
    all_devices = {mount.device for mount in effective(mounts)}
    volumes: dict[str, Volume] = {}
    by_device: dict[tuple[int, int], Volume] = {}
    for mount in shown:
        space = usage_of(mount.mount_point) or (0, 0)
        if mount.mount_point == "/":
            kind = "root"
        elif mount.fstype in NETWORK_FILESYSTEMS:
            kind = "network"
        else:
            kind = "disk"
        volume = Volume(key=mount.mount_point, name=_mount_name(mount, label_of.get(mount.source, "")),
                        mount_point=mount.mount_point, device=mount.source, fstype=mount.fstype,
                        total=space[0], free=space[1], kind=kind, read_only=mount.read_only)
        volumes[volume.key] = volume
        if mount.device != (0, 0):
            by_device.setdefault(mount.device, volume)

    blocks = (udisks or {}).get("blocks", {})
    drives = (udisks or {}).get("drives", {})
    unlocked = {str(props.get("CryptoBackingDevice") or "") for props in blocks.values()}
    for object_path in sorted(blocks):
        props = blocks[object_path]
        usage_kind = props.get("IdUsage")
        if usage_kind not in ("filesystem", "crypto") or props.get("HintIgnore"):
            continue
        device = _text(props.get("Device"))
        if not device or device.startswith("/dev/loop"):
            continue
        drive_path = str(props.get("Drive") or "")
        drive = drives.get(drive_path, {})
        removable = bool(drive.get("Removable") or drive.get("MediaRemovable")
                         or drive.get("ConnectionBus") in ("usb", "sdio", "ieee1394"))
        optical = bool(drive.get("Optical"))
        backing = str(props.get("CryptoBackingDevice") or "")
        backing = backing if backing not in ("", "/") else ""
        description = " ".join(part for part in (str(drive.get("Vendor") or "").strip(),
                                                 str(drive.get("Model") or "").strip()) if part)
        number = devnum_of(device)
        volume = by_device.get(number) if number is not None else None
        if volume is None:
            if number is not None and number in all_devices:
                continue                     # mounted somewhere that is not shown (a system partition)
            if usage_kind == "crypto" and object_path in unlocked:
                continue                     # already unlocked: its clear-text device is the volume
            size = props.get("Size")
            size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else 0
            label = str(props.get("IdLabel") or "") or str(props.get("HintName") or "")
            if usage_kind == "crypto":
                name = label or (f"{format_size(size)} encrypted volume" if size else "Encrypted volume")
            else:
                name = label or (f"{format_size(size)} volume" if size else os.path.basename(device))
            volume = Volume(key=object_path, name=name, device=device, fstype=str(props.get("IdType") or ""),
                            total=size, can_mount=usage_kind == "filesystem", can_unlock=usage_kind == "crypto")
            volumes[volume.key] = volume
        else:
            label = str(props.get("IdLabel") or "")
            if label and volume.kind != "root":
                volume.name = label
            volume.can_unmount = volume.kind != "root"
        volume.object_path = object_path
        volume.drive_path = drive_path if drive_path not in ("", "/") else ""
        volume.backing_path = backing
        volume.description = description
        if volume.kind != "root":
            if optical:
                volume.kind = "optical"
            elif removable:
                volume.kind = "removable"
            elif usage_kind == "crypto" or backing:
                volume.kind = "encrypted"
        volume.can_eject = bool(volume.drive_path) and volume.kind != "root" and bool(
            drive.get("Ejectable") or (removable and drive.get("CanPowerOff")))
        volume.can_power_off = bool(drive.get("CanPowerOff")) and removable

    order = {"root": 0, "disk": 1, "encrypted": 1, "removable": 2, "optical": 2, "network": 3}
    return sorted(volumes.values(), key=lambda volume: (order.get(volume.kind, 9), volume.name.casefold(),
                                                        volume.key))


# --- UDisks2 ------------------------------------------------------------------------------------------------

UDISKS_SERVICE = "org.freedesktop.UDisks2"
UDISKS_ROOT = "/org/freedesktop/UDisks2"
BLOCK_DEVICES = UDISKS_ROOT + "/block_devices"
BLOCK = "org.freedesktop.UDisks2.Block"
DRIVE = "org.freedesktop.UDisks2.Drive"
FILESYSTEM = "org.freedesktop.UDisks2.Filesystem"
ENCRYPTED = "org.freedesktop.UDisks2.Encrypted"
PROPERTIES = "org.freedesktop.DBus.Properties"
INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"

_NODE = re.compile(r'<node\s+name="([A-Za-z0-9_]{1,128})"\s*/>')
_OBJECT_PATH = re.compile(r"^/[A-Za-z0-9_/]{1,255}$")
_FIRST = ("IdUsage", "HintIgnore")
_BLOCK_PROPERTIES = ("Device", "IdLabel", "IdType", "Size", "Drive", "HintSystem", "HintName",
                     "CryptoBackingDevice")
_DRIVE_PROPERTIES = ("Removable", "MediaRemovable", "Ejectable", "Optical", "ConnectionBus", "Vendor", "Model",
                     "CanPowerOff")
_MAX_DEVICES = 512

# callback(ok, values): values are the reply's arguments, or (error name, message) when not ok
BusCall = Callable[[str, str, str, list, Callable[[bool, list], None]], None]


@dataclass
class _Read:
    done: Callable[[dict | None], None]
    blocks: dict[str, dict] = field(default_factory=dict)
    drives: dict[str, dict] = field(default_factory=dict)
    pending: int = 0
    finished: bool = False


class UDisksReader:
    """Reads what ``assemble`` needs from UDisks2 with asynchronous calls of simple types.

    ``call(object path, interface, method, arguments, callback)`` must call
    ``callback(ok, values)`` later (or at once), on the caller's thread.
    """

    def __init__(self, call: BusCall) -> None:
        self._call = call

    def read(self, done: Callable[[dict | None], None]) -> None:
        """Call ``done({"blocks": ..., "drives": ...})``, or ``done(None)`` when UDisks2 does not answer."""
        state = _Read(done)

        def listed(ok: bool, values: list) -> None:
            if not ok or not values or not isinstance(values[0], str):
                state.finished = True
                done(None)
                return
            names = _NODE.findall(values[0][:1024 * 1024])[:_MAX_DEVICES]
            if not names:
                self._finish(state)
                return
            state.pending += 1               # held while the calls go out: a bus may answer at once
            for name in names:
                self._first(state, f"{BLOCK_DEVICES}/{name}")
            state.pending -= 1
            self._check(state)

        self._call(BLOCK_DEVICES, INTROSPECTABLE, "Introspect", [], listed)

    def _get(self, state: _Read, path: str, interface: str, name: str, store: dict,
             then: Callable[[], None] | None = None) -> None:
        state.pending += 1

        def got(ok: bool, values: list) -> None:
            if ok and values:
                store[name] = values[0]
            state.pending -= 1
            if then is not None:
                then()
            self._check(state)

        self._call(path, PROPERTIES, "Get", [interface, name], got)

    def _first(self, state: _Read, path: str) -> None:
        props: dict = {}
        remaining = [len(_FIRST)]

        def one() -> None:
            remaining[0] -= 1
            if remaining[0]:
                return
            if props.get("IdUsage") not in ("filesystem", "crypto") or props.get("HintIgnore"):
                return
            state.blocks[path] = props
            rest = [len(_BLOCK_PROPERTIES)]

            def more() -> None:
                rest[0] -= 1
                if rest[0]:
                    return
                drive = str(props.get("Drive") or "")
                if drive not in ("", "/") and _OBJECT_PATH.match(drive) and drive not in state.drives:
                    target = state.drives.setdefault(drive, {})
                    state.pending += 1
                    for name in _DRIVE_PROPERTIES:
                        self._get(state, drive, DRIVE, name, target)
                    state.pending -= 1

            state.pending += 1
            for name in _BLOCK_PROPERTIES:
                self._get(state, path, BLOCK, name, props, more)
            state.pending -= 1

        state.pending += 1
        for name in _FIRST:
            self._get(state, path, BLOCK, name, props, one)
        state.pending -= 1

    def _check(self, state: _Read) -> None:
        if state.pending == 0 and not state.finished:
            self._finish(state)

    @staticmethod
    def _finish(state: _Read) -> None:
        state.finished = True
        blocks = {path: {key: _plain(value) for key, value in props.items()}
                  for path, props in state.blocks.items()}
        drives = {path: {key: _plain(value) for key, value in props.items()}
                  for path, props in state.drives.items()}
        state.done({"blocks": blocks, "drives": drives})


def _plain(value: object) -> object:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def udisks_error(name: str, message: str) -> str:
    """A UDisks2 or D-Bus error in plain words."""
    short = name.rsplit(".", 1)[-1]
    known = {
        "NotAuthorized": "You are not allowed to do this.",
        "NotAuthorizedCanObtain": "Authentication is needed to do this.",
        "NotAuthorizedDismissed": "Authentication was cancelled.",
        "AlreadyMounted": "The volume is already mounted.",
        "NotMounted": "The volume is not mounted.",
        "DeviceBusy": "The volume is in use. Close the files and folders on it and try again.",
        "AlreadyUnmounting": "The volume is already being removed.",
        "OptionNotPermitted": "This is not permitted for this volume.",
        "NotSupported": "This is not supported for this volume.",
        "Timedout": "The storage service did not answer in time.",
        "NoReply": "The storage service did not answer in time.",
        "ServiceUnknown": "The storage service (UDisks2) is not running.",
        "AccessDenied": "You are not allowed to do this.",
    }
    if short in known:
        return known[short]
    if short == "Failed" and "passphrase" in message.lower():
        return "That passphrase did not unlock the volume."
    text = message.strip().splitlines()[0] if message.strip() else short
    return text[:300]
