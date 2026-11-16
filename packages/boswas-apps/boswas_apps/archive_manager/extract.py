"""Extraction that cannot leave its destination. No Qt.

``extract(archive, destination, ...)`` writes the entries of an archive
below one folder. Whatever the archive says, nothing is created, changed or
read outside that folder:

* Every path is walked component by component from an open descriptor of
  the destination, with ``O_NOFOLLOW``: a symbolic link that is in the way
  (one that was there before, or one a hostile archive would like to plant)
  is never followed. A name with ``..``, a NUL character or nothing in it is
  not extracted; a leading ``/`` is dropped; control characters are replaced.
* Files are written to a temporary name beside their place and renamed into
  it, so an existing file is replaced as a whole and an existing link is
  replaced, not written through.
* Symbolic links are created last, after every file is in place, and only
  if their target stays inside the destination; when all links exist, each
  one is resolved for real and removed if it leads outside (links can be
  chained). Hard links are made only to files this run extracted.
* Device nodes, pipes and sockets are skipped. Set-uid, set-gid and sticky
  bits are dropped; permission bits are kept, masked by the umask (folders
  always stay usable by their owner). Nothing is chowned.
* Sizes are checked before and during the work: a total that is very large,
  very large for what the archive weighs, or larger than the free space
  needs the user's word (``Handler.confirm_size``), and data whose size the
  archive did not announce is counted as it arrives.

Everything that was not extracted is listed in the ``Result``, with the reason.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import threading
from dataclasses import dataclass, field

from . import names
from .errors import ArchiveError, Cancelled, PasswordRequired, WrongPassword
from .model import DEVICE, DIR, FIFO, FILE, HARDLINK, SYMLINK, Entry, human_size
from .reader import Archive, Issue

ASK, REPLACE, SKIP, RENAME = "ask", "replace", "skip", "rename"
REPLACE_ALL, SKIP_ALL, RENAME_ALL, CANCEL = "replace-all", "skip-all", "rename-all", "cancel"
POLICIES = (ASK, REPLACE, SKIP, RENAME)

MIB = 1024 * 1024
GIB = 1024 * MIB
_CHUNK = 1 << 20
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_MAX_OPEN_DIRS = 96
_MAX_ISSUES = 10_000                 # a hostile archive must not fill memory with complaints
_MAX_TIME = 2 ** 40                  # later than any real file time, in seconds


@dataclass(frozen=True)
class Limits:
    """When extraction stops to ask (see ``SizeWarning``)."""

    max_total: int = 8 * GIB             # more unpacked data than this
    max_ratio: float = 200.0             # unpacked size / archive size above this ...
    ratio_floor: int = 256 * MIB         # ... once the unpacked size is above this
    max_entries: int = 500_000


@dataclass(frozen=True)
class SizeWarning:
    """The numbers behind the question "this is unusually large: extract anyway?"."""

    unpacked: int                        # bytes: announced by the archive, or written so far when ``running``
    packed: int                          # bytes the archive (or the selection) takes on disk
    entries: int
    free: int | None                     # free bytes at the destination
    reasons: tuple[str, ...]             # of "size", "ratio", "entries", "space"
    running: bool = False                # asked during extraction: the archive did not announce its size
    limits: Limits = field(default_factory=Limits)

    @property
    def ratio(self) -> float:
        return self.unpacked / self.packed if self.packed > 0 else 0.0

    def title(self) -> str:
        if "space" in self.reasons:
            return "Not enough free space"
        return "This archive unpacks to an unusual size"

    def text(self) -> str:
        """The sentences of the confirmation dialog, with every number."""
        lines = []
        if self.running:
            lines.append(f"{human_size(self.unpacked)} have been written so far, from an archive of "
                         f"{human_size(self.packed)} ({self.ratio:,.0f} times its size), and there is more.")
        else:
            lines.append(f"The archive is {human_size(self.packed)} and unpacks to {human_size(self.unpacked)} "
                         f"in {self.entries:,} item{'s' if self.entries != 1 else ''} "
                         f"({self.ratio:,.0f} times its size).")
        if "ratio" in self.reasons:
            lines.append(f"Archives rarely grow more than {self.limits.max_ratio:,.0f} times; one that does may be "
                         "built to fill the disk.")
        if "size" in self.reasons:
            lines.append(f"That is more than {human_size(self.limits.max_total)}.")
        if "entries" in self.reasons:
            lines.append(f"That is more than {self.limits.max_entries:,} items.")
        if self.free is not None:
            if "space" in self.reasons:
                lines.append(f"Only {human_size(self.free)} are free at the destination.")
            else:
                lines.append(f"{human_size(self.free)} are free at the destination.")
        return " ".join(lines)


@dataclass(frozen=True)
class Conflict:
    """A file that is already there."""

    path: str                            # relative to the destination, for the screen
    existing_size: int
    existing_mtime: float
    new_size: int
    new_mtime: float | None


@dataclass(frozen=True)
class Progress:
    path: str
    bytes_done: int
    bytes_total: int                     # -1 when the archive does not announce sizes
    entries_done: int
    entries_total: int


@dataclass
class Result:
    destination: str
    files: int = 0
    folders: int = 0
    links: int = 0
    bytes: int = 0
    skipped: list[Issue] = field(default_factory=list)
    failed: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    top_level: list[str] = field(default_factory=list)
    cancelled: bool = False
    password: str | None = None          # the password that opened the archive (to remember for the session)

    @property
    def clean(self) -> bool:
        return not (self.skipped or self.failed or self.cancelled)

    def summary(self) -> str:
        parts = [f"{self.files:,} file{'s' if self.files != 1 else ''}"]
        if self.folders:
            parts.append(f"{self.folders:,} folder{'s' if self.folders != 1 else ''}")
        if self.links:
            parts.append(f"{self.links:,} link{'s' if self.links != 1 else ''}")
        text = f"Extracted {', '.join(parts)} ({human_size(self.bytes)})."
        if self.cancelled:
            text = "Cancelled. " + text
        if self.skipped:
            text += f" {len(self.skipped):,} skipped."
        if self.failed:
            text += f" {len(self.failed):,} failed."
        return text


class Handler:
    """What extraction asks and tells. The defaults never ask: they skip, refuse and stay silent.

    Every method is called on the thread that runs ``extract``.
    """

    def progress(self, info: Progress) -> None:
        pass

    def overwrite(self, conflict: Conflict) -> str:
        """One of REPLACE, SKIP, RENAME, REPLACE_ALL, SKIP_ALL, RENAME_ALL, CANCEL."""
        return SKIP

    def confirm_size(self, warning: SizeWarning) -> bool:
        return False

    def password(self, wrong: bool) -> str | None:
        """The password for an encrypted archive; None cancels. ``wrong``: the last one did not fit."""
        return None


class DestinationError(ArchiveError):
    """The destination cannot take more (full, read-only): extraction stops."""


class _Skip(Exception):
    """This entry is left out on purpose."""


class _EntryError(Exception):
    """This entry could not be written."""


def current_umask() -> int:
    """The umask of this process, read without changing it where the kernel tells (Linux)."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8) & 0o777
    except (OSError, ValueError, IndexError):
        pass
    mask = os.umask(0o077)
    os.umask(mask)
    return mask


def default_destination(archive_path: str) -> str:
    """The folder named after the archive, beside it: ``/a/photos.tar.gz`` → ``/a/photos``."""
    from .formats import strip_extension
    directory, name = os.path.split(os.path.abspath(archive_path))
    return os.path.join(directory, strip_extension(name))


def _strerror(error: OSError) -> str:
    return error.strerror or errno.errorcode.get(error.errno or 0, "error")


class _Extractor:
    def __init__(self, archive: Archive, destination: str, policy: str, limits: Limits, password: str | None,
                 handler: Handler, cancel: threading.Event | None, case_insensitive: bool | None) -> None:
        self.archive = archive
        self.destination = destination
        self.policy = policy if policy in POLICIES else ASK
        self.limits = limits
        self.password = password
        self.handler = handler
        self.cancel = cancel
        self.result = Result(destination=destination)
        self.umask = current_umask()
        self.root = -1
        self._dirs: dict[tuple[str, ...], int] = {}
        self._made: set[tuple[str, ...]] = set()                 # folders this run created
        self._dir_meta: dict[tuple[str, ...], tuple[int | None, float | None]] = {}
        self._written: dict[tuple[str, ...], tuple[int, tuple[str, ...], str]] = {}
        self._folded: dict[tuple, str] = {}
        self._links: list[tuple[tuple[str, ...], Entry]] = []
        self._case_insensitive = case_insensitive
        self._budget = 0
        self._packed = 0
        self._bytes_total = -1
        self._entries_total = 0
        self._entries_done = 0
        self._duplicates = 0
        self._case_renames = 0
        self._cleaned = 0
        self._absolute = 0

    # --- small things ---
    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    def _issue(self, bucket: list[Issue], path: str, reason: str) -> None:
        if len(bucket) < _MAX_ISSUES:
            bucket.append(Issue(names.display(path), reason))

    def _report(self, path: str) -> None:
        self.handler.progress(Progress(names.display(path), self.result.bytes, self._bytes_total,
                                       self._entries_done, self._entries_total))

    def _top(self, parts: tuple[str, ...]) -> None:
        if parts and parts[0] not in self.result.top_level and len(self.result.top_level) < 1000:
            self.result.top_level.append(parts[0])

    @staticmethod
    def _temp_name() -> str:
        return f".bw-{secrets.token_hex(8)}.part"

    # --- folders ---
    def _close_dirs(self) -> None:
        for descriptor in self._dirs.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._dirs.clear()

    def _trim_dirs(self) -> None:
        """Called between entries only, so no descriptor handed out for the current entry is closed."""
        if len(self._dirs) >= _MAX_OPEN_DIRS:
            self._close_dirs()

    def _dir(self, parts: tuple[str, ...]) -> int:
        """An open descriptor of the folder ``parts`` below the destination, created if needed; never through a link.

        The walk starts at the longest prefix that is already open and takes
        one component at a time with ``O_NOFOLLOW``. Only the folder asked
        for stays open (in ``_dirs``), so a deep path costs one descriptor.
        """
        if not parts:
            return self.root
        found = self._dirs.get(parts)
        if found is not None:
            return found
        start = len(parts) - 1
        while start > 0 and parts[:start] not in self._dirs:
            start -= 1
        current = self._dirs[parts[:start]] if start > 0 else self.root
        owned = False
        for depth in range(start, len(parts)):
            try:
                following = self._step(current, parts[:depth + 1])
            finally:
                if owned:
                    os.close(current)
            current, owned = following, True
        self._dirs[parts] = current
        return current

    def _step(self, parent: int, parts: tuple[str, ...]) -> int:
        name = parts[-1]
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise _EntryError(self._blocked(parent, parts, error)) from None
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            self._made.add(parts)
            self._remember_name(parts)
            self._top(parts)
        except FileExistsError:
            pass
        except OSError as error:
            self._fatal_if_full(error)
            raise _EntryError(f"the folder “{names.display('/'.join(parts))}” cannot be created: "
                              f"{_strerror(error)}") from None
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent)
        except OSError as error:
            raise _EntryError(self._blocked(parent, parts, error)) from None

    def _blocked(self, parent: int, parts: tuple[str, ...], error: OSError) -> str:
        shown = names.display("/".join(parts))
        try:
            info = os.lstat(parts[-1], dir_fd=parent)
        except OSError:
            return f"the folder “{shown}” cannot be opened: {_strerror(error)}"
        if stat.S_ISLNK(info.st_mode):
            return f"“{shown}” in the destination is a link, and links are not followed"
        if not stat.S_ISDIR(info.st_mode):
            return f"“{shown}” in the destination is a file, not a folder"
        return f"the folder “{shown}” cannot be opened: {_strerror(error)}"

    # --- names that are taken ---
    def _fold_key(self, parts: tuple[str, ...]) -> tuple:
        return tuple(part.casefold() for part in parts)

    def _remember_name(self, parts: tuple[str, ...]) -> None:
        if self._case_insensitive:
            self._folded.setdefault(self._fold_key(parts), parts[-1])

    def _exists(self, directory: int, name: str):
        try:
            return os.lstat(name, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _EntryError(f"cannot look at the destination: {_strerror(error)}") from None

    def _free_name(self, directory: int, parent: tuple[str, ...], name: str) -> str:
        for number in range(2, 10_000):
            candidate = names.numbered(name, number)
            if self._exists(directory, candidate) is not None:
                continue
            if self._case_insensitive and self._fold_key(parent + (candidate,)) in self._folded:
                continue
            return candidate
        raise _EntryError("no free name was found for it")

    def _place(self, directory: int, parts: tuple[str, ...], entry: Entry) -> str:
        """The name ``entry`` gets inside ``directory``: its own, or another one when the name is taken.

        Raises _Skip (the user or the policy keeps what is there) or
        _EntryError (a folder is in the way).
        """
        name = parts[-1]
        parent = parts[:-1]
        info = self._exists(directory, name)
        earlier = self._written.get(parts)
        if earlier is not None:
            # The same path twice in one archive: the later entry wins, under the name the first one got
            # (which is not the stored name when a file that was there before was kept).
            if earlier[0] > entry.index:
                raise _Skip("a later entry of the archive has the same name and replaced it")
            self._duplicates += 1
            return earlier[1][-1]
        if self._case_insensitive:
            taken = self._folded.get(self._fold_key(parts))
            if taken is not None and taken != name:
                # Another entry differs only in upper and lower case and this file system cannot hold both.
                self._case_renames += 1
                return self._free_name(directory, parent, name)
        if info is None:
            return name
        if stat.S_ISDIR(info.st_mode):
            raise _EntryError("a folder with this name is in the way")
        decision = self.policy
        if decision == ASK:
            answer = self.handler.overwrite(Conflict(
                names.display("/".join(parts)), info.st_size, info.st_mtime, entry.size, entry.mtime))
            if answer == CANCEL:
                raise Cancelled()
            if answer in (REPLACE_ALL, SKIP_ALL, RENAME_ALL):
                self.policy = answer.rpartition("-")[0]
            decision = answer.rpartition("-")[0] if answer.endswith("-all") else answer
        if decision == REPLACE:
            return name
        if decision == RENAME:
            return self._free_name(directory, parent, name)
        raise _Skip("a file with this name already exists")

    # --- sizes ---
    def _free_space(self) -> int | None:
        try:
            info = os.fstatvfs(self.root)
        except OSError:
            return None
        return info.f_bavail * info.f_frsize

    def _count(self, size: int) -> None:
        self.result.bytes += size
        if self.result.bytes > self._budget:
            warning = SizeWarning(self.result.bytes, self._packed, self._entries_total, self._free_space(),
                                  ("ratio",) if self._packed and self.result.bytes / self._packed
                                  > self.limits.max_ratio else ("size",), running=True, limits=self.limits)
            if not self.handler.confirm_size(warning):
                raise Cancelled()
            self._budget = max(self._budget, self.result.bytes) * 4

    def _mode(self, entry: Entry, folder: bool = False) -> int:
        if folder:
            base = entry.mode if entry.mode is not None else 0o777
            return ((base & 0o777) & ~self.umask) | 0o700
        base = entry.mode if entry.mode is not None else 0o666
        return (base & 0o777) & ~self.umask

    @staticmethod
    def _time_ns(mtime: float | None) -> int | None:
        if mtime is None or not 0 <= mtime < _MAX_TIME:
            return None
        return int(mtime * 1_000_000_000)

    # --- files ---
    def _write_file(self, entry: Entry, parts: tuple[str, ...], reader) -> None:
        directory = self._dir(parts[:-1])
        name = self._place(directory, parts, entry)
        temporary = self._temp_name()
        try:
            descriptor = os.open(temporary, _FILE_FLAGS, 0o600, dir_fd=directory)
        except OSError as error:
            self._fatal_if_full(error)
            raise _EntryError(f"cannot be created: {_strerror(error)}") from None
        written = counted = 0
        try:
            try:
                while True:
                    self._check_cancel()
                    data = reader.read(_CHUNK)
                    if not data:
                        break
                    written += len(data)
                    if 0 <= entry.size < written:
                        raise _EntryError("holds more data than the archive lists for it")
                    counted += len(data)
                    self._count(len(data))
                    view = memoryview(data)
                    while view:
                        view = view[os.write(descriptor, view):]
                    self._report(entry.path)
                if entry.size >= 0 and written != entry.size:
                    raise _EntryError("is incomplete in the archive")
                os.fchmod(descriptor, self._mode(entry))
                stamp = self._time_ns(entry.mtime)
                if stamp is not None:
                    os.utime(descriptor, ns=(stamp, stamp))
            finally:
                os.close(descriptor)
            os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException as error:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
            self.result.bytes -= counted                         # what was removed again does not count
            if isinstance(error, OSError):
                self._fatal_if_full(error)
                raise _EntryError(f"cannot be written: {_strerror(error)}") from None
            raise
        final = parts[:-1] + (name,)
        self._written[parts] = (entry.index, final, FILE)
        self._remember_name(final)
        self._top(final)
        self.result.files += 1

    def _fatal_if_full(self, error: OSError) -> None:
        if error.errno in (errno.ENOSPC, errno.EDQUOT):
            raise DestinationError("There is no space left at the destination. Extraction stopped.") from None
        if error.errno == errno.EROFS:
            raise DestinationError("The destination is read-only. Extraction stopped.") from None

    # --- links ---
    def _make_hard_link(self, entry: Entry, parts: tuple[str, ...], strip: int) -> None:
        try:
            target, _notes = names.safe_parts(entry.link, strip=strip)
        except names.UnsafeName:
            raise _Skip("it is a hard link to something outside the destination") from None
        source = self._written.get(target)
        if source is None or source[2] != FILE:
            raise _Skip("it is a hard link to a file that was not extracted with it")
        source_parts = source[1]
        source_directory = self._dir(source_parts[:-1])
        directory = self._dir(parts[:-1])
        name = self._place(directory, parts, entry)
        temporary = self._temp_name()
        try:
            os.link(source_parts[-1], temporary, src_dir_fd=source_directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except OSError as error:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
            self._fatal_if_full(error)
            raise _EntryError(f"the hard link cannot be made: {_strerror(error)}") from None
        final = parts[:-1] + (name,)
        self._written[parts] = (entry.index, final, FILE)
        self._remember_name(final)
        self._top(final)
        self.result.files += 1

    def _make_symlink(self, entry: Entry, parts: tuple[str, ...]) -> None:
        try:
            target = self.archive.link_target(entry, self.password)
        except ArchiveError as error:
            raise _EntryError(f"the target of the link cannot be read: {error}") from None
        if not target:
            raise _Skip("the link has no target")
        if len(os.fsencode(target)) > 4095:
            raise _Skip("the target of the link is too long")
        if not names.link_stays_inside(parts[:-1], target):
            raise _Skip(f"it is a link that points outside the destination (to “{names.display(target)[:200]}”)")
        directory = self._dir(parts[:-1])
        name = self._place(directory, parts, entry)
        temporary = self._temp_name()
        try:
            os.symlink(target, temporary, dir_fd=directory)
            stamp = self._time_ns(entry.mtime)
            if stamp is not None:
                try:
                    os.utime(temporary, ns=(stamp, stamp), dir_fd=directory, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
            os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except OSError as error:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
            self._fatal_if_full(error)
            raise _EntryError(f"the link cannot be made: {_strerror(error)}") from None
        final = parts[:-1] + (name,)
        self._written[parts] = (entry.index, final, SYMLINK)
        self._remember_name(final)
        self._links.append((final, entry))
        self._top(final)
        self.result.links += 1

    def _verify_links(self) -> None:
        """Resolve every link made by this run; remove those that lead outside, until none does."""
        try:
            root = os.path.realpath(self.destination, strict=True)
        except OSError:
            root = os.path.realpath(self.destination)
        prefix = root.rstrip(os.sep) + os.sep
        changed = True
        while changed and self._links:
            changed = False
            for item in list(self._links):
                parts, entry = item
                resolved = os.path.realpath(os.path.join(root, *parts))
                if resolved == root or resolved.startswith(prefix):
                    continue
                try:
                    os.unlink(parts[-1], dir_fd=self._dir(parts[:-1]))
                except (OSError, _EntryError):
                    pass
                self._links.remove(item)
                self.result.links -= 1
                self._issue(self.result.skipped, entry.path,
                            "it is a link that leads outside the destination through other links")
                changed = True

    # --- the run ---
    def _guard_sizes(self, selected: list[Entry]) -> bool:
        files = [entry for entry in selected if entry.kind == FILE]
        known = sum(entry.size for entry in files if entry.size > 0)
        unknown = any(entry.size < 0 for entry in files)
        whole = len(selected) >= len(self.archive.entries)
        packed_known = all(entry.packed >= 0 for entry in files)
        self._packed = (sum(entry.packed for entry in files) if packed_known and not whole and files
                        else self.archive.file_size)
        self._entries_total = len(selected)
        self._bytes_total = -1 if unknown else known
        free = self._free_space()
        limits = self.limits
        reasons = []
        if known > limits.max_total:
            reasons.append("size")
        if known > limits.ratio_floor and self._packed > 0 and known / self._packed > limits.max_ratio:
            reasons.append("ratio")
        if len(selected) > limits.max_entries:
            reasons.append("entries")
        if free is not None and known > free:
            reasons.append("space")
        if reasons:
            warning = SizeWarning(known, self._packed, len(selected), free, tuple(reasons), limits=limits)
            if not self.handler.confirm_size(warning):
                return False
        self._budget = known
        if unknown:
            self._budget += min(limits.max_total, max(limits.ratio_floor, int(self._packed * limits.max_ratio)))
        return True

    def _unlock(self, selected: list[Entry]) -> bool:
        if not any(entry.encrypted for entry in selected) and not getattr(self.archive, "header_password", None):
            return True
        opened, self.password = unlock(self.archive, self.password, self.handler)
        self.result.password = self.password if opened else None
        return opened

    def _detect_case(self) -> None:
        if self._case_insensitive is not None:
            return
        probe = f".bw-Case-{secrets.token_hex(6)}"
        try:
            os.close(os.open(probe, _FILE_FLAGS, 0o600, dir_fd=self.root))
        except OSError:
            self._case_insensitive = False
            return
        try:
            self._case_insensitive = self._exists(self.root, probe.lower()) is not None
        except _EntryError:
            self._case_insensitive = False
        finally:
            try:
                os.unlink(probe, dir_fd=self.root)
            except OSError:
                pass

    def run(self, entries: list[Entry] | None, base: str) -> Result:
        selected = list(self.archive.entries if entries is None else entries)
        selected = [entry for entry in selected if not entry.synthetic]
        strip = len(names.split(base))
        try:
            os.makedirs(self.destination, exist_ok=True)
            self.root = os.open(self.destination, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as error:
            raise ArchiveError(f"The destination folder cannot be used: {_strerror(error)}.") from None
        try:
            if not self._unlock(selected) or not self._guard_sizes(selected):
                self.result.cancelled = True
                return self.result
            self._detect_case()
            self._extract(selected, strip)
        except Cancelled:
            self.result.cancelled = True
        finally:
            try:
                self._finish_folders()
            finally:
                self._close_dirs()
                os.close(self.root)
                self.root = -1
        self.result.folders = len(self._made)
        self._notes()
        return self.result

    def _target(self, entry: Entry, strip: int) -> tuple[str, ...] | None:
        """Where ``entry`` goes, or None when it is not extracted (the reason is recorded)."""
        try:
            parts, notes = names.safe_parts(entry.name, strip=strip)
        except names.UnsafeName as error:
            if error.reason == "the name is empty" and entry.kind == DIR:
                return None                                # "./": the destination itself
            self._issue(self.result.skipped, entry.name, error.reason)
            return None
        for note in notes:
            if note.startswith("absolute"):
                self._absolute += 1
            else:
                self._cleaned += 1
        return parts

    def _extract(self, selected: list[Entry], strip: int) -> None:
        wanted = None if len(selected) >= len(self.archive.entries) else {entry.index for entry in selected}
        later: list[tuple[Entry, tuple[str, ...]]] = []
        iterator = self.archive.stream(wanted, self.password)
        try:
            for entry, reader in iterator:
                try:
                    self._check_cancel()
                    self._trim_dirs()
                    self._one(entry, reader, strip, later)
                finally:
                    if reader is not None:
                        reader.close()
                self._entries_done += 1
                self._report(entry.path)
        except DestinationError as error:
            self._issue(self.result.failed, "", str(error))
            return
        except ArchiveError as error:
            self._issue(self.result.failed, "", str(error))
        finally:
            iterator.close()
        # Links come last: no file of this archive can be written through one of its own links.
        try:
            for kind in (HARDLINK, SYMLINK):
                for entry, parts in later:
                    if entry.kind != kind:
                        continue
                    self._check_cancel()
                    self._trim_dirs()
                    try:
                        if kind == HARDLINK:
                            self._make_hard_link(entry, parts, strip)
                        else:
                            self._make_symlink(entry, parts)
                    except _Skip as skip:
                        self._issue(self.result.skipped, entry.path, str(skip))
                    except _EntryError as error:
                        self._issue(self.result.failed, entry.path, str(error))
        except DestinationError as error:
            self._issue(self.result.failed, "", str(error))
        finally:
            self._verify_links()

    def _one(self, entry: Entry, reader, strip: int, later: list) -> None:
        parts = self._target(entry, strip)
        if parts is None:
            return
        try:
            if entry.kind == DIR:
                self._dir(parts)
                if parts in self._made:           # a folder that was there before keeps its bits and time
                    self._dir_meta[parts] = (entry.mode, entry.mtime)
            elif entry.kind == FILE:
                if reader is None:
                    raise _EntryError("has no content in the archive")
                self._write_file(entry, parts, reader)
            elif entry.kind in (SYMLINK, HARDLINK):
                later.append((entry, parts))
            elif entry.kind in (DEVICE, FIFO):
                raise _Skip("it is a device or a pipe, which are never extracted")
            else:
                raise _Skip("it is a special file, which is never extracted")
        except _Skip as skip:
            self._issue(self.result.skipped, entry.path, str(skip))
        except _EntryError as error:
            self._issue(self.result.failed, entry.path, str(error))
        except PasswordRequired as error:
            self._issue(self.result.failed, entry.path, str(error))
        except DestinationError:
            raise
        except ArchiveError as error:
            self._issue(self.result.failed, entry.path, str(error))

    def _finish_folders(self) -> None:
        """Give the folders this run created their permission bits and times, deepest first."""
        default = 0o777 & ~self.umask | 0o700
        for parts in sorted(self._made, key=len, reverse=True):
            mode, mtime = self._dir_meta.get(parts, (None, None))
            try:
                descriptor = self._dir(parts)
                final = default if mode is None else ((mode & 0o777) & ~self.umask) | 0o700
                os.fchmod(descriptor, final)
                stamp = self._time_ns(mtime)
                if stamp is not None:
                    os.utime(descriptor, ns=(stamp, stamp))
            except (OSError, _EntryError):
                continue

    def _notes(self) -> None:
        notes = self.result.notes
        if self._absolute:
            notes.append(f"{self._absolute:,} absolute path{'s were' if self._absolute != 1 else ' was'} made "
                         "relative to the destination.")
        if self._cleaned:
            notes.append(f"Control characters were replaced in {self._cleaned:,} "
                         f"name{'s' if self._cleaned != 1 else ''}.")
        if self._duplicates:
            notes.append(f"{self._duplicates:,} entr{'ies appear' if self._duplicates != 1 else 'y appears'} more "
                         "than once in the archive; the last one was kept.")
        if self._case_renames:
            notes.append(f"{self._case_renames:,} name{'s differ' if self._case_renames != 1 else ' differs'} from "
                         "another only in upper and lower case, which this file system cannot tell apart; "
                         "a number was added.")


def unlock(archive: Archive, password: str | None, handler: Handler) -> tuple[bool, str | None]:
    """Find the password of an encrypted archive: try ``password``, then ask ``handler`` until one fits.

    Returns ``(True, password)``, or ``(False, None)`` when the user gave up.
    """
    while True:
        try:
            archive.check_password(password)
        except WrongPassword:
            wrong = True
        except PasswordRequired:
            wrong = False
        except ArchiveError:
            return True, password           # damaged, not locked: the entries report it one by one
        else:
            return True, password
        password = handler.password(wrong)
        if password is None:
            return False, None


def extract(archive: Archive, destination: str | os.PathLike, *, entries: list[Entry] | None = None, base: str = "",
            policy: str = ASK, limits: Limits | None = None, password: str | None = None,
            handler: Handler | None = None, cancel: threading.Event | None = None,
            case_insensitive: bool | None = None) -> Result:
    """Extract ``entries`` of ``archive`` (default: all) below ``destination`` and return what happened.

    ``base`` is the folder of the archive the user is looking at: that many
    leading components are dropped, so a file selected inside ``docs/img``
    lands directly in the destination. ``policy`` says what happens to a
    file that is already there (``ASK`` calls ``handler.overwrite``).
    ``case_insensitive`` overrides what is found out about the destination's
    file system (tests).

    Raises ``ArchiveError`` only when the destination cannot be used; every
    other problem is in the ``Result``. An empty destination is refused: it
    would silently mean the working directory of the program.
    """
    if not os.fspath(destination):
        raise ArchiveError("No destination folder was given.")
    extractor = _Extractor(archive, os.path.abspath(os.fspath(destination)), policy, limits or Limits(), password,
                           handler or Handler(), cancel, case_insensitive)
    return extractor.run(entries, base)
