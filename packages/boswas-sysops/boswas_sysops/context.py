"""What an operation works with, and cancellation.

Context bundles the file system root, the process runner, the event
emitter, the cancellation request and the requesting user, so the tests
replace each of them. Reporter turns tool output into throttled progress
events and implements the cancellation rule of the contract: a cancel
request is honoured only while nothing has been changed; once a changing
phase begins (dpkg unpack/configure, image extraction) it is refused with a
warning and the operation runs to completion.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from .protocol import Emitter
from .rootfs import Root
from .runner import Runner

CANCEL_REFUSED = "This step cannot be cancelled while the system is being changed; waiting for it to finish."


class CancelRequest:
    def __init__(self):
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def requested(self) -> bool:
        return self._event.is_set()

    def clear(self) -> None:
        self._event.clear()


def watch_stdin(stream, cancel: CancelRequest) -> threading.Thread:
    """Read the caller's stdin in the background: a line "cancel" requests cancellation; EOF is not a cancel.

    A real stream is read from its file descriptor with os.read: a daemon
    thread blocked inside a buffered reader would make the interpreter abort
    at exit (it cannot take the reader's lock), turning a finished operation
    into a crash. Tests pass a plain iterable of lines.
    """

    def lines():
        fileno = getattr(stream, "fileno", None)
        if fileno is None:
            yield from stream
            return
        fd, pending = fileno(), b""
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                return
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield line
            pending = pending[-4096:]

    def loop() -> None:
        try:
            for raw in lines():
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                if line.strip() == "cancel":
                    cancel.request()
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=loop, name="sysops-stdin", daemon=True)
    thread.start()
    return thread


@dataclass
class Context:
    root: Root
    runner: Runner
    emit: Emitter
    cancel: CancelRequest
    uid: int | None
    kernel_release: str
    cancellable: bool = False


class Reporter:
    """Progress events for one long command, with the cancellation state machine."""

    MIN_STEP = 1.0           # percent
    MIN_INTERVAL = 1.0       # seconds between events of the same phase

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.changing = False
        self._last: tuple[str | None, float | None, float] = (None, None, 0.0)

    def _cancellable(self) -> bool:
        return self.ctx.cancellable and not self.changing

    def report(self, phase: str, percent: float | None, message: str = "", force: bool = False) -> None:
        now = time.monotonic()
        last_phase, last_percent, last_time = self._last
        if (not force and phase == last_phase and now - last_time < self.MIN_INTERVAL
                and (percent is None or last_percent is None or abs(percent - last_percent) < self.MIN_STEP)):
            return
        self._last = (phase, percent, now)
        self.ctx.emit.progress(phase, percent, message, cancellable=self._cancellable())

    def changing_begins(self, phase: str, percent: float | None, message: str = "") -> None:
        if not self.changing:
            self.changing = True
            self.report(phase, percent, message, force=True)

    def poll(self) -> bool:
        """Called by the runner a few times a second: True = stop the command now."""
        if not self.ctx.cancel.requested():
            return False
        self.ctx.cancel.clear()
        if self._cancellable():
            return True
        self.ctx.emit.log(CANCEL_REFUSED, "warning")
        return False
