"""Printing a picture: where it goes on the page, and sending it to a printer or a PDF file.

``place`` is the arithmetic (standard library only): a page, a picture and
one of three ways to put the one on the other. ``print_image`` draws the
picture through QtPrintSupport; it may run on a worker thread (Qt allows
painting on a printer from any thread). When QtPrintSupport is not
installed ``AVAILABLE`` is False and Photos does not offer printing.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from PySide6.QtCore import QMarginsF, QRectF
    from PySide6.QtGui import QImage, QPageLayout, QPainter
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
    AVAILABLE = True
except ImportError:                           # python3-pyside6.qtprintsupport is not installed
    AVAILABLE = False

FIT, FILL, ACTUAL = "fit", "fill", "actual"
MODES = (FIT, FILL, ACTUAL)
AUTO, PORTRAIT, LANDSCAPE = "auto", "portrait", "landscape"
DEFAULT_PICTURE_DPI = 96.0
PDF_PRINTER = "\x00pdf"                       # the entry "Save as PDF" of the printer list


class PrintError(Exception):
    """The picture could not be printed; the text says why in plain words."""


@dataclass(frozen=True)
class Placement:
    """Where a picture goes: ``target`` on the page (page units) shows ``source`` of the picture (pixels).
    Each is (x, y, width, height)."""

    target: tuple[float, float, float, float]
    source: tuple[float, float, float, float]


def place(page_width: float, page_height: float, image_width: int, image_height: int, mode: str = FIT, *,
          page_dpi: float = 300.0, picture_dpi: float = DEFAULT_PICTURE_DPI) -> Placement:
    """Put a picture on a page.

    ``fit``: the whole picture, as large as the page allows, centred.
    ``fill``: the page is covered; what does not fit is cut off evenly.
    ``actual``: the size the picture's resolution asks for (``picture_dpi``
    pixels to the inch), centred; what is larger than the page is cut off.
    """
    if page_width <= 0 or page_height <= 0 or image_width <= 0 or image_height <= 0:
        raise ValueError("a page and a picture need a size")
    if mode == FILL:
        scale = max(page_width / image_width, page_height / image_height)
        shown_w, shown_h = page_width / scale, page_height / scale
        return Placement((0.0, 0.0, page_width, page_height),
                         ((image_width - shown_w) / 2, (image_height - shown_h) / 2, shown_w, shown_h))
    if mode == ACTUAL:
        scale = page_dpi / max(1.0, picture_dpi)
    elif mode == FIT:
        scale = min(page_width / image_width, page_height / image_height)
    else:
        raise ValueError(f"no such print mode: {mode!r}")
    width, height = image_width * scale, image_height * scale
    shown_w, shown_h = min(width, page_width), min(height, page_height)
    return Placement(((page_width - shown_w) / 2, (page_height - shown_h) / 2, shown_w, shown_h),
                     ((image_width - shown_w / scale) / 2, (image_height - shown_h / scale) / 2,
                      shown_w / scale, shown_h / scale))


def landscape_for(image_width: int, image_height: int, orientation: str) -> bool:
    """Whether the page lies on its side: as asked, or (``auto``) when the picture is wider than tall."""
    if orientation == LANDSCAPE:
        return True
    if orientation == PORTRAIT:
        return False
    return image_width > image_height


def printers() -> list[tuple[str, str]]:
    """(name, description) of the printers of this computer, the default first. May block: run off the GUI thread."""
    if not AVAILABLE:
        return []
    found = []
    default = QPrinterInfo.defaultPrinterName()
    for info in QPrinterInfo.availablePrinters():
        name = info.printerName()
        label = info.description() or name
        found.append((name, label[:80]))
    found.sort(key=lambda entry: (entry[0] != default, entry[1].casefold()))
    return found


def print_image(image: "QImage", *, printer_name: str | None = None, pdf_path: str | None = None,
                mode: str = FIT, orientation: str = AUTO, copies: int = 1,
                picture_dpi: float = DEFAULT_PICTURE_DPI) -> None:
    """Print ``image`` on ``printer_name`` (None: the default printer) or into the PDF file ``pdf_path``.

    PrintError when there is no printer, the printer refuses or the file
    cannot be written.
    """
    if not AVAILABLE:
        raise PrintError("Printing support is not installed.")
    if image.isNull():
        raise PrintError("There is no picture to print.")
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    if pdf_path:
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(pdf_path)
    else:
        if printer_name:
            printer.setPrinterName(printer_name)
        if not printer.isValid():
            raise PrintError("No printer is set up on this computer.")
    printer.setCopyCount(max(1, min(99, int(copies))))
    printer.setPageOrientation(QPageLayout.Orientation.Landscape
                               if landscape_for(image.width(), image.height(), orientation)
                               else QPageLayout.Orientation.Portrait)
    if mode == FILL:
        printer.setFullPage(True)
        printer.setPageMargins(QMarginsF(0, 0, 0, 0))
    printer.setDocName("Picture")
    painter = QPainter()
    if not painter.begin(printer):
        raise PrintError("The printer did not accept the page." if not pdf_path
                         else "The PDF file could not be written.")
    try:
        page = painter.viewport()
        placed = place(page.width(), page.height(), image.width(), image.height(), mode,
                       page_dpi=float(printer.resolution()), picture_dpi=picture_dpi)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(QRectF(*placed.target), image, QRectF(*placed.source))
    finally:
        painter.end()
