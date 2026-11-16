"""The music and video library: a persistent index of the files under the library folders (no Qt).

``scan`` walks the folders and returns the new index. A file whose size
and modification time did not change keeps its entry, so a rescan of an
unchanged library reads no file at all; new and changed files have their
tags read with ``tags.read_tags``. The scan runs on a worker thread: it
checks a ``threading.Event`` to stop early and reports progress through a
callback.

Symbolic links to folders are not followed (a link back to a parent would
never end), hidden files and folders are skipped, and the depth of the walk
and the number of files are capped.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import model, tags

INDEX_VERSION = 2
MAX_FILES = 200_000
MAX_DEPTH = 24
MAX_COVER_BYTES = tags.MAX_PICTURE_BYTES
COVER_NAMES = ("cover", "folder", "front", "album", "albumart")
COVER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
UNKNOWN_ARTIST = "Unknown artist"
UNKNOWN_ALBUM = "Unknown album"


@dataclass
class Song:
    """One file of the library (audio or video)."""

    path: str
    size: int = 0
    mtime_ns: int = 0
    video: bool = False
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    year: int = 0
    track: int = 0
    disc: int = 0
    duration_ms: int = 0
    has_picture: bool = False

    @property
    def display_title(self) -> str:
        return self.title or os.path.splitext(os.path.basename(self.path))[0]

    @property
    def display_artist(self) -> str:
        return self.artist or self.album_artist or UNKNOWN_ARTIST

    @property
    def display_album(self) -> str:
        return self.album or UNKNOWN_ALBUM

    def to_track(self) -> model.Track:
        return model.Track(self.path, self.display_title, "" if self.video else self.display_artist,
                           self.album, self.duration_ms, self.video)

    @classmethod
    def from_dict(cls, data: object) -> "Song | None":
        if not isinstance(data, dict):
            return None
        path = data.get("path")
        if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path or len(path) > 4096:
            return None

        def text(name: str) -> str:
            value = data.get(name)
            return value[:tags.MAX_TEXT_CHARS] if isinstance(value, str) else ""

        def number(name: str, high: int) -> int:
            value = data.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= high else 0

        return cls(path, number("size", 1 << 62), number("mtime_ns", 1 << 62), data.get("video") is True,
                   text("title"), text("artist"), text("album"), text("album_artist"), text("genre"),
                   number("year", 9999), number("track", 99999), number("disc", 99999),
                   number("duration_ms", 30 * 24 * 3600 * 1000), data.get("has_picture") is True)


@dataclass
class Album:
    title: str
    artist: str
    year: int = 0
    songs: list[Song] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Tells this album from the others of the library (its first file belongs to no other album)."""
        return self.songs[0].path if self.songs else f"{self.artist}\x1f{self.title}"

    @property
    def duration_ms(self) -> int:
        return sum(song.duration_ms for song in self.songs)


@dataclass
class Artist:
    name: str
    songs: list[Song] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)


# --- the index file --------------------------------------------------------------------------------------

def load_index(path: str | os.PathLike) -> dict[str, Song]:
    """The stored index; empty when there is none or it is not one this version wrote."""
    data = model.read_json(path)
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION or not isinstance(data.get("songs"), list):
        return {}
    index: dict[str, Song] = {}
    for raw in data["songs"][:MAX_FILES]:
        song = Song.from_dict(raw)
        if song is not None:
            index[song.path] = song
    return index


def save_index(path: str | os.PathLike, index: dict[str, Song]) -> None:
    model.write_json(path, {"version": INDEX_VERSION, "songs": [asdict(song) for song in index.values()]})


# --- scanning -----------------------------------------------------------------------------------------------

@dataclass
class ScanResult:
    index: dict[str, Song]
    read: int = 0                 # files whose tags were read (new or changed)
    kept: int = 0                 # files taken over from the previous index
    removed: int = 0              # files that are gone
    cancelled: bool = False
    unreadable: list[str] = field(default_factory=list)      # folders that could not be listed


def _walk(folder: str, cancel: threading.Event, unreadable: list[str]) -> Iterable[os.DirEntry]:
    stack = [(folder, 0)]
    seen: set[tuple[int, int]] = set()
    while stack and not cancel.is_set():
        directory, depth = stack.pop()
        try:
            info = os.stat(directory)
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            with os.scandir(directory) as entries:
                listing = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            if depth == 0:
                unreadable.append(directory)
            continue
        for entry in listing:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        stack.append((entry.path, depth + 1))
                elif entry.is_file():                     # a link to a file is followed
                    yield entry
            except OSError:
                continue


def scan(folders: Iterable[str], kinds: tuple[str, ...], previous: dict[str, Song], cancel: threading.Event,
         progress: Callable[[int, str], None] | None = None) -> ScanResult:
    """Walk ``folders`` for files of ``kinds`` (``"audio"``, ``"video"``) and return the new index.

    ``previous`` is the index of the last scan. ``progress(count, path)`` is
    called from the scanning thread now and then.
    """
    result = ScanResult({})
    index = result.index
    for folder in folders:
        folder = os.path.normpath(folder)
        if not os.path.isabs(folder):
            continue
        for entry in _walk(folder, cancel, result.unreadable):
            if cancel.is_set():
                break
            kind = model.classify(entry.name)
            if kind not in kinds or entry.path in index:
                continue
            if len(index) >= MAX_FILES:
                break
            try:
                info = entry.stat()
            except OSError:
                continue
            old = previous.get(entry.path)
            if old is not None and old.size == info.st_size and old.mtime_ns == info.st_mtime_ns:
                index[entry.path] = old
                result.kept += 1
            else:
                index[entry.path] = read_song(entry.path, info.st_size, info.st_mtime_ns, kind == "video")
                result.read += 1
            if progress is not None and len(index) % 25 == 0:
                progress(len(index), entry.path)
    if cancel.is_set():
        result.cancelled = True
        # A scan that was stopped must not forget the files it did not reach.
        for path, song in previous.items():
            index.setdefault(path, song)
    else:
        result.removed = sum(1 for path in previous if path not in index)
    return result


def read_song(path: str, size: int, mtime_ns: int, video: bool) -> Song:
    """The index entry of one file. A file that cannot be read gets an entry without tags."""
    song = Song(path, size, mtime_ns, video)
    try:
        found = tags.read_tags(path)
    except OSError:
        return song
    song.title, song.artist, song.album = found.title, found.artist, found.album
    song.album_artist, song.genre, song.year = found.album_artist, found.genre, found.year
    song.track, song.disc, song.duration_ms = found.track, found.disc, found.duration_ms
    song.has_picture = found.has_picture
    return song


# --- grouping ---------------------------------------------------------------------------------------------

def _sort_text(text: str) -> str:
    folded = text.casefold()
    for article in ("the ", "a ", "an "):
        if folded.startswith(article):
            return folded[len(article):]
    return folded


def songs_of(index: dict[str, Song], video: bool = False) -> list[Song]:
    """The audio (or video) entries, sorted by title."""
    return sorted((song for song in index.values() if song.video == video),
                  key=lambda song: (_sort_text(song.display_title), song.path))


def albums_of(songs: Iterable[Song]) -> list[Album]:
    """Songs grouped into albums, sorted by title.

    Songs with the same album title belong together when they name the
    same album artist, or lie in the same folder (a compilation whose files
    name no album artist), or have the same artist (an album spread over
    several folders). Songs without an album title are grouped per folder.
    """
    by_folder: dict[tuple[str, str], list[Song]] = {}
    for song in songs:
        title = song.album.casefold()
        if song.album and song.album_artist:
            key = (title, f"artist\x1f{song.album_artist.casefold()}")
        else:
            key = (title, f"folder\x1f{os.path.dirname(song.path)}")
        by_folder.setdefault(key, []).append(song)
    albums: dict[tuple[str, str], Album] = {}
    for (title, group), members in by_folder.items():
        first = members[0]
        artists = {song.artist.casefold() for song in members}
        if first.album and first.album_artist:
            key, artist = (title, group), first.album_artist
        elif first.album and len(artists) == 1:
            key, artist = (title, f"artist\x1f{first.artist.casefold()}"), first.artist or UNKNOWN_ARTIST
        else:
            key = (title, group)
            artist = (first.artist or UNKNOWN_ARTIST) if len(artists) == 1 else "Various artists"
        album = albums.setdefault(key, Album(first.display_album, artist))
        album.songs.extend(members)
    for album in albums.values():
        album.songs.sort(key=lambda song: (song.disc, song.track, _sort_text(song.display_title), song.path))
        album.year = next((song.year for song in album.songs if song.year), 0)
    return sorted(albums.values(), key=lambda album: (_sort_text(album.title), _sort_text(album.artist)))


def artists_of(songs: Iterable[Song]) -> list[Artist]:
    """Songs grouped by artist (the album artist when a song has one), sorted by name."""
    artists: dict[str, Artist] = {}
    for song in songs:
        name = song.album_artist or song.artist or UNKNOWN_ARTIST
        artists.setdefault(name.casefold(), Artist(name)).songs.append(song)
    for artist in artists.values():
        artist.albums = albums_of(artist.songs)
        artist.songs.sort(key=lambda song: (_sort_text(song.display_album), song.disc, song.track,
                                            _sort_text(song.display_title)))
    return sorted(artists.values(), key=lambda artist: _sort_text(artist.name))


def search(songs: Iterable[Song], query: str) -> list[Song]:
    """The songs whose title, artist or album contains every word of ``query``."""
    words = query.casefold().split()
    if not words:
        return list(songs)
    found = []
    for song in songs:
        haystack = f"{song.display_title}\n{song.artist}\n{song.album_artist}\n{song.album}".casefold()
        if all(word in haystack for word in words):
            found.append(song)
    return found


# --- covers -----------------------------------------------------------------------------------------------

def folder_cover(directory: str | os.PathLike) -> str | None:
    """The path of ``cover.jpg``, ``folder.jpg``, ... in ``directory`` (any letter case), or None."""
    try:
        with os.scandir(directory) as entries:
            names = {entry.name.lower(): entry.path for entry in entries if entry.is_file()}
    except OSError:
        return None
    for name in COVER_NAMES:
        for extension in COVER_EXTENSIONS:
            path = names.get(name + extension)
            if path is not None:
                return path
    return None


def cover_bytes(path: str | os.PathLike, embedded: bool = True) -> bytes | None:
    """The cover of a file as image bytes: its embedded picture, else an image of its folder; None when neither.

    The bytes are whatever the file holds: decode them with a size limit.
    """
    path = os.fspath(path)
    if embedded:
        try:
            picture = tags.read_picture(path)
        except OSError:
            picture = None
        if picture is not None:
            return picture.data
    cover = folder_cover(os.path.dirname(path))
    if cover is None:
        return None
    try:
        if os.path.getsize(cover) > MAX_COVER_BYTES:
            return None
        with open(cover, "rb") as stream:
            data = stream.read(MAX_COVER_BYTES + 1)
    except OSError:
        return None
    return data if tags.sniff_image(data) else None


def cache_key(path: str | os.PathLike, extra: str = "") -> str:
    """A file name for something derived from ``path`` as it is now (its size and time are part of the key)."""
    path = os.fspath(path)
    try:
        info = os.stat(path)
        stamp = f"{info.st_size}:{info.st_mtime_ns}"
    except OSError:
        stamp = "missing"
    return hashlib.sha256(f"{path}\0{stamp}\0{extra}".encode("utf-8", "surrogatepass")).hexdigest()[:40]


def prune_cache(directory: str | os.PathLike, keep: int = 4000) -> int:
    """Delete the oldest files of a cache folder beyond ``keep`` entries; returns how many were deleted."""
    try:
        with os.scandir(directory) as entries:
            files = [(entry.stat().st_mtime, entry.path) for entry in entries if entry.is_file()]
    except OSError:
        return 0
    files.sort(reverse=True)
    removed = 0
    for _mtime, path in files[keep:]:
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def default_index_path(state_dir: Path) -> Path:
    return state_dir / "library.json"
