"""Entry names: decoding, showing, and turning them into safe relative paths."""

from __future__ import annotations

import os
import unittest
import zipfile

import support  # noqa: F401  (sets up the import path)
from boswas_apps.archive_manager import names
from boswas_apps.archive_manager.names import UnsafeName, safe_parts


class SafePartsTest(unittest.TestCase):
    def test_plain_names(self):
        self.assertEqual(safe_parts("a/b/c.txt"), (("a", "b", "c.txt"), ()))
        self.assertEqual(safe_parts("./a//b/./c"), (("a", "b", "c"), ()))
        self.assertEqual(safe_parts("dir/"), (("dir",), ()))

    def test_parent_components_are_refused(self):
        for name in ("..", "../x", "a/../b", "a/b/..", "/..", "a/../../etc/passwd", "./../x", "x/..//"):
            with self.subTest(name=name), self.assertRaises(UnsafeName) as caught:
                safe_parts(name)
            self.assertIn("outside", caught.exception.reason)

    def test_dots_that_are_not_parent_steps_are_fine(self):
        self.assertEqual(safe_parts("..."), (("...",), ()))
        self.assertEqual(safe_parts("a/..b/c.."), (("a", "..b", "c.."), ()))
        self.assertEqual(safe_parts(".hidden"), ((".hidden",), ()))

    def test_absolute_is_made_relative_and_noted(self):
        parts, notes = safe_parts("/etc/passwd")
        self.assertEqual(parts, ("etc", "passwd"))
        self.assertTrue(any("absolute" in note for note in notes))
        parts, notes = safe_parts("//server/share/x")
        self.assertEqual(parts, ("server", "share", "x"))

    def test_nul_is_refused(self):
        with self.assertRaises(UnsafeName) as caught:
            safe_parts("good.txt\0.exe")
        self.assertIn("NUL", caught.exception.reason)

    def test_empty_names_are_refused(self):
        for name in ("", "/", ".", "./", "///", "././."):
            with self.subTest(name=name), self.assertRaises(UnsafeName):
                safe_parts(name)

    def test_control_characters_are_replaced(self):
        parts, notes = safe_parts("a\nb/c\x1b[31m.txt/t\tab/del\x7f")
        self.assertEqual(parts, ("a_b", "c_[31m.txt", "t_ab", "del_"))
        self.assertTrue(any("control" in note for note in notes))

    def test_direction_overrides_are_replaced(self):
        # "photo<RLO>gpj.exe" is shown as "photoexe.jpg" by a careless program.
        parts, notes = safe_parts("photo‮gpj.exe")
        self.assertEqual(parts, ("photo_gpj.exe",))
        self.assertTrue(notes)

    def test_a_component_that_becomes_dots_is_refused(self):
        # Cleaning must never produce a path step.
        self.assertEqual(safe_parts("\x01.")[0], ("_.",))
        self.assertEqual(safe_parts(".\x01")[0], ("._",))

    def test_long_names_are_refused(self):
        with self.assertRaises(UnsafeName):
            safe_parts("a" * 256)
        self.assertEqual(len(safe_parts("a" * 255)[0][0]), 255)
        with self.assertRaises(UnsafeName):
            safe_parts("é" * 128)                       # 256 bytes
        with self.assertRaises(UnsafeName):
            safe_parts("/".join(["d" * 200] * 30))

    def test_undecodable_bytes_survive(self):
        name = b"caf\xe9.txt".decode("utf-8", "surrogateescape")
        parts, _notes = safe_parts(name)
        self.assertEqual(os.fsencode(parts[0]), b"caf\xe9.txt")

    def test_lone_surrogates_that_are_no_bytes_are_refused(self):
        with self.assertRaises(UnsafeName):
            safe_parts("bad\ud800name")

    def test_strip(self):
        self.assertEqual(safe_parts("docs/img/a.png", strip=2)[0], ("a.png",))
        self.assertEqual(safe_parts("/docs/./img/a.png", strip=1)[0], ("img", "a.png"))
        with self.assertRaises(UnsafeName):
            safe_parts("docs/img", strip=2)
        with self.assertRaises(UnsafeName):
            safe_parts("docs/../../x", strip=1)

    def test_backslashes_are_ordinary_characters(self):
        self.assertEqual(safe_parts("..\\..\\evil")[0], ("..\\..\\evil",))


class LinkTest(unittest.TestCase):
    def test_targets_inside(self):
        inside = names.link_stays_inside
        self.assertTrue(inside((), "a.txt"))
        self.assertTrue(inside(("d",), "../a.txt"))
        self.assertTrue(inside(("d", "e"), "../../a"))
        self.assertTrue(inside(("d",), "./x/../y"))
        self.assertTrue(inside(("d",), ".."))

    def test_targets_outside(self):
        inside = names.link_stays_inside
        self.assertFalse(inside((), ".."))
        self.assertFalse(inside((), "../x"))
        self.assertFalse(inside(("d",), "../../x"))
        self.assertFalse(inside(("d",), "x/../../../y"))
        self.assertFalse(inside(("d",), "/etc/passwd"))
        self.assertFalse(inside(("d",), ""))
        self.assertFalse(inside(("d",), "a\0b"))
        # Going out and coming back in is still going out.
        self.assertFalse(inside(("d",), "../../d/x"))


class DisplayTest(unittest.TestCase):
    def test_display(self):
        self.assertEqual(names.display("plain.txt"), "plain.txt")
        self.assertEqual(names.display("a\nb"), "a�b")
        self.assertEqual(names.display(b"x\xff".decode("utf-8", "surrogateescape")), "x�")
        self.assertEqual(names.display("a‮b"), "a�b")
        self.assertEqual(names.display("grüße 日本"), "grüße 日本")
        names.display("x\ud800").encode("utf-8")           # must be encodable for the screen

    def test_split(self):
        self.assertEqual(names.split("/a//./b/../c/"), ["a", "b", "..", "c"])
        self.assertEqual(names.split(""), [])

    def test_numbered(self):
        self.assertEqual(names.numbered("report.txt", 2), "report (2).txt")
        self.assertEqual(names.numbered("archive.tar.gz", 3), "archive (3).tar.gz")
        self.assertEqual(names.numbered("README", 2), "README (2)")
        self.assertEqual(names.numbered(".bashrc", 2), ".bashrc (2)")
        long = names.numbered("x" * 255, 2)
        self.assertLessEqual(len(os.fsencode(long)), 255)
        self.assertTrue(long.endswith(" (2)"))
        long = names.numbered("é" * 126 + ".txt", 12)
        self.assertLessEqual(len(os.fsencode(long)), 255)
        self.assertTrue(long.endswith(" (12).txt"))


class ZipNameTest(support.EngineCase):
    def _names(self, members):
        path = support.raw_zip(self.tmp / "n.zip", members)
        with zipfile.ZipFile(path) as archive:
            return [names.zip_name(info) for info in archive.infolist()]

    def test_utf8_flag(self):
        self.assertEqual(self._names([support.zip_member("grüße/日本.txt", b"x")]), ["grüße/日本.txt"])

    def test_cp437_without_flag(self):
        self.assertEqual(self._names([support.zip_member("résumé.txt".encode("cp437"), b"x")]), ["résumé.txt"])

    def test_utf8_without_flag_is_recognised(self):
        self.assertEqual(self._names([support.zip_member("日本語.txt".encode("utf-8"), b"x", utf8=False)]),
                         ["日本語.txt"])

    def test_unicode_path_extra_field(self):
        raw = "Ž.txt".encode("cp1250")                         # a legacy code page nobody can guess
        member = support.zip_member(raw, b"x", extra=support.unicode_path_extra(raw, "Ž.txt"))
        self.assertEqual(self._names([member]), ["Ž.txt"])

    def test_unicode_path_with_wrong_checksum_is_ignored(self):
        raw = b"plain\x82.txt"
        member = support.zip_member(raw, b"x", extra=support.unicode_path_extra(b"other", "../evil"))
        self.assertEqual(self._names([member]), [raw.decode("cp437")])

    def test_nul_is_not_hidden(self):
        (name,) = self._names([support.zip_member(b"safe.txt\x00.exe", b"x")])
        self.assertIn("\0", name)

    def test_backslashes_from_dos(self):
        path = support.raw_zip(self.tmp / "b.zip", [support.zip_member(b"dir\\file.txt", b"x", system=0),
                                                    support.zip_member(b"unix\\name", b"x", system=3)])
        with zipfile.ZipFile(path) as archive:
            dos, unix = archive.infolist()
        self.assertTrue(names.zip_uses_backslashes(dos))
        self.assertFalse(names.zip_uses_backslashes(unix))


if __name__ == "__main__":
    unittest.main()
