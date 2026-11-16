"""Reading and writing playlist files: M3U, M3U8 and PLS (standard library only).

A playlist file is foreign input: its size, the number of entries and the
length of a line are capped, only local paths and ``http``/``https``
addresses are accepted (``file://`` addresses become paths, every other
scheme is dropped), and nothing in it is ever executed. Relative paths are
resolved against the folder of the playlist, as every player does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_LINE = 4096
MAX_TITLE = 512
EXTENSIONS = (".m3u", ".m3u8", ".pls")


class PlaylistError(ValueError):
    """The file is not a playlist this module reads (too big, wrong kind)."""


@dataclass
class Entry:
    location: str                 # an absolute path or an http(s) address
    title: str = ""
    duration_s: int = 0           # 0: not known


def is_url(location: str) -> bool:
    return location[:7].lower() == "http://" or location[:8].lower() == "https://"


def _title(text: str) -> str:
    return " ".join("".join(c for c in text if ord(c) >= 0x20 and ord(c) != 0x7F).split())[:MAX_TITLE]


def normalise(location: str, base_dir: str | os.PathLike | None) -> str | None:
    """An entry of a playlist as an absolute path or an http(s) address; None for anything else."""
    location = location.strip()
    if not location or len(location) > MAX_LINE or "\0" in location:
        return None
    if is_url(location):
        parts = urlsplit(location)
        return location if parts.netloc else None
    lowered = location[:8].lower()
    if lowered.startswith("file:"):
        parts = urlsplit(location)
        if parts.netloc not in ("", "localhost"):
            return None
        location = unquote(parts.path)
        if not location.startswith("/"):
            return None
    elif "://" in location[:16]:
        return None                               # rtsp://, mms://, smb://, ...: not played
    elif len(location) > 2 and location[1] == ":" and location[2] in "\\/":
        return None                               # a path of another operating system
    if "\\" in location and "/" not in location:
        location = location.replace("\\", "/")    # a relative path written on another system
    if not os.path.isabs(location):
        if base_dir is None:
            return None
        location = os.path.join(os.fspath(base_dir), location)
    return os.path.normpath(location)


def decode(data: bytes, utf8: bool) -> str:
    """The text of a playlist: UTF-8 (with or without a byte order mark), else Latin-1 for an old ``.m3u``."""
    if len(data) > MAX_FILE_BYTES:
        raise PlaylistError("the playlist is larger than 4 MB")
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if utf8:
            return data.decode("utf-8", "replace")
        return data.decode("latin-1")


def parse_m3u(text: str, base_dir: str | os.PathLike | None = None) -> list[Entry]:
    entries: list[Entry] = []
    title, duration = "", 0
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) > MAX_LINE:
            title, duration = "", 0
            continue
        if line.startswith("#"):
            if line[:8].upper() == "#EXTINF:":
                info = line[8:]
                seconds, _comma, rest = info.partition(",")
                number = seconds.strip().split(" ", 1)[0]
                try:
                    duration = max(0, min(int(float(number)), 7 * 24 * 3600))
                except (ValueError, OverflowError):
                    duration = 0
                title = _title(rest)
            continue
        location = normalise(line, base_dir)
        if location is not None:
            entries.append(Entry(location, title, duration))
            if len(entries) >= MAX_ENTRIES:
                break
        title, duration = "", 0
    return entries


def parse_pls(text: str, base_dir: str | os.PathLike | None = None) -> list[Entry]:
    files: dict[int, str] = {}
    titles: dict[int, str] = {}
    lengths: dict[int, int] = {}
    in_section = False
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) > MAX_LINE or line[0] in ";#":
            continue
        if line.startswith("["):
            in_section = line.lower() == "[playlist]"
            continue
        if not in_section or "=" not in line:
            continue
        key, _equals, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        for prefix, target in (("file", files), ("title", titles), ("length", lengths)):
            if key.startswith(prefix) and key[len(prefix):].isascii() and key[len(prefix):].isdigit():
                number = int(key[len(prefix):][:6])
                if not 0 < number <= MAX_ENTRIES:
                    break
                if target is lengths:
                    try:
                        lengths[number] = max(0, min(int(value), 7 * 24 * 3600))
                    except ValueError:
                        pass
                elif len(target) < MAX_ENTRIES:
                    target[number] = value
                break
    entries = []
    for number in sorted(files):
        location = normalise(files[number], base_dir)
        if location is not None:
            entries.append(Entry(location, _title(titles.get(number, "")), lengths.get(number, 0)))
    return entries


def parse(data: bytes, name: str, base_dir: str | os.PathLike | None = None) -> list[Entry]:
    """The entries of a playlist file with the file name ``name`` (its extension says which format)."""
    extension = os.path.splitext(name)[1].lower()
    if extension not in EXTENSIONS:
        raise PlaylistError("not a playlist file")
    text = decode(data, utf8=extension != ".m3u")
    if extension == ".pls" or text.lstrip()[:10].lower() == "[playlist]":
        return parse_pls(text, base_dir)
    return parse_m3u(text, base_dir)


def load(path: str | os.PathLike) -> list[Entry]:
    """Read a playlist file. Raises ``OSError`` or ``PlaylistError``."""
    path = os.fspath(path)
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise PlaylistError("the playlist is larger than 4 MB")
    with open(path, "rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    return parse(data, os.path.basename(path), os.path.dirname(os.path.abspath(path)))


def _relative(location: str, base_dir: str | os.PathLike | None) -> str:
    """``location`` relative to ``base_dir`` when it lies inside it; else unchanged."""
    if base_dir is None or is_url(location):
        return location
    base = os.path.normpath(os.fspath(base_dir))
    if os.path.commonpath([base, location]) == base if os.path.isabs(location) else False:
        return os.path.relpath(location, base)
    return location


def dump_m3u(entries: list[Entry], base_dir: str | os.PathLike | None = None) -> str:
    """Extended M3U text (write it as UTF-8: an ``.m3u8`` file)."""
    lines = ["#EXTM3U"]
    for entry in entries:
        if "\n" in entry.location or "\r" in entry.location:
            continue                              # a line break cannot be written in this format
        if entry.title or entry.duration_s:
            lines.append(f"#EXTINF:{int(entry.duration_s) if entry.duration_s else -1},{_title(entry.title)}")
        lines.append(_relative(entry.location, base_dir))
    return "\n".join(lines) + "\n"


def dump_pls(entries: list[Entry], base_dir: str | os.PathLike | None = None) -> str:
    lines = ["[playlist]"]
    number = 0
    for entry in entries:
        if "\n" in entry.location or "\r" in entry.location:
            continue
        number += 1
        lines.append(f"File{number}={_relative(entry.location, base_dir)}")
        if entry.title:
            lines.append(f"Title{number}={_title(entry.title)}")
        lines.append(f"Length{number}={int(entry.duration_s) if entry.duration_s else -1}")
    lines.append(f"NumberOfEntries={number}")
    lines.append("Version=2")
    return "\n".join(lines) + "\n"


def dump(entries: list[Entry], name: str, base_dir: str | os.PathLike | None = None) -> bytes:
    """The bytes of a playlist file named ``name`` (``.m3u8``, ``.m3u`` or ``.pls``), UTF-8."""
    extension = os.path.splitext(name)[1].lower()
    if extension == ".pls":
        return dump_pls(entries, base_dir).encode("utf-8")
    if extension in (".m3u", ".m3u8"):
        return dump_m3u(entries, base_dir).encode("utf-8")
    raise PlaylistError("not a playlist file")
