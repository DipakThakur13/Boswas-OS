"""The camera behind a small interface, so that the window never talks to Qt Multimedia directly.

``CameraBackend`` is what the application needs: the cameras that are
connected, a live picture, a photo into a file, a recording into a file,
and the optional controls a camera may offer. ``QtCameraBackend``
implements it with QMediaDevices, QCamera, QMediaCaptureSession,
QImageCapture, QMediaRecorder and QAudioInput; the tests use a fake with
the same signals.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QSize, QUrl, Signal

RECORDING_STATES = ("stopped", "recording", "paused")
SHUTTER_SOUNDS = ("/usr/share/sounds/freedesktop/stereo/camera-shutter.oga",
                  "/usr/share/sounds/freedesktop/stereo/screen-capture.oga")


@dataclass(frozen=True)
class VideoFormat:
    width: int
    height: int
    fps: int

    @property
    def label(self) -> str:
        return f"{self.width} × {self.height}, {self.fps} fps"


@dataclass
class CameraInfo:
    id: str
    name: str
    photo_sizes: list[tuple[int, int]] = field(default_factory=list)     # largest first
    video_formats: list[VideoFormat] = field(default_factory=list)       # largest first


@dataclass
class Features:
    """The optional controls of the camera that is running; a control exists only when its field says so."""

    zoom: tuple[float, float] | None = None       # (minimum, maximum) when the camera can zoom
    flash_modes: tuple[str, ...] = ()             # of "off", "on", "auto"; shown when there is more than "off"
    torch: bool = False
    exposure: bool = False                        # exposure compensation, -2 to +2 EV
    focus_distance: bool = False                  # manual focus, 0 (near) to 1 (far)


class CameraBackend(QObject):
    """What Camera needs from a camera."""

    cameras_changed = Signal()
    active_changed = Signal(bool)
    frame_changed = Signal(object)        # a QVideoFrame or QImage of the live picture; None: none
    error = Signal(str)                   # the camera could not start or failed: the system's message
    features_changed = Signal()
    photo_saved = Signal(str)             # path
    photo_failed = Signal(str)            # message
    recording_changed = Signal(str)       # one of RECORDING_STATES
    recording_time = Signal(int)          # milliseconds recorded so far
    recording_saved = Signal(str)         # path, when the file is complete
    recording_failed = Signal(str)        # message

    def cameras(self) -> list[CameraInfo]:
        raise NotImplementedError

    def start(self, camera_id: str) -> None:
        """Start the live picture of a camera (stops the one that was running)."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def current(self) -> str:
        """The identifier of the camera that was started, or ""."""
        raise NotImplementedError

    def is_active(self) -> bool:
        raise NotImplementedError

    def features(self) -> Features:
        return Features()

    def set_photo_size(self, size: tuple[int, int] | None) -> None:
        raise NotImplementedError

    def set_video_format(self, video_format: VideoFormat | None) -> None:
        raise NotImplementedError

    def capture_photo(self, path: str) -> None:
        """Save a JPEG photo to ``path``; ``photo_saved`` or ``photo_failed`` follows."""
        raise NotImplementedError

    def video_extension(self) -> str:
        """The file extension of the recordings this system makes (``.mp4`` where it can)."""
        return ".mp4"

    def start_recording(self, path: str, quality: str, with_audio: bool) -> None:
        raise NotImplementedError

    def pause_recording(self) -> None:
        raise NotImplementedError

    def resume_recording(self) -> None:
        raise NotImplementedError

    def stop_recording(self) -> None:
        """End the recording; ``recording_saved`` follows when the file is complete."""
        raise NotImplementedError

    def recording_state(self) -> str:
        raise NotImplementedError

    def can_pause(self) -> bool:
        return False

    def has_microphone(self) -> bool:
        return False

    def set_microphone_muted(self, muted: bool) -> None:
        raise NotImplementedError

    # optional controls: called only for what ``features`` offers
    def set_zoom(self, factor: float) -> None:
        raise NotImplementedError

    def set_flash(self, mode: str) -> None:
        raise NotImplementedError

    def set_torch(self, on: bool) -> None:
        raise NotImplementedError

    def set_exposure(self, ev: float) -> None:
        raise NotImplementedError

    def set_focus_distance(self, distance: float | None) -> None:
        """A distance from 0 (near) to 1 (far); None goes back to automatic focus."""
        raise NotImplementedError

    def play_shutter_sound(self) -> None:
        """Play the shutter sound, if the system has one and a device to play it on."""

    def shutdown(self) -> None:
        pass


def _device_id(device) -> str:
    return bytes(device.id().data()).decode("utf-8", "replace")


class QtCameraBackend(CameraBackend):
    """Qt Multimedia: one capture session with a camera, a video sink, an image capture and a recorder."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        import PySide6.QtMultimedia as mm
        self._mm = mm
        self._devices = mm.QMediaDevices(self)
        self._session = mm.QMediaCaptureSession(self)
        self._sink = mm.QVideoSink(self)
        self._capture = mm.QImageCapture(self)
        self._recorder = mm.QMediaRecorder(self)
        self._session.setVideoSink(self._sink)
        self._session.setImageCapture(self._capture)
        self._session.setRecorder(self._recorder)
        self._camera = None
        self._audio_input = None
        self._camera_id = ""
        self._muted = False
        self._photo_path = ""
        self._record_path = ""
        self._photo_size: tuple[int, int] | None = None
        self._video_format: VideoFormat | None = None
        self._sound = None
        self._extension, self._media_format = self._choose_format()
        self._devices.videoInputsChanged.connect(self.cameras_changed)
        self._sink.videoFrameChanged.connect(lambda frame: self.frame_changed.emit(frame if frame.isValid() else None))
        self._capture.imageSaved.connect(self._image_saved)
        self._capture.errorOccurred.connect(self._image_error)
        self._recorder.recorderStateChanged.connect(self._recorder_state)
        self._recorder.durationChanged.connect(lambda value: self.recording_time.emit(int(value)))
        self._recorder.errorOccurred.connect(self._recorder_error)

    # --- cameras ---
    def cameras(self) -> list[CameraInfo]:
        found = []
        for device in self._mm.QMediaDevices.videoInputs():
            sizes = sorted({(size.width(), size.height()) for size in device.photoResolutions()
                            if size.width() > 0 and size.height() > 0}, key=lambda s: -s[0] * s[1])
            formats = sorted({VideoFormat(item.resolution().width(), item.resolution().height(),
                                          max(1, round(item.maxFrameRate())))
                              for item in device.videoFormats() if item.resolution().width() > 0},
                             key=lambda f: (-f.width * f.height, -f.fps))
            found.append(CameraInfo(_device_id(device), device.description() or "Camera", sizes[:64], formats[:64]))
        return found

    def _device(self, camera_id: str):
        for device in self._mm.QMediaDevices.videoInputs():
            if _device_id(device) == camera_id:
                return device
        return None

    def start(self, camera_id: str) -> None:
        self.stop()
        device = self._device(camera_id)
        if device is None:
            self.error.emit("The camera is not connected.")
            return
        camera = self._mm.QCamera(device, self)
        self._camera, self._camera_id = camera, camera_id
        camera.errorOccurred.connect(self._camera_error)
        camera.activeChanged.connect(self._active_changed)
        self._session.setCamera(camera)
        self._apply_format()
        camera.start()
        self.features_changed.emit()

    def stop(self) -> None:
        if self._recorder.recorderState() != self._mm.QMediaRecorder.RecorderState.StoppedState:
            self._recorder.stop()
        camera, self._camera = self._camera, None
        self._camera_id = ""
        if camera is not None:
            camera.stop()
            self._session.setCamera(None)
            camera.deleteLater()
            self.frame_changed.emit(None)

    def current(self) -> str:
        return self._camera_id

    def is_active(self) -> bool:
        return self._camera is not None and bool(self._camera.isActive())

    def _active_changed(self, active: bool) -> None:
        self.active_changed.emit(bool(active))
        if active:
            self.features_changed.emit()

    def _camera_error(self, error, text: str) -> None:
        if error != self._mm.QCamera.Error.NoError:
            self.error.emit(" ".join(str(text).split())[:300] or "The camera reported an error.")

    def features(self) -> Features:
        camera = self._camera
        if camera is None:
            return Features()
        mm = self._mm
        flags = camera.supportedFeatures()
        flash = ["off"]
        if camera.isFlashModeSupported(mm.QCamera.FlashMode.FlashOn):
            flash.append("on")
        if camera.isFlashModeSupported(mm.QCamera.FlashMode.FlashAuto):
            flash.append("auto")
        low, high = float(camera.minimumZoomFactor()), float(camera.maximumZoomFactor())
        return Features(
            zoom=(low, high) if high > low + 0.01 else None,
            flash_modes=tuple(flash) if len(flash) > 1 else (),
            torch=bool(camera.isTorchModeSupported(mm.QCamera.TorchMode.TorchOn)),
            exposure=bool(flags & mm.QCamera.Feature.ExposureCompensation),
            focus_distance=bool(flags & mm.QCamera.Feature.FocusDistance))

    # --- formats ---
    def set_photo_size(self, size: tuple[int, int] | None) -> None:
        self._photo_size = size
        self._capture.setResolution(QSize(*size) if size else QSize())

    def set_video_format(self, video_format: VideoFormat | None) -> None:
        self._video_format = video_format
        self._apply_format()

    def _apply_format(self) -> None:
        camera, wanted = self._camera, self._video_format
        if camera is None or wanted is None:
            return
        for item in camera.cameraDevice().videoFormats():
            size = item.resolution()
            if (size.width(), size.height()) == (wanted.width, wanted.height) and round(item.maxFrameRate()) == wanted.fps:
                camera.setCameraFormat(item)
                return

    # --- photo ---
    def capture_photo(self, path: str) -> None:
        if self._camera is None or not self._capture.isReadyForCapture():
            self.photo_failed.emit("The camera is not ready.")
            return
        self._capture.setFileFormat(self._mm.QImageCapture.FileFormat.JPEG)
        self._capture.setQuality(self._mm.QImageCapture.Quality.VeryHighQuality)
        self._photo_path = path
        self._capture.captureToFile(path)

    def _image_saved(self, _identifier: int, file_name: str) -> None:
        self.photo_saved.emit(file_name or self._photo_path)

    def _image_error(self, _identifier: int, _error, text: str) -> None:
        self.photo_failed.emit(" ".join(str(text).split())[:300] or "The photo could not be saved.")

    # --- video ---
    def _choose_format(self):
        """The container and codecs for recordings: MPEG-4 with H.264 and AAC where the system encodes them."""
        formats = self._mm.QMediaFormat
        probe = formats()
        encode = formats.ConversionMode.Encode
        containers = probe.supportedFileFormats(encode)
        for container, extension in ((formats.FileFormat.MPEG4, ".mp4"), (formats.FileFormat.Matroska, ".mkv"),
                                     (formats.FileFormat.WebM, ".webm"), (formats.FileFormat.QuickTime, ".mov"),
                                     (formats.FileFormat.AVI, ".avi")):
            if container in containers:
                chosen = formats(container)
                video = formats(container).supportedVideoCodecs(encode)
                audio = formats(container).supportedAudioCodecs(encode)
                if formats.VideoCodec.H264 in video:
                    chosen.setVideoCodec(formats.VideoCodec.H264)
                if formats.AudioCodec.AAC in audio:
                    chosen.setAudioCodec(formats.AudioCodec.AAC)
                return extension, chosen
        return ".mp4", None

    def video_extension(self) -> str:
        return self._extension

    def has_microphone(self) -> bool:
        return not self._mm.QMediaDevices.defaultAudioInput().isNull()

    def set_microphone_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        if self._audio_input is not None:
            self._audio_input.setMuted(self._muted)

    def start_recording(self, path: str, quality: str, with_audio: bool) -> None:
        mm = self._mm
        if self._camera is None:
            self.recording_failed.emit("The camera is not ready.")
            return
        if with_audio and self.has_microphone():
            self._audio_input = mm.QAudioInput(mm.QMediaDevices.defaultAudioInput(), self)
            self._audio_input.setMuted(self._muted)
            self._session.setAudioInput(self._audio_input)
        else:
            self._session.setAudioInput(None)
            self._audio_input = None
        levels = mm.QMediaRecorder.Quality
        self._recorder.setQuality({"low": levels.LowQuality, "normal": levels.NormalQuality,
                                   "high": levels.HighQuality, "very-high": levels.VeryHighQuality}.get(
                                       quality, levels.HighQuality))
        if self._media_format is not None:
            self._recorder.setMediaFormat(self._media_format)
        self._record_path = path
        self._recorder.setOutputLocation(QUrl.fromLocalFile(path))
        self._recorder.record()

    def pause_recording(self) -> None:
        self._recorder.pause()

    def resume_recording(self) -> None:
        self._recorder.record()

    def stop_recording(self) -> None:
        self._recorder.stop()

    def can_pause(self) -> bool:
        return True

    def recording_state(self) -> str:
        states = self._mm.QMediaRecorder.RecorderState
        return {states.RecordingState: "recording", states.PausedState: "paused"}.get(
            self._recorder.recorderState(), "stopped")

    def _recorder_state(self, _state) -> None:
        state = self.recording_state()
        self.recording_changed.emit(state)
        if state == "stopped" and self._record_path:
            path, self._record_path = self._record_path, ""
            actual = self._recorder.actualLocation().toLocalFile() or path
            if os.path.isfile(actual) and os.path.getsize(actual) > 0:
                self.recording_saved.emit(actual)
            self._session.setAudioInput(None)
            self._audio_input = None

    def _recorder_error(self, error, text: str) -> None:
        if error != self._mm.QMediaRecorder.Error.NoError:
            self.recording_failed.emit(" ".join(str(text).split())[:300] or "The recording failed.")

    # --- optional controls ---
    def set_zoom(self, factor: float) -> None:
        if self._camera is not None:
            self._camera.setZoomFactor(float(factor))

    def set_flash(self, mode: str) -> None:
        if self._camera is not None:
            modes = self._mm.QCamera.FlashMode
            self._camera.setFlashMode({"on": modes.FlashOn, "auto": modes.FlashAuto}.get(mode, modes.FlashOff))

    def set_torch(self, on: bool) -> None:
        if self._camera is not None:
            modes = self._mm.QCamera.TorchMode
            self._camera.setTorchMode(modes.TorchOn if on else modes.TorchOff)

    def set_exposure(self, ev: float) -> None:
        if self._camera is not None:
            self._camera.setExposureCompensation(max(-2.0, min(2.0, float(ev))))

    def set_focus_distance(self, distance: float | None) -> None:
        if self._camera is None:
            return
        modes = self._mm.QCamera.FocusMode
        if distance is None:
            self._camera.setFocusMode(modes.FocusModeAuto)
        else:
            self._camera.setFocusMode(modes.FocusModeManual)
            self._camera.setFocusDistance(max(0.0, min(1.0, float(distance))))

    def play_shutter_sound(self) -> None:
        mm = self._mm
        path = next((candidate for candidate in SHUTTER_SOUNDS if os.path.isfile(candidate)), None)
        if path is None or mm.QMediaDevices.defaultAudioOutput().isNull():
            return
        if self._sound is None:
            self._sound = mm.QMediaPlayer(self)
            self._sound_output = mm.QAudioOutput(self)
            self._sound.setAudioOutput(self._sound_output)
        self._sound.setSource(QUrl.fromLocalFile(path))
        self._sound.play()

    def shutdown(self) -> None:
        self.stop()
