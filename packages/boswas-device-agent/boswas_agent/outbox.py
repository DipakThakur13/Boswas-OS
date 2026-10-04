"""Messages waiting for the Control Plane, and the retry schedule.

The outbox survives restarts and connectivity loss. It is bounded: status
documents (inventory, status, compliance) are coalesced to the latest one,
and when the outbox is full, events are dropped before command results.

Retries use exponential back-off with jitter, capped, so an unreachable
Control Plane costs one attempt every few minutes, never a busy loop.
"""

from __future__ import annotations

import json
import os
import random
import re
from pathlib import Path

from .storage import atomic_write, ensure_dir, read_json

COALESCED = ("inventory", "status", "compliance")
KINDS = ("result", "event", *COALESCED)
MAX_ITEMS = 500
MAX_ITEM_BYTES = 1024 * 1024
_NAME_RE = re.compile(r"^(\d{12})-([a-z]+)\.json$")


class Backoff:
    """Delay before the next attempt: base * factor^(n-1), capped, with jitter in [50%, 100%]."""

    def __init__(self, base: float = 5.0, cap: float = 900.0, factor: float = 2.0, rng=random.random):
        self.base, self.cap, self.factor, self.rng = base, cap, factor, rng
        self.failures = 0

    def ceiling(self) -> float:
        if self.failures == 0:
            return 0.0
        return min(self.cap, self.base * self.factor ** min(self.failures - 1, 32))

    def failure(self) -> float:
        self.failures += 1
        return self.ceiling() * (0.5 + 0.5 * self.rng())

    def success(self) -> None:
        self.failures = 0


class Outbox:
    def __init__(self, directory: Path, max_items: int = MAX_ITEMS):
        self.directory = Path(directory)
        self.max_items = max_items

    def _entries(self) -> list[tuple[int, str, Path]]:
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return []
        entries = []
        for name in names:
            m = _NAME_RE.fullmatch(name)
            if m and m.group(2) in KINDS:
                entries.append((int(m.group(1)), m.group(2), self.directory / name))
        return sorted(entries)

    def put(self, kind: str, payload: dict) -> str:
        if kind not in KINDS:
            raise ValueError(f"unknown outbox kind {kind!r}")
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        if len(data) > MAX_ITEM_BYTES:
            raise ValueError("outbox item too large")
        ensure_dir(self.directory, 0o700)
        entries = self._entries()
        if kind in COALESCED:
            for _, k, path in entries:
                if k == kind:
                    path.unlink(missing_ok=True)
            entries = [e for e in entries if e[1] != kind]
        while len(entries) >= self.max_items:
            victim = next((e for e in entries if e[1] == "event"), None) or \
                next((e for e in entries if e[1] in COALESCED), None) or entries[0]
            victim[2].unlink(missing_ok=True)
            entries.remove(victim)
        seq = (entries[-1][0] + 1) if entries else 1
        name = f"{seq:012d}-{kind}.json"
        atomic_write(self.directory / name, data, 0o600)
        return name

    def items(self) -> list[tuple[str, str, dict]]:
        out = []
        for _, kind, path in self._entries():
            doc = read_json(path, MAX_ITEM_BYTES)
            if isinstance(doc, dict):
                out.append((path.name, kind, doc))
            else:
                path.unlink(missing_ok=True)            # damaged: never resent
        return out

    def ack(self, item_id: str) -> None:
        if _NAME_RE.fullmatch(item_id):
            (self.directory / item_id).unlink(missing_ok=True)

    def __len__(self) -> int:
        return len(self._entries())

    def counts(self) -> dict[str, int]:
        counts = {k: 0 for k in KINDS}
        for _, kind, _ in self._entries():
            counts[kind] += 1
        return counts
