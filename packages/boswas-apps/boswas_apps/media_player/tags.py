"""Tag readers for the music library: ID3v2/ID3v1, Vorbis comments (Ogg, Opus, FLAC), MP4 ``ilst``, RIFF INFO.

Standard library only (no Qt), so that a library scan is fast, gives the
same answer on every machine and can be tested with bytes made by hand.

Every file is foreign input. The readers never trust a length they read:
each size is checked against what is left of its container and of the
file before anything is read, loops over frames, blocks, atoms, pages and
chunks are counted, text fields and pictures have size caps, and nothing
is ever executed or unpickled. A file that breaks a rule gives the fields
found up to that point; ``read_tags`` raises only ``OSError`` (the file
could not be opened or read).

    tags = read_tags(path)            # Tags(title, artist, album, ..., duration_ms, has_picture)
    picture = read_picture(path)      # Picture(mime, data) or None
"""

from __future__ import annotations

import base64
import io
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Callable

MAX_TEXT_BYTES = 64 * 1024            # a text frame, comment or atom bigger than this is not a title
MAX_TEXT_CHARS = 512                  # what is kept of a text field
MAX_PICTURE_BYTES = 16 * 1024 * 1024  # an embedded cover bigger than this is ignored
MAX_UNSYNC_TAG = 32 * 1024 * 1024     # an unsynchronised ID3v2 tag is read whole; beyond this it is skipped
MAX_COMMENT_PACKET = 20 * 1024 * 1024  # the Vorbis comment packet of an Ogg stream (it can carry a cover)
MAX_FRAMES = 2000                     # ID3 frames, Vorbis comments, ilst items looked at per file
MAX_BLOCKS = 256                      # FLAC metadata blocks, RIFF chunks
MAX_ATOMS = 4096                      # MP4 atoms visited per file
MAX_PAGES = 4096                      # Ogg pages read from the start of a file
MAX_DEPTH = 8                         # MP4 atom nesting
_SYNC_SCAN = 256 * 1024               # how far the first MPEG audio frame is looked for
_TAIL_SCAN = 128 * 1024               # how far from the end the last Ogg page is looked for

GENRES = (
    "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge", "Hip-Hop", "Jazz", "Metal", "New Age",
    "Oldies", "Other", "Pop", "R&B", "Rap", "Reggae", "Rock", "Techno", "Industrial", "Alternative", "Ska",
    "Death Metal", "Pranks", "Soundtrack", "Euro-Techno", "Ambient", "Trip-Hop", "Vocal", "Jazz+Funk", "Fusion",
    "Trance", "Classical", "Instrumental", "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise",
    "Alternative Rock", "Bass", "Soul", "Punk", "Space", "Meditative", "Instrumental Pop", "Instrumental Rock",
    "Ethnic", "Gothic", "Darkwave", "Techno-Industrial", "Electronic", "Pop-Folk", "Eurodance", "Dream",
    "Southern Rock", "Comedy", "Cult", "Gangsta", "Top 40", "Christian Rap", "Pop/Funk", "Jungle",
    "Native American", "Cabaret", "New Wave", "Psychedelic", "Rave", "Showtunes", "Trailer", "Lo-Fi", "Tribal",
    "Acid Punk", "Acid Jazz", "Polka", "Retro", "Musical", "Rock & Roll", "Hard Rock", "Folk", "Folk-Rock",
    "National Folk", "Swing", "Fast Fusion", "Bebop", "Latin", "Revival", "Celtic", "Bluegrass", "Avantgarde",
    "Gothic Rock", "Progressive Rock", "Psychedelic Rock", "Symphonic Rock", "Slow Rock", "Big Band", "Chorus",
    "Easy Listening", "Acoustic", "Humour", "Speech", "Chanson", "Opera", "Chamber Music", "Sonata", "Symphony",
    "Booty Bass", "Primus", "Porn Groove", "Satire", "Slow Jam", "Club", "Tango", "Samba", "Folklore", "Ballad",
    "Power Ballad", "Rhythmic Soul", "Freestyle", "Duet", "Punk Rock", "Drum Solo", "A Cappella", "Euro-House",
    "Dance Hall",
)


@dataclass
class Tags:
    """What a file says about itself. Empty strings and zeros mean "not said"."""

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
    kind: str = ""                    # mp3, flac, ogg, opus, mp4, wav or "" (not recognised)


@dataclass
class Picture:
    mime: str                         # image/jpeg, image/png or "" (unknown; look at the bytes)
    data: bytes


class _Bad(Exception):
    """The file broke a rule of its format; what was read so far stands."""


# --- small helpers ---------------------------------------------------------------------------------------

def _read(stream: BinaryIO, count: int) -> bytes:
    if count < 0:
        raise _Bad("negative length")
    data = stream.read(count)
    if len(data) != count:
        raise _Bad("unexpected end of file")
    return data


def _size_of(stream: BinaryIO) -> int:
    here = stream.tell()
    end = stream.seek(0, os.SEEK_END)
    stream.seek(here)
    return end


def clean(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """A tag value made safe to show: no control characters, one line, at most ``limit`` characters."""
    text = text.split("\0", 1)[0] if "\0" in text else text
    out = []
    for character in text:
        code = ord(character)
        if character in "\t\r\n":
            out.append(" ")
        elif code < 0x20 or 0x7F <= code < 0xA0 or 0xD800 <= code <= 0xDFFF or code in (0xFFFE, 0xFFFF):
            continue
        elif 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069:       # bidirectional overrides
            continue
        else:
            out.append(character)
    return " ".join("".join(out).split())[:limit]


def _number(text: str) -> int:
    """The leading number of ``3``, ``03/12`` or ``2021-05-01``; 0 when there is none or it is absurd."""
    digits = ""
    for character in text.strip()[:16]:
        if character.isascii() and character.isdigit():
            digits += character
        else:
            break
    if not digits:
        return 0
    value = int(digits)
    return value if value < 100000 else 0


def _year(text: str) -> int:
    value = _number(text)
    return value if 0 < value <= 9999 else 0


def _genre(text: str) -> str:
    """ID3 genre text: ``(17)``, ``17`` and ``(17)Rock`` name an entry of the ID3v1 list."""
    text = text.strip()
    if text.startswith("(") and ")" in text:
        inside, rest = text[1:].split(")", 1)
        if rest.strip():
            return clean(rest)
        text = inside
    if text.isascii() and text.isdigit() and len(text) <= 3:
        index = int(text)
        return GENRES[index] if index < len(GENRES) else ""
    return clean(text)


def _set(tags: Tags, field: str, value: str) -> None:
    """Fill a text field unless an earlier (more specific) source already did."""
    value = clean(value)
    if value and not getattr(tags, field):
        setattr(tags, field, value)


def _set_number(tags: Tags, field: str, value: int) -> None:
    if value > 0 and not getattr(tags, field):
        setattr(tags, field, value)


def sniff_image(data: bytes) -> str:
    """``image/jpeg``, ``image/png``, ``image/gif``, ``image/webp``, ``image/bmp`` or ``""`` from the first bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return ""


class _Result:
    """What one pass over a file collects."""

    def __init__(self, want_picture: bool) -> None:
        self.tags = Tags()
        self.want_picture = want_picture
        self.picture: Picture | None = None

    def offer_picture(self, load: Callable[[], bytes], size: int, mime: str = "") -> None:
        """A cover of ``size`` bytes exists; ``load`` reads it (called only when a picture is wanted)."""
        if size <= 0 or size > MAX_PICTURE_BYTES:
            return
        self.tags.has_picture = True
        if self.want_picture and self.picture is None:
            data = load()
            kind = sniff_image(data)
            if kind:                          # only bytes that start like an image are handed on
                self.picture = Picture(kind, data)


# --- ID3 -------------------------------------------------------------------------------------------------

def _syncsafe(data: bytes) -> int:
    if any(byte & 0x80 for byte in data):
        raise _Bad("not a syncsafe integer")
    value = 0
    for byte in data:
        value = (value << 7) | byte
    return value


def _decode_id3_text(data: bytes) -> str:
    """The text of an ID3v2 text frame (first byte: the encoding). Several values are joined with ``, ``."""
    if not data:
        return ""
    encoding, body = data[0], data[1:]
    try:
        if encoding == 0:
            text = body.decode("latin-1")
        elif encoding == 1:
            text = body.decode("utf-16") if body[:2] in (b"\xff\xfe", b"\xfe\xff") else body.decode("utf-16-le")
        elif encoding == 2:
            text = body.decode("utf-16-be")
        elif encoding == 3:
            text = body.decode("utf-8")
        else:
            return ""
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    parts = [clean(part) for part in text.split("\0")]
    return ", ".join(part for part in parts if part)


def _split_terminated(data: bytes, encoding: int) -> tuple[bytes, bytes]:
    """Cut ``data`` at the string terminator of an ID3 encoding: (string, rest)."""
    if encoding in (1, 2):
        for index in range(0, len(data) - 1, 2):
            if data[index] == 0 and data[index + 1] == 0:
                return data[:index], data[index + 2:]
        return data, b""
    index = data.find(b"\0")
    return (data, b"") if index < 0 else (data[:index], data[index + 1:])


_ID3_TEXT = {
    b"TIT2": "title", b"TT2": "title", b"TPE1": "artist", b"TP1": "artist", b"TALB": "album", b"TAL": "album",
    b"TPE2": "album_artist", b"TP2": "album_artist",
}
_ID3_NUMBER = {b"TRCK": "track", b"TRK": "track", b"TPOS": "disc", b"TPA": "disc"}
_ID3_YEAR = (b"TDRC", b"TYER", b"TYE", b"TDOR", b"TORY")


def _id3_picture(result: _Result, frame: bytes, body: bytes) -> None:
    if len(body) < 4:
        return
    encoding = body[0]
    if frame == b"PIC":                       # v2.2: three-letter image format, then type and description
        mime = {"JPG": "image/jpeg", "PNG": "image/png"}.get(body[1:4].decode("latin-1").upper(), "")
        rest = body[5:]
    else:
        raw, rest = _split_terminated(body[1:], 0)
        mime = raw.decode("latin-1", "replace").lower()
        if not rest:
            return
        rest = rest[1:]                       # the picture type
    _description, data = _split_terminated(rest, encoding)
    if mime == "-->":                         # a link, not a picture: never followed
        return
    result.offer_picture(lambda: data, len(data), mime)


def _id3_frame(result: _Result, frame: bytes, body: bytes) -> None:
    tags = result.tags
    if frame in _ID3_TEXT:
        _set(tags, _ID3_TEXT[frame], _decode_id3_text(body))
    elif frame in _ID3_NUMBER:
        _set_number(tags, _ID3_NUMBER[frame], _number(_decode_id3_text(body)))
    elif frame in _ID3_YEAR:
        _set_number(tags, "year", _year(_decode_id3_text(body)))
    elif frame in (b"TCON", b"TCO"):
        if not tags.genre:
            tags.genre = _genre(_decode_id3_text(body))
    elif frame in (b"TLEN", b"TLE"):
        text = _decode_id3_text(body).strip()
        if text.isascii() and text.isdigit() and len(text) <= 9:
            value = int(text)
            if 0 < value <= 48 * 3600 * 1000:
                tags.duration_ms = value
    elif frame in (b"APIC", b"PIC"):
        _id3_picture(result, frame, body)


def _id3v2(stream: BinaryIO, result: _Result, file_size: int) -> int:
    """Read an ID3v2 tag at the current position; returns the offset just after it (or the start: no tag)."""
    start = stream.tell()
    header = stream.read(10)
    if len(header) < 10 or header[:3] != b"ID3" or header[3] not in (2, 3, 4) or header[4] == 0xFF:
        stream.seek(start)
        return start
    version, flags = header[3], header[5]
    try:
        size = _syncsafe(header[6:10])
    except _Bad:
        stream.seek(start)
        return start
    end = start + 10 + size + (10 if version == 4 and flags & 0x10 else 0)
    body_end = min(start + 10 + size, file_size)
    try:
        if flags & 0x80 and version < 4:
            # The whole tag is unsynchronised: it has to be read and undone before frames can be found.
            if size <= MAX_UNSYNC_TAG:
                body = _read(stream, body_end - stream.tell()).replace(b"\xff\x00", b"\xff")
                _id3_frames(io.BytesIO(body), result, version, flags, len(body))
        else:
            _id3_frames(stream, result, version, flags, body_end)
    except (_Bad, struct.error):
        pass
    after = min(end, file_size)
    stream.seek(after)
    return after


def _id3_frames(stream: BinaryIO, result: _Result, version: int, flags: int, end: int) -> None:
    if flags & 0x40:                              # extended header
        if version == 3:
            size = struct.unpack(">I", _read(stream, 4))[0]
            skip = size
        elif version == 4:
            size = _syncsafe(_read(stream, 4))
            skip = size - 4
        else:
            return                                # v2.2: the flag means "compressed"; nothing can be read
        if skip < 0 or stream.tell() + skip > end:
            raise _Bad("extended header")
        stream.seek(skip, os.SEEK_CUR)
    id_length = 3 if version == 2 else 4
    for _count in range(MAX_FRAMES):
        position = stream.tell()
        header_size = 6 if version == 2 else 10
        if position + header_size > end:
            return
        header = _read(stream, header_size)
        frame = header[:id_length]
        if frame.strip(b"\0") == b"":             # padding
            return
        if not all(65 <= byte <= 90 or 48 <= byte <= 57 for byte in frame):
            return
        frame_flags = 0
        if version == 2:
            size = int.from_bytes(header[3:6], "big")
        elif version == 3:
            size = struct.unpack(">I", header[4:8])[0]
            frame_flags = header[9]
        else:
            size = _syncsafe(header[4:8])
            frame_flags = header[9]
        data_start = stream.tell()
        if size <= 0 or data_start + size > end:
            return
        wanted = (frame in _ID3_TEXT or frame in _ID3_NUMBER or frame in _ID3_YEAR
                  or frame in (b"TCON", b"TCO", b"TLEN", b"TLE"))
        picture = frame in (b"APIC", b"PIC")
        compressed = (version == 3 and frame_flags & 0xC0) or (version == 4 and frame_flags & 0x0C)
        if compressed:
            pass                                  # compressed or encrypted frames are skipped
        elif wanted and size <= MAX_TEXT_BYTES:
            _id3_frame(result, frame, _frame_body(_read(stream, size), version, frame_flags))
        elif picture and size <= MAX_PICTURE_BYTES + 1024:
            if result.want_picture and result.picture is None:
                _id3_frame(result, frame, _frame_body(_read(stream, size), version, frame_flags))
            elif b"-->" not in _read(stream, min(size, 16)):       # "-->" is a link to a picture: never followed
                result.tags.has_picture = True
        stream.seek(data_start + size)


def _frame_body(data: bytes, version: int, frame_flags: int) -> bytes:
    if version == 4:
        if frame_flags & 0x01 and len(data) >= 4:     # data length indicator
            data = data[4:]
        if frame_flags & 0x02:                        # this frame is unsynchronised
            data = data.replace(b"\xff\x00", b"\xff")
    return data


def _id3v1(stream: BinaryIO, result: _Result, file_size: int) -> int:
    """Read an ID3v1 tag at the end of the file; returns how many bytes it takes (0 or 128)."""
    if file_size < 128:
        return 0
    stream.seek(file_size - 128)
    block = stream.read(128)
    if len(block) != 128 or block[:3] != b"TAG":
        return 0
    tags = result.tags

    def field(start: int, end: int) -> str:
        return block[start:end].split(b"\0", 1)[0].decode("latin-1").strip()

    _set(tags, "title", field(3, 33))
    _set(tags, "artist", field(33, 63))
    _set(tags, "album", field(63, 93))
    _set_number(tags, "year", _year(field(93, 97)))
    if block[125] == 0 and block[126] != 0:
        _set_number(tags, "track", block[126])
    if not tags.genre and block[127] < len(GENRES):
        tags.genre = GENRES[block[127]]
    return 128


# --- MPEG audio (duration only) --------------------------------------------------------------------------

_BITRATES = {
    (1, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mpeg_header(header: bytes) -> tuple[int, int, int, int, int, int] | None:
    """(version id, layer, bitrate, sample rate, frame bytes, samples per frame) of a frame header, or None."""
    if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version_id = (header[1] >> 3) & 3            # 3: MPEG 1, 2: MPEG 2, 0: MPEG 2.5
    layer = 4 - ((header[1] >> 1) & 3)           # 1, 2, 3
    bitrate_index = header[2] >> 4
    rate_index = (header[2] >> 2) & 3
    padding = (header[2] >> 1) & 1
    if version_id == 1 or layer == 4 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    bitrate = _BITRATES[(1 if version_id == 3 else 2, layer)][bitrate_index] * 1000
    sample_rate = _SAMPLE_RATES[version_id][rate_index]
    if layer == 1:
        samples, frame_bytes = 384, (12 * bitrate // sample_rate + padding) * 4
    elif layer == 2 or version_id == 3:
        samples, frame_bytes = 1152, 144 * bitrate // sample_rate + padding
    else:
        samples, frame_bytes = 576, 72 * bitrate // sample_rate + padding
    if frame_bytes < 24:
        return None
    return version_id, layer, bitrate, sample_rate, frame_bytes, samples


def _mpeg_duration(stream: BinaryIO, start: int, audio_end: int) -> int:
    """Milliseconds of the MPEG audio between ``start`` and ``audio_end``; 0 when no frame is found."""
    stream.seek(start)
    window = stream.read(min(_SYNC_SCAN, max(0, audio_end - start)))
    index = 0
    while True:
        index = window.find(b"\xff", index)
        if index < 0 or index + 4 > len(window):
            return 0
        info = _mpeg_header(window[index:index + 4])
        if info is not None:
            _version, _layer, _bitrate, _rate, frame_bytes, _samples = info
            following = window[index + frame_bytes:index + frame_bytes + 4]
            # A second header where this frame says it ends tells a real frame from a chance 0xFF.
            if len(following) < 4 or _mpeg_header(following) is not None:
                break
        index += 1
    version_id, layer, bitrate, sample_rate, frame_bytes, samples = info
    frame = window[index:index + min(frame_bytes, 512)]
    if layer == 3:
        mono = (frame[3] >> 6) == 3
        side = (17 if mono else 32) if version_id == 3 else (9 if mono else 17)
        tag = frame[4 + side:4 + side + 4]
        if tag in (b"Xing", b"Info") and len(frame) >= 4 + side + 12:
            flags = struct.unpack(">I", frame[8 + side:12 + side])[0]
            if flags & 1:
                frames = struct.unpack(">I", frame[12 + side:16 + side])[0]
                if 0 < frames < 1 << 28:
                    return frames * samples * 1000 // sample_rate
        if frame[36:40] == b"VBRI" and len(frame) >= 54:
            frames = struct.unpack(">I", frame[50:54])[0]
            if 0 < frames < 1 << 28:
                return frames * samples * 1000 // sample_rate
    if bitrate <= 0:
        return 0
    return max(0, audio_end - start - index) * 8 * 1000 // bitrate


def _read_mp3(stream: BinaryIO, result: _Result, file_size: int, start: int) -> None:
    result.tags.kind = "mp3"
    tail = _id3v1(stream, result, file_size)
    if not result.tags.duration_ms:
        try:
            result.tags.duration_ms = _mpeg_duration(stream, start, file_size - tail)
        except (_Bad, struct.error, IndexError):
            pass


# --- Vorbis comments, FLAC ---------------------------------------------------------------------------------

_VORBIS_TEXT = {"TITLE": "title", "ARTIST": "artist", "ALBUM": "album", "ALBUMARTIST": "album_artist",
                "ALBUM ARTIST": "album_artist", "GENRE": "genre"}


def _flac_picture(result: _Result, block: bytes) -> None:
    """A FLAC PICTURE block (also what METADATA_BLOCK_PICTURE holds, base64-encoded)."""
    view = io.BytesIO(block)
    try:
        _kind, mime_length = struct.unpack(">II", _read(view, 8))
        if mime_length > 256:
            return
        mime = _read(view, mime_length).decode("ascii", "replace").lower()
        description_length = struct.unpack(">I", _read(view, 4))[0]
        if description_length > len(block):
            return
        view.seek(description_length + 16, os.SEEK_CUR)
        size = struct.unpack(">I", _read(view, 4))[0]
        start = view.tell()
        if mime == "-->" or size > len(block) - start:
            return
    except (_Bad, struct.error):
        return
    result.offer_picture(lambda: block[start:start + size], size, mime)


def _vorbis_comments(data: bytes, result: _Result) -> None:
    """Parse a Vorbis comment block (vendor string, then ``KEY=value`` entries). Stops at the first bad length."""
    tags = result.tags
    total = len(data)
    if total < 8:
        return
    vendor = struct.unpack_from("<I", data, 0)[0]
    position = 4 + vendor
    if vendor > total or position + 4 > total:
        return
    count = struct.unpack_from("<I", data, position)[0]
    position += 4
    for _index in range(min(count, MAX_FRAMES)):
        if position + 4 > total:
            return
        length = struct.unpack_from("<I", data, position)[0]
        position += 4
        if length > total - position:
            return
        key_end = data.find(b"=", position, position + min(length, 64))
        if key_end > position:
            key = data[position:key_end].decode("ascii", "replace").upper()
            value_start, value_length = key_end + 1, position + length - key_end - 1
            if key == "METADATA_BLOCK_PICTURE":
                if 0 < value_length <= MAX_PICTURE_BYTES * 4 // 3 + 1024:
                    if result.want_picture and result.picture is None:
                        try:
                            block = base64.b64decode(data[value_start:value_start + value_length], validate=True)
                        except (ValueError, TypeError):
                            block = b""
                        if block:
                            _flac_picture(result, block)
                    else:
                        tags.has_picture = True
            elif value_length <= MAX_TEXT_BYTES:
                value = data[value_start:value_start + value_length].decode("utf-8", "replace")
                if key in _VORBIS_TEXT:
                    field = _VORBIS_TEXT[key]
                    if field in ("artist", "genre") and getattr(tags, field):
                        joined = f"{getattr(tags, field)}, {clean(value)}"      # several ARTIST= lines
                        setattr(tags, field, joined[:MAX_TEXT_CHARS])
                    else:
                        _set(tags, field, value)
                elif key in ("TRACKNUMBER", "TRACK"):
                    _set_number(tags, "track", _number(value))
                elif key in ("DISCNUMBER", "DISC"):
                    _set_number(tags, "disc", _number(value))
                elif key in ("DATE", "YEAR", "ORIGINALDATE"):
                    _set_number(tags, "year", _year(value))
        position += length


def _read_flac(stream: BinaryIO, result: _Result, file_size: int) -> None:
    result.tags.kind = "flac"
    for _count in range(MAX_BLOCKS):
        header = _read(stream, 4)
        last, kind = bool(header[0] & 0x80), header[0] & 0x7F
        size = int.from_bytes(header[1:4], "big")
        start = stream.tell()
        if start + size > file_size:
            raise _Bad("block beyond the end of the file")
        if kind == 0 and size >= 18:
            info = _read(stream, 18)
            packed = int.from_bytes(info[10:18], "big")
            sample_rate = packed >> 44
            samples = packed & ((1 << 36) - 1)
            if sample_rate > 0 and samples > 0:
                result.tags.duration_ms = samples * 1000 // sample_rate
        elif kind == 4:
            if size <= MAX_COMMENT_PACKET:
                _vorbis_comments(_read(stream, size), result)
        elif kind == 6:
            if size <= MAX_PICTURE_BYTES + 1024:
                if result.want_picture and result.picture is None:
                    _flac_picture(result, _read(stream, size))
                else:
                    result.tags.has_picture = True
        stream.seek(start + size)
        if last:
            return


# --- Ogg (Vorbis, Opus, FLAC in Ogg) -------------------------------------------------------------------------

def _ogg_packets(stream: BinaryIO, wanted: int, limit: int) -> tuple[list[bytes], int]:
    """The first ``wanted`` packets of the first logical stream, and that stream's serial number."""
    packets: list[bytes] = []
    current = bytearray()
    serial: int | None = None
    for _count in range(MAX_PAGES):
        header = stream.read(27)
        if len(header) < 27 or header[:4] != b"OggS" or header[4] != 0:
            break
        page_serial = struct.unpack_from("<I", header, 14)[0]
        segments = _read(stream, header[26])
        body_size = sum(segments)
        if serial is None:
            serial = page_serial
        if page_serial != serial:
            stream.seek(body_size, os.SEEK_CUR)
            continue
        body = _read(stream, body_size)
        offset = 0
        for lacing in segments:
            if len(current) + lacing > limit:
                raise _Bad("packet too large")
            current += body[offset:offset + lacing]
            offset += lacing
            if lacing < 255:
                packets.append(bytes(current))
                current = bytearray()
                if len(packets) >= wanted:
                    return packets, serial
    return packets, serial if serial is not None else -1


def _ogg_last_granule(stream: BinaryIO, file_size: int, serial: int) -> int:
    """The granule position of the last page of the stream ``serial``; -1 when none is found."""
    start = max(0, file_size - _TAIL_SCAN)
    stream.seek(start)
    tail = stream.read(file_size - start)
    index = len(tail)
    for _count in range(MAX_PAGES):
        index = tail.rfind(b"OggS", 0, index)
        if index < 0:
            return -1
        if index + 27 <= len(tail) and tail[index + 4] == 0:
            granule, page_serial = struct.unpack_from("<qI", tail, index + 6)
            if page_serial == serial and granule >= 0:
                return granule
    return -1


def _read_ogg(stream: BinaryIO, result: _Result, file_size: int) -> None:
    tags = result.tags
    tags.kind = "ogg"
    packets, serial = _ogg_packets(stream, 2, MAX_COMMENT_PACKET)
    if not packets:
        return
    first = packets[0]
    sample_rate, pre_skip = 0, 0
    comment: bytes | None = None
    if first[:7] == b"\x01vorbis" and len(first) >= 16:
        sample_rate = struct.unpack_from("<I", first, 12)[0]
        if len(packets) > 1 and packets[1][:7] == b"\x03vorbis":
            comment = packets[1][7:]
    elif first[:8] == b"OpusHead" and len(first) >= 12:
        tags.kind = "opus"
        pre_skip = struct.unpack_from("<H", first, 10)[0]
        sample_rate = 48000
        if len(packets) > 1 and packets[1][:8] == b"OpusTags":
            comment = packets[1][8:]
    elif first[:5] == b"\x7fFLAC" and len(first) >= 13 + 4 + 18:
        info = first[13 + 4:13 + 4 + 18]
        packed = int.from_bytes(info[10:18], "big")
        sample_rate = packed >> 44
        if len(packets) > 1 and len(packets[1]) > 4 and packets[1][0] & 0x7F == 4:
            comment = packets[1][4:]
    if comment is not None:
        _vorbis_comments(comment, result)
    if 0 < sample_rate <= 768000 and serial >= 0:
        granule = _ogg_last_granule(stream, file_size, serial)
        if granule > pre_skip:
            tags.duration_ms = (granule - pre_skip) * 1000 // sample_rate


# --- MP4 / M4A / MOV -----------------------------------------------------------------------------------------

_MP4_TEXT = {b"\xa9nam": "title", b"\xa9ART": "artist", b"\xa9alb": "album", b"aART": "album_artist",
             b"\xa9gen": "genre"}
_MP4_CONTAINERS = (b"moov", b"udta", b"meta", b"ilst", b"trak", b"mdia")


class _Mp4:
    def __init__(self, stream: BinaryIO, result: _Result, file_size: int) -> None:
        self.stream, self.result, self.file_size = stream, result, file_size
        self.atoms = 0

    def atoms_in(self, start: int, end: int):
        """Yield (type, body start, body end) of the atoms between ``start`` and ``end``."""
        position = start
        while position + 8 <= end:
            self.atoms += 1
            if self.atoms > MAX_ATOMS:
                raise _Bad("too many atoms")
            self.stream.seek(position)
            header = _read(self.stream, 8)
            size, kind = struct.unpack(">I4s", header)
            body = position + 8
            if size == 1:
                size = struct.unpack(">Q", _read(self.stream, 8))[0]
                body += 8
            elif size == 0:
                size = end - position
            if size < body - position or position + size > end:
                return
            yield kind, body, position + size
            position += size

    def walk(self, start: int, end: int, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            return
        for kind, body, atom_end in self.atoms_in(start, end):
            if kind == b"mvhd":
                self.mvhd(body, atom_end)
            elif kind == b"ilst":
                self.ilst(body, atom_end)
            elif kind == b"meta":
                # ISO files: a full atom (4 bytes of version and flags); QuickTime: children at once.
                self.stream.seek(body)
                probe = self.stream.read(8)
                skip = 0 if len(probe) == 8 and probe[4:8] in (b"hdlr", b"ilst", b"keys", b"mhdr") else 4
                self.walk(body + skip, atom_end, depth + 1)
            elif kind in (b"moov", b"udta"):
                self.walk(body, atom_end, depth + 1)
            elif kind in _MP4_TEXT or kind == b"\xa9day":
                self.quicktime_text(kind, body, atom_end)

    def quicktime_text(self, kind: bytes, body: int, end: int) -> None:
        """A QuickTime user data text atom (``moov/udta/\xa9nam``): 16-bit length, language code, text."""
        if not 4 < end - body <= MAX_TEXT_BYTES:
            return
        self.stream.seek(body)
        data = _read(self.stream, end - body)
        length = struct.unpack_from(">H", data, 0)[0]
        if length > len(data) - 4:
            return
        raw = data[4:4 + length]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("mac_roman", "replace")
        if kind == b"\xa9day":
            _set_number(self.result.tags, "year", _year(text))
        else:
            _set(self.result.tags, _MP4_TEXT[kind], text)

    def mvhd(self, body: int, end: int) -> None:
        self.stream.seek(body)
        data = self.stream.read(min(32, end - body))
        if len(data) < 20:
            return
        if data[0] == 1 and len(data) >= 32:
            timescale, duration = struct.unpack_from(">IQ", data, 20)
        else:
            timescale, duration = struct.unpack_from(">II", data, 12)
            if duration == 0xFFFFFFFF:
                return
        if timescale > 0 and 0 < duration < 1 << 48:
            milliseconds = duration * 1000 // timescale
            if milliseconds <= 30 * 24 * 3600 * 1000:
                self.result.tags.duration_ms = milliseconds

    def data_atom(self, body: int, end: int) -> tuple[int, int, int] | None:
        """(type indicator, payload start, payload size) of the first ``data`` atom of an item."""
        for kind, start, atom_end in self.atoms_in(body, end):
            if kind == b"data" and atom_end - start >= 8:
                self.stream.seek(start)
                indicator = struct.unpack(">I", _read(self.stream, 4))[0] & 0xFFFFFF
                return indicator, start + 8, atom_end - start - 8
        return None

    def ilst(self, body: int, end: int) -> None:
        tags = self.result.tags
        for count, (kind, start, atom_end) in enumerate(self.atoms_in(body, end)):
            if count >= MAX_FRAMES:
                return
            found = self.data_atom(start, atom_end)
            if found is None:
                continue
            indicator, payload, size = found
            if kind == b"covr":
                def load(payload=payload, size=size) -> bytes:
                    self.stream.seek(payload)
                    return _read(self.stream, size)
                self.result.offer_picture(load, size, {13: "image/jpeg", 14: "image/png"}.get(indicator, ""))
                continue
            if size > MAX_TEXT_BYTES:
                continue
            self.stream.seek(payload)
            data = _read(self.stream, size)
            if kind in _MP4_TEXT or kind == b"\xa9day":
                try:
                    text = data.decode("utf-16-be" if indicator == 2 else "utf-8")
                except UnicodeDecodeError:
                    text = data.decode("latin-1")
                if kind == b"\xa9day":
                    _set_number(tags, "year", _year(text))
                else:
                    _set(tags, _MP4_TEXT[kind], text)
            elif kind == b"gnre" and len(data) >= 2:
                index = struct.unpack(">H", data[:2])[0] - 1
                if not tags.genre and 0 <= index < len(GENRES):
                    tags.genre = GENRES[index]
            elif kind in (b"trkn", b"disk") and len(data) >= 4:
                _set_number(tags, "track" if kind == b"trkn" else "disc", struct.unpack(">H", data[2:4])[0])


def _read_mp4(stream: BinaryIO, result: _Result, file_size: int) -> None:
    result.tags.kind = "mp4"
    _Mp4(stream, result, file_size).walk(0, file_size)


# --- RIFF WAVE -----------------------------------------------------------------------------------------------

_INFO = {b"INAM": "title", b"IART": "artist", b"IPRD": "album", b"IGNR": "genre"}


def _read_wav(stream: BinaryIO, result: _Result, file_size: int) -> None:
    tags = result.tags
    tags.kind = "wav"
    stream.seek(12)
    byte_rate = 0
    data_size = 0
    for _count in range(MAX_BLOCKS):
        header = stream.read(8)
        if len(header) < 8:
            break
        kind, size = struct.unpack("<4sI", header)
        start = stream.tell()
        available = file_size - start
        if kind == b"data":
            data_size = min(size, available)      # a recorder that was cut off leaves a size that is too big
        elif size > available:
            break
        elif kind == b"fmt " and size >= 16:
            byte_rate = struct.unpack_from("<I", _read(stream, 16), 8)[0]
        elif kind == b"LIST" and 4 <= size <= 1024 * 1024:
            body = _read(stream, size)
            if body[:4] == b"INFO":
                position = 4
                for _item in range(MAX_FRAMES):
                    if position + 8 > len(body):
                        break
                    item, length = struct.unpack_from("<4sI", body, position)
                    position += 8
                    if length > len(body) - position:
                        break
                    if length <= MAX_TEXT_BYTES:
                        raw = body[position:position + length].split(b"\0", 1)[0]
                        try:
                            text = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            text = raw.decode("latin-1")
                        if item in _INFO:
                            _set(tags, _INFO[item], text)
                        elif item == b"ICRD":
                            _set_number(tags, "year", _year(text))
                        elif item in (b"ITRK", b"IPRT"):
                            _set_number(tags, "track", _number(text))
                    position += length + (length & 1)
        elif kind in (b"id3 ", b"ID3 "):
            _id3v2(stream, result, start + size)
        stream.seek(start + min(size, available) + (size & 1))
    if byte_rate > 0 and data_size > 0:
        tags.duration_ms = data_size * 1000 // byte_rate


# --- entry points ----------------------------------------------------------------------------------------------

_EXTENSIONS = {".mp3": "mp3", ".mp2": "mp3", ".flac": "flac", ".ogg": "ogg", ".oga": "ogg", ".opus": "ogg",
               ".m4a": "mp4", ".m4b": "mp4", ".mp4": "mp4", ".m4v": "mp4", ".mov": "mp4", ".3gp": "mp4",
               ".wav": "wav", ".wave": "wav"}


def _scan(path: str | os.PathLike, want_picture: bool) -> _Result:
    result = _Result(want_picture)
    with open(path, "rb") as stream:
        file_size = _size_of(stream)
        try:
            after_id3 = _id3v2(stream, result, file_size)
            head = stream.read(12)
            stream.seek(after_id3)
            extension = os.path.splitext(os.fspath(path))[1].lower()
            if head[:4] == b"fLaC":
                stream.seek(after_id3 + 4)
                _read_flac(stream, result, file_size)
            elif head[:4] == b"OggS":
                _read_ogg(stream, result, file_size)
            elif head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip") and after_id3 == 0:
                _read_mp4(stream, result, file_size)
            elif head[:4] == b"RIFF" and head[8:12] == b"WAVE":
                _read_wav(stream, result, file_size)
            elif after_id3 > 0 or _EXTENSIONS.get(extension) == "mp3" or _mpeg_header(head[:4]) is not None:
                _read_mp3(stream, result, file_size, after_id3)
        except (_Bad, struct.error, IndexError, ValueError, OverflowError, MemoryError):
            pass                                  # a broken file: what was found so far stands
    return result


def read_tags(path: str | os.PathLike) -> Tags:
    """The tags of an audio (or MP4 video) file. Raises ``OSError`` when the file cannot be read."""
    return _scan(path, False).tags


def read_picture(path: str | os.PathLike) -> Picture | None:
    """The first embedded cover of a file, or None. Raises ``OSError`` when the file cannot be read."""
    return _scan(path, True).picture
