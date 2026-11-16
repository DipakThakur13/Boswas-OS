"""The Camera window on the fake camera (offscreen, private directories, no hardware)."""

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

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QToolButton  # noqa: E402

from boswas_apps.camera import storage, window as window_module  # noqa: E402
from boswas_apps.camera.backend import Features, VideoFormat  # noqa: E402
from boswas_apps.camera.window import CameraWindow  # noqa: E402

PACKAGE = Path(window_module.__file__).resolve().parent
NAME = re.compile(r"^(IMG|VID)_\d{8}_\d{6}(_\d+)?\.(jpg|mp4)$")


class CameraCase(testing.FluentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._home = os.environ.get("HOME")
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        self.photos = self.home / "Pictures/Camera Roll"
        self.videos = self.home / "Videos/Camera Roll"
        self.camera = fakes.FakeCamera([fakes.webcam()])
        self.window: CameraWindow | None = None
        self._free = storage.free_bytes
        self.addCleanup(lambda: setattr(storage, "free_bytes", self._free))

    def tearDown(self) -> None:
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        super().tearDown()

    def open(self) -> CameraWindow:
        self.window = self.show(CameraWindow([], backend=self.camera), 1000, 680)
        self.window.tick_ms = 10
        self.settle()
        return self.window

    def settle(self) -> None:
        for _round in range(3):
            self.window.worker.wait(5)
            testing.process(15)

    def files(self, folder: Path) -> list[str]:
        return sorted(entry.name for entry in folder.iterdir()) if folder.is_dir() else []


class StatesTest(CameraCase):
    def test_no_camera_then_one_is_connected(self) -> None:
        self.camera.devices = []
        window = self.open()
        self.assertEqual(window.state, "none")
        self.assertIs(window.centre.currentWidget(), window.message)
        self.assertEqual(window.message.title_label.text(), "Connect a camera")
        self.assertFalse(window.shutter.isEnabled())
        self.assertFalse(window.switch_button.isVisible())
        testing.click(window.message.action_button)                # retry: still nothing
        self.assertEqual((window.state, window.toast.severity()), ("none", "info"))
        window.shutter_pressed()
        self.assertEqual(self.files(self.photos), [])
        self.camera.plug(fakes.webcam())                           # QMediaDevices.videoInputsChanged
        self.settle()
        self.assertEqual(window.state, "ready")
        self.assertIs(window.centre.currentWidget(), window.viewfinder)
        self.assertTrue(window.viewfinder.has_frame() and window.shutter.isEnabled())

    def test_busy_camera_shows_the_message_of_the_system(self) -> None:
        self.camera.busy["cam0"] = "Device or resource busy."
        window = self.open()
        self.assertEqual(window.state, "error")
        self.assertEqual(window.message.title_label.text(), "The camera can't be used right now")
        self.assertIn("The system says: “Device or resource busy”.", window.message.text_label.text())
        self.assertFalse(window.shutter.isEnabled())
        self.camera.busy.clear()
        testing.click(window.message.action_button)
        self.settle()
        self.assertEqual(window.state, "ready")
        shortcut_used = self.camera.calls.count(("start", "cam0"))
        self.assertEqual(shortcut_used, 2)

    def test_camera_that_never_answers_times_out(self) -> None:
        self.camera.silent.add("cam0")
        window = self.open()
        self.assertEqual((window.state, window.message.title_label.text()), ("starting", "Starting the camera…"))
        self.assertFalse(window.message.action_button.isVisible())
        window._start_timeout.setInterval(20)
        window._start_timeout.start()
        testing.process(80)
        self.assertEqual(window.state, "error")
        self.assertIn("did not start", window.message.text_label.text())

    def test_unplugged_while_recording_keeps_the_video(self) -> None:
        window = self.open()
        window.set_mode("video")
        testing.click(window.shutter)
        self.settle()
        self.camera.record(4000)
        self.camera.unplug("cam0")
        self.settle()
        self.assertEqual(window.state, "unplugged")
        self.assertEqual(window.message.title_label.text(), "The camera was disconnected")
        self.assertFalse(window.is_recording())
        saved = self.files(self.videos)
        self.assertEqual(len(saved), 1)
        self.assertGreater((self.videos / saved[0]).stat().st_size, 4000)
        self.assertEqual(window.viewfinder.badge, "")
        self.camera.plug(fakes.webcam())
        self.settle()
        self.assertEqual(window.state, "ready")

    def test_unplugged_with_another_camera_switches_to_it(self) -> None:
        self.camera.devices.append(fakes.webcam("cam1", "USB Camera"))
        window = self.open()
        self.assertTrue(window.switch_button.isVisible())
        self.assertEqual(window.camera_label.text(), "Integrated Webcam")
        self.camera.unplug("cam0")
        self.settle()
        self.assertEqual((window.state, self.camera.current()), ("ready", "cam1"))
        self.assertEqual(window.toast.severity(), "warning")
        self.assertIn("Integrated Webcam was disconnected. Now using USB Camera.", window.toast.message())
        self.assertFalse(window.switch_button.isVisible())

    def test_switch_camera(self) -> None:
        self.camera.devices.append(fakes.webcam("cam1", "USB Camera"))
        window = self.open()
        testing.click(window.switch_button)
        self.settle()
        self.assertEqual((self.camera.current(), window.camera_label.text(), window.settings.camera),
                         ("cam1", "USB Camera", "cam1"))
        testing.click(window.switch_button)
        self.settle()
        self.assertEqual(self.camera.current(), "cam0")
        window.close()
        self.camera = fakes.FakeCamera([fakes.webcam("cam1", "USB Camera"), fakes.webcam()])
        again = self.open()
        self.assertEqual(self.camera.current(), "cam0")            # the camera used last, not the first in the list
        self.assertEqual(again.state, "ready")


class PhotoTest(CameraCase):
    def test_photo_is_saved_to_the_camera_roll(self) -> None:
        window = self.open()
        self.assertEqual(window.shutter.accessibleName(), "Take photo")
        self.assertFalse(window.thumbnail.isEnabled())
        testing.click(window.shutter)
        self.assertGreater(fluent.duration("slow"), 0)
        self.assertTrue(testing.wait_until(window.viewfinder.is_flashing, 3))  # the shutter animation
        self.settle()
        names = self.files(self.photos)
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], NAME)
        image = QImage(str(self.photos / names[0]))
        self.assertFalse(image.isNull())
        self.assertEqual(self.camera.sounds, 1)
        self.assertTrue(window.thumbnail.isEnabled() and window.thumbnail.has_picture())
        self.assertEqual(window.thumbnail.path, str(self.photos / names[0]))
        testing.key(window, Qt.Key.Key_Space, text=" ")
        self.settle()
        self.assertEqual(len(self.files(self.photos)), 2)
        self.assertTrue(testing.wait_until(lambda: not window.viewfinder.is_flashing(), 3))

    def test_reduced_motion_and_sounds_off(self) -> None:
        fluent.manager().set_reduced_motion(True)
        self.addCleanup(lambda: fluent.manager().set_reduced_motion(None))
        window = self.open()
        window.settings.sounds = False
        testing.click(window.shutter)
        self.assertFalse(window.viewfinder.is_flashing())
        self.settle()
        self.assertEqual((len(self.files(self.photos)), self.camera.sounds), (1, 0))

    def test_timer_counts_down_and_can_be_cancelled(self) -> None:
        window = self.open()
        self.assertEqual(window.timer_label.text(), "Off")
        seen = []
        for expected in (3, 5, 10, 0):
            testing.click(window.timer_button)
            seen.append(window.timer_seconds)
        self.assertEqual(seen, [3, 5, 10, 0])
        testing.click(window.timer_button)
        self.assertEqual((window.timer_label.text(), window.timer_button.accessibleName()), ("3 s", "Timer: 3 seconds"))
        window.tick_ms = 60_000
        testing.click(window.shutter)
        self.assertEqual(window.viewfinder.countdown, 3)
        self.assertEqual(window.shutter.accessibleName(), "Cancel the timer")
        self.assertFalse(window.photo_button.isEnabled() or window.settings_button.isEnabled())
        testing.key(window, Qt.Key.Key_Escape)
        self.assertEqual((window.viewfinder.countdown, window.is_busy()), (0, False))
        testing.click(window.shutter)
        testing.click(window.shutter)                                  # the shutter cancels too
        self.assertEqual(window.viewfinder.countdown, 0)
        self.settle()
        self.assertEqual(self.files(self.photos), [])
        window.tick_ms = 10
        counts = []
        original = window.viewfinder.set_countdown
        window.viewfinder.set_countdown = lambda value: (counts.append(value), original(value))[1]
        testing.click(window.shutter)
        self.assertTrue(testing.wait_until(lambda: len(self.files(self.photos)) == 1, 5))
        self.assertEqual(counts, [3, 2, 1, 0])

    def test_mirror_and_grid(self) -> None:
        window = self.open()
        self.assertTrue(window.settings.mirror and window.mirror_button.isChecked())

        def left_is_light() -> bool:
            image = window.viewfinder.grab().toImage()
            return image.pixelColor(20, image.height() // 2).value() > image.pixelColor(
                image.width() - 20, image.height() // 2).value()

        self.assertFalse(left_is_light())                              # mirrored: the light third is on the right
        testing.click(window.mirror_button)
        self.settle()
        self.assertTrue(left_is_light())
        self.assertEqual(window.mirror_button.accessibleName(), "Mirror the preview: off")
        testing.click(window.grid_button)
        self.settle()
        self.assertTrue(window.viewfinder.grid)
        stored = json.loads(window.settings_path.read_text())
        self.assertEqual((stored["mirror"], stored["grid"]), (False, True))
        testing.click(window.shutter)
        self.settle()
        saved = QImage(str(self.photos / self.files(self.photos)[0]))
        self.assertGreater(saved.pixelColor(5, 5).value(), saved.pixelColor(saved.width() - 5, 5).value())

    def test_photo_that_fails_leaves_no_empty_file(self) -> None:
        window = self.open()
        self.camera.photo_error = "Could not write the image."
        testing.click(window.shutter)
        self.settle()
        self.assertEqual(window.toast.severity(), "error")
        self.assertEqual(window.toast.message(), "The photo was not saved. Could not write the image.")
        self.assertEqual(self.files(self.photos), [])
        self.assertTrue(window.shutter.isEnabled())

    def test_folder_that_cannot_be_used(self) -> None:
        (self.home / "Pictures").write_text("a file where the folder should be")
        window = self.open()
        testing.click(window.shutter)
        self.settle()
        self.assertEqual(window.toast.severity(), "error")
        self.assertIn("Couldn't save to", window.toast.message())
        self.assertNotIn("Traceback", window.toast.message())
        self.assertEqual(self.camera.calls, [("start", "cam0")])

    def test_thumbnail_opens_photos_and_falls_back(self) -> None:
        window = self.open()
        testing.click(window.shutter)
        self.settle()
        launched = []
        window.launch_viewer = lambda path: launched.append(path) or True
        testing.click(window.thumbnail)
        self.assertEqual(launched, [window.thumbnail.path])
        del window.launch_viewer
        opened = []
        original = fluent.open_path
        fluent.open_path = lambda path: opened.append(str(path)) or True
        try:
            window.viewer = str(self.tmp / "no-such-program")
            testing.click(window.thumbnail)                            # Photos is not installed: default application
            self.assertEqual(opened, [window.thumbnail.path])
            fake_viewer = self.tmp / "viewer"
            fake_viewer.write_text("#!/bin/sh\nexit 0\n")
            fake_viewer.chmod(0o755)
            window.viewer = str(fake_viewer)
            self.assertTrue(window.launch_viewer(window.thumbnail.path))
            self.assertEqual(len(opened), 1)                           # the program was started instead
            os.unlink(window.thumbnail.path)
            testing.click(window.thumbnail)
            self.assertEqual(window.toast.severity(), "warning")
        finally:
            fluent.open_path = original

    def test_last_capture_of_an_earlier_run_is_shown(self) -> None:
        self.photos.mkdir(parents=True)
        fakes.picture(64, 48).save(str(self.photos / "IMG_20250101_120000.jpg"), "JPEG")
        window = self.open()
        self.assertEqual(Path(window.thumbnail.path).name, "IMG_20250101_120000.jpg")
        self.assertTrue(window.thumbnail.has_picture())


class VideoTest(CameraCase):
    def record(self) -> CameraWindow:
        window = self.open()
        testing.click(window.video_button)
        self.assertEqual((window.mode, window.shutter.accessibleName()), ("video", "Start recording"))
        testing.click(window.shutter)
        self.settle()
        return window

    def test_record_pause_resume_stop(self) -> None:
        window = self.record()
        self.assertTrue(window.is_recording())
        path, quality, audio = self.camera.recordings[0]
        self.assertRegex(Path(path).name, NAME)
        self.assertEqual((Path(path).parent, quality, audio), (self.videos, "high", True))
        self.assertEqual(window.shutter.accessibleName(), "Stop recording")
        self.assertFalse(window.photo_button.isEnabled() or window.settings_button.isEnabled())
        self.camera.record(61_000)
        self.assertEqual((window.viewfinder.badge, window.viewfinder.badge_live), ("01:01", True))
        self.assertTrue(window.pause_button.isVisible())
        testing.click(window.pause_button)
        self.assertEqual(self.camera.recording_state(), "paused")
        self.assertEqual((window.viewfinder.badge, window.pause_button.accessibleName()),
                         ("Paused · 01:01", "Resume recording"))
        testing.click(window.pause_button)
        self.camera.record(2000)
        self.assertEqual(window.viewfinder.badge, "01:03")
        testing.click(window.mic_button)
        self.assertTrue(self.camera.muted)
        self.assertEqual(window.mic_button.accessibleName(), "Microphone: off")
        testing.click(window.shutter)
        self.settle()
        self.assertFalse(window.is_recording())
        self.assertEqual(self.files(self.videos), [Path(path).name])
        self.assertGreater(Path(path).stat().st_size, 60_000)
        self.assertEqual((window.viewfinder.badge, window.toast.severity()), ("", "success"))
        self.assertEqual(window.thumbnail.path, path)
        self.assertTrue(window.thumbnail.has_picture())                # the last picture of the recording
        self.assertTrue(window.photo_button.isEnabled())

    def test_without_microphone_or_pause(self) -> None:
        self.camera.microphone = False
        self.camera.pausable = False
        window = self.record()
        self.assertEqual(self.camera.recordings[0][2], False)
        self.assertFalse(window.mic_button.isVisible() or window.pause_button.isVisible())
        testing.key(window, Qt.Key.Key_Space, text=" ")
        self.settle()
        self.assertEqual(len(self.files(self.videos)), 1)

    def test_recording_does_not_start_when_the_disk_is_nearly_full(self) -> None:
        storage.free_bytes = lambda folder: 212 * storage.MIB
        window = self.record()
        self.assertFalse(window.is_recording())
        self.assertEqual(self.camera.recordings, [])
        self.assertEqual(window.toast.severity(), "error")
        self.assertIn("The disk is almost full: 212 MiB free", window.toast.message())
        self.assertIn("at least 500 MiB", window.toast.message())
        self.assertEqual(self.files(self.videos), [])
        self.assertTrue(window.shutter.isEnabled() and window.photo_button.isEnabled())
        window.set_mode("photo")                                       # a photo needs far less
        testing.click(window.shutter)
        self.settle()
        self.assertEqual(len(self.files(self.photos)), 1)

    def test_recording_ends_when_the_disk_fills_up(self) -> None:
        window = self.record()
        self.camera.record(5000)
        storage.free_bytes = lambda folder: 60 * storage.MIB
        window._check_space()
        self.settle()
        self.assertFalse(window.is_recording())
        self.assertEqual(len(self.files(self.videos)), 1)
        self.assertEqual(window.toast.severity(), "warning")
        self.assertIn("because the disk is almost full (60 MiB left)", window.toast.message())

    def test_recording_that_fails(self) -> None:
        self.camera.record_error = "The encoder could not be started."
        window = self.record()
        self.assertEqual(window.toast.message(), "The recording stopped. The encoder could not be started.")
        self.assertEqual(self.files(self.videos), [])
        self.assertFalse(window.is_busy())
        self.camera.record_error = ""
        testing.click(window.shutter)
        self.settle()
        self.camera.fail_recording("No space left on device.")
        self.settle()
        self.assertIn("No space left on device.", window.toast.message())
        self.assertFalse(window.is_recording())

    def test_closing_the_window_finishes_the_recording(self) -> None:
        window = self.record()
        self.camera.record(3000)
        window.close()
        self.assertEqual(self.camera.recording_state(), "stopped")
        self.assertTrue(self.camera.closed)
        saved = self.files(self.videos)
        self.assertEqual(len(saved), 1)
        self.assertGreater((self.videos / saved[0]).stat().st_size, 3000)

    def test_timer_applies_to_recordings(self) -> None:
        window = self.open()
        window.set_mode("video")
        window.timer_seconds = 3
        testing.click(window.shutter)
        self.assertEqual(self.camera.recordings, [])
        self.assertTrue(testing.wait_until(lambda: len(self.camera.recordings) == 1, 5))
        self.settle()
        self.assertTrue(window.is_recording())


class FeaturesTest(CameraCase):
    def test_controls_exist_only_for_what_the_camera_reports(self) -> None:
        window = self.open()
        for widget in (window.flash_button, window.torch_button, window.adjust_button, window.zoom_row):
            self.assertFalse(window.isVisible() and widget.isVisible())
        window.close()
        self.camera = fakes.FakeCamera([fakes.webcam()])
        self.camera.capabilities = Features(zoom=(1.0, 4.0), flash_modes=("off", "on", "auto"), torch=True,
                                            exposure=True, focus_distance=True)
        window = self.open()
        self.assertTrue(window.flash_button.isVisible() and window.adjust_button.isVisible())
        self.assertTrue(window.zoom_row.isVisible())
        self.assertFalse(window.torch_button.isVisible())              # the light belongs to video
        self.assertEqual((window.zoom_slider.minimum(), window.zoom_slider.maximum()), (100, 400))
        window.zoom_slider.setValue(250)
        self.assertEqual((self.camera.zoom, window.zoom_label.text()), (2.5, "2.5×"))
        names = []
        for _press in range(3):
            testing.click(window.flash_button)
            names.append((self.camera.flash, window.flash_button.accessibleName()))
        self.assertEqual(names, [("on", "Flash: on"), ("auto", "Flash: automatic"), ("off", "Flash: off")])
        window.set_mode("video")
        self.assertTrue(window.torch_button.isVisible())
        self.assertFalse(window.flash_button.isVisible())
        testing.click(window.torch_button)
        self.assertTrue(self.camera.torch)
        window.set_mode("photo")
        self.assertFalse(self.camera.torch)
        window.show_adjust()
        window.exposure_slider.setValue(-15)
        self.assertEqual(self.camera.exposure, -1.5)
        self.assertFalse(window.focus_slider.isEnabled())
        window.focus_switch.setChecked(False)
        window.focus_slider.setValue(80)
        self.assertEqual(self.camera.focus, 0.8)
        window.focus_switch.setChecked(True)
        self.assertIsNone(self.camera.focus)
        window.adjust_flyout.close()


class SettingsTest(CameraCase):
    def test_settings_page(self) -> None:
        window = self.open()
        testing.click(window.settings_button)
        self.assertTrue(window.showing_settings())
        self.assertEqual([window.photo_size_box.itemText(i) for i in range(window.photo_size_box.count())],
                         ["Automatic", "1920 × 1080", "1280 × 720", "640 × 480"])
        self.assertEqual(window.video_size_box.itemText(2), "1280 × 720, 60 fps")
        self.assertEqual(window.photo_size_card.description(), "What Integrated Webcam offers")
        window.photo_size_box.setCurrentIndex(2)
        window.photo_size_box.activated.emit(2)
        window.video_size_box.setCurrentIndex(2)
        window.video_size_box.activated.emit(2)
        self.assertEqual((self.camera.photo_size, self.camera.video_format), ((1280, 720), VideoFormat(1280, 720, 60)))
        for box, index in ((window.mode_box, 1), (window.timer_box, 2), (window.quality_box, 0)):
            box.setCurrentIndex(index)
            box.activated.emit(index)
        self.assertTrue(window.sound_switch.isChecked() and window.mirror_switch.isChecked())
        testing.click(window.sound_switch)
        testing.click(window.grid_switch)
        self.settle()
        self.assertTrue(window.viewfinder.grid and window.grid_button.isChecked())
        stored = json.loads(window.settings_path.read_text())
        self.assertEqual((stored["default_mode"], stored["timer"], stored["quality"], stored["sounds"], stored["grid"]),
                         ("video", 5, "low", False, True))
        self.assertEqual((stored["photo_sizes"], stored["video_sizes"]),
                         ({"cam0": [1280, 720]}, {"cam0": [1280, 720, 60]}))
        self.assertIn("500 MiB", " ".join(card.description() for card in window.settings_page.findChildren(
            fluent.SettingsCard)))

        chosen = self.tmp / "my photos"
        chosen.mkdir()
        window.pick_folder = lambda title, start: str(chosen)
        self.assertEqual(window.folder_cards["photo"].description(), str(self.photos))
        self.assertFalse(window.reset_buttons["photo"].isVisible())
        testing.click(window.folder_buttons["photo"])
        self.settle()
        self.assertEqual(window.folder_cards["photo"].description(), str(chosen))
        testing.key(window, Qt.Key.Key_Escape)
        self.assertFalse(window.showing_settings())
        testing.click(window.shutter)
        self.settle()
        self.assertEqual(len(self.files(chosen)), 1)
        self.assertEqual(self.files(self.photos), [])
        window.show_settings()
        testing.click(window.reset_buttons["photo"])
        self.settle()
        self.assertEqual(window.settings.photo_dir, "")
        testing.click(window.settings_back)
        window.close()

        self.camera = fakes.FakeCamera([fakes.webcam()])
        again = self.open()
        self.assertEqual((again.mode, again.timer_seconds, again.settings.quality), ("video", 5, "low"))
        self.assertTrue(again.video_button.isChecked())
        self.assertEqual((self.camera.photo_size, self.camera.video_format), ((1280, 720), VideoFormat(1280, 720, 60)))
        again.timer_seconds = 0
        testing.click(again.shutter)
        self.settle()
        self.assertEqual(self.camera.recordings[0][1], "low")

    def test_settings_cannot_be_opened_while_recording(self) -> None:
        window = self.open()
        window.set_mode("video")
        testing.click(window.shutter)
        self.settle()
        window.show_settings()
        self.assertFalse(window.showing_settings())
        window.set_mode("photo")
        self.assertEqual(window.mode, "video")
        window.switch_camera()
        self.assertEqual(self.camera.current(), "cam0")


class QualityTest(CameraCase):
    def test_every_button_has_a_name_and_icon_buttons_a_tool_tip(self) -> None:
        self.camera.capabilities = Features(zoom=(1.0, 2.0), flash_modes=("off", "on"), torch=True, exposure=True)
        self.camera.devices.append(fakes.webcam("cam1", "USB Camera"))
        window = self.open()
        window.show_settings()
        window.show_camera()
        testing.click(window.shutter)
        self.settle()
        nameless, untipped = [], []
        for button in window.findChildren(QAbstractButton):
            if button.objectName().startswith("qt_"):
                continue
            name = button.accessibleName() or button.text()
            if not name.strip():
                nameless.append(repr(button))
            if isinstance(button, QToolButton) and not button.text() and not button.toolTip():
                untipped.append(name)
        self.assertEqual((nameless, untipped), ([], []))
        self.assertEqual(window.zoom_slider.accessibleName(), "Zoom")
        window.shutter.setFocus()
        reached = set()
        for _step in range(16):
            reached.add(testing.press_tab())
        for widget in (window.thumbnail, window.settings_button, window.timer_button, window.mirror_button,
                       window.photo_button, window.video_button, window.switch_button):
            self.assertIn(widget, reached, widget.accessibleName())

    def test_no_colour_values_in_the_sources(self) -> None:
        pattern = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|\brgba?\(|QColor\(\s*\d|setStyleSheet")
        found = []
        for path in sorted(PACKAGE.glob("*.py")):
            for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(found, [])

    def test_small_window_keeps_the_controls_inside(self) -> None:
        window = self.open()
        window.resize(520, 400)
        testing.process(50)
        for widget in (window.shutter, window.thumbnail, window.photo_button, window.video_button,
                       window.settings_button):
            top_left = widget.mapTo(window, widget.rect().topLeft())
            bottom_right = widget.mapTo(window, widget.rect().bottomRight())
            self.assertTrue(window.rect().contains(top_left) and window.rect().contains(bottom_right),
                            widget.accessibleName())
        self.assertGreater(window.viewfinder.width(), 200)


if __name__ == "__main__":
    unittest.main()
