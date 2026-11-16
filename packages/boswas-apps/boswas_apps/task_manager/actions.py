"""What Task Manager does to processes: signals, priorities, properties, starting a program.

Standard library only. Every failure is an ``ActionError`` whose text is
written for the user: what was refused and, where the kernel said why, the
reason in plain words.

A PID alone does not identify a process: numbers are reused. Callers pass
the start time (``start_ticks`` of /proc/PID/stat) they saw; a signal or a
priority change is only made when the process with that PID still has that
start time. On kernels with process file descriptors (Linux 5.3) the
signal goes to the descriptor, so the check and the signal cannot be
separated by a reuse of the number.
"""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Mapping

from . import procfs
from .sampler import Proc
from .textutil import decode

SIGNALS = (
    ("Stop (SIGSTOP)", signal.SIGSTOP), ("Continue (SIGCONT)", signal.SIGCONT), ("Hang up (SIGHUP)", signal.SIGHUP),
    ("Interrupt (SIGINT)", signal.SIGINT), ("Terminate (SIGTERM)", signal.SIGTERM), ("Kill (SIGKILL)", signal.SIGKILL),
)
# Priority names as in the Task Manager people know, with the nice value each one sets.
PRIORITIES = (("Very high", -20), ("High", -10), ("Above normal", -5), ("Normal", 0), ("Below normal", 5),
              ("Low", 10), ("Very low", 19))
FILES_PROGRAM = "boswas-files"
_DELETED = " (deleted)"


class ActionError(Exception):
    """An action could not be done; the text is for the user."""


def priority_name(nice: int) -> str:
    """The name of the priority level a nice value falls into."""
    best = min(PRIORITIES, key=lambda level: (abs(level[1] - nice), level[1]))
    return best[0]


def _who(pid: int, name: str = "") -> str:
    return f"{name} (PID {pid})" if name else f"PID {pid}"


def explain(error: OSError) -> str:
    """Why the kernel refused, in plain words."""
    if error.errno == errno.ESRCH:
        return "the process has already ended"
    if error.errno == errno.EPERM:
        return "it belongs to another user or to the system, and you are not allowed to change it"
    if error.errno == errno.EACCES:
        return "permission denied"
    return (error.strerror or str(error)).lower()


def start_ticks_of(pid: int, proc: str = "/proc") -> tuple[int, str] | None:
    """(start time, state letter) of the process that has ``pid`` now; None when there is none."""
    try:
        parsed = procfs.parse_stat(procfs.read_file(f"{proc}/{pid}/stat", 4096))
    except OSError:
        return None
    return (parsed.starttime, parsed.state) if parsed is not None else None


def condition(pid: int, start_ticks: int | None, proc: str = "/proc") -> str:
    """``running``, ``zombie`` (ended, not yet collected by its parent) or ``gone``."""
    found = start_ticks_of(pid, proc)
    if found is None or (start_ticks is not None and found[0] != start_ticks):
        return "gone"
    return "zombie" if found[1] in ("Z", "X", "x") else "running"


def send_signal(pid: int, signum: int, start_ticks: int | None = None, name: str = "", *, proc: str = "/proc",
                kill: Callable[[int, int], None] | None = None) -> None:
    """Send ``signum`` to the process ``pid`` that started at ``start_ticks``.

    ``kill`` replaces the system call (tests). PIDs below 1 are refused:
    to ``kill`` they mean whole process groups.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        raise ActionError("That is not a process.")
    who = _who(pid, name)
    try:
        if kill is None and hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                descriptor = os.pidfd_open(pid)
            except OSError as error:
                if error.errno not in (errno.ENOSYS, errno.EPERM, errno.EMFILE, errno.ENFILE):
                    raise
                descriptor = -1
            if descriptor >= 0:
                try:
                    if start_ticks is not None and condition(pid, start_ticks, proc) == "gone":
                        raise ActionError(f"{who} has already ended.")
                    signal.pidfd_send_signal(descriptor, signum)
                finally:
                    os.close(descriptor)
                return
        if start_ticks is not None and condition(pid, start_ticks, proc) == "gone":
            raise ActionError(f"{who} has already ended.")
        (kill or os.kill)(pid, signum)
    except ProcessLookupError:
        raise ActionError(f"{who} has already ended.") from None
    except OSError as error:
        raise ActionError(f"The system refused to signal {who}: {explain(error)}.") from error


def descendants(procs: Mapping[int, Proc], pid: int) -> list[int]:
    """``pid`` and every process below it, children after their parents."""
    children: dict[int, list[int]] = {}
    for proc in procs.values():
        children.setdefault(proc.ppid, []).append(proc.pid)
    result, queue, seen = [], [pid], {pid}
    while queue:
        current = queue.pop(0)
        result.append(current)
        for child in sorted(children.get(current, ())):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return result


def ancestors(procs: Mapping[int, Proc], pid: int) -> list[int]:
    """The parents of ``pid`` up to init, nearest first."""
    result = []
    current = procs.get(pid)
    while current is not None and current.ppid > 0 and current.ppid not in result and len(result) < 256:
        result.append(current.ppid)
        current = procs.get(current.ppid)
    return result


def set_priority(pid: int, nice: int, start_ticks: int | None = None, name: str = "", *, proc: str = "/proc",
                 setpriority: Callable[[int, int, int], None] | None = None) -> None:
    """Set the nice value of a process. Lowering it (a higher priority) needs privileges; the refusal of
    the kernel is reported as it is."""
    if not isinstance(pid, int) or pid < 1:
        raise ActionError("That is not a process.")
    who = _who(pid, name)
    nice = max(-20, min(19, int(nice)))
    if start_ticks is not None and condition(pid, start_ticks, proc) == "gone":
        raise ActionError(f"{who} has already ended.")
    try:
        (setpriority or os.setpriority)(os.PRIO_PROCESS, pid, nice)
    except ProcessLookupError:
        raise ActionError(f"{who} has already ended.") from None
    except PermissionError as error:
        if error.errno == errno.EACCES:
            raise ActionError(f"The system refused to give {who} the priority “{priority_name(nice)}” "
                              f"(nice {nice}): permission denied. Raising a priority above the level a process "
                              "has needs administrator rights; you can lower it at any time.") from error
        raise ActionError(f"The system refused to change the priority of {who}: {explain(error)}.") from error
    except OSError as error:
        raise ActionError(f"The priority of {who} could not be changed: {explain(error)}.") from error


# --- properties --------------------------------------------------------------------------------------

@dataclass
class Properties:
    pid: int
    exe: str | None = None           # None: could not be read
    exe_deleted: bool = False        # the file was replaced or removed after the program started
    cwd: str | None = None
    open_files: int | None = None
    limits: list[procfs.Limit] = field(default_factory=list)
    status: dict[str, str] = field(default_factory=dict)
    denied: bool = False             # the kernel does not let this user look


def read_properties(pid: int, proc: str = "/proc") -> Properties:
    """What the Properties dialog shows beyond the table: executable, working directory, the number of
    open files and the resource limits. The environment is not read."""
    base = f"{proc}/{pid}"
    result = Properties(pid)
    try:
        exe = decode(os.fsencode(os.readlink(f"{base}/exe")), 4096)
        if exe.endswith(_DELETED):
            exe, result.exe_deleted = exe[:-len(_DELETED)], True
        result.exe = exe
    except PermissionError:
        result.denied = True
    except OSError:
        pass
    try:
        result.cwd = decode(os.fsencode(os.readlink(f"{base}/cwd")), 4096)
    except PermissionError:
        result.denied = True
    except OSError:
        pass
    try:
        result.open_files = len(os.listdir(f"{base}/fd"))
    except PermissionError:
        result.denied = True
    except OSError:
        pass
    try:
        result.limits = procfs.parse_limits(procfs.read_file(f"{base}/limits", 16384))
    except OSError:
        pass
    try:
        result.status = procfs.parse_status(procfs.read_file(f"{base}/status", 16384))
    except OSError:
        pass
    return result


def file_location(exe: str | None) -> str:
    """The path to show in the file manager for an executable; ActionError when there is none to show."""
    if not exe:
        raise ActionError("The location of this program is not known: the system does not let you see the "
                          "executable of another user's process, and kernel threads have none.")
    path = exe[:-len(_DELETED)] if exe.endswith(_DELETED) else exe
    if not path.startswith("/") or not os.path.exists(path):
        raise ActionError(f"“{path}” no longer exists: the program was removed or replaced after it started.")
    return path


# --- starting programs -------------------------------------------------------------------------------

def split_command(text: str) -> list[str]:
    """A typed command as an argument list, with the quoting rules of a shell and nothing else of one:
    no variables, no wildcards, no pipes."""
    try:
        arguments = shlex.split(text, comments=False, posix=True)
    except ValueError:
        raise ActionError("The command is not complete: a quotation mark is not closed.") from None
    if not arguments or not arguments[0]:
        raise ActionError("Type the name of a program.")
    if any("\0" in argument for argument in arguments):
        raise ActionError("The command contains a character that cannot be part of an argument.")
    return arguments


def resolve_directory(text: str, home: str | None = None) -> str:
    """The working directory a person typed (``~`` is their home; empty means home)."""
    home = home or os.path.expanduser("~")
    text = text.strip()
    if not text:
        return home
    if text == "~" or text.startswith("~/"):
        text = home + text[1:]
    path = os.path.abspath(text)
    if not os.path.isdir(path):
        raise ActionError(f"The folder “{text}” does not exist.")
    return path


def resolve_program(name: str, directory: str, search_path: str | None = None, home: str | None = None) -> str:
    """The full path of the program to start: looked up in ``$PATH`` when ``name`` has no slash."""
    if name == "~" or name.startswith("~/"):
        name = (home or os.path.expanduser("~")) + name[1:]
    if "/" in name:
        path = os.path.abspath(os.path.join(directory, name))
        if not os.path.exists(path):
            raise ActionError(f"“{name}” does not exist.")
        if os.path.isdir(path):
            raise ActionError(f"“{name}” is a folder, not a program.")
        if not os.access(path, os.X_OK):
            raise ActionError(f"“{name}” is not a program you may run (it is not marked executable).")
        return path
    found = shutil.which(name, path=search_path)
    if found is None:
        raise ActionError(f"No program named “{name}” was found. Check the spelling, or give the full path.")
    return os.path.abspath(found)


def start_program(arguments: list[str], directory: str, *, search_path: str | None = None,
                  popen: Callable[..., object] = subprocess.Popen) -> object:
    """Start a program with an argument list, without a shell, in its own session. Returns the process
    object (keep it and call ``poll()`` now and then so the ended child is collected)."""
    if not arguments:
        raise ActionError("Type the name of a program.")
    program = resolve_program(arguments[0], directory, search_path)
    try:
        return popen([arguments[0], *arguments[1:]], executable=program, cwd=directory,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
    except OSError as error:
        if error.errno == errno.ENOEXEC:
            reason = "it is not a program this computer can run"
        else:
            reason = explain(error)
        raise ActionError(f"“{arguments[0]}” could not be started: {reason}.") from error


def show_in_files(path: str, *, search_path: str | None = None,
                  popen: Callable[..., object] = subprocess.Popen) -> object:
    """Open the file manager with ``path`` selected (``boswas-files --select PATH``)."""
    if shutil.which(FILES_PROGRAM, path=search_path) is None:
        raise ActionError("Files (boswas-files) is not installed, so the location cannot be shown.")
    return start_program([FILES_PROGRAM, "--select", path], os.path.expanduser("~"), search_path=search_path,
                         popen=popen)
