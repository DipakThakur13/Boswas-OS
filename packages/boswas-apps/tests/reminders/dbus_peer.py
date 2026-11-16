"""A stand-in notification service for the D-Bus test: run as a program on a private session bus.

It owns org.freedesktop.Notifications, writes every call it receives as a
line of JSON to the file named by its first argument, and obeys commands on
standard input: ``action <id> <key>``, ``close <id> <reason>``, ``quit``.
"""

import json
import sys

from PySide6.QtCore import ClassInfo, QCoreApplication, QObject, QSocketNotifier, Signal, Slot
from PySide6.QtDBus import QDBusArgument, QDBusConnection


@ClassInfo({"D-Bus Interface": "org.freedesktop.Notifications"})
class Notifications(QObject):
    ActionInvoked = Signal("uint", str)
    NotificationClosed = Signal("uint", "uint")

    def __init__(self, log_path: str) -> None:
        super().__init__()
        self.log_path = log_path
        self.count = 0

    def log(self, entry: dict) -> None:
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    @Slot(str, "uint", str, str, str, "QStringList", "QVariantMap", int, result="uint")
    def Notify(self, app, replaces, icon, summary, body, actions, hints, timeout):
        self.count += 1
        identifier = int(replaces) or self.count
        plain = {key: (value.currentSignature() if isinstance(value, QDBusArgument) else value)
                 for key, value in hints.items()}
        self.log({"call": "Notify", "id": identifier, "app": app, "replaces": int(replaces), "icon": icon,
                  "summary": summary, "body": body, "actions": list(actions), "hints": plain, "timeout": timeout})
        return identifier

    @Slot("uint")
    def CloseNotification(self, identifier):
        self.log({"call": "CloseNotification", "id": int(identifier)})

    @Slot(result="QStringList")
    def GetCapabilities(self):
        return ["actions", "body", "persistence"]


def main() -> int:
    app = QCoreApplication(sys.argv)
    bus = QDBusConnection.sessionBus()
    service = Notifications(sys.argv[1])
    options = QDBusConnection.RegisterOption.ExportAllSlots | QDBusConnection.RegisterOption.ExportAllSignals
    if not bus.registerObject("/org/freedesktop/Notifications", service, options):
        return 3
    if not bus.registerService("org.freedesktop.Notifications"):
        return 4
    service.log({"call": "ready"})
    reader = QSocketNotifier(sys.stdin.fileno(), QSocketNotifier.Type.Read, app)

    def command() -> None:
        line = sys.stdin.readline()
        if not line:
            app.quit()
            return
        words = line.split()
        if words[:1] == ["action"]:
            service.ActionInvoked.emit(int(words[1]), words[2])
            service.NotificationClosed.emit(int(words[1]), 2)
        elif words[:1] == ["close"]:
            service.NotificationClosed.emit(int(words[1]), int(words[2]))
        elif words[:1] == ["quit"]:
            app.quit()

    reader.activated.connect(command)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
