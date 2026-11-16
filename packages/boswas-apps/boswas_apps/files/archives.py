"""Creating ZIP archives and extracting ZIP and tar archives (no Qt).

An archive is data from somewhere else, so extraction trusts nothing in it:

* every member name is reduced to plain path components; absolute names
  lose their root, and a name with ``..`` in it is refused;
* before anything is written, the folder it goes into is checked to be a
  real folder inside the destination, so a link extracted earlier cannot
  redirect a later member;
* a symbolic link is created only when it is relative and stays inside the
  destination; hard links, devices and FIFOs in a tar are not extracted;
* set-user-ID, set-group-ID and sticky bits are dropped, owners are ignored;
* everything is extracted into a new folder of its own, never over
  existing files.
"""

from __future__ import annotations

import os
import stat
import tarfile
import time
import zipfile

from .fileops import Cancelled, Operation, describe_error, measure, remove_tree
from .text import display_name, split_extension, unique_name

_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz")
_BLOCK = 1024 * 1024
_MAX_LINK = 4096


def is_archive(name: str) -> bool:
    """Whether "Extract all" can open a file of this name."""
    lower = name.lower()
    return lower.endswith(".zip") or lower.endswith(_TAR_SUFFIXES)


def archive_stem(name: str) -> str:
    lower = name.lower()
    for suffix in sorted(_TAR_SUFFIXES + (".zip",), key=len, reverse=True):
        if lower.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return split_extension(name)[0] or name


def safe_parts(name: str) -> list[str] | None:
    """The path components of a member name, or None when the name must not be extracted."""
    name = name.replace("\\", "/")
    if "\0" in name:
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    if len(parts[0]) == 2 and parts[0][1] == ":":       # a DOS drive
        parts = parts[1:]
    if not parts or any(len(part.encode("utf-8", "surrogateescape")) > 255 for part in parts):
        return None
    return parts


def safe_link(parts: list[str], target: str) -> bool:
    """Whether a link at ``parts`` pointing to ``target`` stays inside the extraction folder."""
    if not target or target.startswith("/") or "\0" in target or len(target) > _MAX_LINK:
        return False
    depth = len(parts) - 1
    for part in target.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


class Compress(Operation):
    """Put files and folders into a new ZIP archive next to them."""

    def __init__(self, paths: list[str], archive: str | None = None) -> None:
        super().__init__()
        self.paths = [os.path.abspath(path) for path in paths]
        directory = os.path.dirname(self.paths[0]) if self.paths else ""
        if archive is None and self.paths:
            base = os.path.basename(self.paths[0]) if len(self.paths) == 1 else (os.path.basename(directory) or
                                                                                  "Archive")
            if len(self.paths) == 1 and not os.path.isdir(self.paths[0]):
                base = split_extension(base)[0] or base
            archive = os.path.join(directory, unique_name(
                base + ".zip", lambda name: os.path.lexists(os.path.join(directory, name))))
        self.archive = archive or ""
        self.title = f"Compressing to “{display_name(os.path.basename(self.archive))}”"

    def _run(self) -> None:
        if not self.paths:
            return
        self.progress.preparing = True
        self._emit(force=True)
        for path in self.paths:
            count, size = measure(path, self._check)
            self.progress.items_total += count
            self.progress.bytes_total += size
        self.progress.preparing = False
        partial = self.archive + ".boswas-part"
        try:
            with zipfile.ZipFile(partial, "x", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for path in self.paths:
                    self._add(archive, path, os.path.basename(path))
            os.replace(partial, self.archive)
        except BaseException as error:
            try:
                os.unlink(partial)
            except OSError:
                pass
            if isinstance(error, (OSError, zipfile.BadZipFile, ValueError)) and not isinstance(error, Cancelled):
                self._error(self.archive, error)
                return
            raise
        self.affected.append(self.archive)
        from .fileops import Undo
        self.undo = Undo("create", "Compress", paths=[self.archive])

    def _add(self, archive: zipfile.ZipFile, path: str, name: str) -> None:
        stack = [(path, name)]
        while stack:
            current, arcname = stack.pop()
            self._check()
            self.progress.current = current
            try:
                info = os.lstat(current)
                when = time.localtime(info.st_mtime)[:6]
                if when[0] < 1980:
                    when = (1980, 1, 1, 0, 0, 0)
                if stat.S_ISDIR(info.st_mode):
                    member = zipfile.ZipInfo(arcname + "/", when)
                    member.external_attr = ((info.st_mode & 0xFFFF) << 16) | 0x10
                    archive.writestr(member, b"")
                    with os.scandir(current) as iterator:
                        children = sorted(entry.name for entry in iterator)
                    stack.extend((os.path.join(current, child), f"{arcname}/{child}") for child in reversed(children))
                elif stat.S_ISLNK(info.st_mode):
                    member = zipfile.ZipInfo(arcname, when)
                    member.create_system = 3
                    member.external_attr = (info.st_mode & 0xFFFF) << 16
                    archive.writestr(member, os.fsencode(os.readlink(current)))
                elif stat.S_ISREG(info.st_mode):
                    member = zipfile.ZipInfo(arcname, when)
                    member.create_system = 3
                    member.external_attr = (info.st_mode & 0xFFFF) << 16
                    member.compress_type = zipfile.ZIP_DEFLATED
                    descriptor = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                    with os.fdopen(descriptor, "rb") as source, archive.open(member, "w", force_zip64=True) as sink:
                        while True:
                            self._check()
                            block = source.read(_BLOCK)
                            if not block:
                                break
                            sink.write(block)
                            self.progress.bytes_done += len(block)
                            self._emit()
                else:
                    self._error(current, "Devices, sockets and pipes cannot be archived.")
            except OSError as error:
                self._error(current, error)
            self.progress.items_done += 1
            self._emit()


class Extract(Operation):
    """Extract a ZIP or tar archive into a new folder beside it (or inside ``destination``)."""

    def __init__(self, archive: str, destination: str | None = None) -> None:
        super().__init__()
        self.archive = os.path.abspath(archive)
        self.destination = destination or os.path.dirname(self.archive)
        self.folder = ""
        self.title = f"Extracting “{display_name(os.path.basename(self.archive))}”"

    def _run(self) -> None:
        name = os.path.basename(self.archive)
        if not is_archive(name):
            self._error(self.archive, "This kind of archive cannot be extracted.")
            return
        directory = self.destination
        try:
            folder_name = unique_name(archive_stem(name) or "Extracted",
                                      lambda text: os.path.lexists(os.path.join(directory, text)))
            self.folder = os.path.join(directory, folder_name)
            os.mkdir(self.folder)
        except OSError as error:
            self._error(directory, error)
            return
        self._root = os.path.realpath(self.folder)
        try:
            if name.lower().endswith(".zip"):
                self._zip()
            else:
                self._tar()
        except Cancelled:
            remove_tree(self.folder)
            raise
        except (zipfile.BadZipFile, tarfile.TarError, EOFError, RuntimeError, ValueError, OSError) as error:
            text = describe_error(error) if isinstance(error, OSError) else "The archive is damaged or not readable."
            if isinstance(error, RuntimeError) and "password" in str(error).lower():
                text = "The archive is protected by a password, which is not supported."
            self._error(self.archive, text)
        try:
            empty = not os.listdir(self.folder)
        except OSError:
            empty = False
        if empty:
            try:
                os.rmdir(self.folder)
            except OSError:
                pass
            return
        self.affected.append(self.folder)
        from .fileops import Undo
        self.undo = Undo("create", "Extract", paths=[self.folder])

    # --- shared ---
    def _parent(self, parts: list[str]) -> str:
        """Create and return the folder a member goes into; raises OSError when it is not safely inside."""
        directory = os.path.join(self._root, *parts[:-1])
        os.makedirs(directory, mode=0o755, exist_ok=True)
        real = os.path.realpath(directory)
        if real != self._root and not real.startswith(self._root + os.sep):
            raise PermissionError(13, "The archive tries to write outside the folder", directory)
        return real

    def _refuse(self, name: str, why: str = "It would be written outside the folder; skipped.") -> None:
        self._error(f"{self.archive}: {display_name(name)[:200]}", why)

    def _write(self, source, target: str, mode: int, mtime: float | None) -> None:
        if os.path.lexists(target):
            if os.path.isdir(target) and not os.path.islink(target):
                raise IsADirectoryError(21, "A folder is in the way", target)
            os.unlink(target)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "wb") as sink:
            while True:
                self._check()
                block = source.read(_BLOCK)
                if not block:
                    break
                sink.write(block)
                self.progress.bytes_done += len(block)
                self._emit()
        os.chmod(target, (mode & 0o777) or 0o644)
        if mtime is not None:
            try:
                os.utime(target, (mtime, mtime))
            except (OSError, OverflowError, ValueError):
                pass

    # --- zip ---
    def _zip(self) -> None:
        with zipfile.ZipFile(self.archive) as archive:
            members = archive.infolist()
            self.progress.items_total = len(members)
            self.progress.bytes_total = sum(member.file_size for member in members)
            self._emit(force=True)
            for member in members:
                self._check()
                self.progress.current = member.filename
                try:
                    self._zip_member(archive, member)
                except OSError as error:
                    self._error(f"{self.archive}: {display_name(member.filename)[:200]}", error)
                self.progress.items_done += 1
                self._emit()

    def _zip_member(self, archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> None:
        parts = safe_parts(member.filename)
        if parts is None:
            self._refuse(member.filename)
            return
        if member.flag_bits & 0x1:
            raise RuntimeError("password")
        unix_mode = member.external_attr >> 16 if member.create_system == 3 else 0
        parent = self._parent(parts)
        target = os.path.join(parent, parts[-1])
        if member.is_dir():
            os.makedirs(target, mode=0o755, exist_ok=True)
            return
        try:
            mtime = time.mktime(member.date_time + (0, 0, -1))
        except (ValueError, OverflowError):
            mtime = None
        if stat.S_ISLNK(unix_mode):
            link = archive.read(member)[:_MAX_LINK + 1].decode("utf-8", "surrogateescape")
            if not safe_link(parts, link):
                self._refuse(member.filename, "It is a link to a place outside the folder; skipped.")
                return
            if os.path.lexists(target):
                os.unlink(target)
            os.symlink(link, target)
            return
        with archive.open(member) as source:
            self._write(source, target, unix_mode or 0o644, mtime)

    # --- tar ---
    def _tar(self) -> None:
        try:
            self.progress.bytes_total = os.path.getsize(self.archive)
        except OSError:
            pass
        folders: list[tuple[str, tarfile.TarInfo]] = []
        with open(self.archive, "rb") as raw, tarfile.open(fileobj=raw, mode="r:*") as archive:
            for member in archive:
                self._check()
                self.progress.current = member.name
                self.progress.bytes_done = raw.tell()           # progress by how much of the archive was read
                try:
                    self._tar_member(archive, member, folders)
                except OSError as error:
                    self._error(f"{self.archive}: {display_name(member.name)[:200]}", error)
                self.progress.items_done += 1
                self.progress.items_total = self.progress.items_done
                self._emit()
        for target, member in reversed(folders):
            try:
                os.chmod(target, (member.mode & 0o777) | 0o700)
                os.utime(target, (member.mtime, member.mtime))
            except (OSError, OverflowError, ValueError):
                pass
        self.progress.bytes_done = self.progress.bytes_total

    def _tar_member(self, archive: tarfile.TarFile, member: tarfile.TarInfo, folders: list) -> None:
        parts = safe_parts(member.name)
        if parts is None:
            if member.name.strip("./"):
                self._refuse(member.name)
            return
        parent = self._parent(parts)
        target = os.path.join(parent, parts[-1])
        if member.isdir():
            os.makedirs(target, mode=0o755, exist_ok=True)
            folders.append((target, member))
        elif member.issym():
            if not safe_link(parts, member.linkname):
                self._refuse(member.name, "It is a link to a place outside the folder; skipped.")
                return
            if os.path.lexists(target):
                os.unlink(target)
            os.symlink(member.linkname, target)
        elif member.isreg():
            source = archive.extractfile(member)
            if source is None:
                return
            saved = self.progress.bytes_done
            with source:
                self._write(source, target, member.mode, member.mtime)
            self.progress.bytes_done = saved
        else:
            self._refuse(member.name, "Hard links, devices and pipes are not extracted; skipped.")
