"""For Clock and Calendar: is the reminder service running, and start it when it is not.

The service is started with the session (its autostart entry). An
application still checks when it opens, because the session may have
begun before the package was installed. The check is the service's lock
file: the running service holds a lock on it.
"""

from __future__ import annotations

import fcntl
import os

from . import PROGRAM, paths

PROGRAMS = (f"/usr/bin/{PROGRAM}",)


def is_running() -> bool:
    """Whether a reminder service of this user holds the lock."""
    try:
        descriptor = os.open(paths.service_lock(), os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def program() -> str | None:
    """The installed service program, or None when it is not installed."""
    for candidate in PROGRAMS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ensure_running() -> str:
    """Make sure the service runs. Returns ``running``, ``started`` or ``unavailable`` (not installed, or it
    could not be started)."""
    if is_running():
        return "running"
    path = program()
    if path is None:
        return "unavailable"
    from PySide6.QtCore import QProcess
    started = QProcess.startDetached(path, [])
    return "started" if (started[0] if isinstance(started, tuple) else started) else "unavailable"
