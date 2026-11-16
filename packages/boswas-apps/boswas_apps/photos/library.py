"""The pictures of the gallery: finding them, ordering them, months, folders, search (standard library only).

``scan`` walks the folders the user chose and returns one ``Item`` per
picture file. It does not open the files: a picture is a file whose
extension names a format this installation can read, and its date is the
modification time of the file. Hidden files and folders (names that start
with a dot) are left out, symbolic links to folders are not followed (so a
link that points upwards cannot make the walk endless), and the walk stops
at ``MAX_ITEMS`` files and ``MAX_DEPTH`` levels.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .natsort import natural_key

MAX_ITEMS = 500_000
MAX_DEPTH = 32
MAX_FOLDER_ENTRIES = 200_000
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
_USER_DIR = re.compile(r'^\s*XDG_PICTURES_DIR\s*=\s*"((?:[^"\\]|\\.)*)"\s*$')


@dataclass(frozen=True, slots=True)
class Item:
    path: str
    mtime: float
    size: int

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


@dataclass(frozen=True, slots=True)
class Section:
    """A run of items under one heading: ``count`` items from index ``start``."""

    title: str
    start: int
    count: int


@dataclass(frozen=True, slots=True)
class Folder:
    path: str
    count: int
    cover: Item            # the newest picture in it

    @property
    def name(self) -> str:
        return os.path.basename(self.path) or self.path


def pictures_dir(home: str | None = None, config_home: str | None = None) -> str:
    """The user's Pictures folder: ``XDG_PICTURES_DIR`` of ``user-dirs.dirs``, else ``~/Pictures``."""
    home = home or os.path.expanduser("~")
    if config_home is None:
        value = os.environ.get("XDG_CONFIG_HOME") or ""
        config_home = value if value.startswith("/") else os.path.join(home, ".config")
    try:
        with open(os.path.join(config_home, "user-dirs.dirs"), "r", encoding="utf-8", errors="replace") as handle:
            for line in handle.read(64 * 1024).splitlines():
                match = _USER_DIR.match(line)
                if not match:
                    continue
                value = re.sub(r"\\(.)", r"\1", match.group(1))
                if value.startswith("$HOME/"):
                    value = os.path.join(home, value[6:])
                elif value == "$HOME":
                    value = home
                if value.startswith("/") and os.path.normpath(value) != os.path.normpath(home):
                    return os.path.normpath(value)
    except OSError:
        pass
    return os.path.join(home, "Pictures")


def distinct_roots(roots: Iterable[str]) -> list[str]:
    """``roots`` as absolute paths without repeats and without folders that lie inside another root."""
    resolved: list[tuple[str, str]] = []
    for root in roots:
        if not isinstance(root, str) or not root:
            continue
        absolute = os.path.abspath(os.path.expanduser(root))
        real = os.path.realpath(absolute)
        if any(real == other or real.startswith(other.rstrip(os.sep) + os.sep) for _a, other in resolved):
            continue
        resolved = [(a, other) for a, other in resolved
                    if not other.startswith(real.rstrip(os.sep) + os.sep)]
        resolved.append((absolute, real))
    return [absolute for absolute, _real in resolved]


def scan(roots: Iterable[str], extensions: Iterable[str], *, cancelled: Callable[[], bool] = lambda: False,
         limit: int = MAX_ITEMS, skip: Iterable[str] = ()) -> list[Item]:
    """Every picture under ``roots`` (see the module text), in no particular order.

    ``extensions`` are lower-case extensions without the dot. ``skip`` are
    folders that are not entered. Folders that cannot be read are left out.
    """
    wanted = frozenset(extensions)
    skipped = {os.path.realpath(path) for path in skip}
    items: list[Item] = []
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[str, int]] = [(root, 0) for root in reversed(distinct_roots(roots))]
    while stack and len(items) < limit:
        if cancelled():
            break
        directory, depth = stack.pop()
        try:
            info = os.stat(directory)
        except OSError:
            continue
        identity = (info.st_dev, info.st_ino)
        if identity in seen or os.path.realpath(directory) in skipped:
            continue
        seen.add(identity)
        try:
            with os.scandir(directory) as entries:
                subfolders = []
                for entry in entries:
                    name = entry.name
                    if name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < MAX_DEPTH:
                                subfolders.append(entry.path)
                            continue
                        dot = name.rfind(".")
                        if dot <= 0 or name[dot + 1:].lower() not in wanted:
                            continue
                        if not entry.is_file():
                            continue
                        status = entry.stat()
                    except OSError:
                        continue
                    items.append(Item(entry.path, status.st_mtime, status.st_size))
                    if len(items) >= limit:
                        break
        except OSError:
            continue
        for path in sorted(subfolders, reverse=True):
            stack.append((path, depth + 1))
    return items


def newest_first(items: Iterable[Item]) -> list[Item]:
    """The order of the gallery: newest first; pictures of the same second in natural name order."""
    return sorted(items, key=lambda item: (-int(item.mtime), natural_key(item.name), item.path))


def by_name(items: Iterable[Item]) -> list[Item]:
    return sorted(items, key=lambda item: (natural_key(item.name), item.path))


def month_title(mtime: float) -> str:
    return _month(mtime)[0]


def _month(mtime: float) -> tuple[str, float, float]:
    """The title of the month ``mtime`` lies in (local time) and the time span of that month."""
    try:
        moment = time.localtime(mtime)
        low = time.mktime((moment.tm_year, moment.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        following = (moment.tm_year + 1, 1) if moment.tm_mon == 12 else (moment.tm_year, moment.tm_mon + 1)
        high = time.mktime((*following, 1, 0, 0, 0, 0, 0, -1))
    except (OverflowError, OSError, ValueError):
        return "Unknown date", mtime, mtime
    return f"{MONTHS[moment.tm_mon - 1]} {moment.tm_year}", low, high


def months(items: list[Item]) -> list[Section]:
    """The month sections of ``items`` (which are in ``newest_first`` order)."""
    sections: list[Section] = []
    title: str | None = None
    low = high = 0.0
    start = 0
    for index, item in enumerate(items):
        if title is not None and low <= item.mtime < high:
            continue                              # still in the month of the section that is open
        current, low, high = _month(item.mtime)
        if current != title:
            if title is not None:
                sections.append(Section(title, start, index - start))
            title, start = current, index
    if title is not None:
        sections.append(Section(title, start, len(items) - start))
    return sections


def search(items: list[Item], query: str) -> list[Item]:
    """The items whose file name contains every word of ``query`` (case does not matter)."""
    words = query.casefold().split()
    if not words:
        return list(items)
    return [item for item in items if all(word in item.name.casefold() for word in words)]


def folders(items: Iterable[Item]) -> list[Folder]:
    """The folders that hold pictures, with how many and the newest one, in natural order of their paths."""
    counts: dict[str, int] = {}
    covers: dict[str, Item] = {}
    for item in items:
        folder = item.folder
        counts[folder] = counts.get(folder, 0) + 1
        cover = covers.get(folder)
        if cover is None or item.mtime > cover.mtime:
            covers[folder] = item
    return [Folder(path, counts[path], covers[path])
            for path in sorted(counts, key=lambda p: [natural_key(part) for part in p.split(os.sep)])]


def in_folder(items: Iterable[Item], folder: str) -> list[Item]:
    return [item for item in items if item.folder == folder]


def siblings(path: str, extensions: Iterable[str]) -> list[str]:
    """The pictures in the folder of ``path`` in natural name order, ``path`` itself always among them.

    Hidden files are left out (unless ``path`` is one). OSError when the
    folder cannot be read.
    """
    wanted = frozenset(extensions)
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    names: list[str] = []
    own = os.path.basename(path)
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_FOLDER_ENTRIES:
                break
            name = entry.name
            if name == own or name.startswith("."):
                continue
            dot = name.rfind(".")
            if dot <= 0 or name[dot + 1:].lower() not in wanted:
                continue
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            names.append(name)
    if os.path.lexists(path):
        names.append(own)
    names.sort(key=natural_key)
    return [os.path.join(folder, name) for name in names]


def pictures_in(folder: str, extensions: Iterable[str]) -> list[str]:
    """The pictures directly in ``folder`` in natural name order (hidden files left out)."""
    wanted = frozenset(extensions)
    names = []
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_FOLDER_ENTRIES:
                break
            name = entry.name
            dot = name.rfind(".")
            if name.startswith(".") or dot <= 0 or name[dot + 1:].lower() not in wanted:
                continue
            try:
                if entry.is_file():
                    names.append(name)
            except OSError:
                continue
    names.sort(key=natural_key)
    return [os.path.join(folder, name) for name in names]


def folder_items(folder: str, extensions: Iterable[str], include: str | None = None) -> list[Item]:
    """The pictures directly in ``folder`` as items, in natural name order.

    Hidden files are left out. ``include`` (a path in the folder) is always
    among them when it exists, whatever its name: the file the user opened.
    OSError when the folder cannot be read.
    """
    wanted = frozenset(extensions)
    own = os.path.basename(include) if include else None
    found: list[Item] = []
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_FOLDER_ENTRIES:
                break
            name = entry.name
            if name != own:
                dot = name.rfind(".")
                if name.startswith(".") or dot <= 0 or name[dot + 1:].lower() not in wanted:
                    continue
            try:
                if not entry.is_file():
                    continue
                status = entry.stat()
            except OSError:
                continue
            found.append(Item(entry.path, status.st_mtime, status.st_size))
    return by_name(found)


def item_for(path: str) -> Item:
    """The item of one file (OSError when it cannot be examined)."""
    status = os.stat(path)
    return Item(os.path.abspath(path), status.st_mtime, status.st_size)
