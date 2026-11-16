"""The D-Bus calls of Task Manager: systemd (both managers) and logind, through QtDBus.

Every function here blocks until the service has answered and is meant to
be called on a worker thread (``fluent.Worker``); nothing here touches a
widget. Failures are ``BusError`` with a text for the user: what the
system said, in its own words.

PySide's QtDBus cannot unpack D-Bus structures, the element type of what
``ListUnits`` and ``ListSessions`` return. The same information is read
another way that needs strings and numbers only: the loaded units and the
sessions are the child nodes of ``/org/freedesktop/systemd1/unit`` and
``/org/freedesktop/login1/session`` (``Introspect``), and what the lists
would tell about each is a handful of properties of that object. The
property reads of one refresh are sent together and answered in one go.
Services that are installed but not loaded are found as files in the
directories of the manager's ``UnitPath`` property and their state asked
with ``GetUnitFileState``.

Starting, stopping and restarting use ``StartUnit``, ``StopUnit`` and
``RestartUnit`` with the mode ``replace`` and with interactive
authorisation allowed, so that polkit asks the user when the action needs
it. The call returns when the job is queued; the unit's state is then
watched until the job has had its effect, and the outcome is reported.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from typing import Callable

from PySide6.QtDBus import QDBus, QDBusConnection, QDBusMessage, QDBusObjectPath, QDBusVariant

from . import services
from .services import Session, Unit
from .textutil import clean

SYSTEMD = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
MANAGER = "org.freedesktop.systemd1.Manager"
UNIT = "org.freedesktop.systemd1.Unit"
SERVICE = "org.freedesktop.systemd1.Service"
LOGIND = "org.freedesktop.login1"
LOGIN_SESSION = "org.freedesktop.login1.Session"
PROPERTIES = "org.freedesktop.DBus.Properties"
INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"

CALL_TIMEOUT_MS = 5000
ACTION_TIMEOUT_MS = 120000           # the user may be typing a password for polkit
SETTLE_SECONDS = 15.0
MAX_UNIT_FILES = 2000
_DYNAMIC = ((UNIT, "ActiveState"), (UNIT, "SubState"), (SERVICE, "MainPID"))
_STATIC = ((UNIT, "Description"), (UNIT, "LoadState"), (UNIT, "UnitFileState"))
_SESSION = ("Id", "Name", "TTY", "State", "Timestamp", "Type", "Class", "Leader", "Remote", "RemoteHost", "VTNr")
_REFUSALS = {
    "org.freedesktop.DBus.Error.AccessDenied": "Access denied",
    "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired": "Authorisation is required",
    "org.freedesktop.PolicyKit1.Error.NotAuthorized": "Not authorised",
    "org.freedesktop.PolicyKit1.Error.Cancelled": "The authorisation was cancelled",
}


class BusError(Exception):
    """A D-Bus call failed. ``name`` is the D-Bus error name; the text is for the user."""

    def __init__(self, message: str, name: str = "") -> None:
        super().__init__(message)
        self.name = name


def unwrap(value: object) -> object:
    """A reply value as plain Python: variants and object paths unwrapped, lists and dictionaries walked."""
    if isinstance(value, QDBusVariant):
        return unwrap(value.variant())
    if isinstance(value, QDBusObjectPath):
        return value.path()
    if isinstance(value, (list, tuple)):
        return [unwrap(item) for item in value]
    if isinstance(value, dict):
        return {unwrap(key): unwrap(item) for key, item in value.items()}
    return value


class Bus:
    """One of the two message buses."""

    def __init__(self, kind: str) -> None:
        self.kind = kind                         # "system" or "session"
        self._connection: QDBusConnection | None = None

    def connection(self) -> QDBusConnection:
        if self._connection is None or not self._connection.isConnected():
            if self.kind == "system":
                self._connection = QDBusConnection.systemBus()
            else:
                if not (os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("XDG_RUNTIME_DIR")):
                    raise BusError("There is no session message bus in this session.")
                self._connection = QDBusConnection.sessionBus()
        if not self._connection.isConnected():
            reason = self._connection.lastError().message()
            which = "system" if self.kind == "system" else "session"
            raise BusError(f"The {which} message bus cannot be reached" + (f": {reason}" if reason else "."))
        return self._connection

    @staticmethod
    def _message(service: str, path: str, interface: str, member: str, arguments: tuple) -> QDBusMessage:
        message = QDBusMessage.createMethodCall(service, path, interface, member)
        if arguments:
            message.setArguments(list(arguments))
        return message

    @staticmethod
    def _result(reply: QDBusMessage, what: str) -> list:
        if reply.type() == QDBusMessage.MessageType.ReplyMessage:
            return [unwrap(value) for value in reply.arguments()]
        name = reply.errorName()
        text = reply.errorMessage() or _REFUSALS.get(name, "") or name or "no answer"
        if name in _REFUSALS and _REFUSALS[name].lower() not in text.lower():
            text = f"{_REFUSALS[name]}: {text}"
        raise BusError(f"{what}: {text.rstrip('.')}.", name)

    def call(self, service: str, path: str, interface: str, member: str, *arguments: object,
             timeout_ms: int = CALL_TIMEOUT_MS, interactive: bool = False, what: str = "") -> list:
        """A method call; returns the reply's values. Raises BusError with what the service answered."""
        message = self._message(service, path, interface, member, arguments)
        if interactive:
            message.setInteractiveAuthorizationAllowed(True)
        reply = self.connection().call(message, QDBus.CallMode.Block, timeout_ms)
        return self._result(reply, what or f"{member} was refused")

    def get_many(self, service: str, requests: list[tuple[str, str, str]]) -> list[object]:
        """Many property reads at once: (path, interface, property) each. A read that failed gives None."""
        connection = self.connection()
        pending = []
        for path, interface, name in requests:
            message = self._message(service, path, PROPERTIES, "Get", (interface, name))
            pending.append(connection.asyncCall(message, CALL_TIMEOUT_MS))
        results: list[object] = []
        for call in pending:
            call.waitForFinished()
            reply = call.reply()
            values = reply.arguments() if reply.type() == QDBusMessage.MessageType.ReplyMessage else []
            results.append(unwrap(values[0]) if values else None)
        return results

    def children(self, service: str, path: str) -> list[str]:
        """The names of the child objects of ``path``."""
        values = self.call(service, path, INTROSPECTABLE, "Introspect", what="The service did not describe itself")
        return services.child_nodes(values[0] if values else "")

    def has_service(self, service: str) -> bool:
        try:
            values = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                               "NameHasOwner", service)
        except BusError:
            return False
        return bool(values and values[0] is True)


class SystemdClient:
    """The services of one systemd manager: the system's (``system``) or the user's own (``user``)."""

    def __init__(self, scope: str, bus: Bus | None = None, *, listdir: Callable[[str], list[str]] = os.listdir,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.scope = scope
        self.bus = bus or Bus("system" if scope == "system" else "session")
        self._listdir = listdir
        self._sleep = sleep
        self._static: dict[str, tuple[str, str, str]] = {}       # name -> description, load state, file state
        self._files: dict[str, tuple[str, str]] | None = None      # not loaded: name -> file state, description

    def list_services(self, full: bool = False) -> list[Unit]:
        """Every service the manager knows, loaded or only installed. ``full`` reads again what is
        otherwise remembered between calls: descriptions, enabled states and the installed files."""
        names = services.service_names(self.bus.children(SYSTEMD, services.UNIT_ROOT))
        if full:
            self._static.clear()
            self._files = None
        requests = []
        for name in names:
            path = services.unit_path(name)
            wanted = _DYNAMIC if name in self._static else _DYNAMIC + _STATIC
            requests += [(path, interface, prop) for interface, prop in wanted]
        values = iter(self.bus.get_many(SYSTEMD, requests))
        rows, pids, enabled = [], {}, {}
        for name in names:
            active, sub, pid = next(values), next(values), next(values)
            if name not in self._static:
                description, load, state = next(values), next(values), next(values)
                if isinstance(description, str) and isinstance(load, str):
                    self._static[name] = (description, load, state if isinstance(state, str) else "")
            description, load, state = self._static.get(name, ("", "", ""))
            rows.append((name, description, load, active, sub, "", services.unit_path(name)))
            pids[name] = services.main_pid(pid)
            enabled[name] = state
        units = [replace(unit, pid=pids.get(unit.name, 0), enabled=clean(enabled.get(unit.name, ""), 32))
                 for unit in services.parse_units(rows, self.scope)]
        if self._files is None:
            self._files = self._unit_files(set(names))
        loaded = {unit.name for unit in units}
        files = {name: state for name, (state, _text) in self._files.items() if name not in loaded}
        # A service that was loaded a moment ago and has been unloaded since is still installed.
        for name, (_description, _load, state) in self._static.items():
            if name not in loaded and name not in files and state not in ("", "transient", "generated"):
                files[name] = state
        merged = services.merge_units(units, files, self.scope)
        for index, unit in enumerate(merged):
            if not unit.description:
                text = self._static.get(unit.name, ("",))[0] or self._files.get(unit.name, ("", ""))[1]
                if text:
                    merged[index] = replace(unit, description=clean(text, 512))
        return merged

    def _unit_files(self, loaded: set[str]) -> dict[str, tuple[str, str]]:
        """The installed services that are not loaded: their unit file state and, read from the unit
        file itself, their description."""
        try:
            directories = self.bus.call(SYSTEMD, SYSTEMD_PATH, PROPERTIES, "Get", MANAGER, "UnitPath")[0]
        except (BusError, IndexError):
            return {}
        found: list[str] = []
        where: dict[str, str] = {}
        seen = set(loaded)
        for directory in directories if isinstance(directories, list) else []:
            if not isinstance(directory, str) or not directory.startswith("/"):
                continue
            try:
                entries = sorted(self._listdir(directory))
            except OSError:
                continue
            for entry in entries:
                if entry not in seen and services.is_service_file(entry) and len(found) < MAX_UNIT_FILES:
                    seen.add(entry)
                    found.append(entry)
                    where[entry] = os.path.join(directory, entry)
        connection = self.bus.connection()
        pending = [connection.asyncCall(Bus._message(SYSTEMD, SYSTEMD_PATH, MANAGER, "GetUnitFileState", (name,)),
                                        CALL_TIMEOUT_MS) for name in found]
        result = {}
        for name, call in zip(found, pending):
            call.waitForFinished()
            reply = call.reply()
            values = reply.arguments() if reply.type() == QDBusMessage.MessageType.ReplyMessage else []
            state = unwrap(values[0]) if values else None
            if isinstance(state, str) and state != "alias":          # another name of a unit listed already
                result[name] = (clean(state, 32), _description_of(where[name]))
        return result

    def _state(self, name: str) -> tuple[str, str, str]:
        path = services.unit_path(name)
        active, sub, result = self.bus.get_many(SYSTEMD, [(path, UNIT, "ActiveState"), (path, UNIT, "SubState"),
                                                           (path, SERVICE, "Result")])
        return (active if isinstance(active, str) else "", sub if isinstance(sub, str) else "",
                result if isinstance(result, str) else "")

    def act(self, action: str, name: str) -> str:
        """``start``, ``stop`` or ``restart`` a service; returns a sentence about the outcome.

        Raises BusError when systemd (or polkit) refuses or the service ends up failed.
        """
        member = {"start": "StartUnit", "stop": "StopUnit", "restart": "RestartUnit"}[action]
        doing = {"start": "started", "stop": "stopped", "restart": "restarted"}[action]
        if not services.valid_service_name(name):
            raise BusError(f"“{name}” is not the name of a service.")
        self.bus.call(SYSTEMD, SYSTEMD_PATH, MANAGER, member, name, "replace", timeout_ms=ACTION_TIMEOUT_MS,
                      interactive=True, what=f"The system refused to {action} {name}")
        began = time.monotonic()
        active = sub = result = ""
        while True:
            active, sub, result = self._state(name)
            elapsed = time.monotonic() - began
            if action == "stop":
                done = active in ("inactive", "failed", "")
            else:
                done = active in ("active", "failed") or (active in ("inactive", "") and elapsed > 1.5)
            if done or elapsed > SETTLE_SECONDS:
                break
            self._sleep(0.15)
        if action != "stop" and active == "active" and time.monotonic() - began < 0.5:
            # A program that cannot run at all is "active" for an instant before it is "failed".
            self._sleep(0.3)
            active, sub, result = self._state(name)
        if active == "failed" or (action != "stop" and result not in ("", "success")):
            raise BusError(f"{name} was {doing} but failed" + (f" ({result})." if result else "."), "failed")
        if action == "stop":
            return f"{name} was stopped." if active in ("inactive", "") else f"{name} is still stopping."
        if active == "active":
            if sub == "running":
                return f"{name} was {doing} and is running."
            if sub == "exited":
                return f"{name} was {doing}; it ran and finished successfully."
            return f"{name} was {doing} ({sub})."
        if active in ("inactive", ""):
            return f"{name} was {doing}; it ran and has finished."
        return f"{name} is still starting."


class LogindClient:
    """The sessions logind knows."""

    def __init__(self, bus: Bus | None = None, uid_of: Callable[[str], int | None] | None = None) -> None:
        self.bus = bus or Bus("system")
        self._uid_of = uid_of or _uid_of_name

    def list_sessions(self) -> list[Session]:
        """The sessions with user, terminal, state and start time. Raises BusError without logind.

        The seat is ``seat0`` for a session on a virtual terminal and unknown otherwise: logind tells
        it as a structure, which cannot be read here (see the module text).
        """
        nodes = [node for node in self.bus.children(LOGIND, services.SESSION_ROOT) if node not in ("auto", "self")]
        requests = [(f"{services.SESSION_ROOT}/{node}", LOGIN_SESSION, name) for node in nodes for name in _SESSION]
        values = iter(self.bus.get_many(LOGIND, requests))
        result = []
        for node in nodes:
            properties = {name: next(values) for name in _SESSION}
            name = properties.get("Name")
            session_id = properties.get("Id")
            if not isinstance(name, str) or not isinstance(session_id, str):
                continue
            uid = self._uid_of(name)
            if uid is None:
                continue
            seat = "seat0" if isinstance(properties.get("VTNr"), int) and properties["VTNr"] > 0 else ""
            parsed = services.parse_sessions([(session_id, uid, name, seat, f"{services.SESSION_ROOT}/{node}")])
            if parsed:
                result.append(services.with_properties(parsed[0], properties))
        result.sort(key=lambda session: (session.since, session.id))
        return result


def _description_of(path: str) -> str:
    """The ``Description=`` of a unit file ("" when the file cannot be read or has none)."""
    try:
        with open(path, "rb") as handle:
            text = handle.read(65536).decode("utf-8", "replace")
    except OSError:
        return ""
    for line in text.split("\n"):
        if line.startswith("Description="):
            return clean(line[12:].strip(), 512)
    return ""


def _uid_of_name(name: str) -> int | None:
    try:
        import pwd
        return pwd.getpwnam(name).pw_uid
    except (ImportError, KeyError, OSError):
        return None
