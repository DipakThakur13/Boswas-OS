"""The Media Player window, driven through its widgets on the fake player (offscreen, private directories)."""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boswas_fluent import testing  # noqa: E402

import boswas_fluent as fluent  # noqa: E402
import fakes  # noqa: E402

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QApplication, QToolButton  # noqa: E402

from boswas_apps.media_player import window as window_module  # noqa: E402
from boswas_apps.media_player.window import MainWindow  # noqa: E402

CTRL, ALT, SHIFT = (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.AltModifier,
                    Qt.KeyboardModifier.ShiftModifier)
PACKAGE = Path(window_module.__file__).resolve().parent


def shortcut(window, sequence: str) -> None:
    """Activate the window shortcut ``sequence`` (the offscreen platform has no key map to send it through)."""
    from PySide6.QtGui import QKeySequence, QShortcut
    wanted = QKeySequence(sequence)
    for candidate in window.findChildren(QShortcut):
        if candidate.key() == wanted:
            candidate.activated.emit()
            testing.process()
            return
    from PySide6.QtGui import QAction
    for action in window.findChildren(QAction):
        if action.shortcut() == wanted and action.isEnabled():
            action.trigger()
            testing.process()
            return
    raise AssertionError(f"no shortcut {sequence}")


class WindowCase(testing.FluentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.tmp / "home")
        self.music = self.tmp / "home/Music"
        self.videos = self.tmp / "home/Videos"
        self.music.mkdir(parents=True)
        self.videos.mkdir()
        self.player = fakes.FakePlayer()
        self.grabber = fakes.FakeGrabber()
        self.window: MainWindow | None = None

    def tearDown(self) -> None:
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        super().tearDown()

    def song(self, relative: str, title: str, artist: str = "Artist", album: str = "Album", track: int = 1,
             cover: bool = False) -> Path:
        path = self.music / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return fakes.make_flac(path, [f"TITLE={title}", f"ARTIST={artist}", f"ALBUM={album}", f"TRACKNUMBER={track}",
                                      "DATE=1999", "GENRE=Jazz"], cover=fakes.jpeg_bytes(32, 32) if cover else None)

    def library(self) -> None:
        self.song("Miles/Kind of Blue/1.flac", "So What", "Miles Davis", "Kind of Blue", 1, cover=True)
        self.song("Miles/Kind of Blue/2.flac", "Blue in Green", "Miles Davis", "Kind of Blue", 2)
        self.song("Mac/Rumours/1.flac", "Dreams", "Fleetwood Mac", "Rumours", 1)
        self.song("Mac/Rumours/2.flac", "The Chain", "Fleetwood Mac", "Rumours", 2)
        for name in ("Holiday", "Birthday"):
            fakes.make_mp4(self.videos / f"{name}.mp4", {}, duration=90_000, video=True)

    def open(self, arguments: list[str] | None = None, **options) -> MainWindow:
        window = MainWindow(arguments or [], backend=self.player, grabber=self.grabber, use_mpris=False,
                            backend_info=lambda: {"containers": ["Ogg"], "audio": ["FLAC"], "video": ["VP9"]},
                            **options)
        self.window = self.show(window, 1180, 760)
        self.settle()
        return window

    def settle(self) -> None:
        window = self.window
        for _round in range(4):
            window.worker.wait(5)
            window.controller.worker.wait(5)
            testing.wait_until(lambda: not window.library.is_scanning() and not window.artwork.is_pending(), 5)
            testing.process(15)

    def titles(self) -> list[str]:
        return [track.display_title for track in self.window.controller.queue.items]

    def wav(self, name: str, title: str = "") -> str:
        return str(fakes.make_wav(self.tmp / name, 0.2, info={"INAM": title} if title else None))


class PlaybackTest(WindowCase):
    def test_empty_start_and_open_files(self) -> None:
        window = self.open()
        self.assertEqual(window.navigation.current(), "home")
        self.assertIs(window.home_page.stack.currentWidget(), window.home_page.empty)
        self.assertEqual(window.bar.title.text(), "Nothing is playing")
        self.assertFalse(window.bar.play.isEnabled())
        self.assertFalse(window.bar.seek.isEnabled())
        chosen = [self.wav("one.wav", "First song"), self.wav("two.wav", "Second song")]
        window.choose_files = lambda title, file_filter: chosen
        testing.click(window.home_page.open_files)
        self.settle()
        self.assertEqual(self.titles(), ["First song", "Second song"])
        self.assertEqual((window.bar.title.text(), self.player.state()), ("First song", "playing"))
        self.assertTrue(window.bar.play.is_playing())
        self.assertEqual(window.windowTitle(), "First song – Media Player")
        self.assertIs(window.home_page.stack.currentWidget(), window.home_page.tiles)
        testing.click(window.bar.play)
        self.assertEqual(self.player.state(), "paused")
        self.assertEqual(window.bar.play.accessibleName(), "Play")
        testing.click(window.bar.next)
        self.settle()
        self.assertEqual(window.bar.title.text(), "Second song")
        self.assertFalse(window.bar.next.isEnabled())
        testing.click(window.bar.previous)
        self.settle()
        self.assertEqual(window.bar.title.text(), "First song")

    def test_keyboard(self) -> None:
        window = self.open([self.wav("a.wav", "A"), self.wav("b.wav", "B")])
        self.player.durations.update({track.location: 200_000 for track in window.controller.queue.items})
        window.controller.play_index(0)
        self.settle()
        window.setFocus()
        testing.key(window, Qt.Key.Key_Space)
        self.assertEqual(self.player.state(), "paused")
        shortcut(window, "Ctrl+P")
        self.assertEqual(self.player.state(), "playing")
        testing.key(window, Qt.Key.Key_Right)
        testing.key(window, Qt.Key.Key_Right)
        self.assertEqual(self.player.position(), 20_000)
        testing.key(window, Qt.Key.Key_Left)
        self.assertEqual(self.player.position(), 10_000)
        self.assertEqual(window.bar.position_label.text(), "0:10")
        shortcut(window, "Ctrl+Right")
        self.settle()
        self.assertEqual(window.controller.current().title, "B")
        shortcut(window, "Ctrl+Left")
        self.settle()
        self.assertEqual(window.controller.current().title, "A")
        window.controller.set_volume(0.5)
        shortcut(window, "Ctrl+Up")
        self.assertEqual(self.player.volume, 0.55)
        shortcut(window, "Ctrl+Down")
        shortcut(window, "Ctrl+Down")
        self.assertEqual((self.player.volume, window.bar.volume.value()), (0.45, 45))
        testing.key(window, Qt.Key.Key_M, text="m")
        self.assertTrue(self.player.muted)
        self.assertEqual(window.bar.mute.accessibleName(), "Unmute")
        testing.key(window, Qt.Key.Key_M, text="m")
        self.assertFalse(self.player.muted)
        # A list that has the focus hands these keys on; a text box keeps them.
        window.navigation.list.setFocus()
        testing.key(window.navigation.list, Qt.Key.Key_Space, text=" ")
        self.assertEqual(self.player.state(), "paused")
        window.navigation.set_current("music")
        window.music_page.search.setFocus()
        testing.type_text(window.music_page.search, "m m")
        self.assertEqual((window.music_page.search.text(), self.player.state(), self.player.muted),
                         ("m m", "paused", False))

    def test_open_shortcuts_and_folder(self) -> None:
        window = self.open()
        folder = self.tmp / "album"
        folder.mkdir()
        fakes.make_wav(folder / "2 second.wav", 0.1)
        fakes.make_wav(folder / "10 tenth.wav", 0.1)
        asked = []
        window.choose_files = lambda title, file_filter: asked.append(title) or []
        window.choose_folder = lambda title: asked.append(title) or str(folder)
        shortcut(window, "Ctrl+O")
        self.assertEqual(asked, ["Open files"])
        self.assertIn("*.mp3", window.MEDIA_FILTER)
        self.assertIn("*.mkv", window.MEDIA_FILTER)
        shortcut(window, "Ctrl+Shift+O")
        self.settle()
        self.assertEqual(self.titles(), ["2 second", "10 tenth"])

    def test_command_line_second_start_and_drop(self) -> None:
        first = self.wav("first.wav", "First")
        window = self.open([first, "--unknown-option", "ftp://example.org/nope.mp3"])
        self.assertEqual((self.titles(), self.player.state()), (["First"], "playing"))
        window.handle_arguments([self.wav("later.wav", "Later"), "https://radio.example.org/live.ogg"])
        self.settle()
        self.assertEqual(self.titles(), ["First", "Later", "live.ogg"])
        self.assertEqual(window.controller.current().title, "First")           # the running track goes on
        self.assertEqual(window.toast.severity(), "success")
        self.assertIn("Added 2 items", window.toast.message())
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(self.wav("dropped file.wav", "Dropped")), QUrl("mailto:someone@example.org")])
        from PySide6.QtGui import QDragEnterEvent
        enter = QDragEnterEvent(QPoint(200, 200), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
        window.dragEnterEvent(enter)                    # Qt delivers these only during a real drag
        self.assertTrue(enter.isAccepted())
        drop = QDropEvent(QPointF(200, 200), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        window.dropEvent(drop)
        self.settle()
        self.assertEqual(self.titles(), ["Dropped"])
        text = QMimeData()
        text.setText("not a file")
        refused = QDragEnterEvent(QPoint(200, 200), Qt.DropAction.CopyAction, text, Qt.MouseButton.LeftButton,
                                  Qt.KeyboardModifier.NoModifier)
        refused.ignore()
        window.dragEnterEvent(refused)
        self.assertFalse(refused.isAccepted())

    def test_seek_slider_click_drag_and_hover(self) -> None:
        window = self.open([self.wav("a.wav", "A")])
        self.player.durations[window.controller.current().location] = 100_000
        window.controller.play_index(0)
        self.settle()
        slider = window.bar.seek
        self.assertEqual(slider.maximum(), 100_000)
        self.assertEqual(window.bar.duration_label.text(), "1:40")
        middle = QPoint(slider.width() // 2, slider.height() // 2)
        self.assertAlmostEqual(slider.time_at(middle.x()), 50_000, delta=1500)
        testing.click(slider, middle)
        self.assertAlmostEqual(self.player.position(), 50_000, delta=1500)
        # Dragging shows the time under the handle and seeks once, on release.
        seeks = []
        window.controller.seeked.connect(seeks.append)
        quarter = QPointF(slider.width() / 4, slider.height() / 2)
        for kind, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                              (QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)):
            QApplication.sendEvent(slider, QMouseEvent(kind, quarter, slider.mapToGlobal(quarter),
                                                       Qt.MouseButton.LeftButton, buttons,
                                                       Qt.KeyboardModifier.NoModifier))
        self.assertTrue(slider.is_dragging())
        self.assertEqual(seeks, [])
        self.assertEqual(window.bar.position_label.text(), "0:24")
        self.player.advance(1000)                                              # playback goes on: the handle stays
        self.assertAlmostEqual(slider.sliderPosition(), 25_000, delta=1500)
        self.assertEqual(slider.hover_text, "0:24")
        QApplication.sendEvent(slider, QMouseEvent(QEvent.Type.MouseButtonRelease, quarter,
                                                   slider.mapToGlobal(quarter), Qt.MouseButton.LeftButton,
                                                   Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(len(seeks), 1)
        self.assertAlmostEqual(self.player.position(), 25_000, delta=1500)
        slider.setFocus()
        testing.key(slider, Qt.Key.Key_Right)
        self.assertAlmostEqual(self.player.position(), 35_000, delta=1500)

    def test_volume_speed_shuffle_repeat(self) -> None:
        window = self.open([self.wav("a.wav"), self.wav("b.wav")])
        window.bar.volume.setValue(30)
        self.assertEqual((self.player.volume, window.settings.volume), (0.3, 0.3))
        self.assertEqual(window.bar.mute.toolTip(), "Mute (M)")
        testing.click(window.bar.mute)
        self.assertTrue(self.player.muted)
        menu = window.bar.rate_menu()
        self.assertEqual([action.text() for action in menu.actions()],
                         ["0.25×", "0.5×", "0.75×", "1× (normal)", "1.25×", "1.5×", "1.75×", "2×"])
        self.assertEqual([action.text() for action in menu.actions() if action.isChecked()], ["1× (normal)"])
        menu.actions()[-1].trigger()
        self.assertEqual((self.player.rate, window.bar.rate.text()), (2.0, "2×"))
        testing.click(window.bar.shuffle)
        self.assertTrue(window.controller.queue.shuffle and window.bar.shuffle.isChecked())
        self.assertEqual(window.bar.shuffle.accessibleName(), "Shuffle: on")
        for expected, name in (("all", "Repeat: all"), ("one", "Repeat: one"), ("off", "Repeat: off")):
            testing.click(window.bar.repeat)
            self.assertEqual((window.controller.queue.repeat, window.bar.repeat.accessibleName()), (expected, name))
        shortcut(window, "Ctrl+H")
        shortcut(window, "Ctrl+T")
        self.assertEqual((window.controller.queue.shuffle, window.controller.queue.repeat), (False, "all"))

    def test_track_menus_appear_only_when_there_is_a_choice(self) -> None:
        window = self.open([self.wav("a.wav")])

        def submenus() -> list[str]:
            return [action.text() for action in window.bar.more_menu().actions() if action.menu() is not None]

        self.assertEqual(submenus(), ["Playback speed"])
        self.player.tracks = {"audio": ["English", "Deutsch"], "video": ["Main", "Angle 2"], "subtitle": ["English"]}
        self.assertEqual(submenus(), ["Audio track", "Subtitles", "Video track", "Playback speed"])
        menu = window.bar.more_menu()
        audio = next(action.menu() for action in menu.actions() if action.text() == "Audio track")
        self.assertEqual([(a.text(), a.isChecked()) for a in audio.actions()], [("English", True), ("Deutsch", False)])
        audio.actions()[1].trigger()
        self.assertEqual(self.player.active["audio"], 1)
        subtitles = next(action.menu() for action in menu.actions() if action.text() == "Subtitles")
        self.assertEqual([(a.text(), a.isChecked()) for a in subtitles.actions()], [("Off", True), ("English", False)])
        subtitles.actions()[1].trigger()
        self.assertEqual(self.player.active["subtitle"], 0)
        self.player.tracks["audio"] = ["Only"]
        self.assertNotIn("Audio track", submenus())

    def test_a_file_that_cannot_be_played_is_skipped_with_a_message(self) -> None:
        good, bad = self.wav("good.wav", "Good"), str(self.tmp / "bad.broken")
        Path(bad).write_bytes(b"not media")
        window = self.open([bad, good])
        self.assertEqual((window.controller.current().title, self.player.state()), ("Good", "playing"))
        self.assertTrue(window.toast.isVisible())
        self.assertEqual(window.toast.severity(), "warning")
        self.assertIn("Couldn't play “bad”", window.toast.message())
        self.assertIn("Skipped", window.toast.message())
        self.assertNotIn("Traceback", window.toast.message())
        testing.click(window.toast.close_button)
        self.assertFalse(window.toast.isVisible())

    def test_open_url_dialog(self) -> None:
        window = self.open()

        def answer(text: str, then=None) -> None:
            dialog, edit, error = window.url_dialog
            edit.setText(text)
            dialog.primary_button.click()
            if then is not None:
                QTimer.singleShot(30, then)

        seen = []

        def second() -> None:
            dialog, edit, error = window.url_dialog
            seen.append((error.isVisible(), error.text()))
            answer("https://radio.example.org/live/jazz.mp3")

        QTimer.singleShot(30, lambda: answer("ftp://example.org/file.mp3", second))
        shortcut(window, "Ctrl+U")
        self.settle()
        self.assertEqual(seen, [(True, "Only addresses that start with http:// or https:// can be opened.")])
        track = window.controller.current()
        self.assertEqual(track.location, "https://radio.example.org/live/jazz.mp3")
        self.assertTrue(window.bar.stream_badge.isVisibleTo(window.bar))
        self.assertEqual(window.bar.subtitle.text(), "radio.example.org")
        window.navigation.set_current("queue")
        self.assertEqual(window.controller.queue.items[0].subtitle, "Network stream · radio.example.org")
        for text, problem in (("", "Enter"), ("http://", "host"), ("https://exa mple.org/a", "spaces"),
                              ("javascript:alert(1)", "http://"), ("file:///etc/passwd", "http://")):
            address, why = window.check_stream_address(text)
            self.assertIsNone(address, text)
            self.assertIn(problem, why)
        QTimer.singleShot(30, lambda: window.url_dialog[0].close_button.click())
        testing.click(window.home_page.open_url) if window.navigation.current() == "home" else window.open_url_dialog()
        self.assertEqual(window.controller.current().location, track.location)


class ViewsTest(WindowCase):
    def play_video(self) -> MainWindow:
        film = str(fakes.make_mp4(self.tmp / "Film.mp4", {}, duration=600_000, video=True))
        window = self.open([film, self.wav("song.wav", "Song")])
        self.player.durations[film] = 600_000
        return window

    def test_video_opens_the_now_playing_page_and_full_screen_hides_the_controls(self) -> None:
        window = self.play_video()
        self.assertTrue(window.showing_now_playing())
        self.assertIs(window.now_playing.stack.currentWidget(), window.now_playing.video)
        self.assertTrue(window.now_playing.video.has_frame())
        self.assertIsNotNone(window.now_playing.video.image())
        self.assertTrue(window.bar.isVisible())
        window._hide_timer.setInterval(40)
        testing.key(window, Qt.Key.Key_F, text="f")
        self.assertTrue(window.is_fullscreen() and window.isFullScreen())
        self.assertFalse(window.now_playing.header.isVisible())
        self.assertEqual(window.bar.fullscreen.accessibleName(), "Leave full screen")
        self.assertTrue(testing.wait_until(window.controls_hidden, 2))
        self.assertEqual(window.now_playing.video.cursor().shape(), Qt.CursorShape.BlankCursor)
        window.now_playing.video.activity.emit()                               # the pointer moved
        self.assertTrue(window.bar.isVisible())
        self.assertNotEqual(window.now_playing.video.cursor().shape(), Qt.CursorShape.BlankCursor)
        self.assertTrue(testing.wait_until(window.controls_hidden, 2))
        testing.key(window, Qt.Key.Key_Space, text=" ")                        # paused: the controls stay
        self.assertEqual(self.player.state(), "paused")
        testing.process(120)
        self.assertTrue(window.bar.isVisible())
        testing.key(window, Qt.Key.Key_Escape)
        self.assertFalse(window.is_fullscreen() or window.isFullScreen())
        self.assertTrue(window.now_playing.header.isVisible() and window.bar.isVisible())
        shortcut(window, "F11")
        self.assertTrue(window.is_fullscreen())
        window.now_playing.video.double_clicked.emit()
        self.assertFalse(window.is_fullscreen())
        testing.click(window.now_playing.back)
        self.assertFalse(window.showing_now_playing())
        testing.click(window.bar.cover)
        self.assertTrue(window.showing_now_playing())
        shortcut(window, "Alt+Left")
        self.assertFalse(window.showing_now_playing())

    def test_audio_leaves_full_screen_and_shows_the_cover_page(self) -> None:
        window = self.play_video()
        window.toggle_fullscreen()
        window.controller.next()
        self.settle()
        self.assertFalse(window.is_fullscreen())
        self.assertIs(window.now_playing.stack.currentWidget(), window.now_playing.audio)
        self.assertEqual(window.now_playing.title.text(), "Song")
        window.controller.clear()
        window.toggle_fullscreen()                                              # nothing to show: stays as it is
        self.assertFalse(window.is_fullscreen())

    def test_mini_player(self) -> None:
        window = self.play_video()
        window.resize(780, 700)                         # within the 800 x 800 screen of the offscreen platform
        size = window.size()
        testing.click(window.bar.mini)
        self.assertTrue(window.is_mini())
        self.assertIs(window.shell.pages.currentWidget(), window.mini_player)
        self.assertFalse(window.bar.isVisible())
        self.assertLessEqual(window.width(), 400)
        self.assertEqual(window.mini_player.title.text(), "Film")
        self.assertIs(window.mini_player.stack.currentWidget(), window.mini_player.video)
        testing.click(window.mini_player.play)
        self.assertEqual(self.player.state(), "paused")
        testing.click(window.mini_player.next)
        self.settle()
        self.assertEqual(window.mini_player.title.text(), "Song")
        self.assertFalse(window.mini_player.next.isEnabled())
        testing.click(window.mini_player.leave)
        self.assertFalse(window.is_mini())
        self.assertTrue(window.bar.isVisible())
        self.assertEqual(window.size(), size)
        shortcut(window, "Ctrl+M")
        self.assertTrue(window.is_mini())
        testing.key(window, Qt.Key.Key_Escape)
        self.assertFalse(window.is_mini())

    def test_state_comes_back_after_a_restart(self) -> None:
        window = self.open([self.wav("a.wav", "A"), self.wav("b.wav", "B")])
        window.controller.next()
        self.settle()
        self.player.advance(61_000)
        window.controller.set_volume(0.4)
        window.resize(760, 700)
        window.close()
        self.assertTrue(self.player.closed)
        state = Path(os.environ["XDG_STATE_HOME"]) / "com.boswas.MediaPlayer"
        self.assertEqual(json.loads((state / "queue.json").read_text())["position"], 61_000)
        self.assertTrue((state / "window.ini").is_file())
        self.player, self.grabber = fakes.FakePlayer(), fakes.FakeGrabber()
        again = MainWindow([], backend=self.player, grabber=self.grabber, use_mpris=False, backend_info=dict)
        self.window = self.show(again)
        self.settle()
        self.assertEqual((self.titles(), again.bar.title.text()), (["A", "B"], "B"))
        self.assertEqual((self.player.state(), self.player.position(), self.player.volume), ("stopped", 61_000, 0.4))
        self.assertEqual(again.bar.position_label.text(), "1:01")
        self.assertEqual((again.width(), again.height()), (760, 700))
        self.assertEqual([tile.title for tile in again.home_page.tiles.tiles.tiles], ["B", "A"])

    def test_backend_that_is_missing_says_so(self) -> None:
        self.player.is_available = False
        window = self.open()
        self.assertEqual(window.toast.severity(), "error")
        self.assertIn("multimedia components", window.toast.message())


class LibraryTest(WindowCase):
    def test_songs_albums_artists(self) -> None:
        self.library()
        window = self.open()
        window.navigation.set_current("music")
        page = window.music_page
        table = page.table
        self.assertEqual([table.songs.text(song, "Title") for song in table.songs.songs],
                         ["Blue in Green", "Dreams", "So What", "The Chain"])
        self.assertEqual(page.count_label.text(), "4 songs")
        self.assertEqual(table.model().index(0, 1).data(), "Miles Davis")
        self.assertEqual(table.model().index(0, 5).data(), "0:03")
        table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.assertEqual(table.model().index(0, 1).data(), "Fleetwood Mac")
        table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        # Double click plays from that row, with the rest of the list queued.
        rect = table.visualRect(table.model().index(1, 0))
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick,
                     QEvent.Type.MouseButtonRelease):
            point = QPointF(rect.center())
            QApplication.sendEvent(table.viewport(), QMouseEvent(
                kind, point, table.viewport().mapToGlobal(point), Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        self.settle()
        self.assertEqual((window.controller.current().title, len(window.controller.queue)), ("Dreams", 4))
        self.assertEqual(table.songs.current, window.controller.current().location)
        self.assertEqual(window.bar.subtitle.text(), "Fleetwood Mac")

        page.pivot.set_current("albums")
        self.assertIs(page.stack.currentWidget(), page.album_tiles)
        self.assertEqual([(tile.title, tile.subtitle) for tile in page.album_tiles.tiles.tiles],
                         [("Kind of Blue", "Miles Davis"), ("Rumours", "Fleetwood Mac")])
        self.assertTrue(page.sort_box.isVisible())
        index = page.album_tiles.model().index(0, 0)
        testing.click(page.album_tiles.viewport(), page.album_tiles.visualRect(index).center())
        self.assertIs(page.stack.currentWidget(), page.detail)
        self.assertEqual(page.header.title.text(), "Kind of Blue")
        self.assertEqual(page.header.facts.text(), "Miles Davis · 1999 · 2 songs · 0:06")
        self.assertEqual([song.title for song in page.album_table.songs.songs], ["So What", "Blue in Green"])
        self.settle()
        self.assertTrue(page.header.cover.has_picture())
        testing.click(page.header.play)
        self.settle()
        self.assertEqual(self.titles(), ["So What", "Blue in Green"])
        shortcut(window, "Alt+Left")
        self.assertIs(page.stack.currentWidget(), page.album_tiles)

        page.pivot.set_current("artists")
        self.assertEqual([(tile.title, tile.subtitle) for tile in page.artist_tiles.tiles.tiles],
                         [("Fleetwood Mac", "2 songs"), ("Miles Davis", "2 songs")])
        page.artist_tiles.setCurrentIndex(page.artist_tiles.model().index(1, 0))
        page.artist_tiles.setFocus()
        testing.key(page.artist_tiles, Qt.Key.Key_Return)
        self.assertEqual((page.header.title.text(), page.header.facts.text()),
                         ("Miles Davis", "1 album · 2 songs · 0:06"))
        testing.click(page.header.back)
        self.assertIs(page.stack.currentWidget(), page.artist_tiles)

    def test_search_and_empty_states(self) -> None:
        window = self.open()
        window.navigation.set_current("music")
        page = window.music_page
        self.assertIs(page.stack.currentWidget(), page.empty)
        self.assertEqual(page.empty.title_label.text(), "Your music library is empty")
        self.assertIn(str(self.music), page.empty.text_label.text())
        self.assertFalse(page.shuffle_all.isEnabled())
        self.library()
        shortcut(window, "F5")
        self.settle()
        self.assertIs(page.stack.currentWidget(), page.table)
        shortcut(window, "Ctrl+F")
        self.assertTrue(page.search.hasFocus())
        testing.type_text(page.search, "blue")
        self.assertEqual([song.title for song in page.table.songs.songs], ["Blue in Green", "So What"])
        testing.type_text(page.search, "zzz")
        self.assertIs(page.stack.currentWidget(), page.empty)
        self.assertEqual(page.empty.title_label.text(), "No results")
        self.assertFalse(page.empty.action_button.isVisible())
        page.search.clear()
        testing.click(page.shuffle_all)
        self.settle()
        self.assertEqual(sorted(self.titles()), ["Blue in Green", "Dreams", "So What", "The Chain"])

    def test_song_menu(self) -> None:
        self.library()
        window = self.open()
        window.navigation.set_current("music")
        page = window.music_page
        song = page.table.songs.songs[0]
        menu = window.track_menu([song.to_track()])
        labels = [action.text() for action in menu.actions() if not action.isSeparator()]
        self.assertEqual(labels, ["Play", "Play next", "Add to queue", "Add to playlist", "Open file location",
                                  "Properties"])
        window.controller.play_tracks([page.table.songs.songs[3].to_track()])
        self.settle()
        menu.actions()[1].trigger()                                             # Play next
        menu.actions()[2].trigger()                                             # Add to queue
        self.assertEqual(self.titles(), ["The Chain", "Blue in Green", "Blue in Green"])
        opened = []
        original = fluent.open_path
        fluent.open_path = lambda path: opened.append(str(path)) or True
        try:
            next(action for action in menu.actions() if action.text() == "Open file location").trigger()
        finally:
            fluent.open_path = original
        self.assertEqual(opened, [str(Path(song.path).parent)])
        next(action for action in menu.actions() if action.text() == "Properties").trigger()
        dialog = window.properties_dialog
        self.assertTrue(dialog.is_open())
        texts = [label.text() for label in dialog.findChildren(fluent.Label)]
        self.assertIn(song.path, texts)
        self.assertIn("Blue in Green", texts)
        dialog.close_button.click()
        # The menu of the table itself adds the ways to the album and the artist.
        requested = []
        page.table.menu_requested.connect(lambda songs, position: requested.append(len(songs)))
        page.table.selectRow(1)
        page.table.setFocus()
        testing.key(page.table, Qt.Key.Key_Menu)
        self.assertEqual(requested, [1])
        for popup in QApplication.topLevelWidgets():
            if isinstance(popup, fluent.MenuFlyout) and popup.isVisible():
                self.assertIn("Show album", [action.text() for action in popup.actions()])
                popup.close()

    def test_folders_in_settings_and_incremental_rescan(self) -> None:
        self.library()
        window = self.open()
        extra = self.tmp / "more music"
        extra.mkdir()
        fakes.make_wav(extra / "extra.wav", 0.1, info={"INAM": "Extra"})
        window.navigation.set_current("settings")
        settings = window.navigation.page("settings")
        rows = [card.title() for card in settings.expanders["music"].cards()]
        self.assertEqual(rows, [str(self.music)])
        window.choose_folder = lambda title: str(extra)
        testing.click(settings.add_buttons["music"])
        self.settle()
        self.assertEqual(window.settings.music_folders, [str(self.music), str(extra)])
        self.assertEqual([card.title() for card in settings.expanders["music"].cards()], [str(self.music), str(extra)])
        self.assertIn("Extra", [song.title for song in window.library.songs()])
        stored = json.loads(window.settings_path.read_text())
        self.assertEqual(stored["music_folders"], [str(self.music), str(extra)])
        testing.click(settings.add_buttons["music"])                            # the same folder again
        self.assertEqual(window.toast.severity(), "info")
        self.assertEqual(len(window.settings.music_folders), 2)
        remove = settings.expanders["music"].cards()[0].control()
        self.assertEqual(remove.accessibleName(), f"Remove {self.music}")
        testing.click(remove)
        self.settle()
        self.assertEqual([song.title for song in window.library.songs()], ["Extra"])
        self.assertIn("1 song and 2 videos", settings.refresh_card.description())
        # A rescan of an unchanged library reads no file.
        from boswas_apps.media_player import library as library_module
        reads = []
        original = library_module.tags.read_tags
        library_module.tags.read_tags = lambda path: reads.append(path) or original(path)
        try:
            testing.click(settings.refresh_button)
            self.settle()
        finally:
            library_module.tags.read_tags = original
        self.assertEqual(reads, [])
        index = json.loads((Path(os.environ["XDG_STATE_HOME"]) / "com.boswas.MediaPlayer/library.json").read_text())
        self.assertEqual(len(index["songs"]), 3)

    def test_video_library_thumbnails_and_generic_tiles(self) -> None:
        self.library()
        self.grabber.fails.add(str(self.videos / "Birthday.mp4"))
        window = self.open()
        window.navigation.set_current("video")
        page = window.video_page
        testing.grab(window, self.tmp / "video.png")                            # painting asks for the thumbnails
        self.settle()
        tiles = page.tiles.tiles
        self.assertEqual([(tile.title, tile.subtitle) for tile in tiles.tiles],
                         [("Birthday", "1:30"), ("Holiday", "1:30")])
        self.assertEqual(sorted(self.grabber.requests), sorted(str(self.videos / f"{n}.mp4") for n in
                                                               ("Birthday", "Holiday")))
        self.assertIsNone(tiles.picture(tiles.tiles[0]))                        # no frame: the generic tile
        self.assertIsNotNone(tiles.picture(tiles.tiles[1]))
        thumbs = Path(os.environ["XDG_CACHE_HOME"]) / "com.boswas.MediaPlayer/thumbnails"
        self.assertEqual(sorted(path.suffix for path in thumbs.iterdir()), [".none", ".png"])
        index = page.tiles.model().index(1, 0)
        testing.click(page.tiles.viewport(), page.tiles.visualRect(index).center())
        self.settle()
        self.assertEqual(window.controller.current().title, "Holiday")
        self.assertTrue(window.showing_now_playing())
        window.leave_now_playing()
        testing.type_text(page.search, "nothing")
        self.assertEqual(page.empty.title_label.text(), "No results")

    def test_settings_playback(self) -> None:
        window = self.open()
        window.navigation.set_current("settings")
        settings = window.navigation.page("settings")
        self.assertEqual([settings.device_box.itemText(i) for i in range(settings.device_box.count())],
                         ["System default", "Speakers", "Headset"])
        settings.device_box.setCurrentIndex(2)
        settings.device_box.activated.emit(2)
        self.settle()
        self.assertEqual((self.player.device, window.settings.audio_device), ("headset", "headset"))
        self.assertTrue(settings.resume.isChecked())
        testing.click(settings.resume)
        self.settle()
        self.assertFalse(window.settings.resume_videos)
        self.assertFalse(json.loads(window.settings_path.read_text())["resume_videos"])
        self.player.devices = []
        self.player.devices_changed.emit()
        self.assertFalse(settings.device_box.isEnabled())
        self.assertIn("No audio output device was found", settings.device_card.description())
        self.assertEqual(settings.theme_value.text(), "Dark")
        testing.set_scheme("light")
        self.assertEqual(settings.theme_value.text(), "Light")
        self.assertIn("FLAC", settings.about.description())


class QueueAndPlaylistTest(WindowCase):
    def start(self) -> MainWindow:
        window = self.open([self.wav(f"{name}.wav", name.title()) for name in ("one", "two", "three")])
        return window

    def test_queue_page(self) -> None:
        window = self.start()
        window.navigation.set_current("queue")
        page = window.queue_page
        view = page.list
        self.assertEqual([view.item(row).text() for row in range(view.count())], ["One", "Two", "Three"])
        self.assertEqual((view.current_row, view.playing, page.total.text()), (0, True, "3 items · 3:00"))
        view.setFocus()
        view.setCurrentRow(0)
        testing.key(view, Qt.Key.Key_Down, ALT)
        self.assertEqual((self.titles(), window.controller.queue.index, view.currentRow()),
                         (["Two", "One", "Three"], 1, 1))
        testing.key(view, Qt.Key.Key_Up, ALT)
        self.assertEqual(self.titles(), ["One", "Two", "Three"])
        view.request_move(2, 0)                                                # what a drop does
        self.assertEqual(self.titles(), ["Three", "One", "Two"])
        view.setCurrentRow(2)
        testing.key(view, Qt.Key.Key_Return)
        self.settle()
        self.assertEqual((window.controller.current().title, view.current_row), ("Two", 2))
        view.setCurrentRow(0)
        testing.key(view, Qt.Key.Key_Delete)
        self.assertEqual(self.titles(), ["One", "Two"])
        testing.key(view, Qt.Key.Key_Space, text=" ")
        self.assertEqual(self.player.state(), "paused")
        chosen = [self.wav("four.wav", "Four")]
        window.choose_files = lambda title, file_filter: chosen
        testing.click(page.commands.button(page.add_action))
        self.settle()
        self.assertEqual(self.titles(), ["One", "Two", "Four"])
        testing.click(page.commands.button(page.clear_action))
        self.assertEqual((self.titles(), self.player.source), ([], ""))
        self.assertIs(page.stack.currentWidget(), page.empty)
        self.assertFalse(page.clear_action.isEnabled())
        self.assertEqual(window.bar.title.text(), "Nothing is playing")

    def name_dialog(self, window: MainWindow, *answers: str) -> list[str]:
        """Type each answer into the name dialog and confirm; returns the error shown after each."""
        errors: list[str] = []
        pending = list(answers)

        def step() -> None:
            if window.text_dialog is None:
                return
            dialog, edit, error = window.text_dialog
            if not pending:
                return
            if len(pending) < len(answers):
                errors.append(error.text() if error.isVisible() else "")
            edit.selectAll()
            edit.clear()
            testing.type_text(edit, pending.pop(0))
            QTimer.singleShot(40, step)
            testing.key(edit, Qt.Key.Key_Return)

        QTimer.singleShot(40, step)
        return errors

    def test_playlists(self) -> None:
        window = self.start()
        window.navigation.set_current("playlists")
        page = window.playlists_page
        self.assertEqual(page.master_stack.currentIndex(), 1)                   # "No playlists yet"
        self.name_dialog(window, "Morning")
        testing.click(page.new_button)
        self.settle()
        self.assertEqual(page.names(), ["Morning"])
        self.assertTrue((Path(os.environ["XDG_CONFIG_HOME"]) / "com.boswas.MediaPlayer/playlists/Morning.m3u8")
                        .is_file())
        errors = self.name_dialog(window, "morning", "a/b", "Evening")
        shortcut(window, "Ctrl+N")
        self.settle()
        self.assertEqual(errors, ["There is already a playlist called “morning”.",
                                  "A playlist name cannot contain / or \\, start with a dot or be longer than "
                                  "100 characters."])
        self.assertEqual(page.names(), ["Evening", "Morning"])

        # "Add to playlist" from anywhere: the queue's command, and a track's menu.
        menu = fluent.MenuFlyout(window)
        window.fill_playlist_menu(menu, list(window.controller.queue.items))
        self.assertEqual([action.text() for action in menu.actions() if not action.isSeparator()],
                         ["New playlist…", "Evening", "Morning"])
        menu.actions()[-1].trigger()
        self.settle()
        self.assertIn("Added 3 items to “Morning”", window.toast.message())
        self.assertEqual(page.items["Morning"].subtitle(), "3 items · 3:00")

        page.items["Morning"].click()
        self.assertIs(page.stack.currentWidget(), page.detail)
        self.assertEqual((page.header.title.text(), page.header.facts.text()), ("Morning", "3 items · 3:00"))
        view = page.list
        view.setFocus()
        view.setCurrentRow(0)
        testing.key(view, Qt.Key.Key_Down, ALT)
        view.setCurrentRow(2)
        testing.key(view, Qt.Key.Key_Delete)
        self.settle()
        self.assertEqual([track.title for track in window.playlists.load("Morning")], ["Two", "One"])
        testing.click(page.header.play)
        self.settle()
        self.assertEqual(self.titles(), ["Two", "One"])

        self.name_dialog(window, "Dawn")
        shortcut(window, "F2")
        self.settle()
        self.assertEqual((page.names(), page.current, page.header.title.text()), (["Dawn", "Evening"], "Dawn", "Dawn"))

        target = self.tmp / "exported.pls"
        window.choose_save = lambda title, name, file_filter: str(target)
        testing.click(page.commands.button(page.export_action))
        self.settle()
        self.assertIn("[playlist]", target.read_text())
        self.assertIn("Title1=Two", target.read_text())
        window.choose_files = lambda title, file_filter: [str(target)]
        testing.click(page.header.back)
        testing.click(page.import_button)
        self.settle()
        self.assertEqual(page.names(), ["Dawn", "Evening", "exported"])
        self.assertEqual([track.title for track in window.playlists.load("exported")], ["Two", "One"])

        asked = []
        window.ask = lambda title, text, confirm="OK", cancel="Cancel", **kw: asked.append((title, confirm)) or False
        page.delete("Evening")
        self.settle()
        self.assertEqual((asked, len(page.names())), ([("Delete this playlist?", "Delete")], 3))
        window.ask = lambda *args, **kw: True
        page.open("Dawn")
        testing.click(page.commands.button(page.delete_action))
        self.settle()
        self.assertEqual(page.names(), ["Evening", "exported"])
        self.assertIs(page.stack.currentWidget(), page.master)
        self.assertIn("Deleted the playlist “Dawn”", window.toast.message())

    def test_a_playlist_file_that_cannot_be_read_is_shown_as_such(self) -> None:
        window = self.start()
        folder = Path(os.environ["XDG_CONFIG_HOME"]) / "com.boswas.MediaPlayer/playlists"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "Huge.m3u8").write_bytes(b"x" * (5 * 1024 * 1024))
        window.playlists_changed.emit()
        self.settle()
        page = window.playlists_page
        self.assertEqual(page.items["Huge"].subtitle(), "This file could not be read")
        page.items["Huge"].click()
        self.assertIs(page.stack.currentWidget(), page.master)
        self.assertEqual(window.toast.severity(), "error")


class QualityTest(WindowCase):
    def test_every_button_has_a_name_and_icon_buttons_a_tool_tip(self) -> None:
        self.library()
        window = self.open([self.wav("a.wav", "A")])
        for key in ("music", "video", "queue", "playlists", "settings", "home"):
            window.navigation.set_current(key)
            testing.process()
        nameless, untipped = [], []
        for button in window.findChildren(QAbstractButton):
            if button.window() is not window or button.objectName().startswith("qt_"):
                continue
            name = button.accessibleName() or button.text()
            if not name.strip():
                nameless.append(repr(button))
            if isinstance(button, QToolButton) and not button.text() and not button.toolTip():
                untipped.append(name)
        self.assertEqual((nameless, untipped), ([], []))
        self.assertEqual(window.bar.seek.accessibleName(), "Seek")
        self.assertEqual(window.bar.volume.accessibleName(), "Volume")

    def test_tab_reaches_the_transport_controls(self) -> None:
        window = self.open([self.wav("a.wav", "A"), self.wav("b.wav", "B")])
        window.activateWindow()
        window.bar.shuffle.setFocus()
        reached = set()
        for _step in range(12):
            reached.add(QApplication.focusWidget())
            testing.press_tab()
        for widget in (window.bar.previous, window.bar.play, window.bar.next, window.bar.repeat, window.bar.mute,
                       window.bar.volume, window.bar.more, window.bar.mini, window.bar.fullscreen):
            self.assertIn(widget, reached, widget.accessibleName())
        window.bar.play.setFocus()
        testing.key(window.bar.play, Qt.Key.Key_Space, text=" ")               # the focused button, not the shortcut
        self.assertEqual(self.player.state(), "paused")

    def test_no_colour_values_in_the_sources(self) -> None:
        pattern = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|\brgba?\(|QColor\(\s*\d|setStyleSheet")
        found = []
        for path in sorted(PACKAGE.glob("*.py")):
            for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(found, [])

    def test_narrow_window_keeps_the_essentials(self) -> None:
        window = self.open([self.wav("a.wav", "A")])
        window.resize(500, 460)
        testing.process(50)
        bar = window.bar
        self.assertFalse(bar.volume.isVisible() or bar.rate.isVisible() or bar.shuffle.isVisible())
        for widget in (bar.previous, bar.play, bar.next, bar.mute, bar.more, bar.fullscreen, bar.seek):
            self.assertTrue(widget.isVisible())
            self.assertTrue(bar.rect().contains(widget.geometry()), widget.accessibleName())
        self.assertLessEqual(bar.next.geometry().right(), bar.mute.geometry().left())
        self.assertTrue(window.navigation.is_compact())


if __name__ == "__main__":
    unittest.main()
