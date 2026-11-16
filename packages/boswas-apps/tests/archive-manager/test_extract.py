"""Extraction: nothing a hostile archive holds may reach outside the destination.

Every test extracts into ``<tmp>/jail/dest`` and then looks at the whole
temporary directory: what exists outside ``dest`` must be exactly what the
test put there.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import stat
import tarfile
import threading
import unittest

import support
from boswas_apps.archive_manager import extract, reader
from boswas_apps.archive_manager.errors import ArchiveError
from boswas_apps.archive_manager.extract import ASK, RENAME, REPLACE, SKIP, Limits
from support import Recorder, raw_tar, raw_zip, tar_dir, tar_link, tar_member, tree, zip_member

SECRET = b"the original content"


class JailCase(support.EngineCase):
    def setUp(self) -> None:
        super().setUp()
        self.jail = self.tmp / "jail"
        self.dest = self.jail / "dest"
        self.outside = self.jail / "outside"
        self.outside.mkdir(parents=True)
        (self.outside / "secret.txt").write_bytes(SECRET)
        self.archives = self.tmp / "archives"
        self.archives.mkdir()
        self._before = None

    def run_extract(self, path, **options):
        self.dest.mkdir(exist_ok=True)
        if self._before is None:
            self._before = tree(self.jail)
        archive = reader.open_archive(path)
        try:
            return extract.extract(archive, self.dest, **{"policy": REPLACE, **options})
        finally:
            archive.close()

    def assert_nothing_escaped(self) -> None:
        """Outside ``dest`` the jail holds what it held before, byte for byte."""
        after = {name: kind for name, kind in tree(self.jail).items()
                 if not (name == "dest" or name.startswith("dest" + os.sep))}
        before = {name: kind for name, kind in (self._before or {}).items()
                  if not (name == "dest" or name.startswith("dest" + os.sep))}
        self.assertEqual(after, before)
        self.assertEqual((self.outside / "secret.txt").read_bytes(), SECRET)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["archives", "jail"])
        leftovers = [name for name in tree(self.dest) if os.path.basename(name).startswith(".bw-")]
        self.assertEqual(leftovers, [], "temporary files were left behind")

    def reasons(self, issues) -> dict[str, str]:
        return {issue.path: issue.reason for issue in issues}


class TraversalTest(JailCase):
    NAMES = ("../evil.txt", "../../evil.txt", "a/../../evil.txt", "..", "a/b/../../../outside/secret.txt",
             "../outside/secret.txt", "./../evil.txt", "dest/../../evil.txt")

    def test_zip_parent_components(self):
        path = raw_zip(self.archives / "t.zip", [zip_member(name, b"pwned") for name in self.NAMES]
                       + [zip_member("ok.txt", b"fine")])
        result = self.run_extract(path)
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"ok.txt": "file:4"})
        self.assertEqual(len(result.skipped), len(self.NAMES))
        self.assertTrue(all("outside" in issue.reason for issue in result.skipped))
        self.assertEqual(result.files, 1)

    def test_tar_parent_components(self):
        path = raw_tar(self.archives / "t.tar", [tar_member(name, b"pwned") for name in self.NAMES]
                       + [tar_member("ok.txt", b"fine")])
        result = self.run_extract(path)
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"ok.txt": "file:4"})
        self.assertEqual(len(result.skipped), len(self.NAMES))

    def test_absolute_paths_land_inside(self):
        target = str(self.outside / "planted.txt")
        for kind, path in (("zip", raw_zip(self.archives / "a.zip", [zip_member(target, b"pwned"),
                                                                    zip_member("/etc/cron.d/x", b"pwned")])),
                           ("tar", raw_tar(self.archives / "a.tar", [tar_member(target, b"pwned"),
                                                                    tar_member("/etc/cron.d/x", b"pwned")]))):
            with self.subTest(kind=kind):
                result = self.run_extract(path)
                self.assert_nothing_escaped()
                inside = self.dest / target.lstrip("/")
                self.assertEqual(inside.read_bytes(), b"pwned")
                self.assertEqual((self.dest / "etc/cron.d/x").read_bytes(), b"pwned")
                self.assertTrue(any("absolute" in note for note in result.notes))
                self.assertFalse(result.skipped or result.failed)

    def test_backslash_names_from_dos_zip(self):
        path = raw_zip(self.archives / "b.zip", [zip_member(b"..\\..\\evil.txt", b"pwned", system=0),
                                                 zip_member(b"dir\\ok.txt", b"fine", system=0)])
        result = self.run_extract(path)
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"dir": "dir", "dir/ok.txt": "file:4"})
        self.assertEqual(len(result.skipped), 1)

    def test_nul_in_zip_name(self):
        path = raw_zip(self.archives / "n.zip", [zip_member(b"image.png\x00.sh", b"#!/bin/sh"),
                                                 zip_member("ok.txt", b"fine")])
        result = self.run_extract(path)
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"ok.txt": "file:4"})
        self.assertIn("NUL", result.skipped[0].reason)

    def test_control_characters_are_neutralised(self):
        path = raw_tar(self.archives / "c.tar", [tar_member("a\nb.txt", b"1"), tar_member("esc\x1b[2J.txt", b"2"),
                                                 tar_member("photo‮gpj.exe", b"3")],
                       fmt=tarfile.PAX_FORMAT)
        result = self.run_extract(path)
        self.assert_nothing_escaped()
        self.assertEqual(sorted(tree(self.dest)), ["a_b.txt", "esc_[2J.txt", "photo_gpj.exe"])
        self.assertTrue(any("Control characters" in note for note in result.notes))

    def test_nul_in_pax_path(self):
        path = raw_tar(self.archives / "p.tar", [tar_member("placeholder", b"x", pax={"path": "evil\x00name"}),
                                                 tar_member("ok.txt", b"fine")], fmt=tarfile.PAX_FORMAT)
        self.run_extract(path)
        self.assert_nothing_escaped()
        names = sorted(tree(self.dest))
        self.assertIn("ok.txt", names)
        self.assertTrue(all("\0" not in name for name in names))

    def test_empty_and_dot_names(self):
        path = raw_tar(self.archives / "e.tar", [tar_dir("./"), tar_dir("."), tar_member("./ok.txt", b"fine"),
                                                 tar_dir("/")])
        result = self.run_extract(path)
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"ok.txt": "file:4"})
        self.assertFalse(result.failed)


class LinkTest(JailCase):
    def test_file_written_through_archive_symlink_stays_inside(self):
        """The classic: a link to somewhere else, then a file "inside" the link."""
        for target in ("../outside", str(self.jail / "outside"), "/etc"):
            with self.subTest(target=target):
                path = raw_tar(self.archives / "l.tar", [tar_link("link", target), tar_member("link/planted.txt", b"pwned"),
                                                         tar_member("link/secret.txt", b"pwned")])
                result = self.run_extract(path)
                self.assert_nothing_escaped()
                found = tree(self.dest)
                self.assertEqual(found["link"], "dir")                 # a real folder, not the link
                self.assertEqual(found["link/planted.txt"], "file:5")
                self.assertTrue(result.skipped or result.failed)
                self.assertFalse(os.path.islink(self.dest / "link"))
                import shutil
                shutil.rmtree(self.dest)

    def test_same_attack_in_a_zip(self):
        link_mode = stat.S_IFLNK | 0o777
        path = raw_zip(self.archives / "l.zip", [zip_member("link", b"../outside", mode=link_mode, method=0),
                                                 zip_member("link/planted.txt", b"pwned")])
        archive = reader.open_archive(path)
        self.assertEqual(archive.entries[0].kind, "symlink")
        archive.close()
        self.run_extract(path)
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"link": "dir", "link/planted.txt": "file:5"})

    def test_symlink_entry_after_the_file(self):
        path = raw_tar(self.archives / "l2.tar", [tar_member("link/planted.txt", b"pwned"), tar_link("link", "../outside")])
        self.run_extract(path)
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"link": "dir", "link/planted.txt": "file:5"})

    def test_links_that_point_outside_are_not_created(self):
        members = [tar_link("abs", "/etc/passwd"), tar_link("up", "../outside/secret.txt"), tar_link("d/up2", "../../x"),
                   tar_link("empty", ""), tar_link("good", "real.txt"), tar_link("d/back", "../real.txt"),
                   tar_member("real.txt", b"data")]
        result = self.run_extract(raw_tar(self.archives / "o.tar", members))
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"good": "link:real.txt", "d": "dir", "d/back": "link:../real.txt",
                                           "real.txt": "file:4"})
        self.assertEqual(sorted(issue.path for issue in result.skipped), ["abs", "d/up2", "empty", "up"])
        self.assertEqual(result.links, 2)

    def test_chained_links_that_escape_together_are_removed(self):
        # "self" points at the destination; "esc" walks through it twice and then up twice. Each link is
        # harmless on paper; together they leave the destination.
        members = [tar_link("self", "."), tar_link("esc", "self/self/../.."), tar_link("fine", "self/real.txt"),
                   tar_member("real.txt", b"data")]
        result = self.run_extract(raw_tar(self.archives / "c.tar", members))
        self.assert_nothing_escaped()
        found = tree(self.dest)
        self.assertNotIn("esc", found)
        self.assertEqual(found["fine"], "link:self/real.txt")
        self.assertIn("esc", self.reasons(result.skipped))
        for name, kind in found.items():
            if kind.startswith("link:"):
                resolved = os.path.realpath(self.dest / name)
                self.assertTrue(resolved.startswith(os.path.realpath(self.dest)), name)

    def test_link_through_a_link_that_was_there_before(self):
        self.dest.mkdir()
        os.symlink("../outside", self.dest / "door")
        result = self.run_extract(raw_tar(self.archives / "d.tar", [tar_link("peek", "door/secret.txt")]))
        self.assert_nothing_escaped()
        self.assertFalse(os.path.lexists(self.dest / "peek"))
        self.assertIn("peek", self.reasons(result.skipped))

    def test_existing_symlinked_folder_is_not_followed(self):
        self.dest.mkdir()
        os.symlink("../outside", self.dest / "door")
        os.symlink(str(self.outside), self.dest / "door2")
        members = [tar_member("door/planted.txt", b"pwned"), tar_member("door2/planted.txt", b"pwned"),
                   tar_member("door/sub/deep.txt", b"pwned"), tar_dir("door/newdir"), tar_member("ok.txt", b"fine")]
        result = self.run_extract(raw_tar(self.archives / "f.tar", members))
        self.assert_nothing_escaped()
        self.assertEqual(os.listdir(self.outside), ["secret.txt"])
        self.assertEqual(len(result.failed), 4)
        self.assertTrue(all("link" in issue.reason for issue in result.failed))
        self.assertEqual((self.dest / "ok.txt").read_bytes(), b"fine")

    def test_existing_link_as_the_file_itself_is_replaced_not_followed(self):
        self.dest.mkdir()
        os.symlink("../outside/secret.txt", self.dest / "notes.txt")
        result = self.run_extract(raw_zip(self.archives / "r.zip", [zip_member("notes.txt", b"new content")]))
        self.assert_nothing_escaped()
        self.assertFalse(os.path.islink(self.dest / "notes.txt"))
        self.assertEqual((self.dest / "notes.txt").read_bytes(), b"new content")
        self.assertEqual(result.files, 1)

    def test_existing_link_is_kept_when_skipping(self):
        self.dest.mkdir()
        os.symlink("../outside/secret.txt", self.dest / "notes.txt")
        result = self.run_extract(raw_zip(self.archives / "s.zip", [zip_member("notes.txt", b"new content")]),
                                  policy=SKIP)
        self.assert_nothing_escaped()
        self.assertTrue(os.path.islink(self.dest / "notes.txt"))
        self.assertEqual(len(result.skipped), 1)

    def test_hard_links(self):
        members = [tar_member("real.txt", b"data"), tar_link("twin.txt", "real.txt", hard=True),
                   tar_link("early.txt", "late.txt", hard=True), tar_member("late.txt", b"later"),
                   tar_link("abs", "/etc/passwd", hard=True), tar_link("up", "../outside/secret.txt", hard=True),
                   tar_link("missing", "nowhere.txt", hard=True)]
        result = self.run_extract(raw_tar(self.archives / "h.tar", members))
        self.assert_nothing_escaped()
        self.assertEqual((self.dest / "twin.txt").read_bytes(), b"data")
        self.assertEqual((self.dest / "early.txt").read_bytes(), b"later")
        self.assertEqual(sorted(issue.path for issue in result.skipped), ["abs", "missing", "up"])
        self.assertEqual(sorted(tree(self.dest)), ["early.txt", "late.txt", "real.txt", "twin.txt"])

    def test_hard_link_to_a_file_that_was_there_cannot_be_used_to_overwrite_it(self):
        self.dest.mkdir()
        (self.dest / "victim.txt").write_bytes(b"precious")
        members = [tar_link("h", "victim.txt", hard=True), tar_member("h", b"pwned"),
                   tar_link("h2", "../outside/secret.txt", hard=True), tar_member("h2", b"pwned")]
        result = self.run_extract(raw_tar(self.archives / "v.tar", members))
        self.assert_nothing_escaped()
        self.assertEqual((self.dest / "victim.txt").read_bytes(), b"precious")
        self.assertEqual((self.dest / "h").read_bytes(), b"pwned")
        self.assertEqual(os.stat(self.dest / "victim.txt").st_nlink, 1)
        self.assertEqual(len(result.skipped), 2)

    def test_file_then_link_of_the_same_name(self):
        members = [tar_member("x", b"file"), tar_link("x", "y"), tar_member("y", b"target")]
        self.run_extract(raw_tar(self.archives / "o.tar", members))
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest)["x"], "link:y")           # the later entry wins

    def test_link_then_file_of_the_same_name(self):
        members = [tar_link("x", "y"), tar_member("x", b"file"), tar_member("y", b"target")]
        result = self.run_extract(raw_tar(self.archives / "o2.tar", members))
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest)["x"], "file:4")
        self.assertEqual((self.dest / "y").read_bytes(), b"target")
        self.assertIn("x", self.reasons(result.skipped))

    def test_link_cannot_replace_a_folder(self):
        members = [tar_dir("d"), tar_member("d/f.txt", b"1"), tar_link("d", "../outside")]
        result = self.run_extract(raw_tar(self.archives / "r.tar", members))
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"d": "dir", "d/f.txt": "file:1"})
        self.assertTrue(result.failed or result.skipped)


class SpecialFilesTest(JailCase):
    def test_devices_pipes_and_sockets_are_skipped(self):
        members = [tar_member("null", None, kind=tarfile.CHRTYPE), tar_member("disk", None, kind=tarfile.BLKTYPE),
                   tar_member("pipe", None, kind=tarfile.FIFOTYPE), tar_member("ok.txt", b"fine")]
        result = self.run_extract(raw_tar(self.archives / "s.tar", members))
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"ok.txt": "file:4"})
        self.assertEqual(sorted(issue.path for issue in result.skipped), ["disk", "null", "pipe"])

    def test_zip_special_modes_are_skipped(self):
        members = [zip_member("pipe", b"", mode=stat.S_IFIFO | 0o644), zip_member("dev", b"", mode=stat.S_IFCHR | 0o666),
                   zip_member("sock", b"", mode=stat.S_IFSOCK | 0o666), zip_member("ok.txt", b"fine")]
        result = self.run_extract(raw_zip(self.archives / "s.zip", members))
        self.assert_nothing_escaped()
        self.assertEqual(tree(self.dest), {"ok.txt": "file:4"})
        self.assertEqual(len(result.skipped), 3)

    def test_setuid_setgid_sticky_are_dropped(self):
        members = [tar_member("su", b"x", mode=0o4755), tar_member("sg", b"x", mode=0o2755),
                   tar_member("st", b"x", mode=0o1777), tar_dir("d", mode=0o7777), tar_member("d/f", b"x", mode=0o6777)]
        self.run_extract(raw_tar(self.archives / "m.tar", members))
        zip_members = [zip_member("zsu", b"x", mode=stat.S_IFREG | 0o4755), zip_member("zall", b"x", mode=stat.S_IFREG | 0o7777)]
        self.run_extract(raw_zip(self.archives / "m.zip", zip_members))
        self.assert_nothing_escaped()
        modes = {name: stat.S_IMODE(os.lstat(self.dest / name).st_mode) for name in tree(self.dest)}
        self.assertEqual(modes, {"su": 0o755, "sg": 0o755, "st": 0o755, "d": 0o755, "d/f": 0o755, "zsu": 0o755,
                                 "zall": 0o755})

    def test_permission_bits_follow_the_umask(self):
        os.umask(0o027)
        members = [tar_member("exec.sh", b"x", mode=0o755), tar_member("private", b"x", mode=0o600),
                   tar_member("readonly", b"x", mode=0o444), tar_member("open", b"x", mode=0o666),
                   tar_dir("locked", mode=0o500), tar_member("locked/in.txt", b"x", mode=0o644),
                   tar_member("implied/deep/f", b"x", mode=0o640)]
        self.run_extract(raw_tar(self.archives / "u.tar", members))
        modes = {name: stat.S_IMODE(os.lstat(self.dest / name).st_mode) for name in tree(self.dest)}
        self.assertEqual(modes, {"exec.sh": 0o750, "private": 0o600, "readonly": 0o440, "open": 0o640,
                                 "locked": 0o700, "locked/in.txt": 0o640, "implied": 0o750, "implied/deep": 0o750,
                                 "implied/deep/f": 0o640})

    def test_zip_without_unix_modes_gets_defaults(self):
        path = raw_zip(self.archives / "w.zip", [zip_member("d/", b"", system=0, is_dir=True, method=0),
                                                 zip_member("d/f.txt", b"x", system=0)])
        self.run_extract(path)
        self.assertEqual(stat.S_IMODE(os.lstat(self.dest / "d/f.txt").st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.lstat(self.dest / "d").st_mode), 0o755)

    def test_modification_times_are_kept(self):
        members = [tar_dir("d", mtime=1_600_000_000), tar_member("d/f.txt", b"x", mtime=1_500_000_000),
                   tar_member("neg", b"x", mtime=-5), tar_link("l", "d/f.txt")]
        self.run_extract(raw_tar(self.archives / "t.tar", members))
        self.assertEqual(int(os.stat(self.dest / "d/f.txt").st_mtime), 1_500_000_000)
        self.assertEqual(int(os.stat(self.dest / "d").st_mtime), 1_600_000_000)
        path = raw_zip(self.archives / "t.zip", [zip_member("z.txt", b"x", stamp=(2020, 2, 29, 13, 14, 16)),
                                                 zip_member("bad.txt", b"x", stamp=(1980, 0, 0, 0, 0, 0))])
        self.run_extract(path)
        import time
        self.assertEqual(time.localtime(os.stat(self.dest / "z.txt").st_mtime)[:6], (2020, 2, 29, 13, 14, 16))
        self.assertTrue((self.dest / "bad.txt").exists())


class NamesOnDiskTest(JailCase):
    def test_tar_names_that_are_not_utf8_keep_their_bytes(self):
        name = b"caf\xe9-\xff.txt".decode("utf-8", "surrogateescape")
        self.run_extract(raw_tar(self.archives / "l1.tar", [tar_member(name, b"x")]))
        self.assertEqual(os.listdir(os.fsencode(self.dest)), [b"caf\xe9-\xff.txt"])

    def test_zip_names(self):
        members = [zip_member("résumé.txt".encode("cp437"), b"1"), zip_member("日本語.txt".encode("utf-8"), b"2", utf8=False),
                   zip_member("grüße/ß.txt", b"3")]
        self.run_extract(raw_zip(self.archives / "n.zip", members))
        self.assertEqual(sorted(tree(self.dest)), ["grüße", "grüße/ß.txt", "résumé.txt", "日本語.txt"])

    def test_duplicate_names_last_one_wins(self):
        for kind, path in (("zip", raw_zip(self.archives / "d.zip", [zip_member("dup.txt", b"first"),
                                                                    zip_member("dup.txt", b"second!")])),
                           ("tar", raw_tar(self.archives / "d.tar", [tar_member("dup.txt", b"first"),
                                                                    tar_member("dup.txt", b"second!")]))):
            with self.subTest(kind=kind):
                result = self.run_extract(path, policy=SKIP)       # not a conflict with a file that was there
                self.assertEqual((self.dest / "dup.txt").read_bytes(), b"second!")
                self.assertTrue(any("more than once" in note for note in result.notes))
                self.assertEqual(sorted(tree(self.dest)), ["dup.txt"])
                (self.dest / "dup.txt").unlink()

    def test_duplicate_does_not_overwrite_a_kept_file(self):
        self.dest.mkdir()
        (self.dest / "dup.txt").write_bytes(b"mine")
        path = raw_zip(self.archives / "k.zip", [zip_member("dup.txt", b"first"), zip_member("dup.txt", b"second!")])
        self.run_extract(path, policy=RENAME)
        self.assertEqual((self.dest / "dup.txt").read_bytes(), b"mine")
        self.assertEqual((self.dest / "dup (2).txt").read_bytes(), b"second!")
        self.assertEqual(sorted(tree(self.dest)), ["dup (2).txt", "dup.txt"])

    def test_case_collisions_on_a_case_insensitive_file_system(self):
        members = [zip_member("README", b"upper"), zip_member("readme", b"lower"), zip_member("Docs/a.txt", b"1"),
                   zip_member("docs/A.TXT", b"2"), zip_member("ReadMe", b"mixed")]
        result = self.run_extract(raw_zip(self.archives / "c.zip", members), case_insensitive=True)
        found = tree(self.dest)
        self.assertEqual((self.dest / "README").read_bytes(), b"upper")
        self.assertEqual((self.dest / "readme (2)").read_bytes(), b"lower")
        self.assertEqual((self.dest / "ReadMe (3)").read_bytes(), b"mixed")
        self.assertEqual(len([name for name in found if found[name].startswith("file")]), 5)
        self.assertTrue(any("upper and lower case" in note for note in result.notes))

    def test_case_differences_are_fine_where_the_file_system_tells_them_apart(self):
        members = [zip_member("README", b"upper"), zip_member("readme", b"lower")]
        result = self.run_extract(raw_zip(self.archives / "c2.zip", members), case_insensitive=False)
        self.assertEqual(sorted(tree(self.dest)), ["README", "readme"])
        self.assertFalse(result.notes)

    def test_file_and_folder_of_the_same_name(self):
        result = self.run_extract(raw_tar(self.archives / "x.tar", [tar_member("a", b"file"), tar_member("a/b", b"x")]))
        self.assertEqual(tree(self.dest), {"a": "file:4"})
        self.assertIn("a/b", self.reasons(result.failed))
        import shutil
        shutil.rmtree(self.dest)
        result = self.run_extract(raw_tar(self.archives / "y.tar", [tar_member("a/b", b"x"), tar_member("a", b"file")]))
        self.assertEqual(tree(self.dest), {"a": "dir", "a/b": "file:1"})
        self.assertIn("a", self.reasons(result.failed))

    def test_very_deep_paths_do_not_exhaust_descriptors(self):
        deep = "/".join(["d"] * 400)
        result = self.run_extract(raw_tar(self.archives / "deep.tar", [tar_member(deep + "/f.txt", b"x")],
                                          fmt=tarfile.PAX_FORMAT))
        self.assertTrue((self.dest / deep / "f.txt").exists())
        self.assertFalse(result.failed)

    def test_many_folders(self):
        members = [tar_member(f"dir{index:04d}/f.txt", b"x") for index in range(400)]
        result = self.run_extract(raw_tar(self.archives / "many.tar", members))
        self.assertEqual(result.files, 400)
        self.assertEqual(result.folders, 400)


class OverwriteTest(JailCase):
    def _archive(self):
        return raw_zip(self.archives / "o.zip", [zip_member("a.txt", b"new a"), zip_member("b.txt", b"new b"),
                                                 zip_member("c.txt", b"new c")])

    def _prepare(self):
        self.dest.mkdir()
        (self.dest / "a.txt").write_bytes(b"old a")
        (self.dest / "b.txt").write_bytes(b"old b")

    def test_replace_all(self):
        self._prepare()
        result = self.run_extract(self._archive(), policy=REPLACE)
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"new a")
        self.assertTrue(result.clean)

    def test_skip_all(self):
        self._prepare()
        result = self.run_extract(self._archive(), policy=SKIP)
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"old a")
        self.assertEqual((self.dest / "c.txt").read_bytes(), b"new c")
        self.assertEqual(sorted(issue.path for issue in result.skipped), ["a.txt", "b.txt"])

    def test_rename(self):
        self._prepare()
        (self.dest / "a (2).txt").write_bytes(b"also old")
        result = self.run_extract(self._archive(), policy=RENAME)
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"old a")
        self.assertEqual((self.dest / "a (2).txt").read_bytes(), b"also old")
        self.assertEqual((self.dest / "a (3).txt").read_bytes(), b"new a")
        self.assertEqual((self.dest / "b (2).txt").read_bytes(), b"new b")
        self.assertTrue(result.clean)

    def test_ask_each(self):
        self._prepare()
        handler = Recorder(overwrite=[extract.REPLACE, extract.SKIP])
        result = self.run_extract(self._archive(), policy=ASK, handler=handler)
        self.assertEqual([conflict.path for conflict in handler.conflicts], ["a.txt", "b.txt"])
        self.assertEqual(handler.conflicts[0].existing_size, 5)
        self.assertEqual(handler.conflicts[0].new_size, 5)
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"new a")
        self.assertEqual((self.dest / "b.txt").read_bytes(), b"old b")
        self.assertEqual(len(result.skipped), 1)

    def test_ask_then_apply_to_all(self):
        for answer, expected_a, expected_b in ((extract.REPLACE_ALL, b"new a", b"new b"),
                                               (extract.SKIP_ALL, b"old a", b"old b"),
                                               (extract.RENAME_ALL, b"old a", b"old b")):
            with self.subTest(answer=answer):
                import shutil
                shutil.rmtree(self.dest, ignore_errors=True)
                self._prepare()
                handler = Recorder(overwrite=[answer])
                self.run_extract(self._archive(), policy=ASK, handler=handler)
                self.assertEqual(len(handler.conflicts), 1)             # asked once
                self.assertEqual((self.dest / "a.txt").read_bytes(), expected_a)
                self.assertEqual((self.dest / "b.txt").read_bytes(), expected_b)
                if answer == extract.RENAME_ALL:
                    self.assertEqual((self.dest / "b (2).txt").read_bytes(), b"new b")

    def test_ask_cancel(self):
        self._prepare()
        handler = Recorder(overwrite=[extract.CANCEL])
        result = self.run_extract(self._archive(), policy=ASK, handler=handler)
        self.assertTrue(result.cancelled)
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"old a")
        self.assertFalse((self.dest / "c.txt").exists())
        self.assert_nothing_escaped()

    def test_default_handler_never_overwrites(self):
        self._prepare()
        archive = reader.open_archive(self._archive())
        result = extract.extract(archive, self.dest)                   # policy ASK, silent handler
        archive.close()
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"old a")
        self.assertEqual(len(result.skipped), 2)

    def test_folder_in_the_way_of_a_file(self):
        self.dest.mkdir()
        (self.dest / "a.txt").mkdir()
        (self.dest / "a.txt" / "keep").write_bytes(b"k")
        result = self.run_extract(self._archive(), policy=REPLACE)
        self.assertEqual((self.dest / "a.txt" / "keep").read_bytes(), b"k")
        self.assertIn("a.txt", self.reasons(result.failed))
        self.assertEqual((self.dest / "b.txt").read_bytes(), b"new b")

    def test_folders_merge(self):
        self.dest.mkdir()
        (self.dest / "d").mkdir(mode=0o700)
        (self.dest / "d" / "mine.txt").write_bytes(b"m")
        result = self.run_extract(raw_tar(self.archives / "m.tar", [tar_dir("d", mode=0o755), tar_member("d/new.txt", b"n")]))
        self.assertEqual(sorted(os.listdir(self.dest / "d")), ["mine.txt", "new.txt"])
        self.assertEqual(stat.S_IMODE(os.stat(self.dest / "d").st_mode), 0o700)   # a folder that was there is left alone
        self.assertEqual(result.folders, 0)


class SizeGuardTest(JailCase):
    LIMITS = Limits(max_total=4 * 1024 * 1024, max_ratio=20.0, ratio_floor=256 * 1024, max_entries=50)

    def test_high_ratio_asks_with_the_numbers(self):
        path = raw_zip(self.archives / "bomb.zip", [zip_member("zeros.bin", bytes(1024 * 1024))])
        handler = Recorder(confirm=False)
        result = self.run_extract(path, limits=self.LIMITS, handler=handler)
        self.assertTrue(result.cancelled)
        self.assertEqual(tree(self.dest), {})
        (warning,) = handler.warnings
        self.assertIn("ratio", warning.reasons)
        self.assertEqual(warning.unpacked, 1024 * 1024)
        self.assertEqual(warning.packed, os.path.getsize(path))
        self.assertFalse(warning.running)
        text = warning.text()
        self.assertIn("1.0 MB", text)
        self.assertIn("times its size", text)
        self.assertIn("free", text)
        self.assert_nothing_escaped()

    def test_confirmed_bomb_is_extracted(self):
        path = raw_zip(self.archives / "bomb.zip", [zip_member("zeros.bin", bytes(1024 * 1024))])
        handler = Recorder(confirm=True)
        result = self.run_extract(path, limits=self.LIMITS, handler=handler)
        self.assertFalse(result.cancelled)
        self.assertEqual(os.path.getsize(self.dest / "zeros.bin"), 1024 * 1024)
        self.assertEqual(len(handler.warnings), 1)

    def test_absolute_size_limit(self):
        data = os.urandom(64 * 1024)
        path = raw_zip(self.archives / "big.zip", [zip_member(f"f{index}", data, method=0) for index in range(8)])
        limits = Limits(max_total=256 * 1024, max_ratio=1e9, ratio_floor=1 << 40, max_entries=10 ** 6)
        handler = Recorder(confirm=False)
        result = self.run_extract(path, limits=limits, handler=handler)
        self.assertTrue(result.cancelled)
        self.assertEqual(handler.warnings[0].reasons, ("size",))
        self.assertIn("more than 256", handler.warnings[0].text())

    def test_entry_count_limit(self):
        path = raw_zip(self.archives / "many.zip", [zip_member(f"f{index}", b"") for index in range(60)])
        handler = Recorder(confirm=False)
        result = self.run_extract(path, limits=self.LIMITS, handler=handler)
        self.assertTrue(result.cancelled)
        self.assertEqual(handler.warnings[0].reasons, ("entries",))
        self.assertEqual(handler.warnings[0].entries, 60)

    def test_small_archives_do_not_ask(self):
        path = raw_zip(self.archives / "small.zip", [zip_member("zeros.bin", bytes(100 * 1024))])
        handler = Recorder(confirm=False)
        result = self.run_extract(path, limits=self.LIMITS, handler=handler)
        self.assertEqual(handler.warnings, [])
        self.assertEqual(result.files, 1)

    def test_default_limits_do_not_ask_for_ordinary_archives(self):
        path = raw_tar(self.archives / "n.tar.gz", [tar_member("text.txt", b"lorem ipsum " * 20000)], mode="w:gz")
        handler = Recorder(confirm=False)
        result = self.run_extract(path, handler=handler)
        self.assertEqual(handler.warnings, [])
        self.assertTrue(result.clean)

    def test_tar_gz_bomb(self):
        path = raw_tar(self.archives / "b.tar.gz", [tar_member("zeros.bin", bytes(2 * 1024 * 1024))], mode="w:gz")
        handler = Recorder(confirm=False)
        result = self.run_extract(path, limits=self.LIMITS, handler=handler)
        self.assertTrue(result.cancelled)
        self.assertIn("ratio", handler.warnings[0].reasons)
        self.assertEqual(tree(self.dest), {})

    def test_stream_of_unknown_size_is_counted_while_it_is_written(self):
        path = self.archives / "zeros.bin.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(bytes(3 * 1024 * 1024))
        limits = Limits(max_total=1024 * 1024, max_ratio=20.0, ratio_floor=256 * 1024)
        handler = Recorder(confirm=False)
        result = self.run_extract(path, limits=limits, handler=handler)
        self.assertTrue(result.cancelled)
        (warning,) = handler.warnings
        self.assertTrue(warning.running)
        self.assertGreater(warning.unpacked, 256 * 1024)
        self.assertLessEqual(warning.unpacked, 2 * 1024 * 1024)
        self.assertIn("so far", warning.text())
        self.assertEqual(tree(self.dest), {})                      # the partial file is gone
        self.assert_nothing_escaped()

    def test_stream_guard_asks_again_as_it_keeps_growing(self):
        path = self.archives / "zeros.bin.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(bytes(6 * 1024 * 1024))
        limits = Limits(max_total=1024 * 1024, max_ratio=20.0, ratio_floor=256 * 1024)
        handler = Recorder(confirm=True)
        result = self.run_extract(path, limits=limits, handler=handler)
        self.assertFalse(result.cancelled)
        self.assertGreaterEqual(len(handler.warnings), 2)
        self.assertEqual(os.path.getsize(self.dest / "zeros.bin"), 6 * 1024 * 1024)

    def test_size_that_lies_low_cannot_write_more_than_declared(self):
        path = raw_zip(self.archives / "lie.zip", [zip_member("lie.bin", bytes(512 * 1024), claim_size=10),
                                                   zip_member("ok.txt", b"fine")])
        handler = Recorder(confirm=False)
        result = self.run_extract(path, limits=self.LIMITS, handler=handler)
        self.assertEqual(tree(self.dest), {"ok.txt": "file:4"})
        self.assertIn("lie.bin", self.reasons(result.failed))
        self.assert_nothing_escaped()

    def test_not_enough_space_is_a_question_too(self):
        path = raw_zip(self.archives / "s.zip", [zip_member("a.bin", bytes(1000), method=0)])
        archive = reader.open_archive(path)
        self.dest.mkdir()
        handler = Recorder(confirm=False)
        extractor = extract._Extractor(archive, str(self.dest), REPLACE, Limits(), None, handler, None, None)
        extractor._free_space = lambda: 10
        result = extractor.run(None, "")
        archive.close()
        self.assertTrue(result.cancelled)
        self.assertEqual(handler.warnings[0].reasons, ("space",))
        self.assertIn("Only 10 bytes are free", handler.warnings[0].text())
        self.assertEqual(handler.warnings[0].title(), "Not enough free space")


class ProgressAndCancelTest(JailCase):
    def test_progress_reports_bytes_and_entries(self):
        path = raw_zip(self.archives / "p.zip", [zip_member("a.bin", os.urandom(300_000), method=0),
                                                 zip_member("d/b.bin", os.urandom(100_000), method=0)])
        handler = Recorder()
        result = self.run_extract(path, handler=handler)
        last = handler.progress_calls[-1]
        self.assertEqual((last.bytes_done, last.bytes_total, last.entries_done, last.entries_total),
                         (400_000, 400_000, 2, 2))
        self.assertEqual(result.bytes, 400_000)
        self.assertEqual({info.path for info in handler.progress_calls}, {"a.bin", "d/b.bin"})
        done = [info.bytes_done for info in handler.progress_calls]
        self.assertEqual(done, sorted(done))

    def test_cancel_in_the_middle_of_a_file_leaves_no_partial_file(self):
        path = raw_zip(self.archives / "c.zip", [zip_member("first.txt", b"done"),
                                                 zip_member("big.bin", os.urandom(4 * 1024 * 1024), method=0),
                                                 zip_member("never.txt", b"x")])
        cancel = threading.Event()
        handler = Recorder()

        def stop(info):
            if info.path == "big.bin" and info.bytes_done > 1024 * 1024:
                cancel.set()

        handler.on_progress = stop
        result = self.run_extract(path, handler=handler, cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(tree(self.dest), {"first.txt": "file:4"})
        self.assertEqual(result.files, 1)
        self.assertEqual(result.bytes, 4)
        self.assertIn("Cancelled", result.summary())
        self.assert_nothing_escaped()

    def test_cancelled_before_start(self):
        cancel = threading.Event()
        cancel.set()
        result = self.run_extract(raw_zip(self.archives / "c.zip", [zip_member("a", b"x")]), cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(tree(self.dest), {})


class SelectionTest(JailCase):
    def _archive(self):
        return raw_tar(self.archives / "s.tar.gz", [
            tar_dir("docs"), tar_member("docs/readme.txt", b"r"), tar_dir("docs/img"),
            tar_member("docs/img/a.png", b"aa"), tar_member("docs/img/b.png", b"bb"), tar_member("top.txt", b"t"),
            tar_link("docs/img/link", "a.png")], mode="w:gz")

    def test_selected_entries_only(self):
        archive = reader.open_archive(self._archive())
        chosen = [entry for entry in archive.entries if entry.path in ("docs/img/a.png", "top.txt")]
        result = extract.extract(archive, self.dest, entries=chosen, policy=REPLACE)
        archive.close()
        self.assertEqual(tree(self.dest), {"docs": "dir", "docs/img": "dir", "docs/img/a.png": "file:2",
                                           "top.txt": "file:1"})
        self.assertEqual(result.files, 2)

    def test_base_folder_is_stripped(self):
        archive = reader.open_archive(self._archive())
        folder = next(entry for entry in archive.entries if entry.path == "docs/img")
        chosen = archive.listing.descendants(folder)
        result = extract.extract(archive, self.dest, entries=chosen, base="docs", policy=REPLACE)
        archive.close()
        self.assertEqual(tree(self.dest), {"img": "dir", "img/a.png": "file:2", "img/b.png": "file:2",
                                           "img/link": "link:a.png"})
        self.assertEqual(result.top_level, ["img"])

    def test_link_out_of_the_stripped_base_is_refused(self):
        archive = reader.open_archive(raw_tar(self.archives / "b.tar", [
            tar_member("a/b/f.txt", b"x"), tar_link("a/b/up", "../../outside/secret.txt"),
            tar_link("a/b/near", "../x")]))
        result = extract.extract(archive, self.dest, base="a/b", policy=REPLACE)
        archive.close()
        self.assertEqual(tree(self.dest), {"f.txt": "file:1"})
        self.assertEqual(len(result.skipped), 2)
        self.assert_nothing_escaped_simple()

    def assert_nothing_escaped_simple(self):
        self.assertEqual(os.listdir(self.outside), ["secret.txt"])

    def test_summary_lists_everything(self):
        members = [tar_member("ok.txt", b"fine"), tar_member("../evil", b"x"), tar_member("pipe", None, kind=tarfile.FIFOTYPE),
                   tar_member("a", b"file"), tar_member("a/b", b"x")]
        result = self.run_extract(raw_tar(self.archives / "sum.tar", members))
        self.assertEqual(result.files, 2)
        self.assertEqual(len(result.skipped), 2)
        self.assertEqual(len(result.failed), 1)
        self.assertFalse(result.clean)
        self.assertEqual(result.summary(), "Extracted 2 files (8 bytes). 2 skipped. 1 failed.")
        self.assertEqual(sorted(result.top_level), ["a", "ok.txt"])


class StreamsTest(JailCase):
    def test_single_file_streams(self):
        data = b"plain text\n" * 1000
        for name, opener in (("notes.txt.gz", gzip.open), ("notes.txt.bz2", bz2.open), ("notes.txt.xz", lzma.open)):
            with self.subTest(name=name):
                path = self.archives / name
                with opener(path, "wb") as handle:
                    handle.write(data)
                result = self.run_extract(path)
                self.assertEqual((self.dest / "notes.txt").read_bytes(), data)
                self.assertEqual(result.files, 1)
                (self.dest / "notes.txt").unlink()

    def test_damaged_stream_leaves_nothing(self):
        path = self.archives / "broken.txt.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(os.urandom(200_000))
        raw = bytearray(path.read_bytes())
        del raw[len(raw) // 2:]
        path.write_bytes(raw)
        result = self.run_extract(path)
        self.assertEqual(tree(self.dest), {})
        self.assertEqual(len(result.failed), 1)
        self.assertIn("damaged", result.failed[0].reason)

    def test_damaged_zip_entry_fails_alone(self):
        good = os.urandom(50_000)
        path = raw_zip(self.archives / "crc.zip", [zip_member("bad.bin", b"data" * 100, crc=0x12345678),
                                                   zip_member("good.bin", good)])
        result = self.run_extract(path)
        self.assertEqual(tree(self.dest), {"good.bin": "file:50000"})
        self.assertIn("checksum", self.reasons(result.failed)["bad.bin"])

    def test_truncated_tar_extracts_what_is_there(self):
        path = raw_tar(self.archives / "t.tar", [tar_member("one.txt", b"1" * 2000), tar_member("two.txt", b"2" * 2000)])
        raw = path.read_bytes()
        path.write_bytes(raw[:512 + 2048 + 512 + 700])
        archive = reader.open_archive(path)
        self.assertTrue(archive.warning or len(archive.entries) == 2)
        result = extract.extract(archive, self.dest, policy=REPLACE)
        archive.close()
        self.assertEqual((self.dest / "one.txt").read_bytes(), b"1" * 2000)
        self.assertFalse((self.dest / "two.txt").exists())
        self.assertTrue(result.failed)


class DestinationTest(JailCase):
    def test_destination_is_created(self):
        target = self.jail / "new" / "deeper"
        archive = reader.open_archive(raw_zip(self.archives / "d.zip", [zip_member("a.txt", b"x")]))
        result = extract.extract(archive, target, policy=REPLACE)
        archive.close()
        self.assertEqual(result.destination, str(target))
        self.assertTrue((target / "a.txt").exists())

    def test_destination_that_is_a_file(self):
        target = self.jail / "file"
        target.write_bytes(b"x")
        archive = reader.open_archive(raw_zip(self.archives / "d.zip", [zip_member("a.txt", b"x")]))
        with self.assertRaises(ArchiveError):
            extract.extract(archive, target, policy=REPLACE)
        archive.close()

    @unittest.skipIf(os.geteuid() == 0, "root ignores permission bits")
    def test_read_only_destination_is_reported(self):
        self.dest.mkdir()
        os.chmod(self.dest, 0o500)
        try:
            result = self.run_extract(raw_zip(self.archives / "r.zip", [zip_member("a.txt", b"x"), zip_member("d/b", b"y")]))
        finally:
            os.chmod(self.dest, 0o700)
        self.assertEqual(len(result.failed), 2)
        self.assertEqual(tree(self.dest), {})

    def test_an_empty_destination_is_refused_not_taken_for_the_working_directory(self):
        archive = reader.open_archive(raw_zip(self.archives / "d.zip", [zip_member("a.txt", b"x")]))
        for nothing in ("", b""):
            with self.assertRaises(ArchiveError):
                extract.extract(archive, nothing, policy=REPLACE)
        archive.close()
        self.assertEqual(os.listdir(os.getcwd()), [])

    def test_default_destination(self):
        self.assertEqual(extract.default_destination("/a/b/photos.tar.gz"), "/a/b/photos")
        self.assertEqual(extract.default_destination("/a/b/data.zip"), "/a/b/data")
        self.assertEqual(extract.default_destination("/a/b/noext"), "/a/b/noext.extracted")

    def test_umask_is_read_without_changing_it(self):
        os.umask(0o027)
        self.assertEqual(extract.current_umask(), 0o027)
        self.assertEqual(os.umask(0o027), 0o027)


class PasswordTest(JailCase):
    def _archive(self):
        return raw_zip(self.archives / "enc.zip", [zip_member("secret.txt", b"top secret " * 50, password="hunter2"),
                                                   zip_member("plain.txt", b"public")])

    def test_right_password(self):
        result = self.run_extract(self._archive(), password="hunter2")
        self.assertEqual((self.dest / "secret.txt").read_bytes(), b"top secret " * 50)
        self.assertTrue(result.clean)

    def test_password_is_asked_and_retried(self):
        handler = Recorder(passwords=["wrong", "also wrong", "hunter2"])
        result = self.run_extract(self._archive(), handler=handler)
        self.assertEqual(handler.password_calls, [False, True, True])
        self.assertEqual((self.dest / "secret.txt").read_bytes(), b"top secret " * 50)
        self.assertTrue(result.clean)

    def test_giving_up_cancels_before_anything_is_written(self):
        handler = Recorder(passwords=["wrong"])
        result = self.run_extract(self._archive(), handler=handler)
        self.assertTrue(result.cancelled)
        self.assertEqual(tree(self.dest), {})

    def test_unencrypted_selection_needs_no_password(self):
        archive = reader.open_archive(self._archive())
        handler = Recorder()
        chosen = [entry for entry in archive.entries if entry.path == "plain.txt"]
        result = extract.extract(archive, self.dest, entries=chosen, handler=handler, policy=REPLACE)
        archive.close()
        self.assertEqual(handler.password_calls, [])
        self.assertEqual(result.files, 1)


if __name__ == "__main__":
    unittest.main()
