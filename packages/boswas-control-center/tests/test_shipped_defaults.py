"""Settings' copy of the compositor's defaults against the file the compositor ships.

``compositorconfig.defaults()`` mirrors the defaults compiled into
boswas-compositor. The compositor package also ships them as a file,
``packages/boswas-desktop/config/compositor.toml``; this test reads that file
from the source tree, so a default that changes on one side only is noticed.
It is skipped where the file is not next to this package (an installed copy
of the tests).
"""
import tomllib
import unittest
from pathlib import Path

from boswas_control_center import compositorconfig as cc

SHIPPED = Path(__file__).resolve().parents[2] / "boswas-desktop" / "config" / "compositor.toml"

# Values the shipped file sets on top of the compiled-in defaults: the system
# pointer theme is named there, the compiled-in default is "the theme of the
# environment".
ONLY_IN_THE_FILE = {("pointer", "cursor_theme")}


@unittest.skipUnless(SHIPPED.is_file(), "the compositor's defaults file is not in this tree")
class ShippedDefaults(unittest.TestCase):
    def setUp(self):
        self.shipped = tomllib.loads(SHIPPED.read_text(encoding="utf-8"))
        self.mine = cc.defaults()

    def test_every_shipped_default_is_the_one_settings_assumes(self):
        checked = 0
        for table, keys in self.shipped.items():
            self.assertIsInstance(keys, dict, table)
            self.assertIn(table, self.mine)
            for key, value in keys.items():
                self.assertIn(key, self.mine[table], f"[{table}] {key}")
                if (table, key) in ONLY_IN_THE_FILE:
                    continue
                self.assertEqual(self.mine[table][key], value, f"[{table}] {key}")
                checked += 1
        self.assertGreater(checked, 25)

    def test_the_programs_behind_the_shortcuts_are_the_boswas_applications(self):
        self.assertEqual(self.shipped["apps"], {"terminal": ["boswas-terminal"], "files": ["boswas-files"],
                                                "settings": ["boswas-control-center"],
                                                "task_manager": ["boswas-task-manager"]})
        self.assertEqual(self.mine["apps"], self.shipped["apps"])

    def test_the_exceptions_are_real(self):
        for table, key in ONLY_IN_THE_FILE:
            self.assertIn(key, self.shipped[table])
            self.assertNotEqual(self.mine[table][key], self.shipped[table][key],
                                f"[{table}] {key} no longer differs: drop it from ONLY_IN_THE_FILE")


if __name__ == "__main__":
    unittest.main()
