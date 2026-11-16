"""How many terminal cells a character takes (0, 1 or 2), from the Unicode database of the standard library.

The rules are those of the C library's ``wcwidth``, which is what programs
running in the terminal use to lay out their text, so both sides agree:

* control characters have no width (-1 here; the emulator never prints them);
* combining marks (categories Mn and Me), format characters (Cf: zero width
  joiner, direction marks, variation selectors, tags) and the conjoining
  Hangul vowels and final consonants take no cell: they belong to the
  character before them;
* characters whose East Asian Width is Wide or Fullwidth (CJK, Hangul
  syllables, most emoji) take two cells;
* everything else takes one. Spacing combining marks (Mc, common in Indic
  scripts) take one cell, as in the C library.
"""

from __future__ import annotations

import unicodedata

# Format characters that are visible signs and take a cell (Arabic number signs and the like).
_VISIBLE_FORMAT = frozenset((0x00AD, 0x0600, 0x0601, 0x0602, 0x0603, 0x0604, 0x0605, 0x06DD, 0x070F, 0x0890, 0x0891,
                             0x08E2, 0x110BD, 0x110CD))
_CACHE: dict[str, int] = {}
_CACHE_LIMIT = 1 << 16


def _compute(char: str) -> int:
    code = ord(char)
    if code < 0x20 or 0x7F <= code < 0xA0:
        return -1
    if code < 0x300:
        return 1
    category = unicodedata.category(char)
    if category in ("Mn", "Me"):
        return 0
    if category == "Cf":
        return 1 if code in _VISIBLE_FORMAT else 0
    if 0x1160 <= code <= 0x11FF or 0xD7B0 <= code <= 0xD7FF:
        return 0                      # Hangul Jamo medial vowels and final consonants
    if category in ("Zl", "Zp"):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def wcwidth(char: str) -> int:
    """Cells taken by one character: 0, 1 or 2; -1 for a control character."""
    width = _CACHE.get(char)
    if width is None:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        width = _CACHE[char] = _compute(char)
    return width


def wcswidth(text: str) -> int:
    """Cells taken by a string; control characters count as nothing."""
    if text.isascii():
        return sum(1 for char in text if " " <= char < "\x7f")
    return sum(max(0, wcwidth(char)) for char in text)


def clusters(text: str) -> list[tuple[str, int]]:
    """Split printable text into the pieces a terminal puts into cells: (characters, width 1 or 2).

    A zero-width character joins the piece before it; one at the very start
    of the text is dropped (there is nothing to attach it to).
    """
    out: list[tuple[str, int]] = []
    for char in text:
        width = wcwidth(char)
        if width > 0:
            out.append((char, width))
        elif width == 0 and out:
            previous, previous_width = out[-1]
            out[-1] = (previous + char, previous_width)
    return out
