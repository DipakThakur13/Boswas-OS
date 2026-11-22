"""System > Multitasking (ui/settings/multitasking.py), offscreen, against the real settings files."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)
import test_settings_display as shared  # noqa: E402
import test_settings_taskbar as base  # noqa: E402

if shared.HAVE_QT:
    from boswas_fluent import testing

    from boswas_control_center.ui.settings import multitasking

    class SnapTest(base.DesktopCase):
        def test_snapping_and_its_layouts(self):
            section = self.open(multitasking.SnapSection(config=self.settings))
            self.assertTrue(section.snap.isChecked() and section.expander.is_expanded())
            self.assertTrue(section.on_maximize.isChecked() and section.on_drag.isChecked())
            testing.click(section.on_drag)
            shared.settle(section)
            self.assertFalse(self.get("multitasking", "snap_layouts_on_drag"))
            testing.click(section.snap)
            shared.settle(section)
            self.assertEqual(self.written(), {"multitasking": {"snap_windows": False, "snap_layouts_on_drag": False}})
            # Without snapping the layouts do nothing: their switches say so by being off limits.
            self.assertFalse(section.on_maximize.isEnabled() or section.on_drag.isEnabled())
            self.pictures(section, "multitasking-snap")
            testing.click(section.snap)
            shared.settle(section)
            self.assertTrue(section.on_maximize.isEnabled())

        def test_which_windows_the_taskbar_and_alt_tab_show(self):
            section = self.open(multitasking.DesktopsSection(config=self.settings))
            self.assertEqual(section.taskbar.currentText(), "Only on the desktop I'm using")
            shared.choose(section.taskbar, "all")
            shared.settle(section)
            shared.choose(section.alt_tab, "all")
            shared.settle(section)
            self.assertEqual(self.written(), {"multitasking": {"taskbar_windows": "all", "alt_tab_windows": "all"}})
            self.assertEqual(section.alt_tab.currentText(), "On all desktops")
            self.pictures(section, "multitasking-desktops")


if __name__ == "__main__":
    unittest.main()
