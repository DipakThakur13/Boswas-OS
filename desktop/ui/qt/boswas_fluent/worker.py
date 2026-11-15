"""Work off the GUI thread: Worker and run_in_thread.

A blocking call (reading a directory tree, talking to a service, decoding
an image) must never run on the GUI thread. ``Worker.submit`` runs it on a
thread pool and delivers the result, or the exception it raised, to a
callback **on the GUI thread**, through a queued signal. Widgets are only
ever touched from those callbacks.

The function you submit runs on another thread: it must not touch widgets,
and whatever it shares with the GUI thread needs its own locking.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, Qt, QThreadPool, Signal, Slot


class _Task(QRunnable):
    def __init__(self, worker: "Worker", task_id: int, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        super().__init__()
        self._worker, self._task_id = worker, task_id
        self._fn, self._args, self._kwargs = fn, args, kwargs

    def run(self) -> None:
        try:
            ok, payload = True, self._fn(*self._args, **self._kwargs)
        except Exception as exc:          # delivered to the caller's error callback
            ok, payload = False, exc
        try:
            self._worker.finished.emit(self._task_id, ok, payload)
        except RuntimeError:              # the worker was deleted while the call ran
            pass


class Worker(QObject):
    """Runs blocking calls on a private thread pool; callbacks run on the GUI thread.

    One Worker per window is the usual arrangement: give it the window as
    parent and call ``shutdown()`` when the window closes, so no callback
    arrives afterwards.
    """

    finished = Signal(int, bool, object)       # task ID, succeeded, result or exception

    def __init__(self, parent: QObject | None = None, max_threads: int = 4) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(1, max_threads))
        self._callbacks: dict[int, tuple] = {}
        self._keys: dict[str, int] = {}
        self._next = 0
        self._closed = False
        self.finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(self, fn: Callable[..., Any], *args: Any, on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[Exception], None] | None = None, key: str | None = None,
               **kwargs: Any) -> int | None:
        """Run ``fn(*args, **kwargs)`` on the pool; returns a task ID.

        ``on_done(result)`` or ``on_error(exception)`` is called on the GUI
        thread. With a ``key`` the call is skipped (None is returned) while an
        earlier call with the same key is still running, so refreshes do not
        pile up. After ``shutdown()`` nothing is started.
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
        """Whether a call submitted with ``key`` has not been delivered yet."""
        return key in self._keys

    def pending(self) -> int:
        """How many submitted calls have not been delivered yet."""
        return len(self._callbacks)

    def running(self) -> bool:
        return self.pool.activeThreadCount() > 0

    def discard(self, task_id: int) -> None:
        """Forget the callbacks of a task: it still runs to its end, but nobody is told."""
        on_done, on_error, key = self._callbacks.get(task_id, (None, None, None))
        if task_id in self._callbacks:
            self._callbacks[task_id] = (None, None, key)

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
        """Process events until every submitted call has been delivered (tests, orderly shutdown)."""
        deadline = time.monotonic() + timeout
        while self._callbacks and time.monotonic() < deadline:
            self.pool.waitForDone(20)
            QCoreApplication.processEvents()
        return not self._callbacks

    def shutdown(self, timeout_ms: int = 2000) -> None:
        """Drop pending callbacks, start nothing more and wait briefly for the calls that are running."""
        self._closed = True
        self.pool.clear()
        self.pool.waitForDone(timeout_ms)
        self._callbacks.clear()
        self._keys.clear()


_shared: Worker | None = None


def shared_worker() -> Worker:
    """The Worker behind ``run_in_thread`` (created on first use, shut down when the application quits)."""
    global _shared
    if _shared is None or _shared._closed:
        _shared = Worker()
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(_shared.shutdown)
    return _shared


def run_in_thread(fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
                  on_error: Callable[[Exception], None] | None = None) -> int | None:
    """Run ``fn()`` off the GUI thread; ``on_done(result)`` or ``on_error(exception)`` runs on the GUI thread.

    For a one-off call. A window with many calls should own a ``Worker``.
    """
    return shared_worker().submit(fn, on_done=on_done, on_error=on_error)
