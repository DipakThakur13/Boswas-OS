"""Snips through the desktop (standard library only): the control socket, the Screenshots folder, the wait.

Programs cannot read the screen. The compositor's public control socket
(``$XDG_RUNTIME_DIR/boswas-$WAYLAND_DISPLAY.sock``, or the path in
``BOSWAS_COMPOSITOR_SOCKET``) takes one JSON object per line and answers
one JSON line per request. ``{"cmd": "capture", "mode": "region"}`` (or
``"screen"``) makes the desktop shell show a frozen picture of the screen on
which the user drags an area or confirms the whole screen. The shell saves
the result as ``Screenshot YYYY-MM-DD HHMMSS.png`` in the Screenshots folder
under the user's Pictures folder and copies it to the clipboard.

Nobody tells the program that asked whether the user finished or
cancelled: ``ScreenshotWaiter`` notes what the folder holds before the
request and then looks for a new, complete PNG with the shell's naming.

``screenshots_dir`` and ``file_name`` port the rules of the shell
(boswas-shell ``capture.rs``) exactly; keep them in step.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

SOCKET_TIMEOUT = 3.0                     # seconds for the compositor to answer
MAX_REPLY = 64 * 1024                    # the longest answer line read
MAX_USER_DIRS = 64 * 1024                # user-dirs.dirs is a few lines; anything longer is not read
MAX_ENTRIES = 20_000                     # the most directory entries looked at per scan
MAX_SCREENSHOT_BYTES = 512 * 1024 * 1024
WAIT_SECONDS = 120.0                     # how long a snip is waited for
MTIME_SLACK = 2.0                        # a new file may carry a time stamp this much before the request
MODES = ("region", "screen")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
SCREENSHOT_NAME = re.compile(r"Screenshot \d{4}-\d{2}-\d{2} \d{6}(?: \((?:[2-9]|[1-9]\d{1,2})\))?\.png")


class CaptureError(Exception):
    """A snip could not be started. ``reason``: not-running, refused, timeout or failed; the text is for people."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _clean(text: object, limit: int) -> str:
    """Text from the compositor, fit to show: printable, one line, bounded."""
    if not isinstance(text, str):
        return ""
    text = "".join(character if character.isprintable() else " " for character in text[:limit])
    return " ".join(text.split())


# --- the control socket -----------------------------------------------------------------------------------

def control_socket(environ: Mapping[str, str]) -> str | None:
    """The public control socket of this session's compositor, or None when the session has none."""
    explicit = environ.get("BOSWAS_COMPOSITOR_SOCKET") or ""
    if explicit.startswith("/") and "\0" not in explicit:
        return explicit
    runtime, display = environ.get("XDG_RUNTIME_DIR") or "", environ.get("WAYLAND_DISPLAY") or ""
    if not runtime.startswith("/") or not display or "/" in display or "\0" in runtime + display:
        return None
    return os.path.join(runtime, f"boswas-{display}.sock")


class CaptureClient:
    """Asks the compositor to start a capture; one request per connection. Everything here blocks."""

    _ids = itertools.count(1)
    _lock = threading.Lock()

    def __init__(self, environ: Mapping[str, str] | None = None, *, path: str | None = None,
                 timeout: float = SOCKET_TIMEOUT) -> None:
        self.environ = os.environ if environ is None else environ
        self.path = path
        self.timeout = timeout

    def socket_path(self) -> str | None:
        return self.path or control_socket(self.environ)

    def _checked_path(self) -> str:
        path = self.socket_path()
        if path is None:
            raise CaptureError("not-running", "No Boswas desktop is running in this session.")
        try:
            info = os.stat(path)
        except OSError:
            raise CaptureError("not-running", "No Boswas desktop is running in this session.") from None
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise CaptureError("not-running", "The desktop's control socket is not yours to use.")
        return path

    def available(self) -> bool:
        """Whether the socket is there and the user's (it is not asked anything)."""
        try:
            self._checked_path()
        except CaptureError:
            return False
        return True

    def request(self, mode: str) -> None:
        """Start a capture of a ``region`` or the whole ``screen``. Returns once the compositor took it."""
        if mode not in MODES:
            raise ValueError(f"unknown capture mode {mode!r}")
        path = self._checked_path()
        with self._lock:
            number = next(self._ids)
        line = json.dumps({"id": number, "cmd": "capture", "mode": mode}, separators=(",", ":")).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(path)
                connection.sendall(line)
                answer = self._read_answer(connection, number)
        except (ConnectionRefusedError, FileNotFoundError):
            raise CaptureError("not-running", "No Boswas desktop is running in this session.") from None
        except TimeoutError:
            raise CaptureError("timeout", "The desktop did not answer in time.") from None
        except OSError as exc:
            raise CaptureError("failed", f"The desktop could not be reached ({exc.strerror or exc}).") from None
        if answer.get("ok") is not True:
            reason = _clean(answer.get("error"), 200)
            raise CaptureError("refused", "The desktop refused to start a snip" + (f" ({reason})." if reason else "."))

    @staticmethod
    def _read_answer(connection: socket.socket, number: int) -> dict:
        """The answer line with our ``id`` (a line without an id, such as an event, is skipped)."""
        pending = b""
        received = 0
        while True:
            while b"\n" not in pending:
                chunk = connection.recv(65536)
                if not chunk:
                    raise CaptureError("failed", "The desktop closed the connection without an answer.")
                received += len(chunk)
                if received > MAX_REPLY:
                    raise CaptureError("failed", "The desktop sent an answer that is too long.")
                pending += chunk
            line, pending = pending.split(b"\n", 1)
            try:
                answer = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise CaptureError("failed", "The desktop sent an answer that could not be read.") from None
            if not isinstance(answer, dict):
                raise CaptureError("failed", "The desktop sent an answer that could not be read.")
            if "id" not in answer:
                continue
            reply_id = answer.get("id")
            if reply_id is None or (isinstance(reply_id, int) and not isinstance(reply_id, bool)
                                    and reply_id == number):
                return answer          # a reply with "id": null is the compositor's answer to a line it could not read
            continue


# --- the Screenshots folder --------------------------------------------------------------------------------

def screenshots_dir(home: str | os.PathLike, user_dirs: str | None) -> Path:
    """The folder screenshots are saved in: ``Screenshots`` under the user's Pictures folder.

    The Pictures folder is ``XDG_PICTURES_DIR`` of ``user-dirs.dirs``
    (``$HOME/`` at its start stands for ``home``), else ``home/Pictures``.
    A value that is relative, is ``home`` itself or contains ``..`` is
    ignored, as is every line that is not ``XDG_PICTURES_DIR=``. The same
    rules as the shell's ``capture::screenshots_dir``.
    """
    home = Path(home)
    pictures = None
    for line in (user_dirs or "").splitlines():
        line = line.strip()
        if not line.startswith("XDG_PICTURES_DIR="):
            continue
        value = line[len("XDG_PICTURES_DIR="):].strip().strip('"')
        path = home / value[len("$HOME/"):] if value.startswith("$HOME/") else Path(value)
        if path.is_absolute() and path != home and ".." not in value:
            pictures = path
            break
    return (pictures if pictures is not None else home / "Pictures") / "Screenshots"


def file_name(time_text: str, exists: Callable[[str], bool]) -> str:
    """The name the shell gives a screenshot taken at ``time_text`` ("YYYY-MM-DD HHMMSS") in a folder where
    ``exists(name)`` tells which names are taken (the shell's ``capture::file_name``)."""
    base = f"Screenshot {time_text}"
    name = f"{base}.png"
    number = 2
    while exists(name) and number < 1000:
        name = f"{base} ({number}).png"
        number += 1
    return name


def is_screenshot_name(name: str) -> bool:
    """Whether ``name`` is one the shell gives a screenshot."""
    return SCREENSHOT_NAME.fullmatch(name) is not None


def user_dirs_file(environ: Mapping[str, str] | None = None) -> Path:
    """Where ``user-dirs.dirs`` is: in ``$XDG_CONFIG_HOME`` when that is absolute, else in ``~/.config``."""
    environ = os.environ if environ is None else environ
    config = environ.get("XDG_CONFIG_HOME") or ""
    base = Path(config) if config.startswith("/") else home_dir(environ) / ".config"
    return base / "user-dirs.dirs"


def home_dir(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    home = environ.get("HOME") or ""
    return Path(home) if home else Path.home()


def read_user_dirs(path: str | os.PathLike) -> str | None:
    """The text of ``user-dirs.dirs``, or None when it is missing, unreadable, too long or not text."""
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_USER_DIRS + 1)
    except OSError:
        return None
    if len(data) > MAX_USER_DIRS:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def find_screenshots_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The Screenshots folder of this user, as the shell finds it. Reads a file: not for the GUI thread."""
    environ = os.environ if environ is None else environ
    return screenshots_dir(home_dir(environ), read_user_dirs(user_dirs_file(environ)))


# --- waiting for the snip ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Mark:
    """What is known of a file in the folder: enough to tell that it is new or changed."""

    size: int
    mtime: float


def scan(directory: str | os.PathLike) -> dict[str, Mark]:
    """The screenshots (regular files with the shell's naming) in ``directory`` now; {} when it is missing."""
    found: dict[str, Mark] = {}
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_ENTRIES:
                    break
                if not is_screenshot_name(entry.name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    found[entry.name] = Mark(info.st_size, info.st_mtime)
    except OSError:
        pass
    return found


def is_complete_png(path: str | os.PathLike) -> bool:
    """Whether ``path`` is a regular file that starts like a PNG and ends with its last chunk (fully written)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not len(PNG_SIGNATURE) + len(PNG_END) <= info.st_size \
                <= MAX_SCREENSHOT_BYTES:
            return False
        head = os.pread(fd, len(PNG_SIGNATURE), 0)
        tail = os.pread(fd, len(PNG_END), info.st_size - len(PNG_END))
        return head == PNG_SIGNATURE and tail == PNG_END
    except OSError:
        return False
    finally:
        os.close(fd)


class ScreenshotWaiter:
    """Finds the snip the shell saves after a request.

    ``start()`` notes the screenshots already in the folder; call it before
    the request is sent. ``poll()`` then returns the path of a screenshot
    that was not there before, has a time stamp from after the request and
    is completely written, or None. Everything here reads the disk.
    """

    def __init__(self, directory: str | os.PathLike, *, timeout: float = WAIT_SECONDS,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self.timeout = timeout
        self._clock = clock
        self._wall = wall
        self.before: dict[str, Mark] = {}
        self.started = 0.0
        self.requested_at = 0.0

    def start(self) -> None:
        self.before = scan(self.directory)
        self.started = self._clock()
        self.requested_at = self._wall()

    def expired(self) -> bool:
        return self._clock() - self.started >= self.timeout

    def candidates(self) -> list[Path]:
        """New screenshots, newest first (complete or not)."""
        found = []
        for name, mark in scan(self.directory).items():
            if name in self.before and self.before[name] == mark:
                continue
            if mark.mtime < self.requested_at - MTIME_SLACK:
                continue
            found.append((mark.mtime, name))
        found.sort(reverse=True)
        return [self.directory / name for _mtime, name in found]

    def poll(self) -> Path | None:
        for path in self.candidates():
            if is_complete_png(path):
                return path
        return None
