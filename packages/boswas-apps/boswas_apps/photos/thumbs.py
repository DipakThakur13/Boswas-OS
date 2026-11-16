"""Thumbnails: made on worker threads, cached on disk the freedesktop.org way and in memory.

``thumbnail(path, mtime, size)`` is the whole job for one file and runs on
any thread: it returns the cached thumbnail when there is one that was
made from the file as it is now, else decodes the picture at reduced size,
stores the result in ``$XDG_CACHE_HOME/thumbnails/large`` and returns it.
Files that cannot be decoded get a record in the ``fail`` directory, so
they are not tried again until they change.

``ThumbnailProvider`` is what views use (on the GUI thread). A view says
which pictures it shows now with ``want``; the provider keeps a few
decodes running, most recently wanted first, and forgets requests for
pictures that have scrolled out of sight before their turn came. That is
what keeps a grid of 50 000 pictures responsive: the work is bounded by
what is on screen, not by how far the user scrolled.
"""

from __future__ import annotations

import os
from collections import OrderedDict

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal
from PySide6.QtGui import QImage, QImageReader, QImageWriter, QPixmap

import boswas_fluent as fluent

from . import decode, formats, thumbcache
from .fileutil import atomic_write

SIZE_NAME = "large"
SIZE = thumbcache.SIZES[SIZE_NAME]
MEMORY_BUDGET = 160 * 1024 * 1024
MAX_THUMBNAIL_FILE = 4 * 1024 * 1024        # a cached thumbnail larger than this is not believed


class ThumbnailError(Exception):
    """No thumbnail could be made for a file."""


def _read_cached(cache_file: str, uri: str, mtime: float) -> QImage | None:
    try:
        if os.path.getsize(cache_file) > MAX_THUMBNAIL_FILE:
            return None
    except OSError:
        return None
    if not thumbcache.matches(thumbcache.read_texts(cache_file), uri, mtime):
        return None
    reader = QImageReader(cache_file, b"png")
    size = reader.size()
    if not size.isValid() or size.width() > 1024 or size.height() > 1024:
        return None
    image = reader.read()
    return None if image.isNull() else image


def _write_png(image: QImage, texts: dict[str, str], target: str) -> None:
    for key, value in texts.items():
        image.setText(key, value)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = QImageWriter(buffer, b"png").write(image)
    buffer.close()
    if not ok:
        raise OSError("thumbnail could not be encoded")
    atomic_write(target, bytes(data.data()), mode=0o600)


def cached(path: str, mtime: float, root=None) -> QImage | None:
    """The thumbnail of ``path`` from the disk cache when it is up to date, else None."""
    uri = thumbcache.file_uri(path)
    return _read_cached(str(thumbcache.thumbnail_path(path, SIZE_NAME, root)), uri, mtime)


def has_failed(path: str, mtime: float, root=None) -> bool:
    """Whether an earlier attempt to thumbnail the file as it is now failed."""
    record = thumbcache.fail_path(path, root)
    return thumbcache.matches(thumbcache.read_texts(record), thumbcache.file_uri(path), mtime)


def thumbnail(path: str, mtime: float, file_size: int = 0, root=None) -> QImage:
    """The thumbnail of ``path`` (at most 256 pixels on a side), from the cache or freshly made.

    ThumbnailError when the file cannot be decoded. Runs on any thread.
    """
    uri = thumbcache.file_uri(path)
    cache_file = thumbcache.thumbnail_path(path, SIZE_NAME, root)
    image = _read_cached(str(cache_file), uri, mtime)
    if image is not None:
        return image
    inside = thumbcache.is_inside_cache(path, root)
    if not inside and has_failed(path, mtime, root):
        raise ThumbnailError("failed before")
    try:
        image, info = decode.load_scaled(path, SIZE)
    except decode.DecodeError as exc:
        if not inside and exc.kind not in ("missing", "unreadable"):
            try:
                record = thumbcache.fail_path(path, root)
                thumbcache.ensure_directory(record.parent)
                empty = QImage(1, 1, QImage.Format.Format_ARGB32)
                empty.fill(0)
                _write_png(empty, thumbcache.texts_for(uri, mtime, file_size, 0, 0), str(record))
            except OSError:
                pass
        raise ThumbnailError(str(exc)) from exc
    if not inside:
        try:
            thumbcache.ensure_directory(cache_file.parent)
            mime = formats.mime_for_extension(formats.extension(path)) or ""
            _write_png(QImage(image), thumbcache.texts_for(uri, mtime, file_size, info.width, info.height, mime),
                       str(cache_file))
        except OSError:
            pass                       # a cache that cannot be written only costs time
    return image


class ThumbnailProvider(QObject):
    """Thumbnails for views (GUI thread). See the module text.

    ``pixmap(path, mtime)`` answers from memory at once (None: not there
    yet). ``want(client, items)`` replaces what ``client`` (any hashable)
    is waiting for; ``ready(path)`` is emitted when a thumbnail arrived and
    ``failed(path)`` when there will be none.
    """

    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None, *, threads: int | None = None, root=None,
                 budget: int = MEMORY_BUDGET) -> None:
        super().__init__(parent)
        count = threads or max(2, min(4, (os.cpu_count() or 2) - 1))
        self.worker = fluent.Worker(self, max_threads=count)
        self._threads = count
        self._root = root
        self._budget = budget
        self._used = 0
        self._memory: OrderedDict[str, tuple[int, QPixmap, int]] = OrderedDict()   # path -> (mtime, pixmap, bytes)
        self._failed: dict[str, int] = {}
        self._wanted: dict[object, OrderedDict[str, tuple[float, int]]] = {}
        self._order: list[object] = []           # clients, the one that asked last at the end
        self._running: set[str] = set()

    # --- answers ---
    def pixmap(self, path: str, mtime: float) -> QPixmap | None:
        entry = self._memory.get(path)
        if entry is None or entry[0] != int(mtime):
            return None
        self._memory.move_to_end(path)
        return entry[1]

    def has_failed(self, path: str, mtime: float) -> bool:
        return self._failed.get(path) == int(mtime)

    def cached_count(self) -> int:
        return len(self._memory)

    def memory_used(self) -> int:
        return self._used

    def pending(self) -> int:
        return len(self._running) + sum(len(items) for items in self._wanted.values())

    # --- requests ---
    def want(self, client: object, items) -> None:
        """``items``: (path, mtime, file size) of what ``client`` shows now, the most important first."""
        waiting: OrderedDict[str, tuple[float, int]] = OrderedDict()
        for path, mtime, size in items:
            if self.pixmap(path, mtime) is None and not self.has_failed(path, mtime):
                waiting[path] = (mtime, size)
        if waiting:
            self._wanted[client] = waiting
            if client in self._order:
                self._order.remove(client)
            self._order.append(client)
        else:
            self.forget(client)
        self._pump()

    def forget(self, client: object) -> None:
        self._wanted.pop(client, None)
        if client in self._order:
            self._order.remove(client)

    def invalidate(self, path: str) -> None:
        """Drop what is known about ``path`` (it was edited, renamed or deleted)."""
        entry = self._memory.pop(path, None)
        if entry is not None:
            self._used -= entry[2]
        self._failed.pop(path, None)

    def insert(self, path: str, mtime: float, image: QImage) -> None:
        """Put a thumbnail made elsewhere into the memory cache."""
        self._store(path, mtime, image)

    def _next(self) -> tuple[str, float, int] | None:
        for client in list(reversed(self._order)):
            waiting = self._wanted.get(client)
            while waiting:
                path, (mtime, size) = waiting.popitem(last=False)
                if path not in self._running and self.pixmap(path, mtime) is None:
                    return path, mtime, size
            self.forget(client)
        return None

    def _pump(self) -> None:
        while len(self._running) < self._threads:
            job = self._next()
            if job is None:
                return
            path, mtime, size = job
            self._running.add(path)
            task = self.worker.submit(thumbnail, path, mtime, size, self._root,
                                      on_done=lambda image, p=path, m=mtime: self._done(p, m, image),
                                      on_error=lambda _exc, p=path, m=mtime: self._error(p, m))
            if task is None:                    # the worker is shut down
                self._running.discard(path)
                return

    def _store(self, path: str, mtime: float, image: QImage) -> None:
        self.invalidate(path)
        pixmap = QPixmap.fromImage(image)
        cost = max(1, image.sizeInBytes())
        self._memory[path] = (int(mtime), pixmap, cost)
        self._used += cost
        while self._used > self._budget and len(self._memory) > 1:
            _old, (_m, _p, freed) = self._memory.popitem(last=False)
            self._used -= freed

    def _done(self, path: str, mtime: float, image: QImage) -> None:
        self._running.discard(path)
        self._store(path, mtime, image)
        self.ready.emit(path)
        self._pump()

    def _error(self, path: str, mtime: float) -> None:
        self._running.discard(path)
        self._failed[path] = int(mtime)
        if len(self._failed) > 20000:
            self._failed.clear()
        self.failed.emit(path)
        self._pump()

    def wait(self, timeout: float = 10.0) -> bool:
        """Process events until nothing is running or waiting (tests)."""
        import time
        deadline = time.monotonic() + timeout
        while (self._running or any(self._wanted.values())) and time.monotonic() < deadline:
            self.worker.wait(0.05)
        return not self._running and not any(self._wanted.values())

    def shutdown(self) -> None:
        self._wanted.clear()
        self._order.clear()
        self.worker.shutdown()
