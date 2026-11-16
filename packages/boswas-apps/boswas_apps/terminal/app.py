"""The application: its settings, its windows, one instance for a session.

``fluent.run`` is made for an application with one window. A terminal has
several, any of which may be the one a later start of the program should
open its tab in, so ``main`` is put together here from the same pieces of
the toolkit: ``SingleInstance``, ``apply``, ``install_signal_handlers`` and
``activate_window``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

import boswas_fluent as fluent

from . import APP_ID, DISPLAY_NAME, PROGRAM, VERSION, cli
from . import settings as settings_module

EXIT_USAGE = 2
EXIT_ROOT = 4
SAVE_DELAY_MS = 400


class SettingsStore(QObject):
    """The settings in use, shared by every window. ``update()`` after a change: tabs follow, the file is
    written a moment later (off the GUI thread)."""

    changed = Signal()
    save_failed = Signal(str)

    def __init__(self, path: Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = path if path is not None else fluent.config_dir(APP_ID) / settings_module.FILE_NAME
        self.settings, self.error = settings_module.load(self.path)
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self._save_later)

    def update(self) -> None:
        self._dirty = True
        self.error = None
        self.changed.emit()
        self._timer.start()

    def _save_later(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        snapshot = self.settings.copy()
        fluent.run_in_thread(lambda: settings_module.save(self.path, snapshot), on_error=self._failed)

    def _failed(self, error: Exception) -> None:
        reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
        self.save_failed.emit(f"The settings could not be saved: {reason}.")

    def save_now(self) -> None:
        """Write pending changes before the application ends."""
        self._timer.stop()
        if self._dirty:
            self._dirty = False
            try:
                settings_module.save(self.path, self.settings)
            except OSError as error:
                self._failed(error)


class TerminalApp(QObject):
    """The windows of this process."""

    def __init__(self, session_factory=None, store: SettingsStore | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.store = store if store is not None else SettingsStore(parent=self)
        self.session_factory = session_factory
        self.windows: list = []

    def new_window(self):
        from .window import TerminalWindow
        window = TerminalWindow(self, self.session_factory)
        self.windows.append(window)
        return window

    def active_window(self):
        """The window a new tab goes to: the active one, else the one opened last."""
        for window in self.windows:
            if window.isActiveWindow():
                return window
        return self.windows[-1] if self.windows else None

    def open(self, request: cli.Request):
        """Open what a start of the program asked for: a tab in a window that exists, or a new window."""
        window = None if request.new_window else self.active_window()
        if window is None:
            window = self.new_window()
        window.open_request(request)
        return window

    def handle_arguments(self, arguments: list[str], activation_token: str = "") -> None:
        """Another start of the program passed its request on."""
        window = self.open(cli.decode(arguments))
        fluent.activate_window(window, activation_token)

    def window_closed(self, window) -> None:
        if window in self.windows:
            self.windows.remove(window)
            window.deleteLater()
        if not self.windows:
            self.store.save_now()

    def shutdown(self) -> None:
        self.store.save_now()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    program = os.path.basename(argv[0]) if argv else PROGRAM
    try:
        invocation = cli.parse(argv[1:])
    except cli.UsageError as error:
        sys.stderr.write(f"{program}: {error}\nTry '{PROGRAM} --help'.\n")
        return EXIT_USAGE
    if invocation.show == "help":
        sys.stdout.write(cli.USAGE)
        return 0
    if invocation.show == "version":
        sys.stdout.write(cli.version_text() + "\n")
        return 0
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.stderr.write(f"{program}: runs as the desktop user, never as root\n")
        return EXIT_ROOT
    request = invocation.request
    encoded = cli.encode(request)
    app = QApplication.instance() or QApplication([argv[0] if argv else PROGRAM])
    app.setDesktopFileName(APP_ID)
    app.setApplicationName(APP_ID)
    app.setOrganizationName("Boswas")
    app.setApplicationDisplayName(DISPLAY_NAME)
    app.setApplicationVersion(VERSION)
    instance = None
    if not invocation.separate and encoded is not None:
        instance = fluent.SingleInstance(APP_ID, app)
        if not instance.start(encoded):
            return 0
    fluent.apply(app)
    icon = fluent.icon("boswas-terminal", "utilities-terminal")
    if not icon.isNull():
        app.setWindowIcon(icon)
    # The process directory is of no use to a terminal (every tab has its own) and must not keep a
    # removable disk busy.
    try:
        os.chdir("/")
    except OSError:
        pass
    application = TerminalApp(parent=app)
    window = application.open(request)
    if instance is not None:
        instance.message_received.connect(
            lambda arguments: application.handle_arguments(list(arguments), instance.activation_token))
    signals = fluent.install_signal_handlers(app)
    window.show()
    try:
        return int(app.exec())
    finally:
        signals.restore()
        if instance is not None:
            instance.close()
        application.shutdown()
