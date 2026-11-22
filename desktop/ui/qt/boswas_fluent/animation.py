"""Animations that move at the refresh rate of the screen they are shown on.

Qt's own animations (``QVariantAnimation``, ``QPropertyAnimation``) all
advance on one application timer of 16 ms: 60 steps a second, also on a
monitor that shows 144 or 240. PySide6 exposes neither ``QAnimationDriver``
nor ``QUnifiedTimer``, which would change that, so the toolkit has its own:
``Animation``, the part of ``QVariantAnimation`` that Boswas widgets use,
stepping once per refresh of the screen its widget is on.

    animation = fluent.Animation(self)              # parent: the widget that moves
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setDuration(fluent.duration("fast"))
    animation.setEasingCurve(fluent.easing("decelerate"))
    animation.valueChanged.connect(self._set_position)
    animation.finished.connect(self._arrived)
    animation.start()

* The value of a step comes from the time since ``start()`` on a monotonic
  clock, so a step that comes late never slows the animation down, and the
  last value is the end value exactly.
* Steps keep a fixed pace of one refresh (``QScreen.refreshRate()`` of the
  widget's screen, read again at every step, so a window moved to another
  monitor follows that monitor), on a precise timer.
* With reduced motion (the desktop's setting) ``start()`` goes straight to
  the end: ``valueChanged`` with the end value, then ``finished``.

Values: ``int``, ``float``, ``QPointF`` and ``QRectF``.
"""

from __future__ import annotations

import math
import time
from typing import Callable

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from .theming import manager

FALLBACK_RATE = 60.0            # Hz, when the screen reports no usable rate
MIN_RATE = 24.0                 # slower than this a screen is taken not to know its rate
MAX_RATE = 500.0                # no animation steps faster than this, whatever a screen reports


def refresh_rate(widget: QWidget | None) -> float:
    """The refresh rate (Hz) animations of ``widget`` step at: its screen's, else the primary screen's."""
    screen = widget.screen() if widget is not None else None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    rate = screen.refreshRate() if screen is not None else 0.0
    if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < MIN_RATE:
        return FALLBACK_RATE
    return min(float(rate), MAX_RATE)


def _interpolate(start: object, end: object, progress: float) -> object:
    if isinstance(start, QRectF) and isinstance(end, QRectF):
        return QRectF(start.x() + (end.x() - start.x()) * progress, start.y() + (end.y() - start.y()) * progress,
                      start.width() + (end.width() - start.width()) * progress,
                      start.height() + (end.height() - start.height()) * progress)
    if isinstance(start, QPointF) and isinstance(end, QPointF):
        return QPointF(start.x() + (end.x() - start.x()) * progress, start.y() + (end.y() - start.y()) * progress)
    value = start + (end - start) * progress
    return round(value) if isinstance(start, int) and isinstance(end, int) else float(value)


def _check(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float, QPointF, QRectF)):
        raise TypeError(f"an Animation moves int, float, QPointF or QRectF values, not {type(value).__name__}")
    return QRectF(value) if isinstance(value, QRectF) else QPointF(value) if isinstance(value, QPointF) else value


class Animation(QObject):
    """A value that moves from a start to an end value (through key values) over a duration.

    The subset of ``QVariantAnimation`` the toolkit and the applications use,
    with its names: ``setStartValue``, ``setEndValue``, ``setKeyValueAt``,
    ``setDuration`` (milliseconds), ``setEasingCurve``, ``setLoopCount``
    (-1: until stopped), ``setDirection``, ``start``, ``stop``, ``state``,
    ``currentValue``; signals ``valueChanged(value)`` and ``finished()``.
    ``clock`` (seconds, monotonic) is for tests.
    """

    State = QAbstractAnimation.State
    Direction = QAbstractAnimation.Direction

    valueChanged = Signal(object)
    finished = Signal()

    def __init__(self, parent: QObject | None = None, *, clock: Callable[[], float] | None = None) -> None:
        super().__init__(parent)
        self._clock = clock or time.monotonic
        self._keys: dict[float, object] = {}
        self._duration = 250
        self._easing = QEasingCurve(QEasingCurve.Type.Linear)
        self._loops = 1
        self._direction = QAbstractAnimation.Direction.Forward
        self._state = QAbstractAnimation.State.Stopped
        self._value: object = None
        self._started = 0.0
        self._length = 0.0                # seconds of one loop of the running animation
        self._due = 0.0                   # when the next step is due (clock seconds)
        self._timer: QTimer | None = None

    # --- values ---
    def setStartValue(self, value: object) -> None:
        self._keys[0.0] = _check(value)

    def startValue(self) -> object:
        return self._keys.get(0.0)

    def setEndValue(self, value: object) -> None:
        self._keys[1.0] = _check(value)

    def endValue(self) -> object:
        return self._keys.get(1.0)

    def setKeyValueAt(self, step: float, value: object) -> None:
        if not 0.0 <= step <= 1.0:
            raise ValueError("a key value's step is between 0.0 and 1.0")
        self._keys[float(step)] = _check(value)

    def currentValue(self) -> object:
        return self._value

    # --- timing ---
    def setDuration(self, milliseconds: int) -> None:
        if milliseconds < 0:
            raise ValueError("a duration is not negative")
        self._duration = int(milliseconds)

    def duration(self) -> int:
        return self._duration

    def setEasingCurve(self, curve: QEasingCurve | QEasingCurve.Type) -> None:
        self._easing = QEasingCurve(curve)

    def easingCurve(self) -> QEasingCurve:
        return QEasingCurve(self._easing)

    def setLoopCount(self, loops: int) -> None:
        self._loops = int(loops)

    def loopCount(self) -> int:
        return self._loops

    def setDirection(self, direction: QAbstractAnimation.Direction) -> None:
        self._direction = direction

    def direction(self) -> QAbstractAnimation.Direction:
        return self._direction

    def state(self) -> QAbstractAnimation.State:
        return self._state

    def widget(self) -> QWidget | None:
        """The widget the animation belongs to (its nearest widget parent): its screen sets the pace."""
        parent = self.parent()
        while parent is not None and not isinstance(parent, QWidget):
            parent = parent.parent()
        return parent

    def refresh_rate(self) -> float:
        """The steps a second the animation takes now: the refresh rate of its widget's screen."""
        return refresh_rate(self.widget())

    # --- running ---
    def start(self) -> None:
        """Start from the beginning (nothing happens while it runs)."""
        if self._state == QAbstractAnimation.State.Running:
            return
        if 0.0 not in self._keys or 1.0 not in self._keys:
            raise ValueError("an Animation needs a start and an end value")
        self._length = 0.0 if manager().reduced_motion else self._duration / 1000.0
        self._state = QAbstractAnimation.State.Running
        if self._length <= 0.0 or self._loops == 0:
            self._finish()
            return
        self._started = self._due = self._clock()
        self._emit(self._value_at(0.0))
        self._schedule()

    def stop(self) -> None:
        """Stop where it is; ``finished`` is not emitted."""
        self._state = QAbstractAnimation.State.Stopped
        if self._timer is not None:
            self._timer.stop()

    def _schedule(self) -> None:
        """Arm the timer for the next step, one refresh after the last one was due."""
        if self._state != QAbstractAnimation.State.Running:
            return
        interval = 1.0 / self.refresh_rate()
        now = self._clock()
        self._due += interval
        if self._due <= now:                      # steps missed while the application was busy are skipped
            self._due += (math.floor((now - self._due) / interval) + 1) * interval
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.setTimerType(Qt.TimerType.PreciseTimer)
            self._timer.timeout.connect(self._step)
        self._timer.start(max(0, round((self._due - now) * 1000)))

    def _step(self) -> None:
        if self._state != QAbstractAnimation.State.Running:
            return
        elapsed = self._clock() - self._started
        if self._loops > 0 and elapsed >= self._length * self._loops:
            self._finish()
            return
        self._emit(self._value_at(math.fmod(elapsed, self._length) / self._length))
        self._schedule()

    def _finish(self) -> None:
        self._state = QAbstractAnimation.State.Stopped
        if self._timer is not None:
            self._timer.stop()
        self._emit(self._value_at(1.0))
        self.finished.emit()

    # --- values along the way ---
    def _value_at(self, time_progress: float) -> object:
        """The value a fraction of the way through a loop (before easing), in the animation's direction."""
        t = min(max(time_progress, 0.0), 1.0)
        if self._direction == QAbstractAnimation.Direction.Backward:
            t = 1.0 - t
        if t in (0.0, 1.0):                       # the ends are the start and end values exactly
            return _check(self._keys[t])
        progress = self._easing.valueForProgress(t)
        steps = sorted(self._keys)
        # The pair of key values around the eased progress (the first or last pair beyond the ends,
        # for a curve that overshoots).
        index = next((i for i in range(1, len(steps) - 1) if progress < steps[i]), len(steps) - 1)
        low, high = steps[index - 1], steps[index]
        return _interpolate(self._keys[low], self._keys[high], (progress - low) / (high - low))

    def _emit(self, value: object) -> None:
        self._value = value
        self.valueChanged.emit(value)
