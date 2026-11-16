"""UDisks2 on the system bus through QtDBus: asynchronous calls and change signals.

Only calls whose replies are simple types are made (see ``volumes.py``);
a reply is handed to the callback as plain Python values. Nothing here
blocks: a call returns at once and the callback runs on the GUI thread.
UDisks2 asks polkit for authorisation itself when an action needs it,
which is why actions get a long timeout.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import SLOT, QByteArray, QObject, Signal, Slot
from PySide6.QtDBus import (QDBusConnection, QDBusMessage, QDBusObjectPath, QDBusPendingCallWatcher, QDBusSignature,
                            QDBusVariant)

from .volumes import UDISKS_SERVICE

READ_TIMEOUT_MS = 10_000
ACTION_TIMEOUT_MS = 5 * 60_000
_OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
_PROPERTIES = "org.freedesktop.DBus.Properties"


def plain(value: object) -> object:
    """A D-Bus value as a Python value: variants unwrapped, byte arrays as bytes, object paths as text."""
    if isinstance(value, QDBusVariant):
        return plain(value.variant())
    if isinstance(value, QByteArray):
        return bytes(value)
    if isinstance(value, QDBusObjectPath):
        return value.path()
    if isinstance(value, QDBusSignature):
        return value.signature()
    if isinstance(value, (str, int, float, bool, bytes)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value[:4096]]
    return None                                   # a container PySide cannot read


class UDisksBus(QObject):
    """The transport ``volumes.UDisksReader`` and the volume actions use."""

    changed = Signal()                  # a device was added or removed, or a property changed

    def __init__(self, connection: QDBusConnection | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.connection = connection if connection is not None else QDBusConnection.systemBus()
        self._watching = False

    def is_connected(self) -> bool:
        return self.connection.isConnected()

    def watch(self) -> None:
        """Emit ``changed`` when UDisks2 reports a change."""
        if self._watching or not self.is_connected():
            return
        self._watching = True
        for interface, name in ((_OBJECT_MANAGER, "InterfacesAdded"), (_OBJECT_MANAGER, "InterfacesRemoved"),
                                (_PROPERTIES, "PropertiesChanged")):
            self.connection.connect(UDISKS_SERVICE, "", interface, name, self, SLOT("_signal(QDBusMessage)"))

    @Slot(QDBusMessage)
    def _signal(self, _message: QDBusMessage) -> None:
        self.changed.emit()

    def call(self, path: str, interface: str, method: str, arguments: list,
             callback: Callable[[bool, list], None], timeout_ms: int = READ_TIMEOUT_MS) -> None:
        """Call a method of UDisks2; ``callback(ok, values)`` later. Not ok: values are [error name, message]."""
        if not self.is_connected():
            callback(False, ["org.freedesktop.DBus.Error.ServiceUnknown", "The system bus is not available."])
            return
        message = QDBusMessage.createMethodCall(UDISKS_SERVICE, path, interface, method)
        if arguments:
            message.setArguments(arguments)
        watcher = QDBusPendingCallWatcher(self.connection.asyncCall(message, timeout_ms), self)

        def finished(done: QDBusPendingCallWatcher) -> None:
            reply = done.reply()
            done.deleteLater()
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                callback(False, [reply.errorName(), reply.errorMessage()])
            elif reply.type() != QDBusMessage.MessageType.ReplyMessage:
                callback(False, ["org.freedesktop.DBus.Error.NoReply", "No answer."])
            else:
                callback(True, [plain(value) for value in reply.arguments()])

        watcher.finished.connect(finished)

    def action(self, path: str, interface: str, method: str, arguments: list,
               callback: Callable[[bool, list], None]) -> None:
        self.call(path, interface, method, arguments, callback, ACTION_TIMEOUT_MS)
