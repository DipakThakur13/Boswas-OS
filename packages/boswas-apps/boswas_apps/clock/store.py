"""Clock's document in a running application (QtCore; the toolkit's Worker).

``ClockStore.doc`` is the document in memory. ``mutate(change)`` applies a
change to it at once (so the window can show it) and then, on a worker
thread and under the file lock, to the document on disk; the file is
written atomically. The reminder service changes the same file the same
way, so neither overwrites what the other did. When the file changes on
disk (the service restarted a timer or switched a one-time alarm off) it
is read again and ``changed`` is emitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

import boswas_fluent as fluent

from ..reminders import jsonfile, paths
from . import model
from .settings import Settings

MAX_BYTES = 8 * 1024 * 1024


def _read(path: Path) -> tuple[dict | None, object]:
    """(the normalized document or None when there is no file, the file's signature)."""
    raw = jsonfile.read_json(path, max_bytes=MAX_BYTES)
    return (model.normalize(raw) if raw is not None else None), jsonfile.signature(path)


class ClockStore(QObject):
    changed = Signal()                 # the document in memory is different now
    failed = Signal(str)               # a message for the user: reading or saving did not work

    def __init__(self, path: Path | None = None, parent: QObject | None = None, *,
                 now: Callable[[], model.Now] = model.system_now, watch: bool = True) -> None:
        super().__init__(parent)
        self.path = Path(path) if path is not None else paths.clock_store()
        self.now = now
        self.worker = fluent.Worker(self, max_threads=1)      # one thread: saves happen in the order made
        self._pending = 0
        self._signature: object = None
        self.load_error = ""
        try:
            doc, self._signature = _read(self.path)
        except jsonfile.StoreError as exc:
            aside = jsonfile.set_aside(self.path)
            self.load_error = f"{exc}. It was set aside as {aside.name if aside else 'a copy'}; Clock starts empty."
            doc = None
        if doc is None:
            doc = model.first_run_doc()
            self.doc = doc
            self._save(lambda fresh: fresh.update(doc) if not fresh["timers"] and not fresh["alarms"] else None)
        else:
            self.doc = doc
        self._watcher: QFileSystemWatcher | None = None
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(150)
        self._reload_timer.timeout.connect(self._check_disk)
        if watch:
            self._watcher = QFileSystemWatcher(self)
            self._watcher.directoryChanged.connect(lambda _path: self._reload_timer.start())
            try:
                paths.ensure_private_dir(self.path.parent)
                self._watcher.addPath(str(self.path.parent))
            except OSError:
                pass

    # --- changes ---
    def mutate(self, change: Callable[[dict], None]) -> None:
        """Apply ``change(doc)`` now in memory and then on disk. ``change`` must only depend on the document."""
        change(self.doc)
        self.changed.emit()
        self._save(change)

    def _save(self, change: Callable[[dict], None]) -> None:
        def on_disk(raw):
            doc = model.normalize(raw)
            change(doc)
            return doc

        def work():
            doc = jsonfile.update_json(self.path, on_disk, max_bytes=MAX_BYTES)
            return doc, jsonfile.signature(self.path)

        self._pending += 1
        self.worker.submit(work, on_done=self._saved, on_error=self._save_failed)

    def _saved(self, result) -> None:
        self._pending -= 1
        doc, self._signature = result
        if self._pending == 0 and doc != self.doc:
            self.doc = doc             # somebody else changed the file in between: theirs and ours, merged
            self.changed.emit()

    def _save_failed(self, error: Exception) -> None:
        self._pending -= 1
        self.failed.emit(f"Your change could not be saved: {error}")

    # --- the file changed under us ---
    def _check_disk(self) -> None:
        if self._pending or jsonfile.signature(self.path) == self._signature:
            return
        self.worker.submit(_read, self.path, on_done=self._reloaded, on_error=lambda _error: None, key="reload")

    def _reloaded(self, result) -> None:
        doc, signature = result
        if self._pending:
            return                     # a save of ours is on its way; its result is newer
        self._signature = signature
        if doc is not None and doc != self.doc:
            self.doc = doc
            self.changed.emit()

    def reload_now(self) -> None:
        """Look at the file at once (tests; the window when it is shown again)."""
        self._check_disk()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every change is on disk; False when that takes longer than ``timeout`` seconds."""
        return self.worker.wait(timeout)

    def close(self) -> None:
        self.flush()
        self._reload_timer.stop()
        self.worker.shutdown()


class SettingsStore(QObject):
    """Clock's settings: read once, written (off the GUI thread) when one changes."""

    changed = Signal()

    def __init__(self, path: Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = Path(path) if path is not None else paths.clock_settings()
        try:
            self.values = Settings.from_json(jsonfile.read_json(self.path, max_bytes=1024 * 1024))
        except jsonfile.StoreError:
            self.values = Settings()
        self.worker = fluent.Worker(self, max_threads=1)

    def set(self, name: str, value) -> None:
        if getattr(self.values, name) == value:
            return
        setattr(self.values, name, value)
        self.values = Settings.from_json(self.values.to_json())      # clamps what is out of range
        self.worker.submit(jsonfile.write_json_atomic, self.path, self.values.to_json())
        self.changed.emit()

    def close(self) -> None:
        self.worker.wait(5.0)
        self.worker.shutdown()
