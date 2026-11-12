"""Long operations in the window: one at a time, with progress, Cancel and Retry.

JobController (one per window) runs backend.run_job() on the Runner's thread
pool. Output lines arrive on the worker thread and are forwarded to the GUI
thread through a queued signal; the window never blocks. Only one operation
runs at a time across the whole window (two package operations would only
wait for each other's APT lock). Cancel writes "cancel" to the program's
standard input and is offered only while the program says the current phase
can be cancelled. Success is shown only for a result with ok: true
(jobs.outcome); Retry is offered for network and lock failures.

JobPanel shows the operation a page started: title, progress, the last
message, Cancel, and afterwards the outcome with Retry when useful.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QPushButton, QWidget

from .. import jobs
from ..commands import StreamHandle
from ..errors import user_message
from .common import SP, Card, MessageBanner, icon, muted_label, plain_label, title_label


class Running:
    def __init__(self, spec: jobs.JobSpec, on_done):
        self.spec, self.on_done = spec, on_done
        self.handle = StreamHandle()
        self.state = jobs.JobState()


class JobController(QObject):
    changed = Signal()                      # started, progress, cancellable changed, finished
    finished = Signal(object, object)       # (JobSpec, Outcome)
    _relay = Signal(object, object)         # worker thread -> GUI thread: (Running, Event)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.current: Running | None = None
        # The last finished operation: (spec, outcome, completion callback) for the page's result and Retry.
        self.last: tuple[jobs.JobSpec, jobs.Outcome, object] | None = None
        self._relay.connect(self._event, Qt.ConnectionType.QueuedConnection)

    # --- state ---
    def busy(self) -> bool:
        return self.current is not None

    def cancellable(self) -> bool:
        return self.current is not None and self.current.state.cancellable and \
            not self.current.handle.cancel_requested

    def busy_reason(self) -> str:
        return f"Another operation is running: {self.current.spec.title}." if self.current else ""

    # --- control ---
    def start(self, spec: jobs.JobSpec, on_done=None) -> bool:
        """Start an operation; False (and a message) when another one is running."""
        if self.current is not None:
            self.window.show_status(f"{self.busy_reason()} Wait for it to finish.")
            return False
        running = Running(spec, on_done)
        self.current = running
        self.last = None
        self.window.runner.submit(self.window.backend.run_job, spec,
                                  lambda event, r=running: self._relay.emit(r, event), running.handle,
                                  on_done=lambda result, r=running: self._done(r, result),
                                  on_error=lambda exc, r=running: self._done(r, jobs.Outcome(
                                      False, "FAILED", user_message(exc))))
        self.changed.emit()
        return True

    def cancel(self) -> bool:
        if not self.cancellable():
            return False
        sent = self.current.handle.cancel()
        self.changed.emit()
        return sent

    def stop_for_close(self) -> bool:
        """Before the window closes: end a cancellable operation; False when one must finish first."""
        if self.current is None:
            return True
        if self.current.state.cancellable:
            self.current.handle.cancel()
            self.current.handle.kill()
            return True
        return False

    # --- delivery (GUI thread) ---
    def _event(self, running: Running, event: jobs.Event) -> None:
        if running is not self.current:
            return                          # a late line of a finished operation
        running.state.feed(event)
        self.changed.emit()

    def _done(self, running: Running, outcome: jobs.Outcome) -> None:
        if running is not self.current:
            return
        self.current = None
        self.last = (running.spec, outcome, running.on_done)
        self.changed.emit()
        self.finished.emit(running.spec, outcome)
        if running.on_done is not None:
            running.on_done(outcome)


class JobPanel(Card):
    """Progress and outcome of the operations a page starts (hidden while it has nothing to show)."""

    def __init__(self, page_key: str, controller: JobController, parent: QWidget | None = None):
        super().__init__(parent=parent, margins=14, spacing=8)
        self.page_key, self.controller = page_key, controller
        self.title = title_label("", 1.0, self)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setAccessibleName("Progress of the operation")
        self.message = muted_label("", parent=self)
        row = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setIcon(icon("process-stop", "dialog-cancel", fallback=SP.SP_DialogCancelButton))
        self.cancel_button.setAccessibleName("Cancel the operation")
        self.cancel_button.clicked.connect(self._cancel)
        self.retry_button = QPushButton("Try Again", self)
        self.retry_button.setIcon(icon("view-refresh", fallback=SP.SP_BrowserReload))
        self.retry_button.setAccessibleName("Try the operation again")
        self.retry_button.clicked.connect(self._retry)
        self.result = MessageBanner(closable=True, parent=self)
        self.detail = plain_label("", wrap=True, selectable=True, parent=self)
        self.detail.setAccessibleName("Details of the outcome")
        row.addWidget(self.message, 1)
        row.addWidget(self.cancel_button)
        row.addWidget(self.retry_button)
        self.addWidget(self.title)
        self.addWidget(self.bar)
        self.addLayout(row)
        self.addWidget(self.result)
        self.addWidget(self.detail)
        self.shown: tuple[jobs.JobSpec, jobs.Outcome, object] | None = None
        self.retry: tuple[jobs.JobSpec, object] | None = None
        self.result.closed.connect(self._dismissed)
        controller.changed.connect(self.render)
        self.render()

    def render(self) -> None:
        current = self.controller.current
        last = self.controller.last
        mine_running = current is not None and current.spec.origin == self.page_key
        if last is not None and last[0].origin == self.page_key:
            self.shown = last
        if mine_running:
            self.shown = None
            state = current.state
            self.title.setText(current.spec.title)
            if state.percent is None:
                self.bar.setRange(0, 0)                  # busy indicator
            else:
                self.bar.setRange(0, 100)
                self.bar.setValue(int(state.percent))
            text = state.progress_text
            if current.handle.cancel_requested:
                text = "Cancelling…" if state.cancellable else text
            if state.warnings:
                text += f" — {state.warnings[-1]}"
            self.message.setText(text)
            self.cancel_button.setVisible(True)
            can_cancel = self.controller.cancellable()
            self.cancel_button.setEnabled(can_cancel)
            self.cancel_button.setToolTip("" if can_cancel else
                                          ("Cancelling…" if current.handle.cancel_requested else
                                           "This step cannot be cancelled safely; it will finish on its own."))
            self.retry_button.hide()
            self.result.hide()
            self.detail.hide()
            for widget in (self.title, self.bar, self.message):
                widget.show()
            self.show()
            return
        self.bar.setRange(0, 100)
        self.cancel_button.hide()
        if self.shown is None:
            self.hide()
            return
        spec, outcome, on_done = self.shown
        self.title.setText(spec.title)
        self.bar.hide()
        self.message.hide()
        level = "ok" if outcome.ok else ("warning" if outcome.code == "CANCELLED" or outcome.auth_denied else "error")
        self.result.show_message(level, outcome.message)
        self.detail.setText(outcome.detail if not outcome.ok else "")
        self.detail.setVisible(bool(outcome.detail) and not outcome.ok)
        retry = outcome.retryable and not outcome.ok
        self.retry = (spec, on_done) if retry else None
        self.retry_button.setVisible(retry)
        self.retry_button.setEnabled(retry and not self.controller.busy())
        self.retry_button.setToolTip(self.controller.busy_reason() if self.controller.busy() else "")
        self.show()

    def _cancel(self) -> None:
        self.controller.cancel()

    def _retry(self) -> None:
        if self.retry is not None:
            spec, on_done = self.retry
            self.controller.start(spec, on_done)

    def _dismissed(self) -> None:
        self.shown = None
        if self.controller.last is not None and self.controller.last[0].origin == self.page_key:
            self.controller.last = None
        self.render()
