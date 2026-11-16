#!/usr/bin/env python3
"""Write (and read) XCursor files; the build half of desktop/cursors/install.sh.

    xcursor.py plan MANIFEST
        Print one line per image to render: "PIXELS SOURCE.svg OUTPUT.png".
        install.sh runs rsvg-convert for every line.

    xcursor.py assemble MANIFEST PNG_DIR OUTPUT_DIR
        Read the rendered PNGs, write one XCursor file per shape into
        OUTPUT_DIR and a symbolic link for every alternate name.

    xcursor.py info FILE...
        List the images of XCursor files (nominal size, pixels, hotspot, delay).

MANIFEST is Boswas-Cursors/cursors.json, written by build_cursors.py.

The XCursor format (libXcursor, Xcursor(3)), all numbers little-endian u32:

    file header   "Xcur", header size 16, version 0x00010000, number of entries
    table         per image: type 0xfffd0002, nominal size, position in the file
    image chunk   header size 36, type 0xfffd0002, nominal size, version 1,
                  width, height, xhot, yhot, delay in ms, then width*height
                  pixels: ARGB with premultiplied alpha (bytes B, G, R, A)

The images are written sorted by nominal size, the frames of an animation in
order; a reader shows the images of the size nearest to the one it wants.
Colours are premultiplied the way xcursorgen does it (channel * alpha / 255,
rounded to the nearest integer), so the files are byte for byte what
xcursorgen writes for the same PNGs. There are no timestamps: the output
depends only on the input.

The PNG reader handles what rsvg-convert writes and a little more: 8 bit
grey, grey+alpha, RGB and RGBA, not interlaced, all five row filters.

Python 3 standard library only.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import zlib
from pathlib import Path

MAGIC = b"Xcur"
FILE_HEADER = 16
FILE_VERSION = 0x00010000
IMAGE_TYPE = 0xFFFD0002
IMAGE_HEADER = 36
IMAGE_VERSION = 1
MAX_DIMENSION = 0x7FFF           # libXcursor's XCURSOR_IMAGE_MAX_SIZE

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}      # colour type -> samples per pixel

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SVG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\.svg$")


class FormatError(ValueError):
    """A PNG, XCursor file or manifest that this tool cannot use."""


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------
def png_chunks(data: bytes):
    if not data.startswith(PNG_SIGNATURE):
        raise FormatError("not a PNG file")
    pos = len(PNG_SIGNATURE)
    while pos < len(data):
        if pos + 8 > len(data):
            raise FormatError("truncated PNG chunk")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length or pos + 12 + length > len(data):
            raise FormatError("truncated PNG chunk")
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        if crc != zlib.crc32(kind + body) & 0xFFFFFFFF:
            raise FormatError(f"bad checksum in PNG chunk {kind!r}")
        yield kind, body
        pos += 12 + length


def _unfilter(kind: int, row: bytearray, prior: bytes, bpp: int) -> None:
    """Undo one row filter in place; `prior` is the row above, already unfiltered."""
    size = len(row)
    if kind == 0:
        return
    if kind == 1:                                   # Sub
        for i in range(bpp, size):
            row[i] = (row[i] + row[i - bpp]) & 0xFF
    elif kind == 2:                                 # Up
        for i in range(size):
            row[i] = (row[i] + prior[i]) & 0xFF
    elif kind == 3:                                 # Average
        for i in range(size):
            left = row[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + ((left + prior[i]) >> 1)) & 0xFF
    elif kind == 4:                                 # Paeth
        for i in range(size):
            a = row[i - bpp] if i >= bpp else 0
            b = prior[i]
            c = prior[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            if pa <= pb and pa <= pc:
                predictor = a
            elif pb <= pc:
                predictor = b
            else:
                predictor = c
            row[i] = (row[i] + predictor) & 0xFF
    else:
        raise FormatError(f"unknown PNG row filter {kind}")


def read_png(data: bytes):
    """Decode a PNG into (width, height, RGBA bytes); alpha is not premultiplied."""
    header = None
    packed = []
    for kind, body in png_chunks(data):
        if kind == b"IHDR":
            if len(body) != 13:
                raise FormatError("bad PNG header")
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            packed.append(body)
        elif kind == b"IEND":
            break
    if header is None:
        raise FormatError("PNG without a header")
    width, height, depth, colour_type, compression, filtering, interlace = header
    if depth != 8 or colour_type not in PNG_CHANNELS or compression or filtering or interlace:
        raise FormatError("unsupported PNG: need 8 bit grey, RGB or RGBA without interlacing")
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        raise FormatError("bad PNG size")
    bpp = PNG_CHANNELS[colour_type]
    stride = width * bpp
    try:
        raw = zlib.decompress(b"".join(packed))
    except zlib.error as error:
        raise FormatError(f"bad PNG data: {error}") from None
    if len(raw) != (stride + 1) * height:
        raise FormatError("PNG data does not match its size")

    rows = []
    prior = bytes(stride)
    empty = bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        body = raw[start + 1:start + 1 + stride]
        if kind == 0 or (body == empty and prior == empty and kind <= 4):
            line = body                             # nothing to undo (most of a cursor is empty)
        else:
            row = bytearray(body)
            _unfilter(kind, row, prior, bpp)
            line = bytes(row)
        rows.append(line)
        prior = line
    pixels = b"".join(rows)

    if colour_type == 6:
        rgba = pixels
    else:
        out = bytearray(width * height * 4)
        if colour_type == 2:
            out[0::4], out[1::4], out[2::4] = pixels[0::3], pixels[1::3], pixels[2::3]
            out[3::4] = b"\xff" * (width * height)
        elif colour_type == 0:
            out[0::4] = out[1::4] = out[2::4] = pixels
            out[3::4] = b"\xff" * (width * height)
        else:
            out[0::4] = out[1::4] = out[2::4] = pixels[0::2]
            out[3::4] = pixels[1::2]
        rgba = bytes(out)
    return width, height, rgba


_PREMULTIPLY: dict = {}


def _table(alpha: int) -> bytes:
    table = _PREMULTIPLY.get(alpha)
    if table is None:
        table = bytes((t + (t >> 8)) >> 8 for t in (value * alpha + 0x80 for value in range(256)))
        _PREMULTIPLY[alpha] = table
    return table


def premultiply(rgba: bytes) -> bytes:
    """RGBA bytes to the XCursor pixel format: premultiplied B, G, R, A."""
    out = bytearray(len(rgba))
    out[3::4] = rgba[3::4]
    alphas = set(rgba[3::4])
    if alphas <= {0}:
        return bytes(out)
    if alphas <= {255}:
        out[0::4], out[1::4], out[2::4] = rgba[2::4], rgba[1::4], rgba[0::4]
        return bytes(out)
    for i in range(0, len(rgba), 4):
        alpha = rgba[i + 3]
        if alpha == 255:
            out[i], out[i + 1], out[i + 2] = rgba[i + 2], rgba[i + 1], rgba[i]
        elif alpha:
            table = _table(alpha)
            out[i], out[i + 1], out[i + 2] = table[rgba[i + 2]], table[rgba[i + 1]], table[rgba[i]]
    return bytes(out)


# ---------------------------------------------------------------------------
# XCursor
# ---------------------------------------------------------------------------
class Image:
    """One image of a cursor: `pixels` are premultiplied B, G, R, A bytes, row by row."""

    __slots__ = ("size", "width", "height", "xhot", "yhot", "delay", "pixels")

    def __init__(self, size: int, width: int, height: int, xhot: int, yhot: int, delay: int, pixels: bytes):
        self.size, self.width, self.height = size, width, height
        self.xhot, self.yhot, self.delay, self.pixels = xhot, yhot, delay, pixels

    def check(self) -> None:
        if not (0 < self.width <= MAX_DIMENSION and 0 < self.height <= MAX_DIMENSION):
            raise FormatError("bad image size")
        if not (0 <= self.xhot < self.width and 0 <= self.yhot < self.height):
            raise FormatError("hotspot outside the image")
        if not (0 < self.size <= 0xFFFF and 0 <= self.delay <= 0xFFFF):
            raise FormatError("bad nominal size or delay")
        if len(self.pixels) != 4 * self.width * self.height:
            raise FormatError("pixel data does not match the image size")

    def alpha(self, x: int, y: int) -> int:
        return self.pixels[4 * (y * self.width + x) + 3]

    def colour(self, x: int, y: int):
        """(red, green, blue) of a pixel without the premultiplication; black where it is transparent."""
        i = 4 * (y * self.width + x)
        blue, green, red, alpha = self.pixels[i:i + 4]
        if not alpha:
            return 0, 0, 0
        return tuple(min(255, (channel * 255 + alpha // 2) // alpha) for channel in (red, green, blue))


def encode(images) -> bytes:
    """An XCursor file with `images` in the given order."""
    images = list(images)
    if not images:
        raise FormatError("a cursor needs at least one image")
    for image in images:
        image.check()
    position = FILE_HEADER + 12 * len(images)
    out = [struct.pack("<4sIII", MAGIC, FILE_HEADER, FILE_VERSION, len(images))]
    for image in images:
        out.append(struct.pack("<III", IMAGE_TYPE, image.size, position))
        position += IMAGE_HEADER + len(image.pixels)
    for image in images:
        out.append(struct.pack("<9I", IMAGE_HEADER, IMAGE_TYPE, image.size, IMAGE_VERSION,
                               image.width, image.height, image.xhot, image.yhot, image.delay))
        out.append(image.pixels)
    return b"".join(out)


def decode(data: bytes) -> list:
    """The images of an XCursor file, in the order of its table. Strict: anything odd is an error."""
    if len(data) < FILE_HEADER or data[:4] != MAGIC:
        raise FormatError("not an XCursor file")
    header, _version, count = struct.unpack("<III", data[4:16])
    if header < FILE_HEADER or header + 12 * count > len(data):
        raise FormatError("bad XCursor header")
    images = []
    for index in range(count):
        kind, subtype, position = struct.unpack("<III", data[header + 12 * index:header + 12 * index + 12])
        if kind != IMAGE_TYPE:
            continue                                # comments: not written here, skipped like libXcursor does
        if position + IMAGE_HEADER > len(data):
            raise FormatError("image chunk outside the file")
        size_of_header, chunk_type, size, version, width, height, xhot, yhot, delay = struct.unpack(
            "<9I", data[position:position + IMAGE_HEADER])
        if size_of_header != IMAGE_HEADER or chunk_type != IMAGE_TYPE or size != subtype or version != IMAGE_VERSION:
            raise FormatError("bad image chunk header")
        if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
            raise FormatError("bad image size")
        end = position + IMAGE_HEADER + 4 * width * height
        if end > len(data):
            raise FormatError("truncated image")
        image = Image(size, width, height, xhot, yhot, delay, data[position + IMAGE_HEADER:end])
        image.check()
        images.append(image)
    if not images:
        raise FormatError("XCursor file without images")
    return images


# ---------------------------------------------------------------------------
# The theme
# ---------------------------------------------------------------------------
def load_manifest(path: Path) -> dict:
    """Read and validate cursors.json: names are plain file names, nothing is named twice."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        canvas, nominal, sizes, cursors = (manifest[key] for key in ("canvas", "nominal", "sizes", "cursors"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise FormatError(f"{path}: cannot read the manifest: {error}") from None
    if not (isinstance(canvas, int) and isinstance(nominal, int) and 0 < nominal <= canvas <= 512):
        raise FormatError(f"{path}: bad canvas or nominal size")
    if not (isinstance(sizes, list) and sizes and all(isinstance(s, int) and 8 <= s <= 512 for s in sizes)
            and sizes == sorted(set(sizes))):
        raise FormatError(f"{path}: bad list of sizes")
    if not (isinstance(cursors, list) and cursors):
        raise FormatError(f"{path}: no cursors")
    seen = set()
    for cursor in cursors:
        try:
            name, hotspot, frames, aliases = (cursor[key] for key in ("name", "hotspot", "frames", "aliases"))
        except (KeyError, TypeError) as error:
            raise FormatError(f"{path}: incomplete cursor entry: {error}") from None
        for entry in [name, *aliases]:
            if not (isinstance(entry, str) and NAME.match(entry)):
                raise FormatError(f"{path}: bad cursor name {entry!r}")
            if entry in seen:
                raise FormatError(f"{path}: {entry} is named twice")
            seen.add(entry)
        if not (isinstance(hotspot, list) and len(hotspot) == 2
                and all(isinstance(v, (int, float)) and 0 <= v < canvas for v in hotspot)):
            raise FormatError(f"{path}: bad hotspot of {name}")
        if not (isinstance(frames, list) and frames):
            raise FormatError(f"{path}: {name} has no frames")
        for frame in frames:
            if not (isinstance(frame, dict) and isinstance(frame.get("svg"), str) and SVG_NAME.match(frame["svg"])
                    and isinstance(frame.get("delay"), int) and 0 <= frame["delay"] <= 10000):
                raise FormatError(f"{path}: bad frame of {name}")
        if len(frames) > 1 and any(frame["delay"] == 0 for frame in frames):
            raise FormatError(f"{path}: {name} is animated, its frames need a delay")
    return manifest


def pixels_for(manifest: dict, size: int) -> int:
    """Width and height in pixels of the image of nominal size `size`."""
    return (manifest["canvas"] * size + manifest["nominal"] // 2) // manifest["nominal"]


def hotspot_for(manifest: dict, hotspot, pixels: int):
    """The pixel that contains the hotspot (given in grid units)."""
    return tuple(min(pixels - 1, int(value * pixels / manifest["canvas"])) for value in hotspot)


def png_name(svg: str, size: int) -> str:
    return f"{svg[:-4]}.{size}.png"


def plan(manifest: dict):
    """(pixels, source SVG, PNG name) for every image install.sh has to render."""
    for cursor in manifest["cursors"]:
        for frame in cursor["frames"]:
            for size in manifest["sizes"]:
                yield pixels_for(manifest, size), frame["svg"], png_name(frame["svg"], size)


def build_cursor(manifest: dict, cursor: dict, png_dir: Path) -> bytes:
    images = []
    for size in manifest["sizes"]:
        pixels = pixels_for(manifest, size)
        xhot, yhot = hotspot_for(manifest, cursor["hotspot"], pixels)
        for frame in cursor["frames"]:
            path = png_dir / png_name(frame["svg"], size)
            try:
                width, height, rgba = read_png(path.read_bytes())
            except (OSError, FormatError) as error:
                raise FormatError(f"{path}: {error}") from None
            if (width, height) != (pixels, pixels):
                raise FormatError(f"{path}: is {width}x{height}, expected {pixels}x{pixels}")
            images.append(Image(size, width, height, xhot, yhot, frame["delay"], premultiply(rgba)))
    return encode(images)


def assemble(manifest: dict, png_dir: Path, output: Path) -> int:
    """Write the cursor files and the links for the alternate names; returns the number of names."""
    output.mkdir(parents=True, exist_ok=True)
    names = 0
    for cursor in manifest["cursors"]:
        data = build_cursor(manifest, cursor, png_dir)
        target = output / cursor["name"]
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_bytes(data)
        os.chmod(target, 0o644)
        names += 1
        for alias in sorted(cursor["aliases"]):
            link = output / alias
            if link.is_symlink() or link.exists():
                link.unlink()
            os.symlink(cursor["name"], link)
            names += 1
    return names


def info(paths) -> int:
    status = 0
    for path in paths:
        try:
            images = decode(Path(path).read_bytes())
        except (OSError, FormatError) as error:
            print(f"{path}: {error}", file=sys.stderr)
            status = 1
            continue
        for image in images:
            print(f"{path}: size {image.size}, {image.width}x{image.height}, "
                  f"hotspot {image.xhot},{image.yhot}, delay {image.delay} ms")
    return status


def main(argv: list) -> int:
    try:
        if len(argv) == 2 and argv[0] == "plan":
            for pixels, svg, png in plan(load_manifest(Path(argv[1]))):
                print(pixels, svg, png)
            return 0
        if len(argv) == 4 and argv[0] == "assemble":
            count = assemble(load_manifest(Path(argv[1])), Path(argv[2]), Path(argv[3]))
            print(f"xcursor.py: {count} cursor names in {argv[3]}", file=sys.stderr)
            return 0
        if len(argv) >= 2 and argv[0] == "info":
            return info(argv[1:])
    except (FormatError, OSError) as error:
        print(f"xcursor.py: {error}", file=sys.stderr)
        return 1
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
