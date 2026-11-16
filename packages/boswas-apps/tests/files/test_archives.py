"""ZIP creation; ZIP and tar extraction with archives that try to escape."""

import io
import os
import stat
import tarfile
import unittest
import warnings
import zipfile

from support import TempCase, tree

from boswas_apps.files import archives
from boswas_apps.files.archives import Compress, Extract
from boswas_apps.files.fileops import Reporter


class Cancel(Reporter):
    def __init__(self, after):
        self.after = after
        self.polls = 0

    def cancelled(self):
        self.polls += 1
        return self.polls > self.after


class NamesTest(unittest.TestCase):
    def test_is_archive_and_stem(self):
        for name in ("a.zip", "A.ZIP", "b.tar", "c.tar.gz", "d.tgz", "e.tar.bz2", "f.tar.xz", "g.txz", "h.tbz2"):
            self.assertTrue(archives.is_archive(name), name)
        for name in ("a.zip.txt", "zip", "b.gz", "c.rar", "d.7z", ""):
            self.assertFalse(archives.is_archive(name), name)
        self.assertEqual(archives.archive_stem("photos.tar.gz"), "photos")
        self.assertEqual(archives.archive_stem("My Files.ZIP"), "My Files")
        self.assertEqual(archives.archive_stem(".zip"), ".zip")

    def test_safe_parts(self):
        self.assertEqual(archives.safe_parts("a/b/c.txt"), ["a", "b", "c.txt"])
        self.assertEqual(archives.safe_parts("/etc/passwd"), ["etc", "passwd"])
        self.assertEqual(archives.safe_parts("./a//b/"), ["a", "b"])
        self.assertEqual(archives.safe_parts("C:\\Windows\\x"), ["Windows", "x"])
        self.assertEqual(archives.safe_parts("..."), ["..."])
        for bad in ("../x", "a/../../x", "a/..", "..", "..\\x", "", "/", ".", "a\0b", "x" * 256, "C:"):
            self.assertIsNone(archives.safe_parts(bad), bad)

    def test_safe_link(self):
        self.assertTrue(archives.safe_link(["a", "link"], "file"))
        self.assertTrue(archives.safe_link(["a", "link"], "../other"))
        self.assertTrue(archives.safe_link(["a", "b", "link"], "../../top"))
        self.assertTrue(archives.safe_link(["link"], "./x/../y"))
        for parts, target in ((["link"], ".."), (["link"], "../x"), (["a", "link"], "../../x"), (["link"], "/etc"),
                              (["link"], ""), (["link"], "a/../../x"), (["link"], "x\0"), (["link"], "a/" * 3000)):
            self.assertFalse(archives.safe_link(parts, target), (parts, target))


class CompressTest(TempCase):
    def test_compress_selection(self):
        self.write("w/doc.txt", b"hello" * 1000, mode=0o640)
        self.write("w/dir/inner.bin", os.urandom(2000))
        self.mkdir("w/dir/empty")
        os.symlink("inner.bin", self.path("w/dir/link"))
        self.write("w/tool.sh", b"#!/bin/sh\n", mode=0o755)
        operation = Compress([self.path("w/doc.txt"), self.path("w/dir"), self.path("w/tool.sh")])
        self.assertEqual(operation.archive, self.path("w/w.zip"))
        result = operation.run()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.affected, [self.path("w/w.zip")])
        with zipfile.ZipFile(operation.archive) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(sorted(archive.namelist()),
                             ["dir/", "dir/empty/", "dir/inner.bin", "dir/link", "doc.txt", "tool.sh"])
            self.assertEqual(archive.read("doc.txt"), b"hello" * 1000)
            self.assertLess(archive.getinfo("doc.txt").compress_size, 500)
            self.assertEqual(stat.S_IMODE(archive.getinfo("tool.sh").external_attr >> 16), 0o755)
            self.assertTrue(stat.S_ISLNK(archive.getinfo("dir/link").external_attr >> 16))
        # and it comes back the same
        extracted = Extract(operation.archive, self.mkdir("out")).run()
        self.assertTrue(extracted.ok, extracted.errors)
        self.assertEqual(tree(self.path("out/w")), {"doc.txt": b"hello" * 1000, "tool.sh": b"#!/bin/sh\n",
                                                    "dir": {"inner.bin": self.read(self.path("w/dir/inner.bin")),
                                                            "empty": {}, "link": ("link", "inner.bin")}})
        self.assertEqual(stat.S_IMODE(os.stat(self.path("out/w/tool.sh")).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(self.path("out/w/doc.txt")).st_mode), 0o640)

    def test_names(self):
        self.write("w/report.txt")
        self.mkdir("w/folder")
        self.assertEqual(Compress([self.path("w/report.txt")]).archive, self.path("w/report.zip"))
        self.assertEqual(Compress([self.path("w/folder")]).archive, self.path("w/folder.zip"))
        self.write("w/report.zip")
        self.assertEqual(Compress([self.path("w/report.txt")]).archive, self.path("w/report (2).zip"))

    def test_cancel_removes_the_partial_archive(self):
        self.write("w/big", os.urandom(3 * 1024 * 1024))
        result = Compress([self.path("w/big")]).run(Cancel(4))
        self.assertTrue(result.cancelled)
        self.assertEqual(os.listdir(self.path("w")), ["big"])

    def test_unreadable_entry_is_an_error_and_the_rest_is_kept(self):
        self.write("w/good", b"g")
        os.symlink("/nowhere", self.path("w/dangling"))
        os.mkfifo(self.path("w/pipe"))
        operation = Compress([self.path("w/good"), self.path("w/dangling"), self.path("w/pipe")])
        result = operation.run()
        self.assertEqual([path for path, _m in result.errors], [self.path("w/pipe")])
        with zipfile.ZipFile(operation.archive) as archive:
            self.assertEqual(sorted(archive.namelist()), ["dangling", "good"])

    def test_hostile_names(self):
        names = ["new\nline", "-rf", "é" * 100, " lead", "a\\b"]
        for name in names:
            self.write(os.path.join("w/h", name), name.encode())
        operation = Compress([self.path("w/h")])
        self.assertTrue(operation.run().ok)
        with zipfile.ZipFile(operation.archive) as archive:
            self.assertEqual(sorted(archive.namelist()), sorted(["h/"] + [f"h/{name}" for name in names]))


class ExtractZipTest(TempCase):
    def make_zip(self, members, name="evil.zip"):
        path = self.path(name)
        with zipfile.ZipFile(path, "w") as archive:
            for member, content in members:
                if isinstance(member, str):
                    archive.writestr(member, content)
                else:
                    archive.writestr(member, content)
        return path

    def link_member(self, name, target):
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        return info, target

    def test_path_traversal_is_refused(self):
        outside = self.write("outside.txt", b"original")
        archive = self.make_zip([("good.txt", b"ok"), ("../outside.txt", b"evil"), ("a/../../outside.txt", b"evil"),
                                 ("/abs/file", b"abs"), ("..\\outside.txt", b"evil"), ("dir/", b""),
                                 ("dir/ok.txt", b"fine")])
        destination = self.mkdir("out")
        result = Extract(archive, destination).run()
        self.assertEqual(len(result.errors), 3)
        self.assertEqual(self.read(outside), b"original")
        self.assertEqual(tree(destination), {"evil": {"good.txt": b"ok", "abs": {"file": b"abs"},
                                                      "dir": {"ok.txt": b"fine"}}})
        self.assertEqual(sorted(os.listdir(self.tmp)), ["evil.zip", "home", "out", "outside.txt"])

    def test_links_that_leave_the_folder_are_refused(self):
        outside = self.mkdir("outside")
        archive = self.make_zip([self.link_member("up", "../../outside"), self.link_member("abs", outside),
                                 self.link_member("fine", "real.txt"), ("real.txt", b"r"),
                                 ("up/planted.txt", b"evil"), ("abs/planted.txt", b"evil")])
        destination = self.mkdir("out")
        result = Extract(archive, destination).run()
        self.assertEqual(os.listdir(outside), [])
        folder = self.path("out/evil")
        self.assertEqual(os.readlink(os.path.join(folder, "fine")), "real.txt")
        self.assertFalse(os.path.islink(os.path.join(folder, "up")))
        self.assertFalse(os.path.islink(os.path.join(folder, "abs")))
        self.assertEqual(len(result.errors), 2)

    def test_writing_through_an_extracted_link_is_refused(self):
        outside = self.mkdir("out/target")           # inside the destination, outside the new folder
        archive = self.make_zip([self.link_member("sub/jump", "../.."), ("sub/jump/target/planted", b"evil"),
                                 self.link_member("inner", "sub"), ("inner/ok.txt", b"ok")])
        result = Extract(archive, self.path("out")).run()
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(self.read(self.path("out/evil/sub/ok.txt")), b"ok")       # a link that stays inside works
        self.assertTrue(result.errors)

    def test_modes_lose_special_bits(self):
        info = zipfile.ZipInfo("suid")
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o6755) << 16
        plain = zipfile.ZipInfo("dos")
        plain.create_system = 0
        archive = self.make_zip([(info, b"x"), (plain, b"y")])
        Extract(archive, self.mkdir("out")).run()
        self.assertEqual(stat.S_IMODE(os.stat(self.path("out/evil/suid")).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(self.path("out/evil/dos")).st_mode), 0o644)

    def test_new_folder_never_overwrites(self):
        archive = self.make_zip([("a.txt", b"new")], "pack.zip")
        self.write("pack/a.txt", b"existing")
        result = Extract(archive).run()
        self.assertEqual(result.affected, [self.path("pack (2)")])
        self.assertEqual(self.read(self.path("pack/a.txt")), b"existing")
        self.assertEqual(result.undo.paths, [self.path("pack (2)")])

    def test_damaged_and_encrypted_archives(self):
        broken = self.write("broken.zip", b"PK\x03\x04 this is not a zip")
        result = Extract(broken).run()
        self.assertEqual(len(result.errors), 1)
        self.assertFalse(os.path.exists(self.path("broken")))
        good = self.make_zip([("a", b"x" * 100000)], "trunc.zip")
        with open(good, "r+b") as handle:
            handle.truncate(50)
        self.assertEqual(len(Extract(good).run().errors), 1)
        encrypted = self.make_zip([("secret", b"x")], "enc.zip")
        data = bytearray(self.read(encrypted))
        for magic, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            position = data.find(magic)
            data[position + offset] |= 1
        with open(encrypted, "wb") as handle:
            handle.write(data)
        result = Extract(encrypted).run()
        self.assertIn("password", result.errors[0][1])
        self.assertEqual(len(Extract(self.write("notes.txt")).run().errors), 1)

    def test_cancel_removes_what_was_extracted(self):
        archive = self.make_zip([(f"f{number}", os.urandom(200000)) for number in range(30)], "many.zip")
        result = Extract(archive, self.mkdir("out")).run(Cancel(12))
        self.assertTrue(result.cancelled)
        self.assertEqual(os.listdir(self.path("out")), [])

    def test_duplicate_members_and_file_then_folder(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")             # zipfile warns about the duplicate name it is asked to write
            archive = self.make_zip([("a", b"first"), ("a", b"second"), ("b", b"file"), ("b/c", b"under a file")])
        result = Extract(archive, self.mkdir("out")).run()
        self.assertEqual(self.read(self.path("out/evil/a")), b"second")
        self.assertEqual(self.read(self.path("out/evil/b")), b"file")
        self.assertEqual(len(result.errors), 1)


class ExtractTarTest(TempCase):
    def make_tar(self, members, name="evil.tar", mode="w"):
        path = self.path(name)
        with tarfile.open(path, mode) as archive:
            for info, content in members:
                if isinstance(info, str):
                    info = tarfile.TarInfo(info)
                    info.mode = 0o644
                if content is not None:
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                else:
                    archive.addfile(info)
        return path

    def special(self, name, kind, linkname=""):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = linkname
        info.mode = 0o777
        return info, None

    def test_compressed_tars(self):
        for suffix, mode in ((".tar", "w"), (".tar.gz", "w:gz"), (".tgz", "w:gz"), (".tar.bz2", "w:bz2"),
                             (".tar.xz", "w:xz")):
            folder = tarfile.TarInfo("pack/sub")
            folder.type = tarfile.DIRTYPE
            folder.mode = 0o750
            folder.mtime = 1_200_000_000
            tool = tarfile.TarInfo("pack/sub/tool")
            tool.mode = 0o4755
            tool.mtime = 1_100_000_000
            tool.uid = tool.gid = 0
            archive = self.make_tar([(folder, None), (tool, b"#!/bin/sh\n"), ("pack/readme", b"read me"),
                                     self.special("pack/link", tarfile.SYMTYPE, "readme")], "a" + suffix, mode)
            result = Extract(archive, self.mkdir("out" + suffix)).run()
            self.assertTrue(result.ok, (suffix, result.errors))
            root = self.path("out" + suffix, "a", "pack")
            self.assertEqual(tree(root), {"sub": {"tool": b"#!/bin/sh\n"}, "readme": b"read me",
                                          "link": ("link", "readme")})
            info = os.stat(os.path.join(root, "sub", "tool"))
            self.assertEqual((stat.S_IMODE(info.st_mode), int(info.st_mtime), info.st_uid),
                             (0o755, 1_100_000_000, os.getuid()))
            info = os.stat(os.path.join(root, "sub"))
            self.assertEqual((stat.S_IMODE(info.st_mode), int(info.st_mtime)), (0o750, 1_200_000_000))

    def test_escapes_are_refused(self):
        outside = self.write("outside.txt", b"original")
        target_dir = self.mkdir("outside-dir")
        archive = self.make_tar([
            ("../outside.txt", b"evil"), ("/tmp/absolute-from-tar", b"abs"), ("ok/../../outside.txt", b"evil"),
            self.special("escape", tarfile.SYMTYPE, "../../outside-dir"), ("escape/planted", b"evil"),
            self.special("abs-link", tarfile.SYMTYPE, target_dir), ("abs-link/planted", b"evil"),
            self.special("hard", tarfile.LNKTYPE, "../outside.txt"),
            self.special("device", tarfile.CHRTYPE), self.special("pipe", tarfile.FIFOTYPE),
            ("good.txt", b"ok"),
        ])
        result = Extract(archive, self.mkdir("out")).run()
        self.assertEqual(self.read(outside), b"original")
        self.assertEqual(os.listdir(target_dir), [])
        self.assertFalse(os.path.exists("/tmp/absolute-from-tar"))
        extracted = tree(self.path("out/evil"))
        self.assertEqual(extracted["good.txt"], b"ok")
        self.assertEqual(extracted["tmp"], {"absolute-from-tar": b"abs"})
        for name in ("hard", "device", "pipe"):
            self.assertNotIn(name, extracted)
        self.assertNotIsInstance(extracted.get("escape"), tuple)
        self.assertNotIsInstance(extracted.get("abs-link"), tuple)
        self.assertGreaterEqual(len(result.errors), 7)

    def test_link_then_write_through_it(self):
        victim = self.mkdir("out/victim")
        archive = self.make_tar([self.special("a/jump", tarfile.SYMTYPE, "../.."), ("a/jump/victim/planted", b"x")])
        result = Extract(archive, self.path("out")).run()
        self.assertEqual(os.listdir(victim), [])
        self.assertTrue(result.errors)

    def test_garbage(self):
        for name, content in (("junk.tar", os.urandom(4096)), ("empty.tar.gz", b""), ("text.tar.xz", b"hello")):
            result = Extract(self.write(name, content)).run()
            self.assertEqual(len(result.errors), 1, name)
            self.assertFalse(os.path.exists(self.path(archives.archive_stem(name))))

    def test_cancel(self):
        archive = self.make_tar([(f"f{number}", os.urandom(100000)) for number in range(40)], "many.tar")
        result = Extract(archive, self.mkdir("out")).run(Cancel(15))
        self.assertTrue(result.cancelled)
        self.assertEqual(os.listdir(self.path("out")), [])


if __name__ == "__main__":
    unittest.main()
