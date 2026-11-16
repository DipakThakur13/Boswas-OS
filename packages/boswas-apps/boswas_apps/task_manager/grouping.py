"""Processes as people see them: applications, background processes, system processes, sessions.

Standard library only. ``Grouper.groups`` folds the flat process table
into rows the way the Processes page shows them.

Which processes are one row
  A process joins the row of its parent when the parent's row is an
  application (everything an application starts belongs to it: the shells
  of a terminal, the helpers of a browser), or when it runs the same
  program as the first process of that row and as the same user (the
  workers of a server). Otherwise it starts a row of its own. Children of
  ``init`` always start a row. All kernel threads are one row.

What is an application (the heuristic)
  The process runs as the user of Task Manager, inside a user session
  (its control group is under ``user.slice``; where control groups do not
  tell, the user ID alone decides), and one of its names (the executable,
  the script when the executable is an interpreter, the kernel's name)
  equals the program of a visible, non-terminal desktop entry's ``Exec`` or
  its ``StartupWMClass`` (``desktopfile.AppIndex``). A process that matches
  another application than the row it would join starts its own row.
  Whether the process really owns a window is not known here: the
  compositor is not asked.

The rest
  Rows of the user that are not applications, and rows of other people,
  are background processes. Kernel threads and rows of system accounts
  (user ID below 1000, or ``nobody``) are system processes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .desktopfile import AppIndex, Application
from .procfs import tty_name
from .sampler import Proc, program_names
from .services import Session

KINDS = ("app", "background", "system")
SECTIONS = {"app": "Apps", "background": "Background processes", "system": "System processes"}
KERNEL_GROUP = "Kernel threads"
_SESSION_SCOPE = re.compile(r"/session-([A-Za-z0-9_.-]+)\.scope(?:/|$)")
_SYSTEM_UID_LIMIT = 1000
_NOBODY = 65534


@dataclass
class Group:
    kind: str
    root: Proc
    members: list[Proc]              # the first process of the row, then the others by PID
    name: str
    icon: str = ""                   # an icon name or path from the desktop entry; "" for none
    app: Application | None = None
    cpu: float = 0.0
    mem: int = 0
    disk: float | None = None

    @property
    def key(self) -> str:
        return f"{self.root.pid}.{self.root.start_ticks}"

    @property
    def title(self) -> str:
        return f"{self.name} ({len(self.members)})" if len(self.members) > 1 else self.name

    @property
    def status(self) -> str:
        return status_text(self.root.state)


def status_text(state: str) -> str:
    """What the Status column says: nothing for a process that runs or sleeps normally."""
    return {"T": "Suspended", "t": "Being traced", "Z": "Zombie", "X": "Dead", "x": "Dead"}.get(state, "")


def in_user_session(proc: Proc) -> bool | None:
    """Whether the control group puts the process in a user's session; None when it does not tell."""
    if not proc.cgroup or proc.cgroup == "/":
        return None
    return proc.cgroup.startswith("/user.slice/")


def is_system_uid(uid: int | None) -> bool:
    return uid is None or uid < _SYSTEM_UID_LIMIT or uid == _NOBODY


@dataclass
class _Known:
    args: tuple
    exe: str | None
    name: str
    names: list[str] = field(default_factory=list)
    app: Application | None = None
    candidate: bool = False


class Grouper:
    """Builds the rows of the Processes page; keeps what it worked out about a process between calls."""

    def __init__(self, index: AppIndex, uid: int) -> None:
        self.index = index
        self.uid = uid
        self._known: dict[int, _Known] = {}
        self._groups: dict[int, Group] = {}
        self._procs: dict[int, Proc] | None = None       # the table of the previous call
        self._root_of: dict[int, int] = {}                # and which row each of its processes was in

    def set_index(self, index: AppIndex) -> None:
        self.index = index
        self._known.clear()
        self._groups.clear()
        self._procs = None

    def _look(self, proc: Proc) -> _Known:
        known = self._known.get(proc.pid)
        if known is None or known.args is not proc.args or known.exe != proc.exe or known.name != proc.name:
            known = _Known(proc.args, proc.exe, proc.name)
            if not proc.kthread:
                known.names = program_names(proc)
                known.candidate = proc.uid == self.uid and in_user_session(proc) is not False
                if known.candidate:
                    known.app = self.index.match(known.names)
            self._known[proc.pid] = known
        return known

    def application_of(self, proc: Proc) -> Application | None:
        """The application this process is by itself (not the one whose row it is in)."""
        return self._look(proc).app

    def groups(self, procs: dict[int, Proc]) -> list[Group]:
        """The rows for ``procs``. A row whose processes are the same objects as at the previous call
        is the same ``Group`` object as then."""
        if len(self._known) > 2 * len(procs) + 64:
            self._known = {pid: known for pid, known in self._known.items() if pid in procs}
        kernel_root = 2 if 2 in procs and procs[2].kthread else None
        # Which processes are one row depends on what they are and who started them, not on what they
        # use. When none of that changed since the previous call, the rows are made up as they were.
        before = self._procs
        same = before is not None and len(before) == len(procs)
        if same:
            for pid, proc in procs.items():
                old = before.get(pid)
                if old is proc:
                    continue
                if (old is None or old.ppid != proc.ppid or old.name != proc.name or old.exe != proc.exe
                        or old.args != proc.args or old.uid != proc.uid or old.cgroup != proc.cgroup
                        or old.start_ticks != proc.start_ticks):
                    same = False
                    break
        self._procs = procs
        root_of: dict[int, int] = self._root_of if same else {}
        self._root_of = root_of

        def resolve(pid: int) -> int:
            chain = []
            current = pid
            while current not in root_of:
                proc = procs[current]
                if proc.kthread:
                    root_of[current] = kernel_root if kernel_root is not None else current
                    break
                parent = procs.get(proc.ppid)
                if parent is None or parent.kthread or proc.ppid <= 1 or proc.ppid == current or current in chain:
                    root_of[current] = current
                    break
                chain.append(current)
                current = proc.ppid
            for member in reversed(chain):
                proc = procs[member]
                root = root_of[proc.ppid]
                root_proc = procs[root]
                own, of_root = self._look(proc), self._look(root_proc)
                if own.app is not None and (of_root.app is None or of_root.app.app_id != own.app.app_id):
                    root_of[member] = member
                elif of_root.app is not None:
                    root_of[member] = root
                elif proc.uid == root_proc.uid and own.names[:1] == of_root.names[:1] and own.names:
                    root_of[member] = root
                else:
                    root_of[member] = member
            return root_of[pid]

        members: dict[int, list[Proc]] = {}
        if same:
            for pid, proc in procs.items():
                members.setdefault(root_of[pid], []).append(proc)
        else:
            for pid in procs:
                members.setdefault(resolve(pid), []).append(procs[pid])
        result = []
        previous, self._groups = self._groups, {}
        for root_pid, rows in members.items():
            root = procs[root_pid]
            if len(rows) > 1:
                rows.sort(key=lambda proc: (proc.pid != root_pid, proc.pid))
            old = previous.get(root_pid)
            if old is not None and len(old.members) == len(rows) and all(a is b for a, b in zip(old.members, rows)):
                # The very same process objects as last time (the sampler hands them out again while a
                # process does nothing): the row is the same too.
                self._groups[root_pid] = old
                result.append(old)
                continue
            known = self._look(root)
            if root.kthread:
                group = Group("system", root, rows, KERNEL_GROUP if root_pid == kernel_root else root.name)
            elif known.app is not None:
                group = Group("app", root, rows, known.app.name, known.app.icon, known.app)
            elif root.uid == self.uid or not is_system_uid(root.uid):
                group = Group("background", root, rows, root.name)
            else:
                group = Group("system", root, rows, root.name)
            disk = None
            cpu = 0.0
            mem = 0
            for proc in rows:
                cpu += proc.cpu
                mem += proc.mem
                if proc.disk is not None:
                    disk = proc.disk if disk is None else disk + proc.disk
            group.cpu, group.mem, group.disk = min(100.0, cpu), mem, disk
            self._groups[root_pid] = group
            result.append(group)
        return result


# --- sessions ----------------------------------------------------------------------------------------

def session_of_cgroup(cgroup: str) -> str | None:
    """The logind session a control group belongs to (``.../session-3.scope``), or None."""
    match = _SESSION_SCOPE.search(cgroup)
    return match.group(1) if match else None


def assign_sessions(procs: dict[int, Proc], sessions: list[Session]) -> dict[str, list[Proc]]:
    """The processes of every session, by session ID.

    A process in a session scope belongs to that session. The other
    processes of a user (the services of the user's own service manager)
    are counted with that user's first active session, or the first one
    when none is active. Processes of users without a session are left out.
    """
    result: dict[str, list[Proc]] = {session.id: [] for session in sessions}
    primary: dict[int, str] = {}
    for session in sorted(sessions, key=lambda s: (s.state != "active", s.since or 0.0)):
        primary.setdefault(session.uid, session.id)
    for proc in procs.values():
        if proc.kthread or proc.uid is None:
            continue
        session_id = session_of_cgroup(proc.cgroup) if proc.cgroup else None
        if session_id not in result:
            session_id = primary.get(proc.uid)
        if session_id is not None:
            result[session_id].append(proc)
    for rows in result.values():
        rows.sort(key=lambda proc: proc.pid)
    return result


def fallback_session(procs: dict[int, Proc], uid: int, user: str, pid: int) -> Session:
    """The one session shown when logind cannot be asked: the user of Task Manager, from /proc alone.

    The terminal is the controlling terminal of Task Manager's own process
    and the time is the start of the oldest process of the user.
    """
    own = procs.get(pid)
    mine = [proc for proc in procs.values() if proc.uid == uid and not proc.kthread]
    since = min((proc.start for proc in mine), default=0.0)
    return Session("", uid, user, "", tty_name(own.tty) if own is not None else "", "active", since, "", "user", 0)
