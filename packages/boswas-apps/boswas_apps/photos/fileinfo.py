"""The facts the file info pane shows about a picture (standard library only).

``describe`` turns what is known about a file (its place and size on
disk, the header of the picture, its EXIF) into titled groups of
label-and-value rows, ready to be shown. Rows without a value are left
out, so the pane never shows an empty "Lens:".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from . import exif as exif_module
from . import formats
from .fileutil import format_size


@dataclass(frozen=True)
class Group:
    title: str
    rows: tuple[tuple[str, str], ...]


def format_time(stamp: float | None) -> str:
    """A time for people: ``14 November 2026, 09:41``."""
    if stamp is None:
        return ""
    try:
        moment = time.localtime(stamp)
    except (OverflowError, OSError, ValueError):
        return ""
    month = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December")[moment.tm_mon - 1]
    return f"{moment.tm_mday} {month} {moment.tm_year}, {moment.tm_hour:02d}:{moment.tm_min:02d}"


def format_megapixels(width: int, height: int) -> str:
    megapixels = width * height / 1_000_000
    if megapixels >= 10:
        return f"{megapixels:.0f} MP"
    if megapixels >= 0.1:
        return f"{megapixels:.1f} MP"
    return f"{megapixels:.2f} MP"


def format_depth(bits_per_pixel: int | None, has_alpha: bool | None) -> str:
    if not bits_per_pixel:
        return ""
    text = f"{bits_per_pixel} bits per pixel"
    if has_alpha is True:
        text += ", with transparency"
    elif has_alpha is False:
        text += ", no transparency"
    return text


def format_dpi(dpi: tuple[float, float] | None) -> str:
    if dpi is None:
        return ""
    x, y = round(dpi[0]), round(dpi[1])
    return f"{x} dpi" if x == y else f"{x} × {y} dpi"


def describe(path: str, *, width: int = 0, height: int = 0, fmt: str = "", bits_per_pixel: int | None = None,
             has_alpha: bool | None = None, dpi: tuple[float, float] | None = None, frames: int = 1,
             animated: bool = False, vector: bool = False,
             exif: exif_module.Exif | None = None, status: os.stat_result | None = None) -> list[Group]:
    """The groups of the info pane for one picture. ``status`` defaults to ``os.stat(path)``."""
    if status is None:
        try:
            status = os.stat(path)
        except OSError:
            status = None
    file_rows: list[tuple[str, str]] = [("Folder", os.path.dirname(path))]
    if fmt:
        kind = formats.display_name(fmt)
        if vector:
            kind += " (vector drawing)"
        elif animated:
            kind += f" (animation, {frames} frames)"
        elif frames > 1:
            kind += f" ({frames} pictures)"
        file_rows.append(("Type", kind))
    if status is not None:
        size = format_size(status.st_size)
        blocks = getattr(status, "st_blocks", None)
        if blocks is not None:
            on_disk = blocks * 512
            if abs(on_disk - status.st_size) >= 4096:
                size += f" ({format_size(on_disk)} on disk)"
        file_rows.append(("Size", size))
        created = getattr(status, "st_birthtime", None)
        if created:
            file_rows.append(("Created", format_time(created)))
        file_rows.append(("Modified", format_time(status.st_mtime)))
    groups = [Group("File", tuple(row for row in file_rows if row[1]))]

    image_rows: list[tuple[str, str]] = []
    if width > 0 and height > 0:
        image_rows.append(("Dimensions", f"{width} × {height} pixels"))
        if not vector:
            image_rows.append(("Megapixels", format_megapixels(width, height)))
    if exif is not None and exif.dpi() is not None:
        image_rows.append(("Resolution", format_dpi(exif.dpi())))
    elif dpi is not None and not vector:
        image_rows.append(("Resolution", format_dpi(dpi)))
    image_rows.append(("Colour", format_depth(bits_per_pixel, has_alpha)))
    groups.append(Group("Picture", tuple(row for row in image_rows if row[1])))

    if exif is not None:
        taken = exif.taken()
        camera = " ".join(part for part in (exif.make, exif.model) if part)
        if exif.make and exif.model.lower().startswith(exif.make.lower()):
            camera = exif.model
        lens = " ".join(part for part in (exif.lens_make, exif.lens_model) if part)
        if exif.lens_make and exif.lens_model.lower().startswith(exif.lens_make.lower()):
            lens = exif.lens_model
        camera_rows = [
            ("Date taken", format_time(time.mktime(taken.timetuple())) if taken is not None else ""),
            ("Camera", camera),
            ("Lens", lens),
            ("Exposure", exif_module.format_exposure(exif.exposure_time)),
            ("Aperture", exif_module.format_aperture(exif.f_number)),
            ("ISO", str(exif.iso) if exif.iso else ""),
            ("Focal length", exif_module.format_focal_length(exif.focal_length, exif.focal_length_35mm)),
            ("Exposure bias", exif_module.format_bias(exif.exposure_bias) if exif.exposure_bias else ""),
            ("Program", exif_module.format_program(exif.exposure_program)),
            ("Metering", exif_module.format_metering(exif.metering_mode)),
            ("Flash", exif_module.format_flash(exif.flash)),
            ("Software", exif.software),
            ("Author", exif.artist),
            ("Copyright", exif.copyright),
        ]
        rows = tuple(row for row in camera_rows if row[1])
        if rows:
            groups.append(Group("Camera", rows))
        place_rows = (("Coordinates", exif_module.format_coordinates(exif.latitude, exif.longitude)),
                      ("Altitude", exif_module.format_altitude(exif.altitude)))
        rows = tuple(row for row in place_rows if row[1])
        if rows and place_rows[0][1]:
            groups.append(Group("Place", rows))
    return [group for group in groups if group.rows]
