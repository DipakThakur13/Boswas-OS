"""Photos: the modules without Qt. No display, no home directory, no network."""

from __future__ import annotations

import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import photos_fixtures as fx  # noqa: E402

from boswas_apps.photos import (cli, desktopcfg, exif, fileinfo, fileutil, formats, library, natsort,  # noqa: E402
                                openwith, printing, settings, thumbcache, trash)


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="photos-test-")
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.tmp / "data"), "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"), "HOME": str(self.tmp / "home")})
        self._env.start()
        (self.tmp / "home").mkdir()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()


class NaturalSortTest(unittest.TestCase):
    def test_numbers_compare_by_value(self):
        names = ["IMG_10.jpg", "IMG_2.jpg", "img_1.jpg", "IMG_100.jpg", "IMG_02.jpg"]
        self.assertEqual(natsort.natural_sorted(names),
                         ["img_1.jpg", "IMG_2.jpg", "IMG_02.jpg", "IMG_10.jpg", "IMG_100.jpg"])

    def test_case_does_not_matter_and_ties_are_stable(self):
        self.assertEqual(natsort.natural_sorted(["b.png", "A.png", "a.png", "B.png"]),
                         ["A.png", "a.png", "B.png", "b.png"])

    def test_mixed_shapes_never_fail_to_compare(self):
        names = ["", "1", "a", "1a", "a1", "a1b2", "a1b10", "10", "9", "é2", "é10", "2024-01-02 (3)", "2024-01-02 (12)",
                 "9" * 300, "x" + "7" * 5000]
        ordered = natsort.natural_sorted(names)
        self.assertEqual(sorted(ordered), sorted(names))
        self.assertLess(ordered.index("9"), ordered.index("10"))
        self.assertLess(ordered.index("a1b2"), ordered.index("a1b10"))
        self.assertLess(ordered.index("2024-01-02 (3)"), ordered.index("2024-01-02 (12)"))

    def test_paths_sort_by_their_last_part(self):
        self.assertEqual(natsort.natural_sorted(["/z/a2.png", "/a/a10.png"]), ["/z/a2.png", "/a/a10.png"])


class FormatsTest(unittest.TestCase):
    def test_sniff_knows_the_usual_formats(self):
        cases = {
            b"\x89PNG\r\n\x1a\n" + b"\0" * 8: "png", b"\xff\xd8\xff\xe0": "jpeg", b"GIF89a....": "gif",
            b"RIFF\x10\0\0\0WEBPVP8 ": "webp", b"II*\x00\x08\0\0\0": "tiff", b"MM\x00*\0\0\0\x08": "tiff",
            b"BM" + b"\0" * 20: "bmp", b"\x00\x00\x01\x00\x01\x00": "ico",
            b"\0\0\0\x1cftypavif\0\0\0\0avifmif1": "avif", b"\0\0\0\x18ftypheic\0\0\0\0mif1heic": "heif",
            b"\0\0\0\x18ftypmif1\0\0\0\0mif1avif": "avif",
            b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>': "svg",
            b"\xef\xbb\xbf  <SVG></SVG>": "svg", b"\xff\x0a": "jxl",
        }
        for header, expected in cases.items():
            self.assertEqual(formats.sniff(header), expected, header)

    def test_sniff_says_nothing_about_other_content(self):
        for header in (b"", b"hello", b"<html><body>", b"\0\0\0\x18ftypisom\0\0\0\0isommp41", b"PK\x03\x04", b"B"):
            self.assertIsNone(formats.sniff(header), header)

    def test_extensions(self):
        self.assertEqual(formats.extension("/a/b.c/Photo.JPEG"), "jpeg")
        self.assertEqual(formats.extension("/a/.hidden"), "")
        self.assertEqual(formats.extension("noext"), "")
        self.assertEqual(formats.format_for_extension("JPG"), "jpeg")
        self.assertEqual(formats.mime_for_extension("tif"), "image/tiff")
        self.assertIsNone(formats.format_for_extension("txt"))


class ExifReadTest(unittest.TestCase):
    def test_camera_block(self):
        for endian in "<>":
            data = exif.parse_tiff(fx.camera_exif(6, endian=endian))
            self.assertEqual(data.orientation, 6)
            self.assertEqual((data.make, data.model), ("Boswas", "Boswas Prism One"))
            self.assertEqual((data.lens_make, data.lens_model), ("Boswas", "24-70mm F2.8"))
            self.assertEqual(data.exposure_time, Fraction(1, 250))
            self.assertEqual(data.f_number, Fraction(28, 10))
            self.assertEqual(data.iso, 200)
            self.assertEqual(data.focal_length, 50)
            self.assertEqual(data.focal_length_35mm, 75)
            self.assertEqual(data.exposure_bias, Fraction(-1, 3))
            self.assertEqual(data.flash, 0x19)
            self.assertEqual(data.date_taken, "2026:08:14 18:23:05")
            self.assertEqual(data.taken().year, 2026)
            self.assertAlmostEqual(data.latitude, 48 + 51 / 60 + 30.12 / 3600, places=6)
            self.assertAlmostEqual(data.longitude, 2 + 17 / 60 + 40.12 / 3600, places=6)
            self.assertEqual(data.altitude, 35.0)
            self.assertEqual((data.pixel_width, data.pixel_height), (4000, 3000))
            self.assertEqual(data.dpi(), (300.0, 300.0))

    def test_southern_and_western_coordinates_are_negative(self):
        gps = [(1, fx.ASCII, "S"), (2, fx.RATIONAL, [(33, 1), (51, 1), (0, 1)]), (3, fx.ASCII, "W"),
               (4, fx.RATIONAL, [(151, 1), (12, 1), (0, 1)]), (5, fx.BYTE, [1]), (6, fx.RATIONAL, (12, 1))]
        data = exif.parse_tiff(fx.build_exif([], None, gps))
        self.assertAlmostEqual(data.latitude, -33.85)
        self.assertAlmostEqual(data.longitude, -151.2)
        self.assertEqual(data.altitude, -12.0)
        self.assertEqual(exif.format_coordinates(data.latitude, data.longitude), "33.85000° S, 151.20000° W")

    def test_nonsense_values_are_not_known(self):
        main = [(0x0112, fx.SHORT, 12), (0x011A, fx.RATIONAL, (72, 0)), (0x011B, fx.RATIONAL, (72, 1))]
        photo = [(0x829A, fx.RATIONAL, (1, 0)), (0x829D, fx.RATIONAL, (0, 1)), (0x8827, fx.SHORT, 0),
                 (0x9003, fx.ASCII, "2026:13:45 99:99:99"), (0x920A, fx.RATIONAL, (4000000000, 1)),
                 (0x9209, fx.ASCII, "x")]
        gps = [(1, fx.ASCII, "N"), (2, fx.RATIONAL, [(97, 1), (0, 1), (0, 1)]), (3, fx.ASCII, "Q"),
               (4, fx.RATIONAL, [(10, 1), (75, 1), (0, 1)]), (6, fx.RATIONAL, (5, 0))]
        data = exif.parse_tiff(fx.build_exif(main, photo, gps))
        self.assertEqual(data.orientation, 1)
        for name in ("exposure_time", "f_number", "iso", "focal_length", "latitude", "longitude", "altitude", "flash"):
            self.assertIsNone(getattr(data, name), name)
        self.assertEqual(data.date_taken, "")
        self.assertIsNone(data.dpi())

    def test_text_is_tamed(self):
        nasty = b"Evil\x1b[31m\nCam" + b"A" * 5000
        data = exif.parse_tiff(fx.build_exif([(0x010F, fx.ASCII, nasty + b"\x00"), (0x0110, fx.ASCII, b"\xff\xfeM\x00")]))
        self.assertNotIn("\x1b", data.make)
        self.assertNotIn("\n", data.make)
        self.assertLessEqual(len(data.make), exif.MAX_TEXT)
        self.assertEqual(data.model, "ÿþM")

    def test_values_outside_the_block_are_ignored_not_fatal(self):
        block = bytearray(fx.camera_exif())
        tiff = exif._Tiff(bytes(block))
        entries, _slot, _next = tiff.directory(8)
        make = entries[exif.TAG_MAKE]
        struct.pack_into("<I", block, make.slot + 8, 0xFFFFFF00)          # the value now lies far outside
        struct.pack_into("<I", block, entries[exif.TAG_MODEL].slot + 4, 0x7FFFFFFF)   # a count of two gigabytes
        data = exif.parse_tiff(bytes(block))
        self.assertEqual(data.make, "")
        self.assertEqual(data.model, "")
        self.assertEqual(data.iso, 200)                                   # the rest is still read

    def test_directories_that_point_in_circles_end(self):
        block = bytearray(fx.build_exif([(0x0112, fx.SHORT, 3)], [(0x8827, fx.SHORT, 100)], []))
        tiff = exif._Tiff(bytes(block))
        entries, _slot, _next = tiff.directory(8)
        struct.pack_into("<I", block, entries[exif.TAG_EXIF_IFD].position, 8)      # Exif IFD -> IFD0
        struct.pack_into("<I", block, entries[exif.TAG_GPS_IFD].position, 8)
        data = exif.parse_tiff(bytes(block))
        self.assertEqual(data.orientation, 3)
        self.assertIsNone(data.iso)

    def test_damaged_structures_raise_exif_error_only(self):
        good = fx.camera_exif()
        bad = [b"", b"II", b"II*\x00", b"XX*\x00\x08\x00\x00\x00", b"II+\x00\x08\x00\x00\x00",
               b"II*\x00\xff\xff\xff\xff", b"II*\x00\x08\x00\x00\x00\xff\xff", good[:9], good[:20],
               b"II*\x00\x08\x00\x00\x00" + struct.pack("<H", 60000) + b"\x00" * 100]
        for data in bad:
            with self.assertRaises(exif.ExifError, msg=data[:16]):
                exif.parse_tiff(data)

    def test_every_truncation_and_every_flipped_byte_is_survived(self):
        good = fx.camera_exif(thumbnail=b"\xff\xd8thumb\xff\xd9")
        for cut in range(len(good)):
            try:
                exif.parse_tiff(good[:cut])
            except exif.ExifError:
                pass
        for position in range(len(good)):
            for value in (0x00, 0xFF, 0x7F):
                damaged = bytearray(good)
                damaged[position] = value
                try:
                    result = exif.parse_tiff(bytes(damaged))
                except exif.ExifError:
                    continue
                self.assertIn(result.orientation, range(1, 9))
                self.assertLessEqual(len(result.make), exif.MAX_TEXT)

    def test_jpeg_segments(self):
        block = fx.camera_exif(8)
        jpeg = b"\xff\xd8" + b"\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 9 + b"\xff\xdb\x00\x04ab" + b"\xff\xda\x00\x02rest"
        with_exif = fx.jpeg_with_block(jpeg, block)
        self.assertEqual(exif.jpeg_exif_block(with_exif), block)
        self.assertEqual(exif.parse_jpeg(with_exif).orientation, 8)
        self.assertEqual(exif.parse_jpeg(io.BytesIO(with_exif)).model, "Boswas Prism One")
        self.assertIsNone(exif.parse_jpeg(jpeg))
        # an EXIF segment after fill bytes and other segments is still found
        late = jpeg[:20] + b"\xff\xff\xff" + b"\xff\xe1" + struct.pack(">H", len(block) + 8) + b"Exif\x00\x00" + block + jpeg[20:]
        self.assertEqual(exif.parse_jpeg(late).orientation, 8)

    def test_hostile_jpegs_give_none(self):
        block = fx.camera_exif()
        cases = [b"", b"\xff", b"\xff\xd8", b"\xff\xd8\xff", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xe1\x00",
                 b"\xff\xd8\xff\xe1\x00\x00", b"\xff\xd8\xff\xe1\x00\x01", b"\xff\xd8\xff\xe1\xff\xffExif\x00\x00II",
                 b"\xff\xd8" + b"\xff" * 5000, b"\xff\xd8" + b"\xff\xe0\x00\x02" * 10000,
                 b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00" + block[:8], b"not a jpeg at all",
                 b"\xff\xd8\xff\xda\x00\x02" + b"\xff\xe1\x00\x40Exif\x00\x00" + block]
        for data in cases:
            self.assertIsNone(exif.parse_jpeg(data), data[:12])

    def test_read_file(self):
        with tempfile.TemporaryDirectory() as folder:
            jpeg = Path(folder) / "a.jpg"
            jpeg.write_bytes(fx.jpeg_with_block(b"\xff\xd8\xff\xda\x00\x02", fx.camera_exif(3)))
            tiff = Path(folder) / "a.tif"
            tiff.write_bytes(fx.camera_exif(5, endian=">"))
            other = Path(folder) / "a.png"
            other.write_bytes(b"\x89PNG\r\n\x1a\n")
            empty = Path(folder) / "empty.tif"
            empty.write_bytes(b"")
            short = Path(folder) / "short.tif"
            short.write_bytes(b"II*\x00")
            self.assertEqual(exif.read_file(jpeg).orientation, 3)
            self.assertEqual(exif.read_file(tiff).orientation, 5)
            for path in (other, empty, short, Path(folder) / "missing.jpg", Path(folder)):
                self.assertIsNone(exif.read_file(path), path)


class ExifWriteTest(unittest.TestCase):
    def test_orientation_algebra(self):
        self.assertEqual(exif.compose_orientation(1, 6), 6)
        self.assertEqual(exif.compose_orientation(6, 6), 3)
        self.assertEqual(exif.compose_orientation(6, 8), 1)
        self.assertEqual(exif.compose_orientation(3, 3), 1)
        self.assertEqual(exif.compose_orientation(2, 2), 1)
        for start in range(1, 9):
            value = start
            for _turn in range(4):
                value = exif.compose_orientation(value, exif.ROTATE_CW)
            self.assertEqual(value, start)
            self.assertEqual(exif.swaps_sides(exif.compose_orientation(start, 6)), not exif.swaps_sides(start))
        self.assertEqual({exif.compose_orientation(a, b) for a in range(1, 9) for b in range(1, 9)}, set(range(1, 9)))

    def test_with_orientation_changes_two_bytes(self):
        block = fx.camera_exif(1)
        changed = exif.with_orientation(block, 6)
        self.assertEqual(len(changed), len(block))
        self.assertEqual(sum(a != b for a, b in zip(block, changed)), 1)
        self.assertEqual(exif.parse_tiff(changed).orientation, 6)
        self.assertEqual(exif.parse_tiff(changed).model, "Boswas Prism One")
        with self.assertRaises(exif.ExifError):
            exif.with_orientation(block, 9)

    def test_with_orientation_adds_the_tag_when_missing(self):
        for endian in "<>":
            block = fx.build_exif([(0x010F, fx.ASCII, "Maker"), (0x0131, fx.ASCII, "Soft")],
                                  [(0x8827, fx.SHORT, 400)], endian=endian)
            changed = exif.with_orientation(block, 8)
            parsed = exif.parse_tiff(changed)
            self.assertEqual((parsed.orientation, parsed.make, parsed.software, parsed.iso), (8, "Maker", "Soft", 400))
            self.assertTrue(changed.startswith(block[:4]))

    def test_minimal_block_and_jpeg_orientation(self):
        self.assertEqual(exif.parse_tiff(exif.minimal_block(7)).orientation, 7)
        plain = b"\xff\xd8" + b"\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 9 + b"\xff\xdb\x00\x04ab" + b"\xff\xda\x00\x02rest\xff\xd9"
        turned = exif.jpeg_set_orientation(plain, 6)
        self.assertEqual(exif.parse_jpeg(turned).orientation, 6)
        self.assertEqual(turned[:20], plain[:20])                 # the JFIF segment stays first
        self.assertTrue(turned.endswith(plain[20:]))
        again = exif.jpeg_set_orientation(turned, 3)
        self.assertEqual(len(again), len(turned))
        self.assertEqual(exif.parse_jpeg(again).orientation, 3)
        self.assertEqual(again.count(b"Exif\x00\x00"), 1)
        rich = fx.jpeg_with_block(plain, fx.camera_exif(1))
        self.assertEqual(exif.parse_jpeg(exif.jpeg_set_orientation(rich, 8)).iso, 200)
        with self.assertRaises(ValueError):
            exif.jpeg_set_orientation(b"GIF89a", 6)

    def test_prepared_for_save(self):
        thumbnail = b"\xff\xd8" + b"T" * 500 + b"\xff\xd9"
        block = fx.camera_exif(6, thumbnail=thumbnail)
        prepared = exif.prepared_for_save(block, 1200, 800)
        parsed = exif.parse_tiff(prepared)
        self.assertEqual(parsed.orientation, 1)
        self.assertEqual((parsed.pixel_width, parsed.pixel_height), (1200, 800))
        self.assertEqual((parsed.make, parsed.iso, parsed.date_taken), ("Boswas", 200, "2026:08:14 18:23:05"))
        self.assertAlmostEqual(parsed.latitude, 48.858367, places=4)
        self.assertNotIn(thumbnail, prepared)
        self.assertLess(len(prepared), len(block) - 400)
        self.assertIsNone(exif.prepared_for_save(b"garbage", 1, 1))
        self.assertIsNone(exif.prepared_for_save(block + b"\x00" * 70000, 1, 1))

    def test_jpeg_with_exif_replaces_and_removes(self):
        plain = b"\xff\xd8" + b"\xff\xdb\x00\x04ab" + b"\xff\xda\x00\x02rest\xff\xd9"
        one = exif.jpeg_with_exif(plain, fx.camera_exif(2))
        two = exif.jpeg_with_exif(one, fx.camera_exif(4))
        self.assertEqual(two.count(b"Exif\x00\x00"), 1)
        self.assertEqual(exif.parse_jpeg(two).orientation, 4)
        self.assertEqual(exif.jpeg_with_exif(two, None), plain)
        with self.assertRaises(ValueError):
            exif.jpeg_with_exif(plain, b"\x00" * 70000)

    def test_text_for_people(self):
        self.assertEqual(exif.format_exposure(Fraction(1, 250)), "1/250 s")
        self.assertEqual(exif.format_exposure(Fraction(2, 1)), "2 s")
        self.assertEqual(exif.format_exposure(Fraction(5, 2)), "2.5 s")
        self.assertEqual(exif.format_exposure(None), "")
        self.assertEqual(exif.format_aperture(Fraction(28, 10)), "f/2.8")
        self.assertEqual(exif.format_aperture(Fraction(8, 1)), "f/8")
        self.assertEqual(exif.format_focal_length(Fraction(50, 1), 75), "50 mm (75 mm in 35 mm terms)")
        self.assertEqual(exif.format_focal_length(Fraction(45, 10)), "4.5 mm")
        self.assertEqual(exif.format_flash(0x19), "Fired, automatic")
        self.assertEqual(exif.format_flash(0x10), "Did not fire, switched off")
        self.assertEqual(exif.format_flash(0x20), "No flash function")
        self.assertEqual(exif.format_flash(None), "")
        self.assertEqual(exif.format_bias(Fraction(-1, 3)), "-0.3 EV")
        self.assertEqual(exif.format_coordinates(48.85837, 2.29448), "48.85837° N, 2.29448° E")
        self.assertEqual(exif.format_coordinates(None, 2.0), "")
        self.assertEqual(exif.format_altitude(-12.4), "12 m below sea level")


class TrashTest(TempCase):
    def test_file_goes_to_the_home_trash_with_a_record(self):
        victim = fx.write(self.tmp / "pics" / "a b%é.png", b"data")
        record = trash.trash(victim, now=time.mktime((2026, 3, 4, 5, 6, 7, 0, 0, -1)))
        home = self.tmp / "data" / "Trash"
        self.assertFalse(victim.exists())
        self.assertEqual(Path(record.trashed), home / "files" / "a b%é.png")
        self.assertEqual(Path(record.trashed).read_bytes(), b"data")
        info = (home / "info" / "a b%é.png.trashinfo").read_text("ascii")
        self.assertEqual(info.splitlines()[0], "[Trash Info]")
        self.assertIn(f"Path={str(self.tmp / 'pics')}/a%20b%25%C3%A9.png", info)
        self.assertIn("DeletionDate=2026-03-04T05:06:07", info)
        self.assertEqual(stat.S_IMODE(os.stat(home / "info").st_mode), 0o700)

    def test_same_names_do_not_overwrite(self):
        first = trash.trash(fx.write(self.tmp / "one" / "x.jpg", b"1"))
        second = trash.trash(fx.write(self.tmp / "two" / "x.jpg", b"2"))
        third = trash.trash(fx.write(self.tmp / "three" / "x.jpg", b"3"))
        names = {os.path.basename(record.trashed) for record in (first, second, third)}
        self.assertEqual(len(names), 3)
        self.assertEqual(Path(first.trashed).read_bytes(), b"1")
        self.assertEqual(Path(third.trashed).read_bytes(), b"3")
        for record in (first, second, third):
            self.assertEqual(os.path.basename(record.info), os.path.basename(record.trashed) + ".trashinfo")

    def test_a_file_without_record_in_the_trash_is_not_overwritten(self):
        stray = fx.write(self.tmp / "data" / "Trash" / "files" / "x.jpg", b"stray")
        record = trash.trash(fx.write(self.tmp / "x.jpg", b"new"))
        self.assertEqual(stray.read_bytes(), b"stray")
        self.assertNotEqual(record.trashed, str(stray))

    def test_restore(self):
        victim = fx.write(self.tmp / "pics" / "a.png", b"data")
        record = trash.trash(victim)
        self.assertEqual(trash.restore(record), str(victim))
        self.assertEqual(victim.read_bytes(), b"data")
        self.assertFalse(os.path.exists(record.info))
        self.assertFalse(os.path.exists(record.trashed))

    def test_restore_does_not_overwrite(self):
        victim = fx.write(self.tmp / "a.png", b"old")
        record = trash.trash(victim)
        victim.write_bytes(b"newer")
        with self.assertRaises(trash.TrashError):
            trash.restore(record)
        self.assertEqual(victim.read_bytes(), b"newer")
        self.assertTrue(os.path.exists(record.trashed))

    def test_a_link_is_trashed_not_its_target(self):
        target = fx.write(self.tmp / "target.png", b"keep")
        link = self.tmp / "link.png"
        link.symlink_to(target)
        record = trash.trash(link)
        self.assertTrue(os.path.islink(record.trashed))
        self.assertEqual(target.read_bytes(), b"keep")

    def test_errors_are_plain(self):
        with self.assertRaises(trash.TrashError) as caught:
            trash.trash(self.tmp / "missing.png")
        self.assertIn("no longer there", str(caught.exception))
        (self.tmp / "folder").mkdir()
        with self.assertRaises(trash.TrashError):
            trash.trash(self.tmp / "folder")
        self.assertTrue((self.tmp / "folder").is_dir())

    def test_another_file_system_uses_its_own_trash_or_a_copy(self):
        victim = fx.write(self.tmp / "usb" / "pics" / "a.png", b"data")
        top = self.tmp / "usb"
        real_lstat = os.lstat

        def fake_lstat(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if os.path.abspath(os.fspath(path)).startswith(str(top)):
                values = list(result)
                values[2] = result.st_dev + 1                    # st_dev: pretend it is another device
                return os.stat_result(values)
            return result

        with mock.patch("os.lstat", fake_lstat):
            self.assertEqual(trash.mount_point(str(victim)), str(top))
            with mock.patch("os.rename", side_effect=os.replace):
                record = trash.trash(victim)
        expected = top / f".Trash-{os.getuid()}"
        self.assertEqual(Path(record.trashed), expected / "files" / "a.png")
        info = (expected / "info" / "a.png.trashinfo").read_text()
        self.assertIn("Path=pics/a.png", info)                   # relative to the top of that file system

    def test_shared_trash_needs_the_sticky_bit(self):
        top = self.tmp / "usb"
        (top / ".Trash").mkdir(parents=True)
        self.assertEqual(trash._top_trash(str(top)), top / f".Trash-{os.getuid()}")
        os.chmod(top / ".Trash", 0o1777)
        self.assertEqual(trash._top_trash(str(top)), top / ".Trash" / str(os.getuid()))


class ThumbCacheTest(TempCase):
    def test_uri_and_name_follow_the_standard(self):
        # the example of the Thumbnail Managing Standard
        self.assertEqual(thumbcache.thumbnail_name("file:///home/jens/photos/me.png"),
                         "c6ee772d9e49320e97ec29a7eb5b1697.png")
        self.assertEqual(thumbcache.file_uri("/home/jens/photos/me.png"), "file:///home/jens/photos/me.png")
        self.assertEqual(thumbcache.file_uri("/a b/é#?%.png"), "file:///a%20b/%C3%A9%23%3F%25.png")
        self.assertEqual(thumbcache.file_uri("/a/(1)+x,y=z@w:v.png"), "file:///a/(1)+x,y=z@w:v.png")

    def test_paths(self):
        root = self.tmp / "cache" / "thumbnails"
        self.assertEqual(thumbcache.cache_root(), root)
        self.assertEqual(thumbcache.thumbnail_path("/home/jens/photos/me.png"),
                         root / "large" / "c6ee772d9e49320e97ec29a7eb5b1697.png")
        self.assertEqual(thumbcache.fail_path("/home/jens/photos/me.png"),
                         root / "fail" / "boswas-photos-1" / "c6ee772d9e49320e97ec29a7eb5b1697.png")
        self.assertEqual([thumbcache.size_name(n) for n in (1, 128, 129, 256, 300, 512, 1024, 5000)],
                         ["normal", "normal", "large", "large", "x-large", "x-large", "xx-large", "xx-large"])
        with self.assertRaises(ValueError):
            thumbcache.thumbnail_path("/x.png", "huge")
        self.assertTrue(thumbcache.is_inside_cache(root / "large" / "x.png"))
        self.assertFalse(thumbcache.is_inside_cache(str(root) + "-other/x.png"))

    def test_reading_the_text_of_a_thumbnail(self):
        def text(keyword: bytes, value: bytes) -> bytes:
            return fx.png_chunk(b"tEXt", keyword + b"\x00" + value)
        head = b"\x89PNG\r\n\x1a\n" + fx.png_chunk(b"IHDR", b"\x00" * 13)
        good = (head + text(b"Thumb::URI", b"file:///a%20b.png") + fx.png_chunk(b"pHYs", b"\x00" * 9)
                + text(b"Thumb::MTime", b"1700000000") + text(b"Thumb::URI", b"file:///second") + text(b"", b"x")
                + fx.png_chunk(b"IDAT", b"data") + text(b"After", b"ignored") + fx.png_chunk(b"IEND", b""))
        path = fx.write(self.tmp / "t.png", good)
        self.assertEqual(thumbcache.read_texts(path), {"Thumb::URI": "file:///a%20b.png", "Thumb::MTime": "1700000000"})
        import zlib
        packed = (head + fx.png_chunk(b"zTXt", b"Thumb::URI\x00\x00" + zlib.compress(b"file:///long/" + b"a" * 100))
                  + fx.png_chunk(b"iTXt", b"Thumb::MTime\x00\x00\x00en\x00\x00" + b"42")
                  + fx.png_chunk(b"iTXt", b"Packed\x00\x01\x00\x00\x00" + zlib.compress("é".encode()))
                  + fx.png_chunk(b"zTXt", b"Bomb\x00\x00" + zlib.compress(b"\x00" * 5_000_000))
                  + fx.png_chunk(b"zTXt", b"Broken\x00\x00garbage") + fx.png_chunk(b"zTXt", b"Method\x00\x07x")
                  + fx.png_chunk(b"IEND", b""))
        self.assertEqual(thumbcache.read_texts(fx.write(self.tmp / "packed.png", packed)),
                         {"Thumb::URI": "file:///long/" + "a" * 100, "Thumb::MTime": "42", "Packed": "é"})
        wrong_sum = bytearray(head + text(b"Thumb::URI", b"file:///x") + fx.png_chunk(b"IEND", b""))
        wrong_sum[-14] ^= 0xFF
        huge = head + b"\xff\xff\xff\xfftEXt" + b"x" * 50
        endless = head + fx.png_chunk(b"tIME", b"1234567") * 500
        for name, data in (("empty", b""), ("notpng", b"GIF89a" + good), ("cut", good[:60]), ("sum", bytes(wrong_sum)),
                           ("huge", huge), ("endless", endless), ("noend", head + text(b"Thumb::URI", b"file:///x"))):
            self.assertEqual(thumbcache.read_texts(fx.write(self.tmp / name, data)), {}, name)
        self.assertEqual(thumbcache.read_texts(self.tmp / "missing.png"), {})

    def test_matching(self):
        texts = thumbcache.texts_for("file:///x.png", 1700000000.9, 1234, 640, 480, "image/png")
        self.assertEqual(texts["Thumb::MTime"], "1700000000")
        self.assertEqual(texts["Thumb::Image::Width"], "640")
        self.assertTrue(thumbcache.matches(texts, "file:///x.png", 1700000000.2))
        self.assertFalse(thumbcache.matches(texts, "file:///x.png", 1700000001))
        self.assertFalse(thumbcache.matches(texts, "file:///y.png", 1700000000))
        self.assertFalse(thumbcache.matches({"Thumb::URI": "file:///x.png", "Thumb::MTime": "soon"}, "file:///x.png", 1))
        self.assertFalse(thumbcache.matches({}, "file:///x.png", 1))


class DesktopConfigTest(TempCase):
    def check(self, before: str, expected: str, value: str = "/pics/a.png") -> None:
        after = desktopcfg.set_key(before, "appearance", "wallpaper", value)
        self.assertEqual(after, expected)
        self.assertEqual(tomllib.loads(after)["appearance"]["wallpaper"], value)

    def test_new_file_and_new_table(self):
        self.check("", '[appearance]\nwallpaper = "/pics/a.png"\n')
        self.check("[input]\nx = 1", '[input]\nx = 1\n\n[appearance]\nwallpaper = "/pics/a.png"\n')
        self.check("# mine\n[input]\nx = 1\n", '# mine\n[input]\nx = 1\n\n[appearance]\nwallpaper = "/pics/a.png"\n')

    def test_existing_table_gets_the_key_after_its_last_line(self):
        self.check('# look\n[appearance]\nscheme = "dark"  # keep\n\n# input next\n[input]\nx = 1\n',
                   '# look\n[appearance]\nscheme = "dark"  # keep\nwallpaper = "/pics/a.png"\n\n# input next\n[input]\nx = 1\n')
        self.check("[appearance]", '[appearance]\nwallpaper = "/pics/a.png"\n')
        self.check("[appearance]\n[appearance.more]\na = 1\n",
                   '[appearance]\nwallpaper = "/pics/a.png"\n[appearance.more]\na = 1\n')

    def test_existing_key_is_replaced_in_place(self):
        self.check('[appearance]\nwallpaper = "/old.png" # mine\nscheme = "light"\n',
                   '[appearance]\nwallpaper = "/pics/a.png" # mine\nscheme = "light"\n')
        self.check("[ appearance ]\n  'wallpaper'   =   '''\n/old\n'''   # c\nx = 1\n",
                   '[ appearance ]\n  \'wallpaper\'   =   "/pics/a.png"   # c\nx = 1\n')
        self.check('appearance.wallpaper = "/old"\n[x]\ny = 2\n', 'appearance.wallpaper = "/pics/a.png"\n[x]\ny = 2\n')
        self.check('appearance.scheme = "dark"\n[x]\ny = 2\n',
                   'appearance.scheme = "dark"\nappearance.wallpaper = "/pics/a.png"\n[x]\ny = 2\n')

    def test_look_alikes_are_not_touched(self):
        text = ('[other]\nwallpaper = "no"\ns = """\n[appearance]\nwallpaper = "trap"\n"""\n'
                'a = [\n  "[appearance]", # c\n  "wallpaper = 1",\n]\n[[appearance_list]]\nwallpaper = "no"\n')
        after = desktopcfg.set_key(text, "appearance", "wallpaper", "/p.png")
        self.assertTrue(after.startswith(text))
        self.assertEqual(after[len(text):], '\n[appearance]\nwallpaper = "/p.png"\n')
        parsed = tomllib.loads(after)
        self.assertEqual(parsed["other"]["wallpaper"], "no")
        self.assertEqual(parsed["appearance_list"][0]["wallpaper"], "no")

    def test_windows_line_ends_are_kept(self):
        self.check('[appearance]\r\nscheme = "dark"\r\n', '[appearance]\r\nscheme = "dark"\r\nwallpaper = "/pics/a.png"\r\n')

    def test_awkward_names_are_escaped(self):
        value = '/pics/a "quoted" \\ back\ttab é 日本.png'
        after = desktopcfg.set_key("", "appearance", "wallpaper", value)
        self.assertEqual(tomllib.loads(after)["appearance"]["wallpaper"], value)
        self.assertNotIn("\t", after)

    def test_what_cannot_be_edited_safely_is_refused(self):
        for text in ('appearance = { scheme = "dark" }\n', "[appearance\n", 'appearance = "x"\n', "x = = 1\n"):
            with self.assertRaises(desktopcfg.ConfigError, msg=text):
                desktopcfg.set_key(text, "appearance", "wallpaper", "/p.png")

    def test_everything_else_survives_many_shapes(self):
        samples = [
            '[appearance]\nscheme="dark"\nreduced_motion=false\n[keys]\n"a b" = {x=1,y=[1,2]}\n',
            'title = "t"\n\n[a]\nb = 1979-05-27T07:32:00Z\nc = 1e3\n\n[a.z]\nd = [ [1, 2], ["x"] ]\n',
            "# only comments\n\n# more\n",
            "[appearance]\n# nothing yet\n\n\n[z]\n",
            "x = '''\nraw [appearance]\n'''\n[appearance] # trailing\nwallpaper = 3 # a number\n",
        ]
        for text in samples:
            before = tomllib.loads(text)
            after_text = desktopcfg.set_key(text, "appearance", "wallpaper", "/w.jpg")
            after = tomllib.loads(after_text)
            self.assertEqual(after["appearance"].pop("wallpaper"), "/w.jpg")
            before.get("appearance", {}).pop("wallpaper", None)
            if not after["appearance"] and "appearance" not in before:
                del after["appearance"]
            self.assertEqual(after, before)
            # every line of the original that does not set the key is still there, in order
            kept = [line for line in text.splitlines() if "wallpaper" not in line or line.startswith("raw")]
            position = 0
            for line in kept:
                position = after_text.index(line, position) + len(line)

    def test_set_wallpaper_writes_the_file_atomically(self):
        config = self.tmp / "config" / "boswas" / "compositor.toml"
        self.assertEqual(desktopcfg.config_path(), config)
        self.assertEqual(desktopcfg.current_wallpaper(), "")
        desktopcfg.set_wallpaper("/pics/one.png")
        self.assertEqual(config.read_text(), '[appearance]\nwallpaper = "/pics/one.png"\n')
        config.write_text('# hand-made\n[appearance]\nscheme = "light"\nwallpaper = "/pics/one.png"\n[input]\nnatural = true\n')
        os.chmod(config, 0o640)
        desktopcfg.set_wallpaper("/pics/two.jpg")
        self.assertEqual(config.read_text(),
                         '# hand-made\n[appearance]\nscheme = "light"\nwallpaper = "/pics/two.jpg"\n[input]\nnatural = true\n')
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o640)
        self.assertEqual(desktopcfg.current_wallpaper(), "/pics/two.jpg")
        self.assertEqual([p.name for p in config.parent.iterdir()], ["compositor.toml"])     # no temporary file left
        with self.assertRaises(desktopcfg.ConfigError):
            desktopcfg.set_wallpaper("relative.png")

    def test_a_broken_file_is_left_alone(self):
        config = fx.write(self.tmp / "config" / "boswas" / "compositor.toml", b"[appearance\nbroken")
        with self.assertRaises(desktopcfg.ConfigError):
            desktopcfg.set_wallpaper("/pics/one.png")
        self.assertEqual(config.read_bytes(), b"[appearance\nbroken")
        config.write_bytes(b"\xff\xfe not utf-8")
        with self.assertRaises(desktopcfg.ConfigError):
            desktopcfg.set_wallpaper("/pics/one.png")
        config.write_bytes(b"#" * (desktopcfg.MAX_CONFIG_BYTES + 1))
        with self.assertRaises(desktopcfg.ConfigError):
            desktopcfg.set_wallpaper("/pics/one.png")

    def test_a_link_to_the_file_stays_a_link(self):
        real = fx.write(self.tmp / "dotfiles" / "compositor.toml", b"[appearance]\n")
        link = self.tmp / "config" / "boswas" / "compositor.toml"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        desktopcfg.set_wallpaper("/pics/one.png")
        self.assertTrue(link.is_symlink())
        self.assertIn("/pics/one.png", real.read_text())

    def test_wallpaper_store(self):
        name = desktopcfg.wallpaper_name("My Holiday (1) ünï.JPEG", b"abc", "jpg")
        self.assertRegex(name, r"^My-Holiday-1-n-[0-9a-f]{12}\.jpg$")
        self.assertRegex(desktopcfg.wallpaper_name("///...", b"abc", ".PNG"), r"^wallpaper-[0-9a-f]{12}\.png$")
        stored = desktopcfg.store_wallpaper("a.png", b"content", "png")
        self.assertEqual(stored.parent, self.tmp / "data" / "boswas" / "wallpapers")
        self.assertEqual(stored.read_bytes(), b"content")
        self.assertEqual(desktopcfg.store_wallpaper("a.png", b"content", "png"), stored)
        self.assertNotEqual(desktopcfg.store_wallpaper("a.png", b"other", "png"), stored)
        with self.assertRaises(desktopcfg.ConfigError):
            desktopcfg.store_wallpaper("a.webp", b"x", "webp")

    def test_reload_runs_a_fixed_command_without_a_shell(self):
        calls = []

        def run(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 0, b"{}", b"")

        self.assertEqual(desktopcfg.reload_compositor(run), (True, ""))
        arguments, options = calls[0]
        self.assertEqual(arguments, ["boswas-compositor", "--send", "reload-config"])
        self.assertNotIn("shell", options)
        self.assertLessEqual(options["timeout"], 10)

        def failing(arguments, **options):
            return subprocess.CompletedProcess(arguments, 1, b"", b"boswas-compositor: no compositor is running\n")

        self.assertEqual(desktopcfg.reload_compositor(failing), (False, "boswas-compositor: no compositor is running"))

        def missing(arguments, **options):
            raise FileNotFoundError(arguments[0])

        self.assertFalse(desktopcfg.reload_compositor(missing)[0])

        def slow(arguments, **options):
            raise subprocess.TimeoutExpired(arguments, 5)

        self.assertEqual(desktopcfg.reload_compositor(slow), (False, "the desktop did not answer"))


class LibraryTest(TempCase):
    EXT = ("png", "jpg", "gif")

    def make(self, relative: str, mtime: float | None = None) -> str:
        path = fx.write(self.tmp / "lib" / relative, b"x")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return str(path)

    def test_scan_finds_pictures_and_skips_the_rest(self):
        wanted = {self.make("a.png"), self.make("sub/b.JPG"), self.make("sub/deeper/c.gif")}
        self.make("notes.txt")
        self.make(".hidden.png")
        self.make(".cache/d.png")
        self.make("noext")
        (self.tmp / "lib" / "loop").symlink_to(self.tmp / "lib")
        (self.tmp / "lib" / "dir.png").mkdir()
        found = library.scan([str(self.tmp / "lib")], self.EXT)
        self.assertEqual({item.path for item in found}, wanted)
        self.assertTrue(all(item.size == 1 for item in found))

    def test_scan_roots_cancel_limit_skip(self):
        for index in range(30):
            self.make(f"many/{index}.png")
        self.make("other/x.png")
        root = str(self.tmp / "lib")
        self.assertEqual(len(library.scan([root, root + "/many", root], self.EXT)), 31)
        self.assertEqual(len(library.scan([root], self.EXT, limit=7)), 7)
        self.assertEqual(library.scan([root], self.EXT, cancelled=lambda: True), [])
        self.assertEqual(len(library.scan([root], self.EXT, skip=[root + "/many"])), 1)
        self.assertEqual(library.scan([root + "/missing", ""], self.EXT), [])
        self.assertEqual(library.distinct_roots([root + "/many", root, root + "/other/"]), [root])

    def test_order_months_search_folders(self):
        def stamp(year, month, day):
            return time.mktime((year, month, day, 12, 0, 0, 0, 0, -1))
        self.make("b/IMG_10.png", stamp(2026, 3, 5))
        self.make("b/IMG_2.png", stamp(2026, 3, 5))
        self.make("a/old.jpg", stamp(2024, 12, 31))
        self.make("a/new year.jpg", stamp(2025, 1, 1))
        self.make("a/Summer.png", stamp(2026, 3, 1))
        items = library.newest_first(library.scan([str(self.tmp / "lib")], self.EXT))
        self.assertEqual([item.name for item in items], ["IMG_2.png", "IMG_10.png", "Summer.png", "new year.jpg", "old.jpg"])
        sections = library.months(items)
        self.assertEqual([(s.title, s.start, s.count) for s in sections],
                         [("March 2026", 0, 3), ("January 2025", 3, 1), ("December 2024", 4, 1)])
        self.assertEqual([item.name for item in library.search(items, "img")], ["IMG_2.png", "IMG_10.png"])
        self.assertEqual([item.name for item in library.search(items, " YEAR new ")], ["new year.jpg"])
        self.assertEqual(library.search(items, ""), items)
        self.assertEqual(library.search(items, "nothing"), [])
        folders = library.folders(items)
        self.assertEqual([(f.name, f.count, f.cover.name) for f in folders], [("a", 3, "Summer.png"), ("b", 2, "IMG_2.png")])
        self.assertEqual(len(library.in_folder(items, str(self.tmp / "lib" / "a"))), 3)
        self.assertEqual(library.months([]), [])
        odd = [library.Item("/x.png", 1e300, 1), library.Item("/y.png", float("nan"), 1)]
        self.assertEqual({section.title for section in library.months(odd)}, {"Unknown date"})

    def test_folder_items_are_in_natural_order_and_include_the_opened_file(self):
        self.make("f/IMG_10.png")
        self.make("f/img_9.png")
        self.make("f/.secret.png")
        self.make("f/readme.txt")
        odd = self.make("f/picture.bin")
        folder = str(self.tmp / "lib" / "f")
        self.assertEqual([i.name for i in library.folder_items(folder, self.EXT)], ["img_9.png", "IMG_10.png"])
        self.assertEqual([i.name for i in library.folder_items(folder, self.EXT, odd)],
                         ["img_9.png", "IMG_10.png", "picture.bin"])
        with self.assertRaises(OSError):
            library.folder_items(folder + "/missing", self.EXT)

    def test_pictures_dir(self):
        home = str(self.tmp / "home")
        self.assertEqual(library.pictures_dir(home), home + "/Pictures")
        fx.write(self.tmp / "config" / "user-dirs.dirs",
                 b'# comment\nXDG_MUSIC_DIR="$HOME/Musik"\nXDG_PICTURES_DIR="$HOME/Bilder \\"neu\\""\n')
        self.assertEqual(library.pictures_dir(home), home + '/Bilder "neu"')
        fx.write(self.tmp / "config" / "user-dirs.dirs", b'XDG_PICTURES_DIR="/srv/pics/"\n')
        self.assertEqual(library.pictures_dir(home), "/srv/pics")
        fx.write(self.tmp / "config" / "user-dirs.dirs", b'XDG_PICTURES_DIR="$HOME/"\n')      # "none": the home itself
        self.assertEqual(library.pictures_dir(home), home + "/Pictures")
        fx.write(self.tmp / "config" / "user-dirs.dirs", b'XDG_PICTURES_DIR=relative\n')
        self.assertEqual(library.pictures_dir(home), home + "/Pictures")

    def test_fifty_thousand_items_are_quick(self):
        base = time.time()
        items = [library.Item(f"/pics/{i % 400}/IMG_{i}.jpg", base - i * 1800, 1000) for i in range(50000)]
        started = time.perf_counter()
        ordered = library.newest_first(items)
        sections = library.months(ordered)
        found = library.search(ordered, "img_49")
        folders = library.folders(ordered)
        elapsed = time.perf_counter() - started
        self.assertEqual(sum(section.count for section in sections), 50000)
        self.assertEqual(len(folders), 400)
        self.assertEqual(len(found), 1111)
        self.assertLess(elapsed, 5.0)


class SettingsTest(TempCase):
    def test_defaults_and_round_trip(self):
        path = self.tmp / "config" / "app" / "settings.json"
        loaded = settings.load(path)
        self.assertEqual(loaded, settings.Settings())
        loaded.folders = ["/a/b", "/c"]
        loaded.wheel = "navigate"
        loaded.slideshow_interval = 9
        loaded.confirm_delete = False
        settings.save(loaded, path)
        self.assertEqual(settings.load(path), loaded)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(loaded.gallery_roots("/pics"), ["/pics", "/a/b", "/c"])
        loaded.include_pictures = False
        self.assertEqual(loaded.gallery_roots("/pics"), ["/a/b", "/c"])

    def test_bad_values_fall_back(self):
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({
            "folders": ["/ok", "relative", 5, "/ok", "/with\u0000nul", "/x" * 5000, "/b/../c"], "wheel": "fly",
            "slideshow_interval": 100000, "jpeg_quality": -4, "confirm_delete": "yes", "slideshow_loop": 0,
            "new_window": True, "unknown": {"nested": 1}}))
        loaded = settings.load(path)
        self.assertEqual(loaded.folders, ["/ok", "/c"])
        self.assertEqual(loaded.wheel, "zoom")
        self.assertEqual(loaded.slideshow_interval, settings.INTERVAL_RANGE[1])
        self.assertEqual(loaded.jpeg_quality, settings.QUALITY_RANGE[0])
        self.assertTrue(loaded.confirm_delete)
        self.assertTrue(loaded.slideshow_loop)
        self.assertTrue(loaded.new_window)

    def test_unreadable_files_give_defaults(self):
        path = self.tmp / "settings.json"
        for content in (b"", b"{", b"[1, 2]", b'"text"', b"\xff\xfe", b"[" * 100000, b" " * (settings.MAX_BYTES + 1)):
            path.write_bytes(content)
            self.assertEqual(settings.load(path), settings.Settings(), content[:10])
        self.assertEqual(settings.load(self.tmp / "missing.json"), settings.Settings())


class OpenWithTest(TempCase):
    def entry(self, name: str, body: str, folder: str = "data") -> None:
        fx.write(self.tmp / folder / "applications" / name, ("[Desktop Entry]\n" + body).encode())

    def test_exec_becomes_an_argument_list(self):
        app = openwith.Application("x.desktop", "X Viewer", "x-icon", "", "/usr/share/applications/x.desktop")

        def command(exec_line, files=("/p/a b.png",)):
            with mock.patch("shutil.which", lambda program: "/usr/bin/" + program):
                with mock.patch("os.access", lambda path, mode: True):
                    return openwith.command(openwith.Application(app.entry_id, app.name, app.icon, exec_line, app.path),
                                            list(files))

        self.assertEqual(command("viewer %f"), ["/usr/bin/viewer", "/p/a b.png"])
        self.assertEqual(command("viewer --x %F", ("/1.png", "/2.png")), ["/usr/bin/viewer", "--x", "/1.png", "/2.png"])
        self.assertEqual(command('"/opt/my viewer/run" --title "%c" %U %i'),
                         ["/opt/my viewer/run", "--title", "X Viewer", "/p/a b.png", "--icon", "x-icon"])
        self.assertEqual(command('viewer "a \\"b\\" \\$HOME \\\\" %u'), ["/usr/bin/viewer", 'a "b" $HOME \\', "/p/a b.png"])
        self.assertEqual(command("viewer --file=%f 100%% %k"),
                         ["/usr/bin/viewer", "--file=/p/a b.png", "100%", "/usr/share/applications/x.desktop"])
        self.assertEqual(command("env A=1 viewer %d %n %f"), ["/usr/bin/env", "A=1", "viewer", "/p/a b.png"])
        # nothing a shell would do happens: these stay plain text in one argument
        self.assertEqual(command("viewer %f ; rm -rf $HOME `id` | x"),
                         ["/usr/bin/viewer", "/p/a b.png", ";", "rm", "-rf", "$HOME", "`id`", "|", "x"])
        for bad in ("viewer", "", 'viewer "unterminated %f', "viewer %z %f", "viewer --x=%F"):
            self.assertIsNone(command(bad), bad)
        with mock.patch("shutil.which", lambda program: None):
            self.assertIsNone(openwith.command(openwith.Application("x", "X", "", "missing %f", ""), ["/a.png"]))

    def test_applications_for_a_type(self):
        program = self.tmp / "bin" / "viewer"
        fx.write(program, b"#!/bin/sh\n")
        os.chmod(program, 0o755)
        self.entry("b-viewer.desktop", f"Type=Application\nName=Beta Viewer\nExec={program} %f\nMimeType=image/png;image/jpeg;\nIcon=beta\n")
        self.entry("a-editor.desktop", f"Type=Application\nName=alpha Editor\nExec={program} %F\nMimeType=image/png;\n")
        self.entry("hidden.desktop", f"Type=Application\nName=Hidden\nExec={program} %f\nMimeType=image/png;\nNoDisplay=true\n")
        self.entry("terminal.desktop", f"Type=Application\nName=Term\nExec={program} %f\nMimeType=image/png;\nTerminal=true\n")
        self.entry("gone.desktop", "Type=Application\nName=Gone\nExec=x %f\nTryExec=/nonexistent/x\nMimeType=image/png;\n")
        self.entry("link.desktop", "Type=Link\nName=Link\nURL=http://x\nMimeType=image/png;\n")
        self.entry("other.desktop", f"Type=Application\nName=Other\nExec={program} %f\nMimeType=text/plain;\n")
        self.entry("com.boswas.Photos.desktop", f"Type=Application\nName=Photos\nExec={program} %F\nMimeType=image/png;\n")
        self.entry("b-viewer.desktop", f"Type=Application\nName=System Beta\nExec={program} %f\nMimeType=image/png;\n", "system")
        self.entry("big.desktop", "Type=Application\nName=Big\nExec=x %f\nMimeType=image/png;\n" + "#" * openwith.MAX_ENTRY_BYTES)
        fx.write(self.tmp / "data" / "applications" / "notes.txt", b"not an entry")
        directories = [self.tmp / "data", self.tmp / "system", self.tmp / "missing"]
        found = openwith.applications_for("image/png", exclude=("com.boswas.Photos",), directories=directories,
                                          config_dirs=[])
        self.assertEqual([app.name for app in found], ["alpha Editor", "Beta Viewer"])
        self.assertEqual(found[1].icon, "beta")
        self.assertEqual(openwith.command(found[1], ["/a.png"]), [str(program), "/a.png"])
        fx.write(self.tmp / "config" / "mimeapps.list", b"[Default Applications]\nimage/png=b-viewer.desktop;\n")
        found = openwith.applications_for("IMAGE/PNG", directories=directories, config_dirs=[self.tmp / "config"],
                                          exclude=("com.boswas.Photos.desktop",))
        self.assertEqual([app.name for app in found], ["Beta Viewer", "alpha Editor"])
        self.assertEqual(openwith.applications_for("image/webp", directories=directories, config_dirs=[]), [])

    def test_parse_entry_reads_only_the_main_group(self):
        keys = openwith.parse_entry("# c\n[Other]\nName=No\n[Desktop Entry]\nName=Yes\\sSir\nName[de]=Ja\nExec=a\n"
                                    "Name=Again\n\n[Desktop Action x]\nExec=b\n")
        self.assertEqual(keys, {"Name": "Yes Sir", "Exec": "a"})

    def test_launch_refuses_relative_programs(self):
        self.assertFalse(openwith.launch([]))
        self.assertFalse(openwith.launch(["viewer", "/a.png"]))
        self.assertFalse(openwith.launch(["/nonexistent/program", "/a.png"]))


class CliTest(unittest.TestCase):
    def test_parse(self):
        parsed = cli.parse(["--slideshow", "a.png", "/abs/b.jpg", "file:///c%20d/e.png", "--new-window", "--", "--odd.png"],
                           base="/work")
        self.assertEqual(parsed.paths, ["/work/a.png", "/abs/b.jpg", "/c d/e.png", "/work/--odd.png"])
        self.assertTrue(parsed.slideshow and parsed.new_window)
        self.assertFalse(parsed.gallery or parsed.help or parsed.version)
        self.assertEqual(cli.parse(["--frobnicate", "-x"]).unknown, ["--frobnicate", "-x"])
        self.assertTrue(cli.parse(["-h"]).help)
        self.assertTrue(cli.parse(["--version"]).version)
        self.assertEqual(cli.parse([]).paths, [])

    def test_only_local_files(self):
        for argument in ("http://example.org/a.png", "file://server/share/a.png", "smb://host/a.png", "", "a\x00b",
                         "file:relative.png"):
            self.assertIsNone(cli.local_path(argument, "/work"), argument)
        self.assertEqual(cli.local_path("file://localhost/a/b.png"), "/a/b.png")
        self.assertEqual(cli.local_path("a/../b.png", "/work"), "/work/b.png")
        self.assertEqual(cli.local_path("odd:name.png", "/work"), "/work/odd:name.png")

    def test_absolute_keeps_options_and_fixes_names(self):
        self.assertEqual(cli.absolute(["--new-window", "a.png", "http://x/y.png", "--", "-b.png"], "/w"),
                         ["--new-window", "/w/a.png", "--", "/w/-b.png"])

    def test_too_many_paths_are_cut(self):
        self.assertEqual(len(cli.parse([f"{i}.png" for i in range(cli.MAX_PATHS + 50)], "/w").paths), cli.MAX_PATHS)


class FileUtilTest(TempCase):
    def test_atomic_write_replaces_or_keeps(self):
        target = self.tmp / "file.bin"
        fileutil.atomic_write(target, b"one")
        fileutil.atomic_write(target, b"two")
        self.assertEqual(target.read_bytes(), b"two")
        self.assertEqual(os.listdir(self.tmp), ["home", "file.bin"] if os.listdir(self.tmp)[0] == "home" else
                         sorted(os.listdir(self.tmp)))
        with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                fileutil.atomic_write(target, b"three")
        self.assertEqual(target.read_bytes(), b"two")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["file.bin", "home"])           # the temporary file is gone
        with self.assertRaises(OSError):
            fileutil.atomic_write(self.tmp / "no" / "such" / "dir.bin", b"x")

    def test_names(self):
        taken = fx.write(self.tmp / "a.png", b"")
        self.assertEqual(fileutil.free_name(self.tmp / "b.png"), str(self.tmp / "b.png"))
        self.assertEqual(fileutil.free_name(taken), str(self.tmp / "a (2).png"))
        fx.write(self.tmp / "a (2).png", b"")
        self.assertEqual(fileutil.free_name(taken), str(self.tmp / "a (3).png"))
        for good in ("a.png", "ä ö.jpg", ".hidden", "a" * 255):
            self.assertIsNone(fileutil.check_new_name(good), good)
        for bad in ("", "  ", ".", "..", "a/b", "a\x00b", "a\nb", "a" * 256, "é" * 200):
            self.assertIsNotNone(fileutil.check_new_name(bad), bad)

    def test_rename_never_replaces(self):
        old = fx.write(self.tmp / "old.png", b"old")
        other = fx.write(self.tmp / "other.png", b"other")
        with self.assertRaises(FileExistsError):
            fileutil.rename_no_replace(old, other)
        self.assertEqual(other.read_bytes(), b"other")
        fileutil.rename_no_replace(old, self.tmp / "new.png")
        self.assertFalse(old.exists())
        self.assertEqual((self.tmp / "new.png").read_bytes(), b"old")
        # a file system without hard links: a plain rename, still never onto a file
        with mock.patch("os.link", side_effect=OSError(1, "Operation not permitted")):
            fileutil.rename_no_replace(self.tmp / "new.png", self.tmp / "newer.png")
            with self.assertRaises(FileExistsError):
                fileutil.rename_no_replace(self.tmp / "newer.png", other)
        self.assertEqual((self.tmp / "newer.png").read_bytes(), b"old")

    def test_sizes(self):
        self.assertEqual([fileutil.format_size(n) for n in (0, 1, 1023, 1024, 1536, 5 * 1024 ** 2, 150 * 1024 ** 3)],
                         ["0 bytes", "1 byte", "1023 bytes", "1.0 KB", "1.5 KB", "5.0 MB", "150 GB"])


class FileInfoTest(TempCase):
    def test_describe(self):
        path = fx.write(self.tmp / "holiday" / "a.jpg", b"x" * 3000)
        details = exif.parse_tiff(fx.camera_exif(6))
        groups = fileinfo.describe(str(path), width=3000, height=4000, fmt="jpeg", bits_per_pixel=24, has_alpha=False,
                                   dpi=(96.0, 96.0), exif=details)
        rows = {group.title: dict(group.rows) for group in groups}
        self.assertEqual(list(rows), ["File", "Picture", "Camera", "Place"])
        self.assertEqual(rows["File"]["Folder"], str(self.tmp / "holiday"))
        self.assertEqual(rows["File"]["Type"], "JPEG")
        self.assertTrue(rows["File"]["Size"].startswith("2.9 KB"))
        self.assertEqual(rows["Picture"]["Dimensions"], "3000 × 4000 pixels")
        self.assertEqual(rows["Picture"]["Megapixels"], "12 MP")
        self.assertEqual(rows["Picture"]["Resolution"], "300 dpi")           # EXIF wins over the decoder's guess
        self.assertEqual(rows["Picture"]["Colour"], "24 bits per pixel, no transparency")
        self.assertEqual(rows["Camera"]["Camera"], "Boswas Prism One")       # the maker is not said twice
        self.assertEqual(rows["Camera"]["Lens"], "Boswas 24-70mm F2.8")
        self.assertEqual(rows["Camera"]["Exposure"], "1/250 s")
        self.assertEqual(rows["Camera"]["Aperture"], "f/2.8")
        self.assertEqual(rows["Camera"]["ISO"], "200")
        self.assertEqual(rows["Camera"]["Date taken"], "14 August 2026, 18:23")
        self.assertEqual(rows["Place"]["Coordinates"], "48.85837° N, 2.29448° E")
        self.assertEqual(rows["Place"]["Altitude"], "35 m above sea level")

    def test_describe_without_details_has_no_empty_rows(self):
        groups = fileinfo.describe(str(self.tmp / "missing.png"))
        self.assertEqual([group.title for group in groups], ["File"])
        animated = fileinfo.describe(str(self.tmp / "a.gif"), width=10, height=10, fmt="gif", frames=12, animated=True,
                                     exif=exif.Exif())
        rows = {group.title: dict(group.rows) for group in animated}
        self.assertEqual(rows["File"]["Type"], "GIF (animation, 12 frames)")
        self.assertNotIn("Camera", rows)
        self.assertEqual(fileinfo.format_megapixels(100, 100), "0.01 MP")
        self.assertEqual(fileinfo.format_dpi((72.0, 96.0)), "72 × 96 dpi")


class PrintPlacementTest(unittest.TestCase):
    def test_fit_keeps_everything_and_centres(self):
        placed = printing.place(2000, 3000, 400, 300, printing.FIT)
        self.assertEqual(placed.source, (0.0, 0.0, 400.0, 300.0))
        self.assertEqual(placed.target, (0.0, 750.0, 2000.0, 1500.0))

    def test_fill_covers_the_page_and_cuts_evenly(self):
        placed = printing.place(2000, 3000, 400, 300, printing.FILL)
        self.assertEqual(placed.target, (0.0, 0.0, 2000, 3000))
        self.assertEqual(placed.source, (100.0, 0.0, 200.0, 300.0))

    def test_actual_size_uses_the_resolutions(self):
        placed = printing.place(2400, 3300, 600, 300, printing.ACTUAL, page_dpi=300, picture_dpi=150)
        self.assertEqual(placed.target, (600.0, 1350.0, 1200.0, 600.0))
        self.assertEqual(placed.source, (0.0, 0.0, 600.0, 300.0))
        large = printing.place(1000, 1000, 4000, 400, printing.ACTUAL, page_dpi=100, picture_dpi=100)
        self.assertEqual(large.target, (0.0, 300.0, 1000.0, 400.0))
        self.assertEqual(large.source, (1500.0, 0.0, 1000.0, 400.0))

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            printing.place(0, 100, 10, 10)
        with self.assertRaises(ValueError):
            printing.place(100, 100, 10, 10, "stretch")
        self.assertTrue(printing.landscape_for(400, 300, printing.AUTO))
        self.assertFalse(printing.landscape_for(300, 400, printing.AUTO))
        self.assertFalse(printing.landscape_for(400, 300, printing.PORTRAIT))
        self.assertTrue(printing.landscape_for(300, 400, printing.LANDSCAPE))


if __name__ == "__main__":
    unittest.main()
