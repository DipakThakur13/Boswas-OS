"""The window of Camera: the live picture between two panels of controls, and the settings page."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QCoreApplication, QProcess, QSize, Qt, QTimer
from PySide6.QtGui import QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QScrollArea, QSlider, QStackedWidget, QVBoxLayout,
                               QWidget)

import boswas_fluent as fluent

from . import APP_ID, __version__, storage
from .backend import CameraBackend, CameraInfo, Features, VideoFormat
from .storage import Settings
from .widgets import ShutterButton, Thumbnail, Viewfinder, icon

VIEWER = "/usr/bin/boswas-photos"
START_TIMEOUT_MS = 10_000
SPACE_CHECK_MS = 5000
CLOSE_WAIT_S = 5.0
_FLASH_NAMES = {"off": "Flash: off", "on": "Flash: on", "auto": "Flash: automatic"}


def describe_error(error: Exception) -> str:
    if isinstance(error, OSError) and error.strerror:
        return f"{error.strerror} ({error.filename})" if error.filename else error.strerror
    return str(error) or error.__class__.__name__


def load_thumbnail(path: str, size: int = 112) -> QImage | None:
    """Worker thread: a small picture of a photo file (None for a video or a file that is not a picture)."""
    if storage.is_video(path):
        return None
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    full = reader.size()
    if not full.isValid() or full.width() * full.height() > 200_000_000:
        return None
    reader.setScaledSize(full.scaled(QSize(size, size), Qt.AspectRatioMode.KeepAspectRatioByExpanding))
    image = reader.read()
    return None if image.isNull() else image


class _Panel(QWidget):
    """A strip of controls beside the picture."""

    def paintEvent(self, _event) -> None:
        QPainter(self).fillRect(self.rect(), fluent.qcolor("surface"))


class CameraWindow(fluent.FluentWindow):
    def __init__(self, arguments: list[str] | None = None, *, backend: CameraBackend | None = None) -> None:
        super().__init__(APP_ID, "Camera", icon="camera-photo", size=(1040, 680), minimum_size=(520, 400))
        self.settings_path = fluent.config_dir(APP_ID) / "settings.json"
        self.settings = Settings.load(self.settings_path)
        if backend is None:
            from .backend import QtCameraBackend
            backend = QtCameraBackend(self)
        self.backend = backend
        self.worker = fluent.Worker(self)
        self.mode = self.settings.default_mode
        self.timer_seconds = self.settings.timer
        self.state = "none"               # none, starting, ready, error, unplugged
        self.tick_ms = 1000               # one step of the countdown (tests shorten it)
        self.viewer = VIEWER
        self._cameras: list[CameraInfo] = []
        self._features = Features()
        self._count = 0
        self._pending: str = ""           # the capture that waits for its file: "photo" or "video"
        self._reserved = ""
        self._record_path = ""
        self._last_frame: QImage | None = None
        self._flash_mode = "off"
        self._muted = False
        self._closing = False

        self.stack = QStackedWidget(self)
        self.set_content(self.stack)
        self.stack.addWidget(self._build_camera_page())
        self.settings_page: QWidget | None = None

        self._countdown = QTimer(self)
        self._countdown.timeout.connect(self._tick)
        self._start_timeout = QTimer(self)
        self._start_timeout.setSingleShot(True)
        self._start_timeout.timeout.connect(self._start_timed_out)
        self._space_timer = QTimer(self)
        self._space_timer.setInterval(SPACE_CHECK_MS)
        self._space_timer.timeout.connect(self._check_space)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)

        backend.cameras_changed.connect(self.refresh_cameras)
        backend.active_changed.connect(self._active_changed)
        backend.error.connect(self._camera_error)
        backend.frame_changed.connect(self.viewfinder.set_frame)
        backend.features_changed.connect(self._sync_features)
        backend.photo_saved.connect(self._photo_saved)
        backend.photo_failed.connect(self._photo_failed)
        backend.recording_changed.connect(self._recording_changed)
        backend.recording_time.connect(self._recording_time)
        backend.recording_saved.connect(self._recording_saved)
        backend.recording_failed.connect(self._recording_failed)

        for keys, slot in (("Alt+Left", self.show_camera), ("F5", self.retry), ("Ctrl+W", self.close),
                           ("Ctrl+Q", self.close), ("Ctrl+,", self.show_settings)):
            self.add_shortcut(keys, slot)
        self.viewfinder.set_options(mirror=self.settings.mirror, grid=self.settings.grid)
        self._sync_controls()
        self.refresh_cameras()
        self._find_last_capture()

    # --- building ---
    def _build_camera_page(self) -> QWidget:
        page = QWidget(self)
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        left = _Panel(page)
        left.setFixedWidth(64)
        column = QVBoxLayout(left)
        column.setContentsMargins(12, 12, 12, 12)
        column.setSpacing(8)
        self.settings_button = fluent.IconButton(icon("settings"), "Settings (Ctrl+,)", left, size=40, icon_size=18)
        self.timer_button = fluent.IconButton(icon("timer"), "", left, size=40, icon_size=18, checkable=True)
        self.mirror_button = fluent.IconButton(icon("mirror"), "", left, size=40, icon_size=18, checkable=True)
        self.grid_button = fluent.IconButton(icon("grid"), "", left, size=40, icon_size=18, checkable=True)
        self.flash_button = fluent.IconButton(icon("flash"), "", left, size=40, icon_size=18, checkable=True)
        self.torch_button = fluent.IconButton(icon("torch"), "Light", left, size=40, icon_size=18, checkable=True)
        self.adjust_button = fluent.IconButton(icon("adjust"), "Exposure and focus", left, size=40, icon_size=18)
        self.timer_label = fluent.Label("", "caption", left, tone="secondary")
        self.timer_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(self.settings_button)
        column.addSpacing(8)
        column.addWidget(self.timer_button)
        column.addWidget(self.timer_label)
        for button in (self.mirror_button, self.grid_button, self.flash_button, self.torch_button,
                       self.adjust_button):
            column.addWidget(button)
        column.addStretch(1)
        row.addWidget(left)

        centre = QWidget(page)
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        centre_layout.setSpacing(0)
        self.centre = QStackedWidget(centre)
        self.viewfinder = Viewfinder(self.centre)
        self.message = fluent.EmptyState("", "", "camera-web", "Try again", self.centre)
        self.message.action_triggered.connect(self.retry)
        self.centre.addWidget(self.viewfinder)
        self.centre.addWidget(self.message)
        centre_layout.addWidget(self.centre, 1)
        self.zoom_row = _Panel(centre)
        zoom = QHBoxLayout(self.zoom_row)
        zoom.setContentsMargins(24, 6, 24, 6)
        zoom.setSpacing(12)
        self.zoom_label = fluent.Label("1×", "caption", self.zoom_row, tone="secondary")
        self.zoom_label.setMinimumWidth(36)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self.zoom_row)
        self.zoom_slider.setAccessibleName("Zoom")
        self.zoom_slider.setRange(100, 100)
        zoom.addStretch(1)
        zoom.addWidget(fluent.Label("Zoom", "caption", self.zoom_row, tone="secondary"))
        zoom.addWidget(self.zoom_slider)
        zoom.addWidget(self.zoom_label)
        zoom.addStretch(1)
        self.zoom_slider.setFixedWidth(240)
        centre_layout.addWidget(self.zoom_row)
        self.toast = fluent.InfoBar("info", parent=centre)
        self.toast.hide()
        centre_layout.addWidget(self.toast)
        row.addWidget(centre, 1)

        right = _Panel(page)
        right.setFixedWidth(112)
        column = QVBoxLayout(right)
        column.setContentsMargins(12, 12, 12, 16)
        column.setSpacing(10)
        centred = Qt.AlignmentFlag.AlignHCenter
        self.switch_button = fluent.IconButton(icon("switch"), "Change camera", right, size=40, icon_size=18)
        self.camera_label = fluent.Label("", "caption", right, tone="secondary", wrap=True)
        self.camera_label.setAlignment(centred)
        self.video_button = fluent.IconButton(icon("video"), "Video", right, size=40, icon_size=18, checkable=True)
        self.shutter = ShutterButton(right)
        self.photo_button = fluent.IconButton(icon("photo"), "Photo", right, size=40, icon_size=18, checkable=True)
        self.pause_button = fluent.IconButton(icon("pause"), "Pause recording", right, size=40, icon_size=18)
        self.mic_button = fluent.IconButton(icon("microphone"), "", right, size=40, icon_size=18, checkable=True)
        self.thumbnail = Thumbnail(right)
        column.addWidget(self.switch_button, 0, centred)
        column.addWidget(self.camera_label)
        column.addStretch(1)
        column.addWidget(self.video_button, 0, centred)
        column.addWidget(self.shutter, 0, centred)
        column.addWidget(self.photo_button, 0, centred)
        column.addSpacing(8)
        column.addWidget(self.pause_button, 0, centred)
        column.addWidget(self.mic_button, 0, centred)
        column.addStretch(1)
        column.addWidget(self.thumbnail, 0, centred)
        row.addWidget(right)

        self.settings_button.clicked.connect(self.show_settings)
        self.timer_button.clicked.connect(self.cycle_timer)
        self.mirror_button.clicked.connect(lambda checked: self.set_option("mirror", checked))
        self.grid_button.clicked.connect(lambda checked: self.set_option("grid", checked))
        self.flash_button.clicked.connect(self.cycle_flash)
        self.torch_button.clicked.connect(lambda checked: self.backend.set_torch(checked))
        self.adjust_button.clicked.connect(self.show_adjust)
        self.switch_button.clicked.connect(self.switch_camera)
        self.photo_button.clicked.connect(lambda: self.set_mode("photo"))
        self.video_button.clicked.connect(lambda: self.set_mode("video"))
        self.shutter.clicked.connect(self.shutter_pressed)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.mic_button.clicked.connect(self.set_muted)
        self.thumbnail.clicked.connect(self.open_last)
        self.zoom_slider.valueChanged.connect(self._zoom_changed)
        return page

    # --- messages ---
    def notify(self, severity: str, text: str) -> None:
        self.toast.show_message(severity, text)
        self._toast_timer.stop()
        if severity != "error":
            self._toast_timer.start(10_000 if severity == "warning" else 5000)

    def run(self, function: Callable, *arguments, done: Callable | None = None, what: str = "do that") -> None:
        self.worker.submit(function, *arguments, on_done=done,
                           on_error=lambda error: self.notify("error", f"Couldn't {what}: {describe_error(error)}"))

    def save_settings(self) -> None:
        self.run(self.settings.save, self.settings_path, what="save the settings")

    # --- cameras and states ---
    def cameras(self) -> list[CameraInfo]:
        return list(self._cameras)

    def camera_info(self) -> CameraInfo | None:
        current = self.backend.current()
        return next((camera for camera in self._cameras if camera.id == current), None)

    def refresh_cameras(self) -> None:
        """Called at the start and whenever a camera is connected or removed."""
        self._cameras = self.backend.cameras()
        ids = [camera.id for camera in self._cameras]
        current = self.backend.current()
        if current and current not in ids:
            self._camera_lost()
            return
        if not self._cameras:
            if self.state != "unplugged":
                self._show_state("none")
        elif self.state in ("none", "unplugged", "error"):
            wanted = self.settings.camera if self.settings.camera in ids else ids[0]
            self.start_camera(wanted)
        self._sync_controls()

    def _camera_lost(self) -> None:
        lost = next((c.name for c in getattr(self, "_known", []) if c.id == self.backend.current()), "The camera")
        self.cancel_countdown()
        if self.backend.recording_state() != "stopped":
            self.backend.stop_recording()             # what was recorded so far is kept
        self.backend.stop()
        if self._cameras:
            self.start_camera(self._cameras[0].id)
            self.notify("warning", f"{lost} was disconnected. Now using {self._cameras[0].name}.")
        else:
            self._show_state("unplugged")
        self._sync_controls()

    def start_camera(self, camera_id: str) -> None:
        self._known = list(self._cameras)
        self._show_state("starting")
        self._start_timeout.start(START_TIMEOUT_MS)
        info = next((camera for camera in self._cameras if camera.id == camera_id), None)
        photo = self.settings.photo_sizes.get(camera_id)
        video = self.settings.video_sizes.get(camera_id)
        self.backend.set_photo_size(tuple(photo) if info and photo and tuple(photo) in info.photo_sizes else None)
        wanted = VideoFormat(*video) if video else None
        self.backend.set_video_format(wanted if info and wanted in info.video_formats else None)
        self.backend.start(camera_id)

    def retry(self) -> None:
        self._cameras = self.backend.cameras()
        if not self._cameras:
            self._show_state("none")
            self.notify("info", "No camera was found. It starts by itself when you connect one.")
            return
        ids = [camera.id for camera in self._cameras]
        self.start_camera(self.settings.camera if self.settings.camera in ids else ids[0])
        self._sync_controls()

    def switch_camera(self) -> None:
        if len(self._cameras) < 2 or self.is_busy():
            return
        ids = [camera.id for camera in self._cameras]
        current = self.backend.current()
        following = ids[(ids.index(current) + 1) % len(ids)] if current in ids else ids[0]
        self.start_camera(following)

    def _active_changed(self, active: bool) -> None:
        if active and self.backend.current():
            self._start_timeout.stop()
            self.settings.camera = self.backend.current()
            self._show_state("ready")
            self._sync_features()
        self._sync_controls()

    def _camera_error(self, text: str) -> None:
        self._start_timeout.stop()
        self.cancel_countdown()
        self._show_state("error", text)
        self._sync_controls()

    def _start_timed_out(self) -> None:
        if self.state == "starting":
            self.backend.stop()
            self._show_state("error", "The camera did not start.")
            self._sync_controls()

    def _show_state(self, state: str, detail: str = "") -> None:
        self.state = state
        message = self.message
        if state == "ready":
            self.centre.setCurrentWidget(self.viewfinder)
            return
        if state == "none":
            message.set_title("Connect a camera")
            message.set_text("No camera was found. Connect one; it starts by itself as soon as it is there.")
            message.action_button.setText("Try again")
        elif state == "unplugged":
            message.set_title("The camera was disconnected")
            message.set_text("Connect it again to go on. What was recorded until then has been saved.")
            message.action_button.setText("Try again")
        elif state == "starting":
            message.set_title("Starting the camera…")
            message.set_text("")
        else:
            message.set_title("The camera can't be used right now")
            message.set_text(f"The system says: “{detail.rstrip('.')}”. Another program may be using the camera, "
                             "or this program may not be allowed to use it."
                             if detail else "Another program may be using it.")
            message.action_button.setText("Try again")
        message.action_button.setVisible(state != "starting")
        self.viewfinder.set_frame(None)
        self.centre.setCurrentWidget(message)

    # --- controls ---
    def is_recording(self) -> bool:
        return self.backend.recording_state() != "stopped"

    def is_busy(self) -> bool:
        return self.is_recording() or self._count > 0 or bool(self._pending)

    def _sync_controls(self) -> None:
        ready = self.state == "ready"
        recording = self.is_recording()
        busy = self.is_busy()
        video = self.mode == "video"
        self.shutter.setEnabled(ready and not (self._pending and not self._count))
        self.shutter.set_state(self.mode, recording, self._count > 0)
        self.photo_button.setChecked(not video)
        self.video_button.setChecked(video)
        for button in (self.photo_button, self.video_button):
            button.setEnabled(not busy)
        self.switch_button.setVisible(len(self._cameras) > 1)
        self.switch_button.setEnabled(not busy)
        info = self.camera_info()
        self.camera_label.setText(info.name if info is not None and len(self._cameras) > 1 else "")
        self.camera_label.setVisible(bool(self.camera_label.text()))
        self.pause_button.setVisible(recording and self.backend.can_pause())
        paused = self.backend.recording_state() == "paused"
        self.pause_button.set_icon(icon("resume" if paused else "pause"))
        text = "Resume recording" if paused else "Pause recording"
        self.pause_button.setToolTip(text)
        self.pause_button.setAccessibleName(text)
        microphone = self.backend.has_microphone()
        self.mic_button.setVisible(video and microphone)
        self.mic_button.setChecked(self._muted)
        self.mic_button.set_icon(icon("microphone-muted" if self._muted else "microphone"))
        text = "Microphone: off" if self._muted else "Microphone: on"
        self.mic_button.setToolTip(text)
        self.mic_button.setAccessibleName(text)
        self.timer_button.setChecked(self.timer_seconds > 0)
        self.timer_button.setEnabled(not busy)
        text = f"Timer: {self.timer_seconds} seconds" if self.timer_seconds else "Timer: off"
        self.timer_button.setToolTip(text)
        self.timer_button.setAccessibleName(text)
        self.timer_label.setText(f"{self.timer_seconds} s" if self.timer_seconds else "Off")
        for button, name, value in ((self.mirror_button, "Mirror the preview", self.settings.mirror),
                                    (self.grid_button, "Grid", self.settings.grid)):
            button.setChecked(value)
            text = f"{name}: {'on' if value else 'off'}"
            button.setToolTip(text)
            button.setAccessibleName(text)
        self.settings_button.setEnabled(not busy)
        self._sync_features()

    def _sync_features(self) -> None:
        features = self.backend.features() if self.state == "ready" else Features()
        self._features = features
        video = self.mode == "video"
        self.flash_button.setVisible(bool(features.flash_modes) and not video)
        if self._flash_mode not in features.flash_modes:
            self._flash_mode = "off"
        self.flash_button.setChecked(self._flash_mode != "off")
        self.flash_button.setToolTip(_FLASH_NAMES[self._flash_mode])
        self.flash_button.setAccessibleName(_FLASH_NAMES[self._flash_mode])
        self.torch_button.setVisible(features.torch and video)
        self.adjust_button.setVisible(features.exposure or features.focus_distance)
        self.zoom_row.setVisible(features.zoom is not None)
        if features.zoom is not None:
            low, high = features.zoom
            blocked = self.zoom_slider.blockSignals(True)
            self.zoom_slider.setRange(round(low * 100), round(high * 100))
            self.zoom_slider.blockSignals(blocked)
            self.zoom_label.setText(f"{self.zoom_slider.value() / 100:.1f}×")

    def _zoom_changed(self, value: int) -> None:
        self.zoom_label.setText(f"{value / 100:.1f}×")
        if self._features.zoom is not None:
            self.backend.set_zoom(value / 100)

    def cycle_flash(self) -> None:
        modes = self._features.flash_modes
        if not modes:
            return
        self._flash_mode = modes[(modes.index(self._flash_mode) + 1) % len(modes)]
        self.backend.set_flash(self._flash_mode)
        self._sync_features()

    def show_adjust(self) -> None:
        """A flyout with the exposure and focus sliders the camera offers."""
        flyout = fluent.Flyout(self)
        features = self._features
        if features.exposure:
            flyout.add_widget(fluent.Label("Exposure", "body-strong", flyout.surface))
            exposure = QSlider(Qt.Orientation.Horizontal, flyout.surface)
            exposure.setRange(-20, 20)
            exposure.setValue(round(getattr(self, "_exposure", 0.0) * 10))
            exposure.setAccessibleName("Exposure compensation")
            exposure.setMinimumWidth(220)
            exposure.valueChanged.connect(self._set_exposure)
            flyout.add_widget(exposure)
            self.exposure_slider = exposure
        if features.focus_distance:
            flyout.add_widget(fluent.Label("Focus", "body-strong", flyout.surface))
            automatic = fluent.ToggleSwitch("Automatic focus", flyout.surface, checked=self._focus is None,
                                            on_text="Automatic", off_text="Manual")
            focus = QSlider(Qt.Orientation.Horizontal, flyout.surface)
            focus.setRange(0, 100)
            focus.setValue(round((self._focus if self._focus is not None else 0.5) * 100))
            focus.setEnabled(self._focus is not None)
            focus.setAccessibleName("Focus distance")
            focus.setMinimumWidth(220)
            automatic.toggled.connect(lambda on: (focus.setEnabled(not on),
                                                  self._set_focus(None if on else focus.value() / 100)))
            focus.valueChanged.connect(lambda value: self._set_focus(value / 100))
            flyout.add_widget(automatic)
            flyout.add_widget(focus)
            self.focus_switch, self.focus_slider = automatic, focus
        self.adjust_flyout = flyout
        flyout.show_at(self.adjust_button, "right")

    _focus: float | None = None
    adjust_flyout = None

    def _set_exposure(self, tenths: int) -> None:
        self._exposure = tenths / 10
        self.backend.set_exposure(self._exposure)

    def _set_focus(self, distance: float | None) -> None:
        self._focus = distance
        self.backend.set_focus_distance(distance)

    def set_mode(self, mode: str) -> None:
        if mode in storage.MODES and not self.is_busy():
            self.mode = mode
            if mode == "photo" and self._features.torch:
                self.torch_button.setChecked(False)
                self.backend.set_torch(False)
        self._sync_controls()

    def cycle_timer(self) -> None:
        index = storage.TIMERS.index(self.timer_seconds) if self.timer_seconds in storage.TIMERS else 0
        self.timer_seconds = storage.TIMERS[(index + 1) % len(storage.TIMERS)]
        self._sync_controls()

    def set_option(self, name: str, value: bool) -> None:
        setattr(self.settings, name, bool(value))
        self.viewfinder.set_options(mirror=self.settings.mirror, grid=self.settings.grid)
        self.save_settings()
        self._sync_controls()
        self._sync_settings_page()

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self.backend.set_microphone_muted(self._muted)
        self._sync_controls()

    # --- capturing ---
    def shutter_pressed(self) -> None:
        if self.state != "ready":
            return
        if self._count > 0:
            self.cancel_countdown()
        elif self.is_recording():
            self.stop_recording()
        elif self._pending:
            return
        elif self.timer_seconds > 0:
            self._count = self.timer_seconds
            self.viewfinder.set_countdown(self._count)
            self._countdown.start(self.tick_ms)
            self._sync_controls()
        else:
            self._capture()

    def _tick(self) -> None:
        self._count -= 1
        self.viewfinder.set_countdown(self._count)
        if self._count <= 0:
            self._countdown.stop()
            self._count = 0
            self._capture()

    def cancel_countdown(self) -> None:
        if self._count > 0:
            self._countdown.stop()
            self._count = 0
            self.viewfinder.set_countdown(0)
            self._sync_controls()

    def _capture(self) -> None:
        """Check the free space and make the file (on the worker), then tell the camera."""
        kind = self.mode
        self._pending = kind
        self._sync_controls()
        folder = self.settings.photo_folder() if kind == "photo" else self.settings.video_folder()
        extension = ".jpg" if kind == "photo" else self.backend.video_extension()

        def prepare() -> tuple[str | None, str]:
            problem = storage.space_problem(folder, kind)
            if problem is not None:
                return None, problem
            return str(storage.reserve(folder, kind, extension)), ""

        def failed(error: Exception) -> None:
            self._pending = ""
            self._sync_controls()
            self.notify("error", f"Couldn't save to {folder}: {describe_error(error)}")

        self.worker.submit(prepare, on_done=lambda result: self._prepared(kind, *result), on_error=failed)

    def _prepared(self, kind: str, path: str | None, problem: str) -> None:
        if path is None or self.state != "ready":
            self._pending = ""
            self._sync_controls()
            if path is not None:
                self._discard(path)
            else:
                self.notify("error", problem)
            return
        self._reserved = path
        if kind == "photo":
            self.viewfinder.flash()
            if self.settings.sounds:
                self.backend.play_shutter_sound()
            self.backend.capture_photo(path)
        else:
            self._record_path = path
            self.backend.set_microphone_muted(self._muted)
            self.backend.start_recording(path, self.settings.quality, self.backend.has_microphone())

    def _discard(self, path: str) -> None:
        """Remove a reserved file that stayed empty."""
        def remove() -> None:
            try:
                if os.path.getsize(path) == 0:
                    os.unlink(path)
            except OSError:
                pass
        self.worker.submit(remove)

    def _photo_saved(self, path: str) -> None:
        reserved, self._reserved, self._pending = self._reserved, "", ""
        if reserved and path != reserved:
            self._discard(reserved)
        self._sync_controls()
        self._set_last(path)

    def _photo_failed(self, text: str) -> None:
        reserved, self._reserved, self._pending = self._reserved, "", ""
        if reserved:
            self._discard(reserved)
        self._sync_controls()
        self.notify("error", f"The photo was not saved. {text}")

    def stop_recording(self) -> None:
        if self.is_recording():
            self._last_frame = self.viewfinder.image()
            self.backend.stop_recording()

    def toggle_pause(self) -> None:
        state = self.backend.recording_state()
        if state == "recording":
            self.backend.pause_recording()
        elif state == "paused":
            self.backend.resume_recording()

    def _recording_changed(self, state: str) -> None:
        if state == "stopped":
            self._space_timer.stop()
            self.viewfinder.set_badge("")
            self._pending = ""
        else:
            self._pending = ""
            if not self._space_timer.isActive():
                self._space_timer.start()
            if state == "paused":
                self.viewfinder.set_badge(f"Paused · {self._elapsed_text}", False)
            else:
                self.viewfinder.set_badge(self._elapsed_text, True)
        self._sync_controls()

    _elapsed_text = "00:00"

    def _recording_time(self, milliseconds: int) -> None:
        self._elapsed_text = storage.format_elapsed(milliseconds)
        if self.backend.recording_state() == "recording":
            self.viewfinder.set_badge(self._elapsed_text, True)

    def _recording_saved(self, path: str) -> None:
        self._record_path = self._reserved = ""
        self._elapsed_text = "00:00"
        frame = self._last_frame
        self._last_frame = None
        self._set_last(path, frame)
        if not self._closing:
            self.notify("success", f"The video was saved to {os.path.dirname(path)}.")

    def _recording_failed(self, text: str) -> None:
        path, self._record_path, self._reserved, self._pending = self._record_path, "", "", ""
        if path:
            self._discard(path)
        self._sync_controls()
        self.notify("error", f"The recording stopped. {text}")

    def _check_space(self) -> None:
        folder = self.settings.video_folder()
        self.worker.submit(storage.free_bytes, folder, key="space", on_done=self._space_known)

    def _space_known(self, free: int) -> None:
        if self.is_recording() and free < storage.STOP_FREE_VIDEO:
            self.stop_recording()
            self.notify("warning", "The recording was ended because the disk is almost full "
                                   f"({storage.format_size(free)} left). The video has been saved.")

    # --- the last capture ---
    def _find_last_capture(self) -> None:
        folders = [self.settings.photo_folder(), self.settings.video_folder()]
        self.worker.submit(storage.latest_capture, folders, on_done=lambda path: self._set_last(path) if path else None)

    def _set_last(self, path: str, frame: QImage | None = None) -> None:
        self.thumbnail.set_capture(path, QPixmap.fromImage(frame) if frame is not None else None)
        if frame is None:
            self.worker.submit(load_thumbnail, path,
                               on_done=lambda image, path=path: self._thumbnail_loaded(path, image))

    def _thumbnail_loaded(self, path: str, image: QImage | None) -> None:
        if image is not None and self.thumbnail.path == path:
            self.thumbnail.set_capture(path, QPixmap.fromImage(image))

    def launch_viewer(self, path: str) -> bool:
        """Open ``path`` in Photos, or with the default application when Photos is not installed."""
        if not storage.is_video(path) and os.path.isfile(self.viewer) and os.access(self.viewer, os.X_OK):
            if QProcess.startDetached(self.viewer, [path])[0]:
                return True
        return fluent.open_path(path)

    def open_last(self) -> None:
        path = self.thumbnail.path
        if not path:
            return
        if not os.path.isfile(path):
            self.notify("warning", "That capture is no longer there.")
            self._find_last_capture()
            self.thumbnail.set_capture("", None)
        elif not self.launch_viewer(path):
            self.notify("warning", f"Couldn't open {path}.")

    # --- settings page ---
    def show_settings(self) -> None:
        if self.is_busy():
            return
        if self.settings_page is None:
            self.settings_page = self._build_settings_page()
            self.stack.addWidget(self.settings_page)
        self._sync_settings_page()
        self.stack.setCurrentWidget(self.settings_page)
        self.settings_back.setFocus()

    def show_camera(self) -> None:
        self.stack.setCurrentIndex(0)

    def showing_settings(self) -> bool:
        return self.settings_page is not None and self.stack.currentWidget() is self.settings_page

    def _build_settings_page(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(20, 16, 32, 12)
        head.setSpacing(12)
        self.settings_back = fluent.IconButton(icon("back"), "Back to the camera (Alt+Left)", page, size=36)
        self.settings_back.clicked.connect(self.show_camera)
        head.addWidget(self.settings_back)
        head.addWidget(fluent.Label("Settings", "title", page), 1)
        outer.addLayout(head)
        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(32, 0, 32, 32)
        layout.setSpacing(4)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        def section(title: str) -> None:
            layout.addSpacing(12)
            layout.addWidget(fluent.Label(title, "body-strong", body))
            layout.addSpacing(2)

        def combo(items: list[tuple[str, object]], changed: Callable[[object], None]) -> QComboBox:
            box = QComboBox(body)
            box.setMinimumWidth(220)
            for text, data in items:
                box.addItem(text, data)
            box.activated.connect(lambda index: changed(box.itemData(index)))
            return box

        section("Capturing")
        self.mode_box = combo([("Photo", "photo"), ("Video", "video")], lambda value: self._store("default_mode", value))
        layout.addWidget(fluent.SettingsCard("Start in", "The mode Camera opens with", "camera-photo",
                                             self.mode_box, body))
        self.timer_box = combo([("Off", 0), ("3 seconds", 3), ("5 seconds", 5), ("10 seconds", 10)],
                               lambda value: self._store("timer", value))
        layout.addWidget(fluent.SettingsCard("Timer", "The delay Camera opens with; the timer button changes it "
                                             "for now", icon("timer"), self.timer_box, body))
        self.mirror_switch = fluent.ToggleSwitch(parent=body)
        self.mirror_switch.toggled.connect(lambda on: self.set_option("mirror", on) if on != self.settings.mirror
                                           else None)
        layout.addWidget(fluent.SettingsCard("Mirror the preview", "Show the picture like a mirror. Photos and "
                                             "videos are saved the way the camera sees them.", icon("mirror"),
                                             self.mirror_switch, body))
        self.grid_switch = fluent.ToggleSwitch(parent=body)
        self.grid_switch.toggled.connect(lambda on: self.set_option("grid", on) if on != self.settings.grid else None)
        layout.addWidget(fluent.SettingsCard("Framing grid", "Lines that divide the picture into thirds",
                                             icon("grid"), self.grid_switch, body))
        self.sound_switch = fluent.ToggleSwitch(parent=body)
        self.sound_switch.toggled.connect(lambda on: self._store("sounds", on) if on != self.settings.sounds
                                          else None)
        layout.addWidget(fluent.SettingsCard("Shutter sound", "Play a sound when a photo is taken",
                                             "preferences-desktop-sound", self.sound_switch, body))

        section("Quality")
        self.photo_size_box = combo([], self._photo_size_chosen)
        self.photo_size_card = fluent.SettingsCard("Photo resolution", "", "camera-photo", self.photo_size_box, body)
        layout.addWidget(self.photo_size_card)
        self.video_size_box = combo([], self._video_size_chosen)
        self.video_size_card = fluent.SettingsCard("Video resolution", "", icon("video"), self.video_size_box, body)
        layout.addWidget(self.video_size_card)
        self.quality_box = combo([(storage.QUALITY_NAMES[key], key) for key in storage.QUALITIES],
                                 lambda value: self._store("quality", value))
        layout.addWidget(fluent.SettingsCard("Video quality", "Higher quality makes larger files", icon("video"),
                                             self.quality_box, body))

        section("Where captures are saved")
        self.folder_cards: dict[str, fluent.SettingsCard] = {}
        self.folder_buttons: dict[str, fluent.Button] = {}
        self.reset_buttons: dict[str, fluent.Button] = {}
        for kind, title, glyph in (("photo", "Photos", "folder-pictures"), ("video", "Videos", "folder-videos")):
            change = fluent.Button("Change", body)
            change.setAccessibleName(f"Change the folder for {title.lower()}")
            change.clicked.connect(lambda _checked=False, kind=kind: self.choose_folder(kind))
            reset = fluent.Button("Use Camera Roll", body, subtle=True)
            reset.setAccessibleName(f"Save {title.lower()} to the Camera Roll again")
            reset.clicked.connect(lambda _checked=False, kind=kind: self._set_folder(kind, ""))
            card = fluent.SettingsCard(title, "", glyph, change, body)
            card.add_control(reset)
            layout.addWidget(card)
            self.folder_cards[kind], self.folder_buttons[kind], self.reset_buttons[kind] = card, change, reset
        layout.addWidget(fluent.SettingsCard(
            "Free space", f"A recording does not start with less than {storage.format_size(storage.MIN_FREE_VIDEO)} "
            f"free, and is ended and saved when less than {storage.format_size(storage.STOP_FREE_VIDEO)} is left.",
            "drive-harddisk", None, body))
        section("About")
        layout.addWidget(fluent.SettingsCard(f"Camera {__version__}", "Photos are saved as JPEG; videos with the "
                                             "sound of the default microphone when there is one.", "camera-photo",
                                             None, body))
        layout.addStretch(1)
        return page

    def _sync_settings_page(self) -> None:
        if self.settings_page is None:
            return
        settings = self.settings
        for box, value in ((self.mode_box, settings.default_mode), (self.timer_box, settings.timer),
                           (self.quality_box, settings.quality)):
            box.setCurrentIndex(max(0, box.findData(value)))
        for switch, value in ((self.mirror_switch, settings.mirror), (self.grid_switch, settings.grid),
                              (self.sound_switch, settings.sounds)):
            switch.setChecked(value)              # the handlers do nothing when the value is the stored one
        info = self.camera_info()
        camera = info.name if info is not None else ""
        for box, card, items, chosen in (
                (self.photo_size_box, self.photo_size_card,
                 [(f"{w} × {h}", [w, h]) for w, h in (info.photo_sizes if info else [])],
                 settings.photo_sizes.get(info.id) if info else None),
                (self.video_size_box, self.video_size_card,
                 [(item.label, [item.width, item.height, item.fps]) for item in (info.video_formats if info else [])],
                 settings.video_sizes.get(info.id) if info else None)):
            box.clear()
            box.addItem("Automatic", None)
            for text, data in items:
                box.addItem(text, data)
            index = box.findData(chosen) if chosen else 0
            box.setCurrentIndex(index if index >= 0 else 0)
            box.setEnabled(bool(items))
            if not info:
                card.set_description("No camera is running")
            elif not items:
                card.set_description(f"{camera} offers no choice")
            else:
                card.set_description(f"What {camera} offers")
        for kind in ("photo", "video"):
            custom = settings.photo_dir if kind == "photo" else settings.video_dir
            folder = settings.photo_folder() if kind == "photo" else settings.video_folder()
            self.folder_cards[kind].set_description(str(folder))
            self.reset_buttons[kind].setVisible(bool(custom))

    def _store(self, name: str, value: object) -> None:
        setattr(self.settings, name, value)
        self.save_settings()

    def _photo_size_chosen(self, value: object) -> None:
        info = self.camera_info()
        if info is None:
            return
        if value:
            self.settings.photo_sizes[info.id] = list(value)
        else:
            self.settings.photo_sizes.pop(info.id, None)
        self.backend.set_photo_size(tuple(value) if value else None)
        self.save_settings()

    def _video_size_chosen(self, value: object) -> None:
        info = self.camera_info()
        if info is None:
            return
        if value:
            self.settings.video_sizes[info.id] = list(value)
        else:
            self.settings.video_sizes.pop(info.id, None)
        self.backend.set_video_format(VideoFormat(*value) if value else None)
        self.save_settings()

    def pick_folder(self, title: str, start: str) -> str:
        return QFileDialog.getExistingDirectory(self, title, start)

    def choose_folder(self, kind: str) -> None:
        current = self.settings.photo_folder() if kind == "photo" else self.settings.video_folder()
        start = str(current if current.is_dir() else Path.home())
        folder = self.pick_folder("Save photos to" if kind == "photo" else "Save videos to", start)
        if folder:
            self._set_folder(kind, os.path.normpath(folder))

    def _set_folder(self, kind: str, folder: str) -> None:
        if kind == "photo":
            self.settings.photo_dir = folder
        else:
            self.settings.video_dir = folder
        self.save_settings()
        self._sync_settings_page()
        self._find_last_capture()

    # --- keys, closing ---
    def keyPressEvent(self, event) -> None:
        if not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            if event.key() == Qt.Key.Key_Escape:
                if self._count > 0:
                    self.cancel_countdown()
                elif self.showing_settings():
                    self.show_camera()
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self.showing_settings():
                self.shutter_pressed()
                event.accept()
                return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._closing = True
        self.cancel_countdown()
        if self.is_recording():
            # End the recording and give the recorder a moment to finish the file before the camera goes away.
            self.backend.stop_recording()
            deadline = time.monotonic() + CLOSE_WAIT_S
            while self.backend.recording_state() != "stopped" and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(0.005)
        self._space_timer.stop()
        self._start_timeout.stop()
        try:
            self.settings.save(self.settings_path)
        except OSError:
            pass
        self.worker.shutdown()
        self.backend.shutdown()
        super().closeEvent(event)

