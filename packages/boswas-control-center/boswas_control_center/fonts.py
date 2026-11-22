"""The installed fonts, read-only and without Qt.

`fc-list --format=%{family[0]}\\t%{style[0]}\\t%{file}\\n` (fontconfig) prints one
line per font file: the first family name, the first style name and the
file. They are grouped by family; a family whose files are all in the
user's font folder (<data>/fonts, userfiles.font_dir) was installed by the
user and can be removed there. Fonts of the system come with packages and
are not removed here.

Installing copies a file the user chose into that folder (userfiles.
install_font: a TrueType, OpenType or collection header, at most 64 MB) and
then runs `fc-cache -f <folder>` (commands.fc_cache_argv) so that programs
find it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import viewmodel as vm

MAX_LINES = 20000


@dataclass(frozen=True)
class FontFile:
    path: str
    style: str


@dataclass(frozen=True)
class Family:
    name: str
    files: tuple[FontFile, ...]
    user: bool               # every file of it is in the user's font folder

    @property
    def styles(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.style for f in self.files if f.style))

    @property
    def summary(self) -> str:
        count = len(self.styles) or len(self.files)
        where = "Installed by you" if self.user else "Comes with the system"
        return f"{count} style{'s' if count != 1 else ''} · {where}"


def parse_fc_list(text: str, user_dir: str | None = None) -> list[Family]:
    """Families from the fc-list lines, sorted by name (the user's folder decides what is the user's)."""
    groups: dict[str, list[FontFile]] = {}
    user_dir = os.path.normpath(user_dir) + os.sep if user_dir else None
    for line in (text or "").splitlines()[:MAX_LINES]:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        family, style, path = (vm.one_line(parts[0], 120), vm.one_line(parts[1], 60), parts[2].strip())
        if not family or not path.startswith("/") or "\0" in path or len(path) > 1024:
            continue
        groups.setdefault(family, [])
        if all(existing.path != path for existing in groups[family]):
            groups[family].append(FontFile(path, style))
    families = []
    for name, files in groups.items():
        user = bool(user_dir) and all(os.path.normpath(f.path).startswith(user_dir) for f in files)
        families.append(Family(name, tuple(sorted(files, key=lambda f: (f.style != "Regular", f.style))), user))
    families.sort(key=lambda f: f.name.casefold())
    return families


def search(families: list[Family], query: str) -> list[Family]:
    words = query.casefold().split()
    return [f for f in families if all(word in f.name.casefold() for word in words)]


SAMPLE = "The quick brown fox jumps over the lazy dog. 0123456789"
SIZES = (12, 16, 24, 36, 48, 72)
