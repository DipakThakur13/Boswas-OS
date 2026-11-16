"""Where captures go, what they are called, the settings, and the free-space rules of Camera (no Qt).

Photos are saved to ``<Pictures>/Camera Roll`` and videos to
``<Videos>/Camera Roll`` unless the user chose other folders; the
Pictures and Videos folders come from ``user-dirs.dirs`` (xdg-user-dirs).
File names carry the time of the capture: ``IMG_20261116_143005.jpg``,
``VID_20261116_143005.mp4``; a second capture in the same second gets
``_2``. Nothing is ever overwritten: the file is created exclusively.

A recording does not start when less than ``MIN_FREE_VIDEO`` (500 MiB) is
free where videos go, and a running recording is ended when less than
``STOP_FREE_VIDEO`` (100 MiB) is left. A photo needs ``MIN_FREE_PHOTO``
(20 MiB).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MIB = 1024 * 1024
MIN_FREE_VIDEO = 500 * MIB
STOP_FREE_VIDEO = 100 * MIB
MIN_FREE_PHOTO = 20 * MIB
ROLL = "Camera Roll"
MODES = ("photo", "video")
TIMERS = (0, 3, 5, 10)
QUALITIES = ("low", "normal", "high", "very-high")
QUALITY_NAMES = {"low": "Low (smaller files)", "normal": "Normal", "high": "High", "very-high": "Very high"}
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png")
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".avi")
_MAX_SETTINGS_BYTES = 256 * 1024
_MAX_CAMERAS = 32


def user_dir(name: str) -> Path:
    """The user's ``PICTURES`` or ``VIDEOS`` folder from ``user-dirs.dirs``; else ~/Pictures, ~/Videos."""
    home = Path.home()
    default = home / {"PICTURES": "Pictures", "VIDEOS": "Videos"}.get(name, name.title())
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
        if not line.startswith(key + "="):
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if value.startswith("$HOME/"):
            return home / value[6:]
        if value.startswith("/"):
            return Path(value)
    return default


def default_photo_dir() -> Path:
    return user_dir("PICTURES") / ROLL


def default_video_dir() -> Path:
    return user_dir("VIDEOS") / ROLL


def _folder(value: object) -> str:
    """A stored folder that is still acceptable (an absolute path), else ""."""
    if isinstance(value, str) and os.path.isabs(value) and "\0" not in value and len(value) < 4096:
        return os.path.normpath(value)
    return ""


@dataclass
class Settings:
    """What the user chose. An empty folder means the default Camera Roll."""

    photo_dir: str = ""
    video_dir: str = ""
    default_mode: str = "photo"
    timer: int = 0                    # seconds: 0, 3, 5 or 10
    mirror: bool = True
    grid: bool = False
    quality: str = "high"
    sounds: bool = True
    camera: str = ""                  # the camera used last
    photo_sizes: dict = field(default_factory=dict)     # camera id -> [width, height]
    video_sizes: dict = field(default_factory=dict)     # camera id -> [width, height, frames per second]

    def photo_folder(self) -> Path:
        return Path(self.photo_dir) if self.photo_dir else default_photo_dir()

    def video_folder(self) -> Path:
        return Path(self.video_dir) if self.video_dir else default_video_dir()

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Settings":
        settings = cls()
        try:
            if os.path.getsize(path) > _MAX_SETTINGS_BYTES:
                return settings
            with open(path, "rb") as stream:
                data = json.loads(stream.read(_MAX_SETTINGS_BYTES + 1).decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return settings
        if not isinstance(data, dict):
            return settings
        settings.photo_dir = _folder(data.get("photo_dir"))
        settings.video_dir = _folder(data.get("video_dir"))
        if data.get("default_mode") in MODES:
            settings.default_mode = data["default_mode"]
        if data.get("timer") in TIMERS and not isinstance(data.get("timer"), bool):
            settings.timer = data["timer"]
        if data.get("quality") in QUALITIES:
            settings.quality = data["quality"]
        for name in ("mirror", "grid", "sounds"):
            if isinstance(data.get(name), bool):
                setattr(settings, name, data[name])
        if isinstance(data.get("camera"), str) and len(data["camera"]) <= 512:
            settings.camera = data["camera"]
        for name, length in (("photo_sizes", 2), ("video_sizes", 3)):
            raw = data.get(name)
            if isinstance(raw, dict):
                kept = {}
                for camera, size in list(raw.items())[:_MAX_CAMERAS]:
                    if (isinstance(camera, str) and len(camera) <= 512 and isinstance(size, list)
                            and len(size) == length and all(isinstance(n, int) and not isinstance(n, bool)
                                                            and 0 < n <= 100000 for n in size)):
                        kept[camera] = list(size)
                setattr(settings, name, kept)
        return settings

    def save(self, path: str | os.PathLike) -> None:
        """Write the settings atomically (mode 0600). Raises ``OSError``."""
        target = Path(path)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(asdict(self), stream, ensure_ascii=False, indent=1)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def capture_name(kind: str, extension: str, when: float | None = None) -> str:
    """``IMG_YYYYMMDD_HHMMSS.jpg`` or ``VID_...`` for a capture taken at ``when`` (local time)."""
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when))
    return f"{'IMG' if kind == 'photo' else 'VID'}_{stamp}{extension}"


def reserve(folder: str | os.PathLike, kind: str, extension: str, when: float | None = None) -> Path:
    """Create the folder and an empty, new file for a capture in it; returns the file. Raises ``OSError``.

    The file is created exclusively, so two captures (or two programs) never
    get the same name and nothing that exists is overwritten.
    """
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    name = capture_name(kind, extension, when)
    stem, suffix = os.path.splitext(name)
    for number in range(1, 1000):
        candidate = directory / (name if number == 1 else f"{stem}_{number}{suffix}")
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate
    raise FileExistsError(f"too many captures named {name}")


def free_bytes(folder: str | os.PathLike) -> int:
    """Free space where ``folder`` is (or will be made). Raises ``OSError``."""
    path = Path(folder)
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def format_size(count: int) -> str:
    if count >= 1024 * MIB:
        return f"{count / (1024 * MIB):.1f} GiB"
    return f"{count // MIB} MiB"


def space_problem(folder: str | os.PathLike, kind: str) -> str | None:
    """None when a capture of ``kind`` may start; else a sentence that says why not."""
    needed = MIN_FREE_VIDEO if kind == "video" else MIN_FREE_PHOTO
    try:
        free = free_bytes(folder)
    except OSError as error:
        return f"The folder {folder} cannot be used: {error.strerror or error}."
    if free >= needed:
        return None
    what = "A recording needs" if kind == "video" else "A photo needs"
    return (f"The disk is almost full: {format_size(free)} free in {folder}. {what} at least "
            f"{format_size(needed)}.")


def latest_capture(folders: list[str | os.PathLike]) -> str | None:
    """The newest photo or video in ``folders`` (not below them), or None."""
    newest: tuple[float, str] | None = None
    for folder in folders:
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or not entry.name.lower().endswith(PHOTO_EXTENSIONS
                                                                                     + VIDEO_EXTENSIONS):
                        continue
                    try:
                        if not entry.is_file() or entry.stat().st_size == 0:
                            continue
                        stamp = entry.stat().st_mtime
                    except OSError:
                        continue
                    if newest is None or stamp > newest[0]:
                        newest = (stamp, entry.path)
        except OSError:
            continue
    return newest[1] if newest else None


def is_video(path: str) -> bool:
    return path.lower().endswith(VIDEO_EXTENSIONS)


def format_elapsed(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds)) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
