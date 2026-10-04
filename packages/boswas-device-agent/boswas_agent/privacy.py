"""Privacy guard for everything a device sends.

Each outgoing message type has a closed field tree. ``check`` rejects any
key not in it, so content such as files, keystrokes, screenshots, browsing
history, passwords, keys or hardware serial numbers cannot be added to a
payload by accident; adding a field means changing this allowlist, the JSON
schema and docs/security/privacy.md together. The agent calls ``check`` on
every message before it is queued for the Control Plane.
"""

from __future__ import annotations

from dataclasses import dataclass

from .commands import DEVICE_REPORTED_STATUSES, RESULT_FIELDS
from .errors import PrivacyViolation
from .inventory import TRACKED_PACKAGES
from .models import CHECK_STATUSES, DEVICE_EVENT_TYPES, SECURITY_CHECK_IDS, AppState, DeviceState

MAX_STRING = 4096


@dataclass(frozen=True)
class Optional:
    """The key may be absent."""
    spec: object


@dataclass(frozen=True)
class Nullable:
    """The value may be null."""
    spec: object


@dataclass(frozen=True)
class Enum:
    values: tuple


@dataclass(frozen=True)
class ListOf:
    spec: object
    max_items: int


NONE = type(None)
_OS = {"name": str, "version": str, "version_id": str, "build_id": (str, NONE),
       "debian_version": (str, NONE), "kernel": str}
_HARDWARE = {"vendor": (str, NONE), "model": (str, NONE), "firmware_version": (str, NONE),
             "cpu": (str, NONE), "memory_gib": (int, float, NONE), "tpm_version": (str, NONE),
             "boot_mode": (str, NONE)}
_SUMMARY = {"state": str, "pass": int, "warn": int, "fail": int, "unknown": int}

_INV_OS = {**_OS, "architecture": str}
_INV_HARDWARE = {"cpu_model": (str, NONE), "cpu_count": int, "memory_gib": (int, float, NONE),
                 "vendor": (str, NONE), "model": (str, NONE), "firmware_version": (str, NONE),
                 "boot_mode": (str, NONE), "tpm_version": (str, NONE)}
_INV_STORAGE = {"root_total_gib": (int, float, NONE), "root_free_gib": (int, float, NONE)}
_INV_APP = {"id": str, "version": (str, NONE), "status": (str, NONE), "architecture": (str, NONE),
            "compatibility": str, "app_state": Enum(tuple(s.value for s in AppState)),
            "installations": int, "running": int}
_INV_COMPAT = {"available": bool, "wine_version": (str, NONE), "architectures": ListOf(Enum(("x86_64", "x86")), 4),
               "bubblewrap": (str, NONE), "apparmor": str, "policy_managed": bool, "healthy": bool}

ALLOWLISTS: dict[str, dict] = {
    "boswas-heartbeat/2": {
        "schema": str, "device_id": str, "agent_version": str,
        "state": Enum(tuple(s.value for s in DeviceState)), "policy_version": (str, NONE),
        "inventory_revision": (int, NONE), "pending_results": int, "sent_at": str,
    },
    "boswas-status-report/1": {
        "schema": str, "device_id": str, "agent_version": str, "os": _OS, "uptime_seconds": int,
        "compliance": _SUMMARY, "policy_version": (str, NONE),
        "update": {"channel": str, "state": str, "available_version": (str, NONE)},
        "security": "security-map", "sent_at": str,
    },
    "boswas-enrollment-request/1": {
        "schema": str, "device_id": str, "csr_pem": str, "os": _OS, "hardware": _HARDWARE,
        "profile": (str, NONE), "enrollment_token": str, "agent_version": str, "ephemeral": bool,
        "device_name": (str, NONE),
    },
    "boswas-compliance-report/1": {
        "schema": str, "device_id": str, "policy_version": (str, NONE), "summary": _SUMMARY,
        "checks": "check-list", "assessed_at": str, "basis": str,
    },
    "boswas-inventory/1": {
        "schema": str, "device_id": str, "revision": int, "collected_at": str,
        "policy": Enum(("minimal", "standard")), "os": _INV_OS, "packages": ListOf({
            "name": Enum(TRACKED_PACKAGES), "version": (str, NONE)}, len(TRACKED_PACKAGES)),
        "windows_applications": ListOf(_INV_APP, 1000), "windows_inventory_available": bool,
        "compatibility": _INV_COMPAT, "hardware": Optional(_INV_HARDWARE), "storage": Optional(_INV_STORAGE),
    },
    "boswas-command-result/1": {
        "schema": str, "command_id": str, "device_id": str,
        "status": Enum(tuple(s.value for s in DEVICE_REPORTED_STATUSES)), "result": "result-map",
        "error": Nullable({"code": str, "message": str}), "reported_at": str,
    },
    "boswas-device-event/1": {
        "schema": str, "device_id": str, "events": ListOf({
            "type": Enum(tuple(t.value for t in DEVICE_EVENT_TYPES)), "occurred_at": str,
            "source": Enum(("local", "remote")), "detail": str, "application_id": Optional(str),
            "command_id": Optional(str)}, 200),
    },
}


def _check(value, spec, where: str) -> None:
    if isinstance(spec, Nullable):
        if value is not None:
            _check(value, spec.spec, where)
        return
    if isinstance(spec, Enum):
        if value not in spec.values:
            raise PrivacyViolation(f"{where}: value not allowed")
        return
    if isinstance(spec, ListOf):
        if not isinstance(value, list) or len(value) > spec.max_items:
            raise PrivacyViolation(f"{where}: expected a list of at most {spec.max_items} items")
        for i, item in enumerate(value):
            _check(item, spec.spec, f"{where}[{i}]")
        return
    if spec == "security-map":
        if not isinstance(value, dict):
            raise PrivacyViolation(f"{where}: expected an object")
        for key, status in value.items():
            if key not in SECURITY_CHECK_IDS or status not in CHECK_STATUSES:
                raise PrivacyViolation(f"{where}.{key}: not an allowed posture check result")
        return
    if spec == "check-list":
        if not isinstance(value, list):
            raise PrivacyViolation(f"{where}: expected a list")
        for i, item in enumerate(value):
            _check(item, {"id": str, "status": str, "scored": bool}, f"{where}[{i}]")
            if item["status"] not in CHECK_STATUSES:
                raise PrivacyViolation(f"{where}[{i}].status: not a check status")
        return
    if spec == "result-map":
        if not isinstance(value, dict):
            raise PrivacyViolation(f"{where}: expected an object")
        for key, item in value.items():
            kind = RESULT_FIELDS.get(key)
            if kind is None:
                raise PrivacyViolation(f"{where}.{key}: field is not in the privacy allowlist")
            _check(item, kind, f"{where}.{key}")
        return
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise PrivacyViolation(f"{where}: expected an object")
        for key in value:
            if key not in spec:
                raise PrivacyViolation(f"{where}.{key}: field is not in the privacy allowlist")
        for key, sub in spec.items():
            if isinstance(sub, Optional):
                if key in value:
                    _check(value[key], sub.spec, f"{where}.{key}")
                continue
            if key not in value:
                raise PrivacyViolation(f"{where}.{key}: missing")
            _check(value[key], sub, f"{where}.{key}")
        return
    if isinstance(value, bool) and spec is int:
        raise PrivacyViolation(f"{where}: expected a number")
    if isinstance(value, bool) and isinstance(spec, tuple) and bool not in spec and int in spec:
        raise PrivacyViolation(f"{where}: expected a number")
    if not isinstance(value, spec):
        raise PrivacyViolation(f"{where}: unexpected type {type(value).__name__}")
    if isinstance(value, str) and len(value) > MAX_STRING:
        raise PrivacyViolation(f"{where}: value too long")


def check(payload: dict) -> dict:
    """Return the payload unchanged if it is within its allowlist; raise otherwise."""
    schema = payload.get("schema") if isinstance(payload, dict) else None
    if schema not in ALLOWLISTS:
        raise PrivacyViolation(f"unknown message schema {schema!r}")
    _check(payload, ALLOWLISTS[schema], schema)
    return payload
