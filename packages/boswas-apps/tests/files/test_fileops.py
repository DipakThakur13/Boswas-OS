"""Copy, move, delete, trash, restore, undo: the operations engine."""

import errno
import os
import stat
import tempfile
import shutil
import unittest

from support import IS_ROOT, TempCase, tree

from boswas_apps.files import fileops
from boswas_apps.files.fileops import (CANCEL, KEEP_BOTH, REPLACE, SKIP, Delete, DeleteTrashed, Reporter, Restore,
                                       Transfer, TrashOp, UndoStack)
from boswas_apps.files.trash import Trash


class Recorder(Reporter):
    """Answers conflicts (from a list, or by the name in conflict) and records what the operation reported."""

    def __init__(self, answers=(), cancel_after=None, by_name=None):
        self.answers = list(answers)
        self.by_name = by_name or {}
        self.conflicts = []
        self.updates = []
        self.polls = 0
        self.cancel_after = cancel_after            # cancel at this many polls of cancelled()

    def progress(self, progress):
        self.updates.append((progress.items_done, progress.items_total, progress.bytes_done, progress.bytes_total,
                             progress.preparing))

    def conflict(self, conflict):
        self.conflicts.append(conflict)
        name = os.path.basename(conflict.destination)
        if name in self.by_name:
            return self.by_name[name], False
        return self.answers.pop(0)

    def cancelled(self):
        self.polls += 1
        return self.cancel_after is not None and self.polls > self.cancel_after


def exdev(*_args):
    raise OSError(errno.EXDEV, "Invalid cross-device link")


class OpsCase(TempCase):
    def setUp(self):
        super().setUp()
        self.src = self.mkdir("src")
        self.dst = self.mkdir("dst")

    def sample_tree(self):
        self.write("src/tree/a.txt", b"alpha")
        self.write("src/tree/sub/b.bin", b"\0" * 5000)
        self.write("src/tree/sub/deeper/c", b"")
        self.mkdir("src/tree/empty")
        os.symlink("a.txt", self.path("src/tree/link"))
        os.symlink("/nowhere", self.path("src/tree/broken"))
        os.symlink("sub", self.path("src/tree/dirlink"))
        return self.path("src/tree")

    def no_partials(self, root=None):
        for directory, _dirs, files in os.walk(root or self.tmp):
            for name in files:
                self.assertFalse(name.endswith(".boswas-part"), os.path.join(directory, name))


class CopyTest(OpsCase):
    def test_copy_tree_with_links_and_attributes(self):
        source = self.sample_tree()
        os.chmod(self.path("src/tree/a.txt"), 0o640)
        os.utime(self.path("src/tree/a.txt"), (1_000_000_000, 1_100_000_000))
        os.chmod(self.path("src/tree/sub"), 0o750)
        os.utime(self.path("src/tree/sub"), (1_000_000_000, 1_200_000_000))
        recorder = Recorder()
        result = Transfer([source], self.dst).run(recorder)
        self.assertTrue(result.ok, result.errors)
        copy = self.path("dst/tree")
        self.assertEqual(tree(copy), tree(source))
        self.assertEqual(result.affected, [copy])
        self.assertTrue(os.path.islink(os.path.join(copy, "link")))
        self.assertEqual(os.readlink(os.path.join(copy, "broken")), "/nowhere")
        self.assertTrue(os.path.islink(os.path.join(copy, "dirlink")))
        info = os.stat(os.path.join(copy, "a.txt"))
        self.assertEqual((stat.S_IMODE(info.st_mode), int(info.st_mtime)), (0o640, 1_100_000_000))
        info = os.stat(os.path.join(copy, "sub"))
        self.assertEqual((stat.S_IMODE(info.st_mode), int(info.st_mtime)), (0o750, 1_200_000_000))
        self.assertEqual(recorder.updates[-1][:4], (10, 10, 5005, 5005))
        self.assertTrue(recorder.updates[0][4])                 # "preparing" comes first
        self.assertEqual((result.undo.kind, result.undo.paths), ("copy", [copy]))
        self.no_partials()

    def test_executable_and_read_only_files(self):
        self.write("src/tool", b"#!/bin/sh\n", mode=0o755)
        self.write("src/locked", b"ro", mode=0o400)
        result = Transfer([self.path("src/tool"), self.path("src/locked")], self.dst).run()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(stat.S_IMODE(os.stat(self.path("dst/tool")).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(self.path("dst/locked")).st_mode), 0o400)

    def test_copy_into_the_same_folder_makes_a_copy_name(self):
        path = self.write("src/doc.txt", b"1")
        folder = self.mkdir("src/folder")
        for expected in ("doc - Copy.txt", "doc - Copy (2).txt"):
            result = Transfer([path], self.src).run(Recorder())
            self.assertEqual(result.affected, [os.path.join(self.src, expected)])
        result = Transfer([folder], self.src).run(Recorder())
        self.assertEqual(result.affected, [os.path.join(self.src, "folder - Copy")])

    def test_folder_into_itself_is_refused(self):
        folder = self.mkdir("src/a")
        inner = self.mkdir("src/a/b/c")
        os.symlink(inner, self.path("src/alias"))
        for destination in (folder, inner, self.path("src/alias")):
            for move in (False, True):
                result = Transfer([folder], destination, move=move).run(Recorder())
                self.assertEqual(len(result.errors), 1, destination)
                self.assertIn("inside this folder", result.errors[0][1])
        self.assertEqual(tree(folder), {"b": {"c": {}}})
        self.assertTrue(Transfer([self.write("src/a/file")], inner).run().ok)      # a file into a subfolder is fine

    def test_conflict_replace_skip_keep_both(self):
        for name in ("one", "two", "three"):
            self.write(f"src/{name}", f"new {name}")
            self.write(f"dst/{name}", f"old {name}")
        recorder = Recorder([(REPLACE, False), (SKIP, False), (KEEP_BOTH, False)])
        result = Transfer([self.path("src/one"), self.path("src/two"), self.path("src/three")], self.dst).run(recorder)
        self.assertTrue(result.ok)
        self.assertEqual(tree(self.dst), {"one": b"new one", "two": b"old two", "three": b"old three",
                                          "three (2)": b"new three"})
        self.assertEqual(result.skipped, 1)
        self.assertEqual(len(recorder.conflicts), 3)
        first = recorder.conflicts[0]
        self.assertEqual((first.source, first.destination, first.source_size, first.destination_size),
                         (self.path("src/one"), self.path("dst/one"), 7, 7))
        self.assertEqual(result.undo.paths, [self.path("dst/three (2)")])     # a replaced file is not undoable
        self.assertEqual(recorder.updates[-1][0], recorder.updates[-1][1])

    def test_do_this_for_all(self):
        for name in "abcd":
            self.write(f"src/{name}", "new")
            self.write(f"dst/{name}", "old")
        recorder = Recorder([(REPLACE, True)])
        result = Transfer([self.path(f"src/{name}") for name in "abcd"], self.dst).run(recorder)
        self.assertEqual(len(recorder.conflicts), 1)
        self.assertEqual(set(tree(self.dst).values()), {b"new"})
        self.assertIsNone(result.undo)

    def test_conflict_cancel(self):
        self.write("src/a", "new")
        self.write("dst/a", "old")
        self.write("src/b", "b")
        result = Transfer([self.path("src/a"), self.path("src/b")], self.dst).run(Recorder([(CANCEL, False)]))
        self.assertTrue(result.cancelled)
        self.assertEqual(tree(self.dst), {"a": b"old"})

    def test_merge_folders(self):
        self.write("src/f/same", "new")
        self.write("src/f/only-src", "s")
        self.write("src/f/sub/x", "sx")
        self.write("dst/f/same", "old")
        self.write("dst/f/only-dst", "d")
        self.write("dst/f/sub/y", "dy")
        recorder = Recorder(by_name={"f": REPLACE, "same": SKIP, "sub": REPLACE})
        result = Transfer([self.path("src/f")], self.dst).run(recorder)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(recorder.conflicts[0].destination, self.path("dst/f"))
        self.assertTrue(recorder.conflicts[0].source_is_dir and recorder.conflicts[0].destination_is_dir)
        self.assertEqual(len(recorder.conflicts), 3)
        self.assertEqual(tree(self.path("dst/f")), {"same": b"old", "only-src": b"s", "only-dst": b"d",
                                                    "sub": {"x": b"sx", "y": b"dy"}})
        self.assertEqual(tree(self.path("src/f")), {"same": b"new", "only-src": b"s", "sub": {"x": b"sx"}})
        self.assertIsNone(result.undo)
        self.assertEqual(recorder.updates[-1][0], recorder.updates[-1][1])

    def test_merge_asks_for_each_file(self):
        self.write("src/f/same", "new")
        self.write("src/f/sub/x", "sx")
        self.write("dst/f/same", "old")
        self.write("dst/f/sub/y", "dy")
        recorder = Recorder([(REPLACE, True)])
        Transfer([self.path("src/f")], self.dst).run(recorder)
        self.assertEqual(tree(self.path("dst/f")), {"same": b"new", "sub": {"x": b"sx", "y": b"dy"}})

    def test_file_replaces_folder_and_folder_replaces_file(self):
        self.write("src/a", "file")
        self.write("dst/a/inside", "x")
        self.write("src/b/inside", "y")
        self.write("dst/b", "file")
        result = Transfer([self.path("src/a"), self.path("src/b")], self.dst).run(Recorder([(REPLACE, True)]))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(tree(self.dst), {"a": b"file", "b": {"inside": b"y"}})

    def test_same_file_through_a_link_is_refused(self):
        path = self.write("src/a", "data")
        os.symlink(self.src, self.path("alias"))
        result = Transfer([], pairs=[(path, self.path("alias/a"))]).run(Recorder([(REPLACE, True)]))
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(self.read(path), b"data")

    def test_missing_source_and_destination(self):
        result = Transfer([self.path("src/ghost"), self.write("src/real")], self.dst).run()
        self.assertEqual([path for path, _message in result.errors], [self.path("src/ghost")])
        self.assertEqual(tree(self.dst), {"real": b"x"})
        result = Transfer([self.path("src/real")], self.path("no/such/folder")).run()
        self.assertEqual(len(result.errors), 1)

    def test_cancel_in_the_middle_of_a_file_leaves_no_partial(self):
        big = self.write("src/big", os.urandom(4 * 1024 * 1024))
        self.write("src/after", "x")
        recorder = Recorder(cancel_after=5)
        result = Transfer([big, self.path("src/after")], self.dst).run(recorder)
        self.assertTrue(result.cancelled)
        self.assertEqual(os.listdir(self.dst), [])
        self.no_partials()

    def test_hostile_names(self):
        names = ["new\nline", "-rf", "--version", "..data", "...", " ", "*", "x" * 255, "é" * 127, "a\tb", "$HOME",
                 "`id`", "it's", 'say "hi"']
        for name in names:
            self.write(os.path.join("src/h", name), name.encode())
        result = Transfer([self.path("src/h")], self.dst).run()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(tree(self.path("dst/h")), tree(self.path("src/h")))
        result = Transfer([os.path.join(self.path("src/h"), name) for name in names], self.dst).run()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(sorted(os.listdir(self.dst)), sorted(names + ["h"]))

    def test_special_files(self):
        os.mkfifo(self.path("src/pipe"), 0o600)
        import socket
        server = socket.socket(socket.AF_UNIX)
        server.bind(self.path("src/sock"))
        self.addCleanup(server.close)
        result = Transfer([self.path("src/pipe"), self.path("src/sock")], self.dst).run()
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.path("dst/pipe")).st_mode))
        self.assertEqual([path for path, _message in result.errors], [self.path("src/sock")])
        self.assertFalse(os.path.lexists(self.path("dst/sock")))

    def test_source_that_turns_into_a_link_is_not_followed(self):
        secret = self.write("secret", "secret")
        path = self.write("src/a", "data")
        original = os.open

        def swap(target, flags, *args, **kwargs):
            if target == path and os.path.isfile(path) and not os.path.islink(path):
                os.unlink(path)
                os.symlink(secret, path)
            return original(target, flags, *args, **kwargs)

        os.open = swap
        try:
            result = Transfer([path], self.dst).run()
        finally:
            os.open = original
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(os.listdir(self.dst), [])

    def test_write_error_is_reported_per_item(self):
        self.write("src/ok", "fine")
        bad = self.write("src/bad", "data")
        original = os.write

        def full(descriptor, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        operation = Transfer([bad], self.dst)
        os.write = full
        try:
            result = operation.run()
        finally:
            os.write = original
        self.assertEqual(result.errors, [(bad, "There is not enough free space.")])
        self.assertEqual(os.listdir(self.dst), [])
        self.assertTrue(Transfer([self.path("src/ok")], self.dst).run().ok)

    @unittest.skipIf(IS_ROOT, "root may read and write everything")
    def test_permission_errors(self):
        self.write("src/t/readable", "r")
        self.write("src/t/unreadable", "u", mode=0)
        self.write("src/t/closed/inside", "i")
        os.chmod(self.path("src/t/closed"), 0)
        result = Transfer([self.path("src/t")], self.dst).run()
        self.assertEqual(sorted(path for path, _m in result.errors),
                         [self.path("src/t/closed"), self.path("src/t/unreadable")])
        self.assertTrue(all(message == "You do not have permission." for _p, message in result.errors))
        self.assertEqual(self.read(self.path("dst/t/readable")), b"r")
        self.assertIsNone(result.undo)
        locked = self.mkdir("locked")
        os.chmod(locked, 0o500)
        result = Transfer([self.path("src/t/readable")], locked).run()
        self.assertEqual(len(result.errors), 1)
        self.no_partials()

    def test_deep_tree(self):
        path = self.path("src/deep")
        current = path
        for _level in range(150):
            current = os.path.join(current, "d")
        os.makedirs(current)
        with open(os.path.join(current, "leaf"), "w") as handle:
            handle.write("leaf")
        result = Transfer([path], self.dst).run()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(self.read(os.path.join(self.path("dst/deep"), *["d"] * 150, "leaf")), b"leaf")
        self.assertTrue(Delete([self.path("dst/deep")]).run().ok)
        self.assertFalse(os.path.exists(self.path("dst/deep")))

    def test_too_deep_tree_is_an_error_not_a_crash(self):
        path = self.path("src/deep")
        os.makedirs(os.path.join(path, *["d"] * 260))
        result = Transfer([path], self.dst).run()
        self.assertEqual(len(result.errors), 1)
        self.assertIn("nested too deeply", result.errors[0][1])
        self.assertFalse(result.cancelled)


class MoveTest(OpsCase):
    def test_move_is_a_rename(self):
        source = self.sample_tree()
        expected = tree(source)
        inode = os.stat(self.path("src/tree/sub/b.bin")).st_ino
        recorder = Recorder()
        result = Transfer([source], self.dst, move=True).run(recorder)
        self.assertTrue(result.ok, result.errors)
        self.assertFalse(os.path.exists(source))
        self.assertEqual(tree(self.path("dst/tree")), expected)
        self.assertEqual(os.stat(self.path("dst/tree/sub/b.bin")).st_ino, inode)
        self.assertEqual(recorder.updates[-1][:2], (1, 1))
        self.assertEqual(result.undo.pairs, [(source, self.path("dst/tree"))])

    def test_move_across_devices_copies_and_deletes(self):
        source = self.sample_tree()
        os.chmod(self.path("src/tree/a.txt"), 0o600)
        os.utime(self.path("src/tree/a.txt"), (1_000_000_000, 1_100_000_000))
        expected = tree(source)
        operation = Transfer([source], self.dst, move=True)
        operation.rename = exdev
        recorder = Recorder()
        result = operation.run(recorder)
        self.assertTrue(result.ok, result.errors)
        self.assertFalse(os.path.lexists(source))
        self.assertEqual(tree(self.path("dst/tree")), expected)
        info = os.stat(self.path("dst/tree/a.txt"))
        self.assertEqual((stat.S_IMODE(info.st_mode), int(info.st_mtime)), (0o600, 1_100_000_000))
        self.assertEqual(recorder.updates[-1][:4], (10, 10, 5005, 5005))
        self.assertEqual(result.undo.pairs, [(source, self.path("dst/tree"))])
        self.no_partials()

    def test_real_move_across_file_systems(self):
        if not os.path.isdir("/dev/shm") or os.stat("/dev/shm").st_dev == os.stat(self.tmp).st_dev:
            self.skipTest("no second file system to move to")
        try:
            other = tempfile.mkdtemp(prefix="files-test-", dir="/dev/shm")
        except OSError:
            self.skipTest("/dev/shm is not writable")
        self.addCleanup(shutil.rmtree, other, True)
        source = self.sample_tree()
        expected = tree(source)
        result = Transfer([source], other, move=True).run()
        self.assertTrue(result.ok, result.errors)
        self.assertFalse(os.path.lexists(source))
        self.assertEqual(tree(os.path.join(other, "tree")), expected)
        back = fileops.inverse(result.undo, Trash(self.path("trash"))).run()
        self.assertTrue(back.ok, back.errors)
        self.assertEqual(tree(source), expected)
        self.assertEqual(os.listdir(other), [])

    def test_cross_device_move_keeps_the_source_when_a_copy_fails(self):
        self.write("src/t/good", "g")
        bad = self.write("src/t/bad", "b")
        operation = Transfer([self.path("src/t")], self.dst, move=True)
        operation.rename = exdev
        original = os.open

        def refuse(target, flags, *args, **kwargs):
            if target == bad:
                raise PermissionError(errno.EACCES, "Permission denied")
            return original(target, flags, *args, **kwargs)

        os.open = refuse
        try:
            result = operation.run()
        finally:
            os.open = original
        self.assertEqual(result.errors, [(bad, "You do not have permission.")])
        self.assertEqual(tree(self.path("src/t")), {"bad": b"b"})
        self.assertEqual(tree(self.path("dst/t")), {"good": b"g"})
        self.assertIsNone(result.undo)

    def test_cross_device_cancel_keeps_everything_somewhere(self):
        self.write("src/t/one", os.urandom(3 * 1024 * 1024))
        self.write("src/t/two", os.urandom(3 * 1024 * 1024))
        before = tree(self.path("src/t"))
        operation = Transfer([self.path("src/t")], self.dst, move=True)
        operation.rename = exdev
        result = operation.run(Recorder(cancel_after=9))
        self.assertTrue(result.cancelled)
        left = tree(self.path("src/t"))
        moved = tree(self.path("dst/t")) if os.path.isdir(self.path("dst/t")) else {}
        self.assertEqual({**left, **moved}, before)
        self.no_partials()

    def test_move_within_the_same_folder_does_nothing(self):
        path = self.write("src/a", "1")
        result = Transfer([path], self.src, move=True).run(Recorder())
        self.assertTrue(result.ok)
        self.assertEqual(result.affected, [])
        self.assertEqual(tree(self.src), {"a": b"1"})

    def test_move_conflicts_and_merge(self):
        self.write("src/f/same", "new")
        self.write("src/f/extra", "e")
        self.write("dst/f/same", "old")
        self.write("src/single", "new")
        self.write("dst/single", "old")
        recorder = Recorder([(REPLACE, False), (SKIP, False), (KEEP_BOTH, False)])
        result = Transfer([self.path("src/f"), self.path("src/single")], self.dst, move=True).run(recorder)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(tree(self.dst), {"f": {"same": b"old", "extra": b"e"}, "single": b"old",
                                          "single (2)": b"new"})
        self.assertEqual(tree(self.src), {"f": {"same": b"new"}})          # the skipped file stays where it was
        self.assertEqual(result.undo.pairs, [(self.path("src/single"), self.path("dst/single (2)"))])
        self.assertEqual(recorder.updates[-1][0], recorder.updates[-1][1])

    def test_rename_error_other_than_exdev_is_reported(self):
        path = self.write("src/a", "1")
        operation = Transfer([path], self.dst, move=True)
        operation.rename = lambda *_a: (_ for _ in ()).throw(PermissionError(errno.EACCES, "Permission denied"))
        result = operation.run()
        self.assertEqual(result.errors, [(path, "You do not have permission.")])
        self.assertTrue(os.path.exists(path))

    def test_move_of_links_does_not_touch_targets(self):
        target = self.write("src/target", "t")
        os.symlink(target, self.path("src/link"))
        operation = Transfer([self.path("src/link")], self.dst, move=True)
        operation.rename = exdev
        self.assertTrue(operation.run().ok)
        self.assertEqual(os.readlink(self.path("dst/link")), target)
        self.assertEqual(self.read(target), b"t")


class DeleteTest(OpsCase):
    def test_delete_tree_without_following_links(self):
        keep = self.write("keep/file", "k")
        source = self.sample_tree()
        os.symlink(self.path("keep"), os.path.join(source, "outside"))
        recorder = Recorder()
        result = Delete([source, self.write("src/single")]).run(recorder)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(os.listdir(self.src), [])
        self.assertEqual(self.read(keep), b"k")
        self.assertEqual(recorder.updates[-1][:2], (12, 12))
        self.assertIsNone(result.undo)

    def test_read_only_folders_of_the_user_are_emptied(self):
        self.write("src/ro/inner/file", "x")
        os.chmod(self.path("src/ro/inner"), 0o500)
        os.chmod(self.path("src/ro"), 0o500)
        result = Delete([self.path("src/ro")]).run()
        self.assertTrue(result.ok, result.errors)
        self.assertFalse(os.path.exists(self.path("src/ro")))

    def test_errors_are_collected_and_the_rest_is_deleted(self):
        self.write("src/t/a", "a")
        stubborn = self.write("src/t/stubborn", "s")
        self.write("src/t/z", "z")
        original = os.unlink

        def refuse(path, *args, **kwargs):
            if path == stubborn:
                raise OSError(errno.EBUSY, "Device or resource busy")
            return original(path, *args, **kwargs)

        os.unlink = refuse
        try:
            result = Delete([self.path("src/t"), self.path("src/missing")]).run()
        finally:
            os.unlink = original
        self.assertEqual(sorted(result.errors), sorted([(stubborn, "It is in use."),
                                                       (self.path("src/missing"), "It no longer exists.")]))
        self.assertEqual(tree(self.path("src/t")), {"stubborn": b"s"})

    def test_cancel(self):
        for number in range(300):
            self.write(f"src/many/f{number}")
        result = Delete([self.path("src/many")]).run(Recorder(cancel_after=450))
        self.assertTrue(result.cancelled)
        self.assertTrue(0 < len(os.listdir(self.path("src/many"))) < 300)

    def test_hostile_names(self):
        for name in ("-rf", "--no-preserve-root", "new\nline", "*", ".", ".."):
            if name not in (".", ".."):
                self.write(os.path.join("src/h", name))
        keep = self.write("src/keep")
        self.assertTrue(Delete([self.path("src/h")]).run().ok)
        self.assertTrue(os.path.exists(keep))


class TrashAndUndoTest(OpsCase):
    def setUp(self):
        super().setUp()
        self.volume = self.mkdir("vol")

        def topdir(directory):
            directory = os.path.realpath(directory)
            return self.volume if (directory + "/").startswith(self.volume + "/") else self.tmp

        self.trash = Trash(self.path("home/.local/share/Trash"), topdir_of=topdir,
                           mount_points=lambda: [self.tmp, self.volume])
        self.stack = UndoStack()

    def undo(self):
        record = self.stack.undos.pop()
        result = fileops.inverse(record, self.trash).run(Recorder())
        self.stack.pushed_back(result.undo, redo=True)
        return result

    def redo(self):
        record = self.stack.redos.pop()
        result = fileops.inverse(record, self.trash).run(Recorder())
        self.stack.pushed_back(result.undo, redo=False)
        return result

    def test_trash_restore_and_undo_redo(self):
        a = self.write("src/a", "A")
        folder = self.mkdir("src/folder")
        self.write("src/folder/x", "X")
        result = TrashOp([a, folder], self.trash).run(Recorder())
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(os.listdir(self.src), [])
        self.assertEqual(len(self.trash.items()), 2)
        self.stack.push(result.undo)
        self.assertTrue(self.undo().ok)
        self.assertEqual(tree(self.src), {"a": b"A", "folder": {"x": b"X"}})
        self.assertEqual(self.trash.items(), [])
        self.assertTrue(self.redo().ok)
        self.assertEqual(os.listdir(self.src), [])
        self.assertTrue(self.undo().ok)
        self.assertEqual(tree(self.src), {"a": b"A", "folder": {"x": b"X"}})

    def test_trash_reports_missing_and_volume_without_trash(self):
        blocked = os.path.join(self.volume, f".Trash-{os.getuid()}")
        open(blocked, "w").close()
        on_volume = self.write("vol/data")
        result = TrashOp([self.path("src/ghost"), on_volume, self.write("src/ok")], self.trash).run()
        self.assertEqual(result.errors, [(self.path("src/ghost"), "It no longer exists.")])
        self.assertEqual(result.untrashable, [on_volume])
        self.assertEqual([item.name for item in result.undo.items], ["ok"])

    def test_restore_conflicts(self):
        paths = [self.write(f"src/{name}", "old") for name in ("a", "b", "c")]
        items = TrashOp(paths, self.trash).run().undo.items
        for path in paths:
            self.write(os.path.relpath(path, self.tmp), "new")
        recorder = Recorder([(REPLACE, False), (SKIP, False), (KEEP_BOTH, False)])
        result = Restore(items, self.trash).run(recorder)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(tree(self.src), {"a": b"old", "b": b"new", "c": b"new", "c (2)": b"old"})
        self.assertEqual([item.name for item in self.trash.items()], ["b"])
        self.assertEqual(result.skipped, 1)

    def test_restore_from_a_trash_on_another_file_system(self):
        path = self.write("src/far/file", "data")
        item = self.trash.trash(self.path("src/far"))
        original = os.rename
        os.rename = exdev
        Transfer.rename = staticmethod(exdev)
        try:
            result = Restore([item], self.trash).run(Recorder())
        finally:
            os.rename = original
            Transfer.rename = staticmethod(os.rename)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(self.read(path), b"data")
        self.assertEqual(self.trash.items(), [])
        self.assertEqual(os.listdir(os.path.join(self.trash.home_trash, "files")), [])

    def test_empty_and_delete_from_trash(self):
        for name in ("a", "b", "c"):
            self.trash.trash(self.write(f"src/{name}"))
        self.write("src/d/inner", "x")
        self.trash.trash(self.path("src/d"))
        self.trash.trash(self.write("vol/v"))
        chosen = [item for item in self.trash.items() if item.name in ("a", "d")]
        result = DeleteTrashed(self.trash, chosen).run(Recorder())
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(sorted(item.name for item in self.trash.items()), ["b", "c", "v"])
        sizes = os.path.join(self.trash.home_trash, "directorysizes")
        open(sizes, "w").close()
        recorder = Recorder()
        result = DeleteTrashed(self.trash).run(recorder)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(self.trash.items(), [])
        self.assertFalse(os.path.exists(sizes))
        self.assertEqual(recorder.updates[-1][:2], (3, 3))
        for trash_dir, _top in self.trash.trash_dirs():
            self.assertEqual(os.listdir(os.path.join(trash_dir, "files")), [])
            self.assertEqual(os.listdir(os.path.join(trash_dir, "info")), [])

    def test_undo_rename_move_copy_create(self):
        path = self.write("src/old.txt", "1")
        renamed, record = fileops.rename(path, "new name.txt")
        self.assertEqual(renamed, self.path("src/new name.txt"))
        self.stack.push(record)
        moved = Transfer([renamed], self.dst, move=True).run()
        self.stack.push(moved.undo)
        copied = Transfer([self.path("dst/new name.txt")], self.src).run()
        self.stack.push(copied.undo)
        folder, record = fileops.create_folder(self.src)
        self.stack.push(record)
        self.assertEqual(self.stack.undo_label(), "New folder")
        self.assertEqual(tree(self.tmp)["src"], {"new name.txt": b"1", "New folder": {}})
        self.assertTrue(self.undo().ok)                         # the new folder goes to the Trash
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(self.undo().ok)                         # the copy goes to the Trash
        self.assertEqual(os.listdir(self.src), [])
        self.assertTrue(self.undo().ok)                         # the move goes back
        self.assertEqual(tree(self.src), {"new name.txt": b"1"})
        self.assertTrue(self.undo().ok)                         # the rename goes back
        self.assertEqual(tree(self.src), {"old.txt": b"1"})
        self.assertFalse(self.stack.can_undo())
        for _step in range(4):
            self.assertTrue(self.redo().ok)
        self.assertEqual(tree(self.src), {"new name.txt": b"1", "New folder": {}})
        self.assertEqual(tree(self.dst), {"new name.txt": b"1"})
        self.assertFalse(self.stack.can_redo())

    def test_undo_never_overwrites(self):
        path = self.write("src/a", "mine")
        result = Transfer([path], self.dst, move=True).run()
        self.write("src/a", "somebody else's")
        back = fileops.inverse(result.undo, self.trash).run(Recorder())
        self.assertEqual(len(back.errors), 1)
        self.assertIn("in the way", back.errors[0][1])
        self.assertEqual(self.read(path), b"somebody else's")
        self.assertEqual(self.read(self.path("dst/a")), b"mine")

    def test_a_new_action_clears_redo(self):
        self.stack.push(fileops.Undo("rename", "Rename", pairs=[("/a", "/b")]))
        self.stack.pushed_back(fileops.Undo("rename", "Rename", pairs=[("/b", "/a")]), redo=True)
        self.assertTrue(self.stack.can_redo())
        self.stack.push(None)
        self.assertTrue(self.stack.can_redo())
        self.stack.push(fileops.Undo("create", "New folder", paths=["/c"]))
        self.assertFalse(self.stack.can_redo())
        for number in range(200):
            self.stack.push(fileops.Undo("create", str(number), paths=["/c"]))
        self.assertEqual(len(self.stack.undos), 50)


class SmallOperationsTest(OpsCase):
    def test_rename(self):
        path = self.write("src/a.txt", "1")
        self.write("src/taken", "2")
        for bad in ("", ".", "..", "x/y", "a\0", "n" * 256, "a.txt"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                fileops.rename(path, bad)
        with self.assertRaises(FileExistsError):
            fileops.rename(path, "taken")
        with self.assertRaises(FileNotFoundError):
            fileops.rename(self.path("src/ghost"), "x")
        new, undo = fileops.rename(path, "-rf\n")
        self.assertEqual(new, self.path("src/-rf\n"))
        self.assertEqual(undo.pairs, [(path, new)])
        self.assertEqual(self.read(self.path("src/taken")), b"2")

    def test_rename_of_a_link_renames_the_link(self):
        target = self.write("src/target", "t")
        os.symlink(target, self.path("src/link"))
        new, _undo = fileops.rename(self.path("src/link"), "moved-link")
        self.assertTrue(os.path.islink(new))
        self.assertEqual(self.read(target), b"t")

    def test_create_folder_and_file(self):
        self.assertEqual(os.path.basename(fileops.create_folder(self.src)[0]), "New folder")
        self.assertEqual(os.path.basename(fileops.create_folder(self.src)[0]), "New folder (2)")
        self.assertEqual(os.path.basename(fileops.create_file(self.src)[0]), "New text document.txt")
        self.assertEqual(os.path.basename(fileops.create_file(self.src)[0]), "New text document (2).txt")
        with self.assertRaises(OSError):
            fileops.create_folder(self.path("missing"))

    def test_describe_error(self):
        self.assertEqual(fileops.describe_error(PermissionError(errno.EACCES, "x")), "You do not have permission.")
        self.assertEqual(fileops.describe_error(OSError(errno.EROFS, "x")), "The volume is read-only.")
        self.assertEqual(fileops.describe_error(OSError(9999, "Strange")), "Strange.")
        self.assertEqual(fileops.describe_error(ValueError("plain")), "plain")

    def test_measure(self):
        self.write("src/m/a", b"123")
        self.write("src/m/d/b", b"4567")
        os.symlink(self.tmp, self.path("src/m/out"))
        self.assertEqual(fileops.measure(self.path("src/m")), (5, 7))
        self.assertEqual(fileops.measure(self.path("src/ghost")), (1, 0))


if __name__ == "__main__":
    unittest.main()
