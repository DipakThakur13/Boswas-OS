"""Names and places of the freedesktop.org thumbnail cache (Thumbnail Managing Standard 0.9; standard library only).

A thumbnail of a file is a PNG named after the MD5 of the file's URI,
in ``$XDG_CACHE_HOME/thumbnails/<size>/``, where ``<size>`` is ``normal``
(128 pixels at most on the long side), ``large`` (256), ``x-large`` (512)
or ``xx-large`` (1024). The PNG carries the URI and the modification time
of the original as text (``Thumb::URI``, ``Thumb::MTime``); a thumbnail
whose values do not match the file is out of date. A file that could not
be thumbnailed gets an empty record in ``fail/<program>/`` so nobody tries
again until the file changes.

Because every program that follows the standard uses the same names, the
thumbnails made here are the ones the file manager shows, and the other
way round. Reading and writing the PNGs is in ``thumbs`` (it needs Qt).
"""

from __future__ import annotations

import hashlib
import os
import struct
import zlib
from pathlib import Path
from urllib.parse import quote

SIZES = {"normal": 128, "large": 256, "x-large": 512, "xx-large": 1024}
FAIL_PROGRAM = "boswas-photos-1"
KEY_URI, KEY_MTIME, KEY_SIZE = "Thumb::URI", "Thumb::MTime", "Thumb::Size"
KEY_WIDTH, KEY_HEIGHT, KEY_MIME = "Thumb::Image::Width", "Thumb::Image::Height", "Thumb::Mimetype"
KEY_SOFTWARE = "Software"
MAX_TEXT_CHUNKS = 64
MAX_TEXT_BYTES = 16 * 1024
# The characters a path may keep in a URI (RFC 3986: unreserved, sub-delimiters, ":", "@"), as in GLib.
_URI_SAFE = "/!$&'()*+,;=:@"


def cache_root() -> Path:
    """``$XDG_CACHE_HOME/thumbnails``."""
    base = os.environ.get("XDG_CACHE_HOME") or ""
    root = Path(base) if base.startswith("/") else Path.home() / ".cache"
    return root / "thumbnails"


def file_uri(path: str | os.PathLike) -> str:
    """The canonical ``file://`` URI of an absolute path (the bytes of the name, percent-encoded)."""
    absolute = os.path.abspath(os.fspath(path))
    return "file://" + quote(os.fsencode(absolute), safe=_URI_SAFE)


def thumbnail_name(uri: str) -> str:
    """The file name of the thumbnail of ``uri``: the MD5 of the URI in hexadecimal, ``.png``."""
    return hashlib.md5(uri.encode("utf-8"), usedforsecurity=False).hexdigest() + ".png"


def size_name(pixels: int) -> str:
    """The smallest standard size that holds a thumbnail of ``pixels`` on the long side."""
    for name, limit in SIZES.items():
        if pixels <= limit:
            return name
    return "xx-large"


def thumbnail_path(path: str | os.PathLike, size: str = "large", root: Path | None = None) -> Path:
    if size not in SIZES:
        raise ValueError(f"no such thumbnail size: {size!r}")
    return (root or cache_root()) / size / thumbnail_name(file_uri(path))


def fail_path(path: str | os.PathLike, root: Path | None = None) -> Path:
    return (root or cache_root()) / "fail" / FAIL_PROGRAM / thumbnail_name(file_uri(path))


def is_inside_cache(path: str | os.PathLike, root: Path | None = None) -> bool:
    """Whether ``path`` lies in the thumbnail cache (thumbnails of thumbnails are never made)."""
    base = os.path.abspath(root or cache_root())
    target = os.path.abspath(os.fspath(path))
    return target == base or target.startswith(base + os.sep)


def matches(texts: dict[str, str], uri: str, mtime: float) -> bool:
    """Whether the text of a cached thumbnail says it was made from the file as it is now."""
    if texts.get(KEY_URI) != uri:
        return False
    try:
        return int(texts.get(KEY_MTIME, "")) == int(mtime)
    except ValueError:
        return False


def texts_for(uri: str, mtime: float, file_size: int, width: int, height: int, mime: str = "") -> dict[str, str]:
    """The text a thumbnail must and should carry."""
    texts = {KEY_URI: uri, KEY_MTIME: str(int(mtime)), KEY_SIZE: str(int(file_size)),
             KEY_SOFTWARE: "Boswas Photos"}
    if width > 0 and height > 0:
        texts[KEY_WIDTH] = str(width)
        texts[KEY_HEIGHT] = str(height)
    if mime:
        texts[KEY_MIME] = mime
    return texts


def ensure_directory(directory: Path) -> None:
    """Create a cache directory the way the standard asks: private to the user."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def _inflate(data: bytes) -> bytes | None:
    """zlib data unpacked, at most ``MAX_TEXT_BYTES`` of it; None when it is damaged or larger."""
    try:
        unpacker = zlib.decompressobj()
        text = unpacker.decompress(data, MAX_TEXT_BYTES + 1)
    except zlib.error:
        return None
    return text if len(text) <= MAX_TEXT_BYTES else None


def _text_entry(kind: bytes, data: bytes) -> tuple[str, str] | None:
    """Keyword and text of a ``tEXt``, ``zTXt`` or ``iTXt`` chunk; None when it is malformed."""
    keyword, separator, rest = data.partition(b"\x00")
    if not separator or not 0 < len(keyword) <= 79:
        return None
    if kind == b"tEXt":
        return keyword.decode("latin-1"), rest.decode("latin-1")
    if kind == b"zTXt":
        if rest[:1] != b"\x00":
            return None
        text = _inflate(rest[1:])
        return None if text is None else (keyword.decode("latin-1"), text.decode("latin-1"))
    if len(rest) < 2:
        return None
    compressed, method = rest[0], rest[1]
    _language, separator, rest = rest[2:].partition(b"\x00")
    _translated, second, rest = rest.partition(b"\x00")
    if not separator or not second or compressed > 1 or (compressed and method != 0):
        return None
    text = _inflate(rest) if compressed else rest
    if text is None:
        return None
    return keyword.decode("latin-1"), text.decode("utf-8", "replace")


def read_texts(path: str | os.PathLike) -> dict[str, str]:
    """The text entries of a PNG file that come before its picture data: keyword -> text.

    This is where a thumbnail says what it was made from. The file is read
    chunk by chunk with every length and checksum checked, compressed text
    is unpacked up to a limit, and a file that is no PNG, or is damaged
    before its picture data, gives an empty answer. (Qt's own reader cannot
    be used for this: it splits a keyword at its first colon, and every
    keyword of the standard has two.)
    """
    texts: dict[str, str] = {}
    try:
        with open(path, "rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return {}
            for _ in range(MAX_TEXT_CHUNKS):
                head = handle.read(8)
                if len(head) != 8:
                    return {}
                length, kind = struct.unpack(">I4s", head)
                if kind in (b"IDAT", b"IEND"):
                    return texts
                if kind not in (b"tEXt", b"zTXt", b"iTXt") or length > MAX_TEXT_BYTES:
                    if length > 0x7FFFFFFF:
                        return {}
                    handle.seek(length + 4, os.SEEK_CUR)
                    continue
                data = handle.read(length)
                check = handle.read(4)
                if len(data) != length or len(check) != 4 or struct.unpack(">I", check)[0] != zlib.crc32(kind + data):
                    return {}
                entry = _text_entry(kind, data)
                if entry is not None:
                    texts.setdefault(*entry)
    except OSError:
        return {}
    return {}
