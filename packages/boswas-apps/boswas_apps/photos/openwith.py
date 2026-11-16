"""Other applications that open a file type: desktop entries read the freedesktop.org way (standard library only).

``applications_for("image/png")`` lists the installed applications whose
desktop entry names the type. ``command(app, files)`` turns the ``Exec``
line of an entry into an argument list as the Desktop Entry Specification
describes (quoting, field codes); nothing is ever given to a shell.
Entries are files other programs installed, so they are read with limits
and anything odd makes an entry be skipped, not trusted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_ENTRY_BYTES = 256 * 1024
MAX_ENTRIES = 4000
_FILE_CODES = ("%f", "%F", "%u", "%U")
_DROPPED_CODES = ("%d", "%D", "%n", "%N", "%v", "%m")
_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}


@dataclass(frozen=True)
class Application:
    entry_id: str            # "org.kde.gwenview.desktop"
    name: str
    icon: str
    exec_line: str
    path: str


def data_dirs() -> list[Path]:
    """The XDG data directories, the user's first."""
    home = os.environ.get("XDG_DATA_HOME") or ""
    dirs = [Path(home) if home.startswith("/") else Path.home() / ".local" / "share"]
    for directory in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if directory.startswith("/") and Path(directory) not in dirs:
            dirs.append(Path(directory))
    return dirs


def _unescape(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value) and value[i + 1] in _ESCAPES:
            out.append(_ESCAPES[value[i + 1]])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_entry(text: str) -> dict[str, str]:
    """The keys of the ``[Desktop Entry]`` group (unlocalised keys only)."""
    keys: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if inside:
                break
            inside = line == "[Desktop Entry]"
            continue
        if not inside or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and "[" not in key and key not in keys:
            keys[key] = _unescape(value.strip())
    return keys


def split_exec(exec_line: str) -> list[str]:
    """The ``Exec`` value as arguments (quotes resolved, field codes still in place). ValueError when malformed."""
    arguments: list[str] = []
    current: list[str] = []
    started = False
    i = 0
    while i < len(exec_line):
        ch = exec_line[i]
        if ch == '"':
            started = True
            i += 1
            while True:
                if i >= len(exec_line):
                    raise ValueError("unterminated quote in Exec")
                ch = exec_line[i]
                if ch == '"':
                    break
                if ch == "\\" and i + 1 < len(exec_line) and exec_line[i + 1] in '"`$\\':
                    current.append(exec_line[i + 1])
                    i += 2
                    continue
                current.append(ch)
                i += 1
            i += 1
        elif ch in " \t":
            if started:
                arguments.append("".join(current))
                current, started = [], False
            i += 1
        else:
            started = True
            current.append(ch)
            i += 1
    if started:
        arguments.append("".join(current))
    return arguments


def command(app: Application, files: list[str]) -> list[str] | None:
    """The argument list that opens ``files`` with ``app``; None when the entry cannot take a file."""
    try:
        template = split_exec(app.exec_line)
    except ValueError:
        return None
    if not template or not files:
        return None
    arguments: list[str] = []
    takes_files = False
    for part in template:
        if part in ("%f", "%u"):
            arguments.append(files[0])
            takes_files = True
        elif part in ("%F", "%U"):
            arguments.extend(files)
            takes_files = True
        elif part == "%i":
            if app.icon:
                arguments.extend(["--icon", app.icon])
        elif part == "%c":
            arguments.append(app.name)
        elif part == "%k":
            arguments.append(app.path)
        elif part in _DROPPED_CODES:
            continue
        else:
            text = part.replace("%%", "\x00")
            if "%" in text:
                # A field code inside a larger argument: only the single-file ones have a meaning there.
                replaced = text
                for code in ("%f", "%u"):
                    if code in replaced:
                        replaced = replaced.replace(code, files[0])
                        takes_files = True
                if "%" in replaced:
                    return None
                text = replaced
            arguments.append(text.replace("\x00", "%"))
    if not takes_files or not arguments:
        return None
    program = arguments[0]
    resolved = program if os.path.isabs(program) else shutil.which(program)
    if not resolved or not os.access(resolved, os.X_OK):
        return None
    return [resolved, *arguments[1:]]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _read_application(path: Path, entry_id: str) -> tuple[Application, list[str]] | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_ENTRY_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_ENTRY_BYTES:
        return None
    keys = parse_entry(raw.decode("utf-8", "replace"))
    if keys.get("Type") != "Application" or not keys.get("Exec") or not keys.get("Name"):
        return None
    if _truthy(keys.get("Hidden")) or _truthy(keys.get("NoDisplay")) or _truthy(keys.get("Terminal")):
        return None
    attempt = keys.get("TryExec")
    if attempt and not (os.access(attempt, os.X_OK) if os.path.isabs(attempt) else shutil.which(attempt)):
        return None
    types = [item.strip().lower() for item in keys.get("MimeType", "").split(";") if item.strip()]
    name = " ".join(keys["Name"].split())[:80]
    return Application(entry_id, name, keys.get("Icon", "")[:128], keys["Exec"], str(path)), types


def _default_ids(mime: str, config_dirs: list[Path]) -> list[str]:
    for directory in config_dirs:
        try:
            with open(directory / "mimeapps.list", "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(MAX_ENTRY_BYTES)
        except OSError:
            continue
        inside = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                inside = line == "[Default Applications]"
            elif inside and "=" in line:
                key, value = line.split("=", 1)
                if key.strip().lower() == mime:
                    return [item.strip() for item in value.split(";") if item.strip()]
    return []


def applications_for(mime: str, *, exclude: tuple[str, ...] = (), directories: list[Path] | None = None,
                     config_dirs: list[Path] | None = None) -> list[Application]:
    """The applications that say they open ``mime``, the user's default first, then by name.

    ``exclude`` are desktop file IDs to leave out (this application itself).
    """
    mime = mime.strip().lower()
    found: dict[str, Application] = {}
    seen: set[str] = set()
    count = 0
    for directory in (directories if directories is not None else data_dirs()):
        base = directory / "applications"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop") or name in seen:
                continue
            count += 1
            if count > MAX_ENTRIES:
                break
            seen.add(name)                    # an entry in an earlier directory hides later ones of that ID
            read = _read_application(base / name, name)
            if read is None:
                continue
            application, types = read
            if mime in types and name not in exclude and name.removesuffix(".desktop") not in exclude:
                found[name] = application
    if config_dirs is None:
        home = os.environ.get("XDG_CONFIG_HOME") or ""
        config_dirs = [Path(home) if home.startswith("/") else Path.home() / ".config"]
        config_dirs += [directory / "applications" for directory in data_dirs()]
    defaults = _default_ids(mime, config_dirs)
    ordered = [found.pop(entry_id) for entry_id in defaults if entry_id in found]
    return ordered + sorted(found.values(), key=lambda app: app.name.casefold())


def launch(arguments: list[str]) -> bool:
    """Start a program with an argument list, detached from this one. True when it was started."""
    if not arguments or not os.path.isabs(arguments[0]):
        return False
    try:
        subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return False
    return True
