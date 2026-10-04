"""Who may use the Control Plane.

Devices authenticate with their certificate (mutual TLS; server.py and
service.device_for_certificate). Operators authenticate with an API token
issued locally with `boswas-cp operator add` and sent as
`Authorization: Bearer bcp_<prefix>_<secret>`.

Boswas ID is not part of this release. OperatorAuthenticator is the
attachment point for a future identity provider (an implementation of
boswas_agent.user_identity.IdentityProvider would map an authenticated
principal to an actor and role here); nothing in the Control Plane depends
on user identity otherwise, and device identity never derives from it.

Secrets (operator tokens, enrollment tokens) are stored only as scrypt
hashes; the clear token is shown once when it is created.

Roles: viewer (read), operator (read, create and cancel commands),
admin (everything: catalog, policies, enrollment tokens, operators,
artifacts, device retirement).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

ROLES = ("viewer", "operator", "admin")
RANK = {role: i for i, role in enumerate(ROLES)}
_TOKEN_RE = re.compile(r"^(bcp|bet)_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


@dataclass(frozen=True)
class Principal:
    actor: str             # opaque audit reference, e.g. "operator:alice"
    role: str
    kind: str = "operator"

    def allows(self, role: str) -> bool:
        return RANK[self.role] >= RANK[role]


def hash_secret(secret: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_secret(secret: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(secret.encode("utf-8"), salt=base64.b64decode(salt), dklen=32, **SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token(kind: str) -> tuple[str, str, str]:
    """(token shown once, lookup prefix, stored hash)."""
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)[:43].ljust(43, "A")
    return f"{kind}_{prefix}_{secret}", prefix, hash_secret(secret)


def split_token(token: str, kind: str) -> tuple[str, str] | None:
    m = _TOKEN_RE.fullmatch(token or "")
    if not m or m.group(1) != kind:
        return None
    return m.group(2), m.group(3)


class OperatorAuthenticator(ABC):
    @abstractmethod
    def authenticate(self, authorization: str | None) -> Principal | None:
        """The principal behind an Authorization header, or None."""


class LocalTokenAuthenticator(OperatorAuthenticator):
    """Operator API tokens stored (hashed) in the Control Plane database."""

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self._last_used: dict[int, float] = {}

    def authenticate(self, authorization: str | None) -> Principal | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        parts = split_token(authorization[7:].strip(), "bcp")
        if parts is None:
            return None
        prefix, secret = parts
        row = self.store.one("SELECT * FROM operators WHERE token_prefix = ?", (prefix,))
        if row is None or row["disabled"] or not verify_secret(secret, row["token_hash"]):
            return None
        now = self.clock()
        if now - self._last_used.get(row["id"], 0) > 60:          # do not write on every request
            self._last_used[row["id"]] = now
            self.store.execute("UPDATE operators SET last_used_at = ? WHERE id = ?",
                               (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), row["id"]))
        return Principal(actor=f"operator:{row['name']}", role=row["role"])


class FailureLimiter:
    """Throttle clients that keep presenting bad credentials."""

    def __init__(self, limit: int = 20, window: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, client: str) -> bool:
        now = self.clock()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < self.window]
            self._failures[client] = recent
            return len(recent) >= self.limit

    def failure(self, client: str) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(self.clock())
            if len(self._failures) > 10000:
                self._failures.clear()
