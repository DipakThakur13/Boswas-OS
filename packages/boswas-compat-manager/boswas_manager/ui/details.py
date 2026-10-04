"""Application details: Overview, Permissions (read-only) and Logs tabs.

Permissions are displayed, never edited: what a Windows application may
access comes from the compatibility catalog and the device policy and is
recomputed by the backend at every start. There is deliberately no control
on the Permissions tab that could change it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QMessageBox, QPlainTextEdit, QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from .. import viewmodel as vm
from ..backend import BackendRefused, BackendUnavailable
from .common import (SP, MessageBanner, button, heading, icon, icon_label, message_box, plain_label, export_text,
                     windows_app_icon)

TABS = ("overview", "permissions", "logs")


def _clear_form(form: QFormLayout) -> None:
    while form.rowCount():
        form.removeRow(0)


class DetailsDialog(QDialog):
    """Non-modal details window of one application. Emits changed() after logs were cleared."""

    changed = Signal()

    def __init__(self, backend, runner, app_id: str, *, name: str | None = None, tab: str = "overview",
                 log_kind: str = "latest", system: dict | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.backend, self.runner, self.app_id = backend, runner, app_id
        self.name = vm.one_line(name, 120) or app_id
        self.system = system
        self.status: dict | None = None
        self.log_loaded_kind: str | None = None
        # Replaceable in tests: the save dialog and the confirmation question.
        self.save_dialog = QFileDialog.getSaveFileName
        self.ask = self._ask
        self.setWindowTitle(f"{self.name} — Details")
        self.setWindowIcon(windows_app_icon())
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(icon_label(windows_app_icon(), 48))
        titles = QVBoxLayout()
        self.title = heading(self.name)
        self.subtitle = plain_label(app_id, selectable=True)
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        top.addLayout(titles, 1)
        layout.addLayout(top)
        self.banner = MessageBanner(parent=self)
        layout.addWidget(self.banner)

        self.tabs = QTabWidget(self)
        tab_icons = (icon("documentinfo", "dialog-information", fallback=SP.SP_FileDialogInfoView),
                     icon("security-high", "preferences-system-privacy", fallback=SP.SP_VistaShield),
                     icon("text-x-log", "utilities-log-viewer", fallback=SP.SP_FileDialogContentsView))
        self.tabs.addTab(self._overview_tab(), tab_icons[0], "Overview")
        self.tabs.addTab(self._permissions_tab(), tab_icons[1], "Permissions")
        self.tabs.addTab(self._logs_tab(log_kind), tab_icons[2], "Logs")
        layout.addWidget(self.tabs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self.tabs.setCurrentIndex(TABS.index(tab) if tab in TABS else 0)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.reload()

    # --- tabs ---
    def _overview_tab(self) -> QWidget:
        page = QScrollArea(self)
        page.setWidgetResizable(True)
        page.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget(page)
        self.overview_form = QFormLayout(inner)
        self.overview_form.addRow(plain_label("Loading…"))
        page.setWidget(inner)
        return page

    def _permissions_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        notice = QHBoxLayout()
        notice.addWidget(icon_label(icon("dialog-information", fallback=SP.SP_MessageBoxInformation), 22),
                         0, Qt.AlignmentFlag.AlignTop)
        self.permissions_notice = plain_label(vm.PERMISSIONS_NOTICE, wrap=True)
        notice.addWidget(self.permissions_notice, 1)
        layout.addLayout(notice)
        self.permissions_form = QFormLayout()
        layout.addLayout(self.permissions_form)
        layout.addStretch(1)
        self.permissions_page = page
        return page

    def _logs_tab(self, log_kind: str) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(plain_label("Log:"))
        self.log_kind = QComboBox(page)
        for key, label in vm.LOG_KINDS:
            self.log_kind.addItem(label, key)
        index = self.log_kind.findData(log_kind)
        self.log_kind.setCurrentIndex(max(0, index))
        self.log_kind.currentIndexChanged.connect(lambda _i: self.load_logs())
        top.addWidget(self.log_kind)
        self.log_file = plain_label("", selectable=True)
        top.addWidget(self.log_file, 1)
        self.refresh_button = button("Refresh", icon("view-refresh", fallback=SP.SP_BrowserReload), page)
        self.refresh_button.clicked.connect(self.load_logs)
        top.addWidget(self.refresh_button)
        layout.addLayout(top)
        self.log_view = QPlainTextEdit(page)
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setUndoRedoEnabled(False)
        layout.addWidget(self.log_view, 1)
        bottom = QHBoxLayout()
        self.copy_button = button("Copy", icon("edit-copy", fallback=SP.SP_DialogSaveButton), page)
        self.copy_button.clicked.connect(self.copy_log)
        save_icon = icon("document-save-as", "document-save", fallback=SP.SP_DialogSaveButton)
        self.save_button = button("Save…", save_icon, page)
        self.save_button.clicked.connect(self.save_log)
        clear_icon = icon("edit-clear-history", "edit-clear", fallback=SP.SP_DialogResetButton)
        self.clear_button = button("Clear…", clear_icon, page)
        self.clear_button.clicked.connect(self.clear_logs)
        self.clear_button.setEnabled(False)            # until the status says the application is not running
        bottom.addWidget(self.copy_button)
        bottom.addWidget(self.save_button)
        bottom.addStretch(1)
        bottom.addWidget(self.clear_button)
        layout.addLayout(bottom)
        self._set_log_text("", "")
        return page

    def _tab_changed(self, index: int) -> None:
        if TABS[index] == "logs" and self.log_loaded_kind is None:
            self.load_logs()

    # --- status ---
    def reload(self) -> None:
        self.runner.submit(self.backend.app_status, self.app_id, on_done=self._status_loaded,
                           on_error=self._status_failed)
        if TABS[self.tabs.currentIndex()] == "logs":
            self.load_logs()

    def show_tab(self, tab: str, log_kind: str | None = None) -> None:
        """Switch tab (and log kind) without triggering loads, then reload what is shown once."""
        self.tabs.blockSignals(True)
        self.log_kind.blockSignals(True)
        try:
            if log_kind is not None and self.log_kind.findData(log_kind) >= 0:
                self.log_kind.setCurrentIndex(self.log_kind.findData(log_kind))
            self.tabs.setCurrentIndex(TABS.index(tab) if tab in TABS else 0)
        finally:
            self.tabs.blockSignals(False)
            self.log_kind.blockSignals(False)
        self.reload()

    def set_system(self, system: dict | None) -> None:
        self.system = system
        if self.status is not None:
            self._fill_permissions(self.status)

    def _status_loaded(self, status: dict) -> None:
        self.status = status
        self.banner.hide()
        rows = vm.detail_rows(status)
        name = next((row.value for row in rows if row.label == "Name"), self.name)
        self.name = name
        self.title.setText(name)
        self.setWindowTitle(f"{name} — Details")
        _clear_form(self.overview_form)
        for row in rows:
            self.overview_form.addRow(plain_label(row.label), plain_label(row.value, wrap=True, selectable=True))
        self._fill_permissions(status)
        self.clear_button.setEnabled(not vm.is_running(status))
        self.clear_button.setToolTip("Logs cannot be cleared while the application is running."
                                     if vm.is_running(status) else "")

    def _fill_permissions(self, status: dict) -> None:
        _clear_form(self.permissions_form)
        last = status.get("last_launch") if isinstance(status.get("last_launch"), dict) else {}
        rows = vm.permission_rows(status.get("sandbox"), vm.apparmor_mode(self.system),
                                  vm.apparmor_profile(self.system), last.get("confinement"))
        for row in rows:
            self.permissions_form.addRow(plain_label(row.label), plain_label(row.value, wrap=True, selectable=True))

    def _status_failed(self, exc: Exception) -> None:
        self.banner.show_message("error", self._error(exc))

    @staticmethod
    def _error(exc: Exception) -> str:
        if isinstance(exc, BackendUnavailable):
            return vm.SESSION_UNAVAILABLE
        if isinstance(exc, BackendRefused):
            return vm.error_text(exc.code, exc.message)
        return f"Unexpected error: {vm.one_line(exc, 300)}"

    # --- logs ---
    @property
    def current_kind(self) -> str:
        return self.log_kind.currentData() or "latest"

    def load_logs(self) -> None:
        kind = self.current_kind
        self.log_loaded_kind = kind
        self.runner.submit(self.backend.logs, self.app_id, kind, on_done=lambda r, k=kind: self._logs_loaded(k, r),
                           on_error=lambda e, k=kind: self._logs_failed(k, e))

    def _set_log_text(self, title: str, text: str, placeholder: str = "") -> None:
        self.log_file.setText(title)
        self.log_view.setPlainText(text)
        self.log_view.setPlaceholderText(placeholder)
        has_text = bool(text)
        self.copy_button.setEnabled(has_text)
        self.save_button.setEnabled(has_text)

    def _logs_loaded(self, kind: str, result: dict) -> None:
        if kind != self.current_kind:
            return                        # an older answer for another log kind
        text = vm.log_text(result)
        self._set_log_text(vm.log_title(result), text, "The log is empty.")
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _logs_failed(self, kind: str, exc: Exception) -> None:
        if kind != self.current_kind:
            return
        if isinstance(exc, BackendRefused) and exc.code == "NOT_FOUND":
            self._set_log_text("", "", vm.LOG_MISSING.get(kind, "There is no such log."))
        else:
            self._set_log_text("", "", self._error(exc))

    def log_content(self) -> str:
        return self.log_view.toPlainText()

    def copy_log(self) -> None:
        QGuiApplication.clipboard().setText(self.log_content())

    def save_log(self) -> None:
        text = self.log_content()
        if not text:
            return
        suggested = f"{self.app_id}-{self.current_kind}.log"
        path, _selected = self.save_dialog(self, "Save Log", suggested, "Log files (*.log);;Text files (*.txt);;"
                                           "All files (*)")
        if not path:
            return
        try:
            export_text(path, text)
        except OSError as exc:
            message_box(self, "error", "Save Log", f"The log could not be saved: {exc.strerror or exc}").exec()

    def _ask(self, title: str, text: str, confirm: str) -> bool:
        box = message_box(self, "question", title, text)
        yes = box.addButton(confirm, QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is yes

    def clear_logs(self) -> None:
        if self.status is None or vm.is_running(self.status):
            return
        if not self.ask("Clear Logs", f"Delete all logs of {self.name}? This cannot be undone.", "Clear"):
            return
        self.clear_button.setEnabled(False)
        self.runner.submit(self.backend.clear_logs, self.app_id, on_done=self._logs_cleared,
                           on_error=self._clear_failed)

    def _logs_cleared(self, _result: dict) -> None:
        self.clear_button.setEnabled(self.status is not None and not vm.is_running(self.status))
        self.changed.emit()
        self.load_logs()

    def _clear_failed(self, exc: Exception) -> None:
        self.clear_button.setEnabled(self.status is not None and not vm.is_running(self.status))
        self.banner.show_message("error", self._error(exc))
