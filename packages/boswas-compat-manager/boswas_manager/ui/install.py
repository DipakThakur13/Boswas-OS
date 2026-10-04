"""Install (and update) flow: choose an installer, check it, confirm, follow the job.

The steps follow viewmodel.InstallFlow. Checking calls apps.inspect, which
never installs or runs anything; installing starts a job in the session
agent, polled every 500 ms. Every decision (architecture, catalog, policy)
is the backend's: this widget only shows it. A 32-bit installer gets the
fixed product texts and no way forward except choosing another file.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLineEdit, QPlainTextEdit, QProgressBar, QStackedWidget, QVBoxLayout, QWidget)

from .. import viewmodel as vm
from ..backend import BackendRefused, BackendUnavailable
from .common import SP, button, heading, icon, icon_label, plain_label

POLL_MS = 500
RETRY_MS = 2000
INSTALLER_FILTER = "Windows programs and installers (*.exe *.msi *.EXE *.MSI);;All files (*)"


def _clear_form(form: QFormLayout) -> None:
    while form.rowCount():
        form.removeRow(0)


def _error_parts(exc: Exception) -> tuple[str, str, bool]:
    """(code, message, refused) of a backend exception."""
    if isinstance(exc, BackendRefused):
        return exc.code, exc.message, True
    if isinstance(exc, BackendUnavailable):
        return "UNAVAILABLE", vm.SESSION_UNAVAILABLE, False
    return "INTERNAL", f"Unexpected error: {vm.one_line(exc, 300)}", False


class InstallFlowWidget(QWidget):
    installed = Signal(str)                 # application ID, after a successful install or update
    launch_requested = Signal(str)
    show_log_requested = Signal(str, str)   # application ID, log kind
    show_applications_requested = Signal()

    def __init__(self, backend, runner, *, upgrade_id: str | None = None, upgrade_name: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.backend, self.runner = backend, runner
        self.flow = vm.InstallFlow(upgrade_id, upgrade_name)
        self.open_dialog = QFileDialog.getOpenFileName          # replaceable in tests
        self.poll_interval, self.retry_interval = POLL_MS, RETRY_MS
        self.poll_timer = QTimer(self)
        self.poll_timer.setSingleShot(True)
        self.poll_timer.timeout.connect(self.poll_job)
        self._starting = False

        layout = QVBoxLayout(self)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)
        self.pages = {
            vm.InstallFlow.SELECT: self._select_page(),
            vm.InstallFlow.CHECKING: self._checking_page(),
            vm.InstallFlow.REFUSED_32BIT: self._refused_32bit_page(),
            vm.InstallFlow.REFUSED: self._refused_page(),
            vm.InstallFlow.CONFIRM: self._confirm_page(),
            vm.InstallFlow.INSTALLING: self._installing_page(),
            vm.InstallFlow.DONE: self._done_page(),
            vm.InstallFlow.FAILED: self._failed_page(),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.render()

    # --- pages ---
    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        return page, layout

    def _select_page(self) -> QWidget:
        page, layout = self._page()
        if self.flow.upgrading:
            layout.addWidget(heading(f"Update {self.flow.upgrade_name or self.flow.upgrade_id}"))
            intro = ("Choose the installer of the new version. The application keeps its C: drive and the files "
                     "it saved there.")
        else:
            layout.addWidget(heading("Install a Windows Application"))
            intro = ("Choose a Windows program (.exe) or Windows Installer package (.msi). Boswas checks it against "
                     "the compatibility catalog and the device policy before anything is installed. Every "
                     "application gets its own isolated C: drive.")
        layout.addWidget(plain_label(intro, wrap=True))
        self.supported = plain_label(f"Supported: {vm.SUPPORTED_ARCHITECTURE}", wrap=True)
        layout.addWidget(self.supported)
        row = QHBoxLayout()
        self.file_field = QLineEdit(page)
        self.file_field.setReadOnly(True)
        self.file_field.setPlaceholderText("No installer chosen")
        self.choose_button = button("Choose File…", icon("document-open", fallback=SP.SP_DialogOpenButton), page)
        self.choose_button.clicked.connect(self.choose_file)
        row.addWidget(self.file_field, 1)
        row.addWidget(self.choose_button)
        layout.addLayout(row)

        self.advanced = QGroupBox("Options for applications that are not in the catalog", page)
        self.advanced.setCheckable(True)
        self.advanced.setChecked(False)
        form = QFormLayout(self.advanced)
        self.id_field = QLineEdit(self.advanced)
        self.id_field.setPlaceholderText("local.my-application (optional)")
        self.id_field.setMaxLength(96)
        self.name_field = QLineEdit(self.advanced)
        self.name_field.setPlaceholderText("Display name (optional)")
        self.name_field.setMaxLength(80)
        form.addRow(plain_label("Application ID"), self.id_field)
        form.addRow(plain_label("Display name"), self.name_field)
        self.id_hint = plain_label("Lower-case letters, digits and dashes, in parts separated by dots "
                                   "(for example local.my-application).", wrap=True)
        form.addRow(self.id_hint)
        self.advanced.setVisible(not self.flow.upgrading)
        layout.addWidget(self.advanced)
        self.select_error = plain_label("", wrap=True)
        layout.addWidget(self.select_error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.check_button = button("Check Installer", icon("document-preview", "system-search",
                                                           fallback=SP.SP_FileDialogDetailedView), page)
        self.check_button.clicked.connect(self.check_current)
        buttons.addWidget(self.check_button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _checking_page(self) -> QWidget:
        page, layout = self._page()
        layout.addWidget(heading(vm.CHECKING_INSTALLER))
        self.checking_file = plain_label("", wrap=True)
        layout.addWidget(self.checking_file)
        bar = QProgressBar(page)
        bar.setRange(0, 0)
        layout.addWidget(bar)
        layout.addWidget(plain_label(vm.CHECKING_NOTE, wrap=True))
        layout.addStretch(1)
        return page

    def _refusal_header(self, layout: QVBoxLayout, level_icon) -> tuple:
        top = QHBoxLayout()
        top.addWidget(icon_label(level_icon, 48), 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        title = heading("", 1.2)
        detail = plain_label("", wrap=True, selectable=True)
        texts.addWidget(title)
        texts.addWidget(detail)
        top.addLayout(texts, 1)
        layout.addLayout(top)
        return title, detail, texts

    def _another_file_row(self, layout: QVBoxLayout, page: QWidget, extra: list | None = None) -> None:
        row = QHBoxLayout()
        row.addStretch(1)
        for widget in extra or []:
            row.addWidget(widget)
        another = button("Choose Another File", icon("document-open", fallback=SP.SP_DialogOpenButton), page)
        another.clicked.connect(self.choose_another)
        row.addWidget(another)
        layout.addLayout(row)

    def _refused_32bit_page(self) -> QWidget:
        page, layout = self._page()
        self.r32_title, self.r32_detail, _texts = self._refusal_header(
            layout, icon("dialog-error", fallback=SP.SP_MessageBoxCritical))
        self.r32_file = plain_label("", wrap=True)
        layout.addWidget(self.r32_file)
        self._another_file_row(layout, page)
        layout.addStretch(1)
        return page

    def _refused_page(self) -> QWidget:
        page, layout = self._page()
        self.refused_title, self.refused_message, texts = self._refusal_header(
            layout, icon("dialog-warning", fallback=SP.SP_MessageBoxWarning))
        self.refused_hint = plain_label("", wrap=True)
        texts.addWidget(self.refused_hint)
        self.open_apps_button = button("Open Applications", icon("view-list-details", fallback=SP.SP_DirIcon), page)
        self.open_apps_button.clicked.connect(self.show_applications_requested.emit)
        self._another_file_row(layout, page, [self.open_apps_button])
        layout.addStretch(1)
        return page

    def _confirm_page(self) -> QWidget:
        page, layout = self._page()
        self.confirm_title = heading("")
        layout.addWidget(self.confirm_title)
        self.decision_form = QFormLayout()
        layout.addLayout(self.decision_form)
        sandbox = QGroupBox("What the application can access", page)
        sandbox_layout = QVBoxLayout(sandbox)
        self.sandbox_form = QFormLayout()
        sandbox_layout.addLayout(self.sandbox_form)
        sandbox_layout.addWidget(plain_label(vm.PERMISSIONS_NOTICE, wrap=True))
        layout.addWidget(sandbox)
        self.confirm_status = plain_label("", wrap=True)
        layout.addWidget(self.confirm_status)
        row = QHBoxLayout()
        self.change_button = button("Change ID or Name…", icon("document-edit", fallback=SP.SP_FileDialogBack),
                                    page)
        self.change_button.clicked.connect(self.back_to_select)
        row.addWidget(self.change_button)
        row.addStretch(1)
        back = button("Choose Another File", icon("go-previous", fallback=SP.SP_ArrowBack), page)
        back.clicked.connect(self.choose_another)
        row.addWidget(back)
        self.install_button = button("Install", icon("run-install", "system-software-install", "dialog-ok-apply",
                                                     fallback=SP.SP_DialogApplyButton), page)
        self.install_button.setDefault(True)
        self.install_button.clicked.connect(self.start_install)
        row.addWidget(self.install_button)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _installing_page(self) -> QWidget:
        page, layout = self._page()
        self.installing_title = heading("")
        layout.addWidget(self.installing_title)
        bar = QProgressBar(page)
        bar.setRange(0, 0)
        layout.addWidget(bar)
        self.progress_view = QPlainTextEdit(page)
        self.progress_view.setReadOnly(True)
        self.progress_view.setUndoRedoEnabled(False)
        self.progress_view.setPlaceholderText("Waiting for progress…")
        layout.addWidget(self.progress_view, 1)
        self.installing_note = plain_label("The installation continues if you leave this page.", wrap=True)
        layout.addWidget(self.installing_note)
        return page

    def _done_page(self) -> QWidget:
        page, layout = self._page()
        top = QHBoxLayout()
        top.addWidget(icon_label(icon("dialog-positive", "emblem-success", "dialog-ok",
                                      fallback=SP.SP_DialogApplyButton), 48), 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        self.done_title = heading("", 1.2)
        self.done_note = plain_label("", wrap=True)
        texts.addWidget(self.done_title)
        texts.addWidget(self.done_note)
        top.addLayout(texts, 1)
        layout.addLayout(top)
        row = QHBoxLayout()
        row.addStretch(1)
        self.show_apps_button = button("Show in Applications", icon("view-list-details", fallback=SP.SP_DirIcon),
                                       page)
        self.show_apps_button.clicked.connect(self.show_applications_requested.emit)
        self.another_button = button("Install Another", icon("list-add", fallback=SP.SP_FileDialogNewFolder), page)
        self.another_button.clicked.connect(self.choose_another)
        self.another_button.setVisible(not self.flow.upgrading)
        self.launch_button = button("Launch", icon("media-playback-start", fallback=SP.SP_MediaPlay), page)
        self.launch_button.setDefault(True)
        self.launch_button.clicked.connect(self._launch)
        for widget in (self.show_apps_button, self.another_button, self.launch_button):
            row.addWidget(widget)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _failed_page(self) -> QWidget:
        page, layout = self._page()
        self.failed_title, self.failed_message, _texts = self._refusal_header(
            layout, icon("dialog-error", fallback=SP.SP_MessageBoxCritical))
        self.log_button = button("Show Log", icon("text-x-log", fallback=SP.SP_FileDialogContentsView), page)
        self.log_button.clicked.connect(self._show_log)
        self.retry_button = button("Try Again", icon("view-refresh", fallback=SP.SP_BrowserReload), page)
        self.retry_button.clicked.connect(self.retry)
        self._another_file_row(layout, page, [self.log_button, self.retry_button])
        layout.addStretch(1)
        return page

    # --- rendering ---
    def current_page(self) -> str:
        return next(state for state, page in self.pages.items() if page is self.stack.currentWidget())

    def render(self) -> None:
        flow = self.flow
        state = flow.state
        if state == flow.SELECT:
            self.check_button.setEnabled(bool(self.file_field.text()))
        elif state == flow.CHECKING:
            self.checking_file.setText(flow.file_name)
        elif state == flow.REFUSED_32BIT:
            self.r32_title.setText(flow.refusal_title)
            self.r32_detail.setText(flow.refusal_detail)
            self.r32_file.setText(f"Installer: {flow.file_name}")
        elif state == flow.REFUSED:
            self.refused_title.setText(flow.refusal_title)
            self.refused_message.setText(flow.refusal_detail)
            self.refused_hint.setText(flow.refusal_hint)
            self.refused_hint.setVisible(bool(flow.refusal_hint))
            self.open_apps_button.setVisible(flow.error_code == "already-installed")
        elif state == flow.CONFIRM:
            self.confirm_title.setText(flow.confirm_title)
            _clear_form(self.decision_form)
            for row in flow.decision_rows():
                self.decision_form.addRow(plain_label(row.label), plain_label(row.value, wrap=True, selectable=True))
            _clear_form(self.sandbox_form)
            for prow in flow.sandbox_rows():
                self.sandbox_form.addRow(plain_label(prow.label), plain_label(prow.value, wrap=True))
            self.install_button.setText(flow.confirm_label)
            self.install_button.setEnabled(not self._starting)
            self.change_button.setVisible(not flow.upgrading and flow.unlisted)
            self.confirm_status.setText("Starting…" if self._starting else "")
        elif state == flow.INSTALLING:
            self.installing_title.setText(flow.installing_title)
            self.progress_view.setPlainText("\n".join(flow.progress))
            bar = self.progress_view.verticalScrollBar()
            bar.setValue(bar.maximum())
        elif state == flow.DONE:
            self.done_title.setText(flow.done_title)
            self.done_note.setText(flow.done_note)
            self.done_note.setVisible(bool(flow.done_note))
            self.launch_button.setEnabled(flow.can_launch)
        elif state == flow.FAILED:
            self.failed_title.setText(flow.failed_title)
            self.failed_message.setText(flow.error_message)
            self.log_button.setVisible(flow.failed_phase == "install" and flow.application_id is not None)
        self.stack.setCurrentWidget(self.pages[state])

    # --- actions ---
    def choose_file(self) -> None:
        path, _selected = self.open_dialog(self, "Choose a Windows Installer", "", INSTALLER_FILTER)
        if path:
            self.check_file(path)

    def choose_another(self) -> None:
        if self.flow.state in (self.flow.CHECKING, self.flow.INSTALLING):
            return
        self.poll_timer.stop()
        self.flow.reset()
        self.file_field.clear()
        self.select_error.clear()
        self.render()
        self.choose_file()

    def back_to_select(self) -> None:
        """Back to the first step keeping the file (to set an ID or name for an unlisted application)."""
        if self.flow.state in (self.flow.CHECKING, self.flow.INSTALLING):
            return
        path = self.flow.path
        self.flow.reset()
        self.file_field.setText(path or "")
        self.advanced.setChecked(True)
        self.render()

    def check_current(self) -> None:
        if self.file_field.text():
            self.check_file(self.file_field.text())

    def retry(self) -> None:
        if self.flow.path:
            self.check_file(self.flow.path)

    def check_file(self, path: str) -> None:
        """Start checking an installer (also used by --install FILE and the Dolphin service menu)."""
        if self.flow.state in (self.flow.CHECKING, self.flow.INSTALLING):
            return
        self.file_field.setText(path)
        app_id = name = None
        if self.advanced.isChecked() and not self.flow.upgrading:
            app_id, name = self.id_field.text().strip() or None, self.name_field.text().strip() or None
            if app_id is not None and not vm.valid_id(app_id):
                self.flow.reset()
                self.select_error.setText("The application ID is not valid. " + self.id_hint.text())
                self.render()
                return
        self.select_error.clear()
        try:
            self.flow.select(path, app_id, name)
        except vm.FlowError as exc:
            self.flow.reset()
            self.select_error.setText(str(exc).capitalize() + ".")
            self.render()
            return
        self.render()
        params = self.flow.inspect_params()
        self.runner.submit(self.backend.inspect, params["path"], params["app_id"], params["name"],
                           on_done=self._inspected, on_error=self._inspect_failed)

    def _inspected(self, result: dict) -> None:
        if self.flow.state == self.flow.CHECKING:
            self.flow.inspected(result)
            self.render()

    def _inspect_failed(self, exc: Exception) -> None:
        if self.flow.state == self.flow.CHECKING:
            code, message, _refused = _error_parts(exc)
            self.flow.check_failed(code, message)
            self.render()

    def start_install(self) -> None:
        if self.flow.state != self.flow.CONFIRM or self._starting:
            return
        params = self.flow.install_params()
        self._starting = True
        self.render()
        if self.flow.upgrading:
            self.runner.submit(self.backend.upgrade, params["app_id"], params["path"], on_done=self._started,
                               on_error=self._start_failed)
        else:
            self.runner.submit(self.backend.install, params["path"], params["app_id"], params["name"],
                               on_done=self._started, on_error=self._start_failed)

    def _started(self, job_id: str) -> None:
        self._starting = False
        if self.flow.state == self.flow.CONFIRM:
            self.flow.started(job_id)
            self.render()
            self.poll_timer.start(self.poll_interval)

    def _start_failed(self, exc: Exception) -> None:
        self._starting = False
        if self.flow.state != self.flow.CONFIRM:
            return
        code, message, refused = _error_parts(exc)
        if refused:
            self.flow.start_refused(code, message)
        else:
            self.flow.start_failed(code, message)
        self.render()

    def poll_job(self) -> None:
        """Ask for the job's progress (every 500 ms while installing; tests call it directly)."""
        if self.flow.state != self.flow.INSTALLING or not self.flow.job_id:
            return
        self.runner.submit(self.backend.job, self.flow.job_id, key=f"job:{self.flow.job_id}",
                           on_done=self._job_loaded, on_error=self._job_failed)

    def _job_loaded(self, job: dict) -> None:
        if self.flow.state != self.flow.INSTALLING or job.get("job_id") not in (None, self.flow.job_id):
            return
        state = self.flow.job_update(job)
        self.render()
        if state == self.flow.INSTALLING:
            self.poll_timer.start(self.poll_interval)
        elif state == self.flow.DONE and self.flow.application_id:
            self.installed.emit(self.flow.application_id)

    def _job_failed(self, exc: Exception) -> None:
        if self.flow.state != self.flow.INSTALLING:
            return
        if isinstance(exc, BackendUnavailable):
            self.installing_note.setText(vm.SESSION_UNAVAILABLE)
            self.poll_timer.start(self.retry_interval)
            return
        code, message, _refused = _error_parts(exc)
        if code == "NOT_FOUND":
            message = ("The installation can no longer be followed (the session service was restarted). "
                       "Check Applications to see whether it finished.")
        self.flow.job_lost(code, message)
        self.render()

    def _launch(self) -> None:
        if self.flow.can_launch and self.flow.application_id:
            self.launch_requested.emit(self.flow.application_id)

    def _show_log(self) -> None:
        if self.flow.application_id:
            self.show_log_requested.emit(self.flow.application_id, "install")


class InstallDialog(QDialog):
    """The install flow in its own window (used to update an installed application)."""

    def __init__(self, backend, runner, *, upgrade_id: str | None = None, upgrade_name: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.flow_widget = InstallFlowWidget(backend, runner, upgrade_id=upgrade_id, upgrade_name=upgrade_name,
                                             parent=self)
        title = f"Update {vm.one_line(upgrade_name, 80) or upgrade_id}" if upgrade_id else \
            "Install a Windows Application"
        self.setWindowTitle(title)
        self.resize(680, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(self.flow_widget, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.flow_widget.show_applications_requested.connect(self.close)
