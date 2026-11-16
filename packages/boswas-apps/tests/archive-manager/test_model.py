"""The folder tree of an archive and the texts of the list."""

from __future__ import annotations

import time
import unittest

import support  # noqa: F401
from boswas_apps.archive_manager import model
from boswas_apps.archive_manager.model import DIR, FILE, SYMLINK, Listing, make_entry


def entries(*specs):
    made = []
    for index, spec in enumerate(specs):
        name, kind, size = (spec + (FILE, 1))[:3] if isinstance(spec, tuple) else (spec, FILE, 1)
        made.append(make_entry(index, name, kind, size=size if kind == FILE else 0, packed=1 if kind == FILE else 0))
    return made


def names_in(listing, folder):
    return [(entry.base, entry.kind, entry.synthetic) for entry in listing.folder(folder)]


class ListingTest(unittest.TestCase):
    def test_tree_from_explicit_folders(self):
        listing = Listing.build(entries(("docs/", DIR), "docs/a.txt", ("docs/img/", DIR), "docs/img/b.png", "top.txt"))
        self.assertEqual(names_in(listing, ""), [("docs", DIR, False), ("top.txt", FILE, False)])
        self.assertEqual(names_in(listing, "docs"), [("a.txt", FILE, False), ("img", DIR, False)])
        self.assertEqual(names_in(listing, "docs/img"), [("b.png", FILE, False)])
        self.assertEqual((listing.totals.files, listing.totals.folders, listing.totals.size), (3, 2, 3))

    def test_folders_are_implied(self):
        listing = Listing.build(entries("a/b/c/file.txt", "a/other.txt"))
        self.assertEqual(names_in(listing, ""), [("a", DIR, True)])
        self.assertEqual(names_in(listing, "a"), [("b", DIR, True), ("other.txt", FILE, False)])
        self.assertEqual(names_in(listing, "a/b/c"), [("file.txt", FILE, False)])
        self.assertTrue(listing.has_folder("a/b"))
        self.assertFalse(listing.has_folder("a/x"))
        self.assertEqual(listing.folder("nope"), [])

    def test_folder_named_after_its_contents_replaces_the_implied_one(self):
        listing = Listing.build(entries("d/f.txt", ("d/", DIR), ("d/", DIR)))
        self.assertEqual(names_in(listing, ""), [("d", DIR, False)])
        self.assertEqual(names_in(listing, "d"), [("f.txt", FILE, False)])

    def test_odd_names(self):
        listing = Listing.build(entries("/abs/x", "./rel/./y", "../up/z", ("./", DIR), ("/", DIR), "dup", "dup"))
        self.assertEqual([entry[0] for entry in names_in(listing, "")], ["abs", "rel", "..", "dup", "dup"])
        self.assertEqual(names_in(listing, "../up"), [("z", FILE, False)])

    def test_file_and_folder_with_one_name(self):
        listing = Listing.build(entries("a", "a/b"))
        self.assertEqual(names_in(listing, ""), [("a", FILE, False), ("a", DIR, True)])

    def test_descendants(self):
        listing = Listing.build(entries(("d/", DIR), "d/1", "d/sub/2", "d/sub/deep/3", "other"))
        folder = listing.folder("")[0]
        self.assertEqual(sorted(entry.path for entry in listing.descendants(folder)), ["d", "d/1", "d/sub/2", "d/sub/deep/3"])
        implied = [entry for entry in listing.folder("d") if entry.base == "sub"][0]
        self.assertEqual(sorted(entry.path for entry in listing.descendants(implied)), ["d/sub/2", "d/sub/deep/3"])
        self.assertEqual(listing.folder_size("d"), 3)

    def test_search(self):
        listing = Listing.build(entries("Docs/ReadMe.txt", "src/readme.md", "src/main.c"))
        self.assertEqual(sorted(entry.path for entry in listing.search("README")), ["Docs/ReadMe.txt", "src/readme.md"])
        self.assertEqual([entry.path for entry in listing.search("docs")], ["Docs"])
        self.assertEqual(listing.search(""), [])
        self.assertEqual(len(listing.search("e", limit=2)), 2)

    def test_two_hundred_thousand_entries_build_quickly(self):
        many = [make_entry(index, f"dir{index % 500:03d}/sub{index % 7}/file{index:06d}.txt", FILE, size=10, packed=5)
                for index in range(200_000)]
        started = time.perf_counter()
        listing = Listing.build(many)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 10.0)
        self.assertEqual(len(listing.folder("")), 500)
        self.assertEqual(listing.totals.files, 200_000)
        self.assertEqual(listing.totals.size, 2_000_000)
        started = time.perf_counter()
        found = listing.search("file19999")
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertEqual(len(found), 10)

    def test_wide_folder_named_late_is_not_quadratic(self):
        count = 60_000
        many = [make_entry(index, f"d{index}/f", FILE) for index in range(count)]
        many += [make_entry(count + index, f"d{index}/", DIR) for index in range(count)]
        started = time.perf_counter()
        listing = Listing.build(many)
        self.assertLess(time.perf_counter() - started, 10.0)
        self.assertEqual(len(listing.folder("")), count)
        self.assertFalse(any(entry.synthetic for entry in listing.folder("")))


class TextTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(model.human_size(0), "0 bytes")
        self.assertEqual(model.human_size(1), "1 byte")
        self.assertEqual(model.human_size(1023), "1023 bytes")
        self.assertEqual(model.human_size(1536), "1.5 KB")
        self.assertEqual(model.human_size(1024 ** 2 * 250), "250 MB")
        self.assertEqual(model.human_size(5 * 1024 ** 3), "5.0 GB")
        self.assertEqual(model.human_size(-1), "—")
        self.assertEqual(model.human_size(None), "—")
        self.assertTrue(model.human_size(2 ** 70).endswith("PB"))

    def test_ratio(self):
        self.assertEqual(model.ratio_text(1000, 250), "25 %")
        self.assertEqual(model.ratio_text(0, 0), "—")
        self.assertEqual(model.ratio_text(10, 500), "100 %")

    def test_types_and_icons(self):
        def entry(name, kind=FILE, mode=None):
            return make_entry(0, name, kind, mode=mode)
        self.assertEqual(model.type_text(entry("a/b.PNG")), "PNG image")
        self.assertEqual(model.type_text(entry("x.weird")), "WEIRD file")
        self.assertEqual(model.type_text(entry("Makefile")), "File")
        self.assertEqual(model.type_text(entry("d/", DIR)), "Folder")
        self.assertEqual(model.type_text(entry("l", SYMLINK)), "Symbolic link")
        self.assertEqual(model.icon_name(entry("d/", DIR)), "folder")
        self.assertEqual(model.icon_name(entry("song.flac")), "audio-x-generic")
        self.assertEqual(model.icon_name(entry("run", mode=0o755)), "application-x-executable")
        self.assertEqual(model.icon_name(entry("x.weird")), "unknown")
        self.assertEqual(model.extension(".bashrc"), "")
        self.assertEqual(model.extension("a.tar.GZ"), "gz")

    def test_mode_crc_time(self):
        entry = make_entry(0, "f", FILE, mode=0o4755, crc=0xDEADBEEF, mtime=0.0)
        self.assertEqual(model.mode_text(entry), "-rwsr-xr-x")
        self.assertEqual(model.crc_text(entry), "DEADBEEF")
        self.assertRegex(model.time_text(entry.mtime), r"^19(69|70)-")
        self.assertEqual(model.time_text(None), "")
        self.assertEqual(model.time_text(1e30), "")
        self.assertEqual(model.mode_text(make_entry(0, "d", DIR, mode=0o755)), "drwxr-xr-x")
        self.assertEqual(model.mode_text(make_entry(0, "x", FILE)), "")


if __name__ == "__main__":
    unittest.main()
