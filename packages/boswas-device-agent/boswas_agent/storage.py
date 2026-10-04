"""Small, careful file helpers for agent state.

Every write is atomic (temporary file in the same directory, fsync, rename)
with an explicit mode, and never follows a symbolic link at the target.
Reads are bounded and refuse symbolic links.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path

MAX_JSON_BYTES = 4 * 1024 * 1024


def ensure_dir(path: Path, mode: int) -> Path:
    """Create a real directory (not a symlink) with exactly this mode."""
    path = Path(path)
    try:
        path.mkdir(mode=mode, parents=False)
    except FileExistsError:
        pass
    except FileNotFoundError:
        ensure_dir(path.parent, 0o755)
        try:
            path.mkdir(mode=mode)
        except FileExistsError:          # created concurrently
            pass
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is not a directory (symbolic links are not accepted)")
    if stat.S_IMODE(st.st_mode) != mode:
        os.chmod(path, mode, follow_symlinks=False)
    return path


def atomic_write(path: Path, data: bytes | str, mode: int = 0o644) -> None:
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = path.parent / f".{path.name}.{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, doc: object, mode: int = 0o644) -> None:
    atomic_write(path, json.dumps(doc, indent=2, sort_keys=True) + "\n", mode)


def read_bytes(path: Path, limit: int = MAX_JSON_BYTES) -> bytes | None:
    """A regular file's content (no symlinks, bounded), or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_json(path: Path, limit: int = MAX_JSON_BYTES) -> object | None:
    data = read_bytes(path, limit)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def remove_file(path: Path) -> bool:
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


def is_insecure(path: Path, *, owner: int = 0) -> str | None:
    """Why a root-managed file must not be trusted (None if it may be)."""
    try:
        st = Path(path).lstat()
    except OSError as exc:
        return exc.strerror or "unreadable"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_uid != owner:
        return f"not owned by uid {owner}"
    if st.st_mode & 0o022:
        return "writable by group or others"
    return None
