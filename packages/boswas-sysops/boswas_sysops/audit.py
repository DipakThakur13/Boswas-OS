"""/var/log/boswas/sysops.log: one line per invocation.

Time, requesting UID, operation, validated arguments and the result code.
Never secrets and never file contents. Writing the log never fails the
operation (a full disk must not turn a finished upgrade into an error).
"""

from __future__ import annotations

import grp
import json
import os
import time

LOG_DIR = "/var/log/boswas"
LOG_FILE = "/var/log/boswas/sysops.log"


def _adm_gid() -> int | None:
    try:
        return grp.getgrnam("adm").gr_gid
    except KeyError:
        return None


def record(root, uid: int | None, operation: str, args: list[str], code: str, now: float | None = None) -> bool:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))
    safe_args = [a[:300] for a in args[:60]]
    line = f"{stamp} uid={uid if uid is not None else '-'} op={operation} args={json.dumps(safe_args)} code={code}\n"
    gid = _adm_gid()
    try:
        directory = root(LOG_DIR)
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=0o750, exist_ok=True)
            os.chmod(directory, 0o750)
            if gid is not None and os.geteuid() == 0:
                os.chown(directory, 0, gid)
        fd = os.open(root(LOG_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
        try:
            if os.geteuid() == 0:
                os.fchown(fd, 0, gid if gid is not None else 0)
            os.fchmod(fd, 0o640)
            os.write(fd, line.encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
        return True
    except OSError:
        return False
