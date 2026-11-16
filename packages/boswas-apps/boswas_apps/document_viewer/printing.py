"""Printing a document: page by page, rendered at the printer's resolution on the render thread.

Qt PDF draws pages into images, so a page is printed as an image that is
as sharp as the printer (up to a pixel limit that keeps a page within
memory). Each page is scaled to fit the printable area and centred; a page
that is wider than tall is turned on upright paper (and the other way
round) so it fills the sheet. Copies, duplex and the choice of pages are
what the print dialog set on the QPrinter.

QtPrintSupport is optional: without it ``AVAILABLE`` is False and the
application has no Print command.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Signal
from PySide6.QtGui import QImage, QPainter

from .render import Renderer, Request

try:
    from PySide6.QtPrintSupport import QPrintDialog, QPrinter
    AVAILABLE = True
except ImportError:                                   # python3-pyside6.qtprintsupport is not installed
    QPrintDialog = QPrinter = None
    AVAILABLE = False

MAX_PAGE_PIXELS = 40_000_000
_tags = 0


def new_printer():
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setFullPage(False)
    return printer


def ask(printer, parent, page_count: int, current_page: int) -> bool:
    """Show the print dialog for a document of ``page_count`` pages; True when the user chose Print."""
    dialog = QPrintDialog(printer, parent)
    dialog.setOption(QPrintDialog.PrintDialogOption.PrintPageRange, True)
    dialog.setOption(QPrintDialog.PrintDialogOption.PrintCurrentPage, True)
    dialog.setOption(QPrintDialog.PrintDialogOption.PrintCollateCopies, True)
    dialog.setMinMax(1, page_count)
    dialog.setFromTo(1, page_count)
    return dialog.exec() == QPrintDialog.DialogCode.Accepted


def chosen_pages(printer, page_count: int, current_page: int) -> list[int]:
    """The page indexes the print dialog left on ``printer``, in printing order."""
    choice = printer.printRange()
    if choice == QPrinter.PrintRange.CurrentPage:
        pages = [current_page]
    elif choice == QPrinter.PrintRange.PageRange:
        pages = []
        ranges = printer.pageRanges().toRangeList() if hasattr(printer, "pageRanges") else []
        if ranges:
            for item in ranges:
                pages.extend(range(item.from_ - 1, item.to))
        elif printer.fromPage() > 0:
            pages = list(range(printer.fromPage() - 1, max(printer.fromPage(), printer.toPage())))
        pages = [page for page in pages if 0 <= page < page_count] or list(range(page_count))
    else:
        pages = list(range(page_count))
    if printer.pageOrder() == QPrinter.PageOrder.LastPageFirst:
        pages.reverse()
    return pages


class PrintJob(QObject):
    """Prints ``pages`` of a document on ``printer``. ``progress(done, total)``, then ``finished(ok, message)``."""

    progress = Signal(int, int)
    finished = Signal(bool, str)

    def __init__(self, sizes: list[tuple[float, float]], renderer: Renderer, printer, pages: list[int],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        global _tags
        _tags += 1
        self._tag = _tags
        self._sizes = sizes
        self._renderer = renderer
        self._printer = printer
        copies = 1 if printer.supportsMultipleCopies() else max(1, printer.copyCount())
        self._pages = [page for _copy in range(copies) for page in pages]
        self._done = 0
        self._painter: QPainter | None = None
        self._request: Request | None = None
        self._ended = False
        self._renderer.rendered.connect(self._rendered)

    def start(self) -> None:
        if not self._pages:
            self._end(False, "There are no pages to print.")
            return
        self._painter = QPainter()
        if not self._painter.begin(self._printer):
            self._painter = None
            self._end(False, "The printer could not be started.")
            return
        self.progress.emit(0, len(self._pages))
        self._ask()

    def _target(self, page: int) -> tuple[QRectF, int, int, int]:
        """Where the page goes on the sheet (device pixels), the image size to render, and the quarter turns."""
        area = QRectF(self._painter.viewport())
        width, height = self._sizes[page]
        turn = 1 if (width > height) != (area.width() > area.height()) else 0
        if turn:
            width, height = height, width
        scale = min(area.width() / width, area.height() / height)
        target = QRectF(0, 0, width * scale, height * scale)
        target.moveCenter(area.center())
        pixels_w, pixels_h = target.width(), target.height()
        if pixels_w * pixels_h > MAX_PAGE_PIXELS:
            shrink = (MAX_PAGE_PIXELS / (pixels_w * pixels_h)) ** 0.5
            pixels_w, pixels_h = pixels_w * shrink, pixels_h * shrink
        return target, max(1, round(pixels_w)), max(1, round(pixels_h)), turn

    def _ask(self) -> None:
        page = self._pages[self._done]
        self._area, width, height, turn = self._target(page)
        self._request = Request("print", page, width, height, turn, None, self._tag)
        self._renderer.request(self._request, -10)

    def _rendered(self, request: Request, image: QImage) -> None:
        if self._ended or request != self._request:
            return
        if image.isNull():
            self._end(False, f"Page {request.page + 1} could not be rendered.")
            return
        self._painter.drawImage(self._area, image)
        self._done += 1
        self.progress.emit(self._done, len(self._pages))
        if self._done >= len(self._pages):
            self._end(True, "")
            return
        if not self._printer.newPage():
            self._end(False, "The printer stopped taking pages.")
            return
        self._ask()

    def cancel(self) -> None:
        if not self._ended:
            self._printer.abort()
            self._end(False, "")

    def _end(self, ok: bool, message: str) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            self._renderer.rendered.disconnect(self._rendered)
        except (RuntimeError, TypeError):
            pass
        if self._painter is not None:
            self._painter.end()
            self._painter = None
        self.finished.emit(ok, message)
