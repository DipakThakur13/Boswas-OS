"""Inventory collectors.

Everything collected is explicitly allowlisted (privacy.py checks the
document, `boswas-inventory/1`):

  os                     Boswas and Debian versions, kernel, architecture
  hardware (standard)    CPU model and count, memory, vendor/model/firmware
                         version, boot mode, TPM version. No serial numbers,
                         MAC addresses, disk identifiers or machine-id
  storage (standard)     size and free space of the root filesystem
  packages               versions of the Boswas packages and the runtime
                         (Wine, bubblewrap, AppArmor) only
  windows_applications   managed Windows applications aggregated over users:
                         ID, version, status, architecture, state and counts.
                         No user names, no file names, no prefix contents
  compatibility          Wine version, supported architectures, bubblewrap
                         and AppArmor confinement state

Never collected: browser history, user files, documents, passwords, keys,
application data, command lines or anything typed or shown on screen.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from collections import Counter, defaultdict

from .interfaces import InventoryCollector
from .models import ApplicationInventoryItem
from .paths import AgentPaths
from .storage import read_bytes

INVENTORY_SCHEMA = "boswas-inventory/1"
WINAPP = "/usr/bin/boswas-winapp"
WINAPP_COMMAND = (WINAPP, "--json", "list", "--all-users")
TRACKED_PACKAGES = ("boswas-os", "boswas-cli", "boswas-branding", "boswas-security", "boswas-compat",
                    "boswas-device-agent", "boswas-compat-manager", "wine", "wine64", "bubblewrap", "apparmor")
INVENTORY_POLICIES = ("off", "minimal", "standard")


def _text(value, limit: int = 200):
    if not isinstance(value, str):
        return None
    return "".join(c for c in value if c.isprintable())[:limit] or None


class WindowsApplicationsCollector(InventoryCollector):
    """Windows applications of every user, from `boswas-winapp --json list --all-users` (as root)."""

    name = "windows-applications"

    def __init__(self, run=subprocess.run, command=WINAPP_COMMAND):
        self._run = run
        self._command = list(command)

    def _users(self) -> tuple[list | None, str | None]:
        try:
            proc = self._run(self._command, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, type(exc).__name__
        if proc.returncode != 0:
            return None, f"exit {proc.returncode}"
        try:
            doc = json.loads(proc.stdout)
            users = doc["users"]
        except (ValueError, KeyError, TypeError):
            return None, "unreadable output"
        return (users if isinstance(users, list) else []), None

    def collect(self) -> dict:
        users, error = self._users()
        if users is None:
            return {"available": False, "error": error, "applications": []}
        counts: Counter = Counter()
        for user in users:
            apps = user.get("applications", []) if isinstance(user, dict) else []
            for app in apps if isinstance(apps, list) else []:
                if isinstance(app, dict) and app.get("state") == "installed" and isinstance(app.get("id"), str):
                    counts[(app["id"], _text(app.get("version")), _text(app.get("status")))] += 1
        items = [ApplicationInventoryItem(kind="winapp", id=i, version=v, status=s, installations=n)
                 for (i, v, s), n in sorted(counts.items(), key=lambda kv: kv[0][0])]
        return {"available": True, "applications": [item.to_dict() for item in items]}

    def collect_detailed(self) -> dict:
        """Per application and version: counts, status, architecture, aggregate state."""
        users, error = self._users()
        if users is None:
            return {"available": False, "error": error, "applications": []}
        groups: dict = defaultdict(lambda: {"installations": 0, "running": 0, "failed": 0, "status": None,
                                            "architecture": None, "compatibility": None, "allowed": True})
        for user in users:
            if not isinstance(user, dict):
                continue
            running = set(r for r in user.get("running", []) if isinstance(r, str)) \
                if isinstance(user.get("running"), list) else set()
            apps = user.get("applications", [])
            for app in apps if isinstance(apps, list) else []:
                if not isinstance(app, dict) or not isinstance(app.get("id"), str):
                    continue
                state = app.get("state")
                if state not in ("installed", "failed"):
                    continue
                g = groups[(app["id"], _text(app.get("version")))]
                g["installations"] += 1
                g["running"] += app["id"] in running
                g["failed"] += state == "failed"
                g["status"] = _text(app.get("status"), 20)
                g["architecture"] = _text(app.get("architecture"), 20)
                g["compatibility"] = _text(app.get("compatibility"), 40)
                g["allowed"] = g["allowed"] and app.get("allowed") is not False
        items = []
        for (app_id, version), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            if g["compatibility"] not in (None, "SUPPORTED"):
                state = "UNSUPPORTED"
            elif g["running"]:
                state = "RUNNING"
            elif not g["allowed"]:
                state = "BLOCKED"
            elif g["failed"]:
                state = "ERROR"
            else:
                state = "INSTALLED"
            items.append({"id": app_id[:96], "version": version, "status": g["status"],
                          "architecture": g["architecture"], "compatibility": g["compatibility"] or "SUPPORTED",
                          "app_state": state, "installations": g["installations"], "running": g["running"]})
        return {"available": True, "applications": items}


def _env_file(path) -> dict[str, str]:
    from .config import parse
    data = read_bytes(path, 65536)
    return parse(data.decode("utf-8", errors="replace")) if data else {}


class SystemInventory:
    """The device inventory document (``boswas-inventory/1`` without device ID and revision)."""

    def __init__(self, paths: AgentPaths = AgentPaths(), run=subprocess.run, winapp: WindowsApplicationsCollector | None = None):
        self.paths = paths
        self._run = run
        self.winapp = winapp or WindowsApplicationsCollector(run=run)

    def _read(self, path: str) -> str | None:
        data = read_bytes(self.paths.root / path.lstrip("/"), 1024 * 1024)
        return data.decode("utf-8", errors="replace").strip() if data is not None else None

    def _cmd(self, argv: list[str], timeout: float = 30, ok: tuple[int, ...] = (0,)) -> str | None:
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode in ok else None

    def os_info(self) -> dict:
        release = _env_file(self.paths.release)
        image = _env_file(self.paths.image_info)
        arch = (self._cmd(["dpkg", "--print-architecture"]) or "").strip() or \
            {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
        return {"name": _text(release.get("BOSWAS_NAME")) or "Boswas OS",
                "version": _text(release.get("BOSWAS_VERSION")) or "unknown",
                "version_id": _text(release.get("BOSWAS_VERSION_ID")) or "unknown",
                "build_id": _text(image.get("BOSWAS_BUILD_ID")),
                "debian_version": _text(self._read("/etc/debian_version")),
                "kernel": _text(os.uname().release) or "unknown",
                "architecture": _text(arch, 20) or "unknown"}

    def hardware(self) -> dict:
        cpu = None
        for line in (self._read("/proc/cpuinfo") or "").splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
        memory = None
        for line in (self._read("/proc/meminfo") or "").splitlines():
            if line.startswith("MemTotal:"):
                memory = round(int(line.split()[1]) / 1024 / 1024, 1)
        return {"cpu_model": _text(cpu), "cpu_count": os.cpu_count() or 0, "memory_gib": memory,
                # Product facts only: never product_serial, board_serial, product_uuid.
                "vendor": _text(self._read("/sys/class/dmi/id/sys_vendor")),
                "model": _text(self._read("/sys/class/dmi/id/product_name")),
                "firmware_version": _text(self._read("/sys/class/dmi/id/bios_version")),
                "boot_mode": "UEFI" if (self.paths.root / "sys/firmware/efi").is_dir() else "BIOS",
                "tpm_version": _text(self._read("/sys/class/tpm/tpm0/tpm_version_major"), 4)}

    def storage(self) -> dict:
        try:
            usage = shutil.disk_usage(self.paths.root)
        except OSError:
            return {"root_total_gib": None, "root_free_gib": None}
        gib = 1024 ** 3
        return {"root_total_gib": round(usage.total / gib, 1), "root_free_gib": round(usage.free / gib, 1)}

    def packages(self) -> list[dict]:
        out = self._cmd(["dpkg-query", "-W", "-f=${Package}\t${db:Status-Status}\t${Version}\n",
                         *TRACKED_PACKAGES]) or ""
        found = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] in TRACKED_PACKAGES and parts[1] == "installed":
                found.append({"name": parts[0], "version": _text(parts[2], 64)})
        return sorted(found, key=lambda p: p["name"])

    def compatibility(self) -> dict:
        # Exit 1 means "unhealthy" and still carries the full report (e.g. the AppArmor profile is not loaded).
        out = self._cmd([WINAPP, "--json", "runtime"], timeout=120, ok=(0, 1))
        try:
            doc = json.loads(out) if out else None
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            return {"available": False, "wine_version": None, "architectures": [], "bubblewrap": None,
                    "apparmor": "unknown", "policy_managed": False, "healthy": False}
        archs = doc.get("architectures") if isinstance(doc.get("architectures"), list) else []
        return {"available": True,
                "wine_version": _text((doc.get("wine") or {}).get("version"), 64),
                "architectures": [a for a in archs if a in ("x86_64", "x86")],
                "bubblewrap": _text((doc.get("bubblewrap") or {}).get("version"), 32),
                "apparmor": _text((doc.get("apparmor") or {}).get("mode"), 20) or "unknown",
                "policy_managed": (doc.get("policy") or {}).get("managed") is True,
                "healthy": doc.get("healthy") is True}

    def collect(self, policy: str = "standard") -> dict:
        if policy not in ("minimal", "standard"):
            raise ValueError(f"inventory policy {policy!r} collects nothing")
        winapps = self.winapp.collect_detailed()
        doc = {"policy": policy, "os": self.os_info(), "packages": self.packages(),
               "windows_applications": winapps["applications"], "windows_inventory_available": winapps["available"],
               "compatibility": self.compatibility()}
        if policy == "standard":
            doc["hardware"] = self.hardware()
            doc["storage"] = self.storage()
        return doc


def content_digest(doc: dict) -> str:
    """Digest of an inventory's content (ignoring when it was collected), to bump the revision on change."""
    stable = {k: v for k, v in doc.items() if k not in ("collected_at", "revision", "schema", "device_id")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
