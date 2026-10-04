"""Filesystem contract of boswas-compat (docs/compatibility/README.md).

System paths go through ``system_path`` so unit tests can point them at a fake
root with BOSWAS_SYSROOT, like the boswas CLI.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

# Where an application's state appears *inside* its sandbox. On the host it
# lives in the user's home (USER_DATA_SUBDIR): Wine requires the prefix to be
# owned by the user who runs it, and per-user storage needs no privileged
# helper. The AppArmor profile only grants this sandbox view.
VIEW_ROOT = "/var/lib/boswas/wine"

RUNNER = "/usr/lib/boswas/compat/winapp-exec"
APPARMOR_PROFILE = "boswas-winapp"
BWRAP = "/usr/bin/bwrap"

POLICY_FILE = "/etc/boswas/compat/policy.conf"
# Written only by the Boswas device agent from a verified, signed Control
# Plane policy. When present it replaces POLICY_FILE (the device is managed).
MANAGED_POLICY_FILE = "/var/lib/boswas/compat/policy.conf"
RUNTIME_FILE = "/usr/share/boswas/compat/runtime.conf"
INSTALLERS_FILE = "/usr/share/boswas/compat/installers.json"
PREFIX_DEFAULTS = "/usr/share/boswas/compat/prefix-defaults.reg"

# Catalog layers, highest precedence first. "managed" is reserved for
# manifests synchronised from the Boswas Control Plane (Milestone 2+).
CATALOG_LAYERS = (
    ("managed", "/var/lib/boswas/compat/manifests"),
    ("local", "/etc/boswas/compat/manifests"),
    ("system", "/usr/share/boswas/compat/manifests"),
)

# Per-user locations, relative to the home directory from the passwd entry.
USER_DATA_SUBDIR = ".local/share/boswas/wine"
DESKTOP_SUBDIR = ".local/share/applications"


def sysroot() -> Path:
    return Path(os.environ.get("BOSWAS_SYSROOT", "/"))


def system_path(path: str) -> Path:
    return sysroot() / path.lstrip("/")


def user_home(uid: int | None = None) -> Path:
    """Home directory of the user (passwd entry, not $HOME).

    BOSWAS_WINAPP_HOME overrides it for unit tests only.
    """
    override = os.environ.get("BOSWAS_WINAPP_HOME")
    if override:
        return Path(override)
    return Path(pwd.getpwuid(os.getuid() if uid is None else uid).pw_dir)


def view_dir(app_id: str) -> str:
    return f"{VIEW_ROOT}/{app_id}"


def test_root_active() -> bool:
    """True when a unit test points the system paths at a fake root.

    The installed commands remove BOSWAS_SYSROOT and BOSWAS_WINAPP_HOME from
    their environment before importing this package, so on a device this is
    always False.
    """
    return "BOSWAS_SYSROOT" in os.environ
