"""The desktop around an application: its directories, opening files and addresses, notifications.

Nothing here starts a shell. ``open_url`` and ``open_path`` ask the desktop
portal over D-Bus and fall back to ``xdg-open`` with an argument list;
``notify`` talks to ``org.freedesktop.Notifications`` over D-Bus and does
nothing when no notification service is running.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
URL_SCHEMES = ("http", "https", "mailto")
XDG_OPEN = ("/usr/bin/xdg-open", "/bin/xdg-open")
_PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.portal.OpenURI")
_NOTIFICATIONS = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                  "org.freedesktop.Notifications")
_MAX_URL = 8192
_DBUS_TIMEOUT_MS = 1500


def check_app_id(app_id: str) -> str:
    """``app_id`` if it is usable as a desktop file name and a directory name, else ValueError."""
    if not isinstance(app_id, str) or not APP_ID.match(app_id) or ".." in app_id:
        raise ValueError(f"not an application ID: {app_id!r}")
    return app_id


def _xdg(variable: str, default: str) -> Path:
    value = os.environ.get(variable) or ""
    return Path(value) if value.startswith("/") else Path.home() / default


def _app_dir(base: Path, app_id: str, create: bool) -> Path:
    path = base / check_app_id(app_id)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def config_dir(app_id: str, create: bool = True) -> Path:
    """``$XDG_CONFIG_HOME/<app_id>``: settings the user chose. Created (mode 0700) unless ``create=False``."""
    return _app_dir(_xdg("XDG_CONFIG_HOME", ".config"), app_id, create)


def state_dir(app_id: str, create: bool = True) -> Path:
    """``$XDG_STATE_HOME/<app_id>``: what the application remembers (window size, recent files, history)."""
    return _app_dir(_xdg("XDG_STATE_HOME", ".local/state"), app_id, create)


def cache_dir(app_id: str, create: bool = True) -> Path:
    """``$XDG_CACHE_HOME/<app_id>``: what can be rebuilt (thumbnails)."""
    return _app_dir(_xdg("XDG_CACHE_HOME", ".cache"), app_id, create)


def data_dir(app_id: str, create: bool = True) -> Path:
    """``$XDG_DATA_HOME/<app_id>``: the user's data that is not a document."""
    return _app_dir(_xdg("XDG_DATA_HOME", ".local/share"), app_id, create)


def runtime_dir() -> Path | None:
    """``$XDG_RUNTIME_DIR`` when it is a directory that belongs to this user and nobody else can enter."""
    value = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not value.startswith("/"):
        return None
    try:
        info = os.stat(value)
    except OSError:
        return None
    if not os.path.isdir(value) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return None
    return Path(value)


# --- D-Bus -------------------------------------------------------------------------------------------------

def _session_bus():
    """The session bus, or None when there is none (or QtDBus is not installed)."""
    try:
        from PySide6.QtDBus import QDBusConnection
    except ImportError:
        return None
    if not (os.environ.get("DBUS_SESSION_BUS_ADDRESS") or runtime_dir() is not None):
        return None
    bus = QDBusConnection.sessionBus()
    return bus if bus.isConnected() else None


def _service_running(bus, service: str) -> bool:
    interface = bus.interface()
    if interface is None:
        return False
    reply = interface.isServiceRegistered(service)
    return bool(reply.isValid() and reply.value())


def _xdg_open(argument: str) -> bool:
    from PySide6.QtCore import QProcess
    for program in XDG_OPEN:
        if os.path.isfile(program) and os.access(program, os.X_OK):
            return bool(QProcess.startDetached(program, [argument])[0])
    return False


def check_url(url: str, schemes: tuple[str, ...] = URL_SCHEMES) -> str:
    """``url`` normalised, if it is a valid address with one of ``schemes``; else ValueError."""
    from PySide6.QtCore import QUrl
    if not isinstance(url, str) or not url or len(url) > _MAX_URL or any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        raise ValueError("not a URL")
    parsed = QUrl(url, QUrl.ParsingMode.StrictMode)
    if not parsed.isValid() or parsed.scheme().lower() not in schemes:
        raise ValueError(f"not a URL this application opens: {url[:80]!r}")
    if parsed.scheme().lower() in ("http", "https") and not parsed.host():
        raise ValueError(f"the URL has no host: {url[:80]!r}")
    return bytes(parsed.toEncoded()).decode("ascii")


def open_url(url: str, *, schemes: tuple[str, ...] = URL_SCHEMES) -> bool:
    """Open a web or mail address in the user's default application. True when the request was handed over.

    Only ``http``, ``https`` and ``mailto`` by default; anything else is
    refused (returns False). The desktop portal is asked first, then
    ``xdg-open`` is started with the address as its only argument.
    """
    try:
        address = check_url(url, schemes)
    except ValueError:
        return False
    bus = _session_bus()
    if bus is not None and _service_running(bus, _PORTAL[0]):
        from PySide6.QtDBus import QDBusMessage
        call = QDBusMessage.createMethodCall(*_PORTAL, "OpenURI")
        call.setArguments(["", address, {}])
        bus.asyncCall(call, _DBUS_TIMEOUT_MS)
        return True
    return _xdg_open(address)


def open_path(path: str | os.PathLike) -> bool:
    """Open a file or folder with the user's default application. True when the request was handed over.

    The path must exist. The portal receives an open file descriptor (so it
    opens exactly this file); the fallback is ``xdg-open`` with the absolute
    path as its only argument.
    """
    try:
        target = Path(os.fspath(path)).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    bus = _session_bus()
    if bus is not None and _service_running(bus, _PORTAL[0]):
        from PySide6.QtDBus import QDBusMessage, QDBusUnixFileDescriptor
        try:
            descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            return False
        try:
            call = QDBusMessage.createMethodCall(*_PORTAL, "OpenFile")
            call.setArguments(["", QDBusUnixFileDescriptor(descriptor), {}])
            bus.asyncCall(call, _DBUS_TIMEOUT_MS)
        finally:
            os.close(descriptor)             # QDBusUnixFileDescriptor holds its own duplicate
        return True
    return _xdg_open(str(target))


def notify(summary: str, body: str = "", *, app_name: str | None = None, icon: str = "", timeout_ms: int = -1,
           replaces_id: int = 0, desktop_entry: str | None = None) -> int:
    """Show a desktop notification. Returns its ID, or 0 when none was shown.

    Silent when there is no session bus or no notification service: the
    call returns 0 and nothing else happens. ``timeout_ms``: -1 lets the
    service decide, 0 keeps the notification until dismissed.
    ``replaces_id`` updates an earlier notification. ``desktop_entry`` (default:
    the application's desktop file name) lets the shell show the right
    icon and group. Text is sent as plain text: markup characters are
    escaped, because a service may render basic markup in the body.

    The call waits for the service's answer for at most 1.5 seconds.
    """
    if not isinstance(summary, str) or not summary.strip():
        return 0
    bus = _session_bus()
    if bus is None or not _service_running(bus, _NOTIFICATIONS[0]):
        return 0
    from PySide6.QtCore import Q_ARG, Q_RETURN_ARG, QMetaObject, Qt
    from PySide6.QtDBus import QDBusInterface
    from PySide6.QtGui import QGuiApplication

    interface = QDBusInterface(*_NOTIFICATIONS, bus)
    if not interface.isValid():
        return 0
    interface.setTimeout(_DBUS_TIMEOUT_MS)
    app = QGuiApplication.instance()
    name = app_name or (app.applicationDisplayName() or app.applicationName() if app is not None else "") or ""
    hints: dict[str, object] = {}
    entry = desktop_entry or (QGuiApplication.desktopFileName() if app is not None else "")
    if entry and APP_ID.match(entry):
        hints["desktop-entry"] = entry
    escaped = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    try:
        result = QMetaObject.invokeMethod(
            interface, "Notify", Qt.ConnectionType.DirectConnection, Q_RETURN_ARG("uint"),
            Q_ARG(str, name[:200]), Q_ARG("uint", max(0, int(replaces_id))), Q_ARG(str, icon[:200]),
            Q_ARG(str, summary[:500]), Q_ARG(str, escaped[:4000]), Q_ARG("QStringList", []),
            Q_ARG("QVariantMap", hints), Q_ARG(int, int(timeout_ms)))
    except (RuntimeError, TypeError, ValueError):
        return 0
    return int(result) if isinstance(result, int) and not isinstance(result, bool) else 0
