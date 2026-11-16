"""JSON files shared between programs: bounded reading, atomic writing, locked updating (no Qt).

A file is always replaced as a whole (written beside itself, synced,
renamed over the old one), so a reader sees the old document or the new
one and never half of one. Programs that both change the same file
(Clock and the reminder service change ``clock.json``) do it through
``update_json``, which holds an advisory lock on ``<file>.lock`` while it
reads, changes and writes.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator

MAX_BYTES = 64 * 1024 * 1024


class StoreError(Exception):
    """A file could not be read or written; the message is fit for the user."""


def read_json(path: str | os.PathLike, *, max_bytes: int = MAX_BYTES) -> Any:
    """The document in ``path``; None when the file does not exist.

    Raises StoreError when the file cannot be read, is larger than
    ``max_bytes``, is not UTF-8 JSON or is nested absurdly deep.
    """
    target = Path(path)
    try:
        with open(target, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError(f"{target.name} cannot be read: {exc.strerror or exc}") from exc
    if len(raw) > max_bytes:
        raise StoreError(f"{target.name} is larger than {max_bytes // (1024 * 1024)} MB")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StoreError(f"{target.name} is not a valid JSON document") from exc
    except RecursionError as exc:
        raise StoreError(f"{target.name} is nested too deeply") from exc


def write_json_atomic(path: str | os.PathLike, document: Any, *, sync: bool = True) -> None:
    """Replace ``path`` with ``document`` (mode 0600; the directory is created 0700)."""
    target = Path(path)
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                if sync:
                    os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        if sync:
            with contextlib.suppress(OSError):
                directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    except (OSError, ValueError, TypeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise StoreError(f"{target.name} cannot be written: {reason}") from exc


@contextlib.contextmanager
def locked(path: str | os.PathLike) -> Iterator[None]:
    """Hold the advisory lock of ``path`` (the file ``<path>.lock``) for the block."""
    target = Path(path)
    lock_path = target.with_name(target.name + ".lock")
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise StoreError(f"{target.name} cannot be locked: {exc.strerror or exc}") from exc
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                break
            except OSError as exc:
                if exc.errno != errno.EINTR:
                    raise StoreError(f"{target.name} cannot be locked: {exc.strerror or exc}") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def update_json(path: str | os.PathLike, change: Callable[[Any], Any], *, max_bytes: int = MAX_BYTES,
                sync: bool = True) -> Any:
    """Read ``path``, let ``change(document)`` return the new document, write it; all under the lock.

    ``change`` receives None when the file does not exist (or what an unreadable
    file leaves: None, after the damaged file was set aside as
    ``<name>.damaged``). When it returns the very document unchanged and equal
    to what was read, nothing is written. Returns the new document.
    """
    target = Path(path)
    with locked(target):
        try:
            current = read_json(target, max_bytes=max_bytes)
        except StoreError:
            set_aside(target)
            current = None
        before = json.dumps(current, sort_keys=True) if current is not None else None
        result = change(current)
        if before is None or json.dumps(result, sort_keys=True) != before:
            write_json_atomic(target, result, sync=sync)
        return result


def set_aside(path: str | os.PathLike) -> Path | None:
    """Rename a damaged file to ``<name>.damaged`` (replacing an older one); the new name, or None."""
    target = Path(path)
    aside = target.with_name(target.name + ".damaged")
    try:
        os.replace(target, aside)
    except OSError:
        return None
    return aside


def signature(path: str | os.PathLike) -> tuple[int, int, int] | None:
    """(inode, size, mtime in ns) of ``path``, to tell whether a file was replaced; None when missing."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns
