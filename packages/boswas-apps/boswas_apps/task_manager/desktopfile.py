"""Desktop entries: the installed applications and the programs that start with the session.

Standard library only. Three things live here:

* reading ``.desktop`` files the way the Desktop Entry Specification says
  (groups, localized keys, escapes, the quoting rules and field codes of
  ``Exec``);
* the list of installed applications from ``applications`` in the XDG data
  directories, and ``AppIndex``, which tells which application a running
  program belongs to;
* XDG autostart (Desktop Application Autostart Specification): listing
  the entries of ``autostart`` in the XDG configuration directories with
  ``Hidden``, ``OnlyShowIn``, ``NotShowIn`` and ``TryExec`` honoured, and
  switching one on or off by writing the user's own file of the same name
  with ``Hidden=false`` or ``Hidden=true``.

Files are read with a size cap and written only into the user's autostart
directory, under a name that was checked, by writing a temporary file and
renaming it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable, Mapping

from .textutil import clean

MAX_ENTRY_BYTES = 256 * 1024
MAX_FILES = 5000
DEFAULT_DESKTOP = "Boswas"
MAIN = "Desktop Entry"
_FIELD_CODES = "fFuUdDnNickvm"
_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\", ";": ";"}
_VENDORS = {
    "kde": "KDE", "gnome": "GNOME", "boswas": "Boswas", "mozilla": "Mozilla", "freedesktop": "freedesktop.org",
    "libreoffice": "LibreOffice", "xfce": "Xfce", "videolan": "VideoLAN", "gimp": "GIMP", "inkscape": "Inkscape",
    "chromium": "Chromium", "google": "Google", "debian": "Debian", "gtk": "GTK", "qt": "Qt",
}


class StartupError(Exception):
    """A change to the startup apps could not be made; the text is for the user."""


def unescape(value: str) -> str:
    """The escapes of a string value: ``\\s`` ``\\n`` ``\\t`` ``\\r`` ``\\\\`` (and ``\\;`` in lists)."""
    if "\\" not in value:
        return value
    out = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in _ESCAPES:
            out.append(_ESCAPES[value[index + 1]])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def split_list(value: str) -> list[str]:
    """A list value: items end with ``;``, a ``;`` inside an item is written ``\\;``."""
    items, current, index = [], [], 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            current.append(_ESCAPES.get(value[index + 1], "\\" + value[index + 1]))
            index += 2
            continue
        if char == ";":
            items.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if current:
        items.append("".join(current))
    return [item for item in items if item]


def locale_candidates(env: Mapping[str, str]) -> list[str]:
    """The locale suffixes to try for a localized key, best first (``de_AT@euro``, ``de_AT``, ``de@euro``, ``de``)."""
    locale = env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG") or ""
    base, _, modifier = locale.partition("@")
    base = base.split(".", 1)[0]                 # without the encoding
    if not base or base in ("C", "POSIX"):
        return []
    language, _, country = base.partition("_")
    candidates = []
    if country and modifier:
        candidates.append(f"{language}_{country}@{modifier}")
    if country:
        candidates.append(f"{language}_{country}")
    if modifier:
        candidates.append(f"{language}@{modifier}")
    candidates.append(language)
    return candidates


class DesktopEntry:
    """One parsed ``.desktop`` file. Values are returned unescaped; a missing key is None."""

    def __init__(self, text: str, path: str = "") -> None:
        self.path = path
        self.groups: dict[str, dict[str, str]] = {}
        group: dict[str, str] | None = None
        for line in text.split("\n"):
            line = line.strip()
            if not line or line[0] == "#":
                continue
            if line[0] == "[" and line[-1] == "]":
                group = self.groups.setdefault(line[1:-1], {})
                continue
            key, equals, value = line.partition("=")
            if group is None or not equals:
                continue
            key = key.rstrip()
            if key and key not in group:           # the first of two equal keys counts
                group[key] = value.lstrip()

    @classmethod
    def load(cls, path: str) -> "DesktopEntry | None":
        """Read ``path``; None when it cannot be read, is too large or has no ``[Desktop Entry]`` group."""
        text = read_entry_text(path)
        if text is None:
            return None
        entry = cls(text, path)
        return entry if MAIN in entry.groups else None

    def raw(self, key: str, group: str = MAIN) -> str | None:
        return self.groups.get(group, {}).get(key)

    def get(self, key: str, locales: list[str] | tuple[str, ...] = (), group: str = MAIN) -> str | None:
        values = self.groups.get(group, {})
        for locale in locales:
            if f"{key}[{locale}]" in values:
                return unescape(values[f"{key}[{locale}]"])
        value = values.get(key)
        return None if value is None else unescape(value)

    def boolean(self, key: str, default: bool = False) -> bool:
        value = self.raw(key)
        if value is None:
            return default
        return value.strip().lower() == "true"

    def list(self, key: str) -> list[str]:
        value = self.raw(key)
        return split_list(value) if value else []


def read_entry_text(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ENTRY_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_ENTRY_BYTES or b"\0" in data:
        return None
    return data.decode("utf-8", "replace")


def exec_argv(value: str | None) -> list[str]:
    """The ``Exec`` value as an argument list without field codes.

    Arguments are separated by spaces; an argument in double quotes may
    contain spaces, and inside the quotes a backslash escapes ``"``, `````,
    ``$`` and ``\\``. Field codes (``%f``, ``%U``, ``%i`` ...) are removed,
    ``%%`` is a percent sign.
    """
    if not value:
        return []
    arguments: list[tuple[str, bool]] = []
    current: list[str] = []
    quoted = started = was_quoted = False
    index = 0
    while index < len(value):
        char = value[index]
        if quoted:
            if char == "\\" and index + 1 < len(value) and value[index + 1] in '"`$\\':
                current.append(value[index + 1])
                index += 2
                continue
            if char == '"':
                quoted = False
            else:
                current.append(char)
        elif char == '"':
            quoted = started = was_quoted = True
        elif char in " \t\n":
            if started or current:
                arguments.append(("".join(current), was_quoted))
            current, started, was_quoted = [], False, False
        else:
            current.append(char)
        index += 1
    if started or current:
        arguments.append(("".join(current), was_quoted))
    result = []
    for argument, was_quoted in arguments:
        if "%" not in argument:
            result.append(argument)
            continue
        out, had_code, position = [], False, 0
        while position < len(argument):
            char = argument[position]
            if char == "%" and position + 1 < len(argument):
                following = argument[position + 1]
                if following == "%":
                    out.append("%")
                elif following in _FIELD_CODES:
                    had_code = True
                else:
                    out.append(char + following)
                position += 2
                continue
            out.append(char)
            position += 1
        text = "".join(out)
        if text or (was_quoted and not had_code):
            result.append(text)
    return result


def program_of(argv: list[str]) -> str:
    """The name of the program an argument list starts (after ``env VAR=value``), without directory."""
    index = 0
    if argv and os.path.basename(argv[0]) == "env":
        index = 1
        while index < len(argv) and ("=" in argv[index] or argv[index].startswith("-")):
            index += 1
    return os.path.basename(argv[index]) if index < len(argv) else ""


# --- directories -------------------------------------------------------------------------------------

def _home(env: Mapping[str, str]) -> str:
    return env.get("HOME") or os.path.expanduser("~")


def _absolute(paths: str) -> list[str]:
    result = []
    for path in paths.split(":"):
        if path.startswith("/") and path not in result:
            result.append(path)
    return result


def data_dirs(env: Mapping[str, str]) -> list[str]:
    home = env.get("XDG_DATA_HOME") or ""
    first = home if home.startswith("/") else os.path.join(_home(env), ".local/share")
    return [first, *[path for path in _absolute(env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share")
                     if path != first]]


def config_home(env: Mapping[str, str]) -> str:
    home = env.get("XDG_CONFIG_HOME") or ""
    return home if home.startswith("/") else os.path.join(_home(env), ".config")


def autostart_dirs(env: Mapping[str, str]) -> tuple[str, list[str]]:
    """(the user's autostart directory, the system ones in order of precedence)."""
    user = os.path.join(config_home(env), "autostart")
    system = [os.path.join(path, "autostart") for path in _absolute(env.get("XDG_CONFIG_DIRS") or "/etc/xdg")]
    return user, [path for path in system if path != user]


def current_desktops(env: Mapping[str, str]) -> list[str]:
    """The names this desktop goes by in ``OnlyShowIn`` and ``NotShowIn``: ``$XDG_CURRENT_DESKTOP``, else Boswas."""
    names = [name for name in (env.get("XDG_CURRENT_DESKTOP") or "").split(":") if name]
    return names or [DEFAULT_DESKTOP]


def shown_in(only: list[str], never: list[str], desktops: list[str]) -> bool:
    """Whether an entry with these ``OnlyShowIn`` and ``NotShowIn`` lists applies to ``desktops``."""
    if only and not any(name in only for name in desktops):
        return False
    return not any(name in never for name in desktops)


# --- applications ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Application:
    app_id: str              # the desktop file ID: "org.kde.kate.desktop"
    name: str
    icon: str
    program: str             # what Exec starts, without directory
    wm_class: str
    path: str
    comment: str = ""
    visible: bool = True     # shown in menus: not NoDisplay, applies to this desktop
    terminal: bool = False
    arguments: int = 0       # how many arguments Exec gives the program


def _desktop_files(directory: str, limit: int) -> list[tuple[str, str]]:
    """(ID, path) of the desktop files under ``directory``; sub-directories join the ID with ``-``."""
    found: list[tuple[str, str]] = []
    stack = [("", directory, 0)]
    while stack and len(found) < limit:
        prefix, path, depth = stack.pop()
        try:
            names = sorted(os.listdir(path))
        except OSError:
            continue
        for name in names:
            full = os.path.join(path, name)
            if name.endswith(".desktop"):
                found.append((prefix + name, full))
            elif depth < 3 and not name.startswith(".") and os.path.isdir(full):
                stack.append((f"{prefix}{name}-", full, depth + 1))
    return found


def list_applications(env: Mapping[str, str]) -> list[Application]:
    """The installed applications (``Type=Application`` with an ``Exec``), sorted by name.

    Of two files with the same ID the one in the more important directory
    counts; when that one says ``Hidden=true`` the application is gone.
    """
    locales = locale_candidates(env)
    desktops = current_desktops(env)
    seen: set[str] = set()
    result = []
    for directory in data_dirs(env):
        for app_id, path in _desktop_files(os.path.join(directory, "applications"), MAX_FILES - len(seen)):
            if app_id in seen:
                continue
            seen.add(app_id)
            entry = DesktopEntry.load(path)
            if entry is None or entry.boolean("Hidden") or (entry.raw("Type") or "Application") != "Application":
                continue
            argv = exec_argv(entry.get("Exec"))
            program = program_of(argv)
            if not program:
                continue
            visible = not entry.boolean("NoDisplay") and shown_in(entry.list("OnlyShowIn"),
                                                                    entry.list("NotShowIn"), desktops)
            result.append(Application(
                app_id, clean(entry.get("Name", locales) or app_id[:-8], 120), clean(entry.get("Icon") or "", 256),
                program, clean(entry.get("StartupWMClass") or "", 120), path,
                clean(entry.get("Comment", locales) or entry.get("GenericName", locales) or "", 200), visible,
                entry.boolean("Terminal"), max(0, len(argv) - 1)))
    result.sort(key=lambda app: (app.name.casefold(), app.app_id))
    return result


class AppIndex:
    """Which application a running program is.

    A program belongs to an application when one of its names (see
    ``sampler.program_names``) equals, without regard to case, the program
    the application's ``Exec`` starts or its ``StartupWMClass``. Only
    applications that show a window of their own are considered: visible in
    menus and not ``Terminal=true``. Of several applications that start the
    same program, the one whose ``Exec`` has the fewest arguments wins.
    """

    def __init__(self, applications: list[Application]) -> None:
        self._by_name: dict[str, Application] = {}
        for app in sorted(applications, key=lambda app: (app.arguments, app.app_id)):
            if not app.visible or app.terminal:
                continue
            for name in (app.program, app.wm_class):
                name = name.lower()
                if name and name not in self._by_name:
                    self._by_name[name] = app

    def __len__(self) -> int:
        return len(self._by_name)

    def match(self, names: list[str]) -> Application | None:
        for name in names:
            app = self._by_name.get(name)
            if app is not None:
                return app
        return None


# --- autostart ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StartupEntry:
    file_id: str             # "foo.desktop"
    name: str
    publisher: str
    icon: str
    command: str
    enabled: bool
    source: str              # "User": only in the user's directory; "System": installed for everybody
    path: str                # the file that decides
    user_path: str | None    # the user's file, when there is one
    system_path: str | None  # the system's file, when there is one


def valid_file_id(file_id: str) -> bool:
    return (isinstance(file_id, str) and file_id.endswith(".desktop") and 8 < len(file_id) <= 255
            and "/" not in file_id and "\0" not in file_id and not file_id.startswith("."))


def publisher_of(entry_values: Callable[[str], str | None], file_id: str) -> str:
    """Who made a startup entry. Desktop entries have no key for that, so: ``X-Publisher`` or
    ``X-Vendor`` when a file has one, else the organisation in a reverse-DNS file name
    (``org.kde.kate.desktop`` is from KDE); empty when neither tells."""
    for key in ("X-Publisher", "X-Vendor", "X-AppStream-Developer"):
        value = entry_values(key)
        if value:
            return clean(value, 80)
    parts = file_id[:-8].split(".")
    if len(parts) >= 3 and parts[0] in ("org", "com", "net", "io", "de", "uk", "app", "im", "dev") and parts[1]:
        return _VENDORS.get(parts[1].lower(), parts[1][:1].upper() + parts[1][1:])
    return ""


def list_startup(env: Mapping[str, str], which: Callable[[str], str | None] = shutil.which) -> list[StartupEntry]:
    """The programs that start with the session on this desktop, with their state. See the module text."""
    user_dir, system_dirs = autostart_dirs(env)
    locales = locale_candidates(env)
    desktops = current_desktops(env)
    files: dict[str, list[str]] = {}
    for directory in (user_dir, *system_dirs):
        try:
            names = sorted(os.listdir(directory))[:MAX_FILES]
        except OSError:
            continue
        for name in names:
            if valid_file_id(name):
                files.setdefault(name, []).append(os.path.join(directory, name))
    result = []
    for file_id, paths in files.items():
        entries = [entry for entry in (DesktopEntry.load(path) for path in paths) if entry is not None]
        if not entries:
            continue
        winner = entries[0]

        def value(key: str, localized: bool = False) -> str | None:
            for entry in entries:
                found = entry.get(key, locales if localized else ())
                if found is not None:
                    return found
            return None

        def items(key: str) -> list[str]:
            for entry in entries:
                if entry.raw(key) is not None:
                    return entry.list(key)
            return []

        if (value("Type") or "Application") != "Application":
            continue
        command = value("Exec")
        if not exec_argv(command):
            continue
        if not shown_in(items("OnlyShowIn"), items("NotShowIn"), desktops):
            continue
        try_exec = value("TryExec")
        if try_exec and not _executable(try_exec, which):
            continue
        enabled = not winner.boolean("Hidden")
        if (winner.raw("X-GNOME-Autostart-enabled") or "").strip().lower() == "false":
            enabled = False
        user_path = paths[0] if os.path.dirname(paths[0]) == user_dir else None
        system_path = next((path for path in paths if os.path.dirname(path) != user_dir), None)
        result.append(StartupEntry(
            file_id, clean(value("Name", True) or file_id[:-8], 120), publisher_of(value, file_id),
            clean(value("Icon") or "", 256), clean(command or "", 1024), enabled,
            "System" if system_path else "User", winner.path, user_path, system_path))
    result.sort(key=lambda entry: (entry.name.casefold(), entry.file_id))
    return result


def _executable(program: str, which: Callable[[str], str | None]) -> bool:
    if program.startswith("/"):
        return os.path.isfile(program) and os.access(program, os.X_OK)
    return which(program) is not None


def with_keys(text: str, values: Mapping[str, str]) -> str:
    """``text`` of a desktop file with keys of ``[Desktop Entry]`` set to new values.

    A key that is there is changed in place (a second one of the same name
    is dropped), one that is not is added at the end of the group. Comments,
    the order of the lines and the other groups stay as they are.
    """
    lines = text.split("\n")
    out: list[str] = []
    pending = dict(values)
    in_main = seen_main = False
    last_key = -1                         # index in ``out`` after the last key line of the main group

    def flush() -> None:
        nonlocal last_key
        if pending and seen_main:
            position = last_key if last_key >= 0 else len(out)
            for key, value in pending.items():
                out.insert(position, f"{key}={value}")
                position += 1
            pending.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_main:
                flush()
            in_main = stripped == f"[{MAIN}]" and not seen_main
            if in_main:
                seen_main = True
                out.append(line)
                last_key = len(out)
                continue
        elif in_main and stripped and stripped[0] != "#" and "=" in stripped:
            key = stripped.split("=", 1)[0].rstrip()
            if key in values:
                if key in pending:
                    out.append(f"{key}={pending.pop(key)}")
                    last_key = len(out)
                continue
            out.append(line)
            last_key = len(out)
            continue
        out.append(line)
    if in_main:
        flush()
    if not seen_main:
        out = [f"[{MAIN}]", *[f"{key}={value}" for key, value in values.items()], *out]
    result = "\n".join(out)
    return result if result.endswith("\n") else result + "\n"


def _write_user_file(user_dir: str, file_id: str, text: str) -> str:
    if not valid_file_id(file_id):
        raise StartupError(f"“{file_id}” is not a usable name for a startup entry.")
    target = os.path.join(user_dir, file_id)
    try:
        os.makedirs(user_dir, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".task-manager-", suffix=".tmp", dir=user_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)       # replaces a symbolic link itself, never what it points to
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError as error:
        raise StartupError(f"The startup entry could not be written: {error.strerror or error}.") from error
    return target


def set_startup_enabled(file_id: str, enabled: bool, env: Mapping[str, str]) -> str:
    """Switch a startup entry on or off for this user; returns the path of the user's file.

    The user's file of that name gets ``Hidden=false`` or ``Hidden=true``.
    When the user has no such file yet, or has one that only hides the
    system's entry, it becomes a copy of the system's file with that key.
    """
    if not valid_file_id(file_id):
        raise StartupError(f"“{file_id}” is not a usable name for a startup entry.")
    user_dir, system_dirs = autostart_dirs(env)
    user_path = os.path.join(user_dir, file_id)
    system_path = next((path for path in (os.path.join(directory, file_id) for directory in system_dirs)
                        if os.path.isfile(path)), None)
    text = read_entry_text(user_path) if os.path.isfile(user_path) else None
    if text is not None and system_path is not None and not exec_argv(DesktopEntry(text).get("Exec")):
        text = None                        # a bare "Hidden=true" file: start again from the system's entry
    if text is None:
        if system_path is None:
            raise StartupError("This startup entry no longer exists.")
        text = read_entry_text(system_path)
        if text is None:
            raise StartupError("The system's startup entry could not be read.")
    values = {"Hidden": "false" if enabled else "true"}
    if enabled and (DesktopEntry(text).raw("X-GNOME-Autostart-enabled") or "").strip().lower() == "false":
        values["X-GNOME-Autostart-enabled"] = "true"
    return _write_user_file(user_dir, file_id, with_keys(text, values))


def add_startup(application_path: str, env: Mapping[str, str]) -> str:
    """Make an installed application start with the session: its desktop file is copied into the
    user's autostart directory. Returns the file ID."""
    file_id = os.path.basename(application_path)
    if not valid_file_id(file_id):
        raise StartupError("This application's desktop file has a name that cannot be used.")
    text = read_entry_text(application_path)
    entry = DesktopEntry(text, application_path) if text is not None else None
    if entry is None or MAIN not in entry.groups or not exec_argv(entry.get("Exec")):
        raise StartupError("This application has no command that could be started.")
    for existing in list_startup(env, which=lambda _program: "/"):
        if existing.file_id == file_id:
            if existing.enabled:
                raise StartupError(f"{existing.name} already starts when you sign in.")
            set_startup_enabled(file_id, True, env)
            return file_id
    user_dir, _system = autostart_dirs(env)
    if entry.raw("Hidden") is not None:
        text = with_keys(text, {"Hidden": "false"})
    _write_user_file(user_dir, file_id, text)
    return file_id


def remove_startup(file_id: str, env: Mapping[str, str]) -> None:
    """Delete the user's own startup entry (an entry the system installed cannot be removed, only disabled)."""
    if not valid_file_id(file_id):
        raise StartupError(f"“{file_id}” is not a usable name for a startup entry.")
    user_dir, _system = autostart_dirs(env)
    try:
        os.unlink(os.path.join(user_dir, file_id))
    except FileNotFoundError:
        raise StartupError("This startup entry no longer exists.") from None
    except OSError as error:
        raise StartupError(f"The startup entry could not be removed: {error.strerror or error}.") from error
