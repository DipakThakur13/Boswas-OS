"""Pictures of the Snipping Tool (QtGui): reading with limits, the marks drawn at full resolution, writing."""

from __future__ import annotations

import contextlib
import os
import struct
import sys
import unittest
import zlib
from pathlib import Path
from unittest import mock

from boswas_fluent import testing

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

from boswas_apps.snipping_tool import annotations, picture  # noqa: E402
from boswas_apps.snipping_tool.annotations import Rect, State  # noqa: E402

RED = "#FF0000"
BLUE = "#0000FF"


def rgb(image: QImage, x: int, y: int) -> tuple[int, int, int]:
    colour = image.pixelColor(x, y)
    return colour.red(), colour.green(), colour.blue()


def near(a, b, tolerance: int = 12) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def plain(width: int, height: int, colour=(200, 200, 200)) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(*colour))
    return image


@contextlib.contextmanager
def quiet_stderr():
    """libpng prints its complaint about a damaged file straight to the error output; keep the test output clean."""
    sys.stderr.flush()
    saved = os.dup(2)
    try:
        with open(os.devnull, "wb") as null:
            os.dup2(null.fileno(), 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


def png_header(width: int, height: int) -> bytes:
    """A PNG that claims a size in its header and has no pixel data worth decoding."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b""))


class LoadTest(testing.FluentTestCase):
    def test_png_jpeg_and_webp_are_read_by_their_content(self):
        source = plain(64, 40, (10, 120, 230))
        for fmt, name in (("PNG", "a.png"), ("JPEG", "b.jpg"), ("WEBP", "c.webp")):
            path = self.tmp / name
            if not source.save(str(path), fmt):
                self.skipTest(f"{fmt} cannot be written here")
            image = picture.load(path)
            self.assertEqual((image.width(), image.height()), (64, 40))
            self.assertEqual(image.format(), QImage.Format.Format_RGB32)
            self.assertTrue(near(rgb(image, 5, 5), (10, 120, 230)))
        # the name does not matter, the content does
        (self.tmp / "a.png").rename(self.tmp / "misnamed.jpg")
        self.assertEqual(picture.load(self.tmp / "misnamed.jpg").width(), 64)

    def test_transparency_is_kept(self):
        image = QImage(8, 8, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        image.save(str(self.tmp / "clear.png"))
        loaded = picture.load(self.tmp / "clear.png")
        self.assertTrue(loaded.hasAlphaChannel())
        self.assertEqual(loaded.format(), QImage.Format.Format_ARGB32_Premultiplied)

    def test_what_is_refused(self):
        (self.tmp / "empty.png").write_bytes(b"")
        (self.tmp / "text.png").write_text("hello, this is not a picture")
        (self.tmp / "gif.gif").write_bytes(b"GIF89a" + b"\x00" * 30)
        (self.tmp / "cut.png").write_bytes(png_header(10, 10)[:30])
        (self.tmp / "huge.png").write_bytes(png_header(20000, 100))
        (self.tmp / "heavy.png").write_bytes(png_header(10000, 9000))          # 360 MB decoded
        (self.tmp / "folder.png").mkdir()
        cases = {"empty.png": "empty", "text.png": "not a picture", "gif.gif": "not a picture",
                 "cut.png": "damaged", "huge.png": "too large", "heavy.png": "too large",
                 "folder.png": "not a picture", "missing.png": "no longer there"}
        for name, words in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(picture.PictureError) as caught, quiet_stderr():
                    picture.load(self.tmp / name)
                self.assertIn(words, caught.exception.title)
        with mock.patch.object(picture, "MAX_FILE_BYTES", 10):
            (self.tmp / "big.png").write_bytes(png_header(4, 4))
            with self.assertRaises(picture.PictureError) as caught:
                picture.load(self.tmp / "big.png")
            self.assertIn("too large", caught.exception.title)

    def test_the_limits(self):
        picture.check_size(16384, 4096)
        for width, height in ((16385, 10), (10, 16385), (16384, 16384), (0, 10)):
            with self.assertRaises(picture.PictureError):
                picture.check_size(width, height)
        with self.assertRaises(picture.PictureError):
            picture.accept_image(QImage())
        image = picture.accept_image(plain(5, 5))
        self.assertEqual(image.size().width(), 5)

    def test_sniffing(self):
        self.assertEqual(picture.sniff(b"\x89PNG\r\n\x1a\nxxxx"), "png")
        self.assertEqual(picture.sniff(b"\xff\xd8\xff\xe0"), "jpeg")
        self.assertEqual(picture.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")
        self.assertIsNone(picture.sniff(b"RIFF\x00\x00\x00\x00WAVE"))
        self.assertIsNone(picture.sniff(b""))


class RenderTest(testing.FluentTestCase):
    family = "Inter"

    def test_the_marks_are_drawn_at_the_pictures_full_resolution(self):
        image = plain(3000, 2000)
        line = annotations.stroke("pen", RED, 8, [(100, 1000), (2900, 1000)])
        box = annotations.shape("rectangle", BLUE, 6, (2000, 200), (2800, 600))
        result = picture.render(image, State((line, box)), self.family)
        self.assertEqual((result.width(), result.height()), (3000, 2000))
        self.assertEqual(rgb(result, 1500, 1000), (255, 0, 0))
        self.assertEqual(rgb(result, 1500, 1004 + 2), (200, 200, 200), "the line is 8 picture pixels wide")
        self.assertEqual(rgb(result, 2000, 400), (0, 0, 255))
        self.assertEqual(rgb(result, 2400, 400), (200, 200, 200), "a rectangle is an outline")
        self.assertEqual(rgb(image, 1500, 1000), (200, 200, 200), "the picture itself is not changed")

    def test_the_highlighter_lets_the_picture_show_through(self):
        image = plain(200, 100, (0, 0, 0))
        mark = annotations.stroke("highlighter", "#FFFF00", 20, [(20, 50), (180, 50)])
        result = picture.render(image, State((mark,)), self.family)
        red, green, blue = rgb(result, 100, 50)
        self.assertTrue(80 <= red <= 125 and 80 <= green <= 125 and blue == 0, (red, green, blue))
        crossing = annotations.stroke("highlighter", "#FFFF00", 20, [(20, 50), (180, 50), (100, 20), (100, 90)])
        twice = picture.render(image, State((crossing,)), self.family)
        self.assertEqual(rgb(twice, 100, 50), rgb(result, 100, 50), "one stroke never darkens itself")

    def test_a_crop_is_exported_at_its_pixels_with_the_marks_moved(self):
        image = plain(800, 600)
        image.setPixelColor(300, 200, QColor(0, 255, 0))
        arrow = annotations.shape("arrow", RED, 6, (320, 260), (420, 260))
        state = State((arrow,)).cropped(Rect(300, 200, 200, 100))
        result = picture.render(image, state, self.family)
        self.assertEqual((result.width(), result.height()), (200, 100))
        self.assertEqual(rgb(result, 0, 0), (0, 255, 0))
        self.assertEqual(rgb(result, 60, 60), (255, 0, 0))
        self.assertEqual(rgb(result, 119, 60), (255, 0, 0), "the head is filled up to its tip")

    def test_text_and_dots(self):
        image = plain(300, 120, (255, 255, 255))
        size = 40.0
        box = picture.measure_text("HHH", size, self.family)
        self.assertGreater(box[0], 40)
        self.assertGreater(box[1], 30)
        text = annotations.text_item("#000000", size, (20, 20), "HHH", box)
        dot = annotations.stroke("pen", BLUE, 10, [(250, 60)])
        result = picture.render(image, State((text, dot)), self.family)
        dark = sum(1 for x in range(20, 20 + int(box[0])) for y in range(20, 20 + int(box[1]))
                   if rgb(result, x, y)[0] < 100)
        self.assertGreater(dark, 100, "the text is drawn inside its box")
        self.assertEqual(rgb(result, 250, 60), (0, 0, 255))


class WriteTest(testing.FluentTestCase):
    def test_png_and_jpeg(self):
        image = QImage(40, 30, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        mark = annotations.stroke("pen", RED, 6, [(5, 15), (35, 15)])
        png = picture.save(image, State((mark,)), self.tmp / "a.png", "png", "Inter")
        jpeg = picture.save(image, State((mark,)), self.tmp / "a.jpg", "jpeg", "Inter")
        back = QImage(png)
        self.assertEqual((back.width(), back.height()), (40, 30))
        self.assertEqual(back.pixelColor(2, 2).alpha(), 0, "PNG keeps transparency")
        self.assertEqual(rgb(back, 20, 15), (255, 0, 0))
        self.assertEqual(Path(jpeg).read_bytes()[:3], b"\xff\xd8\xff")
        back = QImage(jpeg)
        self.assertTrue(near(rgb(back, 2, 2), (255, 255, 255)), "JPEG puts transparency on white")
        self.assertTrue(near(rgb(back, 20, 15), (255, 0, 0), 40))

    def test_format_for_path(self):
        self.assertEqual(picture.format_for_path("/a/b.PNG"), "png")
        self.assertEqual(picture.format_for_path("b.jpeg"), "jpeg")
        self.assertEqual(picture.format_for_path("b.jpg"), "jpeg")
        self.assertIsNone(picture.format_for_path("b.webp"))
        self.assertIsNone(picture.format_for_path("b"))

    def test_writing_is_atomic_and_keeps_permissions(self):
        target = self.tmp / "keep.png"
        target.write_bytes(b"old")
        os.chmod(target, 0o640)
        picture.save(plain(4, 4), State(), target, "png", "Inter")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(target.read_bytes()[:4], b"\x89PNG")
        with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(picture.PictureError) as caught:
                picture.save(plain(4, 4), State(), target, "png", "Inter")
        self.assertIn("No space left", caught.exception.detail)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         sorted(["keep.png", "xdg_cache_home", "xdg_config_home", "xdg_data_home", "xdg_runtime_dir",
                                 "xdg_state_home"]))
        new = picture.save(plain(4, 4), State(), self.tmp / "new.png", "png", "Inter")
        self.assertEqual(Path(new).stat().st_mode & 0o777, 0o644)
        with self.assertRaises(picture.PictureError):
            picture.save(plain(4, 4), State(), self.tmp / "missing" / "x.png", "png", "Inter")


if __name__ == "__main__":
    unittest.main()
