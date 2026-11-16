"""The sampler on its own thread.

``Sampling`` owns a thread that calls ``Sampler.tick`` at the chosen
interval and hands each ``Sample`` to the GUI thread through the signal
``sampled``. The GUI says what it wants with ``configure``: which parts,
how often, and whether to sample at all (not while the window is hidden
or minimised). A new sample is not taken while the previous one has not
been taken off the queue by the GUI (``done``), so a busy GUI is never
flooded.

With ``threaded=False`` (tests) there is no thread and no timer: a
sample is taken, synchronously, when ``request`` is called or the parts
change.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal

from ..sampler import Sampler


class _Thread(QThread):
    def __init__(self, owner: "Sampling") -> None:
        super().__init__(owner)
        self._owner = owner

    def run(self) -> None:
        self._owner._loop()


class Sampling(QObject):
    sampled = Signal(object)                  # a sampler.Sample

    def __init__(self, make_sampler: Callable[[], Sampler], threaded: bool = True, parent: QObject | None = None):
        super().__init__(parent)
        self._make = make_sampler
        self._threaded = threaded
        self._condition = threading.Condition()
        self._parts: frozenset[str] = frozenset()
        self._interval: float | None = 1.0
        self._active = False
        self._force = False
        self._stop = False
        self._waiting_for_gui = False
        self._thread: _Thread | None = None
        self._sampler: Sampler | None = None
        self.ticks = 0                        # samples taken (tests, and the cost measurement)

    def parts(self) -> frozenset[str]:
        return self._parts

    def is_active(self) -> bool:
        return self._active

    def interval(self) -> float | None:
        return self._interval

    def start(self) -> None:
        if self._threaded and self._thread is None:
            self._thread = _Thread(self)
            self._thread.start()

    def configure(self, parts: frozenset[str] | set[str] | None = None, interval: float | None | bool = False,
                  active: bool | None = None) -> None:
        """Change what is sampled (``parts``), how often (``interval`` seconds, None: only on request) and
        whether at all (``active``). Arguments left out keep their value. New parts are sampled at once."""
        with self._condition:
            sample_now = False
            if parts is not None and frozenset(parts) != self._parts:
                self._parts = frozenset(parts)
                sample_now = True
            if interval is not False:
                self._interval = interval
            if active is not None and active != self._active:
                self._active = active
                sample_now = sample_now or active
            if sample_now and self._active:
                self._force = True
            self._condition.notify_all()
        if not self._threaded and sample_now and self._active:
            self._sample_inline()

    def request(self) -> None:
        """Take one sample now (F5, and the only way to get one while sampling is paused)."""
        if not self._threaded:
            self._sample_inline()
            return
        with self._condition:
            self._force = True
            self._condition.notify_all()

    def done(self) -> None:
        """The GUI has dealt with the last sample."""
        with self._condition:
            self._waiting_for_gui = False
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.wait(3000)
            self._thread = None
        if self._sampler is not None and not self._threaded:
            self._sampler.close()
            self._sampler = None

    def _sample_inline(self) -> None:
        if self._sampler is None:
            self._sampler = self._make()
        self._force = False
        self.ticks += 1
        self.sampled.emit(self._sampler.tick(self._parts))

    def _loop(self) -> None:
        sampler = self._make()
        due = time.monotonic()
        try:
            while True:
                with self._condition:
                    while not self._stop:
                        now = time.monotonic()
                        if self._waiting_for_gui:
                            self._condition.wait(1.0)
                            if self._waiting_for_gui and time.monotonic() - now >= 1.0:
                                self._waiting_for_gui = False       # the GUI did not answer: do not stall
                            continue
                        if self._force:
                            break
                        if self._active and self._interval is not None:
                            if now >= due:
                                break
                            self._condition.wait(due - now)
                        else:
                            self._condition.wait()
                    if self._stop:
                        return
                    self._force = False
                    parts, interval = self._parts, self._interval
                    self._waiting_for_gui = True
                sample = sampler.tick(parts)
                self.ticks += 1
                self.sampled.emit(sample)
                now = time.monotonic()
                if interval is not None:
                    due = max(due + interval, now + interval / 2) if due + interval > now - interval else now + interval
        finally:
            sampler.close()
