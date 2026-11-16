"""PlayerController: the play queue driven through a PlayerBackend. No widgets.

The window, the mini player and the MPRIS service all talk to this object
and listen to its signals; none of them touches the backend. The
controller decides what happens when a track ends, when one cannot be
played (it is skipped, with a message), and what is remembered between
runs (queue, position, recently played, where each video was stopped).
"""

from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

import boswas_fluent as fluent

from . import model, opening
from .backend import PlayerBackend
from .model import PlayQueue, Recents, ResumePositions, Settings, Track

RESTART_THRESHOLD_MS = 3000           # "previous" restarts the track when it has played longer than this
SEEK_STEP_MS = 10_000
VOLUME_STEP = 0.05


class PlayerController(QObject):
    track_changed = Signal(object)        # another Track (or None) is the current one
    track_updated = Signal(object)        # the current Track has new details (title, length, cover)
    state_changed = Signal(str)           # stopped, playing, paused
    position_changed = Signal(int)
    duration_changed = Signal(int)
    queue_changed = Signal()
    options_changed = Signal()            # shuffle, repeat, volume, mute or rate
    video_changed = Signal(bool)
    tracks_changed = Signal()             # the audio, video or subtitle tracks of the media
    seeked = Signal(int)                  # the position jumped (not ordinary progress)
    art_changed = Signal()
    recents_changed = Signal()
    message = Signal(str, str)            # severity, text

    def __init__(self, backend: PlayerBackend, settings: Settings, state_dir: Path, settings_path: Path,
                 parent: QObject | None = None, rng: random.Random | None = None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.settings = settings
        self.settings_path = Path(settings_path)
        self.state_dir = Path(state_dir)
        self.queue = PlayQueue(rng)
        self.recents = Recents(self.state_dir / "recent.json")
        self.resume = ResumePositions(self.state_dir / "resume.json")
        self.worker = fluent.Worker(self)
        self.art_path = ""                # a picture file of the current track's cover (for MPRIS)
        self.backend_cover = None         # a QImage the backend found in the media, or None
        self.track_serial = 0             # counts up with every track that is loaded
        self._pending_seek = 0
        self._failures = 0
        self._counted = False
        self._play_intent = False
        self._duration = 0
        self._has_video = False
        self._loaded: Track | None = None      # the track whose location the backend holds

        backend.state_changed.connect(self._backend_state)
        backend.status_changed.connect(self._backend_status)
        backend.position_changed.connect(self.position_changed)
        backend.duration_changed.connect(self._backend_duration)
        backend.error.connect(self._backend_error)
        backend.metadata_changed.connect(self._backend_metadata)
        backend.video_changed.connect(self._backend_video)
        backend.tracks_changed.connect(self.tracks_changed)
        backend.set_volume(settings.volume)
        backend.set_muted(settings.muted)
        backend.set_rate(settings.rate)
        backend.set_audio_device(settings.audio_device)

    # --- what is playing ---
    def current(self) -> Track | None:
        return self.queue.current()

    def state(self) -> str:
        return self.backend.state()

    def position(self) -> int:
        return self.backend.position()

    def duration(self) -> int:
        track = self.current()
        return self._duration or (track.duration_ms if track is not None else 0)

    def has_video(self) -> bool:
        return self._has_video

    def play_intent(self) -> bool:
        """Whether the current track was asked to play (true before the backend reports "playing")."""
        return self._play_intent

    def can_seek(self) -> bool:
        return self.current() is not None and self.backend.seekable()

    # --- loading ---
    def _remember_position(self) -> None:
        """Note where the video that is loaded in the backend stands (the queue may already name another)."""
        track = self._loaded
        if track is not None and track.video and not track.is_remote:
            self.resume.remember(track.location, self.backend.position(), self._duration or track.duration_ms)

    def _load(self, index: int, *, play: bool, position: int = 0, resume: bool = True) -> None:
        self._remember_position()
        self._loaded = None
        self.queue.index = index
        track = self.current()
        if track is None:
            self._unload()
            return
        self.track_serial += 1
        self._counted = False
        self._duration = 0
        self._has_video = False
        self.backend_cover = None
        self.art_path = ""
        self._play_intent = play
        self._pending_seek = position
        if not position and resume and self.settings.resume_videos and track.video:
            self._pending_seek = self.resume.get(track.location)
        self._loaded = track
        self.backend.set_source(track.location)
        self.track_changed.emit(track)
        self.duration_changed.emit(track.duration_ms)
        self.position_changed.emit(0)
        if play:
            self.backend.play()

    def _unload(self) -> None:
        self._loaded = None
        self._play_intent = False
        self._duration = 0
        self._has_video = False
        self.backend_cover = None
        self.art_path = ""
        self.backend.stop()
        self.backend.set_source("")
        self.track_serial += 1
        self.track_changed.emit(None)
        self.duration_changed.emit(0)
        self.position_changed.emit(0)
        self.video_changed.emit(False)

    # --- backend signals ---
    def _backend_state(self, state: str) -> None:
        if state == "playing" and not self._counted and self.current() is not None:
            self._counted = True
            self.recents.add(self.current())
            self.recents_changed.emit()
        self.state_changed.emit(state)

    def _backend_status(self, status: str) -> None:
        if status == "loaded":
            self._failures = 0
            if self._pending_seek > 0:
                position, self._pending_seek = self._pending_seek, 0
                duration = self.backend.duration()
                if duration <= 0 or position < duration:
                    self.backend.seek(position)
                    self.seeked.emit(position)
        elif status == "ended":
            QTimer.singleShot(0, self._ended)

    def _ended(self) -> None:
        track = self.current()
        if track is not None:
            self.resume.remember(track.location, 0, 0)       # watched to the end: start over next time
        index = self.queue.next_index(auto=True)
        if index < 0:
            self._play_intent = False
            self.backend.stop()
            self.position_changed.emit(0)
            return
        if index == self.queue.index:                        # repeat one
            self.backend.seek(0)
            self.backend.play()
            self.seeked.emit(0)
            return
        self._load(index, play=True, resume=False)

    def _backend_error(self, text: str) -> None:
        track = self.current()
        if track is None:
            return
        failed_serial = self.track_serial
        self._failures += 1
        name = track.display_title
        more = self.queue.next_index() >= 0 and self._failures < len(self.queue)
        if more and self._play_intent:
            self.message.emit("warning", f"Couldn't play “{name}”. {text} Skipped to the next item.")
            QTimer.singleShot(0, lambda: self._skip_failed(failed_serial))
        else:
            self._play_intent = False
            self.backend.stop()
            self.message.emit("error", f"Couldn't play “{name}”. {text}")

    def _skip_failed(self, serial: int) -> None:
        if serial != self.track_serial:
            return                                           # the user chose something else meanwhile
        index = self.queue.next_index()
        if index >= 0:
            self._load(index, play=True)

    def _backend_duration(self, duration: int) -> None:
        self._duration = max(0, duration)
        track = self.current()
        if track is not None and duration > 0 and track.duration_ms != duration:
            track.duration_ms = duration
            self.track_updated.emit(track)
        self.duration_changed.emit(self.duration())

    def _backend_metadata(self, data: dict) -> None:
        track = self.current()
        if track is None:
            return
        changed = False
        for name in ("title", "artist", "album"):
            value = data.get(name)
            if isinstance(value, str) and value and not getattr(track, name):
                setattr(track, name, " ".join(value.split())[:512])
                changed = True
        cover = data.get("cover")
        if cover is not None and self.backend_cover is None:
            self.backend_cover = cover
            changed = True
        if changed:
            self.track_updated.emit(track)

    def _backend_video(self, has_video: bool) -> None:
        self._has_video = bool(has_video)
        track = self.current()
        if track is not None and has_video and not track.video:
            track.video = True
        self.video_changed.emit(self._has_video)

    # --- transport ---
    def play(self) -> None:
        if self.current() is None:
            if self.queue.items:
                self._load(0, play=True)
            return
        self._play_intent = True
        self.backend.play()

    def pause(self) -> None:
        self._play_intent = False
        self.backend.pause()

    def toggle(self) -> None:
        if self.backend.state() == "playing":
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self._remember_position()
        self._play_intent = False
        self.backend.stop()
        self.position_changed.emit(0)

    def next(self) -> None:
        index = self.queue.next_index()
        if index >= 0:
            self._load(index, play=True)

    def previous(self) -> None:
        index = self.queue.previous_index()
        if self.backend.position() > RESTART_THRESHOLD_MS or index < 0:
            self.seek(0)
        else:
            self._load(index, play=True)

    def play_index(self, index: int) -> None:
        if 0 <= index < len(self.queue):
            self._load(index, play=True)

    def seek(self, position_ms: int) -> None:
        if self.current() is None:
            return
        duration = self.duration()
        position = max(0, int(position_ms))
        if duration > 0:
            position = min(position, max(0, duration - 250))
        self.backend.seek(position)
        self.seeked.emit(position)

    def seek_relative(self, delta_ms: int) -> None:
        self.seek(self.backend.position() + delta_ms)

    # --- options ---
    def set_volume(self, volume: float) -> None:
        volume = max(0.0, min(1.0, float(volume)))
        self.settings.volume = volume
        self.backend.set_volume(volume)
        if volume > 0 and self.settings.muted:
            self.settings.muted = False
            self.backend.set_muted(False)
        self.options_changed.emit()

    def change_volume(self, delta: float) -> None:
        self.set_volume(round(self.settings.volume + delta, 2))

    def set_muted(self, muted: bool) -> None:
        self.settings.muted = bool(muted)
        self.backend.set_muted(self.settings.muted)
        self.options_changed.emit()

    def toggle_mute(self) -> None:
        self.set_muted(not self.settings.muted)

    def set_rate(self, rate: float) -> None:
        rate = max(model.RATES[0], min(model.RATES[-1], float(rate)))
        self.settings.rate = rate
        self.backend.set_rate(rate)
        self.options_changed.emit()

    def set_shuffle(self, shuffle: bool) -> None:
        self.queue.set_shuffle(shuffle)
        self.options_changed.emit()
        self.queue_changed.emit()

    def toggle_shuffle(self) -> None:
        self.set_shuffle(not self.queue.shuffle)

    def set_repeat(self, mode: str) -> None:
        self.queue.set_repeat(mode)
        self.options_changed.emit()
        self.queue_changed.emit()

    def cycle_repeat(self) -> None:
        self.queue.cycle_repeat()
        self.options_changed.emit()
        self.queue_changed.emit()

    def set_audio_device(self, device_id: str) -> None:
        self.settings.audio_device = device_id
        self.backend.set_audio_device(device_id)

    # --- the queue ---
    def play_tracks(self, tracks: list[Track], start: int = 0) -> None:
        """Replace the queue with ``tracks`` and play the one at ``start``."""
        if not tracks:
            return
        self._remember_position()
        self.queue.set_items([Track(**track.to_dict()) for track in tracks], start)
        self.queue_changed.emit()
        self._failures = 0
        self._load(self.queue.index, play=True)

    def enqueue(self, tracks: list[Track], *, quiet: bool = False) -> None:
        """Add ``tracks`` to the end of the queue; starts playing when nothing was loaded."""
        idle = self.current() is None
        first = self.queue.append([Track(**track.to_dict()) for track in tracks])
        if first < 0:
            if tracks:
                self.message.emit("warning", "The play queue is full.")
            return
        self.queue_changed.emit()
        if idle:
            self._load(first, play=True)
        elif not quiet:
            count = len(self.queue) - first
            self.message.emit("success", f"Added {count} item{'s' if count != 1 else ''} to the play queue.")

    def play_next(self, tracks: list[Track]) -> None:
        idle = self.current() is None
        first = self.queue.insert_next([Track(**track.to_dict()) for track in tracks])
        if first < 0:
            return
        self.queue_changed.emit()
        if idle:
            self._load(first, play=True)

    def remove(self, index: int) -> None:
        if not 0 <= index < len(self.queue):
            return
        was_current = index == self.queue.index
        playing = self._play_intent
        self.queue.remove(index)
        self.queue_changed.emit()
        if was_current:
            if self.queue.current() is None:
                self._unload()
            else:
                self._load(self.queue.index, play=playing)

    def move(self, source: int, target: int) -> None:
        self.queue.move(source, target)
        self.queue_changed.emit()

    def clear(self) -> None:
        self._remember_position()
        self.queue.clear()
        self.queue_changed.emit()
        self._unload()

    # --- opening ---
    def open_locations(self, locations: list[str], mode: str = "play") -> None:
        """Open files, folders, playlists and addresses: ``play`` now, ``enqueue`` or play ``next``.

        Folders and playlists are read on a worker thread; nothing happens
        on the GUI thread until the tracks are known.
        """
        locations = [location for location in locations if isinstance(location, str) and location]
        if not locations:
            return
        self.worker.submit(opening.expand, locations,
                           on_done=lambda opened: self._opened(opened, mode),
                           on_error=lambda error: self.message.emit("error", f"Couldn't open that: {error}"))

    def _opened(self, opened: opening.Opened, mode: str) -> None:
        if not opened.tracks:
            if opened.skipped:
                self.message.emit("warning", "Nothing to play was found there.")
            return
        if mode == "enqueue":
            self.enqueue(opened.tracks)
        elif mode == "next":
            self.play_next(opened.tracks)
        else:
            self.play_tracks(opened.tracks)
        if opened.truncated:
            self.message.emit("warning", f"Only the first {opening.MAX_OPEN} items were added.")

    # --- art ---
    def set_art_path(self, path: str) -> None:
        if path != self.art_path:
            self.art_path = path
            self.art_changed.emit()

    # --- between runs ---
    def save_state(self) -> list[str]:
        """Write queue, position, recents, resume positions and settings; returns what could not be written."""
        self._remember_position()
        failed = []
        data = self.queue.to_dict()
        data["position"] = self.backend.position() if self.current() is not None else 0
        for name, write in (("the play queue", lambda: model.write_json(self.state_dir / "queue.json", data)),
                            ("recently played", self.recents.save), ("video positions", self.resume.save),
                            ("the settings", lambda: self.settings.save(self.settings_path))):
            try:
                write()
            except OSError:
                failed.append(name)
        return failed

    def restore_state(self) -> None:
        """Load the queue of the last run; the current track is loaded paused where it was left."""
        data = model.read_json(self.state_dir / "queue.json")
        if not isinstance(data, dict):
            return
        self.queue.load_dict(data)
        self.options_changed.emit()
        self.queue_changed.emit()
        if self.queue.current() is not None:
            position = data.get("position")
            position = position if isinstance(position, int) and not isinstance(position, bool) and position > 0 else 0
            self._load(self.queue.index, play=False, position=position, resume=False)

    def shutdown(self) -> None:
        self.worker.shutdown()
        self.backend.shutdown()
