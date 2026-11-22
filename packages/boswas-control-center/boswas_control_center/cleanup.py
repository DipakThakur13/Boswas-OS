"""Cleanup recommendations of System > Storage: what can go, read-only (no Qt, nothing is changed here).

  Temporary files   the biggest items of the user's cache folder (~/.cache,
                    or $XDG_CACHE_HOME), each with its size; the person picks
                    which to clear. Programs fill them again as they need.
  Trash             the user's Trash of the freedesktop Trash specification
                    ($XDG_DATA_HOME/Trash: files/ and their info/), its size
                    and the number of items
  Downloads         the items directly in the Downloads folder (xdg-user-dirs)
                    that were not changed for N days, biggest first; the
                    person picks which go to the Trash

Deleting and moving is done by userfiles.py only (clear_cache_items,
empty_trash, trash_downloads). Symbolic links are never followed: a link is
counted as itself and removed as itself. Scans stop after MAX_SCAN entries.
"""

from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass

MAX_SCAN = 200000
MAX_ITEMS = 40
DAY = 86400
DOWNLOAD_AGES = (14, 30, 60, 90)
NAME_RE = re.compile(r"^[^/\0]{1,255}$")
# Cache folders that are better left alone while their program runs; they are listed, marked, not preselected.
IN_USE = ("thumbnails",)


@dataclass(frozen=True)
class Item:
    name: str                # the name in its folder
    path: str
    size: int                # bytes (regular files below it; links count as nothing)
    files: int
    folder: bool
    modified: float = 0.0    # seconds since the epoch


def valid_name(name: object) -> bool:
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name)) and name not in (".", "..")


def home(environ) -> str | None:
    value = environ.get("HOME") or ""
    return os.path.normpath(value) if value.startswith("/") and "\0" not in value else None


def _xdg(environ, variable: str, default: str) -> str | None:
    base = home(environ)
    value = environ.get(variable) or ""
    if value.startswith("/") and "\0" not in value:
        return os.path.normpath(value)
    return os.path.join(base, default) if base else None


def cache_dir(environ) -> str | None:
    return _xdg(environ, "XDG_CACHE_HOME", ".cache")


def trash_dir(environ) -> str | None:
    data = _xdg(environ, "XDG_DATA_HOME", os.path.join(".local", "share"))
    return os.path.join(data, "Trash") if data else None


def downloads_dir(environ) -> str | None:
    """The Downloads folder of xdg-user-dirs (XDG_DOWNLOAD_DIR), else ~/Downloads; None when it is the home."""
    base = home(environ)
    if base is None:
        return None
    config = _xdg(environ, "XDG_CONFIG_HOME", ".config")
    chosen = None
    try:
        with open(os.path.join(config, "user-dirs.dirs"), "rb") as handle:
            text = handle.read(65536).decode("utf-8", "replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        match = re.fullmatch(r'\s*XDG_DOWNLOAD_DIR\s*=\s*"((?:[^"\\]|\\.)*)"\s*', line)
        if match is None:
            continue
        value = re.sub(r"\\(.)", r"\1", match.group(1))
        if value.startswith("$HOME/"):
            chosen = os.path.join(base, value[len("$HOME/"):])
        elif value.startswith("/"):
            chosen = value
    chosen = os.path.normpath(chosen or os.path.join(base, "Downloads"))
    if "\0" in chosen or chosen == base:
        return None
    return chosen


def measure(path: str, budget: list[int] | None = None) -> tuple[int, int]:
    """(bytes, files) of the regular files at or below ``path``; symbolic links are not followed."""
    budget = budget if budget is not None else [MAX_SCAN]
    try:
        info = os.lstat(path)
    except OSError:
        return 0, 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size, 1
    if not stat.S_ISDIR(info.st_mode):
        return 0, 0
    size = files = 0
    for current, dirs, names in os.walk(path, followlinks=False):
        for name in names:
            budget[0] -= 1
            if budget[0] <= 0:
                return size, files
            try:
                entry = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            if stat.S_ISREG(entry.st_mode):
                size += entry.st_size
                files += 1
    return size, files


def _items(directory: str | None, *, older_than: float | None = None) -> list[Item]:
    if directory is None or os.path.islink(directory) or not os.path.isdir(directory):
        return []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    budget = [MAX_SCAN]
    found = []
    for name in names[:5000]:
        if not valid_name(name):
            continue
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if older_than is not None and info.st_mtime > older_than:
            continue
        size, files = measure(path, budget)
        found.append(Item(name, path, size, files, stat.S_ISDIR(info.st_mode), info.st_mtime))
    found.sort(key=lambda item: (-item.size, item.name))
    return found


def cache_items(environ, limit: int = MAX_ITEMS) -> list[Item]:
    """The biggest items of the cache folder, biggest first (empty ones left out)."""
    return [item for item in _items(cache_dir(environ)) if item.size > 0][:limit]


@dataclass(frozen=True)
class Trash:
    items: int
    size: int


def trash_usage(environ) -> Trash:
    directory = trash_dir(environ)
    files = os.path.join(directory, "files") if directory else None
    if files is None or os.path.islink(directory) or os.path.islink(files) or not os.path.isdir(files):
        return Trash(0, 0)
    try:
        count = len([name for name in os.listdir(files) if valid_name(name)])
    except OSError:
        count = 0
    size, _files = measure(files)
    return Trash(count, size)


def old_downloads(environ, days: int, now: float | None = None, limit: int = MAX_ITEMS) -> list[Item]:
    """The items directly in Downloads not changed for ``days`` days, biggest first."""
    if days not in DOWNLOAD_AGES:
        raise ValueError("not an offered age")
    cutoff = (time.time() if now is None else now) - days * DAY
    return _items(downloads_dir(environ), older_than=cutoff)[:limit]


def total(items) -> int:
    return sum(item.size for item in items)
