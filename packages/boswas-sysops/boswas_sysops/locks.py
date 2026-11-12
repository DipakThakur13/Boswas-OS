"""Is another package manager running? dpkg and APT lock files.

dpkg and APT hold fcntl (POSIX record) write locks on their lock files.
``check`` tries the same lock on each file without blocking and releases it
at once; a conflict means another process holds it. The holder comes from
/proc/locks (device and inode of the lock file), its command line from
/proc/<pid>/cmdline. APT itself is the final arbiter: a lock taken between
this check and APT's start makes APT fail with "Could not get lock", which
apt.py reports as LOCKED as well.
"""

from __future__ import annotations

import errno
import fcntl
import os

from .protocol import SysopsError

LISTS = "/var/lib/apt/lists/lock"
ARCHIVES = "/var/cache/apt/archives/lock"
FRONTEND = "/var/lib/dpkg/lock-frontend"
DPKG = "/var/lib/dpkg/lock"
FOR_REFRESH = (LISTS,)
FOR_PACKAGES = (FRONTEND, DPKG, ARCHIVES)
FOR_CLEAN = (ARCHIVES,)


def is_locked(path: str) -> bool:
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR):
            return False
        raise
    try:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return True
            raise
        fcntl.lockf(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def holder(root, path: str) -> dict | None:
    """{"pid": n, "command": "..."} of the process holding a lock on path, from /proc/locks."""
    try:
        st = os.stat(root(path))
    except OSError:
        return None
    wanted = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    for line in (root.read("/proc/locks", 1024 * 1024) or "").splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] == "->":
            continue
        pid, ident = fields[4], fields[5]
        if ident != wanted or not pid.isdigit():
            continue
        cmdline = " ".join((root.read(f"/proc/{pid}/cmdline", 4096) or "").replace("\0", " ").split())
        command = cmdline or " ".join((root.read(f"/proc/{pid}/comm", 256) or "").split())
        return {"pid": int(pid), "command": command[:200]}
    return None


def check(root, paths: tuple[str, ...]) -> None:
    for path in paths:
        if is_locked(root(path)):
            info = holder(root, path)
            who = f" ({info['command'] or 'process'} {info['pid']})" if info else ""
            raise SysopsError("LOCKED", f"Another package manager is running{who}. Wait until it has finished, "
                                        "then try again.", {"holder": info, "lock": path})
