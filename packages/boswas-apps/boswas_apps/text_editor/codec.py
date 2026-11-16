"""Text encodings and line endings of files (standard library only, no Qt).

``detect`` finds the encoding of the bytes of a file and refuses binary
data; ``decode`` and ``encode`` are strict, so what was read is written
back byte for byte when the text was not changed:

1. a byte order mark decides: UTF-8 with BOM, UTF-16 LE, UTF-16 BE;
2. data that is valid UTF-8 is UTF-8;
3. data with a zero in every other byte is UTF-16 without a mark;
4. data with zero bytes, or mostly control characters, is binary: refused;
5. anything else is a single-byte encoding: the one of the locale if it has
   one, Windows-1252 if every byte is defined there, ISO 8859-1 (Latin-1)
   otherwise, which gives every byte a character and so never loses one.

In memory text has ``\\n`` line ends. ``count_line_endings`` tells what the
file used, ``to_line_ending`` converts on the way out.
"""

from __future__ import annotations

import codecs
import locale
from dataclasses import dataclass

LF, CRLF, CR = "lf", "crlf", "cr"
LINE_ENDINGS = {LF: "\n", CRLF: "\r\n", CR: "\r"}
LINE_ENDING_LABELS = {LF: "Unix (LF)", CRLF: "Windows (CRLF)", CR: "Macintosh (CR)"}
_SAMPLE = 64 * 1024
_ALLOWED_CONTROLS = frozenset(b"\t\n\r\f\x08\x1b")


class BinaryFileError(ValueError):
    """The data is not text."""


@dataclass(frozen=True)
class Encoding:
    key: str
    label: str
    codec: str                 # the Python codec, without a byte order mark
    bom: bytes = b""


UTF8 = Encoding("utf-8", "UTF-8", "utf-8")
UTF8_BOM = Encoding("utf-8-bom", "UTF-8 with BOM", "utf-8", codecs.BOM_UTF8)
UTF16_LE = Encoding("utf-16-le", "UTF-16 LE", "utf-16-le", codecs.BOM_UTF16_LE)
UTF16_BE = Encoding("utf-16-be", "UTF-16 BE", "utf-16-be", codecs.BOM_UTF16_BE)
UTF16_LE_PLAIN = Encoding("utf-16-le-plain", "UTF-16 LE without BOM", "utf-16-le")
UTF16_BE_PLAIN = Encoding("utf-16-be-plain", "UTF-16 BE without BOM", "utf-16-be")
WINDOWS_1252 = Encoding("windows-1252", "ANSI (Windows-1252)", "cp1252")
LATIN_1 = Encoding("iso-8859-1", "Western (ISO 8859-1)", "iso-8859-1")

# What the status bar offers.
CHOICES = (UTF8, UTF8_BOM, UTF16_LE, UTF16_BE, WINDOWS_1252, LATIN_1)
_BY_KEY = {encoding.key: encoding for encoding in (*CHOICES, UTF16_LE_PLAIN, UTF16_BE_PLAIN)}
_UNICODE_CODECS = ("utf", "ucs", "unicode")
_MULTI_BYTE = ("big5", "gb", "euc", "shift", "sjis", "cp932", "cp936", "cp949", "cp950", "johab", "iso2022", "hz")


def by_key(key: str) -> Encoding:
    """An encoding from the key it was saved under (a session, a setting); UTF-8 for an unknown key."""
    if key in _BY_KEY:
        return _BY_KEY[key]
    if isinstance(key, str) and key.startswith("locale:"):
        found = _single_byte(key[7:])
        if found is not None:
            return found
    return UTF8


def _single_byte(name: str | None) -> Encoding | None:
    """The single-byte encoding ``name`` (a locale's), or None when it is Unicode, multi-byte or unknown."""
    if not name or not isinstance(name, str) or len(name) > 40:
        return None
    try:
        info = codecs.lookup(name)
    except (LookupError, ValueError):
        return None
    normal = info.name.replace("-", "").replace("_", "")
    if normal.startswith(_UNICODE_CODECS) or normal.startswith(_MULTI_BYTE) or normal == "ascii":
        return None
    if info.name == "cp1252":
        return WINDOWS_1252
    if info.name == "iso8859-1":
        return LATIN_1
    return Encoding(f"locale:{info.name}", f"ANSI ({info.name})", info.name)


def locale_encoding() -> Encoding | None:
    """The single-byte encoding of the user's locale, if it has one (a UTF-8 locale has none)."""
    try:
        return _single_byte(locale.getpreferredencoding(False))
    except (ValueError, locale.Error):
        return None


def looks_binary(data: bytes) -> bool:
    """Whether bytes without a Unicode signature are something other than text."""
    sample = data[:_SAMPLE]
    if b"\x00" in data:
        return True
    if not sample:
        return False
    controls = sum(1 for byte in sample if byte < 0x20 and byte not in _ALLOWED_CONTROLS)
    return controls * 10 > len(sample)


def _plain_utf16(data: bytes) -> Encoding | None:
    """UTF-16 without a byte order mark: (nearly) every other byte of Latin text is zero."""
    sample = data[:_SAMPLE]
    if len(data) < 2 or len(data) % 2 or b"\x00" not in sample:
        return None
    even, odd = sample[0::2], sample[1::2]
    zeros_even, zeros_odd = even.count(0), odd.count(0)
    half = max(len(odd), 1)
    if zeros_odd * 10 >= half * 7 and zeros_even * 20 <= half:
        return UTF16_LE_PLAIN
    if zeros_even * 10 >= half * 7 and zeros_odd * 20 <= half:
        return UTF16_BE_PLAIN
    return None


def _has_controls(text: str) -> bool:
    """Whether decoded text holds control characters no text file has."""
    return any(ord(character) < 0x20 and character not in "\t\n\r\f\x08\x1b" for character in text[:_SAMPLE])


def detect(data: bytes, locale_fallback: Encoding | None = None) -> Encoding:
    """The encoding of the bytes of a file. Raises BinaryFileError for data that is not text."""
    if data.startswith(codecs.BOM_UTF8):
        return UTF8_BOM
    if data.startswith(codecs.BOM_UTF16_LE):
        return UTF16_LE
    if data.startswith(codecs.BOM_UTF16_BE):
        return UTF16_BE
    plain = _plain_utf16(data)
    if plain is not None:
        try:
            if not _has_controls(data.decode(plain.codec)):
                return plain
        except UnicodeDecodeError:
            pass
    if looks_binary(data):
        raise BinaryFileError("the file is not text")
    try:
        data.decode("utf-8")
        return UTF8
    except UnicodeDecodeError:
        pass
    for candidate in (locale_fallback, WINDOWS_1252):
        if candidate is None:
            continue
        try:
            data.decode(candidate.codec)
            return candidate
        except UnicodeDecodeError:
            continue
    return LATIN_1


def decode(data: bytes, encoding: Encoding) -> str:
    """The text of ``data`` in ``encoding`` (its byte order mark removed). Strict: raises UnicodeDecodeError."""
    if encoding.bom and data.startswith(encoding.bom):
        data = data[len(encoding.bom):]
    return data.decode(encoding.codec)


def encode(text: str, encoding: Encoding) -> bytes:
    """``text`` as bytes in ``encoding``, with its byte order mark. Strict: raises UnicodeEncodeError."""
    return encoding.bom + text.encode(encoding.codec)


def can_encode(text: str, encoding: Encoding) -> bool:
    try:
        text.encode(encoding.codec)
        return True
    except UnicodeEncodeError:
        return False


# --- line endings ------------------------------------------------------------------------------------------

def count_line_endings(text: str) -> dict[str, int]:
    """How many line ends of each kind ``text`` has."""
    crlf = text.count("\r\n")
    return {LF: text.count("\n") - crlf, CRLF: crlf, CR: text.count("\r") - crlf}


def dominant_line_ending(counts: dict[str, int], default: str = LF) -> str:
    """The most frequent kind (``default`` for text without line ends; CRLF, then LF win a tie)."""
    if not any(counts.values()):
        return default
    return max((CRLF, LF, CR), key=lambda kind: counts.get(kind, 0))


def is_mixed(counts: dict[str, int]) -> bool:
    return sum(1 for value in counts.values() if value) > 1


def describe_line_endings(counts: dict[str, int]) -> str:
    """``120 Windows (CRLF), 3 Unix (LF)`` for the message about mixed line ends."""
    return ", ".join(f"{counts[kind]} {LINE_ENDING_LABELS[kind]}" for kind in (CRLF, LF, CR) if counts.get(kind))


def normalize_line_endings(text: str) -> str:
    """``text`` with every line end as ``\\n``."""
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def to_line_ending(text: str, kind: str) -> str:
    """``text`` (with ``\\n`` line ends) with the line ends of ``kind``."""
    if kind == LF:
        return text
    return text.replace("\n", LINE_ENDINGS[kind])


def trim_trailing_whitespace(text: str) -> str:
    """``text`` without spaces and tabs at the ends of its lines."""
    if " \n" not in text and "\t\n" not in text and not text.endswith((" ", "\t")):
        return text
    return "\n".join(line.rstrip(" \t") for line in text.split("\n"))
