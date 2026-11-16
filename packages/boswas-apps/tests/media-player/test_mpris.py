"""MPRIS against a private ``dbus-daemon --session`` started by the test (never the user's session bus).

The service under test is exported with QtDBus; the tests talk to it with
``dbusclient``, which reads the wire format itself, so every assertion
about a type is about what another program would really receive.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boswas_fluent import testing  # noqa: E402

import dbusclient  # noqa: E402
import fakes  # noqa: E402
from dbusclient import Variant, plain  # noqa: E402

from PySide6.QtDBus import QDBusConnection  # noqa: E402

from boswas_apps.media_player import model, mpris  # noqa: E402
from boswas_apps.media_player.controller import PlayerController  # noqa: E402
from boswas_apps.media_player.model import Track  # noqa: E402

DAEMON = shutil.which("dbus-daemon", path="/usr/bin:/bin")
PROPERTIES = mpris.PROPERTIES_INTERFACE
PLAYER = mpris.PLAYER_INTERFACE
ROOT = mpris.ROOT_INTERFACE


@unittest.skipUnless(DAEMON, "dbus-daemon is not installed")
class MprisTest(testing.FluentTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A socket path must be short: under /tmp, not under the test's own (long) temporary directory.
        self.bus_dir = tempfile.mkdtemp(prefix="mp-bus-", dir="/tmp")
        self.daemon = subprocess.Popen(
            [DAEMON, "--session", "--nofork", "--print-address=1", f"--address=unix:path={self.bus_dir}/bus"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.address = self.daemon.stdout.readline().decode().strip()
        self.assertTrue(self.address.startswith("unix:"))
        self.server = QDBusConnection.connectToBus(self.address, f"mp-server-{id(self)}")
        self.assertTrue(self.server.isConnected())

        self.player = fakes.FakePlayer()
        self.settings = model.Settings(["/music"], ["/videos"])
        (self.tmp / "state").mkdir()
        self.controller = PlayerController(self.player, self.settings, self.tmp / "state", self.tmp / "settings.json",
                                           rng=random.Random(2))
        self.raised: list[str] = []
        self.service = mpris.MprisService(self.controller, connection=self.server,
                                          raise_window=lambda: self.raised.append("raise"),
                                          quit_application=lambda: self.raised.append("quit"))
        self.assertTrue(self.service.start())
        self.client = dbusclient.Client(self.address, testing.process)
        self.client.match_signals(mpris.SERVICE, mpris.PATH)

    def tearDown(self) -> None:
        self.client.close()
        self.service.stop()
        self.controller.shutdown()
        QDBusConnection.disconnectFromBus(self.server.name())
        self.daemon.terminate()
        self.daemon.wait(5)
        self.daemon.stdout.close()
        shutil.rmtree(self.bus_dir, ignore_errors=True)
        super().tearDown()

    # --- a client ---
    def call(self, interface: str, method: str, signature: str = "", *arguments) -> dbusclient.Message:
        message = self.client.call(mpris.SERVICE, mpris.PATH, interface, method, signature, *arguments)
        testing.process(20)
        return message

    def get(self, name: str, interface: str = PLAYER) -> Variant:
        reply = self.call(PROPERTIES, "Get", "ss", interface, name)
        self.assertFalse(reply.is_error, reply.body)
        self.assertEqual(reply.signature, "v")
        return reply.body[0]

    def value(self, name: str, interface: str = PLAYER):
        return plain(self.get(name, interface))

    def set(self, name: str, signature: str, value) -> dbusclient.Message:
        return self.call(PROPERTIES, "Set", "ssv", PLAYER, name, Variant(signature, value))

    def changes(self) -> dict:
        """Every property change announced since the last call, merged."""
        merged: dict = {}
        for message in self.client.take_signals("PropertiesChanged"):
            self.assertEqual(message.signature, "sa{sv}as")
            interface, changed, invalidated = message.body
            self.assertEqual((interface, invalidated), (PLAYER, []))
            merged.update(changed)
        return merged

    def play(self, count: int = 3) -> None:
        self.controller.play_tracks([Track(f"/music/{n}.mp3", f"Track {n}", "Artist", "Album") for n in range(count)])
        testing.process(30)

    # --- tests ---
    def test_root_interface(self) -> None:
        self.assertEqual(self.get("Identity", ROOT), Variant("s", "Media Player"))
        self.assertEqual(self.get("DesktopEntry", ROOT), Variant("s", "com.boswas.MediaPlayer"))
        self.assertEqual(self.get("SupportedUriSchemes", ROOT), Variant("as", ["file", "http", "https"]))
        mime = self.get("SupportedMimeTypes", ROOT)
        self.assertEqual(mime.signature, "as")
        for kind in ("audio/mpeg", "audio/flac", "audio/ogg", "audio/x-wav", "audio/mp4", "video/mp4",
                     "video/x-matroska", "video/webm", "video/quicktime", "video/x-msvideo"):
            self.assertIn(kind, mime.value)
        self.assertEqual(self.get("CanRaise", ROOT), Variant("b", True))
        self.assertEqual(self.get("CanQuit", ROOT), Variant("b", True))
        self.assertEqual(self.get("HasTrackList", ROOT), Variant("b", False))
        self.call(ROOT, "Raise")
        self.call(ROOT, "Quit")
        self.assertEqual(self.raised, ["raise", "quit"])

    def test_introspection_declares_the_specified_types(self) -> None:
        xml = self.call("org.freedesktop.DBus.Introspectable", "Introspect").body[0]
        for fragment in ('<property name="Position" type="x" access="read"/>',
                         '<property name="Metadata" type="a{sv}" access="read"',
                         '<property name="Volume" type="d" access="readwrite"/>',
                         '<property name="Rate" type="d" access="readwrite"/>',
                         '<property name="LoopStatus" type="s" access="readwrite"/>',
                         '<property name="Shuffle" type="b" access="readwrite"/>',
                         '<property name="PlaybackStatus" type="s" access="read"/>',
                         '<property name="SupportedMimeTypes" type="as" access="read"/>',
                         '<signal name="Seeked">', '<method name="SetPosition">', '<method name="OpenUri">'):
            self.assertIn(fragment, xml)
        for method in ("Next", "Previous", "Pause", "PlayPause", "Stop", "Play", "Seek", "Raise", "Quit"):
            self.assertIn(f'<method name="{method}"', xml)
        for name in ("CanGoNext", "CanGoPrevious", "CanPlay", "CanPause", "CanSeek", "CanControl", "MinimumRate",
                     "MaximumRate"):
            self.assertIn(f'<property name="{name}"', xml)

    def test_idle_player(self) -> None:
        reply = self.call(PROPERTIES, "GetAll", "s", PLAYER)
        self.assertEqual(reply.signature, "a{sv}")
        everything = reply.body[0]
        self.assertEqual(everything["PlaybackStatus"], Variant("s", "Stopped"))
        self.assertEqual(everything["Metadata"], Variant("a{sv}", {"mpris:trackid": Variant("o", mpris.NO_TRACK)}))
        self.assertEqual(everything["Position"], Variant("x", 0))
        for name, expected in (("Rate", 1.0), ("MinimumRate", 0.25), ("MaximumRate", 2.0), ("Volume", 1.0)):
            self.assertEqual(everything[name], Variant("d", expected), name)
        for name in ("CanGoNext", "CanGoPrevious", "CanPlay", "CanPause", "CanSeek", "Shuffle"):
            self.assertEqual(everything[name], Variant("b", False), name)
        self.assertEqual(everything["CanControl"], Variant("b", True))
        self.assertEqual(everything["LoopStatus"], Variant("s", "None"))
        for method in ("Play", "Pause", "PlayPause", "Next", "Previous", "Stop"):
            self.assertFalse(self.call(PLAYER, method).is_error)
        self.assertEqual(self.controller.state(), "stopped")

    def test_metadata_and_changes_are_announced(self) -> None:
        self.play()
        metadata = self.get("Metadata")
        self.assertEqual(metadata.signature, "a{sv}")
        self.assertEqual(metadata.value["xesam:title"], Variant("s", "Track 0"))
        self.assertEqual(metadata.value["xesam:artist"], Variant("as", ["Artist"]))
        self.assertEqual(metadata.value["xesam:album"], Variant("s", "Album"))
        self.assertEqual(metadata.value["xesam:url"], Variant("s", "file:///music/0.mp3"))
        self.assertEqual(metadata.value["mpris:trackid"].signature, "o")
        self.assertTrue(metadata.value["mpris:trackid"].value.startswith(mpris.TRACK_PREFIX))
        self.assertEqual(metadata.value["mpris:length"].value, 180_000_000)
        self.assertNotIn("mpris:artUrl", metadata.value)
        self.assertEqual(self.value("PlaybackStatus"), "Playing")
        self.assertTrue(self.value("CanGoNext"))
        changed = self.changes()
        self.assertEqual(changed["PlaybackStatus"], Variant("s", "Playing"))
        self.assertEqual(changed["Metadata"].value["xesam:title"], Variant("s", "Track 0"))
        for name in ("CanPlay", "CanPause", "CanGoNext", "CanGoPrevious", "CanSeek"):
            self.assertEqual(changed[name], Variant("b", True), name)

        cover = self.tmp / "cover art.png"
        cover.write_bytes(fakes.png_bytes())
        self.controller.set_art_path(str(cover))
        testing.process(30)
        self.assertEqual(plain(self.changes()["Metadata"])["mpris:artUrl"], "file://" + str(cover).replace(" ", "%20"))

        self.controller.pause()
        self.controller.pause()
        testing.process(30)
        signals = self.client.take_signals("PropertiesChanged")
        self.assertEqual([message.body[1] for message in signals], [{"PlaybackStatus": Variant("s", "Paused")}])

    def test_length_type_follows_the_documented_limit(self) -> None:
        # PySide cannot choose the type of a value inside a dictionary: 32 bits when it fits, else the 64 of the
        # specification. This test pins that behaviour down so a change in PySide is noticed.
        self.player.durations["/music/long.mp3"] = 3 * 3600 * 1000
        self.play(1)
        self.assertEqual(self.get("Metadata").value["mpris:length"], Variant("i", 180_000_000))
        self.controller.play_tracks([Track("/music/long.mp3", "Long")])
        testing.process(30)
        self.assertEqual(self.get("Metadata").value["mpris:length"], Variant("x", 10_800_000_000))

    def test_transport_methods(self) -> None:
        self.play()
        first = self.value("Metadata")["mpris:trackid"]
        self.call(PLAYER, "Pause")
        self.assertEqual(self.controller.state(), "paused")
        self.call(PLAYER, "PlayPause")
        self.assertEqual(self.controller.state(), "playing")
        self.call(PLAYER, "Next")
        self.assertEqual(self.controller.current().title, "Track 1")
        self.assertNotEqual(self.value("Metadata")["mpris:trackid"], first)
        self.call(PLAYER, "Previous")
        self.assertEqual(self.controller.current().title, "Track 0")
        self.call(PLAYER, "Stop")
        self.assertEqual(self.value("PlaybackStatus"), "Stopped")
        self.call(PLAYER, "Play")
        self.assertEqual(self.value("PlaybackStatus"), "Playing")

    def test_seek_set_position_and_seeked(self) -> None:
        self.play()
        track = self.value("Metadata")["mpris:trackid"]
        self.call(PLAYER, "SetPosition", "ox", track, 60_000_000)
        self.assertEqual(self.player.position(), 60_000)
        self.assertEqual(self.get("Position"), Variant("x", 60_000_000))
        self.call(PLAYER, "Seek", "x", 5_000_000)
        self.call(PLAYER, "Seek", "x", -20_000_000)
        self.assertEqual(self.player.position(), 45_000)
        seeked = self.client.take_signals("Seeked")
        self.assertEqual([(message.signature, message.body) for message in seeked],
                         [("x", [60_000_000]), ("x", [65_000_000]), ("x", [45_000_000])])
        # Refused: another track's identifier, a negative position, a position beyond the end.
        self.call(PLAYER, "SetPosition", "ox", "/com/boswas/MediaPlayer/track/999", 1_000_000)
        self.call(PLAYER, "SetPosition", "ox", track, -5)
        self.call(PLAYER, "SetPosition", "ox", track, 10**15)
        self.assertEqual(self.player.position(), 45_000)
        self.call(PLAYER, "Seek", "x", -(10**17))
        self.assertEqual(self.player.position(), 45_000)
        self.call(PLAYER, "Seek", "x", -(10**9))
        self.assertEqual(self.player.position(), 0)
        self.call(PLAYER, "Seek", "x", 10**12)             # beyond the end: the next track
        self.assertEqual(self.controller.current().title, "Track 1")

    def test_writable_properties_are_validated(self) -> None:
        self.play()
        self.changes()
        self.set("Volume", "d", 0.25)
        self.assertEqual(self.player.volume, 0.25)
        self.set("Volume", "d", 7.5)
        self.assertEqual(self.player.volume, 1.0)
        self.set("Volume", "d", -3.0)
        self.assertEqual(self.player.volume, 0.0)
        self.set("Volume", "d", float("nan"))
        self.assertEqual(self.player.volume, 0.0)
        self.set("Rate", "d", 1.5)
        self.assertEqual(self.player.rate, 1.5)
        for refused in (0.0, 16.0, -1.0, float("inf")):
            self.set("Rate", "d", refused)
            self.assertEqual(self.player.rate, 1.5)
        self.set("LoopStatus", "s", "Playlist")
        self.assertEqual(self.controller.queue.repeat, "all")
        self.set("LoopStatus", "s", "Track")
        self.assertEqual(self.value("LoopStatus"), "Track")
        self.set("LoopStatus", "s", "Sometimes")
        self.assertEqual(self.controller.queue.repeat, "one")
        self.set("Shuffle", "b", True)
        self.assertTrue(self.controller.queue.shuffle)
        self.assertEqual(self.get("Shuffle"), Variant("b", True))
        announced = self.changes()
        self.assertEqual((announced["Volume"], announced["Rate"], announced["LoopStatus"], announced["Shuffle"]),
                         (Variant("d", 0.0), Variant("d", 1.5), Variant("s", "Track"), Variant("b", True)))
        self.assertTrue(self.set("PlaybackStatus", "s", "Playing").is_error)        # read-only
        self.assertTrue(self.set("Volume", "s", "loud").is_error)
        self.assertEqual(self.player.volume, 0.0)

    def test_open_uri_takes_files_and_web_addresses_only(self) -> None:
        song = fakes.make_wav(self.tmp / "a song.wav", 0.2, info={"INAM": "Opened"})
        self.call(PLAYER, "OpenUri", "s", "file://" + str(song).replace(" ", "%20"))
        self.assertTrue(self.controller.worker.wait(5))
        testing.process(30)
        self.assertEqual(self.controller.current().title, "Opened")
        self.call(PLAYER, "OpenUri", "s", "https://radio.example.org/live.mp3")
        self.assertTrue(self.controller.worker.wait(5))
        testing.process(30)
        self.assertEqual(self.controller.current().location, "https://radio.example.org/live.mp3")
        self.assertEqual(self.value("Metadata")["xesam:url"], "https://radio.example.org/live.mp3")
        for refused in ("ftp://example.org/a.mp3", "smb://nas/a.mp3", "javascript:alert(1)", "file://host/etc/passwd",
                        "/etc/passwd", "relative.mp3", "", "http://", "x" * 20000):
            self.call(PLAYER, "OpenUri", "s", refused)
        self.assertTrue(self.controller.worker.wait(5))
        testing.process(30)
        self.assertEqual(self.controller.current().location, "https://radio.example.org/live.mp3")

    def test_unknown_methods_and_wrong_arguments_get_an_error(self) -> None:
        self.assertTrue(self.call(PLAYER, "Explode").is_error)
        self.assertTrue(self.call(PLAYER, "Seek", "s", "far").is_error)
        self.assertTrue(self.call(PLAYER, "SetPosition", "i", 5).is_error)
        self.assertTrue(self.call(PROPERTIES, "Get", "ss", PLAYER, "Nope").is_error)
        self.assertTrue(self.call("org.example.Other", "Play").is_error)
        self.assertEqual(self.controller.state(), "stopped")

    def test_name_is_released_and_cannot_be_taken_twice(self) -> None:
        bus = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
        self.assertIn(mpris.SERVICE, self.client.call(*bus, "ListNames").body[0])
        second = QDBusConnection.connectToBus(self.address, f"mp-second-{id(self)}")
        other = mpris.MprisService(self.controller, connection=second)
        self.assertFalse(other.start())
        self.service.stop()
        testing.process(20)
        self.assertNotIn(mpris.SERVICE, self.client.call(*bus, "ListNames").body[0])
        self.controller.pause()                             # no name, no signal, no error
        self.assertTrue(self.service.start())
        QDBusConnection.disconnectFromBus(second.name())


class NoBusTest(testing.FluentTestCase):
    def test_without_a_session_bus_the_service_stays_off(self) -> None:
        os.environ.pop("XDG_RUNTIME_DIR", None)
        player = fakes.FakePlayer()
        (self.tmp / "state").mkdir()
        controller = PlayerController(player, model.Settings(), self.tmp / "state", self.tmp / "settings.json")
        service = mpris.MprisService(controller)
        self.assertFalse(service.start())
        self.assertFalse(service.is_registered())
        controller.play_tracks([Track("/music/a.mp3")])
        testing.process(20)
        service.stop()
        controller.shutdown()


if __name__ == "__main__":
    unittest.main()
