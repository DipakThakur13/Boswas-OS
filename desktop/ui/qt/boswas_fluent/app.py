"""Starting an application: ``run``, one instance per user, Ctrl+C.

``run(app_id, window_factory)`` is the whole ``main`` of a typical Boswas
application. It refuses to run as root, creates the QApplication with the
right desktop file name (so the compositor shows the application's icon
and groups its windows), makes sure only one instance runs per session,
applies the theme, builds the window, handles SIGINT and SIGTERM, and
returns the exit code.

One instance: the first start listens on a Unix socket,
``$XDG_RUNTIME_DIR/boswas-fluent/<app_id>.sock`` (the directory is mode
0700, so only the same user can connect). A later start connects, sends
its arguments as one line of JSON, and exits with 0. The first instance
raises its window and, if the window has a method
``handle_arguments(list[str])``, passes the arguments to it. Without a
usable ``$XDG_RUNTIME_DIR`` every start is its own instance.

A new window: ``--new-window`` (NEW_WINDOW) on the command line asks for
another window. An application that can show several windows says so with
``run(..., multiple_windows=True)``; a later start with ``--new-window``
then builds one more window with ``window_factory`` in the running
instance. Otherwise the option is taken out of the arguments and the
window comes forward, as for any later start (an application with one
window, such as Clock or Task Manager). A later start without the option
goes to the window that was active last.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QByteArray, QEvent, QObject, QSocketNotifier, Qt, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from . import desktop, icons
from .theming import apply

EXIT_ROOT = 4
NEW_WINDOW = "--new-window"
_MAX_MESSAGE = 64 * 1024
_MAX_ARGUMENTS = 256
_MAX_ARGUMENT = 8192
_CONNECT_MS = 500
_REPLY_MS = 2000
_READ_MS = 2000


def _valid_arguments(value: object) -> list[str] | None:
    if not isinstance(value, list) or len(value) > _MAX_ARGUMENTS:
        return None
    if not all(isinstance(item, str) and len(item) <= _MAX_ARGUMENT and "\0" not in item for item in value):
        return None
    return list(value)


def _valid_token(value: object) -> str:
    if isinstance(value, str) and 0 < len(value) <= 256 and all(0x20 < ord(c) < 0x7F for c in value):
        return value
    return ""


class SingleInstance(QObject):
    """One running instance of an application per user session (see the module text).

    ``start(arguments)`` returns True when this process is the instance (it
    now listens), False when another one was told and this process should
    exit. ``message_received(arguments)`` is emitted in the instance for
    every later start.
    """

    message_received = Signal(list)

    def __init__(self, app_id: str, parent: QObject | None = None, *, directory: str | os.PathLike | None = None):
        super().__init__(parent)
        self.app_id = desktop.check_app_id(app_id)
        self._directory = Path(directory) if directory is not None else None
        self._server: QLocalServer | None = None
        self._buffers: dict[QLocalSocket, bytearray] = {}
        self.activation_token = ""

    def socket_path(self) -> Path | None:
        """Where the instance listens, or None when there is no private runtime directory."""
        base = self._directory
        if base is None:
            runtime = desktop.runtime_dir()
            if runtime is None:
                return None
            base = runtime / "boswas-fluent"
        return base / f"{self.app_id}.sock"

    def is_listening(self) -> bool:
        return self._server is not None and self._server.isListening()

    def start(self, arguments: list[str] | None = None) -> bool:
        path = self.socket_path()
        if path is None:
            return True
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = os.stat(path.parent)
        except OSError:
            return True
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            return True                   # not a private directory: do not trust a socket in it
        for _attempt in range(2):
            if self._send(path, list(arguments or [])):
                return False
            # Nobody is listening: a socket file left behind by a crash is removed before listening.
            QLocalServer.removeServer(str(path))
            server = QLocalServer(self)
            server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            if server.listen(str(path)):
                server.newConnection.connect(self._connected)
                self._server = server
                return True
            server.deleteLater()          # somebody else started listening in between: tell them
        return True

    def _send(self, path: Path, arguments: list[str]) -> bool:
        """Hand the arguments to the instance that listens on ``path``; False when nobody listens.

        An instance that accepts the connection is running, even if it is too
        busy to answer within the wait: the message is in its socket and is
        read when it gets to it. Its socket is never taken away.
        """
        if not os.path.exists(path):
            return False
        client = QLocalSocket()
        try:
            client.connectToServer(str(path))
            if not client.waitForConnected(_CONNECT_MS):
                return False
            message = {"v": 1, "args": arguments, "token": _valid_token(os.environ.get("XDG_ACTIVATION_TOKEN"))}
            data = json.dumps(message, ensure_ascii=True).encode("ascii") + b"\n"
            if len(data) > _MAX_MESSAGE:
                data = json.dumps({"v": 1, "args": [], "token": message["token"]}).encode("ascii") + b"\n"
            client.write(QByteArray(data))
            client.flush()
            client.waitForBytesWritten(_REPLY_MS)
            answer = bytes(client.readAll())
            deadline = time.monotonic() + _REPLY_MS / 1000
            while (b"\n" not in answer and time.monotonic() < deadline
                   and client.state() == QLocalSocket.LocalSocketState.ConnectedState):
                client.waitForReadyRead(50)
                answer += bytes(client.readAll())
            client.disconnectFromServer()
            return True
        finally:
            client.deleteLater()

    def _connected(self) -> None:
        while self._server is not None and self._server.hasPendingConnections():
            client = self._server.nextPendingConnection()
            self._buffers[client] = bytearray()
            client.readyRead.connect(lambda c=client: self._read(c))
            client.disconnected.connect(lambda c=client: self._gone(c))
            if client.bytesAvailable():
                self._read(client)

    def _gone(self, client: QLocalSocket) -> None:
        self._buffers.pop(client, None)
        client.deleteLater()

    def _read(self, client: QLocalSocket) -> None:
        buffer = self._buffers.get(client)
        if buffer is None:
            return
        buffer += bytes(client.readAll())
        if b"\n" not in buffer:
            if len(buffer) > _MAX_MESSAGE:
                client.abort()
            return
        line = bytes(buffer).split(b"\n", 1)[0]
        del self._buffers[client]
        arguments = None
        token = ""
        if len(line) <= _MAX_MESSAGE:
            try:
                message = json.loads(line.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                message = None
            if isinstance(message, dict) and message.get("v") == 1:
                arguments = _valid_arguments(message.get("args"))
                token = _valid_token(message.get("token"))
        if arguments is None:
            client.abort()
            return
        client.write(QByteArray(b"ok\n"))
        client.flush()
        client.disconnectFromServer()
        self.activation_token = token
        self.message_received.emit(arguments)

    def close(self) -> None:
        """Stop listening and remove the socket."""
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None


def split_new_window(arguments: list[str]) -> tuple[bool, list[str]]:
    """Whether ``arguments`` ask for a new window (NEW_WINDOW before a ``--``), and the arguments without it."""
    wanted, rest, options = False, [], True
    for argument in arguments:
        if options and argument == "--":
            options = False
        elif options and argument == NEW_WINDOW:
            wanted = True
            continue
        rest.append(argument)
    return wanted, rest


class _Windows(QObject):
    """The windows ``run`` made, most recently active last: where a later start goes."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._windows: list[QWidget] = []

    def add(self, window: QWidget) -> None:
        self._windows.append(window)
        window.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowActivate:
            for index, window in enumerate(self._windows):
                if window is watched:
                    self._windows.append(self._windows.pop(index))
                    break
        return False

    def windows(self) -> list[QWidget]:
        """The windows that still exist (a window made for ``--new-window`` is deleted when it closes)."""
        self._windows = [window for window in self._windows if isValid(window)]
        return list(self._windows)

    def target(self) -> QWidget | None:
        """The window a later start goes to: the active one, else the one active last; a closed one only if no
        other is open."""
        windows = self.windows()
        shown = [window for window in windows if window.isVisible()]
        for window in shown:
            if window.isActiveWindow():
                return window
        if shown:
            return shown[-1]
        return windows[0] if windows else None


def activate_window(window: QWidget, activation_token: str = "") -> None:
    """Bring ``window`` to the user: shown, not minimised, raised, active (as far as the compositor allows).

    A Wayland compositor activates a window only for a client that holds an
    activation token; the token of the start that asked is passed on here.
    """
    if activation_token:
        os.environ["XDG_ACTIVATION_TOKEN"] = activation_token
    if window.isMinimized():
        window.showNormal()
    window.show()
    window.raise_()
    window.activateWindow()


class _Signals(QObject):
    """SIGINT and SIGTERM quit the application; Python only sees a signal when Qt lets it run, hence the pipe."""

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self._reader, self._writer = socket.socketpair()
        self._reader.setblocking(False)
        self._writer.setblocking(False)
        self._old_fd = signal.set_wakeup_fd(self._writer.fileno())
        self._notifier = QSocketNotifier(self._reader.fileno(), QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._drain)
        self._old = {}
        for number in (signal.SIGINT, signal.SIGTERM):
            self._old[number] = signal.signal(number, lambda *_args: app.quit())

    def _drain(self, *_args) -> None:
        try:
            self._reader.recv(64)
        except OSError:
            pass

    def restore(self) -> None:
        for number, handler in self._old.items():
            signal.signal(number, handler)
        self._old.clear()
        try:
            signal.set_wakeup_fd(self._old_fd)
        except (ValueError, OSError):
            pass
        self._notifier.setEnabled(False)
        self._reader.close()
        self._writer.close()


def install_signal_handlers(app: QApplication) -> QObject:
    """Make Ctrl+C in the terminal (SIGINT) and SIGTERM quit ``app`` cleanly. Main thread only.

    Returns an object whose ``restore()`` puts the previous handlers back.
    """
    return _Signals(app)


def run(app_id: str, window_factory: Callable[[list[str]], QWidget], *, argv: list[str] | None = None,
        display_name: str | None = None, version: str | None = None, icon: str | None = None,
        single_instance: bool = True, allow_root: bool = False, multiple_windows: bool = False) -> int:
    """Run an application until its last window closes; returns the exit code for ``sys.exit``.

    ``app_id`` is the desktop file name without ``.desktop``
    (``com.boswas.Files``). ``window_factory(arguments)`` builds and returns
    the main window; ``arguments`` are the command line arguments without the
    program name, without the options Qt took for itself and without
    ``--new-window``. ``icon`` is an icon name for the windows (default:
    ``app_id``, then the window's own).

    ``multiple_windows=True``: a later start with ``--new-window`` gets a
    window of its own, built by ``window_factory`` with that start's other
    arguments and deleted when it is closed. Without it such a start brings
    the window forward like any other (the application has one window).

    Returns 4 without starting when run as root (``allow_root=False``), and
    0 when another instance was running and has been told to come forward.
    """
    desktop.check_app_id(app_id)
    argv = list(sys.argv if argv is None else argv)
    if not allow_root and hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.stderr.write(f"{Path(argv[0]).name if argv else app_id}: runs as the desktop user, never as root\n")
        return EXIT_ROOT
    existing = QApplication.instance()
    app = existing or QApplication(argv)
    app.setDesktopFileName(app_id)
    app.setApplicationName(app_id)
    app.setOrganizationName("Boswas")
    if display_name:
        app.setApplicationDisplayName(display_name)
    if version:
        app.setApplicationVersion(version)
    # A QApplication made here has taken Qt's own options out of the arguments; one that existed
    # already (tests) never saw this argv.
    arguments = [str(argument) for argument in (argv if existing is not None else app.arguments())[1:]]

    instance: SingleInstance | None = None
    if single_instance:
        instance = SingleInstance(app_id, app)
        if not instance.start(arguments):
            return 0
    apply(app)
    qicon = icons.icon(icon or app_id)
    if not qicon.isNull():
        app.setWindowIcon(qicon)
    window = window_factory(split_new_window(arguments)[1])
    windows = _Windows(app)
    windows.add(window)

    def another_start(forwarded: list) -> None:
        token = instance.activation_token if instance is not None else ""
        wanted, rest = split_new_window([str(item) for item in forwarded])
        if wanted and multiple_windows:
            extra = window_factory(rest)
            extra.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            windows.add(extra)
            activate_window(extra, token)
            return
        target = windows.target() or window
        handler = getattr(target, "handle_arguments", None)
        if callable(handler):
            handler(rest)
        activate_window(target, token)

    if instance is not None:
        instance.message_received.connect(another_start)
    signals = install_signal_handlers(app)
    window.show()
    try:
        return int(app.exec())
    finally:
        signals.restore()
        if instance is not None:
            instance.close()
