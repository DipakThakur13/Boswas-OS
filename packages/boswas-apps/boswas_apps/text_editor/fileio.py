"""Reading and writing the files of the editor (standard library only, no Qt).

``load`` reads a file in pieces (so a caller can show progress and cancel),
finds its encoding and line ends and returns text with ``\\n`` line ends.
``save`` writes atomically: the bytes go to a temporary file in the same
directory, which then replaces the file, so a crash or a full disk never
leaves half a file. The permissions of an existing file are kept (and its
owner and group where the system allows). A symbolic link is followed: the
file it points to is replaced, the link stays. Where the directory does not
allow a temporary file but the file itself is writable, it is written in
place.

``signature`` is what the editor remembers of a file on disk (time, size,
inode) to notice that another program changed it.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from dataclasses import dataclass
from typing import Callable

from . import codec

MIB = 1024 * 1024
LARGE_FILE = 32 * MIB          # from this size on the editor asks before it opens a file
MAX_FILE = 512 * MIB           # larger files are refused
_CHUNK = MIB


class LoadError(Exception):
    """A file could not be opened; ``kind`` says why and the text is fit to show to the user."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class Cancelled(Exception):
    """The caller's ``cancelled()`` answered True."""


@dataclass(frozen=True)
class Signature:
    mtime_ns: int
    size: int
    inode: int
    device: int


@dataclass
class Loaded:
    path: str
    text: str
    encoding: codec.Encoding
    line_ending: str
    line_endings: dict[str, int]
    signature: Signature | None
    writable: bool

    @property
    def mixed(self) -> bool:
        return codec.is_mixed(self.line_endings)


def signature(path: str) -> Signature | None:
    """The signature of the file at ``path`` (None when there is no such file)."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return Signature(info.st_mtime_ns, info.st_size, info.st_ino, info.st_dev)


def describe_size(size: int) -> str:
    if size >= 1024 * MIB:
        return f"{size / (1024 * MIB):.1f} GB"
    if size >= MIB:
        return f"{size / MIB:.0f} MB" if size >= 10 * MIB else f"{size / MIB:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} bytes"


def _problem(path: str, error: OSError, action: str) -> LoadError:
    name = os.path.basename(path) or path
    if isinstance(error, FileNotFoundError):
        return LoadError("missing", f"“{name}” does not exist.")
    if isinstance(error, IsADirectoryError):
        return LoadError("directory", f"“{name}” is a folder, not a file.")
    if isinstance(error, PermissionError):
        return LoadError("permission", f"You do not have permission to {action} “{name}”.")
    return LoadError("io", f"“{name}” could not be {'read' if action == 'open' else 'written'}: "
                           f"{error.strerror or error}.")


def check(path: str, *, max_bytes: int = MAX_FILE) -> int:
    """The size of a file that can be opened. Raises LoadError for anything else (a folder, a device, too big)."""
    try:
        info = os.stat(path)
    except OSError as error:
        raise _problem(path, error, "open") from error
    name = os.path.basename(path) or path
    if stat.S_ISDIR(info.st_mode):
        raise LoadError("directory", f"“{name}” is a folder, not a file.")
    if not stat.S_ISREG(info.st_mode):
        raise LoadError("special", f"“{name}” is not a regular file.")
    if info.st_size > max_bytes:
        raise LoadError("too_large", f"“{name}” is {describe_size(info.st_size)}. Text Editor opens files "
                                     f"of up to {describe_size(max_bytes)}.")
    return info.st_size


def load(path: str, *, max_bytes: int = MAX_FILE, progress: Callable[[float], None] | None = None,
         cancelled: Callable[[], bool] | None = None, encoding: codec.Encoding | None = None,
         default_line_ending: str = codec.LF) -> Loaded:
    """Read the text file at ``path``.

    ``progress`` is called with 0.0 to 1.0 while the file is read, and
    ``cancelled`` is asked between the pieces (Cancelled is raised when it
    says yes). ``encoding`` reads the file in that encoding instead of the
    detected one. Raises LoadError with a message for the user.
    """
    name = os.path.basename(path) or path
    size = check(path, max_bytes=max_bytes)
    pieces: list[bytes] = []
    read = 0
    try:
        with open(path, "rb") as handle:
            mark = signature_of(handle.fileno())
            while True:
                if cancelled is not None and cancelled():
                    raise Cancelled()
                piece = handle.read(_CHUNK)
                if not piece:
                    break
                pieces.append(piece)
                read += len(piece)
                if read > max_bytes:
                    raise LoadError("too_large", f"“{name}” is larger than {describe_size(max_bytes)}.")
                if progress is not None:
                    progress(min(read / size, 1.0) if size else 1.0)
    except OSError as error:
        raise _problem(path, error, "open") from error
    data = b"".join(pieces)
    del pieces
    try:
        used = encoding or codec.detect(data, codec.locale_encoding())
    except codec.BinaryFileError:
        raise LoadError("binary", f"“{name}” is not a text file. Text Editor only opens text.") from None
    try:
        text = codec.decode(data, used)
    except UnicodeDecodeError:
        raise LoadError("decode", f"“{name}” is not valid {used.label} text.") from None
    if "\x00" in text:
        raise LoadError("binary", f"“{name}” is not a text file. Text Editor only opens text.")
    del data
    counts = codec.count_line_endings(text)
    return Loaded(path, codec.normalize_line_endings(text), used, codec.dominant_line_ending(counts,
                  default_line_ending), counts, mark, os.access(path, os.W_OK))


def signature_of(descriptor: int) -> Signature:
    info = os.fstat(descriptor)
    return Signature(info.st_mtime_ns, info.st_size, info.st_ino, info.st_dev)


def to_bytes(text: str, encoding: codec.Encoding, line_ending: str) -> bytes:
    """The bytes of a file for text with ``\\n`` line ends. Raises UnicodeEncodeError."""
    return codec.encode(codec.to_line_ending(text, line_ending), encoding)


def save(path: str, data: bytes) -> Signature:
    """Write ``data`` to ``path`` atomically (see the module text); returns the new signature.

    Raises OSError; the file that was there is untouched then.
    """
    target = os.path.realpath(path)
    directory = os.path.dirname(target) or "."
    try:
        previous = os.stat(target)
    except FileNotFoundError:
        previous = None
    if previous is not None and not stat.S_ISREG(previous.st_mode):
        raise OSError(errno.EINVAL, "not a regular file", target)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(target)[:64]}.", suffix=".tmp",
                                                 dir=directory)
    except OSError:
        if previous is None or not os.access(target, os.W_OK):
            raise
        return _save_in_place(target, data)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            if previous is not None:
                os.fchmod(handle.fileno(), stat.S_IMODE(previous.st_mode))
                if (previous.st_uid, previous.st_gid) != (os.geteuid(), os.getegid()):
                    try:
                        os.fchown(handle.fileno(), previous.st_uid, previous.st_gid)
                    except OSError:
                        pass                 # not ours to give away: the file becomes the user's own
            else:
                mask = os.umask(0)
                os.umask(mask)
                os.fchmod(handle.fileno(), 0o666 & ~mask)
            os.fsync(handle.fileno())
            mark = signature_of(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return mark


def _save_in_place(target: str, data: bytes) -> Signature:
    with open(target, "r+b") as handle:
        handle.write(data)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
        return signature_of(handle.fileno())
