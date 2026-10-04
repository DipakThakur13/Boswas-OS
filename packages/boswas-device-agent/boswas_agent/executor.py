"""Execution of typed remote commands on the device.

Each CommandType has exactly one handler. Handlers decide with the
device's own state and policy, and Windows-application work is done by
boswas-winapp in the active user's session (sessions.py), so a command can
never do more than that user could do with boswas-winapp locally:

  * the applied policy's allowed_commands is checked first;
  * INSTALL/UPDATE refuse 32-bit catalog entries before anything is
    downloaded, check the device's WinCompat policy, verify the installer's
    size and SHA-256 against the manifest pin, and let boswas-winapp
    re-check everything (catalog, policy, architecture, sandbox);
  * APPLY_POLICY only applies a policy that verifies against the pinned key;
  * UPDATE_AGENT needs UPDATE_POLICY=managed on the device and in the policy,
    and installs the named version from the configured, signed APT sources.

Expected failures become FAILED outcomes with a stable error code;
SessionUnavailable and ConnectivityError are raised so the caller can retry
the command later (until it expires).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from boswas_compat import manifest as compat_manifest
from boswas_compat.errors import Refused

from .commands import Command, CommandOutcome, CommandType, architecture_refusal
from .errors import (BackendError, ConnectivityError, ControlPlaneRejected, PolicyVerificationError,
                     SessionUnavailable)
from .ledger import EventLog
from .localapi import ApiError
from .models import EventType
from .policy_store import ManagedCatalog, PolicyStore
from .storage import ensure_dir

INSTALL_TIMEOUT = 4 * 3600
SHORT_TIMEOUT = 120
_FILE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Services:
    artifacts_dir: Path
    policy: PolicyStore
    catalog: ManagedCatalog
    events: EventLog
    dispatch: Callable[[str, dict, float], dict]       # to the active user's session agent
    client: Callable[[], object]                       # the Control Plane client
    compat_policy: Callable[[], object]                 # boswas_compat.policy.Policy (managed or local)
    refresh_inventory: Callable[[], int]
    agent_version: Callable[[], str]
    start_agent_update: Callable[[str], None]
    update_policy: Callable[[], str]                    # UPDATE_POLICY from device.conf


class CommandExecutor:
    def __init__(self, services: Services):
        self.s = services
        self.handlers = {
            CommandType.INSTALL_APPLICATION: self._install,
            CommandType.UPDATE_APPLICATION: self._install,
            CommandType.REMOVE_APPLICATION: self._remove,
            CommandType.LAUNCH_APPLICATION: self._launch,
            CommandType.STOP_APPLICATION: self._stop,
            CommandType.REPAIR_APPLICATION: self._repair,
            CommandType.REFRESH_INVENTORY: self._refresh_inventory,
            CommandType.APPLY_POLICY: self._apply_policy,
            CommandType.UPDATE_AGENT: self._update_agent,
        }

    def allowed(self, ctype: CommandType) -> bool:
        settings = self.s.policy.agent_settings()
        return settings is None or ctype.value in settings.get("allowed_commands", [])

    def execute(self, cmd: Command) -> CommandOutcome:
        if not self.allowed(cmd.type):
            return CommandOutcome.failed("COMMAND_REFUSED", f"{cmd.type.value} is not allowed by the device policy")
        try:
            return self.handlers[cmd.type](cmd)
        except (SessionUnavailable, ConnectivityError):
            raise
        except BackendError as exc:
            return CommandOutcome.failed(exc.code, str(exc), application_id=cmd.application_id or "")
        except ApiError as exc:
            if exc.code in ("NO_USER_SESSION",):
                raise SessionUnavailable(exc.message) from exc
            return CommandOutcome.failed(exc.code if re.fullmatch(r"[A-Z][A-Z0-9_]+", exc.code) else "FAILED",
                                         exc.message)
        except ControlPlaneRejected as exc:
            return CommandOutcome.failed(exc.code if re.fullmatch(r"[A-Z][A-Z0-9_]+", exc.code or "") else "REJECTED",
                                         str(exc))
        except PolicyVerificationError as exc:
            self.s.events.append(EventType.SECURITY_EVENT.value, f"policy rejected: {exc}", source="remote",
                                 command_id=cmd.command_id)
            return CommandOutcome.failed("POLICY_INVALID", str(exc))
        except Exception as exc:            # report, keep the agent running
            return CommandOutcome.failed("INTERNAL", f"internal error ({type(exc).__name__})")

    # --- application commands -------------------------------------------------------------
    def _status(self, app_id: str) -> dict | None:
        try:
            return self.s.dispatch("apps.status", {"id": app_id}, SHORT_TIMEOUT)
        except ApiError as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise

    @staticmethod
    def _app(status: dict | None) -> dict:
        return (status or {}).get("application") or {}

    def _blocked(self, cmd: Command, message: str, code: str) -> CommandOutcome:
        self.s.events.append(EventType.APPLICATION_BLOCKED.value, message, application_id=cmd.application_id,
                             source="remote", command_id=cmd.command_id)
        return CommandOutcome.failed(code, message, application_id=cmd.application_id or "")

    def _install(self, cmd: Command) -> CommandOutcome:
        payload, app_id = cmd.payload, cmd.application_id
        refusal = architecture_refusal(cmd.type, payload)
        if refusal:
            return self._blocked(cmd, refusal, "UNSUPPORTED_ARCHITECTURE")
        manifest = compat_manifest.from_document(payload["manifest"], layer="managed")
        policy = self.s.compat_policy()
        try:
            policy.check_application(app_id)
            policy.check_status(manifest.status, app_id)
        except Refused as exc:
            return self._blocked(cmd, str(exc), "POLICY_REFUSED")
        current = self._status(app_id)
        app = self._app(current)
        installed_sha = ((current or {}).get("installer") or {}).get("sha256")
        update = cmd.type == CommandType.UPDATE_APPLICATION
        if current is not None:
            if installed_sha == payload["installer"]["sha256"] and app.get("state") == "installed":
                return CommandOutcome.ok(application_id=app_id, version=str(app.get("version") or manifest.version),
                                         installed=True, already=True, app_state=str(app.get("app_state") or ""))
            if not update:
                return CommandOutcome.failed("ALREADY_INSTALLED", f"{app_id} {app.get('version')} is installed; "
                                             "use UPDATE_APPLICATION for another version", application_id=app_id)
        elif update:
            return CommandOutcome.failed("NOT_INSTALLED", f"{app_id} is not installed", application_id=app_id)
        self.s.catalog.install(payload["manifest"])
        path = self._artifact(payload["installer"])
        try:
            if update:
                result = self.s.dispatch("apps.upgrade", {"id": app_id, "path": str(path)}, INSTALL_TIMEOUT)
            else:
                result = self.s.dispatch("apps.install", {"path": str(path), "id": app_id}, INSTALL_TIMEOUT)
        finally:
            path.unlink(missing_ok=True)
        app = self._app(result)
        self.s.events.append(EventType.APPLICATION_INSTALLED.value,
                             f"{app_id} {app.get('version') or manifest.version} "
                             f"{'updated' if update else 'installed'} by remote command",
                             application_id=app_id, source="remote", command_id=cmd.command_id)
        return CommandOutcome.ok(application_id=app_id, version=str(app.get("version") or manifest.version),
                                 installed=True, app_state=str(app.get("app_state") or "INSTALLED"))

    def _artifact(self, installer: dict) -> Path:
        """The verified installer for a command, downloaded from the Control Plane."""
        sha, size = installer["sha256"], installer["size"]
        directory = ensure_dir(self.s.artifacts_dir, 0o755)
        name = _FILE_RE.sub("_", installer["file_name"])[:100].strip("._") or "installer"
        path = directory / f"{sha[:16]}-{name}"
        if path.is_file() and path.stat().st_size == size and _sha256(path) == sha:
            return path
        path.unlink(missing_ok=True)
        self.s.client().download_artifact(sha, path, size)
        if path.stat().st_size != size or _sha256(path) != sha:
            path.unlink(missing_ok=True)
            raise ControlPlaneRejected("downloaded installer does not match the command", code="ARTIFACT_MISMATCH")
        path.chmod(0o644)
        return path

    def _remove(self, cmd: Command) -> CommandOutcome:
        app_id = cmd.application_id
        if self._status(app_id) is None:
            return CommandOutcome.ok(application_id=app_id, removed=False, already=True)
        self.s.dispatch("apps.remove", {"id": app_id}, SHORT_TIMEOUT * 5)
        self.s.events.append(EventType.APPLICATION_REMOVED.value, f"{app_id} removed by remote command",
                             application_id=app_id, source="remote", command_id=cmd.command_id)
        return CommandOutcome.ok(application_id=app_id, removed=True)

    def _launch(self, cmd: Command) -> CommandOutcome:
        app_id = cmd.application_id
        status = self._status(app_id)
        if status is None:
            return CommandOutcome.failed("NOT_INSTALLED", f"{app_id} is not installed", application_id=app_id)
        if self._app(status).get("app_state") == "RUNNING":
            return CommandOutcome.ok(application_id=app_id, started=False, already=True, app_state="RUNNING")
        self.s.dispatch("apps.launch", {"id": app_id}, SHORT_TIMEOUT)
        self.s.events.append(EventType.APPLICATION_LAUNCHED.value, f"{app_id} started by remote command",
                             application_id=app_id, source="remote", command_id=cmd.command_id)
        return CommandOutcome.ok(application_id=app_id, started=True, app_state="RUNNING")

    def _stop(self, cmd: Command) -> CommandOutcome:
        app_id = cmd.application_id
        if self._status(app_id) is None:
            return CommandOutcome.failed("NOT_INSTALLED", f"{app_id} is not installed", application_id=app_id)
        result = self.s.dispatch("apps.stop", {"id": app_id}, SHORT_TIMEOUT)
        return CommandOutcome.ok(application_id=app_id, was_running=result.get("was_running") is True,
                                 stopped=result.get("stopped") is True)

    def _repair(self, cmd: Command) -> CommandOutcome:
        app_id = cmd.application_id
        if self._status(app_id) is None:
            return CommandOutcome.failed("NOT_INSTALLED", f"{app_id} is not installed", application_id=app_id)
        result = self.s.dispatch("apps.repair", {"id": app_id}, INSTALL_TIMEOUT)
        healthy = result.get("healthy") is True
        self.s.events.append(EventType.APPLICATION_REPAIRED.value,
                             f"{app_id} repaired by remote command ({'healthy' if healthy else 'reinstall needed'})",
                             application_id=app_id, source="remote", command_id=cmd.command_id)
        if not healthy:
            return CommandOutcome.failed("REPAIR_INCOMPLETE", str(result.get("error") or "the application must be "
                                                                 "reinstalled"), application_id=app_id, healthy=False)
        return CommandOutcome.ok(application_id=app_id, healthy=True)

    # --- device commands -------------------------------------------------------------------
    def _refresh_inventory(self, cmd: Command) -> CommandOutcome:
        return CommandOutcome.ok(inventory_revision=self.s.refresh_inventory())

    def _apply_policy(self, cmd: Command) -> CommandOutcome:
        envelope = self.s.client().fetch_policy()
        if envelope is None:
            return CommandOutcome.failed("NO_POLICY", "the Control Plane has no policy for this device")
        doc, changed = self.s.policy.apply(envelope)
        if changed:
            self.s.events.append(EventType.POLICY_UPDATED.value, f"policy {doc['version']} applied",
                                 source="remote", command_id=cmd.command_id)
        return CommandOutcome.ok(policy_version=doc["version"], already=not changed)

    def _update_agent(self, cmd: Command) -> CommandOutcome:
        version = cmd.payload["version"]
        if self.s.update_policy() != "managed" or self.s.policy.updates_setting() != "managed":
            return CommandOutcome.failed("UPDATE_NOT_PERMITTED", "agent updates by command need UPDATE_POLICY=managed "
                                         "in device.conf and agent_updates=managed in the device policy")
        if self.s.agent_version() == version:
            return CommandOutcome.ok(agent_version=version, already=True)
        self.s.start_agent_update(version)
        return CommandOutcome.ok(agent_version=version, started=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
