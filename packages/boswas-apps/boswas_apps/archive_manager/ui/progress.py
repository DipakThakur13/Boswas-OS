"""The small window of ``--extract``, ``--extract-here`` and ``--create``: progress, questions, a summary.

The Files application starts these from its context menu. The window shows
one operation after the other for the paths it was given, asks what the
engine needs to know (a password, what to do with a file that exists,
whether an unusually large archive should be unpacked), and closes by
itself when everything went through. When something was left out it stays
and says what and why.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QStackedWidget, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import APP_ID, DISPLAY_NAME, create, extract, formats, names, reader
from ..errors import ArchiveError, Cancelled, PasswordRequired, WrongPassword
from ..jobs import OVERWRITE, PASSWORD, SIZE, Job
from . import dialogs
from .dialogs import Result

EXIT_OK, EXIT_PROBLEMS, EXIT_CANCELLED = 0, 1, 2


class Task:
    """One thing the window does: extract an archive somewhere, or create one."""

    def __init__(self, kind: str, path: str, target: str | None = None, sources: list[str] | None = None) -> None:
        self.kind = kind                    # "extract" (into ``target``), "create" (``path`` from ``sources``)
        self.path = path
        self.target = target
        self.sources = sources or []


class ProgressWindow(fluent.FluentWindow):
    def __init__(self, tasks: list[Task], *, close_when_done: bool = True) -> None:
        super().__init__(APP_ID, DISPLAY_NAME, icon="utilities-file-archiver", size=(600, 380),
                         minimum_size=(520, 340), remember_geometry=False)
        self.worker = fluent.Worker(self, max_threads=1)
        self.tasks = list(tasks)
        self.close_when_done = close_when_done
        self.job: Job | None = None
        self.dialog: fluent.ContentDialog | None = None
        self.exit_code = EXIT_OK
        self.lines: list[str] = []
        self.finished_all = False
        self._current: Task | None = None
        self._archive: reader.Archive | None = None
        self._password: str | None = None
        self._last_destination: str | None = None
        self.open_path = fluent.open_path
        self.notify = fluent.notify

        self.panel = dialogs.ProgressPanel(cancel_button=False)
        self.cancel_button = fluent.Button("Cancel")
        self.cancel_button.clicked.connect(self.cancel)
        work = QWidget()
        work_layout = QVBoxLayout(work)
        work_layout.setContentsMargins(24, 24, 24, 20)
        work_layout.setSpacing(16)
        self.heading = fluent.Label("", "subtitle")
        work_layout.addWidget(self.heading)
        work_layout.addWidget(self.panel)
        work_layout.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        work_layout.addLayout(row)

        done = QWidget()
        done_layout = QVBoxLayout(done)
        done_layout.setContentsMargins(24, 24, 24, 20)
        done_layout.setSpacing(12)
        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        self.done_icon = fluent.StatusIcon("success", 24)
        self.done_title = fluent.Label("", "subtitle", wrap=True)
        title_row.addWidget(self.done_icon, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self.done_title, 1)
        done_layout.addLayout(title_row)
        self.done_text = QPlainTextEdit()
        self.done_text.setReadOnly(True)
        self.done_text.setAccessibleName("What happened")
        done_layout.addWidget(self.done_text, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.open_button = fluent.Button("Open folder")
        self.open_button.clicked.connect(self._open_folder)
        self.close_button = fluent.Button("Close", accent=True)
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.close_button)
        done_layout.addLayout(buttons)

        self.stack = QStackedWidget()
        self.stack.addWidget(work)
        self.stack.addWidget(done)
        self.set_content(self.stack)
        self._problems = False
        self._cancelled = False
        QTimer.singleShot(0, self._next)

    # --- the queue ---
    def _next(self) -> None:
        if self._cancelled or not self.tasks:
            self._finish()
            return
        task = self.tasks.pop(0)
        self._current = task
        name = names.display(os.path.basename(task.path))
        if task.kind == "create":
            self.heading.setText(f"Creating {name}")
            self.setWindowTitle(f"Creating {name}")
            if os.path.lexists(task.path):
                self._show_dialog(fluent.ContentDialog(
                    self, "Replace the existing file?", f"“{name}” already exists in that folder.",
                    primary="Replace", close="Cancel", default="close"),
                    lambda result: self._start_create(task) if result == Result.PRIMARY else self._skip_task(
                        f"{name}: not created, the file exists."))
            else:
                self._start_create(task)
        else:
            self.heading.setText(f"Extracting {name}")
            self.setWindowTitle(f"Extracting {name}")
            self._open(task)

    def _skip_task(self, line: str) -> None:
        self.lines.append(line)
        self._problems = True
        self._next()

    def _start_job(self, title: str, function) -> Job:
        job = Job(self.worker, function, self)
        self.job = job
        self.panel.start(title)
        job.handler.progressed.connect(self._progressed)
        job.handler.asked.connect(self._asked)
        job.start()
        return job

    def _progressed(self, info) -> None:
        if hasattr(info, "bytes_done"):
            self.panel.show_progress(info)

    def _end_job(self) -> None:
        job, self.job = self.job, None
        if job is not None:
            job.deleteLater()

    # --- extracting ---
    def _open(self, task: Task, password: str | None = None) -> None:
        path = task.path

        def work(handler):
            return reader.open_archive(path, password, handler.cancel, progress=handler.progress)

        job = self._start_job("Reading the archive", work)
        job.finished.connect(lambda archive: self._opened(task, archive, password))
        job.failed.connect(lambda error: self._open_failed(task, error))

    def _open_failed(self, task: Task, error: Exception) -> None:
        self._end_job()
        if isinstance(error, PasswordRequired):
            dialog = dialogs.PasswordDialog(self, os.path.basename(task.path), wrong=isinstance(error, WrongPassword))
            self._show_dialog(dialog, lambda result: self._open(task, dialog.password()) if result == Result.PRIMARY
                              else self._task_cancelled())
            return
        if isinstance(error, Cancelled):
            self._task_cancelled()
            return
        self._skip_task(f"{names.display(os.path.basename(task.path))}: {self._error_text(error)}")

    def _opened(self, task: Task, archive: reader.Archive, password: str | None) -> None:
        self._end_job()
        self._archive = archive
        target = task.target

        def work(handler):
            return extract.extract(archive, target, policy=extract.ASK, password=password, handler=handler,
                                   cancel=handler.cancel)

        job = self._start_job(f"To {names.display(target)}", work)
        job.finished.connect(lambda result: self._extracted(task, result))
        job.failed.connect(lambda error: self._operation_failed(task, error))

    def _extracted(self, task: Task, result: extract.Result) -> None:
        self._end_job()
        self._close_archive()
        name = names.display(os.path.basename(task.path))
        self._last_destination = result.destination
        self.lines.append(f"{name}: {result.summary()}")
        self.lines.extend(f"  {note}" for note in result.notes)
        for heading, issues in (("Skipped", result.skipped), ("Failed", result.failed)):
            for issue in issues[:500]:
                self.lines.append(f"  {heading}: {issue.path or 'the archive'}: {issue.reason}")
            if len(issues) > 500:
                self.lines.append(f"  … and {len(issues) - 500:,} more")
        if result.cancelled:
            self._cancelled = True
        elif not result.clean:
            self._problems = True
        self._next()

    # --- creating ---
    def _start_create(self, task: Task) -> None:
        fmt = formats.format_for_new(task.path)
        if fmt is None:
            self._skip_task(f"{names.display(os.path.basename(task.path))}: the name must end in .zip, .tar, "
                            ".tar.gz, .tar.xz or .tar.bz2.")
            return
        options = create.Options(format=fmt.key)

        def work(handler):
            return create.create(task.path, task.sources, options, handler, handler.cancel)

        job = self._start_job(f"In {names.display(os.path.dirname(task.path))}", work)
        job.finished.connect(lambda result: self._created(task, result))
        job.failed.connect(lambda error: self._operation_failed(task, error))

    def _created(self, task: Task, result: create.Result) -> None:
        self._end_job()
        name = names.display(os.path.basename(task.path))
        self._last_destination = os.path.dirname(task.path)
        if result.cancelled:
            self.lines.append(f"{name}: cancelled, nothing was created.")
            self._cancelled = True
        else:
            count = result.files + result.folders + result.links
            self.lines.append(f"{name}: packed {count:,} item{'s' if count != 1 else ''}.")
            for heading, issues in (("Skipped", result.skipped), ("Failed", result.failed)):
                self.lines.extend(f"  {heading}: {issue.path}: {issue.reason}" for issue in issues[:500])
            self.lines.extend(f"  {note}" for note in result.notes)
            if not result.clean:
                self._problems = True
        self._next()

    # --- questions, failures ---
    def _show_dialog(self, dialog: fluent.ContentDialog, on_finished) -> None:
        self.dialog = dialog

        def finished(result) -> None:
            if self.dialog is dialog:
                self.dialog = None
            on_finished(result)
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.open()

    def _asked(self, kind: str, payload: object) -> None:
        job = self.job
        if job is None:
            return
        if kind == OVERWRITE:
            dialog = dialogs.OverwriteDialog(self, payload)
            self._show_dialog(dialog, lambda _result: job.handler.answer(dialog.decision))
        elif kind == SIZE:
            dialog = fluent.ContentDialog(self, payload.title(), payload.text(), primary="Extract anyway",
                                          close="Cancel", default="close")
            self._show_dialog(dialog, lambda result: job.handler.answer(result == Result.PRIMARY))
        elif kind == PASSWORD:
            name = os.path.basename(self._current.path) if self._current else ""
            dialog = dialogs.PasswordDialog(self, name, wrong=bool(payload))
            self._show_dialog(dialog, lambda result: job.handler.answer(
                dialog.password() if result == Result.PRIMARY else None))

    @staticmethod
    def _error_text(error: BaseException) -> str:
        if isinstance(error, ArchiveError):
            return str(error)
        if isinstance(error, OSError):
            return f"The system reports: {error.strerror or error}."
        return f"Something unexpected went wrong ({type(error).__name__}: {error})."

    def _operation_failed(self, task: Task, error: Exception) -> None:
        self._end_job()
        self._close_archive()
        if isinstance(error, Cancelled):
            self._task_cancelled()
        else:
            self._skip_task(f"{names.display(os.path.basename(task.path))}: {self._error_text(error)}")

    def _task_cancelled(self) -> None:
        name = names.display(os.path.basename(self._current.path)) if self._current else ""
        self.lines.append(f"{name}: cancelled.")
        self._cancelled = True
        self._next()

    def _close_archive(self) -> None:
        if self._archive is not None:
            self._archive.close()
            self._archive = None

    def cancel(self) -> None:
        self._cancelled = True
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling…")
        if self.job is not None:
            self.job.cancel()
        elif self.dialog is None:
            self._finish()

    # --- the end ---
    def _finish(self) -> None:
        if self.finished_all:
            return
        self.finished_all = True
        self._current = None
        if self._cancelled:
            self.exit_code = EXIT_CANCELLED
        elif self._problems:
            self.exit_code = EXIT_PROBLEMS
        if self.exit_code == EXIT_OK and self.close_when_done:
            self.notify(self.lines[0] if len(self.lines) == 1 else f"{len(self.lines)} archives done",
                        names.display(self._last_destination or ""), icon="utilities-file-archiver")
            self.close()
            return
        if self._cancelled:
            self.done_icon.set_kind("neutral")
            self.done_title.setText("Cancelled")
        elif self._problems:
            self.done_icon.set_kind("warning")
            self.done_title.setText("Finished, but not everything went through")
        else:
            self.done_icon.set_kind("success")
            self.done_title.setText("Done")
        self.done_text.setPlainText("\n".join(self.lines))
        self.open_button.setVisible(bool(self._last_destination) and os.path.isdir(self._last_destination or ""))
        self.setWindowTitle(DISPLAY_NAME)
        self.stack.setCurrentIndex(1)
        self.close_button.setFocus()

    def _open_folder(self) -> None:
        if self._last_destination:
            self.open_path(self._last_destination)

    def closeEvent(self, event) -> None:
        if self.job is not None:
            self.job.abandon()
            self.job = None
            if self.exit_code == EXIT_OK:
                self.exit_code = EXIT_CANCELLED
        self.worker.shutdown()
        self._close_archive()
        super().closeEvent(event)
