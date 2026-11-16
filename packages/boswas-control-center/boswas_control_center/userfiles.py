"""The only files Control Center writes or deletes, all in the user's own home folder.

  ~/Applications/<name>.AppImage                         AppImages the user added
  ~/.local/share/applications/boswas-appimage-<id>.desktop  their menu entries
  ~/.cache/thumbnails/                                   the thumbnail cache (cleanup)
  <config>/boswas/compositor.toml                        the user layer of the compositor's settings
  <config>/boswas/compositor.toml.bak                    its previous content (kept while a change is tried)
  <config>/boswas/appearance.toml                        the [appearance] table (colour scheme, accent, ...)
  <config>/mimeapps.list                                 default applications
  <config>/autostart/<id>.desktop                        start-up applications and their overrides
  ~/.face                                                the account picture
  <data>/recently-used.xbel                              the list of recently used files (emptied)

<config> is $XDG_CONFIG_HOME when that is an absolute path, else ~/.config;
<data> is $XDG_DATA_HOME when that is an absolute path, else ~/.local/share.
The settings files are written by write_config() and deleted by
remove_config(), which accept the names of CONFIG_FILES (and
autostart/<id>.desktop) only: the content replaces the file atomically (a
temporary file in the same directory, then a rename), with mode 0600, and
nothing is written through a symbolic link below <config>.

Every path is built here from a validated name and checked to stay inside
its directory (no symlinks are followed out of it). Nothing here runs a
program, and nothing outside these locations is touched. AppImages run
unsandboxed with the user's permissions: the window says so before adding one.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass

APPIMAGE_DIR = "Applications"
ENTRY_DIR = ".local/share/applications"
ENTRY_PREFIX = "boswas-appimage-"
THUMBNAIL_DIR = ".cache/thumbnails"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_SCAN = 200000


class UserFileError(Exception):
    """A message for the user."""


def _home(environ) -> str:
    home = environ.get("HOME") or ""
    if not home or not os.path.isabs(home) or not os.path.isdir(home):
        raise UserFileError("Your home folder could not be found.")
    return os.path.realpath(home)


def _inside(path: str, directory: str) -> bool:
    real_dir = os.path.realpath(directory)
    real = os.path.realpath(path)
    return real == real_dir or real.startswith(real_dir + os.sep)


def appimage_id(file_name: str) -> str:
    """A safe identifier from a file name: "Krita-5.2.6-x86_64.AppImage" -> "krita-5-2-6-x86-64"."""
    base = os.path.basename(file_name)
    if base.lower().endswith(".appimage"):
        base = base[:-len(".appimage")]
    ident = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:63].strip("-")
    return ident or "appimage"


def _display_name(ident: str) -> str:
    words = [w for w in ident.split("-") if w and not re.fullmatch(r"[0-9]+|x86|64|amd64|x86_64|linux", w)]
    return " ".join(w.capitalize() for w in words) or ident


@dataclass(frozen=True)
class AppImage:
    ident: str
    name: str
    path: str
    entry: str
    size: int


def _desktop_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", " ").replace("\r", " ").replace("\t", " ")


def _exec_quote(path: str) -> str:
    """A path quoted for the Exec key (Desktop Entry Specification: double quotes, escapes)."""
    escaped = path.replace("\\", "\\\\\\\\").replace('"', '\\\\"').replace("`", "\\\\`").replace("$", "\\\\$")
    return f'"{escaped}"'


def list_appimages(environ) -> list[AppImage]:
    home = _home(environ)
    entries = os.path.join(home, ENTRY_DIR)
    found = []
    try:
        names = sorted(os.listdir(entries))
    except OSError:
        return []
    for name in names:
        if not (name.startswith(ENTRY_PREFIX) and name.endswith(".desktop")):
            continue
        ident = name[len(ENTRY_PREFIX):-len(".desktop")]
        if not ID_RE.fullmatch(ident):
            continue
        target = os.path.join(home, APPIMAGE_DIR, f"{ident}.AppImage")
        try:
            size = os.stat(target).st_size
        except OSError:
            size = 0
        display = _display_name(ident)
        try:
            with open(os.path.join(entries, name), encoding="utf-8", errors="replace") as handle:
                for line in handle.read(65536).splitlines():
                    if line.startswith("Name="):
                        display = line[5:].strip()[:120] or display
                        break
        except OSError:
            pass
        found.append(AppImage(ident, display, target, os.path.join(entries, name), size))
    return found


def add_appimage(environ, source: str, name: str | None = None) -> AppImage:
    """Copy an AppImage (already checked by filetypes.detect) to ~/Applications and add a menu entry."""
    home = _home(environ)
    ident = appimage_id(source)
    app_dir = os.path.join(home, APPIMAGE_DIR)
    entry_dir = os.path.join(home, ENTRY_DIR)
    target = os.path.join(app_dir, f"{ident}.AppImage")
    entry = os.path.join(entry_dir, f"{ENTRY_PREFIX}{ident}.desktop")
    for directory in (app_dir, entry_dir):
        if os.path.islink(directory):
            raise UserFileError(f"{directory} is a symbolic link; refusing to write through it.")
        os.makedirs(directory, mode=0o755, exist_ok=True)
    if not _inside(target, app_dir) or not _inside(entry, entry_dir):
        raise UserFileError("The AppImage cannot be stored safely.")
    if os.path.lexists(target) and os.path.realpath(target) != os.path.realpath(source):
        raise UserFileError(f"An AppImage named {ident}.AppImage is already in your Applications folder. Remove it "
                            "first.")
    if os.path.realpath(target) != os.path.realpath(source):
        temporary = target + ".part"
        try:
            shutil.copyfile(source, temporary, follow_symlinks=True)
            os.chmod(temporary, 0o755)
            os.replace(temporary, target)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise UserFileError(f"The AppImage could not be copied ({exc.strerror or exc}).") from None
    else:
        os.chmod(target, 0o755)
    display = (name or _display_name(ident))[:120]
    text = ("[Desktop Entry]\nType=Application\n"
            f"Name={_desktop_escape(display)}\n"
            "Comment=AppImage added with Boswas Control Center (runs without a sandbox)\n"
            f"Exec={_exec_quote(target)} %U\n"
            "Icon=application-x-executable\nTerminal=false\nCategories=Utility;\n"
            f"X-Boswas-AppImage={ident}\n")
    try:
        with open(entry + ".part", "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(entry + ".part", 0o644)
        os.replace(entry + ".part", entry)
    except OSError as exc:
        raise UserFileError(f"The menu entry could not be written ({exc.strerror or exc}).") from None
    return AppImage(ident, display, target, entry, os.stat(target).st_size)


def remove_appimage(environ, ident: str) -> None:
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
        raise UserFileError("Unknown AppImage.")
    home = _home(environ)
    for directory, name in ((os.path.join(home, APPIMAGE_DIR), f"{ident}.AppImage"),
                            (os.path.join(home, ENTRY_DIR), f"{ENTRY_PREFIX}{ident}.desktop")):
        path = os.path.join(directory, name)
        if not os.path.lexists(path):
            continue
        if os.path.islink(directory) or not _inside(os.path.dirname(path), directory):
            raise UserFileError("The AppImage cannot be removed safely.")
        try:
            os.unlink(path)
        except OSError as exc:
            raise UserFileError(f"{name} could not be removed ({exc.strerror or exc}).") from None


# --- thumbnail cache -------------------------------------------------------------------------------------------

def _thumbnail_root(environ) -> str:
    return os.path.join(_home(environ), THUMBNAIL_DIR)


def thumbnail_usage(environ) -> tuple[int, int]:
    """(files, bytes) in ~/.cache/thumbnails (symbolic links are not followed)."""
    root = _thumbnail_root(environ)
    files = size = 0
    if os.path.islink(root) or not os.path.isdir(root):
        return 0, 0
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in names:
            try:
                info = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                files += 1
                size += info.st_size
            if files >= MAX_SCAN:
                return files, size
    return files, size


def clean_thumbnails(environ) -> int:
    """Delete the regular files below ~/.cache/thumbnails; return the bytes freed."""
    root = _thumbnail_root(environ)
    if os.path.islink(root) or not os.path.isdir(root):
        return 0
    freed = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        if not _inside(current, root):
            continue
        for name in names:
            path = os.path.join(current, name)
            try:
                info = os.lstat(path)
                if stat.S_ISREG(info.st_mode):
                    os.unlink(path)
                    freed += info.st_size
            except OSError:
                continue
    return freed


# --- settings files in the user's config directory -------------------------------------------------------------

# The files write_config() and remove_config() accept, relative to the config directory; besides these,
# autostart/<id>.desktop with an id of AUTOSTART_RE.
CONFIG_FILES = ("boswas/compositor.toml", "boswas/compositor.toml.bak", "boswas/appearance.toml", "mimeapps.list")
AUTOSTART_RE = re.compile(r"^autostart/[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}\.desktop$")
MAX_CONFIG = 256 * 1024
FACE_FILE = ".face"
MAX_FACE = 2 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


def config_dir(environ) -> str:
    """The user's config directory: $XDG_CONFIG_HOME when it is an absolute path, else ~/.config."""
    base = environ.get("XDG_CONFIG_HOME") or ""
    if isinstance(base, str) and os.path.isabs(base) and "\0" not in base:
        return os.path.normpath(base)
    return os.path.join(_home(environ), ".config")


def _config_name(name: object) -> tuple[list[str], str]:
    """(directories, file name) of an allow-listed name; UserFileError for anything else."""
    if not isinstance(name, str) or not (name in CONFIG_FILES or AUTOSTART_RE.fullmatch(name)):
        raise UserFileError("Control Center does not write this settings file.")
    parts = name.split("/")
    return parts[:-1], parts[-1]


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc)


def _kind(name: str, dir_fd: int) -> str:
    """missing, link, dir, file or other: what `name` in the open directory is (symlinks are not followed)."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(info.st_mode):
        return "link"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    return "file" if stat.S_ISREG(info.st_mode) else "other"


def _open_config_dir(environ, directories: list[str], create: bool) -> int | None:
    """An open descriptor of <config>/<directories...>, or None when it does not exist and create is False.

    Every directory below <config> is opened without following a symbolic
    link; missing ones are created with mode 0700 when ``create`` is set.
    """
    base = config_dir(environ)
    try:
        if create:
            os.makedirs(base, mode=0o700, exist_ok=True)
        current = os.open(base, _DIR_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UserFileError(f"Your settings folder {base} cannot be used ({_reason(exc)}).") from None
    shown = base
    try:
        for part in directories:
            shown = os.path.join(shown, part)
            kind = _kind(part, current)
            if kind == "missing":
                if not create:
                    os.close(current)
                    return None
                os.mkdir(part, mode=0o700, dir_fd=current)
            elif kind == "link":
                raise UserFileError(f"{shown} is a symbolic link; refusing to write through it.")
            elif kind != "dir":
                raise UserFileError(f"{shown} is not a folder.")
            following = os.open(part, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = following
    except UserFileError:
        os.close(current)
        raise
    except OSError as exc:
        os.close(current)
        raise UserFileError(f"The folder {shown} cannot be used ({_reason(exc)}).") from None
    return current


def _replace_file(dir_fd: int, file_name: str, data: bytes, mode: int, shown: str) -> None:
    """Atomically replace `file_name` in the open directory with `data` (temporary file, fsync, rename)."""
    kind = _kind(file_name, dir_fd)
    if kind == "link":
        raise UserFileError(f"{shown} is a symbolic link; refusing to write through it.")
    if kind not in ("missing", "file"):
        raise UserFileError(f"{shown} is not a regular file; it was not replaced.")
    temporary = f".{file_name}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                             dir_fd=dir_fd)
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, file_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except OSError:
            pass
        raise UserFileError(f"{shown} could not be written ({_reason(exc)}).") from None
    try:
        os.fsync(dir_fd)                  # the rename itself; not every file system can
    except OSError:
        pass


def write_config(environ, name: str, data: bytes) -> str:
    """Replace the settings file `name` (a name of CONFIG_FILES or autostart/<id>.desktop) with `data`.

    Returns the absolute path written. Missing directories are created with
    mode 0700 and the file gets mode 0600. Raises UserFileError, in words
    for the user, when the name is not allow-listed, the content is not bytes
    or larger than MAX_CONFIG, a directory below the config directory or
    the file itself is a symbolic link, the target is not a regular file, or
    writing fails; the previous file is then left as it was.
    """
    directories, file_name = _config_name(name)
    if not isinstance(data, bytes):
        raise UserFileError("The settings could not be saved (the content is not bytes).")
    if len(data) > MAX_CONFIG:
        raise UserFileError(f"The settings file would be larger than {MAX_CONFIG // 1024} KiB; it was not saved.")
    path = os.path.join(config_dir(environ), name)
    dir_fd = _open_config_dir(environ, directories, create=True)
    try:
        _replace_file(dir_fd, file_name, data, 0o600, path)
    finally:
        os.close(dir_fd)
    return path


def remove_config(environ, name: str) -> None:
    """Delete the settings file `name` if it exists (the same names and symlink rules as write_config)."""
    directories, file_name = _config_name(name)
    path = os.path.join(config_dir(environ), name)
    dir_fd = _open_config_dir(environ, directories, create=False)
    if dir_fd is None:
        return
    try:
        kind = _kind(file_name, dir_fd)
        if kind == "missing":
            return
        if kind != "file":
            raise UserFileError(f"{path} is not a regular file; it was not removed.")
        os.unlink(file_name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UserFileError(f"{path} could not be removed ({_reason(exc)}).") from None
    finally:
        os.close(dir_fd)


def write_face(environ, data: bytes) -> str:
    """Replace the account picture ~/.face with `data` (an encoded image); returns its path.

    The file gets mode 0644: the sign-in screen and the accounts service
    read it as another user. A ~/.face that is a symbolic link or not a
    regular file is refused.
    """
    if not isinstance(data, bytes) or not data:
        raise UserFileError("The picture could not be saved (there is no image data).")
    if len(data) > MAX_FACE:
        raise UserFileError("The picture is too large to be saved as the account picture.")
    home = _home(environ)
    path = os.path.join(home, FACE_FILE)
    try:
        dir_fd = os.open(home, _DIR_FLAGS)
    except OSError as exc:
        raise UserFileError(f"Your home folder cannot be used ({_reason(exc)}).") from None
    try:
        _replace_file(dir_fd, FACE_FILE, data, 0o644, path)
    finally:
        os.close(dir_fd)
    return path


# --- the list of recently used files ------------------------------------------------------------------------------

RECENT_FILE = "recently-used.xbel"
MAX_RECENT = 16 * 1024 * 1024
EMPTY_RECENT = (b'<?xml version="1.0" encoding="UTF-8"?>\n<xbel version="1.0"\n'
                b'      xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks"\n'
                b'      xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info"\n></xbel>\n')


def data_dir(environ) -> str:
    """The user's data directory: $XDG_DATA_HOME when it is an absolute path, else ~/.local/share."""
    base = environ.get("XDG_DATA_HOME") or ""
    if isinstance(base, str) and os.path.isabs(base) and "\0" not in base:
        return os.path.normpath(base)
    return os.path.join(_home(environ), ".local", "share")


def recent_files_count(environ) -> int:
    """How many files the freedesktop list of recently used files (recently-used.xbel) names; 0 without one."""
    path = os.path.join(data_dir(environ), RECENT_FILE)
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return 0
        with open(path, "rb") as handle:
            return handle.read(MAX_RECENT).count(b"<bookmark ")
    except OSError:
        return 0


def clear_recent_files(environ) -> int:
    """Empty recently-used.xbel (it stays, as a list without entries); returns how many entries it had.

    Applications that follow the freedesktop convention read this list for
    their "recent files". A list that is a symbolic link or not a regular
    file is refused; no list at all is nothing to clear.
    """
    directory = data_dir(environ)
    path = os.path.join(directory, RECENT_FILE)
    count = recent_files_count(environ)
    try:
        dir_fd = os.open(directory, _DIR_FLAGS)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise UserFileError(f"{directory} cannot be used ({_reason(exc)}).") from None
    try:
        if _kind(RECENT_FILE, dir_fd) == "missing":
            return 0
        _replace_file(dir_fd, RECENT_FILE, EMPTY_RECENT, 0o600, path)
    finally:
        os.close(dir_fd)
    return count
