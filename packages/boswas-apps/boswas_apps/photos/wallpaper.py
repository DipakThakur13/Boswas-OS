"""Making a picture the desktop background (QtGui for re-encoding; see ``desktopcfg`` for the rest).

The desktop shows PNG and JPEG files as they are stored: it does not turn
a picture by its EXIF orientation. So a picture is copied byte for byte
only when it is an upright PNG or JPEG of sensible size; anything else
(WebP, TIFF, SVG, a turned JPEG, a huge picture) is written anew from the
decoded pixels, upright, at most ``MAX_SIDE`` pixels on its long side.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from . import decode, desktopcfg, imageio

MAX_SIDE = 7680
MAX_COPY_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class WallpaperResult:
    file: str             # the copy the desktop now uses
    reloaded: bool        # the running desktop took the change
    note: str             # why not, when it did not


def wallpaper_bytes(path: str, image: QImage, info: decode.ImageInfo) -> tuple[bytes, str]:
    """The content and extension of the wallpaper file for a picture (see the module text)."""
    if (info.format in ("png", "jpeg") and info.orientation == 1 and not info.animated
            and info.file_size <= MAX_COPY_BYTES and max(info.width, info.height) <= MAX_SIDE):
        with open(path, "rb") as handle:
            data = handle.read(MAX_COPY_BYTES + 1)
        if len(data) <= MAX_COPY_BYTES:
            return data, "png" if info.format == "png" else "jpg"
    if max(image.width(), image.height()) > MAX_SIDE:
        image = image.scaled(MAX_SIDE, MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    if image.hasAlphaChannel():
        return imageio.encode(image, "png"), "png"
    return imageio.encode(image, "jpeg", 92), "jpg"


def set_wallpaper(path: str, image: QImage, info: decode.ImageInfo, run=subprocess.run) -> WallpaperResult:
    """Copy the picture, name it in the desktop configuration, ask the desktop to reload.

    ``desktopcfg.ConfigError``, ``imageio.SaveError`` or OSError when the
    copy or the configuration cannot be written. A desktop that does not
    answer is not an error: the result says so.
    """
    content, extension = wallpaper_bytes(path, image, info)
    stored = desktopcfg.store_wallpaper(os.path.basename(path), content, extension)
    desktopcfg.set_wallpaper(str(stored))
    reloaded, note = desktopcfg.reload_compositor(run)
    return WallpaperResult(str(stored), reloaded, note)
