"""The only files Control Center writes or deletes, all in the user's own home folder.

  ~/Applications/<name>.AppImage                         AppImages the user added
  ~/.local/share/applications/boswas-appimage-<id>.desktop  their menu entries
  ~/.cache/thumbnails/                                   the thumbnail cache (cleanup)

Every path is built here from a validated name and checked to stay inside
its directory (no symlinks are followed out of it). Nothing here runs a
program, and nothing outside these three locations is touched. AppImages run
unsandboxed with the user's permissions: the window says so before adding one.
"""

from __future__ import annotations

import os
import re
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
