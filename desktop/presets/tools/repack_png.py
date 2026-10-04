#!/usr/bin/env python3
"""Re-deflate a PNG at maximum compression, keeping its row filters.

rsvg-convert writes PNGs at the default zlib level. Recompressing the image
data at level 9 saves 10-15 % on the presets' smooth gradients, without
decoding a pixel. Only the critical chunks (IHDR, PLTE, IDAT, IEND) and the
colour-space chunks (sRGB, gAMA, cHRM) are kept, so the output is reproducible.

Usage: repack_png.py IN.png OUT.png   (OUT may be IN)

Python 3 standard library only.
"""

from __future__ import annotations

import struct
import sys
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
KEEP = {b"IHDR", b"PLTE", b"sRGB", b"gAMA", b"cHRM"}


def chunks(data: bytes):
    if not data.startswith(SIGNATURE):
        raise ValueError("not a PNG file")
    pos = len(SIGNATURE)
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        yield kind, data[pos + 8:pos + 8 + length]
        pos += 12 + length


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def repack(data: bytes) -> bytes:
    head, idat = [], []
    for kind, body in chunks(data):
        if kind == b"IDAT":
            idat.append(body)
        elif kind in KEEP:
            head.append(chunk(kind, body))
    raw = zlib.decompress(b"".join(idat))
    compressor = zlib.compressobj(9, zlib.DEFLATED, 15, 9, zlib.Z_FILTERED)
    packed = compressor.compress(raw) + compressor.flush()
    return SIGNATURE + b"".join(head) + chunk(b"IDAT", packed) + chunk(b"IEND", b"")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write(__doc__)
        return 2
    with open(argv[1], "rb") as handle:
        data = handle.read()
    out = repack(data)
    if len(out) >= len(data):
        out = data
    with open(argv[2], "wb") as handle:
        handle.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
