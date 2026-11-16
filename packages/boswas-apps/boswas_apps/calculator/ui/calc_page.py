"""The page of the Standard, Scientific and Programmer calculators: display, keypads, keyboard."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QActionGroup, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import (QAbstractButton, QGridLayout, QHBoxLayout, QSizePolicy, QStackedWidget, QVBoxLayout,
                               QWidget)

import boswas_fluent as fluent

from .. import numeric
from ..engine import PROGRAMMER, SCIENTIFIC, STANDARD
from ..model import Calculator
from ..state import SHIFT_MODES
from .panel import HISTORY, MEMORY, RecordPanel
from .widgets import ACCENT, NUMBER, OPERATOR, SUBTLE, FitLabel, KeyButton, copy_paste_menu

MINUS, TIMES, DIVIDE, BACKSPACE, ROOT, PI = "−", "×", "÷", "⌫", "√", "π"
SIGN = f"+/{MINUS}"
MEMORY_KEYS = (("MC", "mc", "Clear all memory (Ctrl+L)"), ("MR", "mr", "Memory recall (Ctrl+R)"),
               ("M+", "m+", "Memory add (Ctrl+P)"), (f"M{MINUS}", "m-", "Memory subtract (Ctrl+Q)"),
               ("MS", "ms", "Memory store (Ctrl+M)"))
ANGLE_LABELS = {numeric.DEG: "DEG", numeric.RAD: "RAD", numeric.GRAD: "GRAD"}
ANGLE_NAMES = {numeric.DEG: "Degrees", numeric.RAD: "Radians", numeric.GRAD: "Gradians"}
WORD_LABELS = {64: "QWORD", 32: "DWORD", 16: "WORD", 8: "BYTE"}
RADIX_LABELS = {16: "HEX", 10: "DEC", 8: "OCT", 2: "BIN"}
RADIX_NAMES = {16: "Hexadecimal", 10: "Decimal", 8: "Octal", 2: "Binary"}
SHIFT_LABELS = {"arithmetic": "Arithmetic shift", "logical": "Logical shift", "rotate": "Rotate circular shift",
                "rotate_carry": "Rotate through carry circular shift"}
SHIFT_COMMANDS = {"arithmetic": ("lsh", "rsh"), "logical": ("lsh", "rshl"), "rotate": ("rol", "ror"),
                  "rotate_carry": ("rolc", "rorc")}
_WORD_ACTIONS = {label.lower(): bits for bits, label in WORD_LABELS.items()}
_RADIX_ACTIONS = {label.lower(): radix for radix, label in RADIX_LABELS.items()}
_SUBSCRIPT = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")

# The six keys of the scientific keypad that the 2nd key changes: (label, command, tool tip) twice.
SECOND = {
    "sqr": ((f"x²", "sqr", "Square"), ("x³", "cube", "Cube")),
    "sqrt": ((f"²{ROOT}x", "sqrt", "Square root"), (f"³{ROOT}x", "cuberoot", "Cube root")),
    "pow": (("xʸ", "pow", "x to the power of y"), (f"ʸ{ROOT}x", "root", "y-th root of x")),
    "pow10": (("10ˣ", "pow10", "Ten to the power of x"), ("2ˣ", "pow2", "Two to the power of x")),
    "log": (("log", "log", "Logarithm to base 10"), ("logᵧx", "logbasey", "Logarithm of x to base y")),
    "ln": (("ln", "ln", "Natural logarithm"), ("eˣ", "powe", "e to the power of x")),
}


def trig_command(name: str, inverse: bool, hyperbolic: bool) -> str:
    return ("a" if inverse else "") + name + ("h" if hyperbolic else "")


def trig_label(name: str, inverse: bool, hyperbolic: bool) -> str:
    return name + ("h" if hyperbolic else "") + ("⁻¹" if inverse else "")


class RadixRow(QAbstractButton):
    """A line of the Programmer display: HEX, DEC, OCT or BIN with the value; click to type in that radix."""

    def __init__(self, radix: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.radix = radix
        self._value = "0"
        self.setCheckable(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(RADIX_NAMES[radix])
        self._describe()

    def _describe(self) -> None:
        self.setAccessibleName(f"{RADIX_NAMES[self.radix]} {self._value}")

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        if value != self._value:
            self._value = value
            self._describe()
            self.update()

    def sizeHint(self) -> QSize:
        line = QFontMetrics(fluent.font("body")).height()
        return QSize(200, line * 2 + 4 if self.radix == 2 else line + 6)

    def minimumSizeHint(self) -> QSize:
        return QSize(120, self.sizeHint().height())

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self.click()
            return
        event.ignore()

    def nextCheckState(self) -> None:
        if not self.isChecked():
            self.setChecked(True)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        radius = fluent.theme().radius("control")
        if self.isDown() or self.underMouse():
            fluent.paint.rounded_rect(painter, self.rect(), radius,
                                      "fill-subtle-pressed" if self.isDown() else "fill-subtle-hover")
        if self.isChecked():
            fluent.paint.rounded_rect(painter, QRectF(0, (self.height() - 16) / 2, 3, 16), 1.5, "accent")
        font = fluent.font("body-strong" if self.isChecked() else "body")
        painter.setFont(font)
        painter.setPen(fluent.qcolor("text" if self.isChecked() else "text-secondary"))
        painter.drawText(QRect(12, 0, 40, self.height()), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         RADIX_LABELS[self.radix])
        painter.setFont(fluent.font("body"))
        painter.setPen(fluent.qcolor("text"))
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap
        painter.drawText(QRect(56, 0, self.width() - 60, self.height()), int(flags), self._value)


class BitButton(QAbstractButton):
    """One bit of the bit keypad."""

    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.setCheckable(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._describe()

    def _describe(self) -> None:
        self.setAccessibleName(f"Bit {self.index}: {1 if self.isChecked() else 0}")

    def checkStateSet(self) -> None:
        self._describe()

    def sizeHint(self) -> QSize:
        return QSize(16, 28)

    def minimumSizeHint(self) -> QSize:
        return QSize(10, 20)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and self.isEnabled():
            self.click()
            return
        event.ignore()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self.isEnabled() and (self.isDown() or self.underMouse()):
            fluent.paint.rounded_rect(painter, self.rect(), fluent.theme().radius("control"),
                                      "fill-subtle-pressed" if self.isDown() else "fill-subtle-hover")
        colour = "text-disabled" if not self.isEnabled() else ("accent-text" if self.isChecked() else "text")
        font = fluent.font("body-strong" if self.isChecked() else "body")
        fluent.paint.elided(painter, self.rect(), "1" if self.isChecked() else "0", font, colour,
                            Qt.AlignmentFlag.AlignCenter)


class BitPad(QWidget):
    """The 64 bits of a value in four rows of sixteen; ``toggled(index)`` when one is clicked."""

    toggled = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.buttons: dict[int, BitButton] = {}
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 4, 0, 4)
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(0)
        for row in range(4):
            for nibble in range(4):
                top = 63 - row * 16 - nibble * 4
                for position in range(4):
                    index = top - position
                    button = BitButton(index, self)
                    button.clicked.connect(lambda _checked=False, i=index: self.toggled.emit(i))
                    self.buttons[index] = button
                    grid.addWidget(button, row * 2, nibble * 5 + position)
                label = fluent.Label(str(top - 3), "caption", self, tone="secondary")
                label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
                grid.addWidget(label, row * 2 + 1, nibble * 5, 1, 4)
                if nibble < 3:
                    grid.setColumnMinimumWidth(nibble * 5 + 4, 8)
            grid.setRowStretch(row * 2, 1)

    def set_value(self, pattern: int, bits: int) -> None:
        for index, button in self.buttons.items():
            button.setEnabled(index < bits)
            checked = bool(pattern >> index & 1) and index < bits
            if button.isChecked() != checked:
                button.setChecked(checked)
            button.update()


class CalcPage(QWidget):
    """Display, keypad and keyboard of the three calculator modes; the ``Calculator`` does the arithmetic."""

    changed = Signal()               # the calculator changed (the window refreshes the side panel)
    settings_changed = Signal()      # angle unit, radix, word size or shift mode changed (the window saves them)

    def __init__(self, calculator: Calculator, state: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.calculator = calculator
        self.state = state
        self.mode = calculator.mode
        self.second = False
        self.trig_inverse = False
        self.trig_hyperbolic = False
        self.narrow = True                       # no side panel: the page offers history and memory itself
        self._keys: dict[str, dict[str, KeyButton]] = {mode: {} for mode in (STANDARD, SCIENTIFIC, PROGRAMMER)}
        self._memory_buttons: dict[str, dict[str, KeyButton]] = {}
        self._clear_keys: dict[str, KeyButton] = {}
        self._open_keys: dict[str, KeyButton] = {}
        self._second_keys: dict[str, KeyButton] = {}
        self._flyout: fluent.Flyout | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        for mode in (SCIENTIFIC, PROGRAMMER):
            calculator.engines[mode].set_angle(state["angle"])
        programmer = calculator.engines[PROGRAMMER]
        programmer.set_word_size(state["bits"])
        programmer.set_radix(state["radix"])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.setSpacing(2)
        self.expression_label = FitLabel("", self, largest="body", tone="text-secondary")
        self.expression_label.setAccessibleDescription("Expression")
        self.display = FitLabel("0", self, largest="title-large")
        self.display.setAccessibleDescription("Display")
        self.display.context_requested.connect(self._display_menu)
        self.display.clicked.connect(self.hide_sheet)
        display_box = QVBoxLayout()
        display_box.setContentsMargins(8, 0, 8, 0)
        display_box.setSpacing(0)
        display_box.addWidget(self.expression_label)
        display_box.addWidget(self.display)
        layout.addLayout(display_box)
        self.pads = QStackedWidget(self)
        layout.addWidget(self.pads, 1)
        self._pad_widgets = {STANDARD: self._build_standard(), SCIENTIFIC: self._build_scientific(),
                             PROGRAMMER: self._build_programmer()}
        for widget in self._pad_widgets.values():
            self.pads.addWidget(widget)

        self.sheet = RecordPanel(calculator, self, surface=True)
        self.sheet.hide()
        self.sheet.changed.connect(self._changed)
        self.sheet.recalled.connect(self.hide_sheet)
        self.set_mode(self.mode)

    # --- building ---
    def _key(self, mode: str, label: str, command: str, kind: str = OPERATOR, tooltip: str = "") -> KeyButton:
        button = KeyButton(label, command, kind, tooltip)
        button.clicked.connect(lambda _checked=False, b=button: self.do(b.command))
        self._keys[mode][command] = button
        return button

    def _digit(self, mode: str, digit: str) -> KeyButton:
        return self._key(mode, digit, digit, NUMBER)

    def _memory_row(self, mode: str, *, programmer: bool = False) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        buttons: dict[str, KeyButton] = {}
        for label, command, tooltip in (MEMORY_KEYS[-1:] if programmer else MEMORY_KEYS):
            button = KeyButton(label, command, SUBTLE, tooltip)
            button.clicked.connect(lambda _checked=False, c=command: self.do(c))
            buttons[command] = button
            row.addWidget(button, 1)
        more = KeyButton("M", "memory", SUBTLE, "Memory", chevron=True)
        more.clicked.connect(lambda _checked=False: self.toggle_sheet(MEMORY))
        buttons["memory"] = more
        row.addWidget(more, 1)
        self._memory_buttons[mode] = buttons
        return row

    def _grid(self, rows: list[list[KeyButton]]) -> QGridLayout:
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        for row_index, row in enumerate(rows):
            grid.setRowStretch(row_index, 1)
            for column, button in enumerate(row):
                grid.setColumnStretch(column, 1)
                grid.addWidget(button, row_index, column)
        return grid

    def _number_rows(self, mode: str) -> list[list[KeyButton]]:
        """The last four rows without their first column: 7 8 9 ×, 4 5 6 −, 1 2 3 +, ± 0 . =."""
        point = self.calculator.decimal_point
        return [
            [self._digit(mode, "7"), self._digit(mode, "8"), self._digit(mode, "9"),
             self._key(mode, TIMES, "*", OPERATOR, "Multiply by")],
            [self._digit(mode, "4"), self._digit(mode, "5"), self._digit(mode, "6"),
             self._key(mode, MINUS, "-", OPERATOR, "Minus")],
            [self._digit(mode, "1"), self._digit(mode, "2"), self._digit(mode, "3"),
             self._key(mode, "+", "+", OPERATOR, "Plus")],
            [self._key(mode, SIGN, "sign", NUMBER, "Positive negative (F9)"), self._digit(mode, "0"),
             self._key(mode, point, ".", NUMBER, "Decimal separator"), self._key(mode, "=", "=", ACCENT, "Equals")],
        ]

    def _build_standard(self) -> QWidget:
        mode = STANDARD
        pad = QWidget(self)
        layout = QVBoxLayout(pad)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(self._memory_row(mode))
        clear = self._key(mode, "C", "c", OPERATOR, "Clear (Esc)")
        rows = [
            [self._key(mode, "%", "percent", OPERATOR, "Percent"),
             self._key(mode, "CE", "ce", OPERATOR, "Clear entry (Delete)"), clear,
             self._key(mode, BACKSPACE, "back", OPERATOR, "Backspace")],
            [self._key(mode, "1/x", "reciprocal", OPERATOR, "Reciprocal (R)"),
             self._key(mode, "x²", "sqr", OPERATOR, "Square (Q)"),
             self._key(mode, f"²{ROOT}x", "sqrt", OPERATOR, "Square root (@)"),
             self._key(mode, DIVIDE, "/", OPERATOR, "Divide by")],
            *self._number_rows(mode),
        ]
        layout.addLayout(self._grid(rows), 1)
        return pad

    def _build_scientific(self) -> QWidget:
        mode = SCIENTIFIC
        pad = QWidget(self)
        layout = QVBoxLayout(pad)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(2)
        self.angle_button = KeyButton("DEG", "angle", SUBTLE, "Angle unit (F3 degrees, F4 radians, F5 gradians)")
        self.angle_button.clicked.connect(lambda _checked=False: self.do("angle"))
        self.fe_button = KeyButton("F-E", "fe", SUBTLE, "Scientific notation (V)", checkable=True)
        self.fe_button.clicked.connect(lambda _checked=False: self.do("fe"))
        top.addWidget(self.angle_button)
        top.addWidget(self.fe_button)
        top.addStretch(1)
        layout.addLayout(top)
        layout.addLayout(self._memory_row(mode))
        functions = QHBoxLayout()
        functions.setSpacing(2)
        self.trig_button = KeyButton("Trigonometry", "trigonometry", SUBTLE, "Trigonometry", chevron=True)
        self.trig_button.clicked.connect(lambda _checked=False: self._show_trigonometry())
        self.function_button = KeyButton("Function", "function", SUBTLE, "Functions", chevron=True)
        self.function_button.clicked.connect(lambda _checked=False: self._show_functions())
        functions.addWidget(self.trig_button)
        functions.addWidget(self.function_button)
        functions.addStretch(1)
        layout.addLayout(functions)

        self.second_button = KeyButton("2ⁿᵈ", "second", OPERATOR, "Second functions", checkable=True)
        self.second_button.clicked.connect(lambda _checked=False: self.do("second"))
        for name, ((label, command, tooltip), _alternate) in SECOND.items():
            button = KeyButton(label, command, OPERATOR, tooltip)
            button.clicked.connect(lambda _checked=False, b=button: self.do(b.command))
            self._second_keys[name] = button
        clear = self._key(mode, "C", "c", OPERATOR, "Clear (Esc)")
        self._clear_keys[mode] = clear
        open_key = self._key(mode, "(", "(", OPERATOR, "Open parenthesis")
        self._open_keys[mode] = open_key
        number_rows = self._number_rows(mode)
        second = self._second_keys
        rows = [
            [self.second_button, self._key(mode, PI, "pi", OPERATOR, "Pi (P)"),
             self._key(mode, "e", "e", OPERATOR, "Euler’s number (E)"), clear,
             self._key(mode, BACKSPACE, "back", OPERATOR, "Backspace")],
            [second["sqr"], self._key(mode, "1/x", "reciprocal", OPERATOR, "Reciprocal (R)"),
             self._key(mode, "|x|", "abs", OPERATOR, "Absolute value"),
             self._key(mode, "exp", "exp", OPERATOR, "Exponent of ten (X)"),
             self._key(mode, "mod", "mod", OPERATOR, "Modulo (%)")],
            [second["sqrt"], open_key, self._key(mode, ")", ")", OPERATOR, "Close parenthesis"),
             self._key(mode, "n!", "fact", OPERATOR, "Factorial (!)"),
             self._key(mode, DIVIDE, "/", OPERATOR, "Divide by")],
            [second["pow"], *number_rows[0]],
            [second["pow10"], *number_rows[1]],
            [second["log"], *number_rows[2]],
            [second["ln"], *number_rows[3]],
        ]
        layout.addLayout(self._grid(rows), 1)
        return pad

    def _build_programmer(self) -> QWidget:
        mode = PROGRAMMER
        pad = QWidget(self)
        layout = QVBoxLayout(pad)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.radix_rows: dict[int, RadixRow] = {}
        for radix in (16, 10, 8, 2):
            row = RadixRow(radix, pad)
            row.clicked.connect(lambda _checked=False, r=radix: self.do(RADIX_LABELS[r].lower()))
            self.radix_rows[radix] = row
            layout.addWidget(row)
        tools = QHBoxLayout()
        tools.setSpacing(2)
        self.full_pad_button = KeyButton("Keypad", "full_pad", SUBTLE, "Full keypad", checkable=True)
        self.bit_pad_button = KeyButton("Bits", "bit_pad", SUBTLE, "Bit toggling keypad", checkable=True)
        self.full_pad_button.clicked.connect(lambda _checked=False: self.set_bit_pad(False))
        self.bit_pad_button.clicked.connect(lambda _checked=False: self.set_bit_pad(True))
        self.word_button = KeyButton("QWORD", "next_word", SUBTLE,
                                     "Word size (F12 QWORD, F2 DWORD, F3 WORD, F4 BYTE)")
        self.word_button.clicked.connect(lambda _checked=False: self.do("next_word"))
        tools.addWidget(self.full_pad_button)
        tools.addWidget(self.bit_pad_button)
        tools.addWidget(self.word_button)
        tools.addLayout(self._memory_row(mode, programmer=True))
        layout.addLayout(tools)
        operators = QHBoxLayout()
        operators.setSpacing(2)
        self.bitwise_button = KeyButton("Bitwise", "bitwise", SUBTLE, "Bitwise operators", chevron=True)
        self.bitwise_button.clicked.connect(lambda _checked=False: self._show_bitwise())
        self.shift_button = KeyButton("Bit shift", "shift", SUBTLE, "Kind of bit shift", chevron=True)
        self.shift_button.clicked.connect(lambda _checked=False: self._show_shift_menu())
        operators.addWidget(self.bitwise_button)
        operators.addWidget(self.shift_button)
        operators.addStretch(1)
        layout.addLayout(operators)

        clear = self._key(mode, "C", "c", OPERATOR, "Clear (Esc)")
        self._clear_keys[mode] = clear
        open_key = self._key(mode, "(", "(", OPERATOR, "Open parenthesis")
        self._open_keys[mode] = open_key
        self.left_shift_key = KeyButton("<<", "lsh", OPERATOR, "Left shift")
        self.right_shift_key = KeyButton(">>", "rsh", OPERATOR, "Right shift")
        for button in (self.left_shift_key, self.right_shift_key):
            button.clicked.connect(lambda _checked=False, b=button: self.do(b.command))
        number_rows = self._number_rows(mode)
        letters = [self._key(mode, letter, letter, NUMBER) for letter in "ABCDEF"]
        rows = [
            [letters[0], self.left_shift_key, self.right_shift_key, clear,
             self._key(mode, BACKSPACE, "back", OPERATOR, "Backspace")],
            [letters[1], open_key, self._key(mode, ")", ")", OPERATOR, "Close parenthesis"),
             self._key(mode, "%", "mod", OPERATOR, "Modulo"), self._key(mode, DIVIDE, "/", OPERATOR, "Divide by")],
            [letters[2], *number_rows[0]],
            [letters[3], *number_rows[1]],
            [letters[4], *number_rows[2]],
            [letters[5], *number_rows[3]],
        ]
        self.programmer_pads = QStackedWidget(pad)
        full = QWidget(pad)
        full.setLayout(self._grid(rows))
        self.bit_pad = BitPad(pad)
        self.bit_pad.toggled.connect(self._toggle_bit)
        self.programmer_pads.addWidget(full)
        self.programmer_pads.addWidget(self.bit_pad)
        layout.addWidget(self.programmer_pads, 1)
        self.full_pad_button.setChecked(True)
        return pad

    # --- flyouts ---
    def _close_flyout(self) -> None:
        if self._flyout is not None:
            self._flyout.close()
            self._flyout.deleteLater()
            self._flyout = None

    def _open_flyout(self, anchor: QWidget, content: QWidget) -> fluent.Flyout:
        self._close_flyout()
        flyout = fluent.Flyout(self, padding=8)
        flyout.add_widget(content)
        flyout.show_at(anchor)
        self._flyout = flyout
        return flyout

    def _flyout_key(self, label: str, command: str, tooltip: str = "", *, checkable: bool = False) -> KeyButton:
        button = KeyButton(label, command, OPERATOR, tooltip, checkable=checkable, style="body")
        button.setFixedSize(72, 40)
        return button

    def _show_trigonometry(self) -> None:
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        second = self._flyout_key("2ⁿᵈ", "trig_second", "Inverse functions", checkable=True)
        hyperbolic = self._flyout_key("hyp", "trig_hyp", "Hyperbolic functions", checkable=True)
        second.setChecked(self.trig_inverse)
        hyperbolic.setChecked(self.trig_hyperbolic)
        grid.addWidget(second, 0, 0)
        grid.addWidget(hyperbolic, 1, 0)
        self.trig_keys: dict[str, KeyButton] = {}
        for index, name in enumerate(("sin", "cos", "tan", "sec", "csc", "cot")):
            button = self._flyout_key(name, name)
            button.clicked.connect(lambda _checked=False, n=name: self._trig_pressed(n))
            self.trig_keys[name] = button
            grid.addWidget(button, index // 3, 1 + index % 3)

        def relabel() -> None:
            self.trig_inverse, self.trig_hyperbolic = second.isChecked(), hyperbolic.isChecked()
            for name, button in self.trig_keys.items():
                button.set_label(trig_label(name, self.trig_inverse, self.trig_hyperbolic))
        second.clicked.connect(lambda _checked=False: relabel())
        hyperbolic.clicked.connect(lambda _checked=False: relabel())
        relabel()
        self.trig_second_key, self.trig_hyp_key = second, hyperbolic
        self._open_flyout(self.trig_button, content)

    def _trig_pressed(self, name: str) -> None:
        command = trig_command(name, self.trig_inverse, self.trig_hyperbolic)
        self._close_flyout()
        self.do(command)

    def _show_functions(self) -> None:
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        self.function_keys: dict[str, KeyButton] = {}
        for index, (label, command, tooltip) in enumerate((
                ("|x|", "abs", "Absolute value"), ("⌊x⌋", "floor", "Floor"), ("⌈x⌉", "ceil", "Ceiling"),
                ("rand", "rand", "Random number"), ("→dms", "dms", "Degrees, minutes, seconds"),
                ("→deg", "degrees", "Decimal degrees"))):
            button = self._flyout_key(label, command, tooltip)
            button.clicked.connect(lambda _checked=False, c=command: (self._close_flyout(), self.do(c)))
            self.function_keys[command] = button
            grid.addWidget(button, index // 3, index % 3)
        self._open_flyout(self.function_button, content)

    def _show_bitwise(self) -> None:
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        self.bitwise_keys: dict[str, KeyButton] = {}
        for index, (label, command) in enumerate((("AND", "and"), ("OR", "or"), ("NOT", "not"), ("NAND", "nand"),
                                                  ("NOR", "nor"), ("XOR", "xor"))):
            button = self._flyout_key(label, command)
            button.clicked.connect(lambda _checked=False, c=command: (self._close_flyout(), self.do(c)))
            self.bitwise_keys[command] = button
            grid.addWidget(button, index // 3, index % 3)
        self._open_flyout(self.bitwise_button, content)

    def _shift_menu(self) -> fluent.MenuFlyout:
        menu = fluent.MenuFlyout(self)
        group = QActionGroup(menu)
        for mode in SHIFT_MODES:
            action = menu.add_item(SHIFT_LABELS[mode], lambda m=mode: self.set_shift_mode(m), checkable=True,
                                   checked=self.state["shift"] == mode)
            group.addAction(action)
        return menu

    def _show_shift_menu(self) -> None:
        self._shift_menu().exec_at(self.shift_button)

    def _display_menu(self, position) -> None:
        clipboard = QGuiApplication.clipboard()
        menu = copy_paste_menu(self, self.copy, self.paste, can_paste=bool(clipboard.text()))
        menu.exec(position)

    # --- mode and settings ---
    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.calculator.set_mode(mode)
        self.pads.setCurrentWidget(self._pad_widgets[mode])
        self.hide_sheet()
        self._close_flyout()
        self.refresh()

    def set_narrow(self, narrow: bool) -> None:
        """Whether the window is too narrow for the side panel: the M˅ key and the sheet take its place."""
        self.narrow = narrow
        if not narrow:
            self.hide_sheet()
        self.refresh()

    def set_shift_mode(self, mode: str) -> None:
        self.state["shift"] = mode
        self.settings_changed.emit()
        self.refresh()

    def set_bit_pad(self, shown: bool) -> None:
        self.programmer_pads.setCurrentIndex(1 if shown else 0)
        self.full_pad_button.setChecked(not shown)
        self.bit_pad_button.setChecked(shown)

    def _toggle_bit(self, index: int) -> None:
        self.calculator.engine.toggle_bit(index)
        self._changed()

    # --- the sheet with history and memory (narrow window) ---
    def sheet_visible(self) -> bool:
        return self.sheet.isVisible()

    def toggle_sheet(self, tab: str = HISTORY) -> None:
        if self.sheet.isVisible() and self.sheet.current_tab() == tab:
            self.hide_sheet()
            return
        self.sheet.refresh()
        self.sheet.show_tab(tab)
        self._place_sheet()
        self.sheet.show()
        self.sheet.raise_()
        self.sheet.setFocus()

    def hide_sheet(self) -> None:
        if self.sheet.isVisible():
            self.sheet.hide()
            self.setFocus()

    def _place_sheet(self) -> None:
        top = self.pads.geometry().top() + 36
        self.sheet.setGeometry(0, top, self.width(), max(self.height() - top, 120))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.sheet.isVisible():
            self._place_sheet()

    def mousePressEvent(self, event) -> None:
        self.hide_sheet()
        self.setFocus()
        super().mousePressEvent(event)

    # --- doing things ---
    def _changed(self) -> None:
        self.refresh()
        self.changed.emit()

    def do(self, action: str) -> bool:
        """Carry out a key of the keypad or the keyboard; False when it was refused."""
        calculator = self.calculator
        engine = calculator.engine
        done = True
        if action == "ms":
            done = calculator.memory_store()
        elif action == "mr":
            done = calculator.memory_recall()
        elif action == "m+":
            done = calculator.memory_add()
        elif action == "m-":
            done = calculator.memory_subtract()
        elif action == "mc":
            calculator.memory_clear()
        elif action == "second":
            self.second = not self.second
        elif action == "angle":
            order = list(ANGLE_LABELS)
            return self.do(order[(order.index(engine.angle) + 1) % len(order)])
        elif action in ANGLE_LABELS:
            if self.mode != SCIENTIFIC:
                return False
            engine.set_angle(action)
            self.state["angle"] = action
            self.settings_changed.emit()
        elif action == "next_word":
            sizes = list(WORD_LABELS)
            return self.do(WORD_LABELS[sizes[(sizes.index(engine.bits) + 1) % len(sizes)]].lower())
        elif action in _WORD_ACTIONS:
            if self.mode != PROGRAMMER:
                return False
            engine.set_word_size(_WORD_ACTIONS[action])
            self.state["bits"] = engine.bits
            self.settings_changed.emit()
        elif action in _RADIX_ACTIONS:
            if self.mode != PROGRAMMER:
                return False
            engine.set_radix(_RADIX_ACTIONS[action])
            self.state["radix"] = engine.radix
            self.settings_changed.emit()
        else:
            done = calculator.press(action)
            if self.second and action in {alternate[1] for _normal, alternate in SECOND.values()}:
                self.second = False              # as on the Windows keypad, 2nd applies to one key
        self._changed()
        return done

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.calculator.copy_text())

    def paste(self) -> bool:
        done = self.calculator.paste(QGuiApplication.clipboard().text())
        self._changed()
        return done

    # --- keyboard ---
    def action_for_key(self, key: int, text: str, modifiers: Qt.KeyboardModifier) -> str | None:
        """The action a key press stands for in the current mode (None: not a calculator key)."""
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if modifiers & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            return None
        mode = self.mode
        keys = Qt.Key
        if ctrl:
            table = {keys.Key_M: "ms", keys.Key_P: "m+", keys.Key_Q: "m-", keys.Key_R: "mr", keys.Key_L: "mc",
                     keys.Key_C: "copy", keys.Key_Insert: "copy", keys.Key_V: "paste"}
            if mode == SCIENTIFIC:
                table.update({keys.Key_Y: "root", keys.Key_N: "powe", keys.Key_G: "pow10",
                              keys.Key_S: "asinh" if shift else "sinh", keys.Key_O: "acosh" if shift else "cosh",
                              keys.Key_T: "atanh" if shift else "tanh", keys.Key_U: "asech" if shift else "sech",
                              keys.Key_I: "acsch" if shift else "csch", keys.Key_J: "acoth" if shift else "coth"})
            return table.get(key)
        if shift and key == keys.Key_Insert:
            return "paste"
        fixed = {keys.Key_Return: "=", keys.Key_Enter: "=", keys.Key_Escape: "c", keys.Key_Delete: "ce",
                 keys.Key_Backspace: "back", keys.Key_F9: "sign"}
        if key in fixed:
            return fixed[key]
        if mode == SCIENTIFIC and key in (keys.Key_F3, keys.Key_F4, keys.Key_F5):
            return {keys.Key_F3: numeric.DEG, keys.Key_F4: numeric.RAD, keys.Key_F5: numeric.GRAD}[key]
        if mode == PROGRAMMER:
            function = {keys.Key_F5: "hex", keys.Key_F6: "dec", keys.Key_F7: "oct", keys.Key_F8: "bin",
                        keys.Key_F12: "qword", keys.Key_F2: "dword", keys.Key_F3: "word", keys.Key_F4: "byte"}
            if key in function:
                return function[key]
        if not text or len(text) != 1:
            return None
        if text.isdigit() and text.isascii():
            return text
        symbols = {"+": "+", "-": "-", MINUS: "-", "*": "*", TIMES: "*", "/": "/", DIVIDE: "/", "=": "="}
        if text in symbols:
            return symbols[text]
        letter = text.upper()
        if mode == PROGRAMMER:
            if letter in "ABCDEF" and self.calculator.engine.radix == 16:
                return letter
            return {"%": "mod", "&": "and", "|": "or", "^": "xor", "~": "not", "<": self.left_shift_key.command,
                    ">": self.right_shift_key.command, "(": "(", ")": ")", ".": "nand", "\\": "nor",
                    "J": "rol", "K": "ror"}.get(letter)
        if text in (".", ",", self.calculator.decimal_point):
            return "."
        if mode == STANDARD:
            return {"%": "percent", "@": "sqrt", "R": "reciprocal", "Q": "sqr"}.get(letter)
        inverse = text.isupper() and shift
        table = {"%": "mod", "D": "mod", "@": "sqrt", "R": "reciprocal", "Q": "sqr", "#": "cube", "(": "(",
                 ")": ")", "^": "pow", "Y": "pow", "!": "fact", "L": "logbasey" if inverse else "log", "N": "ln",
                 "G": "pow2", "P": "pi", "E": "e", "X": "exp", "V": "fe", "M": "dms", "|": "abs", "[": "floor",
                 "]": "ceil", "S": "asin" if inverse else "sin", "O": "acos" if inverse else "cos",
                 "T": "atan" if inverse else "tan", "U": "asec" if inverse else "sec",
                 "I": "acsc" if inverse else "csc", "J": "acot" if inverse else "cot"}
        return table.get(letter)

    def handle_key(self, key: int, text: str, modifiers: Qt.KeyboardModifier) -> bool:
        """A key press from the window; True when it was a calculator key."""
        if key == Qt.Key.Key_Escape and self.sheet.isVisible():
            self.hide_sheet()
            return True
        action = self.action_for_key(key, text, modifiers)
        if action is None:
            return False
        if action == "copy":
            self.copy()
        elif action == "paste":
            self.paste()
        else:
            self.do(action)
            button = self._keys[self.mode].get(action)
            if button is not None and button.isVisible():
                button.setDown(True)             # the key on the screen answers the key on the keyboard
                button.repaint()
                button.setDown(False)
        return True

    def keyPressEvent(self, event) -> None:
        if not self.handle_key(event.key(), event.text(), event.modifiers()):
            event.ignore()

    # --- showing the state ---
    def refresh(self) -> None:
        calculator = self.calculator
        engine = calculator.engine
        mode = self.mode
        error = engine.error is not None
        self.expression_label.set_text(engine.expression_text)
        self.display.set_text(engine.display)
        for command, button in self._keys[mode].items():
            button.setEnabled(engine.accepts(command) and (not error or command in ("c", "ce")))
        memory = self._memory_buttons[mode]
        has_memory = bool(calculator.memory)
        for command, button in memory.items():
            if command in ("mc", "mr"):
                button.setEnabled(has_memory and not error)
            elif command == "memory":
                button.setEnabled(has_memory)
                button.setVisible(self.narrow)
            else:
                button.setEnabled(not error)
        clear = self._clear_keys.get(mode)
        if clear is not None:
            entry = engine.recording and not engine.input.is_empty()
            clear.command = "ce" if entry else "c"
            clear.set_label("CE" if entry else "C", "Clear entry (Delete)" if entry else "Clear (Esc)")
        open_key = self._open_keys.get(mode)
        if open_key is not None:
            depth = engine.parenthesis_depth
            open_key.set_label("(" + (str(depth).translate(_SUBSCRIPT) if depth else ""),
                               f"Open parenthesis ({depth} open)" if depth else "Open parenthesis")
        if mode == SCIENTIFIC:
            self.angle_button.set_label(ANGLE_LABELS[engine.angle],
                                        f"{ANGLE_NAMES[engine.angle]} (F3 degrees, F4 radians, F5 gradians)")
            self.fe_button.setChecked(engine.scientific_notation)
            self.second_button.setChecked(self.second)
            for name, button in self._second_keys.items():
                label, command, tooltip = SECOND[name][1 if self.second else 0]
                button.command = command
                button.set_label(label, tooltip)
                button.setEnabled(not error)
            for button in (self.angle_button, self.fe_button, self.trig_button, self.function_button,
                           self.second_button):
                button.setEnabled(not error)
        elif mode == PROGRAMMER:
            for radix, row in self.radix_rows.items():
                row.set_value(engine.display_in(radix) if not error else "")
                row.setChecked(radix == engine.radix)
                row.setEnabled(not error)
            self.word_button.set_label(WORD_LABELS[engine.bits], "Word size: click for the next (F12, F2, F3, F4)")
            left, right = SHIFT_COMMANDS[self.state["shift"]]
            rotate = self.state["shift"].startswith("rotate")
            self.left_shift_key.command, self.right_shift_key.command = left, right
            self.left_shift_key.set_label("RoL" if rotate else "<<",
                                          SHIFT_LABELS[self.state["shift"]] + " left")
            self.right_shift_key.set_label("RoR" if rotate else ">>",
                                           SHIFT_LABELS[self.state["shift"]] + " right")
            for button in (self.left_shift_key, self.right_shift_key, self.bitwise_button, self.word_button):
                button.setEnabled(not error)
            self.bit_pad.set_value(int(engine.value) if not error else 0, engine.bits)
            self.bit_pad.setEnabled(not error)
        if self.sheet.isVisible():
            self.sheet.refresh()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.Hide:
            self._close_flyout()
        return super().event(event)
