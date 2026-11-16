"""Natural order of file names: runs of digits compare as numbers, letters without regard to case.

``IMG_2.jpg`` sorts before ``IMG_10.jpg`` and ``b.png`` after ``A.png``, as
in the file manager. Names that are equal that way keep a fixed order (by
the exact name), so the order of a folder never depends on how the file
system lists it.
"""

from __future__ import annotations

import os
import re

_DIGITS = re.compile(r"(\d+)")
# A run of digits longer than this is compared as text: no file name needs more, and int() of an
# absurdly long run would be slow (and is refused by Python beyond 4300 digits).
_MAX_NUMBER_DIGITS = 64


def natural_key(name: str) -> tuple:
    """The sort key of one name (not a path)."""
    parts = _DIGITS.split(name.casefold())
    key: list[tuple] = []
    for index, part in enumerate(parts):
        if index % 2 and len(part) <= _MAX_NUMBER_DIGITS:
            # digits: by value, then fewer leading zeros first
            key.append((0, int(part), len(part)))
        else:
            key.append((1, part))
    key.append((2, name))
    return tuple(key)


def path_key(path: str | os.PathLike) -> tuple:
    """The sort key of the last component of a path."""
    return natural_key(os.path.basename(os.fspath(path)))


def natural_sorted(names) -> list:
    """``names`` (names or paths) in natural order of their last component."""
    return sorted(names, key=path_key)
