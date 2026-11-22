"""The Mouse, Touchpad and Keyboard sections, offscreen, on real settings files in a temporary home."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)
import test_settings_display as shared  # noqa: E402
from test_xkb import SAMPLE  # noqa: E402

from boswas_control_center import compositorconfig as cc  # noqa: E402
from boswas_control_center import xkb  # noqa: E402

try:
    from boswas_fluent import testing
    from PySide6.QtCore import Qt
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

# /proc/bus/input/devices of a laptop with a touch screen, a pen and a USB mouse (shortened to the lines read).
DEVICES = """\
I: Bus=0011 Vendor=0001 Product=0001 Version=ab83
N: Name="AT Translated Set 2 keyboard"
H: Handlers=sysrq kbd event3 leds
B: PROP=0
B: EV=120013
B: KEY=402000000 3803078f800d001 feffffdfffefffff fffffffffffffffe

I: Bus=0018 Vendor=06cb Product=cd41 Version=0100
N: Name="SYNA2B31:00 06CB:CD41 Touchpad"
H: Handlers=mouse1 event6
B: PROP=5
B: EV=1b
B: KEY=e520 10000 0 0 0 0
B: ABS=2e0800000000003

I: Bus=0018 Vendor=04f3 Product=2a3b Version=0100
N: Name="ELAN Touchscreen"
H: Handlers=event8
B: PROP=2
B: EV=1b
B: KEY=e520 0 0 0 0 0
B: ABS=3273800000000003

I: Bus=0003 Vendor=056a Product=0374 Version=0110
N: Name="Wacom Intuos S Pen"
H: Handlers=mouse3 event11
B: PROP=1
B: EV=1f
B: KEY=1c63 0 70033 0 0 0 0 0 0 0 0
B: ABS=1000003000003

I: Bus=0003 Vendor=046d Product=c077 Version=0111
N: Name="Logitech USB Optical Mouse"
H: Handlers=mouse2 event9
B: PROP=0
B: EV=17
B: KEY=ff0000 0 0 0 0
B: REL=903
"""


if HAVE_QT:
    from boswas_control_center.ui.settings import input as sections

    class InputCase(testing.FluentTestCase):
        def setUp(self) -> None:
            super().setUp()
            self.user = Path(os.environ["XDG_CONFIG_HOME"], "boswas", "compositor.toml")
            self.compositor = shared.FakeCompositor(shared.HEADLESS)
            self.settings = shared.make_settings(self.compositor)
            self.devices = self.tmp / "devices"
            self.devices.write_text(DEVICES)

        def write(self, text: str) -> None:
            self.user.parent.mkdir(parents=True, exist_ok=True)
            self.user.write_text(text)

        def open(self, section, height: int = 700):
            self.show(section, 900, height)
            section.activate()
            shared.settle(section)
            return section

        def get(self, table: str, key: str):
            return cc.Settings(dict(os.environ)).snapshot().get(table, key)

    class TouchpadDetectionTest(unittest.TestCase):
        def test_only_the_touchpad_is_a_touchpad(self):
            self.assertEqual(sections.touchpads(DEVICES), ["SYNA2B31:00 06CB:CD41 Touchpad"])
            self.assertEqual(sections.touchpads(DEVICES.replace("B: PROP=5", "B: PROP=7")), [])     # a touch screen
            self.assertEqual(sections.touchpads(""), [])

        def test_odd_text_is_skipped(self):
            hostile = ("N: Name=\"\x1b[31mPad\x00\"\nB: KEY=e520 10000 0 0 0 0\nB: PROP=zz\n\n"
                       "B: KEY=not hex at all\n\nN: Name=\nB: KEY=e520 10000 0 0 0 0\n\n"
                       "garbage\n\n" + "B: KEY=" + "ffffffffffffffff " * 500 + "\n")
            # (the last block has every bit set, the pen's too: not a touchpad)
            self.assertEqual(sections.touchpads(hostile), ["[31mPad", "Touchpad"])

        def test_an_unreadable_list_is_unknown(self):
            self.assertIsNone(sections.read_touchpads("/nonexistent/devices"))

    class MouseTest(InputCase):
        def test_shows_and_changes_the_pointer_settings(self):
            self.write("[pointer]\naccel_speed = 0.4\naccel_profile = \"flat\"\nleft_handed = true\n"
                       "natural_scroll = true\ncursor_size = 40\n")
            section = self.open(sections.MouseSection(config=self.settings))
            self.assertEqual(section.speed.value(), 4)
            self.assertFalse(section.precision.isChecked())
            self.assertEqual((section.primary.currentText(), section.scroll.currentText(), section.size.currentText()),
                             ("Right", "Wheel down scrolls up", "Custom (40)"))
            testing.click(section.precision)
            shared.choose(section.primary, False)
            shared.choose(section.scroll, False)
            shared.choose(section.size, 48)
            section.speed.setFocus()
            for _ in range(7):
                testing.key(section.speed, Qt.Key.Key_Left)
            shared.settle(section)
            self.assertEqual([self.get("pointer", key) for key in ("accel_profile", "left_handed", "natural_scroll",
                                                                    "cursor_size", "accel_speed")],
                             ["adaptive", False, False, 48, -0.3])
            self.assertEqual([section.size.itemText(i) for i in range(section.size.count())],
                             ["Small (24)", "Medium (32)", "Large (48)", "Extra large (64)"])
            self.assertFalse(section.notice.isVisible())
            self.assertGreaterEqual(self.compositor.reloads, 2)
            testing.grab(section, self.tmp / "mouse.png")

        def test_a_file_the_desktop_rejects_is_told_and_repaired_by_a_change(self):
            self.write("[pointer]\nleft_handed = true\nwheel = 3\n")
            section = self.open(sections.MouseSection(config=self.settings))
            self.assertEqual(section.primary.currentText(), "Left")                 # the file is not in use
            self.assertEqual(section.notice.severity(), "warning")
            self.assertIn("unknown key pointer.wheel", section.notice.message())
            shared.choose(section.primary, True)
            shared.settle(section)
            self.assertIn("compositor.toml.bak", section.notice.message())
            self.assertTrue(self.get("pointer", "left_handed"))
            self.assertEqual(section.primary.currentText(), "Right")

    class TouchpadTest(InputCase):
        def test_switches_and_scrolling(self):
            section = self.open(sections.TouchpadSection(config=self.settings, devices_file=str(self.devices)))
            self.assertFalse(section.absent.isVisible())
            self.assertTrue(section.tap.isChecked() and section.typing.isChecked() and section.gestures.isChecked())
            self.assertEqual(section.scroll.currentText(), "Content follows your fingers")
            for switch in (section.tap, section.typing, section.gestures):
                testing.click(switch)
            shared.choose(section.scroll, False)
            section.speed.setFocus()
            testing.key(section.speed, Qt.Key.Key_End)
            shared.settle(section)
            self.assertEqual([self.get("touchpad", key) for key in ("tap_to_click", "disable_while_typing", "gestures",
                                                                     "natural_scroll", "accel_speed")],
                             [False, False, False, False, 1.0])
            self.assertFalse(section.tap.isChecked() or section.gestures.isChecked())

        def test_without_a_touchpad_the_section_says_so(self):
            self.devices.write_text(DEVICES.replace("B: PROP=5", "B: PROP=7"))
            section = self.open(sections.TouchpadSection(config=self.settings, devices_file=str(self.devices)))
            self.assertTrue(section.absent.isVisible())
            self.assertIn("No touchpad was found", section.absent.message())
            testing.click(section.tap)                                               # the settings still work
            shared.settle(section)
            self.assertFalse(self.get("touchpad", "tap_to_click"))
            unknown = self.open(sections.TouchpadSection(config=self.settings, devices_file="/nonexistent"))
            self.assertFalse(unknown.absent.isVisible())

    class KeyboardTest(InputCase):
        def keyboard(self, text: str = "", catalog: xkb.Catalog | None = None):
            if text:
                self.write(text)
            return self.open(sections.KeyboardSection(config=self.settings, catalog=catalog or xkb.parse(SAMPLE)), 900)

        def titles(self, section) -> list[str]:
            return [card.title() for card in section.layouts.cards()]

        def layout(self) -> tuple[str, str]:
            return self.get("keyboard", "layout"), self.get("keyboard", "variant")

        def test_layouts_are_listed_by_name(self):
            section = self.keyboard("[keyboard]\nlayout = \"us,de,in,zz\"\nvariant = \",neo,eng,odd\"\n")
            self.assertEqual(self.titles(section), ["English (US)", "German (Neo 2)", "English (India, with rupee)",
                                                    "zz (odd)"])
            self.assertEqual([card.description() for card in section.layouts.cards()],
                             ["us", "de (neo)", "in (eng)", "zz (odd)"])
            self.assertFalse(section.add_button.isEnabled())
            self.assertIn("4 layouts are the most", section.layouts.header.description())
            rows = section.layout_rows
            self.assertFalse(rows[0].up.isEnabled() or rows[3].down.isEnabled())
            self.assertTrue(rows[0].down.isEnabled() and rows[3].up.isEnabled() and rows[1].remove.isEnabled())
            self.assertEqual(rows[1].remove.toolTip(), "Remove German (Neo 2)")
            testing.grab(section, self.tmp / "keyboard.png")

        def test_reorder_and_remove_down_to_one(self):
            section = self.keyboard("[keyboard]\nlayout = \"us,de,in\"\nvariant = \",neo,\"\n")
            testing.click(section.layout_rows[1].up)
            shared.settle(section)
            self.assertEqual(self.layout(), ("de,us,in", "neo,,"))
            self.assertEqual(self.titles(section), ["German (Neo 2)", "English (US)", "Indian"])
            testing.click(section.layout_rows[0].down)
            testing.click(section.layout_rows[1].down)
            shared.settle(section)
            self.assertEqual(self.layout(), ("us,in,de", ",,neo"))
            testing.click(section.layout_rows[0].remove)
            shared.settle(section)
            testing.click(section.layout_rows[1].remove)
            shared.settle(section)
            self.assertEqual(self.layout(), ("in", ""))
            self.assertEqual(self.titles(section), ["Indian"])
            only = section.layout_rows[0]
            self.assertFalse(only.remove.isEnabled() or only.up.isEnabled() or only.down.isEnabled())
            testing.click(only.remove)                                               # at least one stays
            shared.settle(section)
            self.assertEqual(self.layout(), ("in", ""))

        def test_add_a_layout_from_the_searchable_dialog(self):
            section = self.keyboard()
            self.assertEqual(self.titles(section), ["English (US)"])
            testing.click(section.add_button)
            self.assertIsNotNone(section.add_dialog)
            names = [section.add_list.item(i).text() for i in range(section.add_list.count())]
            self.assertEqual(len(names), 12)                                         # everything but the one in use
            self.assertNotIn("English (US)", names)
            self.assertFalse(section.add_dialog.primary_button.isEnabled())
            testing.type_text(section.add_search, "german")
            self.assertEqual([section.add_list.item(i).text() for i in range(section.add_list.count())],
                             ["German", "German (Neo 2)", "German (no dead keys)"])
            testing.grab(section, self.tmp / "add-layout.png")
            testing.click(section.add_list.viewport(),
                          section.add_list.visualItemRect(section.add_list.item(1)).center())
            self.assertTrue(section.add_dialog.primary_button.isEnabled())
            testing.click(section.add_dialog.primary_button)
            shared.settle(section)
            self.assertIsNone(section.add_dialog)
            self.assertEqual(self.layout(), ("us,de", ",neo"))
            self.assertEqual(self.titles(section), ["English (US)", "German (Neo 2)"])
            # Search, then Enter takes the first match; Cancel adds nothing.
            testing.click(section.add_button)
            testing.type_text(section.add_search, "hindi")
            testing.key(section.add_search, Qt.Key.Key_Return)
            shared.settle(section)
            self.assertEqual(self.layout(), ("us,de,in", ",neo,hin-kagapa"))
            testing.click(section.add_button)
            testing.type_text(section.add_search, "klingon")
            self.assertTrue(section.add_empty.isVisible())
            self.assertFalse(section.add_dialog.primary_button.isEnabled())
            testing.key(section.add_search, Qt.Key.Key_Return)                       # nothing to add
            self.assertIsNotNone(section.add_dialog)
            testing.click(section.add_dialog.close_button)
            shared.settle(section)
            self.assertEqual(self.layout(), ("us,de,in", ",neo,hin-kagapa"))

        def test_no_more_than_four_layouts(self):
            section = self.keyboard("[keyboard]\nlayout = \"us,de,in\"\n")
            self.assertTrue(section.add_button.isEnabled())
            section.add_layout(("fr", "bepo"))
            shared.settle(section)
            self.assertEqual(self.layout(), ("us,de,in,fr", ",,,bepo"))
            self.assertFalse(section.add_button.isEnabled())
            section.add_layout(("custom", ""))
            section.open_add_dialog()
            shared.settle(section)
            self.assertIsNone(section.add_dialog)
            self.assertEqual(self.layout(), ("us,de,in,fr", ",,,bepo"))
            five = self.keyboard("[keyboard]\nlayout = \"us,de,in,fr,custom\"\n")     # written by hand
            self.assertIn("not in use", five.layouts.cards()[4].description())
            self.assertNotIn("not in use", five.layouts.cards()[3].description())

        def test_the_second_switching_shortcut_keeps_other_options(self):
            section = self.keyboard("[keyboard]\noptions = \"caps:escape,grp:caps_toggle\"\n")
            self.assertEqual([section.switch.itemText(i) for i in range(section.switch.count())],
                             ["No second shortcut", "Right Alt (while pressed)", "Alt+Shift", "Caps Lock"])
            self.assertEqual(section.switch.currentText(), "Caps Lock")
            shared.choose(section.switch, "grp:alt_shift_toggle")
            shared.settle(section)
            self.assertEqual(self.get("keyboard", "options"), "caps:escape,grp:alt_shift_toggle")
            shared.choose(section.switch, "")
            shared.settle(section)
            self.assertEqual(self.get("keyboard", "options"), "caps:escape")
            self.assertEqual(section.switch.currentText(), "No second shortcut")

        def test_repeat_and_num_lock(self):
            section = self.keyboard()
            self.assertEqual((section.delay_value.text(), section.rate_value.text()), ("400 ms", "30 / s"))
            section.delay.setFocus()
            for _ in range(3):
                testing.key(section.delay, Qt.Key.Key_Left)
            section.rate.setFocus()
            for _ in range(10):
                testing.key(section.rate, Qt.Key.Key_Right)
            self.assertEqual((section.delay_value.text(), section.rate_value.text()), ("250 ms", "40 / s"))
            testing.click(section.numlock)
            shared.settle(section)
            self.assertEqual([self.get("keyboard", key) for key in ("repeat_delay", "repeat_rate", "numlock")],
                             [250, 40, True])
            testing.type_text(section.try_box, "aaa")                                # typing here changes nothing
            self.assertEqual(section.try_box.text(), "aaa")

        def test_without_the_xkb_list(self):
            section = self.keyboard("[keyboard]\nlayout = \"us,de\"\n", xkb.load("/nonexistent/evdev.lst"))
            self.assertTrue(section.problem.isVisible())
            self.assertIn("cannot be read", section.problem.message())
            self.assertEqual(self.titles(section), ["us", "de"])
            self.assertFalse(section.add_button.isEnabled())
            testing.click(section.layout_rows[1].up)                                 # the list can still be ordered
            shared.settle(section)
            self.assertEqual(self.layout(), ("de,us", ""))

        @unittest.skipUnless(Path(xkb.RULES_FILE).exists(), "xkb-data is not installed")
        def test_the_installed_layout_list_is_loaded_off_the_gui_thread(self):
            self.write("[keyboard]\nlayout = \"us,de\"\nvariant = \",neo\"\n")
            section = self.open(sections.KeyboardSection(config=self.settings))
            self.assertEqual(self.titles(section), ["English (US)", "German (Neo 2)"])
            self.assertGreater(section.switch.count(), 20)

    class ContractTest(testing.FluentTestCase):
        def test_sections_follow_the_contract(self):
            self.assertEqual([(cls.title, cls.page) for cls in sections.SECTIONS],
                             [("Mouse", "mouse"), ("Mouse pointer", "mouse-pointer"), ("Touchpad", "touchpad"),
                              ("Keyboard", "keyboard")])
            for cls in sections.SECTIONS:
                self.assertTrue(isinstance(cls.order, int) and cls.keywords)
                section = self.keep(cls())
                self.assertIsInstance(section.config, cc.Settings)
                section.deactivate()


if __name__ == "__main__":
    unittest.main()
