"""The pages that list processes: Processes, Details and Users."""

from __future__ import annotations

import math

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QStackedLayout, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import actions, grouping, procfs
from .. import textutil as fmt
from ..desktopfile import AppIndex
from ..grouping import Group
from ..sampler import Proc, Sample
from ..services import Session
from .dialogs import app_icon
from .table import Column, Row, TableView, TreeModel

SECTION_RANK = {"app": 1, "background": 2, "system": 3}
_TENTH_MB = 104857.6


def proc_key(proc: Proc) -> str:
    return f"p{proc.pid}.{proc.start_ticks}"


def disk_heat(value: float | None) -> float | None:
    """0..1 for a disk rate: 10 KB/s is just visible, 100 MB/s is full."""
    if value is None:
        return None
    return min(1.0, math.log10(1 + value / 10240) / 4)


def matches(needle: str, proc: Proc) -> bool:
    return (needle in proc.name.casefold() or needle == str(proc.pid) or needle in proc.user.casefold()
            or needle in proc.cmdline.casefold())


class Page(QWidget):
    """A page of the window. ``parts`` says what the sampler must read while the page is shown."""

    key = ""
    searchable = True
    search_hint = "Filter by name, PID or user"

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.bar = fluent.CommandBar()
        self.filter_text = ""

    def parts(self) -> frozenset[str]:
        return frozenset()

    def apply(self, sample: Sample) -> None:
        """A new sample has arrived (only called while the page is the one shown)."""

    def set_filter(self, text: str) -> None:
        self.filter_text = text.strip().casefold()

    def activated(self) -> None:
        """The page has become the one shown."""

    def deactivated(self) -> None:
        pass

    def refresh(self) -> None:
        """F5."""
        self.window.sampling.request()

    def settings_changed(self) -> None:
        pass

    def copy_selection(self) -> str:
        """The selected row as text for the clipboard ("" when nothing is selected)."""
        return ""


class ProcessTablePage(Page):
    """What the three process pages share: the table, the selection, the commands on a process."""

    def __init__(self, window, columns: list[Column], *, tree: bool, find_column: str = "name",
                 stretch: str | None = None) -> None:
        super().__init__(window)
        self.model = TreeModel(columns, self)
        self.view = TableView(self.model, self, tree=tree, find_column=find_column, stretch=stretch)
        self.view.setAccessibleName(self.key.capitalize())
        self.empty = fluent.EmptyState("Nothing matches", "No process matches what you typed in the search box.",
                                       "system-search-symbolic")
        self.stack = QStackedLayout()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.empty)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 0, 20, 16)
        layout.setSpacing(8)
        layout.addLayout(self.stack, 1)
        self.procs: dict[int, Proc] = {}
        self.memory_total = 0
        self.view.context_requested.connect(self._context)
        self.view.delete_pressed.connect(self.end_selected)
        self.view.selectionModel().currentChanged.connect(lambda *_args: self.sync_commands())

    # --- selection ---
    def selected_procs(self) -> tuple[str, list[Proc]]:
        """(what to call the selection, its processes: the first one is the one a command acts on)."""
        row = self.view.current_row()
        if row is None:
            return "", []
        data = row.data
        if isinstance(data, Proc):
            return data.name, [data]
        if isinstance(data, Group):
            return data.name, list(data.members)
        return "", []

    def selected_proc(self) -> Proc | None:
        _title, procs = self.selected_procs()
        return procs[0] if procs else None

    def sync_commands(self) -> None:
        pass

    def end_selected(self) -> None:
        title, procs = self.selected_procs()
        if procs:
            self.window.end_task(procs[:1], title)

    def end_tree_selected(self) -> None:
        title, procs = self.selected_procs()
        if procs:
            pids = actions.descendants(self.procs, procs[0].pid)
            self.window.end_task([self.procs[pid] for pid in pids if pid in self.procs], title, tree=True)

    def copy_selection(self) -> str:
        index = self.view.currentIndex()
        if self.view.current_row() is None:
            return ""
        cells = [self.model.data(index.siblingAtColumn(column), Qt.ItemDataRole.DisplayRole) or ""
                 for column in range(len(self.model.columns))]
        return "\t".join(cells)

    def show_rows(self, rows: list[Row], total: int) -> None:
        self.model.set_rows(rows)
        self.stack.setCurrentWidget(self.empty if not rows and total and self.filter_text else self.view)
        self.sync_commands()

    # --- the context menu ---
    def context_menu(self) -> fluent.MenuFlyout | None:
        return None

    def _context(self, position: QPoint) -> None:
        menu = self.context_menu()
        if menu is not None:
            self._menu = menu
            menu.popup(position)

    def add_process_items(self, menu: fluent.MenuFlyout, proc: Proc, title: str, *, details: bool) -> None:
        window = self.window
        menu.add_item("End task", self.end_selected, icon="window-close-symbolic", shortcut="Del")
        menu.add_item("End process tree", self.end_tree_selected)
        menu.addSeparator()
        if details:
            menu.add_item("Go to details", lambda: window.go_to_details(proc))
        menu.add_item("Open file location", lambda: window.open_location(proc), icon="folder-symbolic")
        menu.add_item("Properties", lambda: window.show_properties(proc), shortcut="Alt+Return")

    def cpu_heat(self, row: Row) -> float | None:
        value = getattr(row.data, "cpu", None)
        return None if value is None else value / 100

    def mem_heat(self, row: Row) -> float | None:
        value = getattr(row.data, "mem", None)
        if value is None or not self.memory_total:
            return None if value is None else 0.0
        return value / self.memory_total


# --- Processes -------------------------------------------------------------------------------------------

def _group_icon(row: Row):
    data = row.data
    if isinstance(data, Group):
        if data.app is not None:
            return app_icon(data.icon)
        return fluent.icon("preferences-system" if data.root.kthread else "application-x-executable")
    return fluent.icon("application-x-executable")


def _title(row: Row) -> str:
    data = row.data
    if row.header:
        return f"{data[0]} ({data[1]})"
    return data.title if isinstance(data, Group) else data.name


def _status(row: Row) -> str:
    data = row.data
    return data.status if isinstance(data, Group) else grouping.status_text(data.state)


def _command(row: Row) -> str:
    data = row.data
    return data.root.cmdline if isinstance(data, Group) else data.cmdline


class ProcessesPage(ProcessTablePage):
    key = "processes"
    search_hint = "Filter by name, PID or user"

    def __init__(self, window) -> None:
        base = [
            Column("name", "Name", _title, lambda row: _title(row).casefold(), width=320, icon=_group_icon),
            Column("status", "Status", _status, width=110),
            Column("cpu", "CPU", lambda row: fmt.percent(row.data.cpu), lambda row: row.data.cpu, right=True,
                   width=84, heat=lambda row: self.cpu_heat(row), descending_first=True),
            Column("memory", "Memory", lambda row: fmt.megabytes(row.data.mem), lambda row: row.data.mem,
                   right=True, width=110, heat=lambda row: self.mem_heat(row), descending_first=True),
            Column("disk", "Disk", lambda row: fmt.rate(row.data.disk) if row.data.disk is not None else "",
                   lambda row: row.data.disk, right=True, width=96,
                   heat=lambda row: disk_heat(row.data.disk) if row.data.disk is not None else 0.0,
                   descending_first=True),
        ]
        command = Column("command", "Command line", _command, width=420, tooltip=True)
        super().__init__(window, base + ([command] if window.settings.full_command_line else []), tree=True,
                         stretch=None if window.settings.full_command_line else "name")
        self._base_columns, self._command_column = base, command
        self.grouper = grouping.Grouper(AppIndex([]), window.backend.uid)
        self.groups: list[Group] = []
        bar = self.bar
        self.run_action = bar.add_command("Run new task", "list-add-symbolic", window.run_new_task, shortcut="Ctrl+N")
        self.end_action = bar.add_command("End task", "window-close-symbolic", self.end_selected,
                                          tooltip="End the selected task (Delete)")
        self.group_action = bar.add_command("Group by type", None, self._toggle_grouping, primary=False,
                                            checkable=True)
        self.group_action.setChecked(window.settings.group_by_type)
        bar.add_command("Expand all", None, self.view.expandAll, primary=False)
        bar.add_command("Collapse all", None, self.view.collapseAll, primary=False)
        self.sync_commands()

    def _columns(self, with_command: bool) -> list[Column]:
        return self._base_columns + ([self._command_column] if with_command else [])

    def parts(self) -> frozenset[str]:
        return frozenset({"procs", "io", "memory", "disks"})

    def settings_changed(self) -> None:
        wanted = self.window.settings.full_command_line
        if wanted != (self.model.column_index("command") >= 0):
            self.view._stretch = None if wanted else "name"
            self.model.set_columns(self._columns(wanted))
        self.group_action.setChecked(self.window.settings.group_by_type)
        self.rebuild()

    def set_applications(self, index: AppIndex) -> None:
        self.grouper.set_index(index)
        self.rebuild()

    def _toggle_grouping(self) -> None:
        self.window.settings.group_by_type = self.group_action.isChecked()
        self.window.save_settings()
        self.rebuild()

    def sync_commands(self) -> None:
        self.end_action.setEnabled(self.selected_proc() is not None)

    def set_filter(self, text: str) -> None:
        super().set_filter(text)
        self.rebuild()

    def apply(self, sample: Sample) -> None:
        if sample.memory is not None:
            self.memory_total = sample.memory.total
            self.model.set_metric("memory", fmt.percent(sample.memory.percent, 0))
        if sample.total_cpu is not None:
            self.model.set_metric("cpu", fmt.percent(sample.total_cpu, 0))
        if sample.disks is not None:
            busiest = max((disk.active for disk in sample.disks), default=0.0)
            self.model.set_metric("disk", fmt.percent(busiest, 0))
        if sample.procs is not None:
            self.procs = sample.procs
            self.groups = self.grouper.groups(sample.procs)
            self.rebuild()

    def rebuild(self) -> None:
        needle = self.filter_text
        with_command = self.model.column_index("command") >= 0
        grouped = self.window.settings.group_by_type
        rows: list[Row] = []
        counts = {kind: 0 for kind in SECTION_RANK}
        for group in self.groups:
            if needle and not (needle in group.name.casefold() or any(matches(needle, proc) for proc in group.members)):
                continue
            counts[group.kind] += 1
            disk = None if group.disk is None else int(group.disk / _TENTH_MB)
            children = None
            if len(group.members) > 1:
                children = [Row(proc_key(proc), proc,
                                (proc.name, proc.state, int(proc.cpu * 10), int(proc.mem / _TENTH_MB),
                                 None if proc.disk is None else int(proc.disk / _TENTH_MB),
                                 proc.args if with_command else None))
                            for proc in group.members]
            rows.append(Row("g" + group.key, group,
                            (group.title, group.root.state, int(group.cpu * 10), int(group.mem / _TENTH_MB), disk,
                             group.icon, group.root.args if with_command else None),
                            children, SECTION_RANK[group.kind] if grouped else 0))
        if grouped:
            for kind, rank in SECTION_RANK.items():
                if counts[kind]:
                    rows.append(Row("s:" + kind, (grouping.SECTIONS[kind], counts[kind]), counts[kind], None, rank, True))
        self.show_rows(rows, len(self.groups))

    def section_counts(self) -> dict[str, int]:
        return {row.data[0]: row.data[1] for row in self.model.top_rows() if row.header}

    def context_menu(self) -> fluent.MenuFlyout | None:
        title, procs = self.selected_procs()
        if not procs:
            return None
        menu = fluent.MenuFlyout(self.view)
        index = self.view.currentIndex().siblingAtColumn(0)
        if self.model.hasChildren(index):
            expanded = self.view.isExpanded(index)
            menu.add_item("Collapse" if expanded else "Expand", lambda: self.view.setExpanded(index, not expanded))
            menu.addSeparator()
        self.add_process_items(menu, procs[0], title, details=True)
        return menu


# --- Details ---------------------------------------------------------------------------------------------

class DetailsPage(ProcessTablePage):
    key = "details"
    search_hint = "Filter by name, PID, user or command line"

    def __init__(self, window) -> None:
        number = dict(right=True, descending_first=True)
        columns = [
            Column("name", "Name", lambda row: row.data.name, width=190,
                   icon=lambda row: fluent.icon("application-x-executable")),
            Column("pid", "PID", lambda row: str(row.data.pid), lambda row: row.data.pid, right=True, width=66),
            Column("status", "Status", lambda row: procfs.STATES.get(row.data.state, row.data.state), width=84),
            Column("user", "User name", lambda row: row.data.user, width=92),
            Column("cpu", "CPU", lambda row: fmt.percent(row.data.cpu), lambda row: row.data.cpu, width=64,
                   heat=lambda row: self.cpu_heat(row), **number),
            Column("memory", "Memory (RSS)", lambda row: fmt.megabytes(row.data.rss), lambda row: row.data.rss,
                   width=110, **number),
            Column("pss", "PSS", lambda row: fmt.megabytes(row.data.pss) if row.data.pss is not None else "",
                   lambda row: row.data.pss, width=92, **number),
            Column("threads", "Threads", lambda row: str(row.data.threads), lambda row: row.data.threads, width=70,
                   **number),
            Column("nice", "Nice", lambda row: str(row.data.nice), lambda row: row.data.nice, right=True, width=52),
            Column("started", "Started", lambda row: fmt.moment(row.data.start, self._now),
                   lambda row: row.data.start, width=124, right=True),
            Column("command", "Command line", lambda row: row.data.cmdline, width=520, tooltip=True),
        ]
        super().__init__(window, columns, tree=False)
        self._now: float | None = None
        self._pending_pid: int | None = None
        bar = self.bar
        bar.add_command("Run new task", "list-add-symbolic", window.run_new_task)
        self.end_action = bar.add_command("End task", "window-close-symbolic", self.end_selected,
                                          tooltip="End the selected process (Delete)")
        self.properties_action = bar.add_command("Properties", "help-about-symbolic", self._properties)
        self.tree_action = bar.add_command("End process tree", None, self.end_tree_selected, primary=False)
        self.location_action = bar.add_command("Open file location", "folder-symbolic", self._location, primary=False)
        self.copy_action = bar.add_command("Copy command line", "edit-copy-symbolic", self._copy_command,
                                           primary=False)
        self.view.row_activated.connect(self._properties)
        self.sync_commands()

    def parts(self) -> frozenset[str]:
        return frozenset({"procs", "pss"})

    def sync_commands(self) -> None:
        selected = self.selected_proc() is not None
        for action in (self.end_action, self.properties_action, self.tree_action, self.location_action,
                       self.copy_action):
            action.setEnabled(selected)

    def _properties(self) -> None:
        proc = self.selected_proc()
        if proc is not None:
            self.window.show_properties(proc)

    def _location(self) -> None:
        proc = self.selected_proc()
        if proc is not None:
            self.window.open_location(proc)

    def _copy_command(self) -> None:
        proc = self.selected_proc()
        if proc is not None:
            self.window.copy_text(proc.cmdline, "The command line was copied.")

    def set_filter(self, text: str) -> None:
        super().set_filter(text)
        self.rebuild()

    def select_pid(self, pid: int) -> None:
        """Select the process ``pid`` now, or as soon as the table has it."""
        proc = self.procs.get(pid)
        if proc is None or not self.view.select_key(proc_key(proc)):
            self._pending_pid = pid

    def apply(self, sample: Sample) -> None:
        if sample.procs is None:
            return
        self._now = sample.wall
        self.procs = sample.procs
        self.rebuild()

    def rebuild(self) -> None:
        needle = self.filter_text
        rows = []
        for proc in self.procs.values():
            if needle and not matches(needle, proc):
                continue
            rows.append(Row(proc_key(proc), proc,
                            (proc.name, proc.state, proc.user, int(proc.cpu * 10), int(proc.rss / _TENTH_MB),
                             None if proc.pss is None else int(proc.pss / _TENTH_MB), proc.threads, proc.nice,
                             proc.args)))
        self.show_rows(rows, len(self.procs))
        if self._pending_pid is not None and self._pending_pid in self.procs:
            pid, self._pending_pid = self._pending_pid, None
            self.view.select_key(proc_key(self.procs[pid]))

    def context_menu(self) -> fluent.MenuFlyout | None:
        proc = self.selected_proc()
        if proc is None:
            return None
        window = self.window
        menu = fluent.MenuFlyout(self.view)
        menu.add_item("End task", self.end_selected, icon="window-close-symbolic", shortcut="Del")
        menu.add_item("End process tree", self.end_tree_selected)
        menu.addSeparator()
        priority = menu.add_submenu("Set priority")
        current = actions.priority_name(proc.nice)
        for name, nice in actions.PRIORITIES:
            priority.add_item(f"{name}  (nice {nice})", lambda nice=nice: window.set_priority(proc, nice),
                              checkable=True, checked=name == current)
        signals = menu.add_submenu("Send signal")
        for name, number in actions.SIGNALS:
            signals.add_item(name, lambda number=number: window.send_signal(proc, number))
        menu.addSeparator()
        menu.add_item("Open file location", lambda: window.open_location(proc), icon="folder-symbolic")
        menu.add_item("Copy command line", self._copy_command, icon="edit-copy-symbolic")
        menu.add_item("Properties", lambda: window.show_properties(proc), shortcut="Alt+Return")
        return menu


# --- Users -----------------------------------------------------------------------------------------------

class SessionRow:
    """A session with the processes counted to it."""

    def __init__(self, session: Session, procs: list[Proc]) -> None:
        self.session = session
        self.procs = procs
        self.cpu = min(100.0, sum(proc.cpu for proc in procs))
        self.mem = sum(proc.mem for proc in procs)

    @property
    def title(self) -> str:
        return f"{self.session.user} ({len(self.procs)})"


def _user_text(session_text, proc_text):
    return lambda row: session_text(row.data) if isinstance(row.data, SessionRow) else proc_text(row.data)


class UsersPage(ProcessTablePage):
    key = "users"
    search_hint = "Filter by user or process name"
    SESSIONS_EVERY = 5.0             # seconds between two questions to logind

    def __init__(self, window) -> None:
        number = dict(right=True, descending_first=True)
        columns = [
            Column("name", "User", _user_text(lambda s: s.title, lambda p: p.name), width=280,
                   icon=lambda row: fluent.icon("avatar-default-symbolic") if isinstance(row.data, SessionRow)
                   else fluent.icon("application-x-executable")),
            Column("id", "ID", _user_text(lambda s: s.session.id or "—", lambda p: str(p.pid)),
                   lambda row: row.data.pid if isinstance(row.data, Proc) else row.data.session.id, right=True, width=64),
            Column("status", "Status", _user_text(lambda s: s.session.state_title,
                                                  lambda p: grouping.status_text(p.state)), width=120),
            Column("place", "Seat / terminal", _user_text(lambda s: s.session.place, lambda p: procfs.tty_name(p.tty)),
                   width=140),
            Column("since", "Since", _user_text(lambda s: fmt.moment(s.session.since, self._now),
                                                lambda p: fmt.moment(p.start, self._now)),
                   lambda row: row.data.start if isinstance(row.data, Proc) else row.data.session.since, right=True,
                   width=136),
            Column("cpu", "CPU", lambda row: fmt.percent(row.data.cpu), lambda row: row.data.cpu, width=76,
                   heat=lambda row: self.cpu_heat(row), **number),
            Column("memory", "Memory", lambda row: fmt.megabytes(row.data.mem), lambda row: row.data.mem, width=110,
                   heat=lambda row: self.mem_heat(row), **number),
        ]
        super().__init__(window, columns, tree=True, stretch="name")
        self._now: float | None = None
        self.sessions: list[Session] | None = None           # None: logind was not asked yet or cannot be
        self.logind_error = ""
        self._asked_at = -1e9
        self.note = fluent.Label("", "caption", tone="secondary", wrap=True)
        self.note.hide()
        self.layout().addWidget(self.note)
        bar = self.bar
        self.end_action = bar.add_command("End task", "window-close-symbolic", self.end_selected,
                                          tooltip="End the selected process (Delete)")
        bar.add_command("Expand all", None, self.view.expandAll, primary=False)
        bar.add_command("Collapse all", None, self.view.collapseAll, primary=False)
        self.sync_commands()

    def parts(self) -> frozenset[str]:
        return frozenset({"procs", "memory"})

    def selected_procs(self) -> tuple[str, list[Proc]]:
        row = self.view.current_row()
        if row is not None and isinstance(row.data, Proc):
            return row.data.name, [row.data]
        return "", []

    def sync_commands(self) -> None:
        self.end_action.setEnabled(self.selected_proc() is not None)

    def set_filter(self, text: str) -> None:
        super().set_filter(text)
        self.rebuild()

    def activated(self) -> None:
        self._asked_at = -1e9

    def refresh(self) -> None:
        self._asked_at = -1e9
        super().refresh()

    def _ask_logind(self, now: float) -> None:
        if now - self._asked_at < self.SESSIONS_EVERY:
            return
        self._asked_at = now
        self.window.call(self.window.backend.list_sessions, done=self._sessions, failed=self._no_sessions,
                         key="sessions")

    def _sessions(self, sessions: list[Session]) -> None:
        from ..services import user_sessions
        self.sessions = user_sessions(sessions)
        self.logind_error = ""
        self.rebuild()

    def _no_sessions(self, error: Exception) -> None:
        self.sessions = None
        self.logind_error = str(error)
        self.rebuild()

    def apply(self, sample: Sample) -> None:
        if sample.memory is not None:
            self.memory_total = sample.memory.total
            self.model.set_metric("memory", fmt.percent(sample.memory.percent, 0))
        if sample.total_cpu is not None:
            self.model.set_metric("cpu", fmt.percent(sample.total_cpu, 0))
        if sample.procs is None:
            return
        self._now = sample.wall
        self.procs = sample.procs
        self._ask_logind(sample.time)
        self.rebuild()

    def rebuild(self) -> None:
        backend = self.window.backend
        if self.sessions:
            sessions = self.sessions
            self.note.hide()
        else:
            sessions = [grouping.fallback_session(self.procs, backend.uid, backend.user_name(), backend.pid)]
            if self.logind_error:
                self.note.setText("The login service (logind) could not be asked, so only your own processes are "
                                  f"shown, as one session. {self.logind_error}")
                self.note.show()
            elif self.sessions is not None:
                self.note.setText("The login service knows no session of a user; your own processes are shown.")
                self.note.show()
        assigned = grouping.assign_sessions(self.procs, sessions)
        needle = self.filter_text
        rows = []
        for session in sessions:
            procs = assigned.get(session.id, [])
            if needle and needle not in session.user.casefold():
                procs = [proc for proc in procs if matches(needle, proc)]
                if not procs:
                    continue
            data = SessionRow(session, procs)
            children = [Row(proc_key(proc), proc, (proc.name, proc.state, int(proc.cpu * 10), int(proc.mem / _TENTH_MB),
                                                  proc.tty))
                        for proc in procs]
            rows.append(Row(session.key, data, (data.title, session.state, session.place, session.since,
                                                int(data.cpu * 10), int(data.mem / _TENTH_MB)), children))
        self.show_rows(rows, len(sessions))

    def context_menu(self) -> fluent.MenuFlyout | None:
        row = self.view.current_row()
        if row is None:
            return None
        menu = fluent.MenuFlyout(self.view)
        index = self.view.currentIndex().siblingAtColumn(0)
        if isinstance(row.data, SessionRow):
            expanded = self.view.isExpanded(index)
            menu.add_item("Collapse" if expanded else "Expand", lambda: self.view.setExpanded(index, not expanded))
            return menu
        self.add_process_items(menu, row.data, row.data.name, details=True)
        return menu

