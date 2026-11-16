"""Locations a tab can show, and the user's well-known folders (no Qt).

A location is a string: an absolute path of a folder, ``trash:///`` (the
Trash of every volume, merged) or ``computer:///`` (This PC: the volumes).
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote, urlsplit

TRASH = "trash:///"
COMPUTER = "computer:///"

# XDG name, default folder under $HOME, icon name
USER_DIRS = (
    ("DESKTOP", "Desktop", "user-desktop"),
    ("DOCUMENTS", "Documents", "folder-documents"),
    ("DOWNLOAD", "Downloads", "folder-downloads"),
    ("MUSIC", "Music", "folder-music"),
    ("PICTURES", "Pictures", "folder-pictures"),
    ("VIDEOS", "Videos", "folder-videos"),
)
_USER_DIR_LINE = re.compile(r'^\s*XDG_([A-Z]+)_DIR\s*=\s*"((?:[^"\\]|\\.)*)"\s*$')
_MAX_CONFIG = 64 * 1024


def home() -> str:
    return os.path.expanduser("~")


def is_virtual(location: str) -> bool:
    return location in (TRASH, COMPUTER)


def normalize(text: str, base: str | None = None) -> str:
    """What the user typed into the address bar as a location.

    Accepts ``~`` and ``~/x``, ``file://`` URLs, ``trash:`` and
    ``computer:`` in any spelling, relative paths (against ``base``) and
    absolute paths; ``.`` and ``..`` are resolved without following links.
    """
    text = text.strip()
    lowered = text.lower()
    if lowered in ("trash:", "trash:/", "trash://", TRASH, "trash", "recycle bin"):
        return TRASH
    if lowered in ("computer:", "computer:/", "computer://", COMPUTER, "this pc"):
        return COMPUTER
    if lowered.startswith("file:"):
        parts = urlsplit(text)
        if parts.netloc not in ("", "localhost"):
            raise ValueError("Only files on this computer can be opened.")
        text = unquote(parts.path, errors="surrogateescape")
    if text == "~" or text.startswith("~/"):
        text = home() + text[1:]
    if not text:
        raise ValueError("Type the path of a folder.")
    if "\0" in text:
        raise ValueError("A path cannot contain a null character.")
    if not text.startswith("/"):
        text = os.path.join(base if base and not is_virtual(base) else home(), text)
    return os.path.normpath(text)


def parent(location: str) -> str | None:
    """Where "Up" goes from ``location``; None at the top (This PC)."""
    if location == COMPUTER:
        return None
    if location == TRASH or location == "/":
        return COMPUTER
    return os.path.dirname(location.rstrip("/")) or "/"


def title(location: str) -> str:
    """A short name for tabs and the window title."""
    if location == TRASH:
        return "Trash"
    if location == COMPUTER:
        return "This PC"
    if location == "/":
        return "File system"
    if location == home():
        return "Home"
    return os.path.basename(location.rstrip("/")) or location


def crumbs(location: str, volumes: list[tuple[str, str]] = ()) -> list[tuple[str, str]]:
    """The breadcrumb of ``location`` as (label, location) pairs, the place itself last.

    ``volumes`` are (mount point, name) of the volumes shown under This PC;
    a path below one of them (other than ``/``) starts at that volume.
    """
    if location == COMPUTER:
        return [("This PC", COMPUTER)]
    if location == TRASH:
        return [("Trash", TRASH)]
    path = location.rstrip("/") or "/"
    user_home = home().rstrip("/")
    if user_home and (path == user_home or path.startswith(user_home + "/")):
        start, result = user_home, [("Home", user_home)]
    else:
        best = ""
        best_name = ""
        for mount_point, name in volumes:
            mount_point = mount_point.rstrip("/")
            if mount_point and (path == mount_point or path.startswith(mount_point + "/")) and len(mount_point) > len(best):
                best, best_name = mount_point, name
        if best:
            start, result = best, [("This PC", COMPUTER), (best_name, best)]
        else:
            start, result = "", [("This PC", COMPUTER), ("File system", "/")]
    rest = path[len(start):].strip("/")
    current = start
    for part in (rest.split("/") if rest else []):
        current = f"{current}/{part}"
        result.append((part, current))
    return result


def _unescape_shell(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def user_dirs(config_home: str | None = None, user_home: str | None = None) -> list[tuple[str, str, str]]:
    """The user's well-known folders as (label, path, icon name), in the order of Windows' quick access.

    Read from ``user-dirs.dirs`` (xdg-user-dirs); a folder that is not
    configured falls back to its usual English name under the home
    directory. Folders that do not exist, or that are the home directory
    itself (how xdg-user-dirs disables one), are left out.
    """
    user_home = (user_home or home()).rstrip("/") or "/"
    if config_home is None:
        config_home = os.environ.get("XDG_CONFIG_HOME") or ""
        if not config_home.startswith("/"):
            config_home = os.path.join(user_home, ".config")
    configured: dict[str, str] = {}
    try:
        with open(os.path.join(config_home, "user-dirs.dirs"), "r", encoding="utf-8", errors="surrogateescape") as handle:
            text = handle.read(_MAX_CONFIG)
    except OSError:
        text = ""
    for line in text.splitlines():
        match = _USER_DIR_LINE.match(line)
        if not match:
            continue
        value = _unescape_shell(match.group(2))
        if value == "$HOME" or value.startswith("$HOME/"):
            value = user_home + value[5:]
        if value.startswith("/"):
            configured[match.group(1)] = os.path.normpath(value)
    result = []
    for key, default, icon in USER_DIRS:
        path = configured.get(key, os.path.join(user_home, default))
        if path == user_home or not os.path.isdir(path):
            continue
        result.append((os.path.basename(path) or default, path, icon))
    return result
