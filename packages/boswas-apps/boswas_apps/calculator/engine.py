"""The calculator as a machine driven by key presses (standard library only, no Qt).

``Engine.press(command)`` takes one key at a time and behaves the way the
calculator of Windows does, whose rules are published with its source:

* **Standard** mode executes immediately: ``2 + 3 × 4 =`` is 20, because
  the pending addition is carried out when ``×`` is pressed.
* **Scientific** and **Programmer** mode honour precedence (``2 + 3 × 4 =``
  is 14) and parentheses; lower operators wait on a stack.
* Pressing an operator twice replaces the operator. ``=`` again repeats the
  last operation with the same second operand. An operator followed by
  ``=`` uses the first operand twice (``5 + =`` is 10).
* ``%`` after ``+`` or ``−`` is a percentage of the first operand
  (``50 + 10 % =`` is 55), after ``×`` or ``÷`` a plain hundredth
  (``200 × 10 % =`` is 20), and without an operator 0.
* Backspace edits a number that is being typed and does nothing to a
  result. ``±`` changes the sign of the number being typed (of its exponent
  after ``exp``), and negates a result.
* An error (``Cannot divide by zero``, ``Invalid input``, ``Overflow``,
  ``Result is undefined``) locks the calculator until ``C`` (or ``CE``).

Commands are short strings: the digits ``0``-``9`` and ``A``-``F``, ``.``,
``exp``, ``sign``, ``back``, ``c``, ``ce``, ``=``, ``(``, ``)``, the binary
operators of ``BINARY`` and the functions of ``UNARY``, the constants ``pi``,
``e`` and ``rand``, and ``fe`` (the F-E key).

In Programmer mode a value is the bit pattern of the current word size (an
int from 0 to 2**bits - 1); the decimal display reads it in two's
complement. In the other modes a value is a ``Fraction`` (see ``numeric``).
"""

from __future__ import annotations

import random
from fractions import Fraction
from typing import Callable

from . import numeric
from .numeric import CalcError

STANDARD, SCIENTIFIC, PROGRAMMER = "standard", "scientific", "programmer"
MODES = (STANDARD, SCIENTIFIC, PROGRAMMER)

DIGITS = "0123456789ABCDEF"
PRECEDENCE = {"or": 0, "xor": 0, "and": 1, "nand": 1, "nor": 1, "+": 2, "-": 2, "lsh": 3, "rsh": 3, "rshl": 3,
              "mod": 3, "/": 3, "*": 3, "pow": 4, "root": 4, "logbasey": 4}
BINARY = frozenset(PRECEDENCE)
INTEGER_BINARY = frozenset(("or", "xor", "and", "nand", "nor", "+", "-", "lsh", "rsh", "rshl", "mod", "/", "*"))
DECIMAL_BINARY = frozenset(("+", "-", "*", "/", "mod", "pow", "root", "logbasey"))
TRIGONOMETRIC = ("sin", "cos", "tan", "sec", "csc", "cot")
ANGLE_FUNCTIONS = frozenset(TRIGONOMETRIC) | frozenset("a" + name for name in TRIGONOMETRIC)
HYPERBOLIC = frozenset(name + "h" for name in TRIGONOMETRIC) | frozenset("a" + name + "h" for name in TRIGONOMETRIC)
DECIMAL_UNARY = (frozenset(("percent", "reciprocal", "sqr", "sqrt", "cube", "cuberoot", "pow10", "pow2", "powe",
                            "log", "ln", "fact", "abs", "floor", "ceil", "dms", "degrees"))
                 | ANGLE_FUNCTIONS | HYPERBOLIC)
INTEGER_UNARY = frozenset(("not", "rol", "ror", "rolc", "rorc"))
UNARY = DECIMAL_UNARY | INTEGER_UNARY

BINARY_SYMBOLS = {"+": "+", "-": "−", "*": "×", "/": "÷", "mod": "Mod", "pow": "^", "root": "yroot",
                  "logbasey": "log base", "and": "AND", "or": "OR", "xor": "XOR", "nand": "NAND", "nor": "NOR",
                  "lsh": "Lsh", "rsh": "Rsh", "rshl": "Rsh"}
UNARY_NAMES = {"reciprocal": "1/", "sqr": "sqr", "sqrt": "√", "cube": "cube", "cuberoot": "cuberoot",
               "pow10": "10^", "pow2": "2^", "powe": "e^", "log": "log", "ln": "ln", "fact": "fact", "abs": "abs",
               "floor": "floor", "ceil": "ceil", "dms": "dms", "degrees": "degrees", "negate": "negate",
               "not": "NOT", "rol": "RoL", "ror": "RoR", "rolc": "RoL", "rorc": "RoR"}
_ANGLE_MARK = {numeric.DEG: "₀", numeric.RAD: "ᵣ", numeric.GRAD: "₉"}

MAX_DEPTH = 25                 # open parentheses, and operators waiting for a higher one
MAX_EXPONENT_DIGITS = 4
WORD_SIZES = (64, 32, 16, 8)
RADIXES = (16, 10, 8, 2)

_FUNCTIONS: dict[str, Callable[[Fraction], Fraction]] = {
    "reciprocal": numeric.reciprocal,
    "sqr": lambda x: numeric.multiply(x, x),
    "sqrt": numeric.sqrt,
    "cube": lambda x: numeric.power(x, Fraction(3)),
    "cuberoot": numeric.cube_root,
    "pow10": lambda x: numeric.power(Fraction(10), x),
    "pow2": lambda x: numeric.power(Fraction(2), x),
    "powe": numeric.exp,
    "log": numeric.log10,
    "ln": numeric.ln,
    "fact": numeric.factorial,
    "abs": lambda x: abs(x),
    "floor": numeric.floor,
    "ceil": numeric.ceil,
    "dms": numeric.to_dms,
    "degrees": numeric.from_dms,
    "sinh": numeric.sinh, "cosh": numeric.cosh, "tanh": numeric.tanh,
    "sech": numeric.sech, "csch": numeric.csch, "coth": numeric.coth,
    "asinh": numeric.asinh, "acosh": numeric.acosh, "atanh": numeric.atanh,
    "asech": numeric.asech, "acsch": numeric.acsch, "acoth": numeric.acoth,
}
_ANGLE_FUNCTIONS: dict[str, Callable[[Fraction, str], Fraction]] = {
    "sin": numeric.sin, "cos": numeric.cos, "tan": numeric.tan,
    "sec": numeric.sec, "csc": numeric.csc, "cot": numeric.cot,
    "asin": numeric.asin, "acos": numeric.acos, "atan": numeric.atan,
    "asec": numeric.asec, "acsc": numeric.acsc, "acot": numeric.acot,
}


def unary_label(command: str, angle: str = numeric.DEG) -> str:
    """How a function is written in the expression line: ``√``, ``sqr``, ``sin₀`` (degrees), ``sinh⁻¹``."""
    if command in UNARY_NAMES:
        return UNARY_NAMES[command]
    inverse = command.startswith("a")
    name = command[1:] if inverse else command
    if name in TRIGONOMETRIC:
        name += _ANGLE_MARK[angle]
    return name + ("⁻¹" if inverse else "")


# --- the number being typed -------------------------------------------------------------------------------

class DecimalInput:
    """A decimal number as it is typed: digits, one decimal point, an optional exponent of up to 4 digits."""

    def __init__(self, max_digits: int) -> None:
        self.max_digits = max_digits
        self.clear()

    def clear(self) -> None:
        self.base = ""                  # digits and at most one "."
        self.negative = False
        self.has_point = False
        self.has_exponent = False
        self.exponent = ""
        self.exponent_negative = False

    def is_empty(self) -> bool:
        return not self.base

    def add_digit(self, digit: int) -> bool:
        if digit > 9:
            return False
        if self.has_exponent:
            if not self.exponent and digit == 0:
                return True
            if len(self.exponent) < MAX_EXPONENT_DIGITS:
                self.exponent += str(digit)
                return True
            return False
        if not self.base and digit == 0:
            return True                 # leading zeros are not kept
        limit = self.max_digits + (1 if self.has_point else 0) + (1 if self.base[:1] == "0" else 0)
        if len(self.base) < limit:
            self.base += str(digit)
            return True
        return False

    def add_point(self) -> bool:
        if self.has_point or self.has_exponent:
            return False
        if not self.base:
            self.base = "0"
        self.base += "."
        self.has_point = True
        return True

    def begin_exponent(self) -> bool:
        if self.has_exponent:
            return False
        self.add_point()                # 5 exp is shown as 5.e+0
        self.has_exponent = True
        return True

    def toggle_sign(self) -> bool:
        if not self.base:
            self.negative = self.exponent_negative = False      # zero stays positive
        elif self.has_exponent:
            self.exponent_negative = not self.exponent_negative
        else:
            self.negative = not self.negative
        return True

    def backspace(self) -> None:
        if self.has_exponent:
            if self.exponent:
                self.exponent = self.exponent[:-1]
                if not self.exponent:
                    self.exponent_negative = False
            else:
                self.has_exponent = False
            return
        if self.base:
            self.base = self.base[:-1]
            if self.base == "0":
                self.base = ""
        self.has_point = "." in self.base
        if not self.base:
            self.negative = False

    def value(self) -> Fraction:
        whole, _, fraction = self.base.partition(".")
        number = Fraction(int(whole + fraction or "0"), 10 ** len(fraction))
        if self.has_exponent and self.exponent:
            number *= Fraction(10) ** (-int(self.exponent) if self.exponent_negative else int(self.exponent))
        return -number if self.negative else number

    def text(self, decimal_point: str, group_separator: str) -> str:
        whole, _, fraction = (self.base or "0").partition(".")
        text = ("-" if self.negative else "") + numeric.group_digits(whole, group_separator)
        if self.has_point:
            text += decimal_point + fraction
        if self.has_exponent:
            text += f"e{'-' if self.exponent_negative else '+'}{self.exponent or '0'}"
        return text


class IntegerInput:
    """An integer as it is typed in a radix; a digit that would leave the word size is refused."""

    def __init__(self, radix: int, bits: int) -> None:
        self.radix, self.bits = radix, bits
        self.clear()

    def clear(self) -> None:
        self.digits = ""
        self.negative = False

    def is_empty(self) -> bool:
        return not self.digits

    def _fits(self, digits: str, negative: bool) -> bool:
        magnitude = int(digits, self.radix) if digits else 0
        if self.radix != 10:
            return magnitude < (1 << self.bits)
        limit = 1 << (self.bits - 1)
        return magnitude <= limit if negative else magnitude < limit

    def add_digit(self, digit: int) -> bool:
        if digit >= self.radix:
            return False
        if not self.digits and digit == 0:
            return True
        candidate = self.digits + DIGITS[digit]
        if not self._fits(candidate, self.negative):
            return False
        self.digits = candidate
        return True

    def add_point(self) -> bool:
        return False

    def begin_exponent(self) -> bool:
        return False

    def toggle_sign(self) -> bool:
        if not self.digits:
            self.negative = False
            return True
        if not self._fits(self.digits, not self.negative):
            return False                # -128 in a byte has no positive counterpart
        self.negative = not self.negative
        return True

    def backspace(self) -> None:
        self.digits = self.digits[:-1]
        if not self.digits:
            self.negative = False

    def value(self) -> int:
        magnitude = int(self.digits, self.radix) if self.digits else 0
        return (-magnitude if self.negative else magnitude) & ((1 << self.bits) - 1)

    def text(self, _decimal_point: str, group_separator: str) -> str:
        return ("-" if self.negative else "") + group_radix(self.digits or "0", self.radix, group_separator)


def group_radix(digits: str, radix: int, group_separator: str = ",") -> str:
    """Digits of a radix grouped for reading: hexadecimal and binary in fours, octal in threes (by spaces)."""
    if radix == 10:
        return numeric.group_digits(digits, group_separator)
    return numeric.group_digits(digits, " ", 3 if radix == 8 else 4)


def to_signed(pattern: int, bits: int) -> int:
    """The two's complement reading of a bit pattern."""
    return pattern - (1 << bits) if pattern >> (bits - 1) & 1 else pattern


def format_integer(pattern: int, radix: int, bits: int, group_separator: str = ",", *, grouped: bool = True) -> str:
    """A bit pattern in a radix: decimal is signed (two's complement), the others show the bits."""
    if radix == 10:
        value = to_signed(pattern, bits)
        digits = str(abs(value))
        return ("-" if value < 0 else "") + (numeric.group_digits(digits, group_separator) if grouped else digits)
    digits = {16: format(pattern, "X"), 8: format(pattern, "o"), 2: format(pattern, "b")}[radix]
    return group_radix(digits, radix) if grouped else digits


# --- the expression line -----------------------------------------------------------------------------------

class Expression:
    """What has been keyed in so far, written as a formula (``2 + √(9) ×``), for the line above the result."""

    def __init__(self, on_complete: Callable[[str, str], None] | None = None) -> None:
        self.on_complete = on_complete
        self.completed: str | None = None       # the finished line that stays visible after "="
        self._reset()

    def _reset(self) -> None:
        self.tokens: list[str] = []
        self.operand_start = -1
        self.binop_start = -1
        self.operand_starts: list[int] = []

    @property
    def operand_added(self) -> bool:
        return self.operand_start != -1

    def text(self) -> str:
        if self.completed is not None:
            return self.completed
        return "".join(self.tokens).strip()

    def _touched(self) -> None:
        self.completed = None

    def add_operand(self, text: str) -> None:
        self._touched()
        self.operand_start = len(self.tokens)
        self.tokens.append(text)
        self.binop_start = -1

    def remove_last_operand(self) -> None:
        self._touched()
        if self.operand_start != -1:
            del self.tokens[self.operand_start:]
        self.operand_start = -1

    def add_binary(self, symbol: str) -> None:
        self._touched()
        self.binop_start = len(self.tokens)
        self.tokens.append(f" {symbol} ")
        self.operand_start = -1

    def change_last_binary(self, symbol: str, enclose: bool) -> None:
        if self.binop_start != -1:
            del self.tokens[self.binop_start:]
        if enclose:
            self.enclose()
        self.add_binary(symbol)

    def add_unary(self, label: str) -> None:
        self._touched()
        start = self.operand_start if self.operand_start != -1 else len(self.tokens)
        self.tokens.insert(start, f"{label}(")
        self.tokens.append(")")
        self.operand_start = start
        self.binop_start = -1

    def push_operand_start(self, index: int | None = None) -> None:
        if len(self.operand_starts) < MAX_DEPTH * 2:
            self.operand_starts.append(self.operand_start if index is None else index)

    def pop_operand_start(self) -> None:
        if self.operand_starts:
            self.operand_start = self.operand_starts.pop()

    def open_parenthesis(self) -> None:
        self._touched()
        self.push_operand_start(len(self.tokens))
        self.tokens.append("(")

    def close_parenthesis(self) -> None:
        self._touched()
        self.tokens.append(")")
        self.pop_operand_start()
        self.binop_start = -1

    def enclose(self) -> None:
        """Put what stands after the last remembered operand start in parentheses (a precedence inversion)."""
        start = self.operand_starts[-1] if self.operand_starts else 0
        self.tokens.insert(max(0, start), "(")
        self.tokens.append(")")

    def complete(self, result: str, *, equals: bool = True) -> None:
        """Finish the line: it goes to the history with ``result`` and stays visible until the next key."""
        self.tokens.append(" =")
        text = "".join(self.tokens).strip()
        self._reset()
        self.completed = text if equals else None
        if self.on_complete is not None:
            self.on_complete(text, result)

    def clear(self) -> None:
        self._reset()
        self.completed = None


# --- the machine --------------------------------------------------------------------------------------------

class Engine:
    """One calculator: Standard, Scientific or Programmer. See the module text for the rules."""

    def __init__(self, mode: str = STANDARD, *, decimal_point: str = ".", group_separator: str = ",",
                 rng: Callable[[], float] | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.integer = mode == PROGRAMMER
        self.precedence = mode != STANDARD
        self.precision = 16 if mode == STANDARD else 32
        self.decimal_point = decimal_point
        self.group_separator = group_separator
        self.angle = numeric.DEG
        self.scientific_notation = False            # the F-E key
        self.radix = 10
        self.bits = 64
        self.carry = 0
        self.history: list[tuple[str, str, object]] = []      # (expression, result text, value), newest last
        self.on_history: Callable[[str, str, object], None] | None = None
        self._rng = rng or random.random
        self.expression = Expression(self._equation_completed)
        self.input: DecimalInput | IntegerInput = self._new_input()
        self._zero: Fraction | int = 0 if self.integer else Fraction(0)
        self.current: Fraction | int = self._zero
        self.error: str | None = None
        self._reset_state()

    # --- state ---
    def _new_input(self) -> DecimalInput | IntegerInput:
        if self.integer:
            return IntegerInput(self.radix, self.bits)
        return DecimalInput(self.precision)

    def _reset_state(self) -> None:
        self.last: Fraction | int = self._zero       # the operand left of the pending operator
        self.hold: Fraction | int = self._zero       # the operand "=" repeats with
        self.op: str | None = None                   # the pending operator
        self.prev_op: str | None = None
        self.change_op = False                       # an operator is pending and wants its second operand
        self.no_prev_equ = True
        self.recording = False
        self.last_command: str | None = None
        self._this_command: str | None = None
        self._waiting: list[tuple[str | None, Fraction | int]] = []     # (None, ...) marks a "("
        self._parentheses: list[tuple[str | None, Fraction | int]] = []

    @property
    def parenthesis_depth(self) -> int:
        return len(self._parentheses)

    @property
    def mask(self) -> int:
        return (1 << self.bits) - 1

    # --- text ---
    def format(self, value: Fraction | int, *, radix: int | None = None, grouped: bool = True) -> str:
        """A value as the display writes it."""
        if self.integer:
            return format_integer(int(value) & self.mask, radix or self.radix, self.bits,
                                  self.group_separator, grouped=grouped)
        notation = numeric.SCIENTIFIC if self.scientific_notation else numeric.FLOAT
        return numeric.format_number(Fraction(value), self.precision, notation, decimal_point=self.decimal_point,
                                     group_separator=self.group_separator if grouped else "")

    @property
    def display(self) -> str:
        """The main line: the error, the number being typed, or the current value."""
        if self.error is not None:
            return self.error
        if self.recording:
            return self.input.text(self.decimal_point, self.group_separator)
        return self.format(self.current)

    @property
    def expression_text(self) -> str:
        return self.expression.text()

    @property
    def value(self) -> Fraction | int:
        """The number the display shows (the number being typed counts)."""
        return self.input.value() if self.recording else self.current

    def display_in(self, radix: int) -> str:
        """The current value in another radix (Programmer mode shows all four)."""
        return format_integer(int(self.value) & self.mask, radix, self.bits, self.group_separator)

    def _equation_completed(self, text: str, result: str) -> None:
        item = (text, result, self.current)
        self.history.append(item)
        if self.on_history is not None:
            self.on_history(*item)

    # --- commands that are settings ---
    def _stop_recording(self) -> None:
        if self.recording:
            self.recording = False
            self.current = self.input.value()

    def commit(self) -> Fraction | int:
        """End the number being typed (the memory keys do this) and return the current value."""
        if self.error is None:
            self._stop_recording()
        return self.current

    def set_angle(self, unit: str) -> None:
        if unit not in numeric.ANGLE_UNITS:
            raise ValueError(f"unknown angle unit {unit!r}")
        if self.error is None:
            self._stop_recording()
        self.angle = unit

    def set_radix(self, radix: int) -> None:
        if radix not in RADIXES:
            raise ValueError(f"unsupported radix {radix!r}")
        if self.error is None:
            self._stop_recording()
        self.radix = radix
        self.input = self._new_input()

    def set_word_size(self, bits: int) -> None:
        """Change the word size: a smaller one keeps the low bits, a larger one extends the sign."""
        if bits not in WORD_SIZES:
            raise ValueError(f"unsupported word size {bits!r}")
        if self.error is None:
            self._stop_recording()
        old = self.bits
        self.bits = bits
        if self.integer:
            def resize(value):
                return to_signed(int(value) & ((1 << old) - 1), old) & ((1 << bits) - 1)
            self.current, self.last, self.hold = resize(self.current), resize(self.last), resize(self.hold)
            self._waiting = [(op, resize(value) if op is not None else value) for op, value in self._waiting]
            self._parentheses = [(op, resize(value)) for op, value in self._parentheses]
        self.input = self._new_input()

    def recall(self, value: Fraction | int) -> None:
        """Put a value on the display as if it had been typed (MR, a history entry, paste)."""
        if self.error is not None:
            return
        self._begin("recall")
        self.expression.completed = None
        self._stop_recording()
        self._check_and_add_last_binary()
        self.current = (int(value) & self.mask) if self.integer else numeric.normalize(Fraction(value))

    def toggle_bit(self, index: int) -> None:
        """Flip one bit of the value (the bit keypad of Programmer mode)."""
        if not self.integer or self.error is not None or not 0 <= index < self.bits:
            return
        self._begin("bit")
        self.expression.completed = None
        self._stop_recording()
        if self.last_command in BINARY:
            self.current = self.last
        self._check_and_add_last_binary()
        self.current = (int(self.current) ^ (1 << index)) & self.mask

    # --- pressing a key ---
    def accepts(self, command: str) -> bool:
        """Whether a command exists in this mode (and radix): the keypad disables what does not."""
        if command in DIGITS and len(command) == 1:
            return DIGITS.index(command) < (self.radix if self.integer else 10)
        if self.integer:
            return (command in INTEGER_BINARY or command in INTEGER_UNARY
                    or command in ("sign", "back", "c", "ce", "=", "(", ")"))
        if self.mode == STANDARD:
            return command in ("+", "-", "*", "/", "percent", "reciprocal", "sqr", "sqrt", ".", "sign", "back", "c",
                               "ce", "=")
        return (command in DECIMAL_BINARY or command in DECIMAL_UNARY
                or command in (".", "exp", "sign", "back", "c", "ce", "=", "(", ")", "pi", "e", "rand", "fe"))

    def press(self, command: str) -> bool:
        """Press one key. Returns False when the key was refused (what Windows answers with a sound)."""
        if not self.accepts(command):
            return False
        try:
            return self._process(command)
        except CalcError as error:
            self.error = error.message
            self.recording = False
            return False

    def press_all(self, commands: str | list[str]) -> "Engine":
        """Press several keys: a list, or text with the commands separated by spaces.

        A number may be written in one piece: ``"12.5 + 3 ="`` presses 1, 2, the point, 5, and so on.
        """
        for command in (commands.split() if isinstance(commands, str) else commands):
            if len(command) > 1 and all(character in DIGITS or character == "." for character in command):
                for character in command:
                    self.press(character)
            else:
                self.press(command)
        return self

    def _begin(self, command: str) -> None:
        self.last_command = self._this_command
        self._this_command = command

    def _process(self, command: str) -> bool:
        if command not in ("fe", "back", "exp"):          # these are not operations: the last command stays
            self._begin(command)
        if self.error is not None:
            if command not in ("c", "ce"):
                return False
            command = "c"                                 # after an error CE clears everything too
        self.expression.completed = None

        is_digit = len(command) == 1 and command in DIGITS
        if self.recording:
            if not (is_digit or command in (".", "exp", "back", "c", "ce")
                    or (command == "sign" and not (self.integer and self.radix != 10))):
                self._stop_recording()
        elif is_digit or command == ".":
            self.recording = True
            self.input.clear()
            self._check_and_add_last_binary()

        if is_digit:
            return self.input.add_digit(DIGITS.index(command))
        if command in BINARY:
            return self._binary(command)
        if command in UNARY:
            return self._unary(command)
        handler = self._HANDLERS[command]
        return handler(self)

    # --- operators ---
    def _operand_text(self) -> str:
        return self.format(self.current)

    def _add_operand_if_missing(self) -> None:
        if not self.expression.operand_added:
            self.expression.add_operand(self._operand_text())

    def _binary_symbol(self, command: str) -> str:
        if command == "mod" and self.integer:
            return "%"
        return BINARY_SYMBOLS[command]

    def _binary(self, command: str) -> bool:
        if self.last_command in BINARY:
            # An operator right after an operator replaces it.
            enclose = False
            self.op = command
            if self.precedence and self.prev_op:
                previous = PRECEDENCE[self.prev_op]
                if PRECEDENCE[self.last_command] <= previous < PRECEDENCE[command]:
                    enclose = True
                    self.prev_op = None
            self.expression.change_last_binary(self._binary_symbol(command), enclose)
            return True
        self._add_operand_if_missing()
        if self.change_op:
            while True:
                waiting = PRECEDENCE[command]
                pending = PRECEDENCE[self.op]
                if waiting > pending and self.precedence:
                    if len(self._waiting) >= MAX_DEPTH:
                        return False
                    self._waiting.append((self.op, self.last))
                    self.expression.push_operand_start()
                    break
                self.current = self._operate(self.op, self.current, self.last)
                self.prev_op = self.op
                if not self.precedence:
                    text = self._operand_text()
                    self.expression.complete(text)
                    self.expression.add_operand(text)
                if self._waiting and self._waiting[-1][0] is not None:
                    self.op, self.last = self._waiting.pop()
                    if pending <= PRECEDENCE[self.op]:
                        self.expression.enclose()
                    self.expression.pop_operand_start()
                    continue
                break
        self.last = self.current
        self.op = command
        self.expression.add_binary(self._binary_symbol(command))
        self.no_prev_equ = self.change_op = True
        return True

    def _unary(self, command: str) -> bool:
        if self.last_command in BINARY:
            self.current = self.last               # 5 + √ takes the root of 5
        if command != "percent":
            self._add_operand_if_missing()
            self.expression.add_unary(unary_label(command, self.angle))
        self.current = self._function(command, self.current)
        if command == "percent":
            self._check_and_add_last_binary()
            self.expression.add_operand(self._operand_text())
        return True

    def _sign(self) -> bool:
        if self.recording:
            return self.input.toggle_sign()
        if self.last_command in BINARY:
            self.current = self.last
        self._add_operand_if_missing()
        self.current = (-int(self.current)) & self.mask if self.integer else -self.current
        self.expression.add_unary(UNARY_NAMES["negate"])
        return True

    def _resolve(self) -> None:
        """Carry out the pending operation; a second "=" in a row repeats it with the held operand."""
        if self.op is None:
            return
        if self.no_prev_equ:
            self.hold = self.current
        else:
            self.current = self.hold
            self.expression.add_binary(self._binary_symbol(self.op))
            self.expression.add_operand(self._operand_text())
        self.current = self._operate(self.op, self.current, self.last)
        self.prev_op = self.op
        self.last = self.current
        self.no_prev_equ = False

    def _equals(self) -> bool:
        while self._parentheses:
            # Open parentheses are closed for the user.
            self._this_command = self.last_command
            self._process(")")
            self.last_command = self._this_command
            self._this_command = "="
        if not self.no_prev_equ:
            self.last = self.current
        if self.last_command in BINARY:
            self.current = self.last               # 5 + = is 5 + 5
        self._add_operand_if_missing()
        self._resolve()
        while self.precedence and self._waiting:
            self.op, self.last = self._waiting.pop()
            if PRECEDENCE.get(self.prev_op, 0) <= PRECEDENCE.get(self.op, 0):
                self.expression.enclose()
            self.expression.pop_operand_start()
            self.no_prev_equ = True
            self._resolve()
        self.expression.complete(self._operand_text())
        self.change_op = False
        self.prev_op = None
        return True

    def _open_parenthesis(self) -> bool:
        if len(self._parentheses) >= MAX_DEPTH or (len(self._waiting) >= MAX_DEPTH
                                                    and self._waiting[-1][0] is not None):
            return False
        previous = self.last_command
        if previous is not None and ((len(previous) == 1 and previous in DIGITS) or previous in UNARY
                                     or previous in (".", ")")):
            self._process("*")                     # 2 ( 3 ) is 2 × (3)
        self._check_and_add_last_binary()
        self.expression.open_parenthesis()
        self._parentheses.append((self.op if self.change_op else None, self.last))
        self._waiting.append((None, self._zero))
        self.last = self._zero
        if self.last_command in BINARY:
            self.current = self._zero
        self._this_command = None
        self.op = None
        self.change_op = False
        return True

    def _close_parenthesis(self) -> bool:
        if not self._parentheses:
            return False
        if self.last_command in BINARY:
            self.current = self.last
        self._add_operand_if_missing()
        self.current = self._operate(self.op, self.current, self.last)
        self.prev_op = self.op
        while self._waiting:
            operator, value = self._waiting.pop()
            self.op = operator
            if operator is None:
                break
            if PRECEDENCE.get(self.prev_op, 0) <= PRECEDENCE[operator]:
                self.expression.enclose()
            self.expression.pop_operand_start()
            self.last = value
            self.current = self._operate(operator, self.current, self.last)
            self.prev_op = operator
        self.expression.close_parenthesis()
        self.op, self.last = self._parentheses.pop()
        self.change_op = self.op is not None
        return True

    def _check_and_add_last_binary(self, add_to_history: bool = True) -> None:
        """A new operand begins: drop or finish what stands in the expression in its place."""
        expression = self.expression
        if self.change_op:
            if expression.operand_added:
                expression.remove_last_operand()   # 1 + √(4) then a digit: the digit replaces √(4)
        elif expression.operand_added and self.error is None:
            finished = (self.last_command in UNARY or self.last_command in ("sign", ")")) and not self._parentheses
            if finished and self.last_command != "percent":
                if add_to_history:
                    expression.complete(self._operand_text(), equals=False)
                else:
                    expression.clear()
            else:
                expression.remove_last_operand()

    # --- other keys ---
    def _clear(self) -> bool:
        if not self.change_op:
            self._check_and_add_last_binary(False)
        self.current = self._zero
        self._reset_state()
        self.carry = 0
        self.expression.clear()
        self._clear_entry_state()
        return True

    def _clear_entry_state(self) -> None:
        self.input.clear()
        self.recording = True
        self._check_and_add_last_binary()
        self.error = None

    def _clear_entry(self) -> bool:
        self._clear_entry_state()
        return True

    def _backspace(self) -> bool:
        if not self.recording:
            return False                           # a result cannot be edited
        self.input.backspace()
        return True

    def _point(self) -> bool:
        return self.recording and self.input.add_point()

    def _exponent(self) -> bool:
        return self.recording and self.input.begin_exponent()

    def _constant(self, value: Fraction) -> bool:
        self._check_and_add_last_binary()
        self.current = value
        return True

    def _toggle_notation(self) -> bool:
        self.scientific_notation = not self.scientific_notation
        return True

    _HANDLERS: dict[str, Callable[["Engine"], bool]] = {
        "=": _equals, "(": _open_parenthesis, ")": _close_parenthesis, "sign": _sign, "c": _clear,
        "ce": _clear_entry, "back": _backspace, ".": _point, "exp": _exponent, "fe": _toggle_notation,
        "pi": lambda self: self._constant(numeric.PI),
        "e": lambda self: self._constant(numeric.E),
        "rand": lambda self: self._constant(numeric.normalize(Fraction(repr(float(self._rng()))))),
    }

    # --- arithmetic ---
    def _function(self, command: str, value: Fraction | int) -> Fraction | int:
        if self.integer:
            return self._integer_function(command, int(value))
        if command == "percent":
            if self.op in ("*", "/"):
                return numeric.divide(value, Fraction(100))
            return numeric.multiply(value, numeric.divide(self.last, Fraction(100)))
        if command in _ANGLE_FUNCTIONS:
            return _ANGLE_FUNCTIONS[command](value, self.angle)
        return numeric.normalize(_FUNCTIONS[command](value))

    def _integer_function(self, command: str, value: int) -> int:
        bits, mask = self.bits, self.mask
        value &= mask
        top, bottom = value >> (bits - 1) & 1, value & 1
        if command == "not":
            return value ^ mask
        if command == "rol":
            return ((value << 1) | top) & mask
        if command == "ror":
            return (value >> 1) | (bottom << (bits - 1))
        if command == "rolc":
            result = ((value << 1) | self.carry) & mask
            self.carry = top
            return result
        if command == "rorc":
            result = (value >> 1) | (self.carry << (bits - 1))
            self.carry = bottom
            return result
        raise CalcError(numeric.DOMAIN)

    def _operate(self, operator: str | None, right: Fraction | int, left: Fraction | int) -> Fraction | int:
        """``left operator right``: ``right`` is the number on the display, ``left`` the one before the operator."""
        if operator is None:
            return right
        if self.integer:
            return self._integer_operate(operator, int(left) & self.mask, int(right) & self.mask)
        left, right = Fraction(left), Fraction(right)
        if operator == "+":
            return numeric.add(left, right)
        if operator == "-":
            return numeric.subtract(left, right)
        if operator == "*":
            return numeric.multiply(left, right)
        if operator == "/":
            return numeric.divide(left, right)
        if operator == "mod":
            return numeric.modulo(left, right)
        if operator == "pow":
            return numeric.power(left, right)
        if operator == "root":
            return numeric.root(left, right)
        if operator == "logbasey":
            return numeric.log_base(left, right)
        raise CalcError(numeric.DOMAIN)

    def _integer_operate(self, operator: str, left: int, right: int) -> int:
        bits, mask = self.bits, self.mask
        if operator == "+":
            return (left + right) & mask
        if operator == "-":
            return (left - right) & mask
        if operator == "*":
            return (left * right) & mask
        if operator in ("/", "mod"):
            dividend, divisor = to_signed(left, bits), to_signed(right, bits)
            if divisor == 0:
                raise CalcError(numeric.DIVIDE_BY_ZERO if operator == "/" and dividend else numeric.UNDEFINED)
            quotient = abs(dividend) // abs(divisor)
            if (dividend < 0) != (divisor < 0):
                quotient = -quotient           # the quotient is cut towards zero
            if operator == "/":
                return quotient & mask
            return (dividend - quotient * divisor) & mask       # the remainder has the sign of the dividend
        if operator == "and":
            return left & right
        if operator == "or":
            return left | right
        if operator == "xor":
            return left ^ right
        if operator == "nand":
            return (left & right) ^ mask
        if operator == "nor":
            return (left | right) ^ mask
        if right >= bits:                      # a shift by the word size or more (or by a negative count)
            raise CalcError(numeric.NO_RESULT)
        if operator == "lsh":
            return (left << right) & mask
        if operator == "rsh":                  # arithmetic: the sign bit is copied in
            return (to_signed(left, bits) >> right) & mask
        if operator == "rshl":                 # logical: zeros are shifted in
            return left >> right
        raise CalcError(numeric.DOMAIN)
