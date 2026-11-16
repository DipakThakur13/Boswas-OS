"""Pictures for the tiles and the now-playing bar: album covers and video thumbnails, loaded off the GUI thread.

``Artwork.cover(path, size)`` and ``Artwork.thumbnail(path, size)`` return
a QPixmap at once when it is in memory, else None; the picture is then
fetched in the background and ``loaded(key)`` says when a view should
paint again. Results are kept as small PNG files in the cache directory
(named after the file's path, size and modification time), so a second
start shows them without opening a single media file.

A cover is foreign data: it is decoded with QImageReader, refused when it
claims to be larger than ``MAX_PIXELS``, and scaled while it is decoded.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap

import boswas_fluent as fluent

from . import library
from .backend import FrameGrabber

MAX_PIXELS = 64_000_000           # a cover that claims more pixels than this is not decoded
MEMORY_ENTRIES = 600
ART_SIZE = 512                    # the picture handed to the desktop (MPRIS)


def decode_cover(data: bytes, size: int) -> QImage | None:
    """Image bytes as a square QImage of ``size`` pixels (scaled and cropped to its centre); None if not an image."""
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    full = reader.size()
    if not full.isValid() or full.width() <= 0 or full.height() <= 0:
        return None
    if full.width() * full.height() > MAX_PIXELS or max(full.width(), full.height()) > 32768:
        return None
    scaled = full.scaled(QSize(size, size), Qt.AspectRatioMode.KeepAspectRatioByExpanding)
    reader.setScaledSize(scaled)
    image = reader.read()
    if image.isNull():
        return None
    return crop_square(image, size)


def crop_square(image: QImage, size: int) -> QImage:
    side = min(image.width(), image.height())
    square = image.copy(QRect((image.width() - side) // 2, (image.height() - side) // 2, side, side))
    if side != size:
        square = square.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
    return square


def _cached(directory: Path, key: str) -> tuple[QImage | None, bool]:
    """(picture, known): the cached picture; ``known`` is True when the cache says "there is none"."""
    path = directory / f"{key}.png"
    if path.is_file():
        image = QImage(str(path))
        if not image.isNull():
            return image, True
    return None, (directory / f"{key}.none").exists()


def _store(directory: Path, key: str, image: QImage | None) -> None:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if image is None:
            (directory / f"{key}.none").touch()
        else:
            image.save(str(directory / f"{key}.png"), "PNG")
    except OSError:
        pass                              # the cache is a convenience; without it the picture is made again


def load_cover(path: str, size: int, directory: Path, embedded: bool = True) -> QImage | None:
    """Worker thread: the cover of an audio file at ``size`` pixels, through the cache."""
    key = library.cache_key(path, f"cover{size}")
    image, known = _cached(directory, key)
    if image is not None or known:
        return image
    data = library.cover_bytes(path, embedded)
    image = decode_cover(data, size) if data else None
    _store(directory, key, image)
    return image


def load_thumbnail(path: str, width: int, directory: Path) -> tuple[QImage | None, bool]:
    """Worker thread: the cached thumbnail of a video; (None, False) means a frame has to be grabbed."""
    return _cached(directory, library.cache_key(path, f"thumb{width}"))


def scale_frame(image: QImage, width: int) -> QImage:
    """A video frame as a 16:9 picture ``width`` pixels wide (cropped to the middle)."""
    height = width * 9 // 16
    scaled = image.scaled(QSize(width, height), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
    return scaled.copy(QRect((scaled.width() - width) // 2, (scaled.height() - height) // 2, width, height))


class Artwork(QObject):
    """Covers and thumbnails by file path, with a memory cache and a disk cache."""

    loaded = Signal(str)                  # key: paint the tiles of this file again
    duration_found = Signal(str, int)     # a video's length, learned while its thumbnail was made

    def __init__(self, cache_dir: Path, worker: fluent.Worker, grabber: FrameGrabber | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.covers_dir = Path(cache_dir) / "covers"
        self.thumbs_dir = Path(cache_dir) / "thumbnails"
        self.worker = worker
        self.grabber = grabber
        self._memory: OrderedDict[str, QPixmap | None] = OrderedDict()
        self._pending: set[str] = set()

    @staticmethod
    def key(kind: str, path: str, size: int) -> str:
        return f"{kind}\x1f{size}\x1f{path}"

    def _remember(self, key: str, pixmap: QPixmap | None) -> None:
        self._memory[key] = pixmap
        self._memory.move_to_end(key)
        while len(self._memory) > MEMORY_ENTRIES:
            self._memory.popitem(last=False)

    def peek(self, key: str) -> QPixmap | None:
        return self._memory.get(key)

    def is_pending(self) -> bool:
        return bool(self._pending) or (self.grabber is not None and self.grabber.busy())

    # --- covers ---
    def cover(self, path: str, size: int, embedded: bool = True) -> QPixmap | None:
        """The cover of an audio file, or None (not loaded yet, or there is none)."""
        key = self.key("cover", path, size)
        if key in self._memory:
            self._memory.move_to_end(key)
            return self._memory[key]
        if key not in self._pending:
            self._pending.add(key)
            self.worker.submit(load_cover, path, size, self.covers_dir, embedded,
                               on_done=lambda image, key=key: self._finished(key, image),
                               on_error=lambda _error, key=key: self._finished(key, None))
        return None

    def _finished(self, key: str, image: QImage | None) -> None:
        self._pending.discard(key)
        self._remember(key, QPixmap.fromImage(image) if image is not None else None)
        self.loaded.emit(key)

    def put(self, key: str, image: QImage) -> None:
        """Give a picture that came from elsewhere (the backend found one in a stream)."""
        self._remember(key, QPixmap.fromImage(image))
        self.loaded.emit(key)

    # --- video thumbnails ---
    def thumbnail(self, path: str, width: int) -> QPixmap | None:
        key = self.key("thumb", path, width)
        if key in self._memory:
            self._memory.move_to_end(key)
            return self._memory[key]
        if key not in self._pending:
            self._pending.add(key)
            self.worker.submit(load_thumbnail, path, width, self.thumbs_dir,
                               on_done=lambda found, key=key, path=path, width=width:
                               self._thumbnail_cached(key, path, width, found),
                               on_error=lambda _error, key=key: self._finished(key, None))
        return None

    def _thumbnail_cached(self, key: str, path: str, width: int, found: tuple[QImage | None, bool]) -> None:
        image, known = found
        if image is not None or known or self.grabber is None:
            self._finished(key, image)
            return
        self.grabber.grab(path, lambda frame, duration: self._grabbed(key, path, width, frame, duration))

    def _grabbed(self, key: str, path: str, width: int, frame: object, duration: int) -> None:
        image = scale_frame(frame, width) if isinstance(frame, QImage) and not frame.isNull() else None
        if duration > 0:
            self.duration_found.emit(path, duration)
        self.worker.submit(_store, self.thumbs_dir, library.cache_key(path, f"thumb{width}"), image)
        self._finished(key, image)

    # --- the picture for the desktop ---
    def art_file(self, path: str) -> str:
        """The cache file that holds the cover of ``path`` at ART_SIZE pixels, or "" when it is not there yet."""
        candidate = self.covers_dir / f"{library.cache_key(path, f'cover{ART_SIZE}')}.png"
        return str(candidate) if candidate.is_file() else ""

    def forget_missing(self) -> None:
        """Forget every "this file has no picture" note, in memory and on disk (after a library refresh)."""
        for key in [key for key, value in self._memory.items() if value is None]:
            del self._memory[key]
        for directory in (self.covers_dir, self.thumbs_dir):
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.name.endswith(".none"):
                            os.unlink(entry.path)
            except OSError:
                pass
