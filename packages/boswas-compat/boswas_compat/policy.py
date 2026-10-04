"""WinCompat policy (/etc/boswas/compat/policy.conf).

Missing keys take restrictive built-in defaults and invalid values fall back
to the most restrictive setting, so a damaged or missing policy file can only
make the platform stricter, never more permissive.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import envfile, paths
from .errors import Refused
from .manifest import FOLDERS, STATUSES, WINETRICKS_RE

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
    source: str | None = None
    problems: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path | None = None) -> "Policy":
        path = path or paths.system_path(paths.POLICY_FILE)
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
        return cls(
            allowed_statuses=statuses,
            unlisted_apps=unlisted == "allow",
            unlisted_network=yes_no("UNLISTED_NETWORK", False, False),
            unlisted_devices=word_set("UNLISTED_DEVICES", DEVICES, defaults.unlisted_devices),
            unlisted_folders=tuple(f for f in FOLDERS if f in folders),
            require_apparmor=yes_no("REQUIRE_APPARMOR", True, True),
            max_installer_bytes=max_bytes,
            winetricks_allowed=frozenset(v for v in verbs if v not in bad_verbs),
            source=str(path),
            problems=tuple(problems),
        )

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
            "allowed_statuses": sorted(self.allowed_statuses),
            "unlisted_apps": "allow" if self.unlisted_apps else "deny",
            "unlisted_sandbox": self.unlisted_grants().to_dict(),
            "require_apparmor": self.require_apparmor,
            "max_installer_mb": self.max_installer_bytes // (1024 * 1024),
            "winetricks_allowed": sorted(self.winetricks_allowed),
            "problems": list(self.problems),
        }


def manifest_grants(manifest) -> Grants:
    sb = manifest.sandbox
    return Grants(network=sb.network, display=sb.display, audio=sb.audio, gpu=sb.gpu, folders=sb.folders)
