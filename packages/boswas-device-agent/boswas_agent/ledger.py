"""What the agent did: the ledger of remote commands and the local event log.

The command ledger makes command handling idempotent: a command ID that was
executed before is never executed again, its recorded outcome is reported
instead. The event log is the device-side audit trail of management actions
and security events; it stays on the device unless an event is one the
Control Plane is told about (models.DEVICE_EVENT_TYPES).

Neither ever contains user names, file contents, program output or
credentials.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .commands import format_time, utc_now
from .storage import ensure_dir, read_json, write_json

MAX_LEDGER = 1000
MAX_EVENT_LOG_BYTES = 2 * 1024 * 1024
KEEP_EVENTS = 2000
_DETAIL_LIMIT = 300


def clean(value: object, limit: int = _DETAIL_LIMIT) -> str:
    return "".join(c for c in str(value) if c.isprintable())[:limit]


class CommandLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        doc = read_json(self.path)
        return doc if isinstance(doc, dict) and isinstance(doc.get("commands"), dict) else {"commands": {}}

    def get(self, command_id: str) -> dict | None:
        with self._lock:
            return self._load()["commands"].get(command_id)

    def record(self, command_id: str, entry: dict) -> None:
        with self._lock:
            doc = self._load()
            commands = doc["commands"]
            commands[command_id] = {**commands.get(command_id, {}), **entry, "updated_at": format_time(utc_now())}
            if len(commands) > MAX_LEDGER:
                for old in sorted(commands, key=lambda k: commands[k].get("updated_at", ""))[:len(commands) - MAX_LEDGER]:
                    del commands[old]
            ensure_dir(self.path.parent, 0o755)
            write_json(self.path, doc, 0o600)

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            commands = self._load()["commands"]
        items = [{"command_id": k, **v} for k, v in commands.items()]
        return sorted(items, key=lambda e: e.get("updated_at", ""), reverse=True)[:limit]

    def with_status(self, *statuses: str) -> list[dict]:
        """Commands in these statuses, oldest first (received_at)."""
        with self._lock:
            commands = self._load()["commands"]
        items = [{"command_id": k, **v} for k, v in commands.items() if v.get("status") in statuses]
        return sorted(items, key=lambda e: e.get("received_at", ""))


class EventLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, event_type: str, detail: str = "", *, application_id: str | None = None,
               source: str = "local", command_id: str | None = None) -> dict:
        event = {"type": event_type, "occurred_at": format_time(utc_now()), "source": source,
                 "detail": clean(detail)}
        if application_id:
            event["application_id"] = clean(application_id, 96)
        if command_id:
            event["command_id"] = clean(command_id, 36)
        line = json.dumps(event, sort_keys=True) + "\n"
        with self._lock:
            ensure_dir(self.path.parent, 0o755)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
            if self.path.stat().st_size > MAX_EVENT_LOG_BYTES:
                self._trim()
        return event

    def _trim(self) -> None:
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-KEEP_EVENTS:]
        tmp = self.path.with_name(f".{self.path.name}.trim")
        tmp.write_text("".join(lines), encoding="utf-8")
        os.chmod(tmp, 0o640)
        os.replace(tmp, self.path)

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
            except FileNotFoundError:
                return []
        events = []
        for line in reversed(lines):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                events.append(doc)
        return events
