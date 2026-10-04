"""Control Plane domain rules.

Devices: enrollment with a one-time token and a CSR, the registry,
heartbeats, inventory and status reports, device events.
Commands: typed operations built by the Control Plane itself (an operator
names an application, the payload comes from the catalog), validated with
the same code the device uses, idempotent, expiring, audited.
Catalog: manifests validated by boswas-compat's validator, installers stored
as content-addressed artifacts; 32-bit entries are UNSUPPORTED_ARCHITECTURE
and can never become an install command.
Policies: versioned, validated and signed; devices pull the latest version
of the policy assigned to them.
Audit: every security-relevant action becomes an event in the hash-chained
events table; details never contain secrets.

Every document a device sends is checked against the same closed privacy
allowlists the device uses, so unexpected data is refused, not stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from boswas_agent import commands, policydoc, privacy
from boswas_agent.device_identity import is_valid_device_id
from boswas_agent.errors import PolicyVerificationError, PrivacyViolation, UnsupportedCommand
from boswas_agent.models import DEVICE_EVENT_TYPES, EventType
from boswas_compat import installer as compat_installer
from boswas_compat import manifest as compat_manifest
from boswas_compat.errors import WinAppError

from .auth import Principal, new_token, split_token, verify_secret
from .pki import Pki, PkiError, fingerprint_der
from .store import Store

TIME = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_POLICY = "default"
ACTIVE_COMMANDS = ("QUEUED", "SENT", "ACKNOWLEDGED", "RUNNING")
_SECRET_KEY_RE = re.compile(r"token|secret|password|passwd|private|credential", re.IGNORECASE)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_OPERATOR_RE = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")
_FILE_RE = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,255}$")


class ServiceError(Exception):
    status = 400

    def __init__(self, code: str, message: str, *, status: int | None = None, details: list | None = None):
        super().__init__(message)
        self.code, self.message = code, message
        if status:
            self.status = status
        self.details = details or []


def bad(code, message, details=None):
    return ServiceError(code, message, status=400, details=details)


def not_found(what):
    return ServiceError("NOT_FOUND", f"{what} not found", status=404)


def conflict(code, message):
    return ServiceError(code, message, status=409)


def forbidden(code, message):
    return ServiceError(code, message, status=403)


def unprocessable(code, message, details=None):
    return ServiceError(code, message, status=422, details=details)


def redact(value, depth: int = 0):
    """Event details: no secret-looking keys, short printable strings, bounded depth."""
    if isinstance(value, dict):
        if depth > 2:
            return "…"
        return {str(k)[:64]: redact(v, depth + 1) for k, v in list(value.items())[:30]
                if not _SECRET_KEY_RE.search(str(k))}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:30]]
    if isinstance(value, str):
        return "".join(c for c in value if c.isprintable())[:300]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:100]


class ControlPlane:
    def __init__(self, store: Store, pki: Pki, *, public_url: str, artifacts_dir: Path, clock=time.time,
                 device_cert_days: int = 365, offline_floor: int = 180, max_artifact_bytes: int = 4096 * 1024 ** 2):
        self.store, self.pki = store, pki
        self.public_url = public_url.rstrip("/")
        self.artifacts_dir = Path(artifacts_dir)
        self.clock = clock
        self.device_cert_days = device_cert_days
        self.offline_floor = offline_floor
        self.max_artifact_bytes = max_artifact_bytes

    # --- time ---------------------------------------------------------------------------
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc).replace(microsecond=0)

    def stamp(self, value: datetime | None = None) -> str:
        return (value or self.now()).strftime(TIME)

    @staticmethod
    def parse(value: str) -> datetime:
        return datetime.strptime(value, TIME).replace(tzinfo=timezone.utc)

    # --- events --------------------------------------------------------------------------------
    def record(self, event_type: EventType | str, *, actor: str, source: str = "control-plane",
               device_id: str | None = None, application_id: str | None = None, command_id: str | None = None,
               detail: dict | None = None, occurred_at: str | None = None) -> dict:
        etype = event_type.value if isinstance(event_type, EventType) else event_type
        row = self.store.append_event({
            "occurred_at": occurred_at or self.stamp(), "recorded_at": self.stamp(), "type": etype, "source": source,
            "actor": actor, "device_id": device_id, "application_id": application_id, "command_id": command_id,
            "detail": json.dumps(redact(detail or {}), sort_keys=True)})
        return self._event(row)

    @staticmethod
    def _event(row: dict) -> dict:
        out = {k: row.get(k) for k in ("id", "occurred_at", "recorded_at", "type", "source", "actor", "device_id",
                                       "application_id", "command_id")}
        try:
            out["detail"] = json.loads(row.get("detail") or "{}")
        except ValueError:
            out["detail"] = {}
        return out

    def events(self, *, device_id: str | None = None, event_type: str | None = None, before_id: int | None = None,
               limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM events WHERE 1=1", []
        if device_id:
            sql += " AND device_id = ?"
            params.append(device_id)
        if event_type:
            sql += " AND type = ?"
            params.append(event_type)
        if before_id:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        return [self._event(r) for r in self.store.query(sql, tuple(params))]

    def verify_events(self) -> dict:
        intact, count, broken = self.store.verify_events()
        return {"valid": intact, "events": count, "first_invalid_event": broken}

    # --- operators and enrollment tokens ------------------------------------------------------
    def add_operator(self, actor: str, name: str, role: str) -> dict:
        if not _OPERATOR_RE.fullmatch(name or ""):
            raise bad("INVALID_NAME", "operator names are 2-32 lower-case letters, digits, '.', '_' or '-'")
        if role not in ("viewer", "operator", "admin"):
            raise bad("INVALID_ROLE", "role must be viewer, operator or admin")
        token, prefix, digest = new_token("bcp")
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM operators WHERE name = ?", (name,)).fetchone():
                raise conflict("EXISTS", f"operator {name} already exists")
            conn.execute("INSERT INTO operators (name, role, token_prefix, token_hash, created_at, created_by) "
                         "VALUES (?,?,?,?,?,?)", (name, role, prefix, digest, self.stamp(), actor))
            self.record(EventType.OPERATOR_CHANGED, actor=actor, detail={"operator": name, "role": role,
                                                                         "action": "created"})
        return {"name": name, "role": role, "token": token}

    def list_operators(self) -> list[dict]:
        rows = self.store.query("SELECT name, role, created_at, created_by, disabled, last_used_at FROM operators "
                                "ORDER BY name")
        for r in rows:
            r["disabled"] = bool(r["disabled"])
        return rows

    def disable_operator(self, actor: str, name: str) -> dict:
        with self.store.transaction() as conn:
            cur = conn.execute("UPDATE operators SET disabled = 1 WHERE name = ?", (name,))
            if cur.rowcount == 0:
                raise not_found(f"operator {name}")
            self.record(EventType.OPERATOR_CHANGED, actor=actor, detail={"operator": name, "action": "disabled"})
        return {"name": name, "disabled": True}

    def create_enrollment_token(self, principal: Principal, *, profile: str | None = None,
                                policy_name: str | None = None, ttl_hours: int = 24, allow_ephemeral: bool = False,
                                max_uses: int = 1, description: str | None = None,
                                device_id: str | None = None) -> dict:
        """A one-time enrollment token. With device_id it enrolls only that device, and may replace
        its current registration (a reinstalled device whose credentials are gone)."""
        if device_id is not None and not is_valid_device_id(device_id):
            raise bad("INVALID_DEVICE_ID", "device_id must be a device ID (random UUID version 4)")
        if profile is not None and not _PROFILE_RE.fullmatch(profile):
            raise bad("INVALID_PROFILE", "profile must be a short lower-case identifier")
        if policy_name is not None and not policydoc.NAME_RE.fullmatch(policy_name):
            raise bad("INVALID_POLICY_NAME", "policy names are lower-case identifiers")
        if not isinstance(ttl_hours, int) or isinstance(ttl_hours, bool) or not 1 <= ttl_hours <= 720:
            raise bad("INVALID_TTL", "ttl_hours must be 1 to 720")
        if not isinstance(max_uses, int) or isinstance(max_uses, bool) or not 1 <= max_uses <= 1000:
            raise bad("INVALID_MAX_USES", "max_uses must be 1 to 1000")
        if description is not None and (not isinstance(description, str) or len(description) > 200):
            raise bad("INVALID_DESCRIPTION", "description: at most 200 characters")
        token, prefix, digest = new_token("bet")
        expires = self.now() + timedelta(hours=ttl_hours)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO enrollment_tokens (token_prefix, token_hash, created_at, created_by, expires_at, "
                         "profile, policy_name, allow_ephemeral, max_uses, description, device_id) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (prefix, digest, self.stamp(), principal.actor, self.stamp(expires), profile, policy_name,
                          int(bool(allow_ephemeral)), max_uses, description, device_id))
            self.record(EventType.ENROLLMENT_TOKEN_CREATED, actor=principal.actor, device_id=device_id,
                        detail={"token_id": prefix, "profile": profile, "policy": policy_name,
                                "expires_at": self.stamp(expires), "max_uses": max_uses,
                                "allow_ephemeral": bool(allow_ephemeral), "bound_to_device": bool(device_id)})
        return {"token": token, "token_id": prefix, "expires_at": self.stamp(expires), "max_uses": max_uses,
                "profile": profile, "policy_name": policy_name, "allow_ephemeral": bool(allow_ephemeral),
                "device_id": device_id}

    def list_enrollment_tokens(self) -> list[dict]:
        rows = self.store.query("SELECT token_prefix AS token_id, created_at, created_by, expires_at, profile, "
                                "policy_name, allow_ephemeral, max_uses, uses, description, revoked, device_id "
                                "FROM enrollment_tokens ORDER BY id DESC")
        for r in rows:
            r["allow_ephemeral"], r["revoked"] = bool(r["allow_ephemeral"]), bool(r["revoked"])
        return rows

    def revoke_enrollment_token(self, principal: Principal, token_id: str) -> dict:
        with self.store.transaction() as conn:
            cur = conn.execute("UPDATE enrollment_tokens SET revoked = 1 WHERE token_prefix = ?", (token_id,))
            if cur.rowcount == 0:
                raise not_found("enrollment token")
            self.record(EventType.SECURITY_EVENT, actor=principal.actor,
                        detail={"action": "enrollment token revoked", "token_id": token_id})
        return {"token_id": token_id, "revoked": True}

    # --- enrollment -----------------------------------------------------------------------------
    def enroll(self, doc: object) -> dict:
        if not isinstance(doc, dict) or doc.get("schema") != "boswas-enrollment-request/1":
            raise bad("INVALID_REQUEST", "not an enrollment request")
        try:
            privacy.check(doc)
        except PrivacyViolation as exc:
            raise bad("INVALID_REQUEST", str(exc)) from exc
        device_id = doc["device_id"]
        if not is_valid_device_id(device_id):
            raise bad("INVALID_DEVICE_ID", "the device ID must be a random UUID (version 4)")
        parts = split_token(doc["enrollment_token"], "bet")
        refused = forbidden("INVALID_TOKEN", "the enrollment token is not valid")
        if parts is None:
            raise refused
        prefix, secret = parts
        token = self.store.one("SELECT * FROM enrollment_tokens WHERE token_prefix = ?", (prefix,))
        if token is None or not verify_secret(secret, token["token_hash"]):
            # Recorded outside any transaction, so the refusal itself cannot roll it back.
            self.record(EventType.SECURITY_EVENT, actor=f"device:{device_id}", device_id=device_id,
                        detail={"action": "enrollment refused", "reason": "invalid token"})
            raise refused
        with self.store.transaction() as conn:
            token = conn.execute("SELECT * FROM enrollment_tokens WHERE id = ?", (token["id"],)).fetchone()
            usable = not token["revoked"] and token["uses"] < token["max_uses"] and \
                self.parse(token["expires_at"]) > self.now()
        if not usable:
            self.record(EventType.SECURITY_EVENT, actor=f"device:{device_id}", device_id=device_id,
                        detail={"action": "enrollment refused", "reason": "token expired, used up or revoked",
                                "token_id": prefix})
            raise refused
        with self.store.transaction() as conn:
            token = conn.execute("SELECT * FROM enrollment_tokens WHERE id = ?", (token["id"],)).fetchone()
            if token["revoked"] or token["uses"] >= token["max_uses"]:
                raise refused                        # used concurrently by another device
            if doc["ephemeral"] and not token["allow_ephemeral"]:
                raise forbidden("EPHEMERAL_NOT_ALLOWED", "live-session (ephemeral) devices need a token that "
                                                         "allows them")
            if token["device_id"] is not None and token["device_id"] != device_id:
                raise refused                        # issued for another device
            existing = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            if existing is not None and existing["status"] == "retired":
                raise forbidden("DEVICE_RETIRED", "this device was retired; it cannot enroll again")
            if existing is not None and token["device_id"] != device_id:
                # Device IDs are not secret: replacing an enrolled device's certificate needs a token
                # an administrator issued for that device, not just any valid token.
                raise forbidden("DEVICE_ALREADY_ENROLLED", "this device is already enrolled; re-enrolling it "
                                                           "needs a token issued for this device")
            try:
                cert = self.pki.issue_device_certificate(doc["csr_pem"], device_id, self.device_cert_days)
            except PkiError as exc:
                raise bad("INVALID_CSR", str(exc)) from exc
            policy_name = token["policy_name"] or (existing["policy_name"] if existing else DEFAULT_POLICY)
            profile = doc.get("profile") or token["profile"]
            if profile is not None and not _PROFILE_RE.fullmatch(profile):
                profile = token["profile"]
            name = doc.get("device_name") if doc.get("device_name") and _NAME_RE.fullmatch(doc["device_name"]) \
                else None
            os_doc, hw = doc["os"], doc["hardware"]
            now = self.stamp()
            values = (name, profile, policy_name, cert.fingerprint, cert.expires_at, now, token["id"],
                      int(doc["ephemeral"]), os_doc["name"], os_doc["version"], os_doc["version_id"],
                      os_doc.get("build_id"), os_doc.get("debian_version"), os_doc["kernel"],
                      json.dumps(hw, sort_keys=True), doc["agent_version"], now)
            if existing is None:
                conn.execute("INSERT INTO devices (name, profile, policy_name, certificate_fingerprint, "
                             "certificate_expires_at, enrolled_at, enrolled_with, ephemeral, os_name, os_version, "
                             "os_version_id, os_build_id, debian_version, kernel, hardware, agent_version, updated_at, "
                             "device_id, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active')",
                             (*values, device_id))
            else:
                conn.execute("UPDATE devices SET name = ?, profile = ?, policy_name = ?, certificate_fingerprint = ?, "
                             "certificate_expires_at = ?, enrolled_at = ?, enrolled_with = ?, ephemeral = ?, "
                             "os_name = ?, os_version = ?, os_version_id = ?, os_build_id = ?, debian_version = ?, "
                             "kernel = ?, hardware = ?, agent_version = ?, updated_at = ? WHERE device_id = ?",
                             (*values, device_id))
            conn.execute("UPDATE enrollment_tokens SET uses = uses + 1 WHERE id = ?", (token["id"],))
            self.record(EventType.DEVICE_REGISTERED, actor=f"device:{device_id}", device_id=device_id,
                        detail={"re_enrolled": existing is not None, "ephemeral": bool(doc["ephemeral"]),
                                "profile": profile, "policy": policy_name, "token_id": prefix,
                                "agent_version": doc["agent_version"]})
        policy = self.current_policy(policy_name)
        return {"device_id": device_id, "certificate_pem": cert.pem, "policy_public_key_pem": self.pki.policy_public_key(),
                "policy_version": policy["version"] if policy else None, "profile": profile,
                "control_plane_url": self.public_url, "heartbeat_seconds": self._heartbeat_seconds(policy)}

    # --- device-facing ----------------------------------------------------------------------------
    def device_for_certificate(self, certificate_der: bytes | None, device_id: str) -> dict:
        if not certificate_der:
            raise ServiceError("CERTIFICATE_REQUIRED", "a device certificate is required", status=401)
        row = self.store.one("SELECT * FROM devices WHERE certificate_fingerprint = ?",
                             (fingerprint_der(certificate_der),))
        if row is None:
            if self.store.one("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)):
                raise forbidden("CERTIFICATE_REVOKED", "this certificate was replaced by a newer enrollment")
            raise forbidden("UNKNOWN_DEVICE", "unknown device certificate")
        if row["device_id"] != device_id:
            raise forbidden("DEVICE_MISMATCH", "the certificate belongs to another device")
        if row["status"] != "active":
            raise forbidden("DEVICE_RETIRED", "this device was retired")
        return row

    def _check(self, doc: object, schema: str, device: dict) -> dict:
        if not isinstance(doc, dict) or doc.get("schema") != schema:
            raise bad("INVALID_DOCUMENT", f"expected a {schema} document")
        try:
            privacy.check(doc)
        except PrivacyViolation as exc:
            raise bad("PRIVACY_VIOLATION", str(exc)) from exc
        if doc.get("device_id") != device["device_id"]:
            raise forbidden("DEVICE_MISMATCH", "document for another device")
        return doc

    def heartbeat(self, device: dict, doc: object) -> dict:
        doc = self._check(doc, "boswas-heartbeat/2", device)
        now = self.stamp()
        with self.store.transaction() as conn:
            conn.execute("UPDATE devices SET agent_version = ?, device_state = ?, reported_policy_version = ?, "
                         "inventory_revision = ?, pending_results = ?, last_heartbeat_at = ?, connection = 'online', "
                         "updated_at = ? WHERE device_id = ?",
                         (doc["agent_version"], doc["state"], doc["policy_version"], doc["inventory_revision"],
                          doc["pending_results"], now, now, device["device_id"]))
            if device["connection"] != "online":
                self.record(EventType.DEVICE_ONLINE, actor=f"device:{device['device_id']}",
                            device_id=device["device_id"], detail={"state": doc["state"]})
        policy = self.current_policy(device["policy_name"])
        stored = self.store.one("SELECT revision FROM reports WHERE device_id = ? AND kind = 'inventory'",
                                (device["device_id"],))
        pending = self.store.one("SELECT COUNT(*) AS n FROM commands WHERE device_id = ? AND status IN "
                                 "('QUEUED', 'SENT') AND expires_at > ?", (device["device_id"], now))["n"]
        return {"accepted": True, "policy_version": policy["version"] if policy else None, "commands_pending": pending,
                "next_heartbeat_seconds": self._heartbeat_seconds(policy),
                "inventory_requested": stored is None or stored["revision"] != doc["inventory_revision"],
                "server_time": now}

    def store_report(self, device: dict, kind: str, doc: object) -> dict:
        schema = {"inventory": "boswas-inventory/1", "status": "boswas-status-report/1",
                  "compliance": "boswas-compliance-report/1"}[kind]
        doc = self._check(doc, schema, device)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO reports (device_id, kind, received_at, revision, document) VALUES (?,?,?,?,?) "
                         "ON CONFLICT (device_id, kind) DO UPDATE SET received_at = excluded.received_at, "
                         "revision = excluded.revision, document = excluded.document",
                         (device["device_id"], kind, self.stamp(), doc.get("revision"), json.dumps(doc, sort_keys=True)))
            if kind == "inventory":
                os_doc = doc["os"]
                conn.execute("UPDATE devices SET os_name = ?, os_version = ?, os_version_id = ?, os_build_id = ?, "
                             "debian_version = ?, kernel = ?, architecture = ?, updated_at = ? WHERE device_id = ?",
                             (os_doc["name"], os_doc["version"], os_doc["version_id"], os_doc["build_id"],
                              os_doc["debian_version"], os_doc["kernel"], os_doc["architecture"], self.stamp(),
                              device["device_id"]))
        return {"accepted": True}

    def device_events(self, device: dict, doc: object) -> dict:
        doc = self._check(doc, "boswas-device-event/1", device)
        allowed = {t.value for t in DEVICE_EVENT_TYPES}
        for event in doc["events"]:
            if event["type"] not in allowed:
                continue
            try:
                occurred = self.stamp(self.parse(event["occurred_at"]))
            except ValueError:
                occurred = self.stamp()
            self.record(event["type"], actor=f"device:{device['device_id']}", source="device",
                        device_id=device["device_id"], application_id=event.get("application_id"),
                        command_id=event.get("command_id"), occurred_at=occurred,
                        detail={"detail": event["detail"], "origin": event["source"]})
        return {"accepted": len(doc["events"])}

    def pending_commands(self, device: dict) -> dict:
        """Commands for the device: QUEUED become SENT; SENT ones not acknowledged yet are delivered again
        (the device recognises command IDs it has seen)."""
        now = self.stamp()
        with self.store.transaction() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM commands WHERE device_id = ? AND status IN ('QUEUED', 'SENT') AND expires_at > ? "
                "ORDER BY created_at LIMIT 50", (device["device_id"], now))]
            for row in rows:
                if row["status"] == "QUEUED":
                    conn.execute("UPDATE commands SET status = 'SENT', sent_at = ?, updated_at = ? WHERE command_id = ?",
                                 (now, now, row["command_id"]))
        return {"commands": [self._command_doc(r) for r in rows]}

    def acknowledge(self, device: dict, command_id: str, body: object) -> dict:
        status = body.get("status") if isinstance(body, dict) else None
        if status not in ("ACKNOWLEDGED", "RUNNING"):
            raise bad("INVALID_STATUS", "status must be ACKNOWLEDGED or RUNNING")
        return self._transition(device, command_id, status, None)

    def command_result(self, device: dict, command_id: str, doc: object) -> dict:
        doc = self._check(doc, "boswas-command-result/1", device)
        if doc["command_id"] != command_id:
            raise bad("INVALID_DOCUMENT", "result for another command")
        if doc["status"] not in ("SUCCEEDED", "FAILED", "EXPIRED"):
            raise bad("INVALID_STATUS", "a result reports SUCCEEDED, FAILED or EXPIRED")
        return self._transition(device, command_id, doc["status"], doc)

    def _transition(self, device: dict, command_id: str, status: str, result: dict | None) -> dict:
        now = self.stamp()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM commands WHERE command_id = ? AND device_id = ?",
                               (command_id, device["device_id"])).fetchone()
            if row is None:
                raise not_found("command")
            old = commands.CommandStatus(row["status"])
            new = commands.CommandStatus(status)
            if old == new or (old == commands.CommandStatus.RUNNING and new == commands.CommandStatus.ACKNOWLEDGED):
                return {"command_id": command_id, "status": old.value}
            if not commands.can_transition(old, new):
                raise conflict("COMMAND_NOT_ACTIVE", f"command is {old.value}")
            column = {"ACKNOWLEDGED": "acknowledged_at", "RUNNING": "started_at"}.get(status, "finished_at")
            error = (result or {}).get("error") or {}
            conn.execute(f"UPDATE commands SET status = ?, {column} = ?, updated_at = ?, result = COALESCE(?, result), "
                         "error_code = COALESCE(?, error_code), error_message = COALESCE(?, error_message) "
                         "WHERE command_id = ?",
                         (status, now, now, json.dumps(result["result"], sort_keys=True) if result else None,
                          error.get("code"), error.get("message"), command_id))
            if new in commands.TERMINAL_STATUSES:
                payload = json.loads(row["payload"])
                self.record(EventType.COMMAND_COMPLETED if new == commands.CommandStatus.SUCCEEDED
                            else EventType.COMMAND_FAILED, actor=f"device:{device['device_id']}",
                            device_id=device["device_id"], command_id=command_id,
                            application_id=payload.get("application_id"),
                            detail={"type": row["type"], "status": status, "error_code": error.get("code"),
                                    "error": error.get("message")})
        return {"command_id": command_id, "status": status}

    def policy_envelope(self, device: dict) -> dict | None:
        policy = self.current_policy(device["policy_name"])
        return json.loads(policy["envelope"]) if policy else None

    def artifact_for_device(self, device: dict, sha256: str) -> tuple[Path, dict]:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
            raise not_found("artifact")
        # Only installers of catalogued applications are served.
        row = self.store.one("SELECT a.* FROM artifacts a JOIN applications p ON p.installer_sha256 = a.sha256 "
                             "WHERE a.sha256 = ? LIMIT 1", (sha256,))
        path = self.artifacts_dir / sha256
        if row is None or not path.is_file():
            raise not_found("artifact")
        return path, row

    # --- registry (operators) ---------------------------------------------------------------------
    def _device_view(self, row: dict) -> dict:
        view = {k: row.get(k) for k in ("device_id", "name", "profile", "policy_name", "status", "enrolled_at",
                                        "certificate_fingerprint", "certificate_expires_at", "agent_version",
                                        "device_state", "connection", "last_heartbeat_at", "reported_policy_version",
                                        "inventory_revision", "pending_results", "updated_at", "architecture")}
        view["ephemeral"] = bool(row.get("ephemeral"))
        view["os"] = {"name": row.get("os_name"), "version": row.get("os_version"),
                      "version_id": row.get("os_version_id"), "build_id": row.get("os_build_id"),
                      "debian_version": row.get("debian_version"), "kernel": row.get("kernel")}
        try:
            view["hardware"] = json.loads(row.get("hardware") or "null")
        except ValueError:
            view["hardware"] = None
        policy = self.current_policy(row["policy_name"])
        view["policy_version"] = policy["version"] if policy else None
        view["policy_current"] = bool(policy) and row.get("reported_policy_version") == policy["version"]
        if row.get("last_heartbeat_at"):
            view["seconds_since_heartbeat"] = int((self.now() - self.parse(row["last_heartbeat_at"])).total_seconds())
        return view

    def list_devices(self, *, status: str | None = None, connection: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM devices WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if connection:
            sql += " AND connection = ?"
            params.append(connection)
        return [self._device_view(r) for r in self.store.query(sql + " ORDER BY enrolled_at DESC", tuple(params))]

    def get_device_row(self, device_id: str) -> dict:
        row = self.store.one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        if row is None:
            raise not_found("device")
        return row

    def get_device(self, device_id: str) -> dict:
        return self._device_view(self.get_device_row(device_id))

    def update_device(self, principal: Principal, device_id: str, body: object) -> dict:
        if not isinstance(body, dict) or not body or set(body) - {"name", "policy_name", "profile"}:
            raise bad("INVALID_REQUEST", "name, policy_name and profile can be changed")
        if self.get_device_row(device_id)["status"] == "retired":
            raise conflict("DEVICE_RETIRED", "a retired device cannot be changed")
        sets, params = [], []
        if "name" in body:
            if body["name"] is not None and not (isinstance(body["name"], str) and _NAME_RE.fullmatch(body["name"])):
                raise bad("INVALID_NAME", "names use letters, digits, space, '.', '_' and '-' (max 64)")
            sets.append("name = ?")
            params.append(body["name"])
        if "profile" in body:
            if body["profile"] is not None and not (isinstance(body["profile"], str) and
                                                    _PROFILE_RE.fullmatch(body["profile"])):
                raise bad("INVALID_PROFILE", "profile must be a short lower-case identifier")
            sets.append("profile = ?")
            params.append(body["profile"])
        if "policy_name" in body:
            if not isinstance(body["policy_name"], str) or not policydoc.NAME_RE.fullmatch(body["policy_name"]):
                raise bad("INVALID_POLICY_NAME", "policy names are lower-case identifiers")
            if self.current_policy(body["policy_name"]) is None:
                raise unprocessable("NO_SUCH_POLICY", f"policy {body['policy_name']} does not exist")
            sets.append("policy_name = ?")
            params.append(body["policy_name"])
        with self.store.transaction() as conn:
            conn.execute(f"UPDATE devices SET {', '.join(sets)}, updated_at = ? WHERE device_id = ?",
                         (*params, self.stamp(), device_id))
            self.record(EventType.DEVICE_UPDATED, actor=principal.actor, device_id=device_id, detail=body)
        return self.get_device(device_id)

    def retire_device(self, principal: Principal, device_id: str) -> dict:
        row = self.get_device_row(device_id)
        if row["status"] == "retired":
            return self.get_device(device_id)
        now = self.stamp()
        with self.store.transaction() as conn:
            conn.execute("UPDATE devices SET status = 'retired', connection = 'offline', updated_at = ? "
                         "WHERE device_id = ?", (now, device_id))
            conn.execute("UPDATE commands SET status = 'CANCELLED', finished_at = ?, updated_at = ?, "
                         "error_code = 'DEVICE_RETIRED' WHERE device_id = ? AND status IN ('QUEUED', 'SENT')",
                         (now, now, device_id))
            self.record(EventType.DEVICE_RETIRED, actor=principal.actor, device_id=device_id,
                        detail={"certificate": row["certificate_fingerprint"][:16]})
        return self.get_device(device_id)

    def report(self, device_id: str, kind: str) -> dict | None:
        row = self.store.one("SELECT * FROM reports WHERE device_id = ? AND kind = ?", (device_id, kind))
        if row is None:
            return None
        return {"received_at": row["received_at"], "document": json.loads(row["document"])}

    def device_status(self, device_id: str) -> dict:
        device = self.get_device(device_id)
        return {"device_id": device_id, "status": device["status"], "connection": device["connection"],
                "device_state": device["device_state"], "last_heartbeat_at": device["last_heartbeat_at"],
                "seconds_since_heartbeat": device.get("seconds_since_heartbeat"),
                "agent_version": device["agent_version"], "policy_version": device["policy_version"],
                "reported_policy_version": device["reported_policy_version"],
                "policy_current": device["policy_current"],
                "status_report": self.report(device_id, "status"), "compliance": self.report(device_id, "compliance")}

    def device_inventory(self, device_id: str) -> dict:
        self.get_device_row(device_id)
        return self.report(device_id, "inventory") or {"received_at": None, "document": None}

    def device_applications(self, device_id: str) -> dict:
        inventory = self.device_inventory(device_id)["document"] or {}
        catalog = {a["app_id"]: a for a in self.list_applications()}
        apps = []
        for item in inventory.get("windows_applications", []):
            entry = catalog.get(item["id"])
            apps.append({**item, "catalog": {"version": entry["version"], "status": entry["status"],
                                             "support": entry["support"]} if entry else None,
                         "update_available": bool(entry) and entry["version"] != item.get("version")
                         and entry["support"] == "SUPPORTED"})
        return {"device_id": device_id, "collected_at": inventory.get("collected_at"), "applications": apps}

    # --- commands --------------------------------------------------------------------------------------
    def _command_doc(self, row: dict) -> dict:
        return {"schema": commands.COMMAND_SCHEMA, "command_id": row["command_id"], "device_id": row["device_id"],
                "type": row["type"], "payload": json.loads(row["payload"]), "created_at": row["created_at"],
                "expires_at": row["expires_at"], "created_by": row["created_by"]}

    def _command_view(self, row: dict) -> dict:
        doc = self._command_doc(row)
        doc.pop("schema")
        payload = doc["payload"]
        if "manifest" in payload:          # keep listings compact
            payload = {**payload, "manifest": {"id": payload["manifest"]["id"],
                                               "version": payload["manifest"]["version"]}}
        return {**doc, "payload": payload, "status": row["status"], "sent_at": row["sent_at"],
                "acknowledged_at": row["acknowledged_at"], "started_at": row["started_at"],
                "finished_at": row["finished_at"], "result": json.loads(row["result"]) if row["result"] else None,
                "error": {"code": row["error_code"], "message": row["error_message"]} if row["error_code"] else None,
                "idempotency_key": row["idempotency_key"]}

    def _payload(self, ctype: commands.CommandType, device: dict, body: dict) -> dict:
        T = commands.CommandType
        app_id = body.get("application_id")
        if ctype in (T.INSTALL_APPLICATION, T.UPDATE_APPLICATION):
            app = self.store.one("SELECT * FROM applications WHERE app_id = ?", (app_id,)) \
                if isinstance(app_id, str) else None
            if app is None or app["deprecated"]:
                raise unprocessable("NOT_IN_CATALOG", f"{app_id} is not an active catalog application")
            if app["support"] != "SUPPORTED":
                raise unprocessable(app["support"], f"{app_id} cannot be installed on Boswas OS ({app['support']})")
            if app["status"] == "blocked":
                raise unprocessable("BLOCKED", f"{app_id} is blocked in the catalog")
            artifact = self.store.one("SELECT * FROM artifacts WHERE sha256 = ?", (app["installer_sha256"],))
            if artifact is None or not (self.artifacts_dir / artifact["sha256"]).is_file():
                raise unprocessable("ARTIFACT_MISSING", f"the installer of {app_id} is not stored")
            return {"application_id": app_id, "version": app["version"],
                    "installer": {"sha256": artifact["sha256"], "size": artifact["size"],
                                  "file_name": app["installer_file_name"] or artifact["file_name"]},
                    "manifest": json.loads(app["manifest"])}
        if ctype in (T.REMOVE_APPLICATION, T.LAUNCH_APPLICATION, T.STOP_APPLICATION, T.REPAIR_APPLICATION):
            return {"application_id": app_id}
        if ctype == T.REFRESH_INVENTORY:
            return {}
        if ctype == T.APPLY_POLICY:
            policy = self.current_policy(device["policy_name"])
            if policy is None:
                raise conflict("NO_POLICY", f"policy {device['policy_name']} has no published version")
            return {"policy_version": policy["version"]}
        return {"version": body.get("version")}

    def create_command(self, principal: Principal, device_id: str, body: object) -> tuple[dict, bool]:
        if not isinstance(body, dict):
            raise bad("INVALID_REQUEST", "a JSON object is required")
        unknown = set(body) - {"type", "application_id", "version", "ttl_seconds", "idempotency_key"}
        if unknown:
            raise bad("INVALID_REQUEST", f"unknown field {sorted(unknown)[0]!r} (commands are typed: type, "
                                         "application_id, version, ttl_seconds, idempotency_key)")
        try:
            ctype = commands.command_type(body.get("type"))
        except UnsupportedCommand as exc:
            raise bad("UNSUPPORTED_COMMAND", str(exc)) from exc
        device = self.get_device_row(device_id)
        if device["status"] != "active":
            raise ServiceError("DEVICE_RETIRED", "the device is retired", status=410)
        payload = self._payload(ctype, device, body)
        problems = commands.validate_payload(ctype, payload)
        if problems:
            raise bad("INVALID_PAYLOAD", problems[0], problems)
        policy = self.current_policy(device["policy_name"])
        if policy and ctype.value not in json.loads(policy["document"])["agent"]["allowed_commands"]:
            raise unprocessable("COMMAND_NOT_ALLOWED", f"{ctype.value} is not allowed by policy {policy['version']}")
        ttl = body.get("ttl_seconds", int(commands.DEFAULT_TTL.total_seconds()))
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not \
                commands.MIN_TTL.total_seconds() <= ttl <= commands.MAX_TTL.total_seconds():
            raise bad("INVALID_TTL", "ttl_seconds must be between 60 and 604800")
        key = body.get("idempotency_key")
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", key)):
            raise bad("INVALID_IDEMPOTENCY_KEY", "1-64 characters: letters, digits, '.', '_', ':' or '-'")
        fp = hashlib.sha256((ctype.value + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        created = self.now()
        with self.store.transaction() as conn:
            if key is not None:
                row = conn.execute("SELECT * FROM commands WHERE device_id = ? AND idempotency_key = ?",
                                   (device_id, key)).fetchone()
                if row is not None:
                    if row["fingerprint"] != fp:
                        raise conflict("IDEMPOTENCY_KEY_REUSED", "this idempotency key was used for another command")
                    return self._command_view(dict(row)), False
            row = conn.execute("SELECT * FROM commands WHERE device_id = ? AND fingerprint = ? AND status IN "
                               "('QUEUED', 'SENT', 'ACKNOWLEDGED', 'RUNNING') AND expires_at > ?",
                               (device_id, fp, self.stamp(created))).fetchone()
            if row is not None:
                return self._command_view(dict(row)), False          # the same operation is already pending
            row = {"command_id": str(uuid.uuid4()), "device_id": device_id, "type": ctype.value,
                   "payload": json.dumps(payload, sort_keys=True), "status": "QUEUED",
                   "created_at": self.stamp(created), "expires_at": self.stamp(created + timedelta(seconds=ttl)),
                   "created_by": principal.actor, "idempotency_key": key, "fingerprint": fp,
                   "updated_at": self.stamp(created)}
            conn.execute("INSERT INTO commands (command_id, device_id, type, payload, status, created_at, expires_at, "
                         "created_by, idempotency_key, fingerprint, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         tuple(row[k] for k in ("command_id", "device_id", "type", "payload", "status", "created_at",
                                                "expires_at", "created_by", "idempotency_key", "fingerprint",
                                                "updated_at")))
            self.record(EventType.COMMAND_CREATED, actor=principal.actor, device_id=device_id,
                        command_id=row["command_id"], application_id=payload.get("application_id"),
                        detail={"type": ctype.value, "expires_at": row["expires_at"],
                                "version": payload.get("version")})
        full = self.store.one("SELECT * FROM commands WHERE command_id = ?", (row["command_id"],))
        return self._command_view(full), True

    def list_commands(self, *, device_id: str | None = None, status: str | None = None, limit: int = 100) -> list:
        sql, params = "SELECT * FROM commands WHERE 1=1", []
        if device_id:
            sql += " AND device_id = ?"
            params.append(device_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, command_id LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        return [self._command_view(r) for r in self.store.query(sql, tuple(params))]

    def get_command(self, device_id: str, command_id: str) -> dict:
        row = self.store.one("SELECT * FROM commands WHERE command_id = ? AND device_id = ?", (command_id, device_id))
        if row is None:
            raise not_found("command")
        return self._command_view(row)

    def cancel_command(self, principal: Principal, device_id: str, command_id: str) -> dict:
        now = self.stamp()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM commands WHERE command_id = ? AND device_id = ?",
                               (command_id, device_id)).fetchone()
            if row is None:
                raise not_found("command")
            if row["status"] != "QUEUED":
                raise conflict("NOT_CANCELLABLE", f"only queued commands can be cancelled (this one is {row['status']})")
            conn.execute("UPDATE commands SET status = 'CANCELLED', finished_at = ?, updated_at = ? WHERE command_id = ?",
                         (now, now, command_id))
            self.record(EventType.COMMAND_CANCELLED, actor=principal.actor, device_id=device_id,
                        command_id=command_id, detail={"type": row["type"], "status": "CANCELLED"})
        return self.get_command(device_id, command_id)

    # --- artifacts and catalog -------------------------------------------------------------------
    def add_artifact(self, principal: Principal, upload: Path, sha256: str, size: int, file_name: str) -> dict:
        if not _FILE_RE.fullmatch(file_name or "") or file_name in (".", ".."):
            upload.unlink(missing_ok=True)
            raise bad("INVALID_FILE_NAME", "a plain file name is required")
        try:
            info = compat_installer.inspect(upload)
        except WinAppError as exc:
            upload.unlink(missing_ok=True)
            raise unprocessable("NOT_AN_INSTALLER", str(exc)) from exc
        target = self.artifacts_dir / sha256
        os.replace(upload, target)
        os.chmod(target, 0o640)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO artifacts (sha256, size, file_name, kind, machine, uploaded_at, uploaded_by) "
                         "VALUES (?,?,?,?,?,?,?) ON CONFLICT (sha256) DO UPDATE SET file_name = excluded.file_name",
                         (sha256, size, file_name, info.kind, info.machine, self.stamp(), principal.actor))
            self.record(EventType.ARTIFACT_UPLOADED, actor=principal.actor,
                        detail={"sha256": sha256, "size": size, "file_name": file_name, "kind": info.kind,
                                "architecture": info.machine})
        return self.store.one("SELECT * FROM artifacts WHERE sha256 = ?", (sha256,))

    def import_artifact_file(self, principal: Principal, source: Path, file_name: str | None = None) -> dict:
        """Copy a local installer into the artifact store (boswas-cp artifact add)."""
        self.artifacts_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        tmp = self.artifacts_dir / f".upload-{secrets.token_hex(8)}"
        digest, size = hashlib.sha256(), 0
        with open(source, "rb") as src, open(tmp, "wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                size += len(chunk)
                if size > self.max_artifact_bytes:
                    dst.close()
                    tmp.unlink(missing_ok=True)
                    raise ServiceError("TOO_LARGE", "installer larger than the upload limit", status=413)
                digest.update(chunk)
                dst.write(chunk)
        return self.add_artifact(principal, tmp, digest.hexdigest(), size, file_name or Path(source).name)

    def list_artifacts(self) -> list[dict]:
        return self.store.query("SELECT * FROM artifacts ORDER BY uploaded_at DESC")

    def upsert_application(self, principal: Principal, body: object) -> dict:
        if not isinstance(body, dict) or set(body) - {"manifest", "installer_file_name"} or "manifest" not in body:
            raise bad("INVALID_REQUEST", "expected {\"manifest\": {...}, \"installer_file_name\": optional}")
        doc = body["manifest"]
        problems = compat_manifest.validate(doc)
        if problems:
            raise unprocessable("INVALID_MANIFEST", problems[0], problems)
        manifest = compat_manifest.from_document(doc)
        sha = manifest.installer.sha256
        if not sha:
            raise unprocessable("INSTALLER_NOT_PINNED", "catalog manifests must pin their installer (installer.sha256)")
        artifact = self.store.one("SELECT * FROM artifacts WHERE sha256 = ?", (sha,))
        if artifact is None:
            raise unprocessable("ARTIFACT_MISSING", "upload the installer first (POST /api/v1/artifacts)")
        if manifest.architecture == "x86_64" and artifact["machine"] not in (None, "x86_64"):
            raise unprocessable("ARCHITECTURE_MISMATCH",
                                f"the installer is a {artifact['machine']} Windows program, but the manifest declares "
                                "x86_64; Boswas OS runs 64-bit Windows applications only")
        if manifest.architecture != "x86_64":
            support = "UNSUPPORTED_ARCHITECTURE"
        elif manifest.dependencies or manifest.winetricks:
            support = "MISSING_DEPENDENCIES"
        else:
            support = "SUPPORTED"
        file_name = body.get("installer_file_name") or manifest.installer.file_name or artifact["file_name"]
        if not _FILE_RE.fullmatch(file_name or ""):
            raise bad("INVALID_FILE_NAME", "a plain installer file name is required")
        now = self.stamp()
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO applications (app_id, name, publisher, version, architecture, status, support, "
                         "manifest, installer_sha256, installer_file_name, created_at, updated_at, updated_by) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (app_id) DO UPDATE SET name = excluded.name, "
                         "publisher = excluded.publisher, version = excluded.version, architecture = "
                         "excluded.architecture, status = excluded.status, support = excluded.support, manifest = "
                         "excluded.manifest, installer_sha256 = excluded.installer_sha256, installer_file_name = "
                         "excluded.installer_file_name, deprecated = 0, updated_at = excluded.updated_at, "
                         "updated_by = excluded.updated_by",
                         (manifest.id, manifest.name, manifest.publisher, manifest.version, manifest.architecture,
                          manifest.status, support, json.dumps(doc, sort_keys=True), sha, file_name, now, now,
                          principal.actor))
            self.record(EventType.CATALOG_UPDATED, actor=principal.actor, application_id=manifest.id,
                        detail={"version": manifest.version, "status": manifest.status, "support": support,
                                "architecture": manifest.architecture, "installer": sha})
        return self.get_application(manifest.id)

    def _application_view(self, row: dict) -> dict:
        return {"app_id": row["app_id"], "name": row["name"], "publisher": row["publisher"], "version": row["version"],
                "architecture": row["architecture"], "architecture_label": "x86_64 / 64-bit" if row["architecture"] ==
                "x86_64" else "x86 / 32-bit (not supported)", "status": row["status"], "support": row["support"],
                "installable": row["support"] == "SUPPORTED" and row["status"] != "blocked" and not row["deprecated"],
                "installer": {"sha256": row["installer_sha256"], "file_name": row["installer_file_name"]},
                "manifest": json.loads(row["manifest"]), "deprecated": bool(row["deprecated"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"], "updated_by": row["updated_by"]}

    def list_applications(self) -> list[dict]:
        return [self._application_view(r) for r in self.store.query("SELECT * FROM applications ORDER BY name")]

    def get_application(self, app_id: str) -> dict:
        row = self.store.one("SELECT * FROM applications WHERE app_id = ?", (app_id,))
        if row is None:
            raise not_found("application")
        return self._application_view(row)

    def deprecate_application(self, principal: Principal, app_id: str) -> dict:
        self.get_application(app_id)
        with self.store.transaction() as conn:
            conn.execute("UPDATE applications SET deprecated = 1, updated_at = ?, updated_by = ? WHERE app_id = ?",
                         (self.stamp(), principal.actor, app_id))
            self.record(EventType.CATALOG_UPDATED, actor=principal.actor, application_id=app_id,
                        detail={"action": "deprecated"})
        return self.get_application(app_id)

    # --- policies --------------------------------------------------------------------------------------
    def current_policy(self, name: str) -> dict | None:
        return self.store.one("SELECT * FROM policies WHERE name = ? ORDER BY sequence DESC LIMIT 1", (name,))

    @staticmethod
    def _heartbeat_seconds(policy: dict | None) -> int:
        if not policy:
            return 300
        return int(json.loads(policy["document"])["agent"]["heartbeat_seconds"])

    def publish_policy(self, principal: Principal, body: object) -> dict:
        if not isinstance(body, dict) or set(body) - {"name", "description", "compat", "agent", "updates"}:
            raise bad("INVALID_REQUEST", "a policy has name, description, compat, agent and updates")
        name = body.get("name")
        if not isinstance(name, str) or not policydoc.NAME_RE.fullmatch(name):
            raise bad("INVALID_POLICY_NAME", "policy names are lower-case identifiers (max 32)")
        with self.store.transaction() as conn:
            last = conn.execute("SELECT MAX(sequence) AS s FROM policies WHERE name = ?", (name,)).fetchone()["s"] or 0
            doc = {"schema": policydoc.POLICY_SCHEMA, "name": name, "sequence": last + 1, "version": f"{name}-{last + 1}",
                   "issued_at": self.stamp(), "description": body.get("description", ""),
                   "compat": body.get("compat"), "agent": body.get("agent"), "updates": body.get("updates")}
            problems = policydoc.validate_policy(doc)
            if problems:
                raise unprocessable("INVALID_POLICY", problems[0], problems)
            try:
                envelope = policydoc.make_envelope(doc, self.pki.policy_key, self.pki.policy_public_key())
            except PolicyVerificationError as exc:
                raise unprocessable("INVALID_POLICY", str(exc)) from exc
            conn.execute("INSERT INTO policies (name, sequence, version, document, envelope, created_at, created_by) "
                         "VALUES (?,?,?,?,?,?,?)", (name, doc["sequence"], doc["version"],
                                                    json.dumps(doc, sort_keys=True), json.dumps(envelope),
                                                    doc["issued_at"], principal.actor))
            self.record(EventType.POLICY_UPDATED, actor=principal.actor,
                        detail={"version": doc["version"], "unlisted_apps": doc["compat"]["unlisted_apps"],
                                "allowed_statuses": doc["compat"]["allowed_statuses"]})
        return self.get_policy(name)

    def _policy_view(self, row: dict) -> dict:
        return {"name": row["name"], "version": row["version"], "sequence": row["sequence"],
                "created_at": row["created_at"], "created_by": row["created_by"], "document": json.loads(row["document"])}

    def list_policies(self) -> list[dict]:
        rows = self.store.query("SELECT p.* FROM policies p JOIN (SELECT name, MAX(sequence) AS s FROM policies GROUP BY "
                                "name) m ON p.name = m.name AND p.sequence = m.s ORDER BY p.name")
        out = []
        for row in rows:
            view = self._policy_view(row)
            view["devices"] = self.store.one("SELECT COUNT(*) AS n FROM devices WHERE policy_name = ? AND status = "
                                             "'active'", (row["name"],))["n"]
            out.append(view)
        return out

    def get_policy(self, name: str) -> dict:
        rows = self.store.query("SELECT * FROM policies WHERE name = ? ORDER BY sequence DESC", (name,))
        if not rows:
            raise not_found("policy")
        current = self._policy_view(rows[0])
        current["history"] = [{"version": r["version"], "created_at": r["created_at"], "created_by": r["created_by"]}
                              for r in rows]
        return current

    def ensure_default_policy(self, actor: str = "system:init") -> None:
        if self.current_policy(DEFAULT_POLICY) is None:
            doc = policydoc.default_policy()
            self.publish_policy(Principal(actor, "admin"), {k: doc[k] for k in ("name", "description", "compat",
                                                                                  "agent", "updates")})

    # --- dashboard and maintenance -----------------------------------------------------------------------
    def summary(self) -> dict:
        devices = self.store.query("SELECT status, connection, COUNT(*) AS n FROM devices GROUP BY status, connection")
        count = {"total": 0, "active": 0, "online": 0, "offline": 0, "never_connected": 0, "retired": 0}
        for row in devices:
            count["total"] += row["n"]
            if row["status"] == "retired":
                count["retired"] += row["n"]
                continue
            count["active"] += row["n"]
            count[{"online": "online", "offline": "offline", "never": "never_connected"}[row["connection"]]] += row["n"]
        running = 0
        for row in self.store.query("SELECT r.document FROM reports r JOIN devices d ON d.device_id = r.device_id "
                                    "WHERE r.kind = 'inventory' AND d.status = 'active'"):
            running += sum(int(a.get("running") or 0) for a in json.loads(row["document"]).get("windows_applications", []))
        since = self.stamp(self.now() - timedelta(hours=24))
        cmd = {r["status"]: r["n"] for r in self.store.query("SELECT status, COUNT(*) AS n FROM commands GROUP BY status")}
        failed_24h = self.store.one("SELECT COUNT(*) AS n FROM commands WHERE status IN ('FAILED', 'EXPIRED') AND "
                                    "updated_at >= ?", (since,))["n"]
        outdated = unreported = 0
        for row in self.store.query("SELECT policy_name, reported_policy_version FROM devices WHERE status = 'active'"):
            policy = self.current_policy(row["policy_name"])
            if row["reported_policy_version"] is None:
                unreported += 1
            elif policy and row["reported_policy_version"] != policy["version"]:
                outdated += 1
        apps = self.store.query("SELECT support, COUNT(*) AS n FROM applications WHERE deprecated = 0 GROUP BY support")
        security = [self._event(r) for r in self.store.query(
            "SELECT * FROM events WHERE type IN ('SECURITY_EVENT', 'APPLICATION_BLOCKED') ORDER BY id DESC LIMIT 10")]
        return {"devices": count,
                "applications": {"catalog": sum(r["n"] for r in apps),
                                 "supported": sum(r["n"] for r in apps if r["support"] == "SUPPORTED"),
                                 "running": running},
                "commands": {"pending": sum(cmd.get(s, 0) for s in ACTIVE_COMMANDS), "failed_24h": failed_24h,
                             "by_status": cmd},
                "policies": {"count": len(self.list_policies()), "devices_outdated": outdated,
                             "devices_unreported": unreported},
                "security_events": security, "generated_at": self.stamp()}

    def sweep(self) -> dict:
        """Mark silent devices offline and expire undelivered commands."""
        now = self.now()
        offline = expired = 0
        for row in self.store.query("SELECT * FROM devices WHERE status = 'active' AND connection = 'online'"):
            limit = max(self.offline_floor, 3 * self._heartbeat_seconds(self.current_policy(row["policy_name"])))
            if row["last_heartbeat_at"] and (now - self.parse(row["last_heartbeat_at"])).total_seconds() > limit:
                with self.store.transaction() as conn:
                    conn.execute("UPDATE devices SET connection = 'offline', updated_at = ? WHERE device_id = ?",
                                 (self.stamp(now), row["device_id"]))
                    self.record(EventType.DEVICE_OFFLINE, actor="system:sweeper", device_id=row["device_id"],
                                detail={"last_heartbeat_at": row["last_heartbeat_at"]})
                offline += 1
        stamp = self.stamp(now)
        late = self.stamp(now - timedelta(hours=24))
        for row in self.store.query("SELECT * FROM commands WHERE (status IN ('QUEUED', 'SENT', 'ACKNOWLEDGED') AND "
                                    "expires_at <= ?) OR (status = 'RUNNING' AND expires_at <= ?)", (stamp, late)):
            status, code = ("EXPIRED", "COMMAND_EXPIRED") if row["status"] != "RUNNING" else ("FAILED", "NO_RESULT")
            with self.store.transaction() as conn:
                cur = conn.execute("UPDATE commands SET status = ?, finished_at = ?, updated_at = ?, error_code = ?, "
                                   "error_message = ? WHERE command_id = ? AND status = ?",
                                   (status, stamp, stamp, code, "expired before the device reported a result",
                                    row["command_id"], row["status"]))
                if cur.rowcount:
                    self.record(EventType.COMMAND_FAILED, actor="system:sweeper", device_id=row["device_id"],
                                command_id=row["command_id"], detail={"type": row["type"], "status": status})
                    expired += 1
        return {"offline": offline, "expired": expired}
