"""Boswas policy: excluded and protected packages, and /etc/boswas/apps.conf.

EXCLUDED: never installed through boswas-sysops. The same list the image
build pins to -1 (config/live-build/config/archives/boswas-exclude.pref.chroot);
those pins exist only while the image is built, so the helper enforces the
decision on installed devices.

PROTECTED: never removed through boswas-sysops, directly or as a side effect
of another change (the Boswas components, the kernel and boot chain, the
desktop core, the security baseline, the package manager), plus every
package dpkg marks Essential or Priority required.
"""

from __future__ import annotations

import fnmatch

from . import conffile
from .protocol import SysopsError

APPS_CONF = "/etc/boswas/apps.conf"

EXCLUDED = frozenset({
    "kdeconnect",
    "plasma-discover", "plasma-discover-common", "plasma-discover-backend-fwupd",
    "plasma-discover-backend-flatpak", "packagekit", "packagekit-tools",
    "libpam-fprintd", "fprintd",
    "plasma-welcome",
    "openssh-server",
    "cups-browsed",
})

PROTECTED_EXACT = frozenset({
    "systemd", "systemd-sysv", "sddm", "plasma-workspace", "kwin-wayland", "network-manager",
    "apparmor", "nftables", "auditd", "lvm2", "sudo", "polkitd", "pkexec", "dbus", "apt", "dpkg",
})
PROTECTED_PATTERNS = ("boswas-*", "linux-image-*", "grub-*", "shim-*", "cryptsetup*")

DEFAULTS = {"LOCAL_DEB": "allow", "FLATPAK_REMOTES": "flathub", "ANDROID_RUNTIME": "allow"}


def excluded(package: str) -> bool:
    return package.split(":", 1)[0] in EXCLUDED


def protected_by_name(package: str) -> bool:
    name = package.split(":", 1)[0]
    return name in PROTECTED_EXACT or any(fnmatch.fnmatchcase(name, p) for p in PROTECTED_PATTERNS)


def apps_conf(root) -> dict[str, str]:
    values = dict(DEFAULTS)
    values.update(conffile.parse(root.read(APPS_CONF, 65536)))
    return values


def require_allowed(root, key: str, what: str) -> None:
    """allow / deny switches; any other value counts as deny (fail closed)."""
    value = apps_conf(root).get(key, "").strip().lower()
    if value != "allow":
        raise SysopsError("NOT_ALLOWED", f"{what} is turned off by the device policy ({key} in {APPS_CONF}).")


def flatpak_remotes(root) -> list[str]:
    return apps_conf(root).get("FLATPAK_REMOTES", "").split()
