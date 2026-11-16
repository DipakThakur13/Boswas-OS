"""What the windows of the application share: settings, the Trash, undo, jobs, thumbnails, volumes, applications."""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication

import boswas_fluent as fluent

from . import APP_ID, clipboard, mime, places
from .desktopapps import AppIndex
from .fileops import UndoStack
from .jobs import JobManager
from .launch import Launcher
from .settings import Settings
from .thumbnailer import Thumbnailer
from .trash import Trash
from .volmonitor import VolumeMonitor


class Services(QObject):
    """One per application (tests make one per test, on temporary directories)."""

    settings_changed = Signal(str)          # the key that changed
    undo_changed = Signal()
    cut_changed = Signal()                  # the set of files shown as "cut" changed
    apps_loaded = Signal()

    def __init__(self, parent: QObject | None = None, *, bus=None, settings_path: str | None = None,
                 mountinfo: str | None = None) -> None:
        super().__init__(parent)
        if settings_path is None:
            settings_path = str(fluent.config_dir(APP_ID) / "settings.json")
        self.settings = Settings(settings_path)
        self.trash = Trash()
        self.undo = UndoStack()
        self.undo_running = False               # an undo or redo step is at work; further steps wait in the queue
        self.undo_queue: list[str] = []
        self.worker = fluent.Worker(self, max_threads=4)
        self.jobs = JobManager(self)
        self.thumbnailer = Thumbnailer(self)
        self.launcher = Launcher()
        self.new_index = AppIndex                   # tests give an index of their own directories
        self.apps = self.new_index()
        kwargs = {"mountinfo": mountinfo} if mountinfo else {}
        self.volumes = VolumeMonitor(self.worker, bus, self, **kwargs)
        self.cut_paths: frozenset[str] = frozenset()
        self.user_dirs: list[tuple[str, str, str]] = []
        self.reload_user_dirs()
        clip = QGuiApplication.clipboard()
        if clip is not None:
            clip.dataChanged.connect(self._clipboard_changed)

    def start(self) -> None:
        """Begin the background work: read the applications, watch the volumes."""
        self.reload_apps()
        self.volumes.start()

    def reload_apps(self) -> None:
        index = self.new_index()
        self.worker.submit(index.load, on_done=self._apps_ready, key="apps")

    def _apps_ready(self, index: AppIndex) -> None:
        self.apps = index
        self.apps_loaded.emit()

    def reload_user_dirs(self) -> None:
        self.user_dirs = places.user_dirs()
        special = {path: icon for _label, path, icon in self.user_dirs}
        special[places.home()] = "user-home"
        mime.set_special_folders(special)

    def set_setting(self, key: str, value: object) -> bool:
        if self.settings.get(key) == value:
            return True
        if not self.settings.set(key, value):
            return False
        self.settings_changed.emit(key)
        return True

    # --- undo ---
    def push_undo(self, undo) -> None:
        if undo is not None:
            self.undo.push(undo)
            self.undo_changed.emit()

    # --- clipboard ---
    def copy_files(self, paths: list[str], cut: bool = False) -> None:
        clip = QGuiApplication.clipboard()
        clip.setMimeData(clipboard.mime_data(paths, cut))
        self._set_cut(frozenset(paths) if cut else frozenset())

    def clipboard_files(self) -> tuple[list[str], bool]:
        clip = QGuiApplication.clipboard()
        return clipboard.read(clip.mimeData()) if clip is not None else ([], False)

    def copy_text(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self._set_cut(frozenset())

    def clear_cut(self) -> None:
        self._set_cut(frozenset())

    def _set_cut(self, paths: frozenset[str]) -> None:
        if paths != self.cut_paths:
            self.cut_paths = paths
            self.cut_changed.emit()

    def _clipboard_changed(self) -> None:
        if not self.cut_paths:
            return
        paths, cut = self.clipboard_files()
        if not cut or frozenset(paths) != self.cut_paths:
            self._set_cut(frozenset())

    def shutdown(self) -> None:
        self.volumes.stop()
        self.jobs.cancel_all()
        self.jobs.wait(3.0)
        self.thumbnailer.shutdown()
        self.worker.shutdown()


def default_location(settings: Settings) -> str:
    """Where a new window or tab starts."""
    start = settings.get("start_folder")
    if start == "computer":
        return places.COMPUTER
    if isinstance(start, str) and start.startswith("/") and os.path.isdir(start):
        return start
    return places.home()
