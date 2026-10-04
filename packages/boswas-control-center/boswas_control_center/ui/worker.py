"""Blocking calls off the GUI thread (the Compatibility Manager's Runner).

Reading the security status or the session agent can take seconds, so
widgets never call the backend directly. Runner executes the call on its own
QThreadPool and delivers the result or the exception back on the GUI thread
through a queued signal, where the caller's callback runs.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, Qt, QThreadPool, Signal, Slot


class _Task(QRunnable):
    def __init__(self, runner: "Runner", task_id: int, fn, args: tuple, kwargs: dict):
        super().__init__()
        self._runner, self._task_id = runner, task_id
        self._fn, self._args, self._kwargs = fn, args, kwargs

    def run(self) -> None:
        try:
            ok, payload = True, self._fn(*self._args, **self._kwargs)
        except Exception as exc:          # delivered to the caller's error callback
            ok, payload = False, exc
        try:
            self._runner.finished.emit(self._task_id, ok, payload)
        except RuntimeError:              # the window was closed while the call ran
            pass


class Runner(QObject):
    """Runs blocking calls on a private thread pool; callbacks run on the GUI thread."""

    finished = Signal(int, bool, object)

    def __init__(self, parent: QObject | None = None, max_threads: int = 6):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max_threads)
        self._callbacks: dict[int, tuple] = {}
        self._keys: dict[str, int] = {}
        self._next = 0
        self._closed = False
        self.finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(self, fn, *args, on_done=None, on_error=None, key: str | None = None, **kwargs) -> int | None:
        """Run fn(*args, **kwargs) in the pool.

        With a key, the call is skipped while an earlier call with the same
        key is still running (refreshes must not pile up).
        """
        if self._closed or (key is not None and key in self._keys):
            return None
        self._next += 1
        task_id = self._next
        self._callbacks[task_id] = (on_done, on_error, key)
        if key is not None:
            self._keys[key] = task_id
        self.pool.start(_Task(self, task_id, fn, args, kwargs))
        return task_id

    def busy(self, key: str) -> bool:
        return key in self._keys

    def pending(self) -> int:
        return len(self._callbacks)

    def running(self) -> bool:
        return self.pool.activeThreadCount() > 0

    @Slot(int, bool, object)
    def _deliver(self, task_id: int, ok: bool, payload: object) -> None:
        on_done, on_error, key = self._callbacks.pop(task_id, (None, None, None))
        if key is not None and self._keys.get(key) == task_id:
            del self._keys[key]
        if self._closed:
            return
        if ok and on_done is not None:
            on_done(payload)
        elif not ok and on_error is not None:
            on_error(payload)

    def wait(self, timeout: float = 10.0) -> bool:
        """Process events until every submitted call has been delivered (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        while self._callbacks and time.monotonic() < deadline:
            self.pool.waitForDone(20)
            QCoreApplication.processEvents()
        return not self._callbacks

    def shutdown(self, timeout_ms: int = 2000) -> None:
        """Drop pending callbacks and wait briefly for running calls."""
        self._closed = True
        self.pool.clear()
        self.pool.waitForDone(timeout_ms)
        self._callbacks.clear()
        self._keys.clear()
