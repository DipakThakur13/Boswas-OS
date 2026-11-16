"""Recognising and reading archives: formats, listings, entry content, passwords, testing."""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import stat
import tarfile
import threading
import time
import unittest
import zipfile

import support
from boswas_apps.archive_manager import formats, reader, sevenzip
from boswas_apps.archive_manager.errors import (ArchiveError, Cancelled, PasswordRequired, UnsupportedFormat,
                                                WrongPassword)
from support import raw_tar, raw_zip, tar_dir, tar_link, tar_member, zip_member


def read_all(archive, entry, password=None) -> bytes:
    with archive.open_entry(entry, password) as handle:
        data = b""
        while True:
            piece = handle.read(65536)
            if not piece:
                return data
            data += piece


class DetectTest(support.EngineCase):
    def test_kinds_by_content(self):
        cases = {}
        cases["a.zip"] = raw_zip(self.tmp / "a.zip", [zip_member("x", b"1")])
        cases["a.tar"] = raw_tar(self.tmp / "a.tar", [tar_member("x", b"1")])
        for mode, name in (("w:gz", "a.tar.gz"), ("w:bz2", "a.tar.bz2"), ("w:xz", "a.tar.xz")):
            cases[name] = raw_tar(self.tmp / name, [tar_member("x", b"1")], mode=mode)
        for opener, name in ((gzip.open, "f.txt.gz"), (bz2.open, "f.txt.bz2"), (lzma.open, "f.txt.xz")):
            with opener(self.tmp / name, "wb") as handle:
                handle.write(b"just text, long enough not to look like a header " * 20)
            cases[name] = self.tmp / name
        expected = {"a.zip": "zip", "a.tar": "tar", "a.tar.gz": "tar.gz", "a.tar.bz2": "tar.bz2", "a.tar.xz": "tar.xz",
                    "f.txt.gz": "gz", "f.txt.bz2": "bz2", "f.txt.xz": "xz"}
        for name, path in cases.items():
            self.assertEqual(formats.detect(path).key, expected[name], name)

    def test_the_name_does_not_decide(self):
        path = raw_tar(self.tmp / "really-a-tar.zip", [tar_member("x", b"1")], mode="w:gz")
        self.assertEqual(formats.detect(path).key, "tar.gz")
        path = raw_zip(self.tmp / "really-a-zip.tar.gz", [zip_member("x", b"1")])
        self.assertEqual(formats.detect(path).key, "zip")
        jar = raw_zip(self.tmp / "app.jar", [zip_member("META-INF/MANIFEST.MF", b"1")])
        self.assertEqual(formats.detect(jar).key, "zip")

    def test_empty_archives(self):
        self.assertEqual(formats.detect(raw_zip(self.tmp / "e.zip", [])).key, "zip")
        self.assertEqual(formats.detect(raw_tar(self.tmp / "e.tar", [])).key, "tar")
        self.assertEqual(formats.detect(raw_tar(self.tmp / "e.tar.gz", [], mode="w:gz")).key, "tar.gz")
        for name in ("e.tar", "e.zip", "e.tar.gz"):
            with reader.open_archive(self.tmp / name) as archive:
                self.assertEqual(archive.entries, [])
                self.assertEqual(archive.warning, "")

    def test_zip_with_data_in_front(self):
        inner = raw_zip(self.tmp / "inner.zip", [zip_member("x", b"1")]).read_bytes()
        (self.tmp / "sfx.exe").write_bytes(b"MZ" + bytes(300) + inner)
        self.assertEqual(formats.detect(self.tmp / "sfx.exe").key, "zip")

    def test_not_archives(self):
        (self.tmp / "text.txt").write_text("hello " * 200)
        (self.tmp / "empty.zip").write_bytes(b"")
        (self.tmp / "zeros.bin").write_bytes(bytes(4096))
        for name in ("text.txt", "empty.zip", "zeros.bin"):
            with self.subTest(name=name), self.assertRaises(UnsupportedFormat):
                formats.detect(self.tmp / name)
        with self.assertRaises(ArchiveError):
            formats.detect(self.tmp / "missing.zip")
        with self.assertRaises(UnsupportedFormat):
            formats.detect(self.tmp)

    def test_seven_zip_and_rar_are_recognised(self):
        (self.tmp / "a.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + bytes(64))
        (self.tmp / "a.rar").write_bytes(b"Rar!\x1a\x07\x01\x00" + bytes(64))
        self.assertEqual(formats.detect(self.tmp / "a.7z").key, "7z")
        self.assertEqual(formats.detect(self.tmp / "a.rar").key, "rar")

    def test_without_7z_those_need_the_package(self):
        (self.tmp / "a.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + bytes(64))
        old = sevenzip.PROGRAMS
        sevenzip.PROGRAMS = (str(self.tmp / "no-such-program"),)
        try:
            with self.assertRaises(UnsupportedFormat) as caught:
                reader.open_archive(self.tmp / "a.7z")
            self.assertIn("needs the 7zip package", str(caught.exception))
            self.assertEqual(caught.exception.needs, "7zip")
            states = {item.title: item for item in formats.support()}
            self.assertEqual(states["7z"].state, "not available")
            self.assertIn("7zip", states["7z"].note)
        finally:
            sevenzip.PROGRAMS = old

    def test_zstd_is_listed_honestly(self):
        (self.tmp / "a.zst").write_bytes(b"\x28\xb5\x2f\xfd" + bytes(64))
        states = {item.title: item for item in formats.support()}
        if formats.zstd_module() is None:
            self.assertEqual(formats.detect(self.tmp / "a.zst").key, "zst")
            with self.assertRaises(UnsupportedFormat):
                reader.open_archive(self.tmp / "a.zst")
            self.assertEqual(states["ZST, TAR.ZST"].state, "not available")
        else:
            self.assertEqual(states["ZST, TAR.ZST"].state, "read")

    def test_names(self):
        self.assertEqual(formats.strip_extension("photos.tar.gz"), "photos")
        self.assertEqual(formats.strip_extension("Photos.ZIP"), "Photos")
        self.assertEqual(formats.strip_extension("a.tgz"), "a")
        self.assertEqual(formats.strip_extension(".zip"), ".zip.extracted")
        self.assertEqual(formats.strip_extension("noext"), "noext.extracted")
        self.assertEqual(formats.stream_inner_name("report.txt.gz"), "report.txt")
        self.assertEqual(formats.stream_inner_name("backup.tgz"), "backup.tar")
        self.assertEqual(formats.stream_inner_name("odd"), "odd.out")
        self.assertEqual(formats.format_for_new("/x/a.tar.xz").key, "tar.xz")
        self.assertEqual(formats.format_for_new("a.TGZ").key, "tar.gz")
        self.assertEqual(formats.format_for_new("a.zip").key, "zip")
        self.assertIsNone(formats.format_for_new("a.7z"))


class ZipReadTest(support.EngineCase):
    def test_listing_fields(self):
        path = raw_zip(self.tmp / "l.zip", [
            zip_member("docs/", b"", is_dir=True, method=0), zip_member("docs/a.txt", b"hello world" * 10, mode=stat.S_IFREG | 0o640),
            zip_member("link", b"docs/a.txt", mode=stat.S_IFLNK | 0o777, method=0),
            zip_member("win.txt", b"x", system=0)], comment=b"made by a test")
        archive = reader.open_archive(path)
        by_path = {entry.path: entry for entry in archive.entries}
        self.assertEqual(archive.format.key, "zip")
        self.assertEqual(archive.comment, "made by a test")
        self.assertEqual(by_path["docs"].kind, "dir")
        text = by_path["docs/a.txt"]
        self.assertEqual((text.kind, text.size, text.mode, text.method, text.encrypted), ("file", 110, 0o640, "Deflate", False))
        self.assertLess(text.packed, 110)
        self.assertEqual(text.crc, zipfile.crc32(b"hello world" * 10))
        self.assertEqual(time.localtime(text.mtime)[:6], support.DOS_TIME)
        self.assertEqual(by_path["link"].kind, "symlink")
        self.assertEqual(archive.link_target(by_path["link"]), "docs/a.txt")
        self.assertIsNone(by_path["win.txt"].mode)
        self.assertEqual(read_all(archive, text), b"hello world" * 10)
        self.assertTrue(archive.writable)
        archive.close()

    def test_damaged_zip(self):
        path = raw_zip(self.tmp / "d.zip", [zip_member("a", b"x" * 1000)])
        raw = path.read_bytes()
        (self.tmp / "cut.zip").write_bytes(raw[:len(raw) - 30])
        with self.assertRaises(ArchiveError) as caught:
            reader.open_archive(self.tmp / "cut.zip")
        self.assertNotIsInstance(caught.exception, UnsupportedFormat)
        self.assertIn("damaged", str(caught.exception))

    def test_encrypted_zip(self):
        path = raw_zip(self.tmp / "e.zip", [zip_member("s.txt", b"secret data", password="pw"), zip_member("p.txt", b"open")])
        archive = reader.open_archive(path)
        secret, plain = archive.entries
        self.assertTrue(secret.encrypted and archive.encrypted)
        self.assertFalse(archive.writable)
        self.assertEqual(read_all(archive, plain), b"open")
        with self.assertRaises(PasswordRequired):
            archive.open_entry(secret)
        with self.assertRaises(WrongPassword):
            read_all(archive, secret, "nope")
        self.assertEqual(read_all(archive, secret, "pw"), b"secret data")
        with self.assertRaises(PasswordRequired):
            archive.check_password(None)
        with self.assertRaises(WrongPassword):
            archive.check_password("nope")
        archive.check_password("pw")
        archive.close()

    def test_aes_entries_need_7z(self):
        aes_extra = b"\x01\x99\x07\x00\x02\x00AE\x03\x08\x00"
        path = raw_zip(self.tmp / "aes.zip", [zip_member("s.txt", b"0" * 40, method=99, password="pw", extra=aes_extra)])
        old = sevenzip.PROGRAMS
        sevenzip.PROGRAMS = ()
        try:
            archive = reader.open_archive(path)
            entry = archive.entries[0]
            self.assertEqual(entry.method, "AES")
            with self.assertRaises(UnsupportedFormat) as caught:
                archive.open_entry(entry, "pw")
            self.assertIn("needs the 7zip package", str(caught.exception))
            archive.close()
        finally:
            sevenzip.PROGRAMS = old

    def test_test_finds_the_broken_entry(self):
        path = raw_zip(self.tmp / "t.zip", [zip_member("good", b"g" * 5000), zip_member("bad", b"b" * 5000, crc=1),
                                            zip_member("good2", b"h" * 100)])
        archive = reader.open_archive(path)
        seen = []
        issues = archive.test(progress=lambda done, entry: seen.append((done, entry.path)))
        archive.close()
        self.assertEqual([issue.path for issue in issues], ["bad"])
        self.assertEqual(seen[-1], (5100, "good2"))          # the bytes of the broken entry do not count

    def test_listing_two_hundred_thousand_entries(self):
        members = [zip_member(f"d{index % 300}/f{index}.txt", b"", method=0) for index in range(200_000)]
        path = raw_zip(self.tmp / "huge.zip", members)
        started = time.perf_counter()
        archive = reader.open_archive(path)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(archive.entries), 200_000)
        self.assertEqual(len(archive.listing.folder("")), 300)
        self.assertLess(elapsed, 30.0)
        archive.close()

    def test_listing_can_be_cancelled(self):
        path = raw_zip(self.tmp / "c.zip", [zip_member(f"f{index}", b"", method=0) for index in range(10)])
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            reader.open_archive(path, cancel=cancel)


class TarReadTest(support.EngineCase):
    def test_listing_fields(self):
        name = b"caf\xe9.txt".decode("utf-8", "surrogateescape")
        path = raw_tar(self.tmp / "l.tar.xz", [
            tar_dir("d", mode=0o750), tar_member("d/a.txt", b"hello", mode=0o4755, mtime=1_650_000_000),
            tar_link("d/link", "a.txt"), tar_link("d/hard", "d/a.txt", hard=True),
            tar_member("dev", None, kind=tarfile.CHRTYPE), tar_member("pipe", None, kind=tarfile.FIFOTYPE),
            tar_member(name, b"latin")], mode="w:xz")
        archive = reader.open_archive(path)
        self.assertEqual(archive.format.key, "tar.xz")
        kinds = {entry.path: entry.kind for entry in archive.entries}
        self.assertEqual(kinds, {"d": "dir", "d/a.txt": "file", "d/link": "symlink", "d/hard": "hardlink", "dev": "device",
                                 "pipe": "fifo", name: "file"})
        entry = archive.entries[1]
        self.assertEqual((entry.size, entry.mode, entry.mtime), (5, 0o4755, 1_650_000_000.0))
        self.assertEqual(archive.entries[2].link, "a.txt")
        self.assertEqual(read_all(archive, entry), b"hello")
        self.assertEqual(read_all(archive, archive.entries[-1]), b"latin")
        self.assertEqual([found.path for found, _reader in archive.stream({1, 6})], ["d/a.txt", name])
        archive.close()

    def test_stream_gives_every_entry_once_in_order(self):
        members = [tar_member(f"f{index}", bytes([index]) * index) for index in range(1, 40)]
        archive = reader.open_archive(raw_tar(self.tmp / "s.tar.bz2", members, mode="w:bz2"))
        seen = []
        for entry, handle in archive.stream():
            seen.append((entry.path, handle.read()))
            handle.close()
        self.assertEqual(seen, [(f"f{index}", bytes([index]) * index) for index in range(1, 40)])
        archive.close()

    def test_reading_while_another_reader_is_open_waits_its_turn(self):
        archive = reader.open_archive(raw_tar(self.tmp / "t.tar", [tar_member("a", b"A" * 100), tar_member("b", b"B" * 100)]))
        first = archive.open_entry(archive.entries[0])
        result = []
        worker = threading.Thread(target=lambda: result.append(read_all(archive, archive.entries[1])))
        worker.start()
        time.sleep(0.05)
        self.assertEqual(result, [])                    # the second reader waits for the first
        self.assertEqual(first.read(), b"A" * 100)
        first.close()
        worker.join(5)
        self.assertEqual(result, [b"B" * 100])
        archive.close()

    def test_damaged_archive_shows_what_can_be_read(self):
        path = raw_tar(self.tmp / "d.tar", [tar_member("one", b"1" * 600), tar_member("two", b"2" * 600)])
        raw = bytearray(path.read_bytes())
        raw[1024 + 512 + 100:1024 + 512 + 140] = b"\xff" * 40          # the header of the second member
        path.write_bytes(raw)
        archive = reader.open_archive(path)
        self.assertEqual([entry.path for entry in archive.entries], ["one"])
        self.assertIn("damaged", archive.warning)
        self.assertFalse(archive.writable)
        archive.close()

    def test_garbage_gzip(self):
        (self.tmp / "g.tar.gz").write_bytes(b"\x1f\x8b\x08\x00" + os.urandom(200))
        with self.assertRaises(ArchiveError):
            reader.open_archive(self.tmp / "g.tar.gz")

    def test_changed_file_is_noticed(self):
        path = raw_tar(self.tmp / "c.tar", [tar_member("a", b"A" * 100)])
        archive = reader.open_archive(path)
        raw_tar(path, [tar_member("other", b"B" * 50)])
        with self.assertRaises(ArchiveError):
            for _entry, handle in archive.stream():
                if handle is not None:
                    handle.close()
        archive.close()

    def test_listing_progress_and_cancel(self):
        path = raw_tar(self.tmp / "p.tar", [tar_member(f"f{index}", b"") for index in range(2000)])
        counts = []
        archive = reader.open_archive(path, progress=counts.append)
        self.assertEqual(len(archive.entries), 2000)
        self.assertGreaterEqual(len(counts), 3)
        archive.close()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            reader.open_archive(path, cancel=cancel)


class StreamReadTest(support.EngineCase):
    def test_single_file(self):
        path = self.tmp / "notes.txt.gz"
        with gzip.GzipFile(path, "wb", mtime=1_600_000_000) as handle:
            handle.write(b"text " * 100)
        archive = reader.open_archive(path)
        (entry,) = archive.entries
        self.assertEqual((entry.path, entry.size, entry.packed, entry.mtime), ("notes.txt", -1, os.path.getsize(path),
                                                                               1_600_000_000.0))
        self.assertEqual(read_all(archive, entry), b"text " * 100)
        self.assertFalse(archive.writable)
        self.assertEqual(archive.test(), [])
        archive.close()

    def test_damaged_stream_is_reported_by_test(self):
        path = self.tmp / "x.bin.xz"
        with lzma.open(path, "wb") as handle:
            handle.write(os.urandom(50_000))
        raw = bytearray(path.read_bytes())
        raw[2000:2100] = bytes(100)
        path.write_bytes(raw)
        archive = reader.open_archive(path)
        issues = archive.test()
        archive.close()
        self.assertEqual(len(issues), 1)


if __name__ == "__main__":
    unittest.main()
