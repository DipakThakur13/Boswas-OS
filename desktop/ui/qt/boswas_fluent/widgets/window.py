"""FluentWindow: the base of an application window."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtGui import QGuiApplication, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import QMainWindow, QWidget

from .. import desktop, icons
from ..shortcuts import add_shortcut
from ..theming import ensure_applied
from . import dialogs
from .navigation import NavigationView


class FluentWindow(QMainWindow):
    """The main window of an application.

    * The background is the Mica tint of the theme; the frame and title bar
      come from the compositor, so there is no title bar here.
    * ``app_id`` (the desktop file name without ``.desktop``) is set on the
      application if it has none yet, so the compositor shows the right icon.
    * ``navigation=True`` (or ``navigation={...}`` with NavigationView options)
      makes a NavigationView the central widget: ``window.navigation``.
      Otherwise give the content with ``set_content``.
    * Size and maximised state are remembered between runs in
      ``$XDG_STATE_HOME/<app_id>/window.ini`` (only for a window that has an
      ``app_id`` and was shown).
    * ``ask`` and ``tell`` show a ContentDialog inside the window.
    """

    def __init__(self, app_id: str = "", title: str = "", parent: QWidget | None = None, *,
                 navigation: bool | dict = False, icon: QIcon | str | None = None,
                 size: tuple[int, int] = (1100, 720), minimum_size: tuple[int, int] = (480, 360),
                 remember_geometry: bool = True) -> None:
        super().__init__(parent)
        ensure_applied()
        self.app_id = desktop.check_app_id(app_id) if app_id else ""
        self._remember = bool(remember_geometry and self.app_id)
        self._restored = False
        self._was_shown = False
        self.setObjectName("fluentWindow")
        if self.app_id and not QGuiApplication.desktopFileName():
            QGuiApplication.setDesktopFileName(self.app_id)
        if title:
            self.setWindowTitle(title)
        qicon = icons.icon(icon) if isinstance(icon, str) else icon
        if qicon is None and self.app_id:
            qicon = icons.icon(self.app_id)
        if qicon is not None and not qicon.isNull():
            self.setWindowIcon(qicon)
        self.setMinimumSize(*minimum_size)
        self.resize(*size)
        self.navigation: NavigationView | None = None
        if navigation:
            options = navigation if isinstance(navigation, dict) else {}
            self.navigation = NavigationView(self, **options)
            self.setCentralWidget(self.navigation)

    def set_content(self, widget: QWidget) -> QWidget:
        """Make ``widget`` the content of a window without navigation; returns it."""
        self.setCentralWidget(widget)
        return widget

    def add_shortcut(self, keys: str | QKeySequence | QKeySequence.StandardKey,
                     slot: Callable[[], None]) -> list[QShortcut]:
        """A window shortcut: a standard name (``"find"``, ``"refresh"``, ``"quit"``) or a key sequence."""
        return add_shortcut(self, keys, slot)

    def ask(self, title: str, text: str, confirm: str = "OK", cancel: str = "Cancel", *,
            default: str = "cancel") -> bool:
        """A yes-or-no question in a ContentDialog; True when the user confirmed."""
        return dialogs.ask(self, title, text, confirm, cancel, default=default)

    def tell(self, title: str, text: str, close: str = "OK") -> None:
        """A message in a ContentDialog with one button."""
        dialogs.message(self, title, text, close)

    # --- geometry ---
    def _settings(self) -> QSettings | None:
        if not self._remember:
            return None
        try:
            path = desktop.state_dir(self.app_id) / "window.ini"
        except OSError:
            return None
        return QSettings(str(path), QSettings.Format.IniFormat)

    def restore_geometry(self) -> bool:
        """Apply the remembered size and state (called by the first show); True if there was one."""
        settings = self._settings()
        if settings is None:
            return False
        data = settings.value("window/geometry")
        return isinstance(data, QByteArray) and not data.isEmpty() and self.restoreGeometry(data)

    def save_geometry(self) -> None:
        settings = self._settings()
        if settings is not None:
            settings.setValue("window/geometry", self.saveGeometry())
            settings.sync()

    def showEvent(self, event) -> None:
        if not self._restored:
            self._restored = True
            self.restore_geometry()
        self._was_shown = True
        super().showEvent(event)

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        if event.isAccepted() and self._was_shown:
            self.save_geometry()
