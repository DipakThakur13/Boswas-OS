"""LibraryManager: the library index of the running application and its background scans."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

import boswas_fluent as fluent

from . import library
from .library import Song
from .model import Settings


class LibraryManager(QObject):
    """Keeps the index in memory, scans the library folders on a worker thread and stores the result.

    ``changed`` is emitted when the songs or videos differ from before;
    ``scanning_changed(bool)`` when a scan starts or ends; ``progress(n)``
    during a scan, with the number of files seen so far.
    """

    changed = Signal()
    scanning_changed = Signal(bool)
    progress = Signal(int)
    finished = Signal(object)             # library.ScanResult
    failed = Signal(str)

    def __init__(self, settings: Settings, state_dir: Path, worker: fluent.Worker, parent: QObject | None = None):
        super().__init__(parent)
        self.settings = settings
        self.index_path = library.default_index_path(Path(state_dir))
        self.worker = worker
        self.index: dict[str, Song] = {}
        self.loaded = False
        self._cancel = threading.Event()
        self._scanning = False
        self._again = False
        self._songs: list[Song] | None = None
        self._videos: list[Song] | None = None

    # --- what the pages read ---
    def songs(self) -> list[Song]:
        if self._songs is None:
            self._songs = library.songs_of(self.index, video=False)
        return self._songs

    def videos(self) -> list[Song]:
        if self._videos is None:
            self._videos = library.songs_of(self.index, video=True)
        return self._videos

    def is_scanning(self) -> bool:
        return self._scanning

    def _set_index(self, index: dict[str, Song]) -> None:
        self.index = index
        self._songs = self._videos = None
        self.changed.emit()

    # --- loading and scanning ---
    def start(self) -> None:
        """Read the stored index (on the worker), show it, then look for changes on disk."""
        self.worker.submit(library.load_index, self.index_path, on_done=self._loaded, on_error=self._load_failed)

    def _loaded(self, index: dict[str, Song]) -> None:
        self.loaded = True
        if index:
            self._set_index(index)
        self.rescan()

    def _load_failed(self, _error: Exception) -> None:
        self.loaded = True
        self.rescan()

    def rescan(self) -> None:
        """Scan the library folders; a request during a scan runs once more after it."""
        if self._scanning:
            self._again = True
            return
        self._scanning = True
        self._again = False
        self._cancel = threading.Event()
        self.scanning_changed.emit(True)
        music, videos = list(self.settings.music_folders), list(self.settings.video_folders)
        previous = dict(self.index)
        cancel, path = self._cancel, self.index_path
        progress = self.progress

        def run() -> library.ScanResult:
            audio = library.scan(music, ("audio",), previous, cancel, lambda count, _path: progress.emit(count))
            base = len(audio.index)
            video = library.scan(videos, ("video",), previous, cancel,
                                 lambda count, _path: progress.emit(base + count))
            merged = dict(audio.index)
            merged.update(video.index)
            result = library.ScanResult(merged, audio.read + video.read, audio.kept + video.kept,
                                        0, audio.cancelled or video.cancelled, audio.unreadable + video.unreadable)
            result.removed = sum(1 for known in previous if known not in merged)
            if not result.cancelled:
                library.save_index(path, merged)
            return result

        submitted = self.worker.submit(run, on_done=self._scanned, on_error=self._scan_failed)
        if submitted is None:
            self._scanning = False
            self.scanning_changed.emit(False)

    def cancel(self) -> None:
        self._again = False
        self._cancel.set()

    def _scanned(self, result: library.ScanResult) -> None:
        self._scanning = False
        if not result.cancelled and (result.read or result.removed or len(result.index) != len(self.index)
                                     or not self.index):
            self._set_index(result.index)
        self.scanning_changed.emit(False)
        self.finished.emit(result)
        if self._again:
            self.rescan()

    def _scan_failed(self, error: Exception) -> None:
        self._scanning = False
        self.scanning_changed.emit(False)
        self.failed.emit(str(error) or error.__class__.__name__)
        if self._again:
            self.rescan()

    def set_duration(self, path: str, duration_ms: int) -> None:
        """Note the length of a file the tags did not tell (learned from the player)."""
        song = self.index.get(path)
        if song is not None and duration_ms > 0 and song.duration_ms != duration_ms:
            song.duration_ms = duration_ms
            self._dirty = True

    _dirty = False

    def save_if_changed(self) -> None:
        """Store lengths learned since the last scan (called when the window closes)."""
        if self._dirty and not self._scanning:
            self._dirty = False
            try:
                library.save_index(self.index_path, self.index)
            except OSError:
                pass

    def shutdown(self) -> None:
        self._cancel.set()
        self._again = False
