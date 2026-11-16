"""Moving files to the Trash, after the freedesktop.org Trash specification 1.0 (standard library only).

``trash(path)`` moves one file (or symbolic link; the link itself, never
its target) into a trash directory and writes the ``.trashinfo`` record the
file manager needs to show and restore it:

* a file on the same file system as the home trash goes to
  ``$XDG_DATA_HOME/Trash``;
* a file on another file system goes to ``<mount point>/.Trash/<uid>`` when
  an administrator has set that directory up as the specification demands
  (a real directory with the sticky bit), else to
  ``<mount point>/.Trash-<uid>``; when neither can be used the file is
  copied to the home trash and then removed.

The record is created first, with ``O_EXCL``, which also reserves the name
in the trash; then the file is renamed. ``restore`` puts a file back where
it was, for "Undo".
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

MAX_NAME_ATTEMPTS = 10000


class TrashError(OSError):
    """The file could not be moved to the Trash; the text says why in plain words."""


@dataclass(frozen=True)
class TrashRecord:
    """Where a trashed file was and where it is now."""

    original: str
    trashed: str
    info: str


def home_trash() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or ""
    root = Path(base) if base.startswith("/") else Path.home() / ".local" / "share"
    return root / "Trash"


def mount_point(path: str) -> str:
    """The top directory of the file system ``path`` (an existing absolute path) lives on."""
    current = os.path.dirname(path) or "/"
    device = os.lstat(current).st_dev
    while current != "/":
        parent = os.path.dirname(current)
        try:
            if os.lstat(parent).st_dev != device:
                break
        except OSError:
            break
        current = parent
    return current


def _ensure(directory: Path) -> None:
    for name in ("files", "info"):
        (directory / name).mkdir(mode=0o700, parents=True, exist_ok=True)


def _usable(directory: Path) -> bool:
    try:
        _ensure(directory)
    except OSError:
        return False
    return os.access(directory / "files", os.W_OK | os.X_OK) and os.access(directory / "info", os.W_OK | os.X_OK)


def _top_trash(top: str) -> Path | None:
    """The trash directory of this user on the file system mounted at ``top``, made if needed."""
    uid = os.getuid()
    shared = Path(top) / ".Trash"
    try:
        info = os.lstat(shared)
        # The specification: a real directory (no link) with the sticky bit, else it must not be used.
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_mode & stat.S_ISVTX:
            mine = shared / str(uid)
            if _usable(mine) and os.lstat(mine).st_uid == uid:
                return mine
    except OSError:
        pass
    own = Path(top) / f".Trash-{uid}"
    try:
        if own.is_symlink():
            return None
        if _usable(own) and os.lstat(own).st_uid == uid:
            return own
    except OSError:
        pass
    return None


def _info_text(recorded_path: str, when: float) -> bytes:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(when))
    encoded = quote(os.fsencode(recorded_path), safe="/")
    return f"[Trash Info]\nPath={encoded}\nDeletionDate={stamp}\n".encode("ascii")


def _reserve(directory: Path, name: str, recorded_path: str, when: float) -> tuple[Path, Path]:
    """Create the record for ``name`` (or a free variant of it); returns (record, place for the file)."""
    stem, suffix = os.path.splitext(name)
    if not stem:
        stem, suffix = name, ""
    text = _info_text(recorded_path, when)
    for attempt in range(MAX_NAME_ATTEMPTS):
        candidate = name if attempt == 0 else f"{stem}.{attempt + 1}{suffix}"
        info = directory / "info" / f"{candidate}.trashinfo"
        target = directory / "files" / candidate
        try:
            descriptor = os.open(info, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(text)
        except OSError:
            _remove_quietly(info)
            raise
        if os.path.lexists(target):            # a file without a record: leave it, take another name
            _remove_quietly(info)
            continue
        return info, target
    raise TrashError("The Trash has too many files with this name.")


def _remove_quietly(path: os.PathLike | str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def trash(path: str | os.PathLike, *, now: float | None = None) -> TrashRecord:
    """Move ``path`` to the Trash; returns where it went. TrashError (an OSError) when that fails."""
    original = os.path.abspath(os.fspath(path))
    try:
        info = os.lstat(original)
    except OSError as exc:
        raise TrashError(f"{os.path.basename(original)} is no longer there.") from exc
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        raise TrashError("Folders are not moved to the Trash from here.")
    when = time.time() if now is None else now
    name = os.path.basename(original)
    home = home_trash()
    try:
        _ensure(home)
        home_device = os.lstat(home).st_dev
    except OSError as exc:
        raise TrashError(f"The Trash folder cannot be used: {exc.strerror or exc}.") from exc

    directory, recorded, copy = home, original, False
    if info.st_dev != home_device or os.lstat(os.path.dirname(original)).st_dev != home_device:
        top = mount_point(original)
        candidate = _top_trash(top)
        if candidate is not None:
            directory = candidate
            recorded = os.path.relpath(original, top)
        else:
            copy = True                        # no trash on that file system: copy into the home trash
    try:
        record, target = _reserve(directory, name, recorded, when)
    except TrashError:
        raise
    except OSError as exc:
        raise TrashError(f"The Trash cannot be written: {exc.strerror or exc}.") from exc
    try:
        if copy:
            shutil.copy2(original, target, follow_symlinks=False)
            os.unlink(original)
        else:
            os.rename(original, target)
    except OSError as exc:
        _remove_quietly(record)
        if copy:
            _remove_quietly(target)
        raise TrashError(f"{name} could not be moved to the Trash: {exc.strerror or exc}.") from exc
    return TrashRecord(original, str(target), str(record))


def restore(record: TrashRecord) -> str:
    """Put a trashed file back; returns its path. TrashError when something is in the way or it is gone."""
    if os.path.lexists(record.original):
        raise TrashError(f"{os.path.basename(record.original)} exists again; the file stays in the Trash.")
    if not os.path.lexists(record.trashed):
        raise TrashError("The file is no longer in the Trash.")
    try:
        os.makedirs(os.path.dirname(record.original), exist_ok=True)
        try:
            os.rename(record.trashed, record.original)
        except OSError:
            shutil.copy2(record.trashed, record.original, follow_symlinks=False)
            os.unlink(record.trashed)
    except OSError as exc:
        raise TrashError(f"The file could not be put back: {exc.strerror or exc}.") from exc
    _remove_quietly(record.info)
    return record.original
