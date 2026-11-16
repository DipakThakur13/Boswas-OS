"""Photos: reading, editing and writing pictures (QtGui on the offscreen platform, no widgets shown).

Every picture is made here, in a temporary directory; pixels are checked
after each operation.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import photos_fixtures as fx  # noqa: E402

from boswas_fluent import testing  # noqa: E402

from PySide6.QtCore import QRectF, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter  # noqa: E402

from boswas_apps.photos import (decode, desktopcfg, editops, exif, imageio, library, printing, thumbcache,  # noqa: E402
                                thumbs, wallpaper)

RED, GREEN, BLUE, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)


def rgb(image: QImage, x: int, y: int) -> tuple[int, int, int]:
    colour = image.pixelColor(x, y)
    return colour.red(), colour.green(), colour.blue()


def quadrants(width: int = 40, height: int = 20, alpha: bool = False) -> QImage:
    """Red top left, green top right, blue bottom left, white bottom right."""
    image = QImage(width, height, QImage.Format.Format_ARGB32 if alpha else QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    half_w, half_h = width // 2, height // 2
    for x, y, colour in ((0, 0, RED), (half_w, 0, GREEN), (0, half_h, BLUE), (half_w, half_h, WHITE)):
        painter.fillRect(x, y, half_w, half_h, QColor(*colour))
    painter.end()
    return image


def corners(image: QImage) -> list[tuple[int, int, int]]:
    """The colours near the four corners: top left, top right, bottom left, bottom right."""
    w, h = image.width(), image.height()
    return [rgb(image, 2, 2), rgb(image, w - 3, 2), rgb(image, 2, h - 3), rgb(image, w - 3, h - 3)]


def near(a, b, tolerance: int = 12) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def gradient(width: int = 256, height: int = 64) -> QImage:
    """Grey ramp left to right in the top half, a colour ramp in the bottom half."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    for x in range(width):
        level = round(x * 255 / (width - 1))
        for y in range(height // 2):
            image.setPixelColor(x, y, QColor(level, level, level))
        for y in range(height // 2, height):
            image.setPixelColor(x, y, QColor(level, 255 - level, 128))
    return image


class ImageCase(testing.FluentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pics = self.tmp / "pics"
        self.pics.mkdir()

    def save(self, image: QImage, name: str, quality: int = -1) -> str:
        path = str(self.pics / name)
        self.assertTrue(image.save(path, None, quality), name)
        return path

    def assert_corners(self, image: QImage, expected, tolerance: int = 12) -> None:
        found = corners(image)
        for got, want in zip(found, expected):
            self.assertTrue(near(got, want, tolerance), f"{found} is not {expected}")


class DecodeTest(ImageCase):
    def test_formats_keep_their_pixels(self):
        source = quadrants()
        for name in ("a.png", "a.bmp", "a.jpg", "a.webp", "a.tif", "a.ppm"):
            extension = name.rsplit(".", 1)[1]
            if extension not in decode.supported_extensions():
                continue
            path = self.save(source, name, 100 if extension in ("jpg", "webp") else -1)
            info = decode.probe(path)
            self.assertEqual((info.width, info.height, info.animated, info.vector), (40, 20, False, False), name)
            loaded = decode.load(path)
            self.assertFalse(loaded.reduced or loaded.truncated)
            self.assertEqual(loaded.image.size(), QSize(40, 20))
            self.assertIn(loaded.image.format(), (QImage.Format.Format_RGB32, QImage.Format.Format_ARGB32_Premultiplied))
            self.assert_corners(loaded.image, [RED, GREEN, BLUE, WHITE], 40 if extension in ("jpg", "webp") else 0)

    def test_the_content_decides_not_the_extension(self):
        path = self.save(quadrants(), "really-a.png")
        wrong = str(self.pics / "named.jpg")
        os.rename(path, wrong)
        loaded = decode.load(wrong)
        self.assertEqual(loaded.info.format, "png")
        self.assert_corners(loaded.image, [RED, GREEN, BLUE, WHITE], 0)

    def test_all_eight_orientations(self):
        plain = Path(self.save(quadrants(), "plain.jpg", 100)).read_bytes()
        # where the red, green, blue and white corners end up, per EXIF orientation
        expected = {
            1: [RED, GREEN, BLUE, WHITE], 2: [GREEN, RED, WHITE, BLUE], 3: [WHITE, BLUE, GREEN, RED],
            4: [BLUE, WHITE, RED, GREEN], 5: [RED, BLUE, GREEN, WHITE], 6: [BLUE, RED, WHITE, GREEN],
            7: [WHITE, GREEN, BLUE, RED], 8: [GREEN, WHITE, RED, BLUE],
        }
        for orientation, colours in expected.items():
            path = fx.write(self.pics / f"o{orientation}.jpg", fx.jpeg_with_block(plain, fx.camera_exif(orientation)))
            loaded = decode.load(str(path))
            turned = orientation >= 5
            self.assertEqual(loaded.image.size(), QSize(20, 40) if turned else QSize(40, 20), orientation)
            self.assertEqual((loaded.info.width, loaded.info.height), (20, 40) if turned else (40, 20))
            self.assertEqual(loaded.info.orientation, orientation)
            self.assert_corners(loaded.image, colours, 40)
            # our own turning agrees with the decoder's
            self.assert_corners(decode.apply_orientation(quadrants(), orientation), colours, 0)
            small, _info = decode.load_scaled(str(path), 16)
            self.assertEqual(max(small.width(), small.height()), 16)
            self.assert_corners(small, colours, 60)

    def test_orientation_transform_matches_the_exif_matrices(self):
        for orientation, (a, b, c, d) in exif.ORIENTATION_MATRICES.items():
            point = decode.orientation_transform(orientation).map(3.0, 5.0)
            self.assertEqual(point, (a * 3 + b * 5, c * 3 + d * 5))

    def test_animated_gif(self):
        path = fx.write(self.pics / "anim.gif", fx.gif([RED, GREEN, BLUE], 8, 6))
        info = decode.probe(str(path))
        self.assertEqual((info.format, info.width, info.height), ("gif", 8, 6))
        self.assertTrue(info.animated)
        self.assertEqual(info.frames, 3)
        loaded = decode.load(str(path))
        self.assertEqual(rgb(loaded.image, 4, 3), RED)                  # the first frame stands for the animation
        reader = QImageReader(str(path))
        frames = [rgb(reader.read(), 4, 3) for _ in range(3)]
        self.assertEqual(frames, [RED, GREEN, BLUE])
        self.assertFalse(decode.probe(str(fx.write(self.pics / "still.gif", fx.gif([GREEN])))).animated)

    def test_truncated_jpeg_shows_what_is_there_and_says_so(self):
        import random
        noise = random.Random(3)
        source = QImage(160, 160, QImage.Format.Format_RGB32)
        source.fill(QColor(200, 30, 30))
        for y in range(40, 160):                                # detail, so that the scan is most of the file
            for x in range(160):
                source.setPixelColor(x, y, QColor(noise.randrange(256), noise.randrange(256), noise.randrange(256)))
        data = Path(self.save(source, "whole.jpg", 90)).read_bytes()
        self.assertFalse(decode.load(str(self.pics / "whole.jpg")).truncated)
        cut = fx.write(self.pics / "cut.jpg", data[:len(data) * 2 // 3])
        loaded = decode.load(str(cut))
        self.assertTrue(loaded.truncated)
        self.assertEqual(loaded.image.size(), QSize(160, 160))
        self.assertTrue(near(rgb(loaded.image, 3, 3), (200, 30, 30), 20))
        with self.assertRaises(decode.DecodeError) as caught:
            decode.load(str(fx.write(self.pics / "stub.jpg", data[:40])))
        self.assertEqual(caught.exception.kind, "corrupt")

    def test_a_png_bomb_is_refused_before_any_decoding(self):
        path = fx.write(self.pics / "bomb.png", fx.png_bomb(60000, 60000))
        with mock.patch.object(QImageReader, "read", side_effect=AssertionError("must not decode")):
            for call in (lambda: decode.load(str(path)), lambda: decode.load_scaled(str(path), 256),
                         lambda: decode.read_as_stored(str(path))):
                with self.assertRaises(decode.DecodeError) as caught:
                    call()
                self.assertEqual(caught.exception.kind, "too-large")
                self.assertIn("60 000 × 60 000", str(caught.exception))
        with self.assertRaises(thumbs.ThumbnailError):
            thumbs.thumbnail(str(path), 1.0)
        # a header that lies less boldly is tried and found damaged, not trusted
        liar = fx.write(self.pics / "liar.png", fx.png_bomb(3000, 3000))
        with self.assertRaises(decode.DecodeError) as caught:
            decode.load(str(liar))
        self.assertEqual(caught.exception.kind, "corrupt")

    def test_a_strip_one_pixel_wide(self):
        strip = QImage(1, 100000, QImage.Format.Format_RGB32)
        strip.fill(QColor(10, 200, 30))
        path = self.save(strip, "strip.png")
        loaded = decode.load(path)
        self.assertEqual(loaded.image.size(), QSize(1, 100000))
        self.assertFalse(loaded.reduced)
        small, info = decode.load_scaled(path, 256)
        self.assertEqual(small.size(), QSize(1, 256))
        self.assertEqual(rgb(small, 0, 128), (10, 200, 30))
        self.assertEqual(decode.fit_size(100000, 1, 256), QSize(256, 1))
        self.assertEqual(decode.fit_size(10, 5, 256), QSize(10, 5))
        self.assertEqual(decode.fit_size(0, 0, 256), QSize(1, 1))
        self.assertEqual(thumbs.thumbnail(path, os.stat(path).st_mtime).size(), QSize(1, 256))

    def test_large_pictures_are_reduced_to_the_pixel_budget(self):
        big = QImage(3000, 2000, QImage.Format.Format_RGB32)
        big.fill(QColor(9, 99, 199))
        for name in ("big.jpg", "big.png"):
            path = self.save(big, name, 80)
            loaded = decode.load(path, max_pixels=1_000_000)
            self.assertTrue(loaded.reduced)
            self.assertLessEqual(loaded.image.width() * loaded.image.height(), 1_000_000)
            self.assertEqual((loaded.info.width, loaded.info.height), (3000, 2000))       # the real size is still known
            self.assertAlmostEqual(loaded.image.width() / loaded.image.height(), 1.5, places=2)
            self.assertTrue(near(rgb(loaded.image, 5, 5), (9, 99, 199), 6))
        self.assertEqual(decode.load(self.save(big, "half.jpg", 80), max_pixels=1_600_000).image.size(), QSize(1500, 1000))
        self.assertFalse(decode.load(str(self.pics / "big.png")).reduced)

    def test_decodes_that_need_much_memory_run_one_at_a_time(self):
        image = QImage(200, 200, QImage.Format.Format_RGB32)
        image.fill(QColor(1, 2, 3))
        path = self.save(image, "x.png")
        running = []
        peak = []
        real_read = QImageReader.read

        def slow_read(reader, *args):
            running.append(1)
            peak.append(len(running))
            time.sleep(0.05)
            result = real_read(reader, *args)
            running.pop()
            return result

        with mock.patch.object(decode, "HEAVY_BYTES", 1), mock.patch.object(QImageReader, "read", slow_read):
            workers = [threading.Thread(target=decode.load, args=(path,)) for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertEqual(max(peak), 1)

    def test_what_is_wrong_with_a_file_is_said_in_plain_words(self):
        cases = {
            "missing.png": (None, "missing"),
            "empty.png": (b"", "empty"),
            "text.png": (b"just some words, no picture", "not-a-picture"),
            "notes.txt": (b"just some words", "not-a-picture"),
            "cut.png": (b"\x89PNG\r\n\x1a\n" + fx.png_chunk(b"IHDR", b"\x00" * 13), "corrupt"),
            "new.heic": (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64, "unsupported"),
            "new.avif": (b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"\x00" * 64, "unsupported"),
        }
        for name, (content, kind) in cases.items():
            path = self.pics / name
            if content is not None:
                path.write_bytes(content)
            if kind == "unsupported" and name.rsplit(".", 1)[1] in decode.supported_extensions():
                continue                                # the plugin is installed here
            with self.assertRaises(decode.DecodeError, msg=name) as caught:
                decode.load(str(path))
            self.assertEqual(caught.exception.kind, kind, name)
            self.assertTrue(caught.exception.title and caught.exception.detail)
            self.assertNotIn("Traceback", str(caught.exception))
        with self.assertRaises(decode.DecodeError) as caught:
            decode.load(str(self.pics))
        self.assertEqual(caught.exception.kind, "not-a-picture")
        self.assertIn("kimageformat6-plugins", decode.missing_format_hint("heif"))
        self.assertIn("qt6-image-formats-plugins", decode.missing_format_hint("webp"))

    def test_unreadable_file(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        path = self.save(quadrants(), "locked.png")
        os.chmod(path, 0)
        with self.assertRaises(decode.DecodeError) as caught:
            decode.load(path)
        self.assertEqual(caught.exception.kind, "unreadable")

    def test_transparency_and_depth_are_reported(self):
        loaded = decode.load(self.save(quadrants(alpha=True).copy(0, 0, 40, 20), "alpha.png"))
        self.assertTrue(loaded.info.has_alpha)
        self.assertEqual(loaded.image.format(), QImage.Format.Format_ARGB32_Premultiplied)
        opaque = decode.load(self.save(quadrants(), "opaque.png"))
        self.assertFalse(opaque.info.has_alpha)
        deep = QImage(8, 8, QImage.Format.Format_RGBA64)
        deep.fill(QColor(255, 128, 0, 128))
        loaded = decode.load(self.save(deep, "deep.png"))
        self.assertEqual(loaded.info.bits_per_pixel, 64)
        self.assertEqual(loaded.image.depth(), 32)                      # shown with 8 bits per channel
        self.assertEqual(decode.read_as_stored(str(self.pics / "deep.png")).depth(), 64)

    def test_svg(self):
        path = fx.write(self.pics / "flag.svg", fx.SVG.encode())
        info = decode.probe(str(path))
        self.assertEqual((info.format, info.width, info.height, info.vector), ("svg", 40, 20, True))
        loaded = decode.load(str(path))
        self.assertEqual(loaded.svg, fx.SVG.encode())
        self.assertEqual(loaded.image.size(), QSize(1024, 512))         # a small drawing gets a useful raster copy
        self.assertEqual(rgb(loaded.image, 100, 100), RED)
        self.assertEqual(rgb(loaded.image, 900, 100), BLUE)
        renderer = decode.svg_renderer(loaded.svg)
        # crisp at any zoom: the part around the colour border, rendered as if the drawing were 40 000 wide
        part = decode.render_svg(renderer, QSize(40000, 20000), QRectF(19990, 5000, 20, 10), QSize(200, 100))
        self.assertEqual(part.size(), QSize(200, 100))
        self.assertEqual(rgb(part, 95, 50), RED)
        self.assertEqual(rgb(part, 104, 50), BLUE)
        small, _info = decode.load_scaled(str(path), 20)
        self.assertEqual(small.size(), QSize(20, 10))

    def test_packed_and_hostile_svg(self):
        import gzip
        packed = fx.write(self.pics / "flag.svgz", gzip.compress(fx.SVG.encode()))
        self.assertEqual(decode.load(str(packed)).info.format, "svg")
        with mock.patch.object(decode, "MAX_SVG_BYTES", 4096):
            bomb = fx.write(self.pics / "bomb.svgz", gzip.compress(b"<svg>" + b" " * 1_000_000 + b"</svg>"))
            with self.assertRaises(decode.DecodeError) as caught:
                decode.load(str(bomb))
            self.assertEqual(caught.exception.kind, "too-large")
        for name, content in (("broken.svg", b"<svg><rect"), ("not.svgz", b"\x1f\x8b\x08garbage"),
                              ("nosize.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>')):
            path = fx.write(self.pics / name, content)
            try:
                loaded = decode.load(str(path))
            except decode.DecodeError as error:
                self.assertEqual(error.kind, "corrupt")
            else:
                self.assertGreaterEqual(loaded.image.width(), 1)

    def test_supported_formats_are_consistent(self):
        extensions = decode.supported_extensions()
        self.assertTrue({"png", "jpg", "jpeg", "gif", "bmp", "svg"} <= extensions)
        self.assertNotIn("pdf", extensions)
        self.assertEqual(sorted(set(decode.supported_mime_types())), decode.supported_mime_types())


class EditGeometryTest(ImageCase):
    def test_quarter_turns_and_mirrors_are_exact(self):
        source = quadrants()
        state = editops.EditState()
        right = state.rotated()
        self.assert_corners(editops.render(source, right), [BLUE, RED, WHITE, GREEN], 0)
        self.assertEqual(editops.render(source, right).size(), QSize(20, 40))
        self.assert_corners(editops.render(source, state.rotated(False)), [GREEN, WHITE, RED, BLUE], 0)
        self.assert_corners(editops.render(source, state.flipped(True)), [GREEN, RED, WHITE, BLUE], 0)
        self.assert_corners(editops.render(source, state.flipped(False)), [BLUE, WHITE, RED, GREEN], 0)
        full_turn = state
        for _ in range(4):
            full_turn = full_turn.rotated()
        self.assertEqual(full_turn, state)
        self.assertEqual(editops.render(source, full_turn), source)
        self.assertEqual(state.flipped().flipped(), state)

    def test_crop_takes_the_pixels_it_names(self):
        source = quadrants()
        cropped = editops.render(source, editops.EditState().with_crop((0.5, 0.0, 0.5, 0.5)))
        self.assertEqual(cropped.size(), QSize(20, 10))
        self.assert_corners(cropped, [GREEN] * 4, 0)
        tiny = editops.render(source, editops.EditState().with_crop((0.999, 0.999, 0.0, 0.0)))
        self.assertGreaterEqual(min(tiny.width(), tiny.height()), 1)
        self.assertIsNone(editops.EditState().with_crop((0, 0, 1, 1)).crop)
        wild = editops.EditState().with_crop((-3.0, 0.5, 9.0, float("nan")))
        x, y, w, h = wild.crop
        self.assertTrue(0 <= x and 0 <= y and x + w <= 1.0001 and y + h <= 1.0001 and w > 0 and h > 0)

    def test_the_crop_turns_and_mirrors_with_the_picture(self):
        source = quadrants()
        green = editops.EditState().with_crop((0.5, 0.0, 0.5, 0.5))
        for state in (green.rotated(), green.rotated(False), green.flipped(True), green.flipped(False),
                      green.rotated().flipped(False).rotated(False)):
            result = editops.render(source, state)
            self.assert_corners(result, [GREEN] * 4, 0)
            self.assertEqual(result.width() * result.height(), 200)

    def test_straighten_crops_the_empty_corners_away(self):
        source = QImage(400, 300, QImage.Format.Format_RGB32)
        source.fill(QColor(200, 30, 30))
        for degrees in (-45, -10, 0.5, 20, 45):
            state = editops.EditState().with_straighten(degrees)
            result = editops.render(source, state)
            scale = editops.inscribed_scale(400, 300, degrees)
            self.assertLess(scale, 1.0)
            self.assertEqual(result.size(), QSize(int(400 * scale), int(300 * scale)))
            self.assertAlmostEqual(result.width() / result.height(), 4 / 3, delta=0.03)
            for point in ((0, 0), (result.width() - 1, 0), (0, result.height() - 1),
                          (result.width() - 1, result.height() - 1), (result.width() // 2, result.height() // 2)):
                self.assertTrue(near(rgb(result, *point), (200, 30, 30), 3), (degrees, point, rgb(result, *point)))
        self.assertEqual(editops.inscribed_scale(400, 300, 0), 1.0)
        self.assertEqual(editops.EditState().with_straighten(400).straighten, 45.0)
        self.assertEqual(editops.EditState().with_straighten(float("inf")).straighten, 0.0)

    def test_straighten_turns_clockwise(self):
        source = QImage(200, 200, QImage.Format.Format_RGB32)
        source.fill(QColor(*WHITE))
        painter = QPainter(source)
        painter.fillRect(96, 0, 8, 100, QColor(*RED))             # a bar from the centre straight up
        painter.end()
        result = editops.render(source, editops.EditState().with_straighten(30))
        centre = result.width() // 2
        height = result.height()
        self.assertTrue(near(rgb(result, centre + round(0.5 * 0.25 * height / 0.75 * 1.0), height // 4), RED, 60)
                        or near(rgb(result, centre + 20, height // 2 - 35), RED, 60))
        self.assertTrue(near(rgb(result, centre - 20, height // 2 - 35), WHITE, 10))

    def test_geometry_maps_points_like_the_rendering(self):
        source = quadrants(80, 40)
        state = editops.EditState().rotated().with_crop((0.25, 0.25, 0.5, 0.5))
        placed = editops.geometry(source.size(), state)
        self.assertEqual(placed.canvas, QSize(40, 80))
        self.assertEqual(placed.size, QSize(20, 40))
        result = editops.render(source, state)
        for x, y in ((10, 10), (70, 10), (10, 30), (70, 30), (40, 20)):
            mapped = placed.to_output.map(x + 0.5, y + 0.5)
            px, py = int(mapped[0]), int(mapped[1])
            if 0 <= px < result.width() and 0 <= py < result.height():
                self.assertEqual(rgb(result, px, py), rgb(source, x, y))

    def test_aspect_crop(self):
        x, y, w, h = editops.aspect_crop(400, 300, 1.0)
        self.assertAlmostEqual(w * 400, h * 300)
        self.assertAlmostEqual(h, 1.0)
        self.assertAlmostEqual(x, 0.125)
        x, y, w, h = editops.aspect_crop(400, 300, 16 / 9)
        self.assertAlmostEqual((w * 400) / (h * 300), 16 / 9)
        self.assertAlmostEqual(w, 1.0)
        x, y, w, h = editops.aspect_crop(400, 300, 1.0, (0.5, 0.5, 0.5, 0.25))
        self.assertAlmostEqual(w * 400, h * 300)
        self.assertTrue(0.5 <= x and x + w <= 1.0 and 0.5 <= y and y + h <= 0.75)

    def test_resize(self):
        resized = editops.resize(quadrants(), 20, 10)
        self.assertEqual(resized.size(), QSize(20, 10))
        self.assert_corners(resized, [RED, GREEN, BLUE, WHITE], 10)
        self.assertEqual(editops.resize(quadrants(), 0, -5).size(), QSize(1, 1))


class EditAdjustTest(ImageCase):
    def levels(self, **values) -> list[int]:
        """The grey ramp after an adjustment: the values at 0, 64, 128, 192 and 255."""
        result = editops.apply_adjustments(gradient(), editops.Adjustments(**values))
        return [rgb(result, x, 8)[0] for x in (0, 64, 128, 192, 255)]

    def test_identity_changes_nothing(self):
        source = gradient()
        self.assertEqual(editops.apply_adjustments(source, editops.Adjustments()), source)
        self.assertEqual(editops.render(source, editops.EditState()), source)
        self.assertTrue(editops.EditState().is_identity())

    def test_tone_adjustments_move_the_tones_they_name(self):
        self.assertEqual(self.levels(), [0, 64, 128, 192, 255])
        brighter, darker = self.levels(brightness=0.5), self.levels(brightness=-0.5)
        self.assertEqual((brighter[0], brighter[4], darker[0], darker[4]), (0, 255, 0, 255))     # black and white stay
        self.assertTrue(brighter[2] > 128 > darker[2])
        exposed = self.levels(exposure=0.5)
        self.assertEqual(exposed[0], 0)
        self.assertGreater(exposed[1], 64)
        self.assertEqual(exposed[4], 255)
        self.assertLess(self.levels(exposure=-0.5)[4], 255)
        more, less = self.levels(contrast=0.5), self.levels(contrast=-0.5)
        self.assertTrue(more[1] < 64 and more[3] > 192 and abs(more[2] - 128) <= 1)
        self.assertTrue(less[1] > 64 and less[3] < 192 and less[0] > 0 and less[4] < 255)
        highlights, shadows = self.levels(highlights=-1), self.levels(shadows=1)
        self.assertTrue(highlights[3] < 192 and abs(highlights[1] - 64) <= 3 and highlights[4] == 255)
        self.assertTrue(shadows[1] > 64 and abs(shadows[3] - 192) <= 3 and shadows[0] == 0)

    def test_every_tone_curve_only_rises(self):
        for name in ("brightness", "exposure", "contrast", "highlights", "shadows"):
            for value in (-1.0, -0.5, 0.5, 1.0):
                table = editops.tone_table(editops.Adjustments(**{name: value}))
                self.assertEqual(len(table), 256)
                self.assertTrue(all(a <= b for a, b in zip(table, table[1:])), (name, value))
        self.assertIsNone(editops.tone_table(editops.Adjustments(saturation=1, warmth=1)))

    def test_tables_are_what_the_pixels_get(self):
        source = gradient()
        adjust = editops.Adjustments(brightness=0.3, contrast=0.2, shadows=-0.4, warmth=0.5, tint=-0.3)
        result = editops.apply_adjustments(source, adjust)
        tone = editops.tone_table(adjust)
        red, green, blue = editops.balance_tables(adjust)
        for x in range(0, 256, 5):
            for y in (8, 48):
                r, g, b = rgb(source, x, y)
                self.assertEqual(rgb(result, x, y), (red[tone[r]], green[tone[g]], blue[tone[b]]))

    def test_saturation_is_exact_against_the_grey_picture(self):
        source = gradient()
        grey = source.convertToFormat(QImage.Format.Format_Grayscale8)
        for amount in (-1.0, -0.5, 0.25, 1.0):
            result = editops.apply_adjustments(source, editops.Adjustments(saturation=amount))
            for x in range(0, 256, 17):
                level = grey.pixelColor(x, 48).red()
                expected = tuple(max(0, min(255, c + (1 if amount > 0 else -1) * round(abs(c - level) * abs(amount))
                                             * (1 if c >= level else -1))) for c in rgb(source, x, 48))
                self.assertEqual(rgb(result, x, 48), expected, (amount, x))
                self.assertEqual(rgb(result, x, 8), rgb(source, x, 8))             # grey stays grey
        mono = editops.apply_adjustments(source, editops.Adjustments(saturation=-1))
        self.assertTrue(all(len(set(rgb(mono, x, 48))) == 1 for x in range(0, 256, 9)))

    def test_warmth_and_tint(self):
        source = QImage(8, 8, QImage.Format.Format_RGB32)
        source.fill(QColor(120, 120, 120))
        warm = rgb(editops.apply_adjustments(source, editops.Adjustments(warmth=1)), 4, 4)
        cool = rgb(editops.apply_adjustments(source, editops.Adjustments(warmth=-1)), 4, 4)
        magenta = rgb(editops.apply_adjustments(source, editops.Adjustments(tint=1)), 4, 4)
        self.assertTrue(warm[0] > 120 > warm[2] and warm[1] == 120)
        self.assertTrue(cool[0] < 120 < cool[2])
        self.assertTrue(magenta[1] < 120 < magenta[0])

    def test_sharpness_raises_edges_and_leaves_flat_areas(self):
        source = QImage(64, 64, QImage.Format.Format_RGB32)
        source.fill(QColor(100, 100, 100))
        painter = QPainter(source)
        painter.fillRect(32, 0, 32, 64, QColor(160, 160, 160))
        painter.end()
        result = editops.apply_adjustments(source, editops.Adjustments(sharpness=1))
        self.assertEqual(rgb(result, 5, 32), (100, 100, 100))
        self.assertEqual(rgb(result, 58, 32), (160, 160, 160))
        self.assertLess(rgb(result, 31, 32)[0], 100)                    # darker on the dark side of the edge
        self.assertGreater(rgb(result, 32, 32)[0], 160)                 # brighter on the bright side
        self.assertEqual(editops.blur_factor(800, 600), 2)
        self.assertEqual(editops.blur_factor(8000, 6000), 10)

    def test_vignette(self):
        source = QImage(200, 100, QImage.Format.Format_RGB32)
        source.fill(QColor(150, 150, 150))
        dark = editops.apply_adjustments(source, editops.Adjustments(vignette=1))
        light = editops.apply_adjustments(source, editops.Adjustments(vignette=-1))
        self.assertEqual(rgb(dark, 100, 50), (150, 150, 150))
        self.assertEqual(rgb(light, 100, 50), (150, 150, 150))
        self.assertLess(rgb(dark, 1, 1)[0], 60)
        self.assertGreater(rgb(light, 1, 1)[0], 220)
        self.assertEqual(rgb(dark, 1, 1), rgb(dark, 198, 98))           # the same in every corner
        self.assertTrue(rgb(dark, 100, 50)[0] >= rgb(dark, 50, 25)[0] >= rgb(dark, 20, 10)[0] >= rgb(dark, 1, 1)[0])

    def test_transparency_is_kept(self):
        source = QImage(16, 16, QImage.Format.Format_ARGB32)
        source.fill(QColor(200, 100, 50, 128))
        source.setPixelColor(0, 0, QColor(0, 0, 0, 0))
        adjust = editops.Adjustments(brightness=0.4, saturation=0.5, sharpness=0.5, vignette=0.3, warmth=0.2)
        result = editops.apply_adjustments(source, adjust)
        self.assertTrue(result.hasAlphaChannel())
        self.assertEqual(result.pixelColor(8, 8).alpha(), 128)
        self.assertEqual(result.pixelColor(0, 0).alpha(), 0)
        opaque = QImage(16, 16, QImage.Format.Format_RGB32)
        opaque.fill(QColor(200, 100, 50))
        self.assertTrue(near(rgb(result, 8, 8), rgb(editops.apply_adjustments(opaque, adjust), 8, 8), 3))

    def test_the_same_state_gives_the_same_pixels(self):
        source = gradient()
        state = editops.EditState().rotated().with_straighten(7.5).with_crop((0.1, 0.2, 0.6, 0.5)) \
            .with_adjust(exposure=0.2, contrast=0.3, saturation=0.4, sharpness=0.6, vignette=0.5, tint=0.2) \
            .with_filter("Fade", 0.7)
        first, second = editops.render(source, state), editops.render(source, state)
        self.assertEqual(first, second)
        self.assertNotEqual(first, editops.render(source, state.with_adjust(tint=0.3)))

    def test_bands_give_the_same_picture_as_one_piece(self):
        source = gradient(300, 240)
        adjust = editops.Adjustments(brightness=0.2, exposure=0.1, contrast=0.3, highlights=-0.2, shadows=0.4,
                                     saturation=0.3, warmth=0.2, tint=-0.1, sharpness=0.5, vignette=0.4)
        whole = editops.apply_adjustments(source, adjust)
        with mock.patch.object(editops, "BAND_PIXELS", 300 * 32):
            banded = editops.apply_adjustments(source, adjust)
        worst = max(abs(a - b) for y in range(0, 240, 3) for x in range(0, 300, 7)
                    for a, b in zip(rgb(whole, x, y), rgb(banded, x, y)))
        self.assertLessEqual(worst, 2)
        # without sharpening no band looks at its neighbours: the result is the same to the last bit
        with mock.patch.object(editops, "BAND_PIXELS", 10 ** 9):
            reference = editops.apply_adjustments(source, editops.Adjustments(brightness=0.2, saturation=0.3,
                                                                              warmth=0.2, vignette=0.4))
        with mock.patch.object(editops, "BAND_PIXELS", 300 * 32):
            self.assertEqual(editops.apply_adjustments(source, editops.Adjustments(
                brightness=0.2, saturation=0.3, warmth=0.2, vignette=0.4)), reference)

    def test_filters(self):
        source = gradient()
        self.assertEqual(list(editops.FILTERS)[:2], ["Vivid", "Bright"])
        state = editops.EditState().with_filter("Mono")
        mono = editops.render(source, state)
        self.assertTrue(all(len(set(rgb(mono, x, 48))) == 1 for x in range(0, 256, 9)))
        half = editops.render(source, state.with_filter("Mono", 0.5))
        self.assertEqual(half, editops.apply_adjustments(source, editops.Adjustments(saturation=-0.5)))
        self.assertEqual(editops.render(source, state.with_filter("Mono", 0.0)), source)
        self.assertEqual(editops.render(source, state.with_filter("")), source)
        with self.assertRaises(KeyError):
            state.with_filter("Nope")
        for name, look in editops.FILTERS.items():
            self.assertEqual(look, look.clamped(), name)
            self.assertFalse(look.is_identity(), name)
        self.assertEqual(editops.Adjustments(brightness=9, sharpness=-3, tint=float("nan")).clamped(),
                         editops.Adjustments(brightness=1.0))

    def test_speed_of_a_preview(self):
        source = gradient(64, 64).scaled(1280, 960)
        adjust = editops.Adjustments(0.2, 0.1, 0.3, -0.2, 0.4, 0.3, 0.2, -0.1, 0.5, 0.4)
        started = time.perf_counter()
        editops.apply_adjustments(source, adjust)
        self.assertLess(time.perf_counter() - started, 2.0)             # about 0.1 s on a desktop


class MarkupTest(ImageCase):
    def canvas(self) -> QImage:
        image = QImage(200, 100, QImage.Format.Format_RGB32)
        image.fill(QColor(*WHITE))
        return image

    def test_pen_and_highlighter(self):
        pen = editops.Stroke(editops.PEN, "#ff0000", 10, ((20, 50), (180, 50)))
        result = editops.render(self.canvas(), editops.EditState().with_stroke(pen))
        self.assertEqual(rgb(result, 100, 50), RED)
        self.assertEqual(rgb(result, 100, 53), RED)
        self.assertEqual(rgb(result, 100, 60), WHITE)
        self.assertEqual(rgb(result, 16, 50), RED)                      # round ends reach past the last point
        marker = editops.Stroke(editops.HIGHLIGHTER, "#ffff00", 20, ((20, 50), (180, 50), (20, 52)))
        result = editops.render(self.canvas(), editops.EditState().with_stroke(marker))
        r, g, b = rgb(result, 100, 50)
        self.assertTrue(r == 255 and g == 255 and 100 < b < 180)       # see-through, and not doubled where it overlaps
        self.assertEqual(rgb(result, 100, 20), WHITE)

    def test_strokes_follow_the_geometry_and_the_scale(self):
        pen = editops.Stroke(editops.PEN, "#0000ff", 8, ((10, 10), (10, 90)))        # a line down the left side
        state = editops.EditState().with_stroke(pen)
        turned = editops.render(self.canvas(), state.rotated())
        self.assertEqual(turned.size(), QSize(100, 200))
        self.assertEqual(rgb(turned, 50, 10), BLUE)                     # now a line along the top
        self.assertEqual(rgb(turned, 50, 100), WHITE)
        mirrored = editops.render(self.canvas(), state.flipped())
        self.assertEqual(rgb(mirrored, 189, 50), BLUE)
        cropped = editops.render(self.canvas(), state.with_crop((0.0, 0.0, 0.5, 1.0)))
        self.assertEqual(rgb(cropped, 10, 50), BLUE)
        # a preview half the size: the same stroke at half the size
        small = self.canvas().scaled(100, 50)
        preview = editops.render(small, state, 0.5)
        self.assertEqual(rgb(preview, 5, 25), BLUE)
        self.assertEqual(rgb(preview, 9, 25), WHITE)
        self.assertEqual(editops.render(self.canvas(), state, markup=False), self.canvas())

    def test_markup_is_drawn_over_the_adjustments(self):
        pen = editops.Stroke(editops.PEN, "#ff0000", 10, ((20, 50), (180, 50)))
        state = editops.EditState().with_filter("Mono").with_adjust(vignette=1.0).with_stroke(pen)
        self.assertEqual(rgb(editops.render(self.canvas(), state), 30, 50), RED)

    def test_eraser_hits(self):
        stroke = editops.Stroke(editops.PEN, "#000000", 4, ((0, 0), (100, 0)))
        self.assertTrue(editops.stroke_hit(stroke, 50, 5, 4))
        self.assertFalse(editops.stroke_hit(stroke, 50, 7, 4))
        self.assertTrue(editops.stroke_hit(stroke, 103, 0, 2))
        self.assertFalse(editops.stroke_hit(stroke, 110, 0, 2))
        dot = editops.Stroke(editops.PEN, "#000000", 4, ((5, 5),))
        self.assertTrue(editops.stroke_hit(dot, 6, 6, 1))
        state = editops.EditState().with_stroke(stroke).with_stroke(dot)
        self.assertEqual(state.without_strokes([0]).strokes, (dot,))
        self.assertEqual(editops.render(self.canvas(), state.without_strokes([0, 1])), self.canvas())
        odd = editops.Stroke(editops.PEN, "not a colour", 3, ((1, 1), (5, 5)))
        editops.render(self.canvas(), editops.EditState().with_stroke(odd))       # drawn black, not a crash


class SaveTest(ImageCase):
    def test_formats_round_trip(self):
        source = quadrants()
        self.assertTrue({"jpeg", "png", "bmp"} <= set(imageio.writable_formats()))
        for fmt in imageio.writable_formats():
            target = str(self.pics / f"out.{imageio.WRITERS[fmt][1]}")
            report = imageio.save(source, target, None, 95)
            self.assertEqual(report.size, os.path.getsize(target))
            loaded = decode.load(target)
            self.assertEqual(loaded.info.format, fmt)
            self.assert_corners(loaded.image, [RED, GREEN, BLUE, WHITE], 40 if fmt in ("jpeg", "webp") else 0)
        self.assertEqual(imageio.format_for_path("/x/Photo.JPEG"), "jpeg")
        self.assertIsNone(imageio.format_for_path("/x/photo.gif"))
        with self.assertRaises(imageio.SaveError):
            imageio.save(source, str(self.pics / "out.gif"))
        with self.assertRaises(imageio.SaveError):
            imageio.save(QImage(), str(self.pics / "null.png"))

    def test_jpeg_quality_changes_the_size(self):
        source = gradient(256, 256).scaled(512, 512, Qt.AspectRatioMode.IgnoreAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)
        small = len(imageio.encode(source, "jpeg", 20))
        large = len(imageio.encode(source, "jpeg", 95))
        self.assertLess(small, large)
        estimate = imageio.estimate_size(source, 512, 512, "jpeg", 95)
        self.assertEqual(estimate, large)
        self.assertGreater(imageio.estimate_size(source, 4000, 4000, "jpeg", 95), large)

    def test_transparency(self):
        source = quadrants(alpha=True)
        # A block, not one pixel: JPEG shares colour between neighbouring pixels (chroma
        # subsampling), so a single white pixel beside red comes back pink.
        for x in range(8):
            for y in range(8):
                source.setPixelColor(x, y, QColor(0, 0, 0, 0))
        report = imageio.save(source, str(self.pics / "t.jpg"), quality=100)
        self.assertIn("transparency (now white)", report.lost)
        self.assertTrue(near(rgb(decode.load(report.path).image, 0, 0), WHITE, 30))
        imageio.save(source, str(self.pics / "t.png"))
        self.assertEqual(decode.load(str(self.pics / "t.png")).image.pixelColor(0, 0).alpha(), 0)

    def test_jpeg_keeps_exif_with_the_orientation_set_upright(self):
        original = fx.jpeg_with_block(Path(self.save(quadrants(), "cam.jpg", 100)).read_bytes(),
                                      fx.camera_exif(6, thumbnail=b"\xff\xd8" + b"T" * 300 + b"\xff\xd9"))
        path = fx.write(self.pics / "cam.jpg", original)
        loaded = decode.load(str(path))
        self.assertEqual(loaded.image.size(), QSize(20, 40))
        edited = editops.render(loaded.image, editops.EditState().with_adjust(brightness=0.1))
        report = imageio.save(edited, str(self.pics / "cam (edited).jpg"), "jpeg", 100,
                              exif_block=imageio.read_exif_block(str(path)))
        self.assertEqual(report.kept, ("camera details, date and place (EXIF)", "colour profile (sRGB)"))
        self.assertEqual(report.lost, ())
        details = exif.read_file(report.path)
        self.assertEqual(details.orientation, 1)
        self.assertEqual((details.make, details.model, details.iso), ("Boswas", "Boswas Prism One", 200))
        self.assertEqual((details.pixel_width, details.pixel_height), (20, 40))
        self.assertAlmostEqual(details.latitude, 48.858367, places=4)
        again = decode.load(report.path)
        self.assertEqual(again.image.size(), QSize(20, 40))             # upright pixels, not turned twice
        self.assert_corners(again.image, [BLUE, RED, WHITE, GREEN], 60)
        self.assertNotIn(b"T" * 300, Path(report.path).read_bytes())
        png = imageio.save(edited, str(self.pics / "cam.png"), exif_block=imageio.read_exif_block(str(path)))
        self.assertIn("camera details, date and place (EXIF)", png.lost)
        self.assertIsNone(imageio.read_exif_block(png.path))
        self.assertIsNone(imageio.read_exif_block(str(self.pics / "missing.jpg")))

    def test_saving_is_atomic(self):
        target = self.pics / "keep.png"
        imageio.save(quadrants(), str(target))
        before = target.read_bytes()
        with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(imageio.SaveError) as caught:
                imageio.save(gradient(), str(target))
        self.assertIn("No space left", str(caught.exception))
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.pics)), ["keep.png"])
        with self.assertRaises(imageio.SaveError):
            imageio.save(quadrants(), str(self.pics / "no-such-folder" / "x.png"))

    def test_rotating_a_jpeg_on_disk_does_not_touch_its_pixels(self):
        plain = Path(self.save(quadrants(), "r.jpg", 90)).read_bytes()
        path = fx.write(self.pics / "r.jpg", fx.jpeg_with_block(plain, fx.camera_exif(1)))
        scan = plain[plain.index(b"\xff\xda"):]
        imageio.rotate_file(str(path))
        self.assertTrue(path.read_bytes().endswith(scan))
        self.assertEqual(exif.read_file(path).orientation, 6)
        self.assertEqual(exif.read_file(path).model, "Boswas Prism One")
        self.assert_corners(decode.load(str(path)).image, [BLUE, RED, WHITE, GREEN], 60)
        imageio.rotate_file(str(path), clockwise=False)
        self.assertEqual(exif.read_file(path).orientation, 1)
        for _ in range(4):
            imageio.rotate_file(str(path))
        self.assertEqual(exif.read_file(path).orientation, 1)
        bare = fx.write(self.pics / "bare.jpg", plain)
        imageio.rotate_file(str(bare))
        self.assertEqual(exif.read_file(bare).orientation, 6)
        self.assertEqual(decode.load(str(bare)).image.size(), QSize(20, 40))

    def test_rotating_lossless_formats_on_disk(self):
        deep = QImage(40, 20, QImage.Format.Format_RGBA64)
        deep.fill(QColor(255, 0, 0))
        deep.setText("Comment", "kept")
        cases = [("r.png", quadrants()), ("deep.png", deep), ("r.bmp", quadrants())]
        if "tiff" in imageio.writable_formats():
            cases.append(("r.tif", quadrants()))
        for name, image in cases:
            path = self.save(image, name)
            imageio.rotate_file(path)
            stored = decode.read_as_stored(path)
            self.assertEqual(stored.size(), QSize(20, 40), name)
            self.assertEqual(stored.depth(), image.depth(), name)
            if name == "deep.png":
                self.assertEqual(stored.text("Comment"), "kept")
            else:
                self.assert_corners(stored, [BLUE, RED, WHITE, GREEN], 0)
        for name, content in (("a.gif", fx.gif([RED, GREEN])), ("a.svg", fx.SVG.encode())):
            path = fx.write(self.pics / name, content)
            self.assertFalse(imageio.can_rotate_file(decode.probe(str(path))))
            with self.assertRaises(imageio.SaveError):
                imageio.rotate_file(str(path))
            self.assertEqual(path.read_bytes(), content)
        with self.assertRaises(imageio.SaveError):
            imageio.rotate_file(str(self.pics / "missing.png"))


class ThumbnailTest(ImageCase):
    def test_a_thumbnail_is_made_stored_and_found_again(self):
        source = quadrants(800, 400)
        path = self.save(source, "wide.png")
        mtime = os.stat(path).st_mtime
        self.assertIsNone(thumbs.cached(path, mtime))
        image = thumbs.thumbnail(path, mtime, os.path.getsize(path))
        self.assertEqual(image.size(), QSize(256, 128))
        self.assert_corners(image, [RED, GREEN, BLUE, WHITE], 10)
        stored = thumbcache.thumbnail_path(path)
        self.assertEqual(stored.parent, self.tmp / "xdg_cache_home" / "thumbnails" / "large")
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(stored.parent.stat().st_mode), 0o700)
        texts = thumbcache.read_texts(stored)
        self.assertEqual(texts["Thumb::URI"], thumbcache.file_uri(path))
        self.assertEqual(texts["Thumb::MTime"], str(int(mtime)))
        self.assertEqual(texts["Thumb::Size"], str(os.path.getsize(path)))
        self.assertEqual((texts["Thumb::Image::Width"], texts["Thumb::Image::Height"]), ("800", "400"))
        self.assertEqual(texts["Thumb::Mimetype"], "image/png")
        self.assertEqual(QImageReader(str(stored)).read().text("Thumb::URI"), thumbcache.file_uri(path))
        with mock.patch.object(decode, "load_scaled", side_effect=AssertionError("the cache must answer")):
            again = thumbs.thumbnail(path, mtime)
        self.assertEqual(again.size(), QSize(256, 128))
        self.assertIsNotNone(thumbs.cached(path, mtime))

    def test_a_changed_file_gets_a_new_thumbnail(self):
        path = self.save(quadrants(400, 400), "x.png")
        mtime = os.stat(path).st_mtime
        thumbs.thumbnail(path, mtime)
        red = QImage(400, 400, QImage.Format.Format_RGB32)
        red.fill(QColor(*RED))
        red.save(path)
        os.utime(path, (mtime + 10, mtime + 10))
        self.assertIsNone(thumbs.cached(path, mtime + 10))
        self.assertEqual(rgb(thumbs.thumbnail(path, mtime + 10), 200, 200), RED)

    def test_small_pictures_are_not_blown_up(self):
        path = self.save(quadrants(40, 20), "small.png")
        self.assertEqual(thumbs.thumbnail(path, os.stat(path).st_mtime).size(), QSize(40, 20))

    def test_failures_are_recorded_and_not_retried(self):
        path = str(fx.write(self.pics / "bad.jpg", b"\xff\xd8\xff\xe0 nothing"))
        mtime = os.stat(path).st_mtime
        with self.assertRaises(thumbs.ThumbnailError):
            thumbs.thumbnail(path, mtime)
        self.assertTrue(thumbcache.fail_path(path).is_file())
        self.assertTrue(thumbs.has_failed(path, mtime))
        self.assertFalse(thumbs.has_failed(path, mtime + 5))
        with mock.patch.object(decode, "load_scaled", side_effect=AssertionError("not tried again")):
            with self.assertRaises(thumbs.ThumbnailError):
                thumbs.thumbnail(path, mtime)
        with self.assertRaises(thumbs.ThumbnailError):
            thumbs.thumbnail(str(self.pics / "missing.png"), 1.0)
        self.assertFalse(thumbcache.fail_path(str(self.pics / "missing.png")).exists())

    def test_thumbnails_of_thumbnails_are_not_stored(self):
        path = self.save(quadrants(400, 400), "x.png")
        thumbs.thumbnail(path, os.stat(path).st_mtime)
        stored = str(thumbcache.thumbnail_path(path))
        before = set(os.listdir(os.path.dirname(stored)))
        thumbs.thumbnail(stored, os.stat(stored).st_mtime)
        self.assertEqual(set(os.listdir(os.path.dirname(stored))), before)

    def test_a_forged_cache_entry_is_not_believed(self):
        path = self.save(quadrants(400, 400), "x.png")
        mtime = os.stat(path).st_mtime
        stored = thumbcache.thumbnail_path(path)
        stored.parent.mkdir(parents=True)
        other = QImage(8, 8, QImage.Format.Format_RGB32)
        other.fill(QColor(*BLUE))
        other.setText("Thumb::URI", "file:///somewhere/else.png")
        other.setText("Thumb::MTime", str(int(mtime)))
        other.save(str(stored), "PNG")
        self.assertIsNone(thumbs.cached(path, mtime))
        stored.write_bytes(b"\x89PNG\r\n\x1a\n" + b"junk" * 10)
        self.assertIsNone(thumbs.cached(path, mtime))
        stored.write_bytes(fx.png_bomb(50000, 50000))
        self.assertIsNone(thumbs.cached(path, mtime))
        self.assertEqual(thumbs.thumbnail(path, mtime).size(), QSize(256, 256))

    def test_provider_serves_what_is_wanted_and_forgets_the_rest(self):
        paths = []
        for index in range(12):
            image = QImage(300, 300, QImage.Format.Format_RGB32)
            image.fill(QColor(index * 20, 0, 0))
            paths.append(self.save(image, f"p{index}.png"))
        items = [library.item_for(path) for path in paths]
        provider = thumbs.ThumbnailProvider(threads=2)
        self.addCleanup(provider.shutdown)
        ready: list[str] = []
        provider.ready.connect(ready.append)
        self.assertIsNone(provider.pixmap(items[0].path, items[0].mtime))
        provider.want("grid", [(item.path, item.mtime, item.size) for item in items])
        # the view scrolls on before most of them started: only the new set is wanted now
        provider.want("grid", [(item.path, item.mtime, item.size) for item in items[10:]])
        self.assertTrue(provider.wait(20))
        self.assertTrue({items[10].path, items[11].path} <= set(ready))
        self.assertLessEqual(len(ready), 4)                     # the two running when the view moved, and the new two
        pixmap = provider.pixmap(items[11].path, items[11].mtime)
        self.assertEqual((pixmap.width(), pixmap.height()), (256, 256))
        self.assertIsNone(provider.pixmap(items[11].path, items[11].mtime + 60))       # a changed file: not this one
        provider.invalidate(items[11].path)
        self.assertIsNone(provider.pixmap(items[11].path, items[11].mtime))

    def test_provider_reports_failures_once_and_bounds_its_memory(self):
        bad = str(fx.write(self.pics / "bad.png", b"nope"))
        provider = thumbs.ThumbnailProvider(threads=2, budget=600_000)
        self.addCleanup(provider.shutdown)
        failed: list[str] = []
        provider.failed.connect(failed.append)
        provider.want("a", [(bad, 1.0, 4)])
        self.assertTrue(provider.wait(10))
        self.assertEqual(failed, [bad])
        self.assertTrue(provider.has_failed(bad, 1.0))
        provider.want("a", [(bad, 1.0, 4)])
        self.assertEqual(provider.pending(), 0)
        items = []
        for index in range(6):
            image = QImage(300, 300, QImage.Format.Format_RGB32)
            image.fill(QColor(0, index * 30, 0))
            items.append(library.item_for(self.save(image, f"m{index}.png")))
        provider.want("b", [(item.path, item.mtime, item.size) for item in items])
        self.assertTrue(provider.wait(20))
        self.assertLessEqual(provider.memory_used(), 600_000)
        self.assertLess(provider.cached_count(), 6)


class WallpaperTest(ImageCase):
    def runner(self, code: int = 0):
        calls = []

        def run(arguments, **options):
            calls.append(arguments)
            import subprocess
            return subprocess.CompletedProcess(arguments, code, b"", b"no compositor\n" if code else b"")

        return run, calls

    def test_an_upright_png_is_copied_and_named_in_the_configuration(self):
        path = self.save(quadrants(400, 200), "Desk Top.png")
        loaded = decode.load(path)
        run, calls = self.runner()
        result = wallpaper.set_wallpaper(path, loaded.image, loaded.info, run)
        self.assertTrue(result.reloaded)
        self.assertEqual(calls, [["boswas-compositor", "--send", "reload-config"]])
        stored = Path(result.file)
        self.assertEqual(stored.parent, self.tmp / "xdg_data_home" / "boswas" / "wallpapers")
        self.assertEqual(stored.read_bytes(), Path(path).read_bytes())
        self.assertRegex(stored.name, r"^Desk-Top-[0-9a-f]{12}\.png$")
        config = self.tmp / "xdg_config_home" / "boswas" / "compositor.toml"
        self.assertEqual(config.read_text(), f'[appearance]\nwallpaper = "{stored}"\n')
        os.unlink(path)                                         # the original may go: the desktop has its own copy
        self.assertTrue(stored.is_file())

    def test_other_pictures_are_written_anew_upright(self):
        plain = Path(self.save(quadrants(), "p.jpg", 100)).read_bytes()
        turned = str(fx.write(self.pics / "turned.jpg", fx.jpeg_with_block(plain, fx.camera_exif(6))))
        loaded = decode.load(turned)
        run, _calls = self.runner(1)
        result = wallpaper.set_wallpaper(turned, loaded.image, loaded.info, run)
        self.assertFalse(result.reloaded)
        self.assertEqual(result.note, "no compositor")
        stored = decode.load(result.file)
        self.assertEqual(stored.info.orientation, 1)
        self.assertEqual(stored.image.size(), QSize(20, 40))
        self.assert_corners(stored.image, [BLUE, RED, WHITE, GREEN], 60)
        svg = str(fx.write(self.pics / "flag.svg", fx.SVG.encode()))
        loaded = decode.load(svg)
        data, extension = wallpaper.wallpaper_bytes(svg, loaded.image, loaded.info)
        self.assertEqual(extension, "png")
        self.assertTrue(data.startswith(b"\x89PNG"))
        with mock.patch.object(wallpaper, "MAX_SIDE", 100):
            big = self.save(quadrants(400, 200), "big.png")
            loaded = decode.load(big)
            data, extension = wallpaper.wallpaper_bytes(big, loaded.image, loaded.info)
            written = fx.write(self.pics / ("w." + extension), data)
            self.assertEqual(decode.load(str(written)).image.size(), QSize(100, 50))

    def test_a_configuration_that_cannot_be_edited_stops_it(self):
        fx.write(self.tmp / "xdg_config_home" / "boswas" / "compositor.toml", b'appearance = { scheme = "dark" }\n')
        path = self.save(quadrants(), "a.png")
        loaded = decode.load(path)
        run, calls = self.runner()
        with self.assertRaises(desktopcfg.ConfigError):
            wallpaper.set_wallpaper(path, loaded.image, loaded.info, run)
        self.assertEqual(calls, [])


@unittest.skipUnless(printing.AVAILABLE, "QtPrintSupport is not installed")
class PrintTest(ImageCase):
    def test_print_to_pdf(self):
        target = str(self.pics / "out.pdf")
        for mode in printing.MODES:
            printing.print_image(quadrants(400, 200), pdf_path=target, mode=mode, orientation=printing.AUTO)
            data = Path(target).read_bytes()
            self.assertTrue(data.startswith(b"%PDF-"))
            self.assertGreater(len(data), 500)
        with self.assertRaises(printing.PrintError):
            printing.print_image(QImage(), pdf_path=target)


if __name__ == "__main__":
    unittest.main()
