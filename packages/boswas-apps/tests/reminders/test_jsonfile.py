"""Shared JSON files: bounded reads, atomic writes, updates that do not lose each other."""

import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path

from boswas_apps.reminders import jsonfile


def _bump(path: str, times: int) -> None:
    def change(document):
        document = document or {"n": 0}
        document["n"] += 1
        return document
    for _ in range(times):
        jsonfile.update_json(path, change, sync=False)


class JsonFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_reads_as_none(self):
        self.assertIsNone(jsonfile.read_json(self.tmp / "none.json"))
        self.assertIsNone(jsonfile.signature(self.tmp / "none.json"))

    def test_write_creates_private_files_and_directories(self):
        target = self.tmp / "deep" / "er" / "store.json"
        jsonfile.write_json_atomic(target, {"a": [1, 2, "ä"]})
        self.assertEqual(jsonfile.read_json(target), {"a": [1, 2, "ä"]})
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(target.parent).st_mode), 0o700)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["store.json"])      # no temporary file left

    def test_a_replaced_file_has_a_new_signature(self):
        target = self.tmp / "store.json"
        jsonfile.write_json_atomic(target, {"a": 1})
        first = jsonfile.signature(target)
        jsonfile.write_json_atomic(target, {"a": 2})
        self.assertNotEqual(first, jsonfile.signature(target))

    def test_unreadable_documents_raise_a_plain_error(self):
        target = self.tmp / "store.json"
        for data in (b"{not json", b"\xff\xfe\x00", b"[" * 100_000):
            target.write_bytes(data)
            with self.assertRaises(jsonfile.StoreError) as caught:
                jsonfile.read_json(target)
            self.assertIn("store.json", str(caught.exception))
        target.write_bytes(b"1" * 2000)
        with self.assertRaises(jsonfile.StoreError):
            jsonfile.read_json(target, max_bytes=1000)

    def test_values_json_cannot_hold_are_refused_and_nothing_is_written(self):
        target = self.tmp / "store.json"
        jsonfile.write_json_atomic(target, {"ok": True})
        for bad in ({"x": float("nan")}, {"x": {1, 2}}):
            with self.assertRaises(jsonfile.StoreError):
                jsonfile.write_json_atomic(target, bad)
        self.assertEqual(jsonfile.read_json(target), {"ok": True})
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["store.json"])

    def test_write_into_a_read_only_directory_fails_plainly(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        locked = self.tmp / "locked"
        locked.mkdir(mode=0o500)
        with self.assertRaises(jsonfile.StoreError):
            jsonfile.write_json_atomic(locked / "store.json", {})

    def test_update_changes_under_the_lock(self):
        target = self.tmp / "store.json"
        result = jsonfile.update_json(target, lambda document: {"n": (document or {"n": 0})["n"] + 1})
        self.assertEqual(result, {"n": 1})
        self.assertEqual(jsonfile.update_json(target, lambda document: {"n": document["n"] + 1}), {"n": 2})
        self.assertEqual(json.loads(target.read_text()), {"n": 2})

    def test_update_without_a_change_does_not_touch_the_file(self):
        target = self.tmp / "store.json"
        jsonfile.write_json_atomic(target, {"n": 1})
        before = jsonfile.signature(target)
        jsonfile.update_json(target, lambda document: document)
        self.assertEqual(jsonfile.signature(target), before)

    def test_update_sets_a_damaged_file_aside(self):
        target = self.tmp / "store.json"
        target.write_text("{broken")
        result = jsonfile.update_json(target, lambda document: {"fresh": document is None})
        self.assertEqual(result, {"fresh": True})
        self.assertEqual((self.tmp / "store.json.damaged").read_text(), "{broken")

    def test_concurrent_updates_from_several_processes_are_all_kept(self):
        target = str(self.tmp / "store.json")
        context = multiprocessing.get_context("spawn")
        workers = [context.Process(target=_bump, args=(target, 40)) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(jsonfile.read_json(target), {"n": 240})


if __name__ == "__main__":
    unittest.main()
