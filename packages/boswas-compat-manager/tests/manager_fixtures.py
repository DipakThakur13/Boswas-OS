"""Canned backend documents and a FakeBackend shared by the Compatibility Manager tests.

The documents follow the shapes of the session agent (session_agent.py),
boswas-winapp's --json output (boswas_compat/ops.py) and the device agent
(daemon.py), so the viewmodel and the widgets are tested against what the
real services return.
"""

from __future__ import annotations

import copy
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve()
PACKAGE = HERE.parents[1]
for path in (PACKAGE.parent / "boswas-compat", PACKAGE.parent / "boswas-device-agent", PACKAGE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from boswas_manager.backend import BackendRefused, BackendUnavailable  # noqa: E402

THIRTY_TWO_BIT = ("This application requires 32-bit Windows compatibility, which is not supported by Boswas OS.")


def app(app_id: str, name: str, app_state: str, **extra) -> dict:
    doc = {
        "id": app_id, "name": name, "version": "1.0", "publisher": "Example Ltd", "status": "approved",
        "state": "installed", "installed_at": "2026-10-01T10:00:00Z", "updated_at": None,
        "launch": "C:\\Program Files\\Example\\example.exe", "launch_candidates": [], "error": None,
        "manifest_source": "system", "allowed": True, "running": app_state == "RUNNING", "app_state": app_state,
        "state_reason": None, "architecture": "x86_64", "supported": True, "compatibility": "SUPPORTED",
        "last_launch": None,
    }
    doc.update(extra)
    return doc


APPS = [
    app("com.example.notepad", "Example Notepad", "INSTALLED", version="2.1",
        last_launch={"at": "2026-10-03T08:30:00Z", "exit_code": 0, "stopped": False, "timed_out": False,
                     "confinement": "boswas-winapp (enforce)"}),
    app("com.example.paint", "Example Paint", "RUNNING", status="tested"),
    app("com.example.stopped", "Stopped Tool", "STOPPED", status="experimental",
        last_launch={"at": "2026-10-02T09:00:00Z", "exit_code": None, "stopped": True, "timed_out": False,
                     "confinement": "boswas-winapp (enforce)"}),
    app("com.example.broken", "Broken Editor", "REPAIR_REQUIRED", state_reason="the Wine prefix is missing or damaged"),
    app("com.example.blocked", "Blocked Game", "BLOCKED", allowed=False, status="blocked",
        state_reason="the installer is blocked by the Boswas compatibility catalog"),
    app("com.example.old", "Old Suite", "UNSUPPORTED", supported=False, compatibility="UNSUPPORTED_RUNTIME",
        state_reason="com.example.old requires Wine 9.0; this runtime is Wine 10"),
    app("com.example.failed", "Failed Install", "ERROR", state="failed",
        state_reason="the installer exited with code 1"),
    app("com.example.busy", "Busy App", "INSTALLED", busy="repair"),
    app("com.example.installing", "New App", "INSTALLING", state="installing", busy="install"),
    {"id": "local.damaged", "state": "damaged", "app_state": "ERROR", "state_reason": "no valid installation record"},
]

MANIFESTS = [
    {"id": "com.example.notepad", "name": "Example Notepad", "version": "2.2", "publisher": "Example Ltd",
     "status": "approved", "architecture": "x86_64", "layer": "system", "installer_pinned": True,
     "sandbox": {"network": False, "display": True, "audio": False, "gpu": False, "folders": ["documents"]},
     "supported": True, "compatibility": "SUPPORTED", "support_message": None},
    {"id": "com.example.paint", "name": "Example Paint", "version": "1.0", "publisher": "Example Ltd",
     "status": "tested", "architecture": "x86_64", "layer": "system", "installer_pinned": True,
     "sandbox": {"network": False, "display": True, "audio": False, "gpu": True, "folders": []},
     "supported": True, "compatibility": "SUPPORTED", "support_message": None},
]

SYSTEM = {
    "os": {"name": "Boswas OS", "version": "1.0~alpha3", "version_id": "1.0", "architecture": "x86_64"},
    "windows_architectures": ["x86_64"],
    "runtime": {
        "wine": {"available": True, "version": "wine-10.0", "major": "10"},
        "architectures": ["x86_64"], "winearch": "win64",
        "bubblewrap": {"available": True, "version": "0.11.0", "disable_userns": True},
        "runner": {"available": True},
        "apparmor": {"profile": "boswas-winapp", "mode": "enforce", "required": True},
        "policy": {"source": "local", "managed": False, "problems": []},
        "healthy": True,
    },
    "agent": {"available": False},
    "disk": {"total_gib": 237.9, "free_gib": 118.4},
    "session_agent": {"version": "1.0~alpha3", "connected_to_agent": False},
}

AGENT = {
    "schema": "boswas-agent-status/1", "agent_version": "1.0~alpha3",
    "device_id": "3f2b0c1e-0000-4000-8000-000000000001", "state": "READY", "reasons": [],
    "since": "2026-10-04T08:00:00Z", "connection": "CONNECTED",
    "control_plane": "https://control.example.invalid", "enrollment": "enrolled",
    "policy": {"managed": True, "version": "2026.10.1", "applied_at": "2026-10-04T07:00:00Z"},
    "last_contact": "2026-10-04T11:59:00Z", "last_error": None, "maintenance": False, "remote_commands": True,
    "updated_at": "2026-10-04T12:00:00Z",
}

_LIST_ONLY = ("running", "allowed", "supported", "compatibility", "last_launch")
STATUS = {
    "application": {**{k: v for k, v in APPS[0].items() if k not in _LIST_ONLY},
                    "app_state": "INSTALLED", "state_reason": None},
    "installer": {"file": "notepad-setup.exe", "sha256": "ab" * 32, "size": 1234567, "kind": "exe",
                  "machine": "x86_64", "subsystem": "windows", "framework": "inno"},
    "runtime": {"wine": "wine-10.0", "winearch": "win64"},
    "policy": {"allowed": True, "reason": None, "supported": True, "compatibility": "SUPPORTED"},
    "sandbox": {"network": False, "display": True, "audio": True, "gpu": False, "folders": ["documents", "downloads"]},
    "running": False,
    "health": {"prefix": True, "program": True},
    "last_launch": APPS[0]["last_launch"],
    "paths": {"data": "/home/alice/.local/share/boswas/wine/com.example.notepad",
              "prefix": "/home/alice/.local/share/boswas/wine/com.example.notepad/sandbox/prefix",
              "sandbox_view": "/var/lib/boswas/wine/com.example.notepad",
              "logs": "/home/alice/.local/share/boswas/wine/com.example.notepad/logs"},
}


def status_for(app_doc: dict) -> dict:
    doc = copy.deepcopy(STATUS)
    doc["application"].update({k: v for k, v in app_doc.items() if k in doc["application"] or k in
                               ("app_state", "state_reason", "busy")})
    doc["running"] = app_doc.get("app_state") == "RUNNING"
    return doc


SANDBOX = {"network": False, "display": True, "audio": False, "gpu": False, "folders": ["documents"]}

INSPECT_64 = {
    "installer": {"file": "viewer-setup.exe", "sha256": "cd" * 32, "size": 5_000_000, "kind": "exe",
                  "machine": "x86_64", "subsystem": "windows", "framework": "inno"},
    "architecture": {"detected": "x86_64", "supported": True, "runtime": ["x86_64"], "message": None},
    "application": {"id": "com.example.viewer", "name": "Example Viewer", "version": "3.0", "status": "tested",
                    "manifest_source": "system", "kind": "exe", "already_installed": False},
    "sandbox": SANDBOX,
    "decision": {"allowed": True, "reason": None, "message": None},
}

_32BIT_DETAIL = (f"{THIRTY_TWO_BIT} (setup32.exe is an x86 (32-bit) Windows program; Boswas OS runs x86_64 (64-bit) "
                 "Windows applications only.)")
INSPECT_32 = {
    "installer": {"file": "setup32.exe", "sha256": "ef" * 32, "size": 800_000, "kind": "exe", "machine": "x86",
                  "subsystem": "windows", "framework": None},
    "architecture": {"detected": "x86", "supported": False, "runtime": ["x86_64"], "message": _32BIT_DETAIL},
    "application": None, "sandbox": None,
    "decision": {"allowed": False, "reason": "architecture", "message": _32BIT_DETAIL},
}


def refused_inspection(reason: str, message: str, application: dict | None = None) -> dict:
    doc = copy.deepcopy(INSPECT_64)
    doc["application"] = application
    doc["sandbox"] = None if application is None else SANDBOX
    doc["decision"] = {"allowed": False, "reason": reason, "message": message}
    return doc


INSPECT_BLOCKED = refused_inspection(
    "blocked", "this installer (sha256 cdcdcdcdcdcdcdcd...) is blocked by the Boswas compatibility catalog")
INSPECT_UNLISTED_DENIED = refused_inspection(
    "unlisted-denied", "viewer-setup.exe is not in the Boswas compatibility catalog and the device policy does not "
                       "allow unlisted applications (UNLISTED_APPS=deny)")
INSPECT_INSTALLED = refused_inspection(
    "already-installed", "com.example.notepad is already installed (remove, repair or upgrade it)",
    {"id": "com.example.notepad", "name": "Example Notepad", "version": "2.2", "status": "approved",
     "manifest_source": "system", "kind": "exe", "already_installed": True})

JOB_RUNNING = {"job_id": "0123456789abcdef", "op": "install", "application_id": None, "source": "local",
               "state": "running", "started_at": "2026-10-04T12:00:00Z", "finished_at": None,
               "progress": ["boswas-winapp: verifying viewer-setup.exe",
                            "boswas-winapp: creating the Wine prefix (first run takes a while)"],
               "result": None, "error": None}
JOB_DONE = {**JOB_RUNNING, "state": "succeeded", "finished_at": "2026-10-04T12:01:00Z",
            "progress": JOB_RUNNING["progress"] + ["boswas-winapp: installed com.example.viewer"],
            "result": {"application": {"id": "com.example.viewer", "name": "Example Viewer", "version": "3.0",
                                       "launch": "C:\\Program Files\\Viewer\\viewer.exe"}}}
JOB_FAILED = {**JOB_RUNNING, "state": "failed", "finished_at": "2026-10-04T12:01:00Z",
              "error": {"code": "INSTALLER_FAILED",
                        "message": "the installer exited with code 2 (see /home/alice/.local/share/boswas/wine/"
                                   "com.example.viewer/logs/install-1.log)"}}

EVENTS = [
    {"type": "install.succeeded", "occurred_at": "2026-10-04T11:00:00Z", "detail": "install com.example.notepad 2.1",
     "application_id": "com.example.notepad", "source": "local"},
    {"type": "launch.started", "occurred_at": "2026-10-04T11:30:00Z", "detail": "com.example.paint started",
     "application_id": "com.example.paint", "source": "remote"},
]
COMMANDS = [
    {"command_id": "c-1", "type": "INSTALL_APPLICATION", "application_id": "com.example.paint", "status": "SUCCEEDED",
     "received_at": "2026-10-04T10:00:00Z", "finished_at": "2026-10-04T10:05:00Z", "error_code": None},
]

LOGS = {"application": "com.example.notepad",
        "log": "/home/alice/.local/share/boswas/wine/com.example.notepad/logs/launch-20261003.log",
        "available": ["install-20261001.log", "launch-20261003.log"],
        "lines": ["\x1b[31mfixme:ntdll:NtQuerySystemInformation\x1b[0m", "err:module \x9b2Jcleared\x85 line",
                  "title\x1b]0;evil\x07done", "plain line"]}


class FakeBackend:
    """Implements the Backend methods with canned documents and records every call.

    Set errors[method] to an exception to make that method raise it, and
    job_sequence to the documents jobs.get returns in turn (the last repeats).
    """

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.lock = threading.Lock()
        self.apps = copy.deepcopy(APPS)
        self.manifests = copy.deepcopy(MANIFESTS)
        self.system = copy.deepcopy(SYSTEM)
        self.agent = copy.deepcopy(AGENT)
        self.inspection = copy.deepcopy(INSPECT_64)
        self.job_sequence = [copy.deepcopy(JOB_RUNNING), copy.deepcopy(JOB_DONE)]
        self.logs_doc = copy.deepcopy(LOGS)
        self.errors: dict[str, Exception] = {}

    def _record(self, name: str, *args, **kwargs):
        with self.lock:
            self.calls.append((name, args, kwargs))
        error = self.errors.get(name)
        if error is not None:
            raise error

    def called(self, name: str) -> list[tuple[tuple, dict]]:
        with self.lock:
            return [(args, kwargs) for n, args, kwargs in self.calls if n == name]

    # --- session agent ---
    def list_apps(self):
        self._record("list_apps")
        return copy.deepcopy(self.apps)

    def app_status(self, app_id):
        self._record("app_status", app_id)
        match = next((a for a in self.apps if a.get("id") == app_id), None)
        if match is None:
            raise BackendRefused("NOT_FOUND", f"{app_id} is not installed")
        return status_for(match)

    def inspect(self, path, app_id=None, name=None):
        self._record("inspect", path, app_id, name)
        return copy.deepcopy(self.inspection)

    def install(self, path, app_id=None, name=None):
        self._record("install", path, app_id, name)
        return JOB_RUNNING["job_id"]

    def upgrade(self, app_id, path):
        self._record("upgrade", app_id, path)
        return JOB_RUNNING["job_id"]

    def repair(self, app_id):
        self._record("repair", app_id)
        return "1111111111111111"

    def remove(self, app_id):
        self._record("remove", app_id)
        return "2222222222222222"

    def job(self, job_id):
        self._record("job", job_id)
        with self.lock:
            doc = self.job_sequence.pop(0) if len(self.job_sequence) > 1 else self.job_sequence[0]
        return {**copy.deepcopy(doc), "job_id": job_id}

    def jobs(self):
        self._record("jobs")
        return []

    def launch(self, app_id, display=None, xauthority=None, wayland_display=None):
        given = (("display", display), ("xauthority", xauthority), ("wayland_display", wayland_display))
        kwargs = {key: value for key, value in given if value is not None}
        self._record("launch", app_id, **kwargs)
        return {"application": app_id, "started": True, "finished": False}

    def stop(self, app_id):
        self._record("stop", app_id)
        return {"application": app_id, "was_running": True, "stopped": True}

    def logs(self, app_id, kind=None, lines=1000):
        self._record("logs", app_id, kind)
        return copy.deepcopy(self.logs_doc)

    def clear_logs(self, app_id):
        self._record("clear_logs", app_id)
        return {"application": app_id, "removed": 2}

    def catalog(self):
        self._record("catalog")
        return {"manifests": copy.deepcopy(self.manifests), "problems": [], "layers": [], "policy": {}}

    def system_status(self):
        self._record("system_status")
        return copy.deepcopy(self.system)

    def events(self, limit=50):
        self._record("events", limit)
        return copy.deepcopy(EVENTS)

    # --- device agent ---
    def agent_status(self):
        self._record("agent_status")
        return copy.deepcopy(self.agent)

    def policy_status(self):
        self._record("policy_status")
        return {"policy": self.agent["policy"], "compat": {}}

    def remote_commands(self, limit=20):
        self._record("remote_commands", limit)
        return copy.deepcopy(COMMANDS)


def unavailable(service: str = "session") -> BackendUnavailable:
    return BackendUnavailable(service, f"the service socket /run/{service}.sock is not available (No such file)")
