"""Small JSON files: read with a size cap, written atomically, set aside when damaged (standard library only)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class StoreError(Exception):
    """A file that exists but cannot be used (too large, not JSON, unreadable)."""


def read_json(path: Path, max_bytes: int) -> Any:
    """The JSON value in ``path``, or None when there is no such file. Raises StoreError for a bad file."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError(f"{path.name} could not be read ({exc.strerror or exc})") from exc
    if len(data) > max_bytes:
        raise StoreError(f"{path.name} is larger than {max_bytes // 1024} KB")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StoreError(f"{path.name} is not a valid JSON file") from exc


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` in one step: a private temporary file beside it, flushed, renamed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, value: Any) -> None:
    """Write ``value`` as JSON into ``path`` atomically."""
    text = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=False, allow_nan=False)
    write_bytes_atomic(path, (text + "\n").encode("utf-8"))


def set_aside(path: Path) -> Path | None:
    """Rename a damaged file to ``<name>.damaged`` (replacing an older one); the new path, or None."""
    target = path.with_name(path.name + ".damaged")
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target


def remove(path: Path) -> None:
    """Delete a file if it is there."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
