"""Drag and drop rules shared by the views, the tabs and the navigation pane.

As in Windows: dropping on the same volume moves, dropping on another
volume copies, Ctrl makes it a copy and Shift a move. Dropping on the
Trash always moves there.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QMimeData, Qt

from . import clipformats, places

TRASH_TARGET = places.TRASH
_devices: dict[str, int | None] = {}


def _device(path: str) -> int | None:
    if path not in _devices:
        if len(_devices) > 256:
            _devices.clear()
        try:
            _devices[path] = os.stat(path).st_dev
        except OSError:
            _devices[path] = None
    return _devices[path]


def source_device(data: QMimeData | None) -> int | None:
    """The device of the first dragged file (enough to choose between move and copy)."""
    if data is None or not data.hasFormat(clipformats.URI_LIST):
        return None
    head = bytes(data.data(clipformats.URI_LIST)[:16384])
    for path in clipformats.decode_uri_list(head.rsplit(b"\n", 1)[0] if head.count(b"\n") > 1 else head)[:1]:
        try:
            return os.lstat(path).st_dev
        except OSError:
            return None
    return None


def choose_action(modifiers: Qt.KeyboardModifier, possible: Qt.DropAction, device: int | None,
                  destination: str) -> Qt.DropAction:
    """What a drop on ``destination`` does: CopyAction, MoveAction or IgnoreAction."""
    control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
    if destination == TRASH_TARGET:
        wanted = Qt.DropAction.MoveAction
    elif control and not shift:
        wanted = Qt.DropAction.CopyAction
    elif shift and not control:
        wanted = Qt.DropAction.MoveAction
    else:
        target = _device(destination)
        wanted = Qt.DropAction.MoveAction if device is not None and device == target else Qt.DropAction.CopyAction
    if possible & wanted:
        return wanted
    for other in (Qt.DropAction.CopyAction, Qt.DropAction.MoveAction):
        if possible & other and destination != TRASH_TARGET:
            return other
    return Qt.DropAction.IgnoreAction
