"""WinCompat policy.

Two sources, never merged:

  /var/lib/boswas/compat/policy.conf   managed: written by the Boswas device
                                       agent from a verified, signed Control
                                       Plane policy (root-owned)
  /etc/boswas/compat/policy.conf       local: shipped default, edited by the
                                       device administrator

When the managed file exists the device is managed and it is used instead
of the local file. A managed file that is not a regular, root-owned file
without group/world write access is ignored in favour of the restrictive
built-in defaults.

Missing keys take restrictive built-in defaults and invalid values fall back
to the most restrictive setting, so a damaged or missing policy file can only
make the platform stricter, never more permissive.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import envfile, paths
from .errors import Refused
from .manifest import FOLDERS, STATUSES, WINETRICKS_RE, valid_id

DEVICES = ("display", "audio", "gpu")


@dataclass(frozen=True)
class Grants:
    """What a sandbox contains besides the application's own state."""

    network: bool = False
    display: bool = False
    audio: bool = False
    gpu: bool = False
    folders: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"network": self.network, "display": self.display, "audio": self.audio,
                "gpu": self.gpu, "folders": list(self.folders)}


@dataclass(frozen=True)
class Policy:
    allowed_statuses: frozenset = frozenset({"approved", "tested"})
    unlisted_apps: bool = False
    unlisted_network: bool = False
    unlisted_devices: frozenset = frozenset({"display"})
    unlisted_folders: tuple[str, ...] = ()
    require_apparmor: bool = True
    max_installer_bytes: int = 4096 * 1024 * 1024
    winetricks_allowed: frozenset = frozenset()
    # Device policy by application ID: a block list, and an optional allow
    # list (None: no allow list; every ID that passes the other rules).
    blocked_applications: frozenset = frozenset()
    allowed_applications: frozenset | None = None
    managed: bool = False
    source: str | None = None
    problems: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path | None = None) -> "Policy":
        managed = False
        if path is None:
            managed_path = paths.system_path(paths.MANAGED_POLICY_FILE)
            if os.path.lexists(managed_path):
                why = _untrusted_managed_file(managed_path)
                if why:
                    return cls(source=str(managed_path), managed=True,
                               problems=(f"{managed_path} ignored ({why}): restrictive defaults apply",))
                path, managed = managed_path, True
            else:
                path = paths.system_path(paths.POLICY_FILE)
        if not path.is_file():
            return cls(source=None, problems=(f"{path} missing: restrictive defaults apply",))
        values = envfile.load(path)
        problems: list[str] = []
        defaults = cls()

        def yes_no(key: str, default: bool, restrictive: bool) -> bool:
            if key not in values:
                return default
            value = values[key].strip().lower()
            if value in ("yes", "no"):
                return value == "yes"
            problems.append(f"{key}={values[key]!r} is not yes/no")
            return restrictive

        def word_set(key: str, allowed, default) -> frozenset:
            if key not in values:
                return frozenset(default)
            words = values[key].split()
            bad = [w for w in words if w not in allowed]
            if bad:
                problems.append(f"{key}: ignoring unknown value(s) {' '.join(bad)}")
            return frozenset(w for w in words if w in allowed)

        statuses = word_set("ALLOWED_STATUSES", STATUSES, defaults.allowed_statuses) - {"blocked"}
        unlisted = values.get("UNLISTED_APPS", "deny").strip().lower()
        if unlisted not in ("allow", "deny"):
            problems.append(f"UNLISTED_APPS={values['UNLISTED_APPS']!r} is not allow/deny")
        max_mb_raw = values.get("MAX_INSTALLER_MB", "4096").strip()
        if max_mb_raw.isdigit():
            max_bytes = int(max_mb_raw) * 1024 * 1024
        else:
            problems.append(f"MAX_INSTALLER_MB={max_mb_raw!r} is not a number")
            max_bytes = 0
        folders = word_set("UNLISTED_FOLDERS", FOLDERS, ())
        verbs = values.get("WINETRICKS_ALLOWED", "").split()
        bad_verbs = [v for v in verbs if not WINETRICKS_RE.fullmatch(v)]
        if bad_verbs:
            problems.append(f"WINETRICKS_ALLOWED: ignoring invalid verb(s) {' '.join(bad_verbs)}")

        def id_set(key: str) -> frozenset:
            # An invalid ID can never match an application: ignoring it in the
            # block list loosens nothing, and in the allow list it narrows.
            # ALLOWED_APPLICATIONS="none" is an allow list that allows nothing.
            ids = values.get(key, "").split()
            if key == "ALLOWED_APPLICATIONS" and ids == ["none"]:
                return frozenset()
            bad = [i for i in ids if not valid_id(i)]
            if bad:
                problems.append(f"{key}: ignoring invalid application ID(s) {' '.join(bad)}")
            return frozenset(i for i in ids if valid_id(i))

        allowed_raw = values.get("ALLOWED_APPLICATIONS", "").strip()
        return cls(
            allowed_statuses=statuses,
            unlisted_apps=unlisted == "allow",
            unlisted_network=yes_no("UNLISTED_NETWORK", False, False),
            unlisted_devices=word_set("UNLISTED_DEVICES", DEVICES, defaults.unlisted_devices),
            unlisted_folders=tuple(f for f in FOLDERS if f in folders),
            require_apparmor=yes_no("REQUIRE_APPARMOR", True, True),
            max_installer_bytes=max_bytes,
            winetricks_allowed=frozenset(v for v in verbs if v not in bad_verbs),
            blocked_applications=id_set("BLOCKED_APPLICATIONS"),
            allowed_applications=id_set("ALLOWED_APPLICATIONS") if allowed_raw else None,
            managed=managed,
            source=str(path),
            problems=tuple(problems),
        )

    def check_application(self, app_id: str) -> None:
        """Device policy for one application ID (block list, allow list)."""
        if app_id in self.blocked_applications:
            raise Refused(f"{app_id} is blocked by device policy", reason="policy-blocked")
        if self.allowed_applications is not None and app_id not in self.allowed_applications:
            raise Refused(f"{app_id} is not in the device policy's list of allowed applications",
                          reason="policy-not-allowed")

    def check_status(self, status: str, app_id: str) -> None:
        if status == "blocked":
            raise Refused(f"{app_id} is blocked by the Boswas compatibility catalog", reason="blocked")
        if status not in self.allowed_statuses:
            raise Refused(f"{app_id} has status '{status}', which policy does not allow "
                          f"(allowed: {', '.join(sorted(self.allowed_statuses)) or 'none'})",
                          reason="status-not-allowed")

    def check_unlisted(self) -> None:
        if not self.unlisted_apps:
            raise Refused("this installer matches no Boswas compatibility manifest, and policy "
                          "does not allow unlisted applications (UNLISTED_APPS=deny)",
                          reason="unlisted-denied")

    def unlisted_grants(self) -> Grants:
        return Grants(
            network=self.unlisted_network,
            display="display" in self.unlisted_devices,
            audio="audio" in self.unlisted_devices,
            gpu="gpu" in self.unlisted_devices,
            folders=self.unlisted_folders,
        )

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "managed": self.managed,
            "allowed_statuses": sorted(self.allowed_statuses),
            "unlisted_apps": "allow" if self.unlisted_apps else "deny",
            "unlisted_sandbox": self.unlisted_grants().to_dict(),
            "require_apparmor": self.require_apparmor,
            "max_installer_mb": self.max_installer_bytes // (1024 * 1024),
            "winetricks_allowed": sorted(self.winetricks_allowed),
            "blocked_applications": sorted(self.blocked_applications),
            "allowed_applications": None if self.allowed_applications is None else sorted(self.allowed_applications),
            "problems": list(self.problems),
        }


def _untrusted_managed_file(path: Path) -> str | None:
    """Why the managed policy file must not be trusted, or None."""
    try:
        st = path.lstat()
    except OSError as exc:
        return exc.strerror or "unreadable"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    owners = {0} | ({os.geteuid()} if paths.test_root_active() else set())
    if st.st_uid not in owners:
        return "not owned by root"
    if st.st_mode & 0o022:
        return "writable by group or others"
    return None


def manifest_grants(manifest) -> Grants:
    sb = manifest.sandbox
    return Grants(network=sb.network, display=sb.display, audio=sb.audio, gpu=sb.gpu, folders=sb.folders)
