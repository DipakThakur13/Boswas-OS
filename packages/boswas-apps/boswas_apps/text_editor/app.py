"""The application behind the windows of the editor: settings, recent files, the session, one instance."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject

import boswas_fluent as fluent

from . import session
from . import settings as settings_store
from .ui.window import EditorWindow

NEW_WINDOW = "--new-window"


class EditorApplication(QObject):
    """What the windows share. ``config_dir`` holds the settings, ``state_dir`` the session and recent files."""

    def __init__(self, config_dir: str | os.PathLike, state_dir: str | os.PathLike,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config_dir = Path(config_dir)
        self.state_dir = Path(state_dir)
        self.settings = settings_store.load(self.config_dir)
        self.recent = settings_store.load_recent(self.state_dir)
        self.windows: list[EditorWindow] = []
        self._active: EditorWindow | None = None
        self.worker = fluent.Worker(self, max_threads=1)

    # --- windows ---
    def new_window(self, *, restore: bool = False, show: bool = True) -> EditorWindow:
        """Open a window: with the tabs of the last session (``restore``), else with one empty tab."""
        window = EditorWindow(self)
        self.windows.append(window)
        self._active = window
        if restore and self.settings["restore_session"]:
            states, current = session.load(self.state_dir)
            if states:
                window.restore_session(states, current)
        if window.tab_bar.count() == 0:
            window.new_tab()
        if show:
            window.show()
        return window

    def active_window(self) -> EditorWindow | None:
        if self._active in self.windows:
            return self._active
        return self.windows[-1] if self.windows else None

    def window_activated(self, window: EditorWindow) -> None:
        if window in self.windows:
            self._active = window

    def window_closed(self, window: EditorWindow) -> None:
        if window in self.windows:
            self.windows.remove(window)
        if self._active is window:
            self._active = self.windows[-1] if self.windows else None

    def is_last_window(self, window: EditorWindow) -> bool:
        return [other for other in self.windows if other is not window] == []

    def session_window(self) -> EditorWindow | None:
        """The window whose tabs are the session: the one used last."""
        return self.active_window()

    def quit(self) -> None:
        """File > Exit: close every window (each asks about what it would lose)."""
        for window in list(self.windows):
            window.close()

    def handle_arguments(self, arguments: list[str]) -> EditorWindow | None:
        """Another start of the program: open its files here (in a new window with ``--new-window``)."""
        files = [argument for argument in arguments if argument != NEW_WINDOW]
        window = self.active_window()
        if NEW_WINDOW in arguments or window is None:
            window = self.new_window()
        if files:
            window.open_paths(files)
        return window

    def save_session_now(self) -> None:
        """Keep the session when the program is told to quit without its windows being closed."""
        window = self.session_window()
        if window is not None and self.settings["restore_session"]:
            session.save(self.state_dir, window.session_states(), window._session_current())

    # --- settings and recent files ---
    def settings_changed(self, apply: bool = True) -> None:
        """A setting changed: every window follows, and the file is written in the background."""
        if apply:
            for window in self.windows:
                window.apply_settings()
        self.worker.submit(settings_store.save, self.config_dir, dict(self.settings))

    def add_recent(self, path: str) -> None:
        updated = settings_store.add_recent(self.recent, path)
        if updated != self.recent:
            self.recent[:] = updated
            self.worker.submit(settings_store.save_recent, self.state_dir, list(self.recent))

    def clear_recent(self) -> None:
        self.recent.clear()
        self.worker.submit(settings_store.save_recent, self.state_dir, [])

    def shutdown(self) -> None:
        self.worker.wait(3)
        self.worker.shutdown()
