"""Printing a tab (QtPrintSupport): to a printer or to a PDF file, page by page without holding up the window.

``AVAILABLE`` is False when this PySide6 comes without QtPrintSupport
(python3-pyside6.qtprintsupport is not installed); the editor then has no
Print and Page setup commands.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QTextDocument, QTextLayout, QTextOption

try:
    from PySide6.QtPrintSupport import QPageSetupDialog, QPrintDialog, QPrinter
    AVAILABLE = True
except ImportError:                       # the module is a package of its own in Debian
    QPageSetupDialog = QPrintDialog = QPrinter = None
    AVAILABLE = False

LINES_PER_STEP = 400


class PrintJob(QObject):
    """Prints the lines of a document: a header with the name, a footer with the page number.

    The work is done in steps from the event loop; ``progress(0..1)`` while
    it runs, ``finished(pages, message)`` at the end (pages is 0 and the
    message says why when nothing could be printed, -1 when cancelled).
    """

    progress = Signal(float)
    finished = Signal(int, str)

    def __init__(self, document: QTextDocument, printer, name: str, font: QFont, tab_width: int,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lines = document.toRawText().split(chr(0x2029))
        self._printer = printer
        self._name = name
        self._font = QFont(font)
        self._tab_width = tab_width
        self._index = 0
        self._painter: QPainter | None = None
        self._pages = 0
        self._cancelled = False
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._step)

    def start(self) -> None:
        painter = QPainter()
        if not painter.begin(self._printer):
            self.finished.emit(0, "The printer could not be started.")
            return
        self._painter = painter
        font = QFont(self._font, self._printer)
        painter.setFont(font)
        metrics = QFontMetricsF(font, self._printer)
        self._line_height = metrics.lineSpacing()
        page = QRectF(self._printer.pageRect(QPrinter.Unit.DevicePixel))
        self._page = QRectF(0, 0, page.width(), page.height())
        self._body = self._page.adjusted(0, self._line_height * 2, 0, -self._line_height * 2)
        self._option = QTextOption()
        self._option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._option.setTabStopDistance(metrics.horizontalAdvance(" ") * self._tab_width)
        self._font_on_printer = font
        self._y = self._body.top()
        self._begin_page()
        self._timer.start()

    def cancel(self) -> None:
        self._cancelled = True

    def _begin_page(self) -> None:
        self._pages += 1
        painter = self._painter
        painter.setPen(Qt.GlobalColor.black)
        painter.drawText(QRectF(0, 0, self._page.width(), self._line_height),
                         int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), self._name)
        painter.drawText(QRectF(0, self._page.height() - self._line_height, self._page.width(), self._line_height),
                         int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom), f"Page {self._pages}")
        self._y = self._body.top()

    def _step(self) -> None:
        if self._cancelled:
            self._end(-1, "Printing was cancelled.")
            return
        painter = self._painter
        for _ in range(LINES_PER_STEP):
            if self._index >= len(self._lines):
                self._end(self._pages, "")
                return
            layout = QTextLayout(self._lines[self._index], self._font_on_printer, self._printer)
            layout.setTextOption(self._option)
            layout.beginLayout()
            height = 0.0
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(self._body.width())
                line.setPosition(QRectF(0, height, 0, 0).topLeft())
                height += self._line_height
            layout.endLayout()
            height = max(height, self._line_height)
            for number in range(max(layout.lineCount(), 1)):
                if self._y + self._line_height > self._body.bottom() + 0.5:
                    if not self._printer.newPage():
                        self._end(0, "The printer stopped.")
                        return
                    self._begin_page()
                if number < layout.lineCount():
                    line = layout.lineAt(number)
                    line.draw(painter, QRectF(0, self._y - line.y(), 0, 0).topLeft())
                self._y += self._line_height
            self._index += 1
        self.progress.emit(self._index / max(len(self._lines), 1))

    def _end(self, pages: int, message: str) -> None:
        self._timer.stop()
        if self._painter is not None:
            if pages < 0:
                self._printer.abort()
            self._painter.end()
            self._painter = None
        self.finished.emit(pages, message)

    def run_to_end(self) -> int:
        """Print everything now (tests): returns the number of pages."""
        while self._timer.isActive():
            self._step()
        return self._pages
