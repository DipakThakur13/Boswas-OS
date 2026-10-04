"""Privacy guard for everything a device sends.

Each outgoing message type has a closed field tree. ``check`` rejects any
key not in it, so content such as files, keystrokes, screenshots, browsing
history, passwords or keys cannot be added to a payload by accident; adding
a field means changing this allowlist, the JSON schema and
docs/security/privacy.md together.
"""

from __future__ import annotations

from .errors import PrivacyViolation
from .models import CHECK_STATUSES, SECURITY_CHECK_IDS

_OS = {"name": str, "version": str, "version_id": str, "build_id": (str, type(None)),
       "debian_version": (str, type(None)), "kernel": str}
_HARDWARE = {"vendor": (str, type(None)), "model": (str, type(None)), "firmware_version": (str, type(None)),
             "cpu": (str, type(None)), "memory_gib": (int, float, type(None)), "tpm_version": (str, type(None)),
             "boot_mode": (str, type(None))}
_SUMMARY = {"state": str, "pass": int, "warn": int, "fail": int, "unknown": int}

ALLOWLISTS: dict[str, dict] = {
    "boswas-heartbeat/1": {
        "schema": str, "device_id": str, "agent_version": str, "os": _OS, "uptime_seconds": int,
        "compliance": _SUMMARY, "policy_version": (str, type(None)),
        "update": {"channel": str, "state": str, "available_version": (str, type(None))},
        "security": "security-map", "sent_at": str,
    },
    "boswas-enrollment-request/1": {
        "schema": str, "device_id": str, "csr_pem": str, "os": _OS, "hardware": _HARDWARE,
        "profile": (str, type(None)), "enrollment_token": str,
    },
    "boswas-compliance-report/1": {
        "schema": str, "device_id": str, "policy_version": (str, type(None)), "summary": _SUMMARY,
        "checks": "check-list", "assessed_at": str, "basis": str,
    },
}

MAX_STRING = 4096


def _check(value, spec, where: str) -> None:
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
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise PrivacyViolation(f"{where}: expected an object")
        for key in value:
            if key not in spec:
                raise PrivacyViolation(f"{where}.{key}: field is not in the privacy allowlist")
        for key, sub in spec.items():
            if key not in value:
                raise PrivacyViolation(f"{where}.{key}: missing")
            _check(value[key], sub, f"{where}.{key}")
        return
    if isinstance(value, bool) and spec is int:
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
