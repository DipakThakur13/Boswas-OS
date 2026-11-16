"""The playback backend behind a small interface, so that the window never talks to Qt Multimedia directly.

``PlayerBackend`` is what the application needs from a player: load one
location, play, pause, seek, volume, rate, the tracks of the media, the
video frames and the output devices. ``QtPlayerBackend`` implements it
with QMediaPlayer, QAudioOutput and QVideoSink; the tests use a fake with
the same signals.

``FrameGrabber`` gets one still picture of a video for the library tiles;
``QtFrameGrabber`` does it with a second, silent QMediaPlayer.
"""

from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

from . import playlist_io

STATES = ("stopped", "playing", "paused")
STATUSES = ("none", "loading", "loaded", "buffering", "ended", "invalid")


def to_url(location: str) -> QUrl:
    """The QUrl of a location (an absolute path or an http(s) address)."""
    if playlist_io.is_url(location):
        return QUrl(location, QUrl.ParsingMode.StrictMode)
    return QUrl.fromLocalFile(location)


class PlayerBackend(QObject):
    """What Media Player needs from a player. Times are milliseconds, volume is 0..1."""

    state_changed = Signal(str)            # one of STATES
    status_changed = Signal(str)           # one of STATUSES; "ended": the media played to its end
    position_changed = Signal(int)
    duration_changed = Signal(int)
    seekable_changed = Signal(bool)
    error = Signal(str)                    # the current media cannot be played: a sentence for the user
    metadata_changed = Signal(dict)        # title, artist, album (str) and cover (QImage or None)
    tracks_changed = Signal()
    video_changed = Signal(bool)           # whether the media has pictures
    frame_changed = Signal(object)         # a QVideoFrame or QImage to show; None: nothing to show
    subtitle_changed = Signal(str)         # the subtitle line to show now ("" for none)
    devices_changed = Signal()

    def available(self) -> bool:
        """Whether the multimedia backend of this system can play anything at all."""
        raise NotImplementedError

    def unavailable_reason(self) -> str:
        return ""

    def set_source(self, location: str) -> None:
        raise NotImplementedError

    def play(self) -> None:
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def state(self) -> str:
        raise NotImplementedError

    def position(self) -> int:
        raise NotImplementedError

    def duration(self) -> int:
        raise NotImplementedError

    def seekable(self) -> bool:
        raise NotImplementedError

    def has_video(self) -> bool:
        raise NotImplementedError

    def seek(self, position_ms: int) -> None:
        raise NotImplementedError

    def set_volume(self, volume: float) -> None:
        raise NotImplementedError

    def set_muted(self, muted: bool) -> None:
        raise NotImplementedError

    def set_rate(self, rate: float) -> None:
        raise NotImplementedError

    # tracks: lists of labels; the active index is -1 for "none" (subtitles off)
    def audio_tracks(self) -> list[str]:
        return []

    def video_tracks(self) -> list[str]:
        return []

    def subtitle_tracks(self) -> list[str]:
        return []

    def active_track(self, kind: str) -> int:
        return -1

    def set_active_track(self, kind: str, index: int) -> None:
        raise NotImplementedError

    # output devices: (id, description) pairs; "" selects the system default
    def audio_devices(self) -> list[tuple[str, str]]:
        return []

    def set_audio_device(self, device_id: str) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:
        """Stop and let go of the device (called when the window closes)."""


class FrameGrabber(QObject):
    """Gets a still picture of a video file, one file at a time."""

    def grab(self, path: str, done: Callable[[object, int], None]) -> None:
        """Call ``done(QImage or None, duration_ms)`` on the GUI thread, some time later."""
        raise NotImplementedError

    def busy(self) -> bool:
        return False

    def shutdown(self) -> None:
        pass


# --- Qt Multimedia ---------------------------------------------------------------------------------------

def _track_label(metadata, number: int, kind: str) -> str:
    from PySide6.QtCore import QLocale
    from PySide6.QtMultimedia import QMediaMetaData
    parts = []
    language = metadata.value(QMediaMetaData.Key.Language)
    if isinstance(language, QLocale.Language) and language not in (QLocale.Language.AnyLanguage,
                                                                   QLocale.Language.C):
        parts.append(QLocale.languageToString(language))
    title = metadata.stringValue(QMediaMetaData.Key.Title)
    if title:
        parts.append(title[:80])
    return " · ".join(parts) if parts else f"{kind} {number}"


class QtPlayerBackend(PlayerBackend):
    """QMediaPlayer with a QAudioOutput and a QVideoSink."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer, QVideoSink
        self._mm = __import__("PySide6.QtMultimedia", fromlist=["QMediaPlayer"])
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._sink = QVideoSink(self)
        self._devices = QMediaDevices(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoSink(self._sink)
        self._device_id = ""
        self._failed = False
        player = self._player
        player.playbackStateChanged.connect(self._state_changed)
        player.mediaStatusChanged.connect(self._status_changed)
        player.positionChanged.connect(lambda value: self.position_changed.emit(int(value)))
        player.durationChanged.connect(lambda value: self.duration_changed.emit(int(value)))
        player.seekableChanged.connect(self.seekable_changed)
        player.errorOccurred.connect(self._error)
        player.metaDataChanged.connect(self._metadata)
        player.tracksChanged.connect(self.tracks_changed)
        player.hasVideoChanged.connect(self._video_changed)
        self._sink.videoFrameChanged.connect(self._frame)
        self._sink.subtitleTextChanged.connect(self.subtitle_changed)
        self._devices.audioOutputsChanged.connect(self._outputs_changed)

    # --- signals of the player ---
    def _state_changed(self, state) -> None:
        self.state_changed.emit(self.state())

    def _status_changed(self, status) -> None:
        names = self._mm.QMediaPlayer.MediaStatus
        text = {names.NoMedia: "none", names.LoadingMedia: "loading", names.LoadedMedia: "loaded",
                names.StalledMedia: "buffering", names.BufferingMedia: "buffering",
                names.BufferedMedia: "loaded", names.EndOfMedia: "ended", names.InvalidMedia: "invalid"}.get(status)
        if text == "invalid" and not self._failed:
            self._failed = True
            self.error.emit("The file is damaged or in a format that is not supported.")
        if text is not None:
            self.status_changed.emit(text)

    def _error(self, error, text: str) -> None:
        kinds = self._mm.QMediaPlayer.Error
        if error == kinds.NoError or self._failed:
            return
        self._failed = True
        message = {kinds.ResourceError: "The file could not be opened.",
                   kinds.FormatError: "The format is not supported.",
                   kinds.NetworkError: "The network stream could not be reached.",
                   kinds.AccessDeniedError: "You do not have permission to open it."}.get(error)
        detail = " ".join(str(text).split())[:200]
        if message is None:
            message = detail or "It could not be played."
        self.error.emit(message)

    def _metadata(self) -> None:
        keys = self._mm.QMediaMetaData.Key
        data = self._player.metaData()
        artist = (data.stringValue(keys.ContributingArtist) or data.stringValue(keys.AlbumArtist)
                  or data.stringValue(keys.Author))
        cover = None
        for key in (keys.CoverArtImage, keys.ThumbnailImage):
            image = data.value(key)
            if image is not None and hasattr(image, "isNull") and not image.isNull():
                cover = image
                break
        self.metadata_changed.emit({"title": data.stringValue(keys.Title)[:512], "artist": artist[:512],
                                    "album": data.stringValue(keys.AlbumTitle)[:512], "cover": cover})

    def _video_changed(self, has_video: bool) -> None:
        self.video_changed.emit(bool(has_video))
        if not has_video:
            self.frame_changed.emit(None)

    def _frame(self, frame) -> None:
        self.frame_changed.emit(frame if frame.isValid() else None)

    def _outputs_changed(self) -> None:
        self._apply_device()
        self.devices_changed.emit()

    # --- the interface ---
    def available(self) -> bool:
        return bool(self._player.isAvailable())

    def unavailable_reason(self) -> str:
        return "" if self.available() else "The multimedia components of the system are not installed."

    def set_source(self, location: str) -> None:
        self._failed = False
        self._player.setSource(to_url(location) if location else QUrl())

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def stop(self) -> None:
        self._player.stop()

    def state(self) -> str:
        states = self._mm.QMediaPlayer.PlaybackState
        return {states.PlayingState: "playing", states.PausedState: "paused"}.get(self._player.playbackState(),
                                                                                  "stopped")

    def position(self) -> int:
        return int(self._player.position())

    def duration(self) -> int:
        return max(0, int(self._player.duration()))

    def seekable(self) -> bool:
        return bool(self._player.isSeekable())

    def has_video(self) -> bool:
        return bool(self._player.hasVideo())

    def seek(self, position_ms: int) -> None:
        self._player.setPosition(max(0, int(position_ms)))

    def set_volume(self, volume: float) -> None:
        self._audio.setVolume(max(0.0, min(1.0, float(volume))))

    def set_muted(self, muted: bool) -> None:
        self._audio.setMuted(bool(muted))

    def set_rate(self, rate: float) -> None:
        self._player.setPlaybackRate(float(rate))

    def _tracks(self, kind: str) -> list:
        return {"audio": self._player.audioTracks, "video": self._player.videoTracks,
                "subtitle": self._player.subtitleTracks}[kind]()

    def audio_tracks(self) -> list[str]:
        return [_track_label(data, i + 1, "Audio track") for i, data in enumerate(self._tracks("audio"))]

    def video_tracks(self) -> list[str]:
        return [_track_label(data, i + 1, "Video track") for i, data in enumerate(self._tracks("video"))]

    def subtitle_tracks(self) -> list[str]:
        return [_track_label(data, i + 1, "Subtitles") for i, data in enumerate(self._tracks("subtitle"))]

    def active_track(self, kind: str) -> int:
        return int({"audio": self._player.activeAudioTrack, "video": self._player.activeVideoTrack,
                    "subtitle": self._player.activeSubtitleTrack}[kind]())

    def set_active_track(self, kind: str, index: int) -> None:
        count = len(self._tracks(kind))
        index = index if -1 <= index < count else -1
        {"audio": self._player.setActiveAudioTrack, "video": self._player.setActiveVideoTrack,
         "subtitle": self._player.setActiveSubtitleTrack}[kind](index)
        if kind == "subtitle" and index < 0:
            self.subtitle_changed.emit("")

    def audio_devices(self) -> list[tuple[str, str]]:
        return [(bytes(device.id().data()).decode("utf-8", "replace"), device.description())
                for device in self._mm.QMediaDevices.audioOutputs()]

    def set_audio_device(self, device_id: str) -> None:
        self._device_id = device_id
        self._apply_device()

    def _apply_device(self) -> None:
        devices = self._mm.QMediaDevices
        chosen = devices.defaultAudioOutput()
        if self._device_id:
            for device in devices.audioOutputs():
                if bytes(device.id().data()).decode("utf-8", "replace") == self._device_id:
                    chosen = device
                    break
        self._audio.setDevice(chosen)

    def shutdown(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())


class QtFrameGrabber(FrameGrabber):
    """Loads a video in a silent QMediaPlayer, moves a little into it and keeps the first picture."""

    TIMEOUT_MS = 8000

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
        self._statuses = QMediaPlayer.MediaStatus
        self._player = QMediaPlayer(self)
        self._sink = QVideoSink(self)
        self._player.setVideoSink(self._sink)
        self._player.mediaStatusChanged.connect(self._status)
        self._player.errorOccurred.connect(lambda *_args: self._finish(None))
        self._sink.videoFrameChanged.connect(self._frame)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._finish(None))
        self._queue: list[tuple[str, Callable[[object, int], None]]] = []
        self._done: Callable[[object, int], None] | None = None
        self._seeking = False
        self._closed = False

    def busy(self) -> bool:
        return self._done is not None or bool(self._queue)

    def grab(self, path: str, done: Callable[[object, int], None]) -> None:
        if self._closed:
            return
        self._queue.append((path, done))
        if self._done is None:
            self._next()

    def _next(self) -> None:
        if not self._queue or self._closed:
            return
        path, self._done = self._queue.pop(0)
        self._seeking = False
        self._timer.start(self.TIMEOUT_MS)
        self._player.setSource(QUrl.fromLocalFile(path))

    def _status(self, status) -> None:
        if self._done is None:
            return
        if status == self._statuses.LoadedMedia and not self._seeking:
            self._seeking = True
            duration = max(0, int(self._player.duration()))
            if not self._player.hasVideo():
                self._finish(None)
                return
            self._player.setPosition(min(duration // 10, 10_000))
            self._player.play()
        elif status in (self._statuses.InvalidMedia, self._statuses.EndOfMedia):
            self._finish(None)

    def _frame(self, frame) -> None:
        if self._done is None or not self._seeking or not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            self._finish(image)

    def _finish(self, image) -> None:
        done, self._done = self._done, None
        if done is None:
            return
        self._timer.stop()
        duration = max(0, int(self._player.duration()))
        self._player.stop()
        self._player.setSource(QUrl())
        try:
            done(image, duration)
        finally:
            QTimer.singleShot(0, self._next)

    def shutdown(self) -> None:
        self._closed = True
        self._queue.clear()
        self._done = None
        self._timer.stop()
        self._player.stop()
        self._player.setSource(QUrl())


def describe_backend() -> dict:
    """What the multimedia backend of this machine says it decodes (for ``--backend-info`` and the settings page)."""
    from PySide6.QtMultimedia import QMediaFormat
    formats = QMediaFormat()
    mode = QMediaFormat.ConversionMode.Decode
    return {
        "backend": os.environ.get("QT_MEDIA_BACKEND") or "default",
        "containers": [QMediaFormat.fileFormatName(item) for item in formats.supportedFileFormats(mode)],
        "audio": [QMediaFormat.audioCodecName(item) for item in formats.supportedAudioCodecs(mode)],
        "video": [QMediaFormat.videoCodecName(item) for item in formats.supportedVideoCodecs(mode)],
    }
