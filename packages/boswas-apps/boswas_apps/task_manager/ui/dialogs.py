"""The dialogs of Task Manager: Run new task, Properties, Add startup app.

All three are ContentDialogs inside the window, opened without blocking
(``open()``); the window keeps the open one in ``window.dialog``.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QFormLayout, QFrame, QGridLayout, QLineEdit, QListWidget, QListWidgetItem, QScrollArea,
                               QVBoxLayout, QWidget)

import boswas_fluent as fluent

from .. import actions, procfs
from .. import textutil as fmt
from ..desktopfile import Application
from ..sampler import Proc


def app_icon(name: str):
    """The icon of a desktop entry: a name from the icon theme or an image file; the generic program icon
    when there is none."""
    import os
    from PySide6.QtGui import QIcon
    if name.startswith("/"):
        if name.lower().endswith((".png", ".svg", ".xpm")) and os.path.isfile(name) and os.path.getsize(name) < 4 << 20:
            icon = QIcon(name)
            if not icon.isNull():
                return icon
        name = ""
    return fluent.icon(name, "application-x-executable") if name else fluent.icon("application-x-executable")


class RunDialog(fluent.ContentDialog):
    """Start a program: a command line that is split like a shell would, but run without one."""

    def __init__(self, parent: QWidget, start: Callable[[list[str], str], None], home: str) -> None:
        super().__init__(parent, "Run new task",
                         "Type the name of a program and its arguments. The program is started directly, not by "
                         "a shell: quotes group words, and nothing else has a special meaning.",
                         primary="Run", close="Cancel")
        self._start = start
        self._home = home
        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.command = QLineEdit()
        self.command.setPlaceholderText("boswas-terminal")
        self.command.setAccessibleName("Open")
        self.command.setClearButtonEnabled(True)
        self.directory = QLineEdit()
        self.directory.setText("~")
        self.directory.setAccessibleName("Working folder")
        form.addRow(fluent.Label("Open:"), self.command)
        form.addRow(fluent.Label("Folder:"), self.directory)
        self.content_layout.addLayout(form)
        # The message has its place from the start, so that the dialog need not grow when one appears.
        place = QWidget()
        place.setMinimumHeight(64)
        holder = QVBoxLayout(place)
        holder.setContentsMargins(0, 0, 0, 0)
        self.error = fluent.InfoBar("error", closable=False)
        self.error.hide()
        holder.addWidget(self.error)
        holder.addStretch(1)
        self.content_layout.addWidget(place)
        self.command.returnPressed.connect(lambda: self.done(fluent.ContentDialog.Result.PRIMARY))
        self.directory.returnPressed.connect(lambda: self.done(fluent.ContentDialog.Result.PRIMARY))

    def open(self) -> None:
        super().open()
        self.command.setFocus(Qt.FocusReason.PopupFocusReason)

    def done(self, result) -> None:
        if result == fluent.ContentDialog.Result.PRIMARY and self.is_open():
            try:
                arguments = actions.split_command(self.command.text())
                directory = actions.resolve_directory(self.directory.text(), self._home)
                self._start(arguments, directory)
            except actions.ActionError as error:
                self.error.show_message("error", str(error))
                self.command.setFocus(Qt.FocusReason.OtherFocusReason)
                self.command.selectAll()
                return
        super().done(result)


class PropertiesDialog(fluent.ContentDialog):
    """What is known about one process. The environment is never shown."""

    def __init__(self, parent: QWidget, proc: Proc, found: actions.Properties, now: float | None = None) -> None:
        super().__init__(parent, f"{proc.name} properties", "", primary=None, close="Close", default="close")
        self.values: dict[str, str] = {}
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        unknown = "Not available: the system does not let you look into this process" if found.denied \
            else "Not available"
        exe = found.exe or proc.exe
        if exe and found.exe_deleted:
            exe += "  (replaced or removed since the program started)"
        rows = [
            ("Process ID", str(proc.pid)), ("Parent process ID", str(proc.ppid)), ("User", proc.user or "—"),
            ("Status", procfs.STATES.get(proc.state, proc.state)), ("Started", fmt.moment(proc.start, now)),
            ("Threads", str(proc.threads)),
            ("Priority", f"{actions.priority_name(proc.nice)} (nice {proc.nice})"),
            ("Memory", fmt.size(proc.rss) + (f" resident, {fmt.size(proc.pss)} proportional" if proc.pss is not None
                                              else " resident")),
            ("Executable", exe or ("None: a kernel thread" if proc.kthread else unknown)),
            ("Working folder", found.cwd or ("None: a kernel thread" if proc.kthread else unknown)),
            ("Open files", fmt.count(found.open_files) if found.open_files is not None else unknown),
            ("Command line", proc.cmdline),
        ]
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        for line, (label, value) in enumerate(rows):
            self.values[label] = value
            name = fluent.Label(label, "body", tone="secondary")
            text = fluent.Label(value, "body", wrap=True, selectable=True)
            text.setAccessibleName(label)
            grid.addWidget(name, line, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(text, line, 1)
        layout.addLayout(grid)
        if found.limits:
            layout.addWidget(fluent.Separator())
            layout.addWidget(fluent.Label("Limits", "body-strong"))
            limits = QGridLayout()
            limits.setContentsMargins(0, 0, 0, 0)
            limits.setHorizontalSpacing(16)
            limits.setVerticalSpacing(4)
            limits.setColumnStretch(0, 1)
            for column, title in enumerate(("Limit", "Soft", "Hard")):
                limits.addWidget(fluent.Label(title, "caption", tone="secondary"), 0, column)
            for line, limit in enumerate(found.limits, 1):
                def amount(value: str, units: str = limit.units) -> str:
                    return f"{value} {units}" if units and value.isdigit() else value

                title = limit.name[4:5].upper() + limit.name[5:]
                title = title.replace("Cpu", "CPU")
                self.values[limit.name] = f"{amount(limit.soft)} / {amount(limit.hard)}"
                limits.addWidget(fluent.Label(title, "body"), line, 0)
                limits.addWidget(fluent.Label(amount(limit.soft), "body"), line, 1)
                limits.addWidget(fluent.Label(amount(limit.hard), "body"), line, 2)
            layout.addLayout(limits)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        scroll.setMinimumHeight(320)
        scroll.setAccessibleName("Properties")
        self.set_content(scroll)


class AddStartupDialog(fluent.ContentDialog):
    """Choose one of the installed applications to start when the user signs in."""

    def __init__(self, parent: QWidget, applications: list[Application]) -> None:
        super().__init__(parent, "Add a startup app", "Choose an application to start every time you sign in.",
                         primary="Add", close="Cancel")
        self.search = fluent.SearchBox(placeholder="Search applications")
        self.content_layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setAccessibleName("Applications")
        self.list.setIconSize(QSize(24, 24))
        self.list.setMinimumHeight(280)
        self.list.setUniformItemSizes(True)
        for application in applications:
            if not application.visible:
                continue
            item = QListWidgetItem(app_icon(application.icon), application.name)
            item.setData(Qt.ItemDataRole.UserRole, application.path)
            item.setToolTip(application.comment or application.path)
            self.list.addItem(item)
        self.content_layout.addWidget(self.list)
        self.search.textChanged.connect(self._filter)
        self.list.currentRowChanged.connect(lambda _row: self._sync())
        self.list.itemDoubleClicked.connect(lambda _item: self.done(fluent.ContentDialog.Result.PRIMARY))
        self._sync()

    def _filter(self, text: str) -> None:
        wanted = text.strip().casefold()
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setHidden(bool(wanted) and wanted not in item.text().casefold())
        current = self.list.currentItem()
        if current is not None and current.isHidden():
            self.list.setCurrentRow(-1)
        self._sync()

    def _sync(self) -> None:
        if self.primary_button is not None:
            self.primary_button.setEnabled(self.chosen() is not None)

    def chosen(self) -> str | None:
        """The path of the desktop file of the chosen application."""
        item = self.list.currentItem()
        return None if item is None or item.isHidden() else item.data(Qt.ItemDataRole.UserRole)

    def open(self) -> None:
        super().open()
        self.search.setFocus(Qt.FocusReason.PopupFocusReason)
