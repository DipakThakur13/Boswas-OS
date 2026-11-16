"""The pictures Settings offers as the desktop background (standard library only, no Qt).

Two sources, in this order:

  * the Boswas wallpapers of /usr/share/wallpapers (installed by
    boswas-branding from desktop/presets). Each is a directory

        Boswas-Ocean/metadata.json                    {"KPlugin": {"Name": "Boswas Ocean"}}
        Boswas-Ocean/contents/images/3840x2160.png        the light artwork
        Boswas-Ocean/contents/images_dark/3840x2160.png   the dark artwork

    and is offered once, with the artwork of the current colour mode
    (counterpart() finds the other one when the mode changes). "Boswas", a
    copy of the default preset's wallpaper under a second name, is not
    listed twice;
  * the PNG and JPEG files directly in the user's Pictures folder
    (xdg-user-dirs: XDG_PICTURES_DIR of ~/.config/user-dirs.dirs, else
    ~/Pictures): newest first, at most MAX_PICTURES, none larger than
    MAX_FILE (the shell reads no larger picture). Sub-folders are not
    searched.

A file counts as a picture when it is a regular file that starts with the
PNG or JPEG signature; its name alone is not trusted. Nothing here decodes
an image or writes a file: thumbnails are decoded by the page, off the GUI
thread, and kept in memory in a ThumbnailCache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Hashable, Mapping

SYSTEM_DIR = "/usr/share/wallpapers"
MAX_FILE = 64 * 1024 * 1024              # what boswas-shell reads at most
MAX_PICTURES = 48                        # of the user's own pictures
MAX_PACKAGES = 64
MAX_SCAN = 4000                          # directory entries looked at per folder
EXTENSIONS = (".png", ".jpg", ".jpeg")
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_SIZE_NAME = re.compile(r"^([0-9]{2,5})x([0-9]{2,5})\.(png|jpg|jpeg)$", re.IGNORECASE)


@dataclass(frozen=True)
class Wallpaper:
    path: str                            # the value of [appearance] wallpaper
    name: str                            # "Boswas Ocean", or a file name without its ending
    source: str                          # "boswas" or "pictures"
    light: str = ""                      # the two artworks of a Boswas wallpaper ("" for a plain file)
    dark: str = ""

    def matches(self, path: str) -> bool:
        """Whether ``path`` is this wallpaper (for a Boswas wallpaper: either artwork)."""
        return bool(path) and path in (self.path, self.light, self.dark)


def picture_kind(path: str) -> str:
    """"png" or "jpeg" for a regular file of at most MAX_FILE bytes that starts like one, else ""."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE:
            return ""
        with open(path, "rb") as handle:
            head = handle.read(8)
    except (OSError, ValueError):
        return ""
    return "png" if head == _PNG else "jpeg" if head.startswith(_JPEG) else ""


def check_picture(path: object) -> str:
    """``path`` as it is written to the configuration; ValueError, in words for the user, when it cannot be used."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise ValueError("Choose a picture file.")
    path = os.path.normpath(path)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or len(path.encode("utf-8", "replace")) > 4096:
        raise ValueError("The name of this file cannot be stored in the settings.")
    try:
        info = os.stat(path)
    except OSError as exc:
        raise ValueError(f"The picture cannot be read ({exc.strerror or exc}).") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("That is not a picture file.")
    if info.st_size > MAX_FILE:
        raise ValueError(f"The picture is larger than {MAX_FILE // (1024 * 1024)} MB; the desktop cannot show it.")
    if not picture_kind(path):
        raise ValueError("Only PNG and JPEG pictures can be used as the background.")
    return path


# --- Boswas wallpapers ------------------------------------------------------------------------------------------

def _listing(directory: str) -> list[str]:
    try:
        with os.scandir(directory) as entries:
            names = []
            for entry in entries:
                names.append(entry.name)
                if len(names) >= MAX_SCAN:
                    break
    except OSError:
        return []
    return sorted(names)


def _artwork(directory: str) -> str:
    """The largest picture named WIDTHxHEIGHT.png (or .jpg) in a package's image folder, or ""."""
    best, best_area = "", 0
    for name in _listing(directory):
        match = _SIZE_NAME.fullmatch(name)
        if match is None:
            continue
        area = int(match.group(1)) * int(match.group(2))
        path = os.path.join(directory, name)
        if area > best_area and picture_kind(path):
            best, best_area = path, area
    return best


def _package_name(directory: str, fallback: str) -> str:
    try:
        with open(os.path.join(directory, "metadata.json"), "rb") as handle:
            data = json.loads(handle.read(65536).decode("utf-8"))
        name = data["KPlugin"]["Name"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        name = None
    if not isinstance(name, str) or not name.strip():
        name = fallback.replace("-", " ").replace("_", " ")
    return "".join(ch for ch in name if ch.isprintable()).strip()[:80] or fallback


def _same_content(first: str, second: str) -> bool:
    try:
        if os.path.getsize(first) != os.path.getsize(second):
            return False
        digests = []
        for path in (first, second):
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            digests.append(digest.digest())
    except OSError:
        return False
    return digests[0] == digests[1]


def system_wallpapers(scheme: str = "dark", directory: str = SYSTEM_DIR) -> list[Wallpaper]:
    """The wallpaper packages of ``directory``, each with the artwork for ``scheme``, sorted by name."""
    found: list[Wallpaper] = []
    for entry in _listing(directory):
        if len(found) >= MAX_PACKAGES or entry.startswith("."):
            continue
        package = os.path.join(directory, entry)
        light = _artwork(os.path.join(package, "contents", "images"))
        dark = _artwork(os.path.join(package, "contents", "images_dark"))
        if not (light or dark):
            continue
        light, dark = light or dark, dark or light
        found.append(Wallpaper(dark if scheme == "dark" else light, _package_name(package, entry), "boswas",
                               light, dark))
    # A second name for the same artwork (the "Boswas" copy of the default preset's) is listed once.
    kept: list[Wallpaper] = []
    for wallpaper in sorted(found, key=lambda item: (-len(item.name), item.name.casefold())):
        if not any(_same_content(wallpaper.dark, other.dark) and _same_content(wallpaper.light, other.light)
                   for other in kept):
            kept.append(wallpaper)
    return sorted(kept, key=lambda item: item.name.casefold())


def counterpart(path: str, scheme: str, directory: str = SYSTEM_DIR) -> str:
    """For the artwork of a Boswas wallpaper: the artwork of the same wallpaper for ``scheme``; else ""."""
    if not path:
        return ""
    for wallpaper in system_wallpapers(scheme, directory):
        if path in (wallpaper.light, wallpaper.dark):
            return wallpaper.path
    # Also the copy that is not listed (see system_wallpapers).
    for entry in _listing(directory):
        package = os.path.join(directory, entry)
        light = _artwork(os.path.join(package, "contents", "images"))
        dark = _artwork(os.path.join(package, "contents", "images_dark"))
        if path in (light, dark) and (light or dark):
            return (dark or light) if scheme == "dark" else (light or dark)
    return ""


# --- the user's pictures ----------------------------------------------------------------------------------------

def _home(environ: Mapping[str, str]) -> str | None:
    home = environ.get("HOME") or ""
    return os.path.normpath(home) if home.startswith("/") and "\0" not in home else None


def pictures_dir(environ: Mapping[str, str]) -> str | None:
    """The user's Pictures folder (xdg-user-dirs), or None when there is none or it is the home folder itself."""
    home = _home(environ)
    if home is None:
        return None
    config = environ.get("XDG_CONFIG_HOME") or ""
    config = config if config.startswith("/") and "\0" not in config else os.path.join(home, ".config")
    chosen = None
    try:
        with open(os.path.join(config, "user-dirs.dirs"), "rb") as handle:
            text = handle.read(65536).decode("utf-8", "replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        match = re.fullmatch(r'\s*XDG_PICTURES_DIR\s*=\s*"((?:[^"\\]|\\.)*)"\s*', line)
        if match is None:
            continue
        value = re.sub(r"\\(.)", r"\1", match.group(1))
        if value.startswith("$HOME/"):
            chosen = os.path.join(home, value[len("$HOME/"):])
        elif value.startswith("/"):
            chosen = value
    if chosen is None:
        chosen = os.path.join(home, "Pictures")
    chosen = os.path.normpath(chosen)
    if "\0" in chosen or chosen == home or not os.path.isdir(chosen):
        return None
    return chosen


def user_pictures(environ: Mapping[str, str], limit: int = MAX_PICTURES) -> list[Wallpaper]:
    """The PNG and JPEG files directly in the Pictures folder, newest first, at most ``limit``."""
    directory = pictures_dir(environ)
    if directory is None:
        return []
    candidates = []
    for name in _listing(directory):
        if name.startswith(".") or not name.lower().endswith(EXTENSIONS):
            continue
        path = os.path.join(directory, name)
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
            continue
        try:
            path.encode("utf-8")
            info = os.stat(path)
        except (OSError, UnicodeEncodeError):
            continue
        if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_FILE:
            candidates.append((-info.st_mtime, name.casefold(), path, name))
    found = []
    for _, _, path, name in sorted(candidates):
        if len(found) >= limit:
            break
        if picture_kind(path):
            found.append(Wallpaper(path, os.path.splitext(name)[0][:80], "pictures"))
    return found


def available(environ: Mapping[str, str], scheme: str = "dark", directory: str = SYSTEM_DIR) -> list[Wallpaper]:
    """Everything the background grid offers: the Boswas wallpapers, then the user's pictures."""
    return system_wallpapers(scheme, directory) + user_pictures(environ)


def stamp(path: str) -> tuple[str, int, int] | None:
    """A cache key part that changes when the file does: (path, modification time, size); None if unreadable."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return path, info.st_mtime_ns, info.st_size


class ThumbnailCache:
    """Decoded thumbnails kept in memory: the ``limit`` most recently used, safe to use from several threads.

    ``get(key, make)`` returns the cached value or calls ``make()`` (on the
    calling thread, outside the lock) and keeps its result; a ``make`` that
    raises caches nothing.
    """

    def __init__(self, limit: int = 96) -> None:
        self.limit = max(1, limit)
        self._items: OrderedDict[Hashable, object] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def peek(self, key: Hashable) -> object | None:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        return None

    def get(self, key: Hashable, make: Callable[[], object]) -> object:
        cached = self.peek(key)
        if cached is not None:
            return cached
        value = make()
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
