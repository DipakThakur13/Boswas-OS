"""What the Photos tests share: pictures made on the spot, EXIF blocks, hostile files."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

# EXIF types
BYTE, ASCII, SHORT, LONG, RATIONAL, UNDEFINED, SLONG, SRATIONAL = 1, 2, 3, 4, 5, 7, 9, 10
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _value(endian: str, kind: int, value) -> tuple[bytes, int]:
    """The bytes of a tag value and its count."""
    if kind == ASCII:
        data = value if isinstance(value, bytes) else value.encode("utf-8") + b"\x00"
        return data, len(data)
    if kind in (BYTE, UNDEFINED):
        return bytes(value), len(value)
    values = value if isinstance(value, (list, tuple)) and not (
        kind in (RATIONAL, SRATIONAL) and len(value) == 2 and isinstance(value[0], int)) else [value]
    if kind == SHORT:
        return struct.pack(f"{endian}{len(values)}H", *values), len(values)
    if kind == LONG:
        return struct.pack(f"{endian}{len(values)}I", *values), len(values)
    if kind == SLONG:
        return struct.pack(f"{endian}{len(values)}i", *values), len(values)
    code = "I" if kind == RATIONAL else "i"
    return b"".join(struct.pack(f"{endian}2{code}", *pair) for pair in values), len(values)


def build_exif(main: list, photo: list | None = None, gps: list | None = None, *, endian: str = "<",
               thumbnail: bytes | None = None) -> bytes:
    """A TIFF structure: IFD0 from ``main``, an Exif directory and a GPS directory when given, and an
    IFD1 with ``thumbnail`` at the very end. Entries are (tag, type, value)."""
    order = b"II" if endian == "<" else b"MM"
    main = list(main)
    directories = [("main", main)]
    if photo is not None:
        main.append((0x8769, LONG, "photo"))
        directories.append(("photo", photo))
    if gps is not None:
        main.append((0x8825, LONG, "gps"))
        directories.append(("gps", gps))
    thumb_entries = None
    if thumbnail is not None:
        thumb_entries = [(0x0201, LONG, "thumb"), (0x0202, LONG, len(thumbnail))]
        directories.append(("thumb-ifd", thumb_entries))
    offsets = {}
    position = 8
    for name, entries in directories:
        offsets[name] = position
        position += 2 + 12 * len(entries) + 4
    data = bytearray()
    out = bytearray(order + struct.pack(endian + "H", 42) + struct.pack(endian + "I", 8))
    pending_thumb: list[int] = []
    for name, entries in directories:
        out += struct.pack(endian + "H", len(entries))
        for tag, kind, value in sorted(entries, key=lambda entry: entry[0]):
            if value in ("photo", "gps"):
                raw, count = struct.pack(endian + "I", offsets[value]), 1
            elif value == "thumb":
                raw, count = b"\x00\x00\x00\x00", 1
                pending_thumb.append(len(out) + 8)
            else:
                raw, count = _value(endian, kind, value)
            out += struct.pack(endian + "HHI", tag, kind, count)
            if len(raw) <= 4:
                out += raw.ljust(4, b"\x00")
            else:
                out += struct.pack(endian + "I", position + len(data))
                data += raw + (b"\x00" if len(raw) % 2 else b"")
        following = offsets["thumb-ifd"] if name == "main" and thumbnail is not None else 0
        out += struct.pack(endian + "I", following)
    out += data
    if thumbnail is not None:
        for slot in pending_thumb:
            struct.pack_into(endian + "I", out, slot, len(out))
        out += thumbnail
    return bytes(out)


def camera_exif(orientation: int = 1, **extra) -> bytes:
    """An EXIF block as a camera writes it."""
    main = [(0x010F, ASCII, "Boswas"), (0x0110, ASCII, "Boswas Prism One"), (0x0112, SHORT, orientation),
            (0x011A, RATIONAL, (300, 1)), (0x011B, RATIONAL, (300, 1)), (0x0128, SHORT, 2),
            (0x0131, ASCII, "Firmware 1.2"), (0x0132, ASCII, "2026:08:14 18:23:05")]
    photo = [(0x829A, RATIONAL, (1, 250)), (0x829D, RATIONAL, (28, 10)), (0x8827, SHORT, 200),
             (0x9003, ASCII, "2026:08:14 18:23:05"), (0x9204, SRATIONAL, (-1, 3)), (0x9209, SHORT, 0x19),
             (0x920A, RATIONAL, (50, 1)), (0xA002, LONG, 4000), (0xA003, LONG, 3000),
             (0xA405, SHORT, 75), (0xA433, ASCII, "Boswas"), (0xA434, ASCII, "24-70mm F2.8")]
    gps = [(1, ASCII, "N"), (2, RATIONAL, [(48, 1), (51, 1), (3012, 100)]), (3, ASCII, "E"),
           (4, RATIONAL, [(2, 1), (17, 1), (4012, 100)]), (5, BYTE, [0]), (6, RATIONAL, (35, 1))]
    return build_exif(main, photo, gps, **extra)


def jpeg_with_block(jpeg: bytes, block: bytes) -> bytes:
    """Put an EXIF block into a JPEG, written here by hand (not with the code under test)."""
    payload = b"Exif\x00\x00" + block
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return jpeg[:2] + segment + jpeg[2:]


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_bomb(width: int = 60000, height: int = 60000) -> bytes:
    """A PNG whose header claims a huge picture and whose data is a few bytes."""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(b"\x00" * 64))
            + png_chunk(b"IEND", b""))


def gif(frames: list[tuple[int, int, int]], width: int = 8, height: int = 6, delay: int = 5) -> bytes:
    """An animated GIF with one frame per colour (r, g, b), written without a compressor: the pixel
    data uses only literal codes, with a clear code before the table would grow."""
    palette = b"".join(bytes(colour) for colour in frames)
    size = 1
    while (2 << size) < max(2, len(frames)):
        size += 1
    palette = palette.ljust(3 * (2 << size), b"\x00")
    out = bytearray(b"GIF89a" + struct.pack("<HHBBB", width, height, 0x80 | size, 0, 0) + palette)
    out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    minimum = max(2, size + 1)
    clear, end = 1 << minimum, (1 << minimum) + 1
    for index in range(len(frames)):
        out += b"\x21\xf9\x04\x04" + struct.pack("<H", delay) + b"\x00\x00"
        out += b"\x2c" + struct.pack("<HHHHB", 0, 0, width, height, 0) + bytes([minimum])
        bits = bit_count = 0
        data = bytearray()

        def put(code: int) -> None:
            nonlocal bits, bit_count
            bits |= code << bit_count
            bit_count += minimum + 1
            while bit_count >= 8:
                data.append(bits & 0xFF)
                bits >>= 8
                bit_count -= 8
        count = 0
        put(clear)
        for _pixel in range(width * height):
            put(index)
            count += 1
            if count >= (1 << minimum) - 2:      # before the code size would grow: start over
                put(clear)
                count = 0
        put(end)
        if bit_count:
            data.append(bits & 0xFF)
        for start in range(0, len(data), 255):
            block = data[start:start + 255]
            out += bytes([len(block)]) + block
        out += b"\x00"
    out += b"\x3b"
    return bytes(out)


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="0 0 40 20">'
       '<rect width="20" height="20" fill="#ff0000"/><rect x="20" width="20" height="20" fill="#0000ff"/></svg>')
