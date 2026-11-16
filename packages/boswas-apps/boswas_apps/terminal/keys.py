"""Keyboard, mouse and pasted text as the bytes a program in the terminal expects (no Qt).

The encodings are xterm's, which is what ``TERM=xterm-256color`` promises:

* cursor keys send ``CSI A`` (``SS3 A`` in application cursor mode) and
  ``CSI 1 ; m A`` with modifiers, where m is 1 + Shift 1 + Alt 2 + Ctrl 4 +
  Meta 8; editing and function keys follow the same scheme;
* Alt sends ESC before the key; Ctrl with a letter or one of ``@[\\]^_?``
  sends the control character;
* the numeric keypad sends ``SS3`` codes in application keypad mode.
"""

from __future__ import annotations

import re

from .emulator import (ENCODING_SGR, ENCODING_URXVT, ENCODING_UTF8, MOUSE_ANY, MOUSE_BUTTON, MOUSE_NORMAL, MOUSE_OFF,
                       MOUSE_X10)

SHIFT, ALT, CTRL, META = 1, 2, 4, 8

_CURSOR = {"Up": "A", "Down": "B", "Right": "C", "Left": "D", "Home": "H", "End": "F"}
_FUNCTION_SS3 = {"F1": "P", "F2": "Q", "F3": "R", "F4": "S"}
_TILDE = {"Insert": 2, "Delete": 3, "PageUp": 5, "PageDown": 6, "F5": 15, "F6": 17, "F7": 18, "F8": 19, "F9": 20,
          "F10": 21, "F11": 23, "F12": 24, "F13": 25, "F14": 26, "F15": 28, "F16": 29, "F17": 31, "F18": 32,
          "F19": 33, "F20": 34}
_KEYPAD = {"KP_0": ("0", "p"), "KP_1": ("1", "q"), "KP_2": ("2", "r"), "KP_3": ("3", "s"), "KP_4": ("4", "t"),
           "KP_5": ("5", "u"), "KP_6": ("6", "v"), "KP_7": ("7", "w"), "KP_8": ("8", "x"), "KP_9": ("9", "y"),
           "KP_Decimal": (".", "n"), "KP_Separator": (",", "l"), "KP_Add": ("+", "k"), "KP_Subtract": ("-", "m"),
           "KP_Multiply": ("*", "j"), "KP_Divide": ("/", "o"), "KP_Equal": ("=", "X")}
# Ctrl with these keys gives a control character (the key as it is labelled, without Shift).
_CONTROL = {"@": 0, " ": 0, "2": 0, "[": 27, "3": 27, "\\": 28, "4": 28, "]": 29, "5": 29, "^": 30, "6": 30,
            "~": 30, "_": 31, "7": 31, "/": 31, "-": 31, "?": 127, "8": 127}

SPECIAL_KEYS = frozenset((*_CURSOR, *_FUNCTION_SS3, *_TILDE, *_KEYPAD, "KP_Enter", "Enter", "Tab", "Backtab",
                          "Backspace", "Escape"))


def modifier_parameter(modifiers: int) -> int:
    """The number xterm puts into a key sequence for a set of modifiers (1 for none)."""
    return 1 + (modifiers & (SHIFT | ALT | CTRL | META))


def encode_key(key: str, modifiers: int = 0, text: str = "", *, app_cursor: bool = False, app_keypad: bool = False,
               newline_mode: bool = False) -> bytes:
    """The bytes for one key press.

    ``key`` is the name of a special key (``Up``, ``F5``, ``KP_Enter``, ...:
    see ``SPECIAL_KEYS``) or the character on the key without modifiers
    (``a``, ``[``, ``2``); ``text`` is what the keyboard layout produced for
    it (``A`` with Shift, ``é`` on another layout). Returns ``b""`` for a key
    that sends nothing.
    """
    parameter = modifier_parameter(modifiers)
    alt = b"\x1b" if modifiers & ALT else b""
    if key in _CURSOR:
        final = _CURSOR[key]
        if parameter > 1:
            return f"\x1b[1;{parameter}{final}".encode()
        return f"\x1b{'O' if app_cursor else '['}{final}".encode()
    if key in _FUNCTION_SS3:
        final = _FUNCTION_SS3[key]
        return f"\x1b[1;{parameter}{final}".encode() if parameter > 1 else f"\x1bO{final}".encode()
    if key in _TILDE:
        number = _TILDE[key]
        return f"\x1b[{number};{parameter}~".encode() if parameter > 1 else f"\x1b[{number}~".encode()
    if key == "KP_Enter":
        if app_keypad and not modifiers:
            return b"\x1bOM"
        key = "Enter"
    if key in _KEYPAD:
        literal, code = _KEYPAD[key]
        if app_keypad and not modifiers:
            return f"\x1bO{code}".encode()
        return alt + literal.encode()
    if key == "Enter":
        return alt + (b"\r\n" if newline_mode else b"\r")
    if key == "Backtab" or (key == "Tab" and modifiers & SHIFT):
        return alt + b"\x1b[Z"
    if key == "Tab":
        return alt + b"\t"
    if key == "Backspace":
        return alt + (b"\x08" if modifiers & CTRL else b"\x7f")
    if key == "Escape":
        return alt + b"\x1b"
    if modifiers & CTRL:
        base = key if len(key) == 1 else ""
        if base.isascii() and "a" <= base.lower() <= "z":
            return alt + bytes((ord(base.lower()) - 96,))
        if base in _CONTROL:
            return alt + bytes((_CONTROL[base],))
        if len(text) == 1 and (text < " " or text == "\x7f"):
            return alt + text.encode()
        # Ctrl with a key that has no control character sends the key itself, as in xterm.
    if not text and len(key) == 1:
        text = key
    if not text:
        return b""
    return alt + text.encode("utf-8", "replace")


def encode_mouse(event: str, button: int, column: int, row: int, modifiers: int = 0, *, mode: int,
                 encoding: int) -> bytes:
    """A mouse event as the program asked for it, or ``b""`` when it did not ask for this kind.

    ``event`` is ``press``, ``release`` or ``move``. ``button``: 0 left,
    1 middle, 2 right, 3 none (a move without a button), 4 wheel up, 5 wheel
    down, 6 wheel left, 7 wheel right. ``column`` and ``row`` count cells
    from 0. ``mode`` is the tracking mode (9, 1000, 1002, 1003) and
    ``encoding`` 0, 1005, 1006 or 1015.
    """
    if mode == MOUSE_OFF or column < 0 or row < 0:
        return b""
    wheel = button >= 4
    if mode == MOUSE_X10:
        if event != "press" or wheel:
            return b""
        modifiers = 0
    elif event == "move":
        if mode == MOUSE_NORMAL or (mode == MOUSE_BUTTON and button == 3) or mode not in (MOUSE_BUTTON, MOUSE_ANY):
            return b""
    if wheel:
        if event != "press":
            return b""
        code = 64 + (button - 4)
    else:
        code = button
    if event == "move":
        code += 32
    if modifiers & SHIFT:
        code += 4
    if modifiers & ALT:
        code += 8
    if modifiers & CTRL:
        code += 16
    x, y = column + 1, row + 1
    if encoding == ENCODING_SGR:
        return f"\x1b[<{code};{x};{y}{'m' if event == 'release' else 'M'}".encode()
    if event == "release":
        code = (code & ~3) | 3                 # the old protocols do not say which button was released
    if encoding == ENCODING_URXVT:
        return f"\x1b[{code + 32};{x};{y}M".encode()
    if encoding == ENCODING_UTF8:
        if x > 2015 or y > 2015:
            return b""
        return b"\x1b[M" + chr(code + 32).encode("utf-8") + chr(x + 32).encode("utf-8") + chr(y + 32).encode("utf-8")
    if x > 223 or y > 223:
        return b""                             # one byte a coordinate: beyond this the protocol has no value
    return b"\x1b[M" + bytes((code + 32, x + 32, y + 32))


_PASTE_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
BRACKET_START, BRACKET_END = b"\x1b[200~", b"\x1b[201~"
MAX_PASTE = 16 << 20


def has_several_lines(text: str) -> bool:
    """Whether pasted text would press Enter for the user (a line break before its end)."""
    return "\n" in text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def encode_paste(text: str, bracketed: bool) -> bytes:
    """Pasted text for the program: line breaks become Enter, control characters are removed.

    Escape and the other control characters never reach the program from a
    paste (a pasted text must not be able to end the bracketed paste or send
    key sequences); tabs and line breaks stay. With ``bracketed`` the text
    is wrapped in the bracketed paste markers.
    """
    text = text[:MAX_PASTE].replace("\r\n", "\n").replace("\r", "\n")
    text = _PASTE_UNSAFE.sub("", text).replace("\n", "\r")
    data = text.encode("utf-8", "replace")
    if bracketed:
        return BRACKET_START + data + BRACKET_END
    return data
