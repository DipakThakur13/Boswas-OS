"""Shared fakes of the Control Center tests: commands, agent sockets, a root file system.

The real Backend is used everywhere; only its three boundaries are replaced:

  FakeCommands   records every argv instead of starting a process and
                 answers with canned output (boswas --json status, ...)
  FakeServices   the device agent and session agent sockets (localapi shape)
  make_root()    a temporary root directory with the public files the
                 probes read (/usr/lib/boswas/release, /proc, /sys, APT, ...)

The documents follow the real shapes: build/logs/boot-test/boswas-status.json,
the device agent's agent.status (daemon.py), the session agent's
system.status and apps.list (session_agent.py).

The tests add their own import paths (this package and the sibling packages
of the monorepo), so they run without PYTHONPATH.
"""

from __future__ import annotations

import copy
import json
import os
import struct
import sys
import threading
import zlib
from pathlib import Path

HERE = Path(__file__).resolve()
PACKAGE = HERE.parents[1]
for path in (PACKAGE.parent / "boswas-cli", PACKAGE.parent / "boswas-compat", PACKAGE.parent / "boswas-device-agent",
             PACKAGE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from boswas_agent.localapi import ApiError  # noqa: E402

from boswas_control_center import commands  # noqa: E402
from boswas_control_center.backend import AGENT_SOCKET, Backend  # noqa: E402
from boswas_control_center.catalog import KCM_MODULES  # noqa: E402

E = commands.EXECUTABLES

STATUS = {
    "schema": "boswas-cli/1", "command": "status", "generated_at": "2026-10-04T15:08:45Z",
    "compliance": {"state": "COMPLIANT_WITH_WARNINGS", "pass": 5, "warn": 3, "fail": 0, "unknown": 0,
                   "basis": "local self-assessment against the Boswas OS v1 alpha baseline (not attested)"},
    "checks": [
        {"id": "secure-boot", "category": "security", "title": "Secure Boot", "status": "WARN",
         "detail": "legacy BIOS boot; Boswas OS is UEFI-first", "scored": True},
        {"id": "tpm", "category": "security", "title": "TPM", "status": "WARN", "detail": "no TPM detected",
         "scored": True},
        {"id": "disk-encryption", "category": "security", "title": "Disk encryption", "status": "INFO",
         "detail": "live session: read-only image with RAM overlay, nothing is persisted", "scored": False},
        {"id": "firewall", "category": "security", "title": "Firewall", "status": "PASS",
         "detail": "nftables.service active (Boswas baseline ruleset)", "scored": True},
        {"id": "apparmor", "category": "security", "title": "AppArmor", "status": "WARN",
         "detail": "enabled in kernel; profiles are not loaded in the live session (they load once Boswas OS is installed)",
         "scored": True},
        {"id": "audit", "category": "security", "title": "Audit logging", "status": "PASS",
         "detail": "auditd.service active (Boswas audit rules)", "scored": True},
        {"id": "ssh-server", "category": "security", "title": "SSH server", "status": "PASS",
         "detail": "not installed (no inbound remote access)", "scored": True},
        {"id": "root-account", "category": "security", "title": "Root account", "status": "UNKNOWN",
         "detail": "run as root to check", "scored": False},
        {"id": "apt-trust", "category": "security", "title": "Repository trust", "status": "INFO",
         "detail": "live session: only the boot medium's package pool is unsigned (removed at install)",
         "scored": False},
        {"id": "screen-lock", "category": "security", "title": "Screen lock", "status": "PASS",
         "detail": "automatic lock after 10 min idle, enforced", "scored": True},
        {"id": "usb-policy", "category": "security", "title": "USB storage policy", "status": "INFO",
         "detail": "mode=allow (framework only; enforcement not implemented in v1 alpha)", "scored": False},
        {"id": "winapp-confinement", "category": "security", "title": "Windows app confinement", "status": "UNKNOWN",
         "detail": "run as root to check", "scored": False},
        {"id": "updates", "category": "updates", "title": "Security updates", "status": "PASS",
         "detail": "automatic security updates enabled; package lists 0 day(s) old", "scored": True},
        {"id": "agent", "category": "management", "title": "Boswas agent", "status": "INFO",
         "detail": "boswas-device-agent.service active; device READY, Control Plane STANDALONE", "scored": False},
        {"id": "enrollment", "category": "management", "title": "Enrollment", "status": "INFO",
         "detail": "unenrolled", "scored": False},
    ],
}

AGENT = {
    "schema": "boswas-agent-status/1", "agent_version": "1.0~alpha3",
    "device_id": "1010039e-ea3b-4597-8371-1c8ac9f6b075", "ephemeral": False, "state": "READY", "reasons": [],
    "since": "2026-10-04T15:00:00Z", "agent_enabled": True, "remote_commands": True, "maintenance": False,
    "connection": "STANDALONE", "control_plane": None, "enrollment": "unenrolled", "enrolled_at": None,
    "profile": None, "certificate_fingerprint": None,
    "policy": {"managed": False, "version": None, "name": None, "sequence": None, "applied_at": None},
    "last_contact": None, "last_attempt": None, "last_error": None, "consecutive_failures": 0,
    "inventory_revision": 1, "runtime_healthy": True, "config_valid": True, "updated_at": "2026-10-04T15:08:00Z",
}
AGENT_ENROLLED = {**AGENT, "connection": "CONNECTED", "control_plane": "https://control.example.invalid",
                  "enrollment": "enrolled",
                  "policy": {"managed": True, "version": "2026.10.1", "name": "Standard",
                             "applied_at": "2026-10-04T07:00:00Z"}}
CONFIG = {"path": "/etc/boswas/device.conf", "valid": True,
          "settings": {"AGENT_ENABLED": "yes", "CONTROL_PLANE_URL": "", "INVENTORY_POLICY": "standard",
                       "TELEMETRY_POLICY": "security", "UPDATE_POLICY": "security-only", "LOG_LEVEL": "info"},
          "problems": [], "warnings": []}
RUNTIME = {
    "wine": {"available": True, "version": "wine-10.0 (Debian 10.0~repack-6)", "major": "10"},
    "architectures": ["x86_64"], "winearch": "win64",
    "bubblewrap": {"available": True, "version": "0.11.0", "disable_userns": True},
    "runner": {"available": True}, "apparmor": {"profile": "boswas-winapp", "mode": "enforce", "required": True},
    "policy": {"source": "local", "managed": False, "problems": []}, "healthy": True,
}
SYSTEM = {
    "os": {"name": "Boswas OS", "version": "v1 Alpha", "version_id": "1.0~alpha3", "architecture": "x86_64"},
    "windows_architectures": ["x86_64"], "runtime": copy.deepcopy(RUNTIME), "agent": copy.deepcopy(AGENT),
    "disk": {"total_gib": 58.0, "free_gib": 41.2},
    "session_agent": {"version": "1.0~alpha3", "connected_to_agent": True},
}
WINDOWS_APPS = [
    {"id": "com.example.notepad", "name": "Example Notepad", "version": "2.1", "publisher": "Example Ltd",
     "status": "approved", "app_state": "INSTALLED", "architecture": "x86_64"},
    {"id": "com.example.paint", "name": "Example Paint", "version": "1.0", "publisher": "Example Ltd",
     "status": "tested", "app_state": "RUNNING", "architecture": "x86_64"},
]


def preset_doc(root: Path) -> dict:
    share = root / "usr/share/boswas/presets"
    return {
        "presets": [
            {"id": "boswas-dark", "name": "Boswas Dark", "description": "Deep navy with the Boswas gold accent.",
             "variant": "dark", "accent": "#D9B26E", "preview": str(share / "boswas-dark.png")},
            {"id": "boswas-light", "name": "Boswas Light", "description": "Bright surfaces with gold details.",
             "variant": "light", "accent": "#8A6A2F", "preview": str(share / "boswas-light.png")},
            {"id": "classic", "name": "Classic", "description": "A traditional layout with a bottom panel.",
             "variant": "light", "accent": "#5B7FD6", "preview": None},
            {"id": "../evil", "name": "Bad ID", "description": "must be skipped", "variant": "dark"},
            {"id": "odd-preview", "name": "Odd Preview", "description": "preview outside the rules",
             "variant": "purple", "accent": "red", "preview": "/usr/share/../etc/shadow.png"},
        ],
        "current": "boswas-dark",
    }


APT_SHELL = "UU='1'\nPL='1'\nDL=''\nAC='7'\n"


def completed(stdout: str = "", code: int = 0, stderr: str = "") -> commands.Completed:
    return commands.Completed(code, stdout, stderr)


class FakeCommands:
    """Stands in for commands.CommandRunner: records argv, never starts a process."""

    def __init__(self, root: Path):
        self.installed = set(E.values()) - {E["plasmashell"]}
        self.outputs: dict[tuple, commands.Completed] = {
            (E["boswas"], "--json", "status"): completed(json.dumps(STATUS)),
            (E["boswas-preset"], "--json", "list"): completed(json.dumps(preset_doc(root))),
            (E["systemctl"], "reboot"): completed(),
        }
        self.ran: list[list[str]] = []
        self.started: list[list[str]] = []
        self.timeouts: list[float] = []
        self.start_errors: dict[tuple, Exception] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _check(command: list[str]) -> None:
        assert isinstance(command, list) and all(isinstance(a, str) for a in command), command
        assert command[0] in commands.ALLOWED_PATHS, command

    def available(self, path: str) -> bool:
        return path in self.installed

    def run(self, command: list[str], timeout: float = commands.DEFAULT_TIMEOUT) -> commands.Completed:
        self._check(command)
        assert timeout and timeout > 0
        with self.lock:
            self.ran.append(list(command))
            self.timeouts.append(timeout)
        if command[0] == E["apt-config"]:
            return completed(APT_SHELL)
        if command[:2] == [E["boswas-preset"], "apply"]:
            return self.outputs.get(tuple(command), completed())
        return self.outputs.get(tuple(command), completed("", 1, "no canned output"))

    def start(self, command: list[str], settle: float = commands.START_SETTLE) -> None:
        self._check(command)
        with self.lock:
            self.started.append(list(command))
        error = self.start_errors.get(tuple(command))
        if error is not None:
            raise error

    def ran_program(self, name: str) -> list[list[str]]:
        with self.lock:
            return [c for c in self.ran if c[0] == E[name]]


class FakeServices:
    """The device agent and session agent sockets."""

    def __init__(self):
        self.agent = {"agent.status": copy.deepcopy(AGENT), "device.config": copy.deepcopy(CONFIG),
                      "runtime.status": copy.deepcopy(RUNTIME)}
        self.session = {"apps.list": {"applications": copy.deepcopy(WINDOWS_APPS)},
                        "system.status": copy.deepcopy(SYSTEM)}
        self.agent_down = False
        self.session_down = False
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def factory(self, path: str, timeout: float = 30):
        return FakeClient(self, "agent" if path == AGENT_SOCKET else "session")


class FakeClient:
    def __init__(self, services: FakeServices, service: str):
        self.services, self.service = services, service

    def call(self, op: str, timeout: float | None = None, **params):
        s = self.services
        with s.lock:
            s.calls.append((self.service, op))
        if (s.agent_down if self.service == "agent" else s.session_down):
            raise ConnectionError(f"the service socket /run/{self.service}.sock is not available (No such file)")
        table = s.agent if self.service == "agent" else s.session
        if op not in table:
            raise ApiError("UNKNOWN_OPERATION", f"unknown operation {op!r}")
        value = table[op]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


# --- the fake root file system -------------------------------------------------------------------------

RELEASE = """# Boswas OS release identity.
BOSWAS_NAME="Boswas OS"
BOSWAS_VERSION="v1 Alpha"
BOSWAS_VERSION_ID="1.0~alpha3"
BOSWAS_CHANNEL="dev"
BOSWAS_BASE_NAME="Debian"
"""
IMAGE_INFO = """# Boswas OS image build metadata (generated by build.sh)
BOSWAS_NAME="Boswas OS"
BOSWAS_VERSION="v1 Alpha"
BOSWAS_VERSION_ID="1.0~alpha3"
BOSWAS_BUILD_ID="BOS-1.0~alpha3-20261004T144858Z"
BOSWAS_BUILD_DATE="2026-10-04T14:48:58Z"
BOSWAS_GIT_COMMIT="205e3f45b5fe0fe5f5ac29ea3ed93f65330e1ec0"
"""
CPUINFO = ("processor\t: 0\nvendor_id\t: GenuineIntel\nmodel name\t: Intel(R) Core(TM) i7-1265U\n\n"
           "processor\t: 1\nvendor_id\t: GenuineIntel\nmodel name\t: Intel(R) Core(TM) i7-1265U\n\n")
MEMINFO = "MemTotal:       16303520 kB\nMemFree:         8123456 kB\n"
METAINFO = ('<?xml version="1.0"?><component><id>org.kde.plasmashell</id><releases>'
            '<release version="6.3.6" date="2025-07-08"/><release version="6.3.5"/></releases></component>')
UU_LOG = ("2026-10-03 06:12:01,123 INFO Starting unattended upgrades script\n"
          "2026-10-03 06:12:05,456 INFO Packages that will be upgraded: libssl3t64 openssl\n"
          "2026-10-03 06:13:10,789 INFO All upgrades installed\n")
STAMP_TIME = 1791100000          # 2026-10-04


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A tiny solid-colour PNG (no Qt needed)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


MISSING_KCMS = frozenset({"kcm_feedback", "kcm_tablet"})


def make_root(base: Path, live: bool = False, battery: bool = True, uu_log: bool = True) -> Path:
    root = base / "root"
    write(root / "usr/lib/boswas/release", RELEASE)
    write(root / "usr/lib/boswas/image-info", IMAGE_INFO)
    write(root / "etc/debian_version", "13.7\n")
    write(root / "etc/boswas/update.conf", 'CHANNEL="dev"\nREPOSITORY=""\n')
    write(root / "etc/boswas/device.conf", 'AGENT_ENABLED="yes"\nINVENTORY_POLICY="minimal"\n'
                                           'TELEMETRY_POLICY="none"\nUPDATE_POLICY="security-only"\n'
                                           'CONTROL_PLANE_URL="https://secret.example.invalid"\n')
    write(root / "proc/cpuinfo", CPUINFO)
    write(root / "proc/meminfo", MEMINFO)
    if live:
        write(root / "proc/cmdline", "BOOT_IMAGE=/live/vmlinuz boot=live components quiet splash\n")
        (root / "run/live/medium").mkdir(parents=True)
    else:
        write(root / "proc/cmdline", "BOOT_IMAGE=/boot/vmlinuz-6.12 root=UUID=1234 ro quiet\n")
    if battery:
        write(root / "sys/class/power_supply/BAT0/type", "Battery\n")
        write(root / "sys/class/power_supply/BAT0/status", "Discharging\n")
        write(root / "sys/class/power_supply/BAT0/capacity", "76\n")
        write(root / "sys/class/power_supply/BAT0/model_name", "Example Battery\n")
        write(root / "sys/class/power_supply/BAT0/serial_number", "SECRET-SERIAL\n")
    write(root / "sys/class/power_supply/AC/type", "Mains\n")
    write(root / "sys/class/power_supply/AC/online", "0\n")
    stamp = write(root / "var/lib/apt/periodic/update-success-stamp", "")
    os.utime(stamp, (STAMP_TIME, STAMP_TIME))
    write(root / "etc/apt/apt.conf.d/20auto-upgrades", 'APT::Periodic::Update-Package-Lists "1";\n'
                                                       'APT::Periodic::Unattended-Upgrade "1";\n')
    if uu_log:
        write(root / "var/log/unattended-upgrades/unattended-upgrades.log", UU_LOG)
    write(root / "usr/share/metainfo/org.kde.plasmashell.metainfo.xml", METAINFO)
    kcms = root / "usr/lib/x86_64-linux-gnu/qt6/plugins/plasma/kcms"
    for module in sorted(KCM_MODULES - MISSING_KCMS):
        sub = "systemsettings_qwidgets" if module in ("kcm_networkmanagement", "kcm_proxy", "kcm_kwallet5", "kcm_clock",
                                                      "kcm_recentFiles") else "systemsettings"
        write(kcms / sub / f"{module}.so", "")
    write(root / "usr/share/boswas/presets/boswas-dark.png", png(32, 18, (17, 26, 43)))
    write(root / "usr/share/boswas/presets/boswas-light.png", png(32, 18, (240, 242, 246)))
    (root / "home").mkdir(parents=True, exist_ok=True)
    return root


DESKTOP = "[Desktop Entry]\nType=Application\n"


def make_xdg(base: Path) -> dict:
    """XDG data directories with desktop entries; returns the environment to use."""
    user = base / "home/alice/.local/share/applications"
    system = base / "usr/share/applications"
    write(system / "org.kde.kate.desktop", DESKTOP + "Name=Kate\nName[de]=Kate (de)\nGenericName=Advanced Text Editor\n"
          "Comment=KDE's advanced text editor\nIcon=kate\nExec=kate -b %U\nCategories=Qt;KDE;Utility;TextEditor;\n"
          "Keywords=text;editor;code;\n")
    write(system / "org.kde.dolphin.desktop", DESKTOP + "Name=Dolphin\nGenericName=File Manager\nIcon=org.kde.dolphin\n"
          "Exec=dolphin %u\nCategories=Qt;KDE;System;FileManager;\n")
    write(system / "firefox-esr.desktop", DESKTOP + "Name=Firefox ESR\nComment=Browse the World Wide Web\n"
          "Icon=firefox-esr\nExec=/usr/lib/firefox-esr/firefox-esr %u\n")
    write(system / "hidden.desktop", DESKTOP + "Name=Hidden Tool\nExec=hidden\nNoDisplay=true\n")
    write(system / "gnome-only.desktop", DESKTOP + "Name=GNOME Only\nExec=gnome-thing\nOnlyShowIn=GNOME;\n")
    write(system / "not-kde.desktop", DESKTOP + "Name=Not In KDE\nExec=thing\nNotShowIn=KDE;\n")
    write(system / "kde-only.desktop", DESKTOP + "Name=KDE Only\nExec=kde-thing\nOnlyShowIn=KDE;\n")
    write(system / "missing-tryexec.desktop", DESKTOP + "Name=Missing Program\nExec=nothing\n"
          "TryExec=/nonexistent/program\n")
    write(system / "link.desktop", "[Desktop Entry]\nType=Link\nName=A Link\nURL=https://example.invalid\n")
    write(system / "noexec.desktop", DESKTOP + "Name=No Exec\n")
    write(system / "kcm_networkmanagement.desktop", DESKTOP + "Name=Wi-Fi & Networking\nExec=systemsettings kcm_nm\n"
          "NoDisplay=true\n")
    write(system / "com.boswas.CompatibilityManager.desktop", DESKTOP + "Name=Compatibility Manager\n"
          "GenericName=Windows Application Manager\nExec=boswas-compat-manager\nIcon=boswas-compat-manager\n")
    write(system / "com.boswas.ControlCenter.desktop", DESKTOP + "Name=Boswas Control Center\n"
          "Exec=boswas-control-center\n")
    write(system / "kde4" / "oldapp.desktop", DESKTOP + "Name=Old App\nExec=oldapp\n")
    write(user / "firefox-esr.desktop", DESKTOP + "Name=Firefox (Custom)\nExec=firefox-esr --private-window\n"
          "Icon=firefox-esr\n")
    write(user / "boswas-winapp-com.example.notepad.desktop", DESKTOP + "Name=Example Notepad\n"
          "Exec=boswas-winapp launch com.example.notepad\nX-Boswas-WinApp-Id=com.example.notepad\n")
    write(user / "dolphin-hidden-override.desktop", DESKTOP + "Name=Override\nExec=x\nHidden=true\n")
    runtime = base / "run/user/1000"
    runtime.mkdir(parents=True, exist_ok=True)
    return {"HOME": str(base / "home/alice"), "XDG_DATA_DIRS": f"{base / 'usr/share'}", "LANG": "en_US.UTF-8",
            "PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(runtime)}


class Rig:
    """A real Backend wired to the fakes."""

    def __init__(self, base: Path, live: bool = False, **root_options):
        self.base = Path(base)
        self.root = make_root(self.base, live=live, **root_options)
        self.environ = make_xdg(self.base)
        self.commands = FakeCommands(self.root)
        self.services = FakeServices()
        self.backend = Backend(root=str(self.root), environ=self.environ, commands_runner=self.commands,
                               client_factory=self.services.factory)
