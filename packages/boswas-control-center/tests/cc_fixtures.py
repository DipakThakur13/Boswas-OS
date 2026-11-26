"""Shared fakes of the Control Center tests: commands, agent sockets, a root file system.

The real Backend is used everywhere; only its three boundaries are replaced:

  FakeCommands   records every argv instead of starting a process and
                 answers with canned output (boswas --json status, ...);
                 streamed operations (boswas-sysops, boswas-android) play a
                 script of output lines, optionally waiting for Cancel
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
import gzip
import json
import os
import struct
import sys
import threading
import zlib
from pathlib import Path

HERE = Path(__file__).resolve()
PACKAGE = HERE.parents[1]
# The sibling packages of the monorepo, and the Boswas UI toolkit the widgets are built on.
for path in (PACKAGE.parent / "boswas-cli", PACKAGE.parent / "boswas-compat", PACKAGE.parent / "boswas-device-agent",
             PACKAGE.parents[1] / "desktop" / "ui" / "qt", PACKAGE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from boswas_agent.localapi import ApiError  # noqa: E402

# The toolkit writes the images of its style sheet to the user's cache; in a test run they go to a
# temporary directory instead (boswas_fluent.assets needs no Qt).
import tempfile  # noqa: E402

from boswas_fluent import assets as _fluent_assets  # noqa: E402

_FLUENT_IMAGES = tempfile.TemporaryDirectory(prefix="fluent-test-images-")
_fluent_assets.set_cache_root(_FLUENT_IMAGES.name)

from boswas_control_center import commands  # noqa: E402
from boswas_control_center.backend import AGENT_SOCKET, Backend  # noqa: E402

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
    "architectures": ["x86_64", "x86"], "winearch": "win64",
    "bubblewrap": {"available": True, "version": "0.11.0", "disable_userns": True},
    "runner": {"available": True}, "apparmor": {"profile": "boswas-winapp", "mode": "enforce", "required": True},
    "policy": {"source": "local", "managed": False, "problems": []}, "healthy": True,
}
SYSTEM = {
    "os": {"name": "Boswas OS", "version": "v1 Alpha", "version_id": "1.0~alpha3", "architecture": "x86_64"},
    "windows_architectures": ["x86_64", "x86"], "runtime": copy.deepcopy(RUNTIME), "agent": copy.deepcopy(AGENT),
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

# --- outputs of the programs behind the functional pages (real formats) ---------------------------------------

UPGRADE_SIM = """NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
      Keep also in mind that locking is deactivated,
      so don't depend on the relevance to the real current situation!
Reading package lists...
Building dependency tree...
Calculating upgrade...
The following packages will be upgraded:
  libssl3t64 openssl firefox-esr
3 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.
Inst libssl3t64 [3.5.1-1] (3.5.2-1~deb13u1 Debian-Security:13/stable-security [amd64])
Inst openssl [3.5.1-1] (3.5.2-1~deb13u1 Debian-Security:13/stable-security [amd64])
Inst firefox-esr [140.3.0esr-1~deb13u1] (140.4.0esr-1~deb13u1 Debian:13.2/stable [amd64])
Inst libnew1 (1.0-1 Debian:13.2/stable [amd64])
Conf libssl3t64 (3.5.2-1~deb13u1 Debian-Security:13/stable-security [amd64])
Conf openssl (3.5.2-1~deb13u1 Debian-Security:13/stable-security [amd64])
"""
APT_SEARCH = "vlc - multimedia player and streamer\nvlc-bin - binaries from VLC\nbogus name - skipped\n"
APT_POLICY = """vlc:
  Installed: (none)
  Candidate: 3.0.21-10
  Version table:
     3.0.21-10 500
        500 http://deb.debian.org/debian trixie/main amd64 Packages
vlc-bin:
  Installed: 3.0.21-10
  Candidate: 3.0.21-10
  Version table:
 *** 3.0.21-10 500
        500 http://deb.debian.org/debian trixie/main amd64 Packages
        100 /var/lib/dpkg/status
"""
APT_SHOW = """Package: vlc
Version: 3.0.21-10
Installed-Size: 217
Maintainer: Debian Multimedia Maintainers <debian-multimedia@lists.debian.org>
Section: video
Size: 64536
Homepage: https://www.videolan.org/vlc/
Description: multimedia player and streamer
 VLC is the VideoLAN project's media player.
 .
 It plays MPEG, MPEG-2, MPEG-4, DivX, MOV, WMV and more.

Package: vlc
Version: 3.0.20-1
Description: an older stanza that must be ignored
"""
NMCLI_DEVICES = ("enp3s0:ethernet:unavailable:--\nwlp2s0:wifi:connected:Home\\:Net\n"
                 "lo:loopback:connected (externally):lo\np2p-dev-wlp2s0:wifi-p2p:disconnected:--\n")
NMCLI_SHOW = "IP4.ADDRESS[1]:192.168.1.23/24\nIP4.GATEWAY:192.168.1.1\nIP6.ADDRESS[1]:fe80\\:\\:1/64\n"
LSBLK = json.dumps({"blockdevices": [
    {"name": "nvme0n1", "type": "disk", "size": 512110190592, "fstype": None, "mountpoints": [None],
     "model": "Example NVMe SSD", "tran": "nvme", "rm": False, "ro": False, "children": [
         {"name": "nvme0n1p1", "type": "part", "size": 536870912, "fstype": "vfat", "mountpoints": ["/boot/efi"]},
         {"name": "nvme0n1p3", "type": "part", "size": 510000000000, "fstype": "crypto_LUKS", "mountpoints": [None],
          "children": [{"name": "nvme0n1p3_crypt", "type": "crypt", "size": 509000000000, "fstype": "LVM2_member",
                        "mountpoints": [None], "children": [
                            {"name": "boswas--vg-root", "type": "lvm", "size": 500000000000, "fstype": "ext4",
                             "mountpoints": ["/"]}]}]}]},
    {"name": "loop0", "type": "loop", "size": 1000, "mountpoints": ["/snap"]},
    {"name": "sda", "type": "disk", "size": 32000000000, "model": "USB Stick", "tran": "usb", "rm": True,
     "ro": False, "fstype": None, "mountpoints": [None], "children": []},
]})
FLATPAK_SYSTEM_REMOTES = "flathub\thttps://dl.flathub.org/repo/\tsystem\n"
FLATPAK_USER_APPS = "org.gimp.GIMP\tGNU Image Manipulation Program\t3.0.4\tflathub\n"
FLATPAK_SEARCH = "org.videolan.VLC\tVLC\tVLC media player\t3.0.21\tflathub\n"

# Canned answers of the places added with the categories (fonts, printers, VPN, permissions, packages, logind).
FONT_LIST = ("Inter\tRegular\t/usr/share/fonts/truetype/inter/Inter-Regular.ttf\n"
             "Inter\tBold\t/usr/share/fonts/truetype/inter/Inter-Bold.ttf\n"
             "Noto Sans\tRegular\t/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf\n"
             "Liberation Serif\tRegular\t/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf\n"
             "Hack\tRegular\t/usr/share/fonts/truetype/hack/Hack-Regular.ttf\n"
             "Handwriting Demo\tRegular\t{home}/.local/share/fonts/HandwritingDemo.ttf\n"
             "broken line without tabs\n")
LPSTAT_E = "Office_Laser\nHome_Inkjet\n"
LPSTAT_PD = ("printer Home_Inkjet is idle.  enabled since Sat 21 Nov 2026 09:00:00\n"
             "printer Office_Laser disabled since Sat 21 Nov 2026 08:00:00 -\n\treason unknown\n"
             "system default destination: Office_Laser\n")
VPN_UUID = "5b2c7f2e-1d3a-4c55-9e0b-3f6a7d8e9a10"
WG_UUID = "0c9d8e7f-6a5b-4c3d-2e1f-0a9b8c7d6e5f"
NMCLI_CONNECTIONS = (f"Office VPN:{VPN_UUID}:vpn::no\n"
                     f"wg-home:{WG_UUID}:wireguard:wg-home:yes\n"
                     "Home\\:Net:11111111-2222-3333-4444-555555555555:802-11-wireless:wlp2s0:yes\n"
                     "not a uuid:x:vpn::no\n")
WIRED_UUID = "7d3c1a2b-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
NMCLI_WIRED_DEVICES = "enp3s0:ethernet:connected:Wired connection 1\nwlp2s0:wifi:disconnected:--\n"
NMCLI_WIRED_CONNECTIONS = (f"Office VPN:{VPN_UUID}:vpn::no\n"
                           f"Wired connection 1:{WIRED_UUID}:802-3-ethernet:enp3s0:yes\n"
                           "Spare:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:802-3-ethernet::no\n")
NMCLI_WIRED_SETTINGS = (f"connection.id:Wired connection 1\nconnection.uuid:{WIRED_UUID}\n"
                        "connection.metered:unknown\nipv4.method:auto\nipv4.addresses:\nipv4.gateway:--\n"
                        "ipv4.dns:\nipv4.ignore-auto-dns:no\nipv6.dns:\nipv6.ignore-auto-dns:no\n")
NMCLI_WIRED_DEVICE = ("GENERAL.HWADDR:52\\:54\\:00\\:12\\:34\\:56\nIP4.ADDRESS[1]:192.168.1.23/24\n"
                      "IP4.GATEWAY:192.168.1.1\nIP4.DNS[1]:192.168.1.1\nIP6.ADDRESS[1]:fe80\\:\\:5054\\:ff\\:fe12\\:3456/64\n"
                      "IP6.GATEWAY:--\n")


def wired_outputs(settings: str = NMCLI_WIRED_SETTINGS) -> dict:
    """The answers of nmcli for a computer whose Ethernet adapter enp3s0 is connected (see test_pages_ui)."""
    from boswas_control_center import netconfig
    nmcli = E["nmcli"]
    return {
        (nmcli, "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"): completed(NMCLI_WIRED_DEVICES),
        (nmcli, "-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "connection", "show"): completed(NMCLI_WIRED_CONNECTIONS),
        (nmcli, "-t", "-f", netconfig.WIRED_FIELDS, "connection", "show", "uuid", WIRED_UUID): completed(settings),
        (nmcli, "-t", "-f", netconfig.DEVICE_FIELDS, "device", "show", "enp3s0"): completed(NMCLI_WIRED_DEVICE),
    }


PERMISSIONS = ("devices\tcamera\torg.example.Meet\tyes\t0x00\n"
               "location\tlocation\torg.example.Maps\tEXACT,1700000000\t\n"
               "screenshot\tscreenshot\torg.example.Draw\tno\t\n"
               "background\tbackground\torg.example.Meet\tyes\t\n"
               "notifications\tnotification\t\tyes\t\n")
# Debian packages of the fake desktop entries and their states (dpkg-query); cups is not installed.
OWNERS = {"org.kde.kate.desktop": "kate", "org.kde.dolphin.desktop": "dolphin", "firefox-esr.desktop": "firefox-esr",
          "com.boswas.CompatibilityManager.desktop": "boswas-compat-manager",
          "com.boswas.ControlCenter.desktop": "boswas-control-center"}
PACKAGE_STATES = {"kate": ("ii ", "24.12.3-1", 9000), "dolphin": ("ii ", "24.12.3-1", 12000),
                  "firefox-esr": ("ii ", "140.5.0esr-1", 250000), "boswas-compat-manager": ("ii ", "3.0", 400),
                  "boswas-control-center": ("ii ", "3.0", 2000), "7zip": ("ii ", "24.09+dfsg-4", 6000),
                  "fonts-noto-color-emoji": ("ii ", "2.047-1", 11000), "flatpak": ("ii ", "1.16.1-1", 5000)}
LOGIND = {"CanRebootToFirmwareSetup": "yes", "CanRebootToBootLoaderMenu": "na"}
HISTORY_LOG = """Start-Date: 2026-11-01  10:00:00
Commandline: apt-get install vlc
Requested-By: alice (1000)
Install: vlc:amd64 (3.0.21-1), vlc-bin:amd64 (3.0.21-1, automatic)
End-Date: 2026-11-01  10:01:00

Start-Date: 2026-11-03  06:12:01
Commandline: /usr/bin/unattended-upgrade
Upgrade: libssl3t64:amd64 (3.5.1-1, 3.5.1-1+deb13u1), openssl:amd64 (3.5.1-1, 3.5.1-1+deb13u1)
End-Date: 2026-11-03  06:13:10
"""
HISTORY_LOG_OLD = """Start-Date: 2026-10-02  09:00:00
Commandline: /usr/bin/apt-get -q -y remove gimp
Requested-By: alice (1000)
Remove: gimp:amd64 (3.0.4-1)
End-Date: 2026-10-02  09:00:30
"""


def package_states(command: list[str]) -> "commands.Completed":
    lines = []
    for name in command[3:]:
        status, version, size = PACKAGE_STATES.get(name, ("un ", "", 0))
        if status.startswith("ii"):
            lines.append(f"{name}\t{status}\t{size}\t{version}")
    return completed("\n".join(lines) + "\n", 0 if len(lines) == len(command[3:]) else 1,
                     "" if len(lines) == len(command[3:]) else "dpkg-query: no packages found matching some")


def package_owners(command: list[str]) -> "commands.Completed":
    lines = [f"{OWNERS[os.path.basename(path)]}: {path}" for path in command[2:] if os.path.basename(path) in OWNERS]
    return completed("\n".join(lines) + "\n", 0 if lines else 1)


ANDROID_STATUS = {
    "schema": "boswas-android/1",
    "runtime": {"installed": True, "version": "1.5.4", "repository": True},
    "compatibility": {"supported": True, "checks": [
        {"id": "binder", "status": "PASS", "title": "Android binder driver",
         "detail": "binder_linux is loaded (/dev/binder, /dev/hwbinder, /dev/vndbinder)"},
        {"id": "wayland", "status": "PASS", "title": "Wayland session", "detail": "Plasma Wayland session"},
        {"id": "gpu", "status": "WARN", "title": "Graphics", "detail": "software rendering (no supported GPU)"}]},
    "initialized": True,
    "images": {"type": "VANILLA", "system": "lineage-20.0-20261001", "vendor": "MAINLINE-20261001",
               "bytes": 1200000000},
    "service": {"active": True}, "session": {"running": False, "state": "STOPPED"},
    "storage": {"images_bytes": 1200000000, "user_bytes": 52428800},
    "network": {"interface": "waydroid0", "present": False, "address": None},
    "gpu": {"acceleration": "software", "driver": "virtio_gpu", "render_node": "/dev/dri/renderD128"},
    "google_play": {"available": False,
                    "reason": "Google Play is not offered: Google licenses its apps only for certified devices, and "
                              "no licensed installation method exists for this Android runtime.",
                    "alternatives": ["fdroid", "local-apk"]},
}
ANDROID_STATUS_NOT_INSTALLED = {**copy.deepcopy(ANDROID_STATUS), "runtime": {"installed": False, "version": None,
                                                                            "repository": False},
                                "initialized": False, "service": {"active": None},
                                "session": {"running": False, "state": None}, "images": {}}
ANDROID_STATUS_UNSUPPORTED = {**copy.deepcopy(ANDROID_STATUS_NOT_INSTALLED), "compatibility": {
    "supported": False, "checks": [{"id": "binder", "status": "FAIL", "title": "Android binder driver",
                                    "detail": "the kernel has no binder support"}]}}
ANDROID_APPS = {"schema": "boswas-android/1",
                "apps": [{"package": "org.fdroid.fdroid", "name": "F-Droid", "version": "1.20.0"},
                         {"package": "bad package", "name": "skipped"}]}
APK_DOC = {"schema": "boswas-android/1", "apk": {
    "path": "/home/alice/app.apk", "size": 4096, "sha256": "ab" * 32, "package": "org.example.notes",
    "version_name": "2.1", "version_code": 21, "label": "Example Notes", "min_sdk": 24, "target_sdk": 34,
    "abis": ["x86_64", "arm64-v8a"], "split": {"is_split": False, "name": None, "requires_splits": False},
    "signing": {"schemes": ["v2", "v3"], "certificates": [{"sha256": "CD" * 32}]}, "compatible": True,
    "problems": []}}
APK_ARM_ONLY = {"schema": "boswas-android/1", "apk": {**APK_DOC["apk"], "abis": ["arm64-v8a"], "compatible": False,
                                                      "problems": ["needs an ARM translation layer, which Boswas OS "
                                                                   "does not include"]}}

def sysops_lines(operation: str, ok: bool = True, code: str = "OK", message: str = "Done.", data: dict | None = None,
                 progress: tuple = (("download", 50.0, True), ("install", None, False))) -> list[str]:
    """The JSON Lines boswas-sysops prints (contract section 1)."""
    lines = [json.dumps({"type": "start", "schema": "boswas-sysops/1", "operation": operation,
                         "cancellable": True})]
    for phase, percent, cancellable in progress:
        lines.append(json.dumps({"type": "progress", "phase": phase, "percent": percent, "message": f"{phase}…",
                                 "cancellable": cancellable}))
    lines.append(json.dumps({"type": "result", "ok": ok, "code": code, "message": message, "data": data or {}}))
    return lines


def completed(stdout: str = "", code: int = 0, stderr: str = "") -> commands.Completed:
    return commands.Completed(code, stdout, stderr)


class FakeCommands:
    """Stands in for commands.CommandRunner: records argv, never starts a process."""

    def __init__(self, root: Path):
        self.installed = set(E.values())
        self.outputs: dict[tuple, commands.Completed] = {
            (E["boswas"], "--json", "status"): completed(json.dumps(STATUS)),
            (E["boswas-preset"], "--json", "list"): completed(json.dumps(preset_doc(root))),
            (E["systemctl"], "reboot"): completed(),
        }
        sysops = commands.SYSOPS
        self.outputs.update({
            (E["apt-get"], "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"): completed(UPGRADE_SIM),
            (E["apt-cache"], "search", "--names-only", "vlc"): completed(APT_SEARCH),
            (E["apt-cache"], "policy", "vlc", "vlc-bin"): completed(APT_POLICY),
            (E["apt-cache"], "policy", "vlc"): completed(APT_POLICY),
            (E["apt-cache"], "show", "vlc"): completed(APT_SHOW),
            (E["nmcli"], "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"): completed(NMCLI_DEVICES),
            (E["nmcli"], "-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP6.ADDRESS", "device", "show", "wlp2s0"):
                completed(NMCLI_SHOW),
            (E["nmcli"], "-t", "radio", "wifi"): completed("enabled\n"),
            (E["lsblk"], "-J", "-b", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,TRAN,RM,RO"): completed(LSBLK),
            (E["flatpak"], "remotes", "--system", "--columns=name,url,options"): completed(FLATPAK_SYSTEM_REMOTES),
            (E["flatpak"], "remotes", "--user", "--columns=name,url,options"): completed(""),
            (E["flatpak"], "list", "--user", "--app", "--columns=application,name,version,origin"):
                completed(FLATPAK_USER_APPS),
            (E["flatpak"], "list", "--system", "--app", "--columns=application,name,version,origin"): completed(""),
            (E["flatpak"], "search", "--columns=application,name,description,version,remotes", "vlc"):
                completed(FLATPAK_SEARCH),
            (E["boswas-android"], "--json", "status"): completed(json.dumps(ANDROID_STATUS)),
            (E["boswas-android"], "--json", "app", "list"): completed(json.dumps(ANDROID_APPS)),
        })
        home = str(Path(root).parent / "home" / "alice")
        self.outputs.update({
            (E["fc-list"], commands.FONT_LIST_FORMAT): completed(FONT_LIST.replace("{home}", home)),
            (E["lpstat"], "-e"): completed(LPSTAT_E),
            (E["lpstat"], "-p", "-d"): completed(LPSTAT_PD),
            (E["nmcli"], "-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "connection", "show"): completed(NMCLI_CONNECTIONS),
            (E["flatpak"], "permission-list"): completed(PERMISSIONS),
            (E["systemctl"], "is-active", "bluetooth.service"): completed("active\n"),
            (E["locale"], "-a"): completed("C\nC.utf8\nPOSIX\nde_DE.utf8\nen_US.utf8\n"),
        })
        for question, answer in LOGIND.items():
            self.outputs[tuple(commands.logind_argv(question))] = completed(json.dumps({"type": "s",
                                                                                        "data": [answer]}))
        # Programs whose answer depends on the arguments: a function of the command.
        self.handlers = {E["dpkg-query"]: lambda c: package_states(c) if c[1] == "-W" else package_owners(c)}
        self.ran: list[list[str]] = []
        self.started: list[list[str]] = []
        self.streamed: list[list[str]] = []
        self.streams: dict[tuple, object] = {}
        self.timeouts: list[float] = []
        self.start_errors: dict[tuple, Exception] = {}
        self.lock = threading.Lock()
        self.sysops = sysops

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
        if command[:2] == [E["boswas-preset"], "apply"] or command == [E["boswas-preset"], "reset"]:
            return self.outputs.get(tuple(command), completed())
        if tuple(command) not in self.outputs and command[0] in self.handlers:
            return self.handlers[command[0]](command)
        return self.outputs.get(tuple(command), completed("", 1, "no canned output"))

    def converse(self, command: list[str], dialogue, timeout: float = 60.0) -> int:
        self._check(command)
        raise AssertionError("no test of the whole window changes a password")

    def start(self, command: list[str], settle: float = commands.START_SETTLE) -> None:
        self._check(command)
        with self.lock:
            self.started.append(list(command))
        error = self.start_errors.get(tuple(command))
        if error is not None:
            raise error

    def stream(self, command: list[str], on_line, handle: commands.StreamHandle,
               timeout: float = commands.STREAM_TIMEOUT) -> int:
        """Play the scripted output of a long operation.

        A script is (lines, exit code) or a callable(handle, emit) -> exit code
        that can wait for handle.cancel_requested. Without a script the program
        prints nothing and fails (exit 1): no result, never a success.
        """
        self._check(command)
        commands._check(command)
        assert timeout and timeout > 0
        with self.lock:
            self.streamed.append(list(command))
        script = self.streams.get(tuple(command))
        try:
            if callable(script):
                return script(handle, on_line)
            lines, code = script if script is not None else ([], 1)
            for line in lines:
                on_line(line)
            return code
        finally:
            handle.done()

    def privileged(self) -> list[list[str]]:
        """Every command that went to the privileged helper or the Android runtime manager."""
        with self.lock:
            return [c for c in self.streamed + self.ran + self.started if c in self.streamed or c[0] == E["pkexec"]]

    def changes(self) -> list[list[str]]:
        """Run commands that change state (network, Flatpak, Android apps), as opposed to reads."""
        with self.lock:
            return [c for c in self.ran if c[:2] in ([E["nmcli"], "radio"], [E["nmcli"], "device"]) or
                    (c[0] == E["nmcli"] and any(w in commands.NMCLI_CHANGE_WORDS for w in c[1:])) or
                    (c[0] == E["flatpak"] and c[1] in ("install", "uninstall", "update", "remote-add",
                                                       "permission-remove")) or
                    (c[0] == E["boswas-android"] and c[2:4] == ["app", "launch"]) or
                    c[0] in (E["hostnamectl"], E["lp"], E["fc-cache"]) or
                    (c[0] == E["systemctl"] and c[1] == "reboot")]

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
    write(root / "usr/share/boswas/compositor/version", "1.0~alpha3\n")
    # The device: its name, kind and model; a camera (with its metadata node), CUPS, APT's history, network counters.
    write(root / "etc/hostname", "alice-laptop\n")
    dmi = root / "sys/class/dmi/id"
    write(dmi / "chassis_type", "10\n")
    write(dmi / "sys_vendor", "Example Computers\n")
    write(dmi / "product_name", "Example Book 14\n")
    write(dmi / "product_serial", "SECRET-PRODUCT-SERIAL\n")
    usb_camera = root / "sys/devices/pci0000:00/0000:00:14.0/usb3/3-6/3-6:1.0"
    usb_camera.mkdir(parents=True, exist_ok=True)
    for node, name, index in (("video0", "Integrated Camera: Integrated C", "0"), ("video1", "Integrated Camera: "
                                                                                             "Integrated C", "1")):
        write(root / "sys/class/video4linux" / node / "name", name + "\n")
        write(root / "sys/class/video4linux" / node / "index", index + "\n")
        os.symlink(usb_camera, root / "sys/class/video4linux" / node / "device")
    write(root / "usr/sbin/cupsd", "")
    write(root / "var/log/apt/history.log", HISTORY_LOG)
    write(root / "var/log/apt/history.log.1.gz", gzip.compress(HISTORY_LOG_OLD.encode()))
    for device, received, sent, mac in (("wlp2s0", 1536000000, 204800000, "a0:b1:c2:d3:e4:f5"),
                                        ("enp3s0", 0, 0, "00:11:22:33:44:55")):
        write(root / "sys/class/net" / device / "statistics/rx_bytes", f"{received}\n")
        write(root / "sys/class/net" / device / "statistics/tx_bytes", f"{sent}\n")
        write(root / "sys/class/net" / device / "address", mac + "\n")
    write(root / "sys/class/net/enp3s0/speed", "1000\n")
    # sysfs: an Ethernet controller without a driver, a GPU with one, a USB keyboard (serial numbers present but
    # never read), the ids databases and /proc/stat.
    pci = root / "sys/bus/pci/devices"
    for address, vendor, device, klass, driver in (("0000:03:00.0", "0x10ec", "0x8125", "0x020000", None),
                                                   ("0000:00:02.0", "0x8086", "0x46a6", "0x030000", "i915"),
                                                   ("0000:00:1f.3", "0x8086", "0x51c8", "0x040380", "snd_hda_intel")):
        write(pci / address / "vendor", vendor + "\n")
        write(pci / address / "device", device + "\n")
        write(pci / address / "class", klass + "\n")
        if driver:
            target = root / "sys/bus/pci/drivers" / driver
            target.mkdir(parents=True, exist_ok=True)
            os.symlink(target, pci / address / "driver")
    usb = root / "sys/bus/usb/devices"
    write(usb / "1-2/idVendor", "046d\n")
    write(usb / "1-2/idProduct", "c31c\n")
    write(usb / "1-2/bDeviceClass", "00\n")
    write(usb / "1-2/product", "USB Keyboard\n")
    write(usb / "1-2/manufacturer", "Logitech\n")
    write(usb / "1-2/serial", "SECRET-USB-SERIAL\n")
    (root / "sys/bus/usb/drivers/usbhid").mkdir(parents=True, exist_ok=True)
    (usb / "1-2/1-2:1.0").mkdir(parents=True)
    os.symlink(root / "sys/bus/usb/drivers/usbhid", usb / "1-2/1-2:1.0/driver")
    write(usb / "usb1/idVendor", "1d6b\n")
    write(root / "usr/share/misc/pci.ids", "# pci.ids\n10ec  Realtek Semiconductor Co., Ltd.\n"
          "\t8125  RTL8125 2.5GbE Controller\n\t\t10ec 8125  subsystem line\n8086  Intel Corporation\n"
          "\t46a6  Alder Lake-P GT2 [Iris Xe Graphics]\nC 02  Network controller\n\t00  Ethernet controller\n")
    write(root / "proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0 0 0\n")
    write(root / "proc/meminfo", MEMINFO + "MemAvailable:    8151760 kB\n")
    write(root / "usr/share/boswas/sysops/flatpak-remotes/flathub.flatpakrepo", "[Flatpak Repo]\nTitle=Flathub\n")
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
    write(system / "not-boswas.desktop", DESKTOP + "Name=Not On Boswas\nExec=thing\nNotShowIn=Boswas;\n")
    write(system / "boswas-only.desktop", DESKTOP + "Name=Boswas Only\nExec=boswas-thing\nOnlyShowIn=Boswas;\n")
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
    # The home folder's cache, Trash and Downloads (old and new), and a font the user installed.
    home = base / "home/alice"
    write(home / ".cache/mozilla/firefox/cache2/entry1", b"x" * 300000)
    write(home / ".cache/mozilla/firefox/cache2/entry2", b"x" * 200000)
    write(home / ".cache/pip/http/wheel", b"x" * 120000)
    write(home / ".local/share/Trash/files/old report.odt", b"x" * 50000)
    write(home / ".local/share/Trash/info/old report.odt.trashinfo",
          "[Trash Info]\nPath=/home/alice/Documents/old%20report.odt\nDeletionDate=2026-11-01T10:00:00\n")
    old = write(home / "Downloads/installer-2025.iso", b"x" * 700000)
    os.utime(old, (STAMP_TIME - 90 * 86400, STAMP_TIME - 90 * 86400))
    write(home / "Downloads/today.pdf", b"x" * 1000)
    write(home / ".local/share/fonts/HandwritingDemo.ttf", b"\x00\x01\x00\x00" + b"\x00" * 200)
    return {"HOME": str(base / "home/alice"), "XDG_DATA_DIRS": f"{base / 'usr/share'}", "LANG": "en_US.UTF-8",
            "PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(runtime)}


def add_usb_stick(root: Path, mounted: str | None = "/media/alice/USB STICK", encrypted: bool = False) -> None:
    """A USB stick on port 1-4: /dev/sdb with one partition sdb1 (or an open LUKS volume dm-0 on it), mounted at
    ``mounted`` (None: not mounted), laid out as the kernel's /sys and /proc/self/mountinfo show it."""
    root = Path(root)
    device = root / "sys/devices/pci0000:00/0000:00:14.0/usb1/1-4"
    write(device / "idVendor", "0781\n")
    write(device / "idProduct", "5583\n")
    write(device / "bDeviceClass", "00\n")
    write(device / "product", "Ultra Fit\n")
    write(device / "manufacturer", "SanDisk\n")
    write(device / "serial", "SECRET-STICK-SERIAL\n")
    (root / "sys/bus/usb/drivers/usb-storage").mkdir(parents=True, exist_ok=True)
    (device / "1-4:1.0").mkdir(parents=True, exist_ok=True)
    os.symlink(root / "sys/bus/usb/drivers/usb-storage", device / "1-4:1.0/driver")
    os.symlink(device, root / "sys/bus/usb/devices/1-4")
    scsi = device / "1-4:1.0/host6/target6:0:0/6:0:0:0"
    write(scsi / "vendor", "SanDisk \n")
    write(scsi / "model", "Ultra Fit       \n")
    disk = scsi / "block/sdb"
    write(disk / "dev", "8:16\n")
    write(disk / "size", "61341696\n")
    write(disk / "removable", "1\n")
    os.symlink(scsi, disk / "device")
    write(disk / "sdb1/dev", "8:17\n")
    write(disk / "sdb1/partition", "1\n")
    (disk / "holders").mkdir(exist_ok=True)
    (disk / "sdb1/holders").mkdir(exist_ok=True)
    (root / "sys/block").mkdir(parents=True, exist_ok=True)
    os.symlink(disk, root / "sys/block/sdb")
    lines = ["22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/mapper/boswas--vg-root rw"]
    if encrypted:
        (disk / "sdb1/holders/dm-0").mkdir()
        write(root / "sys/block/dm-0/dev", "254:0\n")
    if mounted is not None:
        point = mounted.replace(" ", "\\040")
        lines.append(f"85 22 {'254:0' if encrypted else '8:17'} / {point} rw,nosuid,nodev shared:45 - vfat "
                     f"/dev/{'mapper/luks-1' if encrypted else 'sdb1'} rw")
    write(root / "proc/self/mountinfo", "\n".join(lines) + "\n")


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


def use_fake_bridge():
    """Make every window of the tests use a FakeBridge: no test ever starts the real services bridge."""
    from boswas_control_center.ui import bridgeclient
    bridgeclient.factory = bridgeclient.FakeBridge
    return bridgeclient


def use_rig_for_settings(rig):
    """Point the settings sections at a Rig: its environment, its recording runner and its root.

    Returns a function that puts the real ones back (for addCleanup).
    """
    from boswas_control_center.ui.settings import context
    saved = (context.environ, context.runner, context.root)
    context.environ = lambda: rig.environ
    context.runner = lambda: rig.commands
    context.root = str(rig.root)

    def restore():
        context.environ, context.runner, context.root = saved
    return restore
