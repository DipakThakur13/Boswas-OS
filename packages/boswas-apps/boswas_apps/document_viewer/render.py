"""Rendering pages off the GUI thread.

``Renderer`` owns one thread per document. Requests carry a priority; the
thread always takes the most urgent one, so what is on the screen comes
before thumbnails and before pages that are only near the screen. Requests
that are still waiting can be dropped (``keep_only``) when the view has
moved on, which is what keeps scrolling through a thousand pages from
rendering a thousand pages.

A request renders a whole page, or one part of it at full resolution
(``clip``): at high zoom a whole page would be hundreds of megabytes.
The image that comes back is opaque, on white paper, ready to be drawn.

Qt PDF's ``QPdfDocument.render`` may be called from another thread (its
own QPdfPageRenderer does the same) and releases Python's lock while it
works, so the GUI thread keeps running.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtPdf import QPdfDocument, QPdfDocumentRenderOptions

_ROTATIONS = (QPdfDocumentRenderOptions.Rotation.None_, QPdfDocumentRenderOptions.Rotation.Clockwise90,
              QPdfDocumentRenderOptions.Rotation.Clockwise180, QPdfDocumentRenderOptions.Rotation.Clockwise270)
MAX_SIDE = 16384                       # no image is asked for with a side longer than this


@dataclass(frozen=True)
class Request:
    kind: str                          # "page", "detail", "thumb", "print", "present", "copy", "recent"
    page: int
    width: int                         # the size of the whole (rotated) page in pixels at the wanted scale
    height: int
    rotation: int = 0                  # quarter turns clockwise
    clip: tuple[int, int, int, int] | None = None     # the part to render: x, y, w, h in those pixels
    tag: int = 0                       # tells otherwise equal requests apart (a generation counter)


def render(document: QPdfDocument, request: Request) -> QImage:
    """Render ``request`` now, on the calling thread; a null image when the engine gives none."""
    width = max(1, min(MAX_SIDE, request.width))
    height = max(1, min(MAX_SIDE, request.height))
    options = QPdfDocumentRenderOptions()
    options.setRotation(_ROTATIONS[request.rotation % 4])
    if request.clip is not None:
        x, y, w, h = request.clip
        options.setScaledSize(QSize(width, height))
        options.setScaledClipRect(QRect(x, y, w, h))
        raw = document.render(request.page, QSize(max(1, w), max(1, h)), options)
    else:
        raw = document.render(request.page, QSize(width, height), options)
    if raw.isNull():
        return raw
    image = QImage(raw.size(), QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)                # paper: a PDF page has no background of its own
    painter = QPainter(image)
    painter.drawImage(0, 0, raw)
    painter.end()
    return image


class Renderer(QObject):
    """The render thread of one document. ``rendered(request, image)`` arrives on the GUI thread."""

    rendered = Signal(object, QImage)

    def __init__(self, document: QPdfDocument, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._document = document
        self._queue: dict[Request, tuple[int, int]] = {}       # request → (priority, order)
        self._order = 0
        self._condition = threading.Condition()
        self._stopped = False
        self._busy = False
        self._thread = threading.Thread(target=self._run, name="pdf-render", daemon=True)
        self._thread.start()

    def request(self, request: Request, priority: int = 0) -> None:
        """Ask for an image. Lower ``priority`` comes first; among equals the latest request comes first."""
        with self._condition:
            if self._stopped:
                return
            self._order += 1
            self._queue[request] = (priority, -self._order)
            self._condition.notify()

    def keep_only(self, kind: str, wanted) -> None:
        """Forget the waiting requests of ``kind`` that are not in ``wanted``."""
        with self._condition:
            for request in [request for request in self._queue if request.kind == kind and request not in wanted]:
                del self._queue[request]

    def pending(self) -> int:
        with self._condition:
            return len(self._queue) + (1 if self._busy else 0)

    def stop(self) -> None:
        """End the thread; waits for the page it is rendering, so the document can be closed afterwards."""
        with self._condition:
            self._stopped = True
            self._queue.clear()
            self._condition.notify()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(10)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
                request = min(self._queue, key=self._queue.get)
                del self._queue[request]
                self._busy = True
            try:
                image = render(self._document, request)
            except Exception:                       # a page the engine cannot draw must not end the thread
                image = QImage()
            with self._condition:
                self._busy = False
                if self._stopped:
                    return
            try:
                self.rendered.emit(request, image)
            except RuntimeError:                    # the receiver is gone
                return
