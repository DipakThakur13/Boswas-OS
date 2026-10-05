"""Read-only probes of public system files (no Qt, no processes).

Every function reads files any user may read and returns plain data; a
missing or unreadable file gives an empty value instead of an exception.
Paths go through ``Root`` so the tests can point the probes at a fake root
directory. Nothing here writes, and nothing reads a user's files except the
desktop entries of the XDG data directories (application names and icons).
"""

from __future__ import annotations

import os
import shutil

from . import viewmodel as vm
from .catalog import KCM_PLUGIN_DIRS, KCM_PLUGIN_SUBDIRS

MAX_FILE = 1024 * 1024
MAX_DESKTOP_FILE = 256 * 1024
MAX_DESKTOP_FILES = 5000
LOG_TAIL = 64 * 1024

RELEASE_FILE = "/usr/lib/boswas/release"
IMAGE_INFO_FILE = "/usr/lib/boswas/image-info"
UPDATE_CONF = "/etc/boswas/update.conf"
DEVICE_CONF = "/etc/boswas/device.conf"
DEBIAN_VERSION = "/etc/debian_version"
PLASMA_METAINFO = "/usr/share/metainfo/org.kde.plasmashell.metainfo.xml"
LIVE_MEDIUM = "/run/live/medium"
SESSION_MARKER = "/run/boswas/session"       # contains "live" in a live session
APT_CONF_DIR = "/etc/apt/apt.conf.d"
APT_PERIODIC_DIR = "/var/lib/apt/periodic"
APT_STAMPS = ("update-success-stamp", "update-stamp", "upgrade-stamp", "unattended-upgrades-stamp",
              "download-upgradeable-stamp", "autoclean-stamp")
UU_LOG = "/var/log/unattended-upgrades/unattended-upgrades.log"
POWER_SUPPLY_DIR = "/sys/class/power_supply"
# Only these attributes of a power supply are read (never serial numbers).
POWER_ATTRIBUTES = ("type", "status", "capacity", "online", "scope", "model_name")


class Root:
    """A file system root ("/" on a device, a temporary directory in the tests)."""

    def __init__(self, path: str = "/"):
        self.path = path or "/"

    def __call__(self, path: str) -> str:
        return os.path.join(self.path, path.lstrip("/"))

    def read(self, path: str, limit: int = MAX_FILE) -> str | None:
        try:
            with open(self(path), "rb") as handle:
                data = handle.read(limit)
        except OSError:
            return None
        return data.decode("utf-8", errors="replace")

    def read_tail(self, path: str, limit: int = LOG_TAIL) -> str | None:
        try:
            with open(self(path), "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - limit))
                data = handle.read(limit)
        except OSError:
            return None
        return data.decode("utf-8", errors="replace")

    def exists(self, path: str) -> bool:
        return os.path.exists(self(path))

    def isdir(self, path: str) -> bool:
        return os.path.isdir(self(path))

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(self(path)))
        except OSError:
            return []

    def mtime(self, path: str) -> float | None:
        try:
            return os.stat(self(path)).st_mtime
        except OSError:
            return None


# --- identity and hardware ------------------------------------------------------------------------------

def release(root: Root) -> dict:
    return {
        "release": vm.parse_env(root.read(RELEASE_FILE, 65536)),
        "image": vm.parse_env(root.read(IMAGE_INFO_FILE, 65536)),
        "update": vm.parse_env(root.read(UPDATE_CONF, 65536)),
        "debian_version": (root.read(DEBIAN_VERSION, 256) or "").strip() or None,
    }


def hardware(root: Root) -> dict:
    model, threads = vm.parse_cpuinfo(root.read("/proc/cpuinfo", 4 * MAX_FILE))
    uname = os.uname()
    return {
        "cpu_model": model or None,
        "cpu_threads": threads or None,
        "memory_bytes": vm.parse_meminfo(root.read("/proc/meminfo", 65536)),
        "kernel": uname.release,
        "machine": uname.machine,
        "hostname": uname.nodename,
        "boot_mode": "UEFI" if root.isdir("/sys/firmware/efi") else "BIOS",
    }


def live_session(root: Root) -> bool:
    return vm.is_live(root.exists(LIVE_MEDIUM), root.read("/proc/cmdline", 65536) or "",
                      root.read(SESSION_MARKER, 256))


def plasma_metainfo_version(root: Root) -> str | None:
    return vm.plasma_version_from_metainfo(root.read(PLASMA_METAINFO, 512 * 1024))


def device_settings_file(root: Root) -> dict:
    """The allowlisted settings of /etc/boswas/device.conf, shaped like the agent's device.config."""
    values = vm.parse_env(root.read(DEVICE_CONF, 65536))
    return {"settings": {k: values[k] for k in vm.PRIVACY_SETTINGS if k in values}, "source": "file"}


# --- storage and power -----------------------------------------------------------------------------------

def _usage(path: str) -> tuple[int, int] | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def _device(path: str) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def storage(root: Root) -> list[dict]:
    """Usage of / and, when it is a separate file system, /home (bytes)."""
    entries = []
    system = _usage(root("/"))
    if system:
        entries.append({"key": "system", "title": "System", "path": "/", "total": system[0], "free": system[1]})
    home_path = root("/home")
    if os.path.isdir(home_path) and _device(home_path) != _device(root("/")):
        home = _usage(home_path)
        if home:
            entries.append({"key": "home", "title": "Home", "path": "/home", "total": home[0], "free": home[1]})
    return entries


def power_supplies(root: Root) -> list[dict]:
    supplies = []
    for name in root.listdir(POWER_SUPPLY_DIR)[:32]:
        doc = {"name": name}
        for attribute in POWER_ATTRIBUTES:
            value = root.read(f"{POWER_SUPPLY_DIR}/{name}/{attribute}", 256)
            if value is not None:
                doc[attribute] = value.strip()
        supplies.append(doc)
    return supplies


# --- updates ------------------------------------------------------------------------------------------------

def apt_conf_texts(root: Root) -> list[str]:
    texts = []
    for name in root.listdir(APT_CONF_DIR)[:200]:
        if name.startswith(".") or name.endswith((".dpkg-old", ".dpkg-dist", ".dpkg-new", "~")):
            continue
        text = root.read(f"{APT_CONF_DIR}/{name}", 256 * 1024)
        if text:
            texts.append(text)
    return texts


def apt_stamps(root: Root) -> dict[str, float]:
    stamps = {}
    for name in APT_STAMPS:
        mtime = root.mtime(f"{APT_PERIODIC_DIR}/{name}")
        if mtime:
            stamps[name] = mtime
    return stamps


def unattended_log(root: Root) -> dict:
    text = root.read_tail(UU_LOG)
    if text is None:
        return {"readable": False, "lines": []}
    return {"readable": True, "lines": text.splitlines()[-400:]}


# --- KDE modules -----------------------------------------------------------------------------------------------

def kcm_installed(root: Root, module_id: str) -> bool:
    for base in KCM_PLUGIN_DIRS:
        for sub in KCM_PLUGIN_SUBDIRS:
            directory = f"{base}/{sub}" if sub else base
            if root.exists(f"{directory}/{module_id}.so"):
                return True
    return False


# --- desktop entries --------------------------------------------------------------------------------------------

def xdg_data_dirs(environ) -> list[str]:
    """$XDG_DATA_HOME (default ~/.local/share) first, then $XDG_DATA_DIRS (default /usr/local/share:/usr/share)."""
    dirs = []
    home = environ.get("XDG_DATA_HOME") or (os.path.join(environ["HOME"], ".local/share") if environ.get("HOME")
                                            else "")
    if home and os.path.isabs(home):
        dirs.append(home)
    for path in (environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if path and os.path.isabs(path) and path not in dirs:
            dirs.append(path)
    return dirs


def desktop_files(data_dirs: list[str]) -> list[tuple[str, str]]:
    """(desktop ID, path) of every *.desktop file; a directory earlier in the list wins for the same ID."""
    found: dict[str, str] = {}
    count = 0
    for data_dir in data_dirs:
        base = os.path.join(data_dir, "applications")
        if not os.path.isdir(base):
            continue
        local: dict[str, str] = {}
        for current, subdirs, files in os.walk(base):
            subdirs.sort()
            for name in sorted(files):
                if not name.endswith(".desktop"):
                    continue
                count += 1
                if count > MAX_DESKTOP_FILES:
                    break
                path = os.path.join(current, name)
                relative = os.path.relpath(path, base)
                desktop_id = relative[:-len(".desktop")].replace(os.sep, "-")
                local.setdefault(desktop_id, path)
        for desktop_id, path in local.items():
            found.setdefault(desktop_id, path)
    return sorted(found.items())


def desktop_entries(environ, locale: str | None = None) -> list[vm.DesktopEntry]:
    """Visible application entries for KDE, from the user's and the system's XDG data directories."""
    search_path = environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"

    def which(program: str) -> str | None:
        if os.path.isabs(program):
            return program if os.access(program, os.X_OK) else None
        return shutil.which(program, path=search_path)

    entries = []
    for desktop_id, path in desktop_files(xdg_data_dirs(environ)):
        try:
            with open(path, "rb") as handle:
                text = handle.read(MAX_DESKTOP_FILE).decode("utf-8", errors="replace")
        except OSError:
            continue
        entry = vm.parse_desktop_entry(text, desktop_id, path, locale, "KDE", which)
        if entry is not None:
            entries.append(entry)
    return entries
