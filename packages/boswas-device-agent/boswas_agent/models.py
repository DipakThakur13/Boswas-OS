"""Messages and states shared by the device agent and the Control Plane (API v1).

Every message has a JSON schema in packages/boswas-device-agent/schemas/;
tests keep both in sync. Field lists are deliberately closed: adding data to
what a device reports needs a schema change, a privacy review
(docs/security/privacy.md) and a test update. See privacy.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EnrollmentState(str, Enum):
    """Enrollment of this device with a Control Plane."""

    UNENROLLED = "unenrolled"
    PENDING = "pending"
    ENROLLED = "enrolled"
    RETIRED = "retired"


class DeviceState(str, Enum):
    """Normalised device state (state.py holds the transitions)."""

    INITIALIZING = "INITIALIZING"
    READY = "READY"
    DEGRADED = "DEGRADED"          # works locally, but a component needs attention
    OFFLINE = "OFFLINE"            # enrolled, Control Plane unreachable; local work continues
    UPDATING = "UPDATING"
    ERROR = "ERROR"                # identity or configuration unusable
    MAINTENANCE = "MAINTENANCE"    # remote commands paused by a local administrator


class AppState(str, Enum):
    """Normalised Windows application state (boswas_compat.ops.APP_STATES)."""

    INSTALLING = "INSTALLING"
    INSTALLED = "INSTALLED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    BLOCKED = "BLOCKED"
    UNSUPPORTED = "UNSUPPORTED"


class ConnectionState(str, Enum):
    STANDALONE = "STANDALONE"      # no Control Plane configured: a fully local device
    UNENROLLED = "UNENROLLED"      # configured, not enrolled
    CONNECTED = "CONNECTED"
    OFFLINE = "OFFLINE"            # enrolled, last contact failed
    REVOKED = "REVOKED"            # the Control Plane no longer accepts this device


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


class EventType(str, Enum):
    """Structured events (device-reported and Control Plane audit)."""

    DEVICE_REGISTERED = "DEVICE_REGISTERED"
    DEVICE_ONLINE = "DEVICE_ONLINE"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    APPLICATION_INSTALLED = "APPLICATION_INSTALLED"
    APPLICATION_REMOVED = "APPLICATION_REMOVED"
    APPLICATION_LAUNCHED = "APPLICATION_LAUNCHED"
    APPLICATION_BLOCKED = "APPLICATION_BLOCKED"
    APPLICATION_REPAIRED = "APPLICATION_REPAIRED"
    POLICY_UPDATED = "POLICY_UPDATED"
    COMMAND_CREATED = "COMMAND_CREATED"
    COMMAND_COMPLETED = "COMMAND_COMPLETED"
    COMMAND_FAILED = "COMMAND_FAILED"
    SECURITY_EVENT = "SECURITY_EVENT"
    # Control Plane administration (audit trail only).
    CATALOG_UPDATED = "CATALOG_UPDATED"
    ARTIFACT_UPLOADED = "ARTIFACT_UPLOADED"
    ENROLLMENT_TOKEN_CREATED = "ENROLLMENT_TOKEN_CREATED"
    OPERATOR_CHANGED = "OPERATOR_CHANGED"
    DEVICE_UPDATED = "DEVICE_UPDATED"
    DEVICE_RETIRED = "DEVICE_RETIRED"
    COMMAND_CANCELLED = "COMMAND_CANCELLED"


# Events a device may report; the others are recorded by the Control Plane itself.
DEVICE_EVENT_TYPES = (EventType.APPLICATION_INSTALLED, EventType.APPLICATION_REMOVED,
                      EventType.APPLICATION_LAUNCHED, EventType.APPLICATION_BLOCKED,
                      EventType.APPLICATION_REPAIRED, EventType.POLICY_UPDATED, EventType.SECURITY_EVENT)

CHECK_STATUSES = ("PASS", "WARN", "FAIL", "INFO", "UNKNOWN")
# Posture checks a status report may summarise (IDs from `boswas status`).
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
    """Inventory attributes. Never used to authenticate a device; no serial numbers."""

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
    """POST /api/v1/devices/{id}/heartbeat: liveness only, never content.

    Exactly what the Control Plane needs to know the device is alive and
    whether it should fetch something: no OS, hardware or user data.
    """

    device_id: str
    agent_version: str
    state: str                       # DeviceState value
    policy_version: str | None
    inventory_revision: int | None
    pending_results: int = 0
    sent_at: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": "boswas-heartbeat/2",
            "device_id": self.device_id,
            "agent_version": self.agent_version,
            "state": self.state,
            "policy_version": self.policy_version,
            "inventory_revision": self.inventory_revision,
            "pending_results": self.pending_results,
            "sent_at": self.sent_at,
        }


@dataclass(frozen=True)
class StatusReport:
    """POST /api/v1/devices/{id}/status: posture summary (TELEMETRY_POLICY=security).

    Status only; never content. Posture checks are reduced to check ID and
    result.
    """

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
            "schema": "boswas-status-report/1",
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
    """POST /api/v1/enroll.

    The enrollment token is a one-time secret issued by an administrator. It
    is sent once, never stored on the device, never logged and never part of
    repr().
    """

    device_id: str
    csr_pem: str                     # PKCS#10 request: public key only
    os: OsInfo
    hardware: HardwareFacts
    profile: str | None = None
    enrollment_token: str = field(default="", repr=False)
    agent_version: str = ""
    ephemeral: bool = False          # live session: identity disappears at shutdown
    device_name: str | None = None   # optional label set by the local administrator

    def to_dict(self) -> dict:
        return {
            "schema": "boswas-enrollment-request/1",
            "device_id": self.device_id,
            "csr_pem": self.csr_pem,
            "os": self.os.to_dict(),
            "hardware": self.hardware.to_dict(),
            "profile": self.profile,
            "enrollment_token": self.enrollment_token,
            "agent_version": self.agent_version,
            "ephemeral": self.ephemeral,
            "device_name": self.device_name,
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
