"""Searching a folder tree by name (no Qt)."""

from __future__ import annotations

import fnmatch
import os
import re
import time
from typing import Callable, Iterator

from .listing import Entry, _fill

_BATCH_SECONDS = 0.15
_BATCH_SIZE = 200


def matcher(query: str) -> Callable[[str], bool]:
    """How a name is tested against what the user typed.

    Every word must occur in the name, in any order and any case. A query
    with ``*`` or ``?`` is a pattern for the whole name instead
    (``*.txt``, ``report-??.pdf``).
    """
    query = query.strip()
    if "*" in query or "?" in query:
        pattern = re.compile(fnmatch.translate(query.casefold()))
        return lambda name: pattern.match(name.casefold()) is not None
    words = query.casefold().split()

    def contains_all(name: str) -> bool:
        folded = name.casefold()
        return all(word in folded for word in words)

    return contains_all


def search(root: str, query: str, cancelled: Callable[[], bool] = lambda: False, *,
           hidden: bool = False) -> Iterator[list[Entry]]:
    """Yield lists of entries below ``root`` whose names match ``query``, as they are found.

    Folders are searched breadth first, links to folders are reported but
    not entered (so a link cannot make the search run in circles), folders
    that cannot be read are passed over. Hidden files and folders are left
    out unless ``hidden``. An empty list is yielded now and then so the
    caller can show that the search is still running.
    """
    matches = matcher(query)
    if not query.strip():
        return
    batch: list[Entry] = []
    last = time.monotonic()
    queue = [root]
    position = 0
    while position < len(queue):
        directory = queue[position]
        position += 1
        if position > 4096:                     # keep the queue from growing without bound
            del queue[:position]
            position = 0
        if cancelled():
            return
        try:
            iterator = os.scandir(directory)
        except OSError:
            continue
        with iterator:
            for item in iterator:
                name = item.name
                if not hidden and name.startswith("."):
                    continue
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                if is_dir:
                    queue.append(item.path)
                if matches(name):
                    entry = Entry(name=name, path=item.path, hidden=name.startswith("."), location=directory)
                    try:
                        _fill(entry, item.stat(follow_symlinks=False), item.path)
                    except OSError:
                        continue
                    batch.append(entry)
                now = time.monotonic()
                if len(batch) >= _BATCH_SIZE or now - last >= _BATCH_SECONDS:
                    yield batch
                    batch = []
                    last = now
                    if cancelled():
                        return
    if batch:
        yield batch
