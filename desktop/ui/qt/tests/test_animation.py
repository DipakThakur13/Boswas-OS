"""fluent.Animation: values over time, at the refresh rate of the screen (Qt, offscreen)."""

from __future__ import annotations

import re
import time
import unittest
from unittest import mock

import support

if support.PYSIDE:
    from PySide6.QtCore import QEasingCurve, QPointF, QRectF
    from PySide6.QtGui import QScreen
    from PySide6.QtWidgets import QWidget

    import boswas_fluent as fluent
    from boswas_fluent import testing

    Base = testing.FluentTestCase
else:
    Base = unittest.TestCase


class Clock:
    """A monotonic clock the test moves by hand (seconds)."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Screen:
    """Stands in for a QScreen with a refresh rate."""

    def __init__(self, rate: float) -> None:
        self.rate = rate

    def refreshRate(self) -> float:  # noqa: N802 (Qt's name)
        return self.rate


def steps_in(animation, seconds: float) -> int:
    """How many values the running animation emits in ``seconds`` of real time."""
    values = []
    animation.valueChanged.connect(values.append)
    testing.process(round(seconds * 1000))
    return len(values)


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class AnimationTest(Base):
    def make(self, start=0.0, end=1.0, duration=200, *, parent=None, clock=None):
        animation = fluent.Animation(parent if parent is not None else self.keep(QWidget()), clock=clock)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setDuration(duration)
        return animation

    def test_values_come_from_the_time_since_start(self):
        clock = Clock()
        animation = self.make(0.0, 10.0, 200, clock=clock)
        values, finished = [], []
        animation.valueChanged.connect(values.append)
        animation.finished.connect(lambda: finished.append(True))
        animation.start()
        self.assertEqual(animation.state(), fluent.Animation.State.Running)
        self.assertEqual(values, [0.0], "the start value at once")
        clock.now += 0.05
        animation._step()
        self.assertAlmostEqual(values[-1], 2.5)
        # A step that comes late is where the time says, not one step further.
        clock.now += 0.1
        animation._step()
        self.assertAlmostEqual(values[-1], 7.5)
        self.assertEqual(finished, [])
        clock.now += 0.2
        animation._step()
        self.assertEqual(values[-1], 10.0, "the end value, exactly")
        self.assertEqual(finished, [True])
        self.assertEqual(animation.state(), fluent.Animation.State.Stopped)
        self.assertEqual(animation.currentValue(), 10.0)

    def test_easing_key_values_loops_and_direction(self):
        clock = Clock()
        animation = self.make(0.0, 100.0, 100, clock=clock)
        animation.setEasingCurve(fluent.easing("decelerate"))
        values = []
        animation.valueChanged.connect(values.append)
        animation.start()
        clock.now += 0.03
        animation._step()
        eased = fluent.easing("decelerate").valueForProgress(0.3) * 100
        self.assertAlmostEqual(values[-1], eased)
        self.assertGreater(values[-1], 30, "decelerating: ahead of the clock early on")
        animation.stop()

        # A pulse that runs until stopped: 1.0 -> 0.45 -> 1.0 every 1.4 s.
        pulse = self.make(1.0, 1.0, 1400, clock=clock)
        pulse.setKeyValueAt(0.5, 0.45)
        pulse.setLoopCount(-1)
        finished = []
        pulse.finished.connect(lambda: finished.append(True))
        pulse.start()
        clock.now += 0.7
        pulse._step()
        self.assertAlmostEqual(pulse.currentValue(), 0.45)
        clock.now += 0.35
        pulse._step()
        self.assertAlmostEqual(pulse.currentValue(), 0.725)
        clock.now += 1.4 * 10
        pulse._step()
        self.assertEqual(pulse.state(), fluent.Animation.State.Running, "loops until stopped")
        pulse.stop()
        self.assertEqual((pulse.state(), finished), (fluent.Animation.State.Stopped, []), "stop() does not finish")

        # Two loops, then it ends; backwards it runs from the end value to the start value.
        twice = self.make(0.0, 1.0, 100, clock=clock)
        twice.setLoopCount(2)
        twice.setDirection(fluent.Animation.Direction.Backward)
        twice.start()
        self.assertEqual(twice.currentValue(), 1.0)
        clock.now += 0.125
        twice._step()
        self.assertAlmostEqual(twice.currentValue(), 0.75)
        clock.now += 0.1
        twice._step()
        self.assertEqual((twice.state(), twice.currentValue()), (fluent.Animation.State.Stopped, 0.0))

    def test_numbers_points_and_rectangles_move(self):
        clock = Clock()
        whole = self.make(0, 101, 100, clock=clock)
        whole.start()
        clock.now += 0.05
        whole._step()
        self.assertEqual(whole.currentValue(), 50)
        self.assertIsInstance(whole.currentValue(), int)
        box = self.make(QRectF(0, 0, 10, 10), QRectF(100, 50, 30, 20), 100, clock=clock)
        box.start()
        clock.now += 0.05
        box._step()
        self.assertEqual(box.currentValue(), QRectF(50, 25, 20, 15))
        point = self.make(QPointF(0, 0), QPointF(10, -10), 100, clock=clock)
        point.start()
        clock.now += 0.025
        point._step()
        self.assertEqual(point.currentValue(), QPointF(2.5, -2.5))
        animation = fluent.Animation()
        for value in ("left", True, None, [1, 2]):
            with self.assertRaises(TypeError, msg=repr(value)):
                animation.setStartValue(value)
        with self.assertRaises(ValueError):
            fluent.Animation().start()

    def test_reduced_motion_goes_straight_to_the_end(self):
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(fluent.manager().set_reduced_motion, None)
        animation = self.make(0.0, 1.0, 5000)
        values, finished = [], []
        animation.valueChanged.connect(values.append)
        animation.finished.connect(lambda: finished.append(True))
        animation.start()
        self.assertEqual((values, finished), ([1.0], [True]), "at once, within start()")
        self.assertEqual(animation.state(), fluent.Animation.State.Stopped)
        # A duration of 0 is the same, with motion.
        fluent.manager().set_reduced_motion(False)
        instant = self.make(0.0, 1.0, 0)
        instant.start()
        self.assertEqual((instant.currentValue(), instant.state()), (1.0, fluent.Animation.State.Stopped))

    def test_it_steps_at_the_refresh_rate_of_the_screen(self):
        widget = self.show(QWidget(), 100, 100)
        self.assertEqual(fluent.Animation(widget).refresh_rate(), 60.0, "the offscreen screen reports 60 Hz")
        with mock.patch.object(QScreen, "refreshRate", lambda _screen: 144.0):
            animation = self.make(0.0, 1.0, 10_000, parent=widget)
            self.assertEqual(animation.refresh_rate(), 144.0)
            animation.start()
            steps = steps_in(animation, 0.5)
            animation.stop()
        self.assertTrue(60 <= steps <= 76, f"{steps} steps in half a second at 144 Hz")
        # Not 60 Hz whatever the screen: back to 60, half a second has about 30 steps.
        animation = self.make(0.0, 1.0, 10_000, parent=widget)
        animation.start()
        steps = steps_in(animation, 0.5)
        animation.stop()
        self.assertTrue(24 <= steps <= 32, f"{steps} steps in half a second at 60 Hz")

    def test_the_pace_follows_the_window_to_another_screen(self):
        widget = self.show(QWidget(), 100, 100)
        screen = Screen(240.0)
        widget.screen = lambda: screen               # the window is on a 240 Hz monitor
        animation = self.make(0.0, 1.0, 10_000, parent=widget)
        animation.start()
        self.assertEqual(animation.refresh_rate(), 240.0)
        fast = steps_in(animation, 0.3)
        screen.rate = 60.0                           # moved to a 60 Hz one (or the mode changed)
        slow = steps_in(animation, 0.3)
        animation.stop()
        self.assertTrue(55 <= fast <= 76, f"{fast} steps in 0.3 s at 240 Hz")
        self.assertTrue(14 <= slow <= 20, f"{slow} steps in 0.3 s at 60 Hz")
        # A child object's animation goes by the widget it belongs to.
        holder = fluent.Animation(fluent.Animation(widget))
        self.assertIs(holder.widget(), widget)

    def test_reported_rates_are_kept_within_bounds(self):
        widget = self.keep(QWidget())
        screen = Screen(0.0)
        widget.screen = lambda: screen
        for reported, used in ((0.0, 60.0), (-1.0, 60.0), (float("nan"), 60.0), (float("inf"), 60.0), (10.0, 60.0),
                               (24.0, 24.0), (59.94, 59.94), (165.0, 165.0), (360.0, 360.0), (1000.0, 500.0)):
            screen.rate = reported
            self.assertEqual(fluent.refresh_rate(widget), used, reported)
        self.assertEqual(fluent.refresh_rate(None), 60.0, "the primary screen")

    def test_late_steps_do_not_slow_it_down(self):
        # Half a second, while the application is too busy to step for a third of it.
        animation = self.make(0.0, 1.0, 300)
        finished = []
        animation.finished.connect(lambda: finished.append(time.monotonic()))
        started = time.monotonic()
        animation.start()
        time.sleep(0.1)                              # blocked: no step can run
        self.assertTrue(testing.wait_until(lambda: finished, 2))
        self.assertLess(finished[0] - started, 0.36, "it ends on time all the same")


@unittest.skipUnless(support.PYSIDE, support.SKIP_QT)
class WidgetsTest(Base):
    def test_toolkit_widgets_move_at_the_refresh_rate(self):
        switch = self.show(fluent.ToggleSwitch())
        positions = []
        with mock.patch.object(QScreen, "refreshRate", lambda _screen: 240.0):
            switch.setChecked(True)
            switch._animation.valueChanged.connect(positions.append)
            self.assertTrue(testing.wait_until(lambda: switch.knob_position() == 1.0, 2))
        # 150 ms at 240 Hz: about 36 steps (Qt's own animations would make 9).
        self.assertGreater(len(positions), 20, positions)
        self.assertEqual(positions[-1], 1.0)

        ring = self.show(fluent.ProgressRing())
        self.assertTrue(ring.is_animating())
        with mock.patch.object(QScreen, "refreshRate", lambda _screen: 144.0):
            phases = []
            ring._spin.valueChanged.connect(phases.append)
            testing.process(250)
        self.assertGreater(len(phases), 25, "a quarter of a second at 144 Hz")

    def test_no_widget_animates_on_qt_animation_timers(self):
        # Qt's animation classes step at 60 Hz; fixed 16 or 33 ms timers do too.
        package = support.QT_DIR / "boswas_fluent"
        fixed = re.compile(r"(setInterval|start)\(\s*(16|17|33|34)\s*\)|_MS\s*=\s*(16|17|33|34)\b")
        for path in sorted(package.rglob("*.py")):
            if path.name == "animation.py":
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                self.assertNotRegex(text, r"\bQ(Variant|Property)Animation\b|QTimeLine|AnimationGroup")
                self.assertIsNone(fixed.search(text))


if __name__ == "__main__":
    unittest.main()
