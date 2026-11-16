"""The details list of the window: one folder of an archive (or a search result) as a table model.

The model holds references to the entries of the folder that is shown,
nothing else, and answers ``data`` from them when the view asks, so a
folder with hundreds of thousands of entries costs one list of references.
Sorting keeps folders first, as a file manager does.
"""

from __future__ import annotations

import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QIcon

import boswas_fluent as fluent

from .. import model, names
from ..model import DIR, Entry

NAME, SIZE, PACKED, MODIFIED, TYPE, CRC, MODE, LOCATION = range(8)
HEADERS = ("Name", "Size", "Packed size", "Modified", "Type", "CRC", "Permissions", "Folder")
EntryRole = Qt.ItemDataRole.UserRole + 1
_DIGITS = re.compile(r"(\d+)")
_RIGHT = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
_LEFT = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def natural_key(text: str) -> tuple:
    """A sort key that puts ``file2`` before ``file10`` and ignores upper and lower case."""
    return tuple(int(part) if part.isdigit() else part for part in _DIGITS.split(text.casefold()))


class EntryModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._entries: list[Entry] = []
        self._icons: dict[str, QIcon] = {}
        self._column = NAME
        self._descending = False
        self.searching = False

    # --- content ---
    def set_entries(self, entries: list[Entry], searching: bool = False) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self.searching = searching
        self._sort()
        self.endResetModel()

    def entries(self) -> list[Entry]:
        return self._entries

    def entry(self, index: QModelIndex) -> Entry | None:
        if index.isValid() and 0 <= index.row() < len(self._entries):
            return self._entries[index.row()]
        return None

    def row_of(self, entry: Entry) -> int:
        for row, candidate in enumerate(self._entries):
            if candidate is entry:
                return row
        return -1

    # --- Qt ---
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _RIGHT if section in (SIZE, PACKED) else _LEFT
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        entry = self.entry(index)
        if entry is None:
            return None
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._text(entry, column)
        if role == Qt.ItemDataRole.DecorationRole and column == NAME:
            return self._icon(model.icon_name(entry))
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _RIGHT if column in (SIZE, PACKED) else _LEFT
        if role == Qt.ItemDataRole.ToolTipRole and column == NAME:
            tip = names.display(entry.path)
            if entry.kind in (model.SYMLINK, model.HARDLINK) and entry.link:
                tip += f"\nLink to {names.display(entry.link)}"
            if entry.encrypted:
                tip += "\nEncrypted"
            return tip
        if role == Qt.ItemDataRole.AccessibleTextRole and column == NAME:
            return f"{names.display(entry.base)}, {model.type_text(entry)}"
        if role == EntryRole:
            return entry
        return None

    def _text(self, entry: Entry, column: int) -> str:
        if column == NAME:
            return names.display(entry.base)
        if column == SIZE:
            return "" if entry.kind == DIR else model.human_size(entry.size)
        if column == PACKED:
            return "" if entry.kind == DIR or entry.packed < 0 else model.human_size(entry.packed)
        if column == MODIFIED:
            return model.time_text(entry.mtime)
        if column == TYPE:
            return model.type_text(entry)
        if column == CRC:
            return model.crc_text(entry)
        if column == MODE:
            return model.mode_text(entry)
        if column == LOCATION:
            return names.display(entry.parent) or "/"
        return ""

    def _icon(self, name: str) -> QIcon:
        icon = self._icons.get(name)
        if icon is None:
            icon = fluent.icon(name, "unknown")
            self._icons[name] = icon
        return icon

    # --- sorting ---
    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self.layoutAboutToBeChanged.emit()
        self._column = column
        self._descending = order == Qt.SortOrder.DescendingOrder
        self._sort()
        self.layoutChanged.emit()

    def _sort(self) -> None:
        column = self._column
        if column == SIZE:
            def key(entry: Entry):
                return entry.size
        elif column == PACKED:
            def key(entry: Entry):
                return entry.packed
        elif column == MODIFIED:
            def key(entry: Entry):
                return entry.mtime or 0.0
        elif column == TYPE:
            def key(entry: Entry):
                return model.type_text(entry).casefold()
        elif column == CRC:
            def key(entry: Entry):
                return entry.crc or 0
        elif column == MODE:
            def key(entry: Entry):
                return entry.mode or 0
        elif column == LOCATION:
            def key(entry: Entry):
                return entry.parent.casefold()
        else:
            key = None
        # Two stable passes: by name first, so equal values of the chosen column stay in name order.
        self._entries.sort(key=lambda entry: natural_key(entry.base))
        if key is not None:
            self._entries.sort(key=key, reverse=self._descending)
        elif self._descending:
            self._entries.reverse()
        self._entries.sort(key=lambda entry: entry.kind != DIR)      # folders first, whatever the order
