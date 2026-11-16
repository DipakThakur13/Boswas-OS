"""Default applications and start-up applications, without Qt.

Default applications (freedesktop "MIME applications" specification):
  the installed applications that declare a kind's main MIME type or scheme
  (MimeType= of their desktop entries), the current default as the desktop
  resolves it (the user's mimeapps.list, then the system's), and the new
  content of ~/.config/mimeapps.list for a choice. Only the keys of the kind
  in [Default Applications] change; every other line of the file (comments,
  other groups, keys this program does not know) is kept as it is.

Start-up applications ("Desktop Application Autostart" specification):
  the entries of ~/.config/autostart and /etc/xdg/autostart that apply to
  the Boswas desktop (OnlyShowIn / NotShowIn), whether each is on (not
  Hidden=true), and the content of the user's file that switches one off or
  on, adds an installed application, or is removed.

Nothing here writes: the texts built here are written by
userfiles.write_config / remove_config, which the sections call.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass

from . import probes
from . import viewmodel as vm

MAX_FILE = 256 * 1024
MAX_ENTRIES = 200
ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")       # userfiles.AUTOSTART_RE without the folder
MIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}/[A-Za-z0-9][A-Za-z0-9.+_-]{0,119}$")
DEFAULTS_GROUP = "Default Applications"
OVERRIDE_KEY = "X-Boswas-Autostart-Override"


@dataclass(frozen=True)
class Kind:
    key: str
    title: str
    description: str
    icon: str
    types: tuple[str, ...]            # the first one decides who is offered; all are set when the app has them


KINDS = (
    Kind("browser", "Web browser", "Opens links and web pages", "text-html",
         ("x-scheme-handler/https", "x-scheme-handler/http", "text/html", "application/xhtml+xml")),
    Kind("mail", "Mail", "Opens e-mail addresses (mailto links)", "mail-unread-symbolic",
         ("x-scheme-handler/mailto",)),
    Kind("files", "File manager", "Opens folders", "inode-directory", ("inode/directory",)),
    Kind("text", "Text editor", "Opens plain text files", "text-plain", ("text/plain",)),
    Kind("images", "Image viewer", "Opens photos and pictures", "image-x-generic",
         ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff", "image/svg+xml")),
    Kind("video", "Video player", "Plays video files", "video-x-generic",
         ("video/mp4", "video/x-matroska", "video/webm", "video/quicktime", "video/x-msvideo", "video/mpeg")),
    Kind("music", "Music player", "Plays music and other sound files", "audio-x-generic",
         ("audio/mpeg", "audio/flac", "audio/ogg", "audio/x-wav", "audio/mp4", "audio/aac")),
    Kind("pdf", "PDF viewer", "Opens PDF documents", "application-pdf", ("application/pdf",)),
)
KIND_BY_KEY = {kind.key: kind for kind in KINDS}


@dataclass(frozen=True)
class App:
    desktop_id: str               # without ".desktop"
    name: str
    icon: str = ""
    types: frozenset[str] = frozenset()
    path: str = ""


def _list(value: object) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()] if isinstance(value, str) else []


def installed_apps(environ, locale: str | None = None) -> list[App]:
    """The applications shown on the Boswas desktop, with the types each declares (blocking: reads files)."""
    search_path = environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"

    def which(program: str) -> str | None:
        if os.path.isabs(program):
            return program if os.access(program, os.X_OK) else None
        return shutil.which(program, path=search_path)

    apps = []
    for desktop_id, path in probes.desktop_files(probes.xdg_data_dirs(environ)):
        try:
            with open(path, "rb") as handle:
                text = handle.read(MAX_FILE).decode("utf-8", errors="replace")
        except OSError:
            continue
        entry = vm.parse_desktop_entry(text, desktop_id, path, locale, vm.CURRENT_DESKTOP, which)
        if entry is None:
            continue
        types = frozenset(t for t in _list(vm.parse_desktop_group(text).get("MimeType"))
                          if MIME_RE.fullmatch(t))
        apps.append(App(desktop_id, entry.name, entry.icon, types, path))
    return sorted(apps, key=lambda app: (app.name.casefold(), app.desktop_id))


def candidates(kind: Kind, apps: list[App]) -> list[App]:
    """The applications that can be the default of a kind: those declaring its main type."""
    return [app for app in apps if kind.types[0] in app.types]


# --- mimeapps.list ------------------------------------------------------------------------------------------

def parse_defaults(text: object) -> dict[str, list[str]]:
    """{type: [desktop ids without .desktop]} of the [Default Applications] group."""
    found: dict[str, list[str]] = {}
    if not isinstance(text, str):
        return found
    group = None
    for line in text.splitlines()[:20000]:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            group = stripped[1:-1]
        elif group == DEFAULTS_GROUP and "=" in stripped and not stripped.startswith("#"):
            key, _sep, value = stripped.partition("=")
            ids = [item[:-len(".desktop")] for item in _list(value) if item.endswith(".desktop")]
            if key.strip() and ids and key.strip() not in found:
                found[key.strip()] = ids
    return found


def mimeapps_files(environ) -> list[str]:
    """Where defaults are looked up, most important first (the specification's order, for the Boswas desktop)."""
    from . import userfiles
    desktop = vm.CURRENT_DESKTOP.lower()
    dirs = []
    try:
        dirs.append(userfiles.config_dir(environ))
    except userfiles.UserFileError:
        pass
    dirs += [d for d in (environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if os.path.isabs(d)]
    files = []
    for directory in dirs:
        files += [os.path.join(directory, f"{desktop}-mimeapps.list"), os.path.join(directory, "mimeapps.list")]
    for directory in probes.xdg_data_dirs(environ):
        files += [os.path.join(directory, "applications", f"{desktop}-mimeapps.list"),
                  os.path.join(directory, "applications", "mimeapps.list")]
    return files


def read_defaults(environ) -> list[dict[str, list[str]]]:
    """The [Default Applications] of every mimeapps.list that exists, most important first (blocking)."""
    layers = []
    for path in mimeapps_files(environ):
        try:
            with open(path, "rb") as handle:
                layers.append(parse_defaults(handle.read(MAX_FILE).decode("utf-8", errors="replace")))
        except OSError:
            continue
    return layers


def current_default(kind: Kind, apps: list[App], layers: list[dict[str, list[str]]]) -> str | None:
    """The desktop id that opens the kind's main type now: the first listed default that is installed."""
    installed = {app.desktop_id for app in apps}
    for layer in layers:
        for desktop_id in layer.get(kind.types[0], ()):
            if desktop_id in installed:
                return desktop_id
    return None


def with_default(text: str, kind: Kind, app: App) -> str:
    """The content of mimeapps.list after making ``app`` the default of ``kind``.

    The kind's main type and every other type of the kind that the
    application declares are set in [Default Applications]; the group is
    added when the file has none. All other lines stay exactly as they
    were.
    """
    if not vm.valid_desktop_id(app.desktop_id) or kind.types[0] not in app.types:
        raise ValueError(f"{app.name} cannot open {kind.title.lower()} files")
    wanted = {t: f"{t}={app.desktop_id}.desktop" for t in kind.types if t in app.types}
    lines = text.split("\n") if text else []
    if lines and lines[-1] == "":
        lines.pop()
    out: list[str] = []
    group = None
    start = end = None                    # the lines of the first [Default Applications] group
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if group == DEFAULTS_GROUP and end is None:
                end = len(out)
            group = stripped[1:-1]
            if group == DEFAULTS_GROUP and start is None:
                start = len(out) + 1
            elif group == DEFAULTS_GROUP:
                group = "second " + DEFAULTS_GROUP     # a repeated group: left alone
        elif group == DEFAULTS_GROUP and end is None and "=" in stripped and not stripped.startswith("#"):
            key = stripped.partition("=")[0].strip()
            if key in wanted:
                out.append(wanted.pop(key))
                continue
        out.append(line)
    if start is None:
        if out and out[-1].strip():
            out.append("")
        out.append(f"[{DEFAULTS_GROUP}]")
        out += list(wanted.values())
    else:
        if end is None:
            end = len(out)
        while end > start and not out[end - 1].strip():
            end -= 1                      # new keys go before the blank lines that end the group
        out[end:end] = list(wanted.values())
    return "\n".join(out) + "\n"


# --- autostart ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Startup:
    ident: str                    # the file name without ".desktop"
    name: str
    comment: str = ""
    icon: str = ""
    enabled: bool = True
    user: bool = False            # there is a file of the user's
    system: bool = False          # there is a system-wide file
    changeable: bool = True       # the id can be written by userfiles (strict file names only)

    @property
    def removable(self) -> bool:
        return self.user and not self.system


def autostart_dirs(environ) -> tuple[str | None, list[str]]:
    """(the user's autostart folder, the system-wide ones)."""
    from . import userfiles
    try:
        user = os.path.join(userfiles.config_dir(environ), "autostart")
    except userfiles.UserFileError:
        user = None
    system = [os.path.join(d, "autostart") for d in (environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":")
              if os.path.isabs(d)]
    return user, system


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(MAX_FILE).decode("utf-8", errors="replace")
    except OSError:
        return None


def _applies(group: dict[str, str]) -> bool:
    only = _list(group.get("OnlyShowIn"))
    return (not only or vm.CURRENT_DESKTOP in only) and vm.CURRENT_DESKTOP not in _list(group.get("NotShowIn"))


def startup_entries(environ, locale: str | None = None) -> list[Startup]:
    """The start-up applications of this desktop (blocking: reads the autostart folders)."""
    user_dir, system_dirs = autostart_dirs(environ)
    found: dict[str, dict] = {}
    for directory, is_user in ([(user_dir, True)] if user_dir else []) + [(d, False) for d in system_dirs]:
        try:
            names = sorted(os.listdir(directory))[:MAX_ENTRIES * 4]
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop") or name.startswith("."):
                continue
            ident = name[:-len(".desktop")]
            record = found.setdefault(ident, {"user": None, "system": None})
            text = _read(os.path.join(directory, name))
            if text is not None and record["user" if is_user else "system"] is None:
                record["user" if is_user else "system"] = text
    entries = []
    for ident, record in sorted(found.items()):
        winner = record["user"] if record["user"] is not None else record["system"]
        base = record["system"] if record["system"] is not None else record["user"]
        if winner is None or base is None:
            continue
        group, base_group = vm.parse_desktop_group(winner), vm.parse_desktop_group(base)
        described = base_group if not group.get("Name") else group
        if described.get("Type", "Application") != "Application" or not _applies(base_group) or not _applies(group):
            continue
        if not (described.get("Exec") or base_group.get("Exec")):
            continue

        def localised(key: str, limit: int) -> str:
            for suffix in vm.locale_keys(locale):
                value = described.get(f"{key}[{suffix}]")
                if value:
                    return vm.one_line(value, limit)
            return vm.one_line(described.get(key, ""), limit)

        entries.append(Startup(ident, localised("Name", 120) or ident, localised("Comment", 200),
                               vm.one_line(described.get("Icon", ""), 200),
                               group.get("Hidden", "").strip().lower() != "true",
                               record["user"] is not None, record["system"] is not None,
                               bool(ID_RE.fullmatch(ident))))
        if len(entries) >= MAX_ENTRIES:
            break
    return sorted(entries, key=lambda e: (e.name.casefold(), e.ident))


def _set_key(text: str, key: str, value: str | None) -> str:
    """The desktop entry text with ``key`` of [Desktop Entry] set to ``value`` (removed for None)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out, group, done, last = [], None, False, None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            group = stripped[1:-1]
        elif group == "Desktop Entry" and stripped.partition("=")[0].strip() == key and "=" in stripped:
            if value is not None and not done:
                out.append(f"{key}={value}")
            done = True
            last = len(out)
            continue
        out.append(line)
        if group == "Desktop Entry" and stripped:
            last = len(out)
    if value is not None and not done:
        if last is None:
            out = ["[Desktop Entry]", "Type=Application", *out]
            last = 2
        out.insert(last, f"{key}={value}")
    return "\n".join(out) + "\n"


def autostart_name(ident: str) -> str:
    """The name for userfiles.write_config; ValueError for an id that cannot be a file of ours."""
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
        raise ValueError("this start-up entry has a file name Control Center does not write")
    return f"autostart/{ident}.desktop"


def _hidden(text: str | None) -> bool:
    return text is not None and vm.parse_desktop_group(text).get("Hidden", "").strip().lower() == "true"


def _is_override(text: str | None) -> bool:
    return text is not None and vm.parse_desktop_group(text).get(OVERRIDE_KEY, "").strip().lower() == "true"


def switched(entry: Startup, enabled: bool, user_text: str | None, system_text: str | None) -> str | None:
    """What the user's file must contain to switch the entry on or off; None means: remove the user's file.

    A system-wide entry is never changed. To differ from it, the user gets
    a file of the same name: a copy of the system-wide entry, marked as an
    override made here, with Hidden=true (off) or Hidden=false (on). When
    the wanted state is the system-wide entry's own state again, an
    override made here is removed; a file of the user's own is kept and
    only its Hidden key changes.
    """
    if system_text is None:
        if user_text is None:
            raise ValueError("there is nothing to switch")
        return _set_key(user_text, "Hidden", None if enabled else "true")
    if enabled != _hidden(system_text) and (user_text is None or _is_override(user_text)):
        return None                       # the system-wide entry already is what is wanted
    base = user_text if user_text is not None else _set_key(system_text, OVERRIDE_KEY, "true")
    return _set_key(base, "Hidden", "false" if enabled else "true")


def added(app_text: str) -> str:
    """The autostart file for an installed application: its own desktop entry, switched on."""
    if len(app_text.encode("utf-8")) > MAX_FILE or vm.parse_desktop_group(app_text).get("Type") != "Application":
        raise ValueError("this is not an application's desktop entry")
    return _set_key(_set_key(app_text, "Hidden", None), "X-Boswas-Autostart-Added", "true")


def read_texts(environ, ident: str) -> tuple[str | None, str | None]:
    """(the user's file, the first system-wide file) of a start-up entry (blocking)."""
    autostart_name(ident)
    user_dir, system_dirs = autostart_dirs(environ)
    user = _read(os.path.join(user_dir, f"{ident}.desktop")) if user_dir else None
    system = next((text for text in (_read(os.path.join(d, f"{ident}.desktop")) for d in system_dirs)
                   if text is not None), None)
    return user, system
