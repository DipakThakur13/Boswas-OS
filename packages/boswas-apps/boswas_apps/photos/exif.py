"""EXIF from JPEG (the APP1 segment) and TIFF files, read with strict bounds checking.

Standard library only. The data comes from files anybody may have made,
so nothing in it is trusted:

* every offset and every length is checked against the size of the data
  before a byte is read; a tag whose value lies outside is ignored;
* a directory has at most ``MAX_IFD_ENTRIES`` entries, a value at most
  ``MAX_VALUE_BYTES`` bytes, text at most ``MAX_TEXT`` characters;
* a directory is never read twice (offsets that point in a circle end
  the walk) and only the directories this program shows are followed
  (IFD0, the Exif directory, the GPS directory);
* numbers that make no sense (a zero denominator, 97 degrees of
  latitude, orientation 12) become "not known", never an exception.

``read_file`` and ``parse_jpeg`` return None when there is no usable EXIF
block; they do not raise on damaged data.

The module also changes two things in an EXIF block without touching the
rest, for saving pictures: ``with_orientation`` (used to turn a JPEG
without re-encoding it) and ``prepared_for_save`` (orientation 1, new
pixel size, no thumbnail).
"""

from __future__ import annotations

import io
import mmap
import os
import struct
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

MAX_IFD_ENTRIES = 512
MAX_VALUE_BYTES = 64 * 1024
MAX_TEXT = 200
MAX_JPEG_SEGMENTS = 4096
MAX_APP1_PAYLOAD = 65533
EXIF_HEADER = b"Exif\x00\x00"

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
_BYTE, _ASCII, _SHORT, _LONG, _RATIONAL, _SBYTE, _UNDEFINED, _SSHORT, _SLONG, _SRATIONAL = range(1, 11)

# IFD0
TAG_MAKE, TAG_MODEL, TAG_ORIENTATION = 0x010F, 0x0110, 0x0112
TAG_X_RESOLUTION, TAG_Y_RESOLUTION, TAG_RESOLUTION_UNIT = 0x011A, 0x011B, 0x0128
TAG_SOFTWARE, TAG_DATETIME, TAG_ARTIST, TAG_COPYRIGHT = 0x0131, 0x0132, 0x013B, 0x8298
TAG_EXIF_IFD, TAG_GPS_IFD = 0x8769, 0x8825
TAG_THUMB_OFFSET, TAG_THUMB_LENGTH = 0x0201, 0x0202
# Exif directory
TAG_EXPOSURE_TIME, TAG_F_NUMBER, TAG_EXPOSURE_PROGRAM, TAG_ISO = 0x829A, 0x829D, 0x8822, 0x8827
TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED = 0x9003, 0x9004
TAG_EXPOSURE_BIAS, TAG_METERING, TAG_FLASH, TAG_FOCAL_LENGTH = 0x9204, 0x9207, 0x9209, 0x920A
TAG_PIXEL_X, TAG_PIXEL_Y = 0xA002, 0xA003
TAG_WHITE_BALANCE, TAG_FOCAL_35MM = 0xA403, 0xA405
TAG_LENS_MAKE, TAG_LENS_MODEL = 0xA433, 0xA434
# GPS directory
GPS_LAT_REF, GPS_LAT, GPS_LON_REF, GPS_LON, GPS_ALT_REF, GPS_ALT = 1, 2, 3, 4, 5, 6


class ExifError(ValueError):
    """The data is not a TIFF structure this module can read."""


@dataclass(frozen=True)
class Exif:
    """What Photos shows of an EXIF block. A field is None (or "" for text) when the file does not say."""

    orientation: int = 1
    make: str = ""
    model: str = ""
    lens_make: str = ""
    lens_model: str = ""
    software: str = ""
    artist: str = ""
    copyright: str = ""
    date_taken: str = ""                  # "YYYY:MM:DD HH:MM:SS" as stored, checked to be a real date
    date_modified: str = ""
    exposure_time: Fraction | None = None     # seconds
    f_number: Fraction | None = None
    iso: int | None = None
    focal_length: Fraction | None = None      # millimetres
    focal_length_35mm: int | None = None
    exposure_bias: Fraction | None = None     # EV
    exposure_program: int | None = None
    metering_mode: int | None = None
    white_balance: int | None = None
    flash: int | None = None
    latitude: float | None = None             # degrees, north positive
    longitude: float | None = None            # degrees, east positive
    altitude: float | None = None             # metres above sea level
    pixel_width: int | None = None
    pixel_height: int | None = None
    x_resolution: Fraction | None = None
    y_resolution: Fraction | None = None
    resolution_unit: int | None = None        # 2 inch, 3 centimetre

    def dpi(self) -> tuple[float, float] | None:
        """Resolution in dots per inch, when the block has a plausible one."""
        if self.x_resolution is None or self.y_resolution is None:
            return None
        factor = 2.54 if self.resolution_unit == 3 else 1.0
        x, y = float(self.x_resolution) * factor, float(self.y_resolution) * factor
        if not (1 <= x <= 100000 and 1 <= y <= 100000):
            return None
        return x, y

    def taken(self) -> datetime | None:
        return parse_datetime(self.date_taken)


@dataclass(frozen=True)
class _Entry:
    kind: int
    count: int
    position: int          # where the value bytes are, inside the data
    slot: int              # where the 12-byte directory entry is


class _Tiff:
    """A TIFF structure in a bytes-like object, every access bounds-checked."""

    def __init__(self, data) -> None:
        self.data = data
        self.size = len(data)
        if self.size < 8:
            raise ExifError("too short for a TIFF header")
        order = bytes(data[0:2])
        if order == b"II":
            self.endian = "<"
        elif order == b"MM":
            self.endian = ">"
        else:
            raise ExifError("no byte order mark")
        if self.u16(2) != 42:
            raise ExifError("not a TIFF header")
        self.first_ifd = self.u32(4)

    def take(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise ExifError("read outside the data")
        return bytes(self.data[offset:offset + length])

    def u16(self, offset: int) -> int:
        return struct.unpack(self.endian + "H", self.take(offset, 2))[0]

    def u32(self, offset: int) -> int:
        return struct.unpack(self.endian + "I", self.take(offset, 4))[0]

    def directory(self, offset: int) -> tuple[dict[int, _Entry], int, int]:
        """The entries of the directory at ``offset`` (damaged ones left out), where the offset of the
        next directory is stored, and that offset."""
        if offset < 8 or offset + 2 > self.size:
            raise ExifError("directory outside the data")
        count = self.u16(offset)
        if count > MAX_IFD_ENTRIES:
            raise ExifError("directory with too many entries")
        end = offset + 2 + count * 12
        if end > self.size:
            raise ExifError("directory runs past the data")
        entries: dict[int, _Entry] = {}
        for index in range(count):
            slot = offset + 2 + index * 12
            tag, kind = self.u16(slot), self.u16(slot + 2)
            number = self.u32(slot + 4)
            unit = _TYPE_SIZES.get(kind)
            if unit is None:
                continue
            length = unit * number
            if length <= 4:
                position = slot + 8
            else:
                position = self.u32(slot + 8)
                if position < 8 or position + length > self.size:
                    continue                    # the value lies outside: ignore the tag
            if tag not in entries:              # the first of a repeated tag counts
                entries[tag] = _Entry(kind, number, position, slot)
        next_slot = end
        next_offset = self.u32(end) if end + 4 <= self.size else 0
        return entries, next_slot, next_offset

    # --- values ---
    def numbers(self, entry: _Entry, limit: int = 16) -> list[int] | None:
        """Up to ``limit`` integers of an integer-typed entry."""
        formats = {_BYTE: "B", _SHORT: "H", _LONG: "I", _SBYTE: "b", _SSHORT: "h", _SLONG: "i", _UNDEFINED: "B"}
        code = formats.get(entry.kind)
        if code is None or entry.count < 1:
            return None
        count = min(entry.count, limit)
        raw = self.take(entry.position, count * _TYPE_SIZES[entry.kind])
        return list(struct.unpack(f"{self.endian}{count}{code}", raw))

    def number(self, entry: _Entry | None) -> int | None:
        if entry is None:
            return None
        values = self.numbers(entry, 1)
        return values[0] if values else None

    def rationals(self, entry: _Entry | None, limit: int = 4) -> list[Fraction | None] | None:
        if entry is None or entry.kind not in (_RATIONAL, _SRATIONAL) or entry.count < 1:
            return None
        count = min(entry.count, limit)
        code = "I" if entry.kind == _RATIONAL else "i"
        raw = self.take(entry.position, count * 8)
        flat = struct.unpack(f"{self.endian}{count * 2}{code}", raw)
        return [Fraction(flat[i], flat[i + 1]) if flat[i + 1] != 0 else None for i in range(0, len(flat), 2)]

    def rational(self, entry: _Entry | None) -> Fraction | None:
        values = self.rationals(entry, 1)
        return values[0] if values else None

    def text(self, entry: _Entry | None) -> str:
        if entry is None or entry.kind not in (_ASCII, _BYTE, _UNDEFINED) or entry.count < 1:
            return ""
        raw = self.take(entry.position, min(entry.count, MAX_VALUE_BYTES))
        raw = raw.split(b"\x00", 1)[0]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        text = "".join(ch if ch.isprintable() else " " for ch in text)
        return " ".join(text.split())[:MAX_TEXT]


def _positive(value: Fraction | None, maximum: float) -> Fraction | None:
    if value is None or value <= 0 or value > maximum:
        return None
    return value


def _choice(value: int | None, allowed: range) -> int | None:
    return value if value is not None and value in allowed else None


def parse_datetime(text: str) -> datetime | None:
    """An EXIF date ("2024:06:01 12:30:00") as a datetime, or None when it is not a real date."""
    if not isinstance(text, str) or len(text) < 19:
        return None
    try:
        return datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _date(text: str) -> str:
    return text[:19] if parse_datetime(text) is not None else ""


def _coordinate(tiff: _Tiff, value: _Entry | None, reference: _Entry | None, positive: str, negative: str,
                maximum: int) -> float | None:
    parts = tiff.rationals(value, 3)
    if not parts or len(parts) != 3 or any(part is None or part < 0 for part in parts):
        return None
    degrees, minutes, seconds = parts
    if minutes >= 60 or seconds >= 60:
        return None
    result = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
    if result > maximum:
        return None
    ref = tiff.text(reference).upper()[:1]
    if ref == negative:
        return -result
    if ref == positive:
        return result
    return None


def parse_tiff(data) -> Exif:
    """The EXIF of a TIFF structure (the content of an APP1 segment after ``Exif\\0\\0``, or a TIFF file).

    Raises ExifError when the header or the first directory cannot be read.
    """
    tiff = _Tiff(data)
    main, _slot, _next = tiff.directory(tiff.first_ifd)
    seen = {tiff.first_ifd}

    def sub(tag: int) -> dict[int, _Entry]:
        pointer = main.get(tag)
        if pointer is None or pointer.kind not in (_LONG, _SHORT) or pointer.count != 1:
            return {}
        try:
            offset = tiff.number(pointer)
            if offset is None or offset in seen:
                return {}
            seen.add(offset)
            return tiff.directory(offset)[0]
        except ExifError:
            return {}

    photo = sub(TAG_EXIF_IFD)
    gps = sub(TAG_GPS_IFD)

    def guarded(function, *args):
        try:
            return function(*args)
        except (ExifError, struct.error):
            return None

    orientation = guarded(tiff.number, main.get(TAG_ORIENTATION))
    iso = guarded(tiff.number, photo.get(TAG_ISO))
    focal_35 = guarded(tiff.number, photo.get(TAG_FOCAL_35MM))
    flash = guarded(tiff.number, photo.get(TAG_FLASH))
    altitude = guarded(tiff.rational, gps.get(GPS_ALT))
    if altitude is not None:
        below = guarded(tiff.number, gps.get(GPS_ALT_REF)) == 1
        altitude = -float(altitude) if below else float(altitude)
        if not -12000 <= altitude <= 100000:
            altitude = None
    width = guarded(tiff.number, photo.get(TAG_PIXEL_X))
    height = guarded(tiff.number, photo.get(TAG_PIXEL_Y))
    return Exif(
        orientation=orientation if orientation in range(1, 9) else 1,
        make=guarded(tiff.text, main.get(TAG_MAKE)) or "",
        model=guarded(tiff.text, main.get(TAG_MODEL)) or "",
        lens_make=guarded(tiff.text, photo.get(TAG_LENS_MAKE)) or "",
        lens_model=guarded(tiff.text, photo.get(TAG_LENS_MODEL)) or "",
        software=guarded(tiff.text, main.get(TAG_SOFTWARE)) or "",
        artist=guarded(tiff.text, main.get(TAG_ARTIST)) or "",
        copyright=guarded(tiff.text, main.get(TAG_COPYRIGHT)) or "",
        date_taken=_date(guarded(tiff.text, photo.get(TAG_DATETIME_ORIGINAL))
                         or guarded(tiff.text, photo.get(TAG_DATETIME_DIGITIZED)) or ""),
        date_modified=_date(guarded(tiff.text, main.get(TAG_DATETIME)) or ""),
        exposure_time=_positive(guarded(tiff.rational, photo.get(TAG_EXPOSURE_TIME)), 86400),
        f_number=_positive(guarded(tiff.rational, photo.get(TAG_F_NUMBER)), 1000),
        iso=iso if iso is not None and 1 <= iso <= 10_000_000 else None,
        focal_length=_positive(guarded(tiff.rational, photo.get(TAG_FOCAL_LENGTH)), 100000),
        focal_length_35mm=focal_35 if focal_35 is not None and 1 <= focal_35 <= 100000 else None,
        exposure_bias=(lambda v: v if v is not None and -100 <= v <= 100 else None)(
            guarded(tiff.rational, photo.get(TAG_EXPOSURE_BIAS))),
        exposure_program=_choice(guarded(tiff.number, photo.get(TAG_EXPOSURE_PROGRAM)), range(0, 9)),
        metering_mode=_choice(guarded(tiff.number, photo.get(TAG_METERING)), range(0, 256)),
        white_balance=_choice(guarded(tiff.number, photo.get(TAG_WHITE_BALANCE)), range(0, 2)),
        flash=flash if flash is not None and 0 <= flash <= 0xFFFF else None,
        latitude=guarded(_coordinate, tiff, gps.get(GPS_LAT), gps.get(GPS_LAT_REF), "N", "S", 90),
        longitude=guarded(_coordinate, tiff, gps.get(GPS_LON), gps.get(GPS_LON_REF), "E", "W", 180),
        altitude=altitude,
        pixel_width=width if width is not None and 0 < width <= 1_000_000 else None,
        pixel_height=height if height is not None and 0 < height <= 1_000_000 else None,
        x_resolution=_positive(guarded(tiff.rational, main.get(TAG_X_RESOLUTION)), 1_000_000),
        y_resolution=_positive(guarded(tiff.rational, main.get(TAG_Y_RESOLUTION)), 1_000_000),
        resolution_unit=_choice(guarded(tiff.number, main.get(TAG_RESOLUTION_UNIT)), range(1, 4)),
    )


# --- JPEG segments -----------------------------------------------------------------------------------------

def _segments(stream):
    """Yield (marker, start, payload length) for the header segments of a JPEG stream.

    ``start`` is the offset of the 0xFF of the marker; the payload begins
    four bytes later. Stops before the first scan (SOS), at EOI, at damage,
    or after ``MAX_JPEG_SEGMENTS`` segments.
    """
    if stream.read(2) != b"\xff\xd8":
        return
    for _ in range(MAX_JPEG_SEGMENTS):
        if stream.read(1) != b"\xff":
            return
        marker = stream.read(1)
        fill = 0
        while marker == b"\xff" and fill < 64:      # fill bytes before a marker
            marker = stream.read(1)
            fill += 1
        if len(marker) != 1 or marker == b"\xff":
            return
        code = marker[0]
        start = stream.tell() - 2
        if code in (0x00, 0xD9, 0xDA):
            return
        if 0xD0 <= code <= 0xD7 or code == 0x01:   # markers without a length
            continue
        raw = stream.read(2)
        if len(raw) != 2:
            return
        length = struct.unpack(">H", raw)[0]
        if length < 2:
            return
        yield code, start, length - 2
        stream.seek(start + 2 + length)


def jpeg_exif_block(source) -> bytes | None:
    """The TIFF structure of the first EXIF segment of a JPEG (bytes or a binary stream), or None."""
    stream = io.BytesIO(source) if isinstance(source, (bytes, bytearray, memoryview)) else source
    for code, start, length in _segments(stream):
        if code != 0xE1 or length < len(EXIF_HEADER) + 8:
            continue
        stream.seek(start + 4)
        head = stream.read(len(EXIF_HEADER))
        if head != EXIF_HEADER:
            continue
        block = stream.read(length - len(EXIF_HEADER))
        if len(block) != length - len(EXIF_HEADER):
            return None
        return block
    return None


def parse_jpeg(source) -> Exif | None:
    """The EXIF of a JPEG given as bytes or a binary stream; None when it has none or it is damaged."""
    try:
        block = jpeg_exif_block(source)
        return parse_tiff(block) if block is not None else None
    except (ExifError, struct.error, OSError, ValueError):
        return None


def read_file(path: str | os.PathLike) -> Exif | None:
    """The EXIF of a JPEG or TIFF file; None when there is none, it is damaged, or the file cannot be read."""
    try:
        with open(path, "rb") as handle:
            magic = handle.read(4)
            handle.seek(0)
            if magic[:2] == b"\xff\xd8":
                return parse_jpeg(handle)
            if magic in (b"II*\x00", b"MM\x00*"):
                if os.fstat(handle.fileno()).st_size < 8:
                    return None
                # The directories of a TIFF file may be anywhere in it: map it, read only what is asked for.
                with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                    return parse_tiff(mapped)
    except (ExifError, struct.error, OSError, ValueError):
        return None
    return None


# --- orientation -------------------------------------------------------------------------------------------

# How the stored picture has to be turned to be seen upright, as a matrix [[a, b], [c, d]] that maps
# a stored direction (x to the right, y down) to the direction on screen.
ORIENTATION_MATRICES = {
    1: (1, 0, 0, 1),        # as stored
    2: (-1, 0, 0, 1),       # mirrored left to right
    3: (-1, 0, 0, -1),      # turned 180 degrees
    4: (1, 0, 0, -1),       # mirrored top to bottom
    5: (0, 1, 1, 0),        # mirrored along the diagonal from top left
    6: (0, -1, 1, 0),       # turned 90 degrees clockwise
    7: (0, -1, -1, 0),      # mirrored along the other diagonal
    8: (0, 1, -1, 0),       # turned 90 degrees anticlockwise
}
_MATRIX_ORIENTATIONS = {matrix: value for value, matrix in ORIENTATION_MATRICES.items()}
ROTATE_CW, ROTATE_CCW, ROTATE_180 = 6, 8, 3
FLIP_HORIZONTAL, FLIP_VERTICAL = 2, 4


def compose_orientation(first: int, then: int) -> int:
    """The orientation that equals applying ``first`` and then ``then`` to a picture."""
    a = ORIENTATION_MATRICES.get(first, ORIENTATION_MATRICES[1])
    b = ORIENTATION_MATRICES.get(then, ORIENTATION_MATRICES[1])
    product = (b[0] * a[0] + b[1] * a[2], b[0] * a[1] + b[1] * a[3],
               b[2] * a[0] + b[3] * a[2], b[2] * a[1] + b[3] * a[3])
    return _MATRIX_ORIENTATIONS[product]


def swaps_sides(orientation: int) -> bool:
    """Whether width and height change places when the orientation is applied."""
    return orientation in (5, 6, 7, 8)


def minimal_block(orientation: int = 1) -> bytes:
    """A TIFF structure that says nothing but the orientation."""
    value = orientation if orientation in range(1, 9) else 1
    return (b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1)
            + struct.pack("<HHIHH", TAG_ORIENTATION, _SHORT, 1, value, 0) + struct.pack("<I", 0))


def with_orientation(block: bytes, orientation: int) -> bytes:
    """``block`` with the orientation tag set to ``orientation``; everything else is kept byte for byte.

    When the first directory has no orientation tag, a copy of the directory
    with the tag is appended and the header points to it. Raises ExifError
    for a block that cannot be read.
    """
    if orientation not in range(1, 9):
        raise ExifError("no such orientation")
    tiff = _Tiff(block)
    entries, next_slot, next_offset = tiff.directory(tiff.first_ifd)
    data = bytearray(block)
    entry = entries.get(TAG_ORIENTATION)
    if entry is not None and entry.kind == _SHORT and entry.count == 1:
        struct.pack_into(tiff.endian + "H", data, entry.position, orientation)
        return bytes(data)
    # Rebuild the first directory at the end: its entries as they are (their offsets stay valid),
    # plus the orientation, in tag order.
    count = tiff.u16(tiff.first_ifd)
    raw = [bytes(block[tiff.first_ifd + 2 + i * 12: tiff.first_ifd + 14 + i * 12]) for i in range(count)]
    raw = [item for item in raw if struct.unpack(tiff.endian + "H", item[:2])[0] != TAG_ORIENTATION]
    raw.append(struct.pack(tiff.endian + "HHIHH", TAG_ORIENTATION, _SHORT, 1, orientation, 0))
    raw.sort(key=lambda item: struct.unpack(tiff.endian + "H", item[:2])[0])
    if len(data) % 2:
        data.append(0)
    new_offset = len(data)
    data += struct.pack(tiff.endian + "H", len(raw)) + b"".join(raw) + struct.pack(tiff.endian + "I", next_offset)
    struct.pack_into(tiff.endian + "I", data, 4, new_offset)
    return bytes(data)


def prepared_for_save(block: bytes, width: int, height: int) -> bytes | None:
    """An EXIF block for a picture that was edited and is saved upright with ``width`` x ``height`` pixels.

    The orientation becomes 1, the recorded pixel size the new one, and the
    thumbnail directory is unlinked (its picture would show the old state);
    its bytes are cut off when they are the end of the block. Camera, lens,
    exposure, dates and GPS stay as they are. None when the block cannot be
    read or would not fit into one JPEG segment.
    """
    try:
        data = bytearray(with_orientation(block, 1))
        tiff = _Tiff(data)
        entries, next_slot, next_offset = tiff.directory(tiff.first_ifd)
        cut = None
        if next_offset:
            try:
                thumb, _slot, _next = tiff.directory(next_offset)
                offset, length = tiff.number(thumb.get(TAG_THUMB_OFFSET)), tiff.number(thumb.get(TAG_THUMB_LENGTH))
                if offset and length and offset > next_offset and offset + length >= len(data) - 1:
                    cut = offset
            except (ExifError, struct.error):
                pass
            struct.pack_into(tiff.endian + "I", data, next_slot, 0)
        pointer = entries.get(TAG_EXIF_IFD)
        if pointer is not None and pointer.kind == _LONG and pointer.count == 1:
            try:
                photo = tiff.directory(tiff.number(pointer))[0]
            except (ExifError, struct.error):
                photo = {}
            for tag, value in ((TAG_PIXEL_X, width), (TAG_PIXEL_Y, height)):
                entry = photo.get(tag)
                if entry is None or entry.count != 1:
                    continue
                if entry.kind == _LONG:
                    struct.pack_into(tiff.endian + "I", data, entry.position, value)
                elif entry.kind == _SHORT and value <= 0xFFFF:
                    struct.pack_into(tiff.endian + "H", data, entry.position, value)
        if cut is not None:
            del data[cut:]
        if len(data) + len(EXIF_HEADER) > MAX_APP1_PAYLOAD:
            return None
        return bytes(data)
    except (ExifError, struct.error):
        return None


def jpeg_with_exif(jpeg: bytes, block: bytes | None) -> bytes:
    """``jpeg`` with its EXIF segment replaced by ``block`` (removed when None).

    The segment goes where the standard wants it: after the JFIF segment
    when there is one, else right after the start of the picture. ValueError
    when ``jpeg`` is not a JPEG or the block is too large for a segment.
    """
    if jpeg[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    if block is not None and len(block) + len(EXIF_HEADER) > MAX_APP1_PAYLOAD:
        raise ValueError("the EXIF block does not fit into a JPEG segment")
    segment = b""
    if block is not None:
        payload = EXIF_HEADER + block
        segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    parts = [jpeg[:2]]
    position = 2
    inserted = False
    for index, (code, start, length) in enumerate(_segments(io.BytesIO(jpeg))):
        stop = start + 4 + length
        if stop > len(jpeg):
            break
        if index == 0 and code != 0xE0:
            parts.append(segment)
            inserted = True
        if code == 0xE1 and jpeg[start + 4:start + 4 + len(EXIF_HEADER)] == EXIF_HEADER:
            parts.append(jpeg[position:start])       # the old EXIF segment is left out
        else:
            parts.append(jpeg[position:stop])
        position = stop
        if index == 0 and not inserted:
            parts.append(segment)
            inserted = True
    if not inserted:
        parts.append(segment)
    parts.append(jpeg[position:])
    return b"".join(parts)


def jpeg_set_orientation(jpeg: bytes, orientation: int) -> bytes:
    """``jpeg`` with the EXIF orientation set, nothing re-encoded. Adds a small EXIF segment when
    there is none (or the present one cannot be read)."""
    block = jpeg_exif_block(jpeg)
    if block is not None:
        try:
            return jpeg_with_exif(jpeg, with_orientation(block, orientation))
        except (ExifError, struct.error, ValueError):
            pass
    return jpeg_with_exif(jpeg, minimal_block(orientation))


# --- text for people ---------------------------------------------------------------------------------------

def format_exposure(value: Fraction | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if number >= 0.3:
        return f"{number:.0f} s" if number == int(number) else f"{number:.1f} s"
    return f"1/{round(1 / value)} s"


def format_aperture(value: Fraction | None) -> str:
    if value is None:
        return ""
    number = float(value)
    return f"f/{number:.0f}" if number == int(number) else f"f/{number:.1f}"


def format_focal_length(value: Fraction | None, equivalent: int | None = None) -> str:
    if value is None:
        return f"{equivalent} mm (35 mm equivalent)" if equivalent else ""
    number = float(value)
    text = f"{number:.0f} mm" if abs(number - round(number)) < 0.05 else f"{number:.1f} mm"
    if equivalent and abs(equivalent - number) >= 1:
        text += f" ({equivalent} mm in 35 mm terms)"
    return text


def format_bias(value: Fraction | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if number == 0:
        return "0 EV"
    return f"{number:+.1f} EV" if number != int(number) else f"{number:+.0f} EV"


def format_flash(value: int | None) -> str:
    if value is None:
        return ""
    if value & 0x20:
        return "No flash function"
    fired = bool(value & 0x01)
    mode = (value >> 3) & 0x03
    words = ["Fired" if fired else "Did not fire"]
    if mode == 3:
        words.append("automatic")
    elif mode == 1 and fired:
        words.append("forced")
    elif mode == 2 and not fired:
        words.append("switched off")
    if value & 0x40 and fired:
        words.append("red-eye reduction")
    return ", ".join(words)


_PROGRAMS = {1: "Manual", 2: "Program", 3: "Aperture priority", 4: "Shutter priority", 5: "Creative",
             6: "Action", 7: "Portrait", 8: "Landscape"}
_METERING = {1: "Average", 2: "Centre-weighted", 3: "Spot", 4: "Multi-spot", 5: "Pattern", 6: "Partial"}


def format_program(value: int | None) -> str:
    return _PROGRAMS.get(value or 0, "")


def format_metering(value: int | None) -> str:
    return _METERING.get(value or 0, "")


def format_coordinates(latitude: float | None, longitude: float | None) -> str:
    """``48.85837° N, 2.29448° E``: decimal degrees with five places (about a metre)."""
    if latitude is None or longitude is None:
        return ""
    return (f"{abs(latitude):.5f}° {'N' if latitude >= 0 else 'S'}, "
            f"{abs(longitude):.5f}° {'E' if longitude >= 0 else 'W'}")


def format_altitude(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.0f} m above sea level" if value >= 0 else f"{-value:.0f} m below sea level"
