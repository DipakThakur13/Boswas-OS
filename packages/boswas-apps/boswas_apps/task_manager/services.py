"""The replies of systemd and logind as plain data (standard library only, no Qt, no D-Bus).

``bus.py`` makes the D-Bus calls and hands the unpacked replies (lists,
tuples, dictionaries, strings and numbers) to the functions here, which
check every row and drop what does not have the documented shape. Text
from a unit file or a session is cleaned before it is shown.

systemd: org.freedesktop.systemd1.Manager.ListUnits returns
``a(ssssssouso)`` (name, description, load state, active state, sub state,
followed unit, object path, job ID, job type, job path) and ListUnitFiles
``a(ss)`` (path, state). logind: org.freedesktop.login1.Manager.ListSessions
returns ``a(susso)`` (session ID, user ID, user name, seat ID, object path).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

from .textutil import clean

MAX_UNITS = 20000
SUFFIX = ".service"
_UNIT_NAME = re.compile(r"^[A-Za-z0-9:_.\\@-]{1,247}\.service$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class Unit:
    name: str                # "cups.service"
    description: str
    load: str                # loaded, not-found, masked, error; "" for a unit file that is not loaded
    active: str              # active, inactive, failed, activating, deactivating, reloading
    sub: str                 # running, exited, dead, failed, start, stop, auto-restart, ...
    path: str                # the unit's D-Bus object path; "" when it is not loaded
    scope: str               # "system" or "user"
    enabled: str = ""        # the unit file state: enabled, disabled, static, masked, ...; "" when unknown
    pid: int = 0             # the main process; 0 when there is none

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.name}"

    @property
    def status(self) -> str:
        return status_text(self.active, self.sub)

    @property
    def scope_title(self) -> str:
        return "System" if self.scope == "system" else "User"


def status_text(active: str, sub: str) -> str:
    """The Status column: Running, Exited, Failed, Inactive, Starting, Stopping, Reloading."""
    if active == "active":
        return {"running": "Running", "exited": "Exited"}.get(sub, f"Active ({sub})" if sub else "Active")
    return {"failed": "Failed", "inactive": "Inactive", "activating": "Starting", "deactivating": "Stopping",
            "reloading": "Reloading", "maintenance": "Maintenance"}.get(active, active.capitalize() or "Unknown")


def can_start(unit: Unit) -> bool:
    return unit.active in ("inactive", "failed") and unit.load != "masked" and unit.enabled != "masked"


def can_stop(unit: Unit) -> bool:
    return unit.active in ("active", "activating", "reloading")


def _text(value: object, limit: int) -> str | None:
    return clean(value, limit) if isinstance(value, str) else None


def parse_units(rows: object, scope: str) -> list[Unit]:
    """The services among the rows of ListUnits. Rows of another shape and other unit types are skipped."""
    result: list[Unit] = []
    if not isinstance(rows, (list, tuple)):
        return result
    for row in rows[:MAX_UNITS]:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        name = row[0]
        if not isinstance(name, str) or not _UNIT_NAME.match(name):
            continue
        texts = [_text(value, 512) for value in row[1:5]]
        if None in texts:
            continue
        path = row[6] if isinstance(row[6], str) and row[6].startswith("/") else ""
        result.append(Unit(name, texts[0], texts[1], texts[2], texts[3], path[:512], scope))
    return result


def parse_unit_files(rows: object) -> dict[str, str]:
    """ListUnitFiles as {unit name: state} for service files. Templates (``name@.service``) are left
    out: only their instances can be started."""
    result: dict[str, str] = {}
    if not isinstance(rows, (list, tuple)):
        return result
    for row in rows[:MAX_UNITS]:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        path, state = row[0], row[1]
        if not isinstance(path, str) or not isinstance(state, str):
            continue
        name = os.path.basename(path)
        if _UNIT_NAME.match(name) and "@." not in name and name not in result:
            result[name] = clean(state, 32)
    return result


def merge_units(units: list[Unit], unit_files: dict[str, str], scope: str) -> list[Unit]:
    """Loaded units with their unit file state, plus the installed services that are not loaded (inactive)."""
    result = []
    seen = set()
    for unit in units:
        seen.add(unit.name)
        # An instance of a template has a state only when "name@instance.service" itself is listed; the
        # state of the template is not the state of this instance, so it is left open.
        state = unit_files.get(unit.name, "")
        result.append(replace(unit, enabled=state) if state else unit)
    for name, state in unit_files.items():
        if name not in seen:
            result.append(Unit(name, "", "", "inactive", "dead", "", scope, state))
    result.sort(key=lambda unit: unit.name.casefold())
    return result


# --- object paths and introspection ------------------------------------------------------------------
#
# PySide's QtDBus cannot unpack D-Bus structures, which is what ListUnits, ListUnitFiles and
# ListSessions return. The object tree tells the same: every loaded unit is a child node of
# /org/freedesktop/systemd1/unit and every session one of /org/freedesktop/login1/session, and their
# properties are plain strings and numbers. These helpers turn node names into unit names and back.

UNIT_ROOT = "/org/freedesktop/systemd1/unit"
SESSION_ROOT = "/org/freedesktop/login1/session"
_NODE = re.compile(r'<node\s+name="([A-Za-z0-9_]{1,1024})"\s*/?>')
_ESCAPED = re.compile(r"_([0-9a-fA-F]{2})")
MAX_INTROSPECTION = 8 * 1024 * 1024


def child_nodes(xml: object) -> list[str]:
    """The names of the child nodes in the XML that org.freedesktop.DBus.Introspectable.Introspect returns."""
    if not isinstance(xml, str) or len(xml) > MAX_INTROSPECTION:
        return []
    return _NODE.findall(xml)[:MAX_UNITS]


def path_decode(node: str) -> str:
    """A label of a systemd object path as the name it stands for: ``cups_2eservice`` is ``cups.service``."""
    if node == "_":
        return ""
    return _ESCAPED.sub(lambda match: chr(int(match.group(1), 16)), node)


def path_encode(name: str) -> str:
    """A name as a label of a systemd object path: every byte that is not a letter or a digit is ``_xx``."""
    if not name:
        return "_"
    return "".join(chr(byte) if chr(byte).isascii() and chr(byte).isalnum() else f"_{byte:02x}"
                   for byte in name.encode("utf-8"))


def unit_path(name: str) -> str:
    return f"{UNIT_ROOT}/{path_encode(name)}"


def service_names(nodes: list[str]) -> list[str]:
    """The services among the child nodes of the unit root, as unit names, sorted, without repeats."""
    names = {path_decode(node) for node in nodes if node.endswith("_2eservice")}
    return sorted(name for name in names if _UNIT_NAME.match(name))


def valid_service_name(name: object) -> bool:
    return isinstance(name, str) and bool(_UNIT_NAME.match(name))


def is_service_file(name: str) -> bool:
    """Whether a file name in a unit directory is a service that can be started by that name."""
    return bool(_UNIT_NAME.match(name)) and "@." not in name


def main_pid(value: object) -> int:
    """The MainPID property as an int (0 for anything that is not a sensible PID)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 1 << 22 else 0


# --- logind ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Session:
    id: str                  # "" for the session made up when logind cannot be asked
    uid: int
    user: str
    seat: str
    tty: str
    state: str               # active, online, closing
    since: float             # seconds since the epoch; 0 when unknown
    kind: str                # wayland, x11, tty, unspecified
    cls: str                 # user, greeter, lock-screen, background, manager
    leader: int
    remote: str = ""         # the host a remote session comes from
    path: str = ""

    @property
    def key(self) -> str:
        return f"session:{self.id}:{self.uid}"

    @property
    def place(self) -> str:
        """Seat and terminal in one cell: ``seat0 · tty2``, ``pts/3``, or the remote host."""
        parts = [part for part in (self.seat, self.tty) if part]
        if self.remote:
            parts.append(f"from {self.remote}")
        return " · ".join(parts)

    @property
    def state_title(self) -> str:
        return {"active": "Active", "online": "In the background", "closing": "Signing out"}.get(
            self.state, self.state.capitalize())


def parse_sessions(rows: object) -> list[Session]:
    """The rows of ListSessions as sessions with what that call tells (ID, user, seat, object path)."""
    result: list[Session] = []
    if not isinstance(rows, (list, tuple)):
        return result
    for row in rows[:1024]:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        session_id, uid, user, seat, path = row[:5]
        if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
            continue
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0 or not isinstance(user, str):
            continue
        result.append(Session(session_id, uid, clean(user, 64), clean(seat, 64) if isinstance(seat, str) else "",
                              "", "online", 0.0, "", "user", 0,
                              path=path[:512] if isinstance(path, str) and path.startswith("/") else ""))
    return result


def with_properties(session: Session, properties: object) -> Session:
    """``session`` completed from the properties of its org.freedesktop.login1.Session object."""
    if not isinstance(properties, dict):
        return session

    def text(key: str, default: str = "") -> str:
        value = properties.get(key)
        return clean(value, 128) if isinstance(value, str) else default

    def number(key: str) -> int:
        value = properties.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    since = number("Timestamp") / 1e6
    remote = text("RemoteHost") if properties.get("Remote") is True else ""
    return replace(session, tty=text("TTY"), state=text("State", session.state), since=since,
                   kind=text("Type"), cls=text("Class", session.cls), leader=number("Leader"), remote=remote)


def user_sessions(sessions: list[Session]) -> list[Session]:
    """The sessions of people: logind also lists the greeter and the sessions of user service managers."""
    return [session for session in sessions if session.cls.startswith("user") or not session.cls]
