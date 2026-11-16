"""The freedesktop.org Trash specification, version 1.0 (no Qt).

* The home trash is ``$XDG_DATA_HOME/Trash``. A file on another volume goes
  to that volume's trash: ``$topdir/.Trash/$uid`` when an administrator
  made a proper ``.Trash`` (a real directory with the sticky bit), else
  ``$topdir/.Trash-$uid``, created on demand. When neither can be used the
  file is not trashed (``TrashUnavailable``); the caller decides what then.
* Every trashed file has ``files/NAME`` and ``info/NAME.trashinfo`` with
  its original path (URL-escaped; relative to ``$topdir`` in a volume
  trash) and the deletion date. The info file is created first, with
  O_EXCL, which is what makes a name ours; a name that is taken gets
  ``.2``, ``.3``, ...
* What is read back from a trash directory is not trusted: a volume trash
  can come from anywhere, so an original path that would leave its volume
  is refused, and nothing is restored through a symbolic link.

The optional ``directorysizes`` cache is not written (and a stale one is
removed when the trash is emptied).
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, unquote_to_bytes

from . import volumes
from .text import truncate_name

_INFO_SUFFIX = ".trashinfo"
_MAX_INFO = 64 * 1024
_MAX_TRIES = 100_000


class TrashError(Exception):
    """Something about the Trash, in words fit for the user."""


class TrashUnavailable(TrashError):
    """The volume of the file has no trash directory this user can use."""


@dataclass(eq=False)
class TrashItem:
    trash_dir: str
    name: str                           # the name inside files/ and (plus .trashinfo) inside info/
    original_path: str
    deleted: float = 0.0                # seconds since the epoch, 0 when unknown

    @property
    def files_path(self) -> str:
        return os.path.join(self.trash_dir, "files", self.name)

    @property
    def info_path(self) -> str:
        return os.path.join(self.trash_dir, "info", self.name + _INFO_SUFFIX)


def find_topdir(path: str) -> str:
    """The mount point of the file system that holds the directory ``path``."""
    current = os.path.realpath(path)
    device = os.stat(current).st_dev
    while True:
        up = os.path.dirname(current)
        if up == current:
            return current
        try:
            if os.stat(up).st_dev != device:
                return current
        except OSError:
            return current
        current = up


def _default_mount_points() -> list[str]:
    return [mount.mount_point for mount in volumes.effective(volumes.read_mounts())
            if mount.fstype not in volumes.PSEUDO_FILESYSTEMS or mount.mount_point == "/"]


def parse_trashinfo(text: str) -> tuple[bytes, float] | None:
    """(original path as bytes, deletion time) of a ``.trashinfo`` text, or None when it is not one."""
    lines = text.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or lines[index].strip() != "[Trash Info]":
        return None
    path: bytes | None = None
    deleted = 0.0
    for line in lines[index + 1:]:
        if line.startswith("["):
            break
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if key == "Path" and path is None:
            path = unquote_to_bytes(value.strip())
        elif key == "DeletionDate" and not deleted:
            try:
                deleted = time.mktime(time.strptime(value.strip()[:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, OverflowError):
                deleted = 0.0
    if not path or b"\0" in path:
        return None
    return path, deleted


class Trash:
    """The trash directories of one user.

    ``topdir_of(directory)`` and ``mount_points()`` exist so that tests can
    declare any directory a volume of its own.
    """

    def __init__(self, home_trash: str | None = None, *, uid: int | None = None,
                 topdir_of: Callable[[str], str] = find_topdir,
                 mount_points: Callable[[], list[str]] = _default_mount_points) -> None:
        if home_trash is None:
            data_home = os.environ.get("XDG_DATA_HOME") or ""
            if not data_home.startswith("/"):
                data_home = os.path.join(os.path.expanduser("~"), ".local", "share")
            home_trash = os.path.join(data_home, "Trash")
        self.home_trash = os.path.abspath(home_trash)
        self.uid = os.getuid() if uid is None else uid
        self._topdir_of = topdir_of
        self._mount_points = mount_points

    # --- directories ---
    @staticmethod
    def _prepare(trash_dir: str) -> bool:
        try:
            for name in ("files", "info"):
                os.makedirs(os.path.join(trash_dir, name), mode=0o700, exist_ok=True)
        except OSError:
            return False
        return True

    def _home_topdir(self) -> str:
        os.makedirs(self.home_trash, mode=0o700, exist_ok=True)
        return self._topdir_of(self.home_trash)

    def _usable(self, directory: str) -> bool:
        """A directory (not a link) that belongs to this user."""
        try:
            info = os.lstat(directory)
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode) and info.st_uid == self.uid

    def volume_trash(self, topdir: str, create: bool = False) -> str | None:
        """The trash directory of this user on the volume ``topdir``, or None."""
        shared = os.path.join(topdir, ".Trash")
        try:
            info = os.lstat(shared)
            shared_ok = stat.S_ISDIR(info.st_mode) and bool(info.st_mode & stat.S_ISVTX)
        except OSError:
            shared_ok = False
        if shared_ok:
            mine = os.path.join(shared, str(self.uid))
            if create and not os.path.lexists(mine):
                try:
                    os.mkdir(mine, 0o700)
                except OSError:
                    pass
            if self._usable(mine):
                return mine
        own = os.path.join(topdir, f".Trash-{self.uid}")
        if create and not os.path.lexists(own):
            try:
                os.mkdir(own, 0o700)
            except OSError:
                return None
        return own if self._usable(own) else None

    def trash_dirs(self) -> list[tuple[str, str | None]]:
        """Every trash directory of this user that exists, as (directory, topdir); topdir None is the home trash."""
        found: list[tuple[str, str | None]] = []
        if os.path.isdir(self.home_trash):
            found.append((self.home_trash, None))
        seen = {self.home_trash}
        try:
            points = self._mount_points()
        except OSError:
            points = []
        for topdir in points:
            # Both kinds can exist on one volume (the administrator added .Trash later): read both.
            candidates = [os.path.join(topdir, f".Trash-{self.uid}")]
            if self.volume_trash(topdir) not in (None, candidates[0]):
                candidates.insert(0, os.path.join(topdir, ".Trash", str(self.uid)))
            for directory in candidates:
                if directory not in seen and self._usable(directory):
                    seen.add(directory)
                    found.append((directory, topdir))
        return found

    def contains(self, path: str) -> bool:
        """Whether ``path`` is a trash directory or lies inside one."""
        path = os.path.abspath(path)
        for directory, _topdir in self.trash_dirs():
            if path == directory or path.startswith(directory + os.sep):
                return True
        return False

    # --- trashing ---
    def trash(self, path: str) -> TrashItem:
        """Move ``path`` (a file, a folder, or a link itself) to the trash of its volume."""
        parent, name = os.path.split(os.path.abspath(path))
        if not name:
            raise TrashError("The file system root cannot be moved to the Trash.")
        path = os.path.join(os.path.realpath(parent), name)
        if not os.path.lexists(path):
            raise FileNotFoundError(2, "No such file or folder", path)
        home_top = self._home_topdir()
        if path == self.home_trash or self.home_trash.startswith(path + os.sep):
            raise TrashError("The Trash cannot be moved into itself.")
        topdir = self._topdir_of(os.path.dirname(path))
        for directory in (self.home_trash, self.volume_trash(topdir)):
            if directory and (path == directory or path.startswith(directory + os.sep)):
                raise TrashError("This item is already in the Trash.")
        if topdir == home_top:
            trash_dir, recorded = self.home_trash, path
        else:
            trash_dir = self.volume_trash(topdir, create=True)
            if trash_dir is None:
                raise TrashUnavailable("This volume has no Trash.")
            recorded = os.path.relpath(path, topdir)
        if not self._prepare(trash_dir):
            raise TrashUnavailable("The Trash of this volume cannot be written.")
        deleted = time.time()
        reserved = self._reserve(trash_dir, name, recorded, deleted)
        item = TrashItem(trash_dir, reserved, path, float(int(deleted)))
        try:
            os.rename(path, item.files_path)
        except OSError:
            try:
                os.unlink(item.info_path)
            except OSError:
                pass
            raise
        return item

    def _reserve(self, trash_dir: str, name: str, recorded: str, deleted: float) -> str:
        content = ("[Trash Info]\nPath={}\nDeletionDate={}\n".format(
            quote(os.fsencode(recorded), safe="/"),
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(deleted)))).encode("ascii")
        room = 255 - len(_INFO_SUFFIX)
        for number in range(1, _MAX_TRIES):
            suffix = "" if number == 1 else f".{number}"
            candidate = truncate_name(name, room - len(suffix)) + suffix
            info_path = os.path.join(trash_dir, "info", candidate + _INFO_SUFFIX)
            try:
                descriptor = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     0o600)
            except FileExistsError:
                continue
            try:
                os.write(descriptor, content)
            finally:
                os.close(descriptor)
            if os.path.lexists(os.path.join(trash_dir, "files", candidate)):
                os.unlink(info_path)             # a file without a record sits there: leave it alone
                continue
            return candidate
        raise TrashError("The Trash is full of items with this name.")

    # --- reading ---
    def items(self) -> list[TrashItem]:
        """Everything in every trash directory of this user. Broken records are skipped."""
        result: list[TrashItem] = []
        for trash_dir, topdir in self.trash_dirs():
            info_dir = os.path.join(trash_dir, "info")
            try:
                names = [entry.name for entry in os.scandir(info_dir) if entry.name.endswith(_INFO_SUFFIX)]
            except OSError:
                continue
            for info_name in names:
                item = self._read(trash_dir, topdir, info_name[:-len(_INFO_SUFFIX)])
                if item is not None:
                    result.append(item)
        return result

    def _read(self, trash_dir: str, topdir: str | None, name: str) -> TrashItem | None:
        if not name or "/" in name or name in (".", ".."):
            return None
        item = TrashItem(trash_dir, name, "")
        try:
            descriptor = os.open(item.info_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            data = os.read(descriptor, _MAX_INFO)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        parsed = parse_trashinfo(data.decode("utf-8", "surrogateescape"))
        if parsed is None or not os.path.lexists(item.files_path):
            return None
        recorded = os.fsdecode(parsed[0])
        if topdir is None:
            if not recorded.startswith("/"):
                return None
            original = os.path.normpath(recorded)
        else:
            original = os.path.normpath(os.path.join(topdir, recorded))
            base = topdir.rstrip("/")
            if not original.startswith(base + "/"):
                return None                       # would be restored outside its volume
        if original.startswith("//"):
            original = original[1:]
        item.original_path = original
        item.deleted = parsed[1]
        return item

    # --- restoring and deleting ---
    def restore(self, item: TrashItem, destination: str | None = None, *, replace: bool = False) -> str:
        """Move ``item`` back to where it was (or to ``destination``); returns the path.

        Raises FileExistsError when something is there and ``replace`` is
        false. Missing parent folders are created. A folder is never
        replaced, only a file or a link.
        """
        target = destination or item.original_path
        if not target.startswith("/"):
            raise TrashError("This item has no place to go back to.")
        if os.path.lexists(target):
            if not replace or (os.path.isdir(target) and not os.path.islink(target)):
                raise FileExistsError(17, "Something with this name is already there", target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.rename(item.files_path, target)
        try:
            os.unlink(item.info_path)
        except OSError:
            pass
        return target

    def forget(self, item: TrashItem) -> None:
        """Remove the record of an item whose file is gone (deleted or moved by the caller)."""
        try:
            os.unlink(item.info_path)
        except OSError:
            pass

    def clean(self, trash_dir: str) -> None:
        """After emptying: drop the size cache, which would be stale."""
        try:
            os.unlink(os.path.join(trash_dir, "directorysizes"))
        except OSError:
            pass
