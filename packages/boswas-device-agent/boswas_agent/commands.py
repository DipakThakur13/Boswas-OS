"""Typed management commands, shared by the device agent and the Control Plane.

A command is one of a closed set of operations with a validated payload, a
target device, a creation and an expiry time, and an opaque actor reference
for the audit trail. There is no command that carries a shell command,
script, program path or code: the Control Plane manages the OS through these
operations only.

    Control Plane                      device agent
    QUEUED -> SENT  ------------------> ACKNOWLEDGED -> RUNNING -> SUCCEEDED | FAILED
       \\-> CANCELLED (before delivery)   (expired on arrival: EXPIRED)
       \\-> EXPIRED  (never delivered)

Idempotency (the device keeps the IDs of executed commands, and each
operation is safe to repeat): installing an installed version, removing a
removed application, stopping a stopped one, applying the current policy and
updating to the installed agent version all succeed without changing
anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from boswas_compat import manifest as compat_manifest

from .errors import CommandExpired, InvalidCommand, UnsupportedCommand

COMMAND_SCHEMA = "boswas-device-command/2"
RESULT_SCHEMA = "boswas-command-result/1"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

MIN_TTL = timedelta(seconds=60)
DEFAULT_TTL = timedelta(hours=24)
MAX_TTL = timedelta(days=7)
MAX_INSTALLER_BYTES = 64 * 1024 ** 3

_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_ACTOR_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9._@-]{1,96}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+:-]{0,63}$")
_POLICY_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class CommandType(str, Enum):
    """Operations the Control Plane may request. Nothing else is accepted.

    Destructive device operations (wipe, retire, factory reset) and any form
    of remote shell or code execution are intentionally absent.
    """

    INSTALL_APPLICATION = "INSTALL_APPLICATION"
    UPDATE_APPLICATION = "UPDATE_APPLICATION"
    REMOVE_APPLICATION = "REMOVE_APPLICATION"
    LAUNCH_APPLICATION = "LAUNCH_APPLICATION"
    STOP_APPLICATION = "STOP_APPLICATION"
    REPAIR_APPLICATION = "REPAIR_APPLICATION"
    REFRESH_INVENTORY = "REFRESH_INVENTORY"
    APPLY_POLICY = "APPLY_POLICY"
    UPDATE_AGENT = "UPDATE_AGENT"


# Run in the active user's session (Windows applications are per user).
APPLICATION_COMMANDS = frozenset({CommandType.INSTALL_APPLICATION, CommandType.UPDATE_APPLICATION,
                                  CommandType.REMOVE_APPLICATION, CommandType.LAUNCH_APPLICATION,
                                  CommandType.STOP_APPLICATION, CommandType.REPAIR_APPLICATION})
DEVICE_COMMANDS = frozenset(set(CommandType) - APPLICATION_COMMANDS)


class CommandStatus(str, Enum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset({CommandStatus.SUCCEEDED, CommandStatus.FAILED, CommandStatus.EXPIRED,
                               CommandStatus.CANCELLED})
# Statuses a device may report for a command it received.
DEVICE_REPORTED_STATUSES = frozenset({CommandStatus.ACKNOWLEDGED, CommandStatus.RUNNING, CommandStatus.SUCCEEDED,
                                      CommandStatus.FAILED, CommandStatus.EXPIRED})
_TRANSITIONS = {
    CommandStatus.QUEUED: {CommandStatus.SENT, CommandStatus.CANCELLED, CommandStatus.EXPIRED},
    CommandStatus.SENT: {CommandStatus.SENT, CommandStatus.ACKNOWLEDGED, CommandStatus.RUNNING,
                         CommandStatus.SUCCEEDED, CommandStatus.FAILED, CommandStatus.EXPIRED},
    CommandStatus.ACKNOWLEDGED: {CommandStatus.RUNNING, CommandStatus.SUCCEEDED, CommandStatus.FAILED,
                                 CommandStatus.EXPIRED},
    CommandStatus.RUNNING: {CommandStatus.SUCCEEDED, CommandStatus.FAILED},
}


def can_transition(old: CommandStatus, new: CommandStatus) -> bool:
    return new in _TRANSITIONS.get(old, set())


# --- time ----------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)


# --- payloads ------------------------------------------------------------------------

def _keys(payload: dict, required: set[str], optional: set[str], problems: list[str], where="payload") -> None:
    for key in sorted(set(payload) - required - optional):
        problems.append(f"{where}: unknown key {key!r}")
    for key in sorted(required - set(payload)):
        problems.append(f"{where}: missing key {key!r}")


def _app_id(payload: dict, problems: list[str]) -> None:
    if "application_id" in payload and not compat_manifest.valid_id(payload["application_id"]):
        problems.append("payload.application_id: not a valid application ID")


def _install_payload(payload: dict, problems: list[str]) -> None:
    _keys(payload, {"application_id", "version", "installer", "manifest"}, set(), problems)
    _app_id(payload, problems)
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip() or len(version) > 40:
        problems.append("payload.version: non-empty string of at most 40 characters")
    inst = payload.get("installer")
    if not isinstance(inst, dict):
        problems.append("payload.installer: must be an object")
        inst = {}
    _keys(inst, {"sha256", "size", "file_name"}, set(), problems, "payload.installer")
    if not isinstance(inst.get("sha256"), str) or not _SHA256_RE.fullmatch(inst.get("sha256", "")):
        problems.append("payload.installer.sha256: 64 lower-case hex characters")
    size = inst.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_INSTALLER_BYTES:
        problems.append("payload.installer.size: positive integer (bytes)")
    name = inst.get("file_name")
    if not isinstance(name, str) or not compat_manifest.FILE_NAME_RE.fullmatch(name) or name in (".", ".."):
        problems.append("payload.installer.file_name: a plain file name")
    doc = payload.get("manifest")
    manifest_problems = compat_manifest.validate(doc)
    problems.extend(f"payload.manifest: {p}" for p in manifest_problems)
    if not manifest_problems and isinstance(doc, dict):
        if doc["id"] != payload.get("application_id"):
            problems.append("payload.manifest.id: must equal payload.application_id")
        if doc["version"].strip() != (version or "").strip():
            problems.append("payload.manifest.version: must equal payload.version")
        if doc.get("installer", {}).get("sha256") != inst.get("sha256"):
            problems.append("payload.manifest.installer.sha256: must pin payload.installer.sha256")
        if doc["status"] == "blocked":
            problems.append("payload.manifest.status: a blocked application cannot be installed")


def _id_payload(payload: dict, problems: list[str]) -> None:
    _keys(payload, {"application_id"}, set(), problems)
    _app_id(payload, problems)


def _empty_payload(payload: dict, problems: list[str]) -> None:
    _keys(payload, set(), set(), problems)


def _policy_payload(payload: dict, problems: list[str]) -> None:
    _keys(payload, {"policy_version"}, set(), problems)
    if not isinstance(payload.get("policy_version"), str) or \
            not _POLICY_VERSION_RE.fullmatch(payload.get("policy_version", "")):
        problems.append("payload.policy_version: policy version, e.g. 'default-3'")


def _agent_payload(payload: dict, problems: list[str]) -> None:
    _keys(payload, {"version"}, set(), problems)
    if not isinstance(payload.get("version"), str) or not _VERSION_RE.fullmatch(payload.get("version", "")):
        problems.append("payload.version: a Debian package version")


PAYLOAD_VALIDATORS = {
    CommandType.INSTALL_APPLICATION: _install_payload,
    CommandType.UPDATE_APPLICATION: _install_payload,
    CommandType.REMOVE_APPLICATION: _id_payload,
    CommandType.LAUNCH_APPLICATION: _id_payload,
    CommandType.STOP_APPLICATION: _id_payload,
    CommandType.REPAIR_APPLICATION: _id_payload,
    CommandType.REFRESH_INVENTORY: _empty_payload,
    CommandType.APPLY_POLICY: _policy_payload,
    CommandType.UPDATE_AGENT: _agent_payload,
}


def command_type(value: object) -> CommandType:
    try:
        return CommandType(value)
    except ValueError:
        raise UnsupportedCommand(f"unsupported command type {str(value)[:40]!r}") from None


def validate_payload(ctype: CommandType, payload: object) -> list[str]:
    """Problems with a command payload (empty list: valid)."""
    if not isinstance(payload, dict):
        return ["payload: must be an object"]
    problems: list[str] = []
    PAYLOAD_VALIDATORS[ctype](payload, problems)
    return problems


def architecture_refusal(ctype: CommandType, payload: dict) -> str | None:
    """Why an install/update payload targets an unsupported Windows architecture.

    Boswas OS runs x86_64 applications only; a 32-bit catalog entry must
    never lead to a download or an installation attempt.
    """
    if ctype not in (CommandType.INSTALL_APPLICATION, CommandType.UPDATE_APPLICATION):
        return None
    arch = (payload.get("manifest") or {}).get("architecture")
    if arch == "x86_64":
        return None
    from boswas_compat import UNSUPPORTED_32BIT_MESSAGE
    if arch == "x86":
        return UNSUPPORTED_32BIT_MESSAGE
    return f"unsupported Windows architecture {str(arch)[:16]!r}; Boswas OS runs x86_64 applications only"


# --- commands ------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    command_id: str
    device_id: str
    type: CommandType
    payload: dict
    created_at: str
    expires_at: str
    created_by: str

    def to_dict(self) -> dict:
        return {"schema": COMMAND_SCHEMA, "command_id": self.command_id, "device_id": self.device_id,
                "type": self.type.value, "payload": self.payload, "created_at": self.created_at,
                "expires_at": self.expires_at, "created_by": self.created_by}

    def expired(self, now: datetime | None = None) -> bool:
        return parse_time(self.expires_at) <= (now or utc_now())

    @property
    def application_id(self) -> str | None:
        return self.payload.get("application_id")


def parse_command(doc: object, *, device_id: str | None = None, now: datetime | None = None) -> Command:
    """Validate a command document received by a device.

    Raises UnsupportedCommand (unknown type), InvalidCommand (envelope or
    payload), CommandExpired (expiry passed). Nothing invalid is ever run.
    """
    if not isinstance(doc, dict):
        raise InvalidCommand("command must be a JSON object")
    ctype = command_type(doc.get("type"))
    allowed = {"schema", "command_id", "device_id", "type", "payload", "created_at", "expires_at", "created_by"}
    unknown = sorted(set(doc) - allowed)
    if unknown:
        raise InvalidCommand(f"unknown command field {unknown[0]!r}")
    if doc.get("schema") != COMMAND_SCHEMA:
        raise InvalidCommand(f"command schema must be {COMMAND_SCHEMA}")
    for key, pattern in (("command_id", _UUID4_RE), ("device_id", _UUID4_RE), ("created_by", _ACTOR_RE)):
        if not isinstance(doc.get(key), str) or not pattern.fullmatch(doc[key]):
            raise InvalidCommand(f"command field {key!r} missing or invalid")
    if device_id is not None and doc["device_id"] != device_id:
        raise InvalidCommand("command addressed to another device")
    try:
        created, expires = parse_time(doc.get("created_at")), parse_time(doc.get("expires_at"))
    except ValueError:
        raise InvalidCommand("command timestamps must be UTC (YYYY-MM-DDTHH:MM:SSZ)") from None
    if expires <= created or expires - created > MAX_TTL:
        raise InvalidCommand("command expiry must be after its creation and at most 7 days later")
    problems = validate_payload(ctype, doc.get("payload"))
    if problems:
        raise InvalidCommand(f"invalid {ctype.value} payload: {problems[0]}")
    command = Command(command_id=doc["command_id"], device_id=doc["device_id"], type=ctype,
                      payload=dict(doc["payload"]), created_at=doc["created_at"], expires_at=doc["expires_at"],
                      created_by=doc["created_by"])
    if command.expired(now):
        raise CommandExpired(f"command {command.command_id} expired at {command.expires_at}")
    return command


# --- results -------------------------------------------------------------------------

# Values a result may carry: short typed facts, never program output or logs.
RESULT_FIELDS = {
    "application_id": str, "version": str, "app_state": str, "was_running": bool, "stopped": bool,
    "healthy": bool, "removed": bool, "installed": bool, "already": bool, "inventory_revision": int,
    "policy_version": str, "agent_version": str, "started": bool,
}
ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MAX_ERROR_MESSAGE = 500


@dataclass
class CommandOutcome:
    status: CommandStatus
    result: dict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def ok(cls, **result) -> "CommandOutcome":
        return cls(CommandStatus.SUCCEEDED, result)

    @classmethod
    def failed(cls, code: str, message: str, **result) -> "CommandOutcome":
        return cls(CommandStatus.FAILED, result, code, message)


def _clean_text(value: str, limit: int) -> str:
    return "".join(c for c in value if c.isprintable())[:limit]


def result_document(command_id: str, device_id: str, outcome: CommandOutcome, at: str | None = None) -> dict:
    """The command-result message, restricted to RESULT_FIELDS."""
    result = {}
    for key, value in outcome.result.items():
        kind = RESULT_FIELDS.get(key)
        if kind is None or isinstance(value, bool) != (kind is bool) or not isinstance(value, kind):
            continue
        result[key] = _clean_text(value, 200) if isinstance(value, str) else value
    error = None
    if outcome.error_code:
        code = outcome.error_code if ERROR_CODE_RE.fullmatch(outcome.error_code) else "FAILED"
        error = {"code": code, "message": _clean_text(outcome.error_message or "", MAX_ERROR_MESSAGE)}
    return {"schema": RESULT_SCHEMA, "command_id": command_id, "device_id": device_id,
            "status": outcome.status.value, "result": result, "error": error,
            "reported_at": at or format_time(utc_now())}
