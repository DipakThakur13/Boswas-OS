"""Helpers for the tests of an application built on boswas_fluent (unittest, no QtTest, no display).

    import unittest
    from boswas_fluent import testing

    class WindowTest(testing.FluentTestCase):
        def test_button(self):
            window = self.keep(MyWindow())
            testing.click(window.save_button)
            self.assertEqual(window.status.text(), "Saved")

``FluentTestCase`` gives every test the offscreen QApplication with the
theme applied (dark, without Boswas glass unless ``set_glass(True)`` or the
environment variable ``BOSWAS_FLUENT_TEST_GLASS=1``, not watching the user's
configuration), private ``XDG_*_HOME`` directories and ``XDG_RUNTIME_DIR``
under a temporary directory (``self.tmp``) and no session bus, fails the
test when a Qt callback raised, and closes the widgets given to ``keep``.
Nothing is read from or written to the home directory of whoever runs the
tests.

Import this module before anything creates a QApplication: it selects the
offscreen platform unless ``QT_QPA_PLATFORM`` is already set.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from typing import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, qInstallMessageHandler  # noqa: E402
from PySide6.QtGui import QEnterEvent, QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from . import assets as _assets  # noqa: E402
from . import theming as _theme  # noqa: E402

_QUIET = ("This plugin does not support", "propagateSizeHints")
# Tests draw opaque, so that colours compare exactly, unless they ask for glass (or the whole run does).
GLASS = os.environ.get("BOSWAS_FLUENT_TEST_GLASS") == "1"
_handler_installed = False
_image_dir: tempfile.TemporaryDirectory | None = None


def _quiet(_mode, _context, message: str) -> None:
    # The offscreen platform cannot raise or resize windows and says so; everything else is still reported.
    if not any(text in message for text in _QUIET):
        sys.stderr.write(message + "\n")


def app(scheme: str = "dark") -> QApplication:
    """The QApplication of the tests, created on first use, with the theme applied in ``scheme``."""
    global _handler_installed
    instance = QApplication.instance() or QApplication(["test"])
    if not _handler_installed:
        qInstallMessageHandler(_quiet)
        _handler_installed = True
    manager = _theme.manager()
    if not manager.applied:
        # The images of the style sheet live in one directory for the whole test run: not in the user's
        # cache, and not in the per-test directories, which are deleted while the application is styled.
        global _image_dir
        if _image_dir is None:
            _image_dir = tempfile.TemporaryDirectory(prefix="fluent-test-images-")
            _assets.set_cache_root(_image_dir.name)
        manager.set_glass(GLASS)
        manager.apply(instance, watch=False)
    if manager.scheme != scheme or manager.follows_desktop:
        manager.set_scheme(scheme)
    return instance


def set_scheme(scheme: str) -> None:
    """Switch the running application to ``"dark"`` or ``"light"``."""
    app(scheme)
    process()


def set_glass(on: bool | None) -> None:
    """Draw with Boswas glass (True), opaque (False: what tests get by default) or as the desktop says (None)."""
    app(_theme.manager().scheme)
    _theme.manager().set_glass(on)
    process()


def process(milliseconds: int = 0) -> None:
    """Let Qt handle pending events, for at least ``milliseconds``."""
    deadline = time.monotonic() + milliseconds / 1000
    QCoreApplication.sendPostedEvents()
    QCoreApplication.processEvents()
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.002)


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Process events until ``condition()`` is true; False if it is not within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.002)
    return bool(condition())


def show(widget: QWidget, width: int | None = None, height: int | None = None) -> QWidget:
    """Show a widget (offscreen) at a size and lay it out; returns it."""
    if width is not None and height is not None:
        widget.resize(width, height)
    widget.show()
    process()
    return widget


def grab(widget: QWidget, path: str | os.PathLike) -> Path:
    """Render ``widget`` as it is now into a PNG file; returns the path."""
    process()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not widget.grab().save(str(target), "PNG"):
        raise OSError(f"could not write {target}")
    return target


def _mouse(widget: QWidget, kind: QEvent.Type, position: QPoint, button: Qt.MouseButton,
           buttons: Qt.MouseButton, modifiers: Qt.KeyboardModifier) -> None:
    local = QPointF(position)
    event = QMouseEvent(kind, local, QPointF(widget.mapToGlobal(position)), button, buttons, modifiers)
    QApplication.sendEvent(widget, event)


def click(widget: QWidget, position: QPoint | None = None, *, button: Qt.MouseButton = Qt.MouseButton.LeftButton,
          modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
    """Press and release a mouse button on ``widget`` (in its centre, or at ``position`` in its coordinates).

    As with a real pointer, the click goes to the child of ``widget`` that
    lies under the point. For an item view pass ``view.viewport()`` and a
    point inside the row.
    """
    point = position if position is not None else widget.rect().center()
    target = widget.childAt(point) or widget
    local = target.mapFrom(widget, point) if target is not widget else point
    _mouse(target, QEvent.Type.MouseButtonPress, local, button, button, modifiers)
    _mouse(target, QEvent.Type.MouseButtonRelease, local, button, Qt.MouseButton.NoButton, modifiers)
    process()


def hover(widget: QWidget, position: QPoint | None = None) -> None:
    """Move the pointer onto ``widget`` (its centre, or ``position`` in its coordinates).

    The move goes to the widget's window, as a real pointer's does, so Qt
    itself sends Enter, Leave and hover events, sets ``underMouse()`` and
    gives an item view's row its hover state.
    """
    window = widget.window()
    point = widget.mapTo(window, position if position is not None else widget.rect().center())
    target = window.windowHandle() or window
    local, screen = QPointF(point), QPointF(window.mapToGlobal(point))
    if not window.underMouse():
        # The pointer enters the window first, as it does on a screen.
        QApplication.sendEvent(target, QEnterEvent(local, local, screen))
    event = QMouseEvent(QEvent.Type.MouseMove, local, screen, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, event)
    process()


def key(widget: QWidget, key_code: Qt.Key, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
        text: str = "") -> None:
    """Press and release a key on ``widget``."""
    for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        QApplication.sendEvent(widget, QKeyEvent(kind, key_code, modifiers, text))
    process()


def type_text(widget: QWidget, text: str) -> None:
    """Type ``text`` into ``widget`` one character at a time, as key events."""
    for character in text:
        code = Qt.Key(ord(character.upper())) if character.isalnum() and ord(character) < 128 else Qt.Key.Key_unknown
        if character == " ":
            code = Qt.Key.Key_Space
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(widget, QKeyEvent(kind, code, Qt.KeyboardModifier.NoModifier, character))
    process()


def press_tab(backwards: bool = False) -> QWidget | None:
    """Press Tab (or Shift+Tab) on the focused widget; returns the widget that has the focus afterwards."""
    widget = QApplication.focusWidget()
    if widget is not None:
        if backwards:
            key(widget, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        else:
            key(widget, Qt.Key.Key_Tab)
    return QApplication.focusWidget()


class FluentTestCase(unittest.TestCase):
    """A TestCase with the test application, private XDG directories and exception checking."""

    scheme = "dark"
    _XDG = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR")

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="fluent-test-")
        self.tmp = Path(self._tmp.name)
        self._environment = {name: os.environ.get(name) for name in (*self._XDG, "DBUS_SESSION_BUS_ADDRESS")}
        for name in self._XDG:
            directory = self.tmp / name.lower()
            directory.mkdir(mode=0o700)
            os.environ[name] = str(directory)
        # No session bus: notifications and the portal are out of reach, as on a build machine.
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        self.app = app(self.scheme)
        polisher = _theme.manager()._polisher
        if polisher is not None:           # every test starts in pointer mode: no focus ring left over
            polisher.keyboard = False
            polisher.ring.detach()
        self._kept: list[QWidget] = []
        self.callback_errors: list[str] = []
        self._excepthook = sys.excepthook
        sys.excepthook = lambda *exc: self.callback_errors.append("".join(traceback.format_exception(*exc)))

    def tearDown(self) -> None:
        for widget in self._kept:
            try:
                widget.close()
                widget.deleteLater()
            except RuntimeError:           # already deleted
                pass
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        sys.excepthook = self._excepthook
        for name, value in self._environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if _theme.manager().scheme != "dark":
            _theme.manager().set_scheme("dark")
        if _theme.manager()._forced_glass is not GLASS:
            _theme.manager().set_glass(GLASS)
        self._tmp.cleanup()
        if self.callback_errors:
            self.fail("exceptions in Qt callbacks:\n" + "\n".join(self.callback_errors))
        super().tearDown()

    def keep(self, widget: QWidget) -> QWidget:
        """Close and delete ``widget`` when the test ends; returns it."""
        self._kept.append(widget)
        return widget

    def show(self, widget: QWidget, width: int | None = None, height: int | None = None) -> QWidget:
        """``keep`` and show a widget; returns it."""
        return show(self.keep(widget), width, height)
