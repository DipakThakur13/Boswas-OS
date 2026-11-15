"""Keyboard shortcuts the way every Boswas application has them."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget

# name -> the key sequences of the platform (QKeySequence.StandardKey) or a fixed sequence
STANDARD = {
    "new": QKeySequence.StandardKey.New,
    "open": QKeySequence.StandardKey.Open,
    "save": QKeySequence.StandardKey.Save,
    "save_as": QKeySequence.StandardKey.SaveAs,
    "print": QKeySequence.StandardKey.Print,
    "close": QKeySequence.StandardKey.Close,
    "quit": QKeySequence.StandardKey.Quit,
    "undo": QKeySequence.StandardKey.Undo,
    "redo": QKeySequence.StandardKey.Redo,
    "cut": QKeySequence.StandardKey.Cut,
    "copy": QKeySequence.StandardKey.Copy,
    "paste": QKeySequence.StandardKey.Paste,
    "select_all": QKeySequence.StandardKey.SelectAll,
    "delete": QKeySequence.StandardKey.Delete,
    "find": QKeySequence.StandardKey.Find,
    "find_next": QKeySequence.StandardKey.FindNext,
    "find_previous": QKeySequence.StandardKey.FindPrevious,
    "replace": QKeySequence.StandardKey.Replace,
    "refresh": QKeySequence.StandardKey.Refresh,
    "zoom_in": QKeySequence.StandardKey.ZoomIn,
    "zoom_out": QKeySequence.StandardKey.ZoomOut,
    "zoom_reset": "Ctrl+0",
    "back": QKeySequence.StandardKey.Back,
    "forward": QKeySequence.StandardKey.Forward,
    "help": QKeySequence.StandardKey.HelpContents,
    "fullscreen": "F11",
    "preferences": "Ctrl+,",
    "rename": "F2",
    "properties": "Alt+Return",
    "new_tab": QKeySequence.StandardKey.AddTab,
    "next_tab": QKeySequence.StandardKey.NextChild,
    "previous_tab": QKeySequence.StandardKey.PreviousChild,
}


# What a name means where the platform has no binding for it (Qt knows none for "quit" without a
# desktop theme, for example).
FALLBACK = {
    "new": "Ctrl+N", "open": "Ctrl+O", "save": "Ctrl+S", "save_as": "Ctrl+Shift+S", "print": "Ctrl+P",
    "close": "Ctrl+W", "quit": "Ctrl+Q", "undo": "Ctrl+Z", "redo": "Ctrl+Shift+Z", "cut": "Ctrl+X",
    "copy": "Ctrl+C", "paste": "Ctrl+V", "select_all": "Ctrl+A", "delete": "Del", "find": "Ctrl+F",
    "find_next": "F3", "find_previous": "Shift+F3", "replace": "Ctrl+H", "refresh": "F5", "zoom_in": "Ctrl++",
    "zoom_out": "Ctrl+-", "back": "Alt+Left", "forward": "Alt+Right", "help": "F1", "new_tab": "Ctrl+T",
    "next_tab": "Ctrl+Tab", "previous_tab": "Ctrl+Shift+Tab",
}


def key_sequences(name: str) -> list[QKeySequence]:
    """The key sequences of a standard shortcut name (see ``STANDARD``): the platform's, else ``FALLBACK``."""
    try:
        value = STANDARD[name]
    except KeyError:
        raise KeyError(f"unknown standard shortcut {name!r}") from None
    if isinstance(value, str):
        return [QKeySequence(value)]
    return list(QKeySequence.keyBindings(value)) or [QKeySequence(FALLBACK[name])]


def add_shortcut(widget: QWidget, keys: str | QKeySequence | QKeySequence.StandardKey, slot: Callable[[], None], *,
                 context: Qt.ShortcutContext = Qt.ShortcutContext.WindowShortcut) -> list[QShortcut]:
    """Make ``keys`` call ``slot`` while the window of ``widget`` is active.

    ``keys`` is a standard name (``"find"``, ``"refresh"``, ``"quit"``: see
    ``STANDARD``), a key sequence as text (``"Ctrl+Shift+N"``) or a
    QKeySequence. Returns the shortcuts made (a standard name can have
    several key sequences).
    """
    if isinstance(keys, str) and keys in STANDARD:
        sequences = key_sequences(keys)
    else:
        sequences = [QKeySequence(keys)]
    made = []
    for sequence in sequences:
        if sequence.isEmpty():
            continue
        shortcut = QShortcut(sequence, widget)
        shortcut.setContext(context)
        shortcut.activated.connect(slot)
        made.append(shortcut)
    return made


def add_shortcuts(widget: QWidget, **slots: Callable[[], None]) -> dict[str, list[QShortcut]]:
    """Several standard shortcuts at once: ``add_shortcuts(window, find=..., refresh=..., quit=window.close)``."""
    return {name: add_shortcut(widget, name, slot) for name, slot in slots.items()}
