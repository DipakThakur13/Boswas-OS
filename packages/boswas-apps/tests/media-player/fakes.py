"""Test doubles and file builders for the Media Player tests.

``FakePlayer`` implements ``PlayerBackend`` without Qt Multimedia: nothing
is decoded and no device is opened; tests move time with ``advance`` and
end a track with ``finish``. ``FakeGrabber`` answers thumbnail requests
with a generated picture. The ``make_*`` functions build small media files
by hand (a WAV with INFO tags, an MP3 with ID3 tags, FLAC, Ogg, MP4) so
that no encoder is needed.
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QImage, QPainter

from boswas_apps.media_player.backend import FrameGrabber, PlayerBackend


def picture(width: int = 320, height: int = 180, hue: int = 200) -> QImage:
    """A generated picture (a colour field with a lighter band) standing in for a video frame or a cover."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor.fromHsv(hue % 360, 140, 150))
    painter = QPainter(image)
    painter.fillRect(0, height // 3, width, height // 3, QColor.fromHsv((hue + 30) % 360, 110, 210))
    painter.end()
    return image


class FakePlayer(PlayerBackend):
    """A player that plays nothing. Locations in ``broken`` fail to load; ``durations`` gives lengths."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.broken: set[str] = set()
        self.durations: dict[str, int] = {}
        self.default_duration = 180_000
        self.videos: set[str] = set()
        self.metadata: dict[str, dict] = {}
        self.tracks: dict[str, list[str]] = {"audio": [], "video": [], "subtitle": []}
        self.active = {"audio": 0, "video": 0, "subtitle": -1}
        self.devices: list[tuple[str, str]] = [("speakers", "Speakers"), ("headset", "Headset")]
        self.device = ""
        self.source = ""
        self.sources: list[str] = []
        self.volume = 1.0
        self.muted = False
        self.rate = 1.0
        self._state = "stopped"
        self._position = 0
        self._duration = 0
        self._video = False
        self._loaded = False
        self.is_available = True
        self.closed = False

    # --- the interface ---
    def available(self) -> bool:
        return self.is_available

    def unavailable_reason(self) -> str:
        return "" if self.is_available else "The multimedia components of the system are not installed."

    def set_source(self, location: str) -> None:
        self.source = location
        self._position = 0
        self._loaded = False
        self._set_state("stopped")
        if not location:
            self._duration = 0
            self._set_video(False)
            self.status_changed.emit("none")
            return
        self.sources.append(location)
        self.status_changed.emit("loading")
        # As with the real player, the result of loading arrives later, from the event loop.
        QTimer.singleShot(0, lambda: self._finish_loading(location))

    def _finish_loading(self, location: str) -> None:
        if location != self.source:
            return
        if location in self.broken or location.endswith(".broken"):
            self._set_state("stopped")
            self.status_changed.emit("invalid")
            self.error.emit("The file is damaged or in a format that is not supported.")
            return
        self._loaded = True
        self._duration = self.durations.get(location, self.default_duration)
        self.duration_changed.emit(self._duration)
        self.seekable_changed.emit(True)
        video = location in self.videos or location.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".avi"))
        self._set_video(video)
        if location in self.metadata:
            self.metadata_changed.emit(dict(self.metadata[location]))
        self.tracks_changed.emit()
        self.status_changed.emit("loaded")
        if video:
            self.frame_changed.emit(picture(640, 360, sum(location.encode()) % 360))

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    def _set_video(self, video: bool) -> None:
        if video != self._video:
            self._video = video
            self.video_changed.emit(video)
            if not video:
                self.frame_changed.emit(None)

    def play(self) -> None:
        if self.source and self.source not in self.broken:
            self._set_state("playing")

    def pause(self) -> None:
        if self._state == "playing":
            self._set_state("paused")

    def stop(self) -> None:
        self._position = 0
        self._set_state("stopped")

    def state(self) -> str:
        return self._state

    def position(self) -> int:
        return self._position

    def duration(self) -> int:
        return self._duration

    def seekable(self) -> bool:
        return self._loaded

    def has_video(self) -> bool:
        return self._video

    def seek(self, position_ms: int) -> None:
        self._position = max(0, min(int(position_ms), self._duration or int(position_ms)))
        self.position_changed.emit(self._position)

    def set_volume(self, volume: float) -> None:
        self.volume = volume

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    def set_rate(self, rate: float) -> None:
        self.rate = rate

    def audio_tracks(self) -> list[str]:
        return list(self.tracks["audio"])

    def video_tracks(self) -> list[str]:
        return list(self.tracks["video"])

    def subtitle_tracks(self) -> list[str]:
        return list(self.tracks["subtitle"])

    def active_track(self, kind: str) -> int:
        return self.active[kind]

    def set_active_track(self, kind: str, index: int) -> None:
        self.active[kind] = index

    def audio_devices(self) -> list[tuple[str, str]]:
        return list(self.devices)

    def set_audio_device(self, device_id: str) -> None:
        self.device = device_id

    def shutdown(self) -> None:
        self.closed = True
        self._state = "stopped"

    # --- what a test does to it ---
    def advance(self, milliseconds: int) -> None:
        """Let ``milliseconds`` of the media play; reaching the end reports "ended"."""
        if self._state != "playing":
            return
        self._position = min(self._position + milliseconds, self._duration)
        self.position_changed.emit(self._position)
        if self._duration and self._position >= self._duration:
            self.finish()

    def finish(self) -> None:
        """The media played to its end."""
        self._position = self._duration
        self._set_state("stopped")
        self.status_changed.emit("ended")

    def fail(self, text: str = "The network stream could not be reached.") -> None:
        self._set_state("stopped")
        self.error.emit(text)


class FakeGrabber(FrameGrabber):
    """Answers every request on the next turn of the event loop; paths in ``fails`` get no picture."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.requests: list[str] = []
        self.fails: set[str] = set()
        self.enabled = True

    def grab(self, path, done) -> None:
        self.requests.append(path)
        image = None if (path in self.fails or not self.enabled) else picture(320, 180, sum(path.encode()) % 360)
        QTimer.singleShot(0, lambda: done(image, 0 if image is None else 95_000))


# --- media files made by hand ----------------------------------------------------------------------------

def jpeg_bytes(width: int = 8, height: int = 8) -> bytes:
    """A real (tiny) JPEG picture."""
    from PySide6.QtCore import QBuffer, QIODevice
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    picture(width, height).save(buffer, "JPEG", 80)
    return bytes(buffer.data())


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    picture(width, height).save(buffer, "PNG")
    return bytes(buffer.data())


def make_wav(path: Path, seconds: float = 1.0, *, info: dict[str, str] | None = None, rate: int = 8000) -> Path:
    """A PCM WAV file (silence) with optional LIST/INFO tags (INAM, IART, IPRD, ICRD, IGNR, ITRK)."""
    frames = int(seconds * rate)
    data = b"\0\0" * frames
    chunks = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    if info:
        body = b"INFO"
        for key, value in info.items():
            raw = value.encode("utf-8") + b"\0"
            body += key.encode("ascii") + struct.pack("<I", len(raw)) + raw + (b"\0" if len(raw) & 1 else b"")
        chunks += b"LIST" + struct.pack("<I", len(body)) + body
    chunks += b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)
    return path


def syncsafe(value: int) -> bytes:
    return bytes(((value >> shift) & 0x7F) for shift in (21, 14, 7, 0))


def id3_frame(frame: str, body: bytes, version: int = 3, flags: bytes = b"\0\0") -> bytes:
    if version == 2:
        return frame.encode("ascii") + len(body).to_bytes(3, "big") + body
    size = syncsafe(len(body)) if version == 4 else struct.pack(">I", len(body))
    return frame.encode("ascii") + size + flags + body


def id3_text(frame: str, text: str, version: int = 3, encoding: int = 3) -> bytes:
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}[encoding]
    return id3_frame(frame, bytes([encoding]) + text.encode(codec), version)


def id3_tag(frames: bytes, version: int = 3, flags: int = 0) -> bytes:
    return b"ID3" + bytes([version, 0, flags]) + syncsafe(len(frames)) + frames


def apic(image: bytes, mime: str = "image/jpeg") -> bytes:
    return b"\0" + mime.encode("ascii") + b"\0" + b"\x03" + b"cover\0" + image


def mpeg_frames(count: int = 40) -> bytes:
    """``count`` silent MPEG-1 Layer III frames (128 kbit/s, 44.1 kHz: 417 bytes and 1152 samples each)."""
    header = b"\xff\xfb\x90\x00"
    return (header + b"\0" * 413) * count


def make_mp3(path: Path, frames: bytes = b"", *, audio_frames: int = 40, id3v1: bytes = b"") -> Path:
    path.write_bytes(frames + mpeg_frames(audio_frames) + id3v1)
    return path


def id3v1(title: str = "", artist: str = "", album: str = "", year: str = "", track: int = 0, genre: int = 255) -> bytes:
    def field(text: str, size: int) -> bytes:
        return text.encode("latin-1")[:size].ljust(size, b"\0")
    comment = b"\0" * 28 + b"\0" + bytes([track]) if track else b"\0" * 30
    return b"TAG" + field(title, 30) + field(artist, 30) + field(album, 30) + field(year, 4) + comment + bytes([genre])


def vorbis_comment(comments: list[str], vendor: str = "test") -> bytes:
    raw = vendor.encode("utf-8")
    data = struct.pack("<I", len(raw)) + raw + struct.pack("<I", len(comments))
    for comment in comments:
        encoded = comment.encode("utf-8")
        data += struct.pack("<I", len(encoded)) + encoded
    return data


def flac_picture(image: bytes, mime: str = "image/jpeg") -> bytes:
    return (struct.pack(">I", 3) + struct.pack(">I", len(mime)) + mime.encode("ascii") + struct.pack(">I", 0)
            + struct.pack(">IIII", 8, 8, 24, 0) + struct.pack(">I", len(image)) + image)


def flac_block(kind: int, body: bytes, last: bool = False) -> bytes:
    return bytes([kind | (0x80 if last else 0)]) + len(body).to_bytes(3, "big") + body


def flac_streaminfo(sample_rate: int = 44100, samples: int = 44100 * 3) -> bytes:
    packed = (sample_rate << 44) | (1 << 41) | (15 << 36) | samples       # 2 channels, 16 bits
    return struct.pack(">HH", 4096, 4096) + b"\0" * 6 + packed.to_bytes(8, "big") + b"\0" * 16


def make_flac(path: Path, comments: list[str], *, cover: bytes | None = None, sample_rate: int = 44100,
              samples: int = 44100 * 3) -> Path:
    blocks = [flac_block(0, flac_streaminfo(sample_rate, samples)), flac_block(4, vorbis_comment(comments))]
    if cover is not None:
        blocks.append(flac_block(6, flac_picture(cover)))
    blocks.append(flac_block(1, b"\0" * 16, last=True))
    path.write_bytes(b"fLaC" + b"".join(blocks) + b"\xff\xf8" + b"\0" * 64)
    return path


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _bit in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def ogg_page(packets: list[bytes], *, serial: int = 7, sequence: int = 0, granule: int = 0, flags: int = 0) -> bytes:
    """One Ogg page holding whole packets (each shorter than 255 * 255 bytes)."""
    segments = b""
    body = b""
    for packet in packets:
        length = len(packet)
        segments += b"\xff" * (length // 255) + bytes([length % 255])
        body += packet
    header = (b"OggS" + bytes([0, flags]) + struct.pack("<qIII", granule, serial, sequence, 0)
              + bytes([len(segments)]) + segments)
    crc = _ogg_crc(header + body)
    return header[:22] + struct.pack("<I", crc) + header[26:] + body


def make_ogg_vorbis(path: Path, comments: list[str], *, sample_rate: int = 44100, samples: int = 44100 * 4) -> Path:
    identification = b"\x01vorbis" + struct.pack("<IBIiiiB", 0, 2, sample_rate, 0, 128000, 0, 0xB8) + b"\x01"
    comment = b"\x03vorbis" + vorbis_comment(comments) + b"\x01"
    path.write_bytes(ogg_page([identification], flags=2) + ogg_page([comment], sequence=1)
                     + ogg_page([b"\0" * 40], sequence=2, granule=samples, flags=4))
    return path


def make_opus(path: Path, comments: list[str], *, pre_skip: int = 312, samples: int = 48000 * 5) -> Path:
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 2, pre_skip, 44100, 0, 0)
    tags = b"OpusTags" + vorbis_comment(comments)
    path.write_bytes(ogg_page([head], flags=2) + ogg_page([tags], sequence=1)
                     + ogg_page([b"\0" * 40], sequence=2, granule=samples + pre_skip, flags=4))
    return path


def atom(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


def mp4_data(payload: bytes, indicator: int = 1) -> bytes:
    return atom(b"data", struct.pack(">II", indicator, 0) + payload)


def make_mp4(path: Path, items: dict[bytes, bytes], *, timescale: int = 1000, duration: int = 5000,
             video: bool = False) -> Path:
    """An MP4 file with an ``ilst`` of ``items`` (atom type -> the bytes of its ``data`` atom) and no media."""
    mvhd = atom(b"mvhd", struct.pack(">IIIII", 0, 0, 0, timescale, duration) + b"\0" * 80)
    ilst = atom(b"ilst", b"".join(atom(kind, data) for kind, data in items.items()))
    hdlr = atom(b"hdlr", b"\0" * 8 + b"mdir" + b"appl" + b"\0" * 9)
    meta = atom(b"meta", b"\0\0\0\0" + hdlr + ilst)
    moov = atom(b"moov", mvhd + atom(b"udta", meta))
    brand = b"isom" if video else b"M4A "
    path.write_bytes(atom(b"ftyp", brand + b"\0\0\0\0" + brand) + moov + atom(b"mdat", b"\0" * 32))
    return path


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")

