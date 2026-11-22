"""Boswas Update > Update history: what the package manager did, from APT's own log (no Qt).

APT writes every run to /var/log/apt/history.log; logrotate keeps older
months as history.log.1.gz, history.log.2.gz, ... Each run is a block:

    Start-Date: 2026-10-03  06:12:01
    Commandline: /usr/bin/unattended-upgrade
    Requested-By: alice (1000)
    Upgrade: libssl3t64:amd64 (3.0.13-1, 3.0.14-1), openssl:amd64 (...)
    Install: ..., Remove: ..., Purge: ..., Reinstall: ..., Downgrade: ...
    End-Date: 2026-10-03  06:13:10

The files the user may read are read (newest first, each at most MAX_FILE
bytes once uncompressed); when the log is restricted on this device the page
says so instead of guessing.
"""

from __future__ import annotations

import gzip
import io
import re
from dataclasses import dataclass, field

LOG_DIR = "/var/log/apt"
LOG = "history.log"
MAX_ROTATED = 12
MAX_FILE = 8 * 1024 * 1024
MAX_ENTRIES = 300
MAX_PACKAGES = 400
ACTIONS = ("Install", "Upgrade", "Downgrade", "Reinstall", "Remove", "Purge")
ACTION_WORDS = {"Install": "Installed", "Upgrade": "Updated", "Downgrade": "Downgraded", "Reinstall": "Reinstalled",
                "Remove": "Removed", "Purge": "Removed with settings"}
_PACKAGE = re.compile(r"([a-z0-9][a-z0-9+.-]*)(?::[a-z0-9-]+)? \(([^()]*)\)")


@dataclass(frozen=True)
class Change:
    package: str
    old: str = ""
    new: str = ""
    automatic: bool = False      # installed as a dependency

    @property
    def text(self) -> str:
        if self.old and self.new:
            return f"{self.package} ({self.old} → {self.new})"
        version = self.new or self.old
        return f"{self.package} ({version})" if version else self.package


@dataclass(frozen=True)
class Entry:
    start: str                   # "2026-10-03 06:12:01"
    command: str = ""
    requested_by: str = ""
    changes: dict = field(default_factory=dict)      # action -> tuple[Change, ...]
    error: str = ""

    @property
    def automatic(self) -> bool:
        return "unattended-upgrade" in self.command

    @property
    def by(self) -> str:
        if self.automatic:
            return "Automatic security updates"
        if "boswas" in self.command and "sysops" in self.command:
            return "Boswas Control Center"
        if self.requested_by:
            return f"Requested by {self.requested_by.split(' (')[0]}"
        return "The package manager"

    @property
    def summary(self) -> str:
        parts = []
        for action in ACTIONS:
            count = len(self.changes.get(action, ()))
            if count:
                parts.append(f"{ACTION_WORDS[action]} {count} package{'s' if count != 1 else ''}")
        return ", ".join(parts) or "No package changed"


def parse_changes(action: str, text: str) -> tuple[Change, ...]:
    found = []
    for match in _PACKAGE.finditer(text):
        name, inside = match.group(1), [part.strip() for part in match.group(2).split(",")]
        automatic = "automatic" in inside
        versions = [part for part in inside if part and part != "automatic"]
        if action == "Upgrade" or action == "Downgrade":
            old, new = (versions + ["", ""])[:2]
        elif action in ("Remove", "Purge"):
            old, new = (versions[0] if versions else ""), ""
        else:
            old, new = "", (versions[0] if versions else "")
        found.append(Change(name, old[:60], new[:60], automatic))
        if len(found) >= MAX_PACKAGES:
            break
    return tuple(found)


def parse(text: str) -> list[Entry]:
    """The runs of one history.log text, in the order they were written."""
    entries = []
    for block in re.split(r"\n\s*\n", text or ""):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition(": ")
            if sep and key in ("Start-Date", "Commandline", "Requested-By", "Error", *ACTIONS):
                fields[key] = value.strip()
        start = " ".join(fields.get("Start-Date", "").split())
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", start):
            continue
        changes = {action: parse_changes(action, fields[action]) for action in ACTIONS if fields.get(action)}
        entries.append(Entry(start, fields.get("Commandline", "")[:300], fields.get("Requested-By", "")[:80],
                             changes, fields.get("Error", "")[:300]))
    return entries


@dataclass(frozen=True)
class History:
    entries: tuple[Entry, ...]
    readable: bool               # False when the log exists but this user may not read it
    found: bool                  # whether there is a log at all


def read(root) -> History:
    """The update history of this device, newest first (``root``: a probes.Root)."""
    texts: list[str] = []
    readable, found = True, False
    for index in range(0, MAX_ROTATED + 1):
        name = LOG if index == 0 else f"{LOG}.{index}.gz"
        path = root(f"{LOG_DIR}/{name}")
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_FILE)
        except FileNotFoundError:
            if index > 1:
                break
            continue
        except OSError:
            found, readable = True, False
            continue
        found = True
        if name.endswith(".gz"):
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as unpacked:
                    data = unpacked.read(MAX_FILE)
            except (OSError, EOFError):
                continue
        texts.append(data.decode("utf-8", "replace"))
    entries: list[Entry] = []
    for text in texts:                             # newest file first, and in each the newest run first
        entries.extend(reversed(parse(text)))
        if len(entries) >= MAX_ENTRIES:
            break
    return History(tuple(entries[:MAX_ENTRIES]), readable, found)
