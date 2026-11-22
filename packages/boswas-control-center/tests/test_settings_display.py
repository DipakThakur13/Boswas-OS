"""The Display sections, offscreen: real widgets, real settings files in a temporary home, a model compositor.

FakeCompositor stands in for the control socket: asked to reload, it reads
the user's file the way boswas-compositor does (through
compositorconfig.Snapshot) and its ``outputs`` follow: mode, scale,
rotation, position, variable refresh rate, monitors turned off. Without a
mode setting a monitor runs at its native size (that of the mode it
prefers) at the highest rate it offers there, and that mode is the one
reported as preferred, as boswas-compositor does. Several monitors exist
only in this model; the real compositor's headless backend has one (its
``outputs`` reply is HEADLESS in test_compositorconfig.py).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)

from boswas_control_center import compositorconfig as cc  # noqa: E402

try:
    from boswas_fluent import testing
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:                                          # the suite also runs without PySide6
    HAVE_QT = False

DELL = {"name": "DP-1", "make": "DEL", "model": "DELL U2720Q", "mm": (597, 336),
        "modes": [(3840, 2160, 60000, True), (3840, 2160, 30000, False), (2560, 1440, 59951, False),
                  (1920, 1080, 60000, False), (1920, 1080, 59940, False)]}
LAPTOP = {"name": "eDP-1", "make": "BOE", "model": "0x0A1B", "mm": (309, 174),
          "modes": [(1920, 1200, 60001, True), (1920, 1200, 48001, False), (1280, 800, 60001, False)]}
BEAMER = {"name": "HDMI-A-1", "make": "EPSON", "model": "EB-U05", "mm": (0, 0),
          "modes": [(1920, 1080, 60000, True), (1280, 720, 60000, False)]}
HEADLESS = {"name": "HEADLESS-1", "make": "Boswas", "model": "Headless", "mm": (0, 0),
            "modes": [(1920, 1080, 60000, True)]}
# A fast monitor as it describes itself: its 60 Hz mode is the preferred one. It offers variable refresh rate.
GAMING = {"name": "DP-2", "make": "AOC", "model": "Q27G2", "mm": (597, 336), "vrr": True,
          "modes": [(2560, 1440, 59951, True), (2560, 1440, 165000, False), (2560, 1440, 144000, False),
                    (2560, 1440, 120000, False), (1920, 1080, 240000, False), (1920, 1080, 60000, False)]}


class FakeCompositor:
    """The control socket of a compositor with the given monitors (see the module text)."""

    def __init__(self, *monitors: dict) -> None:
        self.monitors = sorted(monitors, key=lambda monitor: monitor["name"])
        self.settings: cc.Settings | None = None
        self.running = True
        self.reloads = 0
        self.warnings: list[str] = []
        self.current: list[cc.Output] = []

    def attach(self, settings: cc.Settings) -> None:
        self.settings = settings
        self._read()

    def _check(self) -> None:
        if not self.running:
            raise cc.ControlError("not-running", "No Boswas desktop is running in this session.")

    def reload_config(self) -> list[str]:
        self._check()
        self.reloads += 1
        self._read()
        return list(self.warnings)

    def outputs(self) -> list[cc.Output]:
        self._check()
        return list(self.current)

    def _read(self) -> None:
        snapshot = self.settings.snapshot()
        off = [m["name"] for m in self.monitors if snapshot.output(m["name"]).get("enabled") is False]
        if len(off) == len(self.monitors):
            off = off[1:]                                    # the last monitor is never turned off
        placed, waiting = [], []
        for monitor in self.monitors:
            name, wanted = monitor["name"], snapshot.output(monitor["name"])
            if name in off:
                placed.append(cc.Output(name, enabled=False))
                continue
            native = next((width, height) for width, height, _rate, preferred in monitor["modes"] if preferred)
            fastest = max(rate for width, height, rate, _preferred in monitor["modes"] if (width, height) == native)
            modes = tuple(cc.Mode(width, height, rate, (width, height, rate) == (*native, fastest))
                          for width, height, rate, _preferred in monitor["modes"])
            mode = next(mode for mode in modes if mode.preferred)
            asked = cc.parse_mode(wanted.get("mode"))
            if asked is not None and any(m.size == asked[:2] for m in modes):
                same = [m for m in modes if m.size == asked[:2]]
                mode = min(same, key=lambda m: abs(m.refresh - asked[2]) if asked[2] else -m.refresh)
            scale = wanted.get("scale", snapshot.get("output", "scale"))
            rotation = wanted.get("rotation", "normal")
            width, height = cc.logical_size(mode.width, mode.height, scale, rotation)
            vrr = monitor.get("vrr", False)
            sync = cc.AdaptiveSync(vrr, vrr and wanted.get("adaptive_sync") is True)
            output = cc.Output(name, True, monitor["make"], monitor["model"], 0, 0, width, height, scale, rotation,
                               mode, modes, monitor["mm"], sync)
            (placed if "position" in wanted else waiting).append((output, wanted.get("position")))
        result, right = [], 0
        for entry in placed:
            if isinstance(entry, cc.Output):
                result.append(entry)
                continue
            output, (x, y) = entry
            result.append(cc.Output(**{**output.__dict__, "x": x, "y": y}))
            right = max(right, x + output.width)
        for output, _ in waiting:                            # side by side, to the right of the placed ones
            result.append(cc.Output(**{**output.__dict__, "x": right, "y": 0}))
            right += output.width
        self.current = sorted(result, key=lambda output: output.name)


def make_settings(compositor: FakeCompositor | None = None, **keywords) -> cc.Settings:
    """Settings of the test's private home (FluentTestCase sets XDG_CONFIG_HOME), talking to ``compositor``."""
    settings = cc.Settings(dict(os.environ), system_files=("/nonexistent/defaults", "/nonexistent/system"),
                           control=compositor if compositor is not None else cc.Control({}), **keywords)
    if compositor is not None:
        compositor.attach(settings)
    return settings


if HAVE_QT:
    from boswas_control_center.ui.settings import display

    def settle(section, timeout: float = 5.0) -> None:
        """Let the section finish loading and saving."""
        from boswas_fluent.worker import shared_worker
        testing.process(5)
        testing.wait_until(lambda: not section.busy and not shared_worker().pending() and not section._delay.isActive()
                           and not section._reload.isActive(), timeout)
        testing.process(5)

    def drag(widget, start: QPointF, end: QPointF, steps: int = 6) -> None:
        def send(kind, point, button, buttons):
            QApplication.sendEvent(widget, QMouseEvent(kind, point, QPointF(widget.mapToGlobal(point.toPoint())),
                                                       button, buttons, Qt.KeyboardModifier.NoModifier))
        left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
        send(QEvent.Type.MouseButtonPress, start, left, left)
        for step in range(1, steps + 1):
            send(QEvent.Type.MouseMove, start + (end - start) * (step / steps), none, left)
        send(QEvent.Type.MouseButtonRelease, end, left, none)
        testing.process()

    def choose(box, value) -> None:
        """Pick an entry of a combo box as a user does: open the list, click the entry."""
        target = next(i for i in range(box.count()) if box.itemData(i) == value)
        box.showPopup()
        testing.process(20)
        view = box.view()
        testing.click(view.viewport(), view.visualRect(box.model().index(target, 0)).center())
        testing.process(20)
        box.hidePopup()

    class DisplayCase(testing.FluentTestCase):
        def setUp(self) -> None:
            super().setUp()
            self.user = Path(os.environ["XDG_CONFIG_HOME"], "boswas", "compositor.toml")

        def monitors(self, *monitors: dict, tick_ms: int = 1000, **keywords):
            self.compositor = FakeCompositor(*monitors)
            self.settings = make_settings(self.compositor, **keywords)
            section = self.show(display.MonitorsSection(config=self.settings), 900, 640)
            section.tick_ms = tick_ms                                # 10: the countdown runs in hundredths
            section.activate()
            settle(section)
            return section

        def answer(self, section, answer: str) -> None:
            self.assertTrue(testing.wait_until(lambda: section.keep_dialog is not None), "no question was asked")
            dialog = section.keep_dialog
            if answer == "keep":
                testing.click(dialog.primary_button)
            elif answer == "revert":
                testing.click(dialog.close_button)
            elif answer == "escape":
                testing.key(QApplication.focusWidget() or dialog, Qt.Key.Key_Escape)
            self.assertTrue(testing.wait_until(lambda: section.keep_dialog is None), "the question stayed open")
            settle(section)

    class MonitorsTest(DisplayCase):
        def test_two_monitors_are_drawn_in_proportion_and_numbered(self):
            section = self.monitors(DELL, LAPTOP)
            canvas = section.canvas
            self.assertTrue(section.canvas_card.isVisible() and section.monitor_card.isVisible())
            self.assertFalse(section.unavailable.isVisible())
            self.assertEqual(canvas.numbers, {"DP-1": 1, "eDP-1": 2})
            dell, laptop = canvas.view_rect("DP-1"), canvas.view_rect("eDP-1")
            self.assertAlmostEqual(dell.width() / dell.height(), 3840 / 2160, places=2)
            self.assertAlmostEqual(laptop.width() / laptop.height(), 1920 / 1200, places=2)
            self.assertAlmostEqual(dell.width() / laptop.width(), 2.0, places=2)
            self.assertAlmostEqual(dell.right(), laptop.left(), places=3)          # edge to edge, as arranged
            self.assertTrue(canvas.rect().contains(dell.toRect()) and canvas.rect().contains(laptop.toRect()))
            self.assertEqual(section.current, "DP-1")
            self.assertEqual([section.monitor_box.itemText(i) for i in range(2)],
                             ["1: DELL U2720Q (DP-1)", "2: Built-in display (eDP-1)"])
            self.assertEqual([section.resolution_box.itemText(i) for i in range(section.resolution_box.count())],
                             ["3840 × 2160 (recommended)", "2560 × 1440", "1920 × 1080"])
            self.assertEqual([section.rate_box.itemText(i) for i in range(section.rate_box.count())],
                             ["60 Hz (default)", "30 Hz"])
            self.assertEqual((section.scale_box.currentText(), section.orientation_box.currentText()),
                             ("100 %", "Landscape"))
            self.assertIn("2 monitors", canvas.accessibleDescription())
            testing.grab(section, self.tmp / "monitors.png")

        def test_clicking_a_monitor_selects_it(self):
            section = self.monitors(DELL, LAPTOP)
            testing.click(section.canvas, section.canvas.view_rect("eDP-1").center().toPoint())
            self.assertEqual(section.current, "eDP-1")
            self.assertEqual(section.monitor_box.currentData(), "eDP-1")
            self.assertEqual(section.resolution_box.currentText(), "1920 × 1200 (recommended)")
            self.assertEqual([section.rate_box.itemText(i) for i in range(2)], ["60 Hz (default)", "48 Hz"])
            choose(section.monitor_box, "DP-1")
            self.assertEqual((section.current, section.canvas.current), ("DP-1", "DP-1"))
            testing.key(section.canvas, Qt.Key.Key_Right)
            self.assertEqual(section.current, "eDP-1")
            self.assertFalse(self.user.exists())                                   # looking changes nothing

        def test_a_scale_change_is_applied_asked_about_and_kept(self):
            section = self.monitors(DELL, LAPTOP)
            choose(section.scale_box, 1.5)
            settle(section)
            written = cc.Settings(dict(os.environ)).snapshot()
            self.assertEqual(written.output("DP-1"), {"scale": 1.5, "position": [0, 0]})
            self.assertEqual(written.output("eDP-1"), {"position": [2560, 0]})      # moved up to the smaller monitor
            self.assertEqual(self.compositor.reloads, 1)
            self.assertIsNotNone(section.keep_dialog)
            self.assertIn("15 seconds", section.keep_dialog.text_label.text())
            testing.grab(section, self.tmp / "keep.png")
            self.answer(section, "keep")
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("DP-1")["scale"], 1.5)
            self.assertEqual(section.scale_box.currentText(), "150 %")
            self.assertEqual(section.rects(), {"DP-1": cc.Rect(0, 0, 2560, 1440),
                                               "eDP-1": cc.Rect(2560, 0, 1920, 1200)})
            self.assertEqual(self.compositor.reloads, 1)

        def test_without_an_answer_the_previous_file_comes_back_exactly(self):
            original = b"# my notes\n[outputs.DP-1]\nscale = 1.25   # as I like it\n\n[keyboard]\nlayout = \"de\"\n"
            self.user.parent.mkdir(parents=True)
            self.user.write_bytes(original)
            section = self.monitors(DELL, LAPTOP, tick_ms=10)
            section.confirm_seconds = 80                                             # 0.8 seconds here
            self.assertEqual(section.scale_box.currentText(), "125 %")
            choose(section.orientation_box, "90")
            settle(section)
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("DP-1")["rotation"], "90")
            self.assertEqual(section.rects()["DP-1"], cc.Rect(0, 0, 1728, 3072))
            self.assertIn("compositor.toml.bak", section.notice.message())          # the comments are not lost
            self.assertTrue(testing.wait_until(lambda: section.keep_dialog is None, 5))    # nobody answers
            settle(section)
            self.assertEqual(self.user.read_bytes(), original)
            self.assertEqual(self.compositor.reloads, 2)
            self.assertEqual(section.orientation_box.currentText(), "Landscape")
            self.assertEqual(section.rects()["DP-1"], cc.Rect(0, 0, 3072, 1728))
            self.assertIn("previous display settings are back", section.notice.message())

        def test_escape_and_revert_take_the_change_back(self):
            section = self.monitors(DELL, LAPTOP)
            for answer in ("escape", "revert"):
                choose(section.resolution_box, (1920, 1080))
                settle(section)
                self.assertEqual(section.rects()["DP-1"], cc.Rect(0, 0, 1920, 1080))
                self.assertEqual(self.compositor.current[0].mode, cc.Mode(1920, 1080, 60000))
                self.answer(section, answer)
                self.assertFalse(self.user.exists(), answer)                         # there was no file before
                self.assertEqual(section.resolution_box.currentText(), "3840 × 2160 (recommended)")

        def test_resolution_and_refresh_rate(self):
            section = self.monitors(DELL, LAPTOP)
            choose(section.resolution_box, (2560, 1440))
            settle(section)
            self.answer(section, "keep")
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("DP-1")["mode"], "2560x1440@59.951")
            self.assertEqual([section.rate_box.itemText(i) for i in range(section.rate_box.count())],
                             ["59.95 Hz (default)"])
            self.assertFalse(section.rate_box.isEnabled())
            choose(section.resolution_box, (1920, 1080))
            settle(section)
            self.answer(section, "keep")
            choose(section.rate_box, 59940)
            settle(section)
            self.answer(section, "keep")
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("DP-1")["mode"], "1920x1080@59.94")
            self.assertEqual(self.compositor.current[0].mode, cc.Mode(1920, 1080, 59940))
            self.assertEqual(section.rate_box.currentText(), "59.94 Hz")

        def test_every_refresh_rate_of_a_fast_monitor_and_the_highest_is_the_default(self):
            section = self.monitors(GAMING)
            rates = lambda: [section.rate_box.itemText(i) for i in range(section.rate_box.count())]  # noqa: E731
            # Without a setting the desktop runs it at 165 Hz, not at the 60 Hz of the mode it prefers.
            self.assertEqual(section.resolution_box.currentText(), "2560 × 1440 (recommended)")
            self.assertEqual(rates(), ["165 Hz (default)", "144 Hz", "120 Hz", "59.95 Hz"])
            self.assertEqual(section.rate_box.currentText(), "165 Hz (default)")
            self.assertTrue(section.rate_box.isEnabled())
            choose(section.rate_box, 144000)
            settle(section)
            self.answer(section, "keep")
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("DP-2")["mode"], "2560x1440@144")
            self.assertEqual(self.compositor.current[0].mode, cc.Mode(2560, 1440, 144000))
            self.assertEqual(section.rate_box.currentText(), "144 Hz")
            # Another resolution keeps the rate where it can, and lists that size's own rates.
            choose(section.resolution_box, (1920, 1080))
            settle(section)
            self.answer(section, "keep")
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("DP-2")["mode"], "1920x1080@240")
            self.assertEqual(rates(), ["240 Hz (default)", "60 Hz"])
            testing.grab(section, self.tmp / "rates.png")

        def test_variable_refresh_rate_where_the_monitor_offers_it(self):
            section = self.monitors(GAMING, DELL)
            section.select_monitor("DP-2")
            switch = section.vrr_switch
            self.assertTrue(section.vrr_card.isVisible() and switch.isEnabled())
            self.assertFalse(switch.isChecked(), "off unless asked for")
            self.assertEqual(section.vrr_card.description(), display.VRR_ON)
            testing.click(switch)
            settle(section)
            self.assertIsNone(section.keep_dialog, "nothing to confirm: the picture stays")
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("DP-2"), {"adaptive_sync": True})
            self.assertEqual(self.compositor.current[1].adaptive_sync, cc.AdaptiveSync(True, True))
            self.assertTrue(switch.isChecked())
            testing.grab(section, self.tmp / "vrr-on.png")
            testing.click(switch)
            settle(section)
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("DP-2"), {"adaptive_sync": False})
            self.assertFalse(switch.isChecked())
            # A monitor without it: the switch cannot be turned on, and the card says why.
            section.select_monitor("DP-1")
            self.assertFalse(switch.isEnabled())
            self.assertFalse(switch.isChecked())
            self.assertEqual(section.vrr_card.description(), display.VRR_OFF)
            testing.click(switch)
            settle(section)
            self.assertNotIn("adaptive_sync", cc.Settings(dict(os.environ)).snapshot().output("DP-1"))
            testing.grab(section, self.tmp / "vrr-unsupported.png")

        def test_dragging_a_monitor_to_the_other_side(self):
            section = self.monitors(DELL, LAPTOP)
            canvas = section.canvas
            start = canvas.view_rect("eDP-1").center()
            end = QPointF(canvas.view_rect("DP-1").left() - canvas.view_rect("eDP-1").width() / 2 - 4,
                          canvas.view_rect("DP-1").top() + canvas.view_rect("eDP-1").height() / 2 + 3)
            drag(canvas, start, end)
            settle(section)
            self.assertIsNone(section.keep_dialog)                                   # arranging is not asked about
            rects = section.rects()
            self.assertEqual(rects, {"DP-1": cc.Rect(0, 0, 3840, 2160), "eDP-1": cc.Rect(-1920, 0, 1920, 1200)})
            self.assertTrue(cc.is_arranged(rects))
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("eDP-1"), {"position": [-1920, 0]})
            self.assertEqual(section.current, "eDP-1")
            drag(canvas, canvas.view_rect("eDP-1").center(), canvas.view_rect("eDP-1").center())      # a click
            settle(section)
            self.assertEqual(self.compositor.reloads, 1)

        def test_a_drop_on_another_monitor_lands_beside_it(self):
            section = self.monitors(DELL, LAPTOP, BEAMER)
            canvas = section.canvas
            self.assertEqual(canvas.numbers, {"DP-1": 1, "HDMI-A-1": 2, "eDP-1": 3})
            drag(canvas, canvas.view_rect("eDP-1").center(), canvas.view_rect("DP-1").center())
            settle(section)
            rects = section.rects()
            self.assertTrue(cc.is_arranged(rects), rects)
            self.assertEqual(len(rects), 3)

        def test_the_keyboard_moves_a_monitor(self):
            section = self.monitors(DELL, LAPTOP)
            section.canvas.setFocus()
            testing.key(section.canvas, Qt.Key.Key_Right)
            self.assertEqual(section.current, "eDP-1")
            testing.key(section.canvas, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
            settle(section)
            rects = section.rects()
            self.assertTrue(cc.is_arranged(rects))
            self.assertLess(rects["eDP-1"].x, rects["DP-1"].x)
            testing.key(section.canvas, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
            settle(section)
            rects = section.rects()
            self.assertTrue(cc.is_arranged(rects))
            self.assertEqual(rects["eDP-1"].bottom, rects["DP-1"].y)

        def test_turning_a_monitor_off_and_on(self):
            section = self.monitors(DELL, LAPTOP)
            self.assertTrue(section.use_switch.isChecked() and section.use_switch.isEnabled())
            testing.click(section.use_switch)
            settle(section)
            self.answer(section, "keep")
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("DP-1"), {"enabled": False})
            self.assertEqual([o.enabled for o in section.outputs], [False, True])
            self.assertFalse(section.use_switch.isChecked())
            self.assertFalse(section.resolution_card.isEnabled())
            self.assertIn("Turn the monitor on", section.resolution_card.description())
            self.assertEqual(section.monitor_box.itemText(0), "1: DP-1 (DP-1, off)")
            self.assertEqual(list(section.canvas.rects), ["eDP-1"])
            choose(section.monitor_box, "eDP-1")                                     # the last one stays on
            self.assertFalse(section.use_switch.isEnabled())
            self.assertIn("only monitor in use", section.use_card.description())
            choose(section.monitor_box, "DP-1")
            testing.click(section.use_switch)
            settle(section)
            self.answer(section, "keep")
            self.assertEqual([o.enabled for o in section.outputs], [True, True])
            self.assertTrue(cc.is_arranged(section.rects()))

        def test_turning_off_is_taken_back_when_not_confirmed(self):
            section = self.monitors(DELL, LAPTOP, tick_ms=10)
            section.confirm_seconds = 80
            testing.click(section.use_switch)
            settle(section)
            self.assertEqual([o.enabled for o in self.compositor.current], [False, True])
            self.assertTrue(testing.wait_until(lambda: section.keep_dialog is None, 5))
            settle(section)
            self.assertEqual([o.enabled for o in self.compositor.current], [True, True])
            self.assertTrue(section.use_switch.isChecked())

        def test_a_single_virtual_output(self):
            section = self.monitors(HEADLESS)
            self.assertTrue(section.canvas_card.isVisible())
            self.assertFalse(section.monitor_card.isVisible() or section.use_card.isVisible()
                             or section.hint.isVisible())
            self.assertEqual(section.resolution_box.currentText(), "1920 × 1080 (recommended)")
            self.assertFalse(section.rate_box.isEnabled())
            choose(section.scale_box, 2.0)
            settle(section)
            self.answer(section, "keep")
            self.assertEqual(cc.Settings(dict(os.environ)).snapshot().output("HEADLESS-1"), {"scale": 2.0})
            self.assertEqual(section.rects(), {"HEADLESS-1": cc.Rect(0, 0, 960, 540)})
            drag(section.canvas, section.canvas.view_rect("HEADLESS-1").center(), QPointF(5, 5))
            settle(section)
            self.assertEqual(self.compositor.reloads, 1)                             # one monitor cannot be arranged

        def test_without_a_running_desktop(self):
            self.compositor = FakeCompositor(DELL)
            self.compositor.running = False
            self.settings = make_settings(self.compositor)
            section = self.show(display.MonitorsSection(config=self.settings), 900, 400)
            section.activate()
            settle(section)
            self.assertTrue(section.unavailable.isVisible())
            self.assertIn("No Boswas desktop is running", section.unavailable.message())
            for card in (section.canvas_card, section.monitor_card, section.use_card, *section.detail_cards):
                self.assertFalse(card.isVisible())
            testing.grab(section, self.tmp / "unavailable.png")

        def test_a_failed_save_is_told_and_the_control_goes_back(self):
            def refuse(environ, name, data):
                from boswas_control_center import userfiles
                raise userfiles.UserFileError("Your settings folder is read-only.")
            section = self.monitors(DELL, LAPTOP, write=refuse)
            choose(section.scale_box, 2.0)
            settle(section)
            self.assertIsNone(section.keep_dialog)
            self.assertEqual((section.notice.severity(), section.notice.message()),
                             ("error", "Your settings folder is read-only."))
            self.assertEqual(section.scale_box.currentText(), "100 %")

        def test_leaving_the_page_does_not_keep_an_unconfirmed_change(self):
            section = self.monitors(DELL, LAPTOP)
            choose(section.scale_box, 1.25)
            settle(section)
            self.assertIsNotNone(section.keep_dialog)
            section.deactivate()
            testing.wait_until(lambda: self.compositor.reloads == 2, 5)
            self.assertFalse(self.user.exists())
            self.assertEqual(self.compositor.current[0].scale, 1.0)

    class NightLightTest(DisplayCase):
        def night_light(self, running: bool = True):
            self.compositor = FakeCompositor(HEADLESS)
            self.compositor.running = running
            self.settings = make_settings(self.compositor)
            section = self.show(display.NightLightSection(config=self.settings), 900, 420)
            section.activate()
            settle(section)
            return section

        def get(self, key: str):
            return cc.Settings(dict(os.environ)).snapshot().get("night_light", key)

        def test_shows_what_is_in_use(self):
            self.user.parent.mkdir(parents=True)
            self.user.write_text("[night_light]\nenabled = true\ntemperature = 3400\nscheduled = true\n"
                                 "from = \"21:30\"\nto = \"07:15\"\n")
            section = self.night_light()
            self.assertTrue(section.enabled.isChecked() and section.scheduled.isChecked())
            self.assertEqual((section.strength.value(), section.kelvin.text()), (34, "3400 K"))
            self.assertTrue(section.schedule.is_expanded())
            self.assertEqual((section.start.time().toString("HH:mm"), section.end.time().toString("HH:mm")),
                             ("21:30", "07:15"))
            self.assertEqual(self.compositor.reloads, 0)
            testing.grab(section, self.tmp / "night-light.png")

        def test_switch_strength_and_schedule(self):
            section = self.night_light()
            self.assertFalse(section.enabled.isChecked() or section.schedule.is_expanded())
            testing.click(section.enabled)
            settle(section)
            self.assertTrue(self.get("enabled"))
            section.strength.setFocus()
            for _ in range(5):
                testing.key(section.strength, Qt.Key.Key_Left)                       # warmer
            self.assertEqual(section.kelvin.text(), "4000 K")
            settle(section)
            self.assertEqual(self.get("temperature"), 4000)
            self.assertEqual(self.compositor.reloads, 2)                             # five steps, one write
            testing.click(section.scheduled)
            settle(section)
            self.assertTrue(self.get("scheduled") and section.schedule.is_expanded())
            section.start.setFocus()
            testing.key(section.start, Qt.Key.Key_Up)                                # one hour later
            settle(section)
            self.assertEqual((self.get("from"), self.get("to")), ("21:00", "06:00"))
            self.assertFalse(section.notice.isVisible())

        def test_a_change_made_elsewhere_shows_up(self):
            section = self.night_light()
            cc.Settings(dict(os.environ)).update({("night_light", "enabled"): True})         # creates the directory
            section._files_changed()                                                 # (nothing was watched before)
            settle(section)
            self.assertTrue(section.enabled.isChecked())
            section._watch()
            cc.Settings(dict(os.environ)).update({("night_light", "temperature"): 3000})
            self.assertTrue(testing.wait_until(lambda: section.strength.value() == 30, 5))
            section.deactivate()
            cc.Settings(dict(os.environ)).update({("night_light", "temperature"): 6000})
            testing.process(400)
            self.assertEqual(section.strength.value(), 30)                           # not watching any more

        def test_saved_for_the_next_sign_in_without_a_desktop(self):
            section = self.night_light(running=False)
            testing.click(section.enabled)
            settle(section)
            self.assertTrue(self.get("enabled") and section.enabled.isChecked())
            self.assertEqual((section.notice.severity(), section.notice.message()), ("info", cc.NOT_RUNNING))

        def test_a_refused_value_snaps_back(self):
            section = self.night_light()
            section.change({("night_light", "temperature"): 99999})
            settle(section)
            self.assertEqual(section.notice.severity(), "error")
            self.assertIn("2500 to 6500", section.notice.message())
            self.assertEqual(section.strength.value(), 45)

    class ContractTest(testing.FluentTestCase):
        def test_sections_follow_the_contract(self):
            self.assertEqual(display.SECTIONS, (display.MonitorsSection, display.NightLightSection))
            for cls in display.SECTIONS:
                self.assertEqual(cls.page, "display")
                self.assertTrue(cls.title and isinstance(cls.order, int) and cls.keywords)
                section = self.keep(cls())                                           # parent only: the real factory
                self.assertIsInstance(section.config, cc.Settings)
                section.deactivate()                                                 # harmless before activate()

        def test_the_factory_can_be_replaced(self):
            mine = make_settings(FakeCompositor(HEADLESS))
            original, cc.default = cc.default, lambda: mine
            try:
                self.assertIs(self.keep(display.NightLightSection()).config, mine)
            finally:
                cc.default = original


if __name__ == "__main__":
    unittest.main()
