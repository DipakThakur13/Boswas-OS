"""Thumbnails of image files: made off the GUI thread, kept in the freedesktop.org thumbnail cache."""

from __future__ import annotations

import os
from collections import OrderedDict

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap

import boswas_fluent as fluent

from . import thumbs
from .clipformats import path_to_uri

_MAX_PIXELS = 120_000_000           # refuse to decode larger pictures
_MEMORY_ITEMS = 600
SOFTWARE = "Boswas Files"


def _save(image: QImage, target: str, path: str, mtime: float, size: int, width: int, height: int) -> bool:
    image.setText("Thumb::URI", path_to_uri(path))
    image.setText("Thumb::MTime", str(int(mtime)))
    image.setText("Thumb::Size", str(int(size)))
    if width > 0 and height > 0:
        image.setText("Thumb::Image::Width", str(width))
        image.setText("Thumb::Image::Height", str(height))
    image.setText("Software", SOFTWARE)
    partial = f"{target}.{os.getpid()}.tmp"
    try:
        if not image.save(partial, "PNG"):
            return False
        os.chmod(partial, 0o600)
        os.replace(partial, target)
    except OSError:
        try:
            os.unlink(partial)
        except OSError:
            pass
        return False
    return True


def make_thumbnail(path: str, mtime: float, pixels: int, root: str | None = None) -> QImage | None:
    """The thumbnail of an image file for a box of ``pixels``: from the cache when valid, else made and cached.

    Returns None when the file is not a picture Qt can read; that is
    remembered in the cache's ``fail`` directory until the file changes.
    Blocking: runs on a worker thread.
    """
    size_name, side = thumbs.size_for(pixels)
    cached = thumbs.find_valid(path, mtime, side, root)
    if cached is not None:
        image = QImage(cached)
        if not image.isNull():
            return image
    if thumbs.has_failed(path, mtime, root):
        return None
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return None
    reader = QImageReader(path)
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    original = reader.size()
    image = QImage()
    if original.isValid() and 0 < original.width() * original.height() <= _MAX_PIXELS:
        if original.width() > side or original.height() > side:
            reader.setScaledSize(original.scaled(QSize(side, side), Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
    if image.isNull():
        try:
            marker = QImage(1, 1, QImage.Format.Format_ARGB32)
            marker.fill(0)
            thumbs.ensure_dirs(thumbs.FAIL_DIR, root)
            _save(marker, thumbs.fail_path(path, root), path, mtime, file_size, 0, 0)
        except OSError:
            pass
        return None
    if image.width() > side or image.height() > side:
        image = image.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    if original.width() > side or original.height() > side:      # a picture smaller than a thumbnail is its own
        try:
            thumbs.ensure_dirs(size_name, root)
            _save(QImage(image), thumbs.thumb_path(path, size_name, root), path, mtime, file_size,
                  original.width(), original.height())
        except OSError:
            pass
    return image


class Thumbnailer(QObject):
    """Gives pixmaps to the views at once when it has them, and later (``ready``) when it had to make them."""

    ready = Signal(str)                 # the path whose thumbnail arrived

    def __init__(self, parent: QObject | None = None, root: str | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self.worker = fluent.Worker(self, max_threads=2)
        self._memory: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._failed: set[tuple] = set()
        self._pending: set[tuple] = set()

    def get(self, path: str, mtime: float, size: int, pixels: int) -> QPixmap | None:
        """The thumbnail if it is in memory; else None, and ``ready(path)`` follows if one can be made."""
        if not thumbs.wants_thumbnail(path, size, self.root):
            return None
        key = (path, int(mtime), thumbs.size_for(pixels)[0])
        pixmap = self._memory.get(key)
        if pixmap is not None:
            self._memory.move_to_end(key)
            return pixmap
        if key in self._failed or key in self._pending:
            return None
        self._pending.add(key)
        self.worker.submit(make_thumbnail, path, mtime, pixels, self.root,
                           on_done=lambda image, k=key: self._loaded(k, image),
                           on_error=lambda _error, k=key: self._loaded(k, None))
        return None

    def _loaded(self, key: tuple, image: QImage | None) -> None:
        self._pending.discard(key)
        if image is None or image.isNull():
            if len(self._failed) > 5000:
                self._failed.clear()
            self._failed.add(key)
            return
        self._memory[key] = QPixmap.fromImage(image)
        while len(self._memory) > _MEMORY_ITEMS:
            self._memory.popitem(last=False)
        self.ready.emit(key[0])

    def forget(self, path: str) -> None:
        for key in [key for key in self._memory if key[0] == path]:
            del self._memory[key]
        self._failed = {key for key in self._failed if key[0] != path}

    def shutdown(self) -> None:
        self.worker.shutdown()
