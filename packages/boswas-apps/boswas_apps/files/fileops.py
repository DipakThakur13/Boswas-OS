"""File operations that report progress, can be cancelled and can be undone (no Qt).

An ``Operation`` is created with what it should do and executed with
``run(reporter)`` on a worker thread. The reporter is how it talks to the
user interface: ``progress`` (throttled), ``conflict`` (blocks until the
user chose Replace, Skip or Keep both) and ``cancelled`` (polled between
files and between blocks of a file). The ``Result`` lists what could not
be done, item by item, and an ``Undo`` record when the operation can be
reversed without losing anything.

Rules of the copy and move code:

* nothing follows a symbolic link: a link is copied as a link, and a file
  is opened with O_NOFOLLOW;
* a file is written under a temporary name in the destination folder and
  renamed over its final name when it is complete, so a cancelled or
  failed copy leaves no half file and a replaced file is replaced whole;
* permissions and times are copied (ownership is not: that needs root);
* a move is a rename when the file system allows it and copy-then-delete
  when it answers EXDEV;
* a folder is never copied or moved into itself.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import stat
import time
from dataclasses import dataclass, field

from .text import display_name, plural, unique_name, validate_name
from .trash import Trash, TrashError, TrashItem, TrashUnavailable

REPLACE, SKIP, KEEP_BOTH, CANCEL = "replace", "skip", "keep", "cancel"
_BLOCK = 1024 * 1024
_EMIT_INTERVAL = 0.05
_MAX_DEPTH = 200
_PART_SUFFIX = ".boswas-part"


class Cancelled(Exception):
    """The user cancelled the operation."""


@dataclass
class Progress:
    current: str = ""
    items_done: int = 0
    items_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    preparing: bool = False


@dataclass
class Conflict:
    source: str
    destination: str
    source_is_dir: bool
    destination_is_dir: bool
    source_size: int
    destination_size: int
    source_mtime: float
    destination_mtime: float


class Reporter:
    """What an operation needs from its caller. The defaults suit tests and unattended runs."""

    def progress(self, progress: Progress) -> None:
        pass

    def conflict(self, conflict: Conflict) -> tuple[str, bool]:
        """(REPLACE, SKIP, KEEP_BOTH or CANCEL; whether to do the same for every later conflict)."""
        return SKIP, True

    def cancelled(self) -> bool:
        return False


@dataclass
class Undo:
    """How to reverse an operation: ``inverse`` builds the operation that does it."""

    kind: str                                       # rename, move, copy, create, trash, restore
    label: str
    pairs: list[tuple[str, str]] = field(default_factory=list)      # (where it was, where it is now)
    paths: list[str] = field(default_factory=list)                  # what was created
    items: list[TrashItem] = field(default_factory=list)            # what went to the Trash


@dataclass
class Result:
    title: str
    errors: list[tuple[str, str]] = field(default_factory=list)     # (path, what went wrong)
    affected: list[str] = field(default_factory=list)               # top-level results, to select them
    cancelled: bool = False
    skipped: int = 0
    undo: Undo | None = None
    untrashable: list[str] = field(default_factory=list)            # no Trash on their volume

    @property
    def ok(self) -> bool:
        return not self.errors and not self.cancelled


_MESSAGES = {
    errno.EACCES: "You do not have permission.",
    errno.EPERM: "This is not permitted.",
    errno.ENOENT: "It no longer exists.",
    errno.ENOSPC: "There is not enough free space.",
    errno.EDQUOT: "Your disk quota is used up.",
    errno.EROFS: "The volume is read-only.",
    errno.ENAMETOOLONG: "The name or path is too long.",
    errno.EEXIST: "Something with this name is already there.",
    errno.ENOTEMPTY: "The folder is not empty.",
    errno.EBUSY: "It is in use.",
    errno.EIO: "The device reported an error.",
    errno.ELOOP: "It is a link that cannot be followed.",
    errno.EISDIR: "A folder is in the way.",
    errno.ENOTDIR: "A file is in the way of a folder.",
    errno.EFBIG: "The file is too large for the destination volume.",
    errno.EINVAL: "The name is not allowed on this volume.",
    errno.EXDEV: "It is on another volume.",
    errno.ETXTBSY: "The program is running.",
}


def describe_error(error: BaseException) -> str:
    """An exception in plain words."""
    if isinstance(error, TrashError):
        return str(error)
    if isinstance(error, OSError) and error.errno in _MESSAGES:
        return _MESSAGES[error.errno]
    if isinstance(error, OSError) and error.strerror:
        return error.strerror + "."
    if isinstance(error, RecursionError):
        return "The folders are nested too deeply."
    return str(error) or error.__class__.__name__


def _count_label(paths: list, noun: str = "item") -> str:
    if len(paths) == 1:
        target = paths[0][0] if isinstance(paths[0], tuple) else paths[0]
        return f"“{display_name(os.path.basename(str(target).rstrip('/')))}”"
    return plural(len(paths), noun)


def measure(path: str, check=None) -> tuple[int, int]:
    """(number of entries, bytes of regular files) of ``path`` and everything below it. Follows no link."""
    count = size = 0
    stack = [path]
    while stack:
        current = stack.pop()
        if check is not None:
            check()
        count += 1
        try:
            info = os.lstat(current)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                with os.scandir(current) as iterator:
                    stack.extend(entry.path for entry in iterator)
            except OSError:
                pass
        elif stat.S_ISREG(info.st_mode):
            size += info.st_size
    return count, size


def _make_writable(directory: str) -> bool:
    """Give the owner full access to a directory they own (to be able to empty it)."""
    try:
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            return False
        os.chmod(directory, stat.S_IMODE(info.st_mode) | 0o700)
    except OSError:
        return False
    return True


def remove_tree(path: str, *, on_item=None, on_error=None, check=None) -> bool:
    """Delete ``path`` and everything below it without following links. True when nothing is left.

    ``on_item(path)`` after each removed entry, ``on_error(path, exception)``
    for each entry that could not be removed, ``check()`` between entries
    (it may raise to stop). A folder the user owns but may not write to is
    made writable first, as other file managers do.
    """
    ok = True
    folders: list[str] = []
    stack = [path]
    while stack:
        current = stack.pop()
        if check is not None:
            check()
        try:
            info = os.lstat(current)
            if stat.S_ISDIR(info.st_mode):
                try:
                    with os.scandir(current) as iterator:
                        children = [entry.path for entry in iterator]
                except PermissionError:
                    if not _make_writable(current):
                        raise
                    with os.scandir(current) as iterator:
                        children = [entry.path for entry in iterator]
                folders.append(current)
                stack.extend(children)
                continue
            try:
                os.unlink(current)
            except PermissionError:
                if not _make_writable(os.path.dirname(current)):
                    raise
                os.unlink(current)
        except FileNotFoundError:
            continue
        except OSError as error:
            ok = False
            if on_error is not None:
                on_error(current, error)
            continue
        if on_item is not None:
            on_item(current)
    for folder in reversed(folders):
        try:
            try:
                os.rmdir(folder)
            except PermissionError:
                if not _make_writable(os.path.dirname(folder)):
                    raise
                os.rmdir(folder)
        except FileNotFoundError:
            continue
        except OSError as error:
            ok = False
            # A folder that still holds an entry we already reported is not reported again.
            if on_error is not None and error.errno != errno.ENOTEMPTY:
                on_error(folder, error)
            continue
        if on_item is not None:
            on_item(folder)
    return ok


class Operation:
    """Base class: progress, cancellation, the list of errors."""

    title = "Working"

    def __init__(self) -> None:
        self.progress = Progress()
        self.errors: list[tuple[str, str]] = []
        self.affected: list[str] = []
        self.skipped = 0
        self.undo: Undo | None = None
        self.untrashable: list[str] = []
        self._reporter = Reporter()
        self._emitted = 0.0
        self._remembered: str | None = None

    def run(self, reporter: Reporter | None = None) -> Result:
        self._reporter = reporter or Reporter()
        cancelled = False
        try:
            self._run()
        except Cancelled:
            cancelled = True
        self._emit(force=True)
        return Result(self.title, self.errors, self.affected, cancelled, self.skipped, self.undo, self.untrashable)

    def _run(self) -> None:
        raise NotImplementedError

    def _check(self) -> None:
        if self._reporter.cancelled():
            raise Cancelled()

    def _emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._emitted >= _EMIT_INTERVAL:
            self._emitted = now
            self._reporter.progress(self.progress)

    def _error(self, path: str, error: BaseException | str) -> None:
        self.errors.append((path, error if isinstance(error, str) else describe_error(error)))

    def _ask(self, source: str, destination: str, source_info: os.stat_result,
             destination_info: os.stat_result) -> str:
        if self._remembered is not None:
            return self._remembered
        choice, for_all = self._reporter.conflict(Conflict(
            source, destination, stat.S_ISDIR(source_info.st_mode), stat.S_ISDIR(destination_info.st_mode),
            source_info.st_size, destination_info.st_size, source_info.st_mtime, destination_info.st_mtime))
        if choice == CANCEL or choice not in (REPLACE, SKIP, KEEP_BOTH):
            raise Cancelled()
        if for_all:
            self._remembered = choice
        return choice


class Transfer(Operation):
    """Copy or move files and folders into a folder (or, with ``pairs``, to exact destinations)."""

    rename = staticmethod(os.rename)            # tests replace it to simulate another file system

    def __init__(self, sources: list[str], destination: str = "", *, move: bool = False,
                 pairs: list[tuple[str, str]] | None = None, overwrite: bool = True,
                 title: str | None = None) -> None:
        super().__init__()
        self.sources = [os.path.abspath(source) for source in sources]
        self.destination = os.path.abspath(destination) if destination else ""
        self.move = move
        self.pairs = pairs
        self.overwrite = overwrite              # False: a conflict is an error (undo never overwrites)
        what = pairs if pairs is not None else self.sources
        verb = "Moving" if move else "Copying"
        where = f" to “{display_name(os.path.basename(self.destination) or '/')}”" if destination else ""
        self.title = title or f"{verb} {_count_label(list(what))}{where}"
        self._replaced = False
        self._deep = not move                   # totals count every entry below the sources

    # --- planning ---
    def _plan(self) -> list[tuple[str, str]]:
        if self.pairs is not None:
            return [(os.path.abspath(source), os.path.abspath(target)) for source, target in self.pairs]
        destination = self.destination
        if not os.path.isdir(destination):
            self._error(destination, "The destination folder does not exist.")
            return []
        real_destination = os.path.realpath(destination)
        plan = []
        for source in self.sources:
            name = os.path.basename(source)
            if not name or not os.path.lexists(source):
                self._error(source, "It no longer exists.")
                continue
            if os.path.isdir(source) and not os.path.islink(source):
                real_source = os.path.realpath(source)
                if real_destination == real_source or real_destination.startswith(real_source.rstrip("/") + "/"):
                    self._error(source, "The destination folder is inside this folder.")
                    continue
            if os.path.realpath(os.path.dirname(source)) == real_destination:
                if self.move:
                    continue                    # it is there already
                name = unique_name(name, lambda candidate: os.path.lexists(os.path.join(destination, candidate)),
                                   copy=True)
            plan.append((source, os.path.join(destination, name)))
        return plan

    def _run(self) -> None:
        plan = self._plan()
        progress = self.progress
        if self._deep:
            progress.preparing = True
            self._emit(force=True)
            for source, _target in plan:
                count, size = measure(source, self._check)
                progress.items_total += count
                progress.bytes_total += size
            progress.preparing = False
        else:
            progress.items_total = len(plan)
        self._emit(force=True)
        done: list[tuple[str, str]] = []
        for source, target in plan:
            self._check()
            errors_before = len(self.errors)
            self._replaced = False
            try:
                placed = self._place(source, target, 0)
            except OSError as error:
                self._error(source, error)
                placed = None
            except RecursionError as error:
                self._error(source, error)
                placed = None
            if placed is None:
                continue
            self.affected.append(placed)
            if len(self.errors) == errors_before and not self._replaced:
                done.append((source, placed))
        if done:
            if self.move:
                self.undo = Undo("move", f"Move of {_count_label(done)}", pairs=done)
            else:
                self.undo = Undo("copy", f"Copy of {_count_label(done)}", paths=[target for _source, target in done])

    # --- one entry ---
    def _skip(self, source: str) -> None:
        self.skipped += 1
        if self._deep:
            count, size = measure(source)
            self.progress.items_done += count
            self.progress.bytes_done += size
        else:
            self.progress.items_done += 1
        self._emit()

    def _place(self, source: str, target: str, depth: int) -> str | None:
        """Put ``source`` at ``target``, asking when something is there. Returns the final path, or None."""
        self._check()
        info = os.lstat(source)
        self.progress.current = source
        if os.path.lexists(target):
            target_info = os.lstat(target)
            if os.path.samestat(info, target_info):
                self._error(source, "The source and the destination are the same.")
                self.progress.items_done += 1
                return None
            if not self.overwrite:
                self._error(source, f"Something named “{display_name(os.path.basename(target))}” "
                                    "is in the way.")
                self._skip(source)
                self.skipped -= 1
                return None
            choice = self._ask(source, target, info, target_info)
            if choice == SKIP:
                self._skip(source)
                return None
            if choice == KEEP_BOTH:
                parent = os.path.dirname(target)
                target = os.path.join(parent, unique_name(
                    os.path.basename(target), lambda candidate: os.path.lexists(os.path.join(parent, candidate))))
            else:
                self._replaced = True
                source_is_dir = stat.S_ISDIR(info.st_mode)
                target_is_dir = stat.S_ISDIR(target_info.st_mode)
                if source_is_dir and target_is_dir:
                    return self._merge(source, target, depth)
                if target_is_dir:
                    failures: list = []
                    remove_tree(target, on_error=lambda path, error: failures.append((path, error)),
                                check=self._check)
                    if failures:
                        self._error(source, f"The folder in the way could not be removed: "
                                            f"{describe_error(failures[0][1])}")
                        self._skip(source)
                        self.skipped -= 1
                        return None
                elif source_is_dir:
                    os.unlink(target)
        if self.move:
            try:
                self.rename(source, target)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
            else:
                if self._deep:
                    count, size = measure(target)
                    self.progress.items_done += count
                    self.progress.bytes_done += size
                else:
                    self.progress.items_done += 1
                self._emit()
                return target
            if not self._deep:                  # another file system: now every entry is work
                count, size = measure(source, self._check)
                self.progress.items_total += count - 1
                self.progress.bytes_total += size
        return self._copy(source, target, info, depth, self.move)

    def _merge(self, source: str, target: str, depth: int) -> str:
        if depth > _MAX_DEPTH:
            raise RecursionError()
        with os.scandir(source) as iterator:
            names = [entry.name for entry in iterator]
        if not self._deep:
            self.progress.items_total += len(names)
        for name in names:
            child = os.path.join(source, name)
            try:
                self._place(child, os.path.join(target, name), depth + 1)
            except OSError as error:
                self._error(child, error)
                self.progress.items_done += 1
        self.progress.items_done += 1
        self._emit()
        if self.move:
            try:
                os.rmdir(source)                # only when everything in it has moved
            except OSError:
                pass
        return target

    def _copy(self, source: str, target: str, info: os.stat_result, depth: int, delete_source: bool) -> str | None:
        """Copy one entry and what is below it to ``target`` (free, or a file to replace)."""
        self._check()
        self.progress.current = source
        mode = info.st_mode
        try:
            if stat.S_ISDIR(mode):
                if depth > _MAX_DEPTH:
                    raise RecursionError()
                os.mkdir(target, 0o700)
                complete = True
                with os.scandir(source) as iterator:
                    names = [entry.name for entry in iterator]
                for name in names:
                    child = os.path.join(source, name)
                    try:
                        child_info = os.lstat(child)
                    except OSError as error:
                        self._error(child, error)
                        self.progress.items_done += 1
                        complete = False
                        continue
                    if self._copy(child, os.path.join(target, name), child_info, depth + 1, delete_source) is None:
                        complete = False
                self._copy_attributes(source, target)
                if delete_source and complete:
                    os.rmdir(source)
            elif stat.S_ISLNK(mode):
                link = os.readlink(source)
                if os.path.lexists(target):
                    os.unlink(target)
                os.symlink(link, target)
                self._copy_attributes(source, target)
                if delete_source:
                    os.unlink(source)
            elif stat.S_ISREG(mode):
                self._copy_file(source, target)
                if delete_source:
                    os.unlink(source)
            elif stat.S_ISFIFO(mode):
                if os.path.lexists(target):
                    os.unlink(target)
                os.mkfifo(target, stat.S_IMODE(mode))
                if delete_source:
                    os.unlink(source)
            else:
                self._error(source, "Devices and sockets cannot be copied.")
                self.progress.items_done += 1
                return None
        except (OSError, RecursionError) as error:
            self._error(source, error)
            self.progress.items_done += 1
            self._emit()
            return None
        self.progress.items_done += 1
        self._emit()
        return target

    @staticmethod
    def _copy_attributes(source: str, target: str) -> None:
        try:
            shutil.copystat(source, target, follow_symlinks=False)
        except OSError:
            # A file system without permissions or extended attributes: keep at least the times.
            try:
                info = os.lstat(source)
                os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass

    def _copy_file(self, source: str, target: str) -> None:
        directory = os.path.dirname(target)
        partial = os.path.join(directory, f".{secrets.token_hex(6)}{_PART_SUFFIX}")
        reader = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            writer = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            try:
                try:
                    while True:
                        self._check()
                        block = os.read(reader, _BLOCK)
                        if not block:
                            break
                        view = memoryview(block)
                        while view:
                            written = os.write(writer, view)
                            view = view[written:]
                        self.progress.bytes_done += len(block)
                        self._emit()
                finally:
                    os.close(writer)
                self._copy_attributes(source, partial)
                os.replace(partial, target)
            except BaseException:
                try:
                    os.unlink(partial)
                except OSError:
                    pass
                raise
        finally:
            os.close(reader)


class Delete(Operation):
    """Delete files and folders for good."""

    def __init__(self, paths: list[str], title: str | None = None) -> None:
        super().__init__()
        self.paths = [os.path.abspath(path) for path in paths]
        self.title = title or f"Deleting {_count_label(self.paths)}"

    def _removed(self, path: str) -> None:
        self.progress.items_done += 1
        self.progress.current = path
        self._emit()

    def _run(self) -> None:
        self.progress.preparing = True
        self._emit(force=True)
        for path in self.paths:
            self.progress.items_total += measure(path, self._check)[0]
        self.progress.preparing = False
        for path in self.paths:
            self._check()
            if not os.path.lexists(path):
                self._error(path, "It no longer exists.")
                continue
            remove_tree(path, on_item=self._removed, on_error=self._error, check=self._check)


class TrashOp(Operation):
    """Move files and folders to the Trash."""

    def __init__(self, paths: list[str], trash: Trash, title: str | None = None) -> None:
        super().__init__()
        self.paths = [os.path.abspath(path) for path in paths]
        self.trash = trash
        self.title = title or f"Moving {_count_label(self.paths)} to the Trash"

    def _run(self) -> None:
        self.progress.items_total = len(self.paths)
        items: list[TrashItem] = []
        for path in self.paths:
            self._check()
            self.progress.current = path
            try:
                items.append(self.trash.trash(path))
            except TrashUnavailable:
                self.untrashable.append(path)
            except OSError as error:
                if error.errno == errno.EXDEV:          # e.g. a bind mount inside the volume
                    self.untrashable.append(path)
                else:
                    self._error(path, error)
            except TrashError as error:
                self._error(path, error)
            self.progress.items_done += 1
            self._emit()
        if items:
            self.undo = Undo("trash", f"Delete of {_count_label([item.original_path for item in items])}",
                             items=items)


class Restore(Operation):
    """Put items of the Trash back where they were."""

    def __init__(self, items: list[TrashItem], trash: Trash, title: str | None = None) -> None:
        super().__init__()
        self.items = list(items)
        self.trash = trash
        self.title = title or f"Restoring {_count_label([item.original_path for item in self.items])}"

    def _run(self) -> None:
        self.progress.items_total = len(self.items)
        restored: list[str] = []
        for item in self.items:
            self._check()
            self.progress.current = item.original_path
            try:
                path = self._restore(item)
            except (OSError, TrashError) as error:
                self._error(item.original_path, error)
                path = None
            if path is not None:
                restored.append(path)
                self.affected.append(path)
            self.progress.items_done += 1
            self._emit()
        if restored:
            self.undo = Undo("restore", f"Restore of {_count_label(restored)}", paths=restored)

    def _restore(self, item: TrashItem) -> str | None:
        target = item.original_path
        replace = False
        if os.path.lexists(target):
            choice = self._ask(item.files_path, target, os.lstat(item.files_path), os.lstat(target))
            if choice == SKIP:
                self.skipped += 1
                return None
            if choice == KEEP_BOTH:
                parent = os.path.dirname(target)
                target = os.path.join(parent, unique_name(
                    os.path.basename(target), lambda candidate: os.path.lexists(os.path.join(parent, candidate))))
            else:
                replace = True
        try:
            return self.trash.restore(item, target, replace=replace)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
        # The trash is on another file system than the place the item came from: move it the long way.
        mover = Transfer([], pairs=[(item.files_path, target)], move=True, overwrite=replace)
        mover._remembered = REPLACE
        result = mover.run(self._reporter)
        if result.cancelled:
            raise Cancelled()
        if result.errors:
            self.errors.extend(result.errors)
            return None
        self.trash.forget(item)
        return target


class DeleteTrashed(Operation):
    """Delete items of the Trash for good; without ``items``, empty every trash directory."""

    def __init__(self, trash: Trash, items: list[TrashItem] | None = None) -> None:
        super().__init__()
        self.trash = trash
        self.items = items
        self.title = "Emptying the Trash" if items is None else \
            f"Deleting {_count_label([item.original_path for item in items])}"

    def _run(self) -> None:
        everything = self.items is None
        items = self.trash.items() if everything else list(self.items or [])
        self.progress.items_total = len(items)
        self._emit(force=True)
        for item in items:
            self._check()
            self.progress.current = item.original_path
            if remove_tree(item.files_path, on_error=self._error, check=self._check):
                self.trash.forget(item)
            self.progress.items_done += 1
            self._emit()
        if everything:
            for trash_dir, _topdir in self.trash.trash_dirs():
                self.trash.clean(trash_dir)


# --- small operations (run on a worker thread, no progress) --------------------------------------------------

def rename(path: str, new_name: str) -> tuple[str, Undo]:
    """Rename ``path`` within its folder. Raises ValueError (a bad name), FileExistsError or OSError."""
    problem = validate_name(new_name)
    if problem:
        raise ValueError(problem)
    path = os.path.abspath(path)
    target = os.path.join(os.path.dirname(path), new_name)
    if target == path:
        raise ValueError("That is its name already.")
    if os.path.lexists(target):
        try:
            same = os.path.samestat(os.lstat(path), os.lstat(target))      # a change of case on a FAT volume
        except OSError:
            same = False
        if not same:
            raise FileExistsError(errno.EEXIST, "Something with this name is already there", target)
    os.rename(path, target)
    return target, Undo("rename", "Rename", pairs=[(path, target)])


def create_folder(directory: str, name: str = "New folder") -> tuple[str, Undo]:
    """Create a folder with a free name based on ``name``; returns its path."""
    for _attempt in range(100):
        candidate = unique_name(name, lambda text: os.path.lexists(os.path.join(directory, text)))
        target = os.path.join(directory, candidate)
        try:
            os.mkdir(target)
        except FileExistsError:
            continue
        return target, Undo("create", "New folder", paths=[target])
    raise FileExistsError(errno.EEXIST, "No free name", directory)


def create_file(directory: str, name: str = "New text document.txt") -> tuple[str, Undo]:
    """Create an empty file with a free name based on ``name``; returns its path."""
    for _attempt in range(100):
        candidate = unique_name(name, lambda text: os.path.lexists(os.path.join(directory, text)))
        target = os.path.join(directory, candidate)
        try:
            os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o666))
        except FileExistsError:
            continue
        return target, Undo("create", "New file", paths=[target])
    raise FileExistsError(errno.EEXIST, "No free name", directory)


def inverse(undo: Undo, trash: Trash) -> Operation | None:
    """The operation that reverses ``undo``. It never overwrites and never deletes for good."""
    if undo.kind in ("rename", "move"):
        pairs = [(now, before) for before, now in undo.pairs]
        return Transfer([], pairs=pairs, move=True, overwrite=False, title=f"Undoing: {undo.label}")
    if undo.kind in ("copy", "create", "restore"):
        return TrashOp(undo.paths, trash, title=f"Undoing: {undo.label}")
    if undo.kind == "trash":
        return Restore(undo.items, trash, title=f"Undoing: {undo.label}")
    return None


class UndoStack:
    """What can be undone and redone, newest last."""

    def __init__(self, limit: int = 50) -> None:
        self.limit = limit
        self.undos: list[Undo] = []
        self.redos: list[Undo] = []

    def push(self, undo: Undo | None) -> None:
        """Remember a new action of the user; what was undone before can no longer be redone."""
        if undo is None:
            return
        self.undos.append(undo)
        del self.undos[:-self.limit]
        self.redos.clear()

    def pushed_back(self, undo: Undo | None, redo: bool) -> None:
        """Remember the reverse of an undo (``redo=True``) or of a redo, without touching the other list."""
        if undo is None:
            return
        target = self.redos if redo else self.undos
        target.append(undo)
        del target[:-self.limit]

    def can_undo(self) -> bool:
        return bool(self.undos)

    def can_redo(self) -> bool:
        return bool(self.redos)

    def undo_label(self) -> str:
        return self.undos[-1].label if self.undos else ""

    def redo_label(self) -> str:
        return self.redos[-1].label if self.redos else ""
