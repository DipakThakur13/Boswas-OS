"""Playlist files, the play queue, settings and what is remembered between runs (no Qt)."""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boswas_apps.media_player import model, opening, playlist_io  # noqa: E402
from boswas_apps.media_player.model import PlayQueue, Track  # noqa: E402


def tracks(count: int) -> list[Track]:
    return [Track(f"/music/{n}.mp3", f"Track {n}") for n in range(count)]


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mp-model-")
        self.tmp = Path(self._tmp.name)
        self._home = os.environ.get("HOME")
        self._config = os.environ.get("XDG_CONFIG_HOME")
        os.environ["HOME"] = str(self.tmp / "home")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "home/.config")
        (self.tmp / "home/.config").mkdir(parents=True)

    def tearDown(self) -> None:
        for name, value in (("HOME", self._home), ("XDG_CONFIG_HOME", self._config)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()


class PlaylistIoTest(TmpCase):
    def test_m3u_extended(self) -> None:
        text = ("#EXTM3U\n#EXTINF:215,Miles Davis - So What\n/music/So What.flac\n"
                "#EXTINF:-1,Radio\nhttps://radio.example.org/stream.mp3\n\n# a comment\nsub/relative.mp3\n"
                "..\\other\\back.mp3\n")
        entries = playlist_io.parse_m3u(text, "/lists/jazz")
        self.assertEqual([entry.location for entry in entries],
                         ["/music/So What.flac", "https://radio.example.org/stream.mp3",
                          "/lists/jazz/sub/relative.mp3", "/lists/other/back.mp3"])
        self.assertEqual((entries[0].title, entries[0].duration_s), ("Miles Davis - So What", 215))
        self.assertEqual((entries[1].title, entries[1].duration_s), ("Radio", 0))
        self.assertEqual(entries[2].title, "")

    def test_schemes_and_foreign_paths_are_dropped(self) -> None:
        text = "\n".join(["rtsp://cam/1", "smb://nas/a.mp3", "javascript:alert(1)", "C:\\Music\\a.mp3",
                          "file:///music/a%20b.mp3", "file://remote/music/x.mp3", "http://", "data:audio/mp3;base64,AA",
                          "/ok.mp3"])
        self.assertEqual([entry.location for entry in playlist_io.parse_m3u(text, "/base")],
                         ["/base/javascript:alert(1)", "/music/a b.mp3", "/base/data:audio/mp3;base64,AA", "/ok.mp3"])

    def test_relative_entries_need_a_folder(self) -> None:
        self.assertEqual(playlist_io.parse_m3u("a.mp3\n/b.mp3\n", None)[0].location, "/b.mp3")

    def test_pls(self) -> None:
        text = ("[playlist]\nFile2=/b.ogg\nTitle2=Second\nLength2=120\nFile1=http://example.org/a\nTitle1=First\n"
                "Length1=-1\nFile999999999=/x.mp3\nNumberOfEntries=2\nVersion=2\n[other]\nFile3=/c.mp3\n")
        entries = playlist_io.parse_pls(text, "/lists")
        self.assertEqual([(entry.location, entry.title, entry.duration_s) for entry in entries],
                         [("http://example.org/a", "First", 0), ("/b.ogg", "Second", 120)])

    def test_round_trip(self) -> None:
        entries = [playlist_io.Entry("/music/a b.mp3", "Artist - Title", 61),
                   playlist_io.Entry("https://example.org/s", "", 0),
                   playlist_io.Entry("/music/bad\nname.mp3", "x", 1)]
        for name in ("x.m3u8", "x.m3u", "x.pls"):
            data = playlist_io.dump(entries, name)
            back = playlist_io.parse(data, name, "/elsewhere")
            self.assertEqual([entry.location for entry in back], ["/music/a b.mp3", "https://example.org/s"], name)
            self.assertEqual((back[0].title, back[0].duration_s), ("Artist - Title", 61))
        relative = playlist_io.dump_m3u([playlist_io.Entry("/lists/sub/a.mp3"), playlist_io.Entry("/other/b.mp3")],
                                        "/lists")
        self.assertIn("\nsub/a.mp3\n", relative)
        self.assertIn("\n/other/b.mp3\n", relative)

    def test_encodings_and_caps(self) -> None:
        latin = "#EXTM3U\n/música/canción.mp3\n".encode("latin-1")
        self.assertEqual(playlist_io.parse(latin, "old.m3u")[0].location, "/música/canción.mp3")
        utf8 = b"\xef\xbb\xbf/m\xc3\xbasica/a.mp3\n"
        self.assertEqual(playlist_io.parse(utf8, "new.m3u8")[0].location, "/música/a.mp3")
        with self.assertRaises(playlist_io.PlaylistError):
            playlist_io.parse(b"x" * (playlist_io.MAX_FILE_BYTES + 1), "big.m3u")
        with self.assertRaises(playlist_io.PlaylistError):
            playlist_io.parse(b"/a.mp3\n", "list.txt")
        many = "\n".join(f"/m/{n}.mp3" for n in range(playlist_io.MAX_ENTRIES + 50))
        self.assertEqual(len(playlist_io.parse_m3u(many)), playlist_io.MAX_ENTRIES)
        long_line = "/" + "a" * (playlist_io.MAX_LINE + 1) + ".mp3\n/short.mp3\n"
        self.assertEqual([entry.location for entry in playlist_io.parse_m3u(long_line)], ["/short.mp3"])
        self.assertEqual(playlist_io.parse_m3u("#EXTINF:1e999,x\n/a.mp3\n#EXTINF:nan,y\n/b.mp3\n")[0].duration_s, 0)

    def test_load_reads_relative_to_the_file(self) -> None:
        folder = self.tmp / "lists"
        folder.mkdir()
        (folder / "a.m3u8").write_text("song.mp3\n", "utf-8")
        self.assertEqual(playlist_io.load(folder / "a.m3u8")[0].location, str(folder / "song.mp3"))


class QueueTest(unittest.TestCase):
    def test_linear_order_and_repeat(self) -> None:
        queue = PlayQueue()
        queue.set_items(tracks(3))
        self.assertEqual((queue.index, queue.next_index(), queue.previous_index()), (0, 1, -1))
        queue.index = 2
        self.assertEqual(queue.next_index(), -1)
        self.assertFalse(queue.has_next())
        queue.set_repeat("all")
        self.assertEqual(queue.next_index(), 0)
        queue.index = 0
        self.assertEqual(queue.previous_index(), 2)
        queue.set_repeat("one")
        self.assertEqual(queue.next_index(auto=True), 0)       # the track ended: again
        self.assertEqual(queue.next_index(), 1)                # the user pressed next: on
        self.assertEqual(queue.cycle_repeat(), "off")
        queue.set_repeat("nonsense")
        self.assertEqual(queue.repeat, "off")

    def test_shuffle_plays_everything_once_and_starts_with_the_current(self) -> None:
        queue = PlayQueue(random.Random(1))
        queue.set_items(tracks(20), start=5)
        queue.set_shuffle(True)
        seen = [queue.index]
        while (index := queue.next_index()) >= 0:
            queue.index = index
            seen.append(index)
        self.assertEqual(seen[0], 5)
        self.assertEqual(sorted(seen), list(range(20)))
        self.assertNotEqual(seen, sorted(seen))
        back = queue.previous_index()
        self.assertEqual(back, seen[-2])
        queue.set_repeat("all")
        again = queue.next_index()
        self.assertNotEqual(again, seen[-1])                   # the new round does not repeat the last track

    def test_editing_keeps_the_current_track(self) -> None:
        queue = PlayQueue(random.Random(3))
        queue.set_items(tracks(5), start=2)
        current = queue.current()
        queue.remove(0)
        self.assertIs(queue.current(), current)
        queue.move(1, 3)
        self.assertIs(queue.current(), current)
        self.assertEqual(queue.index, 3)
        queue.move(0, 4)
        self.assertIs(queue.current(), current)
        first = queue.insert_next(tracks(2))
        self.assertEqual(first, queue.index + 1)
        self.assertIs(queue.current(), current)
        queue.remove(queue.index)
        self.assertEqual(queue.current().title, "Track 0")     # the one that followed
        queue.remove(99)
        queue.move(0, 99)
        while len(queue):
            queue.remove(len(queue) - 1)
        self.assertEqual((queue.index, queue.current()), (-1, None))

    def test_shuffle_order_survives_edits(self) -> None:
        queue = PlayQueue(random.Random(5))
        queue.set_items(tracks(6))
        queue.set_shuffle(True)
        queue.append(tracks(3))
        queue.insert_next([Track("/next.mp3", "Next")])
        self.assertEqual(queue.items[queue.next_index()].title, "Next")
        queue.remove(4)
        order = [queue.index]
        while (index := queue.next_index()) >= 0:
            queue.index = index
            order.append(index)
        self.assertEqual(sorted(order), list(range(len(queue))))

    def test_capacity(self) -> None:
        queue = PlayQueue()
        queue.set_items([Track(f"/{n}") for n in range(model.MAX_QUEUE - 1)])
        self.assertEqual(queue.append(tracks(5)), model.MAX_QUEUE - 1)
        self.assertEqual(len(queue), model.MAX_QUEUE)
        self.assertEqual(queue.append(tracks(1)), -1)
        self.assertEqual(queue.insert_next(tracks(1)), -1)

    def test_storage_rejects_foreign_data(self) -> None:
        queue = PlayQueue()
        queue.set_items(tracks(3), start=1)
        queue.set_repeat("all")
        data = json.loads(json.dumps(queue.to_dict()))
        other = PlayQueue()
        other.load_dict(data)
        self.assertEqual(([t.location for t in other.items], other.index, other.repeat), (
            [t.location for t in queue.items], 1, "all"))
        hostile = {"items": [{"location": "relative.mp3"}, {"location": "ftp://x/y"}, 5, None,
                             {"location": "/ok.mp3", "title": 7, "duration_ms": "long", "video": "yes"},
                             {"location": "/a\0b.mp3"}, {"location": "https://example.org/a", "duration_ms": -5}],
                   "index": 99, "repeat": ["all"], "shuffle": "true"}
        other.load_dict(hostile)
        self.assertEqual([t.location for t in other.items], ["/ok.mp3", "https://example.org/a"])
        self.assertEqual((other.items[0].title, other.items[0].duration_ms, other.items[0].video), ("", 0, False))
        self.assertEqual((other.index, other.repeat, other.shuffle), (1, "off", False))
        for junk in (None, [], "x", {"items": "abc"}, {"items": [], "index": True}):
            other.load_dict(junk)
            self.assertEqual((len(other), other.index), (0, -1))


class TrackTest(unittest.TestCase):
    def test_classify_and_titles(self) -> None:
        self.assertEqual(model.classify("/a/B.MP3"), "audio")
        self.assertEqual(model.classify("/a/b.mkv"), "video")
        self.assertEqual(model.classify("/a/b.m3u8"), "playlist")
        self.assertEqual(model.classify("https://example.org/live/stream.webm?token=1"), "video")
        self.assertIsNone(model.classify("/a/b.txt"))
        self.assertEqual(Track("/music/01 Intro.flac").display_title, "01 Intro")
        self.assertEqual(Track("https://radio.example.org/live/jazz.mp3").display_title, "jazz.mp3")
        self.assertEqual(Track("https://radio.example.org/").display_title, "radio.example.org")
        self.assertEqual(Track("https://radio.example.org/x").subtitle, "Network stream · radio.example.org")
        self.assertTrue(Track.from_location("/v/film.mp4").video)

    def test_format_time(self) -> None:
        self.assertEqual([model.format_time(ms) for ms in (0, 999, 61_000, 3_600_000, 3_725_000, -5)],
                         ["0:00", "0:00", "1:01", "1:00:00", "1:02:05", "0:00"])


class StoresTest(TmpCase):
    def test_user_dirs(self) -> None:
        home = Path(os.environ["HOME"])
        self.assertEqual(model.user_dir("MUSIC"), home / "Music")
        (home / ".config/user-dirs.dirs").write_text(
            '# comment\nXDG_MUSIC_DIR="$HOME/Musik"\nXDG_VIDEOS_DIR="/srv/films"\nXDG_PICTURES_DIR="relative"\n'
            'XDG_DESKTOP_DIR="$HOME"\n', "utf-8")
        self.assertEqual(model.user_dir("MUSIC"), home / "Musik")
        self.assertEqual(model.user_dir("VIDEOS"), Path("/srv/films"))
        self.assertEqual(model.user_dir("PICTURES"), home / "Pictures")

    def test_settings(self) -> None:
        path = self.tmp / "settings.json"
        settings = model.Settings.load(path)
        self.assertEqual(settings.music_folders, [str(Path(os.environ["HOME"]) / "Music")])
        settings.music_folders.append("/srv/music")
        settings.volume, settings.rate, settings.resume_videos, settings.audio_device = 0.4, 1.5, False, "hdmi"
        settings.save(path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        again = model.Settings.load(path)
        self.assertEqual((again.music_folders[-1], again.volume, again.rate, again.resume_videos, again.audio_device),
                         ("/srv/music", 0.4, 1.5, False, "hdmi"))
        path.write_text(json.dumps({"music_folders": ["relative", 5, "/ok", "/ok"], "video_folders": "x",
                                    "volume": 7, "rate": 3.3, "muted": "yes", "resume_videos": 1,
                                    "audio_device": 5}), "utf-8")
        hostile = model.Settings.load(path)
        self.assertEqual((hostile.music_folders, hostile.volume, hostile.rate, hostile.muted, hostile.resume_videos,
                          hostile.audio_device), (["/ok"], 1.0, 1.0, False, True, ""))
        path.write_text("not json", "utf-8")
        self.assertEqual(model.Settings.load(path).volume, 1.0)

    def test_recents(self) -> None:
        recents = model.Recents(self.tmp / "recent.json")
        for number in range(model.MAX_RECENTS + 5):
            recents.add(Track(f"/m/{number}.mp3", str(number)))
        recents.add(Track("/m/10.mp3", "again"))
        self.assertEqual(len(recents.items), model.MAX_RECENTS)
        self.assertEqual(recents.items[0].title, "again")
        self.assertEqual(sum(1 for item in recents.items if item.location == "/m/10.mp3"), 1)
        recents.save()
        again = model.Recents(self.tmp / "recent.json")
        self.assertEqual([t.location for t in again.items], [t.location for t in recents.items])
        again.remove("/m/10.mp3")
        self.assertNotIn("/m/10.mp3", [t.location for t in again.items])

    def test_resume_positions(self) -> None:
        resume = model.ResumePositions(self.tmp / "resume.json")
        resume.remember("/v/a.mp4", 60_000, 600_000)
        resume.remember("/v/start.mp4", 2_000, 600_000)
        resume.remember("/v/end.mp4", 595_000, 600_000)
        resume.remember("/v/unknown.mp4", 60_000, 0)
        self.assertEqual((resume.get("/v/a.mp4"), resume.get("/v/start.mp4"), resume.get("/v/end.mp4"),
                          resume.get("/v/unknown.mp4")), (60_000, 0, 0, 0))
        resume.remember("/v/a.mp4", 0, 0)
        self.assertEqual(resume.get("/v/a.mp4"), 0)
        for number in range(model.MAX_RESUME + 10):
            resume.remember(f"/v/{number}.mp4", 60_000, 600_000)
        self.assertEqual(len(resume.positions), model.MAX_RESUME)
        resume.save()
        (self.tmp / "resume2.json").write_text(json.dumps({"relative": 5, "/ok.mp4": 70_000, "/bad.mp4": "x",
                                                           "/neg.mp4": -1, "/bool.mp4": True}), "utf-8")
        self.assertEqual(model.ResumePositions(self.tmp / "resume2.json").positions, {"/ok.mp4": 70_000})

    def test_playlist_store(self) -> None:
        store = model.PlaylistStore(self.tmp / "playlists")
        self.assertEqual(store.names(), [])
        name = store.create("Road trip", [Track("/m/a.mp3", "A", "Artist", duration_ms=61_000),
                                          Track("https://example.org/s")])
        self.assertEqual(store.names(), ["Road trip"])
        loaded = store.load(name)
        self.assertEqual([(t.location, t.title, t.artist, t.duration_ms) for t in loaded],
                         [("/m/a.mp3", "A", "Artist", 61_000), ("https://example.org/s", "", "", 0)])
        with self.assertRaises(model.PlaylistNameError):
            store.create("road TRIP")
        self.assertEqual(store.unique_name("Road trip"), "Road trip 2")
        self.assertEqual(store.rename("Road trip", "Holiday"), "Holiday")
        self.assertEqual(store.names(), ["Holiday"])
        store.create("Other")
        with self.assertRaises(model.PlaylistNameError):
            store.rename("Other", "holiday")
        store.delete("Other")
        self.assertEqual(store.names(), ["Holiday"])
        for bad in ("", "   ", "a/b", "..", ".hidden", "x" * 101, "a\\b", "a\nb\0", "../../etc/passwd"):
            with self.assertRaises(model.PlaylistNameError, msg=repr(bad)):
                store.create(bad)
        self.assertEqual(sorted(p.name for p in (self.tmp / "playlists").iterdir()), ["Holiday.m3u8"])

    def test_atomic_write_leaves_no_temporary_files(self) -> None:
        target = self.tmp / "deep/dir/file.json"
        model.write_json(target, {"a": 1})
        model.write_json(target, {"a": 2})
        self.assertEqual(json.loads(target.read_text()), {"a": 2})
        self.assertEqual([p.name for p in target.parent.iterdir()], ["file.json"])
        self.assertIsNone(model.read_json(self.tmp / "missing.json"))


class OpeningTest(TmpCase):
    def test_to_location(self) -> None:
        self.assertEqual(opening.to_location("song.mp3", "/music"), "/music/song.mp3")
        self.assertEqual(opening.to_location("/a/../b.mp3"), "/b.mp3")
        self.assertEqual(opening.to_location("file:///a/b%20c.mp3"), "/a/b c.mp3")
        self.assertEqual(opening.to_location("https://example.org/a.mp3"), "https://example.org/a.mp3")
        for bad in ("", "ftp://example.org/a.mp3", "file://host/a.mp3", "http://", "a\0b", "x" * 9000, None, 5):
            self.assertIsNone(opening.to_location(bad), repr(bad))

    def test_expand_files_folders_and_playlists(self) -> None:
        music = self.tmp / "music"
        (music / "Album/CD 2").mkdir(parents=True)
        (music / ".hidden").mkdir()
        for name in ("Album/2 b.mp3", "Album/10 c.mp3", "Album/1 a.mp3", "Album/CD 2/z.flac", "Album/cover.jpg",
                     ".hidden/secret.mp3", "notes.txt"):
            (music / name).write_bytes(b"")
        os.symlink(music, music / "Album/loop")
        (music / "list.m3u8").write_text("Album/1 a.mp3\nmissing.mp3\nhttps://example.org/radio\nnotes.txt\n", "utf-8")
        opened = opening.expand([str(music)])
        self.assertEqual([Path(t.location).name for t in opened.tracks], ["1 a.mp3", "2 b.mp3", "10 c.mp3", "z.flac"])
        opened = opening.expand([str(music / "list.m3u8"), "https://example.org/x.mp4", str(music / "nope.mp3"),
                                 "relative.mp3", str(music / "notes.txt")])
        self.assertEqual([t.location for t in opened.tracks],
                         [str(music / "Album/1 a.mp3"), "https://example.org/radio", "https://example.org/x.mp4",
                          str(music / "notes.txt")])
        self.assertEqual(opened.skipped, [str(music / "nope.mp3"), "relative.mp3"])
        self.assertTrue(opened.tracks[2].video)

    def test_expand_is_capped(self) -> None:
        folder = self.tmp / "many"
        folder.mkdir()
        for number in range(40):
            (folder / f"{number}.mp3").write_bytes(b"")
        original = opening.MAX_OPEN
        opening.MAX_OPEN = 25
        try:
            opened = opening.expand([str(folder), str(folder)])
        finally:
            opening.MAX_OPEN = original
        self.assertEqual((len(opened.tracks), opened.truncated), (25, True))


if __name__ == "__main__":
    unittest.main()
