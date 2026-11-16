"""The calculator behind the window: three engines, memory, history, copy and paste (no Qt).

``Calculator`` owns one ``Engine`` per mode. Switching the mode starts a new
calculation in that mode (as Windows does); memory and history stay.
Standard and Scientific share one memory list and each has its own history
of up to 20 calculations; Programmer has its own memory list (whole numbers
read in two's complement) and, as in Windows, no history.

Memory is a list, newest first: MS adds an entry, M+ and M− change the
newest (or create one when the list is empty), MR recalls the newest, MC
empties the list. The memory panel does the same to any entry by index.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from . import numeric
from .engine import MODES, PROGRAMMER, SCIENTIFIC, STANDARD, Engine, to_signed

MAX_HISTORY = 20
MAX_MEMORY = 100
MAX_PASTE = 400


@dataclass(frozen=True)
class HistoryItem:
    expression: str
    result: str
    value: Fraction


class Calculator:
    """The state of the three calculator modes. ``on_change`` is called after anything changed."""

    def __init__(self, *, decimal_point: str = ".", group_separator: str = ",", rng=None) -> None:
        self.decimal_point = decimal_point
        self.group_separator = group_separator
        self.engines = {mode: Engine(mode, decimal_point=decimal_point, group_separator=group_separator, rng=rng)
                        for mode in MODES}
        self.mode = STANDARD
        self.histories: dict[str, list[HistoryItem]] = {STANDARD: [], SCIENTIFIC: []}
        self._memory: list[Fraction] = []
        self._integer_memory: list[int] = []
        for mode in (STANDARD, SCIENTIFIC):
            self.engines[mode].on_history = lambda text, result, value, mode=mode: self._add_history(
                mode, text, result, value)

    # --- mode ---
    @property
    def engine(self) -> Engine:
        return self.engines[self.mode]

    def set_mode(self, mode: str) -> None:
        """Show another mode. Its calculation starts afresh; memory and history are kept."""
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if mode != self.mode:
            self.mode = mode
            self.engine.press("c")

    def press(self, command: str) -> bool:
        return self.engine.press(command)

    # --- history ---
    def _add_history(self, mode: str, text: str, result: str, value) -> None:
        items = self.histories[mode]
        items.insert(0, HistoryItem(text, result, Fraction(value)))
        del items[MAX_HISTORY:]

    @property
    def history(self) -> list[HistoryItem]:
        """The history of the current mode, newest first (empty in Programmer mode)."""
        return self.histories.get(self.mode, [])

    def has_history(self) -> bool:
        return self.mode in self.histories

    def recall_history(self, index: int) -> bool:
        items = self.history
        if not 0 <= index < len(items) or self.engine.error is not None:
            return False
        self.engine.recall(items[index].value)
        return True

    def clear_history(self) -> None:
        if self.mode in self.histories:
            self.histories[self.mode].clear()

    # --- memory ---
    @property
    def memory(self) -> list:
        """The memory of the current mode, newest first."""
        return self._integer_memory if self.mode == PROGRAMMER else self._memory

    def memory_text(self, index: int) -> str:
        value = self.memory[index]
        if self.mode == PROGRAMMER:
            return self.engine.format(value & self.engine.mask)
        return numeric.format_number(value, self.engine.precision, decimal_point=self.decimal_point,
                                     group_separator=self.group_separator)

    def _current(self):
        """The value on the display, ending the number that is being typed; None in an error state."""
        engine = self.engine
        if engine.error is not None:
            return None
        value = engine.commit()
        return to_signed(int(value), engine.bits) if self.mode == PROGRAMMER else value

    def memory_store(self) -> bool:
        value = self._current()
        if value is None:
            return False
        self.memory.insert(0, value)
        del self.memory[MAX_MEMORY:]
        return True

    def memory_recall(self, index: int = 0) -> bool:
        if not 0 <= index < len(self.memory) or self.engine.error is not None:
            return False
        self.engine.recall(self.memory[index])
        return True

    def _memory_change(self, index: int, sign: int) -> bool:
        value = self._current()
        if value is None:
            return False
        memory = self.memory
        if not memory:
            memory.insert(0, sign * value)
            return True
        if not 0 <= index < len(memory):
            return False
        if self.mode == PROGRAMMER:
            memory[index] = to_signed((memory[index] + sign * value) & self.engine.mask, self.engine.bits)
            return True
        try:
            memory[index] = numeric.add(memory[index], sign * value)
        except numeric.CalcError:
            return False
        return True

    def memory_add(self, index: int = 0) -> bool:
        return self._memory_change(index, 1)

    def memory_subtract(self, index: int = 0) -> bool:
        return self._memory_change(index, -1)

    def memory_clear(self, index: int | None = None) -> None:
        """Forget one entry, or with None the whole memory of the current mode."""
        if index is None:
            self.memory.clear()
        elif 0 <= index < len(self.memory):
            del self.memory[index]

    # --- clipboard ---
    def copy_text(self) -> str:
        """What Ctrl+C puts on the clipboard: the number on the display."""
        return self.engine.display

    def parse(self, text: str):
        """The value of pasted text for the current mode, or None when it is not a number that fits."""
        if not isinstance(text, str) or len(text) > MAX_PASTE:
            return None
        if self.mode != PROGRAMMER:
            return numeric.parse_number(text, decimal_point=self.decimal_point, group_separator=self.group_separator)
        engine = self.engine
        cleaned = text.strip().replace("−", "-").replace(" ", "").replace("\u00a0", "").replace("_", "")
        if engine.radix == 10 and self.group_separator:
            cleaned = cleaned.replace(self.group_separator, "")
        negative = cleaned.startswith("-")
        if negative:
            cleaned = cleaned[1:]
        prefix = {16: "0x", 8: "0o", 2: "0b"}.get(engine.radix)
        if prefix and cleaned.lower().startswith(prefix):
            cleaned = cleaned[2:]
        if not cleaned or not cleaned.isascii() or not cleaned.isalnum():
            return None
        try:
            magnitude = int(cleaned, engine.radix)
        except ValueError:
            return None
        if engine.radix == 10:
            limit = 1 << (engine.bits - 1)
            if magnitude > limit or (magnitude == limit and not negative):
                return None
            return (-magnitude if negative else magnitude) & engine.mask
        if negative or magnitude > engine.mask:
            return None
        return magnitude

    def paste(self, text: str) -> bool:
        """Take a number from the clipboard. Text that is not a number gives ``Invalid input``, as in Windows."""
        engine = self.engine
        if engine.error is not None:
            return False
        value = self.parse(text)
        if value is None:
            engine.commit()
            engine.error = numeric.MESSAGES[numeric.DOMAIN]
            return False
        engine.recall(value)
        return True
