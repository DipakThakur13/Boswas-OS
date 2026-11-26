"""The compositor's settings as Control Center reads and changes them (standard library only, no Qt).

boswas-compositor(1) merges four TOML files, later ones overriding earlier
ones key by key:

  /usr/share/boswas/compositor/compositor.toml   shipped defaults        ("defaults")
  /etc/boswas/compositor.toml                    the administrator       ("system")
  <config>/boswas/compositor.toml                the user                ("user")
  <config>/boswas/appearance.toml                the [appearance] table  ("appearance")

Settings changes the last two and then asks the running compositor to read
them again. This module knows every documented key, its type and its range
(SCHEMA, OUTPUT_KEYS), reads the layers the way the compositor does
(Snapshot), writes only values the compositor accepts, and never writes
[policy], [shell], [input_method] or [lock] command, which belong to the
administrator.

For other pages of Control Center (they need no file handling of their own):

    from boswas_control_center import compositorconfig

    compositorconfig.effective("lock", "idle_seconds")            -> 600, the value in use
    compositorconfig.effective("policy", "lock_idle_max_seconds") -> 600, the administrator's limit (0: none)
    compositorconfig.set_value("power", "screen_off_seconds", 900) -> Applied
    compositorconfig.set_value("outputs.eDP-1", "scale", 1.5)
    compositorconfig.set_value("appearance", "wallpaper", None)   # None removes the user's setting

Both read files, and set_value also talks to the compositor's socket: call
them from a worker thread (fluent.run_in_thread), not from the GUI thread.
set_value raises InvalidValue for a value the compositor or the policy
would not accept and SaveError when the file cannot be written; its result
says whether the running desktop took the change (Applied.live) or it
waits for the next sign-in (Applied.message has the words for the user).

Where things are written
  * Everything except the look goes to the user file. The file is rebuilt
    from what it held: every documented key and table the user had is kept.
    What cannot be kept (a key the compositor does not know, a value of the
    wrong kind, [policy] and the three ``command`` keys, comments) is never
    dropped silently: the previous file is copied to compositor.toml.bak
    first and Saved.notice tells the user. The same happens when the file
    was not TOML at all.
  * [appearance] scheme and wallpaper go to appearance.toml, the file
    ``boswas-preset`` writes as a whole (scheme and wallpaper) when a preset
    is applied: the other of the two keys is kept as the preset recorded it.
  * [appearance] reduced_motion goes to the user file. boswas-preset
    replaces appearance.toml with every preset and removes it on a reset;
    a setting kept there would be lost with the next preset.
  * Writing itself (atomic replacement, mode 0600, directories 0700, no
    symbolic links) is done by userfiles.write_config / remove_config, the
    only code of Control Center that changes files.

The compositor ignores a file with an unknown key or a value of the wrong
type as a whole (and reports it), and so it does a file that is not TOML;
what the other files say stays in use. Snapshot mirrors that, so the pages
show what is in use.

The control socket ($XDG_RUNTIME_DIR/boswas-$WAYLAND_DISPLAY.sock, one JSON
object per line) is spoken by Control: reload_config() and outputs().
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import stat
import threading
import tomllib
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from . import userfiles

DEFAULTS_FILE = "/usr/share/boswas/compositor/compositor.toml"
SYSTEM_FILE = "/etc/boswas/compositor.toml"
USER_NAME = "boswas/compositor.toml"            # names relative to the user's config directory (userfiles)
BACKUP_NAME = "boswas/compositor.toml.bak"
APPEARANCE_NAME = "boswas/appearance.toml"
MAX_BYTES = 256 * 1024                          # the compositor ignores a larger file

SCALES = (1.0, 1.25, 1.5, 1.75, 2.0)
ROTATIONS = ("normal", "90", "180", "270")
SCHEMES = ("dark", "light")
ACCEL_PROFILES = ("adaptive", "flat")
MAX_LAYOUTS = 4                                 # XKB keeps four layouts; the compositor's keymap drops a fifth
TEMPERATURE = (2500, 6500)
REPEAT_DELAY = (100, 2000)
REPEAT_RATE = (1, 100)
CURSOR_SIZE = (16, 96)
SCREEN_OFF = (30, 6 * 3600)                     # seconds; 0 means never
POSITION = (-32768, 32767)
# The settings that came with the desktop's Windows-like functions (boswas-compositor(1)).
ACCENTS = ("iris", "azure", "sky", "mint", "sun", "coral", "orchid", "slate")   # palette hues; iris is the default
TEXT_SCALE = (100, 225)                         # percent of the type ramp
COLOR_FILTERS = ("grayscale", "inverted", "grayscale_inverted", "deuteranopia", "protanopia", "tritanopia")
MAGNIFIER_ZOOM = (100, 1600)                    # percent
MAGNIFIER_STEPS = (25, 50, 100, 150, 200, 400)  # percent
KEY_DELAY = (100, 5000)                         # slow and bounce keys, milliseconds; 0 means off
TASKBAR_ALIGNMENTS = ("center", "left")
START_LAYOUTS = ("default", "more_pins", "more_recommendations")
START_FOLDERS = ("settings", "files", "documents", "downloads", "music", "pictures", "videos")
DESKTOP_SYSTEM_ICONS = ("home", "trash", "computer")    # the system icons the desktop can show, in this order
DESKTOP_ICON_SIZES = ("small", "medium", "large")       # 32, 48 and 96 pixels
WINDOW_SCOPES = ("desktop", "all")              # windows of the current desktop, or of every desktop
TOAST_SECONDS = (5, 7, 15, 30, 60, 300)
MAX_MUTED = 256                                 # applications whose banners can be off

HEADER = ("# Settings of this user for the Boswas desktop, written by Boswas Control Center.\n"
          "# Keys that boswas-compositor(1) does not document are not kept when a setting changes.\n")
APPEARANCE_HEADER = ("# Written by boswas-preset and by Boswas Control Center; the next preset replaces "
                     "scheme and wallpaper.\n")

_I32 = (-2 ** 31, 2 ** 31 - 1)
_U32 = (0, 2 ** 32 - 1)
_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_XKB_LIST = re.compile(r"^[A-Za-z0-9_./()+-]*(,[A-Za-z0-9_./()+-]*)*$")
_XKB_OPTIONS = re.compile(r"^[A-Za-z0-9_:+()-]*(,[A-Za-z0-9_:+()-]*)*$")
_XKB_NAME = re.compile(r"^[A-Za-z0-9_./()+-]*$")
_THEME_NAME = re.compile(r"^[A-Za-z0-9 ._+-]{0,128}$")
_WRITE_LOCK = threading.Lock()                  # one read-change-write of the user's files at a time


class ConfigError(Exception):
    """Something about the settings went wrong; the message is written for the user."""


class InvalidValue(ConfigError, ValueError):
    """A value the compositor (or the administrator's policy) does not accept."""


class SaveError(ConfigError):
    """The settings file could not be written; nothing was changed."""


class ControlError(ConfigError):
    """The compositor's control socket did not answer. ``reason``: not-running, timeout or failed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# --- the schema ---------------------------------------------------------------------------------------------

def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return _is_int(value) or isinstance(value, float)


def parse_mode(text: object) -> tuple[int, int, int | None] | None:
    """``WIDTHxHEIGHT`` or ``WIDTHxHEIGHT@HZ`` as (width, height, millihertz or None); None when it is neither."""
    if not isinstance(text, str) or len(text) > 40:
        return None
    size, at, rate = text.partition("@")
    refresh = None
    if at:
        if not re.fullmatch(r"[0-9]{1,4}(\.[0-9]{1,6})?", rate):
            return None
        hertz = float(rate)
        if not 1.0 <= hertz <= 1000.0:
            return None
        refresh = round(hertz * 1000)
    match = re.fullmatch(r"([0-9]{1,5})x([0-9]{1,5})", size)
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if not (1 <= width <= 16384 and 1 <= height <= 16384):
        return None
    return width, height, refresh


def parse_clock(text: object) -> int | None:
    """``HH:MM`` as minutes after midnight; None for anything else."""
    if not isinstance(text, str) or re.fullmatch(r"[0-9]{2}:[0-9]{2}", text) is None:
        return None
    hours, minutes = int(text[:2]), int(text[3:])
    return hours * 60 + minutes if hours < 24 and minutes < 60 else None


def clock_text(minutes: int) -> str:
    """Minutes after midnight as ``HH:MM``."""
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Key:
    """One documented key: what the compositor can read, what Settings may write, what it falls back to."""

    kind: str                                    # bool, int, float, str, strings or position (how it is written)
    default: object                              # the compositor's built-in value (None: not set)
    accepts: Callable[[object], bool]            # readable at all; anything else makes the compositor ignore the file
    check: Callable[[object], object]            # the value as written by Settings; raises InvalidValue
    tidy: Callable[[object], object]             # what the compositor makes of a readable value
    user: bool = True                            # False: only the administrator's files may set it


def _same(value: object) -> object:
    return value


def _boolean(default: bool) -> Key:
    def check(value: object) -> bool:
        if not isinstance(value, bool):
            raise InvalidValue("This setting is either on or off.")
        return value
    return Key("bool", default, lambda v: isinstance(v, bool), check, _same)


def _integer(default: int, low: int, high: int, storage: tuple[int, int] = _I32, what: str = "value",
             zero: bool = False, clamped: bool = True) -> Key:
    """An integer from low to high (with ``zero``: or 0). The compositor clamps a value outside, unless
    ``clamped`` is False (then another rule, the policy, deals with it)."""
    def accepts(value: object) -> bool:
        return _is_int(value) and storage[0] <= value <= storage[1]

    def check(value: object) -> int:
        if not _is_int(value) or not ((zero and value == 0) or low <= value <= high):
            raise InvalidValue(f"The {what} must be a whole number from {low} to {high}"
                               + (", or 0." if zero else "."))
        return value

    def tidy(value: int) -> int:
        return value if (zero and value == 0) or not clamped else max(low, min(high, value))
    return Key("int", default, accepts, check, tidy)


def _speed() -> Key:
    def check(value: object) -> float:
        if not _is_number(value) or not math.isfinite(value) or not -1.0 <= value <= 1.0:
            raise InvalidValue("The speed must be a number from -1 to 1.")
        return round(float(value), 3) + 0.0      # + 0.0: never "-0.0"

    def tidy(value: float) -> float:
        return max(-1.0, min(1.0, float(value))) if math.isfinite(value) else 0.0
    return Key("float", 0.0, _is_number, check, tidy)


def _scale(default: float | None) -> Key:
    def check(value: object) -> float:
        if not _is_number(value) or float(value) not in SCALES:
            raise InvalidValue("The scale must be 100, 125, 150, 175 or 200 percent.")
        return float(value)
    return Key("float", default, _is_number, check, lambda v: float(v) if float(v) in SCALES else default)


def _choice(default: str | None, values: tuple[str, ...], what: str, fallback: str | None) -> Key:
    def check(value: object) -> str:
        if not isinstance(value, str) or value not in values:
            raise InvalidValue(f"The {what} must be one of: {', '.join(values)}.")
        return value
    return Key("str", default, lambda v: isinstance(v, str), check, lambda v: v if v in values else fallback)


def _text(default: str, pattern: re.Pattern, what: str, limit: int = 256) -> Key:
    def check(value: object) -> str:
        if not isinstance(value, str) or len(value) > limit or pattern.fullmatch(value) is None:
            raise InvalidValue(f"That is not a valid {what}.")
        return value
    return Key("str", default, lambda v: isinstance(v, str), check, _same)


def _clock(default: str) -> Key:
    def check(value: object) -> str:
        if parse_clock(value) is None:
            raise InvalidValue("A time is written as HH:MM, for example 20:00.")
        return value
    return Key("str", default, lambda v: isinstance(v, str), check,
               lambda v: v if parse_clock(v) is not None else default)


def _plain_text(text: object, limit: int) -> bool:
    """A string without control characters that fits ``limit`` bytes of UTF-8."""
    if not isinstance(text, str):
        return False
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:               # a lone surrogate: not text
        return False
    return len(encoded) <= limit and not any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _path() -> Key:
    def check(value: object) -> str:
        if value == "":
            return ""
        if not isinstance(value, str) or not _plain_text(value, 4096) or not value.startswith("/"):
            raise InvalidValue("The picture must be named by its full path.")
        return os.path.normpath(value)
    return Key("str", "", lambda v: isinstance(v, str), check, _same)


def _program(default: tuple[str, ...], user: bool = True) -> Key:
    def accepts(value: object) -> bool:
        return isinstance(value, list) and all(isinstance(word, str) for word in value)

    def check(value: object) -> list[str]:
        if (not isinstance(value, (list, tuple)) or len(value) > 32
                or not all(isinstance(word, str) and _plain_text(word, 4096) for word in value)):
            raise InvalidValue("A program is a list of words: its name, then its arguments.")
        return list(value)
    return Key("strings", list(default), accepts, check, _same, user)


def _number_choice(default: int, values: tuple[int, ...], what: str, tidy: Callable[[int], int]) -> Key:
    """A whole number from a fixed list; the compositor makes ``tidy(value)`` of any other number."""
    def check(value: object) -> int:
        if not _is_int(value) or value not in values:
            raise InvalidValue(f"The {what} must be one of: {', '.join(str(v) for v in values)}.")
        return value
    return Key("int", default, lambda v: _is_int(v) and _U32[0] <= v <= _U32[1], check, tidy)


def _names(default: tuple[str, ...], allowed: tuple[str, ...] | None, what: str, limit: int = 64) -> Key:
    """A list of names, each once: from ``allowed`` (when given), else any plain text of up to 128 characters.
    The compositor drops what it cannot use and keeps the rest."""
    def usable(name: object) -> bool:
        if allowed is not None:
            return name in allowed
        return (isinstance(name, str) and bool(name.strip()) and len(name) <= 128
                and not any(ord(ch) < 32 or ord(ch) == 127 for ch in name))

    def check(value: object) -> list[str]:
        if not isinstance(value, (list, tuple)) or len(value) > limit or not all(usable(name) for name in value):
            raise InvalidValue(f"That is not a list of {what}.")
        return list(dict.fromkeys(value))

    def tidy(value: list) -> list[str]:
        return list(dict.fromkeys(name for name in value if usable(name)))[:limit]
    return Key("strings", list(default), lambda v: isinstance(v, list) and all(isinstance(n, str) for n in v),
               check, tidy)


def _nearest(values: tuple[int, ...]) -> Callable[[int], int]:
    return lambda value: value if value in values else min(values, key=lambda v: (abs(v - value), v))


def _mode() -> Key:
    def check(value: object) -> str:
        if parse_mode(value) is None:
            raise InvalidValue("A display mode is written as WIDTHxHEIGHT or WIDTHxHEIGHT@HZ.")
        return value
    return Key("str", None, lambda v: isinstance(v, str), check, lambda v: v if parse_mode(v) is not None else None)


def _position() -> Key:
    def accepts(value: object) -> bool:
        return (isinstance(value, list) and len(value) == 2
                and all(_is_int(part) and _I32[0] <= part <= _I32[1] for part in value))

    def in_range(value: Iterable[int]) -> bool:
        return all(POSITION[0] <= part <= POSITION[1] for part in value)

    def check(value: object) -> list[int]:
        if (not isinstance(value, (list, tuple)) or len(value) != 2 or not all(_is_int(part) for part in value)
                or not in_range(value)):
            raise InvalidValue("A monitor position is two whole numbers from -32768 to 32767.")
        return [int(value[0]), int(value[1])]
    return Key("position", None, accepts, check, lambda v: list(v) if in_range(v) else None)


# Every table and key of boswas-compositor(1), CONFIGURATION. [outputs.NAME] is OUTPUT_KEYS.
SCHEMA: dict[str, dict[str, Key]] = {
    "keyboard": {
        "layout": _text("us", _XKB_LIST, "keyboard layout"),
        "variant": _text("", _XKB_LIST, "keyboard layout variant"),
        "model": _text("", _XKB_NAME, "keyboard model"),
        "options": _text("", _XKB_OPTIONS, "list of keyboard options", 1024),
        "repeat_delay": _integer(400, *REPEAT_DELAY, what="repeat delay"),
        "repeat_rate": _integer(30, *REPEAT_RATE, what="repeat rate"),
        "numlock": _boolean(False),
        "sticky_keys": _boolean(False),
        "sticky_keys_lock": _boolean(True),
        "filter_keys": _boolean(False),
        "slow_keys_ms": _integer(0, *KEY_DELAY, storage=_U32, what="time in milliseconds", zero=True),
        "bounce_keys_ms": _integer(500, *KEY_DELAY, storage=_U32, what="time in milliseconds", zero=True),
    },
    "pointer": {
        "natural_scroll": _boolean(False),
        "accel_speed": _speed(),
        "accel_profile": _choice("adaptive", ACCEL_PROFILES, "acceleration profile", "adaptive"),
        "left_handed": _boolean(False),
        "cursor_theme": _text("", _THEME_NAME, "pointer theme name", 128),
        "cursor_size": _integer(24, *CURSOR_SIZE, storage=_U32, what="pointer size"),
    },
    "touchpad": {
        "tap_to_click": _boolean(True),
        "natural_scroll": _boolean(True),
        "accel_speed": _speed(),
        "disable_while_typing": _boolean(True),
        "gestures": _boolean(True),
    },
    # Not set: each monitor gets the scale the compositor recommends for it.
    "output": {"scale": _scale(None)},
    "night_light": {
        "enabled": _boolean(False),
        "temperature": _integer(4500, *TEMPERATURE, storage=_U32, what="colour temperature"),
        "scheduled": _boolean(False),
        "from": _clock("20:00"),
        "to": _clock("06:00"),
    },
    "power": {"screen_off_seconds": _integer(300, *SCREEN_OFF, storage=_U32, what="time in seconds", zero=True)},
    "lock": {
        "idle_seconds": _integer(600, 1, 7 * 24 * 3600, storage=_U32, what="time in seconds", zero=True,
                                 clamped=False),
        "command": _program(("/usr/libexec/boswas/boswas-shell", "--lock"), user=False),
        # The weather card of the lock screen: the user's preference, not lock policy.
        "show_weather": _boolean(True),
    },
    "shell": {"command": _program(("/usr/libexec/boswas/boswas-shell",), user=False)},
    "input_method": {"command": _program((), user=False)},
    "xwayland": {"enabled": _boolean(True)},
    "apps": {
        "terminal": _program(("boswas-terminal",)),
        "files": _program(("boswas-files",)),
        "settings": _program(("boswas-control-center",)),
        "task_manager": _program(("boswas-task-manager",)),
    },
    "appearance": {
        "scheme": _choice("dark", SCHEMES, "colour mode", "dark"),
        "reduced_motion": _boolean(False),
        "wallpaper": _path(),
        "accent": _choice("iris", ACCENTS, "accent colour", "iris"),
        "transparency": _boolean(True),
    },
    "accessibility": {
        "text_scale": _integer(100, *TEXT_SCALE, storage=_U32, what="text size"),
        "always_show_scrollbars": _boolean(False),
    },
    "color_filter": {
        "enabled": _boolean(False),
        "filter": _choice("grayscale", COLOR_FILTERS, "colour filter", "grayscale"),
        "shortcut": _boolean(False),
    },
    "magnifier": {
        "enabled": _boolean(False),
        "zoom": _integer(200, *MAGNIFIER_ZOOM, storage=_U32, what="zoom"),
        "step": _number_choice(100, MAGNIFIER_STEPS, "zoom increment", lambda v: v if v in MAGNIFIER_STEPS else 100),
    },
    "taskbar": {
        "alignment": _choice("center", TASKBAR_ALIGNMENTS, "taskbar alignment", "center"),
        "auto_hide": _boolean(False),
        "task_view": _boolean(True),
        "clock_seconds": _boolean(False),
        "badges": _boolean(True),
        "widgets": _boolean(True),
    },
    "start": {
        "layout": _choice("default", START_LAYOUTS, "layout of Start", "default"),
        "recently_added": _boolean(True),
        "most_used": _boolean(True),
        "folders": _names(("settings", "files"), START_FOLDERS, "folders Start can show"),
    },
    "multitasking": {
        "snap_windows": _boolean(True),
        "snap_layouts_on_maximize": _boolean(True),
        "snap_layouts_on_drag": _boolean(True),
        "taskbar_windows": _choice("desktop", WINDOW_SCOPES, "choice of windows", "desktop"),
        "alt_tab_windows": _choice("desktop", WINDOW_SCOPES, "choice of windows", "desktop"),
    },
    # The desktop's icons (the shell's desktop folder and system icons); the desktop's View menu changes
    # icons, icon_size, auto_arrange and align_to_grid in the same file.
    "desktop": {
        "icons": _boolean(True),
        "system_icons": _names(("trash",), DESKTOP_SYSTEM_ICONS, "system icons", 3),
        "icon_size": _choice("medium", DESKTOP_ICON_SIZES, "icon size", "medium"),
        "auto_arrange": _boolean(False),
        "align_to_grid": _boolean(True),
    },
    "notifications": {
        "do_not_disturb": _boolean(False),
        "do_not_disturb_scheduled": _boolean(False),
        "do_not_disturb_from": _clock("22:00"),
        "do_not_disturb_to": _clock("07:00"),
        "banners_off": _names((), None, "application names", MAX_MUTED),
        "toast_seconds": _number_choice(5, TOAST_SECONDS, "time", _nearest(TOAST_SECONDS)),
    },
    "policy": {
        "lock_idle_max_seconds": Key("int", 600, lambda v: _is_int(v) and _U32[0] <= v <= _U32[1], _same, _same,
                                     user=False),
        "privileged_protocols": Key("str", "shell", lambda v: isinstance(v, str), _same,
                                    lambda v: v if v in ("shell", "all") else "shell", user=False),
    },
}
OUTPUT_KEYS: dict[str, Key] = {
    "enabled": Key("bool", None, lambda v: isinstance(v, bool), _boolean(True).check, _same),
    "mode": _mode(),
    "scale": _scale(None),
    "position": _position(),
    "rotation": _choice(None, ROTATIONS, "rotation", None),
    "adaptive_sync": Key("bool", None, lambda v: isinstance(v, bool), _boolean(False).check, _same),
}

# The order tables and keys are written in.
_TABLE_ORDER = ("keyboard", "pointer", "touchpad", "output", "outputs", "night_light", "power", "lock", "xwayland",
                "apps", "appearance", "accessibility", "color_filter", "magnifier", "taskbar", "start", "multitasking",
                "desktop", "notifications")
# Which of the user's two files holds an [appearance] key (see the module text).
_APPEARANCE_FILE_KEYS = ("scheme", "wallpaper")


def valid_output_name(name: object) -> bool:
    """Whether ``name`` can be a connector name (``eDP-1``): what the compositor accepts in [outputs.NAME]."""
    return isinstance(name, str) and _OUTPUT_NAME.fullmatch(name) is not None


def _key(table: str, key: str) -> Key:
    """The Key of ``table``.``key``; ``table`` is a table name or ``outputs.NAME``."""
    if table.startswith("outputs."):
        if not valid_output_name(table[len("outputs."):]):
            raise InvalidValue(f"{table[len('outputs.'):]!r} is not the name of a monitor.")
        found = OUTPUT_KEYS.get(key)
    else:
        found = SCHEMA.get(table, {}).get(key)
    if found is None:
        raise InvalidValue(f"{table}.{key} is not a setting of the Boswas desktop.")
    return found


def check_value(table: str, key: str, value: object) -> object:
    """``value`` as it will be written for ``table``.``key``; InvalidValue when the compositor would reject it."""
    found = _key(table, key)
    if not found.user:
        raise InvalidValue(f"{table}.{key} can only be set by the administrator.")
    return found.check(value)


def defaults() -> dict:
    """The compositor's built-in configuration (no file read)."""
    config = {table: {name: (list(key.default) if isinstance(key.default, list) else key.default)
                      for name, key in keys.items()} for table, keys in SCHEMA.items()}
    config["outputs"] = {}
    return config


# --- reading the layers -----------------------------------------------------------------------------------

def config_home(environ: Mapping[str, str]) -> str | None:
    """The user's config directory as the compositor finds it: $XDG_CONFIG_HOME if absolute, else ~/.config."""
    base = environ.get("XDG_CONFIG_HOME") or ""
    if base.startswith("/") and "\0" not in base:
        return os.path.normpath(base)
    home = environ.get("HOME") or ""
    if home.startswith("/") and "\0" not in home:
        return os.path.join(os.path.normpath(home), ".config")
    return None


def _read_bytes(path: str) -> tuple[bytes | None, str]:
    """(content, problem): (None, "") for a missing file, (None, why) for one that cannot be used."""
    try:
        info = os.stat(path)
    except OSError:
        return None, ""
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        return None, "it is not a regular file of a sensible size"
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        return None, f"it cannot be read ({exc.strerror or exc})"
    if len(data) > MAX_BYTES:
        return None, "it is not a regular file of a sensible size"
    return data, ""


def _parse(data: bytes) -> tuple[dict | None, str]:
    try:
        return tomllib.loads(data.decode("utf-8")), ""
    except UnicodeDecodeError:
        return None, "it is not UTF-8 text"
    except tomllib.TOMLDecodeError as exc:
        return None, f"it is not valid TOML ({exc})"
    except RecursionError:
        return None, "it is nested too deeply"


def _schema_problem(document: dict) -> str:
    """Why the compositor cannot read ``document`` at all ("" when it can): an unknown key or a wrong type."""
    for table, entries in document.items():
        if table == "outputs":
            if not isinstance(entries, dict):
                return "outputs is not a table"
            for name, settings in entries.items():
                if not isinstance(settings, dict):
                    return f"outputs.{name} is not a table"
                for key, value in settings.items():
                    if key not in OUTPUT_KEYS:
                        return f"unknown key outputs.{name}.{key}"
                    if not OUTPUT_KEYS[key].accepts(value):
                        return f"outputs.{name}.{key} has a value of the wrong kind"
            continue
        if table not in SCHEMA:
            return f"unknown table [{table}]"
        if not isinstance(entries, dict):
            return f"{table} is not a table"
        for key, value in entries.items():
            if key not in SCHEMA[table]:
                return f"unknown key {table}.{key}"
            if not SCHEMA[table][key].accepts(value):
                return f"{table}.{key} has a value of the wrong kind"
    return ""


@dataclass(frozen=True)
class Layer:
    """One of the four files as the compositor sees it."""

    name: str                 # defaults, system, user or appearance
    path: str
    state: str                # missing, used, skipped (not usable: only this file is left out) or
    #                           rejected (an unknown key or a wrong type: the compositor ignores the whole file)
    problem: str = ""         # why it is skipped or rejected
    values: Mapping[str, dict] = field(default_factory=dict)      # what it contributes
    data: bytes | None = None                                     # its content (None when missing or unreadable)


def _layer(name: str, path: str | None) -> Layer:
    if path is None:
        return Layer(name, "", "missing")
    data, problem = _read_bytes(path)
    if data is None:
        return Layer(name, path, "skipped" if problem else "missing", problem)
    document, problem = _parse(data)
    if document is None:
        return Layer(name, path, "skipped", problem, data=data)
    if name == "user":
        # The compositor takes these out of a user's file before it looks at the rest.
        document = {table: (dict(entries) if isinstance(entries, dict) else entries)
                    for table, entries in document.items() if table != "policy"}
        for table in ("shell", "lock", "input_method"):
            if isinstance(document.get(table), dict):
                document[table].pop("command", None)
    elif name == "appearance":
        if len(document) != 1 or not isinstance(document.get("appearance"), dict):
            return Layer(name, path, "skipped", "it may only contain the [appearance] table", data=data)
    problem = _schema_problem(document)
    if problem:
        return Layer(name, path, "rejected", problem, data=data)
    return Layer(name, path, "used", "", document, data)


def _merge(base: dict, over: Mapping) -> None:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = dict(value) if isinstance(value, dict) else value


def _tidy(merged: Mapping) -> dict:
    """The built-in defaults overlaid with ``merged`` and brought into range, as Config::sanitize does."""
    config = defaults()
    for table, keys in SCHEMA.items():
        for name, key in keys.items():
            if name in merged.get(table, {}):
                value = merged[table][name]
                config[table][name] = key.tidy(list(value) if isinstance(value, list) else value)
    for output, settings in merged.get("outputs", {}).items():
        if not valid_output_name(output):
            continue
        kept = {}
        for name, value in settings.items():
            value = OUTPUT_KEYS[name].tidy(value)
            if value is not None:
                kept[name] = value
        config["outputs"][output] = kept
    limit = config["policy"]["lock_idle_max_seconds"]
    idle = config["lock"]["idle_seconds"]
    if limit > 0 and (idle == 0 or idle > limit):
        config["lock"]["idle_seconds"] = limit
    return config


class Snapshot:
    """The four layers read once, and the configuration the compositor makes of them."""

    def __init__(self, layers: Iterable[Layer]) -> None:
        self.layers: tuple[Layer, ...] = tuple(layers)
        self.rejected = next((layer for layer in self.layers if layer.state == "rejected"), None)
        merged: dict = {}
        for layer in self.layers:
            if layer.state != "rejected":
                _merge(merged, layer.values)
        self.values = _tidy(merged)

    def layer(self, name: str) -> Layer:
        return next(layer for layer in self.layers if layer.name == name)

    def get(self, table: str, key: str) -> object:
        """The value in use. ``table`` may be ``outputs.NAME`` (None for what is not set there)."""
        _key(table, key)
        if table.startswith("outputs."):
            return self.values["outputs"].get(table[len("outputs."):], {}).get(key)
        return self.values[table][key]

    def source(self, table: str, key: str) -> str:
        """Where the value in use comes from: built-in, or the layer defaults, system, user or appearance."""
        _key(table, key)
        path = ("outputs", table[len("outputs."):]) if table.startswith("outputs.") else (table,)
        found = "built-in"
        for layer in self.layers:
            if layer.state == "rejected":
                continue
            entries: object = layer.values
            for part in path:
                entries = entries.get(part, {}) if isinstance(entries, dict) else {}
            if isinstance(entries, dict) and key in entries:
                found = layer.name
        return found

    def output(self, name: str) -> dict:
        """What is set for one monitor (``[outputs.NAME]``); empty when nothing is."""
        return dict(self.values["outputs"].get(name, {}))

    @property
    def lock_limit(self) -> int:
        """The longest automatic lock time the administrator allows, in seconds; 0 for no limit."""
        return self.values["policy"]["lock_idle_max_seconds"]

    def lock_allowed(self, seconds: int) -> bool:
        """Whether the policy lets the user choose this lock time (0: never lock)."""
        limit = self.lock_limit
        return seconds >= 0 and (limit == 0 or 0 < seconds <= limit)

    def problems(self) -> list[str]:
        """What is wrong with the files, in words for the user (empty when all is well)."""
        found = []
        for layer in self.layers:
            if layer.state == "rejected":
                found.append(f"{layer.path} has an entry the desktop does not understand ({layer.problem}). "
                             "The desktop ignores this file until that is repaired.")
            elif layer.state == "skipped":
                found.append(f"{layer.path} is not used by the desktop: {layer.problem}.")
        return found


# --- writing TOML -------------------------------------------------------------------------------------------

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def toml_string(text: str) -> str:
    """A TOML basic string."""
    out = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_value(kind: str, value: object) -> str:
    if kind == "bool":
        return "true" if value else "false"
    if kind == "int":
        return str(int(value))
    if kind == "float":
        text = f"{float(value):.4f}".rstrip("0")
        return text + "0" if text.endswith(".") else text
    if kind == "str":
        return toml_string(value)
    if kind == "strings":
        return "[" + ", ".join(toml_string(word) for word in value) + "]"
    if kind == "position":
        return f"[{int(value[0])}, {int(value[1])}]"
    raise ValueError(kind)


def serialize(config: Mapping[str, dict], header: str = HEADER) -> str:
    """The text of a settings file holding ``config`` ({table: {key: value}}, with ``outputs`` by name).

    Only documented tables and keys are written, in a fixed order; an empty
    table is left out.
    """
    blocks = []
    for table in _TABLE_ORDER:
        entries = config.get(table) or {}
        if table == "outputs":
            for name in sorted(entries):
                lines = [f"{key} = {toml_value(OUTPUT_KEYS[key].kind, entries[name][key])}"
                         for key in OUTPUT_KEYS if key in entries[name]]
                if lines and valid_output_name(name):
                    blocks.append(f"[outputs.{name}]\n" + "\n".join(lines) + "\n")
            continue
        lines = [f"{key} = {toml_value(SCHEMA[table][key].kind, entries[key])}"
                 for key in SCHEMA[table] if key in entries and SCHEMA[table][key].user]
        if lines:
            blocks.append(f"[{table}]\n" + "\n".join(lines) + "\n")
    return header + ("\n" if header and blocks else "") + "\n".join(blocks)


def has_comments(text: str) -> bool:
    """Whether a TOML document (one that parses) contains a comment."""
    index, length = 0, len(text)
    while index < length:
        ch = text[index]
        if ch == "#":
            return True
        if ch in "\"'":
            triple = text.startswith(ch * 3, index)
            end = ch * 3 if triple else ch
            index += len(end)
            while index < length and not text.startswith(end, index):
                index += 2 if ch == '"' and text[index] == "\\" else 1
            if triple:                         # up to two more quotes may belong to the string
                while text.startswith(ch, index + 3):
                    index += 1
            index += len(end)
            continue
        index += 1
    return False


def carry_over(document: Mapping) -> tuple[dict, list[str]]:
    """What a rewritten user file keeps of ``document``, and the names of what it cannot keep."""
    kept: dict = {}
    dropped: list[str] = []
    for table, entries in document.items():
        if table == "outputs" and isinstance(entries, dict):
            for name, settings in entries.items():
                if not valid_output_name(name) or not isinstance(settings, dict):
                    dropped.append(f"outputs.{name}")
                    continue
                for key, value in settings.items():
                    try:
                        kept.setdefault("outputs", {}).setdefault(name, {})[key] = check_value(f"outputs.{name}",
                                                                                               key, value)
                    except InvalidValue:
                        dropped.append(f"outputs.{name}.{key}")
            continue
        if table not in SCHEMA or table == "outputs" or not isinstance(entries, dict):
            dropped.append(f"[{table}]")
            continue
        if not any(key.user for key in SCHEMA[table].values()):
            dropped.append(f"[{table}] (only the administrator's files may set it)")
            continue
        for key, value in entries.items():
            if key in SCHEMA[table] and not SCHEMA[table][key].user:
                dropped.append(f"{table}.{key} (only the administrator's files may set it)")
                continue
            try:
                kept.setdefault(table, {})[key] = check_value(table, key, value)
            except InvalidValue:
                dropped.append(f"{table}.{key}")
    return kept, dropped


# --- results ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Saved:
    """What update() did to the user's files."""

    written: tuple[str, ...] = ()        # the files replaced (absolute paths); empty when nothing had to change
    backup: str = ""                     # compositor.toml.bak, when a copy of the previous file was kept
    dropped: tuple[str, ...] = ()        # what the previous files held and the new ones do not
    notice: str = ""                     # the same in words for the user ("" when there is nothing to tell)


@dataclass(frozen=True)
class Applied:
    """What apply() did: the files, and whether the running desktop took the change."""

    saved: Saved
    live: bool = False                   # the compositor read its files again
    warnings: tuple[str, ...] = ()       # what it said about them
    problem: str = ""                    # why it is not live, in words for the user

    @property
    def message(self) -> str:
        """Everything the user should be told ("" when the change simply worked)."""
        parts = [self.saved.notice]
        if not self.live:
            parts.append(self.problem)
        if self.warnings:
            parts.append("The desktop reported: " + "; ".join(self.warnings))
        return " ".join(part for part in parts if part)


NOT_RUNNING = "Saved. No Boswas desktop is running here, so the change applies the next time you sign in."


# --- the control socket -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Mode:
    width: int
    height: int
    refresh: int                         # millihertz
    preferred: bool = False

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


@dataclass(frozen=True)
class AdaptiveSync:
    """Variable refresh rate of a monitor as ``outputs`` reports it: offered by monitor and driver, and on."""

    supported: bool = False
    enabled: bool = False


@dataclass(frozen=True)
class Output:
    """One monitor as ``outputs`` reports it. A monitor that is turned off has a name and nothing else."""

    name: str
    enabled: bool = True
    make: str = ""
    model: str = ""
    x: int = 0                           # logical pixels: position and size on the desktop
    y: int = 0
    width: int = 0
    height: int = 0
    scale: float = 1.0
    rotation: str = "normal"
    mode: Mode | None = None
    modes: tuple[Mode, ...] = ()              # "preferred": what the compositor uses without a mode setting
    physical_mm: tuple[int, int] = (0, 0)
    adaptive_sync: AdaptiveSync = AdaptiveSync()
    recommended_scale: float = 1.0       # what the monitor gets without a scale setting

    @property
    def label(self) -> str:
        """A name for people: "Dell U2720Q", "Built-in display", else the connector name."""
        if self.name.startswith(("eDP", "LVDS", "DSI")):
            return "Built-in display"
        words = [part for part in (self.make, self.model) if part and part.casefold() != "unknown"]
        if len(words) == 2 and words[1].casefold().startswith(words[0].casefold()):
            words = words[1:]
        return " ".join(words) or self.name


def _clean(text: object, limit: int = 64) -> str:
    if not isinstance(text, str):
        return ""
    return "".join(ch for ch in text if ch.isprintable())[:limit].strip()


def _whole(value: object, low: int, high: int, default: int = 0) -> int:
    return value if _is_int(value) and low <= value <= high else default


def _parse_mode_entry(entry: object) -> Mode | None:
    if not isinstance(entry, dict):
        return None
    width, height = _whole(entry.get("width"), 1, 16384), _whole(entry.get("height"), 1, 16384)
    refresh = _whole(entry.get("refresh"), 1, 1_000_000)
    if not (width and height and refresh):
        return None
    return Mode(width, height, refresh, entry.get("preferred") is True)


def parse_outputs(data: object) -> list[Output]:
    """The reply of ``outputs`` as a list of Output, sorted by name; entries that make no sense are left out."""
    found: dict[str, Output] = {}
    for entry in data if isinstance(data, list) else []:
        if len(found) >= 16 or not isinstance(entry, dict) or not valid_output_name(entry.get("name")):
            continue
        name = entry["name"]
        if entry.get("enabled") is False:
            found.setdefault(name, Output(name, enabled=False))
            continue
        modes = []
        for mode in (entry.get("modes") if isinstance(entry.get("modes"), list) else [])[:512]:
            parsed = _parse_mode_entry(mode)
            if parsed is not None and parsed not in modes:
                modes.append(parsed)
        current = _parse_mode_entry(entry.get("mode"))
        if current is not None:
            current = next((mode for mode in modes if mode.size == current.size and mode.refresh == current.refresh),
                           current)
            if current not in modes:
                modes.append(current)
        width, height = _whole(entry.get("width"), 1, 65536), _whole(entry.get("height"), 1, 65536)
        if not (width and height):
            continue
        scale = entry.get("scale")
        scale = float(scale) if _is_number(scale) and math.isfinite(scale) and 0.25 <= scale <= 8 else 1.0
        recommended = entry.get("recommended_scale")
        recommended = float(recommended) if _is_number(recommended) and float(recommended) in SCALES else 1.0
        rotation = entry.get("rotation") if entry.get("rotation") in ROTATIONS else "normal"
        sync = entry.get("adaptive_sync") if isinstance(entry.get("adaptive_sync"), dict) else {}
        supported = sync.get("supported") is True
        found.setdefault(name, Output(
            name, True, _clean(entry.get("make")), _clean(entry.get("model")),
            _whole(entry.get("x"), -65536, 65536), _whole(entry.get("y"), -65536, 65536), width, height, scale,
            rotation, current, tuple(modes),
            (_whole(entry.get("physical_width_mm"), 0, 100000), _whole(entry.get("physical_height_mm"), 0, 100000)),
            AdaptiveSync(supported, supported and sync.get("enabled") is True), recommended))
    return sorted(found.values(), key=lambda output: _natural(output.name))


def _natural(name: str) -> list:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"([0-9]+)", name)]


def control_socket(environ: Mapping[str, str]) -> str | None:
    """The public control socket of the compositor of this session, or None when the session has none."""
    explicit = environ.get("BOSWAS_COMPOSITOR_SOCKET") or ""
    if explicit.startswith("/") and "\0" not in explicit:
        return explicit
    runtime, display = environ.get("XDG_RUNTIME_DIR") or "", environ.get("WAYLAND_DISPLAY") or ""
    if not runtime.startswith("/") or not display or "/" in display or "\0" in runtime + display:
        return None
    return os.path.join(runtime, f"boswas-{display}.sock")


class Control:
    """A client of the compositor's public control socket: one request per connection, with a timeout."""

    MAX_REPLY = 1024 * 1024

    def __init__(self, environ: Mapping[str, str] | None = None, *, path: str | None = None,
                 timeout: float = 3.0) -> None:
        self.environ = os.environ if environ is None else environ
        self.path = path
        self.timeout = timeout

    def socket_path(self) -> str | None:
        return self.path or control_socket(self.environ)

    def request(self, command: str, **arguments: object) -> object:
        """Send ``{"cmd": command, ...}`` and return the reply's ``data``; ControlError otherwise."""
        path = self.socket_path()
        if path is None:
            raise ControlError("not-running", "No Boswas desktop is running in this session.")
        try:
            info = os.stat(path)
        except OSError:
            raise ControlError("not-running", "No Boswas desktop is running in this session.") from None
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ControlError("not-running", "The desktop's control socket is not yours to use.")
        line = json.dumps({"cmd": command, **arguments}, separators=(",", ":")).encode("utf-8") + b"\n"
        reply = b""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(path)
                connection.sendall(line)
                while b"\n" not in reply:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    reply += chunk
                    if len(reply) > self.MAX_REPLY:
                        raise ControlError("failed", "The desktop sent an answer that is too long.")
        except (ConnectionRefusedError, FileNotFoundError):
            raise ControlError("not-running", "No Boswas desktop is running in this session.") from None
        except TimeoutError:
            raise ControlError("timeout", "The desktop did not answer in time.") from None
        except OSError as exc:
            raise ControlError("failed", f"The desktop could not be reached ({exc.strerror or exc}).") from None
        try:
            answer = json.loads(reply.split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ControlError("failed", "The desktop sent an answer that could not be read.") from None
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            reason = _clean(answer.get("error"), 200) if isinstance(answer, dict) else ""
            raise ControlError("failed", "The desktop refused the request" + (f" ({reason})." if reason else "."))
        return answer.get("data")

    def reload_config(self) -> list[str]:
        """Have the compositor read its files again; returns its warnings about them."""
        data = self.request("reload-config")
        warnings = data.get("warnings") if isinstance(data, dict) else None
        if not isinstance(warnings, list):
            return []
        return [" ".join(_clean(line, 300) for line in text.splitlines() if line.strip())
                for text in warnings[:32] if isinstance(text, str)]

    def outputs(self) -> list[Output]:
        """The monitors of the desktop, those that are turned off included."""
        return parse_outputs(self.request("outputs"))


# --- reading and changing the user's files ------------------------------------------------------------------

class Settings:
    """The compositor's settings of one user: read the layers, change the user's files, tell the compositor.

    Everything here blocks (files, the control socket): call it from a
    worker thread. ``write`` and ``remove`` default to
    userfiles.write_config and userfiles.remove_config; tests pass their own.
    """

    def __init__(self, environ: Mapping[str, str] | None = None, *,
                 system_files: tuple[str, str] = (DEFAULTS_FILE, SYSTEM_FILE),
                 write: Callable[[Mapping[str, str], str, bytes], str] | None = None,
                 remove: Callable[[Mapping[str, str], str], None] | None = None,
                 control: Control | None = None) -> None:
        self.environ = os.environ if environ is None else environ
        self.system_files = tuple(system_files)
        self._write, self._remove = write, remove
        self.control = control if control is not None else Control(self.environ)

    # --- paths and reading ---
    def user_path(self, name: str = USER_NAME) -> str | None:
        home = config_home(self.environ)
        return os.path.join(home, name) if home is not None else None

    def directory(self) -> str | None:
        """The directory of the user's two files (to watch for changes); it may not exist yet."""
        path = self.user_path()
        return os.path.dirname(path) if path else None

    def snapshot(self) -> Snapshot:
        return Snapshot((_layer("defaults", self.system_files[0]), _layer("system", self.system_files[1]),
                         _layer("user", self.user_path(USER_NAME)),
                         _layer("appearance", self.user_path(APPEARANCE_NAME))))

    def effective(self, table: str, key: str) -> object:
        return self.snapshot().get(table, key)

    def user_file(self) -> bytes | None:
        """The exact content of the user file (None when there is none), to restore() later."""
        path = self.user_path()
        if path is None:
            return None
        try:
            with open(path, "rb") as handle:
                return handle.read(MAX_BYTES + 1)
        except OSError:
            return None

    # --- writing ---
    def _store(self, name: str, data: bytes) -> str:
        write = self._write or getattr(userfiles, "write_config", None)
        if write is None:
            raise SaveError("This version of Control Center cannot save settings.")
        try:
            return write(self.environ, name, data)
        except userfiles.UserFileError as exc:
            raise SaveError(str(exc)) from None
        except OSError as exc:
            raise SaveError(f"The settings could not be saved ({exc.strerror or exc}).") from None

    def _delete(self, name: str) -> None:
        remove = self._remove or getattr(userfiles, "remove_config", None)
        if remove is None:
            raise SaveError("This version of Control Center cannot save settings.")
        try:
            remove(self.environ, name)
        except userfiles.UserFileError as exc:
            raise SaveError(str(exc)) from None
        except OSError as exc:
            raise SaveError(f"The settings could not be saved ({exc.strerror or exc}).") from None

    def restore(self, data: bytes | None) -> None:
        """Put the user file back exactly as user_file() returned it (None: there was no file)."""
        with _WRITE_LOCK:
            if data is None:
                self._delete(USER_NAME)
            else:
                self._store(USER_NAME, data)

    def update(self, changes: Mapping[tuple[str, str], object]) -> Saved:
        """Change settings in the user's files. ``changes`` maps (table, key) to a value, or to None to
        remove the user's setting. ``table`` is a table name or ``outputs.NAME``.

        Every value is checked first (InvalidValue, nothing written). The
        files are then read, changed and replaced one after the other; a
        backup of the user file is written before it when something of it
        cannot be kept (see the module text).
        """
        checked: dict[tuple[str, str], object] = {}
        for (table, key), value in changes.items():
            if value is None:
                if not _key(table, key).user:
                    raise InvalidValue(f"{table}.{key} can only be set by the administrator.")
                checked[(table, key)] = None
            else:
                checked[(table, key)] = check_value(table, key, value)
        if self.user_path() is None:
            raise SaveError("Your home folder could not be found, so the settings cannot be saved.")
        with _WRITE_LOCK:
            snapshot = self.snapshot()
            idle = checked.get(("lock", "idle_seconds"))
            if idle is not None and not snapshot.lock_allowed(idle):
                minutes = max(1, snapshot.lock_limit // 60)
                raise InvalidValue("Your organisation requires the screen to lock after at most "
                                   f"{minutes} minute{'s' if minutes != 1 else ''} without input.")
            user_changes, look_changes = {}, {}
            for (table, key), value in checked.items():
                if table == "appearance" and key in _APPEARANCE_FILE_KEYS:
                    look_changes[key] = value
                    if value is None:               # "back to the default": no copy in the other file either
                        user_changes[(table, key)] = None
                else:
                    user_changes[(table, key)] = value
                    if table == "appearance":       # a copy in appearance.toml would hide the new value
                        look_changes[key] = None
            written: list[str] = []
            dropped: list[str] = []
            notices: list[str] = []
            backup = ""
            if user_changes:
                path, backup, lost, notice = self._update_user(snapshot.layer("user"), user_changes)
                written += [path] if path else []
                dropped += lost
                notices += [notice] if notice else []
            if look_changes:
                path, lost, notice = self._update_appearance(snapshot.layer("appearance"), look_changes)
                written += [path] if path else []
                dropped += lost
                notices += [notice] if notice else []
        return Saved(tuple(written), backup, tuple(dropped), " ".join(notices))

    def _update_user(self, layer: Layer, changes: Mapping[tuple[str, str], object]) -> tuple[str, str, list[str], str]:
        previous = layer.data
        path = self.user_path(USER_NAME)
        if previous is None and layer.state == "skipped":
            # Not a readable regular file of a sensible size: there is no way to keep a copy of it.
            raise SaveError(f"{path} is in the way ({layer.problem}). Settings did not replace it; move it away "
                            "and change the setting again.")
        document, unreadable = _parse(previous) if previous is not None else ({}, "")
        config, dropped = carry_over(document or {})
        before = json.dumps(config, sort_keys=True)
        for (table, key), value in changes.items():
            if table.startswith("outputs."):
                entries = config.setdefault("outputs", {}).setdefault(table[len("outputs."):], {})
            else:
                entries = config.setdefault(table, {})
            if value is None:
                entries.pop(key, None)
            else:
                entries[key] = value
        config["outputs"] = {name: entries for name, entries in config.get("outputs", {}).items() if entries}
        config = {table: entries for table, entries in config.items() if entries}
        if document is not None and not dropped and json.dumps(config, sort_keys=True) == before:
            return "", "", [], ""                  # the file already says so: leave it exactly as it is
        if document is not None and not dropped:
            text = previous.decode("utf-8") if previous is not None else ""
            if has_comments(text[len(HEADER):] if text.startswith(HEADER) else text):
                dropped.append("comments")
        data = serialize(config).encode("utf-8")
        if data == previous:
            return "", "", [], ""
        backup, notice = "", ""
        if previous is not None and (document is None or dropped):
            backup = self._store(BACKUP_NAME, previous)
            if document is None:
                dropped = ["everything"]
                notice = (f"{path} could not be read as a settings file ({unreadable}), so it was replaced. "
                          f"The previous file is kept as {backup}.")
            else:
                notice = (f"{path} was rewritten without {_listing(dropped)}, which Settings cannot keep. "
                          f"The previous file is kept as {backup}.")
        return self._store(USER_NAME, data), backup, dropped, notice

    def _update_appearance(self, layer: Layer, changes: Mapping[str, object]) -> tuple[str, list[str], str]:
        previous = layer.data
        path = self.user_path(APPEARANCE_NAME)
        document = _parse(previous)[0] if previous is not None else {}
        look = document.get("appearance") if isinstance(document, dict) else None
        kept, dropped = carry_over({"appearance": look} if isinstance(look, dict) else {})
        dropped += [f"[{table}]" for table in (document or {}) if table != "appearance"]
        if previous is not None and document is None:
            dropped.append("everything")
        entries = kept.setdefault("appearance", {})
        before = dict(entries)
        for key, value in changes.items():
            if value is None:
                entries.pop(key, None)
            else:
                entries[key] = value
        if entries == before and not dropped:
            return "", [], ""                      # nothing of this file changes: leave it as the preset wrote it
        notice = ""
        if dropped:
            notice = f"{path} held {_listing(dropped)}, which does not belong there and was removed."
        if not entries:
            if previous is None:
                return "", [], ""
            self._delete(APPEARANCE_NAME)          # no table left: the compositor would report an empty file
            return path or "", dropped, notice
        data = serialize({"appearance": entries}, APPEARANCE_HEADER).encode("utf-8")
        if data == previous:
            return "", [], ""
        return self._store(APPEARANCE_NAME, data), dropped, notice

    # --- the running compositor ---
    def reload(self) -> list[str]:
        return self.control.reload_config()

    def outputs(self) -> list[Output]:
        return self.control.outputs()

    def apply(self, changes: Mapping[tuple[str, str], object]) -> Applied:
        """update(), then have the running compositor read the files again."""
        return self.reloaded(self.update(changes))

    def reloaded(self, saved: Saved = Saved()) -> Applied:
        """Ask the compositor to read its files; never raises for a compositor that is not there."""
        try:
            return Applied(saved, True, tuple(self.reload()))
        except ControlError as exc:
            if exc.reason == "not-running":
                return Applied(saved, False, (), NOT_RUNNING)
            return Applied(saved, False, (), f"Saved, but the desktop has not taken the change yet: {exc}")


def _listing(names: list[str]) -> str:
    shown = names[:6]
    return ", ".join(shown) + (f" and {len(names) - 6} more" if len(names) > 6 else "")


# The factory the settings sections use; tests replace it.
def default() -> Settings:
    return Settings()


def effective(table: str, key: str, *, environ: Mapping[str, str] | None = None) -> object:
    """The value of ``table``.``key`` the compositor uses (see the module text). Reads files: not for the GUI thread."""
    return Settings(environ).effective(table, key)


def set_value(table: str, key: str, value: object, *, environ: Mapping[str, str] | None = None) -> Applied:
    """Save one setting of the user and have the running compositor take it (see the module text)."""
    return Settings(environ).apply({(table, key): value})


# --- monitors: modes, sizes and arrangement ---------------------------------------------------------------------

def mode_text(width: int, height: int, refresh: int | None = None) -> str:
    """The ``mode`` value of a display mode: "2560x1440@59.951" (refresh in millihertz)."""
    if refresh is None:
        return f"{width}x{height}"
    rate = f"{refresh / 1000:.3f}".rstrip("0").rstrip(".")
    return f"{width}x{height}@{rate}"


def rate_label(refresh: int) -> str:
    """A refresh rate for people: 60 Hz, 59.94 Hz."""
    return f"{refresh / 1000:.2f}".rstrip("0").rstrip(".") + " Hz"


def sizes(modes: Iterable[Mode]) -> list[tuple[int, int]]:
    """The different sizes of a monitor's modes, largest first."""
    return sorted({mode.size for mode in modes}, key=lambda size: (-size[0] * size[1], -size[0]))


def rates(modes: Iterable[Mode], size: tuple[int, int]) -> list[int]:
    """Every refresh rate (millihertz) offered at ``size``, highest first: the first is the compositor's default.

    The compositor runs a monitor at the highest rate it offers at a size unless the mode names another
    (boswas-compositor(1), [outputs.NAME] mode).
    """
    return sorted({mode.refresh for mode in modes if mode.size == size}, reverse=True)


def preferred_size(modes: Iterable[Mode]) -> tuple[int, int] | None:
    return next((mode.size for mode in modes if mode.preferred), None)


def logical_size(width: int, height: int, scale: float, rotation: str = "normal") -> tuple[int, int]:
    """The size a mode takes on the desktop: turned for 90/270, divided by the scale, rounded up."""
    if rotation in ("90", "270"):
        width, height = height, width
    return math.ceil(width / scale - 1e-9), math.ceil(height / scale - 1e-9)


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def at(self, x: int, y: int) -> "Rect":
        return Rect(x, y, self.w, self.h)

    def overlaps(self, other: "Rect") -> bool:
        return self.x < other.right and other.x < self.right and self.y < other.bottom and other.y < self.bottom

    def touches(self, other: "Rect") -> bool:
        """Whether the two share a piece of an edge (a corner alone does not count)."""
        if self.right == other.x or other.right == self.x:
            return min(self.bottom, other.bottom) > max(self.y, other.y)
        if self.bottom == other.y or other.bottom == self.y:
            return min(self.right, other.right) > max(self.x, other.x)
        return False


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def snap(moving: Rect, others: Iterable[Rect], x: int, y: int, magnet: int = 0) -> tuple[int, int]:
    """The legal position of ``moving`` nearest to (x, y): edge to edge with one of ``others``, on none of them.

    Within ``magnet`` pixels of having its top, bottom, left, right or
    middle in line with the neighbour's, it takes that line.
    """
    fixed = list(others)
    if not fixed:
        return x, y
    width, height = moving.w, moving.h
    candidates: list[tuple[float, int, int]] = []

    def consider(cx: int, cy: int, base: tuple[int, int] | None = None) -> None:
        """A candidate place. With ``base`` it is a line-up of the place ``base`` on the same edge."""
        placed = Rect(cx, cy, width, height)
        if any(placed.overlaps(rect) for rect in fixed):
            return
        distance = math.hypot(cx - x, cy - y)
        if base is not None:
            pull = abs(cx - base[0]) + abs(cy - base[1])
            if pull <= magnet:
                # As near as the place it was pulled from, and preferred to it; the nearest line-up wins.
                distance = min(distance, math.hypot(base[0] - x, base[1] - y)) - 0.5 - 0.4 / (1 + pull)
        candidates.append((distance, cx, cy))

    for rect in fixed:
        low, high = rect.y - height + 1, rect.bottom - 1
        lines = {rect.y, rect.bottom - height, rect.y + (rect.h - height) // 2}
        stops = {other.bottom for other in fixed} | {other.y - height for other in fixed}
        wanted = _clamp(y, low, high)
        for cx in (rect.x - width, rect.right):
            consider(cx, wanted)
            for cy in lines | stops:
                if low <= cy <= high:
                    consider(cx, cy, (cx, wanted) if cy in lines else None)
        low, high = rect.x - width + 1, rect.right - 1
        lines = {rect.x, rect.right - width, rect.x + (rect.w - width) // 2}
        stops = {other.right for other in fixed} | {other.x - width for other in fixed}
        wanted = _clamp(x, low, high)
        for cy in (rect.y - height, rect.bottom):
            consider(wanted, cy)
            for cx in lines | stops:
                if low <= cx <= high:
                    consider(cx, cy, (wanted, cy) if cx in lines else None)
    if not candidates:                             # cannot happen: beside the outermost rectangle is always free
        rightmost = max(fixed, key=lambda rect: rect.right)
        return rightmost.right, rightmost.y
    _, best_x, best_y = min(candidates)
    return best_x, best_y


def arrange(rects: Mapping[str, Rect], anchor: str) -> dict[str, Rect]:
    """``rects`` with every gap and overlap removed, moving as little as possible; ``anchor`` stays put.

    The rectangles are attached one by one to those already placed, the
    one that needs the smallest move first.
    """
    placed = {anchor: rects[anchor]}
    waiting = {name: rect for name, rect in rects.items() if name != anchor}
    while waiting:
        best = None
        for name in sorted(waiting):
            rect = waiting[name]
            if not any(rect.overlaps(other) for other in placed.values()) and \
                    any(rect.touches(other) for other in placed.values()):
                best = (0.0, name, rect.x, rect.y)
                break
            x, y = snap(rect, placed.values(), rect.x, rect.y)
            move = math.hypot(x - rect.x, y - rect.y)
            if best is None or move < best[0]:
                best = (move, name, x, y)
        _, name, x, y = best
        placed[name] = waiting.pop(name).at(x, y)
    return {name: placed[name] for name in rects}


def drop(rects: Mapping[str, Rect], name: str, x: int, y: int, magnet: int = 0) -> dict[str, Rect]:
    """The arrangement after ``name`` was dropped at (x, y): it snaps to the nearest legal place, and
    monitors it left behind close up. Positions stay within what the compositor accepts."""
    others = [rect for other, rect in rects.items() if other != name]
    moved = dict(rects)
    moved[name] = rects[name].at(*snap(rects[name], others, x, y, magnet))
    result = arrange(moved, name)
    low_x, low_y = min(r.x for r in result.values()), min(r.y for r in result.values())
    if (low_x < POSITION[0] or low_y < POSITION[0] or max(r.x for r in result.values()) > POSITION[1]
            or max(r.y for r in result.values()) > POSITION[1]):
        result = {key: rect.at(rect.x - low_x, rect.y - low_y) for key, rect in result.items()}
    return result


def is_arranged(rects: Mapping[str, Rect]) -> bool:
    """Whether the rectangles form one group without gaps or overlaps."""
    items = list(rects.values())
    if any(a.overlaps(b) for index, a in enumerate(items) for b in items[index + 1:]):
        return False
    if not items:
        return True
    reached, frontier = {0}, [0]
    while frontier:
        current = frontier.pop()
        for index, rect in enumerate(items):
            if index not in reached and items[current].touches(rect):
                reached.add(index)
                frontier.append(index)
    return len(reached) == len(items)
