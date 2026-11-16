"""Reading a folder into entries and putting them in order (no Qt)."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Callable, Iterator

from .text import natural_key

SORT_COLUMNS = ("name", "date", "type", "size", "location", "deleted")


@dataclass(slots=True, eq=False)
class Entry:
    """One row of a folder view. ``is_dir`` follows a symbolic link; ``broken`` marks a link to nothing."""

    name: str
    path: str
    is_dir: bool = False
    is_symlink: bool = False
    broken: bool = False
    size: int = 0
    mtime: float = 0.0
    mode: int = 0
    hidden: bool = False
    key: tuple | None = None            # natural_key(name), computed once
    mime: str = ""                      # filled by the user interface (QMimeDatabase)
    type_text: str = ""
    location: str = ""                  # the folder (search results) or the original folder (Trash)
    deleted: float = 0.0                # when it was moved to the Trash
    extra: object = None                # the TrashItem of a Trash row

    def name_key(self) -> tuple:
        if self.key is None:
            self.key = natural_key(self.name)
        return self.key


def entry_for(path: str, name: str | None = None) -> Entry:
    """The entry of one path. Raises OSError when the path itself does not exist."""
    info = os.lstat(path)
    name = os.path.basename(path.rstrip("/")) or path if name is None else name
    entry = Entry(name=name, path=path, hidden=name.startswith("."))
    return _fill(entry, info, path)


def _fill(entry: Entry, info: os.stat_result, path: str) -> Entry:
    entry.is_symlink = stat.S_ISLNK(info.st_mode)
    if entry.is_symlink:
        try:
            info = os.stat(path)
        except OSError:
            entry.broken = True
    entry.is_dir = stat.S_ISDIR(info.st_mode)
    entry.size = 0 if entry.is_dir else info.st_size
    entry.mtime = info.st_mtime
    entry.mode = info.st_mode
    return entry


def scan(path: str, cancelled: Callable[[], bool] = lambda: False, chunk: int = 1000) -> Iterator[list[Entry]]:
    """Yield the entries of the folder ``path`` in lists of at most ``chunk``.

    Raises OSError (PermissionError, FileNotFoundError, NotADirectoryError)
    when the folder cannot be read. An entry that disappears while it is
    being looked at is left out. Stops early when ``cancelled()`` is true.
    """
    batch: list[Entry] = []
    with os.scandir(path) as iterator:
        for item in iterator:
            entry = Entry(name=item.name, path=item.path, hidden=item.name.startswith("."))
            try:
                _fill(entry, item.stat(follow_symlinks=False), item.path)
            except OSError:
                continue
            batch.append(entry)
            if len(batch) >= chunk:
                yield batch
                batch = []
                if cancelled():
                    return
    if batch:
        yield batch


def sort_entries(entries: list[Entry], column: str = "name", descending: bool = False, *,
                 folders_first: bool = True, presorted: bool = False) -> list[Entry]:
    """Sort ``entries`` in place (and return them). Folders stay first in both directions.

    ``column``: name, date, type (``type_text``), size, location, deleted.
    Rows equal in the column are ordered by name, ascending. ``presorted``
    says the entries are in name order already, which is the costly part.
    """
    if not presorted:
        entries.sort(key=Entry.name_key)
    if column == "date":
        entries.sort(key=lambda entry: entry.mtime, reverse=descending)
    elif column == "size":
        entries.sort(key=lambda entry: entry.size, reverse=descending)
    elif column == "type":
        keys: dict[str, tuple] = {}

        def type_key(entry: Entry) -> tuple:
            key = keys.get(entry.type_text)
            if key is None:
                key = keys[entry.type_text] = natural_key(entry.type_text)
            return key

        entries.sort(key=type_key, reverse=descending)
    elif column == "location":
        entries.sort(key=lambda entry: natural_key(entry.location), reverse=descending)
    elif column == "deleted":
        entries.sort(key=lambda entry: entry.deleted, reverse=descending)
    elif descending:
        entries.reverse()
    if folders_first:
        entries.sort(key=lambda entry: not entry.is_dir)
    return entries
