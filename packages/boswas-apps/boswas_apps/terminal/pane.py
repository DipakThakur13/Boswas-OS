"""TerminalPane: the content of one tab: a terminal view, its child process, its search bar and messages.

The pane starts the program of its profile, hands the program's output to
the view in slices so the window stays responsive during a flood of output,
and decides what happens when the program ends.
"""

from __future__ import annotations

import os
import signal
import time
from collections import deque
from typing import Callable
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QVBoxLayout, QWidget

import boswas_fluent as fluent

from . import keys, ptyhost
from . import settings as settings_module
from .findbar import FindBar
from .session import PtySession, Session
from .view import TerminalView

SLICE_BYTES = 16384              # bytes handed to the emulator at a time
PUMP_SECONDS = 0.008             # time spent on output before events get their turn again
HIGH_WATER = 1 << 20             # bytes waiting: stop reading from the child
LOW_WATER = 1 << 18

SessionFactory = Callable[[settings_module.Launch, int, int, int, int], Session]


def start_session(launch: settings_module.Launch, cols: int, rows: int, width: int, height: int) -> Session:
    """The real thing: the program on a pseudo-terminal."""
    environ = ptyhost.child_environment(shell=ptyhost.default_shell())
    return PtySession(list(launch.argv), program=launch.program, cwd=launch.directory, environ=environ,
                      cols=cols, rows=rows, width=width, height=height)


def signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


class TerminalPane(QWidget):
    changed = Signal()                   # the title, the marks or the colours of the tab changed
    close_requested = Signal()
    settings_requested = Signal()

    def __init__(self, store, profile_id: str, *, directory: str = "", fallback_directory: str = "",
                 command: list[str] | None = None, title: str = "", session_factory: SessionFactory | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.profile_id = store.settings.profile(profile_id).id
        self.directory = directory
        self.fallback_directory = fallback_directory
        self.command = list(command) if command else None
        self.fixed_title = title
        self.session_factory = session_factory or start_session
        self.session: Session | None = None
        self.launch: settings_module.Launch | None = None
        self.state = "new"               # new, running, ended, failed
        self.exit_status: int | None = None
        self.indicator = ""              # "", "activity", "bell": what happened while the tab was not shown
        self.active = False              # the tab is the one on screen
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0
        self._offset = 0
        self._pumping = False
        self._paused = False
        self._told_clipboard = False
        self._closed = False
        self.confirm_paste: Callable[[str], bool] = self._ask_paste
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.info = fluent.InfoBar("info", parent=self)
        self.info.hide()
        self.info.action_triggered.connect(self.settings_requested)
        layout.addWidget(self.info)
        profile = self.profile()
        self.view = TerminalView(self, scrollback=profile.scrollback)
        layout.addWidget(self.view, 1)
        self.find_bar = FindBar(self.view, self)
        self.setFocusProxy(self.view)
        view = self.view
        view.configure(profile, store.settings)
        view.paste_guard = self._may_paste
        view.send.connect(self._send)
        view.grid_resized.connect(self._grid_resized)
        view.title_changed.connect(lambda _title: self.changed.emit())
        view.bell_rang.connect(self._bell)
        view.colours_changed.connect(self.changed)
        view.content_changed.connect(self._content_changed)
        view.menu_requested.connect(self._menu)
        view.link_activated.connect(self.open_link)
        view.clipboard_denied.connect(self._clipboard_denied)
        store.changed.connect(self.apply_settings)
        self._pump_timer = QTimer(self)
        self._pump_timer.setSingleShot(True)
        self._pump_timer.setInterval(0)
        self._pump_timer.timeout.connect(self._pump)

    # --- settings ---
    def profile(self) -> settings_module.Profile:
        return self.store.settings.profile(self.profile_id)

    def apply_settings(self) -> None:
        if not self._closed:
            self.view.configure(self.profile(), self.store.settings)
            self.changed.emit()

    # --- the program ---
    def ensure_started(self) -> None:
        if self.state == "new":
            self.start()

    def start(self) -> None:
        """Start the program of this tab (again)."""
        if self.session is not None:
            self.session.close()
            self.session = None
        view = self.view
        cols, rows = view.grid_size()
        cell_w, cell_h = view.cell_size()
        try:
            self.launch = settings_module.launch_for(self.profile(), directory=self.directory,
                                                     fallback_directory=self.fallback_directory,
                                                     command=self.command)
            session = self.session_factory(self.launch, cols, rows, cols * cell_w, rows * cell_h)
        except ptyhost.SpawnError as error:
            self.state = "failed"
            view.write_text(f"[{error}]\n")
            self.changed.emit()
            return
        self.session = session
        session.setParent(self)
        session.output.connect(self._output)
        session.finished.connect(self._finished)
        self.state = "running"
        self.exit_status = None
        self.changed.emit()

    def _send(self, data: bytes) -> None:
        if self.state == "running" and self.session is not None:
            self.session.write(data)
        elif self.state in ("ended", "failed"):
            if data in (b"\r", b"\r\n", b"\x1bOM"):
                self.start()
            elif data == b"\x04":
                self.close_requested.emit()

    def _grid_resized(self, cols: int, rows: int) -> None:
        if self.session is not None and self.state == "running":
            cell_w, cell_h = self.view.cell_size()
            self.session.resize(cols, rows, cols * cell_w, rows * cell_h)

    def _output(self, data: bytes) -> None:
        self._pending.append(data)
        self._pending_bytes += len(data)
        if self._pending_bytes > HIGH_WATER and not self._paused and self.session is not None:
            self._paused = True
            self.session.set_reading(False)
        if not self._pump_timer.isActive() and not self._pumping:
            self._pump()

    def _pump(self, everything: bool = False) -> None:
        """Give waiting output to the emulator, for a short time (or all of it)."""
        if self._pumping:
            return
        self._pumping = True
        try:
            deadline = time.monotonic() + PUMP_SECONDS
            pending = self._pending
            feed = self.view.feed
            while pending:
                chunk = pending[0]
                offset = self._offset
                if len(chunk) - offset <= SLICE_BYTES:
                    piece = chunk[offset:] if offset else chunk
                    pending.popleft()
                    self._offset = 0
                else:
                    piece = chunk[offset:offset + SLICE_BYTES]
                    self._offset = offset + SLICE_BYTES
                self._pending_bytes -= len(piece)
                feed(piece)
                if not everything and time.monotonic() > deadline:
                    break
        finally:
            self._pumping = False
        if self._pending:
            self._pump_timer.start()
        elif self._paused and self.session is not None:
            self._paused = False
            self.session.set_reading(True)
        if self._paused and self._pending_bytes < LOW_WATER and self.session is not None:
            self._paused = False
            self.session.set_reading(True)

    def pending_bytes(self) -> int:
        return self._pending_bytes

    def _finished(self, status: int, number: int) -> None:
        self._pump(everything=True)
        self.state = "ended"
        self.exit_status = status
        policy = self.profile().close_on_exit
        if self.session is not None:
            self.session.close()
        if policy == "always" or (policy == "graceful" and status == 0 and not number):
            self.close_requested.emit()
            return
        if number:
            reason = f"[The program was ended by {signal_name(number)}.]"
        else:
            reason = f"[The program ended with exit code {status}.]"
        self.view.write_text(f"{reason}\nPress Enter to start it again, or Ctrl+D to close this tab.\n")
        self.changed.emit()

    def is_busy(self) -> str | None:
        """The name of a program that runs in the foreground of this terminal (not the shell itself), or None."""
        if self.state != "running" or self.session is None:
            return None
        return self.session.foreground_process()

    def current_directory(self) -> str:
        """Where the program of this tab is: what it reported (OSC 7), else what /proc says, else ''."""
        reported = self.view.term.cwd
        if reported and os.path.isdir(reported):
            return reported
        if self.session is not None and self.state == "running":
            return self.session.working_directory() or ""
        return ""

    def shutdown(self) -> None:
        """The tab is going away: hang up on the program."""
        self._closed = True
        self._pump_timer.stop()
        self._pending.clear()
        self.find_bar.close_bar()
        try:
            self.store.changed.disconnect(self.apply_settings)
        except (RuntimeError, TypeError):
            pass
        if self.session is not None:
            self.session.close()
            self.session = None
        self.state = "ended"

    # --- the tab ---
    def title(self) -> str:
        if self.fixed_title:
            return self.fixed_title
        reported = self.view.term.title
        if reported:
            return reported
        if self.launch is not None and self.command:
            return self.launch.name
        return self.profile().name

    def set_fixed_title(self, title: str) -> None:
        self.fixed_title = title.strip()[:256]
        self.changed.emit()

    def set_active(self, active: bool) -> None:
        self.active = active
        if active and self.indicator:
            self.indicator = ""
            self.changed.emit()

    def _bell(self) -> None:
        if not self.active and self.indicator != "bell":
            self.indicator = "bell"
            self.changed.emit()

    def _content_changed(self) -> None:
        if not self.active and not self.indicator:
            self.indicator = "activity"
            self.changed.emit()

    # --- search ---
    def show_find(self) -> None:
        self._place_find()
        self.find_bar.open_bar()

    def _place_find(self) -> None:
        bar = self.find_bar
        size = bar.sizeHint()
        width = min(size.width(), max(120, self.width() - 32))
        top = self.view.geometry().top() + 8
        bar.setGeometry(self.width() - width - 24, top, width, size.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.find_bar.isVisible():
            self._place_find()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self.ensure_started)

    # --- paste ---
    def _may_paste(self, text: str) -> bool:
        if self.state != "running":
            return False
        if self.store.settings.paste_warning and keys.has_several_lines(text):
            return self.confirm_paste(text)
        return True

    def _ask_paste(self, text: str) -> bool:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").count("\n") + 1
        dialog = fluent.ContentDialog(
            self, "Paste text with several lines?",
            f"The text has {lines} lines. In a shell every line break runs what is before it as a command.",
            primary="Paste", close="Cancel", default="close")
        again = QCheckBox("Do not ask again", dialog)
        dialog.set_content(again)
        try:
            accepted = dialog.exec() == fluent.ContentDialog.Result.PRIMARY
            if accepted and again.isChecked():
                self.store.settings.paste_warning = False
                self.store.update()
            return accepted
        finally:
            dialog.deleteLater()
            self.view.setFocus(Qt.FocusReason.OtherFocusReason)

    def _clipboard_denied(self) -> None:
        if not self._told_clipboard:
            self._told_clipboard = True
            self.info.show_message("info", "A program in this tab tried to put text on the clipboard. "
                                   "That is turned off; you can allow it in the settings.")
            self.info.set_action("Settings")

    # --- links ---
    def open_link(self, url: str) -> None:
        """Open an address the user clicked."""
        scheme = url.partition(":")[0].lower()
        opened = False
        if scheme in ("http", "https", "mailto"):
            opened = fluent.open_url(url)
        elif scheme == "file":
            try:
                parts = urlsplit(url)
            except ValueError:
                parts = None
            if parts is not None and parts.netloc in ("", "localhost", self.view.term.hostname):
                opened = fluent.open_path(unquote(parts.path))
        if not opened:
            shown = url if len(url) <= 120 else url[:117] + "…"
            self.info.set_action(None)
            self.info.show_message("warning", f"This address could not be opened: {shown}")

    def _menu(self, position, link: str) -> None:
        menu = fluent.MenuFlyout(self)
        view = self.view
        if link:
            menu.add_item("Open link", lambda: self.open_link(link), icon="document-open-symbolic")
            menu.add_item("Copy link address", lambda: self._copy_text(link), icon="edit-copy-symbolic")
            menu.addSeparator()
        menu.add_item("Copy", view.copy, icon="edit-copy-symbolic", shortcut="Ctrl+Shift+C",
                      enabled=view.has_selection())
        menu.add_item("Paste", view.paste, icon="edit-paste-symbolic", shortcut="Ctrl+Shift+V",
                      enabled=self.state == "running")
        menu.add_item("Select all", view.select_all, shortcut="Ctrl+Shift+A")
        menu.addSeparator()
        menu.add_item("Find", self.show_find, icon="edit-find-symbolic", shortcut="Ctrl+Shift+F")
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.context_menu = menu
        menu.popup(position)

    @staticmethod
    def _copy_text(text: str) -> None:
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(text)
