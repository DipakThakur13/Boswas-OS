"""The JSON Lines protocol: events on stdout, result codes and exit statuses.

    {"type": "start", "schema": "boswas-sysops/1", "operation": "...", "cancellable": true}
    {"type": "progress", "phase": "download", "percent": 42.0, "message": "..."}
    {"type": "log", "level": "info", "message": "..."}
    {"type": "result", "ok": true, "code": "OK", "message": "...", "data": {}}

Exactly one result line is written, always last. Writing never fails an
operation: when the caller has gone away (broken pipe), the helper keeps
working to completion (an interrupted dpkg run would be worse) and only
stops writing.
"""

from __future__ import annotations

import json
import threading

from . import SCHEMA

# code -> exit status (contract, section 1)
EXIT_CODES = {
    "OK": 0,
    "FAILED": 1,
    "DISK_SPACE": 1,
    "INVALID_ARGUMENT": 2,
    "NOT_ALLOWED": 2,
    "NOT_FOUND": 2,
    "UNTRUSTED": 2,
    "LOCKED": 3,
    "CANCELLED": 4,
    "NETWORK": 5,
    "UNSUPPORTED": 6,
}
PHASES = frozenset({"refresh", "download", "install", "configure", "remove", "cleanup", "verify", "init",
                    "service", "inspect"})
LEVELS = frozenset({"info", "warning"})
MAX_MESSAGE = 2000


class SysopsError(Exception):
    """An operation ends with this code (never OK) and a sentence for the user."""

    def __init__(self, code: str, message: str, data: dict | None = None):
        if code not in EXIT_CODES or code == "OK":
            raise ValueError(f"invalid result code {code!r}")
        super().__init__(message)
        self.code, self.message, self.data = code, message, data or {}


def clip(text, limit: int = MAX_MESSAGE) -> str:
    """One line of at most ``limit`` characters (whitespace runs and line breaks collapsed)."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Emitter:
    """Writes events to a text stream, one JSON object per line, flushed."""

    def __init__(self, stream):
        self.stream = stream
        self.result_written = False
        self.broken = False
        self._lock = threading.Lock()

    def _write(self, event: dict) -> None:
        if self.result_written:
            raise RuntimeError("no event may follow the result")
        line = json.dumps(event, ensure_ascii=False, sort_keys=False, separators=(", ", ": "))
        with self._lock:
            if self.broken:
                return
            try:
                self.stream.write(line + "\n")
                self.stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.broken = True       # the caller is gone; finish the work silently

    def start(self, operation: str, cancellable: bool) -> None:
        self._write({"type": "start", "schema": SCHEMA, "operation": operation, "cancellable": bool(cancellable)})

    def progress(self, phase: str, percent: float | None, message: str = "",
                 cancellable: bool | None = None) -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}")
        if percent is not None:
            percent = round(min(100.0, max(0.0, float(percent))), 1)
        event = {"type": "progress", "phase": phase, "percent": percent, "message": clip(message, 300)}
        if cancellable is not None:
            event["cancellable"] = bool(cancellable)
        self._write(event)

    def log(self, message: str, level: str = "info") -> None:
        if level not in LEVELS:
            raise ValueError(f"unknown level {level!r}")
        self._write({"type": "log", "level": level, "message": clip(message)})

    def result(self, ok: bool, code: str, message: str, data: dict | None = None) -> None:
        if code not in EXIT_CODES or (code == "OK") != bool(ok):
            raise ValueError(f"inconsistent result {ok!r}/{code!r}")
        self._write({"type": "result", "ok": bool(ok), "code": code, "message": clip(message), "data": data or {}})
        self.result_written = True
