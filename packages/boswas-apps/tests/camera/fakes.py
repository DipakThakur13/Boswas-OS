"""A camera that needs no hardware, for the Camera tests.

``FakeCamera`` implements ``CameraBackend``: its "cameras" are a list the
test edits (and announces with ``plug`` and ``unplug``), its live picture is
generated, a photo is a real JPEG file, a recording is a small file that
grows with the time the test lets pass.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QImage, QPainter

from boswas_apps.camera.backend import CameraBackend, CameraInfo, Features, VideoFormat


def picture(width: int = 640, height: int = 480, hue: int = 200) -> QImage:
    """A generated picture whose left third is lighter, so that mirroring can be seen."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor.fromHsv(hue % 360, 150, 120))
    painter = QPainter(image)
    painter.fillRect(0, 0, width // 3, height, QColor.fromHsv((hue + 40) % 360, 90, 230))
    painter.end()
    return image


def webcam(identifier: str = "cam0", name: str = "Integrated Webcam") -> CameraInfo:
    return CameraInfo(identifier, name, [(1920, 1080), (1280, 720), (640, 480)],
                      [VideoFormat(1920, 1080, 30), VideoFormat(1280, 720, 60), VideoFormat(640, 480, 30)])


class FakeCamera(CameraBackend):
    def __init__(self, cameras: list[CameraInfo] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.devices: list[CameraInfo] = list(cameras or [])
        self.capabilities = Features()
        self.busy: dict[str, str] = {}            # camera id -> the error it answers a start with
        self.silent: set[str] = set()             # cameras that never answer a start
        self.microphone = True
        self.pausable = True
        self.photo_error = ""
        self.record_error = ""
        self.extension = ".mp4"
        self.calls: list[tuple] = []
        self.sounds = 0
        self.closed = False
        self._current = ""
        self._active = False
        self._state = "stopped"
        self._path = ""
        self._elapsed = 0
        self.photo_size: tuple[int, int] | None = None
        self.video_format: VideoFormat | None = None
        self.muted = False
        self.zoom = 1.0
        self.flash = "off"
        self.torch = False
        self.exposure = 0.0
        self.focus: float | None = None
        self.recordings: list[tuple[str, str, bool]] = []

    # --- cameras ---
    def cameras(self) -> list[CameraInfo]:
        return list(self.devices)

    def plug(self, camera: CameraInfo) -> None:
        self.devices.append(camera)
        self.cameras_changed.emit()

    def unplug(self, camera_id: str) -> None:
        self.devices = [camera for camera in self.devices if camera.id != camera_id]
        self.cameras_changed.emit()

    def start(self, camera_id: str) -> None:
        self.stop()
        self.calls.append(("start", camera_id))
        self._current = camera_id
        QTimer.singleShot(0, lambda: self._started(camera_id))

    def _started(self, camera_id: str) -> None:
        if camera_id != self._current or camera_id in self.silent:
            return
        if camera_id in self.busy:
            self.error.emit(self.busy[camera_id])
            return
        self._active = True
        self.active_changed.emit(True)
        self.features_changed.emit()
        self.frame_changed.emit(picture(hue=sum(camera_id.encode()) * 7))

    def stop(self) -> None:
        if self._state != "stopped":
            self.stop_recording()
        if self._current:
            self._current = ""
            self._active = False
            self.frame_changed.emit(None)

    def current(self) -> str:
        return self._current

    def is_active(self) -> bool:
        return self._active

    def features(self) -> Features:
        return self.capabilities if self._active else Features()

    def set_photo_size(self, size) -> None:
        self.photo_size = size

    def set_video_format(self, video_format) -> None:
        self.video_format = video_format

    # --- photo ---
    def capture_photo(self, path: str) -> None:
        self.calls.append(("photo", path))
        QTimer.singleShot(0, lambda: self._photo(path))

    def _photo(self, path: str) -> None:
        if self.photo_error:
            self.photo_failed.emit(self.photo_error)
            return
        width, height = self.photo_size or (1280, 720)
        picture(width // 8, height // 8).save(path, "JPEG", 85)
        self.photo_saved.emit(path)

    # --- video ---
    def video_extension(self) -> str:
        return self.extension

    def start_recording(self, path: str, quality: str, with_audio: bool) -> None:
        self.calls.append(("record", path, quality, with_audio))
        if self.record_error:
            QTimer.singleShot(0, lambda: self.recording_failed.emit(self.record_error))
            return
        self.recordings.append((path, quality, with_audio))
        self._path, self._elapsed = path, 0
        self._set_state("recording")

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.recording_changed.emit(state)

    def pause_recording(self) -> None:
        if self._state == "recording" and self.pausable:
            self._set_state("paused")

    def resume_recording(self) -> None:
        if self._state == "paused":
            self._set_state("recording")

    def stop_recording(self) -> None:
        if self._state == "stopped":
            return
        path, self._path = self._path, ""
        with open(path, "wb") as stream:
            stream.write(b"\0\0\0\x18ftypisom" + b"\0" * max(16, self._elapsed))
        self._set_state("stopped")
        self.recording_saved.emit(path)

    def recording_state(self) -> str:
        return self._state

    def can_pause(self) -> bool:
        return self.pausable

    def has_microphone(self) -> bool:
        return self.microphone

    def set_microphone_muted(self, muted: bool) -> None:
        self.muted = muted

    def record(self, milliseconds: int) -> None:
        """Let ``milliseconds`` of recording pass."""
        if self._state == "recording":
            self._elapsed += milliseconds
            self.recording_time.emit(self._elapsed)

    def fail_recording(self, text: str) -> None:
        self._path = ""
        self._set_state("stopped")
        self.recording_failed.emit(text)

    # --- optional controls ---
    def set_zoom(self, factor: float) -> None:
        self.zoom = factor

    def set_flash(self, mode: str) -> None:
        self.flash = mode

    def set_torch(self, on: bool) -> None:
        self.torch = on

    def set_exposure(self, ev: float) -> None:
        self.exposure = ev

    def set_focus_distance(self, distance) -> None:
        self.focus = distance

    def play_shutter_sound(self) -> None:
        self.sounds += 1

    def shutdown(self) -> None:
        self.stop()
        self.closed = True
