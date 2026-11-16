"""Reading archives: one class per family (ZIP, TAR, a single compressed file, 7z). No Qt.

``open_archive(path)`` recognises the file and returns an ``Archive``:

* ``listing``          every entry and the folder tree (``model.Listing``)
* ``open_entry(e)``    the bytes of one entry
* ``stream(wanted)``   ``(entry, reader)`` pairs in archive order, in one pass over the file;
                       this is what extraction and testing use
* ``link_target(e)``   where a symbolic link entry points
* ``check_password``   raises ``WrongPassword`` early, before anything is written
* ``test()``           reads everything and returns what is damaged

Nothing here writes to disk. Every reader turns the many exceptions of the
decoders into ``ArchiveError`` with a sentence for the user.
"""

from __future__ import annotations

import lzma
import os
import stat
import struct
import tarfile
import threading
import time
import zipfile
import zlib
from typing import Iterator

from . import formats, names, sevenzip
from .errors import ArchiveError, Cancelled, PasswordRequired, UnsupportedFormat, WrongPassword
from .model import DEVICE, DIR, FIFO, FILE, HARDLINK, OTHER, SYMLINK, Entry, Listing, make_entry

_ZIP_METHODS = {0: "Stored", 8: "Deflate", 9: "Deflate64", 12: "BZip2", 14: "LZMA", 93: "Zstandard", 95: "XZ",
                98: "PPMd", 99: "AES"}
_ZIP_READABLE = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA)
_DECODE_ERRORS = (zlib.error, lzma.LZMAError, EOFError, tarfile.TarError, OSError, ValueError)
_MAX_LINK = 4096
_CHUNK = 1 << 20
_PER_ENTRY_LIMIT = 8           # up to this many wanted entries, 7z is started once per entry


class Issue:
    """Something that is wrong with one entry (or with the archive, when ``path`` is empty)."""

    __slots__ = ("path", "reason")

    def __init__(self, path: str, reason: str) -> None:
        self.path, self.reason = path, reason

    def __repr__(self) -> str:
        return f"Issue({self.path!r}, {self.reason!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Issue) and (self.path, self.reason) == (other.path, other.reason)


class EntryReader:
    """The bytes of one entry. ``read`` raises ArchiveError (or WrongPassword) instead of the decoder's errors."""

    def __init__(self, raw, entry: Entry, on_close=None) -> None:
        self._raw, self.entry, self._on_close = raw, entry, on_close
        self._closed = False

    def read(self, size: int = _CHUNK) -> bytes:
        try:
            return self._raw.read(size)
        except ArchiveError:
            raise
        except zipfile.BadZipFile:
            if self.entry.encrypted:
                raise WrongPassword("The password is not correct, or the data is damaged.") from None
            raise ArchiveError("The data is damaged: the checksum does not match.") from None
        except RuntimeError as error:                      # zipfile: "Bad password for file"
            if "password" in str(error).lower():
                raise WrongPassword() from None
            raise ArchiveError(f"The data cannot be read: {error}") from None
        except _DECODE_ERRORS as error:
            raise ArchiveError(f"The data is damaged or incomplete ({_brief(error)}).") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._raw.close()
        except Exception:                                  # closing a broken decoder must not hide the real error
            pass
        if self._on_close is not None:
            self._on_close()

    def __enter__(self) -> "EntryReader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _brief(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    return text if len(text) <= 120 else text[:117] + "..."


class Archive:
    """An open archive. Subclasses fill ``listing`` and implement ``open_entry`` and ``stream``."""

    def __init__(self, path: str, fmt: formats.Format) -> None:
        self.path = path
        self.format = fmt
        self.listing = Listing()
        self.comment = ""
        self.warning = ""              # shown above the list (a damaged archive that could be read in part)
        try:
            self.file_size = os.path.getsize(path)
        except OSError:
            self.file_size = 0

    @property
    def entries(self) -> list[Entry]:
        return self.listing.entries

    @property
    def encrypted(self) -> bool:
        return self.listing.totals.encrypted > 0

    @property
    def writable(self) -> bool:
        """Whether entries can be added and deleted (the archive is rewritten)."""
        return self.format.writable and not self.encrypted and not self.warning

    def open_entry(self, entry: Entry, password: str | None = None) -> EntryReader:
        raise NotImplementedError

    def stream(self, wanted: set[int] | None = None, password: str | None = None) -> Iterator[tuple[Entry, EntryReader | None]]:
        """``(entry, reader)`` in archive order; ``reader`` is None for everything that is not a file.

        ``wanted`` holds entry indexes (None: all). The reader of one pair
        must be read or closed before the next pair is asked for.
        """
        raise NotImplementedError

    def link_target(self, entry: Entry, password: str | None = None) -> str:
        return entry.link

    def check_password(self, password: str | None) -> None:
        """Raise PasswordRequired or WrongPassword when ``password`` does not open the encrypted entries."""

    def test(self, password: str | None = None, progress=None, cancel=None) -> list[Issue]:
        """Read every entry; returns what could not be read. ``progress(done_bytes, entry)``."""
        issues: list[Issue] = []
        done = 0
        iterator = self.stream(None, password)
        try:
            for entry, reader in iterator:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if reader is None:
                    continue
                try:
                    with reader:
                        while True:
                            data = reader.read(_CHUNK)
                            if not data:
                                break
                            done += len(data)
                            if cancel is not None and cancel.is_set():
                                raise Cancelled()
                            if progress is not None:
                                progress(done, entry)
                except ArchiveError as error:
                    issues.append(Issue(entry.path, str(error)))
        except ArchiveError as error:
            issues.append(Issue("", str(error)))
        finally:
            iterator.close()
        return issues

    def close(self) -> None:
        pass

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# --- ZIP ---------------------------------------------------------------------------------------------------

def _zip_mtime(info: zipfile.ZipInfo) -> float | None:
    extra = info.extra or b""
    position = 0
    while position + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, position)
        position += 4
        if tag == 0x5455 and size >= 5 and position + 5 <= len(extra) and extra[position] & 1:
            return float(struct.unpack_from("<i", extra, position + 1)[0])
        position += size
    try:
        return time.mktime((*info.date_time, 0, 0, -1))
    except (ValueError, OverflowError, TypeError):
        return None


class ZipArchive(Archive):
    def __init__(self, path: str, fmt: formats.Format, cancel=None) -> None:
        super().__init__(path, fmt)
        try:
            self._zip = zipfile.ZipFile(path, "r")
        except zipfile.BadZipFile as error:
            raise ArchiveError(f"The ZIP file is damaged: {_brief(error)}.") from None
        except UnicodeDecodeError:
            raise ArchiveError("The ZIP file is damaged: an entry name is not valid text.") from None
        except (OSError, ValueError, EOFError, struct.error, NotImplementedError) as error:
            raise ArchiveError(f"The ZIP file cannot be read: {_brief(error)}.") from None
        self.comment = (self._zip.comment or b"")[:65536].decode("utf-8", "replace")
        entries = []
        for index, info in enumerate(self._zip.infolist()):
            if cancel is not None and index % 4096 == 0 and cancel.is_set():
                self._zip.close()
                raise Cancelled()
            name = names.zip_name(info)
            if names.zip_uses_backslashes(info):
                name = name.replace("\\", "/")
            unix = (info.external_attr >> 16) & 0xFFFF if info.create_system == 3 else 0
            if name.endswith("/") or stat.S_ISDIR(unix):
                kind = DIR
            elif stat.S_ISLNK(unix):
                kind = SYMLINK
            elif stat.S_ISFIFO(unix):
                kind = FIFO
            elif stat.S_ISCHR(unix) or stat.S_ISBLK(unix):
                kind = DEVICE
            elif stat.S_ISSOCK(unix):
                kind = OTHER
            else:
                kind = FILE
            entries.append(make_entry(
                index, name, kind, size=0 if kind == DIR else info.file_size,
                packed=0 if kind == DIR else info.compress_size, mtime=_zip_mtime(info),
                mode=(unix & 0o7777) if unix else None, crc=self._crc(info) if kind == FILE else None,
                encrypted=bool(info.flag_bits & 0x1),
                method=_ZIP_METHODS.get(info.compress_type, f"Method {info.compress_type}"), handle=info))
        self.listing = Listing.build(entries)

    @staticmethod
    def _crc(info: zipfile.ZipInfo) -> int | None:
        # AES entries of the AE-2 kind store no checksum (the field is zero); the cipher authenticates them.
        return None if info.compress_type == 99 and info.CRC == 0 else info.CRC

    def _needs_7z(self, entry: Entry) -> bool:
        return entry.handle.compress_type not in _ZIP_READABLE

    def _through_7z(self, entry: Entry, password: str | None) -> EntryReader:
        program = sevenzip.find_program()
        if program is None:
            what = "AES encryption" if entry.handle.compress_type == 99 else f"the {entry.method} method"
            raise UnsupportedFormat(f"This entry uses {what}, which {sevenzip.NEEDS}.", needs="7zip")
        if entry.encrypted and not password:
            raise PasswordRequired()
        return EntryReader(_SevenEntry(program, self.path, entry, password), entry)

    def open_entry(self, entry: Entry, password: str | None = None) -> EntryReader:
        info = entry.handle
        if self._needs_7z(entry):
            return self._through_7z(entry, password)
        if entry.encrypted and not password:
            raise PasswordRequired()
        try:
            raw = self._zip.open(info, "r", pwd=password.encode("utf-8") if password else None)
        except RuntimeError as error:
            if "password" in str(error).lower():
                raise (WrongPassword() if password else PasswordRequired()) from None
            raise ArchiveError(f"The entry cannot be read: {_brief(error)}.") from None
        except NotImplementedError:
            return self._through_7z(entry, password)
        except (zipfile.BadZipFile, *_DECODE_ERRORS) as error:
            raise ArchiveError(f"The entry is damaged: {_brief(error)}.") from None
        return EntryReader(raw, entry)

    def stream(self, wanted=None, password=None):
        for entry in self.entries:
            if wanted is not None and entry.index not in wanted:
                continue
            if entry.kind != FILE:
                yield entry, None
                continue
            try:
                reader = self.open_entry(entry, password)
            except ArchiveError as error:
                reader = EntryReader(_Failed(error), entry)
            yield entry, reader

    def link_target(self, entry: Entry, password: str | None = None) -> str:
        if entry.kind != SYMLINK or entry.size > _MAX_LINK:
            return ""
        with self.open_entry(entry, password) as reader:
            data = reader.read(_MAX_LINK + 1)
        return data[:_MAX_LINK].decode("utf-8", "surrogateescape")

    def check_password(self, password: str | None) -> None:
        candidates = [entry for entry in self.entries if entry.encrypted and entry.kind in (FILE, SYMLINK)]
        if not candidates:
            return
        if not password:
            raise PasswordRequired()
        entry = min(candidates, key=lambda e: e.size)
        with self.open_entry(entry, password) as reader:      # the header check of ZipCrypto; 7z for AES
            if self._needs_7z(entry) or entry.size <= 4 * _CHUNK:
                while reader.read(_CHUNK):
                    pass

    def close(self) -> None:
        try:
            self._zip.close()
        except Exception:
            pass


class _Failed:
    """A reader that raises the error that kept an entry from being opened (so one bad entry does not end a pass)."""

    def __init__(self, error: ArchiveError) -> None:
        self._error = error

    def read(self, _size: int = -1) -> bytes:
        raise self._error

    def close(self) -> None:
        pass


# --- TAR ---------------------------------------------------------------------------------------------------

def _tar_kind(member: tarfile.TarInfo) -> str:
    if member.isreg():
        return FILE
    if member.isdir():
        return DIR
    if member.issym():
        return SYMLINK
    if member.islnk():
        return HARDLINK
    if member.ischr() or member.isblk():
        return DEVICE
    if member.isfifo():
        return FIFO
    return OTHER


_DAMAGED = "This archive is damaged or incomplete. Only the entries before the damage are shown."


class TarArchive(Archive):
    def __init__(self, path: str, fmt: formats.Format, cancel=None, progress=None) -> None:
        super().__init__(path, fmt)
        self._lock = threading.Lock()
        self._seekable = True
        self._tar = self._open()
        entries = []
        try:
            for index, member in enumerate(self._tar):
                if index % 512 == 0:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    if progress is not None:
                        progress(index)
                kind = _tar_kind(member)
                entries.append(make_entry(
                    index, member.name, kind, size=member.size if kind == FILE else 0, mtime=float(member.mtime),
                    mode=member.mode & 0o7777, link=member.linkname if kind in (SYMLINK, HARDLINK) else "",
                    handle=index))
        except Cancelled:
            self.close()
            raise
        except _DECODE_ERRORS as error:
            if not entries:
                self.close()
                raise ArchiveError(f"The archive is damaged or incomplete ({_brief(error)}).") from None
            self.warning = _DAMAGED
        else:
            if self._seekable and self._stopped_early():
                self.warning = _DAMAGED
        self.listing = Listing.build(entries)

    def _stopped_early(self) -> bool:
        """Whether tarfile stopped at something that is not the end of the archive.

        tarfile ends its listing quietly at a header it cannot read. A proper
        end is a block of zero bytes or the end of the file.
        """
        try:
            self._tar.fileobj.seek(self._tar.offset)
            block = self._tar.fileobj.read(512)
        except _DECODE_ERRORS:
            return True
        return any(block)

    def _open(self) -> tarfile.TarFile:
        options = {"encoding": "utf-8", "errors": "surrogateescape"}
        try:
            if self.format.compression == "zst":
                try:
                    return tarfile.open(self.path, "r:zst", **options)       # Python 3.14
                except (tarfile.CompressionError, ValueError):
                    self._seekable = False
                    return tarfile.open(fileobj=formats.open_zstd(self.path), mode="r|", **options)
            return tarfile.open(self.path, "r:*", **options)
        except ArchiveError:
            raise
        except _DECODE_ERRORS as error:
            raise ArchiveError(f"The archive cannot be read ({_brief(error)}).") from None

    def _members(self) -> list[tarfile.TarInfo]:
        return self._tar.getmembers()

    def open_entry(self, entry: Entry, password: str | None = None) -> EntryReader:
        if entry.kind != FILE:
            raise ArchiveError("This entry has no content.")
        if not self._seekable:
            for found, reader in self.stream({entry.index}):
                if reader is not None:
                    return reader
            raise ArchiveError("The entry is no longer in the archive.")
        self._lock.acquire()
        try:
            raw = self._tar.extractfile(self._members()[entry.handle])
            if raw is None:
                raise ArchiveError("This entry has no content.")
        except BaseException as error:
            self._lock.release()
            if isinstance(error, _DECODE_ERRORS + (IndexError, KeyError)):
                raise ArchiveError(f"The entry cannot be read ({_brief(error)}).") from None
            raise
        return EntryReader(raw, entry, on_close=self._lock.release)

    def stream(self, wanted=None, password=None):
        left = None if wanted is None else len([index for index in wanted if 0 <= index < len(self.entries)])
        tar = self._open()
        try:
            for index, member in enumerate(tar):
                if index >= len(self.entries):
                    break
                if wanted is not None and index not in wanted:
                    continue
                entry = self.entries[index]
                if entry.name != member.name or (entry.kind == FILE and entry.size != member.size):
                    raise ArchiveError("The archive file changed while it was open. Open it again.")
                if entry.kind == FILE:
                    raw = tar.extractfile(member)
                    yield entry, EntryReader(raw, entry) if raw is not None else None
                else:
                    yield entry, None
                if left is not None:
                    left -= 1
                    if left <= 0:
                        break
        except _DECODE_ERRORS as error:
            raise ArchiveError(f"The archive is damaged or incomplete ({_brief(error)}).") from None
        finally:
            fileobj = getattr(tar, "fileobj", None)
            tar.close()
            if not self._seekable and fileobj is not None:
                try:
                    fileobj.close()
                except Exception:
                    pass

    def close(self) -> None:
        try:
            self._tar.close()
        except Exception:
            pass


# --- one compressed file -----------------------------------------------------------------------------------

def _gzip_mtime(path: str) -> float | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return None
    if len(head) == 8 and head[:2] == b"\x1f\x8b":
        (stamp,) = struct.unpack_from("<I", head, 4)
        if stamp:
            return float(stamp)
    return None


class StreamArchive(Archive):
    """A ``.gz``, ``.bz2``, ``.xz`` or ``.zst`` file that holds one file. Its size is not known before reading."""

    def __init__(self, path: str, fmt: formats.Format) -> None:
        super().__init__(path, fmt)
        if fmt.compression == "zst" and formats.zstd_module() is None:
            raise UnsupportedFormat("Zstandard files cannot be opened on this system: Python has no Zstandard "
                                    "module here.", needs="python3-zstandard")
        mtime = _gzip_mtime(path) if fmt.compression == "gz" else None
        if mtime is None:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
        inner = formats.stream_inner_name(os.path.basename(path))
        self.listing = Listing.build([Entry(index=0, name=inner, path=inner.replace("/", "_"), kind=FILE, size=-1,
                                            packed=self.file_size, mtime=mtime, method=fmt.compression, handle=0)])

    def open_entry(self, entry: Entry, password: str | None = None) -> EntryReader:
        try:
            return EntryReader(formats.open_stream(self.path, self.format.compression), entry)
        except ArchiveError:
            raise
        except _DECODE_ERRORS as error:
            raise ArchiveError(f"The file cannot be read ({_brief(error)}).") from None

    def stream(self, wanted=None, password=None):
        entry = self.entries[0]
        if wanted is None or 0 in wanted:
            yield entry, self.open_entry(entry)


# --- 7z ----------------------------------------------------------------------------------------------------

class _SevenEntry:
    """One entry read by its own 7z process; size and checksum are verified at the end."""

    def __init__(self, program: str, path: str, entry: Entry, password: str | None, name: str | None = None) -> None:
        self._entry = entry
        self._process = sevenzip.read_entry(program, path, name if name is not None else entry.name, password)
        self._count = 0
        self._crc = 0
        self._done = False

    def read(self, size: int = _CHUNK) -> bytes:
        if self._done:
            return b""
        data = self._process.read(size)
        if data:
            self._count += len(data)
            self._crc = zlib.crc32(data, self._crc)
            if 0 <= self._entry.size < self._count:
                self._abort()
                raise ArchiveError("7z returned more data than the archive lists for this entry.")
            return data
        self._done = True
        self._process.finish()
        if self._entry.size >= 0 and self._count != self._entry.size:
            raise ArchiveError("7z could not read this entry (it returned less data than the archive lists).")
        if self._entry.crc is not None and self._count and self._crc != self._entry.crc:
            raise ArchiveError("The data is damaged: the checksum does not match.")
        return b""

    def _abort(self) -> None:
        self._done = True
        self._process.kill()

    def close(self) -> None:
        if not self._done:
            self._abort()


class _SevenChunk:
    """The next ``size`` bytes of a 7z process that writes every file of the archive one after the other."""

    def __init__(self, process: sevenzip.Process, entry: Entry) -> None:
        self._process, self._entry = process, entry
        self._left = max(0, entry.size)
        self._crc = 0
        self.failed = False
        self.finished = self._left == 0

    def read(self, size: int = _CHUNK) -> bytes:
        if self.finished:
            return b""
        data = self._process.read(min(size, self._left))
        if not data:
            self.failed = self.finished = True
            self._process.finish()                              # raises with 7z's own reason, if it gave one
            raise ArchiveError("7z stopped before this entry was complete.")
        self._left -= len(data)
        self._crc = zlib.crc32(data, self._crc)
        if self._left == 0:
            self.finished = True
            if self._entry.crc is not None and self._crc != self._entry.crc:
                self.failed = True
                raise ArchiveError("The data is damaged: the checksum does not match.")
        return data

    def close(self) -> None:
        # The pipe has to stay aligned: what the caller did not read is read and thrown away.
        while not self.finished:
            try:
                self.read(_CHUNK)
            except ArchiveError:
                break


class SevenZipArchive(Archive):
    """7z and RAR archives, read through the 7z program (see ``sevenzip``)."""

    def __init__(self, path: str, fmt: formats.Format, password: str | None = None, cancel=None) -> None:
        super().__init__(path, fmt)
        self._program = sevenzip.find_program()
        if self._program is None:
            raise UnsupportedFormat(f"Opening {fmt.title}s {sevenzip.NEEDS}.", needs="7zip")
        self.header_password = password
        items = sevenzip.list_archive(self._program, path, password, cancel)
        entries = []
        for index, item in enumerate(items):
            kind = item.kind
            entries.append(make_entry(
                index, item.path, kind, size=0 if kind == DIR else item.size, packed=item.packed, mtime=item.mtime,
                mode=item.mode, link=item.link or item.hard_link, crc=item.crc if kind in (FILE, SYMLINK) else None,
                encrypted=item.encrypted, method=item.method, handle=index))
        self.listing = Listing.build(entries)
        if password:
            self.listing.totals.encrypted = max(self.listing.totals.encrypted, 1)   # the list itself is encrypted

    def _password(self, password: str | None) -> str | None:
        return password if password else self.header_password

    def open_entry(self, entry: Entry, password: str | None = None) -> EntryReader:
        password = self._password(password)
        if entry.encrypted and not password:
            raise PasswordRequired()
        return EntryReader(_SevenEntry(self._program, self.path, entry, password), entry)

    def link_target(self, entry: Entry, password: str | None = None) -> str:
        if entry.link or entry.kind != SYMLINK or not 0 < entry.size <= _MAX_LINK:
            return entry.link
        with self.open_entry(entry, password) as reader:
            data = b""
            while len(data) <= _MAX_LINK:
                piece = reader.read(_MAX_LINK + 1)
                if not piece:
                    break
                data += piece
        return data[:_MAX_LINK].decode("utf-8", "surrogateescape")

    def _has_data(self, entry: Entry) -> bool:
        return entry.kind in (FILE, SYMLINK, HARDLINK) and entry.size > 0

    def stream(self, wanted=None, password=None):
        password = self._password(password)
        selected = [e for e in self.entries if wanted is None or e.index in wanted]
        with_data = [e for e in selected if e.kind == FILE and e.size > 0]
        in_one_pass = (len(with_data) > _PER_ENTRY_LIMIT
                       and all(e.crc is not None and e.size >= 0 for e in self.entries if self._has_data(e)))
        process = sevenzip.read_all(self._program, self.path, password) if in_one_pass else None
        left = len(selected)
        try:
            for entry in self.entries:
                if left <= 0:
                    break
                is_wanted = wanted is None or entry.index in wanted
                left -= 1 if is_wanted else 0
                if process is not None and self._has_data(entry):
                    chunk = _SevenChunk(process, entry)
                    if is_wanted and entry.kind == FILE:
                        yield entry, EntryReader(chunk, entry)
                    elif is_wanted:
                        if entry.kind == SYMLINK and not entry.link and entry.size <= _MAX_LINK:
                            try:                      # in a 7z archive the content of a link entry is its target
                                data = b""
                                while not chunk.finished:
                                    data += chunk.read(_MAX_LINK)
                                entry.link = data.decode("utf-8", "surrogateescape")
                            except ArchiveError:
                                pass
                        yield entry, None
                    chunk.close()
                    if chunk.failed:
                        # The pipe can no longer be trusted: every later entry gets its own 7z.
                        process.kill()
                        process = None
                    continue
                if not is_wanted:
                    continue
                if entry.kind != FILE:
                    yield entry, None
                elif entry.size == 0:
                    yield entry, EntryReader(_Empty(), entry)
                else:
                    try:
                        if entry.encrypted and not password:
                            raise PasswordRequired()
                        raw = _SevenEntry(self._program, self.path, entry, password)
                    except ArchiveError as error:
                        raw = _Failed(error)
                    yield entry, EntryReader(raw, entry)
        finally:
            if process is not None:
                process.kill()

    def check_password(self, password: str | None) -> None:
        password = self._password(password)
        candidates = [e for e in self.entries if e.encrypted and e.kind == FILE and e.size > 0]
        if not candidates:
            return
        if not password:
            raise PasswordRequired()
        entry = min(candidates, key=lambda e: e.size)
        with self.open_entry(entry, password) as reader:
            while reader.read(_CHUNK):
                pass

    def test(self, password=None, progress=None, cancel=None) -> list[Issue]:
        try:
            sevenzip.test_archive(self._program, self.path, self._password(password), cancel)
        except PasswordRequired:
            raise
        except ArchiveError as error:
            return [Issue("", str(error))]
        return []


class _Empty:
    def read(self, _size: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        pass


# --- opening -----------------------------------------------------------------------------------------------

def open_archive(path: str | os.PathLike, password: str | None = None, cancel=None, progress=None) -> Archive:
    """Open the archive at ``path``.

    Raises ``UnsupportedFormat`` (not an archive, or one that needs a
    package that is not installed), ``PasswordRequired`` / ``WrongPassword``
    (a 7z or RAR archive whose list of entries is encrypted), ``ArchiveError``
    (damaged, unreadable) or ``Cancelled``. ``progress(count)`` is called
    while a TAR archive is read.
    """
    path = os.fspath(path)
    fmt = formats.detect(path)
    if fmt.family == "zip":
        return ZipArchive(path, fmt, cancel)
    if fmt.family == "tar":
        return TarArchive(path, fmt, cancel, progress)
    if fmt.family == "stream":
        return StreamArchive(path, fmt)
    return SevenZipArchive(path, fmt, password, cancel)
