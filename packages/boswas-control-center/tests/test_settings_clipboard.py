"""System > Clipboard (ui/settings/clipboard.py), offscreen, against the real settings files."""

from __future__ import annotations

import sys
import time
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)
import test_settings_display as shared  # noqa: E402
import test_settings_taskbar as base  # noqa: E402

from boswas_control_center import catalog  # noqa: E402
from boswas_control_center import compositorconfig as cc  # noqa: E402

SHIPPED = Path(__file__).resolve().parents[2] / "boswas-desktop" / "config" / "compositor.toml"


class ClipboardSchemaTest(unittest.TestCase):
    def test_the_keys_and_their_limits_are_the_compositor_s(self):
        self.assertEqual(cc.SCHEMA["clipboard"]["history"].default, True)
        self.assertEqual(cc.check_value("clipboard", "history", False), False)
        self.assertEqual(cc.check_value("clipboard", "cleared_at", 1764180000), 1764180000)
        for bad in (-1, "now", 1.5, True):
            with self.assertRaises(cc.InvalidValue, msg=bad):
                cc.check_value("clipboard", "cleared_at", bad)
        with self.assertRaises(cc.InvalidValue):
            cc.check_value("clipboard", "history", "yes")
        # The shipped defaults say the same as the schema.
        shipped = tomllib.loads(SHIPPED.read_text(encoding="utf-8"))
        self.assertEqual(shipped["clipboard"], {"history": True, "cleared_at": 0})
        self.assertEqual(set(shipped["clipboard"]), set(cc.SCHEMA["clipboard"]))

    def test_the_page_is_in_system_and_search_finds_it(self):
        self.assertEqual(catalog.category_of("clipboard"), "system")
        self.assertEqual(catalog.path("clipboard"), ("System", "Clipboard"))
        titles = [s.title for s in catalog.SETTINGS if s.page == "clipboard"]
        self.assertEqual(titles, ["Clipboard history", "Clear clipboard data"])


if shared.HAVE_QT:
    from boswas_fluent import testing

    from boswas_control_center.ui.settings import clipboard

    class ClipboardTest(base.DesktopCase):
        def test_history_on_and_off(self):
            section = self.open(clipboard.ClipboardSection(config=self.settings))
            self.assertTrue(section.history.isChecked(), "on unless turned off")
            testing.click(section.history)
            shared.settle(section)
            self.assertEqual(self.written(), {"clipboard": {"history": False}})
            self.assertFalse(self.get("clipboard", "history"))
            self.assertTrue(self.compositor.reloads, "the desktop is told at once")
            self.pictures(section, "clipboard-off")
            testing.click(section.history)
            shared.settle(section)
            self.assertTrue(self.get("clipboard", "history"))

        def test_clear_clipboard_data_writes_when_and_says_it_is_done(self):
            section = self.open(clipboard.ClipboardSection(config=self.settings))
            self.assertEqual(section.clear.text(), "Clear")
            before = int(time.time())
            testing.click(section.clear)
            shared.settle(section)
            cleared = self.get("clipboard", "cleared_at")
            self.assertGreaterEqual(cleared, before)
            self.assertLessEqual(cleared, int(time.time()))
            self.assertEqual(self.written(), {"clipboard": {"cleared_at": cleared}})
            self.assertEqual(section.clear.text(), "Cleared")
            self.pictures(section, "clipboard-cleared")
            # Again later: a newer time (the desktop clears again).
            section.deactivate()
            self.assertEqual(section.clear.text(), "Clear")


if __name__ == "__main__":
    unittest.main()
