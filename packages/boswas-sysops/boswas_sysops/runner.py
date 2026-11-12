"""The programs boswas-sysops may start, and the only code that starts them.

Every process is started from an argument list whose first element is one
of the absolute paths in EXECUTABLES, with the helper's fixed environment,
no shell, stdin from /dev/null, and a timeout:

  run(command, timeout)      short commands; output captured
  stream(command, ...)       long commands (APT, Waydroid): output lines and
                             APT Status-Fd lines are handed to callbacks as
                             they arrive; ``poll`` is asked a few times a
                             second whether to stop (cancellation)

For APT's Status-Fd the caller puts STATUS_FD_TOKEN into the option value
("APT::Status-Fd=@STATUS_FD@"); the runner substitutes the number of a pipe
it passes to the child (pass_fds) and reads that pipe. A stopped or timed-out
command's process group gets SIGINT, then SIGTERM, then SIGKILL.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field

EXECUTABLES = {
    "apt-get": "/usr/bin/apt-get",
    "apt-cache": "/usr/bin/apt-cache",
    "dpkg-query": "/usr/bin/dpkg-query",
    "dpkg-deb": "/usr/bin/dpkg-deb",
    "journalctl": "/usr/bin/journalctl",
    "dmesg": "/usr/bin/dmesg",
    "flatpak": "/usr/bin/flatpak",
    "waydroid": "/usr/bin/waydroid",
    "systemctl": "/usr/bin/systemctl",
    "modprobe": "/usr/sbin/modprobe",
}
ALLOWED_PATHS = frozenset(EXECUTABLES.values())
STATUS_FD_TOKEN = "@STATUS_FD@"

# The only environment children see (pkexec has already cleared the caller's).
SAFE_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
    "DEBIAN_FRONTEND": "noninteractive",
    "APT_LISTCHANGES_FRONTEND": "none",
    "HOME": "/root",
}
MAX_OUTPUT = 8 * 1024 * 1024
TAIL_LINES = 40
SIGNAL_STEPS = ((signal.SIGINT, 10.0), (signal.SIGTERM, 10.0), (signal.SIGKILL, 10.0))


@dataclass(frozen=True)
class Completed:
    code: int
    stdout: str
    stderr: str


@dataclass
class Streamed:
    code: int | None
    cancelled: bool = False
    timed_out: bool = False
    tail: list[str] = field(default_factory=list)

    @property
    def detail(self) -> str:
        return "\n".join(self.tail[-15:])


def argv(name: str, *args: str) -> list[str]:
    """[absolute path of name, *args] after checking each argument is a plain single string."""
    path = EXECUTABLES.get(name)
    if path is None:
        raise ValueError(f"{name!r} is not an allowed program")
    for arg in args:
        if not isinstance(arg, str) or not arg or "\0" in arg or "\n" in arg or len(arg) > 4096:
            raise ValueError(f"invalid argument for {name}: {arg!r}")
    return [path, *args]


def _check(command: list[str]) -> None:
    if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
        raise ValueError("a command is a non-empty list of strings")
    if command[0] not in ALLOWED_PATHS:
        raise ValueError(f"{command[0]!r} is not an allowed program")


def _split_lines(buffer: bytes) -> tuple[list[str], bytes]:
    """Complete lines (\\n or \\r terminated: progress bars use \\r) and the unfinished rest."""
    lines = []
    start = 0
    for index, byte in enumerate(buffer):
        if byte in (10, 13):
            if index > start:
                lines.append(buffer[start:index].decode("utf-8", errors="replace"))
            start = index + 1
    return lines, buffer[start:]


class Runner:
    def available(self, path: str) -> bool:
        return path in ALLOWED_PATHS and os.access(path, os.X_OK)

    def run(self, command: list[str], timeout: float = 60.0) -> Completed:
        _check(command)
        if not timeout or timeout <= 0:
            raise ValueError("every command needs a timeout")
        try:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                  env=dict(SAFE_ENV), check=False, close_fds=True)
        except subprocess.TimeoutExpired:
            return Completed(124, "", f"{os.path.basename(command[0])} did not finish within {int(timeout)} seconds")
        except OSError as exc:
            return Completed(127, "", f"{os.path.basename(command[0])} could not be started ({exc.strerror or exc})")
        return Completed(proc.returncode, proc.stdout[:MAX_OUTPUT].decode("utf-8", errors="replace"),
                         proc.stderr[:MAX_OUTPUT].decode("utf-8", errors="replace"))

    def stream(self, command: list[str], *, timeout: float, on_line=None, on_status=None, poll=None) -> Streamed:
        _check(command)
        if not timeout or timeout <= 0:
            raise ValueError("every command needs a timeout")
        status_r = status_w = None
        pass_fds: tuple[int, ...] = ()
        if on_status is not None:
            status_r, status_w = os.pipe()
            command = [arg.replace(STATUS_FD_TOKEN, str(status_w)) for arg in command]
            pass_fds = (status_w,)
        elif any(STATUS_FD_TOKEN in arg for arg in command):
            raise ValueError("a Status-Fd placeholder needs an on_status callback")
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        try:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=dict(SAFE_ENV), close_fds=True,
                                    pass_fds=pass_fds, start_new_session=True)
        except OSError as exc:
            for fd in (status_r, status_w):
                if fd is not None:
                    os.close(fd)
            return Streamed(127, tail=[f"{os.path.basename(command[0])} could not be started ({exc.strerror or exc})"])
        if status_w is not None:
            os.close(status_w)
        selector = selectors.DefaultSelector()
        handlers = {proc.stdout.fileno(): on_line}
        selector.register(proc.stdout.fileno(), selectors.EVENT_READ)
        if status_r is not None:
            handlers[status_r] = on_status
            selector.register(status_r, selectors.EVENT_READ)
        buffers = {fd: b"" for fd in handlers}
        deadline = time.monotonic() + timeout
        result = Streamed(None)
        stopping = None          # (step index, time of the last signal)

        def deliver(fd: int, lines: list[str]) -> None:
            for line in lines:
                if fd == proc.stdout.fileno():
                    tail.append(line)
                handler = handlers[fd]
                if handler is not None:
                    handler(line)

        try:
            while selector.get_map():
                for key, _ in selector.select(timeout=0.2):
                    fd = key.fd
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        selector.unregister(fd)
                        if buffers[fd]:
                            deliver(fd, [buffers[fd].decode("utf-8", errors="replace")])
                            buffers[fd] = b""
                        continue
                    lines, buffers[fd] = _split_lines(buffers[fd] + data)
                    deliver(fd, lines)
                now = time.monotonic()
                if stopping is None:
                    if poll is not None and poll():
                        result.cancelled = True
                        stopping = self._signal(proc, 0)
                    elif now > deadline:
                        result.timed_out = True
                        stopping = self._signal(proc, 0)
                elif now - stopping[1] > SIGNAL_STEPS[stopping[0]][1] and stopping[0] + 1 < len(SIGNAL_STEPS):
                    stopping = self._signal(proc, stopping[0] + 1)
            result.code = proc.wait(timeout=max(1.0, SIGNAL_STEPS[-1][1]))
        except subprocess.TimeoutExpired:
            self._signal(proc, len(SIGNAL_STEPS) - 1)
            result.code = proc.wait()
        finally:
            selector.close()
            proc.stdout.close()
            if status_r is not None:
                os.close(status_r)
        result.tail = list(tail)
        return result

    @staticmethod
    def _signal(proc: subprocess.Popen, step: int) -> tuple[int, float]:
        try:
            os.killpg(proc.pid, SIGNAL_STEPS[step][0])
        except (ProcessLookupError, PermissionError):
            pass
        return step, time.monotonic()
