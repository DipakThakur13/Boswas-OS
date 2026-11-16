"""MIME types of files, through QMimeDatabase: names, descriptions, icons, the chain used to find applications."""

from __future__ import annotations

import os

from PySide6.QtCore import QMimeDatabase, QMimeType
from PySide6.QtGui import QIcon

from . import uiicons
from .listing import Entry
from .text import split_extension

FOLDER = "inode/directory"
_database: QMimeDatabase | None = None
_by_suffix: dict[str, str] = {}
_descriptions: dict[str, str] = {}
_icons: dict[str, QIcon] = {}
_special_folders: dict[str, str] = {}
EXECUTABLE_TYPES = frozenset({"application/x-executable", "application/x-pie-executable", "application/x-sharedlib",
                              "application/x-shellscript", "application/x-desktop", "application/vnd.appimage",
                              "application/x-elf", "text/x-python", "text/x-python3", "application/x-perl",
                              "application/x-ruby", "application/javascript", "application/x-php",
                              "application/x-ms-dos-executable", "application/x-msdownload",
                              "application/vnd.microsoft.portable-executable"})


def database() -> QMimeDatabase:
    global _database
    if _database is None:
        _database = QMimeDatabase()
    return _database


def for_name(name: str) -> str:
    """The MIME type a file name suggests (no file is read). Results are remembered per extension."""
    suffix = split_extension(name)[1].lower()
    key = suffix or name
    found = _by_suffix.get(key)
    if found is None:
        found = database().mimeTypeForFile(name, QMimeDatabase.MatchMode.MatchExtension).name()
        if len(_by_suffix) < 20000:
            _by_suffix[key] = found
    return found


def for_file(path: str) -> str:
    """The MIME type of a file by name and content (reads the start of the file: call off the GUI thread)."""
    return database().mimeTypeForFile(path, QMimeDatabase.MatchMode.MatchDefault).name()


def fill(entry: Entry) -> None:
    """Set ``entry.mime`` and ``entry.type_text`` from what is known without reading the file."""
    if entry.is_dir:
        entry.mime = FOLDER
        entry.type_text = "File folder"
    elif entry.broken:
        entry.mime = "inode/symlink"
        entry.type_text = "Broken link"
    else:
        entry.mime = for_name(entry.name)
        entry.type_text = description(entry.mime)


def _type(name: str) -> QMimeType:
    return database().mimeTypeForName(name)


def description(name: str) -> str:
    """What a person calls the type: "Plain text document", "PNG image"."""
    found = _descriptions.get(name)
    if found is None:
        mime = _type(name)
        comment = mime.comment() if mime.isValid() else ""
        if name == "application/octet-stream" or not comment:
            comment = "File"
        found = comment[:1].upper() + comment[1:]
        _descriptions[name] = found
    return found


def chain(name: str) -> list[str]:
    """The type, its aliases and its ancestors, nearest first: the order in which applications are looked up."""
    mime = _type(name)
    if not mime.isValid():
        return [name]
    result = [mime.name(), *mime.aliases()]
    for ancestor in mime.allAncestors():
        if ancestor not in result:
            result.append(ancestor)
    return result


def inherits(name: str, parent: str) -> bool:
    mime = _type(name)
    return mime.isValid() and mime.inherits(parent)


def is_text(name: str) -> bool:
    return inherits(name, "text/plain")


def set_special_folders(folders: dict[str, str]) -> None:
    """Folders with an icon of their own (path -> icon name): the home folder, Documents, Downloads, ..."""
    _special_folders.clear()
    _special_folders.update(folders)


def icon(name: str) -> QIcon:
    """The icon of a MIME type: its own, its generic one, that of an ancestor, or the icon of an unknown file."""
    found = _icons.get(name)
    if found is not None:
        return found
    candidates: list[str] = []
    mime = _type(name)
    if mime.isValid():
        candidates += [mime.iconName(), mime.genericIconName()]
        for ancestor in mime.allAncestors():
            parent = _type(ancestor)
            if parent.isValid() and ancestor != "application/octet-stream":
                candidates += [parent.iconName(), parent.genericIconName()]
    else:
        candidates.append(name.replace("/", "-"))
    found = QIcon()
    for candidate in candidates:
        if candidate and uiicons.path_of(candidate) is not None:
            found = uiicons.named(candidate)
            break
    if found.isNull():
        found = uiicons.named(*(candidate for candidate in candidates if candidate), "unknown",
                              "application-octet-stream", "text-x-generic")
    _icons[name] = found
    return found


def entry_icon(entry: Entry) -> QIcon:
    if entry.is_dir:
        special = _special_folders.get(entry.path)
        return uiicons.named(special, "folder") if special else uiicons.named("folder")
    if not entry.mime:
        fill(entry)
    return icon(entry.mime if not entry.broken else "application/octet-stream")


def is_executable(path: str, mime_name: str, mode: int) -> bool:
    """Whether opening the file would run it: a program, a script with the execute bit, a desktop entry."""
    if mime_name == "application/x-desktop" or os.path.basename(path).endswith(".desktop"):
        return True
    if not mode & 0o111:
        return False
    return mime_name in EXECUTABLE_TYPES or inherits(mime_name, "application/x-executable") or is_text(mime_name) \
        or mime_name == "application/octet-stream"
