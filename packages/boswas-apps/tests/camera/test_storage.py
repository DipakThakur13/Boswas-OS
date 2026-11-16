"""Where captures go, their names, the settings and the free-space rules (no Qt)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boswas_apps.camera import storage  # noqa: E402


class StorageCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cam-storage-")
        self.tmp = Path(self._tmp.name)
        self._saved = {name: os.environ.get(name) for name in ("HOME", "XDG_CONFIG_HOME")}
        self.home = self.tmp / "home"
        (self.home / ".config").mkdir(parents=True)
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_CONFIG_HOME"] = str(self.home / ".config")

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()


class FoldersTest(StorageCase):
    def test_camera_roll_follows_the_user_directories(self) -> None:
        self.assertEqual(storage.default_photo_dir(), self.home / "Pictures/Camera Roll")
        self.assertEqual(storage.default_video_dir(), self.home / "Videos/Camera Roll")
        (self.home / ".config/user-dirs.dirs").write_text(
            'XDG_PICTURES_DIR="$HOME/Bilder"\nXDG_VIDEOS_DIR="/srv/video"\nXDG_PICTURES_DIRECTORY="/wrong"\n')
        self.assertEqual(storage.default_photo_dir(), self.home / "Bilder/Camera Roll")
        self.assertEqual(storage.default_video_dir(), Path("/srv/video/Camera Roll"))
        (self.home / ".config/user-dirs.dirs").write_text('XDG_PICTURES_DIR="relative"\nXDG_VIDEOS_DIR="$HOME"\n')
        self.assertEqual(storage.default_photo_dir(), self.home / "Pictures/Camera Roll")
        self.assertEqual(storage.default_video_dir(), self.home / "Videos/Camera Roll")

    def test_names_carry_the_time_and_never_collide(self) -> None:
        when = time.mktime((2026, 11, 16, 14, 30, 5, 0, 0, -1))
        self.assertEqual(storage.capture_name("photo", ".jpg", when), "IMG_20261116_143005.jpg")
        self.assertEqual(storage.capture_name("video", ".mp4", when), "VID_20261116_143005.mp4")
        folder = self.tmp / "new/Camera Roll"
        first = storage.reserve(folder, "photo", ".jpg", when)
        second = storage.reserve(folder, "photo", ".jpg", when)
        third = storage.reserve(folder, "photo", ".jpg", when)
        self.assertEqual([path.name for path in (first, second, third)],
                         ["IMG_20261116_143005.jpg", "IMG_20261116_143005_2.jpg", "IMG_20261116_143005_3.jpg"])
        self.assertTrue(all(path.is_file() and path.stat().st_size == 0 for path in (first, second, third)))
        first.write_bytes(b"a photo")
        self.assertEqual(storage.reserve(folder, "photo", ".jpg", when).name, "IMG_20261116_143005_4.jpg")
        self.assertEqual(first.read_bytes(), b"a photo")              # nothing that exists is touched

    def test_reserve_reports_a_folder_that_cannot_be_made(self) -> None:
        (self.tmp / "file").write_text("x")
        with self.assertRaises(OSError):
            storage.reserve(self.tmp / "file/Camera Roll", "photo", ".jpg")

    def test_latest_capture(self) -> None:
        photos, videos = self.tmp / "p", self.tmp / "v"
        photos.mkdir()
        videos.mkdir()
        self.assertIsNone(storage.latest_capture([photos, videos, self.tmp / "missing"]))
        for name, age in (("old.jpg", 300), ("new.mp4", 100), ("notes.txt", 10), (".hidden.jpg", 5), ("empty.jpg", 1)):
            path = (videos if name.endswith(".mp4") else photos) / name
            path.write_bytes(b"" if name == "empty.jpg" else b"x")
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        (photos / "folder.jpg").mkdir()
        self.assertEqual(storage.latest_capture([photos, videos]), str(videos / "new.mp4"))
        self.assertTrue(storage.is_video("/a/B.MP4") and not storage.is_video("/a/b.jpg"))


class SpaceTest(StorageCase):
    def test_thresholds(self) -> None:
        original = storage.free_bytes
        try:
            storage.free_bytes = lambda folder: 600 * storage.MIB
            self.assertIsNone(storage.space_problem("/x", "video"))
            storage.free_bytes = lambda folder: 499 * storage.MIB
            problem = storage.space_problem("/videos/Camera Roll", "video")
            self.assertIn("499 MiB free in /videos/Camera Roll", problem)
            self.assertIn("at least 500 MiB", problem)
            self.assertIsNone(storage.space_problem("/x", "photo"))
            storage.free_bytes = lambda folder: 5 * storage.MIB
            self.assertIn("at least 20 MiB", storage.space_problem("/x", "photo"))

            def broken(folder):
                raise PermissionError(13, "Permission denied")
            storage.free_bytes = broken
            self.assertIn("Permission denied", storage.space_problem("/x", "photo"))
        finally:
            storage.free_bytes = original
        self.assertEqual((storage.MIN_FREE_VIDEO, storage.STOP_FREE_VIDEO), (500 * storage.MIB, 100 * storage.MIB))

    def test_free_space_of_a_folder_that_does_not_exist_yet(self) -> None:
        self.assertGreater(storage.free_bytes(self.tmp / "not/yet/there"), 0)
        self.assertEqual(storage.format_size(212 * storage.MIB), "212 MiB")
        self.assertEqual(storage.format_size(3 * 1024 * storage.MIB), "3.0 GiB")

    def test_elapsed(self) -> None:
        self.assertEqual([storage.format_elapsed(ms) for ms in (0, 999, 61_000, 3_725_000, -5)],
                         ["00:00", "00:00", "01:01", "1:02:05", "00:00"])


class SettingsTest(StorageCase):
    def test_round_trip(self) -> None:
        path = self.tmp / "config/settings.json"
        settings = storage.Settings.load(path)
        self.assertEqual((settings.default_mode, settings.timer, settings.mirror, settings.grid, settings.quality,
                          settings.sounds), ("photo", 0, True, False, "high", True))
        self.assertEqual(settings.photo_folder(), self.home / "Pictures/Camera Roll")
        settings.default_mode, settings.timer, settings.grid, settings.quality = "video", 5, True, "low"
        settings.photo_dir, settings.camera = "/srv/photos", "cam1"
        settings.photo_sizes["cam1"] = [1280, 720]
        settings.video_sizes["cam1"] = [1920, 1080, 30]
        settings.save(path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        again = storage.Settings.load(path)
        self.assertEqual(again, settings)
        self.assertEqual(again.photo_folder(), Path("/srv/photos"))
        self.assertEqual([entry.name for entry in path.parent.iterdir()], ["settings.json"])

    def test_foreign_content_gives_defaults(self) -> None:
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({
            "photo_dir": "relative/path", "video_dir": "/ok/../videos", "default_mode": "panorama", "timer": 7,
            "mirror": "yes", "grid": 1, "quality": "ultra", "sounds": None, "camera": 5,
            "photo_sizes": {"cam": [0, 720], "ok": [640, 480], "bad": "640x480", "three": [1, 2, 3]},
            "video_sizes": {"ok": [640, 480, 30], "bool": [True, 480, 30], "huge": [10**9, 1, 1]}}))
        settings = storage.Settings.load(path)
        self.assertEqual((settings.photo_dir, settings.video_dir), ("", "/videos"))
        self.assertEqual((settings.default_mode, settings.timer, settings.mirror, settings.grid, settings.quality,
                          settings.sounds, settings.camera), ("photo", 0, True, False, "high", True, ""))
        self.assertEqual((settings.photo_sizes, settings.video_sizes), ({"ok": [640, 480]}, {"ok": [640, 480, 30]}))
        for content in ("", "[1, 2]", "{broken", '"text"', "x" * 300_000):
            path.write_text(content)
            self.assertEqual(storage.Settings.load(path), storage.Settings())
        path.write_text(json.dumps({"timer": True, "default_mode": ["video"]}))
        self.assertEqual(storage.Settings.load(path).timer, 0)


if __name__ == "__main__":
    unittest.main()
