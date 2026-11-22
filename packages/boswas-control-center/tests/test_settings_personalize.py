"""The pictures offered as background (wallpapers, no Qt) and the Personalization, Lock screen and Screen sections."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_fixtures  # noqa: E402,F401  (import paths)
import test_settings_display as shared  # noqa: E402

from boswas_control_center import compositorconfig as cc  # noqa: E402
from boswas_control_center import wallpapers  # noqa: E402

try:
    from boswas_fluent import testing
    from PySide6.QtGui import QColor, QImage
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff\xe0"


def package(root: Path, name: str, title: str | None = None, light: bytes | None = PNG + b"light",
            dark: bytes | None = PNG + b"dark") -> Path:
    """A wallpaper package as boswas-branding installs it (desktop/presets/install.sh)."""
    directory = root / name
    for folder, content in (("images", light), ("images_dark", dark)):
        if content is not None:
            (directory / "contents" / folder).mkdir(parents=True, exist_ok=True)
            (directory / "contents" / folder / "3840x2160.png").write_bytes(content + name.encode())
    directory.mkdir(parents=True, exist_ok=True)
    if title is not None:
        (directory / "metadata.json").write_text(json.dumps({"KPlugin": {"Id": name, "Name": title}}))
    return directory


class WallpaperFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cc-wallpapers-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.system = self.tmp / "wallpapers"
        self.system.mkdir()
        self.home = self.tmp / "home"
        (self.home / "Pictures").mkdir(parents=True)
        self.environ = {"HOME": str(self.home)}

    def picture(self, name: str, content: bytes = PNG + b"x", age: int = 0) -> Path:
        path = self.home / "Pictures" / name
        path.write_bytes(content)
        os.utime(path, (1_700_000_000 - age, 1_700_000_000 - age))
        return path

    def test_packages_offer_the_artwork_of_the_colour_mode(self):
        ocean = package(self.system, "Boswas-Ocean", "Boswas Ocean")
        package(self.system, "Boswas-Pearl", "Boswas Pearl")
        dark = wallpapers.system_wallpapers("dark", str(self.system))
        self.assertEqual([item.name for item in dark], ["Boswas Ocean", "Boswas Pearl"])
        self.assertEqual(dark[0].path, str(ocean / "contents/images_dark/3840x2160.png"))
        self.assertEqual((dark[0].source, dark[0].light), ("boswas", str(ocean / "contents/images/3840x2160.png")))
        light = wallpapers.system_wallpapers("light", str(self.system))
        self.assertEqual(light[0].path, str(ocean / "contents/images/3840x2160.png"))
        self.assertTrue(dark[0].matches(light[0].path) and light[0].matches(dark[0].path))
        self.assertFalse(dark[0].matches("") or dark[0].matches(dark[1].path))
        self.assertEqual(wallpapers.counterpart(dark[0].path, "light", str(self.system)), light[0].path)
        self.assertEqual(wallpapers.counterpart(light[0].path, "light", str(self.system)), light[0].path)
        self.assertEqual(wallpapers.counterpart("/home/a/sea.jpg", "light", str(self.system)), "")
        self.assertEqual(wallpapers.counterpart("", "dark", str(self.system)), "")

    def test_the_copy_of_the_default_wallpaper_is_listed_once(self):
        horizon = package(self.system, "Boswas-Horizon", "Boswas Horizon")
        copy = package(self.system, "Boswas", "Boswas")
        for folder in ("images", "images_dark"):                         # the same files under a second name
            (copy / "contents" / folder / "3840x2160.png").write_bytes(
                (horizon / "contents" / folder / "3840x2160.png").read_bytes())
        package(self.system, "Boswas-Slate", "Boswas Slate")
        found = wallpapers.system_wallpapers("dark", str(self.system))
        self.assertEqual([item.name for item in found], ["Boswas Horizon", "Boswas Slate"])
        # A preset recorded the copy's path? The other mode's artwork is still found.
        self.assertEqual(wallpapers.counterpart(str(copy / "contents/images_dark/3840x2160.png"), "light",
                                                str(self.system)), str(copy / "contents/images/3840x2160.png"))

    def test_odd_packages(self):
        package(self.system, "Only-Dark", "Only dark", light=None)
        package(self.system, "No-Metadata")
        package(self.system, "Bad-Metadata").joinpath("metadata.json").write_text("{not json")
        package(self.system, "Odd-Name", "  \x1b[31mRed\x00 name  ")
        package(self.system, "Not-Pictures", "Text files", light=b"plain text", dark=b"<svg/>")
        package(self.system, ".hidden", "Hidden")
        (self.system / "Empty").mkdir()
        (self.system / "a-file.png").write_bytes(PNG)
        sizes = self.system / "Sizes" / "contents" / "images"
        sizes.mkdir(parents=True)
        for name in ("1920x1080.png", "3840x2160.jpg", "5120x2880.png", "screenshot.png", "9999x9999.txt"):
            (sizes / name).write_bytes(JPEG if name.endswith("jpg") else PNG)
        (sizes / "5120x2880.png").write_bytes(b"not a picture")
        found = {item.name: item for item in wallpapers.system_wallpapers("light", str(self.system))}
        self.assertEqual(sorted(found), ["Bad Metadata", "No Metadata", "Only dark", "Sizes", "[31mRed name"])
        self.assertEqual(found["Only dark"].path, found["Only dark"].dark)
        self.assertTrue(found["Sizes"].path.endswith("3840x2160.jpg"))          # the largest real picture
        self.assertEqual(wallpapers.system_wallpapers("dark", str(self.tmp / "missing")), [])

    def test_the_pictures_folder(self):
        self.assertEqual(wallpapers.pictures_dir(self.environ), str(self.home / "Pictures"))
        config = self.home / ".config"
        config.mkdir()
        other = self.home / "Bilder mit \"Namen\""
        other.mkdir()
        for text, expected in (('XDG_PICTURES_DIR="$HOME/Bilder mit \\"Namen\\""\n', str(other)),
                               ('# comment\nXDG_DESKTOP_DIR="$HOME/Desktop"\n  XDG_PICTURES_DIR = "%s"  \n'
                                % (self.home / "Pictures" / ".." / "Pictures"), str(self.home / "Pictures")),
                               ('XDG_PICTURES_DIR="%s"\n' % other, str(self.home / "Pictures")),   # unescaped quotes
                               ('XDG_PICTURES_DIR="$HOME/"\n', None),            # "none": the home folder itself
                               ('XDG_PICTURES_DIR="$HOME/missing"\n', None),
                               ('XDG_PICTURES_DIR="relative"\n', str(self.home / "Pictures")),
                               ('XDG_PICTURES_DIR=$(rm -rf /)\n', str(self.home / "Pictures")),
                               ("\xff\xfe garbage", str(self.home / "Pictures"))):
            (config / "user-dirs.dirs").write_bytes(text.encode("utf-8", "surrogateescape")
                                                    if text.isascii() else b"\xff\xfe garbage")
            self.assertEqual(wallpapers.pictures_dir(self.environ), expected, text)
        self.assertIsNone(wallpapers.pictures_dir({}))
        self.assertIsNone(wallpapers.pictures_dir({"HOME": "relative"}))
        elsewhere = self.tmp / "xdg"
        elsewhere.mkdir()
        (elsewhere / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="%s"\n' % elsewhere)
        self.assertEqual(wallpapers.pictures_dir({"HOME": str(self.home), "XDG_CONFIG_HOME": str(elsewhere)}),
                         str(elsewhere))

    def test_the_users_pictures(self):
        self.picture("old.png", age=500)
        self.picture("new.JPG", JPEG + b"x", age=10)
        self.picture("middle.jpeg", JPEG, age=100)
        self.picture("renamed-text.png", b"just text")
        self.picture("empty.png", b"")
        self.picture(".hidden.png")
        self.picture("notes.txt", PNG)
        self.picture("odd\nname.png")
        (self.home / "Pictures" / "folder.png").mkdir()
        (self.home / "Pictures" / "sub").mkdir()
        (self.home / "Pictures" / "sub" / "deep.png").write_bytes(PNG)
        (self.home / "Pictures" / "link.png").symlink_to(self.home / "Pictures" / "old.png")
        (self.home / "Pictures" / "broken.png").symlink_to(self.home / "nowhere.png")
        with open(self.home / "Pictures" / "huge.png", "wb") as handle:           # sparse: larger than the shell reads
            handle.write(PNG)
            handle.truncate(wallpapers.MAX_FILE + 1)
        found = wallpapers.user_pictures(self.environ)
        self.assertEqual([item.name for item in found], ["new", "middle", "link", "old"])
        self.assertTrue(all(item.source == "pictures" and item.light == "" for item in found))
        self.assertEqual(found[0].path, str(self.home / "Pictures" / "new.JPG"))
        self.assertEqual([item.name for item in wallpapers.user_pictures(self.environ, limit=2)], ["new", "middle"])
        self.assertEqual(wallpapers.user_pictures({}), [])
        package(self.system, "Boswas-Ocean", "Boswas Ocean")
        everything = wallpapers.available(self.environ, "dark", str(self.system))
        self.assertEqual([item.source for item in everything], ["boswas"] + ["pictures"] * 4)

    def test_the_number_of_pictures_is_capped(self):
        for index in range(wallpapers.MAX_PICTURES + 7):
            self.picture(f"p{index:03}.png", age=index)
        found = wallpapers.user_pictures(self.environ)
        self.assertEqual(len(found), wallpapers.MAX_PICTURES)
        self.assertEqual(found[0].name, "p000")

    def test_check_picture(self):
        good = self.picture("sea.png")
        self.assertEqual(wallpapers.check_picture(str(good)), str(good))
        self.assertEqual(wallpapers.check_picture(str(good.parent / ".." / "Pictures" / "sea.png")), str(good))
        text = self.picture("text.png", b"hello")
        for bad, words in ((str(text), "Only PNG and JPEG"), (str(self.home / "missing.png"), "cannot be read"),
                           (str(self.home / "Pictures"), "not a picture file"), ("relative.png", "Choose a picture"),
                           ("", "Choose a picture"), (None, "Choose a picture"),
                           ("/tmp/a\x00b.png", "Choose a picture"),
                           (str(self.picture("line\nbreak.png")), "cannot be stored")):
            with self.assertRaises(ValueError, msg=bad) as caught:
                wallpapers.check_picture(bad)
            self.assertIn(words, str(caught.exception))
        self.assertEqual((wallpapers.picture_kind(str(good)), wallpapers.picture_kind(str(text)),
                          wallpapers.picture_kind(str(self.picture("a.jpg", JPEG)))), ("png", "", "jpeg"))
        self.assertIsNone(wallpapers.stamp(str(self.home / "missing.png")))
        self.assertEqual(wallpapers.stamp(str(good))[0], str(good))


class ThumbnailCacheTest(unittest.TestCase):
    def test_keeps_the_most_recently_used(self):
        cache = wallpapers.ThumbnailCache(limit=3)
        made = []

        def make(key):
            made.append(key)
            return f"thumbnail of {key}"
        for key in ("a", "b", "c"):
            self.assertEqual(cache.get(key, lambda key=key: make(key)), f"thumbnail of {key}")
        self.assertEqual(cache.get("a", lambda: make("again")), "thumbnail of a")     # cached, and now the newest
        cache.get("d", lambda: make("d"))
        self.assertEqual((len(cache), made), (3, ["a", "b", "c", "d"]))
        self.assertIsNone(cache.peek("b"))                                             # the oldest went
        self.assertEqual(cache.peek("a"), "thumbnail of a")
        with self.assertRaises(ValueError):
            cache.get("broken", lambda: (_ for _ in ()).throw(ValueError("cannot decode")))
        self.assertIsNone(cache.peek("broken"))
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_safe_from_several_threads(self):
        cache = wallpapers.ThumbnailCache(limit=16)

        def work(offset: int) -> None:
            for index in range(200):
                cache.get((offset + index) % 40, lambda: object())
        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(cache), 16)


if HAVE_QT:
    from boswas_control_center.ui.settings import personalize

    class LookCase(testing.FluentTestCase):
        def setUp(self) -> None:
            super().setUp()
            self.config_dir = Path(os.environ["XDG_CONFIG_HOME"], "boswas")
            self.look = self.config_dir / "appearance.toml"
            self.user = self.config_dir / "compositor.toml"
            self.home = self.tmp / "home"
            (self.home / "Pictures").mkdir(parents=True)
            self._home = os.environ.get("HOME")
            os.environ["HOME"] = str(self.home)
            self.system = self.tmp / "wallpapers"
            self.system_files = [self.tmp / "defaults.toml", self.tmp / "etc.toml"]
            self.compositor = shared.FakeCompositor(shared.HEADLESS)
            self.settings = cc.Settings(dict(os.environ), system_files=tuple(map(str, self.system_files)),
                                        control=self.compositor)
            self.compositor.attach(self.settings)
            for index, name in enumerate(("Aurora", "Ocean", "Pearl")):
                directory = self.system / f"Boswas-{name}" / "contents"
                for folder, shade in (("images", 200), ("images_dark", 40)):
                    (directory / folder).mkdir(parents=True)
                    image = QImage(96, 54, QImage.Format.Format_RGB32)
                    image.fill(QColor(shade, 60 + index * 60, 255 - index * 80))
                    image.save(str(directory / folder / "3840x2160.png"))
                (directory.parent / "metadata.json").write_text('{"KPlugin": {"Name": "Boswas %s"}}' % name)
            self.photo = self.home / "Pictures" / "Holiday.jpg"
            image = QImage(80, 120, QImage.Format.Format_RGB32)
            image.fill(QColor(30, 160, 90))
            image.save(str(self.photo), "JPG")

        def tearDown(self) -> None:
            if self._home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = self._home
            super().tearDown()

        def open(self, section, height: int = 500):
            self.show(section, 900, height)
            section.activate()
            shared.settle(section)
            return section

        def get(self, table: str, key: str):
            return self.settings.snapshot().get(table, key)

        def art(self, name: str, folder: str = "images_dark") -> str:
            return str(self.system / f"Boswas-{name}" / "contents" / folder / "3840x2160.png")

    class ColourModeTest(LookCase):
        def test_the_mode_applies_at_once(self):
            section = self.open(personalize.ColourModeSection(config=self.settings, wallpaper_dir=str(self.system)))
            self.assertTrue(section.tiles["dark"].isChecked())
            self.assertFalse(section.tiles["light"].isChecked())
            testing.click(section.tiles["light"])
            shared.settle(section)
            self.assertEqual(self.get("appearance", "scheme"), "light")
            self.assertEqual(cc._parse(self.look.read_bytes())[0], {"appearance": {"scheme": "light"}})
            self.assertFalse(self.user.exists())
            self.assertTrue(section.tiles["light"].isChecked() and not section.tiles["dark"].isChecked())
            self.assertEqual(section.tiles["light"].accessibleDescription(), "Selected")
            self.assertEqual(self.compositor.reloads, 1)
            testing.grab(section, self.tmp / "colour-mode.png")

        def test_a_boswas_wallpaper_follows_the_mode(self):
            self.config_dir.mkdir(parents=True)
            self.look.write_text('[appearance]\nscheme = "dark"\nwallpaper = "%s"\n' % self.art("Ocean"))
            section = self.open(personalize.ColourModeSection(config=self.settings, wallpaper_dir=str(self.system)))
            testing.click(section.tiles["light"])
            shared.settle(section)
            self.assertEqual((self.get("appearance", "scheme"), self.get("appearance", "wallpaper")),
                             ("light", self.art("Ocean", "images")))
            self.look.write_text('[appearance]\nscheme = "light"\nwallpaper = "%s"\n' % self.photo)
            shared.settle(section)
            testing.click(section.tiles["dark"])
            shared.settle(section)
            self.assertEqual((self.get("appearance", "scheme"), self.get("appearance", "wallpaper")),
                             ("dark", str(self.photo)))                    # the user's own picture stays

    class BackgroundTest(LookCase):
        def background(self, **keywords):
            section = personalize.BackgroundSection(config=self.settings, wallpaper_dir=str(self.system), **keywords)
            self.open(section, 640)
            self.assertTrue(testing.wait_until(lambda: all(tile.pixmap is not None for tile in section.tiles), 10))
            return section

        def test_the_grid_offers_wallpapers_and_pictures(self):
            section = self.background()
            self.assertEqual([tile.text() for tile in section.boswas_grid.tiles],
                             ["Boswas Aurora", "Boswas Ocean", "Boswas Pearl"])
            self.assertEqual([tile.text() for tile in section.pictures_grid.tiles], ["Holiday"])
            self.assertTrue(all(not tile.pixmap.isNull() for tile in section.tiles))
            self.assertFalse(any(tile.isChecked() for tile in section.tiles))
            self.assertEqual(section.header.description(), "The default Boswas background")
            self.assertFalse(section.default.isEnabled())
            first, second = section.boswas_grid.tiles[:2]
            self.assertEqual(first.y(), second.y())                        # side by side
            self.assertGreater(second.x(), first.x() + first.width() - 1)
            testing.grab(section, self.tmp / "background.png")

        def test_choosing_a_wallpaper_a_picture_and_the_default(self):
            section = self.background()
            ocean = section.boswas_grid.tiles[1]
            testing.click(ocean)
            shared.settle(section)
            self.assertEqual(self.get("appearance", "wallpaper"), self.art("Ocean"))
            self.assertEqual([tile.text() for tile in section.tiles if tile.isChecked()], ["Boswas Ocean"])
            self.assertEqual(section.header.description(), "Boswas Ocean")
            self.assertTrue(section.default.isEnabled())
            testing.click(section.pictures_grid.tiles[0])
            shared.settle(section)
            self.assertEqual(self.get("appearance", "wallpaper"), str(self.photo))
            self.assertEqual([tile.text() for tile in section.tiles if tile.isChecked()], ["Holiday"])
            testing.click(section.default)
            shared.settle(section)
            self.assertEqual(self.get("appearance", "wallpaper"), "")
            self.assertFalse(self.look.exists())
            self.assertFalse(any(tile.isChecked() for tile in section.tiles))
            self.assertEqual(self.compositor.reloads, 3)

        def test_browse_for_any_picture(self):
            elsewhere = self.tmp / "Sea at night.png"
            image = QImage(64, 36, QImage.Format.Format_RGB32)
            image.fill(QColor(10, 20, 90))
            image.save(str(elsewhere))
            answers = [str(elsewhere)]
            section = self.background(choose_file=lambda: answers.pop(0) if answers else "")
            testing.click(section.browse)
            shared.settle(section)
            self.assertEqual(self.get("appearance", "wallpaper"), str(elsewhere))
            self.assertTrue(testing.wait_until(lambda: [t.text() for t in section.tiles if t.isChecked()]
                                               == ["Sea at night"], 5))
            self.assertEqual(section.header.description(), "Sea at night")
            testing.click(section.browse)                                   # the dialog was cancelled
            shared.settle(section)
            self.assertEqual(self.get("appearance", "wallpaper"), str(elsewhere))
            text = self.tmp / "notes.png"
            text.write_text("not a picture")
            answers.append(str(text))
            testing.click(section.browse)
            shared.settle(section)
            self.assertEqual((section.notice.severity(), section.notice.message()),
                             ("error", "Only PNG and JPEG pictures can be used as the background."))
            self.assertEqual(self.get("appearance", "wallpaper"), str(elsewhere))

        def test_the_current_wallpaper_of_either_mode_is_marked(self):
            self.config_dir.mkdir(parents=True)
            self.look.write_text('[appearance]\nscheme = "dark"\nwallpaper = "%s"\n' % self.art("Pearl", "images"))
            section = self.background()
            self.assertEqual([tile.text() for tile in section.tiles if tile.isChecked()], ["Boswas Pearl"])
            self.look.write_text('[appearance]\nwallpaper = "/gone/away.png"\n')
            shared.settle(section)
            testing.wait_until(lambda: "cannot be read" in section.header.description(), 5)
            self.assertIn("/gone/away.png (this picture cannot be read", section.header.description())
            self.assertFalse(any(tile.isChecked() for tile in section.tiles))

        def test_a_picture_that_cannot_be_decoded_is_marked(self):
            (self.home / "Pictures" / "Broken.png").write_bytes(PNG + b"not really")
            section = self.background()
            broken = next(tile for tile in section.tiles if tile.text() == "Broken")
            self.assertTrue(broken.failed and broken.pixmap.isNull())
            self.assertIn("cannot be shown", broken.toolTip())

        def test_nothing_to_offer(self):
            self.photo.unlink()
            section = personalize.BackgroundSection(config=self.settings, wallpaper_dir=str(self.tmp / "none"))
            self.open(section)
            self.assertTrue(section.empty.isVisible())
            self.assertFalse(section.boswas_label.isVisible() or section.pictures_label.isVisible())

    class MotionAndTimesTest(LookCase):
        def test_animation_effects(self):
            section = self.open(personalize.MotionSection(config=self.settings))
            self.assertTrue(section.effects.isChecked())
            testing.click(section.effects)
            shared.settle(section)
            self.assertTrue(self.get("appearance", "reduced_motion"))
            self.assertEqual(cc._parse(self.user.read_bytes())[0], {"appearance": {"reduced_motion": True}})
            self.assertFalse(section.effects.isChecked())
            testing.click(section.effects)
            shared.settle(section)
            self.assertFalse(self.get("appearance", "reduced_motion"))

        def labels(self, box) -> list[tuple[str, bool]]:
            return [(box.itemText(i), box.model().item(i).isEnabled()) for i in range(box.count())]

        def test_the_lock_time_is_limited_by_the_organisation(self):
            section = self.open(personalize.LockScreenSection(config=self.settings))
            self.assertEqual(section.box.currentText(), "10 minutes")
            self.assertEqual(self.labels(section.box),
                             [("1 minute", True), ("2 minutes", True), ("5 minutes", True), ("10 minutes", True),
                              ("15 minutes", False), ("30 minutes", False), ("Never", False)])
            self.assertEqual(section.row.description(),
                             "Your organisation requires the screen to lock after at most 10 minutes")
            shared.choose(section.box, 120)
            shared.settle(section)
            self.assertEqual(self.get("lock", "idle_seconds"), 120)
            shared.choose(section.box, 1800)                                # a disabled entry cannot be picked
            shared.settle(section)
            self.assertEqual((self.get("lock", "idle_seconds"), section.box.currentText()), (120, "2 minutes"))
            section.change({("lock", "idle_seconds"): 0})                   # and the backend refuses it too
            shared.settle(section)
            self.assertEqual(section.notice.severity(), "error")
            self.assertIn("organisation", section.notice.message())
            self.assertEqual(self.get("lock", "idle_seconds"), 120)
            testing.grab(section, self.tmp / "lock.png")

        def test_without_a_limit_never_is_offered(self):
            self.system_files[1].write_text("[policy]\nlock_idle_max_seconds = 0\n[lock]\nidle_seconds = 420\n")
            section = self.open(personalize.LockScreenSection(config=self.settings))
            self.assertEqual(section.box.currentText(), "7 minutes")        # a value that is not in the list
            self.assertTrue(all(enabled for _, enabled in self.labels(section.box)))
            self.assertIn("has not been used for this long", section.row.description())
            shared.choose(section.box, 0)
            shared.settle(section)
            self.assertEqual((self.get("lock", "idle_seconds"), section.box.currentText()), (0, "Never"))
            self.assertEqual(section.box.count(), 7)                        # the odd value is gone from the list

        def test_a_stricter_limit(self):
            self.system_files[1].write_text("[policy]\nlock_idle_max_seconds = 180\n")
            section = self.open(personalize.LockScreenSection(config=self.settings))
            self.assertEqual(section.box.currentText(), "3 minutes")
            self.assertEqual([label for label, enabled in self.labels(section.box) if enabled],
                             ["1 minute", "2 minutes", "3 minutes"])
            self.assertIn("at most 3 minutes", section.row.description())

        def test_turn_off_the_screen_after(self):
            section = self.open(personalize.ScreenSection(config=self.settings))
            self.assertEqual(section.box.currentText(), "5 minutes")
            shared.choose(section.box, 900)
            shared.settle(section)
            self.assertEqual(self.get("power", "screen_off_seconds"), 900)
            shared.choose(section.box, 0)
            shared.settle(section)
            self.assertEqual((self.get("power", "screen_off_seconds"), section.box.currentText()), (0, "Never"))
            self.assertTrue(all(enabled for _, enabled in self.labels(section.box)))

        def test_duration_words(self):
            self.assertEqual([personalize.duration_text(s) for s in (0, 45, 60, 420, 3600, 7200, 5400, 61)],
                             ["Never", "45 seconds", "1 minute", "7 minutes", "1 hour", "2 hours", "90 minutes",
                              "61 seconds"])

    class ContractTest(testing.FluentTestCase):
        def test_sections_follow_the_contract(self):
            pages = [(cls.page, cls.title) for cls in personalize.SECTIONS]
            self.assertEqual(pages, [("colours", "Colour mode"), ("background", "Background"),
                                     ("visual-effects", "Motion"), ("lock-screen", "Lock screen"), ("power", "Screen")])
            self.assertEqual(getattr(personalize.MotionSection, "also", ()), (), "one animation switch, on Visual effects")
            for cls in personalize.SECTIONS:
                self.assertTrue(isinstance(cls.order, int) and cls.keywords)
                section = self.keep(cls())
                self.assertIsInstance(section.config, cc.Settings)
                section.deactivate()
            self.assertIn("lock", personalize.LockScreenSection.keywords)

        def test_the_code_names_no_colour(self):
            import re
            for name in ("display.py", "input.py", "personalize.py"):
                source = (Path(personalize.__file__).parent / name).read_text(encoding="utf-8")
                self.assertEqual(re.findall(r"#[0-9A-Fa-f]{6}\b|setStyleSheet|QColor\(", source), [], name)


if __name__ == "__main__":
    unittest.main()
