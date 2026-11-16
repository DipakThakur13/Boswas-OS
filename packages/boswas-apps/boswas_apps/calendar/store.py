"""Calendar's data in a running application (QtCore; the toolkit's Worker).

``CalendarStore.data`` holds the calendars and events; the views read it.
Every edit is a ``model.Change``: ``commit`` records it for undo and saves
the whole document a moment later, off the GUI thread, by writing a new
file and renaming it over the old one. The reminder service reads that
file. Settings are kept in their own file and saved the same way.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

import boswas_fluent as fluent

from ..reminders import jsonfile, paths
from . import model
from .settings import Settings

SAVE_DELAY_MS = 250
MAX_UNDO = 100


def _write(path: Path, calendars: list, events: list) -> None:
    jsonfile.write_json_atomic(path, {"version": model.VERSION,
                                      "calendars": [calendar.to_json() for calendar in calendars],
                                      "events": [event.to_json() for event in events]})


class CalendarStore(QObject):
    changed = Signal()                 # calendars or events are different now
    settings_changed = Signal()
    failed = Signal(str)               # a message for the user
    undo_changed = Signal()

    def __init__(self, path: Path | None = None, settings_path: Path | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = Path(path) if path is not None else paths.calendar_store()
        self.settings_path = Path(settings_path) if settings_path is not None else paths.calendar_settings()
        self.worker = fluent.Worker(self, max_threads=1)       # one thread: files are written in order
        self.load_error = ""
        raw = None
        try:
            raw = jsonfile.read_json(self.path)
        except jsonfile.StoreError as exc:
            aside = jsonfile.set_aside(self.path)
            self.load_error = (f"{exc}. It was set aside as {aside.name if aside else 'a copy'}; "
                               "Calendar starts empty.")
        self.data = model.CalendarData.from_json(raw)
        try:
            self.settings = Settings.from_json(jsonfile.read_json(self.settings_path, max_bytes=1024 * 1024))
        except jsonfile.StoreError:
            self.settings = Settings()
        self._undo: list = []
        self._redo: list = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self._save_now)
        if self.data.ensure_default():
            self._timer.start()

    # --- saving ---
    def touch(self) -> None:
        """The data was changed: tell the views and save soon."""
        self.changed.emit()
        self._timer.start()

    def _save_now(self) -> None:
        self.worker.submit(_write, self.path, list(self.data.calendars), list(self.data.events()),
                           on_error=lambda error: self.failed.emit(f"Your changes could not be saved: {error}"))

    def flush(self, timeout: float = 10.0) -> bool:
        """Write what is not written yet and wait for it; False when that takes longer than ``timeout``."""
        if self._timer.isActive():
            self._timer.stop()
            self._save_now()
        return self.worker.wait(timeout)

    def close(self) -> None:
        self.flush()
        self.worker.shutdown()

    # --- edits with undo ---
    def commit(self, change: model.Change) -> None:
        """Record an edit that ``self.data`` has already applied."""
        self._undo.append(change)
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self.undo_changed.emit()
        self.touch()

    def commit_action(self, label: str, undo, redo) -> None:
        """Record an edit that is not a change of events (a deleted calendar), as two callables."""
        self._undo.append((label, undo, redo))
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self.undo_changed.emit()
        self.touch()

    def undo_label(self) -> str:
        return self._label(self._undo[-1]) if self._undo else ""

    def redo_label(self) -> str:
        return self._label(self._redo[-1]) if self._redo else ""

    @staticmethod
    def _label(entry) -> str:
        return entry.label if isinstance(entry, model.Change) else entry[0]

    def undo(self) -> str:
        """Take back the last edit; returns what it was (``""`` when there is nothing to undo)."""
        if not self._undo:
            return ""
        entry = self._undo.pop()
        if isinstance(entry, model.Change):
            self.data.apply(entry.reversed())
        else:
            entry[1]()
        self._redo.append(entry)
        self.undo_changed.emit()
        self.touch()
        return self._label(entry)

    def redo(self) -> str:
        if not self._redo:
            return ""
        entry = self._redo.pop()
        if isinstance(entry, model.Change):
            self.data.apply(entry)
        else:
            entry[2]()
        self._undo.append(entry)
        self.undo_changed.emit()
        self.touch()
        return self._label(entry)

    # --- settings ---
    def set_setting(self, name: str, value) -> None:
        if getattr(self.settings, name) == value:
            return
        setattr(self.settings, name, value)
        self.settings = Settings.from_json(self.settings.to_json())
        self.worker.submit(jsonfile.write_json_atomic, self.settings_path, self.settings.to_json(),
                           on_error=lambda error: self.failed.emit(f"The settings could not be saved: {error}"))
        self.settings_changed.emit()

    def default_calendar(self) -> str:
        """The calendar new events go into: the one chosen in the settings, else the first visible one."""
        chosen = self.settings.default_calendar
        if chosen and self.data.calendar(chosen) is not None:
            return chosen
        for calendar in self.data.calendars:
            if calendar.visible:
                return calendar.id
        return self.data.calendars[0].id
