"""HTTPS client for the Control Plane device API (v1), mutual TLS.

The server is verified against the CA pinned in device.conf
(CONTROL_PLANE_CA), with host name checking; after enrollment the device
presents its certificate. Responses are size-limited, and every failure is
either ConnectivityError (retry later) or ControlPlaneRejected (the Control
Plane said no).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import ssl
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import __version__
from .errors import ConnectivityError, ControlPlaneRejected
from .interfaces import ControlPlaneClient, EnrollmentResult, HeartbeatResponse
from .models import EnrollmentRequest

MAX_RESPONSE = 8 * 1024 * 1024
API = "/api/v1"
SEND_ROUTES = {
    "status": ("POST", "status"),
    "compliance": ("POST", "compliance"),
    "inventory": ("PUT", "inventory"),
    "event": ("POST", "events"),
}


class HttpsControlPlaneClient(ControlPlaneClient):
    def __init__(self, base_url: str, context: ssl.SSLContext, device_id: str, *, timeout: float = 30):
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("the Control Plane URL must be https://")
        self.host, self.port = parts.hostname, parts.port or 443
        self.prefix = parts.path.rstrip("/")
        self.context, self.device_id, self.timeout = context, device_id, timeout

    # --- transport -------------------------------------------------------------------
    def _connection(self) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(self.host, self.port, context=self.context, timeout=self.timeout)

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": f"boswas-device-agent/{__version__}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn = self._connection()
        try:
            conn.request(method, self.prefix + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE + 1)
            status = resp.status
        except (OSError, ssl.SSLError, socket.timeout, http.client.HTTPException) as exc:
            raise ConnectivityError(f"Control Plane unreachable: {type(exc).__name__}: {exc}"[:300]) from exc
        finally:
            conn.close()
        if len(raw) > MAX_RESPONSE:
            raise ConnectivityError("Control Plane response too large")
        doc = None
        if raw:
            try:
                doc = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                if status < 400:
                    raise ConnectivityError("Control Plane returned a response that is not JSON") from None
        if status >= 400:
            error = (doc or {}).get("error") if isinstance(doc, dict) else None
            code = error.get("code") if isinstance(error, dict) and isinstance(error.get("code"), str) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise ControlPlaneRejected(f"Control Plane refused {method} {path}: {status} {str(message or '')[:200]}",
                                       code=code or f"HTTP_{status}", status=status)
        if doc is not None and not isinstance(doc, dict):
            raise ConnectivityError("Control Plane returned an unexpected document")
        return status, doc

    def _device(self, suffix: str) -> str:
        return f"{API}/devices/{quote(self.device_id, safe='')}/{suffix}"

    # --- API -----------------------------------------------------------------------------
    def enroll(self, request: EnrollmentRequest) -> EnrollmentResult:
        _, doc = self._request("POST", f"{API}/enroll", request.to_dict())
        doc = doc or {}
        try:
            return EnrollmentResult(device_id=doc["device_id"], certificate_pem=doc["certificate_pem"],
                                    control_plane_url=doc.get("control_plane_url") or "",
                                    policy_version=doc.get("policy_version"), profile=doc.get("profile"),
                                    policy_public_key_pem=doc["policy_public_key_pem"],
                                    heartbeat_seconds=int(doc.get("heartbeat_seconds") or 300))
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectivityError("unexpected enrollment response") from exc

    def heartbeat(self, heartbeat: dict) -> HeartbeatResponse:
        _, doc = self._request("POST", self._device("heartbeat"), heartbeat)
        doc = doc or {}

        def number(key, default):
            value = doc.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else default
        policy = doc.get("policy_version")
        return HeartbeatResponse(accepted=doc.get("accepted") is True,
                                 policy_version_available=policy if isinstance(policy, str) else None,
                                 commands_pending=number("commands_pending", 0),
                                 next_heartbeat_seconds=max(30, min(number("next_heartbeat_seconds", 300), 86400)),
                                 inventory_requested=doc.get("inventory_requested") is True)

    def send(self, kind: str, document: dict) -> None:
        if kind == "result":
            path = self._device(f"commands/{quote(document['command_id'], safe='')}/result")
            self._request("POST", path, document)
            return
        method, suffix = SEND_ROUTES[kind]
        self._request(method, self._device(suffix), document)

    def fetch_policy(self) -> dict | None:
        status, doc = self._request("GET", self._device("policy"))
        if status == 204 or not doc:
            return None
        return doc.get("envelope") if isinstance(doc.get("envelope"), dict) else None

    def fetch_commands(self) -> list[dict]:
        _, doc = self._request("GET", self._device("commands/pending"))
        commands = (doc or {}).get("commands")
        return [c for c in commands if isinstance(c, dict)] if isinstance(commands, list) else []

    def acknowledge(self, command_id: str, status: str) -> None:
        self._request("POST", self._device(f"commands/{quote(command_id, safe='')}/ack"), {"status": status})

    def download_artifact(self, sha256: str, destination: Path, max_bytes: int) -> None:
        """Stream an installer to destination, checking size and SHA-256 as it arrives."""
        conn = self._connection()
        destination = Path(destination)
        tmp = destination.with_name(destination.name + ".part")
        digest, size = hashlib.sha256(), 0
        try:
            conn.request("GET", f"{self.prefix}{API}/artifacts/{quote(sha256, safe='')}",
                         headers={"User-Agent": f"boswas-device-agent/{__version__}"})
            resp = conn.getresponse()
            if resp.status != 200:
                resp.read(65536)
                raise ControlPlaneRejected(f"artifact download refused: {resp.status}", code=f"HTTP_{resp.status}",
                                           status=resp.status)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ControlPlaneRejected("artifact larger than announced", code="ARTIFACT_TOO_LARGE")
                    digest.update(chunk)
                    out.write(chunk)
        except (OSError, ssl.SSLError, socket.timeout, http.client.HTTPException) as exc:
            tmp.unlink(missing_ok=True)
            raise ConnectivityError(f"artifact download failed: {type(exc).__name__}") from exc
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            conn.close()
        if digest.hexdigest() != sha256:
            tmp.unlink(missing_ok=True)
            raise ControlPlaneRejected("artifact does not match its SHA-256", code="ARTIFACT_MISMATCH")
        os.replace(tmp, destination)
