"""The files around the program: desktop entry, launcher and manual page."""

from __future__ import annotations

import configparser
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = ROOT.parents[1]
ENTRY = ROOT / "data/media-player/com.boswas.MediaPlayer.desktop"


class PackageTest(unittest.TestCase):
    def entry(self) -> configparser.SectionProxy:
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read(ENTRY, "utf-8")
        return parser["Desktop Entry"]

    def test_desktop_entry(self) -> None:
        entry = self.entry()
        self.assertEqual((entry["Type"], entry["Name"], entry["Icon"]), ("Application", "Media Player",
                                                                          "multimedia-player"))
        self.assertEqual(entry["Exec"], "boswas-media-player %U")
        self.assertEqual(entry["StartupWMClass"], "com.boswas.MediaPlayer")
        self.assertEqual(entry["Terminal"], "false")
        self.assertTrue(entry["Categories"].endswith(";") and "AudioVideo" in entry["Categories"].split(";"))
        kinds = entry["MimeType"].rstrip(";").split(";")
        self.assertEqual(len(kinds), len(set(kinds)))
        self.assertTrue(entry["MimeType"].endswith(";"))
        for wanted in ("audio/mpeg", "audio/flac", "audio/ogg", "audio/x-vorbis+ogg", "audio/x-opus+ogg", "audio/x-wav",
                       "audio/mp4", "audio/aac", "video/mp4", "video/x-matroska", "video/webm", "video/quicktime",
                       "video/x-msvideo"):
            self.assertIn(wanted, kinds)
        self.assertTrue(all(kind.startswith(("audio/", "video/")) for kind in kinds))

    def test_icon_is_in_the_icon_pack(self) -> None:
        icons = REPOSITORY / "desktop/icons/Boswas-Prism/scalable/apps"
        if not icons.is_dir():
            self.skipTest("the icon pack is not part of this tree")
        self.assertTrue((icons / f"{self.entry()['Icon']}.svg").is_file())

    def test_launcher_and_manual_page(self) -> None:
        launcher = (ROOT / "bin/boswas-media-player").read_text("utf-8")
        self.assertTrue(launcher.startswith("#!/usr/bin/python3 -IB\n"))
        self.assertIn("from boswas_apps.media_player.__main__ import main", launcher)
        manual = (ROOT / "man/boswas-media-player.1").read_text("utf-8")
        for section in (".SH NAME", ".SH SYNOPSIS", ".SH DESCRIPTION", ".SH OPTIONS", ".SH FILES", ".SH SEE ALSO"):
            self.assertIn(section, manual)
        self.assertIn("org.mpris.MediaPlayer2.boswas\\-media\\-player", manual)

    def test_mpris_names_match_the_desktop_entry(self) -> None:
        source = (ROOT / "boswas_apps/media_player/mpris.py").read_text("utf-8")
        self.assertIn('SERVICE = "org.mpris.MediaPlayer2.boswas-media-player"', source)
        self.assertIn('DESKTOP_ENTRY = "com.boswas.MediaPlayer"', source)


if __name__ == "__main__":
    unittest.main()
