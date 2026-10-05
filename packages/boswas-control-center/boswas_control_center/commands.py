"""The programs Control Center may start, and the only code that starts them.

Every process is started from a fixed argument list whose first element is
one of the absolute paths in EXECUTABLES. There is no shell, no search of
$PATH, no string that is split into words, and every call has a timeout:

  run(argv, timeout)   wait for a short command and capture its output
                       (boswas --json status, boswas-preset, apt-config, ...)
  start(argv)          start a desktop program (a KDE settings module, an
                       application, Dolphin) in its own session, wait briefly
                       to report an immediate failure, then let it run on its
                       own; Control Center does not stay its parent

The builders in backend.py validate every variable argument (a module name
from the catalog, a preset or desktop-entry ID matching a strict pattern)
before it reaches argv().
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass

from .errors import CommandFailed, CommandUnavailable

# name -> absolute path. Nothing else is ever executed.
EXECUTABLES = {
    "boswas": "/usr/bin/boswas",                                  # boswas-cli: posture checks
    "boswas-preset": "/usr/bin/boswas-preset",                    # desktop presets
    "boswas-compat-manager": "/usr/bin/boswas-compat-manager",    # Windows applications
    "kcmshell6": "/usr/bin/kcmshell6",                            # libkf6kcmutils-bin: one settings module
    "kstart": "/usr/bin/kstart",                                  # kde-cli-tools: start an application
    "kioclient": "/usr/bin/kioclient",                            # kde-cli-tools: open a desktop entry
    "dolphin": "/usr/bin/dolphin",
    "partitionmanager": "/usr/bin/partitionmanager",
    "kinfocenter": "/usr/bin/kinfocenter",
    "plasmashell": "/usr/bin/plasmashell",                        # only `plasmashell --version`
    "apt-config": "/usr/bin/apt-config",                          # only `apt-config shell` (read-only)
    "systemctl": "/usr/bin/systemctl",                            # only `systemctl reboot`
}
ALLOWED_PATHS = frozenset(EXECUTABLES.values())

DEFAULT_TIMEOUT = 30.0
START_SETTLE = 1.5          # seconds a started program may take to fail visibly
MAX_OUTPUT = 4 * 1024 * 1024


@dataclass(frozen=True)
class Completed:
    code: int
    stdout: str
    stderr: str


def argv(name: str, *args: str) -> list[str]:
    """[absolute path of name, *args] after checking each argument is a plain single string."""
    path = EXECUTABLES.get(name)
    if path is None:
        raise ValueError(f"{name!r} is not an allowed program")
    for arg in args:
        if not isinstance(arg, str) or not arg or "\0" in arg or "\n" in arg or len(arg) > 4096:
            raise ValueError(f"invalid argument for {name}: {arg!r}")
    return [path, *args]


def program_name(command: list[str]) -> str:
    return os.path.basename(command[0]) if command else "?"


def _check(command: list[str]) -> None:
    if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
        raise ValueError("a command is a non-empty list of strings")
    if command[0] not in ALLOWED_PATHS:
        raise ValueError(f"{command[0]!r} is not an allowed program")


def _tail(text: str, limit: int = 400) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    tail = " ".join(lines[-3:])
    return tail if len(tail) <= limit else "…" + tail[-limit:]


class CommandRunner:
    """Runs allowlisted commands (replaced by a recording fake in the tests)."""

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ

    def available(self, path: str) -> bool:
        return path in ALLOWED_PATHS and os.access(path, os.X_OK)

    def run(self, command: list[str], timeout: float = DEFAULT_TIMEOUT) -> Completed:
        """Run a short command and capture its output (C.UTF-8 locale, no stdin)."""
        _check(command)
        if not timeout or timeout <= 0:
            raise ValueError("every command needs a timeout")
        name = program_name(command)
        if not self.available(command[0]):
            raise CommandUnavailable(name)
        env = dict(self.environ)
        env["LC_ALL"] = "C.UTF-8"
        try:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=env,
                                  check=False, close_fds=True)
        except subprocess.TimeoutExpired:
            raise CommandFailed(name, f"{name} did not finish within {int(timeout)} seconds.") from None
        except OSError as exc:
            raise CommandFailed(name, f"{name} could not be started ({exc.strerror or exc}).") from None
        stdout = proc.stdout[:MAX_OUTPUT].decode("utf-8", errors="replace")
        stderr = proc.stderr[:MAX_OUTPUT].decode("utf-8", errors="replace")
        return Completed(proc.returncode, stdout, stderr)

    def start(self, command: list[str], settle: float = START_SETTLE) -> None:
        """Start a desktop program detached from Control Center; raise if it fails at once."""
        _check(command)
        name = program_name(command)
        if not self.available(command[0]):
            raise CommandUnavailable(name)
        try:
            # Own session: closing Control Center does not close the program.
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, env=dict(self.environ), close_fds=True,
                                    start_new_session=True)
        except OSError as exc:
            raise CommandFailed(name, f"{name} could not be started ({exc.strerror or exc}).") from None
        try:
            code = proc.wait(timeout=settle)
        except subprocess.TimeoutExpired:
            # Still running: collect its exit status in the background so it never lingers as a zombie.
            threading.Thread(target=proc.wait, name=f"reap-{name}", daemon=True).start()
            return
        if code != 0:
            raise CommandFailed(name, f"{name} stopped right after it was started (exit code {code}).", code)


def failure(command: list[str], result: Completed, what: str) -> CommandFailed:
    """A CommandFailed for a command that ran but returned an unexpected exit code."""
    name = program_name(command)
    detail = _tail(result.stderr) or _tail(result.stdout)
    message = f"{what} failed ({name} exit code {result.code})" + (f": {detail}" if detail else ".")
    return CommandFailed(name, message, result.code)
