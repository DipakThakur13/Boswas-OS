"""What Archive Manager remembers between runs: recent archives and the last choices. No Qt.

One small JSON file in the state directory. Everything read from it is
checked (it is a file like any other: it can be damaged or edited), and
what does not fit is dropped silently, because losing a list of recent
files is not worth a message.
"""

from __future__ import annotations

import json
import os
import tempfile

MAX_RECENT = 12
_MAX_FILE = 256 * 1024
_POLICIES = ("ask", "replace", "skip", "rename")


class State:
    def __init__(self, directory: str | os.PathLike | None) -> None:
        self.path = os.path.join(os.fspath(directory), "state.json") if directory is not None else None
        self.recent: list[str] = []
        self.policy = "ask"
        self.open_after = True
        self.preview = True
        self.load()

    def load(self) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(_MAX_FILE + 1)
            data = json.loads(raw.decode("utf-8")) if len(raw) <= _MAX_FILE else {}
        except (OSError, ValueError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        recent = data.get("recent")
        if isinstance(recent, list):
            self.recent = [item for item in recent if isinstance(item, str) and item.startswith("/")
                           and "\0" not in item and len(item) < 4096][:MAX_RECENT]
        if data.get("policy") in _POLICIES:
            self.policy = data["policy"]
        if isinstance(data.get("open_after"), bool):
            self.open_after = data["open_after"]
        if isinstance(data.get("preview"), bool):
            self.preview = data["preview"]

    def save(self) -> None:
        if self.path is None:
            return
        data = {"recent": self.recent[:MAX_RECENT], "policy": self.policy, "open_after": self.open_after,
                "preview": self.preview}
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=os.path.dirname(self.path))
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1)
            os.replace(temporary, self.path)
        except (OSError, ValueError):
            pass

    def add_recent(self, path: str) -> None:
        path = os.path.abspath(path)
        self.recent = [path] + [item for item in self.recent if item != path]
        del self.recent[MAX_RECENT:]
        self.save()

    def forget(self, path: str) -> None:
        self.recent = [item for item in self.recent if item != path]
        self.save()
