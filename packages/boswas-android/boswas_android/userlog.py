"""The user's own log of boswas-android actions: ~/.local/state/boswas/android.log.

One line per action (UTC time, action, result, short detail). Owned by the
user (directory 0700, file 0600), never contains file contents or secrets.
Logging never makes a command fail.
"""

from __future__ import annotations

import os
import time

from .system import Host

MAX_DETAIL = 300
ROTATE_BYTES = 1024 * 1024


def path(host: Host) -> str | None:
    state = host.state_home()
    return os.path.join(state, "boswas", "android.log") if state else None


def write(host: Host, action: str, result: str, detail: str = "") -> None:
    target = path(host)
    if not target:
        return
    line = "{} {} {} {}\n".format(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), action, result,
                                  " ".join(detail.split())[:MAX_DETAIL])
    try:
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        try:
            if os.path.getsize(target) > ROTATE_BYTES:
                os.replace(target, target + ".1")
        except OSError:
            pass
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
