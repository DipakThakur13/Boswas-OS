"""Device configuration: /etc/boswas/device.conf.

The file belongs to the device administrator (a dpkg conffile shipped by
boswas-os). The agent reads it and never writes it; the agent's own state
lives in /var/lib/boswas/agent. It holds settings and references only, never
secrets: secret-looking keys or values, private key material and shell
syntax are rejected.

Format: KEY="value" lines, parsed without evaluation.

A configuration with errors is not used for anything that leaves the device:
the agent then stays in ERROR (local applications keep working; nothing
contacts a Control Plane) until `boswas-device config validate` passes.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .storage import read_bytes

MAX_CONF_BYTES = 64 * 1024

_SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|CREDENTIAL)", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(r"-----BEGIN |PRIVATE KEY", re.IGNORECASE)
_FORBIDDEN_CHARS = set("\n\r\"\\`$;|&<>")
_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+-]{0,63}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^[0-9A-Fa-f:.]{2,45}$")   # name, IPv4 or IPv6 literal
CA_DIRS = ("/etc/boswas/", "/usr/share/boswas/", "/etc/ssl/certs/", "/usr/local/share/ca-certificates/")
CERT_DIRS = ("/var/lib/boswas/agent/", "/etc/boswas/")


@dataclass(frozen=True)
class Setting:
    key: str
    default: str
    kind: str                      # bool | int | enum | url | ca | cert | uuid | profile | label | version
    choices: tuple[str, ...] = ()
    low: int = 0
    high: int = 0
    legacy: bool = False           # kept from the Milestone 1 contract


SETTINGS = (
    Setting("AGENT_ENABLED", "yes", "bool"),
    Setting("DEVICE_PROFILE", "", "profile"),
    Setting("DEVICE_NAME", "", "label"),
    Setting("CONTROL_PLANE_URL", "", "url"),
    Setting("CONTROL_PLANE_CA", "", "ca"),
    Setting("REMOTE_COMMANDS", "yes", "bool"),
    Setting("HEARTBEAT_INTERVAL", "300", "int", low=30, high=86400),
    Setting("INVENTORY_POLICY", "standard", "enum", ("off", "minimal", "standard")),
    Setting("INVENTORY_INTERVAL", "3600", "int", low=300, high=604800),
    Setting("TELEMETRY_POLICY", "security", "enum", ("none", "security")),
    Setting("UPDATE_POLICY", "security-only", "enum", ("manual", "security-only", "managed")),
    Setting("LOG_LEVEL", "info", "enum", ("error", "warning", "info", "debug")),
    Setting("DEVICE_ID", "", "uuid", legacy=True),
    Setting("TENANT_ID", "", "label", legacy=True),
    Setting("ENROLLMENT_STATE", "unenrolled", "enum", ("unenrolled", "pending", "enrolled", "retired"), legacy=True),
    Setting("POLICY_VERSION", "", "version", legacy=True),
    Setting("DEVICE_CERTIFICATE", "", "cert", legacy=True),
)
BY_KEY = {s.key: s for s in SETTINGS}


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        try:
            values[key] = " ".join(shlex.split(value, comments=True))
        except ValueError:
            values[key] = "\n"                # unbalanced quotes: rejected below
    return values


def _check(setting: Setting, value: str) -> str | None:
    if value == "":
        return None
    kind = setting.kind
    if kind == "bool" and value not in ("yes", "no"):
        return "must be yes or no"
    if kind == "enum" and value not in setting.choices:
        return f"must be one of {', '.join(setting.choices)}"
    if kind == "int":
        if not value.isdigit() or not setting.low <= int(value) <= setting.high:
            return f"must be a number from {setting.low} to {setting.high}"
    if kind == "uuid" and not _UUID4_RE.fullmatch(value):
        return "must be a random UUID (version 4, lower case)"
    if kind == "profile" and not _PROFILE_RE.fullmatch(value):
        return "must be a short lower-case identifier"
    if kind == "label" and not _LABEL_RE.fullmatch(value):
        return "letters, digits, space, '.', '_' and '-' only (max 64)"
    if kind == "version" and not _VERSION_RE.fullmatch(value):
        return "has invalid characters"
    if kind == "url":
        parts = urlsplit(value)
        if parts.scheme != "https":
            return "must use https://"
        if parts.username or parts.password or "@" in parts.netloc:
            return "must not contain credentials"
        if not parts.hostname or not _HOST_RE.fullmatch(parts.hostname):
            return "must name a host"
        if parts.query or parts.fragment:
            return "must not have a query or fragment"
        try:
            parts.port
        except ValueError:
            return "has an invalid port"
    if kind in ("ca", "cert"):
        p = os.path.normpath(value)
        dirs = CA_DIRS if kind == "ca" else CERT_DIRS
        if not value.startswith("/") or not p.startswith(dirs) or not p.endswith((".crt", ".pem")):
            return f"must be a .crt or .pem file under {' or '.join(dirs)}"
    return None


@dataclass
class AgentConfig:
    values: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    path: str | None = None

    @classmethod
    def from_text(cls, text: str, path: str | None = None) -> "AgentConfig":
        raw = parse(text)
        cfg = cls(path=path)
        for key in sorted(raw):
            if _SECRET_KEY_RE.search(key):
                cfg.problems.append(f"{key}: secret-looking key; device.conf must never contain secrets")
            elif key not in BY_KEY:
                cfg.warnings.append(f"{key}: unknown key (ignored)")
        for setting in SETTINGS:
            value = raw.get(setting.key, setting.default)
            if _SECRET_VALUE_RE.search(value) or any(c in _FORBIDDEN_CHARS for c in value):
                cfg.problems.append(f"{setting.key}: contains characters or content not allowed in device.conf")
                value = setting.default
            else:
                why = _check(setting, value)
                if why:
                    cfg.problems.append(f"{setting.key}: {why}")
                    value = setting.default
            cfg.values[setting.key] = value
        if cfg.values["CONTROL_PLANE_URL"] and not cfg.values["CONTROL_PLANE_CA"]:
            cfg.problems.append("CONTROL_PLANE_CA: required with CONTROL_PLANE_URL (the CA that signs the "
                                "Control Plane's server certificate)")
        return cfg

    @classmethod
    def load(cls, path: Path, *, check_owner: int | None = None) -> "AgentConfig":
        """Read device.conf. With check_owner, the file must belong to that uid
        and must not be writable by group or others."""
        path = Path(path)
        if not os.path.lexists(path):
            cfg = cls.from_text("", str(path))
            cfg.warnings.append(f"{path} does not exist: built-in defaults apply (standalone device)")
            return cfg
        data = read_bytes(path, MAX_CONF_BYTES)
        if data is None:
            cfg = cls.from_text("", str(path))
            cfg.problems.append(f"{path}: not a readable regular file (symbolic links are not accepted)")
            return cfg
        cfg = cls.from_text(data.decode("utf-8", errors="replace"), str(path))
        if check_owner is not None:
            st = path.lstat()
            if st.st_uid != check_owner or st.st_mode & 0o022 or not stat.S_ISREG(st.st_mode):
                cfg.problems.append(f"{path}: insecure ownership or permissions (must be owned by root and not "
                                    "writable by group or others)")
        return cfg

    # --- typed accessors ---------------------------------------------------------
    @property
    def valid(self) -> bool:
        return not self.problems

    def flag(self, key: str) -> bool:
        return self.values[key] == "yes"

    def number(self, key: str) -> int:
        return int(self.values[key])

    @property
    def agent_enabled(self) -> bool:
        return self.flag("AGENT_ENABLED")

    @property
    def remote_commands(self) -> bool:
        return self.flag("REMOTE_COMMANDS")

    @property
    def control_plane_url(self) -> str:
        return self.values["CONTROL_PLANE_URL"].rstrip("/")

    @property
    def managed(self) -> bool:
        """A Control Plane is configured (otherwise the device is standalone)."""
        return bool(self.control_plane_url)

    def to_dict(self) -> dict:
        return {"path": self.path, "valid": self.valid, "settings": dict(self.values),
                "problems": list(self.problems), "warnings": list(self.warnings)}


def template() -> str:
    """Documentation of every setting (used by `boswas-device config show --defaults`)."""
    return "\n".join(f'{s.key}="{s.default}"' for s in SETTINGS if not s.legacy) + "\n"
