"""Small file helpers shared by the modules of Photos (standard library only)."""

from __future__ import annotations

import os
import tempfile

_UNITS = ("bytes", "KB", "MB", "GB", "TB")


def atomic_write(path: str | os.PathLike, data: bytes, *, mode: int | None = None) -> None:
    """Write ``data`` to ``path`` so that a reader sees the old file or the new one, never a part.

    The data goes to a temporary file in the same directory, is flushed to
    disk and renamed over ``path``. ``mode`` is the permission of a new
    file (default 0644 less the umask); an existing file keeps its own. A
    symbolic link at ``path`` is followed, so the file it names is
    replaced and the link stays.
    """
    target = os.path.realpath(os.fspath(path))
    directory = os.path.dirname(target) or "."
    try:
        permissions = os.stat(target).st_mode & 0o7777
    except OSError:
        if mode is None:
            umask = os.umask(0)
            os.umask(umask)
            permissions = 0o644 & ~umask
        else:
            permissions = mode
    descriptor, temporary = tempfile.mkstemp(prefix=".", suffix=".part", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, permissions)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(folder)
    except OSError:
        pass
    finally:
        os.close(folder)


def free_name(path: str | os.PathLike) -> str:
    """``path`` if nothing is there, else the first of ``name (2).ext``, ``name (3).ext``, ... that is free."""
    path = os.fspath(path)
    if not os.path.lexists(path):
        return path
    directory, name = os.path.split(path)
    stem, suffix = os.path.splitext(name)
    for number in range(2, 100000):
        candidate = os.path.join(directory, f"{stem} ({number}){suffix}")
        if not os.path.lexists(candidate):
            return candidate
    raise FileExistsError(path)


def format_size(size: int) -> str:
    """A file size for people: ``312 bytes``, ``4.2 MB``."""
    value = float(max(0, size))
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            if unit == "bytes":
                return f"{int(value)} bytes" if int(value) != 1 else "1 byte"
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
        value /= 1024
    return f"{size} bytes"


def check_new_name(name: str) -> str | None:
    """Why ``name`` cannot be the name of a file, in plain words; None when it can."""
    if not name or not name.strip():
        return "The name is empty."
    if name in (".", ".."):
        return "This name is reserved."
    if "/" in name or "\x00" in name:
        return "A name cannot contain a slash."
    if any(ord(ch) < 0x20 for ch in name):
        return "A name cannot contain control characters."
    try:
        encoded = os.fsencode(name)
    except (UnicodeError, ValueError):
        return "The name has characters that cannot be stored."
    if len(encoded) > 255:
        return "The name is too long."
    return None


def rename_no_replace(old: str | os.PathLike, new: str | os.PathLike) -> None:
    """Rename a file, never onto an existing one. FileExistsError when ``new`` is taken.

    A hard link under the new name followed by removing the old one cannot
    overwrite anything; file systems without hard links get a check
    followed by a plain rename.
    """
    old, new = os.fspath(old), os.fspath(new)
    if os.path.lexists(new):
        raise FileExistsError(new)
    try:
        os.link(old, new, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError:
        if os.path.lexists(new):
            raise FileExistsError(new) from None
        os.rename(old, new)
        return
    os.unlink(old)
