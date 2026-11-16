"""Running the engine's operations on threads, with progress the window can show.

A ``Job`` wraps one ``fileops.Operation``. The operation runs on a QThread
of its own; progress arrives on the GUI thread through signals; a conflict
makes the worker wait until the user interface calls ``answer``.
``JobManager`` owns the running jobs of the application.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal

from .fileops import CANCEL, Conflict, Operation, Progress, Reporter, Result


class _Thread(QThread):
    def __init__(self, job: "Job") -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        job = self._job
        try:
            result = job.operation.run(job._reporter)
        except Exception as error:                  # a bug in an operation must not take the application down
            result = Result(job.operation.title, errors=[("", f"Unexpected error: {error}")])
        job._done.emit(result)


class _Bridge(Reporter):
    def __init__(self, job: "Job") -> None:
        self._job = job

    def progress(self, progress: Progress) -> None:
        job = self._job
        with job._lock:
            job._snapshot = Progress(progress.current, progress.items_done, progress.items_total,
                                     progress.bytes_done, progress.bytes_total, progress.preparing)
        job._progressed.emit()

    def conflict(self, conflict: Conflict) -> tuple[str, bool]:
        job = self._job
        if job._cancel.is_set():
            return CANCEL, False
        job._answer = None
        job._answered.clear()
        job._conflict.emit(conflict)
        job._answered.wait()
        return job._answer or (CANCEL, False)

    def cancelled(self) -> bool:
        return self._job._cancel.is_set()


class Job(QObject):
    """One running operation. Signals are emitted on the GUI thread."""

    changed = Signal()                  # progress or state changed
    conflict = Signal(object)           # a Conflict: call answer()
    finished = Signal(object)           # the Result

    _progressed = Signal()
    _conflict = Signal(object)
    _done = Signal(object)

    def __init__(self, operation: Operation, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.operation = operation
        self.title = operation.title
        self.result: Result | None = None
        self.owner = None                           # the window that started it (for dialogs), set by the caller
        self.started = time.monotonic()
        self.speed = 0.0                            # bytes per second, smoothed
        self.waiting = False                        # a conflict is waiting for an answer
        self._lock = threading.Lock()
        self._snapshot = Progress(preparing=True)
        self._cancel = threading.Event()
        self._answered = threading.Event()
        self._answer: tuple[str, bool] | None = None
        self._reporter = _Bridge(self)
        self._thread = _Thread(self)
        self._last_bytes = 0
        self._last_time = self.started
        self._progressed.connect(self._on_progress)
        self._conflict.connect(self._on_conflict)
        self._done.connect(self._on_done)

    def start(self) -> None:
        self._thread.start()

    def progress(self) -> Progress:
        with self._lock:
            return self._snapshot

    def fraction(self) -> float | None:
        """0..1, or None while the amount of work is not known."""
        progress = self.progress()
        if progress.preparing:
            return None
        if progress.bytes_total > 0:
            return max(0.0, min(1.0, progress.bytes_done / progress.bytes_total))
        if progress.items_total > 0:
            return max(0.0, min(1.0, progress.items_done / progress.items_total))
        return None

    def is_running(self) -> bool:
        return self.result is None

    def is_cancelling(self) -> bool:
        return self._cancel.is_set() and self.result is None

    def cancel(self) -> None:
        self._cancel.set()
        if self.waiting:
            self.answer(CANCEL, False)
        self.changed.emit()

    def answer(self, choice: str, for_all: bool = False) -> None:
        """Answer the conflict that is waiting."""
        self.waiting = False
        self._answer = (choice, for_all)
        self._answered.set()

    def wait(self, timeout: float = 10.0) -> bool:
        """Process events until the job has finished (tests)."""
        deadline = time.monotonic() + timeout
        while self.result is None and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            self._thread.wait(5)
        return self.result is not None

    def _on_progress(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_time
        if elapsed >= 0.5:
            done = self.progress().bytes_done
            current = max(0.0, (done - self._last_bytes) / elapsed)
            self.speed = current if self.speed == 0 else self.speed * 0.6 + current * 0.4
            self._last_bytes, self._last_time = done, now
        self.changed.emit()

    def _on_conflict(self, conflict: Conflict) -> None:
        self.waiting = True
        self.conflict.emit(conflict)

    def _on_done(self, result: Result) -> None:
        self._thread.wait(2000)
        self.result = result
        self.changed.emit()
        self.finished.emit(result)


class JobManager(QObject):
    """The running operations of the application."""

    job_added = Signal(object)
    job_finished = Signal(object)
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.jobs: list[Job] = []
        self.conflict_handler: Callable[[Job, Conflict], None] | None = None

    def run(self, operation: Operation, on_done: Callable[[Result], None] | None = None, owner=None) -> Job:
        job = Job(operation, self)
        job.owner = owner
        self.jobs.append(job)
        job.changed.connect(self.changed)
        job.conflict.connect(lambda conflict, j=job: self._conflict(j, conflict))
        job.finished.connect(lambda result, j=job: self._finished(j, result, on_done))
        self.job_added.emit(job)
        self.changed.emit()
        job.start()
        return job

    def _conflict(self, job: Job, conflict: Conflict) -> None:
        if self.conflict_handler is not None:
            self.conflict_handler(job, conflict)
        else:
            job.answer(CANCEL, False)

    def _finished(self, job: Job, result: Result, on_done) -> None:
        if job in self.jobs:
            self.jobs.remove(job)
        self.job_finished.emit(job)
        self.changed.emit()
        if on_done is not None:
            on_done(result)
        job.deleteLater()

    def busy(self) -> bool:
        return bool(self.jobs)

    def wait(self, timeout: float = 15.0) -> bool:
        """Process events until no job is running (tests, orderly shutdown)."""
        deadline = time.monotonic() + timeout
        while self.jobs and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.002)
        return not self.jobs

    def cancel_all(self) -> None:
        for job in list(self.jobs):
            job.cancel()
