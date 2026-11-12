"""Read-only access to the file system and the process environment.

Every path the package reads goes through ``Root``, so the tests point the
code at a temporary directory that plays the part of "/". Nothing here
writes, starts processes or follows a path outside the root.
"""

from __future__ import annotations

import os
import time

MAX_FILE = 1024 * 1024


class Root:
    """A file system root ("/" on a device, a temporary directory in the tests)."""

    def __init__(self, path: str = "/"):
        self.path = path or "/"

    def __call__(self, path: str) -> str:
        return os.path.join(self.path, path.lstrip("/"))

    def read(self, path: str, limit: int = MAX_FILE) -> str | None:
        data = self.read_bytes(path, limit)
        return None if data is None else data.decode("utf-8", errors="replace")

    def read_bytes(self, path: str, limit: int = MAX_FILE) -> bytes | None:
        try:
            with open(self(path), "rb") as handle:
                return handle.read(limit)
        except OSError:
            return None

    def read_tail(self, path: str, limit: int = 64 * 1024) -> str | None:
        try:
            with open(self(path), "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - limit))
                return handle.read(limit).decode("utf-8", errors="replace")
        except OSError:
            return None

    def exists(self, path: str) -> bool:
        return os.path.exists(self(path))

    def isdir(self, path: str) -> bool:
        return os.path.isdir(self(path))

    def isfile(self, path: str) -> bool:
        return os.path.isfile(self(path))

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(self(path)))
        except OSError:
            return []

    def readlink_name(self, path: str) -> str | None:
        """Last component of a symbolic link's target (e.g. a device's driver)."""
        try:
            return os.path.basename(os.readlink(self(path)))
        except OSError:
            return None

    def size(self, path: str) -> int | None:
        try:
            return os.stat(self(path)).st_size
        except OSError:
            return None

    def free_bytes(self, path: str) -> int | None:
        """Free space on the file system holding the system path (or its nearest existing parent)."""
        return free_bytes(self(path), stop=self.path)


def free_bytes(path: str, stop: str = "/") -> int | None:
    """Free space for unprivileged users on the file system holding a real path (or its nearest existing parent)."""
    probe = path
    while True:
        try:
            st = os.statvfs(probe)
            return st.f_bavail * st.f_frsize
        except OSError:
            parent = os.path.dirname(probe.rstrip("/")) or "/"
            if parent == probe or len(probe) <= len(stop.rstrip("/")):
                return None
            probe = parent


def tree_bytes(top: str, max_entries: int = 100_000, budget: float = 3.0) -> int | None:
    """Disk usage of the files below a real directory; None if absent, unreadable or too large to count quickly."""
    try:
        if os.path.islink(top) or not os.path.isdir(top):
            return None
    except OSError:
        return None
    total = entries = 0
    deadline = time.monotonic() + budget
    complete = True

    def onerror(_exc):
        nonlocal complete
        complete = False

    for current, dirs, files in os.walk(top, onerror=onerror):
        for name in files:
            entries += 1
            try:
                st = os.lstat(os.path.join(current, name))
            except OSError:
                complete = False
                continue
            if not os.path.islink(os.path.join(current, name)):
                total += st.st_blocks * 512 if st.st_blocks else st.st_size
        entries += len(dirs)
        if entries > max_entries or time.monotonic() > deadline:
            return None
    return total if complete else None


class Host:
    """Kernel release, machine and environment (replaced in the tests)."""

    def __init__(self, environ=None, release: str | None = None, machine: str | None = None):
        uname = os.uname()
        self.environ = dict(os.environ if environ is None else environ)
        self.release = release or uname.release
        self.machine = machine or uname.machine

    def home(self) -> str | None:
        home = self.environ.get("HOME")
        return home if home and os.path.isabs(home) else None

    def data_home(self) -> str | None:
        value = self.environ.get("XDG_DATA_HOME")
        if value and os.path.isabs(value):
            return value
        home = self.home()
        return os.path.join(home, ".local", "share") if home else None

    def state_home(self) -> str | None:
        value = self.environ.get("XDG_STATE_HOME")
        if value and os.path.isabs(value):
            return value
        home = self.home()
        return os.path.join(home, ".local", "state") if home else None
