"""The programs Control Center may start, and the only code that starts them.

Every process is started from a fixed argument list whose first element is
one of the absolute paths in EXECUTABLES. There is no shell, no search of
$PATH, no string that is split into words, and every call has a timeout:

  run(argv, timeout)          wait for a short command and capture its output
                              (boswas --json status, boswas-preset, apt-cache, ...)
  start(argv)                 start a desktop program (a KDE settings module, an
                              application, Dolphin) in its own session, wait briefly
                              to report an immediate failure, then let it run on
                              its own; Control Center does not stay its parent
  stream(argv, on_line, ...)  run a long operation (the privileged helper
                              boswas-sysops through pkexec, boswas-android) and
                              hand every output line to on_line as it arrives;
                              the caller may ask the program to cancel by
                              writing "cancel" to its standard input

The builders below validate every variable argument before it reaches argv:
pkexec is only ever started as ``pkexec /usr/libexec/boswas/sysops
<operation> ...`` with an operation of SYSOPS_OPERATIONS and arguments that
pass that operation's validator; apt-get only ever simulates (``-s``);
boswas-android only with a command of ANDROID_COMMANDS.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass

from .errors import CommandFailed, CommandUnavailable

# name -> absolute path. Nothing else is ever executed.
EXECUTABLES = {
    "boswas": "/usr/bin/boswas",                                  # boswas-cli: posture checks
    "boswas-preset": "/usr/bin/boswas-preset",                    # desktop presets
    "boswas-compat-manager": "/usr/bin/boswas-compat-manager",    # Windows applications
    "boswas-android": "/usr/bin/boswas-android",                  # Android runtime manager (boswas-android)
    "kcmshell6": "/usr/bin/kcmshell6",                            # libkf6kcmutils-bin: one settings module
    "kstart": "/usr/bin/kstart",                                  # kde-cli-tools: start an application
    "kioclient": "/usr/bin/kioclient",                            # kde-cli-tools: open a desktop entry
    "dolphin": "/usr/bin/dolphin",
    "partitionmanager": "/usr/bin/partitionmanager",
    "kinfocenter": "/usr/bin/kinfocenter",
    "plasmashell": "/usr/bin/plasmashell",                        # only `plasmashell --version`
    "apt-config": "/usr/bin/apt-config",                          # only `apt-config shell` (read-only)
    "apt-get": "/usr/bin/apt-get",                                # only `apt-get -s ...` (simulation, read-only)
    "apt-cache": "/usr/bin/apt-cache",                            # only search, show, policy (read-only)
    "flatpak": "/usr/bin/flatpak",                                # user installation and read-only queries
    "nmcli": "/usr/bin/nmcli",                                    # NetworkManager (its own polkit rules)
    "lsblk": "/usr/bin/lsblk",                                    # disks (read-only, no serial numbers)
    "systemctl": "/usr/bin/systemctl",                            # only `systemctl reboot`
    "pkexec": "/usr/bin/pkexec",                                  # only `pkexec SYSOPS <operation> ...`
}
ALLOWED_PATHS = frozenset(EXECUTABLES.values())

# The privileged helper (package boswas-sysops) and the operations Control Center uses
# (docs/architecture/platform-contracts.md, section 1). Android operations go through
# boswas-android, which calls the helper itself.
SYSOPS = "/usr/libexec/boswas/sysops"
ADMIN_TOOL = "pkexec"
NO_ADMIN = ("Administrator operations are not available on this system (polkit's authorization tool, pkexec, is "
            "not installed).")
SYSOPS_OPERATIONS = frozenset({
    "apt-refresh", "apt-upgrade", "apt-install", "apt-remove", "apt-install-local", "apt-clean",
    "journal-vacuum", "firmware-report", "flatpak-remote-enable", "flatpak-remote-disable",
})
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,99}(:(amd64|i386|all))?$")
# flatpak: read-only queries of both installations; changes only to the user's own installation.
FLATPAK_SUBCOMMANDS = frozenset({"remotes", "list", "search", "remote-add", "install", "uninstall", "update"})
FLATPAK_USER_ONLY = frozenset({"remote-add", "install", "uninstall", "update"})
REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_PACKAGES = 50

# boswas-android commands (contract section 2): words after `boswas-android --json`.
ANDROID_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
ANDROID_COMMANDS = {
    ("status",): 0, ("check",): 0, ("google-play",): 0, ("app", "list"): 0,
    ("session", "start"): 0, ("session", "stop"): 0, ("fdroid", "install"): 0,
    ("runtime", "install"): 0, ("runtime", "init"): 0, ("runtime", "update"): 0, ("runtime", "repair"): 0,
    ("runtime", "remove"): 0, ("service", "start"): 0, ("service", "stop"): 0, ("service", "restart"): 0,
    ("apk", "inspect"): 1, ("apk", "install"): 1, ("app", "launch"): 1, ("app", "remove"): 1,
}

DEFAULT_TIMEOUT = 30.0
START_SETTLE = 1.5          # seconds a started program may take to fail visibly
MAX_OUTPUT = 4 * 1024 * 1024
MAX_LINE = 64 * 1024        # a streamed line longer than this is cut
STREAM_TIMEOUT = 4 * 3600.0


@dataclass(frozen=True)
class Completed:
    code: int
    stdout: str
    stderr: str


def _plain(arg: object) -> bool:
    return isinstance(arg, str) and bool(arg) and "\0" not in arg and "\n" not in arg and len(arg) <= 4096


def argv(name: str, *args: str) -> list[str]:
    """[absolute path of name, *args] after checking each argument is a plain single string."""
    path = EXECUTABLES.get(name)
    if path is None:
        raise ValueError(f"{name!r} is not an allowed program")
    if name == "pkexec":
        raise ValueError("pkexec is started only through sysops_argv()")
    if name == "apt-get" and (not args or args[0] != "-s"):
        raise ValueError("apt-get is only ever run as a simulation (-s)")
    if name == "apt-cache" and (not args or args[0] not in ("search", "show", "policy")):
        raise ValueError("apt-cache is only used for search, show and policy")
    if name == "nmcli" and (not args or args[0] not in ("-t", "radio", "device")
                            or (args[0] == "radio" and tuple(args[1:]) not in (("wifi", "on"), ("wifi", "off")))
                            or (args[0] == "device" and (len(args) != 3 or args[1] not in ("connect", "disconnect")))):
        raise ValueError("nmcli is used for queries (-t), the Wi-Fi radio and connecting devices only")
    if name == "flatpak":
        if not args or args[0] not in FLATPAK_SUBCOMMANDS:
            raise ValueError(f"flatpak {args[:1]} is not allowed")
        if args[0] in FLATPAK_USER_ONLY and "--user" not in args:
            raise ValueError(f"flatpak {args[0]} is only used for the user's own installation (--user)")
    if name == "boswas-android":
        raise ValueError("boswas-android is started only through android_argv()")
    for arg in args:
        if not _plain(arg):
            raise ValueError(f"invalid argument for {name}: {arg!r}")
    return [path, *args]


def valid_package(name: object) -> bool:
    return isinstance(name, str) and bool(PACKAGE_RE.fullmatch(name))


def _validate_sysops(operation: str, args: tuple[str, ...]) -> None:
    if operation not in SYSOPS_OPERATIONS:
        raise ValueError(f"{operation!r} is not an allowed boswas-sysops operation")
    if operation in ("apt-install", "apt-remove"):
        if not 1 <= len(args) <= MAX_PACKAGES or not all(valid_package(a) for a in args):
            raise ValueError(f"{operation} needs 1-{MAX_PACKAGES} valid package names")
        if len(set(args)) != len(args):
            raise ValueError(f"{operation}: a package is named twice")
    elif operation == "apt-install-local":
        if len(args) != 1 or not _plain(args[0]) or not os.path.isabs(args[0]) or ".." in args[0].split("/"):
            raise ValueError("apt-install-local needs one absolute file path")
    elif operation in ("flatpak-remote-enable", "flatpak-remote-disable"):
        if len(args) != 1 or not REMOTE_RE.fullmatch(args[0]):
            raise ValueError(f"{operation} needs one remote name")
    elif args:
        raise ValueError(f"{operation} takes no arguments")


def sysops_argv(operation: str, *args: str) -> list[str]:
    """pkexec /usr/libexec/boswas/sysops <operation> [args]: the only way pkexec is ever started."""
    _validate_sysops(operation, tuple(args))
    return [EXECUTABLES["pkexec"], SYSOPS, operation, *args]


def android_argv(*words: str) -> list[str]:
    """boswas-android --json <command> [argument]: commands of ANDROID_COMMANDS only.

    ``runtime remove`` may carry ``--purge-data``; apk commands take one absolute file
    path, app launch/remove one Android package name.
    """
    words = tuple(words)
    extra: tuple[str, ...] = ()
    if words[:2] == ("runtime", "remove") and words[2:] == ("--purge-data",):
        words, extra = words[:2], ("--purge-data",)
    command = words[:2] if words[:2] in ANDROID_COMMANDS else words[:1]
    if command not in ANDROID_COMMANDS:
        raise ValueError(f"{' '.join(words)!r} is not an allowed boswas-android command")
    rest = words[len(command):]
    if len(rest) != ANDROID_COMMANDS[command]:
        raise ValueError(f"boswas-android {' '.join(command)} takes {ANDROID_COMMANDS[command]} argument(s)")
    if command[0] == "apk":
        path = rest[0]
        if not _plain(path) or not os.path.isabs(path) or ".." in path.split("/"):
            raise ValueError("an APK is named by its absolute path")
    elif command[0] == "app" and rest:
        if not ANDROID_PACKAGE_RE.fullmatch(rest[0]) or len(rest[0]) > 255:
            raise ValueError(f"invalid Android package name {rest[0]!r}")
    return [EXECUTABLES["boswas-android"], "--json", *command, *rest, *extra]


def program_name(command: list[str]) -> str:
    return os.path.basename(command[0]) if command else "?"


def _check(command: list[str]) -> None:
    if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
        raise ValueError("a command is a non-empty list of strings")
    if command[0] not in ALLOWED_PATHS:
        raise ValueError(f"{command[0]!r} is not an allowed program")
    if command[0] == EXECUTABLES["pkexec"]:
        if len(command) < 3 or command[1] != SYSOPS:
            raise ValueError("pkexec only starts the Boswas system helper")
        _validate_sysops(command[2], tuple(command[3:]))
    if command[0] == EXECUTABLES["apt-get"] and (len(command) < 2 or command[1] != "-s"):
        raise ValueError("apt-get is only ever run as a simulation (-s)")


def _tail(text: str, limit: int = 400) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    tail = " ".join(lines[-3:])
    return tail if len(tail) <= limit else "…" + tail[-limit:]


class StreamHandle:
    """Control of one streamed operation, shared between the worker thread and the window.

    cancel() asks the program to stop by writing "cancel" to its standard input
    (the program decides whether it can stop safely); kill() ends it at once and is
    used only while the program says it can be cancelled.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stdin = None
        self._proc = None
        self.cancel_requested = False
        self.killed = False
        self.finished = False

    def attach(self, proc) -> None:
        with self._lock:
            self._proc, self._stdin = proc, proc.stdin
            if self.killed:
                proc.kill()

    def cancel(self) -> bool:
        with self._lock:
            if self.finished:
                return False
            self.cancel_requested = True
            if self._stdin is None:
                return True
            try:
                self._stdin.write(b"cancel\n")
                self._stdin.flush()
            except (OSError, ValueError):
                return False
            return True

    def kill(self) -> None:
        with self._lock:
            self.killed = True
            if self._proc is not None and not self.finished:
                try:
                    self._proc.kill()
                except OSError:
                    pass

    def done(self) -> None:
        with self._lock:
            self.finished = True
            if self._stdin is not None:
                try:
                    self._stdin.close()
                except OSError:
                    pass
            self._stdin = None


class CommandRunner:
    """Runs allowlisted commands (replaced by a recording fake in the tests)."""

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ

    def available(self, path: str) -> bool:
        return path in ALLOWED_PATHS and os.access(path, os.X_OK)

    def _env(self) -> dict:
        env = dict(self.environ)
        env["LC_ALL"] = "C.UTF-8"
        return env

    def run(self, command: list[str], timeout: float = DEFAULT_TIMEOUT) -> Completed:
        """Run a short command and capture its output (C.UTF-8 locale, no stdin)."""
        _check(command)
        if command[0] == EXECUTABLES["pkexec"]:
            raise ValueError("privileged operations are streamed (stream()), never run()")
        if not timeout or timeout <= 0:
            raise ValueError("every command needs a timeout")
        name = program_name(command)
        if not self.available(command[0]):
            raise CommandUnavailable(name)
        try:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                  env=self._env(), check=False, close_fds=True)
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
        if command[0] == EXECUTABLES["pkexec"]:
            raise ValueError("privileged operations are streamed (stream()), never started detached")
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

    def stream(self, command: list[str], on_line, handle: StreamHandle, timeout: float = STREAM_TIMEOUT) -> int:
        """Run a long operation, calling on_line(text) for every output line; return its exit code.

        Standard output and standard error are read together, line by line
        (lines are cut at MAX_LINE characters). The program's standard input stays
        open so handle.cancel() can ask it to stop. After `timeout` seconds the
        program is killed and CommandFailed is raised.
        """
        _check(command)
        if not timeout or timeout <= 0:
            raise ValueError("every command needs a timeout")
        name = program_name(command)
        if not self.available(command[0]):
            raise CommandUnavailable(name)
        try:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=self._env(), close_fds=True)
        except OSError as exc:
            raise CommandFailed(name, f"{name} could not be started ({exc.strerror or exc}).") from None
        handle.attach(proc)
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            while True:
                raw = proc.stdout.readline(MAX_LINE)
                if not raw:
                    break
                on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            code = proc.wait()
        finally:
            timer.cancel()
            handle.done()
            proc.stdout.close()
        if expired.is_set():
            raise CommandFailed(name, f"{name} did not finish within {int(timeout)} seconds and was stopped.")
        return code


def failure(command: list[str], result: Completed, what: str) -> CommandFailed:
    """A CommandFailed for a command that ran but returned an unexpected exit code."""
    name = program_name(command)
    detail = _tail(result.stderr) or _tail(result.stdout)
    message = f"{what} failed ({name} exit code {result.code})" + (f": {detail}" if detail else ".")
    return CommandFailed(name, message, result.code)
