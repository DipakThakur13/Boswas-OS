"""Control Plane storage: SQLite schema, migrations and repository.

Everything else uses this class only, never SQL, so the database can be
replaced (for example by PostgreSQL) behind the same methods.

Concurrency: one connection per thread, WAL journal, a busy timeout, and
BEGIN IMMEDIATE for every read-modify-write so state transitions are atomic.

The events table is an append-only audit trail with a hash chain: every
event stores the SHA-256 of its predecessor's hash and its own content, so
`verify_events` detects any edited or deleted row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
GENESIS = "0" * 64

MIGRATIONS = {
    1: """
CREATE TABLE operators (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    token_prefix TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT
);
CREATE TABLE enrollment_tokens (
    id INTEGER PRIMARY KEY,
    token_prefix TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    profile TEXT,
    policy_name TEXT,
    allow_ephemeral INTEGER NOT NULL DEFAULT 0,
    max_uses INTEGER NOT NULL DEFAULT 1,
    uses INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    revoked INTEGER NOT NULL DEFAULT 0,
    device_id TEXT
);
CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,
    name TEXT,
    profile TEXT,
    policy_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    certificate_fingerprint TEXT NOT NULL,
    certificate_expires_at TEXT,
    enrolled_at TEXT NOT NULL,
    enrolled_with INTEGER,
    ephemeral INTEGER NOT NULL DEFAULT 0,
    os_name TEXT, os_version TEXT, os_version_id TEXT, os_build_id TEXT, debian_version TEXT, kernel TEXT,
    architecture TEXT,
    hardware TEXT,
    agent_version TEXT,
    device_state TEXT,
    connection TEXT NOT NULL DEFAULT 'never' CHECK (connection IN ('never', 'online', 'offline')),
    last_heartbeat_at TEXT,
    reported_policy_version TEXT,
    inventory_revision INTEGER,
    pending_results INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX devices_fingerprint ON devices (certificate_fingerprint);
CREATE TABLE reports (
    device_id TEXT NOT NULL REFERENCES devices (device_id),
    kind TEXT NOT NULL CHECK (kind IN ('inventory', 'status', 'compliance')),
    received_at TEXT NOT NULL,
    revision INTEGER,
    document TEXT NOT NULL,
    PRIMARY KEY (device_id, kind)
);
CREATE TABLE artifacts (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    kind TEXT,
    machine TEXT,
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT NOT NULL
);
CREATE TABLE applications (
    app_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    publisher TEXT,
    version TEXT NOT NULL,
    architecture TEXT NOT NULL,
    status TEXT NOT NULL,
    support TEXT NOT NULL,
    manifest TEXT NOT NULL,
    installer_sha256 TEXT REFERENCES artifacts (sha256),
    installer_file_name TEXT,
    deprecated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE policies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    version TEXT NOT NULL UNIQUE,
    document TEXT NOT NULL,
    envelope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE (name, sequence)
);
CREATE TABLE commands (
    command_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices (device_id),
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    idempotency_key TEXT,
    fingerprint TEXT NOT NULL,
    sent_at TEXT, acknowledged_at TEXT, started_at TEXT, finished_at TEXT,
    result TEXT,
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (device_id, idempotency_key)
);
CREATE INDEX commands_device_status ON commands (device_id, status);
CREATE INDEX commands_status ON commands (status, expires_at);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    type TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('control-plane', 'device')),
    actor TEXT NOT NULL,
    device_id TEXT,
    application_id TEXT,
    command_id TEXT,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX events_device ON events (device_id, id);
CREATE INDEX events_type ON events (type, id);
""",
}

EVENT_FIELDS = ("occurred_at", "type", "source", "actor", "device_id", "application_id", "command_id", "detail")


def event_hash(prev_hash: str, row: dict) -> str:
    content = json.dumps({k: row.get(k) for k in EVENT_FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + content).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.migrate()

    # --- connections ----------------------------------------------------------------
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self):
        """An immediate (write-locking) transaction; nested use joins the outer one."""
        conn = self.connection()
        if getattr(self._local, "depth", 0):
            self._local.depth += 1
            try:
                yield conn
            finally:
                self._local.depth -= 1
            return
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            self._local.depth = 1
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                self._local.depth = 0

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.connection().execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.connection().execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.transaction() as conn:
            return conn.execute(sql, params)

    def migrate(self) -> None:
        conn = self.connection()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than this Control Plane ({SCHEMA_VERSION})")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            with self.transaction() as c:
                for statement in MIGRATIONS[target].split(";\n"):
                    if statement.strip():
                        c.execute(statement)
                c.execute(f"PRAGMA user_version = {target}")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # --- events (audit trail) -------------------------------------------------------------
    def append_event(self, row: dict) -> dict:
        with self.transaction() as conn:
            last = conn.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else GENESIS
            row = {k: row.get(k) for k in (*EVENT_FIELDS, "recorded_at")}
            row["prev_hash"], row["hash"] = prev, event_hash(prev, row)
            cur = conn.execute(
                "INSERT INTO events (occurred_at, recorded_at, type, source, actor, device_id, application_id, "
                "command_id, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row["occurred_at"], row["recorded_at"], row["type"], row["source"], row["actor"], row["device_id"],
                 row["application_id"], row["command_id"], row["detail"], row["prev_hash"], row["hash"]))
            row["id"] = cur.lastrowid
        return row

    def verify_events(self) -> tuple[bool, int, int | None]:
        """(intact, number of events, first broken event ID)."""
        prev, count = GENESIS, 0
        for row in self.connection().execute("SELECT * FROM events ORDER BY id"):
            row = dict(row)
            if row["prev_hash"] != prev or row["hash"] != event_hash(prev, row):
                return False, count, row["id"]
            prev, count = row["hash"], count + 1
        return True, count, None
