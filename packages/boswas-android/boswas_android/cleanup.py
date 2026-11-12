"""Removing what the Android runtime left in the user's home after the runtime itself was removed.

Waydroid keeps the user's Android data in $XDG_DATA_HOME/waydroid and writes
one desktop entry per Android app as
$XDG_DATA_HOME/applications/waydroid.<package>.desktop (Exec=waydroid app
launch <package>). Waydroid removes an entry when the app is uninstalled,
but nothing removes them when the runtime itself is removed: they would stay
in the launcher and fail. Only files matching that name and Exec line are
removed; symbolic links are never followed.
"""

from __future__ import annotations

import os
import re
import shutil
import stat

ENTRY_RE = re.compile(r"^waydroid\.[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+\.desktop$")
MAX_ENTRY = 256 * 1024


def _is_waydroid_entry(path: str) -> bool:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_ENTRY:
            return False
        with open(path, "rb") as handle:
            text = handle.read(MAX_ENTRY).decode("utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"^Exec=waydroid app launch \S+\s*$", text, re.MULTILINE) is not None


def desktop_entries(data_home: str) -> list[str]:
    directory = os.path.join(data_home, "applications")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names
            if ENTRY_RE.match(n) and _is_waydroid_entry(os.path.join(directory, n))]


def remove_desktop_entries(data_home: str) -> tuple[list[str], list[str]]:
    removed, failed = [], []
    for path in desktop_entries(data_home):
        try:
            os.unlink(path)
            removed.append(path)
        except OSError:
            failed.append(path)
    return removed, failed


def remove_user_data(data_home: str) -> tuple[bool, list[str]]:
    """Delete $XDG_DATA_HOME/waydroid. Returns (existed, paths that could not be removed)."""
    target = os.path.join(data_home, "waydroid")
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False, []
    except OSError:
        return True, [target]
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return True, [target]
    failed: list[str] = []

    def onexc(_function, path, _exc):
        failed.append(path)

    try:
        shutil.rmtree(target, onexc=onexc)
    except TypeError:                                    # Python < 3.12
        shutil.rmtree(target, onerror=lambda f, p, e: failed.append(p))
    return True, failed[:20]
