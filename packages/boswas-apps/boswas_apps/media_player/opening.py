"""Turning what the user opened (files, folders, playlists, addresses) into tracks. No Qt; runs on a worker thread.

Locations come from the command line, a file dialog, a drop or another
program (MPRIS ``OpenUri``): all untrusted. Only absolute local paths and
``http``/``https`` addresses get through; a folder is walked for media
files (hidden entries and links to folders are skipped, the number of
files is capped); a playlist is read once (a playlist inside a playlist is
not followed).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from . import model, playlist_io, tags

MAX_OPEN = 5000                       # tracks taken from one "open"
MAX_WALK_DEPTH = 16
MAX_ARGUMENT = 8192
_DIGITS = re.compile(r"(\d+)")


@dataclass
class Opened:
    tracks: list[model.Track] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)        # what was not a media file, with the reason
    truncated: bool = False


def natural_key(name: str) -> list:
    """Sort key that puts ``Track 2`` before ``Track 10``."""
    return [int(part) if part.isdigit() else part.casefold() for part in _DIGITS.split(name)]


def to_location(argument: str, cwd: str | None = None) -> str | None:
    """A command line argument or dropped address as an absolute path or an http(s) address; None if neither."""
    if not isinstance(argument, str) or not argument or len(argument) > MAX_ARGUMENT or "\0" in argument:
        return None
    if playlist_io.is_url(argument):
        return argument if urlsplit(argument).netloc else None
    if argument[:5].lower() == "file:":
        parts = urlsplit(argument)
        if parts.netloc not in ("", "localhost"):
            return None
        path = unquote(parts.path)
        return os.path.normpath(path) if path.startswith("/") else None
    if "://" in argument[:16]:
        return None
    path = os.path.expanduser(argument) if argument.startswith("~") else argument
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(path)


def track_from_file(path: str) -> model.Track:
    """A track with the tags of the file (title, artist, album, length) when they can be read."""
    track = model.Track.from_location(path)
    try:
        found = tags.read_tags(path)
    except OSError:
        return track
    track.title, track.artist, track.album = found.title, found.artist or found.album_artist, found.album
    track.duration_ms = found.duration_ms
    return track


def _walk(folder: str, limit: int) -> tuple[list[str], bool]:
    found: list[str] = []
    stack = [(folder, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                listing = sorted(entries, key=lambda entry: natural_key(entry.name))
        except OSError:
            continue
        folders = []
        for entry in listing:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_WALK_DEPTH:
                        folders.append((entry.path, depth + 1))
                elif entry.is_file() and model.classify(entry.name) in ("audio", "video"):
                    found.append(entry.path)
                    if len(found) >= limit:
                        return found, True
            except OSError:
                continue
        stack.extend(reversed(folders))
    return found, False


def expand(locations: list[str], *, read_tags: bool = True) -> Opened:
    """The tracks behind ``locations`` (absolute paths and http(s) addresses), in order."""
    opened = Opened()

    def add(track: model.Track) -> bool:
        if len(opened.tracks) >= MAX_OPEN:
            opened.truncated = True
            return False
        opened.tracks.append(track)
        return True

    def add_file(path: str) -> bool:
        return add(track_from_file(path) if read_tags else model.Track.from_location(path))

    for location in locations:
        if opened.truncated:
            break
        if playlist_io.is_url(location):
            add(model.Track.from_location(location))
            continue
        if not os.path.isabs(location):
            opened.skipped.append(location)
            continue
        if os.path.isdir(location):
            files, cut = _walk(location, MAX_OPEN - len(opened.tracks))
            for path in files:
                if not add_file(path):
                    break
            opened.truncated = opened.truncated or cut
            if not files:
                opened.skipped.append(location)
            continue
        if not os.path.isfile(location):
            opened.skipped.append(location)
            continue
        kind = model.classify(location)
        if kind == "playlist":
            try:
                entries = playlist_io.load(location)
            except (OSError, playlist_io.PlaylistError):
                opened.skipped.append(location)
                continue
            for entry in entries:
                if playlist_io.is_url(entry.location):
                    track = model.Track.from_location(entry.location)
                elif model.classify(entry.location) in ("audio", "video") and os.path.isfile(entry.location):
                    track = track_from_file(entry.location) if read_tags else model.Track.from_location(entry.location)
                else:
                    continue
                if not track.title and entry.title:
                    track.title = entry.title
                if not track.duration_ms:
                    track.duration_ms = entry.duration_s * 1000
                if not add(track):
                    break
        elif kind in ("audio", "video"):
            add_file(location)
        else:
            # A file with another extension is given to the player all the same: the backend decides.
            add(model.Track.from_location(location))
    return opened
