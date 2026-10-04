"""Remote application commands reach the user through their session agent.

Windows applications belong to a user and never run as root, so the root
agent cannot run them itself. Each user's session agent (a systemd user
service) connects to the agent's socket and registers; the kernel tells the
agent which user is on the other end (SO_PEERCRED). Application commands
are then dispatched over that connection to the active user's session
agent, which runs boswas-winapp as that user.

The session side accepts only the application operations in
REMOTE_OPERATIONS, with the same parameter checks as the Compatibility
Manager's requests.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time

from .errors import SessionUnavailable
from .localapi import PROTOCOL, ApiError, Connection, TAKEN_OVER, Request
from .outbox import Backoff

REMOTE_OPERATIONS = ("apps.install", "apps.upgrade", "apps.remove", "apps.launch", "apps.stop", "apps.repair",
                     "apps.status")
KEEPALIVE_SECONDS = 30


class SessionChannel:
    """The agent's end of one registered session agent connection."""

    def __init__(self, conn: Connection, uid: int):
        self.conn, self.uid = conn, uid
        self.connected_at = time.time()
        self._lock = threading.Lock()
        self.closed = threading.Event()
        self._seq = 0

    def dispatch(self, op: str, params: dict, timeout: float) -> dict:
        if op not in REMOTE_OPERATIONS:
            raise ApiError("UNKNOWN_OPERATION", f"{op} cannot be dispatched to a session")
        with self._lock:
            if self.closed.is_set():
                raise SessionUnavailable("the user's session agent disconnected")
            self._seq += 1
            dispatch_id = f"d{self._seq}"
            try:
                self.conn.send({"v": PROTOCOL, "id": dispatch_id, "dispatch": {"op": op, "params": params}})
                while True:
                    reply = self.conn.receive(timeout=timeout)
                    if reply is None:
                        raise SessionUnavailable("the user's session agent disconnected")
                    if reply.get("id") == dispatch_id:
                        break
            except (OSError, socket.timeout) as exc:
                self.close()
                raise SessionUnavailable(f"the user's session agent did not answer ({type(exc).__name__})") from exc
        if reply.get("ok") is True:
            result = reply.get("result")
            return result if isinstance(result, dict) else {}
        error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        raise ApiError(str(error.get("code", "FAILED"))[:64], str(error.get("message", "failed"))[:2000])

    def serve(self) -> None:
        """Keep the connection alive until it closes (runs in the server's handler thread)."""
        while not self.closed.wait(KEEPALIVE_SECONDS):
            if not self._lock.acquire(blocking=False):
                continue                          # a dispatch is in progress: the peer is alive
            try:
                self.conn.send({"v": PROTOCOL, "id": "keepalive", "ping": True})
                reply = self.conn.receive(timeout=15)
                if not reply or reply.get("pong") is not True:
                    self.close()
            except (OSError, ApiError, socket.timeout):
                self.close()
            finally:
                self._lock.release()

    def close(self) -> None:
        self.closed.set()
        self.conn.close()


def active_session_uid(run=subprocess.run) -> int | None:
    """The user of the active session on seat0 (logind), if any."""
    def query(*args: str) -> str:
        try:
            proc = run(["loginctl", *args], capture_output=True, text=True, timeout=10, check=False,
                       stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""
    session = query("show-seat", "seat0", "-p", "ActiveSession", "--value")
    if not session or not session.isalnum():
        return None
    uid = query("show-session", session, "-p", "User", "--value")
    return int(uid) if uid.isdigit() else None


class SessionRegistry:
    def __init__(self, active_uid=active_session_uid):
        self._sessions: dict[int, SessionChannel] = {}
        self._lock = threading.Lock()
        self._active_uid = active_uid

    def register(self, request: Request) -> object:
        """session.register: this connection now carries dispatches for its user."""
        channel = SessionChannel(request.connection, request.peer.uid)
        with self._lock:
            old = self._sessions.get(channel.uid)
            self._sessions[channel.uid] = channel
        if old is not None:
            old.close()
        request.connection.send({"v": PROTOCOL, "id": "register", "ok": True,
                                 "result": {"registered": True, "uid": channel.uid}})
        try:
            channel.serve()
        finally:
            with self._lock:
                if self._sessions.get(channel.uid) is channel:
                    del self._sessions[channel.uid]
        return TAKEN_OVER

    def pick(self) -> SessionChannel:
        with self._lock:
            sessions = {uid: ch for uid, ch in self._sessions.items() if not ch.closed.is_set()}
        if not sessions:
            raise SessionUnavailable("no user is logged in with a running Boswas session agent")
        uid = self._active_uid()
        if uid in sessions:
            return sessions[uid]
        if len(sessions) == 1:
            return next(iter(sessions.values()))
        raise SessionUnavailable("several users are logged in and none of them has the active session")

    def summary(self) -> dict:
        with self._lock:
            return {"connected": sum(1 for ch in self._sessions.values() if not ch.closed.is_set())}


class SessionLink(threading.Thread):
    """Session agent side: register with the device agent and serve dispatches."""

    def __init__(self, socket_path: str, execute, *, backoff: Backoff | None = None):
        super().__init__(name="boswas-session-link", daemon=True)
        self.socket_path, self.execute = socket_path, execute
        self.backoff = backoff or Backoff(base=2.0, cap=120.0)
        self.stop_event = threading.Event()
        self.connected = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._session()
                self.backoff.success()
            except (OSError, ApiError, socket.timeout):
                pass
            self.connected.clear()
            self.stop_event.wait(self.backoff.failure())

    def _session(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        conn = Connection(sock)
        try:
            conn.send({"v": PROTOCOL, "id": "register", "op": "session.register", "params": {}})
            reply = conn.receive(timeout=10)
            if not reply or reply.get("ok") is not True:
                raise ApiError("REFUSED", "registration refused")
            self.connected.set()
            self.backoff.success()
            while not self.stop_event.is_set():
                try:
                    message = conn.receive(timeout=KEEPALIVE_SECONDS * 4)
                except socket.timeout:
                    return                         # the agent stopped pinging: reconnect
                if message is None:
                    return
                if message.get("ping") is True:
                    conn.send({"v": PROTOCOL, "id": message.get("id"), "pong": True})
                    continue
                dispatch = message.get("dispatch")
                if not isinstance(dispatch, dict):
                    continue
                conn.send(self._answer(message.get("id"), dispatch))
        finally:
            conn.close()

    def _answer(self, msg_id, dispatch: dict) -> dict:
        op = dispatch.get("op")
        if op not in REMOTE_OPERATIONS:
            return {"v": PROTOCOL, "id": msg_id, "ok": False,
                    "error": {"code": "UNKNOWN_OPERATION", "message": f"{str(op)[:40]} is not a remote operation"}}
        try:
            result = self.execute(op, dispatch.get("params"))
            return {"v": PROTOCOL, "id": msg_id, "ok": True, "result": result}
        except ApiError as exc:
            return {"v": PROTOCOL, "id": msg_id, "ok": False, "error": exc.to_dict()}
        except Exception as exc:
            return {"v": PROTOCOL, "id": msg_id, "ok": False,
                    "error": {"code": "INTERNAL", "message": f"internal error ({type(exc).__name__})"}}

    def stop(self) -> None:
        self.stop_event.set()
