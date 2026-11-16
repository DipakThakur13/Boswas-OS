"""The files around the program: desktop entry, launcher and manual page."""

from __future__ import annotations

import configparser
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = ROOT.parents[1]


class PackageTest(unittest.TestCase):
    def test_desktop_entry(self) -> None:
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read(ROOT / "data/camera/com.boswas.Camera.desktop", "utf-8")
        entry = parser["Desktop Entry"]
        self.assertEqual((entry["Type"], entry["Name"], entry["Icon"], entry["Exec"]),
                         ("Application", "Camera", "camera-photo", "boswas-camera"))
        self.assertEqual((entry["StartupWMClass"], entry["Terminal"]), ("com.boswas.Camera", "false"))
        self.assertTrue(entry["Categories"].endswith(";"))
        icons = REPOSITORY / "desktop/icons/Boswas-Prism/scalable/apps"
        if icons.is_dir():
            self.assertTrue((icons / "camera-photo.svg").is_file())

    def test_launcher_and_manual_page(self) -> None:
        launcher = (ROOT / "bin/boswas-camera").read_text("utf-8")
        self.assertTrue(launcher.startswith("#!/usr/bin/python3 -IB\n"))
        self.assertIn("from boswas_apps.camera.__main__ import main", launcher)
        manual = (ROOT / "man/boswas-camera.1").read_text("utf-8")
        for section in (".SH NAME", ".SH SYNOPSIS", ".SH DESCRIPTION", ".SH OPTIONS", ".SH FILES", ".SH SEE ALSO"):
            self.assertIn(section, manual)
        self.assertIn("500 MiB", manual)                    # the free-space threshold is stated


if __name__ == "__main__":
    unittest.main()
