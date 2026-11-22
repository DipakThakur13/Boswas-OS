"""Personalization > Taskbar and Start (ui/settings/taskbar.py), offscreen, against the real settings files.

Every test changes a setting the way a person does, then reads the user's
compositor.toml back through compositorconfig (what the compositor and with
it the shell will use) and checks that the running desktop was told
(FakeCompositor counts the reloads). Pictures of the sections in both
colour modes go to the test's temporary directory (BOSWAS_TEST_GRABS keeps
them).
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)
import test_settings_display as shared  # noqa: E402

from boswas_control_center import compositorconfig as cc  # noqa: E402

if shared.HAVE_QT:
    from boswas_fluent import testing

    from boswas_control_center.ui.settings import taskbar

    class DesktopCase(testing.FluentTestCase):
        """A temporary home with the user's compositor.toml, and a model of the running compositor."""

        def setUp(self) -> None:
            super().setUp()
            self.user = Path(os.environ["XDG_CONFIG_HOME"], "boswas", "compositor.toml")
            self.compositor = shared.FakeCompositor(shared.HEADLESS)
            self.settings = shared.make_settings(self.compositor)

        def open(self, section, height: int = 520):
            self.show(section, 900, height)
            section.activate()
            shared.settle(section)
            return section

        def get(self, table: str, key: str):
            return self.settings.snapshot().get(table, key)

        def written(self) -> dict:
            return cc._parse(self.user.read_bytes())[0]

        def pictures(self, section, name: str) -> None:
            """The section in both colour modes, kept when BOSWAS_TEST_GRABS names a directory."""
            keep = os.environ.get("BOSWAS_TEST_GRABS")
            section.resize(900, section.sizeHint().height())        # as tall as the page would make it
            testing.process(20)
            for scheme in ("dark", "light"):
                testing.set_scheme(scheme)
                path = testing.grab(section, self.tmp / f"{name}-{scheme}.png")
                if keep:
                    os.makedirs(keep, exist_ok=True)
                    shutil.copy(path, os.path.join(keep, path.name))
            testing.set_scheme("dark")

    class TaskbarTest(DesktopCase):
        def test_task_view_button(self):
            section = self.open(taskbar.TaskbarItemsSection(config=self.settings))
            self.assertTrue(section.task_view.isChecked())
            testing.click(section.task_view)
            shared.settle(section)
            self.assertFalse(self.get("taskbar", "task_view"))
            self.assertEqual(self.written(), {"taskbar": {"task_view": False}})
            self.assertGreaterEqual(self.compositor.reloads, 1, "the running desktop takes it at once")

        def test_behaviours(self):
            section = self.open(taskbar.TaskbarBehavioursSection(config=self.settings))
            self.assertEqual(section.alignment.currentText(), "Center")
            self.assertFalse(section.auto_hide.isChecked() or section.seconds.isChecked())
            self.assertTrue(section.badges.isChecked())
            shared.choose(section.alignment, "left")
            shared.settle(section)
            for switch in (section.auto_hide, section.seconds, section.badges):
                testing.click(switch)
                shared.settle(section)
            self.assertEqual(self.written(), {"taskbar": {"alignment": "left", "auto_hide": True,
                                                          "clock_seconds": True, "badges": False}})
            self.assertEqual(section.alignment.currentText(), "Left")
            self.pictures(section, "taskbar")

        def test_a_change_made_elsewhere_is_shown(self):
            self.user.parent.mkdir(parents=True, exist_ok=True)          # a directory that exists can be watched
            section = self.open(taskbar.TaskbarBehavioursSection(config=self.settings))
            self.settings.apply({("taskbar", "auto_hide"): True})
            self.assertTrue(testing.wait_until(lambda: section.auto_hide.isChecked()), "the file watch did not see it")

    class StartTest(DesktopCase):
        def test_layout_recommendations_and_folders(self):
            section = self.open(taskbar.StartSection(config=self.settings), 760)
            self.assertEqual(section.layout_box.currentText(), "Default")
            self.assertTrue(section.recent.isChecked() and section.most_used.isChecked())
            self.assertEqual([key for key, switch in section.folder_switches.items() if switch.isChecked()],
                             ["settings", "files"])
            shared.choose(section.layout_box, "more_pins")
            shared.settle(section)
            testing.click(section.most_used)
            shared.settle(section)
            section.folders.set_expanded(True, animate=False)
            testing.process(20)
            for key in ("music", "files", "downloads"):
                testing.click(section.folder_switches[key])
                shared.settle(section)
            self.assertEqual(self.get("start", "layout"), "more_pins")
            self.assertFalse(self.get("start", "most_used"))
            self.assertEqual(self.get("start", "folders"), ["settings", "downloads", "music"],
                             "in the order Settings lists them; Files is off again")
            self.assertEqual(section.folders.header.description(), "3 shown next to the power button")
            self.pictures(section, "start")

        def test_a_list_with_a_folder_settings_does_not_know_is_not_dropped_silently(self):
            self.user.parent.mkdir(parents=True, exist_ok=True)
            self.user.write_text('[start]\nfolders = ["music", "/etc/passwd"]\n')
            self.assertEqual(self.get("start", "folders"), ["music"], "Settings reads it as the compositor does")
            section = self.open(taskbar.StartSection(config=self.settings))
            self.assertTrue(section.folder_switches["music"].isChecked())
            testing.click(section.recent)
            shared.settle(section)
            # The rewritten file cannot keep a list Settings would not write itself: the previous file is kept
            # as a backup and the section says so.
            self.assertEqual(self.written()["start"], {"recently_added": False})
            self.assertIn("/etc/passwd", self.user.with_name("compositor.toml.bak").read_text())
            self.assertEqual(section.notice.severity(), "warning")
            self.assertIn("start.folders", section.notice.message())


if __name__ == "__main__":
    unittest.main()
