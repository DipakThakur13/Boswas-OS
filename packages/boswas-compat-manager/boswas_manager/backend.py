"""The Compatibility Manager's only connection to the system: two local sockets.

  session agent  $XDG_RUNTIME_DIR/boswas/session.sock (owner-only)
                 every application operation; it runs the WinCompat backend
                 as this user, so policy, manifests, bubblewrap and AppArmor
                 are enforced exactly as on the command line
  device agent   /run/boswas-agent/agent.sock
                 read-only status (device state, Control Plane, policy,
                 remote commands); only operations open to any user are used

The GUI has no other way to act: it does not start processes or read
application files. Methods here map one-to-one to API operations and turn
transport problems into two exceptions the widgets understand:
BackendUnavailable (the service is not running) and BackendRefused (the
service answered with an error code and a message for the user).

All methods block; the widgets call them from worker threads.
"""

from __future__ import annotations

import os

from boswas_agent.localapi import ApiClient, ApiError

AGENT_SOCKET = "/run/boswas-agent/agent.sock"
DEFAULT_TIMEOUT = 30.0
# Inspect and install hash the installer first (up to the policy's 4 GiB
# limit); the session agent allows the WinCompat backend 900 s for that.
INSPECT_TIMEOUT = 960.0
# Launch waits for the program to settle; stop waits for the sandbox to end;
# system status runs the runtime check and asks the device agent.
SLOW_TIMEOUT = 120.0
AGENT_TIMEOUT = 10.0
MAX_LOG_LINES = 5000

SESSION = "session"
AGENT = "agent"

# Operations this client may send. The device agent list is read-only on
# purpose: the Compatibility Manager never administers the device.
SESSION_OPERATIONS = frozenset({
    "apps.list", "apps.status", "apps.inspect", "apps.install", "apps.upgrade", "apps.repair", "apps.remove",
    "apps.launch", "apps.stop", "apps.logs", "apps.clear_logs", "catalog.list", "system.status", "jobs.get",
    "jobs.list", "events.list",
})
AGENT_OPERATIONS = frozenset({"agent.status", "policy.status", "commands.list"})


class BackendError(Exception):
    """Base class of the errors the widgets display."""


class BackendUnavailable(BackendError):
    """The service socket is missing or the service closed the connection."""

    def __init__(self, service: str, message: str):
        super().__init__(message)
        self.service, self.message = service, message


class BackendRefused(BackendError):
    """The service refused or failed the operation (code and message from the service)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def session_socket_path(environ=None) -> str:
    """Where the user's session agent listens (same rule as the session agent itself)."""
    environ = os.environ if environ is None else environ
    runtime = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "boswas", "session.sock")


def _list(result: dict, key: str) -> list[dict]:
    items = result.get(key)
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


class Backend:
    """Typed calls to the session agent and the device agent (blocking)."""

    def __init__(self, session_socket: str | None = None, agent_socket: str = AGENT_SOCKET, *,
                 client_factory=ApiClient, timeout: float = DEFAULT_TIMEOUT):
        self.session_socket = session_socket or session_socket_path()
        self.agent_socket = agent_socket
        self._session = client_factory(self.session_socket, timeout=timeout)
        self._agent = client_factory(agent_socket, timeout=AGENT_TIMEOUT)

    # --- transport ---------------------------------------------------------------------------
    @staticmethod
    def _call(client, service: str, op: str, timeout: float | None, params: dict) -> dict:
        params = {key: value for key, value in params.items() if value is not None}
        try:
            result = client.call(op, timeout=timeout, **params)
        except ApiError as exc:
            raise BackendRefused(str(exc.code), str(exc.message)) from None
        except ConnectionError as exc:
            raise BackendUnavailable(service, str(exc)) from None
        except OSError as exc:
            raise BackendUnavailable(service, f"the {service} service could not be reached ({exc})") from None
        if not isinstance(result, dict):
            raise BackendRefused("BAD_RESPONSE", f"{op} returned an unexpected answer")
        return result

    def _session_call(self, op: str, timeout: float | None = None, **params) -> dict:
        if op not in SESSION_OPERATIONS:
            raise ValueError(f"{op} is not a session operation")
        return self._call(self._session, SESSION, op, timeout, params)

    def _agent_call(self, op: str, **params) -> dict:
        if op not in AGENT_OPERATIONS:
            raise ValueError(f"{op} is not a read-only device agent operation")
        return self._call(self._agent, AGENT, op, None, params)

    @staticmethod
    def _job_id(result: dict) -> str:
        job_id = result.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise BackendRefused("BAD_RESPONSE", "the session service did not return a job")
        return job_id

    # --- applications (session agent) ---------------------------------------------------------
    def list_apps(self) -> list[dict]:
        return _list(self._session_call("apps.list"), "applications")

    def app_status(self, app_id: str) -> dict:
        return self._session_call("apps.status", id=app_id)

    def inspect(self, path: str, app_id: str | None = None, name: str | None = None) -> dict:
        """What installing would decide; never installs or runs anything."""
        return self._session_call("apps.inspect", timeout=INSPECT_TIMEOUT, path=path, id=app_id, name=name)

    def install(self, path: str, app_id: str | None = None, name: str | None = None) -> str:
        return self._job_id(self._session_call("apps.install", timeout=INSPECT_TIMEOUT, path=path, id=app_id,
                                               name=name))

    def upgrade(self, app_id: str, path: str) -> str:
        return self._job_id(self._session_call("apps.upgrade", timeout=INSPECT_TIMEOUT, id=app_id, path=path))

    def repair(self, app_id: str) -> str:
        return self._job_id(self._session_call("apps.repair", id=app_id))

    def remove(self, app_id: str) -> str:
        return self._job_id(self._session_call("apps.remove", id=app_id))

    def job(self, job_id: str) -> dict:
        return self._session_call("jobs.get", job_id=job_id)

    def jobs(self) -> list[dict]:
        return _list(self._session_call("jobs.list"), "jobs")

    def launch(self, app_id: str, display: str | None = None, xauthority: str | None = None,
               wayland_display: str | None = None) -> dict:
        """Start an application in its sandbox; the display values tell the session agent which session to use."""
        return self._session_call("apps.launch", timeout=SLOW_TIMEOUT, id=app_id, display=display,
                                  xauthority=xauthority, wayland_display=wayland_display)

    def stop(self, app_id: str) -> dict:
        return self._session_call("apps.stop", timeout=SLOW_TIMEOUT, id=app_id)

    def logs(self, app_id: str, kind: str | None = None, lines: int = 1000) -> dict:
        return self._session_call("apps.logs", id=app_id, kind=kind, lines=max(0, min(int(lines), MAX_LOG_LINES)))

    def clear_logs(self, app_id: str) -> dict:
        return self._session_call("apps.clear_logs", id=app_id)

    def catalog(self) -> dict:
        return self._session_call("catalog.list")

    def system_status(self) -> dict:
        return self._session_call("system.status", timeout=SLOW_TIMEOUT)

    def events(self, limit: int = 50) -> list[dict]:
        return _list(self._session_call("events.list", limit=limit), "events")

    # --- device (device agent, read-only) ------------------------------------------------------
    def agent_status(self) -> dict:
        return self._agent_call("agent.status")

    def policy_status(self) -> dict:
        return self._agent_call("policy.status")

    def remote_commands(self, limit: int = 20) -> list[dict]:
        return _list(self._agent_call("commands.list", limit=limit), "commands")
