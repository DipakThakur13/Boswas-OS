"""A PDF document as the application uses it: Qt PDF's QPdfDocument plus what it does not keep itself.

``PdfDocument`` loads a file (asking the caller for a password when the
file needs one), remembers the size of every page, and gives the text, the
links and the information of the document in plain Python values. The text
of pages is cached, because search reads it again and again.

Qt PDF takes one lock around everything it asks its engine, so the calls
made here are safe from the render and search threads too.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from PySide6.QtCore import QModelIndex, QObject, QPointF
from PySide6.QtPdf import QPdfBookmarkModel, QPdfDocument, QPdfLinkModel

READY, NEEDS_PASSWORD, WRONG_PASSWORD, FAILED = "ready", "password", "wrong-password", "error"
MAX_TEXT_CACHE_CHARS = 64 * 1024 * 1024

_ERRORS = {
    QPdfDocument.Error.FileNotFound: "The file is not there any more, or it cannot be read.",
    QPdfDocument.Error.InvalidFileFormat: "This file is not a PDF document, or it is damaged.",
    QPdfDocument.Error.UnsupportedSecurityScheme: "This document is protected in a way that cannot be opened here.",
    QPdfDocument.Error.DataNotYetAvailable: "The file could not be read completely.",
    QPdfDocument.Error.Unknown: "The document could not be opened.",
}
_FIELDS = (("Title", QPdfDocument.MetaDataField.Title), ("Author", QPdfDocument.MetaDataField.Author),
           ("Subject", QPdfDocument.MetaDataField.Subject), ("Keywords", QPdfDocument.MetaDataField.Keywords),
           ("Creator", QPdfDocument.MetaDataField.Creator), ("Producer", QPdfDocument.MetaDataField.Producer),
           ("Created", QPdfDocument.MetaDataField.CreationDate),
           ("Modified", QPdfDocument.MetaDataField.ModificationDate))


@dataclass(frozen=True)
class Link:
    rect: tuple[float, float, float, float]      # on the page, in points, origin top left
    page: int                                    # target page, or -1 for a web link
    y: float                                     # where on the target page, in points from the top
    url: str                                     # the address of a web link, else ""


def paper_name(width: float, height: float) -> str:
    """``A4`` for 595 × 842 points; empty for a size that has no common name."""
    short, long_ = sorted((width, height))
    for name, (w, h) in (("A3", (842, 1191)), ("A4", (595, 842)), ("A5", (420, 595)), ("Letter", (612, 792)),
                         ("Legal", (612, 1008)), ("Tabloid", (792, 1224))):
        if abs(short - w) <= 2 and abs(long_ - h) <= 2:
            return name
    return ""


def size_text(width: float, height: float) -> str:
    """A page size for people: millimetres, and the name of the paper when it has one."""
    text = f"{width / 72 * 25.4:.0f} × {height / 72 * 25.4:.0f} mm"
    name = paper_name(width, height)
    if name:
        text += f" ({name}, {'landscape' if width > height else 'portrait'})"
    return text


class PdfDocument(QObject):
    def __init__(self, path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = os.path.abspath(path)
        self.qt = QPdfDocument(self)
        self.state = FAILED
        self.error_text = ""
        self.sizes: list[tuple[float, float]] = []
        self.password = ""
        self._links: dict[int, list[Link]] = {}
        self._link_model: QPdfLinkModel | None = None
        self._text: dict[int, str] = {}
        self._text_chars = 0
        self._lock = threading.Lock()
        self._bookmarks: QPdfBookmarkModel | None = None

    # --- loading ---
    def load(self, password: str | None = None) -> str:
        """Open the file; returns READY, NEEDS_PASSWORD, WRONG_PASSWORD or FAILED (see ``error_text``)."""
        self._links.clear()
        with self._lock:
            self._text.clear()
            self._text_chars = 0
        self.sizes = []
        if password is not None:
            self.qt.setPassword(password)
            self.password = password
        error = self.qt.load(self.path)
        if error == QPdfDocument.Error.None_:
            count = self.qt.pageCount()
            if count <= 0:
                self.state, self.error_text = FAILED, "The document has no pages."
                return self.state
            self.sizes = []
            for page in range(count):
                size = self.qt.pagePointSize(page)
                width, height = size.width(), size.height()
                if width <= 0 or height <= 0:                 # a page the engine could not measure
                    width, height = 595.0, 842.0
                self.sizes.append((width, height))
            self.state, self.error_text = READY, ""
        elif error == QPdfDocument.Error.IncorrectPassword:
            self.state = WRONG_PASSWORD if password else NEEDS_PASSWORD
            self.error_text = ""
        else:
            self.state = FAILED
            self.error_text = _ERRORS.get(error, _ERRORS[QPdfDocument.Error.Unknown])
        return self.state

    @property
    def page_count(self) -> int:
        return len(self.sizes)

    def close(self) -> None:
        self.qt.close()
        self.sizes = []
        self.state = FAILED

    # --- facts ---
    def label(self, page: int) -> str:
        """The page number as the document prints it (``iv``, ``12``); the plain number when it has none."""
        text = self.qt.pageLabel(page) if 0 <= page < self.page_count else ""
        return text or str(page + 1)

    def page_for_label(self, text: str) -> int:
        """The page a typed page number means: a label of the document first, then the plain number; -1 for neither."""
        text = text.strip()
        if not text:
            return -1
        page = self.qt.pageIndexForLabel(text)
        if 0 <= page < self.page_count:
            return page
        if text.isdigit() and 1 <= int(text) <= self.page_count:
            return int(text) - 1
        return -1

    def title(self) -> str:
        return str(self.qt.metaData(QPdfDocument.MetaDataField.Title) or "").strip()

    def properties(self) -> list[tuple[str, str]]:
        """Name and value of everything the Properties dialog shows."""
        rows = [("File", os.path.basename(self.path)), ("Folder", os.path.dirname(self.path))]
        for name, field in _FIELDS:
            value = self.qt.metaData(field)
            if hasattr(value, "isValid"):
                text = value.toLocalTime().toString("yyyy-MM-dd HH:mm") if value.isValid() else ""
            else:
                text = str(value or "").strip()
            rows.append((name, text[:2000] or "—"))
        rows.append(("Pages", f"{self.page_count:,}"))
        if self.sizes:
            first = self.sizes[0]
            same = all(abs(w - first[0]) < 1 and abs(h - first[1]) < 1 for w, h in self.sizes)
            rows.append(("Page size", size_text(*first) + ("" if same else " (first page; sizes vary)")))
        try:
            size = os.path.getsize(self.path)
            rows.append(("File size", _bytes_text(size)))
        except OSError:
            rows.append(("File size", "—"))
        rows.append(("Password", "Protected" if self.password else "None"))
        return rows

    def bookmarks(self) -> QPdfBookmarkModel:
        """The outline of the document as an item model (roles of QPdfBookmarkModel)."""
        if self._bookmarks is None:
            self._bookmarks = QPdfBookmarkModel(self)
            self._bookmarks.setDocument(self.qt)
        return self._bookmarks

    def has_outline(self) -> bool:
        return self.bookmarks().rowCount(QModelIndex()) > 0

    # --- links ---
    def links(self, page: int) -> list[Link]:
        found = self._links.get(page)
        if found is not None:
            return found
        if self._link_model is None:
            self._link_model = QPdfLinkModel(self)
            self._link_model.setDocument(self.qt)
        model = self._link_model
        model.setPage(page)
        found = []
        for row in range(min(model.rowCount(QModelIndex()), 2000)):
            index = model.index(row, 0)
            rect = index.data(QPdfLinkModel.Role.Rectangle.value)
            target = index.data(QPdfLinkModel.Role.Page.value)
            location = index.data(QPdfLinkModel.Role.Location.value)
            url = index.data(QPdfLinkModel.Role.Url.value)
            address = url.toString() if url is not None and not url.isEmpty() else ""
            if rect is None or rect.isEmpty():
                continue
            target = target if isinstance(target, int) and 0 <= target < self.page_count else -1
            if target < 0 and not address:
                continue
            found.append(Link((rect.x(), rect.y(), rect.width(), rect.height()), target,
                              location.y() if location is not None else 0.0, address[:4096]))
        if len(self._links) > 512:
            self._links.clear()
        self._links[page] = found
        return found

    def link_at(self, page: int, x: float, y: float) -> Link | None:
        for link in self.links(page):
            left, top, width, height = link.rect
            if left <= x <= left + width and top <= y <= top + height:
                return link
        return None

    # --- text ---
    def text(self, page: int) -> str:
        """The text of a page as the engine reads it (lines end with a line break). Safe from any thread."""
        with self._lock:
            cached = self._text.get(page)
        if cached is not None:
            return cached
        text = self.qt.getAllText(page).text() if 0 <= page < self.page_count else ""
        with self._lock:
            if self._text_chars > MAX_TEXT_CACHE_CHARS:
                self._text.clear()
                self._text_chars = 0
            self._text[page] = text
            self._text_chars += len(text)
        return text

    def rects_at(self, page: int, start: int, length: int) -> list[tuple[float, float, float, float]]:
        """Where ``length`` characters from ``start`` are on the page: rectangles in points, origin top left."""
        selection = self.qt.getSelectionAtIndex(page, start, length)
        return [(box.x(), box.y(), box.width(), box.height())
                for box in (polygon.boundingRect() for polygon in selection.bounds())]

    def index_at(self, page: int, x: float, y: float) -> int:
        """The character under a point of the page (points), or -1."""
        for reach in (4.0, 12.0):
            selection = self.qt.getSelection(page, QPointF(x, y), QPointF(x + reach, y))
            if selection.isValid():
                return selection.startIndex()
            selection = self.qt.getSelection(page, QPointF(x - reach, y), QPointF(x, y))
            if selection.isValid():
                return max(selection.startIndex(), selection.endIndex() - 1)
        return -1


def _bytes_text(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{size} bytes"
