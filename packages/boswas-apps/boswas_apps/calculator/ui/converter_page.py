"""The unit converters: two values with their units and a number pad; typing in either converts into the other."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QComboBox, QGridLayout, QVBoxLayout, QWidget

import boswas_fluent as fluent

from .. import numeric, units
from .calc_page import BACKSPACE, SIGN
from .widgets import NUMBER, OPERATOR, FitLabel, KeyButton, copy_paste_menu

TOP, BOTTOM = 0, 1


class ConverterPage(QWidget):
    """One converter page for all categories (``set_category``).

    The field that is being typed into is the active one (drawn strong);
    the other shows the conversion. Click the other value, or move to it
    with Tab, to type there instead: it keeps the number it shows.
    ``units_changed(category key, from key, to key)`` when a unit is chosen.
    """

    units_changed = Signal(str, str, str)

    def __init__(self, chosen: dict[str, list[str]], parent: QWidget | None = None, *, decimal_point: str = ".",
                 group_separator: str = ",") -> None:
        super().__init__(parent)
        self.chosen = chosen
        self.decimal_point, self.group_separator = decimal_point, group_separator
        self.category = units.CATEGORIES[0]
        self.active = TOP
        self.entry = units.ConverterInput()
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.setSpacing(2)
        fields = QVBoxLayout()
        fields.setContentsMargins(8, 0, 8, 8)
        fields.setSpacing(4)
        self.values: list[FitLabel] = []
        self.unit_boxes: list[QComboBox] = []
        for index, name in ((TOP, "Input"), (BOTTOM, "Output")):
            value = FitLabel("0", self, largest="title-large", strong_when_active=True, left=True)
            value.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            value.setCursor(Qt.CursorShape.PointingHandCursor)
            value.setAccessibleDescription(name)
            value.focus_received.connect(lambda i=index: self.set_active(i))
            value.context_requested.connect(lambda position, i=index: self._menu(i, position))
            box = QComboBox(self)
            box.setAccessibleName(f"{name} unit")
            box.setMaxVisibleItems(12)
            box.currentIndexChanged.connect(lambda _index, i=index: self._unit_chosen(i))
            self.values.append(value)
            self.unit_boxes.append(box)
            fields.addWidget(value)
            fields.addWidget(box, 0, Qt.AlignmentFlag.AlignLeft)
            if index == TOP:
                fields.addSpacing(8)
        layout.addLayout(fields)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        self.keys: dict[str, KeyButton] = {}

        def key(label: str, command: str, kind: str, row: int, column: int, tooltip: str = "") -> None:
            button = KeyButton(label, command, kind, tooltip)
            button.clicked.connect(lambda _checked=False, c=command: self.press(c))
            self.keys[command] = button
            grid.addWidget(button, row, column)

        key("CE", "ce", OPERATOR, 0, 1, "Clear entry")
        key(BACKSPACE, "back", OPERATOR, 0, 2, "Backspace")
        for row, digits in enumerate(("789", "456", "123"), start=1):
            for column, digit in enumerate(digits):
                key(digit, digit, NUMBER, row, column)
        key(SIGN, "sign", NUMBER, 4, 0, "Positive negative (F9)")
        key("0", "0", NUMBER, 4, 1)
        key(decimal_point, ".", NUMBER, 4, 2, "Decimal separator")
        for index in range(5):
            grid.setRowStretch(index, 1)
        for index in range(3):
            grid.setColumnStretch(index, 1)
        layout.addLayout(grid, 1)
        self.set_category(self.category.key)

    # --- category and units ---
    def set_category(self, key: str) -> None:
        self.category = units.category(key)
        self._loading = True
        pair = self.chosen.get(key) or [self.category.default_from, self.category.default_to]
        for index, box in enumerate(self.unit_boxes):
            box.clear()
            for unit in self.category.units:
                box.addItem(unit.name, unit.key)
            box.setCurrentIndex(max(0, box.findData(pair[index])))
        self._loading = False
        self.active = TOP
        self.entry.clear()
        self.keys["sign"].setEnabled(self.category.negative)
        self.refresh()

    def unit(self, index: int) -> units.Unit:
        return self.category.unit(self.unit_boxes[index].currentData())

    def set_unit(self, index: int, key: str) -> None:
        self.unit_boxes[index].setCurrentIndex(self.unit_boxes[index].findData(key))

    def _unit_chosen(self, _index: int) -> None:
        if self._loading:
            return
        pair = [self.unit_boxes[TOP].currentData(), self.unit_boxes[BOTTOM].currentData()]
        self.chosen[self.category.key] = pair
        self.refresh()
        self.units_changed.emit(self.category.key, pair[0], pair[1])

    # --- typing ---
    def set_active(self, index: int) -> None:
        """Type into the other field from now on; it starts with the number it shows."""
        if index != self.active:
            shown = self.converted()
            self.active = index
            self.entry.set_value(numeric.round_significant(shown, units.MAX_INPUT_DIGITS) if shown else shown)
        self.refresh()

    def converted(self):
        """The value of the field that is not being typed into."""
        other = BOTTOM if self.active == TOP else TOP
        return units.convert(self.entry.value(), self.unit(self.active), self.unit(other))

    def press(self, command: str) -> bool:
        done = self.entry.press(command, allow_negative=self.category.negative)
        self.refresh()
        return done

    def text(self, index: int) -> str:
        return self.values[index].text()

    def refresh(self) -> None:
        other = BOTTOM if self.active == TOP else TOP
        self.values[self.active].set_text(self.entry.display(self.decimal_point, self.group_separator))
        result = units.format_result(self.converted(), units.significant_digits(self.entry.text),
                                     decimal_point=self.decimal_point, group_separator=self.group_separator)
        self.values[other].set_text(result)
        self.values[self.active].set_active(True)
        self.values[other].set_active(False)

    # --- clipboard ---
    def copy(self, index: int | None = None) -> None:
        QGuiApplication.clipboard().setText(self.text(self.active if index is None else index))

    def paste(self, index: int | None = None) -> bool:
        if index is not None and index != self.active:
            self.set_active(index)
        value = numeric.parse_number(QGuiApplication.clipboard().text(), decimal_point=self.decimal_point,
                                     group_separator=self.group_separator)
        if value is None or (value < 0 and not self.category.negative):
            return False
        text = numeric.format_number(abs(value), units.MAX_INPUT_DIGITS + 8, group_separator="")
        if "e" in text or sum(character.isdigit() for character in text) > units.MAX_INPUT_DIGITS:
            return False
        self.entry.set_value(value)
        self.refresh()
        return True

    def _menu(self, index: int, position) -> None:
        menu = copy_paste_menu(self, lambda: self.copy(index), lambda: self.paste(index),
                               can_paste=bool(QGuiApplication.clipboard().text()))
        menu.exec(position)

    # --- keyboard ---
    def handle_key(self, key: int, text: str, modifiers: Qt.KeyboardModifier) -> bool:
        keys = Qt.Key
        if modifiers & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            return False
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if key in (keys.Key_C, keys.Key_Insert):
                self.copy()
                return True
            if key == keys.Key_V:
                self.paste()
                return True
            return False
        command = {keys.Key_Backspace: "back", keys.Key_Delete: "ce", keys.Key_Escape: "ce",
                   keys.Key_F9: "sign"}.get(key)
        if command is None and len(text) == 1:
            if text.isdigit() and text.isascii():
                command = text
            elif text in (".", ",", self.decimal_point):
                command = "."
        if command is None:
            return False
        self.press(command)
        return True

    def focus_entry(self) -> None:
        """Put the keyboard focus on the field that is being typed into."""
        self.values[self.active].setFocus()

    def keyPressEvent(self, event) -> None:
        if not self.handle_key(event.key(), event.text(), event.modifiers()):
            event.ignore()
