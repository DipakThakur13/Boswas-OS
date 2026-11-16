"""The optional ``7z`` program: where it is, what it lists, and reading entries through a pipe. No Qt.

7z is only ever asked to *read*: it lists an archive, tests it, or writes
the bytes of entries to its standard output. It never creates a file, so
whatever a hostile archive makes it do, nothing lands on disk except through
the extractor of this application.

The program is looked up at fixed paths and started with an argument list,
never through a shell. A password goes to its standard input (7z asks for
it there), not onto the command line, where other processes could read it.
Entry names are passed as ``-i!name`` with ``-spd`` (no wild cards), so a
name cannot be taken for a switch or a list file.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from .errors import ArchiveError, Cancelled, PasswordRequired, WrongPassword

PROGRAMS = ("/usr/bin/7z", "/usr/bin/7zz", "/usr/bin/7za")
NEEDS = "needs the 7zip package"
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
_POSIX_MODE = re.compile(r"^[-dlcbps][-rwxsStT]{9}$")
_MAX_STDERR = 64 * 1024
_MAX_LISTING_LINE = 64 * 1024


def find_program() -> str | None:
    """The 7z program of this system, or None when the 7zip package is not installed."""
    for candidate in PROGRAMS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


@dataclass
class Item:
    """One block of ``7z l -slt``."""

    path: str = ""
    size: int = -1
    packed: int = -1
    mtime: float | None = None
    attributes: str = ""
    crc: int | None = None
    encrypted: bool = False
    method: str = ""
    folder: bool = False
    link: str = ""
    hard_link: str = ""
    fields: dict = field(default_factory=dict)

    @property
    def posix(self) -> str:
        for token in self.attributes.split():
            if _POSIX_MODE.match(token):
                return token
        return ""

    @property
    def kind(self) -> str:
        posix = self.posix
        windows = self.attributes.split()[0] if self.attributes.split() and not _POSIX_MODE.match(
            self.attributes.split()[0]) else ""
        if self.folder or posix.startswith("d") or (not posix and "D" in windows):
            return "dir"
        if posix.startswith("l") or self.link:
            return "symlink"
        if self.hard_link:
            return "hardlink"
        if posix[:1] in ("c", "b"):
            return "device"
        if posix.startswith("p"):
            return "fifo"
        if posix.startswith("s"):
            return "other"
        return "file"

    @property
    def mode(self) -> int | None:
        posix = self.posix
        if not posix:
            return None
        mode = 0
        for position, bit in enumerate((0o400, 0o200, 0o100, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001)):
            if posix[1 + position] not in "-ST":
                mode |= bit
        if posix[3] in "sS":
            mode |= 0o4000
        if posix[6] in "sS":
            mode |= 0o2000
        if posix[9] in "tT":
            mode |= 0o1000
        return mode


def _number(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return -1


def _time(text: str) -> float | None:
    try:
        return time.mktime(time.strptime(text[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def parse_listing(text: str) -> list[Item]:
    """The entries of the output of ``7z l -slt -ba``.

    A block is a run of ``Key = Value`` lines that starts with ``Path``. The
    first value of a key in a block counts, so a name that holds a line break
    and a fake ``Size = `` line cannot overwrite the real path; whatever else
    such a name confuses is caught later, because every entry that is read is
    checked against its listed size and checksum.
    """
    items: list[Item] = []
    current: Item | None = None
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line.strip():
            current = None
            continue
        key, separator, value = line.partition(" = ")
        if not separator:
            if line.endswith(" ="):                      # an empty value has no trailing space
                key, value = line[:-2], ""
            else:
                continue
        if key == "Path" and current is None:
            current = Item(path=value)
            items.append(current)
            continue
        if current is None or key in current.fields:
            continue
        current.fields[key] = value
        if key == "Size":
            current.size = _number(value)
        elif key == "Packed Size":
            current.packed = _number(value)
        elif key == "Modified":
            current.mtime = _time(value)
        elif key == "Attributes":
            current.attributes = value
        elif key == "CRC":
            try:
                current.crc = int(value, 16) if value else None
            except ValueError:
                current.crc = None
        elif key == "Encrypted":
            current.encrypted = value.strip() == "+"
        elif key == "Method":
            current.method = value
        elif key == "Folder":
            current.folder = value.strip() == "+"
        elif key == "Symbolic Link":
            current.link = value
        elif key == "Hard Link":
            current.hard_link = value
    return items


def _password_input(password: str | None) -> bytes:
    if password and ("\n" in password or "\r" in password or "\0" in password):
        raise WrongPassword()
    return ((password or "") + "\n").encode("utf-8", "surrogateescape")


def _classify(code: int, errors: str, password: str | None) -> ArchiveError:
    lowered = errors.lower()
    if "wrong password" in lowered or "break signaled" in lowered or "enter password" in lowered:
        return WrongPassword() if password else PasswordRequired()
    if "unsupported method" in lowered or "unsupported compression" in lowered:
        return ArchiveError("7z cannot read this archive: it uses a compression method this 7z does not have "
                            "(RAR needs the 7zip-rar package).")
    if "can not open the file as" in lowered or "cannot open the file as" in lowered or "is not archive" in lowered:
        return ArchiveError("7z cannot open this file as an archive (RAR needs the 7zip-rar package).")
    if "data error" in lowered or "crc failed" in lowered or "headers error" in lowered:
        return ArchiveError("The archive is damaged: 7z reports a data error.")
    detail = next((line.strip() for line in errors.splitlines() if line.strip().upper().startswith("ERROR")), "")
    return ArchiveError(f"7z failed (exit code {code}). {detail}".strip())


class Process:
    """A running 7z whose standard output is read here; errors are collected in a temporary file."""

    def __init__(self, program: str, arguments: list[str], password: str | None) -> None:
        self.password = password
        self._errors = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen([program, *arguments], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=self._errors, env=dict(_ENVIRONMENT), cwd="/",
                                             close_fds=True, start_new_session=True)
        except OSError as error:
            self._errors.close()
            raise ArchiveError(f"7z could not be started: {error.strerror or error}") from None
        self.stdout = self._process.stdout
        try:
            self._process.stdin.write(_password_input(password))
            self._process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        except WrongPassword:
            self.kill()
            raise

    def read(self, size: int) -> bytes:
        return self.stdout.read(size)

    def read_exactly(self, size: int, chunk: int = 1 << 20):
        """Yield the next ``size`` bytes in pieces; stops early when 7z stops writing."""
        left = size
        while left > 0:
            data = self.stdout.read(min(chunk, left))
            if not data:
                return
            left -= len(data)
            yield data

    def error_text(self) -> str:
        try:
            self._errors.seek(0)
            return self._errors.read(_MAX_STDERR).decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""

    def finish(self) -> None:
        """Wait for 7z to end; raises the matching ArchiveError when it reports a failure."""
        try:
            self.stdout.close()
        except OSError:
            pass
        code = self._process.wait()
        errors = self.error_text()
        self._errors.close()
        if code != 0:
            raise _classify(code, errors, self.password)

    def kill(self) -> None:
        """Stop 7z now (cancel, or the reader has what it wanted)."""
        try:
            self._process.kill()
        except OSError:
            pass
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except OSError:
            pass
        try:
            self.stdout.close()
        except OSError:
            pass
        self._process.wait()
        try:
            self._errors.close()
        except OSError:
            pass


def list_archive(program: str, path: str, password: str | None = None, cancel=None) -> list[Item]:
    """The entries of the archive at ``path`` as 7z lists them."""
    process = Process(program, ["l", "-slt", "-ba", "-bd", "-sccUTF-8", "--", path], password)
    chunks: list[bytes] = []
    try:
        while True:
            data = process.read(1 << 16)
            if not data:
                break
            chunks.append(data)
            if cancel is not None and cancel.is_set():
                raise Cancelled()
    except BaseException:
        process.kill()
        raise
    process.finish()
    return parse_listing(b"".join(chunks).decode("utf-8", "surrogateescape"))


def entry_arguments(path: str, name: str) -> list[str]:
    """The arguments that make 7z write exactly the entry ``name`` of ``path`` to its standard output."""
    return ["x", "-so", "-bd", "-spd", "-sccUTF-8", f"-i!{name}", "--", path]


def read_entry(program: str, path: str, name: str, password: str | None = None) -> Process:
    """A Process whose output is the content of one entry. Call ``finish()`` after reading."""
    return Process(program, entry_arguments(path, name), password)


def read_all(program: str, path: str, password: str | None = None) -> Process:
    """A Process whose output is the content of every file of the archive, one after the other, in listing order."""
    return Process(program, ["x", "-so", "-bd", "-sccUTF-8", "--", path], password)


def test_archive(program: str, path: str, password: str | None = None, cancel=None) -> None:
    """Let 7z verify every entry; raises ArchiveError with what it found."""
    process = Process(program, ["t", "-bd", "-sccUTF-8", "--", path], password)
    try:
        while process.read(1 << 16):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
    except BaseException:
        process.kill()
        raise
    process.finish()
