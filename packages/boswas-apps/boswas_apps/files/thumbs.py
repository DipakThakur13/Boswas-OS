"""The freedesktop.org thumbnail cache: where a thumbnail lives and whether it is still valid (no Qt).

Thumbnails are PNG files in ``$XDG_CACHE_HOME/thumbnails/<size>/<md5 of
the file's URL>.png`` with the text chunks ``Thumb::URI`` and
``Thumb::MTime``; a thumbnail is valid while both match the file. A file
that could not be thumbnailed gets an empty marker in
``fail/boswas-files``, so it is not tried again until it changes. The
images themselves are made in ``thumbnailer.py`` (Qt).
"""

from __future__ import annotations

import hashlib
import os
import struct
import zlib

from .clipformats import path_to_uri

SIZES = (("normal", 128), ("large", 256), ("x-large", 512), ("xx-large", 1024))
FAIL_DIR = os.path.join("fail", "boswas-files")
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".webp", ".svg", ".svgz", ".tif",
                            ".tiff", ".ico", ".ppm", ".pgm", ".pbm", ".xpm", ".xbm", ".tga"})
MAX_SOURCE_BYTES = 256 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_TEXT_CHUNK = 64 * 1024


def cache_root() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or ""
    if not cache.startswith("/"):
        cache = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "thumbnails")


def size_for(pixels: int) -> tuple[str, int]:
    """The smallest standard size (name, pixels) that covers ``pixels``."""
    for name, side in SIZES:
        if pixels <= side:
            return name, side
    return SIZES[-1]


def thumb_name(path: str) -> str:
    return hashlib.md5(path_to_uri(path).encode("ascii"), usedforsecurity=False).hexdigest() + ".png"


def thumb_path(path: str, size_name: str, root: str | None = None) -> str:
    return os.path.join(root or cache_root(), size_name, thumb_name(path))


def fail_path(path: str, root: str | None = None) -> str:
    return os.path.join(root or cache_root(), FAIL_DIR, thumb_name(path))


def wants_thumbnail(path: str, size: int, root: str | None = None) -> bool:
    """Whether a file is an image this program thumbnails (by name and size; nothing in the cache itself)."""
    if os.path.splitext(path)[1].lower() not in IMAGE_SUFFIXES or size > MAX_SOURCE_BYTES or size <= 0:
        return False
    base = root or cache_root()
    return not os.path.abspath(path).startswith(base.rstrip("/") + "/")


def _inflate(data: bytes) -> bytes | None:
    try:
        return zlib.decompressobj().decompress(data, _MAX_TEXT_CHUNK)
    except zlib.error:
        return None


def _text_chunk(kind: bytes, data: bytes) -> tuple[str, str] | None:
    """(keyword, text) of a tEXt, zTXt or iTXt chunk; None when it is malformed."""
    key, separator, rest = data.partition(b"\0")
    if not separator or not key:
        return None
    if kind == b"tEXt":
        return key.decode("latin-1"), rest.decode("latin-1")
    if kind == b"zTXt":
        if rest[:1] != b"\0":
            return None
        value = _inflate(rest[1:])
        return (key.decode("latin-1"), value.decode("latin-1")) if value is not None else None
    if len(rest) < 2:
        return None
    compressed, method = rest[0], rest[1]
    parts = rest[2:].split(b"\0", 2)                # language tag, translated keyword, text
    if len(parts) < 3:
        return None
    value = parts[2]
    if compressed:
        value = _inflate(value) if method == 0 else None
        if value is None:
            return None
    return key.decode("latin-1"), value.decode("utf-8", "replace")


def read_png_text(path: str) -> dict[str, str]:
    """The text chunks (tEXt, zTXt, iTXt) before the image data of a PNG file. Empty when it is not a readable PNG."""
    result: dict[str, str] = {}
    try:
        with open(path, "rb") as handle:
            if handle.read(8) != _PNG_MAGIC:
                return {}
            for _chunk in range(64):
                header = handle.read(8)
                if len(header) < 8:
                    break
                length, kind = struct.unpack(">I4s", header)
                if kind in (b"IDAT", b"IEND"):
                    break
                if kind not in (b"tEXt", b"zTXt", b"iTXt") or length > _MAX_TEXT_CHUNK:
                    handle.seek(length + 4, os.SEEK_CUR)
                    continue
                data = handle.read(length)
                checksum = handle.read(4)
                if len(data) < length or len(checksum) < 4 or struct.unpack(">I", checksum)[0] != (
                        zlib.crc32(kind + data) & 0xFFFFFFFF):
                    break
                parsed = _text_chunk(kind, data)
                if parsed is not None:
                    result.setdefault(*parsed)
    except OSError:
        return {}
    return result


def is_valid(thumbnail: str, path: str, mtime: float) -> bool:
    """Whether the PNG ``thumbnail`` was made from ``path`` as it is now (same URL, same modification time)."""
    text = read_png_text(thumbnail)
    if text.get("Thumb::URI") != path_to_uri(path):
        return False
    try:
        return int(text.get("Thumb::MTime", "")) == int(mtime)
    except ValueError:
        return False


def find_valid(path: str, mtime: float, pixels: int, root: str | None = None) -> str | None:
    """A cached thumbnail of at least ``pixels`` that is valid for the file, or None."""
    wanted = size_for(pixels)[1]
    for name, side in SIZES:
        if side < wanted:
            continue
        candidate = thumb_path(path, name, root)
        if os.path.isfile(candidate) and is_valid(candidate, path, mtime):
            return candidate
    return None


def has_failed(path: str, mtime: float, root: str | None = None) -> bool:
    """Whether an earlier attempt to thumbnail this version of the file failed."""
    marker = fail_path(path, root)
    return os.path.isfile(marker) and is_valid(marker, path, mtime)


def ensure_dirs(size_name: str, root: str | None = None) -> str:
    """Create the cache directory of a size (mode 0700, as the specification asks) and return it."""
    base = root or cache_root()
    directory = os.path.join(base, size_name)
    current = base
    os.makedirs(os.path.dirname(base), exist_ok=True)
    for part in [""] + size_name.split(os.sep):
        current = os.path.join(current, part) if part else current
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass
    return directory
