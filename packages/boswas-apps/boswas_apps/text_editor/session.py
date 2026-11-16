"""The session: which tabs were open, with the text that was not saved yet (standard library only, no Qt).

When the editor closes it writes ``session.json`` into its state directory
and the next start opens the same tabs again, unsaved text included, as
Notepad does. A tab of a saved, unchanged file is remembered by its path
alone and read from disk again. Unsaved text is kept in the session file
itself, up to 4 MiB per tab and 16 MiB in all; the editor asks about a tab
that is larger than that instead of keeping it silently incomplete.

The file is written atomically with mode 0600 and read with a size cap;
every value is checked, and a file that is damaged gives an empty session.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import codec
from .settings import valid_path, write_atomically

FILE_NAME = "session.json"
VERSION = 1
MAX_TABS = 100
MAX_TAB_TEXT = 4 * 1024 * 1024           # characters of unsaved text kept for one tab
MAX_TOTAL_TEXT = 16 * 1024 * 1024
_MAX_FILE = 5 * MAX_TOTAL_TEXT            # JSON escapes and four bytes a character at the very worst


@dataclass
class TabState:
    path: str | None = None               # None: a tab that was never saved
    text: str | None = None               # the unsaved text; None: read the file
    encoding: str = codec.UTF8.key
    line_ending: str = codec.LF
    cursor: int = 0
    title: str = ""                       # of an untitled tab

    def has_text(self) -> bool:
        return self.text is not None


def fits(tabs: list[TabState]) -> list[bool]:
    """For each tab whether its unsaved text can be kept (the caps of the module text, in order)."""
    result = []
    total = 0
    for index, tab in enumerate(tabs):
        size = len(tab.text) if tab.text is not None else 0
        keep = index < MAX_TABS and size <= MAX_TAB_TEXT and total + size <= MAX_TOTAL_TEXT
        if keep:
            total += size
        result.append(keep)
    return result


def dump(tabs: list[TabState], current: int = 0) -> bytes:
    records = []
    for tab, keep in zip(tabs, fits(tabs)):
        if not keep:
            continue
        records.append({"path": tab.path, "text": tab.text, "encoding": tab.encoding,
                        "line_ending": tab.line_ending, "cursor": tab.cursor, "title": tab.title})
    return json.dumps({"version": VERSION, "current": current, "tabs": records}, ensure_ascii=False).encode("utf-8")


def parse(data: bytes) -> tuple[list[TabState], int]:
    """(tabs, index of the current one) from the bytes of a session file; ([], 0) when they are not one."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return [], 0
    if not isinstance(raw, dict) or raw.get("version") != VERSION or not isinstance(raw.get("tabs"), list):
        return [], 0
    tabs: list[TabState] = []
    total = 0
    for record in raw["tabs"][:MAX_TABS]:
        if not isinstance(record, dict):
            continue
        path = record.get("path")
        text = record.get("text")
        if path is not None and not valid_path(path):
            continue
        if text is not None and (not isinstance(text, str) or len(text) > MAX_TAB_TEXT or "\x00" in text):
            continue
        if path is None and text is None:
            continue
        if text is not None:
            total += len(text)
            if total > MAX_TOTAL_TEXT:
                break
            text = codec.normalize_line_endings(text)
        line_ending = record.get("line_ending")
        cursor = record.get("cursor")
        title = record.get("title")
        encoding = record.get("encoding")
        tabs.append(TabState(
            path=path, text=text,
            encoding=codec.by_key(encoding).key if isinstance(encoding, str) else codec.UTF8.key,
            line_ending=line_ending if line_ending in codec.LINE_ENDINGS else codec.LF,
            cursor=cursor if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0 else 0,
            title=title if isinstance(title, str) and len(title) <= 200 and title.isprintable() else ""))
    current = raw.get("current")
    if isinstance(current, bool) or not isinstance(current, int) or not 0 <= current < len(tabs):
        current = 0
    return tabs, current


def load(directory: str | os.PathLike) -> tuple[list[TabState], int]:
    path = Path(directory) / FILE_NAME
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MAX_FILE + 1)
    except OSError:
        return [], 0
    if len(data) > _MAX_FILE:
        return [], 0
    return parse(data)


def save(directory: str | os.PathLike, tabs: list[TabState], current: int = 0) -> bool:
    return write_atomically(Path(directory) / FILE_NAME, dump(tabs, current))


def clear(directory: str | os.PathLike) -> None:
    try:
        os.unlink(Path(directory) / FILE_NAME)
    except OSError:
        pass
