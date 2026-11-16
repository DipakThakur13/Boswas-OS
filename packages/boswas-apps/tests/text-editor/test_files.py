"""Encodings, line endings, reading and atomic writing, the session and the settings (no Qt)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from boswas_apps.text_editor import codec, fileio, session, settings


class Temp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="editor-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def file(self, name: str, data: bytes) -> str:
        path = self.tmp / name
        path.write_bytes(data)
        return str(path)


class DetectTest(unittest.TestCase):
    def test_signatures(self):
        self.assertIs(codec.detect(b"\xef\xbb\xbfhello"), codec.UTF8_BOM)
        self.assertIs(codec.detect("\ufeffhi".encode("utf-16-le")), codec.UTF16_LE)
        self.assertIs(codec.detect("\ufeffhi".encode("utf-16-be")), codec.UTF16_BE)
        self.assertIs(codec.detect(b""), codec.UTF8)
        self.assertIs(codec.detect(b"plain ascii\n"), codec.UTF8)
        self.assertIs(codec.detect("grüße 日本 \U0001f600".encode("utf-8")), codec.UTF8)

    def test_utf16_without_a_mark(self):
        self.assertIs(codec.detect("plain text, no mark".encode("utf-16-le")), codec.UTF16_LE_PLAIN)
        self.assertIs(codec.detect("plain text, no mark".encode("utf-16-be")), codec.UTF16_BE_PLAIN)

    def test_single_byte_fallbacks_lose_nothing(self):
        windows = b"caf\xe9 \x93quoted\x94\n"
        self.assertIs(codec.detect(windows), codec.WINDOWS_1252)
        self.assertEqual(codec.decode(windows, codec.WINDOWS_1252), "café “quoted”\n")
        undefined_in_1252 = b"a\x81b\x8d\x8f\x90\x9d\n"
        self.assertIs(codec.detect(undefined_in_1252), codec.LATIN_1)
        every_byte = bytes(range(1, 256)).replace(b"\x00", b"")
        for data in (windows, undefined_in_1252):
            encoding = codec.detect(data)
            self.assertEqual(codec.encode(codec.decode(data, encoding), encoding), data)
        self.assertEqual(codec.encode(codec.decode(every_byte, codec.LATIN_1), codec.LATIN_1), every_byte)

    def test_locale_encoding_is_tried_first(self):
        cyrillic = codec._single_byte("koi8-r")
        self.assertEqual(cyrillic.key, "locale:koi8-r")
        data = "привет".encode("koi8-r")
        self.assertEqual(codec.detect(data, cyrillic), cyrillic)
        self.assertEqual(codec.decode(data, cyrillic), "привет")
        self.assertEqual(codec.by_key("locale:koi8-r"), cyrillic)
        for name in ("utf-8", "UTF-16", "big5", "shift_jis", "no-such-codec", "", None, "ascii", "x" * 100):
            self.assertIsNone(codec._single_byte(name), name)
        self.assertIs(codec._single_byte("cp1252"), codec.WINDOWS_1252)
        self.assertIs(codec._single_byte("latin-1"), codec.LATIN_1)

    def test_binary_is_refused(self):
        for data in (b"\x7fELF\x02\x01\x01\x00" + bytes(64), b"PK\x03\x04\x14\x00\x00\x00", b"\x89PNG\r\n\x1a\n\x00\x00",
                     bytes(range(32)) * 10, b"text\x00with a zero", b"\x00", b"\x01\x02\x03\x04\x05\x06"):
            with self.subTest(data=data[:12]):
                with self.assertRaises(codec.BinaryFileError):
                    codec.detect(data)
        self.assertFalse(codec.looks_binary(b"tabs\tand\x1b[0m escapes\r\n\x0c"))

    def test_keys(self):
        for encoding in (*codec.CHOICES, codec.UTF16_LE_PLAIN, codec.UTF16_BE_PLAIN):
            self.assertIs(codec.by_key(encoding.key), encoding)
        for bad in ("", "klingon", "locale:", "locale:utf-8", "locale:" + "x" * 99, None, 5):
            self.assertIs(codec.by_key(bad), codec.UTF8)

    def test_round_trips_of_every_choice(self):
        text = "café\nnaïve"
        for encoding in codec.CHOICES:
            with self.subTest(encoding=encoding.key):
                data = codec.encode(text, encoding)
                self.assertTrue(data.startswith(encoding.bom))
                self.assertEqual(codec.decode(data, encoding), text)
        self.assertTrue(codec.can_encode("€", codec.WINDOWS_1252))
        self.assertFalse(codec.can_encode("€", codec.LATIN_1))
        self.assertFalse(codec.can_encode("日", codec.WINDOWS_1252))
        with self.assertRaises(UnicodeEncodeError):
            codec.encode("日", codec.LATIN_1)


class LineEndingTest(unittest.TestCase):
    def test_counting(self):
        self.assertEqual(codec.count_line_endings("a\nb\r\nc\rd\r\n"), {"lf": 1, "crlf": 2, "cr": 1})
        self.assertEqual(codec.count_line_endings(""), {"lf": 0, "crlf": 0, "cr": 0})
        self.assertEqual(codec.count_line_endings("\r\r\n\n"), {"lf": 1, "crlf": 1, "cr": 1})

    def test_dominant(self):
        self.assertEqual(codec.dominant_line_ending({"lf": 0, "crlf": 0, "cr": 0}), "lf")
        self.assertEqual(codec.dominant_line_ending({"lf": 0, "crlf": 0, "cr": 0}, "crlf"), "crlf")
        self.assertEqual(codec.dominant_line_ending({"lf": 5, "crlf": 2, "cr": 0}), "lf")
        self.assertEqual(codec.dominant_line_ending({"lf": 1, "crlf": 2, "cr": 9}), "cr")
        self.assertEqual(codec.dominant_line_ending({"lf": 3, "crlf": 3, "cr": 0}), "crlf")

    def test_mixed(self):
        self.assertFalse(codec.is_mixed({"lf": 9, "crlf": 0, "cr": 0}))
        self.assertTrue(codec.is_mixed({"lf": 9, "crlf": 1, "cr": 0}))
        self.assertEqual(codec.describe_line_endings({"lf": 3, "crlf": 120, "cr": 0}),
                         "120 Windows (CRLF), 3 Unix (LF)")

    def test_conversion(self):
        self.assertEqual(codec.normalize_line_endings("a\r\nb\rc\nd"), "a\nb\nc\nd")
        self.assertEqual(codec.to_line_ending("a\nb\n", "crlf"), "a\r\nb\r\n")
        self.assertEqual(codec.to_line_ending("a\nb\n", "cr"), "a\rb\r")
        self.assertEqual(codec.to_line_ending("a\nb\n", "lf"), "a\nb\n")

    def test_trim(self):
        self.assertEqual(codec.trim_trailing_whitespace("a  \n\tb\t \n\n c"), "a\n\tb\n\n c")
        self.assertEqual(codec.trim_trailing_whitespace("end \t"), "end")
        self.assertEqual(codec.trim_trailing_whitespace("clean\ntext"), "clean\ntext")
        self.assertEqual(codec.trim_trailing_whitespace("nbsp\u00a0\n"), "nbsp\u00a0\n")


class LoadTest(Temp):
    def test_text_comes_back_byte_for_byte(self):
        cases = {
            "utf8.txt": "héllo\nwörld\n".encode("utf-8"),
            "bom.txt": b"\xef\xbb\xbfa\r\nb\r\n",
            "utf16le.txt": "\ufeffhi\r\nthere".encode("utf-16-le"),
            "utf16be.txt": "\ufeffhi\nthere\n".encode("utf-16-be"),
            "ansi.txt": b"caf\xe9 \x93q\x94\r\n",
            "latin.txt": b"x\x81y\rz\r",
            "mac.txt": b"one\rtwo\rthree",
            "empty.txt": b"",
            "nbsp.txt": "a\u00a0b\u2028c\n".encode("utf-8"),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                loaded = fileio.load(self.file(name, data))
                self.assertNotIn("\r", loaded.text)
                self.assertEqual(fileio.to_bytes(loaded.text, loaded.encoding, loaded.line_ending), data)

    def test_what_is_known_about_a_file(self):
        loaded = fileio.load(self.file("w.txt", b"\xef\xbb\xbfa\r\nb\r\n"))
        self.assertEqual((loaded.encoding.key, loaded.line_ending, loaded.text, loaded.mixed, loaded.writable),
                         ("utf-8-bom", "crlf", "a\nb\n", False, True))
        self.assertEqual(loaded.signature, fileio.signature(loaded.path))
        mixed = fileio.load(self.file("m.txt", b"a\r\nb\r\nc\nd\n\re"))
        self.assertTrue(mixed.mixed)
        self.assertEqual(mixed.line_endings, {"lf": 2, "crlf": 2, "cr": 1})
        self.assertEqual(mixed.line_ending, "crlf")
        self.assertEqual(fileio.load(self.file("n.txt", b"no line end"), default_line_ending="crlf").line_ending, "crlf")

    def test_a_given_encoding_is_used(self):
        path = self.file("k.txt", "привет".encode("koi8-r"))
        self.assertEqual(fileio.load(path, encoding=codec._single_byte("koi8-r")).text,
                         "привет")
        with self.assertRaises(fileio.LoadError) as caught:
            fileio.load(self.file("bad.txt", b"\xff\xfe\xfd"), encoding=codec.UTF8)
        self.assertEqual(caught.exception.kind, "decode")

    def test_refusals_say_why(self):
        cases = (
            ("missing", str(self.tmp / "nope.txt")),
            ("directory", str(self.tmp)),
            ("binary", self.file("a.out", b"\x7fELF\x00\x00\x01")),
            ("too_large", self.file("big.txt", b"x" * 2000)),
        )
        for kind, path in cases:
            with self.subTest(kind=kind):
                with self.assertRaises(fileio.LoadError) as caught:
                    fileio.load(path, max_bytes=1000)
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn("Traceback", str(caught.exception))
                self.assertTrue(str(caught.exception).endswith("."))

    def test_special_files_are_not_read(self):
        fifo = self.tmp / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(fileio.LoadError) as caught:
            fileio.load(str(fifo))
        self.assertEqual(caught.exception.kind, "special")
        with self.assertRaises(fileio.LoadError):
            fileio.check("/dev/zero")

    @unittest.skipIf(os.geteuid() == 0, "root reads everything")
    def test_permission(self):
        path = self.file("secret.txt", b"x")
        os.chmod(path, 0)
        with self.assertRaises(fileio.LoadError) as caught:
            fileio.load(path)
        self.assertEqual(caught.exception.kind, "permission")

    def test_progress_and_cancel(self):
        path = self.file("long.txt", b"line\n" * 700000)             # 3.5 MB: four pieces
        seen: list[float] = []
        loaded = fileio.load(path, progress=seen.append)
        self.assertEqual(loaded.text.count("\n"), 700000)
        self.assertGreaterEqual(len(seen), 3)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)
        calls = []
        with self.assertRaises(fileio.Cancelled):
            fileio.load(path, cancelled=lambda: calls.append(1) or len(calls) > 2)

    def test_sizes_in_words(self):
        self.assertEqual(fileio.describe_size(512), "512 bytes")
        self.assertEqual(fileio.describe_size(2048), "2 KB")
        self.assertEqual(fileio.describe_size(3 * fileio.MIB + fileio.MIB // 2), "3.5 MB")
        self.assertEqual(fileio.describe_size(fileio.LARGE_FILE), "32 MB")
        self.assertEqual(fileio.describe_size(fileio.MAX_FILE), "512 MB")
        self.assertEqual(fileio.describe_size(3 * 1024 * fileio.MIB), "3.0 GB")


class SaveTest(Temp):
    def test_new_file(self):
        path = str(self.tmp / "new.txt")
        mark = fileio.save(path, b"hello\n")
        self.assertEqual(Path(path).read_bytes(), b"hello\n")
        self.assertEqual(mark, fileio.signature(path))
        mask = os.umask(0)
        os.umask(mask)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o666 & ~mask)
        self.assertEqual(os.listdir(self.tmp), ["new.txt"])              # no temporary file is left

    def test_permissions_are_kept(self):
        path = self.file("script.sh", b"#!/bin/sh\n")
        for mode in (0o755, 0o600, 0o640):
            os.chmod(path, mode)
            fileio.save(path, b"#!/bin/sh\necho hi\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode)
        self.assertEqual(Path(path).read_bytes(), b"#!/bin/sh\necho hi\n")

    def test_the_file_is_replaced_not_rewritten(self):
        path = self.file("a.txt", b"old")
        with open(path, "rb") as reader:                                 # somebody still reads the old file
            before = os.stat(path).st_ino
            fileio.save(path, b"new content")
            self.assertEqual(reader.read(), b"old")
        self.assertNotEqual(os.stat(path).st_ino, before)
        self.assertEqual(Path(path).read_bytes(), b"new content")

    def test_a_symbolic_link_stays_a_link(self):
        target = self.file("real.txt", b"old")
        link = str(self.tmp / "link.txt")
        os.symlink(target, link)
        fileio.save(link, b"through the link")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(Path(target).read_bytes(), b"through the link")

    def test_failure_leaves_the_old_file(self):
        path = self.file("keep.txt", b"precious")
        with self.assertRaises(OSError):
            fileio.save(str(self.tmp / "no-such-folder" / "x.txt"), b"data")
        with self.assertRaises(OSError):
            fileio.save(str(self.tmp), b"data")                          # a folder is not a file
        self.assertEqual(Path(path).read_bytes(), b"precious")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["keep.txt"])

    @unittest.skipIf(os.geteuid() == 0, "root writes everywhere")
    def test_folder_without_write_permission(self):
        folder = self.tmp / "locked"
        folder.mkdir()
        path = folder / "file.txt"
        path.write_bytes(b"old")
        os.chmod(folder, 0o555)
        try:
            fileio.save(str(path), b"written in place")                  # the file itself is writable
            self.assertEqual(path.read_bytes(), b"written in place")
            with self.assertRaises(OSError):
                fileio.save(str(folder / "other.txt"), b"x")
        finally:
            os.chmod(folder, 0o755)

    def test_signature_notices_changes(self):
        path = self.file("s.txt", b"one")
        first = fileio.signature(path)
        self.assertEqual(first, fileio.signature(path))
        fileio.save(path, b"two!")
        self.assertNotEqual(first, fileio.signature(path))
        os.unlink(path)
        self.assertIsNone(fileio.signature(path))

    def test_unencodable_text(self):
        with self.assertRaises(UnicodeEncodeError):
            fileio.to_bytes("日本", codec.WINDOWS_1252, "lf")
        self.assertEqual(fileio.to_bytes("a\nb", codec.UTF16_LE, "crlf"), b"\xff\xfea\x00\r\x00\n\x00b\x00")


class SettingsTest(Temp):
    def test_defaults_and_round_trip(self):
        self.assertEqual(settings.load(self.tmp), settings.DEFAULTS)
        self.assertFalse(settings.DEFAULTS["line_numbers"])
        self.assertTrue(settings.DEFAULTS["restore_session"])
        changed = dict(settings.DEFAULTS, font_size=14, word_wrap=False, theme="dark", font_family="Inter",
                       tab_width=4, default_line_ending="crlf")
        self.assertTrue(settings.save(self.tmp, changed))
        self.assertEqual(settings.load(self.tmp), changed)
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp / settings.SETTINGS_FILE).st_mode), 0o600)

    def test_bad_values_become_defaults(self):
        raw = {"font_size": 9999, "tab_width": 0, "word_wrap": "yes", "theme": "pink", "font_family": "x" * 500,
               "default_line_ending": "\r\n", "line_numbers": 1, "regex": None, "unknown": 5, "font_bold": True}
        cleaned = settings.validated(raw)
        self.assertEqual({key: value for key, value in cleaned.items() if settings.DEFAULTS[key] != value},
                         {"font_bold": True})
        for junk in (None, [], "text", 5, {"font_size": True}, {"font_family": "bad\nname"}):
            self.assertEqual(settings.validated(junk), settings.DEFAULTS)

    def test_damaged_files(self):
        for content in (b"", b"{", b"\xff\xff", b"[" * 50000, b"x" * (settings.MAX_BYTES + 1), b"null"):
            (self.tmp / settings.SETTINGS_FILE).write_bytes(content)
            (self.tmp / settings.RECENT_FILE).write_bytes(content)
            self.assertEqual(settings.load(self.tmp), settings.DEFAULTS)
            self.assertEqual(settings.load_recent(self.tmp), [])
        self.assertFalse(settings.save(self.tmp / "missing", settings.DEFAULTS))

    def test_recent_files(self):
        recent: list[str] = []
        for number in range(15):
            recent = settings.add_recent(recent, f"/home/u/file{number}.txt")
        self.assertEqual(len(recent), settings.MAX_RECENT)
        self.assertEqual(recent[0], "/home/u/file14.txt")
        recent = settings.add_recent(recent, "/home/u/file10.txt")
        self.assertEqual(recent[:2], ["/home/u/file10.txt", "/home/u/file14.txt"])
        self.assertEqual(len(recent), settings.MAX_RECENT)
        for bad in ("relative.txt", "", "/x\x00y", "/" + "a" * 5000, None, 7):
            self.assertEqual(settings.add_recent(recent, bad), recent)
        self.assertTrue(settings.save_recent(self.tmp, recent))
        self.assertEqual(settings.load_recent(self.tmp), recent)
        (self.tmp / settings.RECENT_FILE).write_text(json.dumps(["/a", "b", 5, "/a", None, "/c"] + ["/x%d" % i for i in range(30)]))
        self.assertEqual(settings.load_recent(self.tmp)[:2], ["/a", "/c"])
        self.assertEqual(len(settings.load_recent(self.tmp)), settings.MAX_RECENT)

    def test_zoom_steps(self):
        self.assertEqual(settings.next_zoom(100, 1), 110)
        self.assertEqual(settings.next_zoom(100, -1), 90)
        self.assertEqual(settings.next_zoom(500, 1), 500)
        self.assertEqual(settings.next_zoom(10, -1), 10)


class SessionTest(Temp):
    def test_round_trip(self):
        tabs = [session.TabState(path="/home/u/a.txt", cursor=12, encoding="utf-8-bom", line_ending="crlf"),
                session.TabState(text="unsaved \u00a0 words\n\U0001f600", title="Untitled 2", cursor=3),
                session.TabState(path="/home/u/b.txt", text="changed", encoding="windows-1252")]
        self.assertTrue(session.save(self.tmp, tabs, 2))
        self.assertEqual(session.load(self.tmp), (tabs, 2))
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp / session.FILE_NAME).st_mode), 0o600)
        session.clear(self.tmp)
        self.assertEqual(session.load(self.tmp), ([], 0))
        session.clear(self.tmp)                                          # nothing to clear: no error

    def test_caps(self):
        big = session.TabState(text="x" * (session.MAX_TAB_TEXT + 1))
        small = session.TabState(text="small")
        self.assertEqual(session.fits([big, small]), [False, True])
        quarter = session.TabState(text="y" * session.MAX_TAB_TEXT)
        self.assertEqual(session.fits([quarter] * 5), [True, True, True, True, False])
        self.assertEqual(len(session.parse(session.dump([big, small]))[0]), 1)
        many = [session.TabState(path=f"/f{number}") for number in range(session.MAX_TABS + 20)]
        self.assertEqual(len(session.parse(session.dump(many))[0]), session.MAX_TABS)

    def test_hostile_files(self):
        for data in (b"", b"{", b"\xff", b"[]", b'{"version": 2, "tabs": []}', b'{"version": 1, "tabs": 5}',
                     b"[" * 100000, b'{"version": 1}'):
            self.assertEqual(session.parse(data), ([], 0))
        records = [
            {"path": "relative.txt"}, {"path": "/ok.txt", "cursor": -5, "line_ending": "x", "encoding": 7},
            {"path": None, "text": None}, {"text": "zero\x00byte"}, {"text": 5}, "junk", None,
            {"path": "/p\x00q"}, {"text": "fine\r\nwindows", "title": "bad\ntitle", "cursor": True},
            {"text": "z" * (session.MAX_TAB_TEXT + 1)},
        ]
        tabs, current = session.parse(json.dumps({"version": 1, "current": 99, "tabs": records}).encode())
        self.assertEqual(current, 0)
        self.assertEqual(tabs, [session.TabState(path="/ok.txt"), session.TabState(text="fine\nwindows")])
        (self.tmp / session.FILE_NAME).write_bytes(b"x" * 1000)
        self.assertEqual(session.load(self.tmp), ([], 0))
        self.assertEqual(session.load(self.tmp / "missing"), ([], 0))


if __name__ == "__main__":
    unittest.main()
