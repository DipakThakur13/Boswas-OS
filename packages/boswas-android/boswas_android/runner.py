"""The programs boswas-android may start, and the only code that starts them.

Every process is started from a fixed argument list whose first element is
one of the absolute paths in EXECUTABLES; never a shell, never a search of
$PATH, never a string split into words. pkexec is only ever started as

    pkexec /usr/libexec/boswas/sysops <android-* operation> [arguments]

with an operation from SYSOPS_OPERATIONS (docs/architecture/platform-contracts.md).

  run(argv, timeout)          wait for a command, capture its output
  start(argv)                 start a long-running user program (an Android
                              session or app) detached, report an immediate failure
  stream(argv, on_line, ...)  run a command, hand each stdout line to on_line,
                              forward cancel requests to its stdin
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass

# name -> absolute path. Nothing else is ever executed.
EXECUTABLES = {
    "waydroid": "/usr/bin/waydroid",      # Android runtime (user-level commands only)
    "pkexec": "/usr/bin/pkexec",          # only for boswas-sysops android-* operations
    "systemctl": "/usr/bin/systemctl",    # only `systemctl is-active waydroid-container.service`
    "ip": "/usr/bin/ip",                  # only `ip -j -4 addr show dev waydroid0`
}
ALLOWED_PATHS = frozenset(EXECUTABLES.values())
SYSOPS = "/usr/libexec/boswas/sysops"
SYSOPS_OPERATIONS = frozenset({"android-runtime-install", "android-runtime-remove", "android-init",
                               "android-update", "android-repair", "android-service"})
DEFAULT_TIMEOUT = 60.0
START_SETTLE = 2.0
MAX_OUTPUT = 4 * 1024 * 1024
MAX_LINE = 64 * 1024


@dataclass(frozen=True)
class Completed:
    code: int
    stdout: str
    stderr: str


class RunnerError(Exception):
    """A program is missing, could not start or did not finish in time."""

    def __init__(self, message: str, *, missing: bool = False):
        super().__init__(message)
        self.message, self.missing = message, missing


def argv(name: str, *args: str) -> list[str]:
    path = EXECUTABLES.get(name)
    if path is None:
        raise ValueError(f"{name!r} is not an allowed program")
    command = [path, *args]
    check(command)
    return command


def sysops_argv(operation: str, *args: str) -> list[str]:
    return argv("pkexec", SYSOPS, operation, *args)


def check(command: list[str]) -> None:
    if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
        raise ValueError("a command is a non-empty list of strings")
    if command[0] not in ALLOWED_PATHS:
        raise ValueError(f"{command[0]!r} is not an allowed program")
    for arg in command[1:]:
        if not arg or "\0" in arg or "\n" in arg or len(arg) > 4096:
            raise ValueError(f"invalid argument {arg!r}")
    if command[0] == EXECUTABLES["pkexec"]:
        if len(command) < 3 or command[1] != SYSOPS or command[2] not in SYSOPS_OPERATIONS:
            raise ValueError("pkexec may only start boswas-sysops with an android-* operation")


def program_name(command: list[str]) -> str:
    return os.path.basename(command[0]) if command else "?"


class Runner:
    """Runs allowlisted commands (the tests use a recording fake with the same methods)."""

    def __init__(self, environ=None):
        self.environ = dict(os.environ if environ is None else environ)

    def available(self, name: str) -> bool:
        path = EXECUTABLES.get(name)
        return path is not None and os.access(path, os.X_OK)

    def _env(self) -> dict:
        env = dict(self.environ)
        env["LC_ALL"] = "C.UTF-8"
        env.pop("PYTHONPATH", None)
        return env

    def _require(self, command: list[str]) -> None:
        check(command)
        if not os.access(command[0], os.X_OK):
            raise RunnerError(f"{program_name(command)} is not installed ({command[0]}).", missing=True)

    def run(self, command: list[str], timeout: float = DEFAULT_TIMEOUT) -> Completed:
        self._require(command)
        if not timeout or timeout <= 0:
            raise ValueError("every command needs a timeout")
        name = program_name(command)
        try:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                  env=self._env(), check=False, close_fds=True)
        except subprocess.TimeoutExpired:
            raise RunnerError(f"{name} did not finish within {int(timeout)} seconds.") from None
        except OSError as exc:
            raise RunnerError(f"{name} could not be started ({exc.strerror or exc}).") from None
        return Completed(proc.returncode, proc.stdout[:MAX_OUTPUT].decode("utf-8", errors="replace"),
                         proc.stderr[:MAX_OUTPUT].decode("utf-8", errors="replace"))

    def start(self, command: list[str], log_path: str | None = None, settle: float = START_SETTLE) -> None:
        """Start a program in its own session (it outlives this command); raise if it fails at once."""
        self._require(command)
        name = program_name(command)
        out = subprocess.DEVNULL
        handle = None
        if log_path:
            try:
                handle = open(log_path, "ab")
                out = handle
            except OSError:
                handle = None
        try:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=self._env(),
                                    close_fds=True, start_new_session=True)
        except OSError as exc:
            raise RunnerError(f"{name} could not be started ({exc.strerror or exc}).") from None
        finally:
            if handle is not None:
                handle.close()
        try:
            code = proc.wait(timeout=settle)
        except subprocess.TimeoutExpired:
            threading.Thread(target=proc.wait, name=f"reap-{name}", daemon=True).start()
            return
        if code != 0:
            raise RunnerError(f"{name} stopped right after it was started (exit code {code}).")

    def stream(self, command: list[str], on_line, cancel_event: threading.Event | None = None,
               timeout: float | None = None) -> int:
        """Run command; call on_line(text) for each stdout line; write "cancel" to its stdin when
        cancel_event is set. Returns the exit status. stderr is collected and handed to on_line
        only if stdout produced nothing (pkexec's own messages)."""
        self._require(command)
        name = program_name(command)
        try:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=self._env(), close_fds=True)
        except OSError as exc:
            raise RunnerError(f"{name} could not be started ({exc.strerror or exc}).") from None
        stderr_chunks: list[bytes] = []
        done = threading.Event()

        def read_stderr():
            data = proc.stderr.read(MAX_OUTPUT)
            if data:
                stderr_chunks.append(data)

        def watch_cancel():
            while not done.is_set():
                if cancel_event is not None and cancel_event.wait(0.2):
                    try:
                        proc.stdin.write(b"cancel\n")
                        proc.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    cancel_event.clear()
                elif cancel_event is None:
                    done.wait(0.5)

        err_thread = threading.Thread(target=read_stderr, daemon=True)
        err_thread.start()
        cancel_thread = threading.Thread(target=watch_cancel, daemon=True)
        cancel_thread.start()
        produced = False
        try:
            for raw in iter(lambda: proc.stdout.readline(MAX_LINE), b""):
                produced = True
                on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RunnerError(f"{name} did not finish within {int(timeout or 0)} seconds.") from None
        finally:
            done.set()
            err_thread.join(timeout=5)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        if not produced and stderr_chunks:
            for line in b"".join(stderr_chunks).decode("utf-8", errors="replace").splitlines()[-5:]:
                if line.strip():
                    on_line(line.rstrip())
        return code
