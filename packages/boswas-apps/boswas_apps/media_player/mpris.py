"""MPRIS: Media Player on the session bus as ``org.mpris.MediaPlayer2.boswas-media-player`` (QtDBus).

The desktop's media keys, the quick settings card and the lock screen find
a player by this name, read ``org.mpris.MediaPlayer2`` and
``org.mpris.MediaPlayer2.Player`` at ``/org/mpris/MediaPlayer2`` and call
its methods. Two adaptors carry the two interfaces; QtDBus answers
``org.freedesktop.DBus.Properties`` and introspection from their declared
properties, so ``Position``, ``Seek``, ``SetPosition`` and ``Seeked`` have
the 64-bit type the specification asks for.

Everything that arrives over the bus is untrusted: positions, volume and
rate are clamped, ``LoopStatus`` must be one of the three words,
``OpenUri`` takes ``file``, ``http`` and ``https`` only, and a track
identifier must be the current one.

Known limit of PySide's QtDBus: the type of a value *inside* the
``Metadata`` dictionary cannot be chosen from Python. ``mpris:length`` is
therefore sent as a 32-bit integer when the length fits into one (below
about 35 minutes 47 seconds) and as the specified 64-bit integer above.
"""

from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import ClassInfo, Property, QMetaType, QObject, QUrl, Signal, Slot
from PySide6.QtDBus import QDBusAbstractAdaptor, QDBusArgument, QDBusConnection, QDBusMessage, QDBusObjectPath

from . import model, opening
from .controller import PlayerController

SERVICE = "org.mpris.MediaPlayer2.boswas-media-player"
PATH = "/org/mpris/MediaPlayer2"
ROOT_INTERFACE = "org.mpris.MediaPlayer2"
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
TRACK_PREFIX = "/com/boswas/MediaPlayer/track/"
DESKTOP_ENTRY = "com.boswas.MediaPlayer"
URI_SCHEMES = ["file", "http", "https"]
MIME_TYPES = [
    "audio/mpeg", "audio/flac", "audio/x-flac", "audio/ogg", "audio/x-vorbis+ogg", "audio/x-opus+ogg", "audio/opus",
    "audio/wav", "audio/x-wav", "audio/mp4", "audio/x-m4a", "audio/aac", "audio/x-ms-wma", "audio/x-mpegurl",
    "audio/mpegurl", "audio/x-scpls", "video/mp4", "video/x-matroska", "video/webm", "video/quicktime",
    "video/x-msvideo", "video/mpeg", "video/ogg", "video/x-ms-wmv", "video/3gpp", "video/x-flv", "video/mp2t",
]
_LOOP = {"off": "None", "one": "Track", "all": "Playlist"}
_STATUS = {"playing": "Playing", "paused": "Paused", "stopped": "Stopped"}
_MAX_MICROSECONDS = 30 * 24 * 3600 * 1_000_000


@ClassInfo({"D-Bus Interface": ROOT_INTERFACE})
class RootAdaptor(QDBusAbstractAdaptor):
    def __init__(self, service: "MprisService") -> None:
        super().__init__(service.holder)
        self._service = service

    @Slot()
    def Raise(self) -> None:
        self._service.raise_window()

    @Slot()
    def Quit(self) -> None:
        self._service.quit_application()

    CanQuit = Property(bool, lambda self: True)
    CanRaise = Property(bool, lambda self: True)
    HasTrackList = Property(bool, lambda self: False)
    Identity = Property(str, lambda self: "Media Player")
    DesktopEntry = Property(str, lambda self: DESKTOP_ENTRY)
    SupportedUriSchemes = Property("QStringList", lambda self: list(URI_SCHEMES))
    SupportedMimeTypes = Property("QStringList", lambda self: list(MIME_TYPES))


@ClassInfo({"D-Bus Interface": PLAYER_INTERFACE})
class PlayerAdaptor(QDBusAbstractAdaptor):
    Seeked = Signal("qlonglong")

    def __init__(self, service: "MprisService") -> None:
        super().__init__(service.holder)
        self._service = service
        self._controller = service.controller

    # --- methods ---
    @Slot()
    def Next(self) -> None:
        self._controller.next()

    @Slot()
    def Previous(self) -> None:
        self._controller.previous()

    @Slot()
    def Pause(self) -> None:
        self._controller.pause()

    @Slot()
    def PlayPause(self) -> None:
        self._controller.toggle()

    @Slot()
    def Stop(self) -> None:
        self._controller.stop()

    @Slot()
    def Play(self) -> None:
        self._controller.play()

    @Slot("qlonglong")
    def Seek(self, offset: int) -> None:
        if not isinstance(offset, int) or abs(offset) > _MAX_MICROSECONDS or self._controller.current() is None:
            return
        target = self._controller.position() + offset // 1000
        duration = self._controller.duration()
        if duration > 0 and target >= duration:
            self._controller.next()
        else:
            self._controller.seek(max(0, target))

    @Slot(QDBusObjectPath, "qlonglong")
    def SetPosition(self, track: QDBusObjectPath, position: int) -> None:
        if track.path() != self._service.track_id() or self._controller.current() is None:
            return
        duration = self._controller.duration()
        if not isinstance(position, int) or position < 0 or (duration > 0 and position // 1000 > duration):
            return
        self._controller.seek(position // 1000)

    @Slot(str)
    def OpenUri(self, uri: str) -> None:
        if not isinstance(uri, str) or len(uri) > opening.MAX_ARGUMENT or uri.split(":", 1)[0].lower() not in URI_SCHEMES:
            return
        location = opening.to_location(uri)
        if location is not None:
            self._controller.open_locations([location], "play")

    # --- properties ---
    def _playback_status(self) -> str:
        return _STATUS.get(self._controller.state(), "Stopped")

    def _loop_status(self) -> str:
        return _LOOP[self._controller.queue.repeat]

    def _set_loop_status(self, value: str) -> None:
        for mode, name in _LOOP.items():
            if name == value:
                self._controller.set_repeat(mode)

    def _rate(self) -> float:
        return float(self._controller.settings.rate)

    def _set_rate(self, value: float) -> None:
        if isinstance(value, (int, float)) and value == value and model.RATES[0] <= value <= model.RATES[-1]:
            self._controller.set_rate(float(value))

    def _shuffle(self) -> bool:
        return bool(self._controller.queue.shuffle)

    def _set_shuffle(self, value: bool) -> None:
        self._controller.set_shuffle(bool(value))

    def _volume(self) -> float:
        return 0.0 if self._controller.settings.muted else float(self._controller.settings.volume)

    def _set_volume(self, value: float) -> None:
        if isinstance(value, (int, float)) and value == value:
            self._controller.set_volume(max(0.0, min(1.0, float(value))))

    def _position(self) -> int:
        return max(0, self._controller.position()) * 1000 if self._controller.current() is not None else 0

    PlaybackStatus = Property(str, _playback_status)
    LoopStatus = Property(str, _loop_status, _set_loop_status)
    Rate = Property("double", _rate, _set_rate)
    Shuffle = Property(bool, _shuffle, _set_shuffle)
    Metadata = Property("QVariantMap", lambda self: self._service.metadata())
    Volume = Property("double", _volume, _set_volume)
    Position = Property("qlonglong", _position)
    MinimumRate = Property("double", lambda self: float(model.RATES[0]))
    MaximumRate = Property("double", lambda self: float(model.RATES[-1]))
    CanGoNext = Property(bool, lambda self: self._service.can("next"))
    CanGoPrevious = Property(bool, lambda self: self._service.can("previous"))
    CanPlay = Property(bool, lambda self: self._service.can("play"))
    CanPause = Property(bool, lambda self: self._service.can("pause"))
    CanSeek = Property(bool, lambda self: self._service.can("seek"))
    CanControl = Property(bool, lambda self: True)


class MprisService(QObject):
    """Exports a PlayerController on a D-Bus connection and keeps the bus told about its changes."""

    def __init__(self, controller: PlayerController, parent: QObject | None = None, *,
                 connection: QDBusConnection | None = None, service: str = SERVICE,
                 raise_window: Callable[[], None] | None = None,
                 quit_application: Callable[[], None] | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.service = service
        self._connection = connection
        self._raise = raise_window
        self._quit = quit_application
        self._registered = False
        self._last: dict[str, object] = {}
        self.holder = QObject(self)
        self.root = RootAdaptor(self)
        self.player = PlayerAdaptor(self)
        controller.state_changed.connect(lambda _state: self.notify())
        controller.track_changed.connect(lambda _track: self.notify())
        controller.track_updated.connect(lambda _track: self.notify())
        controller.art_changed.connect(self.notify)
        controller.options_changed.connect(self.notify)
        controller.queue_changed.connect(self.notify)
        controller.seeked.connect(self._seeked)

    # --- lifetime ---
    def start(self) -> bool:
        """Register the object and the name; False when there is no session bus or the name is taken."""
        if self._registered:
            return True
        if self._connection is None:
            if not (os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("XDG_RUNTIME_DIR")):
                return False
            self._connection = QDBusConnection.sessionBus()
        bus = self._connection
        if not bus.isConnected():
            return False
        if not bus.registerObject(PATH, self.holder, QDBusConnection.RegisterOption.ExportAdaptors):
            return False
        if not bus.registerService(self.service):
            bus.unregisterObject(PATH)
            return False
        self._registered = True
        self._last = self._snapshot()
        return True

    def stop(self) -> None:
        if self._registered and self._connection is not None:
            self._connection.unregisterService(self.service)
            self._connection.unregisterObject(PATH)
        self._registered = False

    def is_registered(self) -> bool:
        return self._registered

    # --- what the adaptors ask ---
    def raise_window(self) -> None:
        if self._raise is not None:
            self._raise()

    def quit_application(self) -> None:
        if self._quit is not None:
            self._quit()

    def track_id(self) -> str:
        if self.controller.current() is None:
            return NO_TRACK
        return f"{TRACK_PREFIX}{self.controller.track_serial}"

    def can(self, what: str) -> bool:
        controller = self.controller
        has_track = controller.current() is not None
        if what == "next":
            return controller.queue.has_next()
        if what == "previous":
            return has_track
        if what == "play":
            return has_track or bool(controller.queue.items)
        if what == "pause":
            return has_track
        return has_track and controller.can_seek()

    def metadata(self) -> dict:
        track = self.controller.current()
        data: dict[str, object] = {"mpris:trackid": QDBusObjectPath(self.track_id())}
        if track is None:
            return data
        duration = self.controller.duration()
        if duration > 0:
            data["mpris:length"] = int(duration) * 1000
        data["xesam:title"] = track.display_title
        if track.artist:
            data["xesam:artist"] = [track.artist]
        if track.album:
            data["xesam:album"] = track.album
        if track.is_remote:
            data["xesam:url"] = track.location
        else:
            data["xesam:url"] = bytes(QUrl.fromLocalFile(track.location).toEncoded()).decode("ascii")
        if self.controller.art_path:
            data["mpris:artUrl"] = bytes(QUrl.fromLocalFile(self.controller.art_path).toEncoded()).decode("ascii")
        return data

    # --- telling the bus ---
    def _snapshot(self) -> dict[str, object]:
        player = self.player
        return {
            "PlaybackStatus": player._playback_status(), "LoopStatus": player._loop_status(),
            "Rate": player._rate(), "Shuffle": player._shuffle(), "Volume": player._volume(),
            "Metadata": self.metadata(), "CanGoNext": self.can("next"), "CanGoPrevious": self.can("previous"),
            "CanPlay": self.can("play"), "CanPause": self.can("pause"), "CanSeek": self.can("seek"),
        }

    @staticmethod
    def _comparable(value: object) -> object:
        if isinstance(value, dict):
            return {key: (item.path() if isinstance(item, QDBusObjectPath) else item) for key, item in value.items()}
        return value

    def notify(self) -> None:
        """Send ``PropertiesChanged`` for the properties whose value differs from what the bus was told last."""
        if not self._registered:
            return
        current = self._snapshot()
        changed = {name: value for name, value in current.items()
                   if self._comparable(value) != self._comparable(self._last.get(name))}
        self._last = current
        if not changed:
            return
        invalidated = QDBusArgument()             # an empty list of strings, with its type
        invalidated.beginArray(QMetaType(QMetaType.Type.QString))
        invalidated.endArray()
        signal = QDBusMessage.createSignal(PATH, PROPERTIES_INTERFACE, "PropertiesChanged")
        signal.setArguments([PLAYER_INTERFACE, changed, invalidated])
        self._connection.send(signal)

    def _seeked(self, position_ms: int) -> None:
        if self._registered:
            self.player.Seeked.emit(max(0, int(position_ms)) * 1000)
