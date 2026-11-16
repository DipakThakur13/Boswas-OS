"""What the widget tests of the terminal share: a window on fake sessions, mouse and wheel events."""

from boswas_fluent import testing  # noqa: F401  (first: selects the offscreen platform)

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

from boswas_apps.terminal import cli
from boswas_apps.terminal.app import SettingsStore, TerminalApp
from boswas_apps.terminal.session import FakeSession

NONE = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
CTRL = Qt.KeyboardModifier.ControlModifier
ALT = Qt.KeyboardModifier.AltModifier
LEFT = Qt.MouseButton.LeftButton


def mouse(widget, kind, position, button=LEFT, buttons=None, modifiers=NONE):
    kinds = {"press": QEvent.Type.MouseButtonPress, "release": QEvent.Type.MouseButtonRelease,
             "move": QEvent.Type.MouseMove, "double": QEvent.Type.MouseButtonDblClick}
    if buttons is None:
        buttons = Qt.MouseButton.NoButton if kind == "release" else button
    event = QMouseEvent(kinds[kind], QPointF(position), QPointF(widget.mapToGlobal(position)), button, buttons,
                        modifiers)
    QApplication.sendEvent(widget, event)
    testing.process()


def drag(widget, start, end, modifiers=NONE, button=LEFT):
    mouse(widget, "press", start, button, modifiers=modifiers)
    middle = QPoint((start.x() + end.x()) // 2, (start.y() + end.y()) // 2)
    mouse(widget, "move", middle, Qt.MouseButton.NoButton, button, modifiers)
    mouse(widget, "move", end, Qt.MouseButton.NoButton, button, modifiers)
    mouse(widget, "release", end, button, modifiers=modifiers)


def wheel(widget, position, notches, modifiers=NONE):
    event = QWheelEvent(QPointF(position), QPointF(widget.mapToGlobal(position)), QPoint(0, 0),
                        QPoint(0, int(notches * 120)), Qt.MouseButton.NoButton, modifiers,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)
    testing.process()


def cell_point(view, column, row):
    """The middle of a cell of a view, in its coordinates."""
    return view.cell_rect(column, row).center()


class Sessions:
    """Makes fake sessions and keeps them, with what each was started with."""

    def __init__(self):
        self.made = []
        self.launches = []
        self.sizes = []

    def __call__(self, launch, cols, rows, width, height):
        session = FakeSession()
        self.made.append(session)
        self.launches.append(launch)
        self.sizes.append((cols, rows, width, height))
        return session


class WindowCase(testing.FluentTestCase):
    """A test case with a terminal application whose tabs run nothing."""

    def setUp(self):
        super().setUp()
        self.sessions = Sessions()
        self.store = SettingsStore()
        self.application = TerminalApp(session_factory=self.sessions, store=self.store)

    def tearDown(self):
        for window in list(self.application.windows):
            window.store.settings.confirm_close = False
            window.close()
        testing.process()
        super().tearDown()

    def window(self, request=None, width=900, height=560):
        window = self.application.open(request or cli.Request())
        self.keep(window)
        window.resize(width, height)
        window.show()
        testing.process(20)
        return window

    def assertClosed(self, window):
        """The window is gone: hidden or already deleted, and no longer one of the application's."""
        try:
            visible = window.isVisible()
        except RuntimeError:
            visible = False
        self.assertFalse(visible)
        self.assertNotIn(window, self.application.windows)

    def play(self, window, text, index=None):
        """Output for a tab (the current one), drawn."""
        pane = window.current_pane() if index is None else window.tabs[index]
        pane.session.play(text)
        testing.process()
        pane.view.flush()
        return pane
