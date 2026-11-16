"""The 7z back end: the listing parser, a stand-in program, and the real 7z when it is installed."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
import zlib

import support
from boswas_apps.archive_manager import extract, reader, sevenzip
from boswas_apps.archive_manager.errors import ArchiveError, PasswordRequired, WrongPassword
from support import Recorder, fake_seven_archive, seven_entry, tree

LISTING = """
Path = dir
Size = 0
Packed Size = 0
Modified = 2026-11-15 20:03:15.1335375
Attributes = D drwxr-xr-x
CRC = 
Encrypted = -
Method = 
Block = 

Path = a = b.txt
Size = 6
Packed Size = 789
Modified = 2026-11-15 20:03:15.1335375
Attributes = A -rwsr-xr-x
CRC = 363A3020
Encrypted = +
Method = LZMA2:12k
Block = 0

Path = link
Size = 5
Packed Size = 
Attributes = A lrwxrwxrwx
CRC = C1EBF7BA

Path = win
Size = 3
Attributes = RHSA
Folder = -

Path = windir
Attributes = D
Folder = +

Path = rarlink
Size = 0
Symbolic Link = ../target
"""


class ParserTest(unittest.TestCase):
    def test_fields(self):
        items = sevenzip.parse_listing(LISTING)
        self.assertEqual([item.path for item in items], ["dir", "a = b.txt", "link", "win", "windir", "rarlink"])
        folder, text, link, win, windir, rarlink = items
        self.assertEqual((folder.kind, folder.mode), ("dir", 0o755))
        self.assertEqual((text.kind, text.size, text.packed, text.crc, text.encrypted, text.mode),
                         ("file", 6, 789, 0x363A3020, True, 0o4755))
        self.assertIsNotNone(text.mtime)
        self.assertEqual((link.kind, link.size, link.packed), ("symlink", 5, -1))
        self.assertEqual((win.kind, win.mode), ("file", None))
        self.assertEqual(windir.kind, "dir")
        self.assertEqual((rarlink.kind, rarlink.link), ("symlink", "../target"))

    def test_a_name_cannot_overwrite_fields(self):
        text = "Path = evil\nSize = 99999\nCRC = FFFFFFFF\nPath = second line of the name\nSize = 7\nCRC = 00000001\n"
        (item,) = sevenzip.parse_listing(text)
        self.assertEqual((item.path, item.size, item.crc), ("evil", 99999, 0xFFFFFFFF))

    def test_noise_is_ignored(self):
        text = "\nEnter password (will not be echoed):\nPath = a\nSize = 1\ngarbage line\n\n\nSize = 5\nPath = b\nSize = x\n"
        items = sevenzip.parse_listing(text)
        self.assertEqual([(item.path, item.size) for item in items], [("a", 1), ("b", -1)])

    def test_entry_arguments_cannot_be_switches(self):
        arguments = sevenzip.entry_arguments("/tmp/-o/evil.7z", "-oevil")
        self.assertEqual(arguments[-2:], ["--", "/tmp/-o/evil.7z"])
        self.assertIn("-i!-oevil", arguments)
        self.assertIn("-spd", arguments)
        self.assertIn("-i!@list", sevenzip.entry_arguments("x", "@list"))
        self.assertFalse(any(argument.startswith("-p") for argument in arguments))


class FakeProgramCase(support.EngineCase):
    def setUp(self) -> None:
        super().setUp()
        self._programs = sevenzip.PROGRAMS
        sevenzip.PROGRAMS = (support.fake_7z(self.tmp),)
        self.addCleanup(self._restore_programs)
        self.dest = self.tmp / "jail" / "dest"
        self.dest.parent.mkdir()
        self.log = self.tmp / "calls.log"

    def _restore_programs(self) -> None:
        sevenzip.PROGRAMS = self._programs

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def archive(self, entries, **spec):
        return fake_seven_archive(self.tmp / "t.7z", entries, log=str(self.log), **spec)


class FakeSevenZipTest(FakeProgramCase):
    def entries(self, count=12):
        made = [seven_entry("docs", attributes="D drwxr-xr-x"), seven_entry("docs/empty.txt")]
        made += [seven_entry(f"docs/f{index:02d}.bin", bytes([65 + index]) * (100 + index)) for index in range(count)]
        made.append(seven_entry("docs/link", b"f00.bin", attributes="A lrwxrwxrwx"))
        return made

    def test_listing(self):
        with reader.open_archive(self.archive(self.entries())) as archive:
            self.assertEqual(archive.format.key, "7z")
            self.assertEqual(len(archive.entries), 15)
            kinds = {entry.path: entry.kind for entry in archive.entries}
            self.assertEqual(kinds["docs"], "dir")
            self.assertEqual(kinds["docs/link"], "symlink")
            self.assertEqual(archive.entries[2].crc, zlib.crc32(b"A" * 100))
            self.assertFalse(archive.writable)

    def test_one_entry_is_read_by_its_own_process(self):
        with reader.open_archive(self.archive(self.entries())) as archive:
            entry = archive.entries[4]
            with archive.open_entry(entry) as handle:
                self.assertEqual(handle.read(), b"C" * 102)
                self.assertEqual(handle.read(), b"")
            self.assertEqual(archive.link_target(archive.entries[-1]), "f00.bin")
        last = self.calls()[-1]
        self.assertIn("-i!docs/link", last)
        self.assertTrue(all(not argument.startswith("-p") for call in self.calls() for argument in call))

    def test_extract_all_in_one_pass(self):
        with reader.open_archive(self.archive(self.entries())) as archive:
            result = extract.extract(archive, self.dest, policy=extract.REPLACE)
        self.assertTrue(result.clean, (result.skipped, result.failed))
        found = tree(self.dest)
        self.assertEqual(found["docs/f11.bin"], "file:111")
        self.assertEqual(found["docs/empty.txt"], "file:0")
        self.assertEqual(found["docs/link"], "link:f00.bin")
        self.assertEqual((self.dest / "docs" / "f05.bin").read_bytes(), b"F" * 105)
        extractions = [call for call in self.calls() if call[0] == "x"]
        self.assertEqual(len(extractions), 1)                      # one 7z for the whole archive
        self.assertFalse(any(argument.startswith("-i!") for argument in extractions[0]))

    def test_few_entries_are_read_one_by_one(self):
        with reader.open_archive(self.archive(self.entries(3))) as archive:
            result = extract.extract(archive, self.dest, policy=extract.REPLACE)
        self.assertTrue(result.clean)
        self.assertEqual(len([call for call in self.calls() if call[0] == "x"]), 4)     # three files and the link

    def test_selection_from_the_pipe_stays_aligned(self):
        with reader.open_archive(self.archive(self.entries(30))) as archive:
            chosen = [entry for entry in archive.entries if entry.path.endswith(("f03.bin", "f17.bin", "f29.bin"))]
            chosen += [entry for entry in archive.entries if entry.path.endswith(tuple(f"f{i:02d}.bin" for i in range(5, 12)))]
            result = extract.extract(archive, self.dest, entries=chosen, policy=extract.REPLACE)
        self.assertTrue(result.clean)
        self.assertEqual(result.files, 10)
        self.assertEqual((self.dest / "docs" / "f17.bin").read_bytes(), bytes([65 + 17]) * 117)
        self.assertEqual((self.dest / "docs" / "f29.bin").read_bytes(), bytes([65 + 29]) * 129)

    def test_wrong_data_in_the_pipe_is_caught_and_the_rest_is_read_one_by_one(self):
        entries = self.entries()
        # 7z writes the right number of bytes but the wrong ones for f03 when it writes everything.
        entries[5]["stream"] = seven_entry("x", b"!" * 103)["data"]
        with reader.open_archive(self.archive(entries)) as archive:
            result = extract.extract(archive, self.dest, policy=extract.REPLACE)
        self.assertEqual([issue.path for issue in result.failed], ["docs/f03.bin"])
        self.assertIn("checksum", result.failed[0].reason)
        self.assertFalse((self.dest / "docs" / "f03.bin").exists())
        for index in (0, 2, 4, 11):
            self.assertEqual((self.dest / "docs" / f"f{index:02d}.bin").read_bytes(), bytes([65 + index]) * (100 + index))
        self.assertGreater(len([call for call in self.calls() if call[0] == "x"]), 5)

    def test_short_pipe_fails_the_entry_not_the_run(self):
        entries = self.entries()
        entries[4]["stream"] = seven_entry("x", b"C" * 10)["data"]          # f02 is cut short: everything after shifts
        with reader.open_archive(self.archive(entries)) as archive:
            result = extract.extract(archive, self.dest, policy=extract.REPLACE)
        self.assertEqual([issue.path for issue in result.failed], ["docs/f02.bin"])
        self.assertEqual((self.dest / "docs" / "f11.bin").read_bytes(), b"L" * 111)

    def test_listing_that_lies_about_a_size(self):
        entries = [seven_entry("small.bin", b"x" * 1000, size=10), seven_entry("ok.txt", b"fine")]
        with reader.open_archive(self.archive(entries)) as archive:
            result = extract.extract(archive, self.dest, policy=extract.REPLACE)
        self.assertEqual(tree(self.dest), {"ok.txt": "file:4"})
        self.assertEqual([issue.path for issue in result.failed], ["small.bin"])

    def test_hostile_names_and_links(self):
        outside = self.dest.parent / "outside"
        outside.mkdir()
        entries = [seven_entry("../evil.txt", b"pwned"), seven_entry("/abs/x.txt", b"inside"),
                   seven_entry("door", b"../outside", attributes="A lrwxrwxrwx"), seven_entry("door/planted.txt", b"pwned"),
                   seven_entry("rarlink", attributes="A lrwxrwxrwx", **{"Symbolic Link": "/etc/passwd"}),
                   seven_entry("dev", attributes="A crw-rw-rw-"), seven_entry("good", b"abs/x.txt", attributes="A lrwxrwxrwx")]
        with reader.open_archive(self.archive(entries)) as archive:
            result = extract.extract(archive, self.dest, policy=extract.REPLACE)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(sorted(os.listdir(self.dest.parent)), ["dest", "outside"])
        self.assertEqual(tree(self.dest), {"abs": "dir", "abs/x.txt": "file:6", "door": "dir", "door/planted.txt": "file:5",
                                           "good": "link:abs/x.txt"})
        self.assertEqual(sorted(issue.path for issue in result.skipped), ["../evil.txt", "dev", "door", "rarlink"])

    def test_password_for_the_content(self):
        entries = [seven_entry("secret.txt", b"classified"), seven_entry("empty")]
        path = self.archive(entries, password="hunter2")
        with reader.open_archive(path) as archive:
            self.assertTrue(archive.encrypted)
            with self.assertRaises(PasswordRequired):
                archive.open_entry(archive.entries[0])
            with self.assertRaises(WrongPassword):
                with archive.open_entry(archive.entries[0], "nope") as handle:
                    handle.read()
            handler = Recorder(passwords=["nope", "hunter2"])
            result = extract.extract(archive, self.dest, handler=handler, policy=extract.REPLACE)
        self.assertEqual(handler.password_calls, [False, True])
        self.assertEqual((self.dest / "secret.txt").read_bytes(), b"classified")
        self.assertTrue(result.clean)

    def test_password_for_the_list_of_names(self):
        path = self.archive([seven_entry("secret.txt", b"classified")], password="hunter2", encrypted_names=True)
        with self.assertRaises(PasswordRequired) as caught:
            reader.open_archive(path)
        self.assertNotIsInstance(caught.exception, WrongPassword)
        with self.assertRaises(WrongPassword):
            reader.open_archive(path, password="nope")
        with reader.open_archive(path, password="hunter2") as archive:
            self.assertTrue(archive.encrypted)
            result = extract.extract(archive, self.dest, policy=extract.REPLACE)       # remembers the password
        self.assertEqual((self.dest / "secret.txt").read_bytes(), b"classified")
        self.assertTrue(result.clean)

    def test_password_with_a_line_break_is_refused(self):
        path = self.archive([seven_entry("s", b"x")], password="a")
        with reader.open_archive(path) as archive:
            with self.assertRaises(WrongPassword):
                archive.open_entry(archive.entries[0], "a\nb")

    def test_test_command(self):
        with reader.open_archive(self.archive(self.entries())) as archive:
            self.assertEqual(archive.test(), [])
        with reader.open_archive(self.archive(self.entries(), broken="docs/f01.bin")) as archive:
            issues = archive.test()
        self.assertEqual(len(issues), 1)
        self.assertIn("damaged", issues[0].reason)

    def test_program_failure_is_an_archive_error(self):
        path = self.tmp / "broken.7z"
        path.write_bytes(support.SEVEN_MAGIC + b"this is not json")
        with self.assertRaises(ArchiveError):
            reader.open_archive(path)

    def test_aes_zip_entry_goes_through_7z(self):
        # The same file is a ZIP for zipfile (an AES entry it cannot read) and readable by the stand-in.
        aes_extra = b"\x01\x99\x07\x00\x02\x00AE\x03\x08\x00"
        zip_path = support.raw_zip(self.tmp / "aes.zip", [support.zip_member("s.txt", b"0" * 40, method=99, password="pw",
                                                                             extra=aes_extra, claim_size=9)])
        with reader.open_archive(zip_path) as archive:
            entry = archive.entries[0]
            self.assertEqual((entry.method, entry.encrypted), ("AES", True))
            with self.assertRaises(PasswordRequired):
                archive.open_entry(entry)
            # Swap the file for one the stand-in understands, keeping the open ZipFile: only 7z reads it now.
            fake_seven_archive(zip_path, [seven_entry("s.txt", b"plaintext", crc="")], password="pw", log=str(self.log))
            with archive.open_entry(entry, "pw") as handle:
                self.assertEqual(handle.read(), b"plaintext")
            with self.assertRaises(WrongPassword):
                with archive.open_entry(entry, "bad") as handle:
                    handle.read()


REAL = sevenzip.find_program()


@unittest.skipUnless(REAL, "the 7zip package is not installed")
class RealSevenZipTest(support.EngineCase):
    """Against the real program: what the stand-in assumes about 7z is true."""

    def setUp(self) -> None:
        super().setUp()
        self.src = self.tmp / "src"
        (self.src / "dir" / "sub").mkdir(parents=True)
        (self.src / "a.txt").write_bytes(b"hello\n")
        (self.src / "dir" / "b.txt").write_bytes(b"second file\n")
        (self.src / "dir" / "sub" / "c.bin").write_bytes(os.urandom(70_000))
        (self.src / "empty").write_bytes(b"")
        (self.src / "we*ird[1] @name.txt").write_bytes(b"star\n")
        (self.src / "-dash.txt").write_bytes(b"dash\n")
        os.symlink("a.txt", self.src / "link")
        os.symlink("/etc/passwd", self.src / "abs")
        for index in range(12):
            (self.src / "dir" / f"n{index}.txt").write_bytes(f"file {index}\n".encode() * (index + 1))
        self.dest = self.tmp / "jail" / "dest"
        self.dest.parent.mkdir()

    def pack(self, name, *switches) -> str:
        target = str(self.tmp / name)
        subprocess.run([REAL, "a", "-bso0", "-bsp0", "-snl", *switches, "--", target, "."], cwd=self.src, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        return target

    def test_list_and_extract_everything(self):
        with reader.open_archive(self.pack("plain.7z")) as archive:
            kinds = {entry.path: entry.kind for entry in archive.entries}
            self.assertEqual(kinds["dir"], "dir")
            self.assertEqual(kinds["link"], "symlink")
            self.assertEqual(kinds["a.txt"], "file")
            result = extract.extract(archive, self.dest, policy=extract.REPLACE)
        found = tree(self.dest)
        self.assertEqual(found["link"], "link:a.txt")
        self.assertNotIn("abs", found)                                 # the link to /etc/passwd is refused
        self.assertEqual([issue.path for issue in result.skipped], ["abs"])
        self.assertFalse(result.failed)
        for relative in ("a.txt", "dir/b.txt", "dir/sub/c.bin", "empty", "we*ird[1] @name.txt", "-dash.txt", "dir/n11.txt"):
            self.assertEqual((self.dest / relative).read_bytes(), (self.src / relative).read_bytes(), relative)
        self.assertEqual(sorted(os.listdir(self.dest.parent)), ["dest"])

    def test_single_entries_with_odd_names(self):
        with reader.open_archive(self.pack("odd.7z")) as archive:
            for name in ("we*ird[1] @name.txt", "-dash.txt", "dir/b.txt"):
                entry = next(entry for entry in archive.entries if entry.path == name)
                with archive.open_entry(entry) as handle:
                    self.assertEqual(handle.read(), (self.src / name).read_bytes())
            self.assertEqual(archive.link_target(next(e for e in archive.entries if e.path == "link")), "a.txt")
            self.assertEqual(archive.test(), [])

    def test_encrypted_content_and_names(self):
        path = self.pack("enc.7z", "-pS3cret pass", "-mhe=on")
        with self.assertRaises(PasswordRequired):
            reader.open_archive(path)
        with self.assertRaises(WrongPassword):
            reader.open_archive(path, password="wrong")
        with reader.open_archive(path, password="S3cret pass") as archive:
            result = extract.extract(archive, self.dest, policy=extract.REPLACE)
        self.assertEqual((self.dest / "dir" / "sub" / "c.bin").read_bytes(), (self.src / "dir" / "sub" / "c.bin").read_bytes())
        self.assertFalse(result.failed)

    def test_aes_zip(self):
        path = self.pack("aes.zip", "-tzip", "-pS3cret", "-mem=AES256")
        with reader.open_archive(path) as archive:
            self.assertEqual(archive.format.key, "zip")
            entry = next(entry for entry in archive.entries if entry.path == "dir/b.txt")
            self.assertEqual(entry.method, "AES")
            with archive.open_entry(entry, "S3cret") as handle:
                self.assertEqual(handle.read(), b"second file\n")
            handler = Recorder(passwords=["bad", "S3cret"])
            result = extract.extract(archive, self.dest, handler=handler, policy=extract.REPLACE)
        self.assertEqual(handler.password_calls, [False, True])
        self.assertEqual((self.dest / "dir" / "sub" / "c.bin").read_bytes(), (self.src / "dir" / "sub" / "c.bin").read_bytes())
        self.assertFalse(result.failed, result.failed)


if __name__ == "__main__":
    unittest.main()
