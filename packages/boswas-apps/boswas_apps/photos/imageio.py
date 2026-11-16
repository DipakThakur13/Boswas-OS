"""Writing pictures: JPEG, PNG, WebP, BMP and TIFF, atomically (QtGui only, no widgets).

``save`` encodes into memory, then writes the file in one step
(``fileutil.atomic_write``): a crash or a full disk leaves the old file,
never half a picture.

What happens to the information around the pixels:

* JPEG: the EXIF block of the original (camera, lens, exposure, dates,
  GPS, maker notes) is carried over when one is given, with the
  orientation set to "upright" (the pixels are saved upright), the
  recorded pixel size corrected and the embedded preview removed. XMP,
  IPTC and comments are not carried over: Qt does not read them.
* All formats: pictures are saved in sRGB; Qt writes the sRGB colour
  profile where the format has room for one. EXIF is not written to PNG,
  WebP, BMP or TIFF: Qt has no way to.
* JPEG and BMP have no transparency: transparent parts become white.

``rotate_file`` turns a picture on disk by a quarter turn. A JPEG is not
re-encoded: only its EXIF orientation changes, so no quality is lost.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColorSpace, QImage, QImageWriter, QPainter

from . import decode, exif, formats
from .fileutil import atomic_write

# format key -> (Qt's name, extension for new files, what people call it, has a quality setting)
WRITERS = {
    "jpeg": ("jpeg", "jpg", "JPEG", True),
    "png": ("png", "png", "PNG", False),
    "webp": ("webp", "webp", "WebP", True),
    "bmp": ("bmp", "bmp", "BMP", False),
    "tiff": ("tiff", "tif", "TIFF", False),
}
NO_ALPHA = ("jpeg", "bmp")
DEFAULT_QUALITY = 90


class SaveError(Exception):
    """A picture could not be written; the text says why in plain words."""


@dataclass(frozen=True)
class SaveReport:
    path: str
    size: int                    # bytes written
    kept: tuple[str, ...]        # information about the picture that was carried over
    lost: tuple[str, ...]        # information that the new file does not have


def writable_formats() -> list[str]:
    """The keys of ``WRITERS`` this installation can write, in the order they are offered."""
    available = {bytes(name).decode("ascii", "replace").lower() for name in QImageWriter.supportedImageFormats()}
    return [key for key, (qt_name, _ext, _label, _quality) in WRITERS.items()
            if qt_name in available or (key == "jpeg" and "jpg" in available) or (key == "tiff" and "tif" in available)]


def format_for_path(path: str | os.PathLike) -> str | None:
    """The writable format a file name asks for, or None."""
    fmt = formats.format_for_extension(formats.extension(path))
    return fmt if fmt in WRITERS and fmt in writable_formats() else None


def file_filters() -> list[tuple[str, str]]:
    """(format key, file dialog filter) for the formats that can be written."""
    patterns = {"jpeg": "*.jpg *.jpeg", "png": "*.png", "webp": "*.webp", "bmp": "*.bmp", "tiff": "*.tif *.tiff"}
    return [(key, f"{WRITERS[key][2]} ({patterns[key]})") for key in writable_formats()]


def flattened(image: QImage) -> QImage:
    """``image`` on white, without an alpha channel."""
    if not image.hasAlphaChannel():
        return image if image.format() == QImage.Format.Format_RGB32 else image.convertToFormat(
            QImage.Format.Format_RGB32)
    result = QImage(image.size(), QImage.Format.Format_RGB32)
    result.fill(Qt.GlobalColor.white)
    painter = QPainter(result)
    painter.drawImage(0, 0, image)
    painter.end()
    result.setColorSpace(image.colorSpace())
    return result


def encode(image: QImage, fmt: str, quality: int = DEFAULT_QUALITY) -> bytes:
    """``image`` as the bytes of a file of format ``fmt``. SaveError when it cannot be encoded."""
    if fmt not in WRITERS or fmt not in writable_formats():
        raise SaveError(f"Pictures cannot be saved as {formats.display_name(fmt)} here.")
    if image.isNull():
        raise SaveError("There is no picture to save.")
    if fmt in NO_ALPHA:
        image = flattened(image)
    if not image.colorSpace().isValid():
        image = QImage(image)
        image.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.SRgb))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, WRITERS[fmt][0].encode("ascii"))
    if WRITERS[fmt][3]:
        writer.setQuality(max(1, min(100, int(quality))))
    if fmt == "jpeg":
        writer.setOptimizedWrite(True)
    elif fmt == "tiff":
        writer.setCompression(1)                 # LZW: lossless
    ok = writer.write(image)
    buffer.close()
    if not ok or data.isEmpty():
        raise SaveError(f"The picture could not be encoded: {writer.errorString()}.")
    return bytes(data.data())


def save(image: QImage, path: str | os.PathLike, fmt: str | None = None, quality: int = DEFAULT_QUALITY, *,
         exif_block: bytes | None = None, had_exif: bool = False) -> SaveReport:
    """Write ``image`` to ``path`` (see the module text). ``exif_block`` is the EXIF of the original."""
    path = os.fspath(path)
    fmt = fmt or format_for_path(path)
    if fmt is None:
        raise SaveError("The file name does not end in a picture type that can be saved (.jpg, .png, .webp, "
                        ".bmp, .tif).")
    data = encode(image, fmt, quality)
    kept: list[str] = []
    lost: list[str] = []
    if fmt == "jpeg" and exif_block is not None:
        prepared = exif.prepared_for_save(exif_block, image.width(), image.height())
        if prepared is not None:
            try:
                data = exif.jpeg_with_exif(data, prepared)
                kept.append("camera details, date and place (EXIF)")
            except ValueError:
                prepared = None
        if prepared is None:
            lost.append("camera details, date and place (EXIF)")
    elif exif_block is not None or had_exif:
        lost.append("camera details, date and place (EXIF)")
    if fmt != "bmp":
        kept.append("colour profile (sRGB)")
    if fmt in NO_ALPHA and image.hasAlphaChannel():
        lost.append("transparency (now white)")
    try:
        atomic_write(path, data)
    except OSError as exc:
        raise SaveError(f"{os.path.basename(path)} could not be written: {exc.strerror or exc}.") from exc
    return SaveReport(path, len(data), tuple(kept), tuple(lost))


def read_exif_block(path: str | os.PathLike) -> bytes | None:
    """The EXIF block of a JPEG file, to hand to ``save``; None when it has none or is no JPEG."""
    try:
        with open(path, "rb") as handle:
            if handle.read(2) != b"\xff\xd8":
                return None
            handle.seek(0)
            return exif.jpeg_exif_block(handle)
    except (OSError, ValueError):
        return None


# Formats a quarter turn can be written back to without losing anything.
_LOSSLESS_REWRITE = ("png", "bmp", "tiff")
MAX_ROTATE_BYTES = 512 * 1024 * 1024


def can_rotate_file(info: decode.ImageInfo) -> bool:
    """Whether ``rotate_file`` can turn this picture on disk without losing quality or frames."""
    if info.animated or info.vector or info.frames > 1:
        return False
    return info.format == "jpeg" or (info.format in _LOSSLESS_REWRITE and info.format in writable_formats())


def rotate_file(path: str | os.PathLike, clockwise: bool = True) -> None:
    """Turn the picture in ``path`` by a quarter turn, on disk.

    JPEG: the EXIF orientation is changed, the pixels are not touched.
    PNG, BMP, TIFF: the pixels are turned and written back (lossless
    formats). SaveError for other formats and when writing fails.
    """
    path = os.fspath(path)
    try:
        info = decode.probe(path)
    except decode.DecodeError as exc:
        raise SaveError(str(exc)) from exc
    if not can_rotate_file(info):
        raise SaveError(f"{formats.display_name(info.format)} pictures are not turned on disk.")
    turn = exif.ROTATE_CW if clockwise else exif.ROTATE_CCW
    try:
        if info.format == "jpeg":
            if info.file_size > MAX_ROTATE_BYTES:
                raise SaveError("The file is too large to be turned.")
            with open(path, "rb") as handle:
                data = handle.read()
            block = exif.jpeg_exif_block(data)
            current = 1
            if block is not None:
                try:
                    current = exif.parse_tiff(block).orientation
                except exif.ExifError:
                    current = 1
            atomic_write(path, exif.jpeg_set_orientation(data, exif.compose_orientation(current, turn)))
            return
        # Read as stored (bit depth, colour profile and text are kept), turn, write back.
        image = decode.read_as_stored(path)
        atomic_write(path, encode(decode.apply_orientation(image, turn), info.format))
    except OSError as exc:
        raise SaveError(f"{os.path.basename(path)} could not be written: {exc.strerror or exc}.") from exc
    except decode.DecodeError as exc:
        raise SaveError(str(exc)) from exc


SAMPLE_PIXELS = 3_000_000


def estimate_size(image: QImage, width: int, height: int, fmt: str, quality: int = DEFAULT_QUALITY) -> int:
    """About how many bytes ``image`` takes when saved at ``width`` x ``height`` as ``fmt``.

    Up to three megapixels the picture is really encoded, so the number is
    exact; a larger one is encoded at reduced size and the result scaled up
    by the number of pixels, which is close for photographs.
    """
    from .editops import resize
    width, height = max(1, int(width)), max(1, int(height))
    pixels = width * height
    if pixels <= SAMPLE_PIXELS:
        return len(encode(resize(image, width, height), fmt, quality))
    factor = (SAMPLE_PIXELS / pixels) ** 0.5
    sample = resize(image, max(1, round(width * factor)), max(1, round(height * factor)))
    return round(len(encode(sample, fmt, quality)) * pixels / (sample.width() * sample.height()))
