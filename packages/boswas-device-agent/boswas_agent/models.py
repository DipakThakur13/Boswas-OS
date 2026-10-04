"""Messages exchanged between the device agent and the Control Plane (API v1).

Every message has a JSON schema in packages/boswas-device-agent/schemas/;
tests keep both in sync. Field lists are deliberately closed: adding data to
what a device reports needs a schema change, a privacy review
(docs/security/privacy.md) and a test update. See privacy.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .errors import UnsupportedCommand


class EnrollmentState(str, Enum):
    """ENROLLMENT_STATE in /etc/boswas/device.conf."""

    UNENROLLED = "unenrolled"
    PENDING = "pending"
    ENROLLED = "enrolled"
    RETIRED = "retired"


class ComplianceState(str, Enum):
    COMPLIANT = "COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    PENDING = "PENDING"        # a policy was received but is not applied/assessed yet
    UNKNOWN = "UNKNOWN"        # no assessment available

    @classmethod
    def from_local_assessment(cls, state: str | None) -> "ComplianceState":
        """Map `boswas status` states onto the Control Plane states.

        COMPLIANT_WITH_WARNINGS is COMPLIANT: warnings are reported per check,
        and only FAIL results make a device non-compliant.
        """
        return {
            "COMPLIANT": cls.COMPLIANT,
            "COMPLIANT_WITH_WARNINGS": cls.COMPLIANT,
            "NON_COMPLIANT": cls.NON_COMPLIANT,
        }.get(state or "", cls.UNKNOWN)


class CommandType(str, Enum):
    """Controlled operations the Control Plane may request.

    Destructive operations (remote wipe, retire) are intentionally absent.
    They will need their own command types, explicit authorization recorded
    in the audit trail, and a separate review before they are added.
    """

    SYNC_POLICY = "SYNC_POLICY"
    CHECK_UPDATE = "CHECK_UPDATE"
    INSTALL_UPDATE = "INSTALL_UPDATE"
    RESTART = "RESTART"
    LOCK_DEVICE = "LOCK_DEVICE"
    REFRESH_CONFIGURATION = "REFRESH_CONFIGURATION"


CHECK_STATUSES = ("PASS", "WARN", "FAIL", "INFO", "UNKNOWN")
# Posture checks a heartbeat may summarise (IDs from `boswas status`).
SECURITY_CHECK_IDS = ("secure-boot", "tpm", "disk-encryption", "firewall", "apparmor", "audit",
                      "ssh-server", "root-account", "apt-trust", "screen-lock", "usb-policy",
                      "winapp-confinement")


@dataclass(frozen=True)
class OsInfo:
    name: str
    version: str
    version_id: str
    build_id: str | None
    debian_version: str | None
    kernel: str

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "version_id": self.version_id,
                "build_id": self.build_id, "debian_version": self.debian_version, "kernel": self.kernel}


@dataclass(frozen=True)
class HardwareFacts:
    """Inventory attributes. Never used to authenticate a device."""

    vendor: str | None
    model: str | None
    firmware_version: str | None
    cpu: str | None
    memory_gib: float | None
    tpm_version: str | None
    boot_mode: str | None

    def to_dict(self) -> dict:
        return {"vendor": self.vendor, "model": self.model, "firmware_version": self.firmware_version,
                "cpu": self.cpu, "memory_gib": self.memory_gib, "tpm_version": self.tpm_version,
                "boot_mode": self.boot_mode}


@dataclass(frozen=True)
class ComplianceSummary:
    state: ComplianceState
    passed: int = 0
    warnings: int = 0
    failed: int = 0
    unknown: int = 0

    def to_dict(self) -> dict:
        return {"state": self.state.value, "pass": self.passed, "warn": self.warnings, "fail": self.failed,
                "unknown": self.unknown}


@dataclass(frozen=True)
class UpdateStatus:
    channel: str
    state: str = "unknown"          # up-to-date | available | staged | applying | failed | unknown
    available_version: str | None = None

    STATES = ("up-to-date", "available", "staged", "applying", "failed", "unknown")

    def to_dict(self) -> dict:
        return {"channel": self.channel, "state": self.state, "available_version": self.available_version}


@dataclass(frozen=True)
class Heartbeat:
    """POST /api/v1/devices/{id}/heartbeat. Status only; never content."""

    device_id: str
    agent_version: str
    os: OsInfo
    uptime_seconds: int
    compliance: ComplianceSummary
    policy_version: str | None
    update: UpdateStatus
    security: dict[str, str] = field(default_factory=dict)   # check ID -> status
    sent_at: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": "boswas-heartbeat/1",
            "device_id": self.device_id,
            "agent_version": self.agent_version,
            "os": self.os.to_dict(),
            "uptime_seconds": self.uptime_seconds,
            "compliance": self.compliance.to_dict(),
            "policy_version": self.policy_version,
            "update": self.update.to_dict(),
            "security": dict(sorted(self.security.items())),
            "sent_at": self.sent_at,
        }


@dataclass(frozen=True)
class EnrollmentRequest:
    """POST /api/v1/devices/enroll.

    The enrollment token is a one-time secret issued by an administrator. It
    is sent once, never stored in /etc/boswas/device.conf, never logged and
    never part of repr().
    """

    device_id: str
    csr_pem: str                     # PKCS#10 request: public key only
    os: OsInfo
    hardware: HardwareFacts
    profile: str | None = None
    enrollment_token: str = field(default="", repr=False)

    def to_dict(self) -> dict:
        return {
            "schema": "boswas-enrollment-request/1",
            "device_id": self.device_id,
            "csr_pem": self.csr_pem,
            "os": self.os.to_dict(),
            "hardware": self.hardware.to_dict(),
            "profile": self.profile,
            "enrollment_token": self.enrollment_token,
        }


@dataclass(frozen=True)
class CheckResult:
    id: str
    status: str
    scored: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "scored": self.scored}


@dataclass(frozen=True)
class ComplianceReport:
    """POST /api/v1/devices/{id}/compliance. Check IDs and results only."""

    device_id: str
    policy_version: str | None
    summary: ComplianceSummary
    checks: tuple[CheckResult, ...]
    assessed_at: str
    basis: str = "local self-assessment (not attested)"

    def to_dict(self) -> dict:
        return {
            "schema": "boswas-compliance-report/1",
            "device_id": self.device_id,
            "policy_version": self.policy_version,
            "summary": self.summary.to_dict(),
            "checks": [c.to_dict() for c in self.checks],
            "assessed_at": self.assessed_at,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class DeviceCommand:
    """A command received from the Control Plane (GET .../commands).

    ``issued_by`` is an opaque Control Plane actor reference for the audit
    trail, not a user identity (Boswas ID is not integrated).
    """

    id: str
    type: CommandType
    issued_at: str
    expires_at: str
    issued_by: str
    parameters: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, doc: dict) -> "DeviceCommand":
        try:
            ctype = CommandType(doc.get("type"))
        except ValueError:
            raise UnsupportedCommand(f"unsupported command type {doc.get('type')!r}") from None
        params = doc.get("parameters") or {}
        if not isinstance(params, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in params.items()):
            raise UnsupportedCommand("command parameters must be a string map")
        for key in ("id", "issued_at", "expires_at", "issued_by"):
            if not isinstance(doc.get(key), str) or not doc[key]:
                raise UnsupportedCommand(f"command field {key!r} missing")
        return cls(id=doc["id"], type=ctype, issued_at=doc["issued_at"], expires_at=doc["expires_at"],
                   issued_by=doc["issued_by"], parameters=dict(params))


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    state: str          # succeeded | failed | rejected | expired
    detail: str = ""

    def to_dict(self) -> dict:
        return {"command_id": self.command_id, "state": self.state, "detail": self.detail}


@dataclass(frozen=True)
class ApplicationInventoryItem:
    """One application on the device, aggregated over users (no user names)."""

    kind: str           # deb | winapp | flatpak
    id: str
    version: str | None
    status: str | None
    installations: int = 1

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "version": self.version, "status": self.status,
                "installations": self.installations}
