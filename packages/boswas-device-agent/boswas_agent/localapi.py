"""Local management API: typed JSON operations over a Unix socket.

Framing: one JSON object per line (at most 1 MiB).

  request   {"v": 1, "id": 7, "op": "agent.status", "params": {}}
  response  {"v": 1, "id": 7, "ok": true, "result": {...}}
            {"v": 1, "id": 7, "ok": false, "error": {"code": "FORBIDDEN", "message": "..."}}

Who is calling is never taken from the request: the server reads the
peer's user ID from the kernel (SO_PEERCRED) and authorises every operation
by role:

  any       any local user (read-only status)
  admin     root only (enrollment, maintenance, sync)
  session   a user's own session agent registering for remote application
            commands (uid >= 1000; the connection then carries dispatches)
  owner     the socket's owning user only (session agent API)

Every operation declares its parameters; unknown or ill-typed parameters
are rejected before any handler runs. There is no operation that takes a
command line, a script or a program path to execute.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

PROTOCOL = 1
MAX_MESSAGE = 1024 * 1024
ROLES = ("any", "admin", "session", "owner")


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    gid: int


def peer_credentials(sock: socket.socket) -> Peer:
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", data)
    return Peer(pid, uid, gid)


@dataclass(frozen=True)
class Param:
    kind: type | tuple
    required: bool = False
    check: Callable[[object], str | None] | None = None
    max_len: int = 4096


@dataclass
class Operation:
    name: str
    handler: Callable
    role: str = "any"
    params: dict[str, Param] = field(default_factory=dict)


@dataclass
class Request:
    peer: Peer
    op: str
    params: dict
    connection: "Connection"


def validate_params(spec: dict[str, Param], params: object) -> dict:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ApiError("BAD_REQUEST", "params must be an object")
    for key in params:
        if key not in spec:
            raise ApiError("BAD_REQUEST", f"unknown parameter {str(key)[:40]!r}")
    out = {}
    for key, p in spec.items():
        if key not in params or params[key] is None:
            if p.required:
                raise ApiError("BAD_REQUEST", f"missing parameter {key!r}")
            continue
        value = params[key]
        kinds = p.kind if isinstance(p.kind, tuple) else (p.kind,)
        if isinstance(value, bool) and bool not in kinds:
            raise ApiError("BAD_REQUEST", f"parameter {key!r} has the wrong type")
        if not isinstance(value, kinds):
            raise ApiError("BAD_REQUEST", f"parameter {key!r} has the wrong type")
        if isinstance(value, str) and (len(value) > p.max_len or "\0" in value):
            raise ApiError("BAD_REQUEST", f"parameter {key!r} is too long or contains NUL")
        if p.check:
            problem = p.check(value)
            if problem:
                raise ApiError("BAD_REQUEST", f"parameter {key!r}: {problem}")
        out[key] = value
    return out


def encode(message: dict) -> bytes:
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > MAX_MESSAGE:
        raise ApiError("TOO_LARGE", "message too large")
    return data


class Connection:
    """One socket with line framing; safe for one reader and serialised writers."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buffer = b""
        self._write_lock = threading.Lock()

    def send(self, message: dict) -> None:
        data = encode(message)
        with self._write_lock:
            self.sock.sendall(data)

    def receive(self, timeout: float | None = None) -> dict | None:
        """The next message, or None at end of stream."""
        self.sock.settimeout(timeout)
        while b"\n" not in self._buffer:
            if len(self._buffer) > MAX_MESSAGE:
                raise ApiError("TOO_LARGE", "message too large")
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("BAD_REQUEST", "not a JSON message") from None
        if not isinstance(message, dict):
            raise ApiError("BAD_REQUEST", "a message must be a JSON object")
        return message

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class ApiServer:
    """Threaded Unix-socket server for a table of operations."""

    def __init__(self, path: Path, operations: dict[str, Operation], *, owner_uid: int | None = None,
                 mode: int = 0o666, min_session_uid: int = 1000):
        self.path = Path(path)
        self.operations = operations
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self.mode = mode
        self.min_session_uid = min_session_uid
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        self._thread: threading.Thread | None = None

    def authorize(self, peer: Peer, role: str) -> bool:
        if role == "any":
            return True
        if role == "admin":
            return peer.uid == 0
        if role == "owner":
            return peer.uid == self.owner_uid
        if role == "session":
            return peer.uid >= self.min_session_uid and peer.uid != 65534
        return False

    def handle(self, conn: Connection, peer: Peer, message: dict) -> dict | None:
        """Process one request; returns the response (None if the connection was taken over)."""
        msg_id = message.get("id")
        if not isinstance(msg_id, (int, str)) or isinstance(msg_id, bool) or len(str(msg_id)) > 64:
            msg_id = None
        try:
            if message.get("v") != PROTOCOL:
                raise ApiError("BAD_REQUEST", f"protocol version {PROTOCOL} required")
            name = message.get("op")
            op = self.operations.get(name) if isinstance(name, str) else None
            if op is None:
                raise ApiError("UNKNOWN_OPERATION", f"unknown operation {str(name)[:60]!r}")
            if not self.authorize(peer, op.role):
                raise ApiError("FORBIDDEN", f"{op.name} is not permitted for this user")
            params = validate_params(op.params, message.get("params"))
            result = op.handler(Request(peer, op.name, params, conn))
            if result is TAKEN_OVER:
                return None
            return {"v": PROTOCOL, "id": msg_id, "ok": True, "result": result}
        except ApiError as exc:
            return {"v": PROTOCOL, "id": msg_id, "ok": False, "error": exc.to_dict()}
        except Exception as exc:          # never leak a traceback with local state
            return {"v": PROTOCOL, "id": msg_id, "ok": False,
                    "error": {"code": "INTERNAL", "message": f"internal error ({type(exc).__name__})"}}

    def _make_handler(self):
        api = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                conn = Connection(self.request)
                try:
                    peer = peer_credentials(self.request)
                except OSError:
                    return
                try:
                    while True:
                        try:
                            message = conn.receive(timeout=300)
                        except ApiError as exc:
                            conn.send({"v": PROTOCOL, "id": None, "ok": False, "error": exc.to_dict()})
                            return
                        except (OSError, socket.timeout):
                            return
                        if message is None:
                            return
                        response = api.handle(conn, peer, message)
                        if response is None:
                            return                   # a session channel owned the connection until it closed
                        try:
                            conn.send(response)
                        except ApiError as exc:
                            conn.send({"v": PROTOCOL, "id": response.get("id"), "ok": False,
                                       "error": exc.to_dict()})
                finally:
                    conn.close()
        return Handler

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        old = os.umask(0o177)
        try:
            server = socketserver.ThreadingUnixStreamServer(str(self.path), self._make_handler())
        finally:
            os.umask(old)
        server.daemon_threads = True
        os.chmod(self.path, self.mode)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="boswas-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


TAKEN_OVER = object()


class ApiClient:
    def __init__(self, path: Path | str, timeout: float = 30):
        self.path, self.timeout = str(path), timeout
        self._next = 0

    def call(self, op: str, timeout: float | None = None, **params) -> dict:
        """Run one operation; raises ApiError (refused/failed) or ConnectionError (no service)."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout or self.timeout)
            sock.connect(self.path)
        except OSError as exc:
            sock.close()
            raise ConnectionError(f"the service socket {self.path} is not available ({exc.strerror})") from exc
        conn = Connection(sock)
        try:
            self._next += 1
            conn.send({"v": PROTOCOL, "id": self._next, "op": op, "params": params})
            reply = conn.receive(timeout=timeout or self.timeout)
        except socket.timeout as exc:
            raise ApiError("TIMEOUT", f"{op} did not answer in time") from exc
        except OSError as exc:
            raise ConnectionError(f"the service closed the connection ({exc})") from exc
        finally:
            conn.close()
        if not isinstance(reply, dict):
            raise ConnectionError("the service closed the connection")
        if reply.get("ok") is True:
            result = reply.get("result")
            return result if isinstance(result, dict) else {"value": result}
        error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        raise ApiError(str(error.get("code", "FAILED")), str(error.get("message", "request failed")))
