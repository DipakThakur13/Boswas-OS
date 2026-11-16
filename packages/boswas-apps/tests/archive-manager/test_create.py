"""Creating archives and changing them: complete or not at all."""

from __future__ import annotations

import os
import socket
import stat
import tarfile
import threading
import unittest
import zipfile

import support
from boswas_apps.archive_manager import create, extract, reader
from boswas_apps.archive_manager.create import Options
from boswas_apps.archive_manager.errors import ArchiveError
from support import Recorder, raw_tar, raw_zip, tar_dir, tar_link, tar_member, tree, zip_member


class SourceCase(support.EngineCase):
    def setUp(self) -> None:
        super().setUp()
        self.src = self.tmp / "project"
        (self.src / "sub").mkdir(parents=True)
        (self.src / "a.txt").write_bytes(b"alpha\n" * 100)
        (self.src / "sub" / "b.bin").write_bytes(os.urandom(5000))
        (self.src / ".hidden").write_bytes(b"h")
        (self.src / "sub" / ".git").mkdir()
        (self.src / "sub" / ".git" / "config").write_bytes(b"c")
        (self.src / "run.sh").write_bytes(b"#!/bin/sh\n")
        os.chmod(self.src / "run.sh", 0o755)
        os.symlink("a.txt", self.src / "link")
        os.utime(self.src / "a.txt", (1_600_000_000, 1_600_000_000))
        self.out = self.tmp / "out"
        self.out.mkdir()

    def listing(self, path) -> dict[str, tuple]:
        with reader.open_archive(path) as archive:
            return {entry.path: (entry.kind, entry.size) for entry in archive.entries}

    def round_trip(self, path) -> dict[str, str]:
        target = self.tmp / ("x-" + os.path.basename(path))
        with reader.open_archive(path) as archive:
            result = extract.extract(archive, target, policy=extract.REPLACE)
        self.assertTrue(result.clean, (result.skipped, result.failed))
        return tree(target)


class CreateTest(SourceCase):
    def test_every_format_round_trips(self):
        for key in ("zip", "tar", "tar.gz", "tar.xz", "tar.bz2"):
            with self.subTest(format=key):
                path = self.out / ("p" + {"zip": ".zip", "tar": ".tar", "tar.gz": ".tar.gz", "tar.xz": ".tar.xz",
                                          "tar.bz2": ".tar.bz2"}[key])
                result = create.create(path, [str(self.src)], Options(format=key))
                self.assertEqual((result.files, result.folders, result.links), (5, 3, 1))
                self.assertTrue(result.clean)
                self.assertEqual(result.size, os.path.getsize(path))
                with reader.open_archive(path) as archive:
                    self.assertEqual(archive.format.key, key)
                found = self.round_trip(path)
                self.assertEqual(found["project/link"], "link:a.txt")
                self.assertEqual(found["project/sub/b.bin"], "file:5000")
                extracted = self.tmp / ("x-" + path.name) / "project"
                self.assertEqual((extracted / "sub" / "b.bin").read_bytes(), (self.src / "sub" / "b.bin").read_bytes())
                self.assertEqual(stat.S_IMODE(os.stat(extracted / "run.sh").st_mode), 0o755)
                self.assertEqual(int(os.stat(extracted / "a.txt").st_mtime) // 2, 1_600_000_000 // 2)
                self.assertEqual([name for name in os.listdir(self.out) if name.startswith(".bw-")], [])
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_zip_levels(self):
        stored = create.create(self.out / "s.zip", [str(self.src / "a.txt")], Options(format="zip", level=0))
        best = create.create(self.out / "b.zip", [str(self.src / "a.txt")], Options(format="zip", level=9))
        self.assertGreater(stored.size, 600)
        self.assertLess(best.size, 300)
        with zipfile.ZipFile(self.out / "s.zip") as archive:
            self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)
        with zipfile.ZipFile(self.out / "b.zip") as archive:
            self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_DEFLATED)
            self.assertIsNone(archive.testzip())

    def test_follow_symlinks(self):
        os.symlink("sub", self.src / "dirlink")
        os.symlink("..", self.src / "sub" / "loop")
        os.symlink("nowhere", self.src / "broken")
        result = create.create(self.out / "f.tar", [str(self.src)], Options(format="tar", follow_symlinks=True))
        found = self.listing(self.out / "f.tar")
        self.assertEqual(found["project/link"], ("file", 600))
        self.assertEqual(found["project/dirlink/b.bin"], ("file", 5000))
        self.assertEqual(result.links, 0)
        reasons = {issue.path: issue.reason for issue in result.skipped}
        self.assertIn("project/broken", reasons)
        self.assertTrue(any("leads back" in reason for reason in reasons.values()))
        self.assertFalse(any(path.count("/loop/") > 1 for path in found))

    def test_stored_links_are_not_followed(self):
        outside = self.tmp / "private.txt"
        outside.write_bytes(b"do not pack me")
        os.symlink(str(outside), self.src / "abs")
        create.create(self.out / "l.zip", [str(self.src)], Options(format="zip"))
        with reader.open_archive(self.out / "l.zip") as archive:
            entry = next(entry for entry in archive.entries if entry.path == "project/abs")
            self.assertEqual(entry.kind, "symlink")
            self.assertEqual(archive.link_target(entry), str(outside))
        self.assertNotIn(b"do not pack me", (self.out / "l.zip").read_bytes())

    def test_exclude_hidden(self):
        create.create(self.out / "h.zip", [str(self.src)], Options(format="zip", exclude_hidden=True))
        found = self.listing(self.out / "h.zip")
        self.assertNotIn("project/.hidden", found)
        self.assertFalse(any("/.git" in path for path in found))
        self.assertIn("project/a.txt", found)
        # What the user chose is packed even when its own name starts with a dot.
        create.create(self.out / "h2.zip", [str(self.src / ".hidden")], Options(format="zip", exclude_hidden=True))
        self.assertEqual(list(self.listing(self.out / "h2.zip")), [".hidden"])

    def test_special_files_are_skipped(self):
        os.mkfifo(self.src / "pipe")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.src / "sock"))
        try:
            result = create.create(self.out / "s.tar.gz", [str(self.src)], Options(format="tar.gz"))
        finally:
            listener.close()
        self.assertEqual(sorted(issue.path for issue in result.skipped), ["project/pipe", "project/sock"])
        self.assertNotIn("project/pipe", self.listing(self.out / "s.tar.gz"))

    def test_same_names_from_different_folders(self):
        other = self.tmp / "other"
        other.mkdir()
        (other / "a.txt").write_bytes(b"other")
        create.create(self.out / "n.zip", [str(self.src / "a.txt"), str(other / "a.txt")], Options(format="zip"))
        self.assertEqual(self.listing(self.out / "n.zip"), {"a.txt": ("file", 600), "a (2).txt": ("file", 5)})

    def test_the_archive_does_not_pack_itself(self):
        target = self.src / "self.tar"
        target.write_bytes(b"old archive")
        result = create.create(target, [str(self.src)], Options(format="tar"))
        found = self.listing(target)
        self.assertNotIn("project/self.tar", found)
        self.assertFalse(any(".bw-" in path for path in found))
        self.assertEqual(result.files, 5)

    def test_tar_holds_no_owner(self):
        create.create(self.out / "o.tar", [str(self.src / "a.txt")], Options(format="tar"))
        with tarfile.open(self.out / "o.tar") as archive:
            member = archive.getmembers()[0]
        self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, "", ""))

    def test_names_that_are_not_text(self):
        odd = os.fsencode(self.src) + b"/caf\xe9.txt"
        with open(odd, "wb") as handle:
            handle.write(b"x")
        create.create(self.out / "odd.tar", [str(self.src)], Options(format="tar"))
        with reader.open_archive(self.out / "odd.tar") as archive:
            self.assertIn(b"project/caf\xe9.txt", [os.fsencode(entry.name) for entry in archive.entries])
        result = create.create(self.out / "odd.zip", [str(self.src)], Options(format="zip"))
        self.assertTrue(any("not valid text" in note for note in result.notes))
        self.assertIn("project/caf�.txt", self.listing(self.out / "odd.zip"))

    def test_cancel_leaves_the_old_archive(self):
        target = self.out / "keep.zip"
        target.write_bytes(b"the old archive")
        (self.src / "big.bin").write_bytes(os.urandom(6 * 1024 * 1024))
        cancel = threading.Event()
        handler = Recorder()
        handler.on_progress = lambda info: cancel.set() if info.bytes_done > 2 * 1024 * 1024 else None
        result = create.create(target, [str(self.src)], Options(format="zip", level=0), handler, cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(target.read_bytes(), b"the old archive")
        self.assertEqual(os.listdir(self.out), ["keep.zip"])

    def test_failure_leaves_the_old_archive(self):
        target = self.out / "keep.tar.gz"
        target.write_bytes(b"the old archive")
        with self.assertRaises(ArchiveError):
            create.create(target, [str(self.tmp / "does-not-exist")], Options(format="tar.gz"))
        self.assertEqual(target.read_bytes(), b"the old archive")
        self.assertEqual(os.listdir(self.out), ["keep.tar.gz"])

    def test_unknown_format(self):
        with self.assertRaises(ArchiveError):
            create.create(self.out / "x.7z", [str(self.src)], Options(format="7z"))
        self.assertEqual(os.listdir(self.out), [])

    def test_output_folder_missing(self):
        with self.assertRaises(ArchiveError):
            create.create(self.tmp / "no" / "x.zip", [str(self.src)], Options(format="zip"))

    def test_progress(self):
        handler = Recorder()
        create.create(self.out / "p.tar", [str(self.src)], Options(format="tar"), handler)
        last = handler.progress_calls[-1]
        self.assertEqual((last.bytes_done, last.bytes_total, last.entries_done, last.entries_total), (5612, 5612, 9, 9))

    @unittest.skipIf(os.geteuid() == 0, "root reads everything")
    def test_unreadable_file_is_reported_and_the_rest_is_packed(self):
        os.chmod(self.src / "a.txt", 0)
        result = create.create(self.out / "u.zip", [str(self.src)], Options(format="zip"))
        self.assertEqual([issue.path for issue in result.failed], ["project/a.txt"])
        self.assertIn("project/sub/b.bin", self.listing(self.out / "u.zip"))


class ModifyTest(SourceCase):
    def _zip(self):
        return raw_zip(self.out / "m.zip", [
            zip_member("docs/", b"", is_dir=True, method=0), zip_member("docs/a.txt", b"old a", mode=stat.S_IFREG | 0o640),
            zip_member("docs/keep.txt", b"keep me" * 100), zip_member("résumé.txt".encode("cp437"), b"legacy name"),
            zip_member("top.txt", b"top", method=0)], comment=b"my comment")

    def _tar(self, mode="w:gz", name="m.tar.gz"):
        return raw_tar(self.out / name, [
            tar_dir("docs", mode=0o750), tar_member("docs/a.txt", b"old a", mode=0o640, mtime=1_500_000_000),
            tar_member("docs/keep.txt", b"keep me" * 100), tar_link("docs/link", "a.txt"),
            tar_member("top.txt", b"top")], mode=mode)

    def test_delete_from_zip(self):
        path = self._zip()
        os.chmod(path, 0o600)
        with reader.open_archive(path) as archive:
            doomed = [entry for entry in archive.entries if entry.path in ("docs/a.txt", "top.txt")]
            result = create.delete(archive, doomed)
        self.assertEqual(result.removed, 2)
        with reader.open_archive(path) as archive:
            self.assertEqual([entry.path for entry in archive.entries], ["docs", "docs/keep.txt", "résumé.txt"])
            self.assertEqual(archive.comment, "my comment")
            self.assertEqual(archive.test(), [])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(os.listdir(self.out), ["m.zip"])

    def test_delete_a_folder_from_tar(self):
        path = self._tar()
        with reader.open_archive(path) as archive:
            folder = next(entry for entry in archive.entries if entry.path == "docs")
            create.delete(archive, archive.listing.descendants(folder))
        with reader.open_archive(path) as archive:
            self.assertEqual(archive.format.key, "tar.gz")
            self.assertEqual([entry.path for entry in archive.entries], ["top.txt"])

    def test_delete_everything(self):
        for path in (self._zip(), self._tar("w", "m.tar"), self._tar()):
            with reader.open_archive(path) as archive:
                create.delete(archive, list(archive.entries))
            with reader.open_archive(path) as archive:
                self.assertEqual(archive.entries, [])

    def test_add_to_zip_replaces_same_paths(self):
        path = self._zip()
        with reader.open_archive(path) as archive:
            self.assertEqual(create.replaced_by(archive, [str(self.src / "a.txt")], "docs"), ["docs/a.txt"])
            self.assertEqual(create.replaced_by(archive, [str(self.src / "a.txt")], ""), [])
            result = create.add(archive, [str(self.src / "a.txt"), str(self.src / "sub")], "docs")
        self.assertEqual(result.removed, 1)
        found = self.listing(path)
        self.assertEqual(found["docs/a.txt"], ("file", 600))
        self.assertEqual(found["docs/sub/b.bin"], ("file", 5000))
        self.assertEqual(found["docs/keep.txt"], ("file", 700))
        self.assertEqual(found["résumé.txt"], ("file", 11))
        self.assertEqual(len([name for name in found if name == "docs"]), 1)
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.comment, b"my comment")
            kept = archive.getinfo("docs/keep.txt")
            self.assertEqual(kept.date_time, support.DOS_TIME)

    def test_add_to_tar_keeps_everything_else(self):
        path = self._tar("w:xz", "m.tar.xz")
        with reader.open_archive(path) as archive:
            create.add(archive, [str(self.src / "a.txt")], "docs")
        with tarfile.open(path) as archive:
            members = {member.name: member for member in archive.getmembers()}
            self.assertEqual(sorted(members), ["docs", "docs/a.txt", "docs/keep.txt", "docs/link", "top.txt"])
            self.assertEqual(members["docs"].mode, 0o750)
            self.assertEqual(members["docs/link"].linkname, "a.txt")
            self.assertEqual(archive.extractfile("docs/a.txt").read(), b"alpha\n" * 100)
            self.assertEqual(archive.extractfile("docs/keep.txt").read(), b"keep me" * 100)
        with reader.open_archive(path) as archive:
            self.assertEqual(archive.format.key, "tar.xz")

    def test_add_at_the_root(self):
        path = self._tar("w", "m.tar")
        with reader.open_archive(path) as archive:
            create.add(archive, [str(self.src)], "")
        self.assertIn("project/sub/b.bin", self.listing(path))

    def test_cancel_keeps_the_original(self):
        for path in (self._zip(), self._tar()):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                (self.src / "big.bin").write_bytes(os.urandom(5 * 1024 * 1024))
                cancel = threading.Event()
                handler = Recorder()
                handler.on_progress = lambda info: cancel.set() if info.bytes_done > 1024 * 1024 else None
                with reader.open_archive(path) as archive:
                    result = create.add(archive, [str(self.src / "big.bin")], "", Options(level=0), handler, cancel)
                self.assertTrue(result.cancelled)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual([name for name in os.listdir(self.out) if name.startswith(".bw-")], [])

    def test_failure_keeps_the_original(self):
        path = raw_zip(self.out / "bad.zip", [zip_member("good.txt", b"g" * 100), zip_member("bad.txt", b"b" * 100, crc=5)])
        before = path.read_bytes()
        with reader.open_archive(path) as archive:
            with self.assertRaises(ArchiveError):
                create.delete(archive, [archive.entries[0]])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(os.listdir(self.out), ["bad.zip"])

    def test_what_cannot_be_changed(self):
        encrypted = raw_zip(self.out / "e.zip", [zip_member("s.txt", b"secret", password="pw")])
        before = encrypted.read_bytes()
        with reader.open_archive(encrypted) as archive:
            with self.assertRaises(ArchiveError) as caught:
                create.add(archive, [str(self.src / "a.txt")])
            self.assertIn("password", str(caught.exception))
            with self.assertRaises(ArchiveError):
                create.delete(archive, list(archive.entries))
        self.assertEqual(encrypted.read_bytes(), before)
        import gzip
        with gzip.open(self.out / "f.txt.gz", "wb") as handle:
            handle.write(b"x" * 1000)
        with reader.open_archive(self.out / "f.txt.gz") as archive:
            with self.assertRaises(ArchiveError):
                create.delete(archive, list(archive.entries))
        with reader.open_archive(self._zip()) as archive:
            with self.assertRaises(ArchiveError):
                create.delete(archive, [])
            with self.assertRaises(ArchiveError):
                create.add(archive, [str(self.tmp / "missing")])
        self.assertEqual(sorted(os.listdir(self.out)), ["e.zip", "f.txt.gz", "m.zip"])


if __name__ == "__main__":
    unittest.main()
