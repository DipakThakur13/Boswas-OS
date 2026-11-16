"""Creating archives, and changing them by writing a new one. No Qt.

``create`` packs files and folders into a new ZIP or TAR archive. ``add``
and ``delete`` change an existing ZIP or TAR archive. All three write a
temporary file beside the target and rename it into place when it is
complete and on disk; if anything fails or the user cancels, the temporary
file is removed and the archive that was there is exactly as before.

Sockets, devices and pipes are never packed. A symbolic link is stored as a
link, or followed when asked (loops are noticed). Owner names and numbers
are not written to TAR archives: an archive is for passing on, and who made
it on which machine is nobody's business.
"""

from __future__ import annotations

import os
import stat
import struct
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field

from . import formats, names
from .errors import ArchiveError, Cancelled
from .extract import Progress, current_umask
from .model import DIR, FILE, SYMLINK, Entry
from .reader import Archive, Issue

_CHUNK = 1 << 20
_ZIP_WRITABLE = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA)
_ZIP64_LIMIT = (1 << 31) - 1
_DROPPED_EXTRA = (0x0001, 0x7075)          # ZIP64 sizes (written anew), Info-ZIP Unicode path (tied to the old name)


@dataclass
class Options:
    format: str = "zip"                # one of formats.CREATABLE
    level: int = 6                     # 0 (store) .. 9 (smallest)
    follow_symlinks: bool = False      # pack what a link points to instead of the link
    exclude_hidden: bool = False       # leave out names that start with a dot (below what was chosen)


@dataclass
class Result:
    path: str
    files: int = 0
    folders: int = 0
    links: int = 0
    bytes: int = 0                     # of file content read
    size: int = 0                      # of the finished archive
    removed: int = 0                   # entries dropped from the old archive (deleted or replaced)
    skipped: list[Issue] = field(default_factory=list)
    failed: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def clean(self) -> bool:
        return not (self.skipped or self.failed or self.cancelled)


class Handler:
    def progress(self, info: Progress) -> None:
        pass


@dataclass(slots=True)
class Item:
    """Something on disk that goes into an archive under ``name``."""

    source: str
    name: str
    kind: str
    info: os.stat_result
    target: str = ""


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _stat(path: str, follow: bool) -> os.stat_result:
    return os.stat(path) if follow else os.lstat(path)


def scan(sources: list[str], options: Options, result: Result, cancel: threading.Event | None = None,
         prefix: str = "", exclude: set[tuple[int, int]] | None = None) -> list[Item]:
    """What packing ``sources`` puts into the archive, parents before their contents.

    ``prefix`` is the folder inside the archive the items go to. ``exclude``
    holds (device, inode) pairs that are never packed (the archive being
    written). What cannot be packed is recorded in ``result``.
    """
    items: list[Item] = []
    exclude = exclude or set()
    taken: set[str] = set()
    follow = options.follow_symlinks
    prefix_parts = names.split(prefix)
    for source in sources:
        _check(cancel)
        source = os.path.abspath(source)
        base = os.path.basename(source.rstrip("/")) or "root"
        name, number = base, 2
        while name in taken:
            name = names.numbered(base, number)
            number += 1
        taken.add(name)
        seen_dirs: set[tuple[int, int]] = set()
        stack = [(source, "/".join([*prefix_parts, name]))]
        while stack:
            _check(cancel)
            path, arcname = stack.pop()
            try:
                info = _stat(path, follow)
            except OSError as error:
                if follow and os.path.islink(path):
                    result.skipped.append(Issue(arcname, "the link points to something that is not there"))
                else:
                    result.failed.append(Issue(arcname, f"cannot be read: {error.strerror or error}"))
                continue
            if (info.st_dev, info.st_ino) in exclude:
                continue
            if stat.S_ISDIR(info.st_mode):
                identity = (info.st_dev, info.st_ino)
                if follow and identity in seen_dirs:
                    result.skipped.append(Issue(arcname, "a link leads back into a folder that is already packed"))
                    continue
                seen_dirs.add(identity)
                items.append(Item(path, arcname, DIR, info))
                try:
                    with os.scandir(path) as listing:
                        children = sorted((child.name for child in listing), reverse=True)
                except OSError as error:
                    result.failed.append(Issue(arcname, f"the folder cannot be read: {error.strerror or error}"))
                    continue
                for child in children:
                    if options.exclude_hidden and child.startswith("."):
                        continue
                    stack.append((os.path.join(path, child), f"{arcname}/{child}"))
            elif stat.S_ISREG(info.st_mode):
                items.append(Item(path, arcname, FILE, info))
            elif stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(path)
                except OSError as error:
                    result.failed.append(Issue(arcname, f"the link cannot be read: {error.strerror or error}"))
                    continue
                items.append(Item(path, arcname, SYMLINK, info, target))
            else:
                result.skipped.append(Issue(arcname, "it is a device, a pipe or a socket, which are never packed"))
    return items


class _Source:
    """The content of a file being packed: exactly ``size`` bytes, counted, cancellable.

    A file that shrinks while it is read is padded with zero bytes and one
    that grows is cut, so the archive stays well formed; both are noted.
    """

    def __init__(self, item: Item, follow: bool, writer: "_Writer") -> None:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW)
        descriptor = os.open(item.source, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("it is no longer a regular file")
        except BaseException:
            os.close(descriptor)
            raise
        self.size = info.st_size
        self.info = info
        self._file = os.fdopen(descriptor, "rb", buffering=0)
        self._left = self.size
        self._writer = writer
        self._item = item
        self.padded = False

    def read(self, size: int = _CHUNK) -> bytes:
        if self._left <= 0:
            return b""
        _check(self._writer.cancel)
        size = min(size if size and size > 0 else _CHUNK, self._left, _CHUNK)
        pieces = []
        missing = size
        while missing > 0:
            piece = self._file.read(missing)
            if not piece:                        # the file became shorter: the rest is zero bytes
                self.padded = True
                piece = bytes(missing)
            pieces.append(piece)
            missing -= len(piece)
        data = pieces[0] if len(pieces) == 1 else b"".join(pieces)
        self._left -= size
        self._writer.count(size, self._item.name)
        return data

    def close(self) -> None:
        self._file.close()


class _Writer:
    """What ZIP and TAR writing share: the temporary file, progress, the result."""

    def __init__(self, output: str, result: Result, handler: Handler, cancel: threading.Event | None,
                 total_bytes: int, total_entries: int) -> None:
        self.output = output
        self.result = result
        self.handler = handler
        self.cancel = cancel
        self.total_bytes = total_bytes
        self.total_entries = total_entries
        self.entries_done = 0
        self.bytes_done = 0
        directory = os.path.dirname(output) or "."
        try:
            descriptor, self.temporary = tempfile.mkstemp(prefix=".bw-", suffix=".part", dir=directory)
        except OSError as error:
            raise ArchiveError(f"The archive cannot be written there: {error.strerror or error}.") from None
        self.file = os.fdopen(descriptor, "w+b")
        info = os.fstat(descriptor)
        self.identity = (info.st_dev, info.st_ino)

    def count(self, size: int, name: str) -> None:
        self.bytes_done += size
        self.handler.progress(Progress(names.display(name), self.bytes_done, self.total_bytes, self.entries_done,
                                       self.total_entries))

    def entry_done(self, name: str) -> None:
        self.entries_done += 1
        self.handler.progress(Progress(names.display(name), self.bytes_done, self.total_bytes, self.entries_done,
                                       self.total_entries))

    def commit(self, mode: int | None = None) -> None:
        """Put the finished file in place of ``output``."""
        try:
            self.file.flush()
            os.fsync(self.file.fileno())
            os.fchmod(self.file.fileno(), mode if mode is not None else 0o666 & ~current_umask())
            self.result.size = os.fstat(self.file.fileno()).st_size
            self.file.close()
            os.replace(self.temporary, self.output)
        except OSError as error:
            self.discard()
            raise ArchiveError(f"The archive could not be saved: {error.strerror or error}.") from None

    def discard(self) -> None:
        try:
            self.file.close()
        except OSError:
            pass
        try:
            os.unlink(self.temporary)
        except OSError:
            pass


# --- ZIP ---------------------------------------------------------------------------------------------------

def _zip_compression(level: int) -> tuple[int, int | None]:
    if level <= 0:
        return zipfile.ZIP_STORED, None
    return zipfile.ZIP_DEFLATED, max(1, min(9, level))


def _set_level(info: zipfile.ZipInfo, compression: int, level: int | None) -> None:
    info.compress_type = compression
    if hasattr(zipfile.ZipInfo, "compress_level"):          # Python 3.13
        info.compress_level = level
    else:
        info._compresslevel = level


def _zip_name(name: str, result: Result) -> str:
    try:
        name.encode("utf-8")
        return name
    except UnicodeEncodeError:
        fixed = names.display(name)
        result.notes.append(f"“{fixed}” has a name that is not valid text; it was stored with replacement "
                            "characters.")
        return fixed


def _zip_time(seconds: float) -> tuple:
    try:
        stamp = time.localtime(seconds)[:6]
    except (OverflowError, OSError, ValueError):
        return (1980, 1, 1, 0, 0, 0)
    if stamp[0] < 1980:
        return (1980, 1, 1, 0, 0, 0)
    if stamp[0] > 2107:
        return (2107, 12, 31, 23, 59, 59)
    return stamp


def _zip_info(item: Item, name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name + ("/" if item.kind == DIR else ""), _zip_time(item.info.st_mtime))
    info.create_system = 3
    info.external_attr = (item.info.st_mode & 0xFFFF) << 16
    if item.kind == DIR:
        info.external_attr |= 0x10
    return info


def _write_zip_items(archive: zipfile.ZipFile, items: list[Item], options: Options, writer: _Writer) -> None:
    compression, level = _zip_compression(options.level)
    result = writer.result
    for item in items:
        _check(writer.cancel)
        name = _zip_name(item.name, result)
        info = _zip_info(item, name)
        if item.kind == DIR:
            _set_level(info, zipfile.ZIP_STORED, None)
            archive.writestr(info, b"")
            result.folders += 1
        elif item.kind == SYMLINK:
            _set_level(info, zipfile.ZIP_STORED, None)
            archive.writestr(info, os.fsencode(item.target))
            result.links += 1
        else:
            try:
                source = _Source(item, options.follow_symlinks, writer)
            except OSError as error:
                result.failed.append(Issue(item.name, f"cannot be read: {error.strerror or error}"))
                continue
            try:
                _set_level(info, compression, level)
                info.file_size = source.size
                with archive.open(info, "w", force_zip64=source.size >= _ZIP64_LIMIT) as target:
                    while True:
                        data = source.read(_CHUNK)
                        if not data:
                            break
                        target.write(data)
            finally:
                source.close()
            if source.padded:
                result.notes.append(f"“{names.display(item.name)}” changed while it was read.")
            result.files += 1
            result.bytes += source.size
        writer.entry_done(item.name)


def _clean_extra(extra: bytes) -> bytes:
    kept = b""
    position = 0
    while position + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, position)
        if position + 4 + size > len(extra):
            break
        if tag not in _DROPPED_EXTRA:
            kept += extra[position:position + 4 + size]
        position += 4 + size
    return kept


def _copy_zip(source: zipfile.ZipFile, target: zipfile.ZipFile, keep, writer: _Writer) -> None:
    for old in source.infolist():
        _check(writer.cancel)
        if not keep(old):
            writer.result.removed += 1
            continue
        name = names.zip_name(old)
        if names.zip_uses_backslashes(old):
            name = name.replace("\\", "/")
        info = zipfile.ZipInfo(_zip_name(name, writer.result), old.date_time)
        info.create_system = old.create_system
        info.external_attr = old.external_attr
        info.comment = old.comment
        info.extra = _clean_extra(old.extra or b"")
        _set_level(info, old.compress_type if old.compress_type in _ZIP_WRITABLE else zipfile.ZIP_DEFLATED, None)
        if old.is_dir():
            target.writestr(info, b"")
        else:
            try:
                with source.open(old, "r") as reader, target.open(
                        info, "w", force_zip64=old.file_size >= _ZIP64_LIMIT) as out:
                    while True:
                        _check(writer.cancel)
                        data = reader.read(_CHUNK)
                        if not data:
                            break
                        out.write(data)
                        writer.count(len(data), name)
            except NotImplementedError:
                raise ArchiveError("This archive cannot be changed: an entry uses a compression method that "
                                   "only 7z reads.") from None
            except RuntimeError as error:
                raise ArchiveError(f"This archive cannot be changed: {error}.") from None
        writer.entry_done(name)


# --- TAR ---------------------------------------------------------------------------------------------------

def _open_tar_writer(fmt: formats.Format, level: int, fileobj) -> tarfile.TarFile:
    level = max(0, min(9, level))
    if fmt.compression == "gz":
        return tarfile.open(fileobj=fileobj, mode="w:gz", format=tarfile.PAX_FORMAT, compresslevel=level)
    if fmt.compression == "bz2":
        return tarfile.open(fileobj=fileobj, mode="w:bz2", format=tarfile.PAX_FORMAT, compresslevel=max(1, level))
    if fmt.compression == "xz":
        return tarfile.open(fileobj=fileobj, mode="w:xz", format=tarfile.PAX_FORMAT, preset=level)
    if fmt.compression == "":
        return tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT)
    raise ArchiveError(f"{fmt.title}s cannot be written.")


def _tar_info(item: Item) -> tarfile.TarInfo:
    info = tarfile.TarInfo(item.name)
    info.mtime = item.info.st_mtime
    info.mode = stat.S_IMODE(item.info.st_mode)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if item.kind == DIR:
        info.type = tarfile.DIRTYPE
    elif item.kind == SYMLINK:
        info.type = tarfile.SYMTYPE
        info.linkname = item.target
    else:
        info.type = tarfile.REGTYPE
    return info


def _write_tar_items(archive: tarfile.TarFile, items: list[Item], options: Options, writer: _Writer) -> None:
    result = writer.result
    for item in items:
        _check(writer.cancel)
        info = _tar_info(item)
        if item.kind == DIR:
            archive.addfile(info)
            result.folders += 1
        elif item.kind == SYMLINK:
            archive.addfile(info)
            result.links += 1
        else:
            try:
                source = _Source(item, options.follow_symlinks, writer)
            except OSError as error:
                result.failed.append(Issue(item.name, f"cannot be read: {error.strerror or error}"))
                continue
            try:
                info.size = source.size
                archive.addfile(info, source)
            finally:
                source.close()
            if source.padded:
                result.notes.append(f"“{names.display(item.name)}” changed while it was read.")
            result.files += 1
            result.bytes += source.size
        writer.entry_done(item.name)


class _Counted:
    """Reads of an old archive's member, counted for the progress and cancellable."""

    def __init__(self, raw, name: str, writer: _Writer) -> None:
        self._raw, self._name, self._writer = raw, name, writer

    def read(self, size: int = _CHUNK) -> bytes:
        _check(self._writer.cancel)
        data = self._raw.read(size)
        if data:
            self._writer.count(len(data), self._name)
        return data


def _copy_tar(source: tarfile.TarFile, target: tarfile.TarFile, keep, writer: _Writer) -> None:
    for index, member in enumerate(source):
        _check(writer.cancel)
        if not keep(index, member):
            writer.result.removed += 1
            continue
        if member.isreg():
            if member.type == tarfile.GNUTYPE_SPARSE or getattr(member, "sparse", None):
                # The holes are read as zero bytes here; the copy is an ordinary file.
                member.type = tarfile.REGTYPE
                member.sparse = None
                member.pax_headers = {key: value for key, value in member.pax_headers.items()
                                      if not key.startswith("GNU.sparse")}
            raw = source.extractfile(member)
            target.addfile(member, _Counted(raw, member.name, writer) if raw is not None else None)
        else:
            target.addfile(member)
        writer.entry_done(member.name)


# --- the three operations ----------------------------------------------------------------------------------

def _totals(items: list[Item]) -> int:
    return sum(item.info.st_size for item in items if item.kind == FILE)


def _finish(writer: _Writer, result: Result, action, mode: int | None = None) -> Result:
    try:
        action()
    except Cancelled:
        writer.discard()
        result.cancelled = True
        return result
    except ArchiveError:
        writer.discard()
        raise
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, ValueError, struct.error) as error:
        writer.discard()
        reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
        raise ArchiveError(f"The archive could not be written: {reason}.") from None
    except BaseException:
        writer.discard()
        raise
    writer.commit(mode)
    return result


def create(output: str | os.PathLike, sources: list[str], options: Options | None = None,
           handler: Handler | None = None, cancel: threading.Event | None = None) -> Result:
    """Pack ``sources`` (files and folders) into the new archive ``output``.

    An existing ``output`` is replaced only when the new archive is complete.
    Raises ``ArchiveError`` when nothing could be written; what could not be
    packed is in the ``Result``.
    """
    options = options or Options()
    output = os.path.abspath(os.fspath(output))
    fmt = formats.FORMATS.get(options.format)
    if fmt is None or options.format not in formats.CREATABLE:
        raise ArchiveError(f"Archives of the kind “{options.format}” cannot be created.")
    result = Result(path=output)
    handler = handler or Handler()
    writer = _Writer(output, result, handler, cancel, 0, 0)
    exclude = {writer.identity}
    try:
        existing = os.stat(output)
        exclude.add((existing.st_dev, existing.st_ino))
    except OSError:
        pass

    def work() -> None:
        items = scan(sources, options, result, cancel, exclude=exclude)
        if not items:
            raise ArchiveError("There is nothing to pack: none of the chosen files could be read.")
        writer.total_bytes, writer.total_entries = _totals(items), len(items)
        if fmt.family == "zip":
            with zipfile.ZipFile(writer.file, "w", allowZip64=True, strict_timestamps=False) as archive:
                _write_zip_items(archive, items, options, writer)
        else:
            with _open_tar_writer(fmt, options.level, writer.file) as archive:
                _write_tar_items(archive, items, options, writer)

    return _finish(writer, result, work)


def _require_writable(archive: Archive) -> None:
    if archive.encrypted:
        raise ArchiveError("A password-protected archive cannot be changed here.")
    if archive.warning:
        raise ArchiveError("A damaged archive cannot be changed.")
    if not archive.format.writable:
        raise ArchiveError(f"{archive.format.title}s cannot be changed; they can only be read.")


def _old_mode(path: str) -> int | None:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def replaced_by(archive: Archive, sources: list[str], folder: str = "", options: Options | None = None) -> list[str]:
    """The entries of ``archive`` that adding ``sources`` to ``folder`` would replace (to ask before doing it)."""
    options = options or Options()
    scratch = Result(path=archive.path)
    new = {item.name for item in scan(sources, options, scratch, prefix=folder) if item.kind != DIR}
    return sorted({entry.path for entry in archive.entries if entry.kind != DIR and entry.path in new})


def add(archive: Archive, sources: list[str], folder: str = "", options: Options | None = None,
        handler: Handler | None = None, cancel: threading.Event | None = None) -> Result:
    """Add ``sources`` to the folder ``folder`` of ``archive``; entries with the same path are replaced."""
    _require_writable(archive)
    options = options or Options()
    result = Result(path=archive.path)
    handler = handler or Handler()
    writer = _Writer(archive.path, result, handler, cancel, 0, 0)
    exclude = {writer.identity}
    try:
        existing = os.stat(archive.path)
        exclude.add((existing.st_dev, existing.st_ino))
    except OSError:
        pass
    family = archive.format.family

    def work() -> None:
        items = scan(sources, options, result, cancel, prefix=folder, exclude=exclude)
        if not items:
            raise ArchiveError("There is nothing to add: none of the chosen files could be read.")
        new_paths = {item.name for item in items}
        old_dirs = {entry.path for entry in archive.entries if entry.kind == DIR and not entry.synthetic}
        # A folder that is already listed is not listed twice.
        items[:] = [item for item in items if not (item.kind == DIR and item.name in old_dirs)]
        kept_bytes = sum(entry.size for entry in archive.entries
                         if entry.kind == FILE and entry.size > 0 and entry.path not in new_paths)
        writer.total_bytes = _totals(items) + kept_bytes
        writer.total_entries = len(items) + len(archive.entries)
        if family == "zip":
            with zipfile.ZipFile(archive.path, "r") as old, zipfile.ZipFile(
                    writer.file, "w", allowZip64=True, strict_timestamps=False) as new:
                new.comment = old.comment

                def keep(info: zipfile.ZipInfo) -> bool:
                    name = names.zip_name(info)
                    if names.zip_uses_backslashes(info):
                        name = name.replace("\\", "/")
                    path = "/".join(names.split(name))
                    return info.is_dir() or path not in new_paths

                _copy_zip(old, new, keep, writer)
                _write_zip_items(new, items, options, writer)
        else:
            with tarfile.open(archive.path, "r:*", encoding="utf-8", errors="surrogateescape") as old, \
                    _open_tar_writer(archive.format, options.level, writer.file) as new:
                _copy_tar(old, new, lambda _index, member: member.isdir()
                          or "/".join(names.split(member.name)) not in new_paths, writer)
                _write_tar_items(new, items, options, writer)

    return _finish(writer, result, work, _old_mode(archive.path))


def delete(archive: Archive, entries: list[Entry], handler: Handler | None = None,
           cancel: threading.Event | None = None, level: int = 6) -> Result:
    """Remove ``entries`` from ``archive`` (give every entry below a folder to remove the folder)."""
    _require_writable(archive)
    result = Result(path=archive.path)
    handler = handler or Handler()
    doomed = {entry.index for entry in entries if not entry.synthetic}
    if not doomed:
        raise ArchiveError("Nothing was chosen to delete.")
    kept_bytes = sum(entry.size for entry in archive.entries
                     if entry.kind == FILE and entry.size > 0 and entry.index not in doomed)
    writer = _Writer(archive.path, result, handler, cancel, kept_bytes, len(archive.entries) - len(doomed))
    family = archive.format.family

    def work() -> None:
        if family == "zip":
            with zipfile.ZipFile(archive.path, "r") as old, zipfile.ZipFile(
                    writer.file, "w", allowZip64=True, strict_timestamps=False) as new:
                new.comment = old.comment
                positions = {id(info): index for index, info in enumerate(old.infolist())}
                if len(positions) != len(archive.entries):
                    raise ArchiveError("The archive file changed while it was open. Open it again.")
                _copy_zip(old, new, lambda info: positions[id(info)] not in doomed, writer)
        else:
            with tarfile.open(archive.path, "r:*", encoding="utf-8", errors="surrogateescape") as old, \
                    _open_tar_writer(archive.format, level, writer.file) as new:
                _copy_tar(old, new, lambda index, _member: index not in doomed, writer)

    return _finish(writer, result, work, _old_mode(archive.path))
