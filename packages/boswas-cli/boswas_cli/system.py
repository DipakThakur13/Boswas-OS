"""Read-only helpers for inspecting the local system.

Everything here is safe to run as an unprivileged user: nothing is modified,
commands get a short timeout, and missing tools degrade to "unknown" instead
of raising. All filesystem paths go through ``sysroot_path`` so tests can point
the CLI at a fake root via the BOSWAS_SYSROOT environment variable.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def sysroot() -> Path:
    return Path(os.environ.get("BOSWAS_SYSROOT", "/"))


def sysroot_path(p: str) -> Path:
    return sysroot() / p.lstrip("/")


def read_text(p: str) -> str | None:
    try:
        return sysroot_path(p).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def read_bytes(p: str) -> bytes | None:
    try:
        return sysroot_path(p).read_bytes()
    except OSError:
        return None


def parse_env(text: str) -> dict[str, str]:
    """Parse os-release style KEY=value lines. Never evaluates shell code."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            continue
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            continue
        result[key] = " ".join(parts)
    return result


def load_env(p: str) -> dict[str, str]:
    text = read_text(p)
    return parse_env(text) if text else {}


def run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a command, returning (exit code, stripped stdout).

    127 means the tool is not installed, 124 a timeout, 126 any other failure
    to execute.
    """
    if shutil.which(cmd[0]) is None:
        return 127, ""
    env = dict(os.environ, LC_ALL="C")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 126, ""
    return proc.returncode, proc.stdout.strip()


def systemd_running() -> bool:
    return sysroot_path("/run/systemd/system").is_dir()


def unit_active(unit: str) -> str:
    """'active', 'inactive', 'failed', ... or 'unknown' without a running systemd."""
    if not systemd_running():
        return "unknown"
    _, out = run(["systemctl", "is-active", unit])
    return out or "unknown"


def unit_enabled(unit: str) -> str:
    _, out = run(["systemctl", "is-enabled", unit])
    return out or "unknown"


def package_version(package: str) -> str | None:
    """Installed version of a Debian package, or None if not installed."""
    code, out = run(["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", package])
    if code != 0 or not out.startswith("installed "):
        return None
    return out.split(" ", 1)[1]


def upstream_version(debian_version: str | None) -> str | None:
    """'4:6.3.6-2' -> '6.3.6'."""
    if not debian_version:
        return None
    version = debian_version.split(":", 1)[-1]
    return version.rsplit("-", 1)[0] if "-" in version else version


def is_live_session() -> bool:
    cmdline = read_text("/proc/cmdline") or ""
    return sysroot_path("/run/live").is_dir() or "boot=live" in cmdline.split()


def boot_mode() -> str:
    return "UEFI" if sysroot_path("/sys/firmware/efi").is_dir() else "BIOS"


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0
