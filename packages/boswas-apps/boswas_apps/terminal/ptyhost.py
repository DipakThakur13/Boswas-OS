"""The child process of a terminal on a pseudo-terminal (no Qt).

``PtyProcess`` opens a pseudo-terminal pair, starts one program on the
slave side as the leader of a new session with the slave as its controlling
terminal, and keeps the master side: ``read`` and ``write`` never block,
``resize`` tells the kernel the window size (which sends SIGWINCH to the
foreground process group), ``poll`` reaps the child.

The program is started with an argument list, never through a shell. The
only code that runs between fork and exec is ``_child_setup``: it makes the
slave the controlling terminal and gives the child default signal handling.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pwd
import shlex
import shutil
import signal
import struct
import subprocess
import termios

from . import PROGRAM, VERSION

BASHRC = "/usr/share/boswas/terminal/bashrc"
FALLBACK_SHELL = "/bin/sh"
# Not for the child: the compositor's private descriptors, what belongs to our own start, and
# sizes a program must ask the terminal for.
DROPPED_ENVIRONMENT = (
    "WAYLAND_SOCKET", "BOSWAS_COMPOSITOR_FD", "COLUMNS", "LINES", "TERMCAP", "WINDOWID", "XDG_ACTIVATION_TOKEN",
    "DESKTOP_STARTUP_ID", "GIO_LAUNCHED_DESKTOP_FILE", "GIO_LAUNCHED_DESKTOP_FILE_PID", "INVOCATION_ID",
    "JOURNAL_STREAM", "NOTIFY_SOCKET", "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "MANAGERPID",
    "SYSTEMD_EXEC_PID", "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE",
)
_DEFAULT_SIGNALS = ("SIGINT", "SIGQUIT", "SIGTSTP", "SIGTTIN", "SIGTTOU", "SIGCHLD", "SIGHUP", "SIGTERM", "SIGCONT",
                    "SIGALRM", "SIGUSR1", "SIGUSR2", "SIGWINCH", "SIGPIPE")
MAX_COMMAND_LENGTH = 32768


class SpawnError(Exception):
    """The program could not be started; the text is for the user."""


def _usable_shell(path: str | None) -> bool:
    return bool(path) and path.startswith("/") and os.path.isfile(path) and os.access(path, os.X_OK)


def default_shell(environ: dict | None = None) -> str:
    """The user's login shell: the passwd entry, then $SHELL, then /bin/sh."""
    environ = os.environ if environ is None else environ
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except (KeyError, OSError):
        shell = ""
    if _usable_shell(shell):
        return shell
    shell = environ.get("SHELL", "")
    if _usable_shell(shell):
        return shell
    return FALLBACK_SHELL


def home_directory() -> str:
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        home = ""
    home = home or os.environ.get("HOME", "")
    return home if home.startswith("/") and os.path.isdir(home) else "/"


def shell_command(shell: str, bashrc: str = BASHRC) -> tuple[str, list[str]]:
    """(program, argument list) that starts ``shell`` for interactive use.

    bash takes the Boswas start-up file when it is installed (as the Konsole
    profile of Boswas does); it reads the user's own ~/.bashrc first. Every
    other shell is started as a login shell, with a dash in front of its name.
    """
    name = os.path.basename(shell)
    if name == "bash" and os.path.isfile(bashrc):
        return shell, ["bash", "--rcfile", bashrc]
    return shell, ["-" + name]


def split_command(command: str) -> list[str]:
    """A command line from the settings as an argument list (quoting as in a shell, nothing is run)."""
    if len(command) > MAX_COMMAND_LENGTH or "\0" in command:
        raise SpawnError("The command is too long.")
    try:
        return shlex.split(command)
    except ValueError as error:
        raise SpawnError(f"The command is not written correctly: {error}.") from None


def resolve_program(name: str, cwd: str, environ: dict) -> str:
    """The absolute path of the program ``name`` (looked up in PATH when it has no slash)."""
    if "/" in name:
        path = name if name.startswith("/") else os.path.normpath(os.path.join(cwd, name))
    else:
        path = shutil.which(name, path=environ.get("PATH") or os.defpath) or ""
    if not path:
        raise SpawnError(f"“{name}” was not found.")
    if os.path.isdir(path):
        raise SpawnError(f"“{name}” is a folder, not a program.")
    if not os.path.isfile(path):
        raise SpawnError(f"“{name}” was not found.")
    if not os.access(path, os.X_OK):
        raise SpawnError(f"“{name}” may not be run (no permission).")
    return path


def child_environment(base: dict | None = None, shell: str | None = None) -> dict[str, str]:
    """The environment of the child: ours without what is private to us, with the terminal's identity."""
    environ = dict(os.environ if base is None else base)
    for name in DROPPED_ENVIRONMENT:
        environ.pop(name, None)
    environ["TERM"] = "xterm-256color"
    environ["COLORTERM"] = "truecolor"
    environ["TERM_PROGRAM"] = PROGRAM
    environ["TERM_PROGRAM_VERSION"] = VERSION
    if shell:
        environ["SHELL"] = shell
    return environ


def usable_directory(*candidates: str | None) -> str:
    """The first of ``candidates`` that is a directory we may enter; the home directory or / otherwise."""
    for candidate in (*candidates, home_directory(), "/"):
        if candidate and candidate.startswith("/") and os.path.isdir(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "/"


def _child_setup() -> None:
    # Runs in the child between fork and exec. Standard input is the slave of the pseudo-terminal and the
    # process already leads a new session: take the terminal as its controlling terminal.
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass
    # A program started here must not inherit ignored or blocked signals from the desktop.
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, [])
        for name in _DEFAULT_SIGNALS:
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    except (OSError, ValueError):
        pass


def set_window_size(fd: int, cols: int, rows: int, width: int = 0, height: int = 0) -> None:
    """TIOCSWINSZ: characters and pixels of the terminal window."""
    limit = 0xFFFF
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", min(max(rows, 1), limit), min(max(cols, 1), limit),
                                                    min(max(width, 0), limit), min(max(height, 0), limit)))


class PtyProcess:
    """One program running on a pseudo-terminal."""

    def __init__(self, argv: list[str], *, program: str | None = None, cwd: str | None = None,
                 environ: dict | None = None, cols: int = 80, rows: int = 24, width: int = 0,
                 height: int = 0) -> None:
        if not argv or any("\0" in argument for argument in argv):
            raise SpawnError("There is no command to run.")
        environ = child_environment() if environ is None else environ
        directory = usable_directory(cwd)
        path = program or resolve_program(argv[0], directory, environ)
        self.argv = list(argv)
        self.program = path
        self.master = -1
        self.returncode: int | None = None
        self.exit_signal = 0            # the signal that ended the child, if one did
        master, slave = os.openpty()
        try:
            try:
                attributes = termios.tcgetattr(slave)
                attributes[0] |= termios.IUTF8
                termios.tcsetattr(slave, termios.TCSANOW, attributes)
            except termios.error:
                pass
            set_window_size(master, cols, rows, width, height)
            os.set_blocking(master, False)
            try:
                self.process = subprocess.Popen(
                    self.argv, executable=path, stdin=slave, stdout=slave, stderr=slave, cwd=directory, env=environ,
                    start_new_session=True, preexec_fn=_child_setup, close_fds=True, restore_signals=True)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
                raise SpawnError(f"“{argv[0]}” could not be started: {reason}.") from None
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master = master
        self.pid = self.process.pid
        self.cwd_at_start = directory

    # --- data ---
    def read(self, size: int = 65536) -> bytes | None:
        """What the child wrote: bytes, b"" when there is nothing now, None when the terminal has hung up."""
        if self.master < 0:
            return None
        try:
            data = os.read(self.master, size)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError as error:
            if error.errno == errno.EAGAIN:
                return b""
            return None                 # EIO: every descriptor of the slave side is closed
        return data or None

    def write(self, data: bytes) -> int:
        """Send bytes to the child; returns how many the kernel took (0 when its buffer is full)."""
        if self.master < 0 or not data:
            return 0
        try:
            return os.write(self.master, data)
        except (BlockingIOError, InterruptedError):
            return 0
        except OSError as error:
            if error.errno == errno.EAGAIN:
                return 0
            return len(data)            # the other side is gone: nothing can be delivered any more

    def resize(self, cols: int, rows: int, width: int = 0, height: int = 0) -> None:
        if self.master >= 0:
            try:
                set_window_size(self.master, cols, rows, width, height)
            except OSError:
                pass

    # --- the process ---
    def poll(self) -> int | None:
        """The exit status once the child has ended (reaping it): 0..255, or 128 + signal number."""
        if self.returncode is None:
            status = self.process.poll()
            if status is not None:
                self.returncode = status if status >= 0 else 128 - status
                self.exit_signal = -status if status < 0 else 0
        return self.returncode

    def alive(self) -> bool:
        return self.poll() is None

    def working_directory(self) -> str | None:
        """The current directory of the child, read from /proc."""
        try:
            path = os.readlink(f"/proc/{self.pid}/cwd")
        except OSError:
            return None
        return path if path.startswith("/") and os.path.isdir(path) else None

    def foreground_process(self) -> str | None:
        """The name of the program in the foreground of the terminal when it is not the child itself."""
        if self.master < 0 or not self.alive():
            return None
        try:
            group = os.tcgetpgrp(self.master)
        except OSError:
            return None
        if group <= 0 or group == self.pid:
            return None
        try:
            with open(f"/proc/{group}/comm", encoding="utf-8", errors="replace") as handle:
                name = handle.read(64).strip()
        except OSError:
            name = ""
        return name or "a program"

    def hangup(self) -> None:
        """Close the terminal: the kernel hangs up on the session; the child gets SIGHUP as well."""
        if self.master >= 0:
            try:
                group = os.tcgetpgrp(self.master)
            except OSError:
                group = 0
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = -1
            if self.alive():
                for target in {self.pid, group} - {0}:
                    try:
                        os.killpg(target, signal.SIGHUP)
                    except (OSError, ProcessLookupError):
                        pass
                    if target != self.pid:
                        try:
                            os.killpg(target, signal.SIGCONT)
                        except (OSError, ProcessLookupError):
                            pass

    def kill(self) -> None:
        """SIGKILL for a child that ignored the hang-up (used when the application quits)."""
        if self.alive():
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
