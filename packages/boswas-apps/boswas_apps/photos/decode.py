"""Reading pictures: what a file is, and its pixels, with limits on size and memory (QtGui and QtSvg, no widgets).

Every function here may run on a worker thread. A file is whatever
somebody put on disk, so before pixels are decoded the header is asked for
the size, and:

* a picture that would need more than ``MAX_DECODE_BYTES`` to decode is
  refused with ``DecodeError("too-large")`` (a PNG whose header claims
  60 000 x 60 000 pixels never gets its memory);
* a picture with more than ``DISPLAY_PIXELS`` pixels is decoded to a
  reduced copy: JPEG by decoding at 1/2, 1/4 or 1/8 (the decoder then
  never holds the full picture), other formats by scaling after decoding;
* Qt's own allocation limit is set just above ``MAX_DECODE_BYTES`` as a
  second line of defence;
* at most one decode that needs a lot of memory runs at a time.

Pictures come back upright (the EXIF orientation applied), in sRGB, with
8 bits per channel: ``Format_RGB32`` or ``Format_ARGB32_Premultiplied``.

SVG is rendered with QtSvg from the bytes of the file (``svgz`` is
unpacked here, with a limit on the unpacked size).
"""

from __future__ import annotations

import math
import os
import threading
import zlib
from dataclasses import dataclass, replace

from PySide6.QtCore import QByteArray, QLoggingCategory, QRectF, QSize, Qt
from PySide6.QtGui import QColorSpace, QImage, QImageIOHandler, QImageReader, QPainter, QTransform
from PySide6.QtSvg import QSvgRenderer

from . import exif, formats

MAX_DECODE_BYTES = 1 << 30              # the most memory a decode may need
DISPLAY_PIXELS = 48_000_000             # the most pixels of a picture kept for display and editing
ALLOCATION_LIMIT_MB = 1100              # Qt's own limit, just above MAX_DECODE_BYTES
HEAVY_BYTES = 192 * 1024 * 1024         # decodes above this run one at a time
MAX_SVG_BYTES = 32 * 1024 * 1024        # an SVG file, and an unpacked svgz
SVG_DEFAULT_SIDE = 1024                 # the long side given to an SVG that names no size
SVG_RASTER_SIDE = 4096                  # the long side of the raster copy of an SVG (editing, copying)
MAX_SIDE = 1_000_000                    # no picture format has more pixels on a side

_TRANSFORMATIONS = {
    QImageIOHandler.Transformation.TransformationNone: 1,
    QImageIOHandler.Transformation.TransformationMirror: 2,
    QImageIOHandler.Transformation.TransformationRotate180: 3,
    QImageIOHandler.Transformation.TransformationFlip: 4,
    QImageIOHandler.Transformation.TransformationFlipAndRotate90: 5,
    QImageIOHandler.Transformation.TransformationRotate90: 6,
    QImageIOHandler.Transformation.TransformationMirrorAndRotate90: 7,
    QImageIOHandler.Transformation.TransformationRotate270: 8,
}
_MESSAGES = {
    "missing": ("This file is no longer there", "It was moved, renamed or deleted."),
    "unreadable": ("This file cannot be read", "You do not have permission to open it."),
    "empty": ("This file is empty", "There is no picture in it."),
    "not-a-picture": ("This file is not a picture", "Photos does not know this kind of file."),
    "unsupported": ("This kind of picture cannot be shown", ""),
    "corrupt": ("This picture is damaged", "The file is incomplete or was not saved correctly."),
    "too-large": ("This picture is too large to open", ""),
}

# Formats whose files always begin with a signature ``formats.sniff`` knows: a file with such an
# extension and other content is no picture, not a damaged one.
_MAGIC_FORMATS = frozenset(("png", "jpeg", "gif", "webp", "tiff", "bmp", "ico", "cur", "avif", "heif", "heic",
                            "jxl", "qoi"))

_heavy = threading.Lock()
_configured = False
_formats: frozenset[str] | None = None


class DecodeError(Exception):
    """A file could not be shown. ``kind`` is one of the keys of ``_MESSAGES``; ``title`` and ``detail``
    are sentences for the user."""

    def __init__(self, kind: str, detail: str = "") -> None:
        title, default = _MESSAGES.get(kind, _MESSAGES["corrupt"])
        self.kind = kind
        self.title = title
        self.detail = detail or default
        super().__init__(f"{title}. {self.detail}".strip())


@dataclass(frozen=True)
class ImageInfo:
    """What the header of a picture file says."""

    path: str
    format: str                     # Qt's name of the format: "jpeg", "png", "svg", ...
    width: int                      # as seen (orientation applied)
    height: int
    orientation: int = 1            # EXIF orientation 1..8 of the stored pixels
    animated: bool = False
    frames: int = 1
    has_alpha: bool | None = None   # None: not known before decoding
    bits_per_pixel: int | None = None
    vector: bool = False
    file_size: int = 0
    mtime: float = 0.0

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1_000_000


@dataclass
class Loaded:
    """A decoded picture."""

    info: ImageInfo
    image: QImage                   # upright; at most DISPLAY_PIXELS pixels
    reduced: bool = False           # the image has fewer pixels than the picture
    truncated: bool = False         # the file ends early; part of the picture is missing
    svg: bytes | None = None        # the SVG document, when the picture is one
    dpi: tuple[float, float] | None = None


def configure() -> None:
    """Set Qt's allocation limit for image readers (once per process)."""
    global _configured
    if not _configured:
        QImageReader.setAllocationLimit(ALLOCATION_LIMIT_MB)
        # What a decoder thinks of a damaged file is shown to the user in the window, not logged
        # line by line for every thumbnail.
        QLoggingCategory.setFilterRules("qt.gui.imageio*=false\nqt.svg*=false")
        _configured = True


def supported_formats() -> frozenset[str]:
    """The format names this installation reads (Qt's image plugins, and SVG through QtSvg)."""
    global _formats
    if _formats is None:
        names = {bytes(name).decode("ascii", "replace").lower() for name in QImageReader.supportedImageFormats()}
        names.discard("pdf")
        names.update(("svg", "svgz"))
        if "jpg" in names:
            names.add("jpeg")
        if "tif" in names:
            names.add("tiff")
        if "heif" in names or "heic" in names:
            names.update(("heif", "heic"))
        _formats = frozenset(names)
    return _formats


def supported_extensions() -> frozenset[str]:
    """The file extensions (lower case, no dot) of pictures this installation reads."""
    known = supported_formats()
    return frozenset(ext for ext, (fmt, _mime) in formats.EXTENSIONS.items() if fmt in known)


def supported_mime_types() -> list[str]:
    known = supported_formats()
    return sorted({mime for _ext, (fmt, mime) in formats.EXTENSIONS.items() if fmt in known})


def missing_format_hint(fmt: str) -> str:
    package = formats.PLUGIN_PACKAGES.get(fmt)
    name = formats.display_name(fmt)
    if package:
        return f"This is a {name} picture. Support for it comes with the package {package}, which is not installed."
    return f"This is a {name} picture, which this installation cannot read."


# --- orientation -------------------------------------------------------------------------------------------

def orientation_transform(orientation: int) -> QTransform:
    a, b, c, d = exif.ORIENTATION_MATRICES.get(orientation, exif.ORIENTATION_MATRICES[1])
    # QTransform(m11, m12, m21, m22, dx, dy) maps (x, y) to (x*m11 + y*m21, x*m12 + y*m22).
    return QTransform(a, c, b, d, 0, 0)


def apply_orientation(image: QImage, orientation: int) -> QImage:
    """``image`` turned and mirrored as an EXIF orientation says; exact (no resampling)."""
    if orientation not in exif.ORIENTATION_MATRICES or orientation == 1 or image.isNull():
        return image
    return image.transformed(orientation_transform(orientation), Qt.TransformationMode.FastTransformation)


# --- helpers -----------------------------------------------------------------------------------------------

def _stat(path: str) -> os.stat_result:
    try:
        status = os.stat(path)
    except FileNotFoundError:
        raise DecodeError("missing") from None
    except PermissionError:
        raise DecodeError("unreadable") from None
    except OSError as exc:
        raise DecodeError("unreadable", exc.strerror or str(exc)) from None
    if not os.path.isfile(path):
        raise DecodeError("not-a-picture", "This is a folder or a special file.")
    if status.st_size == 0:
        raise DecodeError("empty")
    return status


def _sniff(path: str) -> str | None:
    try:
        return formats.sniff_file(path)
    except PermissionError:
        raise DecodeError("unreadable") from None
    except OSError as exc:
        raise DecodeError("unreadable", exc.strerror or str(exc)) from None


def _kind_of(path: str) -> str | None:
    """The format of a file by its content, else by its extension."""
    sniffed = _sniff(path)
    if sniffed == "svgz" and formats.extension(path) != "svgz":
        sniffed = None                           # some other gzip file
    return sniffed or formats.format_for_extension(formats.extension(path))


def _bytes_per_pixel(reader: QImageReader) -> float:
    fmt = reader.imageFormat()
    if fmt == QImage.Format.Format_Invalid:
        return 4.0
    bits = QImage.toPixelFormat(fmt).bitsPerPixel()
    return max(1, bits) / 8


def _reader(path: str) -> QImageReader:
    configure()
    reader = QImageReader(path)
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    return reader


def _fail(path: str, reader: QImageReader | None, kind: str | None) -> DecodeError:
    """The error for a file Qt could not read."""
    if kind is not None and kind not in supported_formats():
        return DecodeError("unsupported", missing_format_hint(kind))
    try:
        sniffed = formats.sniff_file(path)
    except OSError:
        sniffed = None
    if kind is None or (sniffed is None and kind in _MAGIC_FORMATS):
        return DecodeError("not-a-picture", "Its content is not a picture Photos knows.")
    detail = ""
    if reader is not None and reader.error() == QImageReader.ImageReaderError.InvalidDataError:
        detail = "The file is incomplete or was not saved correctly."
    return DecodeError("corrupt", detail)


def _to_display(image: QImage) -> QImage:
    """sRGB, 8 bits per channel, a format the painter draws fast."""
    space = image.colorSpace()
    if space.isValid() and space != QColorSpace(QColorSpace.NamedColorSpace.SRgb):
        try:
            converted = image.convertedToColorSpace(QColorSpace(QColorSpace.NamedColorSpace.SRgb))
            if not converted.isNull():
                image = converted
        except (RuntimeError, AttributeError):
            pass
    wanted = (QImage.Format.Format_ARGB32_Premultiplied if image.hasAlphaChannel()
              else QImage.Format.Format_RGB32)
    if image.format() != wanted:
        image = image.convertToFormat(wanted)
    return image


def _jpeg_is_cut_short(path: str) -> bool:
    """Whether a JPEG file lacks its end marker (a download or copy that stopped early)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 4096))
            tail = handle.read().rstrip(b"\x00")
    except OSError:
        return False
    return b"\xff\xd9" not in tail[-64:]


def fit_size(width: int, height: int, max_side: int) -> QSize:
    """The size of a ``width`` x ``height`` picture scaled down to ``max_side`` on its long side.

    Never larger than the picture, never less than one pixel on a side
    (a 1 x 100 000 strip stays one pixel wide).
    """
    longest = max(width, height, 1)
    if longest <= max_side:
        return QSize(max(1, width), max(1, height))
    return QSize(max(1, round(width * max_side / longest)), max(1, round(height * max_side / longest)))


# --- SVG ---------------------------------------------------------------------------------------------------

def read_svg(path: str) -> bytes:
    """The SVG document of a file (unpacked when it is an svgz), within the size limits."""
    status = _stat(path)
    if status.st_size > MAX_SVG_BYTES:
        raise DecodeError("too-large", "The drawing is larger than Photos opens.")
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_SVG_BYTES + 1)
    except PermissionError:
        raise DecodeError("unreadable") from None
    except OSError as exc:
        raise DecodeError("unreadable", exc.strerror or str(exc)) from None
    if data[:2] == b"\x1f\x8b":
        try:
            unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data = unpacker.decompress(data, MAX_SVG_BYTES + 1)
        except zlib.error:
            raise DecodeError("corrupt") from None
        if len(data) > MAX_SVG_BYTES:
            raise DecodeError("too-large", "The drawing is larger than Photos opens.")
    return data


def svg_renderer(data: bytes) -> QSvgRenderer:
    """A renderer for an SVG document; DecodeError when the document is not valid SVG."""
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        raise DecodeError("corrupt", "The drawing is not valid SVG.")
    renderer.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
    return renderer


def svg_natural_size(renderer: QSvgRenderer) -> QSize:
    """The size an SVG asks for, made sane: at least one pixel, at most ``MAX_SIDE`` on a side."""
    size = renderer.defaultSize()
    width, height = size.width(), size.height()
    if width <= 0 or height <= 0:
        box = renderer.viewBoxF()
        if box.width() > 0 and box.height() > 0:
            scale = SVG_DEFAULT_SIDE / max(box.width(), box.height())
            width, height = round(box.width() * scale), round(box.height() * scale)
        else:
            width = height = SVG_DEFAULT_SIDE
    return QSize(max(1, min(width, MAX_SIDE)), max(1, min(height, MAX_SIDE)))


def render_svg(renderer: QSvgRenderer, size: QSize, clip: QRectF | None = None,
               device_size: QSize | None = None) -> QImage:
    """The drawing at ``size`` pixels as a picture.

    With ``clip`` (a rectangle in the coordinates of the drawing at
    ``size``) only that part is rendered, into a picture of ``device_size``
    pixels: what a zoomed-in view needs, however large ``size`` is.
    """
    if clip is None:
        target = QSize(max(1, size.width()), max(1, size.height()))
        image = QImage(target, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, target.width(), target.height()))
        painter.end()
        return image
    target = device_size or QSize(max(1, round(clip.width())), max(1, round(clip.height())))
    image = QImage(target, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(target.width() / max(clip.width(), 1e-6), target.height() / max(clip.height(), 1e-6))
    painter.translate(-clip.x(), -clip.y())
    renderer.render(painter, QRectF(0, 0, size.width(), size.height()))
    painter.end()
    return image


# --- probing and loading -----------------------------------------------------------------------------------

def probe(path: str | os.PathLike) -> ImageInfo:
    """What the header of ``path`` says, without decoding pixels. DecodeError when it is no picture."""
    path = os.fspath(path)
    status = _stat(path)
    kind = _kind_of(path)
    if kind in ("svg", "svgz"):
        size = svg_natural_size(svg_renderer(read_svg(path)))
        return ImageInfo(path, "svg", size.width(), size.height(), has_alpha=True, vector=True,
                         file_size=status.st_size, mtime=status.st_mtime)
    reader = _reader(path)
    if not reader.canRead():
        raise _fail(path, reader, kind)
    size = reader.size()
    fmt = bytes(reader.format()).decode("ascii", "replace").lower() or (kind or "")
    fmt = {"jpg": "jpeg", "tif": "tiff"}.get(fmt, fmt)
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        raise _fail(path, reader, kind)
    orientation = _TRANSFORMATIONS.get(reader.transformation(), 1)
    width, height = size.width(), size.height()
    if exif.swaps_sides(orientation):
        width, height = height, width
    frames = max(1, reader.imageCount())             # frames of an animation, pages of a TIFF, sizes of an icon
    animated = reader.supportsAnimation() and frames > 1
    pixel_format = reader.imageFormat()
    has_alpha = bits = None
    if pixel_format != QImage.Format.Format_Invalid:
        described = QImage.toPixelFormat(pixel_format)
        has_alpha = described.alphaUsage() == described.AlphaUsage.UsesAlpha
        bits = described.bitsPerPixel()
    return ImageInfo(path, fmt, width, height, orientation, animated, frames, has_alpha, bits, False,
                     status.st_size, status.st_mtime)


def _decode(path: str, info: ImageInfo, kind: str | None, max_pixels: int) -> tuple[QImage, bool]:
    """Decode the first frame of a raster picture to at most ``max_pixels`` pixels; (image, reduced)."""
    reader = _reader(path)
    size = reader.size()
    stored_w, stored_h = size.width(), size.height()
    pixels = stored_w * stored_h
    estimate = pixels * _bytes_per_pixel(reader)
    reduced = False
    if info.format == "jpeg":
        divisor = 1
        while divisor < 8 and pixels / (divisor * divisor) > max_pixels:
            divisor *= 2
        if divisor > 1:
            reader.setScaledSize(QSize(max(1, math.ceil(stored_w / divisor)), max(1, math.ceil(stored_h / divisor))))
            estimate /= divisor * divisor
            reduced = True
        if estimate > MAX_DECODE_BYTES:
            raise DecodeError("too-large", f"It has {stored_w:,} × {stored_h:,} pixels.".replace(",", " "))
    else:
        if estimate > MAX_DECODE_BYTES:
            raise DecodeError("too-large", f"It has {stored_w:,} × {stored_h:,} pixels.".replace(",", " "))
        if pixels > max_pixels:
            scale = math.sqrt(max_pixels / pixels)
            reader.setScaledSize(QSize(max(1, int(stored_w * scale)), max(1, int(stored_h * scale))))
            reduced = True
    gate = _heavy if estimate > HEAVY_BYTES else None
    if gate is not None:
        gate.acquire()
    try:
        image = reader.read()
        if image.isNull():
            raise _fail(path, reader, kind)
        image = _to_display(image)
        if image.width() * image.height() > max_pixels:
            scale = math.sqrt(max_pixels / (image.width() * image.height()))
            image = image.scaled(max(1, int(image.width() * scale)), max(1, int(image.height() * scale)),
                                 Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            reduced = True
    finally:
        if gate is not None:
            gate.release()
    return image, reduced


def load(path: str | os.PathLike, max_pixels: int = DISPLAY_PIXELS) -> Loaded:
    """Decode ``path`` for display and editing. DecodeError when it cannot be shown."""
    path = os.fspath(path)
    info = probe(path)
    kind = _kind_of(path)
    if info.vector:
        data = read_svg(path)
        renderer = svg_renderer(data)
        natural = QSize(info.width, info.height)
        raster = fit_size(natural.width(), natural.height(), SVG_RASTER_SIDE)
        if max(natural.width(), natural.height()) < SVG_DEFAULT_SIDE:
            # a small drawing gets a raster copy that is still worth editing or copying
            scale = SVG_DEFAULT_SIDE / max(natural.width(), natural.height())
            raster = QSize(max(1, round(natural.width() * scale)), max(1, round(natural.height() * scale)))
        image = render_svg(renderer, raster)
        return Loaded(replace(info, has_alpha=True, bits_per_pixel=32), image, False, False, data, None)
    image, reduced = _decode(path, info, kind, max_pixels)
    dpi = None
    if image.dotsPerMeterX() > 0 and image.dotsPerMeterY() > 0:
        dpi = (image.dotsPerMeterX() * 0.0254, image.dotsPerMeterY() * 0.0254)
    info = replace(info, has_alpha=image.hasAlphaChannel() if info.has_alpha is None else info.has_alpha,
                   bits_per_pixel=info.bits_per_pixel or image.depth())
    truncated = info.format == "jpeg" and _jpeg_is_cut_short(path)
    return Loaded(info, image, reduced, truncated, None, dpi)


def load_scaled(path: str | os.PathLike, max_side: int) -> tuple[QImage, ImageInfo]:
    """The picture scaled down to ``max_side`` pixels on its long side (thumbnails, previews, slideshow).

    JPEG is decoded at reduced size by the decoder; the first frame stands
    for an animation. Returns the image (upright, sRGB) and the header.
    """
    path = os.fspath(path)
    info = probe(path)
    if info.vector:
        renderer = svg_renderer(read_svg(path))
        return render_svg(renderer, fit_size(info.width, info.height, max_side)), info
    kind = _kind_of(path)
    reader = _reader(path)
    size = reader.size()
    stored_w, stored_h = size.width(), size.height()
    estimate = stored_w * stored_h * _bytes_per_pixel(reader)
    target = fit_size(stored_w, stored_h, max_side)
    if info.format == "jpeg":
        # the decoder halves as often as the target allows: the full picture is never held
        divisor = 1
        while divisor < 8 and stored_w / (divisor * 2) >= target.width() and stored_h / (divisor * 2) >= target.height():
            divisor *= 2
        estimate /= divisor * divisor
    if estimate > MAX_DECODE_BYTES:
        raise DecodeError("too-large", f"It has {stored_w:,} × {stored_h:,} pixels.".replace(",", " "))
    if target != size:
        reader.setScaledSize(target)
    gate = _heavy if estimate > HEAVY_BYTES else None
    if gate is not None:
        gate.acquire()
    try:
        image = reader.read()
    finally:
        if gate is not None:
            gate.release()
    if image.isNull():
        raise _fail(path, reader, kind)
    return _to_display(image), info


def read_as_stored(path: str | os.PathLike) -> QImage:
    """The picture upright but otherwise as the file has it: bit depth, colour profile and text kept.

    For writing a picture back without losing anything. DecodeError when
    it cannot be read or needs more than ``MAX_DECODE_BYTES``.
    """
    path = os.fspath(path)
    _stat(path)
    kind = _kind_of(path)
    reader = _reader(path)
    size = reader.size()
    if not size.isValid():
        raise _fail(path, reader, kind)
    estimate = size.width() * size.height() * _bytes_per_pixel(reader)
    if estimate > MAX_DECODE_BYTES:
        raise DecodeError("too-large", f"It has {size.width():,} × {size.height():,} pixels.".replace(",", " "))
    gate = _heavy if estimate > HEAVY_BYTES else None
    if gate is not None:
        gate.acquire()
    try:
        image = reader.read()
    finally:
        if gate is not None:
            gate.release()
    if image.isNull():
        raise _fail(path, reader, kind)
    return image
