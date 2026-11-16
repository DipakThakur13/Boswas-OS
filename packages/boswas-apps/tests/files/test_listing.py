"""Reading and sorting folders, searching, places, settings."""

import json
import os
import unittest

from support import IS_ROOT, TempCase

from boswas_apps.files import listing, places, search, settings


class ScanTest(TempCase):
    def names(self, path, **kwargs):
        return sorted(entry.name for batch in listing.scan(path, **kwargs) for entry in batch)

    def test_kinds_of_entries(self):
        folder = self.mkdir("d")
        self.write("d/file.txt", b"12345")
        self.mkdir("d/sub")
        os.symlink("file.txt", os.path.join(folder, "link"))
        os.symlink("sub", os.path.join(folder, "dirlink"))
        os.symlink("/nowhere/at/all", os.path.join(folder, "broken"))
        os.symlink("loop", os.path.join(folder, "loop"))
        os.mkfifo(os.path.join(folder, "pipe"))
        self.write("d/.hidden", b"")
        entries = {entry.name: entry for batch in listing.scan(folder) for entry in batch}
        self.assertEqual(sorted(entries), [".hidden", "broken", "dirlink", "file.txt", "link", "loop", "pipe", "sub"])
        self.assertEqual(entries["file.txt"].size, 5)
        self.assertFalse(entries["file.txt"].is_dir or entries["file.txt"].is_symlink)
        self.assertTrue(entries["sub"].is_dir)
        self.assertTrue(entries["link"].is_symlink and not entries["link"].broken)
        self.assertEqual(entries["link"].size, 5)
        self.assertTrue(entries["dirlink"].is_dir and entries["dirlink"].is_symlink)
        self.assertTrue(entries["broken"].broken and not entries["broken"].is_dir)
        self.assertTrue(entries["loop"].broken)
        self.assertTrue(entries[".hidden"].hidden)
        self.assertFalse(entries["pipe"].is_dir)

    def test_hostile_names(self):
        folder = self.mkdir("h")
        names = ["new\nline", "-rf", "--help", " leading", "trailing ", "tab\there", "\\back", "$(reboot)", "a;b",
                 "é" * 127, "x" * 255, "*", "?", "~", "%41"]
        for name in names:
            self.write(os.path.join("h", name))
        raw = os.path.join(os.fsencode(folder), b"bad\xff\xfe")
        try:
            open(raw, "wb").close()
            names.append(os.fsdecode(b"bad\xff\xfe"))
        except OSError:
            pass                                  # a file system that insists on UTF-8
        self.assertEqual(self.names(folder), sorted(names))
        for batch in listing.scan(folder):
            for entry in batch:
                self.assertEqual(os.lstat(entry.path).st_size, entry.size)

    def test_errors(self):
        with self.assertRaises(FileNotFoundError):
            list(listing.scan(self.path("missing")))
        with self.assertRaises(NotADirectoryError):
            list(listing.scan(self.write("plain")))

    @unittest.skipIf(IS_ROOT, "root reads every folder")
    def test_unreadable_folder(self):
        folder = self.mkdir("locked")
        os.chmod(folder, 0)
        with self.assertRaises(PermissionError):
            list(listing.scan(folder))

    def test_chunks_and_cancel(self):
        folder = self.mkdir("many")
        for number in range(250):
            open(os.path.join(folder, f"f{number}"), "wb").close()
        batches = list(listing.scan(folder, chunk=100))
        self.assertEqual([len(batch) for batch in batches], [100, 100, 50])
        seen = []
        for batch in listing.scan(folder, cancelled=lambda: len(seen) >= 1, chunk=100):
            seen.append(batch)
        self.assertEqual(len(seen), 1)

    def test_large_folder(self):
        folder = self.mkdir("big")
        for number in range(20000):
            os.close(os.open(os.path.join(folder, f"file{number}.dat"), os.O_CREAT | os.O_WRONLY))
        entries = [entry for batch in listing.scan(folder) for entry in batch]
        self.assertEqual(len(entries), 20000)
        listing.sort_entries(entries)
        self.assertEqual([entry.name for entry in entries[:3]], ["file0.dat", "file1.dat", "file2.dat"])
        self.assertEqual(entries[-1].name, "file19999.dat")

    def test_entry_for(self):
        path = self.write("one.txt", b"abc")
        entry = listing.entry_for(path)
        self.assertEqual((entry.name, entry.size, entry.is_dir), ("one.txt", 3, False))
        self.assertEqual(listing.entry_for(self.tmp + "/").name, os.path.basename(self.tmp))
        with self.assertRaises(OSError):
            listing.entry_for(self.path("nope"))


class SortTest(unittest.TestCase):
    def entries(self):
        E = listing.Entry
        return [E("b10.txt", "/b10.txt", size=5, mtime=30, type_text="Text"),
                E("b2.txt", "/b2.txt", size=50, mtime=10, type_text="Text"),
                E("Zeta", "/Zeta", is_dir=True, mtime=5, type_text="Folder"),
                E("alpha", "/alpha", is_dir=True, mtime=50, type_text="Folder"),
                E("a.png", "/a.png", size=7, mtime=20, type_text="Image")]

    def order(self, column, descending=False, **kwargs):
        return [entry.name for entry in listing.sort_entries(self.entries(), column, descending, **kwargs)]

    def test_by_name(self):
        self.assertEqual(self.order("name"), ["alpha", "Zeta", "a.png", "b2.txt", "b10.txt"])
        self.assertEqual(self.order("name", True), ["Zeta", "alpha", "b10.txt", "b2.txt", "a.png"])

    def test_by_size_date_type(self):
        self.assertEqual(self.order("size"), ["alpha", "Zeta", "b10.txt", "a.png", "b2.txt"])
        self.assertEqual(self.order("size", True), ["alpha", "Zeta", "b2.txt", "a.png", "b10.txt"])
        self.assertEqual(self.order("date"), ["Zeta", "alpha", "b2.txt", "a.png", "b10.txt"])
        self.assertEqual(self.order("date", True), ["alpha", "Zeta", "b10.txt", "a.png", "b2.txt"])
        self.assertEqual(self.order("type"), ["alpha", "Zeta", "a.png", "b2.txt", "b10.txt"])
        self.assertEqual(self.order("type", True), ["alpha", "Zeta", "b2.txt", "b10.txt", "a.png"])

    def test_folders_mixed_in(self):
        self.assertEqual(self.order("name", folders_first=False), ["a.png", "alpha", "b2.txt", "b10.txt", "Zeta"])


class SearchTest(TempCase):
    def found(self, query, **kwargs):
        root = self.path("s")
        return sorted(os.path.relpath(entry.path, root) for batch in search.search(root, query, **kwargs)
                      for entry in batch)

    def setUp(self):
        super().setUp()
        for name in ("Report 2024.pdf", "notes.txt", "deep/a/b/report-final.PDF", "deep/photo.jpg",
                     ".secret/report.txt", "deep/.hidden-report", "REPORTS/x.txt"):
            self.write(os.path.join("s", name))
        os.symlink(self.path("s"), self.path("s", "deep", "cycle"))

    def test_words_any_case_recursive(self):
        self.assertEqual(self.found("report"), ["REPORTS", "Report 2024.pdf", "deep/a/b/report-final.PDF"])
        self.assertEqual(self.found("  2024 report "), ["Report 2024.pdf"])
        self.assertEqual(self.found("zzz"), [])
        self.assertEqual(self.found("   "), [])

    def test_patterns(self):
        self.assertEqual(self.found("*.pdf"), ["Report 2024.pdf", "deep/a/b/report-final.PDF"])
        self.assertEqual(self.found("?otes.txt"), ["notes.txt"])

    def test_hidden(self):
        self.assertEqual(self.found("report", hidden=True),
                         [".secret/report.txt", "REPORTS", "Report 2024.pdf", "deep/.hidden-report",
                          "deep/a/b/report-final.PDF"])

    def test_links_are_not_followed_and_location_is_set(self):
        results = [entry for batch in search.search(self.path("s"), "cycle") for entry in batch]
        self.assertEqual([entry.name for entry in results], ["cycle"])
        self.assertEqual(results[0].location, self.path("s", "deep"))
        self.assertTrue(results[0].is_symlink)

    def test_cancel(self):
        for number in range(3000):
            self.write(f"s/bulk/match{number}")
        batches = 0
        for _batch in search.search(self.path("s"), "match", cancelled=lambda: batches >= 1):
            batches += 1
        self.assertLessEqual(batches, 2)

    def test_special_characters_are_literal(self):
        self.write("s/we[ird].txt")
        self.write("s/dollar$.txt")
        self.assertEqual(self.found("[ird]"), ["we[ird].txt"])
        self.assertEqual(self.found("$."), ["dollar$.txt"])


class PlacesTest(TempCase):
    def test_normalize(self):
        home = self.home
        self.assertEqual(places.normalize("~"), home)
        self.assertEqual(places.normalize("~/Documents/"), home + "/Documents")
        self.assertEqual(places.normalize("/usr//share/../lib/"), "/usr/lib")
        self.assertEqual(places.normalize("sub/x", "/base"), "/base/sub/x")
        self.assertEqual(places.normalize("x", places.TRASH), home + "/x")
        self.assertEqual(places.normalize("file:///tmp/a%20b"), "/tmp/a b")
        self.assertEqual(places.normalize(" Trash:/// "), places.TRASH)
        self.assertEqual(places.normalize("computer:"), places.COMPUTER)
        self.assertEqual(places.normalize("~user"), home + "/~user")
        for bad in ("", "   ", "a\0b", "file://server/share"):
            with self.assertRaises(ValueError):
                places.normalize(bad)

    def test_parent_and_title(self):
        self.assertEqual(places.parent("/a/b"), "/a")
        self.assertEqual(places.parent("/a"), "/")
        self.assertEqual(places.parent("/"), places.COMPUTER)
        self.assertEqual(places.parent(places.TRASH), places.COMPUTER)
        self.assertIsNone(places.parent(places.COMPUTER))
        self.assertEqual(places.title(self.home), "Home")
        self.assertEqual(places.title("/"), "File system")
        self.assertEqual(places.title("/a/b c"), "b c")
        self.assertEqual(places.title(places.TRASH), "Trash")

    def test_crumbs(self):
        home = self.home
        self.assertEqual(places.crumbs(home + "/Documents/Work"),
                         [("Home", home), ("Documents", home + "/Documents"), ("Work", home + "/Documents/Work")])
        self.assertEqual(places.crumbs("/usr/share"), [("This PC", places.COMPUTER), ("File system", "/"),
                                                       ("usr", "/usr"), ("share", "/usr/share")])
        self.assertEqual(places.crumbs("/"), [("This PC", places.COMPUTER), ("File system", "/")])
        volumes = [("/", "File system"), ("/media/me/STICK", "STICK"), ("/media", "Media")]
        self.assertEqual(places.crumbs("/media/me/STICK/a", volumes),
                         [("This PC", places.COMPUTER), ("STICK", "/media/me/STICK"), ("a", "/media/me/STICK/a")])
        self.assertEqual(places.crumbs(places.TRASH), [("Trash", places.TRASH)])
        self.assertEqual(places.crumbs(home + "x"), [("This PC", places.COMPUTER), ("File system", "/"),
                                                     *[(part, "/" + "/".join((home + "x").strip("/").split("/")[:i + 1]))
                                                       for i, part in enumerate((home + "x").strip("/").split("/"))]])

    def test_user_dirs(self):
        for name in ("Desktop", "Documents", "Musik", "Bilder"):
            os.makedirs(os.path.join(self.home, name))
        os.makedirs(self.path("elsewhere", "Vids"))
        config = self.write("home/.config/user-dirs.dirs", (
            '# comment\nXDG_DESKTOP_DIR="$HOME/Desktop"\nXDG_MUSIC_DIR="$HOME/Musik"\n'
            'XDG_PICTURES_DIR="$HOME/Bilder"\nXDG_VIDEOS_DIR="{}"\nXDG_DOWNLOAD_DIR="$HOME"\n'
            'XDG_TEMPLATES_DIR="$HOME/T"\nBROKEN LINE\nXDG_PUBLICSHARE_DIR=relative\n'
        ).format(self.path("elsewhere", "Vids")))
        self.assertTrue(os.path.exists(config))
        dirs = places.user_dirs()
        self.assertEqual([(label, os.path.relpath(path, self.tmp)) for label, path, _icon in dirs],
                         [("Desktop", "home/Desktop"), ("Documents", "home/Documents"), ("Musik", "home/Musik"),
                          ("Bilder", "home/Bilder"), ("Vids", "elsewhere/Vids")])
        self.assertEqual(dirs[0][2], "user-desktop")

    def test_user_dirs_without_configuration(self):
        os.makedirs(os.path.join(self.home, "Downloads"))
        self.assertEqual([label for label, _path, _icon in places.user_dirs()], ["Downloads"])


class SettingsTest(TempCase):
    def test_defaults_set_and_reload(self):
        path = self.path("cfg", "settings.json")
        store = settings.Settings(path)
        self.assertEqual(store.get("view_mode"), "details")
        self.assertTrue(store.set("view_mode", "icons-large"))
        self.assertTrue(store.set("show_hidden", True))
        self.assertFalse(store.set("view_mode", "hologram"))
        self.assertFalse(store.set("show_hidden", "yes"))
        self.assertFalse(store.set("nonsense", 1))
        self.assertFalse(store.set("start_folder", "relative/path"))
        self.assertTrue(store.set("start_folder", "/data"))
        again = settings.Settings(path)
        self.assertEqual((again.get("view_mode"), again.get("show_hidden"), again.get("start_folder")),
                         ("icons-large", True, "/data"))
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)

    def test_junk_is_ignored(self):
        for junk in ("{", "[1, 2]", '"text"', json.dumps({"view_mode": 5, "pinned": ["relative", 3],
                                                         "show_hidden": True, "navigation_width": True,
                                                         "column_widths": {"name": "wide"}, "extra": 1}),
                     "\0\0\0", "[" * 100000):
            path = self.write("cfg/settings.json", junk)
            store = settings.Settings(path)
            self.assertEqual(store.get("view_mode"), "details")
            self.assertEqual(store.get("pinned"), [])
            self.assertEqual(store.get("navigation_width"), 240)
            self.assertNotIn("extra", store.values)
        self.assertTrue(settings.Settings(self.write("cfg/settings.json", '{"show_hidden": true}')).get("show_hidden"))

    def test_pins(self):
        store = settings.Settings(self.path("cfg", "settings.json"))
        self.assertTrue(store.pin("/a"))
        self.assertTrue(store.pin("/b"))
        self.assertTrue(store.pin("/c"))
        self.assertFalse(store.pin("/a"))
        self.assertFalse(store.pin("relative"))
        self.assertTrue(store.move_pin("/c", 0))
        self.assertEqual(store.pinned(), ["/c", "/a", "/b"])
        self.assertTrue(store.move_pin("/c", 99))
        self.assertEqual(store.pinned(), ["/a", "/b", "/c"])
        self.assertTrue(store.unpin("/b"))
        self.assertFalse(store.unpin("/b"))
        self.assertEqual(settings.Settings(store.path).pinned(), ["/a", "/c"])

    def test_no_file_and_unwritable(self):
        store = settings.Settings(None)
        self.assertTrue(store.set("show_hidden", True))
        blocked = settings.Settings(self.write("file") + "/sub/settings.json")
        self.assertTrue(blocked.set("show_hidden", True))
        self.assertTrue(blocked.error)


if __name__ == "__main__":
    unittest.main()
