"""What Media Player knows and remembers, without Qt: tracks, the play queue, settings, recents, playlists.

Everything that is stored is JSON (or ``.m3u8`` for playlists) in the
application's XDG directories, written atomically with mode 0600 and
validated field by field when read: a damaged or foreign file gives the
defaults, never an exception in the window.
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import playlist_io

AUDIO_EXTENSIONS = (".mp3", ".flac", ".ogg", ".oga", ".opus", ".wav", ".m4a", ".m4b", ".aac", ".wma", ".mp2",
                    ".mka", ".aiff", ".aif")
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".mpg", ".mpeg", ".ogv", ".3gp",
                    ".ts", ".m2ts", ".flv")
PLAYLIST_EXTENSIONS = playlist_io.EXTENSIONS
REPEAT_MODES = ("off", "all", "one")
RATES = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
MAX_QUEUE = 20000
MAX_RECENTS = 40
MAX_RESUME = 500
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_FOLDERS = 64
RESUME_MARGIN_MS = 10_000            # a video is resumed only between its first and last ten seconds
_NAME = re.compile(r"^[^/\\\0-\x1f\x7f]{1,100}$")


def classify(location: str) -> str | None:
    """``"audio"``, ``"video"`` or ``"playlist"`` by the file extension; None for anything else."""
    extension = os.path.splitext(urlsplit(location).path if playlist_io.is_url(location) else location)[1].lower()
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in PLAYLIST_EXTENSIONS:
        return "playlist"
    return None


def format_time(milliseconds: int) -> str:
    """``3:07`` or ``1:02:09``; ``0:00`` for nothing or nonsense."""
    seconds = max(0, int(milliseconds)) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _text(value: object, limit: int = 512) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _int(value: object, low: int, high: int, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return max(low, min(high, int(value)))
    except (OverflowError, ValueError):
        return default


def valid_location(value: object) -> str | None:
    """A stored location that is still acceptable: an absolute path or an http(s) address."""
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
        return None
    if playlist_io.is_url(value):
        return value if urlsplit(value).netloc else None
    return value if os.path.isabs(value) else None


@dataclass
class Track:
    """One thing that can be played: a local file or a network stream."""

    location: str                     # absolute path or http(s) address
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    video: bool = False

    @property
    def is_remote(self) -> bool:
        return playlist_io.is_url(self.location)

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.is_remote:
            parts = urlsplit(self.location)
            name = unquote(os.path.basename(parts.path.rstrip("/")))
            return name or parts.netloc
        return os.path.splitext(os.path.basename(self.location))[0] or self.location

    @property
    def subtitle(self) -> str:
        """Artist, or for a stream the host it comes from."""
        if self.is_remote:
            return f"Network stream · {urlsplit(self.location).netloc}"
        return self.artist

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> "Track | None":
        if not isinstance(data, dict):
            return None
        location = valid_location(data.get("location"))
        if location is None:
            return None
        return cls(location, _text(data.get("title")), _text(data.get("artist")), _text(data.get("album")),
                   _int(data.get("duration_ms"), 0, 30 * 24 * 3600 * 1000), data.get("video") is True)

    @classmethod
    def from_location(cls, location: str) -> "Track":
        return cls(location, video=classify(location) == "video")


# --- the play queue ----------------------------------------------------------------------------------------

class PlayQueue:
    """The list of tracks being played, the current one, shuffle and repeat.

    ``index`` is the position of the current track in ``items`` (-1: none).
    With shuffle on, ``next`` and ``previous`` walk a random order of the
    whole list in which the current track comes first; the list itself
    keeps its order.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        self.items: list[Track] = []
        self.index = -1
        self.shuffle = False
        self.repeat = "off"
        self._order: list[int] = []
        self._rng = rng or random.Random()

    def __len__(self) -> int:
        return len(self.items)

    def current(self) -> Track | None:
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    # --- order ---
    def _reshuffle(self) -> None:
        rest = [i for i in range(len(self.items)) if i != self.index]
        self._rng.shuffle(rest)
        self._order = ([self.index] if 0 <= self.index < len(self.items) else []) + rest

    def _sequence(self) -> list[int]:
        if not self.shuffle:
            return list(range(len(self.items)))
        if sorted(self._order) != list(range(len(self.items))):
            self._reshuffle()
        return self._order

    def set_shuffle(self, shuffle: bool) -> None:
        self.shuffle = bool(shuffle)
        if self.shuffle:
            self._reshuffle()

    def set_repeat(self, mode: str) -> None:
        self.repeat = mode if mode in REPEAT_MODES else "off"

    def cycle_repeat(self) -> str:
        self.repeat = REPEAT_MODES[(REPEAT_MODES.index(self.repeat) + 1) % len(REPEAT_MODES)]
        return self.repeat

    def _step(self, step: int, wrap: bool) -> int:
        sequence = self._sequence()
        if not sequence:
            return -1
        if self.index not in sequence:
            return sequence[0]
        position = sequence.index(self.index) + step
        if 0 <= position < len(sequence):
            return sequence[position]
        if not wrap:
            return -1
        if self.shuffle and step > 0:
            self._reshuffle()                     # a new round in a new order
            sequence = self._order
            if len(sequence) > 1:                 # do not start the round with the track that just ended
                sequence.append(sequence.pop(0))
            return sequence[0]
        return sequence[position % len(sequence)]

    def next_index(self, *, auto: bool = False) -> int:
        """The index to play next (-1: nothing). ``auto``: the track ended by itself, so repeat-one applies."""
        if auto and self.repeat == "one" and self.current() is not None:
            return self.index
        return self._step(1, self.repeat == "all")

    def previous_index(self) -> int:
        return self._step(-1, self.repeat == "all")

    def has_next(self) -> bool:
        return self.next_index() >= 0

    def has_previous(self) -> bool:
        return self.previous_index() >= 0

    # --- content ---
    def set_items(self, tracks: list[Track], start: int = 0) -> None:
        self.items = list(tracks)[:MAX_QUEUE]
        self.index = start if 0 <= start < len(self.items) else (0 if self.items else -1)
        self._order = []
        if self.shuffle:
            self._reshuffle()

    def append(self, tracks: list[Track]) -> int:
        """Add tracks at the end; returns the index of the first one added (-1: the queue is full)."""
        room = MAX_QUEUE - len(self.items)
        tracks = list(tracks)[:max(0, room)]
        if not tracks:
            return -1
        first = len(self.items)
        self.items.extend(tracks)
        if self.shuffle:
            added = list(range(first, len(self.items)))
            self._rng.shuffle(added)
            self._order.extend(added)
        return first

    def insert_next(self, tracks: list[Track]) -> int:
        """Add tracks right after the current one; returns the index of the first one added."""
        room = MAX_QUEUE - len(self.items)
        tracks = list(tracks)[:max(0, room)]
        if not tracks:
            return -1
        at = self.index + 1 if self.index >= 0 else len(self.items)
        self.items[at:at] = tracks
        if self.shuffle:
            count = len(tracks)
            self._order = [i + count if i >= at else i for i in self._order]
            position = self._order.index(self.index) + 1 if self.index in self._order else len(self._order)
            self._order[position:position] = list(range(at, at + count))
        return at

    def remove(self, index: int) -> None:
        if not 0 <= index < len(self.items):
            return
        del self.items[index]
        if self.shuffle:
            self._order = [i - 1 if i > index else i for i in self._order if i != index]
        if index < self.index:
            self.index -= 1
        elif index == self.index:
            self.index = min(self.index, len(self.items) - 1)

    def move(self, source: int, target: int) -> None:
        """Move the item at ``source`` so that it ends up at index ``target``."""
        count = len(self.items)
        if not (0 <= source < count and 0 <= target < count) or source == target:
            return
        current = self.current()
        self.items.insert(target, self.items.pop(source))
        if current is not None:
            self.index = next(i for i, item in enumerate(self.items) if item is current)
        self._order = []
        if self.shuffle:
            self._reshuffle()

    def clear(self) -> None:
        self.items, self.index, self._order = [], -1, []

    # --- storage ---
    def to_dict(self) -> dict:
        return {"items": [track.to_dict() for track in self.items], "index": self.index, "shuffle": self.shuffle,
                "repeat": self.repeat}

    def load_dict(self, data: object) -> None:
        self.clear()
        if not isinstance(data, dict):
            return
        items = data.get("items")
        if isinstance(items, list):
            for raw in items[:MAX_QUEUE]:
                track = Track.from_dict(raw)
                if track is not None:
                    self.items.append(track)
        self.index = _int(data.get("index"), -1, len(self.items) - 1, -1)
        if self.items and self.index < 0:
            self.index = 0
        self.set_repeat(data.get("repeat") if isinstance(data.get("repeat"), str) else "off")
        self.set_shuffle(data.get("shuffle") is True)


# --- files ---------------------------------------------------------------------------------------------------

def read_json(path: str | os.PathLike) -> object:
    """The JSON value in ``path``; None when the file is missing, too big or not JSON."""
    try:
        if os.path.getsize(path) > MAX_JSON_BYTES:
            return None
        with open(path, "rb") as stream:
            return json.loads(stream.read(MAX_JSON_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def write_atomic(path: str | os.PathLike, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same folder (mode 0600). Raises ``OSError``."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_json(path: str | os.PathLike, value: object) -> None:
    write_atomic(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def user_dir(name: str) -> Path:
    """The user's ``MUSIC`` or ``VIDEOS`` folder from ``user-dirs.dirs`` (xdg-user-dirs); else ~/Music, ~/Videos."""
    home = Path.home()
    default = home / {"MUSIC": "Music", "VIDEOS": "Videos", "PICTURES": "Pictures"}.get(name, name.title())
    config = os.environ.get("XDG_CONFIG_HOME") or ""
    source = (Path(config) if config.startswith("/") else home / ".config") / "user-dirs.dirs"
    try:
        if source.stat().st_size > 64 * 1024:
            return default
        text = source.read_text("utf-8", "replace")
    except OSError:
        return default
    key = f"XDG_{name}_DIR"
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(key) or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if value.startswith("$HOME/"):
            path = home / value[6:]
        elif value == "$HOME":
            return default                        # "not set" in xdg-user-dirs
        elif value.startswith("/"):
            path = Path(value)
        else:
            continue
        return path
    return default


def _folders(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value[:MAX_FOLDERS]:
        if isinstance(item, str) and os.path.isabs(item) and "\0" not in item and len(item) < 4096:
            path = os.path.normpath(item)
            if path not in out:
                out.append(path)
    return out


@dataclass
class Settings:
    """What the user chose. ``None`` for the folder lists means "the default folder of the desktop"."""

    music_folders: list[str] = field(default_factory=list)
    video_folders: list[str] = field(default_factory=list)
    resume_videos: bool = True
    audio_device: str = ""            # "" follows the system default
    volume: float = 1.0
    muted: bool = False
    rate: float = 1.0

    @classmethod
    def defaults(cls) -> "Settings":
        return cls([str(user_dir("MUSIC"))], [str(user_dir("VIDEOS"))])

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Settings":
        settings = cls.defaults()
        data = read_json(path)
        if not isinstance(data, dict):
            return settings
        for name in ("music_folders", "video_folders"):
            folders = _folders(data.get(name))
            if folders is not None:
                setattr(settings, name, folders)
        if isinstance(data.get("resume_videos"), bool):
            settings.resume_videos = data["resume_videos"]
        if isinstance(data.get("muted"), bool):
            settings.muted = data["muted"]
        device = data.get("audio_device")
        if isinstance(device, str) and len(device) <= 512:
            settings.audio_device = device
        volume = data.get("volume")
        if isinstance(volume, (int, float)) and not isinstance(volume, bool) and 0 <= volume <= 1:
            settings.volume = float(volume)
        rate = data.get("rate")
        if isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate in RATES:
            settings.rate = float(rate)
        return settings

    def save(self, path: str | os.PathLike) -> None:
        write_json(path, asdict(self))


class Recents:
    """The tracks played most recently, newest first."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.items: list[Track] = []
        data = read_json(self.path)
        if isinstance(data, list):
            for raw in data[:MAX_RECENTS]:
                track = Track.from_dict(raw)
                if track is not None and all(track.location != other.location for other in self.items):
                    self.items.append(track)

    def add(self, track: Track) -> None:
        self.items = [item for item in self.items if item.location != track.location]
        self.items.insert(0, Track(**track.to_dict()))
        del self.items[MAX_RECENTS:]

    def remove(self, location: str) -> None:
        self.items = [item for item in self.items if item.location != location]

    def clear(self) -> None:
        self.items = []

    def save(self) -> None:
        write_json(self.path, [track.to_dict() for track in self.items])


class ResumePositions:
    """Where the user stopped watching each video (milliseconds), newest last."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.positions: dict[str, int] = {}
        data = read_json(self.path)
        if isinstance(data, dict):
            for location, position in list(data.items())[-MAX_RESUME:]:
                if valid_location(location) and isinstance(position, int) and not isinstance(position, bool):
                    if 0 < position < 30 * 24 * 3600 * 1000:
                        self.positions[location] = position

    def get(self, location: str) -> int:
        return self.positions.get(location, 0)

    def remember(self, location: str, position_ms: int, duration_ms: int) -> None:
        """Keep ``position_ms`` for ``location``, or forget it when it is near the start or the end."""
        self.positions.pop(location, None)
        if duration_ms > 0 and RESUME_MARGIN_MS <= position_ms <= duration_ms - RESUME_MARGIN_MS:
            self.positions[location] = int(position_ms)
            while len(self.positions) > MAX_RESUME:
                del self.positions[next(iter(self.positions))]

    def save(self) -> None:
        write_json(self.path, self.positions)


# --- playlists -----------------------------------------------------------------------------------------------

class PlaylistNameError(ValueError):
    """A playlist name that cannot be a file name, or that is taken."""


def check_playlist_name(name: str) -> str:
    """``name`` trimmed, if it can be a playlist (and a file) name; else ``PlaylistNameError`` with the reason."""
    name = " ".join(name.split()) if isinstance(name, str) else ""
    if not name:
        raise PlaylistNameError("Give the playlist a name.")
    if not _NAME.match(name) or name.startswith(".") or name in (".", ".."):
        raise PlaylistNameError("A playlist name cannot contain / or \\, start with a dot or be longer than "
                                "100 characters.")
    if len(name.encode("utf-8")) > 240:
        raise PlaylistNameError("That name is too long.")
    return name


class PlaylistStore:
    """The user's playlists: one ``<name>.m3u8`` file each in a folder of the configuration directory."""

    def __init__(self, directory: str | os.PathLike) -> None:
        self.directory = Path(directory)

    def _path(self, name: str) -> Path:
        return self.directory / f"{check_playlist_name(name)}.m3u8"

    def names(self) -> list[str]:
        try:
            found = [entry.name[:-5] for entry in os.scandir(self.directory)
                     if entry.name.endswith(".m3u8") and not entry.name.startswith(".") and entry.is_file()]
        except OSError:
            return []
        return sorted(found, key=str.casefold)

    def exists(self, name: str) -> bool:
        try:
            return self._path(name).is_file()
        except PlaylistNameError:
            return False

    def unique_name(self, wanted: str = "New playlist") -> str:
        taken = {name.casefold() for name in self.names()}
        if wanted.casefold() not in taken:
            return wanted
        number = 2
        while f"{wanted} {number}".casefold() in taken:
            number += 1
        return f"{wanted} {number}"

    def load(self, name: str) -> list[Track]:
        """The tracks of a playlist. Raises ``OSError`` or ``playlist_io.PlaylistError``."""
        path = self._path(name)
        tracks = []
        for entry in playlist_io.load(path):
            track = Track.from_location(entry.location)
            artist, separator, title = entry.title.partition(" - ")
            track.title, track.artist = (title, artist) if separator else (entry.title, "")
            track.duration_ms = entry.duration_s * 1000
            tracks.append(track)
        return tracks

    def save(self, name: str, tracks: list[Track]) -> None:
        entries = []
        for track in tracks:
            title = f"{track.artist} - {track.title}" if track.artist and track.title else track.title
            entries.append(playlist_io.Entry(track.location, title, track.duration_ms // 1000))
        write_atomic(self._path(name), playlist_io.dump_m3u(entries).encode("utf-8"))

    def create(self, name: str, tracks: list[Track] | None = None) -> str:
        name = check_playlist_name(name)
        if name.casefold() in {other.casefold() for other in self.names()}:
            raise PlaylistNameError(f"There is already a playlist called “{name}”.")
        self.save(name, tracks or [])
        return name

    def rename(self, old: str, new: str) -> str:
        new = check_playlist_name(new)
        if new == old:
            return new
        taken = {other.casefold() for other in self.names() if other != old}
        if new.casefold() in taken:
            raise PlaylistNameError(f"There is already a playlist called “{new}”.")
        os.replace(self._path(old), self._path(new))
        return new

    def delete(self, name: str) -> None:
        self._path(name).unlink()

    def modified(self, name: str) -> float:
        try:
            return self._path(name).stat().st_mtime
        except (OSError, PlaylistNameError):
            return 0.0

