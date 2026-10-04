"""boswas-session-agent: the user's local management service (systemd user unit).

It is the backend of the Compatibility Manager and the hand of the device
agent in the user's session:

  * Local API on $XDG_RUNTIME_DIR/boswas/session.sock (directory 0700; only
    the same user may connect, checked with SO_PEERCRED). The Compatibility
    Manager talks only to this socket and to the device agent's read-only
    status operations; it never runs Wine, bubblewrap or boswas-winapp itself.
  * Registers with the device agent and runs the application operations the
    agent dispatches for remote commands (sessions.REMOTE_OPERATIONS).

Every operation runs boswas-winapp as this user (winapp.py), so policy,
manifests, prefixes, bubblewrap and AppArmor are always enforced by the
same backend as on the command line. Long operations (install, upgrade,
repair, remove) run as jobs whose progress the GUI polls.
"""

from __future__ import annotations

import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from boswas_compat.executor import sanitize
from boswas_compat.manifest import valid_id

from . import __version__
from .commands import format_time, utc_now
from .config import parse as parse_env
from .errors import BackendError
from .localapi import ApiClient, ApiError, ApiServer, Operation, Param, validate_params
from .sessions import SessionLink
from .storage import read_bytes
from .winapp import Winapp, finish_launch

AGENT_SOCKET = "/run/boswas-agent/agent.sock"
ARTIFACTS_PREFIX = "/var/lib/boswas/agent/artifacts/"
RELEASE_FILE = "/usr/lib/boswas/release"
MAX_JOBS = 50
MAX_EVENTS = 300
LAUNCH_SETTLE_SECONDS = 4.0
_DISPLAY_RE = re.compile(r"^(?:unix)?:\d{1,4}(?:\.\d+)?$")
_WAYLAND_RE = re.compile(r"^wayland-[0-9]{1,3}$")
SESSION_ENV = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "LANG", "TZ")


def _id_check(value: str) -> str | None:
    return None if valid_id(value) else "not a valid application ID"


def _path_check(value: str) -> str | None:
    return None if value.startswith("/") else "must be an absolute path"


def _kind_check(value: str) -> str | None:
    return None if value in ("latest", "launch", "install", "repair") else "latest, launch, install or repair"


def backend_error(exc: BackendError) -> ApiError:
    return ApiError(exc.reason.upper().replace("-", "_")[:40] or "FAILED", str(exc))


@dataclass
class Job:
    job_id: str
    op: str
    application_id: str | None
    source: str                        # local | remote
    state: str = "running"             # running | succeeded | failed
    started_at: str = field(default_factory=lambda: format_time(utc_now()))
    finished_at: str | None = None
    progress: list[str] = field(default_factory=list)
    result: dict | None = None
    error: dict | None = None

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "op": self.op, "application_id": self.application_id, "source": self.source,
                "state": self.state, "started_at": self.started_at, "finished_at": self.finished_at,
                "progress": self.progress[-50:], "result": self.result, "error": self.error}


class SessionAgent:
    def __init__(self, *, socket_path: Path, winapp: Winapp | None = None, agent_socket: str = AGENT_SOCKET,
                 environ=None, run=subprocess.run, release_file: str = RELEASE_FILE,
                 artifacts_prefix: str = ARTIFACTS_PREFIX):
        self.socket_path = Path(socket_path)
        self.remote_params = remote_params(artifacts_prefix)
        self.environ = os.environ if environ is None else environ
        self.winapp = winapp or Winapp(env=self.environ)
        self.agent_socket = agent_socket
        self._run = run
        self.release_file = release_file
        self.jobs: dict[str, Job] = {}
        self.events: list[dict] = []
        self.launches: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.server = ApiServer(self.socket_path, self.operations(), mode=0o600)
        self.link = SessionLink(agent_socket, self.remote_execute)

    # --- events and jobs ------------------------------------------------------------------
    def event(self, kind: str, detail: str, *, application_id: str | None = None, source: str = "local") -> None:
        entry = {"type": kind, "occurred_at": format_time(utc_now()), "detail": sanitize(detail)[:500],
                 "application_id": application_id, "source": source}
        with self.lock:
            self.events.append(entry)
            del self.events[:-MAX_EVENTS]

    def _new_job(self, op: str, app_id: str | None, source: str) -> Job:
        job = Job(secrets.token_hex(8), op, app_id, source)
        with self.lock:
            self.jobs[job.job_id] = job
            finished = [j for j in self.jobs.values() if j.state != "running"]
            for old in sorted(finished, key=lambda j: j.started_at)[:max(0, len(self.jobs) - MAX_JOBS)]:
                del self.jobs[old.job_id]
        return job

    def _run_job(self, job: Job, work) -> dict:
        def progress(line: str) -> None:
            if line:
                job.progress.append(line[:500])
                del job.progress[:-500]
        try:
            job.result = work(progress)
            job.state = "succeeded"
            self.event(f"{job.op}.succeeded", self._describe(job), application_id=job.application_id, source=job.source)
            return job.result
        except BackendError as exc:
            job.state = "failed"
            job.error = {"code": backend_error(exc).code, "message": str(exc)}
            kind = "policy-refused" if exc.reason in ("blocked", "policy", "policy-blocked", "policy-not-allowed",
                                                       "architecture", "unlisted-denied", "status-not-allowed",
                                                       "installer-mismatch") else f"{job.op}.failed"
            self.event(kind, f"{job.op} {job.application_id or ''}: {exc}", application_id=job.application_id,
                       source=job.source)
            raise backend_error(exc) from exc
        finally:
            job.finished_at = format_time(utc_now())

    @staticmethod
    def _describe(job: Job) -> str:
        app = (job.result or {}).get("application")
        version = app.get("version") if isinstance(app, dict) else None
        return f"{job.op} {job.application_id or ''} {version or ''}".strip()

    def _start_job(self, op: str, app_id: str | None, work) -> dict:
        job = self._new_job(op, app_id, "local")

        def target():
            try:
                self._run_job(job, work)
            except ApiError:
                pass
        threading.Thread(target=target, name=f"job-{job.job_id}", daemon=True).start()
        return {"job_id": job.job_id}

    # --- session environment ------------------------------------------------------------------
    def session_env(self, params: dict | None = None) -> dict:
        """Display variables for a launch: from the request (GUI), else the user manager's environment."""
        env = {}
        try:
            proc = self._run(["systemctl", "--user", "show-environment"], capture_output=True, text=True,
                             timeout=10, check=False)
            for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
                key, _, value = line.partition("=")
                if key in SESSION_ENV:
                    env[key] = value
        except (OSError, subprocess.SubprocessError):
            pass
        for key in SESSION_ENV:
            if key not in env and isinstance(self.environ.get(key), str):
                env[key] = self.environ[key]
        params = params or {}
        if params.get("display") is not None:
            env["DISPLAY"] = params["display"]
        if params.get("xauthority") is not None:
            env["XAUTHORITY"] = params["xauthority"]
        if params.get("wayland_display") is not None:
            env["WAYLAND_DISPLAY"] = params["wayland_display"]
        return env

    # --- application operations ------------------------------------------------------------
    def list_apps(self, _req=None) -> dict:
        try:
            result = self.winapp.list()
        except BackendError as exc:
            raise backend_error(exc) from exc
        with self.lock:
            busy = {j.application_id: j.op for j in self.jobs.values() if j.state == "running"}
        for app in result.get("applications", []):
            if isinstance(app, dict) and app.get("id") in busy:
                app["busy"] = busy[app["id"]]
        return result

    def call(self, fn, *args, **kwargs) -> dict:
        try:
            return fn(*args, **kwargs)
        except BackendError as exc:
            raise backend_error(exc) from exc

    def launch(self, app_id: str, params: dict | None = None, source: str = "local") -> dict:
        status = self.call(self.winapp.status, app_id)
        app = status.get("application", {})
        if app.get("app_state") == "RUNNING":
            return {"application": app_id, "started": False, "already_running": True}
        if not status.get("policy", {}).get("allowed", False):
            reason = status.get("policy", {}).get("reason") or "not allowed by policy"
            self.event("policy-refused", f"launch {app_id}: {reason}", application_id=app_id, source=source)
            raise ApiError("POLICY", f"{app_id} may not be started: {reason}")
        proc = self.call(self.winapp.launch, app_id, env=self.session_env(params))
        try:
            proc.wait(timeout=LAUNCH_SETTLE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        if proc.poll() is not None:
            outcome = finish_launch(proc)
            if not outcome["ok"]:
                self.event("launch.failed", f"launch {app_id}: {outcome.get('message')}", application_id=app_id,
                           source=source)
                raise ApiError(str(outcome.get("reason", "failed")).upper().replace("-", "_"),
                               outcome.get("message") or "the application did not start")
            self.event("launch.finished", f"{app_id} exited with code {outcome.get('exit_code')}",
                       application_id=app_id, source=source)
            return {"application": app_id, "started": True, "finished": True, "exit_code": outcome.get("exit_code")}
        with self.lock:
            self.launches[app_id] = proc
        self.event("launch.started", f"{app_id} started", application_id=app_id, source=source)
        threading.Thread(target=self._reap, args=(app_id, proc, source), daemon=True).start()
        return {"application": app_id, "started": True, "finished": False}

    def _reap(self, app_id: str, proc, source: str) -> None:
        outcome = finish_launch(proc)
        with self.lock:
            if self.launches.get(app_id) is proc:
                del self.launches[app_id]
        if outcome.get("stopped"):
            detail = f"{app_id} was stopped"
        elif outcome["ok"]:
            detail = f"{app_id} exited with code {outcome.get('exit_code')}"
        else:
            detail = f"{app_id}: {outcome.get('message')}"
        self.event("launch.finished", detail, application_id=app_id, source=source)

    def install(self, path: str, app_id: str | None, name: str | None, progress=None) -> dict:
        return self.winapp.install(path, app_id, name, on_line=progress, env=self.session_env())

    def system_status(self, _req=None) -> dict:
        try:
            runtime = self.winapp.runtime()
        except BackendError as exc:
            runtime = {"healthy": False, "error": str(exc)}
        agent = None
        agent_runtime = None
        try:
            client = ApiClient(self.agent_socket, timeout=10)
            agent = client.call("agent.status")
            agent_runtime = client.call("runtime.status")
        except (ConnectionError, ApiError):
            agent = None
        if agent_runtime and isinstance(agent_runtime.get("apparmor"), dict):
            runtime.setdefault("apparmor", {})["mode"] = agent_runtime["apparmor"].get("mode")
            runtime["healthy"] = agent_runtime.get("healthy", runtime.get("healthy"))
        release = parse_env((read_bytes(Path(self.release_file), 65536) or b"").decode("utf-8", errors="replace"))
        try:
            usage = shutil.disk_usage(str(Path(self.environ.get("HOME", "/"))))
            disk = {"total_gib": round(usage.total / 1024 ** 3, 1), "free_gib": round(usage.free / 1024 ** 3, 1)}
        except OSError:
            disk = {"total_gib": None, "free_gib": None}
        return {
            "os": {"name": release.get("BOSWAS_NAME", "Boswas OS"), "version": release.get("BOSWAS_VERSION"),
                   "version_id": release.get("BOSWAS_VERSION_ID"), "architecture": platform.machine()},
            "windows_architectures": runtime.get("architectures", ["x86_64"]),
            "runtime": runtime,
            "agent": agent if agent is not None else {"available": False},
            "disk": disk,
            "session_agent": {"version": __version__, "connected_to_agent": self.link.connected.is_set()},
        }

    # --- remote dispatch (from the device agent) ---------------------------------------------------
    def remote_execute(self, op: str, params: object) -> dict:
        params = validate_params(self.remote_params[op], params)
        app_id = params.get("id")
        if op == "apps.status":
            return self.call(self.winapp.status, app_id)
        if op == "apps.launch":
            return self.launch(app_id, None, source="remote")
        if op == "apps.stop":
            result = self.call(self.winapp.stop, app_id)
            self.event("stop", f"{app_id} stopped by remote command", application_id=app_id, source="remote")
            return result
        job = self._new_job(op.split(".", 1)[1], app_id, "remote")
        if op == "apps.install":
            return self._run_job(job, lambda progress: self.install(params["path"], app_id, None, progress))
        if op == "apps.upgrade":
            return self._run_job(job, lambda progress: self.winapp.upgrade(app_id, params["path"], on_line=progress,
                                                                           env=self.session_env()))
        if op == "apps.repair":
            return self._run_job(job, lambda progress: self.winapp.repair(app_id, on_line=progress,
                                                                          env=self.session_env()))
        if op == "apps.remove":
            return self._run_job(job, lambda _p: self.winapp.remove(app_id))
        raise ApiError("UNKNOWN_OPERATION", op)

    # --- API table ---------------------------------------------------------------------------------
    def operations(self) -> dict[str, Operation]:
        app = Param(str, required=True, check=_id_check, max_len=96)
        path = Param(str, required=True, check=_path_check)
        display = {"display": Param(str, check=lambda v: None if _DISPLAY_RE.fullmatch(v) else "a local display"),
                   "xauthority": Param(str, check=_path_check),
                   "wayland_display": Param(str, check=lambda v: None if _WAYLAND_RE.fullmatch(v) else "wayland-N")}

        def o(op_name, handler, **params):
            return op_name, Operation(op_name, handler, "owner", params)

        def job_get(req):
            with self.lock:
                job = self.jobs.get(req.params["job_id"])
            if job is None:
                raise ApiError("NOT_FOUND", "no such job")
            return job.to_dict()

        def jobs_list(_req):
            with self.lock:
                return {"jobs": [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: j.started_at,
                                                             reverse=True)]}

        def events(req):
            with self.lock:
                items = list(reversed(self.events))[:req.params.get("limit", 100)]
            return {"events": items}

        def install(req):
            p = req.params
            # Fail fast with boswas-winapp's own decision (nothing is created or run).
            inspected = self.call(self.winapp.inspect, p["path"], p.get("id"), p.get("name"))
            decision = inspected.get("decision") or {}
            if decision.get("allowed") is not True:
                reason = str(decision.get("reason") or "refused")
                self.event("policy-refused" if reason != "already-installed" else "install.refused",
                           f"install {p.get('id') or Path(p['path']).name}: {decision.get('message')}",
                           application_id=p.get("id"))
                raise ApiError(reason.upper().replace("-", "_")[:40], str(decision.get("message") or "refused"))
            # The ID it will get (catalog match or derived), so the library can show it as busy.
            app_id = p.get("id") or (inspected.get("application") or {}).get("id")
            return self._start_job("install", app_id if valid_id(app_id) else None,
                                   lambda progress: self.install(p["path"], p.get("id"), p.get("name"), progress))

        def stop(req):
            result = self.call(self.winapp.stop, req.params["id"])
            if result.get("was_running"):
                self.event("stop", f"{req.params['id']} stopped", application_id=req.params["id"])
            return result

        return dict([
            o("apps.list", self.list_apps),
            o("apps.status", lambda r: self.call(self.winapp.status, r.params["id"]), id=app),
            o("apps.inspect", lambda r: self.call(self.winapp.inspect, r.params["path"], r.params.get("id"),
                                                  r.params.get("name")),
              path=path, id=Param(str, check=_id_check, max_len=96), name=Param(str, max_len=80)),
            o("apps.install", install, path=path, id=Param(str, check=_id_check, max_len=96),
              name=Param(str, max_len=80)),
            o("apps.upgrade", lambda r: self._start_job(
                "upgrade", r.params["id"], lambda progress: self.winapp.upgrade(
                    r.params["id"], r.params["path"], on_line=progress, env=self.session_env())), id=app, path=path),
            o("apps.launch", lambda r: self.launch(r.params["id"], r.params), id=app, **display),
            o("apps.stop", stop, id=app),
            o("apps.repair", lambda r: self._start_job(
                "repair", r.params["id"], lambda progress: self.winapp.repair(r.params["id"], on_line=progress,
                                                                              env=self.session_env())), id=app),
            o("apps.remove", lambda r: self._start_job("remove", r.params["id"],
                                                       lambda _p: self.winapp.remove(r.params["id"])), id=app),
            o("apps.logs", lambda r: self.call(self.winapp.logs, r.params["id"], r.params.get("kind"),
                                               r.params.get("lines", 200)),
              id=app, kind=Param(str, check=_kind_check), lines=Param(int, check=lambda v: None if 0 <= v <= 5000
                                                                       else "0 to 5000")),
            o("apps.clear_logs", lambda r: self.call(self.winapp.clear_logs, r.params["id"]), id=app),
            o("catalog.list", lambda _r: self.call(self.winapp.catalog)),
            o("runtime.status", lambda _r: self.call(self.winapp.runtime)),
            o("system.status", self.system_status),
            o("jobs.get", job_get, job_id=Param(str, required=True, check=lambda v: None if re.fullmatch(
                r"[0-9a-f]{16}", v) else "a job ID")),
            o("jobs.list", jobs_list),
            o("events.list", events, limit=Param(int, check=lambda v: None if 1 <= v <= MAX_EVENTS else
                                                  f"1 to {MAX_EVENTS}")),
        ])

    # --- service ---------------------------------------------------------------------------------
    def start(self) -> None:
        directory = self.socket_path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.server.start()
        self.link.start()

    def stop(self) -> None:
        self.link.stop()
        self.server.stop()


def remote_params(artifacts_prefix: str) -> dict[str, dict[str, Param]]:
    """Parameters of the operations the device agent may dispatch. Installers
    for remote installs can only come from the agent's verified artifacts."""
    def artifact(value: str) -> str | None:
        normalized = os.path.normpath(value)
        return None if normalized.startswith(artifacts_prefix) and normalized == value else "must be an agent artifact"
    app = Param(str, required=True, check=_id_check, max_len=96)
    path = Param(str, required=True, check=artifact)
    return {"apps.status": {"id": app}, "apps.launch": {"id": app}, "apps.stop": {"id": app},
            "apps.remove": {"id": app}, "apps.repair": {"id": app},
            "apps.install": {"id": app, "path": path}, "apps.upgrade": {"id": app, "path": path}}


def default_socket(environ=os.environ) -> Path:
    runtime = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "boswas" / "session.sock"


def main(argv: list[str] | None = None) -> int:
    if os.geteuid() == 0:
        sys.stderr.write("boswas-session-agent: runs as a user (systemd --user), never as root\n")
        return 4
    agent = SessionAgent(socket_path=default_socket())
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    agent.start()
    while not stop.wait(3600):
        pass
    agent.stop()
    return 0
