"""The main window of Task Manager."""

from __future__ import annotations

import signal
from typing import Callable

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

import boswas_fluent as fluent

from .. import APP_ID, DISPLAY_NAME, PAGE_TITLES, PAGES, actions
from ..desktopfile import AppIndex
from ..history import History
from ..sampler import Proc, Sample
from .backend import Backend
from .dialogs import PropertiesDialog, RunDialog
from .pages import DetailsPage, Page, ProcessesPage, UsersPage
from .performance import PerformancePage
from .sampling import Sampling
from .services_page import ServicesPage
from .settings_page import SettingsPage
from .startup_page import StartupPage

_PAGES = (
    ("processes", ProcessesPage, "view-app-grid-symbolic"),
    ("performance", PerformancePage, "power-profile-performance-symbolic"),
    ("startup", StartupPage, "media-playback-start-symbolic"),
    ("users", UsersPage, "avatar-default-symbolic"),
    ("details", DetailsPage, "view-list-symbolic"),
    ("services", ServicesPage, "view-task-symbolic"),
    ("settings", SettingsPage, "preferences-system-symbolic"),
)


def page_argument(arguments: list[str]) -> str | None:
    """The page asked for on the command line (``--page NAME`` or ``--page=NAME``), if it is one."""
    wanted = None
    for index, argument in enumerate(arguments):
        if argument == "--page" and index + 1 < len(arguments):
            wanted = arguments[index + 1]
        elif argument.startswith("--page="):
            wanted = argument[7:]
    return wanted if wanted in PAGES else None


class TaskManagerWindow(fluent.FluentWindow):
    END_GRACE_MS = 3000              # how long a task gets to end before "Force" is offered

    def __init__(self, arguments: list[str] | None = None, backend: Backend | None = None) -> None:
        self.backend = backend or Backend()
        self.settings = self.backend.load_settings()
        super().__init__(APP_ID, DISPLAY_NAME,
                         navigation={"pane_toggle": True, "pane_width": 232, "compact_below": 900},
                         icon="utilities-system-monitor", size=(1180, 760), minimum_size=(640, 440))
        self.worker = fluent.Worker(self)
        self.history = History()
        self.dialog: fluent.ContentDialog | None = None
        self.last_sample: Sample | None = None
        self.procs: dict[int, Proc] = {}
        self._banner_callback: Callable[[], None] | None = None
        self._ending: list[tuple[int, int, str]] = []
        self._ending_title = ""
        self._theme = "desktop"
        self._closed = False

        self.sampling = Sampling(self.backend.make_sampler, self.backend.threaded, self)
        self.sampling.sampled.connect(self._sample)

        nav = self.navigation
        nav.title_label.set_style("subtitle")
        self.banner = nav.add_banner(fluent.InfoBar("info"))
        self.banner.hide()
        self.banner.action_triggered.connect(self._banner_action)
        self._banner_timer = QTimer(self)
        self._banner_timer.setSingleShot(True)
        self._banner_timer.setInterval(8000)
        self._banner_timer.timeout.connect(self.banner.hide)

        tools = QWidget()
        row = QHBoxLayout(tools)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.search = fluent.SearchBox(placeholder="Filter")
        self.search.setMinimumWidth(200)
        self.search.setMaximumWidth(300)
        self.search.textChanged.connect(self._filter_changed)
        row.addWidget(self.search, 1)
        self.pages: dict[str, Page] = {}
        for key, cls, icon in _PAGES:
            page = cls(self)
            self.pages[key] = page
            nav.add_page(key, PAGE_TITLES[key], page, icon=icon, footer=key == "settings",
                         tooltip=PAGE_TITLES[key])
            page.bar.hide()
            row.addWidget(page.bar)
        nav.add_header_widget(tools)

        self.add_shortcut("find", self.focus_search)
        self.add_shortcut("refresh", self.refresh)
        self.add_shortcut("Ctrl+N", self.run_new_task)
        self.add_shortcut("Alt+N", self.run_new_task)
        self.add_shortcut("Ctrl+Tab", lambda: self.step_page(1))
        self.add_shortcut("Ctrl+Shift+Tab", lambda: self.step_page(-1))
        self.add_shortcut("copy", self.copy_selection)
        self.add_shortcut("properties", self.properties_of_selection)
        self.add_shortcut("close", self.close)
        self.add_shortcut("quit", self.close)

        self._grace = QTimer(self)
        self._grace.setSingleShot(True)
        self._grace.timeout.connect(self.check_ended)
        self._reaper = QTimer(self)
        self._reaper.setInterval(5000)
        self._reaper.timeout.connect(self.backend.collect_children)

        self.history.interval = self.settings.interval() or 1.0
        self._apply_theme()
        start = page_argument(arguments or []) or self.settings.start_page
        if nav.current() != start:
            nav.set_current(start)
        nav.current_changed.connect(self._page_changed)
        self._page_changed(start)
        self.sampling.start()
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.shut_down)
        self.reload_applications()

    # --- pages ---
    def current_page(self) -> Page:
        return self.pages[self.navigation.current() or "processes"]

    def show_page(self, key: str) -> Page:
        if self.navigation.current() != key:
            self.navigation.set_current(key)
        return self.pages[key]

    def step_page(self, step: int) -> None:
        keys = list(PAGES)
        current = self.navigation.current() or keys[0]
        self.show_page(keys[(keys.index(current) + step) % len(keys)])

    def handle_arguments(self, arguments: list[str]) -> None:
        """Another start of the program passed its arguments on."""
        wanted = page_argument(arguments)
        if wanted:
            self.show_page(wanted)

    def _page_changed(self, key: str) -> None:
        for name, page in self.pages.items():
            page.bar.setVisible(name == key)
            if name != key and getattr(page, "_shown", False):
                page._shown = False
                page.deactivated()
        page = self.pages[key]
        page._shown = True
        if self.banner.severity() in ("info", "success") and self.banner.action_button is None:
            self.banner.hide()                       # a result that belonged to the page that was left
        self.search.setVisible(page.searchable)
        self.search.setPlaceholderText(page.search_hint)
        self.search.setAccessibleName(page.search_hint)
        if page.searchable:
            page.set_filter(self.search.text())
        self.setWindowTitle(f"{PAGE_TITLES[key]} — {DISPLAY_NAME}")
        page.activated()
        self._update_sampling()

    def _filter_changed(self, text: str) -> None:
        page = self.current_page()
        if page.searchable:
            page.set_filter(text)

    def focus_search(self) -> None:
        if self.current_page().searchable:
            self.search.setFocus()
            self.search.selectAll()

    def refresh(self) -> None:
        page = self.current_page()
        if page.key == "processes":
            self.reload_applications()
        page.refresh()

    # --- sampling ---
    def _update_sampling(self) -> None:
        parts = self.current_page().parts()
        visible = self.isVisible() and not self.isMinimized()
        # A page that shows nothing from the sampler (Startup apps, Services, Settings) stops it too.
        self.sampling.configure(parts, self.settings.interval(), visible and bool(parts))

    def _sample(self, sample: Sample) -> None:
        try:
            if self._closed:
                return
            self.last_sample = sample
            if sample.procs is not None:
                self.procs = sample.procs
            self.history.push(sample)
            self.current_page().apply(sample)
        finally:
            self.sampling.done()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_sampling()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._update_sampling()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "sampling"):
            self._update_sampling()

    def closeEvent(self, event) -> None:
        self.shut_down()
        super().closeEvent(event)

    def shut_down(self) -> None:
        """Stop the sampler thread and the worker (when the window closes or the application quits)."""
        if not self._closed:
            self._closed = True
            self.sampling.stop()
            self.worker.shutdown()

    # --- settings ---
    def save_settings(self) -> None:
        try:
            self.backend.save_settings(self.settings)
        except OSError as error:
            self.notify("error", f"The settings could not be saved: {error.strerror or error}.")

    def settings_changed(self) -> None:
        self.save_settings()
        self.history.interval = self.settings.interval() or 1.0
        self._apply_theme()
        for page in self.pages.values():
            if page.key != "settings":
                page.settings_changed()
        self._update_sampling()

    def _apply_theme(self) -> None:
        if self.settings.theme != self._theme:
            self._theme = self.settings.theme
            fluent.manager().set_scheme(None if self._theme == "desktop" else self._theme)

    # --- messages ---
    def notify(self, severity: str, text: str, action: str | None = None,
               callback: Callable[[], None] | None = None) -> None:
        """Show a message above the page; ``action`` adds a button that calls ``callback``."""
        self._banner_callback = callback
        if self.banner.action_button is not None:
            self.banner.action_button.hide()         # set_action deletes it later; it must not show meanwhile
        self.banner.set_action(action)
        self.banner.show_message(severity, text)
        self._banner_timer.stop()
        if severity in ("info", "success") and action is None and self.backend.threaded:
            self._banner_timer.start()

    def _banner_action(self) -> None:
        callback, self._banner_callback = self._banner_callback, None
        self.banner.hide()
        if callback is not None:
            callback()

    def call(self, function: Callable, *arguments, done: Callable | None = None, failed: Callable | None = None,
             key: str | None = None) -> None:
        """Run a blocking call off the GUI thread; ``done(result)`` or ``failed(exception)`` run on it."""
        failed = failed or (lambda error: self.notify("error", str(error)))
        if self.backend.threaded:
            self.worker.submit(function, *arguments, on_done=done, on_error=failed, key=key)
            return
        try:
            result = function(*arguments)
        except Exception as error:               # delivered like the worker would
            failed(error)
            return
        if done is not None:
            done(result)

    def open_dialog(self, dialog: fluent.ContentDialog, finished: Callable | None = None) -> None:
        if self.dialog is not None:
            self.dialog.reject()
        self.dialog = dialog

        def closed(result) -> None:
            if self.dialog is dialog:
                self.dialog = None
            if finished is not None:
                finished(result)
            dialog.deleteLater()

        dialog.finished.connect(closed)
        dialog.open()

    # --- applications (for the Apps group) ---
    def reload_applications(self) -> None:
        self.call(self.backend.list_applications, done=lambda apps: self.applications_loaded(AppIndex(apps)),
                  failed=lambda _error: None, key="applications")

    def applications_loaded(self, index: AppIndex) -> None:
        self.pages["processes"].set_applications(index)

    # --- what can be done to a process ---
    def run_new_task(self) -> None:
        def start(arguments: list[str], directory: str) -> None:
            self.backend.start_program(arguments, directory)
            self._reaper.start()
            self.notify("success", f"{arguments[0]} was started.")

        self.open_dialog(RunDialog(self, start, self.backend.home()))

    def end_task(self, procs: list[Proc], title: str, tree: bool = False) -> None:
        """Ask the processes to end (SIGTERM). After a few seconds, offer to force what is still there."""
        if not procs:
            return
        own = {self.backend.pid, *actions.ancestors(self.procs, self.backend.pid)}
        if any(proc.pid in own for proc in procs):
            if not self.ask("End this task?",
                            f"“{title}” started Task Manager, or is Task Manager itself. Ending it closes this "
                            "window and may end your whole desktop session; unsaved work in the programs that go "
                            "with it is lost.", "End task"):
                return
        elif any(proc.pid == 1 for proc in procs):
            if not self.ask("End this task?", "This is the first process of the system; everything else depends "
                                              "on it.", "End task"):
                return
        sent, errors = [], []
        for proc in reversed(procs) if tree else procs:           # the children first
            try:
                self.backend.send_signal(proc.pid, signal.SIGTERM, proc.start_ticks, proc.name)
                sent.append((proc.pid, proc.start_ticks, proc.name))
            except actions.ActionError as error:
                errors.append(str(error))
        if errors:
            more = f" ({len(errors) - 1} more could not be ended either.)" if len(errors) > 1 else ""
            self.notify("error", errors[0] + more)
        elif self.banner.severity() == "warning":
            self.banner.hide()
        if sent:
            self._ending, self._ending_title = sent, title if not tree else f"{title} and what it started"
            self._grace.start(self.END_GRACE_MS)

    def check_ended(self) -> None:
        """Called a few seconds after "End task": offer "Force" for what did not end."""
        left = [(pid, start, name) for pid, start, name in self._ending
                if self.backend.condition(pid, start) == "running"]
        self._ending = []
        if not left:
            return
        what = self._ending_title if len(left) > 1 else left[0][2]
        self.notify("warning", f"{what} did not end when asked to. Forcing it ends it at once; it cannot save "
                               "anything first.", "Force", lambda: self.force_end(left))

    def force_end(self, targets: list[tuple[int, int, str]]) -> None:
        errors = []
        for pid, start, name in targets:
            try:
                self.backend.send_signal(pid, signal.SIGKILL, start, name)
            except actions.ActionError as error:
                errors.append(str(error))
        if errors:
            self.notify("error", errors[0])

    def send_signal(self, proc: Proc, signum: int) -> None:
        try:
            self.backend.send_signal(proc.pid, signum, proc.start_ticks, proc.name)
        except actions.ActionError as error:
            self.notify("error", str(error))
            return
        name = signal.Signals(signum).name
        self.notify("success", f"{name} was sent to {proc.name} (PID {proc.pid}).")

    def set_priority(self, proc: Proc, nice: int) -> None:
        try:
            self.backend.set_priority(proc.pid, nice, proc.start_ticks, proc.name)
        except actions.ActionError as error:
            self.notify("error", str(error))
            return
        self.notify("success", f"{proc.name} (PID {proc.pid}) now has the priority "
                               f"“{actions.priority_name(nice)}” (nice {nice}).")
        self.sampling.request()

    def open_location(self, proc: Proc) -> None:
        try:
            self.show_file(actions.file_location(proc.exe), checked=True)
        except actions.ActionError as error:
            self.notify("error", str(error))

    def show_file(self, path: str, checked: bool = False) -> None:
        """Show ``path`` selected in the file manager."""
        try:
            if not checked:
                path = actions.file_location(path)
            self.backend.show_in_files(path)
            self._reaper.start()
        except actions.ActionError as error:
            self.notify("error", str(error))

    def show_properties(self, proc: Proc) -> None:
        self.call(self.backend.read_properties, proc.pid,
                  done=lambda found: self.open_dialog(PropertiesDialog(
                      self, self.procs.get(proc.pid, proc), found,
                      self.last_sample.wall if self.last_sample is not None else None)))

    def properties_of_selection(self) -> None:
        page = self.current_page()
        proc = page.selected_proc() if hasattr(page, "selected_proc") else None
        if proc is not None:
            self.show_properties(proc)

    def go_to_details(self, proc: Proc) -> None:
        self.go_to_pid(proc.pid)

    def go_to_pid(self, pid: int) -> None:
        self.search.clear()
        page = self.show_page("details")
        page.select_pid(pid)
        page.view.setFocus()

    def copy_text(self, text: str, message: str = "") -> None:
        self.backend.copy_text(text)
        if message:
            self.notify("success", message)

    def copy_selection(self) -> None:
        text = self.current_page().copy_selection()
        if text:
            self.backend.copy_text(text)
