"""Device identity and /etc/boswas/device.conf.

The device ID is a random UUID (version 4, from the kernel CSPRNG), created
once per installation. It is never derived from the hostname, a MAC address,
a user name or any hardware serial number; hardware identifiers are
inventory attributes only. After enrollment the device authenticates with
its certificate (DEVICE_CERTIFICATE), never with the ID alone.

device.conf holds identifiers and references only, never secrets: the
writer refuses secret-looking keys and values.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import tempfile
import uuid
from dataclasses import dataclass, fields
from pathlib import Path

from .errors import DeviceConfigError
from .models import EnrollmentState

DEVICE_CONF = Path("/etc/boswas/device.conf")
# Public certificate locations a device.conf may reference.
CERTIFICATE_DIRS = ("/var/lib/boswas/agent/", "/etc/boswas/")

_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+-]{0,63}$")
_SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|KEY)", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(r"-----BEGIN |PRIVATE KEY", re.IGNORECASE)

HEADER = """\
# Boswas device identity and enrollment state (/etc/boswas/device.conf).
#
# Managed by the Boswas device agent. Identifiers and references only:
# this file must NEVER contain secrets. The device private key lives in the
# agent's credential store (root only; TPM-backed later), enrollment tokens
# are used once and never stored.
"""


def generate_device_id() -> str:
    return str(uuid.UUID(bytes=secrets.token_bytes(16), version=4))


def is_valid_device_id(value: str | None) -> bool:
    return bool(value) and bool(_UUID4_RE.fullmatch(value))


def _parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            values[key.strip()] = " ".join(shlex.split(value, comments=True))
        except ValueError:
            continue
    return values


@dataclass
class DeviceConfig:
    device_id: str = ""
    tenant_id: str = ""
    control_plane_url: str = ""
    enrollment_state: str = EnrollmentState.UNENROLLED.value
    policy_version: str = ""
    device_profile: str = ""
    device_certificate: str = ""

    KEYS = ("DEVICE_ID", "TENANT_ID", "CONTROL_PLANE_URL", "ENROLLMENT_STATE", "POLICY_VERSION",
            "DEVICE_PROFILE", "DEVICE_CERTIFICATE")

    @classmethod
    def load(cls, path: Path = DEVICE_CONF) -> "DeviceConfig":
        try:
            values = _parse(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            values = {}
        cfg = cls(**{f.name: values.get(f.name.upper(), getattr(cls, f.name)) for f in fields(cls)})
        if not cfg.enrollment_state:
            cfg.enrollment_state = EnrollmentState.UNENROLLED.value
        return cfg

    def problems(self) -> list[str]:
        problems = []
        if self.device_id and not is_valid_device_id(self.device_id):
            problems.append("DEVICE_ID must be a random UUID (version 4, lower case)")
        if self.enrollment_state not in {s.value for s in EnrollmentState}:
            problems.append(f"ENROLLMENT_STATE must be one of {', '.join(s.value for s in EnrollmentState)}")
        if self.control_plane_url and not self.control_plane_url.startswith("https://"):
            problems.append("CONTROL_PLANE_URL must use https://")
        if self.device_profile and not _PROFILE_RE.fullmatch(self.device_profile):
            problems.append("DEVICE_PROFILE must be a short lower-case identifier")
        if self.policy_version and not _VERSION_RE.fullmatch(self.policy_version):
            problems.append("POLICY_VERSION has invalid characters")
        if self.device_certificate:
            p = os.path.normpath(self.device_certificate)
            if not p.startswith(CERTIFICATE_DIRS) or not p.endswith((".crt", ".pem")):
                problems.append("DEVICE_CERTIFICATE must reference a public certificate (.crt/.pem) "
                                f"under {' or '.join(CERTIFICATE_DIRS)}")
        if self.enrollment_state == EnrollmentState.ENROLLED.value and not (
                self.device_id and self.control_plane_url and self.device_certificate):
            problems.append("an enrolled device needs DEVICE_ID, CONTROL_PLANE_URL and DEVICE_CERTIFICATE")
        for f in fields(self):
            value = getattr(self, f.name)
            if _SECRET_VALUE_RE.search(value) or any(c in value for c in "\n\r\"\\`$"):
                problems.append(f"{f.name.upper()} contains characters or content not allowed in device.conf")
        return problems

    def render(self) -> str:
        lines = [HEADER]
        for f in fields(self):
            lines.append(f'{f.name.upper()}="{getattr(self, f.name)}"')
        return "\n".join(lines) + "\n"

    def write(self, path: Path = DEVICE_CONF) -> None:
        problems = self.problems()
        if problems:
            raise DeviceConfigError("; ".join(problems))
        for key in self.KEYS:
            if _SECRET_KEY_RE.search(key):   # guards future additions to KEYS
                raise DeviceConfigError(f"refusing secret-looking key {key}")
        fd, tmp = tempfile.mkstemp(prefix=".device.conf.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.render())
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


def ensure_device_id(path: Path = DEVICE_CONF) -> DeviceConfig:
    """Create the device ID on first use (agent start, Milestone 2); keep it afterwards."""
    cfg = DeviceConfig.load(path)
    if not cfg.device_id:
        cfg.device_id = generate_device_id()
        cfg.write(path)
    elif not is_valid_device_id(cfg.device_id):
        raise DeviceConfigError("existing DEVICE_ID is not a valid device ID; refusing to replace it silently")
    return cfg
