"""Names, natural ordering and human-readable numbers (no Qt)."""

from __future__ import annotations

import locale
import re
from typing import Callable

_DIGITS = re.compile(r"([0-9]+)")
_COMPOUND_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz", ".tar.lzma", ".tar.z")
NAME_MAX = 255                      # bytes, on every file system Linux users meet


def _collate(text: str) -> str:
    if not text:
        return ""
    folded = text.casefold()
    try:
        return locale.strxfrm(folded)
    except (ValueError, OSError, MemoryError):      # an embedded NUL, a code point the C library refuses
        return folded


def natural_key(name: str) -> tuple:
    """A sort key that orders names as people read them: ``file2`` before ``file10``.

    Runs of ASCII digits compare by value (any length), text compares in the
    order of the current locale (``LC_COLLATE``) without regard to case, and
    names that are otherwise equal fall back to their exact spelling.

    The key is one string and the name, so that sorting a hundred thousand
    of them is quick: text is written as the locale's collation string, a
    number as NUL (which sorts before everything a name can hold), its
    count of digits, the digits, and its count of leading zeros.
    """
    parts = _DIGITS.split(name)             # text, digits, text, ...: the same parity for every name
    if len(parts) == 1:
        return (_collate(name), name)
    pieces = []
    for index, part in enumerate(parts):
        if index % 2:
            value = part.lstrip("0")
            pieces.append(f"\0{len(value):05d}{value}{chr(48 + min(len(part) - len(value), 1000))}")
        else:
            pieces.append(_collate(part))
    return ("".join(pieces), name)


def split_extension(name: str) -> tuple[str, str]:
    """``("report", ".pdf")``, ``("backup", ".tar.gz")``, ``(".bashrc", "")``, ``("README", "")``."""
    lower = name.lower()
    for extension in _COMPOUND_EXTENSIONS:
        if lower.endswith(extension) and len(name) > len(extension):
            return name[:-len(extension)], name[-len(extension):]
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem or not extension or "/" in extension:
        return name, ""
    return stem, "." + extension


def display_name(name: str) -> str:
    """``name`` made safe to show: undecodable bytes and control characters become visible marks."""
    if name.isprintable():
        return name
    out = []
    for character in name:
        code = ord(character)
        if 0xDC80 <= code <= 0xDCFF:            # a byte that was not UTF-8 (surrogateescape)
            out.append("�")
        elif code < 0x20:
            out.append(chr(0x2400 + code))      # the "control pictures" block
        elif code == 0x7F:
            out.append("␡")
        else:
            out.append(character)
    return "".join(out)


def name_bytes(name: str) -> int:
    return len(name.encode("utf-8", "surrogateescape"))


def validate_name(name: str) -> str | None:
    """None when ``name`` can be the name of a file, else why not, in plain words."""
    if not name:
        return "A name cannot be empty."
    if name in (".", ".."):
        return f"“{name}” is not a name a file can have."
    if "/" in name:
        return "A name cannot contain a slash (/)."
    if "\0" in name:
        return "A name cannot contain a null character."
    if name_bytes(name) > NAME_MAX:
        return "This name is too long."
    return None


def truncate_name(name: str, limit: int) -> str:
    """``name`` cut to at most ``limit`` bytes of UTF-8 without splitting a character."""
    if name_bytes(name) <= limit:
        return name
    kept = []
    total = 0
    for character in name:
        total += len(character.encode("utf-8", "surrogateescape"))
        if total > limit:
            break
        kept.append(character)
    return "".join(kept)


def unique_name(name: str, exists: Callable[[str], bool], *, copy: bool = False) -> str:
    """``name`` if it is free, else ``stem (2).ext``, ``stem (3).ext``, ...

    With ``copy`` the first try is ``stem - Copy.ext`` (a paste into the
    folder the file came from). Names stay within the 255 byte limit.
    """
    if not copy and not exists(name):
        return name
    stem, extension = split_extension(name)
    number = 1 if copy else 2
    while True:
        if copy:
            suffix = " - Copy" if number == 1 else f" - Copy ({number})"
        else:
            suffix = f" ({number})"
        candidate = truncate_name(stem, NAME_MAX - name_bytes(suffix) - name_bytes(extension)) + suffix + extension
        if not exists(candidate):
            return candidate
        number += 1


_UNITS = ("KB", "MB", "GB", "TB", "PB", "EB")


def format_size(size: int) -> str:
    """``0 bytes``, ``1 byte``, ``1.46 KB``, ``12.5 MB``, ``256 GB`` (1 KB = 1024 bytes, as in Windows)."""
    size = max(0, int(size))
    if size < 1024:
        return f"{size} byte" if size == 1 else f"{size} bytes"
    value = float(size)
    unit = ""
    for unit in _UNITS:
        value /= 1024
        if value < 1024:
            break
    if value >= 100:
        return f"{value:.0f} {unit}"
    if value >= 10:
        return f"{value:.1f} {unit}"
    return f"{value:.2f} {unit}"


def plural(count: int, one: str, many: str | None = None) -> str:
    """``1 item``, ``2 items``, ``1,204 items``."""
    word = one if count == 1 else (many if many is not None else one + "s")
    return f"{count:,} {word}"
