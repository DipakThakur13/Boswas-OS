"""Desktop notifications with buttons, through org.freedesktop.Notifications (QtCore and QtDBus only).

``Notifier.notify(message)`` shows a ``messages.Message`` and returns the
ID the notification service gave it (0 when it could not be shown).
``action(id, key)`` is emitted when the user presses a button (``key`` is
the action's key, or ``default`` for a click on the notification itself),
``closed(id, reason)`` when a notification goes away, and
``available(bool)`` when the notification service appears or leaves.

How the call is made. The method ``Notify`` wants an unsigned 32-bit
number and, for the urgency hint, a byte. A Python number sent through
QDBusMessage arrives as a signed 32-bit one, which a notification service
rejects. So the call goes through a QDBusInterface (which learns the
signature from the service's introspection data) with typed arguments,
and the byte is built with QDBusArgument, where a one-character string
selects the byte overload. Both are checked: when the byte cannot be
built the urgency hint is left out and the notification is still shown.
The call blocks for at most ``TIMEOUT_MS``.
"""

from __future__ import annotations

import os

from PySide6.QtCore import SLOT, Q_ARG, Q_RETURN_ARG, QMetaObject, QObject, Qt, Signal, Slot
from PySide6.QtDBus import (QDBusArgument, QDBusConnection, QDBusInterface, QDBusServiceWatcher)

from . import messages, paths

SERVICE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
INTERFACE = "org.freedesktop.Notifications"
TIMEOUT_MS = 3000
APPS = {
    messages.CLOCK: ("Clock", "com.boswas.Clock", "preferences-system-time"),
    messages.CALENDAR: ("Calendar", "com.boswas.Calendar", "office-calendar"),
}


def escape_body(text: str) -> str:
    """Plain text for a body that a notification service may read as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def urgency_byte(level: int) -> QDBusArgument | None:
    """The urgency (0, 1 or 2) as a D-Bus byte, or None when this PySide cannot build one."""
    argument = QDBusArgument()
    try:
        argument << chr(max(0, min(2, int(level))))
    except (TypeError, ValueError, OverflowError):
        return None
    return argument if argument.currentSignature() == "y" else None


class Notifier(QObject):
    action = Signal(int, str)
    closed = Signal(int, int)
    available = Signal(bool)

    def __init__(self, parent: QObject | None = None, bus: QDBusConnection | None = None) -> None:
        super().__init__(parent)
        self._interface: QDBusInterface | None = None
        self._watcher: QDBusServiceWatcher | None = None
        self.last_error = ""
        self.bus: QDBusConnection | None = bus
        if bus is None and (os.environ.get("DBUS_SESSION_BUS_ADDRESS") or paths.runtime_dir() is not None):
            # Without an address and without a runtime directory Qt would try to start a bus of its own.
            self.bus = QDBusConnection.sessionBus()
        if self.bus is None or not self.bus.isConnected():
            self.bus = None
            self.last_error = "there is no session bus"
            return
        self.bus.connect(SERVICE, PATH, INTERFACE, "ActionInvoked", self, SLOT("_on_action(uint,QString)"))
        self.bus.connect(SERVICE, PATH, INTERFACE, "NotificationClosed", self, SLOT("_on_closed(uint,uint)"))
        self._watcher = QDBusServiceWatcher(SERVICE, self.bus, QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange,
                                            self)
        self._watcher.serviceOwnerChanged.connect(self._owner_changed)

    @Slot("uint", str)
    def _on_action(self, identifier: int, key: str) -> None:
        self.action.emit(int(identifier), str(key))

    @Slot("uint", "uint")
    def _on_closed(self, identifier: int, reason: int) -> None:
        self.closed.emit(int(identifier), int(reason))

    def _owner_changed(self, _name: str, _old: str, new: str) -> None:
        self._interface = None
        self.available.emit(bool(new))

    def is_connected(self) -> bool:
        return self.bus is not None and self.bus.isConnected()

    def _get_interface(self) -> QDBusInterface | None:
        if self.bus is None or not self.bus.isConnected():
            self.last_error = "there is no session bus"
            return None
        if self._interface is None or not self._interface.isValid():
            interface = QDBusInterface(SERVICE, PATH, INTERFACE, self.bus, self)
            if self._interface is not None:
                self._interface.deleteLater()
            self._interface = interface
            if not interface.isValid():
                self.last_error = interface.lastError().message() or "no notification service is running"
                return None
            interface.setTimeout(TIMEOUT_MS)
        return self._interface

    def notify(self, message: messages.Message, replaces_id: int = 0) -> int:
        """Show ``message``; returns its ID, or 0 (see ``last_error``) when it could not be shown."""
        interface = self._get_interface()
        if interface is None:
            return 0
        name, desktop_entry, icon = APPS.get(message.app, APPS[messages.CLOCK])
        hints: dict[str, object] = {"desktop-entry": desktop_entry}
        urgency = urgency_byte(message.urgency)
        if urgency is not None:
            hints["urgency"] = urgency
        if message.sound:
            hints["suppress-sound"] = True          # the service plays its own chime
        actions: list[str] = []
        for key, label in message.actions:
            actions += [key, label]
        try:
            result = QMetaObject.invokeMethod(
                interface, "Notify", Qt.ConnectionType.DirectConnection, Q_RETURN_ARG("uint"),
                Q_ARG(str, name), Q_ARG("uint", max(0, int(replaces_id))), Q_ARG(str, icon),
                Q_ARG(str, message.summary[:500]), Q_ARG(str, escape_body(message.body[:4000])),
                Q_ARG("QStringList", actions), Q_ARG("QVariantMap", hints), Q_ARG(int, int(message.timeout)))
        except (RuntimeError, TypeError, ValueError) as exc:
            self.last_error = str(exc)
            self._interface = None
            return 0
        if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
            self.last_error = interface.lastError().message() or "the notification service did not answer"
            self._interface = None
            return 0
        self.last_error = ""
        return int(result)

    def close(self, identifier: int) -> None:
        """Take a notification away (nothing happens when it is gone already)."""
        interface = self._get_interface()
        if interface is None or identifier <= 0:
            return
        try:
            QMetaObject.invokeMethod(interface, "CloseNotification", Qt.ConnectionType.DirectConnection,
                                     Q_ARG("uint", int(identifier)))
        except (RuntimeError, TypeError, ValueError):
            pass
