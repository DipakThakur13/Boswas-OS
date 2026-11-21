"""SnipFlow: one snip from the countdown to the new file in the Screenshots folder.

1. With a delay, a countdown ticks once a second (``countdown(seconds)``).
2. ``hide_requested``: the window gets out of the way. The desktop takes
   its frozen picture of the screen when the request arrives, so the
   request waits ``hide_ms`` for the compositor to draw the screen without
   the window.
3. On a worker thread: the Screenshots folder is noted, then the request
   is sent. A refusal ends the snip with ``failed(title, message)``.
4. Waiting: a QFileSystemWatcher on the folder (or on the nearest folder
   above it that exists) and a poll every ``poll_ms`` look for the new
   screenshot. After ``reshow_ms`` the window comes back
   (``show_requested``), under the desktop's capture view, so that its
   Cancel button is there.
5. Nobody reports that the user cancelled on the desktop. While the
   capture view is up it covers the screen and has the pointer and the
   keyboard, so the window gets no input at all; the first input it gets
   after coming back means the view has closed (confirmed or cancelled).
   The window reports that with ``overlay_closed()`` and a grace of
   ``grace_ms`` starts: a confirmed snip is saved well within it; when
   none has arrived by then, the snip was cancelled. (Whether the window
   is active says nothing: Qt calls it active as soon as it is shown,
   under the capture view too.)
6. ``finished(path)`` with the new file; ``ended("dismissed")`` when the
   grace ran out without one, ``ended("timeout")`` after ``timeout``
   seconds (the last resort), ``ended("cancelled")`` after ``cancel()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

import boswas_fluent as fluent

from .. import capture


class SnipFlow(QObject):
    countdown = Signal(int)          # seconds left
    hide_requested = Signal()
    show_requested = Signal()
    waiting = Signal()               # the desktop took the request
    finished = Signal(str)           # the path of the new screenshot
    failed = Signal(str, str)        # title, message
    ended = Signal(str)              # "cancelled", "dismissed" or "timeout"

    def __init__(self, client, parent: QObject | None = None, *,
                 folder: Callable[[], Path] = capture.find_screenshots_dir) -> None:
        super().__init__(parent)
        self.client = client
        self.folder = folder
        self.hide_ms = 300
        self.reshow_ms = 1500
        self.poll_ms = 500
        self.timeout = capture.WAIT_SECONDS
        self.grace_ms = 2500
        self.worker = fluent.Worker(self, max_threads=1)
        self.phase = "idle"              # idle, countdown, hiding, requesting, waiting
        self.mode = "region"
        self.directory: Path | None = None
        self._generation = 0
        self._left = 0
        self._waiter: capture.ScreenshotWaiter | None = None
        self._watcher: QFileSystemWatcher | None = None
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._count)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._send)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        self._reshow_timer = QTimer(self)
        self._reshow_timer.setSingleShot(True)
        self._reshow_timer.timeout.connect(self._reshow)
        self._grace = QTimer(self)
        self._grace.setSingleShot(True)
        self._grace.timeout.connect(self._grace_over)
        self._grace_expired = False

    def is_active(self) -> bool:
        return self.phase != "idle"

    def seconds_left(self) -> int:
        return self._left if self.phase == "countdown" else 0

    def start(self, mode: str, delay: int = 0) -> bool:
        """Start a snip of a ``region`` or the whole ``screen`` after ``delay`` seconds; False while one runs."""
        if self.is_active() or mode not in capture.MODES:
            return False
        self._generation += 1
        self.mode = mode
        if delay > 0:
            self.phase = "countdown"
            self._left = int(delay)
            self.countdown.emit(self._left)
            self._tick.start()
        else:
            self._capture()
        return True

    def cancel(self) -> None:
        if self.is_active():
            self._stop()
            self.ended.emit("cancelled")

    def shutdown(self) -> None:
        self._stop()
        self.worker.shutdown()

    # --- the steps ---
    def _count(self) -> None:
        self._left -= 1
        if self._left > 0:
            self.countdown.emit(self._left)
            return
        self._tick.stop()
        self._capture()

    def _capture(self) -> None:
        self.phase = "hiding"
        self._left = 0
        self.hide_requested.emit()
        self._hide_timer.start(self.hide_ms)

    def _send(self) -> None:
        self.phase = "requesting"
        generation, mode, folder, client, timeout = self._generation, self.mode, self.folder, self.client, self.timeout

        def job() -> tuple[capture.ScreenshotWaiter, Path | None]:
            waiter = capture.ScreenshotWaiter(folder(), timeout=timeout)
            waiter.start()
            client.request(mode)
            return waiter, capture.nearest_folder(waiter.directory)

        self.worker.submit(job, on_done=lambda result: self._requested(generation, *result),
                           on_error=lambda exc: self._request_failed(generation, exc))

    def _requested(self, generation: int, waiter: capture.ScreenshotWaiter, nearest: Path | None) -> None:
        if generation != self._generation or self.phase != "requesting":
            return
        self.phase = "waiting"
        self._waiter = waiter
        self.directory = waiter.directory
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._folder_changed)
        self._watch(nearest)
        self._poll_timer.start(self.poll_ms)
        self._reshow_timer.start(self.reshow_ms)
        self.waiting.emit()
        self.poll()

    def _request_failed(self, generation: int, exc: Exception) -> None:
        if generation != self._generation:
            return
        self._stop()
        if isinstance(exc, capture.CaptureError):
            message = str(exc)
        else:
            message = f"Something went wrong: {exc}"
        self.failed.emit("A snip could not be started", message)

    def _reshow(self) -> None:
        if self.phase == "waiting":
            self.show_requested.emit()

    def overlay_closed(self) -> None:
        """The window got input: while waiting, the desktop's capture view has closed."""
        if self.phase == "waiting" and not self._grace.isActive() and not self._grace_expired:
            self._grace.start(self.grace_ms)

    def _grace_over(self) -> None:
        self._grace_expired = True
        self.poll()                              # a last look; with nothing new the snip was cancelled

    # --- watching the folder ---
    def _watch(self, folder: Path | None) -> None:
        """Watch ``folder``: the Screenshots folder, or the nearest one above it while it does not exist yet."""
        if self._watcher is None or folder is None:
            return
        path = str(folder)
        watched = self._watcher.directories()
        if path not in watched:
            if watched:
                self._watcher.removePaths(watched)
            self._watcher.addPath(path)

    def _folder_changed(self, _path: str) -> None:
        self.poll()

    def poll(self) -> None:
        """Look for the new screenshot now (on the worker)."""
        if self.phase != "waiting" or self._waiter is None:
            return
        if self._waiter.expired():
            self._stop()
            self.ended.emit("timeout")
            return
        generation, waiter = self._generation, self._waiter

        def job() -> tuple[Path | None, Path | None]:
            return waiter.poll(), capture.nearest_folder(waiter.directory)

        self.worker.submit(job, key="poll", on_done=lambda result: self._polled(generation, *result),
                           on_error=lambda _exc: None)

    def _polled(self, generation: int, path: Path | None, nearest: Path | None) -> None:
        if generation != self._generation or self.phase != "waiting":
            return
        if path is None:
            if self._grace_expired:
                self._stop()
                self.ended.emit("dismissed")
                return
            self._watch(nearest)
            return
        self._stop()
        self.finished.emit(str(path))

    def _stop(self) -> None:
        self._generation += 1
        self.phase = "idle"
        self._left = 0
        self._grace_expired = False
        for timer in (self._tick, self._hide_timer, self._poll_timer, self._reshow_timer, self._grace):
            timer.stop()
        if self._watcher is not None:
            self._watcher.deleteLater()
            self._watcher = None
        self._waiter = None
