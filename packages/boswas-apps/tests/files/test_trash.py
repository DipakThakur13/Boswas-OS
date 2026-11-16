"""The freedesktop.org Trash: home trash, volume trashes, records, hostile content."""

import os
import stat
import time
import unittest
from urllib.parse import quote

from support import IS_ROOT, TempCase, tree

from boswas_apps.files import trash as trash_module
from boswas_apps.files.trash import Trash, TrashError, TrashUnavailable, parse_trashinfo


class TrashCase(TempCase):
    """HOME is one volume; ``self.tmp/vol*`` are declared volumes of their own."""

    def setUp(self):
        super().setUp()
        self.volumes = [self.mkdir("volA"), self.mkdir("volB")]
        self.home_trash = os.path.join(self.home, ".local", "share", "Trash")
        self.trash = self.make()

    def topdir(self, directory):
        directory = os.path.realpath(directory)
        for volume in self.volumes:
            if directory == volume or directory.startswith(volume + os.sep):
                return volume
        return self.tmp

    def make(self, **kwargs):
        return Trash(self.home_trash, topdir_of=self.topdir, mount_points=lambda: [self.tmp, *self.volumes], **kwargs)

    def info(self, item):
        return self.read(item.info_path).decode()


class HomeTrashTest(TrashCase):
    def test_trash_writes_file_and_record(self):
        path = self.write("home/Documents/report.txt", b"content")
        before = int(time.time())
        item = self.trash.trash(path)
        self.assertFalse(os.path.lexists(path))
        self.assertEqual(item.trash_dir, self.home_trash)
        self.assertEqual(item.name, "report.txt")
        self.assertEqual(self.read(item.files_path), b"content")
        lines = self.info(item).splitlines()
        self.assertEqual(lines[0], "[Trash Info]")
        self.assertEqual(lines[1], "Path=" + quote(path, safe="/"))
        self.assertRegex(lines[2], r"^DeletionDate=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertGreaterEqual(item.deleted, before - 1)
        for name in ("", "files", "info"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.home_trash, name)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(item.info_path).st_mode), 0o600)

    def test_default_location_follows_xdg_data_home(self):
        self.assertEqual(Trash().home_trash, self.home_trash)
        os.environ["XDG_DATA_HOME"] = "relative"
        self.assertEqual(Trash().home_trash, os.path.join(self.home, ".local", "share", "Trash"))

    def test_name_collisions(self):
        items = []
        for number in range(4):
            items.append(self.trash.trash(self.write("home/same.txt", str(number))))
        self.assertEqual([item.name for item in items], ["same.txt", "same.txt.2", "same.txt.3", "same.txt.4"])
        self.assertEqual([self.read(item.files_path) for item in items], [b"0", b"1", b"2", b"3"])
        listed = sorted(self.trash.items(), key=lambda item: item.name)
        self.assertEqual([item.original_path for item in listed], [self.path("home/same.txt")] * 4)

    def test_a_file_without_a_record_is_not_overwritten(self):
        os.makedirs(os.path.join(self.home_trash, "files"))
        orphan = os.path.join(self.home_trash, "files", "x")
        with open(orphan, "w") as handle:
            handle.write("orphan")
        item = self.trash.trash(self.write("home/x", "new"))
        self.assertEqual(item.name, "x.2")
        self.assertEqual(self.read(orphan), b"orphan")
        self.assertFalse(os.path.exists(os.path.join(self.home_trash, "info", "x.trashinfo")))

    def test_hostile_names_round_trip(self):
        names = ["new\nline", "-rf", "--", " space ", "100%", "a=b", "[Trash Info]", "ü ñ 名前", "%2e%2e", "tab\t",
                 "semi;colon", "back\\slash", "x" * 255, "é" * 127]
        folder = self.mkdir("home/h")
        for name in names:
            self.write(os.path.join("home/h", name), name.encode())
        raw = os.path.join(os.fsencode(folder), b"raw\xff")
        try:
            with open(raw, "wb") as handle:
                handle.write(os.fsencode("raw\udcff"))
            names.append(os.fsdecode(b"raw\xff"))
        except OSError:
            pass
        items = [self.trash.trash(os.path.join(folder, name)) for name in names]
        self.assertEqual(os.listdir(folder), [])
        for item in items:
            self.assertLessEqual(len(os.fsencode(item.name)) + len(".trashinfo"), 255)
            self.assertEqual(len(self.info(item).splitlines()), 3)
        listed = {item.original_path: item for item in self.trash.items()}
        self.assertEqual(sorted(listed), sorted(os.path.join(folder, name) for name in names))
        for path, item in listed.items():
            self.assertEqual(self.trash.restore(item), path)
        self.assertEqual(sorted(os.listdir(folder)), sorted(names))
        for name in names:
            self.assertEqual(self.read(os.path.join(folder, name)), os.fsencode(name))
        self.assertEqual(os.listdir(os.path.join(self.home_trash, "info")), [])

    def test_folders_and_links(self):
        folder = self.mkdir("home/project")
        self.write("home/project/a/b.txt", b"deep")
        target = self.write("home/target.txt", b"keep")
        link = self.path("home/link")
        os.symlink(target, link)
        broken = self.path("home/broken")
        os.symlink("/nowhere", broken)
        self.trash.trash(folder + "/")
        link_item = self.trash.trash(link)
        broken_item = self.trash.trash(broken)
        self.assertTrue(os.path.islink(link_item.files_path))
        self.assertEqual(self.read(target), b"keep")
        self.assertEqual(os.readlink(broken_item.files_path), "/nowhere")
        self.assertEqual(tree(os.path.join(self.home_trash, "files", "project")), {"a": {"b.txt": b"deep"}})

    def test_link_in_the_path_is_resolved_but_not_the_item(self):
        real = self.mkdir("home/real")
        self.write("home/real/f", b"1")
        os.symlink(real, self.path("home/alias"))
        item = self.trash.trash(self.path("home/alias/f"))
        self.assertEqual(item.original_path, os.path.join(real, "f"))

    def test_refusals(self):
        with self.assertRaises(FileNotFoundError):
            self.trash.trash(self.path("home/missing"))
        with self.assertRaises(TrashError):
            self.trash.trash("/")
        item = self.trash.trash(self.write("home/a"))
        for inside in (item.files_path, item.info_path, self.home_trash, os.path.dirname(self.home_trash),
                       os.path.join(self.home_trash, "files")):
            with self.assertRaises(TrashError, msg=inside):
                self.trash.trash(inside)
        self.assertTrue(os.path.exists(item.files_path))

    def test_rename_failure_leaves_no_record(self):
        path = self.write("home/a")
        original = os.rename
        os.rename = lambda *_args: (_ for _ in ()).throw(PermissionError(13, "denied"))
        try:
            with self.assertRaises(PermissionError):
                self.trash.trash(path)
        finally:
            os.rename = original
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.listdir(os.path.join(self.home_trash, "info")), [])

    def test_restore_conflict_replace_and_elsewhere(self):
        path = self.write("home/d/file", b"old")
        item = self.trash.trash(path)
        self.write("home/d/file", b"new")
        with self.assertRaises(FileExistsError):
            self.trash.restore(item)
        self.assertEqual(self.trash.restore(item, replace=True), path)
        self.assertEqual(self.read(path), b"old")
        item = self.trash.trash(path)
        other = self.path("home/d/sub/other name")
        self.assertEqual(self.trash.restore(item, other), other)
        self.assertEqual(self.read(other), b"old")
        self.assertEqual(self.trash.items(), [])

    def test_restore_never_replaces_a_folder(self):
        item = self.trash.trash(self.write("home/name", b"file"))
        self.mkdir("home/name")
        with self.assertRaises(FileExistsError):
            self.trash.restore(item, replace=True)
        self.assertTrue(os.path.isdir(self.path("home/name")))

    def test_restore_recreates_missing_folders(self):
        path = self.write("home/gone/deeper/file", b"1")
        item = self.trash.trash(path)
        os.rmdir(self.path("home/gone/deeper"))
        os.rmdir(self.path("home/gone"))
        self.assertEqual(self.trash.restore(item), path)
        self.assertEqual(self.read(path), b"1")


class VolumeTrashTest(TrashCase):
    def test_volume_gets_its_own_trash_with_relative_path(self):
        path = self.write("volA/data/x.bin", b"vol")
        item = self.trash.trash(path)
        expected = os.path.join(self.volumes[0], f".Trash-{os.getuid()}")
        self.assertEqual(item.trash_dir, expected)
        self.assertEqual(stat.S_IMODE(os.stat(expected).st_mode), 0o700)
        self.assertIn("Path=data/x.bin\n", self.info(item))
        self.assertFalse(os.path.exists(os.path.join(self.home_trash, "files", "x.bin")))
        listed = self.trash.items()
        self.assertEqual([(entry.trash_dir, entry.original_path) for entry in listed], [(expected, path)])
        self.assertEqual(self.trash.restore(listed[0]), path)

    def test_shared_trash_needs_the_sticky_bit(self):
        shared = os.path.join(self.volumes[0], ".Trash")
        os.mkdir(shared, 0o777)
        os.chmod(shared, 0o777)
        item = self.trash.trash(self.write("volA/one"))
        self.assertEqual(item.trash_dir, os.path.join(self.volumes[0], f".Trash-{os.getuid()}"))
        os.chmod(shared, 0o1777)
        item = self.trash.trash(self.write("volA/two"))
        self.assertEqual(item.trash_dir, os.path.join(shared, str(os.getuid())))
        self.assertEqual(sorted(entry.name for entry in self.trash.items()), ["one", "two"])

    def test_shared_trash_that_is_a_link_is_not_used(self):
        elsewhere = self.mkdir("elsewhere")
        os.chmod(elsewhere, 0o1777)
        os.symlink(elsewhere, os.path.join(self.volumes[0], ".Trash"))
        item = self.trash.trash(self.write("volA/one"))
        self.assertEqual(item.trash_dir, os.path.join(self.volumes[0], f".Trash-{os.getuid()}"))
        self.assertEqual(os.listdir(elsewhere), [])

    def test_own_trash_that_is_a_link_or_a_file_is_unavailable(self):
        own = os.path.join(self.volumes[0], f".Trash-{os.getuid()}")
        os.symlink(self.mkdir("elsewhere"), own)
        path = self.write("volA/one")
        with self.assertRaises(TrashUnavailable):
            self.trash.trash(path)
        os.unlink(own)
        open(own, "w").close()
        with self.assertRaises(TrashUnavailable):
            self.trash.trash(path)
        self.assertTrue(os.path.exists(path))

    def test_trash_of_another_user_is_not_used(self):
        other = self.make(uid=os.getuid() + 1)
        own = os.path.join(self.volumes[0], f".Trash-{os.getuid() + 1}")
        os.mkdir(own)                               # exists, but belongs to the user running the test
        with self.assertRaises(TrashUnavailable):
            other.trash(self.write("volA/one"))

    @unittest.skipIf(IS_ROOT, "root can write anywhere")
    def test_read_only_volume_has_no_trash(self):
        path = self.write("volB/one")
        os.chmod(self.volumes[1], 0o500)
        try:
            with self.assertRaises(TrashUnavailable):
                self.trash.trash(path)
        finally:
            os.chmod(self.volumes[1], 0o700)

    def test_records_that_leave_the_volume_are_refused(self):
        self.trash.trash(self.write("volA/real"))
        trash_dir = os.path.join(self.volumes[0], f".Trash-{os.getuid()}")
        victim = self.write("home/victim", b"safe")
        hostile = {"up": "../home/victim", "up2": "a/../../home/victim", "abs": victim,
                   "enc": "%2e%2e/home/victim", "self": ".", "empty": ""}
        for name, recorded in hostile.items():
            with open(os.path.join(trash_dir, "files", name), "w") as handle:
                handle.write("evil")
            with open(os.path.join(trash_dir, "info", name + ".trashinfo"), "w") as handle:
                handle.write(f"[Trash Info]\nPath={recorded}\nDeletionDate=2024-01-01T00:00:00\n")
        listed = self.trash.items()
        self.assertEqual([item.name for item in listed], ["real"])
        self.assertEqual(self.read(victim), b"safe")

    def test_absolute_record_inside_the_volume_is_accepted(self):
        self.trash.trash(self.write("volA/real"))
        trash_dir = os.path.join(self.volumes[0], f".Trash-{os.getuid()}")
        open(os.path.join(trash_dir, "files", "abs"), "w").close()
        with open(os.path.join(trash_dir, "info", "abs.trashinfo"), "w") as handle:
            handle.write(f"[Trash Info]\nPath={self.volumes[0]}/there\nDeletionDate=2024-01-01T00:00:00\n")
        self.assertIn(os.path.join(self.volumes[0], "there"), [item.original_path for item in self.trash.items()])

    def test_broken_records_are_skipped(self):
        self.trash.trash(self.write("home/good"))
        info = os.path.join(self.home_trash, "info")
        files = os.path.join(self.home_trash, "files")
        samples = {"nohdr": "Path=/a\n", "nopath": "[Trash Info]\nDeletionDate=2024-01-01T00:00:00\n",
                   "relative": "[Trash Info]\nPath=relative/path\n", "nul": "[Trash Info]\nPath=/a%00b\n",
                   "binary": "\0\1\2", "other": "[Other]\nPath=/x\n", "nofile": "[Trash Info]\nPath=/nofile\n"}
        for name, content in samples.items():
            with open(os.path.join(info, name + ".trashinfo"), "w") as handle:
                handle.write(content)
            if name != "nofile":
                open(os.path.join(files, name), "w").close()
        os.symlink("/etc/passwd", os.path.join(info, "link.trashinfo"))
        open(os.path.join(files, "link"), "w").close()
        open(os.path.join(info, "not-a-record.txt"), "w").close()
        with open(os.path.join(info, "nodate.trashinfo"), "w") as handle:
            handle.write("\n[Trash Info]\nFoo=bar\nPath=/somewhere/no%20date\nDeletionDate=garbage\n[Next]\nPath=/no\n")
        open(os.path.join(files, "nodate"), "w").close()
        listed = {item.name: item for item in self.trash.items()}
        self.assertEqual(sorted(listed), ["good", "nodate"])
        self.assertEqual(listed["nodate"].original_path, "/somewhere/no date")
        self.assertEqual(listed["nodate"].deleted, 0.0)

    def test_items_of_all_volumes_and_contains(self):
        self.trash.trash(self.write("home/h"))
        self.trash.trash(self.write("volA/a"))
        self.trash.trash(self.write("volB/b"))
        self.assertEqual(sorted(item.name for item in self.trash.items()), ["a", "b", "h"])
        self.assertEqual(len(self.trash.trash_dirs()), 3)
        self.assertTrue(self.trash.contains(os.path.join(self.home_trash, "files", "h")))
        self.assertTrue(self.trash.contains(os.path.join(self.volumes[0], f".Trash-{os.getuid()}")))
        self.assertFalse(self.trash.contains(self.volumes[0]))

    def test_no_trash_yet(self):
        self.assertEqual(self.trash.items(), [])
        self.assertEqual(self.trash.trash_dirs(), [])


class ParseTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_trashinfo("[Trash Info]\nPath=/a%20b/c%0Ad\nDeletionDate=2024-03-05T10:20:30\n"),
                         (b"/a b/c\nd", time.mktime((2024, 3, 5, 10, 20, 30, 0, 0, -1))))
        self.assertEqual(parse_trashinfo("[Trash Info]\r\nPath = /x \r\n")[0], b"/x")
        self.assertEqual(parse_trashinfo("[Trash Info]\nPath=/first\nPath=/second\n")[0], b"/first")
        self.assertEqual(parse_trashinfo("[Trash Info]\nPath=/%ff%fe\n")[0], b"/\xff\xfe")
        for bad in ("", "Path=/x", "[Trash Info]", "[trash info]\nPath=/x", "[Trash Info]\nPath=\n"):
            self.assertIsNone(parse_trashinfo(bad), bad)

    def test_find_topdir_of_a_real_folder(self):
        top = trash_module.find_topdir(os.path.dirname(os.path.abspath(__file__)))
        self.assertTrue(os.path.ismount(top))


if __name__ == "__main__":
    unittest.main()
