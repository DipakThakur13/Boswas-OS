"""OS identity, image build metadata and device configuration."""

from __future__ import annotations

import json
import os
import platform
import re
import socket

from . import system

RELEASE_FILE = "/usr/lib/boswas/release"
IMAGE_INFO_FILE = "/usr/lib/boswas/image-info"
DEVICE_CONF = "/etc/boswas/device.conf"
UPDATE_CONF = "/etc/boswas/update.conf"
# Public files of the device agent (Milestone 2): it owns the device identity
# and the enrollment state; device.conf holds configuration.
AGENT_IDENTITY = "/var/lib/boswas/agent/identity.json"
AGENT_STATUS = "/var/lib/boswas/agent/status.json"
_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

# Only these device.conf keys are ever displayed. Anything else (for example a
# future credential reference) is ignored so it can never leak via the CLI.
DEVICE_KEYS = ("DEVICE_ID", "TENANT_ID", "CONTROL_PLANE_URL", "ENROLLMENT_STATE", "POLICY_VERSION",
               "DEVICE_PROFILE", "DEVICE_CERTIFICATE")


def architecture() -> str:
    code, out = system.run(["dpkg", "--print-architecture"])
    if code == 0 and out:
        return out
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())


def os_identity() -> dict:
    release = system.load_env(RELEASE_FILE)
    image = system.load_env(IMAGE_INFO_FILE)
    os_release = system.load_env("/etc/os-release")
    update = system.load_env(UPDATE_CONF)
    debian_version = system.read_text("/etc/debian_version")
    plasma = system.upstream_version(system.package_version("plasma-workspace"))
    return {
        "name": release.get("BOSWAS_NAME", "Boswas OS"),
        "version": release.get("BOSWAS_VERSION", "unknown"),
        "version_id": release.get("BOSWAS_VERSION_ID", "unknown"),
        "channel": update.get("CHANNEL") or release.get("BOSWAS_CHANNEL", "unknown"),
        "build": {
            "id": image.get("BOSWAS_BUILD_ID"),
            "date": image.get("BOSWAS_BUILD_DATE"),
            "git_commit": image.get("BOSWAS_GIT_COMMIT"),
            "live_build": image.get("BOSWAS_LIVE_BUILD_VERSION"),
        },
        "base": {
            "name": os_release.get("NAME", "Debian GNU/Linux"),
            "version": debian_version or os_release.get("VERSION_ID", "unknown"),
            "codename": os_release.get("VERSION_CODENAME", release.get("BOSWAS_BASE_CODENAME", "unknown")),
        },
        "architecture": architecture(),
        "kernel": os.uname().release,
        "desktop": f"KDE Plasma {plasma}" if plasma else "not installed",
        "hostname": socket.gethostname(),
        "boot_mode": system.boot_mode(),
        "live_session": system.is_live_session(),
    }


def _agent_file(path: str) -> dict:
    text = system.read_text(path)
    try:
        doc = json.loads(text) if text else {}
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def device_config() -> dict:
    conf = system.load_env(DEVICE_CONF)
    device = {key.lower(): (conf.get(key) or None) for key in DEVICE_KEYS}
    ident, status = _agent_file(AGENT_IDENTITY), _agent_file(AGENT_STATUS)
    if isinstance(ident.get("device_id"), str) and _UUID4_RE.fullmatch(ident["device_id"]):
        device["device_id"] = ident["device_id"]
    if isinstance(status.get("enrollment"), str):
        device["enrollment_state"] = status["enrollment"]
    if isinstance(status.get("control_plane"), str):
        device["control_plane_url"] = status["control_plane"]
    policy = status.get("policy") if isinstance(status.get("policy"), dict) else {}
    if isinstance(policy.get("version"), str):
        device["policy_version"] = policy["version"]
    return device


def agent_status() -> dict | None:
    """The device agent's last published state (None if it never ran)."""
    status = _agent_file(AGENT_STATUS)
    if not status:
        return None
    keys = ("state", "connection", "agent_version", "last_contact", "updated_at", "maintenance")
    return {k: status.get(k) for k in keys}


def _dmi(name: str) -> str | None:
    value = system.read_text(f"/sys/class/dmi/id/{name}")
    return value or None


def _cpu_model() -> str | None:
    for line in (system.read_text("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def _memory_gib() -> float | None:
    for line in (system.read_text("/proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            return round(int(line.split()[1]) / 1024 / 1024, 1)
    return None


def hardware_summary() -> dict:
    """Non-sensitive hardware facts readable without root (no serial numbers)."""
    return {
        "vendor": _dmi("sys_vendor"),
        "product": _dmi("product_name"),
        "firmware_version": _dmi("bios_version"),
        "cpu": _cpu_model(),
        "memory_gib": _memory_gib(),
        "boot_mode": system.boot_mode(),
        "tpm_version": system.read_text("/sys/class/tpm/tpm0/tpm_version_major"),
    }
