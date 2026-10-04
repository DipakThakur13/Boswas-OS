"""boswas-device-agent: the device agent service (root, systemd).

Responsibilities: the persistent device identity, device state, inventory
and posture, the local management API, enrollment, the conversation with the
Control Plane (heartbeat, policy, typed commands, results, events) with an
outbox for connectivity loss, and execution of typed commands.

What it never does: run a shell command, script or program named by the
Control Plane; run Windows software (users' session agents do, through
boswas-winapp); send anything outside the privacy allowlists; let the
Control Plane's availability affect local work. A device without a Control
Plane (or with an unreachable one) keeps every local function.

The main loop sleeps on an event between scheduled tasks (inventory,
runtime check, sync); retries use bounded exponential back-off.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from boswas_compat import policy as compat_policy

from . import __version__, privacy
from .client import HttpsControlPlaneClient
from .commands import (CommandOutcome, CommandStatus, format_time, parse_command, result_document, utc_now)
from .config import AgentConfig
from .credentials import FileCredentialStore
from .errors import (AgentError, CommandExpired, ConnectivityError, ControlPlaneRejected, CredentialError,
                     IdentityError, InvalidCommand, PolicyVerificationError, SessionUnavailable, UnsupportedCommand)
from .executor import CommandExecutor, Services
from .identity import DeviceIdentity, IdentityStore
from .interfaces import ControlPlaneClient, OfflineControlPlaneClient
from .inventory import INVENTORY_SCHEMA, SystemInventory, content_digest
from .ledger import CommandLedger, EventLog, clean
from .localapi import ApiError, ApiServer, Operation, Param
from .models import (DEVICE_EVENT_TYPES, SECURITY_CHECK_IDS, CheckResult, ComplianceReport, ComplianceState,
                     ComplianceSummary, ConnectionState, DeviceState, EnrollmentRequest, EventType, HardwareFacts,
                     Heartbeat, OsInfo, StatusReport, UpdateStatus)
from .outbox import Backoff, Outbox
from .paths import AgentPaths
from .policy_store import ManagedCatalog, PolicyStore
from .sessions import SessionRegistry
from .state import Facts, StateMachine, evaluate
from .storage import ensure_dir, read_bytes, read_json, write_json

log = logging.getLogger("boswas-device-agent")

STATUS_SCHEMA = "boswas-agent-status/1"
OFFLINE_AFTER_FAILURES = 3
RUNTIME_CHECK_SECONDS = 600
MAX_WAIT = 60
BOSWAS = "/usr/bin/boswas"
REPORTED_CHECKS = SECURITY_CHECK_IDS + ("updates", "agent", "enrollment")
_TOKEN_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")


REPORTABLE_EVENTS = frozenset(t.value for t in DEVICE_EVENT_TYPES)


def _token_problem(value: str) -> str | None:
    if not 16 <= len(value) <= 256 or not set(value) <= _TOKEN_CHARS:
        return "an enrollment token is 16-256 URL-safe characters"
    return None


class _ReportingEvents:
    """The executor's event log: records locally and reports to the Control Plane."""

    def __init__(self, agent: "Agent"):
        self.agent = agent

    def append(self, event_type: str, detail: str = "", *, application_id: str | None = None, source: str = "local",
               command_id: str | None = None) -> dict:
        return self.agent.report_event(event_type, detail, application_id=application_id, source=source,
                                       command_id=command_id)

    def recent(self, limit: int = 100) -> list[dict]:
        return self.agent.events.recent(limit)


class Agent:
    def __init__(self, paths: AgentPaths = AgentPaths(), *, run=subprocess.run, client_factory=None,
                 inventory: SystemInventory | None = None, sessions: SessionRegistry | None = None,
                 clock=time.time, is_root: bool | None = None, backoff: Backoff | None = None):
        self.paths, self._run, self.clock = paths, run, clock
        self.is_root = (os.geteuid() == 0) if is_root is None else is_root
        self.client_factory = client_factory
        self.machine = StateMachine(clock)
        self.identity_store = IdentityStore(paths)
        self.credentials = FileCredentialStore(paths.credentials)
        self.policy = PolicyStore(paths, self.credentials)
        self.catalog = ManagedCatalog(paths)
        self.events = EventLog(paths.events)
        self.ledger = CommandLedger(paths.ledger)
        self.outbox = Outbox(paths.outbox)
        self.sessions = sessions or SessionRegistry()
        self.inventory_source = inventory or SystemInventory(paths, run=run)
        self.backoff = backoff or Backoff()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.work = threading.Event()
        self.stopping = threading.Event()
        self.identity: DeviceIdentity | None = None
        self.identity_problem: str | None = None
        self.config = AgentConfig()
        self._conf_stamp = None
        self.local = self._load_local_state()
        self.consecutive_failures = 0
        self.last_contact: str | None = None
        self.last_attempt: str | None = None
        self.last_error: str | None = None
        self.revoked = False
        self.runtime: dict | None = None
        self.inventory_ok: bool | None = None
        self.compliance_failed = False
        self.updating_until = 0.0
        self.next_sync = 0.0
        self.next_inventory = 0.0
        self.next_runtime = 0.0
        self.heartbeat_seconds: int | None = None
        self.executor = CommandExecutor(Services(
            artifacts_dir=paths.artifacts, policy=self.policy, catalog=self.catalog, events=_ReportingEvents(self),
            dispatch=lambda op, params, timeout: self.sessions.pick().dispatch(op, params, timeout),
            client=self.client, compat_policy=lambda: compat_policy.Policy.load(),
            refresh_inventory=lambda: self.refresh_inventory(force=True),
            agent_version=self.installed_agent_version, start_agent_update=self.start_agent_update,
            update_policy=lambda: self.config.values.get("UPDATE_POLICY", "security-only")))
        self.api = ApiServer(paths.socket, self.operations(), mode=0o666)
        self._worker: threading.Thread | None = None

    # --- local state ------------------------------------------------------------------------
    @property
    def _local_path(self) -> Path:
        return self.paths.state_dir / "agent-state.json"

    def _load_local_state(self) -> dict:
        doc = read_json(self.paths.state_dir / "agent-state.json")
        doc = doc if isinstance(doc, dict) else {}
        doc.setdefault("maintenance", False)
        doc.setdefault("inventory_revision", 0)
        doc.setdefault("inventory_digest", None)
        doc.setdefault("enrollment", {"state": "unenrolled"})
        return doc

    def _save_local_state(self) -> None:
        ensure_dir(self.paths.state_dir, 0o755)
        write_json(self._local_path, self.local, 0o600)

    @property
    def device_id(self) -> str | None:
        return self.identity.device_id if self.identity else None

    @property
    def enrolled(self) -> bool:
        return self.local["enrollment"].get("state") == "enrolled" and self.credentials.has_credential()

    # --- configuration and identity -----------------------------------------------------------------
    def load_config(self) -> None:
        self.config = AgentConfig.load(self.paths.device_conf, check_owner=0 if self.is_root else None)
        try:
            st = self.paths.device_conf.stat()
            self._conf_stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._conf_stamp = None
        level = {"error": logging.ERROR, "warning": logging.WARNING, "info": logging.INFO,
                 "debug": logging.DEBUG}[self.config.values.get("LOG_LEVEL", "info")]
        log.setLevel(level)
        for problem in self.config.problems:
            log.error("device.conf: %s", problem)

    def _config_changed(self) -> bool:
        try:
            st = self.paths.device_conf.stat()
            return (st.st_mtime_ns, st.st_size) != self._conf_stamp
        except OSError:
            return self._conf_stamp is not None

    def load_identity(self) -> None:
        try:
            self.identity = self.identity_store.ensure()
            self.identity_problem = None
        except (IdentityError, OSError) as exc:
            self.identity, self.identity_problem = None, str(exc)
            log.error("device identity: %s", exc)

    def initialize(self) -> None:
        self.load_config()
        self.load_identity()
        for directory, mode in ((self.paths.state_dir, 0o755), (self.paths.compat_dir, 0o755),
                                (self.paths.managed_manifests, 0o755)):
            try:
                ensure_dir(directory, mode)
            except OSError as exc:
                log.error("cannot prepare %s: %s", directory, exc)
        self._recover_interrupted()
        self.evaluate()

    # --- Control Plane client ---------------------------------------------------------------------
    def should_sync(self) -> bool:
        return (self.config.valid and self.config.agent_enabled and self.config.managed and self.enrolled
                and self.identity is not None and not self.revoked)

    def client(self) -> ControlPlaneClient:
        if self.client_factory is not None:
            return self.client_factory(self)
        if not self.should_sync():
            return OfflineControlPlaneClient()
        return HttpsControlPlaneClient(self.config.control_plane_url, self.credentials.tls_context(self.ca_path()),
                                       self.device_id)

    def ca_path(self) -> Path:
        """The pinned Control Plane CA (an absolute path on the device)."""
        return self.paths.root / self.config.values["CONTROL_PLANE_CA"].lstrip("/")

    def connection_state(self) -> ConnectionState:
        if not self.config.managed or not self.config.agent_enabled:
            return ConnectionState.STANDALONE
        if not self.enrolled:
            return ConnectionState.UNENROLLED
        if self.revoked:
            return ConnectionState.REVOKED
        if self.consecutive_failures >= OFFLINE_AFTER_FAILURES or (self.consecutive_failures and not self.last_contact):
            return ConnectionState.OFFLINE
        return ConnectionState.CONNECTED if self.last_contact else ConnectionState.OFFLINE

    # --- outgoing messages -------------------------------------------------------------------------
    def queue(self, kind: str, document: dict) -> None:
        privacy.check(document)
        self.outbox.put(kind, document)

    def flush_outbox(self, client: ControlPlaneClient) -> None:
        for item_id, kind, doc in self.outbox.items():
            try:
                client.send(kind, doc)
            except ControlPlaneRejected as exc:
                # The Control Plane will never accept it (e.g. a result for a
                # cancelled command): drop it rather than retry forever.
                log.warning("Control Plane refused queued %s: %s", kind, exc)
            self.outbox.ack(item_id)

    def report_event(self, event_type: EventType | str, detail: str = "", *, application_id: str | None = None,
                     source: str = "local", command_id: str | None = None) -> dict:
        """Record an event locally; tell the Control Plane if it is a device-reportable type."""
        etype = event_type.value if isinstance(event_type, EventType) else str(event_type)
        event = self.events.append(etype, detail, application_id=application_id, source=source,
                                   command_id=command_id)
        if etype in REPORTABLE_EVENTS and self.enrolled and self.device_id:
            try:
                self.queue("event", {"schema": "boswas-device-event/1", "device_id": self.device_id,
                                     "events": [event]})
            except (AgentError, OSError, ValueError) as exc:
                log.warning("event not queued: %s", exc)
        return event

    # --- inventory, posture, runtime ------------------------------------------------------------------
    def inventory_policy(self) -> str:
        return self.config.values.get("INVENTORY_POLICY", "standard")

    def refresh_inventory(self, force: bool = False) -> int:
        """Collect the inventory locally; queue it for the Control Plane when it changed (or when forced)."""
        try:
            doc = self.inventory_source.collect("standard")
            self.inventory_ok = True
        except Exception as exc:          # a broken collector must not stop the agent
            self.inventory_ok = False
            log.error("inventory collection failed: %s", exc)
            return int(self.local["inventory_revision"])
        digest = content_digest(doc)
        changed = digest != self.local.get("inventory_digest")
        with self.lock:
            if changed:
                self.local["inventory_revision"] = int(self.local["inventory_revision"]) + 1
                self.local["inventory_digest"] = digest
                self._save_local_state()
            revision = int(self.local["inventory_revision"])
        full = {"schema": INVENTORY_SCHEMA, "device_id": self.device_id or "", "revision": revision,
                "collected_at": format_time(utc_now()), **doc}
        try:
            ensure_dir(self.paths.state_dir, 0o755)
            write_json(self.paths.inventory, full, 0o644)
        except OSError as exc:
            log.error("cannot store the inventory: %s", exc)
        policy = self.inventory_policy()
        if self.enrolled and self.device_id and policy != "off" and (changed or force):
            outgoing = dict(full)
            outgoing["policy"] = policy
            if policy == "minimal":
                outgoing.pop("hardware", None)
                outgoing.pop("storage", None)
            self.queue("inventory", outgoing)
        if self.config.values.get("TELEMETRY_POLICY") == "security":
            self.report_posture(doc)
        return revision

    def report_posture(self, inventory: dict) -> None:
        try:
            proc = self._run([BOSWAS, "--json", "status"], capture_output=True, text=True, timeout=120, check=False)
            doc = json.loads(proc.stdout)
            checks = [c for c in doc["checks"] if isinstance(c, dict) and c.get("id") in REPORTED_CHECKS]
            local = doc["compliance"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            return
        summary = ComplianceSummary(ComplianceState.from_local_assessment(local.get("state")),
                                    int(local.get("pass", 0)), int(local.get("warn", 0)), int(local.get("fail", 0)),
                                    int(local.get("unknown", 0)))
        self.compliance_failed = summary.failed > 0
        if not (self.enrolled and self.device_id):
            return
        results = tuple(CheckResult(c["id"], c.get("status", "UNKNOWN") if c.get("status") in
                                    ("PASS", "WARN", "FAIL", "INFO", "UNKNOWN") else "UNKNOWN",
                                    c.get("scored") is not False) for c in checks)
        now = format_time(utc_now())
        self.queue("compliance", ComplianceReport(self.device_id, self.policy.version(), summary, results,
                                                  now).to_dict())
        os_doc = inventory.get("os", {})
        uptime = 0
        raw = read_bytes(self.paths.root / "proc/uptime", 4096)
        if raw:
            try:
                uptime = int(float(raw.split()[0]))
            except (ValueError, IndexError):
                uptime = 0
        channel = "unknown"
        update_conf = read_bytes(self.paths.root / "etc/boswas/update.conf", 65536)
        if update_conf:
            from .config import parse
            channel = parse(update_conf.decode("utf-8", errors="replace")).get("CHANNEL") or "unknown"
        security = {r.id: r.status for r in results if r.id in SECURITY_CHECK_IDS}
        report = StatusReport(self.device_id, __version__,
                              OsInfo(os_doc.get("name") or "Boswas OS", os_doc.get("version") or "unknown",
                                     os_doc.get("version_id") or "unknown", os_doc.get("build_id"),
                                     os_doc.get("debian_version"), os_doc.get("kernel") or "unknown"),
                              uptime, summary, self.policy.version(), UpdateStatus(channel=clean(channel, 20)),
                              security, now)
        self.queue("status", report.to_dict())

    def check_runtime(self) -> None:
        try:
            proc = self._run(["/usr/bin/boswas-winapp", "--json", "runtime"], capture_output=True, text=True,
                             timeout=60, check=False)
            doc = json.loads(proc.stdout)
            self.runtime = doc if isinstance(doc, dict) else None
        except (OSError, subprocess.SubprocessError, ValueError):
            self.runtime = None

    # --- synchronisation -----------------------------------------------------------------------
    def interval(self, key: str, config_key: str) -> int:
        settings = self.policy.agent_settings() or {}
        value = settings.get(key)
        return int(value) if isinstance(value, int) else self.config.number(config_key)

    def sync(self) -> None:
        now = self.clock()
        self.last_attempt = format_time(utc_now())
        try:
            client = self.client()
            heartbeat = Heartbeat(device_id=self.device_id, agent_version=__version__,
                                  state=self.machine.state.value, policy_version=self.policy.version(),
                                  inventory_revision=int(self.local["inventory_revision"]) or None,
                                  pending_results=self.outbox.counts()["result"],
                                  sent_at=format_time(utc_now())).to_dict()
            privacy.check(heartbeat)
            response = client.heartbeat(heartbeat)
            self._contact_ok()
            if response.policy_version_available and response.policy_version_available != self.policy.version():
                self.fetch_policy(client)
            if response.inventory_requested:
                self.refresh_inventory(force=True)
            if self.config.remote_commands and not self.local["maintenance"]:
                self.fetch_commands(client)
            self.flush_outbox(client)
            self.heartbeat_seconds = response.next_heartbeat_seconds
            self.next_sync = now + min(response.next_heartbeat_seconds,
                                       self.interval("heartbeat_seconds", "HEARTBEAT_INTERVAL"))
            if self.policy.version() != heartbeat["policy_version"]:
                self.next_sync = now        # a new policy was applied: report its version right away
        except ConnectivityError as exc:
            self._contact_failed(str(exc))
            self.next_sync = now + self.backoff.failure()
        except ControlPlaneRejected as exc:
            if exc.status in (401, 403, 404, 410) and exc.code in ("DEVICE_REVOKED", "DEVICE_RETIRED",
                                                                    "UNKNOWN_DEVICE", "CERTIFICATE_REVOKED"):
                self.revoked = True
                self.last_error = f"the Control Plane no longer accepts this device ({exc.code})"
                self.report_event(EventType.SECURITY_EVENT, self.last_error)
            else:
                self._contact_failed(str(exc))
            self.next_sync = now + self.backoff.failure()
        except (CredentialError, OSError) as exc:
            self._contact_failed(f"{type(exc).__name__}: {exc}")
            self.next_sync = now + self.backoff.failure()

    def _contact_ok(self) -> None:
        if self.consecutive_failures >= OFFLINE_AFTER_FAILURES:
            log.info("Control Plane reachable again")
        self.consecutive_failures = 0
        self.backoff.success()
        self.last_contact = format_time(utc_now())
        self.last_error = None

    def _contact_failed(self, reason: str) -> None:
        self.consecutive_failures += 1
        self.last_error = clean(reason, 300)
        if self.consecutive_failures == OFFLINE_AFTER_FAILURES:
            log.warning("Control Plane unreachable (%s); continuing offline", self.last_error)

    def fetch_policy(self, client: ControlPlaneClient) -> None:
        envelope = client.fetch_policy()
        if envelope is None:
            return
        try:
            doc, changed = self.policy.apply(envelope)
        except PolicyVerificationError as exc:
            self.report_event(EventType.SECURITY_EVENT, f"received policy rejected; current policy kept: {exc}")
            return
        if changed:
            self.report_event(EventType.POLICY_UPDATED, f"policy {doc['version']} applied")

    def fetch_commands(self, client: ControlPlaneClient) -> None:
        received = False
        for doc in client.fetch_commands():
            command_id = doc.get("command_id") if isinstance(doc.get("command_id"), str) else None
            if command_id is None or len(command_id) > 64:
                continue
            known = self.ledger.get(command_id)
            if known is not None:
                if known.get("status") in ("SUCCEEDED", "FAILED", "EXPIRED") and known.get("result"):
                    self.queue("result", known["result"])        # the result was lost: send it again
                continue
            try:
                command = parse_command(doc, device_id=self.device_id)
            except CommandExpired as exc:
                self._finish(command_id, str(doc.get("type", ""))[:40],
                             CommandOutcome(CommandStatus.EXPIRED, {}, "COMMAND_EXPIRED", str(exc)))
                continue
            except UnsupportedCommand as exc:          # also InvalidCommand
                self._finish(command_id, str(doc.get("type", ""))[:40], CommandOutcome.failed(exc.code, str(exc)))
                self.report_event(EventType.SECURITY_EVENT, f"rejected command {command_id}: {exc}",
                                  source="remote", command_id=command_id)
                continue
            self.ledger.record(command_id, {"status": CommandStatus.ACKNOWLEDGED.value, "type": command.type.value,
                                            "application_id": command.application_id,
                                            "received_at": format_time(utc_now()), "document": command.to_dict(),
                                            "created_by": command.created_by})
            try:
                client.acknowledge(command_id, CommandStatus.ACKNOWLEDGED.value)
            except (ConnectivityError, ControlPlaneRejected):
                pass
            received = True
        if received:
            self.work.set()

    # --- command execution ----------------------------------------------------------------------
    def _finish(self, command_id: str, ctype: str, outcome: CommandOutcome) -> None:
        doc = result_document(command_id, self.device_id or "", outcome)
        self.ledger.record(command_id, {"status": outcome.status.value, "type": ctype, "result": doc,
                                        "finished_at": doc["reported_at"], "error_code": outcome.error_code})
        try:
            self.queue("result", doc)
        except (AgentError, OSError, ValueError) as exc:
            log.error("result for %s not queued: %s", command_id, exc)
        self.wake.set()

    def process_pending(self) -> None:
        for entry in self.ledger.with_status(CommandStatus.ACKNOWLEDGED.value):
            if self.stopping.is_set() or self.local["maintenance"]:
                return
            command_id = entry["command_id"]
            try:
                command = parse_command(entry.get("document"), device_id=self.device_id)
            except CommandExpired as exc:
                self._finish(command_id, entry.get("type", ""),
                             CommandOutcome(CommandStatus.EXPIRED, {}, "COMMAND_EXPIRED", str(exc)))
                continue
            except UnsupportedCommand as exc:
                self._finish(command_id, entry.get("type", ""), CommandOutcome.failed(exc.code, str(exc)))
                continue
            self.ledger.record(command_id, {"status": CommandStatus.RUNNING.value})
            try:
                self.client().acknowledge(command_id, CommandStatus.RUNNING.value)
            except (AgentError, OSError):
                pass
            log.info("executing %s %s %s", command.type.value, command_id, command.application_id or "")
            try:
                outcome = self.executor.execute(command)
            except (SessionUnavailable, ConnectivityError) as exc:
                # Not possible right now (nobody logged in, or offline): try
                # again later, until the command expires.
                self.ledger.record(command_id, {"status": CommandStatus.ACKNOWLEDGED.value,
                                                "waiting": clean(exc, 200)})
                continue
            self._finish(command_id, command.type.value, outcome)

    def _recover_interrupted(self) -> None:
        """Commands that were running when the agent stopped."""
        for entry in self.ledger.with_status(CommandStatus.RUNNING.value):
            if entry.get("type") == "UPDATE_AGENT":
                wanted = ((entry.get("document") or {}).get("payload") or {}).get("version")
                if wanted and wanted == self.installed_agent_version():
                    self._finish(entry["command_id"], "UPDATE_AGENT", CommandOutcome.ok(agent_version=wanted))
                    continue
            self._finish(entry["command_id"], entry.get("type", ""),
                         CommandOutcome.failed("INTERRUPTED", "the agent stopped while the command was running"))

    def _worker_loop(self) -> None:
        while not self.stopping.is_set():
            self.work.wait(MAX_WAIT)
            self.work.clear()
            if self.stopping.is_set():
                return
            try:
                self.process_pending()
            except Exception as exc:          # keep the worker alive
                log.exception("command processing failed: %s", exc)

    # --- agent updates -------------------------------------------------------------------------
    def installed_agent_version(self) -> str:
        try:
            proc = self._run(["dpkg-query", "-W", "-f=${Version}", "boswas-device-agent"], capture_output=True,
                             text=True, timeout=30, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return __version__

    def start_agent_update(self, version: str) -> None:
        proc = self._run(["systemd-escape", "--template=boswas-agent-update@.service", version], capture_output=True,
                         text=True, timeout=30, check=False)
        unit = proc.stdout.strip()
        if proc.returncode != 0 or not unit.startswith("boswas-agent-update@") or not unit.endswith(".service"):
            raise AgentError("cannot name the update unit", code="UPDATE_FAILED")
        started = self._run(["systemctl", "start", "--no-block", unit], capture_output=True, text=True, timeout=60,
                            check=False)
        if started.returncode != 0:
            raise AgentError("the update could not be started", code="UPDATE_FAILED")
        self.updating_until = self.clock() + 1800

    # --- state -----------------------------------------------------------------------------------
    def facts(self) -> Facts:
        problems = list(self.config.problems)
        if self.identity_problem:
            problems.insert(0, self.identity_problem)
        return Facts(identity_ok=self.identity is not None, config_ok=self.config.valid,
                     maintenance=bool(self.local["maintenance"]), updating=self.clock() < self.updating_until,
                     runtime_healthy=None if self.runtime is None else self.runtime.get("healthy") is True,
                     inventory_ok=self.inventory_ok, compliance_failed=self.compliance_failed,
                     offline=self.connection_state() == ConnectionState.OFFLINE, problems=problems)

    def evaluate(self) -> None:
        target, reasons = evaluate(self.facts())
        with self.lock:
            if self.machine.move_to(target, reasons):
                log.info("device state %s (%s)", target.value, "; ".join(reasons) or "ok")
        self.publish_status()

    def status(self) -> dict:
        enrollment = self.local["enrollment"]
        return {
            "schema": STATUS_SCHEMA,
            "agent_version": __version__,
            "device_id": self.device_id,
            "ephemeral": self.identity.ephemeral if self.identity else None,
            **self.machine.to_dict(),
            "agent_enabled": self.config.agent_enabled,
            "remote_commands": self.config.remote_commands,
            "maintenance": bool(self.local["maintenance"]),
            "connection": self.connection_state().value,
            "control_plane": self.config.control_plane_url or None,
            "enrollment": enrollment.get("state", "unenrolled"),
            "enrolled_at": enrollment.get("enrolled_at"),
            "profile": enrollment.get("profile") or self.config.values.get("DEVICE_PROFILE") or None,
            "certificate_fingerprint": self.credentials.certificate_fingerprint() if self.enrolled else None,
            "policy": self.policy.summary(),
            "last_contact": self.last_contact,
            "last_attempt": self.last_attempt,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "outbox": self.outbox.counts(),
            "inventory_revision": int(self.local["inventory_revision"]),
            "runtime_healthy": None if self.runtime is None else self.runtime.get("healthy") is True,
            "sessions": self.sessions.summary(),
            "config_valid": self.config.valid,
            "updated_at": format_time(utc_now()),
        }

    def publish_status(self) -> None:
        try:
            ensure_dir(self.paths.state_dir, 0o755)
            write_json(self.paths.status, self.status(), 0o644)
        except OSError as exc:
            log.error("cannot publish the agent status: %s", exc)

    # --- enrollment ---------------------------------------------------------------------------
    def enroll(self, token: str) -> dict:
        if self._config_changed():          # device.conf edited just before `boswas-device enroll`
            with self.lock:
                self.load_config()
        if not self.config.valid:
            raise ApiError("CONFIG_INVALID", "device.conf has errors: " + "; ".join(self.config.problems))
        if not self.config.managed:
            raise ApiError("NOT_CONFIGURED", "set CONTROL_PLANE_URL and CONTROL_PLANE_CA in /etc/boswas/device.conf")
        if self.identity is None:
            raise ApiError("IDENTITY_INVALID", self.identity_problem or "no device identity")
        try:
            csr = self.credentials.create_csr(self.device_id)
            if self.client_factory is not None:
                client = self.client_factory(self)
            else:
                context = self.credentials.tls_context(self.ca_path())
                client = HttpsControlPlaneClient(self.config.control_plane_url, context, self.device_id)
            os_doc, hw = self.inventory_source.os_info(), self.inventory_source.hardware()
            request = EnrollmentRequest(
                device_id=self.device_id, csr_pem=csr,
                os=OsInfo(os_doc["name"], os_doc["version"], os_doc["version_id"], os_doc.get("build_id"),
                          os_doc.get("debian_version"), os_doc["kernel"]),
                hardware=HardwareFacts(hw.get("vendor"), hw.get("model"), hw.get("firmware_version"),
                                       hw.get("cpu_model"), hw.get("memory_gib"), hw.get("tpm_version"),
                                       hw.get("boot_mode")),
                profile=self.config.values.get("DEVICE_PROFILE") or None, enrollment_token=token,
                agent_version=__version__, ephemeral=self.identity.ephemeral,
                device_name=self.config.values.get("DEVICE_NAME") or None)
            privacy.check(request.to_dict())
            result = client.enroll(request)
            if result.device_id != self.device_id:
                raise ApiError("ENROLLMENT_FAILED", "the Control Plane answered for another device")
            self.credentials.store_certificate(result.certificate_pem, self.device_id)
            self.credentials.pin_policy_key(result.policy_public_key_pem)
        except ConnectivityError as exc:
            raise ApiError("CONTROL_PLANE_UNREACHABLE", str(exc)) from exc
        except ControlPlaneRejected as exc:
            raise ApiError(exc.code or "ENROLLMENT_REFUSED", str(exc)) from exc
        except CredentialError as exc:
            raise ApiError(exc.code, str(exc)) from exc
        with self.lock:
            self.local["enrollment"] = {"state": "enrolled", "control_plane_url": self.config.control_plane_url,
                                        "enrolled_at": format_time(utc_now()), "profile": result.profile}
            self._save_local_state()
        self.revoked = False
        self.consecutive_failures = 0
        self.backoff.success()
        self.events.append(EventType.DEVICE_REGISTERED.value, f"enrolled with {self.config.control_plane_url}")
        log.info("enrolled with %s", self.config.control_plane_url)
        self.next_sync = 0
        self.next_inventory = 0
        self.wake.set()
        return {"enrolled": True, "device_id": self.device_id, "control_plane": self.config.control_plane_url,
                "profile": result.profile, "certificate_fingerprint": self.credentials.certificate_fingerprint()}

    def unenroll(self) -> dict:
        """Forget the Control Plane: credentials, managed policy and catalog, queued messages."""
        with self.lock:
            self.credentials.clear()
            self.policy.clear()
            self.catalog.clear()
            for item_id, _kind, _doc in self.outbox.items():
                self.outbox.ack(item_id)
            self.local["enrollment"] = {"state": "unenrolled"}
            self._save_local_state()
            self.revoked = False
            self.last_contact = None
            self.consecutive_failures = 0
        self.events.append(EventType.SECURITY_EVENT.value, "device unenrolled by a local administrator; the local "
                                                           "WinCompat policy applies again")
        self.wake.set()
        return {"enrolled": False}

    # --- local API ---------------------------------------------------------------------------------
    def operations(self) -> dict[str, Operation]:
        def limit(default=50, high=1000):
            return Param(int, check=lambda v: None if 1 <= v <= high else f"1 to {high}")

        def op(name, handler, role="any", **params):
            return name, Operation(name, handler, role, params)

        def require_identity():
            if self.identity is None:
                raise ApiError("IDENTITY_INVALID", self.identity_problem or "no device identity")

        def identity(_req):
            require_identity()
            return {**self.identity.to_dict(), "enrollment": self.local["enrollment"].get("state", "unenrolled"),
                    "certificate_fingerprint": self.credentials.certificate_fingerprint() if self.enrolled else None}

        def inventory(_req):
            doc = read_json(self.paths.inventory)
            if not isinstance(doc, dict):
                self.refresh_inventory()
                doc = read_json(self.paths.inventory)
            return doc if isinstance(doc, dict) else {"available": False}

        def policy_status(_req):
            return {"policy": self.policy.summary(), "compat": compat_policy.Policy.load().to_dict()}

        def events(req):
            return {"events": self.events.recent(req.params.get("limit", 100))}

        def commands(req):
            items = []
            for entry in self.ledger.recent(req.params.get("limit", 50)):
                items.append({k: entry.get(k) for k in ("command_id", "type", "application_id", "status",
                                                        "received_at", "finished_at", "error_code", "waiting",
                                                        "created_by")})
            return {"commands": items}

        def runtime(_req):
            if self.runtime is None:
                self.check_runtime()
            return self.runtime or {"available": False}

        def refresh(_req):
            return {"inventory_revision": self.refresh_inventory(force=True)}

        def sync_now(_req):
            if not self.should_sync():
                raise ApiError("NOT_ENROLLED", "nothing to synchronise: the device is not enrolled or the agent "
                                               "is disabled")
            self.next_sync = 0
            self.wake.set()
            return {"scheduled": True}

        def maintenance(req):
            with self.lock:
                self.local["maintenance"] = req.params["enabled"]
                self._save_local_state()
            self.events.append(EventType.SECURITY_EVENT.value,
                               f"maintenance mode {'on' if req.params['enabled'] else 'off'} (local administrator)")
            self.evaluate()
            if not req.params["enabled"]:
                self.work.set()
            return {"maintenance": req.params["enabled"]}

        def enroll(req):
            return self.enroll(req.params["token"])

        def unenroll(_req):
            return self.unenroll()

        def reset_identity(req):
            if not req.params.get("confirm"):
                raise ApiError("BAD_REQUEST", "confirm=true is required: this creates a new device")
            if self.enrolled:
                raise ApiError("REFUSED", "unenroll the device first")
            self.identity = self.identity_store.reset()
            self.identity_problem = None
            self.events.append(EventType.SECURITY_EVENT.value, "device identity reset by a local administrator")
            self.evaluate()
            return self.identity.to_dict()

        return dict([
            op("agent.status", lambda _r: self.status()),
            op("device.identity", identity),
            op("device.inventory", inventory),
            op("device.config", lambda _r: self.config.to_dict()),
            op("policy.status", policy_status),
            op("events.list", events, limit=limit()),
            op("commands.list", commands, limit=limit()),
            op("runtime.status", runtime),
            op("inventory.refresh", refresh, "admin"),
            op("sync.now", sync_now, "admin"),
            op("maintenance.set", maintenance, "admin", enabled=Param(bool, required=True)),
            op("enroll", enroll, "admin", token=Param(str, required=True, check=_token_problem, max_len=256)),
            op("unenroll", unenroll, "admin"),
            op("identity.reset", reset_identity, "admin", confirm=Param(bool)),
            op("session.register", self.sessions.register, "session"),
        ])

    # --- main loop -----------------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        if self._config_changed():
            log.info("device.conf changed; reloading")
            self.load_config()
            self.next_sync = 0
        if now >= self.next_runtime:
            self.check_runtime()
            self.next_runtime = now + RUNTIME_CHECK_SECONDS
        if now >= self.next_inventory:
            self.refresh_inventory()
            self.next_inventory = now + self.interval("inventory_seconds", "INVENTORY_INTERVAL")
        if self.should_sync() and now >= self.next_sync:
            self.sync()
        self.evaluate()

    def next_wait(self) -> float:
        now = self.clock()
        due = [self.next_runtime, self.next_inventory]
        if self.should_sync():
            due.append(self.next_sync)
        return max(1.0, min(min(due) - now, MAX_WAIT))

    def run(self) -> None:
        self.initialize()
        self.api.start()
        self._worker = threading.Thread(target=self._worker_loop, name="boswas-commands", daemon=True)
        self._worker.start()
        self.work.set()                        # commands left from before a restart
        log.info("boswas-device-agent %s started (device %s, %s)", __version__, self.device_id,
                 self.connection_state().value)
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception as exc:          # never die on one bad cycle
                log.exception("agent cycle failed: %s", exc)
            self.wake.wait(self.next_wait())
            self.wake.clear()
        self.api.stop()
        self.work.set()
        self.publish_status()
        log.info("boswas-device-agent stopped")

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s: %(message)s")
    if os.geteuid() != 0:
        sys.stderr.write("boswas-device-agent: must run as root (systemd unit boswas-device-agent.service)\n")
        return 4
    agent = Agent()
    signal.signal(signal.SIGTERM, lambda *_: agent.stop())
    signal.signal(signal.SIGINT, lambda *_: agent.stop())
    signal.signal(signal.SIGHUP, lambda *_: agent.wake.set())
    agent.run()
    return 0
