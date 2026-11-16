"""Running the archive engine off the GUI thread: progress and questions cross over as signals.

A ``Job`` runs one engine call (open, extract, create, add, delete, test,
read an entry) on a thread of a ``fluent.Worker``. The engine reports
progress and asks its questions (overwrite? unusually large? password?) on
that thread; ``JobHandler`` passes them to the GUI thread as queued signals
and makes the engine wait for ``answer()``. Cancelling sets an event the
engine looks at between blocks of data, and also releases a pending question.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from . import extract

OVERWRITE, SIZE, PASSWORD = "overwrite", "size", "password"
_PROGRESS_INTERVAL = 0.04          # seconds between progress signals: enough for a smooth bar, cheap for the GUI


class JobHandler(QObject):
    """The ``Handler`` of extraction and creation, speaking Qt signals.

    ``progressed(Progress)`` and ``asked(kind, payload)`` arrive on the GUI
    thread. After ``asked`` the engine waits until ``answer(value)``:
    ``overwrite`` wants one of the ``extract`` decisions, ``size`` a bool,
    ``password`` a string or None.
    """

    progressed = Signal(object)
    asked = Signal(str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cancel = threading.Event()
        self._answered = threading.Event()
        self._answer: Any = None
        self._last = 0.0
        self.latest = None

    # --- engine thread ---
    def progress(self, info) -> None:
        self.latest = info
        now = time.monotonic()
        if now - self._last >= _PROGRESS_INTERVAL:
            self._last = now
            self.progressed.emit(info)

    def _ask(self, kind: str, payload: object) -> Any:
        self._answered.clear()
        self._answer = None
        self.asked.emit(kind, payload)
        while not self._answered.wait(0.05):
            if self.cancel.is_set():
                return None
        return self._answer

    def overwrite(self, conflict) -> str:
        return self._ask(OVERWRITE, conflict) or extract.CANCEL

    def confirm_size(self, warning) -> bool:
        return bool(self._ask(SIZE, warning))

    def password(self, wrong: bool) -> str | None:
        answer = self._ask(PASSWORD, wrong)
        return answer if isinstance(answer, str) and answer else None

    # --- GUI thread ---
    def answer(self, value: Any) -> None:
        self._answer = value
        self._answered.set()

    def stop(self) -> None:
        """Cancel the operation; a question that is waiting is answered with "cancel"."""
        self.cancel.set()
        self._answered.set()


class Job(QObject):
    """One engine call on a worker thread.

    ``finished(result)`` or ``failed(exception)`` arrives on the GUI thread
    (never both, and neither after ``abandon()``).
    """

    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, worker, function: Callable[..., Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.handler = JobHandler(self)
        self._worker = worker
        self._function = function
        self._task: int | None = None
        self.done = False

    def start(self) -> "Job":
        self._task = self._worker.submit(self._function, self.handler, on_done=self._done, on_error=self._error)
        return self

    def _done(self, result: object) -> None:
        self.done = True
        self.finished.emit(result)

    def _error(self, error: Exception) -> None:
        self.done = True
        self.failed.emit(error)

    def cancel(self) -> None:
        self.handler.stop()

    def abandon(self) -> None:
        """Cancel and never report back (the window is closing)."""
        self.handler.stop()
        if self._task is not None:
            self._worker.discard(self._task)
