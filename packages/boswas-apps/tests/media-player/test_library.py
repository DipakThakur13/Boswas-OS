"""The library index: scanning, incremental rescans, grouping, covers (no Qt widgets)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402

from boswas_apps.media_player import library, tags  # noqa: E402


class LibraryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mp-library-")
        self.tmp = Path(self._tmp.name)
        self.music = self.tmp / "Music"
        self.music.mkdir()
        self.cancel = threading.Event()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def song(self, relative: str, title: str, artist: str = "Artist", album: str = "Album", track: int = 1,
             year: int = 2001, album_artist: str = "") -> Path:
        path = self.music / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        comments = [f"TITLE={title}", f"ARTIST={artist}", f"ALBUM={album}", f"TRACKNUMBER={track}", f"DATE={year}"]
        if album_artist:
            comments.append(f"ALBUMARTIST={album_artist}")
        return fakes.make_flac(path, comments)

    def scan(self, previous=None, kinds=("audio",), folders=None) -> library.ScanResult:
        return library.scan(folders or [str(self.music)], kinds, previous or {}, self.cancel)


class ScanTest(LibraryCase):
    def test_scan_reads_tags_and_skips_what_is_not_music(self) -> None:
        self.song("Kind of Blue/01 So What.flac", "So What", "Miles Davis", "Kind of Blue", 1, 1959)
        self.song("Kind of Blue/02 Freddie.flac", "Freddie Freeloader", "Miles Davis", "Kind of Blue", 2, 1959)
        (self.music / "notes.txt").write_text("x")
        (self.music / "film.mp4").write_bytes(b"")
        (self.music / ".trash").mkdir()
        self.song(".trash/gone.flac", "Gone")
        (self.music / ".hidden.flac").write_bytes(b"")
        result = self.scan()
        self.assertEqual(sorted(Path(p).name for p in result.index), ["01 So What.flac", "02 Freddie.flac"])
        song = result.index[str(self.music / "Kind of Blue/01 So What.flac")]
        self.assertEqual((song.title, song.artist, song.album, song.track, song.year, song.duration_ms, song.video),
                         ("So What", "Miles Davis", "Kind of Blue", 1, 1959, 3000, False))
        self.assertEqual((result.read, result.kept, result.removed), (2, 0, 0))
        videos = self.scan(kinds=("video",))
        self.assertEqual([Path(p).name for p in videos.index], ["film.mp4"])
        self.assertTrue(next(iter(videos.index.values())).video)

    def test_rescan_reads_only_what_changed(self) -> None:
        first = self.song("a.flac", "A")
        second = self.song("b.flac", "B")
        third = self.song("c.flac", "C")
        before = self.scan().index
        reads = []
        original = library.tags.read_tags
        library.tags.read_tags = lambda path: (reads.append(os.fspath(path)), original(path))[1]
        try:
            unchanged = self.scan(before)
            self.assertEqual((reads, unchanged.read, unchanged.kept), ([], 0, 3))
            fakes.make_flac(second, ["TITLE=B changed", "ARTIST=Someone else"])
            third.unlink()
            fourth = self.song("d.flac", "D")
            changed = self.scan(before)
        finally:
            library.tags.read_tags = original
        self.assertEqual(sorted(reads), sorted([str(second), str(fourth)]))
        self.assertEqual((changed.read, changed.kept, changed.removed), (2, 1, 1))
        self.assertEqual(changed.index[str(second)].title, "B changed")
        self.assertIs(changed.index[str(first)], before[str(first)])
        self.assertNotIn(str(third), changed.index)

    def test_links_loops_and_unreadable_folders(self) -> None:
        self.song("real/a.flac", "A")
        os.symlink(self.music / "real", self.music / "link-to-folder")
        os.symlink(self.music, self.music / "real/back-to-parent")
        os.symlink(self.music / "real/a.flac", self.music / "link-to-file.flac")
        result = self.scan(folders=[str(self.music), str(self.music / "real"), str(self.tmp / "missing"), "relative"])
        self.assertEqual(sorted(Path(p).name for p in result.index), ["a.flac", "link-to-file.flac"])
        self.assertEqual(result.unreadable, [str(self.tmp / "missing")])

    def test_cancel_keeps_the_previous_index(self) -> None:
        for number in range(60):
            self.song(f"{number:02d}.flac", f"Song {number}")
        before = self.scan().index
        seen = []

        def progress(count: int, path: str) -> None:
            seen.append(count)
            self.cancel.set()

        result = library.scan([str(self.music)], ("audio",), before, self.cancel, progress)
        self.assertTrue(result.cancelled)
        self.assertEqual(seen, [25])
        self.assertEqual(len(result.index), 60)
        self.assertEqual(result.removed, 0)

    def test_unreadable_file_gets_an_entry_without_tags(self) -> None:
        path = self.song("locked.flac", "Locked")
        os.chmod(path, 0)
        try:
            result = self.scan()
        finally:
            os.chmod(path, 0o600)
        song = result.index[str(path)]
        if os.geteuid() != 0:                       # root reads everything
            self.assertEqual((song.title, song.display_title), ("", "locked"))
        self.assertEqual(song.size, os.path.getsize(path))

    def test_index_file_round_trip_and_foreign_content(self) -> None:
        self.song("a.flac", "A", album_artist="Various")
        index = self.scan().index
        path = self.tmp / "state/library.json"
        library.save_index(path, index)
        loaded = library.load_index(path)
        self.assertEqual(loaded, index)
        self.assertEqual(self.scan(loaded).read, 0)
        raw = json.loads(path.read_text())
        raw["songs"] += [{"path": "relative.mp3"}, {"path": "/x.mp3", "title": 5, "year": 99999, "size": -1,
                                                    "duration_ms": "long", "video": "yes"}, 7, None]
        path.write_text(json.dumps(raw))
        loaded = library.load_index(path)
        self.assertEqual(sorted(loaded), sorted([*index, "/x.mp3"]))
        self.assertEqual((loaded["/x.mp3"].title, loaded["/x.mp3"].year, loaded["/x.mp3"].size, loaded["/x.mp3"].video),
                         ("", 0, 0, False))
        for content in ("[]", "{}", '{"version": 1, "songs": []}', '{"version": 2, "songs": {}}', "garbage"):
            path.write_text(content)
            self.assertEqual(library.load_index(path), {})


class GroupingTest(LibraryCase):
    def build(self) -> list[library.Song]:
        self.song("kob/1.flac", "So What", "Miles Davis", "Kind of Blue", 1, 1959)
        self.song("kob/2.flac", "Blue in Green", "Miles Davis", "Kind of Blue", 3, 1959)
        self.song("mix/1.flac", "The Chain", "Fleetwood Mac", "Mixtape", 2)
        self.song("mix/2.flac", "A Day in the Life", "The Beatles", "Mixtape", 1)
        self.song("comp/1.flac", "One", "Artist A", "Compilation", 1, album_artist="Various Artists")
        self.song("comp/2.flac", "Two", "Artist B", "Compilation", 2, album_artist="Various Artists")
        (self.music / "loose").mkdir()
        fakes.make_wav(self.music / "loose/untitled.wav", 0.1)
        fakes.make_wav(self.music / "other/untitled 2.wav", 0.1) if (self.music / "other").mkdir() is None else None
        return library.songs_of(self.scan().index)

    def test_songs_sorted_by_title_ignoring_articles(self) -> None:
        titles = [song.display_title for song in self.build()]
        self.assertEqual(titles, ["Blue in Green", "The Chain", "A Day in the Life", "One", "So What", "Two", "untitled",
                                  "untitled 2"])

    def test_albums(self) -> None:
        albums = library.albums_of(self.build())
        summary = [(album.title, album.artist, [song.display_title for song in album.songs]) for album in albums]
        self.assertIn(("Kind of Blue", "Miles Davis", ["So What", "Blue in Green"]), summary)
        self.assertIn(("Mixtape", "Various artists", ["A Day in the Life", "The Chain"]), summary)
        self.assertIn(("Compilation", "Various Artists", ["One", "Two"]), summary)
        unknown = [album for album in albums if album.title == library.UNKNOWN_ALBUM]
        self.assertEqual(len(unknown), 2)                      # one per folder, not one for everything
        self.assertEqual(next(a for a in albums if a.title == "Kind of Blue").year, 1959)

    def test_artists_and_search(self) -> None:
        songs = self.build()
        artists = library.artists_of(songs)
        names = [artist.name for artist in artists]
        self.assertEqual(names, ["The Beatles", "Fleetwood Mac", "Miles Davis", library.UNKNOWN_ARTIST,
                                 "Various Artists"])
        miles = artists[names.index("Miles Davis")]
        self.assertEqual(([song.display_title for song in miles.songs], len(miles.albums)),
                         (["So What", "Blue in Green"], 1))
        self.assertEqual([song.display_title for song in library.search(songs, "miles BLUE")],
                         ["Blue in Green", "So What"])
        self.assertEqual(len(library.search(songs, "  ")), len(songs))
        self.assertEqual(library.search(songs, "nothing matches this"), [])


class CoverTest(LibraryCase):
    def test_embedded_picture_wins_over_the_folder_image(self) -> None:
        jpeg, png = fakes.jpeg_bytes(), fakes.png_bytes()
        with_art = fakes.make_flac(self.music / "a.flac", ["TITLE=A"], cover=jpeg)
        without = self.song("b.flac", "B")
        self.assertIsNone(library.cover_bytes(without))
        (self.music / "Folder.PNG").write_bytes(png)
        self.assertEqual(library.cover_bytes(with_art), jpeg)
        self.assertEqual(library.cover_bytes(without), png)
        self.assertEqual(library.cover_bytes(with_art, embedded=False), png)

    def test_folder_image_must_be_an_image_of_sane_size(self) -> None:
        song = self.song("album/a.flac", "A")
        (self.music / "album/cover.jpg").write_text("<html>not a picture</html>")
        self.assertIsNone(library.cover_bytes(song))
        (self.music / "album/cover.jpg").write_bytes(fakes.jpeg_bytes())
        (self.music / "album/front.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 10)
        self.assertEqual(library.folder_cover(self.music / "album"), str(self.music / "album/cover.jpg"))
        self.assertIsNone(library.folder_cover(self.music / "missing"))
        original = library.MAX_COVER_BYTES
        library.MAX_COVER_BYTES = 10
        try:
            self.assertIsNone(library.cover_bytes(song))
        finally:
            library.MAX_COVER_BYTES = original

    def test_cache_key_and_prune(self) -> None:
        song = self.song("a.flac", "A")
        key = library.cache_key(song, "256")
        self.assertEqual(key, library.cache_key(song, "256"))
        self.assertNotEqual(key, library.cache_key(song, "64"))
        fakes.make_flac(song, ["TITLE=A much longer title than before"])
        self.assertNotEqual(key, library.cache_key(song, "256"))
        self.assertRegex(key, r"^[0-9a-f]{40}$")
        cache = self.tmp / "cache"
        cache.mkdir()
        for number in range(10):
            (cache / f"{number}.png").write_bytes(b"x")
            os.utime(cache / f"{number}.png", (number, number))
        self.assertEqual(library.prune_cache(cache, keep=4), 6)
        self.assertEqual(sorted(p.name for p in cache.iterdir()), ["6.png", "7.png", "8.png", "9.png"])
        _ = tags


if __name__ == "__main__":
    unittest.main()
