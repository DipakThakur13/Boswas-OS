"""Main window: a navigation pane (Dashboard, Applications, Install, System Status) and the pages.

The window is a FluentWindow of the Boswas UI toolkit with a NavigationView;
the device and Control Plane state stand beside the page title, messages
appear as InfoBars between title and page, and questions are asked in a
ContentDialog inside the window.

State shown here is refreshed every 5 seconds from the session agent
(applications, activity) and the device agent (device and Control Plane
status); the catalog and the runtime check are slower and refreshed every
30 seconds. Every call runs on the Runner's thread pool. Either service may
be missing: without the device agent the window works normally and shows
the agent as not running; without the session agent a banner explains why
applications cannot be managed.
"""

from __future__ import annotations

import os

import boswas_fluent as fluent
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QWidget

from .. import viewmodel as vm
from ..backend import BackendRefused, BackendUnavailable
from .common import SP, MessageBanner, app_icon, dot_label, icon, plain_label, set_dot
from .dashboard import DashboardPage
from .details import DetailsDialog
from .install import InstallDialog, InstallFlowWidget
from .library import LibraryPage
from .remove import RemoveDialog
from .system import SystemPage
from .worker import Runner

APP_ID = "com.boswas.CompatibilityManager"
PAGES = (
    ("dashboard", "Dashboard", ("user-home", "go-home"), SP.SP_DirHomeIcon),
    ("applications", "Applications", ("boswas-windows-application", "applications-other"),
     SP.SP_FileDialogListView),
    ("install", "Install", ("boswas-windows-installer", "list-add"), SP.SP_FileDialogNewFolder),
    ("system", "System Status", ("computer", "dialog-information"), SP.SP_ComputerIcon),
)
HEADINGS = {"applications": "Windows Applications"}
REFRESH_MS = 5000
SLOW_EVERY = 6            # catalog and runtime: every 6th refresh (30 s)
JOB_POLL_MS = 500


class MainWindow(fluent.FluentWindow):
    def __init__(self, backend, *, runner: Runner | None = None, environ=None, refresh_ms: int = REFRESH_MS,
                 start: bool = True, parent: QWidget | None = None):
        super().__init__(APP_ID, "Boswas Compatibility Manager", parent, navigation={"compact_below": 860, "pane_width": 232},
                         icon=app_icon(), size=(1240, 760), minimum_size=(760, 520))
        self.backend = backend
        self.runner = runner or Runner(self)
        self.environ = os.environ if environ is None else environ
        self.apps: list[dict] = []
        self.manifests: list[dict] = []
        self.rows: list[vm.AppRow] = []
        self.system: dict | None = None
        self.agent: dict | None = None
        self.agent_checked = False
        self.events: list[dict] = []
        self.commands: list[dict] = []
        self.session_available: bool | None = None
        self.jobs: dict[str, tuple[str, str, str]] = {}       # job ID -> (operation, application ID, name)
        self.details: dict[str, DetailsDialog] = {}
        self.update_dialogs: dict[str, InstallDialog] = {}
        self._tick = 0
        # Modal interactions go through these attributes so tests can answer them.
        self.dialog_exec = lambda dialog: dialog.exec()
        self.ask = self._ask
        self.notify_error = self._notify_error

        self._build()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(refresh_ms)
        self.refresh_timer.timeout.connect(self.refresh)
        self.job_timer = QTimer(self)
        self.job_timer.setInterval(JOB_POLL_MS)
        self.job_timer.timeout.connect(self.poll_jobs)
        self.add_shortcut("refresh", lambda: self.refresh(full=True))
        self.add_shortcut("find", self._focus_search)
        self.add_shortcut("quit", self.close)
        if start:
            self.refresh(full=True)
            self.refresh_timer.start()

    # --- layout ---
    def _build(self) -> None:
        nav = self.navigation
        self.sidebar = nav.list
        self.sidebar.setIconSize(QSize(20, 20))
        self.sidebar.setAccessibleName("Sections")
        self.stack = nav.stack

        status = QWidget()
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)
        self.device_dot = dot_label("neutral", status)
        self.device_label = plain_label("", parent=status)
        self.plane_dot = dot_label("neutral", status)
        self.plane_label = plain_label("", parent=status)
        self.refresh_button = fluent.IconButton(icon("view-refresh", fallback=SP.SP_BrowserReload), "Refresh (F5)",
                                                status)
        self.refresh_button.setAccessibleName("Refresh")
        self.refresh_button.clicked.connect(lambda: self.refresh(full=True))
        status_layout.addWidget(self.device_dot)
        status_layout.addWidget(self.device_label)
        status_layout.addSpacing(12)
        status_layout.addWidget(self.plane_dot)
        status_layout.addWidget(self.plane_label)
        status_layout.addSpacing(8)
        status_layout.addWidget(self.refresh_button)
        nav.add_header_widget(status)

        self.session_banner = MessageBanner(closable=False)
        self.message = MessageBanner()
        nav.add_banner(self.session_banner)
        nav.add_banner(self.message)

        self.dashboard = DashboardPage()
        self.library = LibraryPage()
        self.install_page = InstallFlowWidget(self.backend, self.runner)
        self.install_page.setContentsMargins(21, 0, 21, 13)       # with the flow's own margins: the page margins
        self.system_page = SystemPage()
        self.pages = {"dashboard": self.dashboard, "applications": self.library, "install": self.install_page,
                      "system": self.system_page}
        for key, label, names, fallback in PAGES:
            nav.add_page(key, label, self.pages[key], icon=icon(*names, fallback=fallback),
                         heading=HEADINGS.get(key, label))

        nav.current_changed.connect(self._page_changed)
        self.dashboard.filter_requested.connect(self.show_filter)
        self.library.action_requested.connect(self.run_action)
        self.install_page.installed.connect(self._installed)
        self.install_page.launch_requested.connect(self.launch_app)
        self.install_page.show_log_requested.connect(lambda app_id, kind: self.open_details(app_id, "logs", kind))
        self.install_page.show_applications_requested.connect(lambda: self.show_page("applications"))
        self._update_header()
        self._update_status_rows()

    def show_page(self, key: str) -> None:
        if key not in self.pages:
            raise ValueError(f"unknown page {key!r}")
        self.navigation.set_current(key)

    def current_page(self) -> str:
        return self.navigation.current()

    def show_filter(self, key: str) -> None:
        self.library.set_filter(key)
        self.show_page("applications")

    def _page_changed(self, key: str) -> None:
        if key == "system":
            self._fetch_system()

    def _focus_search(self) -> None:
        self.show_page("applications")
        self.library.search.setFocus()
        self.library.search.selectAll()

    # --- refresh ---
    def refresh(self, full: bool = False) -> None:
        self._tick += 1
        submit = self.runner.submit
        submit(self.backend.list_apps, key="apps", on_done=self._apps_loaded, on_error=self._apps_failed)
        submit(self.backend.events, 50, key="events", on_done=self._events_loaded, on_error=self._events_failed)
        submit(self.backend.agent_status, key="agent", on_done=self._agent_loaded, on_error=self._agent_failed)
        submit(self.backend.remote_commands, 20, key="commands", on_done=self._commands_loaded,
               on_error=self._commands_failed)
        if full or self._tick % SLOW_EVERY == 0:
            submit(self.backend.catalog, key="catalog", on_done=self._catalog_loaded,
                   on_error=lambda _exc: None)     # without a catalog there is just no update information
            self._fetch_system()

    def _fetch_system(self) -> None:
        self.runner.submit(self.backend.system_status, key="system", on_done=self._system_loaded,
                           on_error=self._system_failed)

    def _apps_loaded(self, apps: list[dict]) -> None:
        self.set_session_available(True)
        self.apps = apps
        self._rebuild_rows()

    def _apps_failed(self, exc: Exception) -> None:
        if isinstance(exc, BackendUnavailable):
            self.set_session_available(False)
        else:
            self.show_message("error", f"The application list could not be loaded: {self._error_text(exc)}")

    def _catalog_loaded(self, doc: dict) -> None:
        manifests = doc.get("manifests")
        self.manifests = manifests if isinstance(manifests, list) else []
        self._rebuild_rows()

    def _events_loaded(self, events: list[dict]) -> None:
        self.events = events
        self._update_activity()

    def _events_failed(self, _exc: Exception) -> None:
        self.events = []
        self._update_activity()

    def _commands_loaded(self, commands: list[dict]) -> None:
        self.commands = commands
        self._update_activity()

    def _commands_failed(self, _exc: Exception) -> None:
        self.commands = []
        self._update_activity()

    def _agent_loaded(self, status: dict) -> None:
        self.agent, self.agent_checked = status, True
        self._update_header()
        self._update_status_rows()

    def _agent_failed(self, _exc: Exception) -> None:
        # Any failure means no usable device status; applications keep working through the session agent.
        self.agent, self.agent_checked = None, True
        self._update_header()
        self._update_status_rows()

    def _system_loaded(self, doc: dict) -> None:
        self.system = doc
        self._update_status_rows()
        for dialog in self.details.values():
            dialog.set_system(doc)

    def _system_failed(self, exc: Exception) -> None:
        self.system = None
        if isinstance(exc, BackendUnavailable):
            self.set_session_available(False)
        self._update_status_rows()

    # --- derived views ---
    def _rebuild_rows(self) -> None:
        self.rows = vm.app_rows(self.apps, self.manifests)
        self.library.set_rows(self.rows)
        self.dashboard.set_summary(vm.dashboard_summary(self.rows))

    def _update_activity(self) -> None:
        self.dashboard.set_activity(vm.activity_items(self.events, self.commands))

    def _update_header(self) -> None:
        header = vm.header_status(self.agent, self.agent_checked)
        self.device_label.setText(header.device)
        set_dot(self.device_dot, header.device_level)
        self.plane_label.setText(header.control_plane)
        set_dot(self.plane_dot, header.control_plane_level)

    def _update_status_rows(self) -> None:
        if not self.agent_checked and self.system is None:
            return                                   # keep "Checking…" until something is known
        rows = vm.system_rows(self.system, self.agent)
        self.system_page.set_rows(rows)
        self.dashboard.set_status_rows(rows)

    def set_session_available(self, available: bool) -> None:
        if available == self.session_available:
            return
        self.session_available = available
        if available:
            self.session_banner.hide()
        else:
            self.session_banner.show_message("error", vm.SESSION_UNAVAILABLE)
        self.library.setEnabled(available)
        self.install_page.setEnabled(available)

    # --- messages ---
    def show_message(self, level: str, text: str) -> None:
        self.message.show_message(level, text)

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, BackendUnavailable):
            return vm.SESSION_UNAVAILABLE
        if isinstance(exc, BackendRefused):
            return vm.error_text(exc.code, exc.message)
        return f"Unexpected error: {vm.one_line(exc, 300)}"

    def _notify_error(self, title: str, text: str) -> None:
        fluent.message(self, title, text, "Close")

    def _ask(self, title: str, text: str, confirm: str) -> bool:
        # A ContentDialog inside the window; Cancel is the default, so Enter never confirms by accident.
        return fluent.ask(self, title, text, confirm, "Cancel", default="cancel")

    def _action_failed(self, title: str, exc: Exception) -> None:
        if isinstance(exc, BackendUnavailable):
            self.set_session_available(False)
        self.message.hide()
        self.notify_error(title, self._error_text(exc))
        self.refresh()

    # --- application actions ---
    def _name(self, app_id: str) -> str:
        return next((row.name for row in self.rows if row.id == app_id), app_id)

    def run_action(self, action: str, app_id: str) -> None:
        handlers = {"launch": self.launch_app, "stop": self.stop_app, "repair": self.repair_app,
                    "update": self.update_app, "remove": self.remove_app,
                    "logs": lambda a: self.open_details(a, "logs"), "details": self.open_details}
        handlers[action](app_id)

    def launch_app(self, app_id: str) -> None:
        name = self._name(app_id)
        self.show_message("info", f"Starting {name}…")
        self.runner.submit(self.backend.launch, app_id, **vm.display_params(self.environ),
                           on_done=lambda result: self._launched(name, result),
                           on_error=lambda exc: self._action_failed(f"{name} could not be started", exc))

    def _launched(self, name: str, result: dict) -> None:
        self.show_message("info", vm.launch_message(result, name))
        self.refresh()

    def stop_app(self, app_id: str) -> None:
        name = self._name(app_id)
        title, text = vm.stop_question(name)
        if not self.ask(title, text, "Stop"):
            return
        self.runner.submit(self.backend.stop, app_id, on_done=lambda result: self._stopped(name, result),
                           on_error=lambda exc: self._action_failed(f"{name} could not be stopped", exc))

    def _stopped(self, name: str, result: dict) -> None:
        self.show_message("info", f"{name} was stopped." if result.get("was_running") else f"{name} was not running.")
        self.refresh()

    def repair_app(self, app_id: str) -> None:
        self._start_job("repair", app_id, self.backend.repair)

    def remove_app(self, app_id: str) -> None:
        name = self._name(app_id)
        dialog = RemoveDialog(vm.removal_plan(name, app_id), self)
        accepted = self.dialog_exec(dialog) == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if accepted:
            self._start_job("remove", app_id, self.backend.remove)

    def update_app(self, app_id: str) -> InstallDialog:
        dialog = self.update_dialogs.get(app_id)
        if dialog is None:
            dialog = InstallDialog(self.backend, self.runner, upgrade_id=app_id, upgrade_name=self._name(app_id),
                                   parent=self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.destroyed.connect(lambda _obj=None, a=app_id: self.update_dialogs.pop(a, None))
            dialog.flow_widget.installed.connect(self._installed)
            dialog.flow_widget.launch_requested.connect(self.launch_app)
            dialog.flow_widget.show_log_requested.connect(lambda a, kind: self.open_details(a, "logs", kind))
            self.update_dialogs[app_id] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _start_job(self, op: str, app_id: str, start) -> None:
        name = self._name(app_id)
        self.show_message("info", vm.job_started_text(op, name))
        self.runner.submit(start, app_id, on_done=lambda job_id: self._track_job(job_id, op, app_id, name),
                           on_error=lambda exc: self._action_failed(
                               f"{vm.JOB_VERBS.get(op, ('', '', 'The operation'))[2]} of {name} could not start",
                               exc))

    def _track_job(self, job_id: str, op: str, app_id: str, name: str) -> None:
        self.jobs[job_id] = (op, app_id, name)
        if not self.job_timer.isActive():
            self.job_timer.start()
        self.refresh()

    def poll_jobs(self) -> None:
        if not self.jobs:
            self.job_timer.stop()
            return
        for job_id in list(self.jobs):
            self.runner.submit(self.backend.job, job_id, key=f"job:{job_id}",
                               on_done=self._job_loaded, on_error=lambda exc, j=job_id: self._job_failed(j, exc))

    def _job_loaded(self, job: dict) -> None:
        job_id = job.get("job_id")
        if job_id not in self.jobs or job.get("state") == "running":
            return
        op, app_id, name = self.jobs.pop(job_id)
        level, text = vm.job_finished_text(job, name)
        self.show_message(level, text)
        if op == "remove" and level == "ok" and app_id in self.details:
            self.details[app_id].close()
        self.refresh()

    def _job_failed(self, job_id: str, exc: Exception) -> None:
        if job_id not in self.jobs or isinstance(exc, BackendUnavailable):
            return                                    # keep following it once the service is back
        _op, _app_id, name = self.jobs.pop(job_id)
        self.show_message("warning", f"The progress of {name} can no longer be followed: {self._error_text(exc)}")
        self.refresh()

    def open_details(self, app_id: str, tab: str = "overview", log_kind: str = "latest") -> DetailsDialog:
        dialog = self.details.get(app_id)
        if dialog is None:
            dialog = DetailsDialog(self.backend, self.runner, app_id, name=self._name(app_id), tab=tab,
                                   log_kind=log_kind, system=self.system, parent=self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.destroyed.connect(lambda _obj=None, a=app_id: self.details.pop(a, None))
            dialog.changed.connect(self.refresh)
            self.details[app_id] = dialog
        else:
            dialog.show_tab(tab, log_kind if tab == "logs" else None)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _installed(self, app_id: str) -> None:
        self.refresh(full=True)
        self.library.select_app(app_id)

    # --- install entry point ---
    def open_install(self, path: str | None = None) -> None:
        """Show the Install page, checking path right away when given (--install FILE)."""
        self.show_page("install")
        if path:
            self.install_page.check_file(path)

    # --- shutdown ---
    def shutdown(self) -> None:
        self.refresh_timer.stop()
        self.job_timer.stop()
        self.install_page.poll_timer.stop()
        self.runner.shutdown()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
