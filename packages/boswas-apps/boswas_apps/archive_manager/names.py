"""Names of archive entries: decoding them, showing them, and turning them into safe relative paths.

An entry name is untrusted text. Three things are done with it here:

* ``zip_name`` decodes the name of a ZIP entry (UTF-8 flag, the Info-ZIP
  Unicode path field, UTF-8 without the flag, else code page 437).
* ``display`` makes any name safe to put on the screen (no control
  characters, no lone surrogates, no direction overrides).
* ``safe_parts`` turns a name into the path components that may be created
  below a destination folder, or raises ``UnsafeName`` with the reason.
  Nothing it returns is absolute, empty, ``.`` or ``..``.

No Qt, no file system access.
"""

from __future__ import annotations

import os
import struct
import unicodedata
import zlib

MAX_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 4000              # below PATH_MAX, leaving room for the destination itself
_BIDI = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏؜")
_REPLACEMENT = "�"


class UnsafeName(ValueError):
    """An entry name that is not extracted. ``reason`` is a sentence for the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_control(character: str) -> bool:
    code = ord(character)
    return code < 0x20 or 0x7F <= code <= 0x9F or character in _BIDI


def _is_surrogate(character: str) -> bool:
    return 0xD800 <= ord(character) <= 0xDFFF


def display(name: str) -> str:
    """``name`` for the screen: control characters, direction overrides and undecodable bytes become U+FFFD."""
    if name.isascii() and name.isprintable():
        return name
    return "".join(_REPLACEMENT if _is_control(c) or _is_surrogate(c) else c for c in name)


def split(name: str) -> list[str]:
    """The components of an entry name as the archive tree shows them: no empty ones, no ``.``; ``..`` is kept."""
    return [part for part in name.split("/") if part and part != "."]


def is_absolute(name: str) -> bool:
    return name.startswith("/")


def clean_component(part: str) -> tuple[str, bool]:
    """``part`` with control characters and direction overrides replaced by ``_``; whether anything changed."""
    if part.isascii() and part.isprintable():
        return part, False
    cleaned = "".join("_" if _is_control(c) else c for c in part)
    return cleaned, cleaned != part


def safe_parts(name: str, *, strip: int = 0) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The components to create below a destination for the entry ``name``, and notes about what was changed.

    ``strip`` leading components are dropped first (extracting from inside a
    folder of the archive). Raises ``UnsafeName`` when the name has a NUL
    character, a ``..`` component, a component the file system cannot hold,
    or nothing left. A leading ``/`` is dropped and control characters are
    replaced; both are reported in the notes.
    """
    if "\0" in name:
        raise UnsafeName("the name contains a NUL character")
    notes: list[str] = []
    raw = name.split("/")
    if any(part == ".." for part in raw):
        raise UnsafeName("the name leads outside the destination (“..”)")
    if is_absolute(name):
        notes.append("absolute path made relative")
    parts = [part for part in raw if part and part != "."][strip:]
    if not parts:
        raise UnsafeName("the name is empty")
    result = []
    total = 0
    changed = False
    for part in parts:
        cleaned, was_changed = clean_component(part)
        changed = changed or was_changed
        try:
            encoded = os.fsencode(cleaned)
        except (UnicodeEncodeError, ValueError):
            raise UnsafeName("the name cannot be written on this system") from None
        if len(encoded) > MAX_COMPONENT_BYTES:
            raise UnsafeName("the name is too long")
        if cleaned in (".", ".."):                    # only possible after cleaning; never a path step
            raise UnsafeName("the name leads outside the destination (“..”)")
        total += len(encoded) + 1
        result.append(cleaned)
    if total > MAX_PATH_BYTES:
        raise UnsafeName("the path is too long")
    if changed:
        notes.append("control characters in the name replaced")
    return tuple(result), tuple(notes)


def link_stays_inside(parent: tuple[str, ...], target: str) -> bool:
    """Whether the symbolic link target ``target``, read from the folder ``parent``, stays below the root.

    A lexical check: absolute targets, NUL and any walk above the root fail.
    The extractor checks the resolved path of every link again once all links
    exist, because links can be chained.
    """
    if not target or "\0" in target or target.startswith("/"):
        return False
    depth = len(parent)
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def numbered(name: str, number: int) -> str:
    """``report.txt`` → ``report (2).txt``; ``archive.tar.gz`` → ``archive (2).tar.gz``; dot files keep their name."""
    stem, extension = name, ""
    lowered = name.lower()
    for double in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if lowered.endswith(double) and len(name) > len(double):
            stem, extension = name[:-len(double)], name[-len(double):]
            break
    else:
        dot = name.rfind(".")
        if dot > 0 and len(name) - dot <= 12:
            stem, extension = name[:dot], name[dot:]
    suffix = f" ({number})"
    room = MAX_COMPONENT_BYTES - len(os.fsencode(extension)) - len(suffix)
    encoded = os.fsencode(stem)
    if len(encoded) > room:                 # keep the result a valid component: cut the stem, not the extension
        stem = os.fsdecode(encoded[:max(1, room)]).rstrip()
        while stem and len(os.fsencode(stem)) > room:
            stem = stem[:-1]
    return f"{stem}{suffix}{extension}"


# --- ZIP ---------------------------------------------------------------------------------------------------

_UNICODE_PATH = 0x7075
_UTF8_FLAG = 0x800


def _extra_fields(extra: bytes):
    position = 0
    while position + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, position)
        position += 4
        if position + size > len(extra):
            return
        yield tag, extra[position:position + size]
        position += size


def zip_name(info) -> str:
    """The name of a ``zipfile.ZipInfo`` as the user should see it.

    zipfile decodes a name without the UTF-8 flag as code page 437. Many
    programs write UTF-8 without setting the flag, and Info-ZIP adds the
    UTF-8 name in an extra field; both are honoured here. A name that is not
    valid UTF-8 stays code page 437, which is what the format says. The name
    is not cut at a NUL character (zipfile does that): ``safe_parts`` rejects
    such an entry instead.
    """
    name = getattr(info, "orig_filename", None) or info.filename
    if info.flag_bits & _UTF8_FLAG:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:              # a metadata_encoding other than cp437 was used
        return name
    for tag, data in _extra_fields(info.extra or b""):
        if tag == _UNICODE_PATH and len(data) > 5 and data[0] == 1:
            (checksum,) = struct.unpack_from("<I", data, 1)
            if checksum == zlib.crc32(raw) & 0xFFFFFFFF:
                try:
                    return data[5:].decode("utf-8")
                except UnicodeDecodeError:
                    break
    if not raw.isascii():
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return name


def zip_uses_backslashes(info) -> bool:
    """Whether a ZIP entry written on DOS or Windows uses ``\\`` as its path separator (against the format)."""
    return info.create_system == 0 and "\\" in info.filename and "/" not in info.filename


def nfc(text: str) -> str:
    """The composed form of ``text`` for comparing names (lone surrogates are left alone)."""
    try:
        return unicodedata.normalize("NFC", text)
    except (UnicodeError, ValueError):
        return text
