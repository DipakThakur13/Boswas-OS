"""Files on the clipboard and in a drag, in the formats other file managers understand (Qt side)."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QMimeData

from . import clipformats

_READ = (clipformats.URI_LIST, clipformats.GNOME_COPIED, clipformats.KDE_CUT, clipformats.PLAIN)


def mime_data(paths: list[str], cut: bool = False) -> QMimeData:
    data = QMimeData()
    for name, content in clipformats.encode(paths, cut).items():
        data.setData(name, QByteArray(content))
    return data


def read(data: QMimeData | None) -> tuple[list[str], bool]:
    """(paths, cut) of what a clipboard or a drop holds; ([], False) when it holds no local files."""
    if data is None:
        return [], False
    formats = {name: bytes(data.data(name)) for name in _READ if data.hasFormat(name)}
    return clipformats.decode(formats)


def has_files(data: QMimeData | None) -> bool:
    return data is not None and (data.hasFormat(clipformats.URI_LIST) or data.hasFormat(clipformats.GNOME_COPIED))
