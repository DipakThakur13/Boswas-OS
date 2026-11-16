"""PlayerController on the fake player: the queue, skipping what cannot be played, what is remembered."""

from __future__ import annotations

import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boswas_fluent import testing  # noqa: E402

import fakes  # noqa: E402

from boswas_apps.media_player import model  # noqa: E402
from boswas_apps.media_player.controller import PlayerController  # noqa: E402
from boswas_apps.media_player.model import Track  # noqa: E402


class ControllerCase(testing.FluentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.player = fakes.FakePlayer()
        self.settings = model.Settings(["/music"], ["/videos"])
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.controller = self.make()
        self.messages: list[tuple[str, str]] = []
        self.controller.message.connect(lambda severity, text: self.messages.append((severity, text)))

    def make(self) -> PlayerController:
        return PlayerController(self.player, self.settings, self.state, self.tmp / "settings.json",
                                rng=random.Random(4))

    def tearDown(self) -> None:
        self.controller.shutdown()
        super().tearDown()

    def tracks(self, count: int, extension: str = ".mp3") -> list[Track]:
        return [Track(f"/music/{n}{extension}", f"Track {n}", "Artist") for n in range(count)]

    def settle(self) -> None:
        testing.process(20)


class TransportTest(ControllerCase):
    def test_play_tracks_and_end_of_queue(self) -> None:
        changes = []
        self.controller.track_changed.connect(lambda track: changes.append(track.title if track else None))
        self.controller.play_tracks(self.tracks(2))
        self.settle()
        self.assertEqual((self.controller.state(), self.controller.current().title), ("playing", "Track 0"))
        self.assertEqual(self.controller.duration(), 180_000)
        self.player.finish()
        self.settle()
        self.assertEqual((self.controller.current().title, self.controller.state()), ("Track 1", "playing"))
        self.player.finish()
        self.settle()
        self.assertEqual((self.controller.state(), self.controller.current().title), ("stopped", "Track 1"))
        self.assertEqual(changes, ["Track 0", "Track 1"])
        self.controller.play()
        self.assertEqual(self.controller.state(), "playing")

    def test_previous_restarts_or_goes_back(self) -> None:
        self.controller.play_tracks(self.tracks(3), start=1)
        self.settle()
        self.player.advance(10_000)
        seeks = []
        self.controller.seeked.connect(seeks.append)
        self.controller.previous()
        self.assertEqual((self.controller.current().title, self.player.position(), seeks), ("Track 1", 0, [0]))
        self.controller.previous()
        self.settle()
        self.assertEqual(self.controller.current().title, "Track 0")
        self.controller.previous()                         # nothing before the first: restart it
        self.assertEqual(self.controller.current().title, "Track 0")

    def test_seek_is_clamped(self) -> None:
        self.controller.seek(5000)                         # nothing loaded: nothing happens
        self.controller.play_tracks(self.tracks(1))
        self.settle()
        self.controller.seek(10**9)
        self.assertEqual(self.player.position(), 180_000 - 250)
        self.controller.seek_relative(-10**9)
        self.assertEqual(self.player.position(), 0)

    def test_repeat_one_and_all(self) -> None:
        self.controller.play_tracks(self.tracks(2))
        self.settle()
        self.controller.set_repeat("one")
        self.player.advance(180_000)
        self.settle()
        self.assertEqual((self.controller.current().title, self.controller.state(), self.player.position()),
                         ("Track 0", "playing", 0))
        self.controller.set_repeat("all")
        self.controller.next()
        self.settle()
        self.player.finish()
        self.settle()
        self.assertEqual((self.controller.current().title, self.controller.state()), ("Track 0", "playing"))

    def test_options_reach_the_player_and_the_settings(self) -> None:
        self.controller.set_volume(0.3)
        self.controller.change_volume(0.05)
        self.assertEqual((self.player.volume, self.settings.volume), (0.35, 0.35))
        self.controller.change_volume(5)
        self.assertEqual(self.player.volume, 1.0)
        self.controller.toggle_mute()
        self.assertTrue(self.player.muted)
        self.controller.set_volume(0.5)                    # turning the volume up ends mute
        self.assertFalse(self.player.muted)
        self.controller.set_rate(9)
        self.assertEqual((self.player.rate, self.settings.rate), (2.0, 2.0))
        self.controller.set_rate(0.01)
        self.assertEqual(self.player.rate, 0.25)
        self.controller.set_audio_device("headset")
        self.assertEqual((self.player.device, self.settings.audio_device), ("headset", "headset"))


class FailureTest(ControllerCase):
    def test_a_broken_file_is_skipped_with_a_message(self) -> None:
        tracks = self.tracks(3)
        self.player.broken.add(tracks[1].location)
        self.controller.play_tracks(tracks)
        self.settle()
        self.player.finish()
        self.settle()
        self.assertEqual(self.controller.current().title, "Track 2")
        self.assertEqual(self.controller.state(), "playing")
        self.assertEqual(len(self.messages), 1)
        severity, text = self.messages[0]
        self.assertEqual(severity, "warning")
        self.assertIn("Track 1", text)
        self.assertIn("Skipped", text)

    def test_nothing_playable_stops_instead_of_looping(self) -> None:
        tracks = self.tracks(4)
        self.player.broken.update(track.location for track in tracks)
        self.controller.set_repeat("all")
        self.controller.play_tracks(tracks)
        testing.process(100)
        self.assertEqual(self.controller.state(), "stopped")
        self.assertEqual(len(self.player.sources), 4)       # each one tried once
        self.assertEqual([severity for severity, _text in self.messages], ["warning"] * 3 + ["error"])

    def test_a_single_broken_file_and_a_stream_that_drops(self) -> None:
        self.controller.play_tracks([Track("/music/only.broken", "Only")])
        self.settle()
        self.assertEqual(self.messages[-1][0], "error")
        self.assertIn("“Only”", self.messages[-1][1])
        self.controller.play_tracks([Track("https://radio.example.org/live")])
        self.settle()
        self.player.fail("The network stream could not be reached.")
        self.settle()
        self.assertIn("could not be reached", self.messages[-1][1])
        self.assertEqual(self.controller.state(), "stopped")

    def test_choosing_another_track_cancels_the_skip(self) -> None:
        tracks = self.tracks(3)
        self.player.broken.add(tracks[0].location)
        self.controller.play_tracks(tracks)
        self.controller.play_index(2)                      # before the failure of track 0 is reported
        self.settle()
        self.assertEqual(self.controller.current().title, "Track 2")


class QueueEditingTest(ControllerCase):
    def test_enqueue_starts_when_idle_and_tells_when_not(self) -> None:
        self.controller.enqueue(self.tracks(1))
        self.settle()
        self.assertEqual((self.controller.state(), self.messages), ("playing", []))
        self.controller.enqueue(self.tracks(2))
        self.assertEqual(self.messages, [("success", "Added 2 items to the play queue.")])
        self.assertEqual((len(self.controller.queue), self.controller.current().title), (3, "Track 0"))

    def test_play_next_remove_and_clear(self) -> None:
        self.controller.play_tracks(self.tracks(3))
        self.settle()
        self.controller.play_next([Track("/music/next.mp3", "Next")])
        self.assertEqual([t.title for t in self.controller.queue.items], ["Track 0", "Next", "Track 1", "Track 2"])
        self.controller.remove(0)                          # the current one: the next takes over, still playing
        self.settle()
        self.assertEqual((self.controller.current().title, self.controller.state()), ("Next", "playing"))
        self.controller.pause()
        self.controller.remove(0)
        self.settle()
        self.assertEqual((self.controller.current().title, self.controller.state()), ("Track 1", "stopped"))
        self.controller.move(0, 1)
        self.assertEqual(self.controller.queue.index, 1)
        cleared = []
        self.controller.track_changed.connect(cleared.append)
        self.controller.clear()
        self.assertEqual((cleared, self.player.source, len(self.controller.queue)), ([None], "", 0))

    def test_the_caller_keeps_its_own_tracks(self) -> None:
        mine = self.tracks(1)
        self.controller.play_tracks(mine)
        self.settle()
        self.assertIsNot(self.controller.current(), mine[0])
        self.assertEqual(mine[0].duration_ms, 0)            # the queue's copy got the length, not the library's


class MetadataTest(ControllerCase):
    def test_backend_fills_in_what_the_track_lacks(self) -> None:
        url = "https://radio.example.org/live"
        self.player.metadata[url] = {"title": "Evening\nJazz ", "artist": "Radio One", "album": "", "cover": None}
        self.player.durations[url] = 0
        updates = []
        self.controller.track_updated.connect(lambda track: updates.append(track.title))
        self.controller.play_tracks([Track(url)])
        self.settle()
        track = self.controller.current()
        self.assertEqual((track.title, track.artist, track.duration_ms), ("Evening Jazz", "Radio One", 0))
        self.assertEqual(updates, ["Evening Jazz"])
        tagged = Track("/music/tagged.mp3", "My title", "My artist")
        self.player.metadata[tagged.location] = {"title": "Other", "artist": "Other", "album": "Album", "cover": None}
        self.controller.play_tracks([tagged])
        self.settle()
        current = self.controller.current()
        self.assertEqual((current.title, current.artist, current.album), ("My title", "My artist", "Album"))

    def test_video_detection_and_recents(self) -> None:
        self.player.videos.add("/music/clip.bin")
        flags = []
        self.controller.video_changed.connect(flags.append)
        self.controller.play_tracks([Track("/music/clip.bin")])
        self.settle()
        self.assertTrue(self.controller.has_video())
        self.assertTrue(self.controller.current().video)
        self.assertEqual(flags, [True])
        self.assertEqual([t.location for t in self.controller.recents.items], ["/music/clip.bin"])
        self.controller.play_tracks(self.tracks(1))
        self.settle()
        self.assertEqual(flags, [True, False])
        self.assertEqual(len(self.controller.recents.items), 2)


class PersistenceTest(ControllerCase):
    def test_queue_and_position_come_back_paused(self) -> None:
        self.controller.play_tracks(self.tracks(3), start=1)
        self.settle()
        self.player.advance(42_000)
        self.controller.set_repeat("all")
        self.controller.set_volume(0.6)
        self.assertEqual(self.controller.save_state(), [])
        saved = json.loads((self.state / "queue.json").read_text())
        self.assertEqual((saved["index"], saved["position"], saved["repeat"]), (1, 42_000, "all"))
        self.assertEqual(json.loads((self.tmp / "settings.json").read_text())["volume"], 0.6)

        self.controller.shutdown()
        self.player = fakes.FakePlayer()
        self.settings = model.Settings.load(self.tmp / "settings.json")
        self.controller = self.make()
        self.controller.restore_state()
        self.settle()
        self.assertEqual((self.controller.current().title, self.controller.state(), self.player.position()),
                         ("Track 1", "stopped", 42_000))
        self.assertEqual((self.controller.queue.repeat, self.player.volume), ("all", 0.6))
        self.assertEqual(len(self.controller.recents.items), 1)

    def test_videos_resume_where_they_were_stopped(self) -> None:
        film = Track("/videos/film.mp4", "Film", video=True)
        self.player.durations[film.location] = 600_000
        self.controller.play_tracks([film])
        self.settle()
        self.player.advance(125_000)
        self.controller.play_tracks(self.tracks(1))        # leaving the film remembers where it was
        self.settle()
        self.assertEqual(self.controller.resume.get(film.location), 125_000)
        self.controller.play_tracks([film])
        self.settle()
        self.assertEqual(self.player.position(), 125_000)
        self.player.finish()                               # watched to the end: forgotten
        self.settle()
        self.assertEqual(self.controller.resume.get(film.location), 0)
        self.settings.resume_videos = False
        self.controller.resume.remember(film.location, 300_000, 600_000)
        self.controller.play_tracks([film])
        self.settle()
        self.assertEqual(self.player.position(), 0)

    def test_restore_ignores_foreign_state(self) -> None:
        (self.state / "queue.json").write_text('{"items": [{"location": "../x"}], "index": 0, "position": "far"}')
        self.controller.restore_state()
        self.assertIsNone(self.controller.current())
        (self.state / "queue.json").write_text("[1, 2, 3]")
        self.controller.restore_state()
        self.assertEqual(self.player.sources, [])

    def test_save_reports_what_failed(self) -> None:
        self.controller.state_dir = self.tmp / "state" / "queue.json" / "impossible"
        (self.tmp / "state" / "queue.json").write_text("{}")
        failed = self.controller.save_state()
        self.assertIn("the play queue", failed)


class OpenTest(ControllerCase):
    def test_open_locations_reads_tags_off_the_gui_thread(self) -> None:
        music = self.tmp / "music"
        music.mkdir()
        fakes.make_wav(music / "2.wav", 0.5, info={"INAM": "Second", "IART": "B"})
        fakes.make_wav(music / "1.wav", 0.5, info={"INAM": "First", "IART": "A"})
        self.controller.open_locations([str(music)])
        self.assertIsNone(self.controller.current())       # nothing yet: the folder is read on the worker
        self.assertTrue(self.controller.worker.wait(5))
        self.settle()
        self.assertEqual([t.title for t in self.controller.queue.items], ["First", "Second"])
        self.assertEqual(self.controller.state(), "playing")
        self.controller.open_locations([str(music / "2.wav")], "enqueue")
        self.controller.open_locations([str(music / "1.wav")], "next")
        self.assertTrue(self.controller.worker.wait(5))
        self.assertEqual([t.title for t in self.controller.queue.items], ["First", "First", "Second", "Second"])
        self.controller.open_locations([str(self.tmp / "nothing-here")])
        self.assertTrue(self.controller.worker.wait(5))
        self.assertEqual(self.messages[-1], ("warning", "Nothing to play was found there."))
        self.controller.open_locations([])
        self.controller.open_locations([None, ""])          # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()
