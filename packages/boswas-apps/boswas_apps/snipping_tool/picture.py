"""Pictures of the Snipping Tool (QtGui only, no widgets): reading with limits, drawing the marks, writing.

Every function here may run on a worker thread. A file is whatever
somebody put on disk, so before a picture is decoded its header is asked
for the size and the picture is refused when it is larger than
``MAX_SIDE`` pixels a side or would need more than ``MAX_DECODED_BYTES``
of memory. Only PNG, JPEG and WebP are opened, told apart by their
content, not by the file name.

``render`` draws the marks of a ``State`` on the visible part of the
picture at the picture's own resolution: that is what is saved and copied.
Files are written atomically: a temporary file in the same folder, then
``os.replace``; a full disk leaves the old file, never half a picture.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QLoggingCategory, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QImageReader, QImageWriter, QPainter,
                           QPainterPath, QPen, QPolygonF)

from . import annotations
from .annotations import Item, State

MAX_SIDE = 16384
MAX_DECODED_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
ALLOCATION_LIMIT_MB = 300               # Qt's own limit for image readers, just above MAX_DECODED_BYTES
FORMATS = ("png", "jpeg", "webp")
SAVE_FORMATS = ("png", "jpeg")
JPEG_QUALITY = 92
EXTENSIONS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "jpe": "jpeg", "webp": "webp"}

_configured = False


class PictureError(Exception):
    """A picture could not be read or written; ``title`` and ``detail`` are sentences for the user."""

    def __init__(self, title: str, detail: str = "") -> None:
        super().__init__(f"{title}. {detail}".strip() if detail else title)
        self.title = title
        self.detail = detail


def configure() -> None:
    """Set Qt's allocation limit for image readers (once per process)."""
    global _configured
    if not _configured:
        QImageReader.setAllocationLimit(ALLOCATION_LIMIT_MB)
        # What a decoder thinks of a damaged file is said in the window, not logged.
        QLoggingCategory.setFilterRules("qt.gui.imageio*=false")
        _configured = True


def sniff(head: bytes) -> str | None:
    """The format the first bytes of a file say: "png", "jpeg", "webp" or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def check_size(width: int, height: int) -> None:
    """Refuse a picture that is empty or larger than the limits."""
    if width <= 0 or height <= 0:
        raise PictureError("This picture is damaged", "It has no pixels.")
    if width > MAX_SIDE or height > MAX_SIDE or width * height * 4 > MAX_DECODED_BYTES:
        raise PictureError("This picture is too large to open",
                           f"It is {width} × {height} pixels. The Snipping Tool opens pictures of up to "
                           f"{MAX_SIDE} pixels a side and {MAX_DECODED_BYTES // (1024 * 1024)} MB.")


def normalized(image: QImage) -> QImage:
    """``image`` in the two formats the editor works in, at a device pixel ratio of 1."""
    wanted = QImage.Format.Format_ARGB32_Premultiplied if image.hasAlphaChannel() else QImage.Format.Format_RGB32
    result = image if image.format() == wanted else image.convertToFormat(wanted)
    if result.devicePixelRatio() != 1.0:
        result = result.copy()
        result.setDevicePixelRatio(1.0)
    return result


def load(path: str | os.PathLike) -> QImage:
    """Decode a PNG, JPEG or WebP file with the limits of the module text; PictureError otherwise."""
    configure()
    path = os.fspath(path)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        raise PictureError("This file is no longer there", "It was moved, renamed or deleted.") from None
    except OSError as exc:
        raise PictureError("This file cannot be read", exc.strerror or str(exc)) from None
    if not stat.S_ISREG(info.st_mode):
        raise PictureError("This is not a picture", "Only files can be opened.")
    if info.st_size == 0:
        raise PictureError("This file is empty", "There is no picture in it.")
    if info.st_size > MAX_FILE_BYTES:
        raise PictureError("This file is too large to open",
                           f"The Snipping Tool opens files of up to {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    try:
        with open(path, "rb") as stream:
            head = stream.read(16)
    except OSError as exc:
        raise PictureError("This file cannot be read", exc.strerror or str(exc)) from None
    fmt = sniff(head)
    if fmt is None:
        raise PictureError("This file is not a picture the Snipping Tool opens",
                           "It opens PNG, JPEG and WebP pictures.")
    reader = QImageReader(path, fmt.encode("ascii"))
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid():
        if fmt == "webp" and fmt.encode() not in [bytes(name) for name in QImageReader.supportedImageFormats()]:
            raise PictureError("WebP pictures cannot be opened", "Support for WebP is not installed.")
        raise PictureError("This picture is damaged", "The file is incomplete or was not saved correctly.")
    check_size(size.width(), size.height())
    image = reader.read()
    if image.isNull():
        raise PictureError("This picture is damaged", "The file is incomplete or was not saved correctly.")
    check_size(image.width(), image.height())
    return normalized(image)


def accept_image(image: QImage) -> QImage:
    """A picture from elsewhere (the clipboard), checked against the limits like a file."""
    if image is None or image.isNull():
        raise PictureError("There is no picture", "")
    check_size(image.width(), image.height())
    return normalized(image)


# --- the marks -----------------------------------------------------------------------------------------------

def text_font(family: str, size: float) -> QFont:
    font = QFont(family)
    font.setPixelSize(max(1, round(size)))
    return font


def measure_text(text: str, size: float, family: str) -> tuple[float, float]:
    """The width and height a text mark of letter height ``size`` takes, in picture pixels."""
    metrics = QFontMetricsF(text_font(family, size))
    return metrics.horizontalAdvance(text), metrics.height()


def draw_item(painter: QPainter, item: Item, family: str) -> None:
    """One mark; ``painter`` is set up in picture pixels."""
    colour = QColor(item.colour)
    if item.kind == "highlighter":
        colour.setAlphaF(annotations.HIGHLIGHTER_OPACITY)
    points = [QPointF(x, y) for x, y in item.points]
    if item.kind in ("pen", "highlighter"):
        if len(points) == 1:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(colour))
            painter.drawEllipse(points[0], item.width / 2, item.width / 2)
            return
        pen = QPen(colour, item.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        painter.drawPath(path)
    elif item.kind == "arrow":
        tail, head = item.points
        if tail == head:
            return
        end = annotations.arrow_shaft_end(tail, head, item.width)
        painter.setPen(QPen(colour, item.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(points[0], QPointF(*end))
        painter.setPen(QPen(colour, max(1.0, item.width / 2), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QBrush(colour))
        painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in annotations.arrow_head(tail, head, item.width)]))
    elif item.kind == "rectangle":
        box = annotations.Rect.from_points(*item.points)
        painter.setPen(QPen(colour, item.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap,
                            Qt.PenJoinStyle.MiterJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(box.x, box.y, box.width, box.height))
    elif item.kind == "text":
        font = text_font(family, item.width)
        painter.setFont(font)
        painter.setPen(colour)
        x, y = item.points[0]
        painter.drawText(QPointF(x, y + QFontMetricsF(font).ascent()), item.text)


def draw_items(painter: QPainter, items, family: str) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    for item in items:
        draw_item(painter, item, family)
    painter.restore()


def visible_rect(image: QImage, state: State) -> QRect:
    box = state.visible(image.width(), image.height())
    return QRect(int(box.x), int(box.y), int(box.width), int(box.height)).intersected(image.rect())


def render(image: QImage, state: State, family: str) -> QImage:
    """The picture as edited, at its full resolution: the crop applied, the marks drawn."""
    area = visible_rect(image, state)
    result = image.copy(area)
    wanted = QImage.Format.Format_ARGB32_Premultiplied if result.hasAlphaChannel() else QImage.Format.Format_RGB32
    if result.format() != wanted:
        result = result.convertToFormat(wanted)
    if state.items:
        painter = QPainter(result)
        painter.translate(-area.x(), -area.y())
        draw_items(painter, state.items, family)
        painter.end()
    return result


# --- writing -------------------------------------------------------------------------------------------------

def format_for_path(path: str | os.PathLike) -> str | None:
    """The format a file name asks for when saving: "png", "jpeg" or None."""
    fmt = EXTENSIONS.get(Path(path).suffix.lower().lstrip("."))
    return fmt if fmt in SAVE_FORMATS else None


def flattened(image: QImage) -> QImage:
    """``image`` on white, without transparency (for JPEG)."""
    if not image.hasAlphaChannel():
        return image
    result = QImage(image.size(), QImage.Format.Format_RGB32)
    result.fill(Qt.GlobalColor.white)
    painter = QPainter(result)
    painter.drawImage(0, 0, image)
    painter.end()
    return result


def encode(image: QImage, fmt: str, quality: int = JPEG_QUALITY) -> bytes:
    if fmt not in SAVE_FORMATS:
        raise PictureError("This kind of file cannot be saved", "Save as PNG or JPEG.")
    if fmt == "jpeg":
        image = flattened(image)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, fmt.encode("ascii"))
    if fmt == "jpeg":
        writer.setQuality(quality)
    else:
        writer.setCompression(6)
    if not writer.write(image):
        raise PictureError("The picture could not be saved", writer.errorString())
    buffer.close()
    return bytes(buffer.data())


def write_atomic(path: str | os.PathLike, data: bytes) -> None:
    """Write ``data`` to ``path`` in one step; an existing file keeps its permissions (new ones get 0644)."""
    target = Path(path)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        mode = 0o644
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save(image: QImage, state: State, path: str | os.PathLike, fmt: str, family: str) -> str:
    """Render and write the picture; returns the path. PictureError (with the reason) when it fails."""
    data = encode(render(image, state, family), fmt)
    try:
        write_atomic(path, data)
    except OSError as exc:
        raise PictureError("The picture could not be saved", exc.strerror or str(exc)) from None
    return os.fspath(path)
