"""HTTP API /api/v1 and the dashboard's static files.

Every route declares who may call it:

  public     no credentials (health only)
  enroll     no client certificate; the body carries a one-time token
  device     mutual TLS: the device certificate must belong to the device
             named in the path, and the device must be active
  artifact   mutual TLS: any active device (installer downloads)
  viewer     operator token, any role
  operator   operator or admin
  admin      admin only

Two listeners serve disjoint parts of the API (LISTENERS): the device port
asks for a client certificate and serves enrollment and the device routes;
the operator port never asks for one (so browsers show no certificate
prompt) and serves the dashboard and the operator routes. Both answer
health. A route requested on the other port is refused with WRONG_PORT.

Requests: JSON bodies only (Content-Type application/json, at most 4 MiB),
except installer uploads (application/octet-stream, streamed to disk while
hashed). There is no endpoint that accepts a command line, script or code:
commands are created from a type and an application ID.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .auth import FailureLimiter, OperatorAuthenticator, Principal
from .service import ControlPlane, ServiceError

log = logging.getLogger("boswas-control-plane")

MAX_JSON = 4 * 1024 * 1024
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
APP = r"[a-z0-9][a-z0-9.-]{1,95}"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                    "Strict-Transport-Security": "max-age=31536000", "Cross-Origin-Opener-Policy": "same-origin",
                    "Cross-Origin-Resource-Policy": "same-origin"}
DEVICE, OPERATOR = "device", "operator"
LISTENERS = {"public": {DEVICE, OPERATOR}, "enroll": {DEVICE}, "device": {DEVICE}, "artifact": {DEVICE},
             "viewer": {OPERATOR}, "operator": {OPERATOR}, "admin": {OPERATOR}}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


@dataclass
class Request:
    method: str
    path: str
    query: dict
    headers: dict                       # lower-case names
    client: str
    certificate: bytes | None           # DER of a verified client certificate
    read: Callable[[int], bytes]        # body reader
    length: int | None
    listener: str = DEVICE              # the port it arrived on (DEVICE or OPERATOR)


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict = field(default_factory=dict)
    file: Path | None = None            # streamed instead of body


def json_response(status: int, doc: object, **headers) -> Response:
    return Response(status, (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8"), headers=headers)


def error(status: int, code: str, message: str, details: list | None = None, **headers) -> Response:
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = [str(d)[:300] for d in details[:20]]
    return json_response(status, body, **headers)


@dataclass
class Route:
    method: str
    pattern: re.Pattern
    handler: Callable
    access: str


class Api:
    def __init__(self, cp: ControlPlane, authenticator: OperatorAuthenticator, *, dashboard: Path | None = None,
                 limiter: FailureLimiter | None = None, max_upload: int | None = None):
        self.cp, self.authenticator = cp, authenticator
        self.dashboard = Path(dashboard) if dashboard else None
        self.limiter = limiter or FailureLimiter()
        self.max_upload = max_upload or cp.max_artifact_bytes
        self.routes: list[Route] = []
        self._register()

    def route(self, method: str, path: str, access: str):
        if access not in LISTENERS:
            raise ValueError(f"unknown access level {access!r}")

        def wrap(fn):
            self.routes.append(Route(method, re.compile(f"^{path}$"), fn, access))
            return fn
        return wrap

    # --- dispatch --------------------------------------------------------------------------
    def dispatch(self, req: Request) -> tuple[Response, str]:
        """(response, actor for the access log)."""
        if req.method == "GET" and req.path in STATIC and self.dashboard and req.listener == OPERATOR:
            return self._static(req.path), "-"
        everywhere = [(r, m) for r in self.routes for m in [r.pattern.match(req.path)] if m]
        matched = [(r, m) for r, m in everywhere if req.listener in LISTENERS[r.access]]
        if not matched:
            if everywhere:
                other = OPERATOR if req.listener == DEVICE else DEVICE
                return error(404, "WRONG_PORT", f"this endpoint is served on the {other} port"), "-"
            return error(404, "NOT_FOUND", "no such endpoint"), "-"
        found = next(((r, m) for r, m in matched if r.method == req.method), None)
        if found is None:
            return error(405, "METHOD_NOT_ALLOWED", "method not allowed",
                         Allow=", ".join(sorted({r.method for r, _ in matched}))), "-"
        route, match = found
        actor = "-"
        try:
            args = {k: unquote(v) for k, v in match.groupdict().items()}
            if route.access == "device":
                device = self.cp.device_for_certificate(req.certificate, args["device_id"])
                actor = f"device:{device['device_id']}"
                return route.handler(req, device=device, **args), actor
            if route.access in ("viewer", "operator", "admin"):
                if self.limiter.blocked(req.client):
                    return error(429, "TOO_MANY_ATTEMPTS", "too many failed authentication attempts"), actor
                principal = self.authenticator.authenticate(req.headers.get("authorization"))
                if principal is None:
                    self.limiter.failure(req.client)
                    return error(401, "UNAUTHORIZED", "an operator token is required",
                                 **{"WWW-Authenticate": 'Bearer realm="boswas"'}), actor
                actor = principal.actor
                if not principal.allows(route.access):
                    return error(403, "FORBIDDEN", f"this needs the {route.access} role"), actor
                return route.handler(req, principal=principal, **args), actor
            return route.handler(req, **args), actor
        except ServiceError as exc:
            return error(exc.status, exc.code, exc.message, exc.details), actor
        except Exception as exc:                 # never leak internals
            log.exception("internal error on %s %s", req.method, req.path)
            return error(500, "INTERNAL", f"internal error ({type(exc).__name__})"), actor

    def _static(self, path: str) -> Response:
        name, ctype = STATIC[path]
        file = self.dashboard / name
        if not file.is_file():
            return error(404, "NOT_FOUND", "dashboard not installed")
        headers = {"Content-Security-Policy": CSP, "Cache-Control": "no-cache"}
        return Response(200, file.read_bytes(), ctype, headers)

    # --- request helpers ------------------------------------------------------------------------
    @staticmethod
    def body(req: Request) -> object:
        ctype = req.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ServiceError("UNSUPPORTED_MEDIA_TYPE", "send application/json", status=415)
        if req.length is None:
            raise ServiceError("LENGTH_REQUIRED", "Content-Length is required", status=411)
        if req.length > MAX_JSON:
            raise ServiceError("TOO_LARGE", "request body too large", status=413)
        raw = req.read(req.length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ServiceError("INVALID_JSON", "the body is not valid JSON", status=400) from None

    @staticmethod
    def q(req: Request, name: str, pattern: str | None = None) -> str | None:
        value = (req.query.get(name) or [None])[0]
        if value is not None and pattern and not re.fullmatch(pattern, value):
            raise ServiceError("INVALID_QUERY", f"invalid {name}", status=400)
        return value

    @staticmethod
    def limit(req: Request, default: int = 100) -> int:
        value = (req.query.get("limit") or [str(default)])[0]
        if not value.isdigit() or not 1 <= int(value) <= 1000:
            raise ServiceError("INVALID_QUERY", "limit must be 1 to 1000", status=400)
        return int(value)

    def _upload(self, req: Request, principal: Principal) -> Response:
        ctype = req.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/octet-stream":
            raise ServiceError("UNSUPPORTED_MEDIA_TYPE", "send the installer as application/octet-stream", status=415)
        if req.length is None:
            raise ServiceError("LENGTH_REQUIRED", "Content-Length is required", status=411)
        if req.length > self.max_upload:
            raise ServiceError("TOO_LARGE", "installer larger than the upload limit", status=413)
        name = unquote(req.headers.get("x-boswas-file-name", ""))
        self.cp.artifacts_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        tmp = self.cp.artifacts_dir / f".upload-{secrets.token_hex(8)}"
        digest, remaining = hashlib.sha256(), req.length
        try:
            with open(tmp, "wb") as out:
                while remaining:
                    chunk = req.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ServiceError("INCOMPLETE_UPLOAD", "the upload ended early", status=400)
                    remaining -= len(chunk)
                    digest.update(chunk)
                    out.write(chunk)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        artifact = self.cp.add_artifact(principal, tmp, digest.hexdigest(), req.length, name)
        return json_response(201, artifact)

    # --- routes ---------------------------------------------------------------------------------
    def _register(self) -> None:
        cp, r = self.cp, self.route
        D = f"/api/v1/devices/(?P<device_id>{UUID})"

        @r("GET", "/api/v1/health", "public")
        def health(req):
            return json_response(200, {"status": "ok", "version": __version__, "api": "v1", "time": cp.stamp()})

        # Devices -------------------------------------------------------------------------------
        @r("POST", "/api/v1/enroll", "enroll")
        def enroll(req):
            return json_response(201, cp.enroll(self.body(req)))

        @r("POST", f"{D}/heartbeat", "device")
        def heartbeat(req, device, device_id):
            return json_response(200, cp.heartbeat(device, self.body(req)))

        @r("PUT", f"{D}/inventory", "device")
        def put_inventory(req, device, device_id):
            return json_response(200, cp.store_report(device, "inventory", self.body(req)))

        @r("POST", f"{D}/status", "device")
        def post_status(req, device, device_id):
            return json_response(200, cp.store_report(device, "status", self.body(req)))

        @r("POST", f"{D}/compliance", "device")
        def post_compliance(req, device, device_id):
            return json_response(200, cp.store_report(device, "compliance", self.body(req)))

        @r("POST", f"{D}/events", "device")
        def post_events(req, device, device_id):
            return json_response(200, cp.device_events(device, self.body(req)))

        @r("GET", f"{D}/commands/pending", "device")
        def pending(req, device, device_id):
            return json_response(200, cp.pending_commands(device))

        @r("POST", f"{D}/commands/(?P<command_id>{UUID})/ack", "device")
        def ack(req, device, device_id, command_id):
            return json_response(200, cp.acknowledge(device, command_id, self.body(req)))

        @r("POST", f"{D}/commands/(?P<command_id>{UUID})/result", "device")
        def result(req, device, device_id, command_id):
            return json_response(200, cp.command_result(device, command_id, self.body(req)))

        @r("GET", f"{D}/policy", "device")
        def policy(req, device, device_id):
            envelope = cp.policy_envelope(device)
            return json_response(200, {"envelope": envelope}) if envelope else Response(204)

        @r("GET", "/api/v1/artifacts/(?P<sha256>[0-9a-f]{64})", "artifact")
        def artifact(req, sha256):
            # Device authentication is checked here because the path names no device.
            if not req.certificate:
                raise ServiceError("CERTIFICATE_REQUIRED", "a device certificate is required", status=401)
            from .pki import fingerprint_der
            device = cp.store.one("SELECT * FROM devices WHERE certificate_fingerprint = ?",
                                  (fingerprint_der(req.certificate),))
            if device is None or device["status"] != "active":
                raise ServiceError("UNKNOWN_DEVICE", "unknown device certificate", status=403)
            path, row = cp.artifact_for_device(device, sha256)
            return Response(200, content_type="application/octet-stream", file=path,
                            headers={"Content-Disposition": "attachment"})

        # Operators ------------------------------------------------------------------------------
        @r("GET", "/api/v1/me", "viewer")
        def me(req, principal):
            return json_response(200, {"actor": principal.actor, "role": principal.role})

        @r("GET", "/api/v1/summary", "viewer")
        def summary(req, principal):
            return json_response(200, cp.summary())

        @r("GET", "/api/v1/devices", "viewer")
        def devices(req, principal):
            return json_response(200, {"devices": cp.list_devices(
                status=self.q(req, "status", "active|retired"),
                connection=self.q(req, "connection", "online|offline|never"))})

        @r("GET", D, "viewer")
        def device(req, principal, device_id):
            return json_response(200, cp.get_device(device_id))

        @r("PATCH", D, "admin")
        def update_device(req, principal, device_id):
            return json_response(200, cp.update_device(principal, device_id, self.body(req)))

        @r("POST", f"{D}/retire", "admin")
        def retire(req, principal, device_id):
            return json_response(200, cp.retire_device(principal, device_id))

        @r("GET", f"{D}/status", "viewer")
        def device_status(req, principal, device_id):
            return json_response(200, cp.device_status(device_id))

        @r("GET", f"{D}/inventory", "viewer")
        def device_inventory(req, principal, device_id):
            return json_response(200, cp.device_inventory(device_id))

        @r("GET", f"{D}/applications", "viewer")
        def device_applications(req, principal, device_id):
            return json_response(200, cp.device_applications(device_id))

        @r("GET", f"{D}/commands", "viewer")
        def device_commands(req, principal, device_id):
            cp.get_device_row(device_id)
            return json_response(200, {"commands": cp.list_commands(device_id=device_id, limit=self.limit(req))})

        @r("POST", f"{D}/commands", "operator")
        def create_command(req, principal, device_id):
            doc, created = cp.create_command(principal, device_id, self.body(req))
            return json_response(201 if created else 200, {**doc, "duplicate": not created})

        @r("GET", f"{D}/commands/(?P<command_id>{UUID})", "viewer")
        def get_command(req, principal, device_id, command_id):
            return json_response(200, cp.get_command(device_id, command_id))

        @r("POST", f"{D}/commands/(?P<command_id>{UUID})/cancel", "operator")
        def cancel(req, principal, device_id, command_id):
            return json_response(200, cp.cancel_command(principal, device_id, command_id))

        @r("GET", f"{D}/events", "viewer")
        def device_events(req, principal, device_id):
            cp.get_device_row(device_id)
            return json_response(200, {"events": cp.events(device_id=device_id, limit=self.limit(req))})

        @r("GET", "/api/v1/commands", "viewer")
        def commands_list(req, principal):
            return json_response(200, {"commands": cp.list_commands(
                status=self.q(req, "status", "QUEUED|SENT|ACKNOWLEDGED|RUNNING|SUCCEEDED|FAILED|EXPIRED|CANCELLED"),
                limit=self.limit(req))})

        @r("GET", "/api/v1/applications", "viewer")
        def applications(req, principal):
            return json_response(200, {"applications": cp.list_applications()})

        @r("POST", "/api/v1/applications", "admin")
        def add_application(req, principal):
            return json_response(201, cp.upsert_application(principal, self.body(req)))

        @r("GET", f"/api/v1/applications/(?P<app_id>{APP})", "viewer")
        def application(req, principal, app_id):
            return json_response(200, cp.get_application(app_id))

        @r("DELETE", f"/api/v1/applications/(?P<app_id>{APP})", "admin")
        def deprecate(req, principal, app_id):
            return json_response(200, cp.deprecate_application(principal, app_id))

        @r("GET", "/api/v1/artifacts", "viewer")
        def artifacts(req, principal):
            return json_response(200, {"artifacts": cp.list_artifacts()})

        @r("POST", "/api/v1/artifacts", "admin")
        def upload(req, principal):
            return self._upload(req, principal)

        @r("GET", "/api/v1/policies", "viewer")
        def policies(req, principal):
            return json_response(200, {"policies": cp.list_policies()})

        @r("POST", "/api/v1/policies", "admin")
        def publish(req, principal):
            return json_response(201, cp.publish_policy(principal, self.body(req)))

        @r("GET", "/api/v1/policies/(?P<name>[a-z0-9][a-z0-9-]{0,31})", "viewer")
        def policy_detail(req, principal, name):
            return json_response(200, cp.get_policy(name))

        @r("GET", "/api/v1/events", "viewer")
        def events(req, principal):
            before = self.q(req, "before", r"\d{1,12}")
            return json_response(200, {"events": cp.events(
                device_id=self.q(req, "device_id", UUID), event_type=self.q(req, "type", r"[A-Z_]{3,40}"),
                before_id=int(before) if before else None, limit=self.limit(req))})

        @r("GET", "/api/v1/events/verify", "admin")
        def verify(req, principal):
            return json_response(200, cp.verify_events())

        @r("GET", "/api/v1/enrollment-tokens", "admin")
        def tokens(req, principal):
            return json_response(200, {"tokens": cp.list_enrollment_tokens()})

        @r("POST", "/api/v1/enrollment-tokens", "admin")
        def create_token(req, principal):
            body = self.body(req)
            if not isinstance(body, dict) or set(body) - {"profile", "policy_name", "ttl_hours", "allow_ephemeral",
                                                          "max_uses", "description", "device_id"}:
                raise ServiceError("INVALID_REQUEST", "unknown token field", status=400)
            return json_response(201, cp.create_enrollment_token(principal, **body))

        @r("POST", "/api/v1/enrollment-tokens/(?P<token_id>[0-9a-f]{12})/revoke", "admin")
        def revoke_token(req, principal, token_id):
            return json_response(200, cp.revoke_enrollment_token(principal, token_id))

        @r("GET", "/api/v1/operators", "admin")
        def operators(req, principal):
            return json_response(200, {"operators": cp.list_operators()})

        @r("POST", "/api/v1/operators", "admin")
        def add_operator(req, principal):
            body = self.body(req)
            if not isinstance(body, dict) or set(body) != {"name", "role"}:
                raise ServiceError("INVALID_REQUEST", "name and role are required", status=400)
            return json_response(201, cp.add_operator(principal.actor, body["name"], body["role"]))

        @r("POST", "/api/v1/operators/(?P<name>[a-z][a-z0-9._-]{1,31})/disable", "admin")
        def disable_operator(req, principal, name):
            if principal.actor == f"operator:{name}":
                raise ServiceError("REFUSED", "you cannot disable your own account", status=409)
            return json_response(200, cp.disable_operator(principal.actor, name))


def parse_target(target: str) -> tuple[str, dict]:
    parts = urlsplit(target)
    return parts.path, parse_qs(parts.query, max_num_fields=20)


def access_line(req: Request, response: Response, actor: str, started: float) -> str:
    return (f"{req.client} {req.method} {req.path} {response.status} {actor} "
            f"{int((time.monotonic() - started) * 1000)}ms")
