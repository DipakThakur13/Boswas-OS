"""Applications that open files: desktop entries, the Exec key, mimeapps.list (no Qt).

Follows the Desktop Entry Specification (parsing, string escapes, the Exec
key with its quoting and field codes) and the MIME Applications
Associations specification (which mimeapps.list files are read, in which
order, and where a new default is written: ``$XDG_CONFIG_HOME/mimeapps.list``).

``expand_exec`` returns argument lists; nothing here goes through a shell.
As GLib does, single quotes are accepted in Exec (the specification only
knows double quotes, real desktop files use both), and an Exec line without
a file field code gets one file appended.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from .clipformats import path_to_uri

_MAX_FILE = 1024 * 1024
_MAX_FILES = 8000
_MAX_DEPTH = 3
DEFAULTS, ADDED, REMOVED = "Default Applications", "Added Associations", "Removed Associations"


class ExecError(ValueError):
    """The Exec line of a desktop entry cannot be used."""


@dataclass
class DesktopApp:
    app_id: str                         # "org.kde.kate.desktop"
    path: str
    name: str
    exec_line: str
    icon: str = ""
    mime_types: tuple[str, ...] = ()
    terminal: bool = False
    no_display: bool = False
    working_dir: str = ""
    comment: str = ""
    actions: dict[str, tuple[str, str]] = field(default_factory=dict)       # id -> (name, exec)


def unescape_value(raw: str) -> str:
    r"""A string value of a desktop entry with ``\s \n \t \r \\`` resolved."""
    if "\\" not in raw:
        return raw
    out = []
    index = 0
    table = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    while index < len(raw):
        character = raw[index]
        if character == "\\" and index + 1 < len(raw) and raw[index + 1] in table:
            out.append(table[raw[index + 1]])
            index += 2
            continue
        out.append(character)
        index += 1
    return "".join(out)


def parse_ini(text: str) -> dict[str, dict[str, str]]:
    """Groups and their raw key=value pairs. The first occurrence of a group and of a key wins."""
    groups: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            if name in groups:
                current = {}                 # a repeated group is ignored
            else:
                current = groups[name] = {}
            continue
        if current is None:
            continue
        key, separator, value = line.partition("=")
        if separator:
            current.setdefault(key.strip(), value.strip())
    return groups


def _languages() -> list[str]:
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(variable) or ""
        if value:
            base = value.split(".", 1)[0].split("@", 1)[0]
            if base in ("C", "POSIX"):
                return []
            return [base, base.split("_", 1)[0]] if "_" in base else [base]
    return []


def _localized(group: dict[str, str], key: str, languages: list[str]) -> str:
    for language in languages:
        value = group.get(f"{key}[{language}]")
        if value:
            return unescape_value(value)
    return unescape_value(group.get(key, ""))


def _is_true(value: str) -> bool:
    return value.strip().lower() == "true"


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def find_program(name: str, search_path: str | None = None) -> str | None:
    """The absolute path of an executable named in Exec or TryExec, or None."""
    if not name or "\0" in name:
        return None
    if "/" in name:
        return name if name.startswith("/") and os.path.isfile(name) and os.access(name, os.X_OK) else None
    return shutil.which(name, path=search_path)


def load_app(path: str, app_id: str, languages: list[str] | None = None) -> DesktopApp | None:
    """The application of a desktop file; None when the file is not a usable application entry.

    A hidden entry (``Hidden=true``) is returned with an empty ``exec_line``
    so that it can mask an entry of the same ID further down the search path.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(_MAX_FILE)
    except OSError:
        return None
    groups = parse_ini(text)
    entry = groups.get("Desktop Entry")
    if entry is None or entry.get("Type", "Application") != "Application":
        return None
    languages = _languages() if languages is None else languages
    name = _localized(entry, "Name", languages) or app_id
    if _is_true(entry.get("Hidden", "")):
        return DesktopApp(app_id, path, name, "")
    exec_line = unescape_value(entry.get("Exec", ""))
    if not exec_line.strip():
        return None
    try_exec = unescape_value(entry.get("TryExec", ""))
    if try_exec and find_program(try_exec) is None:
        return None
    actions = {}
    for action in _list(entry.get("Actions", "")):
        group = groups.get(f"Desktop Action {action}")
        if group and group.get("Exec"):
            actions[action] = (_localized(group, "Name", languages) or action, unescape_value(group["Exec"]))
    return DesktopApp(
        app_id=app_id, path=path, name=name, exec_line=exec_line, icon=unescape_value(entry.get("Icon", "")),
        mime_types=tuple(_list(entry.get("MimeType", ""))), terminal=_is_true(entry.get("Terminal", "")),
        no_display=_is_true(entry.get("NoDisplay", "")), working_dir=unescape_value(entry.get("Path", "")),
        comment=_localized(entry, "Comment", languages), actions=actions)


# --- the Exec key -------------------------------------------------------------------------------------------

def split_exec(line: str) -> list[tuple[str, bool]]:
    """The arguments of an Exec line as (text, was quoted). Raises ExecError for an unfinished quote."""
    tokens: list[tuple[str, bool]] = []
    current: list[str] = []
    quoted = started = False
    index, length = 0, len(line)
    while index < length:
        character = line[index]
        if character in "\"'":
            started = quoted = True
            index += 1
            while True:
                if index >= length:
                    raise ExecError("a quote is not closed")
                inner = line[index]
                if inner == character:
                    index += 1
                    break
                if character == '"' and inner == "\\" and index + 1 < length and line[index + 1] in '"`$\\':
                    current.append(line[index + 1])
                    index += 2
                    continue
                current.append(inner)
                index += 1
        elif character in " \t\n":
            if started:
                tokens.append(("".join(current), quoted))
                current, quoted, started = [], False, False
            index += 1
        elif character == "\\" and index + 1 < length:
            current.append(line[index + 1])
            started = True
            index += 2
        else:
            current.append(character)
            started = True
            index += 1
    if started:
        tokens.append(("".join(current), quoted))
    return tokens


_FILE_CODES = ("%f", "%F", "%u", "%U")


def _expand_token(token: str, path: str | None, app: DesktopApp) -> str | None:
    out = []
    index = 0
    used_code = False
    while index < len(token):
        character = token[index]
        if character != "%" or index + 1 >= len(token):
            out.append(character)
            index += 1
            continue
        code = token[index + 1]
        index += 2
        if code == "%":
            out.append("%")
            continue
        used_code = True
        if code in "fF":
            out.append(path or "")
        elif code in "uU":
            out.append(path_to_uri(path) if path else "")
        elif code == "c":
            out.append(app.name)
        elif code == "k":
            out.append(app.path)
        # %i is handled by the caller; deprecated and unknown codes expand to nothing
    text = "".join(out)
    return None if used_code and not text else text


def expand_exec(app: DesktopApp, paths: list[str], exec_line: str | None = None) -> list[list[str]]:
    """The command lines that open ``paths`` with ``app``: one, or one per file for ``%f`` and ``%u``.

    Field codes are expanded in unquoted arguments only. ``%F`` and ``%U``
    as arguments of their own become one argument per file; ``%U`` and
    ``%u`` give ``file://`` URLs.
    """
    tokens = split_exec(app.exec_line if exec_line is None else exec_line)
    if not tokens:
        raise ExecError("the Exec line is empty")
    plain = [token for token, quoted in tokens if not quoted]
    takes_many = any(token in ("%F", "%U") for token in plain)
    takes_one = any(code in token for token in plain for code in _FILE_CODES) and not takes_many
    if paths and not takes_many and not takes_one:
        tokens.append(("%f", False))
    groups = [list(paths)] if takes_many or not paths else [[path] for path in paths]
    commands = []
    for group in groups:
        argv: list[str] = []
        for token, quoted in tokens:
            if quoted or "%" not in token:
                argv.append(token)
            elif token == "%F":
                argv.extend(group)
            elif token == "%U":
                argv.extend(path_to_uri(path) for path in group)
            elif token == "%i":
                if app.icon:
                    argv.extend(["--icon", app.icon])
            else:
                expanded = _expand_token(token, group[0] if group else None, app)
                if expanded is not None:
                    argv.append(expanded)
        if not argv or not argv[0]:
            raise ExecError("the Exec line names no program")
        commands.append(argv)
    return commands


# --- where applications and associations live -----------------------------------------------------------------

def _xdg_list(variable: str, default: str) -> list[str]:
    return [item for item in (os.environ.get(variable) or default).split(":") if item.startswith("/")]


def _xdg_home(variable: str, default: str) -> str:
    value = os.environ.get(variable) or ""
    return value if value.startswith("/") else os.path.join(os.path.expanduser("~"), default)


def application_dirs() -> list[str]:
    """The ``applications`` directories, the user's first."""
    dirs = [os.path.join(_xdg_home("XDG_DATA_HOME", ".local/share"), "applications")]
    dirs += [os.path.join(item, "applications") for item in _xdg_list("XDG_DATA_DIRS", "/usr/local/share:/usr/share")]
    return list(dict.fromkeys(dirs))


def mimeapps_files() -> list[str]:
    """Every mimeapps.list that may exist, most important first (the specification's order)."""
    desktops = [name.lower() for name in (os.environ.get("XDG_CURRENT_DESKTOP") or "").split(":") if name]
    names = [f"{desktop}-mimeapps.list" for desktop in desktops] + ["mimeapps.list"]
    dirs = [_xdg_home("XDG_CONFIG_HOME", ".config")] + _xdg_list("XDG_CONFIG_DIRS", "/etc/xdg") + application_dirs()
    return list(dict.fromkeys(os.path.join(directory, name) for directory in dirs for name in names))


def user_mimeapps() -> str:
    return os.path.join(_xdg_home("XDG_CONFIG_HOME", ".config"), "mimeapps.list")


def _read_associations(path: str) -> dict[str, dict[str, list[str]]] | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            groups = parse_ini(handle.read(_MAX_FILE))
    except OSError:
        return None
    return {name: {mime: _list(value) for mime, value in groups.get(name, {}).items()}
            for name in (DEFAULTS, ADDED, REMOVED)}


class AppIndex:
    """The installed applications and which of them opens which MIME type."""

    def __init__(self, app_dirs: list[str] | None = None, list_files: list[str] | None = None,
                 user_list: str | None = None) -> None:
        self._app_dirs = app_dirs
        self._list_files = list_files
        self._user_list = user_list
        self.apps: dict[str, DesktopApp] = {}
        self._by_mime: dict[str, list[str]] = {}
        self._lists: list[dict[str, dict[str, list[str]]]] = []
        self.loaded = False

    def load(self) -> "AppIndex":
        """Read every desktop file and mimeapps.list (blocking: call it off the GUI thread)."""
        apps: dict[str, DesktopApp] = {}
        masked: set[str] = set()
        count = 0
        languages = _languages()
        for directory in (self._app_dirs if self._app_dirs is not None else application_dirs()):
            for path, app_id in self._desktop_files(directory):
                count += 1
                if count > _MAX_FILES:
                    break
                if app_id in apps or app_id in masked:
                    continue
                app = load_app(path, app_id, languages)
                if app is None:
                    continue
                if not app.exec_line:
                    masked.add(app_id)
                    continue
                apps[app_id] = app
        by_mime: dict[str, list[str]] = {}
        for app_id in sorted(apps, key=lambda key: (apps[key].name.casefold(), key)):
            for mime in apps[app_id].mime_types:
                by_mime.setdefault(mime, []).append(app_id)
        lists = []
        for path in (self._list_files if self._list_files is not None else mimeapps_files()):
            parsed = _read_associations(path)
            if parsed is not None:
                lists.append(parsed)
        self.apps, self._by_mime, self._lists, self.loaded = apps, by_mime, lists, True
        return self

    @staticmethod
    def _desktop_files(directory: str):
        stack = [(directory, "", 0)]
        while stack:
            current, prefix, depth = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda entry: entry.name)
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir() and depth < _MAX_DEPTH:
                        stack.append((entry.path, f"{prefix}{entry.name}-", depth + 1))
                    elif entry.name.endswith(".desktop") and entry.is_file():
                        yield entry.path, prefix + entry.name
                except OSError:
                    continue

    def associated(self, mime: str) -> list[DesktopApp]:
        """The applications for exactly ``mime``: added associations, then what declares it; removed ones not."""
        result: list[str] = []
        removed: set[str] = set()
        for parsed in self._lists:
            for app_id in parsed[ADDED].get(mime, []):
                if app_id in self.apps and app_id not in removed and app_id not in result:
                    result.append(app_id)
            removed.update(parsed[REMOVED].get(mime, []))
        for app_id in self._by_mime.get(mime, []):
            if app_id not in removed and app_id not in result:
                result.append(app_id)
        return [self.apps[app_id] for app_id in result]

    def default_for(self, mimes: list[str]) -> DesktopApp | None:
        """The default application of a file whose MIME type, aliases and ancestors are ``mimes`` (in that order)."""
        for mime in mimes:
            for parsed in self._lists:
                for app_id in parsed[DEFAULTS].get(mime, []):
                    if app_id in self.apps:
                        return self.apps[app_id]
            associated = self.associated(mime)
            if associated:
                return associated[0]
        return None

    def apps_for(self, mimes: list[str]) -> list[DesktopApp]:
        """Every application that opens the type, the default first."""
        result: list[DesktopApp] = []
        default = self.default_for(mimes)
        if default is not None:
            result.append(default)
        for mime in mimes:
            for app in self.associated(mime):
                if app not in result:
                    result.append(app)
        return result

    def visible_apps(self) -> list[DesktopApp]:
        """Every application meant for the menu, by name."""
        return sorted((app for app in self.apps.values() if not app.no_display),
                      key=lambda app: (app.name.casefold(), app.app_id))

    def set_default(self, mime: str, app_id: str) -> None:
        """Make ``app_id`` the default for ``mime`` in the user's mimeapps.list. Raises OSError, ValueError."""
        if not mime or any(character in mime for character in "=;[]\n\r") or "/" not in mime:
            raise ValueError(f"not a MIME type: {mime!r}")
        if not app_id.endswith(".desktop") or any(character in app_id for character in "=;\n\r/"):
            raise ValueError(f"not a desktop file ID: {app_id!r}")
        path = self._user_list or user_mimeapps()
        set_default_in_file(path, mime, app_id)
        if self.loaded:
            files = list(self._list_files if self._list_files is not None else mimeapps_files())
            if path not in files:
                files.insert(0, path)
            self._lists = [parsed for parsed in map(_read_associations, files) if parsed is not None]


def set_default_in_file(path: str, mime: str, app_id: str) -> None:
    """Edit a mimeapps.list in place of a rewrite: other groups, keys and comments stay as they are."""
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            lines = handle.read(_MAX_FILE).splitlines()
    except FileNotFoundError:
        lines = []
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            sections.append((stripped[1:-1], []))
        else:
            sections[-1][1].append(line)

    created: set[str] = set()

    def body(name: str) -> list[str]:
        for section_name, content in sections:
            if section_name == name:
                return content
        content: list[str] = []
        sections.append((name, content))
        created.add(name)
        return content

    def update(name: str, change) -> None:
        content = body(name)
        for index, line in enumerate(content):
            key, separator, value = line.partition("=")
            if separator and key.strip() == mime:
                new = change(_list(value))
                if new is None:
                    del content[index]
                else:
                    content[index] = f"{mime}={new}"
                return
        new = change([])
        if new is not None:
            position = len(content)
            while position > 0 and not content[position - 1].strip():
                position -= 1
            content.insert(position, f"{mime}={new}")

    update(DEFAULTS, lambda _old: app_id)
    update(ADDED, lambda old: ";".join([app_id] + [item for item in old if item != app_id]) + ";")
    if any(name == REMOVED for name, _content in sections):
        def without(old: list[str]) -> str | None:
            kept = [item for item in old if item != app_id]
            return ";".join(kept) + ";" if kept else None
        update(REMOVED, without)
    out: list[str] = []
    for name, content in sections:
        if name is not None:
            if name in created and out and out[-1].strip():
                out.append("")
            out.append(f"[{name}]")
        out.extend(content)
    text = "\n".join(out).rstrip("\n") + "\n"
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    partial = f"{path}.{os.getpid()}.tmp"
    try:
        with open(partial, "w", encoding="utf-8", errors="surrogateescape") as handle:
            handle.write(text)
        os.replace(partial, path)
    except BaseException:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise
